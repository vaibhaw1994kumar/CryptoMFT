"""Phase D, D7d (a run wrapper, read only, no network): the audit of a recorded live paper session. Usage: audit.py SESSION_DIR OUT_JSON
Reports, from the recorder's own files: the run's span; per-venue frame counts and the gaps between consecutive BOOK DATA frames (the natural quiet of each book stream, pongs excluded) with counts above 0.5, 1 and 2 s;
the gaps between consecutive frames of any kind on the book connection (what the liveness detector sees); trade-stream gaps; sequence continuity (Bybit update ids, Binance U/u chain); connection, resync and
reconnect events from the structured log; the core's data-quality counters and defects; the time-integrity report (host-clock offset, negative latency); the book-gate suspended seconds by cause."""
import collections
import glob
import json
import sys
from pathlib import Path

sess = Path(sys.argv[1])
out = {"session": sess.name}
parts = sorted(sess.glob("part-*"))


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * q))] if v else None


def gap_stats(ts):
    g = [(b - a) / 1e6 for a, b in zip(ts, ts[1:])]
    return {"frames": len(ts), "gap_ms": {"p50": pct(g, .5), "p90": pct(g, .9), "p99": pct(g, .99), "p999": pct(g, .999), "max": max(g) if g else None},
            "gaps_over_ms": {"500": sum(1 for x in g if x > 500), "1000": sum(1 for x in g if x > 1000), "2000": sum(1 for x in g if x > 2000)}}


book_data, book_any, binance_depth, trades = collections.defaultdict(list), collections.defaultdict(list), [], collections.defaultdict(list)
first = last = None
for part in parts:
    for line in (part / "wire.jsonl").open():
        w = json.loads(line)
        t, v, ch, fd = w["receive_ts_ns"], w["venue"], w["channel"], w["frame_data"]
        first = t if first is None else min(first, t)
        last = t if last is None else max(last, t)
        if v == "BYBIT" and ch.startswith("orderbook"):
            book_any["BYBIT"].append(t)
            if '"topic":"orderbook' in fd:
                book_data["BYBIT"].append(t)
        elif v == "BINANCE" and "depth" in ch:
            book_any["BINANCE"].append(t)
            book_data["BINANCE"].append(t)
        elif "publicTrade" in ch or ch.endswith("@trade"):
            trades[v].append(t)
out["span_s"] = (last - first) / 1e9 if first else None
out["book_data_frames"] = {v: gap_stats(sorted(ts)) for v, ts in book_data.items()}
out["book_connection_any_frame"] = {v: gap_stats(sorted(ts)) for v, ts in book_any.items()}
out["trade_frames"] = {v: gap_stats(sorted(ts)) for v, ts in trades.items()}
# 10-minute bins of the natural Bybit book quiet
bins = collections.defaultdict(lambda: [0, 0.0])
ts = sorted(book_data.get("BYBIT", []))
for a, b in zip(ts, ts[1:]):
    k = int((a - first) / 6e11)
    bins[k][0] += (b - a) / 1e6 > 500
    bins[k][1] = max(bins[k][1], (b - a) / 1e6)
out["bybit_book_quiet_by_10min"] = {str(k): {"gaps_over_500ms": v[0], "max_gap_ms": v[1]} for k, v in sorted(bins.items())}
# sequence continuity from the canonical books (as recorded; the core's own gap handling is in the data-quality counters below)
seq = {"BYBIT": {"events": 0, "non_consecutive": 0, "backwards": 0, "snapshots": 0}, "BINANCE": {"events": 0, "chain_breaks": 0, "backwards": 0, "snapshots": 0}}
prev = {"BYBIT": None, "BINANCE": None}
for part in parts:
    for line in (part / "canonical.jsonl").open():
        r = json.loads(line)
        v = r["venue"]
        s = int(r["sequence"])
        st = int(r.get("sequence_start") or s)
        d = seq[v]
        d["events"] += 1
        if r["event_type"] == "BOOK_SNAPSHOT":
            d["snapshots"] += 1
        elif prev[v] is not None:
            if v == "BYBIT":
                if s < prev[v]:
                    d["backwards"] += 1
                elif s != prev[v] + 1:
                    d["non_consecutive"] += 1
            else:
                pu = r["native"].get("pu") if isinstance(r.get("native"), dict) else None      # USD-M futures: each diff names the final id of the one before it (pu)
                if s < prev[v]:
                    d["backwards"] += 1
                elif pu is None or int(pu) != prev[v]:
                    d["chain_breaks"] += 1
        prev[v] = s
out["sequence_continuity"] = seq
out["sequence_note"] = "Bybit update ids are venue ids; Binance U/u are venue ids; neither is synthetic in this recording (March 2024 sequences were SYNTHETIC)"
logs = [json.loads(line) for line in (sess / "structured_logs.jsonl").open()]
t0 = next((r["ts_ns"] for r in logs if r["event_type"] == "SESSION_STARTED"), first)
out["structured_log_counts"] = {f"{r_t}|{r_v}": n for (r_t, r_v), n in sorted(collections.Counter((r["event_type"], r.get("venue", "")) for r in logs).items())}
out["connection_events"] = [{"t_s": round((r["ts_ns"] - t0) / 1e9, 1), "event": r["event_type"], "venue": r.get("venue"), "message": r.get("message")} for r in logs
                            if r["event_type"] in ("RESYNC_REQUIRED", "CONNECTOR_ERROR", "RECONNECT_SCHEDULED", "FEED_DELAYED", "FEED_CAUGHT_UP", "TRADE_FEED_STALE", "HOST_CLOCK_OFFSET") or r["level"] in ("ERROR", "CRITICAL")][:60]
paper = sess / "paper"
for name in ("data_quality_report.json", "time_integrity_report.json", "latency_report.json"):
    p = paper / name
    out[name[:-5]] = json.loads(p.read_text()) if p.exists() else None
defects = paper / "data_quality_defects.jsonl"
out["data_quality_defects"] = [{k: d.get(k) for k in ("code", "level", "metric", "observed", "threshold")} for d in map(json.loads, defects.open())] if defects.exists() else []
summ = paper / "summary.json"
if summ.exists():
    s = json.loads(summ.read_text())
    out["book_integrity_gate"] = s.get("book_integrity_gate")
    out["session_strategies"] = s.get("strategies")
    out["opportunities"] = s.get("opportunities")
    out["closed_plans"] = s.get("closed_plans")
    out["events_dropped_from_memory"] = s.get("events_dropped_from_memory")
man = sess / "live_manifest.json"
if man.exists():
    m = json.loads(man.read_text())
    out["recorder"] = {k: m.get(k) for k in ("wire_records", "canonical_events", "trade_ticks", "derivative_ticks", "control_frames", "rejected_frames", "frame_accounting", "session_sha256")}
    svc = m.get("service", {})
    out["venue_stats"] = {v: {k: st.get(k) for k in ("connections", "book_stream_frames", "book_stream_silences", "stale_disconnects", "validation_errors", "parse_errors", "control_frames", "book_syncs", "feed_delayed_events", "reconciles_ok")}
                          for v, st in svc.get("venues", {}).items()}
    out["strategy_gate"] = svc.get("strategy_gate")
Path(sys.argv[2]).write_text(json.dumps(out, indent=1, default=str))
print(json.dumps({k: out[k] for k in ("span_s", "book_data_frames", "sequence_continuity", "venue_stats") if k in out}, indent=1, default=str)[:3500])
