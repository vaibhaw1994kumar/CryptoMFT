"""Fit the Binance-perp flicker law from hedge-capture replays (execution layer, phase 1).

usage: fit_flicker.py [--cap-root out/exec_layer/cap] [--tag singapore_bybit_perp_mm_clean_s1] [--out config/flicker_binance_perp.json]
                      [--set TESTDAY:TRAIN1,TRAIN2,...]...          (default: 2022-01-13 <- 01-05..01-10 ; 2022-01-05 <- 01-06..01-10)

Each set trains ONLY on the listed days. A set whose training days all come after the test day is labelled FORWARD_LOOKING (it
cannot be out-of-sample); a set whose days are all strictly earlier is OUT_OF_SAMPLE. Measurement rule: see
new_backtest/execution/flicker_fit.py. Output: the versioned law file with a content hash, per-cell n / fit / KS / QQ table."""
import argparse, gzip, json, sys
from pathlib import Path

P = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P / "src"))
from new_backtest.execution import flicker_fit as ff

ap = argparse.ArgumentParser()
ap.add_argument("--cap-root", default=str(P.parent / "out" / "exec_layer" / "cap"))
ap.add_argument("--tag", default="singapore_bybit_perp_mm_clean_s1")
ap.add_argument("--out", default=str(P / "config" / "flicker_binance_perp.json"))
ap.add_argument("--set", action="append", default=[])
ap.add_argument("--snapshot-fingerprint", default="")
a = ap.parse_args()
SETS = a.set or ["2022-01-13:2022-01-05,2022-01-06,2022-01-07,2022-01-08,2022-01-09,2022-01-10",
                 "2022-01-05:2022-01-06,2022-01-07,2022-01-08,2022-01-09,2022-01-10"]


def load_rows(day):
    f = Path(a.cap_root) / "perp_perp_maker_taker_v1" / a.tag / day / "hedge_capture.json.gz"
    if not f.exists():
        return None
    with gzip.open(f, "rt") as g:
        return json.load(g)["attempts"]


sets, per_day = {}, {}
for spec in SETS:
    test_day, days = spec.split(":")
    days = days.split(",")
    rows, used, missing = [], [], []
    for d in days:
        r = load_rows(d)
        if r is None:
            missing.append(d); continue
        rows += [x for x in r if x["venue"] == "binance"]
        used.append(d); per_day[d] = len([x for x in r if x["venue"] == "binance"])
    label = "OUT_OF_SAMPLE" if all(d < test_day for d in used) else ("FORWARD_LOOKING" if all(d > test_day for d in used) else "MIXED_NOT_OUT_OF_SAMPLE")
    s = ff.fit_set(rows)
    s.update({"test_day": test_day, "train_days_requested": days, "train_days_used": used, "train_days_missing": missing, "label": label})
    sets[f"for_{test_day}"] = s

doc = {"schema": "flicker/1", "product": "perp", "venue": "binance", "source": "REPLAY_2022",
       "cutoff": max(d for s in sets.values() for d in s["train_days_used"]) if any(s["train_days_used"] for s in sets.values()) else None,
       "units": "bps of the fundamental, FAVOURABLE sign (sell: bid up; buy: ask down)",
       "measurement": {"flicker": "arrival touch minus fundamental, favourable bps", "fundamental": "median of the strategy-view touch over the 0.25 s before the decision (reversion filter in time)",
                       "arrival_book": "the book the hedge matches against: the strategy view (engine: _execute_hedge_with_faults / _execute_arrival with truth books off)",
                       "decision_window_s": 0.25, "zero_tolerance_bps": ff.TOL_BPS, "min_n": ff.MIN_N, "min_tail": ff.MIN_TAIL,
                       "hour_bucket_h": ff.HOUR_BUCKET_H, "volatility": "1 s range of the view mid at decision, terciles of the training set",
                       "fit": "mixture MLE: p0 = share |F| <= tol; eta = 1 / mean tail size; fallback cell -> side|tercile -> side -> pooled when n < min_n"},
       "capture": {"tag": a.tag, "snapshot_fingerprint": a.snapshot_fingerprint, "binance_attempts_per_day": per_day},
       "caveats": ["Jan-2022 books are tick-discrete (SOL tick 0.01): flickers are multiples of a tick, so the exponential tails are an approximation (see the QQ tables)",
                   "few hundred hedge attempts per training set: terciles x sides x hour buckets mostly fall back to a parent cell (the level and n are in each cell)",
                   "REPLAY_2022 partial Binance coverage; not a mainnet measurement"],
       "sets": sets}
doc["content_sha256"] = ff.content_sha256(doc)
Path(a.out).write_text(json.dumps(doc, indent=1, default=str))
print(f"wrote {a.out}  sha256 {doc['content_sha256'][:16]}")
for k, s in sets.items():
    print(k, s["label"], "used", s["train_days_used"], "missing", s["train_days_missing"], "n", s["n_samples"],
          {c: (v["n"], None if v["ks"] is None else round(v["ks"], 3)) for c, v in s["cells"].items() if c in ("pooled", "sell", "buy")})
