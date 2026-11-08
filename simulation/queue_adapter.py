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
from .order_book import OrderBook as SimBook, float_mirror
from .postonly import would_cross
from .venue_state import VenueState

# fill_evidence modes in which every fill that rests on a depth change needs a confirming print (see MakerConfig.fill_evidence).
EVIDENCE_REQUIRED_MODES = ("trade_confirm", "trade_confirm+fractional", "confirm_delta_snapshot", "confirm_delta")
_NS = 1_000_000_000          # an int: ns / _NS is the correctly rounded float seconds (dividing by 1e9 loses about 100 ns at today's epoch)
_EPS = 1e-12


class QueueAwareVenueAdapter(DeterministicVenueAdapter):
    def __init__(
        self, venue: str, executor: MakerExecutor, *, tick_size: float, maker_fee_bps: float, pair: str = "",
        state: VenueState | None = None,
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
            return super().handle(request, book)                       # IOC / FOK / market: a taker fill, the deterministic walk
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

    def _place(self, request: CanonicalOrderRequest, book: CoreBook | None) -> tuple[ExecutionCallback, ...]:
        if book is None or not book.is_tradable:
            return (self._callback(request, ExecutionEventType.REJECTED, reason="no valid book at arrival"),)
        side = "buy" if request.side == CoreSide.BUY else "sell"
        if would_cross(side, float(request.price), float(book.best_bid), float(book.best_ask)):
            return (self._callback(request, ExecutionEventType.REJECTED, reason="postonly_would_take_liquidity"),)
        now = self._at(request)
        mirror = self._mirror(book, now)
        order = self.executor.place(
            venue=self.venue, pair=self.pair or request.instrument_id, symbol=self.pair or request.instrument_id,
            side=SimSide.BID if side == "buy" else SimSide.ASK, price=float(request.price), size=float(request.quantity),
            book=mirror, state=self.state.to_market_state(mirror, now), regime_bucket=self.state.regime().bucket, now=now,
            active_ts=now, tick_size=self.tick_size, arrival_ts=now,        # placed at arrival: queue position and the pre-arrival gate are judged from then
        )
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
        if trade.venue != self.venue:
            return ()
        now = self._secs(trade.receive_ts_ns)
        sign = 1 if trade.aggressor_side == CoreSide.BUY.value else -1
        self.state.on_trade(now, float(trade.price), float(trade.quantity), sign)
        fills = self.executor.on_trade(
            self.venue, float(trade.price), float(trade.quantity), sign, now, self.maker_fee_bps,
            event_ts=self._secs(trade.exchange_ts_ns),
        )
        return self._relay(fills)

    def on_book_update(self, book: CoreBook) -> tuple[tuple[CanonicalOrderRequest, ExecutionCallback], ...]:
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
