"""Named fault scenarios for strategy-level backtests: clean, realistic, stressed.

    clean           no injection, no PostOnly model, venue-preset latencies: the original backtester. The strategy's answer to a
                    failed hedge is the legacy oracle (an instant, latency-free flatten).
    clean_managed   nothing is injected and nothing is modelled, but hedge failures that DO arise (a real depth miss) are handled by
                    the production-like response below. It separates "the response policy" from "the faults" in comparisons.
    realistic       what config/lifecycle_calibration_bybit.json measured on real orders, and nothing that it did not:
                      * PostOnly rejects modelled from the book (postonly_reject_model="on");
                      * passive-order send-to-arrival latency sampled from the measured distribution (period "all");
                      * non-PostOnly rejects: 0 observed in the window, so reject_prob is 0 (the 95% upper bound is reported);
                      * disconnects: the data cannot see them. The only evidence is the activity gaps with an order open across
                        them; their count over the window and their durations are used as an upper-bound reading, on bybit
                        only (there are no binance logs at all);
                      * cancel latency: not in the logs, the venue preset is kept.
    stressed        realistic made worse in the ways a desk fears, each by a stated factor (see STRESS below).

Every non-clean preset answers a failed hedge with retry (twice, 0.25 s apart) then unwind on the maker venue, with the entry
guard on, so differences between presets are differences in the environment, not in the policy.

The calibration comes from 2026-04 / 2026-05 and the default replay data from 2022-01: the calibration POST-dates the test data.
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import BINANCE, BYBIT
from .hedge_faults import DisconnectSpec, HedgeFaultConfig
from .postonly import AckLatencyConfig

PRESETS = ("clean", "clean_managed", "realistic", "stressed")

# How the stressed preset departs from the realistic one.
STRESS = {
    "latency_x": 5.0,                       # ack latency samples, hedge entry latency, cancel latency
    "reject_prob_x": 5.0,                   # times the 95% upper bound (rule of three) of the observed non-PostOnly reject rate
    "fast_move_bps": 5.0, "fast_move_window_s": 1.0, "fast_move_reject_prob": 0.5,   # ~p99-p99.9 of the hedge venue's 1 s mid move (SOLUSDT 2022-01-05/13)
    "disconnect_rate_per_day": 2.0, "disconnect_guaranteed_per_day": 1, "disconnect_median_s": 300.0, "disconnect_sigma": 0.5,
    "ack_unknown_prob": 0.002, "unknown_resolve_s": 10.0,
}
POLICY = dict(response="retry_unwind", max_retries=2, retry_backoff_s=0.25, entry_guard=True, outage_policy="blackhole", ack_timeout_s=2.0)


def _read(calibration_path: str | Path) -> dict:
    return json.loads(Path(calibration_path).read_text())


def calibration_inputs(calibration_path: str | Path) -> dict:
    doc = _read(calibration_path)
    rej = doc["rejects"]["other_reasons"]
    gaps = doc["disconnect_candidates"]
    win_days = gaps["window_seconds"] / 86400.0
    cand = [g["seconds"] for g in gaps["longest_with_an_order_open_across"] if g["seconds"] > 60.0]
    n_over_60 = gaps["by_threshold"][">60s"]["with_an_order_open_across"]
    return {
        "reject_prob_observed": rej["rate_over_all_orders_in_window"]["rate"],
        "reject_prob_upper_95": rej["upper_bound_95_rule_of_three"],
        "disconnect_candidates_over_60s": n_over_60,
        "disconnect_window_days": win_days,
        "disconnect_rate_per_day": n_over_60 / win_days,
        "disconnect_durations_s": tuple(round(x, 1) for x in cand[:n_over_60]),
        "n_orders_in_window": doc["structure"]["unfilled_window"]["orders_in_window"],
        "n_ack_samples_all": doc["ack_latency_proxy"]["empirical_quantile_grids"]["all"]["n"],
        "calibration_sha256": doc["meta"]["content_sha256"],
    }


def build(name: str, *, seed: int, calibration_path: str | Path) -> dict:
    """ArbBacktestConfig keyword overrides for a preset (empty for clean)."""
    if name not in PRESETS:
        raise ValueError(f"preset must be one of {PRESETS}, got {name!r}")
    if name == "clean":
        return {}
    if name == "clean_managed":
        return {"hedge_faults": HedgeFaultConfig(seed=seed, label=name, **POLICY)}
    c = calibration_inputs(calibration_path)
    cal = str(calibration_path)
    if name == "realistic":
        faults = HedgeFaultConfig(
            seed=seed, label=name, **POLICY, reject_prob=0.0,
            disconnects=(DisconnectSpec(BYBIT, rate_per_day=c["disconnect_rate_per_day"], durations_s=c["disconnect_durations_s"], kind="gateway"),),
        )
        return {"postonly_reject_model": "on", "ack_latency": AckLatencyConfig("empirical", calibration_path=cal, period="all", seed=seed + 5),
                "hedge_faults": faults}
    s = STRESS
    p = s["reject_prob_x"] * c["reject_prob_upper_95"]
    faults = HedgeFaultConfig(
        seed=seed, label=name, **POLICY, reject_prob=p, reject_prob_by_venue=((BYBIT, p), (BINANCE, p)), latency_scale=s["latency_x"],
        fast_move_reject_prob=s["fast_move_reject_prob"], fast_move_bps=s["fast_move_bps"], fast_move_window_s=s["fast_move_window_s"],
        ack_unknown_prob=s["ack_unknown_prob"], unknown_resolve_s=s["unknown_resolve_s"],
        disconnects=tuple(DisconnectSpec(v, rate_per_day=s["disconnect_rate_per_day"], guaranteed_per_day=s["disconnect_guaranteed_per_day"],
                                         median_s=s["disconnect_median_s"], sigma=s["disconnect_sigma"], kind="gateway") for v in (BYBIT, BINANCE)),
    )
    return {"postonly_reject_model": "on",
            "ack_latency": AckLatencyConfig("empirical", calibration_path=cal, period="all", scale=s["latency_x"], seed=seed + 5),
            "hedge_faults": faults}


def describe(name: str, *, seed: int, calibration_path: str | Path) -> dict:
    """The exact parameters of a preset, as they will be applied (for reports)."""
    kw = build(name, seed=seed, calibration_path=calibration_path)
    out: dict = {"preset": name, "seed": seed, "postonly_reject_model": kw.get("postonly_reject_model", "off")}
    a = kw.get("ack_latency")
    out["ack_latency"] = ({"source": a.source, "period": a.period, "scale": a.scale, "max_ms": a.max_ms} if a else {"source": "preset"})
    f = kw.get("hedge_faults")
    out["hedge_faults"] = f.as_dict() if f is not None else None
    if name in ("realistic", "stressed"):
        out["calibration_inputs"] = calibration_inputs(calibration_path)
    if name == "stressed":
        out["stress_factors"] = dict(STRESS)
    return out
