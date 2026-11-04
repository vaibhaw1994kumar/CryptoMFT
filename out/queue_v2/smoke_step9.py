"""Step-9 smoke (owner, 2026-11-04): a 15-minute January slice and a 10-minute March slice, every step-9 switch on, then automated checks on the output.
Not a result: it checks that the switches act and the plumbing is right before the 2- and 7-hour replays."""
import json, os, subprocess, sys
from pathlib import Path
X = Path("/Users/vaibhav/Documents/Newproject/out/queue_v2")
SP = Path("/private/tmp/claude-501/-Users-vaibhav-Documents-Newproject-FAST-MM/05e290d4-c27d-4bcb-abe7-e02ddda9e697/scratchpad")
SNAP = X / "snap_qv2i"
OUT = SP / "smoke_out"
HM = {"label": "stub", "method": "stub for the smoke", "horizon_s": 1.0, "days": ["2022-01-05"], "expected_hedge_move_bps": {"bid": -0.37, "ask": -0.21},
      "stats": {"bid": {"n": 123}, "ask": {"n": 45}}}
(OUT).mkdir(parents=True, exist_ok=True)
(OUT / "hm_jan.json").write_text(json.dumps(HM))
(OUT / "hm_mar.json").write_text(json.dumps({**HM, "days": ["2024-03-07"]}))
REASON = "smoke slice"


def run(name, script, args, env):
    e = dict(os.environ, **env)
    r = subprocess.run(["nice", "-n", "5", "python3.12", str(X / script), *args], env=e, capture_output=True, text=True)
    print(name, "rc", r.returncode); print(r.stdout[-600:]); print(r.stderr[-1500:])
    return r.returncode


SMOKE_TAG = os.environ.get("SMOKE_TAG", "smoke")
extra = {"QV2_MAKER_VENUES": json.dumps(["bybit"])} if os.environ.get("JAN_BYBIT_ONLY") else {}
jan = 0 if os.environ.get("SKIP_JAN") else run("jan", "qv2_run.py", ["perp_perp_maker_taker_v1_1", "2022-01-13", "--allow-rejected-data", REASON],
          {"EXEC_SNAP": str(SNAP), "EXEC_OUT": str(OUT / "jan"), "EXEC_TAG": SMOKE_TAG, **extra, "QV2_DATA_ROOT": str(SP / "mini_jan4"),
           "QV2_MAKER": json.dumps({"unknown_queue_fit": str(X / "unknown_queue_fit_for_2022-01-13.json")}), "QV2_CFG": json.dumps({"data_manifest": str(X / "manifest.json")}),
           "QV2_VENUE_TICKS": json.dumps({"bybit": 0.005, "binance": 0.001}), "QV2_HEDGE_MOVE_FILE": str(OUT / "hm_jan.json")})
mar = 0 if os.environ.get("SKIP_MAR") else run("mar", "qv2_march_run.py", ["perp_perp_maker_taker_v1_1", "2024-03-08", "--group", "smoke", "--cell", "v1_1", "--allow-rejected-data", REASON],
          {"QV2_DATA_ROOT": str(SP / "mini_data"), "QV2_RUNS": str(OUT / "mar"), "QV2_MAKER": json.dumps({"unknown_queue_fit": str(X / "unknown_queue_fit_mar2024_NONE.json")}),
           "QV2_CFG": json.dumps({"data_manifest": str(X / "manifest_mar2024.json")}),
           "QV2_VENUE_TICKS": json.dumps({"bybit": 0.001, "binance": 0.001}), "QV2_MAKER_VENUES": json.dumps(["bybit"]), "QV2_HEDGE_MOVE_FILE": str(OUT / "hm_mar.json")})
print("jan rc", jan, "mar rc", mar)
