"""Queue-aware passive (maker) simulation on the platform's execution path.

The simulator quotes post-only at the touch on each venue and simulates the
outcome through the same abstractions the OMS replay uses:

* **one event clock** (``SimulationClock``): replay and live paper both drive
  it with each canonical event's ``receive_ts_ns``; arrivals, cancels,
  acknowledgements, fill reports and the quote scheduler are timers on it.
  No component reads a wall clock;
* **the platform ``LatencyModel``** (built from the config ``latency`` section,
  as a separate seeded instance so enabling the maker simulation never shifts
  the OMS draws). Stage composition mirrors the OMS: a decision at ``t`` is
  dispatched at ``t + strategy + risk``; each order reaches the venue at
  ``dispatch + entry`` and **its queue position is assigned then**, against
  the book as it is at arrival; a cancel applies at ``send + cancel`` and the
  order can still fill until it does; fill reports are observed at
  ``match + fill`` and acknowledgements/rejects at ``venue time + submit_ack``.
  Recorded ``receive_ts_ns`` already contains market-data latency, so that
  stage is never drawn on top of it. ``feed_lag_ns`` (default 0, per venue)
  is the explicit staleness assumption: a venue-side instant ``X`` (order
  arrival, cancel applied) is placed at ``X + feed_lag`` on the receive-time
  clock, i.e. the queue is taken from the book we *would have received* for
  the venue's state at arrival. Reports are then observed ``feed_lag`` earlier
  than the venue instant on that clock, plus their own latency;
* **canonical events**: ``MarketEvent`` books and ``TradeEvent`` prints;
* **the C++ ``mm_matching_engine``** (``maker_engine.CppQueueEngine``), or its
  bit-identical Python mirror when the extension is not built. Every fill is
  caused by one public print and records that print's ``event_id``. Queue
  ahead at arrival is ``queue_position_frac`` x the visible level (default 1.0);
* **markouts**: each fill's signed mid move after each ``markout_horizons_ns``
  (positive = favourable), as an adverse-selection measure.

Quote cadence (``quote_interval_ms``) is strategy scheduling, configured
separately from execution latency. A quote whose price is unchanged is kept
resting, so it keeps its queue priority. The strategy's inventory view only
changes when a fill report is observed; the inventory band is enforced on that
view plus every unfinished order, so the true position cannot leave the band.

Nothing is ever sent to a venue.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from hashlib import sha256
import heapq
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .canonical import CanonicalEventDispatcher
from .config import load_config
from .fees import Liquidity
from .latency import LatencyModel, latency_model_from_config
from .maker_engine import QueueEngine, make_queue_engine
from .models import BookState, MarketEvent, TradeEvent
from .orderbook import OrderBook
from .sim_clock import SimulationClock, TimerHandle


MAKER_REPORT_SCHEMA_VERSION = "2.0.0"
EPS = 1e-12
DEFAULT_TIE_PRIORITY = ("TRADE", "BOOK")

# Order lifecycle, venue side.
IN_FLIGHT = "IN_FLIGHT"
RESTING = "RESTING"
FILLED = "FILLED"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"


@dataclass(slots=True)
class SimOrder:
    client_order_id: str
    venue: str
    side: str  # "BUY" / "SELL"
    price: float
    size: float
    decision_ts_ns: int
    dispatch_ts_ns: int
    arrival_ts_ns: int
    state: str = IN_FLIGHT
    queue_ahead_at_arrival: float | None = None
    filled: float = 0.0
    # Strategy view: what has been *observed* so far.
    known_filled: float = 0.0
    cancel_requested: bool = False
    cancel_send_ts_ns: int | None = None
    pending_reports: int = 0
    terminal_observed: bool = False
    crossed_seen: bool = False

    @property
    def done(self) -> bool:
        """Finished from the strategy's point of view (counts for nothing)."""
        return self.terminal_observed and self.pending_reports == 0


@dataclass(slots=True)
class _VenueStats:
    quotes: int = 0
    quotes_kept: int = 0
    fills: int = 0
    fills_through: int = 0
    fills_while_cancel_pending: int = 0
    orders_filled: int = 0
    post_only_rejects: int = 0
    cancels_sent: int = 0
    cancels_applied: int = 0
    cancel_rejects_too_late: int = 0
    cancel_rejects_unknown: int = 0
    buy_quantity: float = 0.0
    sell_quantity: float = 0.0
    cash: float = 0.0
    fees: float = 0.0
    first_mid: float | None = None
    last_mid: float | None = None
    last_top: tuple[float, float] | None = None
    trades_seen: int = 0
    book_events: int = 0
    invalid_book_events: int = 0
    crossed_without_print: int = 0
    queue_ahead_sum: float = 0.0
    queue_ahead_count: int = 0
    markout_sum_bps: dict[int, float] = field(default_factory=dict)
    markout_count: dict[int, int] = field(default_factory=dict)
    markouts_truncated: int = 0
    max_inventory_seen: float = 0.0
    min_inventory_seen: float = 0.0
    sanity_violations: list[str] = field(default_factory=list)
    sanity_violation_count: int = 0


@dataclass(slots=True)
class _VenueState:
    engine: QueueEngine
    inventory: float
    known_inventory: float
    valid: bool = False
    best_bid: float | None = None
    best_ask: float | None = None
    fed_levels: dict[tuple[str, Decimal], float] = field(default_factory=dict)
    quote_timer: TimerHandle | None = None
    # Orders the strategy still has to account for (not ``done``), by id.
    active: dict[str, SimOrder] = field(default_factory=dict)


class MakerDryRunSimulator:
    def __init__(
        self,
        venues: Iterable[str],
        symbol: str,
        *,
        instrument_id: str = "",
        latency_model: LatencyModel | None,
        quote_size: float = 0.001,
        quote_interval_ns: int = 1_000_000_000,
        max_inventory: float = 0.01,
        initial_inventory: float | None = None,
        maker_fee_bps: Mapping[str, float] | None = None,
        depth: int = 50,
        engine: str = "auto",
        clock: SimulationClock | None = None,
        max_fill_records: int = 1_000_000,
        queue_position_frac: float = 1.0,
        feed_lag_ns: int | Mapping[str, int] = 0,
        markout_horizons_ns: Iterable[int] = (1_000_000_000, 5_000_000_000),
    ) -> None:
        if quote_interval_ns <= 0:
            raise ValueError("quote_interval_ns must be positive")
        venues = tuple(venues)
        self.symbol = symbol
        self.instrument_id = instrument_id
        self.latency_model = latency_model
        self.quote_size = quote_size
        self.quote_interval_ns = quote_interval_ns
        self.max_inventory = max_inventory
        self.initial_inventory = max_inventory / 2 if initial_inventory is None else initial_inventory
        self.maker_fee_bps = {venue: float(bps) for venue, bps in (maker_fee_bps or {}).items()}
        self.depth = depth
        self.clock = clock or SimulationClock()
        self.max_fill_records = max_fill_records
        self.venues: dict[str, _VenueState] = {}
        self.stats: dict[str, _VenueStats] = {}
        self.orders: dict[str, SimOrder] = {}
        self.fills: list[dict[str, Any]] = []
        self.fill_records_dropped = 0
        self._sequence = 0
        self._fill_sequence = 0
        self._finished = False
        self._draining = False
        self.queue_position_frac = float(queue_position_frac)
        self.feed_lag_ns = (
            {venue: int(feed_lag_ns.get(venue, 0)) for venue in venues}
            if isinstance(feed_lag_ns, Mapping) else int(feed_lag_ns)
        )
        if any(lag < 0 for lag in (self._lags().values() or [self.feed_lag_ns])):
            raise ValueError("feed_lag_ns must be non-negative")
        self.markout_horizons_ns = tuple(int(h) for h in markout_horizons_ns)
        engine_names = set()
        for venue in venues:
            queue_engine = make_queue_engine(engine, queue_position_frac=queue_position_frac)
            engine_names.add(queue_engine.name)
            self.venues[venue] = _VenueState(
                queue_engine, self.initial_inventory, self.initial_inventory
            )
            self.stats[venue] = _VenueStats(
                max_inventory_seen=self.initial_inventory,
                min_inventory_seen=self.initial_inventory,
            )
        self.engine = engine_names.pop() if len(engine_names) == 1 else "mixed"

    # -- latency ---------------------------------------------------------------
    def _lags(self) -> dict[str, int]:
        return self.feed_lag_ns if isinstance(self.feed_lag_ns, dict) else {}

    def _lag(self, venue: str) -> int:
        return self.feed_lag_ns.get(venue, 0) if isinstance(self.feed_lag_ns, dict) else self.feed_lag_ns

    def _observed_at(self, venue: str, venue_ts: int, stage: str) -> int:
        """Local time a report about venue instant ``venue_ts`` is observed."""
        return max(self.clock.now_ns(), venue_ts - self._lag(venue) + self._draw(venue, stage))

    def _draw(self, venue: str, stage: str) -> int:
        return self.latency_model.draw(venue, stage) if self.latency_model is not None else 0

    # -- market data -----------------------------------------------------------
    def on_book(self, venue: str, book: OrderBook, ts_ns: int, event_id: str = "") -> None:
        """Apply one reconstructed book state, observed at ``ts_ns`` (receive time)."""
        state = self.venues.get(venue)
        if state is None or self._finished:
            return
        now = self.clock.advance_to(ts_ns)
        stats = self.stats[venue]
        if book.state != BookState.VALID or not book.bids or not book.asks:
            state.valid = False
            stats.invalid_book_events += 1
            return
        stats.book_events += 1
        self._sync_levels(state, book)
        best_bid, best_ask = float(book.best_bid), float(book.best_ask)
        state.best_bid, state.best_ask = best_bid, best_ask
        mid = (best_bid + best_ask) / 2
        stats.first_mid = stats.first_mid if stats.first_mid is not None else mid
        stats.last_mid = mid
        stats.last_top = (best_bid, best_ask)
        for order in state.active.values():
            if order.state != RESTING or order.crossed_seen:
                continue
            if (order.side == "BUY" and best_ask <= order.price) or (
                order.side == "SELL" and best_bid >= order.price
            ):
                # The real book traded through our price without a print that
                # reached our queue: diagnostic only, never a fill.
                order.crossed_seen = True
                stats.crossed_without_print += 1
        if not state.valid:
            state.valid = True
            if state.quote_timer is None:
                state.quote_timer = self.clock.schedule(
                    now, lambda t, venue=venue: self._on_quote_timer(venue, t),
                    label=f"quote:{venue}",
                )

    def _sync_levels(self, state: _VenueState, book: OrderBook) -> None:
        """Feed the engine every level that changed inside the watched band.

        The band is the top ``depth`` levels per side plus every price we have
        fed before (so a level leaving the band, or one we rest at, is updated
        with its true size, possibly zero).
        """
        current: dict[tuple[str, Decimal], float] = {}
        for price in heapq.nlargest(self.depth, book.bids):
            current[("bid", price)] = float(book.bids[price])
        for price in heapq.nsmallest(self.depth, book.asks):
            current[("ask", price)] = float(book.asks[price])
        for key in state.fed_levels:
            if key not in current:
                side, price = key
                size = (book.bids if side == "bid" else book.asks).get(price)
                current[key] = float(size) if size is not None else 0.0
        for (side, price), size in current.items():
            if state.fed_levels.get((side, price)) != size:
                state.engine.set_level(side, float(price), size)
        state.fed_levels = {key: size for key, size in current.items() if size > 0.0}

    def on_trade(self, trade: TradeEvent) -> None:
        """Apply one canonical public print at its receive time."""
        state = self.venues.get(trade.venue)
        if state is None or self._finished:
            return
        if self.instrument_id and trade.instrument_id != self.instrument_id:
            return
        now = self.clock.advance_to(trade.receive_ts_ns)
        stats = self.stats[trade.venue]
        stats.trades_seen += 1
        price = float(trade.price)
        for fill in state.engine.on_trade(price, float(trade.quantity), trade.aggressor_side):
            self._record_fill(state, stats, fill, trade, now)

    def on_trade_tick(self, tick: TradeEvent) -> None:
        self.on_trade(tick)

    # -- strategy: quote scheduler --------------------------------------------
    def _on_quote_timer(self, venue: str, now: int) -> None:
        state = self.venues[venue]
        state.quote_timer = self.clock.schedule(
            now + self.quote_interval_ns,
            lambda t, venue=venue: self._on_quote_timer(venue, t),
            label=f"quote:{venue}",
        )
        if not state.valid or state.best_bid is None or state.best_ask is None:
            return
        dispatch = now + self._draw(venue, "strategy") + self._draw(venue, "risk")
        desired = {"BUY": state.best_bid, "SELL": state.best_ask}
        for side in ("BUY", "SELL"):
            current = self._working_order(venue, side)
            if current is not None and current.price == desired[side]:
                self.stats[venue].quotes_kept += 1
                continue
            if current is not None:
                self._send_cancel(current, dispatch)
            if self._exposure_allows(venue, side):
                self._send_order(venue, side, desired[side], now, dispatch)

    def _working_order(self, venue: str, side: str) -> SimOrder | None:
        working = [
            order for order in self.venues[venue].active.values()
            if order.side == side and not order.cancel_requested
            and order.state in (IN_FLIGHT, RESTING)
        ]
        return working[-1] if working else None

    def _exposure_allows(self, venue: str, side: str) -> bool:
        state = self.venues[venue]
        outstanding = sum(
            order.size - order.known_filled for order in state.active.values()
            if order.side == side
        )
        if side == "BUY":
            return state.known_inventory + outstanding + self.quote_size <= self.max_inventory + EPS
        return state.known_inventory - outstanding - self.quote_size >= -EPS

    def _send_order(self, venue: str, side: str, price: float, decision: int, dispatch: int) -> None:
        self._sequence += 1
        arrival = dispatch + self._draw(venue, "entry") + self._lag(venue)
        order = SimOrder(
            client_order_id=f"{venue.lower()}-{side.lower()}-{self._sequence}",
            venue=venue, side=side, price=price, size=self.quote_size,
            decision_ts_ns=decision, dispatch_ts_ns=dispatch, arrival_ts_ns=arrival,
        )
        self.orders[order.client_order_id] = order
        self.venues[venue].active[order.client_order_id] = order
        self.stats[venue].quotes += 1
        self.clock.schedule(arrival, lambda t, order=order: self._on_arrival(order, t),
                            label="arrival")

    def _send_cancel(self, order: SimOrder, send_ts: int) -> None:
        order.cancel_requested = True
        order.cancel_send_ts_ns = send_ts
        self.stats[order.venue].cancels_sent += 1
        applied = send_ts + self._draw(order.venue, "cancel") + self._lag(order.venue)
        self.clock.schedule(applied, lambda t, order=order: self._on_cancel_applied(order, t),
                            label="cancel")

    # -- venue side ------------------------------------------------------------
    def _on_arrival(self, order: SimOrder, now: int) -> None:
        state = self.venues[order.venue]
        stats = self.stats[order.venue]
        crosses = (
            (order.side == "BUY" and state.best_ask is not None and order.price >= state.best_ask)
            or (order.side == "SELL" and state.best_bid is not None and order.price <= state.best_bid)
        )
        if crosses:
            order.state = REJECTED  # post-only would take liquidity
            stats.post_only_rejects += 1
            self._observe_terminal(order, now)
            return
        engine_side = "bid" if order.side == "BUY" else "ask"
        order.queue_ahead_at_arrival = state.engine.place(
            order.client_order_id, engine_side, order.price, order.size, now
        )
        order.state = RESTING
        stats.queue_ahead_sum += order.queue_ahead_at_arrival
        stats.queue_ahead_count += 1

    def _on_cancel_applied(self, order: SimOrder, now: int) -> None:
        stats = self.stats[order.venue]
        if order.state == RESTING:
            self.venues[order.venue].engine.cancel(order.client_order_id)
            order.state = CANCELLED
            stats.cancels_applied += 1
            self._observe_terminal(order, now)
        elif order.state == IN_FLIGHT:
            # The cancel overtook its order: the venue rejects it as unknown
            # and the order will rest; the next quote decision re-cancels.
            stats.cancel_rejects_unknown += 1
            ack = self._observed_at(order.venue, now, "submit_ack")
            self.clock.schedule(ack, lambda t, order=order: self._observe_cancel_reject(order),
                                label="cancel_reject")
        else:
            stats.cancel_rejects_too_late += 1

    def _observe_terminal(self, order: SimOrder, venue_ts: int) -> None:
        ack = self._observed_at(order.venue, venue_ts, "submit_ack")
        self.clock.schedule(ack, lambda t, order=order: self._observe_terminal_ack(order),
                            label="terminal_ack")

    def _observe_cancel_reject(self, order: SimOrder) -> None:
        order.cancel_requested = False

    def _observe_terminal_ack(self, order: SimOrder) -> None:
        order.terminal_observed = True
        self._retire(order)

    def _retire(self, order: SimOrder) -> None:
        if order.done:
            self.venues[order.venue].active.pop(order.client_order_id, None)

    def _record_fill(self, state: _VenueState, stats: _VenueStats, fill, trade: TradeEvent, now: int) -> None:
        order = self.orders[fill.client_order_id]
        quantity, price = fill.quantity, fill.price
        fee = quantity * price * self.maker_fee_bps.get(order.venue, 0.0) / 10_000
        self._check_fill(order, stats, quantity, price)
        order.filled += quantity
        signed = quantity if order.side == "BUY" else -quantity
        state.inventory += signed
        stats.cash -= signed * price
        stats.fees += fee
        stats.fills += 1
        stats.fills_through += int(fill.through)
        stats.fills_while_cancel_pending += int(order.cancel_requested)
        if order.side == "BUY":
            stats.buy_quantity += quantity
        else:
            stats.sell_quantity += quantity
        stats.max_inventory_seen = max(stats.max_inventory_seen, state.inventory)
        stats.min_inventory_seen = min(stats.min_inventory_seen, state.inventory)
        if state.inventory > self.max_inventory + 1e-9 or state.inventory < -1e-9:
            self._violation(stats, f"inventory {state.inventory} outside [0, {self.max_inventory}]")
        fully = order.filled >= order.size - 1e-12
        if fully:
            order.state = FILLED
            stats.orders_filled += 1
        observed = self._observed_at(order.venue, now, "fill")
        order.pending_reports += 1
        for horizon in self.markout_horizons_ns:
            self.clock.schedule(
                now + horizon,
                lambda t, order=order, price=price, horizon=horizon:
                    self._markout(order, price, horizon),
                label="markout",
            )
        self._fill_sequence += 1
        record = {
            "fill_id": self._fill_sequence,
            "client_order_id": order.client_order_id,
            "venue": order.venue,
            "side": order.side,
            "price": price,
            "quantity": quantity,
            "fee": fee,
            "cause": "TRADE_THROUGH" if fill.through else "TRADE",
            "trade_event_id": trade.event_id,
            "trade_id": trade.trade_id,
            "trade_price": float(trade.price),
            "trade_quantity": float(trade.quantity),
            "trade_aggressor_side": trade.aggressor_side,
            "trade_receive_ts_ns": trade.receive_ts_ns,
            "decision_ts_ns": order.decision_ts_ns,
            "dispatch_ts_ns": order.dispatch_ts_ns,
            "arrival_ts_ns": order.arrival_ts_ns,
            "queue_ahead_at_arrival": order.queue_ahead_at_arrival,
            "feed_lag_ns": self._lag(order.venue),
            "match_ts_ns": now,
            "report_observed_ts_ns": observed,
            "cancel_pending": order.cancel_requested,
            "order_fully_filled": fully,
        }
        if len(self.fills) < self.max_fill_records:
            self.fills.append(record)
        else:
            self.fill_records_dropped += 1
        self.clock.schedule(
            observed,
            lambda t, order=order, signed=signed, quantity=quantity, fully=fully:
                self._observe_fill(order, signed, quantity, fully),
            label="fill_report",
        )

    def _markout(self, order: SimOrder, price: float, horizon: int) -> None:
        stats = self.stats[order.venue]
        if self._draining or stats.last_mid is None:
            stats.markouts_truncated += 1  # horizon ends after the last event
            return
        move = (stats.last_mid - price) / price * 10_000
        signed = move if order.side == "BUY" else -move
        stats.markout_sum_bps[horizon] = stats.markout_sum_bps.get(horizon, 0.0) + signed
        stats.markout_count[horizon] = stats.markout_count.get(horizon, 0) + 1

    def _observe_fill(self, order: SimOrder, signed: float, quantity: float, fully: bool) -> None:
        self.venues[order.venue].known_inventory += signed
        order.known_filled += quantity
        order.pending_reports -= 1
        if fully:
            order.terminal_observed = True
        self._retire(order)

    def _check_fill(self, order: SimOrder, stats: _VenueStats, quantity: float, price: float) -> None:
        if not (quantity > 0 and price > 0):
            self._violation(stats, f"non-positive fill {order.side} {quantity}@{price}")
        if order.filled + quantity > order.size * 1.000001:
            self._violation(stats, f"fill {quantity} exceeds order {order.client_order_id} size {order.size}")
        if price != order.price:
            self._violation(stats, f"fill price {price} differs from order price {order.price}")
        if stats.last_top is not None:
            best_bid, best_ask = stats.last_top
            band = 0.02 * (best_bid + best_ask) / 2
            if not best_bid - band <= price <= best_ask + band:
                self._violation(stats, f"fill price {price} outside 2% of book {best_bid}/{best_ask}")

    @staticmethod
    def _violation(stats: _VenueStats, message: str) -> None:
        stats.sanity_violation_count += 1
        if len(stats.sanity_violations) < 20:
            stats.sanity_violations.append(message)

    # -- lifecycle -------------------------------------------------------------
    def finish(self, end_ts_ns: int | None = None) -> None:
        """Stop quoting and let every in-flight arrival, cancel and report land."""
        if self._finished:
            return
        if end_ts_ns is not None:
            self.clock.advance_to(end_ts_ns)
        for state in self.venues.values():
            if state.quote_timer is not None:
                state.quote_timer.cancel()
                state.quote_timer = None
        self._draining = True
        self.clock.drain()
        self._finished = True

    def write_fills(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for row in self.fills:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
        return path

    def report(self) -> dict[str, Any]:
        venues = {}
        for venue, stats in sorted(self.stats.items()):
            state = self.venues[venue]
            pnl = None
            if stats.last_mid is not None and stats.first_mid is not None:
                pnl = (
                    stats.cash + state.inventory * stats.last_mid
                    - self.initial_inventory * stats.first_mid - stats.fees
                )
            states = Counter(order.state for order in self.orders.values() if order.venue == venue)
            venues[venue] = {
                "quotes": stats.quotes,
                "quotes_kept": stats.quotes_kept,
                "fills": stats.fills,
                "fills_trade_through": stats.fills_through,
                "fills_while_cancel_pending": stats.fills_while_cancel_pending,
                "orders_filled": stats.orders_filled,
                "fill_ratio": stats.fills / stats.quotes if stats.quotes else 0.0,
                "post_only_rejects": stats.post_only_rejects,
                "cancels_sent": stats.cancels_sent,
                "cancels_applied": stats.cancels_applied,
                "cancel_rejects_too_late": stats.cancel_rejects_too_late,
                "cancel_rejects_unknown_order": stats.cancel_rejects_unknown,
                "orders_rested": stats.queue_ahead_count,
                "mean_queue_ahead_at_arrival": (
                    stats.queue_ahead_sum / stats.queue_ahead_count if stats.queue_ahead_count else None
                ),
                "crossed_without_print": stats.crossed_without_print,
                "markout_bps": {
                    f"{horizon // 1_000_000}ms": {
                        "mean": stats.markout_sum_bps[horizon] / stats.markout_count[horizon],
                        "count": stats.markout_count[horizon],
                    }
                    for horizon in self.markout_horizons_ns if stats.markout_count.get(horizon)
                },
                "markouts_truncated": stats.markouts_truncated,
                "order_states": dict(sorted(states.items())),
                "trades_seen": stats.trades_seen,
                "book_events": stats.book_events,
                "invalid_book_events": stats.invalid_book_events,
                "buy_quantity": stats.buy_quantity,
                "sell_quantity": stats.sell_quantity,
                "inventory": state.inventory,
                "known_inventory": state.known_inventory,
                "inventory_range": [stats.min_inventory_seen, stats.max_inventory_seen],
                "fees": stats.fees,
                "mark_to_market_pnl_quote": pnl,
                "sanity_violations": list(stats.sanity_violations),
                "sanity_violation_count": stats.sanity_violation_count,
            }
        return {
            "schema_version": MAKER_REPORT_SCHEMA_VERSION,
            "engine": self.engine,
            "source": "sim_engine mm_matching_engine (crypto_stat_arb copy with queue-accounting fixes)",
            "symbol": self.symbol,
            "instrument_id": self.instrument_id,
            "quote_size": self.quote_size,
            "quote_cadence": {"quote_interval_ns": self.quote_interval_ns},
            "quote_interval_ns": self.quote_interval_ns,
            "max_inventory": self.max_inventory,
            "initial_inventory": self.initial_inventory,
            "queue_position_frac": self.queue_position_frac,
            "feed_lag_ns": self.feed_lag_ns,
            "timeline": {
                "clock": "receive-time event clock (SimulationClock)",
                "timers_fired": self.clock.fired,
                "clock_regressions": self.clock.regressions,
            },
            "latency": (
                self.latency_model.report() if self.latency_model is not None
                else {"source": "DISABLED_ZERO_LATENCY"}
            ),
            "fill_records": len(self.fills),
            "fill_records_dropped": self.fill_records_dropped,
            "venues": venues,
            "limitations": [
                "Passive at-touch quoting harness for fill-model validation, not a strategy.",
                "Fills are strictly print-driven: a book that crosses a resting order without a "
                "print reaching its queue is counted in crossed_without_print, never filled.",
                "Cancellations ahead of a resting order are only inferred when the visible level "
                "shrinks below the order's queue ahead (conservative).",
                "The recorded receive_ts_ns is the event clock; an order's arrival is placed on "
                "that clock without an extra market-data leg, as in the OMS replay.",
            ],
        }


def _derived_seed(seed: int) -> int:
    return int(sha256(f"maker_sim|{seed}".encode()).hexdigest()[:8], 16)


def maker_latency_model(cfg, settings: Mapping[str, Any]) -> LatencyModel | None:
    """The config ``latency`` section (plus ``maker_simulation.latency``
    overrides) as a separate instance with its own derived seed."""
    document = deepcopy(dict(cfg.latency or {}))
    overrides = settings.get("latency") or {}
    if overrides:
        document["enabled"] = overrides.get("enabled", document.get("enabled", True))
        for key in ("preset", "seed"):
            if key in overrides:
                document[key] = overrides[key]
        profiles = dict(document.get("profiles") or {})
        for venue, stages in (overrides.get("profiles") or {}).items():
            profiles[venue] = {**profiles.get(venue, {}), **stages}
        document["profiles"] = profiles
    if not document:
        return None
    base_seed = int(settings.get("seed", document.get("seed", 0)))
    document["seed"] = _derived_seed(base_seed)
    model = latency_model_from_config(document, tuple(cfg.venues))
    if model is not None:
        model.source = f"maker:{model.source}"
    return model


def maker_simulator_from_config(cfg, *, engine: str | None = None) -> MakerDryRunSimulator | None:
    settings = (cfg.strategies or {}).get("maker_simulation") or {}
    if not settings.get("enabled", False):
        return None
    if "latency_ms" in settings:
        raise ValueError(
            "maker_simulation.latency_ms is no longer supported: maker latency comes from the "
            "top-level 'latency' section (override stages under maker_simulation.latency)"
        )
    venues = tuple(settings.get("venues", cfg.venues))
    spec = cfg.instruments.get(venues[0], cfg.instrument_id)
    return MakerDryRunSimulator(
        venues, f"{spec.base}/{spec.quote}",
        instrument_id=cfg.instrument_id,
        latency_model=maker_latency_model(cfg, settings),
        quote_size=float(settings.get("quote_size", "0.001")),
        quote_interval_ns=int(float(settings.get("quote_interval_ms", 1_000)) * 1_000_000),
        max_inventory=float(settings.get("max_inventory", "0.01")),
        maker_fee_bps={
            venue: float(cfg.instruments.fee_bps(venue, cfg.instrument_id, Liquidity.MAKER))
            for venue in venues
        },
        depth=int(settings.get("depth", 50)),
        engine=engine or str(settings.get("engine", "auto")),
        queue_position_frac=float(settings.get("queue_position_frac", 1.0)),
        feed_lag_ns=_feed_lag_ns(settings.get("feed_lag_ms", 0)),
        markout_horizons_ns=tuple(
            int(float(ms) * 1_000_000) for ms in settings.get("markout_horizons_ms", (1_000, 5_000))
        ),
    )


def _feed_lag_ns(value: Any) -> int | dict[str, int]:
    """``feed_lag_ms``: one number for every venue, or ``{venue: ms}``."""
    if isinstance(value, Mapping):
        return {venue: int(float(ms) * 1_000_000) for venue, ms in value.items()}
    return int(float(value) * 1_000_000)


# -- replay ------------------------------------------------------------------
def _trace_ordinals(trace_paths: list[Path]) -> dict[str, tuple[int, int]]:
    """``event_id -> (part, ordinal)`` from the live ingestion trace.

    Newer traces carry ``arrival_ordinal``; older ones are already written in
    processing order, so the line number is the ordinal. A trace in which an
    event id repeats cannot place that event unambiguously, so it is not used
    at all (fail closed to receive-time ordering).
    """
    ordinals: dict[str, tuple[int, int]] = {}
    for part, path in enumerate(trace_paths):
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("disposition") not in ("CANONICAL", "TRADE"):
                    continue
                ordinal = int(row.get("arrival_ordinal") or line_number)
                if row["canonical_event_id"] in ordinals:
                    return {}
                ordinals[row["canonical_event_id"]] = (part, ordinal)
    return ordinals


def replay_order(
    books: list[MarketEvent],
    trades: list[TradeEvent],
    *,
    ordinals: Mapping[str, tuple[int, int]] | None = None,
    tie_priority: Mapping[str, Iterable[str]] | None = None,
) -> tuple[list[tuple[str, Any]], str]:
    """Merge books and trades into one processing order.

    With an ingestion trace covering every event, the live processing order
    (``arrival_ordinal``) is reproduced exactly. Otherwise events are ordered
    by ``receive_ts_ns``, then exchange time, then a declared per-venue
    priority of event kinds, then ``event_id``. The default priority
    (``TRADE`` before ``BOOK`` on an exact tie) is a stated assumption -- a
    book delta usually reports the result of the prints at the same instant --
    and can be set per venue; it is not presented as an exchange rule. Native
    sequences are never compared across books and trades (separate spaces).
    """
    items: list[tuple[str, Any]] = [("BOOK", event) for event in books]
    items += [("TRADE", trade) for trade in trades]
    ordinals = ordinals or {}
    unique = len({item.event_id for _, item in items}) == len(items)
    if items and unique and all(item.event_id in ordinals for _, item in items):
        items.sort(key=lambda pair: ordinals[pair[1].event_id])
        return items, "arrival_ordinal"
    priority = {
        venue: {kind: rank for rank, kind in enumerate(kinds)}
        for venue, kinds in (tie_priority or {}).items()
    }
    default = {kind: rank for rank, kind in enumerate(priority.get("*", DEFAULT_TIE_PRIORITY))}

    def key(pair: tuple[str, Any]) -> tuple:
        kind, item = pair
        ranks = priority.get(item.venue, default)
        return (item.receive_ts_ns, item.source_ts_ns or item.exchange_ts_ns,
                ranks.get(kind, len(ranks)), item.event_id)

    items.sort(key=key)
    return items, "receive_ts+tie_priority"


def _read_jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def load_recorded_stream(
    cfg, canonical_paths: list[Path], trade_paths: list[Path],
    trace_paths: list[Path] | None = None,
) -> tuple[list[tuple[str, Any]], str]:
    """Read a recorded session once, in its replay order (see ``replay_order``)."""
    books = [
        MarketEvent.from_dict(row)
        for path in canonical_paths for row in _read_jsonl(path)
    ]
    trades = [
        TradeEvent.from_dict(row)
        for path in trade_paths if path.exists() for row in _read_jsonl(path)
    ]
    if trace_paths is None:
        trace_paths = [path.with_name("trace.jsonl") for path in canonical_paths]
    tie_priority = ((cfg.strategies or {}).get("maker_simulation") or {}).get("replay_tie_priority")
    return replay_order(
        books, trades, ordinals=_trace_ordinals(trace_paths), tie_priority=tie_priority,
    )


def replay_stream(cfg, simulator: MakerDryRunSimulator, stream: list[tuple[str, Any]]) -> dict[str, Any]:
    """Drive ``simulator`` with an ordered stream on fresh books; returns counts."""
    order_books = {
        venue: cfg.book_sanity.new_book(venue, cfg.instrument_id) for venue in cfg.venues
    }
    counts = {"book_events": 0, "trades": 0}

    def deliver(event) -> None:
        if isinstance(event, TradeEvent):
            simulator.on_trade(event)
            counts["trades"] += 1
            return
        book = order_books.get(event.venue)
        if book is None:
            return
        book.apply(event)
        simulator.on_book(event.venue, book, event.receive_ts_ns, event.event_id)
        counts["book_events"] += 1

    class _SimulatorSink:
        accepts = None  # books and trades on one canonical path

        def on_event(self, event) -> None:
            deliver(event)

    # Stored sessions may predate raw_ref on every row; every other envelope
    # field is validated, and an invalid event is counted, never applied.
    dispatcher = CanonicalEventDispatcher((_SimulatorSink(),), require_raw_ref=False)
    rejected: Counter[str] = Counter()
    last_ts = 0
    for kind, item in stream:
        last_ts = max(last_ts, item.receive_ts_ns)
        try:
            dispatcher.dispatch(item)
        except ValueError:
            rejected[kind] += 1
    simulator.finish(last_ts or None)
    return {**counts, "rejected_invalid_envelopes": dict(sorted(rejected.items()))}


def simulate_recorded_session(
    config_path: Path, canonical_paths: list[Path], trade_paths: list[Path],
    output_path: Path | None = None,
    *,
    trace_paths: list[Path] | None = None,
    engine: str | None = None,
) -> dict[str, Any]:
    """Replay stored books and trades through the maker simulation.

    ``trace_paths`` defaults to the ``trace.jsonl`` next to each canonical
    file, so recorded live sessions replay in their exact live order.
    """
    cfg, _ = load_config(config_path)
    settings = (cfg.strategies or {}).get("maker_simulation")
    if not (settings or {}).get("enabled", False):
        cfg.strategies["maker_simulation"] = {**(settings or {}), "enabled": True}  # type: ignore[index]
    simulator = maker_simulator_from_config(cfg, engine=engine)
    assert simulator is not None
    stream, ordering = load_recorded_stream(cfg, canonical_paths, trade_paths, trace_paths)
    counts = replay_stream(cfg, simulator, stream)
    report = simulator.report()
    report["replay"] = {**counts, "ordering": ordering}
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fills_path = simulator.write_fills(output_path.with_name(output_path.stem + "_fills.jsonl"))
        report["fills_path"] = fills_path.name
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report
