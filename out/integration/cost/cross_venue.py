"""Execution cost model, step D (a run wrapper; feed-only analysis of the prepared grids): cross-venue dynamics of Bybit and Binance SOLUSDT perps in the spirit of Makarov and Schoar (JFE 2020), at 1 s, 5 s and 60 s.
Common and venue-specific parts of returns and signed flow, the common lambda, the cross-venue impact, the deviation (basis) persistence and what predicts its next move, and how much of the arb's delay drift (step A) the flow
explains. PROXY (public prints), EXPLORATORY, one coin (SOL), the 2026-10-29 capture (14 h) and three March 2024 windows (2 h) as a check. usage: cross_venue.py"""
import json
from pathlib import Path
import numpy as np
D = Path("/Users/vaibhav/Documents/Newproject/out/integration/cost/impact")
OUT = Path("/Users/vaibhav/Documents/Newproject/out/integration/cost/cross_venue"); OUT.mkdir(exist_ok=True)
WINDOWS = ["capture_2026-10-29", "2024-03-01", "2024-03-08", "2024-03-11"]
DELTAS = (1.0, 5.0, 60.0)
BLOCK_S, REPS = 600.0, 1000
rng = np.random.default_rng(1)


def load(name):
    z = np.load(D / f"data_{name}.npz")
    return {v: {k: z[f"{v}_{k}"] for k in ("t", "mid", "tr_t", "tr_v", "tr_s")} for v in ("bybit", "binance")}


def bins(win, delta):
    t0 = max(win["bybit"]["t"][0], win["binance"]["t"][0]); t1 = min(win["bybit"]["t"][-1], win["binance"]["t"][-1])
    edges = np.arange(t0, t1, delta)
    out = {"edges": edges[:-1]}
    for v, d in win.items():
        idx = np.clip(np.searchsorted(d["t"], edges, side="right") - 1, 0, len(d["t"]) - 1)
        m = d["mid"][idx]
        out["r_" + v] = (m[1:] - m[:-1]) / m[:-1] * 1e4
        sv = d["tr_s"] * d["tr_v"]
        cs = np.r_[0.0, np.cumsum(sv)]
        k = np.searchsorted(d["tr_t"], edges, side="left")
        out["f_" + v] = cs[k[1:]] - cs[k[:-1]]
        out["mid_" + v] = m[:-1]
    return out


def ols(X, y, blocks):
    """OLS with an intercept; coefficients, R2 and 95% block-bootstrap intervals (resampling whole blocks)."""
    A = np.c_[np.ones(len(y)), X]
    coef = np.linalg.lstsq(A, y, rcond=None)[0]
    r2 = 1 - ((y - A @ coef) ** 2).sum() / ((y - y.mean()) ** 2).sum()
    ub = np.unique(blocks)
    idx = [np.flatnonzero(blocks == b) for b in ub]
    boots = []
    for _ in range(REPS):
        pick = np.concatenate([idx[i] for i in rng.integers(0, len(ub), len(ub))])
        boots.append(np.linalg.lstsq(A[pick], y[pick], rcond=None)[0])
    boots = np.array(boots)
    return {"coef": coef[1:].tolist(), "intercept": float(coef[0]), "r2": float(r2), "ci95": [[float(np.percentile(boots[:, j + 1], 2.5)), float(np.percentile(boots[:, j + 1], 97.5))] for j in range(X.shape[1])], "n": int(len(y))}


report = {"label": "PROXY (public prints), EXPLORATORY, SOL only; capture 14 h + three 2 h March windows as a check", "windows": {}}
for name in WINDOWS:
    win = load(name)
    w = {}
    for delta in DELTAS:
        b = bins(win, delta)
        blocks = np.floor((b["edges"] - b["edges"][0]) / BLOCK_S).astype(int)
        ok = np.isfinite(b["r_bybit"]) & np.isfinite(b["r_binance"])
        rY, rB, fY, fB, bl = b["r_bybit"][ok], b["r_binance"][ok], b["f_bybit"][ok], b["f_binance"][ok], blocks[ok]
        rc, fc = (rY + rB) / 2, (fY + fB) / 2
        row = {"bins": int(ok.sum())}
        row["common_lambda_bps_per_SOL"] = ols(fc[:, None], rc, bl)
        row["bybit_return_on_own_and_binance_flow"] = ols(np.c_[fY, fB], rY, bl)          # coef[0] own (Bybit) flow, coef[1] cross (Binance) flow
        row["binance_return_on_own_and_bybit_flow"] = ols(np.c_[fB, fY], rB, bl)          # coef[0] own (Binance) flow, coef[1] cross (Bybit) flow = the move on Binance per SOL of Bybit flow
        row["return_specific_vs_flow_specific"] = ols(((fY - fB) / 2)[:, None], (rY - rB) / 2, bl)
        # deviation: the Bybit mid against the Binance mid, bps of their average
        dev = (b["mid_bybit"] - b["mid_binance"]) / ((b["mid_bybit"] + b["mid_binance"]) / 2) * 1e4
        dv = dev[ok]
        x, y = dv[:-1], dv[1:]
        c = np.cov(x, y)
        rho = float(c[0, 1] / c[0, 0])
        row["deviation"] = {"mean_bps": float(dv.mean()), "sd_bps": float(dv.std()), "ar1_rho": rho, "half_life_s": float(np.log(0.5) / np.log(rho) * delta) if 0 < rho < 1 else None}
        # does venue-specific flow predict the next change of the deviation? change(t -> t+delta) on the level and on the Bybit-specific flow of the bin that just ended
        fs = ((fY - fB) / 2)[1:]
        row["deviation_next_change_on_level_and_flow"] = ols(np.c_[dv[:-1], ((fY - fB) / 2)[:-1]], dv[1:] - dv[:-1], bl[:-1])
        row["deviation_next_change_on_level_only"] = ols(dv[:-1][:, None], dv[1:] - dv[:-1], bl[:-1])
        w[f"{int(delta)}s"] = row
    report["windows"][name] = w

# ---- how much of the arb's delay drift (step A) the flow explains ---------------------------------------------------------------------------
E = Path("/Users/vaibhav/Documents/Newproject/out/integration/e3_sweep")
rows = [json.loads(l) for l in open("/Users/vaibhav/Documents/Newproject/out/integration/cost/shortfall/shortfall_rows.jsonl")]
sig_ts = {}
for run in ("fixed_size_1", "fixed_size_5", "size_25c", "fixed_size_100"):
    sigs = {s["opportunity_id"]: s for s in map(json.loads, open(E / run / "session_signals.jsonl"))}
    orders = [json.loads(l) for l in open(E / run / "session_orders.jsonl")]
    fills = [json.loads(l) for l in open(E / run / "session_fills.jsonl")]
    opp = {f["order_id"]: f["opportunity_id"] for f in fills if f.get("opportunity_id")}
    for o in orders:
        if o["intent_id"] in {r["maker_intent_id"] for r in rows if r["run"] == run} and opp.get(o["order_id"]) in sigs:
            sig_ts[(run, o["intent_id"])] = sigs[opp[o["order_id"]]]["ts"]
cap = load("capture_2026-10-29")
bb = cap["bybit"]
sv = bb["tr_s"] * bb["tr_v"]
cs = np.r_[0.0, np.cumsum(sv)]
def flow(t0, t1):
    a, b = np.searchsorted(bb["tr_t"], t0, "left"), np.searchsorted(bb["tr_t"], t1, "left")
    return cs[b] - cs[a]
kappa = {k: report["windows"]["capture_2026-10-29"][k]["binance_return_on_own_and_bybit_flow"]["coef"][1] for k in ("1s", "5s", "60s")}       # Binance move (bps) per SOL of signed Bybit flow
fit_rows = []
for r in rows:
    q = sig_ts.get((r["run"], r["maker_intent_id"]))
    if q is None:
        continue
    sgn = 1.0 if r["maker_side"] == "buy" else -1.0
    f_during = flow(q, r["maker_fill_ts"])                          # signed Bybit flow while the quote rested (negative = sellers hit the bids)
    f_before = flow(q - 5.0, q)                                     # the 5 s before the quote (known when it is placed)
    fit_rows.append({"drift": r["delay_drift_bps"], "pred_during": -sgn * kappa["5s"] * f_during, "pred_before": -sgn * kappa["5s"] * f_before, "rest_s": r["maker_fill_ts"] - q, "qty": r["qty"]})
d = np.array([x["drift"] for x in fit_rows]); pd_ = np.array([x["pred_during"] for x in fit_rows]); pb = np.array([x["pred_before"] for x in fit_rows])
def summary(pred):
    if len(d) < 5 or pred.std() == 0:
        return None
    slope = float(np.cov(pred, d)[0, 1] / pred.var(ddof=1))
    return {"corr": float(np.corrcoef(pred, d)[0, 1]), "r2": float(np.corrcoef(pred, d)[0, 1] ** 2), "slope_realised_on_predicted": slope, "mean_predicted_bps": float(pred.mean()), "mean_realised_bps": float(d.mean()),
            "share_of_mean_drift_explained": float(pred.mean() / d.mean()) if d.mean() else None}
report["arb_delay_drift"] = {"label": "115 fills from about 27 quotes of the corrected sweep (one capture day): the fills of one quote share its drift, so the effective sample is smaller; no interval is given", "fills_joined": len(fit_rows), "median_rest_s": float(np.median([x["rest_s"] for x in fit_rows])),
                             "kappa_binance_bps_per_SOL_of_bybit_flow": kappa, "during_the_rest": summary(pd_), "five_seconds_before_the_quote": summary(pb)}
(OUT / "cross_venue.json").write_text(json.dumps(report, indent=1))
print(json.dumps({k: v for k, v in report.items() if k != "windows"}, indent=1))
cw = report["windows"]["capture_2026-10-29"]
for dl, row in cw.items():
    print(dl, "bins", row["bins"], "| common lambda %.5f bps/SOL r2 %.3f" % (row["common_lambda_bps_per_SOL"]["coef"][0], row["common_lambda_bps_per_SOL"]["r2"]),
          "| Bybit->Binance %.5f ci %s | own Binance %.5f | Binance->Bybit %.5f" % (row["binance_return_on_own_and_bybit_flow"]["coef"][1], np.round(row["binance_return_on_own_and_bybit_flow"]["ci95"][1], 5), row["binance_return_on_own_and_bybit_flow"]["coef"][0], row["bybit_return_on_own_and_binance_flow"]["coef"][1]),
          "| dev rho %.3f hl %s s, sd %.2f bps" % (row["deviation"]["ar1_rho"], row["deviation"]["half_life_s"] and round(row["deviation"]["half_life_s"], 2), row["deviation"]["sd_bps"]),
          "| flow adds r2 %.4f -> %.4f, gamma %.6f ci %s" % (row["deviation_next_change_on_level_only"]["r2"], row["deviation_next_change_on_level_and_flow"]["r2"], row["deviation_next_change_on_level_and_flow"]["coef"][1], np.round(row["deviation_next_change_on_level_and_flow"]["ci95"][1], 6)))
