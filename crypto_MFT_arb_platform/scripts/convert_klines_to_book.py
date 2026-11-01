"""Convert 1-minute OHLCV klines (cryptomft CSV format) to the platform's
book_snapshots + book_events JSONL.gz layout.

Usage:
    python scripts/convert_klines_to_book.py \
        /path/to/cryptomft-binance-BTCUSDT-1m-*.csv \
        /path/to/cryptomft-bybit-BTCUSDT-1m-*.csv \
        --data-root /path/to/data \
        --pair BTCUSDT

Output layout (mirrors the existing SOL data):
    <data_root>/<venue>/book_snapshots/pair=<PAIR>/date=<YYYY-MM-DD>/<day_ms>.jsonl.gz
    <data_root>/<venue>/book_events/   pair=<PAIR>/date=<YYYY-MM-DD>/<day_ms>.jsonl.gz

Book synthesis:
  Each 1-minute bar produces:
  - One snapshot at open_time (OHLCV → synthetic 5-level book centred on open price).
  - Three delta events within the bar representing intra-minute price movement:
      t+20s  → high (or low, depending on direction)
      t+40s  → other extreme
      t+59s  → close
  These give the strategy ~4 evaluation opportunities per bar rather than 1.

Depth model:
  Depth is estimated from quote_volume / estimated_book_turns.  A simple
  exponential taper distributes it across 5 levels on each side.  The model
  is deliberately conservative — it understates depth so spread-capture
  signals have a realistic queue to beat.

Limitations (document these clearly):
  - 1-minute resolution: intra-minute price path is approximate.
  - No real queue depth: fill model will be less accurate than with tick data.
  - No trade stream: fills are driven by book touch changes only.
  - Latency model still simulated (singapore preset).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone, date, timedelta
from pathlib import Path


# ── Per-venue book parameters ─────────────────────────────────────────────────

_VENUE_PARAMS = {
    "binance": {
        "tick": 0.10,    # BTCUSDT perp tick on Binance ($0.10)
        "spread_ticks": 2,
        "level_count": 5,
        "latency_ms": 50,    # local_ts offset from exchange_ts (ms)
    },
    "bybit": {
        "tick": 0.50,    # BTCUSDT perp tick on Bybit ($0.50)
        "spread_ticks": 2,
        "level_count": 5,
        "latency_ms": 55,
    },
}

# Volume per level as fraction of total side depth, summing to 1.0
_LEVEL_FRACTIONS = [0.35, 0.25, 0.18, 0.13, 0.09]


# ── Book synthesis ────────────────────────────────────────────────────────────

def _round_to_tick(price: float, tick: float, direction: str) -> float:
    """Round price to the nearest tick, up (ask) or down (bid)."""
    if direction == "up":
        return math.ceil(price / tick) * tick
    return math.floor(price / tick) * tick


def _build_book(price: float, volume_base: float, params: dict, pair: str, venue: str,
                ts_s: float) -> dict:
    """Build a synthetic 5-level book snapshot centred on `price`."""
    tick = params["tick"]
    half_spread = (params["spread_ticks"] * tick) / 2.0
    best_bid = _round_to_tick(price - half_spread, tick, "down")
    best_ask = _round_to_tick(price + half_spread, tick, "up")

    # Estimate total displayed depth from volume (1 full-bar vol ≈ 10 book turns)
    side_depth = max(volume_base / 10.0, 0.01)

    bids, asks = [], []
    for i, frac in enumerate(_LEVEL_FRACTIONS[:params["level_count"]]):
        qty = round(side_depth * frac, 3)
        bids.append([round(best_bid - i * tick, 2), qty])
        asks.append([round(best_ask + i * tick, 2), qty])

    lat = params["latency_ms"] / 1000.0
    return {
        "timestamp": ts_s,
        "local_ts": ts_s + lat,
        "exchange": venue,
        "pair": pair,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bids": bids,
        "asks": asks,
    }


def _build_delta(price: float, volume_base: float, params: dict, pair: str, venue: str,
                 ts_s: float) -> list[dict]:
    """Three bid+ask modify events representing a price move to `price`."""
    tick = params["tick"]
    half_spread = (params["spread_ticks"] * tick) / 2.0
    best_bid = _round_to_tick(price - half_spread, tick, "down")
    best_ask = _round_to_tick(price + half_spread, tick, "up")
    side_depth = max(volume_base / 10.0, 0.01)
    lat = params["latency_ms"] / 1000.0

    events = []
    for side, best, sign in [("bid", best_bid, -1), ("ask", best_ask, 1)]:
        for i, frac in enumerate(_LEVEL_FRACTIONS[:params["level_count"]]):
            px = round(best + sign * i * tick, 2)
            events.append({
                "exchange": venue,
                "pair": pair,
                "exchange_ts": ts_s,
                "local_ts": ts_s + lat,
                "side": side,
                "price": px,
                "volume": round(side_depth * frac, 3),
                "event_type": "modify",
            })
    return events


# ── CSV parsing ───────────────────────────────────────────────────────────────

def _load_csv(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    return rows


def _normalise_venue(raw: str) -> str:
    return raw.strip().lower()


# ── Per-day writer ────────────────────────────────────────────────────────────

def _day_start_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)


def _write_day(day_str: str, snaps: list[dict], deltas: list[dict],
               data_root: Path, venue: str, pair: str, dry_run: bool) -> tuple[int, int]:
    day = datetime.strptime(day_str, "%Y-%m-%d").date()
    ms = _day_start_ms(day)
    fname = f"{ms}.jsonl.gz"

    for subdir, records, ts_key in [
        ("book_snapshots", snaps, "timestamp"),
        ("book_events", deltas, "exchange_ts"),
    ]:
        out_dir = data_root / venue / subdir / f"pair={pair}" / f"date={day_str}"
        out_path = out_dir / fname
        if dry_run:
            print(f"  [dry-run] would write {len(records)} records → {out_path}")
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        with gzip.open(out_path, "wt", compresslevel=6) as fh:
            for rec in sorted(records, key=lambda r: r.get(ts_key, 0.0)):
                fh.write(json.dumps(rec) + "\n")

    return len(snaps), len(deltas)


# ── Main ──────────────────────────────────────────────────────────────────────

def convert(csv_paths: list[Path], data_root: Path, pair: str,
            dry_run: bool = False) -> None:
    # Collect all rows, keyed by (venue, day_str)
    by_venue_day: dict[tuple[str, str], list[dict]] = defaultdict(list)

    for path in csv_paths:
        rows = _load_csv(path)
        for row in rows:
            venue = _normalise_venue(row["venue"])
            ts_ms = int(row["open_time_ms"])
            day_str = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            by_venue_day[(venue, day_str)].append(row)

    total_snap = total_delta = 0
    for (venue, day_str), rows in sorted(by_venue_day.items()):
        params = _VENUE_PARAMS.get(venue)
        if params is None:
            print(f"Warning: unknown venue {venue!r}, skipping", file=sys.stderr)
            continue

        snaps, deltas = [], []
        for row in sorted(rows, key=lambda r: int(r["open_time_ms"])):
            ts_ms = int(row["open_time_ms"])
            ts_s = ts_ms / 1000.0
            vol_base = float(row.get("volume") or 0) or 0.001

            o = float(row["open"])
            h = float(row["high"])
            l = float(row["low"])
            c = float(row["close"])

            # Snapshot at bar open
            snaps.append(_build_book(o, vol_base, params, pair, venue, ts_s))

            # Intra-bar deltas: simulate the intra-minute price path
            # t+20s: move toward high or low (first major move)
            going_up = c > o
            first_extreme = h if going_up else l
            second_extreme = l if going_up else h
            deltas.extend(_build_delta(first_extreme, vol_base / 3, params, pair, venue, ts_s + 20.0))
            # t+40s: retrace to opposite extreme (roughly)
            deltas.extend(_build_delta(second_extreme, vol_base / 3, params, pair, venue, ts_s + 40.0))
            # t+59s: close price
            deltas.extend(_build_delta(c, vol_base / 3, params, pair, venue, ts_s + 59.0))

        ns, nd = _write_day(day_str, snaps, deltas, data_root, venue, pair, dry_run)
        total_snap += ns; total_delta += nd
        print(f"  {venue}/{day_str}: {ns} snaps, {nd} deltas")

    print(f"\nTotal: {total_snap} snapshots, {total_delta} delta events")
    if not dry_run:
        print(f"Written to {data_root}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Convert 1-min klines to book_snapshots + book_events")
    ap.add_argument("csv", nargs="+", help="Input CSV files (cryptomft format)")
    ap.add_argument("--data-root", required=True, help="Root of the platform data directory")
    ap.add_argument("--pair", default="BTCUSDT", help="Trading pair (default BTCUSDT)")
    ap.add_argument("--dry-run", action="store_true", help="Print what would be written, don't write")
    args = ap.parse_args()

    csv_paths = [Path(p) for p in args.csv]
    for p in csv_paths:
        if not p.exists():
            print(f"Error: {p} does not exist", file=sys.stderr)
            sys.exit(1)

    convert(csv_paths, Path(args.data_root), args.pair, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
