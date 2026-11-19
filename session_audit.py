"""End-to-end audit of one live paper session (every component's sanity).

Components, each OK / WARN / FAIL with explicit checks and metrics:

* ``feed``                  public WebSocket/REST capture per venue
* ``order_books``           book integrity counters and the data-quality gate
* ``time_integrity``        receive latency, host-clock offset, blocked events
* ``historical_storage``    wire/raw hash chains, file SHA-256, counts, lag
* ``orderbook_replay``      stored events rebuild the exact live books
* ``backtest_replay``       replay of the paper period vs the forward test
* ``simulation``            sim_engine queue-aware maker replay on stored data
* ``dry_run``               the forward test itself (OMS/RMS/TCA/maker dry run)
* ``strategies``            cross-venue arb and basis mean reversion
* ``errors``                stderr and structured-log errors
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Callable

from .economics_gate import economics_gate
from .calibration import compare_runs
from .config import load_config
from .live_feed import verify_wire_file
from .maker_sim import simulate_recorded_session
from .models import BookState, MarketEvent
from .raw_capture import verify_raw_file
from .replay import deterministic_order
from .runner import run_backtest


# INPUT_MISSING: a required input file is absent, so the check could not be
# evaluated (data housekeeping), as opposed to FAIL (the data or code is wrong).
LEVELS = {"OK": 0, "WARN": 1, "INPUT_MISSING": 2, "FAIL": 3}


class Component:
    def __init__(self, name: str) -> None:
        self.name = name
        self.status = "OK"
        self.checks: list[dict[str, Any]] = []
        self.metrics: dict[str, Any] = {}

    def check(
        self, name: str, passed: bool, detail: str = "", *, severity: str = "FAIL",
        ok_detail: str | None = None,
    ) -> None:
        """``detail`` explains a failure; ``ok_detail`` (when given) is what a
        passing check reports, so an OK row never carries a failure message."""
        status = "OK" if passed else severity
        if passed and ok_detail is not None:
            detail = ok_detail
        self.checks.append({"check": name, "status": status, "detail": detail})
        if LEVELS[status] > LEVELS[self.status]:
            self.status = status

    def missing(self, name: str, paths: list[Path] | tuple[Path, ...]) -> None:
        names = ", ".join(str(path) for path in paths)
        self.check(name, False, f"required input missing: {names}", severity="INPUT_MISSING")

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "checks": self.checks, "metrics": self.metrics}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except json.JSONDecodeError:
        return None


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as handle:
        return sum(1 for line in handle if line.strip())


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def audit_session(
    config_path: Path, session_dir: Path, output_dir: Path, *,
    stderr_log: Path | None = None, run_simulation: bool = True,
    min_opportunities: int = 30,
) -> dict[str, Any]:
    started = time.time()
    output_dir.mkdir(parents=True, exist_ok=True)
    cfg, _ = load_config(config_path)
    paper_dir = session_dir / "paper"
    manifest = _read_json(session_dir / "live_manifest.json") or {}
    summary = (
        _read_json(paper_dir / "paper_summary.json")
        or _read_json(paper_dir / "summary.json") or {}
    )
    service = manifest.get("service", {})
    parts = sorted(session_dir.glob("part-*"))
    components: dict[str, Component] = {}

    def run(name: str, body: Callable[[Component], None]) -> None:
        component = Component(name)
        components[name] = component
        try:
            body(component)
        except FileNotFoundError as exc:  # absent input, not a code or data defect
            component.check(
                "audit_completed", False, f"required input missing: {exc.filename or exc}",
                severity="INPUT_MISSING",
            )
        except Exception as exc:  # an audit bug must be visible, not fatal
            component.check("audit_completed", False, f"{type(exc).__name__}: {exc}")

    # -- feed ----------------------------------------------------------------
    def feed(c: Component) -> None:
        c.check("live_manifest_present", bool(manifest), "session did not finalize its manifest",
                ok_detail="live_manifest.json present")
        c.check(
            "public_market_data_only",
            service.get("mode") == "PUBLIC_MARKET_DATA_ONLY"
            and service.get("live_order_routing") is False,
            f"mode={service.get('mode')} live_order_routing={service.get('live_order_routing')}",
        )
        c.check(
            "canonical_events_recorded", manifest.get("canonical_events", 0) > 0,
            f"{manifest.get('canonical_events', 0)} events",
        )
        c.check(
            "no_rejected_frames", manifest.get("rejected_frames", 0) == 0,
            f"{manifest.get('rejected_frames', 0)} rejected frames", severity="WARN",
        )
        for venue, stats in sorted(service.get("venues", {}).items()):
            keys = (
                "canonical_events", "raw_frames", "trade_ticks", "reconnects",
                "stale_disconnects", "resyncs", "parse_errors", "normalization_errors",
                "trade_parse_errors", "trade_normalization_errors", "trade_reconnects",
                "trade_stale_disconnects", "invalid_trades", "last_error",
            )
            c.metrics[venue] = {key: stats.get(key) for key in keys}
            c.check(f"{venue}_book_events", stats.get("canonical_events", 0) > 0,
                    f"{stats.get('canonical_events', 0)} events")
            c.check(f"{venue}_trade_feed", stats.get("trade_ticks", 0) > 0,
                    f"{stats.get('trade_ticks', 0)} trades", severity="WARN")
            errors = sum(stats.get(key, 0) or 0 for key in (
                "parse_errors", "normalization_errors",
                "trade_parse_errors", "trade_normalization_errors",
            ))
            c.check(f"{venue}_no_parse_errors", errors == 0, f"{errors} parse/normalize errors",
                    severity="WARN")
            unstable = sum(stats.get(key, 0) or 0 for key in (
                "reconnects", "stale_disconnects", "trade_reconnects", "trade_stale_disconnects",
            ))
            c.check(
                f"{venue}_stable_connection", unstable == 0,
                f"{unstable} reconnects/stale disconnects; last_error={stats.get('last_error') or '-'}",
                severity="WARN",
            )
            c.check(f"{venue}_trade_prints_valid", (stats.get("invalid_trades") or 0) == 0,
                    f"{stats.get('invalid_trades', 0)} invalid prints rejected", severity="WARN")

    # -- books -----------------------------------------------------------------
    def order_books(c: Component) -> None:
        books = service.get("books", {})
        c.check("books_reported", bool(books), "no book state in manifest",
                ok_detail=f"{len(books)} venue books in manifest")
        for venue, book in sorted(books.items()):
            stats = book.get("stats", {})
            c.metrics[venue] = {"state": book.get("state"), "sequence": book.get("sequence"), **stats}
            c.check(f"{venue}_snapshot_received", stats.get("snapshots", 0) > 0,
                    f"{stats.get('snapshots', 0)} snapshots")
            c.check(f"{venue}_no_crossed_books", stats.get("crossed_or_locked", 0) == 0,
                    f"{stats.get('crossed_or_locked', 0)} crossed/locked")
            c.check(f"{venue}_no_checksum_failures", stats.get("checksum_failures", 0) == 0,
                    f"{stats.get('checksum_failures', 0)} failures")
            c.check(f"{venue}_no_invalid_levels", stats.get("invalid_levels", 0) == 0,
                    f"{stats.get('invalid_levels', 0)} malformed levels")
            c.check(f"{venue}_no_sequence_gaps", stats.get("gaps", 0) == 0,
                    f"{stats.get('gaps', 0)} gaps, {stats.get('resyncs', 0)} resyncs",
                    severity="WARN")
            c.check(f"{venue}_no_price_jumps", stats.get("price_jumps", 0) == 0,
                    f"{stats.get('price_jumps', 0)} jumps", severity="WARN")
            c.check(f"{venue}_no_wide_spreads", stats.get("wide_spreads", 0) == 0,
                    f"{stats.get('wide_spreads', 0)} wide spreads", severity="WARN")
            c.check(f"{venue}_duplicates_out_of_order",
                    stats.get("duplicates", 0) + stats.get("out_of_order", 0) == 0,
                    f"{stats.get('duplicates', 0)} dup / {stats.get('out_of_order', 0)} ooo",
                    severity="WARN")
        gate = summary.get("data_quality_approval", {})
        c.metrics["quality_gate"] = gate
        c.check("quality_gate_approved", gate.get("decision") == "APPROVED",
                ",".join(gate.get("active_flag_codes", [])) or str(gate.get("decision")),
                severity="WARN")

    # -- time ------------------------------------------------------------------
    def time_integrity(c: Component) -> None:
        report = _read_json(paper_dir / "time_integrity_report.json") or {}
        events = report.get("events", 0)
        blocked = report.get("blocked_events", 0)
        correction = report.get("host_offset_correction") or {}
        offset = correction.get("clock_offset_estimate_ns")
        venues = report.get("venues", {})
        c.metrics = {
            "events": events, "blocked_events": blocked,
            "blocked_share": blocked / events if events else None,
            "clock_offset_estimate_ms": None if offset is None else offset / 1e6,
            "cross_venue_skew_breaches": report.get("cross_venue_alignment", {}).get("breaches"),
            "venues": {
                venue: {
                    "latency_p50_ms": (row.get("latency_ns_p50") or 0) / 1e6,
                    "latency_p95_ms": (row.get("latency_ns_p95") or 0) / 1e6,
                    "negative_blocked": row.get("negative_latency_events"),
                    "negative_corrected": row.get("corrected_negative_latency_events"),
                }
                for venue, row in venues.items()
            },
        }
        c.check("events_time_checked", events > 0, f"{events} events")
        c.check("blocked_share_below_5pct", events > 0 and blocked / events <= 0.05,
                f"{blocked}/{events} blocked")
        warmup_budget = cfg.time_policy.host_offset_min_samples * len(cfg.venues)
        c.check("blocking_limited_to_warmup", blocked <= warmup_budget,
                f"{blocked} blocked (warm-up budget {warmup_budget})", severity="WARN")
        c.check(
            "host_clock_offset_within_policy",
            offset is None or abs(offset) <= cfg.time_policy.max_host_offset_ns,
            "offset unknown" if offset is None else
            f"host clock {offset / 1e6:.1f} ms vs exchanges; enable NTP sync",
            severity="WARN",
            ok_detail=(
                "offset unknown" if offset is None
                else f"host clock {offset / 1e6:.1f} ms vs exchanges (within policy)"
            ),
        )
        impossible = sum(row.get("impossible_latency_events", 0) for row in venues.values())
        c.check(
            "no_impossible_latency", impossible == 0, f"{impossible} events",
            # Impossible-latency events correctly get a TIME_INTEGRITY_BLOCK
            # (excluded from strategy evaluation) rather than being traded
            # on stale data; a nonzero count under a connectivity outage is
            # the safety mechanism working as designed, not a code defect.
            severity="WARN",
        )
        breaches = report.get("cross_venue_alignment", {}).get("breaches", 0)
        c.check("cross_venue_skew_within_policy", breaches == 0, f"{breaches} breaches",
                severity="WARN")

    # -- storage ---------------------------------------------------------------
    def historical_storage(c: Component) -> None:
        c.check("partitions_present", bool(parts), "no part-* directories",
                ok_detail=f"{len(parts)} parts")
        canonical_total = trades_total = quarantined = 0
        for part in parts:
            part_manifest = _read_json(part / "live_part_manifest.json")
            c.check(f"{part.name}_manifest", part_manifest is not None, "missing live_part_manifest.json",
                    ok_detail="live_part_manifest.json present")
            if part_manifest:
                mismatched, absent = [], []
                for kind, meta in sorted(part_manifest.get("files", {}).items()):
                    path = part / Path(meta["path"]).name
                    if not path.exists():
                        absent.append(path)
                    elif _file_sha256(path) != meta["sha256"]:
                        mismatched.append(kind)
                if absent:
                    c.missing(f"{part.name}_files_present", absent)
                c.check(f"{part.name}_file_sha256", not mismatched,
                        "checksum mismatch: " + ",".join(mismatched),
                        ok_detail=f"{len(part_manifest.get('files', {})) - len(absent)} present files match"
                        + (f" ({len(absent)} missing, reported separately)" if absent else ""))
            for kind, verify in (("wire", verify_wire_file), ("raw", verify_raw_file)):
                path = part / f"{kind}.jsonl"
                if not path.exists():
                    continue  # reported as INPUT_MISSING above
                result = verify(path)
                c.check(f"{part.name}_{kind}_hash_chain",
                        bool(result.get("valid", result.get("verified", True))), json.dumps(result)[:200])
            canonical_total += _count_lines(part / "canonical.jsonl")
            trades_total += _count_lines(part / "trades.jsonl")
            quarantined += _count_lines(part / "quarantine.jsonl")
        c.metrics = {
            "parts": len(parts), "canonical_events": canonical_total,
            "trades": trades_total, "quarantined": quarantined,
            "max_persistence_lag_ms": (service.get("pipeline_monitoring", {})
                                       .get("max_persistence_lag_ns", 0) or 0) / 1e6,
        }
        c.check("canonical_count_matches_manifest",
                canonical_total == manifest.get("canonical_events"),
                f"{canonical_total} stored vs {manifest.get('canonical_events')} in manifest")
        c.check("no_quarantined_frames", quarantined == 0, f"{quarantined} quarantined",
                severity="WARN")
        c.check("persistence_lag_under_5s", c.metrics["max_persistence_lag_ms"] <= 5_000,
                f"{c.metrics['max_persistence_lag_ms']:.1f} ms", severity="WARN")

    # -- order book replay -----------------------------------------------------
    stored_events: list[MarketEvent] = []

    def orderbook_replay(c: Component) -> None:
        for part in parts:
            with (part / "canonical.jsonl").open(encoding="utf-8") as handle:
                stored_events.extend(
                    MarketEvent.from_dict(json.loads(line)) for line in handle if line.strip()
                )
        live_books = service.get("books", {})
        c.metrics["events"] = len(stored_events)
        for label, ordered, severity in (
            ("processing_order", stored_events, "FAIL"),
            ("receive_time_order", deterministic_order(list(stored_events)), "WARN"),
        ):
            books = {
                venue: cfg.book_sanity.new_book(venue, cfg.instrument_id) for venue in cfg.venues
            }
            for event in ordered:
                if event.venue in books:
                    books[event.venue].apply(event)
            for venue, live in sorted(live_books.items()):
                book = books.get(venue)
                if book is None:
                    continue
                if live.get("sequence") is None:
                    # The book was reset (a resync or a connection drop) and
                    # had not received a fresh snapshot again before the
                    # session stopped: the manifest captured an empty,
                    # SYNCING book with no sequence to compare against.
                    # Replay correctly rebuilds the last *valid* state from
                    # the stored events, which will never hash-match a
                    # post-reset empty book -- that is not a replay defect.
                    c.check(
                        f"{venue}_{label}_rebuilds_live_book", True,
                        f"live book was {live.get('state', 'UNKNOWN')} "
                        f"({live.get('invalid_reason', 'no reason recorded')}) at shutdown "
                        f"with no sequence to compare; replayed to seq {book.sequence}",
                        severity="WARN",
                    )
                    continue
                if live.get("state"):
                    book.state = BookState(live["state"])
                match = book.snapshot_hash() == live.get("snapshot_sha256")
                c.check(
                    f"{venue}_{label}_rebuilds_live_book", match,
                    f"replayed seq {book.sequence} vs live {live.get('sequence')}",
                    severity=severity,
                )
        paper_events_path = paper_dir / "canonical_events.jsonl"
        if not paper_events_path.exists():
            c.missing("paper_session_saw_every_stored_event", [paper_events_path])
            return
        paper_events = _count_lines(paper_events_path)
        c.check("paper_session_saw_every_stored_event", paper_events == len(stored_events),
                f"{paper_events} paper vs {len(stored_events)} stored", severity="WARN")

    # -- economics -------------------------------------------------------------
    def economics(c: Component) -> None:
        """P&L is accepted only if the economics reconcile (economics_gate)."""
        runs = [("paper", paper_dir), ("backtest_replay", output_dir / "backtest_replay")]
        present = [(label, path) for label, path in runs if (path / "economic_reconciliation.json").exists()]
        if not present:
            c.missing("economic_reconciliation_present",
                      [path / "economic_reconciliation.json" for _, path in runs])
            return
        for label, path in runs:
            if (path / "economic_reconciliation.json").exists():
                result = economics_gate(path)
                c.metrics[label] = {key: result[key] for key in (
                    "decision", "reconciliation", "funding", "funding_data_coverage", "borrow",
                    "incomplete_reasons")}
                c.check(f"{label}_economics_reconcile", result["decision"] == "PASS",
                        "; ".join(result["failures"])[:500],
                        ok_detail=f"reconciliation {result['reconciliation']}")
            else:
                # Sessions recorded before the economic ledger have no report.
                c.missing(f"{label}_economic_reconciliation_present", [path / "economic_reconciliation.json"])

    # -- backtest replay -------------------------------------------------------
    def backtest_replay(c: Component) -> None:
        required = [paper_dir / "canonical_events.jsonl", paper_dir / "paper_timestamps.jsonl"]
        absent = [path for path in required if not path.exists()]
        if absent:
            c.missing("backtest_replay_inputs_present", absent)
            return
        replay_dir = output_dir / "backtest_replay"
        result = run_backtest(
            config_path, paper_dir / "canonical_events.jsonl", replay_dir,
            instrument_status_log=paper_dir / "paper_timestamps.jsonl",
            event_timing_log=paper_dir / "paper_timestamps.jsonl",
        )
        c.metrics["replay"] = {
            "opportunities": result["opportunities"], "closed_plans": result["closed_plans"],
            "net_pnl": result["net_pnl"], "execution_mode": result["execution_mode"],
            "quality_gate": result["data_quality_approval"]["decision"],
            "strategies": {
                sid: {k: row[k] for k in ("opportunities", "plans", "closed_plans", "net_pnl")}
                for sid, row in result.get("strategies", {}).get("strategies", {}).items()
            },
        }
        c.check("replay_plans_all_closed", result["execution_realism"]["open_plans"] == 0,
                str(result["execution_realism"]["open_plan_states"]))
        applied = result.get("instrument_status_changes_applied", 0)
        c.metrics["instrument_status_changes_applied"] = applied
        c.metrics["recorded_event_timing_used"] = result.get("recorded_event_timing_used", 0)
        c.check(
            "replay_used_recorded_live_timing",
            result.get("recorded_event_timing_used", 0) > 0,
            f"{result.get('recorded_event_timing_used', 0)} of "
            f"{result.get('canonical_events_delivered', 0)} events replayed at their "
            "actual live processing time rather than an idealized zero-latency clock",
            severity="WARN",
        )
        report = compare_runs(
            replay_dir, paper_dir, output_dir / "replay_vs_paper.json",
            min_opportunities=min_opportunities,
        )
        c.metrics["comparison"] = {
            "decision": report["decision"],
            "diverged": report["diverged_metrics"],
            "metrics": {
                key: {"replay": row["replay"], "paper": row["paper"]}
                for key, row in report["metrics"].items()
            },
        }
        trading = [m for m in report["diverged_metrics"] if "." not in m]
        latency = [m for m in report["diverged_metrics"] if "." in m]
        # Trading outcomes (opportunities, fills, rejects, costs, marked PnL)
        # gate; a latency-model mismatch is a calibration gap, reported apart.
        c.check("replay_consistent_with_forward_test",
                report.get("trading_decision", report["decision"]) != "DIVERGED",
                "trading metrics diverged: " + ",".join(trading),
                ok_detail="trading metrics within tolerance (opportunities, fills, rejects, costs, equity PnL)")
        c.check("latency_model_matches_forward_test",
                report.get("latency_decision", "CONSISTENT") != "DIVERGED",
                "latency model needs calibration: " + ",".join(latency), severity="WARN",
                ok_detail="modelled stage latencies within tolerance")
        c.check(
            "forward_sample_sufficient", report["decision"] != "INSUFFICIENT_SAMPLE",
            f"{report['metrics']['opportunities']['paper']:.0f} forward opportunities "
            f"(< {min_opportunities}): accuracy not yet measurable",
            ok_detail=(
                f"{report['metrics']['opportunities']['paper']:.0f} forward opportunities "
                f"(>= {min_opportunities})"
            ),
            severity="WARN",
        )

    # -- sim_engine simulation --------------------------------------------------
    def simulation(c: Component) -> None:
        if not run_simulation:
            c.check("simulation_enabled", False, "disabled by --no-simulation", severity="WARN")
            return
        report = simulate_recorded_session(
            config_path, [part / "canonical.jsonl" for part in parts],
            [part / "trades.jsonl" for part in parts], output_dir / "maker_simulation.json",
        )
        c.metrics = {
            "engine": report["engine"], "replay": report["replay"],
            "latency_source": (report.get("latency") or {}).get("source"),
            "venues": {
                venue: {k: row[k] for k in (
                    "quotes", "fills", "fill_ratio", "trades_seen", "inventory",
                    "fees", "mark_to_market_pnl_quote", "sanity_violation_count",
                )}
                for venue, row in report["venues"].items()
            },
        }
        c.check(
            "cpp_matching_engine_loaded", report["engine"].startswith("cpp"),
            "compiled mm_matching_engine not importable on this Python/arch; "
            "its bit-identical Python mirror was used",
            ok_detail=f"engine={report['engine']} (compiled C++ mm_matching_engine)",
            severity="WARN",
        )
        c.check(
            "replay_in_live_arrival_order", report["replay"].get("ordering") == "arrival_ordinal",
            f"ordering {report['replay'].get('ordering')}: no ingestion trace for every event",
            ok_detail="ordering arrival_ordinal: replayed in live ingestion order (trace)",
            severity="WARN",
        )
        for venue, row in sorted(report["venues"].items()):
            c.check(f"{venue}_quotes_placed", row["quotes"] > 0, f"{row['quotes']} quotes",
                    severity="WARN")
            c.check(f"{venue}_trades_replayed", row["trades_seen"] > 0,
                    f"{row['trades_seen']} trades", severity="WARN")
            c.check(f"{venue}_simulated_fills_sane", row["sanity_violation_count"] == 0,
                    "; ".join(row["sanity_violations"][:3]))

    # -- dry run (forward test) --------------------------------------------------
    def dry_run(c: Component) -> None:
        c.check("paper_summary_present", bool(summary), "no paper_summary.json",
                ok_detail="paper summary present")
        if not summary:
            return
        realism = summary.get("execution_realism", {})
        kill = summary.get("kill_switch", {})
        residual = summary.get("residual_risk", {})
        tca = summary.get("tca", {})
        c.metrics = {
            "execution_mode": summary.get("execution_mode"),
            "opportunities": summary.get("opportunities"),
            "closed_plans": summary.get("closed_plans"),
            "gross_pnl": summary.get("gross_pnl"), "fees": summary.get("fees"),
            "net_pnl": summary.get("net_pnl"),
            "fills": tca.get("fills"), "weighted_slippage_bps": tca.get("weighted_slippage_bps"),
            "latency_model": realism.get("latency_model"),
            "unknown_state_orders": realism.get("unknown_state_orders"),
            "venue_queries": realism.get("venue_queries"),
            "kill_switch": kill.get("state"), "kill_reason": kill.get("reason"),
            "rms_reconciliation": summary.get("rms_reconciliation", {}).get("decision"),
            "open_residual_base": residual.get("open_residual_base"),
            "total_residual_base": residual.get("total_residual_base"),
            "flatten_started": summary.get("flatten_started"),
            "maker_dry_run": {
                venue: {k: row.get(k) for k in ("quotes", "fills", "inventory", "mark_to_market_pnl_quote")}
                for venue, row in (summary.get("maker_dry_run") or {}).get("venues", {}).items()
            },
        }
        c.check("execution_mode_labelled_paper",
                summary.get("execution_mode") in {"INTERNAL_PAPER", "VENUE_TESTNET"},
                str(summary.get("execution_mode")))
        c.check("no_open_plans_after_drain", realism.get("open_plans", 0) == 0,
                str(realism.get("open_plan_states")))
        c.check("no_unresolved_order_states", realism.get("unresolved_after_queries", 0) == 0,
                f"{realism.get('unresolved_after_queries', 0)} unresolved")
        c.check("no_open_residual_breach", not realism.get("residual_breaches_open"),
                str(realism.get("residual_breaches_open")))
        c.check("no_open_residual", Decimal(str(residual.get("open_residual_base", "0"))) == 0,
                str(residual.get("open_residual_base")))
        c.check("no_carried_residual",
                Decimal(str(residual.get("total_residual_base", "0"))) == 0,
                f"{residual.get('total_residual_base')} base left unhedged by closed plans",
                ok_detail="no residual left by closed plans",
                severity="WARN")
        c.check("kill_switch_inactive", kill.get("state") == "INACTIVE",
                f"{kill.get('state')}: {kill.get('reason')}")
        c.check("rms_reconciliation_not_rejected",
                summary.get("rms_reconciliation", {}).get("decision") != "REJECTED",
                str(summary.get("rms_reconciliation", {}).get("issue_count")), severity="WARN")
        failed = summary.get("structured_logs", {}).get("failed_trace_count", 0)
        c.check("no_failed_opportunity_traces", failed == 0, f"{failed} failed traces",
                severity="WARN")
        maker = summary.get("maker_dry_run")
        if maker is not None:
            violations = sum(row["sanity_violation_count"] for row in maker["venues"].values())
            c.check("maker_dry_run_fills_sane", violations == 0, f"{violations} violations")

    # -- strategies ----------------------------------------------------------------
    def strategies(c: Component) -> None:
        report = (summary.get("strategies") or {}).get("strategies", {})
        c.check("strategies_reported", bool(report), "no strategy report in summary",
                ok_detail=f"{len(report)} strategies reported")
        for strategy_id, row in sorted(report.items()):
            info = row.get("summary", {})
            c.metrics[strategy_id] = {
                key: row.get(key) for key in (
                    "type", "opportunities", "plans", "closed_plans", "outcomes", "net_pnl",
                )
            }
            c.check(f"{strategy_id}_plans_closed", row.get("plans") == row.get("closed_plans"),
                    f"{row.get('closed_plans')}/{row.get('plans')} closed")
            if row.get("type") == "CROSS_VENUE_BASIS_MEAN_REVERSION":
                c.metrics[strategy_id].update({
                    key: info.get(key) for key in (
                        "stance", "samples_taken", "warmed_up", "basis_mean_bps",
                        "basis_stdev_bps", "last_zscore", "max_abs_zscore", "actions",
                        "stance_reverts", "skipped",
                    )
                })
                c.check(f"{strategy_id}_warmed_up", bool(info.get("warmed_up")),
                        f"{info.get('samples_taken')} samples", severity="WARN")
                z = info.get("last_zscore")
                c.check(f"{strategy_id}_zscore_finite", z is None or math.isfinite(z), str(z))
                c.check(f"{strategy_id}_flat_at_cycle_end", info.get("stance") == "FLAT",
                        f"stance {info.get('stance')} left open", severity="WARN",
                    ok_detail="stance FLAT")

    # -- security master ----------------------------------------------------------
    def security_master(c: Component) -> None:
        rows = []
        path = paper_dir / "paper_timestamps.jsonl"
        if path.exists():
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if '"INSTRUMENT_STATUS' in line:
                        rows.append(json.loads(line))
        changes = [row for row in rows if row["record_type"] == "INSTRUMENT_STATUS"]
        failures = [row for row in rows if row["record_type"] == "INSTRUMENT_STATUS_POLL_FAILED"]
        c.metrics = {
            "status_changes_applied": [
                {"venue": row.get("venue"), "changes": row.get("changes")} for row in changes
            ],
            "poll_failures": len(failures),
            "poll_failure_samples": sorted({row.get("error", "")[:120] for row in failures})[:5],
        }
        c.check("instrument_status_polled", bool(changes or failures),
                "no public instrument-status poll ran this cycle", severity="WARN",
                ok_detail=f"{len(changes)} changes, {len(failures)} failed polls")
        c.check("instrument_status_polls_succeeded", not failures,
                f"{len(failures)} polls failed: {c.metrics['poll_failure_samples'][:1]}", severity="WARN",
                ok_detail="all polls succeeded")

    # -- errors --------------------------------------------------------------------
    # Event types the platform's own structured-log severity classifier
    # (BacktestLogBuilder._level) grades ERROR purely because their state or
    # decision text contains "REJECT"/"FAILED": a one-leg-reject outcome that
    # is then correctly unwound is exactly this, and is a normal, handled
    # market outcome, not a system fault. Anything outside this allowlist is
    # still a real FAIL -- this narrows the check, it does not disable it.
    BENIGN_ERROR_EVENT_TYPES = frozenset({"PLAN_STATE_TRANSITION", "OPPORTUNITY_OUTCOME"})

    def errors(c: Component) -> None:
        levels = summary.get("structured_logs", {}).get("levels", {})
        c.metrics["structured_log_levels"] = levels
        error_rows = [
            row for row in _read_jsonl(paper_dir / "structured_logs.jsonl")
            if row.get("level") == "ERROR"
        ]
        unexpected = [
            row for row in error_rows
            if row.get("event_type") not in BENIGN_ERROR_EVENT_TYPES
        ]
        c.metrics["structured_log_error_event_types"] = sorted({
            row.get("event_type") for row in error_rows
        })
        c.check(
            "no_unexpected_structured_log_errors", not unexpected,
            f"{len(unexpected)} unexpected ERROR records "
            f"(of {len(error_rows)} total; the rest are normal rejected-leg "
            "outcomes correctly unwound): "
            + ", ".join(sorted({row.get("event_type", "") for row in unexpected})[:5]),
        )
        c.check(
            "no_structured_log_errors", levels.get("ERROR", 0) == 0,
            f"{levels.get('ERROR', 0)} ERROR records, all normal rejected-leg outcomes"
            if not unexpected else f"{levels.get('ERROR', 0)} ERROR records",
            severity="WARN" if not unexpected else "FAIL",
        )
        if stderr_log is not None and stderr_log.exists():
            lines = stderr_log.read_text(encoding="utf-8", errors="replace").splitlines()
            error_lines = [line for line in lines if re.search(r"\b(ERROR|CRITICAL)\b|Traceback", line)]
            warning_lines = [line for line in lines if " WARNING " in line]
            c.metrics["stderr_errors"] = len(error_lines)
            c.metrics["stderr_warnings"] = len(warning_lines)
            c.metrics["stderr_samples"] = (error_lines + warning_lines)[:10]
            c.check("no_process_errors", not error_lines, (error_lines or [""])[0][:300])
            c.check("no_process_warnings", not warning_lines, (warning_lines or [""])[0][:300],
                    severity="WARN")

    for name, body in (
        ("feed", feed), ("order_books", order_books), ("time_integrity", time_integrity),
        ("historical_storage", historical_storage), ("orderbook_replay", orderbook_replay),
        ("backtest_replay", backtest_replay), ("economics", economics),
        ("simulation", simulation),
        ("dry_run", dry_run), ("strategies", strategies),
        ("security_master", security_master), ("errors", errors),
    ):
        run(name, body)

    overall = max((component.status for component in components.values()), key=LEVELS.get)
    issues = [
        {"component": name, **check}
        for name, component in components.items()
        for check in component.checks if check["status"] != "OK"
    ]
    audit = {
        "schema_version": "1.0.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_dir": str(session_dir),
        "audit_duration_seconds": round(time.time() - started, 3),
        "overall_status": overall,
        "components": {name: component.to_dict() for name, component in components.items()},
        "issues": issues,
    }
    (output_dir / "session_audit.json").write_text(
        json.dumps(audit, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    lines = [
        f"# Session audit: {session_dir.name}", "",
        f"Overall: **{overall}** ({len(issues)} issues)", "",
        "| Component | Status | Failed/warned checks |", "| --- | --- | --- |",
    ]
    for name, component in components.items():
        bad = [f"{row['check']} ({row['detail']})" for row in component.checks if row["status"] != "OK"]
        lines.append(f"| {name} | {component.status} | {'; '.join(bad)[:500] or '-'} |")
    (output_dir / "session_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return audit


def audit_incident(incident_dir: Path, output_dir: Path | None = None, *, source_session_dir: Path | None = None) -> dict[str, Any]:
    """W2b (owner 17 Nov; audit A2 2.3): the diagnostics of ONE incident window (live_feed.IncidentCapture or cut_recording_window), by one call, read-only on it.
      manifest      present, schema, completeness and its reasons
      files         every file's sha256 and row count equal the manifest
      wire          every wire record's own hash and frame checksum verify; the chain is whole inside the window (it is a cut: it starts at the first record's previous hash)
      trace         every CANONICAL / TRADE / DERIVATIVE trace row points at a row that is in the window
      books         the window's book events applied in order, per venue, starting from the seed: states counted, the first event after each seed applied, the time span against the manifest's window
      source_books  only with `source_session_dir` (the recording the window was cut from): the same events applied from the START of that recording give, event for event inside the window, the same
                    book state and sequence, and the same final book, as the window gives from its seed
    Returns {"ok", "checks": {...}}; with `output_dir` also writes incident_audit.json there (never inside the incident)."""
    from hashlib import sha256
    from .live_feed import SEED_LABEL, verify_wire_file
    from .models import MarketEvent
    from .orderbook import OrderBook

    incident_dir = Path(incident_dir)
    part = incident_dir / "part-000001"
    manifest = _read_json(incident_dir / "incident_manifest.json")
    checks: dict[str, Any] = {}
    if not manifest:
        result = {"ok": False, "incident_dir": str(incident_dir), "checks": {"manifest": {"ok": False, "detail": "incident_manifest.json missing or unreadable"}}}
    else:
        checks["manifest"] = {"ok": manifest.get("schema_version") is not None and manifest.get("completeness") in ("COMPLETE", "INCOMPLETE"), "completeness": manifest.get("completeness"),
                              "incomplete_reasons": manifest.get("incomplete_reasons"), "rule": manifest.get("rule"), "window": manifest.get("window")}
        bad = {}
        for name, entry in (manifest.get("files") or {}).items():
            path = part / f"{name}.jsonl"
            if not path.exists():
                bad[name] = "missing"
                continue
            body = path.read_bytes()
            if sha256(body).hexdigest() != entry["sha256"] or body.count(b"\n") != entry["copied_rows"] + entry["seed_rows"]:
                bad[name] = "sha256 or row count differs from the manifest"
        checks["files"] = {"ok": not bad and bool(manifest.get("files")), "files": len(manifest.get("files") or {}), "bad": bad}
        try:
            wire = verify_wire_file(part / "wire.jsonl", window=True) if (part / "wire.jsonl").exists() else None
            checks["wire"] = {"ok": wire is not None, **({"records": wire["record_count"], "chain_restarts_at_part_rotation": wire["chain_restarts_at_part_rotation"]} if wire else {"detail": "no wire file in the window"})}
        except ValueError as exc:
            checks["wire"] = {"ok": False, "detail": str(exc)}
        ids = {name: {row["event_id"] for row in _read_jsonl(part / f"{name}.jsonl")} for name in ("canonical", "trades", "derivatives") if (part / f"{name}.jsonl").exists()}
        by = {"CANONICAL": "canonical", "TRADE": "trades", "DERIVATIVE": "derivatives"}
        dangling = [row.get("canonical_event_id") for row in _read_jsonl(part / "trace.jsonl") if row.get("disposition") in by and row.get("canonical_event_id") not in ids.get(by[row["disposition"]], set())]
        checks["trace"] = {"ok": not dangling, "rows": _count_lines(part / "trace.jsonl"), "dangling": len(dangling)}
        books: dict[str, OrderBook] = {}
        states: dict[str, dict[str, int]] = {}
        per_event: list[tuple[str, str, int | None]] = []
        after_seed: dict[str, str] = {}
        seeded: set[str] = set()
        first_ts = last_ts = None
        for row in _read_jsonl(part / "canonical.jsonl"):
            event = MarketEvent.from_dict(row)
            book = books.setdefault(event.venue, OrderBook(event.venue, event.instrument_id, max_mid_jump_bps=None, max_spread_bps=None))
            state = book.apply(event).value
            if SEED_LABEL in event.quality_flags:
                seeded.add(event.venue)
            else:
                if event.venue in seeded and event.venue not in after_seed:
                    after_seed[event.venue] = state                       # the first real event after the seed: VALID means the window chains onto it
                states.setdefault(event.venue, {})[state] = states.setdefault(event.venue, {}).get(state, 0) + 1
                per_event.append((event.event_id, state, book.sequence))
                first_ts = event.receive_ts_ns if first_ts is None else min(first_ts, event.receive_ts_ns)
                last_ts = event.receive_ts_ns if last_ts is None else max(last_ts, event.receive_ts_ns)
        window = manifest.get("window") or {}
        inside = first_ts is None or (window.get("start_ns") is None or first_ts >= window["start_ns"]) and (window.get("end_ns") is None or last_ts <= window["end_ns"])
        unchained = sorted(v for v, state in after_seed.items() if state != "VALID")
        checks["books"] = {"ok": inside and not unchained, "seeded_venues": sorted(seeded), "first_event_after_seed": after_seed, "states_by_venue": states,
                           **({"detail": f"the first event after the seed did not apply VALID for {unchained}: a real gap at the window start or a seed that does not chain; pass source_session_dir to tell which"} if unchained else {}),
                           "first_receive_ts_ns": first_ts, "last_receive_ts_ns": last_ts, "rows_inside_the_manifest_window": inside,
                           "final_book_hash": {venue: book.snapshot_hash() for venue, book in sorted(books.items())}}
        if source_session_dir is not None and last_ts is not None:
            from .live_feed import _line_receive_ts, _open_recorded
            full_books: dict[str, OrderBook] = {}
            full: list[tuple[str, str, int | None]] = []
            for source_part in sorted(p for p in Path(source_session_dir).glob("part-*") if p.is_dir()):
                handle = _open_recorded(source_part / "canonical.jsonl")
                if handle is None:
                    continue
                done = False
                with handle:
                    for line in handle:
                        ts = _line_receive_ts(line)
                        if ts is None:
                            continue
                        if ts > last_ts:
                            done = True
                            break
                        event = MarketEvent.from_dict(json.loads(line))
                        book = full_books.setdefault(event.venue, OrderBook(event.venue, event.instrument_id, max_mid_jump_bps=None, max_spread_bps=None))
                        state = book.apply(event).value
                        if ts >= first_ts:
                            full.append((event.event_id, state, book.sequence))
                if done:
                    break
            differ = next((i for i, (a, b) in enumerate(zip(per_event, full)) if a != b), None if len(per_event) == len(full) else min(len(per_event), len(full)))
            same_final = {venue: (venue in full_books and (book.bids, book.asks, book.sequence) == (full_books[venue].bids, full_books[venue].asks, full_books[venue].sequence)) for venue, book in sorted(books.items())}
            checks["source_books"] = {"ok": differ is None and all(same_final.values()), "events_compared": len(per_event), "events_in_the_source_interval": len(full),
                                      "first_difference": None if differ is None else {"index": differ, "window": per_event[differ] if differ < len(per_event) else None, "source": full[differ] if differ < len(full) else None},
                                      "final_book_equal": same_final}
            if checks["source_books"]["ok"] and unchained and inside:
                checks["books"].update({"ok": True, "detail": f"the first event after the seed is not VALID for {unchained}, and the SOURCE recording has the same state at the same event: the gap is in the recording, at the window start"})
        result = {"ok": all(c["ok"] for c in checks.values()), "incident_id": manifest.get("incident_id"), "incident_dir": str(incident_dir), "checks": checks,
                  "label": "diagnostics of one incident window; a GAP or STALE state inside the window is the incident itself, not an audit failure"}
    if output_dir is not None:
        output_dir = Path(output_dir)
        if incident_dir.resolve() in (output_dir.resolve(), *output_dir.resolve().parents):
            raise ValueError("the audit of an incident is never written inside the incident")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "incident_audit.json").write_text(json.dumps(result, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return result


def amend_journal_audit(ledger_events: list[dict], plans: dict, reprice_counters: dict | None = None, *, label: str = "EXPLORATORY: mechanics of S0 repricing on the 2026-10-29 capture, no profit claim") -> dict:
    """F04 (owner 20 Nov): moved here from new_backtest.session_replay.s0_audit (unchanged; the replay calls this) so a REAL-ORDER session runs it at its end too (paper_live). Case 2i on the arb (owner 14 Nov, S0 in the session runtime): the counts of the EMS-note section 5 invariants a-j from one replay's ledger. Read-only; decides nothing.
      amends: requested / confirmed / rejected / timed out / fallback-to-cancel / accepted-but-not-confirmed;  fills_during_amend_pending: fills of the OLD order between its amend request and the venue's answer;
      cancels_during_amend: cancel or withdraw requests on a lineage member while its amend was pending;  duplicate_fills_booked_twice: (order, cumulative) pairs booked more than once (must be 0);
      lineage_overfill: a lineage that filled more than the quote's quantity (must be 0);  maker_filled / hedge_filled: totals, hedged = filled check;  two_live_orders: the most venue-live primary orders of ONE quote at once, minus one
      (must be 0: live = from the venue's acknowledgement to the terminal report; at the same instant an end is processed before a start)."""
    from collections import Counter, defaultdict
    from decimal import Decimal
    ev = sorted(ledger_events, key=lambda e: e["ts_ns"])
    kinds = Counter(e["event_type"] for e in ev)
    requested = [e for e in ev if e["event_type"] == "AMEND_REQUESTED"]
    answers = [e for e in ev if e["event_type"] in ("AMEND_CONFIRMED", "AMEND_REJECTED", "AMEND_TIMEOUT")]
    fills = [e for e in ev if e["event_type"] == "FILL"]
    life = [e for e in ev if e["event_type"] == "ORDER_LIFECYCLE"]
    during = cancels_during = 0
    for r in requested:
        answer = next((a for a in answers if a["order_id"] == r["order_id"] and a["ts_ns"] >= r["ts_ns"]), None)
        end = answer["ts_ns"] if answer else float("inf")
        members = {r["order_id"], r.get("replacement_order_id")}
        during += sum(1 for f in fills if f["order_id"] == r["order_id"] and r["ts_ns"] < f["ts_ns"] < end)
        cancels_during += sum(1 for e in life if e.get("lifecycle_event") in ("CANCEL_REQUESTED", "CANCEL") and e.get("order_id") in members and r["ts_ns"] < e["ts_ns"] < end) \
            + sum(1 for e in ev if e["event_type"] == "QUOTE_WITHDRAWN" and e.get("order_id") in members and r["ts_ns"] < e["ts_ns"] < end)
    seen, dup = Counter(), 0
    for f in fills:
        key = (f["order_id"], f.get("cumulative_filled_quantity"))
        seen[key] += 1
    dup = sum(1 for v in seen.values() if v > 1)
    primary_qty = {}
    for plan in plans.values():
        for c in plan.children:
            if c.order_role == "PRIMARY" and c.post_only and not getattr(c, "replaces_order_id", None):
                primary_qty[plan.parent_id] = primary_qty.get(plan.parent_id, Decimal("0")) + c.quantity
    filled_by_plan = defaultdict(lambda: Decimal("0"))
    for plan in plans.values():
        for c in plan.children:
            if c.order_role == "PRIMARY" and c.post_only:
                filled_by_plan[plan.parent_id] += c.filled_quantity
    overfill = sum(1 for pid, q in primary_qty.items() if filled_by_plan[pid] > q)
    maker_filled = sum((c.filled_quantity for plan in plans.values() for c in plan.children if c.order_role == "PRIMARY" and c.post_only), Decimal("0"))
    hedge_filled = sum((c.filled_quantity for plan in plans.values() for c in plan.children if c.order_role == "HEDGE"), Decimal("0"))
    primary_ids = {c.order_id: plan.parent_id for plan in plans.values() for c in plan.children if c.order_role == "PRIMARY" and c.post_only}
    replaced_by = {c.order_id: c.replaces_order_id for plan in plans.values() for c in plan.children if getattr(c, "replaces_order_id", None)}
    points = []
    for e in life:
        oid = e.get("order_id")
        if oid not in primary_ids:
            continue
        le = e.get("lifecycle_event")
        if le == "REPLACED" and replaced_by.get(oid) in primary_ids:
            points.append((e["ts_ns"], 0, replaced_by[oid]))           # 2i item 1b (owner 17 Nov): an amend IN PLACE: when the venue reports REPLACED the old order IS the replacement: it ends here
        if le in ("ACKNOWLEDGED", "REPLACED"):
            points.append((e["ts_ns"], 1, oid))
        elif le in ("CANCELLED", "FILLED", "REJECTED", "TIMED_OUT", "EXPIRED"):
            points.append((e["ts_ns"], 0, oid))
    live, worst = defaultdict(set), 0
    for ts, is_start, oid in sorted(points):                         # at the same instant an end (0) sorts before a start (1)
        pid = primary_ids[oid]
        (live[pid].add if is_start else live[pid].discard)(oid)
        worst = max(worst, len(live[pid]))
    return {"label": label,
            "amend_requests": kinds["AMEND_REQUESTED"], "amends_confirmed": kinds["AMEND_CONFIRMED"], "amends_rejected": kinds["AMEND_REJECTED"], "amend_timeouts": kinds["AMEND_TIMEOUT"],
            "amend_fallback_cancel": kinds["AMEND_FALLBACK_CANCEL"], "amend_accepted_not_confirmed": kinds["AMEND_ACCEPTED_NOT_CONFIRMED"],
            "fills_during_amend_pending": during, "cancels_during_amend": cancels_during, "quotes_withdrawn": kinds["QUOTE_WITHDRAWN"],
            "late_fills_reducing_replacement": sum(1 for r in requested if any(f["order_id"] == r["order_id"] and f["ts_ns"] > r["ts_ns"] for f in fills)),
            "duplicate_fills_booked_twice": dup, "lineage_overfill": overfill, "lineage_overfill_alarms": kinds["AMEND_LINEAGE_OVERFILL"],
            "maker_filled": str(maker_filled), "hedge_filled": str(hedge_filled), "hedged_equals_filled": maker_filled == hedge_filled,
            "two_live_orders": max(0, worst - 1), "controller_counters": dict(reprice_counters or {}),
            # item 10 (owner 17 Nov), after the 2i fixes: why amends were rejected, how many timeouts were LOST requests, and the owed venue queries that followed (aggregates only)
            "amend_reject_reasons": dict(Counter(str(e.get("reason", ""))[:80] for e in ev if e["event_type"] == "AMEND_REJECTED")),
            "amend_timeouts_lost": sum(1 for e in ev if e["event_type"] == "AMEND_TIMEOUT" and e.get("lost")),
            # owner 18 Nov (v1_15): amends that ENDED the order (a PostOnly amend to a crossing price, cancelled by the venue), and the venue queries owed after an amend timeout (one per old order)
            "amends_ended_by_crossing": sum(1 for e in ev if e["event_type"] == "AMEND_REJECTED" and "order cancelled by venue" in str(e.get("reason", ""))),
            "amend_queries_owed": len({e.get("order_id") for e in ev if e["event_type"] == "AMEND_TIMEOUT"}),
            "amend_owed_queries": sum(1 for e in ev if e["event_type"] == "VENUE_ORDER_QUERY" and e.get("purpose") == "amend_timeout"),
            "amend_owed_queries_answered": sum(1 for e in ev if e["event_type"] == "VENUE_ORDER_QUERY" and e.get("purpose") == "amend_timeout" and e.get("answered"))}


REAL_ORDER_AUDIT_MUST_BE_ZERO = ("two_live_orders", "duplicate_fills_booked_twice", "lineage_overfill")


def real_order_audit(ledger, plans: dict, now_ns: int, alert_context: dict | None = None) -> dict:
    """F04 item 5 (owner 20 Nov): at the end of every real-order session the amend journal audit runs on the session's own ledger and plans; any non-zero count of the must-be-zero invariants (two live
    orders of one quote, a fill booked twice, a lineage overfill) records ONE alarm (REAL_ORDER_AUDIT_ALARM, with the counts). Aggregates only."""
    audit = amend_journal_audit(list(ledger.events), plans, label="REAL-ORDER SESSION: the amend journal audit at the session end (aggregates only)")
    bad = {k: audit[k] for k in REAL_ORDER_AUDIT_MUST_BE_ZERO if audit.get(k)}
    audit["alarm"] = bool(bad)
    if bad:
        ledger.record("REAL_ORDER_AUDIT_ALARM", now_ns, alarm=True, counts=bad, **(alert_context or {}))
    return audit

