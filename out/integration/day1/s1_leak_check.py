"""Step C (owner 11 Nov), S1 label leak check on ONE TRAIN day (a run wrapper over new_backtest.market_hedge_move.day_labels; READ ONLY on data/).
Question: is the Binance mid used as m(t) in a label row the mid strictly before the Bybit sweep's own updates (no timing leak)?
By construction the Binance mid comes from a book that only ever receives Binance events (`hedge_book`), so Bybit's sweep and the Bybit updates that follow it cannot enter it; the merge order is (exchange_ts, venue kind 0 = Binance first, index),
so a Binance event with exactly the sweep's exchange timestamp is applied BEFORE the print: m(t) is 'at or before t', not 'strictly before t'. This script counts those ties and compares the 50 ms label of tied and untied events.
usage: s1_leak_check.py DAY OUT_JSON"""
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from new_backtest.market_hedge_move import day_labels

day, out = sys.argv[1], Path(sys.argv[2])
cur = prev = None            # the last two distinct Binance exchange times seen so far (tick_hook runs BEFORE close_group of a group, so the item that closes a group may already be in `cur`)
tied_rows: list[bool] = []


def tick(ts, kind, ev, hedge_book, maker_book):
    global cur, prev
    if kind == 0 and ts != cur:
        prev, cur = cur, ts


def hook(row, hedge_book, maker_book, sweep):
    tied_rows.append(row["ts"] == cur or row["ts"] == prev)


t = time.time()
lab = day_labels("/Users/vaibhav/Documents/Newproject/data", "SOLUSDT", day, horizons=(0.05, 1.0), hook=hook, tick_hook=tick)
rows = lab["rows"]
assert len(rows) == len(tied_rows)
res = {"day": day, "events": len(rows), "events_with_a_binance_update_at_the_same_exchange_ts": sum(tied_rows), "share_tied": sum(tied_rows) / max(len(rows), 1), "wall_s": round(time.time() - t, 1)}
for h in ("move_0.05s", "move_1s"):
    for name, flag in (("tied", True), ("untied", False)):
        v = [r[h] for r, f in zip(rows, tied_rows) if f == flag and r[h] is not None]
        res[f"{h}_{name}"] = {"n": len(v), "mean_bps": statistics.mean(v) if v else None, "median_bps": statistics.median(v) if v else None}
out.write_text(json.dumps(res, indent=1))
print(json.dumps(res, indent=1))
