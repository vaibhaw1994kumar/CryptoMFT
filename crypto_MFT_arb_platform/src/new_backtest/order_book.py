"""L2 order book with snapshot resync, incremental deltas, and walk-the-book.

Why this exists rather than reusing the snapshot-only book in
altcoin_mm_v1/state_engine.py: Bybit snapshots in data/bybit/ land at a 1-second
cadence. A cross-venue arb opportunity lives for 10-100ms, so a 1s book is both
too slow to see the edge and — worse — stale enough to manufacture edge that
never existed. The book must be driven by book_events deltas, with the periodic
snapshot used only as a resync that bounds drift.

Structure, and why it is shaped this way: prices live in a plain dict, best
bid/ask come off a lazily-compacted heap, and the sorted price list is cached
and invalidated only on *structural* change (a level appearing or disappearing).
A pure size update at an existing level leaves the ordering intact, so it does
not dirty the cache — and size updates are the large majority of book events.
That keeps the hot path (best bid/ask, touched on every single event) at O(1)
amortised while depth walks, which happen only when a signal fires, pay for a
sort at most once per structural change.

Stdlib only. No sortedcontainers — this has to run under a fresh poetry env on
a locked-down host without reaching for wheels.

Four properties the backtester depends on:

  * staleness. age_s(now) reports how long since the last update. The strategy
    refuses to quote off a book older than a threshold. Without this, a gap in
    one venue's feed reads as a huge (fake) arb.

  * touch confirmation. Measured against data/bybit/ snapshots, the delta
    stream reconstructs level *sizes* correctly 98.9% of the time but the
    *touch* is wrong in ~16% of one-second intervals: the collector emitted
    2,653 cancels against 193,943 modifies and no deletes at all, so a
    consumed best level frequently lingers until the next snapshot. A phantom
    touch on one venue is precisely what fabricates a cross-venue spread.
    touch_age_s() reports how long since each side's best level was last
    confirmed by a snapshot, a delta at that price, or a trade at that price;
    the strategy refuses to hit a touch that has not been confirmed recently.

  * crossed repair with the right prior. When an incoming level crosses the
    book, the level that just arrived is the truth and the side that did not
    update is stale — so that side is trimmed. The trade tape repairs too: a
    buy-aggressor print at p means every ask below p was consumed first.

  * walk-the-book. Taking size larger than the touch consumes multiple levels;
    the fill VWAP, not the touch price, is what the strategy actually gets.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass


@dataclass(frozen=True)
class WalkResult:
    """Outcome of consuming `requested` units against one side of the book."""
    filled_qty: float
    vwap: float
    levels_consumed: int
    exhausted: bool          # ran out of book (or hit the limit price) before filling
    worst_price: float

    @property
    def is_empty(self) -> bool:
        return self.filled_qty <= 0.0


class OrderBook:
    """One venue's L2 book for one instrument."""

    __slots__ = (
        "venue", "pair", "delta_semantics", "last_update_ts", "last_snapshot_ts",
        "updates", "snapshots", "crossed_events", "levels_trimmed",
        "trade_repairs", "trades_applied",
        "_bids", "_asks", "_bid_heap", "_ask_heap",
        "_bid_sorted", "_ask_sorted", "_max_levels",
        "_bid_conf", "_ask_conf",
    )

    def __init__(
        self, venue: str, pair: str,
        delta_semantics: str = "hybrid", max_levels: int = 200,
    ) -> None:
        self.venue = venue
        self.pair = pair
        self.delta_semantics = delta_semantics
        self._max_levels = max_levels

        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}
        self._bid_heap: list[float] = []     # negated prices -> max-heap
        self._ask_heap: list[float] = []     # prices -> min-heap
        self._bid_sorted: list[float] | None = None
        self._ask_sorted: list[float] | None = None
        # Per-level "last confirmed to exist" stamps. Per-level, not per-side:
        # when a confirmed best is removed, the level beneath it must NOT
        # inherit the confirmation — it may be one of the phantoms.
        self._bid_conf: dict[float, float] = {}
        self._ask_conf: dict[float, float] = {}

        self.last_update_ts: float = 0.0
        self.last_snapshot_ts: float = 0.0
        self.updates = 0
        self.snapshots = 0
        self.crossed_events = 0
        self.levels_trimmed = 0
        self.trade_repairs = 0
        self.trades_applied = 0

    # ── Internal index maintenance ───────────────────────────────────────────

    def _set_bid(self, price: float, size: float) -> None:
        if size <= 0:
            if self._bids.pop(price, None) is not None:
                self._bid_sorted = None
            self._bid_conf.pop(price, None)
            return
        if price not in self._bids:
            heapq.heappush(self._bid_heap, -price)
            self._bid_sorted = None
        self._bids[price] = size

    def _set_ask(self, price: float, size: float) -> None:
        if size <= 0:
            if self._asks.pop(price, None) is not None:
                self._ask_sorted = None
            self._ask_conf.pop(price, None)
            return
        if price not in self._asks:
            heapq.heappush(self._ask_heap, price)
            self._ask_sorted = None
        self._asks[price] = size

    def _compact_bids(self) -> None:
        if len(self._bid_heap) > 2 * len(self._bids) + 32:
            self._bid_heap = [-p for p in self._bids]
            heapq.heapify(self._bid_heap)

    def _compact_asks(self) -> None:
        if len(self._ask_heap) > 2 * len(self._asks) + 32:
            self._ask_heap = list(self._asks)
            heapq.heapify(self._ask_heap)

    def _sorted_bids(self) -> list[float]:
        if self._bid_sorted is None:
            self._bid_sorted = sorted(self._bids, reverse=True)
        return self._bid_sorted

    def _sorted_asks(self) -> list[float]:
        if self._ask_sorted is None:
            self._ask_sorted = sorted(self._asks)
        return self._ask_sorted

    # ── State ────────────────────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        return bool(self._bids) and bool(self._asks)

    @property
    def best_bid(self) -> float:
        h, b = self._bid_heap, self._bids
        while h:
            p = -h[0]
            if p in b:
                return p
            heapq.heappop(h)
        return 0.0

    @property
    def best_ask(self) -> float:
        h, a = self._ask_heap, self._asks
        while h:
            p = h[0]
            if p in a:
                return p
            heapq.heappop(h)
        return 0.0

    @property
    def best_bid_size(self) -> float:
        p = self.best_bid
        return self._bids.get(p, 0.0) if p > 0 else 0.0

    @property
    def best_ask_size(self) -> float:
        p = self.best_ask
        return self._asks.get(p, 0.0) if p > 0 else 0.0

    @property
    def mid(self) -> float:
        if not self.ready:
            return 0.0
        return 0.5 * (self.best_bid + self.best_ask)

    @property
    def microprice(self) -> float:
        """Size-weighted touch price — a better short-horizon fair value than mid."""
        if not self.ready:
            return 0.0
        bb, ba = self.best_bid, self.best_ask
        bs, as_ = self._bids.get(bb, 0.0), self._asks.get(ba, 0.0)
        d = bs + as_
        if d <= 0:
            return 0.5 * (bb + ba)
        return (ba * bs + bb * as_) / d

    @property
    def spread_bps(self) -> float:
        m = self.mid
        if m <= 0:
            return 0.0
        return (self.best_ask - self.best_bid) / m * 10_000.0

    def is_crossed(self) -> bool:
        return self.ready and self.best_bid >= self.best_ask

    def age_s(self, now: float) -> float:
        if self.last_update_ts <= 0:
            return float("inf")
        return max(0.0, now - self.last_update_ts)

    def is_stale(self, now: float, max_age_s: float) -> bool:
        return not self.ready or self.age_s(now) > max_age_s

    def touch_age_s(self, side: str, now: float) -> float:
        """Seconds since this side's *current* best level was last confirmed
        to exist by a snapshot, a delta at that price, or a trade at that price."""
        if side.startswith("b"):
            best = self.best_bid
            ts = self._bid_conf.get(best, 0.0) if best > 0 else 0.0
        else:
            best = self.best_ask
            ts = self._ask_conf.get(best, 0.0) if best > 0 else 0.0
        if ts <= 0:
            return float("inf")
        return max(0.0, now - ts)

    # ── Mutation ─────────────────────────────────────────────────────────────

    def apply_snapshot(self, bids, asks, ts: float) -> None:
        """Full replace. Resyncs any drift accumulated from deltas."""
        self._bids.clear()
        self._asks.clear()
        self._bid_heap.clear()
        self._ask_heap.clear()

        for level in bids[: self._max_levels]:
            if len(level) < 2:
                continue
            price, size = float(level[0]), float(level[1])
            if price > 0 and size > 0:
                self._bids[price] = size
        for level in asks[: self._max_levels]:
            if len(level) < 2:
                continue
            price, size = float(level[0]), float(level[1])
            if price > 0 and size > 0:
                self._asks[price] = size

        self._bid_heap = [-p for p in self._bids]
        self._ask_heap = list(self._asks)
        heapq.heapify(self._bid_heap)
        heapq.heapify(self._ask_heap)
        self._bid_sorted = None
        self._ask_sorted = None

        self._bid_conf = {p: ts for p in self._bids}
        self._ask_conf = {p: ts for p in self._asks}

        self.last_update_ts = ts
        self.last_snapshot_ts = ts
        self.snapshots += 1
        if self.is_crossed():
            self.crossed_events += 1
            self._uncross(None)

    def apply_delta(self, side: str, price: float, volume: float, event_type: str, ts: float) -> None:
        """Apply one incremental book event.

        Semantics vary by collector:
          absolute    — every event carries the level's new total size
          decremental — the event carries a size change
          hybrid      — add/modify absolute, cancel decremental (the Bybit
                        collector that wrote data/bybit/ emits this shape)

        Hybrid is the safe default: treating a decremental cancel as absolute
        would resurrect a deleted level at the cancelled size.
        """
        if price <= 0:
            return
        is_bid = side.startswith("b")
        book = self._bids if is_bid else self._asks
        et = event_type.lower()
        current = book.get(price, 0.0)

        if et in ("delete", "remove"):
            new_size = 0.0
        elif et == "cancel":
            new_size = max(0.0, volume) if self.delta_semantics == "absolute" \
                else max(0.0, current - volume)
        elif et in ("add", "insert"):
            new_size = current + max(0.0, volume) if self.delta_semantics == "decremental" \
                else max(0.0, volume)
        else:   # modify / update / set
            new_size = max(0.0, volume)

        if is_bid:
            self._set_bid(price, new_size)
            self._compact_bids()
        else:
            self._set_ask(price, new_size)
            self._compact_asks()

        if len(book) > self._max_levels * 2:
            self._trim(is_bid)

        self.last_update_ts = ts
        self.updates += 1

        if self.is_crossed():
            self.crossed_events += 1
            self._uncross(side)

        # A delta carrying a positive size confirms that level is really there.
        if new_size > 0:
            (self._bid_conf if is_bid else self._ask_conf)[price] = ts

    def on_trade(self, price: float, volume: float, sign: int, ts: float) -> None:
        """Repair the book from the trade tape.

        A buy-aggressor print at p means every ask below p was already taken
        (price priority), and the level at p lost `volume`. Mirror for sells.
        This recovers most of the level removals the collector never emitted,
        and a print at the touch confirms the touch.
        """
        if price <= 0 or volume <= 0:
            return
        self.trades_applied += 1
        if sign > 0:
            # Lifted asks: anything strictly below the print is gone.
            for p in list(self._sorted_asks()):
                if p < price - 1e-12:
                    self._set_ask(p, 0.0)
                    self.trade_repairs += 1
                else:
                    break
            cur = self._asks.get(price)
            if cur is not None:
                self._set_ask(price, cur - volume)
                if price in self._asks:
                    self._ask_conf[price] = ts     # still there: confirmed
            self._compact_asks()
        else:
            for p in list(self._sorted_bids()):
                if p > price + 1e-12:
                    self._set_bid(p, 0.0)
                    self.trade_repairs += 1
                else:
                    break
            cur = self._bids.get(price)
            if cur is not None:
                self._set_bid(price, cur - volume)
                if price in self._bids:
                    self._bid_conf[price] = ts
            self._compact_bids()
        self.last_update_ts = ts

    def _trim(self, is_bid: bool) -> None:
        """Drop the deep tail. Unbounded growth happens when delete events are
        lost — levels far from touch never get removed and walking gets slow."""
        prices = self._sorted_bids() if is_bid else self._sorted_asks()
        book = self._bids if is_bid else self._asks
        for p in prices[self._max_levels:]:
            book.pop(p, None)
            self.levels_trimmed += 1
        if is_bid:
            self._bid_sorted = None
            self._bid_heap = [-p for p in self._bids]
            heapq.heapify(self._bid_heap)
        else:
            self._ask_sorted = None
            self._ask_heap = list(self._asks)
            heapq.heapify(self._ask_heap)

    def _uncross(self, incoming_side: str | None) -> None:
        """Remove crossed levels, trimming the side that did NOT just update.

        A crossed book means a removal was missed. The level that just arrived
        is the exchange telling us what is there now; the side it crosses is
        the stale one. Dropping the smaller side instead (the usual heuristic)
        is a coin flip that deletes the truth half the time — and on this data
        it deleted the incoming level and preserved the phantom touch.
        With no incoming side (a crossed snapshot) fall back to size.
        """
        guard = 0
        while guard < 64 and self._bids and self._asks:
            bb, ba = self.best_bid, self.best_ask
            if bb < ba:
                break
            if incoming_side is None:
                if self._bids.get(bb, 0.0) <= self._asks.get(ba, 0.0):
                    self._set_bid(bb, 0.0)
                else:
                    self._set_ask(ba, 0.0)
            elif incoming_side.startswith("b"):
                self._set_ask(ba, 0.0)      # new bid arrived: stale asks below it
            else:
                self._set_bid(bb, 0.0)      # new ask arrived: stale bids above it
            guard += 1

    # ── Depth queries ────────────────────────────────────────────────────────

    def level_size(self, side: str, price: float) -> float:
        """Displayed size at one price after all events so far (0.0 when the level is absent)."""
        return (self._bids if side.startswith("b") else self._asks).get(price, 0.0)

    def levels(self, side: str, n: int = 10) -> list[tuple[float, float]]:
        if side.startswith("b"):
            prices, book = self._sorted_bids(), self._bids
        else:
            prices, book = self._sorted_asks(), self._asks
        out: list[tuple[float, float]] = []
        for p in prices:
            size = book.get(p)
            if size:
                out.append((p, size))
                if len(out) >= n:
                    break
        return out

    def depth_within(self, side: str, limit_price: float) -> float:
        """Cumulative size at prices at least as good as limit_price."""
        is_bid = side.startswith("b")
        prices, book = (self._sorted_bids(), self._bids) if is_bid \
            else (self._sorted_asks(), self._asks)
        total = 0.0
        for p in prices:
            if is_bid:
                if p < limit_price:
                    break
            elif p > limit_price:
                break
            total += book.get(p, 0.0)
        return total

    def total_depth(self, n: int = 5) -> float:
        return (sum(s for _, s in self.levels("bid", n))
                + sum(s for _, s in self.levels("ask", n)))

    def imbalance(self, n: int = 5) -> float:
        bid = sum(s for _, s in self.levels("bid", n))
        ask = sum(s for _, s in self.levels("ask", n))
        d = bid + ask
        return 0.0 if d <= 0 else (bid - ask) / d

    # ── Taker execution ──────────────────────────────────────────────────────

    def walk(self, side: str, qty: float, limit_price: float | None = None) -> WalkResult:
        """Price `qty` against this side without mutating the book.

        side="ask" consumes asks (we are buying); side="bid" consumes bids (we
        are selling). limit_price caps how far we are willing to walk — this is
        what makes the fill an IOC rather than an unbounded market order, and it
        is what causes partial fills when the book moves during our flight time.
        """
        if qty <= 0:
            return WalkResult(0.0, 0.0, 0, True, 0.0)
        buying = side.startswith("a")
        prices, book = (self._sorted_asks(), self._asks) if buying \
            else (self._sorted_bids(), self._bids)

        remaining = qty
        notional = 0.0
        levels = 0
        worst = 0.0

        for price in prices:
            size = book.get(price, 0.0)
            if size <= 0:
                continue
            if limit_price is not None:
                if buying and price > limit_price:
                    break
                if not buying and price < limit_price:
                    break
            take = size if size < remaining else remaining
            notional += take * price
            remaining -= take
            levels += 1
            worst = price
            if remaining <= 1e-12:
                break

        filled = qty - remaining
        if filled <= 1e-12:
            return WalkResult(0.0, 0.0, 0, True, 0.0)
        return WalkResult(filled, notional / filled, levels, remaining > 1e-12, worst)

    def consume(self, side: str, qty: float, limit_price: float | None = None) -> WalkResult:
        """walk() but actually removes the liquidity.

        Use when modelling own-impact: our taker order really did eat those
        levels, so a second signal in the same instant cannot fill against depth
        we already consumed. The replay stream overwrites the book at the next
        update, which correctly bounds own-impact to sub-tick time.
        """
        result = self.walk(side, qty, limit_price)
        if result.is_empty:
            return result

        buying = side.startswith("a")
        prices = list(self._sorted_asks()) if buying else list(self._sorted_bids())
        book = self._asks if buying else self._bids
        setter = self._set_ask if buying else self._set_bid

        remaining = result.filled_qty
        for price in prices:
            if remaining <= 1e-12:
                break
            size = book.get(price, 0.0)
            if size <= 0:
                continue
            if limit_price is not None:
                if buying and price > limit_price:
                    break
                if not buying and price < limit_price:
                    break
            if size <= remaining + 1e-12:
                remaining -= size
                setter(price, 0.0)
            else:
                setter(price, size - remaining)
                remaining = 0.0
        return result

    # ── Diagnostics ──────────────────────────────────────────────────────────

    def stats(self) -> dict[str, float]:
        return {
            "updates": float(self.updates),
            "snapshots": float(self.snapshots),
            "trades_applied": float(self.trades_applied),
            "trade_repairs": float(self.trade_repairs),
            "crossed_events": float(self.crossed_events),
            "levels_trimmed": float(self.levels_trimmed),
            "bid_levels": float(len(self._bids)),
            "ask_levels": float(len(self._asks)),
        }

    def __repr__(self) -> str:
        if not self.ready:
            return f"<OrderBook {self.venue}:{self.pair} EMPTY>"
        return (
            f"<OrderBook {self.venue}:{self.pair} "
            f"{self.best_bid:.6g} x {self.best_bid_size:.4g} | "
            f"{self.best_ask:.6g} x {self.best_ask_size:.4g} "
            f"spread={self.spread_bps:.2f}bps>"
        )
