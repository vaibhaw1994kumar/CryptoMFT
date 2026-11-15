"""Item 2 clock-basis check (FOLLOWUP_RULES.md): the probe fill timestamps against the two Binance series: (A) the probe_shortfall.py MAIN basis (data/binance/book_snapshots, local_ts or timestamp) and (B) the t5_split.py basis (data/binance book events, exchange_ts).
For each day: the mean 45 ms fill-to-hedge move of the Binance bid, (bid(t + 0.045) - bid(t)) / bid(t) in bps, per basis; pinned (7 TRAIN days, basis A): -0.463. usage: t5c_clock_check.py DAY..."""
import bisect, glob, gzip, json, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
import numpy as np
from new_backtest.event_loader import BOOK, iter_venue_events
B0 = Path("/Users/vaibhav/Documents/Newproject/out/integration/b0")
SNAP = "/Users/vaibhav/Documents/Newproject/data/binance/book_snapshots/pair=SOLUSDT"
for day in sys.argv[1:]:
    t0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    group = "probe_adverse" if day <= "2024-03-07" else "probe/select"
    fills = [r["ts"] for r in json.load(open(B0 / group / f"{day}.json"))["rows"]]
    ts, bid = [], []
    for fp in sorted(glob.glob(f"{SNAP}/date={day}/*.gz")):
        with gzip.open(fp, "rt") as fh:
            for l in fh:
                r = json.loads(l)
                t = float(r.get("local_ts") or r["timestamp"])
                if t > t0 + 7200 + 60:
                    break
                ts.append(t); bid.append(float(r["best_bid"]))
    bt, bb = [], []
    for ev in iter_venue_events("/Users/vaibhav/Documents/Newproject/data", "binance", "SOLUSDT", day, day):
        if ev.exchange_ts > t0 + 7200 + 60:
            break
        if ev.kind == BOOK:
            b = ev.payload.get("bids") or []
            if b:
                bt.append(ev.exchange_ts); bb.append(float(b[0][0]))
    atA = lambda t: bid[max(0, bisect.bisect_right(ts, t) - 1)]
    atB = lambda t: bb[max(0, bisect.bisect_right(bt, t) - 1)]
    mA = np.mean([(atA(t + 0.045) - atA(t)) / atA(t) * 1e4 for t in fills])
    mB = np.mean([(atB(t + 0.045) - atB(t)) / atB(t) * 1e4 for t in fills])
    off = np.mean([atA(t) - atB(t) for t in fills])
    print(f"{day}: fills {len(fills)}  fill->hedge 45 ms move, A snapshot series {mA:.3f} bps, B event series {mB:.3f} bps, mean bid(A) - bid(B) at the fill {off:.5f}; series sizes A {len(ts)} B {len(bt)}; first A {ts[0]-t0:.3f} B {bt[0]-t0:.3f} s after midnight", flush=True)
