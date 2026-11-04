#!/usr/bin/env python3
"""Task 5 (perp lifecycle): does the backtester's PostOnly rule (new_backtest.postonly.would_cross on the EXCHANGE-TIME book at arrival)
agree with what the venue really did?

    python3 -I scripts/perp_postonly_validation.py --logger-root ../order_lifecycle_logger --out ../out/perp_lifecycle/postonly_validation.json

Two checks, kept apart, because the data supports only one of them fully:
  A  (full) fillprobe orders (PostOnly/GTX, priced at/near the touch) that have a concurrent public book capture: rebuild the book at the
     order's EXCHANGE create time from the captured snapshot + deltas, apply would_cross, compare with what the venue did. In these runs the venue
     rejected none of them, so the 2x2 has only a real=accepted row: it measures FALSE rejects of the model, and cannot measure missed rejects.
  B  (partial) deliberate crossing probes from the latency logs, which carry only the best bid/ask at SEND (no book stream, no book at arrival):
     would_cross on that top of book. Not an arrival-time check.
No order ids or prices are written. TESTNET data from the Mac.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE)]
from new_backtest.order_book import OrderBook          # noqa: E402
from new_backtest.postonly import would_cross           # noqa: E402
import lifecycle_calibration_perp as LC                 # noqa: E402

MIN_CELL = 30


def load_timeline(cap: Path, venue: str, pair: str):
    snaps, evs = [], []
    for st, dst in (("book_snapshots", snaps), ("book_events", evs)):
        for f in sorted((cap / venue / st).glob(f"pair={pair}/date=*/*.jsonl.gz")):
            for l in gzip.open(f, "rt"):
                try:
                    dst.append(json.loads(l))
                except Exception:
                    pass
    snaps.sort(key=lambda s: s["timestamp"])
    evs.sort(key=lambda e: (e["exchange_ts"], e.get("local_mono_ns", 0)))
    return snaps, evs


class Replayer:
    """Rebuild the book at exchange time t: last snapshot at or before t, then the deltas in (snapshot, t] by exchange timestamp."""

    def __init__(self, venue, pair, snaps, evs):
        self.venue, self.pair, self.snaps, self.evs = venue, pair, snaps, evs
        self.ets = [e["exchange_ts"] for e in evs]
        self.event_types = Counter(e.get("event_type") for e in evs)

    def book_at(self, t: float, inclusive: bool = False):
        s = None
        for x in self.snaps:
            if x["timestamp"] <= t:
                s = x
        if s is None:
            return None, None
        b = OrderBook(self.venue, self.pair, delta_semantics="absolute")
        b.apply_snapshot(s["bids"], s["asks"], s["timestamp"])
        lo = bisect_right(self.ets, s["timestamp"])
        hi = bisect_right(self.ets, t) if inclusive else bisect_right([x - 1e-9 for x in self.ets], t - 1e-9)
        last = s["timestamp"]
        for e in self.evs[lo:hi]:
            b.apply_delta(str(e.get("side", "bid")).lower(), float(e["price"]), float(e.get("volume", 0.0)), str(e.get("event_type", "modify")).lower(), e["exchange_ts"])
            last = e["exchange_ts"]
        return b, t - last


def tick_of(prices):
    ps = sorted(set(prices))
    d = [round(b - a, 10) for a, b in zip(ps, ps[1:]) if b - a > 1e-12]
    return min(d) if d else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logger-root", required=True)
    ap.add_argument("--out", default="../out/perp_lifecycle/postonly_validation.json")
    a = ap.parse_args(argv)
    runs = LC.discover(Path(a.logger_root))
    res: dict = {"label": "TESTNET data, logger on the Mac. A = full (arrival-time book), B = partial (top of book at send)", "A": {}, "B": {}}
    # ---------------- A
    cells = defaultdict(lambda: Counter())
    integrity = {}
    integrity_seen: set = set()
    per_run_n = Counter()
    gaps = defaultdict(list)
    sensitivity = Counter()
    for r in runs:
        if r.kind != "fillprobe" or r.capture is None:
            continue
        venue = r.venue
        reps = {}
        for sym in r.symbols:
            snaps, evs = load_timeline(r.capture, venue, sym)
            if snaps and evs:
                reps[sym] = Replayer(venue, sym, snaps, evs)
        # integrity: the capture's OWN periodic reconciliation of its reconstructed book against the venue's REST book (recon_checks.jsonl), once per fillprobe run
        rc = r.capture.parent / "recon_checks.jsonl"
        if rc.exists() and r.capture.parent.name not in integrity_seen:
            integrity_seen.add(r.capture.parent.name)
            for l in open(rc):
                row = json.loads(l)
                v = {"bybit": "bybit", "binance": "binance"}.get(str(row.get("venue")).split("_")[0], str(row.get("venue")))
                integrity.setdefault(v, Counter()).update({"checks": 1, "match": int(row.get("match") is True), "not_evaluated(match=null)": int(row.get("match") is None),
                                                           "mismatch": int(row.get("match") is False)})
        for o in r.orders:
            if o.get("kind") != "fill_probe":
                continue
            sym = o["symbol"]
            ex = o.get("ex") or {}
            arr = (ex.get("created_ms") or ex.get("wsnew_event_ms") or 0) / 1000.0
            rp = reps.get(sym)
            if rp is None or not arr:
                per_run_n["no_book_or_no_arrival_time"] += 1
                continue
            side = "buy" if str(o["side"]).lower().startswith("b") else "sell"
            px = float(o["price"])
            bk, age = rp.book_at(arr)
            if bk is None or not bk.ready:
                per_run_n["no_book_before_arrival"] += 1
                continue
            sim = would_cross(side, px, bk.best_bid, bk.best_ask)
            bk2, _ = rp.book_at(arr, inclusive=True)
            sim2 = would_cross(side, px, bk2.best_bid, bk2.best_ask) if bk2 and bk2.ready else None
            sensitivity["sim_changes_if_events_at_arrival_time_are_included"] += int(sim2 is not None and sim2 != sim)
            real_reject = bool(o.get("post_only_reject")) or str(o.get("outcome", "")).startswith("rejected")
            fresh = "fresh_lt_1s" if (age is not None and age < 1.0) else "stale_ge_1s"
            dist = (bk.best_ask - px) if side == "buy" else (px - bk.best_bid)       # >0 rests, <=0 would cross
            ticks = None
            key = (venue, sym)
            c = cells[key]
            c["n"] += 1
            c["real_reject" if real_reject else "real_accept"] += 1
            c["sim_reject" if sim else "sim_accept"] += 1
            c[f"{'R' if real_reject else 'A'}{'R' if sim else 'A'}"] += 1
            c[fresh] += 1
            gaps[key].append(dist / px * 1e4)
            hour = int(arr // 3600) % 24
            cells[(venue, f"hour_utc={hour:02d}")]["n"] += 1
            cells[(venue, f"hour_utc={hour:02d}")][f"{'R' if real_reject else 'A'}{'R' if sim else 'A'}"] += 1
    out_cells = {}
    tot = defaultdict(Counter)
    for (venue, k), c in sorted(cells.items(), key=str):
        if k.startswith("hour_utc"):
            out_cells[f"{venue}/{k}"] = {"n": c["n"], "published": c["n"] >= MIN_CELL, **{x: c[x] for x in ("AA", "AR", "RA", "RR")}}
            continue
        tot[venue].update(c)
        d = sorted(gaps[(venue, k)])
        out_cells[f"{venue}/{k}"] = {
            "n": c["n"], "published": c["n"] >= MIN_CELL,
            "confusion": {"real_accepted_sim_accepted": c["AA"], "real_accepted_sim_REJECTED(false reject)": c["AR"], "real_rejected_sim_accepted(missed)": c["RA"], "real_rejected_sim_rejected": c["RR"]},
            "false_reject_rate": (c["AR"] / c["real_accept"]) if c["real_accept"] and c["n"] >= MIN_CELL else None,
            "false_reject_wilson95": LC.wilson(c["AR"], c["real_accept"]) if c["n"] >= MIN_CELL else None,
            "book_age_at_arrival": {"fresh_lt_1s": c["fresh_lt_1s"], "stale_ge_1s": c["stale_ge_1s"]},
            "distance_to_cross_bps_at_arrival": {"min": d[0], "median": d[len(d) // 2], "max": d[-1]} if d else None,
        }
    res["A"] = {
        "definition": "fillprobe PostOnly/GTX orders (priced at/near the touch), book rebuilt at the venue's order create time",
        "cells": out_cells,
        "per_venue_total": {v: {"n": c["n"], "real_accepted": c["real_accept"], "real_rejected": c["real_reject"], "sim_rejected": c["sim_reject"],
                                "false_rejects": c["AR"], "missed_rejects": c["RA"],
                                "false_reject_wilson95": LC.wilson(c["AR"], c["real_accept"]), "rule_of_three_upper95_if_zero": (3 / c["real_accept"]) if c["AR"] == 0 and c["real_accept"] else None}
                            for v, c in tot.items()},
        "skipped": dict(per_run_n), "sensitivity": dict(sensitivity),
        "book_reconstruction_integrity": {v: dict(c) for v, c in integrity.items()},
        "power_note": "no real PostOnly reject occurred in these runs, so this cannot show that the model rejects when the venue does; it shows only that it did not reject when the venue accepted",
    }
    # ---------------- B
    bcell = defaultdict(Counter)
    for r in runs:
        for o in r.orders:
            if o.get("kind") != "reject_probe" or not (o.get("post_only_reject") or o.get("outcome") == "cancel_failed"):
                continue
            try:
                bid, ask = float(o["bid_at_send"]), float(o["ask_at_send"])
            except Exception:
                continue
            side = "buy" if str(o["side"]).lower().startswith("b") else "sell"
            sim = would_cross(side, float(o["price"]), bid, ask)
            real = bool(o.get("post_only_reject"))
            c = bcell[r.venue]
            c["n"] += 1
            c[f"{'R' if real else 'A'}{'R' if sim else 'A'}"] += 1
    res["B"] = {"definition": "crossing probes: would_cross on the best bid/ask recorded at SEND (not at arrival); the 3 probes that rested are counted as real accepted",
                "per_venue": {v: {"n": c["n"], "real_rejected_sim_rejected": c["RR"], "real_rejected_sim_accepted(missed)": c["RA"], "real_accepted_sim_rejected": c["AR"], "real_accepted_sim_accepted": c["AA"],
                                  "agreement_wilson95": LC.wilson(c["RR"] + c["AA"], c["n"])} for v, c in bcell.items()}}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({"A_totals": res["A"]["per_venue_total"], "integrity": res["A"]["book_reconstruction_integrity"], "skipped": res["A"]["skipped"],
                      "sens": res["A"]["sensitivity"], "B": res["B"]["per_venue"]}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
