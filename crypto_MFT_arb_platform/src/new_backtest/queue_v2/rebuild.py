"""Queue model v2, step 2: an exact L2 book and the rebuild-acceptance harness (guide s2).

ExactBook keeps prices and sizes as integers (value x 1e8, parsed from the raw JSON text, never accumulated in binary floating point).
It applies a message as "this level's size is now N (0 removes it)", applies a frame of messages as one unit, never deletes a level to repair a crossed
book (it records the crossing instead) and goes invalid across a declared gap until the next snapshot.

Acceptance: between two reference snapshots every variant is rebuilt from deltas only (reset to snapshot k), and compared with snapshot k+1 at its own
timestamp: touch price and size on both sides, and the top-10 levels (price and size). Variants: the exact book per message, the exact book per frame, and the
engine's OrderBook with and without tape repair. Run it under both event orders to see what the order of equal timestamps does."""
from __future__ import annotations

import json
from collections import Counter

SCALE = 10 ** 8


def to_int(x) -> int:
    return int(round(float(x) * SCALE))


class ExactBook:
    __slots__ = ("bids", "asks", "valid", "crossed_events", "messages")

    def __init__(self) -> None:
        self.bids: dict[int, int] = {}
        self.asks: dict[int, int] = {}
        self.valid = False
        self.crossed_events = 0
        self.messages = 0

    def reset(self, bids, asks) -> None:
        """A snapshot replaces the book wholesale and makes it valid again."""
        self.bids = {to_int(p): to_int(s) for p, s in bids if to_int(s) > 0}
        self.asks = {to_int(p): to_int(s) for p, s in asks if to_int(s) > 0}
        self.valid = True

    def set_level(self, side: str, price: int, size: int) -> None:
        """amount = the level's NEW absolute size; zero removes it."""
        book = self.bids if side.startswith("b") else self.asks
        self.messages += 1
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size

    def apply_frame(self, msgs: list[tuple[str, int, int]]) -> None:
        """All messages of one frame take effect together; the crossed-book check runs once, after the last one."""
        for side, price, size in msgs:
            self.set_level(side, price, size)
        self.check_cross()

    def check_cross(self) -> bool:
        if self.bids and self.asks and max(self.bids) >= min(self.asks):
            self.crossed_events += 1
            return True
        return False

    def invalidate(self) -> None:
        self.valid = False

    def top(self, side: str, n: int) -> list[tuple[int, int]]:
        book = self.bids if side.startswith("b") else self.asks
        return [(p, book[p]) for p in sorted(book, reverse=side.startswith("b"))[:n]]


def _engine_top(book, side: str, n: int) -> list[tuple[int, int]]:
    return [(to_int(p), to_int(s)) for p, s in book.levels(side, n)]


def compare(variant_top, ref_bids, ref_asks, n: int = 10) -> dict:
    """variant_top(side, n) -> [(price, size)] in the same integer scale. Returns the touch and top-n agreement with one reference snapshot."""
    rb = [(to_int(p), to_int(s)) for p, s in ref_bids[:n]]
    ra = [(to_int(p), to_int(s)) for p, s in ref_asks[:n]]
    vb, va = variant_top("bid", n), variant_top("ask", n)
    out = {"touch_price": int(bool(rb and ra and vb and va and vb[0][0] == rb[0][0] and va[0][0] == ra[0][0])),
           "touch_size": int(bool(rb and ra and vb and va and vb[0] == rb[0] and va[0] == ra[0]))}
    lv_ok = lv_n = 0
    for ref, var in ((rb, vb), (ra, va)):
        for i, lvl in enumerate(ref):
            lv_n += 1
            lv_ok += int(i < len(var) and var[i] == lvl)
    out["levels_ok"], out["levels_n"] = lv_ok, lv_n
    return out


def acceptance(events, variants: dict, day: str, max_mismatches: int = 20) -> dict:
    """events: iterable of RawEvent (kinds 'book', 'book_event', 'trade'). variants: name -> object with .reset/.delta/.trade/.top/.frame_flush."""
    stats = {v: Counter() for v in variants}
    mismatches = {v: [] for v in variants}
    last_msgs: list[dict] = []
    n_snap = 0
    first = True
    for ev in events:
        k = ev.kind
        p = ev.payload
        if k == "book":
            bids, asks = p.get("bids") or [], p.get("asks") or []
            if not first:
                n_snap += 1
                for name, v in variants.items():
                    v.flush()
                    c = compare(v.top, bids, asks)
                    s = stats[name]
                    s["snapshots"] += 1
                    s["touch_price_ok"] += c["touch_price"]
                    s["touch_size_ok"] += c["touch_size"]
                    s["levels_ok"] += c["levels_ok"]
                    s["levels_n"] += c["levels_n"]
                    if not c["touch_size"] and len(mismatches[name]) < max_mismatches:
                        mismatches[name].append({"ts": ev.exchange_ts, "ref_touch": [bids[:1], asks[:1]],
                                                 "variant_touch": [v.top("bid", 1), v.top("ask", 1)], "last_messages": last_msgs[-3:]})
            for v in variants.values():
                v.reset(bids, asks)
            first = False
            last_msgs.clear()
        elif k == "book_event":
            side = str(p.get("side", "bid")).lower()
            price, size = to_int(p.get("price", 0)), to_int(p.get("volume", p.get("size", 0)) or 0)
            et = str(p.get("event_type", "modify")).lower()
            if et in ("delete", "remove"):
                size = 0
            key = (p.get("exchange_ts"), p.get("local_ts"))
            last_msgs.append({"ts": ev.exchange_ts, "side": side, "price": price / SCALE, "size": size / SCALE, "type": et})
            if len(last_msgs) > 6:
                del last_msgs[0]
            for v in variants.values():
                v.delta(side, price, size, key)
        elif k == "trade":
            price, vol = float(p.get("price", 0) or 0), float(p.get("volume", p.get("size", 0)) or 0)
            side = p.get("side", "b")
            sign = 1 if (side.lower().startswith("b") if isinstance(side, str) else float(side) > 0) else -1
            for v in variants.values():
                v.trade(price, vol, sign, ev.exchange_ts)
    out = {"day": day, "snapshots_compared": n_snap, "variants": {}}
    for name, s in stats.items():
        n = max(1, s["snapshots"])
        out["variants"][name] = {"snapshots": s["snapshots"], "touch_price_match_pct": round(100.0 * s["touch_price_ok"] / n, 3),
                                 "touch_size_match_pct": round(100.0 * s["touch_size_ok"] / n, 3),
                                 "top10_level_match_pct": round(100.0 * s["levels_ok"] / max(1, s["levels_n"]), 3),
                                 "extra": variants[name].extra(), "mismatch_examples": mismatches[name]}
    return out


class ExactMessage:
    """One message at a time; a crossed book is counted, never repaired."""
    def __init__(self) -> None:
        self.b = ExactBook()
    def reset(self, bids, asks): self.b.reset(bids, asks)
    def delta(self, side, price, size, key):
        self.b.set_level(side, price, size)
        self.b.check_cross()
    def trade(self, *a): pass
    def flush(self): pass
    def top(self, side, n): return self.b.top(side, n)
    def extra(self): return {"crossed_events": self.b.crossed_events}


class ExactFrame:
    """Messages with the same (exchange time, receive time) form one frame and take effect together."""
    def __init__(self) -> None:
        self.b = ExactBook()
        self.pending: list = []
        self.key = None
    def reset(self, bids, asks):
        self.pending, self.key = [], None
        self.b.reset(bids, asks)
    def delta(self, side, price, size, key):
        if key != self.key:
            self.flush()
            self.key = key
        self.pending.append((side, price, size))
    def trade(self, *a): pass
    def flush(self):
        if self.pending:
            self.b.apply_frame(self.pending)
            self.pending = []
    def top(self, side, n): return self.b.top(side, n)
    def extra(self): return {"crossed_events": self.b.crossed_events}


class EngineBook:
    """The engine's OrderBook (legacy delta semantics, per-message uncross) with tape repair on or off."""
    def __init__(self, venue: str, tape_repair: bool) -> None:
        from ..order_book import OrderBook
        self.OrderBook = OrderBook
        self.venue, self.tape_repair = venue, tape_repair
        self.book = OrderBook(venue, "X", tape_repair=tape_repair)
        self.ts = 0.0
    def reset(self, bids, asks):
        self.book.apply_snapshot(bids, asks, self.ts)
    def delta(self, side, price, size, key):
        self.ts = key[0] or self.ts
        self.book.apply_delta(side, price / SCALE, size / SCALE, "modify", self.ts)
    def trade(self, price, vol, sign, ts):
        self.ts = ts
        self.book.on_trade(price, vol, sign, ts)
    def flush(self): pass
    def top(self, side, n): return _engine_top(self.book, side, n)
    def extra(self): return {"trade_repairs": self.book.trade_repairs, "crossed_events": self.book.crossed_events, "tape_repair": self.tape_repair}


def default_variants(venue: str) -> dict:
    return {"exact_message": ExactMessage(), "exact_frame": ExactFrame(), "engine_tape_repair_on": EngineBook(venue, True),
            "engine_tape_repair_off": EngineBook(venue, False)}
