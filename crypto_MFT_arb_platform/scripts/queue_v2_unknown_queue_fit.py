#!/usr/bin/env python3
"""Queue model v2, step 7: fit the unknown-queue distribution on TRAINING days only (reads book snapshots; read-only on data/).

    python3.12 scripts/queue_v2_unknown_queue_fit.py --data-root DATA --pair SOLUSDT --venues binance,bybit --train-days 2022-01-04 --test-day 2022-01-05 --out out/queue_v2/unknown_queue_fit_for_2022-01-05.json
Every training day must be strictly before the test day. The size at distance d from the touch is the queue a new order would join behind when the level is displayed."""
import argparse, glob, gzip, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from new_backtest.queue_v2.unknown_queue import fit_from_snapshots   # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--data-root", required=True); ap.add_argument("--pair", default="SOLUSDT"); ap.add_argument("--venues", default="binance,bybit")
ap.add_argument("--train-days", required=True); ap.add_argument("--test-day", required=True); ap.add_argument("--out", required=True)
a = ap.parse_args()
days = a.train_days.split(",")
assert all(d < a.test_day for d in days), "training days must be strictly before the test day"
doc = {"schema": "queue_v2_unknown_queue_fit/v1", "pair": a.pair, "test_day": a.test_day, "train_days": days, "source": "book_snapshots, in-depth levels", "venues": {}}
for v in a.venues.split(","):
    def snaps():
        for d in days:
            for f in sorted(glob.glob(f"{a.data_root}/{v}/book_snapshots/pair={a.pair}/date={d}/*.jsonl.gz")):
                try:
                    with gzip.open(f, "rt") as fh:
                        for line in fh:
                            yield json.loads(line)
                except EOFError:
                    pass
    # tick = the most common positive gap between neighbouring levels in the first 2000 snapshots
    gaps = []
    for i, s in enumerate(snaps()):
        if i > 2000: break
        for side in ("bids", "asks"):
            lv = s.get(side) or []
            gaps += [round(abs(float(lv[j][0]) - float(lv[j + 1][0])), 8) for j in range(len(lv) - 1)]
    from collections import Counter
    tick = Counter(g for g in gaps if g > 0).most_common(1)[0][0]            # the modal gap: a stray sub-tick level must not set the grid
    fit = fit_from_snapshots(snaps(), tick)
    fit["tick"] = tick
    doc["venues"][v] = fit
    print(v, "tick", tick, [(b["d_lo"], b["n"], b["median"] and round(b["median"], 2), b["sigma"] and round(b["sigma"], 2)) for b in fit["buckets"]])
Path(a.out).write_text(json.dumps(doc, indent=1))
