"""v1.1 on the March-2024 days (owner, 2026-11-04). Derived from out/exec_layer_p1lite/p1_run.py (same data, same training rule, same cfg), but: frozen snapshot snap_qv2h,
profile FILE perp_perp_maker_taker_v1_1, env QV2_MAKER / QV2_CFG overrides (data manifest, fit path), qv2 summary extras, outputs under out/queue_v2/runs_mar2024.
Original doc: Phase 1-lite: one replay per invocation (one day, one setting) on the March-2024 SOLUSDT perp files. "15-day study".

usage: p1_run.py PROFILE DAY --group G --cell C [--buffer none|0|1|2|4|8] [--fee mm|vip0] [--cancel-model profile|behind_first|ahead_first]
                 [--scenario clean|hedge_reject_fast_move|outage_binance_2s] [--train D1,D2,...] [--split-file F]
Code: the frozen snapshot out/exec_layer_p1lite/snap (sha256 in snap/SHA256SUMS). Latency preset singapore in the profile and the timing block.
Training (v1): expected hedge move per side = pooled executed hedge move of the legacy_v0 runs (group train_legacy) of the TRAIN days only.
Clean = no injected faults; the profile's response policy (retries, banded unwind, breaker) stays on.
No funding file exists for March 2024: funding is not settled in these runs (stated in every summary).
Output: out/exec_layer_p1lite/runs/<group>/<cell>/<day>/summary.json (+ hedge_attempts.json, legging_rows.json)."""
import argparse, calendar, dataclasses, json, os, sys, time, warnings
from pathlib import Path

E = Path("/Users/vaibhav/Documents/Newproject/out/exec_layer_p1lite")        # split.json, training runs, fast-move file live here (read-only)
Q = Path(__file__).resolve().parent
SNAP = Path(os.environ["QV2_SNAP"]) if os.environ.get("QV2_SNAP") else Q / "snap_qv2i"
sys.path.insert(0, str(SNAP / "src"))
from new_backtest import fault_presets
from new_backtest.backtester import CrossVenueArbBacktester
from new_backtest.profiles import apply_profile, load_profile, switch_table

RUNS = Path(os.environ["QV2_RUNS"]) if os.environ.get("QV2_RUNS") else Q / "runs_mar2024"
TRAIN_RUNS = E / "runs"
DATA = os.environ.get("QV2_DATA_ROOT", "/Users/vaibhav/Documents/Newproject/data")
FEES = {"mm": "bybit_perp_mm", "vip0": "bybit_perp_vip0"}
ap = argparse.ArgumentParser()
ap.add_argument("profile", nargs="?", default="perp_perp_maker_taker_v1_1"); ap.add_argument("day")
ap.add_argument("--reference", action="store_true", help="REFERENCE_ONLY: required to run legacy_v0 or perp_perp_maker_taker_v1 (never for decisions)")
ap.add_argument("--latency-preset", default="singapore", choices=("singapore", "tokyo"), help="singapore is the run default; tokyo only for a labelled latency sensitivity run (no preset is changed)")
ap.add_argument("--hedge-move-sensitivity", default=None, help="a labelled SENSITIVITY run: replace the hedge-move file a pinned profile names with this one (hash-checked, TRAIN days only); the profile file is not edited")
ap.add_argument("--group", required=True); ap.add_argument("--cell", required=True)
ap.add_argument("--buffer", default="none")
ap.add_argument("--fee", default="mm", choices=sorted(FEES))
ap.add_argument("--cancel-model", default="profile", choices=("profile", "behind_first", "ahead_first"))
ap.add_argument("--scenario", default="clean", choices=("clean", "hedge_reject_fast_move", "outage_binance_2s"))
ap.add_argument("--train", default="")
ap.add_argument("--split-file", default=str(E / "split.json"))
ap.add_argument("--allow-rejected-data", default=None)
ap.add_argument("--allow-manifest-gap", default=None, help="written reason for a data-manifest block ONLY; the preflight still blocks (owner 6 Nov: use this for every v1_2 headline run)")
a = ap.parse_args()
if a.profile in ("legacy_v0", "perp_perp_maker_taker_v1") and not a.reference:
    raise SystemExit(f"{a.profile} is a REFERENCE profile, never for decisions: pass --reference to run it (the default profile is perp_perp_maker_taker_v1_1)")
out = RUNS / a.group / a.cell / a.day
out.mkdir(parents=True, exist_ok=True)

doc = load_profile(SNAP / "profiles" / f"{a.profile}.yaml")
if a.hedge_move_sensitivity:
    import hashlib as _hl
    _sf = Path(a.hedge_move_sensitivity); _sf = _sf if _sf.is_absolute() else SNAP / _sf
    if not doc.get("hedge_move_calibration"):
        raise SystemExit("--hedge-move-sensitivity replaces the file a profile PINS; this profile pins none")
    doc["hedge_move_calibration"] = {"file": str(_sf), "sha256": _hl.sha256(_sf.read_bytes()).hexdigest()}
doc["latency_preset"] = a.latency_preset
if isinstance(doc.get("timing"), dict):
    doc["timing"]["from_latency_preset"] = a.latency_preset
doc.setdefault("fee_presets", {})["bybit"] = FEES[a.fee]
if a.buffer != "none":
    doc.setdefault("execution", {}).setdefault("hedge", {}).update({"cap": "breakeven_plus_buffer", "cap_buffer_bps": float(a.buffer)})

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
_pinned = doc.get("hedge_move_calibration")
if _pinned:                          # the profile pins its own hedge-move file (v1_2); the environment must not name another
    if os.environ.get("QV2_HEDGE_MOVE_FILE") or os.environ.get("QV2_MOVES"):
        raise SystemExit("this profile pins hedge_move_calibration; QV2_HEDGE_MOVE_FILE / QV2_MOVES must not be set")
    _pp = Path(_pinned["file"]); _pp = _pp if _pp.is_absolute() else SNAP / _pp
    moves, training = _hm_training(_pp, a.day)
    training["sha256_pinned_by_profile"] = _pinned["sha256"]
elif a.profile != "legacy_v0" and os.environ.get("QV2_HEDGE_MOVE_FILE"):
    moves, training = _hm_training(os.environ["QV2_HEDGE_MOVE_FILE"], a.day)
elif a.profile != "legacy_v0" and os.environ.get("QV2_MOVES"):
    moves = {k: float(v) for k, v in json.loads(os.environ["QV2_MOVES"]).items()}
    training = {"days": [], "status": "OVERRIDE (ladder row)", "source": os.environ.get("QV2_MOVES_SOURCE", "QV2_MOVES")}
elif a.profile != "legacy_v0":
    days = [d for d in a.train.split(",") if d]
    assert days and all(d < a.day for d in days), "v1 needs TRAIN days, all strictly before the run day"
    pooled = {"bid": [0.0, 0], "ask": [0.0, 0]}
    used = []
    for d in days:
        p = TRAIN_RUNS / "train_legacy" / "legacy_v0" / d / "summary.json"
        if not p.exists():
            raise SystemExit(f"missing training run {p}")
        used.append(d)
        leg = json.loads(p.read_text())["legging"]
        for side in pooled:
            ex = leg.get("hedge_exec_move_bps", {}).get(side, {})
            if ex.get("n"):
                pooled[side][0] += ex["n"] * ex["mean"]; pooled[side][1] += ex["n"]
    moves = {s: (v[0] / v[1] if v[1] else 0.0) for s, v in pooled.items()}
    training = {"days": used, "cutoff": max(used), "pooled_n": {s: v[1] for s, v in pooled.items()}, "status": "trained",
                "source": "legacy_v0 replays of the TRAIN days (same code snapshot, singapore, clean, bybit_perp_mm)"}

kw = dict(data_root=DATA, pair="SOLUSDT", start_date=a.day, end_date=a.day, output_dir=str(out), funding_rate_dir=None,
          max_trade_log=10 ** 7, equity_bridge="warn", record_exposure=True, record_hedge_attempts=True,
          )
if a.profile == "legacy_v0":
    kw["hedge_arrival_book"] = "view"        # a non-legacy profile carries its own hedge_arrival_book (v1.1: truth); the runner does not override it
if a.allow_rejected_data:
    if a.profile.endswith("_v1_2"):
        raise SystemExit("v1_2 headline runs do not override the preflight: use --allow-manifest-gap for the manifest and let the preflight block (owner 6 Nov)")
    kw["allow_rejected_data"] = a.allow_rejected_data
if a.allow_manifest_gap:
    kw["allow_manifest_gap"] = a.allow_manifest_gap
cfg = apply_profile(doc, **kw)
if a.cancel_model != "profile":
    cfg.maker = dataclasses.replace(cfg.maker, cancel_model=a.cancel_model)
import json as _json, os
if os.environ.get("QV2_MAKER"):
    cfg.maker = dataclasses.replace(cfg.maker, **_json.loads(os.environ["QV2_MAKER"]))
for _k, _v in _json.loads(os.environ.get("QV2_CFG", "{}")).items():
    setattr(cfg, _k, _v)
if os.environ.get("QV2_VENUE_TICKS") and not _pinned:
    cfg.arb.venue_ticks = {k: float(v) for k, v in json.loads(os.environ["QV2_VENUE_TICKS"]).items()}      # declared per dataset (v1.1 step 9b); the engine checks the first snapshot against it
for _k, _v in json.loads(os.environ.get("QV2_ARB", "{}")).items():
    setattr(cfg.arb, _k, _v)
if os.environ.get("QV2_MAKER_VENUES") and not _pinned:
    cfg.arb.maker_venues = tuple(json.loads(os.environ["QV2_MAKER_VENUES"]))                                # v1.1 step 9d: a data-dependent choice, set by the runner
if not _pinned:
    cfg.arb.expected_hedge_move_bps_bid = moves["bid"]
    cfg.arb.expected_hedge_move_bps_ask = moves["ask"]
if not _pinned:                      # a profile that pins its run-defining settings (v1_2) carries them itself
    cfg.arb.block_on_toxic = True
    cfg.arb.max_cross_venue_skew_s = 0.2
    cfg.arb.fill_gate_horizon_s = 5
scenario_desc = None
if a.scenario != "clean":
    if cfg.hedge_faults is None:
        raise SystemExit("a fault scenario needs the profile's hedge_faults block")
    over = fault_presets.conditional_scenario(a.scenario, day=a.day, fast_move_path=E / "fast_move_thresholds_2024_03.json")
    cfg.hedge_faults = dataclasses.replace(cfg.hedge_faults, **over)
    scenario_desc = over

split = json.loads(Path(a.split_file).read_text()) if Path(a.split_file).exists() else None
t0 = time.time()
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    _bt = CrossVenueArbBacktester(cfg)
    r = _bt.run()
hr = r.hedge_report or {}
br = hr.get("circuit_breaker") or {}
d0 = calendar.timegm(time.strptime(a.day, "%Y-%m-%d"))
mg = r.margin_report or {}
cap = r.hedge_capture or {}
(out / "hedge_attempts.json").write_text(json.dumps(cap.get("attempts", []), default=str))
(out / "legging_rows.json").write_text(json.dumps((r.legging_report or {}).get("rows", []), default=str))
summary = {
    "label": "15-day study (March 2024 SOLUSDT perps); EXPLORATORY", "split": split, "group": a.group, "cell": a.cell, "day": a.day,
    "profile": a.profile, "buffer_bps": a.buffer, "fee": a.fee, "bybit_fee_preset": cfg.bybit_fee_preset, "binance_fee_preset": cfg.binance_fee_preset,
    "cancel_model": cfg.maker.cancel_model, "scenario": a.scenario, "scenario_overrides": scenario_desc, "latency_preset": cfg.latency_preset,
    "execution_hedge": dict(cfg.execution.hedge) if getattr(cfg, "execution", None) is not None else None,
    "funding": "NOT SETTLED: no March-2024 funding file", "hedge_arrival_book": cfg.hedge_arrival_book, "venue_ticks_declared": dict(cfg.arb.venue_ticks), "tick_size_used": dict(_bt.tick_size), "maker_venues": list(cfg.arb.maker_venues), "round_quotes_to_tick": cfg.arb.round_quotes_to_tick, "strategy_rejects": dict(getattr(getattr(_bt, "strategy", None), "rejects", {}) or {}), "training": training, "expected_hedge_move_bps_used": moves,
    "p_fail": cfg.arb.p_fail, "p_fail_status": "PLACEHOLDER_NOT_MEASURED", "size_base": cfg.arb.max_position_base,
    "signals": r.signals_generated, "signals_completed": r.signals_completed, "fills": r.orders_filled, "orders_sent": r.orders_sent,
    "signals_unhedged": r.signals_unhedged, "fees_paid": r.fees_paid, "gross_pnl": r.gross_pnl, "cash_pnl": r.cash_pnl,
    "ending_equity_common_timestamp": r.ending_equity, "last_mark_equity": r.last_mark_equity, "final_inventory": r.final_inventory,
    "traded_notional": r.traded_notional, "equity_bridge": r.equity_bridge, "exposure": hr.get("exposure", {}), "counters": hr.get("counters", {}),
    "hedge_counters": {k: v for k, v in hr.items() if isinstance(v, (int, float))},
    "breaker_trips": [{"t_of_day_s": t["ts"] - d0, "cause": t["cause"]} for t in br.get("trips", [])],
    "alarms": r.alarms, "alarm_active": r.alarm_active, "margin": {k: v for k, v in mg.items() if k != "events"},
    "legging": {k: v for k, v in (r.legging_report or {}).items() if k != "rows"},
    "execution": {k: v for k, v in (r.execution_report or {}).items() if k not in ("plans",)} if getattr(r, "execution_report", None) else None,
    "data_quality": getattr(r, "data_quality", None), "book_stats": r.book_stats, "trades_path": r.trades_path, "fill_log_path": r.fill_log_path,
    "events_processed": r.events_processed, "wall_seconds_engine": r.wall_seconds, "execution_reconciliation": r.execution_reconciliation, "release_identity": r.release_identity,
    "switch_table": switch_table(doc), "maker_config": {k: v for k, v in dataclasses.asdict(cfg.maker).items() if not (k == "depletion_ledger" and not v)},   # a switch that is off is not listed: every earlier result keeps its bytes
    "warnings": sorted({str(w.message)[:300] for w in caught}),
    "data_integrity": getattr(r, "data_integrity", None), "queue_parameter_status": getattr(r, "queue_parameter_status", None),
    "qv2": {"maker": _json.loads(os.environ.get("QV2_MAKER", "{}")), "cfg": _json.loads(os.environ.get("QV2_CFG", "{}")),
            "markout_summary": r.markout_summary, "markout_by_venue_role": getattr(r, "markout_by_venue_role", None), "maker_observations": r.maker_observations,
            "data_manifest": r.data_manifest, "maker_stats": r.maker_stats, "maker_diagnostics": r.maker_diagnostics,
            "queue_updates": dict(getattr(_bt.maker, "queue_updates", {})),
            **({"label": "REFERENCE_ONLY"} if a.reference else {}), **({"latency_label": "LATENCY_SENSITIVITY"} if a.latency_preset != "singapore" else {}),
            **({"hedge_move_label": "SENSITIVITY", "hedge_move_file": a.hedge_move_sensitivity} if a.hedge_move_sensitivity else {})},
}
if getattr(r, "core_shadow", None):
    (out / "core_shadow.json").write_text(json.dumps(r.core_shadow, indent=1))
if getattr(_bt.maker, "ledger_stats", None):
    (out / "ledger_stats.json").write_text(json.dumps({"stats": _bt.maker.ledger_stats, "diag": {k: v for k, v in _bt.maker.diag.items() if k.startswith(("ledger_", "trade_volume_credited", "delta_shrink_explained", "evidence_"))}}, indent=1))   # depletion ledger: how much matched volume came book-first vs trade-first
if getattr(_bt.maker, "cross_log", None):
    (out / "cross_log.json").write_text(json.dumps(_bt.maker.cross_log, indent=1, default=str))
summary["wall_s"] = time.time() - t0
(out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
print(a.group, a.cell, a.day, f"wall {summary['wall_s']:.0f}s sig {r.signals_generated} fills {r.orders_filled} cash {r.cash_pnl:.4f} "
      f"eq {r.ending_equity:.4f} bridge_ok {r.equity_bridge.get('ok')} trips {len(summary['breaker_trips'])} alarms {len(r.alarms)}", flush=True)
