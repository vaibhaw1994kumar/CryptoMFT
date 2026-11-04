"""Phase 1-lite STEP 0: inventory of the March-2024 SOLUSDT perp files (read-only on data/).
Per venue-day-channel: sha256 of each file, rows, gzip integrity (truncated), first/last ts, gaps > 5 s, update/trade id fields, levels per snapshot,
and a per-minute coverage mask. Writes out/exec_layer_p1lite/step0/<venue>.json.
usage: step0_inventory.py VENUE FIRST_DAY LAST_DAY"""
import datetime as dt, glob, hashlib, json, os, re, sys, zlib
import numpy as np

DATA = "/Users/vaibhav/Documents/Newproject/data"
OUT = "/Users/vaibhav/Documents/Newproject/out/exec_layer_p1lite/step0"
TS = {"book_events": re.compile(rb'"exchange_ts":\s*([0-9.]+)'), "trades": re.compile(rb'"exchange_ts":\s*([0-9.]+)'),
      "book_snapshots": re.compile(rb'"timestamp":\s*([0-9.]+)')}
ID_KEYS = (b'"trade_id"', b'"agg_id"', b'"t":', b'"a":', b'"i":', b'"id":', b'"u":', b'"U":', b'"pu":', b'"seq":', b'"update_id"')
venue, d0, d1 = sys.argv[1], dt.date.fromisoformat(sys.argv[2]), dt.date.fromisoformat(sys.argv[3])


def scan(path, ch, day0):
    h, d = hashlib.sha256(), zlib.decompressobj(31)
    rows, first, prev, gaps, gap_s, back, carry, ok = 0, None, None, 0, 0.0, 0, b"", True
    mins = np.zeros(1440, dtype=np.int64)
    ids, levels = set(), []
    with open(path, "rb") as fh:
        while True:
            raw = fh.read(4 << 20)
            if not raw:
                break
            h.update(raw)
            try:
                buf = d.decompress(raw)
            except zlib.error:
                ok = False
                break
            lines = (carry + buf).split(b"\n")
            carry = lines.pop()
            for ln in lines:
                m = TS[ch].search(ln)
                if m is None:
                    continue
                t = float(m.group(1))
                rows += 1
                if rows <= 2000:
                    ids |= {k.decode() for k in ID_KEYS if k in ln}
                    if ch == "book_snapshots" and rows <= 50:
                        r = json.loads(ln)
                        levels.append((len(r.get("bids") or []), len(r.get("asks") or [])))
                if first is None:
                    first = t
                if prev is not None:
                    if t < prev:
                        back += 1
                    elif t - prev > 5.0:
                        gaps += 1
                        gap_s += t - prev
                prev = t
                k = int((t - day0) // 60)
                if 0 <= k < 1440:
                    mins[k] += 1
    if ok and not d.eof:
        ok = False
    return {"path": os.path.relpath(path, DATA), "bytes": os.path.getsize(path), "sha256": h.hexdigest(), "truncated": not ok, "rows": rows,
            "first_ts": first, "last_ts": prev, "gaps_over_5s": gaps, "gap_seconds": round(gap_s, 1), "backward_steps": back,
            "id_fields_in_first_2000": sorted(ids), "snapshot_levels_first50": sorted(set(levels))}, mins


res = {}
day = d0
while day <= d1:
    ds = day.isoformat()
    day0 = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp()
    ent = {}
    for ch in ("book_events", "book_snapshots", "trades"):
        fs = sorted(glob.glob(f"{DATA}/{venue}/{ch}/pair=SOLUSDT/date={ds}/*.jsonl.gz"))
        recs, mask = [], np.zeros(1440, dtype=np.int64)
        for f in fs:
            r, m = scan(f, ch, day0)
            recs.append(r)
            mask += m
        ent[ch] = {"files": recs, "rows": sum(r["rows"] for r in recs), "truncated": any(r["truncated"] for r in recs),
                   "minute_counts": mask.tolist()}
    res[ds] = ent
    print(venue, ds, {c: (e["rows"], e["truncated"]) for c, e in ent.items()}, flush=True)
    json.dump({"venue": venue, "days": res}, open(f"{OUT}/{venue}.json", "w"))
    day += dt.timedelta(1)
