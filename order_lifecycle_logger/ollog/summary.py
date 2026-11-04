"""Aggregate one market's orders.jsonl into summary.json. No order ids, client ids or prices leave this module."""
from __future__ import annotations

import datetime as dt
import json
import math
from collections import Counter
from pathlib import Path

from .endpoints import LABEL

MIN_N = {"p50": 30, "p90": 30, "p99": 100}      # same publication thresholds as the Bybit mainnet calibration

CAVEAT = ("Measured on the exchange TESTNET matching engine and its gateways, from this machine's network path. It is not "
          "mainnet and is not a calibration input for the realistic preset. Buy-only, minimum-size post-only orders below the "
          "touch, one live per market, so fill-versus-cancel races are not measured here and no race rate is reported. "
          "Exchange-referenced figures carry the stated clock uncertainty and whole-millisecond exchange stamps.")


def quantiles(values: list[float]) -> dict:
    v = sorted(x for x in values if x is not None)
    out: dict = {"n": len(v), "unit": "ms"}
    if not v:
        return out
    out.update(min=round(v[0], 3), max=round(v[-1], 3), mean=round(sum(v) / len(v), 3))
    for name, q in (("p50", .5), ("p90", .9), ("p99", .99)):
        if len(v) >= MIN_N[name]:
            out[name] = round(v[min(len(v) - 1, math.ceil(q * len(v)) - 1)], 3)
        else:
            out[name] = None
    if any(out[k] is None for k in ("p50", "p90", "p99")):
        out["suppressed_below_n"] = {k: m for k, m in MIN_N.items() if out[k] is None}
    return out


def summarize(directory: Path) -> dict:
    orders = [json.loads(line) for line in (directory / "orders.jsonl").read_text().splitlines() if line.strip()]
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines() if line.strip()]
    sends = [o["t"]["send_wall_ns"] for o in orders if o["t"].get("send_wall_ns")]
    fam: dict[str, dict[str, list]] = {"local_ms": {}, "exchange_referenced_ms": {}}
    for o in orders:
        if o["outcome"] == "rejected_price_limit" or o["kind"] == "reject_probe":   # refused orders are not lifecycles
            continue
        for f in fam:
            for k, v in o["derived"][f].items():
                fam[f].setdefault(k, []).append(v)
    probes = [o for o in orders if o["kind"] == "reject_probe"]
    genuine = [o for o in probes if o.get("post_only_reject")]
    probe_doc = {"sent": len(probes), "post_only_rejected": len(genuine), "refused_for_other_reason": sum(
                     1 for o in probes if o["outcome"] == "probe_rejected_other"),
                 "rested_unexpectedly": sum(1 for o in probes if "probe_order_rested" in o["errors"]),
                 "by_symbol": dict(Counter(o["symbol"] for o in genuine)),
                 "other_reasons": dict(Counter(o.get("reject_code") or o.get("reject_reason") or "unknown"
                                               for o in probes if o["outcome"] == "probe_rejected_other")),
                 "post_only_reject_rest_rtt_ms": quantiles([o["derived"]["local_ms"]["place_rest_rtt"] for o in genuine
                                                            if o["outcome"] == "rejected_on_rest"]),
                 "post_only_reject_send_to_stream_ms": quantiles([o["derived"]["local_ms"]["place_send_to_ws_reject"] for o in genuine
                                                                  if o["outcome"] == "rejected_on_stream"])}
    syncs = [e for e in events if e["type"] == "clock_sync"]
    iso = lambda ns: dt.datetime.fromtimestamp(ns / 1e9, dt.timezone.utc).isoformat()
    doc = {
        "label": LABEL, "caveat": CAVEAT, "market": orders[0]["market"] if orders else None,
        "orders": len(orders), "by_kind": dict(Counter(o["kind"] for o in orders)),
        "outcomes": dict(Counter(o["outcome"] or "unknown" for o in orders)),
        "clean_orders": sum(1 for o in orders if o.get("clean")),
        "price_limit": {"learned": sum(1 for o in orders if o["outcome"] == "rejected_price_limit"),
                        "orders_at_learned_cap": sum(1 for o in orders if "price_cap_applied" in o)},
        "reject_probes": probe_doc,
        "error_flags": dict(Counter(e for o in orders for e in o["errors"])),
        "first_send_utc": iso(min(sends)) if sends else None, "last_send_utc": iso(max(sends)) if sends else None,
        "clock": {"syncs": len(syncs), "offset_ms": quantiles([s["offset_ms"] for s in syncs]),
                  "uncertainty_ms": quantiles([s["uncertainty_ms"] for s in syncs])},
        "latency_local_ms": {k: quantiles(v) for k, v in fam["local_ms"].items()},
        "latency_exchange_referenced_ms": {k: quantiles(v) for k, v in fam["exchange_referenced_ms"].items()},
        "stream": {"ws_events": sum(1 for e in events if e["type"] == "ws_event"),
                   "unmatched": sum(1 for e in events if e["type"] == "ws_event" and not e["matched"])},
        "run_end": [{k: e.get(k) for k in ("reason", "orders", "clean", "errors")} for e in events if e["type"] == "run_end"],
    }
    (directory / "summary.json").write_text(json.dumps(doc, indent=2, sort_keys=True))
    return doc
