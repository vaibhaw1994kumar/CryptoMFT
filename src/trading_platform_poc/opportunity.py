"""Two-venue concurrent-leg opportunity state machine.

This module coordinates a multi-leg strategy (arbitrage, spread, or
market-neutral hedge) across two or more venues while guaranteeing:

* concurrent leg dispatch with no head-of-line blocking,
* exactly-once logical orders across retries, timeouts and restarts,
* deterministic recovery of residual exposure,
* four-way reconciliation before an opportunity is allowed to close.

The module deliberately depends only on the standard library, the
``ExchangeAdapter`` protocol and :mod:`trading_platform_poc.opportunity_store`.
It never imports the native extension, so it keeps working on hosts where
Application Control blocks native DLLs.

Lifecycle
---------

``CREATED`` -> ``RISK_RESERVED`` -> ``ORDERS_CREATED`` -> ``DISPATCHED``
-> ``ACKNOWLEDGED`` -> ``PARTIALLY_FILLED`` / ``FULLY_FILLED``
-> ``HEDGE_REQUIRED`` -> ``UNWINDING`` -> ``RECONCILING`` -> ``CLOSED``

``REJECTED`` and ``TIMEOUT`` are transient waypoints that always resolve
into hedge, unwind or closure; they are never terminal on their own.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

import structlog

from trading_platform_poc.exchange_adapter import (
    AmbiguousOrderError,
    ExchangeAdapter,
    ExchangeAdapterError,
    ExchangeAPIError,
    ExchangeOrder,
    ExchangeTrade,
)
from trading_platform_poc.execution_controls import (
    CapacityReservationError,
    ExecutionControlPlane,
    MessageRequest,
    OrderStage,
    Priority,
    ReservationRequest,
    normalise_scope,
)
from trading_platform_poc.models import Order, OrderState, OrderType, Side
from trading_platform_poc.opportunity_store import (
    FillRecord,
    LegRecord,
    OpportunityRecord,
    OpportunityStore,
)


logger = structlog.get_logger()

_LIVE_STATUSES = frozenset(
    {
        "accepted",
        "cancel_requested",
        "live",
        "new",
        "open",
        "partially_filled",
        "pending_new",
        "untriggered",
    }
)
_FILLED_STATUSES = frozenset({"filled"})
_CANCELED_STATUSES = frozenset(
    {"canceled", "cancelled", "expired", "mmp_canceled"}
)
_REJECTED_STATUSES = frozenset({"rejected"})


class OpportunityState(str, Enum):
    CREATED = "created"
    RISK_RESERVED = "risk_reserved"
    ORDERS_CREATED = "orders_created"
    DISPATCHED = "dispatched"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FULLY_FILLED = "fully_filled"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    HEDGE_REQUIRED = "hedge_required"
    UNWINDING = "unwinding"
    RECONCILING = "reconciling"
    CLOSED = "closed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


TERMINAL_STATES = frozenset(
    {
        OpportunityState.CLOSED,
        OpportunityState.RECONCILIATION_REQUIRED,
    }
)


class LegState(str, Enum):
    PENDING = "pending"
    SUBMITTING = "submitting"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    AMBIGUOUS = "ambiguous"
    ABANDONED = "abandoned"
    BLOCKED = "blocked"
    """Never transmitted: a control refused it before it reached a venue.

    Distinct from ``REJECTED``, which is the venue's answer, and from
    ``ABANDONED``, which means we hold positive evidence no order exists.
    A blocked leg is terminal because nothing is in flight to reconcile.
    """


TERMINAL_LEG_STATES = frozenset(
    {
        LegState.FILLED,
        LegState.CANCELED,
        LegState.REJECTED,
        LegState.ABANDONED,
        LegState.BLOCKED,
    }
)

# Unwind legs are numbered from here so they never collide with the
# strategy legs, which are indexed from zero.
_UNWIND_INDEX_BASE = 1000

# Longest-first so BTCUSDT resolves to USDT rather than USD.
_QUOTE_SUFFIXES: tuple[str, ...] = (
    "USDT",
    "USDC",
    "TUSD",
    "BUSD",
    "USD",
    "EUR",
    "BTC",
    "ETH",
)


class RejectReason(str, Enum):
    PRICE_BAND = "price_band"
    POST_ONLY = "post_only"
    INSUFFICIENT_MARGIN = "insufficient_margin"
    RISK_LIMIT = "risk_limit"
    EXCHANGE_MAINTENANCE = "exchange_maintenance"
    RATE_LIMITED = "rate_limited"
    DUPLICATE_ORDER = "duplicate_order"
    INVALID_ORDER = "invalid_order"
    UNKNOWN = "unknown"


class RejectAction(str, Enum):
    REPRICE = "reprice"
    CANCEL_REPLACE = "cancel_replace"
    ABORT = "abort"
    ABORT_PERMANENT = "abort_permanent"
    ROUTE_ALTERNATE = "route_alternate"
    BACKOFF_RETRY = "backoff_retry"
    ADOPT_EXISTING = "adopt_existing"
    MANUAL_INVESTIGATION = "manual_investigation"


class RetryDirective(str, Enum):
    """What to do about a transport-level failure."""

    QUERY_THEN_RETRY = "query_then_retry"
    RECONNECT_AND_RESYNC = "reconnect_and_resync"
    BACKOFF_RETRY = "backoff_retry"
    DELAY_RETRY = "delay_retry"
    NO_RETRY = "no_retry"


class UnwindPriceMode(str, Enum):
    MARKET = "market"
    AGGRESSIVE_LIMIT = "aggressive_limit"


_REJECT_ACTIONS: Mapping[RejectReason, RejectAction] = {
    RejectReason.PRICE_BAND: RejectAction.REPRICE,
    RejectReason.POST_ONLY: RejectAction.CANCEL_REPLACE,
    RejectReason.INSUFFICIENT_MARGIN: RejectAction.ABORT,
    RejectReason.RISK_LIMIT: RejectAction.ABORT_PERMANENT,
    RejectReason.EXCHANGE_MAINTENANCE: RejectAction.ROUTE_ALTERNATE,
    RejectReason.RATE_LIMITED: RejectAction.BACKOFF_RETRY,
    RejectReason.DUPLICATE_ORDER: RejectAction.ADOPT_EXISTING,
    RejectReason.INVALID_ORDER: RejectAction.MANUAL_INVESTIGATION,
    RejectReason.UNKNOWN: RejectAction.MANUAL_INVESTIGATION,
}

# Ordered most specific first so classification is deterministic.
_REJECT_PATTERNS: tuple[tuple[RejectReason, tuple[str, ...]], ...] = (
    (
        RejectReason.DUPLICATE_ORDER,
        ("duplicate", "already exists", "clientorderid is duplicated"),
    ),
    (
        RejectReason.POST_ONLY,
        ("post only", "post-only", "would immediately match", "postonly"),
    ),
    (
        RejectReason.PRICE_BAND,
        (
            "price band",
            "percent price",
            "price filter",
            "price is outside",
            "limit price",
            "price_filter",
        ),
    ),
    (
        RejectReason.INSUFFICIENT_MARGIN,
        (
            "insufficient",
            "not enough",
            "balance too low",
            "margin",
        ),
    ),
    (
        RejectReason.RISK_LIMIT,
        ("risk limit", "position limit", "exceeds the maximum position"),
    ),
    (
        RejectReason.EXCHANGE_MAINTENANCE,
        ("maintenance", "system upgrade", "service unavailable"),
    ),
    (
        RejectReason.RATE_LIMITED,
        ("rate limit", "too many requests", "request weight"),
    ),
    (
        RejectReason.INVALID_ORDER,
        (
            "invalid",
            "unknown symbol",
            "lot size",
            "min notional",
            "notional",
            "unsupported",
        ),
    ),
)

_RETRY_PATTERNS: tuple[tuple[RetryDirective, tuple[str, ...]], ...] = (
    (
        RetryDirective.DELAY_RETRY,
        ("rate limit", "too many requests", "request weight"),
    ),
    (
        RetryDirective.BACKOFF_RETRY,
        ("busy", "overloaded", "try again", "maintenance", "unavailable"),
    ),
    (
        RetryDirective.RECONNECT_AND_RESYNC,
        (
            "disconnect",
            "connection reset",
            "connection aborted",
            "broken pipe",
            "server disconnected",
            "closed",
        ),
    ),
    (
        RetryDirective.QUERY_THEN_RETRY,
        ("timeout", "timed out"),
    ),
    (
        RetryDirective.NO_RETRY,
        ("invalid", "unknown symbol", "lot size", "notional"),
    ),
)


def _normalize(message: str) -> str:
    """Fold exchange filter codes onto the pattern vocabulary.

    Venues report ``PERCENT_PRICE``/``MIN_NOTIONAL`` style codes, so
    separators are collapsed to spaces before matching.
    """

    return message.lower().replace("_", " ").replace("-", " ")


def classify_reject(message: str) -> RejectReason:
    """Map an exchange reject message onto a deterministic reason."""

    lowered = _normalize(message)
    for reason, needles in _REJECT_PATTERNS:
        if any(_normalize(needle) in lowered for needle in needles):
            return reason
    return RejectReason.UNKNOWN


def action_for_reject(reason: RejectReason) -> RejectAction:
    return _REJECT_ACTIONS[reason]


def classify_retry(error: BaseException) -> RetryDirective:
    """Map a transport failure onto a deterministic retry directive."""

    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return RetryDirective.QUERY_THEN_RETRY
    if isinstance(error, AmbiguousOrderError):
        return RetryDirective.QUERY_THEN_RETRY
    if isinstance(error, ExchangeAPIError):
        if error.status == 429 or error.status == 418:
            return RetryDirective.DELAY_RETRY
        if error.status >= 500:
            return RetryDirective.BACKOFF_RETRY
        return RetryDirective.NO_RETRY
    lowered = _normalize(str(error))
    for directive, needles in _RETRY_PATTERNS:
        if any(_normalize(needle) in lowered for needle in needles):
            return directive
    if isinstance(error, (ConnectionError, OSError)):
        return RetryDirective.RECONNECT_AND_RESYNC
    return RetryDirective.NO_RETRY


@dataclass(frozen=True, slots=True)
class OpportunityTimeouts:
    """Per-leg deadlines, measured from the moment the leg is dispatched."""

    ack_timeout_ms: int = 100
    first_fill_timeout_ms: int = 500
    complete_fill_timeout_ms: int = 2000
    poll_interval_ms: int = 10
    max_submit_attempts: int = 3
    backoff_base_ms: int = 20

    def __post_init__(self) -> None:
        if self.ack_timeout_ms <= 0:
            raise ValueError("ack timeout must be positive")
        if self.first_fill_timeout_ms < self.ack_timeout_ms:
            raise ValueError(
                "first fill timeout must not precede the ack timeout"
            )
        if self.complete_fill_timeout_ms < self.first_fill_timeout_ms:
            raise ValueError(
                "complete fill timeout must not precede the first fill "
                "timeout"
            )
        if self.poll_interval_ms <= 0:
            raise ValueError("poll interval must be positive")
        if self.max_submit_attempts <= 0:
            raise ValueError("submit attempts must be positive")
        if self.backoff_base_ms < 0:
            raise ValueError("backoff base must not be negative")


@dataclass(frozen=True, slots=True)
class ResidualPolicy:
    """Bounds on residual exposure and how to unwind it."""

    max_residual_ratio: Decimal = Decimal("0.05")
    max_residual_duration_ms: int = 2000
    max_hedge_delay_ms: int = 500
    unwind_venue_priority: tuple[str, ...] = ()
    unwind_price_mode: UnwindPriceMode = UnwindPriceMode.MARKET
    unwind_aggression_bps: Decimal = Decimal("10")
    max_unwind_attempts: int = 3

    def __post_init__(self) -> None:
        if not Decimal(0) <= self.max_residual_ratio <= Decimal(1):
            raise ValueError("residual ratio must fall within [0, 1]")
        if self.max_residual_duration_ms < 0:
            raise ValueError("residual duration must not be negative")
        if self.max_hedge_delay_ms < 0:
            raise ValueError("hedge delay must not be negative")
        if self.unwind_aggression_bps < 0:
            raise ValueError("unwind aggression must not be negative")
        if self.max_unwind_attempts <= 0:
            raise ValueError("unwind attempts must be positive")


@dataclass(frozen=True, slots=True)
class MarketQuote:
    """Top of book for deterministic unwind venue and price selection."""

    venue: str
    symbol: str
    bid_price: Decimal
    ask_price: Decimal
    bid_quantity: Decimal = Decimal(0)
    ask_quantity: Decimal = Decimal(0)
    fee_bps: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if self.bid_price <= 0 or self.ask_price <= 0:
            raise ValueError("quote prices must be positive")
        if self.bid_price >= self.ask_price:
            raise ValueError("quote is crossed or locked")
        if self.fee_bps < 0:
            raise ValueError("fee must not be negative")


@dataclass(frozen=True, slots=True)
class LegRequest:
    venue: str
    symbol: str
    side: Side
    quantity: Decimal
    reference_price: Decimal
    order_type: OrderType = OrderType.LIMIT
    limit_price: Decimal | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("leg quantity must be positive")
        if self.reference_price <= 0:
            raise ValueError("leg reference price must be positive")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("leg limit price must be positive")


@dataclass(frozen=True, slots=True)
class OpportunityRequest:
    strategy_id: str
    strategy_version: str
    signal_id: str
    canonical_symbol: str
    legs: tuple[LegRequest, ...]
    expected_edge: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if len(self.legs) < 2:
            raise ValueError("an opportunity requires at least two legs")
        venues = [leg.venue.lower() for leg in self.legs]
        if len(set(venues)) != len(venues):
            raise ValueError("each leg must target a distinct venue")
        if not self.signal_id:
            raise ValueError("signal id is required for idempotency")

    @property
    def target_quantity(self) -> Decimal:
        return max(leg.quantity for leg in self.legs)


@dataclass(frozen=True, slots=True)
class UnwindPlan:
    venue: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal | None
    reason: str
    candidates: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class LegOutcome:
    leg_index: int
    venue: str
    symbol: str
    side: Side
    target_quantity: Decimal
    filled_quantity: Decimal
    average_price: Decimal | None
    state: LegState
    client_order_id: str
    idempotency_key: str
    exchange_order_id: str | None
    attempts: int
    reject_reason: RejectReason | None
    reject_action: RejectAction | None
    last_error: str | None

    @property
    def remaining_quantity(self) -> Decimal:
        return max(Decimal(0), self.target_quantity - self.filled_quantity)

    @property
    def signed_filled_quantity(self) -> Decimal:
        if self.side is Side.BUY:
            return self.filled_quantity
        return -self.filled_quantity


@dataclass(frozen=True, slots=True)
class OpportunityReconciliation:
    """Four independent views of the same position, compared."""

    opportunity_id: str
    exchange_positions: Mapping[str, Decimal]
    local_positions: Mapping[str, Decimal]
    risk_positions: Mapping[str, Decimal]
    ledger_position: Decimal
    net_position: Decimal
    open_order_count: int
    risk_released: bool
    gross_notional: Decimal
    fees: Decimal
    realized_pnl: Decimal
    hedge_cost: Decimal
    slippage: Decimal
    discrepancies: tuple[str, ...]
    fees_by_currency: Mapping[str, Decimal] = field(default_factory=dict)
    unconverted_fees: Mapping[str, Decimal] = field(default_factory=dict)
    quote_currency: str = "USDT"

    @property
    def positions_reconcile(self) -> bool:
        """Whether every position view agrees and nothing is still live.

        This is the question that decides whether risk may be released.
        A fee reported in the base asset is an accounting nuance, not
        evidence of unflat exposure, so it must not gate it.
        """

        return not self.discrepancies

    @property
    def balanced(self) -> bool:
        return not self.discrepancies

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "exchange_positions": {
                venue: format(value, "f")
                for venue, value in sorted(self.exchange_positions.items())
            },
            "local_positions": {
                venue: format(value, "f")
                for venue, value in sorted(self.local_positions.items())
            },
            "risk_positions": {
                venue: format(value, "f")
                for venue, value in sorted(self.risk_positions.items())
            },
            "ledger_position": format(self.ledger_position, "f"),
            "net_position": format(self.net_position, "f"),
            "open_order_count": self.open_order_count,
            "risk_released": self.risk_released,
            "gross_notional": format(self.gross_notional, "f"),
            "fees": format(self.fees, "f"),
            "quote_currency": self.quote_currency,
            "fees_by_currency": {
                currency: format(value, "f")
                for currency, value in sorted(self.fees_by_currency.items())
            },
            "unconverted_fees": {
                currency: format(value, "f")
                for currency, value in sorted(self.unconverted_fees.items())
            },
            "realized_pnl": format(self.realized_pnl, "f"),
            "hedge_cost": format(self.hedge_cost, "f"),
            "slippage": format(self.slippage, "f"),
            "discrepancies": list(self.discrepancies),
            "balanced": self.balanced,
        }


@dataclass(frozen=True, slots=True)
class OpportunityResult:
    opportunity_id: str
    strategy_id: str
    canonical_symbol: str
    state: OpportunityState
    target_quantity: Decimal
    legs: tuple[LegOutcome, ...]
    residual_quantity: Decimal
    reserved_notional: Decimal
    unwinds: tuple[UnwindPlan, ...]
    reconciliation: OpportunityReconciliation | None
    transitions: tuple[str, ...]

    @property
    def duplicate_free(self) -> bool:
        identifiers = [leg.client_order_id for leg in self.legs]
        return len(set(identifiers)) == len(identifiers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "strategy_id": self.strategy_id,
            "canonical_symbol": self.canonical_symbol,
            "state": self.state.value,
            "target_quantity": format(self.target_quantity, "f"),
            "residual_quantity": format(self.residual_quantity, "f"),
            "reserved_notional": format(self.reserved_notional, "f"),
            "transitions": list(self.transitions),
            "legs": [
                {
                    "leg_index": leg.leg_index,
                    "venue": leg.venue,
                    "symbol": leg.symbol,
                    "side": leg.side.value,
                    "target_quantity": format(leg.target_quantity, "f"),
                    "filled_quantity": format(leg.filled_quantity, "f"),
                    "remaining_quantity": format(
                        leg.remaining_quantity,
                        "f",
                    ),
                    "average_price": (
                        None
                        if leg.average_price is None
                        else format(leg.average_price, "f")
                    ),
                    "state": leg.state.value,
                    "client_order_id": leg.client_order_id,
                    "idempotency_key": leg.idempotency_key,
                    "exchange_order_id": leg.exchange_order_id,
                    "attempts": leg.attempts,
                    "reject_reason": (
                        None
                        if leg.reject_reason is None
                        else leg.reject_reason.value
                    ),
                    "reject_action": (
                        None
                        if leg.reject_action is None
                        else leg.reject_action.value
                    ),
                    "last_error": leg.last_error,
                }
                for leg in self.legs
            ],
            "unwinds": [
                {
                    "venue": plan.venue,
                    "symbol": plan.symbol,
                    "side": plan.side.value,
                    "quantity": format(plan.quantity, "f"),
                    "price": (
                        None
                        if plan.price is None
                        else format(plan.price, "f")
                    ),
                    "reason": plan.reason,
                    "candidates": [
                        {"venue": venue, "cost": cost}
                        for venue, cost in plan.candidates
                    ],
                }
                for plan in self.unwinds
            ],
            "reconciliation": (
                None
                if self.reconciliation is None
                else self.reconciliation.to_dict()
            ),
            "duplicate_free": self.duplicate_free,
        }


class OpportunityRiskLimitError(RuntimeError):
    """Raised when an opportunity cannot reserve the notional it needs."""


class OpportunityCapacityError(RuntimeError):
    """Raised when an opportunity cannot reserve its message budget.

    Distinct from :class:`OpportunityRiskLimitError`: the firm is willing
    to carry the risk, but there is no room to send the orders that would
    establish it. Sending the legs we *can* afford would leave a naked
    position, so nothing is transmitted.
    """


class ResidualExposureError(RuntimeError):
    """Raised when residual exposure cannot be neutralised."""


class SignalHashMismatchError(RuntimeError):
    """Raised when a stored opportunity describes a different signal."""


class ResyncOutcome(str, Enum):
    """Why a resync ended, which is not the same as what it found."""

    ADOPTED = "adopted"
    ABSENT = "absent"
    UNKNOWN = "unknown"


# Venue codes that mean "this order genuinely does not exist" rather than
# "we could not reach the venue". Binance uses -2013/-2011, OKX 51603.
_ABSENT_ORDER_CODES = frozenset({-2013, -2011, 51603, "51603"})


def _is_definitive_absence(error: BaseException) -> bool:
    """Distinguish a venue saying "no such order" from a failed request.

    Only the former makes it safe to submit. Treating a timeout or a 500
    as absence is how a recovery places a duplicate live order.
    """

    code = getattr(error, "code", None)
    if code in _ABSENT_ORDER_CODES:
        return True
    status = getattr(error, "status", None)
    return status == 404


@dataclass(slots=True)
class _Leg:
    """Mutable in-flight state for one venue leg."""

    index: int
    venue: str
    symbol: str
    side: Side
    order_type: OrderType
    target_quantity: Decimal
    reference_price: Decimal
    limit_price: Decimal | None
    source_id: str
    client_order_id: str
    idempotency_key: str
    state: LegState = LegState.PENDING
    exchange_order_id: str | None = None
    filled_quantity: Decimal = Decimal(0)
    average_price: Decimal | None = None
    attempt: int = 0
    reject_reason: RejectReason | None = None
    reject_action: RejectAction | None = None
    last_error: str | None = None
    submitted_ns: int | None = None
    acknowledged_ns: int | None = None
    is_unwind: bool = False

    @property
    def remaining_quantity(self) -> Decimal:
        return max(Decimal(0), self.target_quantity - self.filled_quantity)

    @property
    def signed_filled_quantity(self) -> Decimal:
        if self.side is Side.BUY:
            return self.filled_quantity
        return -self.filled_quantity

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_LEG_STATES


def _now_ns_default() -> int:
    import time

    return time.time_ns()


async def _sleep_default(seconds: float) -> None:
    await asyncio.sleep(seconds)


class OpportunityCoordinator:
    """Persistent, restart-safe orchestration of multi-venue opportunities."""

    def __init__(
        self,
        adapters: Mapping[str, ExchangeAdapter],
        store: OpportunityStore,
        *,
        timeouts: OpportunityTimeouts | None = None,
        residual_policy: ResidualPolicy | None = None,
        max_reserved_notional: Decimal = Decimal("1000000"),
        quantity_tolerance: Decimal = Decimal("0.00000001"),
        control_plane: ExecutionControlPlane | None = None,
        messages_per_leg: int = 3,
        hedge_messages: int = 2,
        now_ns: Callable[[], int] = _now_ns_default,
        sleep: Callable[[float], Awaitable[None]] = _sleep_default,
    ) -> None:
        normalized = {
            venue.lower(): adapter for venue, adapter in adapters.items()
        }
        if len(normalized) < 2:
            raise ValueError("at least two venues are required")
        for venue, adapter in normalized.items():
            if adapter.venue.lower() != venue:
                raise ValueError(f"adapter venue mismatch for {venue}")
        if max_reserved_notional <= 0:
            raise ValueError("risk limit must be positive")
        if quantity_tolerance < 0:
            raise ValueError("quantity tolerance must not be negative")
        if messages_per_leg < 1:
            raise ValueError("each leg needs at least one message")
        if hedge_messages < 0:
            raise ValueError("hedge messages must not be negative")
        self.adapters = normalized
        self.store = store
        self.timeouts = timeouts or OpportunityTimeouts()
        self.residual_policy = residual_policy or ResidualPolicy()
        self.max_reserved_notional = max_reserved_notional
        self.quantity_tolerance = quantity_tolerance
        self.control_plane = control_plane
        self.messages_per_leg = messages_per_leg
        self.hedge_messages = hedge_messages
        self._now_ns = now_ns
        self._sleep = sleep
        self._quotes: dict[tuple[str, str], MarketQuote] = {}
        self._reservations: dict[str, Decimal] = {}
        self._risk_positions: dict[str, dict[str, Decimal]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # quotes and risk
    # ------------------------------------------------------------------

    def update_quote(self, quote: MarketQuote) -> None:
        self._quotes[(quote.venue.lower(), quote.symbol.upper())] = quote

    @property
    def reserved_notional(self) -> Decimal:
        return sum(self._reservations.values(), Decimal(0))

    def risk_positions(self, opportunity_id: str) -> dict[str, Decimal]:
        """Exposure the risk engine believes it has consumed per venue.

        This is accumulated incrementally by :meth:`_consume_risk` as fills
        are observed, deliberately *not* derived from the leg table. A view
        recomputed from the same rows as ``local_positions`` could never
        disagree with it, which would make the comparison meaningless.
        """

        return dict(self._risk_positions.get(opportunity_id, {}))

    def _consume_risk(
        self,
        opportunity_id: str,
        leg: _Leg,
        quantity_delta: Decimal,
    ) -> None:
        if quantity_delta == 0:
            return
        signed = (
            quantity_delta if leg.side is Side.BUY else -quantity_delta
        )
        positions = self._risk_positions.setdefault(opportunity_id, {})
        positions[leg.venue] = positions.get(leg.venue, Decimal(0)) + signed

    def _rebuild_risk_positions(self, opportunity_id: str) -> None:
        """Re-derive the risk view after a restart.

        Recovery has no in-memory history, so the ledger is the only
        surviving source. Re-deriving here is explicit and auditable.
        """

        positions: dict[str, Decimal] = {}
        for leg in self.store.load_legs(opportunity_id):
            signed = (
                leg.filled_quantity
                if leg.side == Side.BUY.value
                else -leg.filled_quantity
            )
            positions[leg.venue] = (
                positions.get(leg.venue, Decimal(0)) + signed
            )
        self._risk_positions[opportunity_id] = positions

    @staticmethod
    def _quote_currency(canonical_symbol: str) -> str:
        symbol = canonical_symbol.upper().strip()
        for separator in ("-", "/", "_"):
            if separator in symbol:
                return symbol.rsplit(separator, 1)[1]
        for suffix in _QUOTE_SUFFIXES:
            if symbol.endswith(suffix) and len(symbol) > len(suffix):
                return suffix
        return "USDT"

    # ------------------------------------------------------------------
    # public entry points
    # ------------------------------------------------------------------

    async def execute(self, request: OpportunityRequest) -> OpportunityResult:
        opportunity_id = self.opportunity_id(request)
        lock = self._locks.setdefault(opportunity_id, asyncio.Lock())
        async with lock:
            existing = self.store.load_opportunity(opportunity_id)
            if existing is not None:
                # The id only hashes strategy, version and signal id, so
                # two different leg sets can collide onto it. Neither
                # resuming nor replaying a stored result is safe when the
                # plan behind it differs from the one we were handed.
                incoming_hash = self.signal_hash(request)
                if existing.signal_hash != incoming_hash:
                    raise SignalHashMismatchError(
                        f"{opportunity_id} was created from signal "
                        f"{existing.signal_hash} but this request hashes "
                        f"to {incoming_hash}; refusing to reuse it"
                    )
            if existing is not None and existing.terminal:
                # Replaying the same signal must never create new orders.
                return self._result_from_store(opportunity_id)
            return await self._run(request, opportunity_id)

    async def recover(self) -> tuple[OpportunityResult, ...]:
        """Resume every non-terminal opportunity after a process restart."""

        results: list[OpportunityResult] = []
        for opportunity_id in self.store.open_opportunity_ids():
            lock = self._locks.setdefault(opportunity_id, asyncio.Lock())
            async with lock:
                results.append(await self._recover_one(opportunity_id))
        return tuple(results)

    def opportunity_id(self, request: OpportunityRequest) -> str:
        digest = hashlib.sha256(
            "|".join(
                (
                    request.strategy_id,
                    request.strategy_version,
                    request.signal_id,
                )
            ).encode()
        ).hexdigest()
        return f"OPP-{digest[:16].upper()}"

    def signal_hash(self, request: OpportunityRequest) -> str:
        payload = "|".join(
            [
                request.strategy_id,
                request.strategy_version,
                request.signal_id,
                request.canonical_symbol.upper(),
            ]
            + [
                ":".join(
                    (
                        leg.venue.lower(),
                        leg.symbol.upper(),
                        leg.side.value,
                        format(leg.quantity, "f"),
                        format(leg.reference_price, "f"),
                        leg.order_type.value,
                        (
                            "-"
                            if leg.limit_price is None
                            else format(leg.limit_price, "f")
                        ),
                    )
                )
                for leg in request.legs
            ]
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    # ------------------------------------------------------------------
    # the state machine
    # ------------------------------------------------------------------

    async def _run(
        self,
        request: OpportunityRequest,
        opportunity_id: str,
    ) -> OpportunityResult:
        transitions: list[str] = []
        record = self._create(request, opportunity_id, transitions)
        legs = self._build_legs(request, opportunity_id)

        try:
            record = self._reserve_risk(record, request, transitions)
        except OpportunityRiskLimitError:
            self._transition(
                record,
                OpportunityState.RECONCILIATION_REQUIRED,
                transitions,
                terminal=True,
                detail="risk reservation refused",
            )
            raise

        # The spec is explicit that message capacity is reserved for every
        # planned message *before* the first leg transmits. Reserving it
        # leg-by-leg would let leg A execute and leg B discover there is no
        # budget left to send it, which is a naked position nobody asked
        # for. Nothing has been transmitted at this point, so a refusal
        # here costs only the opportunity.
        try:
            self._reserve_capacity(record, request, legs)
        except OpportunityCapacityError:
            self._release_risk(
                record.opportunity_id,
                reason="capacity_refused",
            )
            self._transition(
                record,
                OpportunityState.RECONCILIATION_REQUIRED,
                transitions,
                terminal=True,
                detail="message capacity refused",
            )
            raise

        self._create_orders(record, legs, transitions)
        # Unwind legs live at indexes >= _UNWIND_INDEX_BASE and are not
        # described by the request, so a resumed run would neither see
        # their fills nor know their indexes were taken. Adopt them before
        # anything reads the residual.
        legs.extend(self._load_unwind_legs(opportunity_id))
        try:
            await self._dispatch(record, legs, transitions)
            await self._await_fills(record, legs, transitions)
            unwinds = await self._resolve_residual(record, legs, transitions)
            reconciliation = await self._close(record, legs, transitions)
        except BaseException:
            # A reservation held by a failed run would wedge every later
            # opportunity against the aggregate limit. Release it, but keep
            # the ledger open so recovery can still reconcile the legs.
            self._release_risk(
                record.opportunity_id,
                reason="run_failed",
            )
            self._release_capacity(record.opportunity_id)
            raise
        self._release_capacity(record.opportunity_id)
        return self._result(
            record,
            legs,
            unwinds,
            reconciliation,
            transitions,
        )

    def _create(
        self,
        request: OpportunityRequest,
        opportunity_id: str,
        transitions: list[str],
    ) -> OpportunityRecord:
        now = self._now_ns()
        stored = self.store.load_opportunity(opportunity_id)
        if stored is not None:
            # The id only hashes strategy/version/signal, so two different
            # leg sets can map onto the same id. Resuming under those
            # circumstances would execute a plan nobody asked for.
            incoming_hash = self.signal_hash(request)
            if stored.signal_hash != incoming_hash:
                raise SignalHashMismatchError(
                    f"{opportunity_id} was created from signal "
                    f"{stored.signal_hash} but this request hashes to "
                    f"{incoming_hash}; refusing to resume"
                )
            transitions.append(f"resume:{stored.state}")
            self._rebuild_risk_positions(opportunity_id)
            return stored
        record = OpportunityRecord(
            opportunity_id=opportunity_id,
            strategy_id=request.strategy_id,
            strategy_version=request.strategy_version,
            signal_hash=self.signal_hash(request),
            canonical_symbol=request.canonical_symbol.upper(),
            target_quantity=request.target_quantity,
            expected_edge=request.expected_edge,
            state=OpportunityState.CREATED.value,
            created_ns=now,
            updated_ns=now,
            metadata={"signal_id": request.signal_id},
        )
        self.store.save_opportunity(record)
        self.store.append_event(
            opportunity_id,
            "opportunity_created",
            {
                "strategy_id": request.strategy_id,
                "signal_id": request.signal_id,
                "canonical_symbol": record.canonical_symbol,
                "target_quantity": format(record.target_quantity, "f"),
            },
            now,
        )
        transitions.append(OpportunityState.CREATED.value)
        return record

    def _reserve_risk(
        self,
        record: OpportunityRecord,
        request: OpportunityRequest,
        transitions: list[str],
    ) -> OpportunityRecord:
        if record.reservation_id is not None:
            self._reservations[record.opportunity_id] = (
                record.reserved_notional
            )
            return record
        notional = sum(
            (leg.quantity * leg.reference_price for leg in request.legs),
            Decimal(0),
        )
        projected = self.reserved_notional + notional
        if projected > self.max_reserved_notional:
            self.store.append_event(
                record.opportunity_id,
                "risk_reservation_refused",
                {
                    "requested_notional": format(notional, "f"),
                    "reserved_notional": format(self.reserved_notional, "f"),
                    "limit": format(self.max_reserved_notional, "f"),
                },
                self._now_ns(),
            )
            raise OpportunityRiskLimitError(
                f"reserving {notional} would exceed the aggregate limit "
                f"{self.max_reserved_notional}"
            )
        reservation_id = f"RSV-{record.opportunity_id}"
        updated = replace(
            record,
            reservation_id=reservation_id,
            reserved_notional=notional,
            state=OpportunityState.RISK_RESERVED.value,
            updated_ns=self._now_ns(),
        )
        self.store.save_opportunity(updated)
        # Only claim the notional once the claim is durable, otherwise a
        # failed write leaves an in-memory reservation nothing can release.
        self._reservations[record.opportunity_id] = notional
        self.store.append_event(
            record.opportunity_id,
            "risk_reserved",
            {
                "reservation_id": reservation_id,
                "notional": format(notional, "f"),
            },
            updated.updated_ns,
        )
        transitions.append(OpportunityState.RISK_RESERVED.value)
        return updated

    def _build_legs(
        self,
        request: OpportunityRequest,
        opportunity_id: str,
    ) -> list[_Leg]:
        legs: list[_Leg] = []
        for index, leg_request in enumerate(request.legs):
            legs.append(
                self._make_leg(
                    opportunity_id,
                    index,
                    leg_request.venue,
                    leg_request.symbol,
                    leg_request.side,
                    leg_request.order_type,
                    leg_request.quantity,
                    leg_request.reference_price,
                    leg_request.limit_price,
                )
            )
        return legs

    def _load_unwind_legs(self, opportunity_id: str) -> list[_Leg]:
        """Rehydrate unwind legs, which no request ever describes."""

        return [
            self._leg_from_record(record)
            for record in self.store.load_legs(opportunity_id)
            if record.leg_index >= _UNWIND_INDEX_BASE
        ]

    def _make_leg(
        self,
        opportunity_id: str,
        index: int,
        venue: str,
        symbol: str,
        side: Side,
        order_type: OrderType,
        quantity: Decimal,
        reference_price: Decimal,
        limit_price: Decimal | None,
        *,
        is_unwind: bool = False,
    ) -> _Leg:
        source_id = f"{opportunity_id}:{index}"
        adapter = self._adapter(venue)
        probe = Order(
            order_id=uuid5(NAMESPACE_URL, source_id),
            intent_id=uuid5(NAMESPACE_URL, f"{source_id}:intent"),
            client_order_id=source_id,
            symbol=symbol.upper(),
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=limit_price,
            state=OrderState.RISK_APPROVED,
            created_timestamp_ns=0,
            venue=venue.lower(),
        )
        return _Leg(
            index=index,
            venue=venue.lower(),
            symbol=symbol.upper(),
            side=side,
            order_type=order_type,
            target_quantity=quantity,
            reference_price=reference_price,
            limit_price=limit_price,
            source_id=source_id,
            client_order_id=adapter.client_order_id(probe),
            idempotency_key=hashlib.sha256(
                f"{source_id}|{venue.lower()}|{side.value}|"
                f"{format(quantity, 'f')}".encode()
            ).hexdigest(),
            is_unwind=is_unwind,
        )

    def _create_orders(
        self,
        record: OpportunityRecord,
        legs: Sequence[_Leg],
        transitions: list[str],
    ) -> None:
        stored = {leg.leg_index: leg for leg in self.store.load_legs(
            record.opportunity_id
        )}
        for leg in legs:
            persisted = stored.get(leg.index)
            if persisted is not None:
                self._adopt_persisted_leg(leg, persisted)
                continue
            self._persist_leg(record.opportunity_id, leg)
        self._transition(
            record,
            OpportunityState.ORDERS_CREATED,
            transitions,
            detail=f"{len(legs)} legs",
        )

    async def _dispatch(
        self,
        record: OpportunityRecord,
        legs: Sequence[_Leg],
        transitions: list[str],
    ) -> None:
        pending = [leg for leg in legs if not leg.terminal]
        if not pending:
            return
        self._transition(
            record,
            OpportunityState.DISPATCHED,
            transitions,
            detail=f"concurrent:{len(pending)}",
        )
        await self._gather_all(
            *(
                self._dispatch_leg(
                    record.opportunity_id, leg, record.strategy_id
                )
                for leg in pending
            )
        )
        if any(
            leg.state
            in {
                LegState.ACKNOWLEDGED,
                LegState.PARTIALLY_FILLED,
                LegState.FILLED,
            }
            for leg in legs
        ):
            self._transition(
                record,
                OpportunityState.ACKNOWLEDGED,
                transitions,
            )
        if any(leg.state is LegState.REJECTED for leg in legs):
            transitions.append(OpportunityState.REJECTED.value)
        if any(
            leg.state in {LegState.TIMED_OUT, LegState.ABANDONED}
            for leg in legs
        ):
            transitions.append(OpportunityState.TIMEOUT.value)

    async def _gather_all(self, *awaitables: Awaitable[Any]) -> None:
        """Run every awaitable to completion, then raise the first failure.

        Plain ``gather`` propagates the first exception while leaving its
        siblings running unsupervised, which can place a live order after
        the caller has already been told the opportunity failed.
        """

        outcomes = await asyncio.gather(*awaitables, return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome

    async def _dispatch_leg(
        self, opportunity_id: str, leg: _Leg, strategy_id: str = ""
    ) -> None:
        deadline_ns = (
            self._now_ns() + self.timeouts.ack_timeout_ms * 1_000_000
        )
        # A leg we have already contacted a venue about must never be
        # resubmitted blind. Venues such as Binance permit a client order
        # id to be reused once the previous order is no longer live, so a
        # deterministic id alone does not prevent a duplicate. Ask first.
        if leg.submitted_ns is not None or leg.exchange_order_id is not None:
            outcome = await self._resync_outcome(opportunity_id, leg)
            if outcome is ResyncOutcome.ADOPTED:
                if leg.terminal or leg.state in {
                    LegState.ACKNOWLEDGED,
                    LegState.PARTIALLY_FILLED,
                }:
                    return
            elif outcome is ResyncOutcome.UNKNOWN:
                # We could not establish whether an order is live. Guessing
                # either way is unsafe, so stop and let recovery ask again
                # once the venue is reachable.
                leg.state = LegState.AMBIGUOUS
                leg.last_error = "resync inconclusive; not resubmitting"
                self._persist_leg(opportunity_id, leg)
                return
        else:
            # Only a first transmission is gated. Re-admitting a leg the
            # venue has already seen could strand a live order behind a
            # control that has since bound.
            if not self._admit_leg(opportunity_id, strategy_id, leg):
                return
        while leg.attempt < self.timeouts.max_submit_attempts:
            leg.attempt += 1
            leg.state = LegState.SUBMITTING
            leg.submitted_ns = self._now_ns()
            # Write ahead: the intent to submit is durable before the call.
            self._persist_leg(opportunity_id, leg)
            self.store.append_event(
                opportunity_id,
                "leg_submitting",
                {
                    "leg_index": leg.index,
                    "venue": leg.venue,
                    "client_order_id": leg.client_order_id,
                    "idempotency_key": leg.idempotency_key,
                    "attempt": leg.attempt,
                },
                leg.submitted_ns,
            )
            try:
                exchange_order = await asyncio.wait_for(
                    self._adapter(leg.venue).submit_order(
                        self._order_for(leg)
                    ),
                    timeout=self.timeouts.ack_timeout_ms / 1000,
                )
            except Exception as error:  # noqa: BLE001 - classified below
                if await self._handle_dispatch_error(
                    opportunity_id,
                    leg,
                    error,
                    deadline_ns,
                ):
                    continue
                return
            self._apply_exchange_order(opportunity_id, leg, exchange_order)
            leg.acknowledged_ns = self._now_ns()
            self._persist_leg(opportunity_id, leg)
            self.store.append_event(
                opportunity_id,
                "leg_acknowledged",
                {
                    "leg_index": leg.index,
                    "venue": leg.venue,
                    "exchange_order_id": leg.exchange_order_id,
                    "state": leg.state.value,
                },
                leg.acknowledged_ns,
            )
            return
        self._expire_leg(opportunity_id, leg, "submit attempts exhausted")

    def _expire_leg(
        self,
        opportunity_id: str,
        leg: _Leg,
        reason: str,
    ) -> None:
        """Retire a leg that ran out of time.

        ``ABANDONED`` is used when we hold positive evidence that no order
        exists at the venue (we never received an exchange order id and a
        resync found nothing), which makes the leg terminal with zero
        exposure. ``TIMED_OUT`` is reserved for the genuinely uncertain
        case where an order may still be live and must be cancelled.
        """

        leg.state = (
            LegState.ABANDONED
            if leg.exchange_order_id is None
            else LegState.TIMED_OUT
        )
        leg.last_error = reason
        self._persist_leg(opportunity_id, leg)
        self.store.append_event(
            opportunity_id,
            "leg_expired",
            {
                "leg_index": leg.index,
                "venue": leg.venue,
                "state": leg.state.value,
                "reason": reason,
            },
            self._now_ns(),
        )

    async def _handle_dispatch_error(
        self,
        opportunity_id: str,
        leg: _Leg,
        error: BaseException,
        deadline_ns: int,
    ) -> bool:
        """Returns ``True`` when the caller should retry the same leg."""

        directive = classify_retry(error)
        leg.last_error = str(error)
        self.store.append_event(
            opportunity_id,
            "leg_dispatch_error",
            {
                "leg_index": leg.index,
                "venue": leg.venue,
                "attempt": leg.attempt,
                "directive": directive.value,
                "error": str(error),
            },
            self._now_ns(),
        )

        if isinstance(error, ExchangeAPIError) and directive is (
            RetryDirective.NO_RETRY
        ):
            reason = classify_reject(error.message)
            leg.reject_reason = reason
            leg.reject_action = action_for_reject(reason)
            if reason is RejectReason.DUPLICATE_ORDER:
                # The exchange already has our order: adopt, never resend.
                adopted = await self._resync_leg(opportunity_id, leg)
                if adopted:
                    return False
            leg.state = LegState.REJECTED
            self._persist_leg(opportunity_id, leg)
            return False

        if directive in {
            RetryDirective.QUERY_THEN_RETRY,
            RetryDirective.RECONNECT_AND_RESYNC,
        }:
            # Ambiguous: the order may or may not have reached the venue.
            leg.state = LegState.AMBIGUOUS
            self._persist_leg(opportunity_id, leg)
            if await self._resync_leg(opportunity_id, leg):
                return False
            if leg.attempt >= self.timeouts.max_submit_attempts:
                self._expire_leg(opportunity_id, leg, str(error))
                return False
            return True

        if directive in {
            RetryDirective.BACKOFF_RETRY,
            RetryDirective.DELAY_RETRY,
        }:
            if leg.attempt >= self.timeouts.max_submit_attempts:
                self._expire_leg(opportunity_id, leg, str(error))
                return False
            backoff = self.timeouts.backoff_base_ms * (2 ** (leg.attempt - 1))
            if directive is RetryDirective.DELAY_RETRY:
                backoff = max(backoff, self.timeouts.backoff_base_ms)
            await self._sleep(backoff / 1000)
            if self._now_ns() > deadline_ns:
                self._expire_leg(opportunity_id, leg, "ack deadline exceeded")
                return False
            return True

        reason = classify_reject(str(error))
        leg.reject_reason = reason
        leg.reject_action = action_for_reject(reason)
        leg.state = LegState.REJECTED
        self._persist_leg(opportunity_id, leg)
        return False

    async def _resync_leg(self, opportunity_id: str, leg: _Leg) -> bool:
        """Query the venue for our client order id and adopt what exists.

        This is the single mechanism behind late-ack handling, reconnect
        recovery and duplicate suppression: we never resend blindly, we
        ask the venue whether our deterministic identifier already lives
        there.
        """

        return await self._resync_outcome(opportunity_id, leg) is (
            ResyncOutcome.ADOPTED
        )

    async def _resync_outcome(
        self,
        opportunity_id: str,
        leg: _Leg,
    ) -> ResyncOutcome:
        """Resync, distinguishing a real absence from a failed question.

        "The venue says this order does not exist" and "we could not ask
        the venue" look identical at the call site unless they are kept
        apart, and confusing them is how a resubmission becomes a second
        live order.
        """

        try:
            exchange_order = await self._adapter(leg.venue).query_order(
                leg.symbol,
                client_order_id=leg.client_order_id,
            )
        except Exception as error:  # noqa: BLE001 - classified below
            definitive = _is_definitive_absence(error)
            self.store.append_event(
                opportunity_id,
                "leg_resync_absent" if definitive else "leg_resync_failed",
                {
                    "leg_index": leg.index,
                    "venue": leg.venue,
                    "client_order_id": leg.client_order_id,
                    "error": str(error),
                    "definitive": definitive,
                },
                self._now_ns(),
            )
            return (
                ResyncOutcome.ABSENT if definitive else ResyncOutcome.UNKNOWN
            )
        self._apply_exchange_order(opportunity_id, leg, exchange_order)
        self._persist_leg(opportunity_id, leg)
        self.store.append_event(
            opportunity_id,
            "leg_resync_adopted",
            {
                "leg_index": leg.index,
                "venue": leg.venue,
                "client_order_id": leg.client_order_id,
                "exchange_order_id": leg.exchange_order_id,
                "state": leg.state.value,
            },
            self._now_ns(),
        )
        return ResyncOutcome.ADOPTED

    async def _await_fills(
        self,
        record: OpportunityRecord,
        legs: Sequence[_Leg],
        transitions: list[str],
    ) -> None:
        start_ns = self._now_ns()
        first_fill_ns = (
            start_ns + self.timeouts.first_fill_timeout_ms * 1_000_000
        )
        complete_ns = (
            start_ns + self.timeouts.complete_fill_timeout_ms * 1_000_000
        )
        first_fill_enforced = False
        while True:
            outstanding = [leg for leg in legs if not leg.terminal]
            if not outstanding:
                break
            now = self._now_ns()
            if now >= complete_ns:
                await self._cancel_outstanding(
                    record.opportunity_id,
                    outstanding,
                    reason="complete_fill_timeout",
                )
                transitions.append(OpportunityState.TIMEOUT.value)
                break
            if (
                not first_fill_enforced
                and now >= first_fill_ns
                and all(
                    leg.filled_quantity == 0 for leg in outstanding
                )
            ):
                first_fill_enforced = True
                await self._cancel_outstanding(
                    record.opportunity_id,
                    outstanding,
                    reason="first_fill_timeout",
                )
                transitions.append(OpportunityState.TIMEOUT.value)
                break
            await self._gather_all(
                *(
                    self._poll_leg(record.opportunity_id, leg)
                    for leg in outstanding
                )
            )
            if all(leg.terminal for leg in legs):
                break
            await self._sleep(self.timeouts.poll_interval_ms / 1000)

        await self._collect_trades(record.opportunity_id, legs)
        filled = sum(
            (leg.filled_quantity for leg in legs),
            Decimal(0),
        )
        target = sum((leg.target_quantity for leg in legs), Decimal(0))
        if filled >= target - self.quantity_tolerance:
            self._transition(
                record,
                OpportunityState.FULLY_FILLED,
                transitions,
            )
        elif filled > 0:
            self._transition(
                record,
                OpportunityState.PARTIALLY_FILLED,
                transitions,
            )

    async def _poll_leg(self, opportunity_id: str, leg: _Leg) -> None:
        if leg.terminal:
            return
        try:
            exchange_order = await self._adapter(leg.venue).query_order(
                leg.symbol,
                exchange_order_id=leg.exchange_order_id,
                client_order_id=leg.client_order_id,
            )
        except Exception as error:  # noqa: BLE001 - transient poll failure
            leg.last_error = str(error)
            self.store.append_event(
                opportunity_id,
                "leg_poll_error",
                {
                    "leg_index": leg.index,
                    "venue": leg.venue,
                    "error": str(error),
                },
                self._now_ns(),
            )
            return
        previous = leg.filled_quantity
        self._apply_exchange_order(opportunity_id, leg, exchange_order)
        if leg.filled_quantity != previous:
            self.store.append_event(
                opportunity_id,
                "leg_fill_progress",
                {
                    "leg_index": leg.index,
                    "venue": leg.venue,
                    "filled_quantity": format(leg.filled_quantity, "f"),
                    "state": leg.state.value,
                },
                self._now_ns(),
            )
        self._persist_leg(opportunity_id, leg)

    async def _cancel_outstanding(
        self,
        opportunity_id: str,
        legs: Sequence[_Leg],
        *,
        reason: str,
    ) -> None:
        async def cancel(leg: _Leg) -> None:
            if leg.terminal:
                return
            try:
                exchange_order = await self._adapter(leg.venue).cancel_order(
                    leg.symbol,
                    exchange_order_id=leg.exchange_order_id,
                    client_order_id=leg.client_order_id,
                )
            except Exception as error:  # noqa: BLE001 - resolve by query
                leg.last_error = str(error)
                if await self._resync_leg(opportunity_id, leg):
                    return
                # A cancel that fails on a leg we never got an id for is
                # evidence the order never landed, not an open question.
                leg.state = (
                    LegState.ABANDONED
                    if leg.exchange_order_id is None
                    else LegState.AMBIGUOUS
                )
                self._persist_leg(opportunity_id, leg)
                return
            self._apply_exchange_order(opportunity_id, leg, exchange_order)
            if leg.state not in TERMINAL_LEG_STATES:
                leg.state = LegState.CANCELED
            self._persist_leg(opportunity_id, leg)
            self.store.append_event(
                opportunity_id,
                "leg_canceled",
                {
                    "leg_index": leg.index,
                    "venue": leg.venue,
                    "reason": reason,
                    "filled_quantity": format(leg.filled_quantity, "f"),
                },
                self._now_ns(),
            )

        await self._gather_all(*(cancel(leg) for leg in legs))

    async def _collect_trades(
        self,
        opportunity_id: str,
        legs: Sequence[_Leg],
    ) -> None:
        """Pull executions so fees and slippage are attributable."""

        by_venue: dict[str, list[_Leg]] = {}
        for leg in legs:
            if leg.filled_quantity > 0:
                by_venue.setdefault(leg.venue, []).append(leg)

        async def pull(venue: str, venue_legs: list[_Leg]) -> None:
            symbols = {leg.symbol for leg in venue_legs}
            for symbol in sorted(symbols):
                try:
                    trades = await self._adapter(venue).trades(symbol)
                except Exception as error:  # noqa: BLE001 - optional detail
                    self.store.append_event(
                        opportunity_id,
                        "trade_pull_error",
                        {"venue": venue, "error": str(error)},
                        self._now_ns(),
                    )
                    return
                self._record_trades(opportunity_id, venue_legs, trades)

        await self._gather_all(
            *(pull(venue, items) for venue, items in sorted(by_venue.items()))
        )

    def _record_trades(
        self,
        opportunity_id: str,
        legs: Sequence[_Leg],
        trades: Sequence[ExchangeTrade],
    ) -> None:
        by_order_id = {
            leg.exchange_order_id: leg
            for leg in legs
            if leg.exchange_order_id is not None
        }
        for trade in trades:
            leg = by_order_id.get(trade.exchange_order_id)
            if leg is None:
                continue
            self.store.record_fill(
                FillRecord(
                    opportunity_id=opportunity_id,
                    leg_index=leg.index,
                    venue=leg.venue,
                    trade_id=trade.trade_id,
                    exchange_order_id=trade.exchange_order_id,
                    side=trade.side,
                    price=trade.price,
                    quantity=trade.quantity,
                    timestamp_ms=trade.timestamp_ms,
                    fee=trade.fee or Decimal(0),
                    fee_currency=trade.fee_currency,
                )
            )

    # ------------------------------------------------------------------
    # residual handling
    # ------------------------------------------------------------------

    def residual_quantity(self, legs: Sequence[_Leg]) -> Decimal:
        return sum(
            (leg.signed_filled_quantity for leg in legs),
            Decimal(0),
        )

    async def _resolve_residual(
        self,
        record: OpportunityRecord,
        legs: list[_Leg],
        transitions: list[str],
    ) -> tuple[UnwindPlan, ...]:
        residual = self.residual_quantity(legs)
        if abs(residual) <= self.quantity_tolerance:
            return ()
        self._transition(
            record,
            OpportunityState.HEDGE_REQUIRED,
            transitions,
            detail=f"residual={format(residual, 'f')}",
        )
        self.store.append_event(
            record.opportunity_id,
            "hedge_required",
            {
                "residual_quantity": format(residual, "f"),
                "hedge_side": (
                    Side.SELL.value if residual > 0 else Side.BUY.value
                ),
                "hedge_quantity": format(abs(residual), "f"),
            },
            self._now_ns(),
        )
        self._transition(record, OpportunityState.UNWINDING, transitions)
        plans: list[UnwindPlan] = []
        attempt = 0
        # Continue the persisted unwind sequence rather than restarting at
        # 1001, or recovery mints a second leg with an index that already
        # exists and the same fill is counted twice.
        next_index = max(
            (leg.index for leg in legs if leg.index >= _UNWIND_INDEX_BASE),
            default=_UNWIND_INDEX_BASE,
        )
        while (
            abs(residual) > self.quantity_tolerance
            and attempt < self.residual_policy.max_unwind_attempts
        ):
            attempt += 1
            try:
                plan = self.plan_unwind(
                    record.canonical_symbol,
                    residual,
                    legs,
                )
            except ResidualExposureError as error:
                # No venue can flatten us. Never raise into the caller:
                # fall through so reconciliation flags the open exposure.
                self.store.append_event(
                    record.opportunity_id,
                    "unwind_unavailable",
                    {
                        "residual_quantity": format(residual, "f"),
                        "error": str(error),
                    },
                    self._now_ns(),
                )
                break
            plans.append(plan)
            self.store.append_event(
                record.opportunity_id,
                "unwind_planned",
                {
                    "attempt": attempt,
                    "venue": plan.venue,
                    "side": plan.side.value,
                    "quantity": format(plan.quantity, "f"),
                    "price": (
                        None if plan.price is None else format(plan.price, "f")
                    ),
                    "reason": plan.reason,
                    "candidates": [
                        {"venue": venue, "cost": cost}
                        for venue, cost in plan.candidates
                    ],
                },
                self._now_ns(),
            )
            unwind_leg = self._make_leg(
                record.opportunity_id,
                next_index + 1,
                plan.venue,
                plan.symbol,
                plan.side,
                (
                    OrderType.MARKET
                    if plan.price is None
                    else OrderType.LIMIT
                ),
                plan.quantity,
                plan.price or self._reference_for(plan.venue, plan.symbol),
                plan.price,
                is_unwind=True,
            )
            legs.append(unwind_leg)
            next_index += 1
            self._persist_leg(record.opportunity_id, unwind_leg)
            await self._dispatch_leg(
                record.opportunity_id, unwind_leg, record.strategy_id
            )
            await self._drain_leg(record.opportunity_id, unwind_leg)
            residual = self.residual_quantity(legs)
        if abs(residual) > self.quantity_tolerance:
            self.store.append_event(
                record.opportunity_id,
                "unwind_incomplete",
                {"residual_quantity": format(residual, "f")},
                self._now_ns(),
            )
        return tuple(plans)

    async def _drain_leg(self, opportunity_id: str, leg: _Leg) -> None:
        deadline = (
            self._now_ns()
            + self.residual_policy.max_hedge_delay_ms * 1_000_000
        )
        while not leg.terminal and self._now_ns() < deadline:
            await self._poll_leg(opportunity_id, leg)
            if leg.terminal:
                break
            await self._sleep(self.timeouts.poll_interval_ms / 1000)
        if not leg.terminal:
            await self._cancel_outstanding(
                opportunity_id,
                [leg],
                reason="hedge_delay_exceeded",
            )
        await self._collect_trades(opportunity_id, [leg])

    def plan_unwind(
        self,
        canonical_symbol: str,
        residual: Decimal,
        legs: Sequence[_Leg],
    ) -> UnwindPlan:
        """Deterministically pick where and at what price to flatten.

        Venues are ranked by an explicit tuple so identical inputs always
        yield an identical plan:

        1. position in ``ResidualPolicy.unwind_venue_priority``,
        2. all-in execution cost (top of book adjusted for fees),
        3. venue name, as a final tie-break.
        """

        side = Side.SELL if residual > 0 else Side.BUY
        quantity = abs(residual)
        symbol_by_venue = {
            leg.venue: leg.symbol for leg in legs if not leg.is_unwind
        }
        priority = tuple(
            venue.lower()
            for venue in self.residual_policy.unwind_venue_priority
        )
        scored: list[tuple[int, Decimal, str, str, Decimal | None]] = []
        for venue in sorted(self.adapters):
            symbol = symbol_by_venue.get(venue, canonical_symbol.upper())
            quote = self._quotes.get((venue, symbol.upper()))
            if quote is None:
                continue
            fee = quote.fee_bps / Decimal(10_000)
            if side is Side.BUY:
                raw = quote.ask_price
                cost = raw * (Decimal(1) + fee)
            else:
                raw = quote.bid_price
                cost = -(raw * (Decimal(1) - fee))
            rank = priority.index(venue) if venue in priority else len(priority)
            scored.append((rank, cost, venue, symbol.upper(), raw))
        if not scored:
            raise ResidualExposureError(
                f"no venue can unwind {quantity} of {canonical_symbol}"
            )
        scored.sort(key=lambda item: (item[0], item[1], item[2]))
        _, _, venue, symbol, reference = scored[0]
        price: Decimal | None = None
        if (
            self.residual_policy.unwind_price_mode
            is UnwindPriceMode.AGGRESSIVE_LIMIT
            and reference is not None
        ):
            aggression = self.residual_policy.unwind_aggression_bps / Decimal(
                10_000
            )
            if side is Side.BUY:
                price = reference * (Decimal(1) + aggression)
            else:
                price = reference * (Decimal(1) - aggression)
        return UnwindPlan(
            venue=venue,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            reason="residual_exposure",
            candidates=tuple(
                (item[2], format(item[1], "f")) for item in scored
            ),
        )

    # ------------------------------------------------------------------
    # closure and reconciliation
    # ------------------------------------------------------------------

    async def _close(
        self,
        record: OpportunityRecord,
        legs: Sequence[_Leg],
        transitions: list[str],
    ) -> OpportunityReconciliation:
        self._transition(record, OpportunityState.RECONCILING, transitions)
        reconciliation = await self.reconcile(record.opportunity_id, legs)
        if reconciliation.positions_reconcile:
            # Release first so the persisted view reports the final truth.
            self._release_risk(record.opportunity_id)
            reconciliation = replace(reconciliation, risk_released=True)
            self._transition(
                record,
                OpportunityState.CLOSED,
                transitions,
                terminal=True,
            )
        else:
            # The opportunity is terminal either way, so recovery will
            # never revisit it. Holding the reservation would wedge every
            # later opportunity against the aggregate limit forever; the
            # exposure itself remains recorded in the position tables.
            self._release_risk(
                record.opportunity_id,
                reason="reconciliation_required",
            )
            reconciliation = replace(reconciliation, risk_released=True)
            self._transition(
                record,
                OpportunityState.RECONCILIATION_REQUIRED,
                transitions,
                terminal=True,
                detail=";".join(reconciliation.discrepancies),
            )
        self.store.append_event(
            record.opportunity_id,
            "reconciliation",
            reconciliation.to_dict(),
            self._now_ns(),
        )
        return reconciliation

    async def reconcile(
        self,
        opportunity_id: str,
        legs: Sequence[_Leg] | None = None,
    ) -> OpportunityReconciliation:
        """Compare the four independent views of the same position."""

        record = self.store.load_opportunity(opportunity_id)
        leg_records = self.store.load_legs(opportunity_id)
        fills = self.store.load_fills(opportunity_id)

        local_positions: dict[str, Decimal] = {}
        for leg in leg_records:
            signed = (
                leg.filled_quantity
                if leg.side == Side.BUY.value
                else -leg.filled_quantity
            )
            local_positions[leg.venue] = (
                local_positions.get(leg.venue, Decimal(0)) + signed
            )

        ledger_position = sum(
            (
                fill.quantity
                if fill.side.lower() == Side.BUY.value
                else -fill.quantity
                for fill in fills
            ),
            Decimal(0),
        )

        exchange_positions: dict[str, Decimal] = {}
        open_order_count = 0
        discrepancies: list[str] = []
        for venue in sorted({leg.venue for leg in leg_records}):
            venue_legs = [leg for leg in leg_records if leg.venue == venue]
            adapter = self.adapters.get(venue)
            if adapter is None:
                discrepancies.append(f"{venue}: adapter unavailable")
                continue
            observed = Decimal(0)
            for leg in venue_legs:
                try:
                    exchange_order = await adapter.query_order(
                        leg.symbol,
                        exchange_order_id=leg.exchange_order_id,
                        client_order_id=leg.client_order_id,
                    )
                except Exception as error:  # noqa: BLE001 - recorded below
                    if leg.exchange_order_id is not None:
                        discrepancies.append(
                            f"{venue} leg {leg.leg_index}: "
                            f"unqueryable ({error})"
                        )
                    continue
                signed = (
                    exchange_order.filled_quantity
                    if leg.side == Side.BUY.value
                    else -exchange_order.filled_quantity
                )
                observed += signed
                if exchange_order.status.lower() in _LIVE_STATUSES:
                    open_order_count += 1
            exchange_positions[venue] = observed

        risk_positions = self.risk_positions(opportunity_id)
        if not risk_positions and opportunity_id not in self._risk_positions:
            # Reconciling from a cold process: re-derive once so the
            # comparison below is against something rather than nothing.
            self._rebuild_risk_positions(opportunity_id)
            risk_positions = self.risk_positions(opportunity_id)

        for venue, local in sorted(local_positions.items()):
            observed = exchange_positions.get(venue)
            if observed is None:
                continue
            if abs(observed - local) > self.quantity_tolerance:
                discrepancies.append(
                    f"{venue}: exchange {format(observed, 'f')} != "
                    f"local {format(local, 'f')}"
                )
        for venue in sorted(set(local_positions) | set(risk_positions)):
            local = local_positions.get(venue, Decimal(0))
            reserved = risk_positions.get(venue, Decimal(0))
            if abs(reserved - local) > self.quantity_tolerance:
                discrepancies.append(
                    f"{venue}: risk {format(reserved, 'f')} != "
                    f"local {format(local, 'f')}"
                )
        net_position = sum(local_positions.values(), Decimal(0))
        if abs(net_position) > self.quantity_tolerance:
            discrepancies.append(
                f"net position {format(net_position, 'f')} is not flat"
            )
        if open_order_count:
            discrepancies.append(
                f"{open_order_count} order(s) still live on exchange"
            )
        if fills and abs(ledger_position - net_position) > (
            self.quantity_tolerance
        ):
            discrepancies.append(
                f"ledger {format(ledger_position, 'f')} != "
                f"local {format(net_position, 'f')}"
            )

        gross_notional = Decimal(0)
        realized = Decimal(0)
        hedge_cost = Decimal(0)
        slippage = Decimal(0)
        reference_by_index = {
            leg.index: leg.reference_price for leg in (legs or ())
        }
        for leg_record in leg_records:
            # After a restart the in-memory legs are gone, so fall back to
            # the price we persisted at creation time. Without it slippage
            # would be measured against the limit price, or not at all.
            if (
                leg_record.leg_index not in reference_by_index
                and leg_record.reference_price is not None
            ):
                reference_by_index[leg_record.leg_index] = (
                    leg_record.reference_price
                )
        unwind_indexes = {
            leg.index for leg in (legs or ()) if leg.is_unwind
        }
        for leg in leg_records:
            if leg.average_price is None or leg.filled_quantity == 0:
                continue
            notional = leg.average_price * leg.filled_quantity
            gross_notional += notional
            if leg.side == Side.BUY.value:
                realized -= notional
            else:
                realized += notional
            if leg.leg_index in unwind_indexes:
                hedge_cost += notional if leg.side == Side.BUY.value else (
                    -notional
                )
            reference = reference_by_index.get(leg.leg_index)
            if reference is not None:
                delta = (leg.average_price - reference) * leg.filled_quantity
                slippage += delta if leg.side == Side.BUY.value else -delta
        quote_currency = self._quote_currency(
            record.canonical_symbol if record is not None else ""
        )
        fees_by_currency: dict[str, Decimal] = {}
        for fill in fills:
            # Venues report fees in different assets, and some report a
            # charge as a negative number. Summing them raw would produce a
            # meaningless total, so keep them separated by currency.
            currency = (fill.fee_currency or quote_currency).upper()
            fees_by_currency[currency] = (
                fees_by_currency.get(currency, Decimal(0)) + abs(fill.fee)
            )
        fees = fees_by_currency.get(quote_currency, Decimal(0))
        unconverted = {
            currency: amount
            for currency, amount in fees_by_currency.items()
            if currency != quote_currency and amount != 0
        }

        return OpportunityReconciliation(
            opportunity_id=opportunity_id,
            exchange_positions=exchange_positions,
            local_positions=local_positions,
            risk_positions=risk_positions,
            ledger_position=ledger_position,
            net_position=net_position,
            open_order_count=open_order_count,
            risk_released=opportunity_id not in self._reservations,
            gross_notional=gross_notional,
            fees=fees,
            fees_by_currency=dict(sorted(fees_by_currency.items())),
            unconverted_fees=dict(sorted(unconverted.items())),
            quote_currency=quote_currency,
            realized_pnl=realized - fees,
            hedge_cost=hedge_cost,
            slippage=slippage,
            discrepancies=tuple(discrepancies),
        )

    # ------------------------------------------------------------------
    # restart recovery
    # ------------------------------------------------------------------

    async def _recover_one(self, opportunity_id: str) -> OpportunityResult:
        record = self.store.load_opportunity(opportunity_id)
        if record is None:
            raise ResidualExposureError(
                f"opportunity {opportunity_id} vanished from the store"
            )
        transitions = [f"recover:{record.state}"]
        self._rebuild_risk_positions(opportunity_id)
        if record.reservation_id is not None:
            self._reservations.setdefault(
                opportunity_id,
                record.reserved_notional,
            )
        legs = [
            self._leg_from_record(record_leg)
            for record_leg in self.store.load_legs(opportunity_id)
        ]
        self.store.append_event(
            opportunity_id,
            "recovery_started",
            {
                "state": record.state,
                "leg_count": len(legs),
            },
            self._now_ns(),
        )
        # Ask both venues what actually happened before doing anything.
        for leg in legs:
            await self._resync_leg(opportunity_id, leg)
        await self._collect_trades(opportunity_id, legs)
        await self._cancel_outstanding(
            opportunity_id,
            [leg for leg in legs if not leg.terminal],
            reason="recovery",
        )
        unwinds = await self._resolve_residual(record, legs, transitions)
        reconciliation = await self._close(record, legs, transitions)
        return self._result(
            record,
            legs,
            unwinds,
            reconciliation,
            transitions,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _adapter(self, venue: str) -> ExchangeAdapter:
        adapter = self.adapters.get(venue.lower())
        if adapter is None:
            raise ResidualExposureError(f"no adapter configured for {venue}")
        return adapter

    def _reference_for(self, venue: str, symbol: str) -> Decimal:
        quote = self._quotes.get((venue.lower(), symbol.upper()))
        if quote is None:
            raise ResidualExposureError(
                f"no quote available for {venue} {symbol}"
            )
        return (quote.bid_price + quote.ask_price) / Decimal(2)

    def _order_for(self, leg: _Leg) -> Order:
        return Order(
            order_id=uuid5(NAMESPACE_URL, leg.source_id),
            intent_id=uuid5(NAMESPACE_URL, f"{leg.source_id}:intent"),
            client_order_id=leg.source_id,
            symbol=leg.symbol,
            side=leg.side,
            order_type=leg.order_type,
            quantity=leg.target_quantity,
            price=leg.limit_price,
            state=OrderState.RISK_APPROVED,
            created_timestamp_ns=leg.submitted_ns or self._now_ns(),
            venue=leg.venue,
        )

    def _apply_exchange_order(
        self,
        opportunity_id: str,
        leg: _Leg,
        exchange_order: ExchangeOrder,
    ) -> None:
        if exchange_order.client_order_id and (
            exchange_order.client_order_id != leg.client_order_id
        ):
            self.store.append_event(
                opportunity_id,
                "leg_identity_mismatch",
                {
                    "leg_index": leg.index,
                    "expected": leg.client_order_id,
                    "observed": exchange_order.client_order_id,
                },
                self._now_ns(),
            )
            leg.state = LegState.AMBIGUOUS
            leg.last_error = "client order id mismatch"
            return
        leg.exchange_order_id = (
            exchange_order.exchange_order_id or leg.exchange_order_id
        )
        # Filled quantity is monotonic: a stale poll can never rewind it.
        previous_filled = leg.filled_quantity
        leg.filled_quantity = max(
            leg.filled_quantity,
            exchange_order.filled_quantity,
        )
        self._consume_risk(
            opportunity_id,
            leg,
            leg.filled_quantity - previous_filled,
        )
        if exchange_order.average_fill_price is not None:
            leg.average_price = exchange_order.average_fill_price
        status = exchange_order.status.lower()
        if status in _FILLED_STATUSES:
            leg.state = LegState.FILLED
        elif status in _CANCELED_STATUSES:
            leg.state = LegState.CANCELED
        elif status in _REJECTED_STATUSES:
            leg.state = LegState.REJECTED
            if leg.reject_reason is None:
                leg.reject_reason = RejectReason.UNKNOWN
                leg.reject_action = action_for_reject(RejectReason.UNKNOWN)
        elif leg.filled_quantity > 0:
            leg.state = LegState.PARTIALLY_FILLED
        else:
            leg.state = LegState.ACKNOWLEDGED

    def _persist_leg(self, opportunity_id: str, leg: _Leg) -> None:
        self.store.save_leg(
            LegRecord(
                opportunity_id=opportunity_id,
                leg_index=leg.index,
                venue=leg.venue,
                symbol=leg.symbol,
                side=leg.side.value,
                order_type=leg.order_type.value,
                target_quantity=leg.target_quantity,
                client_order_id=leg.client_order_id,
                idempotency_key=leg.idempotency_key,
                state=leg.state.value,
                updated_ns=self._now_ns(),
                limit_price=leg.limit_price,
                reference_price=leg.reference_price,
                exchange_order_id=leg.exchange_order_id,
                filled_quantity=leg.filled_quantity,
                average_price=leg.average_price,
                reject_reason=(
                    None
                    if leg.reject_reason is None
                    else leg.reject_reason.value
                ),
                reject_action=(
                    None
                    if leg.reject_action is None
                    else leg.reject_action.value
                ),
                attempt=leg.attempt,
                last_error=leg.last_error,
                submitted_ns=leg.submitted_ns,
                acknowledged_ns=leg.acknowledged_ns,
            )
        )

    def _adopt_persisted_leg(self, leg: _Leg, record: LegRecord) -> None:
        if record.client_order_id != leg.client_order_id:
            raise ResidualExposureError(
                f"persisted client order id {record.client_order_id} does "
                f"not match derived {leg.client_order_id}"
            )
        leg.state = LegState(record.state)
        leg.exchange_order_id = record.exchange_order_id
        leg.filled_quantity = record.filled_quantity
        leg.average_price = record.average_price
        leg.attempt = record.attempt
        leg.last_error = record.last_error
        leg.submitted_ns = record.submitted_ns
        leg.acknowledged_ns = record.acknowledged_ns
        if record.reject_reason is not None:
            leg.reject_reason = RejectReason(record.reject_reason)
        if record.reject_action is not None:
            leg.reject_action = RejectAction(record.reject_action)

    def _leg_from_record(self, record: LegRecord) -> _Leg:
        leg = _Leg(
            index=record.leg_index,
            venue=record.venue,
            symbol=record.symbol,
            side=Side(record.side),
            order_type=OrderType(record.order_type),
            target_quantity=record.target_quantity,
            reference_price=(
                record.reference_price
                or record.limit_price
                or Decimal(1)
            ),
            limit_price=record.limit_price,
            source_id=f"{record.opportunity_id}:{record.leg_index}",
            client_order_id=record.client_order_id,
            idempotency_key=record.idempotency_key,
            is_unwind=record.leg_index >= _UNWIND_INDEX_BASE,
        )
        self._adopt_persisted_leg(leg, record)
        return leg

    def _release_risk(
        self,
        opportunity_id: str,
        *,
        reason: str = "closed",
    ) -> None:
        if self._reservations.pop(opportunity_id, None) is not None:
            self.store.append_event(
                opportunity_id,
                "risk_released",
                {
                    "reservation_id": f"RSV-{opportunity_id}",
                    "reason": reason,
                },
                self._now_ns(),
            )

    # ------------------------------------------------------------------
    # message capacity
    # ------------------------------------------------------------------

    def _reserve_capacity(
        self,
        record: OpportunityRecord,
        request: OpportunityRequest,
        legs: Sequence[_Leg],
    ) -> None:
        """Take message budget for every planned message, atomically."""

        if self.control_plane is None:
            return
        venue_messages: dict[str, int] = {}
        for leg in legs:
            if leg.terminal:
                continue
            venue = normalise_scope(leg.venue)
            venue_messages[venue] = (
                venue_messages.get(venue, 0) + self.messages_per_leg
            )
        if not venue_messages:
            return
        try:
            self.control_plane.ledger.reserve(
                ReservationRequest(
                    opportunity_id=record.opportunity_id,
                    strategy_id=request.strategy_id,
                    venue_messages=venue_messages,
                    notional=record.reserved_notional,
                    hedge_messages=self.hedge_messages,
                    priority=Priority.MARKET_MAKING,
                )
            )
        except (CapacityReservationError, ValueError) as error:
            # `ReservationRequest.__post_init__` raises `ValueError` for
            # a malformed request, and it is constructed inside this
            # `try`. Catching only the capacity error would let that
            # escape past the audit event, the structured log and the
            # translation, surfacing untranslated to the caller.
            self.store.append_event(
                record.opportunity_id,
                "capacity_refused",
                {"reason": str(error)},
                self._now_ns(),
            )
            logger.warning(
                "opportunity_capacity_refused",
                opportunity_id=record.opportunity_id,
                reason=str(error),
            )
            raise OpportunityCapacityError(str(error)) from error
        self.store.append_event(
            record.opportunity_id,
            "capacity_reserved",
            {
                "venue_messages": dict(venue_messages),
                "hedge_messages": self.hedge_messages,
            },
            self._now_ns(),
        )

    def _release_capacity(self, opportunity_id: str) -> None:
        """Hand back whatever was reserved but never sent."""

        if self.control_plane is None:
            return
        self.control_plane.ledger.release(opportunity_id)

    def _admit_leg(
        self, opportunity_id: str, strategy_id: str, leg: _Leg
    ) -> bool:
        """Ask the control plane whether this leg may reach the venue.

        An unwind leg is exposure-reducing, so it is presented as an
        emergency hedge and is exempt from every capacity control. Being
        throttled out of closing a position we already hold would turn a
        capacity problem into a risk problem.
        """

        if self.control_plane is None:
            return True
        if leg.is_unwind:
            stage = OrderStage.EMERGENCY_HEDGE
            priority = Priority.EMERGENCY_HEDGE
        else:
            stage = OrderStage.NEW
            priority = Priority.MARKET_MAKING
        decision = self.control_plane.evaluate(
            MessageRequest(
                message_id=leg.client_order_id,
                strategy_id=strategy_id,
                venue=leg.venue,
                stage=stage,
                priority=priority,
                opportunity_id=opportunity_id,
            )
        )
        if decision.transmits:
            return True
        leg.state = LegState.BLOCKED
        leg.last_error = (
            f"blocked by {decision.control.value}: {decision.reason}"
        )
        self._persist_leg(opportunity_id, leg)
        self.store.append_event(
            opportunity_id,
            "leg_blocked",
            {
                "client_order_id": leg.client_order_id,
                "control": decision.control.value,
                "action": decision.action.value,
                "reason": decision.reason,
            },
            self._now_ns(),
        )
        logger.warning(
            "leg_blocked_by_control",
            opportunity_id=opportunity_id,
            client_order_id=leg.client_order_id,
            control=decision.control.value,
            reason=decision.reason,
        )
        return False

    def _transition(
        self,
        record: OpportunityRecord,
        state: OpportunityState,
        transitions: list[str],
        *,
        terminal: bool = False,
        detail: str | None = None,
    ) -> None:
        now = self._now_ns()
        self.store.save_opportunity(
            replace(
                record,
                state=state.value,
                terminal=terminal,
                updated_ns=now,
                reserved_notional=self._reservations.get(
                    record.opportunity_id,
                    record.reserved_notional,
                ),
            )
        )
        self.store.append_event(
            record.opportunity_id,
            "state_transition",
            {"state": state.value, "detail": detail or ""},
            now,
        )
        transitions.append(state.value)
        logger.info(
            "opportunity_state",
            opportunity_id=record.opportunity_id,
            state=state.value,
            detail=detail or "",
        )

    def _result(
        self,
        record: OpportunityRecord,
        legs: Sequence[_Leg],
        unwinds: tuple[UnwindPlan, ...],
        reconciliation: OpportunityReconciliation | None,
        transitions: Sequence[str],
    ) -> OpportunityResult:
        stored = self.store.load_opportunity(record.opportunity_id)
        state = OpportunityState(
            stored.state if stored is not None else record.state
        )
        return OpportunityResult(
            opportunity_id=record.opportunity_id,
            strategy_id=record.strategy_id,
            canonical_symbol=record.canonical_symbol,
            state=state,
            target_quantity=record.target_quantity,
            legs=tuple(
                LegOutcome(
                    leg_index=leg.index,
                    venue=leg.venue,
                    symbol=leg.symbol,
                    side=leg.side,
                    target_quantity=leg.target_quantity,
                    filled_quantity=leg.filled_quantity,
                    average_price=leg.average_price,
                    state=leg.state,
                    client_order_id=leg.client_order_id,
                    idempotency_key=leg.idempotency_key,
                    exchange_order_id=leg.exchange_order_id,
                    attempts=leg.attempt,
                    reject_reason=leg.reject_reason,
                    reject_action=leg.reject_action,
                    last_error=leg.last_error,
                )
                for leg in legs
            ),
            residual_quantity=self.residual_quantity(legs),
            reserved_notional=record.reserved_notional,
            unwinds=unwinds,
            reconciliation=reconciliation,
            transitions=tuple(transitions),
        )

    def _result_from_store(self, opportunity_id: str) -> OpportunityResult:
        record = self.store.load_opportunity(opportunity_id)
        if record is None:
            raise ResidualExposureError(
                f"opportunity {opportunity_id} is not persisted"
            )
        legs = [
            self._leg_from_record(leg)
            for leg in self.store.load_legs(opportunity_id)
        ]
        return self._result(
            record,
            legs,
            (),
            None,
            (f"replayed:{record.state}",),
        )


__all__ = [
    "LegOutcome",
    "LegRequest",
    "LegState",
    "MarketQuote",
    "OpportunityCoordinator",
    "OpportunityReconciliation",
    "OpportunityRequest",
    "OpportunityResult",
    "OpportunityRiskLimitError",
    "OpportunityState",
    "OpportunityTimeouts",
    "RejectAction",
    "RejectReason",
    "ResidualExposureError",
    "ResidualPolicy",
    "RetryDirective",
    "TERMINAL_LEG_STATES",
    "TERMINAL_STATES",
    "UnwindPlan",
    "UnwindPriceMode",
    "action_for_reject",
    "classify_reject",
    "classify_retry",
]
