"""Phase 1-lite: one replay per invocation (one day, one setting) on the March-2024 SOLUSDT perp files. "15-day study".

usage: p1_run.py PROFILE DAY --group G --cell C [--buffer none|0|1|2|4|8] [--fee mm|vip0] [--cancel-model profile|behind_first|ahead_first]
                 [--scenario clean|hedge_reject_fast_move|outage_binance_2s] [--train D1,D2,...] [--split-file F]
Code: the frozen snapshot out/exec_layer_p1lite/snap (sha256 in snap/SHA256SUMS). Latency preset singapore in the profile and the timing block.
Training (v1): expected hedge move per side = pooled executed hedge move of the legacy_v0 runs (group train_legacy) of the TRAIN days only.
Clean = no injected faults; the profile's response policy (retries, banded unwind, breaker) stays on.
No funding file exists for March 2024: funding is not settled in these runs (stated in every summary).
Output: out/exec_layer_p1lite/runs/<group>/<cell>/<day>/summary.json (+ hedge_attempts.json, legging_rows.json)."""
import argparse, calendar, dataclasses, json, sys, time, warnings
from pathlib import Path

E = Path(__file__).resolve().parent
SNAP = E / "snap"
sys.path.insert(0, str(SNAP / "src"))
from new_backtest import fault_presets
from new_backtest.backtester import CrossVenueArbBacktester
from new_backtest.profiles import apply_profile, load_profile, switch_table

RUNS = E / "runs"
DATA = "/Users/vaibhav/Documents/Newproject/data"
FEES = {"mm": "bybit_perp_mm", "vip0": "bybit_perp_vip0"}
ap = argparse.ArgumentParser()
ap.add_argument("profile"); ap.add_argument("day")
ap.add_argument("--group", required=True); ap.add_argument("--cell", required=True)
ap.add_argument("--buffer", default="none")
ap.add_argument("--fee", default="mm", choices=sorted(FEES))
ap.add_argument("--cancel-model", default="profile", choices=("profile", "behind_first", "ahead_first"))
ap.add_argument("--scenario", default="clean", choices=("clean", "hedge_reject_fast_move", "outage_binance_2s"))
ap.add_argument("--train", default="")
ap.add_argument("--split-file", default=str(E / "split.json"))
ap.add_argument("--allow-rejected-data", default=None)
a = ap.parse_args()
out = RUNS / a.group / a.cell / a.day
out.mkdir(parents=True, exist_ok=True)

doc = load_profile(SNAP / "profiles" / f"{a.profile}.yaml")
from new_backtest import profiles as _pf
print("profile:", getattr(_pf, "profile_label", lambda n: n)(a.profile), flush=True)          # B2 step 8: REFERENCE_ONLY / CONTROL labels (an older snapshot without the registry prints the bare name)
doc["latency_preset"] = "singapore"
if isinstance(doc.get("timing"), dict):
    doc["timing"]["from_latency_preset"] = "singapore"
doc.setdefault("fee_presets", {})["bybit"] = FEES[a.fee]
if a.buffer != "none":
    doc.setdefault("execution", {}).setdefault("hedge", {}).update({"cap": "breakeven_plus_buffer", "cap_buffer_bps": float(a.buffer)})

training = {"days": [], "status": "none"}
moves = {"bid": 0.0, "ask": 0.0}
if a.profile != "legacy_v0":
    days = [d for d in a.train.split(",") if d]
    assert days and all(d < a.day for d in days), "v1 needs TRAIN days, all strictly before the run day"
    pooled = {"bid": [0.0, 0], "ask": [0.0, 0]}
    used = []
    for d in days:
        p = RUNS / "train_legacy" / "legacy_v0" / d / "summary.json"
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
          hedge_arrival_book=("view" if a.profile == "legacy_v0" else "truth"))
if a.allow_rejected_data:
    kw["allow_rejected_data"] = a.allow_rejected_data
cfg = apply_profile(doc, **kw)
if a.cancel_model != "profile":
    cfg.maker = dataclasses.replace(cfg.maker, cancel_model=a.cancel_model)
cfg.arb.expected_hedge_move_bps_bid = moves["bid"]
cfg.arb.expected_hedge_move_bps_ask = moves["ask"]
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
    r = CrossVenueArbBacktester(cfg).run()
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
    "funding": "NOT SETTLED: no March-2024 funding file", "hedge_arrival_book": cfg.hedge_arrival_book, "training": training, "expected_hedge_move_bps_used": moves,
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
    "switch_table": switch_table(doc), "maker_config": dataclasses.asdict(cfg.maker),
    "warnings": sorted({str(w.message)[:300] for w in caught}),
}
summary["wall_s"] = time.time() - t0
(out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
print(a.group, a.cell, a.day, f"wall {summary['wall_s']:.0f}s sig {r.signals_generated} fills {r.orders_filled} cash {r.cash_pnl:.4f} "
      f"eq {r.ending_equity:.4f} bridge_ok {r.equity_bridge.get('ok')} trips {len(summary['breaker_trips'])} alarms {len(r.alarms)}", flush=True)
