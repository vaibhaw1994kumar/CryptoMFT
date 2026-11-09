"""S1 (owner 9 Nov), a run wrapper: choose the adverse-selection table's scheme and shrinkage on SELECT, then fit the chosen one on TRAIN.
Labels: <labels dir>/labels_<day>.json, written by `new_backtest.market_hedge_move --labels-out --label-horizons` from MARKET DATA ONLY (a print on the maker venue (Bybit) that would fill a passive quote, the HEDGE venue's (Binance) mid move after
50 ms, 150 ms, 1 s, 5 s and 30 s). TRAIN 2024-03-01..07 fits every candidate; SELECT 2024-03-08..11 scores them (mean squared error of the predicted move, per event, by horizon) and the best at the SELECTION HORIZON is kept;
the final table is refit on TRAIN only. HOLDOUT (2024-03-13..15) is not read: no label file for those days exists and this script cannot open one. Not fitted on any strategy fill, simulated or not. EXPLORATORY until the simulator validation step.
Rules, fixed before the 50 ms / 150 ms labels were scored (owner 9 Nov, S1 item 1): (1) the selection horizon is 0.05 s, the one closest to the assumed hedge arrival delay (45 ms; no measured Binance delay exists); the first selection (labels at 1 / 5 / 30 s only)
used 1 s, which gave depth + vol + flow, depth buckets (1, 2, 4, 8), k = 300; (2) TIE-BREAK, changed after the first run: candidates within 0.01 of the best score tie, and a tie goes to fewer dimensions, then MORE shrinkage (the first run took the smaller k; k = 0
and k = 30 scored 54.803 and 54.803 at 1 s, a 0.006 difference to the chosen k = 300, and k = 0 leaves thin cells, such as a depth bucket with no volatility value, unshrunk); HOLDOUT was not touched by either; (3) besides the squared error, the report gives the
median absolute error and the 0.1 %-trimmed squared error, per SELECT day and with 03-11 left out (03-11 carries most of the pooled squared error).
Usage: s1_select.py LABELS_DIR OUT_DIR [SELECTION_HORIZON_S]"""
import itertools
import json
import sys
from pathlib import Path

sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from crypto_stat_arb.strategies.edge import fit_adverse_table

HERE = Path(sys.argv[1])
TRAIN = [f"2024-03-0{d}" for d in range(1, 8)]
SELECT = ["2024-03-08", "2024-03-09", "2024-03-10", "2024-03-11"]
HOLDOUT_PREFIXES = ("2024-03-12", "2024-03-13", "2024-03-14", "2024-03-15")
HORIZONS = (0.05, 0.15, 1.0, 5.0, 30.0)
SEL_H = float(sys.argv[3]) if len(sys.argv) > 3 else 0.05
out_dir = Path(sys.argv[2])
out_dir.mkdir(parents=True, exist_ok=True)


def load(days):
    rows = []
    for d in days:
        if d.startswith(HOLDOUT_PREFIXES):
            raise SystemExit(f"{d} is HOLDOUT: not to be read")
        rows += json.loads((HERE / f"labels_{d}.json").read_text())["rows"]
    return rows


def quantiles(values, qs):
    v = sorted(x for x in values if x is not None)
    return tuple(round(v[min(len(v) - 1, int(len(v) * q))], 6) for q in qs)


train, select = load(TRAIN), load(SELECT)
print(f"TRAIN {len(train)} events, SELECT {len(select)} events", flush=True)
vol_edges = quantiles([r["vol_bps"] for r in train], (1 / 3, 2 / 3))
spread_edges = quantiles([r["spread_bps"] for r in train], (1 / 3, 2 / 3))
flow_edges = quantiles([r["flow"] for r in train], (1 / 3, 2 / 3))
DEPTH_OPTIONS = {"depth4": (1, 2, 4, 8), "depth2": (1, 3), "depth1": (1,)}
SCHEMES = [(), ("depth",), ("depth", "vol"), ("depth", "flow"), ("depth", "spread"), ("depth", "vol", "flow"), ("depth", "vol", "spread")]
KS = (0.0, 30.0, 100.0, 300.0, 1000.0)


def mse(table, rows, h):
    key, hk = f"move_{h:g}s", f"{h:g}"
    n = e = 0.0
    for r in rows:
        y = r[key]
        if y is None:
            continue
        p = table.expected_move_bps(r["side"], depth_ticks=r["depth_ticks"], vol_bps=r["vol_bps"], flow=r["flow"], spread_bps=r["spread_bps"], horizon_s=h)
        e += (y - p) ** 2
        n += 1
    return e / n if n else None


def fit(dims, depth_name, k, rows):
    return fit_adverse_table(rows, dims=dims, depth_edges=DEPTH_OPTIONS[depth_name], vol_edges=vol_edges, flow_edges=flow_edges, spread_edges=spread_edges, shrink_k=k, horizons_s=HORIZONS,
                             fitted_on={"days": TRAIN, "source": "MARKET_DATA_LABELS", "events": len(rows), "scheme": list(dims), "depth_edges": list(DEPTH_OPTIONS[depth_name]),
                                        "label": "EXPLORATORY: fitted on TRAIN only, scheme and k chosen on SELECT; HOLDOUT never read; no strategy fill used"})


baseline = fit((), "depth1", 0.0, train)                       # one number per side: the form the strategy uses today
base_mse = {f"{h:g}": mse(baseline, select, h) for h in HORIZONS}
print("baseline (one mean per side) SELECT MSE", base_mse, flush=True)
results = []
for dims, (depth_name, _), k in itertools.product(SCHEMES, DEPTH_OPTIONS.items(), KS):
    if not dims and depth_name != "depth4":
        continue                                                # the depth options only matter when depth is a dimension
    t = fit(dims, depth_name, k, train)
    m = {f"{h:g}": mse(t, select, h) for h in HORIZONS}
    results.append({"dims": list(dims), "depth": depth_name, "k": k, "select_mse": m, "cells": len(t.cells), "gain_vs_baseline": {k: 1 - m[k] / base_mse[k] for k in m}})
floor = min(r["select_mse"][f"{SEL_H:g}"] for r in results)
best = min((r for r in results if r["select_mse"][f"{SEL_H:g}"] <= floor + 0.01), key=lambda r: (len(r["dims"]), -r["k"], r["select_mse"]["1"]))   # scores within 0.01 of the best tie: prefer fewer dimensions, then more shrinkage (thin cells such as a depth bucket with no volatility value stay pulled to their parent)
print(f"best on SELECT ({SEL_H:g} s):", json.dumps(best), flush=True)
final = fit(tuple(best["dims"]), best["depth"], best["k"], train)
final.save(out_dir / "adverse_table_v1.json")
def errors(table, rows, h):
    key = f"move_{h:g}s"
    return [r[key] - table.expected_move_bps(r["side"], depth_ticks=r["depth_ticks"], vol_bps=r["vol_bps"], flow=r["flow"], spread_bps=r["spread_bps"], horizon_s=h) for r in rows if r[key] is not None]


def scores(errs):
    sq = sorted(e * e for e in errs)
    cut = int(len(sq) * 0.999)                                   # the 0.1 % largest squared errors are dropped
    ab = sorted(abs(e) for e in errs)
    return {"n": len(errs), "mse": sum(sq) / len(sq), "trimmed_mse_0.1pct": sum(sq[:cut]) / cut, "median_abs_error": ab[len(ab) // 2]}


by_day = {d: load([d]) for d in SELECT}
robust = {}
for h in (0.05, 1.0):
    per = {}
    for d in SELECT:
        per[d] = {"baseline": scores(errors(baseline, by_day[d], h)), "chosen": scores(errors(final, by_day[d], h))}
    ex = [r for d in SELECT if d != "2024-03-11" for r in by_day[d]]
    per["without_2024-03-11"] = {"baseline": scores(errors(baseline, ex, h)), "chosen": scores(errors(final, ex, h))}
    per["all_select"] = {"baseline": scores(errors(baseline, select, h)), "chosen": scores(errors(final, select, h))}
    for k, v in per.items():
        v["gain"] = {m: 1 - v["chosen"][m] / v["baseline"][m] for m in ("mse", "trimmed_mse_0.1pct", "median_abs_error")}
    robust[f"{h:g}"] = per
    print(f"--- SELECT scores at {h:g} s (gain = 1 - chosen / baseline; positive = the table is better)")
    for k, v in per.items():
        print(f"  {k:20s} n={v['baseline']['n']:6d}  mse {v['baseline']['mse']:.4f} -> {v['chosen']['mse']:.4f} ({100*v['gain']['mse']:+.2f}%)  trimmed {100*v['gain']['trimmed_mse_0.1pct']:+.2f}%  median|err| {v['baseline']['median_abs_error']:.4f} -> {v['chosen']['median_abs_error']:.4f} ({100*v['gain']['median_abs_error']:+.2f}%)")
(out_dir / "s1_robust.json").write_text(json.dumps(robust, indent=1))

summary = {"label": "EXPLORATORY: S1 adverse-selection table; market-data labels only", "train_days": TRAIN, "select_days": SELECT, "events": {"train": len(train), "select": len(select)},
           "edges": {"vol": vol_edges, "spread": spread_edges, "flow": flow_edges}, "baseline_select_mse": base_mse, "chosen": best, "candidates": sorted(results, key=lambda r: r["select_mse"][f"{SEL_H:g}"])[:12],
           "all_candidates_count": len(results), "holdout": "never read"}
(out_dir / "s1_selection.json").write_text(json.dumps(summary, indent=1))
for side in ("bid", "ask"):
    print(side, "side mean (TRAIN):", final.side_mean[side])
print("done")
