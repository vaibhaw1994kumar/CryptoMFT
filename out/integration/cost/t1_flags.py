"""Step G, T1 addition (a run wrapper): survival and cliff-flag frequency per venue, for the gap run length 2 / 3 / 5 bps (dq_gap_min_bps = 3 is ASSUMED). usage: t1_flags.py NAME [NAME ...]  (npz of t1/, with the flag2 / flag5 columns: the capture runs)"""
import json, sys
from pathlib import Path
import numpy as np
T = Path("/Users/vaibhav/Documents/Newproject/out/integration/cost/t1")
res = {"label": "EXPLORATORY PROXY; dq_gap_min_bps = 3 ASSUMED, 2 and 5 are the sensitivity", "sets": {}}
for n in sys.argv[1:]:
    z = np.load(T / f"{n}.npz")
    s, f = z["s"], z["f"]
    row = {"samples": int(len(s)), "survival_mean_percentiles": {str(p): float(np.percentile(s, p)) for p in (1, 5, 25, 50, 75, 95)}, "survival_below_0.9_share": float((s < 0.9).mean()), "flag_share": {}}
    for nm, col in (("2bps", "flag2"), ("3bps", "flag"), ("5bps", "flag5")):
        fl = z[col] > 0.5
        row["flag_share"][nm] = {"overall": float(fl.mean()), "by_f": {str(x): float(fl[np.isclose(f, x)].mean()) for x in sorted(set(np.round(f, 4)))}, "mean_realised_cost_flagged": float(z["real"][fl].mean()) if fl.any() else None,
                                 "mean_realised_cost_unflagged": float(z["real"][~fl].mean())}
    res["sets"][n] = row
    print(n, {k: (round(v, 4) if isinstance(v, float) else v) for k, v in row.items() if k in ("samples", "survival_below_0.9_share")}, row["survival_mean_percentiles"])
    for nm, r in row["flag_share"].items():
        print("  ", nm, "flag share %.4f" % r["overall"], {k: round(v, 4) for k, v in r["by_f"].items()}, "cost flagged", None if r["mean_realised_cost_flagged"] is None else round(r["mean_realised_cost_flagged"], 3), "unflagged", round(r["mean_realised_cost_unflagged"], 3))
(T / "flags.json").write_text(json.dumps(res, indent=1))
