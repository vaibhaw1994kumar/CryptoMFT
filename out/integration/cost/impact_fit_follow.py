"""Step C follow-up (a run wrapper; rules: IMPACT_FIT_RULES.md plus the owner's follow-up of 14 Nov): (1) single taker orders rebuilt first (one exchange timestamp and one side = one order sweeping levels; Binance aggTrade
style, Bybit prints with identical timestamp and side) and the small-size end fitted on them; (2) all-runs and isolated-runs fits side by side, isolated as the overlay default and all-runs as the upper bound; (3) the
fit of our own hedges or probe fills, to measure the bias of the proxy: NOT POSSIBLE with what exists (see the output). PROXY (public prints), EXPLORATORY. NOTE: the capture is looked at for a SECOND time here
(the first look was the all-runs fit of impact_fit.py); parameters are fitted on 03-01 and 03-08/03-11 only. usage: impact_fit_follow.py"""
import json, sys
sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/out/integration/cost")
import numpy as np
import impact_lib as L
from impact_lib import D, KS, HORIZONS, BLOCK_S, cat, fit_a, fit_b, pred_a, pred_b, runs_of, load, boot_ratio, boot_mean_diff
TRAIN, SELECT, HOLD = ["2024-03-01"], ["2024-03-08", "2024-03-11"], "capture_2026-10-29"
DT, K = 0.1, 10.0
W = {n: load(n) for n in TRAIN + SELECT + [HOLD]}
report = {"label": "PROXY (public prints), EXPLORATORY; the capture is a SECOND look (out-of-period)", "windows": {n: round(h, 2) for n, (_, h) in W.items()}}


def R(name, venue, mode, min_size=None):
    return runs_of(W[name][0], W[name][1], venue, "binance" if venue == "bybit" else "bybit", DT, name, mode=mode, min_size=min_size)


def sub(r, mask):
    """A run dict restricted to a boolean mask."""
    out = dict(r)
    for k in ("ts", "te", "Q", "sign", "peak", "iso"):
        out[k] = r[k][mask]
    out["res"] = {h: v[mask] for h, v in r["res"].items()}
    out["cross"] = {h: v[mask] for h, v in r["cross"].items()}
    out["depth"] = {k: v[mask] for k, v in r["depth"].items()}
    out["path"] = {k: v[mask] for k, v in r["path"].items()}
    out["n"] = int(mask.sum())
    return out


def per_run(rs, key):
    return np.repeat([r[key] for r in rs], [r["n"] for r in rs])


def fit_pair(rs, label):
    Q, Dk, y = cat(rs, "Q"), cat(rs, "depth", K), cat(rs, "peak")
    fa = fit_a(Q, Dk, y) if np.isfinite(Dk).any() else None
    fb = fit_b(Q, per_run(rs, "sigma"), per_run(rs, "vday"), y)
    return {"n": int(len(y)), "mean_peak_bps": float(y.mean()), "form_a": fa, "form_b": fb}


def mae_eval(rs, fa, fb, null_mean):
    Q, Dk, y = cat(rs, "Q"), cat(rs, "depth", K), cat(rs, "peak")
    sg, V = per_run(rs, "sigma"), per_run(rs, "vday")
    blocks = np.floor((cat(rs, "ts") - min(r["ts"].min() for r in rs)) / BLOCK_S).astype(int)
    out = {"n": int(len(y)), "mae_null": float(np.abs(y - null_mean).mean())}
    eb = np.abs(pred_b(fb, Q, sg, V) - y)
    out["mae_form_b"] = float(eb.mean())
    out["b_minus_null_ci95"] = boot_mean_diff(blocks, eb, np.abs(y - null_mean))
    if fa is not None:
        ok = np.isfinite(Dk) & (Dk > 0)
        ea = np.abs(pred_a(fa, Q[ok], Dk[ok]) - y[ok])
        out["mae_form_a"] = float(ea.mean())
        out["a_minus_null_ci95"] = boot_mean_diff(blocks[ok], ea, np.abs(y[ok] - null_mean))
    return out


# ---------------- (1) single taker orders ------------------------------------------------------------------------------------------------------------
ORD_MIN = 0.1
orders = {(n, v): R(n, v, "order", ORD_MIN) for n in W for v in ("bybit", "binance")}
runs = {(n, v): R(n, v, "run") for n in W for v in ("bybit", "binance")}
bins = [0.1, 0.3, 1, 3, 10, 30, 100, 1e9]
table = {}
for v in ("bybit", "binance"):
    rs = [orders[(n, v)] for n in TRAIN + SELECT]
    Q, y = cat(rs, "Q"), cat(rs, "peak")
    rows = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (Q >= lo) & (Q < hi)
        if m.sum() >= 30:
            rows.append({"size_SOL": f"{lo:g}-{hi:g}" if hi < 1e8 else f">={lo:g}", "n": int(m.sum()), "mean_peak_bps": float(y[m].mean()), "se_bps": float(y[m].std() / np.sqrt(m.sum()))})
    table[v] = rows
report["single_orders_binned_TRAIN_SELECT"] = table
tr = [orders[(n, "bybit")] for n in TRAIN]
fo = fit_pair(tr, "orders TRAIN Bybit")
Qt, Dt = cat(tr, "Q"), cat(tr, "depth", K)
okd = np.isfinite(Dt) & (Dt > 0)
med = float(np.median(Qt[okd] / Dt[okd]))
bands = {}
for nm, m in (("small", okd & (Qt / np.where(Dt > 0, Dt, np.nan) <= med)), ("large", okd & (Qt / np.where(Dt > 0, Dt, np.nan) > med))):
    bands[nm] = fit_a(Qt[m], Dt[m], cat(tr, "peak")[m])
fo["size_band_median_Q_over_D"] = med
fo["form_a_by_size_band"] = bands
bs = {}
for nm, lo, hi in (("small_Q<3", 0, 3), ("large_Q>=3", 3, 1e9)):
    m = (Qt >= lo) & (Qt < hi)
    z = per_run(tr, "sigma")[m] * (Qt[m] / per_run(tr, "vday")[m])
    bs[nm] = fit_b(Qt[m], per_run(tr, "sigma")[m], per_run(tr, "vday")[m], cat(tr, "peak")[m])
fo["form_b_by_size_SOL"] = bs
report["single_orders_fit_TRAIN_bybit"] = fo
se = [orders[(n, "bybit")] for n in SELECT]
ho = [orders[(HOLD, "bybit")]]
report["single_orders_eval"] = {"SELECT": mae_eval(se, fo["form_a"], fo["form_b"], cat(tr, "peak").mean()), "HOLDOUT_capture_second_look": mae_eval(ho, fo["form_a"], fo["form_b"], cat(tr, "peak").mean())}

# ---------------- (2) all runs vs isolated runs, side by side --------------------------------------------------------------------------------------------
pool_names = TRAIN + SELECT
def subset(rs, iso):
    return [sub(r, r["iso"]) if iso else r for r in rs]
side = {}
for tag, iso in (("all_runs", False), ("isolated_runs", True)):
    rs_b = subset([runs[(n, "bybit")] for n in pool_names], iso)
    fit = fit_pair(rs_b, tag)
    # decay and cross, both venues
    dec, cross = {}, {}
    for v, o in (("bybit", "binance"), ("binance", "bybit")):
        rv = subset([runs[(n, v)] for n in pool_names], iso)
        peak = cat(rv, "peak")
        res = {h: cat(rv, "res", h) for h in HORIZONS}
        blocks = np.concatenate([np.floor((r["ts"] - r["ts"].min()) / BLOCK_S).astype(int) + 1000 * i for i, r in enumerate(rv)])
        phi = float(res[60.0].sum() / peak.sum()); ratio = {h: float(res[h].sum() / peak.sum()) for h in HORIZONS}
        tau = min((float(np.sum([(phi + (1 - phi) * np.exp(-h / t) - ratio[h]) ** 2 for h in (1.0, 10.0)])), float(t)) for t in np.arange(0.5, 200.01, 0.5))[1]
        beta = min((float(np.sum([(phi + (1 - phi) * (1 + h) ** (-b) - ratio[h]) ** 2 for h in (1.0, 10.0)])), float(b)) for b in np.arange(0.05, 2.0001, 0.05))[1]
        dec[v] = {"n_runs": int(len(peak)), "permanent_fraction": phi, "permanent_fraction_ci95": boot_ratio(blocks, res[60.0], peak), "tau_s_exp": tau, "beta_power": beta, "mean_peak_bps": float(peak.mean())}
        own = peak
        oth = cat(rv, "cross", 0.0)
        cross[f"{v}->{o}"] = {"h0_coefficient": float((own * oth).sum() / (own * own).sum()), "h0_ci95": boot_ratio(blocks, own * oth, own * own)}
    fa, fb = fit["form_a"], fit["form_b"]
    hold = [subset([runs[(HOLD, "bybit")]], iso)[0]]
    side[tag] = {"fit_TRAIN_SELECT_bybit": fit, "decay": dec, "cross": cross, "holdout_capture_second_look": mae_eval(hold, fa, fb, fit["mean_peak_bps"]) if hold[0]["n"] > 20 else {"n": hold[0]["n"], "note": "too few runs"}}
report["all_vs_isolated"] = side

# ---------------- overlay settings ---------------------------------------------------------------------------------------------------------------------------
cap = W[HOLD][0]["binance"]
one = cap["mid"][::600]; sig_b = float(np.std(np.diff(np.log(one[one > 0]))) * np.sqrt(1440) * 1e4)
hours = (max(W[HOLD][0]["bybit"]["t"][-1], cap["t"][-1]) - min(W[HOLD][0]["bybit"]["t"][0], cap["t"][0])) / 3600
vday_b = float(cap["tr_v"].sum() * 24 / hours)
def settings(tag, label):
    s = side[tag]
    fb = s["fit_TRAIN_SELECT_bybit"]["form_b"]
    d = s["decay"]["binance"]
    return {"level": 3, "form": "volume", "y": fb["Y"], "sigma_daily_bps": sig_b, "daily_volume": vday_b, "delta_volume": fb["delta"], "permanent_fraction": min(max(d["permanent_fraction"], 0.0), 1.0), "kernel": "exp",
            "tau_s": max(d["tau_s_exp"], 0.5), "cross_bybit_to_binance": max(s["cross"]["bybit->binance"]["h0_coefficient"], 0.0), "cross_binance_to_bybit": max(s["cross"]["binance->bybit"]["h0_coefficient"], 0.0), "label": label}
iso_set = settings("isolated_runs", "PROXY, ISOLATED runs of 03-01, 03-08, 03-11 (Y, delta, phi capped at 1, tau, cross at the end of the run); sigma and V from the 2026-10-29 capture (Binance). DEFAULT")
all_set = settings("all_runs", "PROXY, ALL runs of 03-01, 03-08, 03-11 (phi capped at 1): the UPPER BOUND; sigma and V from the 2026-10-29 capture (Binance)")
report["overlay_settings"] = {"default_isolated": iso_set, "upper_all_runs": all_set}
import shutil, os
base = "/Users/vaibhav/Documents/Newproject/out/integration/cost/"
if os.path.exists(base + "impact_overlay_settings.json") and not os.path.exists(base + "impact_overlay_settings_first_fit.json"):
    shutil.copy(base + "impact_overlay_settings.json", base + "impact_overlay_settings_first_fit.json")          # the first fit's settings (all runs, phi and tau ASSUMED) stay for the replays already made
json.dump(iso_set, open(base + "impact_overlay_settings.json", "w"), indent=1)
json.dump(all_set, open(base + "impact_overlay_settings_upper.json", "w"), indent=1)
report["own_fills"] = {"status": "NOT POSSIBLE NOW", "why": "no real aggressive fill of ours exists with a book to fit against: the arb never ran live, the testnet runs have no usable depth, the real MM orders were PostOnly spot orders whose books are lost, and the simulated hedges "
                       "(replays, probe) are priced by a walk on displayed depth, so their 'impact' is the model's own and fitting it would only return the model (circular). The probe fills are PASSIVE fills and move nothing. The bias of "
                       "the proxy (Y, delta, permanent fraction) therefore stays UNMEASURED; the procedure is the same run definition applied to our own aggressive fills once they exist."}
(D / "impact_fit_follow.json").write_text(json.dumps(report, indent=1, default=float))
print(json.dumps({k: report[k] for k in ("single_orders_fit_TRAIN_bybit", "single_orders_eval")}, indent=1, default=float))
for tag, s in side.items():
    print(tag, "Bybit form b", s["fit_TRAIN_SELECT_bybit"]["form_b"], "form a", s["fit_TRAIN_SELECT_bybit"]["form_a"], "n", s["fit_TRAIN_SELECT_bybit"]["n"])
    print("  decay", {v: {k: (round(x, 3) if isinstance(x, float) else x) for k, x in d.items() if k != "permanent_fraction_ci95"} for v, d in s["decay"].items()})
    print("  cross", {k: round(v["h0_coefficient"], 3) for k, v in s["cross"].items()}, "holdout", {k: (round(x, 3) if isinstance(x, float) else x) for k, x in s["holdout_capture_second_look"].items() if not k.endswith("ci95")})
print(json.dumps(report["single_orders_binned_TRAIN_SELECT"], indent=0, default=float)[:1800])
