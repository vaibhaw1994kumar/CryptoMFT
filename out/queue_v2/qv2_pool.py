"""Queue model v2 replay pool: resumable, N parallel, one job = one v1 replay (singapore, bybit_perp_mm, clean, seed 1, size 1) with maker / config overrides.
usage: qv2_pool.py JOBSET [N]    (JOBSET: A | B)"""
import json, os, subprocess, sys, time
from pathlib import Path
X = Path("/Users/vaibhav/Documents/Newproject/out/queue_v2")
SNAP = X / os.environ.get("QV2_SNAP", "snap_qv2")
RUNS = X / os.environ.get("QV2_RUNS", "runs")
MAN = str(X / "manifest.json")
REASON = "exploratory: Binance trade tape truncated at ~17 h on 2022-01-05 and 2022-01-13 (queue_v2 manifest); Bybit maker fills use the Bybit tape"
REASON_OCT = "exploratory: Oct-2024 Binance book_snapshots are placeholders (every level 50.0): ignored, Binance book from deltas only; no funding file for this month"
T13 = "2022-01-05,2022-01-06,2022-01-07,2022-01-08,2022-01-09,2022-01-10"
DAYS = ["2022-01-05", "2022-01-13"]
FIT = {d: str(X / f"unknown_queue_fit_for_{d}.json") for d in DAYS}


def fitted(d): return {"unknown_queue_dist": "fitted", "unknown_queue_fit": FIT[d]}


def jobs_A():
    out = []
    for d in DAYS:
        S = [("base", {}, {}), ("behind_first", {"cancel_model": "behind_first"}, {}), ("ahead_first", {"cancel_model": "ahead_first"}, {}),
             ("base_fit", fitted(d), {}), ("behind_first_fit", {"cancel_model": "behind_first", **fitted(d)}, {}), ("ahead_first_fit", {"cancel_model": "ahead_first", **fitted(d)}, {}),
             ("through", {"fill_through_prints": True}, {}), ("through_vol", {"fill_through_prints": True, "through_print_volume_limited": True}, {}),
             ("ledger_epoch", {"ledger_epoch": True}, {}), ("tape_off", {}, {"book_tape_repair": False}), ("capture", {}, {"event_order": "capture"}),
             ("overlay", {}, {"own_impact": "overlay"})]
        if d == "2022-01-13":                       # the floor-vs-fitted twin is dropped on this day: 2022-01-05 shows them identical (v1 never quotes outside captured depth)
            S = [x for x in S if not x[0].endswith("_fit")]
        out += [(tag, d, m, c) for tag, m, c in S]
    return out


OCT_ALL = [f"2024-10-{d:02d}" for d in range(1, 16)]
OCT_SUB = ["2024-10-01", "2024-10-04", "2024-10-07", "2024-10-10", "2024-10-14"]
SKIP = {"skip_snapshot_venues": ["binance"]}       # Binance Oct-2024 snapshots are placeholders (manifest): its book comes from deltas only


def jobs_O():
    """Oct-2024 days with trades on both venues. base on all 15 days; the other scenarios on 5 of them (+ the jobsB scenarios if present)."""
    out = [("base", d, {}, dict(SKIP)) for d in OCT_ALL]
    extra = [("behind_first", {"cancel_model": "behind_first"}), ("ahead_first", {"cancel_model": "ahead_first"})]
    try:
        for t, m in json.loads((X / "jobsB_oct.json").read_text()):
            if t not in [e[0] for e in extra]: extra.append((t, m))
    except Exception:
        pass
    for d in OCT_SUB:
        out += [(t, d, m, dict(SKIP)) for t, m in extra]
    return out


def jobs_B():
    spec = json.loads((X / "jobsB.json").read_text())
    return [(t, d, m, c) for t, d, m, c in spec]


def main():
    which, n = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 3
    jobs = {"A": jobs_A, "B": jobs_B, "O": jobs_O}[which]()
    (X / "logs").mkdir(exist_ok=True)
    pending, running = list(jobs), {}
    def done(tag, day): return (RUNS / "perp_perp_maker_taker_v1" / f"singapore_bybit_perp_mm_clean_s1_{tag}" / day / "summary.json").exists()
    while pending or running:
        for k, (p, tag, day) in list(running.items()):
            if p.poll() is not None:
                print(time.strftime("%T"), "rc", p.returncode, tag, day, flush=True); del running[k]
        while pending and len(running) < n:
            tag, day, mk, cf = pending.pop(0)
            if done(tag, day): continue
            cf = {"data_manifest": MAN, **cf}
            env = dict(os.environ, EXEC_SNAP=str(SNAP), EXEC_OUT=str(RUNS), EXEC_TAG=tag, QV2_MAKER=json.dumps(mk), QV2_CFG=json.dumps(cf))
            args = ["nice", "-n", "5", "python3.12", str(X / "qv2_run.py"), "perp_perp_maker_taker_v1", day, "--allow-rejected-data", REASON]
            if day == "2022-01-13": args += ["--train", T13]
            if day.startswith("2024"): args += ["--allow-rejected-data", REASON_OCT]
            log = open(X / "logs" / f"run_{tag}_{day}.log", "w")
            running[(tag, day)] = (subprocess.Popen(args, env=env, stdout=log, stderr=subprocess.STDOUT), tag, day)
            print(time.strftime("%T"), "start", tag, day, flush=True)
        time.sleep(5)
    print(time.strftime("%T"), "pool done", which, flush=True)
main()
