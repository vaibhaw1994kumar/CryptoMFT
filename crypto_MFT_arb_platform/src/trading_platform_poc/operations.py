from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

import uvicorn
from fastapi import FastAPI, Response
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)


@dataclass(slots=True)
class RuntimeStatus:
    mode: str
    run_id: str
    symbol: str
    feed_state: str = "connecting"
    event_count: int = 0
    order_count: int = 0
    fill_count: int = 0
    last_event_timestamp_ns: int | None = None
    market_data_delay_ns: int | None = None
    processing_lag_ns: int | None = None
    message: str = "starting"
    anomaly_count: int = 0
    blocking_anomaly_count: int = 0
    reconciliation_required_order_count: int = 0
    orders_by_venue: dict[str, int] = field(default_factory=dict)


class Operations:
    def __init__(
        self,
        status: RuntimeStatus,
        kill_switch: Callable[[int], None] | None = None,
    ) -> None:
        self.status = status
        self._kill_switch = kill_switch
        self.registry = CollectorRegistry()
        self.events = Counter(
            "trading_poc_events_total",
            "Processed market events",
            registry=self.registry,
        )
        self.orders = Gauge(
            "trading_poc_orders",
            "Current total order count",
            registry=self.registry,
        )
        self.fills = Gauge(
            "trading_poc_fills",
            "Current total fill count",
            registry=self.registry,
        )
        self.feed_healthy = Gauge(
            "trading_poc_feed_healthy",
            "Whether the market-data feed is healthy",
            registry=self.registry,
        )
        self.market_data_delay = Gauge(
            "trading_poc_market_data_delay_ns",
            "Exchange-to-local receive delay in nanoseconds",
            registry=self.registry,
        )
        self.processing_lag = Gauge(
            "trading_poc_processing_lag_ns",
            "Receive-to-normalization processing lag in nanoseconds",
            registry=self.registry,
        )
        self.anomalies = Gauge(
            "trading_poc_data_quality_anomalies",
            "Total detected market-data anomalies",
            registry=self.registry,
        )
        self.blocking_anomalies = Gauge(
            "trading_poc_blocking_data_quality_anomalies",
            "Total blocking market-data anomalies",
            registry=self.registry,
        )
        self.reconciliation_required_orders = Gauge(
            "trading_poc_reconciliation_required_orders",
            "Orders requiring operator reconciliation",
            registry=self.registry,
        )
        self.app = self._create_app()

    def update_from_engine(self, engine, event) -> None:
        self.status.event_count = engine.event_count
        self.status.order_count = len(engine.oms.orders)
        self.status.fill_count = len(engine.portfolio.fills)
        self.status.last_event_timestamp_ns = event.receive_timestamp_ns
        self.status.market_data_delay_ns = max(
            0,
            event.receive_timestamp_ns - event.exchange_timestamp_ns,
        )
        self.status.processing_lag_ns = max(
            0,
            event.process_timestamp_ns - event.receive_timestamp_ns,
        )
        quality_summary = engine.data_quality.summary()
        self.status.anomaly_count = quality_summary["count"]
        self.status.blocking_anomaly_count = quality_summary[
            "blocking_count"
        ]
        self.status.reconciliation_required_order_count = sum(
            order.state.value == "reconciliation_required"
            for order in engine.oms.orders
        )
        orders_by_venue: dict[str, int] = {}
        for order in engine.oms.orders:
            venue = order.venue or "unassigned"
            orders_by_venue[venue] = orders_by_venue.get(venue, 0) + 1
        self.status.orders_by_venue = dict(sorted(orders_by_venue.items()))
        self.events.inc()
        self.orders.set(self.status.order_count)
        self.fills.set(self.status.fill_count)
        self.market_data_delay.set(self.status.market_data_delay_ns)
        self.processing_lag.set(self.status.processing_lag_ns)
        self.anomalies.set(self.status.anomaly_count)
        self.blocking_anomalies.set(
            self.status.blocking_anomaly_count
        )
        self.reconciliation_required_orders.set(
            self.status.reconciliation_required_order_count
        )

    def set_feed_state(self, state: str, message: str) -> None:
        self.status.feed_state = state
        self.status.message = message
        self.feed_healthy.set(1 if state == "healthy" else 0)

    def _create_app(self) -> FastAPI:
        app = FastAPI(title="Trading Platform POC Operations")

        @app.get("/status")
        async def status() -> dict:
            return asdict(self.status)

        @app.get("/metrics")
        async def metrics() -> Response:
            return Response(
                generate_latest(self.registry),
                media_type="text/plain; version=0.0.4",
            )

        @app.post("/kill")
        async def kill() -> dict:
            if self._kill_switch is None:
                return {
                    "activated": False,
                    "reason": "kill switch is not configured",
                }
            timestamp_ns = (
                self.status.last_event_timestamp_ns or time.time_ns()
            )
            self._kill_switch(timestamp_ns)
            self.set_feed_state(
                "stopped",
                "manual kill switch activated",
            )
            return {
                "activated": True,
                "timestamp_ns": timestamp_ns,
            }

        return app


class OperationsServer:
    def __init__(self, operations: Operations, host: str, port: int) -> None:
        self._server = uvicorn.Server(
            uvicorn.Config(
                operations.app,
                host=host,
                port=port,
                log_level="warning",
                access_log=False,
            )
        )
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started and not self._task.done():
            await asyncio.sleep(0.01)
        if self._task.done():
            await self._task

    async def stop(self) -> None:
        if self._task is None:
            return
        self._server.should_exit = True
        await self._task
