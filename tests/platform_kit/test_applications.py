import asyncio
from types import SimpleNamespace

import pytest

from trading_platform_poc import applications
from trading_platform_poc.config import AppConfig, StorageConfig
from trading_platform_poc.models import EventType


class FakeOperations:
    def __init__(self, status, kill_switch=None) -> None:
        self.status = status
        self.kill_switch = kill_switch
        self.states: list[tuple[str, str]] = []

    def set_feed_state(self, state: str, message: str) -> None:
        self.states.append((state, message))

    def update_from_engine(self, engine, event) -> None:
        pass


class FakeEngine:
    def __init__(self, processing_error: BaseException | None = None) -> None:
        self.processing_error = processing_error
        self.book = SimpleNamespace(initialized=False, last_update_id=None)
        self.risk = SimpleNamespace(kill_switch_active=False)
        self.data_quality = SimpleNamespace(report=lambda *args, **kwargs: None)
        self.market_data_healthy = True
        self.closed = False

    def activate_kill_switch(self, timestamp_ns: int) -> None:
        self.risk.kill_switch_active = True

    def process(self, event) -> bool:
        if event.event_type is EventType.BOOK_SNAPSHOT:
            self.book.initialized = True
            self.book.last_update_id = 10
            return True
        if self.processing_error is not None:
            raise self.processing_error
        return True

    def close(self, timestamp_ns: int) -> None:
        self.closed = True


class FakeFeed:
    def __init__(self, event) -> None:
        self.event = event

    async def events(self):
        yield {}, self.event


def install_paper_fakes(
    monkeypatch,
    *,
    processing_error: BaseException | None = None,
    synchronization_error: BaseException | None = None,
    reporting_error: BaseException | None = None,
    cleanup_error: BaseException | None = None,
):
    delta_event = SimpleNamespace(
        event_type=EventType.BOOK_DELTA,
        receive_timestamp_ns=2,
        payload=SimpleNamespace(final_update_id=11),
    )
    snapshot_event = SimpleNamespace(
        event_type=EventType.BOOK_SNAPSHOT,
        receive_timestamp_ns=1,
    )
    engine = FakeEngine(processing_error)

    class TrackingOperationsServer:
        instances = []

        def __init__(self, operations, host: str, port: int) -> None:
            self._stop_requested = asyncio.Event()
            self._task: asyncio.Task | None = None
            self.instances.append(self)

        async def start(self) -> None:
            self._task = asyncio.create_task(self._stop_requested.wait())
            await asyncio.sleep(0)

        async def stop(self) -> None:
            self._stop_requested.set()
            assert self._task is not None
            await self._task
            if cleanup_error is not None:
                raise cleanup_error

    async def fetch_snapshot(*args, **kwargs):
        if synchronization_error is not None:
            raise synchronization_error
        return {}, snapshot_event

    def write_report(*args, **kwargs):
        if reporting_error is not None:
            raise reporting_error
        return {"event_count": 1}

    monkeypatch.setattr(applications, "TradingEngine", lambda config: engine)
    monkeypatch.setattr(applications, "Operations", FakeOperations)
    monkeypatch.setattr(
        applications,
        "BinancePublicFeed",
        lambda *args, **kwargs: FakeFeed(delta_event),
    )
    monkeypatch.setattr(
        applications,
        "OperationsServer",
        TrackingOperationsServer,
    )
    monkeypatch.setattr(applications, "fetch_snapshot_event", fetch_snapshot)
    monkeypatch.setattr(applications, "write_report", write_report)
    return engine, TrackingOperationsServer


def paper_config(tmp_path) -> AppConfig:
    return AppConfig(
        storage=StorageConfig(report_directory=tmp_path / "reports")
    )


async def test_paper_stops_server_when_synchronization_fails(
    monkeypatch,
    tmp_path,
) -> None:
    synchronization_error = RuntimeError("snapshot synchronization failed")
    cleanup_error = RuntimeError("server cleanup failed")
    _, server_type = install_paper_fakes(
        monkeypatch,
        synchronization_error=synchronization_error,
        cleanup_error=cleanup_error,
    )

    with pytest.raises(RuntimeError) as raised:
        await applications.run_paper(
            paper_config(tmp_path),
            duration_seconds=1,
            max_events=1,
            status_port=8000,
            kill_after_seconds=None,
        )

    assert raised.value is synchronization_error
    assert server_type.instances[0]._task.done()
    assert "OperationsServer.stop() also failed" in raised.value.__notes__[0]


@pytest.mark.parametrize(
    "failure_stage,error",
    [
        ("processing", RuntimeError("event processing failed")),
        ("reporting", RuntimeError("report generation failed")),
        ("cancellation", asyncio.CancelledError("paper run cancelled")),
    ],
)
async def test_paper_stops_server_and_preserves_processing_or_report_error(
    monkeypatch,
    tmp_path,
    failure_stage: str,
    error: BaseException,
) -> None:
    cleanup_error = RuntimeError("server cleanup failed")
    processing_error = error if failure_stage != "reporting" else None
    reporting_error = error if failure_stage == "reporting" else None
    _, server_type = install_paper_fakes(
        monkeypatch,
        processing_error=processing_error,
        reporting_error=reporting_error,
        cleanup_error=cleanup_error,
    )

    with pytest.raises(type(error)) as raised:
        await applications.run_paper(
            paper_config(tmp_path),
            duration_seconds=1,
            max_events=1,
            status_port=8000,
            kill_after_seconds=None,
        )

    assert raised.value is error
    assert server_type.instances[0]._task.done()


async def test_paper_stops_server_after_success(monkeypatch, tmp_path) -> None:
    engine, server_type = install_paper_fakes(monkeypatch)

    await applications.run_paper(
        paper_config(tmp_path),
        duration_seconds=1,
        max_events=1,
        status_port=8000,
        kill_after_seconds=None,
    )

    assert engine.closed
    assert server_type.instances[0]._task.done()


async def test_paper_propagates_cleanup_when_it_is_only_failure(
    monkeypatch,
    tmp_path,
) -> None:
    cleanup_error = RuntimeError("server cleanup failed")
    _, server_type = install_paper_fakes(
        monkeypatch,
        cleanup_error=cleanup_error,
    )

    with pytest.raises(RuntimeError) as raised:
        await applications.run_paper(
            paper_config(tmp_path),
            duration_seconds=1,
            max_events=1,
            status_port=8000,
            kill_after_seconds=None,
        )

    assert raised.value is cleanup_error
    assert server_type.instances[0]._task.done()
