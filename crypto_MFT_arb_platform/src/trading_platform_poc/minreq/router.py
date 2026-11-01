"""Order management, routing and residual policy (MR-07, MR-08, section 6).

One canonical order API serves every venue. A parent plan owns its child
orders, moves through an explicit state machine, and is never allowed to leave
one leg of a two-venue opportunity outstanding without an owner: any residual
is hedged or unwound under a stated policy and everything is written to the
append-only ledger so a restart rebuilds the same picture.

Execution is event driven. Child orders are sent, spend a sampled latency in
flight, and are executed by the venue when they arrive; the router hears about
the result only when the acknowledgement or fill report comes back. Between
those moments time advances through :meth:`OrderRouter.advance`, which is what
gives hedges, unwinds, timeouts and residual limits a real duration.

Invariants enforced here:

* A plan cannot reach ``reconciled`` or ``closed`` while it holds residual
  exposure or while any child order's fate is unknown.
* An acknowledgement that does not arrive in time moves the child to
  ``unknown``, never to rejected or cancelled. The venue is queried; the order
  is never resent under a new identifier while it is unknown.
* Residual size and age are checked continuously against the risk limits. A
  breach marks the plan ``residual_breach``, forces an aggressive unwind and is
  surfaced to the kill-switch supervisor.
* An IOC order's unfilled remainder is explicitly cancelled.
"""

from __future__ import annotations

import heapq
import itertools
import uuid
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Sequence

from trading_platform_poc.minreq.canonical import BookValidity, Liquidity
from trading_platform_poc.minreq.fees import CostModel, LegEconomics, walk_depth
from trading_platform_poc.minreq.latency import LatencyModel
from trading_platform_poc.minreq.ledger import (
    DerivedState,
    EventLedger,
    LedgerEvent,
)
from trading_platform_poc.minreq.matching import (
    MatchOutcome,
    OrderRequest,
    PaperMatchingEngine,
    SimulatedFill,
    VenueOrderView,
)
from trading_platform_poc.minreq.quality import CanonicalBook
from trading_platform_poc.minreq.riskcontrols import (
    LimitId,
    RiskCheckRequest,
    RiskEngine,
    Reservation,
)
from trading_platform_poc.minreq.securitymaster import SecurityMaster

CLIENT_ID_NAMESPACE = uuid.UUID("6f5d5b34-0e0b-5a2c-9d3e-6a1c2f7b8d40")
BPS = Decimal(10_000)
DRAIN_HORIZON_NS = 60_000_000_000


class PlanState(str, Enum):
    CREATED = "created"
    RISK_REJECTED = "risk_rejected"
    RISK_RESERVED = "risk_reserved"
    DISPATCHED = "dispatched"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    HEDGING = "hedging"
    UNWINDING = "unwinding"
    RESIDUAL_BREACH = "residual_breach"
    RECONCILED = "reconciled"
    CLOSED = "closed"


class ChildState(str, Enum):
    CREATED = "created"
    RISK_RESERVED = "risk_reserved"
    DISPATCHED = "dispatched"
    UNKNOWN = "unknown"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    CLOSED = "closed"


PLAN_TRANSITIONS: dict[PlanState, frozenset[PlanState]] = {
    PlanState.CREATED: frozenset(
        {PlanState.RISK_RESERVED, PlanState.RISK_REJECTED}
    ),
    PlanState.RISK_REJECTED: frozenset({PlanState.CLOSED}),
    PlanState.RISK_RESERVED: frozenset({PlanState.DISPATCHED, PlanState.REJECTED}),
    PlanState.DISPATCHED: frozenset(
        {
            PlanState.PARTIALLY_FILLED,
            PlanState.FILLED,
            PlanState.REJECTED,
            PlanState.TIMED_OUT,
        }
    ),
    PlanState.PARTIALLY_FILLED: frozenset(
        {
            PlanState.FILLED,
            PlanState.HEDGING,
            PlanState.UNWINDING,
            PlanState.TIMED_OUT,
            PlanState.RESIDUAL_BREACH,
        }
    ),
    PlanState.FILLED: frozenset({PlanState.RECONCILED, PlanState.UNWINDING}),
    PlanState.REJECTED: frozenset({PlanState.RECONCILED, PlanState.UNWINDING}),
    PlanState.TIMED_OUT: frozenset(
        {
            PlanState.PARTIALLY_FILLED,
            PlanState.FILLED,
            PlanState.REJECTED,
            PlanState.HEDGING,
            PlanState.UNWINDING,
            PlanState.RESIDUAL_BREACH,
            PlanState.RECONCILED,
        }
    ),
    PlanState.HEDGING: frozenset(
        {
            PlanState.FILLED,
            PlanState.UNWINDING,
            PlanState.RESIDUAL_BREACH,
            PlanState.RECONCILED,
        }
    ),
    PlanState.UNWINDING: frozenset(
        {PlanState.RECONCILED, PlanState.RESIDUAL_BREACH}
    ),
    PlanState.RESIDUAL_BREACH: frozenset(
        {PlanState.UNWINDING, PlanState.RECONCILED}
    ),
    PlanState.RECONCILED: frozenset({PlanState.CLOSED}),
    PlanState.CLOSED: frozenset(),
}

CHILD_TRANSITIONS: dict[ChildState, frozenset[ChildState]] = {
    ChildState.CREATED: frozenset(
        {ChildState.RISK_RESERVED, ChildState.REJECTED}
    ),
    ChildState.RISK_RESERVED: frozenset(
        {ChildState.DISPATCHED, ChildState.REJECTED}
    ),
    ChildState.DISPATCHED: frozenset(
        {
            ChildState.ACKNOWLEDGED,
            ChildState.PARTIALLY_FILLED,
            ChildState.FILLED,
            ChildState.REJECTED,
            ChildState.UNKNOWN,
            ChildState.CANCELLED,
        }
    ),
    ChildState.UNKNOWN: frozenset(
        {
            ChildState.ACKNOWLEDGED,
            ChildState.PARTIALLY_FILLED,
            ChildState.FILLED,
            ChildState.REJECTED,
            ChildState.CANCELLED,
            ChildState.TIMED_OUT,
        }
    ),
    ChildState.ACKNOWLEDGED: frozenset(
        {
            ChildState.PARTIALLY_FILLED,
            ChildState.FILLED,
            ChildState.CANCELLED,
            ChildState.TIMED_OUT,
        }
    ),
    ChildState.PARTIALLY_FILLED: frozenset(
        {ChildState.FILLED, ChildState.CANCELLED, ChildState.TIMED_OUT}
    ),
    ChildState.FILLED: frozenset({ChildState.CLOSED}),
    ChildState.REJECTED: frozenset({ChildState.CLOSED}),
    ChildState.TIMED_OUT: frozenset(
        {
            ChildState.ACKNOWLEDGED,
            ChildState.PARTIALLY_FILLED,
            ChildState.FILLED,
            ChildState.REJECTED,
            ChildState.CANCELLED,
            ChildState.CLOSED,
        }
    ),
    ChildState.CANCELLED: frozenset({ChildState.CLOSED}),
    ChildState.CLOSED: frozenset(),
}

# A child in one of these states may still change the plan's exposure.
UNRESOLVED_CHILD_STATES = frozenset(
    {ChildState.DISPATCHED, ChildState.UNKNOWN, ChildState.TIMED_OUT}
)


class StateTransitionError(RuntimeError):
    """Raised when an order or plan is moved along an undefined edge."""


def assert_plan_transition(current: PlanState, target: PlanState) -> None:
    if target not in PLAN_TRANSITIONS[current]:
        raise StateTransitionError(
            f"plan transition {current.value} -> {target.value} is not allowed"
        )


def assert_child_transition(current: ChildState, target: ChildState) -> None:
    if target not in CHILD_TRANSITIONS[current]:
        raise StateTransitionError(
            f"order transition {current.value} -> {target.value} is not allowed"
        )


class ResidualPolicy(str, Enum):
    HEDGE_THEN_UNWIND = "hedge_then_unwind"
    UNWIND_IMMEDIATELY = "unwind_immediately"
    HOLD = "hold"


class LegDispatch(str, Enum):
    """How the two legs of a package are released."""

    PARALLEL = "parallel"
    HARDEST_FIRST = "hardest_first"


class ExecutionStyle(str, Enum):
    TAKER = "taker"
    MAKER_TAKER = "maker_taker"


@dataclass(frozen=True, slots=True)
class RouterConfig:
    residual_policy: ResidualPolicy = ResidualPolicy.HEDGE_THEN_UNWIND
    ack_timeout_ns: int = 500_000_000
    latency_penalty_bps_per_ms: Decimal = Decimal("0.02")
    max_hedge_attempts: int = 1
    time_in_force: str = "ioc"
    order_type: str = "limit"
    aggression_bps: Decimal = Decimal("2")
    leg_dispatch: LegDispatch = LegDispatch.PARALLEL
    max_leg_exposure_notional: Decimal = Decimal("2500")
    execution_style: ExecutionStyle = ExecutionStyle.TAKER
    maker_ttl_ns: int = 2_000_000_000
    min_fill_probability: Decimal = Decimal("0.5")
    query_retry_ns: int = 50_000_000
    max_order_queries: int = 5
    residual_retry_ns: int = 250_000_000
    max_residual_retry_ns: int = 5_000_000_000
    max_unwind_aggression_bps: Decimal = Decimal("100")

    def to_dict(self) -> dict[str, Any]:
        return {
            "residual_policy": self.residual_policy.value,
            "ack_timeout_ns": self.ack_timeout_ns,
            "latency_penalty_bps_per_ms": str(self.latency_penalty_bps_per_ms),
            "max_hedge_attempts": self.max_hedge_attempts,
            "time_in_force": self.time_in_force,
            "order_type": self.order_type,
            "aggression_bps": str(self.aggression_bps),
            "leg_dispatch": self.leg_dispatch.value,
            "max_leg_exposure_notional": str(self.max_leg_exposure_notional),
            "execution_style": self.execution_style.value,
            "maker_ttl_ns": self.maker_ttl_ns,
            "min_fill_probability": str(self.min_fill_probability),
            "query_retry_ns": self.query_retry_ns,
            "max_order_queries": self.max_order_queries,
            "residual_retry_ns": self.residual_retry_ns,
            "max_residual_retry_ns": self.max_residual_retry_ns,
            "max_unwind_aggression_bps": str(self.max_unwind_aggression_bps),
        }


@dataclass(frozen=True, slots=True)
class LegIntent:
    venue: str
    instrument_id: str
    side: str
    quantity: Decimal
    limit_price: Decimal
    reference_price: Decimal
    order_type: str = "limit"
    time_in_force: str = "ioc"
    post_only: bool = False
    risk_reducing: bool = False
    data_age_ns: int = 0
    book_valid: bool = True

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.limit_price


@dataclass(frozen=True, slots=True)
class OpportunityIntent:
    """What the strategy wants, before the router decides how to get it."""

    opportunity_id: str
    economic_group: str
    feature_id: str
    as_of_ns: int
    target_quantity: Decimal
    buy_venue: str
    sell_venue: str
    buy_instrument_id: str
    sell_instrument_id: str
    signal: Decimal
    reason: str = "entry"
    risk_reducing: bool = False


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    buy_venue: str
    sell_venue: str
    quantity: Decimal
    buy_price: Decimal
    sell_price: Decimal
    gross_edge: Decimal
    fees: Decimal
    slippage_cost: Decimal
    latency_penalty: Decimal
    net_edge: Decimal
    feasible: bool
    reason: str
    fill_probability: Decimal = Decimal(1)
    expected_value: Decimal = Decimal(0)

    @property
    def score(self) -> Decimal:
        return self.expected_value if self.feasible else self.net_edge

    def to_dict(self) -> dict[str, Any]:
        return {
            "buy_venue": self.buy_venue,
            "sell_venue": self.sell_venue,
            "quantity": str(self.quantity),
            "buy_price": str(self.buy_price),
            "sell_price": str(self.sell_price),
            "gross_edge": str(self.gross_edge),
            "fees": str(self.fees),
            "slippage_cost": str(self.slippage_cost),
            "latency_penalty": str(self.latency_penalty),
            "net_edge": str(self.net_edge),
            "fill_probability": str(self.fill_probability),
            "expected_value": str(self.expected_value),
            "feasible": self.feasible,
            "reason": self.reason,
        }


@dataclass(slots=True)
class ChildOrder:
    child_order_id: str
    client_order_id: str
    plan_id: str
    venue: str
    instrument_id: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    time_in_force: str
    state: ChildState
    created_ns: int
    role: str = "leg"
    filled_quantity: Decimal = Decimal(0)
    average_price: Decimal | None = None
    fees: Decimal = Decimal(0)
    reject_reason: str = ""
    venue_order_id: str = ""
    updated_ns: int = 0
    sent_ns: int = 0
    query_attempts: int = 0
    liquidity: str = "taker"
    expected_price: Decimal | None = None
    post_only: bool = False
    first_fill_execution_ns: int = 0

    @property
    def leaves_quantity(self) -> Decimal:
        return max(Decimal(0), self.quantity - self.filled_quantity)

    @property
    def working(self) -> bool:
        """Resting at the venue and still able to fill."""

        return self.state is ChildState.ACKNOWLEDGED or (
            self.state is ChildState.PARTIALLY_FILLED
            and self.time_in_force.lower() == "gtc"
        )

    @property
    def signed_filled(self) -> Decimal:
        return self.filled_quantity if self.side == "buy" else -self.filled_quantity

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_order_id": self.child_order_id,
            "client_order_id": self.client_order_id,
            "plan_id": self.plan_id,
            "venue": self.venue,
            "instrument_id": self.instrument_id,
            "side": self.side,
            "order_type": self.order_type,
            "quantity": str(self.quantity),
            "price": None if self.price is None else str(self.price),
            "time_in_force": self.time_in_force,
            "state": self.state.value,
            "role": self.role,
            "liquidity": self.liquidity,
            "filled_quantity": str(self.filled_quantity),
            "average_price": (
                None if self.average_price is None else str(self.average_price)
            ),
            "fees": str(self.fees),
            "reject_reason": self.reject_reason,
            "venue_order_id": self.venue_order_id,
            "created_ns": self.created_ns,
            "sent_ns": self.sent_ns,
            "updated_ns": self.updated_ns,
            "query_attempts": self.query_attempts,
        }


@dataclass(slots=True)
class ParentPlan:
    plan_id: str
    opportunity_id: str
    economic_group: str
    feature_id: str
    created_ns: int
    state: PlanState
    intent: OpportunityIntent
    candidate: RouteCandidate
    children: list[ChildOrder] = field(default_factory=list)
    reservation: Reservation | None = None
    risk_decision_id: str = ""
    residual_quantity: Decimal = Decimal(0)
    residual_instrument_id: str = ""
    residual_venue: str = ""
    residual_side: str = ""
    residual_opened_ns: int = 0
    hedge_attempts: int = 0
    reason: str = ""
    updated_ns: int = 0
    realized_legs: list[LegEconomics] = field(default_factory=list)
    phase: str = "entry"
    dispatch_policy: str = LegDispatch.PARALLEL.value
    execution_style: str = ExecutionStyle.TAKER.value
    pending_legs: list[tuple[str, LegIntent]] = field(default_factory=list)
    hedge_template: tuple[str, LegIntent] | None = None
    maker_child_id: str = ""
    hedge_requested: Decimal = Decimal(0)
    opening_exposure: Decimal = Decimal(0)
    residual_breached: bool = False
    residual_breached_ns: int = 0
    unwind_attempts: int = 0
    closed_ns: int = 0

    def child(self, child_order_id: str) -> ChildOrder | None:
        for order in self.children:
            if order.child_order_id == child_order_id:
                return order
        return None

    @property
    def terminal(self) -> bool:
        return self.state is PlanState.CLOSED

    @property
    def net_exposure(self) -> Decimal:
        """Signed base exposure the plan owns: inherited plus every fill."""

        return self.opening_exposure + sum(
            (order.signed_filled for order in self.children), Decimal(0)
        )

    @property
    def unresolved_children(self) -> list[ChildOrder]:
        return [
            order for order in self.children if order.state in UNRESOLVED_CHILD_STATES
        ]

    @property
    def working_children(self) -> list[ChildOrder]:
        return [order for order in self.children if order.working]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "opportunity_id": self.opportunity_id,
            "economic_group": self.economic_group,
            "feature_id": self.feature_id,
            "created_ns": self.created_ns,
            "updated_ns": self.updated_ns,
            "closed_ns": self.closed_ns,
            "state": self.state.value,
            "phase": self.phase,
            "reason": self.reason,
            "risk_decision_id": self.risk_decision_id,
            "dispatch_policy": self.dispatch_policy,
            "execution_style": self.execution_style,
            "residual_quantity": str(self.residual_quantity),
            "residual_venue": self.residual_venue,
            "residual_side": self.residual_side,
            "residual_opened_ns": self.residual_opened_ns,
            "residual_breached": self.residual_breached,
            "net_exposure": str(self.net_exposure),
            "hedge_attempts": self.hedge_attempts,
            "unwind_attempts": self.unwind_attempts,
            "candidate": self.candidate.to_dict(),
            "children": [order.to_dict() for order in self.children],
        }


@dataclass(frozen=True, slots=True)
class PlanResult:
    plan: ParentPlan
    accepted: bool
    reason: str
    fills: tuple[SimulatedFill, ...]
    outcomes: tuple[MatchOutcome, ...]
    candidates: tuple[RouteCandidate, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "plan": self.plan.to_dict(),
            "fill_count": len(self.fills),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


def client_order_id(
    run_id: str, plan_id: str, venue: str, role: str, attempt: int
) -> str:
    """Deterministic and unique: the same input never yields a new id."""

    key = f"{run_id}|{plan_id}|{venue}|{role}|{attempt}"
    return f"mr-{uuid.uuid5(CLIENT_ID_NAMESPACE, key).hex[:24]}"


def plan_id_for(run_id: str, opportunity_id: str) -> str:
    key = f"{run_id}|{opportunity_id}"
    return f"plan-{uuid.uuid5(CLIENT_ID_NAMESPACE, key).hex[:20]}"


class OrderRouter:
    """Canonical order API, route selection and residual resolution."""

    def __init__(
        self,
        *,
        run_id: str,
        security_master: SecurityMaster,
        cost_model: CostModel,
        risk_engine: RiskEngine,
        matcher: PaperMatchingEngine,
        ledger: EventLedger,
        books: Mapping[str, CanonicalBook],
        latency: LatencyModel,
        config: RouterConfig | None = None,
    ) -> None:
        self.run_id = run_id
        self.security_master = security_master
        self.cost_model = cost_model
        self.risk_engine = risk_engine
        self.matcher = matcher
        self.ledger = ledger
        self.books = books
        self.latency = latency
        self.config = config or RouterConfig()
        self.plans: dict[str, ParentPlan] = {}
        self.duplicate_suppressed = 0
        self._client_ids: set[str] = set()
        self.missed_opportunities: list[dict[str, Any]] = []
        self.route_log: list[dict[str, Any]] = []
        self.now_ns = 0
        self._sequence = itertools.count()
        self._timers: list[tuple[int, int, str, str, Any]] = []
        self._reports: list[tuple[int, int, str, Any]] = []
        self._children_by_cid: dict[str, tuple[str, str]] = {}
        self._applied_fill_ids: set[str] = set()
        self.residual_breaches: list[dict[str, Any]] = []
        self.unresolved_orders: list[dict[str, Any]] = []
        self.venue_reject_streak: dict[str, int] = {}
        self.execution_quality: list[dict[str, Any]] = []

    # ----------------------------------------------------------------- ledger
    def _emit(
        self,
        event_type: str,
        timestamp_ns: int,
        payload: Mapping[str, Any],
        *,
        plan_id: str | None = None,
        child_order_id: str | None = None,
        client_order_id_value: str | None = None,
        venue: str | None = None,
        instrument_id: str | None = None,
        source: str = "internal",
    ) -> None:
        event_id = uuid.uuid5(
            CLIENT_ID_NAMESPACE,
            f"{self.run_id}|{event_type}|{plan_id}|{child_order_id}"
            f"|{timestamp_ns}|{sorted(payload.items())}",
        ).hex
        self.ledger.append(
            LedgerEvent(
                event_id=event_id,
                timestamp_ns=timestamp_ns,
                event_type=event_type,
                run_id=self.run_id,
                payload=dict(payload),
                plan_id=plan_id,
                child_order_id=child_order_id,
                client_order_id=client_order_id_value,
                venue=venue,
                instrument_id=instrument_id,
                source=source,
            )
        )

    def _set_plan_state(
        self, plan: ParentPlan, target: PlanState, timestamp_ns: int, reason: str = ""
    ) -> None:
        assert_plan_transition(plan.state, target)
        if target in {PlanState.RECONCILED, PlanState.CLOSED}:
            exposure = plan.net_exposure
            unresolved = plan.unresolved_children or plan.working_children
            if exposure != 0 or unresolved:
                raise StateTransitionError(
                    f"plan {plan.plan_id} cannot become {target.value} with "
                    f"exposure {exposure} and {len(unresolved)} unresolved orders"
                )
        plan.state = target
        plan.updated_ns = timestamp_ns
        if reason:
            plan.reason = reason
        self._emit(
            "plan_state",
            timestamp_ns,
            {"state": target.value, "reason": reason},
            plan_id=plan.plan_id,
        )

    def _set_child_state(
        self,
        plan: ParentPlan,
        order: ChildOrder,
        target: ChildState,
        timestamp_ns: int,
        *,
        reason: str = "",
        venue_order_id: str = "",
    ) -> None:
        assert_child_transition(order.state, target)
        order.state = target
        order.updated_ns = timestamp_ns
        if reason:
            order.reject_reason = reason
        if venue_order_id:
            order.venue_order_id = venue_order_id
        self._emit(
            "order_state",
            timestamp_ns,
            {
                "state": target.value,
                "reason": reason,
                "venue_order_id": venue_order_id or order.venue_order_id,
            },
            plan_id=plan.plan_id,
            child_order_id=order.child_order_id,
            client_order_id_value=order.client_order_id,
            venue=order.venue,
            instrument_id=order.instrument_id,
        )

    # ------------------------------------------------------------- routing
    def score_route(
        self,
        *,
        buy_venue: str,
        sell_venue: str,
        buy_instrument_id: str,
        sell_instrument_id: str,
        quantity: Decimal,
        at_ns: int,
    ) -> RouteCandidate:
        """Score one venue pair on expected value after fees, slippage,
        latency and the chance both legs still fill on arrival.

        Depth is the matcher's *remaining* executable depth, so liquidity that
        earlier orders already consumed is not counted twice.
        """

        buy_book = self.books.get(buy_venue)
        sell_book = self.books.get(sell_venue)
        if buy_book is None or sell_book is None:
            return _infeasible(buy_venue, sell_venue, quantity, "book_unavailable")
        buy_levels = self.matcher.available_depth(buy_venue, "buy")
        sell_levels = self.matcher.available_depth(sell_venue, "sell")
        if not buy_levels or not sell_levels:
            return _infeasible(buy_venue, sell_venue, quantity, "empty_book")

        buy_reference = buy_book.mid_price or buy_levels[0].price
        sell_reference = sell_book.mid_price or sell_levels[0].price
        buy_walk = walk_depth(buy_levels, quantity, buy_reference, "buy")
        sell_walk = walk_depth(sell_levels, quantity, sell_reference, "sell")
        if buy_walk.filled_quantity < quantity or sell_walk.filled_quantity < quantity:
            return _infeasible(
                buy_venue, sell_venue, quantity, "insufficient_depth"
            )

        try:
            buy_instrument = self.security_master.resolve(buy_instrument_id, at_ns)
            sell_instrument = self.security_master.resolve(sell_instrument_id, at_ns)
        except Exception:
            return _infeasible(
                buy_venue, sell_venue, quantity, "instrument_unavailable"
            )

        expected_ack_ms = Decimal(
            self.latency.expected_ns(buy_venue, "entry")
            + self.latency.expected_ns(sell_venue, "entry")
        ) / Decimal(2_000_000)
        latency_penalty_bps = expected_ack_ms * self.config.latency_penalty_bps_per_ms

        economics = self.cost_model.expected_edge(
            buy_instrument,
            sell_instrument,
            buy_price=buy_walk.average_price,
            sell_price=sell_walk.average_price,
            quantity=quantity,
            at_ns=at_ns,
            liquidity=Liquidity.TAKER,
            latency_penalty_bps=latency_penalty_bps,
        )
        latency_penalty = (
            (buy_walk.notional + sell_walk.notional)
            / Decimal(2)
            * latency_penalty_bps
            / BPS
        )
        probability = self.matcher.expected_fill_probability(
            buy_venue, "buy", quantity, None
        ) * self.matcher.expected_fill_probability(sell_venue, "sell", quantity, None)
        # If only one leg fills, the residual costs roughly the package fees
        # plus the latency penalty to flatten.
        residual_cost = economics.fees + latency_penalty
        expected_value = (
            economics.net_pnl * probability - (Decimal(1) - probability) * residual_cost
        )
        return RouteCandidate(
            buy_venue=buy_venue,
            sell_venue=sell_venue,
            quantity=quantity,
            buy_price=buy_walk.average_price,
            sell_price=sell_walk.average_price,
            gross_edge=economics.gross_edge,
            fees=economics.fees,
            slippage_cost=economics.slippage_cost,
            latency_penalty=latency_penalty,
            net_edge=economics.net_pnl,
            feasible=True,
            reason="ok",
            fill_probability=probability,
            expected_value=expected_value,
        )

    def candidates_for(
        self, intent: OpportunityIntent, quantity: Decimal
    ) -> tuple[RouteCandidate, ...]:
        pairs = [(intent.buy_venue, intent.sell_venue)]
        if not intent.risk_reducing:
            pairs.append((intent.sell_venue, intent.buy_venue))
        scored: list[RouteCandidate] = []
        for buy_venue, sell_venue in pairs:
            buy_instrument = (
                intent.buy_instrument_id
                if buy_venue == intent.buy_venue
                else intent.sell_instrument_id
            )
            sell_instrument = (
                intent.sell_instrument_id
                if sell_venue == intent.sell_venue
                else intent.buy_instrument_id
            )
            scored.append(
                self.score_route(
                    buy_venue=buy_venue,
                    sell_venue=sell_venue,
                    buy_instrument_id=buy_instrument,
                    sell_instrument_id=sell_instrument,
                    quantity=quantity,
                    at_ns=intent.as_of_ns,
                )
            )
        return tuple(
            sorted(
                scored,
                key=lambda item: (item.feasible, item.score),
                reverse=True,
            )
        )

    # ------------------------------------------------------------- execution
    def submit_opportunity(self, intent: OpportunityIntent) -> PlanResult:
        timestamp_ns = max(intent.as_of_ns, self.now_ns)
        self.now_ns = timestamp_ns
        plan_id = plan_id_for(self.run_id, intent.opportunity_id)
        if plan_id in self.plans:
            self.duplicate_suppressed += 1
            plan = self.plans[plan_id]
            return PlanResult(plan, False, "duplicate_opportunity", (), (), ())

        quantity, buy_leg, sell_leg, reason = self._size_legs(intent, timestamp_ns)
        if buy_leg is None or sell_leg is None:
            self._record_miss(intent, reason, timestamp_ns)
            return _empty_result(intent, plan_id, reason)

        candidates = self.candidates_for(intent, quantity)
        best = candidates[0] if candidates else None
        if best is None or not best.feasible:
            reason = "no_feasible_route" if best is None else best.reason
            self._record_miss(intent, reason, timestamp_ns)
            return _empty_result(intent, plan_id, reason)
        if best.net_edge <= self.risk_engine.limits.minimum_net_edge and not intent.risk_reducing:
            self._record_miss(intent, "negative_net_edge", timestamp_ns)
            return _empty_result(intent, plan_id, "negative_net_edge")
        if (
            best.fill_probability < self.config.min_fill_probability
            and not intent.risk_reducing
        ):
            self._record_miss(intent, "low_fill_probability", timestamp_ns)
            return _empty_result(intent, plan_id, "low_fill_probability")

        if best.buy_venue != intent.buy_venue:
            flipped = replace(
                intent,
                buy_venue=best.buy_venue,
                sell_venue=best.sell_venue,
                buy_instrument_id=intent.sell_instrument_id,
                sell_instrument_id=intent.buy_instrument_id,
            )
            quantity, buy_leg, sell_leg, reason = self._size_legs(
                flipped, timestamp_ns
            )
            if buy_leg is None or sell_leg is None:
                self._record_miss(intent, reason, timestamp_ns)
                return _empty_result(intent, plan_id, reason)
            intent = flipped

        policy, policy_reason = self._dispatch_policy(buy_leg, sell_leg)
        style = (
            ExecutionStyle.MAKER_TAKER
            if self.config.execution_style is ExecutionStyle.MAKER_TAKER
            and not intent.risk_reducing
            else ExecutionStyle.TAKER
        )
        plan = ParentPlan(
            plan_id=plan_id,
            opportunity_id=intent.opportunity_id,
            economic_group=intent.economic_group,
            feature_id=intent.feature_id,
            created_ns=timestamp_ns,
            state=PlanState.CREATED,
            intent=intent,
            candidate=best,
            dispatch_policy=policy.value,
            execution_style=style.value,
        )
        self.plans[plan_id] = plan
        self._emit(
            "plan_created",
            timestamp_ns,
            {
                "opportunity_id": intent.opportunity_id,
                "economic_group": intent.economic_group,
                "feature_id": intent.feature_id,
                "expected_edge": str(best.net_edge),
                "fill_probability": str(best.fill_probability),
                "signal": str(intent.signal),
                "reason": intent.reason,
                "dispatch_policy": policy.value,
                "dispatch_policy_reason": policy_reason,
                "execution_style": style.value,
                "legs": [
                    {
                        "venue": leg.venue,
                        "instrument_id": leg.instrument_id,
                        "side": leg.side,
                        "quantity": str(leg.quantity),
                        "price": str(leg.limit_price),
                    }
                    for leg in (buy_leg, sell_leg)
                ],
            },
            plan_id=plan_id,
        )
        self.route_log.append(
            {
                "timestamp_ns": timestamp_ns,
                "plan_id": plan_id,
                "chosen": best.to_dict(),
                "considered": [candidate.to_dict() for candidate in candidates],
                "dispatch_policy": policy.value,
                "dispatch_policy_reason": policy_reason,
                "execution_style": style.value,
            }
        )

        requests = [
            RiskCheckRequest(
                instrument_id=leg.instrument_id,
                venue=leg.venue,
                side=leg.side,
                quantity=leg.quantity,
                price=leg.limit_price,
                notional=leg.notional,
                reference_price=leg.reference_price,
                data_age_ns=leg.data_age_ns,
                book_valid=leg.book_valid,
                risk_reducing=leg.risk_reducing,
                order_type=leg.order_type,
            )
            for leg in (buy_leg, sell_leg)
        ]
        reservation, decisions = self.risk_engine.reserve(
            plan_id, requests, timestamp_ns, expected_net_edge=best.net_edge
        )
        plan.risk_decision_id = decisions[-1].decision_id if decisions else ""
        for decision in decisions:
            self._emit(
                "risk_decision",
                timestamp_ns,
                decision.to_dict(),
                plan_id=plan_id,
            )
        if reservation is None:
            reason = decisions[-1].reason if decisions else "risk_rejected"
            self._set_plan_state(plan, PlanState.RISK_REJECTED, timestamp_ns, reason)
            self._set_plan_state(plan, PlanState.CLOSED, timestamp_ns, reason)
            self._record_miss(intent, f"risk:{reason}", timestamp_ns)
            return PlanResult(plan, False, reason, (), (), candidates)

        plan.reservation = reservation
        self._set_plan_state(plan, PlanState.RISK_RESERVED, timestamp_ns)

        expected = {"buy": best.buy_price, "sell": best.sell_price}
        if style is ExecutionStyle.MAKER_TAKER:
            self._dispatch_maker_taker(plan, buy_leg, sell_leg, timestamp_ns)
        elif policy is LegDispatch.HARDEST_FIRST:
            first, second = self._rank_legs(buy_leg, sell_leg)
            plan.pending_legs = [second]
            order = self._create_child(
                plan, first[1], first[0], timestamp_ns, expected=expected[first[0]]
            )
            self._dispatch(plan, order, first[1], timestamp_ns)
        else:
            for role, leg in (("buy", buy_leg), ("sell", sell_leg)):
                order = self._create_child(
                    plan, leg, role, timestamp_ns, expected=expected[role]
                )
                self._dispatch(plan, order, leg, timestamp_ns)

        self._set_plan_state(plan, PlanState.DISPATCHED, timestamp_ns)
        self._evaluate_plan(plan, timestamp_ns)
        return PlanResult(plan, True, "dispatched", (), (), candidates)

    def _dispatch_policy(
        self, buy_leg: LegIntent, sell_leg: LegIntent
    ) -> tuple[LegDispatch, str]:
        if self.config.leg_dispatch is LegDispatch.PARALLEL:
            return LegDispatch.PARALLEL, "configured"
        # Sequential release leaves the first leg's fill unhedged until the
        # second leg lands; above the leg-exposure cap that window is too big.
        exposure = max(buy_leg.notional, sell_leg.notional)
        if exposure > self.config.max_leg_exposure_notional:
            return (
                LegDispatch.PARALLEL,
                f"leg_exposure {exposure} > {self.config.max_leg_exposure_notional}",
            )
        return LegDispatch.HARDEST_FIRST, "configured"

    def _rank_legs(
        self, buy_leg: LegIntent, sell_leg: LegIntent
    ) -> tuple[tuple[str, LegIntent], tuple[str, LegIntent]]:
        """Hardest-to-fill leg first: lowest fill probability, then slowest venue."""

        def difficulty(role: str, leg: LegIntent) -> tuple[Decimal, int]:
            probability = self.matcher.expected_fill_probability(
                leg.venue, role, leg.quantity, leg.limit_price
            )
            return probability, -self.latency.expected_ns(leg.venue, "entry")

        legs = [("buy", buy_leg), ("sell", sell_leg)]
        legs.sort(key=lambda item: difficulty(*item))
        return legs[0], legs[1]

    def _dispatch_maker_taker(
        self,
        plan: ParentPlan,
        buy_leg: LegIntent,
        sell_leg: LegIntent,
        timestamp_ns: int,
    ) -> None:
        """Rest the buy leg passively at the touch; hedge each fill as a taker."""

        book = self.books[buy_leg.venue]
        touch = book.best_bid or buy_leg.limit_price
        maker_leg = replace(
            buy_leg,
            limit_price=touch,
            time_in_force="gtc",
            post_only=True,
        )
        order = self._create_child(
            plan, maker_leg, "buy", timestamp_ns, expected=touch, liquidity="maker"
        )
        plan.maker_child_id = order.child_order_id
        plan.hedge_template = ("sell", sell_leg)
        self._dispatch(plan, order, maker_leg, timestamp_ns)
        self._schedule(
            timestamp_ns + self.config.maker_ttl_ns, "maker_ttl", plan.plan_id, None
        )

    def _size_legs(
        self, intent: OpportunityIntent, timestamp_ns: int
    ) -> tuple[Decimal, LegIntent | None, LegIntent | None, str]:
        buy_book = self.books.get(intent.buy_venue)
        sell_book = self.books.get(intent.sell_venue)
        if buy_book is None or sell_book is None:
            return Decimal(0), None, None, "sizing_failed"
        buy_reference = buy_book.mid_price
        sell_reference = sell_book.mid_price
        best_ask = buy_book.best_ask
        best_bid = sell_book.best_bid
        if (
            buy_reference is None
            or sell_reference is None
            or best_ask is None
            or best_bid is None
        ):
            return Decimal(0), None, None, "sizing_failed"

        target = intent.target_quantity
        if intent.risk_reducing:
            # A reducing package may only take positions toward flat. It can
            # never open the opposite exposure because a signal was stale.
            short_on_buy_venue = -self.risk_engine.position(
                intent.buy_instrument_id, intent.buy_venue
            )
            long_on_sell_venue = self.risk_engine.position(
                intent.sell_instrument_id, intent.sell_venue
            )
            cap = min(short_on_buy_venue, long_on_sell_venue)
            if cap <= 0:
                return Decimal(0), None, None, "nothing_to_reduce"
            target = min(target, cap)

        aggression = self.config.aggression_bps / BPS
        buy_limit = best_ask * (Decimal(1) + aggression)
        sell_limit = best_bid * (Decimal(1) - aggression)

        def size(instrument_id: str, side: str, quantity: Decimal, price: Decimal, reference: Decimal):
            return self.security_master.size_order(
                instrument_id,
                at_ns=timestamp_ns,
                side=side,
                target_quantity=quantity,
                price=price,
                reference_price=reference,
                order_type=self.config.order_type,
                time_in_force=self.config.time_in_force,
                allow_risk_reducing=intent.risk_reducing,
            )

        buy_sizing = size(intent.buy_instrument_id, "buy", target, buy_limit, buy_reference)
        sell_sizing = size(intent.sell_instrument_id, "sell", target, sell_limit, sell_reference)
        if not buy_sizing.eligible or not sell_sizing.eligible:
            return Decimal(0), None, None, "sizing_failed"

        quantity = min(buy_sizing.quantity, sell_sizing.quantity)
        harmonised_buy = size(intent.buy_instrument_id, "buy", quantity, buy_limit, buy_reference)
        harmonised_sell = size(intent.sell_instrument_id, "sell", quantity, sell_limit, sell_reference)
        if not harmonised_buy.eligible or not harmonised_sell.eligible:
            return Decimal(0), None, None, "sizing_failed"
        if harmonised_buy.quantity != harmonised_sell.quantity:
            return Decimal(0), None, None, "sizing_failed"

        quantity = harmonised_buy.quantity
        buy_leg = LegIntent(
            venue=intent.buy_venue,
            instrument_id=intent.buy_instrument_id,
            side="buy",
            quantity=quantity,
            limit_price=harmonised_buy.price or buy_limit,
            reference_price=buy_reference,
            order_type=self.config.order_type,
            time_in_force=self.config.time_in_force,
            risk_reducing=intent.risk_reducing,
            data_age_ns=max(0, timestamp_ns - buy_book.last_update_ns),
            book_valid=buy_book.validity is BookValidity.VALID,
        )
        sell_leg = LegIntent(
            venue=intent.sell_venue,
            instrument_id=intent.sell_instrument_id,
            side="sell",
            quantity=quantity,
            limit_price=harmonised_sell.price or sell_limit,
            reference_price=sell_reference,
            order_type=self.config.order_type,
            time_in_force=self.config.time_in_force,
            risk_reducing=intent.risk_reducing,
            data_age_ns=max(0, timestamp_ns - sell_book.last_update_ns),
            book_valid=sell_book.validity is BookValidity.VALID,
        )
        return quantity, buy_leg, sell_leg, ""

    def _create_child(
        self,
        plan: ParentPlan,
        leg: LegIntent,
        role: str,
        timestamp_ns: int,
        *,
        expected: Decimal | None = None,
        liquidity: str = "taker",
    ) -> ChildOrder:
        attempt = sum(1 for order in plan.children if order.role == role)
        cid = client_order_id(self.run_id, plan.plan_id, leg.venue, role, attempt)
        order = ChildOrder(
            child_order_id=f"{plan.plan_id}:{role}:{attempt}",
            client_order_id=cid,
            plan_id=plan.plan_id,
            venue=leg.venue,
            instrument_id=leg.instrument_id,
            side=leg.side,
            order_type=leg.order_type,
            quantity=leg.quantity,
            price=leg.limit_price,
            time_in_force=leg.time_in_force,
            state=ChildState.CREATED,
            created_ns=timestamp_ns,
            role=role,
            liquidity=liquidity,
            expected_price=expected if expected is not None else leg.reference_price,
            post_only=leg.post_only,
        )
        plan.children.append(order)
        self._emit(
            "order_created",
            timestamp_ns,
            {
                "side": leg.side,
                "order_type": leg.order_type,
                "quantity": str(leg.quantity),
                "price": str(leg.limit_price),
                "time_in_force": leg.time_in_force,
                "post_only": leg.post_only,
                "role": role,
            },
            plan_id=plan.plan_id,
            child_order_id=order.child_order_id,
            client_order_id_value=cid,
            venue=leg.venue,
            instrument_id=leg.instrument_id,
        )
        self._set_child_state(plan, order, ChildState.RISK_RESERVED, timestamp_ns)
        return order

    def _dispatch(
        self,
        plan: ParentPlan,
        order: ChildOrder,
        leg: LegIntent,
        timestamp_ns: int,
    ) -> None:
        if order.client_order_id in self._client_ids:
            self.duplicate_suppressed += 1
            self._set_child_state(
                plan,
                order,
                ChildState.REJECTED,
                timestamp_ns,
                reason="duplicate_client_order_id",
            )
            return
        self._client_ids.add(order.client_order_id)
        self._children_by_cid[order.client_order_id] = (
            plan.plan_id,
            order.child_order_id,
        )
        order.sent_ns = timestamp_ns
        self._set_child_state(plan, order, ChildState.DISPATCHED, timestamp_ns)
        self.matcher.send(_to_request(order, leg, timestamp_ns))
        self._schedule(
            timestamp_ns + self.config.ack_timeout_ns,
            "ack_deadline",
            order.client_order_id,
            None,
        )

    # ---------------------------------------------------------------- time
    def _schedule(self, due_ns: int, kind: str, key: str, payload: Any) -> None:
        heapq.heappush(self._timers, (due_ns, next(self._sequence), kind, key, payload))

    def _next_due(self) -> int | None:
        candidates = [
            value
            for value in (
                self.matcher.next_event_ns(),
                self._reports[0][0] if self._reports else None,
                self._timers[0][0] if self._timers else None,
            )
            if value is not None
        ]
        return min(candidates) if candidates else None

    def advance(self, now_ns: int) -> None:
        """Move router time forward, processing everything due by ``now_ns``.

        Venue arrivals come first at a given instant, then reports we have
        received, then timers, so a report and its timeout at the same
        nanosecond resolve in favour of the report.
        """

        target = max(now_ns, self.now_ns)
        while True:
            due = self._next_due()
            if due is None or due > target:
                break
            self.now_ns = max(self.now_ns, due)
            arrival = self.matcher.next_event_ns()
            if arrival is not None and arrival <= due:
                for outcome in self.matcher.release(due):
                    heapq.heappush(
                        self._reports,
                        (
                            outcome.report_timestamp_ns,
                            next(self._sequence),
                            "outcome",
                            outcome,
                        ),
                    )
                continue
            if self._reports and self._reports[0][0] <= due:
                _, _, kind, item = heapq.heappop(self._reports)
                if kind == "outcome":
                    self._on_report(item, due)
                else:
                    self._on_fill_report(item, due)
                continue
            _, _, kind, key, payload = heapq.heappop(self._timers)
            self._on_timer(kind, key, payload, due)
        self.now_ns = target
        self._enforce_residual_limits(target)

    def drain(self, *, horizon_ns: int = DRAIN_HORIZON_NS) -> int:
        """Advance until nothing is pending or the horizon passes."""

        deadline = self.now_ns + horizon_ns
        while True:
            due = self._next_due()
            if due is None or due > deadline:
                break
            self.advance(due)
        return self.now_ns

    def quiescent(self) -> bool:
        """No order in flight, no unread report and no unresolved child."""

        return (
            self.matcher.in_flight_count == 0
            and not self._reports
            and not any(plan.unresolved_children for plan in self.plans.values())
        )

    def on_maker_fills(self, fills: Sequence[SimulatedFill]) -> None:
        """Queue fill reports for resting orders the venue filled from flow."""

        for fill in fills:
            heapq.heappush(
                self._reports,
                (fill.timestamp_ns, next(self._sequence), "fill", fill),
            )

    # ------------------------------------------------------------- reports
    def _locate(self, client_order_id_value: str) -> tuple[ParentPlan, ChildOrder] | None:
        located = self._children_by_cid.get(client_order_id_value)
        if located is None:
            return None
        plan = self.plans.get(located[0])
        order = plan.child(located[1]) if plan is not None else None
        if plan is None or order is None:
            return None
        return plan, order

    def _on_report(self, outcome: MatchOutcome, timestamp_ns: int) -> None:
        located = self._locate(outcome.request.client_order_id)
        if located is None:
            return
        plan, order = located
        if outcome.kind == "cancel":
            if outcome.fills:
                self._apply_fills(plan, order, outcome.fills, timestamp_ns)
            if outcome.accepted and outcome.state == "cancelled" and order.working:
                self._set_child_state(
                    plan, order, ChildState.CANCELLED, timestamp_ns, reason="cancelled"
                )
            self._evaluate_plan(plan, timestamp_ns)
            return

        if order.state in UNRESOLVED_CHILD_STATES:
            if not outcome.accepted:
                self._set_child_state(
                    plan,
                    order,
                    ChildState.REJECTED,
                    timestamp_ns,
                    reason=outcome.reject_reason,
                )
                if outcome.reject_reason in {"venue_unavailable", "book_unavailable"}:
                    self.venue_reject_streak[order.venue] = (
                        self.venue_reject_streak.get(order.venue, 0) + 1
                    )
            else:
                self.venue_reject_streak[order.venue] = 0
                if outcome.fills:
                    self._apply_fills(plan, order, outcome.fills, timestamp_ns)
                elif outcome.state == "acknowledged":
                    self._set_child_state(
                        plan,
                        order,
                        ChildState.ACKNOWLEDGED,
                        timestamp_ns,
                        venue_order_id=outcome.venue_order_id,
                    )
                self._expire_ioc_remainder(plan, order, outcome.expired_quantity, timestamp_ns)
        elif outcome.fills:
            self._apply_fills(plan, order, outcome.fills, timestamp_ns)
        self._evaluate_plan(plan, timestamp_ns)

    def _on_fill_report(self, fill: SimulatedFill, timestamp_ns: int) -> None:
        located = self._locate(fill.client_order_id)
        if located is None:
            return
        plan, order = located
        self._apply_fills(plan, order, [fill], timestamp_ns)
        self._evaluate_plan(plan, timestamp_ns)

    def _expire_ioc_remainder(
        self,
        plan: ParentPlan,
        order: ChildOrder,
        expired_quantity: Decimal,
        timestamp_ns: int,
    ) -> None:
        if expired_quantity > 0 and order.state is ChildState.PARTIALLY_FILLED:
            self._set_child_state(
                plan,
                order,
                ChildState.CANCELLED,
                timestamp_ns,
                reason="ioc_remainder_expired",
            )
            self._emit(
                "order_expired",
                timestamp_ns,
                {"expired_quantity": str(expired_quantity), "time_in_force": order.time_in_force},
                plan_id=plan.plan_id,
                child_order_id=order.child_order_id,
                client_order_id_value=order.client_order_id,
                venue=order.venue,
                instrument_id=order.instrument_id,
            )

    def _apply_fills(
        self,
        plan: ParentPlan,
        order: ChildOrder,
        fills: Sequence[SimulatedFill],
        timestamp_ns: int | None = None,
        *,
        source_override: str | None = None,
    ) -> None:
        applied = False
        for fill in fills:
            if fill.fill_id:
                if fill.fill_id in self._applied_fill_ids:
                    continue
                self._applied_fill_ids.add(fill.fill_id)
            applied = True
            ledger_ns = fill.timestamp_ns if timestamp_ns is None else max(
                timestamp_ns, fill.execution_timestamp_ns
            )
            instrument = self.security_master.resolve(
                order.instrument_id, fill.execution_timestamp_ns or fill.timestamp_ns
            )
            economics = self.cost_model.leg_economics(
                instrument,
                side=fill.side,
                quantity=fill.quantity,
                price=fill.price,
                at_ns=fill.execution_timestamp_ns or fill.timestamp_ns,
                liquidity=fill.liquidity,
                reference_price=order.price,
            )
            previous_notional = order.filled_quantity * (
                order.average_price or Decimal(0)
            )
            order.filled_quantity += fill.quantity
            order.average_price = (
                previous_notional + fill.quantity * fill.price
            ) / order.filled_quantity
            order.fees += economics.fee
            if not order.first_fill_execution_ns:
                order.first_fill_execution_ns = fill.execution_timestamp_ns or ledger_ns
            plan.realized_legs.append(economics)
            self.risk_engine.apply_fill(
                order.instrument_id,
                order.venue,
                fill.side,
                fill.quantity,
                fill.price,
                fee=economics.fee,
            )
            latency_slippage_bps = Decimal(0)
            if order.expected_price and order.expected_price > 0:
                signed = (
                    fill.price - order.expected_price
                    if fill.side == "buy"
                    else order.expected_price - fill.price
                )
                latency_slippage_bps = signed / order.expected_price * BPS
            self.execution_quality.append(
                {
                    "plan_id": plan.plan_id,
                    "child_order_id": order.child_order_id,
                    "role": order.role,
                    "venue": order.venue,
                    "side": fill.side,
                    "quantity": str(fill.quantity),
                    "liquidity": fill.liquidity.value,
                    "expected_price": str(order.expected_price),
                    "fill_price": str(fill.price),
                    "latency_slippage_bps": str(latency_slippage_bps),
                    "sent_ns": order.sent_ns,
                    "execution_ns": fill.execution_timestamp_ns,
                    "report_ns": ledger_ns,
                }
            )
            self._emit(
                "fill",
                ledger_ns,
                {
                    "fill_id": fill.fill_id,
                    "side": fill.side,
                    "quantity": str(fill.quantity),
                    "price": str(fill.price),
                    "fee": str(economics.fee),
                    "fee_bps": str(economics.fee_bps),
                    "slippage_bps": str(economics.slippage_bps),
                    "expected_price": str(order.expected_price),
                    "latency_slippage_bps": str(latency_slippage_bps),
                    "execution_timestamp_ns": fill.execution_timestamp_ns,
                    "liquidity": fill.liquidity.value,
                    "levels_consumed": fill.levels_consumed,
                    "base_currency": instrument.base_currency,
                    "quote_currency": instrument.quote_currency,
                },
                plan_id=plan.plan_id,
                child_order_id=order.child_order_id,
                client_order_id_value=order.client_order_id,
                venue=order.venue,
                instrument_id=order.instrument_id,
                source=source_override or fill.source.value,
            )
        if not applied:
            return
        target = (
            ChildState.FILLED
            if order.filled_quantity >= order.quantity
            else ChildState.PARTIALLY_FILLED
        )
        if order.state is not target and target in CHILD_TRANSITIONS[order.state]:
            self._set_child_state(
                plan,
                order,
                target,
                ledger_ns,
            )

    # -------------------------------------------------------------- timers
    def _on_timer(self, kind: str, key: str, payload: Any, timestamp_ns: int) -> None:
        if kind == "ack_deadline":
            located = self._locate(key)
            if located is None:
                return
            plan, order = located
            if order.state is ChildState.DISPATCHED:
                # Silence is not a rejection: the order may have traded.
                self._set_child_state(
                    plan, order, ChildState.UNKNOWN, timestamp_ns, reason="ack_timeout"
                )
                self._emit(
                    "order_timeout",
                    timestamp_ns,
                    {"deadline_ns": timestamp_ns, "sent_ns": order.sent_ns},
                    plan_id=plan.plan_id,
                    child_order_id=order.child_order_id,
                    client_order_id_value=order.client_order_id,
                    venue=order.venue,
                    instrument_id=order.instrument_id,
                )
                self._send_query(order, timestamp_ns)
            return
        if kind == "query_send":
            located = self._locate(key)
            if located is not None and located[1].state in {
                ChildState.UNKNOWN,
                ChildState.TIMED_OUT,
            }:
                self._send_query(located[1], timestamp_ns)
            return
        if kind == "query_land":
            located = self._locate(key)
            if located is None:
                return
            plan, order = located
            if order.state not in {ChildState.UNKNOWN, ChildState.TIMED_OUT}:
                return
            view = self.matcher.query_order(key, timestamp_ns)
            self._schedule(
                timestamp_ns + self.latency.draw(order.venue, "submit_ack"),
                "query_response",
                key,
                view,
            )
            return
        if kind == "query_response":
            located = self._locate(key)
            if located is None:
                return
            plan, order = located
            if order.state not in {ChildState.UNKNOWN, ChildState.TIMED_OUT}:
                return
            self._on_query_response(plan, order, payload, timestamp_ns)
            return
        if kind == "residual_retry":
            plan = self.plans.get(key)
            if plan is None or plan.state is PlanState.CLOSED:
                return
            if plan.unresolved_children or plan.working_children:
                return
            if plan.net_exposure != 0:
                self._send_unwind(plan, timestamp_ns)
            return
        if kind == "maker_ttl":
            plan = self.plans.get(key)
            if plan is None:
                return
            maker = plan.child(plan.maker_child_id)
            if maker is not None and maker.working:
                self.matcher.request_cancel(maker.client_order_id, timestamp_ns)
            return

    def _send_query(self, order: ChildOrder, timestamp_ns: int) -> None:
        self._schedule(
            timestamp_ns + self.latency.draw(order.venue, "entry"),
            "query_land",
            order.client_order_id,
            None,
        )

    def _on_query_response(
        self,
        plan: ParentPlan,
        order: ChildOrder,
        view: VenueOrderView | None,
        timestamp_ns: int,
    ) -> None:
        order.query_attempts += 1
        self._emit(
            "order_query",
            timestamp_ns,
            {
                "attempt": order.query_attempts,
                "venue_state": None if view is None else view.state,
                "venue_filled_quantity": (
                    None if view is None else str(view.filled_quantity)
                ),
            },
            plan_id=plan.plan_id,
            child_order_id=order.child_order_id,
            client_order_id_value=order.client_order_id,
            venue=order.venue,
            instrument_id=order.instrument_id,
        )
        if view is None:
            if order.query_attempts < self.config.max_order_queries:
                self._schedule(
                    timestamp_ns + self.config.query_retry_ns,
                    "query_send",
                    order.client_order_id,
                    None,
                )
                return
            if order.state is ChildState.UNKNOWN:
                self._set_child_state(
                    plan,
                    order,
                    ChildState.TIMED_OUT,
                    timestamp_ns,
                    reason="venue_unconfirmed",
                )
                self.unresolved_orders.append(
                    {
                        "timestamp_ns": timestamp_ns,
                        "plan_id": plan.plan_id,
                        "child_order_id": order.child_order_id,
                        "client_order_id": order.client_order_id,
                        "venue": order.venue,
                        "query_attempts": order.query_attempts,
                    }
                )
            self._evaluate_plan(plan, timestamp_ns)
            return

        # The venue is the source of truth: record what it says happened.
        self._apply_fills(
            plan, order, view.fills, timestamp_ns, source_override="venue_query"
        )
        if view.state == "rejected" and order.state in UNRESOLVED_CHILD_STATES:
            self._set_child_state(
                plan, order, ChildState.REJECTED, timestamp_ns, reason=view.reject_reason
            )
        elif view.state == "cancelled":
            if order.state is ChildState.PARTIALLY_FILLED:
                self._set_child_state(
                    plan,
                    order,
                    ChildState.CANCELLED,
                    timestamp_ns,
                    reason="ioc_remainder_expired",
                )
            elif order.state in {ChildState.UNKNOWN, ChildState.TIMED_OUT}:
                self._set_child_state(
                    plan, order, ChildState.CANCELLED, timestamp_ns, reason="venue_cancelled"
                )
        elif view.state in {"acknowledged", "partially_filled"} and order.state in {
            ChildState.UNKNOWN,
            ChildState.TIMED_OUT,
        }:
            self._set_child_state(plan, order, ChildState.ACKNOWLEDGED, timestamp_ns)
        self._evaluate_plan(plan, timestamp_ns)

    # ----------------------------------------------------------- settlement
    def _evaluate_plan(self, plan: ParentPlan, timestamp_ns: int) -> None:
        if plan.state in {PlanState.CLOSED, PlanState.RISK_REJECTED, PlanState.CREATED, PlanState.RISK_RESERVED}:
            return
        if plan.phase == "entry":
            if plan.execution_style == ExecutionStyle.MAKER_TAKER.value:
                self._hedge_maker_fills(plan, timestamp_ns)
            if plan.unresolved_children:
                if (
                    plan.state is PlanState.DISPATCHED
                    and any(c.state is ChildState.TIMED_OUT for c in plan.children)
                    and not any(
                        c.state in {ChildState.DISPATCHED, ChildState.UNKNOWN}
                        for c in plan.children
                    )
                ):
                    self._set_plan_state(
                        plan, PlanState.TIMED_OUT, timestamp_ns, "venue_unconfirmed"
                    )
                return
            if plan.working_children:
                return
            if plan.pending_legs:
                self._release_dependent_leg(plan, timestamp_ns)
                if plan.unresolved_children:
                    return
            self._settle(plan, timestamp_ns)
            return
        if plan.phase == "residual":
            if plan.unresolved_children or plan.working_children:
                return
            if plan.net_exposure == 0:
                self._close_flat(plan, timestamp_ns)
                return
            self._continue_residual(plan, timestamp_ns)

    def _release_dependent_leg(self, plan: ParentPlan, timestamp_ns: int) -> None:
        role, leg = plan.pending_legs.pop(0)
        first_filled = sum(
            (order.filled_quantity for order in plan.children), Decimal(0)
        )
        if first_filled <= 0:
            return
        sizing = self.security_master.size_order(
            leg.instrument_id,
            at_ns=timestamp_ns,
            side=leg.side,
            target_quantity=min(first_filled, leg.quantity),
            price=leg.limit_price,
            reference_price=leg.reference_price,
            order_type=leg.order_type,
            time_in_force=leg.time_in_force,
        )
        if not sizing.eligible or sizing.quantity <= 0:
            return
        dependent = replace(leg, quantity=sizing.quantity)
        order = self._create_child(
            plan, dependent, role, timestamp_ns, expected=leg.reference_price
        )
        self._dispatch(plan, order, dependent, timestamp_ns)

    def _hedge_maker_fills(self, plan: ParentPlan, timestamp_ns: int) -> None:
        if plan.hedge_template is None:
            return
        maker = plan.child(plan.maker_child_id)
        if maker is None:
            return
        outstanding = maker.filled_quantity - plan.hedge_requested
        if outstanding <= 0:
            return
        role, template = plan.hedge_template
        book = self.books.get(template.venue)
        best = None if book is None else book.best_bid
        if best is None:
            return
        limit = best * (Decimal(1) - self.config.aggression_bps / BPS)
        sizing = self.security_master.size_order(
            template.instrument_id,
            at_ns=timestamp_ns,
            side=template.side,
            target_quantity=outstanding,
            price=limit,
            reference_price=book.mid_price,
            order_type="limit",
            time_in_force="ioc",
        )
        if not sizing.eligible or sizing.quantity <= 0:
            return
        plan.hedge_requested += sizing.quantity
        leg = replace(
            template,
            quantity=sizing.quantity,
            limit_price=sizing.price or limit,
            reference_price=book.mid_price or limit,
            time_in_force="ioc",
            post_only=False,
        )
        order = self._create_child(plan, leg, role, timestamp_ns, expected=best)
        self._dispatch(plan, order, leg, timestamp_ns)

    def _settle(self, plan: ParentPlan, timestamp_ns: int) -> None:
        legs = [order for order in plan.children if order.role in {"buy", "sell"}]
        bought = sum(
            (order.filled_quantity for order in legs if order.side == "buy"),
            Decimal(0),
        )
        sold = sum(
            (order.filled_quantity for order in legs if order.side == "sell"),
            Decimal(0),
        )
        matched = min(bought, sold)
        residual = plan.net_exposure

        if residual == 0 and matched > 0:
            self._set_plan_state(plan, PlanState.FILLED, timestamp_ns, "both_legs")
            self._close_flat(plan, timestamp_ns)
            return
        if residual == 0:
            reason = next(
                (order.reject_reason for order in legs if order.reject_reason),
                "no_fill",
            )
            self._set_plan_state(plan, PlanState.REJECTED, timestamp_ns, reason)
            self._close_flat(plan, timestamp_ns)
            return

        source_side = "buy" if residual > 0 else "sell"
        source_orders = [order for order in legs if order.side == source_side] or legs
        source_order = source_orders[0]
        plan.residual_quantity = abs(residual)
        plan.residual_side = "sell" if residual > 0 else "buy"
        plan.residual_venue = source_order.venue
        plan.residual_instrument_id = source_order.instrument_id
        # Exposure has existed since the over-filled leg executed, not since
        # the router noticed it.
        plan.residual_opened_ns = min(
            (
                order.first_fill_execution_ns
                for order in source_orders
                if order.first_fill_execution_ns
            ),
            default=timestamp_ns,
        )
        plan.phase = "residual"
        self._set_plan_state(
            plan,
            PlanState.PARTIALLY_FILLED,
            timestamp_ns,
            f"residual:{plan.residual_quantity}",
        )
        self._start_residual(plan, timestamp_ns)

    def _start_residual(self, plan: ParentPlan, timestamp_ns: int) -> None:
        self._check_residual_limits(plan, timestamp_ns)
        if plan.residual_breached:
            if not plan.unresolved_children:
                self._send_unwind(plan, timestamp_ns)
            return
        policy = self.config.residual_policy
        if policy is ResidualPolicy.HOLD:
            plan.reason = "residual_held"
            return
        if (
            policy is ResidualPolicy.HEDGE_THEN_UNWIND
            and plan.hedge_attempts < self.config.max_hedge_attempts
        ):
            self._send_hedge(plan, timestamp_ns)
            return
        if plan.state is not PlanState.UNWINDING:
            self._set_plan_state(plan, PlanState.UNWINDING, timestamp_ns)
        self._send_unwind(plan, timestamp_ns)

    def _continue_residual(self, plan: ParentPlan, timestamp_ns: int) -> None:
        exposure = plan.net_exposure
        plan.residual_quantity = abs(exposure)
        plan.residual_side = "sell" if exposure > 0 else "buy"
        if plan.state is PlanState.HEDGING:
            if plan.hedge_attempts < self.config.max_hedge_attempts:
                self._send_hedge(plan, timestamp_ns)
                return
            self._set_plan_state(plan, PlanState.UNWINDING, timestamp_ns, "hedge_incomplete")
            self._send_unwind(plan, timestamp_ns)
            return
        if plan.state in {PlanState.UNWINDING, PlanState.RESIDUAL_BREACH}:
            plan.unwind_attempts += 1
            self._schedule(
                timestamp_ns + self._retry_delay_ns(plan),
                "residual_retry",
                plan.plan_id,
                None,
            )
            return
        if plan.state in {PlanState.PARTIALLY_FILLED, PlanState.TIMED_OUT}:
            if plan.reason == "residual_held":
                return
            self._start_residual(plan, timestamp_ns)

    def _send_hedge(self, plan: ParentPlan, timestamp_ns: int) -> None:
        plan.hedge_attempts += 1
        if plan.state is not PlanState.HEDGING:
            self._set_plan_state(plan, PlanState.HEDGING, timestamp_ns)
        hedge_venue = (
            plan.intent.sell_venue
            if plan.residual_side == "sell"
            else plan.intent.buy_venue
        )
        hedge_instrument = (
            plan.intent.sell_instrument_id
            if plan.residual_side == "sell"
            else plan.intent.buy_instrument_id
        )
        dispatched = self._execute_reducing_order(
            plan,
            role="hedge",
            venue=hedge_venue,
            instrument_id=hedge_instrument,
            side=plan.residual_side,
            quantity=plan.residual_quantity,
            timestamp_ns=timestamp_ns,
            aggression_bps=self.config.aggression_bps * 5,
        )
        if not dispatched:
            self._set_plan_state(plan, PlanState.UNWINDING, timestamp_ns, "hedge_not_dispatched")
            self._send_unwind(plan, timestamp_ns)

    def _send_unwind(self, plan: ParentPlan, timestamp_ns: int) -> None:
        if plan.state not in {PlanState.UNWINDING, PlanState.RESIDUAL_BREACH}:
            self._set_plan_state(plan, PlanState.UNWINDING, timestamp_ns)
        exposure = plan.net_exposure
        if exposure == 0:
            return
        plan.residual_quantity = abs(exposure)
        plan.residual_side = "sell" if exposure > 0 else "buy"
        aggression = min(
            self.config.aggression_bps * 5 * Decimal(1 + plan.unwind_attempts),
            self.config.max_unwind_aggression_bps,
        )
        dispatched = self._execute_reducing_order(
            plan,
            role="unwind",
            venue=plan.residual_venue,
            instrument_id=plan.residual_instrument_id,
            side=plan.residual_side,
            quantity=plan.residual_quantity,
            timestamp_ns=timestamp_ns,
            aggression_bps=aggression,
        )
        if not dispatched:
            plan.unwind_attempts += 1
            self._schedule(
                timestamp_ns + self._retry_delay_ns(plan),
                "residual_retry",
                plan.plan_id,
                None,
            )

    def _retry_delay_ns(self, plan: ParentPlan) -> int:
        """Exponential backoff, never faster than a venue's rate-limit window.

        Retrying an unwind faster than the venue admits orders only produces
        more rate-limit rejects and keeps the residual open longer.
        """

        exponent = min(max(plan.unwind_attempts - 1, 0), 16)
        delay = self.config.residual_retry_ns * (2**exponent)
        last = next(
            (order for order in reversed(plan.children) if order.role in {"hedge", "unwind"}),
            None,
        )
        if last is not None and last.reject_reason == "rate_limit":
            delay = max(delay, 1_000_000_000)
        return min(delay, self.config.max_residual_retry_ns)

    def _close_flat(self, plan: ParentPlan, timestamp_ns: int) -> None:
        reason = "unwound" if plan.phase == "residual" else plan.reason
        if plan.state is PlanState.HEDGING:
            self._set_plan_state(plan, PlanState.FILLED, timestamp_ns, "hedged")
        self._set_plan_state(plan, PlanState.RECONCILED, timestamp_ns, reason if plan.phase == "residual" else "")
        self._set_plan_state(plan, PlanState.CLOSED, timestamp_ns)
        plan.residual_quantity = Decimal(0)
        plan.phase = "closed"
        plan.closed_ns = timestamp_ns
        self.risk_engine.release(plan.reservation)
        self.risk_engine.release_hold(plan.plan_id)
        plan.reservation = None

    def _execute_reducing_order(
        self,
        plan: ParentPlan,
        *,
        role: str,
        venue: str,
        instrument_id: str,
        side: str,
        quantity: Decimal,
        timestamp_ns: int,
        aggression_bps: Decimal,
    ) -> bool:
        book = self.books.get(venue)
        if book is None or not book.initialized:
            return False
        reference = book.mid_price
        best = book.best_ask if side == "buy" else book.best_bid
        if reference is None or best is None:
            return False
        aggression = aggression_bps / BPS
        limit = (
            best * (Decimal(1) + aggression)
            if side == "buy"
            else best * (Decimal(1) - aggression)
        )
        sizing = self.security_master.size_order(
            instrument_id,
            at_ns=timestamp_ns,
            side=side,
            target_quantity=quantity,
            price=limit,
            reference_price=reference,
            order_type=self.config.order_type,
            time_in_force=self.config.time_in_force,
            allow_risk_reducing=True,
        )
        if not sizing.eligible or sizing.quantity <= 0:
            return False
        leg = LegIntent(
            venue=venue,
            instrument_id=instrument_id,
            side=side,
            quantity=sizing.quantity,
            limit_price=sizing.price or limit,
            reference_price=reference,
            order_type=self.config.order_type,
            time_in_force=self.config.time_in_force,
            risk_reducing=True,
            data_age_ns=max(0, timestamp_ns - book.last_update_ns),
            book_valid=book.validity is BookValidity.VALID,
        )
        decision = self.risk_engine.check(
            RiskCheckRequest(
                instrument_id=instrument_id,
                venue=venue,
                side=side,
                quantity=leg.quantity,
                price=leg.limit_price,
                notional=leg.notional,
                reference_price=reference,
                data_age_ns=leg.data_age_ns,
                book_valid=leg.book_valid,
                risk_reducing=True,
                order_type=leg.order_type,
            ),
            timestamp_ns,
        )
        self._emit(
            "risk_decision",
            timestamp_ns,
            {**decision.to_dict(), "role": role},
            plan_id=plan.plan_id,
        )
        if not decision.approved:
            return False
        self.risk_engine.record_message(timestamp_ns)
        order = self._create_child(plan, leg, role, timestamp_ns, expected=reference)
        self._dispatch(plan, order, leg, timestamp_ns)
        return True

    # ------------------------------------------------------ residual limits
    def _check_residual_limits(self, plan: ParentPlan, timestamp_ns: int) -> None:
        exposure = abs(plan.net_exposure)
        if exposure == 0 or plan.residual_breached or not plan.residual_venue:
            return
        book = self.books.get(plan.residual_venue)
        mark = (book.mid_price if book is not None else None) or next(
            (order.average_price for order in plan.children if order.average_price),
            Decimal(0),
        )
        notional = exposure * mark
        age = max(0, timestamp_ns - plan.residual_opened_ns)
        limit, reason = self.risk_engine.residual_breach(notional, age)
        if limit is None:
            return
        plan.residual_breached = True
        plan.residual_breached_ns = timestamp_ns
        # New risk is held while this residual is open; the supervisor
        # escalates to the kill switch if the breach outlives its window.
        self.risk_engine.hold_new_risk(plan.plan_id, limit, reason, timestamp_ns)
        record = {
            "timestamp_ns": timestamp_ns,
            "plan_id": plan.plan_id,
            "limit_id": limit.value,
            "reason": reason,
            "residual_quantity": str(exposure),
            "residual_notional": str(notional),
            "residual_age_ns": age,
            "venue": plan.residual_venue,
        }
        self.residual_breaches.append(record)
        self.risk_engine.binding_events.append(
            {
                "timestamp_ns": timestamp_ns,
                "limit_id": limit.value,
                "reason": reason,
                "plan_id": plan.plan_id,
                "venue": plan.residual_venue,
            }
        )
        self._emit("residual_breach", timestamp_ns, record, plan_id=plan.plan_id)
        if plan.state in {
            PlanState.PARTIALLY_FILLED,
            PlanState.HEDGING,
            PlanState.UNWINDING,
            PlanState.TIMED_OUT,
        }:
            self._set_plan_state(plan, PlanState.RESIDUAL_BREACH, timestamp_ns, reason)

    def _enforce_residual_limits(self, timestamp_ns: int) -> None:
        for plan in list(self.plans.values()):
            if plan.phase != "residual" or plan.state is PlanState.CLOSED:
                continue
            was_breached = plan.residual_breached
            self._check_residual_limits(plan, timestamp_ns)
            if (
                plan.residual_breached
                and not was_breached
                and not plan.unresolved_children
                and not plan.working_children
            ):
                # Held or slow residuals are forced out the moment a limit binds.
                self._send_unwind(plan, timestamp_ns)

    # ------------------------------------------------------------ bookkeeping
    def _record_miss(
        self, intent: OpportunityIntent, reason: str, timestamp_ns: int
    ) -> None:
        self.missed_opportunities.append(
            {
                "timestamp_ns": timestamp_ns,
                "opportunity_id": intent.opportunity_id,
                "economic_group": intent.economic_group,
                "reason": reason,
                "signal": str(intent.signal),
            }
        )
        self._emit(
            "opportunity_missed",
            timestamp_ns,
            {
                "opportunity_id": intent.opportunity_id,
                "reason": reason,
                "signal": str(intent.signal),
            },
        )

    def cancel_all(self, timestamp_ns: int, reason: str) -> int:
        """Request cancellation of every working order; cancels take latency."""

        requested = 0
        for plan in self.plans.values():
            for order in plan.children:
                if order.working:
                    if self.matcher.request_cancel(order.client_order_id, timestamp_ns) is not None:
                        requested += 1
        if requested:
            self._emit(
                "cancel_all",
                timestamp_ns,
                {"reason": reason, "requested": requested},
            )
        return requested

    def flatten_positions(self, timestamp_ns: int, reason: str) -> int:
        """Open one reduce-only plan per non-flat position not owned by a plan."""

        if any(plan.state is not PlanState.CLOSED for plan in self.plans.values()):
            return 0
        opened = 0
        for (instrument_id, venue), quantity in sorted(
            self.risk_engine.positions().items()
        ):
            if quantity == 0:
                continue
            opportunity_id = f"flatten|{reason}|{venue}|{instrument_id}|{timestamp_ns}"
            plan_id = plan_id_for(self.run_id, opportunity_id)
            if plan_id in self.plans:
                continue
            intent = OpportunityIntent(
                opportunity_id=opportunity_id,
                economic_group="flatten",
                feature_id="kill_switch",
                as_of_ns=timestamp_ns,
                target_quantity=abs(quantity),
                buy_venue=venue,
                sell_venue=venue,
                buy_instrument_id=instrument_id,
                sell_instrument_id=instrument_id,
                signal=Decimal(0),
                reason=f"flatten:{reason}",
                risk_reducing=True,
            )
            plan = ParentPlan(
                plan_id=plan_id,
                opportunity_id=opportunity_id,
                economic_group="flatten",
                feature_id="kill_switch",
                created_ns=timestamp_ns,
                state=PlanState.CREATED,
                intent=intent,
                candidate=_infeasible(venue, venue, abs(quantity), "flatten"),
                opening_exposure=quantity,
                phase="residual",
                residual_quantity=abs(quantity),
                residual_side="sell" if quantity > 0 else "buy",
                residual_venue=venue,
                residual_instrument_id=instrument_id,
                residual_opened_ns=timestamp_ns,
            )
            self.plans[plan_id] = plan
            self._emit(
                "plan_created",
                timestamp_ns,
                {
                    "opportunity_id": opportunity_id,
                    "economic_group": "flatten",
                    "reason": f"flatten:{reason}",
                    "opening_exposure": str(quantity),
                    "legs": [],
                },
                plan_id=plan_id,
            )
            self._set_plan_state(plan, PlanState.RISK_RESERVED, timestamp_ns, "risk_reducing")
            self._set_plan_state(plan, PlanState.DISPATCHED, timestamp_ns)
            self._set_plan_state(
                plan, PlanState.PARTIALLY_FILLED, timestamp_ns, f"flatten:{reason}"
            )
            self._set_plan_state(plan, PlanState.UNWINDING, timestamp_ns)
            self._send_unwind(plan, timestamp_ns)
            opened += 1
        return opened

    def open_residual_breaches(self) -> list[dict[str, Any]]:
        return [
            {
                "plan_id": plan.plan_id,
                "breached_ns": plan.residual_breached_ns,
                "net_exposure": str(plan.net_exposure),
                "venue": plan.residual_venue,
            }
            for plan in self.plans.values()
            if plan.residual_breached and plan.state is not PlanState.CLOSED
        ]

    def open_plans(self) -> tuple[ParentPlan, ...]:
        return tuple(
            plan for plan in self.plans.values() if plan.state is not PlanState.CLOSED
        )

    def summary(self) -> dict[str, Any]:
        states: dict[str, int] = {}
        for plan in self.plans.values():
            states[plan.state.value] = states.get(plan.state.value, 0) + 1
        slippage = [
            Decimal(item["latency_slippage_bps"])
            for item in self.execution_quality
            if item["role"] in {"buy", "sell"}
        ]
        return {
            "plans": len(self.plans),
            "plan_states": dict(sorted(states.items())),
            "duplicate_suppressed": self.duplicate_suppressed,
            "missed_opportunities": len(self.missed_opportunities),
            "open_plans": len(self.open_plans()),
            "residual_breaches": len(self.residual_breaches),
            "unresolved_orders": len(self.unresolved_orders),
            "entry_fills": len(slippage),
            "mean_latency_slippage_bps": (
                str(sum(slippage, Decimal(0)) / len(slippage)) if slippage else "0"
            ),
        }


def recover_router_state(
    ledger: EventLedger, run_id: str
) -> tuple[DerivedState, dict[str, Any]]:
    """Rebuild the picture a restarted process must reconcile against."""

    state = ledger.derive(run_id=run_id)
    open_orders = state.open_orders()
    unclosed_plans = {
        plan_id: plan
        for plan_id, plan in state.plans.items()
        if plan.get("state") not in {PlanState.CLOSED.value, None}
    }
    residuals: dict[str, str] = {}
    for (instrument_id, venue), position in state.positions.items():
        if position.quantity != 0:
            residuals[f"{instrument_id}@{venue}"] = str(position.quantity)
    return state, {
        "run_id": run_id,
        "event_count": state.event_count,
        "open_orders": [order.to_dict() for order in open_orders],
        "unclosed_plans": unclosed_plans,
        "residual_positions": residuals,
        "state_hash": state.state_hash(),
    }


def _to_request(
    order: ChildOrder, leg: LegIntent, timestamp_ns: int
) -> OrderRequest:
    return OrderRequest(
        client_order_id=order.client_order_id,
        child_order_id=order.child_order_id,
        plan_id=order.plan_id,
        venue=order.venue,
        instrument_id=order.instrument_id,
        side=order.side,
        order_type=order.order_type,
        quantity=order.quantity,
        price=order.price,
        time_in_force=order.time_in_force,
        timestamp_ns=timestamp_ns,
        post_only=leg.post_only,
        risk_reducing=leg.risk_reducing,
    )


def _infeasible(
    buy_venue: str, sell_venue: str, quantity: Decimal, reason: str
) -> RouteCandidate:
    zero = Decimal(0)
    return RouteCandidate(
        buy_venue=buy_venue,
        sell_venue=sell_venue,
        quantity=quantity,
        buy_price=zero,
        sell_price=zero,
        gross_edge=zero,
        fees=zero,
        slippage_cost=zero,
        latency_penalty=zero,
        net_edge=Decimal("-1e18"),
        feasible=False,
        reason=reason,
        fill_probability=zero,
        expected_value=Decimal("-1e18"),
    )


def _empty_result(
    intent: OpportunityIntent, plan_id: str, reason: str
) -> PlanResult:
    placeholder = ParentPlan(
        plan_id=plan_id,
        opportunity_id=intent.opportunity_id,
        economic_group=intent.economic_group,
        feature_id=intent.feature_id,
        created_ns=intent.as_of_ns,
        state=PlanState.CREATED,
        intent=intent,
        candidate=_infeasible(
            intent.buy_venue, intent.sell_venue, intent.target_quantity, reason
        ),
        reason=reason,
    )
    return PlanResult(placeholder, False, reason, (), (), ())


ORDER_STATE_MACHINE_DOC: dict[str, Any] = {
    "parent_plan": {
        state.value: sorted(target.value for target in targets)
        for state, targets in PLAN_TRANSITIONS.items()
    },
    "child_order": {
        state.value: sorted(target.value for target in targets)
        for state, targets in CHILD_TRANSITIONS.items()
    },
    "notes": [
        "Client order identifiers are uuid5 over run, plan, venue, role and "
        "attempt, so a retry after a restart reuses the same identifier and "
        "the venue rejects the duplicate instead of doubling the position.",
        "Risk capacity is reserved for the whole parent plan before any child "
        "order is dispatched and released only when the plan reaches a "
        "terminal state.",
        "A plan cannot become reconciled or closed while it holds residual "
        "exposure or while any child order is dispatched, unknown or timed "
        "out; the transition raises instead.",
        "A missing acknowledgement moves the child to unknown and the venue is "
        "queried; the order is never resent under a new identifier while its "
        "fate is unknown. Exhausted queries move it to timed_out, which blocks "
        "the plan and trips the kill switch.",
        "A residual is always owned: it is hedged on the intended venue first "
        "and unwound with escalating aggression until flat. Breaching the "
        "residual size or duration limit moves the plan to residual_breach "
        "and forces the unwind.",
        "An IOC order's unfilled remainder is cancelled explicitly and "
        "recorded as an order_expired ledger event.",
        "Orders execute against the book when they arrive at the venue after "
        "the entry latency, not when they were decided.",
    ],
}
