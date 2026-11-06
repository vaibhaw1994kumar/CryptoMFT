"""Phase B, report only (owner 6 Nov step 3 check): how far does queue_ahead at placement differ between the VIEW book (what the replay places against: the feed-delayed book at the placement time) and the
TRUTH book at the order's arrival (the exchange-time book PostOnly is judged on)? Existing logs record only the view value (maker_place.queue_ahead / level_size), so this RECONSTRUCTS both from the raw data
for the orders already placed in existing runs. It is a read-only data scan, not a replay; nothing here is used by a run.
Method: for each placed order (venue, side, price, ts = host time of placement = release time) the size of the level at that price is tracked through every snapshot and delta of the day (hybrid semantics for Bybit,
exactly as OrderBook.apply_delta). VIEW = last state whose record was received (local_ts) at or before ts. TRUTH = last state whose exchange_ts is at or before ts minus the host-exchange offset (median of
local_ts - exchange_ts over the 10 s before ts: what the replay's clock aligner estimates). The reconstruction is validated against the level_size the run logged.
usage: placement_view_vs_truth.py   -> out/integration/phase_b/placement_view_vs_truth.json"""
import glob
import json
import statistics
import sys
import time
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path

ROOT = Path("/Users/vaibhav/Documents/Newproject")
sys.path.insert(0, str(ROOT / "crypto_MFT_arb_platform" / "src"))
from new_backtest.event_loader import BOOK, DELTA, delta_fields, iter_venue_events, snapshot_levels  # noqa: E402

R = ROOT / "out" / "integration"
SOURCES = [  # (dataset, day, observation file glob)
    ("jan2022", "2022-01-13", str(R / "phase_b/ledger/runs/l0/jan/perp_perp_maker_taker_v1_1/*/2022-01-13/maker_observations.jsonl")),
    ("jan2022", "2022-01-05", str(R / "baselines/runs/b1/jan/perp_perp_maker_taker_v1_1/*/2022-01-05/maker_observations.jsonl")),
] + [("mar2024", d, str(R / f"phase_b/ledger/runs/l0/base/perp_perp_maker_taker_v1_2/{d}/maker_observations.jsonl")) for d in ("2024-03-08", "2024-03-09", "2024-03-10", "2024-03-11")]


def levels(side_list):
    for lv in side_list:
        if isinstance(lv, dict):
            yield float(lv.get("price", 0.0)), float(lv.get("size", lv.get("volume", 0.0)))
        else:
            yield float(lv[0]), float(lv[1])


def orders_of(pattern):
    out = []
    for f in glob.glob(pattern):
        for line in open(f):
            r = json.loads(line)
            if r.get("obs") == "maker_place" and r["venue"] == "bybit":
                out.append(r)
    return out


def scan_day(day, orders):
    keys = {("bid" if o["side"] == "buy" else "ask", round(o["price"], 8)) for o in orders}
    hist = defaultdict(list)                      # key -> [(exchange_ts, local_ts, size)] in loader order
    size = {k: 0.0 for k in keys}
    offs = []                                     # (local_ts, local_ts - exchange_ts) of every Nth record, for the host offset
    n = 0
    t_last = max(o["ts"] for o in orders) + 5.0
    for ev in iter_venue_events(ROOT / "data", "bybit", "SOLUSDT", day, day, include_trades=False):
        if ev.local_ts > 0 and ev.local_ts > t_last:
            break
        n += 1
        if ev.local_ts > 0 and n % 50 == 0:
            offs.append((ev.local_ts, ev.local_ts - ev.exchange_ts))
        if ev.kind == BOOK:
            bids, asks = snapshot_levels(ev.payload)
            m = {("bid", round(p, 8)): s for p, s in levels(bids)}
            m.update({("ask", round(p, 8)): s for p, s in levels(asks)})
            for k in keys:
                new = m.get(k, 0.0)
                size[k] = new
                hist[k].append((ev.exchange_ts, ev.local_ts, new))
        elif ev.kind == DELTA:
            side, price, vol, et = delta_fields(ev.payload)
            k = ("bid" if side.startswith("b") else "ask", round(price, 8))
            if k not in keys:
                continue
            cur = size[k]
            if et in ("delete", "remove"):
                new = 0.0
            elif et == "cancel":
                new = max(0.0, cur - vol)
            else:
                new = max(0.0, vol)
            size[k] = new
            hist[k].append((ev.exchange_ts, ev.local_ts, new))
    return hist, offs


def lookup(hist_k, t, by):
    best = None
    for e in hist_k:
        if e[by] <= t:
            best = e[2]
        elif by == 0:
            break
    return best if best is not None else 0.0


def main():
    t0 = time.time()
    rows = []
    for ds, day, pattern in SOURCES:
        orders = orders_of(pattern)
        if not orders:
            continue
        hist, offs = scan_day(day, orders)
        lts = [a for a, _ in offs]
        for o in orders:
            k = ("bid" if o["side"] == "buy" else "ask", round(o["price"], 8))
            i = bisect_right(lts, o["ts"])
            window = [d for (a, d) in offs[max(0, i - 40):i]] or [0.0]
            offset = statistics.median(window)
            view = lookup(hist[k], o["ts"], 1)
            truth = lookup(hist[k], o["ts"] - offset, 0)
            rows.append({"dataset": ds, "day": day, "ts": o["ts"], "price": o["price"], "side": o["side"], "logged_level": o["level_size"], "logged_queue_ahead": o["queue_ahead"],
                         "level_was_absent_logged": o["level_was_absent"], "view": view, "truth": truth, "offset_ms": offset * 1000.0})
    summ = {}
    for ds in ("jan2022", "mar2024", "all"):
        rr = [r for r in rows if ds == "all" or r["dataset"] == ds]
        if not rr:
            continue
        valid = [abs(r["view"] - r["logged_level"]) <= 1e-6 for r in rr]
        d = [r["truth"] - r["view"] for r in rr]
        ad = sorted(abs(x) for x in d)
        q = lambda p: ad[min(len(ad) - 1, int(p * len(ad)))]
        rel = sorted(abs(r["truth"] - r["view"]) / r["view"] for r in rr if r["view"] > 0)
        summ[ds] = {"orders": len(rr), "reconstruction_matches_logged_level": sum(valid), "differ_at_all": sum(abs(x) > 1e-9 for x in d), "abs_diff_median": q(0.5), "abs_diff_p90": q(0.9), "abs_diff_max": ad[-1],
                    "rel_diff_median_of_view": (rel[len(rel) // 2] if rel else None), "rel_diff_p90_of_view": (rel[int(0.9 * len(rel))] if rel else None),
                    "truth_larger": sum(x > 1e-9 for x in d), "truth_smaller": sum(x < -1e-9 for x in d),
                    "view_absent_truth_present": sum(r["view"] == 0.0 and r["truth"] > 0 for r in rr), "view_present_truth_absent": sum(r["view"] > 0 and r["truth"] == 0.0 for r in rr),
                    "view_absent_and_truth_absent": sum(r["view"] == 0.0 and r["truth"] == 0.0 for r in rr), "median_offset_ms": statistics.median(r["offset_ms"] for r in rr)}
    out = {"summary": summ, "rows": rows, "seconds": time.time() - t0}
    (R / "phase_b" / "placement_view_vs_truth.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(summ, indent=1))


main()
