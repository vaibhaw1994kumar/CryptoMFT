#!/usr/bin/env python3
"""Perpetual order-lifecycle inventory (Task 1) and calibration (Task 2) from the ollog perp order-logger runs.

    python3 -I scripts/lifecycle_calibration_perp.py --logger-root ../order_lifecycle_logger \
        --out-dir config --inventory-out out/perp_lifecycle/inventory.json

Writes aggregates only: no order ids, client ids, prices or API responses. Reads <logger-root>/latency_logs/<run>/ and
<logger-root>/fillprobe_runs/<run>/<venue_run>/ (orders.jsonl, events.jsonl, meta.json) plus fillprobe capture/ (public book + trades).

What these runs are, and are not
  * ollog places a few hundred small orders per venue on the venue TESTNET from the machine running the logger (here: the Mac, not the VM)
    and records local and exchange timestamps. Every latency below is Mac -> testnet. Nothing here is a mainnet or VM -> mainnet latency.
  * 'normal' orders in latency_logs rest far from the touch and are cancelled; they never fill. 'reject_probe' orders are priced to CROSS on purpose,
    so their reject fraction is a property of the probe, not a PostOnly reject rate. 'fill_probe' orders (fillprobe_runs) are PostOnly/GTX orders
    priced to rest at or near the touch; some fill. Only those give a natural PostOnly outcome, and only under the logger's pricing policy.
  * Cross-clock quantities (anything using an exchange timestamp) carry the logger's own clock-offset estimate (5 samples of exchange server time vs the local
    clock, uncertainty ~85-100 ms, offset between runs 26..234 ms). That is NOT NTP/chrony. Raw values, the offset and the corrected value are
    kept in separate fields; the offset is the sample taken for that order's run segment, never a rolling median.
  * Percentiles are published only where n >= 30 (p99 only where n >= 100); smaller cells list n only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

SCHEMA = "lifecycle_calibration_perp/v1"
PRODUCT = "perp"
MIN_N = {"p50": 30, "p90": 30, "p99": 100}
MIN_N_CELL = 30
LATENCY_RUN_MIN_CLEAN = 10          # a run with fewer clean orders is PLUMBING_ONLY for latency
QGRID = [i / 100 for i in range(100)] + [0.995, 0.999, 1.0]
VENUES = {"bybit_linear": "bybit", "binance_futures": "binance"}
PRODUCT_NAME = {"bybit_linear": "linear_perp", "binance_futures": "usdm_perp"}


# ------------------------------------------------------------------ statistics helpers
def sha256_file(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for ch in iter(lambda: fh.read(1 << 20), b""):
            h.update(ch)
    return h.hexdigest()


def wilson(k: int, n: int, z: float = 1.959964) -> list | None:
    if n <= 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [max(0.0, c - h), min(1.0, c + h)]


def quant(xs: list[float], q: float) -> float:
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def describe(xs: list[float], unit: str = "ms") -> dict:
    """Summary with the publication gate: p50/p90 need n>=30, p99 needs n>=100. Below that only n (and min/max)."""
    xs = [x for x in xs if x is not None and math.isfinite(x)]
    n = len(xs)
    out: dict = {"n": n, "unit": unit}
    if n == 0:
        return out
    out["min"], out["max"], out["mean"] = min(xs), max(xs), sum(xs) / n
    out["negative_fraction"] = sum(1 for x in xs if x < 0) / n
    for k, q in (("p50", .5), ("p90", .9), ("p99", .99)):
        out[k] = quant(xs, q) if n >= MIN_N[k] else None
    if n < MIN_N["p50"]:
        out["note"] = f"n={n} < {MIN_N['p50']}: percentiles withheld"
    elif n < MIN_N["p99"]:
        out["note"] = f"n={n} < {MIN_N['p99']}: p99 withheld"
    return out


def grid(xs: list[float]) -> dict | None:
    xs = [x for x in xs if x is not None and math.isfinite(x)]
    if len(xs) < MIN_N["p99"]:
        return None
    return {"q": QGRID, "ms": [quant(xs, q) for q in QGRID], "n": len(xs)}


def utc(s: float) -> str:
    return dt.datetime.fromtimestamp(s, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ loading
class Run:
    def __init__(self, run_id: str, kind: str, path: Path, capture: Path | None, net_hint: str | None):
        self.id, self.kind, self.path, self.capture = run_id, kind, path, capture
        self.meta = json.loads((path / "meta.json").read_text()) if (path / "meta.json").exists() else {}
        self.orders = [json.loads(l) for l in open(path / "orders.jsonl")] if (path / "orders.jsonl").exists() else []
        self.events = [json.loads(l) for l in open(path / "events.jsonl")] if (path / "events.jsonl").exists() else []
        first = (self.orders[0].get("market") if self.orders else None) or run_id.rsplit("_", 1)[0].rsplit("_", 1)[0]
        self.market = self.orders[0]["market"] if self.orders else self.meta.get("market") or first
        self.network = self._network(net_hint)
        self.symbols = sorted({o["symbol"] for o in self.orders if o.get("symbol")})

    def _network(self, hint):
        if hint:
            return hint
        hosts = " ".join(str(self.meta.get(k)) for k in ("rest", "ws"))
        if "testnet" in hosts or "binancefuture.com" in hosts and "fstream.binance.com" not in hosts:
            return "testnet"
        return "mainnet" if hosts.strip() else "unknown"

    @property
    def venue(self) -> str:
        return VENUES.get(self.market, self.market)

    def files_sha(self) -> dict:
        return {f: sha256_file(self.path / f) for f in ("orders.jsonl", "events.jsonl", "meta.json") if (self.path / f).exists()}


def discover(root: Path) -> list[Run]:
    runs: list[Run] = []
    for d in sorted((root / "latency_logs").glob("*")):
        if d.name.startswith(("bybit_linear", "binance_futures")) and (d / "meta.json").exists():
            runs.append(Run(d.name, "latency_log", d, None, None))
    for fp in sorted((root / "fillprobe_runs").glob("fillprobe_*")):
        meta = json.loads((fp / "fillprobe_meta.json").read_text()) if (fp / "fillprobe_meta.json").exists() else {}
        net = meta.get("network")
        cap = fp / "capture" if (fp / "capture").is_dir() else None
        for d in sorted(fp.glob("*_2*")):
            if d.is_dir() and d.name.startswith(("bybit_linear", "binance_futures")):
                r = Run(f"{fp.name}/{d.name}", "fillprobe", d, cap, net)
                r.capture_only = bool(meta.get("capture_only"))
                r.fp_meta = meta
                runs.append(r)
    # capture-only mainnet runs (no orders) are listed by --extra-capture-only dirs in main()
    return runs


# ------------------------------------------------------------------ per-order metrics
def ms(ns):
    return None if ns is None else ns / 1e6


def order_metrics(o: dict) -> dict:
    t, e, c = o.get("t") or {}, o.get("ex") or {}, o.get("clock") or {}
    off = c.get("offset_ms")
    unc = c.get("uncertainty_ms")
    m: dict = {"host_offset_ms": off, "host_offset_uncertainty_ms": unc}
    sw, sm = t.get("send_wall_ns"), t.get("send_mono_ns")
    if sm is not None and t.get("rest_mono_ns") is not None:
        m["ack_rest_rtt_ms"] = (t["rest_mono_ns"] - sm) / 1e6                          # local clock only
    if sm is not None and t.get("wsnew_mono_ns") is not None:
        m["ack_ws_new_local_ms"] = (t["wsnew_mono_ns"] - sm) / 1e6                     # local clock only
    if sm is not None and t.get("wsrej_mono_ns") is not None:
        m["reject_ws_local_ms"] = (t["wsrej_mono_ns"] - sm) / 1e6
    ev_new = e.get("wsnew_event_ms") or e.get("created_ms")
    if sw is not None and ev_new:
        m["entry_raw_ms"] = ev_new - sw / 1e6                                          # exchange create/new event - local send (cross-clock)
        if off is not None:
            m["entry_offset_corrected_ms"] = m["entry_raw_ms"] - off
    if "ack_rest_rtt_ms" in m:
        m["entry_half_rest_rtt_ms"] = m["ack_rest_rtt_ms"] / 2                         # symmetric-path assumption, local clock only
    if e.get("wsnew_event_ms") and t.get("wsnew_wall_ns") is not None:
        m["report_delay_new_raw_ms"] = t["wsnew_wall_ns"] / 1e6 - e["wsnew_event_ms"]   # local recv - exchange event (cross-clock)
        if off is not None:
            m["report_delay_new_corrected_ms"] = m["report_delay_new_raw_ms"] + off
    if t.get("wscan_wall_ns") is not None and e.get("wscan_event_ms"):
        m["report_delay_cancel_raw_ms"] = t["wscan_wall_ns"] / 1e6 - e["wscan_event_ms"]
        if off is not None:
            m["report_delay_cancel_corrected_ms"] = m["report_delay_cancel_raw_ms"] + off
    if sm is not None and False:
        pass
    cm = t.get("csend_mono_ns")
    if cm is not None and t.get("crest_mono_ns") is not None:
        m["cancel_rest_rtt_ms"] = (t["crest_mono_ns"] - cm) / 1e6
    if cm is not None and t.get("wscan_mono_ns") is not None:
        m["cancel_send_to_ws_cancelled_local_ms"] = (t["wscan_mono_ns"] - cm) / 1e6
    if t.get("csend_wall_ns") is not None and e.get("wscan_event_ms"):
        m["cancel_arrival_raw_ms"] = e["wscan_event_ms"] - t["csend_wall_ns"] / 1e6     # exchange cancel event - local cancel send (cross-clock)
        if off is not None:
            m["cancel_arrival_corrected_ms"] = m["cancel_arrival_raw_ms"] - off
    if cm is not None and sm is not None:
        m["place_to_cancel_send_hold_ms"] = (cm - sm) / 1e6
    # fills (private report): local receipt - exchange fill time, per exec
    fills = []
    for x in o.get("execs") or []:
        if x.get("ex_ms") and x.get("recv_wall_ns") is not None:
            raw = x["recv_wall_ns"] / 1e6 - x["ex_ms"]
            fills.append({"raw": raw, "corrected": raw + off if off is not None else None})
    m["_fills"] = fills
    return m


def collect(runs: list[Run]) -> dict:
    """{(venue_market, network): {'orders': [(run, order, metrics)], 'runs': [...]}}"""
    cells: dict = defaultdict(lambda: {"rows": [], "runs": []})
    for r in runs:
        k = (r.market, r.network)
        cells[k]["runs"].append(r)
        for o in r.orders:
            cells[k]["rows"].append((r, o, order_metrics(o)))
    return cells


# ------------------------------------------------------------------ inventory (Task 1)
def run_inventory(r: Run) -> dict:
    oc = Counter((o.get("kind"), o.get("outcome")) for o in r.orders)
    send = [o["t"]["send_wall_ns"] / 1e9 for o in r.orders if (o.get("t") or {}).get("send_wall_ns")]
    keys = lambda f: sum(1 for o in r.orders if f(o))                                  # noqa: E731
    has = {
        "local_send_ts": keys(lambda o: (o.get("t") or {}).get("send_wall_ns") is not None),
        "rest_ack_ts": keys(lambda o: (o.get("t") or {}).get("rest_wall_ns") is not None),
        "private_stream_new_ts": keys(lambda o: (o.get("t") or {}).get("wsnew_wall_ns") is not None),
        "exchange_create_ts": keys(lambda o: (o.get("ex") or {}).get("wsnew_event_ms") or (o.get("ex") or {}).get("created_ms")),
        "cancel_send_ts": keys(lambda o: (o.get("t") or {}).get("csend_wall_ns") is not None),
        "cancel_rest_ack_ts": keys(lambda o: (o.get("t") or {}).get("crest_wall_ns") is not None),
        "cancel_stream_ack_ts": keys(lambda o: (o.get("t") or {}).get("wscan_wall_ns") is not None),
        "fill_reports": keys(lambda o: o.get("execs")),
        "fill_reports_with_exec_id": 0,                                                # the logger stores ex time, price, qty, recv time; no exec id
        "reject_reason_code": keys(lambda o: o.get("reject_reason") or o.get("reject_code")),
        "best_bid_ask_at_send": keys(lambda o: (o.get("bid_at_send") or o.get("best_bid_at_send")) is not None),
        "top5_book_at_send": keys(lambda o: o.get("top5_at_send")),
    }
    codes = Counter(str(o.get("reject_reason") or o.get("reject_code")) for o in r.orders if o.get("reject_reason") or o.get("reject_code"))
    evt = Counter(e.get("type") for e in r.events)
    clocks = [e for e in r.events if e.get("type") == "clock_sync"] or []
    offs = [e["offset_ms"] for e in clocks]
    unc = [e["uncertainty_ms"] for e in clocks]
    if not clocks:                                                                      # per-order stamp
        offs = sorted({round(o["clock"]["offset_ms"], 3) for o in r.orders if o.get("clock")})
        unc = sorted({round(o["clock"]["uncertainty_ms"], 3) for o in r.orders if o.get("clock")})
    clean = sum(1 for o in r.orders if o.get("clean") and o.get("kind") != "reject_probe")
    n_fill = sum(1 for o in r.orders if o.get("outcome") == "filled")
    n_rej = sum(1 for o in r.orders if o.get("kind") == "reject_probe" and o.get("post_only_reject"))
    cap = None
    if r.capture is not None:
        cap = {"book_events": any((r.capture / r.venue / "book_events").glob("**/*.gz")) if (r.capture / r.venue / "book_events").exists() else False,
               "book_snapshots": any((r.capture / r.venue / "book_snapshots").glob("**/*.gz")) if (r.capture / r.venue / "book_snapshots").exists() else False,
               "trades": any((r.capture / r.venue / "trades").glob("**/*.gz")) if (r.capture / r.venue / "trades").exists() else False,
               "capture_path_shared_with_other_venue_run": True}
    # classification (non-exclusive)
    cls = []
    if clean >= LATENCY_RUN_MIN_CLEAN or (r.kind == "fillprobe" and len(r.orders) >= LATENCY_RUN_MIN_CLEAN):
        cls.append("USABLE_FOR_LATENCY")
    if n_rej or any(o.get("outcome") in ("rejected_on_rest", "rejected_on_stream") for o in r.orders):
        cls.append("USABLE_FOR_REJECTS")
    if cap and cap["book_events"] and n_rej > 0:
        cls.append("USABLE_FOR_TRUE_REJECT_VALIDATION")
    if not cls:
        cls.append("PLUMBING_ONLY")
    elif "USABLE_FOR_LATENCY" not in cls and "USABLE_FOR_REJECTS" not in cls:
        cls.append("PLUMBING_ONLY")
    if len(r.orders) < LATENCY_RUN_MIN_CLEAN and "PLUMBING_ONLY" not in cls:
        cls.append("PLUMBING_ONLY")
    return {
        "run_id": r.id, "source_kind": r.kind, "venue": r.venue, "market": r.market, "product": PRODUCT_NAME.get(r.market), "network": r.network,
        "label_note": "TESTNET: message semantics, and a bound only for mainnet latency" if r.network == "testnet" else "MAINNET",
        "logger_host": "Mac (the machine that ran ollog); NOT the VM", "symbols": r.symbols,
        "first_send_utc": utc(min(send)) if send else None, "last_send_utc": utc(max(send)) if send else None,
        "duration_s": (max(send) - min(send)) if send else None,
        "orders": len(r.orders), "by_kind_outcome": {f"{k[0]}/{k[1]}": v for k, v in sorted(oc.items(), key=str)},
        "clean_orders": clean, "filled_orders": n_fill, "reject_probe_rejects": n_rej,
        "events_present": has, "reject_reason_codes": dict(codes), "event_types": dict(evt),
        "source": "REST ack + private WebSocket stream (both timestamped locally)",
        "concurrent_public_capture": cap, "classification": cls,
        "clock": {"source": "the logger's own clock_sync (exchange server time vs local clock, 5 samples); no NTP/chrony record exists for the logger host",
                  "n_syncs": len(clocks) or None, "offset_ms_range": [min(offs), max(offs)] if offs else None,
                  "uncertainty_ms_range": [min(unc), max(unc)] if unc else None},
        "input_sha256": r.files_sha(),
    }


def capture_only_runs(vm_listing: list[dict]) -> list[dict]:
    return vm_listing


# ------------------------------------------------------------------ calibration (Task 2)
def collect_metric(rows, key) -> list[float]:
    return [m[key] for (_, _, m) in rows if m.get(key) is not None]


def per_symbol(rows, key, min_n=MIN_N_CELL) -> dict:
    by = defaultdict(list)
    for (_, o, m) in rows:
        if m.get(key) is not None:
            by[o.get("symbol")].append(m[key])
    return {s: (describe(v) if len(v) >= min_n else {"n": len(v), "note": f"n<{min_n}: listed with n only"}) for s, v in sorted(by.items())}


LAT_METRICS = [
    ("ack_rest_rtt_ms", "ack round trip: local send -> REST response, local monotonic clock only"),
    ("ack_ws_new_local_ms", "local send -> private-stream 'New', local monotonic clock only"),
    ("entry_half_rest_rtt_ms", "entry (arrival) estimate = REST round trip / 2, local clock only, assumes a symmetric path"),
    ("entry_raw_ms", "entry (arrival): exchange create/new event time - local send wall time. RAW, includes the host clock offset"),
    ("entry_offset_corrected_ms", "entry (arrival): raw minus the logger's clock-offset sample for this run segment (uncertainty ~85-100 ms)"),
    ("report_delay_new_raw_ms", "private report delay for acks: local receipt - exchange event time (RAW, cross-clock)"),
    ("report_delay_new_corrected_ms", "private report delay for acks, offset-corrected"),
    ("report_delay_cancel_raw_ms", "private report delay for cancel reports: local receipt - exchange event time (RAW)"),
    ("report_delay_cancel_corrected_ms", "private report delay for cancel reports, offset-corrected"),
    ("cancel_rest_rtt_ms", "cancel round trip: cancel send -> REST response, local clock only"),
    ("cancel_send_to_ws_cancelled_local_ms", "cancel send -> private-stream 'Cancelled', local clock only"),
    ("cancel_arrival_raw_ms", "cancel arrival: exchange cancel event - local cancel send (RAW, cross-clock)"),
    ("cancel_arrival_corrected_ms", "cancel arrival, offset-corrected"),
]


def fill_delays(rows) -> dict:
    raw = [f["raw"] for (_, _, m) in rows for f in m["_fills"]]
    cor = [f["corrected"] for (_, _, m) in rows for f in m["_fills"] if f["corrected"] is not None]
    return {"report_delay_fill_raw_ms": describe(raw), "report_delay_fill_corrected_ms": describe(cor)}


def public_print_comparison(runs: list[Run]) -> dict:
    """Private fill report vs the public trade print of the same trade (fillprobe runs with a concurrent trade capture).

    Matching: same symbol, same price, same quantity, exchange timestamps within 250 ms of the private fill's exchange time. Both receipts are
    on the same local clock, so (private receipt - public receipt) needs no offset."""
    out = {"pairs": 0, "private_minus_public_receipt_ms": [], "unmatched_fills": 0}
    for r in runs:
        if r.capture is None or r.kind != "fillprobe":
            continue
        prints = defaultdict(list)
        base = r.capture / r.venue / "trades"
        if not base.exists():
            continue
        for f in base.glob("**/*.jsonl.gz"):
            for l in gzip.open(f, "rt"):
                try:
                    t = json.loads(l)
                except Exception:
                    continue
                prints[t["pair"]].append(t)
        for o in r.orders:
            for x in o.get("execs") or []:
                try:
                    pr, q = float(x["price"]), float(x["qty"])
                except Exception:
                    continue
                best = None
                for t in prints.get(o["symbol"], []):
                    if abs(t["price"] - pr) < 1e-12 and abs(t["volume"] - q) < 1e-9 and abs(t["exchange_ts"] * 1000 - x["ex_ms"]) <= 250:
                        d = x["recv_wall_ns"] / 1e6 - t["local_ts"] * 1000
                        if best is None or abs(t["exchange_ts"] * 1000 - x["ex_ms"]) < best[0]:
                            best = (abs(t["exchange_ts"] * 1000 - x["ex_ms"]), d)
                if best:
                    out["pairs"] += 1
                    out["private_minus_public_receipt_ms"].append(best[1])
                else:
                    out["unmatched_fills"] += 1
    xs = out.pop("private_minus_public_receipt_ms")
    out["private_minus_public_receipt"] = describe(xs)
    out["note"] = "same local clock for both receipts (offset-free). Matching on symbol+price+qty+exchange time within 250 ms; unmatched fills are counted"
    return out


def reject_section(rows, runs) -> dict:
    probes = [(r, o, m) for (r, o, m) in rows if o.get("kind") == "reject_probe"]
    genuine = [(r, o, m) for (r, o, m) in probes if o.get("post_only_reject")]
    other_ref = [(r, o, m) for (r, o, m) in probes if o.get("outcome", "").startswith("rejected") and not o.get("post_only_reject")]
    rested = [(r, o, m) for (r, o, m) in probes if o.get("outcome") == "cancel_failed"]
    codes = Counter(str(o.get("reject_reason") or o.get("reject_code")) for (_, o, _) in genuine)
    codes_other = Counter(str(o.get("reject_reason") or o.get("reject_code")) + " | " + (o.get("reject_msg") or "")[:60] for (_, o, _) in other_ref)
    channel = Counter(o.get("outcome") for (_, o, _) in genuine)
    lat_rest = [m["ack_rest_rtt_ms"] for (_, _, m) in genuine if m.get("ack_rest_rtt_ms") is not None]
    lat_ws = [m["reject_ws_local_ms"] for (_, _, m) in genuine if m.get("reject_ws_local_ms") is not None]
    valid = len(genuine) + len(rested)                       # probes that were valid orders: they either were PostOnly-rejected or rested
    natural = [(r, o, m) for (r, o, m) in rows if o.get("kind") == "fill_probe"]
    nat_rej = sum(1 for (_, o, _) in natural if o.get("outcome") in ("rejected_on_rest", "rejected_on_stream") or o.get("post_only_reject"))
    by_sym = defaultdict(lambda: [0, 0])
    for (_, o, _) in natural:
        by_sym[o.get("symbol")][0] += 1
        by_sym[o.get("symbol")][1] += int(o.get("outcome") in ("rejected_on_rest", "rejected_on_stream"))
    return {
        "reject_probes": {
            "definition": "orders priced to CROSS the book on purpose; they exist to capture the venue's reject message shape. The fraction rejected measures the probe, NOT a PostOnly reject rate",
            "n_sent": len(probes), "postonly_rejects": len(genuine), "refused_for_another_reason": len(other_ref), "rested_instead_of_rejecting": len(rested),
            "postonly_reject_codes": dict(codes), "postonly_reject_channel": dict(channel), "other_refusals": dict(codes_other),
            "valid_probes_rejected_fraction": (len(genuine) / valid) if valid else None, "valid_probes_rejected_wilson95": wilson(len(genuine), valid),
            "valid_probes_rejected_fraction_label": "probe_cross_reject_fraction (designed-to-cross probes that were valid orders): NOT a PostOnly reject rate",
            "reject_latency_rest_rtt_ms": describe(lat_rest), "reject_latency_stream_local_ms": describe(lat_ws),
        },
        "natural_postonly": {
            "definition": "fill_probe orders (PostOnly/GTX) priced to rest at or near the touch by the logger's own policy; a reject here is a real PostOnly would-cross event under that pricing",
            "n_orders": len(natural), "n_rejects": nat_rej,
            "reject_rate": (nat_rej / len(natural)) if natural else None, "wilson95": wilson(nat_rej, len(natural)),
            "rule_of_three_upper95": (3 / len(natural)) if natural and nat_rej == 0 else None,
            "by_symbol": {s: {"n": n, "rejects": k, "published": n >= MIN_N_CELL, "wilson95": wilson(k, n) if n >= MIN_N_CELL else None} for s, (n, k) in sorted(by_sym.items())},
            "by_hour_utc": "withheld: cells with n < 30; the runs span a few hours (see inventory)",
            "caveat": "orders were placed to rest; the strategy quotes at/near the touch, so the strategy's real PostOnly reject rate can be higher",
        },
    }


def races_and_disconnects(rows, runs) -> dict:
    fills_after_cancel = 0
    cancel_failed = 0
    partial_then_cancel = 0
    filled_with_cancel_attempt = 0
    for (_, o, _) in rows:
        c = o.get("cancel")
        if o.get("outcome") == "filled" and c is not None:
            filled_with_cancel_attempt += 1
            if c.get("ok") is False:
                fills_after_cancel += 1
        if o.get("outcome") == "cancel_failed" or (c is not None and c.get("ok") is False):
            cancel_failed += 1
    reconnects = 0
    for r in runs:
        reconnects += sum(1 for e in r.events if str(e.get("type", "")).startswith("stream_reconnect"))
        notes = (r.capture.parent / "capture_notes.jsonl") if r.capture is not None else None
        if notes and notes.exists():
            reconnects += sum(1 for l in open(notes) if '"reconnect"' in l)
    return {"orders_filled_that_also_had_a_cancel_request": filled_with_cancel_attempt, "cancel_requests_that_failed": cancel_failed,
            "fill_after_cancel_request_races_observed": fills_after_cancel,
            "stream_or_capture_reconnect_events": reconnects,
            "note": "counts only; a race is counted when an order that filled also carried a cancel request that did not succeed"}


def calibrate(market: str, network: str, cell: dict) -> dict:
    rows, runs = cell["rows"], cell["runs"]
    normal_like = [x for x in rows if x[1].get("kind") in ("normal", "fill_probe") and x[1].get("outcome") not in ("rejected_on_rest", "cancel_failed")]
    out = {"venue": VENUES.get(market, market), "market": market, "product": PRODUCT_NAME.get(market), "network": network,
           "orders": len(rows), "runs": [r.id for r in runs],
           "host_clock": {"source": "logger clock_sync (5 samples of exchange server time vs the Mac clock); not NTP/chrony",
                          "offset_ms": describe([m["host_offset_ms"] for (_, _, m) in rows if m.get("host_offset_ms") is not None]),
                          "uncertainty_ms": describe([m["host_offset_uncertainty_ms"] for (_, _, m) in rows if m.get("host_offset_uncertainty_ms") is not None]),
                          "note": "kept separate; raw cross-clock values are published next to the corrected ones"}}
    lat = {}
    for key, desc in LAT_METRICS:
        xs = collect_metric(normal_like, key)
        lat[key] = {"definition": desc, **describe(xs), "by_symbol": per_symbol(normal_like, key)}
    out["latency"] = lat
    out["fill_report_delay"] = fill_delays(normal_like)
    out["public_trade_print_comparison"] = public_print_comparison(runs)
    span = 0.0
    for r in runs:
        send = [o["t"]["send_wall_ns"] / 1e9 for o in r.orders if (o.get("t") or {}).get("send_wall_ns")]
        span += (max(send) - min(send)) if len(send) > 1 else 0.0
    out["observation"] = {"seconds_between_first_and_last_order_summed_over_runs": span, "runs": len(runs),
                          "note": "used only to turn 'zero disconnects seen' into a rule-of-three rate bound: 3 / observation seconds"}
    out["rejects"] = reject_section(rows, runs)
    out["cancel_fill_races_and_disconnects"] = races_and_disconnects(rows, runs)
    return out


def sampling_grid(cal: dict) -> dict:
    """Quantile grids in the layout AckLatencySource reads (ack_latency_proxy.empirical_quantile_grids[period] -> q, ms, n)."""
    return {}


def content_hash(doc: dict) -> str:
    body = {k: v for k, v in doc.items() if k != "meta"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def build_config(venue_market: str, network_cells: dict, inventory_runs: list[dict], script_path: Path) -> dict:
    nets = {}
    grids = {}
    for (market, net), cell in network_cells.items():
        if market != venue_market:
            continue
        cal = calibrate(market, net, cell)
        nets[net] = cal
        g_src = [m for (_, o, m) in cell["rows"] if o.get("kind") in ("normal", "fill_probe") and o.get("outcome") not in ("rejected_on_rest", "cancel_failed")]
        grids[net] = {k: grid([m[k] for m in g_src if m.get(k) is not None]) for k in ("entry_half_rest_rtt_ms", "entry_raw_ms", "entry_offset_corrected_ms",
                                                                                         "ack_rest_rtt_ms", "cancel_rest_rtt_ms", "cancel_send_to_ws_cancelled_local_ms")}
    doc = {
        "schema": SCHEMA, "product": PRODUCT, "venue": VENUES.get(venue_market, venue_market), "market": venue_market,
        "version": 1,
        "script": {"path": "scripts/lifecycle_calibration_perp.py", "sha256": sha256_file(script_path)},
        "min_sample_sizes": {"percentiles": MIN_N, "cell": MIN_N_CELL},
        "provenance": {"logger_host": "Mac (not the VM)", "networks_with_data": sorted(nets),
                       "mainnet": "NO ORDER-EVENT DATA: the only mainnet runs are capture-only (public book/trades, no orders); no mainnet table is published" if "mainnet" not in nets else "see networks.mainnet",
                       "source_runs": [r for r in inventory_runs if r["market"] == venue_market],
                       "label": "TESTNET: message semantics; latency is an indicative bound for mainnet, not a measurement of it"},
        "networks": nets,
        "empirical_quantile_grids_by_network": {n: {k: v for k, v in gs.items() if v} for n, gs in grids.items()},
        "ack_latency_proxy": {"definition": "AckLatencySource-compatible block. grid 'testnet_half_rest_rtt' = entry arrival estimated as REST round trip / 2 (local clock only). "
                                            "The raw and offset-corrected one-way grids are in empirical_quantile_grids_by_network.",
                              "empirical_quantile_grids": {k: g for k, g in (("testnet_half_rest_rtt", grids.get("testnet", {}).get("entry_half_rest_rtt_ms")),
                                                                              ("all", grids.get("testnet", {}).get("entry_half_rest_rtt_ms"))) if g}},
    }
    doc["meta"] = {"generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "content_sha256": content_hash(doc),
                   "content_sha256_covers": "every key except meta"}
    return doc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logger-root", required=True)
    ap.add_argument("--out-dir", default="config")
    ap.add_argument("--inventory-out", default="out/perp_lifecycle/inventory.json")
    ap.add_argument("--extra-inventory", default=None, help="JSON list of capture-only run descriptors (e.g. VM mainnet runs) to append to the inventory")
    a = ap.parse_args(argv)
    root = Path(a.logger_root)
    runs = discover(root)
    inv = [run_inventory(r) for r in runs]
    extra = json.loads(Path(a.extra_inventory).read_text()) if a.extra_inventory else []
    cells = collect(runs)
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    for market, fname in (("bybit_linear", "lifecycle_calibration_perp_bybit.json"), ("binance_futures", "lifecycle_calibration_perp_binance.json")):
        cfg = build_config(market, cells, inv, script)
        (out_dir / fname).write_text(json.dumps(cfg, indent=1, default=str))
        print("wrote", out_dir / fname, cfg["meta"]["content_sha256"][:12])
    Path(a.inventory_out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.inventory_out).write_text(json.dumps({"runs": inv, "capture_only_runs_not_in_calibration": extra}, indent=1, default=str))
    print("inventory:", len(inv), "order-log runs +", len(extra), "capture-only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
