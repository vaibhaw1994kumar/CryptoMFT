"""v1.1 step 9 (profile FILE perp_perp_maker_taker_v1_1, frozen snapshot snap_qv2i) on the March-2024 days 03-08..03-15 (SELECT 03-08..12, HOLDOUT 03-13..15 of
out/exec_layer_p1lite/split.json; no choice is made from the HOLDOUT days). The expected hedge move comes from MARKET DATA on the TRAIN days 03-01..07 (hedge_move/mar_train_2024-03-01_07.json,
new_backtest.market_hedge_move), not from legacy_v0 replays: this pool no longer waits for them. Makers rest on Bybit only (Binance March-2024 has no depth), the hedge goes to Binance; ticks 0.001 on both venues.
Resumable (a day with a summary.json is skipped). usage: qv2_march_pool.py N_PARALLEL [DAY ...]"""
import json, os, subprocess, sys, time
from pathlib import Path
Q = Path("/Users/vaibhav/Documents/Newproject/out/queue_v2")
P1 = Path("/Users/vaibhav/Documents/Newproject/out/exec_layer_p1lite")
TRAIN = ["2024-03-0%d" % i for i in range(1, 8)]
DAYS = ["2024-03-08", "2024-03-09", "2024-03-10", "2024-03-11", "2024-03-12", "2024-03-13", "2024-03-14", "2024-03-15"]
MAN = str(Q / "manifest_mar2024.json")
FIT = str(Q / "unknown_queue_fit_mar2024_NONE.json")
REASON = ("exploratory: Binance March-2024 has bookTicker (L1) only, no book_events (manifest EMPTY); funding not settled (no March file); each maker order is judged against the tape of "
          "the venue it rests on")
MAKER = {"unknown_queue_fit": FIT}                      # the rest of the maker settings come from the v1_1 profile file (key-by-key equality checked)
CFG = {"data_manifest": MAN}
HM = str(Q / "hedge_move" / "mar_train_2024-03-01_07.json")
TICKS = {"bybit": 0.001, "binance": 0.001}                  # measured: gcd of the traded prices and of the first-snapshot levels of both venues on 03-05 and 03-12


def ready():
    return Path(HM).exists()


def main():
    n = int(sys.argv[1]); days = sys.argv[2:] or DAYS
    pending, running = [d for d in days if not (Q / "runs_mar2024" / "mar2024_v1_1" / "v1_1" / d / "summary.json").exists()], {}
    while pending or running:
        for d, p in list(running.items()):
            if p.poll() is not None:
                print(time.strftime("%T"), "rc", p.returncode, d, flush=True); del running[d]
        try:
            n = int((Q / "march_pool_n.txt").read_text().strip())          # the parallelism can be changed while the pool runs (RAM: ~1.7 GB per replay on an 8.6 GB machine)
        except (OSError, ValueError):
            pass
        if pending and len(running) < n and ready():
            d = pending.pop(0)
            env = dict(os.environ, QV2_MAKER=json.dumps(MAKER), QV2_CFG=json.dumps(CFG), QV2_HEDGE_MOVE_FILE=HM, QV2_VENUE_TICKS=json.dumps(TICKS), QV2_MAKER_VENUES=json.dumps(["bybit"]))
            args = ["nice", "-n", "5", "python3.12", "-I", str(Q / "qv2_march_run.py"), "perp_perp_maker_taker_v1_1", d, "--group", "mar2024_v1_1", "--cell", "v1_1",
                    "--allow-rejected-data", REASON]
            running[d] = subprocess.Popen(args, env=env, stdout=open(Q / "logs" / f"march_{d}.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
            print(time.strftime("%T"), "start", d, flush=True)
        time.sleep(30)
    print(time.strftime("%T"), "march pool done", flush=True)


if __name__ == "__main__":
    main()
