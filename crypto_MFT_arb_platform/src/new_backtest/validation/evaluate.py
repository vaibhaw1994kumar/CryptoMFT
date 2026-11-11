"""Spot simulator validation, step 2: aggregates from the per-order rows of spot_order_replay (no order ids exist in them; only aggregates leave this module).

Classes (real outcome): CANCELLED (a simulated fill before the real cancel = FALSE FILL; the replay cancels at exactly the real cancel time, so any simulated fill is one),
FILLED (first-fill timing: hit = |sim - real| <= 1 s, premature = sim more than 1 s early, late = more than 1 s late, unfilled; denominator all orders, the canonical
definition of the cancel-model study), REJECTED PostOnly vs accepted (confusion matrix of the simulated PostOnly check at arrival), PartiallyFilledCanceled (own class).
Excluded before any rate (owner 11 Nov 2026): 2026-04-23 06:04-06:20 UTC, and any order resting across that day's resync (ADA 06:08:58.321, MNT 06:09:08.302 UTC).
Censored, counted, never scored: orders resting across the end of their coin-day (one coin-day per replay), and orders the engine skipped (book not ready).
Fresh book: no gap longer than 2 s between depth records over [created - 2 s, real end] (rule (c) of the fill-evidence study; the archive has no receive stamps, so its
"delivered more than 1 s late" half does not apply)."""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HIT_S = 1.0
FRESH_GAP_S = 2.0
RESYNC = {"ADAUSDT": datetime(2026, 4, 23, 6, 8, 58, 321000, tzinfo=timezone.utc).timestamp(),
          "MNTUSDT": datetime(2026, 4, 23, 6, 9, 8, 302000, tzinfo=timezone.utc).timestamp()}
WINDOW = (datetime(2026, 4, 23, 6, 4, tzinfo=timezone.utc).timestamp(), datetime(2026, 4, 23, 6, 20, tzinfo=timezone.utc).timestamp())
SECOND_WEEK_FROM = "2026-04-26"      # all 14 days are the public ob200 archive (owner 12 Nov: the recorder capture of 04-19..04-25 was lost); split kept by week


def wilson(k: int, n: int, z: float = 1.96) -> list:
    if n == 0:
        return [None, None]
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def rate(k: int, n: int) -> dict:
    return {"n": n, "k": k, "rate": round(k / n, 4) if n else None, "ci95": wilson(k, n)}


def excluded(r: dict) -> str:
    end = r["real_end"]
    if WINDOW[0] <= r["created"] < WINDOW[1]:
        return "restart_window"
    rs = RESYNC.get(r["pair"])
    if rs is not None and r["created"] < rs <= end:
        return "resting_across_resync"
    return ""


PRE_STALE_S = 1.0                    # TOLERANCES.md (10 Nov, before any number): stale = the last book message is older than 1.0 s at placement
REF_HIT = 0.805                      # the "earlier 81 %" (fill_evidence_report.html: proportional, fresh-book set); consistent if within 5 points


def pre_stale(r: dict) -> str:
    """Pre-registered (TOLERANCES.md): stale when the last book message is older than 1.0 s at placement (exchange clock, the order's arrival)."""
    return "stale (>1 s)" if (r.get("book_age_at_placement_s") or 0.0) > PRE_STALE_S else "fresh"


def pre_queue(r: dict) -> str:
    """Pre-registered (TOLERANCES.md): simulated queue ahead in quote currency: inside the touch (0), up to 1,000, 1,000 to 10,000, above 10,000."""
    if r.get("placed_ts") is None:
        return "not placed (simulated PostOnly reject)"
    q = (r.get("queue_ahead") or 0.0) * (r.get("price") or 0.0)
    if q <= 0 or (r.get("relative_to_touch_ticks") or 0) > 0:
        return "a. inside the touch (0)"
    return "b. <= 1,000" if q <= 1_000 else "c. 1,000-10,000" if q <= 10_000 else "d. > 10,000"


_CACHE: dict = {}


def enrich(rows: list[dict], orders_dir: Path, data_root: Path) -> None:
    """Adds `price` (the real limit price) and `book_age_at_placement_s` (arrival time - last depth record at or before it, exchange clock) to each row.
    Rows are matched to the real orders by their schedule index (both are in create-time order, see spot_order_replay.main)."""
    import bisect
    from new_backtest.validation.spot_order_replay import depth_times, load_orders
    by = defaultdict(list)
    for r in rows:
        by[(r["pair"], r["day"])].append(r)
    for (pair, day), rs in by.items():
        if (pair, day) not in _CACHE:
            _CACHE[(pair, day)] = (load_orders(orders_dir, pair, day), depth_times(data_root, pair, day))
        orders, times = _CACHE[(pair, day)]
        for r in rs:
            o = orders[r["i"]]
            if abs(o["created"] - r["created"]) > 1e-9 or o["side"] != r["side"]:
                raise RuntimeError(f"row {r['i']} of {pair} {day} does not match its order")
            r["price"] = o["price"]
            t = r.get("arrival_ts") or r["created"]
            j = bisect.bisect_right(times, t)
            r["book_age_at_placement_s"] = (t - times[j - 1]) if j else None


def verdicts(res: dict) -> dict:
    """The pass rules of TOLERANCES.md (fixed 10 Nov 2026, before any order row was looked at), applied as written."""
    out = {}
    v2a = {}
    for m, v in res.items():
        a = v["cancelled_false_fill"]["all"]
        lo, hi = a["ci95"]
        v2a[m] = "PASS" if (a["rate"] <= 0.05 and hi <= 0.10) else ("FAIL" if lo > 0.10 else "INCONCLUSIVE")
    out["2a_false_fill"] = {"per_model": v2a, "verdict": v2a["proportional"] if len(set(v2a.values())) == 1 else "MODEL-DEPENDENT",
                            "rule": "PASS = point <= 5 % AND upper 95 % bound <= 10 %; FAIL = lower bound > 10 %; same under all three models or model-dependent"}
    t = res["proportional"]["filled_timing"]["all"]
    hit, prem = t["hit"]["rate"], t["premature"]["rate"]
    out["2b_fill_timing"] = {"hit": hit, "reference": REF_HIT, "consistent_with_reference": abs(hit - REF_HIT) <= 0.05, "premature": prem,
                             "premature_ok": prem <= 0.10, "verdict": "PASS" if (abs(hit - REF_HIT) <= 0.05 and prem <= 0.10) else "FAIL",
                             "rule": "hit within 5 points of the earlier 80.5 % (proportional) AND premature <= 10 %"}
    cm = res["proportional"]["postonly_confusion"]
    rr, ra = cm.get("real_rejected__sim_rejected", 0), cm.get("real_rejected__sim_accepted", 0)
    ar, aa = cm.get("real_accepted__sim_rejected", 0), cm.get("real_accepted__sim_accepted", 0)
    sens, fr = rate(rr, rr + ra), rate(ar, ar + aa)
    out["2c_postonly"] = {"sensitivity": sens, "false_reject_rate": fr, "sensitivity_ok": sens["rate"] >= 0.70, "false_reject_ok": fr["rate"] <= 0.02,
                          "verdict": "PASS" if (sens["rate"] >= 0.70 and fr["rate"] <= 0.02) else "FAIL",
                          "rule": "sensitivity >= 70 % AND false-reject rate on real accepted orders <= 2 %"}
    return out


def queue_bucket(r: dict) -> str:
    """Queue ahead at placement in multiples of the order's own size. A new order joins at the back of its level, so its share of the level says
    nothing (it is always the whole level); the size of the queue and the distance from the touch (touch_bucket) do."""
    if r.get("placed_ts") is None:
        return "not placed (simulated PostOnly reject)"
    if r.get("queue_unknown"):
        return "unknown"
    if r.get("level_was_absent"):
        return "new_level"
    x = (r.get("queue_ahead") or 0.0) / max(r.get("qty") or 0.0, 1e-12)
    return "<10x" if x < 10 else "10-100x" if x < 100 else "100-1000x" if x < 1000 else ">=1000x"


def touch_bucket(r: dict) -> str:
    t = r.get("relative_to_touch_ticks")
    return "not placed (simulated PostOnly reject)" if t is None else ("inside the spread" if t > 0 else "at the touch" if t == 0 else f"{-t} tick(s) behind")


def sim_first(r: dict):
    return r.get("first_fill_exchange_ts") if r.get("first_fill_exchange_ts") is not None else r.get("first_fill_ts")


def load(root: Path, cancel_model: str) -> list[dict]:
    rows = []
    for f in sorted(root.glob(f"*/*/{cancel_model}/orders.jsonl")):
        rows += [json.loads(line) for line in open(f)]
    return rows


def evaluate(rows: list[dict]) -> dict:
    out = {"orders": len(rows), "excluded": defaultdict(int), "censored": defaultdict(int)}
    scored = []
    for r in rows:
        why = excluded(r)
        if why:
            out["excluded"][why] += 1
        elif r.get("skipped"):
            out["censored"]["engine_skipped_" + r["skipped"]] += 1
        elif r["crosses_day_end"]:
            out["censored"]["resting_across_day_end"] += 1
        else:
            scored.append(r)
    out["scored"] = len(scored)

    def group(sel, key):
        g = defaultdict(list)
        for r in sel:
            g[key(r)].append(r)
        return dict(sorted(g.items()))

    # a. real CANCELLED
    canc = [r for r in scored if r["real_status"] == "Cancelled"]
    ff = lambda sel: rate(sum(1 for r in sel if (r.get("filled_qty") or 0) > 1e-12), len(sel))
    out["cancelled_false_fill"] = {"all": ff(canc), "by_day": {k: ff(v) for k, v in group(canc, lambda r: r["day"]).items()},
                                   "by_pair": {k: ff(v) for k, v in group(canc, lambda r: r["pair"]).items()},
                                   "PRE_by_queue_quote": {k: ff(v) for k, v in group(canc, pre_queue).items()},
                                   "PRE_by_book_age": {k: ff(v) for k, v in group(canc, pre_stale).items()},
                                   "by_queue_at_placement": {k: ff(v) for k, v in group(canc, queue_bucket).items()},
                                   "by_ticks_from_touch": {k: ff(v) for k, v in group(canc, touch_bucket).items()},
                                   "by_book": {k: ff(v) for k, v in group(canc, lambda r: "fresh" if r["max_depth_gap_s"] <= FRESH_GAP_S else "stale").items()},
                                   "rejected_by_sim_postonly": sum(1 for r in canc if r.get("rejected"))}
    # b. real FILLED
    filled = [r for r in scored if r["real_status"] == "Filled" and r.get("real_first_fill") is not None]

    def timing(sel):
        c = defaultdict(int)
        for r in sel:
            s = sim_first(r)
            if r.get("rejected"):
                c["sim_rejected"] += 1
            elif s is None:
                c["unfilled"] += 1
            elif s < r["real_first_fill"] - HIT_S:
                c["premature"] += 1
            elif s > r["real_first_fill"] + HIT_S:
                c["late"] += 1
            else:
                c["hit"] += 1
        n = len(sel)
        return {"n": n, **{k: rate(c[k], n) for k in ("hit", "premature", "late", "unfilled", "sim_rejected")}}

    out["filled_timing"] = {"all": timing(filled), "by_day": {k: timing(v) for k, v in group(filled, lambda r: r["day"]).items()},
                            "by_pair": {k: timing(v) for k, v in group(filled, lambda r: r["pair"]).items()},
                            "by_book": {k: timing(v) for k, v in group(filled, lambda r: "fresh" if r["max_depth_gap_s"] <= FRESH_GAP_S else "stale").items()},
                            "PRE_by_queue_quote": {k: timing(v) for k, v in group(filled, pre_queue).items()},
                            "PRE_by_book_age": {k: timing(v) for k, v in group(filled, pre_stale).items()},
                            "by_queue_at_placement": {k: timing(v) for k, v in group(filled, queue_bucket).items()},
                            "by_ticks_from_touch": {k: timing(v) for k, v in group(filled, touch_bucket).items()},
                            "by_week": {k: timing(v) for k, v in group(filled, lambda r: "04-26..05-02" if r["day"] >= SECOND_WEEK_FROM else "04-19..04-25").items()},
                            "reference": "80.5% hit (proportional) on the fresh-book set of the fill-evidence study (22 coins, collector data, 2026-10-30)"}
    # c. PostOnly reject at arrival
    po = [r for r in scored if r["real_status"] in ("Rejected", "Filled", "Cancelled", "PartiallyFilledCanceled")]
    cm = defaultdict(int)
    for r in po:
        real = "rejected" if r["real_status"] == "Rejected" else "accepted"
        sim = "rejected" if r.get("rejected") == "postonly_reject" else "accepted"
        cm[f"real_{real}__sim_{sim}"] += 1
    out["postonly_confusion"] = dict(cm)
    out["partially_filled_cancelled"] = [{"day": r["day"], "pair": r["pair"], "sim_filled": (r.get("filled_qty") or 0) > 1e-12} for r in scored
                                         if r["real_status"] == "PartiallyFilledCanceled"]
    out["excluded"], out["censored"] = dict(out["excluded"]), dict(out["censored"])
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--orders", required=True); ap.add_argument("--data-root", required=True)
    a = ap.parse_args()
    res = {}
    for m in ("proportional", "behind_first", "ahead_first"):
        rows = load(Path(a.runs), m)
        enrich(rows, Path(a.orders), Path(a.data_root))
        res[m] = evaluate(rows)
    Path(a.out).write_text(json.dumps({"label": "EXPLORATORY", "metric_definitions": __doc__,
                                       "note": "PRE_* breakdowns and `verdicts` use the definitions of TOLERANCES.md (10 Nov, before any number); the other breakdowns were chosen after the first results and are supplementary",
                                       "verdicts": verdicts(res), "results": res}, indent=1))
    print(json.dumps({m: {"scored": v["scored"], "false_fill": v["cancelled_false_fill"]["all"], "hit": v["filled_timing"]["all"].get("hit")} for m, v in res.items()}, indent=1))
