"""Live paper trading on public feeds (MR-07 paper mode, forward testing).

``run_live_paper`` drives the same :class:`TradingSession` a backtest uses, but
from live public WebSocket feeds instead of a replayed dataset:

    PublicFeed x N -> LiveCapture -> normalizers -> QualityMonitor
                   -> TradingSession (features, strategy, risk, router)
                   -> PaperMatchingEngine (internal_paper)
                      or VenueTestnetExecution (venue_testnet)

Nothing between the canonical event and the ledger differs from a backtest,
which is what makes a forward test comparable with a replay of the same
capture: pass ``record_dataset`` and the exact raw frames the session traded
on are persisted as a dataset that ``mr-backtest --dataset`` can replay.

Time advances on a monotonic wall clock. Heartbeats advance it when the feeds
are quiet so order timeouts, residual limits and stale-feed kills still fire.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from trading_platform_poc.minreq.backtest import (
    BacktestConfig,
    BacktestResult,
    TradingSession,
)
from trading_platform_poc.minreq.fees import FeeBook
from trading_platform_poc.minreq.ingest import (
    Clock,
    FeedFactory,
    LiveCapture,
    MonotonicWallClock,
    public_feed_factory,
)
from trading_platform_poc.minreq.pipeline import (
    NORMALIZATION_ERRORS,
    Pipeline,
    record_digest,
)
from trading_platform_poc.minreq.quality import QualityMonitor, QualityThresholds
from trading_platform_poc.minreq.securitymaster import (
    SecurityMaster,
    default_security_master,
)
from trading_platform_poc.minreq.venues import RawRecord, build_normalizer

MatcherFactory = Callable[..., Any]


async def run_live_paper(
    config: BacktestConfig,
    output_dir: str | Path,
    *,
    symbol: str = "BTCUSDT",
    duration_seconds: float | None = 60.0,
    max_records: int | None = None,
    record_dataset: str | Path | None = None,
    security_master: SecurityMaster | None = None,
    fee_book: FeeBook | None = None,
    feed_factory: FeedFactory = public_feed_factory,
    matcher_factory: MatcherFactory | None = None,
    clock: Clock | None = None,
    heartbeat_seconds: float = 0.05,
    settle_seconds: float = 5.0,
) -> BacktestResult:
    if config.mode != "paper":
        raise ValueError("live runs are paper runs; set BacktestConfig.mode='paper'")
    master = security_master or default_security_master()
    wall = clock or MonotonicWallClock()
    venues = tuple(config.venue_instruments)
    session = TradingSession(
        config,
        output_dir,
        security_master=master,
        fee_book=fee_book,
        dataset_info={
            "source": "live_public_feeds",
            "venues": list(venues),
            "symbol": symbol.upper(),
        },
        matcher_factory=matcher_factory,
    )
    execution = session.matcher
    monitor = QualityMonitor(
        stale_after_ns=config.stale_after_ns, thresholds=QualityThresholds()
    )
    normalizers = {venue: build_normalizer(venue, master) for venue in venues}
    capture = LiveCapture(
        venues,
        symbol,
        feed_factory=feed_factory,
        clock=wall,
        heartbeat_seconds=heartbeat_seconds,
    )
    recorder = Pipeline(Path(record_dataset), master) if record_dataset else None
    pending: list[RawRecord] = []
    raw_records: dict[str, int] = {}

    start = getattr(execution, "start", None)
    if start is not None:
        await start()
    try:
        async for record in capture.stream(
            duration_seconds=duration_seconds, max_records=max_records
        ):
            if record is None:
                session.advance(wall())
                continue
            raw_records[record.venue] = raw_records.get(record.venue, 0) + 1
            if recorder is not None:
                pending.append(record)
                if len(pending) >= 500:
                    recorder.ingest(pending)
                    pending = []
            try:
                events = normalizers[record.venue].normalize(record)
            except NORMALIZATION_ERRORS as error:
                events = (
                    monitor.record_schema_exception(
                        venue=record.venue,
                        channel=record.channel,
                        receive_timestamp_ns=record.receive_timestamp_ns,
                        reason=f"{type(error).__name__}: {error}",
                        record_digest=record_digest(record),
                    ),
                )
            for event in sorted(events, key=lambda item: item.ordering_key):
                for graded in monitor.process(event):
                    session.process(graded)
            await asyncio.sleep(0)

        # Let orders already on the wire land and report in real time before
        # the session drains, so live results are not cut off mid-flight.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settle_seconds
        while not session.router.quiescent() and loop.time() < deadline:
            session.advance(wall())
            await asyncio.sleep(heartbeat_seconds)
        session.advance(wall())
    finally:
        close = getattr(execution, "close", None)
        if close is not None:
            await close()

    result = session.finish(
        quality_report=monitor.report(),
        replay_signature=f"live:{config.run_id}:{sum(raw_records.values())}",
        inputs={
            "venues": sorted(venues),
            "raw_records": dict(sorted(raw_records.items())),
            "capture": capture.stats.to_dict(),
            "clock_regressions": getattr(wall, "regressions", 0),
        },
    )
    if recorder is not None:
        if pending:
            recorder.ingest(pending)
        if recorder.ingestor.checkpoint.records_written:
            manifest = recorder.run()
            (Path(output_dir) / "recorded_dataset.json").write_text(
                __import__("json").dumps(
                    {
                        "dataset": str(record_dataset),
                        "checksum": manifest.get("checksum"),
                        "approved": manifest.get("approved"),
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
    return result
