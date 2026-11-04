"""Oct-2024 Bybit rebuild acceptance with misses broken down by cause (session start, gaps, side, level depth). Read-only on data/. usage: oct_acc_causes.py DAY
Per snapshot the exact fixed-point book (per message, deltas only since the previous snapshot) is compared with the reference: touch price, touch size, top-10 levels.
Cause of a snapshot: session_start = within 120 s of the first snapshot of the day or of the first snapshot after an event gap >= 5 s; gap = a delta gap >= 1 s since the previous snapshot; else in_session."""
import collections, json, os, sys
sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from new_backtest.event_loader import BOOK, DELTA, iter_venue_events, snapshot_levels
from new_backtest.queue_v2.rebuild import ExactBook, to_int

DATA = "/Users/vaibhav/Documents/Newproject/data"
day = sys.argv[1]
man = json.load(open("/Users/vaibhav/Documents/Newproject/out/queue_v2/manifest.json"))["days"][f"bybit/SOLUSDT/{day}"]["channels"]
t_stop = man["book_events"]["last_ts"] + 0.5
b = ExactBook()
n_snap = 0
sess_start, last_ev, last_delta, last_snap = None, None, None, None
interval_gap = 0.0
cause_n = collections.Counter(); touch_price_miss = collections.Counter(); touch_size_miss = collections.Counter()
lvl_miss = collections.Counter(); lvl_n = collections.Counter()
examples = []
def bucket(i): return "L1" if i == 0 else ("L2-3" if i < 3 else ("L4-5" if i < 5 else "L6-10"))
for ev in iter_venue_events(DATA, "bybit", "SOLUSDT", day, day, include_trades=False):
    ts = ev.exchange_ts
    if ts > t_stop: break
    if last_ev is not None and ts - last_ev >= 5.0:
        sess_start = None                                   # a restart: the next snapshot opens a new session
    last_ev = ts
    if ev.kind == DELTA:
        p = ev.payload
        b.set_level(str(p.get("side", "bid")).lower(), to_int(p["price"]), 0 if str(p.get("event_type")).lower() in ("delete", "remove") else to_int(p.get("volume", 0)))
        if last_delta is not None:
            interval_gap = max(interval_gap, ts - last_delta)
        last_delta = ts
    elif ev.kind == BOOK:
        bids, asks = snapshot_levels(ev.payload)
        if sess_start is None:
            sess_start = ts
        elif b.valid:
            n_snap += 1
            cause = "session_start" if ts - sess_start < 120.0 else ("gap" if interval_gap >= 1.0 else "in_session")
            cause_n[cause] += 1
            rb = [(to_int(p), to_int(s)) for p, s in bids[:10]]; ra = [(to_int(p), to_int(s)) for p, s in asks[:10]]
            vb, va = b.top("bid", 10), b.top("ask", 10)
            tp = bool(rb and ra and vb and va and vb[0][0] == rb[0][0] and va[0][0] == ra[0][0])
            ts_ok = bool(rb and ra and vb and va and vb[0] == rb[0] and va[0] == ra[0])
            touch_price_miss[cause] += (not tp); touch_size_miss[cause] += (not ts_ok)
            for side, ref, var in (("bid", rb, vb), ("ask", ra, va)):
                for i, lvl in enumerate(ref):
                    k = (cause, side, bucket(i)); lvl_n[k] += 1; lvl_miss[k] += not (i < len(var) and var[i] == lvl)
            if not tp and len(examples) < 10:
                examples.append({"ts": ts, "cause": cause, "ref": [rb[:2], ra[:2]], "rebuilt": [vb[:2], va[:2]]})
        b.reset(bids, asks)
        last_snap = ts
        interval_gap = 0.0
out = {"day": day, "snapshots_compared": n_snap, "by_cause": {}}
for c in cause_n:
    n = cause_n[c]
    out["by_cause"][c] = {"snapshots": n, "touch_price_miss_pct": 100 * touch_price_miss[c] / n, "touch_size_miss_pct": 100 * touch_size_miss[c] / n,
                          "level_miss_pct": {f"{s}/{bk}": 100 * lvl_miss[(c, s, bk)] / lvl_n[(c, s, bk)] for s in ("bid", "ask") for bk in ("L1", "L2-3", "L4-5", "L6-10") if lvl_n[(c, s, bk)]}}
out["examples"] = examples
os.makedirs("/Users/vaibhav/Documents/Newproject/out/queue_v2/oct_causes", exist_ok=True)
json.dump(out, open(f"/Users/vaibhav/Documents/Newproject/out/queue_v2/oct_causes/{day}.json", "w"), indent=1)
print(day, n_snap, {c: (v["snapshots"], round(v["touch_price_miss_pct"], 2)) for c, v in out["by_cause"].items()}, flush=True)
