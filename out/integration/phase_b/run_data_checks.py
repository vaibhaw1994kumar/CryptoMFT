"""Phase B data checks, run wrapper (no logic): quality_preflight.data_checks_report for one dataset on one venue-day, observation only.
usage: run_data_checks.py DATASET VENUE DAY      DATASET jan2022 (data/, full day) | mar2024 (the 00:00-02:00 UTC slices under out/queue_v2/slices_mar2024) | mar2024full (data/, full day, Bybit only; TRAIN days only, never HOLDOUT)
Writes data_checks/DATASET_VENUE_DAY.json. March Binance is best bid/ask only (no book events): its message-level results say so."""
import json, sys
from pathlib import Path

sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from new_backtest.quality_preflight import data_checks_report

ds, venue, day = sys.argv[1:4]
ROOT = {"jan2022": "/Users/vaibhav/Documents/Newproject/data", "mar2024": "/Users/vaibhav/Documents/Newproject/out/queue_v2/slices_mar2024", "mar2024full": "/Users/vaibhav/Documents/Newproject/data"}[ds]
TICKS = {"jan2022": {"bybit": 0.005, "binance": 0.001}, "mar2024": {"bybit": 0.001, "binance": 0.001}, "mar2024full": {"bybit": 0.001, "binance": 0.001}}[ds]
rep = data_checks_report(ROOT, (venue,), "SOLUSDT", (day,), ticks=TICKS)
rep["dataset"] = ds
if ds == "mar2024" and venue == "binance":
    rep["note"] = "Binance March 2024 is best bid/ask only (bookTicker, one level per snapshot, no book events): message-end crosses, delta sizes and depth checks are not applicable"
Path(__file__).parent.joinpath("data_checks", f"{ds}_{venue}_{day}.json").write_text(json.dumps(rep, indent=1, default=str))
print(ds, venue, day, "done", rep["rows"][0]["added_scan_seconds"], rep["rows"][0]["preflight_scan_seconds"], flush=True)
