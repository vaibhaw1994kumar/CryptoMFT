"""Oct-2024 BYBIT_ONLY_WINDOW maker-only analysis through the MAIN runner (qv2_maker_only.py -> analysis_mode='maker_only'): one run per (day, queue/cancel model), identical grid of
hypothetical v1-style PASSIVE orders (touch / improve / second, both sides, every 90 s, qty 1), time-in-force from the v1 profile. Binance is NOT in the replay (trade tape only, used offline).
Resumable: a run with summary.json is skipped.  usage: qv2_oct_pool.py N_PARALLEL [day ...]"""
import json, os, subprocess, sys, time
X = "/Users/vaibhav/Documents/Newproject/out/queue_v2"
SNAP = f"{X}/snap_qv2e"
MODELS = {
    "min_baseline": {"cancel_model": "all_ahead"}, "behind_first": {"cancel_model": "behind_first"}, "proportional": {"cancel_model": "proportional"},
    "ahead_first": {"cancel_model": "ahead_first"}, "power_0.5": {"cancel_model": "power", "cancel_power_n": 0.5}, "power_2.0": {"cancel_model": "power", "cancel_power_n": 2.0},
    "cohort_youngest_first": {"queue_model": "cohort", "cohort_age": "youngest_first"}, "cohort_oldest_first": {"queue_model": "cohort", "cohort_age": "oldest_first"},
    "cohort_uniform": {"queue_model": "cohort", "cohort_age": "uniform"}, "cohort_hazard_0.5": {"queue_model": "cohort", "cohort_age": "hazard", "cohort_hazard_theta": 0.5},
}
COMMON = {"opposite_cross_fills": False}          # same setting as the parity proof and the calibration runs; the live v1 profile has it on (owner edit 2026-11-04 05:09), so it is pinned here
man = json.load(open(f"{X}/manifest.json"))["days"]
n = int(sys.argv[1])
days = sys.argv[2:] or sorted(k.split("/")[2] for k in man if k.startswith("bybit/SOLUSDT/2024-10"))
jobs = []
for d in days:
    be = man[f"bybit/SOLUSDT/{d}"]["channels"]["book_events"]
    for m, kw in MODELS.items():
        out = f"{X}/maker_only/oct/{m}/{d}/summary.json"
        if os.path.exists(out):
            continue
        spec = {"tag": f"oct/{m}", "day": d, "venue": "bybit", "maker": {**COMMON, **kw}, "gate_class": "BYBIT_ONLY_WINDOW", "max_event_ts": be["last_ts"] + 0.5,
                "grid": {"start": be["first_ts"] + 120.0, "end": be["last_ts"] - 20.0, "every_s": 90.0, "rules": ["improve", "touch", "second"], "qty": 1.0},
                "allow_rejected_data": "BYBIT_ONLY_WINDOW: Binance Oct-2024 is trade tape only and is not in this replay; Bybit book hours from the manifest"}
        jobs.append((d, m, spec))
print(len(jobs), "runs to do", flush=True)
run = []
while jobs or run:
    run = [(p, j) for p, j in run if p.poll() is None]
    while jobs and len(run) < n:
        d, m, spec = jobs.pop(0)
        env = dict(os.environ, QV2_SPEC=json.dumps(spec), EXEC_SNAP=SNAP)
        log = open(f"{X}/logs/oct_{m}_{d}.log", "w")
        run.append((subprocess.Popen(["nice", "-n", "5", "python3.12", f"{X}/qv2_maker_only.py"], env=env, stdout=log, stderr=subprocess.STDOUT), (d, m)))
    time.sleep(5)
print("pool done", flush=True)
