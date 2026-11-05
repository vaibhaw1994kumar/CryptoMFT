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

from .latency_model import BINANCE, BYBIT
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
        venue_reject_applies_to_hedges=False,       # WP6: hedges carry reject_prob once; the by-venue rate is for passive orders
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


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# PERP presets (perp_perp_maker_taker_v1): parameters from config/lifecycle_calibration_perp_bybit.json (+ _binance.json), every one with its source.
# The profile already fixes the response policy (retry_unwind, 15 bps unwind band, 3 attempts, entry guard, circuit breaker ON), so a preset only
# changes the ENVIRONMENT. In this profile `clean` and `clean_managed` are the same configuration by construction (the legacy oracle response is
# forbidden), so the pair is a determinism check, not two scenarios.
#
# Source tags:
#   MEASURED_TESTNET_MAC   measured by ollog on the venue TESTNET from the Mac (n and host stated); not mainnet, not the VM
#   MEASURED_2022_DATA     measured on the replay data itself (config/fast_move_thresholds.json)
#   RULE_OF_THREE_BOUND    zero events seen in n trials: the 95% upper bound 3/n (a bound, not a measurement)
#   SCENARIO_CHOICE        chosen by the assistant as a stress, not measured
#   CARRIED_FROM_29_OCT    the value the 29 Oct spot study used for the same stress
#   PROFILE                set by the profile and not changed by the preset
# ═══════════════════════════════════════════════════════════════════════════════════════════════════
PERP_PRESETS = ("clean", "clean_managed", "realistic", "stressed")
PERP_STRESS = {"latency_x": 5.0, "fast_move_reject_prob": 0.5, "disconnect_rate_per_day": 2.0, "disconnect_guaranteed_per_day": 1,
               "disconnect_median_s": 300.0, "disconnect_sigma": 0.5, "ack_unknown_prob": 0.002, "unknown_resolve_s": 10.0}


def perp_calibration_inputs(maker_path: str | Path, hedge_path: str | Path) -> dict:
    mk, hg = _read(maker_path), _read(hedge_path)
    out: dict = {"maker_file_sha256": mk["meta"]["content_sha256"], "hedge_file_sha256": hg["meta"]["content_sha256"]}
    for tag, d in (("maker", mk), ("hedge", hg)):
        t = d["networks"]["testnet"]
        nat = t["rejects"]["natural_postonly"]
        n, k = nat["n_orders"], nat["n_rejects"]
        out[tag] = {"venue": d["venue"], "network": "testnet", "natural_orders": n, "non_postonly_or_postonly_rejects_among_them": k,
                    "reject_prob_observed": k / n if n else None, "reject_prob_upper95_rule_of_three": (3.0 / n) if n and k == 0 else None,
                    "observation_seconds": t["observation"]["seconds_between_first_and_last_order_summed_over_runs"],
                    "disconnects_seen": t["cancel_fill_races_and_disconnects"]["stream_or_capture_reconnect_events"],
                    "ack_latency_samples_n": (d["ack_latency_proxy"]["empirical_quantile_grids"].get("all") or {}).get("n")}
        o = out[tag]
        o["disconnect_rate_upper95_per_day_rule_of_three"] = (3.0 / o["observation_seconds"] * 86400.0) if o["disconnects_seen"] == 0 and o["observation_seconds"] else None
    return out


def fast_move_p99(path: str | Path, day: str) -> float:
    return float(_read(path)["days"][day]["p99"])


def perp_preset(name: str, *, seed: int, maker_calibration: str | Path, hedge_calibration: str | Path, fast_move_path: str | Path, day: str) -> dict:
    """{'ack_latency': AckLatencyConfig, 'faults': {HedgeFaultConfig field: value}, 'sources': {param: {value, source, note}}}."""
    if name not in PERP_PRESETS:
        raise ValueError(f"perp preset must be one of {PERP_PRESETS}, got {name!r}")
    c = perp_calibration_inputs(maker_calibration, hedge_calibration)
    src: dict = {}

    def put(param, value, source, note=""):
        src[param] = {"value": value, "source": source, "note": note}

    lat_scale = PERP_STRESS["latency_x"] if name == "stressed" else 1.0
    ack = AckLatencyConfig("empirical", calibration_path=str(maker_calibration), period="all", scale=lat_scale, seed=seed + 5)
    put("ack_latency (maker venue arrival)", f"empirical grid 'all' x{lat_scale:g}", "MEASURED_TESTNET_MAC" if lat_scale == 1.0 else "SCENARIO_CHOICE",
        f"REST round trip / 2, local clock only, n={c['maker']['ack_latency_samples_n']}, Bybit linear TESTNET from the Mac" + ("; stress factor is a scenario choice" if lat_scale != 1.0 else ""))
    f: dict = {"seed": seed, "label": name}
    put("response policy (retry 2, unwind band, attempts, entry guard, breaker)", "profile", "PROFILE")
    if name in ("clean", "clean_managed"):
        put("faults", "none", "PROFILE", "nothing injected; arrival latency still sampled from the measured grid (the profile's PostOnly model needs one)")
        return {"ack_latency": ack, "faults": f, "sources": src, "calibration_inputs": c}
    put("reject_prob (non-PostOnly rejects)", 0.0, "MEASURED_TESTNET_MAC",
        f"0 rejects in {c['maker']['natural_orders']} (Bybit) / {c['hedge']['natural_orders']} (Binance) natural PostOnly/GTX orders; upper 95% bounds "
        f"{c['maker']['reject_prob_upper95_rule_of_three']:.3f} / {c['hedge']['reject_prob_upper95_rule_of_three']:.3f} are NOT applied in 'realistic'")
    put("disconnects", "none", "MEASURED_TESTNET_MAC",
        f"{c['maker']['disconnects_seen']} reconnects in {c['maker']['observation_seconds'] / 60:.0f} min of logging (Bybit): no information on a rate (95% bound "
        f"{c['maker']['disconnect_rate_upper95_per_day_rule_of_three']:.0f}/day), so none is injected")
    if name == "realistic":
        f.update(reject_prob=0.0)
        return {"ack_latency": ack, "faults": f, "sources": src, "calibration_inputs": c}
    s = PERP_STRESS
    p_hedge, p_maker = c["hedge"]["reject_prob_upper95_rule_of_three"], c["maker"]["reject_prob_upper95_rule_of_three"]
    thr = fast_move_p99(fast_move_path, day)
    f.update(reject_prob=p_hedge, reject_prob_by_venue=((BYBIT, p_maker),), latency_scale=s["latency_x"], venue_reject_applies_to_hedges=False,
             fast_move_reject_prob=s["fast_move_reject_prob"], fast_move_bps=thr, fast_move_window_s=1.0,
             ack_unknown_prob=s["ack_unknown_prob"], unknown_resolve_s=s["unknown_resolve_s"],
             disconnects=tuple(DisconnectSpec(v, rate_per_day=s["disconnect_rate_per_day"], guaranteed_per_day=s["disconnect_guaranteed_per_day"],
                                              median_s=s["disconnect_median_s"], sigma=s["disconnect_sigma"], kind="gateway") for v in (BYBIT, BINANCE)))
    put("reject_prob on hedges (applied ONCE)", p_hedge, "RULE_OF_THREE_BOUND", f"Binance natural orders n={c['hedge']['natural_orders']}, 0 rejects; 1x the bound (the 29 Oct stress used 5x)")
    put("reject_prob on passive orders, Bybit", p_maker, "RULE_OF_THREE_BOUND", f"Bybit natural orders n={c['maker']['natural_orders']}, 0 rejects; venue_reject_applies_to_hedges=False so hedges are not hit twice")
    put("latency_scale (hedge entry, cancel)", s["latency_x"], "CARRIED_FROM_29_OCT", "x5; the Binance entry grid is measured but not wired into hedge entry (the engine samples it from the latency preset)")
    put("fast_move hedge reject", f"p={s['fast_move_reject_prob']} when |1 s mid move| >= {thr:g} bps", "SCENARIO_CHOICE",
        f"threshold = measured p99 of the hedge venue's 1 s move on {day} (MEASURED_2022_DATA); the probability is a scenario choice")
    put("disconnects", f"{s['disconnect_rate_per_day']:g}/day (>=1 guaranteed), median {s['disconnect_median_s']:g} s, both venues", "CARRIED_FROM_29_OCT", "not measured")
    put("ack_unknown_prob", s["ack_unknown_prob"], "CARRIED_FROM_29_OCT", f"unknown_resolve_s={s['unknown_resolve_s']:g}")
    return {"ack_latency": ack, "faults": f, "sources": src, "calibration_inputs": c}


# Conditional scenarios: injected AT a trigger rather than at random. Parameters are the scenario itself (SCENARIO_CHOICE) except the fast-move threshold.
CONDITIONAL = ("outage_binance_2s", "outage_binance_30s", "outage_binance_5min", "outage_bybit_2s", "outage_bybit_30s", "outage_bybit_5min",
               "hedge_reject_fast_move", "lost_ack_hedge", "ghost_hedge", "partial_hedge", "failed_unwind", "restart")


def conditional_scenario(name: str, *, day: str, fast_move_path: str | Path) -> dict:
    if name not in CONDITIONAL:
        raise ValueError(f"conditional scenario must be one of {CONDITIONAL}, got {name!r}")
    if name.startswith("outage_"):
        _, venue, dur = name.split("_")
        secs = {"2s": 2.0, "30s": 30.0, "5min": 300.0}[dur]
        return {"outage_on_fill_s": secs, "outage_on_fill_venue": "hedge" if venue == "binance" else "maker", "outage_policy": "blackhole", "ack_timeout_s": 2.0}
    if name == "hedge_reject_fast_move":
        return {"fast_move_bps": fast_move_p99(fast_move_path, day), "fast_move_window_s": 1.0, "fast_move_reject_prob": 1.0}
    if name == "lost_ack_hedge":
        return {"ack_unknown_prob": 1.0, "ack_unknown_scope": "hedge", "ack_timeout_s": 2.0, "unknown_resolve_s": 10.0}
    if name == "ghost_hedge":
        return {"outage_on_fill_s": 30.0, "outage_on_fill_venue": "hedge", "outage_policy": "ghost", "ack_timeout_s": 2.0}
    if name == "partial_hedge":
        return {"partial_prob": 1.0, "partial_fraction": 0.5}
    if name == "failed_unwind":
        # every hedge is refused AND the maker venue (where the unwind goes) is dark for 120 s from the fill: the unwind attempts are lost
        return {"reject_prob": 1.0, "outage_on_fill_s": 120.0, "outage_on_fill_venue": "maker", "outage_policy": "blackhole", "ack_timeout_s": 2.0}
    return {"restart_s": 60.0, "restart_at_fills": (5, 30)}
