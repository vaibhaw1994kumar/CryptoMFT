"""Dataset quality manifest for every replay (market-data audit Step 14).

``dataset_manifest.json`` records which data a run used and how far it can be
trusted: venues, instruments and feeds; the time range (UTC nanoseconds); source
files and their checksums; schema and adapter versions; the instrument-metadata
version; the reconstruction rules applied; message and rejection counts;
sequence-gap and recovery statistics; every interval in which a venue's book was
not VALID; timestamp anomalies and latency; attached source comparisons; and
every exclusion or repair. Nothing is repaired silently: the only change replay
makes to the input order (within-stream re-placement after a host clock step) is
counted here, and the original file is untouched.

Invalid intervals come from an independent pass that rebuilds each venue's book
from the dataset alone, so they describe the data, not one strategy's run.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
from typing import Any

from . import __version__
from .models import BookState, MarketEvent, canonical_json
from .replay import event_order_key

MAX_INTERVALS = 500

RECONSTRUCTION = {
    "BINANCE_SPOT": "REST /api/v3/depth snapshot (lastUpdateId) with depth@100ms buffered from before it; "
                    "first delta must bridge (U <= lastUpdateId+1 <= u); continuity U == previous u + 1",
    "BINANCE_USDM": "REST /fapi/v1/depth snapshot with buffered depth@100ms; first delta must bridge; "
                    "continuity pu == previous u",
    "BYBIT": "v5 orderbook.N websocket snapshot then deltas; continuity u == previous u + 1 (observed; "
             "a new snapshot - reconnect or u=1 restart - replaces the book)",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _iso(ns: int | None) -> str | None:
    return None if ns is None else datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat()


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _source_sha256() -> str:
    """Hash of the package's own source files: identifies the code that ran, with or without git."""
    digest = hashlib.sha256()
    package = Path(__file__).parent
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(package).as_posix()
        if "/tests/" in f"/{relative}" or relative.startswith(("crypto_MFT_arb_platform/", "src/")):
            continue                                     # nested copies, not the running package
        digest.update(relative.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _git_revision() -> dict[str, Any]:
    """Revision of the checkout the package sits in; ``dirty`` means it differs from that commit."""
    import os

    if os.environ.get("HFT_PLATFORM_KIT_GIT_REVISION"):
        return {"revision": os.environ["HFT_PLATFORM_KIT_GIT_REVISION"], "dirty": None, "source": "environment"}
    package = Path(__file__).parent
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=package, capture_output=True, text=True,
                              timeout=5, check=False)
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=package,
                                capture_output=True, text=True, timeout=5, check=False)
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=package, capture_output=True,
                             text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return {"revision": None, "dirty": None, "source": "unavailable"}
    if head.returncode != 0:
        return {"revision": None, "dirty": None, "source": "not a git checkout"}
    return {"revision": head.stdout.strip(), "dirty": bool(status.stdout.strip()),
            "checkout": top.stdout.strip() or None, "source": "git"}


def _live_provenance(events_path: Path) -> dict[str, Any] | None:
    """The live session a part's canonical.jsonl came from, if the file sits inside one."""
    for parent in list(events_path.resolve().parents)[:4]:
        manifest = _read(parent / "live_manifest.json")
        if manifest is None:
            continue
        quarantined = {}
        for part in sorted(parent.glob("part-*")):
            path = part / "quarantine.jsonl"
            if path.exists():
                quarantined[part.name] = sum(1 for line in path.open(encoding="utf-8") if line.strip())
        return {"session_dir": str(parent), "session_id": manifest.get("session_id"),
                "session_sha256": manifest.get("session_sha256"),
                "wire_records": manifest.get("wire_records"), "rejected_frames": manifest.get("rejected_frames"),
                "quarantined_by_part": quarantined}
    return None


def _feed(events: list[MarketEvent], venue: str) -> str:
    if venue == "BINANCE":
        futures = any(isinstance(e.native, dict) and "pu" in e.native for e in events if e.venue == venue)
        return "BINANCE_USDM" if futures else "BINANCE_SPOT"
    return venue


def _venue_blocks(events: list[MarketEvent], cfg) -> tuple[dict[str, Any], dict[str, Any], dict[str, list]]:
    """Per-venue description, book statistics and not-VALID intervals (independent rebuild)."""
    describe: dict[str, dict[str, Any]] = {}
    books, intervals, open_interval = {}, {}, {}
    silent, last_book_receive = {}, {}
    for event in events:
        row = describe.setdefault(event.venue, {
            "instrument_ids": set(), "event_types": {}, "adapter_versions": set(), "schema_versions": set(),
            "first_exchange_ts_ns": None, "last_exchange_ts_ns": None,
            "first_receive_ts_ns": None, "last_receive_ts_ns": None, "with_raw_ref": 0, "events": 0,
        })
        row["events"] += 1
        row["instrument_ids"].add(event.instrument_id)
        row["event_types"][event.event_type.value] = row["event_types"].get(event.event_type.value, 0) + 1
        row["adapter_versions"].add(event.adapter_version)
        row["schema_versions"].add(event.schema_version)
        row["with_raw_ref"] += bool(event.raw_ref)
        for key, value in (("exchange", event.exchange_ts_ns), ("receive", event.receive_ts_ns)):
            first, last = f"first_{key}_ts_ns", f"last_{key}_ts_ns"
            row[first] = value if row[first] is None else min(row[first], value)
            row[last] = value if row[last] is None else max(row[last], value)
        if event.event_type.value not in ("BOOK_SNAPSHOT", "BOOK_DELTA"):
            continue
        key = (event.venue, event.instrument_id)
        previous = last_book_receive.get(key)
        if previous is not None and event.receive_ts_ns - previous > cfg.max_book_age_ns:
            silent.setdefault(event.venue, []).append({
                "instrument_id": event.instrument_id, "start_receive_ts_ns": previous,
                "end_receive_ts_ns": event.receive_ts_ns, "duration_ns": event.receive_ts_ns - previous,
                "resumed_with": event.event_type.value, "resume_raw_ref": event.raw_ref})
        last_book_receive[key] = max(previous or 0, event.receive_ts_ns)
        book = books.get(key)
        if book is None:
            book = books[key] = cfg.book_sanity.new_book(event.venue, event.instrument_id)
        state = book.apply(event)
        current = open_interval.get(key)
        if state != BookState.VALID and current is None:
            open_interval[key] = {"instrument_id": event.instrument_id, "start_receive_ts_ns": event.receive_ts_ns,
                                  "start_raw_ref": event.raw_ref, "reasons": {book.invalid_reason or state.value},
                                  "events": 1}
        elif state != BookState.VALID:
            current["events"] += 1
            current["reasons"].add(book.invalid_reason or state.value)
        elif current is not None:
            current["end_receive_ts_ns"] = event.receive_ts_ns
            intervals.setdefault(event.venue, []).append(open_interval.pop(key))
    for (venue, _), current in open_interval.items():
        current["end_receive_ts_ns"] = None                      # never recovered within the dataset
        intervals.setdefault(venue, []).append(current)
    for venue_intervals in intervals.values():
        for row in venue_intervals:
            row["reasons"] = sorted(row["reasons"])
            end = row["end_receive_ts_ns"]
            row["duration_ns"] = None if end is None else end - row["start_receive_ts_ns"]
    stats = {venue: {} for venue in describe}
    for (venue, instrument), book in books.items():
        s = book.stats
        stats[venue][instrument] = {"final_state": book.state.value, "snapshots": s.snapshots,
                                    "deltas_applied": s.deltas_applied, "gaps": s.gaps, "resyncs": s.resyncs,
                                    "duplicates": s.duplicates, "out_of_order": s.out_of_order,
                                    "crossed_or_locked": s.crossed_or_locked, "invalid_levels": s.invalid_levels,
                                    "price_jumps": s.price_jumps, "wide_spreads": s.wide_spreads}
    for venue, row in describe.items():
        row["feed"] = _feed(events, venue)
        row["reconstruction"] = RECONSTRUCTION.get(row["feed"])
        row["instrument_ids"] = sorted(row["instrument_ids"])
        row["adapter_versions"] = sorted(row["adapter_versions"])
        row["schema_versions"] = sorted(row["schema_versions"])
        for key in ("first_exchange_ts", "last_exchange_ts", "first_receive_ts", "last_receive_ts"):
            row[f"{key}_utc"] = _iso(row[f"{key}_ns"])
    return describe, stats, intervals, silent


def build_dataset_manifest(
    *, cfg, raw_config: dict, config_path: Path, events_path: Path, events: list[MarketEvent],
    output_dir: Path, derivatives_log: Path | None = None, comparison_reports: list[Path] | None = None,
) -> dict[str, Any]:
    describe, book_stats, intervals, silent = _venue_blocks(events, cfg)
    reordered = sum(1 for a, b in zip(sorted(events, key=event_order_key), events) if a is not b)
    quality = _read(output_dir / "data_quality_report.json") or {}
    timing = _read(output_dir / "time_integrity_report.json") or {}
    override = _read(output_dir / "data_quality_override.json")
    inputs = {"events": {"path": str(events_path), "sha256": _sha256(events_path), "events": len(events)}}
    if derivatives_log is not None:
        inputs["derivatives"] = {"path": str(derivatives_log), "sha256": _sha256(derivatives_log)}
    comparisons = []
    for path in comparison_reports or ():
        report = _read(Path(path)) or {}
        comparisons.append({"path": str(path), "sha256": _sha256(Path(path)), "venue": report.get("venue"),
                            "samples": report.get("samples"), "mismatch_share": report.get("mismatch_share"),
                            "reference": report.get("inputs", {}).get("tardis_csv")})
    instruments = [x for x in raw_config.get("instruments", [])]
    manifest = {
        "schema_version": "1.0.0",
        "timestamp_convention": "integer nanoseconds since the Unix epoch, UTC; *_utc fields are ISO-8601 UTC",
        "dataset": {
            "dataset_sha256": hashlib.sha256(
                "".join(v["sha256"] for _, v in sorted(inputs.items())).encode()).hexdigest(),
            "inputs": inputs,
            "live_provenance": _live_provenance(events_path),
            "window": {
                "first_receive_ts_ns": min((v["first_receive_ts_ns"] for v in describe.values()), default=None),
                "last_receive_ts_ns": max((v["last_receive_ts_ns"] for v in describe.values()), default=None),
            },
        },
        "venues": describe,
        "instrument_metadata": {
            "sha256": hashlib.sha256(canonical_json(instruments).encode()).hexdigest(),
            "fees_sha256": hashlib.sha256(canonical_json(raw_config.get("fees", [])).encode()).hexdigest(),
            "rows": [{k: x.get(k) for k in ("venue", "instrument_id", "symbol", "product_type", "effective_from")}
                     for x in instruments],
        },
        "replay_ordering": "receive_ts_ns total order across streams; within a book stream, deltas between "
                           "snapshots in venue sequence order (replay.deterministic_order)",
        "message_counts": {
            "by_venue": {venue: row["event_types"] for venue, row in describe.items()},
            "quality_gate_counts": quality.get("counts"),
        },
        "sequence_and_recovery": book_stats,
        "invalid_intervals": {venue: rows[:MAX_INTERVALS] for venue, rows in intervals.items()},
        "invalid_intervals_truncated": {venue: max(0, len(rows) - MAX_INTERVALS) for venue, rows in intervals.items()},
        # Missing-data intervals: no book update for longer than the configured max book age.
        # A reconnect leaves no sequence gap in the data, only this silence.
        "silent_intervals": {venue: rows[:MAX_INTERVALS] for venue, rows in silent.items()},
        "silent_interval_threshold_ns": cfg.max_book_age_ns,
        "timestamp_anomalies": {
            venue: {k: row.get(k) for k in ("negative_latency_events", "corrected_negative_latency_events",
                                            "impossible_latency_events", "feed_delayed_events",
                                            "latency_ns_p50", "latency_ns_p95", "latency_ns_max",
                                            "max_corrected_delay_ns")}
            for venue, row in (timing.get("venues") or {}).items()
        } | {"clock_offset_estimate_ns": (timing.get("host_offset_correction") or {}).get("clock_offset_estimate_ns"),
             "host_clock_state": (timing.get("host_clock") or {}).get("state")},
        "source_comparisons": comparisons,
        "exclusions_and_repairs": {
            "exclusion_policy": "no interval is removed: invalid intervals are replayed with decisions blocked "
                                "by the book gate and are listed under invalid_intervals",
            "excluded_intervals": [],
            "repairs": ([{"type": "REPLAY_REORDER_WITHIN_STREAM", "events_moved": reordered,
                          "reason": "host receive clock stepped backwards; deltas re-placed in venue sequence "
                                    "order on the same time slots; the input file is unchanged"}]
                        if reordered else []),
            "truncation": "depth as recorded: Bybit orderbook.N is venue-maintained at N levels; "
                          "Binance keeps every level seen since the snapshot",
            "data_quality_decision": quality.get("decision"),
            "data_quality_override": override,
        },
        "run": {
            "config_path": str(config_path), "config_sha256": _sha256(config_path),
            "platform_version": __version__, "source_sha256": _source_sha256(), "git": _git_revision(),
            "python": sys.version.split()[0], "host": platform.platform(),
        },
    }
    manifest["manifest_sha256"] = hashlib.sha256(canonical_json(manifest).encode()).hexdigest()
    return manifest


def write_dataset_manifest(output_dir: Path, manifest: dict[str, Any]) -> Path:
    path = Path(output_dir) / "dataset_manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return path


def record_override(output_dir: Path, override: dict[str, Any]) -> None:
    """A REJECTED dataset used anyway: the justification belongs in the manifest too."""
    path = Path(output_dir) / "dataset_manifest.json"
    manifest = _read(path)
    if manifest is None:
        return
    manifest.pop("manifest_sha256", None)
    manifest["exclusions_and_repairs"]["data_quality_override"] = override
    manifest["manifest_sha256"] = hashlib.sha256(canonical_json(manifest).encode()).hexdigest()
    write_dataset_manifest(output_dir, manifest)
