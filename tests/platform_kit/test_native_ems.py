from __future__ import annotations

import importlib
import math
import threading

import pytest


ems = importlib.import_module("hft_platform_kit._ems")


def quote(
    venue: str,
    *,
    symbol: str = "BTCUSD",
    bid: float = 99.0,
    ask: float = 100.0,
    bid_size: float = 10.0,
    ask_size: float = 10.0,
    fee_bps: float = 0.0,
    latency_us: float = 0.0,
    healthy: bool = True,
):
    value = ems.VenueQuote()
    value.venue = venue
    value.symbol = symbol
    value.bid = bid
    value.ask = ask
    value.bid_size = bid_size
    value.ask_size = ask_size
    value.fee_bps = fee_bps
    value.latency_us = latency_us
    value.healthy = healthy
    return value


def leg(
    symbol: str,
    side,
    quantity: float,
    reference_price: float,
    preferences: list[str] | None = None,
):
    value = ems.Leg()
    value.symbol = symbol
    value.side = side
    value.quantity = quantity
    value.reference_price = reference_price
    value.venue_preferences = preferences or []
    return value


def call_in_thread(function):
    result: list[BaseException | object] = []

    def run() -> None:
        try:
            result.append(function())
        except BaseException as error:
            result.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join()
    return result[0]


def test_single_loop_orders_by_timestamp_then_sequence() -> None:
    loop = ems.SingleLoop()
    assert loop.enqueue(20, "late") == 0
    assert loop.enqueue(10, "first", "a") == 1
    assert loop.enqueue(10, "second", "b") == 2

    drained = loop.drain_until(10)
    assert [(event.timestamp_ns, event.sequence, event.event_type) for event in drained] == [
        (10, 1, "first"),
        (10, 2, "second"),
    ]
    assert loop.pop().event_type == "late"
    assert loop.pop() is None


def test_stateful_native_objects_enforce_thread_affinity() -> None:
    loop = ems.SingleLoop()
    risk = ems.RiskReservationBook(1000.0)
    system = ems.ExecutionManagementSystem()

    for operation in (
        lambda: loop.enqueue(1, "event"),
        lambda: loop.size,
        lambda: risk.reserved_notional,
        lambda: system.plan("missing"),
        lambda: system.loop(),
    ):
        error = call_in_thread(operation)
        assert isinstance(error, RuntimeError)
        assert "thread" in str(error)


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf, 0.0, -1.0])
def test_schedule_rejects_invalid_quantities(invalid: float) -> None:
    with pytest.raises(ValueError):
        ems.Scheduler.twap(1, 2, invalid, 2)


def test_twap_has_deterministic_timestamps_and_exact_conservation() -> None:
    slices = ems.Scheduler.twap(10, 20, 1.0, 4)
    assert [item.timestamp_ns for item in slices] == [10, 13, 16, 20]
    assert sum(item.quantity for item in slices) == 1.0
    assert all(item.quantity >= 0.0 for item in slices)


def test_vwap_supports_zero_buckets_and_conserves_quantity() -> None:
    slices = ems.Scheduler.vwap(100, 104, 7.3, [0.0, 2.0, 0.0, 3.0, 5.0])
    assert [item.timestamp_ns for item in slices] == [100, 101, 102, 103, 104]
    assert slices[0].quantity == 0.0
    assert slices[2].quantity == 0.0
    assert sum(item.quantity for item in slices) == 7.3

    with pytest.raises(ValueError):
        ems.Scheduler.vwap(0, 1, 1.0, [0.0, 0.0])
    with pytest.raises(ValueError):
        ems.Scheduler.vwap(0, 1, 1.0, [1.0, math.nan])


def test_router_accounts_for_fee_and_latency_and_conserves_quantity() -> None:
    router = ems.SmartOrderRouter(latency_penalty_bps_per_ms=5.0)
    routes = router.route(
        ems.Side.BUY,
        8.0,
        [
            quote("A", ask=100.0, ask_size=6.0, fee_bps=10.0, latency_us=1000.0),
            quote("B", ask=100.05, ask_size=5.0),
        ],
    )
    assert [route.venue for route in routes] == ["B", "A"]
    assert [route.quantity for route in routes] == [5.0, 3.0]
    assert sum(route.quantity for route in routes) == 8.0
    assert routes[1].expected_fee == pytest.approx(0.3)


def test_router_ties_are_lexical_and_insufficient_liquidity_fails() -> None:
    router = ems.SmartOrderRouter()
    routes = router.route(
        ems.Side.BUY,
        2.0,
        [quote("z", ask_size=2.0), quote("a", ask_size=2.0)],
    )
    assert [route.venue for route in routes] == ["a"]

    with pytest.raises(RuntimeError, match="insufficient"):
        router.route(
            ems.Side.SELL,
            5.0,
            [quote("a", bid_size=2.0), quote("b", bid_size=2.0)],
        )


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("bid", math.nan),
        ("ask", math.inf),
        ("bid_size", -1.0),
        ("ask_size", math.nan),
        ("fee_bps", -0.1),
        ("latency_us", math.inf),
    ],
)
def test_router_rejects_malformed_quotes(field: str, invalid: float) -> None:
    value = quote("bad")
    setattr(value, field, invalid)
    with pytest.raises(ValueError):
        ems.SmartOrderRouter().route(ems.Side.BUY, 1.0, [value])


def test_risk_reservation_is_atomic_idempotent_and_rollback_safe() -> None:
    risk = ems.RiskReservationBook(1000.0)
    first = [
        leg("BTCUSD", ems.Side.BUY, 2.0, 100.0),
        leg("ETHUSD", ems.Side.SELL, 3.0, 50.0),
    ]
    too_large = [
        leg("BTCUSD", ems.Side.BUY, 4.0, 100.0),
        leg("ETHUSD", ems.Side.SELL, 6.0, 50.0),
    ]

    assert risk.reserve("first", first)
    assert risk.reserve("first", first)
    assert risk.reserved_notional == 350.0
    assert not risk.reserve("second", too_large)
    assert risk.reserved_notional == 350.0
    assert not risk.is_reserved("second")

    changed = list(first)
    changed[0] = leg("BTCUSD", ems.Side.BUY, 1.0, 200.0)
    with pytest.raises(RuntimeError, match="different"):
        risk.reserve("first", changed)
    assert risk.release("first")
    assert risk.reserved_notional == 0.0
    assert not risk.release("first")


def test_multi_leg_route_failure_enters_recovery_and_releases_at_terminal() -> None:
    system = ems.ExecutionManagementSystem()
    risk = ems.RiskReservationBook(10_000.0)
    legs = [
        leg("BTCUSD", ems.Side.BUY, 2.0, 100.0),
        leg("ETHUSD", ems.Side.SELL, 3.0, 50.0),
    ]
    system.create_plan("pair", legs)
    assert system.reserve_plan("pair", risk)
    system.update_quote(quote("venue", symbol="BTCUSD", ask_size=10.0))

    with pytest.raises(RuntimeError, match="leg symbol"):
        system.route_plan("pair")
    plan = system.plan("pair")
    assert plan.state == ems.PlanState.RECOVERY_REQUIRED
    assert plan.risk_reserved
    assert risk.is_reserved("pair")

    with pytest.raises(RuntimeError, match="terminal"):
        system.release_plan("pair", risk)
    system.recover_plan("pair", ems.PlanState.CANCEL_REMAINING)
    system.transition_plan("pair", ems.PlanState.CLOSED)

    other_risk = ems.RiskReservationBook(10_000.0)
    assert other_risk.reserve("pair", legs)
    with pytest.raises(RuntimeError, match="originating risk book"):
        system.release_plan("pair", other_risk)
    assert system.plan("pair").risk_reserved
    assert risk.is_reserved("pair")
    assert other_risk.is_reserved("pair")

    system.release_plan("pair", risk)
    assert not system.plan("pair").risk_reserved
    assert risk.reserved_notional == 0.0
    assert other_risk.is_reserved("pair")
    assert other_risk.release("pair")


def test_multi_leg_state_machine_and_preference_tie_breaking() -> None:
    system = ems.ExecutionManagementSystem()
    risk = ems.RiskReservationBook(10_000.0)
    legs = [
        leg("BTCUSD", ems.Side.BUY, 1.0, 100.0, ["b", "a"]),
        leg("ETHUSD", ems.Side.SELL, 1.0, 50.0),
    ]
    system.create_plan("pair", legs)
    with pytest.raises(RuntimeError, match="invalid"):
        system.transition_plan("pair", ems.PlanState.EXECUTING)
    assert system.reserve_plan("pair", risk)

    for value in (
        quote("a", symbol="BTCUSD", ask_size=1.0),
        quote("b", symbol="BTCUSD", ask_size=1.0),
        quote("x", symbol="ETHUSD", bid=50.0, ask=51.0, bid_size=1.0),
    ):
        system.update_quote(value)

    system.transition_plan("pair", ems.PlanState.ORDERS_CREATED)
    routes = system.route_plan("pair")
    assert routes[0][0].venue == "b"
    assert system.plan("pair").state == ems.PlanState.LEGS_DISPATCHED
    system.transition_plan("pair", ems.PlanState.EXECUTING)
    system.transition_plan("pair", ems.PlanState.PARTIALLY_FILLED)
    system.transition_plan("pair", ems.PlanState.HEDGE_REQUIRED)
    system.transition_plan("pair", ems.PlanState.HEDGING)
    system.transition_plan("pair", ems.PlanState.FILLED)
    system.transition_plan("pair", ems.PlanState.RECONCILING)
    system.transition_plan("pair", ems.PlanState.CLOSED)
    with pytest.raises(RuntimeError, match="invalid"):
        system.transition_plan("pair", ems.PlanState.EXECUTING)
    system.release_plan("pair", risk)
