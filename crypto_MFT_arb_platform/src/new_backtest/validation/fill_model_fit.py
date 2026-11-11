"""Spot simulator validation, step 3: FillModelV2 fitted on REAL outcomes (owner brief 10 Nov 2026). Output tag MEASURED_SPOT_2026; not approved as a quote filter.

Features: the placement features the engine computed for each replayed order (fill_events.jsonl of the proportional run: queue ahead, depth, spread, short vol,
trade count and sign, regime, ticks from touch), i.e. the replayed book at the real create time. LABELS ARE REAL, never the simulator's fills: filled within h
= the real first execution within h seconds of the real create time; censored = the real order was cancelled before h without a fill (IPCW as in
fill_model_v1, weights from fill_label.compute_ipcw_weights). price_moved_through (used only by the adverse model) is the market's, from the replayed book.
Split: chronological. Fit on the days before the test day, test on that day; the headline test day is 2026-05-02 (flagged: possibly a different quoting regime,
with most of the cancels). Brier score and reliability against the base rate of the fit days (fill_model_v1.calibration_report).
Excluded as in evaluate.py (04-23 restart window and orders resting across the resync); orders the simulator PostOnly-rejected keep their real label (the
features exist only for orders the engine placed, so those orders are dropped and counted)."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from new_backtest.fill_label import FillLabelRecord, compute_ipcw_weights, informative_censoring_check
from new_backtest.fill_model_v1 import FillModelV2, calibration_report
from new_backtest.validation.evaluate import excluded

HORIZONS = (1, 5, 15)
GATE_H = 5
TAG = "MEASURED_SPOT_2026"


def _key(ts: float, side: str, qa: float) -> tuple:
    return (round(ts, 6), side, round(qa, 6))


def records_for_run(run_dir: Path) -> tuple[list[FillLabelRecord], dict]:
    """FillLabelRecords (one per order and horizon) of one coin-day: engine features, real labels."""
    feats: dict[tuple, dict] = {}
    for line in open(run_dir / "engine" / "fill_events.jsonl"):
        e = json.loads(line)
        side = "buy" if str(e["side"]).lower() in ("bid", "buy", "side.bid") else "sell"
        feats.setdefault(_key(e["timestamp"], side, e["queue_ahead"]), {})[int(e["horizon_s"])] = e
    recs, n = [], defaultdict(int)
    for line in open(run_dir / "orders.jsonl"):
        r = json.loads(line)
        n["orders"] += 1
        if r["real_status"] == "Rejected":
            n["real_rejected_not_modelled"] += 1
            continue
        if excluded(r):
            n["excluded"] += 1
            continue
        if r.get("placed_ts") is None:
            n["no_features_sim_rejected_or_skipped"] += 1
            continue
        f = feats.get(_key(r["placed_ts"], r["side"], r["queue_ahead"]))
        if f is None:
            n["no_feature_row"] += 1
            continue
        t_fill = (r["real_first_fill"] - r["created"]) if r.get("real_first_fill") is not None else None
        t_end = (r["real_end"] - r["created"])
        reason = "full_fill" if r["real_status"] == "Filled" else ("partial_then_cancel" if r["real_filled_qty"] > 0 else "cancel")
        for h in HORIZONS:
            e = f.get(h)
            if e is None:
                continue
            filled = t_fill is not None and t_fill <= h
            recs.append(FillLabelRecord(
                timestamp=r["created"], symbol=r["pair"], side="bid" if r["side"] == "buy" else "ask", horizon_s=h, filled=filled,
                relative_to_touch_ticks=int(e["relative_to_touch_ticks"]), queue_ahead=float(e["queue_ahead"]), local_depth=float(e["local_depth"]),
                local_depth_baseline=float(e["local_depth_baseline"]), spread_pct=float(e["spread_pct"]), short_vol=float(e["short_vol"]),
                trade_count=float(e["trade_count"]), trade_sign=float(e["trade_sign"]), regime_bucket=str(e["regime_bucket"]).split(".")[-1].lower(),
                terminal_reason=reason, fill_ratio=(r["real_filled_qty"] / r["qty"]) if r["qty"] else 0.0,
                price_moved_through=bool(e.get("price_moved_through", False)),
                observation_time_s=max(1e-6, t_fill if t_fill is not None else t_end),
                is_censored=(not filled) and r["real_status"] != "Filled" and t_end < h))
        n["used"] += 1
    return recs, dict(n)


def calibration_slope(y, p, w) -> float | None:
    """Weighted logistic recalibration: slope b of logit P(y=1) = a + b * logit(p) (Newton steps). 1 = calibrated spread; None when y has no 0s or no 1s."""
    import numpy as np
    y, p, w = (np.asarray(v, dtype=float) for v in (y, p, w))
    if y.min() == y.max():
        return None
    x = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
    X = np.column_stack([np.ones_like(x), x])
    beta = np.zeros(2)
    for _ in range(50):
        mu = 1 / (1 + np.exp(-(X @ beta)))
        g = X.T @ (w * (y - mu))
        H = (X * (w * mu * (1 - mu))[:, None]).T @ X
        step = np.linalg.solve(H + 1e-9 * np.eye(2), g)
        beta += step
        if np.max(np.abs(step)) < 1e-10:
            break
    return float(beta[1])


def _score(model: FillModelV2, recs: list[FillLabelRecord], base: float, h: int) -> dict:
    hr = [r for r in recs if r.horizon_s == h]
    w = compute_ipcw_weights(hr, h)
    keep = [(r, wi) for r, wi in zip(hr, w) if not r.is_censored and wi > 0]
    if len(keep) < 5:
        return {"n": len(keep), "note": "too few uncensored rows"}
    m = model.per_horizon.get(str(h))
    p = [m.predict(queue_ahead=r.queue_ahead, local_depth=r.local_depth, local_depth_baseline=r.local_depth_baseline, spread_pct=r.spread_pct,
                   short_vol=r.short_vol, trade_count=r.trade_count, trade_sign=r.trade_sign, regime_bucket=r.regime_bucket,
                   relative_to_touch_ticks=r.relative_to_touch_ticks) for r, _ in keep]
    y, w = [float(r.filled) for r, _ in keep], [wi for _, wi in keep]
    rep = calibration_report(y, p, w, baseline_p=[base] * len(keep), n_bins=5)
    return {"n_rows": len(hr), "n_uncensored": len(keep), "censored": sum(1 for r in hr if r.is_censored), "calibration_slope": calibration_slope(y, p, w), **rep}


def _base_rate(recs: list[FillLabelRecord], h: int) -> float:
    hr = [r for r in recs if r.horizon_s == h]
    w = compute_ipcw_weights(hr, h)
    num = sum(wi for r, wi in zip(hr, w) if r.filled)
    den = sum(w)
    return num / den if den else 0.0


def main(argv=None) -> dict:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True); ap.add_argument("--out", required=True); ap.add_argument("--test-day", default="2026-05-02")
    a = ap.parse_args(argv)
    by_day: dict[str, list] = defaultdict(list)
    counts = {}
    for run in sorted(Path(a.runs).glob("*/*/proportional")):
        pair, day = run.parent.parent.name, run.parent.name
        recs, n = records_for_run(run)
        by_day[day] += recs
        counts[f"{pair} {day}"] = n
    days = sorted(by_day)
    out = {"tag": TAG, "label": "EXPLORATORY; not approved as a quote filter", "method": __doc__, "counts": counts, "folds": {}}
    for test in days[1:]:
        fit = [r for d in days if d < test for r in by_day[d]]
        model = FillModelV2.fit(fit, gate_horizon_s=GATE_H, horizons=HORIZONS)
        if model is None:
            out["folds"][test] = {"note": "no model could be fitted"}
            continue
        out["folds"][test] = {"fit_days": [d for d in days if d < test], "n_fit_orders": sum(1 for r in fit if r.horizon_s == GATE_H),
                              "headline": test == a.test_day,
                              "by_horizon": {h: {"base_rate_fit": round(_base_rate(fit, h), 4), **_score(model, by_day[test], _base_rate(fit, h), h)} for h in HORIZONS},
                              "censoring_check_fit": informative_censoring_check(fit, GATE_H)}
    folds = {d: f["by_horizon"][GATE_H] for d, f in out["folds"].items() if "by_horizon" in f}
    head = folds.get(a.test_day, {})
    not_worse = sum(1 for f in folds.values() if f.get("brier_model_minus_baseline", 1) <= 0)
    rel = (head.get("baseline_brier") - head.get("brier")) / head.get("baseline_brier") if head.get("baseline_brier") else None
    slope = head.get("calibration_slope")
    out["verdict"] = {"horizon_s": GATE_H, "test_day": a.test_day, "brier_reduction_vs_base_rate": rel, "brier_reduction_ok": rel is not None and rel >= 0.05,
                      "folds_not_worse": f"{not_worse}/{len(folds)}", "folds_not_worse_ok": bool(folds) and not_worse / len(folds) >= 0.70,
                      "calibration_slope": slope, "calibration_slope_ok": slope is not None and 0.8 <= slope <= 1.2,
                      "rule": "TOLERANCES.md: Brier on the test day >= 5 % below the base rate AND no worse than the base rate on >= 70 % of test days AND calibration slope 0.8-1.2"}
    out["verdict"]["verdict"] = "IMPROVEMENT" if all(out["verdict"][k] for k in ("brier_reduction_ok", "folds_not_worse_ok", "calibration_slope_ok")) else "NO IMPROVEMENT"
    Path(a.out).write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out["verdict"], indent=1, default=str))
    for d, f in out["folds"].items():
        g = f.get("by_horizon", {}).get(GATE_H, {})
        print(d, "fit orders", f.get("n_fit_orders"), "test uncensored", g.get("n_uncensored"), "brier", g.get("brier"), "baseline", g.get("baseline_brier"))
    return out


if __name__ == "__main__":
    main()
