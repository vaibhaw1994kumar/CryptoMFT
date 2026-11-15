"""T5 helper (feed only): sigma_1s = the standard deviation of 1 s Binance mid returns, in bps, from the BBO series of the given days (TRAIN)."""
import json, sys
import numpy as np
sys.argv = [sys.argv[0], "x"] + sys.argv[1:]
exec(open("/Users/vaibhav/Documents/Newproject/out/integration/cost/t5_split.py").read().split("if mode == \"ratios\":")[0])
res = {}
for day in sys.argv[2:]:
    if day == "x":
        continue
    t0, bbo, trd = load(day)
    g = np.arange(t0 + 120, t0 + 7200, 1.0)
    idx = np.maximum(np.searchsorted(bbo["bt"], g, side="right") - 1, 0)
    mid = 0.5 * (bbo["bb"][idx] + bbo["ba"][idx])
    res[day] = float(np.std(np.diff(mid) / mid[:-1] * 1e4))
    print(day, round(res[day], 3), flush=True)
json.dump(res, open("/Users/vaibhav/Documents/Newproject/out/integration/cost/t5/sigma_1s.json", "w"), indent=1)
print("pooled mean", np.mean(list(res.values())))
