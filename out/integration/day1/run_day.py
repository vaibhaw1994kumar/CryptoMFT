"""Day-1 end-to-end replay, step B3 (a run wrapper, no logic): the session runtime on the recorder's capture of SOLUSDT perps, both venues, 2026-10-29 (READ ONLY on data/).
Profile v1_6 or v1_7, pair economics, the statistics and metadata block, the maker lifetime timer, lot rounding and the decomposed markouts on; 1 SOL max position. EXPLORATORY: recorder quality REJECTED, host clock UNKNOWN, funding not included
(no derivatives files locally), mark = mid, S1 table out-of-period (fitted March 2024), no own orders on the venues so the fills are simulated and show mechanics only.
usage: run_day.py PROFILE OUT_DIR [MINUTES]      (MINUTES limits the run to the first minutes of the capture, for a trial)"""
import itertools
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from new_backtest.profiles import apply_profile, load_profile
from new_backtest.session_replay import SessionReplay, dated_ticks, iter_recorded_events, write_result

SESSION = Path("/Users/vaibhav/Documents/Newproject/data/live_sessions/solusdt/2026-10-29/SOLUSDT/e9b81728a3054812a48035a1b3e02d42")
name, out = sys.argv[1], Path(sys.argv[2])
minutes = float(sys.argv[3]) if len(sys.argv) > 3 else None
profile = load_profile(name)
cfg = apply_profile(profile, dataset="mar2024", data_root="", pair="SOLUSDT", start_date="2026-10-29", end_date="2026-10-29", output_dir=str(out))
cfg.arb.max_position_base = 1.0
replay = SessionReplay(cfg, profile, ticks=dated_ticks("2026-10-29"), recorded=True, decompose_markouts=True)
events = iter_recorded_events(SESSION)
if minutes is not None:
    first = next(events)
    t0 = first.receive_ts_ns
    events = itertools.chain([first], itertools.takewhile(lambda e: e.receive_ts_ns - t0 <= minutes * 60e9, events))
t = time.time()
result = replay.run_recorded(events)
result["run"] = {"profile": name, "minutes_limit": minutes, "wall_s": round(time.time() - t, 1), "session": SESSION.name,
                 "labels": ["EXPLORATORY", "recorder quality REJECTED; host clock UNKNOWN", "funding not included (no derivatives files locally); mark = mid", "S1 table out-of-period (SOL, fitted March 2024)",
                            "no own orders on the venues: simulated fills show mechanics only", "size 1 SOL max position"]}
write_result(result, out)
print(json.dumps({"profile": name, "wall_s": result["run"]["wall_s"], "events": result["events"], "signals": len(result["signals"]), "orders": len(result["orders"]), "fills": len(result["fills"]),
                  "positions": result["positions"], "ledger_events": sum(result["ledger_event_counts"].values())}, indent=1))
