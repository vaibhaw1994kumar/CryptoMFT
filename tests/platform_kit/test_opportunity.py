"""Behavioural tests for the two-venue concurrent-leg state machine."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_platform_poc.exchange_adapter import (
    AccountSnapshot,
    AmbiguousOrderError,
    ExchangeAPIError,
    ExchangeOrder,
    ExchangeTrade,
    InstrumentRules,
    NormalizedOrder,
    QuantityUnit,
    ReconciliationSnapshot,
)
from trading_platform_poc.models import Order, OrderType, Side
from trading_platform_poc.opportunity import (
    LegRequest,
    LegState,
    MarketQuote,
    OpportunityCoordinator,
    OpportunityRequest,
    OpportunityRiskLimitError,
    OpportunityState,
    OpportunityTimeouts,
    RejectAction,
    RejectReason,
    ResidualPolicy,
    RetryDirective,
    UnwindPriceMode,
    action_for_reject,
    classify_reject,
    classify_retry,
)
from trading_platform_poc.opportunity_store import (
    DuplicateClientOrderIdError,
    LegRecord,
    OpportunityStore,
)


FAST = OpportunityTimeouts(
    ack_timeout_ms=50,
    first_fill_timeout_ms=200,
    complete_fill_timeout_ms=400,
    poll_interval_ms=1,
    max_submit_attempts=3,
    backoff_base_ms=1,
)


class ScriptedAdapter:
    """Deterministic in-memory venue used to drive every branch."""

    last_request = None

    def __init__(
        self,
        venue: str,
        *,
        submit_actions: tuple[str, ...] = (),
        fill_schedule: tuple[Decimal, ...] | None = None,
        fill_price: Decimal = Decimal("100"),
        query_actions: tuple[str, ...] = (),
        latent_order: bool = False,
    ) -> None:
        self.venue = venue
        self.submit_actions = deque(submit_actions)
        self.query_actions = deque(query_actions)
        self.fill_schedule = deque(fill_schedule or ())
        self.fill_price = fill_price
        self.submitted: list[Order] = []
        self.canceled: list[str] = []
        self.queried: list[str] = []
        self.closed = False
        self.orders: dict[str, ExchangeOrder] = {}
        self._trades: list[ExchangeTrade] = []
        self._sequence = 0
        if latent_order:
            self._latent = True
        else:
            self._latent = False

    # -- protocol -----------------------------------------------------

    def client_order_id(self, order: Order) -> str:
        return f"{self.venue}-{order.client_order_id}"

    async def close(self) -> None:
        self.closed = True

    async def instrument_rules(self, symbol: str) -> InstrumentRules:
        return InstrumentRules(
            venue=self.venue,
            symbol=symbol,
            tick_size=Decimal("0.01"),
            quantity_step=Decimal("0.0001"),
            minimum_quantity=Decimal("0.0001"),
            quantity_unit=QuantityUnit.BASE,
        )

    async def normalize_order(
        self,
        order: Order,
        *,
        reference_price: Decimal | None = None,
    ) -> NormalizedOrder:
        return NormalizedOrder(
            quantity=order.quantity,
            price=order.price,
            client_order_id=self.client_order_id(order),
        )

    async def submit_order(self, order: Order) -> ExchangeOrder:
        action = (
            self.submit_actions.popleft() if self.submit_actions else "ok"
        )
        client_id = self.client_order_id(order)
        if action == "hang":
            await asyncio.sleep(5)
        if action == "slow_then_land":
            # The venue accepts the order but our ack never arrives in time.
            self._register(order, client_id, Decimal(0), "new")
            await asyncio.sleep(5)
        if action.startswith("reject:"):
            raise ExchangeAPIError(
                venue=self.venue,
                status=400,
                code=-1013,
                message=action.split(":", 1)[1],
                payload={},
            )
        if action.startswith("retryable:"):
            raise ExchangeAPIError(
                venue=self.venue,
                status=503,
                code=-1001,
                message=action.split(":", 1)[1],
                payload={},
            )
        if action == "rate_limited":
            raise ExchangeAPIError(
                venue=self.venue,
                status=429,
                code=-1003,
                message="Too many requests",
                payload={},
            )
        if action == "ambiguous":
            self._register(order, client_id, Decimal(0), "new")
            raise AmbiguousOrderError(self.venue, client_id)
        self.submitted.append(order)
        filled = (
            self.fill_schedule.popleft()
            if self.fill_schedule
            else order.quantity
        )
        status = self._status(filled, order.quantity)
        return self._register(order, client_id, filled, status)

    async def cancel_order(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> ExchangeOrder:
        existing = self._lookup(exchange_order_id, client_order_id)
        if existing is None:
            raise ExchangeAPIError(
                venue=self.venue,
                status=400,
                code=-2011,
                message="Unknown order sent",
                payload={},
            )
        self.canceled.append(existing.client_order_id)
        canceled = ExchangeOrder(
            venue=existing.venue,
            symbol=existing.symbol,
            exchange_order_id=existing.exchange_order_id,
            client_order_id=existing.client_order_id,
            status="canceled",
            side=existing.side,
            order_type=existing.order_type,
            quantity=existing.quantity,
            filled_quantity=existing.filled_quantity,
            price=existing.price,
            average_fill_price=existing.average_fill_price,
            timestamp_ms=1,
            raw={},
        )
        self.orders[existing.client_order_id] = canceled
        return canceled

    async def query_order(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> ExchangeOrder:
        self.queried.append(client_order_id or exchange_order_id or "")
        action = self.query_actions.popleft() if self.query_actions else "ok"
        if action == "absent":
            raise ExchangeAPIError(
                venue=self.venue,
                status=400,
                code=-2013,
                message="Order does not exist",
                payload={},
            )
        existing = self._lookup(exchange_order_id, client_order_id)
        if existing is None:
            raise ExchangeAPIError(
                venue=self.venue,
                status=400,
                code=-2013,
                message="Order does not exist",
                payload={},
            )
        if self.fill_schedule and existing.status not in {
            "filled",
            "canceled",
        }:
            filled = self.fill_schedule.popleft()
            status = self._status(filled, existing.quantity)
            updated = ExchangeOrder(
                venue=existing.venue,
                symbol=existing.symbol,
                exchange_order_id=existing.exchange_order_id,
                client_order_id=existing.client_order_id,
                status=status,
                side=existing.side,
                order_type=existing.order_type,
                quantity=existing.quantity,
                filled_quantity=filled,
                price=existing.price,
                average_fill_price=(
                    self.fill_price if filled > 0 else None
                ),
                timestamp_ms=1,
                raw={},
            )
            self.orders[existing.client_order_id] = updated
            self._emit_trade(updated)
            return updated
        return existing

    async def open_orders(
        self,
        symbol: str | None = None,
    ) -> tuple[ExchangeOrder, ...]:
        return tuple(
            order
            for order in self.orders.values()
            if order.status in {"new", "partially_filled"}
        )

    async def trades(
        self,
        symbol: str,
        *,
        limit: int = 100,
    ) -> tuple[ExchangeTrade, ...]:
        return tuple(self._trades)

    async def account(self, currency: str | None = None) -> AccountSnapshot:
        return AccountSnapshot(
            venue=self.venue,
            currency=currency,
            timestamp_ms=1,
            raw={},
        )

    async def reconcile(
        self,
        oms: Any,
        symbol: str,
    ) -> ReconciliationSnapshot:
        return ReconciliationSnapshot(
            venue=self.venue,
            symbol=symbol,
            captured_at_ms=1,
            exchange_open_orders=await self.open_orders(symbol),
            recent_trades=await self.trades(symbol),
            account=await self.account(),
            missing_on_exchange=(),
            unknown_on_exchange=(),
        )

    # -- helpers ------------------------------------------------------

    def _status(self, filled: Decimal, quantity: Decimal) -> str:
        if filled >= quantity:
            return "filled"
        if filled > 0:
            return "partially_filled"
        return "new"

    def _register(
        self,
        order: Order,
        client_id: str,
        filled: Decimal,
        status: str,
    ) -> ExchangeOrder:
        self._sequence += 1
        exchange_order = ExchangeOrder(
            venue=self.venue,
            symbol=order.symbol,
            exchange_order_id=f"{self.venue}-EX-{self._sequence}",
            client_order_id=client_id,
            status=status,
            side=order.side.value,
            order_type=order.order_type.value,
            quantity=order.quantity,
            filled_quantity=filled,
            price=order.price,
            average_fill_price=self.fill_price if filled > 0 else None,
            timestamp_ms=1,
            raw={},
        )
        self.orders[client_id] = exchange_order
        self._emit_trade(exchange_order)
        return exchange_order

    def _emit_trade(self, order: ExchangeOrder) -> None:
        if order.filled_quantity <= 0:
            return
        recorded = sum(
            (
                trade.quantity
                for trade in self._trades
                if trade.exchange_order_id == order.exchange_order_id
            ),
            Decimal(0),
        )
        delta = order.filled_quantity - recorded
        if delta <= 0:
            return
        self._trades.append(
            ExchangeTrade(
                venue=self.venue,
                symbol=order.symbol,
                trade_id=f"{order.exchange_order_id}-T{len(self._trades)}",
                exchange_order_id=order.exchange_order_id,
                side=order.side,
                price=self.fill_price,
                quantity=delta,
                fee=Decimal("0.01"),
                fee_currency="USDT",
                timestamp_ms=1,
                raw={},
            )
        )

    def _lookup(
        self,
        exchange_order_id: str | None,
        client_order_id: str | None,
    ) -> ExchangeOrder | None:
        if client_order_id is not None and client_order_id in self.orders:
            return self.orders[client_order_id]
        if exchange_order_id is not None:
            for order in self.orders.values():
                if order.exchange_order_id == exchange_order_id:
                    return order
        return None


def build_request(
    *,
    signal_id: str = "sig-1",
    quantity: Decimal = Decimal("10"),
) -> OpportunityRequest:
    return OpportunityRequest(
        strategy_id="basis-arb",
        strategy_version="v3.4.2",
        signal_id=signal_id,
        canonical_symbol="BTC-USDT",
        expected_edge=Decimal("420"),
        legs=(
            LegRequest(
                venue="binance",
                symbol="BTCUSDT",
                side=Side.BUY,
                quantity=quantity,
                reference_price=Decimal("100"),
                order_type=OrderType.LIMIT,
                limit_price=Decimal("100"),
            ),
            LegRequest(
                venue="okx",
                symbol="BTC-USDT",
                side=Side.SELL,
                quantity=quantity,
                reference_price=Decimal("101"),
                order_type=OrderType.LIMIT,
                limit_price=Decimal("101"),
            ),
        ),
    )


def build_coordinator(
    adapters: dict[str, ScriptedAdapter],
    store: OpportunityStore,
    *,
    residual_policy: ResidualPolicy | None = None,
    timeouts: OpportunityTimeouts | None = None,
    max_reserved_notional: Decimal = Decimal("1000000"),
) -> OpportunityCoordinator:
    coordinator = OpportunityCoordinator(
        adapters,
        store,
        timeouts=timeouts or FAST,
        residual_policy=residual_policy or ResidualPolicy(),
        max_reserved_notional=max_reserved_notional,
    )
    coordinator.update_quote(
        MarketQuote(
            venue="binance",
            symbol="BTCUSDT",
            bid_price=Decimal("99.9"),
            ask_price=Decimal("100.1"),
            fee_bps=Decimal("10"),
        )
    )
    coordinator.update_quote(
        MarketQuote(
            venue="okx",
            symbol="BTC-USDT",
            bid_price=Decimal("99.95"),
            ask_price=Decimal("100.05"),
            fee_bps=Decimal("2"),
        )
    )
    return coordinator


# ----------------------------------------------------------------------
# classification
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Filter failure: PERCENT_PRICE", RejectReason.PRICE_BAND),
        ("Order would immediately match and take", RejectReason.POST_ONLY),
        ("Account has insufficient balance", RejectReason.INSUFFICIENT_MARGIN),
        ("Position exceeds the maximum position", RejectReason.RISK_LIMIT),
        ("System under maintenance", RejectReason.EXCHANGE_MAINTENANCE),
        ("Too many requests", RejectReason.RATE_LIMITED),
        ("Duplicate clientOrderId", RejectReason.DUPLICATE_ORDER),
        ("Filter failure: MIN_NOTIONAL", RejectReason.INVALID_ORDER),
        ("something nobody has seen", RejectReason.UNKNOWN),
    ],
)
def test_reject_classification_is_deterministic(
    message: str,
    expected: RejectReason,
) -> None:
    assert classify_reject(message) is expected
    assert classify_reject(message) is classify_reject(message.upper())


def test_reject_actions_cover_every_reason() -> None:
    for reason in RejectReason:
        assert isinstance(action_for_reject(reason), RejectAction)
    assert action_for_reject(RejectReason.RISK_LIMIT) is (
        RejectAction.ABORT_PERMANENT
    )
    assert action_for_reject(RejectReason.INVALID_ORDER) is (
        RejectAction.MANUAL_INVESTIGATION
    )


def test_retry_classification() -> None:
    assert classify_retry(asyncio.TimeoutError()) is (
        RetryDirective.QUERY_THEN_RETRY
    )
    assert classify_retry(AmbiguousOrderError("binance", "abc")) is (
        RetryDirective.QUERY_THEN_RETRY
    )
    assert classify_retry(
        ExchangeAPIError(
            venue="binance",
            status=429,
            code=-1003,
            message="Too many requests",
            payload={},
        )
    ) is RetryDirective.DELAY_RETRY
    assert classify_retry(
        ExchangeAPIError(
            venue="binance",
            status=503,
            code=-1001,
            message="Service busy",
            payload={},
        )
    ) is RetryDirective.BACKOFF_RETRY
    assert classify_retry(
        ExchangeAPIError(
            venue="binance",
            status=400,
            code=-1013,
            message="Filter failure: LOT_SIZE",
            payload={},
        )
    ) is RetryDirective.NO_RETRY
    assert classify_retry(ConnectionResetError("connection reset")) is (
        RetryDirective.RECONNECT_AND_RESYNC
    )


# ----------------------------------------------------------------------
# happy path
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_venue_happy_path_closes_flat() -> None:
    adapters = {
        "binance": ScriptedAdapter("binance"),
        "okx": ScriptedAdapter("okx"),
    }
    with OpportunityStore() as store:
        coordinator = build_coordinator(adapters, store)
        result = await coordinator.execute(build_request())

    assert result.state is OpportunityState.CLOSED
    assert result.residual_quantity == 0
    assert result.duplicate_free
    assert [leg.state for leg in result.legs] == [
        LegState.FILLED,
        LegState.FILLED,
    ]
    assert OpportunityState.RISK_RESERVED.value in result.transitions
    assert OpportunityState.ORDERS_CREATED.value in result.transitions
    assert OpportunityState.DISPATCHED.value in result.transitions
    assert OpportunityState.FULLY_FILLED.value in result.transitions
    assert OpportunityState.RECONCILING.value in result.transitions
    assert result.reconciliation is not None
    assert result.reconciliation.balanced
    assert result.reconciliation.risk_released
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_legs_are_dispatched_concurrently() -> None:
    order: list[str] = []

    class SlowAdapter(ScriptedAdapter):
        def __init__(self, venue: str, delay: float) -> None:
            super().__init__(venue)
            self.delay = delay

        async def submit_order(self, submitted: Order) -> ExchangeOrder:
            order.append(f"start:{self.venue}")
            await asyncio.sleep(self.delay)
            order.append(f"end:{self.venue}")
            return await super().submit_order(submitted)

    adapters = {
        "binance": SlowAdapter("binance", 0.02),
        "okx": SlowAdapter("okx", 0.02),
    }
    with OpportunityStore() as store:
        coordinator = build_coordinator(
            adapters,
            store,
            timeouts=OpportunityTimeouts(
                ack_timeout_ms=500,
                first_fill_timeout_ms=800,
                complete_fill_timeout_ms=1000,
                poll_interval_ms=1,
            ),
        )
        await coordinator.execute(build_request())

    # Both legs start before either finishes: dispatch is truly parallel.
    assert order[:2] == ["start:binance", "start:okx"] or order[:2] == [
        "start:okx",
        "start:binance",
    ]


# ----------------------------------------------------------------------
# partial fills, hedging and unwind
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_fill_triggers_deterministic_unwind() -> None:
    adapters = {
        # Buys only 6 of 10 and then stops progressing.
        "binance": ScriptedAdapter(
            "binance",
            fill_schedule=(Decimal("6"),),
        ),
        "okx": ScriptedAdapter("okx"),
    }
    with OpportunityStore() as store:
        coordinator = build_coordinator(adapters, store)
        result = await coordinator.execute(build_request())

        assert result.transitions.count(
            OpportunityState.HEDGE_REQUIRED.value
        ) == 1
        assert OpportunityState.UNWINDING.value in result.transitions
        assert len(result.unwinds) == 1
        plan = result.unwinds[0]
        # Long 6 versus short 10 leaves us short 4, so we must buy 4 back.
        assert plan.side is Side.BUY
        assert plan.quantity == Decimal("4")
        assert result.residual_quantity == 0
        assert result.state is OpportunityState.CLOSED

        events = [
            event.event_type for event in store.load_events(
                result.opportunity_id
            )
        ]
        assert "hedge_required" in events
        assert "unwind_planned" in events


@pytest.mark.asyncio
async def test_unwind_venue_selection_is_deterministic_and_priced() -> None:
    adapters = {
        "binance": ScriptedAdapter("binance"),
        "okx": ScriptedAdapter("okx"),
    }
    with OpportunityStore() as store:
        coordinator = build_coordinator(
            adapters,
            store,
            residual_policy=ResidualPolicy(
                unwind_price_mode=UnwindPriceMode.AGGRESSIVE_LIMIT,
                unwind_aggression_bps=Decimal("10"),
            ),
        )
        legs = coordinator._build_legs(  # noqa: SLF001 - direct unit check
            build_request(),
            "OPP-TEST",
        )
        first = coordinator.plan_unwind("BTC-USDT", Decimal("-4"), legs)
        second = coordinator.plan_unwind("BTC-USDT", Decimal("-4"), legs)

    # Same inputs, same plan - every time.
    assert first == second
    # OKX asks 100.05 with 2bps fee, Binance asks 100.1 with 10bps: OKX wins.
    assert first.venue == "okx"
    assert first.side is Side.BUY
    assert first.price is not None
    assert first.price > Decimal("100.05")
    assert [venue for venue, _ in first.candidates] == ["okx", "binance"]


@pytest.mark.asyncio
async def test_unwind_priority_overrides_price() -> None:
    adapters = {
        "binance": ScriptedAdapter("binance"),
        "okx": ScriptedAdapter("okx"),
    }
    with OpportunityStore() as store:
        coordinator = build_coordinator(
            adapters,
            store,
            residual_policy=ResidualPolicy(
                unwind_venue_priority=("binance",),
            ),
        )
        legs = coordinator._build_legs(  # noqa: SLF001
            build_request(),
            "OPP-TEST",
        )
        plan = coordinator.plan_unwind("BTC-USDT", Decimal("-4"), legs)

    assert plan.venue == "binance"
    assert plan.price is None  # market mode


@pytest.mark.asyncio
async def test_unwind_without_a_quote_flags_reconciliation() -> None:
    adapters = {
        "binance": ScriptedAdapter(
            "binance",
            fill_schedule=(Decimal("6"),),
        ),
        "okx": ScriptedAdapter("okx"),
    }
    with OpportunityStore() as store:
        coordinator = OpportunityCoordinator(
            adapters,
            store,
            timeouts=FAST,
        )
        # Deliberately no quotes registered.
        result = await coordinator.execute(build_request())

        assert result.state is OpportunityState.RECONCILIATION_REQUIRED
        assert result.residual_quantity == Decimal("-4")
        assert result.reconciliation is not None
        assert not result.reconciliation.balanced
        events = [
            event.event_type
            for event in store.load_events(result.opportunity_id)
        ]
        assert "unwind_unavailable" in events


# ----------------------------------------------------------------------
# rejects
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_is_classified_and_never_retried() -> None:
    adapters = {
        "binance": ScriptedAdapter(
            "binance",
            submit_actions=("reject:Filter failure: MIN_NOTIONAL",),
        ),
        "okx": ScriptedAdapter("okx"),
    }
    with OpportunityStore() as store:
        coordinator = build_coordinator(adapters, store)
        result = await coordinator.execute(build_request())

    rejected = result.legs[0]
    assert rejected.state is LegState.REJECTED
    assert rejected.reject_reason is RejectReason.INVALID_ORDER
    assert rejected.reject_action is RejectAction.MANUAL_INVESTIGATION
    assert rejected.attempts == 1
    assert adapters["binance"].submitted == []
    # The surviving short leg must be unwound.
    assert result.unwinds
    assert result.unwinds[0].side is Side.BUY
    assert result.residual_quantity == 0


@pytest.mark.asyncio
async def test_duplicate_reject_adopts_the_existing_order() -> None:
    binance = ScriptedAdapter("binance")

    async def submit(order: Order) -> ExchangeOrder:
        # Pretend a previous attempt landed: register then reject as dupe.
        binance._register(  # noqa: SLF001 - scripted venue behaviour
            order,
            binance.client_order_id(order),
            order.quantity,
            "filled",
        )
        raise ExchangeAPIError(
            venue="binance",
            status=400,
            code=-2010,
            message="Duplicate order sent",
            payload={},
        )

    binance.submit_order = submit  # type: ignore[method-assign]
    adapters = {"binance": binance, "okx": ScriptedAdapter("okx")}
    with OpportunityStore() as store:
        coordinator = build_coordinator(adapters, store)
        result = await coordinator.execute(build_request())

    leg = result.legs[0]
    assert leg.reject_reason is RejectReason.DUPLICATE_ORDER
    assert leg.reject_action is RejectAction.ADOPT_EXISTING
    assert leg.state is LegState.FILLED
    assert leg.filled_quantity == Decimal("10")
    assert result.state is OpportunityState.CLOSED
    assert result.residual_quantity == 0


@pytest.mark.asyncio
async def test_retryable_error_backs_off_then_succeeds() -> None:
    adapters = {
        "binance": ScriptedAdapter(
            "binance",
            submit_actions=("retryable:Service busy", "ok"),
        ),
        "okx": ScriptedAdapter("okx"),
    }
    with OpportunityStore() as store:
        coordinator = build_coordinator(adapters, store)
        result = await coordinator.execute(build_request())

    leg = result.legs[0]
    assert leg.attempts == 2
    assert leg.state is LegState.FILLED
    assert result.state is OpportunityState.CLOSED
    assert len(adapters["binance"].submitted) == 1


@pytest.mark.asyncio
async def test_rate_limited_submission_delays_then_succeeds() -> None:
    adapters = {
        "binance": ScriptedAdapter(
            "binance",
            submit_actions=("rate_limited", "ok"),
        ),
        "okx": ScriptedAdapter("okx"),
    }
    with OpportunityStore() as store:
        coordinator = build_coordinator(adapters, store)
        result = await coordinator.execute(build_request())

    assert result.legs[0].attempts == 2
    assert result.state is OpportunityState.CLOSED


# ----------------------------------------------------------------------
# timeouts, late acks and reconnects
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ack_timeout_adopts_a_late_landing_order() -> None:
    """The classic late-ack: we time out, the venue accepted it anyway."""

    adapters = {
        "binance": ScriptedAdapter(
            "binance",
            submit_actions=("slow_then_land",),
        ),
        "okx": ScriptedAdapter("okx"),
    }
    with OpportunityStore() as store:
        coordinator = build_coordinator(adapters, store)
        result = await coordinator.execute(build_request())

        leg = result.legs[0]
        # Exactly one attempt: the resync found our client order id.
        assert leg.attempts == 1
        assert leg.exchange_order_id is not None
        events = {
            event.event_type
            for event in store.load_events(result.opportunity_id)
        }
        assert "leg_resync_adopted" in events


@pytest.mark.asyncio
async def test_ambiguous_submission_is_resolved_by_query_not_resend() -> None:
    adapters = {
        "binance": ScriptedAdapter("binance", submit_actions=("ambiguous",)),
        "okx": ScriptedAdapter("okx"),
    }
    with OpportunityStore() as store:
        coordinator = build_coordinator(adapters, store)
        result = await coordinator.execute(build_request())

    leg = result.legs[0]
    assert leg.attempts == 1
    assert leg.exchange_order_id is not None
    assert adapters["binance"].submitted == []


@pytest.mark.asyncio
async def test_hard_timeout_cancels_and_unwinds() -> None:
    adapters = {
        "binance": ScriptedAdapter("binance", submit_actions=("hang",)),
        "okx": ScriptedAdapter("okx"),
    }
    with OpportunityStore() as store:
        coordinator = build_coordinator(
            adapters,
            store,
            timeouts=OpportunityTimeouts(
                ack_timeout_ms=20,
                first_fill_timeout_ms=40,
                complete_fill_timeout_ms=60,
                poll_interval_ms=1,
                max_submit_attempts=1,
                backoff_base_ms=1,
            ),
        )
        result = await coordinator.execute(build_request())

    # No exchange order id was ever issued, so we know it never landed.
    assert result.legs[0].state is LegState.ABANDONED
    assert result.legs[0].exchange_order_id is None
    assert OpportunityState.TIMEOUT.value in result.transitions
    # The filled sell leg leaves us short, which must be bought back.
    assert result.unwinds
    assert result.unwinds[0].side is Side.BUY
    assert result.residual_quantity == 0
    assert result.state is OpportunityState.CLOSED


# ----------------------------------------------------------------------
# idempotency and duplicate suppression
# ----------------------------------------------------------------------


def test_opportunity_id_is_stable_for_a_signal() -> None:
    with OpportunityStore() as store:
        coordinator = build_coordinator(
            {
                "binance": ScriptedAdapter("binance"),
                "okx": ScriptedAdapter("okx"),
            },
            store,
        )
        first = coordinator.opportunity_id(build_request())
        second = coordinator.opportunity_id(build_request())
        other = coordinator.opportunity_id(build_request(signal_id="sig-2"))

    assert first == second
    assert first != other
    assert first.startswith("OPP-")


@pytest.mark.asyncio
async def test_client_order_ids_are_stable_across_attempts() -> None:
    adapters = {
        "binance": ScriptedAdapter(
            "binance",
            submit_actions=("retryable:busy", "retryable:busy", "ok"),
        ),
        "okx": ScriptedAdapter("okx"),
    }
    with OpportunityStore() as store:
        coordinator = build_coordinator(adapters, store)
        result = await coordinator.execute(build_request())
        leg_records = store.load_legs(result.opportunity_id)

    leg = result.legs[0]
    assert leg.attempts == 3
    # One identity, three attempts: retries can never duplicate the order.
    assert leg.client_order_id == leg_records[0].client_order_id
    assert len({record.client_order_id for record in leg_records}) == len(
        leg_records
    )
    assert leg.idempotency_key == leg_records[0].idempotency_key


@pytest.mark.asyncio
async def test_replaying_a_closed_signal_creates_no_new_orders() -> None:
    adapters = {
        "binance": ScriptedAdapter("binance"),
        "okx": ScriptedAdapter("okx"),
    }
    with OpportunityStore() as store:
        coordinator = build_coordinator(adapters, store)
        first = await coordinator.execute(build_request())
        submissions = len(adapters["binance"].submitted)
        second = await coordinator.execute(build_request())

    assert first.opportunity_id == second.opportunity_id
    assert len(adapters["binance"].submitted) == submissions
    assert second.state is OpportunityState.CLOSED
    assert second.transitions == (f"replayed:{OpportunityState.CLOSED.value}",)


def test_store_refuses_to_reuse_a_client_order_id() -> None:
    with OpportunityStore() as store:
        base = LegRecord(
            opportunity_id="OPP-A",
            leg_index=0,
            venue="binance",
            symbol="BTCUSDT",
            side="buy",
            order_type="limit",
            target_quantity=Decimal("1"),
            client_order_id="binance-OPP-A:0",
            idempotency_key="k",
            state="pending",
            updated_ns=1,
        )
        store.save_leg(base)
        store.save_leg(base)  # idempotent re-save is fine

        with pytest.raises(DuplicateClientOrderIdError):
            store.save_leg(replace(base, opportunity_id="OPP-B"))


@pytest.mark.asyncio
async def test_fills_are_deduplicated_in_the_ledger() -> None:
    adapters = {
        "binance": ScriptedAdapter("binance"),
        "okx": ScriptedAdapter("okx"),
    }
    with OpportunityStore() as store:
        coordinator = build_coordinator(adapters, store)
        result = await coordinator.execute(build_request())
        before = len(store.load_fills(result.opportunity_id))
        # Replaying the same executions must not double count.
        await coordinator._collect_trades(  # noqa: SLF001
            result.opportunity_id,
            [
                coordinator._leg_from_record(record)  # noqa: SLF001
                for record in store.load_legs(result.opportunity_id)
            ],
        )
        after = len(store.load_fills(result.opportunity_id))

    assert before > 0
    assert before == after


# ----------------------------------------------------------------------
# persistence and restart recovery
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_is_written_ahead_of_every_external_call() -> None:
    adapters = {
        "binance": ScriptedAdapter("binance"),
        "okx": ScriptedAdapter("okx"),
    }
    with OpportunityStore() as store:
        coordinator = build_coordinator(adapters, store)
        result = await coordinator.execute(build_request())
        events = store.load_events(result.opportunity_id)

    types = [event.event_type for event in events]
    assert types[0] == "opportunity_created"
    submitting = types.index("leg_submitting")
    acknowledged = types.index("leg_acknowledged")
    # The intent to submit is durable before the acknowledgement lands.
    assert submitting < acknowledged
    assert "risk_reserved" in types
    assert "risk_released" in types
    assert "reconciliation" in types
    assert [event.sequence for event in events] == sorted(
        event.sequence for event in events
    )


@pytest.mark.asyncio
async def test_process_restart_recovers_and_flattens(
    tmp_path: Path,
) -> None:
    database = tmp_path / "opportunities.sqlite3"

    # ---- process 1: dies after one leg fills -------------------------
    crashed = {
        "binance": ScriptedAdapter("binance"),
        "okx": ScriptedAdapter("okx", submit_actions=("hang",)),
    }
    with OpportunityStore(database) as store:
        coordinator = build_coordinator(
            crashed,
            store,
            timeouts=OpportunityTimeouts(
                ack_timeout_ms=20,
                first_fill_timeout_ms=40,
                complete_fill_timeout_ms=60,
                poll_interval_ms=1,
                max_submit_attempts=1,
                backoff_base_ms=1,
            ),
        )
        request = build_request()
        opportunity_id = coordinator.opportunity_id(request)
        record = coordinator._create(  # noqa: SLF001
            request,
            opportunity_id,
            [],
        )
        legs = coordinator._build_legs(request, opportunity_id)  # noqa: SLF001
        coordinator._reserve_risk(record, request, [])  # noqa: SLF001
        coordinator._create_orders(record, legs, [])  # noqa: SLF001
        await coordinator._dispatch_leg(  # noqa: SLF001
            opportunity_id,
            legs[0],
        )
        # The process dies here: leg 0 filled, leg 1 never dispatched.

    # ---- process 2: fresh objects, same database ---------------------
    restarted = {
        "binance": ScriptedAdapter("binance"),
        "okx": ScriptedAdapter("okx"),
    }
    # Recreate the venue state the first process left behind.
    restarted["binance"].orders = dict(crashed["binance"].orders)
    restarted["binance"]._trades = list(  # noqa: SLF001
        crashed["binance"]._trades  # noqa: SLF001
    )
    restarted["binance"]._sequence = crashed[  # noqa: SLF001
        "binance"
    ]._sequence  # noqa: SLF001

    with OpportunityStore(database) as store:
        assert store.open_opportunity_ids() == (opportunity_id,)
        coordinator = build_coordinator(restarted, store)
        results = await coordinator.recover()

    assert len(results) == 1
    recovered = results[0]
    assert recovered.opportunity_id == opportunity_id
    assert recovered.transitions[0].startswith("recover:")
    # We were long 10 with no hedge, so recovery must sell 10 back.
    assert recovered.unwinds
    assert recovered.unwinds[0].side is Side.SELL
    assert recovered.unwinds[0].quantity == Decimal("10")
    assert recovered.residual_quantity == 0
    assert recovered.state is OpportunityState.CLOSED
    # No duplicate order was created for the leg that already existed.
    assert len(restarted["binance"].submitted) == 0

    with OpportunityStore(database) as store:
        assert store.open_opportunity_ids() == ()


@pytest.mark.asyncio
async def test_recovery_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "opportunities.sqlite3"
    adapters = {
        "binance": ScriptedAdapter("binance"),
        "okx": ScriptedAdapter("okx"),
    }
    with OpportunityStore(database) as store:
        coordinator = build_coordinator(adapters, store)
        await coordinator.execute(build_request())

    with OpportunityStore(database) as store:
        coordinator = build_coordinator(adapters, store)
        assert await coordinator.recover() == ()


# ----------------------------------------------------------------------
# risk and reconciliation
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aggregate_risk_limit_blocks_the_opportunity() -> None:
    adapters = {
        "binance": ScriptedAdapter("binance"),
        "okx": ScriptedAdapter("okx"),
    }
    with OpportunityStore() as store:
        coordinator = build_coordinator(
            adapters,
            store,
            max_reserved_notional=Decimal("100"),
        )
        with pytest.raises(OpportunityRiskLimitError):
            await coordinator.execute(build_request())

        assert adapters["binance"].submitted == []
        assert adapters["okx"].submitted == []
        events = [
            event.event_type
            for event in store.load_events(
                coordinator.opportunity_id(build_request())
            )
        ]
        assert "risk_reservation_refused" in events


@pytest.mark.asyncio
async def test_reconciliation_compares_four_views() -> None:
    adapters = {
        "binance": ScriptedAdapter("binance"),
        "okx": ScriptedAdapter("okx"),
    }
    with OpportunityStore() as store:
        coordinator = build_coordinator(adapters, store)
        result = await coordinator.execute(build_request())

    reconciliation = result.reconciliation
    assert reconciliation is not None
    assert set(reconciliation.exchange_positions) == {"binance", "okx"}
    assert set(reconciliation.local_positions) == {"binance", "okx"}
    assert set(reconciliation.risk_positions) == {"binance", "okx"}
    assert reconciliation.local_positions["binance"] == Decimal("10")
    assert reconciliation.local_positions["okx"] == Decimal("-10")
    assert reconciliation.exchange_positions["binance"] == Decimal("10")
    assert reconciliation.net_position == 0
    assert reconciliation.ledger_position == 0
    assert reconciliation.open_order_count == 0
    assert reconciliation.risk_released
    assert reconciliation.fees > 0
    assert reconciliation.balanced

    payload = reconciliation.to_dict()
    assert payload["balanced"] is True
    assert payload["net_position"] == "0"


@pytest.mark.asyncio
async def test_result_serialises_for_operators() -> None:
    adapters = {
        "binance": ScriptedAdapter(
            "binance",
            fill_schedule=(Decimal("6"),),
        ),
        "okx": ScriptedAdapter("okx"),
    }
    with OpportunityStore() as store:
        coordinator = build_coordinator(adapters, store)
        result = await coordinator.execute(build_request())

    payload = result.to_dict()
    assert payload["state"] == OpportunityState.CLOSED.value
    assert payload["duplicate_free"] is True
    assert len(payload["legs"]) == 3  # two legs plus the unwind
    assert payload["unwinds"][0]["side"] == Side.BUY.value
    assert payload["reconciliation"]["balanced"] is True
    assert all(
        leg["idempotency_key"] for leg in payload["legs"]
    )


# ----------------------------------------------------------------------
# validation
# ----------------------------------------------------------------------


def test_requests_reject_invalid_shapes() -> None:
    with pytest.raises(ValueError, match="at least two legs"):
        OpportunityRequest(
            strategy_id="s",
            strategy_version="v",
            signal_id="sig",
            canonical_symbol="BTC-USDT",
            legs=(
                LegRequest(
                    venue="binance",
                    symbol="BTCUSDT",
                    side=Side.BUY,
                    quantity=Decimal("1"),
                    reference_price=Decimal("100"),
                ),
            ),
        )

    with pytest.raises(ValueError, match="distinct venue"):
        OpportunityRequest(
            strategy_id="s",
            strategy_version="v",
            signal_id="sig",
            canonical_symbol="BTC-USDT",
            legs=(
                LegRequest(
                    venue="binance",
                    symbol="BTCUSDT",
                    side=Side.BUY,
                    quantity=Decimal("1"),
                    reference_price=Decimal("100"),
                ),
                LegRequest(
                    venue="binance",
                    symbol="BTCUSDT",
                    side=Side.SELL,
                    quantity=Decimal("1"),
                    reference_price=Decimal("100"),
                ),
            ),
        )


def test_timeouts_and_policies_are_validated() -> None:
    with pytest.raises(ValueError, match="ack timeout"):
        OpportunityTimeouts(ack_timeout_ms=0)
    with pytest.raises(ValueError, match="first fill timeout"):
        OpportunityTimeouts(ack_timeout_ms=100, first_fill_timeout_ms=50)
    with pytest.raises(ValueError, match="complete fill timeout"):
        OpportunityTimeouts(
            ack_timeout_ms=10,
            first_fill_timeout_ms=20,
            complete_fill_timeout_ms=15,
        )
    with pytest.raises(ValueError, match="residual ratio"):
        ResidualPolicy(max_residual_ratio=Decimal("2"))
    with pytest.raises(ValueError, match="crossed or locked"):
        MarketQuote(
            venue="binance",
            symbol="BTCUSDT",
            bid_price=Decimal("101"),
            ask_price=Decimal("100"),
        )


def test_coordinator_requires_two_venues() -> None:
    with OpportunityStore() as store:
        with pytest.raises(ValueError, match="two venues"):
            OpportunityCoordinator(
                {"binance": ScriptedAdapter("binance")},
                store,
            )
        with pytest.raises(ValueError, match="venue mismatch"):
            OpportunityCoordinator(
                {
                    "binance": ScriptedAdapter("okx"),
                    "okx": ScriptedAdapter("okx"),
                },
                store,
            )
