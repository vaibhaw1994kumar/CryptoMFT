"""Spot simulator validation, step 5 (owner brief 10 Nov 2026): perp taker check, "8 orders: gross-error check only, not a calibration".

Our real Bybit linear (USDT perp) Market IOC orders of one day are walked through the core taker book walk (crypto_stat_arb TakerExecutor, no limit, no own
impact) against the book rebuilt from the converted public archive (bybit_perp_archive.py output), as the book stood at the order's arrival: every book
event with exchange time at or before arrival is applied (event_order = delta_first, the order under which the day's rebuild check passes 100%). Arrival =
the real create time + the latency preset's Bybit entry latency (singapore: 6 ms); a 0 ms sensitivity is reported beside it. Compared per order with the real
execution: VWAP difference in bps (signed: + = the simulated price is worse for us) and filled quantity. Rows carry an index, never an order id.

usage: python3.12 -m new_backtest.validation.perp_taker_check --orders DIR --data-root ROOT --day 2026-04-29 --out FILE.json  (ROOT/bybit/... = converted archive)
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

PAIRS = ("ADAUSDT", "MNTUSDT")
ENTRY_S = {"singapore_6ms": 0.006, "zero_latency": 0.0}


def real_orders(orders_dir: Path, pair: str, day: str) -> list[dict]:
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    execs: dict[str, list] = {}
    for line in open(orders_dir / f"exec_linear_{pair}.jsonl"):
        e = json.loads(line)
        if e.get("execType") == "Trade":
            execs.setdefault(e["orderId"], []).append((float(e["execPrice"]), float(e["execQty"])))
    out = []
    for line in open(orders_dir / f"order_linear_{pair}.jsonl"):
        o = json.loads(line)
        t = int(o["createdTime"]) / 1000.0
        if o.get("timeInForce") != "IOC" or not start <= t < start + 86_400:
            continue
        ex = execs.get(o["orderId"], [])
        q = sum(x[1] for x in ex)
        out.append({"created": t, "side": o["side"].lower(), "qty": float(o["qty"]), "type": o["orderType"], "status": o["orderStatus"],
                    "real_qty": q, "real_vwap": (sum(p * x for p, x in ex) / q) if q else None, "real_execs": len(ex)})
    return sorted(out, key=lambda r: r["created"])


def books_at(data_root: Path, pair: str, day: str, times: list[float]):
    """Yield (time, OrderBook) for each time in ascending order: the book after every book event stamped at or before it."""
    from crypto_stat_arb.simulation.order_book import OrderBook
    from new_backtest.event_loader import BOOK, DELTA, delta_fields, iter_venue_events, snapshot_levels
    book = OrderBook("bybit", pair, delta_semantics="absolute", tape_repair=False)
    pending = sorted(times)
    for ev in iter_venue_events(str(data_root), "bybit", pair, day, day, include_trades=False, event_order="delta_first"):
        while pending and ev.exchange_ts > pending[0]:
            yield pending.pop(0), book
        if ev.kind == BOOK:
            bids, asks = snapshot_levels(ev.payload)
            book.apply_snapshot(bids, asks, ev.exchange_ts)
        elif ev.kind == DELTA:
            side, price, volume, event_type = delta_fields(ev.payload)
            book.apply_delta(side, price, volume, event_type, ev.exchange_ts)
    while pending:
        yield pending.pop(0), book


def check(orders_dir: Path, data_root: Path, day: str) -> dict:
    from crypto_stat_arb.simulation.fill_engine import TakerExecutor
    taker = TakerExecutor(consume_liquidity=False)
    rows = []
    for pair in PAIRS:
        orders = real_orders(orders_dir, pair, day)
        for name, lat in ENTRY_S.items():
            arrivals = [o["created"] + lat for o in orders]
            for (t, book), o in zip(books_at(data_root, pair, day, arrivals), orders):
                lim = float("inf") if o["side"] == "buy" else 0.0
                res = taker.execute(book, o["side"], o["qty"], lim, 0.0, t)
                vwap = res.fill.price if res.fill else None
                sign = 1.0 if o["side"] == "buy" else -1.0
                rows.append({"pair": pair, "latency": name, "side": o["side"], "qty": o["qty"], "real_execs": o["real_execs"],
                             "sim_status": res.reason, "sim_levels": res.fill.levels_consumed if res.fill else 0,
                             "qty_ratio_sim_to_real": round((res.fill.qty if res.fill else 0.0) / o["real_qty"], 6) if o["real_qty"] else None,
                             "vwap_diff_bps": round(sign * (vwap - o["real_vwap"]) / o["real_vwap"] * 1e4, 3) if (vwap and o["real_vwap"]) else None})
    for i, r in enumerate(sorted(rows, key=lambda r: (r["latency"], r["pair"]))):
        r["i"] = i
    agg = {}
    for name in ENTRY_S:
        rs = [r for r in rows if r["latency"] == name and r["vwap_diff_bps"] is not None]
        d = [r["vwap_diff_bps"] for r in rs]
        agg[name] = {"orders": len([r for r in rows if r["latency"] == name]), "compared": len(rs),
                     "vwap_diff_bps_mean": round(sum(d) / len(d), 3) if d else None, "vwap_diff_bps_max_abs": max((abs(x) for x in d), default=None),
                     "exact_price": sum(1 for x in d if abs(x) < 1e-6), "qty_equal": sum(1 for r in rs if r["qty_ratio_sim_to_real"] == 1.0)}
    return {"label": "8 orders: gross-error check only, not a calibration", "day": day, "method": __doc__, "aggregate": agg,
            "per_order": sorted(rows, key=lambda r: r["i"])}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--orders", required=True); ap.add_argument("--data-root", required=True); ap.add_argument("--day", default="2026-04-29")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    res = check(Path(a.orders), Path(a.data_root), a.day)
    Path(a.out).write_text(json.dumps(res, indent=1))
    print(json.dumps(res["aggregate"], indent=1))
    for r in res["per_order"]:
        print({k: r[k] for k in ("i", "pair", "latency", "side", "real_execs", "sim_status", "sim_levels", "qty_ratio_sim_to_real", "vwap_diff_bps")})
