"""e2e perp-perp exploratory replay (SOLUSDT Jan-2022, Bybit linear perp maker <-> Binance USD-M taker). One run per invocation.

usage: e2e_run.py PROFILE DAY [--fee bybit_perp_mm|bybit_perp_vip0] [--faults clean|realistic|stressed] [--seed N]
                  [--latency singapore] [--train D1,D2,...] [--allow-rejected-data WHY]
PROFILE is a profiles/<name>.yaml in the frozen snapshot (snap/). The latency preset is OVERRIDDEN to --latency (default singapore)
in both the profile's latency_preset and timing.from_latency_preset: the YAML files are not edited.
Training (v1): expected hedge move per side = pooled executed hedge move of legacy_v0 runs (same latency, clean, mm fees) of
strictly earlier days from this output root. p_fail stays the profile's PLACEHOLDER.
Output: out/e2e_perp/<profile>/<latency>_<fee>_<faults>_s<seed>/<day>/summary.json"""
import argparse, calendar, dataclasses, json, os, sys, time, warnings
from pathlib import Path

import os
SNAP = Path(os.environ["EXEC_SNAP"]).resolve()          # frozen code snapshot to run
# queue model v2 runner: QV2_MAKER (JSON, MakerConfig overrides), QV2_CFG (JSON, ArbBacktestConfig overrides), QV2_TAG, EXEC_OUT
sys.path.insert(0, str(SNAP / "src"))
from new_backtest import fault_presets
from new_backtest.backtester import CrossVenueArbBacktester
from new_backtest.profiles import apply_profile, load_profile, switch_table

ROOT = Path(os.environ["EXEC_OUT"]).resolve()                       # output root of this label
TRAIN_ROOT = Path("/Users/vaibhav/Documents/Newproject/out/e2e_perp")   # read-only: pinned training input (legacy_v0 clean replays of 2 Nov)
ap = argparse.ArgumentParser()
ap.add_argument("profile", nargs="?", default="perp_perp_maker_taker_v1_1"); ap.add_argument("day")
ap.add_argument("--reference", action="store_true", help="REFERENCE_ONLY: required to run legacy_v0 or perp_perp_maker_taker_v1 (never for decisions)")
ap.add_argument("--fee", default="bybit_perp_mm")
ap.add_argument("--faults", default="clean", choices=("clean", "realistic", "stressed"))
ap.add_argument("--seed", type=int, default=1)
ap.add_argument("--latency", default="singapore")
ap.add_argument("--train", default="")
ap.add_argument("--allow-rejected-data", default=None)
a = ap.parse_args()
if a.profile in ("legacy_v0", "perp_perp_maker_taker_v1") and not a.reference:
    raise SystemExit(f"{a.profile} is a REFERENCE profile, never for decisions: pass --reference to run it (the default profile is perp_perp_maker_taker_v1_1)")
tag = f"{a.latency}_{a.fee}_{a.faults}_s{a.seed}" + (("_" + os.environ["EXEC_TAG"]) if os.environ.get("EXEC_TAG") else "")
out = ROOT / a.profile / tag / a.day
out.mkdir(parents=True, exist_ok=True)

doc = load_profile(SNAP / "profiles" / f"{a.profile}.yaml")
profile_latency = doc.get("latency_preset")
doc["latency_preset"] = a.latency
if isinstance(doc.get("timing"), dict):
    doc["timing"]["from_latency_preset"] = a.latency
doc.setdefault("fee_presets", {})["bybit"] = a.fee
if os.environ.get("EXEC_HEDGE"):                    # phase-1 policy cells: override the profile's execution.hedge block for this run
    doc.setdefault("execution", {}).setdefault("hedge", {}).update(json.loads(os.environ["EXEC_HEDGE"]))
if os.environ.get("EXEC_TAG"):
    pass

def _hm_training(path, run_day):
    """v1.1 step 9a: the expected hedge move from MARKET DATA (new_backtest.market_hedge_move) on the TRAIN days, never from simulated fills."""
    d = json.loads(Path(path).read_text())
    assert d["days"] and all(x < run_day for x in d["days"]), "training days must be strictly before the run day"
    mv = d["expected_hedge_move_bps"]
    return ({"bid": float(mv["bid"]), "ask": float(mv["ask"])},
            {"days": d["days"], "pooled_n": {s: d["stats"][s]["n"] for s in ("bid", "ask")}, "status": "trained", "source": "MARKET DATA only: " + d["method"],
             "file": str(path), "horizon_s": d["horizon_s"], "stats": d["stats"]})

training = {"days": [], "status": "none"}
moves = {"bid": 0.0, "ask": 0.0}
if a.profile != "legacy_v0" and os.environ.get("QV2_HEDGE_MOVE_FILE"):
    moves, training = _hm_training(os.environ["QV2_HEDGE_MOVE_FILE"], a.day)
elif a.profile != "legacy_v0":
    days = [d for d in a.train.split(",") if d]
    assert all(d < a.day for d in days), "training days must be strictly before the test day"
    pooled = {"bid": [0.0, 0], "ask": [0.0, 0]}
    used = []
    for d in days:
        p = TRAIN_ROOT / "legacy_v0" / f"{a.latency}_bybit_perp_mm_clean_s1" / d / "summary.json"
        if not p.exists():
            continue
        used.append(d)
        leg = json.loads(p.read_text())["legging"]
        for side in pooled:
            ex = leg.get("hedge_exec_move_bps", {}).get(side, {})
            if ex.get("n"):
                pooled[side][0] += ex["n"] * ex["mean"]; pooled[side][1] += ex["n"]
    moves = {s: (v[0] / v[1] if v[1] else 0.0) for s, v in pooled.items()}
    training = {"days": used, "requested": days, "pooled_n": {s: v[1] for s, v in pooled.items()},
                "status": "trained" if used else "NO_TRAINING_DAY (move 0 = no information)",
                "source": "legacy_v0 replays of 2 Nov (out/e2e_perp), same latency preset, clean, bybit_perp_mm; pinned input shared by every label"}

kw = dict(data_root=os.environ.get("QV2_DATA_ROOT", "/Users/vaibhav/Documents/Newproject/data"), pair="SOLUSDT", start_date=a.day, end_date=a.day,
          output_dir=str(out), funding_rate_dir="/Users/vaibhav/Documents/Newproject/out/funding/SOLUSDT", max_trade_log=10 ** 7,
          equity_bridge="warn", record_exposure=True)
if a.allow_rejected_data:
    kw["allow_rejected_data"] = a.allow_rejected_data
cfg = apply_profile(doc, **kw)
if os.environ.get("QV2_VENUE_TICKS"):
    cfg.arb.venue_ticks = {k: float(v) for k, v in json.loads(os.environ["QV2_VENUE_TICKS"]).items()}      # declared per dataset (v1.1 step 9b); the engine checks the first snapshot against it
if os.environ.get("QV2_MAKER_VENUES"):
    cfg.arb.maker_venues = tuple(json.loads(os.environ["QV2_MAKER_VENUES"]))                                # v1.1 step 9d: a data-dependent choice, set by the runner
cfg.arb.expected_hedge_move_bps_bid = moves["bid"]
cfg.arb.expected_hedge_move_bps_ask = moves["ask"]
cfg.arb.block_on_toxic = True
cfg.arb.max_cross_venue_skew_s = 0.2
cfg.arb.fill_gate_horizon_s = 5
cal = SNAP / "config" / "lifecycle_calibration_bybit.json"
fault_desc = None
if a.faults != "clean":
    over = fault_presets.build(a.faults, seed=a.seed, calibration_path=cal)
    fault_desc = fault_presets.describe(a.faults, seed=a.seed, calibration_path=cal)
    if a.profile != "legacy_v0" and cfg.hedge_faults is not None:
        # keep the profile's response policy (band, attempts, recovery); take the preset's injected faults
        hf_over = over["hedge_faults"]                 # shallow: asdict() would turn the nested DisconnectSpec tuples into dicts
        inj = {fl.name: getattr(hf_over, fl.name) for fl in dataclasses.fields(hf_over)
               if fl.name not in ("unwind_band_bps", "unwind_max_attempts", "unknown_query_max_attempts")}
        over["hedge_faults"] = dataclasses.replace(cfg.hedge_faults, **inj)
        # the presets' reject/disconnect rates come from the SPOT real-order calibration (the perp file has no reject section);
        # entry/ack latency stays the profile's PERP calibration, scaled as the preset says
        if cfg.ack_latency is not None and over.get("ack_latency") is not None:
            over["ack_latency"] = dataclasses.replace(over["ack_latency"], calibration_path=cfg.ack_latency.calibration_path)
        fault_desc["approximation"] = ("reject/disconnect rates from the SPOT real-order calibration (lifecycle_calibration_bybit.json); "
                                       "ack latency from the profile's perp testnet calibration")
    for k, v in over.items():
        setattr(cfg, k, v)

import json as _json
if os.environ.get("QV2_MAKER"):
    cfg.maker = dataclasses.replace(cfg.maker, **_json.loads(os.environ["QV2_MAKER"]))
for _k, _v in _json.loads(os.environ.get("QV2_CFG", "{}")).items():
    setattr(cfg, _k, _v)
if os.environ.get('EXEC_CAPTURE'):
    cfg.record_hedge_attempts = True                 # opt-in hedge capture (no effect on the replay)
import os
if os.environ.get('E2E_DRYRUN'):
    print('DRYRUN', a.profile, tag, cfg.latency_preset, cfg.timing and cfg.timing.__class__.__name__, cfg.bybit_fee_preset, cfg.postonly_reject_model, cfg.ack_latency, cfg.hedge_faults and cfg.hedge_faults.reject_prob, cfg.circuit_breaker is not None, cfg.margin is not None); raise SystemExit(0)
t0 = time.time()
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    _bt = CrossVenueArbBacktester(cfg)
    r = _bt.run()
hr = r.hedge_report or {}
c = hr.get("counters", {})
ex = hr.get("exposure", {})
br = hr.get("circuit_breaker") or {}
d0 = calendar.timegm(time.strptime(a.day, "%Y-%m-%d"))
sl = r.signal_log or []


def dist(xs):
    xs = sorted(x for x in xs if x is not None)
    q = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None
    return {"n": len(xs), "mean": (sum(xs) / len(xs)) if xs else None, "p10": q(0.1), "p50": q(0.5), "p90": q(0.9)}


mg = r.margin_report or {}
summary = {
    "label": "EXPLORATORY (fewer than 8 FULL two-venue days)", "day": a.day, "profile": a.profile, "tag": tag,
    "size_base": cfg.arb.max_position_base, "fee_presets": {"bybit": cfg.bybit_fee_preset, "binance": cfg.binance_fee_preset},
    "latency_preset": a.latency, "profile_latency_preset_in_yaml": profile_latency, "fault_scenario": a.faults, "seed": a.seed,
    "fault_description": fault_desc, "hedge_arrival_book": cfg.hedge_arrival_book, "venue_ticks_declared": dict(getattr(cfg.arb, "venue_ticks", {})), "tick_size_used": dict(_bt.tick_size), "maker_venues": list(getattr(cfg.arb, "maker_venues", ())), "round_quotes_to_tick": getattr(cfg.arb, "round_quotes_to_tick", False), "strategy_rejects": dict(getattr(getattr(_bt, "strategy", None), "rejects", {}) or {}), "training": training, "expected_hedge_move_bps_used": moves,
    "p_fail": cfg.arb.p_fail, "p_fail_status": "PLACEHOLDER_NOT_MEASURED", "wall_s": None,
    "signals": r.signals_generated, "signals_completed": r.signals_completed, "fills": r.orders_filled, "orders_sent": r.orders_sent,
    "signals_unhedged": r.signals_unhedged, "fees_paid": r.fees_paid, "funding_paid": r.funding_paid, "gross_pnl": r.gross_pnl,
    "cash_pnl": r.cash_pnl, "ending_equity_common_timestamp": r.ending_equity, "last_mark_equity": r.last_mark_equity,
    "attribution_pnl": r.attribution_pnl, "final_inventory": r.final_inventory, "traded_notional": r.traded_notional,
    "equity_bridge": r.equity_bridge, "exposure": ex, "counters": c,
    "postonly": r.postonly_report,  # the PostOnly counters live in postonly_report, not in counters (fixed after the 2026-11-02 runs)
    "breaker_trips": [{"t_of_day_s": t["ts"] - d0, "cause": t["cause"]} for t in br.get("trips", [])],
    "alarms": r.alarms, "alarm_active": r.alarm_active,
    "margin": {k: v for k, v in mg.items() if k != "events"},
    "naive_edge_bps": dist([x.get("naive_edge_bps") for x in sl]), "conditional_edge_bps": dist([x.get("conditional_edge_bps") for x in sl]),
    "legging": {k: v for k, v in r.legging_report.items() if k != "rows"},
    "funding_report": {k: v for k, v in (r.funding_report or {}).items() if k != "settlements"},
    "funding_settlements": (r.funding_report or {}).get("settlements", []),
    "data_quality": getattr(r, "data_quality", None), "book_stats": r.book_stats,
    "release_identity": r.release_identity, "product_by_venue": r.product_by_venue,
    "data_integrity": getattr(r, "data_integrity", None), "queue_parameter_status": getattr(r, "queue_parameter_status", None),
    "strategy_validation_label": r.strategy_validation_label, "switch_table": switch_table(doc),
    "warnings": sorted({str(w.message)[:300] for w in caught}),
    "qv2": {"maker": _json.loads(os.environ.get("QV2_MAKER", "{}")), "cfg": _json.loads(os.environ.get("QV2_CFG", "{}")),
            "markout_summary": r.markout_summary, "markout_by_venue_role": getattr(r, "markout_by_venue_role", None), "maker_observations": r.maker_observations, "data_manifest": r.data_manifest,
            "maker_stats": r.maker_stats, "maker_diagnostics": r.maker_diagnostics,
            **({"label": "REFERENCE_ONLY"} if a.reference else {})},
}
if getattr(r, "core_shadow", None):
    (out / "core_shadow.json").write_text(json.dumps(r.core_shadow, indent=1))
if getattr(_bt.maker, "cross_log", None):
    (out / "cross_log.json").write_text(json.dumps(_bt.maker.cross_log, indent=1, default=str))        # opposite_cross_confirm_ms: one row per armed / confirmed / dropped cross
summary["wall_s"] = time.time() - t0
(out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
if getattr(r, "hedge_capture", None):
    import gzip
    with gzip.open(out / "hedge_capture.json.gz", "wt") as gz:
        json.dump(r.hedge_capture, gz, default=str)
if getattr(r, "execution_report", None):
    (out / "execution_report.json").write_text(json.dumps(r.execution_report, indent=1, default=str))   # post-refactor runs only
if getattr(r, "execution_reconciliation", None):
    (out / "execution_reconciliation.json").write_text(json.dumps(r.execution_reconciliation, indent=1, default=str))   # Phase 0 hardening: independent ledger check
if getattr(r, "maker_observations", None):
    (out / "maker_observations_summary.json").write_text(json.dumps(r.maker_observations, indent=1, default=str))
print(a.profile, tag, a.day, f"wall {summary['wall_s']:.0f}s sig {r.signals_generated} fills {r.orders_filled} cash {r.cash_pnl:.4f} "
      f"eq {r.ending_equity:.4f} bridge_ok {r.equity_bridge.get('ok')} trips {len(summary['breaker_trips'])} alarms {len(r.alarms)}", flush=True)
