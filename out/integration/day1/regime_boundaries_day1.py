"""Regime boundaries from the 2026-10-29 capture itself (owner 12 Nov: the seven March training days are not on this Mac, "we can use one day 29th october data"). IN-SAMPLE: the day is bucketed by its OWN terciles, so the
regime split of a day-1 replay describes that day's own distribution and carries no out-of-sample claim. Same sampler as regime_boundaries.py (the core's `regime_sample`, one sample per second of the maker venue's view book);
trading is switched off (no signal passes the edge gate), the replay only feeds the books. usage: regime_boundaries_day1.py --src DIR"""
import hashlib
import json
import sys
import time
from pathlib import Path

argv = sys.argv[1:]
src = "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src"
if "--src" in argv:
    i = argv.index("--src"); src = argv[i + 1]; del argv[i:i + 2]
sys.path.insert(0, src)
from crypto_stat_arb.metrics import tercile_cuts
from new_backtest.profiles import apply_profile, load_profile
from new_backtest.session_replay import SessionReplay, dated_ticks, iter_recorded_events

SESSION = Path("/Users/vaibhav/Documents/Newproject/data/live_sessions/solusdt/2026-10-29/SOLUSDT/e9b81728a3054812a48035a1b3e02d42")
profile = dict(load_profile("perp_perp_maker_taker_v1_7"))
for key in ("hedge_move_calibration", "adverse_table", "do_not_quote"):
    profile.pop(key, None)
cfg = apply_profile(profile, dataset="mar2024", data_root="", pair="SOLUSDT", start_date="2026-10-29", end_date="2026-10-29", output_dir="/tmp/regime_boundaries_day1_unused")
cfg.arb.min_net_edge_bps = 1e9
t = time.time()
rep = SessionReplay(cfg, profile, ticks=dated_ticks("2026-10-29"), recorded=True, mark_from_mid=True, lifecycle_report=True)
rep.run_recorded(iter_recorded_events(SESSION))
vals = list(rep._regime_samples.values())
out = {"coverage": "IN-SAMPLE: the 2026-10-29 capture itself (owner decision 12 Nov; the March training days are not on this machine)",
       "label": "regime boundaries of the evaluated day's own terciles: no out-of-sample claim", "boundaries": {"volatility": tercile_cuts([v[0] for v in vals]), "spread": tercile_cuts([v[1] for v in vals]), "liquidity": tercile_cuts([v[2] for v in vals])},
       "definitions": {"volatility": "|mid change since the previous sample| in bps", "spread": "touch spread in bps", "liquidity": "bid size + ask size at the touch (SOL)", "buckets": "low <= cut1 < mid <= cut2 < high"},
       "days": ["2026-10-29"], "n_seconds": len(vals), "wall_s": round(time.time() - t, 1), "script": Path(__file__).name, "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
dest = Path("/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/regime_boundaries_day1_2026-10-29_insample.json")
dest.write_text(json.dumps(out, indent=1))
print(json.dumps(out["boundaries"], indent=1), out["n_seconds"])
