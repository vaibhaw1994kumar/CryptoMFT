"""Phase 0 baselines (run wrapper, no logic). Replays legacy_v0, v1 and v1.1 on 2022-01-05, 2022-01-13 and the 2-hour March slice 2024-03-08 00:00-02:00 UTC
against the LIVE tree (crypto_MFT_arb_platform), using the existing runners out/queue_v2/qv2_run.py (January) and qv2_march_run.py (March) with the same recipes the
earlier gates used (qv2_step9_pool.py, qv2_ladder9_pool.py, run_legacy_check.sh). Singapore latency, bybit_perp_mm, clean, size 1, seed 1, EXPLORATORY.
Inputs that are pinned for v1 and v1.1 (so the baseline is a code identity check, not a result): the hedge-move files under out/queue_v2/hedge_move/.
usage: run_baselines.py LABEL [N_PARALLEL]   -> out/integration/baselines/runs/LABEL/...   Resumable (a run with summary.json is skipped)."""
import calendar, json, os, subprocess, sys, time
from pathlib import Path

ROOT = Path("/Users/vaibhav/Documents/Newproject")
LIVE = ROOT / "crypto_MFT_arb_platform"
X = ROOT / "out" / "queue_v2"
B = ROOT / "out" / "integration" / "baselines"
label = sys.argv[1]
n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
RUNS = B / "runs" / label
REASON_JAN = "exploratory: Binance trade tape truncated at ~17 h on 2022-01-05 and 2022-01-13 (queue_v2 manifest); each maker order is judged against the tape of the venue it rests on"
REASON_MAR = ("exploratory: 2-hour slice; Binance March-2024 has bookTicker (L1) only (manifest EMPTY); funding not settled; makers judged against the tape of the venue they rest on")
PROFILES = ("legacy_v0", "perp_perp_maker_taker_v1", "perp_perp_maker_taker_v1_1")
TICKS_JAN = {"bybit": 0.005, "binance": 0.001}
TICKS_MAR = {"bybit": 0.001, "binance": 0.001}
MAR_DAYS = ("2024-03-08", "2024-03-11")      # 03-08: v1.1 has 0 signals in its 2 h slice (an empty trade log is weak identity evidence); 03-11: v1.1 trades
HM_JAN = {"2022-01-13": str(X / "hedge_move" / "jan_train_for_2022-01-13.json")}
HM_MAR = str(X / "hedge_move" / "mar_train_2024-03-01_07.json")


def jobs():
    out = []
    for day in ("2022-01-13", "2022-01-05"):                      # the slow day first
        for prof in PROFILES:
            env = {"EXEC_SNAP": str(LIVE), "EXEC_OUT": str(RUNS / "jan"), "EXEC_TAG": "base"}
            if prof == "perp_perp_maker_taker_v1_1":
                env.update(QV2_MAKER=json.dumps({"unknown_queue_fit": str(X / f"unknown_queue_fit_for_{day}.json")}),
                           QV2_CFG=json.dumps({"data_manifest": str(X / "manifest.json")}), QV2_VENUE_TICKS=json.dumps(TICKS_JAN))
            else:
                env.update(QV2_MAKER="{}", QV2_CFG="{}")
            if prof != "legacy_v0" and day in HM_JAN:
                env["QV2_HEDGE_MOVE_FILE"] = HM_JAN[day]
            done = RUNS / "jan" / prof / "singapore_bybit_perp_mm_clean_s1_base" / day / "summary.json"
            out.append((f"jan/{prof}/{day}", done, env, ["python3.12", str(X / "qv2_run.py"), prof, day, "--allow-rejected-data", REASON_JAN]))
    for MAR_DAY in MAR_DAYS:
        t1 = calendar.timegm(time.strptime(MAR_DAY, "%Y-%m-%d")) + 7200 + 1.0
        for prof in PROFILES:
            env = {"QV2_SNAP": str(LIVE), "QV2_RUNS": str(RUNS / "mar"), "QV2_DATA_ROOT": str(X / "slices_mar2024")}
            cfg = {"max_event_ts": t1}
            mk = {}
            if prof != "legacy_v0":
                env["QV2_HEDGE_MOVE_FILE"] = HM_MAR
            if prof == "perp_perp_maker_taker_v1_1":
                mk = {"unknown_queue_fit": str(X / "unknown_queue_fit_mar2024_NONE.json")}
                cfg["data_manifest"] = str(X / "manifest_mar2024.json")
                env["QV2_VENUE_TICKS"] = json.dumps(TICKS_MAR)
                env["QV2_MAKER_VENUES"] = json.dumps(["bybit"])
            env.update(QV2_MAKER=json.dumps(mk), QV2_CFG=json.dumps(cfg))
            done = RUNS / "mar" / "base" / prof / MAR_DAY / "summary.json"
            out.append((f"mar/{prof}/{MAR_DAY}", done, env,
                        ["python3.12", "-I", str(X / "qv2_march_run.py"), prof, MAR_DAY, "--group", "base", "--cell", prof, "--allow-rejected-data", REASON_MAR]))
    return out


def main():
    only = os.environ.get("BASELINE_ONLY", "")                      # e.g. "mar": run only the jobs whose name starts with it (determinism re-check)
    pending, running = [j for j in jobs() if j[0].startswith(only) and not j[1].exists()], {}
    (B / "logs").mkdir(parents=True, exist_ok=True)
    while pending or running:
        for k, (p, t0) in list(running.items()):
            if p.poll() is not None:
                print(time.strftime("%T"), "rc", p.returncode, k, f"{time.time() - t0:.0f}s", flush=True); del running[k]
        while pending and len(running) < n:
            name, done, env, args = pending.pop(0)
            e = dict(os.environ, **env)
            log = B / "logs" / (label + "_" + name.replace("/", "_") + ".log")
            running[name] = (subprocess.Popen(["nice", "-n", "5", *args], env=e, stdout=open(log, "w"), stderr=subprocess.STDOUT, start_new_session=True), time.time())
            print(time.strftime("%T"), "start", name, flush=True)
        time.sleep(5)
    print(time.strftime("%T"), "baselines done", label, flush=True)


if __name__ == "__main__":
    main()
