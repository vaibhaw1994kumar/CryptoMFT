from fastapi.testclient import TestClient
from types import SimpleNamespace

from trading_platform_poc.models import OrderState
from trading_platform_poc.operations import Operations, RuntimeStatus


def test_exposes_status_and_prometheus_metrics() -> None:
    kill_timestamps: list[int] = []
    operations = Operations(
        RuntimeStatus(
            mode="paper",
            run_id="run-1",
            symbol="BTCUSDT",
            last_event_timestamp_ns=123,
        ),
        kill_switch=kill_timestamps.append,
    )
    operations.set_feed_state("healthy", "book synchronized")
    client = TestClient(operations.app)

    status = client.get("/status")
    metrics = client.get("/metrics")
    killed = client.post("/kill")

    assert status.status_code == 200
    assert status.json()["feed_state"] == "healthy"
    assert metrics.status_code == 200
    assert "trading_poc_feed_healthy 1.0" in metrics.text
    assert killed.json() == {"activated": True, "timestamp_ns": 123}
    assert kill_timestamps == [123]


def test_surfaces_reconciliation_required_orders() -> None:
    operations = Operations(
        RuntimeStatus(
            mode="paper",
            run_id="run-1",
            symbol="BTCUSDT",
        )
    )
    engine = SimpleNamespace(
        event_count=1,
        oms=SimpleNamespace(
            orders=(
                SimpleNamespace(
                    state=OrderState.RECONCILIATION_REQUIRED,
                    venue="binance",
                ),
            )
        ),
        portfolio=SimpleNamespace(fills=()),
        data_quality=SimpleNamespace(
            summary=lambda: {
                "count": 0,
                "blocking_count": 0,
            }
        ),
    )
    event = SimpleNamespace(
        receive_timestamp_ns=2,
        exchange_timestamp_ns=1,
        process_timestamp_ns=3,
    )

    operations.update_from_engine(engine, event)
    client = TestClient(operations.app)

    assert (
        client.get("/status").json()[
            "reconciliation_required_order_count"
        ]
        == 1
    )
    assert (
        "trading_poc_reconciliation_required_orders 1.0"
        in client.get("/metrics").text
    )
    assert client.get("/status").json()["orders_by_venue"] == {
        "binance": 1
    }
