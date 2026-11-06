"""Phase B depletion-ledger run wrapper (no logic): before / after of MakerConfig.depletion_ledger on 2022-01-13 and the four 2-hour March-2024 SELECT slices (03-08..03-11, 00:00-02:00 UTC),
through the existing runners out/queue_v2/qv2_run.py (January) and qv2_march_run.py (March) against the LIVE tree, Singapore latency, bybit_perp_mm, clean, size 1, seed 1, EXPLORATORY.
March: profile perp_perp_maker_taker_v1_2 as it stands (message_atomic_book on, 45 ms pin, 35 ms floor, A9, query before replace); the switch is flipped by a MakerConfig override only.
January: perp_perp_maker_taker_v1_1 + message_atomic_book (v1_2's hedge-move file needs days before the run start, so v1_2 cannot run on January), the Phase 0 recipe otherwise.
cells:  t1 = March only: v1_2 exactly as the profile now stands (depletion_ledger on AND trust_local_ts false on both venues: the preset feed delay replaces the constructed 2 ms stamps); compare with l1 (the same profile before the stamp change).
        l0 / l1 = switch off / on, cancel model proportional (the headline); l0_bf / l1_bf = behind_first; l0_af / l1_af = ahead_first; l0_co / l1_co = cohort uniform (with the profile's cross rule, confirm window and cap-to-print).
usage: run_ledger_cells.py N_PARALLEL DATASET CELL [CELL ...]      DATASET = mar | jan      -> out/integration/phase_b/ledger/runs/CELL/{base|jan}/...   Resumable (a run with summary.json is skipped)."""
import calendar, json, os, subprocess, sys, time
from pathlib import Path

ROOT = Path("/Users/vaibhav/Documents/Newproject")
LIVE = ROOT / "crypto_MFT_arb_platform"
X = ROOT / "out" / "queue_v2"
OUT = ROOT / "out" / "integration" / "phase_b" / "ledger"
n, dataset, cells = int(sys.argv[1]), sys.argv[2], sys.argv[3:]
MAR_DAYS = ("2024-03-08", "2024-03-09", "2024-03-10", "2024-03-11")
JAN_DAY = "2022-01-13"
REASON_MAR = ("exploratory: 2-hour slice; Binance March-2024 has bookTicker (L1) only (manifest EMPTY); funding not settled; makers judged against the tape of the venue they rest on")
REASON_JAN = "exploratory: Binance trade tape truncated at ~17 h on 2022-01-05 and 2022-01-13 (queue_v2 manifest); each maker order is judged against the tape of the venue it rests on"
TICKS_MAR = {"bybit": 0.001, "binance": 0.001}
TICKS_JAN = {"bybit": 0.005, "binance": 0.001}
HM_JAN = str(X / "hedge_move" / "jan_train_for_2022-01-13.json")
MAR_PROF, JAN_PROF = "perp_perp_maker_taker_v1_2", "perp_perp_maker_taker_v1_1"


def variant(cell):
    if cell == "t1":
        return {}                                  # the profile alone
    if cell == "ck":
        return {"dedupe_window_s": 0.05, "evidence_window_ms": 20.0}          # identity check (7 Nov): v1_2 as it stood at t1 (through rule off, the old windows; the profile's March dataset_settings undone) -> must equal t1 byte for byte
    if cell == "tp2":
        return {"fill_through_prints": True, "through_print_volume_limited": True}      # t1 + the through-print rule, volume-limited (owner 6 Nov)
    on = cell.startswith("l1")
    kind = cell.split("_", 1)[1] if "_" in cell else "prop"
    mk = {"depletion_ledger": on}
    if kind in ("bf", "af"):
        mk["cancel_model"] = {"bf": "behind_first", "af": "ahead_first"}[kind]
    if kind == "co":
        mk.update(queue_model="cohort", cohort_age="uniform")      # the profile's cross rule stays on (the cohort queue accepts it since the 6 Nov cohort fixes); uniform only, no hazard rule (SPOT_CALIBRATED theta)
    return mk


def jobs():
    out = []
    for cell in cells:
        runs = OUT / "runs" / cell
        mk = variant(cell)
        if dataset == "mar":
            for day in MAR_DAYS:
                t1 = calendar.timegm(time.strptime(day, "%Y-%m-%d")) + 7200 + 1.0
                env = dict(os.environ, QV2_SNAP=str(LIVE), QV2_RUNS=str(runs), QV2_DATA_ROOT=str(X / "slices_mar2024"),
                           QV2_MAKER=json.dumps({"unknown_queue_fit": str(X / "unknown_queue_fit_mar2024_NONE.json"), **mk}),
                           QV2_CFG=json.dumps({"max_event_ts": t1, "data_manifest": str(X / "manifest_mar2024.json")}))
                cmd = ["python3.12", "-I", str(X / "qv2_march_run.py"), MAR_PROF, day, "--group", "base", "--cell", MAR_PROF, "--allow-manifest-gap", REASON_MAR]      # v1_2 headline runs override the manifest only: the preflight blocks (owner 6 Nov)
                out.append((f"{cell}/{day}", runs / "base" / MAR_PROF / day / "summary.json", env, cmd))
        else:
            env = dict(os.environ, EXEC_SNAP=str(LIVE), EXEC_OUT=str(runs / "jan"), EXEC_TAG="base", QV2_HEDGE_MOVE_FILE=HM_JAN,
                       QV2_MAKER=json.dumps({"unknown_queue_fit": str(X / f"unknown_queue_fit_for_{JAN_DAY}.json"), **mk}),
                       QV2_CFG=json.dumps({"data_manifest": str(X / "manifest.json"), "message_atomic_book": True}), QV2_VENUE_TICKS=json.dumps(TICKS_JAN))
            cmd = ["python3.12", str(X / "qv2_run.py"), JAN_PROF, JAN_DAY, "--allow-rejected-data", REASON_JAN]
            out.append((f"{cell}/{JAN_DAY}", runs / "jan" / JAN_PROF / "singapore_bybit_perp_mm_clean_s1_base" / JAN_DAY / "summary.json", env, cmd))
    return out


pending = [j for j in jobs() if not j[1].exists()]
running = {}
(OUT / "logs").mkdir(parents=True, exist_ok=True)
while pending or running:
    for name, p in list(running.items()):
        if p.poll() is not None:
            print(time.strftime("%T"), "rc", p.returncode, name, flush=True)
            del running[name]
    if pending and len(running) < n:
        name, _, env, cmd = pending.pop(0)
        log = open(OUT / "logs" / f"{dataset}_{name.replace('/', '_')}.log", "w")
        running[name] = subprocess.Popen(["nice", "-n", "5"] + cmd, env=env, stdout=log, stderr=subprocess.STDOUT, cwd=str(LIVE))
        print(time.strftime("%T"), "start", name, flush=True)
    time.sleep(5)
print(time.strftime("%T"), dataset, cells, "done", flush=True)
