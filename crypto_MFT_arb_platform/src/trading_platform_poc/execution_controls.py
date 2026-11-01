"""Execution controls: rate limits, throttles, stale feeds, kill switches.

Every control in this module answers one question: *may this message be
transmitted right now, and if not, what happens to it instead?* The
answer must be deterministic enough that an exchange, a regulator or an
internal risk committee can reproduce any decision from the inputs alone,
so the control-to-order-state matrix is encoded as literal data in
:data:`CONTROL_MATRIX` rather than scattered through branching code.

Two principles run through the whole module:

1. **Risk-reducing actions are never blocked.** A cancel, an emergency
   hedge or a forced liquidation reduces exposure. Throttling those to
   protect a message budget would trade a small operational problem for a
   large financial one.
2. **Higher controls always win.** :class:`ControlLevel` is ordered, and
   the most senior binding control decides. A strategy may tighten its own
   limits but can never relax a firm-level one.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, IntEnum
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar

import structlog

logger = structlog.get_logger(__name__)


class ControlLevel(IntEnum):
    """Precedence hierarchy. Lower ordinal means more senior.

    Higher-level controls always override lower-level ones. A portfolio
    manager can tighten a strategy limit; nothing below
    :attr:`GLOBAL_KILL_SWITCH` can relax it.
    """

    FIRM_RISK = 0
    GLOBAL_KILL_SWITCH = 1
    FIRM_MESSAGE_LIMIT = 2
    PM_LIMIT = 3
    STRATEGY_LIMIT = 4
    VENUE_LIMIT = 5
    INSTRUMENT_LIMIT = 6
    ORDER = 7


class Priority(IntEnum):
    """Scheduling class. Lower ordinal is served first.

    Capacity is allocated in this order, so research traffic starves
    before quoting, and quoting starves before a hedge.
    """

    FORCED_LIQUIDATION = 0
    EMERGENCY_HEDGE = 1
    RISK_REDUCING = 2
    MARKET_MAKING = 3
    QUOTE_REFRESH = 4
    RESEARCH = 5


#: How far up a shared budget each class may push utilisation.
#:
#: Priority that only reorders a queue does not *allocate* capacity: a
#: research backfill arriving first still spends the firm budget before
#: any market-making message is evaluated, and the queue can only
#: reorder the ones that already lost. A ceiling reserves the headroom
#: instead, so the lower classes starve first, which is the point.
PRIORITY_CEILING_PCT: Mapping[Priority, Decimal] = {
    Priority.MARKET_MAKING: Decimal(100),
    Priority.QUOTE_REFRESH: Decimal(80),
    Priority.RESEARCH: Decimal(60),
}


#: Classes that reduce exposure. No throttle may block these.
EXEMPT_PRIORITIES = frozenset(
    {
        Priority.FORCED_LIQUIDATION,
        Priority.EMERGENCY_HEDGE,
        Priority.RISK_REDUCING,
    }
)


class OrderStage(str, Enum):
    """Rows of the control matrix: where an order sits in its life."""

    NEW = "new"
    RISK_APPROVED_WAITING = "risk_approved_waiting"
    SENT_AWAITING_ACK = "sent_awaiting_ack"
    RESTING = "resting"
    PARTIAL_FILL = "partial_fill"
    FILLED_AWAITING_HEDGE = "filled_awaiting_hedge"
    REPLACE_REQUEST = "replace_request"
    CANCEL_REQUEST = "cancel_request"
    EMERGENCY_HEDGE = "emergency_hedge"
    FORCED_LIQUIDATION = "forced_liquidation"


class ControlKind(str, Enum):
    """Columns of the control matrix."""

    VENUE_RATE_LIMIT = "venue_rate_limit"
    GLOBAL_THROTTLE = "global_throttle"
    STALE_FEED = "stale_feed"
    KILL_SWITCH = "kill_switch"


class ControlAction(str, Enum):
    """What actually happens to the message.

    The distinctions matter. ``REJECT`` never generates a message and the
    caller learns synchronously. ``QUEUE`` means the order is live
    internally and will either be sent or expire. ``DELAY`` is an
    intentional postponement -- an auction, a volatility interruption --
    and the order remains valid throughout.
    """

    ADMIT = "admit"
    QUEUE = "queue"
    DELAY = "delay"
    REJECT = "reject"
    CANCEL_ADMISSION = "cancel_admission"
    CANCEL = "cancel"
    MASS_CANCEL = "mass_cancel"
    MONITOR = "monitor"
    HEDGE_ONLY = "hedge_only"


#: The behaviour every control requires of every order stage.
#:
#: Encoded as data so it can be inspected, exported and asserted against
#: directly. ``None`` means the control does not apply to that stage.
CONTROL_MATRIX: Mapping[
    OrderStage, Mapping[ControlKind, ControlAction | None]
] = {
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

#: Stages whose whole purpose is to reduce exposure or remove risk.
RISK_REDUCING_STAGES = frozenset(
    {
        OrderStage.CANCEL_REQUEST,
        OrderStage.EMERGENCY_HEDGE,
        OrderStage.FORCED_LIQUIDATION,
        OrderStage.FILLED_AWAITING_HEDGE,
    }
)


_CAPACITY_CONTROLS = frozenset(
    {ControlKind.VENUE_RATE_LIMIT, ControlKind.GLOBAL_THROTTLE}
)
"""Controls that gate *when* a message may be sent, not *whether*."""


ADVISORY_READS = frozenset({"above_ceiling", "room_for", "_check_ceiling"})
"""Methods whose answer is true when given and may be stale after.

This is a declaration, not a description. It is the trust base for
``tests/test_execution_controls_shape.py``, which enumerates every
place an advisory answer is read and something is then done to the
same object -- the shape behind the priority-ceiling defect, which
appeared at three separate sites and was fixed twice before anyone
found the third.

The invariant these methods carry:

    Every advisory read must be paired either with an authoritative
    call in the same acquisition, or with a handler for that call's
    refusal.

The authoritative siblings are :meth:`MessageBudget.try_reserve_within`
for ``above_ceiling`` and ``_check_ceiling``, and
:meth:`MessageBudget.consume` for ``room_for``. Both re-test under the
lock they take, so a caller that goes through them cannot act on an
answer that has expired.

It is declared rather than inferred because inferring it is what went
wrong: a classifier that decided from the syntax which methods were
pure and which mutated was corrected three times, and each correction
came from a miss rather than from design. One line per method, kept by
hand, states the intent instead of guessing at it.

The test for membership is:

    Can another thread make this return value wrong before the caller
    acts on it?

That is deliberately narrower than "is this a read". :meth:`never_fits`
reads only ``limit``, assigned once in ``__init__``, and its two
arguments; a pure function of immutable state cannot be falsified in
the gap, so there is no window for it to be advisory *in*. It is out
for that reason, not because its call sites happen to be
branch-exclusive today -- branch-exclusivity is a property of the
caller and evaporates in a refactor nobody thinks to check, while
immutability can only change by editing the method, which is where the
question is in front of you.

:meth:`available` does read mutable state and is a genuine advisory
read. It is out only by measurement -- it currently yields no pair the
enumerator does not already have -- and it is the first name to
reconsider if this module grows a new spend path.
"""


CONTROL_PRECEDENCE: tuple[tuple[ControlKind, ControlLevel], ...] = (
    (ControlKind.KILL_SWITCH, ControlLevel.GLOBAL_KILL_SWITCH),
    (ControlKind.STALE_FEED, ControlLevel.FIRM_RISK),
    (ControlKind.GLOBAL_THROTTLE, ControlLevel.FIRM_MESSAGE_LIMIT),
    (ControlKind.GLOBAL_THROTTLE, ControlLevel.PM_LIMIT),
    (ControlKind.GLOBAL_THROTTLE, ControlLevel.STRATEGY_LIMIT),
    (ControlKind.VENUE_RATE_LIMIT, ControlLevel.VENUE_LIMIT),
    (ControlKind.VENUE_RATE_LIMIT, ControlLevel.INSTRUMENT_LIMIT),
)
"""The order controls are evaluated in, and the authority each carries.

The kill switch comes first because the specification makes it absolute:
no other control may override it. Everything after it is in ascending
:class:`ControlLevel` order, so the control that decides an outcome is
always the most senior one that binds -- and the level recorded on the
decision is the level that actually decided it.

A stale feed carries ``FIRM_RISK`` authority rather than a venue- or
instrument-scoped level. It is not a capacity control: it says the firm
cannot price this market at all, which outranks any question of how many
messages are left. Queueing an order that cannot be priced would only
transmit it later, still unpriced.

A control *kind* may appear at more than one level. A throttle is a
throttle whether the budget it exhausts belongs to the firm, a portfolio
manager or a single strategy -- what differs is the authority it carries,
and therefore which one wins when several bind at once. The four scoped
tiers are optional: leaving a tier unconfigured means no budget exists at
that level, so it never binds.

``ControlLevel.ORDER`` is deliberately absent. It is not a capacity tier
that can be exhausted and refilled; it is the per-order ceiling checked
before the walk begins, where an oversized request is a fat finger and is
rejected synchronously rather than queued to wait for room it can never
receive.
"""


class UtilizationBand(str, Enum):
    """Capacity headroom, surfaced long before a control binds.

    Each band is named for its upper bound: green runs to 60% of
    capacity, yellow to 80%, orange to 95%, and red covers the last 5%
    before a control binds outright.
    """

    GREEN = "green"
    YELLOW = "yellow"
    ORANGE = "orange"
    RED = "red"


#: Percentage at which each band is entered.
BAND_THRESHOLDS: tuple[tuple[UtilizationBand, Decimal], ...] = (
    (UtilizationBand.RED, Decimal("95")),
    (UtilizationBand.ORANGE, Decimal("80")),
    (UtilizationBand.YELLOW, Decimal("60")),
    (UtilizationBand.GREEN, Decimal("0")),
)


def band_for(utilization_pct: Decimal) -> UtilizationBand:
    for band, threshold in BAND_THRESHOLDS:
        if utilization_pct >= threshold:
            return band
    return UtilizationBand.GREEN


class ControlError(RuntimeError):
    """Base class for control-plane failures."""


class KillSwitchAuthorizationError(ControlError):
    """Raised when someone without authority touches the kill switch."""


class CapacityReservationError(ControlError):
    """Raised when an atomic reservation cannot be satisfied."""


@dataclass(frozen=True, slots=True)
class ControlDecision:
    """The single, reproducible answer for one message."""

    action: ControlAction
    control: ControlKind | None
    level: ControlLevel | None
    reason: str
    stage: OrderStage
    priority: Priority
    retry_after_ms: int | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def transmits(self) -> bool:
        """Whether a message actually reaches the venue."""

        return self.action in {
            ControlAction.ADMIT,
            ControlAction.CANCEL,
            ControlAction.MASS_CANCEL,
        }

    @property
    def synchronous_rejection(self) -> bool:
        """No message generated and the caller is told immediately."""

        return self.action in {
            ControlAction.REJECT,
            ControlAction.CANCEL_ADMISSION,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "control": None if self.control is None else self.control.value,
            "level": None if self.level is None else self.level.name,
            "reason": self.reason,
            "stage": self.stage.value,
            "priority": self.priority.name,
            "retry_after_ms": self.retry_after_ms,
            "transmits": self.transmits,
            "synchronous_rejection": self.synchronous_rejection,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    """What a dashboard shows before anything breaches."""

    name: str
    level: ControlLevel
    limit: int
    used: int
    reserved: int
    queued: int
    dropped: int
    rejected: int = 0

    @property
    def consumed(self) -> int:
        return self.used + self.reserved

    @property
    def available(self) -> int:
        return max(self.limit - self.consumed, 0)

    @property
    def utilization_pct(self) -> Decimal:
        if self.limit <= 0:
            return Decimal(100)
        return (
            Decimal(self.consumed) / Decimal(self.limit) * Decimal(100)
        ).quantize(Decimal("0.01"))

    @property
    def band(self) -> UtilizationBand:
        return band_for(self.utilization_pct)

    @property
    def binding(self) -> bool:
        return self.available <= 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "level": self.level.name,
            "limit": self.limit,
            "used": self.used,
            "reserved": self.reserved,
            "available": self.available,
            "queued": self.queued,
            "dropped": self.dropped,
            "rejected": self.rejected,
            "utilization_pct": format(self.utilization_pct, "f"),
            "band": self.band.value,
            "binding": self.binding,
        }


class MessageBudget:
    """A refilling allowance of exchange messages per window.

    Deliberately not a leaky bucket: exchanges publish limits as "N
    messages per second", and a caller reasoning about whether it can
    send a five-message opportunity needs to ask about five messages at
    once, not five times about one.
    """

    def __init__(
        self,
        name: str,
        *,
        limit: int,
        window_ms: int = 1000,
        level: ControlLevel = ControlLevel.VENUE_LIMIT,
        now_ns: Callable[[], int] | None = None,
    ) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_ms <= 0:
            raise ValueError("window must be positive")
        self.name = name
        self.limit = limit
        self.window_ns = window_ms * 1_000_000
        self.level = level
        self._now_ns = now_ns or _default_clock
        self._lock = threading.RLock()
        self._used = 0
        self._reserved = 0
        self._window_start_ns = self._now_ns()
        self._queued = 0
        self._dropped = 0
        self._rejected = 0

    # -- window ------------------------------------------------------

    def _roll(self) -> None:
        now = self._now_ns()
        if now - self._window_start_ns >= self.window_ns:
            elapsed = now - self._window_start_ns
            # Jump whole windows so a long pause does not leave the
            # budget replaying history one window at a time.
            self._window_start_ns += (
                elapsed // self.window_ns
            ) * self.window_ns
            self._used = 0

    def available(self) -> int:
        with self._lock:
            self._roll()
            return max(self.limit - self._used - self._reserved, 0)

    def time_to_refill_ms(self) -> int:
        with self._lock:
            self._roll()
            remaining = self.window_ns - (
                self._now_ns() - self._window_start_ns
            )
            return max(int(remaining // 1_000_000), 0)

    # -- reservation -------------------------------------------------

    def try_reserve(self, messages: int) -> bool:
        """Hold capacity without spending it. Never partially succeeds."""

        if messages <= 0:
            return True
        with self._lock:
            self._roll()
            if self.limit - self._used - self._reserved < messages:
                return False
            self._reserved += messages
            return True

    def release(self, messages: int) -> None:
        with self._lock:
            self._reserved = max(self._reserved - messages, 0)

    def try_reserve_within(
        self, ceiling_pct: Decimal, messages: int
    ) -> str:
        """Take capacity only if it stays inside the class's share.

        Returns an empty string when the capacity was taken, otherwise
        the rule that refused: ``"ceiling"`` or ``"capacity"``.

        Testing the ceiling and then reserving are each atomic on their
        own, which is not the same as being atomic together. Two threads
        that both read "still below the ceiling" both go on to reserve,
        and the class ends up past its share -- and the hard limit does
        not catch it, because being over a 50% ceiling is nowhere near
        being over the budget. The overshoot comes out of the headroom
        being held for higher-priority traffic, which is the one thing
        the ceiling exists to protect.

        The lock is re-entrant, so folding both steps into one
        acquisition costs nothing and closes the window entirely.
        """

        with self._lock:
            if self.above_ceiling(ceiling_pct, messages):
                return "ceiling"
            return "" if self.try_reserve(messages) else "capacity"

    def above_ceiling(
        self, ceiling_pct: Decimal, messages: int = 0
    ) -> bool:
        """Whether this class may still draw on the budget.

        The class may draw up to its share and no further: refused once
        utilisation has *reached* the ceiling, and refused again if this
        request would *cross* it. Testing only the first would let a
        single large request overshoot by ``messages - 1``, which is
        unbounded unless a per-order cap is configured -- and a reserve
        that only shapes flows of single messages is not a reserve.

        One concession, and it is deliberately the smallest one that
        works: a *single* message against an *untouched* budget is
        admitted even when it crosses. On a limit of one, one message is
        already 100%, so without this every class below the top would be
        banned outright and never make progress. Widening the concession
        to any first request is what lets a bulk reservation take a
        whole budget in one call, so it stays at one message.

        Read under the budget's own lock, so the utilisation tested is
        a consistent one. It is not necessarily the one a reservation
        will act on: by the time the caller reserves, another thread
        may have taken capacity. Callers that are about to reserve must
        use :meth:`try_reserve_within`, which folds this test and the
        reservation into a single acquisition. Calling this on its own
        is a read, and is only ever advisory.
        """

        if ceiling_pct >= 100 or self.limit <= 0:
            return False
        with self._lock:
            self._roll()
            consumed = Decimal(self._used + self._reserved)
            allowed = Decimal(self.limit) * ceiling_pct / Decimal(100)
            if consumed >= allowed:
                return True
            if consumed + Decimal(messages) <= allowed:
                return False
            return not (consumed == 0 and messages <= 1)

    def never_fits(self, ceiling_pct: Decimal, messages: int) -> bool:
        """Whether no amount of waiting could ever fit this request.

        Two ways to be impossible, and the cheaper one must not be
        gated on the other:

        * larger than the window's whole limit, which needs no ceiling
          arithmetic and applies to *every* class -- including market
          making, whose ceiling is 100%; and
        * larger than this class's share of that limit.

        Either way an empty budget refuses it, so waiting does not help.
        Distinguishing that from ordinary congestion is what stops it
        being queued to die -- the same reason a message whose deadline
        falls before capacity returns is rejected up front.

        Independent of current utilisation, and deliberately so.
        """

        if self.limit <= 0:
            return False
        if messages > self.limit:
            return True
        if ceiling_pct >= 100:
            return False
        if messages <= 1:
            # One message against an untouched budget is always admitted,
            # so waiting for the window to roll does eventually work.
            return False
        allowed = Decimal(self.limit) * ceiling_pct / Decimal(100)
        return Decimal(messages) > allowed

    def consume_reserved(self, messages: int) -> None:
        """Spend capacity previously held by :meth:`try_reserve`."""

        with self._lock:
            self._roll()
            self._reserved = max(self._reserved - messages, 0)
            self._used += messages

    def force_consume(self, messages: int) -> None:
        """Spend capacity even past the limit.

        Used only by exposure-reducing traffic, which is exempt from
        throttling. The overage is still recorded so it is visible.
        """

        with self._lock:
            self._roll()
            self._used += messages

    # -- telemetry ---------------------------------------------------

    def note_queued(self, count: int = 1) -> None:
        with self._lock:
            self._queued += count

    def note_dequeued(self, count: int = 1) -> None:
        with self._lock:
            self._queued = max(self._queued - count, 0)

    def note_dropped(self, count: int = 1) -> None:
        """A queued message expired before capacity returned."""

        with self._lock:
            self._dropped += count
            self._queued = max(self._queued - count, 0)

    def note_rejected(self, count: int = 1) -> None:
        """Refused before being queued, so the queue depth is unchanged."""

        with self._lock:
            self._rejected += count

    def snapshot(self) -> CapacitySnapshot:
        with self._lock:
            self._roll()
            return CapacitySnapshot(
                name=self.name,
                level=self.level,
                limit=self.limit,
                used=self._used,
                reserved=self._reserved,
                queued=self._queued,
                dropped=self._dropped,
                rejected=self._rejected,
            )


def _default_clock() -> int:
    import time

    return time.monotonic_ns()


def _normalise_venue(venue: str) -> str:
    """Venue identifiers reach this module in mixed case.

    ``exchange_adapter`` case-folds venue names and the opportunity state
    machine lower-cases them, so a budget looked up by the raw string
    would miss -- and a missed lookup used to be indistinguishable from
    "no limit configured", silently disabling the control.
    """

    return venue.strip().casefold()


def normalise_scope(scope: str) -> str:
    """Public fold, so callers cannot diverge from this module's own.

    A caller that folds differently -- ``.lower()`` against this
    module's ``strip().casefold()`` -- produces keys this module then
    folds *again*, collapsing two of the caller's keys into one. That
    is how a two-leg opportunity came to reserve one leg's messages.
    """

    return _normalise_venue(scope)


def _stamp_level(
    budgets: dict[str, MessageBudget], level: ControlLevel
) -> dict[str, MessageBudget]:
    """Label each budget with the tier it actually enforces.

    ``MessageBudget`` defaults to ``VENUE_LIMIT``, so an unstamped
    strategy budget would show up on the operator dashboard as a venue
    limit -- naming the wrong control as the one that is binding.
    """

    for budget in budgets.values():
        budget.level = level
    return budgets


def _normalise_budgets(
    budgets: Mapping[str, MessageBudget],
) -> dict[str, MessageBudget]:
    normalised: dict[str, MessageBudget] = {}
    for venue, budget in budgets.items():
        key = _normalise_venue(venue)
        if key in normalised:
            raise ValueError(f"duplicate message budget for venue {key!r}")
        normalised[key] = budget
    return normalised


_T = TypeVar("_T")


def _fold_keys(
    mapping: Mapping[str, _T],
    subject: str,
    error: type[Exception] = ValueError,
) -> dict[str, _T]:
    """Fold a mapping's keys, refusing a collision.

    Folding silently is what let one call frame hold two identities for
    one quantity: ``reserve`` reserved the firm tier from the caller's
    raw map and ``release`` credited it back from the folded one, so
    every dropped duplicate leaked firm capacity permanently -- on the
    *success* path, with no error anywhere.
    """

    folded: dict[str, _T] = {}
    for key, value in mapping.items():
        name = _normalise_venue(key)
        if name in folded:
            raise error(f"duplicate {subject} for {name!r}")
        folded[name] = value
    return folded


def _normalise_routes(routes: Mapping[str, str]) -> dict[str, str]:
    """Fold a strategy-to-manager routing table.

    Folding without a duplicate check would be worse here than for a
    budget map. Two spellings of one strategy do not merely pick one of
    two equivalent budget objects -- they pick one of two *different
    managers*, silently discarding the routing the operator wrote and
    charging the strategy against an allowance they never assigned it.
    """

    folded: dict[str, str] = {}
    for strategy, manager in routes.items():
        key = _normalise_venue(strategy)
        if key in folded:
            raise ValueError(f"duplicate manager routing for {key!r}")
        folded[key] = _normalise_venue(manager)
    return folded


def _normalise_scopes(
    budgets: Mapping[str, MessageBudget],
) -> dict[str, MessageBudget]:
    """PM and strategy tiers are looked up by identifier, like venues.

    Leaving them keyed on the raw string makes them case-evadable: a
    lookup that misses is indistinguishable from "no tier configured",
    which ``_binds`` treats as "does not apply". A strategy could then
    exceed its own allowance indefinitely by varying the case of its id.
    The asymmetry with venues is what makes it dangerous -- an operator
    who has seen venue normalisation work will assume it holds here.
    """

    normalised: dict[str, MessageBudget] = {}
    for scope, budget in budgets.items():
        key = _normalise_venue(scope)
        if key in normalised:
            raise ValueError(f"duplicate message budget for scope {key!r}")
        normalised[key] = budget
    return normalised


_GLOBAL_BUDGET_KEY = "\x00firm"
_PM_KEY_PREFIX = "\x00pm:"
_STRATEGY_KEY_PREFIX = "\x00strategy:"
_INSTRUMENT_KEY_PREFIX = "\x00instrument:"
"""Reserved key for the firm budget.

Not a legal venue identifier, so a venue can never collide with it no
matter what the operator names its budgets.
"""

_HEDGE_KEY = "\x00hedge"

_SHARED_LEVELS = frozenset(
    {
        ControlLevel.FIRM_MESSAGE_LIMIT,
        ControlLevel.PM_LIMIT,
        ControlLevel.STRATEGY_LIMIT,
    }
)
"""Tiers whose budget is shared by messages for other venues.

Scanning past one of these when it refuses would give its last slot to
whatever queued message happens to be small enough -- which is a
priority inversion, not a scoping decision.
"""
"""Reserved key for the hedge allowance inside a reservation's spend map.

Hedge capacity has no venue at reserve time, so it needs a home that no
venue can occupy.
"""


class ReleaseVerdict(Enum):
    """What a drain should do with the queued message it is looking at."""

    TAKE = "take"
    SKIP = "skip"
    """This message's own venue is saturated; others may still go."""
    STOP = "stop"
    """A shared budget is exhausted.

    Nothing behind this that draws on the *same* budget can go either, so
    the scan must not hand its last slot to something smaller and lower
    priority. Reported with the exhausted budget's key: an empty key
    means firm-wide and ends the scan, while a scoped key blocks only
    the messages that share it. Aborting the whole scan for one busy
    strategy would stall every other strategy in the queue.
    """


@dataclass(frozen=True, slots=True)
class QueuedMessage:
    """A message accepted internally but not yet transmitted."""

    message_id: str
    strategy_id: str
    venue: str
    stage: OrderStage
    priority: Priority
    messages: int
    enqueued_ns: int
    expires_ns: int
    budget_key: str = ""
    """Which budget this message was queued against, by *key*.

    Queue and drop counters must be settled on the same budget that
    recorded them, or a firm-throttle queue would be cleared off a venue
    budget and the firm counter would grow forever. The key is used
    rather than the display name because two venues may share a name.
    """
    instrument: str = ""
    """Scopes the instrument tier when the message drains."""

    def __post_init__(self) -> None:
        # ``pop_if`` runs the release callback against every message it
        # scans, so one malformed entry would raise on each drain and
        # never be removed -- taking everything queued behind it down
        # with it. Refuse it at the door instead.
        if self.messages <= 0:
            raise ValueError("a queued message must carry at least 1 message")

    def expired(self, now_ns: int) -> bool:
        return now_ns >= self.expires_ns


class PriorityMessageQueue:
    """Capacity is allocated by priority, then by arrival.

    A queue that served first-in-first-out would let a research backfill
    delay a hedge, which is exactly backwards.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._queues: dict[Priority, deque[QueuedMessage]] = {
            priority: deque() for priority in Priority
        }

    def __len__(self) -> int:
        with self._lock:
            return sum(len(queue) for queue in self._queues.values())

    def push(self, message: QueuedMessage) -> None:
        with self._lock:
            self._queues[message.priority].append(message)

    def peek(self) -> QueuedMessage | None:
        with self._lock:
            for priority in Priority:
                queue = self._queues[priority]
                if queue:
                    return queue[0]
            return None

    def pop(self) -> QueuedMessage | None:
        with self._lock:
            for priority in Priority:
                queue = self._queues[priority]
                if queue:
                    return queue.popleft()
            return None

    def pop_if(
        self,
        admits: Callable[
            [QueuedMessage, frozenset[str]], tuple["ReleaseVerdict", str]
        ],
    ) -> QueuedMessage | None:
        """Remove the first message ``admits`` accepts, atomically.

        Peeking and then popping as two operations lets two concurrent
        drains report the same message while removing two different ones,
        which at an exchange is a duplicate order.

        ``admits`` returns a :class:`ReleaseVerdict` and the key of the
        budget that refused. ``SKIP`` scans past a message whose own
        venue is saturated, so a single busy venue cannot stall the whole
        firm. ``STOP`` with an empty key ends the scan outright, because
        the firm budget is shared by everything queued. ``STOP`` with a
        key instead blocks that one scope and keeps scanning: a strategy
        budget is shared only by that strategy's messages, so aborting on
        its behalf would idle every other strategy -- the same
        head-of-line stall ``SKIP`` exists to prevent, one scope up.

        The accumulated keys are handed back to ``admits`` so it can
        refuse a message in an already-blocked scope without touching a
        budget.
        """

        blocked: set[str] = set()
        with self._lock:
            for priority in Priority:
                queue = self._queues[priority]
                for index, message in enumerate(queue):
                    verdict, scope = admits(message, frozenset(blocked))
                    if verdict is ReleaseVerdict.TAKE:
                        del queue[index]
                        return message
                    if verdict is ReleaseVerdict.STOP:
                        if not scope:
                            return None
                        blocked.add(scope)
            return None

    def expire(self, now_ns: int) -> tuple[QueuedMessage, ...]:
        """Drop everything past its deadline. Returns what was dropped."""

        dropped: list[QueuedMessage] = []
        with self._lock:
            for priority in Priority:
                queue = self._queues[priority]
                kept = deque(
                    message
                    for message in queue
                    if not message.expired(now_ns)
                )
                dropped.extend(
                    message for message in queue if message.expired(now_ns)
                )
                self._queues[priority] = kept
        return tuple(dropped)

    def depth_by_priority(self) -> dict[str, int]:
        with self._lock:
            return {
                priority.name: len(queue)
                for priority, queue in self._queues.items()
                if queue
            }

    def strategies(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                sorted(
                    {
                        message.strategy_id
                        for queue in self._queues.values()
                        for message in queue
                    }
                )
            )


class KillSwitch:
    """Absolute, and only a designated administrator may clear it.

    Engaging is deliberately unrestricted: anyone who believes the firm
    is in trouble must be able to stop it. Clearing is restricted,
    because that is the decision that puts capital back at risk.
    """

    def __init__(
        self, administrators: Iterable[str] = ("risk-admin",)
    ) -> None:
        self._administrators = frozenset(administrators)
        if not self._administrators:
            raise ValueError(
                "a kill switch with no administrators can never be "
                "cleared; name at least one risk administrator"
            )
        self._lock = threading.RLock()
        self._active = False
        self._reason: str | None = None
        self._engaged_by: str | None = None
        self._engaged_ns: int | None = None

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def engage(
        self,
        *,
        actor: str,
        reason: str,
        timestamp_ns: int | None = None,
    ) -> None:
        with self._lock:
            if self._active:
                return
            self._active = True
            self._reason = reason
            self._engaged_by = actor
            self._engaged_ns = timestamp_ns
        logger.warning(
            "kill_switch_engaged",
            actor=actor,
            reason=reason,
        )

    def clear(self, *, actor: str) -> None:
        with self._lock:
            if actor not in self._administrators:
                raise KillSwitchAuthorizationError(
                    f"{actor} is not a risk administrator and cannot "
                    "clear the kill switch"
                )
            self._active = False
            self._reason = None
            self._engaged_by = None
            self._engaged_ns = None
        logger.warning("kill_switch_cleared", actor=actor)

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._active,
                "reason": self._reason,
                "engaged_by": self._engaged_by,
                "engaged_ns": self._engaged_ns,
                "administrators": sorted(self._administrators),
            }


class FeedMonitor:
    """Tracks per-venue market data age against a staleness threshold."""

    def __init__(
        self,
        *,
        stale_after_ms: int = 100,
        now_ns: Callable[[], int] | None = None,
    ) -> None:
        if stale_after_ms <= 0:
            raise ValueError("stale threshold must be positive")
        self.stale_after_ns = stale_after_ms * 1_000_000
        self._now_ns = now_ns or _default_clock
        self._lock = threading.RLock()
        self._last_tick_ns: dict[str, int] = {}
        self._clamped: dict[str, int] = {}

    def record_tick(self, venue: str, timestamp_ns: int | None = None) -> None:
        """Record a tick, never dated ahead of the local clock.

        A caller feeding exchange tick times is the normal case, and a
        venue clock running fast would otherwise make `age_ns` negative
        -- and a negative age never exceeds the stale threshold, so the
        control silently switches off for that venue and admits new
        quoting into a dead feed. `is_stale` already treats the unknown
        case as stale rather than fresh; this is the same choice for the
        one input that failed in the unsafe direction.
        """

        now = self._now_ns()
        key = _normalise_venue(venue)
        with self._lock:
            if timestamp_ns is None:
                self._last_tick_ns[key] = now
                return
            # Clamping only ever moves a stored timestamp *earlier*, so
            # age can be over-reported but never under-reported: it can
            # neither make a stale feed look fresh nor a fresh one look
            # stale. What it does destroy is the evidence -- a venue an
            # hour ahead now renders identically to a healthy one. The
            # count keeps that visible without touching any decision.
            if timestamp_ns > now:
                self._clamped[key] = self._clamped.get(key, 0) + 1
            self._last_tick_ns[key] = min(timestamp_ns, now)

    def age_ns(self, venue: str) -> int | None:
        with self._lock:
            last = self._last_tick_ns.get(_normalise_venue(venue))
        return None if last is None else self._now_ns() - last

    def is_stale(self, venue: str) -> bool:
        """A venue we have never heard from is stale, not fresh."""

        age = self.age_ns(venue)
        return True if age is None else age >= self.stale_after_ns

    def clamped(self, venue: str) -> int:
        """How many ticks this venue dated ahead of the local clock.

        A non-zero count means that venue's timestamps are unusable for
        measuring anything, not merely that one arrived early.
        """

        with self._lock:
            return self._clamped.get(_normalise_venue(venue), 0)

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            venues = sorted(self._last_tick_ns)
        return {
            "stale_after_ms": self.stale_after_ns // 1_000_000,
            "venues": {
                venue: {
                    "age_ms": (
                        None
                        if (age := self.age_ns(venue)) is None
                        else age // 1_000_000
                    ),
                    "stale": self.is_stale(venue),
                    "clamped_ticks": self.clamped(venue),
                }
                for venue in venues
            },
        }


@dataclass(frozen=True, slots=True)
class ReservationRequest:
    """Everything a multi-leg opportunity needs, asked for at once."""

    opportunity_id: str
    strategy_id: str
    venue_messages: Mapping[str, int]
    instrument: str = ""
    """The opportunity-level instrument, if a tier is configured.

    Used for any venue not named in ``venue_instruments`` and for the
    hedge allowance, whose venue is only chosen at unwind time.
    """
    venue_instruments: Mapping[str, str] = field(default_factory=dict)
    """Per-leg instrument overrides, keyed by venue.

    A spread is two instruments, not one. Reserving both legs under a
    single instrument would leave the second leg's tier unreserved --
    and therefore unenforced, because a reserved message skips the
    capacity walk.
    """
    notional: Decimal = Decimal(0)
    margin: Decimal = Decimal(0)
    position_quantity: Decimal = Decimal(0)
    hedge_messages: int = 0
    priority: Priority = Priority.MARKET_MAKING

    def __post_init__(self) -> None:
        # Note: this class deliberately has no `total_messages`. The
        # total must be computed from the *folded* venue map inside
        # `reserve`, because that is the map `release` credits back
        # from; a helper that summed the raw map is exactly how the two
        # sides came to disagree and leak firm capacity permanently.
        # Every one of these is a way to *credit* a budget rather than
        # draw on it. A negative message count makes `try_reserve`
        # succeed holding nothing and `release` grow the hold it should
        # shrink, which survives every window roll and ends with the
        # firm tier -- the top of the hierarchy -- refusing everything
        # while holding zero reservations. A negative notional walks
        # straight through FIRM_RISK. No control may be overridable by
        # the request that is subject to it.
        for venue, messages in self.venue_messages.items():
            # Zero is legitimate and load-bearing: it names a venue that
            # takes part in this opportunity's budget structure without
            # sending on this pass. Only a negative credits a budget.
            if messages < 0:
                raise ValueError(
                    f"venue_messages[{venue!r}] must not be negative"
                )
        if self.hedge_messages < 0:
            raise ValueError("hedge_messages must not be negative")
        for name in ("notional", "margin"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")
        # `position_quantity` is deliberately absent: its sign carries
        # direction, and a short leg is a negative. It is safe because
        # the check, the charge and the release all take `abs` of it,
        # so no sign can make the three disagree.


@dataclass(frozen=True, slots=True)
class Reservation:
    """A granted, releasable hold across every resource."""

    opportunity_id: str
    strategy_id: str
    venue_messages: Mapping[str, int]
    hedge_messages: int
    notional: Decimal
    margin: Decimal
    position_quantity: Decimal
    instrument: str = ""
    venue_instruments: Mapping[str, str] = field(default_factory=dict)
    scoped_charges: tuple[tuple[str, tuple[str, ...]], ...] = ()
    """Scoped tiers held, each with the legs it was charged for.

    Recorded so a spend or a release settles the same tiers the
    reservation took. A message backed by a reservation skips the
    capacity walk entirely, so a tier that was not reserved would be a
    tier the opportunity never pays. Sources are normalised venue names
    plus the hedge sentinel, because an instrument tier is charged per
    leg while the PM and strategy tiers are charged for the whole
    opportunity.
    """

    def instrument_for(self, venue: str) -> str:
        """The normalised instrument this leg trades."""

        key = _normalise_venue(venue)
        return _normalise_venue(
            self.venue_instruments.get(key, self.instrument)
        )

    @property
    def total_messages(self) -> int:
        return sum(self.venue_messages.values()) + self.hedge_messages

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "strategy_id": self.strategy_id,
            "venue_messages": dict(self.venue_messages),
            "hedge_messages": self.hedge_messages,
            "notional": format(self.notional, "f"),
            "margin": format(self.margin, "f"),
            "position_quantity": format(self.position_quantity, "f"),
        }


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Firm-level ceilings the reservation ledger enforces."""

    max_notional: Decimal = Decimal("100000000")
    max_margin: Decimal = Decimal("100000000")
    max_position_quantity: Decimal = Decimal("1000000")


class ReservationLedger:
    """All-or-nothing reservation across every resource an order needs.

    A multi-leg opportunity that reserved resources one at a time could
    send its first leg and then discover the second has no message budget
    left, leaving a naked position nobody asked for. So every resource is
    checked and taken under one lock, and any failure releases whatever
    was already taken.
    """

    def __init__(
        self,
        *,
        venue_budgets: Mapping[str, MessageBudget],
        global_budget: MessageBudget,
        pm_budgets: Mapping[str, MessageBudget] | None = None,
        strategy_pm: Mapping[str, str] | None = None,
        strategy_budgets: Mapping[str, MessageBudget] | None = None,
        instrument_budgets: Mapping[str, MessageBudget] | None = None,
        priority_ceilings: Mapping[Priority, Decimal] | None = None,
        limits: RiskLimits | None = None,
    ) -> None:
        self.venue_budgets = _stamp_level(
            _normalise_budgets(venue_budgets), ControlLevel.VENUE_LIMIT
        )
        self.global_budget = global_budget
        self.global_budget.level = ControlLevel.FIRM_MESSAGE_LIMIT
        self.pm_budgets = _stamp_level(
            _normalise_scopes(pm_budgets or {}), ControlLevel.PM_LIMIT
        )
        self.strategy_pm = _normalise_routes(strategy_pm or {})
        self.strategy_budgets = _stamp_level(
            _normalise_scopes(strategy_budgets or {}),
            ControlLevel.STRATEGY_LIMIT,
        )
        self.instrument_budgets = _stamp_level(
            _normalise_budgets(instrument_budgets or {}),
            ControlLevel.INSTRUMENT_LIMIT,
        )
        self.priority_ceilings = dict(
            PRIORITY_CEILING_PCT
            if priority_ceilings is None
            else priority_ceilings
        )
        self.limits = limits or RiskLimits()
        self._lock = threading.RLock()
        self._reservations: dict[str, Reservation] = {}
        self._consumed: dict[str, dict[str, int]] = {}
        self._notional = Decimal(0)
        self._margin = Decimal(0)
        self._position = Decimal(0)

    # -- introspection -----------------------------------------------

    @property
    def reserved_notional(self) -> Decimal:
        with self._lock:
            return self._notional

    @property
    def reserved_margin(self) -> Decimal:
        with self._lock:
            return self._margin

    @property
    def reserved_position(self) -> Decimal:
        with self._lock:
            return self._position

    def get(self, opportunity_id: str) -> Reservation | None:
        with self._lock:
            return self._reservations.get(opportunity_id)

    # -- the atomic operation ----------------------------------------

    def _scoped(
        self,
        strategy_id: str,
        instrument: str,
        venue_instruments: Mapping[str, str],
        venues: Iterable[str],
        hedge: bool,
    ) -> tuple[tuple[str, MessageBudget, tuple[str, ...]], ...]:
        """The PM, strategy and instrument tiers this request must hold.

        Reserving only firm and venue would leave the scoped tiers unpaid
        while the plane skips them for anything holding a reservation,
        so a single opportunity could send straight through its own
        strategy limit.

        Each tier carries the legs it is charged for. The PM and strategy
        tiers cover every leg; an instrument tier covers only the legs
        that trade it, so a two-instrument spread holds two of them.
        """

        legs = tuple(sorted(_normalise_venue(venue) for venue in venues))
        whole = legs + ((_HEDGE_KEY,) if hedge else ())
        scoped: list[tuple[str, MessageBudget, tuple[str, ...]]] = []
        strategy_id = _normalise_venue(strategy_id)
        manager = self.strategy_pm.get(strategy_id)
        if manager is not None:
            budget = self.pm_budgets.get(manager)
            if budget is not None:
                scoped.append((_PM_KEY_PREFIX + manager, budget, whole))
        strategy = self.strategy_budgets.get(strategy_id)
        if strategy is not None:
            scoped.append(
                (_STRATEGY_KEY_PREFIX + strategy_id, strategy, whole)
            )

        per_leg: dict[str, list[str]] = {}
        overrides = dict(venue_instruments)
        for venue in legs:
            name = _normalise_venue(overrides.get(venue, instrument))
            if name:
                per_leg.setdefault(name, []).append(venue)
        # The hedge venue is only chosen at unwind time, so its
        # instrument can only be the opportunity-level one.
        if hedge and instrument:
            per_leg.setdefault(_normalise_venue(instrument), []).append(
                _HEDGE_KEY
            )
        for name, sources in sorted(per_leg.items()):
            budget = self.instrument_budgets.get(name)
            if budget is not None:
                scoped.append(
                    (_INSTRUMENT_KEY_PREFIX + name, budget, tuple(sources))
                )
        return tuple(scoped)

    def _scoped_budget(self, key: str) -> MessageBudget | None:
        if key.startswith(_PM_KEY_PREFIX):
            return self.pm_budgets.get(key[len(_PM_KEY_PREFIX):])
        if key.startswith(_STRATEGY_KEY_PREFIX):
            return self.strategy_budgets.get(key[len(_STRATEGY_KEY_PREFIX):])
        if key.startswith(_INSTRUMENT_KEY_PREFIX):
            return self.instrument_budgets.get(
                key[len(_INSTRUMENT_KEY_PREFIX):]
            )
        return None

    def reserve(self, request: ReservationRequest) -> Reservation:
        """Take everything or nothing. Idempotent per opportunity."""

        with self._lock:
            existing = self._reservations.get(request.opportunity_id)
            if existing is not None:
                return existing

            self._check_risk(request)

            venue_messages = _fold_keys(
                request.venue_messages,
                "venue in venue_messages",
                CapacityReservationError,
            )
            venue_instruments = _fold_keys(
                request.venue_instruments,
                "venue in venue_instruments",
                CapacityReservationError,
            )
            unknown = sorted(set(venue_messages) - set(self.venue_budgets))
            if unknown:
                raise CapacityReservationError(
                    f"no message budget configured for {', '.join(unknown)}"
                )

            ceiling = self._ceiling_for(request.priority)
            scoped = self._scoped(
                request.strategy_id,
                request.instrument,
                venue_instruments,
                venue_messages,
                request.hedge_messages > 0,
            )
            planned = dict(venue_messages)
            planned[_HEDGE_KEY] = request.hedge_messages
            self._preflight(request, ceiling, venue_messages, scoped, planned)

            taken: list[tuple[MessageBudget, int]] = []
            try:
                for venue, messages in sorted(venue_messages.items()):
                    budget = self.venue_budgets[venue]
                    self._take(
                        budget, ceiling, messages, request.priority, venue
                    )
                    taken.append((budget, messages))

                # The hedge budget is reserved up front precisely so a
                # hedge can never be the thing that cannot be sent.
                total = sum(venue_messages.values())
                total += request.hedge_messages
                self._take(
                    self.global_budget, ceiling, total, request.priority
                )
                taken.append((self.global_budget, total))

                for _key, budget, sources in scoped:
                    share = sum(planned[source] for source in sources)
                    self._take(
                        budget, ceiling, share, request.priority
                    )
                    taken.append((budget, share))
            except CapacityReservationError:
                for budget, messages in taken:
                    budget.release(messages)
                raise

            reservation = Reservation(
                opportunity_id=request.opportunity_id,
                strategy_id=_normalise_venue(request.strategy_id),
                venue_messages=venue_messages,
                hedge_messages=request.hedge_messages,
                notional=request.notional,
                margin=request.margin,
                position_quantity=request.position_quantity,
                instrument=_normalise_venue(request.instrument),
                venue_instruments={
                    venue: _normalise_venue(name)
                    for venue, name in venue_instruments.items()
                },
                scoped_charges=tuple(
                    (key, sources) for key, _budget, sources in scoped
                ),
            )
            self._reservations[request.opportunity_id] = reservation
            self._consumed[request.opportunity_id] = {
                venue: 0 for venue in venue_messages
            }
            self._consumed[request.opportunity_id][_HEDGE_KEY] = 0
            self._notional += request.notional
            self._margin += request.margin
            self._position += abs(request.position_quantity)
            return reservation

    def _ceiling_for(self, priority: Priority) -> Decimal:
        if priority in EXEMPT_PRIORITIES:
            return Decimal(100)
        return self.priority_ceilings.get(priority, Decimal(100))

    def _preflight(
        self,
        request: ReservationRequest,
        ceiling: Decimal,
        venue_messages: Mapping[str, int],
        scoped: tuple[tuple[str, MessageBudget, tuple[str, ...]], ...],
        planned: Mapping[str, int],
    ) -> None:
        """Refuse naming the most senior tier, not the first one walked.

        The module publishes ``CONTROL_PRECEDENCE`` and promises the most
        senior binding control is the one reported. Taking the tiers in
        whatever order is convenient means a caller whose firm budget is
        exhausted is told about a venue, and retries against the wrong
        constraint. Rollback makes the *take* order immaterial, so the
        ordering belongs in a check that runs before anything is taken.
        """

        shares = {
            key: sum(planned[source] for source in sources)
            for key, _budget, sources in scoped
        }
        by_key = {key: budget for key, budget, _sources in scoped}

        def group(instrument: bool) -> list[tuple[MessageBudget, int]]:
            return [
                (by_key[key], shares[key])
                for key in shares
                if key.startswith(_INSTRUMENT_KEY_PREFIX) is instrument
            ]

        firm_demand = sum(venue_messages.values()) + request.hedge_messages
        subordinate = max(
            (*venue_messages.values(), *shares.values(), 0)
        )
        if firm_demand < subordinate:
            # The hierarchy's invariant, asserted rather than assumed.
            # Every subordinate tier's demand is a subset of the firm
            # tier's, so a firm hold smaller than one of them means the
            # top of `CONTROL_PRECEDENCE` overstates its availability
            # for the life of the reservation and admits an opportunity
            # it should have refused. The two quantities agree today
            # only because the same map feeds both -- an arithmetic
            # coincidence, not a checked property.
            raise CapacityReservationError(
                f"firm demand {firm_demand} is below the "
                f"{subordinate} a subordinate tier requires"
            )

        tiers: list[tuple[MessageBudget, int]] = [
            (self.global_budget, firm_demand)
        ]
        tiers.extend(group(instrument=False))
        tiers.extend(
            (self.venue_budgets[venue], messages)
            for venue, messages in sorted(venue_messages.items())
        )
        tiers.extend(group(instrument=True))

        for budget, messages in tiers:
            self._check_ceiling(budget, ceiling, messages, request.priority)
            available = budget.available()
            if available < messages:
                raise CapacityReservationError(
                    f"{budget.name} has {available} of "
                    f"{messages} messages available"
                )

    def _check_ceiling(
        self,
        budget: MessageBudget,
        ceiling: Decimal,
        messages: int,
        priority: Priority,
    ) -> None:
        """Reserving must respect the class ceiling too.

        A reserved message draws down instead of walking the capacity
        controls, so a ceiling enforced only at admission is a ceiling
        any caller can evade by reserving first -- and a reservation is
        the one call that can take a whole budget at once.
        """

        if budget.above_ceiling(ceiling, messages):
            raise CapacityReservationError(
                f"{messages} messages on {budget.name} would take "
                f"{priority.name.lower()} past its "
                f"{format(ceiling.normalize(), 'f')}% ceiling"
            )

    def _take(
        self,
        budget: MessageBudget,
        ceiling: Decimal,
        messages: int,
        priority: Priority,
        label: str | None = None,
    ) -> None:
        """Take capacity for a reservation: ceiling and limit together.

        Testing the ceiling and then reserving are each atomic on their
        own, which is not the same as being atomic together. The ledger
        lock is no help here, because the party racing a reservation is
        ``ExecutionControlPlane.evaluate``, which never takes it -- and
        both are spending the *same* budget objects.

        That makes this the worst of the three places the pair appears,
        for the reason :meth:`_check_ceiling` already gives: a
        reservation is the one call that can take a whole budget at
        once, so a ceiling it can straddle is a ceiling any caller can
        evade by reserving first.
        """

        refusal = budget.try_reserve_within(ceiling, messages)
        if refusal == "ceiling":
            raise CapacityReservationError(
                f"{messages} messages on {budget.name} would take "
                f"{priority.name.lower()} past its "
                f"{format(ceiling.normalize(), 'f')}% ceiling"
            )
        if refusal:
            raise CapacityReservationError(
                f"{label or budget.name} has {budget.available()} of "
                f"{messages} messages available"
            )

    def _check_risk(self, request: ReservationRequest) -> None:
        if self._notional + request.notional > self.limits.max_notional:
            raise CapacityReservationError(
                f"notional {format(request.notional, 'f')} exceeds the "
                f"firm limit with {format(self._notional, 'f')} reserved"
            )
        if self._margin + request.margin > self.limits.max_margin:
            raise CapacityReservationError(
                f"margin {format(request.margin, 'f')} exceeds the firm "
                f"limit with {format(self._margin, 'f')} reserved"
            )
        projected = self._position + abs(request.position_quantity)
        if projected > self.limits.max_position_quantity:
            raise CapacityReservationError(
                f"position {format(projected, 'f')} exceeds the firm "
                f"limit of {format(self.limits.max_position_quantity, 'f')}"
            )

    def consume(
        self, opportunity_id: str, venue: str, messages: int = 1
    ) -> None:
        """Spend part of a held reservation on an actual transmission.

        What is spent must be remembered, or the later release would hand
        back capacity that has already been sent to the exchange -- and
        that surplus is then handed to somebody else, who spends it too.
        """

        if messages <= 0:
            # `consume_reserved` does `_reserved -= n; _used += n`, so a
            # negative both inflates the hold and drives `used` below
            # zero, and `available()` then exceeds the exchange's hard
            # limit. `room_for` and `consume_hedge` both guard this.
            raise CapacityReservationError("spend must be positive")
        with self._lock:
            reservation = self._reservations.get(opportunity_id)
            if reservation is None:
                raise CapacityReservationError(
                    f"{opportunity_id} holds no reservation"
                )
            key = _normalise_venue(venue)
            if key not in reservation.venue_messages:
                raise CapacityReservationError(
                    f"{opportunity_id} reserved no capacity on {venue}"
                )
            spent = self._consumed[opportunity_id]
            remaining = reservation.venue_messages[key] - spent[key]
            if messages > remaining:
                raise CapacityReservationError(
                    f"{opportunity_id} has {remaining} of {messages} "
                    f"reserved messages left on {venue}"
                )

            self.venue_budgets[key].consume_reserved(messages)
            self.global_budget.consume_reserved(messages)
            self._settle_scoped(reservation, key, messages)
            spent[key] += messages

    def _settle_scoped(
        self, reservation: Reservation, source: str, messages: int
    ) -> None:
        """Draw a leg's spend off every tier that reserved for it."""

        for scoped_key, sources in reservation.scoped_charges:
            if source not in sources:
                continue
            budget = self._scoped_budget(scoped_key)
            if budget is not None:
                budget.consume_reserved(messages)

    def room_for(
        self,
        opportunity_id: str,
        venue: str,
        messages: int = 1,
        *,
        strategy_id: str | None = None,
        instrument: str | None = None,
        hedge_eligible: bool = False,
    ) -> str | None:
        """Which allowance can cover this send, without spending it.

        Returns ``"venue"``, ``"hedge"`` or ``None``. Used by the control
        plane so a message backed by a reservation is not charged twice:
        once when the opportunity reserved it, and again when it is sent.

        A reservation is an authorisation, not a bearer token. It is
        honoured only for the strategy and instrument it was granted to,
        because a message that skips the capacity walk skips *its own*
        PM, strategy and instrument tiers -- and would be charged to
        whichever ones the reservation happens to hold instead.

        The hedge allowance is offered only to risk-reducing traffic.
        It is spent with an unchecked venue charge so that nothing can
        block a hedge, so handing it to ordinary traffic would let an
        opportunity walk straight through a venue's hard limit.

        Advisory, deliberately. The answer can be stale before the
        caller acts on it, but the authoritative check is in
        :meth:`consume`, which re-tests the same allowance under the
        lock that records the spend and raises rather than overspend.
        Nothing is transmitted on the strength of this answer alone.
        """

        if messages <= 0:
            return None
        with self._lock:
            reservation = self._reservations.get(opportunity_id)
            if reservation is None:
                return None
            if (
                strategy_id is not None
                and _normalise_venue(strategy_id)
                != reservation.strategy_id
            ):
                return None
            key = _normalise_venue(venue)
            if instrument:
                declared = _normalise_venue(instrument)
                if declared != reservation.instrument_for(venue):
                    return None
            spent = self._consumed[opportunity_id]
            planned = reservation.venue_messages.get(key)
            if planned is not None and planned - spent[key] >= messages:
                return "venue"
            if (
                hedge_eligible
                and reservation.hedge_messages - spent[_HEDGE_KEY] >= messages
            ):
                return "hedge"
            return None

    def consume_hedge(
        self, opportunity_id: str, venue: str, messages: int = 1
    ) -> None:
        """Spend part of the reserved hedge allowance.

        Hedge capacity is reserved firm-wide but deliberately not against
        any one venue: which venue an unwind goes to is chosen
        deterministically at unwind time, not when the opportunity is
        admitted. So the firm draw is a reserved consume, while the venue
        is charged fresh -- and charged unconditionally, because a hedge
        must never be blocked by a venue allowance.
        """

        if messages <= 0:
            raise CapacityReservationError("hedge spend must be positive")
        with self._lock:
            reservation = self._reservations.get(opportunity_id)
            if reservation is None:
                raise CapacityReservationError(
                    f"{opportunity_id} holds no reservation"
                )
            spent = self._consumed[opportunity_id]
            remaining = reservation.hedge_messages - spent[_HEDGE_KEY]
            if messages > remaining:
                raise CapacityReservationError(
                    f"{opportunity_id} has {remaining} of {messages} "
                    f"reserved hedge messages left"
                )
            self.global_budget.consume_reserved(messages)
            self._settle_scoped(reservation, _HEDGE_KEY, messages)
            budget = self.venue_budgets.get(_normalise_venue(venue))
            if budget is not None:
                budget.force_consume(messages)
            spent[_HEDGE_KEY] += messages

    def release(self, opportunity_id: str) -> None:
        """Give back whatever is left. Safe to call more than once."""

        with self._lock:
            reservation = self._reservations.pop(opportunity_id, None)
            if reservation is None:
                return
            spent = self._consumed.pop(opportunity_id, {})
            for venue, messages in reservation.venue_messages.items():
                unspent = messages - spent.get(venue, 0)
                if unspent > 0:
                    self.venue_budgets[venue].release(unspent)
            unspent_total = reservation.total_messages - sum(spent.values())
            self.global_budget.release(unspent_total)
            planned = dict(reservation.venue_messages)
            planned[_HEDGE_KEY] = reservation.hedge_messages
            for scoped_key, sources in reservation.scoped_charges:
                budget = self._scoped_budget(scoped_key)
                if budget is None:
                    continue
                # Only this tier's own legs: a two-instrument spread
                # holds two instrument tiers, and handing each of them
                # back the whole opportunity's remainder would credit
                # capacity that was never taken.
                unspent = sum(
                    planned[source] - spent.get(source, 0)
                    for source in sources
                )
                if unspent > 0:
                    budget.release(unspent)
            self._notional -= reservation.notional
            self._margin -= reservation.margin
            self._position -= abs(reservation.position_quantity)
            if self._notional < 0:
                self._notional = Decimal(0)
            if self._margin < 0:
                self._margin = Decimal(0)
            if self._position < 0:
                self._position = Decimal(0)

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "open_reservations": len(self._reservations),
                "reserved_notional": format(self._notional, "f"),
                "reserved_margin": format(self._margin, "f"),
                "reserved_position": format(self._position, "f"),
                "limits": {
                    "max_notional": format(self.limits.max_notional, "f"),
                    "max_margin": format(self.limits.max_margin, "f"),
                    "max_position_quantity": format(
                        self.limits.max_position_quantity, "f"
                    ),
                },
            }


@dataclass(frozen=True, slots=True)
class MessageRequest:
    """One prospective message, evaluated against every control."""

    message_id: str
    strategy_id: str
    venue: str
    stage: OrderStage
    priority: Priority = Priority.MARKET_MAKING
    messages: int = 1
    opportunity_id: str | None = None
    instrument: str = ""
    """Scopes the instrument tier. Empty means the tier does not apply."""
    ttl_ms: int = 250
    delay_reason: str | None = None
    """Set when an external event -- an auction, a volatility halt --
    intentionally postpones an otherwise valid order."""

    def __post_init__(self) -> None:
        # A negative count would *credit* the budget on transmission,
        # handing a strategy a way to manufacture capacity and defeat
        # every rate limit. No control may be overridable from a request.
        if self.messages <= 0:
            raise ValueError("a message request must carry at least 1 message")
        if self.ttl_ms < 0:
            raise ValueError("ttl must not be negative")


class ExecutionControlPlane:
    """Applies every control to every message, deterministically.

    Evaluation walks :data:`CONTROL_PRECEDENCE` and stops at the first
    control that binds, so two controls can never disagree about an
    outcome and the level recorded on a decision is the level that
    actually decided it. The matrix decides *what* a binding control
    does; this class decides *which* control binds.
    """

    def __init__(
        self,
        *,
        venue_budgets: Mapping[str, MessageBudget],
        global_budget: MessageBudget,
        pm_budgets: Mapping[str, MessageBudget] | None = None,
        strategy_pm: Mapping[str, str] | None = None,
        strategy_budgets: Mapping[str, MessageBudget] | None = None,
        instrument_budgets: Mapping[str, MessageBudget] | None = None,
        max_messages_per_order: int | None = None,
        require_reservation: bool = False,
        priority_ceilings: Mapping[Priority, Decimal] | None = None,
        kill_switch: KillSwitch | None = None,
        feed_monitor: FeedMonitor | None = None,
        limits: RiskLimits | None = None,
        cancel_resting_when_stale: bool = True,
        decision_history: int = 4096,
        now_ns: Callable[[], int] | None = None,
    ) -> None:
        self.venue_budgets = _stamp_level(
            _normalise_budgets(venue_budgets), ControlLevel.VENUE_LIMIT
        )
        self.global_budget = global_budget
        self.global_budget.level = ControlLevel.FIRM_MESSAGE_LIMIT
        self.pm_budgets = _stamp_level(
            _normalise_scopes(pm_budgets or {}), ControlLevel.PM_LIMIT
        )
        self.strategy_pm = _normalise_routes(strategy_pm or {})
        self.strategy_budgets = _stamp_level(
            _normalise_scopes(strategy_budgets or {}),
            ControlLevel.STRATEGY_LIMIT,
        )
        self.instrument_budgets = _stamp_level(
            _normalise_budgets(instrument_budgets or {}),
            ControlLevel.INSTRUMENT_LIMIT,
        )
        if max_messages_per_order is not None and max_messages_per_order < 1:
            raise ValueError(
                "max_messages_per_order must admit at least one message"
            )
        self.max_messages_per_order = max_messages_per_order
        self.require_reservation = require_reservation
        self.priority_ceilings = dict(
            PRIORITY_CEILING_PCT
            if priority_ceilings is None
            else priority_ceilings
        )
        for priority, ceiling in self.priority_ceilings.items():
            if not Decimal(0) <= ceiling <= Decimal(100):
                raise ValueError(
                    f"{priority.name} ceiling {format(ceiling, 'f')} is "
                    "not a percentage"
                )
        """Refuse any message naming an opportunity that never reserved.

        The specification requires capacity to be reserved for every
        planned message before the first leg transmits. Off by default so
        single-order callers need no ledger; on, it makes the requirement
        enforceable by the plane rather than by convention.
        """
        unknown = set(self.strategy_pm.values()) - set(self.pm_budgets)
        if unknown:
            # A strategy mapped to a portfolio manager with no budget
            # would silently skip the PM tier entirely.
            raise ValueError(
                "strategy_pm names portfolio managers with no budget: "
                + ", ".join(sorted(unknown))
            )
        self.kill_switch = kill_switch or KillSwitch()
        self.feed_monitor = feed_monitor or FeedMonitor()
        self.cancel_resting_when_stale = cancel_resting_when_stale
        self._now_ns = now_ns or _default_clock
        self.ledger = ReservationLedger(
            venue_budgets=self.venue_budgets,
            global_budget=self.global_budget,
            pm_budgets=self.pm_budgets,
            strategy_pm=self.strategy_pm,
            strategy_budgets=self.strategy_budgets,
            instrument_budgets=self.instrument_budgets,
            priority_ceilings=self.priority_ceilings,
            limits=limits,
        )
        self.queue = PriorityMessageQueue()
        self._lock = threading.RLock()
        # Bounded: evaluate() runs on the hot path, so an unbounded audit
        # list would grow for the lifetime of the process.
        self._decisions: deque[ControlDecision] = deque(
            maxlen=decision_history
        )

    # -- evaluation ---------------------------------------------------

    def evaluate(self, request: MessageRequest) -> ControlDecision:
        decision = self._evaluate(request)
        with self._lock:
            self._decisions.append(decision)
        if decision.action is not ControlAction.ADMIT:
            logger.info(
                "control_decision",
                message_id=request.message_id,
                stage=request.stage.value,
                action=decision.action.value,
                control=(
                    None if decision.control is None else decision.control.value
                ),
                reason=decision.reason,
            )
        return decision

    def _evaluate(self, request: MessageRequest) -> ControlDecision:
        stage = request.stage
        row = CONTROL_MATRIX[stage]
        venue = _normalise_venue(request.venue)

        # Validate the venue once, before the walk. Doing it inside the
        # venue-limit branch let a message for an unconfigured venue slip
        # through whenever a more senior control decided first: it would
        # be queued against the firm budget and then drain with no venue
        # limit applied at all.
        if venue not in self.venue_budgets:
            return ControlDecision(
                action=ControlAction.REJECT,
                control=ControlKind.VENUE_RATE_LIMIT,
                level=ControlLevel.VENUE_LIMIT,
                reason=f"no message budget configured for {request.venue}",
                stage=stage,
                priority=request.priority,
            )

        # The bottom of the hierarchy: a single order that plans more
        # messages than the per-order ceiling is a fat finger. Queueing it
        # would only postpone a request that can never fit, so the spec's
        # synchronous rejection applies -- and it applies before the walk,
        # because no amount of waiting makes an oversized order legal.
        ceiling = self.max_messages_per_order
        if ceiling is not None and request.messages > ceiling:
            return ControlDecision(
                action=ControlAction.REJECT,
                control=ControlKind.VENUE_RATE_LIMIT,
                level=ControlLevel.ORDER,
                reason=(
                    f"order plans {request.messages} messages, above the "
                    f"per-order ceiling of {ceiling}"
                ),
                stage=stage,
                priority=request.priority,
            )

        # A message backed by a live reservation has already passed every
        # capacity control -- that is what reserving *meant*. Charging it
        # again here would spend the budget twice for one transmission,
        # and worse, its own reservation counts against availability, so
        # the reservation itself could be what throttles it.
        drawdown: str | None = None
        if request.opportunity_id is not None:
            drawdown = self.ledger.room_for(
                request.opportunity_id,
                venue,
                request.messages,
                strategy_id=request.strategy_id,
                instrument=request.instrument,
                hedge_eligible=self._is_exempt(request),
            )
            if drawdown is None and self.require_reservation:
                return ControlDecision(
                    action=ControlAction.REJECT,
                    control=ControlKind.GLOBAL_THROTTLE,
                    level=ControlLevel.FIRM_MESSAGE_LIMIT,
                    reason=(
                        f"{request.opportunity_id} holds no reserved "
                        "capacity for this message"
                    ),
                    stage=stage,
                    priority=request.priority,
                )
        elif self.require_reservation:
            return ControlDecision(
                action=ControlAction.REJECT,
                control=ControlKind.GLOBAL_THROTTLE,
                level=ControlLevel.FIRM_MESSAGE_LIMIT,
                reason="no opportunity_id, so no capacity was reserved",
                stage=stage,
                priority=request.priority,
            )

        for kind, level in CONTROL_PRECEDENCE:
            action = row[kind]
            if kind in _CAPACITY_CONTROLS and drawdown is not None:
                # Already paid for. Kill switch and stale feed still apply.
                continue
            binding, reason = self._binds(kind, level, request, venue)
            if not binding:
                continue

            # The matrix says this stage is not subject to this control.
            if action is None or action is ControlAction.ADMIT:
                if kind in _CAPACITY_CONTROLS:
                    return self._transmit(
                        request, venue, "stage_exempt_from_control"
                    )
                continue

            if kind in _CAPACITY_CONTROLS:
                # Exposure-reducing traffic outranks any capacity limit.
                if self._is_exempt(request):
                    return self._transmit(
                        request, venue, "risk_reducing_exemption"
                    )
                if request.delay_reason:
                    return self._delayed(request)
                return self._capacity_decision(request, kind, level, venue)


            if (
                kind is ControlKind.STALE_FEED
                and action is ControlAction.CANCEL
                and not self.cancel_resting_when_stale
            ):
                action = ControlAction.MONITOR

            return ControlDecision(
                action=action,
                control=kind,
                level=level,
                reason=reason,
                stage=stage,
                priority=request.priority,
            )

        # Nothing bound. An intentional postponement is not a control
        # breach: the order stays valid and nothing is rejected.
        if request.delay_reason:
            return self._delayed(request)
        if drawdown is not None:
            return self._draw_down(request, venue, drawdown)
        return self._admit(request, venue, "admitted")

    def _draw_down(
        self, request: MessageRequest, venue: str, allowance: str
    ) -> ControlDecision:
        """Spend held capacity rather than taking it a second time."""

        assert request.opportunity_id is not None
        try:
            if allowance == "hedge":
                self.ledger.consume_hedge(
                    request.opportunity_id, venue, request.messages
                )
            else:
                self.ledger.consume(
                    request.opportunity_id, venue, request.messages
                )
        except CapacityReservationError:
            # The reservation was released or exhausted between the check
            # and the spend. Fall back to taking capacity outright.
            return self._admit(request, venue, "admitted")
        return ControlDecision(
            action=ControlAction.ADMIT,
            control=None,
            level=None,
            reason=f"reserved_{allowance}_capacity",
            stage=request.stage,
            priority=request.priority,
        )

    def _admit(
        self, request: MessageRequest, venue: str, reason: str
    ) -> ControlDecision:
        """Admit non-exempt traffic, or fall back if capacity vanished.

        ``_binds`` decides by *reading* the remaining allowance. Spending
        it with an unchecked increment would make every admission a
        check-then-act race, and two threads that both read "one slot
        left" would both send. The reservation below is a single atomic
        compare-and-increment, so the venue's hard limit holds.
        """

        refused = self._reserve_capacity(request, venue)
        if refused is None:
            return ControlDecision(
                action=ControlAction.ADMIT,
                control=None,
                level=None,
                reason=reason,
                stage=request.stage,
                priority=request.priority,
            )

        # Capacity disappeared between the read and the spend. Exposure
        # reducing traffic is still never blocked.
        if self._is_exempt(request):
            return self._transmit(request, venue, "risk_reducing_exemption")
        kind, level, budget_key, budget = refused
        action = CONTROL_MATRIX[request.stage][kind]
        if action is None or action is ControlAction.ADMIT:
            return self._transmit(
                request, venue, "stage_exempt_from_control"
            )
        return self._capacity_decision(
            request, kind, level, venue, budget_key, budget
        )

    def _reserve_capacity(
        self, request: MessageRequest, venue: str
    ) -> tuple[ControlKind, ControlLevel, str, MessageBudget] | None:
        """Take every configured tier together, or none of them.

        Returns the tier that refused, or ``None`` when all were taken.
        A partial take would let a message spend a portfolio manager's
        budget and then be refused by its venue, quietly draining an
        allowance for a message that was never sent.

        The take enforces the priority ceiling as well as the hard
        limit. ``_binds`` has already read the ceiling, but a read is
        only a read: without re-testing it under the same acquisition
        that reserves, two threads both cleared by ``_binds`` both
        reserve and the class ends up past its share. A tier refused
        here lands in the same "capacity disappeared" fallback as any
        other late refusal, so the message is queued or rejected with
        the reason the matrix prescribes.
        """

        ceiling = self._ceiling_for(request.priority)
        tiers = self._capacity_tiers(request, venue)
        taken: list[MessageBudget] = []
        for tier in tiers:
            budget = tier[3]
            if not budget.try_reserve_within(ceiling, request.messages):
                taken.append(budget)
                continue
            for done in taken:
                done.release(request.messages)
            return tier
        for budget in taken:
            budget.consume_reserved(request.messages)
        return None

    def _tier(
        self, level: ControlLevel, request: MessageRequest, venue: str
    ) -> tuple[str, MessageBudget] | None:
        """The budget guarding one level, or ``None`` if unconfigured.

        An unconfigured tier does not bind. That is what makes the four
        scoped tiers opt-in: a plane built with only firm and venue
        budgets behaves exactly as it did before they existed.
        """

        if level is ControlLevel.FIRM_MESSAGE_LIMIT:
            return _GLOBAL_BUDGET_KEY, self.global_budget
        if level is ControlLevel.PM_LIMIT:
            manager = self.strategy_pm.get(
                _normalise_venue(request.strategy_id)
            )
            if manager is None:
                return None
            budget = self.pm_budgets.get(manager)
            if budget is None:
                return None
            return _PM_KEY_PREFIX + manager, budget
        if level is ControlLevel.STRATEGY_LIMIT:
            strategy = _normalise_venue(request.strategy_id)
            budget = self.strategy_budgets.get(strategy)
            if budget is None:
                return None
            return _STRATEGY_KEY_PREFIX + strategy, budget
        if level is ControlLevel.VENUE_LIMIT:
            budget = self.venue_budgets.get(venue)
            if budget is None:
                return None
            return venue, budget
        if level is ControlLevel.INSTRUMENT_LIMIT:
            if not request.instrument:
                return None
            key = _normalise_venue(request.instrument)
            budget = self.instrument_budgets.get(key)
            if budget is None:
                return None
            return _INSTRUMENT_KEY_PREFIX + key, budget
        return None

    def _capacity_tiers(
        self, request: MessageRequest, venue: str
    ) -> tuple[tuple[ControlKind, ControlLevel, str, MessageBudget], ...]:
        """Every configured capacity tier, in precedence order."""

        tiers: list[tuple[ControlKind, ControlLevel, str, MessageBudget]] = []
        for kind, level in CONTROL_PRECEDENCE:
            if kind not in _CAPACITY_CONTROLS:
                continue
            resolved = self._tier(level, request, venue)
            if resolved is None:
                continue
            tiers.append((kind, level, resolved[0], resolved[1]))
        return tuple(tiers)

    def _binds(
        self,
        kind: ControlKind,
        level: ControlLevel,
        request: MessageRequest,
        venue: str,
    ) -> tuple[bool, str]:
        """Whether a control currently applies, and why."""

        if kind is ControlKind.KILL_SWITCH:
            if not self.kill_switch.active:
                return False, ""
            return True, self.kill_switch.reason or "kill_switch_active"

        if kind is ControlKind.STALE_FEED:
            if not self.feed_monitor.is_stale(request.venue):
                return False, ""
            age = self.feed_monitor.age_ns(request.venue)
            if age is None:
                return True, f"{request.venue} feed has never ticked"
            return True, f"{request.venue} feed age {age // 1_000_000}ms"

        resolved = self._tier(level, request, venue)
        if resolved is None:
            if level is ControlLevel.VENUE_LIMIT:
                # An unknown venue has no configured allowance. Treating
                # that as "unlimited" would silently disable the control.
                return (
                    True,
                    f"no message budget configured for {request.venue}",
                )
            # An unconfigured optional tier simply does not exist.
            return False, ""
        budget = resolved[1]
        if level is ControlLevel.FIRM_MESSAGE_LIMIT:
            reason = "firm message budget exhausted"
        else:
            reason = f"{budget.name} message budget exhausted"
        if budget.available() < request.messages:
            return True, reason
        ceiling = self._ceiling_for(request.priority)
        if budget.above_ceiling(ceiling, request.messages):
            return True, (
                f"{budget.name} is past {format(ceiling, 'f')}%, held "
                f"for traffic above {request.priority.name.lower()}"
            )
        return False, reason

    def _ceiling_for(self, priority: Priority) -> Decimal:
        """How far this class may push a shared budget.

        Exempt classes have no ceiling: requirement 10 says nothing may
        block exposure reduction, and a headroom rule is still a block.
        """

        if priority in EXEMPT_PRIORITIES:
            return Decimal(100)
        return self.priority_ceilings.get(priority, Decimal(100))

    def _is_exempt(self, request: MessageRequest) -> bool:
        return (
            request.priority in EXEMPT_PRIORITIES
            or request.stage in RISK_REDUCING_STAGES
        )

    def _delayed(self, request: MessageRequest) -> ControlDecision:
        return ControlDecision(
            action=ControlAction.DELAY,
            control=None,
            level=None,
            reason=request.delay_reason or "delayed",
            stage=request.stage,
            priority=request.priority,
        )

    def _transmit(
        self, request: MessageRequest, venue: str, reason: str
    ) -> ControlDecision:
        """Admit exempt traffic unconditionally, and charge for it.

        Only exposure-reducing traffic reaches here. It is charged even
        when that pushes usage past the allowance -- nothing may stop a
        hedge, but a hedge storm that doubles the firm's message rate
        must still be visible to operations. Non-exempt traffic goes
        through :meth:`_admit`, which cannot overshoot.
        """

        for _kind, _level, _key, budget in self._capacity_tiers(
            request, venue
        ):
            budget.force_consume(request.messages)
        return ControlDecision(
            action=ControlAction.ADMIT,
            control=None,
            level=None,
            reason=reason,
            stage=request.stage,
            priority=request.priority,
        )

    def _never_fits(
        self, request: MessageRequest, venue: str, ceiling: Decimal
    ) -> tuple[MessageBudget, ControlLevel] | None:
        """The most senior tier that could never supply this request.

        ``_evaluate`` returns on the *first* binding control, so the
        tiers below it were never consulted. A message that fits the one
        that bound but can never fit a lower one would be queued, never
        be drainable at any point in its life, and be dropped at expiry
        -- exactly what this guard exists to prevent.
        """

        for _kind, level, _key, budget in self._capacity_tiers(
            request, venue
        ):
            if budget.never_fits(ceiling, request.messages):
                return budget, level
        return None

    def _capacity_decision(
        self,
        request: MessageRequest,
        control: ControlKind,
        level: ControlLevel,
        venue: str,
        budget_key: str | None = None,
        budget: MessageBudget | None = None,
    ) -> ControlDecision:
        action = CONTROL_MATRIX[request.stage][control]
        assert action is not None  # guarded by the caller
        if budget is None:
            resolved = self._tier(level, request, venue)
            if resolved is not None:
                budget_key, budget = resolved
        if budget is None or budget_key is None:
            return ControlDecision(
                action=ControlAction.REJECT,
                control=control,
                level=level,
                reason=f"no message budget configured for {request.venue}",
                stage=request.stage,
                priority=request.priority,
            )

        retry_ms = budget.time_to_refill_ms()
        if action is ControlAction.QUEUE:
            ceiling = self._ceiling_for(request.priority)
            impossible = self._never_fits(request, venue, ceiling)
            if impossible is not None:
                # Larger than this tier can ever supply, so an empty
                # budget would refuse it too. Queueing it would park a
                # message that no drain can ever release, hold its scope
                # against every later scan, and lose it silently at
                # expiry.
                blocker, blocker_level = impossible
                # Count the refusal against the tier that refused, not
                # the tier that merely happened to bind first. The
                # reason names `blocker`, and an operator drilling into
                # per-tier counts to decide which limit to raise must
                # be pointed at the same tier the message names.
                blocker.note_rejected()
                return ControlDecision(
                    action=ControlAction.REJECT,
                    control=control,
                    level=blocker_level,
                    reason=(
                        f"{request.messages} messages exceed the "
                        f"{format(ceiling.normalize(), 'f')}% of "
                        f"{blocker.name} allowed to "
                        f"{request.priority.name.lower()}"
                        if ceiling < 100
                        else (
                            f"{request.messages} messages exceed the "
                            f"{blocker.limit} {blocker.name} allows "
                            f"in one window"
                        )
                    ),
                    stage=request.stage,
                    priority=request.priority,
                )
            if retry_ms >= request.ttl_ms:
                # It would expire before capacity returns, so rejecting
                # now is more honest than queueing it to die.
                budget.note_rejected()
                return ControlDecision(
                    action=ControlAction.REJECT,
                    control=control,
                    level=level,
                    reason=(
                        f"{budget.name} capacity returns in {retry_ms}ms, "
                        f"past the {request.ttl_ms}ms deadline"
                    ),
                    stage=request.stage,
                    priority=request.priority,
                    retry_after_ms=retry_ms,
                )
            now = self._now_ns()
            self.queue.push(
                QueuedMessage(
                    message_id=request.message_id,
                    # Folded like `venue`, so the queued message carries
                    # one identity. Enforcement folds at every lookup so
                    # it was already correct, but `strategies()` puts
                    # this field straight on the operator dashboard: one
                    # strategy on one budget appeared as three separate
                    # names, inflating the apparent blast radius of the
                    # incident the dashboard exists to explain, and
                    # breaking any responder that intersects it with the
                    # configured budget keys.
                    strategy_id=_normalise_venue(request.strategy_id),
                    venue=venue,
                    stage=request.stage,
                    priority=request.priority,
                    messages=request.messages,
                    enqueued_ns=now,
                    expires_ns=now + request.ttl_ms * 1_000_000,
                    budget_key=budget_key,
                    instrument=_normalise_venue(request.instrument),
                )
            )
            budget.note_queued()
        return ControlDecision(
            action=action,
            control=control,
            level=level,
            reason=f"{budget.name} limit reached",
            stage=request.stage,
            priority=request.priority,
            retry_after_ms=retry_ms,
            detail={"queue_depth": len(self.queue)},
        )

    # -- queue draining ----------------------------------------------

    def _budget_named(self, key: str) -> MessageBudget | None:
        """Resolve the budget a queued message was recorded against.

        Resolution is by dict *key*, not display name: two venues may
        legitimately share a name, and scanning for the first name match
        would settle the counter on the wrong budget -- reintroducing the
        unbounded queue-depth drift this field exists to prevent.
        """

        if key == _GLOBAL_BUDGET_KEY:
            return self.global_budget
        if key.startswith(_PM_KEY_PREFIX):
            return self.pm_budgets.get(key[len(_PM_KEY_PREFIX):])
        if key.startswith(_STRATEGY_KEY_PREFIX):
            return self.strategy_budgets.get(key[len(_STRATEGY_KEY_PREFIX):])
        if key.startswith(_INSTRUMENT_KEY_PREFIX):
            return self.instrument_budgets.get(
                key[len(_INSTRUMENT_KEY_PREFIX):]
            )
        return self.venue_budgets.get(key)

    def _try_release(
        self, message: QueuedMessage, blocked: frozenset[str] = frozenset()
    ) -> tuple[ReleaseVerdict, str]:
        """Reserve capacity for a queued message, all or nothing.

        A tier shared by messages beyond this one's venue is reported as
        ``STOP``: if it cannot cover this message it cannot cover
        anything behind it either, and scanning on would hand its last
        slot to lower-priority traffic. That is true of the firm budget,
        and equally true of a PM's and a strategy's -- the shared-budget
        property is what makes ``STOP`` necessary, not the firm level.

        The key of the refusing budget is returned with the verdict so
        the scan can block just that scope. ``_GLOBAL_BUDGET_KEY`` is
        reported as an empty key, which ends the scan: every queued
        message draws on the firm budget, so nothing behind it can go.
        """

        if message.venue not in self.venue_budgets:
            # No configured allowance. Transmitting would apply no venue
            # limit at all, so it stays queued rather than escaping.
            return ReleaseVerdict.SKIP, ""
        tiers = self._capacity_tiers(
            MessageRequest(
                message_id=message.message_id,
                strategy_id=message.strategy_id,
                venue=message.venue,
                stage=message.stage,
                priority=message.priority,
                messages=message.messages,
                instrument=message.instrument,
            ),
            message.venue,
        )
        # A scope already known to be short must not be probed again:
        # reserving against it would succeed for a smaller message and
        # reintroduce the inversion the earlier STOP was preventing.
        if any(key in blocked for _kind, _level, key, _budget in tiers):
            return ReleaseVerdict.SKIP, ""

        ceiling = self._ceiling_for(message.priority)
        taken: list[MessageBudget] = []

        def refuse(
            level: ControlLevel, key: str, forever: bool = False
        ) -> tuple[ReleaseVerdict, str]:
            for done in taken:
                done.release(message.messages)
            if forever or level not in _SHARED_LEVELS:
                # A venue or instrument tier is genuinely scoped, so a
                # message for a different one may still be releasable.
                return ReleaseVerdict.SKIP, ""
            if level is ControlLevel.FIRM_MESSAGE_LIMIT:
                return ReleaseVerdict.STOP, ""
            return ReleaseVerdict.STOP, key

        for _kind, level, key, budget in tiers:
            # Draining must apply the same headroom rule as admission,
            # or the ceiling would only ever delay a message by one
            # drain cycle instead of reserving anything. Testing it and
            # reserving happen under one acquisition: `pop_if` holds the
            # queue lock so two drains cannot interleave, but a
            # concurrent admission can, and two separate acquisitions
            # would let the pair straddle it and overshoot the ceiling.
            refusal = budget.try_reserve_within(ceiling, message.messages)
            if refusal == "ceiling":
                # A message too large for its class's whole share says
                # nothing about the budget, so it must not stop the
                # scan on the budget's behalf. Admission rejects these,
                # but a caller may push one onto the queue directly.
                return refuse(
                    level,
                    key,
                    forever=budget.never_fits(ceiling, message.messages),
                )
            if refusal:
                return refuse(level, key)
            taken.append(budget)
        for budget in taken:
            budget.consume_reserved(message.messages)
        return ReleaseVerdict.TAKE, ""

    def drain(self, limit: int | None = None) -> tuple[QueuedMessage, ...]:
        """Release queued messages in priority order as capacity allows."""

        now = self._now_ns()
        for message in self.queue.expire(now):
            settled = self._budget_named(message.budget_key)
            if settled is not None:
                settled.note_dropped()

        released: list[QueuedMessage] = []
        while limit is None or len(released) < limit:
            # Peeking and popping separately would let two concurrent
            # drains transmit the same message twice.
            message = self.queue.pop_if(self._try_release)
            if message is None:
                break
            settled = self._budget_named(message.budget_key)
            if settled is not None:
                settled.note_dequeued()
            released.append(message)
        return tuple(released)

    # -- bulk actions -------------------------------------------------

    def mass_cancel_scope(
        self,
        *,
        strategy_id: str | None = None,
        venue: str | None = None,
        instrument: str | None = None,
    ) -> dict[str, Any]:
        """Describe a batch cancellation without performing it.

        Mass cancel is always scoped -- by strategy, venue, instrument or
        the whole account -- and the scope is recorded so an audit can
        reconstruct exactly what was pulled.
        """

        scope = "account"
        if instrument is not None:
            scope = "instrument"
        elif venue is not None:
            scope = "venue"
        elif strategy_id is not None:
            scope = "strategy"
        return {
            "scope": scope,
            "strategy_id": strategy_id,
            "venue": venue,
            "instrument": instrument,
            "reason": self.kill_switch.reason or "operator_request",
        }

    # -- telemetry ----------------------------------------------------

    def _all_budgets(self) -> tuple[MessageBudget, ...]:
        """Every configured capacity tier, firm first.

        A tier that is queueing, rejecting and dropping messages but
        never appears here is a control the operator cannot see.
        """

        tiers: list[MessageBudget] = [self.global_budget]
        for scope in (
            self.pm_budgets,
            self.strategy_budgets,
            self.instrument_budgets,
            self.venue_budgets,
        ):
            tiers.extend(scope[name] for name in sorted(scope))
        return tuple(tiers)

    def snapshots(self) -> tuple[CapacitySnapshot, ...]:
        return tuple(budget.snapshot() for budget in self._all_budgets())

    def dashboard(self) -> dict[str, Any]:
        """Everything an operator needs *before* a control binds.

        Each budget is snapshotted under its own lock, but not all of
        them under one, so a busy plane can report two budgets from
        moments either side of a reservation. That is accepted rather
        than overlooked: this is an operator view and nothing routes on
        it, so the cost of a torn read is a band that flickers between
        two adjacent values on one refresh. Locking the whole plane to
        remove it would put every dashboard poll on the admission path.
        """

        budgets = self._all_budgets()
        snapshots = tuple(budget.snapshot() for budget in budgets)
        worst = max(snapshots, key=lambda item: item.utilization_pct)
        # "When does the thing that is blocking me recover?" and "who is
        # being held back by a ceiling?" are two different questions, and
        # folding them into one scalar answers neither. A max() over both
        # sets lets an idle daily budget at 65% -- blocking nobody --
        # dominate the recovery of the venue that is actually binding,
        # reporting a day where the truth is a second.
        recovery = [
            budget.time_to_refill_ms()
            for budget, item in zip(budgets, snapshots)
            if item.binding
        ]
        floor = min(self.priority_ceilings.values(), default=Decimal(100))
        holding_back = [
            {
                "name": item.name,
                "level": item.level.name,
                "utilization_pct": str(item.utilization_pct),
                "refills_in_ms": budget.time_to_refill_ms(),
                "classes_held_back": [
                    priority.name.lower()
                    for priority, ceiling in sorted(
                        self.priority_ceilings.items(),
                        key=lambda pair: pair[0],
                    )
                    if ceiling < 100 and item.utilization_pct >= ceiling
                ],
            }
            for budget, item in zip(budgets, snapshots)
            if not item.binding and item.utilization_pct >= floor
        ]
        return {
            "band": worst.band.value,
            "binding": any(item.binding for item in snapshots),
            "driver": worst.name,
            "capacity": [item.to_dict() for item in snapshots],
            "queued_messages": len(self.queue),
            "queue_by_priority": self.queue.depth_by_priority(),
            "affected_strategies": list(self.queue.strategies()),
            "dropped_messages": sum(item.dropped for item in snapshots),
            "rejected_messages": sum(item.rejected for item in snapshots),
            "expected_recovery_ms": max(recovery, default=0),
            "holding_back": holding_back,
            "kill_switch": self.kill_switch.to_dict(),
            "feeds": self.feed_monitor.to_dict(),
            "reservations": self.ledger.to_dict(),
            "recommended_actions": _recommended_actions(worst.band),
        }

    def decisions(self) -> tuple[ControlDecision, ...]:
        with self._lock:
            return tuple(self._decisions)


def _recommended_actions(band: UtilizationBand) -> list[str]:
    """What operations should do at each level, before the breach."""

    if band is UtilizationBand.GREEN:
        return []
    if band is UtilizationBand.YELLOW:
        return [
            "notify portfolio manager: approaching rate limit",
            "expect queue growth and added latency",
        ]
    if band is UtilizationBand.ORANGE:
        return [
            "reduce quote refresh rate",
            "suspend passive strategies",
            "reserve hedge capacity",
        ]
    return [
        "control is binding: non-exempt traffic is queued or rejected",
        "only risk-reducing traffic is guaranteed transmission",
    ]
