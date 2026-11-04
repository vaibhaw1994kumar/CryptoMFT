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
