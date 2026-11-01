"""Periodic REST cross-check of a live order book (market-data audit P2).

A REST depth snapshot carries a sequence on the same counter as the websocket
feed, so the local book can be compared with it *at that sequence*:

* **Binance** (spot and USDⓈ-M): ``lastUpdateId`` is on the websocket ``u`` counter.
* **Bybit v5**: the REST ``u`` is *not* the websocket ``u``; both carry the
  cross-sequence ``seq`` (websocket ``data.seq``), which is used instead.

A REST response arrives after the websocket has usually moved past its
sequence, so while a request is in flight the live service keeps the book's top
levels after every applied update (``BookHistory``); the snapshot is compared
with the states just before and just after the delta that crossed its sequence
(that delta may bundle changes from both sides of it) and the closer match counts. Only prices inside the snapshot's window are
compared, price by price (a positional comparison would count one missing level
as many).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import heapq
from typing import Any

from .models import MarketEvent
from .orderbook import OrderBook


@dataclass(frozen=True, slots=True)
class RestBook:
    venue: str
    cross_sequence: int
    bids: dict[Decimal, Decimal]
    asks: dict[Decimal, Decimal]
    receive_ts_ns: int
    raw_ref: str = ""


@dataclass(frozen=True, slots=True)
class Comparison:
    levels_compared: int
    differing_prices: tuple[Decimal, ...]
    touch_matches: bool                     # best bid and best ask agree

    @property
    def differences(self) -> int:
        return len(self.differing_prices)


def _levels(rows: Any) -> dict[Decimal, Decimal]:
    levels = {Decimal(str(price)): Decimal(str(quantity)) for price, quantity, *_ in rows}
    return {price: quantity for price, quantity in levels.items() if quantity > 0}


def parse_rest_book(venue: str, payload: dict[str, Any], receive_ts_ns: int, raw_ref: str = "") -> RestBook:
    if venue == "BINANCE":
        return RestBook(venue, int(payload["lastUpdateId"]), _levels(payload["bids"]),
                        _levels(payload["asks"]), receive_ts_ns, raw_ref)
    if payload.get("retCode") != 0:
        raise ValueError(f"Bybit orderbook retCode {payload.get('retCode')}: {payload.get('retMsg')}")
    result = payload["result"]
    return RestBook(venue, int(result["seq"]), _levels(result["b"]), _levels(result["a"]),
                    receive_ts_ns, raw_ref)


def cross_sequence(event: MarketEvent) -> int | None:
    """The event's position on the counter its venue's REST snapshot uses."""
    if event.venue == "BINANCE":
        return event.sequence
    data = (event.native or {}).get("data") if isinstance(event.native, dict) else None
    seq = data.get("seq") if isinstance(data, dict) else None
    return None if seq is None else int(seq)


def top_levels(book: OrderBook, depth: int) -> tuple[dict[Decimal, Decimal], dict[Decimal, Decimal]]:
    bids = {p: book.bids[p] for p in heapq.nlargest(depth, book.bids)}
    asks = {p: book.asks[p] for p in heapq.nsmallest(depth, book.asks)}
    return bids, asks


def compare(bids: dict[Decimal, Decimal], asks: dict[Decimal, Decimal], rest: RestBook) -> Comparison:
    if not rest.bids or not rest.asks or not bids or not asks:
        return Comparison(0, (), False)
    low_bid, high_ask = min(rest.bids), max(rest.asks)
    live_bids = {p: q for p, q in bids.items() if p >= low_bid}
    live_asks = {p: q for p, q in asks.items() if p <= high_ask}
    differing = sorted(
        {p for p in set(live_bids) | set(rest.bids) if live_bids.get(p) != rest.bids.get(p)}
        | {p for p in set(live_asks) | set(rest.asks) if live_asks.get(p) != rest.asks.get(p)}
    )
    touch = max(bids) == max(rest.bids) and min(asks) == min(rest.asks)
    return Comparison(len(rest.bids) + len(rest.asks), tuple(differing), touch)


@dataclass(slots=True)
class BookHistory:
    """Top levels after each applied update while a REST request is in flight."""

    depth: int
    limit: int = 5_000
    states: list[tuple[int, dict[Decimal, Decimal], dict[Decimal, Decimal]]] = field(default_factory=list)
    overflowed: bool = False

    def record(self, sequence: int | None, book: OrderBook) -> None:
        if sequence is None or self.overflowed:
            return
        if len(self.states) >= self.limit:
            self.overflowed = True
            return
        bids, asks = top_levels(book, self.depth)
        self.states.append((sequence, bids, asks))

    def around(self, sequence: int):
        """(state at or before ``sequence``, first state after it); either may be None."""
        before = after = None
        for state in self.states:
            if state[0] <= sequence:
                before = state
            elif after is None:
                after = state
        return before, after


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """Outcome at the delta that crossed the snapshot's sequence."""

    status: str                 # OK | DEEP_DIFF | TOUCH_MISMATCH
    rest_cross_sequence: int
    before: Comparison
    after: Comparison

    @property
    def differences(self) -> int:
        return min(self.before.differences, self.after.differences)


def reconcile(rest: RestBook, before: Comparison, after: Comparison) -> Reconciliation:
    best = min(before.differences, after.differences)
    if best == 0:
        status = "OK"
    elif before.touch_matches or after.touch_matches:
        status = "DEEP_DIFF"            # below the touch: warn (seen: venue REST/WS disagreement)
    else:
        status = "TOUCH_MISMATCH"       # best bid/ask disagree on both sides: the book is wrong
    return Reconciliation(status, rest.cross_sequence, before, after)
