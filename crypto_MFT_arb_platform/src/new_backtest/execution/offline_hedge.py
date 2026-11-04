"""Offline evaluator for the hedge policy (phase 1): replay one policy cell over the hedge decisions recorded by a capture replay.

A capture replay (ArbBacktestConfig.record_hedge_attempts) stores, per hedge attempt, the decision-time view touch, and for the hedge
venue the top-5 book after every update in [decision - 1 s, decision + 10 s]. Here the book at any time in that window is rebuilt and
the engine's own TakerExecutor runs on it, so a fill is decided by the same code as in a replay.

What this does NOT model (so numbers from it are OFFLINE_FIRST_ORDER, never end-to-end): a slower or failed hedge keeps exposure open
longer, which blocks later signals and moves breaker trips; the Bybit unwind is priced as a flat penalty, not played; arrival latency
is re-sampled (paired across cells: the same seeds in every cell), not the engine's own draw.
Validation (`validate`): with the phase-0 hedge and the RECORDED arrival times and limits, every recorded attempt must come out
the same (filled quantity, VWAP, reason). Anything else means the path is not faithful and the sweep must not be trusted.
"""
from __future__ import annotations

import bisect
import gzip
import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..fill_engine import TakerExecutor
from ..order_book import OrderBook
from .ioc_policy import attempts_from_latency, choose, limit_price
from .ioc_runtime import IocRuntime, median


def load_capture(path: str | Path):
    with gzip.open(path, "rt") as g:
        d = json.load(g)
    return d["attempts"], d["paths"]


class Path5:
    """The hedge venue's top-5 path: book_at(ts) = the book after the last update at or before / strictly before ts."""

    def __init__(self, entries):
        self.e = sorted(entries, key=lambda x: x[0])
        self.ts = [x[0] for x in self.e]

    def _build(self, i: int, venue: str) -> OrderBook | None:
        if i < 0:
            return None
        ts, bids, asks = self.e[i]
        b = OrderBook(venue, "SOLUSDT")
        b.apply_snapshot([(p, s) for p, s in bids], [(p, s) for p, s in asks], ts)
        return b

    def at_or_before(self, t: float, venue: str = "binance"):
        return self._build(bisect.bisect_right(self.ts, t) - 1, venue)

    def strictly_before(self, t: float, venue: str = "binance"):
        return self._build(bisect.bisect_left(self.ts, t) - 1, venue)

    def touch_window(self, t: float, w: float = 0.25):
        i = bisect.bisect_right(self.ts, t)
        out = [(self.e[k][1][0][0], self.e[k][2][0][0]) for k in range(max(0, bisect.bisect_left(self.ts, t - w)), i) if self.e[k][1] and self.e[k][2]]
        return out


def _execute(book: OrderBook, side: str, qty: float, limit: float, fee_bps: float, expected: float):
    out = TakerExecutor().execute(book=book, side=side, qty=qty, limit_price=limit, fee_bps=fee_bps, now=0.0, expected_price=expected)
    if out.filled and out.fill is not None:
        return out.fill.qty, out.fill.price, out.reason
    return 0.0, None, out.reason


def validate(attempts, paths, *, fee_by_venue=None) -> dict:
    """Recompute every recorded attempt (every hedge venue) from the path at its recorded arrival with its recorded limit."""
    fee_by_venue = fee_by_venue or {"binance": 5.0, "bybit": 5.5}
    pbs = {v: Path5(paths[v]) for v in paths if paths[v]}
    n = ok = 0
    bad = []
    by_venue = defaultdict(lambda: [0, 0])
    for r in attempts:
        venue = r["venue"]
        if r.get("fault") or r.get("ghost") or venue not in pbs:
            continue                                        # injected faults are not part of a clean capture; none expected
        n += 1
        by_venue[venue][0] += 1
        pb = pbs[venue]
        fee_bps = fee_by_venue.get(venue, 5.0)
        book = pb.strictly_before(r["arrival_ts"], venue)
        if book is None:
            bad.append({"order_id": r["order_id"], "why": "no book"}); continue
        q, px, why = _execute(book, r["side"], r["qty"], r["limit"], fee_bps, 0.0)
        same_q = abs(q - r["filled_qty"]) < 1e-9
        same_px = (px is None and r["vwap"] is None) or (px is not None and r["vwap"] is not None and abs(px - r["vwap"]) < 1e-9)
        if same_q and same_px:
            ok += 1
            by_venue[venue][1] += 1
        elif len(bad) < 10:
            bad.append({"order_id": r["order_id"], "recorded": [r["filled_qty"], r["vwap"], r["reason"]], "offline": [q, px, why],
                        "arrival_ts": r["arrival_ts"], "processed_ts": r["processed_ts"]})
    return {"attempts": n, "identical": ok, "mismatched": n - ok, "by_venue": {v: {"attempts": a, "identical": b} for v, (a, b) in by_venue.items()},
            "examples": bad}


@dataclass(frozen=True)
class Cell:
    name: str
    rule: str                      # "phase0" | "policy"
    cap: str = "touch_slack"       # policy only: touch_slack | conditional_edge_breakeven
    cap_buffer_bps: float = 0.0
    phi: float = 0.0
    deadline_s: float = 2.0
    price_model: str = "limit"
    attempts: object = "from_latency"


def chains_from(attempts, trades, *, venue="binance"):
    """One chain per hedge intent: its first attempt's decision context plus the maker fill price (joined from trades.jsonl by signal id)."""
    by = defaultdict(list)
    for r in attempts:
        if r["venue"] == venue and r["intent_id"]:
            by[r["intent_id"]].append(r)
    maker = defaultdict(list)
    for t in trades:
        if t.get("kind") == "maker" and not t.get("taker"):
            maker[t["signal_id"]].append(t)
    out = []
    for iid, rows in by.items():
        rows.sort(key=lambda r: (r["attempt"], r["order_id"]))
        first = rows[0]
        cand = [t for t in maker.get(first["signal_id"], []) if t["ts"] <= first["decision_ts"] + 1e-9]
        fill = max(cand, key=lambda t: t["ts"]) if cand else None
        out.append({"intent_id": iid, "signal_id": first["signal_id"], "side": first["side"], "qty": first["qty"], "decision_ts": first["decision_ts"],
                    "dec_touch": first["dec_bid"] if first["side"] == "sell" else first["dec_ask"], "vol": first["vol_bps_1s"] or 0.0,
                    "hour": first["hour_utc"], "maker_fill_price": fill["price"] if fill else None, "recorded": rows})
    out.sort(key=lambda c: c["decision_ts"])
    return out


class Sampler:
    """Lognormal around a mean with the given std, as timing._Sampler, seeded per (day, chain, attempt, stream) so cells are paired."""

    def __init__(self, mean_ms: float, std_ms: float):
        var = math.log(1.0 + (std_ms * std_ms) / (mean_ms * mean_ms))
        self.mu, self.sigma = math.log(mean_ms) - var / 2.0, math.sqrt(var)

    def draw(self, key: str) -> float:
        return random.Random(key).lognormvariate(self.mu, self.sigma) / 1e3


def simulate(chain, pb: Path5, cell: Cell, *, runtime: IocRuntime | None, day: str, maker_bps: float, taker_bps: float,
             entry=Sampler(40.0, 8.0), report=Sampler(36.0, 8.0), backoff_s: float = 0.25, max_walk_bps: float = 2.0,
             slack_floor_bps: float = 5.0, max_retries: int = 2) -> dict:
    side, qty0 = chain["side"], chain["qty"]
    t = chain["decision_ts"]
    cycle = 0.040 + 0.036 + backoff_s
    if cell.rule == "phase0":
        total = max_retries + 1
    else:
        total = runtime.attempts(cycle)
    deadline = chain["decision_ts"] + (cell.deadline_s if cell.rule == "policy" else 1e18)
    remaining = qty0
    fills: list[tuple[float, float]] = []
    attempts_used = first_full = 0
    exhausted = unreachable = False
    final_ts = None
    worst_fixed = None
    if cell.rule == "policy" and cell.cap == "conditional_edge_breakeven":
        if chain["maker_fill_price"] is None:
            return {"skip": "no maker fill price"}
        worst_fixed = runtime.break_even_cap(side, chain["maker_fill_price"], maker_bps, taker_bps)
    for a in range(total):
        view = pb.at_or_before(t)
        if view is None or not view.ready:
            exhausted = True
            break
        touch = view.best_bid if side == "sell" else view.best_ask
        slack = touch * max(max_walk_bps, slack_floor_bps) / 10_000.0
        cap0 = touch - slack if side == "sell" else touch + slack          # the phase-0 cap, re-taken on every attempt
        if cell.rule == "phase0":
            limit = cap0
        else:
            worst = cap0 if cell.cap == "touch_slack" else worst_fixed
            win = pb.touch_window(t)
            fund = median([w[0] for w in win]) if side == "sell" else median([w[1] for w in win])
            plan = runtime.plan(side=side, fundamental=fund, worst_price=worst, attempts_left=total - a, hour=chain["hour"], vol_bps=chain["vol"],
                                fee_bps=taker_bps, q=remaining, cycle_s=cycle)
            if plan is None:
                unreachable = True
                # the engine sends nothing, learns after local_reject (0 here) and backs off; the next attempt re-plans
                attempts_used += 1
                t = t + backoff_s
                if t > deadline:
                    exhausted = True
                    break
                continue
            limit = plan.limit
        attempts_used += 1
        arr = t + entry.draw(f"{day}|{chain['intent_id']}|{a}|entry")
        book = pb.strictly_before(arr)
        if book is None:
            exhausted = True
            break
        q, px, why = _execute(book, side, remaining, limit, taker_bps, 0.0)
        if q > 1e-12:
            fills.append((q, px))
            remaining -= q
        if remaining <= 1e-9:
            if a == 0:
                first_full = 1
            final_ts = arr
            break
        learn = arr + report.draw(f"{day}|{chain['intent_id']}|{a}|report")
        t = learn + backoff_s
        if t > deadline:
            exhausted = True
            break
    else:
        exhausted = remaining > 1e-9
    if remaining > 1e-9 and not exhausted:
        exhausted = True
    filled = qty0 - max(remaining, 0.0)
    vwap = (sum(q * p for q, p in fills) / filled) if filled > 1e-12 else None
    return {"filled": filled, "vwap": vwap, "attempts": attempts_used, "first_full": first_full, "exhausted": exhausted,
            "unreachable_seen": unreachable, "legging_s": (final_ts - chain["decision_ts"]) if final_ts else None, "remaining": max(remaining, 0.0)}


def metrics(chains, results, *, loss_fail_bps: float = 7.8) -> dict:
    """Per-cell metrics over chains. Slippage is the favourable bps of the VWAP against the decision touch."""
    def q(xs, p):
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None
    n = len(results)
    sl, leg = [], []
    pnl = 0.0
    for c, r in zip(chains, results):
        if r.get("skip"):
            continue
        if r["vwap"] is not None:
            s = (r["vwap"] - c["dec_touch"]) / c["dec_touch"] * 1e4 * (1 if c["side"] == "sell" else -1)
            sl.append(s)
        if r["legging_s"] is not None:
            leg.append(r["legging_s"])
        if r["exhausted"]:
            pnl -= loss_fail_bps / 1e4 * c["dec_touch"] * r["remaining"]
    k = sum(1 for r in results if not r.get("skip"))
    return {"hedges": k, "first_attempt_fill_rate": sum(r.get("first_full", 0) for r in results) / k if k else None,
            "attempts_per_hedge": sum(r.get("attempts", 0) for r in results if not r.get("skip")) / k if k else None,
            "misses": sum(max(r.get("attempts", 0) - 1, 0) + (1 if r.get("exhausted") else 0) for r in results if not r.get("skip")),
            "exhausted": sum(1 for r in results if r.get("exhausted")), "cap_unreachable_chains": sum(1 for r in results if r.get("unreachable_seen")),
            "slippage_bps": {"mean": sum(sl) / len(sl) if sl else None, "p10": q(sl, 0.1), "p90": q(sl, 0.9), "n": len(sl)},
            "legging_s": {"mean": sum(leg) / len(leg) if leg else None, "p90": q(leg, 0.9), "max": max(leg) if leg else None, "n": len(leg)},
            "unwind_penalty_usdt": pnl}
