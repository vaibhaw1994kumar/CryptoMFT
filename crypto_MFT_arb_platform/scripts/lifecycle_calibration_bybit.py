#!/usr/bin/env python3
"""Measure order-lifecycle statistics from the Bybit order / execution export (bybit_30d.zip).

    python3 -I scripts/lifecycle_calibration_bybit.py --zip ~/Downloads/bybit_30d.zip \
        --out config/lifecycle_calibration_bybit.json

Reads the zip in place (nothing is extracted), writes aggregates only: no order ids, link ids or prices.

What the export is, and is not
  * One FINAL-STATE record per order (createdTime, updatedTime, orderStatus, rejectReason, cancelType). It is not an
    event stream: there is no acknowledgement time, no cancel-sent time and no intermediate update.
  * Unfilled orders (Cancelled / Rejected) are present only from the moment the export's retention reaches them
    (first cancel 2026-04-30 23:55 UTC, first reject 2026-05-01 00:00 UTC). Before that only orders that filled
    are present, so any rate computed over earlier days would be biased toward zero. Rates here use only the window
    in which unfilled orders are present (`unfilled_window`).
  * Quantity measured as "ack latency" is a PROXY: createdTime (exchange clock) minus the client send stamp that the
    order manager embeds in orderLinkId ('mm' + 9 digits of epoch-ms mod 1e9, optionally followed by a 4-digit counter).
    It is one-way client-stamp -> exchange-created, including an unknown client/exchange clock offset. The return leg
    (exchange -> client) is not in the data.

Anything the data cannot answer is written as null with a reason; nothing is filled with a guess.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import hashlib
import json
import math
import re
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

SCHEMA = "lifecycle_calibration_bybit/v1"
MIN_N = {"p50": 30, "p90": 30, "p99": 100}     # a percentile is published only from this many samples
MIN_N_CELL = 30                                # coin x hour cells below this are listed with n only
UNFILLED_WINDOW_START = dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc)   # first full hour with Rejected orders present
LINK_RE = re.compile(r"^mm(\d{9})(\d{4})?$")
PLAUSIBLE_MS = (-60_000, 600_000)              # reconstructed client-stamp -> created gap outside this is "unparseable"
GAP_N_S = (10, 30, 60, 120, 300, 900)
QGRID = [i / 100 for i in range(100)] + [0.995, 0.999, 1.0]   # for sampling the empirical distribution in a backtest (inverse CDF)


def utc(ms: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc)


def day(ms: float) -> str:
    return utc(ms).strftime("%Y-%m-%d")


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def quantile(sorted_xs: list[float], q: float) -> float:
    """Linear interpolation between order statistics."""
    n = len(sorted_xs)
    if n == 1:
        return sorted_xs[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def pstats(xs: list[float], unit: str = "ms") -> dict:
    """n always; min / max / mean always when n > 0; percentiles only above the minimum sample sizes."""
    n = len(xs)
    out: dict = {"n": n}
    if n == 0:
        return out
    s = sorted(xs)
    out["min"] = round(s[0], 3)
    out["max"] = round(s[-1], 3)
    out["mean"] = round(sum(s) / n, 3)
    for name, q in (("p50", 0.5), ("p90", 0.9), ("p99", 0.99)):
        out[name] = round(quantile(s, q), 3) if n >= MIN_N[name] else None
    if any(out[k] is None for k in ("p50", "p90", "p99")):
        out["suppressed"] = [k for k in ("p50", "p90", "p99") if out[k] is None]
        out["suppressed_reason"] = f"fewer than {MIN_N} samples required per percentile"
    out["unit"] = unit
    return out


def wilson(k: int, n: int, z: float = 1.96) -> list[float | None]:
    if n <= 0:
        return [None, None]
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 6), round(min(1.0, c + h), 6)]


def rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": (round(k / n, 6) if n else None), "wilson95": wilson(k, n)}


def parse_client_ms(link: str, created_ms: int) -> int | None:
    """Epoch-ms client stamp reconstructed from orderLinkId, or None when the id carries none."""
    m = LINK_RE.match(link)
    if not m:
        return None
    ts9 = int(m.group(1))
    base = created_ms - created_ms % 1_000_000_000
    best = None
    for cand in (base - 1_000_000_000 + ts9, base + ts9, base + 1_000_000_000 + ts9):
        gap = created_ms - cand
        if PLAUSIBLE_MS[0] <= gap <= PLAUSIBLE_MS[1] and (best is None or abs(gap) < abs(created_ms - best)):
            best = cand
    return best


def load(zip_path: str):
    z = zipfile.ZipFile(zip_path)
    orders, execs, linear = [], defaultdict(list), Counter()
    for name in z.namelist():
        base = name.rsplit("/", 1)[-1]
        if base.startswith("order_spot_") and base.endswith(".jsonl"):
            for line in z.open(name):
                if line.strip():
                    orders.append(json.loads(line))
        elif base.startswith("exec_spot_") and base.endswith(".jsonl"):
            for line in z.open(name):
                if line.strip():
                    r = json.loads(line)
                    execs[r["orderId"]].append(r)
        elif base.startswith(("order_linear_", "exec_linear_")) and base.endswith(".jsonl"):
            for line in z.open(name):
                if line.strip():
                    linear[base.split("_")[0] + "_linear"] += 1
    return orders, execs, linear, z


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    orders, execs, linear, z = load(args.zip)
    n_all = len(orders)
    for o in orders:
        o["_c"] = int(o["createdTime"])
        o["_u"] = int(o["updatedTime"])
        o["_day"] = day(o["_c"])
        o["_hour"] = utc(o["_c"]).hour
        o["_post"] = o["timeInForce"] == "PostOnly"
        o["_client"] = parse_client_ms(o["orderLinkId"], o["_c"]) if o["_post"] else None
    window_start_ms = int(UNFILLED_WINDOW_START.timestamp() * 1000)
    for o in orders:
        o["_win"] = o["_c"] >= window_start_ms

    # ── structure of the export ──────────────────────────────────────────────
    by_day = defaultdict(Counter)
    for o in orders:
        by_day[o["_day"]][o["orderStatus"]] += 1
    first_cancel = min((o["_c"] for o in orders if o["orderStatus"] in ("Cancelled", "PartiallyFilledCanceled")), default=None)
    first_cancel_pure = min((o["_c"] for o in orders if o["orderStatus"] == "Cancelled"), default=None)
    first_reject = min((o["_c"] for o in orders if o["orderStatus"] == "Rejected"), default=None)
    link_forms = Counter()
    for o in orders:
        link = o["orderLinkId"]
        m = LINK_RE.match(link)
        link_forms["mm+9digits" if m and m.group(2) is None else "mm+9digits+4digits" if m else
                   "numeric_19_no_stamp" if re.fullmatch(r"\d{19}", link) else "other_prefix_no_stamp"] += 1

    structure = {
        "orders_total_spot": n_all,
        "orders_excluded_linear": dict(linear),
        "linear_note": "perpetual (linear) records are a few hundred rows and are not analysed here",
        "records_are": "one final-state snapshot per order; duplicate orderIds: "
                       + str(n_all - len({o['orderId'] for o in orders})),
        "created_range_utc": [utc(min(o["_c"] for o in orders)).isoformat(), utc(max(o["_c"] for o in orders)).isoformat()],
        "status_by_day_utc": {d: dict(c) for d, c in sorted(by_day.items())},
        "first_cancel_created_utc": utc(first_cancel).isoformat() if first_cancel else None,
        "first_pure_cancel_created_utc": utc(first_cancel_pure).isoformat() if first_cancel_pure else None,
        "first_reject_created_utc": utc(first_reject).isoformat() if first_reject else None,
        "unfilled_window": {
            "start_utc": UNFILLED_WINDOW_START.isoformat(),
            "end_utc": utc(max(o["_c"] for o in orders)).isoformat(),
            "why": "Cancelled / Rejected orders are absent from the export before this point (only orders that filled "
                   "survive), so cancel and reject rates are measured here and nowhere else. The real PostOnly reject "
                   "rate on 2026-04-19..25 is therefore UNKNOWN, not zero.",
            "orders_in_window": sum(o["_win"] for o in orders),
            "postonly_orders_in_window": sum(o["_win"] and o["_post"] for o in orders),
        },
        "orderLinkId_forms": dict(link_forms),
        "fields_not_in_the_export": {
            "ack_time": "no acknowledgement timestamp exists; see ack_latency_proxy for what is derivable",
            "cancel_sent_time": "no cancel request timestamp exists, so cancel latency cannot be measured",
            "intermediate_updates": "only the final state per order is stored (no New -> PartiallyFilled history)",
            "receive_side_timestamps": "no client receive times; the exchange -> client leg is unmeasured",
            "disconnect_markers": "no connection events; disconnects can only be suspected from activity gaps",
        },
    }

    # ── a) ack latency proxy ─────────────────────────────────────────────────
    parsed = [o for o in orders if o["_post"] and o["_client"] is not None]
    for o in parsed:
        o["_ack"] = float(o["_c"] - o["_client"])
    unparseable = [o for o in orders if o["_post"] and o["_client"] is None]
    unparse_reason = Counter()
    for o in unparseable:
        link = o["orderLinkId"]
        unparse_reason["no_stamp_in_link_id" if not LINK_RE.match(link) else "gap_outside_plausible_range"] += 1
    outside_neg = sum(1 for o in parsed if o["_ack"] < 0)

    def ack(sel):
        return pstats([o["_ack"] for o in sel if "_ack" in o])

    forms = (("mm+9digits", lambda l: len(l) == 11), ("mm+9digits+4digits", lambda l: len(l) == 15))
    form_days = {f: {o["_day"] for o in parsed if t(o["orderLinkId"])} for f, t in forms}
    overlap_days = sorted(form_days["mm+9digits"] & form_days["mm+9digits+4digits"])
    by_form = {d: {f: ack([o for o in parsed if o["_day"] == d and t(o["orderLinkId"])]) for f, t in forms} for d in overlap_days}
    by_day = {d: ack([o for o in parsed if o["_day"] == d]) for d in sorted({o["_day"] for o in parsed})}
    by_status_window = {st: ack([o for o in parsed if o["_win"] and o["orderStatus"] == st]) for st in ("Filled", "Cancelled", "Rejected")}
    by_coin = {s: ack([o for o in parsed if o["symbol"] == s]) for s in sorted({o["symbol"] for o in parsed})}
    by_hour = {f"{h:02d}": ack([o for o in parsed if o["_hour"] == h]) for h in range(24)}
    cell = defaultdict(list)
    for o in parsed:
        cell[(o["symbol"], o["_hour"])].append(o["_ack"])
    coin_hour = {f"{s}|{h:02d}": ({"n": len(v)} if len(v) < MIN_N_CELL else pstats(v)) for (s, h), v in sorted(cell.items())}
    periods = {
        "before_2026-04-19": [o for o in parsed if o["_day"] < "2026-04-19"],
        "2026-04-19..2026-04-25": [o for o in parsed if "2026-04-19" <= o["_day"] <= "2026-04-25"],
        "2026-04-26..2026-05-02": [o for o in parsed if o["_day"] >= "2026-04-26"],
        "unfilled_window": [o for o in parsed if o["_win"]],
        "all": parsed,
    }
    by_period = {k: ack(v) for k, v in periods.items()}
    quantile_grids = {}
    for k, v in periods.items():
        xs = sorted(o["_ack"] for o in v)
        if len(xs) >= MIN_N["p50"]:
            quantile_grids[k] = {"n": len(xs), "q": QGRID, "ms": [round(quantile(xs, q), 3) for q in QGRID]}
    ack_block = {
        "definition": "createdTime minus the client send stamp embedded in orderLinkId, milliseconds. One-way "
                      "client-stamp -> exchange-created PLUS an unknown client/exchange clock offset. This is the "
                      "quantity a backtest needs for 'order arrives at the matching engine'. It is NOT an ack "
                      "round trip and says nothing about the exchange -> client leg.",
        "validation_of_the_stamp_reading": {
            "negative_gaps": outside_neg, "n_parsed": len(parsed),
            "note": "if the stamp were not a client send time, gaps would scatter around zero including negatives",
            "id_forms_compared_on_days_where_both_occur": by_form,
            "id_forms_note": "the two forms are compared within the same day only, because latency differs by day",
        },
        "orders_postonly_total": sum(o["_post"] for o in orders),
        "orders_parsed": len(parsed),
        "orders_excluded": dict(unparse_reason),
        "overall_postonly": ack(parsed),
        "by_period": by_period,
        "by_day_utc": by_day,
        "non_stationarity_note": "the proxy's median moves from 17 ms to 73 ms across days (it contains the client/exchange "
                                 "clock offset, so a drifting offset and a real latency change cannot be told apart). A "
                                 "single pooled distribution hides this; stressed presets should cover the worst day.",
        "by_status_in_unfilled_window": by_status_window,
        "by_status_note": "status groups differ mostly because of WHEN they occurred (05-01 holds 405 of 596 rejects and "
                          "is the slowest day), not because rejects take longer",
        "by_coin": by_coin,
        "by_hour_utc": by_hour,
        "by_coin_hour_utc": coin_hour,
        "by_coin_hour_note": f"cells with n < {MIN_N_CELL} carry n only",
        "empirical_quantile_grids": quantile_grids,
        "rejected_orders_only": ack([o for o in parsed if o["orderStatus"] == "Rejected"]),
        "return_leg_exchange_to_client": None,
        "return_leg_reason": "not in the export (no client receive times)",
    }

    # ── b) rejects, from the window in which unfilled orders exist ───────────
    win = [o for o in orders if o["_win"]]
    win_post = [o for o in win if o["_post"]]
    rej = [o for o in win if o["orderStatus"] == "Rejected"]
    rej_reason = Counter(o["rejectReason"] for o in rej)
    pow_rej = [o for o in rej if o["rejectReason"] == "EC_PostOnlyWillTakeLiquidity"]
    other_rej = [o for o in rej if o["rejectReason"] != "EC_PostOnlyWillTakeLiquidity"]
    n_win_non_post = len(win) - len(win_post)

    def rej_rate(sel_all, sel_rej):
        return rate(len(sel_rej), len(sel_all))

    by_coin_rej = {}
    for s in sorted({o["symbol"] for o in win_post}):
        a = [o for o in win_post if o["symbol"] == s]
        by_coin_rej[s] = rej_rate(a, [o for o in a if o["orderStatus"] == "Rejected" and o["rejectReason"] == "EC_PostOnlyWillTakeLiquidity"])
    by_hour_rej = {}
    for h in range(24):
        a = [o for o in win_post if o["_hour"] == h]
        by_hour_rej[f"{h:02d}"] = rej_rate(a, [o for o in a if o["orderStatus"] == "Rejected" and o["rejectReason"] == "EC_PostOnlyWillTakeLiquidity"])
    by_day_rej = {}
    for d in sorted({o["_day"] for o in win_post}):
        a = [o for o in win_post if o["_day"] == d]
        by_day_rej[d] = rej_rate(a, [o for o in a if o["orderStatus"] == "Rejected"])
    rej_side = Counter(o["side"] for o in pow_rej)
    rejects = {
        "window": unfilled_window_label(UNFILLED_WINDOW_START, orders),
        "denominator_note": "PostOnly orders created in the window (Filled + Cancelled + PartiallyFilledCanceled + Rejected)",
        "postonly_would_take_liquidity": rej_rate(win_post, pow_rej),
        "other_reasons": {
            "count": len(other_rej),
            "by_reason": {k: v for k, v in rej_reason.items() if k != "EC_PostOnlyWillTakeLiquidity"},
            "rate_over_all_orders_in_window": rate(len(other_rej), len(win)),
            "rate_over_non_postonly_orders": rate(len([o for o in other_rej if not o["_post"]]), n_win_non_post),
            "upper_bound_95_rule_of_three": round(3.0 / max(1, len(win)), 6),
            "interpretation": "zero non-PostOnly rejects were observed; the data bound the rate, they do not measure it",
        },
        "by_reason_counts": dict(rej_reason),
        "by_coin": by_coin_rej,
        "by_hour_utc": by_hour_rej,
        "by_day_utc": by_day_rej,
        "rejected_side_counts": dict(rej_side),
        "reject_processing_time_ms_updated_minus_created": pstats([float(o["_u"] - o["_c"]) for o in rej]),
    }

    # ── c) cancel latency ────────────────────────────────────────────────────
    cancelled = [o for o in win if o["orderStatus"] == "Cancelled"]
    cancel_block = {
        "cancel_latency_sent_to_confirmed": None,
        "reason": "the export has no cancel-request timestamp; updatedTime of a Cancelled order is the confirmation "
                  "time and the send time is not recorded",
        "informational_resting_time_before_cancel_confirmed_ms": pstats([float(o["_u"] - o["_c"]) for o in cancelled]),
        "informational_note": "this is order lifetime, driven by the strategy's own timeout, not a latency",
        "cancelled_orders_in_window": len(cancelled),
        "cancelled_by_type": dict(Counter(o["cancelType"] for o in cancelled)),
    }

    # ── d) cancel / fill races ───────────────────────────────────────────────
    filled_cancel_user = [o for o in orders if o["orderStatus"] == "Filled" and o["cancelType"] == "CancelByUser"]
    filled_origcl = [o for o in orders if o["orderStatus"] == "Filled" and o["rejectReason"] == "EC_OrigClOrdIDDoesNotExist"]
    ids_a = {o["orderId"] for o in filled_cancel_user}
    ids_b = {o["orderId"] for o in filled_origcl}
    pfc = [o for o in orders if o["orderStatus"] == "PartiallyFilledCanceled"]
    gap_last_fill = []
    for o in pfc:
        fs = [int(e["execTime"]) for e in execs.get(o["orderId"], [])]
        if fs:
            gap_last_fill.append(float(o["_u"] - max(fs)))
    cancelled_with_fill = sum(1 for o in orders if o["orderStatus"] == "Cancelled" and execs.get(o["orderId"]))
    late_exec = 0
    for o in orders:
        if o["orderStatus"] in ("Cancelled", "PartiallyFilledCanceled"):
            late_exec += sum(1 for e in execs.get(o["orderId"], []) if int(e["execTime"]) > o["_u"] + 1)

    def in_win(sel):
        return [o for o in sel if o["_win"]]

    cancel_requests_visible_win = len(cancelled) + len(in_win(pfc)) + len(in_win(filled_cancel_user)) + len(in_win(filled_origcl))
    races = {
        "definitions": {
            "filled_after_cancel_requested": "orderStatus Filled with cancelType CancelByUser: a cancel was sent, the order filled anyway",
            "cancel_found_nothing": "orderStatus Filled with rejectReason EC_OrigClOrdIDDoesNotExist: the cancel / amend reached the exchange after the order was gone",
            "partial_then_cancelled": "orderStatus PartiallyFilledCanceled: some fill, then cancelled; whether the fill came after the cancel was sent is not recorded",
        },
        "filled_after_cancel_requested": {"all_days": len(filled_cancel_user), "in_unfilled_window": len(in_win(filled_cancel_user)),
                                          "by_day": dict(Counter(o["_day"] for o in filled_cancel_user))},
        "cancel_found_nothing": {"all_days": len(filled_origcl), "in_unfilled_window": len(in_win(filled_origcl)),
                                 "by_day": dict(Counter(o["_day"] for o in filled_origcl))},
        "overlap_of_the_two_groups": len(ids_a & ids_b),
        "union_of_the_two_groups": len(ids_a | ids_b),
        "partial_then_cancelled": {"all_days": len(pfc), "in_unfilled_window": len(in_win(pfc)),
                                   "last_fill_to_cancel_confirmed_ms": pstats(gap_last_fill),
                                   "share_with_last_fill_within_ms": {
                                       str(t): rate(sum(1 for g in gap_last_fill if g <= t), len(gap_last_fill)) for t in (50, 100, 250, 500, 1000)}},
        "cancel_requests_visible_in_unfilled_window": cancel_requests_visible_win,
        "race_share_in_unfilled_window": rate(len(in_win(filled_cancel_user)) + len(in_win(filled_origcl)) - len({o['orderId'] for o in in_win(filled_cancel_user)} & {o['orderId'] for o in in_win(filled_origcl)}),
                                              cancel_requests_visible_win),
        "race_share_note": "numerator = filled orders that carry evidence of a cancel; denominator = every cancel attempt "
                           "that left a trace in the window. A cancel sent to an order that filled normally leaves no trace "
                           "only when it also found nothing, so this is a lower bound on the true race share.",
        "integrity_checks": {"cancelled_orders_with_executions": cancelled_with_fill,
                             "executions_after_cancel_confirmed": late_exec},
    }

    # ── e) disconnect candidates ─────────────────────────────────────────────
    ev = []
    iv = []
    for o in win:
        ev.append(o["_c"])
        ev.append(o["_u"])
        iv.append((o["_c"], o["_u"]))
        for e in execs.get(o["orderId"], []):
            ev.append(int(e["execTime"]))
    ev.sort()
    iv.sort()
    starts = [s for s, _ in iv]
    # max end of orders started before position i, to test "an order was open across the gap"
    max_end = []
    cur = 0
    for _, e in iv:
        cur = max(cur, e)
        max_end.append(cur)

    def open_across(a, b):
        i = bisect.bisect_left(starts, a)       # orders created strictly before a
        return i > 0 and max_end[i - 1] >= b
    gap_rows = []
    for i in range(1, len(ev)):
        g = (ev[i] - ev[i - 1]) / 1000.0
        if g > GAP_N_S[0]:
            gap_rows.append((g, ev[i - 1], ev[i], open_across(ev[i - 1], ev[i])))
    gap_summary = {}
    for n in GAP_N_S:
        sel = [r for r in gap_rows if r[0] > n]
        sel_open = [r for r in sel if r[3]]
        gap_summary[f">{n}s"] = {"all_gaps": len(sel), "all_gap_seconds": round(sum(r[0] for r in sel), 1),
                                 "with_an_order_open_across": len(sel_open),
                                 "open_gap_seconds": round(sum(r[0] for r in sel_open), 1)}
    window_s = (max(ev) - min(ev)) / 1000.0 if ev else 0.0
    top = sorted((r for r in gap_rows if r[3]), key=lambda r: -r[0])[:10]
    gaps = {
        "window": [utc(min(ev)).isoformat(), utc(max(ev)).isoformat()] if ev else None,
        "window_seconds": round(window_s, 1),
        "events_used": len(ev),
        "definition": "interval with no order create / fill / final-update event on ANY of the 22 symbols",
        "by_threshold": gap_summary,
        "longest_with_an_order_open_across": [
            {"start_utc": utc(a).isoformat(), "end_utc": utc(b).isoformat(), "seconds": round(g, 1)} for g, a, b, _ in top],
        "interpretation": "CANDIDATES ONLY. The export cannot tell a connection loss from an idle bot or a quiet market; "
                          "a final-state record carries no heartbeat. No disconnect rate is derived from this.",
        "disconnect_rate_per_day": None,
        "disconnect_duration_distribution": None,
        "reason_null": "not observable from final-state order records",
    }

    # ── assemble ─────────────────────────────────────────────────────────────
    body = {
        "schema": SCHEMA,
        "source": {
            "zip_name": Path(args.zip).name,
            "zip_sha256": sha256_file(args.zip),
            "zip_members": len(z.namelist()),
            "venue": "bybit", "market": "spot", "account_notes": "own order flow of the market-making bot (orderLinkId prefix 'mm')",
        },
        "script": {"path": "scripts/lifecycle_calibration_bybit.py", "sha256": sha256_file(__file__)},
        "min_sample_sizes": {"percentiles": MIN_N, "coin_hour_cell": MIN_N_CELL},
        "structure": structure,
        "ack_latency_proxy": ack_block,
        "rejects": rejects,
        "cancel_latency": cancel_block,
        "cancel_fill_races": races,
        "disconnect_candidates": gaps,
    }
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
    out = {"meta": {"generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "content_sha256": hashlib.sha256(canon.encode()).hexdigest(),
                    "content_sha256_covers": "every key except meta"}, **body}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1, sort_keys=False) + "\n")
    print(f"wrote {args.out}  orders={n_all} parsed_ack={len(parsed)} window_postonly={len(win_post)} rejects={len(rej)}")
    return 0


def unfilled_window_label(start: dt.datetime, orders) -> list[str]:
    return [start.isoformat(), utc(max(o["_c"] for o in orders)).isoformat()]


if __name__ == "__main__":
    sys.exit(main())
