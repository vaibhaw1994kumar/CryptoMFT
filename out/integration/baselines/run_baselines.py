"""Phase 0 baselines (run wrapper, no logic). Replays legacy_v0, v1 and v1.1 on 2022-01-05, 2022-01-13 and the 2-hour March slice 2024-03-08 00:00-02:00 UTC
against the LIVE tree (crypto_MFT_arb_platform), using the existing runners out/queue_v2/qv2_run.py (January) and qv2_march_run.py (March) with the same recipes the
earlier gates used (qv2_step9_pool.py, qv2_ladder9_pool.py, run_legacy_check.sh). Singapore latency, bybit_perp_mm, clean, size 1, seed 1, EXPLORATORY.
Inputs that are pinned for v1 and v1.1 (so the baseline is a code identity check, not a result): the hedge-move files under out/queue_v2/hedge_move/.
usage: run_baselines.py LABEL [N_PARALLEL]   -> out/integration/baselines/runs/LABEL/...   Resumable (a run with summary.json is skipped)."""
import calendar, json, os, subprocess, sys, time
from pathlib import Path

ROOT = Path("/Users/vaibhav/Documents/Newproject")
LIVE = Path(os.environ.get("BASELINE_SNAP", ROOT / "crypto_MFT_arb_platform"))   # BASELINE_SNAP: run another tree (e.g. the pre-Phase-A backup) instead of the live one
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
FAULTS = (("realistic", 1), ("stressed", 1), ("stressed", 2), ("stressed", 3), ("realistic", 2))        # (preset, seed); the realistic seed is changed in the label notes if nothing fires with seed 1
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
    # Gate-approved additions (owner, 5 Nov). (1) The frozen v1 reference: v1 on 2022-01-13 with its ORIGINAL legacy-trained hedge move (pooled legacy_v0 replays of 01-05..01-10, -1.737 bps bid),
    # which gave 45 fills and cash +1.0734 on every earlier gate. The standard v1 baseline above uses the market-data hedge-move file instead, hence its different count.
    # (2) Faulted v1.1 on 2022-01-13, seed 1, presets realistic and stressed.
    day = "2022-01-13"
    T13 = "2022-01-05,2022-01-06,2022-01-07,2022-01-08,2022-01-09,2022-01-10"
    envf = {"EXEC_SNAP": str(LIVE), "EXEC_OUT": str(RUNS / "jan"), "EXEC_TAG": "frozen", "QV2_MAKER": "{}", "QV2_CFG": "{}"}
    out.append(("jan/perp_perp_maker_taker_v1@frozen/2022-01-13", RUNS / "jan" / "perp_perp_maker_taker_v1" / "singapore_bybit_perp_mm_clean_s1_frozen" / day / "summary.json", envf,
                ["python3.12", str(X / "qv2_run.py"), "perp_perp_maker_taker_v1", day, "--train", T13, "--allow-rejected-data", REASON_JAN]))
    for preset in FAULTS:
        env = {"EXEC_SNAP": str(LIVE), "EXEC_OUT": str(RUNS / "jan"), "EXEC_TAG": "base", "QV2_HEDGE_MOVE_FILE": HM_JAN[day],
               "QV2_MAKER": json.dumps({"unknown_queue_fit": str(X / f"unknown_queue_fit_for_{day}.json")}),
               "QV2_CFG": json.dumps({"data_manifest": str(X / "manifest.json")}), "QV2_VENUE_TICKS": json.dumps(TICKS_JAN)}
        p, seed = preset
        out.append((f"jan/perp_perp_maker_taker_v1_1@{p}_s{seed}/{day}", RUNS / "jan" / "perp_perp_maker_taker_v1_1" / f"singapore_bybit_perp_mm_{p}_s{seed}_base" / day / "summary.json", env,
                    ["python3.12", str(X / "qv2_run.py"), "perp_perp_maker_taker_v1_1", day, "--faults", p, "--seed", str(seed), "--allow-rejected-data", REASON_JAN]))
    # Short faulted checks (Phase A, per move): the four faulted runs that fire something (stressed seeds 1-3, realistic seed 2), cut to the first 7,500 s of 2022-01-13 so each takes
    # minutes. 7,500 s covers every trip (274, 1075, 2968 and 6800 s). Reference = the same runs on the pre-Phase-A tree (BASELINE_SNAP=backup), label f0.
    cut = calendar.timegm(time.strptime("2022-01-13", "%Y-%m-%d")) + 7500.0 + 1.0
    for p, seed in (("stressed", 1), ("stressed", 2), ("stressed", 3), ("realistic", 2)):
        env = {"EXEC_SNAP": str(LIVE), "EXEC_OUT": str(RUNS / "short"), "EXEC_TAG": "base", "QV2_HEDGE_MOVE_FILE": HM_JAN["2022-01-13"],
               "QV2_MAKER": json.dumps({"unknown_queue_fit": str(X / "unknown_queue_fit_for_2022-01-13.json")}),
               "QV2_CFG": json.dumps({"data_manifest": str(X / "manifest.json"), "max_event_ts": cut}), "QV2_VENUE_TICKS": json.dumps(TICKS_JAN)}
        out.append((f"short/perp_perp_maker_taker_v1_1@{p}_s{seed}/2022-01-13", RUNS / "short" / "perp_perp_maker_taker_v1_1" / f"singapore_bybit_perp_mm_{p}_s{seed}_base" / "2022-01-13" / "summary.json", env,
                    ["python3.12", str(X / "qv2_run.py"), "perp_perp_maker_taker_v1_1", "2022-01-13", "--faults", p, "--seed", str(seed), "--allow-rejected-data", REASON_JAN]))
    return out


def main():
    only = os.environ.get("BASELINE_ONLY", "")                      # e.g. "mar": run only the jobs whose name starts with it; "re:<regex>": jobs whose name matches
    import re
    pick = (lambda n: re.search(only[3:], n)) if only.startswith("re:") else (lambda n: n.startswith(only))
    skip = os.environ.get("BASELINE_SKIP", "")                      # regex: jobs whose name matches are not run (owner-approved omissions are named in the report)
    pending, running = [j for j in jobs() if pick(j[0]) and not j[1].exists() and (only.startswith("short") or not j[0].startswith("short/")) and not (skip and re.search(skip, j[0]))], {}
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
