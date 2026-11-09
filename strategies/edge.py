"""The one place an arb edge is computed (docs/strategy_contract.md, section 2).

Two profiles:

* ``perp_perp_maker_taker_v1``
  - ``taker_taker_edge_bps``: depth-weighted. The buy VWAP walks the buy venue's asks and the sell VWAP walks the sell venue's
    bids over the levels ``size`` consumes; ``insufficient_depth`` when either book cannot fill the size. Reference price
    (``ref``) = the BUY venue's mid at decision time, so a bps figure means "per unit of the price we are about to pay" and the
    two directions (buy A / sell B, buy B / sell A) are two explicit calls with their own reference, never a sign flip.
  - ``maker_taker_expected_edge_bps``: conditional. The hedge is not executed at the quote it shows now; it is executed after
    the maker fill, at the price the hedge venue has moved to by then. The expected hedge price is the hedge touch now moved by
    ``expected_hedge_move_bps`` (estimated from replay: the hedge venue's move between quote time and fill time, conditional on a
    fill, by side; NEGATIVE = adverse), less expected slippage beyond the touch, less funding/holding, less p_fail x loss_fail.
    The naive edge (current opposite quote locked) is returned alongside so the gap is visible on every signal.

* ``legacy_v0``: ``legacy_taker_taker_edge_bps`` / ``legacy_maker_bid_edge_bps`` / ``legacy_maker_ask_edge_bps`` reproduce the
  expressions used by every result before 2026-11-01 exactly (top of book, reference = buy ask / our maker price / hedge price).

All functions are pure. ``fees_bps`` is the sum of the two legs' fee rates in bps (a maker rebate is negative).
"""
from __future__ import annotations

import bisect
import json
from dataclasses import dataclass, field
from pathlib import Path

INSUFFICIENT_DEPTH = "insufficient_depth"
NO_BOOK = "no_book"
OK = "ok"


@dataclass(frozen=True)
class EdgeResult:
    status: str                      # ok | insufficient_depth | no_book
    gross_bps: float | None          # before fees, relative to ref
    fee_bps: float
    net_bps: float | None            # gross - fees (the caller subtracts its own safety buffer)
    buy_vwap: float | None
    sell_vwap: float | None
    ref: float | None
    ref_name: str
    size: float
    buy_depth: float = 0.0           # depth available on the walked levels (for diagnostics)
    sell_depth: float = 0.0
    naive_bps: float | None = None   # maker/taker only: the edge with the current opposite quote locked, before fees
    components: dict | None = None   # maker/taker only: each deduction in bps


def vwap_for_size(book, side: str, size: float) -> tuple[float | None, float]:
    """(VWAP over the levels `size` consumes on `side` ("bid" | "ask"), depth walked). VWAP is None when the book cannot fill
    the size. Walks best-first; a level is consumed in full until the last one, which is consumed partially."""
    if size <= 0 or book is None or not getattr(book, "ready", False):
        return None, 0.0
    is_bid = side.startswith("b")
    prices = book._sorted_bids() if is_bid else book._sorted_asks()
    levels = book._bids if is_bid else book._asks
    left, cost, walked = size, 0.0, 0.0
    for p in prices:
        q = levels.get(p, 0.0)
        if q <= 0:
            continue
        take = min(q, left)
        cost += take * p
        walked += take
        left -= take
        if left <= 1e-12:
            return cost / size, walked
    return None, walked


def taker_taker_edge_bps(buy_book, sell_book, size: float, buy_fee_bps: float, sell_fee_bps: float, ref: float | None = None) -> EdgeResult:
    """Depth-weighted taker/taker edge: 1e4 x (vwap_bid_sell(size) - vwap_ask_buy(size)) / ref - fees.
    ref defaults to the BUY venue's mid at decision time (ref_name "buy_mid")."""
    fee = buy_fee_bps + sell_fee_bps
    if buy_book is None or sell_book is None or not buy_book.ready or not sell_book.ready:
        return EdgeResult(NO_BOOK, None, fee, None, None, None, None, "buy_mid", size)
    r = ref if ref is not None else buy_book.mid
    if r is None or r <= 0:
        return EdgeResult(NO_BOOK, None, fee, None, None, None, None, "buy_mid", size)
    buy_vwap, bd = vwap_for_size(buy_book, "ask", size)
    sell_vwap, sd = vwap_for_size(sell_book, "bid", size)
    if buy_vwap is None or sell_vwap is None:
        return EdgeResult(INSUFFICIENT_DEPTH, None, fee, None, buy_vwap, sell_vwap, r, "buy_mid", size, bd, sd)
    gross = (sell_vwap - buy_vwap) / r * 10_000.0
    return EdgeResult(OK, gross, fee, gross - fee, buy_vwap, sell_vwap, r, "buy_mid", size, bd, sd)


def maker_taker_expected_edge_bps(q: float, side: str, hedge_book_now, size: float, maker_fee_bps: float, taker_fee_bps: float, *,
                                  expected_hedge_move_bps: float = 0.0, expected_hedge_slippage_bps: float = 0.0,
                                  funding_holding_bps: float = 0.0, p_fail: float = 0.0, loss_fail_bps: float = 0.0,
                                  ref: float | None = None) -> EdgeResult:
    """Conditional expected edge of resting at price q on the maker venue (side "bid": we buy at q and the hedge SELLS on
    hedge_book_now's bid; side "ask": we sell at q and the hedge BUYS on its ask).

        edge = (E[hedge price | fill] - q) / ref        (bid)   or   (q - E[hedge price | fill]) / ref   (ask)
               - maker_fee - taker_fee - E[hedge slippage] - funding/holding - p_fail x loss_fail

    E[hedge price | fill] = hedge touch now x (1 + expected_hedge_move_bps / 1e4) for a sell hedge (a negative move lowers the bid:
    adverse) and x (1 - expected_hedge_move_bps / 1e4) for a buy hedge (a negative move raises the ask: adverse). ref defaults to
    the price we pay: q for a bid, the expected hedge price for an ask (ref_name "buy_price")."""
    fee = maker_fee_bps + taker_fee_bps
    if hedge_book_now is None or not hedge_book_now.ready or q <= 0 or size <= 0:
        return EdgeResult(NO_BOOK, None, fee, None, None, None, None, "buy_price", size)
    touch = hedge_book_now.best_bid if side == "bid" else hedge_book_now.best_ask
    if touch <= 0:
        return EdgeResult(NO_BOOK, None, fee, None, None, None, None, "buy_price", size)
    if side == "bid":
        expected = touch * (1.0 + expected_hedge_move_bps / 10_000.0)
        r = ref if ref is not None else q
        gross = (expected - q) / r * 10_000.0
        naive = (touch - q) / r * 10_000.0
        buy_vwap, sell_vwap = q, expected
    else:
        expected = touch * (1.0 - expected_hedge_move_bps / 10_000.0)
        r = ref if ref is not None else expected
        gross = (q - expected) / r * 10_000.0
        naive = (q - touch) / r * 10_000.0
        buy_vwap, sell_vwap = expected, q
    deductions = {"maker_fee_bps": maker_fee_bps, "taker_fee_bps": taker_fee_bps, "hedge_slippage_bps": expected_hedge_slippage_bps,
                  "funding_holding_bps": funding_holding_bps, "failure_cost_bps": p_fail * loss_fail_bps, "hedge_move_bps": expected_hedge_move_bps}
    net = gross - fee - expected_hedge_slippage_bps - funding_holding_bps - p_fail * loss_fail_bps
    return EdgeResult(OK, gross, fee, net, buy_vwap, sell_vwap, r, "buy_price", size, naive_bps=naive, components=deductions)


# ── legacy_v0: the expressions used before 2026-11-01, unchanged ───────────────────────────────────────────────────────

def legacy_taker_taker_edge_bps(buy_px: float, sell_px: float, buy_fee_bps: float, sell_fee_bps: float) -> tuple[float, float]:
    """(gross, net) with gross = (sell_bid - buy_ask) / buy_ask x 1e4 (top of book, reference = buy ask); net = gross - fees."""
    gross = (sell_px - buy_px) / buy_px * 10_000.0
    return gross, gross - (buy_fee_bps + sell_fee_bps)


def legacy_maker_bid_edge_bps(hedge_px: float, max_bid: float, fee_bps: float) -> tuple[float, float]:
    """(gross, net) with gross = (hedge_bid - max_bid) / max_bid x 1e4 (reference = our maker price)."""
    gross = (hedge_px - max_bid) / max_bid * 10_000.0
    return gross, gross - fee_bps


def legacy_maker_ask_edge_bps(min_ask: float, hedge_px: float, fee_bps: float) -> tuple[float, float]:
    """(gross, net) with gross = (min_ask - hedge_ask) / hedge_ask x 1e4 (reference = the hedge price we pay)."""
    gross = (min_ask - hedge_px) / hedge_px * 10_000.0
    return gross, gross - fee_bps


# ── S1 (owner 9 Nov): the adverse-selection table ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
class AdverseTable:
    """Expected hedge-venue mid move after a public print that would fill a passive quote, by the state at the print, fitted from MARKET-DATA LABELS ONLY (no strategy fill, simulated or not).
    Negative = adverse, the sign convention of `maker_taker_expected_edge_bps`' `expected_hedge_move_bps`. It replaces that single number per side and the uncalibrated toxic thresholds.
    A cell is a side plus a bucket of each dimension in `dims`, in this order, taken from: depth (ticks behind the touch the print reached), vol (hedge-mid volatility, bps), flow (signed same-direction maker-venue
    volume just before the print), spread (maker-venue spread, bps). Each cell's mean is SHRUNK toward its parent (the same key without the last dimension; the root is the side mean):
    estimate = (n x cell mean + k x parent estimate) / (n + k), so a thin cell is mostly its parent. A lookup uses the full key when the cell exists, else the longest existing prefix, else the side mean.
    Fitted on TRAIN days, scheme and k chosen on SELECT, never on HOLDOUT (the caller's rule, recorded in `fitted_on`)."""

    DIMS = ("depth", "vol", "flow", "spread")

    def __init__(self, dims: tuple, edges: dict, shrink_k: float, horizons_s: tuple, cells: dict, side_mean: dict, fitted_on: dict | None = None, version: str = "adverse_table_v1") -> None:
        bad = [d for d in dims if d not in self.DIMS]
        if bad or len(set(dims)) != len(dims):
            raise ValueError(f"dims must be distinct names from {self.DIMS}, not {dims}")
        self.dims, self.edges, self.shrink_k, self.horizons_s = tuple(dims), {d: tuple(edges.get(d, ())) for d in dims}, float(shrink_k), tuple(float(h) for h in horizons_s)
        self.cells, self.side_mean, self.fitted_on, self.version = cells, side_mean, dict(fitted_on or {}), version

    @staticmethod
    def _bucket(x, edges: tuple) -> str:
        return "na" if x is None else str(bisect.bisect_right(edges, x))

    def parts(self, side: str, depth_ticks=0, vol_bps=None, flow=0.0, spread_bps=None) -> list:
        value = {"depth": depth_ticks, "vol": vol_bps, "flow": flow, "spread": spread_bps}
        return [side] + [self._bucket(value[d], self.edges[d]) for d in self.dims]

    def expected_move_bps(self, side: str, *, depth_ticks=0, vol_bps=None, flow=0.0, spread_bps=None, horizon_s: float = 1.0) -> float:
        if side not in ("bid", "ask"):
            raise ValueError("side must be bid or ask")
        h = f"{float(horizon_s):g}"
        if h not in {f"{x:g}" for x in self.horizons_s}:
            raise ValueError(f"horizon {horizon_s} is not in {self.horizons_s}")
        parts = self.parts(side, depth_ticks, vol_bps, flow, spread_bps)
        for n in range(len(parts), 1, -1):
            cell = self.cells.get("|".join(parts[:n]))
            if cell is not None:
                return cell["est"][h]
        return self.side_mean[side][h]

    def expected_move_reaching_bps(self, side: str, *, quote_depth_ticks=0, vol_bps=None, flow=0.0, spread_bps=None, horizon_s: float = 1.0) -> float:
        """The expected move for a RESTING quote `quote_depth_ticks` behind the touch. Each label is keyed by how deep its sweep went, and a quote that deep is filled by every sweep that reached it or went deeper,
        so this is the mean over the depth buckets at or beyond the quote's, weighted by how many prints each bucket holds (its `n`), each bucket read at the given vol, flow and spread. Without a depth
        dimension it is the plain lookup."""
        if "depth" not in self.dims:
            return self.expected_move_bps(side, depth_ticks=quote_depth_ticks, vol_bps=vol_bps, flow=flow, spread_bps=spread_bps, horizon_s=horizon_s)
        h = f"{float(horizon_s):g}"
        top = len(self.edges["depth"])                              # buckets 0..top
        first = int(self._bucket(quote_depth_ticks, self.edges["depth"]))
        rest = self.parts(side, quote_depth_ticks, vol_bps, flow, spread_bps)[2:]
        num = den = 0.0
        for b in range(first, top + 1):
            mass = (self.cells.get(f"{side}|{b}") or {}).get("n", {}).get(h, 0)
            if mass <= 0:
                continue
            parts = [side, str(b)] + rest
            for n in range(len(parts), 1, -1):
                cell = self.cells.get("|".join(parts[:n]))
                if cell is not None:
                    num += mass * cell["est"][h]
                    den += mass
                    break
        return num / den if den > 0 else self.side_mean[side][h]

    def to_dict(self) -> dict:
        return {"version": self.version, "dims": list(self.dims), "edges": {d: list(e) for d, e in self.edges.items()}, "shrink_k": self.shrink_k, "horizons_s": list(self.horizons_s),
                "cells": self.cells, "side_mean": self.side_mean, "fitted_on": self.fitted_on,
                "convention": "hedge-venue mid move in bps after a print that fills a passive quote; negative = adverse; shrunk toward the parent cell"}

    @classmethod
    def from_dict(cls, d: dict) -> "AdverseTable":
        return cls(tuple(d["dims"]), {k: tuple(v) for k, v in d["edges"].items()}, d["shrink_k"], tuple(d["horizons_s"]), d["cells"], d["side_mean"], d.get("fitted_on"), d.get("version", "adverse_table_v1"))

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=1, sort_keys=True))

    @classmethod
    def load(cls, path) -> "AdverseTable":
        return cls.from_dict(json.loads(Path(path).read_text()))


def fit_adverse_table(rows, *, dims=("depth",), depth_edges=(1, 2, 4, 8), vol_edges=(), flow_edges=(), spread_edges=(), shrink_k: float = 100.0, horizons_s=(1.0, 5.0, 30.0),
                      fitted_on: dict | None = None, version: str = "adverse_table_v1") -> AdverseTable:
    """`rows`: dicts with side, depth_ticks, vol_bps, flow, spread_bps and move_<h>s (None = unresolved, skipped for that horizon). Pure; deterministic."""
    edges = {"depth": depth_edges, "vol": vol_edges, "flow": flow_edges, "spread": spread_edges}
    table = AdverseTable(dims, edges, shrink_k, horizons_s, {}, {}, fitted_on, version)
    hs = [f"{float(h):g}" for h in horizons_s]
    # statistics per prefix: (n, sum) per horizon; root = side
    acc: dict[str, dict] = {}
    for r in rows:
        parts = table.parts(r["side"], r.get("depth_ticks", 0), r.get("vol_bps"), r.get("flow", 0.0), r.get("spread_bps"))
        for n in range(1, len(parts) + 1):
            key = "|".join(parts[:n])
            a = acc.setdefault(key, {h: [0, 0.0] for h in hs})
            for h in hs:
                v = r.get(f"move_{h}s")
                if v is not None:
                    a[h][0] += 1
                    a[h][1] += v
    for side in ("bid", "ask"):
        table.side_mean[side] = {h: (acc[side][h][1] / acc[side][h][0]) if side in acc and acc[side][h][0] else 0.0 for h in hs}
    for key in sorted(acc, key=lambda k: k.count("|")):
        if "|" not in key:
            continue
        parent_key = key.rsplit("|", 1)[0]
        est, n_out = {}, {}
        for h in hs:
            n, total = acc[key][h]
            parent = table.side_mean[key.split("|")[0]][h] if "|" not in parent_key else table.cells[parent_key]["est"][h]
            est[h] = (total + shrink_k * parent) / (n + shrink_k) if (n + shrink_k) > 0 else parent
            n_out[h] = n
        table.cells[key] = {"n": n_out, "est": est}
    return table


class AdverseFeatures:
    """The quote-time features of `AdverseTable`, defined as `new_backtest.market_hedge_move.day_labels` defines them at a print: the hedge mid is sampled once per whole second, `vol_bps` is the standard
    deviation of the last `vol_window_s` seconds' 1 s changes in bps of the current mid (None until 5 changes), and the flow of a side is the maker venue's signed volume of the last `flow_window_s` seconds,
    positive in the direction of the print that would fill that side's quote (a passive bid is filled by a sell print, an ask by a buy print)."""

    def __init__(self, vol_window_s: float = 30.0) -> None:
        self.vol_window_s = float(vol_window_s)
        self._mids: dict[str, list] = {}                        # venue -> [(whole second, mid)]
        self._last_sec: dict[str, int] = {}

    def on_mid(self, venue: str, ts: float, mid: float) -> None:
        sec = int(ts)
        if mid <= 0 or self._last_sec.get(venue) == sec:
            return
        self._last_sec[venue] = sec
        hist = self._mids.setdefault(venue, [])
        hist.append((sec, mid))
        while hist and hist[0][0] < sec - self.vol_window_s:
            hist.pop(0)

    def vol_bps(self, venue: str, mid: float):
        hist = self._mids.get(venue) or []
        ch = [b - a for (_, a), (_, b) in zip(hist, hist[1:])]
        if len(ch) < 5 or mid <= 0:
            return None
        m = sum(ch) / len(ch)
        return (sum((x - m) ** 2 for x in ch) / (len(ch) - 1)) ** 0.5 / mid * 1e4

    @staticmethod
    def side_flow(signed_volume: float, side: str) -> float:
        """`signed_volume`: maker-venue volume, + for buy-aggressor prints. A bid is hit by sell prints, so its flow is the negative of it; an ask's is the volume itself."""
        return -signed_volume if side == "bid" else signed_volume

