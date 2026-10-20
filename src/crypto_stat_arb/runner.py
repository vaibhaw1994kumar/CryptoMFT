from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import time

from .funding import is_perpetual
from .config import load_config
from .economics import Valuation, reconcile
from .canonical import CallbackEventSink, CanonicalEventDispatcher, CanonicalEventRecorder
from .models import MarketEvent, canonical_json, utc_now_iso
from .replay import deterministic_order
from .quality import QualityGate, QualitySnapshot
from .session import ExecutionMode, TradingSession
from .monitoring import PipelineMonitor
from .tca import BasicTCA
from .structured_logging import BacktestLogBuilder, StructuredLogStore


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_events(path: Path) -> list[MarketEvent]:
    with path.open(encoding="utf-8") as handle:
        return [MarketEvent.from_dict(json.loads(line)) for line in handle if line.strip()]


def _valuation(session: TradingSession, marks: dict, reporting: str) -> Valuation:
    valuation = Valuation(reporting_currency=reporting)
    for (venue, instrument_id), mark in marks.items():
        spec = session.cfg.instruments.get(venue, instrument_id)
        if spec.quote == reporting and is_perpetual(spec.product_type):
            # A linear contract is worth mark x multiplier of the quote.
            valuation.prices[(venue, spec.instrument_id)] = mark * spec.contract_multiplier
            valuation.sources[(venue, spec.instrument_id)] = (
                f"mid {instrument_id} x multiplier (approximation of the mark price)"
            )
        elif spec.quote == reporting:
            valuation.prices[(venue, spec.base)] = mark
            valuation.sources[(venue, spec.base)] = f"mid {instrument_id}"
    for (venue, asset), price in session.oms.asset_prices.items():
        for target in (session.cfg.venues if venue == "*" else (venue,)):
            valuation.prices.setdefault((target, asset), price)
            valuation.sources.setdefault((target, asset), "configured asset price")
    return valuation


def write_economic_artifacts(
    session: TradingSession, closing_marks: dict, account_state: dict, output_dir: Path,
    reporting_currency: str = "USDT",
) -> dict:
    """Economic event log + closing-equity reconciliation (ECO06 gate)."""
    ledger = session.ledger.economics
    ledger.write_jsonl(output_dir / "economic_events.jsonl")
    funding = session.funding.report(closing_marks)
    (output_dir / "funding_report.json").write_text(
        json.dumps(funding, indent=2, sort_keys=True), encoding="utf-8"
    )
    borrow = session.borrow.report()
    (output_dir / "borrow_report.json").write_text(
        json.dumps(borrow, indent=2, sort_keys=True), encoding="utf-8"
    )
    venues = account_state.get("venues", {})
    report = reconcile(
        ledger,
        opening=_valuation(session, session.opening_marks, reporting_currency),
        closing=_valuation(session, closing_marks, reporting_currency),
        trade_pnl_by_venue={
            venue: Decimal(row["realized_pnl"]) + Decimal(row["unrealized_pnl"])
            for venue, row in venues.items()
        },
        legacy_fees_by_venue={venue: Decimal(row["fees"]) for venue, row in venues.items()},
        plan_net_pnl=sum((plan.net_pnl for plan in session.oms.plans.values()), Decimal("0")),
        product_types={
            venue: session.cfg.instruments.get(venue, session.cfg.instrument_id).product_type
            for venue in session.cfg.venues
        },
        funding_modelled_venues=[venue for venue, _ in session.funding.specs],
        funding_flags_by_venue=session.funding.blocking_flags_by_venue(),
        legacy_funding_by_venue={venue: Decimal(row.get("funding", "0")) for venue, row in venues.items()},
        borrow_status_by_venue=session.borrow.status_by_venue(
            session.cfg.venues, [venue for venue, _ in session.funding.specs],
        ),
        borrow_flags_by_venue=session.borrow.blocking_flags_by_venue(),
        borrow_notes_by_venue=session.borrow.notes_by_venue(),
        legacy_borrow_cost_by_venue={venue: Decimal(row.get("borrow_cost", "0")) for venue, row in venues.items()},
        estimated_by_venue=session.funding.estimated_by_venue(),
    )
    report["borrow"] = {"status": borrow["status"], "report": "borrow_report.json"}
    report["funding"] = {
        "status": funding["status"],
        "settlements_booked": funding.get("settlements_booked", 0),
        "report": "funding_report.json",
    }
    report["limitations"] = [
        (
            "Funding is settled from rate records for configured perpetuals (funding_report.json)."
            if session.funding.configured else
            "Funding does not apply: no perpetual instruments are configured."
        ),
        (
            "Spot borrowing is booked for configured borrow lines (borrow_report.json); quote borrowing for margin buys is not modelled."
            if session.borrow.configured else
            "No borrow lines are configured: spot shorts beyond held base are refused (borrow N/A)."
            if session.borrow.unfinanced_short_policy == "FAIL" else
            "Unconstrained inventory: spot shorts are admitted without financing (borrow NOT_MODELLED)."
        ),
        "Transfers are not modelled: no events are generated (reported NOT_MODELLED, not zero).",
        *(
            ["Perpetual margin is not modelled: contract positions are valued at mid x multiplier (ECO04)."]
            if session.funding.configured else []
        ),
        "The maker dry-run simulator's fills are a separate harness and are not booked in this ledger.",
        "Venue-reported equity is not available in simulation or internal paper mode.",
    ]
    (output_dir / "economic_reconciliation.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    return {
        "status": report["status"],
        "report": "economic_reconciliation.json",
        "events": "economic_events.jsonl",
        "identity_residual": report["consolidated"]["identity_residual"],
        "strategy_equity_pnl": report["consolidated"]["strategy_equity_pnl"],
        "failed_checks": [c["check"] for c in report["checks"] if not c["passed"]],
        "funding": funding["status"],
        "borrow": borrow["status"],
        "estimated": report["estimated"],
    }


def write_session_artifacts(
    session: TradingSession,
    *,
    events: list[MarketEvent],
    output_dir: Path,
    correlation_id: str,
    source_path: str,
    source_sha256: str,
    replay_duration_ns: int,
    canonical_events_delivered: int,
    scope: str = "BACKTEST_DATASET",
    monitor_mode: str = "REPLAY",
    job_mode: str = "BACKTEST",
    clock_label: str = "RECORDED_RECEIVE_TIME",
    live_metrics: dict | None = None,
    invalid_trades: int = 0,
    instrument_status_changes_applied: int = 0,
    recorded_event_timing_used: int = 0,
    limitations: tuple[str, ...] = (
        "Host clock state is not sampled during recorded-time replay.",
        "Approval covers the supplied canonical event file and configured instruments only.",
    ),
) -> dict:
    """Write every run artifact for a completed (already drained) session.

    Shared by backtest replay and live paper so both produce identical,
    comparable reports.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    cfg = session.cfg
    ledger = session.ledger
    risk = session.risk
    alignment_policy = session.alignment_policy
    oms = session.oms
    books = session.books
    book_gate = session.book_gate
    time_monitor = session.time_monitor
    quality_gate = QualityGate(cfg.quality_thresholds)
    data_quality = {"events": len(events), "gaps": 0, "invalid_books": 0}
    live_metrics = live_metrics or {
        "reconnects": 0, "stale_disconnects": 0, "persistence_lag_ns": 0,
    }
    execution_realism = oms.execution_realism_snapshot()
    latency_report = session.latency_report()
    (output_dir / "latency_report.json").write_text(
        json.dumps(latency_report, indent=2, sort_keys=True), encoding="utf-8"
    )
    (output_dir / "order_timestamps.jsonl").write_text(
        "".join(
            json.dumps(row, sort_keys=True) + "\n" for row in session.order_timestamps()
        ),
        encoding="utf-8",
    )

    time_report = time_monitor.write_report(output_dir / "time_integrity_report.json")
    mark_prices = {}
    for key, book in books.items():
        if book.bids and book.asks:
            mark_prices[key] = (book.best_bid + book.best_ask) / Decimal("2")
    account_state = oms.write_account_state(
        output_dir / "account_state.json",
        mark_prices=mark_prices,
        initial_capital_by_venue=cfg.initial_capital_by_venue,
    )
    economics = write_economic_artifacts(session, mark_prices, account_state, output_dir)
    exposure_utilization = risk.update_exposure_state(
        plans=oms.plans, ledger=ledger,
        now_ns=(events[-1].receive_ts_ns if events else 0),
        mark_prices=mark_prices,
    )
    (output_dir / "exposure_utilization.json").write_text(
        json.dumps(exposure_utilization, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    margin_status = oms.write_margin_status(
        output_dir / "margin_status.json",
        now_ns=(events[-1].receive_ts_ns if events else 0),
        mark_prices=mark_prices,
        initial_capital_by_venue=cfg.initial_capital_by_venue,
    )
    if risk.last_pretrade_validation is not None:
        risk.last_pretrade_validation.write(
            output_dir / "pretrade_validation.json"
        )
    reconciliation_report = oms.reconcile_risk_state(
        now_ns=(events[-1].receive_ts_ns if events else 0),
        require_venue_orders=False,
        require_complete_opportunities=True,
        report_path=output_dir / "rms_reconciliation_report.json",
    )
    kill_switch_status = oms.kill_switch_snapshot()
    (output_dir / "kill_switch_status.json").write_text(
        json.dumps(kill_switch_status, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    ledger.write_jsonl(output_dir / "ledger.jsonl")
    execution_trace = oms.write_execution_trace(output_dir / "order_trace.jsonl")
    data_quality.update({
        "gaps": sum(book.stats.gaps for book in books.values()),
        "invalid_books": sum(
            book.stats.checksum_failures + book.stats.crossed_or_locked
            for book in books.values()
        ),
        "duplicates": sum(book.stats.duplicates for book in books.values()),
        "out_of_order": sum(book.stats.out_of_order for book in books.values()),
        "checksum_failures": sum(book.stats.checksum_failures for book in books.values()),
        "crossed_or_locked": sum(book.stats.crossed_or_locked for book in books.values()),
        "stale_transitions": sum(book.stats.stale_transitions for book in books.values()),
        "resyncs": sum(book.stats.resyncs for book in books.values()),
        "price_jumps": sum(book.stats.price_jumps for book in books.values()),
        "confirmed_price_moves": sum(
            book.stats.confirmed_price_moves for book in books.values()
        ),
        "wide_spreads": sum(book.stats.wide_spreads for book in books.values()),
        "invalid_levels": sum(book.stats.invalid_levels for book in books.values()),
        "silent_feeds": session.silent_feeds(),
    })
    venue_time = time_report["venues"]
    quality_snapshot = QualitySnapshot(
        events=len(events),
        gaps=data_quality["gaps"],
        stale_intervals=data_quality["stale_transitions"],
        crossed_books=data_quality["crossed_or_locked"],
        duplicates=data_quality["duplicates"],
        out_of_order=data_quality["out_of_order"],
        negative_latency=sum(row["negative_latency_events"] for row in venue_time.values()),
        impossible_latency=sum(row["impossible_latency_events"] for row in venue_time.values()),
        price_jumps=data_quality["price_jumps"],
        wide_spreads=data_quality["wide_spreads"],
        invalid_levels=data_quality["invalid_levels"],
        silent_feeds=len(data_quality["silent_feeds"]),
        invalid_trades=invalid_trades,
        corrected_negative_latency=sum(
            row["corrected_negative_latency_events"] for row in venue_time.values()
        ),
    )
    quality_report = quality_gate.write_artifacts(
        output_dir / "data_quality_report.json",
        output_dir / "data_quality_defects.jsonl",
        quality_snapshot,
        scope=scope,
        correlation_id=correlation_id,
        limitations=limitations,
    )
    tca_report, tca_daily_paths = BasicTCA.write_run_and_daily(
        oms=oms, events=events, output_dir=output_dir,
        correlation_id=correlation_id, source_path=source_path,
        source_sha256=source_sha256,
    )
    structured_store = StructuredLogStore(
        output_dir / "structured_logs.jsonl",
        output_dir / "structured_logs.sqlite",
    )
    structured_log_stats = structured_store.replace(BacktestLogBuilder.build(
        correlation_id=correlation_id, events=events, ledger=ledger,
        router=oms.router, plans=oms.plans.values(),
    ))
    failed_trace_count = structured_store.write_failed_opportunity_traces(
        oms.plans.values(), output_dir / "failed_opportunity_traces.jsonl"
    )
    structured_log_stats["failed_trace_count"] = failed_trace_count
    structured_log_stats["failed_traces"] = "failed_opportunity_traces.jsonl"
    monitor_now_ns = events[-1].receive_ts_ns if events else 0
    monitor = PipelineMonitor(cfg.monitoring_thresholds)
    strategy_execution = monitor.strategy_execution_snapshot(
        oms, account_state=account_state, margin_status=margin_status,
        kill_switch_status=kill_switch_status,
    )
    monitoring_report = monitor.build(
        mode=monitor_mode, generated_ts_ns=monitor_now_ns,
        correlation_id=correlation_id,
        job={"state": "COMPLETE", "mode": job_mode},
        data={
            "event_count": len(events),
            "latest_receive_ts_ns": monitor_now_ns if events else None,
            "clock": clock_label,
        },
        partitions={
            "total": 1, "complete": 1, "completeness": 1.0,
            "rejected": int(quality_report["decision"] == "REJECTED"),
        },
        books={
            venue: {
                "instrument_id": book.instrument_id,
                "state": book.state.value,
                "age_ns": max(0, monitor_now_ns - (book.last_receive_ts_ns or monitor_now_ns)),
                "sequence": book.sequence,
                "gaps": book.stats.gaps,
                "duplicates": book.stats.duplicates,
                "out_of_order": book.stats.out_of_order,
                "checksum_failures": book.stats.checksum_failures,
            }
            for (venue, _), book in books.items()
        },
        versions={
            "canonical_schema_expected": "1.2.0",
            "canonical_schema_observed": sorted({event.schema_version for event in events}),
            "adapter_versions": sorted({event.adapter_version for event in events}),
            "schema_mismatches": sum(event.schema_version != "1.2.0" for event in events),
            "instrument_master": cfg.instruments.version_snapshot(),
        },
        performance={
            "replay_duration_ns": replay_duration_ns,
            "replay_events_per_second": len(events) * 1_000_000_000 / replay_duration_ns,
        },
        live=live_metrics,
        strategy_execution=strategy_execution,
        tca=tca_report,
        structured_logs=structured_log_stats,
    )
    monitor.write(
        monitoring_report,
        output_dir / "pipeline_monitoring.json",
        output_dir / "pipeline_metrics.prom",
    )
    plans = list(oms.plans.values())
    summary = {
        "opportunities": len(plans),
        "closed_plans": sum(x.state.value == "CLOSED" for x in plans),
        "net_pnl": str(sum((x.net_pnl for x in plans), start=0)),
        "gross_pnl": str(sum((x.gross_pnl for x in plans), start=0)),
        "fees": str(sum((x.fees for x in plans), start=0)),
        "economics": economics,
        # Which inventory assumption produced these results (pre-funded spot
        # balances, or unconstrained selling that implies shorting).
        "inventory": oms.risk.inventory_snapshot(),
        "execution_api": {
            "schema_version": "1.0.0",
            **oms.router.stats_snapshot(),
            "trace": {
                "path": "order_trace.jsonl",
                "records": execution_trace["records"],
            },
            "dispatch": oms.dispatch_stats_snapshot(),
            "state_machine": oms.state_machine_stats_snapshot(),
        },
        "risk_reservation": risk.stats_snapshot(),
        "kill_switch": {
            "state": kill_switch_status["state"],
            "reason": kill_switch_status["reason"],
            "risk_reducing_orders_allowed": (
                kill_switch_status["risk_reducing_orders_allowed"]
            ),
            "reconciliation_required_before_resume": (
                kill_switch_status["reconciliation_required_before_resume"]
            ),
            "report": "kill_switch_status.json",
        },
        "exposure_limits": {
            "state": exposure_utilization["state"],
            "binding_event_count": len(exposure_utilization["binding_events"]),
            "transition_count": len(exposure_utilization["transitions"]),
            "report": "exposure_utilization.json",
        },
        "margin_collateral": {
            "state": margin_status["state"],
            "alert_count": len(margin_status["alerts"]),
            "report": "margin_status.json",
        },
        "pretrade_validation": {
            "decision": (
                risk.last_pretrade_validation.decision.value
                if risk.last_pretrade_validation is not None else "NOT_RUN"
            ),
            "failure_codes": (
                [
                    check.code
                    for check in risk.last_pretrade_validation.failures
                ]
                if risk.last_pretrade_validation is not None else []
            ),
            "report": (
                "pretrade_validation.json"
                if risk.last_pretrade_validation is not None else None
            ),
        },
        "account_state": {
            "positions": len(account_state["positions"]),
            "venues": len(account_state["venues"]),
            "report": "account_state.json",
        },
        "residual_risk": oms.residual_risk_snapshot(),
        "execution_mode": session.execution_mode.value,
        "strategies": session.strategy_report(),
        "instrument_status_changes_applied": instrument_status_changes_applied,
        "recorded_event_timing_used": recorded_event_timing_used,
        "execution_realism": execution_realism,
        "latency": {
            "report": "latency_report.json",
            "order_timestamps": "order_timestamps.jsonl",
            "model_source": (
                session.latency_model.source if session.latency_model else None
            ),
            "seed": session.latency_model.seed if session.latency_model else None,
        },
        "rms_reconciliation": {
            "decision": reconciliation_report.decision.value,
            "issue_count": len(reconciliation_report.issues),
            "reconciliation_id": reconciliation_report.reconciliation_id,
            "report": "rms_reconciliation_report.json",
        },
        "data_quality": data_quality,
        "canonical_events_delivered": canonical_events_delivered,
        "cross_venue_alignment": {
            "policy": "cross-venue-eligibility-v1",
            "evaluations": alignment_policy.evaluations,
            "eligible_evaluations": alignment_policy.eligible_evaluations,
            "blocked_evaluations": alignment_policy.blocked_evaluations,
            "routing_rejections": oms.alignment_rejections,
        },
        "time_integrity": {
            "events": time_report["events"],
            "blocked_events": time_report["blocked_events"],
            "cross_venue_skew_breaches": time_report["cross_venue_alignment"]["breaches"],
            "host_clock_state": time_report["host_clock"]["state"],
        },
        "data_quality_approval": {
            "decision": quality_report["decision"],
            "health": quality_report["health"],
            "active_flag_codes": [flag["code"] for flag in quality_report["active_flags"]],
            "defect_count": quality_report["defect_count"],
        },
        "pipeline_monitoring": {
            "health": monitoring_report["health"],
            "alert_count": len(monitoring_report["alerts"]),
            "strategy_execution": {
                "opportunities": strategy_execution["opportunities"]["count"],
                "orders": strategy_execution["orders"]["total"],
                "net_pnl": strategy_execution["pnl"]["net"],
            },
            "report": "pipeline_monitoring.json",
            "prometheus_metrics": "pipeline_metrics.prom",
        },
        "tca": {
            "report": "tca_report.json",
            "daily_reports": [str(path.relative_to(output_dir)) for path in tca_daily_paths],
            "fills": tca_report["summary"]["fills"],
            "weighted_slippage_bps": tca_report["summary"]["weighted_slippage_bps"],
            "fees": tca_report["summary"]["fees"],
            "missed_opportunities": tca_report["summary"]["missed_opportunities"],
        },
        "structured_logs": structured_log_stats,
        "book_integrity_gate": {
            "suspended": book_gate.suspended,
            "reason": book_gate.reason,
            "suspend_count": book_gate.suspend_count,
            "resume_count": book_gate.resume_count,
            "cancel_requests": book_gate.cancel_requests,
            "cancelled_orders": book_gate.cancelled_orders,
            "venue_transitions": book_gate.venue_transitions,
            "venues": {
                venue: book_gate.venue_state(venue) for venue in cfg.venues
            },
        },
        "book_hashes": {
            f"{venue}:{cfg.instrument_id}": book.snapshot_hash()
            for (venue, _), book in sorted(books.items())
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def apply_recorded_instrument_status(cfg, instrument_status_log: Path) -> int:
    """Replay effective-dated instrument/rule changes discovered live.

    A live-paper session's public instrument-status poller may append new
    instrument versions mid-session (a tick, lot, minimum or status change
    the exchange published after the static config was written). Those
    versions live only in that session's in-memory ``InstrumentMaster``
    unless replayed back in: without this, a backtest replay of the same
    period validates orders against the *stale* config-file rules and can
    reject (or, worse, silently accept at the wrong tick) orders the live
    session correctly accepted under the rules actually in force at the time.
    """
    if not instrument_status_log.exists():
        return 0
    applied = 0
    with instrument_status_log.open(encoding="utf-8") as handle:
        for line in handle:
            if '"INSTRUMENT_STATUS"' not in line:
                continue
            row = json.loads(line)
            if row.get("record_type") != "INSTRUMENT_STATUS":
                continue
            changes = row.get("changes", {})
            cfg.instruments.apply_status(
                row["venue"], row["instrument_id"],
                effective_from_ns=int(row["effective_from_ns"]),
                source=str(row.get("source", "recorded")),
                status=changes.get("status"),
                **{
                    key: Decimal(value) for key, value in changes.items()
                    if key in ("tick_size", "quantity_step", "min_quantity", "min_notional")
                },
            )
            applied += 1
    return applied


def load_recorded_event_timing(event_timing_log: Path) -> dict[str, int]:
    """Load each event's *actual* live processing wall-clock time.

    A replay that times every decision at the event's own ``receive_ts_ns``
    implicitly assumes zero processing latency -- every event is decided on
    the instant it arrives. Live paper does not have that luxury: real
    processing (OMS dispatch, ledger writes, occasional reconnect-storm
    backlog) can put a decision seconds after the event that triggered it,
    and time-sensitive strategy logic (a cooldown window, a report-timeout
    timer) is measured against *that* real elapsed time, not the idealized
    event clock. Replaying with the live-recorded ``processed_ts_ns`` per
    event reproduces the same "now" the strategy actually saw, which is what
    a like-for-like replay-vs-paper comparison needs.
    """
    if not event_timing_log.exists():
        return {}
    timing: dict[str, int] = {}
    with event_timing_log.open(encoding="utf-8") as handle:
        for line in handle:
            if '"EVENT"' not in line:
                continue
            row = json.loads(line)
            if row.get("record_type") != "EVENT":
                continue
            timing[row["event_id"]] = int(row["processed_ts_ns"])
    return timing


def run_backtest(
    config_path: Path, events_path: Path, output_dir: Path, *,
    instrument_status_log: Path | None = None,
    event_timing_log: Path | None = None,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    cfg, raw_config = load_config(config_path)
    applied_instrument_status = (
        apply_recorded_instrument_status(cfg, instrument_status_log)
        if instrument_status_log is not None else 0
    )
    event_timing = (
        load_recorded_event_timing(event_timing_log)
        if event_timing_log is not None else {}
    )
    timed_events = 0
    session = TradingSession(
        cfg, execution_mode=ExecutionMode(cfg.execution_mode), live_data=False
    )
    events = deterministic_order(load_events(events_path))

    def process_strategy_event(event: MarketEvent) -> None:
        nonlocal timed_events
        recorded_now = event_timing.get(event.event_id)
        if recorded_now is not None:
            timed_events += 1
        session.on_event(event, now_ns=recorded_now)

    replay_started_ns = time.perf_counter_ns()
    with CanonicalEventRecorder(output_dir / "canonical_events.jsonl") as recorder:
        dispatcher = CanonicalEventDispatcher(
            (CallbackEventSink(process_strategy_event), recorder),
            require_raw_ref=False,
        )
        for event in events:
            dispatcher.dispatch(event)
    replay_duration_ns = max(1, time.perf_counter_ns() - replay_started_ns)
    session.drain()
    summary = write_session_artifacts(
        session, events=events, output_dir=output_dir,
        correlation_id=events_path.name, source_path=str(events_path),
        source_sha256=file_sha256(events_path),
        replay_duration_ns=replay_duration_ns,
        canonical_events_delivered=dispatcher.delivered,
        instrument_status_changes_applied=applied_instrument_status,
        recorded_event_timing_used=timed_events,
    )
    manifest = {
        "created_at": utc_now_iso(),
        "platform_version": "0.1.0",
        "mode": "BACKTEST",
        "config_sha256": file_sha256(config_path),
        "events_sha256": file_sha256(events_path),
        "config": raw_config,
        "deterministic_result_sha256": sha256(canonical_json(summary).encode()).hexdigest(),
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return summary
