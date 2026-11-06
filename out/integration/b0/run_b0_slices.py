"""Phase B0 run wrapper (no logic): the four 2-hour March-2024 SELECT slices (03-08..03-11, 00:00-02:00 UTC) through the existing runner out/queue_v2/qv2_march_run.py
against the LIVE tree, Singapore latency, bybit_perp_mm, clean, size 1, seed 1, EXPLORATORY. Same recipe as out/integration/baselines/run_baselines.py (manifest, fitted unknown queue, ticks, Bybit-only makers).
cells:  a = v1.1 with the market-data hedge move (control; identical to the Phase A baselines)
        b = v1.1 with the PROBE hedge move                                     (B0.2: the hedge-move change alone)
        d1 = v1_2 after the owner's 6 Nov pin (7-day 45 ms arrival estimate -0.488); d2 = d1 + min_ms 35 on the Binance entry and cancel timelines; d3 = d2 + message_atomic_book; d4 = d3 + A9 above_last_tier, query_before_replace and the read-only core accounting shadow (core_shadow.json); s_bf / s_af / s_co = d4 with the cancel model behind_first / ahead_first / cohort uniform (with the profile cross rule since the 6 Nov cohort fixes): the sensitivity range (headline = d4, proportional); s130 = d4 with the 130 ms hedge move -0.862
        c = v1_2 (join the touch when tick rounding lands on it), the PROBE hedge move pinned in the profile, nothing else from the environment   (B0.3: the switch alone, vs cell b)
        tp = cell a + MakerConfig fill_through_prints and through_print_volume_limited (owner 6 Nov; the same four slices, nothing else changed)
usage: run_b0_slices.py CELL [N_PARALLEL [PRESET]]   -> out/integration/b0/runs/CELL[_PRESET]/...   Resumable. PRESET singapore (default) or tokyo (labelled LATENCY_SENSITIVITY, B0 follow-up c)."""
import calendar, json, os, subprocess, sys, time
from pathlib import Path

ROOT = Path("/Users/vaibhav/Documents/Newproject")
LIVE = ROOT / "crypto_MFT_arb_platform"
X = ROOT / "out" / "queue_v2"
cell = sys.argv[1]
n = int(sys.argv[2]) if len(sys.argv) > 2 else 2
PRESET = sys.argv[3] if len(sys.argv) > 3 else "singapore"
RUNS = ROOT / "out" / "integration" / "b0" / "runs" / (cell if PRESET == "singapore" else f"{cell}_{PRESET}")
DAYS = ("2024-03-08", "2024-03-09", "2024-03-10", "2024-03-11")
REASON = ("exploratory: 2-hour slice; Binance March-2024 has bookTicker (L1) only (manifest EMPTY); funding not settled; makers judged against the tape of the venue they rest on")
PROF = {"a": "perp_perp_maker_taker_v1_1", "tp": "perp_perp_maker_taker_v1_1", "b": "perp_perp_maker_taker_v1_1", "c": "perp_perp_maker_taker_v1_2", "d1": "perp_perp_maker_taker_v1_2", "d2": "perp_perp_maker_taker_v1_2", "d3": "perp_perp_maker_taker_v1_2", "d4": "perp_perp_maker_taker_v1_2", "s_bf": "perp_perp_maker_taker_v1_2", "s_af": "perp_perp_maker_taker_v1_2", "s_co": "perp_perp_maker_taker_v1_2", "s130": "perp_perp_maker_taker_v1_2"}[cell]
HM = str(X / "hedge_move" / ("mar_train_2024-03-01_07.json" if cell in ("a", "tp") else "probe_train_2024-03-01_03.json"))
TICKS = {"bybit": 0.001, "binance": 0.001}


def job(day):
    t1 = calendar.timegm(time.strptime(day, "%Y-%m-%d")) + 7200 + 1.0
    env = dict(os.environ, QV2_SNAP=str(LIVE), QV2_RUNS=str(RUNS), QV2_DATA_ROOT=str(X / "slices_mar2024"), QV2_HEDGE_MOVE_FILE=HM,
               QV2_MAKER=json.dumps({"unknown_queue_fit": str(X / "unknown_queue_fit_mar2024_NONE.json"), **({"fill_through_prints": True, "through_print_volume_limited": True} if cell == "tp" else {}), **({"queue_model": "cohort", "cohort_age": "uniform"} if cell == "s_co" else {})}),
               QV2_CFG=json.dumps({"max_event_ts": t1, "data_manifest": str(X / "manifest_mar2024.json"), **({"core_shadow": True} if cell in ("d4", "s_bf", "s_af", "s_co", "s130") else {})}),
               QV2_VENUE_TICKS=json.dumps(TICKS), QV2_MAKER_VENUES=json.dumps(["bybit"]))
    if cell in ("c", "d1", "d2", "d3", "d4", "s_bf", "s_af", "s_co", "s130"):                      # v1_2 carries maker_venues, venue_ticks, block_on_toxic, skew, fill-gate horizon and the pinned hedge-move file itself: the runner supplies none of them
        for k in ("QV2_HEDGE_MOVE_FILE", "QV2_VENUE_TICKS", "QV2_MAKER_VENUES"):
            env.pop(k, None)
    done = RUNS / "base" / PROF / day / "summary.json"
    cmd = ["python3.12", "-I", str(X / "qv2_march_run.py"), PROF, day, "--group", "base", "--cell", PROF, "--allow-rejected-data", REASON]
    if PRESET != "singapore":
        cmd += ["--latency-preset", PRESET]
    if cell in ("s_bf", "s_af"):
        cmd += ["--cancel-model", {"s_bf": "behind_first", "s_af": "ahead_first"}[cell]]
    if cell == "s130":
        cmd += ["--hedge-move-sensitivity", "config/hedge_move_arrival130ms_mar2024_train_0301_0307_v1.json"]
    return done, env, cmd


pending, running = [d for d in DAYS if not job(d)[0].exists()], {}
while pending or running:
    for d, p in list(running.items()):
        if p.poll() is not None:
            print(time.strftime("%T"), "rc", p.returncode, cell, d, flush=True)
            del running[d]
    if pending and len(running) < n:
        d = pending.pop(0)
        _, env, cmd = job(d)
        log = open(RUNS.parent / f"{RUNS.name}_{d}.log", "w")
        running[d] = subprocess.Popen(["nice", "-n", "5"] + cmd, env=env, stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT / "crypto_MFT_arb_platform"))
        print(time.strftime("%T"), "start", cell, d, flush=True)
    time.sleep(5)
print(time.strftime("%T"), "cell", cell, "done", flush=True)
