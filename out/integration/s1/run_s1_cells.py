"""S1 replay cells (owner 9 Nov), a run wrapper (no logic): the SELECT day 2024-03-11, 2-hour slice (00:00-02:00 UTC), one cell at a time, through out/queue_v2/qv2_march_run.py from the NEW frozen snapshot out/queue_v2/snap_s1.
Recipe = the `g1s` cell of out/integration/phase_b/run_ledger_cells.py (v1_5: Bybit VIP 5 + Binance VIP 9 from the profile, the 2-hour March slice, manifest gap reason, queue fit NONE, max_event_ts), unchanged.
  v15   perp_perp_maker_taker_v1_5                    the single pinned hedge move (-0.488 bps)
  v16g  perp_perp_maker_taker_v1_6, toxic gate ON     v1_5 + the adverse table (hedge move at 50 ms, repricing horizon 1 s); the gate as in v1_5
  v15m  v1_5 with the hedge move set to the table's TRAIN side means at 50 ms (-0.192 bid, -0.157 ask): a CONTROL that separates the level change from the conditioning
  v16n  perp_perp_maker_taker_v1_6, toxic gate OFF    the same with arb.block_on_toxic false (QV2_ARB), measured alone
  v16l1 / v16l3  perp_perp_maker_taker_v1_6 with arb.maker_max_live_orders set to 1 / 3 explicitly (everything else equal)
Every result is EXPLORATORY, a SELECT-day slice, Singapore latency, size 1, no funding (no March file). HOLDOUT is not touched.
usage: run_s1_cells.py CELL [CELL ...]   -> out/integration/s1/runs/CELL/base/<profile>/2024-03-11/summary.json   Resumable."""
import calendar, json, os, subprocess, sys, time
from pathlib import Path

ROOT = Path("/Users/vaibhav/Documents/Newproject")
X = ROOT / "out" / "queue_v2"
SNAP = Path(os.environ["S1_SNAP"]) if os.environ.get("S1_SNAP") else X / "snap_s1"          # S1_SNAP / S1_OUT: a later snapshot and another output folder (identity re-runs, the maker_max_live_orders cells)
OUT = Path(os.environ["S1_OUT"]) if os.environ.get("S1_OUT") else ROOT / "out" / "integration" / "s1" / "runs"
DAY = os.environ.get("S1_DAY", "2024-03-11")          # S1_DAY: another March slice day; S1_TRACE=1: record the strategy's rejects with their time (session_reject_trace.jsonl, observation only)
REASON_MAR = ("exploratory: 2-hour slice; Binance March-2024 has bookTicker (L1) only (manifest EMPTY); funding not settled; makers judged against the tape of the venue they rest on")
PROFILE = {"v15": "perp_perp_maker_taker_v1_5", "v15m": "perp_perp_maker_taker_v1_5", "v16g": "perp_perp_maker_taker_v1_6", "v16n": "perp_perp_maker_taker_v1_6",
           "v16l1": "perp_perp_maker_taker_v1_6", "v16l3": "perp_perp_maker_taker_v1_6", "v17": "perp_perp_maker_taker_v1_7"}
ARB = {"v16n": {"block_on_toxic": False},
       "v16l1": {"maker_max_live_orders": 1}, "v16l3": {"maker_max_live_orders": 3},     # owner 9 Nov: the controlled rerun, maker_max_live_orders 1 against 3, everything else equal (report only, no profile change)
       "v15m": {"expected_hedge_move_bps_bid": -0.19243779483019816, "expected_hedge_move_bps_ask": -0.15736086120639317}}   # control: v1_5 with the table's TRAIN side means at 50 ms instead of the pinned -0.488 (isolates conditioning from the level)
t1 = calendar.timegm(time.strptime(DAY, "%Y-%m-%d")) + 7200 + 1.0
for cell in sys.argv[1:]:
    prof = PROFILE[cell]
    runs = OUT / cell
    done = runs / "base" / prof / DAY / "summary.json"
    if done.exists():
        print("skip", cell, flush=True)
        continue
    env = dict(os.environ, QV2_SNAP=str(SNAP), QV2_RUNS=str(runs), QV2_DATA_ROOT=str(X / "slices_mar2024"),
               QV2_MAKER=json.dumps({"unknown_queue_fit": str(X / "unknown_queue_fit_mar2024_NONE.json")}),
               QV2_CFG=json.dumps({"max_event_ts": t1, "data_manifest": str(X / "manifest_mar2024.json"), **({"trace_rejects": True} if os.environ.get("S1_TRACE") else {})}), QV2_ARB=json.dumps(ARB.get(cell, {})))
    cmd = ["python3.12", "-I", str(X / "qv2_march_run.py"), prof, DAY, "--group", "base", "--cell", prof, "--allow-manifest-gap", REASON_MAR, "--fee", "profile", "--binance-fee", "profile"]
    (OUT / "logs").mkdir(parents=True, exist_ok=True)
    print(time.strftime("%T"), "start", cell, flush=True)
    rc = subprocess.call(["nice", "-n", "5"] + cmd, env=env, stdout=open(OUT / "logs" / f"{cell}.log", "w"), stderr=subprocess.STDOUT, cwd=str(SNAP))
    print(time.strftime("%T"), "rc", rc, cell, flush=True)
    if rc:
        sys.exit(rc)
