"""QueueAwareVenueAdapter (Phase D, step D3): the simulated venue for the arb maker leg, behind ``VenueExecutionAdapter``.

A resting post-only order is placed in ``MakerExecutor`` (queue v2) and fills only where the executor says so. The executor requires a real print for every fill
(``fill_evidence`` in a trade-confirm mode); the adapter refuses an executor without that rule, so a depth change can move the queue but never fills an order
(CLAUDE.md: a fill needs a real trade). Entry points:

  handle(request, book)   post-only GTC limit -> executor.place; cancel and replace of its own orders; everything else (IOC, FOK, market: taker fills) goes to
                          ``DeterministicVenueAdapter``, the class this one extends and keeps for taker fills only (F9: its resting fills come from a book cross, not a print)
  on_trade(trade)         the print: queue drain and fills (the hook the router calls beside ``on_book_update``, D2)
  on_book_update(book)    queue movement from the levels our orders rest on (``on_delta``) and the opposite-cross rule; fills only through the executor's evidence rules

The float/Decimal conversion lives here and nowhere else: the executor works in float seconds and float prices, the core in Decimal and nanoseconds.
Known difference from the backtester's own loop, recorded for the D5 parity manifest: a core book update does not say whether it was a snapshot or a delta, so the snapshot-shrink
signal (``on_snapshot``) is not fed; queue movement comes from the level diff.
"""
from __future__ import annotations

from decimal import Decimal

from ..execution import (
    CanonicalOrderRequest, DeterministicVenueAdapter, ExecutionCallback, ExecutionEventType, OrderAction, OrderType, TimeInForce,
)
from ..models import Side as CoreSide
from ..models import TradeEvent
from ..orderbook import OrderBook as CoreBook
from .fill_engine import Fill, MakerExecutor
from .models import Side as SimSide
from .order_book import OrderBook as SimBook, core_book_from_float, float_mirror
from .postonly import would_cross
from .venue_state import VenueState

# fill_evidence modes in which every fill that rests on a depth change needs a confirming print (see MakerConfig.fill_evidence).
EVIDENCE_REQUIRED_MODES = ("trade_confirm", "trade_confirm+fractional", "confirm_delta_snapshot", "confirm_delta")
_NS = 1_000_000_000          # an int: ns / _NS is the correctly rounded float seconds (dividing by 1e9 loses about 100 ns at today's epoch)
_EPS = 1e-12


class QueueAwareVenueAdapter(DeterministicVenueAdapter):
    def __init__(
        self, venue: str, executor: MakerExecutor, *, tick_size: float, maker_fee_bps: float, pair: str = "",
        state: VenueState | None = None, truth=None, feed_venue: str = "", view_book=None, driver_fed: bool = False,
    ) -> None:
        if executor.config.fill_evidence not in EVIDENCE_REQUIRED_MODES:
            raise ValueError(
                f"QueueAwareVenueAdapter needs MakerConfig.fill_evidence in {EVIDENCE_REQUIRED_MODES}, got {executor.config.fill_evidence!r}: "
                "a fill needs a real trade, so a depth change must never fill an order on its own"
            )
        super().__init__(venue, consume_liquidity=True)
        self.executor = executor
        self.tick_size = float(tick_size)
        self.maker_fee_bps = float(maker_fee_bps)
        self.pair = pair
        # A replay driver that owns the float view books (`view_book()` returns this venue's persistent view) and feeds the executor itself, exactly as the backtester's event loop does (`feed_*`): the
        # router's per-event hooks below then do nothing, so the executor sees every snapshot, delta, print and cross check once.
        self.view_book = view_book
        self.driver_fed = driver_fed
        self.truth = truth                                             # timing.TruthBooks (the shared rule) or None: taker arrivals execute against the venue-truth book, PostOnly is judged on it when complete
        self.feed_venue = feed_venue or venue.lower()
        self._owns_state = state is None
        self.state = state or VenueState(venue)
        self._maker_id: dict[str, int] = {}                            # core order id -> executor order id
        self._order: dict[int, CanonicalOrderRequest] = {}             # executor order id -> the request that placed it
        self._cum: dict[str, tuple[float, float]] = {}                 # core order id -> (filled quantity, notional)
        self._seen_size: dict[tuple[str, float], float] = {}           # (side, price) -> displayed size at the last book update we looked at
        self._arrival_ns: int | None = None                            # set by handle_at for the one request being handled
        self.fills_by_source: dict[str, int] = {}                      # cause of each fill the executor produced (trade, delta, ...): evidence for the parity manifest

    # ---- boundary conversions -------------------------------------------------------------------------------------------------

    @staticmethod
    def _secs(ns: int) -> float:
        return ns / _NS

    def _mirror(self, book: CoreBook, now: float) -> SimBook:
        """The core's Decimal book as the float book the executor reads. Rebuilt from the current levels, never mutated by our orders."""
        mirror = float_mirror(self.venue, self.pair or book.instrument_id, book.bids, book.asks, now)
        if self._owns_state:
            self.state.on_book(mirror, now)               # an externally supplied state is updated by its owner, once per market event
        return mirror

    def _view(self, book: CoreBook, now: float) -> SimBook:
        return self.view_book() if self.view_book is not None else self._mirror(book, now)

    def _decimal(self, value: float) -> Decimal:
        return Decimal(repr(round(value, 12)))

    # ---- requests -------------------------------------------------------------------------------------------------------------

    def handle_at(self, request: CanonicalOrderRequest, book: CoreBook | None, arrival_ts_ns: int) -> tuple[ExecutionCallback, ...]:
        """The router calls this with the time the request reached the venue (creation time plus entry latency): the order is placed, or cancelled, then, against the book as it is at arrival."""
        self._arrival_ns = arrival_ts_ns
        try:
            return self.handle(request, book)
        finally:
            self._arrival_ns = None

    def _at(self, request: CanonicalOrderRequest) -> float:
        return self._secs(request.created_ts_ns if self._arrival_ns is None else self._arrival_ns)

    def handle(self, request: CanonicalOrderRequest, book: CoreBook | None) -> tuple[ExecutionCallback, ...]:
        mine = request.action == OrderAction.CANCEL and (request.target_order_id or request.order_id) in self._maker_id
        replace_mine = request.action == OrderAction.REPLACE and (request.target_order_id or "") in self._maker_id
        resting = (
            request.action == OrderAction.SUBMIT and request.time_in_force == TimeInForce.GTC
            and request.order_type == OrderType.LIMIT
        )
        if not (mine or replace_mine or resting):
            callbacks = super().handle(request, self._taker_book(request, book))      # IOC / FOK / market: a taker fill, the deterministic walk
            if (request.time_in_force == TimeInForce.IOC and len(callbacks) == 1 and callbacks[0].event_type == ExecutionEventType.REJECTED
                    and callbacks[0].reason == "no executable liquidity"):
                # An IOC that finds nothing inside its limit is not a venue reject: the exchange ends it unfilled (Bybit "Cancelled", Binance "EXPIRED"). The OMS then sees an unfilled IOC (retry or unwind by its own rule)
                # instead of an unclassified reject (which fails closed to an unwind at once).
                callbacks = (self._callback(request, ExecutionEventType.CANCELLED, reason="IOC expired unfilled"),)
                self._remember(callbacks)
            return callbacks
        if resting and not request.post_only:
            callbacks: tuple[ExecutionCallback, ...] = (self._callback(
                request, ExecutionEventType.REJECTED,
                reason="queue adapter rests post-only orders only; a marketable order is a taker fill",
            ),)
        elif mine:
            callbacks = self._cancel_own(request)
        elif replace_mine:
            callbacks = self._replace_own(request, book)
        else:
            callbacks = self._place(request, book)
        self._remember(callbacks)
        return callbacks

    def _taker_book(self, request: CanonicalOrderRequest, book: CoreBook | None) -> CoreBook | None:
        """A taker arrival executes against the venue-truth book at its arrival's exchange time (hedge_arrival_book 'truth'); the view when the truth book is not ready."""
        if self.truth is None or self._arrival_ns is None or book is None or request.action != OrderAction.SUBMIT:
            return book
        now = self._secs(self._arrival_ns)
        view = self._view(book, now)
        chosen = self.truth.at_arrival(self.feed_venue, now, view)
        return book if chosen is view else core_book_from_float(self.venue, book.instrument_id, chosen, self._arrival_ns)

    def _place(self, request: CanonicalOrderRequest, book: CoreBook | None) -> tuple[ExecutionCallback, ...]:
        if book is None or not book.is_tradable:
            return (self._callback(request, ExecutionEventType.REJECTED, reason="no valid book at arrival"),)
        side = "buy" if request.side == CoreSide.BUY else "sell"
        best_bid, best_ask = float(book.best_bid), float(book.best_ask)
        if self.truth is not None and self._arrival_ns is not None:
            if self.truth.complete_at(self.feed_venue, self._secs(self._arrival_ns)):
                tb = self.truth.books[self.feed_venue]                                 # PostOnly is judged at ARRIVAL on the book the matching engine held (postonly_reject_model 'on'), when that book is complete
                best_bid, best_ask = tb.best_bid, tb.best_ask
                self.truth.stats["postonly_checks_truth_at_arrival"] += 1
            else:                                                                      # the backtester waits for a later event here; this path checks the view now (counted: BUILD item if it ever happens)
                self.truth.stats["postonly_view_fallback_truth_incomplete"] += 1
        if would_cross(side, float(request.price), best_bid, best_ask):
            return (self._callback(request, ExecutionEventType.REJECTED, reason="postonly_would_take_liquidity"),)
        now = self._at(request)
        mirror = self._view(book, now)
        order = self.executor.place(
            venue=self.venue, pair=self.pair or request.instrument_id, symbol=self.pair or request.instrument_id,
            side=SimSide.BID if side == "buy" else SimSide.ASK, price=float(request.price), size=float(request.quantity),
            book=mirror, state=self.state.to_market_state(mirror, now), regime_bucket=self.state.regime().bucket, now=now,
            active_ts=now, tick_size=self.tick_size, arrival_ts=now,        # placed at arrival: queue position and the pre-arrival gate are judged from then
        )
        lc_accept = getattr(self.executor, "lc_accept", None)
        if lc_accept is not None:
            lc_accept(order.order_id, order, now, "modelled")             # the executor's lifecycle record, as the backtester's _place_passive
        self._maker_id[request.order_id] = order.order_id
        self._order[order.order_id] = request
        self._cum[request.order_id] = (0.0, 0.0)
        return (self._callback(request, ExecutionEventType.ACKNOWLEDGED),)

    def _cancel_own(self, request: CanonicalOrderRequest) -> tuple[ExecutionCallback, ...]:
        target = request.target_order_id or request.order_id
        order = self.executor.cancel(self._maker_id[target], self._at(request))
        if order is None:
            return (self._callback(request, ExecutionEventType.REJECTED, reason="cancel target is not open"),)
        filled, notional = self._cum.get(target, (0.0, 0.0))
        return (self._callback(
            request, ExecutionEventType.CANCELLED, filled=self._decimal(filled),
            average_price=self._decimal(notional / filled) if filled > _EPS else Decimal("0"),
        ),)

    def _replace_own(self, request: CanonicalOrderRequest, book: CoreBook | None) -> tuple[ExecutionCallback, ...]:
        target = request.target_order_id or ""
        old = self.executor.cancel(self._maker_id[target], self._at(request))
        if old is None:
            return (self._callback(request, ExecutionEventType.REJECTED, reason="replace target is not open"),)
        replaced = self._callback(request, ExecutionEventType.REPLACED)
        return (replaced, *self._place(request, book))                 # the new order loses its queue place, like an amend on the venue

    # ---- market data ----------------------------------------------------------------------------------------------------------

    def on_trade(self, trade: TradeEvent) -> tuple[tuple[CanonicalOrderRequest, ExecutionCallback], ...]:
        if trade.venue != self.venue or self.driver_fed:
            return ()
        now = self._secs(trade.receive_ts_ns)
        sign = 1 if trade.aggressor_side == CoreSide.BUY.value else -1
        if self._owns_state:                                           # an externally supplied state is fed by its owner, once per print
            self.state.on_trade(now, float(trade.price), float(trade.quantity), sign)
        fills = self.executor.on_trade(
            self.venue, float(trade.price), float(trade.quantity), sign, now, self.maker_fee_bps,
            event_ts=self._secs(trade.exchange_ts_ns),
        )
        return self._relay(fills)

    def on_book_update(self, book: CoreBook) -> tuple[tuple[CanonicalOrderRequest, ExecutionCallback], ...]:
        if self.driver_fed:
            return ()
        live = self.executor.live_orders(self.venue)
        if not live or not book.is_tradable:
            self._seen_size.clear()
            return ()
        now = self._secs(book.last_receive_ts_ns or 0)
        fills: list[Fill] = []
        for key in sorted({("bid" if o.side == SimSide.BID else "ask", o.price) for o in live}):
            side, price = key
            levels = book.bids if side == "bid" else book.asks
            size = float(levels.get(Decimal(repr(price)), Decimal("0")))
            if key in self._seen_size and abs(self._seen_size[key] - size) > _EPS:
                fills += self.executor.on_delta(self.venue, side, price, size, now, self.tick_size, self.maker_fee_bps)
            self._seen_size[key] = size
        if self.executor.config.opposite_cross_fills:
            fills += self.executor.on_opposite_cross(self.venue, self._mirror(book, now), now, self.maker_fee_bps)
        return self._relay(fills)

    # ---- the driver feeds the executor (the backtester loop's calls, in its order) ---------------------------------------------

    def feed_snapshot(self, view: SimBook, now: float, ets: float | None):
        self.executor.mark_adverse(self.venue, view, now, self.tick_size)
        return self._relay(self.executor.on_snapshot(self.venue, view, now, self.tick_size, self.maker_fee_bps, event_ts=ets))

    def feed_delta(self, side: str, price: float, new_size: float, now: float, ets: float | None):
        return self._relay(self.executor.on_delta(self.venue, side, price, new_size, now, self.tick_size, self.maker_fee_bps, event_ts=ets))

    def feed_trade(self, price: float, volume: float, sign: int, now: float, ets: float | None):
        return self._relay(self.executor.on_trade(self.venue, price, volume, sign, now, self.maker_fee_bps, event_ts=ets))

    def feed_cross(self, view: SimBook, now: float, ets: float | None):
        if not self.executor.config.opposite_cross_fills:
            return ()
        return self._relay(self.executor.on_opposite_cross(self.venue, view, now, self.maker_fee_bps, event_ts=ets))

    # ---- fills -> reports -----------------------------------------------------------------------------------------------------

    def _relay(self, fills: list[Fill]) -> tuple[tuple[CanonicalOrderRequest, ExecutionCallback], ...]:
        out = []
        for fill in fills:
            request = self._order.get(fill.order_id)
            if request is None or fill.qty <= 0:
                continue
            self.fills_by_source[fill.cause or "unknown"] = self.fills_by_source.get(fill.cause or "unknown", 0) + 1
            filled, notional = self._cum[request.order_id]
            filled, notional = filled + fill.qty, notional + fill.qty * fill.price
            self._cum[request.order_id] = (filled, notional)
            complete = filled >= float(request.quantity) - _EPS
            cumulative = request.quantity if complete else self._decimal(filled)
            callback = self._callback(
                request, ExecutionEventType.FILLED if complete else ExecutionEventType.PARTIAL, filled=cumulative,
                last_fill=self._decimal(fill.qty), average_price=self._decimal(notional / filled), liquidity="MAKER",
            )
            if complete:
                self._maker_id.pop(request.order_id, None)
                self.terminal_orders.add(request.order_id)
            self._remember((callback,))
            out.append((request, callback))
        return tuple(out)
