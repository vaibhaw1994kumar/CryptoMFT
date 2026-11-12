"""Observation records for the maker leg (execution-layer brief, steps 6 and 7). OBSERVATION ONLY: nothing in the engine reads them,
they draw no random number and change no state, so a replay with them on is identical to one without (gate 0, new log fields excluded).

Per maker order, at placement: relative queue position (ahead / level size), near-side and opposite-side size at our price, the
imbalance (B - A) / (B + A) at the touch on BOTH venues, and the hedge venue's touch at the decision. At each maker fill: the same
picture, plus the fill record the fill engine attaches (cause tag / trade id, volume offered and consumed, queue estimate before the
fill, netting credit, cancel model, uncertainty flags). At each hedge arrival: the hedge venue's touch.
"""
from __future__ import annotations

import json
from collections import Counter

OBS_MAX = 300_000
_TRADE_ID_FIELDS = ("trade_id", "agg_id", "t", "a", "i", "id")


def touch_doc(book) -> dict | None:
    """Best bid / ask, their sizes and the touch imbalance (B - A) / (B + A); None when the book has no two-sided touch."""
    if book is None or not book.ready:
        return None
    b, a = book.best_bid, book.best_ask
    bs, as_ = book.level_size("bid", b), book.level_size("ask", a)
    tot = bs + as_
    return {"bid": b, "ask": a, "bid_size": bs, "ask_size": as_, "imbalance": (bs - as_) / tot if tot > 0 else None}


def trade_identity(payload) -> dict:
    """The causal print's own id when the record carries one (Binance t / aggTrade a / Bybit i / trade_id); Jan-2022 files carry none."""
    if isinstance(payload, dict):
        for f in _TRADE_ID_FIELDS:
            v = payload.get(f)
            if v not in (None, ""):
                return {"trade_id": str(v), "trade_id_field": f}
    return {"trade_id": None}


class ObservationLog:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.counts: Counter = Counter()
        self.dropped = 0

    def add(self, kind: str, row: dict) -> None:
        self.counts[kind] += 1
        if len(self.rows) < OBS_MAX:
            self.rows.append({"obs": kind, **row})
        else:
            self.dropped += 1

    def summary(self, taker=None) -> dict:
        flags: Counter = Counter()
        causes: Counter = Counter()
        for r in self.rows:
            if r["obs"] == "maker_fill":
                causes[r.get("fill", {}).get("cause", "")] += 1
                for f in r.get("fill", {}).get("flags", []):
                    flags[f] += 1
        out = {"counts": dict(self.counts), "rows_kept": len(self.rows), "rows_dropped": self.dropped, "maker_fill_causes": dict(causes),
               "maker_fill_uncertainty_flags": dict(flags)}
        if taker is not None:
            out["own_taker_book_consumption"] = {"consume_liquidity": taker.consume_liquidity, "fills_that_removed_liquidity": taker.consume_calls,
                                                 "qty_removed_from_the_view_book": taker.consumed_qty}
        return out

    def write_jsonl(self, path) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            for r in self.rows:
                fh.write(json.dumps(r, sort_keys=True, default=str) + "\n")


class SessionObservations:
    """E0 step (g): the maker observation records on the session runtime, ported from the backtester's `_obs_place`, `_obs_fill` and `_obs_hedge_arrival`. The queue adapters call `place`, `fill` and
    `hedge_arrival`; each only appends a row to the ObservationLog. OBSERVATION ONLY: no random number, no state change, and nothing in the engine reads the rows (a test scans for any reader). The row layout
    is the backtester's, with `order_id` (the OMS order) beside `maker_order_id` (the simulator's) and the layer's `intent_id`. `views` maps the feed venue to its float view book."""

    def __init__(self, log: "ObservationLog", views: dict, intent_of) -> None:
        self.log, self.views, self.intent_of = log, views, intent_of

    def _touches(self) -> dict:
        return {v: touch_doc(b) for v, b in self.views.items()}

    def place(self, adapter, request, mo, now: float) -> None:
        view = self.views[adapter.feed_venue]
        lvl = float(getattr(mo, "observed_level_size", 0.0) or 0.0)
        ahead = getattr(mo, "queue_ahead", None)
        side = "buy" if getattr(mo.side, "value", mo.side) in ("bid", "buy", "BID") else "sell"
        self.log.add("maker_place", {
            "ts": now, "maker_order_id": mo.order_id, "order_id": request.order_id, "intent_id": self.intent_of(request.order_id), "venue": adapter.feed_venue, "side": side,
            "price": float(request.price), "size": float(request.quantity), "queue_ahead": ahead, "level_size": lvl,
            "relative_queue_position": (ahead / lvl) if (ahead is not None and lvl > 0) else None,
            "queue_unknown": getattr(mo, "queue_unknown", None), "level_was_absent": getattr(mo, "level_was_absent", None),
            "relative_to_touch_ticks": getattr(mo, "relative_to_touch_ticks", None), "touch": self._touches(), "touch_own_venue": touch_doc(view)})

    def fill(self, adapter, request, fill) -> None:
        ob = dict(getattr(fill, "obs", None) or {})
        trig = dict(ob.pop("trigger", None) or {})
        if getattr(fill, "is_synthetic", False):
            trig["kind"] = "synthetic"
        lvl = ob.get("level_now") or 0.0
        qb = ob.get("queue_before")
        self.log.add("maker_fill", {
            "ts": fill.ts, "maker_order_id": fill.order_id, "order_id": request.order_id, "intent_id": self.intent_of(request.order_id), "venue": adapter.feed_venue,
            "side": fill.side, "price": fill.price, "qty": fill.qty, "relative_queue_position_at_fill": (qb / lvl) if (qb is not None and lvl > 0) else None,
            "touch": self._touches(), "trigger": trig, "fill": ob})

    def hedge_arrival(self, adapter, request, now: float) -> None:
        self.log.add("hedge_arrival", {
            "ts": now, "order_id": request.order_id, "intent_id": self.intent_of(request.order_id), "venue": adapter.feed_venue,
            "side": "buy" if str(getattr(request.side, "value", request.side)).lower() == "buy" else "sell", "qty": float(request.quantity),
            "limit_price": None if request.price is None else float(request.price), "touch_at_arrival": touch_doc(self.views.get(adapter.feed_venue))})

