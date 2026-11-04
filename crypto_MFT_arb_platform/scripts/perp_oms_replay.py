#!/usr/bin/env python3
"""Tasks 3 and 4 (perp lifecycle): (3) count every native status / reason / REST code seen in the ollog perp runs and map it with
new_backtest.venue_semantics_perp (anything unmapped is listed, nothing is defaulted); (4) replay each usable run's REAL order updates, in local
arrival order, through crypto_stat_arb.oms.OMS.ingest_order_update (tests/parity_adapters.run_oms) and compare the OMS end state, booked quantity and
fill increments with the exchange's final state for that order.

    python3 -I scripts/perp_oms_replay.py --logger-root ../order_lifecycle_logger --out-dir ../out/perp_lifecycle

Aggregates only: no order, client or exec ids and no prices are written. Every replayed event carries source='historical'; nothing is modelled or injected.
The exchange's final state is taken from fields the logger recorded independently of the status stream (outcome, filled_qty, per-exec quantities), so the
comparison is not circular for state/quantity; the event KINDS fed to the OMS do come from the Task 3 mapping, which is the thing under test with it.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from new_backtest.order_state_machine import Event, Kind, State           # noqa: E402
from new_backtest.venue_semantics_perp import UnmappedVenueStatus, map_rest_reject, map_stream_update   # noqa: E402

sys.path.insert(0, str(HERE))
import lifecycle_calibration_perp as LC                                    # noqa: E402

EPS = 1e-9


def exchange_final(o: dict) -> tuple[State | None, float, list[float]]:
    """Final state, booked fraction and fill increments the exchange holds, from the logger's independent fields."""
    qty = float(o.get("qty") or 0) or None
    execs = [float(x["qty"]) for x in (o.get("execs") or [])]
    out = o.get("outcome") or ""
    filled = float(o.get("filled_qty") or sum(execs) or 0.0)
    frac = (filled / qty) if qty else 0.0
    incr = [x / qty for x in execs] if qty else []
    if out == "cancel_failed":
        return None, frac, incr                       # the probe order rested and could not be cancelled: the exchange state is not recorded
    if frac >= 1.0 - 1e-6:
        return State.FILLED, min(frac, 1.0), incr
    if out.startswith("rejected"):
        return State.REJECTED, 0.0, []
    if out in ("cancelled", "cancelled_unfilled") or "cancel" in out:
        return State.CANCELLED, frac, incr
    return None, frac, incr


def build_script(run: LC.Run, o: dict, stream: list[dict]) -> tuple[list[Event], dict]:
    """Events in LOCAL ARRIVAL ORDER for one order, plus counters about the mapping."""
    t = o.get("t") or {}
    qty = float(o.get("qty") or 0) or 1.0
    send = t["send_wall_ns"] / 1e9
    ev: list[Event] = [Event(Kind.SUBMIT, 0.0, 0.0, source="historical")]
    info = Counter()
    tif = "PostOnly" if run.market == "bybit_linear" else "GTX"
    execs = [x for x in (o.get("execs") or [])]
    exec_cum, execs_iter = 0.0, iter(sorted(execs, key=lambda x: x["recv_wall_ns"]))
    cancel_at = (t["csend_wall_ns"] / 1e9 - send) if t.get("csend_wall_ns") else None
    items: list[tuple[float, str, dict]] = []
    if o.get("outcome", "").startswith("rejected_on_rest"):
        code = str(o.get("reject_code") or o.get("reject_reason") or "")
        items.append((t["rest_wall_ns"] / 1e9 - send, "rest_reject", {"code": code}))
    for e in stream:
        items.append((e["recv_wall_ns"] / 1e9 - send, "stream", e))
    if cancel_at is not None:
        items.append((cancel_at, "cancel_req", {}))
    items.sort(key=lambda x: (x[0], 0 if x[1] == "cancel_req" else 1))
    cancel_requested, cum_seen = False, 0.0
    for at, kind, e in items:
        if kind == "cancel_req":
            ev.append(Event(Kind.CANCEL_REQUEST, at, at, source="historical"))
            cancel_requested = True
            continue
        if kind == "rest_reject":
            m = map_rest_reject(run.market, e["code"])
            ev.append(Event(Kind.REJECT, at, at, source="historical"))
            info[f"rest:{m.label}"] += 1
            continue
        exm = (e.get("ex_event_ms") or 0) / 1000.0
        m = map_stream_update(run.market, e["status"], e.get("reason", ""), tif=tif, cancel_requested=cancel_requested, cum_filled=cum_seen)
        info[f"stream:{m.label}"] += 1
        ts = at
        if m.kind == Kind.ACK:
            ev.append(Event(Kind.ACK, ts, at, source="historical"))
        elif m.kind == Kind.REJECT:
            ev.append(Event(Kind.REJECT, ts, at, source="historical"))
        elif m.kind == Kind.CANCEL_CONFIRM:
            ev.append(Event(Kind.CANCEL_CONFIRM, ts, at, cum=cum_seen, source="historical"))
        elif m.kind == Kind.FILL:
            x = next(execs_iter, None)
            if x is not None:
                exec_cum += float(x["qty"])
            cum_seen = 1.0 if m.label == "fill" else min(exec_cum / qty, 1.0)
            if m.label == "fill" and abs(exec_cum - qty) > max(1e-6, 1e-6 * qty):
                info["fill_total_differs_from_order_qty"] += 1
            ev.append(Event(Kind.FILL, ts, at, cum=cum_seen, source="historical"))
    return ev, dict(info)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logger-root", required=True)
    ap.add_argument("--out-dir", default="../out/perp_lifecycle")
    a = ap.parse_args(argv)
    from parity_adapters import run_oms, run_spec, NotRepresentable
    runs = LC.discover(Path(a.logger_root))
    inv = {r.id: LC.run_inventory(r) for r in runs}
    # ---- Task 3: every observed native status -> mapping (or unmapped)
    seen = Counter()
    unmapped = Counter()
    for r in runs:
        for e in r.events:
            if e.get("type") == "ws_event":
                seen[(r.market, "stream", str(e.get("status")), str(e.get("reason") or ""))] += 1
        for o in r.orders:
            c = o.get("reject_code") or (o.get("reject_reason") if o.get("outcome") == "rejected_on_rest" else None)
            if c:
                seen[(r.market, "rest", "REST", str(c))] += 1
    observed_rows = []
    for (market, ch, status, reason), n in sorted(seen.items()):
        try:
            m = map_rest_reject(market, reason) if ch == "rest" else map_stream_update(market, status, reason, tif="PostOnly" if market == "bybit_linear" else "GTX")
            observed_rows.append({"market": market, "channel": ch, "native_status": status, "reason_or_code": reason, "count": n, "maps_to": m.kind.value, "label": m.label, "basis": m.basis})
        except UnmappedVenueStatus as ex:
            unmapped[(market, ch, status, reason)] += n
            observed_rows.append({"market": market, "channel": ch, "native_status": status, "reason_or_code": reason, "count": n, "maps_to": ex.code, "label": None, "basis": "UNMAPPED"})
    from new_backtest.venue_semantics_perp import mapping_table
    sem = {"observed": observed_rows, "unmapped_observed": [dict(zip(("market", "channel", "status", "reason"), k), count=v) for k, v in unmapped.items()],
           "table": mapping_table(), "networks": sorted({r.network for r in runs}), "label": "TESTNET message shapes"}
    # ---- Task 4: replay
    by_venue = defaultdict(lambda: Counter())
    causes = defaultdict(Counter)
    patterns = defaultdict(Counter)
    increments = defaultdict(lambda: Counter())
    excluded = Counter()
    for r in runs:
        cls = inv[r.id]["classification"]
        if not ("USABLE_FOR_LATENCY" in cls or "USABLE_FOR_REJECTS" in cls):
            excluded[(r.market, "run not usable (PLUMBING_ONLY)")] += len(r.orders)
            continue
        stream_by = defaultdict(list)
        for e in r.events:
            if e.get("type") == "ws_event" and e.get("matched"):
                stream_by[e["order_id"]].append(e)
        for o in r.orders:
            fin, booked_x, incr_x = exchange_final(o)
            if fin is None or not (o.get("t") or {}).get("send_wall_ns"):
                excluded[(r.market, "exchange final state not recorded (probe rested / no send stamp)")] += 1
                continue
            try:
                script, info = build_script(r, o, sorted(stream_by.get(o["order_id"], []), key=lambda e: e["recv_wall_ns"]))
            except UnmappedVenueStatus as ex:
                by_venue[r.market]["unmapped_raise"] += 1
                causes[r.market][f"unmapped status raised {ex.code}"] += 1
                continue
            try:
                oms = run_oms(script)
            except NotRepresentable as ex:
                by_venue[r.market]["not_representable"] += 1
                causes[r.market][str(ex)[:80]] += 1
                continue
            except Exception as ex:                                                  # OMS refused the sequence: a finding, counted by type
                by_venue[r.market]["oms_exception"] += 1
                causes[r.market][f"OMS raised {type(ex).__name__}: {str(ex)[:60]}"] += 1
                continue
            spec = run_spec(script)
            n = by_venue[r.market]
            n["orders"] += 1
            ok_state = oms.state == fin.value
            ok_qty = abs(oms.booked - booked_x) < 1e-4
            ok_inc = len(oms.fills) == len(incr_x) and all(abs(a - b) < 1e-4 for a, b in zip(oms.fills, incr_x)) if incr_x else (len(oms.fills) == 0 or abs(sum(oms.fills) - booked_x) < 1e-4)
            n["state_agree"] += ok_state
            n["booked_agree"] += ok_qty
            n["increments_agree"] += ok_inc
            n["all_agree"] += (ok_state and ok_qty and ok_inc)
            n["spec_equals_oms"] += (spec.state == oms.state and abs(spec.booked - oms.booked) < 1e-9)
            pat = "->".join(e.kind.value for e in script)
            patterns[r.market][pat] += 1
            if not (ok_state and ok_qty and ok_inc):
                why = []
                if not ok_state:
                    why.append(f"state: OMS {oms.state} vs exchange {fin.value}")
                if not ok_qty:
                    why.append("booked qty differs")
                if not ok_inc:
                    why.append("fill increments differ")
                causes[r.market]["; ".join(why) + f" | events {pat}"] += 1
            for k, v in info.items():
                increments[r.market][k] += v
    result = {"label": "historical events only (source='historical'); TESTNET logs from the Mac", "per_venue": {}, "excluded_orders": {f"{k[0]}: {k[1]}": v for k, v in excluded.items()}}
    for mkt, n in by_venue.items():
        tot = n["orders"]
        result["per_venue"][mkt] = {**dict(n), "agreement_pct": (100.0 * n["all_agree"] / tot) if tot else None,
                                    "state_agreement_pct": (100.0 * n["state_agree"] / tot) if tot else None,
                                    "event_sequences": dict(patterns[mkt].most_common(12)),
                                    "mapped_event_counts": dict(increments[mkt]),
                                    "disagreements_by_cause": dict(causes[mkt])}
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "message_mapping.json").write_text(json.dumps(sem, indent=1, default=str))
    (out / "oms_replay.json").write_text(json.dumps(result, indent=1, default=str))
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk in ("orders", "all_agree", "agreement_pct", "state_agree", "unmapped_raise", "oms_exception", "not_representable")} for k, v in result["per_venue"].items()}, indent=1))
    print("excluded:", result["excluded_orders"])
    print("unmapped observed:", sem["unmapped_observed"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
