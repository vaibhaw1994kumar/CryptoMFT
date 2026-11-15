"""Step E/F input (a run wrapper, feed only, READ ONLY): samples of the outflow ratio of a resting limit order's queue, from the public prints of the 2026-10-29 capture. For each second and each side: the volume of the
prints that deplete that side over the NEXT T = 2 s (a sell aggressor hits the bids), divided by the volume that depleted it over the PREVIOUS 10 s (never below 1 SOL, the same floor the evidence uses). Any aggressor volume on the side
drains the touch queue before it reaches deeper levels, so it is the outflow of a queue at the touch (cancels at the touch are not counted: conservative for a limit order). PROXY, EXPLORATORY.
usage: outflow_samples.py VENUE OUT_JSON"""
import json, sys
import numpy as np
venue, out = sys.argv[1], sys.argv[2]
z = np.load("/Users/vaibhav/Documents/Newproject/out/integration/cost/impact/data_capture_2026-10-29.npz")
t, v, s = z[f"{venue}_tr_t"], z[f"{venue}_tr_v"], z[f"{venue}_tr_s"]
T, W, FLOOR = 2.0, 10.0, 1.0
grid = np.arange(t[0] + W, t[-1] - T, 1.0)
ratios = []
for sign in (1, -1):
    m = s == sign
    tt, vv = t[m], v[m]
    cs = np.r_[0.0, np.cumsum(vv)]
    f = lambda a, b: cs[np.searchsorted(tt, b, "right")] - cs[np.searchsorted(tt, a, "right")]
    prev, nxt = f(grid - W, grid), f(grid, grid + T)
    ratios.append(nxt / np.maximum(prev, FLOOR))
r = np.concatenate(ratios)
r = np.sort(r)[:: max(1, len(r) // 5000)]
json.dump([float(x) for x in r], open(out, "w"))
print(venue, "samples", len(r), "quantiles 10/50/90/99:", [round(float(np.percentile(r, p)), 3) for p in (10, 50, 90, 99)])
