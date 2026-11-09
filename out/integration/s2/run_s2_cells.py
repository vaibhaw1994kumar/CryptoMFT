"""S2 hedge cap sweep (owner 9 Nov), a run wrapper (no logic): profile perp_perp_maker_taker_v1_6 (v1_5 plus the frozen S1 table, toxic gate as in the profile), the SELECT days 2024-03-08..03-11 (see DAYS),
2-hour slice of each day (00:00-02:00 UTC, the March slices under out/queue_v2/slices_mar2024), through out/queue_v2/qv2_march_run.py from the frozen snapshot out/queue_v2/snap_s2 (fingerprint in its FINGERPRINT.txt).
The cap in the session runtime is `touch_slack`: each hedge attempt is capped at the hedge venue's CURRENT touch moved against us by max(arb.max_walk_bps = 2, execution.hedge.slack_bps_floor) bps. Cells = the floor:
  slack2 (cap 2 bps), slack3, slack5 (TODAY'S cap, the v1_6 profile unchanged), slack8, slack12.
The break-even-plus-buffer cap exists only in the backtester's own loop, not in the session runtime (v1_5 and later), so it is NOT in this sweep.
One change per cell: only slack_bps_floor, set through QV2_HEDGE (recorded in the summary's qv2 block). Every result is EXPLORATORY: 2-hour slices, Singapore latency, size 1, no funding file for March. HOLDOUT is not touched.
usage: run_s2_cells.py [WORKERS=2]   -> out/integration/s2/runs/CELL/base/<profile>/<day>/summary.json   Resumable (a finished summary is skipped)."""
import calendar, json, os, subprocess, sys, time
from pathlib import Path

ROOT = Path("/Users/vaibhav/Documents/Newproject")
X = ROOT / "out" / "queue_v2"
SNAP = X / "snap_s2"
OUT = ROOT / "out" / "integration" / "s2" / "runs"
PROFILE = "perp_perp_maker_taker_v1_6"
DAYS = [f"2024-03-{d:02d}" for d in range(8, 12)]       # SELECT days only: v1_6 pins an adverse table and a hedge-move file fitted on 03-01..03-07, and the loaders refuse a run day that is not strictly after every fit day, so the TRAIN days cannot be run with v1_6 (checked: the runner stops with "training days must be strictly before the run day")
CELLS = {"slack2": 2.0, "slack3": 3.0, "slack5": 5.0, "slack8": 8.0, "slack12": 12.0}
REASON_MAR = ("exploratory: 2-hour slice; Binance March-2024 has bookTicker (L1) only (manifest EMPTY); funding not settled; makers judged against the tape of the venue they rest on")
WORKERS = int(sys.argv[1]) if len(sys.argv) > 1 else 2
jobs = [(d, c) for d in DAYS for c in CELLS]
running = {}


def start(day, cell):
    t1 = calendar.timegm(time.strptime(day, "%Y-%m-%d")) + 7200 + 1.0
    env = dict(os.environ, QV2_SNAP=str(SNAP), QV2_RUNS=str(OUT / cell), QV2_DATA_ROOT=str(X / "slices_mar2024"),
               QV2_MAKER=json.dumps({"unknown_queue_fit": str(X / "unknown_queue_fit_mar2024_NONE.json")}),
               QV2_CFG=json.dumps({"max_event_ts": t1, "data_manifest": str(X / "manifest_mar2024.json")}), QV2_HEDGE=json.dumps({"slack_bps_floor": CELLS[cell]}))
    cmd = ["python3.12", "-I", str(X / "qv2_march_run.py"), PROFILE, day, "--group", "base", "--cell", PROFILE, "--allow-manifest-gap", REASON_MAR, "--fee", "profile", "--binance-fee", "profile"]
    (OUT / "logs").mkdir(parents=True, exist_ok=True)
    print(time.strftime("%T"), "start", cell, day, flush=True)
    return subprocess.Popen(["nice", "-n", "5"] + cmd, env=env, stdout=open(OUT / "logs" / f"{cell}_{day}.log", "w"), stderr=subprocess.STDOUT, cwd=str(SNAP))


failed = []
while jobs or running:
    for key, p in list(running.items()):
        if p.poll() is not None:
            print(time.strftime("%T"), "rc", p.returncode, *key, flush=True)
            if p.returncode:
                failed.append(key)
            del running[key]
    while jobs and len(running) < WORKERS:
        day, cell = jobs.pop(0)
        if (OUT / cell / "base" / PROFILE / day / "summary.json").exists():
            continue
        running[(cell, day)] = start(day, cell)
    time.sleep(5)
print("done; failed:", failed)
