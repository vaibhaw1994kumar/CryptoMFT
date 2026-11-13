"""Day-1 end-to-end replay, step B3 (a run wrapper, no logic): the session runtime on the recorder's capture of SOLUSDT perps, both venues, 2026-10-29 (READ ONLY on data/).
Profile v1_6 or v1_7, pair economics, the statistics and metadata block, the maker lifetime timer, lot rounding and the decomposed markouts on; 1 SOL clip with 5 SOL gross per venue (owner 11 Nov);
margin marks from the recorded derivatives stream when the part has derivatives.jsonl, else the labelled MARK_FROM_MID fallback (a replay of 11 Nov before this change tripped the kill switch from a stale Binance book). EXPLORATORY: recorder quality REJECTED, host clock UNKNOWN, funding not included
(no derivatives files locally), mark = mid, S1 table out-of-period (fitted March 2024), no own orders on the venues so the fills are simulated and show mechanics only.
usage: run_day.py PROFILE OUT_DIR [MINUTES] [--size SOL] [--gross SOL] [--cap] [--src DIR] [--processing-strategy-ms X] [--processing-risk-ms Y] [--regime-boundaries FILE]      (MINUTES limits the run to the first minutes of the capture, for a trial; --size is the clip and net ceiling (default 1), --gross the per-venue
limit (default 5 SOL; for a size sweep use at least 5 x size); --cap switches the E3 participation cap on (maker.participation_cap, default off). Owner 12 Nov: the sweep sizes are run settings, not a copy of this file)"""
import itertools
import json
import sys
import time
from pathlib import Path


SESSION = Path("/Users/vaibhav/Documents/Newproject/data/live_sessions/solusdt/2026-10-29/SOLUSDT/e9b81728a3054812a48035a1b3e02d42")
argv = [a for a in sys.argv[1:]]
src = "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src"      # the working tree, unless --src names a frozen snapshot's src (out/queue_v2/snap_*/src)
if "--src" in argv:
    i = argv.index("--src"); src = argv[i + 1]; del argv[i:i + 2]
sys.path.insert(0, src)
def _opt(flag, default, cast=float):
    if flag in argv:
        i = argv.index(flag); v = cast(argv[i + 1]); del argv[i:i + 2]; return v
    return default
size, gross_cli = _opt("--size", 1.0), _opt("--gross", None)       # owner 13 Nov: without --gross the profile's own gross per venue is used (v1_9: 2.0, approved production limit), else 5.0
proc_strategy, proc_risk = _opt("--processing-strategy-ms", 0.0), _opt("--processing-risk-ms", 0.0)      # owner 12 Nov: the decision and risk processing time (default 0: nothing changes)
boundaries = _opt("--regime-boundaries", None, str)                                                      # the training-day boundaries file: with it the lifecycle report is written (session_lifecycle_report.json)
cap = "--cap" in argv
if cap:
    argv.remove("--cap")
from new_backtest.profiles import apply_profile, load_profile           # after --src is on the path
from new_backtest.session_replay import SessionReplay, dated_ticks, iter_recorded_events, write_result
name, out = argv[0], Path(argv[1])
minutes = float(argv[2]) if len(argv) > 2 else None
profile = load_profile(name)
cfg = apply_profile(profile, dataset="mar2024", data_root="", pair="SOLUSDT", start_date="2026-10-29", end_date="2026-10-29", output_dir=str(out))
cfg.arb.max_position_base = size                # the clip and the net ceiling (1 SOL by default, + the profile's fill headroom)
gross = gross_cli if gross_cli is not None else (cfg.arb.max_venue_imbalance_base or 5.0)
cfg.arb.max_venue_imbalance_base = gross        # owner 11 Nov: 5 SOL gross per venue (the net stays at the ceiling above); the core's per-venue risk limit follows it
cfg.maker.participation_cap = cap               # E3 (owner 12 Nov): off unless --cap
if proc_strategy or proc_risk:
    import dataclasses
    cfg.timing = dataclasses.replace(cfg.timing, processing_ms={k: v for k, v in (("strategy", proc_strategy), ("risk", proc_risk)) if v})
replay = SessionReplay(cfg, profile, ticks=dated_ticks("2026-10-29"), recorded=True, decompose_markouts=True, mark_from_mid=True, lifecycle_report=boundaries is not None, regime_boundaries=boundaries, profile_safety=(name == "perp_perp_maker_taker_v1_9"))      # owner 13 Nov: v1_9 runs with the profile's own safety values (layer gate enforce, the breaker block)
events = iter_recorded_events(SESSION)
if minutes is not None:
    first = next(events)
    t0 = first.receive_ts_ns
    events = itertools.chain([first], itertools.takewhile(lambda e: e.receive_ts_ns - t0 <= minutes * 60e9, events))
t = time.time()
result = replay.run_recorded(events)
result["run"] = {"profile": name, "minutes_limit": minutes, "wall_s": round(time.time() - t, 1), "session": SESSION.name,
                 "labels": ["EXPLORATORY", "recorder quality REJECTED; host clock UNKNOWN", "funding not included (no derivatives files locally); mark = mid", "S1 table out-of-period (SOL, fitted March 2024)",
                            "no own orders on the venues: simulated fills show mechanics only", f"size {size} SOL clip, net ceiling {size} SOL + headroom, {gross} SOL gross per venue (owner 11 Nov; sweep settings), participation cap {'ON' if cap else 'off'}", f"processing time strategy {proc_strategy} ms + risk {proc_risk} ms" + (" (ASSUMED sensitivity)" if proc_strategy or proc_risk else " (ASSUMED_ZERO)"), "marks: the recorded derivatives stream when present, else MARK_FROM_MID (replay only)"]}
write_result(result, out)
# Owner 13 Nov (the end-to-end rehearsal): the files the report needs that write_result does not make. The safety events in full (payloads included), and one file with every stop-quoting condition and every limit counter of the run,
# read from the session (counts are complete, never a truncated list).
import collections
sess = replay.session
led = sess.ledger.events
safety = [e for e in led if e["event_type"].startswith(("BREAKER_", "KILL_SWITCH", "UNWIND_", "HEDGE_ALARM", "HEDGE_CHAIN", "HEDGE_STATE", "MARGIN_", "NEW_RISK_HELD", "RECONCILIATION", "QUOTE_SKIPPED", "QUOTE_WITHDRAWN", "RESIDUAL_", "LAYER_RISK", "RISK_FLATTEN", "ORDER_LIFETIME", "TIME_INTEGRITY", "ORDER_TIMED_OUT", "ORDER_REJECT_CLASSIFIED"))]
(out / "session_safety_events.jsonl").write_text("".join(json.dumps({k: (str(v) if not isinstance(v, (int, float, bool, type(None), list, dict)) else v) for k, v in e.items()}, sort_keys=True, default=str) + "\n" for e in safety))
lim = getattr(sess.router, "scoped_limits", None)
rejects = getattr(replay.strategy, "rejects", None)
doc = {"label": "EXPLORATORY: every counter of the run; counts are complete (nothing truncated)",
       "ledger_event_type_counts": dict(sorted(collections.Counter(e["event_type"] for e in led).items())),
       "stop_quoting": {"signals_blocked_by_breaker": sess.signals_blocked_by_breaker, "breaker": sess.breaker_report().get("breaker"), "breaker_alarms": list(sess.breaker_alarms),
                        "margin_price_paused_at_end": sess.margin_price_paused, "signals_blocked_by_margin_price": sess.signals_blocked_by_margin_price,
                        "quotes_skipped_rate_limit": sess.quotes_skipped_rate_limit, "quotes_withdrawn": sess.quotes_withdrawn, "risk_gate_refusals_by_reason": dict(sess.gate_counts),
                        "kill_switch": bool(sess.oms.risk.kill_switch), "kill_switch_reason": getattr(sess.oms.risk, "kill_switch_reason", ""),
                        "strategy_reject_counts": (dict(rejects.counts) if hasattr(rejects, "counts") else (dict(rejects) if isinstance(rejects, dict) else None)),
                        "oms_new_risk_held": {k: v for k, v in sess.oms.realism_counters.items() if k.startswith("new_risk_held")}},
       "limits_that_bound": {"rate_limiter": None if lim is None else lim.snapshot(getattr(replay, "last_ns", None)), "risk_engine_refusals": dict(getattr(sess.oms.risk, "refusals", {}) or {}),
                             "layer_gate": {k: v for k, v in (result.get("execution_layer", {}).get("layer", {}).get("counts", {}) or {}).items() if "risk" in k or "refus" in k},
                             "residual_breaches": result["manifest"].get("residual_risk") if isinstance(result.get("manifest"), dict) else None,
                             "oms_realism_counters": dict(sess.oms.realism_counters)},
       "safety_from_profile": getattr(replay, "safety_from_profile", None)}
(out / "session_limits_and_stops.json").write_text(json.dumps(doc, indent=1, default=str))
print(json.dumps({"profile": name, "wall_s": result["run"]["wall_s"], "events": result["events"], "signals": len(result["signals"]), "orders": len(result["orders"]), "fills": len(result["fills"]),
                  "positions": result["positions"], "ledger_events": sum(result["ledger_event_counts"].values())}, indent=1))
