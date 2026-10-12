"""Coverage for the execution control plane.

Every cell of the control-to-order-state matrix is asserted directly, so
a change to the published behaviour breaks a named test rather than
silently altering what reaches an exchange. The clock is injected
throughout: a control plane whose tests depend on wall-clock timing is a
control plane whose tests are flaky.
"""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from trading_platform_poc.execution_controls import (
    BAND_THRESHOLDS,
    CONTROL_MATRIX,
    CONTROL_PRECEDENCE,
    EXEMPT_PRIORITIES,
    RISK_REDUCING_STAGES,
    CapacityReservationError,
    ControlAction,
    ControlKind,
    ControlLevel,
    ExecutionControlPlane,
    FeedMonitor,
    KillSwitch,
    KillSwitchAuthorizationError,
    MessageBudget,
    MessageRequest,
    OrderStage,
    Priority,
    PriorityMessageQueue,
    QueuedMessage,
    ReservationLedger,
    Reservation,
    ReservationRequest,
    RiskLimits,
    UtilizationBand,
    band_for,
)


class Clock:
    """A hand-cranked nanosecond clock."""

    def __init__(self) -> None:
        self.ns = 1_000_000_000

    def __call__(self) -> int:
        return self.ns

    def advance_ms(self, milliseconds: int) -> None:
        self.ns += milliseconds * 1_000_000


@pytest.fixture
def clock() -> Clock:
    return Clock()


def build_plane(
    clock: Clock,
    *,
    venue_limit: int = 3,
    global_limit: int = 100,
    stale_after_ms: int = 100,
    limits: RiskLimits | None = None,
) -> ExecutionControlPlane:
    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance",
                limit=venue_limit,
                window_ms=1000,
                now_ns=clock,
            ),
            "okx": MessageBudget(
                name="okx", limit=venue_limit, window_ms=1000, now_ns=clock
            ),
        },
        global_budget=MessageBudget(
            name="firm", limit=global_limit, window_ms=1000, now_ns=clock
        ),
        feed_monitor=FeedMonitor(stale_after_ms=stale_after_ms, now_ns=clock),
        limits=limits,
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    plane.feed_monitor.record_tick("okx")
    return plane


def message(
    stage: OrderStage = OrderStage.NEW,
    *,
    venue: str = "binance",
    priority: Priority = Priority.MARKET_MAKING,
    identifier: str = "m1",
    messages: int = 1,
    ttl_ms: int = 5_000,
    delay_reason: str | None = None,
) -> MessageRequest:
    return MessageRequest(
        message_id=identifier,
        strategy_id="alpha",
        venue=venue,
        stage=stage,
        priority=priority,
        messages=messages,
        ttl_ms=ttl_ms,
        delay_reason=delay_reason,
    )


# ---------------------------------------------------------------------
# The published matrix
# ---------------------------------------------------------------------


def test_matrix_covers_every_stage_and_control() -> None:
    assert set(CONTROL_MATRIX) == set(OrderStage)
    for stage, row in CONTROL_MATRIX.items():
        assert set(row) == set(ControlKind), stage


EXPECTED_MATRIX = {
    OrderStage.NEW: {
        ControlKind.VENUE_RATE_LIMIT: ControlAction.QUEUE,
        ControlKind.GLOBAL_THROTTLE: ControlAction.QUEUE,
        ControlKind.STALE_FEED: ControlAction.REJECT,
        ControlKind.KILL_SWITCH: ControlAction.REJECT,
    },
    OrderStage.RISK_APPROVED_WAITING: {
        ControlKind.VENUE_RATE_LIMIT: ControlAction.QUEUE,
        ControlKind.GLOBAL_THROTTLE: ControlAction.QUEUE,
        ControlKind.STALE_FEED: ControlAction.CANCEL_ADMISSION,
        ControlKind.KILL_SWITCH: ControlAction.CANCEL_ADMISSION,
    },
    OrderStage.SENT_AWAITING_ACK: {
        ControlKind.VENUE_RATE_LIMIT: ControlAction.MONITOR,
        ControlKind.GLOBAL_THROTTLE: ControlAction.MONITOR,
        ControlKind.STALE_FEED: ControlAction.MONITOR,
        ControlKind.KILL_SWITCH: ControlAction.CANCEL,
    },
    OrderStage.RESTING: {
        ControlKind.VENUE_RATE_LIMIT: ControlAction.MONITOR,
        ControlKind.GLOBAL_THROTTLE: ControlAction.MONITOR,
        ControlKind.STALE_FEED: ControlAction.CANCEL,
        ControlKind.KILL_SWITCH: ControlAction.MASS_CANCEL,
    },
    OrderStage.PARTIAL_FILL: {
        ControlKind.VENUE_RATE_LIMIT: ControlAction.MONITOR,
        ControlKind.GLOBAL_THROTTLE: ControlAction.HEDGE_ONLY,
        ControlKind.STALE_FEED: ControlAction.HEDGE_ONLY,
        ControlKind.KILL_SWITCH: ControlAction.HEDGE_ONLY,
    },
    OrderStage.FILLED_AWAITING_HEDGE: {
        ControlKind.VENUE_RATE_LIMIT: ControlAction.ADMIT,
        ControlKind.GLOBAL_THROTTLE: ControlAction.ADMIT,
        ControlKind.STALE_FEED: ControlAction.ADMIT,
        ControlKind.KILL_SWITCH: ControlAction.ADMIT,
    },
    OrderStage.REPLACE_REQUEST: {
        ControlKind.VENUE_RATE_LIMIT: ControlAction.QUEUE,
        ControlKind.GLOBAL_THROTTLE: ControlAction.QUEUE,
        ControlKind.STALE_FEED: ControlAction.REJECT,
        ControlKind.KILL_SWITCH: ControlAction.CANCEL,
    },
    OrderStage.CANCEL_REQUEST: {
        ControlKind.VENUE_RATE_LIMIT: ControlAction.ADMIT,
        ControlKind.GLOBAL_THROTTLE: ControlAction.ADMIT,
        ControlKind.STALE_FEED: ControlAction.ADMIT,
        ControlKind.KILL_SWITCH: ControlAction.ADMIT,
    },
    OrderStage.EMERGENCY_HEDGE: {
        ControlKind.VENUE_RATE_LIMIT: ControlAction.ADMIT,
        ControlKind.GLOBAL_THROTTLE: ControlAction.ADMIT,
        ControlKind.STALE_FEED: ControlAction.ADMIT,
        ControlKind.KILL_SWITCH: ControlAction.ADMIT,
    },
    OrderStage.FORCED_LIQUIDATION: {
        ControlKind.VENUE_RATE_LIMIT: None,
        ControlKind.GLOBAL_THROTTLE: None,
        ControlKind.STALE_FEED: ControlAction.ADMIT,
        ControlKind.KILL_SWITCH: ControlAction.ADMIT,
    },
}


@pytest.mark.parametrize("stage", list(OrderStage))
def test_matrix_cells_match_the_specification(stage: OrderStage) -> None:
    assert CONTROL_MATRIX[stage] == EXPECTED_MATRIX[stage]


# ---------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        (OrderStage.NEW, ControlAction.REJECT),
        (OrderStage.RISK_APPROVED_WAITING, ControlAction.CANCEL_ADMISSION),
        (OrderStage.SENT_AWAITING_ACK, ControlAction.CANCEL),
        (OrderStage.RESTING, ControlAction.MASS_CANCEL),
        (OrderStage.PARTIAL_FILL, ControlAction.HEDGE_ONLY),
        (OrderStage.FILLED_AWAITING_HEDGE, ControlAction.ADMIT),
        (OrderStage.REPLACE_REQUEST, ControlAction.CANCEL),
        (OrderStage.CANCEL_REQUEST, ControlAction.ADMIT),
        (OrderStage.EMERGENCY_HEDGE, ControlAction.ADMIT),
        (OrderStage.FORCED_LIQUIDATION, ControlAction.ADMIT),
    ],
)
def test_kill_switch_behaviour_per_stage(
    clock: Clock, stage: OrderStage, expected: ControlAction
) -> None:
    plane = build_plane(clock)
    plane.kill_switch.engage(reason="firm halt", actor="risk-admin")
    assert plane.evaluate(message(stage)).action is expected


def test_kill_switch_outranks_a_breached_rate_limit(clock: Clock) -> None:
    """A queued-by-capacity order is rejected, not queued, once halted."""

    plane = build_plane(clock, venue_limit=1)
    assert plane.evaluate(message(identifier="m1")).action is ControlAction.ADMIT
    assert plane.evaluate(message(identifier="m2")).action is ControlAction.QUEUE

    plane.kill_switch.engage(reason="halt", actor="risk-admin")
    decision = plane.evaluate(message(identifier="m3"))
    assert decision.action is ControlAction.REJECT
    assert decision.control is ControlKind.KILL_SWITCH
    assert decision.level is ControlLevel.GLOBAL_KILL_SWITCH


def test_only_administrators_may_clear_the_kill_switch() -> None:
    switch = KillSwitch(administrators=("risk-admin",))
    switch.engage(reason="halt", actor="any-trader")
    assert switch.active

    with pytest.raises(KillSwitchAuthorizationError):
        switch.clear(actor="any-trader")
    assert switch.active

    switch.clear(actor="risk-admin")
    assert not switch.active


def test_anyone_may_engage_the_kill_switch() -> None:
    """Stopping is never gated; only restarting is."""

    switch = KillSwitch(administrators=("risk-admin",))
    switch.engage(reason="saw something wrong", actor="junior-trader")
    assert switch.active
    assert switch.reason == "saw something wrong"


# ---------------------------------------------------------------------
# Stale feed
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        (OrderStage.NEW, ControlAction.REJECT),
        (OrderStage.RISK_APPROVED_WAITING, ControlAction.CANCEL_ADMISSION),
        (OrderStage.RESTING, ControlAction.CANCEL),
        (OrderStage.REPLACE_REQUEST, ControlAction.REJECT),
        (OrderStage.PARTIAL_FILL, ControlAction.HEDGE_ONLY),
        (OrderStage.FILLED_AWAITING_HEDGE, ControlAction.ADMIT),
        (OrderStage.CANCEL_REQUEST, ControlAction.ADMIT),
        (OrderStage.EMERGENCY_HEDGE, ControlAction.ADMIT),
        (OrderStage.FORCED_LIQUIDATION, ControlAction.ADMIT),
    ],
)
def test_stale_feed_behaviour_per_stage(
    clock: Clock, stage: OrderStage, expected: ControlAction
) -> None:
    plane = build_plane(clock, stale_after_ms=100)
    clock.advance_ms(250)
    assert plane.feed_monitor.is_stale("binance")
    assert plane.evaluate(message(stage)).action is expected


def test_a_venue_never_heard_from_is_stale(clock: Clock) -> None:
    monitor = FeedMonitor(stale_after_ms=100, now_ns=clock)
    assert monitor.is_stale("binance")
    assert monitor.age_ns("binance") is None

    monitor.record_tick("binance")
    assert not monitor.is_stale("binance")


def test_staleness_is_per_venue(clock: Clock) -> None:
    plane = build_plane(clock, stale_after_ms=100)
    clock.advance_ms(250)
    plane.feed_monitor.record_tick("okx")

    assert plane.evaluate(message(venue="binance")).action is (
        ControlAction.REJECT
    )
    assert plane.evaluate(message(venue="okx")).action is ControlAction.ADMIT


def test_stale_cancellation_of_resting_orders_is_configurable(
    clock: Clock,
) -> None:
    plane = build_plane(clock, stale_after_ms=100)
    plane.cancel_resting_when_stale = False
    clock.advance_ms(250)
    decision = plane.evaluate(message(OrderStage.RESTING))
    assert decision.action is ControlAction.MONITOR


# ---------------------------------------------------------------------
# Rate limits, queueing and the reject/queue/delay distinction
# ---------------------------------------------------------------------


def test_venue_rate_limit_queues_rather_than_dropping(clock: Clock) -> None:
    plane = build_plane(clock, venue_limit=3)
    for index in range(3):
        assert plane.evaluate(
            message(identifier=f"m{index}")
        ).action is ControlAction.ADMIT

    decision = plane.evaluate(message(identifier="m3"))
    assert decision.action is ControlAction.QUEUE
    assert decision.control is ControlKind.VENUE_RATE_LIMIT
    assert decision.level is ControlLevel.VENUE_LIMIT
    assert decision.retry_after_ms > 0
    assert len(plane.queue) == 1


def test_firm_limit_binds_before_any_venue_limit(clock: Clock) -> None:
    """The firm budget is more senior, so it must be the named cause."""

    plane = build_plane(clock, venue_limit=50, global_limit=2)
    plane.evaluate(message(identifier="m0"))
    plane.evaluate(message(identifier="m1"))

    decision = plane.evaluate(message(identifier="m2"))
    assert decision.action is ControlAction.QUEUE
    assert decision.control is ControlKind.GLOBAL_THROTTLE
    assert decision.level is ControlLevel.FIRM_MESSAGE_LIMIT


def test_a_firm_limit_rejection_does_not_leak_venue_capacity(
    clock: Clock,
) -> None:
    """The venue reservation must be returned when the firm check wins."""

    plane = build_plane(clock, venue_limit=10, global_limit=1)
    plane.evaluate(message(identifier="m0"))
    before = plane.venue_budgets["binance"].available()
    plane.evaluate(message(identifier="m1"))
    assert plane.venue_budgets["binance"].available() == before


def test_a_venue_rejection_does_not_leak_firm_capacity(clock: Clock) -> None:
    plane = build_plane(clock, venue_limit=1, global_limit=50)
    plane.evaluate(message(identifier="m0"))
    before = plane.global_budget.available()
    decision = plane.evaluate(message(identifier="m1"))
    assert decision.control is ControlKind.VENUE_RATE_LIMIT
    assert plane.global_budget.available() == before


def test_a_message_that_would_expire_is_rejected_not_queued(
    clock: Clock,
) -> None:
    """Queueing a message past its own deadline just hides the loss."""

    plane = build_plane(clock, venue_limit=1)
    plane.evaluate(message(identifier="m0"))
    decision = plane.evaluate(message(identifier="m1", ttl_ms=5))

    assert decision.action is ControlAction.REJECT
    assert len(plane.queue) == 0
    snapshot = plane.venue_budgets["binance"].snapshot()
    assert snapshot.rejected == 1
    assert snapshot.dropped == 0
    assert snapshot.queued == 0


def test_delay_is_distinct_from_rejection(clock: Clock) -> None:
    """A halted auction postpones a still-valid order."""

    plane = build_plane(clock)
    decision = plane.evaluate(
        message(delay_reason="volatility interruption")
    )
    assert decision.action is ControlAction.DELAY
    assert not decision.transmits
    assert not decision.synchronous_rejection
    assert decision.reason == "volatility interruption"


def test_synchronous_rejection_is_flagged(clock: Clock) -> None:
    plane = build_plane(clock, stale_after_ms=100)
    clock.advance_ms(250)
    decision = plane.evaluate(message(OrderStage.NEW))
    assert decision.action is ControlAction.REJECT
    assert decision.synchronous_rejection
    assert not decision.transmits


def test_budget_window_rolls_over(clock: Clock) -> None:
    budget = MessageBudget(
        name="binance", limit=2, window_ms=1000, now_ns=clock
    )
    assert budget.try_reserve(2)
    budget.consume_reserved(2)
    assert not budget.try_reserve(1)

    clock.advance_ms(1_001)
    assert budget.try_reserve(1)


def test_time_to_refill_shrinks_as_the_window_elapses(clock: Clock) -> None:
    budget = MessageBudget(
        name="binance", limit=1, window_ms=1000, now_ns=clock
    )
    budget.try_reserve(1)
    budget.consume_reserved(1)
    first = budget.time_to_refill_ms()
    clock.advance_ms(400)
    assert budget.time_to_refill_ms() < first


# ---------------------------------------------------------------------
# Exemptions: risk-reducing traffic is never blocked
# ---------------------------------------------------------------------


@pytest.mark.parametrize("stage", sorted(RISK_REDUCING_STAGES, key=str))
def test_risk_reducing_stages_transmit_with_no_capacity_left(
    clock: Clock, stage: OrderStage
) -> None:
    plane = build_plane(clock, venue_limit=1, global_limit=1)
    plane.evaluate(message(identifier="m0"))
    assert plane.venue_budgets["binance"].available() == 0

    decision = plane.evaluate(message(stage, identifier="hedge"))
    assert decision.action is ControlAction.ADMIT, stage


@pytest.mark.parametrize("stage", list(OrderStage))
@pytest.mark.parametrize("priority", sorted(EXEMPT_PRIORITIES, key=str))
def test_exempt_priorities_are_admitted_at_every_stage(
    clock: Clock, priority: Priority, stage: OrderStage
) -> None:
    """The whole grid, not just the stages the matrix already admits.

    ``transmits`` is true for CANCEL and MASS_CANCEL too, so asserting on
    it would let a bug that mass-cancels the book in place of sending one
    hedge pass unnoticed. The action itself is asserted instead.
    """

    plane = build_plane(clock, venue_limit=1, global_limit=1)
    plane.evaluate(message(identifier="m0"))
    assert plane.venue_budgets["binance"].available() == 0

    decision = plane.evaluate(
        message(stage, priority=priority, identifier="risk")
    )
    assert decision.action is ControlAction.ADMIT
    # Either the matrix never subjected this stage to the control, or the
    # exemption carried it through. Both are admissions; neither is a
    # capacity decision that happened to look like one.
    assert decision.reason in {
        "risk_reducing_exemption",
        "stage_exempt_from_control",
    }
    assert decision.control is None


@pytest.mark.parametrize("stage", list(OrderStage))
def test_a_blocked_exempt_message_is_never_charged_for(
    clock: Clock, stage: OrderStage
) -> None:
    """Capacity may only be spent by something that actually transmits."""

    plane = build_plane(clock, venue_limit=1, global_limit=1)
    plane.evaluate(message(identifier="m0"))
    plane.kill_switch.engage(reason="halt", actor="risk-admin")
    before = plane.venue_budgets["binance"].snapshot().used

    decision = plane.evaluate(
        message(stage, priority=Priority.EMERGENCY_HEDGE, identifier="x")
    )
    after = plane.venue_budgets["binance"].snapshot().used
    if decision.action is ControlAction.ADMIT:
        assert after > before
    else:
        assert after == before, decision.action


def test_exempt_traffic_is_still_counted(clock: Clock) -> None:
    """Overage must remain visible even though nothing stopped it."""

    plane = build_plane(clock, venue_limit=1, global_limit=1)
    plane.evaluate(message(identifier="m0"))
    plane.evaluate(
        message(OrderStage.EMERGENCY_HEDGE, priority=Priority.EMERGENCY_HEDGE)
    )
    snapshot = plane.venue_budgets["binance"].snapshot()
    assert snapshot.used == 2
    assert snapshot.utilization_pct > Decimal(100)


def test_cancel_is_never_blocked_by_any_control(clock: Clock) -> None:
    plane = build_plane(clock, venue_limit=1, global_limit=1, stale_after_ms=10)
    plane.evaluate(message(identifier="m0"))
    clock.advance_ms(5)
    plane.kill_switch.engage(reason="halt", actor="risk-admin")

    decision = plane.evaluate(message(OrderStage.CANCEL_REQUEST))
    assert decision.action is ControlAction.ADMIT


def test_forced_liquidation_is_never_blocked(clock: Clock) -> None:
    plane = build_plane(clock, venue_limit=1, global_limit=1, stale_after_ms=10)
    plane.evaluate(message(identifier="m0"))
    clock.advance_ms(500)
    plane.kill_switch.engage(reason="halt", actor="risk-admin")

    decision = plane.evaluate(
        message(
            OrderStage.FORCED_LIQUIDATION,
            priority=Priority.FORCED_LIQUIDATION,
        )
    )
    assert decision.action is ControlAction.ADMIT


# ---------------------------------------------------------------------
# Priority queue
# ---------------------------------------------------------------------


def queued(
    identifier: str,
    priority: Priority,
    *,
    enqueued_ns: int = 0,
    expires_ns: int = 10**18,
    venue: str = "binance",
) -> QueuedMessage:
    return QueuedMessage(
        message_id=identifier,
        strategy_id="alpha",
        venue=venue,
        stage=OrderStage.NEW,
        priority=priority,
        messages=1,
        enqueued_ns=enqueued_ns,
        expires_ns=expires_ns,
    )


def test_queue_orders_by_priority_then_arrival() -> None:
    queue = PriorityMessageQueue()
    queue.push(queued("research", Priority.RESEARCH, enqueued_ns=1))
    queue.push(queued("quote", Priority.QUOTE_REFRESH, enqueued_ns=2))
    queue.push(queued("hedge", Priority.EMERGENCY_HEDGE, enqueued_ns=3))
    queue.push(queued("hedge2", Priority.EMERGENCY_HEDGE, enqueued_ns=4))

    order = []
    while (item := queue.pop()) is not None:
        order.append(item.message_id)
    assert order == ["hedge", "hedge2", "quote", "research"]


def test_queue_expiry_drops_stale_entries() -> None:
    queue = PriorityMessageQueue()
    queue.push(queued("old", Priority.MARKET_MAKING, expires_ns=100))
    queue.push(queued("live", Priority.MARKET_MAKING, expires_ns=10**18))

    expired = queue.expire(200)
    assert [item.message_id for item in expired] == ["old"]
    assert len(queue) == 1


def test_drain_releases_in_priority_order_as_capacity_returns(
    clock: Clock,
) -> None:
    plane = build_plane(clock, venue_limit=1, global_limit=10)
    plane.evaluate(message(identifier="m0"))
    plane.evaluate(message(identifier="low", priority=Priority.RESEARCH))
    plane.evaluate(message(identifier="high", priority=Priority.MARKET_MAKING))
    assert len(plane.queue) == 2

    assert plane.drain() == ()

    clock.advance_ms(1_001)
    released = plane.drain()
    assert [item.message_id for item in released] == ["high"]

    clock.advance_ms(1_001)
    released = plane.drain()
    assert [item.message_id for item in released] == ["low"]


def test_drain_counts_expired_messages_as_dropped(clock: Clock) -> None:
    plane = build_plane(clock, venue_limit=1)
    plane.evaluate(message(identifier="m0"))
    plane.evaluate(message(identifier="m1", ttl_ms=1_100))
    assert len(plane.queue) == 1

    clock.advance_ms(1_200)
    assert plane.drain() == ()
    assert plane.venue_budgets["binance"].snapshot().dropped == 1
    assert len(plane.queue) == 0


# ---------------------------------------------------------------------
# Utilization bands
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pct", "expected"),
    [
        (Decimal(0), UtilizationBand.GREEN),
        (Decimal("59.9"), UtilizationBand.GREEN),
        (Decimal(60), UtilizationBand.YELLOW),
        (Decimal("79.9"), UtilizationBand.YELLOW),
        (Decimal(80), UtilizationBand.ORANGE),
        (Decimal("94.9"), UtilizationBand.ORANGE),
        (Decimal(95), UtilizationBand.RED),
        (Decimal(150), UtilizationBand.RED),
    ],
)
def test_band_thresholds(pct: Decimal, expected: UtilizationBand) -> None:
    assert band_for(pct) is expected


def test_band_thresholds_are_ordered_descending() -> None:
    values = [threshold for _, threshold in BAND_THRESHOLDS]
    assert values == sorted(values, reverse=True)


def test_dashboard_warns_before_the_control_binds(clock: Clock) -> None:
    plane = build_plane(clock, venue_limit=10, global_limit=100)
    for index in range(7):
        plane.evaluate(message(identifier=f"m{index}"))

    dashboard = plane.dashboard()
    assert dashboard["band"] == UtilizationBand.YELLOW.value
    assert not dashboard["binding"]
    assert dashboard["driver"] == "binance"
    assert dashboard["recommended_actions"]


def test_dashboard_reports_the_binding_control_and_its_victims(
    clock: Clock,
) -> None:
    plane = build_plane(clock, venue_limit=1)
    plane.evaluate(message(identifier="m0"))
    plane.evaluate(message(identifier="m1"))

    dashboard = plane.dashboard()
    assert dashboard["band"] == UtilizationBand.RED.value
    assert dashboard["binding"]
    assert dashboard["queued_messages"] == 1
    assert dashboard["affected_strategies"] == ["alpha"]
    assert dashboard["expected_recovery_ms"] > 0


def test_dashboard_is_json_serialisable(clock: Clock) -> None:
    import json

    plane = build_plane(clock, venue_limit=1)
    plane.evaluate(message(identifier="m0"))
    plane.evaluate(message(identifier="m1"))
    plane.kill_switch.engage(reason="halt", actor="risk-admin")
    json.dumps(plane.dashboard())


# ---------------------------------------------------------------------
# Atomic reservation
# ---------------------------------------------------------------------


def build_ledger(
    clock: Clock,
    *,
    venue_limit: int = 10,
    global_limit: int = 100,
    limits: RiskLimits | None = None,
) -> ReservationLedger:
    return ReservationLedger(
        venue_budgets={
            "binance": MessageBudget(
                name="binance",
                limit=venue_limit,
                window_ms=1000,
                now_ns=clock,
            ),
            "okx": MessageBudget(
                name="okx", limit=venue_limit, window_ms=1000, now_ns=clock
            ),
        },
        global_budget=MessageBudget(
            name="firm", limit=global_limit, window_ms=1000, now_ns=clock
        ),
        limits=limits,
    )


def reservation(
    opportunity_id: str = "OPP-1",
    *,
    binance: int = 2,
    okx: int = 2,
    hedge: int = 2,
    notional: str = "1000",
    margin: str = "100",
    quantity: str = "1",
    strategy_id: str = "alpha",
) -> ReservationRequest:
    return ReservationRequest(
        opportunity_id=opportunity_id,
        strategy_id=strategy_id,
        venue_messages={"binance": binance, "okx": okx},
        hedge_messages=hedge,
        notional=Decimal(notional),
        margin=Decimal(margin),
        position_quantity=Decimal(quantity),
    )


def test_reservation_takes_every_resource_at_once(clock: Clock) -> None:
    ledger = build_ledger(clock)
    held = ledger.reserve(reservation())

    assert held.total_messages == 6
    assert ledger.venue_budgets["binance"].available() == 8
    assert ledger.global_budget.available() == 94
    assert ledger.reserved_notional == Decimal(1000)


def test_a_failed_reservation_leaves_every_budget_untouched(
    clock: Clock,
) -> None:
    """The whole point: no half-reserved opportunity may exist."""

    ledger = build_ledger(clock, venue_limit=10, global_limit=3)
    before_binance = ledger.venue_budgets["binance"].available()
    before_okx = ledger.venue_budgets["okx"].available()
    before_global = ledger.global_budget.available()

    with pytest.raises(CapacityReservationError, match="firm"):
        ledger.reserve(reservation())

    assert ledger.venue_budgets["binance"].available() == before_binance
    assert ledger.venue_budgets["okx"].available() == before_okx
    assert ledger.global_budget.available() == before_global
    assert ledger.get("OPP-1") is None


def test_a_lost_reservation_race_rolls_back_what_reserve_already_took(
    clock: Clock,
) -> None:
    """The rollback loop is a race guard, so only a race reaches it.

    The pre-flight walk refuses an ordinary shortage before anything is
    taken, which is what makes the error name the most senior tier. That
    leaves the rollback reachable only when another thread spends between
    the read and the take -- reproduced deterministically here rather
    than hoped for.
    """

    okx = _RacingBudget("okx", limit=100, window_ms=1000, now_ns=clock)
    ledger = ReservationLedger(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=1000, now_ns=clock
            ),
            "okx": okx,
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=1000, now_ns=clock
        ),
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=100, window_ms=1000, now_ns=clock
            )
        },
    )
    with pytest.raises(CapacityReservationError, match="okx"):
        ledger.reserve(reservation())

    assert okx.refusals == 1
    assert ledger.venue_budgets["binance"].snapshot().reserved == 0
    assert ledger.global_budget.snapshot().reserved == 0
    assert ledger.strategy_budgets["alpha"].snapshot().reserved == 0
    assert ledger.get("OPP-1") is None


def test_a_second_venue_failure_releases_the_first(clock: Clock) -> None:
    ledger = build_ledger(clock)
    ledger.venue_budgets["okx"].try_reserve(10)
    before = ledger.venue_budgets["binance"].available()

    with pytest.raises(CapacityReservationError, match="okx"):
        ledger.reserve(reservation())

    assert ledger.venue_budgets["binance"].available() == before


def test_risk_limits_are_checked_before_any_capacity_is_taken(
    clock: Clock,
) -> None:
    ledger = build_ledger(
        clock, limits=RiskLimits(max_notional=Decimal(500))
    )
    before = ledger.global_budget.available()

    with pytest.raises(CapacityReservationError, match="notional"):
        ledger.reserve(reservation(notional="1000"))

    assert ledger.global_budget.available() == before


def test_margin_and_position_limits_are_enforced(clock: Clock) -> None:
    ledger = build_ledger(clock, limits=RiskLimits(max_margin=Decimal(50)))
    with pytest.raises(CapacityReservationError, match="margin"):
        ledger.reserve(reservation(margin="100"))

    ledger = build_ledger(
        clock, limits=RiskLimits(max_position_quantity=Decimal("0.5"))
    )
    with pytest.raises(CapacityReservationError, match="position"):
        ledger.reserve(reservation(quantity="1"))


def test_position_limits_accumulate_across_opportunities(
    clock: Clock,
) -> None:
    ledger = build_ledger(
        clock, limits=RiskLimits(max_position_quantity=Decimal("1.5"))
    )
    ledger.reserve(reservation("OPP-1", quantity="1"))
    with pytest.raises(CapacityReservationError, match="position"):
        ledger.reserve(reservation("OPP-2", quantity="1"))


def test_short_exposure_consumes_the_same_position_budget(
    clock: Clock,
) -> None:
    """A short leg is exposure too; sign must not cancel it out."""

    ledger = build_ledger(
        clock, limits=RiskLimits(max_position_quantity=Decimal("1.5"))
    )
    ledger.reserve(reservation("OPP-1", quantity="1"))
    with pytest.raises(CapacityReservationError):
        ledger.reserve(reservation("OPP-2", quantity="-1"))


def test_reservation_is_idempotent_per_opportunity(clock: Clock) -> None:
    ledger = build_ledger(clock)
    first = ledger.reserve(reservation())
    after = ledger.global_budget.available()
    second = ledger.reserve(reservation())

    assert second is first
    assert ledger.global_budget.available() == after


def test_release_returns_everything(clock: Clock) -> None:
    ledger = build_ledger(clock)
    before = ledger.global_budget.available()
    ledger.reserve(reservation())
    ledger.release("OPP-1")

    assert ledger.global_budget.available() == before
    assert ledger.reserved_notional == Decimal(0)
    assert ledger.reserved_margin == Decimal(0)
    assert ledger.reserved_position == Decimal(0)


def test_release_is_safe_to_call_twice(clock: Clock) -> None:
    ledger = build_ledger(clock)
    ledger.reserve(reservation())
    ledger.release("OPP-1")
    before = ledger.global_budget.available()
    ledger.release("OPP-1")
    assert ledger.global_budget.available() == before


def test_unknown_venue_is_refused_rather_than_silently_unreserved(
    clock: Clock,
) -> None:
    ledger = build_ledger(clock)
    request = ReservationRequest(
        opportunity_id="OPP-1",
        strategy_id="alpha",
        venue_messages={"binance": 1, "kraken": 1},
    )
    with pytest.raises(CapacityReservationError, match="kraken"):
        ledger.reserve(request)
    assert ledger.get("OPP-1") is None


def test_consume_spends_held_capacity_without_double_counting(
    clock: Clock,
) -> None:
    ledger = build_ledger(clock)
    ledger.reserve(reservation())
    available = ledger.venue_budgets["binance"].available()

    ledger.consume("OPP-1", "binance", 1)

    assert ledger.venue_budgets["binance"].available() == available
    assert ledger.venue_budgets["binance"].snapshot().used == 1


def test_releasing_a_spent_reservation_cannot_hand_out_capacity_twice(
    clock: Clock,
) -> None:
    """The bug this guards against overshot a hard exchange limit.

    A single holder hides it -- ``MessageBudget.release`` clamps at zero.
    It only shows with a second opportunity whose capacity is the thing
    being given away.
    """

    ledger = build_ledger(clock, venue_limit=10, global_limit=1000)
    ledger.reserve(reservation("OPP-1", binance=3, okx=0, hedge=0))
    ledger.reserve(reservation("OPP-2", binance=4, okx=0, hedge=0))
    budget = ledger.venue_budgets["binance"]
    assert budget.available() == 3

    for _ in range(3):
        ledger.consume("OPP-1", "binance", 1)
    ledger.release("OPP-1")

    assert budget.available() == 3

    ledger.reserve(reservation("OPP-3", binance=3, okx=0, hedge=0))
    for _ in range(4):
        ledger.consume("OPP-2", "binance", 1)
    for _ in range(3):
        ledger.consume("OPP-3", "binance", 1)

    assert budget.snapshot().used == 10


def test_a_reservation_cannot_outspend_what_it_holds(clock: Clock) -> None:
    ledger = build_ledger(clock)
    ledger.reserve(reservation("OPP-1", binance=2, okx=0, hedge=0))
    ledger.consume("OPP-1", "binance", 2)

    with pytest.raises(CapacityReservationError, match="reserved messages"):
        ledger.consume("OPP-1", "binance", 1)


def test_a_reservation_cannot_spend_another_venues_capacity(
    clock: Clock,
) -> None:
    ledger = build_ledger(clock)
    ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            venue_messages={"binance": 2},
        )
    )
    okx_before = ledger.venue_budgets["okx"].available()

    with pytest.raises(CapacityReservationError, match="reserved no capacity"):
        ledger.consume("OPP-1", "okx", 1)

    assert ledger.venue_budgets["okx"].available() == okx_before


def test_hedge_capacity_survives_a_sibling_release(clock: Clock) -> None:
    ledger = build_ledger(clock, venue_limit=10, global_limit=8)
    ledger.reserve(reservation("OPP-1", binance=1, okx=1, hedge=2))
    ledger.reserve(reservation("OPP-2", binance=1, okx=1, hedge=2))

    ledger.consume("OPP-1", "binance", 1)
    ledger.release("OPP-1")

    # OPP-2 still holds its two legs plus two hedge messages.
    assert ledger.global_budget.available() == 3
    ledger.consume("OPP-2", "binance", 1)
    ledger.consume("OPP-2", "okx", 1)


def test_consume_without_a_reservation_is_an_error(clock: Clock) -> None:
    ledger = build_ledger(clock)
    with pytest.raises(CapacityReservationError):
        ledger.consume("OPP-missing", "binance", 1)


def test_hedge_capacity_is_reserved_up_front(clock: Clock) -> None:
    """A hedge must never be the message that cannot be sent."""

    ledger = build_ledger(clock, global_limit=6)
    ledger.reserve(reservation(binance=2, okx=2, hedge=2))
    assert ledger.global_budget.available() == 0

    with pytest.raises(CapacityReservationError):
        ledger.reserve(reservation("OPP-2", binance=1, okx=1, hedge=0))


# ---------------------------------------------------------------------
# Control hierarchy
# ---------------------------------------------------------------------


def test_control_levels_are_ordered_most_senior_first() -> None:
    assert list(ControlLevel) == sorted(ControlLevel, key=int)
    assert ControlLevel.FIRM_RISK < ControlLevel.GLOBAL_KILL_SWITCH
    assert ControlLevel.GLOBAL_KILL_SWITCH < ControlLevel.FIRM_MESSAGE_LIMIT
    assert ControlLevel.FIRM_MESSAGE_LIMIT < ControlLevel.PM_LIMIT
    assert ControlLevel.PM_LIMIT < ControlLevel.STRATEGY_LIMIT
    assert ControlLevel.STRATEGY_LIMIT < ControlLevel.VENUE_LIMIT
    assert ControlLevel.VENUE_LIMIT < ControlLevel.INSTRUMENT_LIMIT
    assert ControlLevel.INSTRUMENT_LIMIT < ControlLevel.ORDER


def test_evaluation_order_follows_the_declared_hierarchy() -> None:
    """The precedence table must not drift from the level ordinals.

    The kill switch is first by definition -- the specification makes it
    absolute -- and everything after it must be in ascending level order,
    or the control that decides an outcome would not be the most senior
    one that bound.
    """

    kinds = [kind for kind, _ in CONTROL_PRECEDENCE]
    assert set(kinds) == set(ControlKind)
    assert kinds[0] is ControlKind.KILL_SWITCH

    # A kind may appear at several levels -- a throttle is a throttle
    # whether the exhausted budget belongs to the firm, a portfolio
    # manager or one strategy -- but each (kind, level) pair is unique
    # and the levels after the kill switch strictly ascend, so the most
    # senior control that binds is always the one that decides.
    pairs = list(CONTROL_PRECEDENCE)
    assert len(pairs) == len(set(pairs))
    levels = [level for _, level in CONTROL_PRECEDENCE[1:]]
    assert levels == sorted(levels, key=int)
    assert len(levels) == len(set(levels))

    # Every declared level must be reachable, or the published hierarchy
    # would promise authority tiers that decide nothing. ORDER is the
    # exception: it is checked before the walk, not inside it.
    declared = {level for _, level in CONTROL_PRECEDENCE}
    assert declared == set(ControlLevel) - {ControlLevel.ORDER}


def test_the_kill_switch_decides_when_it_and_a_stale_feed_both_bind(
    clock: Clock,
) -> None:
    plane = build_plane(clock, stale_after_ms=100)
    clock.advance_ms(250)
    plane.kill_switch.engage(reason="halt", actor="risk-admin")

    decision = plane.evaluate(message(OrderStage.RESTING))

    assert decision.control is ControlKind.KILL_SWITCH
    assert decision.level is ControlLevel.GLOBAL_KILL_SWITCH
    # A firm-wide halt pulls the whole book, not one resting order.
    assert decision.action is ControlAction.MASS_CANCEL


def test_a_stale_feed_decides_over_an_exhausted_firm_budget(
    clock: Clock,
) -> None:
    """The senior control must both decide and be named in the audit."""

    plane = build_plane(clock, venue_limit=1, global_limit=1)
    plane.evaluate(message(identifier="m0"))
    clock.advance_ms(250)

    decision = plane.evaluate(message(identifier="m1"))

    assert decision.control is ControlKind.STALE_FEED
    assert decision.level is ControlLevel.FIRM_RISK
    assert decision.action is ControlAction.REJECT
    assert len(plane.queue) == 0


def test_the_firm_budget_decides_over_an_exhausted_venue_budget(
    clock: Clock,
) -> None:
    plane = build_plane(clock, venue_limit=1, global_limit=1)
    plane.evaluate(message(identifier="m0"))

    decision = plane.evaluate(message(identifier="m1"))

    assert decision.control is ControlKind.GLOBAL_THROTTLE
    assert decision.level is ControlLevel.FIRM_MESSAGE_LIMIT


def test_priorities_are_ordered_with_liquidation_first() -> None:
    assert list(Priority) == sorted(Priority, key=int)
    assert Priority.FORCED_LIQUIDATION < Priority.EMERGENCY_HEDGE
    assert Priority.EMERGENCY_HEDGE < Priority.RISK_REDUCING
    assert Priority.RISK_REDUCING < Priority.MARKET_MAKING
    assert Priority.MARKET_MAKING < Priority.QUOTE_REFRESH
    assert Priority.QUOTE_REFRESH < Priority.RESEARCH


def test_decisions_are_recorded_for_audit(clock: Clock) -> None:
    plane = build_plane(clock, venue_limit=1)
    plane.evaluate(message(identifier="m0"))
    plane.evaluate(message(identifier="m1"))

    decisions = plane.decisions()
    assert len(decisions) == 2
    assert decisions[0].action is ControlAction.ADMIT
    assert decisions[1].action is ControlAction.QUEUE
    assert decisions[1].to_dict()["control"] == ControlKind.VENUE_RATE_LIMIT.value


def test_mass_cancel_scope_narrows_from_account_to_instrument(
    clock: Clock,
) -> None:
    plane = build_plane(clock)
    assert plane.mass_cancel_scope()["scope"] == "account"
    assert plane.mass_cancel_scope(strategy_id="alpha")["scope"] == "strategy"
    assert plane.mass_cancel_scope(venue="binance")["scope"] == "venue"
    assert (
        plane.mass_cancel_scope(instrument="BTCUSDT")["scope"] == "instrument"
    )


def test_a_kill_switch_with_no_administrators_is_refused() -> None:
    """Otherwise engaging it would be a permanent, unrecoverable halt."""

    with pytest.raises(ValueError, match="never be"):
        KillSwitch(administrators=())


def test_the_default_kill_switch_can_be_cleared(clock: Clock) -> None:
    plane = build_plane(clock)
    plane.kill_switch.engage(reason="halt", actor="trader")
    plane.kill_switch.clear(actor="risk-admin")
    assert not plane.kill_switch.active


# ---------------------------------------------------------------------
# Regressions from the control-plane review
# ---------------------------------------------------------------------


def test_a_venue_name_in_mixed_case_still_hits_its_budget(
    clock: Clock,
) -> None:
    """Venue identifiers arrive case-folded from the adapter layer.

    A missed lookup used to be indistinguishable from "no limit
    configured", which silently switched the control off.
    """

    plane = build_plane(clock, venue_limit=1)
    first = plane.evaluate(message(identifier="m0", venue="Binance"))
    second = plane.evaluate(message(identifier="m1", venue="BINANCE"))

    assert first.action is ControlAction.ADMIT
    assert second.action is not ControlAction.ADMIT
    assert plane.venue_budgets["binance"].snapshot().used == 1


def test_an_unknown_venue_is_rejected_rather_than_waved_through(
    clock: Clock,
) -> None:
    plane = build_plane(clock)
    # Give the venue a fresh feed so the more senior stale-feed control
    # does not bind first; the missing budget must be what catches it.
    plane.feed_monitor.record_tick("kraken")

    decision = plane.evaluate(message(identifier="m0", venue="kraken"))

    assert decision.action is ControlAction.REJECT
    assert decision.control is ControlKind.VENUE_RATE_LIMIT
    assert "kraken" in decision.reason


def test_an_unknown_venue_with_no_feed_is_still_rejected(
    clock: Clock,
) -> None:
    plane = build_plane(clock)
    assert plane.evaluate(
        message(identifier="m0", venue="kraken")
    ).action is ControlAction.REJECT


def test_duplicate_venue_budgets_are_refused(clock: Clock) -> None:
    with pytest.raises(ValueError, match="duplicate message budget"):
        ExecutionControlPlane(
            venue_budgets={
                "binance": MessageBudget(
                    name="binance", limit=1, window_ms=1000, now_ns=clock
                ),
                "Binance": MessageBudget(
                    name="binance", limit=1, window_ms=1000, now_ns=clock
                ),
            },
            global_budget=MessageBudget(
                name="firm", limit=1, window_ms=1000, now_ns=clock
            ),
            now_ns=clock,
        )


@pytest.mark.concurrency
def test_concurrent_drains_never_transmit_the_same_message_twice(
    clock: Clock,
) -> None:
    """A duplicate here is a duplicate order at the exchange."""

    import sys
    import threading

    plane = build_plane(clock, venue_limit=10**6, global_limit=10**6)
    total = 400
    for index in range(total):
        plane.queue.push(
            QueuedMessage(
                message_id=f"m{index}",
                strategy_id="alpha",
                venue="binance",
                stage=OrderStage.NEW,
                priority=Priority.MARKET_MAKING,
                messages=1,
                enqueued_ns=clock.ns,
                expires_ns=clock.ns + 10**12,
                budget_key="binance",
            )
        )

    released: list[QueuedMessage] = []
    guard = threading.Lock()

    def drain() -> None:
        batch = plane.drain()
        with guard:
            released.extend(batch)

    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=drain) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(previous)

    identifiers = [item.message_id for item in released]
    assert len(plane.queue) == 0
    assert len(identifiers) == total
    assert len(set(identifiers)) == total


def test_a_saturated_venue_does_not_stall_a_healthy_one(
    clock: Clock,
) -> None:
    """Head-of-line blocking across venues would idle the whole firm."""

    plane = build_plane(clock, venue_limit=1, global_limit=100)
    plane.venue_budgets["binance"].force_consume(1)
    for identifier, venue in (("bnb", "binance"), ("okx", "okx")):
        plane.queue.push(
            QueuedMessage(
                message_id=identifier,
                strategy_id="alpha",
                venue=venue,
                stage=OrderStage.NEW,
                priority=Priority.MARKET_MAKING,
                messages=1,
                enqueued_ns=clock.ns,
                expires_ns=clock.ns + 10**12,
                budget_key=venue,
            )
        )

    released = plane.drain()

    assert [item.message_id for item in released] == ["okx"]
    assert len(plane.queue) == 1


def test_queue_counters_settle_on_the_budget_that_recorded_them(
    clock: Clock,
) -> None:
    """A firm-throttle queue cleared off a venue budget grows forever."""

    plane = build_plane(clock, venue_limit=100, global_limit=1)
    plane.evaluate(message(identifier="m0"))
    plane.evaluate(message(identifier="m1"))

    assert plane.global_budget.snapshot().queued == 1
    assert plane.venue_budgets["binance"].snapshot().queued == 0

    clock.advance_ms(1_001)
    assert [item.message_id for item in plane.drain()] == ["m1"]
    assert plane.global_budget.snapshot().queued == 0
    assert plane.venue_budgets["binance"].snapshot().queued == 0


def test_an_expired_firm_queue_entry_is_dropped_against_the_firm_budget(
    clock: Clock,
) -> None:
    plane = build_plane(clock, venue_limit=100, global_limit=1)
    plane.evaluate(message(identifier="m0"))
    plane.evaluate(message(identifier="m1", ttl_ms=1_100))

    clock.advance_ms(1_200)
    assert plane.drain() == ()
    assert plane.global_budget.snapshot().dropped == 1
    assert plane.venue_budgets["binance"].snapshot().dropped == 0


def test_expected_recovery_reflects_the_binding_budget(clock: Clock) -> None:
    """A venue window says nothing about when the firm window refills."""

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=100, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=2, window_ms=10_000, now_ns=clock
        ),
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    for index in range(3):
        plane.evaluate(message(identifier=f"m{index}", ttl_ms=60_000))

    dashboard = plane.dashboard()
    assert dashboard["driver"] == "firm"
    assert dashboard["expected_recovery_ms"] == 10_000


def test_the_decision_log_is_bounded(clock: Clock) -> None:
    """evaluate() is the hot path; an unbounded audit list never frees."""

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=10**6, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=10**6, window_ms=1000, now_ns=clock
        ),
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        decision_history=8,
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    for index in range(50):
        plane.evaluate(message(identifier=f"m{index}"))

    decisions = plane.decisions()
    assert len(decisions) == 8


def test_every_stage_exempt_from_a_capacity_control_is_risk_reducing() -> None:
    """Keeps the matrix and the exemption set from drifting apart.

    If a stage were exempted in the matrix but not treated as
    risk-reducing, it would reach the capacity path with no action to
    apply.
    """

    for control in (ControlKind.VENUE_RATE_LIMIT, ControlKind.GLOBAL_THROTTLE):
        for stage, row in CONTROL_MATRIX.items():
            action = row[control]
            if action is None or action is ControlAction.ADMIT:
                assert stage in RISK_REDUCING_STAGES, (stage, control)


# ---------------------------------------------------------------------
# Regressions from the second control-plane review
# ---------------------------------------------------------------------


@pytest.mark.concurrency
def test_admission_spends_capacity_without_a_second_decision() -> None:
    """Admission must be compare-and-increment, not check-then-act.

    Deciding by reading the remaining allowance and then spending it
    with an unchecked increment lets two threads that both see one slot
    left both send. At an exchange that is a rate-limit breach.

    Read what this does and does not hold. It pins the *ordering* --
    that the decision and the spend are not two decisions with a gap
    between them -- and restoring the original defect is caught here
    and nowhere else in the suite. It does not pin the lock inside
    ``try_reserve``: with every acquisition in the module removed this
    test still passes, because what is left is a window a few bytecodes
    wide that sampling cannot reach. The lock itself is held by
    ``test_two_threads_cannot_both_take_the_last_message_slot``, which
    forces the interleaving instead of hoping for it. Neither test
    subsumes the other and deleting either reopens a real defect.
    """

    import sys
    import threading

    limit = 50

    def one_trial() -> tuple[int, int]:
        plane = ExecutionControlPlane(
            venue_budgets={
                "binance": MessageBudget(
                    name="binance", limit=limit, window_ms=100_000
                )
            },
            global_budget=MessageBudget(
                name="firm", limit=10**9, window_ms=100_000
            ),
            feed_monitor=FeedMonitor(stale_after_ms=10**9),
        )
        plane.feed_monitor.record_tick("binance")
        admitted: list[int] = []
        guard = threading.Lock()

        def worker(worker_id: int) -> None:
            count = 0
            for index in range(40):
                decision = plane.evaluate(
                    MessageRequest(
                        message_id=f"{worker_id}-{index}",
                        strategy_id="alpha",
                        venue="binance",
                        stage=OrderStage.NEW,
                        priority=Priority.MARKET_MAKING,
                    )
                )
                if decision.action is ControlAction.ADMIT:
                    count += 1
            with guard:
                admitted.append(count)

        threads = [
            threading.Thread(target=worker, args=(w,)) for w in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return sum(admitted), plane.venue_budgets["binance"].snapshot().used

    # A single trial races only sometimes, which would make this test
    # pass against a broken implementation more often than not.
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        results = [one_trial() for _ in range(15)]
    finally:
        sys.setswitchinterval(previous)

    assert max(count for count, _ in results) <= limit
    assert max(used for _, used in results) <= limit

def test_an_unknown_venue_cannot_escape_through_the_firm_queue(
    clock: Clock,
) -> None:
    """The firm throttle is evaluated first, so it must not shadow the
    venue check: a message queued against the firm budget used to drain
    with no venue limit applied at all."""

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=1, window_ms=10_000, now_ns=clock
        ),
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    for venue in ("binance", "kraken"):
        plane.feed_monitor.record_tick(venue)
    plane.evaluate(message(identifier="m0"))

    decision = plane.evaluate(
        message(identifier="m1", venue="kraken", ttl_ms=60_000)
    )

    assert decision.action is ControlAction.REJECT
    assert decision.control is ControlKind.VENUE_RATE_LIMIT
    clock.advance_ms(20_000)
    assert plane.drain() == ()


def test_a_shared_firm_budget_is_not_handed_to_lower_priority_traffic(
    clock: Clock,
) -> None:
    """Scanning past a venue-blocked message is only safe when the
    blocker is that venue's own budget. Every queued message draws on
    the firm budget, so scanning past when *it* is short would invert
    the priority ordering the queue exists to enforce."""

    plane = ExecutionControlPlane(
        venue_budgets={
            "a": MessageBudget(name="a", limit=99, window_ms=100, now_ns=clock),
            "b": MessageBudget(name="b", limit=99, window_ms=100, now_ns=clock),
        },
        global_budget=MessageBudget(
            name="firm", limit=4, window_ms=10_000, now_ns=clock
        ),
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    for venue in ("a", "b"):
        plane.feed_monitor.record_tick(venue)
    # Two firm slots left -- enough for the small low-priority message
    # but not for the large high-priority one ahead of it.
    plane.global_budget.force_consume(2)
    for identifier, venue, priority, size in (
        ("mm-a", "a", Priority.MARKET_MAKING, 5),
        ("research-b", "b", Priority.RESEARCH, 1),
    ):
        plane.queue.push(
            QueuedMessage(
                message_id=identifier,
                strategy_id="alpha",
                venue=venue,
                stage=OrderStage.NEW,
                priority=priority,
                messages=size,
                enqueued_ns=clock.ns,
                expires_ns=clock.ns + 10**12,
                budget_key=venue,
            )
        )

    assert plane.global_budget.available() == 2
    assert plane.venue_budgets["a"].available() == 99

    # Neither venue budget is the blocker, so any release here would be
    # the firm's last slots going to RESEARCH ahead of MARKET_MAKING.
    assert plane.drain() == ()
    assert len(plane.queue) == 2
    assert plane.global_budget.available() == 2


def test_counters_settle_by_key_not_display_name(clock: Clock) -> None:
    """Two venues may legitimately share a display name."""

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance-spot": MessageBudget(
                name="binance", limit=1, window_ms=1000, now_ns=clock
            ),
            "binance-futures": MessageBudget(
                name="binance", limit=1, window_ms=1000, now_ns=clock
            ),
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=1000, now_ns=clock
        ),
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    for venue in ("binance-spot", "binance-futures"):
        plane.feed_monitor.record_tick(venue)
    for index in range(2):
        plane.evaluate(
            message(
                identifier=f"f{index}",
                venue="binance-futures",
                ttl_ms=60_000,
            )
        )

    assert plane.venue_budgets["binance-futures"].snapshot().queued == 1
    assert plane.venue_budgets["binance-spot"].snapshot().queued == 0

    clock.advance_ms(1_100)
    plane.drain()

    assert plane.venue_budgets["binance-futures"].snapshot().queued == 0
    assert plane.venue_budgets["binance-spot"].snapshot().queued == 0


def test_reserved_hedge_capacity_can_actually_be_spent(
    clock: Clock,
) -> None:
    """Requirement 8 reserves hedge capacity so it is there when needed.

    Capacity that is reserved but unreachable is bookkeeping, not a
    control: the hedge would find the budget "full" at the one moment it
    is guaranteed to be needed.
    """

    ledger = ReservationLedger(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=1000, now_ns=clock
        ),
    )
    ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            venue_messages={"binance": 2},
            hedge_messages=2,
        )
    )
    assert ledger.global_budget.snapshot().reserved == 4

    ledger.consume_hedge("OPP-1", "binance", 2)

    with pytest.raises(CapacityReservationError, match="hedge"):
        ledger.consume_hedge("OPP-1", "binance", 1)

    ledger.release("OPP-1")
    assert ledger.global_budget.snapshot().reserved == 0
    # Two leg messages were never sent and come back; the two hedge
    # messages were spent and must not.
    assert ledger.global_budget.snapshot().used == 2


def test_hedge_spend_requires_a_live_reservation(clock: Clock) -> None:
    ledger = ReservationLedger(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=1000, now_ns=clock
        ),
    )
    with pytest.raises(CapacityReservationError, match="holds no reservation"):
        ledger.consume_hedge("OPP-nope", "binance", 1)


@pytest.mark.parametrize("messages", [0, -1, -50])
def test_a_request_cannot_carry_a_non_positive_message_count(
    messages: int,
) -> None:
    """A negative count would *credit* the budget when it transmits,
    handing a strategy a way to manufacture capacity."""

    with pytest.raises(ValueError, match="at least 1 message"):
        MessageRequest(
            message_id="x",
            strategy_id="alpha",
            venue="binance",
            stage=OrderStage.NEW,
            messages=messages,
        )


# ---------------------------------------------------------------------
# Scoped authority tiers: PM, strategy, instrument and per-order
#
# The published hierarchy declares eight levels. Four of them decided
# nothing until these budgets existed, so the precedence table promised
# authority it did not have.
# ---------------------------------------------------------------------


def _tiered_plane(clock: Clock, **kwargs: object) -> ExecutionControlPlane:
    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=1000, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=1000, window_ms=1000, now_ns=clock
        ),
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
        **kwargs,
    )
    plane.feed_monitor.record_tick("binance")
    return plane


def _ask(
    plane: ExecutionControlPlane,
    *,
    strategy_id: str = "alpha",
    instrument: str = "",
    messages: int = 1,
    stage: OrderStage = OrderStage.NEW,
    priority: Priority = Priority.MARKET_MAKING,
) -> ControlDecision:
    return plane.evaluate(
        MessageRequest(
            message_id="m",
            strategy_id=strategy_id,
            venue="binance",
            stage=stage,
            priority=priority,
            messages=messages,
            instrument=instrument,
        )
    )


def test_a_strategy_budget_binds_at_strategy_level(clock: Clock) -> None:
    """One strategy must not be able to spend the whole firm budget."""

    plane = _tiered_plane(
        clock,
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=2, window_ms=100, now_ns=clock
            )
        },
    )
    assert _ask(plane).action is ControlAction.ADMIT
    assert _ask(plane).action is ControlAction.ADMIT
    blocked = _ask(plane)
    assert blocked.action is ControlAction.QUEUE
    assert blocked.level is ControlLevel.STRATEGY_LIMIT
    assert blocked.control is ControlKind.GLOBAL_THROTTLE
    # A strategy with no budget of its own is unaffected by alpha's.
    assert _ask(plane, strategy_id="beta").action is ControlAction.ADMIT


def test_a_portfolio_manager_budget_binds_above_its_strategies(
    clock: Clock,
) -> None:
    """PM limits aggregate the strategies reporting to them."""

    plane = _tiered_plane(
        clock,
        pm_budgets={
            "pm-1": MessageBudget(
                name="pm-1", limit=2, window_ms=1000, now_ns=clock
            )
        },
        strategy_pm={"alpha": "pm-1", "beta": "pm-1"},
    )
    assert _ask(plane, strategy_id="alpha").action is ControlAction.ADMIT
    assert _ask(plane, strategy_id="beta").action is ControlAction.ADMIT
    # Two different strategies drew on one shared allowance.
    blocked = _ask(plane, strategy_id="alpha")
    assert blocked.level is ControlLevel.PM_LIMIT
    assert plane.pm_budgets["pm-1"].snapshot().used == 2


def test_an_instrument_budget_binds_at_instrument_level(
    clock: Clock,
) -> None:
    plane = _tiered_plane(
        clock,
        instrument_budgets={
            "BTCUSDT": MessageBudget(
                name="BTCUSDT", limit=1, window_ms=1000, now_ns=clock
            )
        },
    )
    assert _ask(plane, instrument="BTCUSDT").action is ControlAction.ADMIT
    blocked = _ask(plane, instrument="btcusdt")
    assert blocked.level is ControlLevel.INSTRUMENT_LIMIT
    # A request that names no instrument does not touch the tier.
    assert _ask(plane).action is ControlAction.ADMIT
    # An instrument with no configured budget is not silently limited.
    assert _ask(plane, instrument="ETHUSDT").action is ControlAction.ADMIT


def test_the_per_order_ceiling_rejects_before_the_walk(
    clock: Clock,
) -> None:
    """An oversized order can never fit, so queueing it would be a lie."""

    plane = _tiered_plane(clock, max_messages_per_order=3)
    assert _ask(plane, messages=3).action is ControlAction.ADMIT
    refused = _ask(plane, messages=4)
    assert refused.action is ControlAction.REJECT
    assert refused.level is ControlLevel.ORDER
    assert refused.retry_after_ms is None
    assert len(plane.queue) == 0
    # It is checked before the walk, so even a control that would have
    # queued the message cannot override it.
    plane.kill_switch.engage(actor="ops", reason="drill")
    assert _ask(plane, messages=4).level is ControlLevel.ORDER


def test_a_ceiling_below_one_message_is_refused(clock: Clock) -> None:
    with pytest.raises(ValueError):
        _tiered_plane(clock, max_messages_per_order=0)


def test_a_strategy_mapped_to_an_unbudgeted_manager_is_refused(
    clock: Clock,
) -> None:
    """Silently skipping the PM tier would be worse than failing loudly."""

    with pytest.raises(ValueError, match="no budget"):
        _tiered_plane(clock, strategy_pm={"alpha": "pm-ghost"})


def test_the_most_senior_exhausted_tier_decides(clock: Clock) -> None:
    """Firm outranks PM, which outranks strategy, which outranks venue."""

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=1, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=1, window_ms=1000, now_ns=clock
        ),
        pm_budgets={
            "pm-1": MessageBudget(
                name="pm-1", limit=1, window_ms=1000, now_ns=clock
            )
        },
        strategy_pm={"alpha": "pm-1"},
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=1, window_ms=1000, now_ns=clock
            )
        },
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    # Exhaust every tier at once.
    assert _ask(plane).action is ControlAction.ADMIT
    assert _ask(plane).level is ControlLevel.FIRM_MESSAGE_LIMIT

    # With only the strategy tier short, the strategy tier decides.
    plane.global_budget.limit = 1000
    plane.pm_budgets["pm-1"].limit = 1000
    assert _ask(plane).level is ControlLevel.STRATEGY_LIMIT


class _RacingBudget(MessageBudget):
    """A budget that loses exactly one reservation race.

    The precedence walk reads every tier before spending any of it, so
    the two only disagree when another thread spends in between. Rather
    than hope a real race lands, this reproduces it deterministically:
    the read says there is room and the reservation then fails.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.refusals = 0

    def try_reserve(self, messages: int) -> bool:
        if self.refusals == 0:
            self.refusals += 1
            return False
        return super().try_reserve(messages)


def test_a_lost_reservation_race_rolls_back_the_tiers_above_it(
    clock: Clock,
) -> None:
    """A partial take would spend allowances on a message never sent.

    Losing the venue race after the firm and strategy tiers were already
    reserved must return both, or every lost race permanently burns
    capacity that no message ever used.
    """

    venue = _RacingBudget(
        "binance", limit=100, window_ms=1000, now_ns=clock
    )
    plane = ExecutionControlPlane(
        venue_budgets={"binance": venue},
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=1000, now_ns=clock
        ),
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=100, window_ms=1000, now_ns=clock
            )
        },
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")

    decision = _ask(plane)
    assert venue.refusals == 1
    assert decision.action is not ControlAction.ADMIT
    assert decision.level is ControlLevel.VENUE_LIMIT
    # Neither senior tier may keep anything from the lost race.
    for budget in (plane.global_budget, plane.strategy_budgets["alpha"]):
        snapshot = budget.snapshot()
        assert snapshot.used == 0, budget.name
        assert snapshot.reserved == 0, budget.name
        assert budget.available() == 100, budget.name

    # And the next message, which wins, still gets full capacity.
    assert _ask(plane).action is ControlAction.ADMIT
    assert plane.global_budget.snapshot().used == 1


def test_a_refused_tier_does_not_drain_the_tiers_above_it(
    clock: Clock,
) -> None:
    """A partial take would spend allowances on a message never sent."""

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=1, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=1000, now_ns=clock
        ),
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=100, window_ms=1000, now_ns=clock
            )
        },
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    plane.venue_budgets["binance"].force_consume(1)
    decision = _ask(plane)
    assert decision.level is ControlLevel.VENUE_LIMIT
    firm = plane.global_budget.snapshot()
    strategy = plane.strategy_budgets["alpha"].snapshot()
    assert firm.used == 0 and firm.reserved == 0
    assert strategy.used == 0 and strategy.reserved == 0


def test_exempt_traffic_still_spends_every_configured_tier(
    clock: Clock,
) -> None:
    """Exemption means "never blocked", not "never counted".

    A hedge that spent nothing would leave the dashboard understating
    real usage exactly when the firm most needs it to be accurate.
    """

    plane = _tiered_plane(
        clock,
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=1, window_ms=1000, now_ns=clock
            )
        },
        instrument_budgets={
            "BTCUSDT": MessageBudget(
                name="BTCUSDT", limit=1, window_ms=1000, now_ns=clock
            )
        },
    )
    plane.strategy_budgets["alpha"].force_consume(1)
    decision = _ask(
        plane,
        instrument="BTCUSDT",
        stage=OrderStage.EMERGENCY_HEDGE,
        priority=Priority.EMERGENCY_HEDGE,
    )
    assert decision.action is ControlAction.ADMIT
    assert plane.strategy_budgets["alpha"].snapshot().used == 2
    assert plane.instrument_budgets["btcusdt"].snapshot().used == 1


def test_a_queued_message_rechecks_its_scoped_tiers_on_drain(
    clock: Clock,
) -> None:
    """Draining must apply the same tiers admission applied."""

    plane = _tiered_plane(
        clock,
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=1, window_ms=100, now_ns=clock
            )
        },
    )
    assert _ask(plane).action is ControlAction.ADMIT
    queued = _ask(plane)
    assert queued.action is ControlAction.QUEUE
    assert queued.level is ControlLevel.STRATEGY_LIMIT
    assert len(plane.queue) == 1

    # The strategy window has not rolled over yet.
    assert plane.drain() == ()
    clock.advance_ms(150)
    released = plane.drain()
    assert [message.message_id for message in released] == ["m"]
    assert plane.strategy_budgets["alpha"].snapshot().queued == 0


def test_scoped_tier_counters_settle_on_the_scoped_budget(
    clock: Clock,
) -> None:
    """Queue depth recorded on a strategy must clear off that strategy."""

    plane = _tiered_plane(
        clock,
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=1, window_ms=50, now_ns=clock
            )
        },
    )
    plane.strategy_budgets["alpha"].force_consume(1)
    decision = _ask(plane)
    assert decision.action is ControlAction.QUEUE
    assert plane.strategy_budgets["alpha"].snapshot().queued == 1
    assert plane.global_budget.snapshot().queued == 0

    clock.advance_ms(10_000)
    plane.drain()
    strategy = plane.strategy_budgets["alpha"].snapshot()
    assert strategy.queued == 0
    assert strategy.dropped == 1
    assert plane.global_budget.snapshot().dropped == 0


def test_unconfigured_tiers_never_bind(clock: Clock) -> None:
    """The four scoped tiers are opt-in and default to absent."""

    plane = _tiered_plane(clock)
    for _ in range(50):
        assert _ask(plane, instrument="ANYTHING").action is ControlAction.ADMIT
    assert plane.pm_budgets == {}
    assert plane.strategy_budgets == {}
    assert plane.instrument_budgets == {}
    assert plane.max_messages_per_order is None


def test_a_lost_race_while_draining_rolls_back_the_tiers_above_it(
    clock: Clock,
) -> None:
    """The drain path needs the same all-or-nothing guarantee.

    A queued message that reserves the firm and strategy tiers and then
    loses the venue must hand both back. Otherwise every failed drain
    attempt burns firm capacity, and a message that keeps losing burns it
    on every pass -- the queue would throttle the firm by existing.
    """

    venue = _RacingBudget("binance", limit=100, window_ms=50, now_ns=clock)
    plane = ExecutionControlPlane(
        venue_budgets={"binance": venue},
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=10_000, now_ns=clock
        ),
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=1, window_ms=50, now_ns=clock
            )
        },
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    plane.strategy_budgets["alpha"].force_consume(1)

    queued = _ask(plane)
    assert queued.action is ControlAction.QUEUE
    assert queued.level is ControlLevel.STRATEGY_LIMIT
    assert venue.refusals == 0, "the venue was never asked yet"

    clock.advance_ms(60)
    # The strategy window has rolled, so the drain gets as far as the
    # venue -- and loses.
    assert plane.drain() == ()
    assert venue.refusals == 1
    assert len(plane.queue) == 1
    firm = plane.global_budget.snapshot()
    assert firm.used == 0 and firm.reserved == 0
    assert plane.global_budget.available() == 100
    strategy = plane.strategy_budgets["alpha"].snapshot()
    assert strategy.reserved == 0

    # The next pass wins, and spends each tier exactly once.
    released = plane.drain()
    assert [message.message_id for message in released] == ["m"]
    assert plane.global_budget.snapshot().used == 1
    assert venue.snapshot().used == 1
    assert plane.strategy_budgets["alpha"].snapshot().used == 1


# ---------------------------------------------------------------------
# Reserved capacity is drawn down, not taken a second time
# ---------------------------------------------------------------------


def _reserving_plane(clock: Clock, **kwargs: object) -> ExecutionControlPlane:
    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=10, window_ms=100_000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=100_000, now_ns=clock
        ),
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
        **kwargs,
    )
    plane.feed_monitor.record_tick("binance")
    return plane


def _send(
    plane: ExecutionControlPlane,
    *,
    opportunity_id: str | None = "OPP-1",
    messages: int = 1,
    stage: OrderStage = OrderStage.NEW,
    priority: Priority = Priority.MARKET_MAKING,
    strategy_id: str = "alpha",
    instrument: str = "",
    venue: str = "binance",
    ttl_ms: int | None = None,
) -> ControlDecision:
    request = MessageRequest(
        message_id="m",
        strategy_id=strategy_id,
        venue=venue,
        stage=stage,
        priority=priority,
        messages=messages,
        opportunity_id=opportunity_id,
        instrument=instrument,
        **({} if ttl_ms is None else {"ttl_ms": ttl_ms}),
    )
    return plane.evaluate(request)


def test_a_reserved_message_is_not_charged_twice(clock: Clock) -> None:
    """Reserving three and sending three must cost three, not six.

    The reservation already moved the capacity out of availability. If
    transmission then took it again, an opportunity would consume double
    its own budget and the firm would run out at half its stated limit.
    """

    plane = _reserving_plane(clock)
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            venue_messages={"binance": 3},
        )
    )
    binance = plane.venue_budgets["binance"]
    assert binance.available() == 7

    for _ in range(3):
        decision = _send(plane)
        assert decision.action is ControlAction.ADMIT
        assert decision.reason == "reserved_venue_capacity"
        # Availability does not move: it was already accounted for.
        assert binance.available() == 7

    snapshot = binance.snapshot()
    assert snapshot.used == 3
    assert snapshot.reserved == 0


def test_a_reservation_cannot_throttle_the_message_it_reserved_for(
    clock: Clock,
) -> None:
    """Its own reservation must not be what blocks it.

    ``available()`` counts reserved capacity as unavailable, so an
    opportunity that reserved the whole venue budget would read zero
    availability and queue behind itself.
    """

    plane = _reserving_plane(clock)
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            venue_messages={"binance": 10},
        )
    )
    assert plane.venue_budgets["binance"].available() == 0
    for _ in range(10):
        assert _send(plane).action is ControlAction.ADMIT
    assert plane.venue_budgets["binance"].snapshot().used == 10


def test_sending_beyond_the_reservation_falls_back_to_the_controls(
    clock: Clock,
) -> None:
    """The exemption covers what was reserved, and nothing more."""

    plane = _reserving_plane(clock)
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            venue_messages={"binance": 1},
        )
    )
    assert _send(plane).reason == "reserved_venue_capacity"
    # The reservation is spent, so the next message is charged normally.
    assert _send(plane).reason == "admitted"
    assert plane.venue_budgets["binance"].snapshot().used == 2


def test_a_reserved_message_still_obeys_the_kill_switch(
    clock: Clock,
) -> None:
    """Reserved capacity is a capacity exemption, not a risk exemption."""

    plane = _reserving_plane(clock)
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            venue_messages={"binance": 3},
        )
    )
    plane.kill_switch.engage(actor="ops", reason="halt")
    decision = _send(plane)
    assert decision.action is ControlAction.REJECT
    assert decision.control is ControlKind.KILL_SWITCH
    # Nothing was drawn down for a message that never went.
    assert plane.venue_budgets["binance"].snapshot().used == 0


def test_a_reserved_message_still_obeys_a_stale_feed(clock: Clock) -> None:
    plane = _reserving_plane(clock)
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            venue_messages={"binance": 3},
        )
    )
    plane.feed_monitor = FeedMonitor(stale_after_ms=1, now_ns=clock)
    decision = _send(plane)
    assert decision.action is ControlAction.REJECT
    assert decision.control is ControlKind.STALE_FEED


def test_a_hedge_draws_on_its_reserved_hedge_allowance(
    clock: Clock,
) -> None:
    """Hedge capacity is reserved so a hedge can never be unsendable."""

    plane = _reserving_plane(clock)
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            venue_messages={"binance": 1},
            hedge_messages=2,
        )
    )
    assert _send(plane).reason == "reserved_venue_capacity"
    hedge = _send(
        plane,
        stage=OrderStage.EMERGENCY_HEDGE,
        priority=Priority.EMERGENCY_HEDGE,
    )
    assert hedge.action is ControlAction.ADMIT
    assert hedge.reason == "reserved_hedge_capacity"


def test_require_reservation_refuses_unreserved_transmission(
    clock: Clock,
) -> None:
    """Makes "reserve before you send" enforceable, not just documented."""

    plane = _reserving_plane(clock, require_reservation=True)
    refused = _send(plane, opportunity_id="OPP-NEVER-RESERVED")
    assert refused.action is ControlAction.REJECT
    assert "no reserved capacity" in refused.reason
    # A message that names no opportunity at all is equally refused.
    assert _send(plane, opportunity_id=None).action is ControlAction.REJECT

    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            venue_messages={"binance": 1},
        )
    )
    assert _send(plane).action is ControlAction.ADMIT


def test_require_reservation_is_off_by_default(clock: Clock) -> None:
    plane = _reserving_plane(clock)
    assert plane.require_reservation is False
    assert _send(plane, opportunity_id=None).action is ControlAction.ADMIT
    assert _send(plane, opportunity_id="OPP-UNKNOWN").action is (
        ControlAction.ADMIT
    )


def test_a_released_reservation_stops_being_drawn_down(
    clock: Clock,
) -> None:
    """Release hands back what was never sent, and nothing more."""

    plane = _reserving_plane(clock)
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            venue_messages={"binance": 3},
        )
    )
    assert _send(plane).reason == "reserved_venue_capacity"
    plane.ledger.release("OPP-1")
    binance = plane.venue_budgets["binance"]
    # One really was sent; the other two go back.
    assert binance.snapshot().used == 1
    assert binance.snapshot().reserved == 0
    assert binance.available() == 9
    assert _send(plane).reason == "admitted"


def test_a_reservation_also_holds_the_scoped_tiers(clock: Clock) -> None:
    """A drawdown skips the capacity walk, so it must have paid for it.

    Reserving only firm and venue while the plane waives every capacity
    control for anything holding a reservation would let one opportunity
    send straight through its own strategy and instrument limits.
    """

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=100_000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=100_000, now_ns=clock
        ),
        pm_budgets={
            "pm-1": MessageBudget(
                name="pm-1", limit=100, window_ms=100_000, now_ns=clock
            )
        },
        strategy_pm={"alpha": "pm-1"},
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=100, window_ms=100_000, now_ns=clock
            )
        },
        instrument_budgets={
            "BTCUSDT": MessageBudget(
                name="BTCUSDT", limit=100, window_ms=100_000, now_ns=clock
            )
        },
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            venue_messages={"binance": 4},
            instrument="BTCUSDT",
        )
    )
    scoped = (
        plane.pm_budgets["pm-1"],
        plane.strategy_budgets["alpha"],
        plane.instrument_budgets["btcusdt"],
    )
    for budget in scoped:
        assert budget.snapshot().reserved == 4, budget.name

    for _ in range(4):
        assert _send(plane, messages=1).action is ControlAction.ADMIT

    for budget in scoped:
        snapshot = budget.snapshot()
        assert snapshot.used == 4, budget.name
        assert snapshot.reserved == 0, budget.name


def test_a_reservation_is_refused_when_a_scoped_tier_is_short(
    clock: Clock,
) -> None:
    """And refusing must not leave the other tiers drained."""

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=100_000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=100_000, now_ns=clock
        ),
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=2, window_ms=100_000, now_ns=clock
            )
        },
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    with pytest.raises(CapacityReservationError, match="alpha"):
        plane.ledger.reserve(
            ReservationRequest(
                opportunity_id="OPP-1",
                strategy_id="alpha",
                venue_messages={"binance": 5},
            )
        )
    for budget in (
        plane.global_budget,
        plane.venue_budgets["binance"],
        plane.strategy_budgets["alpha"],
    ):
        snapshot = budget.snapshot()
        assert snapshot.reserved == 0, budget.name
        assert snapshot.used == 0, budget.name


def test_releasing_hands_back_the_scoped_tiers_too(clock: Clock) -> None:
    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=100_000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=100_000, now_ns=clock
        ),
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=100, window_ms=100_000, now_ns=clock
            )
        },
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            venue_messages={"binance": 5},
        )
    )
    assert _send(plane).action is ControlAction.ADMIT
    plane.ledger.release("OPP-1")
    strategy = plane.strategy_budgets["alpha"].snapshot()
    assert strategy.used == 1
    assert strategy.reserved == 0
    assert plane.strategy_budgets["alpha"].available() == 99


def test_a_hedge_spend_settles_the_scoped_tiers_too(clock: Clock) -> None:
    """Hedge capacity was reserved on every tier, so spend it there.

    Leaving the scoped tiers reserved after a hedge went out would make
    ``release`` hand back capacity that is already on the wire, and that
    surplus is then given to somebody else who spends it again.
    """

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=100_000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=100_000, now_ns=clock
        ),
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=100, window_ms=100_000, now_ns=clock
            )
        },
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            venue_messages={"binance": 1},
            hedge_messages=2,
        )
    )
    strategy = plane.strategy_budgets["alpha"]
    assert strategy.snapshot().reserved == 3

    # Spend the venue allowance first: the hedge reserve exists for what
    # the venue allowance cannot cover.
    assert _send(plane).reason == "reserved_venue_capacity"
    hedge = _send(
        plane,
        stage=OrderStage.EMERGENCY_HEDGE,
        priority=Priority.EMERGENCY_HEDGE,
    )
    assert hedge.reason == "reserved_hedge_capacity"
    snapshot = strategy.snapshot()
    assert snapshot.used == 2
    assert snapshot.reserved == 1

    # Release must not credit back the messages already sent.
    plane.ledger.release("OPP-1")
    assert strategy.snapshot().used == 2
    assert strategy.snapshot().reserved == 0
    assert strategy.available() == 98


# ---------------------------------------------------------------------
# Round-3 review: a reservation is an authorisation, not a bearer token
# ---------------------------------------------------------------------


def test_ordinary_traffic_cannot_spend_the_hedge_allowance(
    clock: Clock,
) -> None:
    """The hedge allowance is charged to the venue unconditionally.

    That is deliberate -- nothing may block a hedge -- so offering it to
    non-exempt traffic hands ordinary orders an unchecked ``force_consume``
    and lets one opportunity walk straight through a venue's hard limit.
    """

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=2, window_ms=100_000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=100_000, now_ns=clock
        ),
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            venue_messages={"binance": 1},
            hedge_messages=5,
        )
    )
    actions = [
        _send(plane, priority=Priority.RESEARCH).action for _ in range(6)
    ]
    assert actions[0] is ControlAction.ADMIT
    assert plane.venue_budgets["binance"].snapshot().used <= 2
    assert ControlAction.ADMIT not in actions[2:]


def test_a_hedge_still_reaches_its_reserved_allowance(clock: Clock) -> None:
    """The gate must not also lock out the traffic it exists for."""

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=2, window_ms=100_000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=100_000, now_ns=clock
        ),
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            venue_messages={"binance": 1},
            hedge_messages=3,
        )
    )
    assert _send(plane).reason == "reserved_venue_capacity"
    for _ in range(3):
        decision = _send(
            plane,
            stage=OrderStage.EMERGENCY_HEDGE,
            priority=Priority.EMERGENCY_HEDGE,
        )
        assert decision.action is ControlAction.ADMIT
        assert decision.reason == "reserved_hedge_capacity"


def _identity_plane(clock: Clock) -> ExecutionControlPlane:
    return _tiered_plane(
        clock,
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=1000, window_ms=1000, now_ns=clock
            ),
            "beta": MessageBudget(
                name="beta", limit=1, window_ms=1000, now_ns=clock
            ),
        },
        instrument_budgets={
            "BTCUSDT": MessageBudget(
                name="BTCUSDT", limit=1000, window_ms=1000, now_ns=clock
            ),
            "ETHUSDT": MessageBudget(
                name="ETHUSDT", limit=1, window_ms=1000, now_ns=clock
            ),
        },
    )


def test_a_reservation_is_not_honoured_for_another_strategy(
    clock: Clock,
) -> None:
    """A reserved message skips the capacity walk -- so it skips *its own*
    tiers, and is charged to whichever ones the reservation holds."""

    plane = _identity_plane(clock)
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            instrument="BTCUSDT",
            venue_messages={"binance": 10},
        )
    )
    actions = [
        _send(plane, strategy_id="beta", instrument="ETHUSDT").action
        for _ in range(4)
    ]
    assert actions == [ControlAction.ADMIT] + [ControlAction.REJECT] * 3
    assert plane.strategy_budgets["beta"].snapshot().used == 1
    assert plane.instrument_budgets["ethusdt"].snapshot().used == 1
    # And nothing was billed to the strategy that did reserve.
    assert plane.strategy_budgets["alpha"].snapshot().used == 0
    assert plane.instrument_budgets["btcusdt"].snapshot().used == 0


def test_a_reservation_is_honoured_for_its_own_identity(clock: Clock) -> None:
    """The identity check must not refuse the opportunity that paid."""

    plane = _identity_plane(clock)
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            instrument="BTCUSDT",
            venue_messages={"binance": 3},
        )
    )
    for _ in range(3):
        decision = _send(plane, strategy_id="alpha", instrument="BTCUSDT")
        assert decision.reason == "reserved_venue_capacity"


def test_a_spread_reserves_an_instrument_tier_for_each_leg(
    clock: Clock,
) -> None:
    """Two instruments is two tiers.

    ``venue_messages`` is a mapping because multi-leg is the point of the
    class, so folding both legs under one instrument would leave the
    second leg's tier unreserved -- and therefore unenforced.
    """

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=100_000, now_ns=clock
            ),
            "okx": MessageBudget(
                name="okx", limit=100, window_ms=100_000, now_ns=clock
            ),
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=100_000, now_ns=clock
        ),
        instrument_budgets={
            "BTCUSDT": MessageBudget(
                name="BTCUSDT", limit=100, window_ms=100_000, now_ns=clock
            ),
            "ETHUSDT": MessageBudget(
                name="ETHUSDT", limit=100, window_ms=100_000, now_ns=clock
            ),
        },
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    plane.feed_monitor.record_tick("okx")
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            instrument="BTCUSDT",
            venue_instruments={"okx": "ETHUSDT"},
            venue_messages={"binance": 2, "okx": 3},
        )
    )
    btc = plane.instrument_budgets["btcusdt"]
    eth = plane.instrument_budgets["ethusdt"]
    assert btc.snapshot().reserved == 2
    assert eth.snapshot().reserved == 3

    # Each leg spends its own tier, not the opportunity's default.
    assert (
        _send(plane, venue="okx", instrument="ETHUSDT").reason
        == "reserved_venue_capacity"
    )
    assert eth.snapshot().used == 1
    assert btc.snapshot().used == 0

    plane.ledger.release("OPP-1")
    assert eth.snapshot().reserved == 0
    assert btc.snapshot().reserved == 0
    assert eth.available() == 99
    assert btc.available() == 100


# ---------------------------------------------------------------------
# Round-3 review: shared tiers, visibility, poison entries and ceilings
# ---------------------------------------------------------------------


def _queue_two(plane: ExecutionControlPlane) -> None:
    """A large high-priority message ahead of a small low-priority one."""

    for message_id, priority, messages in (
        ("mm-big", Priority.MARKET_MAKING, 2),
        ("research-small", Priority.RESEARCH, 1),
    ):
        plane.queue.push(
            QueuedMessage(
                message_id=message_id,
                venue="binance",
                strategy_id="alpha",
                priority=priority,
                stage=OrderStage.NEW,
                messages=messages,
                enqueued_ns=0,
                expires_ns=10**18,
            )
        )


def test_a_short_strategy_budget_stops_the_drain_scan(clock: Clock) -> None:
    """A strategy budget is shared by every queued message of that strategy.

    That is the property that made ``STOP`` necessary for the firm
    budget. Scanning past a strategy tier that refused reproduces the
    same inversion one scope down: the low-priority message is small
    enough to fit and takes the slot the high-priority one was waiting
    for.
    """

    plane = _tiered_plane(
        clock,
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=1, window_ms=1000, now_ns=clock
            )
        },
    )
    _queue_two(plane)
    assert [item.message_id for item in plane.drain()] == []
    assert sum(plane.queue.depth_by_priority().values()) == 2


def test_a_short_pm_budget_stops_the_drain_scan(clock: Clock) -> None:
    plane = _tiered_plane(
        clock,
        pm_budgets={
            "pm-1": MessageBudget(
                name="pm-1", limit=1, window_ms=1000, now_ns=clock
            )
        },
        strategy_pm={"alpha": "pm-1"},
    )
    _queue_two(plane)
    assert [item.message_id for item in plane.drain()] == []


def test_a_short_instrument_budget_only_skips_that_instrument(
    clock: Clock,
) -> None:
    """An instrument tier is genuinely scoped, so scanning past is safe."""

    plane = _tiered_plane(
        clock,
        instrument_budgets={
            "BTCUSDT": MessageBudget(
                name="BTCUSDT", limit=1, window_ms=1000, now_ns=clock
            )
        },
    )
    for message_id, priority, instrument in (
        ("btc-big", Priority.MARKET_MAKING, "BTCUSDT"),
        ("eth-small", Priority.RESEARCH, "ETHUSDT"),
    ):
        plane.queue.push(
            QueuedMessage(
                message_id=message_id,
                venue="binance",
                strategy_id="alpha",
                priority=priority,
                stage=OrderStage.NEW,
                messages=2 if instrument == "BTCUSDT" else 1,
                enqueued_ns=0,
                expires_ns=10**18,
                instrument=instrument,
            )
        )
    assert [item.message_id for item in plane.drain()] == ["eth-small"]


def test_every_configured_tier_appears_on_the_dashboard(
    clock: Clock,
) -> None:
    """A control the operator cannot see is a control they cannot act on.

    The tiers record their own queue, drop and reject counters. If the
    dashboard only reads firm and venue, a strategy limit can be
    binding, queueing and rejecting while the banner still says green.
    """

    plane = _tiered_plane(
        clock,
        pm_budgets={
            "pm-1": MessageBudget(
                name="pm-1", limit=1000, window_ms=1000, now_ns=clock
            )
        },
        strategy_pm={"alpha": "pm-1"},
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=1, window_ms=30_000, now_ns=clock
            )
        },
        instrument_budgets={
            "BTCUSDT": MessageBudget(
                name="BTCUSDT", limit=1000, window_ms=1000, now_ns=clock
            )
        },
    )
    names = [item.name for item in plane.snapshots()]
    assert names == ["firm", "pm-1", "alpha", "BTCUSDT", "binance"]

    assert _ask(plane).action is ControlAction.ADMIT
    rejected = plane.evaluate(
        MessageRequest(
            message_id="m2",
            strategy_id="alpha",
            venue="binance",
            stage=OrderStage.NEW,
            priority=Priority.MARKET_MAKING,
            messages=1,
            ttl_ms=10,
        )
    )
    assert rejected.action is ControlAction.REJECT

    dashboard = plane.dashboard()
    assert dashboard["band"] == "red"
    assert dashboard["binding"] is True
    assert dashboard["driver"] == "alpha"
    assert dashboard["rejected_messages"] == 1
    # The venue window is 1s; alpha needs 30s. Reporting the shorter one
    # tells the operator to wait a second for a thirty-second outage.
    assert dashboard["expected_recovery_ms"] > 1000


def test_a_tier_is_labelled_with_the_level_it_enforces(clock: Clock) -> None:
    """``MessageBudget`` defaults to ``VENUE_LIMIT``.

    Left unstamped, the dashboard names a binding strategy limit as a
    venue limit -- the wrong control, and the wrong remedy.
    """

    plane = _tiered_plane(
        clock,
        pm_budgets={
            "pm-1": MessageBudget(
                name="pm-1", limit=5, window_ms=1000, now_ns=clock
            )
        },
        strategy_pm={"alpha": "pm-1"},
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=5, window_ms=1000, now_ns=clock
            )
        },
        instrument_budgets={
            "BTCUSDT": MessageBudget(
                name="BTCUSDT", limit=5, window_ms=1000, now_ns=clock
            )
        },
    )
    levels = {item.name: item.level for item in plane.snapshots()}
    assert levels == {
        "firm": ControlLevel.FIRM_MESSAGE_LIMIT,
        "pm-1": ControlLevel.PM_LIMIT,
        "alpha": ControlLevel.STRATEGY_LIMIT,
        "BTCUSDT": ControlLevel.INSTRUMENT_LIMIT,
        "binance": ControlLevel.VENUE_LIMIT,
    }


def test_a_queued_message_must_carry_at_least_one_message() -> None:
    """``pop_if`` runs the release callback over every message it scans.

    One malformed entry would therefore raise on every drain and never
    be removed, taking down everything queued behind it -- an outage,
    not a dropped message.
    """

    with pytest.raises(ValueError, match="at least 1 message"):
        QueuedMessage(
            message_id="poison",
            venue="binance",
            strategy_id="alpha",
            priority=Priority.RESEARCH,
            stage=OrderStage.NEW,
            messages=0,
            enqueued_ns=0,
            expires_ns=10**18,
        )


# ---------------------------------------------------------------------
# Round-3 review: priority allocates capacity, not just queue position
# ---------------------------------------------------------------------


def _flood_plane(clock: Clock) -> ExecutionControlPlane:
    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=10, window_ms=100_000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=10, window_ms=100_000, now_ns=clock
        ),
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    return plane


def test_research_cannot_spend_the_headroom_higher_classes_need(
    clock: Clock,
) -> None:
    """Priority that only reorders a queue does not allocate capacity.

    A research backfill arriving first spends the firm budget before any
    market-making message is even evaluated, and the priority queue can
    only reorder the ones that already lost. A ceiling reserves the
    headroom instead.
    """

    plane = _flood_plane(clock)
    admitted = sum(
        _send(plane, opportunity_id=None, priority=Priority.RESEARCH).action
        is ControlAction.ADMIT
        for _ in range(10)
    )
    assert admitted == 6  # 60% of ten, not all ten
    assert plane.global_budget.snapshot().used == 6

    for priority in (Priority.QUOTE_REFRESH, Priority.MARKET_MAKING):
        decision = _send(plane, opportunity_id=None, priority=priority)
        assert decision.action is ControlAction.ADMIT, priority


def test_quote_refresh_stops_higher_up_than_research(clock: Clock) -> None:
    plane = _flood_plane(clock)
    admitted = sum(
        _send(
            plane, opportunity_id=None, priority=Priority.QUOTE_REFRESH
        ).action
        is ControlAction.ADMIT
        for _ in range(10)
    )
    assert admitted == 8
    assert (
        _send(
            plane, opportunity_id=None, priority=Priority.MARKET_MAKING
        ).action
        is ControlAction.ADMIT
    )


def test_a_ceiling_never_blocks_exposure_reduction(clock: Clock) -> None:
    """Requirement 10 outranks the ceiling: a headroom rule is still a
    block, and nothing may block a hedge."""

    plane = _flood_plane(clock)
    for _ in range(10):
        _send(plane, opportunity_id=None, priority=Priority.MARKET_MAKING)
    assert plane.global_budget.available() == 0
    for priority, stage in (
        (Priority.EMERGENCY_HEDGE, OrderStage.EMERGENCY_HEDGE),
        (Priority.FORCED_LIQUIDATION, OrderStage.FORCED_LIQUIDATION),
        (Priority.RISK_REDUCING, OrderStage.CANCEL_REQUEST),
    ):
        decision = _send(
            plane, opportunity_id=None, priority=priority, stage=stage
        )
        assert decision.action is ControlAction.ADMIT, priority


def test_the_drain_applies_the_same_ceiling_as_admission(
    clock: Clock,
) -> None:
    """Otherwise the ceiling delays a message by one cycle and no more."""

    plane = _flood_plane(clock)
    for _ in range(6):
        _send(plane, opportunity_id=None, priority=Priority.MARKET_MAKING)
    assert plane.global_budget.snapshot().used == 6

    plane.queue.push(
        QueuedMessage(
            message_id="research",
            venue="binance",
            strategy_id="alpha",
            priority=Priority.RESEARCH,
            stage=OrderStage.NEW,
            messages=1,
            enqueued_ns=0,
            expires_ns=10**18,
        )
    )
    assert plane.drain() == ()

    # Once the window rolls the headroom is back and it releases.
    clock.advance_ms(100_001)
    assert [item.message_id for item in plane.drain()] == ["research"]


def test_a_ceiling_must_be_a_percentage(clock: Clock) -> None:
    with pytest.raises(ValueError, match="not a percentage"):
        _flood_plane_with(clock, {Priority.RESEARCH: Decimal(140)})


def _flood_plane_with(
    clock: Clock, ceilings: dict[Priority, Decimal]
) -> ExecutionControlPlane:
    return ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=10, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=10, window_ms=1000, now_ns=clock
        ),
        priority_ceilings=ceilings,
        now_ns=clock,
    )


def test_ceilings_are_configurable(clock: Clock) -> None:
    plane = _flood_plane_with(clock, {Priority.RESEARCH: Decimal(30)})
    plane.feed_monitor.record_tick("binance")
    admitted = sum(
        _send(plane, opportunity_id=None, priority=Priority.RESEARCH).action
        is ControlAction.ADMIT
        for _ in range(10)
    )
    assert admitted == 3


def test_a_reservation_is_refused_on_a_strategy_mismatch_alone(
    clock: Clock,
) -> None:
    """Each half of the identity check must stand on its own."""

    plane = _identity_plane(clock)
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            venue_messages={"binance": 10},
        )
    )
    actions = [
        _send(plane, strategy_id="beta").action for _ in range(3)
    ]
    assert actions == [ControlAction.ADMIT, ControlAction.REJECT,
                       ControlAction.REJECT]
    assert plane.strategy_budgets["beta"].snapshot().used == 1


def test_a_reservation_is_refused_on_an_instrument_mismatch_alone(
    clock: Clock,
) -> None:
    plane = _identity_plane(clock)
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            instrument="BTCUSDT",
            venue_messages={"binance": 10},
        )
    )
    actions = [
        _send(plane, strategy_id="alpha", instrument="ETHUSDT").action
        for _ in range(3)
    ]
    assert actions == [ControlAction.ADMIT, ControlAction.REJECT,
                       ControlAction.REJECT]
    assert plane.instrument_budgets["ethusdt"].snapshot().used == 1


def test_releasing_a_spread_leaves_another_holder_untouched(
    clock: Clock,
) -> None:
    """Crediting each tier the whole opportunity's remainder would hand
    back capacity that tier never took -- and the surplus comes out of
    whoever else is holding a reservation on it."""

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=100_000, now_ns=clock
            ),
            "okx": MessageBudget(
                name="okx", limit=100, window_ms=100_000, now_ns=clock
            ),
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=100_000, now_ns=clock
        ),
        instrument_budgets={
            "BTCUSDT": MessageBudget(
                name="BTCUSDT", limit=100, window_ms=100_000, now_ns=clock
            ),
            "ETHUSDT": MessageBudget(
                name="ETHUSDT", limit=100, window_ms=100_000, now_ns=clock
            ),
        },
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    for venue in ("binance", "okx"):
        plane.feed_monitor.record_tick(venue)

    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-OTHER",
            strategy_id="beta",
            instrument="BTCUSDT",
            venue_messages={"binance": 4},
        )
    )
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-SPREAD",
            strategy_id="alpha",
            instrument="BTCUSDT",
            venue_instruments={"okx": "ETHUSDT"},
            venue_messages={"binance": 2, "okx": 3},
        )
    )
    btc = plane.instrument_budgets["btcusdt"]
    assert btc.snapshot().reserved == 6

    assert (
        _send(
            plane,
            opportunity_id="OPP-SPREAD",
            venue="okx",
            instrument="ETHUSDT",
        ).reason
        == "reserved_venue_capacity"
    )
    plane.ledger.release("OPP-SPREAD")

    # The spread held two of BTCUSDT and spent none of them, so exactly
    # two come back and the other holder's four are still theirs.
    assert btc.snapshot().reserved == 4


def test_a_queued_hedge_is_never_held_back_by_a_ceiling(
    clock: Clock,
) -> None:
    """The exemption has to survive into the drain path too.

    ``_binds`` is never reached for exempt traffic, so the ceiling
    exemption is only observable where a queued hedge is released.
    """

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=10, window_ms=100_000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=10, window_ms=100_000, now_ns=clock
        ),
        priority_ceilings={Priority.EMERGENCY_HEDGE: Decimal(10)},
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    for _ in range(5):
        _send(plane, opportunity_id=None, priority=Priority.MARKET_MAKING)
    assert plane.global_budget.snapshot().used == 5

    plane.queue.push(
        QueuedMessage(
            message_id="hedge",
            venue="binance",
            strategy_id="alpha",
            priority=Priority.EMERGENCY_HEDGE,
            stage=OrderStage.EMERGENCY_HEDGE,
            messages=1,
            enqueued_ns=0,
            expires_ns=10**18,
        )
    )
    assert [item.message_id for item in plane.drain()] == ["hedge"]


# ---------------------------------------------------------------------
# Round-4 review: scoped STOP, ceilings on the reserve path, recovery
# ---------------------------------------------------------------------


def _push(
    plane: ExecutionControlPlane,
    message_id: str,
    *,
    strategy_id: str = "alpha",
    priority: Priority = Priority.MARKET_MAKING,
    messages: int = 1,
    instrument: str = "",
    venue: str = "binance",
) -> None:
    plane.queue.push(
        QueuedMessage(
            message_id=message_id,
            venue=venue,
            strategy_id=strategy_id,
            priority=priority,
            stage=OrderStage.NEW,
            messages=messages,
            instrument=instrument,
            enqueued_ns=0,
            expires_ns=10**18,
        )
    )


def test_one_strategys_shortage_does_not_stall_another_strategy(
    clock: Clock,
) -> None:
    """``STOP`` has to carry a scope or it becomes a firm-wide stall.

    A strategy budget is shared only by that strategy's messages, so
    refusing on its behalf says nothing about anyone else. Ending the
    scan there idles every other strategy behind it -- the same
    head-of-line block ``SKIP`` exists to prevent, one scope up.
    """

    plane = _tiered_plane(
        clock,
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=1, window_ms=1000, now_ns=clock
            ),
            "beta": MessageBudget(
                name="beta", limit=100, window_ms=1000, now_ns=clock
            ),
        },
    )
    _push(plane, "alpha-1", strategy_id="alpha", messages=2)
    _push(plane, "beta-1", strategy_id="beta")
    _push(plane, "beta-2", strategy_id="beta")

    assert [item.message_id for item in plane.drain()] == [
        "beta-1",
        "beta-2",
    ]
    assert sum(plane.queue.depth_by_priority().values()) == 1


def test_a_short_firm_budget_still_ends_the_whole_scan(
    clock: Clock,
) -> None:
    """The firm tier is the one scope every queued message shares.

    Scoping ``STOP`` must not weaken it: scanning past the firm's last
    slot hands it to strictly lower-priority traffic.
    """

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=1000, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=1, window_ms=1000, now_ns=clock
        ),
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    _push(plane, "mm-big", messages=2)
    _push(plane, "beta-small", strategy_id="beta", priority=Priority.RESEARCH)

    assert [item.message_id for item in plane.drain()] == []
    assert sum(plane.queue.depth_by_priority().values()) == 2


def test_a_blocked_scope_is_not_reprobed_later_in_the_same_scan(
    clock: Clock,
) -> None:
    """Blocking the scope, not just the message, is the point.

    Without the accumulated keys the scan would reach ``alpha``'s
    smaller message, find its one free slot and take it -- restoring
    exactly the inversion the ``STOP`` was raised to prevent.
    """

    plane = _tiered_plane(
        clock,
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=1, window_ms=1000, now_ns=clock
            ),
            "beta": MessageBudget(
                name="beta", limit=100, window_ms=1000, now_ns=clock
            ),
        },
    )
    _push(plane, "alpha-big", strategy_id="alpha", messages=2)
    _push(plane, "beta-1", strategy_id="beta")
    _push(
        plane,
        "alpha-small",
        strategy_id="alpha",
        priority=Priority.RESEARCH,
        messages=1,
    )

    assert [item.message_id for item in plane.drain()] == ["beta-1"]
    assert plane.strategy_budgets["alpha"].snapshot().used == 0


def test_a_reservation_cannot_take_a_budget_past_its_ceiling(
    clock: Clock,
) -> None:
    """A reservation is the one call that can take a whole budget at once.

    Reserved messages draw down instead of walking the capacity
    controls, so a ceiling enforced only at admission is a ceiling any
    caller can evade by reserving first.
    """

    plane = _reserving_plane(
        clock, priority_ceilings={Priority.RESEARCH: Decimal(60)}
    )
    with pytest.raises(CapacityReservationError) as excinfo:
        plane.ledger.reserve(
            ReservationRequest(
                opportunity_id="OPP-RESEARCH",
                strategy_id="alpha",
                venue_messages={"binance": 10},
                priority=Priority.RESEARCH,
            )
        )
    assert "60% ceiling" in str(excinfo.value)
    assert plane.global_budget.snapshot().reserved == 0
    assert plane.venue_budgets["binance"].snapshot().reserved == 0


def test_the_same_reservation_is_admitted_for_a_higher_class(
    clock: Clock,
) -> None:
    """The refusal has to come from the ceiling, not from the capacity.

    Same size, same budgets, only the scheduling class differs.
    """

    plane = _reserving_plane(
        clock, priority_ceilings={Priority.RESEARCH: Decimal(60)}
    )
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-MM",
            strategy_id="alpha",
            venue_messages={"binance": 10},
            priority=Priority.MARKET_MAKING,
        )
    )
    assert plane.global_budget.snapshot().reserved == 10


def _wide_venue_plane(clock: Clock, **kwargs: object) -> ExecutionControlPlane:
    """Room at the venue, so a refusal can only come from another tier.

    ``reserve`` walks the venue budgets first, so a plane whose venue is
    the tightest tier can never demonstrate anything about the ones
    behind it.
    """

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=1000, window_ms=100_000, now_ns=clock
            )
        },
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
        **kwargs,
    )
    plane.feed_monitor.record_tick("binance")
    return plane


def test_a_reservation_ceiling_covers_the_strategy_tier_too(
    clock: Clock,
) -> None:
    """The venue is not the only tier a reservation charges."""

    plane = _wide_venue_plane(
        clock,
        global_budget=MessageBudget(
            name="firm", limit=1000, window_ms=100_000, now_ns=clock
        ),
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=10, window_ms=100_000, now_ns=clock
            )
        },
        priority_ceilings={Priority.RESEARCH: Decimal(50)},
    )
    with pytest.raises(CapacityReservationError):
        plane.ledger.reserve(
            ReservationRequest(
                opportunity_id="OPP-RESEARCH",
                strategy_id="alpha",
                venue_messages={"binance": 8},
                priority=Priority.RESEARCH,
            )
        )
    assert plane.strategy_budgets["alpha"].snapshot().reserved == 0
    assert plane.venue_budgets["binance"].snapshot().reserved == 0


def test_a_reservation_ceiling_covers_the_firm_tier_too(
    clock: Clock,
) -> None:
    """The most senior tier is checked on the reserve path as well."""

    plane = _wide_venue_plane(
        clock,
        global_budget=MessageBudget(
            name="firm", limit=10, window_ms=100_000, now_ns=clock
        ),
        priority_ceilings={Priority.RESEARCH: Decimal(50)},
    )
    with pytest.raises(CapacityReservationError):
        plane.ledger.reserve(
            ReservationRequest(
                opportunity_id="OPP-RESEARCH",
                strategy_id="alpha",
                venue_messages={"binance": 8},
                priority=Priority.RESEARCH,
            )
        )
    assert plane.global_budget.snapshot().reserved == 0
    assert plane.venue_budgets["binance"].snapshot().reserved == 0


def test_a_large_message_cannot_overshoot_its_ceiling(
    clock: Clock,
) -> None:
    """Testing only ``consumed >= ceiling`` lets one message cross it.

    The overshoot is ``messages - 1`` and is unbounded unless a per-order
    cap is configured, so a reserve that checks only the before-state
    shapes flows of single messages and nothing else.
    """

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=1000, window_ms=100_000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=10, window_ms=100_000, now_ns=clock
        ),
        priority_ceilings={Priority.RESEARCH: Decimal(60)},
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    for _ in range(5):
        _send(plane, opportunity_id=None, priority=Priority.MARKET_MAKING)
    assert plane.global_budget.snapshot().used == 5

    decision = _send(
        plane,
        opportunity_id=None,
        priority=Priority.RESEARCH,
        messages=5,
    )
    assert decision.action is not ControlAction.ADMIT
    assert plane.global_budget.snapshot().used == 5

    # The headroom the ceiling held back is still there for the class
    # it was held for.
    assert (
        _send(
            plane,
            opportunity_id=None,
            priority=Priority.MARKET_MAKING,
            messages=5,
        ).action
        is ControlAction.ADMIT
    )


def test_a_single_message_still_fits_a_budget_too_small_to_share(
    clock: Clock,
) -> None:
    """The concession that keeps small budgets usable, and its limit.

    On a limit of one, one message is already 100%, so a strict rule
    would ban every class below the top one outright. One message on an
    untouched budget goes; the next one does not.
    """

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=1000, window_ms=100_000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=1, window_ms=100_000, now_ns=clock
        ),
        priority_ceilings={Priority.RESEARCH: Decimal(60)},
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    first = _send(plane, opportunity_id=None, priority=Priority.RESEARCH)
    assert first.action is ControlAction.ADMIT

    second = _send(plane, opportunity_id=None, priority=Priority.RESEARCH)
    assert second.action is not ControlAction.ADMIT


def test_an_idle_long_window_tier_is_not_reported_as_recovery_time(
    clock: Clock,
) -> None:
    """Recovery is what a *constraining* control needs, not the longest window.

    Timing every configured window reports the longest one the firm
    happens to run as the wait on a completely quiet system, which reads
    as an outage that is not happening.
    """

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=1000, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=1000, window_ms=99_999, now_ns=clock
        ),
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    board = plane.dashboard()
    assert board["binding"] is False
    assert board["queued_messages"] == 0
    assert board["expected_recovery_ms"] == 0


def test_a_binding_tier_still_reports_when_it_refills(clock: Clock) -> None:
    """The other half: a control that *is* binding must be timed."""

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=1, window_ms=5_000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=1000, window_ms=99_999, now_ns=clock
        ),
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    _send(plane, opportunity_id=None)
    board = plane.dashboard()
    assert board["binding"] is True
    assert board["expected_recovery_ms"] == 5_000


def _ceiling_plane(clock: Clock, limit: int = 10) -> ExecutionControlPlane:
    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=1000, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=limit, window_ms=1000, now_ns=clock
        ),
        priority_ceilings={Priority.RESEARCH: Decimal(60)},
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    return plane


def test_a_request_larger_than_its_whole_share_is_rejected_not_queued(
    clock: Clock,
) -> None:
    """Queueing something no drain can release only hides the loss.

    Seven messages against a firm budget of ten is more than the 60% a
    research order may ever use, so an *empty* budget refuses it too.
    Waiting cannot help. Queued, it would occupy a slot, hold its scope
    against every later scan, and disappear at expiry.
    """

    plane = _ceiling_plane(clock)
    decision = _send(
        plane,
        opportunity_id=None,
        priority=Priority.RESEARCH,
        messages=7,
        ttl_ms=10_000,
    )
    assert decision.action is ControlAction.REJECT
    assert "exceed the 60% of firm" in decision.reason
    assert sum(plane.queue.depth_by_priority().values()) == 0


def test_a_request_that_only_has_to_wait_is_still_queued(
    clock: Clock,
) -> None:
    """The discriminator: congested now is not the same as impossible.

    Same class and a size that *does* fit the 60% share, refused only
    because the window is currently busy. Rejecting this one too would
    turn a ceiling into a ban.
    """

    plane = _ceiling_plane(clock)
    for _ in range(4):
        _send(plane, opportunity_id=None, priority=Priority.MARKET_MAKING)
    decision = _send(
        plane,
        opportunity_id=None,
        priority=Priority.RESEARCH,
        messages=5,
        ttl_ms=10_000,
    )
    assert decision.action is ControlAction.QUEUE
    assert sum(plane.queue.depth_by_priority().values()) == 1


def test_one_message_waits_even_for_a_budget_too_small_to_share(
    clock: Clock,
) -> None:
    """The concession is only visible where the share is under one message.

    60% of a budget of one is 0.6, so by size alone a single message
    already "exceeds" it. But one message against an untouched budget is
    always admitted, so waiting genuinely does work and this must queue
    rather than be rejected as impossible.
    """

    plane = _ceiling_plane(clock, limit=1)
    _send(plane, opportunity_id=None, priority=Priority.MARKET_MAKING)
    decision = _send(
        plane,
        opportunity_id=None,
        priority=Priority.RESEARCH,
        messages=1,
        ttl_ms=10_000,
    )
    assert decision.action is ControlAction.QUEUE


def test_a_class_without_a_ceiling_is_queued_not_rejected(
    clock: Clock,
) -> None:
    """The refusal must come from the ceiling, not from the size.

    The budget has to be *congested* for this to mean anything: with
    room to spare nothing binds and the message is simply admitted,
    never reaching the queue-or-reject decision under test.
    """

    plane = _ceiling_plane(clock)
    for _ in range(8):
        _send(plane, opportunity_id=None, priority=Priority.MARKET_MAKING)
    assert plane.global_budget.snapshot().used == 8

    decision = _send(
        plane,
        opportunity_id=None,
        priority=Priority.MARKET_MAKING,
        messages=7,
        ttl_ms=10_000,
    )
    assert decision.action is ControlAction.QUEUE


def test_an_undrainable_message_cannot_stall_the_drain_scan(
    clock: Clock,
) -> None:
    """Admission rejects these, but the queue is a public object.

    A message too large for its class's whole share says nothing about
    the budget's capacity, so it must never ``STOP`` the scan on the
    budget's behalf -- that would block every other message behind it
    until it expired.
    """

    plane = _ceiling_plane(clock)
    _push(plane, "poison", priority=Priority.RESEARCH, messages=7)
    _push(plane, "ok", priority=Priority.RESEARCH, messages=1)

    assert [item.message_id for item in plane.drain()] == ["ok"]


def test_blocking_a_strategy_does_not_block_a_venue_of_the_same_name(
    clock: Clock,
) -> None:
    """Budget keys are namespaced, and the drain scan now relies on it.

    ``pop_if`` accumulates the keys of refusing budgets and skips any
    message carrying one. A strategy named after a venue would otherwise
    produce the same key, so blocking the strategy would silently block
    every message routed to that venue -- including other strategies'.
    """

    plane = _tiered_plane(
        clock,
        strategy_budgets={
            "binance": MessageBudget(
                name="binance-strategy",
                limit=1,
                window_ms=1000,
                now_ns=clock,
            ),
            "gamma": MessageBudget(
                name="gamma", limit=100, window_ms=1000, now_ns=clock
            ),
        },
    )
    # Too large for its own strategy tier, so it blocks that scope.
    _push(plane, "same-name", strategy_id="binance", messages=2)
    # Different strategy, but routed to the venue sharing that name.
    _push(plane, "gamma-1", strategy_id="gamma", venue="binance")

    assert [item.message_id for item in plane.drain()] == ["gamma-1"]


def test_a_reservation_reports_the_most_senior_tier_that_refuses(
    clock: Clock,
) -> None:
    """``CONTROL_PRECEDENCE`` is a published promise about *reporting*.

    Firm, strategy and venue would all refuse this request. Naming the
    venue -- the least senior of the three -- sends the caller to retry
    against the wrong constraint. Rollback makes the order in which the
    tiers are *taken* immaterial, so the ordering belongs in a check that
    runs before anything is taken.
    """

    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=10, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=10, window_ms=1000, now_ns=clock
        ),
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=10, window_ms=1000, now_ns=clock
            )
        },
        priority_ceilings={Priority.RESEARCH: Decimal(60)},
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    with pytest.raises(CapacityReservationError) as excinfo:
        plane.ledger.reserve(
            ReservationRequest(
                opportunity_id="OPP-1",
                strategy_id="alpha",
                venue_messages={"binance": 8},
                priority=Priority.RESEARCH,
            )
        )
    assert "firm" in str(excinfo.value)
    assert "binance" not in str(excinfo.value)


def test_a_plain_shortage_also_names_the_most_senior_tier(
    clock: Clock,
) -> None:
    """Not only the ceiling: ordinary capacity must be reported in order too.

    The firm needs 6 and has 3; the venue needs 2 and has 1. Both refuse,
    and the venue is the one the take walk reaches first.
    """

    ledger = build_ledger(clock, venue_limit=1, global_limit=3)
    with pytest.raises(CapacityReservationError) as excinfo:
        ledger.reserve(reservation())

    assert "firm has 3 of 6" in str(excinfo.value)
    assert ledger.venue_budgets["binance"].snapshot().reserved == 0


class _CeilingRacingBudget(MessageBudget):
    """A budget that passes the pre-flight and then crosses its ceiling.

    ``reserve`` reads every tier before spending any of it, and the plane
    charges ordinary sends without holding the ledger's lock, so another
    thread can push a tier past its ceiling in between. Reproduced here
    rather than hoped for.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.checks = 0

    def above_ceiling(
        self, ceiling_pct: Decimal, messages: int = 0
    ) -> bool:
        self.checks += 1
        if self.checks == 1:
            return False
        return True


@pytest.mark.parametrize("racing", ["venue", "firm", "strategy"])
def test_a_ceiling_crossed_after_the_preflight_still_refuses(
    clock: Clock, racing: str
) -> None:
    """The take-path ceiling guard is a race guard, and the race is real.

    Every tier the pre-flight reads has to be re-checked when it is
    taken, not just the venue: the loser of the race is whichever tier
    another thread happened to charge.
    """

    def budget(name: str) -> MessageBudget:
        maker = (
            _CeilingRacingBudget if racing == name else MessageBudget
        )
        return maker(name, limit=100, window_ms=1000, now_ns=clock)

    okx = budget("venue")
    firm = budget("firm")
    strategy = budget("strategy")
    ledger = ReservationLedger(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=1000, now_ns=clock
            ),
            "okx": okx,
        },
        global_budget=firm,
        strategy_budgets={"alpha": strategy},
        priority_ceilings={Priority.RESEARCH: Decimal(60)},
    )
    with pytest.raises(CapacityReservationError, match="ceiling"):
        ledger.reserve(reservation(binance=1, okx=1, hedge=0))

    loser = {"venue": okx, "firm": firm, "strategy": strategy}[racing]
    assert isinstance(loser, _CeilingRacingBudget)
    assert loser.checks >= 2
    assert ledger.venue_budgets["binance"].snapshot().reserved == 0
    assert ledger.global_budget.snapshot().reserved == 0
    assert ledger.strategy_budgets["alpha"].snapshot().reserved == 0
    assert ledger.get("OPP-1") is None


def test_a_message_larger_than_a_lower_tier_is_refused_not_queued() -> None:
    """A queue is a promise the message will eventually be sent.

    ``_evaluate`` returns on the first control that binds, so testing
    "can this ever fit?" against only that control asks the question of
    the wrong budget. A venue with room to spare answers yes while an
    instrument two tiers down can never supply the request, and the
    message waits for a refill that changes nothing.
    """

    clock = Clock()
    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=10, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=1000, window_ms=1000, now_ns=clock
        ),
        instrument_budgets={
            "btcusdt": MessageBudget(
                name="btcusdt", limit=3, window_ms=1000, now_ns=clock
            )
        },
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    # Warm the venue so it is the tier that binds, hiding the instrument.
    plane.venue_budgets["binance"].force_consume(8)

    decision = _send(
        plane,
        messages=3,
        instrument="btcusdt",
        priority=Priority.RESEARCH,
        ttl_ms=10_000,
    )

    assert decision.action is ControlAction.REJECT
    assert decision.level is ControlLevel.INSTRUMENT_LIMIT
    assert decision.retry_after_ms is None
    assert len(plane.queue) == 0


def test_a_class_with_no_ceiling_is_still_refused_when_it_cannot_fit() -> None:
    """``MARKET_MAKING`` has a 100% ceiling, so the ceiling arithmetic
    short-circuits. The plain "larger than the whole budget" case has to
    be answered before that short-circuit or the firm's primary trading
    class is the one class with no never-fits protection at all.
    """

    clock = Clock()
    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=10, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=1000, window_ms=1000, now_ns=clock
        ),
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")

    decision = _send(
        plane,
        messages=15,
        priority=Priority.MARKET_MAKING,
        ttl_ms=10_000,
    )

    assert decision.action is ControlAction.REJECT
    assert decision.retry_after_ms is None
    assert "15 messages exceed" in decision.reason
    assert len(plane.queue) == 0
    budget = plane.venue_budgets["binance"]
    assert budget.never_fits(Decimal(100), 15) is True
    assert budget.never_fits(Decimal(100), 10) is False


@pytest.mark.parametrize("configured", ["alpha", "Alpha"])
@pytest.mark.parametrize("spelling", ["alpha", "Alpha", " ALPHA "])
def test_a_strategy_limit_survives_a_change_of_case(
    spelling: str, configured: str
) -> None:
    """Venues are normalised; PM and strategy tiers were not.

    A missed lookup is indistinguishable from "no tier configured", and
    ``_binds`` treats an absent optional tier as not applying -- so the
    limit does not fire late, it disappears. Varying the case of a
    strategy id was enough to opt out of its own allowance.
    """

    clock = Clock()
    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=1000, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=1000, window_ms=1000, now_ns=clock
        ),
        strategy_budgets={
            configured: MessageBudget(
                name="alpha", limit=1, window_ms=1000, now_ns=clock
            )
        },
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")

    actions = [
        _send(plane, strategy_id=spelling, ttl_ms=10_000).action.value
        for _ in range(3)
    ]

    assert actions == ["admit", "queue", "queue"]


def test_a_pm_limit_survives_a_change_of_case() -> None:
    """The PM tier is reached through ``strategy_pm``, so both the map's
    keys and its values have to be normalised -- the value is itself a
    lookup into ``pm_budgets``.
    """

    clock = Clock()
    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=1000, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=1000, window_ms=1000, now_ns=clock
        ),
        pm_budgets={
            "desk-a": MessageBudget(
                name="desk-a", limit=1, window_ms=1000, now_ns=clock
            )
        },
        strategy_pm={"Alpha": "DESK-A"},
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")

    first = _send(plane, strategy_id="alpha", ttl_ms=10_000)
    second = _send(plane, strategy_id="ALPHA", ttl_ms=10_000)

    assert first.action is ControlAction.ADMIT
    assert second.action is ControlAction.QUEUE
    assert second.level is ControlLevel.PM_LIMIT


@pytest.mark.parametrize("configured", ["alpha", "Alpha"])
def test_a_reservation_strategy_limit_survives_a_change_of_case(
    configured: str,
) -> None:
    """The ledger resolves the same tiers through its own ``_scoped``.

    Fixing only the plane would leave the reservation path -- the one
    that runs *before* any order is transmitted -- still evadable.
    """

    clock = Clock()
    ledger = ReservationLedger(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=1000, window_ms=1000, now_ns=clock
            ),
            "okx": MessageBudget(
                name="okx", limit=1000, window_ms=1000, now_ns=clock
            ),
        },
        global_budget=MessageBudget(
            name="firm", limit=1000, window_ms=1000, now_ns=clock
        ),
        strategy_budgets={
            configured: MessageBudget(
                name="alpha", limit=1, window_ms=1000, now_ns=clock
            )
        },
    )

    ledger.reserve(
        reservation(binance=1, okx=0, hedge=0, strategy_id="alpha")
    )
    with pytest.raises(CapacityReservationError, match="alpha"):
        ledger.reserve(
            reservation(
                opportunity_id="OPP-2",
                binance=1,
                okx=0,
                hedge=0,
                strategy_id="ALPHA",
            )
        )


def test_recovery_times_the_binding_tier_not_the_longest_window() -> None:
    """A tier held back by a ceiling and a tier that is binding answer
    two different questions. Reporting one number for both lets an idle
    daily budget -- blocking nobody -- claim the venue's recovery is a
    day away when it is a second away.
    """

    clock = Clock()
    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=2, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=86_400_000, now_ns=clock
        ),
        priority_ceilings={Priority.RESEARCH: Decimal(60)},
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    plane.venue_budgets["binance"].force_consume(2)
    plane.global_budget.force_consume(67)

    board = plane.dashboard()

    assert board["binding"] is True
    assert board["driver"] == "binance"
    assert board["expected_recovery_ms"] == 1000
    assert board["holding_back"] == [
        {
            "name": "firm",
            "level": "FIRM_MESSAGE_LIMIT",
            "utilization_pct": "67.00",
            "refills_in_ms": 86_400_000,
            "classes_held_back": ["research"],
        }
    ]


def test_a_hedge_priority_does_not_survive_a_stale_feed_at_a_new_stage(
) -> None:
    """The matrix exempts hedging from *capacity* controls by priority,
    but from stale-feed and kill-switch by *stage*. The two axes are
    deliberately different: a strategy that has taken no risk yet cannot
    buy admission on a blind market by labelling its traffic urgent.
    Only a stage that reduces exposure is exempt.
    """

    clock = Clock()
    plane = _tiered_plane(clock)
    plane.feed_monitor = FeedMonitor(stale_after_ms=1, now_ns=clock)
    plane.feed_monitor.record_tick("binance")
    clock.advance_ms(50)

    labelled = _ask(
        plane, stage=OrderStage.NEW, priority=Priority.EMERGENCY_HEDGE
    )
    hedging = _ask(
        plane,
        stage=OrderStage.EMERGENCY_HEDGE,
        priority=Priority.EMERGENCY_HEDGE,
    )

    assert labelled.action is ControlAction.REJECT
    assert labelled.control is ControlKind.STALE_FEED
    assert hedging.action is ControlAction.ADMIT


@pytest.mark.parametrize(
    ("reserved_as", "sent_as"),
    [("alpha", "alpha"), ("Alpha", "alpha"), ("alpha", "ALPHA")],
)
def test_a_reservation_is_drawn_down_whatever_case_the_sender_uses(
    reserved_as: str, sent_as: str
) -> None:
    """A reservation must not disagree with itself.

    ``scoped_charges`` is built from the normalised id -- that is the
    budget the reservation actually charged -- so storing the raw id
    alongside it gave one object two identities. The drawdown gate
    checked the raw one, missed, and charged every message a *second*
    time: three messages drew six off every tier. Under
    ``require_reservation`` it is worse than double-charging, because
    the opportunity holds the capacity and still cannot send its
    first leg.
    """

    clock = Clock()
    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=1000, window_ms=1000, now_ns=clock
        ),
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=1000, window_ms=1000, now_ns=clock
            )
        },
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="o1",
            strategy_id=reserved_as,
            venue_messages={"binance": 3},
            hedge_messages=0,
            notional=Decimal("100"),
            margin=Decimal("10"),
            position_quantity=Decimal("1"),
        )
    )

    actions = [
        _send(plane, strategy_id=sent_as, opportunity_id="o1").action
        for _ in range(3)
    ]

    assert actions == [ControlAction.ADMIT] * 3
    # The reservation is spent, not spent *and* charged again.
    venue = plane.venue_budgets["binance"].snapshot()
    assert (venue.reserved, venue.used) == (0, 3)
    for budget in (plane.global_budget, plane.strategy_budgets["alpha"]):
        item = budget.snapshot()
        assert item.reserved + item.used == 3
    held = plane.ledger.get("o1")
    assert held is not None
    assert held.strategy_id == "alpha"


def test_a_reservation_is_still_refused_to_a_different_strategy() -> None:
    """Folding the identity must not make the gate stop gating.

    A reservation is honoured only for the strategy it was granted to,
    because a drawn-down message skips its own PM and strategy tiers.
    """

    clock = Clock()
    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=1000, window_ms=1000, now_ns=clock
        ),
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
        require_reservation=True,
    )
    plane.feed_monitor.record_tick("binance")
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="o1",
            strategy_id="alpha",
            venue_messages={"binance": 3},
            hedge_messages=0,
            notional=Decimal("100"),
            margin=Decimal("10"),
            position_quantity=Decimal("1"),
        )
    )

    mine = _send(plane, strategy_id="ALPHA", opportunity_id="o1")
    theirs = _send(plane, strategy_id="beta", opportunity_id="o1")

    assert mine.action is ControlAction.ADMIT
    assert theirs.action is ControlAction.REJECT


def test_two_spellings_of_one_strategy_are_refused_at_construction(
) -> None:
    """Folding a config map silently would halve the enforcement the
    operator asked for, so a duplicate has to be a startup error rather
    than a last-wins.
    """

    clock = Clock()
    budgets = {
        "Alpha": MessageBudget(
            name="alpha", limit=1, window_ms=1000, now_ns=clock
        ),
        "alpha": MessageBudget(
            name="alpha", limit=100, window_ms=1000, now_ns=clock
        ),
    }
    with pytest.raises(ValueError, match="duplicate message budget"):
        _tiered_plane(clock, strategy_budgets=budgets)
    with pytest.raises(ValueError, match="duplicate message budget"):
        _tiered_plane(clock, pm_budgets=budgets)


def test_two_spellings_of_one_routing_are_refused_at_construction() -> None:
    """``strategy_pm`` is the one map where last-wins changes an
    enforcement *outcome* rather than which equivalent budget object
    survives: it chooses which manager's allowance is charged. An
    operator who routed a strategy to a one-message manager must not be
    silently rerouted to a hundred-message one.
    """

    clock = Clock()
    with pytest.raises(ValueError, match="duplicate manager routing"):
        _tiered_plane(
            clock,
            pm_budgets={
                "pm-a": MessageBudget(
                    name="pm-a", limit=1, window_ms=1000, now_ns=clock
                ),
                "pm-b": MessageBudget(
                    name="pm-b", limit=100, window_ms=1000, now_ns=clock
                ),
            },
            strategy_pm={"Alpha": "pm-a", "alpha": "pm-b"},
        )


def test_an_impossible_request_names_the_most_senior_tier_it_fails(
) -> None:
    """``_never_fits`` returns ``(budget, level)`` rather than a bool so
    the reported level is trustworthy. With only one blocker at a time
    that promise is indistinguishable from returning whichever tier the
    walk happened to reach last, so this pins two blockers at once.
    """

    clock = Clock()
    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=5, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=1000, window_ms=1000, now_ns=clock
        ),
        instrument_budgets={
            "btcusdt": MessageBudget(
                name="btcusdt", limit=3, window_ms=1000, now_ns=clock
            )
        },
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    # 8 fits neither the venue's 5 nor the instrument's 3.
    blocked = plane._never_fits(
        MessageRequest(
            message_id="m",
            strategy_id="alpha",
            venue="binance",
            stage=OrderStage.NEW,
            priority=Priority.MARKET_MAKING,
            messages=8,
            instrument="btcusdt",
        ),
        "binance",
        Decimal(100),
    )

    assert blocked is not None
    budget, level = blocked
    assert level is ControlLevel.VENUE_LIMIT
    assert budget.name == "binance"

    decision = _send(
        plane, messages=8, instrument="btcusdt", ttl_ms=10_000
    )
    assert decision.action is ControlAction.REJECT
    assert decision.level is ControlLevel.VENUE_LIMIT


def test_two_spellings_of_one_venue_in_a_reservation_are_refused() -> None:
    """Folding a caller's map silently is worse than folding a config
    map silently, because ``reserve`` and ``release`` read the quantity
    from *different* maps: reserve took the firm tier from the caller's
    raw total, release credited it back from the folded one. Every
    dropped duplicate leaked firm capacity permanently, on the success
    path, with no error anywhere.
    """

    clock = Clock()
    ledger = build_ledger(clock, global_limit=100)

    with pytest.raises(
        CapacityReservationError, match="duplicate venue in venue_messages"
    ):
        ledger.reserve(
            ReservationRequest(
                opportunity_id="o1",
                strategy_id="alpha",
                venue_messages={"Binance": 4, "binance": 6},
                hedge_messages=0,
                notional=Decimal("100"),
                margin=Decimal("10"),
                position_quantity=Decimal("1"),
            )
        )

    assert ledger.get("o1") is None
    assert ledger.global_budget.snapshot().reserved == 0
    assert ledger.venue_budgets["binance"].snapshot().reserved == 0


def test_two_spellings_of_one_venue_in_venue_instruments_are_refused(
) -> None:
    """An under-reserved instrument tier is a *disabled* tier: a
    reserved message skips the capacity walk, so the leg whose
    instrument was dropped is never charged and never limited.

    The refusal must also come before anything is taken. Detecting the
    collision at the point of use meant the venue, firm and instrument
    tiers were already reserved, so refusing leaked what it had taken.
    """

    clock = Clock()
    ledger = ReservationLedger(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=1000, now_ns=clock
        ),
        instrument_budgets={
            "btc-perp": MessageBudget(
                name="btc-perp", limit=10, window_ms=1000, now_ns=clock
            ),
            "btc-usdt": MessageBudget(
                name="btc-usdt", limit=10, window_ms=1000, now_ns=clock
            ),
        },
    )

    with pytest.raises(
        CapacityReservationError,
        match="duplicate venue in venue_instruments",
    ):
        ledger.reserve(
            ReservationRequest(
                opportunity_id="s1",
                strategy_id="alpha",
                venue_messages={"binance": 2},
                hedge_messages=0,
                venue_instruments={
                    "Binance": "btc-perp",
                    "binance": "btc-usdt",
                },
                notional=Decimal("100"),
                margin=Decimal("10"),
                position_quantity=Decimal("1"),
            )
        )

    assert ledger.get("s1") is None
    assert ledger.global_budget.snapshot().reserved == 0
    for budget in ledger.instrument_budgets.values():
        assert budget.snapshot().reserved == 0


def test_a_released_reservation_leaves_every_budget_at_zero() -> None:
    """The conservation invariant the leak violated.

    ``_roll`` clears ``used`` but never ``reserved``, so a reservation
    that credits back less than it took is not merely a transient
    accounting error -- it survives every window roll and accumulates.
    Twenty-three cycles of a four-message discrepancy jammed a
    100-message firm budget at 92% held by nothing, and firm capacity
    is the top of the precedence hierarchy, so the terminal state is
    the whole plane refusing everything.
    """

    clock = Clock()
    ledger = ReservationLedger(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=1000, now_ns=clock
            ),
            "okx": MessageBudget(
                name="okx", limit=100, window_ms=1000, now_ns=clock
            ),
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=1000, now_ns=clock
        ),
        instrument_budgets={
            "btc-usdt": MessageBudget(
                name="btc-usdt", limit=100, window_ms=1000, now_ns=clock
            ),
            "btc-perp": MessageBudget(
                name="btc-perp", limit=100, window_ms=1000, now_ns=clock
            ),
        },
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=100, window_ms=1000, now_ns=clock
            )
        },
    )

    for cycle in range(30):
        ledger.reserve(
            ReservationRequest(
                opportunity_id=f"o{cycle}",
                strategy_id="Alpha",
                venue_messages={"Binance": 2, " OKX ": 3},
                hedge_messages=1,
                venue_instruments={
                    "Binance": "BTC-USDT",
                    " OKX ": "btc-perp",
                },
                notional=Decimal("100"),
                margin=Decimal("10"),
                position_quantity=Decimal("1"),
            )
        )
        assert ledger.global_budget.snapshot().reserved == 6
        ledger.release(f"o{cycle}")
        for budget in (
            ledger.global_budget,
            *ledger.venue_budgets.values(),
            *ledger.instrument_budgets.values(),
            *ledger.strategy_budgets.values(),
        ):
            item = budget.snapshot()
            assert item.reserved == 0, (cycle, budget.name, item.reserved)


def test_the_dashboard_names_one_strategy_once() -> None:
    """``affected_strategies`` is the output an operator matches against
    the configured budget map. Reporting the sender's spelling made one
    strategy on one budget appear as three, inflating the apparent blast
    radius during the incident the dashboard exists to explain.
    """

    clock = Clock()
    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=1, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=1000, window_ms=1000, now_ns=clock
        ),
        strategy_budgets={
            "alpha": MessageBudget(
                name="alpha", limit=1000, window_ms=1000, now_ns=clock
            )
        },
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")

    for spelling in ("alpha", "Alpha", "ALPHA"):
        _send(plane, strategy_id=spelling, ttl_ms=10_000)

    board = plane.dashboard()
    assert board["affected_strategies"] == ["alpha"]
    assert set(board["affected_strategies"]) <= set(plane.strategy_budgets)


def test_spending_a_negative_count_cannot_manufacture_capacity() -> None:
    """`consume` is a budget draw; a negative one is a budget credit.

    `consume_reserved` does ``_reserved -= n; _used += n``, so a negative
    both inflates the hold and drives `used` below zero -- and once the
    reservation is released, `available()` reports more than the venue's
    hard limit. That is the one breach the exchange answers by
    disconnecting the session.
    """

    clock = Clock()
    ledger = build_ledger(clock, venue_limit=10)
    ledger.reserve(reservation(binance=3, okx=0, hedge=0))

    with pytest.raises(CapacityReservationError, match="must be positive"):
        ledger.consume("OPP-1", "binance", -5)

    ledger.release("OPP-1")
    budget = ledger.venue_budgets["binance"]
    assert budget.available() <= budget.limit
    assert budget.snapshot().used >= 0
    assert budget.snapshot().reserved == 0


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("venue_messages", {"binance": -4, "okx": 0}),
        ("hedge_messages", -3),
        ("notional", Decimal("-9000")),
        ("margin", Decimal("-5")),
    ],
)
def test_a_reservation_request_cannot_ask_for_negative_resources(
    field_name: str, value: object
) -> None:
    """Each of these is a way to credit a control rather than draw on it.

    A negative venue count makes `try_reserve` succeed holding nothing
    while `release` *grows* the hold, which no window roll clears; a
    negative notional walks straight through the firm risk limit. The
    opportunity layer validates its own inputs, but the ledger is public
    API and that layer is bypassable -- a control whose invariants live
    only in its caller is not a control.
    """

    kwargs: dict[str, object] = {
        "opportunity_id": "OPP-1",
        "strategy_id": "alpha",
        "venue_messages": {"binance": 1, "okx": 0},
        field_name: value,
    }
    with pytest.raises(ValueError):
        ReservationRequest(**kwargs)  # type: ignore[arg-type]


def test_a_negative_leg_cannot_permanently_strand_the_firm_budget() -> None:
    """The consequence the guard exists to prevent, stated as an outcome.

    Before the guard, twenty-five reserve/release cycles left the firm
    tier holding its entire limit against zero open reservations.
    """

    clock = Clock()
    ledger = build_ledger(clock, venue_limit=100, global_limit=100)
    for index in range(25):
        with pytest.raises(ValueError):
            reservation(f"OPP-{index}", binance=-4, okx=0, hedge=0)

    assert ledger.global_budget.snapshot().reserved == 0
    ledger.reserve(reservation("OPP-real", binance=10, okx=0, hedge=0))
    assert ledger.global_budget.snapshot().reserved == 10


def test_a_venue_may_take_part_without_sending_on_this_pass() -> None:
    """Zero is legitimate, and tightening the guard to `<= 0` broke it.

    A zero-message leg names a venue that belongs to this opportunity's
    budget structure but sends nothing right now. It draws nothing and
    credits nothing; only a negative is a hazard.
    """

    clock = Clock()
    ledger = build_ledger(clock)
    ledger.reserve(reservation(binance=2, okx=0, hedge=0))
    assert ledger.venue_budgets["okx"].snapshot().reserved == 0
    assert ledger.venue_budgets["binance"].snapshot().reserved == 2


def test_a_short_leg_is_a_negative_quantity_and_still_charges_size() -> None:
    """`position_quantity` carries direction, so its sign must survive.

    Validating it away would forbid shorting outright. It is safe
    unvalidated because the check, the charge and the release all take
    `abs` of it -- the three can never disagree about magnitude.
    """

    clock = Clock()
    ledger = build_ledger(
        clock, limits=RiskLimits(max_position_quantity=Decimal("1.5"))
    )
    ledger.reserve(reservation("OPP-1", quantity="-1", hedge=0))
    with pytest.raises(CapacityReservationError, match="position"):
        ledger.reserve(reservation("OPP-2", quantity="-1", hedge=0))

    ledger.release("OPP-1")
    ledger.reserve(reservation("OPP-3", quantity="-1", hedge=0))


def test_the_firm_tier_must_dominate_every_tier_it_bounds() -> None:
    """The hierarchy's invariant, asserted rather than assumed.

    Every subordinate tier's demand is a subset of the firm tier's, so a
    firm hold below one of them means the top of `CONTROL_PRECEDENCE`
    overstates its availability for the life of the reservation. The two
    agree today only because one map feeds both -- arithmetic, not a
    checked property.

    This is a second line of defence, so the test reaches it by
    disabling the first: `__post_init__` now rejects the negative hedge
    that was the original door in.
    """

    clock = Clock()
    ledger = build_ledger(clock, venue_limit=100, global_limit=100)
    request = reservation(binance=5, okx=0, hedge=0)
    object.__setattr__(request, "hedge_messages", -3)

    with pytest.raises(CapacityReservationError, match="firm demand"):
        ledger.reserve(request)

    assert ledger.global_budget.snapshot().reserved == 0
    assert ledger.venue_budgets["binance"].snapshot().reserved == 0


def test_a_request_offers_no_total_computed_from_the_raw_map() -> None:
    """The total must come from the folded map `release` credits back.

    A public property summing the unfolded map is how the two sides came
    to disagree and leak firm capacity permanently; deleting it removes
    the seam rather than documenting it.
    """

    request = reservation()
    assert not hasattr(request, "total_messages")
    assert hasattr(Reservation, "total_messages")


def test_a_queued_message_stores_a_normalised_instrument() -> None:
    """Stored identity is normalised, and that is asserted directly.

    `_tier` folds again at lookup, so no downstream behaviour depends on
    this today -- a mutation that pushed the raw spelling survived the
    whole suite. It is kept rather than deleted because round 6 showed
    the real hazard is an object holding a raw identity beside folded
    keys: one thing with two names. Asserting the field itself makes
    the property load-bearing instead of incidentally true.
    """

    clock = Clock()
    plane = _tiered_plane(clock)
    plane.venue_budgets["binance"].force_consume(
        plane.venue_budgets["binance"].limit
    )
    decision = _send(
        plane,
        instrument="BTC-USD",
        ttl_ms=10_000,
        priority=Priority.QUOTE_REFRESH,
    )
    assert decision.action is ControlAction.QUEUE

    message = plane.queue.peek()
    assert message is not None
    assert message.instrument == "btc-usd"


def test_a_tick_from_a_fast_venue_clock_cannot_disable_the_control() -> None:
    """A feed can never be fresher than now, so age can never go negative.

    `is_stale` is `age >= threshold`, and a negative age never exceeds
    it -- so a venue whose clock runs ahead would have the stale-feed
    control silently switched off, admitting new quoting into a dead
    feed. `is_stale` already treats an unknown venue as stale rather
    than fresh; this is the same choice for the one input that failed
    in the unsafe direction.
    """

    clock = Clock()
    monitor = FeedMonitor(stale_after_ms=100, now_ns=clock)
    monitor.record_tick("binance", timestamp_ns=clock() + 3600 * 10**9)

    assert monitor.age_ns("binance") == 0
    assert monitor.is_stale("binance") is False

    clock.advance_ms(250)
    assert monitor.is_stale("binance") is True
    assert monitor.to_dict()["venues"]["binance"]["age_ms"] >= 0


def test_a_refusal_is_counted_against_the_tier_that_refused() -> None:
    """The counter must agree with the reason the message carries.

    `_never_fits` names the most senior tier that can never supply the
    request, but the refusal was counted against the tier that merely
    bound first. An operator drilling into per-tier rejection counts to
    decide which limit to raise was pointed at the wrong limit.
    """

    clock = Clock()
    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=10, window_ms=1000, now_ns=clock
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=1000, window_ms=1000, now_ns=clock
        ),
        instrument_budgets={
            "btc": MessageBudget(
                name="btc", limit=3, window_ms=1000, now_ns=clock
            )
        },
        priority_ceilings={Priority.QUOTE_REFRESH: Decimal(80)},
        feed_monitor=FeedMonitor(stale_after_ms=10**9, now_ns=clock),
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")
    plane.venue_budgets["binance"].force_consume(10)

    decision = _send(
        plane,
        messages=8,
        instrument="btc",
        priority=Priority.QUOTE_REFRESH,
        ttl_ms=10_000,
    )
    assert decision.action is ControlAction.REJECT
    assert "btc" in decision.reason
    assert plane.instrument_budgets["btc"].snapshot().rejected == 1
    assert plane.venue_budgets["binance"].snapshot().rejected == 0


def test_a_tick_dated_in_the_past_is_honoured_not_discarded() -> None:
    """The other half of the clamp: a past stamp must still count.

    `min(timestamp_ns, now)` has two arguments, and only the `now` half
    was pinned. Discarding the caller's timestamp entirely survived the
    whole suite -- and it is the more dangerous direction, because a
    feed handler stamping exchange time while running behind would read
    as perfectly fresh no matter how far it lagged. A caller feeding
    exchange tick times is the documented normal case.
    """

    clock = Clock()
    monitor = FeedMonitor(stale_after_ms=100, now_ns=clock)
    monitor.record_tick("binance", timestamp_ns=clock() - 500 * 10**6)

    assert monitor.age_ns("binance") == 500 * 10**6
    assert monitor.is_stale("binance") is True
    assert monitor.to_dict()["venues"]["binance"]["age_ms"] == 500
    assert monitor.clamped("binance") == 0


def test_a_venue_dating_ticks_ahead_is_counted_not_just_corrected() -> None:
    """Clamping is safe but silent, so the evidence is kept separately.

    A venue whose clock runs ahead renders identically to a healthy one
    once its ticks are clamped. Every previous defect in this area was
    the dashboard telling an operator something untrue about which
    thing is wrong; the count makes a data-quality fault visible
    without touching any decision.
    """

    clock = Clock()
    monitor = FeedMonitor(stale_after_ms=100, now_ns=clock)
    monitor.record_tick("binance", timestamp_ns=clock() + 3600 * 10**9)
    monitor.record_tick("binance", timestamp_ns=clock() + 10**9)
    monitor.record_tick("binance")
    monitor.record_tick("okx", timestamp_ns=clock() - 10**6)
    # Exactly `now` was not moved by `min`, so it is not a clamp.
    monitor.record_tick("okx", timestamp_ns=clock())

    assert monitor.clamped("binance") == 2
    assert monitor.clamped("okx") == 0
    # The public method folds too. External monitoring calls this with
    # whatever spelling the config uses, and a venue an hour ahead
    # reporting 0 is the whole failure this counter exists to prevent.
    assert monitor.clamped("BINANCE") == 2
    venues = monitor.to_dict()["venues"]
    assert venues["binance"]["clamped_ticks"] == 2
    assert venues["okx"]["clamped_ticks"] == 0


def test_a_tick_recorded_in_mixed_case_reaches_the_same_feed() -> None:
    """The write side folds too, and nothing held it there.

    Every `record_tick` call in this suite passed an already-lowercase
    name, so the fold was exercised only on the read side -- the same
    half-covered pattern that hid the `_normalise_scopes` and
    `_normalise_routes` defects. Without it a live feed reports as
    never having ticked, every new order on that venue is rejected
    indefinitely, and the dashboard files it under a second key. It
    fails safe, so it would look like the venue simply went quiet.

    Two spellings are one feed, so last-write-wins is correct here --
    unlike a budget map, where merging two entries discards one.
    """

    clock = Clock()
    monitor = FeedMonitor(stale_after_ms=100, now_ns=clock)
    monitor.record_tick("Binance")

    assert monitor.is_stale("binance") is False
    assert monitor.age_ns("binance") == 0
    assert list(monitor.to_dict()["venues"]) == ["binance"]

    monitor.record_tick("BINANCE")
    assert list(monitor.to_dict()["venues"]) == ["binance"]


def test_a_feed_recorded_in_mixed_case_still_admits_orders() -> None:
    """The consequence, at the plane rather than the monitor.

    A venue whose feed handler uses the configured spelling would have
    every new order rejected as though the feed had never ticked.
    """

    clock = Clock()
    plane = _tiered_plane(clock)
    plane.feed_monitor = FeedMonitor(stale_after_ms=100, now_ns=clock)
    plane.feed_monitor.record_tick("Binance")

    decision = _send(plane, ttl_ms=10_000)
    assert decision.action is ControlAction.ADMIT


def _instrument_ledger(clock: Clock) -> ReservationLedger:
    return ReservationLedger(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=1000, now_ns=clock
            ),
            "okx": MessageBudget(
                name="okx", limit=100, window_ms=1000, now_ns=clock
            ),
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=1000, now_ns=clock
        ),
        instrument_budgets={
            "btc": MessageBudget(
                name="btc", limit=100, window_ms=1000, now_ns=clock
            ),
            "eth": MessageBudget(
                name="eth", limit=100, window_ms=1000, now_ns=clock
            ),
        },
    )


@pytest.mark.parametrize("spelling", ["binance", "Binance", "BINANCE"])
def test_a_spend_can_name_its_venue_in_any_case(spelling: str) -> None:
    """`consume` folds the venue it is given, and nothing held it there.

    Unfolded, a legitimate spend is refused with "reserved no capacity
    on Binance" for a leg that plainly holds a reservation, so the
    message is either retried forever or sent unaccounted.
    """

    clock = Clock()
    ledger = build_ledger(clock)
    ledger.reserve(reservation(binance=3, okx=0, hedge=0))

    ledger.consume("OPP-1", spelling, 2)
    assert ledger.venue_budgets["binance"].snapshot().used == 2


@pytest.mark.parametrize("spelling", ["binance", "Binance", "BINANCE"])
def test_a_capacity_question_can_name_its_venue_in_any_case(
    spelling: str,
) -> None:
    """`room_for` answering `None` for a held leg is round 6 again.

    A caller told there is no reservation takes the capacity walk
    instead, and the leg is charged a second time.
    """

    clock = Clock()
    ledger = build_ledger(clock)
    ledger.reserve(reservation(binance=3, okx=0, hedge=0))

    assert ledger.room_for("OPP-1", spelling, 2) == "venue"


@pytest.mark.parametrize("spelling", ["binance", "Binance", "BINANCE"])
def test_a_hedge_spend_charges_its_venue_in_any_case(spelling: str) -> None:
    """The unfolded lookup silently skips the venue charge entirely.

    `consume_hedge` looks the venue budget up with `.get`, so an
    unfolded name yields `None` and the `force_consume` is skipped
    without error: the hedge goes to the exchange and that venue's
    budget is never drawn. That is capacity the venue's own limit does
    not know it has given out.
    """

    clock = Clock()
    ledger = build_ledger(clock)
    ledger.reserve(reservation(binance=2, okx=0, hedge=3))

    ledger.consume_hedge("OPP-1", spelling, 2)
    assert ledger.venue_budgets["binance"].snapshot().used == 2


@pytest.mark.parametrize("spelling", ["binance", "Binance", "BINANCE"])
def test_a_leg_instrument_is_found_whatever_case_the_caller_uses(
    spelling: str,
) -> None:
    """`instrument_for` falling through returns the wrong instrument.

    The per-leg override is missed and the opportunity-level name is
    returned instead, so `room_for`'s instrument check compares a leg
    against an instrument it does not trade and refuses it.
    """

    clock = Clock()
    ledger = _instrument_ledger(clock)
    held = ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            venue_messages={"binance": 2},
            instrument="BTC",
            venue_instruments={"Binance": "ETH"},
        )
    )

    assert held.instrument_for(spelling) == "eth"
    assert ledger.room_for("OPP-1", spelling, 1, instrument="ETH") == "venue"


def test_a_reservation_stores_folded_instrument_names() -> None:
    """One object must not hold two names for one thing.

    Round 6 was `strategy_id` stored raw beside folded keys. The
    instrument fields are the same shape, and `instrument_for` folding
    on read papers over the stored value rather than fixing it.
    """

    clock = Clock()
    ledger = _instrument_ledger(clock)
    held = ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="Alpha",
            venue_messages={"Binance": 2},
            instrument="BTC",
            venue_instruments={"Binance": "ETH"},
        )
    )

    assert held.instrument == "btc"
    assert held.strategy_id == "alpha"
    assert held.venue_instruments == {"binance": "eth"}
    assert set(held.venue_messages) == {"binance"}


def test_a_mixed_case_opportunity_instrument_still_reserves_the_hedge() -> None:
    """The hedge's instrument tier is keyed on the raw name otherwise.

    The hedge venue is only chosen at unwind time, so its instrument can
    only be the opportunity-level one. Unfolded, `instrument_budgets`
    misses, no tier is charged for the hedge, and the hedge escapes the
    instrument limit that every other leg obeys.
    """

    clock = Clock()
    ledger = _instrument_ledger(clock)
    ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            venue_messages={"binance": 2},
            instrument="BTC",
            hedge_messages=3,
        )
    )

    assert ledger.instrument_budgets["btc"].snapshot().reserved == 5


def test_scoped_tiers_fold_the_leg_names_they_are_given() -> None:
    """A second line of defence on a private helper, pinned directly.

    `reserve` folds `venue_messages` before calling `_scoped`, so this
    cannot be reached through the public API today. It is kept because
    `_scoped` takes venue names as an argument and a second caller is
    one refactor away, and it is asserted here rather than left to be
    incidentally true.
    """

    clock = Clock()
    ledger = _instrument_ledger(clock)
    scoped = ledger._scoped(
        "alpha", "btc", {"BINANCE": "btc"}, {"BINANCE": 2}, False
    )
    sources = {source for _key, _budget, srcs in scoped for source in srcs}
    assert sources == {"binance"}


def test_instrument_for_folds_a_directly_constructed_reservation() -> None:
    """`Reservation` is public and does not normalise itself.

    `reserve` folds every name on the way in, so through the ledger the
    stored values are already correct and this fold is redundant. It is
    not redundant for a `Reservation` built directly -- a test double, a
    restored snapshot, a future recovery path -- and `instrument_for`
    is the accessor every consumer goes through.
    """

    held = Reservation(
        opportunity_id="OPP-1",
        strategy_id="alpha",
        venue_messages={"binance": 2},
        hedge_messages=0,
        notional=Decimal(0),
        margin=Decimal(0),
        position_quantity=Decimal(0),
        instrument="BTC",
        venue_instruments={"binance": "ETH"},
    )

    assert held.instrument_for("binance") == "eth"
    assert held.instrument_for("okx") == "btc"


# -- mutual exclusion --------------------------------------------------
#
# The two stress tests above race probabilistically: they start eight
# threads and hope the interleaving that breaks the invariant actually
# occurs. That works when the window is wide -- the original
# check-then-act admission bug lost 54 trials in 60 -- but it is blind
# to a window only a few bytecodes long. Removing the lock from
# ``try_reserve`` leaves exactly such a window, and seventy-five stress
# trials never once caught it.
#
# These tests do not hope. They park two threads inside the critical
# section at the seam between reading a value and acting on it, so the
# interleaving is forced rather than sampled. The value is captured
# *before* parking because that is precisely what a race is: a thread
# loads a value, is preempted, and then acts on what it loaded. Reading
# it again after the park would model a thread that never raced.
#
# Nothing about the code under test changes -- the method bodies, locks
# included, are inherited unaltered. Only the scheduling is pinned
# down, and only at a point where the interpreter is genuinely free to
# switch threads anyway.


class _Rendezvous:
    """Proves two threads were inside one critical section together.

    If the lock holds, the second thread can never arrive, so the wait
    times out and ``both_inside`` stays false. That timeout is the
    positive signal, not a failure.
    """

    def __init__(self) -> None:
        self._barrier = threading.Barrier(2)
        self.both_inside = False
        self.arrived: set[str] = set()

    def wait(self) -> None:
        self.arrived.add(threading.current_thread().name)
        try:
            self._barrier.wait(timeout=0.25)
            self.both_inside = True
        except threading.BrokenBarrierError:
            pass

    def assert_exclusive(self, *, arrivals: int) -> None:
        """The seam was reached as expected, and never by two at once.

        ``both_inside is False`` on its own cannot tell a working lock
        from a harness that never contended at all -- a setup call that
        quietly spends the rendezvous leaves the test unable to fail,
        which is exactly what happened to the spend test before this
        check existed. Counting who actually arrived separates the two.

        ``arrivals`` is two where both threads run the section, and one
        where the second is turned back before reaching it: the ledger
        returns the existing reservation without ever pricing risk
        again, which is the idempotency the lock exists to protect.
        """

        assert len(self.arrived) == arrivals, self.arrived
        assert self.both_inside is False


class _ParkedBudget(MessageBudget):
    """A budget that parks on the first two reads of one field."""

    _park_on = ""

    def __init__(self, *, park: _Rendezvous, **kwargs: object) -> None:
        self._armed = False
        self._park = park
        self._parked_threads: set[str] = set()
        self._values: dict[str, int] = {}
        super().__init__(**kwargs)  # type: ignore[arg-type]

    def arm(self) -> None:
        """Start parking only now.

        Setting the budget up reads these fields too, and a read during
        setup would spend the rendezvous before the threads exist --
        leaving the test unable to fail no matter what the code does.
        """

        self._parked_threads = set()
        self._armed = True

    def disarm(self) -> None:
        """Stop parking, so asserting on the result cannot join in."""

        self._armed = False

    def _parked(self, field: str, value: int) -> int:
        """Park each thread once, on its first read of the field.

        Once per *thread*, not once per read: ``used += n`` reads the
        field again to increment it, so counting reads globally lets
        the first thread spend both slots and leaves the second never
        parking at all.
        """

        if not self._armed or field != self._park_on:
            return value
        name = threading.current_thread().name
        if name not in self._parked_threads:
            self._parked_threads.add(name)
            self._park.wait()
        return value

    @property
    def _used(self) -> int:
        return self._parked("_used", self._values["_used"])

    @_used.setter
    def _used(self, value: int) -> None:
        self._values["_used"] = value

    @property
    def _reserved(self) -> int:
        return self._parked("_reserved", self._values["_reserved"])

    @_reserved.setter
    def _reserved(self, value: int) -> None:
        self._values["_reserved"] = value


def _run_both(target) -> None:
    threads = [
        threading.Thread(target=target, name=f"racer-{index}")
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


@pytest.mark.concurrency
def test_two_threads_cannot_both_take_the_last_message_slot() -> None:
    """The venue allowance is what the exchange itself enforces.

    ``try_reserve`` decides on ``limit - used - reserved`` and then
    increments. Both threads are parked on the last read that decision
    makes, so both see the free slot. Only the lock stops both taking
    it, and granting two messages against a limit of one is a
    rate-limit breach at the venue.
    """

    class _Budget(_ParkedBudget):
        _park_on = "_reserved"

    park = _Rendezvous()
    budget = _Budget(
        park=park, name="binance", limit=1, window_ms=100_000
    )
    granted: list[int] = []

    budget.arm()
    _run_both(lambda: granted.append(1) if budget.try_reserve(1) else None)
    budget.disarm()

    assert len(granted) == 1
    assert budget.snapshot().reserved == 1
    park.assert_exclusive(arrivals=2)


@pytest.mark.concurrency
def test_two_threads_cannot_lose_a_spend_against_the_same_budget() -> None:
    """A lost update under-counts what was sent, so the next window
    over-admits.

    ``consume_reserved`` does ``used += messages``. Two threads that
    both read the old value write the same total back, and one send
    vanishes from the budget while still having reached the exchange.
    """

    class _Budget(_ParkedBudget):
        _park_on = "_used"

    park = _Rendezvous()
    budget = _Budget(
        park=park, name="binance", limit=10, window_ms=100_000
    )
    assert budget.try_reserve(2) is True

    budget.arm()
    _run_both(lambda: budget.consume_reserved(1))
    budget.disarm()

    assert budget.snapshot().used == 2
    park.assert_exclusive(arrivals=2)


@pytest.mark.concurrency
def test_two_releases_cannot_lose_a_venue_reservation() -> None:
    """A lost release leaks allowance that never comes back.

    ``release`` writes ``reserved - messages``. Two threads that both
    read the old value write the same total back, so one release is
    dropped and the venue's allowance is permanently smaller than the
    exchange actually grants. Nothing ever refills it: the budget
    refuses orders it should admit, quietly, for the life of the
    process.
    """

    class _Budget(_ParkedBudget):
        _park_on = "_reserved"

    park = _Rendezvous()
    budget = _Budget(
        park=park, name="binance", limit=10, window_ms=100_000
    )
    assert budget.try_reserve(2) is True

    budget.arm()
    _run_both(lambda: budget.release(1))
    budget.disarm()

    assert budget.snapshot().reserved == 0
    assert budget.available() == 10
    park.assert_exclusive(arrivals=2)


@pytest.mark.concurrency
def test_two_hedges_cannot_lose_a_forced_spend() -> None:
    """Exempt traffic still has to be counted.

    ``force_consume`` is how risk-reducing messages spend past the
    limit, and the overage is recorded precisely so it stays visible.
    A lost update hides a hedge that really did reach the exchange,
    which is the one thing the overage accounting exists to show.
    """

    class _Budget(_ParkedBudget):
        _park_on = "_used"

    park = _Rendezvous()
    budget = _Budget(
        park=park, name="binance", limit=10, window_ms=100_000
    )

    budget.arm()
    _run_both(lambda: budget.force_consume(1))
    budget.disarm()

    assert budget.snapshot().used == 2
    park.assert_exclusive(arrivals=2)


class _ParkedLedger(ReservationLedger):
    """A ledger that parks both callers past the idempotency check.

    ``_check_risk`` is the first thing ``reserve`` does after deciding
    the opportunity has no reservation yet, so parking there holds open
    the whole window between that decision and recording the result.
    """

    def __init__(self, *, park: _Rendezvous, **kwargs: object) -> None:
        self._park = park
        super().__init__(**kwargs)  # type: ignore[arg-type]

    def _check_risk(self, request: ReservationRequest) -> None:
        self._park.wait()
        return super()._check_risk(request)


@pytest.mark.concurrency
def test_one_opportunity_reserved_twice_at_once_takes_capacity_once(
) -> None:
    """Idempotency per opportunity is what stops duplicate orders.

    ``reserve`` returns the existing reservation if there is one, but
    the check and the record are separated by the entire multi-tier
    take. Two threads carrying the same opportunity id both pass the
    check without the lock, and the opportunity is then reserved twice:
    two reservation objects, venue capacity charged twice, and the firm
    notional counted twice against its limit.
    """

    park = _Rendezvous()
    ledger = _ParkedLedger(
        park=park,
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=100_000
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=100_000
        ),
    )
    seen: list[int] = []

    def reserve_once() -> None:
        seen.append(
            id(
                ledger.reserve(
                    ReservationRequest(
                        opportunity_id="OPP-1",
                        strategy_id="alpha",
                        instrument="btc",
                        venue_messages={"binance": 4},
                        hedge_messages=0,
                        notional=Decimal(1000),
                        margin=Decimal(100),
                        position_quantity=Decimal(1),
                        priority=Priority.MARKET_MAKING,
                    )
                )
            )
        )

    _run_both(reserve_once)

    assert len(seen) == 2
    assert len(set(seen)) == 1
    assert ledger.venue_budgets["binance"].snapshot().reserved == 4
    assert ledger.global_budget.snapshot().reserved == 4
    assert ledger.reserved_notional == Decimal(1000)
    assert ledger.reserved_margin == Decimal(100)
    park.assert_exclusive(arrivals=1)


class _NotionalLedger(ReservationLedger):
    """A ledger that parks on the running firm notional.

    ``release`` is already safe against being called twice for one
    opportunity -- ``dict.pop`` decides that atomically. The exposed
    seam is two *different* opportunities closing at once, both
    subtracting from the same firm total.
    """

    def __init__(self, *, park: _Rendezvous, **kwargs: object) -> None:
        self._armed = False
        self._park = park
        self._parked_threads: set[str] = set()
        self._value = Decimal(0)
        super().__init__(**kwargs)  # type: ignore[arg-type]

    def arm(self) -> None:
        self._parked_threads = set()
        self._armed = True

    def disarm(self) -> None:
        self._armed = False

    @property
    def _notional(self) -> Decimal:
        value = self._value
        if self._armed:
            name = threading.current_thread().name
            if name not in self._parked_threads:
                self._parked_threads.add(name)
                self._park.wait()
        return value

    @_notional.setter
    def _notional(self, value: Decimal) -> None:
        self._value = value


@pytest.mark.concurrency
def test_two_opportunities_closing_at_once_free_all_the_notional(
) -> None:
    """Stranded notional refuses every later opportunity.

    Firm notional is the aggregate risk reservation, and it is only
    ever given back here. Two opportunities closing together both read
    the running total and both write their own answer, so one release
    is lost and the firm looks permanently more exposed than it is.
    Every subsequent opportunity is then measured against a limit that
    is short by the stranded amount, and nothing ever clears it.
    """

    park = _Rendezvous()
    ledger = _NotionalLedger(
        park=park,
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=100_000
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=100_000
        ),
    )
    for opportunity in ("OPP-1", "OPP-2"):
        ledger.reserve(
            ReservationRequest(
                opportunity_id=opportunity,
                strategy_id="alpha",
                instrument="btc",
                venue_messages={"binance": 4},
                hedge_messages=0,
                notional=Decimal(1000),
                margin=Decimal(100),
                position_quantity=Decimal(1),
                priority=Priority.MARKET_MAKING,
            )
        )
    assert ledger.reserved_notional == Decimal(2000)

    closing = iter(("OPP-1", "OPP-2"))
    guard = threading.Lock()

    def close_one() -> None:
        with guard:
            opportunity = next(closing)
        ledger.release(opportunity)

    ledger.arm()
    _run_both(close_one)
    ledger.disarm()

    assert ledger.reserved_notional == Decimal(0)
    park.assert_exclusive(arrivals=2)


class _ConsumedLedger(ReservationLedger):
    """A ledger that parks on the per-opportunity spend record.

    ``consume`` reads how much of the reservation is already spent,
    decides the leg still fits, and only then records it. Parking on
    the read of that record holds the decision open.
    """

    def __init__(self, *, park: _Rendezvous, **kwargs: object) -> None:
        self._armed = False
        self._park = park
        self._parked_threads: set[str] = set()
        self._value: dict[str, dict[str, int]] = {}
        super().__init__(**kwargs)  # type: ignore[arg-type]

    def arm(self) -> None:
        self._parked_threads = set()
        self._armed = True

    def disarm(self) -> None:
        self._armed = False

    @property
    def _consumed(self) -> dict[str, dict[str, int]]:
        value = self._value
        if self._armed:
            name = threading.current_thread().name
            if name not in self._parked_threads:
                self._parked_threads.add(name)
                self._park.wait()
        return value

    @_consumed.setter
    def _consumed(self, value: dict[str, dict[str, int]]) -> None:
        self._value = value


@pytest.mark.concurrency
def test_two_legs_cannot_spend_the_same_reserved_message() -> None:
    """Spending past the reservation is a breach at the venue.

    ``consume`` works out how much of the reservation is left and
    refuses anything larger. Two legs of one opportunity settling
    together both read the same spend record, both conclude the last
    reserved message is still theirs, and both send it. The venue sees
    two messages where one was reserved, which is the rate limit the
    reservation existed to respect.
    """

    park = _Rendezvous()
    ledger = _ConsumedLedger(
        park=park,
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=100_000
            )
        },
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=100_000
        ),
    )
    ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            instrument="btc",
            venue_messages={"binance": 1},
            hedge_messages=0,
            notional=Decimal(1000),
            margin=Decimal(100),
            position_quantity=Decimal(1),
            priority=Priority.MARKET_MAKING,
        )
    )
    refused: list[str] = []

    def spend_one() -> None:
        try:
            ledger.consume("OPP-1", "binance", 1)
        except CapacityReservationError as error:
            refused.append(str(error))

    ledger.arm()
    _run_both(spend_one)
    ledger.disarm()

    assert len(refused) == 1
    assert "has 0 of 1 reserved messages left" in refused[0]
    assert ledger.venue_budgets["binance"].snapshot().used == 1
    assert ledger.global_budget.snapshot().used == 1
    park.assert_exclusive(arrivals=2)


class _SettleLedger(ReservationLedger):
    """A ledger that parks between checking a spend and recording it.

    ``consume_hedge`` reads how much hedge allowance is left, refuses
    anything larger, and only then records the spend. The gap between
    the two is where ``_settle_scoped`` runs, so parking there holds an
    approved-but-unrecorded decision open for the other thread.

    Parking on the read of the spend record instead would prove
    nothing: the record is re-read after the park to compute what is
    left, so the loser would see the winner's write and correctly
    refuse -- a thread that never raced.
    """

    def __init__(self, *, park: _Rendezvous, **kwargs: object) -> None:
        self._armed = False
        self._park = park
        self._parked_threads: set[str] = set()
        super().__init__(**kwargs)  # type: ignore[arg-type]

    def arm(self) -> None:
        self._parked_threads = set()
        self._armed = True

    def disarm(self) -> None:
        self._armed = False

    def _settle_scoped(self, *args: object, **kwargs: object) -> None:
        if self._armed:
            name = threading.current_thread().name
            if name not in self._parked_threads:
                self._parked_threads.add(name)
                self._park.wait()
        super()._settle_scoped(*args, **kwargs)  # type: ignore[arg-type]


@pytest.mark.concurrency
def test_two_hedges_cannot_both_spend_one_reserved_allowance() -> None:
    """The exempt path is the one that must not over-transmit.

    A hedge is charged to the venue unconditionally -- nothing may
    block it -- so the reserved allowance is the *only* thing standing
    between a hedge and an unbounded venue charge. Two unwind legs
    settling together both read the same remaining allowance, both
    conclude the last reserved message is theirs, and both send.

    The firm budget here is sized to exactly what the two
    opportunities reserved, so the overspend has nowhere to hide: the
    extra message is drawn from the shared reserved pool, which is to
    say out of the *other* opportunity's hedge reservation. That
    reservation was the promise that its unwind could always be paid
    for, and it is withdrawn by an opportunity it has no relationship
    with.
    """

    park = _Rendezvous()
    firm = MessageBudget(name="firm", limit=11, window_ms=100_000)
    ledger = _SettleLedger(
        park=park,
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=100, window_ms=100_000
            )
        },
        global_budget=firm,
    )
    for name, hedge in (("OPP-1", 1), ("OPP-2", 8)):
        ledger.reserve(
            ReservationRequest(
                opportunity_id=name,
                strategy_id="alpha",
                instrument="btc",
                venue_messages={"binance": 1},
                hedge_messages=hedge,
                notional=Decimal(1000),
                margin=Decimal(100),
                position_quantity=Decimal(1),
                priority=Priority.MARKET_MAKING,
            )
        )
    refused: list[str] = []

    def hedge_once() -> None:
        try:
            ledger.consume_hedge("OPP-1", "binance", 1)
        except CapacityReservationError as error:
            refused.append(str(error))

    ledger.arm()
    _run_both(hedge_once)
    ledger.disarm()

    assert len(refused) == 1
    assert "has 0 of 1 reserved hedge messages left" in refused[0]

    for _ in range(8):
        ledger.consume_hedge("OPP-2", "binance", 1)
    ledger.consume("OPP-2", "binance", 1)
    ledger.consume("OPP-1", "binance", 1)

    spent = firm.snapshot()
    assert spent.used == 11
    assert spent.reserved == 0
    park.assert_exclusive(arrivals=1)


class _ParkedCeilingBudget(MessageBudget):
    """A budget that parks between deciding a ceiling and acting on it.

    The verdict is computed by ``super()`` first and returned after the
    park, so the caller acts on what it read before being preempted --
    which is what a thread losing this race actually does.

    Where the park lands is the whole point. Both threads reach the
    advisory read in ``_binds`` first, so both are held there until
    each has decided it is below the ceiling. Releasing them then
    stages the exact race: two admissions that have each read
    "there is room" and are about to act on it.
    """

    def __init__(self, *, park: _Rendezvous, **kwargs: object) -> None:
        self._armed = False
        self._park = park
        self._parked_threads: set[str] = set()
        super().__init__(**kwargs)  # type: ignore[arg-type]

    def arm(self) -> None:
        self._parked_threads = set()
        self._armed = True

    def disarm(self) -> None:
        self._armed = False

    def above_ceiling(
        self, ceiling_pct: Decimal, messages: int = 0
    ) -> bool:
        verdict = super().above_ceiling(ceiling_pct, messages)
        if self._armed:
            name = threading.current_thread().name
            if name not in self._parked_threads:
                self._parked_threads.add(name)
                self._park.wait()
        return verdict


@pytest.mark.concurrency
def test_two_admissions_cannot_both_clear_one_priority_ceiling() -> None:
    """The ceiling is headroom held for traffic that outranks this.

    A ceiling is not a budget: being past a 50% share is nowhere near
    being past the limit, so the hard limit never catches an overshoot
    and the compare-and-increment that protects it is no help. Two
    research messages that both read "still below the ceiling" both
    reserve, and the class ends up holding six against a share of five.

    What it costs is the point. The overshoot comes out of the headroom
    reserved for higher-priority traffic -- ultimately the capacity an
    emergency hedge is promised -- taken by the lowest-priority class
    on the system.

    The same two admissions run one after the other admit one and
    reject the other, so the concurrent outcome must match.
    """

    park = _Rendezvous()
    venue = _ParkedCeilingBudget(
        park=park, name="binance", limit=10, window_ms=100_000
    )
    plane = ExecutionControlPlane(
        venue_budgets={"binance": venue},
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=100_000
        ),
        priority_ceilings={Priority.RESEARCH: Decimal(50)},
    )
    plane.feed_monitor.record_tick("binance")
    actions: list[str] = []

    def admit_three() -> None:
        actions.append(
            plane.evaluate(
                MessageRequest(
                    message_id=threading.current_thread().name,
                    strategy_id="alpha",
                    venue="binance",
                    stage=OrderStage.NEW,
                    priority=Priority.RESEARCH,
                    messages=3,
                    instrument="btc",
                )
            ).action.name
        )

    venue.arm()
    _run_both(admit_three)
    venue.disarm()

    assert sorted(actions) == ["ADMIT", "REJECT"]
    held = venue.snapshot()
    assert held.used + held.reserved == 3
    # Unlike the tests above, contention here is the *premise*, not the
    # thing being ruled out: the ceiling read is advisory and two
    # threads are entitled to pass it together. Asserting they did is
    # what stops this test quietly passing because the race never
    # happened. What must hold is that the take re-tests the ceiling
    # under the acquisition that reserves.
    assert park.both_inside is True


@pytest.mark.concurrency
def test_a_drain_and_an_admission_cannot_both_clear_one_ceiling() -> None:
    """Draining applies the ceiling too, and races the same way.

    ``pop_if`` holds the queue lock for the whole scan, so two drains
    cannot interleave -- which is exactly why the drain path looks safe
    and is not. An admission does not take the queue lock, so it can
    slip between a drain's ceiling test and its reservation, and then
    both hold capacity the ceiling said only one of them could have.

    Parking the drain on its ceiling test shows which side of the fix
    the code is on without asserting anything about timing: while the
    test and the take share an acquisition the budget lock is held
    across the park, so the admission cannot even read the ceiling
    until the drain has finished reserving.
    """

    clock = Clock()
    park = _Rendezvous()
    venue = _ParkedCeilingBudget(
        park=park, name="binance", limit=10, window_ms=1000, now_ns=clock
    )
    plane = ExecutionControlPlane(
        venue_budgets={"binance": venue},
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=1000, now_ns=clock
        ),
        feed_monitor=FeedMonitor(stale_after_ms=100, now_ns=clock),
        priority_ceilings={Priority.RESEARCH: Decimal(50)},
        now_ns=clock,
    )
    plane.feed_monitor.record_tick("binance")

    def research(identifier: str) -> MessageRequest:
        return MessageRequest(
            message_id=identifier,
            strategy_id="alpha",
            venue="binance",
            stage=OrderStage.NEW,
            priority=Priority.RESEARCH,
            messages=3,
            instrument="btc",
            ttl_ms=10_000,
        )

    # Fill the window so the next message has to queue, then roll it so
    # there is room again and the ceiling is the only thing deciding.
    plane.evaluate(
        MessageRequest(
            message_id="filler",
            strategy_id="alpha",
            venue="binance",
            stage=OrderStage.NEW,
            priority=Priority.MARKET_MAKING,
            messages=10,
            instrument="btc",
        )
    )
    assert plane.evaluate(research("queued")).action is ControlAction.QUEUE
    clock.advance_ms(1_001)
    plane.feed_monitor.record_tick("binance")

    released: list[int] = []
    admitted: list[str] = []

    venue.arm()
    threads = [
        threading.Thread(
            target=lambda: released.append(len(plane.drain())),
            name="racer-drain",
        ),
        threading.Thread(
            target=lambda: admitted.append(
                plane.evaluate(research("fresh")).action.name
            ),
            name="racer-admit",
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    venue.disarm()

    held = venue.snapshot()
    assert held.used + held.reserved == 3
    assert released[0] + (admitted[0] == "ADMIT") == 1
    park.assert_exclusive(arrivals=2)



class _Gate:
    """Holds one thread at a seam while another runs past it.

    A barrier is the wrong tool when only one side should wait: the
    admission has to *complete* inside the reservation's window, not
    meet it there.
    """

    def __init__(self) -> None:
        self.checked = threading.Event()
        self.finished = threading.Event()
        self.reached = False


class _GatedCeilingBudget(MessageBudget):
    """Holds the reserving thread at its *take-time* ceiling test.

    The count matters. A reservation tests every ceiling twice: once in
    ``_preflight``, which is advisory and deliberately outside the
    take, and once when it actually reserves. Stopping on the first
    call parks in the preflight and proves nothing -- the reservation
    then goes on to test and take without interruption, and the test
    passes whether or not those two are atomic. Stopping on the second
    lands in the seam that matters, wherever the code puts it.
    """

    def __init__(self, *, gate: _Gate, **kwargs: object) -> None:
        self._armed = False
        self._gate = gate
        self._calls = 0
        super().__init__(**kwargs)  # type: ignore[arg-type]

    def arm(self) -> None:
        self._calls = 0
        self._armed = True

    def disarm(self) -> None:
        self._armed = False

    def above_ceiling(
        self, ceiling_pct: Decimal, messages: int = 0
    ) -> bool:
        verdict = super().above_ceiling(ceiling_pct, messages)
        if (
            self._armed
            and threading.current_thread().name == "racer-reserve"
        ):
            self._calls += 1
            if self._calls == 2:
                self._gate.reached = True
                self._gate.checked.set()
                self._gate.finished.wait(timeout=0.25)
        return verdict


@pytest.mark.concurrency
def test_a_reservation_and_an_admission_cannot_both_clear_one_ceiling(
) -> None:
    """A reservation can take a whole budget, so it races hardest.

    ``ReservationLedger`` and ``ExecutionControlPlane`` spend the *same*
    budget objects -- the plane hands its own to the ledger it builds --
    and the plane never takes the ledger's lock. So holding the ledger
    lock across a reservation does nothing to stop an admission landing
    between the ledger's ceiling test and its reservation, and both then
    hold capacity the ceiling said only one of them could have.

    Reserving is the worst place for this pair, for the reason
    ``_check_ceiling`` gives itself: a reservation draws down later
    instead of walking the capacity controls, so a ceiling it can
    straddle is a ceiling any caller can evade by reserving first.

    Run one after the other, these two admit one and refuse the other.
    The concurrent outcome must match. While the test and the take
    share an acquisition the admission cannot even read the budget
    until the reservation has finished with it, so the gate times out
    -- and that timeout is the pass.
    """

    gate = _Gate()
    venue = _GatedCeilingBudget(
        gate=gate, name="binance", limit=10, window_ms=100_000
    )
    plane = ExecutionControlPlane(
        venue_budgets={"binance": venue},
        global_budget=MessageBudget(
            name="firm", limit=100, window_ms=100_000
        ),
        priority_ceilings={Priority.RESEARCH: Decimal(50)},
    )
    plane.feed_monitor.record_tick("binance")
    admitted: list[str] = []
    refused: list[str] = []
    reserved: list[bool] = []

    def admit() -> None:
        gate.checked.wait(timeout=2)
        try:
            admitted.append(
                plane.evaluate(
                    MessageRequest(
                        message_id="fresh",
                        strategy_id="alpha",
                        venue="binance",
                        stage=OrderStage.NEW,
                        priority=Priority.RESEARCH,
                        messages=3,
                        instrument="btc",
                    )
                ).action.name
            )
        finally:
            gate.finished.set()

    def reserve() -> None:
        try:
            plane.ledger.reserve(
                ReservationRequest(
                    opportunity_id="OPP-1",
                    strategy_id="alpha",
                    instrument="btc",
                    venue_messages={"binance": 3},
                    hedge_messages=0,
                    notional=Decimal(1000),
                    margin=Decimal(100),
                    position_quantity=Decimal(1),
                    priority=Priority.RESEARCH,
                )
            )
            reserved.append(True)
        except CapacityReservationError as error:
            refused.append(str(error))

    venue.arm()
    threads = [
        threading.Thread(target=reserve, name="racer-reserve"),
        threading.Thread(target=admit, name="racer-admit"),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    venue.disarm()

    # Without this the test passes when nothing ever raced: an
    # admission that never ran, or a seam that was never reached,
    # leaves the budget holding exactly the 3 asserted below.
    assert gate.reached is True
    assert len(admitted) == 1
    assert len(reserved) + len(refused) == 1

    held = venue.snapshot()
    assert held.used + held.reserved == 3
    # Either may win; exactly one may.
    assert (admitted[0] == "ADMIT") + len(reserved) == 1, (
        admitted,
        refused,
    )


@pytest.mark.concurrency
def test_a_reservation_released_mid_spend_still_admits(
    clock: Clock,
) -> None:
    """The other half of the advisory invariant, held behaviourally.

    An advisory read must be paired either with an authoritative call
    in the same acquisition, or with a handler for that call's
    refusal. ``_draw_down`` is the project's only instance of the
    second form: ``_binds`` reads that a reservation covers this
    message, and ``consume`` -- two frames later, under a lock
    ``_binds`` never held -- is what actually spends it.

    So the read can go stale, and the only thing standing between that
    and a control plane that *raises instead of deciding* is one
    ``except`` clause. Deleting it fails nothing: the whole suite
    passes with the handler gone, because every other test spends a
    reservation that is still there.

    Here the reservation is released between the check and the spend.
    The message is still legitimate -- it simply loses its exemption
    and must be charged at the ordinary rate, which is an ADMIT with
    the ordinary reason, not an exception escaping ``evaluate``.
    """

    plane = _reserving_plane(clock)
    plane.ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-1",
            strategy_id="alpha",
            venue_messages={"binance": 3},
        )
    )
    gate = _Gate()
    spend = plane.ledger.consume

    def parked(opportunity_id: str, venue: str, messages: int) -> None:
        gate.reached = True
        gate.checked.set()
        gate.finished.wait(timeout=0.25)
        spend(opportunity_id, venue, messages)

    plane.ledger.consume = parked  # type: ignore[method-assign]
    decisions: list[ControlDecision] = []
    escaped: list[BaseException] = []

    def send() -> None:
        try:
            decisions.append(_send(plane))
        except BaseException as error:  # noqa: BLE001
            escaped.append(error)

    def release() -> None:
        gate.checked.wait(timeout=2)
        plane.ledger.release("OPP-1")
        gate.finished.set()

    threads = [
        threading.Thread(target=send),
        threading.Thread(target=release),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Without this the test passes when the spend never happened, so
    # the release never landed in the window it was written to hit.
    assert gate.reached is True
    assert escaped == [], (
        "the control plane raised instead of deciding: a released "
        "reservation is a lost exemption, not an error"
    )
    assert decisions[0].action is ControlAction.ADMIT
    # Not `reserved_venue_capacity`: the reservation it would have
    # drawn on is gone, so this was charged like any other message.
    # Seeing that reason instead means the release never landed in the
    # window, and the test proved nothing.
    assert decisions[0].reason == "admitted"
    assert plane.venue_budgets["binance"].snapshot().used == 1
