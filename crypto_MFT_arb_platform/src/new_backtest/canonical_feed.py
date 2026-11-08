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
_SAME_STAMP_NS = 1_000      # an other-venue event held inside a message can be up to SAME_STAMP_S out of receive order
_TRADE_ID_FIELDS = ("trade_id", "agg_id", "t", "a", "i", "id")
ADAPTER_VERSION = "research-feed-canonical-v1"


def continues_message(prev: tuple[float, float], nxt: tuple[float, float]) -> bool:
    """The one rule for 'this delta belongs to the same exchange message as the one before': same exchange time and receive stamps within SAME_STAMP_S. Each argument is
    (exchange_ts, receive stamp). The backtester, the converter and the data-quality scan (`quality_preflight.scan_venue_day_checks`) all ask this."""
    return nxt[0] == prev[0] and abs(nxt[1] - prev[1]) < SAME_STAMP_S


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
            if nx.kind == DELTA and continues_message((last.exchange_ts, last.receive_ts), (nx.exchange_ts, nx.receive_ts)):
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


def _uncross(bids: dict[Decimal, Decimal], asks: dict[Decimal, Decimal], incoming_side: str | None) -> None:
    """The backtester book's repair of a crossed book (`simulation.order_book.OrderBook._uncross`), on exact levels: the side that did NOT just update loses its crossed levels; a crossed
    snapshot (no incoming side) drops the smaller touch first. The converter keeps its own levels repaired this way, so a resync snapshot equals the backtester's book."""
    for _ in range(64):
        if not bids or not asks:
            return
        best_bid, best_ask = max(bids), min(asks)
        if best_bid < best_ask:
            return
        if incoming_side is None:
            if bids[best_bid] <= asks[best_ask]:
                del bids[best_bid]
            else:
                del asks[best_ask]
        elif incoming_side.startswith("b"):
            del asks[best_ask]
        else:
            del bids[best_bid]


class CanonicalFeed:
    def __init__(self, instrument_id: str, *, venue_names: dict[str, str] | None = None, delta_semantics: dict[str, str] | None = None) -> None:
        self.instrument_id = instrument_id
        self.venue_names = venue_names or {}
        self.delta_semantics = delta_semantics or {}
        self._levels: dict[str, tuple[dict[Decimal, Decimal], dict[Decimal, Decimal]]] = {}
        self._sequence: dict[str, int] = {}
        self._ordinal = 0
        self.counts = {"snapshots": 0, "delta_messages": 0, "delta_records": 0, "trades": 0, "levels_changed": 0, "crossed_messages": 0, "crossed_snapshots": 0, "resync_snapshots": 0}
        self._inverse = {core: feed for feed, core in self.venue_names.items()}
        self._emitted_ns: dict[str, int] = {}            # per venue: the latest receive time of a book event this feed has handed out

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
        event_levels = (tuple(Level(px, sz) for px, sz in bids.items()), tuple(Level(px, sz) for px, sz in asks.items()))      # what the record said: a crossed snapshot invalidates the core book
        kept_bids, kept_asks = dict(bids), dict(asks)
        if kept_bids and kept_asks and max(kept_bids) >= min(kept_asks):
            self.counts["crossed_snapshots"] += 1
            _uncross(kept_bids, kept_asks, None)
        self._levels[ev.venue] = (kept_bids, kept_asks)
        sequence = self._next_sequence(venue)
        self.counts["snapshots"] += 1
        return self._market_event(EventType.BOOK_SNAPSHOT, venue, ev, p.get("timestamp"), sequence, *event_levels)

    def _delta_message(self, frame: list, feed_venue: str, venue: str) -> MarketEvent | None:
        bids, asks = self._levels.setdefault(feed_venue, ({}, {}))
        semantics = self.delta_semantics.get(feed_venue, "hybrid")
        changed_bids: dict[Decimal, Decimal] = {}
        changed_asks: dict[Decimal, Decimal] = {}
        last_side = None
        for ev in frame:
            p = ev.payload
            price = self._text_number(p.get("price", 0) or 0)
            self.counts["delta_records"] += 1
            if price <= 0:
                continue
            side = str(p.get("side", "bid")).lower()
            last_side = side
            book, changed = (bids, changed_bids) if side.startswith("b") else (asks, changed_asks)
            size = _new_size(semantics, str(p.get("event_type", "modify")), book.get(price, Decimal(0)), self._text_number(p.get("volume", p.get("size", 0)) or 0))
            if size > 0:
                book[price] = size
            else:
                book.pop(price, None)
            changed[price] = size
        if not changed_bids and not changed_asks:
            return None
        if bids and asks and max(bids) >= min(asks):         # crossed at message end: the event below still says what the message said (the core invalidates on it); our own levels are repaired as the backtester's book does
            self.counts["crossed_messages"] += 1
            _uncross(bids, asks, last_side)
        sequence = self._next_sequence(venue)
        self.counts["delta_messages"] += 1
        self.counts["levels_changed"] += len(changed_bids) + len(changed_asks)
        last = frame[-1]
        return self._market_event(EventType.BOOK_DELTA, venue, last, frame[0].payload.get("exchange_ts"), sequence,
                                  tuple(Level(px, sz) for px, sz in changed_bids.items()), tuple(Level(px, sz) for px, sz in changed_asks.items()))

    def resync_snapshot(self, venue: str, instrument_id: str, now_ns: int) -> MarketEvent | None:
        """What a resubscribe would deliver at `now_ns`: the exchange state as the data holds it after every record read so far (no later information), repaired of a cross as above, at the
        latest constructed sequence so the deltas that follow chain on. None when nothing has been read for that venue. The session's replay book policy calls this."""
        feed_venue = self._inverse.get(venue, venue.lower())
        levels, sequence = self._levels.get(feed_venue), self._sequence.get(venue)
        if levels is None or sequence is None or instrument_id != self.instrument_id or not (levels[0] and levels[1]):
            return None
        if now_ns + _SAME_STAMP_NS < self._emitted_ns.get(venue, 0):
            raise ValueError(f"resync snapshot asked for {now_ns} but this feed has already handed out {venue} events up to {self._emitted_ns[venue]}: that would be later information; "
                             "replay the feed lazily (one event into the session before the next is converted), never convert it all first")
        self.counts["resync_snapshots"] += 1
        bids, asks = levels
        return MarketEvent(
            event_id=stable_id("canonical", "research-feed-resync", venue, sequence, now_ns), event_type=EventType.BOOK_SNAPSHOT, venue=venue, instrument_id=self.instrument_id,
            exchange_ts_ns=now_ns, receive_ts_ns=now_ns, sequence=sequence, source_ts_ns=now_ns, sequence_start=sequence,
            raw_ref=f"research-feed-resync:{venue}:{now_ns}:{sequence}", adapter_version=ADAPTER_VERSION,
            bids=tuple(Level(px, sz) for px, sz in bids.items()), asks=tuple(Level(px, sz) for px, sz in asks.items()),
            quality_flags=("SEQUENCE_CONSTRUCTED", "RESYNC_SIMULATED"),
        )

    def manifest(self) -> dict:
        """What the run manifest must say about this feed: the sequence numbers are made up here, so their gaps cannot be measured, and a resync snapshot is simulated."""
        return {
            "adapter_version": ADAPTER_VERSION,
            "sequence_numbers": "SYNTHETIC",
            "sequence_gaps": {"status": "NOT_MEASURED", "count": None, "reason": "the files carry no update ids; the sequence is constructed contiguous by the converter, so a gap can never show and zero would mean nothing"},
            "trade_ids": "TRADE_ID_CONSTRUCTED where the record has no id",
            "resync_snapshots": "SIMULATED: the data's own state at the resync time, repaired of a cross; not an exchange snapshot",
            "counts": dict(self.counts),
        }

    def _market_event(self, kind: EventType, venue: str, ev, exchange_ts, sequence: int, bids: tuple, asks: tuple) -> MarketEvent:
        exchange_ns = _ns_exact(exchange_ts if exchange_ts is not None else ev.exchange_ts)
        self._emitted_ns[venue] = max(self._emitted_ns.get(venue, 0), _ns_exact(ev.receive_ts))
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
