"""S3 do-not-quote filter (owner 10 Nov, = design Phase 2, Albers features): a run wrapper around `new_backtest.market_hedge_move.QuoteFilterStudy` and `strategies.edge.DoNotQuoteFilter` (the logic lives in src).
EVENTS: every market-data moment where the v1_6 ENTRY RULE would post a Bybit quote and a sweep would fill it (the S2 events), under two queue models, scored separately: `clear` = the sweep must print the visible queue ahead plus our size
(the S2 rule), `touch` = touching our price is enough. FEATURES (known at quote time, `QuoteFeatures`): book imbalance at the touch and top 5 (Bybit), the hedge venue's touch share (Binance best bid/ask only), printed volume by side over 1 s,
queue ahead at our price, spread, short hedge-mid volatility, time since the maker touch last moved, the naive edge after fees (no expected move in it). They are read at the sweep time MINUS A LAG: 8 ms (Bybit feed 2 + cancel 6, singapore preset,
ASSUMED) is the one that counts, because a standing do-not-quote state could cancel a quote only that much ahead of the sweep; 1 s is reported as the filter acting on a quote that was not cancelled in time. LABEL: the Binance mid move after the fill at
50 ms (the one used) and 1 s (information), market data only. REALISED NET per event = minus the slack-5 hedge chain cost of the S2 study (fees, retries, unwinds, drift included). Nothing is fitted on a simulated fill.
RULES, WRITTEN BEFORE SELECT IS SCORED:
  (1) FIT on TRAIN only (DEVIATION recorded 10 Nov, 'cut it short': 2024-03-01..04 and 03-06, full days; 03-05 unfinished, 03-07 dropped): one ridge model per side (penalty 10, standardised features) of the 50 ms move on the features at the 8 ms lag, on the `touch`-rule events (the superset). The threshold is NOT fitted.
  (2) RULE: skip the quote when naive_edge_bps + predicted_move_bps < tau. tau is chosen on SELECT (DEVIATION: 2024-03-08..03-11 as 2-hour slices, 00:00-02:00 UTC; 03-12 dropped) from {-1.0, -0.5, 0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0} as the one with the largest total net bps avoided under the `clear` rule,
      where avoided = minus the sum of the realised net of the skipped events (positive = the skipped quotes lost money).
  (3) ACCEPT the filter only if, at the chosen tau on SELECT, the gain is positive under BOTH queue rules AND the 90% day-block bootstrap interval (4 SELECT day blocks, 10,000 resamples, seed 1) of the gain per event under `clear` excludes zero.
      With five day blocks the interval is COARSE and is reported as such. Otherwise v1_7 ships with the filter OFF and the finding is reported as it is.
  (4) COMPARE against keep-all and against v1_6's own `block_on_toxic` gate reproduced on the same stream (the core VenueState, the profile's thresholds), on the same events and the same score; the filter has to beat the gate, not only keep-all.
  (5) HOLDOUT 2024-03-13..03-15 is extracted and scored ONCE, only after the model and tau are frozen (file and sha256 written), with its own lock file; prediction metrics (MSE, trimmed MSE, median absolute error, per day) and the same net score. Used once before for S1 (prediction only).
  (6) BLOCK_ON_TOXIC ON HOLDOUT (owner 10 Nov), scored ONCE, gate flag only, no model: on the HOLDOUT 03-13..15 events (full days, both queue rules) the net of the events the v1_6 `block_on_toxic` gate would flag (at the 8 ms lag) against the net of the unflagged events;
      D = mean net flagged - mean net unflagged; 90% day-block bootstrap of D (3 day blocks, 10,000 resamples, seed 1). DECISION: if the 5th percentile of D under the `clear` rule is NOT below -1.0 bps (flagged events are not worse), build v1_7 = v1_6 with
      block_on_toxic off, pinned, EXPLORATORY; otherwise keep the gate. Lock file `s3_gate_holdout.lock`. The HOLDOUT event files are kept for the later S3 rerun (and were not used to choose anything).
usage: dnq_study.py extract [N_PARALLEL] [DAY,DAY,...]      TRAIN + SELECT days, or the days named (resumable) -> s3/study/days/
       dnq_study.py fit                        rules 1-4 -> s3/study/dnq_v1.json (model at the chosen tau), s3/study/s3_select_report.json
       dnq_study.py holdout [N_PARALLEL]       freeze check, extract 03-13..15, score once -> s3/study/s3_holdout_report.json (+ lock)"""
import hashlib
import json
import math
import os
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/Users/vaibhav/Documents/Newproject")
SRC = ROOT / "crypto_MFT_arb_platform" / "src"
sys.path.insert(0, str(SRC))
OUT = Path(os.environ["S3_OUT"]) if os.environ.get("S3_OUT") else ROOT / "out" / "integration" / "s3" / "study"          # S3_OUT: another folder (the 217-event reconciliation re-extraction)
TRAIN = ["2024-03-01", "2024-03-02", "2024-03-03", "2024-03-04", "2024-03-06"]       # "cut it short" (owner 10 Nov): 03-05 had not finished its extraction and 03-07 was dropped; full days
SELECT = ["2024-03-08", "2024-03-09", "2024-03-10", "2024-03-11"]                  # the 2-hour slices 00:00-02:00 UTC (out/queue_v2/slices_mar2024); 03-12 has no slice and was dropped
HOLDOUT = ["2024-03-13", "2024-03-14", "2024-03-15"]
LAG = "0.008"
TAUS = (-1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0)
RIDGE = 10.0


def extract(days, n, allow_holdout=False, data_root=None):
    (OUT / "days").mkdir(parents=True, exist_ok=True)
    pending, running = [d for d in days if not (OUT / "days" / f"{d}.json").exists()], {}
    env = {"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin"}
    while pending or running:
        for d, p in list(running.items()):
            if p.poll() is not None:
                print(time.strftime("%T"), "rc", p.returncode, d, flush=True)
                if p.returncode == 0:
                    (OUT / "days" / f"{d}.json.part").rename(OUT / "days" / f"{d}.json")
                del running[d]
        while pending and len(running) < n:
            d = pending.pop(0)
            assert allow_holdout or d <= "2024-03-12", "HOLDOUT is not read here"
            print(time.strftime("%T"), "start", d, flush=True)
            running[d] = subprocess.Popen(["nice", "-n", "5", sys.executable, "-m", "new_backtest.market_hedge_move", "--data-root", str(data_root or ROOT / "data"), "--pair", "SOLUSDT", "--days", d, "--out", "/dev/null",
                                           "--quote-filter-out", str(OUT / "days" / f"{d}.json.part")], env=env, cwd=ROOT / "crypto_MFT_arb_platform", stdout=open(OUT / "days" / f"{d}.log", "w"), stderr=subprocess.STDOUT)
        time.sleep(5)
    print("done")


def load(days):
    return {d: json.loads((OUT / "days" / f"{d}.json").read_text()) for d in days}


def usable(r, lag=LAG):
    return lag in r["features"] and r["cost_bps_slack5"] is not None and r["move_50ms"] is not None


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * q))]


def fit_model(per_day):
    from crypto_stat_arb.strategies.edge import QUOTE_FEATURE_NAMES, fit_do_not_quote_filter

    rows = {"bid": ([], []), "ask": ([], [])}
    for d in per_day.values():
        for r in d["records"]:
            if r["rule"] == "touch" and usable(r):
                rows[r["side"]][0].append(r["features"][LAG])
                rows[r["side"]][1].append(r["move_50ms"])
    return fit_do_not_quote_filter({s: v for s, v in rows.items() if v[0]}, horizon_s=0.05, ridge=RIDGE, names=QUOTE_FEATURE_NAMES,
                                   fitted_on={"days": TRAIN, "events": {s: len(v[1]) for s, v in rows.items()}, "source": "MARKET_DATA_LABELS", "lag_s": float(LAG), "ridge": RIDGE, "label": "EXPLORATORY: ridge of the 50 ms Binance mid move on quote-time features, TRAIN only; no simulated fill; HOLDOUT never read at fit"})


def skipped(model, r, tau):
    x = r["features"][LAG]
    return model.with_threshold(tau).skip(r["side"], x)


def score(per_day, model, tau, rule, gate=None):
    """Per day: events, skipped, realised net of kept and skipped, total net avoided (minus the net of the skipped events)."""
    rows = []
    for day, d in per_day.items():
        ev = [r for r in d["records"] if r["rule"] == rule and usable(r)]
        sk = [(skipped(model, r, tau) if gate is None else bool(r["toxic_gate_blocks"].get(LAG))) for r in ev]
        net = [-r["cost_bps_slack5"] for r in ev]
        rows.append({"day": day, "events": len(ev), "skipped": sum(sk), "net_all": sum(net), "net_skipped": sum(n for n, s in zip(net, sk) if s), "net_kept": sum(n for n, s in zip(net, sk) if not s)})
    return rows


def summarise(rows):
    n, k = sum(r["events"] for r in rows), sum(r["skipped"] for r in rows)
    gain = -sum(r["net_skipped"] for r in rows)
    kept = n - k
    return {"events": n, "skipped": k, "share_skipped": k / n if n else 0.0, "net_per_event_keep_all": sum(r["net_all"] for r in rows) / n if n else 0.0,
            "net_per_event_kept": sum(r["net_kept"] for r in rows) / kept if kept else None, "net_per_event_skipped": (-gain / k) if k else None,
            "total_net_avoided_bps": gain, "gain_per_event_bps": gain / n if n else 0.0}


def bootstrap(rows, b=10000, seed=1):
    rnd = random.Random(seed)
    vals = []
    for _ in range(b):
        pick = [rows[rnd.randrange(len(rows))] for _ in rows]
        n = sum(p["events"] for p in pick)
        vals.append(-sum(p["net_skipped"] for p in pick) / n if n else 0.0)
    vals.sort()
    return {"p5": vals[int(0.05 * b)], "p95": vals[int(0.95 * b)], "blocks": len(rows), "note": "COARSE: day blocks, few of them"}


def fit():
    train, select = load(TRAIN), load(SELECT)
    model = fit_model(train)
    res = {"label": "EXPLORATORY: S3 do-not-quote filter, March 2024 market data; ASSUMED 8 ms lag, ASSUMED 45 ms hedge delay, 15 bps unwind band; HOLDOUT not read", "rules": __doc__.split("RULES, WRITTEN BEFORE SELECT IS SCORED:")[1].split("usage:")[0].strip(),
           "counts": {name: {rule: sum(1 for d in days.values() for r in d["records"] if r["rule"] == rule and usable(r)) for rule in ("clear", "touch")} for name, days in (("TRAIN", train), ("SELECT", select))}}
    # prediction quality (information): the 50 ms move, table of the side mean (the v1_5 form) against the model, per split
    pred = {}
    for name, days in (("TRAIN", train), ("SELECT", select)):
        rec = [r for d in days.values() for r in d["records"] if r["rule"] == "touch" and usable(r)]
        base = {s: statistics.mean(r["move_50ms"] for r in train_rec(train, s)) for s in ("bid", "ask")}
        e_m = [(r["move_50ms"] - model.predict_move_bps(r["side"], r["features"][LAG])) ** 2 for r in rec]
        e_b = [(r["move_50ms"] - base[r["side"]]) ** 2 for r in rec]
        pred[name] = {"n": len(rec), "mse_side_mean": statistics.mean(e_b), "mse_model": statistics.mean(e_m), "gain": 1 - statistics.mean(e_m) / statistics.mean(e_b)}
    res["prediction_50ms"] = pred
    grid = {}
    for tau in TAUS:
        grid[f"{tau:g}"] = {rule: summarise(score(select, model, tau, rule)) for rule in ("clear", "touch")}
    best = max(TAUS, key=lambda t: grid[f"{t:g}"]["clear"]["total_net_avoided_bps"])
    res["tau_grid_select"] = grid
    res["chosen_tau"] = best
    rows_clear = score(select, model, best, "clear")
    both_positive = all(grid[f"{best:g}"][rule]["total_net_avoided_bps"] > 0 for rule in ("clear", "touch"))
    ci = bootstrap(rows_clear)
    res["select_at_chosen_tau"] = {"clear": {"summary": grid[f"{best:g}"]["clear"], "per_day": rows_clear, "bootstrap_gain_per_event_90pct": ci},
                                   "touch": {"summary": grid[f"{best:g}"]["touch"], "per_day": score(select, model, best, "touch"), "bootstrap_gain_per_event_90pct": bootstrap(score(select, model, best, "touch"))}}
    gate = {rule: {"summary": summarise(score(select, model, best, rule, gate=True)), "per_day": score(select, model, best, rule, gate=True)} for rule in ("clear", "touch")}
    res["toxic_gate_v1_6_on_the_same_events"] = gate
    res["keep_all"] = {rule: summarise([dict(r, skipped=0, net_skipped=0.0, net_kept=r["net_all"]) for r in score(select, model, best, rule)]) for rule in ("clear", "touch")}
    # the filter must beat the gate: the S3 gain per event against the gate's, same events
    beats_gate = grid[f"{best:g}"]["clear"]["gain_per_event_bps"] > gate["clear"]["summary"]["gain_per_event_bps"]
    # the filter at the 1 s lag (a quote that was not cancelled in time): information
    res["accept"] = {"gain_positive_under_both_queue_rules": both_positive, "bootstrap_excludes_zero": ci["p5"] > 0, "beats_toxic_gate": beats_gate, "ACCEPTED": bool(both_positive and ci["p5"] > 0 and beats_gate)}
    model_final = model.with_threshold(best)
    OUT.mkdir(parents=True, exist_ok=True)
    model_final.save(OUT / "dnq_v1.json")
    res["model_file"] = {"path": "out/integration/s3/study/dnq_v1.json", "sha256": hashlib.sha256((OUT / "dnq_v1.json").read_bytes()).hexdigest()}
    (OUT / "s3_select_report.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({k: res[k] for k in ("counts", "prediction_50ms", "chosen_tau", "accept")}, indent=1))
    for tau in TAUS:
        g = grid[f"{tau:g}"]
        print(f"tau {tau:+.1f}  clear skipped {g['clear']['share_skipped']*100:5.2f}% gain/event {g['clear']['gain_per_event_bps']:+.4f}   touch skipped {g['touch']['share_skipped']*100:5.2f}% gain/event {g['touch']['gain_per_event_bps']:+.4f}")
    print("toxic gate (v1_6) on the same events:", {r: (gate[r]["summary"]["share_skipped"], gate[r]["summary"]["gain_per_event_bps"]) for r in gate})
    print("bootstrap 90% (clear):", ci)


def errors(model, rows, base):
    e_m = [r["move_50ms"] - model.predict_move_bps(r["side"], r["features"][LAG]) for r in rows]
    e_b = [r["move_50ms"] - base[r["side"]] for r in rows]

    def sc(errs):
        sq = sorted(e * e for e in errs)
        cut = int(len(sq) * 0.999)
        ab = sorted(abs(e) for e in errs)
        return {"n": len(errs), "mse": sum(sq) / len(sq), "trimmed_mse_0.1pct": sum(sq[:cut]) / cut, "median_abs_error": ab[len(ab) // 2]}

    m, b = sc(e_m), sc(e_b)
    return {"model": m, "side_mean_baseline": b, "gain": {k: 1 - m[k] / b[k] for k in ("mse", "trimmed_mse_0.1pct", "median_abs_error")}}


def holdout(n):
    """Rule 5: HOLDOUT is extracted and scored ONCE, after the model and tau are frozen; a lock file refuses a second run."""
    lock = OUT / "s3_holdout.lock"
    if lock.exists():
        raise SystemExit(f"{lock} exists: HOLDOUT was already scored for S3 and is not used again")
    sel = json.loads((OUT / "s3_select_report.json").read_text())
    assert hashlib.sha256((OUT / "dnq_v1.json").read_bytes()).hexdigest() == sel["model_file"]["sha256"], "the frozen model changed"
    from crypto_stat_arb.strategies.edge import DoNotQuoteFilter

    model = DoNotQuoteFilter.load(OUT / "dnq_v1.json")
    tau = model.threshold_bps
    extract(HOLDOUT, n, allow_holdout=True)
    hold, train = load(HOLDOUT), load(TRAIN)
    base = {s: statistics.mean(r["move_50ms"] for r in train_rec(train, s)) for s in ("bid", "ask")}
    res = {"label": "EXPLORATORY: S3 do-not-quote filter scored ONCE on HOLDOUT 2024-03-13..15 (used once before, for the S1 prediction check); model and tau frozen before extraction; ASSUMED 8 ms lag", "model_sha256": sel["model_file"]["sha256"], "tau": tau,
           "prediction_50ms": {"per_day": {}, "all": None}, "net": {}}
    for d, x in hold.items():
        rows = [r for r in x["records"] if r["rule"] == "touch" and usable(r)]
        res["prediction_50ms"]["per_day"][d] = errors(model, rows, base)
    allrows = [r for x in hold.values() for r in x["records"] if r["rule"] == "touch" and usable(r)]
    res["prediction_50ms"]["all"] = errors(model, allrows, base)
    for rule in ("clear", "touch"):
        rows = score(hold, model, tau, rule)
        gate = score(hold, model, tau, rule, gate=True)
        res["net"][rule] = {"filter": {"summary": summarise(rows), "per_day": rows, "bootstrap_gain_per_event_90pct": bootstrap(rows)}, "toxic_gate_v1_6": {"summary": summarise(gate), "per_day": gate},
                            "keep_all": summarise([dict(r, skipped=0, net_skipped=0.0, net_kept=r["net_all"]) for r in rows])}
    (OUT / "s3_holdout_report.json").write_text(json.dumps(res, indent=1, default=str))
    lock.write_text("HOLDOUT scored once for S3 (owner 10 Nov); do not run again\n")
    print(json.dumps({"prediction_all": res["prediction_50ms"]["all"], "net_clear": res["net"]["clear"]["filter"]["summary"], "net_touch": res["net"]["touch"]["filter"]["summary"]}, indent=1))


def gate_holdout():
    lock = OUT / "s3_gate_holdout.lock"
    if lock.exists():
        raise SystemExit(f"{lock} exists: the block_on_toxic gate was already scored on HOLDOUT and is not scored again")
    hold = load([d for d in HOLDOUT if (OUT / "days" / f"{d}.json").exists()])          # the days that finished (the owner asked to cut it short): the report names them
    res = {"label": "EXPLORATORY: block_on_toxic gate flag scored ONCE on HOLDOUT 2024-03-13..15 (full days); no model; net = minus the slack-5 hedge cost of the S2 chain; ASSUMED 8 ms lag", "rule": "5th percentile of the day-block bootstrap of D (flagged minus unflagged mean net) not below -1.0 bps => the gate is not protecting anything => v1_7 with block_on_toxic off",
           "rules": {}}
    rnd = random.Random(1)
    for rule in ("clear", "touch"):
        per = {}
        for day, d in hold.items():
            ev = [r for r in d["records"] if r["rule"] == rule and usable(r)]
            fl = [-r["cost_bps_slack5"] for r in ev if r["toxic_gate_blocks"].get(LAG)]
            un = [-r["cost_bps_slack5"] for r in ev if not r["toxic_gate_blocks"].get(LAG)]
            per[day] = {"events": len(ev), "flagged": len(fl), "unflagged": len(un), "net_flagged_sum": sum(fl), "net_unflagged_sum": sum(un)}
        def diff(blocks):
            nf, nu = sum(b["flagged"] for b in blocks), sum(b["unflagged"] for b in blocks)
            if not nf or not nu:
                return None
            return sum(b["net_flagged_sum"] for b in blocks) / nf - sum(b["net_unflagged_sum"] for b in blocks) / nu
        vals = []
        for _ in range(10000):
            v = diff([per[d] for d in rnd.choices(list(per), k=len(per))])
            if v is not None:
                vals.append(v)
        vals.sort()
        pooled = diff(list(per.values()))
        res["rules"][rule] = {"per_day": per, "events": sum(b["events"] for b in per.values()), "flagged": sum(b["flagged"] for b in per.values()), "share_flagged": sum(b["flagged"] for b in per.values()) / max(1, sum(b["events"] for b in per.values())),
                              "mean_net_flagged": sum(b["net_flagged_sum"] for b in per.values()) / max(1, sum(b["flagged"] for b in per.values())), "mean_net_unflagged": sum(b["net_unflagged_sum"] for b in per.values()) / max(1, sum(b["unflagged"] for b in per.values())),
                              "D_pooled": pooled, "D_bootstrap_90pct": {"p5": vals[int(0.05 * len(vals))], "p50": vals[len(vals) // 2], "p95": vals[int(0.95 * len(vals))], "valid_resamples": len(vals), "note": "COARSE: the day blocks that finished"} if vals else None}
    p5 = (res["rules"]["clear"]["D_bootstrap_90pct"] or {}).get("p5")
    res["decision"] = {"p5_clear": p5, "gate_off_v1_7": bool(p5 is not None and p5 >= -1.0)}
    (OUT / "s3_gate_holdout_report.json").write_text(json.dumps(res, indent=1, default=str))
    lock.write_text("block_on_toxic gate scored once on HOLDOUT (owner 10 Nov); do not run again\n")
    print(json.dumps({"rules": {r: {k: v for k, v in x.items() if k != "per_day"} for r, x in res["rules"].items()}, "decision": res["decision"]}, indent=1, default=str))


def train_rec(per_day, side):
    return [r for d in per_day.values() for r in d["records"] if r["rule"] == "touch" and usable(r) and r["side"] == side]


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "gate-holdout":
        gate_holdout()
    elif cmd == "extract-holdout":          # owner 10 Nov: HOLDOUT 03-13..15 full days, for the block_on_toxic gate scoring (rule in `gate_holdout`) and kept for the later S3 rerun
        extract(HOLDOUT, int(sys.argv[2]) if len(sys.argv) > 2 else 3, allow_holdout=True)
    elif cmd == "extract-slices":          # the 2-hour March slices (00:00-02:00 UTC, out/queue_v2/slices_mar2024): the owner's "cut it short" of 10 Nov for the SELECT days
        extract(sys.argv[3].split(","), int(sys.argv[2]), data_root=ROOT / "out" / "queue_v2" / "slices_mar2024")
    elif cmd == "extract":
        extract(sys.argv[3].split(",") if len(sys.argv) > 3 else TRAIN + SELECT, int(sys.argv[2]) if len(sys.argv) > 2 else 2)
    elif cmd == "fit":
        fit()
    elif cmd == "holdout":
        holdout(int(sys.argv[2]) if len(sys.argv) > 2 else 3)
