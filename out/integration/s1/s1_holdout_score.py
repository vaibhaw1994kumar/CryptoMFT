"""S1 (owner 9 Nov), a run wrapper: score the PINNED 50 ms adverse-selection table ONCE on HOLDOUT (2024-03-13..15), prediction metrics only.
The table is `config/adverse_table_mar2024_train_0301_0307_fullday_v1.json` (sha256 checked; its fit days must all be TRAIN). The baseline is the form v1_5 used: one mean move per side, fitted on the same TRAIN labels.
Per HOLDOUT day and pooled: mean squared error, 0.1 %-trimmed squared error, median absolute error, at 50 ms (the score that counts) and at 1 s (information). No strategy, no fills, no replay: labels are market data only.
HOLDOUT is used once for S1 and S2: this wrapper writes a lock file next to its result and refuses to run again.
Usage: s1_holdout_score.py HOLDOUT_LABELS_DIR TRAIN_LABELS_DIR OUT_JSON"""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from crypto_stat_arb.strategies.edge import AdverseTable, fit_adverse_table

HOLD_DIR, TRAIN_DIR, OUT = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
LOCK = OUT.with_suffix(".lock")
if LOCK.exists():
    raise SystemExit(f"{LOCK} exists: HOLDOUT was already scored for S1 and is not used again")
TABLE = Path("/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/adverse_table_mar2024_train_0301_0307_fullday_v1.json")
PINNED = "9c06b5c983e7d56cbaeacbd6eb188a6f42fa79246fd78e99febafbe090198854"
TRAIN = [f"2024-03-0{d}" for d in range(1, 8)]
HOLDOUT = ["2024-03-13", "2024-03-14", "2024-03-15"]
HORIZONS = (0.05, 0.15, 1.0, 5.0, 30.0)
assert hashlib.sha256(TABLE.read_bytes()).hexdigest() == PINNED, "the pinned table changed"
table = AdverseTable.load(TABLE)
assert all(d in TRAIN for d in json.loads(TABLE.read_text())["fitted_on"]["days"])
load = lambda d, days: [r for day in days for r in json.loads((d / f"labels_{day}.json").read_text())["rows"]]
train = load(TRAIN_DIR, TRAIN)
baseline = fit_adverse_table(train, dims=(), depth_edges=(1,), shrink_k=0.0, horizons_s=HORIZONS, fitted_on={"days": TRAIN, "source": "MARKET_DATA_LABELS", "events": len(train), "scheme": []})
by_day = {d: load(HOLD_DIR, [d]) for d in HOLDOUT}


def errors(t, rows, h):
    key = f"move_{h:g}s"
    return [r[key] - t.expected_move_bps(r["side"], depth_ticks=r["depth_ticks"], vol_bps=r["vol_bps"], flow=r["flow"], spread_bps=r["spread_bps"], horizon_s=h) for r in rows if r[key] is not None]


def scores(errs):
    sq = sorted(e * e for e in errs)
    cut = int(len(sq) * 0.999)
    ab = sorted(abs(e) for e in errs)
    return {"n": len(errs), "mse": sum(sq) / len(sq), "trimmed_mse_0.1pct": sum(sq[:cut]) / cut, "median_abs_error": ab[len(ab) // 2]}


result = {"label": "EXPLORATORY: S1 adverse-selection table scored ONCE on HOLDOUT, prediction metrics only; market-data labels, no strategy fill", "table_sha256": PINNED, "holdout_days": HOLDOUT,
          "baseline": "one mean move per side, fitted on the same TRAIN labels (the form v1_5 used)", "horizons": {}}
for h in (0.05, 1.0):
    per = {}
    for d in HOLDOUT + ["all_holdout"]:
        rows = [r for x in HOLDOUT for r in by_day[x]] if d == "all_holdout" else by_day[d]
        b, c = scores(errors(baseline, rows, h)), scores(errors(table, rows, h))
        per[d] = {"baseline": b, "table": c, "gain": {m: 1 - c[m] / b[m] for m in ("mse", "trimmed_mse_0.1pct", "median_abs_error")}}
    result["horizons"][f"{h:g}"] = per
    print(f"--- HOLDOUT at {h:g} s (gain = 1 - table / baseline; positive = the table is better)")
    for d, v in per.items():
        print(f"  {d:12s} n={v['baseline']['n']:7d}  mse {v['baseline']['mse']:.4f} -> {v['table']['mse']:.4f} ({100*v['gain']['mse']:+.2f}%)  trimmed {100*v['gain']['trimmed_mse_0.1pct']:+.2f}%  median|err| {v['baseline']['median_abs_error']:.4f} -> {v['table']['median_abs_error']:.4f} ({100*v['gain']['median_abs_error']:+.2f}%)")
OUT.write_text(json.dumps(result, indent=1))
LOCK.write_text("HOLDOUT scored once for S1 (owner 9 Nov); do not run again\n")
