"""Day-1 end-to-end replay, step B3 (a run wrapper, no logic): the session runtime on the recorder's capture of SOLUSDT perps, both venues, 2026-10-29 (READ ONLY on data/).
Profile v1_6 or v1_7, pair economics, the statistics and metadata block, the maker lifetime timer, lot rounding and the decomposed markouts on; 1 SOL clip with 5 SOL gross per venue (owner 11 Nov);
margin marks from the recorded derivatives stream when the part has derivatives.jsonl, else the labelled MARK_FROM_MID fallback (a replay of 11 Nov before this change tripped the kill switch from a stale Binance book). EXPLORATORY: recorder quality REJECTED, host clock UNKNOWN, funding not included
(no derivatives files locally), mark = mid, S1 table out-of-period (fitted March 2024), no own orders on the venues so the fills are simulated and show mechanics only.
usage: run_day.py PROFILE OUT_DIR [MINUTES] [--size SOL] [--gross SOL] [--cap] [--src DIR]      (MINUTES limits the run to the first minutes of the capture, for a trial; --size is the clip and net ceiling (default 1), --gross the per-venue
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
size, gross = _opt("--size", 1.0), _opt("--gross", 5.0)
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
cfg.arb.max_venue_imbalance_base = gross        # owner 11 Nov: 5 SOL gross per venue (the net stays at the ceiling above); the core's per-venue risk limit follows it
cfg.maker.participation_cap = cap               # E3 (owner 12 Nov): off unless --cap
replay = SessionReplay(cfg, profile, ticks=dated_ticks("2026-10-29"), recorded=True, decompose_markouts=True, mark_from_mid=True)
events = iter_recorded_events(SESSION)
if minutes is not None:
    first = next(events)
    t0 = first.receive_ts_ns
    events = itertools.chain([first], itertools.takewhile(lambda e: e.receive_ts_ns - t0 <= minutes * 60e9, events))
t = time.time()
result = replay.run_recorded(events)
result["run"] = {"profile": name, "minutes_limit": minutes, "wall_s": round(time.time() - t, 1), "session": SESSION.name,
                 "labels": ["EXPLORATORY", "recorder quality REJECTED; host clock UNKNOWN", "funding not included (no derivatives files locally); mark = mid", "S1 table out-of-period (SOL, fitted March 2024)",
                            "no own orders on the venues: simulated fills show mechanics only", f"size {size} SOL clip, net ceiling {size} SOL + headroom, {gross} SOL gross per venue (owner 11 Nov; sweep settings), participation cap {'ON' if cap else 'off'}", "marks: the recorded derivatives stream when present, else MARK_FROM_MID (replay only)"]}
write_result(result, out)
print(json.dumps({"profile": name, "wall_s": result["run"]["wall_s"], "events": result["events"], "signals": len(result["signals"]), "orders": len(result["orders"]), "fills": len(result["fills"]),
                  "positions": result["positions"], "ledger_events": sum(result["ledger_event_counts"].values())}, indent=1))
