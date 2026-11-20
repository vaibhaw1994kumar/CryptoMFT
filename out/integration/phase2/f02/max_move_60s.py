"""Phase 2 F02c (ASSUMED stress input): the largest 60 s trade-price move per venue on the 29 Oct capture, in bps (max over windows of (max - min) / min). Run wrapper; reads only."""
import glob, gzip, json, sys
from collections import deque
root = "/Users/vaibhav/Documents/Newproject/data/live_sessions/solusdt/2026-10-29/SOLUSDT/e9b81728a3054812a48035a1b3e02d42"
W = 60_000_000_000
state = {}
for path in sorted(glob.glob(root + "/part-*/trades.jsonl.gz")):
    with gzip.open(path, "rt") as f:
        for line in f:
            r = json.loads(line)
            v = r.get("venue") or ("BINANCE" if "binance" in r.get("adapter_version", "") else "BYBIT")
            t, p = int(r["exchange_ts_ns"]), float(r["price"])
            s = state.setdefault(v, {"mx": deque(), "mn": deque(), "best": 0.0, "at": None, "n": 0})
            s["n"] += 1
            for dq, cmp in ((s["mx"], lambda a, b: a <= b), (s["mn"], lambda a, b: a >= b)):
                while dq and cmp(dq[-1][1], p):
                    dq.pop()
                dq.append((t, p))
                while dq[0][0] < t - W:
                    dq.popleft()
            hi, lo = s["mx"][0][1], s["mn"][0][1]
            bps = (hi - lo) / lo * 1e4
            if bps > s["best"]:
                s["best"], s["at"] = bps, t
out = {v: {"max_60s_move_bps": round(s["best"], 2), "at_ns": s["at"], "trades": s["n"]} for v, s in state.items()}
json.dump(out, open("/Users/vaibhav/Documents/Newproject/out/integration/phase2/f02/max_move_60s.json", "w"), indent=1)
print(out)
