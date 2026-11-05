#!/usr/bin/env python3
"""One perp_perp_maker_taker_v1 run under a fault preset or a conditional scenario (task 7 of the perp lifecycle work).

    python3 scripts/perp_fault_run.py preset realistic 2022-01-13 --seed 3 --size 1 --out-root OUT --data-root DATA
    python3 scripts/perp_fault_run.py scenario outage_binance_30s 2022-01-13 --size 5 --out-root OUT --data-root DATA

Profile as shipped (margin ON, circuit breaker ON, postonly model ON, calibration = config/lifecycle_calibration_perp_bybit.json); the preset only
changes the environment (new_backtest.fault_presets.perp_preset / conditional_scenario). Writes OUT/<kind>/<name>/size<S>/<day>/seed<N>/summary.json.
A summary whose release_identity or sources differ from what is asked for is never reused: the file is skipped only if it already exists."""
import argparse, calendar, dataclasses, json, os, sys, time, warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from new_backtest import fault_presets as FP                       # noqa: E402
from new_backtest.backtester import CrossVenueArbBacktester         # noqa: E402
from new_backtest.profiles import apply_profile, load_profile       # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("kind", choices=("preset", "scenario")); ap.add_argument("name"); ap.add_argument("day")
ap.add_argument("--seed", type=int, default=0); ap.add_argument("--size", type=float, default=1.0)
ap.add_argument("--out-root", required=True); ap.add_argument("--data-root", required=True)
ap.add_argument("--funding-dir", default=None); ap.add_argument("--pair", default="SOLUSDT")
ap.add_argument("--no-breaker", action="store_true", help="control: same run with circuit_breaker=None")
ap.add_argument("--reference", action="store_true", help="REFERENCE_ONLY: required, this script runs the frozen v1 profile (never for decisions)")
a = ap.parse_args()
if not a.reference:
    raise SystemExit("perp_fault_run.py runs perp_perp_maker_taker_v1, a REFERENCE profile, never for decisions: pass --reference")
out = Path(a.out_root) / a.kind / (a.name + ("_nobreaker" if a.no_breaker else "") + (("_" + os.environ["EXEC_TAG"]) if os.environ.get("EXEC_TAG") else "")) / f"size{a.size:g}" / a.day / f"seed{a.seed}"
if (out / "summary.json").exists():
    print("exists, skipped", out); sys.exit(0)
out.mkdir(parents=True, exist_ok=True)

doc = load_profile(ROOT / "profiles" / "perp_perp_maker_taker_v1.yaml")
if os.environ.get("EXEC_MODE"):                   # execution layer: override execution.* switches (JSON), e.g. {"one_hedge_child": "count"} for the legacy control
    doc.setdefault("execution", {}).update(json.loads(os.environ["EXEC_MODE"]))
if os.environ.get("EXEC_HEDGE"):                  # execution layer phase 1: override execution.hedge for this run (JSON)
    doc.setdefault("execution", {}).setdefault("hedge", {}).update(json.loads(os.environ["EXEC_HEDGE"]))
cfg = apply_profile(doc, data_root=a.data_root, pair=a.pair, start_date=a.day, end_date=a.day, funding_rate_dir=a.funding_dir or f"{a.data_root}/../out/funding/{a.pair}",
                    max_trade_log=10 ** 7, equity_bridge="warn", record_exposure=True, seed=a.seed)
cfg.arb.max_position_base = a.size
cfg.arb.fill_headroom_base = doc["arb"]["fill_headroom_base"] * a.size
cfg.arb.block_on_toxic = True
cfg.arb.max_cross_venue_skew_s = 0.2
cfg.arb.fill_gate_horizon_s = 5
mk, hg, fm = ROOT / "config/lifecycle_calibration_perp_bybit.json", ROOT / "config/lifecycle_calibration_perp_binance.json", ROOT / "config/fast_move_thresholds.json"
sources = None
if a.kind == "preset":
    p = FP.perp_preset(a.name, seed=a.seed, maker_calibration=mk, hedge_calibration=hg, fast_move_path=fm, day=a.day)
    cfg.ack_latency = p["ack_latency"]
    cfg.hedge_faults = dataclasses.replace(cfg.hedge_faults, **p["faults"])
    sources = p["sources"]
else:
    cfg.ack_latency = dataclasses.replace(cfg.ack_latency, seed=a.seed + 5)
    cfg.hedge_faults = dataclasses.replace(cfg.hedge_faults, seed=a.seed, label=a.name, **FP.conditional_scenario(a.name, day=a.day, fast_move_path=fm))
if a.no_breaker:
    cfg.circuit_breaker = None

t0 = time.time()
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    bt = CrossVenueArbBacktester(cfg)
    r = bt.run()
hr = r.hedge_report or {}
c, ex, br = hr.get("counters", {}), hr.get("exposure", {}), (hr.get("circuit_breaker") or {})
d0 = calendar.timegm(time.strptime(a.day, "%Y-%m-%d"))
trips = [{"t_of_day_s": t["ts"] - d0, "cause": t["cause"], "net_at_trip": t["net_at_trip"], "pending_hedges": t["pending_hedges"]} for t in br.get("trips", [])]
mg = r.margin_report or {}
summary = {
    "kind": a.kind, "name": a.name, "day": a.day, "seed": a.seed, "size_base": a.size, "breaker": "off" if a.no_breaker else "on", "wall_s": time.time() - t0,
    "profile": "perp_perp_maker_taker_v1", "label": "REFERENCE_ONLY", "release_identity": r.release_identity, "sources": sources, "faults": cfg.hedge_faults.as_dict(),
    "ack_latency": cfg.ack_latency.__dict__ if hasattr(cfg.ack_latency, "__dict__") else str(cfg.ack_latency),
    "signals": r.signals_generated, "fills": r.orders_filled, "orders_sent": r.orders_sent, "signals_unhedged": r.signals_unhedged, "fees_paid": r.fees_paid,
    "cash_pnl": r.cash_pnl, "ending_equity_common_timestamp": r.ending_equity, "attribution_pnl": r.attribution_pnl, "final_inventory": r.final_inventory,
    "net_base_position": r.net_base_position, "traded_notional": r.traded_notional, "equity_bridge_ok": r.equity_bridge.get("ok"),
    "exposure": ex, "counters": c, "breaker_summary": br, "trips": trips, "first_trip_t_of_day_s": trips[0]["t_of_day_s"] if trips else None,
    "exposure_after_first_trip": {k: ex.get(k) for k in ("peak_abs_net_base", "longest_net_nonzero_s", "share_of_time_net_nonzero")},
    "alarms": r.alarms, "alarm_active": r.alarm_active, "postonly": r.postonly_report,
    "order_events_by_source": r.order_event_summary, "maker_lifecycle_counts": dict(bt.maker.lifecycle_counts),
    "margin": {k: v for k, v in mg.items() if k != "events"}, "margin_events": (mg.get("events") or [])[:10],
    "signals_blocked": {k: v for k, v in c.items() if k.startswith("signals_blocked")}, "warnings": sorted({str(w.message)[:300] for w in caught}),
    "execution_report": {k: v for k, v in (r.execution_report or {}).items() if k != "ioc"},
    "execution_reconciliation": {k: v for k, v in (r.execution_reconciliation or {}).items() if k != "per_venue"},
    "ioc": {k: v for k, v in ((r.execution_report or {}).get("ioc") or {}).items() if k != "plans"},
    "unknown_hedges": {k: v for k, v in c.items() if "unknown" in k},
}
(out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
print(a.kind, a.name, a.day, f"seed{a.seed} size{a.size:g}", f"wall {summary['wall_s']:.0f}s sig {r.signals_generated} fills {r.orders_filled} cash {r.cash_pnl:.3f} "
      f"eq {r.ending_equity:.3f} trips {len(trips)} alarms {len(r.alarms)}", flush=True)
