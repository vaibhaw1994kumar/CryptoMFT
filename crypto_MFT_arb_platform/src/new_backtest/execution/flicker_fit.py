"""Flicker estimation and fit (phase 1). The measurement rule is stated once here and recorded in the output file.

Sample = one hedge ATTEMPT from a capture replay (ArbBacktestConfig.record_hedge_attempts). In FAVOURABLE bps:
    sell hedge:  F = (arrival bid  - fundamental bid) / fundamental bid * 1e4        (the bid moving up is good)
    buy  hedge:  F = (fundamental ask - arrival ask)  / fundamental ask  * 1e4       (the ask moving down is good)
fundamental = the MEDIAN of the strategy-view touch over the 0.25 s before the decision (a move that reverts inside the decision
window does not move a median: this is the reversion filter; the paper filters by spread width, we filter in time because the
Jan-2022 books are tick-discrete). arrival = the touch of the book the hedge actually matches against at its arrival, which in
this engine is the strategy view (`_execute_hedge_with_faults`, `_execute_arrival` with truth books off), so the measured law
contains the feed-staleness the hedge really faces. Volatility = the 1 s range of the view mid at decision (bps).

Fit (maximum likelihood for the mixture): p0 = share with |F| <= tol, p+ / p- = shares above / below, eta = 1 / mean tail size.
Cells fall back cell -> side x tercile -> side -> pooled when n < MIN_N (the same n >= 30 gate used for percentiles).
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict

import numpy as np

MIN_N = 30
MIN_TAIL = 5
TOL_BPS = 0.005
HOUR_BUCKET_H = 8
QQ_P = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)


def flicker_bps(row: dict) -> float | None:
    side = row["side"]
    if side == "sell":
        f, a = row.get("fund_bid"), row.get("arr_bid")
        if not f or not a or f <= 0 or a <= 0:
            return None
        return (a - f) / f * 1e4
    f, a = row.get("fund_ask"), row.get("arr_ask")
    if not f or not a or f <= 0 or a <= 0:
        return None
    return (f - a) / f * 1e4


def samples(rows) -> list[dict]:
    out = []
    for r in rows:
        f = flicker_bps(r)
        if f is None or r.get("vol_bps_1s") is None:
            continue
        out.append({"side": r["side"], "F": f, "vol": r["vol_bps_1s"], "bucket": int(r["hour_utc"]) // HOUR_BUCKET_H})
    return out


def fit_mixture(F) -> dict | None:
    F = np.asarray(F, dtype=float)
    n = F.size
    if n < MIN_N:
        return None
    pos, neg = F[F > TOL_BPS], -F[F < -TOL_BPS]
    p_plus, p_minus = pos.size / n, neg.size / n
    p0 = 1.0 - p_plus - p_minus
    if pos.size < MIN_TAIL or neg.size < MIN_TAIL:
        return None
    return {"p0": p0, "p_plus": p_plus, "p_minus": p_minus, "eta_plus": 1.0 / float(pos.mean()), "eta_minus": 1.0 / float(neg.mean())}


def cdf(fit: dict, x: np.ndarray) -> np.ndarray:
    """CDF of the mixture at x (the atom at 0 is included for x >= 0)."""
    x = np.asarray(x, dtype=float)
    neg = fit["p_minus"] * np.exp(fit["eta_minus"] * np.minimum(x, 0.0))                       # P(F <= x) for x < 0: p- exp(eta- x)
    below = np.where(x < 0, neg, fit["p_minus"] + fit["p0"] + fit["p_plus"] * (1 - np.exp(-fit["eta_plus"] * np.maximum(x, 0.0))))
    return below


def quantile(fit: dict, p: float) -> float:
    if p <= fit["p_minus"]:
        return math.log(max(p / fit["p_minus"], 1e-300)) / fit["eta_minus"] if fit["p_minus"] > 0 else 0.0
    if p <= fit["p_minus"] + fit["p0"]:
        return 0.0
    q = (p - fit["p_minus"] - fit["p0"]) / fit["p_plus"]
    return -math.log(max(1.0 - q, 1e-300)) / fit["eta_plus"]


def ks(fit: dict, F) -> float:
    """KS distance between the empirical distribution (ties handled: the atom at 0 is one jump) and the fitted mixture."""
    Fs = np.sort(np.asarray(F, dtype=float))
    n = Fs.size
    right = np.searchsorted(Fs, Fs, side="right") / n
    left = np.searchsorted(Fs, Fs, side="left") / n
    return float(max(np.abs(right - cdf(fit, Fs)).max(), np.abs(left - cdf(fit, Fs - 1e-9)).max()))


def qq(fit: dict, F) -> list[dict]:
    F = np.asarray(F, dtype=float)
    return [{"p": p, "empirical": float(np.quantile(F, p)), "fitted": quantile(fit, p)} for p in QQ_P]


def _cell(F) -> dict:
    fit = fit_mixture(F)
    return {"n": int(len(F)), "fit": fit, "ks": ks(fit, F) if fit else None, "qq": qq(fit, F) if fit else None,
            "mean_F": float(np.mean(F)) if len(F) else None, "p10_F": float(np.quantile(F, 0.1)) if len(F) else None}


def fit_set(train_rows) -> dict:
    """All cells for one training set. Terciles are cut on the training volatilities (edges stored for lookup)."""
    s = samples(train_rows)
    vols = np.array([x["vol"] for x in s])
    edges = [float(np.quantile(vols, 1 / 3)), float(np.quantile(vols, 2 / 3))] if len(s) >= 3 else [0.0, 0.0]
    for x in s:
        x["t"] = int(x["vol"] > edges[0]) + int(x["vol"] > edges[1])
    groups: dict[str, list[float]] = defaultdict(list)
    for x in s:
        groups["pooled"].append(x["F"])
        groups[x["side"]].append(x["F"])
        groups[f"{x['side']}|t{x['t']}"].append(x["F"])
        groups[f"{x['side']}|b{x['bucket']}|t{x['t']}"].append(x["F"])
    return {"n_samples": len(s), "vol_edges_bps": edges, "cells": {k: _cell(v) for k, v in sorted(groups.items())}}


def content_sha256(doc: dict) -> str:
    core = {k: v for k, v in doc.items() if k != "content_sha256"}
    return hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()
