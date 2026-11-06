"""Owner 7 Nov, S0 design, report only: how often would a quote that follows the hedge venue be repriced, against the rate limits of both venues? NOT a fit and NOT a P&L: the bands below are a design grid,
the fit of band, debounce and age on the 7 TRAIN days comes with the S0 build. Orders: the 3,213 probe orders of the 7 TRAIN days (probe_adverse, v1_2 settings), each live from its arrival to its first fill or 15 s.
Trigger: the quote is priced as bid_binance x (1 - q), q = 6.546 bps (the probe's own rule); at every Binance bid update (view delay +36 ms, the singapore preset) the target = bid x (1 - q) rounded down to the tick;
reprice DOWN when resting - target > B bps, UP when target - resting > U bps (U = never, or 2 B); at most one reprice per min_interval M. Every reprice is one amend (Bybit /v5/order/amend; queue priority lost).
Limits from core capabilities.py: Bybit PERP: IP 600 requests / 5 s (DOCUMENTED), /v5/order/amend 10 / s, create 10 / s, cancel 10 / s (ASSUMED); Binance PERP: ORDERS 300 / 10 s and 1,200 / 60 s, weight 2,400 / 60 s (ASSUMED).
Output probe_adverse/s0_reprice_rate_estimate.json. EXPLORATORY."""
import bisect, glob, gzip, json, math, statistics as st
from pathlib import Path
R = Path("/Users/vaibhav/Documents/Newproject/out/integration/b0/probe_adverse")
SL = Path("/Users/vaibhav/Documents/Newproject/out/queue_v2/slices_mar2024")
DAYS = [f"2024-03-0{i}" for i in range(1, 8)]
Q, TICK, TTL, DELAY = 6.546, 0.001, 15.0, 0.036
BANDS, MINS, UPS = (0.5, 1.0, 2.0, 3.0, 5.0), (0.0, 0.25, 1.0), ("never", "2B")


def load(day):
    ts, bid = [], []
    for fp in sorted(glob.glob(f"{SL}/binance/book_snapshots/pair=SOLUSDT/date={day}/*.gz")):
        with gzip.open(fp, "rt") as fh:
            for l in fh:
                r = json.loads(l); ts.append(float(r.get("local_ts") or r["timestamp"]) + DELAY); bid.append(float(r["best_bid"]))
    d = json.load(open(R / f"{day}.json"))
    obs = [json.loads(l) for l in open(R / f"run_{day}" / "maker_observations.jsonl")]
    first_fill = {}
    for o in obs:
        if o["obs"] == "maker_fill":
            first_fill.setdefault(o["signal_id"], o["ts"])
    orders = [(o["ts"], o["price"], min(first_fill.get(o["signal_id"], 1e18), o["ts"] + TTL), o["signal_id"] in first_fill) for o in obs if o["obs"] == "maker_place"]
    return ts, bid, orders


def simulate(ts, bid, orders, B, M, U):
    per_order, events_by_day = [], []
    for (t0, price, end, filled) in orders:
        i = bisect.bisect_right(ts, t0)
        resting, last, n, times = price, t0 - 1e9, 0, []
        while i < len(ts) and ts[i] <= end:
            target = math.floor(bid[i] * (1 - Q / 1e4) / TICK + 1e-9) * TICK
            down = (resting - target) / resting * 1e4 > B
            up = U != "never" and (target - resting) / resting * 1e4 > 2 * B
            if (down or up) and ts[i] - last >= M:
                resting, last, n = target, ts[i], n + 1
                times.append(ts[i])
            i += 1
        per_order.append((n, end - t0 if end < 1e17 else TTL, times, filled))
    return per_order


def peak_window(times, w):
    best, j = 0, 0
    for i in range(len(times)):
        while times[i] - times[j] >= w:
            j += 1
        best = max(best, i - j + 1)
    return best


res = {"label": "S0 design: reprice-rate estimate for a quote that follows Binance; probe orders of 7 TRAIN days; design grid, not a fit; EXPLORATORY", "grid": []}
data = {d: load(d) for d in DAYS}
n_orders = sum(len(v[2]) for v in data.values())
live_s = sum(min(o[2] - o[0], TTL) for v in data.values() for o in v[2])
res["orders"] = n_orders; res["live_seconds"] = live_s
for B in BANDS:
    for M in MINS:
        for U in UPS:
            counts, peaks1, peaks5, alltimes_peak10, total, filled_after = [], [], [], [], 0, 0
            peak_day_1s = 0; peak_day_5s = 0
            for d in DAYS:
                ts, bid, orders = data[d]
                po = simulate(ts, bid, orders, B, M, U)
                ev = sorted(t for (_, _, times, _) in po for t in times)       # one live order at a time in the probe: the per-day stream is the account's amend stream
                peak_day_1s = max(peak_day_1s, peak_window(ev, 1.0)); peak_day_5s = max(peak_day_5s, peak_window(ev, 5.0))
                counts += [n for (n, _, _, _) in po]; total += sum(n for (n, _, _, _) in po)
            cs = sorted(counts)
            res["grid"].append({"band_bps": B, "min_interval_s": M, "up": U, "reprices_total": total, "per_order_mean": total / len(counts), "per_order_p90": cs[int(0.9 * len(cs))], "per_order_max": cs[-1],
                                "orders_with_no_reprice_share": sum(c == 0 for c in counts) / len(counts), "per_live_second": total / live_s,
                                "peak_amends_in_any_1s": peak_day_1s, "peak_amends_in_any_5s": peak_day_5s,
                                "bybit_amend_utilisation_peak_1s_of_10": peak_day_1s / 10.0, "bybit_ip_peak_5s_of_600": (peak_day_5s + 0) / 600.0})
(R / "s0_reprice_rate_estimate.json").write_text(json.dumps(res, indent=1))
print("orders", n_orders, "live seconds", round(live_s))
for g in res["grid"]:
    print(f"B={g['band_bps']:>3} M={g['min_interval_s']:<4} up={g['up']:5s} total={g['reprices_total']:6d} per_order mean={g['per_order_mean']:.2f} p90={g['per_order_p90']} max={g['per_order_max']} no_reprice={g['orders_with_no_reprice_share']:.2f} per_s={g['per_live_second']:.3f} peak1s={g['peak_amends_in_any_1s']} peak5s={g['peak_amends_in_any_5s']}")
