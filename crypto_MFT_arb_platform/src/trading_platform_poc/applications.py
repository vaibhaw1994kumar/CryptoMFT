from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from uuid import uuid4

import structlog

from trading_platform_poc.binance_feed import BinancePublicFeed
from trading_platform_poc.config import AppConfig
from trading_platform_poc.data_quality import (
    AnomalyCode,
    AnomalySeverity,
    DataQualityValidator,
)
from trading_platform_poc.engine import TradingEngine
from trading_platform_poc.models import EventType
from trading_platform_poc.models import FeedState
from trading_platform_poc.order_book import SequenceGapError
from trading_platform_poc.order_book import BinanceOrderBook
from trading_platform_poc.order_book import InvalidBookError
from trading_platform_poc.operations import (
    Operations,
    OperationsServer,
    RuntimeStatus,
)
from trading_platform_poc.recorder import JsonlRecorder, config_sha256
from trading_platform_poc.replay import ReplaySource
from trading_platform_poc.reporting import write_report
from trading_platform_poc.recovery import fetch_snapshot_event
from trading_platform_poc.tardis_integration import TardisBinanceSource


logger = structlog.get_logger()


async def run_record(
    config: AppConfig,
    duration_seconds: float,
    max_events: int | None,
) -> None:
    run_id = uuid4()
    output_directory = config.storage.data_directory / str(run_id)
    output_directory.mkdir(parents=True, exist_ok=False)
    recording_book = BinanceOrderBook(config.symbol)
    recording_quality = DataQualityValidator(config.data_quality)
    snapshot_recorded = False

    def recording_feed_status(
        state: FeedState,
        reason: str,
        details: dict[str, str],
    ) -> None:
        nonlocal snapshot_recorded
        if state in {FeedState.DISCONNECTED, FeedState.INVALID}:
            snapshot_recorded = False
            recording_quality.report(
                (
                    AnomalyCode.FEED_DISCONNECTED
                    if state is FeedState.DISCONNECTED
                    else AnomalyCode.INVALID_BOOK
                ),
                AnomalySeverity.ERROR,
                reason,
                symbol=config.symbol,
                event_type="feed_status",
                timestamp_ns=time.time_ns(),
                blocking=True,
                details=details,
            )

    feed = BinancePublicFeed(
        config.binance,
        config.symbol,
        run_id,
        status_handler=recording_feed_status,
    )
    event_count = 0
    logger.info(
        "record_mode_started",
        run_id=str(run_id),
        symbol=config.symbol,
        output_directory=str(output_directory),
        duration_seconds=duration_seconds,
        max_events=max_events,
    )
    with JsonlRecorder(
        output_directory,
        symbol=config.symbol,
        config_hash=config_sha256(config),
    ) as recorder:
        async def record_snapshot(timestamp_ns: int) -> bool:
            raw_snapshot, snapshot_event = await fetch_snapshot_event(
                feed,
                config.symbol,
                timestamp_ns,
            )
            anomaly_start = recording_quality.marker()
            quality_state = recording_quality.capture_state()
            book_state = recording_book.capture_state()
            if not recording_quality.validate_event(snapshot_event):
                recorder.quarantine(
                    {
                        "source": "rest_snapshot",
                        "data": raw_snapshot,
                        "symbol": config.symbol,
                    },
                    snapshot_event,
                    recording_quality.since(anomaly_start),
                )
                return False
            try:
                recording_book.load_snapshot(
                    snapshot_event.payload,
                    snapshot_event.receive_timestamp_ns,
                )
            except InvalidBookError as error:
                recording_quality.restore_state(quality_state)
                recording_book.restore_state(book_state)
                recording_quality.report(
                    AnomalyCode.INVALID_BOOK,
                    AnomalySeverity.ERROR,
                    str(error),
                    event=snapshot_event,
                    blocking=True,
                )
                recorder.quarantine(
                    {
                        "source": "rest_snapshot",
                        "data": raw_snapshot,
                        "symbol": config.symbol,
                    },
                    snapshot_event,
                    recording_quality.since(anomaly_start),
                )
                return False
            if not recording_quality.validate_book(
                recording_book,
                snapshot_event,
            ):
                recording_quality.restore_state(quality_state)
                recording_book.restore_state(book_state)
                recorder.quarantine(
                    {
                        "source": "rest_snapshot",
                        "data": raw_snapshot,
                        "symbol": config.symbol,
                    },
                    snapshot_event,
                    recording_quality.since(anomaly_start),
                )
                return False
            recorder.write(
                {
                    "source": "rest_snapshot",
                    "data": raw_snapshot,
                    "symbol": config.symbol,
                },
                snapshot_event,
            )
            return True

        try:
            async with asyncio.timeout(duration_seconds):
                async for raw_message, event in feed.events():
                    if (
                        not snapshot_recorded
                        and event.event_type.value == "book_delta"
                    ):
                        snapshot_recorded = await record_snapshot(
                            event.receive_timestamp_ns
                        )
                        if not snapshot_recorded:
                            continue
                        event_count += 1
                        if (
                            max_events is not None
                            and event_count >= max_events
                        ):
                            break
                        if (
                            event.payload.final_update_id
                            <= (recording_book.last_update_id or -1)
                        ):
                            continue
                    anomaly_start = recording_quality.marker()
                    quality_state = recording_quality.capture_state()
                    if not recording_quality.validate_event(event):
                        recorder.quarantine(
                            raw_message,
                            event,
                            recording_quality.since(anomaly_start),
                        )
                        recording_quality.restore_state(
                            quality_state
                        )
                        continue
                    if event.event_type is EventType.BOOK_DELTA:
                        book_state = recording_book.capture_state()
                        try:
                            applied = recording_book.apply_delta(
                                event.payload,
                                event.receive_timestamp_ns,
                            )
                            if not applied:
                                recording_quality.report(
                                    AnomalyCode.OUT_OF_ORDER_BOOK_UPDATE,
                                    AnomalySeverity.WARNING,
                                    "duplicate or stale book delta was quarantined",
                                    event=event,
                                    blocking=True,
                                    details={
                                        "book_last_update_id": str(
                                            recording_book.last_update_id
                                        ),
                                        "delta_first_update_id": str(
                                            event.payload.first_update_id
                                        ),
                                        "delta_final_update_id": str(
                                            event.payload.final_update_id
                                        ),
                                    },
                                )
                                recorder.quarantine(
                                    raw_message,
                                    event,
                                    recording_quality.since(anomaly_start),
                                )
                                recording_quality.restore_state(
                                    quality_state
                                )
                                continue
                        except (SequenceGapError, InvalidBookError) as error:
                            code = (
                                AnomalyCode.SEQUENCE_GAP
                                if isinstance(error, SequenceGapError)
                                else AnomalyCode.INVALID_BOOK
                            )
                            recording_quality.report(
                                code,
                                AnomalySeverity.ERROR,
                                str(error),
                                event=event,
                                blocking=True,
                            )
                            recorder.quarantine(
                                raw_message,
                                event,
                                recording_quality.since(anomaly_start),
                            )
                            recording_quality.restore_state(quality_state)
                            snapshot_recorded = await record_snapshot(
                                event.receive_timestamp_ns
                            )
                            if snapshot_recorded:
                                event_count += 1
                            continue
                        if not recording_quality.validate_book(
                            recording_book,
                            event,
                        ):
                            recording_quality.restore_state(quality_state)
                            recording_book.restore_state(book_state)
                            recorder.quarantine(
                                raw_message,
                                event,
                                recording_quality.since(anomaly_start),
                            )
                            snapshot_recorded = await record_snapshot(
                                event.receive_timestamp_ns
                            )
                            if snapshot_recorded:
                                event_count += 1
                            continue
                    recorder.write(raw_message, event)
                    event_count += 1
                    if max_events is not None and event_count >= max_events:
                        break
        except TimeoutError:
            pass
    logger.info(
        "record_mode_completed",
        run_id=str(run_id),
        event_count=event_count,
        output_directory=str(output_directory),
    )


async def run_replay(
    config: AppConfig,
    data_path: Path,
    speed: str | float = "max",
) -> None:
    run_id = uuid4()
    source = ReplaySource(data_path, run_id)
    engine = TradingEngine(config)
    final_timestamp_ns = 0
    logger.info(
        "replay_mode_started",
        run_id=str(run_id),
        symbol=config.symbol,
        data_path=str(source.data_path),
        speed=speed,
    )
    async for event in source.paced_events(speed):
        final_timestamp_ns = event.receive_timestamp_ns
        engine.process(event)
    if not engine.book.initialized:
        raise ValueError("replay dataset did not contain a usable book snapshot")
    engine.close(final_timestamp_ns)
    report_directory = config.storage.report_directory / str(run_id)
    summary = write_report(
        report_directory,
        run_id,
        "replay",
        engine,
    )
    logger.info(
        "replay_mode_completed",
        report_directory=str(report_directory),
        **summary,
    )


async def run_tardis_import(
    config: AppConfig,
    *,
    from_date: str,
    to_date: str,
    channels: list[str],
    api_key: str,
    cache_directory: Path,
    output_directory: Path | None,
    max_events: int | None = None,
) -> Path:
    run_id = uuid4()
    output = output_directory or config.storage.data_directory / str(run_id)
    source = TardisBinanceSource(
        run_id,
        from_date=from_date,
        to_date=to_date,
        symbols=[config.symbol],
        channels=channels,
        api_key=api_key,
        cache_dir=cache_directory,
    )
    request_config = {
        "app": config.model_dump(mode="json"),
        "tardis": {
            "exchange": "binance",
            "from_date": from_date,
            "to_date": to_date,
            "channels": channels,
        },
    }
    event_count = 0
    data_quality = DataQualityValidator(config.data_quality)
    with JsonlRecorder(
        output,
        symbol=config.symbol,
        config_hash=config_sha256(request_config),
    ) as recorder:
        async for raw_message, event in source.events():
            anomaly_start = data_quality.marker()
            if data_quality.validate_event(event):
                recorder.write(raw_message, event)
            else:
                recorder.quarantine(
                    raw_message,
                    event,
                    data_quality.since(anomaly_start),
                )
            event_count += 1
            if max_events is not None and event_count >= max_events:
                break
    logger.info(
        "tardis_import_completed",
        output_directory=str(output),
        event_count=event_count,
    )
    return output


async def run_paper(
    config: AppConfig,
    duration_seconds: float,
    max_events: int | None,
    status_port: int | None,
    kill_after_seconds: float | None,
) -> None:
    run_id = uuid4()
    engine = TradingEngine(config)
    snapshot_loaded = False
    stream_event_count = 0
    final_timestamp_ns = 0
    operations = Operations(
        RuntimeStatus(
            mode="paper",
            run_id=str(run_id),
            symbol=config.symbol,
        ),
        kill_switch=engine.activate_kill_switch,
    )

    def paper_feed_status(
        state: FeedState,
        reason: str,
        details: dict[str, str],
    ) -> None:
        nonlocal snapshot_loaded
        if state in {FeedState.DISCONNECTED, FeedState.INVALID}:
            snapshot_loaded = False
            timestamp_ns = time.time_ns()
            engine.mark_market_data_unhealthy(timestamp_ns)
            engine.data_quality.report(
                (
                    AnomalyCode.FEED_DISCONNECTED
                    if state is FeedState.DISCONNECTED
                    else AnomalyCode.INVALID_BOOK
                ),
                AnomalySeverity.ERROR,
                reason,
                symbol=config.symbol,
                event_type="feed_status",
                timestamp_ns=timestamp_ns,
                blocking=True,
                details=details,
            )
            operations.set_feed_state(state.value, reason)
        elif state is FeedState.CONNECTING:
            operations.set_feed_state(state.value, reason)
        elif state is FeedState.HEALTHY:
            operations.set_feed_state(
                FeedState.SYNCHRONIZING.value,
                "feed connected; validating market continuity",
            )

    feed = BinancePublicFeed(
        config.binance,
        config.symbol,
        run_id,
        status_handler=paper_feed_status,
    )
    server = (
        OperationsServer(
            operations,
            config.operations.status_host,
            status_port,
        )
        if status_port is not None
        else None
    )
    server_started = False
    try:
        if server is not None:
            await server.start()
            server_started = True
        started_at = asyncio.get_running_loop().time()
        logger.info(
            "paper_mode_started",
            run_id=str(run_id),
            symbol=config.symbol,
            strategy=config.strategy.name,
            duration_seconds=duration_seconds,
            max_events=max_events,
        )
        operations.set_feed_state(
            "synchronizing",
            "waiting for book snapshot",
        )
        try:
            async with asyncio.timeout(duration_seconds):
                async for _, event in feed.events():
                    final_timestamp_ns = event.receive_timestamp_ns
                    stream_event_count += 1
                    if (
                        not snapshot_loaded
                        and event.event_type is EventType.BOOK_DELTA
                    ):
                        _, snapshot_event = await fetch_snapshot_event(
                            feed,
                            config.symbol,
                        )
                        final_timestamp_ns = (
                            snapshot_event.receive_timestamp_ns
                        )
                        snapshot_loaded = engine.process(snapshot_event)
                        if not snapshot_loaded:
                            operations.set_feed_state(
                                "invalid",
                                "snapshot failed data-quality validation",
                            )
                            continue
                        operations.set_feed_state(
                            "healthy",
                            "book synchronized",
                        )
                        if (
                            event.payload.final_update_id
                            <= (engine.book.last_update_id or -1)
                        ):
                            operations.update_from_engine(
                                engine,
                                snapshot_event,
                            )
                            if (
                                max_events is not None
                                and stream_event_count >= max_events
                            ):
                                break
                            continue
                    processed = engine.process(event)
                    if (
                        event.event_type is EventType.BOOK_DELTA
                        and not processed
                        and not engine.market_data_healthy
                    ):
                        snapshot_loaded = False
                        operations.set_feed_state(
                            "synchronizing",
                            "resynchronizing invalid book",
                        )
                        _, snapshot_event = await fetch_snapshot_event(
                            feed,
                            config.symbol,
                        )
                        final_timestamp_ns = (
                            snapshot_event.receive_timestamp_ns
                        )
                        snapshot_loaded = engine.process(snapshot_event)
                        if snapshot_loaded:
                            operations.set_feed_state(
                                "healthy",
                                "book resynchronized",
                            )
                        else:
                            operations.set_feed_state(
                                "invalid",
                                "resynchronized book failed validation",
                            )
                    if not engine.risk.kill_switch_active:
                        if engine.market_data_healthy:
                            operations.set_feed_state(
                                "healthy",
                                "market data healthy",
                            )
                        elif snapshot_loaded:
                            operations.set_feed_state(
                                "invalid",
                                "market data blocked by quality controls",
                            )
                    operations.update_from_engine(engine, event)
                    if (
                        kill_after_seconds is not None
                        and asyncio.get_running_loop().time() - started_at
                        >= kill_after_seconds
                        and not engine.risk.kill_switch_active
                    ):
                        engine.activate_kill_switch(final_timestamp_ns)
                        operations.set_feed_state(
                            "stopped",
                            "manual kill switch activated",
                        )
                    if (
                        max_events is not None
                        and stream_event_count >= max_events
                    ):
                        break
        except TimeoutError:
            operations.set_feed_state("stale", "paper duration elapsed")
        if not engine.book.initialized:
            raise RuntimeError(
                "paper mode ended before the order book synchronized"
            )
        engine.close(final_timestamp_ns)
        report_directory = config.storage.report_directory / str(run_id)
        summary = write_report(
            report_directory,
            run_id,
            "paper",
            engine,
        )
        logger.info(
            "paper_mode_completed",
            report_directory=str(report_directory),
            **summary,
        )
    finally:
        if server_started:
            active_exception = sys.exception()
            try:
                await server.stop()
            except BaseException as cleanup_error:
                if active_exception is None:
                    raise
                active_exception.add_note(
                    "OperationsServer.stop() also failed: "
                    f"{cleanup_error!r}"
                )
