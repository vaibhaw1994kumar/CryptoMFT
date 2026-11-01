#!/usr/bin/env python3
"""Distribution of the hedge venue's 1-second mid-price move, to ground the 'fast move' threshold of the stressed fault preset.

Reads book_snapshots (best_bid / best_ask, one per ~second) of one venue/pair/day from a Tardis-layout data root, takes the last mid in
each whole second and reports the percentiles of |mid(t) - mid(t-1s)| / mid(t-1s) in basis points. Truncated gzip tails are tolerated.
Read only. Usage: python3 -I scripts/fast_move_threshold.py --data-root ../data --venue binance --pair SOLUSDT --days 2022-01-05,2022-01-13 --out config/fast_move_thresholds.json
"""
import argparse, glob, gzip, json
from pathlib import Path


def series(root, venue, pair, day):
    grid, truncated = {}, 0
    for f in sorted(glob.glob(f"{root}/{venue}/book_snapshots/pair={pair}/date={day}/*")):
        try:
            with gzip.open(f, "rt") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    if r.get("best_bid") and r.get("best_ask"):
                        grid[int(r["timestamp"])] = (r["best_bid"] + r["best_ask"]) / 2
        except EOFError:
            truncated += 1
    return grid, truncated


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", required=True); ap.add_argument("--venue", required=True); ap.add_argument("--pair", required=True)
    ap.add_argument("--days", required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = {"schema": "fast_move_thresholds/v1", "venue": a.venue, "pair": a.pair, "unit": "bps of mid over 1 s", "days": {}}
    for day in a.days.split(","):
        g, trunc = series(a.data_root, a.venue, a.pair, day)
        ch = sorted(abs(g[k] - g[k - 1]) / g[k - 1] * 1e4 for k in g if k - 1 in g)
        n = len(ch)
        out["days"][day] = {"seconds": len(g), "pairs": n, "truncated_files": trunc,
                            **{f"p{p}": round(ch[int(p / 100 * (n - 1))], 2) for p in (50, 90, 99)}, "p99.9": round(ch[int(0.999 * (n - 1))], 2), "max": round(ch[-1], 2)}
    Path(a.out).write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(out["days"]))


main()
