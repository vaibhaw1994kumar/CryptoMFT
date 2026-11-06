"""Step 8 re-check (owner 7 Nov), report only: reads the 14 probe runs of run_probes_through.sh (7 TRAIN days x through rule off / on, v1_2 settings, 00:00-02:00 UTC) and the Bybit tape of the slices.
Writes probe_through/analysis.json. Nothing here feeds a replay. EXPLORATORY: March 2024, Bybit-only, 1 SOL bid at the strategy's quote depth every 15 s, fees not involved (markout is a price move)."""
import json, statistics as st, sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path("/Users/vaibhav/Documents/Newproject")
sys.path.insert(0, str(ROOT / "crypto_MFT_arb_platform" / "src"))
from new_backtest.event_loader import TRADE, iter_venue_events, trade_fields  # noqa: E402

P = ROOT / "out" / "integration" / "b0" / "probe_through"
DAYS = [f"2024-03-0{i}" for i in range(1, 8)]
HOR = ("move_bps", "move_bps_5s", "move_bps_30s")           # move_bps = 1 s (the probe's original field)
SLOT = 15.0
TOL = 1e-9


def load(cell, day):
    d = json.loads((P / cell / f"{day}.json").read_text())
    obs = [json.loads(l) for l in (P / cell / f"run_{day}" / "maker_observations.jsonl").read_text().splitlines()]
    return d, obs


def grp(cause):
    if cause is None:
        return "unknown"
    if cause.startswith("trade_confirmed_cross"):
        return "cross_confirmed"
    if cause in ("trade", "trade_after_front"):
        return "trade_at_our_price"
    if cause == "trade_through":
        return "trade_through"
    return cause


def mstats(vals):
    v = [x for x in vals if x is not None]
    return {"n": len(v), "mean": (sum(v) / len(v)) if v else None, "median": st.median(v) if v else None,
            "se": (st.pstdev(v) / len(v) ** 0.5) if len(v) > 1 else None}


def tape_prints(day):
    out = []
    for ev in iter_venue_events(ROOT / "out" / "queue_v2" / "slices_mar2024", "bybit", "SOLUSDT", day, day, include_snapshots=False, include_deltas=False, include_trades=True, trade_dedupe="multiset"):
        if ev.kind == TRADE:
            price, vol, sign = trade_fields(ev.payload)
            if price > 0 and vol > 0 and sign < 0:
                out.append((ev.exchange_ts, price, vol))
    out.sort()
    return out


def main():
    res = {"label": "Step 8 re-check: through-print rule off vs on (fill_through_prints + through_print_volume_limited), v1_2 settings, probe, 7 TRAIN days 03-01..03-07 00:00-02:00 UTC; EXPLORATORY",
           "cells": {}, "per_day": {}}
    fills = {"off": [], "on": []}
    orders = {"off": [], "on": []}
    for cell in ("off", "on"):
        for day in DAYS:
            d, obs = load(cell, day)
            res["per_day"].setdefault(day, {})[cell] = {"orders": d["orders_scheduled"], "fills": d["fills"], "causes": d["maker_fill_causes"]}
            fo = {(o["signal_id"], round(o["ts"], 6)): o for o in obs if o["obs"] == "maker_fill"}
            for r in d["rows"]:
                o = fo.get((r["signal_id"], round(r["ts"], 6)))
                fills[cell].append(dict(r, day=day, trig=(o or {}).get("trigger", {}), arrival=(o or {}).get("fill", {}).get("arrival_ts")))
            for o in obs:
                if o["obs"] == "maker_place":
                    orders[cell].append(dict(o, day=day))
    # 1. fills by cause, markouts
    for cell in ("off", "on"):
        by = defaultdict(list)
        for f in fills[cell]:
            by[grp(f["cause"])].append(f)
        cause_ct = Counter(f["cause"] for f in fills[cell])
        res["cells"][cell] = {
            "orders_placed": len(orders[cell]), "fills": len(fills[cell]), "fill_qty": sum(f["qty"] for f in fills[cell]), "by_cause_detail": dict(cause_ct),
            "by_group": {g: {"fills": len(v), "qty": sum(x["qty"] for x in v), "move_bps_1s": mstats([x["move_bps"] for x in v]), "move_bps_5s": mstats([x["move_bps_5s"] for x in v]),
                             "move_bps_30s": mstats([x["move_bps_30s"] for x in v])} for g, v in sorted(by.items())},
            "all": {"move_bps_1s": mstats([x["move_bps"] for x in fills[cell]]), "move_bps_5s": mstats([x["move_bps_5s"] for x in fills[cell]]), "move_bps_30s": mstats([x["move_bps_30s"] for x in fills[cell]])}}
    # 1b. through fills vs the SAME order in the off run
    offf = defaultdict(list)
    for f in fills["off"]:
        offf[(f["day"], f["signal_id"])].append(f)
    cov = Counter(); covq = Counter(); new_marks = defaultdict(list); cov_marks = defaultdict(list)
    for f in fills["on"]:
        if f["cause"] != "trade_through":
            continue
        k = (f["day"], f["signal_id"])
        o = offf.get(k)
        if not o:
            c = "not_filled_in_off_run"
        elif any(grp(x["cause"]) == "cross_confirmed" for x in o):
            c = "covered_by_opposite_cross_confirm_in_off_run"
        else:
            c = "filled_in_off_run_by_" + "+".join(sorted({grp(x["cause"]) for x in o}))
        cov[c] += 1; covq[c] += f["qty"]
        for h in HOR:
            (new_marks if c == "not_filled_in_off_run" else cov_marks)[h].append(f[h])
    res["through_fills_vs_off_run"] = {"counts": dict(cov), "qty": dict(covq), "through_fills_total": sum(cov.values()),
                                       "markout_of_new_fills_only": {h: mstats(v) for h, v in new_marks.items()}, "markout_of_already_filled_in_off_run": {h: mstats(v) for h, v in cov_marks.items()}}
    # off-run fills whose order the on run filled earlier by the through rule (the order set that changed)
    onf = defaultdict(list)
    for f in fills["on"]:
        onf[(f["day"], f["signal_id"])].append(f)
    res["orders_with_changed_outcome"] = {"filled_in_on_not_off": sum(1 for k in onf if k not in offf), "filled_in_off_not_on": sum(1 for k in offf if k not in onf),
                                          "fills_total_on_minus_off": len(fills["on"]) - len(fills["off"])}
    # 2. prints exactly at our price vs strictly through it, during each order's life (from the off run: unaffected by the rule)
    atpx = Counter(); thr = Counter(); atv = thr_v = 0.0; orders_with_at = orders_with_thr = 0; n_orders = 0
    ends = {(f["day"], f["signal_id"]): max(x["ts"] for x in offf[(f["day"], f["signal_id"])]) for f in fills["off"]}
    full = defaultdict(float); seen = {}
    for f in fills["off"]:
        full[(f["day"], f["signal_id"])] += f["qty"]
    sizes = {(o["day"], o["signal_id"]): o["size"] for o in orders["off"]}
    for day in DAYS:
        tape = tape_prints(day)
        lat = [f["arrival"] - f["ts"] for f in fills["off"] if f["day"] == day and f["arrival"] and f["ts"] - f["arrival"] < SLOT]
        for o in [o for o in orders["off"] if o["day"] == day]:
            a0 = o["ts"] + (st.median([x["arrival"] - o["ts"] for x in fills["off"] if x["day"] == day and x["signal_id"] == o["signal_id"] and x["arrival"]] or [0.0]) if any(x["signal_id"] == o["signal_id"] and x["day"] == day and x["arrival"] for x in fills["off"]) else 0.0)
            k = (day, o["signal_id"])
            t_end = o["ts"] + SLOT
            if k in ends and full[k] >= sizes[k] - 1e-9:
                t_end = min(t_end, ends[k])                           # fully filled: it stops resting
            n_orders += 1
            a = t = False
            for (ts_, px, vol) in tape:
                if ts_ < a0 or ts_ >= t_end:
                    continue
                if abs(px - o["price"]) <= TOL * max(1.0, px):
                    atpx["prints"] += 1; atv += vol; a = True
                elif px < o["price"]:
                    thr["prints"] += 1; thr_v += vol; t = True
            orders_with_at += a; orders_with_thr += t
            seen[k] = (a, t)
    res["prints_reaching_orders_off_run_windows"] = {"orders": n_orders, "prints_exactly_at_our_price": atpx["prints"], "volume_at_our_price": atv, "orders_with_such_a_print": orders_with_at,
                                                     "prints_strictly_through_our_price": thr["prints"], "volume_strictly_through": thr_v, "orders_with_such_a_print_through": orders_with_thr,
                                                     "note": "window = placement time + the median placement-to-arrival latency of that order's fills (0 if none) up to placement + 15 s, or the full fill; Bybit sell prints (sign -1) from the slice tape, multiset de-duplication; a through print is evidence only, the rule decides the fill"}
    # 3. what the rule changed, order by order (same signal id in both runs): timing of the first fill, queue ahead at placement, through prints seen
    first = {c: {} for c in ("off", "on")}
    for cell in ("off", "on"):
        for f in sorted(fills[cell], key=lambda x: x["ts"]):
            first[cell].setdefault((f["day"], f["signal_id"]), f)
    dts = [first["on"][k]["ts"] - first["off"][k]["ts"] for k in first["on"] if k in first["off"]]
    same = sum(abs(x) < 1e-6 for x in dts)
    res["timing_on_minus_off_first_fill_s"] = {"orders_filled_in_both": len(dts), "identical_timestamp": same, "earlier_in_on": sum(x < -1e-6 for x in dts), "later_in_on": sum(x > 1e-6 for x in dts),
                                               "min": min(dts) if dts else None, "median_of_changed": st.median([x for x in dts if abs(x) >= 1e-6]) if dts != [0.0] * len(dts) and any(abs(x) >= 1e-6 for x in dts) else None}
    q0 = Counter()
    for o in orders["off"]:
        q0["queue_ahead_zero_at_placement" if o["queue_ahead"] <= 1e-12 else "queue_ahead_positive_at_placement"] += 1
    res["orders_by_queue_at_placement"] = dict(q0)
    # OFF-run fills classified by the PRINT that filled them (the cause 'trade' also covers a print below our price when the rule is off)
    by_print = Counter(); by_print_q = Counter()
    for f in fills["off"]:
        tp = f["trig"].get("price")
        if grp(f["cause"]) == "trade_at_our_price" and tp is not None:
            rel = "print_at_our_price" if abs(tp - f["price"]) <= TOL * max(1.0, tp) else "print_strictly_through_our_price"
            by_print[rel] += 1; by_print_q[rel] += f["qty"]
    res["off_run_trade_fills_by_the_print_that_filled_them"] = {"fills": dict(by_print), "qty": dict(by_print_q)}
    tally = Counter()
    qpos = {(o["day"], o["signal_id"]): o["queue_ahead"] for o in orders["off"]}
    for k, (a, t) in seen.items():
        q = "queue_positive" if qpos[k] > 1e-12 else "queue_zero"
        tally[(q, "orders")] += 1
        if t:
            tally[(q, "with_through_print_in_window")] += 1
            tally[(q, "with_through_print_filled_in_off")] += k in offf
            tally[(q, "with_through_print_filled_in_on")] += k in onf
        if a:
            tally[(q, "with_at_price_print_in_window")] += 1
    res["through_exposure_by_queue_at_placement"] = {f"{q}.{n}": v for (q, n), v in sorted(tally.items())}
    (P / "analysis.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({k: v for k, v in res.items() if k not in ("per_day",)}, indent=1, default=str))


main()
