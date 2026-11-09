"""S1 run wrapper (argument `holdout`: the three HOLDOUT days 03-13..03-15 into labels_holdout/, for the single scoring in s1_holdout_score.py; owner 9 Nov, HOLDOUT used once): label rows at 50 ms, 150 ms, 1 s, 5 s and 30 s for the TRAIN and SELECT days (HOLDOUT 03-12..03-15 is not in the list). Market data only (Bybit prints, Binance mid). Three days at a time."""
import os, subprocess, sys, time
from pathlib import Path
ROOT = Path("/Users/vaibhav/Documents/Newproject")
HOLD = len(sys.argv) > 1 and sys.argv[1] == "holdout"
OUT = ROOT / ("out/integration/s1/labels_holdout" if HOLD else "out/integration/s1/labels_h")
OUT.mkdir(exist_ok=True)
DAYS = [f"2024-03-{d:02d}" for d in (range(13, 16) if HOLD else range(1, 12))]
env = {**os.environ, "PYTHONPATH": str(ROOT / "crypto_MFT_arb_platform/src")}
pending, running = list(DAYS), {}
while pending or running:
    for d, p in list(running.items()):
        if p.poll() is not None:
            print("done", d, "rc", p.returncode, flush=True)
            del running[d]
    while pending and len(running) < 3:
        d = pending.pop(0)
        if (OUT / f"labels_{d}.json").exists():
            continue
        running[d] = subprocess.Popen([sys.executable, "-m", "new_backtest.market_hedge_move", "--data-root", str(ROOT / "data"), "--pair", "SOLUSDT", "--days", d, "--out", "/dev/null",
                                       "--label-horizons", "0.05,0.15,1,5,30", "--labels-out", str(OUT / f"labels_{d}.json")], env=env, cwd=ROOT / "crypto_MFT_arb_platform",
                                      stdout=open(OUT / f"{d}.log", "w"), stderr=subprocess.STDOUT)
    time.sleep(3)
print("all done")
