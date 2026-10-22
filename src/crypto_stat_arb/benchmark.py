"""MR-14 performance baseline: replay throughput and paper-path overhead.

Measures, on the same trading stack used in production modes:

* replay throughput (events/s) of the pure event loop, and of the full
  backtest including artifact writing;
* wall-clock feed-to-strategy, strategy evaluation and route-decision latency
  percentiles;
* per-frame paper overhead (JSON decode + canonical event + full session).

Thresholds come from the config ``performance`` section; the report states
PASS/FAIL per check and what was not exercised.
"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import random
import sys
import time
from typing import Any

try:  # Unix only; on Windows peak RSS is reported as unavailable (None)
    import resource
except ImportError:
    resource = None

from .config import load_config
from .models import MarketEvent
from .replay import deterministic_order
from .runner import run_backtest
from .session import ExecutionMode, StageTimer, TradingSession


def synthetic_book_events(
    count: int, *, venues: tuple[str, str] = ("BINANCE", "BYBIT"),
    instrument_id: str = "BTC-USDT-SPOT", seed: int = 7,
    start_ns: int = 1_700_000_000_000_000_000, interval_ns: int = 50_000_000,
    dislocation_every: int = 200, dislocation_bps: Decimal = Decimal("14"),
    tick: Decimal = Decimal("0.10"),
) -> list[dict[str, Any]]:
    """Deterministic two-venue top-of-book stream with periodic dislocations."""
    rng = random.Random(seed)
    mid = Decimal("60000.00")
    sequence = {venue: 1_000 for venue in venues}
    top: dict[str, tuple[Decimal, Decimal]] = {}
    rows: list[dict[str, Any]] = []
    ts = start_ns

    def text(value: Decimal) -> str:
        return str(value.quantize(tick))

    for venue in venues:
        top[venue] = (mid - tick, mid + tick)
        rows.append({
            "event_type": "BOOK_SNAPSHOT", "venue": venue,
            "instrument_id": instrument_id, "exchange_ts_ns": ts,
            "receive_ts_ns": ts + 1_000_000, "sequence": sequence[venue],
            "bids": [[text(mid - tick), "1.000"]], "asks": [[text(mid + tick), "1.000"]],
        })
        ts += interval_ns
    dislocated_until = -1
    for index in range(max(0, count - len(venues))):
        venue = venues[index % len(venues)]
        mid += tick * rng.choice((-1, 0, 1))
        if index % dislocation_every == 0:
            dislocated_until = index + 3 * len(venues)
        offset = Decimal("0")
        if venue == venues[1] and index <= dislocated_until:
            offset = (mid * dislocation_bps / Decimal("10000") / tick).to_integral_value() * tick
        bid, ask = mid - tick + offset, mid + tick + offset
        old_bid, old_ask = top[venue]
        bids = [[text(bid), "1.000"]]
        asks = [[text(ask), "1.000"]]
        if old_bid != bid:
            bids.insert(0, [text(old_bid), "0"])
        if old_ask != ask:
            asks.insert(0, [text(old_ask), "0"])
        top[venue] = (bid, ask)
        sequence[venue] += 1
        rows.append({
            "event_type": "BOOK_DELTA", "venue": venue,
            "instrument_id": instrument_id, "exchange_ts_ns": ts,
            "receive_ts_ns": ts + 1_000_000, "sequence": sequence[venue],
            "bids": bids, "asks": asks,
        })
        ts += interval_ns
    return rows


def _peak_rss_bytes() -> int | None:
    if resource is None:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def _check(
    name: str, observed: float | None, limit: Any, *, higher_is_better: bool
) -> dict[str, Any]:
    if limit is None:
        return {"check": name, "status": "NOT_CONFIGURED", "observed": observed}
    if observed is None:
        return {"check": name, "status": "NOT_EXERCISED", "limit": limit}
    passed = observed >= float(limit) if higher_is_better else observed <= float(limit)
    return {
        "check": name, "status": "PASS" if passed else "FAIL",
        "observed": observed, "limit": float(limit),
    }


def run_benchmark(
    config_path: Path, output_dir: Path, *, events_path: Path | None = None,
    synthetic_count: int = 20_000, seed: int = 7,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    cfg, _ = load_config(config_path)
    if events_path is None:
        events_path = output_dir / "benchmark_events.jsonl"
        rows = synthetic_book_events(
            synthetic_count, venues=cfg.venues, instrument_id=cfg.instrument_id, seed=seed,
        )
        events_path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
    lines = [line for line in events_path.read_text(encoding="utf-8").splitlines() if line]

    events = deterministic_order([MarketEvent.from_dict(json.loads(line)) for line in lines])
    replay = TradingSession(cfg)
    started = time.perf_counter_ns()
    for event in events:
        replay.on_event(event)
    replay.drain()
    replay_ns = max(1, time.perf_counter_ns() - started)

    paper_clock = [0]
    paper = TradingSession(
        cfg, execution_mode=ExecutionMode.INTERNAL_PAPER, live_data=True,
        clock_ns=lambda: paper_clock[0],
    )
    frame_timer = StageTimer()
    for line in lines:
        frame_started = time.perf_counter_ns()
        event = MarketEvent.from_dict(json.loads(line))
        paper_clock[0] = event.receive_ts_ns
        paper.on_event(event, now_ns=event.receive_ts_ns)
        frame_timer.add("paper_frame_overhead_ns", time.perf_counter_ns() - frame_started)

    started = time.perf_counter_ns()
    summary = run_backtest(config_path, events_path, output_dir / "backtest")
    end_to_end_ns = max(1, time.perf_counter_ns() - started)

    stages = replay.stage_timer.report()
    paper_stages = {**paper.stage_timer.report(), **frame_timer.report()}
    replay_eps = len(events) * 1_000_000_000 / replay_ns
    limits = cfg.performance

    def p99_ms(report: dict[str, Any], stage: str) -> float | None:
        row = report.get(stage)
        return row["p99_ns"] / 1_000_000 if row else None

    checks = [
        _check("replay_events_per_second", replay_eps,
               limits.get("min_replay_events_per_second"), higher_is_better=True),
        _check("feed_to_strategy_p99_ms", p99_ms(stages, "feed_to_strategy_ns"),
               limits.get("max_feed_to_strategy_p99_ms"), higher_is_better=False),
        _check("route_decision_p99_ms", p99_ms(stages, "route_decision_ns"),
               limits.get("max_route_decision_p99_ms"), higher_is_better=False),
        _check("paper_frame_overhead_p99_ms", p99_ms(paper_stages, "paper_frame_overhead_ns"),
               limits.get("max_paper_frame_overhead_p99_ms"), higher_is_better=False),
    ]
    statuses = {check["status"] for check in checks}
    decision = "FAIL" if "FAIL" in statuses else (
        "PASS" if statuses <= {"PASS"} else "PARTIAL"
    )
    report = {
        "schema_version": "1.0.0",
        "events": len(events),
        "events_source": str(events_path),
        "replay": {
            "duration_ns": replay_ns,
            "events_per_second": replay_eps,
            "opportunities": replay.opportunities,
            "stages": stages,
        },
        "end_to_end_backtest": {
            "duration_ns": end_to_end_ns,
            "events_per_second": len(events) * 1_000_000_000 / end_to_end_ns,
            "opportunities": summary["opportunities"],
            "note": "includes ledger, TCA, structured-log and report writing",
        },
        "paper": {
            "execution_mode": paper.execution_mode.value,
            "opportunities": paper.opportunities,
            "stages": paper_stages,
        },
        "peak_rss_bytes": _peak_rss_bytes(),
        "host": {"python": sys.version.split()[0], "platform": sys.platform},
        "checks": checks,
        "decision": decision,
        "limitations": [
            "Paper frame overhead excludes websocket receive, TLS and recorder "
            "fsync; live-paper sessions report paper_frame_overhead_ns from real frames.",
            "Single-process CPython measurement on the host above; not a "
            "production latency claim.",
        ],
    }
    (output_dir / "benchmark_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    return report
