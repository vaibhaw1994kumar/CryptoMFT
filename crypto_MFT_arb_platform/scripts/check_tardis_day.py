"""Tardis data quality check — run per venue-day before transferring to VM.

Usage (on JupyterHub or locally):
    python check_tardis_day.py --data-dir /path/to/tardis/raw --pair SOLUSDT --start 2022-01-05 --end 2022-01-31

Prints a per-day coverage table. Paste the output here before starting the full transfer.

Checks:
  1. Data types present (book_l2, trades, derivative_ticker) per venue-day
  2. Product type (perpetual vs spot — must be perp for both)
  3. Temporal coverage (first/last timestamp per venue-day, both venues same hours)
  4. Event counts (constant daily counts = generated/synthetic data flag)
  5. Common-day intersection across venues
  6. Minimum 8 common days with full overlap (≥22 hours each venue)
"""
from __future__ import annotations

import argparse
import gzip
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path


def iter_lines(path: Path):
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            yield from f
    else:
        with path.open(encoding="utf-8") as f:
            yield from f


def check_file(path: Path) -> dict:
    """Return {first_ts_ns, last_ts_ns, count, product_types} for a JSONL file."""
    first_ts = last_ts = None
    count = 0
    product_types: set[str] = set()
    for raw in iter_lines(path):
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        count += 1
        ts = row.get("timestamp") or row.get("exchange_ts_ns") or row.get("ts")
        if ts is not None:
            ts = int(ts)
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
        # Detect product type from symbol or instrument_id if present
        sym = row.get("symbol", "") or row.get("instrument_id", "")
        if "PERP" in sym.upper() or "PERPETUAL" in sym.upper():
            product_types.add("PERPETUAL")
        elif "SPOT" in sym.upper():
            product_types.add("SPOT")
    return {"first_ts_ns": first_ts, "last_ts_ns": last_ts, "count": count,
            "product_types": list(product_types)}


def ts_to_hours(ts_ns: int | None) -> float | None:
    if ts_ns is None:
        return None
    return (ts_ns / 1e9) % 86400 / 3600.0  # hours since midnight UTC


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, help="Root of Tardis raw downloads")
    parser.add_argument("--pair", required=True, help="e.g. SOLUSDT")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD (exclusive)")
    parser.add_argument("--venues", nargs="+", default=["bybit", "binance"],
                        help="Venue subdirs to check")
    args = parser.parse_args()

    root = Path(args.data_dir)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    STREAM_TYPES = ["book_l2", "trades", "derivative_ticker"]

    # {date_str: {venue: {stream: check_result}}}
    results: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(dict))

    d = start
    while d < end:
        ds = d.isoformat()
        for venue in args.venues:
            for stream in STREAM_TYPES:
                # Tardis typically: <root>/<venue>/<pair>/<date>/<stream>.jsonl.gz
                # or <root>/<venue>/<pair>_<stream>_<date>.csv.gz — adapt as needed
                candidates = [
                    root / venue / args.pair / ds / f"{stream}.jsonl.gz",
                    root / venue / args.pair / ds / f"{stream}.jsonl",
                    root / venue / f"{args.pair}_{stream}_{ds}.csv.gz",
                    root / venue / f"{args.pair}_{stream}_{ds}.jsonl.gz",
                    root / ds / venue / args.pair / f"{stream}.jsonl.gz",
                ]
                found = next((p for p in candidates if p.exists()), None)
                if found:
                    results[ds][venue][stream] = check_file(found)
                else:
                    results[ds][venue][stream] = None
        d += timedelta(days=1)

    # Print coverage table
    print(f"\n{'DATE':<12} {'VENUE':<10} {'STREAM':<20} {'FIRST_UTC':>8} {'LAST_UTC':>8} {'COUNT':>8} {'PRODUCT':>12}")
    print("-" * 80)

    book_counts_by_venue: dict[str, list[int]] = defaultdict(list)
    common_days = []

    d = start
    while d < end:
        ds = d.isoformat()
        d += timedelta(days=1)
        if ds not in results:
            continue
        day_ok = True
        for venue in args.venues:
            for stream in STREAM_TYPES:
                r = results[ds][venue].get(stream)
                if r is None:
                    print(f"{ds:<12} {venue:<10} {stream:<20} {'MISSING':>8}")
                    day_ok = False
                    continue
                first_h = ts_to_hours(r["first_ts_ns"])
                last_h = ts_to_hours(r["last_ts_ns"])
                pt = ",".join(r["product_types"]) or "unknown"
                fh = f"{first_h:.1f}h" if first_h is not None else "?"
                lh = f"{last_h:.1f}h" if last_h is not None else "?"
                flag = ""
                if pt not in ("PERPETUAL", ""):
                    flag += " ⚠️SPOT"
                if stream == "book_l2" and r["count"] > 0:
                    book_counts_by_venue[venue].append(r["count"])
                print(f"{ds:<12} {venue:<10} {stream:<20} {fh:>8} {lh:>8} {r['count']:>8} {pt:>12}{flag}")
        if day_ok:
            # Check temporal overlap
            firsts = []
            lasts = []
            for venue in args.venues:
                b = results[ds][venue].get("book_l2")
                if b and b["first_ts_ns"] and b["last_ts_ns"]:
                    firsts.append(b["first_ts_ns"])
                    lasts.append(b["last_ts_ns"])
            if len(firsts) == len(args.venues):
                overlap_start = max(firsts)
                overlap_end = min(lasts)
                overlap_h = max(0.0, (overlap_end - overlap_start) / 1e9 / 3600.0)
                if overlap_h >= 22:
                    common_days.append(ds)
                    print(f"  → {ds} common overlap {overlap_h:.1f}h ✓")
                else:
                    print(f"  → {ds} overlap only {overlap_h:.1f}h ✗ (need ≥22h)")

    print(f"\n{'='*80}")
    print(f"Common days (both venues, ≥22h overlap): {len(common_days)}")
    if len(common_days) < 8:
        print(f"  ⚠️  Only {len(common_days)} common days — need ≥8 for walk-forward, ideally 30")
    else:
        print(f"  ✓  {common_days[0]} .. {common_days[-1]}")

    # Synthetic data detector: flag if all book_l2 event counts are identical
    for venue, counts in book_counts_by_venue.items():
        if counts and len(set(counts)) == 1:
            print(f"  ⚠️  {venue} book_l2 count is {counts[0]} every day — likely generated/synthetic!")

    print()


if __name__ == "__main__":
    main()
