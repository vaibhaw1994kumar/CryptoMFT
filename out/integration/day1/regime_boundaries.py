"""Regime boundaries from the TRAINING days only (March 2024-03-01..07), owner 12 Nov (v1_9_TOLERANCES.md section D). A run wrapper: it replays each training day with trading switched off (no signal can pass the edge
gate) only to sample the maker venue's (Bybit) view book once a second through the core's own `regime_sample`, then takes the tercile cut points of volatility, spread and touch liquidity over all training seconds
(`tercile_cuts`). Writes config/regime_boundaries_train_0301_0307.json. READ ONLY on the slices. usage: regime_boundaries.py [--src DIR] [DAYS...]"""
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
from new_backtest.session_replay import SessionReplay

SLICES = "/Users/vaibhav/Documents/Newproject/out/queue_v2/slices_mar2024"
DAYS = argv or [f"2024-03-0{d}" for d in range(1, 8)]
profile = dict(load_profile("perp_perp_maker_taker_v1_7"))
for key in ("hedge_move_calibration", "adverse_table", "do_not_quote"):      # their guard (calibration days strictly before the run start) cannot hold for the training days themselves; nothing is traded here, so no calibration is used
    profile.pop(key, None)
vol, spread, depth, per_day = [], [], [], {}
for day in DAYS:
    t = time.time()
    cfg = apply_profile(profile, dataset="mar2024", data_root=SLICES, pair="SOLUSDT", start_date=day, end_date=day, output_dir="/tmp/regime_boundaries_unused")
    cfg.arb.min_net_edge_bps = 1e9                 # no signal passes: nothing is traded, the replay only feeds the books
    rep = SessionReplay(cfg, profile, lifecycle_report=True)
    rep.run()
    vals = list(rep._regime_samples.values())
    vol += [v[0] for v in vals]; spread += [v[1] for v in vals]; depth += [v[2] for v in vals]
    per_day[day] = {"seconds": len(vals), "wall_s": round(time.time() - t, 1)}
    print(day, per_day[day], flush=True)
out = {"label": "TRAINING-DAY regime boundaries (March 2024-03-01..07, Bybit view book, one sample per second); a day is bucketed by these, never by its own distribution",
       "boundaries": {"volatility": tercile_cuts(vol), "spread": tercile_cuts(spread), "liquidity": tercile_cuts(depth)},
       "definitions": {"volatility": "|mid change since the previous sample| in bps", "spread": "touch spread in bps", "liquidity": "bid size + ask size at the touch (SOL)",
                       "buckets": "low <= cut1 < mid <= cut2 < high"},
       "days": DAYS, "seconds_per_day": per_day, "n_seconds": len(vol), "script": Path(__file__).name, "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
dest = Path("/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/regime_boundaries_train_0301_0307.json")
dest.write_text(json.dumps(out, indent=1))
print(json.dumps(out["boundaries"], indent=1))
