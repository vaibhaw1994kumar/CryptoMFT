"""Step G, T1 addition (a run wrapper): the cost curve cost_bps = a + b (Q / D10)^delta fitted on the T1 outcomes (Q / D10 is the size fraction f), with block-bootstrap 95% intervals (10-minute blocks, 2,000 resamples).
Per group of windows; the sufficient statistics per (block, f) make the bootstrap cheap. usage: t1_fit.py GROUP NAME [NAME ...]  ->  t1/fit_GROUP.json"""
import json, sys
from pathlib import Path
import numpy as np
def _flag(name, default):
    if name in sys.argv:
        i = sys.argv.index(name); v = sys.argv[i + 1]; del sys.argv[i:i + 2]; return v
    return default


T = Path("/Users/vaibhav/Documents/Newproject/out/integration/cost") / _flag("--dir", "t1")          # --dir t1b and --fracs 0.25,0.5,1,2,4 for the follow-up runs
FR = tuple(float(x) for x in _flag("--fracs", "0.05,0.1,0.25,0.5,1.0").split(","))
group, names = sys.argv[1], sys.argv[2:]
DELTAS = np.round(np.arange(0.2, 1.5001, 0.01), 2)
rng = np.random.default_rng(1)
rows = []
for i, n in enumerate(names):
    z = np.load(T / f"{n}.npz")
    blk = np.floor((z["ts"] - z["ts"].min()) / 600.0).astype(int) + 1000 * i
    for j, f in enumerate(FR):
        m = np.isclose(z["f"], f)
        for b in np.unique(blk[m]):
            r = z["real"][m & (blk == b)]
            rows.append((b, j, len(r), r.sum()))
agg = np.array(rows)
blocks = np.unique(agg[:, 0])
by_block = [agg[agg[:, 0] == b] for b in blocks]
x_f = np.array(FR)


def fit(ns, sums):
    """ns, sums: per f counts and sums. Weighted least squares over the five fraction means for each delta; returns the best (a, b, delta)."""
    means = np.where(ns > 0, sums / np.maximum(ns, 1), 0.0)
    best = None
    for dl in DELTAS:
        x = x_f ** dl
        W = ns
        Sx, Sy, Sxx, Sxy, Sw = (W * x).sum(), (W * means).sum(), (W * x * x).sum(), (W * x * means).sum(), W.sum()
        det = Sw * Sxx - Sx * Sx
        if det <= 0:
            continue
        b = (Sw * Sxy - Sx * Sy) / det
        a = (Sy - b * Sx) / Sw
        if b < 0:
            b, a = 0.0, Sy / Sw
        sse = float((W * (means - a - b * x) ** 2).sum())
        if best is None or sse < best[0]:
            best = (sse, float(a), float(b), float(dl))
    return best[1:]


def totals(blks):
    ns, sums = np.zeros(len(FR)), np.zeros(len(FR))
    for bl in blks:
        for _, j, n, s in bl:
            ns[int(j)] += n; sums[int(j)] += s
    return ns, sums


ns, sums = totals(by_block)
a, b, dl = fit(ns, sums)
boots = np.array([fit(*totals([by_block[i] for i in rng.integers(0, len(by_block), len(by_block))])) for _ in range(500)])
res = {"group": group, "windows": names, "n": int(ns.sum()), "fit": {"a_bps": a, "b_bps": b, "delta": dl}, "ci95": {k: [float(np.percentile(boots[:, j], 2.5)), float(np.percentile(boots[:, j], 97.5))] for j, k in enumerate(("a_bps", "b_bps", "delta"))},
       "mean_cost_by_f": {str(f): float(s / n) for f, n, s in zip(FR, ns, sums)}, "label": "EXPLORATORY PROXY: 500 block-bootstrap resamples", "form": "cost_bps = a + b * (Q / D10) ** delta"}
(T / f"fit_{group}.json").write_text(json.dumps(res, indent=1))
print(json.dumps({k: res[k] for k in ("group", "n", "fit", "ci95", "mean_cost_by_f")}, indent=0))
