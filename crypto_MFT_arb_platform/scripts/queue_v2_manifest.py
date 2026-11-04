#!/usr/bin/env python3
"""Queue model v2, step 1: data contract and manifest. Read-only on the data tree; writes only under --out-dir.

    python3.12 scripts/queue_v2_manifest.py --data-root DATA --dataset jan2022 --out-dir out/queue_v2/manifest_parts
    python3.12 scripts/queue_v2_manifest.py --merge out/queue_v2/manifest_parts --out out/queue_v2/manifest.json

One entry per file: provider, exchange id, product, symbol, date, channel, sha256, size, parser/schema version, unit scales (decimals seen in the
raw text, never via float), snapshot count, id availability (trade ids / update ids), capture gaps, rows, first/last time, gzip integrity and a
`truncated` flag. Provider, exchange id and product are DECLARED below (they cannot be derived from the files) and say so.
capture_region and client_version are DECLARED per dataset (UNKNOWN where the files do not say; the data request asks the provider).
A day is OK, EMPTY (a channel has no rows) or TRUNCATED (any file's gzip stream ends early). new_backtest.data_manifest blocks TRUNCATED days
unless the run carries --allow-rejected-data "<reason>"."""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import gzip
import hashlib
import json
import os
import re
import sys
import zlib
from pathlib import Path

CHUNK = 4 << 20
GAP_S = 5.0
SAMPLE_LINES = 20000
TS_RE = {"book_events": re.compile(rb'"exchange_ts":\s*([0-9.]+)'), "trades": re.compile(rb'"exchange_ts":\s*([0-9.]+)'),
         "book_snapshots": re.compile(rb'"timestamp":\s*([0-9.]+)')}
TRADE_ID_FIELDS = ("trade_id", "agg_id", "t", "a", "i", "id")
UPDATE_ID_FIELDS = ("u", "U", "pu", "seq", "update_id", "sequence", "lastUpdateId")

# DECLARED labels: the files do not say which product they are. Sources: profiles/perp_perp_maker_taker_v1.yaml (bybit linear / binance USD-M perps),
# config/spot_book_identity_check.json (the 2026-04 collector files are the SPOT market).
UNREC = "UNKNOWN (not recorded in the files; ask the provider)"
DATASETS = {
    "jan2022": {"venues": {"binance": ("binance-futures", "perp_usdt_margined"), "bybit": ("bybit", "perp_linear")}, "symbols": ["SOLUSDT"],
                "days": ("2022-01-03", "2022-01-13"), "provider": "tardis-shaped files (declared)", "capture_region": UNREC, "client_version": UNREC},
    "oct2024": {"venues": {"binance": ("binance-futures", "perp_usdt_margined"), "bybit": ("bybit", "perp_linear")}, "symbols": ["SOLUSDT"],
                "days": ("2024-10-01", "2024-10-20"), "provider": "tardis-iobc-client archives binance_oct24 / bybit_oct24 (declared)", "capture_region": UNREC,
                "client_version": "tardis-iobc-client, version UNKNOWN (not recorded in the files)"},
    "mar2024": {"venues": {"binance": ("binance-futures", "perp_usdt_margined"), "bybit": ("bybit", "perp_linear")}, "symbols": ["SOLUSDT"],
                "days": ("2024-03-08", "2024-03-15"), "provider": "Binance: data.binance.vision futures/um bookTicker (L1 only) + trades; Bybit: bycsi linear ob500 + public trades (declared)",
                "capture_region": UNREC, "client_version": UNREC},
    "spot2026": {"venues": {"bybit-spot": ("bybit", "spot")}, "symbols": None, "days": ("2026-04-13", "2026-04-25"),
                 "provider": "own collector (declared)", "capture_region": "own collector host (declared; the exact host/region is not recorded in the files)",
                 "client_version": "own collector, version UNKNOWN (not recorded in the files)"},
}


def parser_version(root: Path) -> dict:
    src = (root / "src" / "new_backtest" / "event_loader.py").read_bytes()
    return {"loader": "new_backtest.event_loader", "sha256_12": hashlib.sha256(src).hexdigest()[:12], "dedupe": "set (default)"}


def decimals(tok: str) -> int:
    return len(tok.split(".", 1)[1]) if "." in tok else 0


def scan(path: str, channel: str) -> dict:
    ts_re = TS_RE[channel]
    h = hashlib.sha256()
    d = zlib.decompressobj(31)
    rows, first, last, prev, backward, max_back = 0, None, None, None, 0, 0.0
    gaps: list[tuple[float, float]] = []
    gap_total, carry, ok, err = 0.0, b"", True, ""
    sample: list[bytes] = []
    with open(path, "rb") as fh:
        while True:
            raw = fh.read(CHUNK)
            if not raw:
                break
            h.update(raw)
            try:
                buf = d.decompress(raw)
            except zlib.error as e:
                ok, err = False, f"zlib: {e}"[:100]
                break
            lines = (carry + buf).split(b"\n")
            carry = lines.pop()
            for line in lines:
                if not line:
                    continue
                m = ts_re.search(line)
                if m is None:
                    continue
                t = float(m.group(1))
                rows += 1
                if first is None:
                    first = t
                if len(sample) < SAMPLE_LINES:
                    sample.append(line)
                if prev is not None:
                    dt_ = t - prev
                    if dt_ < 0:
                        backward += 1
                        max_back = max(max_back, -dt_)
                    elif dt_ > GAP_S:
                        gap_total += dt_
                        gaps.append((dt_, prev))
                prev = t
        if ok and not d.eof:
            ok, err = False, "unexpected end of gzip stream (truncated)"
        last = prev
    if ok and carry.strip():
        pass                                       # a complete stream whose last line has no newline: counted nowhere, not an error
    # sample-based fields (raw text via parse_float=str, so '152.40' keeps its two decimals)
    keys: set[str] = set()
    level_sizes: set = set()
    pdec = sdec = 0
    for line in sample[:2000] + sample[-2000:]:
        try:
            r = json.loads(line, parse_float=str)
        except Exception:
            continue
        keys |= set(r)
        for k in ("price", "volume", "size"):
            v = r.get(k)
            if isinstance(v, str):
                if k == "price":
                    pdec = max(pdec, decimals(v))
                else:
                    sdec = max(sdec, decimals(v))
        for side in ("bids", "asks"):
            for lv in (r.get(side) or [])[:5]:
                if len(lv) >= 2 and isinstance(lv[0], str):
                    pdec, sdec = max(pdec, decimals(lv[0])), max(sdec, decimals(str(lv[1])))
                if len(lv) >= 2:
                    level_sizes.add(str(lv[1]))
    gaps.sort(reverse=True)
    return {"sha256": h.hexdigest(), "gzip_ok": ok, "error": err, "truncated": not ok, "rows": rows,
            "first_ts": first, "last_ts": last, "backward_steps": backward, "max_backward_s": round(max_back, 6),
            "gaps_over_5s": len(gaps), "gap_seconds_total": round(gap_total, 3),
            "longest_gaps": [{"after_ts": a, "seconds": round(g, 3)} for g, a in gaps[:5]],
            "schema_keys": sorted(keys), "price_decimals": pdec, "size_decimals": sdec,
            "placeholder_snapshots": bool(channel == "book_snapshots" and rows > 50 and len(level_sizes) <= 1), "distinct_level_sizes_in_sample": len(level_sizes),
            "trade_id_field": next((f for f in TRADE_ID_FIELDS if f in keys), None) if channel == "trades" else None,
            "update_id_field": next((f for f in UPDATE_ID_FIELDS if f in keys), None) if channel == "book_events" else None}


def same_stream(path_a: str, path_b: str, n: int = 20000) -> bool:
    """True when the first n records of two channel files carry the same (exchange_ts, price, volume): the 'book_events' are then a copy of the trade tape, not depth."""
    try:
        with gzip.open(path_a, "rt") as fa, gzip.open(path_b, "rt") as fb:
            k = 0
            for la, lb in zip(fa, fb):
                a, b = json.loads(la), json.loads(lb)
                if (a.get("exchange_ts"), a.get("price"), a.get("volume")) != (b.get("exchange_ts"), b.get("price"), b.get("volume")):
                    return False
                k += 1
                if k >= n:
                    break
            return k >= 50
    except (EOFError, OSError, ValueError):
        return False


def days_between(a: str, b: str):
    d0, d1 = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
    return [(d0 + dt.timedelta(n)).isoformat() for n in range((d1 - d0).days + 1)]


def run(args) -> None:
    root = Path(args.platform_root)
    ds = DATASETS[args.dataset]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    parser = parser_version(root)
    files, days = [], {}
    only = set(args.symbols.split(",")) if args.symbols else None
    for venue, (exchange_id, product) in ds["venues"].items():
        vroot = Path(args.data_root) / venue
        syms = ds["symbols"] or sorted(p.name.split("=", 1)[1] for p in (vroot / "book_events").glob("pair=*"))
        if only:
            syms = [s for s in syms if s in only]
        for sym in syms:
            for day in days_between(*ds["days"]):
                entry = {"channels": {}, "status": "OK"}
                for channel in ("book_events", "book_snapshots", "trades"):
                    fs = sorted(glob.glob(f"{vroot}/{channel}/pair={sym}/date={day}/*.jsonl.gz"))
                    per = []
                    for f in fs:
                        s = scan(f, channel)
                        rec = {"path": os.path.relpath(f, args.data_root), "provider": ds["provider"], "exchange_id": exchange_id, "product": product,
                               "capture_region": ds.get("capture_region", UNREC), "client_version": ds.get("client_version", UNREC),
                               "provenance_unrecorded": [k for k in ("capture_region", "client_version") if "UNKNOWN" in str(ds.get(k, UNREC))],
                               "venue_dir": venue, "symbol": sym, "date": day, "channel": channel, "bytes": os.path.getsize(f),
                               "parser": parser, "snapshot_count": s["rows"] if channel == "book_snapshots" else None, **s}
                        files.append(rec)
                        per.append(rec)
                    entry["channels"][channel] = {"files": len(per), "rows": sum(r["rows"] for r in per), "truncated": any(r["truncated"] for r in per),
                                                  "placeholder": any(r.get("placeholder_snapshots") for r in per),
                                                  "first_ts": min((r["first_ts"] for r in per if r["first_ts"] is not None), default=None),
                                                  "last_ts": max((r["last_ts"] for r in per if r["last_ts"] is not None), default=None)}
                    rngs = sorted((r["first_ts"], r["last_ts"]) for r in per if r["first_ts"] is not None)
                    entry["channels"][channel]["overlapping_files"] = any(rngs[i + 1][0] < rngs[i][1] for i in range(len(rngs) - 1))
                if not any(c["files"] for c in entry["channels"].values()):
                    continue
                be_f = sorted(glob.glob(f"{vroot}/book_events/pair={sym}/date={day}/*.jsonl.gz"))
                tr_f = [f for f in sorted(glob.glob(f"{vroot}/trades/pair={sym}/date={day}/*.jsonl.gz")) if os.path.getsize(f) > 100]
                entry["book_events_is_trade_copy"] = bool(be_f and tr_f and entry["channels"]["book_events"]["rows"] == entry["channels"]["trades"]["rows"]
                                                          and same_stream(be_f[0], tr_f[0]))
                if any(c["truncated"] for c in entry["channels"].values()):
                    entry["status"] = "TRUNCATED"
                elif any(c["rows"] == 0 for c in entry["channels"].values()):
                    entry["status"] = "EMPTY"
                days[f"{venue}/{sym}/{day}"] = entry
                print(f"{venue}/{sym}/{day}: {entry['status']}  " + " ".join(f"{k}={v['rows']}" for k, v in entry['channels'].items()), flush=True)
    (out_dir / f"{args.dataset}{'_' + args.symbols.replace(',', '_')[:40] if args.symbols else ''}.json").write_text(json.dumps({"dataset": args.dataset, "files": files, "days": days}, indent=1))


def annotate(args) -> None:
    """Add the declared capture_region / client_version to a manifest written before those fields existed (no rescan: hashes and counts are kept).
    The dataset of a file follows from its path (a year directory in the date: 2022 -> jan2022, 2024 -> oct2024, 2026 -> spot2026)."""
    man = json.loads(Path(args.annotate).read_text())
    by_year = {"2022": "jan2022", "2024": "oct2024", "2026": "spot2026"}
    n = 0
    for f in man["files"]:
        ds = DATASETS["mar2024" if f["date"].startswith("2024-03") else by_year[f["date"][:4]]]
        for k in ("capture_region", "client_version"):
            if k not in f:
                f[k] = ds[k]
                n += 1
        f["provenance_unrecorded"] = [k for k in ("capture_region", "client_version") if "UNKNOWN" in str(f[k])]
    man["datasets"] = DATASETS | {}
    man["provenance_note"] = "capture_region and client_version are DECLARED per dataset and mostly UNKNOWN: the files do not carry them; the Oct-2024 data request asks the provider to state them"
    Path(args.out).write_text(json.dumps(man, indent=1))
    print("annotated", args.out, n, "fields added;", sum(1 for f in man["files"] if f["provenance_unrecorded"]), "of", len(man["files"]), "files have unrecorded provenance")


def merge(args) -> None:
    files, days, sets = [], {}, []
    for p in sorted(Path(args.merge).glob("*.json")):
        d = json.loads(p.read_text())
        files += d["files"]
        days.update(d["days"])
        sets.append(d["dataset"])
    doc = {"schema": "queue_v2_manifest/v1", "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "datasets": DATASETS | {},
           "datasets_scanned": sorted(set(sets)), "declared_labels_note": "provider, exchange_id and product are declared by the project, not derived from the files",
           "n_files": len(files), "days": days, "files": files}
    Path(args.out).write_text(json.dumps(doc, indent=1))
    c = {}
    for v in days.values():
        c[v["status"]] = c.get(v["status"], 0) + 1
    print("manifest", args.out, len(files), "files", len(days), "venue-days", c)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root"); ap.add_argument("--dataset", choices=sorted(DATASETS)); ap.add_argument("--out-dir")
    ap.add_argument("--platform-root", default=str(Path(__file__).resolve().parents[1])); ap.add_argument("--symbols", default="")
    ap.add_argument("--merge"); ap.add_argument("--out"); ap.add_argument("--annotate")
    a = ap.parse_args()
    annotate(a) if a.annotate else (merge(a) if a.merge else run(a))
