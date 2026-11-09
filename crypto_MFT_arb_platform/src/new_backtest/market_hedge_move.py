"""Expected hedge move estimated from MARKET DATA only (v1.1 step 9a).

The conditional edge of the v1 / v1.1 profiles needs E[hedge price | our passive fill] - quote, in bps (ArbConfig.expected_hedge_move_bps_{bid,ask}).
v1 estimated it from the executed hedges of legacy_v0 REPLAYS. Those replays contain simulated fills that no trade supports, and the price moves after
those fills were unusually good, so the number was optimistic and the quotes built on it too aggressive. This module needs no simulated fill:

    for every public print on the MAKER venue that would hit our passive quote (a sell aggressor for the bid side, a buy aggressor for the ask side),
    take the HEDGE venue's mid at the print's exchange time t and at t + horizon_s (default 1 s), both from the venue's own book rebuilt with the
    engine's OrderBook. One event per (exchange time, aggressor side): the several prints of one sweep count once.

    move_bid = (mid(t+h) - mid(t)) / mid(t) * 1e4          a passive bid filled -> the hedge SELLS; a fall of the hedge mid is adverse, so negative = adverse
    move_ask = -(mid(t+h) - mid(t)) / mid(t) * 1e4         a passive ask filled -> the hedge BUYS; a rise of the hedge mid is adverse, so negative = adverse
    (the sign convention of edge.maker_taker_expected_edge_bps: expected hedge = touch * (1 + move_bid/1e4) for a sell, touch * (1 - move_ask/1e4) for a buy)

Only TRAIN days may be passed (the caller's rule, recorded in the output). A print counts only when the hedge book is ready and fresh (its last update at most
max_book_age_s before t and before t + h), so a truncated or gapped hedge feed contributes no pairs instead of stale ones. Exchange times on both venues.
CLI: python -m new_backtest.market_hedge_move --data-root D --pair SOLUSDT --days 2024-03-01,2024-03-02 --out file.json [--jobs 3]
EXPLORATORY: it is the unconditional drift after a print, not the drift after OUR fills (those are adversely selected); the conditional number needs real fills."""
from __future__ import annotations

import argparse
import heapq
import json
import math
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

from .event_loader import BOOK, DELTA, TRADE, delta_fields, iter_venue_events, reset_truncation, snapshot_levels, trade_fields, truncation_report
from .order_book import OrderBook

HIST_BPS = 0.05           # histogram resolution for the pooled median
HIST_CLIP = 50.0


def day_stats(data_root, pair: str, day: str, maker_venue: str = "bybit", hedge_venue: str = "binance",
              horizon_s: float = 1.0, max_book_age_s: float = 2.0) -> dict:
    """Pair counts and move sums for ONE day (a day's pending tail at midnight is dropped, counted as unresolved)."""
    reset_truncation()
    book = OrderBook(hedge_venue, pair, tape_repair=False)
    hedge = iter_venue_events(data_root, hedge_venue, pair, day, day, include_snapshots=True, include_deltas=True, include_trades=False)
    maker = iter_venue_events(data_root, maker_venue, pair, day, day, include_snapshots=False, include_deltas=False, include_trades=True, trade_dedupe="multiset")
    # hedge events sort before maker trades at equal exchange time: a print at t sees the hedge book including every event up to t
    merged = heapq.merge(((e.exchange_ts, 0, i, e) for i, e in enumerate(hedge)), ((e.exchange_ts, 1, i, e) for i, e in enumerate(maker)), key=lambda x: x[:3])
    acc = {s: {"n": 0, "sum": 0.0, "sumsq": 0.0, "hist": {}} for s in ("bid", "ask")}
    cnt = {"maker_prints": 0, "events": 0, "no_book_at_t": 0, "stale_at_t": 0, "stale_at_t_plus_h": 0, "unresolved_at_end": 0, "hedge_events": 0}
    pending: deque = deque()                 # (t, side, mid0)
    last_key = None

    def resolve_until(limit_ts: float) -> None:
        while pending and pending[0][0] + horizon_s < limit_ts:
            t, side, mid0 = pending.popleft()
            t1 = t + horizon_s
            if not book.ready or book.mid <= 0 or t1 - book.last_update_ts > max_book_age_s:
                cnt["stale_at_t_plus_h"] += 1
                continue
            bps = (book.mid - mid0) / mid0 * 1e4
            if side == "ask":
                bps = -bps
            a = acc[side]
            a["n"] += 1; a["sum"] += bps; a["sumsq"] += bps * bps
            k = int(round(max(-HIST_CLIP, min(HIST_CLIP, bps)) / HIST_BPS))
            a["hist"][k] = a["hist"].get(k, 0) + 1

    for ts, kind, _, ev in merged:
        if kind == 0:
            resolve_until(ts)
            cnt["hedge_events"] += 1
            if ev.kind == BOOK:
                bids, asks = snapshot_levels(ev.payload)
                if bids and asks:
                    book.apply_snapshot(bids, asks, ts)
            elif ev.kind == DELTA:
                side, price, volume, event_type = delta_fields(ev.payload)
                if price > 0:
                    book.apply_delta(side, price, volume, event_type, ts)
            continue
        if ev.kind != TRADE:
            continue
        cnt["maker_prints"] += 1
        _, _, sign = trade_fields(ev.payload)
        key = (ts, sign)
        if key == last_key:
            continue                          # the same sweep: one event
        last_key = key
        cnt["events"] += 1
        if not book.ready or book.mid <= 0:
            cnt["no_book_at_t"] += 1
            continue
        if ts - book.last_update_ts > max_book_age_s:
            cnt["stale_at_t"] += 1
            continue
        pending.append((ts, "bid" if sign < 0 else "ask", book.mid))
    cnt["unresolved_at_end"] = len(pending)
    return {"day": day, "counts": cnt, "acc": {s: {**a, "hist": {str(k): v for k, v in a["hist"].items()}} for s, a in acc.items()},
            "truncation": truncation_report()}


def day_labels(data_root, pair: str, day: str, maker_venue: str = "bybit", hedge_venue: str = "binance", horizons: tuple = (1.0, 5.0, 30.0), max_book_age_s: float = 2.0,
               tick: float = 0.001, vol_window_s: float = 30.0, flow_window_s: float = 1.0) -> dict:
    """S1 (owner 9 Nov): one LABEL ROW per public print event on the maker venue that would hit our passive quote, from MARKET DATA only (no strategy fill, simulated or not).
    Event, side, hedge-venue mid and the adverse sign are exactly `day_stats`'s. Each row carries the features known at the print and the hedge-mid move after each horizon (negative = adverse):
      depth_ticks   how far the DEEPEST price of the sweep is behind the maker touch just before it (0 = the touch itself): a quote that deep or shallower is filled by this print
      spread_bps    maker-venue touch spread in bps of its mid, at the print
      vol_bps       standard deviation of the hedge mid's 1 s changes over the last `vol_window_s`, in bps (None until 5 changes are known)
      flow          signed maker-venue volume of the last `flow_window_s` BEFORE the print, positive when it ran in this print's direction (the aggressor's), in base units
      move_<h>s     hedge-mid move after h seconds (None when the hedge book was stale or missing at t or t + h, or the day ended first)
    Only TRAIN days may be fitted from these rows (the caller's rule)."""
    reset_truncation()
    hedge_book = OrderBook(hedge_venue, pair, tape_repair=False)
    maker_book = OrderBook(maker_venue, pair, tape_repair=False)
    hedge_ev = iter_venue_events(data_root, hedge_venue, pair, day, day, include_snapshots=True, include_deltas=True, include_trades=False)
    maker_ev = iter_venue_events(data_root, maker_venue, pair, day, day, include_snapshots=True, include_deltas=True, include_trades=True, trade_dedupe="multiset")
    merged = heapq.merge(((e.exchange_ts, 0, i, e) for i, e in enumerate(hedge_ev)), ((e.exchange_ts, 1, i, e) for i, e in enumerate(maker_ev)), key=lambda x: x[:3])
    rows: list[dict] = []
    pending = {h: deque() for h in horizons}          # per horizon: (row index, t, side, mid0)
    cnt = {"prints": 0, "events": 0, "no_book": 0, "stale": 0, "unresolved_at_end": 0}
    mids: deque = deque()                              # (second, hedge mid): one per second, for the volatility
    flows: deque = deque()                             # (ts, signed volume) of recent maker prints
    last_sec = None
    group: dict | None = None                          # the sweep being assembled: same exchange time and aggressor side

    def resolve_until(limit_ts: float) -> None:
        for h, q in pending.items():
            while q and q[0][1] + h < limit_ts:
                idx, t, side, mid0 = q.popleft()
                if not hedge_book.ready or hedge_book.mid <= 0 or (t + h) - hedge_book.last_update_ts > max_book_age_s:
                    continue
                bps = (hedge_book.mid - mid0) / mid0 * 1e4
                rows[idx][f"move_{h:g}s"] = -bps if side == "ask" else bps

    def close_group() -> None:
        nonlocal group
        if group is None:
            return
        g, group = group, None
        cnt["events"] += 1
        if not (hedge_book.ready and hedge_book.mid > 0 and maker_book.ready and maker_book.mid > 0):
            cnt["no_book"] += 1
            return
        if g["ts"] - hedge_book.last_update_ts > max_book_age_s or g["ts"] - maker_book.last_update_ts > max_book_age_s:
            cnt["stale"] += 1
            return
        side = "bid" if g["sign"] < 0 else "ask"
        touch = maker_book.best_bid if side == "bid" else maker_book.best_ask
        deepest = g["min_px"] if side == "bid" else g["max_px"]
        depth = max(0, int(round(((touch - deepest) if side == "bid" else (deepest - touch)) / tick)))
        ch = [b - a for (_, a), (_, b) in zip(mids, list(mids)[1:])]
        vol = None
        if len(ch) >= 5:
            m = sum(ch) / len(ch)
            vol = math.sqrt(sum((x - m) ** 2 for x in ch) / (len(ch) - 1)) / hedge_book.mid * 1e4
        flow = sum(v for t, v in flows if g["ts"] - flow_window_s <= t < g["ts"]) * (1 if g["sign"] > 0 else -1)
        row = {"ts": g["ts"], "side": side, "depth_ticks": depth, "spread_bps": (maker_book.best_ask - maker_book.best_bid) / maker_book.mid * 1e4, "vol_bps": vol, "flow": flow,
               **{f"move_{h:g}s": None for h in horizons}}
        rows.append(row)
        for h in horizons:
            pending[h].append((len(rows) - 1, g["ts"], side, hedge_book.mid))

    for ts, kind, _, ev in merged:
        if kind == 0:
            close_group()
            resolve_until(ts)
            if ev.kind == BOOK:
                bids, asks = snapshot_levels(ev.payload)
                if bids and asks:
                    hedge_book.apply_snapshot(bids, asks, ts)
            elif ev.kind == DELTA:
                side, price, volume, event_type = delta_fields(ev.payload)
                if price > 0:
                    hedge_book.apply_delta(side, price, volume, event_type, ts)
            sec = int(ts)
            if hedge_book.ready and hedge_book.mid > 0 and sec != last_sec:
                last_sec = sec
                mids.append((sec, hedge_book.mid))
                while mids and mids[0][0] < sec - vol_window_s:
                    mids.popleft()
            continue
        if ev.kind == BOOK:
            close_group()
            bids, asks = snapshot_levels(ev.payload)
            if bids and asks:
                maker_book.apply_snapshot(bids, asks, ts)
            continue
        if ev.kind == DELTA:
            close_group()
            side, price, volume, event_type = delta_fields(ev.payload)
            if price > 0:
                maker_book.apply_delta(side, price, volume, event_type, ts)
            continue
        if ev.kind != TRADE:
            continue
        price, vol, sign = trade_fields(ev.payload)
        if price <= 0 or vol <= 0:
            continue
        cnt["prints"] += 1
        if group is not None and (group["ts"], group["sign"]) != (ts, sign):
            close_group()
        if group is None:
            group = {"ts": ts, "sign": sign, "min_px": price, "max_px": price}
        group["min_px"], group["max_px"] = min(group["min_px"], price), max(group["max_px"], price)
        flows.append((ts, vol * (1 if sign > 0 else -1)))
        while flows and flows[0][0] < ts - 5 * flow_window_s:
            flows.popleft()
    close_group()
    cnt["unresolved_at_end"] = sum(len(q) for q in pending.values())
    return {"day": day, "counts": cnt, "rows": rows, "truncation": truncation_report(), "horizons": list(horizons), "tick": tick, "max_book_age_s": max_book_age_s}


def combine(per_day: list[dict], horizon_s: float, maker_venue: str, hedge_venue: str, pair: str, data_root: str) -> dict:
    out = {"label": "expected hedge move from MARKET DATA only (v1.1 step 9a); EXPLORATORY", "method": "per public print on the maker venue that hits our side (one event per exchange time and aggressor side): hedge-venue mid at t and t+horizon, bps; bid = change, ask = -change",
           "pair": pair, "maker_venue": maker_venue, "hedge_venue": hedge_venue, "horizon_s": horizon_s, "data_root": str(data_root),
           "days": [d["day"] for d in per_day], "per_day": {d["day"]: {"counts": d["counts"], "truncation": d["truncation"],
                                                                          **{s: {"n": d["acc"][s]["n"], "mean_bps": (d["acc"][s]["sum"] / d["acc"][s]["n"]) if d["acc"][s]["n"] else None}
                                                                             for s in ("bid", "ask")}} for d in per_day},
           "expected_hedge_move_bps": {}, "stats": {}}
    for s in ("bid", "ask"):
        n = sum(d["acc"][s]["n"] for d in per_day)
        sm = sum(d["acc"][s]["sum"] for d in per_day)
        sq = sum(d["acc"][s]["sumsq"] for d in per_day)
        hist: dict[int, int] = {}
        for d in per_day:
            for k, v in d["acc"][s]["hist"].items():
                hist[int(k)] = hist.get(int(k), 0) + v
        mean = sm / n if n else 0.0
        sd = math.sqrt(max(sq / n - mean * mean, 0.0)) if n else 0.0
        med = None
        if n:
            run = 0
            for k in sorted(hist):
                run += hist[k]
                if run >= n / 2:
                    med = k * HIST_BPS
                    break
        out["expected_hedge_move_bps"][s] = mean
        out["stats"][s] = {"n": n, "mean_bps": mean, "median_bps": med, "stdev_bps": sd, "se_bps": sd / math.sqrt(n) if n else None}
    return out


def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", required=True); ap.add_argument("--pair", required=True); ap.add_argument("--days", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--maker-venue", default="bybit"); ap.add_argument("--hedge-venue", default="binance")
    ap.add_argument("--horizon-s", type=float, default=1.0); ap.add_argument("--max-book-age-s", type=float, default=2.0)
    ap.add_argument("--jobs", type=int, default=1); ap.add_argument("--day-file", default=None, help="internal: compute one day and write its raw stats here")
    ap.add_argument("--labels-out", default=None, help="S1: write the per-print LABEL ROWS of the first day in --days to this JSON file and stop")
    ap.add_argument("--label-horizons", default="1,5,30", help="S1: the horizons in seconds of the label rows (e.g. 0.05,0.15,1,5,30)")
    a = ap.parse_args()
    if a.labels_out:
        Path(a.labels_out).write_text(json.dumps(day_labels(a.data_root, a.pair, a.days.split(",")[0], a.maker_venue, a.hedge_venue, horizons=tuple(float(x) for x in a.label_horizons.split(",")), max_book_age_s=a.max_book_age_s)))
        return
    days = [d for d in a.days.split(",") if d]
    if a.day_file:
        Path(a.day_file).write_text(json.dumps(day_stats(a.data_root, a.pair, days[0], a.maker_venue, a.hedge_venue, a.horizon_s, a.max_book_age_s)))
        return
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    work = out.with_suffix(".days"); work.mkdir(exist_ok=True)
    running, pending = {}, [d for d in days if not (work / f"{d}.json").exists()]
    while pending or running:
        for d, p in list(running.items()):
            if p.poll() is not None:
                if p.returncode:
                    raise SystemExit(f"day {d} failed rc={p.returncode}")
                del running[d]
        while pending and len(running) < max(1, a.jobs):
            d = pending.pop(0)
            args = [sys.executable, "-m", "new_backtest.market_hedge_move", "--data-root", a.data_root, "--pair", a.pair, "--days", d, "--out", str(out),
                    "--maker-venue", a.maker_venue, "--hedge-venue", a.hedge_venue, "--horizon-s", str(a.horizon_s), "--max-book-age-s", str(a.max_book_age_s),
                    "--day-file", str(work / f"{d}.json.part")]
            running[d] = subprocess.Popen(args, env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        time.sleep(2)
        for d in days:
            part = work / f"{d}.json.part"
            if part.exists() and d not in running:
                part.rename(work / f"{d}.json")
    per_day = [json.loads((work / f"{d}.json").read_text()) for d in days]
    out.write_text(json.dumps(combine(per_day, a.horizon_s, a.maker_venue, a.hedge_venue, a.pair, a.data_root), indent=1))
    print(json.dumps(json.loads(out.read_text())["stats"]))


if __name__ == "__main__":
    _main()
