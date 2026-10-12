from __future__ import annotations

import asyncio
from collections import deque
from decimal import Decimal, ROUND_DOWN
from typing import Any

import pytest
import hft_platform_kit as hft

from trading_platform_poc.cli import _ems_demo, _ems_inspection
from trading_platform_poc.config import AppConfig, EMSConfig
from trading_platform_poc.ems import (
    DispatchPolicy,
    ExecutionCoordinator,
    ExecutionLeg,
    ExecutionOutcome,
    ExecutionPlanRequest,
    PlanRecoveryOrderEvidence,
    RouteOrderStatus,
    ScheduleRecoveryOrderEvidence,
    ScheduleRiskLimitError,
    ScheduleState,
    UnexecutableResidualError,
    VenueQuote,
)
from trading_platform_poc.exchange_adapter import (
    AccountSnapshot,
    AmbiguousOrderError,
    ExchangeAdapterError,
    ExchangeOrder,
    InstrumentRules,
    NormalizedOrder,
    QuantityUnit,
    ReconciliationSnapshot,
)
from trading_platform_poc.models import Order, OrderType, Side


_REFERENCE_FILL_PRICE = object()


class FakeAdapter:
    last_request = None

    def __init__(
        self,
        venue: str,
        *,
        submissions: tuple[str, ...] = (),
        queries: tuple[str, ...] = (),
        cancellations: tuple[str, ...] = (),
        cancel_status: str = "canceled",
        quantity_step: Decimal = Decimal("0.0001"),
        minimum_quantity: Decimal = Decimal("0.0001"),
        maximum_quantity: Decimal | None = None,
        minimum_notional: Decimal | None = None,
        quantity_unit: QuantityUnit = QuantityUnit.BASE,
        average_fill_price: Decimal | None | object = _REFERENCE_FILL_PRICE,
        response_venue: str | None = None,
        response_symbol: str | None = None,
        response_client_order_id: str | None = None,
    ) -> None:
        self.venue = venue
        self.submission_actions = deque(submissions)
        self.query_actions = deque(queries)
        self.cancellation_actions = deque(cancellations)
        self.cancel_status = cancel_status
        self.quantity_step = quantity_step
        self.minimum_quantity = minimum_quantity
        self.maximum_quantity = maximum_quantity
        self.minimum_notional = minimum_notional
        self.quantity_unit = quantity_unit
        self.average_fill_price = average_fill_price
        self.response_venue = response_venue
        self.response_symbol = response_symbol
        self.response_client_order_id = response_client_order_id
        self.submitted: list[Order] = []
        self.canceled: list[str] = []
        self.queried: list[str] = []
        self.loop_ids: list[int] = []
        self.instrument_symbols: list[str] = []
        self.reference_prices: list[Decimal | None] = []
        self.events: list[str] = []
        self.external_orders: dict[str, ExchangeOrder] = {}
        self.closed = False

    def client_order_id(self, order: Order) -> str:
        return f"{self.venue}-{order.client_order_id}"

    async def close(self) -> None:
        self.closed = True

    async def instrument_rules(self, symbol: str) -> InstrumentRules:
        self.instrument_symbols.append(symbol)
        return InstrumentRules(
            venue=self.venue,
            symbol=symbol,
            tick_size=Decimal("0.01"),
            quantity_step=self.quantity_step,
            minimum_quantity=self.minimum_quantity,
            maximum_limit_quantity=self.maximum_quantity,
            maximum_market_quantity=self.maximum_quantity,
            quantity_unit=self.quantity_unit,
        )

    async def normalize_order(
        self,
        order: Order,
        *,
        reference_price: Decimal | None = None,
    ) -> NormalizedOrder:
        self.loop_ids.append(id(asyncio.get_running_loop()))
        self.reference_prices.append(reference_price)
        quantity = (
            (order.quantity / self.quantity_step).to_integral_value(
                rounding=ROUND_DOWN
            )
            * self.quantity_step
        )
        if quantity < self.minimum_quantity:
            raise ExchangeAdapterError("quantity below venue minimum")
        if self.maximum_quantity is not None and quantity > self.maximum_quantity:
            raise ExchangeAdapterError("quantity above venue maximum")
        if (
            self.minimum_notional is not None
            and (order.price or reference_price) is not None
            and quantity * (order.price or reference_price)
            < self.minimum_notional
        ):
            raise ExchangeAdapterError("order below venue minimum notional")
        return NormalizedOrder(
            quantity=quantity,
            price=order.price,
            client_order_id=self.client_order_id(order),
        )

    async def submit_order(self, order: Order) -> ExchangeOrder:
        self.loop_ids.append(id(asyncio.get_running_loop()))
        self.submitted.append(order)
        self.events.append(f"submit:{order.symbol}")
        action = self.submission_actions.popleft() if self.submission_actions else "filled"
        if action == "error":
            return self._order(order, "rejected")
        if action == "timeout":
            raise asyncio.TimeoutError(f"{self.venue} submission timed out")
        if action == "disconnect":
            raise ConnectionError(f"{self.venue} disconnected during submission")
        if action == "unknown":
            raise ExchangeAdapterError(
                f"{self.venue} submission result is unknown"
            )
        if action == "ambiguous":
            raise AmbiguousOrderError(self.venue, self.client_order_id(order))
        return self._order(order, action)

    async def cancel_order(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> ExchangeOrder:
        self.loop_ids.append(id(asyncio.get_running_loop()))
        identifier = exchange_order_id or client_order_id or "missing"
        self.canceled.append(identifier)
        self.events.append(f"cancel:{symbol}")
        order = self._find_order(identifier)
        action = (
            self.cancellation_actions.popleft()
            if self.cancellation_actions
            else self.cancel_status
        )
        if action == "ambiguous":
            raise AmbiguousOrderError(self.venue, order.client_order_id)
        if action == "error":
            raise ExchangeAdapterError(f"{self.venue} cancel failed")
        if action == "late_fill":
            action = "filled"
        return self._order(order, action)

    async def query_order(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> ExchangeOrder:
        self.loop_ids.append(id(asyncio.get_running_loop()))
        self.events.append(f"query:{symbol}")
        action = self.query_actions.popleft() if self.query_actions else "open"
        identifier = exchange_order_id or client_order_id or ""
        self.queried.append(identifier)
        external = self.external_orders.get(identifier)
        if external is not None:
            return external
        if action == "error":
            raise ExchangeAdapterError(
                f"{self.venue} reconciliation unavailable"
            )
        if action == "not_found":
            raise ExchangeAdapterError(f"{self.venue} order not found")
        if action == "timeout":
            raise asyncio.TimeoutError(f"{self.venue} query timed out")
        if action == "disconnect":
            raise ConnectionError(f"{self.venue} disconnected during query")
        order = self._find_order(identifier)
        return self._order(order, action)

    async def open_orders(
        self,
        symbol: str | None = None,
    ) -> tuple[ExchangeOrder, ...]:
        return ()

    async def trades(
        self,
        symbol: str,
        *,
        limit: int = 100,
    ) -> tuple[Any, ...]:
        return ()

    async def account(self, currency: str | None = None) -> AccountSnapshot:
        return AccountSnapshot(self.venue, currency, None, {})

    async def reconcile(
        self,
        oms: Any,
        symbol: str,
    ) -> ReconciliationSnapshot:
        return ReconciliationSnapshot(
            self.venue,
            symbol,
            0,
            (),
            (),
            await self.account(),
            (),
            (),
        )

    def _order(self, order: Order, status: str) -> ExchangeOrder:
        filled = {
            "filled": order.quantity,
            "partially_filled": order.quantity / Decimal(2),
            "canceled_partial": order.quantity / Decimal(2),
            "expired_partial": order.quantity / Decimal(2),
            "rejected_partial": order.quantity / Decimal(2),
        }.get(status, Decimal(0))
        exchange_status = {
            "canceled_partial": "canceled",
            "expired_partial": "expired",
            "rejected_partial": "rejected",
        }.get(status, status)
        average_fill_price = None
        if filled:
            average_fill_price = (
                self.reference_prices[-1]
                if self.average_fill_price is _REFERENCE_FILL_PRICE
                else self.average_fill_price
            )
        order_number = next(
            index
            for index, submitted in enumerate(self.submitted, start=1)
            if submitted is order
        )
        return ExchangeOrder(
            venue=self.response_venue or self.venue,
            symbol=self.response_symbol or order.symbol,
            exchange_order_id=f"{self.venue}-{order_number}",
            client_order_id=(
                self.response_client_order_id
                or self.client_order_id(order)
            ),
            status=exchange_status,
            side=order.side.value,
            order_type=order.order_type.value,
            quantity=order.quantity,
            filled_quantity=filled,
            price=order.price,
            average_fill_price=average_fill_price,
            timestamp_ms=1,
            raw={},
        )

    def add_external_fill(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: Decimal,
        exchange_order_id: str,
        client_order_id: str,
        average_fill_price: Decimal | None = Decimal("100"),
    ) -> ExchangeOrder:
        order = ExchangeOrder(
            venue=self.venue,
            symbol=symbol,
            exchange_order_id=exchange_order_id,
            client_order_id=client_order_id,
            status="filled",
            side=side.value,
            order_type=OrderType.MARKET.value,
            quantity=quantity,
            filled_quantity=quantity,
            price=None,
            average_fill_price=average_fill_price,
            timestamp_ms=2,
            raw={},
        )
        self.external_orders[exchange_order_id] = order
        self.external_orders[client_order_id] = order
        return order

    def _find_order(self, identifier: str) -> Order:
        if identifier.startswith(f"{self.venue}-") and identifier.removeprefix(
            f"{self.venue}-"
        ).isdigit():
            return self.submitted[int(identifier.rsplit("-", 1)[1]) - 1]
        return next(
            item
            for item in self.submitted
            if (
                item.client_order_id in identifier
                or identifier.endswith(item.client_order_id)
            )
        )


class DelayedFakeAdapter(FakeAdapter):
    def __init__(
        self,
        venue: str,
        *,
        block_submit: bool = False,
        block_cancel: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(venue, **kwargs)
        self.block_submit = block_submit
        self.block_cancel = block_cancel
        self.submit_started = asyncio.Event()
        self.cancel_started = asyncio.Event()
        self.allow_submit = asyncio.Event()
        self.allow_cancel = asyncio.Event()

    async def submit_order(self, order: Order) -> ExchangeOrder:
        self.submit_started.set()
        if self.block_submit:
            await self.allow_submit.wait()
        return await super().submit_order(order)

    async def cancel_order(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> ExchangeOrder:
        self.cancel_started.set()
        if self.block_cancel:
            await self.allow_cancel.wait()
        return await super().cancel_order(
            symbol,
            exchange_order_id=exchange_order_id,
            client_order_id=client_order_id,
        )


class DelayedNormalizeAdapter(FakeAdapter):
    def __init__(self, venue: str, **kwargs: Any) -> None:
        super().__init__(venue, **kwargs)
        self.normalize_started = asyncio.Event()
        self.allow_normalize = asyncio.Event()

    async def normalize_order(
        self,
        order: Order,
        *,
        reference_price: Decimal | None = None,
    ) -> NormalizedOrder:
        self.normalize_started.set()
        await self.allow_normalize.wait()
        return await super().normalize_order(
            order,
            reference_price=reference_price,
        )


class CollidingClientIdAdapter(FakeAdapter):
    def client_order_id(self, order: Order) -> str:
        return f"{self.venue}-constant-client-id"


class CountingExecutionCoordinator(ExecutionCoordinator):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.schedule_release_count = 0
        self.schedule_release_states: list[ScheduleState] = []
        self.plan_release_count = 0

    def _release_schedule_risk(self, schedule_id: str) -> None:
        was_released = self.schedule_result(schedule_id).risk_released
        super()._release_schedule_risk(schedule_id)
        if not was_released and self.schedule_result(schedule_id).risk_released:
            self.schedule_release_count += 1
            self.schedule_release_states.append(
                self.schedule_result(schedule_id).state
            )

    def _release_risk(self, result: Any) -> None:
        was_released = result.risk_released
        super()._release_risk(result)
        if not was_released and result.risk_released:
            self.plan_release_count += 1


def quote(
    venue: str,
    symbol: str,
    *,
    bid: str,
    ask: str,
    bid_size: str = "10",
    ask_size: str = "10",
    fee_bps: str = "0",
    latency_us: str = "0",
) -> VenueQuote:
    return VenueQuote(
        venue=venue,
        symbol=symbol,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=Decimal(bid_size),
        ask_size=Decimal(ask_size),
        fee_bps=Decimal(fee_bps),
        latency_us=Decimal(latency_us),
    )


def leg(symbol: str, side: Side, quantity: str, price: str) -> ExecutionLeg:
    return ExecutionLeg(
        symbol=symbol,
        side=side,
        quantity=Decimal(quantity),
        reference_price=Decimal(price),
        order_type=OrderType.MARKET,
    )


def recovery_order(
    recovery_id: str,
    symbol: str,
    side: Side,
    quantity: str,
    *,
    exchange_order_id: str | None = None,
    client_order_id: str | None = None,
) -> PlanRecoveryOrderEvidence:
    return PlanRecoveryOrderEvidence(
        recovery_id=recovery_id,
        canonical_symbol=symbol,
        side=side,
        filled_quantity=Decimal(quantity),
        venue="binance",
        exchange_order_id=(
            exchange_order_id
            if exchange_order_id is not None
            else f"exchange-{recovery_id}"
        ),
        client_order_id=client_order_id,
        reconciliation_evidence="query_order confirmed FILLED",
    )


def verified_recovery_order(
    adapter: FakeAdapter,
    recovery_id: str,
    symbol: str,
    side: Side,
    canonical_quantity: str,
    *,
    venue_quantity: str | None = None,
    exchange_order_id: str | None = None,
    client_order_id: str | None = None,
    average_fill_price: str = "100",
    use_client_fallback: bool = False,
) -> PlanRecoveryOrderEvidence:
    exchange_id = exchange_order_id or f"exchange-{recovery_id}"
    client_id = client_order_id or f"client-{recovery_id}"
    adapter.add_external_fill(
        symbol=symbol,
        side=side,
        quantity=Decimal(venue_quantity or canonical_quantity),
        exchange_order_id=exchange_id,
        client_order_id=client_id,
        average_fill_price=Decimal(average_fill_price),
    )
    return PlanRecoveryOrderEvidence(
        recovery_id=recovery_id,
        canonical_symbol=symbol,
        side=side,
        filled_quantity=Decimal(canonical_quantity),
        venue=adapter.venue,
        exchange_order_id=None if use_client_fallback else exchange_id,
        client_order_id=client_id if use_client_fallback else None,
        reconciliation_evidence="query_order confirmed FILLED",
    )


async def add_two_symbol_quotes(coordinator: ExecutionCoordinator) -> None:
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.update_quote(
        quote("okx", "ETHUSDT", bid="50", ask="51")
    )


@pytest.mark.asyncio
async def test_native_routing_accounts_for_fee_latency_and_executes() -> None:
    binance = FakeAdapter("binance")
    okx = FakeAdapter("okx")
    coordinator = ExecutionCoordinator(
        {"binance": binance, "okx": okx},
        latency_penalty_bps_per_ms=Decimal("5"),
    )
    await coordinator.update_quote(
        quote(
            "binance",
            "BTCUSDT",
            bid="99",
            ask="100",
            ask_size="1",
            fee_bps="10",
            latency_us="1000",
        )
    )
    await coordinator.update_quote(
        quote(
            "okx",
            "BTCUSDT",
            bid="99",
            ask="100.05",
            ask_size="1",
        )
    )
    await coordinator.update_quote(
        quote("binance", "ETHUSDT", bid="50", ask="51")
    )

    route = await coordinator.preview_route(
        "BTCUSDT",
        Side.BUY,
        Decimal("1"),
    )
    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "routed",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )

    assert route[0]["venue"] == "okx"
    assert result.outcome is ExecutionOutcome.FILLED
    assert result.native_state == "CLOSED"
    assert result.risk_released
    assert result.legs[0].orders[0].venue == "okx"


@pytest.mark.asyncio
async def test_sequential_failure_cancels_accepted_leg_and_closes_safely() -> None:
    binance = FakeAdapter(
        "binance",
        submissions=("open",),
        queries=("canceled",),
    )
    okx = FakeAdapter("okx", submissions=("error",))
    coordinator = ExecutionCoordinator({"binance": binance, "okx": okx})
    await add_two_symbol_quotes(coordinator)

    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "sequential-failure",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
            DispatchPolicy.SEQUENTIAL,
        )
    )

    assert result.outcome is ExecutionOutcome.FAILED
    assert [order.status for order in result.orders] == [
        RouteOrderStatus.CANCELED,
        RouteOrderStatus.REJECTED,
    ]
    assert binance.canceled
    assert result.state_history[-3:] == [
        "RECOVERY_REQUIRED",
        "CANCEL_REMAINING",
        "CLOSED",
    ]
    assert result.risk_released


@pytest.mark.asyncio
async def test_simultaneous_partial_failure_marks_hedge_and_unwind() -> None:
    binance = FakeAdapter("binance", submissions=("filled",))
    okx = FakeAdapter("okx", submissions=("error",))
    coordinator = ExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=DispatchPolicy.SIMULTANEOUS,
    )
    await add_two_symbol_quotes(coordinator)

    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "partial-failure",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )

    assert result.outcome is ExecutionOutcome.UNWIND_REQUIRED
    assert result.native_state == "UNWIND_REQUIRED"
    assert "HEDGE_REQUIRED" in result.state_history
    assert "UNWIND_REQUIRED" in result.state_history
    assert not result.risk_released


@pytest.mark.asyncio
async def test_partial_and_accepted_siblings_are_canceled_before_unwind() -> None:
    binance = FakeAdapter(
        "binance",
        submissions=("partially_filled",),
        cancellations=("canceled_partial",),
        queries=("canceled_partial",),
    )
    okx = FakeAdapter(
        "okx",
        submissions=("accepted",),
        cancellations=("canceled",),
        queries=("canceled",),
    )
    coordinator = ExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=DispatchPolicy.SIMULTANEOUS,
    )
    await add_two_symbol_quotes(coordinator)

    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "partial-accepted-siblings",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )

    assert result.outcome is ExecutionOutcome.UNWIND_REQUIRED
    assert [order.status for order in result.orders] == [
        RouteOrderStatus.CANCELED,
        RouteOrderStatus.CANCELED,
    ]
    assert result.orders[0].filled_quantity == Decimal("0.5")
    assert binance.events[-2:] == ["cancel:BTCUSDT", "query:BTCUSDT"]
    assert okx.events[-2:] == ["cancel:ETH-USDT", "query:ETH-USDT"]
    assert result.state_history[-2:] == ["HEDGE_REQUIRED", "UNWIND_REQUIRED"]
    assert not result.risk_released


@pytest.mark.asyncio
async def test_sequential_failure_skips_never_dispatched_route() -> None:
    binance = FakeAdapter("binance", submissions=("filled",))
    okx = FakeAdapter("okx", submissions=("error",))
    deribit = FakeAdapter(
        "deribit",
        quantity_step=Decimal("10"),
        minimum_quantity=Decimal("10"),
        quantity_unit=QuantityUnit.QUOTE,
    )
    coordinator = ExecutionCoordinator(
        {"binance": binance, "okx": okx, "deribit": deribit},
        dispatch_policy=DispatchPolicy.SEQUENTIAL,
    )
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.update_quote(
        quote("okx", "ETHUSDT", bid="50", ask="51")
    )
    await coordinator.update_quote(
        quote(
            "deribit",
            "BTC-PERPETUAL",
            bid="100000",
            ask="100000",
            bid_size="100",
            ask_size="100",
        )
    )

    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "filled-failed-pending",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
                leg("BTC-PERPETUAL", Side.SELL, "0.001", "100000"),
            ),
        )
    )

    assert [order.status for order in result.orders] == [
        RouteOrderStatus.FILLED,
        RouteOrderStatus.REJECTED,
        RouteOrderStatus.SKIPPED,
    ]
    assert result.orders[2].reconciled
    assert not deribit.submitted
    assert result.outcome is ExecutionOutcome.UNWIND_REQUIRED
    assert "RECONCILIATION_REQUIRED" not in result.state_history


@pytest.mark.asyncio
async def test_reconcile_terminal_plan_is_idempotent_after_release() -> None:
    binance = FakeAdapter("binance", submissions=("filled",))
    okx = FakeAdapter("okx", submissions=("filled",))
    coordinator = CountingExecutionCoordinator(
        {"binance": binance, "okx": okx}
    )
    await add_two_symbol_quotes(coordinator)
    initial = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "terminal-plan-idempotent",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )
    event_counts = (len(binance.events), len(okx.events))

    first = await coordinator.reconcile_plan("terminal-plan-idempotent")
    second = await coordinator.reconcile_plan("terminal-plan-idempotent")

    assert first is initial is second
    assert second.outcome is ExecutionOutcome.FILLED
    assert second.native_state == "CLOSED"
    assert second.state_history.count("CLOSED") == 1
    assert coordinator.plan_release_count == 1
    assert (len(binance.events), len(okx.events)) == event_counts
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_reconcile_failed_terminal_plan_does_not_release_twice() -> None:
    binance = FakeAdapter(
        "binance",
        submissions=("accepted",),
        cancellations=("canceled",),
        queries=("canceled",),
    )
    okx = FakeAdapter("okx", submissions=("error",))
    coordinator = CountingExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=DispatchPolicy.SEQUENTIAL,
    )
    await add_two_symbol_quotes(coordinator)
    initial = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "failed-terminal-idempotent",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )
    event_counts = (len(binance.events), len(okx.events))

    first = await coordinator.reconcile_plan("failed-terminal-idempotent")
    second = await coordinator.reconcile_plan("failed-terminal-idempotent")

    assert first is initial is second
    assert second.outcome is ExecutionOutcome.FAILED
    assert second.native_state == "CLOSED"
    assert second.state_history.count("CLOSED") == 1
    assert coordinator.plan_release_count == 1
    assert (len(binance.events), len(okx.events)) == event_counts
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_reconcile_waits_for_dispatch_recovery_before_closing_plan() -> None:
    binance = DelayedFakeAdapter(
        "binance",
        block_cancel=True,
        submissions=("open",),
        cancellations=("canceled",),
        queries=("canceled",),
    )
    okx = FakeAdapter("okx", submissions=("error",))
    coordinator = CountingExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=DispatchPolicy.SIMULTANEOUS,
    )
    await add_two_symbol_quotes(coordinator)

    execute = asyncio.create_task(
        coordinator.execute_plan(
            ExecutionPlanRequest(
                "dispatch-reconcile-recovery-race",
                (
                    leg("BTCUSDT", Side.BUY, "1", "100"),
                    leg("ETHUSDT", Side.SELL, "1", "50"),
                ),
            )
        )
    )
    await binance.cancel_started.wait()
    first_reconcile = asyncio.create_task(
        coordinator.reconcile_plan("dispatch-reconcile-recovery-race")
    )
    second_reconcile = asyncio.create_task(
        coordinator.reconcile_plan("dispatch-reconcile-recovery-race")
    )
    await asyncio.sleep(0)

    assert not first_reconcile.done()
    assert not second_reconcile.done()
    assert coordinator.reserved_notional == Decimal("150")

    binance.allow_cancel.set()
    results = await asyncio.gather(
        execute,
        first_reconcile,
        second_reconcile,
    )

    assert results[0] is results[1] is results[2]
    assert results[0].outcome is ExecutionOutcome.FAILED
    assert results[0].native_state == "CLOSED"
    assert results[0].state_history.count("RECOVERY_REQUIRED") == 1
    assert results[0].state_history.count("CLOSED") == 1
    assert len(binance.canceled) == 1
    assert coordinator.plan_release_count == 1
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_repeated_reconcile_waits_for_concurrent_filled_dispatch() -> None:
    binance = DelayedFakeAdapter(
        "binance",
        block_submit=True,
        submissions=("filled",),
    )
    okx = DelayedFakeAdapter(
        "okx",
        block_submit=True,
        submissions=("filled",),
    )
    coordinator = CountingExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=DispatchPolicy.SIMULTANEOUS,
    )
    await add_two_symbol_quotes(coordinator)

    execute = asyncio.create_task(
        coordinator.execute_plan(
            ExecutionPlanRequest(
                "concurrent-filled-reconcile",
                (
                    leg("BTCUSDT", Side.BUY, "1", "100"),
                    leg("ETHUSDT", Side.SELL, "1", "50"),
                ),
            )
        )
    )
    await asyncio.gather(
        binance.submit_started.wait(),
        okx.submit_started.wait(),
    )
    first_reconcile = asyncio.create_task(
        coordinator.reconcile_plan("concurrent-filled-reconcile")
    )
    second_reconcile = asyncio.create_task(
        coordinator.reconcile_plan("concurrent-filled-reconcile")
    )
    await asyncio.sleep(0)

    assert not first_reconcile.done()
    assert not second_reconcile.done()
    assert coordinator.reserved_notional == Decimal("150")

    binance.allow_submit.set()
    okx.allow_submit.set()
    results = await asyncio.gather(
        execute,
        first_reconcile,
        second_reconcile,
    )

    assert results[0] is results[1] is results[2]
    assert results[0].outcome is ExecutionOutcome.FILLED
    assert [order.status for order in results[0].orders] == [
        RouteOrderStatus.FILLED,
        RouteOrderStatus.FILLED,
    ]
    assert results[0].state_history.count("CLOSED") == 1
    assert not binance.queried
    assert not okx.queried
    assert coordinator.plan_release_count == 1
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_ambiguous_submission_requires_reconciliation() -> None:
    binance = FakeAdapter("binance", submissions=("open",))
    okx = FakeAdapter(
        "okx",
        submissions=("ambiguous",),
        queries=("error",),
    )
    coordinator = ExecutionCoordinator({"binance": binance, "okx": okx})
    await add_two_symbol_quotes(coordinator)

    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "ambiguous",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )

    assert result.outcome is ExecutionOutcome.RECONCILIATION_REQUIRED
    assert result.native_state == "RECOVERY_REQUIRED"
    assert not result.orders[1].reconciled
    assert result.orders[1].cancel_error
    assert binance.canceled
    assert not result.risk_released


@pytest.mark.asyncio
async def test_terminal_reconciliation_clears_stale_cancel_error() -> None:
    binance = FakeAdapter(
        "binance",
        submissions=("open",),
        cancellations=("error",),
        queries=("error",),
    )
    okx = FakeAdapter("okx", submissions=("error",))
    coordinator = ExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=DispatchPolicy.SEQUENTIAL,
    )
    await add_two_symbol_quotes(coordinator)

    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "stale-cancel-error",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )

    assert result.orders[0].cancel_error
    binance.query_actions.append("canceled")

    reconciled = await coordinator.reconcile_plan(result.plan_id)

    assert reconciled.orders[0].status is RouteOrderStatus.CANCELED
    assert reconciled.orders[0].cancel_error is None
    assert reconciled.orders[0].reconciled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [DispatchPolicy.SEQUENTIAL, DispatchPolicy.SIMULTANEOUS],
)
@pytest.mark.parametrize(
    "submission_action",
    ["timeout", "disconnect", "ambiguous", "unknown"],
)
async def test_uncertain_plan_submit_queries_client_id_and_releases_only_when_filled(
    policy: DispatchPolicy,
    submission_action: str,
) -> None:
    binance = FakeAdapter(
        "binance",
        submissions=(submission_action,),
        queries=("filled",),
    )
    okx = FakeAdapter("okx", submissions=("filled",))
    coordinator = CountingExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=policy,
    )
    await add_two_symbol_quotes(coordinator)

    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            f"uncertain-filled-{policy.value}-{submission_action}",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )

    assert result.outcome is ExecutionOutcome.FILLED
    assert [order.status for order in result.orders] == [
        RouteOrderStatus.FILLED,
        RouteOrderStatus.FILLED,
    ]
    assert all(order.reconciled for order in result.orders)
    assert binance.queried == [
        binance.client_order_id(binance.submitted[0])
    ]
    assert len(binance.submitted) == len(okx.submitted) == 1
    assert coordinator.plan_release_count == 1
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_identity",
    [
        {"response_venue": "okx"},
        {"response_symbol": "ETHUSDT"},
        {"response_client_order_id": "binance-wrong-client"},
    ],
)
async def test_ems_rejects_mismatched_adapter_response_identity(
    response_identity: dict[str, str],
) -> None:
    adapter = FakeAdapter(
        "binance",
        submissions=("filled",),
        **response_identity,
    )
    okx = FakeAdapter("okx", submissions=("filled",))
    coordinator = CountingExecutionCoordinator(
        {"binance": adapter, "okx": okx}
    )
    await add_two_symbol_quotes(coordinator)

    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            f"identity-mismatch-{next(iter(response_identity))}",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )

    routed = next(order for order in result.orders if order.venue == "binance")
    assert result.outcome is ExecutionOutcome.RECONCILIATION_REQUIRED
    assert routed.status is RouteOrderStatus.AMBIGUOUS
    assert routed.exchange_order is None
    assert routed.cumulative_filled_quantity == 0
    assert not routed.reconciled
    assert "adapter response identity mismatch" in (
        routed.reconciliation_error or ""
    )
    assert not result.risk_released
    assert coordinator.reserved_notional == Decimal("150")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [DispatchPolicy.SEQUENTIAL, DispatchPolicy.SIMULTANEOUS],
)
async def test_timeout_query_finds_live_plan_order_without_duplicate_submit(
    policy: DispatchPolicy,
) -> None:
    binance = FakeAdapter(
        "binance",
        submissions=("timeout",),
        queries=("open",),
    )
    okx = FakeAdapter("okx", submissions=("accepted",))
    coordinator = CountingExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=policy,
    )
    await add_two_symbol_quotes(coordinator)

    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            f"timeout-live-{policy.value}",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )

    assert result.outcome is ExecutionOutcome.OPEN
    assert [order.status for order in result.orders] == [
        RouteOrderStatus.ACCEPTED,
        RouteOrderStatus.ACCEPTED,
    ]
    assert not any(order.reconciled for order in result.orders)
    assert binance.queried == [
        binance.client_order_id(binance.submitted[0])
    ]
    assert len(binance.submitted) == len(okx.submitted) == 1
    assert coordinator.plan_release_count == 0
    assert coordinator.reserved_notional == Decimal("150")


@pytest.mark.asyncio
@pytest.mark.parametrize("query_action", ["not_found", "timeout"])
async def test_uncertain_plan_query_failure_stays_ambiguous_and_reserved(
    query_action: str,
) -> None:
    binance = FakeAdapter(
        "binance",
        submissions=("timeout",),
        queries=(query_action, query_action),
        cancellations=("ambiguous",),
    )
    okx = FakeAdapter("okx")
    coordinator = CountingExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=DispatchPolicy.SEQUENTIAL,
    )
    await add_two_symbol_quotes(coordinator)

    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            f"uncertain-query-{query_action}",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )

    assert result.outcome is ExecutionOutcome.RECONCILIATION_REQUIRED
    assert result.orders[0].status is RouteOrderStatus.AMBIGUOUS
    assert not result.orders[0].reconciled
    assert result.orders[1].status is RouteOrderStatus.SKIPPED
    assert binance.queried == [
        binance.client_order_id(binance.submitted[0]),
        binance.client_order_id(binance.submitted[0]),
    ]
    assert len(binance.submitted) == 1
    assert not okx.submitted
    assert coordinator.plan_release_count == 0
    assert coordinator.reserved_notional == Decimal("150")


@pytest.mark.asyncio
async def test_delayed_timeout_submit_reconciles_without_resubmission() -> None:
    binance = DelayedFakeAdapter(
        "binance",
        block_submit=True,
        submissions=("timeout",),
        queries=("filled",),
    )
    okx = FakeAdapter("okx", submissions=("filled",))
    coordinator = CountingExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=DispatchPolicy.SEQUENTIAL,
    )
    await add_two_symbol_quotes(coordinator)

    execute = asyncio.create_task(
        coordinator.execute_plan(
            ExecutionPlanRequest(
                "delayed-timeout-submit",
                (
                    leg("BTCUSDT", Side.BUY, "1", "100"),
                    leg("ETHUSDT", Side.SELL, "1", "50"),
                ),
            )
        )
    )
    await binance.submit_started.wait()

    assert not execute.done()
    assert coordinator.reserved_notional == Decimal("150")
    assert not binance.queried

    binance.allow_submit.set()
    result = await execute

    assert result.outcome is ExecutionOutcome.FILLED
    assert len(binance.submitted) == 1
    assert binance.queried == [
        binance.client_order_id(binance.submitted[0])
    ]
    assert coordinator.plan_release_count == 1
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_twap_vwap_slices_use_native_loop_ordering() -> None:
    binance = FakeAdapter("binance")
    coordinator = ExecutionCoordinator({"binance": binance})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    scheduled_leg = leg("BTCUSDT", Side.BUY, "1", "100")

    first = await coordinator.schedule_twap(
        "first",
        scheduled_leg,
        start_ns=10,
        end_ns=20,
        slice_count=2,
    )
    second = await coordinator.schedule_vwap(
        "second",
        scheduled_leg,
        start_ns=10,
        end_ns=20,
        volume_weights=(Decimal("1"), Decimal("3")),
    )
    due = await coordinator.run_due(20)

    assert [item.schedule_id for item in due] == [
        "first",
        "second",
        "first",
        "second",
    ]
    assert [timestamp for timestamp, _ in first.slices] == [10, 20]
    assert [quantity for _, quantity in second.slices] == [
        Decimal("0.25"),
        Decimal("0.75"),
    ]
    assert all(item.outcome is ExecutionOutcome.FILLED for item in due)


@pytest.mark.asyncio
async def test_coordinator_rejects_cross_loop_access_and_dispatches_on_one_loop() -> None:
    binance = FakeAdapter("binance")
    okx = FakeAdapter("okx")
    coordinator = ExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=DispatchPolicy.SIMULTANEOUS,
    )
    await add_two_symbol_quotes(coordinator)
    await coordinator.execute_plan(
        ExecutionPlanRequest(
            "single-loop",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )

    owner_loop = id(asyncio.get_running_loop())
    assert set(binance.loop_ids + okx.loop_ids) == {owner_loop}

    def run_other_loop() -> None:
        asyncio.run(
            coordinator.preview_route("BTCUSDT", Side.BUY, Decimal("1"))
        )

    with pytest.raises(RuntimeError, match="owning asyncio loop"):
        await asyncio.to_thread(run_other_loop)


@pytest.mark.asyncio
async def test_config_and_cli_are_guarded_inspection_only() -> None:
    config = AppConfig(ems=EMSConfig(enabled_venues=("binance",)))

    inspection = _ems_inspection(config)
    # Adapters are injected so the suite never depends on live exchange
    # connectivity: Binance blocks requests from restricted regions, which
    # made this test fail on hosted CI runners while passing locally.
    demo = await _ems_demo(config, {"binance": FakeAdapter("binance")})

    assert inspection["live_trading_enabled"] is False
    assert "reconcile_schedule" in inspection["schedule_lifecycle"]["reconciliation"]
    assert demo["mode"] == "inspection_only"
    assert demo["orders_submitted"] == 0
    assert len(demo["twap"]) == 4
    assert demo["twap_state"] == "scheduled"
    assert hft.ExecutionCoordinator is ExecutionCoordinator
    assert hft.OKXDemoAdapter is not None
    assert hft.DeribitTestAdapter is not None


def test_config_rejects_unsupported_ems_venues_and_live_endpoints() -> None:
    with pytest.raises(ValueError):
        EMSConfig(enabled_venues=("coinbase",))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AppConfig(
            okx_demo_execution={
                "rest_base_url": "https://www.okx.com.evil.example"
            }
        )
    with pytest.raises(ValueError):
        AppConfig(
            deribit_test_execution={
                "rest_base_url": "https://www.deribit.com/api/v2"
            }
        )


@pytest.mark.asyncio
async def test_schedule_reserves_aggregate_exposure_and_rejects_over_limit() -> None:
    adapter = FakeAdapter("binance")
    coordinator = ExecutionCoordinator(
        {"binance": adapter},
        max_reserved_notional=Decimal("99"),
    )
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )

    with pytest.raises(ScheduleRiskLimitError, match="max_reserved_notional"):
        await coordinator.schedule_twap(
            "over-limit",
            leg("BTCUSDT", Side.BUY, "1", "100"),
            start_ns=10,
            end_ns=20,
            slice_count=2,
        )

    assert coordinator.reserved_notional == 0
    assert not coordinator.schedule_is_reserved("over-limit")


@pytest.mark.asyncio
async def test_schedule_reservation_is_held_until_all_slices_fill() -> None:
    adapter = FakeAdapter(
        "binance",
        quantity_step=Decimal("0.1"),
        minimum_quantity=Decimal("0.1"),
    )
    coordinator = ExecutionCoordinator(
        {"binance": adapter},
        max_reserved_notional=Decimal("200"),
    )
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    schedule = await coordinator.schedule_twap(
        "reserved",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=20,
        slice_count=2,
    )

    assert schedule.normalized_quantity == Decimal("1")
    assert coordinator.reserved_notional == Decimal("100")
    await coordinator.run_due(10)
    assert coordinator.reserved_notional == Decimal("100")
    await coordinator.run_due(20)
    assert coordinator.reserved_notional == 0
    assert not coordinator.schedule_is_reserved("reserved")


@pytest.mark.asyncio
async def test_schedule_releases_after_completed_failure_cancellation() -> None:
    adapter = FakeAdapter(
        "binance",
        submissions=("open", "error"),
        queries=("open", "rejected", "canceled"),
        quantity_step=Decimal("0.1"),
        minimum_quantity=Decimal("0.1"),
    )
    coordinator = ExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.schedule_twap(
        "recovered",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=20,
        slice_count=2,
    )

    results = await coordinator.run_due(20)

    assert [result.outcome for result in results] == [
        ExecutionOutcome.CANCELED,
        ExecutionOutcome.FAILED,
    ]
    assert adapter.canceled
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_okx_accepted_open_schedule_stays_reserved_until_reconciled() -> None:
    okx = FakeAdapter("okx", submissions=("accepted",), queries=("open",))
    coordinator = ExecutionCoordinator({"okx": okx})
    await coordinator.update_quote(
        quote("okx", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.schedule_twap(
        "okx-open",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )

    slices = await coordinator.run_due(10)
    schedule = await coordinator.reconcile_schedule("okx-open")

    assert slices[0].outcome is ExecutionOutcome.OPEN
    assert schedule.state is ScheduleState.OPEN
    assert schedule.orders[0].status is RouteOrderStatus.ACCEPTED
    assert not schedule.orders[0].reconciled
    assert coordinator.reserved_notional == Decimal("100")


@pytest.mark.asyncio
async def test_complete_schedule_recovery_queries_cancels_and_confirms_terminal() -> None:
    okx = FakeAdapter(
        "okx",
        submissions=("accepted",),
        queries=("open", "canceled"),
        cancellations=("cancel_requested",),
    )
    coordinator = ExecutionCoordinator({"okx": okx})
    await coordinator.update_quote(
        quote("okx", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.schedule_twap(
        "cancel-terminal",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )
    await coordinator.run_due(10)

    schedule = await coordinator.complete_schedule_recovery(
        "cancel-terminal"
    )

    assert schedule.state is ScheduleState.CANCELED
    assert schedule.risk_released
    assert schedule.orders[0].status is RouteOrderStatus.CANCELED
    assert okx.events[-3:] == [
        "query:BTC-USDT",
        "cancel:BTC-USDT",
        "query:BTC-USDT",
    ]
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_cancel_schedule_tracks_and_skips_queued_routes() -> None:
    adapter = FakeAdapter(
        "binance",
        submissions=("accepted",),
        queries=("open", "canceled"),
        cancellations=("cancel_requested",),
    )
    coordinator = ExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.schedule_twap(
        "cancel-queued",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=20,
        slice_count=2,
    )
    await coordinator.run_due(10)

    schedule = await coordinator.cancel_schedule("cancel-queued")

    assert len(schedule.orders) == 2
    assert len(schedule.slices) == 2
    assert [order.status for order in schedule.orders] == [
        RouteOrderStatus.CANCELED,
        RouteOrderStatus.SKIPPED,
    ]
    assert schedule.queued_slice_count == 0
    assert schedule.state is ScheduleState.CANCELED
    assert coordinator.reserved_notional == 0
    assert await coordinator.run_due(20) == ()


@pytest.mark.asyncio
async def test_partial_schedule_cancel_requires_unwind_before_release() -> None:
    adapter = FakeAdapter(
        "binance",
        submissions=("partially_filled",),
        queries=(
            "open",
            "open",
            "canceled_partial",
            "canceled_partial",
            "canceled_partial",
        ),
        cancellations=("canceled_partial",),
    )
    coordinator = CountingExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.schedule_vwap(
        "partial",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=10,
        volume_weights=(Decimal("1"),),
    )

    slices = await coordinator.run_due(10)
    first = await coordinator.reconcile_schedule("partial")
    first_state = first.state
    first_fill = first.cumulative_filled_quantity
    terminal = await coordinator.cancel_schedule("partial")

    assert slices[0].outcome is ExecutionOutcome.PARTIAL
    assert first_state is ScheduleState.PARTIALLY_FILLED
    assert first_fill == Decimal("0.5")
    assert terminal.state is ScheduleState.UNWIND_REQUIRED
    assert terminal.cumulative_filled_quantity == Decimal("0.5")
    assert terminal.remaining_quantity == Decimal("0.5")
    assert terminal.residual_canonical_exposure == Decimal("0.5")
    assert terminal.orders[0].filled_quantity == Decimal("0.5")
    assert not terminal.risk_released
    assert coordinator.reserved_notional == Decimal("100")

    repeated = await coordinator.reconcile_schedule("partial")
    repeated_state = repeated.state
    repeated_unwind_count = repeated.state_history.count(
        ScheduleState.UNWIND_REQUIRED
    )
    recovery = verified_recovery_order(
        adapter,
        "partial-unwind",
        "BTCUSDT",
        Side.SELL,
        "0.5",
    )
    recovered = await coordinator.complete_schedule_recovery(
        "partial",
        cancel_open_orders=False,
        unwind_completed=True,
        recovery_orders=(recovery,),
    )

    assert repeated_state is ScheduleState.UNWIND_REQUIRED
    assert repeated_unwind_count == 1
    assert recovered.state is ScheduleState.RECOVERED
    assert recovered.recovery_completed
    assert recovered.residual_canonical_exposure == 0
    assert recovered.risk_released
    assert coordinator.schedule_release_count == 1
    assert coordinator.schedule_release_states == [ScheduleState.RECOVERED]
    assert coordinator.reserved_notional == 0

    final = await coordinator.reconcile_schedule("partial")

    assert final is recovered
    assert coordinator.schedule_release_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_action", "terminal_status"),
    [
        ("expired_partial", RouteOrderStatus.CANCELED),
        ("rejected_partial", RouteOrderStatus.REJECTED),
    ],
)
async def test_partial_schedule_terminal_reconciliation_requires_unwind(
    terminal_action: str,
    terminal_status: RouteOrderStatus,
) -> None:
    adapter = FakeAdapter(
        "binance",
        submissions=("partially_filled",),
        queries=(terminal_action, terminal_action, terminal_action),
    )
    coordinator = CountingExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.schedule_twap(
        f"partial-{terminal_action}",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=20,
        slice_count=2,
    )
    await coordinator.run_due(10)

    first = await coordinator.reconcile_schedule(
        f"partial-{terminal_action}"
    )
    repeated = await coordinator.reconcile_schedule(
        f"partial-{terminal_action}"
    )

    assert first is repeated
    assert repeated.state is ScheduleState.UNWIND_REQUIRED
    assert [order.status for order in repeated.orders] == [
        terminal_status,
        RouteOrderStatus.SKIPPED,
    ]
    assert repeated.queued_slice_count == 0
    assert repeated.cumulative_filled_quantity == Decimal("0.25")
    assert repeated.residual_canonical_exposure == Decimal("0.25")
    assert repeated.state_history.count(ScheduleState.UNWIND_REQUIRED) == 1
    assert not repeated.risk_released
    assert coordinator.schedule_release_count == 0
    assert coordinator.reserved_notional == Decimal("100")
    assert await coordinator.run_due(20) == ()

    recovery = verified_recovery_order(
        adapter,
        f"{terminal_action}-unwind",
        "BTCUSDT",
        Side.SELL,
        "0.25",
    )
    recovered = await coordinator.complete_schedule_recovery(
        f"partial-{terminal_action}",
        cancel_open_orders=False,
        unwind_completed=True,
        recovery_orders=(recovery,),
    )

    assert recovered.state is ScheduleState.RECOVERED
    assert recovered.risk_released
    assert recovered.residual_canonical_exposure == 0
    assert coordinator.schedule_release_count == 1
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_ambiguous_schedule_query_holds_reservation() -> None:
    adapter = FakeAdapter(
        "binance",
        submissions=("accepted",),
        queries=("error",),
    )
    coordinator = ExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.schedule_twap(
        "ambiguous-query",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )
    await coordinator.run_due(10)

    schedule = await coordinator.reconcile_schedule("ambiguous-query")

    assert schedule.state is ScheduleState.RECOVERY_REQUIRED
    assert schedule.orders[0].status is RouteOrderStatus.AMBIGUOUS
    assert schedule.reconciliation_errors
    assert not schedule.risk_released
    assert coordinator.reserved_notional == Decimal("100")


@pytest.mark.asyncio
async def test_schedule_cancel_reconciliation_captures_late_fill() -> None:
    adapter = FakeAdapter(
        "binance",
        submissions=("accepted",),
        queries=("open", "filled"),
        cancellations=("cancel_requested",),
    )
    coordinator = ExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.schedule_twap(
        "late-schedule-fill",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )
    await coordinator.run_due(10)

    schedule = await coordinator.cancel_schedule("late-schedule-fill")

    assert schedule.state is ScheduleState.FILLED
    assert schedule.cumulative_filled_quantity == Decimal("1")
    assert schedule.orders[0].status is RouteOrderStatus.FILLED
    assert schedule.risk_released
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_schedule_reconciliation_is_idempotent_after_release() -> None:
    adapter = FakeAdapter(
        "binance",
        submissions=("filled",),
        queries=("filled", "filled"),
    )
    coordinator = ExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.schedule_twap(
        "idempotent",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )
    await coordinator.run_due(10)

    first = await coordinator.reconcile_schedule("idempotent")
    second = await coordinator.reconcile_schedule("idempotent")

    assert first is second
    assert second.state is ScheduleState.FILLED
    assert second.cumulative_filled_quantity == Decimal("1")
    assert second.state_history.count(ScheduleState.FILLED) == 1
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_plan_and_schedule_order_namespaces_do_not_collide() -> None:
    adapter = FakeAdapter(
        "binance",
        submissions=("filled", "filled", "filled", "open"),
        queries=("filled", "filled"),
    )
    coordinator = ExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.update_quote(
        quote("binance", "ETHUSDT", bid="50", ask="51")
    )
    await coordinator.schedule_twap(
        "foo",
        leg("BTCUSDT", Side.BUY, "2", "100"),
        start_ns=10,
        end_ns=20,
        slice_count=2,
    )

    plan = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "foo:1",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )
    schedule_slice_one = coordinator.schedule_result("foo").orders[1]
    plan_route_zero = plan.orders[0]

    assert schedule_slice_one.order.order_id != plan_route_zero.order.order_id
    assert (
        schedule_slice_one.order.client_order_id
        != plan_route_zero.order.client_order_id
    )
    assert plan.risk_released

    await coordinator.run_due(20)
    query_count = len(adapter.queried)
    submission_count = len(adapter.submitted)
    schedule_exchange_ids = [
        routed.exchange_order.exchange_order_id
        for routed in coordinator.schedule_result("foo").orders
    ]
    reconciled = await coordinator.reconcile_schedule("foo")
    repeated = await coordinator.reconcile_schedule("foo")

    assert reconciled is repeated
    assert reconciled.state is ScheduleState.FILLED
    assert reconciled.risk_released
    assert len({order.order_id for order in adapter.submitted}) == 4
    assert len({order.client_order_id for order in adapter.submitted}) == 4
    assert adapter.queried[query_count:] == schedule_exchange_ids
    assert plan_route_zero.exchange_order is not None
    assert (
        reconciled.orders[1].exchange_order.exchange_order_id
        != plan_route_zero.exchange_order.exchange_order_id
    )
    assert len(adapter.submitted) == submission_count


@pytest.mark.asyncio
async def test_coordinator_rejects_client_id_collision_before_submission() -> None:
    adapter = CollidingClientIdAdapter("binance")
    coordinator = ExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.update_quote(
        quote("binance", "ETHUSDT", bid="50", ask="51")
    )
    await coordinator.schedule_twap(
        "reserved-id",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )

    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "colliding-plan",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )

    assert result.outcome is ExecutionOutcome.FAILED
    assert not adapter.submitted
    assert coordinator.schedule_is_reserved("reserved-id")
    assert coordinator.reserved_notional == Decimal("100")


@pytest.mark.asyncio
async def test_concurrent_duplicate_schedule_is_reserved_before_await() -> None:
    adapter = DelayedNormalizeAdapter(
        "binance",
        submissions=("accepted",),
        queries=("open", "canceled"),
        cancellations=("canceled",),
    )
    coordinator = ExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    scheduled_leg = leg("BTCUSDT", Side.BUY, "1", "100")

    first_task = asyncio.create_task(
        coordinator.schedule_twap(
            "duplicate-race",
            scheduled_leg,
            start_ns=10,
            end_ns=10,
            slice_count=1,
        )
    )
    await adapter.normalize_started.wait()
    second_task = asyncio.create_task(
        coordinator.schedule_twap(
            "duplicate-race",
            scheduled_leg,
            start_ns=10,
            end_ns=10,
            slice_count=1,
        )
    )
    await asyncio.sleep(0)
    adapter.allow_normalize.set()
    outcomes = await asyncio.gather(
        first_task,
        second_task,
        return_exceptions=True,
    )

    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, ValueError) for outcome in outcomes) == 1
    slices = await coordinator.run_due(10)
    assert len(slices) == 1
    assert len(adapter.submitted) == 1
    assert coordinator.schedule_result("duplicate-race").state is ScheduleState.OPEN

    terminal = await coordinator.cancel_schedule("duplicate-race")
    assert terminal.state is ScheduleState.CANCELED
    assert len(adapter.submitted) == 1


@pytest.mark.asyncio
async def test_run_schedule_coordinates_with_cancel_during_sleep() -> None:
    adapter = FakeAdapter("binance")
    coordinator = CountingExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.schedule_twap(
        "run-cancel-race",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )
    sleep_started = asyncio.Event()
    allow_sleep = asyncio.Event()

    async def controlled_sleep(_: float) -> None:
        sleep_started.set()
        await allow_sleep.wait()

    run_task = asyncio.create_task(
        coordinator.run_schedule(
            "run-cancel-race",
            clock_ns=lambda: 0,
            sleep=controlled_sleep,
        )
    )
    await sleep_started.wait()

    assert coordinator.reserved_notional == Decimal("100")
    canceled = await coordinator.cancel_schedule("run-cancel-race")
    assert canceled.state is ScheduleState.CANCELED
    assert canceled.risk_released

    allow_sleep.set()
    slices = await run_task

    assert len(slices) == 1
    assert slices[0].outcome is ExecutionOutcome.CANCELED
    assert not adapter.submitted
    assert coordinator.schedule_release_states == [ScheduleState.CANCELED]
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_failed_cancel_and_query_require_explicit_operator_resolution() -> None:
    adapter = FakeAdapter(
        "binance",
        submissions=("accepted",),
        queries=("open", "error", "open"),
        cancellations=("error",),
    )
    coordinator = CountingExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.schedule_twap(
        "operator-recovery",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )
    await coordinator.run_due(10)

    unresolved = await coordinator.cancel_schedule("operator-recovery")
    assert unresolved.state is ScheduleState.RECOVERY_REQUIRED
    assert not unresolved.risk_released
    assert coordinator.reserved_notional == Decimal("100")

    with pytest.raises(ValueError, match="bare operator resolution"):
        await coordinator.complete_schedule_recovery(
            "operator-recovery",
            cancel_open_orders=False,
            operator_resolved=True,
        )

    assert not unresolved.risk_released
    assert coordinator.reserved_notional == Decimal("100")

    resolved = await coordinator.resolve_schedule_recovery(
        "operator-recovery",
        reason="venue support confirmed cancellation",
        external_evidence="support ticket OPS-41",
        order_evidence=(
            ScheduleRecoveryOrderEvidence(
                venue="binance",
                exchange_order_id="binance-1",
                status=RouteOrderStatus.CANCELED,
                reconciliation_evidence=(
                    "venue support confirmed terminal cancellation"
                ),
            ),
        ),
        cancel_open_orders=False,
    )
    repeated = await coordinator.resolve_schedule_recovery(
        "operator-recovery",
        reason="venue support confirmed cancellation",
        external_evidence="support ticket OPS-41",
        order_evidence=(
            ScheduleRecoveryOrderEvidence(
                venue="binance",
                exchange_order_id="binance-1",
                status=RouteOrderStatus.CANCELED,
                reconciliation_evidence=(
                    "venue support confirmed terminal cancellation"
                ),
            ),
        ),
        cancel_open_orders=False,
    )

    assert repeated is resolved
    assert resolved.state is ScheduleState.OPERATOR_RESOLVED
    assert resolved.operator_resolved
    assert (
        resolved.operator_resolution_reason
        == "venue support confirmed cancellation"
    )
    assert resolved.operator_resolution_evidence == "support ticket OPS-41"
    assert len(resolved.operator_order_evidence) == 1
    assert resolved.risk_released
    assert coordinator.schedule_release_count == 1
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_operator_schedule_resolution_requires_every_live_order() -> None:
    adapter = FakeAdapter(
        "binance",
        submissions=("accepted", "accepted"),
        queries=("error", "error", "error", "error"),
    )
    coordinator = CountingExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.schedule_twap(
        "all-live-orders",
        leg("BTCUSDT", Side.BUY, "2", "100"),
        start_ns=10,
        end_ns=20,
        slice_count=2,
    )
    await coordinator.run_due(20)

    with pytest.raises(
        RuntimeError,
        match="terminal evidence for every live exchange order",
    ):
        await coordinator.resolve_schedule_recovery(
            "all-live-orders",
            reason="manual venue reconciliation",
            external_evidence="ticket OPS-42",
            order_evidence=(
                ScheduleRecoveryOrderEvidence(
                    venue="binance",
                    exchange_order_id="binance-1",
                    status=RouteOrderStatus.CANCELED,
                    reconciliation_evidence="first order canceled",
                ),
            ),
            cancel_open_orders=False,
        )

    result = coordinator.schedule_result("all-live-orders")
    assert not result.operator_order_evidence
    assert not result.operator_resolved
    assert not result.risk_released
    assert coordinator.schedule_release_count == 0
    assert coordinator.reserved_notional == Decimal("200")


@pytest.mark.asyncio
async def test_twap_thirds_allocate_lot_residual_to_last_slice() -> None:
    adapter = FakeAdapter(
        "binance",
        quantity_step=Decimal("0.1"),
        minimum_quantity=Decimal("0.1"),
    )
    coordinator = ExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )

    schedule = await coordinator.schedule_twap(
        "thirds",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=30,
        slice_count=3,
    )
    results = await coordinator.run_due(30)

    assert [quantity for _, quantity in schedule.slices] == [
        Decimal("0.3"),
        Decimal("0.3"),
        Decimal("0.4"),
    ]
    assert [result.quantity for result in results] == [
        Decimal("0.3"),
        Decimal("0.3"),
        Decimal("0.4"),
    ]
    assert sum(
        (order.quantity for order in adapter.submitted),
        start=Decimal(0),
    ) == Decimal("1")


@pytest.mark.asyncio
async def test_vwap_uneven_weights_preserve_normalized_conservation() -> None:
    adapter = FakeAdapter(
        "binance",
        quantity_step=Decimal("0.1"),
        minimum_quantity=Decimal("0.1"),
    )
    coordinator = ExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )

    schedule = await coordinator.schedule_vwap(
        "uneven",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=30,
        volume_weights=(Decimal("1"), Decimal("2"), Decimal("4")),
    )

    assert [quantity for _, quantity in schedule.slices] == [
        Decimal("0.1"),
        Decimal("0.3"),
        Decimal("0.6"),
    ]
    assert sum(
        (quantity for _, quantity in schedule.slices),
        start=Decimal(0),
    ) == schedule.normalized_quantity == Decimal("1")


@pytest.mark.asyncio
async def test_multi_venue_route_reallocates_rounding_residual_deterministically() -> None:
    binance = FakeAdapter(
        "binance",
        quantity_step=Decimal("0.1"),
        minimum_quantity=Decimal("0.1"),
    )
    okx = FakeAdapter(
        "okx",
        quantity_step=Decimal("0.05"),
        minimum_quantity=Decimal("0.05"),
    )
    coordinator = ExecutionCoordinator({"binance": binance, "okx": okx})
    await coordinator.update_quote(
        quote(
            "binance",
            "BTCUSDT",
            bid="99",
            ask="100",
            ask_size="0.55",
        )
    )
    await coordinator.update_quote(
        quote(
            "okx",
            "BTCUSDT",
            bid="98",
            ask="101",
            ask_size="1",
        )
    )

    schedule = await coordinator.schedule_twap(
        "multi-venue",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )
    results = await coordinator.run_due(10)

    assert schedule.slices == ((10, Decimal("1.00")),)
    assert [
        (order.venue, order.order.quantity)
        for order in results[0].orders
    ] == [
        ("binance", Decimal("0.5")),
        ("okx", Decimal("0.50")),
    ]


@pytest.mark.asyncio
async def test_unexecutable_lot_residual_is_rejected_without_leaking_risk() -> None:
    adapter = FakeAdapter(
        "binance",
        quantity_step=Decimal("0.1"),
        minimum_quantity=Decimal("0.1"),
    )
    coordinator = ExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100", ask_size="2")
    )

    with pytest.raises(UnexecutableResidualError, match="residual"):
        await coordinator.schedule_twap(
            "lot-residual",
            leg("BTCUSDT", Side.BUY, "1.05", "100"),
            start_ns=10,
            end_ns=20,
            slice_count=2,
        )

    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_min_notional_slices_are_coalesced_or_rejected() -> None:
    adapter = FakeAdapter(
        "binance",
        quantity_step=Decimal("0.1"),
        minimum_quantity=Decimal("0.1"),
        minimum_notional=Decimal("2"),
    )
    coordinator = ExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="9", ask="10")
    )
    limit_leg = ExecutionLeg(
        symbol="BTCUSDT",
        side=Side.BUY,
        quantity=Decimal("0.3"),
        reference_price=Decimal("10"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("10"),
    )

    schedule = await coordinator.schedule_twap(
        "min-notional",
        limit_leg,
        start_ns=10,
        end_ns=30,
        slice_count=3,
    )

    assert [quantity for _, quantity in schedule.slices] == [
        Decimal("0"),
        Decimal("0"),
        Decimal("0.3"),
    ]

    rejecting = FakeAdapter(
        "okx",
        quantity_step=Decimal("0.1"),
        minimum_quantity=Decimal("0.1"),
        minimum_notional=Decimal("4"),
    )
    rejected_coordinator = ExecutionCoordinator({"okx": rejecting})
    await rejected_coordinator.update_quote(
        quote("okx", "BTCUSDT", bid="9", ask="10")
    )
    with pytest.raises(UnexecutableResidualError):
        await rejected_coordinator.schedule_twap(
            "min-notional-rejected",
            limit_leg,
            start_ns=10,
            end_ns=30,
            slice_count=3,
        )
    assert rejected_coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_incompatible_deribit_product_is_rejected_from_spot_sor() -> None:
    binance = FakeAdapter("binance")
    deribit = FakeAdapter("deribit")
    coordinator = ExecutionCoordinator(
        {"binance": binance, "deribit": deribit}
    )
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )

    with pytest.raises(ValueError, match="not compatible"):
        await coordinator.update_quote(
            quote("deribit", "BTCUSDT", bid="99", ask="100")
        )

    route = await coordinator.preview_route(
        "BTCUSDT",
        Side.BUY,
        Decimal("1"),
    )
    assert [item["venue"] for item in route] == ["binance"]


@pytest.mark.asyncio
async def test_okx_spot_symbol_mapping_is_used_for_rules_and_orders() -> None:
    okx = FakeAdapter("okx")
    coordinator = ExecutionCoordinator({"okx": okx})
    await coordinator.update_quote(
        quote("okx", "BTCUSDT", bid="99", ask="100")
    )

    await coordinator.schedule_twap(
        "okx-symbol",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )
    await coordinator.run_due(10)

    assert okx.instrument_symbols == ["BTC-USDT"]
    assert [order.symbol for order in okx.submitted] == ["BTC-USDT"]


@pytest.mark.asyncio
async def test_deribit_usd_amount_conversion_preserves_canonical_quantity() -> None:
    deribit = FakeAdapter(
        "deribit",
        quantity_step=Decimal("10"),
        minimum_quantity=Decimal("10"),
        quantity_unit=QuantityUnit.QUOTE,
    )
    coordinator = ExecutionCoordinator(
        {"deribit": deribit},
        max_reserved_notional=Decimal("1000"),
    )
    await coordinator.update_quote(
        quote(
            "deribit",
            "BTC-PERPETUAL",
            bid="100000",
            ask="100000",
            bid_size="100",
            ask_size="100",
        )
    )

    schedule = await coordinator.schedule_twap(
        "deribit-usd-amount",
        leg("BTC-PERPETUAL", Side.BUY, "0.001", "100000"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )
    assert coordinator.reserved_notional == Decimal("100")
    results = await coordinator.run_due(10)

    assert deribit.instrument_symbols == ["BTC-PERPETUAL"]
    assert deribit.submitted[0].quantity == Decimal("100.000")
    assert results[0].orders[0].canonical_quantity == Decimal("0.001")
    assert results[0].orders[0].filled_quantity == Decimal("0.001")
    assert schedule.normalized_quantity == Decimal("0.001")
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_deribit_fill_uses_average_price_and_retains_slippage_risk() -> None:
    deribit = FakeAdapter(
        "deribit",
        submissions=("filled",),
        queries=("filled",),
        quantity_step=Decimal("10"),
        minimum_quantity=Decimal("10"),
        quantity_unit=QuantityUnit.QUOTE,
        average_fill_price=Decimal("50000"),
    )
    coordinator = ExecutionCoordinator(
        {"deribit": deribit},
        max_reserved_notional=Decimal("1000"),
    )
    await coordinator.update_quote(
        quote(
            "deribit",
            "BTC-PERPETUAL",
            bid="100000",
            ask="100000",
            bid_size="100",
            ask_size="100",
        )
    )
    await coordinator.schedule_twap(
        "deribit-slippage",
        leg("BTC-PERPETUAL", Side.BUY, "0.001", "100000"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )

    slices = await coordinator.run_due(10)
    schedule = coordinator.schedule_result("deribit-slippage")

    assert deribit.submitted[0].quantity == Decimal("100.000")
    assert slices[0].orders[0].filled_quantity == Decimal("0.002")
    assert slices[0].outcome is ExecutionOutcome.UNWIND_REQUIRED
    assert schedule.cumulative_filled_quantity == Decimal("0.002")
    assert schedule.residual_canonical_exposure == Decimal("0.002")
    assert schedule.state is ScheduleState.UNWIND_REQUIRED
    assert not schedule.risk_released
    assert coordinator.reserved_notional == Decimal("100")


@pytest.mark.asyncio
async def test_deribit_missing_fill_price_requires_schedule_reconciliation() -> None:
    deribit = FakeAdapter(
        "deribit",
        submissions=("filled",),
        queries=("filled", "filled"),
        cancellations=("late_fill",),
        quantity_step=Decimal("10"),
        minimum_quantity=Decimal("10"),
        quantity_unit=QuantityUnit.QUOTE,
        average_fill_price=None,
    )
    coordinator = ExecutionCoordinator(
        {"deribit": deribit},
        max_reserved_notional=Decimal("1000"),
    )
    await coordinator.update_quote(
        quote(
            "deribit",
            "BTC-PERPETUAL",
            bid="100000",
            ask="100000",
            bid_size="100",
            ask_size="100",
        )
    )
    await coordinator.schedule_twap(
        "deribit-missing-price",
        leg("BTC-PERPETUAL", Side.BUY, "0.001", "100000"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )

    slices = await coordinator.run_due(10)
    schedule = coordinator.schedule_result("deribit-missing-price")
    routed = schedule.orders[0]

    assert slices[0].outcome is ExecutionOutcome.RECONCILIATION_REQUIRED
    assert routed.status is RouteOrderStatus.AMBIGUOUS
    assert routed.reconciliation_error is not None
    assert "average_fill_price" in routed.reconciliation_error
    assert schedule.state is ScheduleState.RECOVERY_REQUIRED
    assert not schedule.risk_released
    assert coordinator.reserved_notional == Decimal("100")


def test_default_deribit_mapping_uses_usd_amount_not_contract_count() -> None:
    mapping = EMSConfig().instruments["BTC-PERPETUAL"].venues["deribit"]

    assert mapping.quantity_unit == "quote"
    assert mapping.contract_size_quote is None


@pytest.mark.asyncio
async def test_deribit_partial_usd_fill_converts_back_to_canonical_base() -> None:
    deribit = FakeAdapter(
        "deribit",
        submissions=("partially_filled",),
        queries=(
            "partially_filled",
            "canceled_partial",
            "canceled_partial",
        ),
        cancellations=("canceled_partial",),
        quantity_step=Decimal("10"),
        minimum_quantity=Decimal("10"),
        quantity_unit=QuantityUnit.QUOTE,
        average_fill_price=Decimal("50000"),
    )
    coordinator = ExecutionCoordinator(
        {"deribit": deribit},
        max_reserved_notional=Decimal("1000"),
    )
    await coordinator.update_quote(
        quote(
            "deribit",
            "BTC-PERPETUAL",
            bid="100000",
            ask="100000",
            bid_size="100",
            ask_size="100",
        )
    )

    await coordinator.schedule_twap(
        "deribit-partial-usd",
        leg("BTC-PERPETUAL", Side.BUY, "0.001", "100000"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )
    results = await coordinator.run_due(10)
    routed = results[0].orders[0]
    schedule = await coordinator.cancel_schedule("deribit-partial-usd")

    assert routed.order.quantity == Decimal("100.000")
    assert routed.exchange_order is not None
    assert routed.exchange_order.filled_quantity == Decimal("50.000")
    assert routed.filled_quantity == Decimal("0.001")
    assert routed.status is RouteOrderStatus.CANCELED
    assert schedule.state is ScheduleState.UNWIND_REQUIRED
    assert schedule.residual_canonical_exposure == Decimal("0.001")
    assert not schedule.risk_released
    assert coordinator.reserved_notional == Decimal("100")

    recovery = verified_recovery_order(
        deribit,
        "deribit-partial-unwind",
        "BTC-PERPETUAL",
        Side.SELL,
        "0.001",
        venue_quantity="50",
        average_fill_price="50000",
    )
    recovered = await coordinator.complete_schedule_recovery(
        "deribit-partial-usd",
        cancel_open_orders=False,
        unwind_completed=True,
        recovery_orders=(recovery,),
    )

    assert recovered.state is ScheduleState.RECOVERED
    assert recovered.residual_canonical_exposure == 0
    assert recovered.risk_released
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_deribit_slippage_requires_plan_unwind_before_release() -> None:
    deribit = FakeAdapter(
        "deribit",
        submissions=("filled",),
        quantity_step=Decimal("10"),
        minimum_quantity=Decimal("10"),
        quantity_unit=QuantityUnit.QUOTE,
        average_fill_price=Decimal("50000"),
    )
    binance = FakeAdapter("binance", submissions=("filled",))
    coordinator = ExecutionCoordinator(
        {"deribit": deribit, "binance": binance},
        dispatch_policy=DispatchPolicy.SIMULTANEOUS,
        max_reserved_notional=Decimal("1000"),
    )
    await coordinator.update_quote(
        quote(
            "deribit",
            "BTC-PERPETUAL",
            bid="100000",
            ask="100000",
            bid_size="100",
            ask_size="100",
        )
    )
    await coordinator.update_quote(
        quote("binance", "ETHUSDT", bid="50", ask="51")
    )

    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "deribit-plan-slippage",
            (
                leg("BTC-PERPETUAL", Side.BUY, "0.001", "100000"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )

    assert result.orders[0].filled_quantity == Decimal("0.002")
    assert result.outcome is ExecutionOutcome.UNWIND_REQUIRED
    assert result.native_state == "UNWIND_REQUIRED"
    assert not result.risk_released
    assert coordinator.reserved_notional == Decimal("150")


@pytest.mark.asyncio
async def test_confirmed_plan_unwind_releases_once_and_restores_capacity() -> None:
    deribit = FakeAdapter(
        "deribit",
        submissions=("filled", "filled"),
        quantity_step=Decimal("10"),
        minimum_quantity=Decimal("10"),
        quantity_unit=QuantityUnit.QUOTE,
        average_fill_price=Decimal("50000"),
    )
    binance = FakeAdapter("binance", submissions=("filled", "filled"))
    coordinator = CountingExecutionCoordinator(
        {"deribit": deribit, "binance": binance},
        dispatch_policy=DispatchPolicy.SIMULTANEOUS,
        max_reserved_notional=Decimal("150"),
    )
    await coordinator.update_quote(
        quote(
            "deribit",
            "BTC-PERPETUAL",
            bid="100000",
            ask="100000",
            bid_size="100",
            ask_size="100",
        )
    )
    await coordinator.update_quote(
        quote("binance", "ETHUSDT", bid="50", ask="51")
    )
    request_legs = (
        leg("BTC-PERPETUAL", Side.BUY, "0.001", "100000"),
        leg("ETHUSDT", Side.SELL, "1", "50"),
    )
    initial = await coordinator.execute_plan(
        ExecutionPlanRequest("recover-slippage", request_legs)
    )
    blocked = await coordinator.execute_plan(
        ExecutionPlanRequest("blocked-by-reservation", request_legs)
    )

    assert initial.outcome is ExecutionOutcome.UNWIND_REQUIRED
    assert blocked.outcome is ExecutionOutcome.FAILED
    assert coordinator.reserved_notional == Decimal("150")

    recovery_orders = (
        verified_recovery_order(
            deribit,
            "unwind-btc",
            "BTC-PERPETUAL",
            Side.SELL,
            "0.002",
            venue_quantity="100",
            average_fill_price="50000",
        ),
        verified_recovery_order(
            binance,
            "unwind-eth",
            "ETHUSDT",
            Side.BUY,
            "1",
        ),
    )
    recovered = await coordinator.complete_plan_recovery(
        "recover-slippage",
        recovery_orders=recovery_orders,
    )
    repeated = await coordinator.complete_plan_recovery(
        "recover-slippage",
        recovery_orders=recovery_orders,
    )

    assert repeated is recovered
    assert recovered.outcome is ExecutionOutcome.RECOVERED
    assert recovered.native_state == "CLOSED"
    assert recovered.state_history[-4:] == [
        "UNWINDING",
        "RECONCILING",
        "RECOVERED",
        "CLOSED",
    ]
    assert recovered.residual_canonical_exposure == 0
    assert recovered.recovery_completed
    assert recovered.risk_released
    assert coordinator.plan_release_count == 1
    assert coordinator.reserved_notional == 0

    capacity_restored = await coordinator.execute_plan(
        ExecutionPlanRequest("capacity-restored", request_legs)
    )
    assert capacity_restored.outcome is ExecutionOutcome.UNWIND_REQUIRED
    assert coordinator.reserved_notional == Decimal("150")


@pytest.mark.asyncio
async def test_plan_recovery_refuses_nonzero_residual_exposure() -> None:
    binance = FakeAdapter("binance", submissions=("filled",))
    okx = FakeAdapter("okx", submissions=("error",))
    coordinator = CountingExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=DispatchPolicy.SIMULTANEOUS,
    )
    await add_two_symbol_quotes(coordinator)
    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "nonzero-recovery",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )

    with pytest.raises(RuntimeError, match="nonzero residual"):
        await coordinator.complete_plan_recovery(
            "nonzero-recovery",
            recovery_orders=(
                verified_recovery_order(
                    binance,
                    "wrong-size-unwind",
                    "BTCUSDT",
                    Side.SELL,
                    "0.5",
                ),
            ),
        )

    assert result.residual_canonical_exposure == Decimal("0.5")
    assert result.recovery_actions[-1] == (
        "recovery_order_confirmed:wrong-size-unwind"
    )
    assert not result.risk_released
    assert coordinator.plan_release_count == 0
    assert coordinator.reserved_notional == Decimal("150")


@pytest.mark.asyncio
@pytest.mark.parametrize("use_exchange_id", [True, False])
async def test_plan_recovery_rejects_duplicate_underlying_fill(
    use_exchange_id: bool,
) -> None:
    binance = FakeAdapter("binance", submissions=("filled",))
    okx = FakeAdapter("okx", submissions=("error",))
    coordinator = CountingExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=DispatchPolicy.SIMULTANEOUS,
    )
    await add_two_symbol_quotes(coordinator)
    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            f"duplicate-fill-{use_exchange_id}",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )
    exchange_order_id = "underlying-fill-1" if use_exchange_id else None
    client_order_id = None if use_exchange_id else "recovery-client-1"
    binance.add_external_fill(
        symbol="BTCUSDT",
        side=Side.SELL,
        quantity=Decimal("0.5"),
        exchange_order_id=exchange_order_id or "underlying-exchange-1",
        client_order_id=client_order_id or "underlying-client-1",
    )

    with pytest.raises(RuntimeError, match="duplicate.*fill evidence"):
        await coordinator.complete_plan_recovery(
            f"duplicate-fill-{use_exchange_id}",
            recovery_orders=(
                PlanRecoveryOrderEvidence(
                    recovery_id="caller-id-1",
                    canonical_symbol="BTCUSDT",
                    side=Side.SELL,
                    filled_quantity=Decimal("0.5"),
                    venue="binance",
                    exchange_order_id=exchange_order_id,
                    client_order_id=client_order_id,
                    reconciliation_evidence="query confirmed FILLED",
                ),
                PlanRecoveryOrderEvidence(
                    recovery_id="caller-id-2",
                    canonical_symbol="BTCUSDT",
                    side=Side.SELL,
                    filled_quantity=Decimal("0.5"),
                    venue="binance",
                    exchange_order_id=exchange_order_id,
                    client_order_id=client_order_id,
                    reconciliation_evidence="query confirmed FILLED",
                ),
            ),
        )

    assert not result.recovery_orders
    assert result.residual_canonical_exposure == Decimal("1")
    assert not result.risk_released
    assert coordinator.plan_release_count == 0
    assert coordinator.reserved_notional == Decimal("150")


@pytest.mark.asyncio
async def test_plan_recovery_rejects_conflicting_underlying_fill() -> None:
    binance = FakeAdapter("binance", submissions=("filled",))
    okx = FakeAdapter("okx", submissions=("error",))
    coordinator = CountingExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=DispatchPolicy.SIMULTANEOUS,
    )
    await add_two_symbol_quotes(coordinator)
    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "conflicting-fill",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )
    binance.add_external_fill(
        symbol="BTCUSDT",
        side=Side.SELL,
        quantity=Decimal("0.5"),
        exchange_order_id="underlying-fill-2",
        client_order_id="underlying-client-2",
    )

    with pytest.raises(RuntimeError, match="conflicting.*fill evidence"):
        await coordinator.complete_plan_recovery(
            "conflicting-fill",
            recovery_orders=(
                recovery_order(
                    "caller-id-1",
                    "BTCUSDT",
                    Side.SELL,
                    "0.5",
                    exchange_order_id="underlying-fill-2",
                ),
                recovery_order(
                    "caller-id-2",
                    "BTCUSDT",
                    Side.SELL,
                    "1",
                    exchange_order_id="underlying-fill-2",
                ),
            ),
        )

    assert not result.recovery_orders
    assert result.residual_canonical_exposure == Decimal("1")
    assert not result.risk_released
    assert coordinator.plan_release_count == 0
    assert coordinator.reserved_notional == Decimal("150")


@pytest.mark.asyncio
async def test_plan_recovery_rejects_opposite_side_replay_of_original_fill() -> None:
    binance = FakeAdapter("binance", submissions=("filled",))
    okx = FakeAdapter("okx", submissions=("error",))
    coordinator = CountingExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=DispatchPolicy.SIMULTANEOUS,
    )
    await add_two_symbol_quotes(coordinator)
    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "original-fill-replay",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )
    original = result.orders[0].exchange_order
    assert original is not None

    with pytest.raises(RuntimeError, match="already owned by plan:"):
        await coordinator.complete_plan_recovery(
            "original-fill-replay",
            recovery_orders=(
                PlanRecoveryOrderEvidence(
                    recovery_id="fake-opposite-side-hedge",
                    canonical_symbol="BTCUSDT",
                    side=Side.SELL,
                    filled_quantity=Decimal("1"),
                    venue="binance",
                    exchange_order_id=original.exchange_order_id,
                    client_order_id=original.client_order_id,
                    reconciliation_evidence="replayed original fill",
                ),
            ),
        )

    assert not result.recovery_orders
    assert result.residual_canonical_exposure == Decimal("1")
    assert not result.risk_released
    assert coordinator.plan_release_count == 0
    assert coordinator.reserved_notional == Decimal("150")


@pytest.mark.asyncio
async def test_recovery_identity_cannot_be_reused_across_plans() -> None:
    binance = FakeAdapter("binance", submissions=("filled", "filled"))
    okx = FakeAdapter("okx", submissions=("error", "error"))
    coordinator = CountingExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=DispatchPolicy.SIMULTANEOUS,
    )
    await add_two_symbol_quotes(coordinator)
    for plan_id in ("recovery-owner-one", "recovery-owner-two"):
        result = await coordinator.execute_plan(
            ExecutionPlanRequest(
                plan_id,
                (
                    leg("BTCUSDT", Side.BUY, "1", "100"),
                    leg("ETHUSDT", Side.SELL, "1", "50"),
                ),
            )
        )
        assert result.outcome is ExecutionOutcome.UNWIND_REQUIRED

    first_evidence = verified_recovery_order(
        binance,
        "owner-one-hedge",
        "BTCUSDT",
        Side.SELL,
        "1",
        exchange_order_id="shared-recovery-exchange",
        client_order_id="shared-recovery-client",
    )
    first = await coordinator.complete_plan_recovery(
        "recovery-owner-one",
        recovery_orders=(first_evidence,),
    )

    with pytest.raises(RuntimeError, match="already owned by recovery:plan"):
        await coordinator.complete_plan_recovery(
            "recovery-owner-two",
            recovery_orders=(
                PlanRecoveryOrderEvidence(
                    recovery_id="owner-two-hedge",
                    canonical_symbol="BTCUSDT",
                    side=Side.SELL,
                    filled_quantity=Decimal("1"),
                    venue="binance",
                    exchange_order_id="shared-recovery-exchange",
                    client_order_id="shared-recovery-client",
                    reconciliation_evidence="query_order confirmed FILLED",
                ),
            ),
        )

    second = coordinator.plan("recovery-owner-two")
    assert first.risk_released
    assert not second.recovery_orders
    assert second.residual_canonical_exposure == Decimal("1")
    assert not second.risk_released
    assert coordinator.plan_release_count == 1
    assert coordinator.reserved_notional == Decimal("150")


@pytest.mark.asyncio
async def test_client_fallback_distinct_hedge_is_verified_and_idempotent() -> None:
    binance = FakeAdapter("binance", submissions=("filled",))
    okx = FakeAdapter("okx", submissions=("error",))
    coordinator = CountingExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=DispatchPolicy.SIMULTANEOUS,
    )
    await add_two_symbol_quotes(coordinator)
    await coordinator.execute_plan(
        ExecutionPlanRequest(
            "client-fallback-hedge",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )
    evidence = verified_recovery_order(
        binance,
        "client-fallback-first",
        "BTCUSDT",
        Side.SELL,
        "0.5",
        exchange_order_id="client-fallback-first-exchange",
        client_order_id="client-fallback-first-client",
        use_client_fallback=True,
    )

    with pytest.raises(RuntimeError, match="nonzero residual"):
        await coordinator.complete_plan_recovery(
            "client-fallback-hedge",
            recovery_orders=(evidence,),
        )

    retained = coordinator.plan("client-fallback-hedge")
    assert retained.residual_canonical_exposure == Decimal("0.5")
    assert not retained.risk_released
    assert coordinator.plan_release_count == 0

    second = verified_recovery_order(
        binance,
        "client-fallback-second",
        "BTCUSDT",
        Side.SELL,
        "0.5",
    )
    recovered = await coordinator.complete_plan_recovery(
        "client-fallback-hedge",
        recovery_orders=(evidence, second),
    )
    repeated = await coordinator.complete_plan_recovery(
        "client-fallback-hedge",
        recovery_orders=(evidence, second),
    )

    assert repeated is recovered
    assert recovered.recovery_orders[0].exchange_order_id == (
        "client-fallback-first-exchange"
    )
    assert recovered.recovery_orders[0].client_order_id == (
        "client-fallback-first-client"
    )
    assert binance.queried.count("client-fallback-first-client") == 1
    assert recovered.residual_canonical_exposure == 0
    assert recovered.risk_released
    assert coordinator.plan_release_count == 1
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_schedule_recovery_requires_distinct_verified_hedge() -> None:
    adapter = FakeAdapter(
        "binance",
        submissions=("partially_filled",),
        queries=(
            "open",
            "open",
            "canceled_partial",
            "canceled_partial",
            "canceled_partial",
        ),
        cancellations=("canceled_partial",),
    )
    coordinator = CountingExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.schedule_twap(
        "schedule-distinct-hedge",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )
    await coordinator.run_due(10)
    result = await coordinator.cancel_schedule("schedule-distinct-hedge")
    original = result.orders[0].exchange_order
    assert original is not None
    replay = PlanRecoveryOrderEvidence(
        recovery_id="schedule-original-replay",
        canonical_symbol="BTCUSDT",
        side=Side.SELL,
        filled_quantity=Decimal("0.5"),
        venue="binance",
        exchange_order_id=original.exchange_order_id,
        client_order_id=original.client_order_id,
        reconciliation_evidence="opposite-side replay",
    )

    with pytest.raises(RuntimeError, match="already owned by schedule:"):
        await coordinator.complete_schedule_recovery(
            "schedule-distinct-hedge",
            cancel_open_orders=False,
            unwind_completed=True,
            recovery_orders=(replay,),
        )

    assert result.residual_canonical_exposure == Decimal("0.5")
    assert not result.recovery_orders
    assert not result.risk_released
    assert coordinator.schedule_release_count == 0
    assert coordinator.reserved_notional == Decimal("100")

    hedge = verified_recovery_order(
        adapter,
        "schedule-distinct",
        "BTCUSDT",
        Side.SELL,
        "0.5",
    )
    recovered = await coordinator.complete_schedule_recovery(
        "schedule-distinct-hedge",
        cancel_open_orders=False,
        unwind_completed=True,
        recovery_orders=(hedge,),
    )
    repeated = await coordinator.complete_schedule_recovery(
        "schedule-distinct-hedge",
        cancel_open_orders=False,
        unwind_completed=True,
        recovery_orders=(hedge,),
    )

    assert repeated is recovered
    assert recovered.state is ScheduleState.RECOVERED
    assert recovered.residual_canonical_exposure == 0
    assert recovered.risk_released
    assert coordinator.schedule_release_count == 1
    assert coordinator.reserved_notional == 0

    adapter.submission_actions.extend(("filled", "error"))
    plan = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "plan-after-schedule-recovery",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("BTCUSDT", Side.SELL, "1", "100"),
            ),
            dispatch_policy=DispatchPolicy.SIMULTANEOUS,
        )
    )
    assert plan.outcome is ExecutionOutcome.UNWIND_REQUIRED

    with pytest.raises(
        RuntimeError,
        match="already owned by recovery:schedule",
    ):
        await coordinator.complete_plan_recovery(
            "plan-after-schedule-recovery",
            recovery_orders=(
                PlanRecoveryOrderEvidence(
                    recovery_id="plan-reuses-schedule-hedge",
                    canonical_symbol="BTCUSDT",
                    side=Side.SELL,
                    filled_quantity=Decimal("0.5"),
                    venue="binance",
                    exchange_order_id="exchange-schedule-distinct",
                    client_order_id="client-schedule-distinct",
                    reconciliation_evidence="query_order confirmed FILLED",
                ),
            ),
        )

    assert not plan.risk_released
    assert coordinator.plan_release_count == 0
    assert coordinator.reserved_notional == Decimal("200")


@pytest.mark.asyncio
async def test_operator_plan_resolution_records_external_evidence() -> None:
    binance = FakeAdapter("binance", submissions=("filled",))
    okx = FakeAdapter("okx", submissions=("error",))
    coordinator = CountingExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=DispatchPolicy.SIMULTANEOUS,
    )
    await add_two_symbol_quotes(coordinator)
    await coordinator.execute_plan(
        ExecutionPlanRequest(
            "operator-plan-recovery",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )

    resolved = await coordinator.resolve_plan_recovery(
        "operator-plan-recovery",
        reason="manual hedge completed",
        external_evidence="ticket OPS-42 and venue fill 987",
    )
    repeated = await coordinator.resolve_plan_recovery(
        "operator-plan-recovery",
        reason="manual hedge completed",
        external_evidence="ticket OPS-42 and venue fill 987",
    )

    assert repeated is resolved
    assert resolved.outcome is ExecutionOutcome.OPERATOR_RESOLVED
    assert resolved.native_state == "CLOSED"
    assert resolved.operator_resolved
    assert resolved.operator_resolution_reason == "manual hedge completed"
    assert (
        resolved.operator_resolution_evidence
        == "ticket OPS-42 and venue fill 987"
    )
    assert resolved.residual_canonical_exposure == 0
    assert resolved.recovery_actions[-1] == (
        "operator_resolved:manual hedge completed"
    )
    assert resolved.risk_released
    assert coordinator.plan_release_count == 1
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_plan_recovery_refuses_live_or_ambiguous_sibling() -> None:
    binance = FakeAdapter(
        "binance",
        submissions=("accepted",),
        queries=("open", "open"),
        cancellations=("cancel_requested",),
    )
    okx = FakeAdapter(
        "okx",
        submissions=("accepted",),
        queries=("open", "open"),
        cancellations=("cancel_requested",),
    )
    coordinator = CountingExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=DispatchPolicy.SIMULTANEOUS,
    )
    await add_two_symbol_quotes(coordinator)
    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "live-sibling-recovery",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )

    with pytest.raises(RuntimeError, match="live, ambiguous, or unreconciled"):
        await coordinator.resolve_plan_recovery(
            "live-sibling-recovery",
            reason="external review",
            external_evidence="ticket OPS-43",
        )

    assert result.outcome is ExecutionOutcome.RECONCILIATION_REQUIRED
    assert any(
        order.status
        in {
            RouteOrderStatus.ACCEPTED,
            RouteOrderStatus.AMBIGUOUS,
        }
        for order in result.orders
    )
    assert not result.risk_released
    assert coordinator.plan_release_count == 0
    assert coordinator.reserved_notional == Decimal("150")


@pytest.mark.asyncio
async def test_deribit_missing_fill_price_blocks_plan_release() -> None:
    deribit = FakeAdapter(
        "deribit",
        submissions=("filled",),
        queries=("filled", "filled"),
        cancellations=("late_fill",),
        quantity_step=Decimal("10"),
        minimum_quantity=Decimal("10"),
        quantity_unit=QuantityUnit.QUOTE,
        average_fill_price=None,
    )
    binance = FakeAdapter("binance", submissions=("filled",))
    coordinator = ExecutionCoordinator(
        {"deribit": deribit, "binance": binance},
        dispatch_policy=DispatchPolicy.SIMULTANEOUS,
        max_reserved_notional=Decimal("1000"),
    )
    await coordinator.update_quote(
        quote(
            "deribit",
            "BTC-PERPETUAL",
            bid="100000",
            ask="100000",
            bid_size="100",
            ask_size="100",
        )
    )
    await coordinator.update_quote(
        quote("binance", "ETHUSDT", bid="50", ask="51")
    )

    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "deribit-plan-missing-price",
            (
                leg("BTC-PERPETUAL", Side.BUY, "0.001", "100000"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )

    assert result.orders[0].status is RouteOrderStatus.AMBIGUOUS
    assert result.orders[0].reconciliation_error is not None
    assert result.outcome is ExecutionOutcome.RECONCILIATION_REQUIRED
    assert not result.risk_released
    assert coordinator.reserved_notional == Decimal("150")


@pytest.mark.asyncio
async def test_sibling_orders_cancel_before_partial_plan_unwind() -> None:
    binance = FakeAdapter(
        "binance",
        submissions=("filled",),
        quantity_step=Decimal("0.1"),
        minimum_quantity=Decimal("0.1"),
    )
    okx = FakeAdapter(
        "okx",
        submissions=("open",),
        cancellations=("canceled",),
        queries=("canceled",),
        quantity_step=Decimal("0.1"),
        minimum_quantity=Decimal("0.1"),
    )
    deribit = FakeAdapter(
        "deribit",
        submissions=("error",),
        quantity_step=Decimal("10"),
        minimum_quantity=Decimal("10"),
        quantity_unit=QuantityUnit.QUOTE,
    )
    coordinator = ExecutionCoordinator(
        {"binance": binance, "okx": okx, "deribit": deribit},
        dispatch_policy=DispatchPolicy.SIMULTANEOUS,
    )
    await coordinator.update_quote(
        quote(
            "binance",
            "BTCUSDT",
            bid="99",
            ask="100",
            ask_size="1",
        )
    )
    await coordinator.update_quote(
        quote(
            "okx",
            "BTCUSDT",
            bid="98",
            ask="101",
            ask_size="1",
        )
    )
    await coordinator.update_quote(
        quote(
            "deribit",
            "BTC-PERPETUAL",
            bid="100000",
            ask="100000",
            bid_size="100",
            ask_size="100",
        )
    )

    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "cancel-before-unwind",
            (
                leg("BTCUSDT", Side.BUY, "1.5", "100"),
                leg("BTC-PERPETUAL", Side.SELL, "0.001", "100000"),
            ),
        )
    )

    assert result.outcome is ExecutionOutcome.UNWIND_REQUIRED
    assert okx.events[-2:] == ["cancel:BTC-USDT", "query:BTC-USDT"]
    assert [order.status for order in result.orders] == [
        RouteOrderStatus.FILLED,
        RouteOrderStatus.CANCELED,
        RouteOrderStatus.REJECTED,
    ]
    assert result.state_history.index("HEDGE_REQUIRED") > 0


@pytest.mark.asyncio
async def test_reconcile_plan_cancels_partial_and_open_siblings_first() -> None:
    binance = FakeAdapter(
        "binance",
        submissions=("accepted",),
        queries=("partially_filled", "canceled_partial"),
        cancellations=("cancel_requested",),
    )
    okx = FakeAdapter(
        "okx",
        submissions=("accepted",),
        queries=("open", "canceled"),
        cancellations=("cancel_requested",),
    )
    coordinator = ExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=DispatchPolicy.SIMULTANEOUS,
    )
    await add_two_symbol_quotes(coordinator)
    initial = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "reconcile-live-siblings",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )

    assert initial.outcome is ExecutionOutcome.OPEN
    result = await coordinator.reconcile_plan("reconcile-live-siblings")

    assert result.outcome is ExecutionOutcome.UNWIND_REQUIRED
    assert [order.status for order in result.orders] == [
        RouteOrderStatus.CANCELED,
        RouteOrderStatus.CANCELED,
    ]
    assert result.orders[0].filled_quantity == Decimal("0.5")
    assert binance.events[-3:] == [
        "query:BTCUSDT",
        "cancel:BTCUSDT",
        "query:BTCUSDT",
    ]
    assert okx.events[-3:] == [
        "query:ETH-USDT",
        "cancel:ETH-USDT",
        "query:ETH-USDT",
    ]
    assert "HEDGE_REQUIRED" in result.state_history
    assert not result.risk_released


@pytest.mark.asyncio
async def test_late_fill_during_cancel_is_exposure_not_canceled_success() -> None:
    binance = FakeAdapter(
        "binance",
        submissions=("open",),
        cancellations=("accepted",),
        queries=("filled",),
    )
    okx = FakeAdapter("okx", submissions=("error",))
    coordinator = ExecutionCoordinator({"binance": binance, "okx": okx})
    await add_two_symbol_quotes(coordinator)

    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "late-fill",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )

    assert result.orders[0].status is RouteOrderStatus.FILLED
    assert result.orders[0].filled_quantity == Decimal("1")
    assert result.outcome is ExecutionOutcome.UNWIND_REQUIRED
    assert "HEDGE_REQUIRED" in result.state_history
    assert binance.events[-2:] == [
        "cancel:BTCUSDT",
        "query:BTCUSDT",
    ]


@pytest.mark.asyncio
async def test_ambiguous_sibling_cancel_stays_recovery_required() -> None:
    binance = FakeAdapter(
        "binance",
        submissions=("open",),
        cancellations=("ambiguous",),
        queries=("error",),
    )
    okx = FakeAdapter("okx", submissions=("error",))
    coordinator = ExecutionCoordinator({"binance": binance, "okx": okx})
    await add_two_symbol_quotes(coordinator)

    result = await coordinator.execute_plan(
        ExecutionPlanRequest(
            "ambiguous-cancel",
            (
                leg("BTCUSDT", Side.BUY, "1", "100"),
                leg("ETHUSDT", Side.SELL, "1", "50"),
            ),
        )
    )

    assert result.orders[0].status is RouteOrderStatus.AMBIGUOUS
    assert result.outcome is ExecutionOutcome.RECONCILIATION_REQUIRED
    assert result.native_state == "RECOVERY_REQUIRED"
    assert "HEDGE_REQUIRED" not in result.state_history
    assert not result.risk_released


@pytest.mark.asyncio
async def test_cancel_schedule_waits_for_blocked_submit_then_cancels_ack() -> None:
    adapter = DelayedFakeAdapter(
        "binance",
        block_submit=True,
        submissions=("accepted",),
        queries=("open", "canceled"),
        cancellations=("cancel_requested",),
    )
    coordinator = CountingExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.schedule_twap(
        "cancel-blocked-submit",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )

    run_task = asyncio.create_task(coordinator.run_due(10))
    await adapter.submit_started.wait()
    cancel_task = asyncio.create_task(
        coordinator.cancel_schedule("cancel-blocked-submit")
    )
    await asyncio.sleep(0)

    assert not cancel_task.done()
    assert not adapter.canceled
    assert coordinator.reserved_notional == Decimal("100")

    adapter.allow_submit.set()
    slices, schedule = await asyncio.gather(run_task, cancel_task)

    assert slices[0].outcome is ExecutionOutcome.CANCELED
    assert schedule.state is ScheduleState.CANCELED
    assert schedule.orders[0].status is RouteOrderStatus.CANCELED
    assert len(adapter.canceled) == 1
    assert coordinator.schedule_release_count == 1
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_schedule_late_fill_during_blocked_cancel_stays_reserved() -> None:
    adapter = DelayedFakeAdapter(
        "binance",
        block_cancel=True,
        submissions=("accepted",),
        queries=("open", "filled"),
        cancellations=("cancel_requested",),
    )
    coordinator = CountingExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.schedule_twap(
        "late-fill-blocked-cancel",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )
    await coordinator.run_due(10)

    cancel_task = asyncio.create_task(
        coordinator.cancel_schedule("late-fill-blocked-cancel")
    )
    await adapter.cancel_started.wait()

    assert not cancel_task.done()
    assert coordinator.reserved_notional == Decimal("100")

    adapter.allow_cancel.set()
    schedule = await cancel_task

    assert schedule.state is ScheduleState.FILLED
    assert schedule.orders[0].status is RouteOrderStatus.FILLED
    assert schedule.cumulative_filled_quantity == Decimal("1")
    assert coordinator.schedule_release_count == 1
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_ambiguous_schedule_cancel_keeps_risk_reserved() -> None:
    adapter = FakeAdapter(
        "binance",
        submissions=("accepted",),
        queries=("open", "error"),
        cancellations=("ambiguous",),
    )
    coordinator = ExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.schedule_twap(
        "ambiguous-schedule-cancel",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )
    await coordinator.run_due(10)

    schedule = await coordinator.cancel_schedule(
        "ambiguous-schedule-cancel"
    )

    assert schedule.state is ScheduleState.RECOVERY_REQUIRED
    assert schedule.orders[0].status is RouteOrderStatus.AMBIGUOUS
    assert schedule.reconciliation_errors
    assert not schedule.risk_released
    assert coordinator.reserved_notional == Decimal("100")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query_action", "expected_outcome", "expected_state", "risk_released"),
    [
        (
            "open",
            ExecutionOutcome.OPEN,
            ScheduleState.OPEN,
            False,
        ),
        (
            "filled",
            ExecutionOutcome.FILLED,
            ScheduleState.FILLED,
            True,
        ),
    ],
)
async def test_timeout_schedule_submit_queries_client_id_without_resubmission(
    query_action: str,
    expected_outcome: ExecutionOutcome,
    expected_state: ScheduleState,
    risk_released: bool,
) -> None:
    adapter = FakeAdapter(
        "binance",
        submissions=("timeout",),
        queries=(query_action,),
    )
    coordinator = CountingExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.schedule_twap(
        f"timeout-schedule-{query_action}",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )

    slices = await coordinator.run_due(10)
    schedule = coordinator.schedule_result(
        f"timeout-schedule-{query_action}"
    )

    assert slices[0].outcome is expected_outcome
    assert schedule.state is expected_state
    assert schedule.risk_released is risk_released
    assert schedule.orders[0].reconciled is risk_released
    assert adapter.queried == [
        adapter.client_order_id(adapter.submitted[0])
    ]
    assert len(adapter.submitted) == 1
    assert coordinator.schedule_release_count == int(risk_released)
    assert coordinator.reserved_notional == (
        Decimal(0) if risk_released else Decimal("100")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [DispatchPolicy.SEQUENTIAL, DispatchPolicy.SIMULTANEOUS],
)
async def test_split_schedule_timeout_reconciles_in_all_dispatch_modes(
    policy: DispatchPolicy,
) -> None:
    binance = FakeAdapter(
        "binance",
        submissions=("timeout",),
        queries=("filled",),
    )
    okx = FakeAdapter("okx", submissions=("filled",))
    coordinator = CountingExecutionCoordinator(
        {"binance": binance, "okx": okx},
        dispatch_policy=policy,
    )
    await coordinator.update_quote(
        quote(
            "binance",
            "BTCUSDT",
            bid="99",
            ask="100",
            ask_size="0.5",
        )
    )
    await coordinator.update_quote(
        quote(
            "okx",
            "BTCUSDT",
            bid="98",
            ask="101",
            ask_size="0.5",
        )
    )
    await coordinator.schedule_twap(
        f"split-timeout-{policy.value}",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )

    slices = await coordinator.run_due(10)
    schedule = coordinator.schedule_result(
        f"split-timeout-{policy.value}"
    )

    assert slices[0].outcome is ExecutionOutcome.FILLED
    assert schedule.state is ScheduleState.FILLED
    assert all(order.reconciled for order in schedule.orders)
    assert len(binance.submitted) == len(okx.submitted) == 1
    assert binance.queried == [
        binance.client_order_id(binance.submitted[0])
    ]
    assert coordinator.schedule_release_count == 1
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("query_action", ["not_found", "timeout"])
async def test_uncertain_schedule_query_failure_retains_reservation(
    query_action: str,
) -> None:
    adapter = FakeAdapter(
        "binance",
        submissions=("timeout",),
        queries=(query_action, query_action, query_action),
        cancellations=("ambiguous",),
    )
    coordinator = CountingExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.schedule_twap(
        f"uncertain-schedule-{query_action}",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )

    slices = await coordinator.run_due(10)
    schedule = coordinator.schedule_result(
        f"uncertain-schedule-{query_action}"
    )

    assert slices[0].outcome is ExecutionOutcome.RECONCILIATION_REQUIRED
    assert schedule.state is ScheduleState.RECOVERY_REQUIRED
    assert schedule.orders[0].status is RouteOrderStatus.AMBIGUOUS
    assert not schedule.orders[0].reconciled
    assert len(adapter.submitted) == 1
    assert len(adapter.queried) == 3
    assert set(adapter.queried) == {
        adapter.client_order_id(adapter.submitted[0])
    }
    assert coordinator.schedule_release_count == 0
    assert coordinator.reserved_notional == Decimal("100")


@pytest.mark.asyncio
async def test_live_query_after_terminal_cancel_requires_retry() -> None:
    adapter = FakeAdapter(
        "binance",
        submissions=("accepted",),
        queries=("open", "open", "open", "canceled"),
        cancellations=("canceled", "canceled"),
    )
    coordinator = CountingExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.schedule_twap(
        "terminal-cancel-conflict",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )
    await coordinator.run_due(10)

    unresolved = await coordinator.cancel_schedule(
        "terminal-cancel-conflict"
    )

    assert unresolved.state is ScheduleState.RECOVERY_REQUIRED
    assert unresolved.orders[0].status is RouteOrderStatus.CANCELED
    assert not unresolved.orders[0].reconciled
    assert coordinator.reserved_notional == Decimal("100")

    resolved = await coordinator.cancel_schedule(
        "terminal-cancel-conflict"
    )

    assert resolved.state is ScheduleState.CANCELED
    assert resolved.orders[0].status is RouteOrderStatus.CANCELED
    assert len(adapter.canceled) == 2
    assert coordinator.schedule_release_count == 1
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_concurrent_cancel_and_reconcile_release_schedule_once() -> None:
    adapter = DelayedFakeAdapter(
        "binance",
        block_cancel=True,
        submissions=("accepted",),
        queries=("open", "canceled"),
        cancellations=("cancel_requested",),
    )
    coordinator = CountingExecutionCoordinator({"binance": adapter})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )
    await coordinator.schedule_twap(
        "concurrent-recovery",
        leg("BTCUSDT", Side.BUY, "1", "100"),
        start_ns=10,
        end_ns=10,
        slice_count=1,
    )
    await coordinator.run_due(10)

    first_cancel = asyncio.create_task(
        coordinator.cancel_schedule("concurrent-recovery")
    )
    await adapter.cancel_started.wait()
    reconcile = asyncio.create_task(
        coordinator.reconcile_schedule("concurrent-recovery")
    )
    second_cancel = asyncio.create_task(
        coordinator.cancel_schedule("concurrent-recovery")
    )
    adapter.allow_cancel.set()

    results = await asyncio.gather(first_cancel, reconcile, second_cancel)

    assert results[0] is results[1] is results[2]
    assert results[0].state is ScheduleState.CANCELED
    assert results[0].state_history.count(ScheduleState.CANCELED) == 1
    assert len(adapter.canceled) == 1
    assert coordinator.schedule_release_count == 1
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_market_schedule_passes_reference_price_for_min_notional() -> None:
    binance = FakeAdapter(
        "binance",
        quantity_step=Decimal("0.001"),
        minimum_quantity=Decimal("0.001"),
        minimum_notional=Decimal("2"),
    )
    coordinator = ExecutionCoordinator({"binance": binance})
    await coordinator.update_quote(
        quote("binance", "BTCUSDT", bid="99", ask="100")
    )

    with pytest.raises(UnexecutableResidualError, match="cannot execute"):
        await coordinator.schedule_twap(
            "market-min-notional",
            leg("BTCUSDT", Side.BUY, "0.01", "100"),
            start_ns=10,
            end_ns=20,
            slice_count=2,
        )

    assert binance.reference_prices == [Decimal("100.0")]
    assert not binance.submitted
    assert coordinator.reserved_notional == 0
