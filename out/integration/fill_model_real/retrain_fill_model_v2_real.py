"""Gap E (owner 8 Nov): retrain FillModelV2 on REAL orders only. A run wrapper: the reusable parts are in src/new_backtest (FillModelV2.fit with its IPCW censoring, fill_label.compute_ipcw_weights,
fill_model_v1.calibration_report); this file only reads the account's order history and the collector books and wires them together.

Data (read only):
  orders      ~/Downloads/bybit_30d.zip: order_spot_<COIN>.jsonl (create, update, status, cum. executed quantity) and exec_spot_<COIN>.jsonl (first execution time). Account data stays local: the output holds
              aggregates only, no order ids, no raw rows.
  books       data/bybit-spot/book_snapshots (the collector's 1 s snapshots, 50 levels) for 2026-04-19..25
Set     every PostOnly Limit spot order created 2026-04-19..25 UTC that ended Filled or Cancelled (Rejected ones never rested), with a fresh collector snapshot at creation (age <= 2 s) and the 60 s look-back
        for volatility. The study of 2026-10-30 selected the 2,396 orders with an execution that also pass its replay rules; cancels without a fill are needed here (they are the censored rows), so this set is broader.
Label   per horizon h in (1, 5, 15) s: filled = first execution within h of creation; an order cancelled without an execution before h is CENSORED (is_censored, weight 0 in the fit, used for the
        censoring survival G(t) in IPCW), never "did not fill"; otherwise it survived h unfilled.
Features (4): log1p(queue ahead at its price, base units), distance from the touch in ticks (behind = positive, inside the spread = negative), spread / mid (a fraction), short volatility = standard
        deviation of the 5 s log-mid returns over the 60 s before creation (a fraction). The other five FillModelV1 inputs are held at fixed constants (coefficient 0): this is NOT the simulator's feature set.
Split   chronological by creation day: train 04-19..04-22, holdout 04-23..04-25.
Models  FillModelV2 (gate horizon 5 s) vs two baselines on the holdout: the train base rate, and a queue-only logistic. Brier score with 95% bootstrap intervals (rows), reliability table, and a proxy for
        informative censoring (cancel rate by predicted-probability bin).
Output  when the set holds censored rows: fill_model_v2_MEASURED_SPOT_2026_v1.json (loadable by FillModelV2.load_json) and .meta.json. NOT used as a quote filter until the owner approves.
        When it does NOT (owner 9 Nov): status E_STATUS.json = BLOCKED_NO_CENSORED_ROWS and time_to_fill_diagnostic.json (aggregates of time to first fill on executed orders, a DIAGNOSTIC, not a model); no model is written or published.
usage: retrain_fill_model_v2_real.py OUT_DIR [N_PROCESSES]"""
import bisect
import collections
import datetime as dt
import glob
import gzip
import json
import math
import multiprocessing as mp
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from new_backtest.fill_label import FillLabelRecord, compute_ipcw_weights  # noqa: E402
from new_backtest.fill_model_v1 import FillModelV1, FillModelV2, calibration_report  # noqa: E402

ZIP = Path.home() / "Downloads" / "bybit_30d.zip"
ROOT = "/Users/vaibhav/Documents/Newproject/data/bybit-spot"
DAYS = [f"2026-04-{d}" for d in range(19, 26)]
TRAIN_DAYS, HOLD_DAYS = DAYS[:4], DAYS[4:]
HORIZONS = (1, 5, 15)
GATE = 5
FRESH_S = 2.0                      # the snapshot used must be at most this old at creation
VOL_STEPS, VOL_STEP_S = 12, 5.0    # 60 s of look-back in 5 s steps
SOURCE_TAG = "MEASURED_SPOT_2026"
TS = re.compile(rb'"timestamp": ?([0-9.]+)')
CONST = dict(local_depth=1.0, local_depth_baseline=1.0, trade_count=0.0, trade_sign=0.0, regime_bucket="calm")
day_of = lambda s: dt.datetime.fromtimestamp(s, dt.timezone.utc).strftime("%Y-%m-%d")


def load_orders():
    """-> list of dicts (no ids): coin, side, price, qty, created_s, t_fill (s after creation or None), t_end (s after creation, for a cancel without execution) and the terminal status."""
    out, funnel = [], collections.Counter()
    z = zipfile.ZipFile(ZIP)
    names = set(z.namelist())
    for name in sorted(n for n in names if "/order_spot_" in n):
        coin = name.split("order_spot_")[1].split(".")[0]
        first_exec = {}
        en = name.replace("order_spot_", "exec_spot_")
        if en in names:
            with z.open(en) as f:
                for line in f:
                    r = json.loads(line)
                    if float(r.get("execQty") or 0) > 0:
                        t = int(r["execTime"]) / 1000.0
                        k = r["orderId"]
                        if k not in first_exec or t < first_exec[k]:
                            first_exec[k] = t
        with z.open(name) as f:
            for line in f:
                r = json.loads(line)
                funnel["orders_seen"] += 1
                created = int(r["createdTime"]) / 1000.0
                if not (DAYS[0] <= day_of(created) <= DAYS[-1]):
                    continue
                funnel["in_window"] += 1
                if r["orderType"] != "Limit" or r["timeInForce"] != "PostOnly":
                    funnel["not_postonly_limit"] += 1
                    continue
                funnel[f"status_{r['orderStatus']}"] += 1
                if r["orderStatus"] not in ("Filled", "Cancelled", "PartiallyFilledCanceled"):
                    funnel["rejected_or_other"] += 1
                    continue
                tf = first_exec.get(r["orderId"])
                if tf is None and float(r.get("cumExecQty") or 0) > 0:
                    funnel["executed_but_no_exec_row"] += 1
                    continue
                if tf is not None and tf < created:
                    funnel["execution_before_creation"] += 1
                    continue
                out.append({"coin": coin, "side": r["side"], "price": float(r["price"]), "qty": float(r["qty"]), "created": created,
                            "t_fill": None if tf is None else tf - created, "t_end": (int(r["updatedTime"]) / 1000.0 - created),
                            "status": r["orderStatus"]})
                funnel["kept_before_book_rules"] += 1
    return out, dict(funnel)


def book_features(args):
    """One (coin, day): stream the day's snapshots once, keep the latest snapshot at or before each wanted time, and compute the features of every order created that day."""
    coin, day, orders = args
    wanted = set()
    for o in orders:
        wanted.add(round(o["created"], 3))
        for k in range(1, VOL_STEPS + 1):
            wanted.add(round(o["created"] - k * VOL_STEP_S, 3))
    W = sorted(wanted)
    snap = {}                                   # wanted time -> (timestamp, parsed record or mid only)
    need_full = {round(o["created"], 3) for o in orders}
    idx, prev_line, prev_ts = 0, None, None
    for path in sorted(glob.glob(f"{ROOT}/book_snapshots/pair={coin}/date={day}/*.jsonl.gz")):
        with gzip.open(path, "rb") as fh:
            try:
                for line in fh:
                    m = TS.search(line)
                    if not m:
                        continue
                    ts = float(m.group(1))
                    while idx < len(W) and ts > W[idx]:
                        if prev_line is not None:
                            r = json.loads(prev_line)
                            snap[W[idx]] = (prev_ts, r if W[idx] in need_full else (r["best_bid"] + r["best_ask"]) / 2.0)
                        idx += 1
                    prev_line, prev_ts = line, ts
            except EOFError:
                pass
    while idx < len(W) and prev_line is not None:
        r = json.loads(prev_line)
        snap[W[idx]] = (prev_ts, r if W[idx] in need_full else (r["best_bid"] + r["best_ask"]) / 2.0)
        idx += 1
    out, dropped = [], collections.Counter()
    for o in orders:
        key = round(o["created"], 3)
        got = snap.get(key)
        if got is None or not isinstance(got[1], dict):
            dropped["no_snapshot"] += 1
            continue
        ts, r = got
        if o["created"] - ts > FRESH_S:
            dropped["snapshot_older_than_2s"] += 1
            continue
        mids = []
        for k in range(VOL_STEPS, -1, -1):
            g = snap.get(round(o["created"] - k * VOL_STEP_S, 3)) if k else (ts, (r["best_bid"] + r["best_ask"]) / 2.0)
            mids.append(None if g is None else (g[1] if not isinstance(g[1], dict) else (g[1]["best_bid"] + g[1]["best_ask"]) / 2.0))
        if any(v is None or v <= 0 for v in mids):
            dropped["no_volatility_lookback"] += 1
            continue
        rets = [math.log(b / a) for a, b in zip(mids, mids[1:])]
        mean = sum(rets) / len(rets)
        short_vol = math.sqrt(sum((x - mean) ** 2 for x in rets) / (len(rets) - 1))
        bids, asks = r["bids"], r["asks"]
        bb, ba = r["best_bid"], r["best_ask"]
        diffs = [b[0] - a[0] for a, b in zip(bids[::-1], bids[::-1][1:])] + [b[0] - a[0] for a, b in zip(asks, asks[1:])]
        diffs = [d for d in diffs if d > 1e-12]
        if not diffs or bb <= 0 or ba <= bb:
            dropped["bad_book"] += 1
            continue
        tick = min(diffs)
        price = o["price"]
        buy = o["side"] == "Buy"
        side_levels, touch = (bids, bb) if buy else (asks, ba)
        beyond = (price < side_levels[-1][0] - 1e-12) if buy else (price > side_levels[-1][0] + 1e-12)
        if beyond:
            dropped["beyond_depth"] += 1
            continue
        level = [s for p, s in side_levels if abs(p - price) <= tick * 0.01]
        queue = float(level[0]) if level else 0.0
        rel = round(((touch - price) if buy else (price - touch)) / tick)
        if (buy and price >= ba) or ((not buy) and price <= bb):
            dropped["crossing_postonly_price"] += 1
            continue
        out.append({**{k: o[k] for k in ("coin", "created", "t_fill", "t_end", "status")}, "queue_ahead": queue, "rel_ticks": int(rel), "spread": (ba - bb) / ((ba + bb) / 2.0), "short_vol": short_vol,
                    "qty": o["qty"]})
    return out, dict(dropped)


def records_for(rows, feature_set):
    """FillLabelRecords, one per (row, horizon). feature_set: 'full' (four features) or 'queue_only' (the other three held constant)."""
    recs = []
    for o in rows:
        for h in HORIZONS:
            filled = o["t_fill"] is not None and o["t_fill"] <= h
            censored = (not filled) and o["t_fill"] is None and o["t_end"] < h
            obs = o["t_fill"] if filled else (o["t_end"] if censored else float(h))
            full = feature_set == "full"
            recs.append(FillLabelRecord(
                timestamp=o["created"], symbol=o["coin"], side="bid", horizon_s=h, filled=bool(filled),
                relative_to_touch_ticks=o["rel_ticks"] if full else 0, queue_ahead=o["queue_ahead"], spread_pct=o["spread"] if full else 0.001,
                short_vol=o["short_vol"] if full else 0.0, terminal_reason="full_fill" if filled else "cancel", observation_time_s=float(max(obs, 0.0)),
                is_censored=bool(censored), **CONST))
    return recs


def predict_all(model, recs):
    return [model.predict(queue_ahead=r.queue_ahead, local_depth=r.local_depth, local_depth_baseline=r.local_depth_baseline, spread_pct=r.spread_pct, short_vol=r.short_vol,
                          trade_count=r.trade_count, trade_sign=r.trade_sign, regime_bucket=r.regime_bucket, relative_to_touch_ticks=r.relative_to_touch_ticks) for r in recs]


def archive_cancel_inventory():
    """Counts only: every order of the archive that ended without a full fill, by file kind, status and creation day, and the days the collector holds spot books for."""
    z = zipfile.ZipFile(ZIP)
    inv = collections.defaultdict(collections.Counter)
    for n in z.namelist():
        b = n.split("/")[-1]
        if b.startswith("order_"):
            kind = b.split("_")[1]
            with z.open(n) as f:
                for line in f:
                    r = json.loads(line)
                    if r["orderStatus"] in ("Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"):
                        inv[f"{kind}|{r['orderStatus']}"][day_of(int(r["createdTime"]) / 1000.0)] += 1
    books = sorted({p.split("date=")[1] for p in glob.glob(f"{ROOT}/book_snapshots/pair=*/date=2026-*")})
    return {k: dict(sorted(v.items())) for k, v in sorted(inv.items())}, books


def write_blocked(out_dir, rows, funnel, sets):
    inv, books = archive_cancel_inventory()
    cancel_days = sorted({d for k, v in inv.items() if k.endswith("|Cancelled") for d in v})
    status = {"status": "BLOCKED_NO_CENSORED_ROWS", "reason": "no cancelled or unfilled order has book features: every order of the collector week 2026-04-19..25 was executed; the Cancelled orders of the archive are on "
              f"{cancel_days} and the spot collector holds no books for those days; the only linear cancel is on 2026-05-02 and the perp collector holds no 2026 books",
              "archive_unfilled_inventory_counts_by_kind_status_day": inv, "spot_collector_days_2026_first_and_count": [books[0], len(books)] if books else None,
              "rows_with_book_features": len(rows), "funnel": funnel, "sets": sets,
              "not_published": "no fill-probability model and no quote filter is written; a model fitted on executed orders only answers P(first execution within h | executed), not P(fill)"}
    (out_dir / "E_STATUS.json").write_text(json.dumps(status, indent=1, default=str))
    # time to first execution of the executed orders, aggregates only (a diagnostic): overall, by queue-ahead tercile and by distance from the touch
    def q(v):
        v = sorted(v)
        return {"n": len(v), **{f"p{int(k * 100)}": v[min(len(v) - 1, int(len(v) * k))] for k in (.1, .25, .5, .75, .9)}} if len(v) >= 30 else {"n": len(v)}
    tt = [o for o in rows if o["t_fill"] is not None]
    qs = sorted(o["queue_ahead"] for o in tt)
    cuts = (qs[len(qs) // 3], qs[2 * len(qs) // 3])
    diag = {"label": "DIAGNOSTIC ONLY: time to first execution on EXECUTED orders (selection: orders that never executed are absent); not a fill probability",
            "days": DAYS, "overall_s": q([o["t_fill"] for o in tt]),
            "by_queue_ahead_tercile_s": {"low": q([o["t_fill"] for o in tt if o["queue_ahead"] <= cuts[0]]), "mid": q([o["t_fill"] for o in tt if cuts[0] < o["queue_ahead"] <= cuts[1]]),
                                         "high": q([o["t_fill"] for o in tt if o["queue_ahead"] > cuts[1]]), "tercile_cuts_base_units": list(cuts)},
            "by_distance_from_touch_ticks_s": {"inside_spread(<0)": q([o["t_fill"] for o in tt if o["rel_ticks"] < 0]), "at_touch(0)": q([o["t_fill"] for o in tt if o["rel_ticks"] == 0]),
                                               "behind_1": q([o["t_fill"] for o in tt if o["rel_ticks"] == 1]), "behind_2plus": q([o["t_fill"] for o in tt if o["rel_ticks"] >= 2])}}
    (out_dir / "time_to_fill_diagnostic.json").write_text(json.dumps(diag, indent=1, default=str))
    print(json.dumps({"status": status["status"], "reason": status["reason"], "diagnostic": diag}, indent=1, default=str))


def main():
    out_dir = Path(sys.argv[1])
    procs = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    out_dir.mkdir(parents=True, exist_ok=True)
    orders, funnel = load_orders()
    jobs = collections.defaultdict(list)
    for o in orders:
        jobs[(o["coin"], day_of(o["created"]))].append(o)
    with mp.Pool(procs) as pool:
        results = pool.map(book_features, [(c, d, v) for (c, d), v in sorted(jobs.items())], chunksize=1)
    rows, dropped = [], collections.Counter()
    for r, d in results:
        rows += r
        dropped.update(d)
    funnel["dropped_by_book_rules"] = dict(dropped)
    funnel["final_orders"] = len(rows)
    rows.sort(key=lambda o: o["created"])
    train = [o for o in rows if day_of(o["created"]) in TRAIN_DAYS]
    hold = [o for o in rows if day_of(o["created"]) in HOLD_DAYS]
    trained_until = dt.datetime.strptime(HOLD_DAYS[0], "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp()
    sets = {"n_train_orders": len(train), "n_holdout_orders": len(hold), "cancelled_without_execution_train": sum(1 for o in train if o["t_fill"] is None),
            "cancelled_without_execution_holdout": sum(1 for o in hold if o["t_fill"] is None), "coins": len({o["coin"] for o in rows})}
    if sets["cancelled_without_execution_train"] == 0:
        write_blocked(out_dir, rows, funnel, sets)
        return
    rec_tr, rec_ho = records_for(train, "full"), records_for(hold, "full")
    model = FillModelV2.fit(rec_tr, gate_horizon_s=GATE, trained_until_ts=trained_until, horizons=HORIZONS)
    qmodel = FillModelV2.fit(records_for(train, "queue_only"), gate_horizon_s=GATE, trained_until_ts=trained_until, horizons=HORIZONS)
    if model is None or qmodel is None:
        raise SystemExit("a model could not be fitted (one class only?)")
    qrec_ho = records_for(hold, "queue_only")
    metrics = {}
    for h in HORIZONS:
        rh = [r for r in rec_ho if r.horizon_s == h]
        qh = [r for r in qrec_ho if r.horizon_s == h]
        w = compute_ipcw_weights(rh, h)
        keep = [i for i, x in enumerate(w) if x > 0]
        y = [int(rh[i].filled) for i in keep]
        wk = [w[i] for i in keep]
        p = predict_all(model.per_horizon[str(h)], [rh[i] for i in keep])
        pq = predict_all(qmodel.per_horizon[str(h)], [qh[i] for i in keep])
        tr_h = [r for r in rec_tr if r.horizon_s == h]
        wt = compute_ipcw_weights(tr_h, h)
        base = sum(w_ * int(r.filled) for r, w_ in zip(tr_h, wt) if w_ > 0) / sum(w_ for w_ in wt if w_ > 0)
        base_p = [base] * len(y)
        censored = [r for r in rh if r.is_censored]
        cens_by_bin = []
        order = sorted(range(len(rh)), key=lambda i: model.per_horizon[str(h)].predict(queue_ahead=rh[i].queue_ahead, local_depth=1.0, local_depth_baseline=1.0, spread_pct=rh[i].spread_pct,
                                                                                         short_vol=rh[i].short_vol, trade_count=0.0, trade_sign=0.0, regime_bucket="calm",
                                                                                         relative_to_touch_ticks=rh[i].relative_to_touch_ticks))
        for chunk in (order[i::1] for i in range(1)):
            n = len(chunk)
            for b in range(4):
                part = chunk[b * n // 4:(b + 1) * n // 4]
                cens_by_bin.append({"bin": b + 1, "n": len(part), "cancel_before_horizon_rate": sum(1 for i in part if rh[i].is_censored) / max(len(part), 1)})
        metrics[f"{h}s"] = {
            "holdout_rows": len(rh), "censored_rows": len(censored), "rows_scored": len(y), "train_base_rate_ipcw": base,
            "model_vs_train_base_rate": calibration_report(y, p, wk, baseline_p=base_p),
            "model_vs_queue_only_logistic": calibration_report(y, p, wk, baseline_p=pq),
            "censoring_proxy_cancel_rate_by_predicted_quartile": cens_by_bin,
            "model_coefficients_standardised": dict(zip(["log1p_queue_ahead", "(const)", "spread", "short_vol", "(const)", "(const)", "rel_ticks", "(const)", "(const)"], model.per_horizon[str(h)].coef)),
        }
    stem = out_dir / f"fill_model_v2_{SOURCE_TAG}_conditional_on_fill_v1"
    model.save_json(str(stem) + ".json")
    never_filled = funnel.get("status_Cancelled", 0)
    meta = {"source_tag": SOURCE_TAG, "version": 1, "approved_for_quote_filter": False, "valid_as_fill_probability": never_filled > 0 and sets["cancelled_without_execution_train"] > 0,
            "target": "P(first execution within h | the order was executed at some point) -- a time-to-fill model conditional on an eventual fill, NOT P(fill)" if sets["cancelled_without_execution_train"] == 0 else "P(first execution within h), cancels censored",
            "finding": f"in the collector window {DAYS[0]}..{DAYS[-1]} the account's order history holds {never_filled} Cancelled orders and every candidate order was executed (14 PartiallyFilledCanceled count as executed); the cancelled orders of the archive are on 2026-05-01 and 05-02, where there are no collector books",
            "note": "NOT a quote filter until the owner approves; features are defined as in this file's docstring, not as the simulator defines them",
            "gate_horizon_s": GATE, "horizons_s": list(HORIZONS), "train_days": TRAIN_DAYS, "holdout_days": HOLD_DAYS, "trained_until_ts": trained_until, "fresh_snapshot_max_age_s": FRESH_S,
            "features": ["log1p(queue ahead, base units)", "distance from touch in ticks (behind > 0)", "spread / mid (fraction)", "std of 5 s log-mid returns over the prior 60 s (fraction)"],
            "constant_inputs": CONST, "funnel": funnel, "sets": sets, "metrics": metrics,
            "caveats": ["snapshots are 1 s apart: the book at creation is up to 2 s old (no replay of deltas)", "cancels are informative: see the censoring proxy; IPCW is consistent only under non-informative censoring",
                        "one account's orders (a market maker's quoting behaviour), Bybit spot, one week", "price_moved_through is not available: the adverse model is not fitted"]}
    Path(str(stem) + ".meta.json").write_text(json.dumps(meta, indent=1, default=str))
    print(json.dumps({"funnel": funnel, "sets": sets, "brier": {k: {"model": v["model_vs_train_base_rate"]["brier"], "base_rate": v["model_vs_train_base_rate"]["baseline_brier"],
                                                                    "queue_only": v["model_vs_queue_only_logistic"]["baseline_brier"]} for k, v in metrics.items()}}, indent=1, default=str))


if __name__ == "__main__":
    main()
