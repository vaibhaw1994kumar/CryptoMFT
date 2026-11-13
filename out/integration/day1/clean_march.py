"""Clean v1_9 replay of the local March 2024-03-11 slice (2 hours; Bybit full book, Binance best bid/ask only) with the lifecycle report, the EMS path coverage table and the per-order checks (owner 12 Nov, section J). A run wrapper
over SessionReplay. The regime boundaries are the March training-slice ones (PARTIAL: 2 h of 2024-03-01). usage: clean_march.py OUT_DIR [--src DIR]"""
import sys
import time
from pathlib import Path

argv = sys.argv[1:]
src = "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src"
if "--src" in argv:
    i = argv.index("--src"); src = argv[i + 1]; del argv[i:i + 2]
sys.path.insert(0, src)
from new_backtest.profiles import apply_profile, load_profile
from new_backtest.session_replay import SessionReplay, write_result

SLICES = "/Users/vaibhav/Documents/Newproject/out/queue_v2/slices_mar2024"
BND = "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/regime_boundaries_train_0301_0307.json"
out = Path(argv[0])
profile = load_profile("perp_perp_maker_taker_v1_9")
cfg = apply_profile(profile, dataset="mar2024", data_root=SLICES, pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir=str(out))
cfg.arb.max_position_base = 1.0
t = time.time()
result = SessionReplay(cfg, profile, decompose_markouts=True, lifecycle_report=True, regime_boundaries=BND, profile_safety=True).run()
result["run"] = {"case": "clean_march_0311", "wall_s": round(time.time() - t, 1), "labels": ["EXPLORATORY: mechanics only", "March 2024-03-11, the local 2-hour slice (not the full day)", "no funding file in March", "regime boundaries: March training slice, PARTIAL"]}
write_result(result, out)
print({"signals": len(result["signals"]), "orders": len(result["orders"]), "fills": len(result["fills"]), "wall_s": result["run"]["wall_s"]})
