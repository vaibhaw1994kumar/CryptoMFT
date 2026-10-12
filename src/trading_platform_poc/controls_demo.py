"""Offline demonstration of the execution control plane.

Every scenario runs against an injected clock and simulated budgets, so
the output is byte-identical on every platform and every run. No
network, credentials, or native extensions are involved.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from trading_platform_poc.execution_controls import (
    CONTROL_MATRIX,
    CapacityReservationError,
    ControlAction,
    ControlKind,
    ExecutionControlPlane,
    FeedMonitor,
    MessageBudget,
    MessageRequest,
    OrderStage,
    Priority,
    ReservationRequest,
    RiskLimits,
)

CONTROL_SCENARIOS: tuple[str, ...] = (
    "venue-rate-limit",
    "global-throttle",
    "stale-feed",
    "kill-switch",
    "atomic-reservation",
    "control-matrix",
)


class _DemoClock:
    """A deterministic clock so demo output never varies by timing."""

    def __init__(self) -> None:
        self.ns = 1_700_000_000_000_000_000

    def __call__(self) -> int:
        return self.ns

    def advance_ms(self, milliseconds: int) -> None:
        self.ns += milliseconds * 1_000_000


def _build_plane(
    clock: _DemoClock,
    *,
    venue_limit: int = 5,
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
                name="okx",
                limit=venue_limit,
                window_ms=1000,
                now_ns=clock,
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


def _request(
    identifier: str,
    stage: OrderStage = OrderStage.NEW,
    *,
    venue: str = "binance",
    priority: Priority = Priority.MARKET_MAKING,
    strategy_id: str = "spread-capture",
    ttl_ms: int = 5_000,
    delay_reason: str | None = None,
) -> MessageRequest:
    return MessageRequest(
        message_id=identifier,
        strategy_id=strategy_id,
        venue=venue,
        stage=stage,
        priority=priority,
        ttl_ms=ttl_ms,
        delay_reason=delay_reason,
    )


def _record(
    label: str, plane: ExecutionControlPlane, request: MessageRequest
) -> dict[str, Any]:
    decision = plane.evaluate(request)
    return {
        "label": label,
        "stage": request.stage.value,
        "priority": request.priority.name,
        **decision.to_dict(),
    }


def _venue_rate_limit(clock: _DemoClock) -> dict[str, Any]:
    """The exchange allows 5/sec; the strategy wants 8."""

    plane = _build_plane(clock, venue_limit=5)
    steps = [
        _record(f"quote-{index}", plane, _request(f"q{index}"))
        for index in range(8)
    ]
    before_drain = plane.dashboard()

    clock.advance_ms(1_001)
    released = plane.drain()

    return {
        "scenario": "venue-rate-limit",
        "narrative": (
            "Five messages fit inside the venue allowance; the remaining "
            "three are queued rather than dropped, then transmitted once "
            "the window rolls over. The exchange limit is never exceeded."
        ),
        "steps": steps,
        "admitted": sum(1 for s in steps if s["action"] == "admit"),
        "queued": sum(1 for s in steps if s["action"] == "queue"),
        "dashboard_while_binding": before_drain,
        "released_after_window": [item.message_id for item in released],
    }


def _global_throttle(clock: _DemoClock) -> dict[str, Any]:
    """Firm capacity is scarce, so priority decides who gets it."""

    plane = _build_plane(clock, venue_limit=50, global_limit=2)
    steps = [
        _record("firm-1", plane, _request("f1")),
        _record("firm-2", plane, _request("f2")),
    ]
    for priority in (
        Priority.RESEARCH,
        Priority.QUOTE_REFRESH,
        Priority.MARKET_MAKING,
    ):
        steps.append(
            _record(
                priority.name.lower(),
                plane,
                _request(
                    priority.name.lower(),
                    priority=priority,
                    strategy_id=priority.name.lower(),
                ),
            )
        )

    clock.advance_ms(1_001)
    first_wave = plane.drain(limit=1)
    clock.advance_ms(1_001)
    second_wave = plane.drain(limit=1)
    clock.advance_ms(1_001)
    third_wave = plane.drain(limit=1)

    return {
        "scenario": "global-throttle",
        "narrative": (
            "The firm limit binds before any venue limit because it is the "
            "more senior control. Queued traffic is then released strictly "
            "by priority: market making ahead of quote refresh, research "
            "last."
        ),
        "steps": steps,
        "binding_control": ControlKind.GLOBAL_THROTTLE.value,
        "release_order": [
            item.message_id
            for wave in (first_wave, second_wave, third_wave)
            for item in wave
        ],
    }


def _stale_feed(clock: _DemoClock) -> dict[str, Any]:
    """Market data goes dark on one venue but not the other."""

    plane = _build_plane(clock, stale_after_ms=100)
    fresh = [
        _record("new-order-fresh", plane, _request("n1")),
        _record("resting-fresh", plane, _request("r1", OrderStage.RESTING)),
    ]

    clock.advance_ms(250)
    plane.feed_monitor.record_tick("okx")

    stale = [
        _record("new-order", plane, _request("n2")),
        _record("replace", plane, _request("x1", OrderStage.REPLACE_REQUEST)),
        _record("resting", plane, _request("r2", OrderStage.RESTING)),
        _record("cancel", plane, _request("c1", OrderStage.CANCEL_REQUEST)),
        _record(
            "hedge",
            plane,
            _request(
                "h1",
                OrderStage.EMERGENCY_HEDGE,
                priority=Priority.EMERGENCY_HEDGE,
            ),
        ),
        _record("other-venue", plane, _request("o1", venue="okx")),
    ]

    return {
        "scenario": "stale-feed",
        "narrative": (
            "Once the Binance feed ages past 100ms, new orders and replaces "
            "are rejected and resting orders are cancelled -- but cancels "
            "and hedges still transmit, because they reduce exposure. The "
            "OKX feed is fresh, so OKX is unaffected."
        ),
        "before_staleness": fresh,
        "after_staleness": stale,
        "feeds": plane.feed_monitor.to_dict(),
    }


def _kill_switch(clock: _DemoClock) -> dict[str, Any]:
    """The absolute control: everything stops except risk reduction."""

    plane = _build_plane(clock)
    plane.kill_switch.engage(
        reason="firm-wide halt requested by risk", actor="risk-admin"
    )

    steps = [
        _record(stage.name.lower(), plane, _request(f"k{index}", stage))
        for index, stage in enumerate(OrderStage)
    ]

    unauthorized: str | None = None
    try:
        plane.kill_switch.clear(actor="spread-capture-strategy")
    except Exception as error:  # noqa: BLE001 - demonstrating the guard
        unauthorized = str(error)

    plane.kill_switch.clear(actor="risk-admin")

    return {
        "scenario": "kill-switch",
        "narrative": (
            "Anyone may engage the kill switch; only a designated risk "
            "administrator may clear it. While engaged, no strategy can "
            "override it, yet cancels, hedges and forced liquidation are "
            "still permitted so exposure can always be reduced."
        ),
        "steps": steps,
        "mass_cancel": plane.mass_cancel_scope(venue="binance"),
        "strategy_clear_refused": unauthorized,
        "cleared_by_administrator": not plane.kill_switch.active,
    }


def _atomic_reservation(clock: _DemoClock) -> dict[str, Any]:
    """All-or-nothing capacity for a two-leg opportunity."""

    plane = _build_plane(
        clock,
        venue_limit=10,
        global_limit=6,
        limits=RiskLimits(max_notional=Decimal(2_000_000)),
    )
    ledger = plane.ledger

    granted = ledger.reserve(
        ReservationRequest(
            opportunity_id="OPP-DEMO-1",
            strategy_id="spread-capture",
            venue_messages={"binance": 2, "okx": 2},
            hedge_messages=2,
            notional=Decimal(1_200_000),
            margin=Decimal(120_000),
            position_quantity=Decimal("10"),
        )
    )

    before = {
        "binance": ledger.venue_budgets["binance"].available(),
        "okx": ledger.venue_budgets["okx"].available(),
        "firm": ledger.global_budget.available(),
    }

    failure: str | None = None
    try:
        ledger.reserve(
            ReservationRequest(
                opportunity_id="OPP-DEMO-2",
                strategy_id="spread-capture",
                venue_messages={"binance": 2, "okx": 2},
                hedge_messages=2,
                notional=Decimal(500_000),
            )
        )
    except CapacityReservationError as error:
        failure = str(error)

    after = {
        "binance": ledger.venue_budgets["binance"].available(),
        "okx": ledger.venue_budgets["okx"].available(),
        "firm": ledger.global_budget.available(),
    }

    ledger.release("OPP-DEMO-1")

    return {
        "scenario": "atomic-reservation",
        "narrative": (
            "The first opportunity reserves message capacity for both legs "
            "AND its hedge before a single order is transmitted. The second "
            "cannot obtain the firm budget, so it takes nothing at all -- "
            "note the venue allowances are unchanged after the failure. "
            "This is what prevents one leg filling while the other cannot "
            "be sent."
        ),
        "granted": granted.to_dict(),
        "capacity_before_failed_attempt": before,
        "failed_attempt": failure,
        "capacity_after_failed_attempt": after,
        "rollback_was_complete": before == after,
        "after_release": {
            "binance": ledger.venue_budgets["binance"].available(),
            "okx": ledger.venue_budgets["okx"].available(),
            "firm": ledger.global_budget.available(),
        },
    }


def _control_matrix(_: _DemoClock) -> dict[str, Any]:
    """The published matrix, rendered as data."""

    return {
        "scenario": "control-matrix",
        "narrative": (
            "The required behaviour for every order state under every "
            "control, published as data so it can be diffed, exported and "
            "asserted against directly."
        ),
        "controls": [kind.value for kind in ControlKind],
        "matrix": {
            stage.value: {
                kind.value: (
                    row[kind].value if row[kind] is not None else "n/a"
                )
                for kind in ControlKind
            }
            for stage, row in CONTROL_MATRIX.items()
        },
    }


_SCENARIO_RUNNERS = {
    "venue-rate-limit": _venue_rate_limit,
    "global-throttle": _global_throttle,
    "stale-feed": _stale_feed,
    "kill-switch": _kill_switch,
    "atomic-reservation": _atomic_reservation,
    "control-matrix": _control_matrix,
}


def run_controls_demo(*, scenario: str = "all") -> dict[str, Any]:
    """Demonstrate every execution control without touching a network."""

    names = (
        CONTROL_SCENARIOS if scenario == "all" else (scenario,)
    )
    unknown = [name for name in names if name not in _SCENARIO_RUNNERS]
    if unknown:
        raise ValueError(f"unknown scenario: {', '.join(unknown)}")

    scenarios = [_SCENARIO_RUNNERS[name](_DemoClock()) for name in names]
    return {
        "demo": "execution-controls",
        "scenarios": scenarios,
        "summary": {
            "scenarios_run": len(scenarios),
            "order_stages": len(OrderStage),
            "controls": len(ControlKind),
            "matrix_cells": len(OrderStage) * len(ControlKind),
            "priority_classes": [item.name for item in Priority],
        },
    }


__all__ = ["CONTROL_SCENARIOS", "run_controls_demo"]
