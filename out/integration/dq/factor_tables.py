"""Depth-quality factor study, step 2 (a run wrapper): the tables and the keep rule of FACTOR_STUDY_RULES.md applied to factor_rows.jsonl. Nothing is fitted; every number is fixed in the rules file.
EXPLORATORY, ONE DAY, PROXY. usage: factor_tables.py STUDY_DIR"""
import json
import sys
from pathlib import Path

import numpy as np

D = Path(sys.argv[1])
meta = json.loads((D / "factor_meta.json").read_text())
rows = [json.loads(l) for l in open(D / "factor_rows.jsonl")]
T0, T1 = meta["first_ts"], meta["last_ts"]
SPLIT = (T0 + T1) / 2.0
MIN_N, MIN_EFFECT, BLOCK_S, RESAMPLES = 200, 0.3, 300.0, 2000
rng = np.random.default_rng(1)


def half(r):
    return "train" if r["ts"] < SPLIT else "holdout"


def stats_of(vals):
    return {"n": len(vals), "mean": float(np.mean(vals)) if vals else None}


def block_ci(hi_rows, lo_rows, key):
    """95% interval of mean(hi) - mean(lo) from a block bootstrap over BLOCK_S blocks (the 1 s samples overlap in time)."""
    blocks = {}
    for tag, rs in (("hi", hi_rows), ("lo", lo_rows)):
        for r in rs:
            b = blocks.setdefault(int((r["ts"] - T0) // BLOCK_S), {"hi": [0.0, 0], "lo": [0.0, 0]})
            b[tag][0] += r[key]
            b[tag][1] += 1
    ids = list(blocks)
    arr = np.array([[blocks[i]["hi"][0], blocks[i]["hi"][1], blocks[i]["lo"][0], blocks[i]["lo"][1]] for i in ids])
    out = []
    for _ in range(RESAMPLES):
        pick = arr[rng.integers(0, len(arr), len(arr))]
        s = pick.sum(axis=0)
        if s[1] > 0 and s[3] > 0:
            out.append(s[0] / s[1] - s[2] / s[3])
    return [float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))] if out else [None, None]


def judge(buckets_by_half, order, direction, key, label):
    """buckets_by_half: half -> bucket -> rows. order: buckets from the least to the most risky. direction +1: more risk = higher outcome (slippage), -1: more risk = lower outcome (markout)."""
    res = {"factor": label, "order": order, "halves": {}}
    ok = True
    for h in ("train", "holdout"):
        cells = {b: [r[key] for r in buckets_by_half[h].get(b, [])] for b in order}
        res["halves"][h] = {b: stats_of(cells[b]) for b in order}
        if any(len(cells[b]) < MIN_N for b in order):
            res["halves"][h]["testable"] = False
            ok = False
            continue
        means = [float(np.mean(cells[b])) for b in order]
        mono = all(direction * (means[i + 1] - means[i]) >= 0 for i in range(len(means) - 1))
        res["halves"][h]["monotonic_in_claimed_direction"] = mono
        ok = ok and mono
    if ok:
        hi, lo = buckets_by_half["holdout"][order[-1]], buckets_by_half["holdout"][order[0]]
        diff = direction * (np.mean([r[key] for r in hi]) - np.mean([r[key] for r in lo]))
        ci = block_ci(hi, lo, key)
        ci = [direction * ci[0], direction * ci[1]] if direction > 0 else [direction * ci[1], direction * ci[0]]
        res.update(holdout_extreme_diff_bps_in_claimed_direction=float(diff), holdout_ci95=ci)
        res["KEPT"] = bool(diff >= MIN_EFFECT and ci[0] is not None and ci[0] > 0)
    else:
        res["KEPT"] = False
        res["why_not"] = "not testable (a bucket under %d samples in a half) or not monotonic in both halves" % MIN_N
    return res


def by_half(rs, bucket_of):
    out = {"train": {}, "holdout": {}}
    for r in rs:
        b = bucket_of(r)
        if b is not None:
            out[half(r)].setdefault(b, []).append(r)
    return out


def flicker_edges(rs):
    pos = [r["flicker"] for r in rs if half(r) == "train" and r["flicker"] > 0]
    return float(np.median(pos)) if pos else None


def flicker_bucket(edge):
    def f(r):
        if r["flicker"] == 0:
            return "F0"
        return "F1" if edge is not None and r["flicker"] <= edge else "F2"
    return f


report = {"label": meta["label"], "split_ts": SPLIT, "train_hours": (SPLIT - T0) / 3600, "holdout_hours": (T1 - SPLIT) / 3600, "rules": "FACTOR_STUDY_RULES.md", "counts": meta["counts"], "tables": []}
slip = [r for r in rows if r["kind"] == "slip"]
mk = [r for r in rows if r["kind"] == "markout"]

for q in meta["sizes"]:
    rs = [r for r in slip if r["q"] == q]
    t1 = judge(by_half(rs, lambda r: "blocks" if (r["status"] == "GAP" and r["gap_depth"] is not None and r["gap_depth"] < q) else "clear"), ["clear", "blocks"], +1, "ds", f"hedge slippage change, Q={q:g}: gap_blocks")
    t2 = judge(by_half(rs, flicker_bucket(flicker_edges(rs))), ["F0", "F1", "F2"], +1, "ds", f"hedge slippage change, Q={q:g}: flicker")
    t1["outcome"] = t2["outcome"] = f"dS(Q={q:g}) = S(t+0.2s) - S(t), bps, positive = worse"
    report["tables"] += [t1, t2]

gap_depths = [r["gap_depth"] for r in mk if half(r) == "train" and r["status"] == "GAP" and r["gap_depth"] is not None]
med = float(np.median(gap_depths)) if gap_depths else None


def gap_bucket(r):
    if r["status"] == "NO_GAP_WITHIN_CAPTURE":
        return "NONE"
    if r["status"] == "GAP" and r["gap_depth"] is not None and med is not None:
        return "FAR" if r["gap_depth"] >= med else "NEAR"
    return None


t3 = judge(by_half(mk, gap_bucket), ["NONE", "FAR", "NEAR"], -1, "markout_bps", "maker markout +5 s at the touch: own-side depth_to_first_gap")
t4 = judge(by_half(mk, flicker_bucket(flicker_edges(mk))), ["F0", "F1", "F2"], -1, "markout_bps", "maker markout +5 s at the touch: own-side flicker")
t3["outcome"] = t4["outcome"] = "markout of the passive side at +5 s, bps, positive = favourable (proxy: public prints at the touch)"
t3["train_median_gap_depth"] = med
report["tables"] += [t3, t4]
(D / "factor_tables.json").write_text(json.dumps(report, indent=1))

lines = [f"# Depth-quality factors: tables ({report['label']})", "",
         f"Train {report['train_hours']:.1f} h, holdout {report['holdout_hours']:.1f} h (chronological halves of one day). Keep rule in FACTOR_STUDY_RULES.md; fixed before the run.", ""]
for t in report["tables"]:
    lines += [f"## {t['factor']}  ->  {'KEPT' if t['KEPT'] else 'NOT KEPT'}", f"Outcome: {t['outcome']}", "", "| bucket | train n | train mean | holdout n | holdout mean |", "|---|---|---|---|---|"]
    for b in t["order"]:
        tr, ho = t["halves"]["train"].get(b, {}), t["halves"]["holdout"].get(b, {})
        f = lambda s: "-" if s.get("mean") is None else f"{s['mean']:.3f}"
        lines.append(f"| {b} | {tr.get('n', 0)} | {f(tr)} | {ho.get('n', 0)} | {f(ho)} |")
    if "holdout_extreme_diff_bps_in_claimed_direction" in t:
        lines.append(f"\nHoldout extreme-bucket difference in the claimed direction: {t['holdout_extreme_diff_bps_in_claimed_direction']:.3f} bps, 95% CI {t['holdout_ci95'][0]:.3f} to {t['holdout_ci95'][1]:.3f}.")
    else:
        lines.append(f"\n{t.get('why_not', '')}")
    lines.append("")
(D / "factor_tables.md").write_text("\n".join(lines))
print("\n".join(lines))
