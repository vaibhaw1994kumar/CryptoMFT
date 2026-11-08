"""Canonical feed converter (Phase D, step D4): the research feed's records as the same `MarketEvent` and `TradeEvent` the live adapters produce.

Input: the `TimedEvent` stream of `CrossVenueFeed` (raw Tardis-shaped records with the receive time the backtester assigns). Output, in the same order:
  snapshot record        -> BOOK_SNAPSHOT (every level of the record)
  run of delta records   -> one BOOK_DELTA per exchange message: the levels the message changed, each with its NEW absolute size. The grouping is the backtester's
                            message-atomic rule (same venue, same exchange time, receive stamps within SAME_STAMP_S; another venue's event that falls inside the run is
                            held until after it), so the core applies a message as one unit exactly as `CrossVenueArbBacktester._mark_frames` does.
  trade record           -> TradeEvent (aggressor BUY/SELL)
Prices and sizes come from the record's JSON text as exact Decimals (`event_loader.decimal_numbers`), never through a float. The delta semantics ("hybrid": add and modify
are absolute, cancel is decremental) are the book's `apply_delta` rules, run on a Decimal level map so a cancel becomes an absolute new size.

Differences from the live adapters, recorded here and in the run manifest (the files do not allow more): the live adapters' `normalize_bybit_depth` / `normalize_binance_depth`
read native websocket payloads (`data.b`, `u`, `E`), which these records are not, so only their envelope (the MarketEvent fields, `stable_id` event ids, quality flags) is
reused. The files carry no update ids, so each venue gets a CONSTRUCTED contiguous sequence (one per emitted book event, flag SEQUENCE_CONSTRUCTED); the core book therefore
sees no gaps, which the raw data cannot prove or disprove. Receive times are the backtester's float seconds rounded to nanoseconds (the float step is about 240 ns at today's epoch).
"""
from __future__ import annotations

from collections import deque
from decimal import Decimal
from typing import Callable, Iterable, Iterator

from crypto_stat_arb.models import EventType, Level, MarketEvent, Side, TradeEvent, stable_id

from .event_loader import BOOK, DELTA, TRADE

SAME_STAMP_S = 1e-6        # the one definition: backtester._SAME_STAMP_S is this value, and `CrossVenueArbBacktester._mark_frames` runs `message_frames` below (one grouping rule, not two)
_BILLION = Decimal(10 ** 9)
_TRADE_ID_FIELDS = ("trade_id", "agg_id", "t", "a", "i", "id")
ADAPTER_VERSION = "research-feed-canonical-v1"


def message_frames(stream: Iterable, on_moved: Callable[[int], None] | None = None) -> Iterator[list]:
    """The message grouping rule, used by the backtester (`_mark_frames`) and by the converter: yields a list of the deltas of one exchange message, or a one-element list for any other record.
    `on_moved(n)` is called before a message is yielded with the number of other-venue events that sat BETWEEN two of its deltas and were held until after it."""
    it = iter(stream)
    pending: deque = deque()

    def pull():
        return pending.popleft() if pending else next(it, None)

    while True:
        ev = pull()
        if ev is None:
            return
        if ev.kind != DELTA:
            yield [ev]
            continue
        frame, held, held_at, last = [ev], [], [], ev
        while True:
            nx = pull()
            if nx is None:
                break
            if nx.venue != ev.venue:
                held.append(nx)
                held_at.append(len(frame))                  # how many of the message's deltas had been read when it showed up
                continue
            if nx.kind == DELTA and nx.exchange_ts == ev.exchange_ts and abs(nx.receive_ts - last.receive_ts) < SAME_STAMP_S:
                frame.append(nx)
                last = nx
                continue
            pending.appendleft(nx)
            break
        for h in reversed(held):
            pending.appendleft(h)
        moved = sum(1 for n in held_at if n < len(frame))       # only events that sat BETWEEN two deltas of the message were moved
        if moved and on_moved is not None:
            on_moved(moved)
        yield frame


def _ns_exact(value) -> int:
    """Nanoseconds from a timestamp: exact for a Decimal read from text, rounded for a float."""
    if isinstance(value, Decimal):
        return int((value * _BILLION).to_integral_value())
    return int(round(float(value) * 1e9))


def _new_size(semantics: str, event_type: str, current: Decimal, volume: Decimal) -> Decimal:
    """The book's `apply_delta` rule on exact numbers."""
    et = event_type.lower()
    if et in ("delete", "remove"):
        return Decimal(0)
    if et == "cancel":
        return max(Decimal(0), volume) if semantics == "absolute" else max(Decimal(0), current - volume)
    if et in ("add", "insert"):
        return current + max(Decimal(0), volume) if semantics == "decremental" else max(Decimal(0), volume)
    return max(Decimal(0), volume)


class CanonicalFeed:
    def __init__(self, instrument_id: str, *, venue_names: dict[str, str] | None = None, delta_semantics: dict[str, str] | None = None) -> None:
        self.instrument_id = instrument_id
        self.venue_names = venue_names or {}
        self.delta_semantics = delta_semantics or {}
        self._levels: dict[str, tuple[dict[Decimal, Decimal], dict[Decimal, Decimal]]] = {}
        self._sequence: dict[str, int] = {}
        self._ordinal = 0
        self.counts = {"snapshots": 0, "delta_messages": 0, "delta_records": 0, "trades": 0, "levels_changed": 0}

    def _venue(self, feed_venue: str) -> str:
        return self.venue_names.get(feed_venue, feed_venue.upper())

    def _next_sequence(self, venue: str) -> int:
        self._sequence[venue] = self._sequence.get(venue, 0) + 1
        return self._sequence[venue]

    def convert(self, stream: Iterable) -> Iterator[MarketEvent | TradeEvent]:
        """Convert a TimedEvent stream built with `decimal_numbers=True` (CrossVenueFeed / iter_venue_events): a record whose numbers are floats is refused, so a price can never come from a float."""
        for frame in message_frames(stream):
            head = frame[0]
            venue = self._venue(head.venue)
            if head.kind == BOOK:
                yield self._snapshot(head, venue)
            elif head.kind == DELTA:
                event = self._delta_message(frame, head.venue, venue)
                if event is not None:
                    yield event
            elif head.kind == TRADE:
                event = self._trade(head, venue)
                if event is not None:
                    yield event

    @staticmethod
    def _text_number(value) -> Decimal:
        if isinstance(value, float):
            raise ValueError("the canonical feed needs the records' numbers as Decimals read from text: build the feed with decimal_numbers=True")
        return Decimal(str(value))

    def _snapshot(self, ev, venue: str) -> MarketEvent:
        p = ev.payload
        bids = {self._text_number(px): self._text_number(sz) for px, sz in (p.get("bids") or []) if self._text_number(sz) > 0}
        asks = {self._text_number(px): self._text_number(sz) for px, sz in (p.get("asks") or []) if self._text_number(sz) > 0}
        self._levels[ev.venue] = (bids, asks)
        sequence = self._next_sequence(venue)
        self.counts["snapshots"] += 1
        return self._market_event(EventType.BOOK_SNAPSHOT, venue, ev, p.get("timestamp"), sequence, tuple(Level(px, sz) for px, sz in bids.items()), tuple(Level(px, sz) for px, sz in asks.items()))

    def _delta_message(self, frame: list, feed_venue: str, venue: str) -> MarketEvent | None:
        bids, asks = self._levels.setdefault(feed_venue, ({}, {}))
        semantics = self.delta_semantics.get(feed_venue, "hybrid")
        changed_bids: dict[Decimal, Decimal] = {}
        changed_asks: dict[Decimal, Decimal] = {}
        for ev in frame:
            p = ev.payload
            price = self._text_number(p.get("price", 0) or 0)
            self.counts["delta_records"] += 1
            if price <= 0:
                continue
            side = str(p.get("side", "bid")).lower()
            book, changed = (bids, changed_bids) if side.startswith("b") else (asks, changed_asks)
            size = _new_size(semantics, str(p.get("event_type", "modify")), book.get(price, Decimal(0)), self._text_number(p.get("volume", p.get("size", 0)) or 0))
            if size > 0:
                book[price] = size
            else:
                book.pop(price, None)
            changed[price] = size
        if not changed_bids and not changed_asks:
            return None
        sequence = self._next_sequence(venue)
        self.counts["delta_messages"] += 1
        self.counts["levels_changed"] += len(changed_bids) + len(changed_asks)
        last = frame[-1]
        return self._market_event(EventType.BOOK_DELTA, venue, last, frame[0].payload.get("exchange_ts"), sequence,
                                  tuple(Level(px, sz) for px, sz in changed_bids.items()), tuple(Level(px, sz) for px, sz in changed_asks.items()))

    def _market_event(self, kind: EventType, venue: str, ev, exchange_ts, sequence: int, bids: tuple, asks: tuple) -> MarketEvent:
        exchange_ns = _ns_exact(exchange_ts if exchange_ts is not None else ev.exchange_ts)
        return MarketEvent(
            event_id=stable_id("canonical", "research-feed", venue, sequence), event_type=kind, venue=venue, instrument_id=self.instrument_id,
            exchange_ts_ns=exchange_ns, receive_ts_ns=_ns_exact(ev.receive_ts), sequence=sequence, source_ts_ns=exchange_ns, sequence_start=sequence,
            raw_ref=f"research-feed:{venue}:{exchange_ns}:{sequence}", adapter_version=ADAPTER_VERSION, bids=bids, asks=asks,
            quality_flags=("SEQUENCE_CONSTRUCTED",),
        )

    def _trade(self, ev, venue: str) -> TradeEvent | None:
        p = ev.payload
        price = self._text_number(p.get("price", 0) or 0)
        quantity = self._text_number(p.get("volume", p.get("size", 0)) or 0)
        if price <= 0 or quantity <= 0:
            return None
        side = p.get("side", "b")
        buy = side.lower().startswith("b") if isinstance(side, str) else self._text_number(side) > 0
        self._ordinal += 1
        self.counts["trades"] += 1
        native_id = next((str(p[f]) for f in _TRADE_ID_FIELDS if p.get(f) not in (None, "")), "")
        exchange_ns = _ns_exact(p.get("exchange_ts", ev.exchange_ts))
        return TradeEvent(
            event_id=stable_id("canonical-trade", "research-feed", venue, self._ordinal), venue=venue, instrument_id=self.instrument_id,
            trade_id=native_id or f"{venue}-{self._ordinal}", exchange_ts_ns=exchange_ns, source_ts_ns=exchange_ns, receive_ts_ns=_ns_exact(ev.receive_ts),
            price=price, quantity=quantity, aggressor_side=(Side.BUY if buy else Side.SELL).value, arrival_ordinal=self._ordinal,
            raw_ref=f"research-feed:{venue}:{exchange_ns}:{self._ordinal}", adapter_version=ADAPTER_VERSION,
            quality_flags=() if native_id else ("TRADE_ID_CONSTRUCTED",),
        )
