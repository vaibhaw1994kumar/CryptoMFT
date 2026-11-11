"""Spot simulator validation, step 1: replay real Bybit spot PostOnly orders, one coin-day per process, through the main runner (analysis_mode="maker_only": the
same PASSIVE intent, latency, PostOnly and queue path a strategy order takes; no strategy, no hedge). Each order is released at its real create time (the profile's
entry latency is added by the engine) and cancelled at exactly its real cancel time (scheduled_orders' fifth item).

Privacy: the account export stays on this Mac. Order ids are used only to join an order to its executions inside this process; every row written carries the
schedule index instead, and the rows go to --out (a scratch folder), never into a report. Reports read only aggregates (evaluate.py).

Settings recorded in every result (owner 11 Nov 2026):
  * event_order = "delta_first": each archive day ends with the next day's 00:00:01 snapshot, stamped with the same exchange time and update id as the last
    deltas, so it is the book AFTER them (T4 re-run: 28/28 with delta_first, 26/28 with legacy). Set here, in the validation run config only.
  * trust_local_ts = False on the archive venue: the archive's local_ts is Bybit's own send stamp, not a receive time; the latency preset's feed delay is used.
  * maker_max_age_s = 0 (only the real cancels remove an order); spot_requires_inventory = False and the risk limits lifted (no strategy, so nothing may refuse an order).

usage: python3.12 -m new_backtest.validation.spot_order_replay --orders DIR --data-root ROOT --pair ADAUSDT --day 2026-04-26 --cancel-model proportional --out DIR
  DIR holds order_spot_<PAIR>.jsonl and exec_spot_<PAIR>.jsonl; ROOT/bybit/{book_snapshots,book_events,trades}/pair=<PAIR>/date=<DAY>/ is the converted archive.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import resource
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

PROFILE = "spot_spot_maker_taker_v1"
VENUE = "bybit"
DAY_S = 86_400.0


def _day_bounds(day: str) -> tuple[float, float]:
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    return start, start + DAY_S


def load_orders(orders_dir: Path, pair: str, day: str) -> list[dict]:
    """The real PostOnly limit orders created on `day` (UTC), in create-time order, each with its real outcome. Ids stay inside this function's result."""
    start, end = _day_bounds(day)
    first_exec: dict[str, float] = {}
    for line in open(orders_dir / f"exec_spot_{pair}.jsonl"):
        e = json.loads(line)
        t = int(e["execTime"]) / 1000.0
        oid = e["orderId"]
        if oid not in first_exec or t < first_exec[oid]:
            first_exec[oid] = t
    rows = []
    for line in open(orders_dir / f"order_spot_{pair}.jsonl"):
        o = json.loads(line)
        if o.get("orderType") != "Limit" or o.get("timeInForce") != "PostOnly":
            continue
        created = int(o["createdTime"]) / 1000.0
        if not start <= created < end:
            continue
        status = o["orderStatus"]
        updated = int(o["updatedTime"]) / 1000.0
        rows.append({"_id": o["orderId"], "created": created, "side": o["side"].lower(), "price": float(o["price"]), "qty": float(o["qty"]),
                     "status": status, "updated": updated, "real_filled_qty": float(o.get("cumExecQty") or 0.0),
                     "real_first_fill": first_exec.get(o["orderId"]),
                     "reject_reason": o.get("rejectReason") if status == "Rejected" else None,
                     "cancel_ts": updated if status in ("Cancelled", "PartiallyFilledCanceled") else None})
    rows.sort(key=lambda r: r["created"])
    return rows


def depth_times(data_root: Path, pair: str, day: str) -> list[float]:
    """Exchange times of every depth record of the day (book_events and book_snapshots), sorted: the fresh-book rule (c) of the fill-evidence study."""
    import glob, gzip
    out: list[float] = []
    for stream, key in (("book_events", "exchange_ts"), ("book_snapshots", "timestamp")):
        for f in sorted(glob.glob(f"{data_root}/{VENUE}/{stream}/pair={pair}/date={day}/*.jsonl.gz")):
            with gzip.open(f, "rt") as fh:
                last = None
                for line in fh:
                    t = float(json.loads(line)[key])
                    if t != last:
                        out.append(t)
                        last = t
    out.sort()
    return out


def max_gap(times: list[float], a: float, b: float) -> float:
    """Longest interval without a depth record inside [a, b] (the window's own ends count as edges)."""
    import bisect
    i, j = bisect.bisect_left(times, a), bisect.bisect_right(times, b)
    pts = [a] + times[i:j] + [b]
    return max(y - x for x, y in zip(pts, pts[1:])) if len(pts) > 1 else b - a


def build_config(data_root: Path, pair: str, day: str, cancel_model: str, out_dir: Path, schedule: list[tuple]):
    from new_backtest.profiles import apply_profile, load_profile
    doc = load_profile(PROFILE)
    cfg = apply_profile(doc, data_root=str(data_root), pair=pair, start_date=day, end_date=day, output_dir=str(out_dir),
                        max_trade_log=10 ** 7, equity_bridge="warn", record_exposure=False)
    cfg.venues = (VENUE,)
    cfg.event_order = "delta_first"
    cfg.trust_local_ts = {VENUE: False}
    cfg.hedge_faults = None
    cfg.margin = None
    cfg.circuit_breaker = None
    cfg.spot_requires_inventory = False
    cfg.maker = dataclasses.replace(cfg.maker, cancel_model=cancel_model)
    cfg.arb.maker_max_age_s = 0.0
    cfg.arb.maker_max_live_orders = 10 ** 6
    cfg.arb.max_position_base = 1e12
    cfg.arb.max_venue_imbalance_base = 1e12
    cfg.analysis_mode = "maker_only"
    cfg.analysis_entry_s = None                      # the profile's entry latency is drawn for every order
    cfg.scheduled_orders = tuple(schedule)
    cfg.max_event_ts = None
    cfg.gate_class = "spot_order_replay"
    cfg.allow_rejected_data = "spot order replay on the converted public archive (owner brief 10 Nov 2026, step 1)"
    return cfg


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orders", required=True); ap.add_argument("--data-root", required=True); ap.add_argument("--pair", required=True)
    ap.add_argument("--day", required=True); ap.add_argument("--cancel-model", default="proportional", choices=("proportional", "behind_first", "ahead_first"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    warnings.filterwarnings("ignore")
    from new_backtest.backtester import CrossVenueArbBacktester
    t0 = time.time()
    data_root, out = Path(a.data_root), Path(a.out) / a.pair / a.day / a.cancel_model
    out.mkdir(parents=True, exist_ok=True)
    orders = load_orders(Path(a.orders), a.pair, a.day)
    schedule = [(o["created"], o["side"], o["price"], o["qty"], o["cancel_ts"]) for o in orders]
    r = CrossVenueArbBacktester(build_config(data_root, a.pair, a.day, a.cancel_model, out / "engine", schedule)).run()
    sim = r.analysis["orders"]
    if len(sim) != len(orders):
        raise RuntimeError(f"{len(sim)} scheduled outcomes for {len(orders)} orders")
    times = depth_times(data_root, a.pair, a.day)
    _, day_end = _day_bounds(a.day)
    rows = []
    for o, s in zip(orders, sim):                    # both in create-time order (the engine sorts the schedule by release time, stable)
        end = o["updated"] if o["status"] != "Filled" else (o["real_first_fill"] or o["updated"])
        rows.append({"i": s["schedule_index"], "pair": a.pair, "day": a.day, "cancel_model": a.cancel_model, "created": o["created"], "side": o["side"],
                     "real_status": o["status"], "real_end": o["updated"], "real_first_fill": o["real_first_fill"], "real_filled_qty": o["real_filled_qty"],
                     "qty": o["qty"], "real_reject_reason": o["reject_reason"], "crosses_day_end": o["updated"] >= day_end,
                     "max_depth_gap_s": max_gap(times, o["created"] - 2.0, end),
                     **{k: v for k, v in s.items() if k not in ("schedule_index", "signal_id", "price")}})
    with open(out / "orders.jsonl", "w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    summary = {"pair": a.pair, "day": a.day, "cancel_model": a.cancel_model, "profile": PROFILE, "event_order": "delta_first", "label": "EXPLORATORY",
               "orders": len(orders), "fired": r.analysis["fired"], "skipped": r.analysis["skipped"], "events": r.events_processed,
               "postonly": r.postonly_report, "release_identity": r.release_identity, "wall_s": round(time.time() - t0, 1),
               "max_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6, 1)}
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print(a.pair, a.day, a.cancel_model, "orders", len(orders), "fired", summary["fired"], "skipped", summary["skipped"],
          f"{summary['wall_s']}s rss {summary['max_rss_mb']} MB", flush=True)
    return summary


if __name__ == "__main__":
    main()
