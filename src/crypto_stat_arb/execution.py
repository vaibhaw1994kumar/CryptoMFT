from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
import json
from pathlib import Path
from typing import Any, Callable, Protocol

from .instruments import InstrumentMaster
from .models import Side, stable_id
from .orderbook import OrderBook


ORDER_API_SCHEMA_VERSION = "1.0.0"


class OrderAction(StrEnum):
    SUBMIT = "SUBMIT"
    CANCEL = "CANCEL"
    REPLACE = "REPLACE"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class TimeInForce(StrEnum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class MakerTakerIntent(StrEnum):
    MAKER = "MAKER"
    TAKER = "TAKER"
    ANY = "ANY"


class DispatchPolicy(StrEnum):
    DETERMINISTIC_SEQUENTIAL = "DETERMINISTIC_SEQUENTIAL"


class UnwindUrgency(StrEnum):
    PASSIVE = "PASSIVE"
    NORMAL = "NORMAL"
    AGGRESSIVE = "AGGRESSIVE"


@dataclass(frozen=True, slots=True)
class ResidualPolicy:
    """Fail-closed size, time, venue and execution controls for residual risk."""

    max_residual_base: Decimal
    max_residual_age_ns: int
    hedge_venue_priority: tuple[str, ...] = ()
    unwind_urgency: UnwindUrgency = UnwindUrgency.NORMAL
    unwind_price_slippage_bps: Decimal = Decimal("10")
    allow_risk_reducing_under_stop: bool = True
    max_unwind_attempts: int = 2
    max_one_sided_notional: Decimal = Decimal("Infinity")
    # While any plan is in residual breach no new risk is admitted; if the
    # breach is still open after ``kill_after_ns`` the kill switch trips.
    hold_new_risk_on_breach: bool = True
    kill_after_ns: int | None = None

    def __post_init__(self) -> None:
        if self.max_residual_base < 0:
            raise ValueError("maximum residual size must be non-negative")
        if self.max_residual_age_ns < 0:
            raise ValueError("maximum residual age must be non-negative")
        if self.unwind_price_slippage_bps < 0:
            raise ValueError("unwind price slippage must be non-negative")
        if self.max_unwind_attempts < 0:
            raise ValueError("maximum unwind attempts must be non-negative")
        if self.max_one_sided_notional < 0:
            raise ValueError("maximum one-sided notional must be non-negative")
        if len(set(self.hedge_venue_priority)) != len(self.hedge_venue_priority):
            raise ValueError("hedge venue priority must contain unique venues")


class RateLimitPolicy(StrEnum):
    REJECT = "REJECT"
    QUEUE = "QUEUE"
    DELAY = "DELAY"


class ThrottleAction(StrEnum):
    ALLOW = "ALLOW"
    REJECT = "REJECT"
    QUEUE = "QUEUE"
    DELAY = "DELAY"
    RELEASED = "RELEASED"
    IN_FLIGHT = "IN_FLIGHT"
    DELIVERED = "DELIVERED"


# Routing outcomes where the OMS has no venue answer yet and must wait.
DEFERRED_ACTIONS = frozenset({
    ThrottleAction.QUEUE, ThrottleAction.DELAY, ThrottleAction.IN_FLIGHT,
})


class ExecutionEventType(StrEnum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    REPLACED = "REPLACED"
    TIMED_OUT = "TIMED_OUT"
    # Recorded by the OMS itself (source INTERNAL) when it dispatches, not by a venue.
    SENT = "SENT"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"


class OrderUpdateSource(StrEnum):
    ADAPTER_CALLBACK = "ADAPTER_CALLBACK"
    PRIVATE_STREAM = "PRIVATE_STREAM"
    REST_QUERY = "REST_QUERY"
    TIMER = "TIMER"
    INTERNAL = "INTERNAL"


@dataclass(frozen=True, slots=True)
class CanonicalOrderRequest:
    """Immutable submit/cancel/replace contract shared by all execution modes."""

    schema_version: str
    request_id: str
    idempotency_key: str
    action: OrderAction
    order_id: str
    target_order_id: str | None
    parent_opportunity_id: str
    parent_order_id: str
    instrument_id: str
    venue: str
    side: Side
    price: Decimal | None
    quantity: Decimal
    order_type: OrderType
    time_in_force: TimeInForce
    post_only: bool
    reduce_only: bool
    created_ts_ns: int

    @classmethod
    def create(
        cls,
        *,
        action: OrderAction,
        order_id: str,
        parent_opportunity_id: str,
        parent_order_id: str = "",
        instrument_id: str,
        venue: str,
        side: Side,
        price: Decimal | None,
        quantity: Decimal,
        order_type: OrderType = OrderType.LIMIT,
        time_in_force: TimeInForce = TimeInForce.GTC,
        post_only: bool = False,
        reduce_only: bool = False,
        created_ts_ns: int,
        target_order_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> "CanonicalOrderRequest":
        request_id = stable_id(
            "order-request", action, order_id, target_order_id or "", created_ts_ns
        )
        return cls(
            schema_version=ORDER_API_SCHEMA_VERSION,
            request_id=request_id,
            idempotency_key=idempotency_key or request_id,
            action=action,
            order_id=order_id,
            target_order_id=target_order_id,
            parent_opportunity_id=parent_opportunity_id,
            parent_order_id=parent_order_id,
            instrument_id=instrument_id,
            venue=venue,
            side=side,
            price=price,
            quantity=quantity,
            order_type=order_type,
            time_in_force=time_in_force,
            post_only=post_only,
            reduce_only=reduce_only,
            created_ts_ns=created_ts_ns,
        )

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        for key, value in tuple(row.items()):
            if isinstance(value, (Decimal, StrEnum)):
                row[key] = str(value)
        return row


@dataclass(frozen=True, slots=True)
class ExecutionCallback:
    schema_version: str
    callback_id: str
    request_id: str
    order_id: str
    parent_opportunity_id: str
    parent_order_id: str
    venue: str
    instrument_id: str
    event_type: ExecutionEventType
    event_ts_ns: int
    cumulative_filled_quantity: Decimal = Decimal("0")
    last_fill_quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    reason: str = ""
    native: dict[str, Any] | None = None
    # MAKER (a resting order filled later) / TAKER (filled on arrival) / "" unknown.
    liquidity: str = ""

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        for key, value in tuple(row.items()):
            if isinstance(value, (Decimal, StrEnum)):
                row[key] = str(value)
        return row


@dataclass(frozen=True, slots=True)
class CanonicalOrderUpdate:
    """Source-neutral order state input consumed by the OMS reducer."""

    update_id: str
    source: OrderUpdateSource
    parent_order_id: str
    order_id: str
    venue: str
    instrument_id: str
    state: ExecutionEventType
    event_ts_ns: int
    observed_ts_ns: int
    sequence: int = 0
    cumulative_filled_quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    reason: str = ""
    venue_order_id: str = ""
    liquidity: str = ""

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        for key, value in tuple(row.items()):
            if isinstance(value, (Decimal, StrEnum)):
                row[key] = str(value)
        return row

    @classmethod
    def from_callback(
        cls, callback: ExecutionCallback, observed_ts_ns: int
    ) -> "CanonicalOrderUpdate":
        return cls(
            update_id=callback.callback_id,
            source=OrderUpdateSource.ADAPTER_CALLBACK,
            parent_order_id=callback.parent_order_id,
            order_id=callback.order_id,
            venue=callback.venue,
            instrument_id=callback.instrument_id,
            state=callback.event_type,
            event_ts_ns=callback.event_ts_ns,
            observed_ts_ns=observed_ts_ns,
            cumulative_filled_quantity=callback.cumulative_filled_quantity,
            average_price=callback.average_price,
            reason=callback.reason,
            liquidity=callback.liquidity,
        )


@dataclass(frozen=True, slots=True)
class RoutingInstruction:
    """Strategy-owned routing constraints, including an optional venue override."""

    strategy_id: str
    eligible_venues: tuple[str, ...]
    instrument_id: str
    side: Side
    order_type: OrderType
    price: Decimal | None
    quantity: Decimal
    route_priority: tuple[str, ...]
    maker_taker_intent: MakerTakerIntent
    time_in_force: TimeInForce
    post_only: bool
    reduce_only: bool
    parent_opportunity_id: str
    parent_order_id: str
    order_id: str
    created_ts_ns: int
    route_override: str | None = None
    idempotency_key: str = ""
    risk_reducing: bool = False


@dataclass(slots=True)
class VenueRouteStatus:
    account_enabled: bool = True
    venue_enabled: bool = True
    rate_limit_capacity: int = 1_000
    rate_limit_used: int = 0
    rate_limit_window_ns: int = 1_000_000_000
    rate_limit_window_start_ns: int = 0
    rate_limit_policy: RateLimitPolicy = RateLimitPolicy.REJECT
    throttle_delay_ns: int = 0
    max_throttle_queue: int = 1_000
    _rate_limit_window_initialized: bool = field(
        default=False, init=False, repr=False
    )
    supported_order_types: frozenset[OrderType] = field(
        default_factory=lambda: frozenset((OrderType.LIMIT, OrderType.MARKET))
    )
    supports_post_only: bool = True
    supports_reduce_only: bool = True
    # From crypto_stat_arb.capabilities (venue ∩ adapter); defaults allow everything.
    supported_time_in_force: frozenset[TimeInForce] = field(
        default_factory=lambda: frozenset(TimeInForce)
    )
    supports_replace: bool = True
    allow_risk_reducing_when_disabled: bool = True
    allow_risk_reducing_rate_limit_override: bool = True

    @property
    def rate_limit_headroom(self) -> int:
        return max(0, self.rate_limit_capacity - self.rate_limit_used)

    def __post_init__(self) -> None:
        if self.rate_limit_capacity < 0 or self.rate_limit_used < 0:
            raise ValueError("rate-limit capacity and usage must be non-negative")
        if self.rate_limit_window_ns <= 0:
            raise ValueError("rate-limit window must be positive")
        if self.throttle_delay_ns < 0 or self.max_throttle_queue < 0:
            raise ValueError("throttle delay and queue size must be non-negative")

    def refresh_window(self, now_ns: int) -> bool:
        if not self._rate_limit_window_initialized:
            self._rate_limit_window_initialized = True
            if self.rate_limit_window_start_ns == 0 and now_ns >= self.rate_limit_window_ns:
                self.rate_limit_window_start_ns = (
                    now_ns // self.rate_limit_window_ns
                ) * self.rate_limit_window_ns
            return False
        if now_ns < self.rate_limit_window_start_ns + self.rate_limit_window_ns:
            return False
        windows = (now_ns - self.rate_limit_window_start_ns) // self.rate_limit_window_ns
        self.rate_limit_window_start_ns += windows * self.rate_limit_window_ns
        self.rate_limit_used = 0
        return True

    @property
    def next_window_ts_ns(self) -> int:
        return self.rate_limit_window_start_ns + self.rate_limit_window_ns


@dataclass(frozen=True, slots=True)
class RouteCheck:
    venue: str
    eligible: bool
    rate_limit_capacity: int
    rate_limit_used: int
    rate_limit_headroom: int
    throttle_policy: RateLimitPolicy
    throttle_action: ThrottleAction
    limit_bound: bool
    release_ts_ns: int | None
    reasons: tuple[str, ...]
    control_overrides: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RoutingResult:
    route_id: str
    selected_venue: str | None
    override_applied: bool
    checks: tuple[RouteCheck, ...]
    request: CanonicalOrderRequest | None
    callbacks: tuple[ExecutionCallback, ...]
    throttle_action: ThrottleAction = ThrottleAction.ALLOW
    release_ts_ns: int | None = None


@dataclass(frozen=True, slots=True)
class PendingRoute:
    route_id: str
    request: CanonicalOrderRequest
    book: OrderBook
    checks: tuple[RouteCheck, ...]
    override_applied: bool
    throttle_action: ThrottleAction
    enqueued_ts_ns: int
    release_ts_ns: int
    queue_ordinal: int


@dataclass(frozen=True, slots=True)
class ChildDispatchMeasurement:
    parent_order_id: str
    order_id: str
    venue: str
    leg_index: int
    dispatch_started_ts_ns: int
    dispatch_completed_ts_ns: int
    dispatch_duration_ns: int
    callback_count: int
    first_callback_observed_ts_ns: int | None
    last_callback_observed_ts_ns: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OrderTimeoutPolicy:
    """Deadlines for venue reports (MR-08C unknown-state handling).

    A child with no terminal venue state ``report_timeout_ns`` after it was
    first seen non-terminal (and with no request or report still in flight)
    is queried at the venue every ``query_interval_ns``. It is never resent.
    After ``max_queries`` unanswered queries it is marked TIMED_OUT, the plan
    cannot close, and the kill switch is activated.
    """

    report_timeout_ns: int = 2_000_000_000
    query_interval_ns: int = 500_000_000
    max_queries: int = 3
    kill_on_unresolved: bool = True

    def __post_init__(self) -> None:
        if self.report_timeout_ns < 0 or self.query_interval_ns <= 0:
            raise ValueError("report timeout must be >= 0 and query interval > 0")
        if self.max_queries < 1:
            raise ValueError("at least one venue query is required")


@dataclass(frozen=True, slots=True)
class VenueOrderStatus:
    """Venue's answer to an order query; ``state=None`` means no such order."""

    order_id: str
    venue: str
    instrument_id: str
    state: ExecutionEventType | None
    cumulative_filled_quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    reason: str = ""
    venue_ts_ns: int = 0
    venue_order_id: str = ""


@dataclass(frozen=True, slots=True)
class PendingDelivery:
    """A venue report travelling back to the OMS."""

    deliver_ts_ns: int
    ordinal: int
    route_id: str
    request: CanonicalOrderRequest
    callback: ExecutionCallback
    venue_ts_ns: int


class VenueExecutionAdapter(Protocol):
    def handle(
        self, request: CanonicalOrderRequest, book: OrderBook | None
    ) -> tuple[ExecutionCallback, ...]: ...


CallbackHandler = Callable[[ExecutionCallback], None]


class DeterministicVenueAdapter:
    """Backtest/paper adapter with deterministic L2 matching and order semantics.

    * Liquidity our own orders take is withheld from later orders until the
      venue's published book reflects it (the level shrinks or a new snapshot
      arrives), so one displayed quantity is never sold twice.
    * IOC remainders are cancelled explicitly with a CANCELLED report.
    * Resting GTC orders fill (at their limit) when the opposite touch later
      trades through them; queue-position fills from prints are not modelled
      here (the advanced SDK backtester has the queue model).
    * The latest venue state of every order is queryable, which the OMS uses
      when a report is lost.
    """

    def __init__(self, venue: str, *, consume_liquidity: bool = True) -> None:
        self.venue = venue
        self.consume_liquidity = consume_liquidity
        self.open_orders: dict[str, CanonicalOrderRequest] = {}
        self.terminal_orders: set[str] = set()
        self.order_status: dict[str, VenueOrderStatus] = {}
        self.consumption_events = 0
        self._fills: dict[str, tuple[Decimal, Decimal]] = {}
        self._consumed: dict[tuple[str, Side, Decimal], list[Decimal]] = {}
        self._book_marker: dict[str, tuple[int, int]] = {}

    @staticmethod
    def _callback(
        request: CanonicalOrderRequest,
        event_type: ExecutionEventType,
        *,
        filled: Decimal = Decimal("0"),
        last_fill: Decimal = Decimal("0"),
        average_price: Decimal = Decimal("0"),
        reason: str = "",
        liquidity: str = "",
    ) -> ExecutionCallback:
        return ExecutionCallback(
            schema_version=ORDER_API_SCHEMA_VERSION,
            callback_id=stable_id(
                "execution-callback", request.request_id, event_type, filled, reason
            ),
            request_id=request.request_id,
            order_id=request.order_id,
            parent_opportunity_id=request.parent_opportunity_id,
            parent_order_id=request.parent_order_id,
            venue=request.venue,
            instrument_id=request.instrument_id,
            event_type=event_type,
            event_ts_ns=request.created_ts_ns,
            cumulative_filled_quantity=filled,
            last_fill_quantity=last_fill,
            average_price=average_price,
            reason=reason,
            liquidity=liquidity,
            native={"adapter": "deterministic-venue-adapter-v2"},
        )

    def _remember(self, callbacks: tuple[ExecutionCallback, ...]) -> None:
        for callback in callbacks:
            if callback.event_type == ExecutionEventType.REPLACED:
                continue
            previous = self.order_status.get(callback.order_id)
            if (
                callback.event_type == ExecutionEventType.REJECTED
                and previous is not None and previous.state is not None
                and previous.state != ExecutionEventType.REJECTED
            ):
                # A rejected cancel does not change the target order's state.
                continue
            self.order_status[callback.order_id] = VenueOrderStatus(
                order_id=callback.order_id, venue=callback.venue,
                instrument_id=callback.instrument_id, state=callback.event_type,
                cumulative_filled_quantity=callback.cumulative_filled_quantity,
                average_price=callback.average_price, reason=callback.reason,
                venue_ts_ns=callback.event_ts_ns,
            )

    def query_order(self, order_id: str) -> VenueOrderStatus:
        return self.order_status.get(order_id) or VenueOrderStatus(
            order_id=order_id, venue=self.venue, instrument_id="", state=None,
            reason="order unknown to venue",
        )

    def handle(
        self, request: CanonicalOrderRequest, book: OrderBook | None
    ) -> tuple[ExecutionCallback, ...]:
        if request.action == OrderAction.CANCEL:
            callbacks = self._cancel(request)
        elif request.action == OrderAction.REPLACE:
            target = request.target_order_id or ""
            if target not in self.open_orders:
                callbacks = (self._callback(
                    request, ExecutionEventType.REJECTED,
                    reason="replace target is not open",
                ),)
            else:
                del self.open_orders[target]
                self.terminal_orders.add(target)
                self._fills.pop(target, None)
                replaced = self._callback(request, ExecutionEventType.REPLACED)
                callbacks = (replaced, *self._submit(request, book))
        else:
            callbacks = self._submit(request, book)
        self._remember(callbacks)
        return callbacks

    def _cancel(
        self, request: CanonicalOrderRequest
    ) -> tuple[ExecutionCallback, ...]:
        target = request.target_order_id or request.order_id
        if target not in self.open_orders:
            return (self._callback(
                request, ExecutionEventType.REJECTED,
                reason="cancel target is not open",
            ),)
        del self.open_orders[target]
        self.terminal_orders.add(target)
        filled, notional = self._fills.pop(target, (Decimal("0"), Decimal("0")))
        return (self._callback(
            request, ExecutionEventType.CANCELLED, filled=filled,
            average_price=(notional / filled if filled else Decimal("0")),
        ),)

    def _refresh(self, book: OrderBook) -> None:
        """Release withheld liquidity only as the venue's book reflects it."""
        marker = (book.stats.snapshots, book.stats.resyncs)
        keys = [key for key in self._consumed if key[0] == book.instrument_id]
        if self._book_marker.get(book.instrument_id) != marker:
            self._book_marker[book.instrument_id] = marker
            for key in keys:
                del self._consumed[key]
            return
        for key in keys:
            taken, published = self._consumed[key]
            levels = book.asks if key[1] == Side.BUY else book.bids
            current = levels.get(key[2], Decimal("0"))
            if current <= 0:
                del self._consumed[key]
                continue
            if current < published:
                taken -= published - current
                if taken <= 0:
                    del self._consumed[key]
                    continue
            self._consumed[key] = [taken, current]

    def available(self, book: OrderBook, side: Side, price: Decimal) -> Decimal:
        """Displayed quantity at ``price`` minus our own unreflected takes."""
        if self.consume_liquidity:
            self._refresh(book)
        levels = book.asks if side == Side.BUY else book.bids
        used = self._consumed.get((book.instrument_id, side, price))
        return max(Decimal("0"), levels.get(price, Decimal("0")) - (
            used[0] if used and self.consume_liquidity else Decimal("0")
        ))

    def _walk(
        self, book: OrderBook, side: Side, quantity: Decimal, limit: Decimal
    ) -> tuple[Decimal, Decimal, list[tuple[Decimal, Decimal]]]:
        if self.consume_liquidity:
            self._refresh(book)
        levels = book.asks if side == Side.BUY else book.bids
        prices = sorted(levels) if side == Side.BUY else sorted(levels, reverse=True)
        remaining = quantity
        filled = Decimal("0")
        notional = Decimal("0")
        takes: list[tuple[Decimal, Decimal]] = []
        for price in prices:
            if side == Side.BUY and price > limit:
                break
            if side == Side.SELL and price < limit:
                break
            used = self._consumed.get((book.instrument_id, side, price))
            available = levels[price] - (
                used[0] if used and self.consume_liquidity else Decimal("0")
            )
            if available <= 0:
                continue
            take = min(remaining, available)
            takes.append((price, take))
            filled += take
            notional += take * price
            remaining -= take
            if remaining == 0:
                break
        return filled, (notional / filled if filled else Decimal("0")), takes

    def _consume(
        self, book: OrderBook, side: Side, takes: list[tuple[Decimal, Decimal]]
    ) -> None:
        if not self.consume_liquidity or not takes:
            return
        levels = book.asks if side == Side.BUY else book.bids
        for price, take in takes:
            key = (book.instrument_id, side, price)
            used = self._consumed.get(key)
            self._consumed[key] = [
                (used[0] if used else Decimal("0")) + take,
                levels.get(price, Decimal("0")),
            ]
        self.consumption_events += 1

    def _submit(
        self, request: CanonicalOrderRequest, book: OrderBook | None
    ) -> tuple[ExecutionCallback, ...]:
        if book is None or not book.is_tradable:
            return (self._callback(
                request, ExecutionEventType.REJECTED, reason="book is not tradable"
            ),)
        limit = request.price
        if request.order_type == OrderType.MARKET:
            limit = Decimal("Infinity") if request.side == Side.BUY else Decimal("0")
        if request.post_only and request.price is not None:
            crosses = (
                request.side == Side.BUY and request.price >= book.best_ask
            ) or (
                request.side == Side.SELL and request.price <= book.best_bid
            )
            if crosses:
                return (self._callback(
                    request, ExecutionEventType.REJECTED,
                    reason="post-only order would take liquidity",
                ),)
        filled, average_price, takes = self._walk(
            book, request.side, request.quantity, limit
        )
        if request.time_in_force == TimeInForce.FOK and filled < request.quantity:
            return (self._callback(
                request, ExecutionEventType.REJECTED,
                reason="fill-or-kill quantity unavailable",
            ),)
        self._consume(book, request.side, takes)
        acknowledged = self._callback(request, ExecutionEventType.ACKNOWLEDGED)
        if filled == request.quantity:
            self.terminal_orders.add(request.order_id)
            return (acknowledged, self._callback(
                request,
                ExecutionEventType.FILLED,
                filled=filled,
                last_fill=filled,
                average_price=average_price,
                liquidity="TAKER",
            ))
        if filled > 0:
            if request.time_in_force == TimeInForce.GTC:
                self.open_orders[request.order_id] = request
                self._fills[request.order_id] = (filled, filled * average_price)
                return (acknowledged, self._callback(
                    request, ExecutionEventType.PARTIAL, filled=filled,
                    last_fill=filled, average_price=average_price,
                    reason="remaining quantity resting", liquidity="TAKER",
                ))
            self.terminal_orders.add(request.order_id)
            return (
                acknowledged,
                self._callback(
                    request, ExecutionEventType.PARTIAL, filled=filled,
                    last_fill=filled, average_price=average_price,
                    reason="remaining quantity cancelled by IOC", liquidity="TAKER",
                ),
                self._callback(
                    request, ExecutionEventType.CANCELLED, filled=filled,
                    average_price=average_price,
                    reason="IOC remainder cancelled",
                ),
            )
        if request.time_in_force == TimeInForce.GTC:
            self.open_orders[request.order_id] = request
            return (acknowledged,)
        self.terminal_orders.add(request.order_id)
        return (self._callback(
            request, ExecutionEventType.REJECTED, reason="no executable liquidity"
        ),)

    def on_book_update(
        self, book: OrderBook
    ) -> tuple[tuple[CanonicalOrderRequest, ExecutionCallback], ...]:
        """Fill resting orders the published book now trades through."""
        if not book.is_tradable or not self.open_orders:
            return ()
        produced = []
        for order_id, request in sorted(self.open_orders.items()):
            if request.instrument_id != book.instrument_id or request.price is None:
                continue
            filled_before, notional_before = self._fills.get(
                order_id, (Decimal("0"), Decimal("0"))
            )
            remaining = request.quantity - filled_before
            crosses = (
                request.side == Side.BUY and book.best_ask <= request.price
            ) or (
                request.side == Side.SELL and book.best_bid >= request.price
            )
            if remaining <= 0 or not crosses:
                continue
            filled, _, takes = self._walk(book, request.side, remaining, request.price)
            if filled <= 0:
                continue
            self._consume(book, request.side, takes)
            cumulative = filled_before + filled
            notional = notional_before + filled * request.price
            if cumulative == request.quantity:
                event_type = ExecutionEventType.FILLED
                del self.open_orders[order_id]
                self._fills.pop(order_id, None)
                self.terminal_orders.add(order_id)
            else:
                event_type = ExecutionEventType.PARTIAL
                self._fills[order_id] = (cumulative, notional)
            callback = self._callback(
                request, event_type, filled=cumulative, last_fill=filled,
                average_price=notional / cumulative,
                reason="resting order traded through by book", liquidity="MAKER",
            )
            self._remember((callback,))
            produced.append((request, callback))
        return tuple(produced)


class StrategyDirectedRouter:
    """Validates and routes an explicit strategy-selected venue request."""

    def __init__(
        self,
        instruments: InstrumentMaster,
        adapters: dict[str, VenueExecutionAdapter] | None = None,
        callback_handler: CallbackHandler | None = None,
        route_status: dict[str, VenueRouteStatus] | None = None,
        *,
        latency_model: Any | None = None,
    ) -> None:
        self.instruments = instruments
        self.latency_model = latency_model
        self._deliveries: list[PendingDelivery] = []
        self._delivery_ordinal = 0
        self._last_delivery_ts: dict[str, int] = {}
        self.adapters = adapters or {}
        self.callback_handler = callback_handler
        self.route_status = route_status or {}
        self._results: dict[str, tuple[ExecutionCallback, ...]] = {}
        self._requests_by_idempotency: dict[str, CanonicalOrderRequest] = {}
        self._pending: list[PendingRoute] = []
        self._pending_by_idempotency: dict[str, PendingRoute] = {}
        self._queue_ordinal = 0
        self.trace: list[dict[str, Any]] = []

    def route_controlled(
        self,
        instruction: RoutingInstruction,
        books: dict[tuple[str, str], OrderBook],
    ) -> RoutingResult:
        if not instruction.eligible_venues:
            raise ValueError("strategy must provide at least one eligible venue")
        if len(set(instruction.eligible_venues)) != len(instruction.eligible_venues):
            raise ValueError("eligible venues must be unique")
        unknown_priority = set(instruction.route_priority) - set(instruction.eligible_venues)
        if unknown_priority:
            raise ValueError("route priority contains an ineligible venue")
        if (
            instruction.route_override is not None
            and instruction.route_override not in instruction.eligible_venues
        ):
            raise ValueError("route override must be an eligible venue")
        if (
            instruction.maker_taker_intent == MakerTakerIntent.MAKER
            and instruction.order_type != OrderType.LIMIT
        ):
            raise ValueError("maker intent requires a limit order")

        if (
            instruction.idempotency_key
            and instruction.idempotency_key in self._results
        ):
            cached_request = self._requests_by_idempotency[instruction.idempotency_key]
            cached_callbacks = self._results[instruction.idempotency_key]
            route_id = stable_id(
                "route", instruction.parent_opportunity_id, instruction.order_id,
                instruction.route_override or "", instruction.created_ts_ns,
            )
            self.trace.append({
                "record_type": "DUPLICATE_SUPPRESSED",
                "request_id": cached_request.request_id,
                "idempotency_key": instruction.idempotency_key,
                "ts_ns": instruction.created_ts_ns,
            })
            return RoutingResult(
                route_id, cached_request.venue,
                instruction.route_override is not None, (),
                cached_request, cached_callbacks,
            )
        if (
            instruction.idempotency_key
            and instruction.idempotency_key in self._pending_by_idempotency
        ):
            pending = self._pending_by_idempotency[instruction.idempotency_key]
            self.trace.append({
                "record_type": "DUPLICATE_SUPPRESSED",
                "request_id": pending.request.request_id,
                "idempotency_key": instruction.idempotency_key,
                "ts_ns": instruction.created_ts_ns,
                "pending_throttle_action": pending.throttle_action.value,
            })
            return RoutingResult(
                pending.route_id, pending.request.venue,
                pending.override_applied, pending.checks, pending.request, (),
                pending.throttle_action, pending.release_ts_ns,
            )

        ordered = list(instruction.route_priority)
        ordered.extend(x for x in instruction.eligible_venues if x not in ordered)
        if instruction.route_override is not None:
            ordered = [instruction.route_override]
        checks: list[RouteCheck] = []
        selected: str | None = None
        selected_action = ThrottleAction.REJECT
        release_ts_ns: int | None = None
        for venue in ordered:
            status = self.route_status.setdefault(venue, VenueRouteStatus())
            status.refresh_window(instruction.created_ts_ns)
            reasons = []
            control_overrides: list[str] = []
            disabled_override = (
                instruction.risk_reducing
                and status.allow_risk_reducing_when_disabled
            )
            if not status.account_enabled and not disabled_override:
                reasons.append("account_disabled")
            elif not status.account_enabled:
                control_overrides.append("account_disabled")
            if not status.venue_enabled and not disabled_override:
                reasons.append("venue_disabled")
            elif not status.venue_enabled:
                control_overrides.append("venue_disabled")
            if instruction.order_type not in status.supported_order_types:
                reasons.append("order_type_unsupported")
            if instruction.maker_taker_intent == MakerTakerIntent.MAKER:
                if not status.supports_post_only:
                    reasons.append("maker_intent_unsupported")
            if instruction.reduce_only and not status.supports_reduce_only:
                reasons.append("reduce_only_unsupported")
            try:
                self.instruments.get(
                    venue, instruction.instrument_id, instruction.created_ts_ns
                )
            except ValueError:
                reasons.append("instrument_unsupported")
            book = books.get((venue, instruction.instrument_id))
            if book is None:
                reasons.append("book_unavailable")
            elif not book.is_tradable:
                reasons.append("book_not_tradable")
            limit_bound = status.rate_limit_headroom < 1
            action = ThrottleAction.ALLOW
            candidate_release_ts: int | None = None
            rate_limit_overridden = False
            if limit_bound:
                if (
                    instruction.risk_reducing
                    and status.allow_risk_reducing_rate_limit_override
                ):
                    control_overrides.append("rate_limit_exhausted")
                    action = ThrottleAction.ALLOW
                    rate_limit_overridden = True
                else:
                    reasons.append("rate_limit_exhausted")
                if rate_limit_overridden:
                    pass
                elif status.rate_limit_capacity == 0:
                    action = ThrottleAction.REJECT
                    reasons.append("rate_limit_capacity_zero")
                elif status.rate_limit_policy == RateLimitPolicy.REJECT:
                    action = ThrottleAction.REJECT
                else:
                    queued_for_venue = sum(
                        item.request.venue == venue
                        and item.throttle_action != ThrottleAction.IN_FLIGHT
                        for item in self._pending
                    )
                    if queued_for_venue >= status.max_throttle_queue:
                        action = ThrottleAction.REJECT
                        reasons.append("throttle_queue_full")
                    else:
                        action = ThrottleAction(status.rate_limit_policy.value)
                        candidate_release_ts = status.next_window_ts_ns + (
                            status.throttle_delay_ns
                            if action == ThrottleAction.DELAY else 0
                        )
            non_rate_reasons = [
                reason for reason in reasons
                if reason not in {"rate_limit_exhausted"}
            ]
            if non_rate_reasons and action in {
                ThrottleAction.QUEUE, ThrottleAction.DELAY
            }:
                action = ThrottleAction.REJECT
                candidate_release_ts = None
            can_select = (
                not non_rate_reasons
                and action in {
                    ThrottleAction.ALLOW, ThrottleAction.QUEUE, ThrottleAction.DELAY
                }
            )
            check = RouteCheck(
                venue=venue,
                eligible=can_select,
                rate_limit_capacity=status.rate_limit_capacity,
                rate_limit_used=status.rate_limit_used,
                rate_limit_headroom=status.rate_limit_headroom,
                throttle_policy=status.rate_limit_policy,
                throttle_action=action,
                limit_bound=limit_bound,
                release_ts_ns=candidate_release_ts,
                reasons=tuple(reasons),
                control_overrides=tuple(control_overrides),
            )
            checks.append(check)
            if can_select:
                selected = venue
                selected_action = action
                release_ts_ns = candidate_release_ts
                break

        route_id = stable_id(
            "route",
            instruction.parent_opportunity_id,
            instruction.order_id,
            instruction.route_override or "",
            instruction.created_ts_ns,
        )
        decision = {
            "record_type": "ROUTE_DECISION",
            "ts_ns": instruction.created_ts_ns,
            "route_id": route_id,
            "strategy_id": instruction.strategy_id,
            "parent_opportunity_id": instruction.parent_opportunity_id,
            "parent_order_id": instruction.parent_order_id,
            "order_id": instruction.order_id,
            "instrument_id": instruction.instrument_id,
            "eligible_venues": list(instruction.eligible_venues),
            "route_priority": list(instruction.route_priority),
            "route_override": instruction.route_override,
            "selected_venue": selected,
            "throttle_action": selected_action.value,
            "release_ts_ns": release_ts_ns,
            "maker_taker_intent": instruction.maker_taker_intent.value,
            "risk_reducing": instruction.risk_reducing,
            "checks": [check.to_dict() for check in checks],
        }
        self.trace.append(decision)
        for check in checks:
            if check.limit_bound:
                self.trace.append({
                    "record_type": "RATE_LIMIT_ACTION",
                    "ts_ns": instruction.created_ts_ns,
                    "route_id": route_id,
                    "venue": check.venue,
                    "selected": check.venue == selected,
                    "policy": check.throttle_policy.value,
                    "action": check.throttle_action.value,
                    "rate_limit_capacity": check.rate_limit_capacity,
                    "rate_limit_used": check.rate_limit_used,
                    "rate_limit_headroom": check.rate_limit_headroom,
                    "release_ts_ns": check.release_ts_ns,
                    "reasons": list(check.reasons),
                })
        if selected is None:
            return RoutingResult(
                route_id, None, False, tuple(checks), None, (),
                ThrottleAction.REJECT, None,
            )

        status = self.route_status[selected]
        post_only = instruction.post_only
        if instruction.maker_taker_intent == MakerTakerIntent.MAKER:
            post_only = True
        elif instruction.maker_taker_intent == MakerTakerIntent.TAKER:
            post_only = False
        request = CanonicalOrderRequest.create(
            action=OrderAction.SUBMIT,
            order_id=instruction.order_id,
            parent_opportunity_id=instruction.parent_opportunity_id,
            parent_order_id=instruction.parent_order_id,
            instrument_id=instruction.instrument_id,
            venue=selected,
            side=instruction.side,
            price=instruction.price,
            quantity=instruction.quantity,
            order_type=instruction.order_type,
            time_in_force=instruction.time_in_force,
            post_only=post_only,
            reduce_only=instruction.reduce_only,
            created_ts_ns=instruction.created_ts_ns,
            idempotency_key=instruction.idempotency_key or stable_id(
                "route-submit", route_id, selected
            ),
        )
        self._validate(request)
        if selected_action in {ThrottleAction.QUEUE, ThrottleAction.DELAY}:
            assert release_ts_ns is not None
            self._queue_ordinal += 1
            pending = PendingRoute(
                route_id=route_id, request=request,
                book=books[(selected, instruction.instrument_id)],
                checks=tuple(checks),
                override_applied=instruction.route_override is not None,
                throttle_action=selected_action,
                enqueued_ts_ns=instruction.created_ts_ns,
                release_ts_ns=release_ts_ns,
                queue_ordinal=self._queue_ordinal,
            )
            self._pending.append(pending)
            self._pending_by_idempotency[request.idempotency_key] = pending
            self.trace.append({
                "record_type": "THROTTLE_BOUND",
                "ts_ns": instruction.created_ts_ns,
                "route_id": route_id,
                "request_id": request.request_id,
                "venue": selected,
                "action": selected_action.value,
                "rate_limit_capacity": status.rate_limit_capacity,
                "rate_limit_used": status.rate_limit_used,
                "rate_limit_headroom": status.rate_limit_headroom,
                "release_ts_ns": release_ts_ns,
                "queue_depth": sum(
                    item.request.venue == selected for item in self._pending
                ),
            })
            return RoutingResult(
                route_id=route_id, selected_venue=selected,
                override_applied=instruction.route_override is not None,
                checks=tuple(checks), request=request, callbacks=(),
                throttle_action=selected_action, release_ts_ns=release_ts_ns,
            )
        status.rate_limit_used += 1
        if self.latency_model is not None:
            pending = self._send_in_flight(
                route_id, request, books[(selected, instruction.instrument_id)],
                tuple(checks), instruction.route_override is not None,
                instruction.created_ts_ns, "entry",
            )
            return RoutingResult(
                route_id=route_id, selected_venue=selected,
                override_applied=instruction.route_override is not None,
                checks=tuple(checks), request=request, callbacks=(),
                throttle_action=ThrottleAction.IN_FLIGHT,
                release_ts_ns=pending.release_ts_ns,
            )
        callbacks = self._execute(
            request, books[(selected, instruction.instrument_id)], deliver=True
        )
        return RoutingResult(
            route_id=route_id,
            selected_venue=selected,
            override_applied=instruction.route_override is not None,
            checks=tuple(checks),
            request=request,
            callbacks=callbacks,
            throttle_action=ThrottleAction.ALLOW,
        )

    def _send_in_flight(
        self, route_id: str, request: CanonicalOrderRequest,
        book: OrderBook | None, checks: tuple[RouteCheck, ...],
        override_applied: bool, send_ts_ns: int, stage: str,
    ) -> PendingRoute:
        latency_ns = self.latency_model.draw(request.venue, stage)
        self._queue_ordinal += 1
        pending = PendingRoute(
            route_id=route_id, request=request, book=book, checks=checks,
            override_applied=override_applied,
            throttle_action=ThrottleAction.IN_FLIGHT,
            enqueued_ts_ns=send_ts_ns, release_ts_ns=send_ts_ns + latency_ns,
            queue_ordinal=self._queue_ordinal,
        )
        self._pending.append(pending)
        self._pending_by_idempotency[request.idempotency_key] = pending
        self.trace.append({
            "record_type": "ORDER_IN_FLIGHT",
            "ts_ns": send_ts_ns,
            "route_id": route_id,
            "request_id": request.request_id,
            "order_id": request.order_id,
            "action": request.action.value,
            "venue": request.venue,
            "latency_stage": stage,
            "latency_ns": latency_ns,
            "arrival_ts_ns": pending.release_ts_ns,
        })
        return pending

    def _arrive(self, pending: PendingRoute) -> None:
        """The venue matching engine receives the request at its arrival time."""
        request = pending.request
        arrival_ts = pending.release_ts_ns
        self.trace.append({
            "record_type": "ORDER_ARRIVED",
            "ts_ns": arrival_ts,
            "route_id": pending.route_id,
            "request_id": request.request_id,
            "order_id": request.order_id,
            "action": request.action.value,
            "venue": request.venue,
            "sent_ts_ns": pending.enqueued_ts_ns,
            "latency_ns": arrival_ts - pending.enqueued_ts_ns,
        })
        callbacks = self._execute(
            request, pending.book, deliver=False, venue_ts_ns=arrival_ts
        )
        self._schedule(pending.route_id, request, callbacks, arrival_ts)

    def _schedule(
        self, route_id: str, request: CanonicalOrderRequest,
        callbacks: tuple[ExecutionCallback, ...], venue_ts_ns: int,
    ) -> None:
        for callback in callbacks:
            latency_ns = 0
            if self.latency_model is not None:
                stage = (
                    "fill"
                    if callback.cumulative_filled_quantity > 0
                    or callback.event_type in {
                        ExecutionEventType.PARTIAL, ExecutionEventType.FILLED,
                    }
                    else "submit_ack"
                )
                latency_ns = self.latency_model.draw(callback.venue, stage)
            deliver_ts = max(
                venue_ts_ns + latency_ns,
                self._last_delivery_ts.get(callback.order_id, 0),
            )
            self._last_delivery_ts[callback.order_id] = deliver_ts
            self._delivery_ordinal += 1
            self._deliveries.append(PendingDelivery(
                deliver_ts_ns=deliver_ts, ordinal=self._delivery_ordinal,
                route_id=route_id, request=request, callback=callback,
                venue_ts_ns=venue_ts_ns,
            ))

    def _deliver_due(self, now_ns: int) -> list[RoutingResult]:
        due = sorted(
            (item for item in self._deliveries if item.deliver_ts_ns <= now_ns),
            key=lambda item: (item.deliver_ts_ns, item.ordinal),
        )
        if not due:
            return []
        self._deliveries = [
            item for item in self._deliveries if item.deliver_ts_ns > now_ns
        ]
        results = []
        for item in due:
            self.trace.append({
                "record_type": "EXECUTION_CALLBACK_DELIVERED",
                "ts_ns": item.deliver_ts_ns,
                "venue_ts_ns": item.venue_ts_ns,
                "report_latency_ns": item.deliver_ts_ns - item.venue_ts_ns,
                "callback_id": item.callback.callback_id,
                "order_id": item.callback.order_id,
                "venue": item.callback.venue,
                "callback_type": item.callback.event_type.value,
            })
            if self.callback_handler is not None:
                self.callback_handler(item.callback)
            results.append(RoutingResult(
                route_id=item.route_id, selected_venue=item.request.venue,
                override_applied=False, checks=(), request=item.request,
                callbacks=(item.callback,),
                throttle_action=ThrottleAction.DELIVERED,
                release_ts_ns=item.deliver_ts_ns,
            ))
        return results

    def process_throttled(self, now_ns: int) -> tuple[RoutingResult, ...]:
        """Advance EMS time: release throttled requests in FIFO order, match
        in-flight requests on arrival, and deliver due venue reports."""
        released: list[RoutingResult] = []
        current = self._pending
        self._pending = []
        for pending in sorted(
            current,
            key=lambda item: (item.release_ts_ns, item.queue_ordinal, item.route_id),
        ):
            if pending.release_ts_ns > now_ns:
                self._pending.append(pending)
                continue
            if pending.throttle_action == ThrottleAction.IN_FLIGHT:
                self._pending_by_idempotency.pop(pending.request.idempotency_key, None)
                self._arrive(pending)
                continue
            status = self.route_status[pending.request.venue]
            status.refresh_window(now_ns)
            if status.rate_limit_headroom < 1:
                next_release = status.next_window_ts_ns + (
                    status.throttle_delay_ns
                    if pending.throttle_action == ThrottleAction.DELAY else 0
                )
                rescheduled = replace(pending, release_ts_ns=next_release)
                self._pending.append(rescheduled)
                self._pending_by_idempotency[
                    pending.request.idempotency_key
                ] = rescheduled
                self.trace.append({
                    "record_type": "THROTTLE_RESCHEDULED",
                    "ts_ns": now_ns,
                    "route_id": pending.route_id,
                    "request_id": pending.request.request_id,
                    "venue": pending.request.venue,
                    "action": pending.throttle_action.value,
                    "release_ts_ns": next_release,
                    "rate_limit_used": status.rate_limit_used,
                    "rate_limit_headroom": status.rate_limit_headroom,
                })
                continue
            status.rate_limit_used += 1
            self._pending_by_idempotency.pop(pending.request.idempotency_key, None)
            self.trace.append({
                "record_type": "THROTTLE_RELEASED",
                "ts_ns": now_ns,
                "route_id": pending.route_id,
                "request_id": pending.request.request_id,
                "venue": pending.request.venue,
                "original_action": pending.throttle_action.value,
                "action": ThrottleAction.RELEASED.value,
                "enqueued_ts_ns": pending.enqueued_ts_ns,
                "scheduled_release_ts_ns": pending.release_ts_ns,
                "actual_delay_ns": now_ns - pending.enqueued_ts_ns,
                "rate_limit_used": status.rate_limit_used,
                "rate_limit_headroom": status.rate_limit_headroom,
            })
            if self.latency_model is not None:
                self._send_in_flight(
                    pending.route_id, pending.request, pending.book,
                    pending.checks, pending.override_applied, now_ns, "entry",
                )
                callbacks: tuple[ExecutionCallback, ...] = ()
            else:
                callbacks = self._execute(pending.request, pending.book, deliver=True)
            released.append(RoutingResult(
                route_id=pending.route_id,
                selected_venue=pending.request.venue,
                override_applied=pending.override_applied,
                checks=pending.checks,
                request=pending.request,
                callbacks=callbacks,
                throttle_action=ThrottleAction.RELEASED,
                release_ts_ns=pending.release_ts_ns,
            ))
        released.extend(self._deliver_due(now_ns))
        return tuple(released)

    def next_due_ts(self) -> int | None:
        """Earliest pending release, arrival or report delivery time."""
        candidates = [item.release_ts_ns for item in self._pending]
        candidates.extend(item.deliver_ts_ns for item in self._deliveries)
        return min(candidates) if candidates else None

    @staticmethod
    def _restamp_status(adapter: object, callbacks: tuple[ExecutionCallback, ...]) -> None:
        """Keep the adapter's queryable order status on the venue match time."""
        status = getattr(adapter, "order_status", None)
        if status is None:
            return
        for callback in callbacks:
            current = status.get(callback.order_id)
            if (
                current is not None and current.state == callback.event_type
                and current.cumulative_filled_quantity == callback.cumulative_filled_quantity
            ):
                status[callback.order_id] = replace(current, venue_ts_ns=callback.event_ts_ns)

    def on_book_update(self, venue: str, book: OrderBook, now_ns: int) -> int:
        """Let the venue simulator fill resting orders against a new book."""
        hook = getattr(self.adapters.get(venue), "on_book_update", None)
        if hook is None:
            return 0
        produced = hook(book)
        for request, callback in produced:
            # The venue matched now, not when the order was placed: stamp the
            # report and the queryable order status with the match time.
            callback = replace(callback, event_ts_ns=now_ns)
            self._restamp_status(self.adapters.get(venue), (callback,))
            self.trace.append({
                "record_type": "EXECUTION_CALLBACK",
                "ts_ns": now_ns,
                "callback": callback.to_dict(),
            })
            self._schedule(
                stable_id("resting-fill", callback.callback_id), request,
                (callback,), now_ns,
            )
        return len(produced)

    def query_order(
        self, venue: str, order_id: str, now_ns: int
    ) -> VenueOrderStatus | None:
        """Ask the venue for an order's state; never resends the order."""
        query = getattr(self.adapters.get(venue), "query_order", None)
        self.trace.append({
            "record_type": "ORDER_QUERY", "ts_ns": now_ns,
            "venue": venue, "order_id": order_id,
            "supported": query is not None,
        })
        if query is None:
            return None
        try:
            status = query(order_id)
        except Exception as exc:  # venue/transport failure is an unanswered query
            self.trace.append({
                "record_type": "ORDER_QUERY_FAILED", "ts_ns": now_ns,
                "venue": venue, "order_id": order_id, "error": str(exc),
            })
            return None
        self.trace.append({
            "record_type": "ORDER_QUERY_RESULT", "ts_ns": now_ns,
            "venue": venue, "order_id": order_id,
            "state": status.state.value if status and status.state else None,
            "cumulative_filled_quantity": (
                str(status.cumulative_filled_quantity) if status else None
            ),
        })
        return status

    def rate_limit_snapshot(self, now_ns: int | None = None) -> dict[str, Any]:
        venues: dict[str, Any] = {}
        for venue, status in sorted(self.route_status.items()):
            if now_ns is not None:
                status.refresh_window(now_ns)
            pending = [
                item for item in self._pending
                if item.request.venue == venue
                and item.throttle_action != ThrottleAction.IN_FLIGHT
            ]
            venues[venue] = {
                "policy": status.rate_limit_policy.value,
                "capacity": status.rate_limit_capacity,
                "used": status.rate_limit_used,
                "headroom": status.rate_limit_headroom,
                "window_start_ts_ns": status.rate_limit_window_start_ns,
                "next_window_ts_ns": status.next_window_ts_ns,
                "throttle_delay_ns": status.throttle_delay_ns,
                "queue_depth": len(pending),
                "queued": sum(
                    item.throttle_action == ThrottleAction.QUEUE for item in pending
                ),
                "delayed": sum(
                    item.throttle_action == ThrottleAction.DELAY for item in pending
                ),
                "next_release_ts_ns": min(
                    (item.release_ts_ns for item in pending), default=None
                ),
                "in_flight": sum(
                    item.request.venue == venue
                    and item.throttle_action == ThrottleAction.IN_FLIGHT
                    for item in self._pending
                ),
            }
        return {
            "schema_version": "1.0.0",
            "pending_requests": sum(
                item.throttle_action != ThrottleAction.IN_FLIGHT
                for item in self._pending
            ),
            "in_flight_requests": sum(
                item.throttle_action == ThrottleAction.IN_FLIGHT
                for item in self._pending
            ),
            "undelivered_reports": len(self._deliveries),
            "venues": venues,
        }

    def has_pending_parent(self, parent_order_id: str) -> bool:
        return any(
            item.request.parent_order_id == parent_order_id for item in self._pending
        ) or any(
            item.request.parent_order_id == parent_order_id
            for item in self._deliveries
        )

    def has_pending_order(self, order_id: str) -> bool:
        return any(
            item.request.order_id == order_id for item in self._pending
        ) or any(
            item.callback.order_id == order_id for item in self._deliveries
        )

    def cancel_pending_order(
        self, order_id: str, *, now_ns: int, reason: str
    ) -> CanonicalOrderRequest | None:
        pending = next(
            (
                item for item in self._pending
                if item.request.order_id == order_id
                and item.throttle_action != ThrottleAction.IN_FLIGHT
            ),
            None,
        )
        if pending is None:
            return None
        self._pending.remove(pending)
        self._pending_by_idempotency.pop(pending.request.idempotency_key, None)
        self.trace.append({
            "record_type": "THROTTLE_CANCELLED",
            "ts_ns": now_ns,
            "route_id": pending.route_id,
            "request_id": pending.request.request_id,
            "order_id": order_id,
            "venue": pending.request.venue,
            "reason": reason,
        })
        return pending.request

    def route(
        self, request: CanonicalOrderRequest, book: OrderBook | None = None
    ) -> tuple[ExecutionCallback, ...]:
        """Send a request. With a latency model it travels and its reports are
        delivered later through ``process_throttled``; otherwise the adapter
        answers synchronously."""
        if self.latency_model is None:
            return self._execute(request, book, deliver=True)
        key = request.idempotency_key
        if key in self._results or key in self._pending_by_idempotency:
            self.trace.append({
                "record_type": "DUPLICATE_SUPPRESSED",
                "request_id": request.request_id,
                "idempotency_key": key,
                "ts_ns": request.created_ts_ns,
            })
            return self._results.get(key, ())
        self._validate(request)
        self._send_in_flight(
            stable_id("route-direct", request.request_id), request, book, (),
            False, request.created_ts_ns,
            "cancel" if request.action == OrderAction.CANCEL else "entry",
        )
        return ()

    def _execute(
        self, request: CanonicalOrderRequest, book: OrderBook | None, *,
        deliver: bool, venue_ts_ns: int | None = None,
    ) -> tuple[ExecutionCallback, ...]:
        if request.idempotency_key in self._results:
            callbacks = self._results[request.idempotency_key]
            self.trace.append({
                "record_type": "DUPLICATE_SUPPRESSED",
                "request_id": request.request_id,
                "idempotency_key": request.idempotency_key,
                "ts_ns": request.created_ts_ns,
            })
            return callbacks
        self._validate(request)
        self.trace.append({
            "record_type": "ORDER_REQUEST",
            "ts_ns": request.created_ts_ns,
            "request": request.to_dict(),
        })
        adapter = self.adapters.setdefault(
            request.venue, DeterministicVenueAdapter(request.venue)
        )
        callbacks = adapter.handle(request, book)
        if venue_ts_ns is not None:
            callbacks = tuple(
                replace(callback, event_ts_ns=venue_ts_ns) for callback in callbacks
            )
            self._restamp_status(adapter, callbacks)
        self._results[request.idempotency_key] = callbacks
        self._requests_by_idempotency[request.idempotency_key] = request
        for callback in callbacks:
            self.trace.append({
                "record_type": "EXECUTION_CALLBACK",
                "ts_ns": callback.event_ts_ns,
                "callback": callback.to_dict(),
            })
            if deliver and self.callback_handler is not None:
                self.callback_handler(callback)
        return callbacks

    def _validate(self, request: CanonicalOrderRequest) -> None:
        if request.schema_version != ORDER_API_SCHEMA_VERSION:
            raise ValueError("unsupported order API schema version")
        spec = self.instruments.get(
            request.venue, request.instrument_id, request.created_ts_ns
        )
        if spec.status != "TRADING":
            raise ValueError("instrument is not trading")
        if request.quantity <= 0:
            raise ValueError("quantity must be positive")
        if request.quantity < spec.min_quantity:
            raise ValueError("quantity below minimum")
        if request.quantity % spec.quantity_step:
            raise ValueError("quantity violates step")
        if request.order_type == OrderType.LIMIT and request.price is None:
            raise ValueError("limit order requires price")
        if request.order_type == OrderType.MARKET and request.post_only:
            raise ValueError("market order cannot be post-only")
        if request.price is not None and request.price % spec.tick_size:
            raise ValueError("price violates tick")
        if (
            request.action != OrderAction.CANCEL
            and request.price is not None
            and request.quantity * request.price < spec.min_notional
        ):
            raise ValueError("notional below minimum")
        if request.action in (OrderAction.CANCEL, OrderAction.REPLACE):
            if not request.target_order_id:
                raise ValueError(f"{request.action.value.lower()} requires target_order_id")
        status = self.route_status.get(request.venue)
        if status is not None and request.action != OrderAction.CANCEL:
            # The route's capabilities (crypto_stat_arb.capabilities): refuse what the venue
            # or our adapter cannot carry out instead of letting it be mistranslated.
            if request.action == OrderAction.REPLACE and not status.supports_replace:
                raise ValueError(f"{request.venue} route does not support replace; cancel and resubmit")
            if request.order_type not in status.supported_order_types:
                raise ValueError(f"{request.venue} route does not support order type {request.order_type.value}")
            if request.order_type == OrderType.LIMIT and request.time_in_force not in status.supported_time_in_force:
                raise ValueError(f"{request.venue} route does not support time in force {request.time_in_force.value}")
            if request.post_only and not status.supports_post_only:
                raise ValueError(f"{request.venue} route does not support post-only")

    def write_trace(self, path: Path) -> dict[str, Any]:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for record in self.trace:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        return {"path": str(path), "records": len(self.trace)}

    def stats_snapshot(self) -> dict[str, Any]:
        stats = {
            "unique_requests": len(self._results),
            "trace_records": len(self.trace),
            "callbacks": sum(
                row["record_type"] == "EXECUTION_CALLBACK" for row in self.trace
            ),
            "duplicates_suppressed": sum(
                row["record_type"] == "DUPLICATE_SUPPRESSED" for row in self.trace
            ),
            "route_decisions": sum(
                row["record_type"] == "ROUTE_DECISION" for row in self.trace
            ),
            "route_rejections": sum(
                row["record_type"] == "ROUTE_DECISION"
                and row["selected_venue"] is None
                for row in self.trace
            ),
            "route_overrides": sum(
                row["record_type"] == "ROUTE_DECISION"
                and row["route_override"] is not None
                for row in self.trace
            ),
            "rate_limit_rejections": sum(
                row["record_type"] == "RATE_LIMIT_ACTION"
                and row["action"] == ThrottleAction.REJECT.value
                for row in self.trace
            ),
            "rate_limit_queued": sum(
                row["record_type"] == "THROTTLE_BOUND"
                and row["action"] == ThrottleAction.QUEUE.value
                for row in self.trace
            ),
            "rate_limit_delayed": sum(
                row["record_type"] == "THROTTLE_BOUND"
                and row["action"] == ThrottleAction.DELAY.value
                for row in self.trace
            ),
            "rate_limit_released": sum(
                row["record_type"] == "THROTTLE_RELEASED" for row in self.trace
            ),
            "latency_model": (
                self.latency_model.source if self.latency_model is not None
                else "DISABLED_ZERO_LATENCY"
            ),
            "in_flight_sent": sum(
                row["record_type"] == "ORDER_IN_FLIGHT" for row in self.trace
            ),
            "arrivals": sum(
                row["record_type"] == "ORDER_ARRIVED" for row in self.trace
            ),
            "reports_delivered": sum(
                row["record_type"] == "EXECUTION_CALLBACK_DELIVERED"
                for row in self.trace
            ),
            "venue_queries": sum(
                row["record_type"] == "ORDER_QUERY" for row in self.trace
            ),
            "liquidity_consumption_events": sum(
                getattr(adapter, "consumption_events", 0)
                for adapter in self.adapters.values()
            ),
        }
        stats["rate_limits"] = self.rate_limit_snapshot()
        return stats
