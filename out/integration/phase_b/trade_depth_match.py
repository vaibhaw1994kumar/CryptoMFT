"""Phase B, report only: how long after a public print does the depth message that shows the same execution arrive? One venue-day slice per call (TRAIN days only), read-only.
Short-lived measurement script (like out/queue_v2/qv2_probe.py): the reusable rules stay in src/new_backtest; nothing here is used by a replay.

A "print" is one trade record (multiset de-duplication, as the replays load them). A "decrease" is a depth message that lowers the size of the level the print hit (a sell aggressor hits the bid
level at its price, a buy aggressor the ask level), with the level size tracked exactly as OrderBook.apply_delta does (snapshots replace, 'hybrid' for Bybit, 'absolute' for Binance).
Size-aware pairing: a print is PAIRED with the nearest-in-time decrease at its price and side that is at least as large as the print (a batch of prints at one price shows as one decrease, so
"at least as large" is the test; equal size is reported as the strong tier). lag = exchange time of the decrease minus exchange time of the print (positive: depth after print).
The false-match rate of a window is a placebo: the same pairing, with the decrease times of that price shifted by +-7.3 s, +-13.1 s, +-29.7 s (far outside any window), compared with the real rate.
usage: trade_depth_match.py DATASET VENUE DAY [HOURS=2]  -> out/integration/phase_b/match/DATASET_VENUE_DAY.json"""
import json
import sys
import time
from array import array
from bisect import bisect_left, bisect_right
from pathlib import Path

ROOT = Path("/Users/vaibhav/Documents/Newproject")
sys.path.insert(0, str(ROOT / "crypto_MFT_arb_platform" / "src"))
from new_backtest.event_loader import BOOK, DELTA, TRADE, delta_fields, iter_venue_events, snapshot_levels, trade_fields  # noqa: E402

DATASET, VENUE, DAY = sys.argv[1:4]
HOURS = float(sys.argv[4]) if len(sys.argv) > 4 else 2.0
SEMANTICS = {"bybit": "hybrid", "binance": "absolute"}[VENUE]
WIDE = 1.0                                   # s: widest pairing window looked at
WINDOWS_MS = (5, 10, 20, 50, 100, 150, 200, 300, 500, 1000)
SHIFTS_S = (7.3, -7.3, 13.1, -13.1, 29.7, -29.7)
TOL = 1e-7
import os
OUT = Path(os.environ.get("MATCH_OUT", ROOT / "out" / "integration" / "phase_b" / "match"))
OUT.mkdir(parents=True, exist_ok=True)


def levels(side_list):
    for lv in side_list:
        if isinstance(lv, dict):
            yield float(lv.get("price", 0.0)), float(lv.get("size", lv.get("volume", 0.0)))
        else:
            yield float(lv[0]), float(lv[1])


def collect():
    import calendar
    t0 = calendar.timegm(time.strptime(DAY, "%Y-%m-%d"))
    t_end = t0 + HOURS * 3600.0
    book = {"bid": {}, "ask": {}}
    prints: dict[tuple, list] = {}               # (side, price) -> [(t, q)]
    decs: dict[tuple, tuple] = {}                # (side, price) -> (array t, array d)
    n = {"delta": 0, "decrease": 0, "snapshot": 0, "trade": 0, "trade_volume": 0.0, "decrease_volume": 0.0}
    stats: dict[str, int] = {}
    for ev in iter_venue_events(ROOT / "data", VENUE, "SOLUSDT", DAY, DAY, include_trades=True, stats=stats, trade_dedupe="multiset"):
        if ev.exchange_ts >= t_end:
            break
        if ev.kind == BOOK:
            bids, asks = snapshot_levels(ev.payload)
            book = {"bid": dict(levels(bids)), "ask": dict(levels(asks))}
            n["snapshot"] += 1
        elif ev.kind == DELTA:
            side, price, vol, et = delta_fields(ev.payload)
            if price <= 0:
                continue
            s = "bid" if side.startswith("b") else "ask"
            cur = book[s].get(price, 0.0)
            if et in ("delete", "remove"):
                new = 0.0
            elif et == "cancel":
                new = max(0.0, vol) if SEMANTICS == "absolute" else max(0.0, cur - vol)
            elif et in ("add", "insert"):
                new = max(0.0, vol)
            else:
                new = max(0.0, vol)
            if new > 0.0:
                book[s][price] = new
            else:
                book[s].pop(price, None)
            n["delta"] += 1
            if new < cur - TOL:
                key = (s, round(price, 8))
                ent = decs.get(key)
                if ent is None:
                    ent = decs[key] = (array("d"), array("d"))
                ent[0].append(ev.exchange_ts)
                ent[1].append(cur - new)
                n["decrease"] += 1
                n["decrease_volume"] += cur - new
        elif ev.kind == TRADE:
            price, vol, sign = trade_fields(ev.payload)
            if price <= 0 or vol <= 0:
                continue
            key = ("bid" if sign < 0 else "ask", round(price, 8))
            prints.setdefault(key, []).append((ev.exchange_ts, vol))
            n["trade"] += 1
            n["trade_volume"] += vol
    return prints, decs, n, stats


def pair(prints, decs, shift=0.0, wide=WIDE):
    """For every print: (lag of the nearest covering decrease or None, exact?, any decrease inside the wide window?, print volume)."""
    rows = []
    for key, plist in prints.items():
        ent = decs.get(key)
        if ent is None:
            rows.extend((None, False, False, q, t) for t, q in plist)
            continue
        order = sorted(range(len(ent[0])), key=ent[0].__getitem__)
        dt = [ent[0][i] + shift for i in order]
        dv = [ent[1][i] for i in order]
        for t, q in plist:
            lo, hi = bisect_left(dt, t - wide), bisect_right(dt, t + wide)
            best = None
            exact = False
            for j in range(lo, hi):
                if dv[j] >= q - TOL:
                    lag = dt[j] - t
                    if best is None or abs(lag) < abs(best):
                        best, exact = lag, abs(dv[j] - q) <= TOL
                    elif abs(lag) == abs(best) and abs(dv[j] - q) <= TOL:
                        exact = True
            rows.append((best, exact, hi > lo, q, t))
    return rows


def summarise(rows, windows_ms=WINDOWS_MS):
    n = len(rows)
    vol = sum(r[3] for r in rows)
    out = {"prints": n, "print_volume": vol, "paired_wide": sum(r[0] is not None for r in rows), "exact_size_pair": sum(r[1] for r in rows),
           "no_decrease_at_all_in_wide_window": sum(not r[2] for r in rows), "decrease_only_smaller_than_print": sum(r[2] and r[0] is None for r in rows)}
    out["share_no_pair"] = (n - out["paired_wide"]) / n if n else None
    for w in windows_ms:
        k = [r for r in rows if r[0] is not None and abs(r[0]) <= w / 1000.0]
        out[f"within_{w}ms"] = len(k)
        out[f"share_within_{w}ms"] = len(k) / n if n else None
        out[f"volume_within_{w}ms"] = sum(r[3] for r in k)
    lags = sorted(r[0] for r in rows if r[0] is not None)
    if lags:
        q = lambda p: lags[min(len(lags) - 1, int(p * len(lags)))]
        hist = [0] * 130                               # 10 ms bins from -300 ms to +1000 ms
        for l in lags:
            b = int((l + 0.3) / 0.01)
            if 0 <= b < 130:
                hist[b] += 1
        out["lag_hist_10ms_from_minus300ms"] = hist
        out["lag_s"] = {"p05": q(0.05), "p25": q(0.25), "p50": q(0.5), "p75": q(0.75), "p95": q(0.95), "share_depth_first": sum(l < 0 for l in lags) / len(lags)}
    return out


def main():
    t0 = time.time()
    prints, decs, n, stats = collect()
    t1 = time.time()
    real = pair(prints, decs)
    res = {"dataset": DATASET, "venue": VENUE, "day": DAY, "hours_from_00utc": HOURS, "counts": n, "loader": stats, "real": summarise(real), "collect_s": t1 - t0}
    plc = [summarise(pair(prints, decs, shift=s)) for s in SHIFTS_S]
    res["placebo_lag_hist_10ms_from_minus300ms"] = [sum(p["lag_hist_10ms_from_minus300ms"][i] for p in plc) / len(plc) for i in range(130)]
    res["placebo"] = {f"within_{w}ms": sum(p[f"within_{w}ms"] for p in plc) / len(plc) for w in WINDOWS_MS}
    res["placebo_share"] = {f"within_{w}ms": (sum(p[f"within_{w}ms"] for p in plc) / len(plc)) / max(1, len(real)) for w in WINDOWS_MS}
    # missed by today's windows (credit clock is the exchange time): dedupe_window_s 0.05 both directions; evidence_window_ms 20 only for a decrease BEFORE its print
    paired = [r for r in real if r[0] is not None]
    res["today"] = {
        "paired_prints": len(paired),
        "outside_dedupe_50ms": sum(abs(r[0]) > 0.05 for r in paired), "volume_outside_dedupe_50ms": sum(r[3] for r in paired if abs(r[0]) > 0.05),
        "depth_first_pairs": sum(r[0] < 0 for r in paired),
        "depth_first_outside_evidence_20ms": sum(r[0] < -0.020 for r in paired), "volume_depth_first_outside_evidence_20ms": sum(r[3] for r in paired if r[0] < -0.020),
        "print_first_pairs": sum(r[0] >= 0 for r in paired),
    }
    res["total_s"] = time.time() - t0
    (OUT / f"{DATASET}_{VENUE}_{DAY}.json").write_text(json.dumps(res, indent=1))
    r = res["real"]
    print(DATASET, VENUE, DAY, "prints", r["prints"], "paired", r["paired_wide"], "within50", r["share_within_50ms"], "within100", r["share_within_100ms"], f"{res['total_s']:.0f}s", flush=True)


main()
