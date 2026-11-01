#!/usr/bin/env python3
"""PostOnly acceptance rule checked against REAL accepted orders (bybit_30d.zip) and the recorded exchange-time book.

Every PostOnly order in the 2026-04-19..25 export FILLED, so the exchange accepted it: none should be flagged as "would take
liquidity". For each such order the rule of new_backtest.postonly.would_cross is evaluated against the book just before the first
recorded event whose exchange time is >= the order's createdTime (the backtester's rule), and a flag is a false reject.

It also (a) checks that the recorded book is the SPOT book, from the real maker fills: a maker buy at p executes against a resting
bid level at p, so the level must exist in the book just before the fill; (b) measures the collector's feed clock drift
(local_ts - exchange_ts) of the depth stream, which decides how stale a receive-time book can be; (c) repeats the order check with
the probe time shifted, to show how sensitive the verdict is to the unknown offset between the matching-engine clock and the
public-feed clock.

Reads the zip and data/ only; writes one JSON of aggregates (no ids, no prices). Usage:
    python3 -I scripts/postonly_false_reject_check.py --zip ~/Downloads/bybit_30d.zip --data-root ../data \
        --days 2026-04-19:2026-04-25 --out config/postonly_false_reject_check.json [--symbols SOLUSDT,...] [--workers 8]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import math
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from new_backtest.config import make_venues                                  # noqa: E402
from new_backtest.event_loader import BOOK, DELTA, delta_fields, iter_venue_events, snapshot_levels  # noqa: E402
from new_backtest.order_book import OrderBook                                # noqa: E402
from new_backtest.postonly import would_cross                                # noqa: E402

SHIFTS_S = (-0.5, -0.2, -0.05, 0.0, 0.05, 0.2, 0.5)
FRESH_S = 2.0            # a probe is "fresh" when the last recorded depth event is at most this old (the book updates ~17 times a second)
SHORT_LIFE_S = 5.0       # orders that lived at most this long are unlikely to carry an amended price


def utc_day(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc).strftime("%Y-%m-%d")


def wilson(k: int, n: int, z: float = 1.96):
    if n <= 0:
        return [None, None]
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 6), round(min(1.0, c + h), 6)]


def load_orders(zip_path: str, symbols, days):
    z = zipfile.ZipFile(zip_path)
    orders, fills = defaultdict(list), defaultdict(list)
    for name in z.namelist():
        base = name.rsplit("/", 1)[-1]
        if base.startswith("order_spot_"):
            sym = base[len("order_spot_"):-len(".jsonl")]
            if symbols and sym not in symbols:
                continue
            for line in z.open(name):
                r = json.loads(line)
                d = utc_day(int(r["createdTime"]))
                if r["timeInForce"] == "PostOnly" and days[0] <= d <= days[1]:
                    orders[(sym, d)].append((int(r["createdTime"]) / 1000.0, "buy" if r["side"] == "Buy" else "sell", float(r["price"]), r["orderStatus"],
                                             (int(r["updatedTime"]) - int(r["createdTime"])) / 1000.0))
        elif base.startswith("exec_spot_"):
            sym = base[len("exec_spot_"):-len(".jsonl")]
            if symbols and sym not in symbols:
                continue
            for line in z.open(name):
                r = json.loads(line)
                d = utc_day(int(r["execTime"]))
                if r.get("isMaker") and r["execType"] == "Trade" and days[0] <= d <= days[1]:
                    fills[(sym, d)].append((int(r["execTime"]) / 1000.0, "buy" if r["side"] == "Buy" else "sell", float(r["execPrice"])))
    return orders, fills


def one_task(args):
    sym, day, orders, fills, data_root = args
    spec = make_venues("singapore", "bybit_spot_mm" if False else "bybit_perp_mm", "binance_perp_vip0")["bybit"]
    book = OrderBook("bybit", sym, spec.delta_semantics)
    probes = sorted((t + s, i, j) for i, (t, *_rest) in enumerate(orders) for j, s in enumerate(SHIFTS_S))
    fprobes = sorted((t, i) for i, (t, *_r) in enumerate(fills))
    pi = fi = 0
    flagged = defaultdict(int)      # shift index -> false rejects
    checked = defaultdict(int)
    cat = defaultdict(lambda: defaultdict(int))   # category -> counters, at shift 0
    lives = {"flagged": [], "not_flagged": []}    # order lifetimes on a fresh book, at shift 0
    last_delta = None
    snap_agree = snap_total = 0
    unready = 0
    fill_level_exists = fill_checked = fill_at_or_behind_touch = 0
    drift = []
    n_events = 0
    for ev in iter_venue_events(data_root, "bybit", sym, day, day):
        n_events += 1
        ets = ev.exchange_ts
        if ev.kind == DELTA:
            if ev.local_ts:
                drift.append(ev.local_ts - ets)
        while pi < len(probes) and probes[pi][0] <= ets:
            t, i, j = probes[pi]
            pi += 1
            if not book.ready:
                unready += 1
                continue
            _t, side, price, _st, life = orders[i]
            checked[j] += 1
            crossed = would_cross(side, price, book.best_bid, book.best_ask)
            if crossed:
                flagged[j] += 1
            if SHIFTS_S[j] == 0.0:
                fresh = last_delta is not None and (t - last_delta) <= FRESH_S
                if fresh:
                    lives["flagged" if crossed else "not_flagged"].append((side, life))
                for name, member in (("all", True), ("fresh_book", fresh), ("fresh_book_short_lived", fresh and life <= SHORT_LIFE_S),
                                     ("stale_book", not fresh)):
                    if member:
                        cat[name]["checked"] += 1
                        cat[name]["false_rejects"] += int(crossed)
                        cat[name]["false_rejects_" + side] += int(crossed)
                        cat[name]["checked_" + side] += 1
        while fi < len(fprobes) and fprobes[fi][0] <= ets:
            _t, i = fprobes[fi]
            fi += 1
            if not book.ready:
                continue
            _t, side, price = fills[i]
            fill_checked += 1
            if book.level_size("bid" if side == "buy" else "ask", price) > 0.0:
                fill_level_exists += 1
            if (side == "buy" and price <= book.best_bid + 1e-12) or (side == "sell" and price >= book.best_ask - 1e-12):
                fill_at_or_behind_touch += 1
        if ev.kind == BOOK:
            bids, asks = snapshot_levels(ev.payload)
            if bids and asks:
                if book.ready and "best_bid" in ev.payload and "best_ask" in ev.payload:
                    snap_total += 1
                    snap_agree += int(abs(book.best_bid - ev.payload["best_bid"]) < 1e-9 and abs(book.best_ask - ev.payload["best_ask"]) < 1e-9)
                book.apply_snapshot(bids, asks, ets)
        elif ev.kind == DELTA:
            side, price, volume, etype = delta_fields(ev.payload)
            if price > 0:
                book.apply_delta(side, price, volume, etype, ets)
                last_delta = ets
    drift.sort()
    n = len(drift)
    dstats = None
    if n:
        med = drift[n // 2]
        dstats = {"n": n, "p01": drift[int(0.01 * (n - 1))], "p50": med, "p90": drift[int(0.9 * (n - 1))], "p99": drift[int(0.99 * (n - 1))],
                  "share_outside_0.3s_of_median": sum(1 for d in drift if abs(d - med) > 0.3) / n}
    return {"symbol": sym, "day": day, "orders": len(orders), "events": n_events, "unready": unready,
            "checked": dict(checked), "flagged": dict(flagged), "fills": len(fills), "fill_checked": fill_checked,
            "fill_level_exists": fill_level_exists, "fill_at_or_behind_touch": fill_at_or_behind_touch, "drift": dstats,
            "categories": {k: dict(v) for k, v in cat.items()}, "snapshot_touch_agree": snap_agree, "snapshot_touch_total": snap_total,
            "lifetimes": lives}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", required=True)
    ap.add_argument("--data-root", required=True)
    ap.add_argument("--days", required=True, help="first:last, inclusive")
    ap.add_argument("--out", required=True)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    days = a.days.split(":")
    symbols = set(a.symbols.split(",")) if a.symbols else set()
    orders, fills = load_orders(a.zip, symbols, days)
    tasks = [(s, d, orders[(s, d)], fills.get((s, d), []), a.data_root) for (s, d) in sorted(orders)]
    print(f"{len(tasks)} pair-days, {sum(len(t[2]) for t in tasks)} orders, {sum(len(t[3]) for t in tasks)} maker fills", flush=True)
    results = []
    with cf.ProcessPoolExecutor(max_workers=a.workers) as ex:
        for r in ex.map(one_task, tasks):
            results.append(r)
            print(f"  {r['symbol']} {r['day']} orders={r['orders']} events={r['events']} flagged0={r['flagged'].get(SHIFTS_S.index(0.0), 0)}", flush=True)
    z0 = SHIFTS_S.index(0.0)

    def agg(sel):
        out = {"n_orders": sum(r["orders"] for r in sel)}
        out["by_shift_s"] = {}
        for j, s in enumerate(SHIFTS_S):
            k = sum(r["flagged"].get(j, 0) for r in sel)
            n = sum(r["checked"].get(j, 0) for r in sel)
            out["by_shift_s"][str(s)] = {"checked": n, "false_rejects": k, "rate": (k / n if n else None), "wilson95": wilson(k, n)}
        fc = sum(r["fill_checked"] for r in sel)
        out["real_maker_fills"] = {"checked": fc, "level_exists_in_book_before_fill": sum(r["fill_level_exists"] for r in sel),
                                   "price_at_or_behind_touch": sum(r["fill_at_or_behind_touch"] for r in sel)}
        out["orders_before_book_ready"] = sum(r["unready"] for r in sel)
        out["categories_at_shift_0"] = {}
        for name in ("all", "fresh_book", "fresh_book_short_lived", "stale_book"):
            k = sum(r["categories"].get(name, {}).get("false_rejects", 0) for r in sel)
            n = sum(r["categories"].get(name, {}).get("checked", 0) for r in sel)
            out["categories_at_shift_0"][name] = {
                "checked": n, "false_rejects": k, "rate": (k / n if n else None), "wilson95": wilson(k, n),
                "false_rejects_buy": sum(r["categories"].get(name, {}).get("false_rejects_buy", 0) for r in sel),
                "false_rejects_sell": sum(r["categories"].get(name, {}).get("false_rejects_sell", 0) for r in sel),
                "checked_buy": sum(r["categories"].get(name, {}).get("checked_buy", 0) for r in sel),
                "checked_sell": sum(r["categories"].get(name, {}).get("checked_sell", 0) for r in sel)}
        def lstats(rows):
            xs = sorted(x for _s, x in rows)
            n = len(xs)
            return {"n": n, "p50": (xs[n // 2] if n else None), "p90": (xs[int(0.9 * (n - 1))] if n else None)}
        out["fresh_book_lifetime_s"] = {
            "flagged": lstats([x for r in sel for x in r["lifetimes"]["flagged"]]),
            "not_flagged": lstats([x for r in sel for x in r["lifetimes"]["not_flagged"]]),
            "flagged_sells": lstats([x for r in sel for x in r["lifetimes"]["flagged"] if x[0] == "sell"]),
            "not_flagged_sells": lstats([x for r in sel for x in r["lifetimes"]["not_flagged"] if x[0] == "sell"])}
        out["replay_fidelity_snapshot_touch"] = {"agree": sum(r["snapshot_touch_agree"] for r in sel), "total": sum(r["snapshot_touch_total"] for r in sel)}
        return out
    doc = {
        "schema": "postonly_false_reject_check/v1",
        "what": "false-reject check of the PostOnly rule on real ACCEPTED orders; not a validation of the real reject rate",
        "orders_source": {"zip_name": Path(a.zip).name, "zip_sha256": hashlib.sha256(open(a.zip, "rb").read()).hexdigest()},
        "definitions": {"fresh_book": f"last recorded depth event at most {FRESH_S} s before the probe", "fresh_book_short_lived":
                        f"fresh_book and order lifetime (updatedTime - createdTime) <= {SHORT_LIFE_S} s: unlikely to carry an amended price",
                        "stale_book": "last depth event older than the fresh limit: the collector was not recording, the rebuilt book is not the market"},
        "limits": ["the export keeps one final record per order: if the bot amended an order, `price` is the last price, not the price at creation",
                   "the recorded book has collector gaps; probes inside them say nothing about the rule",
                   "every order here was accepted by the exchange (it filled): this measures false rejects only, never missed rejects"],
        "days": days, "market_data": "data/bybit book_snapshots + book_events (exchange_ts), read only",
        "shifts_s": list(SHIFTS_S), "rule": "buy: price >= best ask; sell: price <= best bid, book just before the first event with exchange_ts >= probe time",
        "overall": agg(results),
        "by_symbol": {s: agg([r for r in results if r["symbol"] == s]) for s in sorted({r["symbol"] for r in results})},
        "by_day": {d: agg([r for r in results if r["day"] == d]) for d in sorted({r["day"] for r in results})},
        "feed_drift_local_minus_exchange_s": {f"{r['symbol']}|{r['day']}": r["drift"] for r in results if r["drift"]},
    }
    Path(a.out).write_text(json.dumps(doc, indent=1) + "\n")
    o = doc["overall"]
    print("overall false rejects at shift 0:", o["by_shift_s"]["0.0"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
