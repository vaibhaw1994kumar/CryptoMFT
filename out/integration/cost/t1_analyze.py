"""Step G, T1 analysis (a run wrapper; rules G_RULES.md): prediction error of the hedge cost with displayed depth (P0), effective depth with the measured survival and x0.8 / x1.2 (P1), the decisions that flip, the cliff flag.
usage: t1_analyze.py GROUP NAME [NAME ...]   (npz names under out/integration/cost/t1, e.g. march_2024-03-01 ...)   ->  t1/result_GROUP.json"""
import json, sys
from pathlib import Path
import numpy as np
def _flag(name, default=None, has_value=True):
    if name in sys.argv:
        i = sys.argv.index(name)
        if not has_value:
            del sys.argv[i]
            return True
        v = sys.argv[i + 1]
        del sys.argv[i:i + 2]
        return v
    return default


T = Path("/Users/vaibhav/Documents/Newproject/out/integration/cost") / _flag("--dir", "t1")      # --dir t1b: the follow-up runs (FOLLOWUP_RULES.md)
EXTRA = _flag("--extra", False, False)                                                         # also the follow-up sections: per-fraction unfillable share, book updates between decision and arrival, flags, survival, cancel-to-trade, volatile vs normal blocks, repeated-hedge sequences
VOL_HALF = _flag("--vol-half", False, False)                                                   # one window split in time: the first half of its 10-minute blocks fixes the volatile threshold, the table is read on the second half only
VOL_FROM = _flag("--vol-from")                                                                 # comma list of window names whose 10-minute blocks fix the volatile threshold (75th percentile); default: the windows of this group itself
def do_sequences(names, T, REPS, rng, X):
    sq = [(i, T / f"{n}_seq.npz") for i, n in enumerate(names) if (T / f"{n}_seq.npz").exists()]
    if sq:
        parts = {k: [] for k in np.load(sq[0][1]).files}
        sb = []
        for i, fp in sq:
            z = np.load(fp)
            for k in parts:
                parts[k].append(z[k])
            sb.append(np.floor((z["t0"] - z["t0"].min()) / 600.0).astype(int) + 1000 * i)
        Q = {k: np.concatenate(v) for k, v in parts.items()}
        QB = np.concatenate(sb)
        def bci(x, b):
            ub = np.unique(b); ix = [np.flatnonzero(b == u) for u in ub]
            v = []
            for _ in range(REPS):
                pick = np.concatenate([ix[j] for j in rng.integers(0, len(ub), len(ub))])
                v.append(np.nanmean(x[pick]) if np.isfinite(x[pick]).any() else np.nan)
            return [float(np.nanpercentile(v, 2.5)), float(np.nanpercentile(v, 97.5))]
        seq = {}
        for fq in sorted(set(Q["f"])):
            for sp in sorted(set(Q["s"])):
                m = (Q["f"] == fq) & (Q["s"] == sp)
                cell = {"hedges": int(m.sum()), "total_size_over_D10": float(fq * 10)}
                for var in [k[5:] for k in Q if k.startswith("cost_")]:
                    c = Q[f"cost_{var}"][m]
                    cell[var] = {"mean_cost_bps_per_unit": [float(np.nanmean(c)), *bci(c, QB[m])], "share_unfillable": float(np.isnan(c).mean()),
                                 "by_k": {str(int(kk)): float(np.nanmean(c[Q["k"][m] == kk])) for kk in sorted(set(Q["k"]))}}
                cell["refill_minus_off"] = [float(np.nanmean(Q["cost_refill"][m] - Q["cost_off"][m])), *bci(Q["cost_refill"][m] - Q["cost_off"][m], QB[m])]
                for ov_ in [k[5:] for k in Q if k.startswith("cost_") and "_ov" in k]:                       # overlay variants minus their own-consumption base (MODELLED)
                    base_ = ov_.split("_")[0]
                    cell[f"{ov_}_minus_{base_}"] = [float(np.nanmean(Q[f"cost_{ov_}"][m] - Q[f"cost_{base_}"][m])), *bci(Q[f"cost_{ov_}"][m] - Q[f"cost_{base_}"][m], QB[m])]
                seq[f"f={fq:g} s={sp:g}"] = cell
        X["sequences"] = {"note": "K = 10 hedges of f x D10 each, every s seconds; cost = side-signed VWAP of the walk 0.1 s after each decision against the UNMODIFIED touch at that decision; own consumption is a virtual offset (the live book is never changed); refill tau 3.5 s", "cells": seq}
    return X

group, names = sys.argv[1], sys.argv[2:]
REPS, BLOCK_S = 2000, 600.0
rng = np.random.default_rng(1)
if "--seq-only" in sys.argv:
    sys.argv.remove("--seq-only")
    group, names = sys.argv[1], sys.argv[2:]
    X = do_sequences(names, T, REPS, rng, {"group": group, "windows": names})
    (T / f"extra_{group}.json").write_text(json.dumps(X, indent=1))
    print(json.dumps({k: v for k, v in X["sequences"]["cells"].items() if k.endswith("s=1") or k.endswith("s=10")}, indent=0)[:3000])
    sys.exit(0)
cols = {}
blocks = []
for i, n in enumerate(names):
    z = np.load(T / f"{n}.npz")
    for k in z.files:
        if k != "counts":
            cols.setdefault(k, []).append(z[k])
    ts = z["ts"]
    blocks.append(np.floor((ts - ts.min()) / BLOCK_S).astype(int) + 1000 * i)
C = {k: np.concatenate(v) for k, v in cols.items()}
B = np.concatenate(blocks)
real, f = C["real"], C["f"]
preds = {"P0_displayed": C["p_disp"], "P1_s_x0.8": C["p_eff08"], "P1_s_measured": C["p_eff10"], "P1_s_x1.2": C["p_eff12"]}
ok = np.ones(len(real), bool)
for p in preds.values():
    ok &= np.isfinite(p)
n_total = len(real)
R = {k: v[ok] for k, v in C.items()}
real, f, Bk = real[ok], f[ok], B[ok]
preds = {k: v[ok] for k, v in preds.items()}
ub = np.unique(Bk)
idx = [np.flatnonzero(Bk == b) for b in ub]


def boot(stat):
    """95% interval of stat(sample indices) over blocks."""
    out = []
    for _ in range(REPS):
        pick = np.concatenate([idx[i] for i in rng.integers(0, len(ub), len(ub))])
        out.append(stat(pick))
    return [float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))]


res = {"group": group, "windows": names, "rows_total": int(n_total), "rows_all_predictions_defined": int(ok.sum()), "label": "EXPLORATORY PROXY: hypothetical market-style orders on Bybit, outcome = a walk of the book 0.1 s later against the touch at the decision"}
err = {k: v - real for k, v in preds.items()}
res["mean_realised_cost_bps_by_f"] = {str(x): float(real[f == x].mean()) for x in sorted(set(f))}
res["mae_bps"] = {k: {"pooled": float(np.abs(e).mean()), "pooled_ci95": boot(lambda p, e=e: float(np.abs(e[p]).mean())), "mean_signed_error": float(e.mean()), "by_f": {str(x): float(np.abs(e[f == x]).mean()) for x in sorted(set(f))}} for k, e in err.items()}
diffs = {}
for a, b in (("P1_s_measured", "P0_displayed"), ("P1_s_x0.8", "P1_s_measured"), ("P1_s_x1.2", "P1_s_measured"), ("P1_s_x0.8", "P1_s_x1.2")):
    d = lambda p, a=a, b=b: float(np.abs(err[a][p]).mean() - np.abs(err[b][p]).mean())
    diffs[f"{a} minus {b}"] = {"mean": d(np.arange(len(real))), "ci95": boot(d)}
    diffs[f"{a} minus {b}"]["by_f"] = {str(x): float(np.abs(err[a][f == x]).mean() - np.abs(err[b][f == x]).mean()) for x in sorted(set(f))}
res["mae_difference_bps"] = diffs
# decisions
TH = 2.0
allow = {k: v <= TH for k, v in preds.items()}
dec = {"market_allowed_when_predicted_cost_le_2bps": {k: float(a.mean()) for k, a in allow.items()}}
flip = lambda a, b: float((a != b).mean())
dec["flip_share_market_allowed"] = {"P1 x0.8 vs x1.2": flip(allow["P1_s_x0.8"], allow["P1_s_x1.2"]), "P1 x0.8 vs measured": flip(allow["P1_s_x0.8"], allow["P1_s_measured"]), "P1 x1.2 vs measured": flip(allow["P1_s_x1.2"], allow["P1_s_measured"]),
                                    "P1 measured vs P0": flip(allow["P1_s_measured"], allow["P0_displayed"])}
cv = {"x0.8": R["cover08"] > 0.5, "measured": R["cover10"] > 0.5, "x1.2": R["cover12"] > 0.5}
dec["cr_lq_covers_share"] = {k: float(v.mean()) for k, v in cv.items()}
dec["flip_share_cr_lq"] = {"x0.8 vs x1.2": flip(cv["x0.8"], cv["x1.2"]), "x0.8 vs measured": flip(cv["x0.8"], cv["measured"]), "x1.2 vs measured": flip(cv["x1.2"], cv["measured"])}
# the realised outcome of a decision that flipped: when x0.8 and x1.2 disagree on "market allowed", what did the order cost?
dis = allow["P1_s_x0.8"] != allow["P1_s_x1.2"]
dec["cost_when_the_two_survival_scalings_disagree"] = {"n": int(dis.sum()), "mean_realised_cost_bps": float(real[dis].mean()) if dis.any() else None, "mean_realised_cost_when_they_agree": float(real[~dis].mean())}
res["decisions"] = dec
# the cliff flag
fl = R["flag"] > 0.5
e1 = np.abs(err["P1_s_measured"])
flag = {"flagged_share": float(fl.mean()), "n_flagged": int(fl.sum())}
if fl.any():
    flag.update({"mean_realised_cost_flagged": float(real[fl].mean()), "mean_realised_cost_unflagged": float(real[~fl].mean()), "mae_P1_flagged": float(e1[fl].mean()), "mae_P1_unflagged": float(e1[~fl].mean()),
                 "mae_P1_all": float(e1.mean()), "share_of_total_absolute_error_in_flagged": float(e1[fl].sum() / e1.sum()),
                 "mae_P1_all_minus_unflagged_ci95": boot(lambda p: float(e1[p].mean() - e1[p][~fl[p]].mean()) if (~fl[p]).any() else 0.0)})
res["cliff_flag"] = flag
(T / f"result_{group}.json").write_text(json.dumps(res, indent=1))
print(json.dumps({k: res[k] for k in ("rows_total", "rows_all_predictions_defined", "mean_realised_cost_bps_by_f")}, indent=0))
print("MAE", {k: round(v["pooled"], 4) for k, v in res["mae_bps"].items()}, "signed", {k: round(v["mean_signed_error"], 3) for k, v in res["mae_bps"].items()})
for k, v in diffs.items():
    print(" ", k, round(v["mean"], 4), [round(x, 4) for x in v["ci95"]])
print(json.dumps(res["decisions"], indent=0))
print(json.dumps(res["cliff_flag"], indent=0))


if EXTRA:
    # ---------------- FOLLOWUP_RULES.md 1a / 1b / 3 / 4 sections ----------------
    X = {"label": "EXPLORATORY PROXY (FOLLOWUP_RULES.md); HOLDOUT NOT READ", "group": group, "windows": names}
    fr = sorted(set(C["f"]))
    # per fraction: unfillable shares (all windows), signed errors
    dec = {x: 0 for x in fr}; unf_p = {x: 0 for x in fr}; unf_a = {x: 0 for x in fr}
    for n in names:
        zf = T / f"{n}_aux.npz"
        if zf.exists():
            z = np.load(zf)
            for i, x in enumerate(z["fracs"]):
                x = float(x)
                if x in dec:
                    dec[x] += int(z["dec"][i]); unf_p[x] += int(z["unf_pred"][i]); unf_a[x] += int(z["unf_arr"][i])
    X["by_fraction"] = {str(x): {"decisions": dec[x], "unfillable_in_predictions": unf_p[x], "unfillable_at_arrival_walk": unf_a[x], "share_unfillable": (unf_p[x] + unf_a[x]) / dec[x] if dec[x] else None,
                                 "realised_cost_bps": float(real[f == x].mean()) if (f == x).any() else None,
                                 "signed_error_bps": {k: float(e[f == x].mean()) for k, e in err.items()} if (f == x).any() else None,
                                 "mae_bps": {k: float(np.abs(e[f == x]).mean()) for k, e in err.items()} if (f == x).any() else None} for x in fr}
    # book updates between the decision and the arrival (item 4)
    nu = R["nupd"]
    X["book_updates_between_decision_and_arrival"] = {"share_none": float((nu == 0).mean()), "share_one": float((nu == 1).mean()), "share_two_or_more": float((nu >= 2).mean()), "median": float(np.median(nu)), "mean": float(nu.mean())}
    # flags at 2 / 3 / 5 bps and the 3-tick rule
    flags = {"2 bps": R["flag2"] > 0.5, "3 bps (ASSUMED)": R["flag"] > 0.5, "5 bps": R["flag5"] > 0.5, "3 ticks": R["flag3t"] > 0.5}
    X["cliff_flag"] = {k: {"share": float(v.mean()), "mean_realised_cost_flagged": float(real[v].mean()) if v.any() else None, "mean_realised_cost_unflagged": float(real[~v].mean()),
                           "mae_P1_flagged": float(e1[v].mean()) if v.any() else None, "mae_P1_unflagged": float(e1[~v].mean())} for k, v in flags.items()}
    c2t = R["c2t"][np.isfinite(R["c2t"])]
    X["survival_mean_per_sample"] = {"mean": float(R["s"].mean()), "median": float(np.median(R["s"])), "p10": float(np.percentile(R["s"], 10)), "share_below_0.9": float((R["s"] < 0.9).mean())}
    X["cancel_to_trade"] = {"n": int(len(c2t)), "mean": float(c2t.mean()) if len(c2t) else None, "median": float(np.median(c2t)) if len(c2t) else None, "p90": float(np.percentile(c2t, 90)) if len(c2t) else None}
    # volatile vs normal 10-minute blocks (realised volatility of the 1 s mid log returns)
    def block_vol(zs):
        out = {}
        for i, (n, z) in enumerate(zs):
            m = (z["f"] == z["f"].min()) & (z["side"] > 0)
            tt, mid = z["ts"][m], z["mid"][m]
            if len(tt) < 10:
                continue
            bk = np.floor((tt - tt.min()) / 600.0).astype(int)
            for b in np.unique(bk):
                mm = bk == b
                r = np.diff(np.log(mid[mm]))
                if len(r) >= 30:
                    out[(n, int(b))] = float(np.std(r) * 1e4)
        return out
    zs = [(n, dict(np.load(T / f"{n}.npz"))) for n in names]
    bv = block_vol(zs)
    if VOL_HALF:
        cut = {n: float(np.median([b for (nn, b) in bv if nn == n])) for n in names}
        ref = {k: v for k, v in bv.items() if k[1] <= cut[k[0]]}
        bv = {k: v for k, v in bv.items() if k[1] > cut[k[0]]}
    else:
        ref = bv if not VOL_FROM else block_vol([(n, dict(np.load(T / f"{n}.npz"))) for n in VOL_FROM.split(",")])
    thr = float(np.percentile(list(ref.values()), 75)) if ref else None
    X["volatility"] = {"threshold_bps_per_sqrt_1s_75th_percentile": thr, "threshold_from": VOL_FROM or ("the first half of this window's blocks (table on the second half)" if VOL_HALF else "this group's own blocks"), "blocks": len(bv), "volatile_blocks": sum(v > thr for v in bv.values()) if thr else None}
    if thr:
        # the row's block: same rule as above on the (ok-filtered) rows
        key = []
        for i, n in enumerate(names):
            pass
        rowsblk = {}
        tsR = R["ts"]
        # rebuild each row's (window, block) from its window offset: rows of window i occupy the same order as the concatenation, so use the block ids already in Bk (blocks of 600 s, + 1000 i)
        vol_of = {}
        for i, n in enumerate(names):
            for (nn, b), v in bv.items():
                if nn == n:
                    vol_of[b + 1000 * i] = v
        volatile = np.array([vol_of.get(b, np.nan) > thr for b in Bk])
        known = np.array([b in vol_of for b in Bk])
        X["volatility"]["rows_in_volatile"] = int((volatile & known).sum()); X["volatility"]["rows_in_normal"] = int((~volatile & known).sum())
        for pn in ("P0_displayed", "P1_s_measured"):
            e = np.abs(err[pn])
            ub_ = np.unique(Bk[known]); ix_ = {b: np.flatnonzero((Bk == b) & known) for b in ub_}
            def diff_stat(pick):
                a = np.concatenate([ix_[b] for b in pick]); v = volatile[a]
                return float(e[a][v].mean() - e[a][~v].mean()) if v.any() and (~v).any() else np.nan
            base = diff_stat(ub_)
            boots = [diff_stat(ub_[rng.integers(0, len(ub_), len(ub_))]) for _ in range(REPS)]
            boots = [x for x in boots if np.isfinite(x)]
            X["volatility"][pn] = {"mae_volatile": float(e[volatile & known].mean()), "mae_normal": float(e[~volatile & known].mean()), "signed_volatile": float(err[pn][volatile & known].mean()), "signed_normal": float(err[pn][~volatile & known].mean()),
                                   "mean_realised_cost_volatile": float(real[volatile & known].mean()), "mean_realised_cost_normal": float(real[~volatile & known].mean()),
                                   "mae_volatile_minus_normal": [base, float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))] if boots else None,
                                   "by_f": {str(x): {"mae_volatile": float(e[volatile & known & (f == x)].mean()), "mae_normal": float(e[~volatile & known & (f == x)].mean())} for x in fr if (volatile & known & (f == x)).any() and (~volatile & known & (f == x)).any()}}
    # repeated-hedge sequences
    do_sequences(names, T, REPS, rng, X)
    # LARGE_GAPS_RULES.md step 6: the prediction error (displayed depth) split by the one-sided trade flow of the 10 s before the decision, and by orders within 1 s after a simulated maker fill (SOL March only: the probe's fills)
    import csv, gzip, re
    CST = T.parent
    def flow_prints(name):
        name = name.replace("march_", "")
        if name.startswith("archive_"):
            _, coin, day, _h = name.split("_")
            t_, v_ = [], []
            with gzip.open(Path.home() / "Downloads" / "bybit_data" / "trades" / coin / f"{coin}{day}.csv.gz", "rt", newline="") as fh:
                for r_ in csv.DictReader(fh):
                    t_.append(float(r_["timestamp"])); v_.append(float(r_["size"]) * (1 if r_["side"] == "Buy" else -1))
            o = np.argsort(t_)
            return np.array(t_)[o], np.array(v_)[o]
        if name.startswith("capture_"):
            zc = np.load(CST / "impact/data_capture_2026-10-29.npz")
            vn = name.split("_")[1]
            return zc[f"{vn}_tr_t"], zc[f"{vn}_tr_v"] * zc[f"{vn}_tr_s"]
        if re.match(r"\d{4}-\d{2}-\d{2}$", name) and (CST / f"impact/data_{name}.npz").exists():
            zc = np.load(CST / f"impact/data_{name}.npz")
            return zc["bybit_tr_t"], zc["bybit_tr_v"] * zc["bybit_tr_s"]
        return None
    win = Bk // 1000
    imb = np.full(len(real), np.nan)
    for i, n in enumerate(names):
        fp = flow_prints(n)
        if fp is None:
            continue
        tt, sv = fp
        cs, ca = np.r_[0.0, np.cumsum(sv)], np.r_[0.0, np.cumsum(np.abs(sv))]
        m = win == i
        t0_, t1_ = np.searchsorted(tt, R["ts"][m] - 10.0, "right"), np.searchsorted(tt, R["ts"][m], "right")
        tot = ca[t1_] - ca[t0_]
        imb[m] = np.where(tot > 0, (cs[t1_] - cs[t0_]) / np.maximum(tot, 1e-12), np.nan) * R["side"][m]     # + = the flow runs the way the order consumes
    fills = {}
    if (CST / "t5c/times.json").exists():
        for d_, lst in json.load(open(CST / "t5c/times.json")).items():
            fills[d_] = np.array(sorted(x[0] for x in lst))
    post = np.full(len(real), np.nan)
    for i, n in enumerate(names):
        nn = n.replace("march_", "")
        if nn in fills and len(fills[nn]):
            m = win == i
            ft = fills[nn]
            j = np.searchsorted(ft, R["ts"][m], "right") - 1
            post[m] = np.where(j >= 0, (R["ts"][m] - ft[np.maximum(j, 0)] < 1.0).astype(float), 0.0)
    SP = {"note": "one-sided flow = |signed volume of the previous 10 s prints of the venue| / total >= 0.6; aligned = the flow runs the way the order consumes (a buy order and net buying); post_fill = decision within 1 s after a SIMULATED maker fill of the probe (SOL March only; ADA, MNT and the capture have no maker fills: N/A)"}
    e0 = np.abs(err["P0_displayed"])
    def cmp_(mask, rest):
        if mask.sum() < 30 or rest.sum() < 30:
            return None
        ub_ = np.unique(Bk[mask | rest]); ix_ = {b: np.flatnonzero((Bk == b) & (mask | rest)) for b in ub_}
        def st(pick):
            a = np.concatenate([ix_[b] for b in pick]); mm = mask[a]
            return float(e0[a][mm].mean() - e0[a][~mm].mean()) if mm.any() and (~mm).any() else np.nan
        base = st(ub_)
        bs = [x for x in (st(ub_[rng.integers(0, len(ub_), len(ub_))]) for _ in range(REPS)) if np.isfinite(x)]
        return {"n": int(mask.sum()), "n_rest": int(rest.sum()), "mae": float(e0[mask].mean()), "mae_rest": float(e0[rest].mean()), "signed": float(err["P0_displayed"][mask].mean()), "signed_rest": float(err["P0_displayed"][rest].mean()),
                "realised_cost": float(real[mask].mean()), "realised_cost_rest": float(real[rest].mean()), "mae_minus_rest": [base, float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))] if bs else None}
    if np.isfinite(imb).any():
        known = np.isfinite(imb)
        SP["flow_aligned_one_sided"] = cmp_(known & (imb >= 0.6), known & (imb < 0.6) & (imb > -0.6))
        SP["flow_opposite_one_sided"] = cmp_(known & (imb <= -0.6), known & (imb < 0.6) & (imb > -0.6))
        SP["flow_any_one_sided"] = cmp_(known & (np.abs(imb) >= 0.6), known & (np.abs(imb) < 0.6))
    else:
        SP["flow"] = "N/A: no print source for these windows"
    if np.isfinite(post).any():
        known = np.isfinite(post)
        SP["post_fill_1s_all_orders"] = cmp_(known & (post > 0.5), known & (post < 0.5))
        sell = R["side"] < 0
        SP["post_fill_1s_sell_orders"] = cmp_(known & (post > 0.5) & sell, known & (post < 0.5) & sell)
    else:
        SP["post_fill_1s"] = "N/A: no maker fills (SOL March probe only)"
    X["splits_flow_and_post_fill"] = SP
    (T / f"extra_{group}.json").write_text(json.dumps(X, indent=1))
    print("EXTRA", json.dumps({k: X[k] for k in ("book_updates_between_decision_and_arrival", "cliff_flag", "survival_mean_per_sample", "cancel_to_trade", "volatility")}, indent=0)[:6000])
    print("by fraction", json.dumps(X["by_fraction"], indent=0)[:3000])
