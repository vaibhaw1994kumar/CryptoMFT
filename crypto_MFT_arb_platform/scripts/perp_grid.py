#!/usr/bin/env python3
"""Resumable job pool for the perp fault grid (lifecycle task 7).

    python3 scripts/perp_grid.py --out-root OUT --data-root DATA --workers 4 [--shard 0/6] [--stage A,B] [--seeds 20] [--dry-run]

Stages (priority order; a job whose summary.json exists is skipped, so the pool can be stopped and restarted):
  A  conditional scenarios, size 1, both days                       12 x 2        =  24 runs
  B  presets clean/clean_managed/realistic/stressed, size 1, seeds  4 x 2 x S     = 160 runs at S=20 (seed-major: every preset gets seed k before any gets k+1)
  C  conditional scenarios, size 5                                  12 x 2        =  24 runs
  D  presets, size 5                                                4 x 2 x S     = 160 runs at S=20
Each run is a full day (about 33 M events) and takes minutes to tens of minutes, so the whole grid is hours of CPU; --shard 0,1,2/6 takes jobs whose index mod 6 is 0, 1 or 2.
The pool never kills anything but its own children."""
import argparse, itertools, json, os, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from new_backtest.fault_presets import CONDITIONAL, PERP_PRESETS     # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--out-root", required=True); ap.add_argument("--data-root", required=True); ap.add_argument("--funding-dir", default=None)
ap.add_argument("--workers", type=int, default=1); ap.add_argument("--stage", default="A,B,C,D"); ap.add_argument("--seeds", type=int, default=20)
ap.add_argument("--shard", default="0/1"); ap.add_argument("--python", default=sys.executable); ap.add_argument("--nice", type=int, default=5)
ap.add_argument("--days", default="2022-01-05,2022-01-13"); ap.add_argument("--dry-run", action="store_true")
a = ap.parse_args()
days = a.days.split(",")
jobs = []
for st in a.stage.split(","):
    size = 1 if st in ("A", "B") else 5
    if st in ("A", "C"):
        jobs += [("scenario", n, d, 0, size) for n in CONDITIONAL for d in days]
    else:
        jobs += [("preset", n, d, s, size) for s in range(a.seeds) for n in PERP_PRESETS for d in days]
idx, n = a.shard.split("/")
n = int(n)
keep = {int(x) for x in idx.split(",")}
jobs = [j for k, j in enumerate(jobs) if k % n in keep]
def done(j):
    k, name, d, s, size = j
    return (Path(a.out_root) / k / name / f"size{size:g}" / d / f"seed{s}" / "summary.json").exists()
todo = [j for j in jobs if not done(j)]
print(f"{len(jobs)} jobs in this shard, {len(jobs) - len(todo)} already done, {len(todo)} to run, {a.workers} workers", flush=True)
if a.dry_run:
    sys.exit(0)
running, k0, t0, ok, bad = [], 0, time.time(), 0, 0
while todo or running:
    while todo and len(running) < a.workers:
        k, name, d, s, size = todo.pop(0)
        cmd = ["nice", "-n", str(a.nice), a.python, str(ROOT / "scripts/perp_fault_run.py"), k, name, d, "--seed", str(s), "--size", str(size),
               "--out-root", a.out_root, "--data-root", a.data_root] + (["--funding-dir", a.funding_dir] if a.funding_dir else [])
        log = Path(a.out_root) / "logs"; log.mkdir(parents=True, exist_ok=True)
        lf = open(log / f"{k}_{name}_{d}_s{s}_z{size:g}.log", "w")
        env = dict(os.environ, NEW_BACKTEST_PREFLIGHT_CACHE=str(Path(a.out_root) / "preflight_cache"))      # the day scan is reused across runs
        running.append((subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env), (k, name, d, s, size), time.time()))
    for r in list(running):
        if r[0].poll() is not None:
            running.remove(r)
            if r[0].returncode == 0: ok += 1
            else: bad += 1
            print(f"[{time.strftime('%H:%M:%S')}] {'ok  ' if r[0].returncode == 0 else 'FAIL'} {r[1]} {time.time() - r[2]:.0f}s  (ok {ok}, failed {bad}, left {len(todo) + len(running)})", flush=True)
    time.sleep(5)
print(f"pool finished: ok {ok}, failed {bad}, {time.time() - t0:.0f}s", flush=True)
