"""Shared functions of the impact fits (copied from impact_fit.py, which is kept as run): loading the prepared grids, aggressor runs OR single taker orders, curve fits, block bootstrap."""
import numpy as np
D = __import__("pathlib").Path("/Users/vaibhav/Documents/Newproject/out/integration/cost/impact")
KS, KIDX = (2.0, 5.0, 10.0), {2.0: 0, 5.0: 1, 10.0: 2}
DELTAS = np.round(np.arange(0.2, 1.5001, 0.01), 2)
HORIZONS = (1.0, 10.0, 60.0)
ISO_W, ISO_FRAC, BLOCK_S, REPS = 60.0, 0.75, 600.0, 2000
rng = np.random.default_rng(1)
MIN_SIZE = 1.0

def load(name):
    z = np.load(D / f"data_{name}.npz")
    out = {}
    for v in ("bybit", "binance"):
        out[v] = {k: z[f"{v}_{k}"] for k in ("t", "mid", "dbid", "dask", "tr_t", "tr_p", "tr_v", "tr_s")}
        out[v]["tr_xt"] = z[f"{v}_tr_xt"] if f"{v}_tr_xt" in z.files else out[v]["tr_t"]          # the exchange time of each print (the March files carry only that clock)
    hours = (max(out["bybit"]["t"][-1], out["binance"]["t"][-1]) - min(out["bybit"]["t"][0], out["binance"]["t"][0])) / 3600
    return out, hours


def gi(t_grid, t):
    return np.clip(np.searchsorted(t_grid, t, side="right") - 1, 0, len(t_grid) - 1)


def runs_of(win, hours, venue, other, dt, name, mode="run", min_size=None):
    """mode run: consecutive same-side prints less than dt apart; mode order: consecutive same-side prints with the SAME exchange timestamp (one taker order sweeping levels: Binance aggTrades / Bybit prints of one order)."""
    d, o = win[venue], win[other]
    t, v, s, p = d["tr_t"], d["tr_v"], d["tr_s"], d["tr_p"]
    if len(t) < 2:
        return None
    xt = d["tr_xt"]
    brk = np.r_[True, (np.diff(t) >= dt) | (s[1:] != s[:-1])] if mode == "run" else np.r_[True, (xt[1:] != xt[:-1]) | (s[1:] != s[:-1])]
    gid = np.cumsum(brk) - 1
    first = np.flatnonzero(brk)
    last = np.r_[first[1:] - 1, len(t) - 1]
    vol = np.add.reduceat(v, first)
    keep = vol >= (MIN_SIZE if min_size is None else min_size)
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
    return dict(name=name, venue=venue, dt=dt, mode=mode, ts=ts, te=te, Q=vol, sign=sign, peak=peak, res=res, cross=cross, depth=dep, iso=iso, path=path, sigma=sigma, vday=vday, n=len(vol))


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


