"""Paper matching against replayed depth (MR-07, section 6).

Orders are *sent*, travel for a sampled entry latency, and execute against the
book as it is **when they arrive** at the simulated matching engine, not as it
was when the strategy decided. Acknowledgements and fill reports then travel
back with their own latency. That is what makes adverse selection and latency
measurable: an edge that disappears in flight is simply not there to fill.

Taker orders walk the executable book and consume it. Consumed quantity is
remembered per price level and only released when the venue's own book update
reflects the trade (the published size drops), or when a snapshot replaces the
book, so the same liquidity can never be sold twice.

Maker orders rest with a queue position taken from the visible size at their
price when they arrive. Printed trades at or through the price consume the
queue ahead first; cancellations ahead of us are inferred pro rata from level
shrinkage that is not explained by trades. This is the queue-aware model from
``sim_engine/dry_run_core.py`` (``_QueueAwareFillModel``), rewritten on
decimal prices and the canonical book.

Fills are tagged with the execution mode that produced them. Simulated fills
are ``internal_simulation`` (backtest) or ``internal_paper`` (paper) and are
never labelled ``venue_testnet``; only a real venue adapter emits that label.
"""

from __future__ import annotations

import hashlib
import heapq
import itertools
import math
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Sequence

from trading_platform_poc.minreq.canonical import Liquidity, PriceLevel
from trading_platform_poc.minreq.fees import DepthWalk, walk_depth
from trading_platform_poc.minreq.latency import LatencyModel
from trading_platform_poc.minreq.quality import CanonicalBook

SECOND_NS = 1_000_000_000


class ExecutionMode(str, Enum):
    INTERNAL_SIMULATION = "internal_simulation"
    INTERNAL_PAPER = "internal_paper"
    VENUE_TESTNET = "venue_testnet"


SIMULATED_MODES = frozenset(
    {ExecutionMode.INTERNAL_SIMULATION, ExecutionMode.INTERNAL_PAPER}
)


class RejectReason(str, Enum):
    RATE_LIMIT = "rate_limit"
    NO_LIQUIDITY = "no_liquidity"
    FOK_UNFILLABLE = "fok_unfillable"
    POST_ONLY_WOULD_CROSS = "post_only_would_cross"
    BOOK_UNAVAILABLE = "book_unavailable"
    DUPLICATE_CLIENT_ORDER_ID = "duplicate_client_order_id"
    VENUE_UNAVAILABLE = "venue_unavailable"
    UNKNOWN_ORDER = "unknown_order"


@dataclass(frozen=True, slots=True)
class OrderRequest:
    client_order_id: str
    child_order_id: str
    plan_id: str
    venue: str
    instrument_id: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    time_in_force: str
    timestamp_ns: int
    post_only: bool = False
    risk_reducing: bool = False

    @property
    def is_buy(self) -> bool:
        return self.side.lower() == "buy"


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    client_order_id: str
    child_order_id: str
    plan_id: str
    venue: str
    instrument_id: str
    side: str
    quantity: Decimal
    price: Decimal
    timestamp_ns: int
    liquidity: Liquidity
    source: ExecutionMode
    levels_consumed: int
    fill_id: str = ""
    execution_timestamp_ns: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "client_order_id": self.client_order_id,
            "child_order_id": self.child_order_id,
            "plan_id": self.plan_id,
            "venue": self.venue,
            "instrument_id": self.instrument_id,
            "side": self.side,
            "quantity": str(self.quantity),
            "price": str(self.price),
            "timestamp_ns": self.timestamp_ns,
            "execution_timestamp_ns": self.execution_timestamp_ns,
            "liquidity": self.liquidity.value,
            "source": self.source.value,
            "levels_consumed": self.levels_consumed,
        }


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    request: OrderRequest
    accepted: bool
    state: str
    ack_timestamp_ns: int
    fills: tuple[SimulatedFill, ...]
    reject_reason: str = ""
    venue_order_id: str = ""
    resting_quantity: Decimal = Decimal(0)
    depth_walk: DepthWalk | None = None
    source: ExecutionMode = ExecutionMode.INTERNAL_SIMULATION
    arrival_timestamp_ns: int = 0
    expired_quantity: Decimal = Decimal(0)
    kind: str = "order"

    @property
    def filled_quantity(self) -> Decimal:
        return sum((fill.quantity for fill in self.fills), Decimal(0))

    @property
    def report_timestamp_ns(self) -> int:
        """When the sender has heard everything this outcome carries."""

        return max(
            [self.ack_timestamp_ns, *(fill.timestamp_ns for fill in self.fills)]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "client_order_id": self.request.client_order_id,
            "child_order_id": self.request.child_order_id,
            "accepted": self.accepted,
            "state": self.state,
            "arrival_timestamp_ns": self.arrival_timestamp_ns,
            "ack_timestamp_ns": self.ack_timestamp_ns,
            "filled_quantity": str(self.filled_quantity),
            "resting_quantity": str(self.resting_quantity),
            "expired_quantity": str(self.expired_quantity),
            "reject_reason": self.reject_reason,
            "venue_order_id": self.venue_order_id,
            "source": self.source.value,
            "fills": [fill.to_dict() for fill in self.fills],
            "depth_walk": (
                None if self.depth_walk is None else self.depth_walk.to_dict()
            ),
        }


@dataclass(frozen=True, slots=True)
class VenueOrderView:
    """What the venue would answer to an order-status query."""

    client_order_id: str
    state: str
    quantity: Decimal
    filled_quantity: Decimal
    average_price: Decimal | None
    fills: tuple[SimulatedFill, ...]
    reject_reason: str
    resting_quantity: Decimal
    arrival_timestamp_ns: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "state": self.state,
            "quantity": str(self.quantity),
            "filled_quantity": str(self.filled_quantity),
            "average_price": (
                None if self.average_price is None else str(self.average_price)
            ),
            "fills": [fill.to_dict() for fill in self.fills],
            "reject_reason": self.reject_reason,
            "resting_quantity": str(self.resting_quantity),
            "arrival_timestamp_ns": self.arrival_timestamp_ns,
        }


@dataclass(slots=True)
class RestingOrder:
    request: OrderRequest
    remaining: Decimal
    venue_order_id: str
    rested_at_ns: int
    queue_ahead: Decimal
    visible_at_price: Decimal = Decimal(0)


@dataclass(slots=True)
class _VenueOrder:
    request: OrderRequest
    arrival_ns: int
    state: str = "acknowledged"
    filled_quantity: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)
    fills: list[SimulatedFill] = field(default_factory=list)
    reject_reason: str = ""
    resting_quantity: Decimal = Decimal(0)

    def view(self) -> VenueOrderView:
        return VenueOrderView(
            client_order_id=self.request.client_order_id,
            state=self.state,
            quantity=self.request.quantity,
            filled_quantity=self.filled_quantity,
            average_price=(
                self.notional / self.filled_quantity
                if self.filled_quantity > 0
                else None
            ),
            fills=tuple(self.fills),
            reject_reason=self.reject_reason,
            resting_quantity=self.resting_quantity,
            arrival_timestamp_ns=self.arrival_ns,
        )


@dataclass(frozen=True, slots=True)
class MatchingConfig:
    mode: ExecutionMode = ExecutionMode.INTERNAL_SIMULATION
    max_orders_per_second: int = 20
    maker_queue_factor: Decimal = Decimal("1.0")
    partial_fill_enabled: bool = True
    reject_on_exhausted_book: bool = True
    liquidity_half_life_ns: int = 250_000_000

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "max_orders_per_second": self.max_orders_per_second,
            "maker_queue_factor": str(self.maker_queue_factor),
            "partial_fill_enabled": self.partial_fill_enabled,
            "reject_on_exhausted_book": self.reject_on_exhausted_book,
            "liquidity_half_life_ns": self.liquidity_half_life_ns,
        }


def _book_side(taker_side: str) -> str:
    """A taker buy consumes asks; a taker sell consumes bids."""

    return "ask" if taker_side == "buy" else "bid"


class PaperMatchingEngine:
    """Deterministic matcher driven by the replayed canonical book."""

    def __init__(
        self,
        books: Mapping[str, CanonicalBook],
        latency: LatencyModel,
        *,
        config: MatchingConfig | None = None,
    ) -> None:
        self._books = books
        self._latency = latency
        self.config = config or MatchingConfig()
        # (venue, book_side, price) -> [consumed quantity, published size when last seen]
        self._consumed: dict[tuple[str, str, Decimal], list[Decimal]] = {}
        self._snapshot_marker: dict[str, int] = {}
        self._rate_windows: dict[str, deque[int]] = {}
        self._resting: dict[str, RestingOrder] = {}
        self._seen_client_ids: set[str] = set()
        self._venue_orders: dict[str, _VenueOrder] = {}
        self._in_flight: list[tuple[int, int, str, OrderRequest]] = []
        self._sequence = itertools.count()
        self._fill_sequence: dict[str, int] = {}
        self.unavailable_venues: set[str] = set()
        self.rejects: list[dict[str, Any]] = []
        self.positions: dict[tuple[str, str], Decimal] = {}
        self.fills: list[SimulatedFill] = []
        self.orders_received = 0

    @property
    def mode(self) -> ExecutionMode:
        return self.config.mode

    def _record_fill(self, fill: SimulatedFill) -> None:
        """The venue's own view of what it filled, kept apart from the ledger."""

        key = (fill.instrument_id, fill.venue)
        signed = fill.quantity if fill.side == "buy" else -fill.quantity
        self.positions[key] = self.positions.get(key, Decimal(0)) + signed
        self.fills.append(fill)
        record = self._venue_orders.get(fill.client_order_id)
        if record is not None:
            record.fills.append(fill)
            record.filled_quantity += fill.quantity
            record.notional += fill.quantity * fill.price

    def _fill_id(self, client_order_id: str) -> str:
        index = self._fill_sequence.get(client_order_id, 0)
        self._fill_sequence[client_order_id] = index + 1
        return hashlib.sha256(f"{client_order_id}|{index}".encode()).hexdigest()[:24]

    def inject_position_break(
        self, instrument_id: str, venue: str, delta: Decimal
    ) -> None:
        """Simulate a venue-side discrepancy so reconciliation can be tested."""

        key = (instrument_id, venue)
        self.positions[key] = self.positions.get(key, Decimal(0)) + delta

    # ------------------------------------------------------------- utilities
    def _book(self, venue: str) -> CanonicalBook | None:
        return self._books.get(venue)

    def _refresh(self, venue: str, book: CanonicalBook) -> None:
        """Release consumed liquidity only as the venue's book reflects it."""

        marker = book.snapshot_count
        if self._snapshot_marker.get(venue) != marker:
            self._snapshot_marker[venue] = marker
            for key in [key for key in self._consumed if key[0] == venue]:
                del self._consumed[key]
            return
        for key in [key for key in self._consumed if key[0] == venue]:
            taken, published = self._consumed[key]
            current = book.level_quantity(key[1], key[2])
            if current <= 0:
                del self._consumed[key]
                continue
            if current < published:
                taken -= published - current
                if taken <= 0:
                    del self._consumed[key]
                    continue
            self._consumed[key] = [taken, current]

    def _available_levels(
        self, venue: str, book: CanonicalBook, side: str
    ) -> list[PriceLevel]:
        self._refresh(venue, book)
        book_side = _book_side(side)
        levels: list[PriceLevel] = []
        for level in book.depth(side):
            used = self._consumed.get((venue, book_side, level.price))
            remaining = level.quantity - (used[0] if used else Decimal(0))
            if remaining > 0:
                levels.append(PriceLevel(price=level.price, quantity=remaining))
        return levels

    def available_depth(self, venue: str, side: str) -> tuple[PriceLevel, ...]:
        """Executable depth for a taker ``side`` after our own consumption."""

        book = self._book(venue)
        if book is None or not book.initialized:
            return ()
        return tuple(self._available_levels(venue, book, side))

    def _consume(
        self,
        venue: str,
        book: CanonicalBook,
        side: str,
        levels: Sequence[PriceLevel],
        quantity: Decimal,
    ) -> None:
        book_side = _book_side(side)
        remaining = quantity
        for level in levels:
            if remaining <= 0:
                break
            take = level.quantity if level.quantity < remaining else remaining
            key = (venue, book_side, level.price)
            used = self._consumed.get(key)
            self._consumed[key] = [
                (used[0] if used else Decimal(0)) + take,
                book.level_quantity(book_side, level.price),
            ]
            remaining -= take

    def _rate_limited(self, venue: str, timestamp_ns: int) -> bool:
        window = self._rate_windows.setdefault(venue, deque())
        start = timestamp_ns - SECOND_NS
        while window and window[0] < start:
            window.popleft()
        if len(window) >= self.config.max_orders_per_second:
            return True
        window.append(timestamp_ns)
        return False

    def _reject(
        self,
        request: OrderRequest,
        reason: RejectReason,
        ack_ns: int,
        arrival_ns: int,
    ) -> MatchOutcome:
        self.rejects.append(
            {
                "timestamp_ns": ack_ns,
                "venue": request.venue,
                "instrument_id": request.instrument_id,
                "client_order_id": request.client_order_id,
                "reason": reason.value,
            }
        )
        record = self._venue_orders.get(request.client_order_id)
        if record is not None and record.request is request:
            record.state = "rejected"
            record.reject_reason = reason.value
        return MatchOutcome(
            request=request,
            accepted=False,
            state="rejected",
            ack_timestamp_ns=ack_ns,
            fills=(),
            reject_reason=reason.value,
            source=self.config.mode,
            arrival_timestamp_ns=arrival_ns,
        )

    # ------------------------------------------------------------ in flight
    def send(self, request: OrderRequest) -> int:
        """Put an order on the wire. Returns its arrival time at the venue."""

        arrival = request.timestamp_ns + self._latency.draw(request.venue, "entry")
        heapq.heappush(
            self._in_flight, (arrival, next(self._sequence), "order", request)
        )
        return arrival

    def request_cancel(self, client_order_id: str, timestamp_ns: int) -> int | None:
        """Send a cancel. It lands after the cancel latency, not immediately."""

        resting = self._resting.get(client_order_id)
        record = self._venue_orders.get(client_order_id)
        request = (
            resting.request
            if resting is not None
            else (record.request if record is not None else None)
        )
        if request is None:
            request = next(
                (
                    item[3]
                    for item in self._in_flight
                    if item[2] == "order" and item[3].client_order_id == client_order_id
                ),
                None,
            )
        if request is None:
            return None
        landing = timestamp_ns + self._latency.draw(request.venue, "cancel")
        heapq.heappush(
            self._in_flight, (landing, next(self._sequence), "cancel", request)
        )
        return landing

    def next_event_ns(self) -> int | None:
        return self._in_flight[0][0] if self._in_flight else None

    @property
    def in_flight_count(self) -> int:
        return len(self._in_flight)

    def release(self, now_ns: int) -> list[MatchOutcome]:
        """Execute every order and cancel that has reached the venue by now."""

        outcomes: list[MatchOutcome] = []
        while self._in_flight and self._in_flight[0][0] <= now_ns:
            arrival, _, kind, request = heapq.heappop(self._in_flight)
            if kind == "order":
                outcomes.append(self._execute(request, arrival))
            else:
                outcomes.append(self._land_cancel(request, arrival))
        return outcomes

    def submit(self, request: OrderRequest) -> MatchOutcome:
        """Send and execute immediately against the current book.

        Kept for unit tests of matching mechanics; the router always uses
        :meth:`send` and :meth:`release` so latency moves the market.
        """

        arrival = request.timestamp_ns + self._latency.draw(request.venue, "entry")
        return self._execute(request, arrival)

    # ---------------------------------------------------------------- execute
    def _execute(self, request: OrderRequest, arrival_ns: int) -> MatchOutcome:
        ack_ns = arrival_ns + self._latency.draw(request.venue, "submit_ack")
        self.orders_received += 1
        if request.client_order_id in self._seen_client_ids:
            return self._reject(
                request, RejectReason.DUPLICATE_CLIENT_ORDER_ID, ack_ns, arrival_ns
            )
        self._seen_client_ids.add(request.client_order_id)
        self._venue_orders[request.client_order_id] = _VenueOrder(
            request=request, arrival_ns=arrival_ns
        )

        if request.venue in self.unavailable_venues:
            return self._reject(
                request, RejectReason.VENUE_UNAVAILABLE, ack_ns, arrival_ns
            )
        if self._rate_limited(request.venue, arrival_ns):
            return self._reject(request, RejectReason.RATE_LIMIT, ack_ns, arrival_ns)

        book = self._book(request.venue)
        if book is None or not book.initialized:
            return self._reject(
                request, RejectReason.BOOK_UNAVAILABLE, ack_ns, arrival_ns
            )

        side = "buy" if request.is_buy else "sell"
        levels = self._available_levels(request.venue, book, side)
        if request.order_type.lower() == "limit" and request.price is not None:
            levels = [
                level
                for level in levels
                if (
                    level.price <= request.price
                    if request.is_buy
                    else level.price >= request.price
                )
            ]

        if request.post_only:
            return self._rest(
                request, ack_ns, arrival_ns, book, marketable=bool(levels)
            )

        if not levels:
            if request.time_in_force.lower() in {"ioc", "fok"}:
                return self._reject(
                    request, RejectReason.NO_LIQUIDITY, ack_ns, arrival_ns
                )
            return self._rest(request, ack_ns, arrival_ns, book, marketable=False)

        reference = book.mid_price or levels[0].price
        walk = walk_depth(levels, request.quantity, reference, side)
        if walk.filled_quantity <= 0:
            return self._reject(request, RejectReason.NO_LIQUIDITY, ack_ns, arrival_ns)
        if (
            request.time_in_force.lower() == "fok"
            and walk.filled_quantity < request.quantity
        ):
            return self._reject(
                request, RejectReason.FOK_UNFILLABLE, ack_ns, arrival_ns
            )
        if (
            not self.config.partial_fill_enabled
            and walk.filled_quantity < request.quantity
        ):
            return self._reject(request, RejectReason.NO_LIQUIDITY, ack_ns, arrival_ns)

        self._consume(request.venue, book, side, levels, walk.filled_quantity)
        fill = SimulatedFill(
            client_order_id=request.client_order_id,
            child_order_id=request.child_order_id,
            plan_id=request.plan_id,
            venue=request.venue,
            instrument_id=request.instrument_id,
            side=side,
            quantity=walk.filled_quantity,
            price=walk.average_price,
            timestamp_ns=arrival_ns + self._latency.draw(request.venue, "fill"),
            liquidity=Liquidity.TAKER,
            source=self.config.mode,
            levels_consumed=walk.levels_consumed,
            fill_id=self._fill_id(request.client_order_id),
            execution_timestamp_ns=arrival_ns,
        )
        self._record_fill(fill)
        record = self._venue_orders[request.client_order_id]
        remaining = request.quantity - walk.filled_quantity
        resting = Decimal(0)
        expired = Decimal(0)
        if remaining <= 0:
            state = "filled"
        elif request.time_in_force.lower() == "gtc":
            state = "partially_filled"
            resting = remaining
            self._resting[request.client_order_id] = RestingOrder(
                request=request,
                remaining=remaining,
                venue_order_id=_venue_order_id(request),
                rested_at_ns=arrival_ns,
                queue_ahead=Decimal(0),
                visible_at_price=Decimal(0),
            )
        else:
            # IOC: whatever did not trade on arrival is cancelled by the venue.
            state = "cancelled"
            expired = remaining
        record.state = state
        record.resting_quantity = resting
        return MatchOutcome(
            request=request,
            accepted=True,
            state=state,
            ack_timestamp_ns=ack_ns,
            fills=(fill,),
            venue_order_id=_venue_order_id(request),
            resting_quantity=resting,
            depth_walk=walk,
            source=self.config.mode,
            arrival_timestamp_ns=arrival_ns,
            expired_quantity=expired,
        )

    def _rest(
        self,
        request: OrderRequest,
        ack_ns: int,
        arrival_ns: int,
        book: CanonicalBook,
        *,
        marketable: bool,
    ) -> MatchOutcome:
        if request.post_only and marketable:
            return self._reject(
                request, RejectReason.POST_ONLY_WOULD_CROSS, ack_ns, arrival_ns
            )
        if request.price is None:
            return self._reject(request, RejectReason.NO_LIQUIDITY, ack_ns, arrival_ns)
        visible = book.level_quantity(
            "bid" if request.is_buy else "ask", request.price
        )
        self._resting[request.client_order_id] = RestingOrder(
            request=request,
            remaining=request.quantity,
            venue_order_id=_venue_order_id(request),
            rested_at_ns=arrival_ns,
            queue_ahead=visible * self.config.maker_queue_factor,
            visible_at_price=visible,
        )
        record = self._venue_orders[request.client_order_id]
        record.state = "acknowledged"
        record.resting_quantity = request.quantity
        return MatchOutcome(
            request=request,
            accepted=True,
            state="acknowledged",
            ack_timestamp_ns=ack_ns,
            fills=(),
            venue_order_id=_venue_order_id(request),
            resting_quantity=request.quantity,
            source=self.config.mode,
            arrival_timestamp_ns=arrival_ns,
        )

    # ------------------------------------------------------------ maker flow
    def on_book_update(self, venue: str) -> None:
        """Infer cancellations ahead of resting orders from level shrinkage."""

        book = self._book(venue)
        if book is None:
            return
        for resting in self._resting.values():
            request = resting.request
            if request.venue != venue or request.price is None:
                continue
            current = book.level_quantity(
                "bid" if request.is_buy else "ask", request.price
            )
            previous = resting.visible_at_price
            if current < previous and previous > 0 and resting.queue_ahead > 0:
                cancelled = (previous - current) * resting.queue_ahead / previous
                resting.queue_ahead = max(Decimal(0), resting.queue_ahead - cancelled)
            resting.visible_at_price = current

    def on_trade(
        self,
        venue: str,
        instrument_id: str,
        price: Decimal,
        quantity: Decimal,
        aggressor_side: str,
        timestamp_ns: int,
    ) -> list[SimulatedFill]:
        """Fill resting maker orders that the printed trade traded through."""

        fills: list[SimulatedFill] = []
        for client_order_id in sorted(self._resting):
            resting = self._resting[client_order_id]
            request = resting.request
            if request.venue != venue or request.instrument_id != instrument_id:
                continue
            if request.price is None or resting.rested_at_ns > timestamp_ns:
                continue
            crossed = (
                aggressor_side == "sell" and price <= request.price
                if request.is_buy
                else aggressor_side == "buy" and price >= request.price
            )
            if not crossed:
                continue
            available = quantity
            if resting.queue_ahead > 0:
                consumed = min(resting.queue_ahead, available)
                resting.queue_ahead -= consumed
                resting.visible_at_price = max(
                    Decimal(0), resting.visible_at_price - consumed
                )
                available -= consumed
            if available <= 0:
                continue
            traded = min(available, resting.remaining)
            resting.remaining -= traded
            quantity -= traded
            fills.append(
                SimulatedFill(
                    client_order_id=request.client_order_id,
                    child_order_id=request.child_order_id,
                    plan_id=request.plan_id,
                    venue=venue,
                    instrument_id=instrument_id,
                    side="buy" if request.is_buy else "sell",
                    quantity=traded,
                    price=request.price,
                    timestamp_ns=timestamp_ns + self._latency.draw(venue, "fill"),
                    liquidity=Liquidity.MAKER,
                    source=self.config.mode,
                    levels_consumed=1,
                    fill_id=self._fill_id(request.client_order_id),
                    execution_timestamp_ns=timestamp_ns,
                )
            )
            record = self._venue_orders.get(client_order_id)
            if resting.remaining <= 0:
                del self._resting[client_order_id]
                if record is not None:
                    record.state = "filled"
                    record.resting_quantity = Decimal(0)
            elif record is not None:
                record.state = "partially_filled"
                record.resting_quantity = resting.remaining
            if quantity <= 0:
                break
        for fill in fills:
            self._record_fill(fill)
        return fills

    def _land_cancel(self, request: OrderRequest, landing_ns: int) -> MatchOutcome:
        ack_ns = landing_ns + self._latency.draw(request.venue, "submit_ack")
        resting = self._resting.pop(request.client_order_id, None)
        record = self._venue_orders.get(request.client_order_id)
        if resting is None:
            # Already filled, already expired, or not yet arrived: the venue
            # rejects the cancel and the order keeps whatever it already did.
            return MatchOutcome(
                request=request,
                accepted=False,
                state="cancel_rejected",
                ack_timestamp_ns=ack_ns,
                fills=(),
                reject_reason=RejectReason.UNKNOWN_ORDER.value,
                source=self.config.mode,
                arrival_timestamp_ns=landing_ns,
                kind="cancel",
            )
        if record is not None:
            record.state = "cancelled"
            record.resting_quantity = Decimal(0)
        return MatchOutcome(
            request=request,
            accepted=True,
            state="cancelled",
            ack_timestamp_ns=ack_ns,
            fills=(),
            venue_order_id=resting.venue_order_id,
            source=self.config.mode,
            arrival_timestamp_ns=landing_ns,
            expired_quantity=resting.remaining,
            kind="cancel",
        )

    def cancel(self, client_order_id: str, timestamp_ns: int) -> MatchOutcome | None:
        """Cancel a resting order immediately (operator drills and tests)."""

        resting = self._resting.pop(client_order_id, None)
        if resting is None:
            return None
        record = self._venue_orders.get(client_order_id)
        if record is not None:
            record.state = "cancelled"
            record.resting_quantity = Decimal(0)
        ack_ns = timestamp_ns + self._latency.draw(resting.request.venue, "cancel")
        return MatchOutcome(
            request=resting.request,
            accepted=True,
            state="cancelled",
            ack_timestamp_ns=ack_ns,
            fills=(),
            venue_order_id=resting.venue_order_id,
            source=self.config.mode,
            arrival_timestamp_ns=timestamp_ns,
            expired_quantity=resting.remaining,
            kind="cancel",
        )

    # ------------------------------------------------------------- queries
    def query_order(
        self, client_order_id: str, as_of_ns: int
    ) -> VenueOrderView | None:
        """Order status as the venue knows it. ``None`` means unknown order."""

        record = self._venue_orders.get(client_order_id)
        if record is None or record.arrival_ns > as_of_ns:
            return None
        return record.view()

    def expected_fill_probability(
        self,
        venue: str,
        side: str,
        quantity: Decimal,
        limit_price: Decimal | None,
    ) -> Decimal:
        """Chance a taker order still fills after its entry latency.

        Depth coverage within the limit, decayed by how long the order is in
        flight relative to the configured liquidity half-life.
        """

        if quantity <= 0:
            return Decimal(0)
        levels = self.available_depth(venue, side)
        if limit_price is not None:
            levels = tuple(
                level
                for level in levels
                if (level.price <= limit_price if side == "buy" else level.price >= limit_price)
            )
        coverage = min(
            Decimal(1),
            sum((level.quantity for level in levels), Decimal(0)) / quantity,
        )
        half_life = max(1, self.config.liquidity_half_life_ns)
        decay = math.pow(0.5, self._latency.expected_ns(venue, "entry") / half_life)
        return (coverage * Decimal(repr(round(decay, 6)))).quantize(Decimal("0.000001"))

    def open_orders(self) -> tuple[RestingOrder, ...]:
        return tuple(self._resting[key] for key in sorted(self._resting))

    def venue_snapshot(self) -> dict[str, Any]:
        """Shape the reconciliation routine expects from a venue."""

        return {
            "orders": {
                key: {
                    "state": "acknowledged",
                    "filled_quantity": str(
                        resting.request.quantity - resting.remaining
                    ),
                    "leaves_quantity": str(resting.remaining),
                }
                for key, resting in sorted(self._resting.items())
            },
            "positions": {
                f"{instrument_id}@{venue}": str(quantity)
                for (instrument_id, venue), quantity in sorted(
                    self.positions.items()
                )
            },
        }

    def set_venue_available(self, venue: str, available: bool) -> None:
        if available:
            self.unavailable_venues.discard(venue)
        else:
            self.unavailable_venues.add(venue)


def _venue_order_id(request: OrderRequest) -> str:
    key = f"{request.venue}|{request.client_order_id}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
