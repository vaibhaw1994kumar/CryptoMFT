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


# 7 Nov final-v1_2 cells (March only; the profile as committed: March windows 0.15 s / 50 ms applied by the runner, placement_book view, through rule off, ledger on, stamps untrusted):
#   f0 / f1 = ledger off / on with the March stamps TRUSTED (the constructed 2 ms stamps used as they are); ft = the profile as it stands (ledger on, stamps untrusted: the preset feed delay applies);
#   fp = ft + placement_book truth (alone); fbf / faf / fco = ft with cancel model behind_first / ahead_first / cohort uniform (the profile's cross rule stays on): the four-model range with ft as the headline.
# 7 Nov S0 cells: fs = v1_2 again on the final code (must equal ft byte for byte: the engine change for repricing is inert when off); v3 = v1_3 with the switch off (must equal ft too);
# B2.3 cells: jsm / fsm = the shadow with the margin comparison (January 01-13 / March), results must equal jsh / fsh (and so l0 / ft) byte for byte.
# v3on = v1_3 with the rule ON and ILLUSTRATIVE numbers (a wiring smoke run, not a setting and not a result: the numbers are the trader's to set).
ILLUSTRATIVE = {"enabled": True, "source": "ILLUSTRATIVE_NOT_FITTED", "band_down_bps": 2.0, "band_up_bps": None, "debounce_ms": None, "jump_bps": None, "min_interval_ms": 250.0, "max_age_s": 15.0,
                "queue_hold": True, "amend_first": True}
# B2 step 7 cells: jcs = jc with the 8-hour funding schedule (7a; compare jc); jl = January v1.1 with funding, margin and ledger from the core (compare jm); g1l = March v1_4 with the ledger from the core (compare g1m).
# B2 step 6 cells: jm = January v1.1 with funding_source core + margin_source core (compare jc); g1m = v1_4 headline fees on the final v1_4 (margin from the core; compare g1).
# B2 step 4 cells (v1_4, fee_source core, shadow on so the shadow's schedule is the run's own): g0 = identity cell, tiers bybit_perp_mm + binance_perp_vip0 (4.5 bps, the old ASSUMED set): must equal ft byte for byte;
# g1 = the v1_4 headline, Bybit VIP 5 + Binance VIP 9 (2.7 bps, PUBLIC_SCHEDULE_UNVERIFIED); g2 = MM-rebate scenario, bybit_perp_mm + Binance VIP 9 (1.2 bps, the rebate UNCONFIRMED).
PROFILE_OF = {"v3": "perp_perp_maker_taker_v1_3", "v3on": "perp_perp_maker_taker_v1_3", "g0": "perp_perp_maker_taker_v1_4", "g1": "perp_perp_maker_taker_v1_4", "g2": "perp_perp_maker_taker_v1_4", "g1m": "perp_perp_maker_taker_v1_4", "g1l": "perp_perp_maker_taker_v1_4"}
EXTRA_ARGS = {"g1l": ["--fee", "profile", "--binance-fee", "profile"], "g1m": ["--fee", "profile", "--binance-fee", "profile"], "g0": ["--fee", "mm", "--binance-fee", "vip0"], "g1": ["--fee", "profile", "--binance-fee", "profile"], "g2": ["--fee", "mm", "--binance-fee", "vip9"]}
FINAL = {"jl": ({}, {"funding_source": "core", "margin_source": "core", "ledger_source": "core"}), "g1l": ({}, {}), "jcs": ({}, {"funding_source": "core"}), "jm": ({}, {"funding_source": "core", "margin_source": "core"}), "g1m": ({}, {}), "jc": ({}, {"funding_source": "core"}), "g0": ({}, {"core_shadow": True}), "g1": ({}, {"core_shadow": True}), "g2": ({}, {"core_shadow": True}), "jsh": ({}, {"core_shadow": True}), "fsh": ({}, {"core_shadow": True}), "jsm": ({}, {"core_shadow": True}), "fsm": ({}, {"core_shadow": True}), "fs": ({}, {}), "v3": ({}, {}), "v3on": ({}, {}), "f0": ({"depletion_ledger": False}, {"trust_local_ts": {"bybit": True, "binance": True}}),
         "f1": ({"depletion_ledger": True}, {"trust_local_ts": {"bybit": True, "binance": True}}),
         "ft": ({}, {}),
         "fp": ({}, {"placement_book": "truth"}),
         "fbf": ({"cancel_model": "behind_first"}, {}),
         "faf": ({"cancel_model": "ahead_first"}, {}),
         "fco": ({"queue_model": "cohort", "cohort_age": "uniform"}, {})}


def variant(cell):
    if cell == "t1":
        return {}                                  # the profile alone
    if cell in FINAL:
        return FINAL[cell][0]
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
                env = dict(os.environ, **({"QV2_REPRICE": json.dumps(ILLUSTRATIVE)} if cell == "v3on" else {}), QV2_SNAP=str(LIVE), QV2_RUNS=str(runs), QV2_DATA_ROOT=str(X / "slices_mar2024"),
                           QV2_MAKER=json.dumps({"unknown_queue_fit": str(X / "unknown_queue_fit_mar2024_NONE.json"), **mk}),
                           QV2_CFG=json.dumps({"max_event_ts": t1, "data_manifest": str(X / "manifest_mar2024.json"), **FINAL.get(cell, ({}, {}))[1]}))
                cmd = ["python3.12", "-I", str(X / "qv2_march_run.py"), PROFILE_OF.get(cell, MAR_PROF), day, "--group", "base", "--cell", PROFILE_OF.get(cell, MAR_PROF), "--allow-manifest-gap", REASON_MAR, *EXTRA_ARGS.get(cell, [])]      # v1_2 headline runs override the manifest only: the preflight blocks (owner 6 Nov)
                out.append((f"{cell}/{day}", runs / "base" / PROFILE_OF.get(cell, MAR_PROF) / day / "summary.json", env, cmd))
        else:
            env = dict(os.environ, EXEC_SNAP=str(LIVE), EXEC_OUT=str(runs / "jan"), EXEC_TAG="base", QV2_HEDGE_MOVE_FILE=HM_JAN,
                       QV2_MAKER=json.dumps({"unknown_queue_fit": str(X / f"unknown_queue_fit_for_{JAN_DAY}.json"), **mk}),
                       QV2_CFG=json.dumps({"data_manifest": str(X / "manifest.json"), "message_atomic_book": True, **FINAL.get(cell, ({}, {}))[1]}), QV2_VENUE_TICKS=json.dumps(TICKS_JAN))
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
