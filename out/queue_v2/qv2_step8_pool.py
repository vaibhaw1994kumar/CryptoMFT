"""v1.1 next steps (owner, 2026-11-04): step 7 re-run with the per-venue markout split (switch-off identity check against the ladder's s7) and step 8
(opposite_cross_confirm_ms = 500). Frozen snapshot snap_qv2h. singapore, bybit_perp_mm, clean, size 1, seed 1, 2022-01-05 and 2022-01-13 (EXPLORATORY).
usage: qv2_step8_pool.py N_PARALLEL"""
import json, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import qv2_ladder_pool as L
X = L.X
SNAP = X / "snap_qv2h"
RUNS = X / "runs_step8"
REASON = "exploratory: Binance trade tape truncated at ~17 h on 2022-01-05 and 2022-01-13 (queue_v2 manifest); each maker order is judged against the tape of the venue it rests on"
S7_TAG = "s7r_markout_split"; S8_TAG = "s8_cross_confirmed"


def jobs():
    out = []
    for d in ("2022-01-13", "2022-01-05"):
        _, s7 = L.steps(d)
        out.append((S7_TAG, d, "perp_perp_maker_taker_v1", s7[1], s7[2]))
        out.append((S8_TAG, d, "perp_perp_maker_taker_v1", {**s7[1], "opposite_cross_confirm_ms": 500.0}, s7[2]))
    return out


def main():
    n = int(sys.argv[1]); pending, running = jobs(), {}
    (X / "logs").mkdir(exist_ok=True)
    def done(tag, prof, day): return (RUNS / prof / f"singapore_bybit_perp_mm_clean_s1_{tag}" / day / "summary.json").exists()
    while pending or running:
        for k, p in list(running.items()):
            if p.poll() is not None:
                print(time.strftime("%T"), "rc", p.returncode, *k, flush=True); del running[k]
        while pending and len(running) < n:
            tag, day, prof, mk, cf = pending.pop(0)
            if done(tag, prof, day): continue
            env = dict(os.environ, EXEC_SNAP=str(SNAP), EXEC_OUT=str(RUNS), EXEC_TAG=tag, QV2_MAKER=json.dumps(mk), QV2_CFG=json.dumps(cf))
            args = ["nice", "-n", "5", "python3.12", str(X / "qv2_run.py"), prof, day, "--allow-rejected-data", REASON]
            if day == "2022-01-13": args += ["--train", L.T13]
            running[(tag, day)] = subprocess.Popen(args, env=env, stdout=open(X / "logs" / f"step8_{tag}_{day}.log", "w"), stderr=subprocess.STDOUT)
            print(time.strftime("%T"), "start", tag, day, flush=True)
        time.sleep(5)
    print(time.strftime("%T"), "step8 pool done", flush=True)


if __name__ == "__main__":
    main()
