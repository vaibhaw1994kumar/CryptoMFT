"""Sweep-cost check for MARKET_BANDED (owner 9 Nov, item 3). A run wrapper, read only. Compares the simulator's multi-level sweep with the account's REAL IOC and market sweeps, aggregates only.

Simulator side: the core's own taker walk, `DeterministicVenueAdapter._walk(book, side, quantity, limit)` (what every simulated IOC / market order uses), run on the collector's 1 s snapshot (<= 2 s old at the order's creation), consume_liquidity off
(a single order). Real side: orders of the account's archive (bybit_30d.zip) with orderType Market or timeInForce IOC / FOK, their executions (volume-weighted price, quantity, number of distinct prices).
Cost in bps of the volume-weighted price against the touch on the order's side (best ask for a buy, best bid for a sell): positive = paid up. Only the collector week 2026-04-19..25 (spot) has books. Nothing here carries an order id; account data stays on this Mac.
Usage: sweep_cost_check.py OUT_JSON"""
import collections
import datetime as dt
import glob
import gzip
import json
import re
import sys
import zipfile
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from crypto_stat_arb.execution import DeterministicVenueAdapter  # noqa: E402
from crypto_stat_arb.models import Side  # noqa: E402

ZIP = Path.home() / "Downloads" / "bybit_30d.zip"
ROOT = "/Users/vaibhav/Documents/Newproject/data/bybit-spot"
TS = re.compile(rb'"timestamp": ?([0-9.]+)')
day_of = lambda s: dt.datetime.fromtimestamp(s, dt.timezone.utc).strftime("%Y-%m-%d")


class Book:                                              # the two fields _walk reads
    def __init__(self, asks, bids):
        self.asks, self.bids, self.instrument_id = asks, bids, "X"


def load_orders():
    z = zipfile.ZipFile(ZIP)
    names = set(z.namelist())
    out, inv = [], collections.Counter()
    for n in sorted(names):
        b = n.split("/")[-1]
        if not b.startswith("order_"):
            continue
        cat, coin = b.split("_")[1], b.split("_")[2].split(".")[0]
        ex = collections.defaultdict(list)
        en = n.replace("order_", "exec_")
        if en in names:
            with z.open(en) as f:
                for line in f:
                    r = json.loads(line)
                    q = float(r.get("execQty") or 0)
                    if q > 0:
                        ex[r["orderId"]].append((float(r["execPrice"]), q))
        with z.open(n) as f:
            for line in f:
                r = json.loads(line)
                if not (r["orderType"] == "Market" or r["timeInForce"] in ("IOC", "FOK")):
                    continue
                fills = ex.get(r["orderId"], [])
                q = sum(x[1] for x in fills)
                inv[(cat, r["orderType"], r["timeInForce"])] += 1
                out.append({"cat": cat, "coin": coin, "buy": r["side"] == "Buy", "qty": float(r["qty"]), "limit": float(r["price"]) if r["orderType"] == "Limit" else None, "created": int(r["createdTime"]) / 1000.0,
                            "filled": q, "vwap": sum(p * x for p, x in fills) / q if q else None, "levels": len({p for p, _ in fills}), "market_unit": r.get("marketUnit")})
    return out, {f"{k[0]}|{k[1]}|{k[2]}": v for k, v in inv.items()}


def snapshots(coin, day, wanted):
    """The latest snapshot at or before each wanted time (one pass over the day's files)."""
    W = sorted(wanted)
    out, idx, prev_line, prev_ts = {}, 0, None, None
    for path in sorted(glob.glob(f"{ROOT}/book_snapshots/pair={coin}/date={day}/*.jsonl.gz")):
        with gzip.open(path, "rb") as fh:
            try:
                for line in fh:
                    m = TS.search(line)
                    if not m:
                        continue
                    ts = float(m.group(1))
                    while idx < len(W) and ts > W[idx]:
                        if prev_line is not None:
                            out[W[idx]] = (prev_ts, json.loads(prev_line))
                        idx += 1
                    prev_line, prev_ts = line, ts
            except EOFError:
                pass
    while idx < len(W) and prev_line is not None:
        out[W[idx]] = (prev_ts, json.loads(prev_line))
        idx += 1
    return out


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * q))] if v else None


def main():
    orders, inventory = load_orders()
    spot = [o for o in orders if o["cat"] == "spot" and "2026-04-19" <= day_of(o["created"]) <= "2026-04-25"]
    by = collections.defaultdict(list)
    for o in spot:
        by[(o["coin"], day_of(o["created"]))].append(o)
    rows, dropped = [], collections.Counter()
    adapter = DeterministicVenueAdapter("BYBIT", consume_liquidity=False)
    for (coin, day), os_ in sorted(by.items()):
        snaps = snapshots(coin, day, {round(o["created"], 3) for o in os_})
        for o in os_:
            got = snaps.get(round(o["created"], 3))
            if got is None:
                dropped["no_snapshot"] += 1
                continue
            ts, r = got
            if o["created"] - ts > 2.0:
                dropped["snapshot_older_than_2s"] += 1
                continue
            if o["filled"] <= 0:
                dropped["real_order_not_executed"] += 1
                continue
            if o["market_unit"] == "quoteCoin":
                dropped["quote_sized_market_order"] += 1                  # the quantity is in quote currency: not comparable without the executed base quantity
                continue
            asks = {Decimal(repr(p)): Decimal(repr(s)) for p, s in r["asks"]}
            bids = {Decimal(repr(p)): Decimal(repr(s)) for p, s in r["bids"]}
            side = Side.BUY if o["buy"] else Side.SELL
            limit = Decimal(repr(o["limit"])) if o["limit"] is not None else (Decimal("Infinity") if o["buy"] else Decimal("0"))
            qty = Decimal(repr(o["qty"]))
            filled, avg, takes = adapter._walk(Book(asks, bids), side, qty, limit)
            touch = r["best_ask"] if o["buy"] else r["best_bid"]
            sign = 1.0 if o["buy"] else -1.0
            rows.append({"levels_real": o["levels"], "levels_sim": len(takes), "real_cost_bps": sign * (o["vwap"] - touch) / touch * 1e4,
                         "sim_cost_bps": sign * (float(avg) - touch) / touch * 1e4 if filled > 0 else None, "real_filled": o["filled"], "sim_filled": float(filled), "qty": o["qty"],
                         "sim_depth_exhausted": float(filled) < float(qty) - 1e-12, "notional_usdt": o["filled"] * o["vwap"]})
    both = [x for x in rows if x["sim_cost_bps"] is not None]
    d = [x["sim_cost_bps"] - x["real_cost_bps"] for x in both]
    multi_real = [x for x in both if x["levels_real"] >= 2]
    multi_sim = [x for x in both if x["levels_sim"] >= 2]
    res = {"label": "UNVALIDATED_SWEEP_COST until the owner accepts this comparison", "orders_in_archive_by_kind": inventory, "spot_orders_in_collector_week": len(spot), "dropped": dict(dropped),
           "compared_orders": len(both),
           "real_multi_level_sweeps_in_archive_spot_and_linear": sum(1 for o in orders if o["levels"] >= 2), "real_multi_level_in_compared_set": len(multi_real), "sim_multi_level_in_compared_set": len(multi_sim),
           "cost_bps_vs_touch": {"real": {"mean": sum(x["real_cost_bps"] for x in both) / max(len(both), 1), "p50": pct([x["real_cost_bps"] for x in both], .5), "p90": pct([x["real_cost_bps"] for x in both], .9)},
                                 "sim": {"mean": sum(x["sim_cost_bps"] for x in both) / max(len(both), 1), "p50": pct([x["sim_cost_bps"] for x in both], .5), "p90": pct([x["sim_cost_bps"] for x in both], .9)},
                                 "sim_minus_real": {"mean": sum(d) / max(len(d), 1), "p10": pct(d, .1), "p50": pct(d, .5), "p90": pct(d, .9), "min": min(d) if d else None, "max": max(d) if d else None, "orders_outside_0.5bps": sum(1 for v in d if abs(v) > 0.5), "share_sim_within_0.5bps": sum(1 for v in d if abs(v) <= 0.5) / max(len(d), 1)}},
           "multi_level_real_orders": [{"levels_real": x["levels_real"], "levels_sim": x["levels_sim"], "qty": x["qty"], "notional_usdt": round(x["notional_usdt"], 2), "real_cost_bps": round(x["real_cost_bps"], 3),
                                        "sim_cost_bps": None if x["sim_cost_bps"] is None else round(x["sim_cost_bps"], 3)} for x in multi_real],
           "depth_exhausted_in_sim": sum(1 for x in both if x["sim_depth_exhausted"]), "real_partially_filled": sum(1 for x in both if x["real_filled"] < x["qty"] * 0.999),
           "note": "single-level orders test the touch and the one-level price; the multi-level sample is too small to test a depth walk; the snapshot is up to 2 s old"}
    Path(sys.argv[1]).write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res, indent=1, default=str)[:4500])


if __name__ == "__main__":
    main()
