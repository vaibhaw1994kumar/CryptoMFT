"""Impact fits step 2 (a run wrapper; rules in IMPACT_FIT_RULES.md, written before any number): aggressor runs, peaks, residuals, cross moves, the two curve forms, selection on SELECT, one look at the HOLDOUT.
PROXY (public prints, no trader ids), EXPLORATORY. usage: impact_fit.py [--no-holdout]"""
import json, sys
from pathlib import Path
import numpy as np
D = Path("/Users/vaibhav/Documents/Newproject/out/integration/cost/impact")
TRAIN, SELECT, HOLD = ["2024-03-01"], ["2024-03-08", "2024-03-11"], "capture_2026-10-29"
DTS, KS, KIDX = (0.1, 0.5, 1.0), (2.0, 5.0, 10.0), {2.0: 0, 5.0: 1, 10.0: 2}
DELTAS = np.round(np.arange(0.2, 1.5001, 0.01), 2)
HORIZONS = (1.0, 10.0, 60.0)
ISO_W, ISO_FRAC, MIN_SIZE, BLOCK_S, REPS = 60.0, 0.75, 1.0, 600.0, 2000
rng = np.random.default_rng(1)
no_holdout = "--no-holdout" in sys.argv


def load(name):
    z = np.load(D / f"data_{name}.npz")
    out = {}
    for v in ("bybit", "binance"):
        out[v] = {k: z[f"{v}_{k}"] for k in ("t", "mid", "dbid", "dask", "tr_t", "tr_p", "tr_v", "tr_s")}
    hours = (max(out["bybit"]["t"][-1], out["binance"]["t"][-1]) - min(out["bybit"]["t"][0], out["binance"]["t"][0])) / 3600
    return out, hours


def gi(t_grid, t):
    return np.clip(np.searchsorted(t_grid, t, side="right") - 1, 0, len(t_grid) - 1)


def runs_of(win, hours, venue, other, dt, name):
    d, o = win[venue], win[other]
    t, v, s, p = d["tr_t"], d["tr_v"], d["tr_s"], d["tr_p"]
    if len(t) < 2:
        return None
    brk = np.r_[True, (np.diff(t) >= dt) | (s[1:] != s[:-1])]
    gid = np.cumsum(brk) - 1
    first = np.flatnonzero(brk)
    last = np.r_[first[1:] - 1, len(t) - 1]
    vol = np.add.reduceat(v, first)
    keep = vol >= MIN_SIZE
    first, last, vol = first[keep], last[keep], vol[keep]
    sign = s[first].astype(float)
    ts, te = t[first], t[last]
    g, gm = d["t"], d["mid"]
    i0 = gi(g, ts)
    m0 = gm[i0]
    def mid_after(tt, base_idx=None):
        return gm[np.minimum(gi(g, tt) + 1, len(g) - 1)]
    m_end = mid_after(te)
    peak = sign * (m_end - m0) / m0 * 1e4
    res = {h: sign * (mid_after(te + h) - m0) / m0 * 1e4 for h in HORIZONS}
    # other venue over the same intervals
    og, om = o["t"], o["mid"]
    o0 = om[gi(og, ts)]
    oend = om[np.minimum(gi(og, te) + 1, len(og) - 1)]
    cross = {0.0: sign * (oend - o0) / o0 * 1e4}
    for h in HORIZONS:
        cross[h] = sign * (om[np.minimum(gi(og, te + h) + 1, len(og) - 1)] - o0) / o0 * 1e4
    # depth at start on the side the run consumes (buy aggressor consumes asks)
    dep = {k: np.where(sign > 0, d["dask"][i0, KIDX[k]], d["dbid"][i0, KIDX[k]]) for k in KS}
    # isolation: net signed volume of the venue's prints in [ts - W, te + W]
    cs = np.r_[0.0, np.cumsum(v * s)]
    a, b = np.searchsorted(t, ts - ISO_W, "left"), np.searchsorted(t, te + ISO_W, "right")
    net = cs[b] - cs[a]
    iso = (net * sign > 0) & (vol >= ISO_FRAC * np.abs(net))
    # path: mid when the cumulative run volume first reaches r of the run
    cv = np.cumsum(v)
    base = np.r_[0.0, cv][first]
    path = {}
    for r in (0.25, 0.5, 0.75):
        idx = np.searchsorted(cv, base + r * vol, side="left")
        idx = np.minimum(idx, len(t) - 1)
        path[r] = sign * (gm[np.minimum(gi(g, t[idx]) + 1, len(g) - 1)] - m0) / m0 * 1e4
    one_min = gm[::600]
    sigma = float(np.std(np.diff(np.log(one_min[one_min > 0]))) * np.sqrt(1440) * 1e4)
    vday = float(v.sum() * 24.0 / hours)
    return dict(name=name, venue=venue, dt=dt, ts=ts, te=te, Q=vol, sign=sign, peak=peak, res=res, cross=cross, depth=dep, iso=iso, path=path, sigma=sigma, vday=vday, n=len(vol))


def cat(rs, key, sub=None):
    parts = []
    for r in rs:
        x = r[key] if sub is None else r[key][sub]
        parts.append(np.asarray(x))
    return np.concatenate(parts) if parts else np.array([])


def fit_a(Q, D, y):
    ok = np.isfinite(D) & (D > 0)
    Q, D, y = Q[ok], D[ok], y[ok]
    best = None
    for dl in DELTAS:
        x = (Q / D) ** dl
        A = np.c_[np.ones_like(x), x]
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
        if coef[1] < 0:
            coef = np.array([y.mean(), 0.0])
        sse = float(((A @ coef - y) ** 2).sum())
        if best is None or sse < best[0]:
            best = (sse, float(coef[0]), float(coef[1]), float(dl))
    return {"a": best[1], "b": best[2], "delta": best[3], "n": int(ok.sum())}


def pred_a(f, Q, D):
    return f["a"] + f["b"] * (Q / D) ** f["delta"]


def fit_b(Q, sig, V, y):
    best = None
    for dl in DELTAS:
        z = sig * (Q / V) ** dl
        Y = float((z * y).sum() / (z * z).sum())
        sse = float(((Y * z - y) ** 2).sum())
        if best is None or sse < best[0]:
            best = (sse, Y, float(dl))
    return {"Y": best[1], "delta": best[2]}


def pred_b(f, Q, sig, V):
    return f["Y"] * sig * (Q / V) ** f["delta"]


def boot_mean_diff(blocks, e1, e2):
    """95% interval of mean(e1) - mean(e2) over blocks (resampled together)."""
    ub = np.unique(blocks)
    idx = {b: np.flatnonzero(blocks == b) for b in ub}
    s1 = np.array([e1[idx[b]].sum() for b in ub]); s2 = np.array([e2[idx[b]].sum() for b in ub]); n = np.array([len(idx[b]) for b in ub])
    out = []
    for _ in range(REPS):
        pick = rng.integers(0, len(ub), len(ub))
        out.append((s1[pick].sum() - s2[pick].sum()) / max(1, n[pick].sum()))
    return [float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))]


def boot_ratio(blocks, num, den):
    ub = np.unique(blocks)
    idx = {b: np.flatnonzero(blocks == b) for b in ub}
    sn = np.array([num[idx[b]].sum() for b in ub]); sd = np.array([den[idx[b]].sum() for b in ub])
    out = []
    for _ in range(REPS):
        pick = rng.integers(0, len(ub), len(ub))
        if sd[pick].sum() != 0:
            out.append(sn[pick].sum() / sd[pick].sum())
    return [float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))] if out else [None, None]


W = {n: load(n) for n in TRAIN + SELECT}
if not no_holdout and (D / f"data_{HOLD}.npz").exists():
    W[HOLD] = load(HOLD)
report = {"label": "PROXY (public prints, no trader ids), EXPLORATORY, 3 March windows of 2 h + one out-of-period day", "rules": "IMPACT_FIT_RULES.md", "windows": {n: round(h, 2) for n, (_, h) in W.items()}}

# ---- selection of dt and k on SELECT (form a, Bybit) ----------------------------------------------------------------------------------------
sel = []
for dt in DTS:
    tr = [runs_of(W[n][0], W[n][1], "bybit", "binance", dt, n) for n in TRAIN]
    se = [runs_of(W[n][0], W[n][1], "bybit", "binance", dt, n) for n in SELECT]
    for k in KS:
        f = fit_a(cat(tr, "Q"), cat(tr, "depth", k), cat(tr, "peak"))
        Qs, Ds, ys = cat(se, "Q"), cat(se, "depth", k), cat(se, "peak")
        ok = np.isfinite(Ds) & (Ds > 0)
        mae = float(np.abs(pred_a(f, Qs[ok], Ds[ok]) - ys[ok]).mean())
        null = float(np.abs(ys[ok] - cat(tr, "peak").mean()).mean())
        sel.append({"dt": dt, "k": k, "select_mae": mae, "select_null_mae": null, "fit": f, "n_select": int(ok.sum()), "n_train": f["n"]})
sel.sort(key=lambda r: (r["select_mae"], r["dt"], r["k"]))
best = sel[0]
DT, K = best["dt"], best["k"]
report["selection"] = {"grid": sel, "chosen": {"dt": DT, "k": K}}

# ---- the runs with the chosen dt, all windows ------------------------------------------------------------------------------------------------
R = {(n, v): runs_of(W[n][0], W[n][1], v, "binance" if v == "bybit" else "bybit", DT, n) for n in W for v in ("bybit", "binance")}
tr = [R[(n, "bybit")] for n in TRAIN]
se = [R[(n, "bybit")] for n in SELECT]
fa = fit_a(cat(tr, "Q"), cat(tr, "depth", K), cat(tr, "peak"))
fb = fit_b(cat(tr, "Q"), np.repeat([r["sigma"] for r in tr], [r["n"] for r in tr]), np.repeat([r["vday"] for r in tr], [r["n"] for r in tr]), cat(tr, "peak"))
report["train_fit"] = {"form_a": fa, "form_b": fb, "train_mean_peak_bps": float(cat(tr, "peak").mean()), "runs_train": int(sum(r["n"] for r in tr)), "isolated_share": float(cat(tr, "iso").mean())}
# size bands
Qt, Dt = cat(tr, "Q"), cat(tr, "depth", K)
ok = np.isfinite(Dt) & (Dt > 0)
med = float(np.median(Qt[ok] / Dt[ok]))
bands = {}
for nm, m in (("small", ok & (Qt / np.where(Dt > 0, Dt, np.nan) <= med)), ("large", ok & (Qt / np.where(Dt > 0, Dt, np.nan) > med))):
    bands[nm] = fit_a(Qt[m], Dt[m], cat(tr, "peak")[m])
report["train_fit"]["size_band_median_Q_over_D"] = med
report["train_fit"]["form_a_by_size_band"] = bands
# Y per window (form b delta fixed at the train value)
yw = {}
for n in W:
    r = R[(n, "bybit")]
    z = r["sigma"] * (r["Q"] / r["vday"]) ** fb["delta"]
    yw[n] = float((z * r["peak"]).sum() / (z * z).sum())
report["Y_per_window"] = {"values": yw, "mean": float(np.mean(list(yw.values()))), "sd": float(np.std(list(yw.values()), ddof=1)) if len(yw) > 1 else None, "n_windows": len(yw)}


def evaluate(rs, label):
    Q, Dk, y = cat(rs, "Q"), cat(rs, "depth", K), cat(rs, "peak")
    sig = np.repeat([r["sigma"] for r in rs], [r["n"] for r in rs]); V = np.repeat([r["vday"] for r in rs], [r["n"] for r in rs])
    blocks = np.floor((cat(rs, "ts") - min(r["ts"].min() for r in rs)) / BLOCK_S).astype(int)
    ok = np.isfinite(Dk) & (Dk > 0)
    ea = np.abs(pred_a(fa, Q[ok], Dk[ok]) - y[ok]); eb = np.abs(pred_b(fb, Q[ok], sig[ok], V[ok]) - y[ok]); en = np.abs(y[ok] - report["train_fit"]["train_mean_peak_bps"])
    bl = blocks[ok]
    return {"label": label, "n": int(ok.sum()), "mae_form_a": float(ea.mean()), "mae_form_b": float(eb.mean()), "mae_null": float(en.mean()),
            "a_minus_b_ci95": boot_mean_diff(bl, ea, eb), "a_minus_null_ci95": boot_mean_diff(bl, ea, en), "b_minus_null_ci95": boot_mean_diff(bl, eb, en),
            "better": "form_a" if ea.mean() < eb.mean() else "form_b"}


report["select_eval"] = evaluate(se, "SELECT (03-08 + 03-11), Bybit, parameters from TRAIN")
# ---- path, permanent fraction, tau / beta, cross impact (TRAIN + SELECT pooled, both venues) --------------------------------------------------
pool = [R[(n, v)] for n in TRAIN + SELECT for v in ("bybit", "binance")]
def decay(rs, iso_only):
    out = {}
    for v in ("bybit", "binance"):
        sub = [r for r in rs if r["venue"] == v]
        m = [r["iso"] if iso_only else np.ones(r["n"], bool) for r in sub]
        peak = np.concatenate([r["peak"][mm] for r, mm in zip(sub, m)])
        res = {h: np.concatenate([r["res"][h][mm] for r, mm in zip(sub, m)]) for h in HORIZONS}
        blocks = np.concatenate([np.floor((r["ts"][mm] - r["ts"].min()) / BLOCK_S).astype(int) + 1000 * i for i, (r, mm) in enumerate(zip(sub, m))])
        phi = float(res[60.0].sum() / peak.sum())
        phi_ci = boot_ratio(blocks, res[60.0], peak)
        ratio = {h: float(res[h].sum() / peak.sum()) for h in HORIZONS}
        best_t = min(((float(np.sum([(phi + (1 - phi) * np.exp(-h / tau) - ratio[h]) ** 2 for h in (1.0, 10.0)])), float(tau)) for tau in np.arange(0.5, 200.01, 0.5)))[1]
        best_b = min(((float(np.sum([(phi + (1 - phi) * (1 + h) ** (-be) - ratio[h]) ** 2 for h in (1.0, 10.0)])), float(be)) for be in np.arange(0.05, 2.0001, 0.05)))[1]
        out[v] = {"n_runs": int(len(peak)), "mean_peak_bps": float(peak.mean()), "residual_over_peak": ratio, "permanent_fraction": phi, "permanent_fraction_ci95": phi_ci, "tau_s_exp": best_t, "beta_power_tau1s": best_b}
    return out
report["decay_all"], report["decay_isolated"] = decay(pool, False), decay(pool, True)
cross = {}
for v, o in (("bybit", "binance"), ("binance", "bybit")):
    sub = [r for r in pool if r["venue"] == v]
    peak = np.concatenate([r["peak"] for r in sub])
    blocks = np.concatenate([np.floor((r["ts"] - r["ts"].min()) / BLOCK_S).astype(int) + 1000 * i for i, r in enumerate(sub)])
    row = {}
    for h in (0.0,) + HORIZONS:
        oth = np.concatenate([r["cross"][h] for r in sub])
        own = peak if h == 0.0 else np.concatenate([r["res"][h] for r in sub])
        row[str(h)] = {"coefficient": float((own * oth).sum() / (own * own).sum()), "ci95": boot_ratio(blocks, own * oth, own * own)}
    cross[f"{v}->{o}"] = row
report["cross_impact"] = cross
paths = {}
for v in ("bybit", "binance"):
    sub = [r for r in pool if r["venue"] == v]
    Q, pk = np.concatenate([r["Q"] for r in sub]), np.concatenate([r["peak"] for r in sub])
    rows = {}
    for rfrac in (0.25, 0.5, 0.75):
        pr = np.concatenate([r["path"][rfrac] for r in sub])
        sizes = rfrac * Q
        agree = []
        for q, pv in zip(sizes, pr):
            m = (Q >= 0.8 * q) & (Q <= 1.2 * q)
            if m.sum() >= 5:
                agree.append((pv, float(pk[m].mean())))
        a = np.array(agree)
        rows[str(rfrac)] = {"n": int(len(a)), "mean_path_impact_at_fraction_bps": float(a[:, 0].mean()) if len(a) else None, "mean_peak_of_a_run_of_that_size_bps": float(a[:, 1].mean()) if len(a) else None}
    paths[v] = rows
report["path"] = paths
# ---- one look at the HOLDOUT ------------------------------------------------------------------------------------------------------------------
if HOLD in W:
    ho = [R[(HOLD, "bybit")]]
    report["holdout_eval_bybit"] = evaluate(ho, "HOLDOUT = 2026-10-29 capture (OUT-OF-PERIOD), Bybit, parameters from TRAIN")
    rb = R[(HOLD, "binance")]
    Dk = rb["depth"][K]; ok = np.isfinite(Dk) & (Dk > 0)
    report["capture_binance_in_sample_form_a"] = fit_a(rb["Q"], Dk, rb["peak"])
    sg, vd = np.full(rb["n"], rb["sigma"]), np.full(rb["n"], rb["vday"])
    report["capture_binance_in_sample_form_b"] = fit_b(rb["Q"], sg, vd, rb["peak"])
    # size where the predicted peak reaches one tick (0.01 on ~100) and half a tick, by form
    tick_bps = lambda mid: 0.01 / mid * 1e4
    mid_c = float(np.nanmedian(W[HOLD][0]["bybit"]["mid"]))
    Dmed = float(np.nanmedian(R[(HOLD, "bybit")]["depth"][K]))
    one = {}
    for nm, tb in (("one_tick", tick_bps(mid_c)), ("half_tick", tick_bps(mid_c) / 2)):
        qa = Dmed * ((tb - fa["a"]) / fa["b"]) ** (1 / fa["delta"]) if fa["b"] > 0 and tb > fa["a"] else None
        sig, Vd = R[(HOLD, "bybit")]["sigma"], R[(HOLD, "bybit")]["vday"]
        qb = Vd * (tb / (fb["Y"] * sig)) ** (1 / fb["delta"]) if fb["Y"] > 0 else None
        one[nm] = {"tick_bps": tb, "form_a_size_SOL_at_median_depth": qa, "form_b_size_SOL": qb}
    report["tick_threshold"] = {"mid": mid_c, "median_D_k_capture_bybit": Dmed, "sizes": one}
for key in ("selection",):
    report[key]["grid"] = report[key]["grid"][:6]
(D / "impact_fit.json").write_text(json.dumps(report, indent=1, default=float))
print(json.dumps(report, indent=1, default=float))
