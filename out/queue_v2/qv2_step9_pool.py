"""v1.1 step 9 January re-run (owner, 2026-11-04): the profile FILE perp_perp_maker_taker_v1_1 (steps 1-8 + the step-9 corrections) on 2022-01-05 and 2022-01-13, frozen snapshot snap_qv2i,
singapore, bybit_perp_mm, clean, size 1, seed 1 (EXPLORATORY). Expected hedge move: market data on the TRAIN days (01-13: 01-05..01-10; 01-05 has no earlier day with a usable Binance book: move 0 as before).
Ticks declared from the data (Bybit 0.005; Binance 0.001, the finest grid its books use). Makers rest on BOTH venues (Binance has depth in January). usage: qv2_step9_pool.py N_PARALLEL"""
import json, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import qv2_ladder_pool as L
X = L.X
SNAP = X / "snap_qv2i"
RUNS = X / "runs_step9"
TAG = "s9_v1_1"
REASON = "exploratory: Binance trade tape truncated at ~17 h on 2022-01-05 and 2022-01-13 (queue_v2 manifest); each maker order is judged against the tape of the venue it rests on"
HM = {"2022-01-13": str(X / "hedge_move" / "jan_train_for_2022-01-13.json")}
TICKS = {"bybit": 0.005, "binance": 0.001}


def main():
    n = int(sys.argv[1]); pending, running = list(L.DAYS), {}
    (X / "logs").mkdir(exist_ok=True)
    def done(day): return (RUNS / "perp_perp_maker_taker_v1_1" / f"singapore_bybit_perp_mm_clean_s1_{TAG}" / day / "summary.json").exists()
    while pending or running:
        for k, p in list(running.items()):
            if p.poll() is not None:
                print(time.strftime("%T"), "rc", p.returncode, k, flush=True); del running[k]
        while pending and len(running) < n:
            day = pending.pop(0)
            if done(day): continue
            env = dict(os.environ, EXEC_SNAP=str(SNAP), EXEC_OUT=str(RUNS), EXEC_TAG=TAG, QV2_MAKER=json.dumps({"unknown_queue_fit": L.FIT(day)}),
                       QV2_CFG=json.dumps({"data_manifest": L.MAN}), QV2_VENUE_TICKS=json.dumps(TICKS))
            if day in HM: env["QV2_HEDGE_MOVE_FILE"] = HM[day]
            args = ["nice", "-n", "5", "python3.12", str(X / "qv2_run.py"), "perp_perp_maker_taker_v1_1", day, "--allow-rejected-data", REASON]
            running[day] = subprocess.Popen(args, env=env, stdout=open(X / "logs" / f"step9_{TAG}_{day}.log", "w"), stderr=subprocess.STDOUT)
            print(time.strftime("%T"), "start", day, flush=True)
        time.sleep(5)
    print(time.strftime("%T"), "step9 january pool done", flush=True)


if __name__ == "__main__":
    main()
