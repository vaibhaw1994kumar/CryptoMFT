"""Phase D, D7d (a run wrapper, no logic): the core config file the live-paper command reads, built from the working profile (v1_5) exactly as the D7c harness and the session replay build it.
Usage: make_config.py OUT_JSON"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from new_backtest.profiles import apply_profile, load_profile
from new_backtest.session_replay import SessionReplay

profile = load_profile("perp_perp_maker_taker_v1_5")
bt_cfg = apply_profile(profile, dataset="mar2024", data_root="/Users/vaibhav/Documents/Newproject/out/queue_v2/slices_mar2024", pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_d7d")
bt_cfg.arb.max_position_base = 1.0
rep = SessionReplay(bt_cfg, profile)
Path(sys.argv[1]).write_text(json.dumps(rep.raw_config, indent=1))
print("written", sys.argv[1])
