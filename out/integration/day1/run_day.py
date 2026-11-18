"""Day-1 end-to-end replay, step B3 (a run wrapper, no logic): the session runtime on the recorder's capture of SOLUSDT perps, both venues, 2026-10-29 (READ ONLY on data/).
Profile v1_6 or v1_7, pair economics, the statistics and metadata block, the maker lifetime timer, lot rounding and the decomposed markouts on; 1 SOL clip with 5 SOL gross per venue (owner 11 Nov);
margin marks from the recorded derivatives stream when the part has derivatives.jsonl, else the labelled MARK_FROM_MID fallback (a replay of 11 Nov before this change tripped the kill switch from a stale Binance book). EXPLORATORY: recorder quality REJECTED, host clock UNKNOWN, funding not included
(no derivatives files locally), mark = mid, S1 table out-of-period (fitted March 2024), no own orders on the venues so the fills are simulated and show mechanics only.
usage: run_day.py PROFILE OUT_DIR [MINUTES] [--size SOL] [--gross SOL] [--cap] [--src DIR] [--processing-strategy-ms X] [--processing-risk-ms Y] [--regime-boundaries FILE]      (MINUTES limits the run to the first minutes of the capture, for a trial; --size is the clip and net ceiling (default 1), --gross the per-venue
limit (default 5 SOL; for a size sweep use at least 5 x size); --cap switches the E3 participation cap on (maker.participation_cap, default off). Owner 12 Nov: the sweep sizes are run settings, not a copy of this file)"""
import dataclasses
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
maker_venue = _opt("--maker-venue", None, str)                   # owner 14 Nov (step G T2): the venue that rests the quote (the other hedges); default = the profile's own (Bybit)
split_file = _opt("--split", None, str)                          # owner 14 Nov (step F): a JSON file of SplitConfig settings: the order-split evidence journaled with every decision (needs --depth-quality); observation only
impact_file = _opt("--impact", None, str)                        # owner 14 Nov (step C): a JSON file of ImpactConfig settings: the level-3 overlay on; without it nothing changes
part_max = _opt("--participation-max", None)                  # owner 14 Nov (E3 follow-up): override arb.participation_max for an observation run (1.0 = the limit almost never binds)
depth_quality = "--depth-quality" in argv                       # owner 14 Nov: VenueStateConfig.depth_quality on (gap depth and flicker journaled with every intent and child); observation only, off by default
if depth_quality:
    argv.remove("--depth-quality")
samples = "--samples" in argv                                  # record one row per (price, second) of every candidate that reaches the participation check: session_participation_samples.jsonl
if samples:
    argv.remove("--samples")
hedge_min_notional = _opt("--hedge-min-notional", None)          # owner 16 Nov: the hedge venue's minimum notional for THIS run (the replay's default is 1 USDT; Binance SOLUSDT's real minimum is 5, read from the live instrument rules); needed by the dust cases
overrides = []                                                    # owner 16 Nov: --set a.b=value (repeatable): a profile override for this run, labelled in the result (e.g. hedge_faults.dust_carry=next_hedge on v1_9); the profile file is never edited
while "--set" in argv:
    i = argv.index("--set"); overrides.append(argv[i + 1]); del argv[i:i + 2]
cap = "--cap" in argv
s0 = "--s0-audit" in argv                                       # owner 18 Nov: also write session_s0_audit.json (the case-2i amend counts from the run's ledger); off by default, so the identity file set is unchanged
if s0:
    argv.remove("--s0-audit")
if cap:
    argv.remove("--cap")
from new_backtest.profiles import apply_profile, load_profile           # after --src is on the path
from new_backtest.session_replay import SessionReplay, dated_ticks, iter_recorded_events, write_evidence, write_result, write_run_record
name, out = argv[0], Path(argv[1])
minutes = float(argv[2]) if len(argv) > 2 else None
profile = load_profile(name)
for item in overrides:
    path, _, raw_value = item.partition("=")
    node = profile
    for part in path.split(".")[:-1]:
        node = node.setdefault(part, {})
    node[path.split(".")[-1]] = json.loads(raw_value) if raw_value[:1] in "0123456789-[{" or raw_value in ("true", "false", "null") else raw_value
cfg = apply_profile(profile, dataset="mar2024", data_root="", pair="SOLUSDT", start_date="2026-10-29", end_date="2026-10-29", output_dir=str(out))
if part_max is not None:
    cfg.arb.participation_max = part_max
if maker_venue:
    cfg.arb.maker_venues = (maker_venue,)
if split_file:
    from crypto_stat_arb.ems.venue_selector import SplitConfig
    cfg.split = SplitConfig(**json.loads(Path(split_file).read_text()))
if impact_file:
    from crypto_stat_arb.simulation.impact import ImpactConfig
    cfg.impact = ImpactConfig(**json.loads(Path(impact_file).read_text()))
if depth_quality:
    cfg.venue_state.depth_quality = True
cfg.arb.max_position_base = size                # the clip and the net ceiling (1 SOL by default, + the profile's fill headroom)
gross = gross_cli if gross_cli is not None else (cfg.arb.max_venue_imbalance_base or 5.0)
cfg.arb.max_venue_imbalance_base = gross        # owner 11 Nov: 5 SOL gross per venue (the net stays at the ceiling above); the core's per-venue risk limit follows it
cfg.maker.participation_cap = cap or bool(cfg.maker.participation_cap)               # E3 (owner 12 Nov): the profile's value (v1_9: on, owner 13 Nov), or --cap
if proc_strategy or proc_risk:
    import dataclasses
    cfg.timing = dataclasses.replace(cfg.timing, processing_ms={k: v for k, v in (("strategy", proc_strategy), ("risk", proc_risk)) if v})
replay = SessionReplay(cfg, profile, ticks=dated_ticks("2026-10-29"), recorded=True, decompose_markouts=True, mark_from_mid=True, lifecycle_report=boundaries is not None, regime_boundaries=boundaries, profile_safety=(name in ("perp_perp_maker_taker_v1_9", "perp_perp_maker_taker_v1_10", "perp_perp_maker_taker_v1_11", "perp_perp_maker_taker_v1_12", "perp_perp_maker_taker_v1_13", "perp_perp_maker_taker_v1_14", "perp_perp_maker_taker_v1_15")), hedge_min_notional=hedge_min_notional)      # owner 13 Nov: v1_9 runs with the profile's own safety values (layer gate enforce, the breaker block)
if samples:
    replay.strategy.participation_sample_cap = 400_000
events = iter_recorded_events(SESSION)
if minutes is not None:
    first = next(events)
    t0 = first.receive_ts_ns
    events = itertools.chain([first], itertools.takewhile(lambda e: e.receive_ts_ns - t0 <= minutes * 60e9, events))
t = time.time()
import collections as _c
traded = _c.defaultdict(float)                       # E3: the public volume printed at each (venue, price), summed from the capture itself (read only)
def _tally(it):
    for e in it:
        if hasattr(e, "trade_id") and hasattr(e, "aggressor_side"):
            traded[(e.venue, str(e.price))] += float(e.quantity)
        yield e
result = replay.run_recorded(_tally(events))
result["run"] = {"profile_overrides": overrides, "hedge_min_notional": hedge_min_notional, "profile": name, "minutes_limit": minutes, "wall_s": round(time.time() - t, 1), "session": SESSION.name,
                 "labels": ["EXPLORATORY", "recorder quality REJECTED; host clock UNKNOWN", "funding not included (no derivatives files locally); mark = mid", "S1 table out-of-period (SOL, fitted March 2024)",
                            "no own orders on the venues: simulated fills show mechanics only", f"size {size} SOL clip, net ceiling {size} SOL + headroom, {gross} SOL gross per venue (owner 11 Nov; sweep settings), participation cap {'ON' if cap else 'off'}", f"processing time strategy {proc_strategy} ms + risk {proc_risk} ms" + (" (ASSUMED sensitivity)" if proc_strategy or proc_risk else " (ASSUMED_ZERO)"), "marks: the recorded derivatives stream when present, else MARK_FROM_MID (replay only)"]}
write_result(result, out)
# W2a hygiene (owner 17 Nov): the evidence files (safety events, limits and stops, participation, impact) are written by the replay module, not here; the run record (input and output hashes, data span,
# speed) is written last.
write_evidence(replay, result, out, traded)
if s0:
    from new_backtest.session_replay import s0_audit
    (out / "session_s0_audit.json").write_text(json.dumps(s0_audit(replay.session.ledger.events, replay.session.oms.plans, replay.session.reprice_counters), indent=1, default=str))
write_run_record(out, replay)
print(json.dumps({"profile": name, "wall_s": result["run"]["wall_s"], "events": result["events"], "signals": len(result["signals"]), "orders": len(result["orders"]), "fills": len(result["fills"]),
                  "positions": result["positions"], "ledger_events": sum(result["ledger_event_counts"].values())}, indent=1))
