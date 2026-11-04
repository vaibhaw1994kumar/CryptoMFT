"""Queue model v2, step 11 add-on (guide s8): time-to-first-fill under censoring. Kaplan-Meier, inverse-probability-of-censoring weights (IPCW), restricted mean survival time (RMST)
and the diagnostics that say whether the censoring is supported by the data.

Definitions (T = time to first fill, C = time the order stopped being observed without a fill: cancelled by the trader, expired by the time-in-force, or the data ended):
  observed time X = min(T, C), event delta = 1 when X = T (a fill), 0 when censored.
  naive      P(T <= H) = share of ALL orders filled by H: it is the probability UNDER THE ACTUAL CANCEL POLICY, not the survival of a quote left alive.
  KM         S(t) = prod over fill times t_i <= t of (1 - d_i / n_i);  valid when censoring is independent of the fill time given the strata.
  IPCW       F(H) = mean over orders of delta_i * 1(X_i <= H) / G(X_i-), G = Kaplan-Meier of the censoring times (strata: one G per stratum).
  RMST_H     = integral from 0 to H of S(t) dt = E[min(T, H)]  (restricted: a horizon-limited model cannot infer the unobserved tail without assumptions).
Support diagnostics: how many orders were censored before H, the at-risk count at H, the smallest G(X-) used, the largest weight and the effective sample size of the weights.
A fill estimate is flagged `identifiable = False` when the data cannot support it (no censoring information to learn the cancel policy from, G(H) below `min_g`, or fewer than `min_at_risk` orders still at risk at H)."""
from __future__ import annotations

import numpy as np


def _km(x: np.ndarray, event: np.ndarray):
    """Kaplan-Meier. Ties: events are processed before censorings at the same time. Returns (times, survival after each time, at risk before each time)."""
    order = np.lexsort((1 - event, x))                 # sort by time, events first
    x, event = x[order], event[order]
    n = len(x)
    t_out, s_out, risk_out = [], [], []
    s, i = 1.0, 0
    while i < n:
        t = x[i]
        j = i
        d = c = 0
        while j < n and x[j] == t:
            d += int(event[j]); c += int(1 - event[j]); j += 1
        risk = n - i
        if d:
            s *= 1.0 - d / risk
        t_out.append(t); s_out.append(s); risk_out.append(risk)
        i = j
    return np.array(t_out), np.array(s_out), np.array(risk_out)


def _step(t: np.ndarray, s: np.ndarray, at: float, left: bool = False) -> float:
    """Value of a right-continuous step function (1.0 before the first knot) at `at`; left=True gives the left limit s(at-)."""
    k = np.searchsorted(t, at, side="left" if left else "right")
    return 1.0 if k == 0 else float(s[k - 1])


def km_survival(x, event) -> tuple[np.ndarray, np.ndarray]:
    t, s, _ = _km(np.asarray(x, float), np.asarray(event, int))
    return t, s


def rmst(x, event, horizon: float) -> float:
    """E[min(T, H)] from the Kaplan-Meier curve. The curve must reach `horizon` with someone still observable; otherwise the tail is not identified and nan is returned."""
    x, event = np.asarray(x, float), np.asarray(event, int)
    if len(x) == 0 or x.max() < horizon:
        return float("nan")
    t, s, _ = _km(x, event)
    area, prev_t, prev_s = 0.0, 0.0, 1.0
    for ti, si in zip(t, s):
        if ti >= horizon:
            break
        area += prev_s * (ti - prev_t)
        prev_t, prev_s = ti, si
    return float(area + prev_s * (horizon - prev_t))


def ipcw_cdf(x, event, horizon: float, strata=None, min_g: float = 0.05) -> dict:
    """IPCW estimate of P(T <= horizon) with the censoring distribution G estimated by Kaplan-Meier per stratum, plus the weight diagnostics."""
    x, event = np.asarray(x, float), np.asarray(event, int)
    n = len(x)
    strata = np.zeros(n, dtype=int) if strata is None else np.asarray(strata)
    w = np.zeros(n)
    for g in np.unique(strata):
        m = strata == g
        tc, sc, _ = _km(x[m], 1 - event[m])             # censoring distribution: censorings are the "events"
        for idx in np.where(m)[0]:
            if event[idx] == 1 and x[idx] <= horizon:
                w[idx] = 1.0 / max(_step(tc, sc, x[idx], left=True), 1e-12)
    used = w[w > 0]
    g_min = float(1.0 / used.max()) if len(used) else 1.0
    ess = float(used.sum() ** 2 / (used ** 2).sum()) if len(used) else 0.0
    est = float(min(1.0, w.sum() / n)) if n else float("nan")
    n_cens = int(((event == 0) & (x <= horizon)).sum())
    return {"cdf": est, "naive": float(((event == 1) & (x <= horizon)).mean()) if n else float("nan"), "n": n, "n_filled_by_H": int(len(used)),
            "n_censored_before_H": n_cens, "share_censored_before_H": n_cens / n if n else float("nan"), "at_risk_at_H": int((x >= horizon).sum()),
            "min_G": g_min, "max_weight": float(used.max()) if len(used) else 1.0, "effective_sample_size_of_weights": ess,
            "identifiable": bool(n_cens > 0 and g_min >= min_g)}


def diagnostics(x, event, horizons=(1.0, 5.0, 15.0, 60.0), strata=None, min_at_risk: int = 20) -> dict:
    """One row per horizon: naive, IPCW and KM estimates of P(T <= H), RMST, and the support flags."""
    x, event = np.asarray(x, float), np.asarray(event, int)
    out = {"n": int(len(x)), "n_events": int(event.sum()), "n_censored": int((1 - event).sum()),
           "share_censored": float((1 - event).mean()) if len(x) else float("nan"), "horizons": {}}
    for h in horizons:
        r = ipcw_cdf(x, event, h, strata)
        t, s, _ = _km(x, event) if len(x) else (np.array([]), np.array([]), None)
        r["km_cdf"] = float(1.0 - _step(t, s, h)) if len(x) else float("nan")
        r["rmst"] = rmst(x, event, h)
        r["identifiable"] = bool(r["identifiable"] and r["at_risk_at_H"] >= min_at_risk)
        out["horizons"][f"{h:g}s"] = r
    return out
