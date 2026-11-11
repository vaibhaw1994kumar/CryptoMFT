"""Day-1 step B2 (a read-only scan of the capture): (1) which trades carry quality flags or are rejected, per venue and flag, with samples and the hours they fall in; (2) the gaps in the receive stream per venue and stream type (book, bbo, trade),
counts above 0.5, 1, 5 s and the longest, by hour. usage: quality_scan.py SESSION_DIR OUT_JSON"""
import gzip, json, sys, time
from collections import Counter, defaultdict
from pathlib import Path
D, OUT = Path(sys.argv[1]), Path(sys.argv[2])
flags, samples, byhour = Counter(), {}, Counter()
trades_by_venue = Counter(); other_trade_keys = Counter()
last = {}; gaps = defaultdict(lambda: {"n_over_0.5s": 0, "n_over_1s": 0, "n_over_5s": 0, "longest_s": 0.0, "longest_at_ns": None}); gaps_by_hour = Counter()
disp_count = Counter(); t0 = time.time()
for i, part in enumerate(sorted(D.glob("part-*")), 1):
    for l in gzip.open(part / "trades.jsonl.gz", "rt"):
        r = json.loads(l); trades_by_venue[r["venue"]] += 1
        qf = tuple(r.get("quality_flags") or ())
        for f in qf or ("<none>",):
            flags[(r["venue"], f)] += 1
        if qf:
            hour = int(r["receive_ts_ns"] // 3_600_000_000_000) % 24
            byhour[(r["venue"], hour)] += 1
            samples.setdefault((r["venue"], qf), {k: r.get(k) for k in ("trade_id", "price", "quantity", "aggressor_side", "exchange_ts_ns", "receive_ts_ns", "symbol", "native", "schema_version")})
    for l in gzip.open(part / "trace.jsonl.gz", "rt"):
        r = json.loads(l); disp_count[r["disposition"]] += 1
        key = (r["venue"], r["disposition"])
        ts = r["receive_ts_ns"]
        p = last.get(key)
        if p is not None:
            g = (ts - p) / 1e9
            c = gaps[key]
            if g > 0.5: c["n_over_0.5s"] += 1
            if g > 1: c["n_over_1s"] += 1
            if g > 5:
                c["n_over_5s"] += 1; gaps_by_hour[(r["venue"], r["disposition"], int(ts // 3_600_000_000_000) % 24)] += 1
            if g > c["longest_s"]: c["longest_s"], c["longest_at_ns"] = g, ts
        last[key] = ts
    if i % 300 == 0: print(time.strftime("%T"), i, flush=True)
res = {"trades_by_venue": dict(trades_by_venue), "trade_quality_flags": {f"{v}|{f}": n for (v, f), n in flags.items()},
       "flagged_trades_by_venue_and_utc_hour": {f"{v}|{h:02d}": n for (v, h), n in sorted(byhour.items())},
       "flag_samples": [{"venue": v, "flags": list(f), **s} for (v, f), s in samples.items()], "trace_dispositions": dict(disp_count),
       "gaps_by_venue_and_stream": {f"{v}|{d}": c for (v, d), c in gaps.items()}, "gaps_over_5s_by_utc_hour": {f"{v}|{d}|{h:02d}": n for (v, d, h), n in sorted(gaps_by_hour.items())}, "seconds": round(time.time() - t0, 1)}
OUT.write_text(json.dumps(res, indent=1, default=str))
print(json.dumps({k: res[k] for k in ("trades_by_venue", "trade_quality_flags", "gaps_by_venue_and_stream")}, indent=1, default=str))
