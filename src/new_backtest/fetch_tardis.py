"""Get Binance data into the layout the backtester reads.

The Bybit side already exists under data/bybit/. Binance does not, and without
it there is no second venue and therefore no arb. This module fills that gap in
whichever way is available:

    download    pull straight from the Tardis HTTP API (needs TARDIS_API_KEY)
    normalize   convert Tardis-format CSVs already on disk into the layout

`normalize` exists because the data path is not always the public API — an
obfuscated/vendored Tardis client, a shared archive, or an export from a
restricted host all produce the same Tardis CSV schema. Whatever produced the
CSVs, normalize turns them into the partitioned jsonl.gz tree, and the
backtester neither knows nor cares which route was used.

Output, mirroring data/bybit/ exactly:

    <out>/binance/book_snapshots/pair=<PAIR>/date=<YYYY-MM-DD>/<epoch_ms>.jsonl.gz
    <out>/binance/book_events/   pair=<PAIR>/date=<YYYY-MM-DD>/<epoch_ms>.jsonl.gz
    <out>/binance/trades/        pair=<PAIR>/date=<YYYY-MM-DD>/<epoch_ms>.jsonl.gz

Tardis CSV schemas consumed here (timestamps are MICROSECONDS):

    incremental_book_L2
        exchange,symbol,timestamp,local_timestamp,is_snapshot,side,price,amount
    trades
        exchange,symbol,timestamp,local_timestamp,id,side,price,amount

In Tardis L2 the `amount` is the level's new absolute size and 0 means delete —
which is why config.VenueSpec for Binance sets delta_semantics="absolute".
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

TARDIS_BASE = "https://datasets.tardis.dev/v1"
SNAPSHOT_LEVELS = 25


# ── Helpers ───────────────────────────────────────────────────────────────────

def _days(start: str, end: str) -> Iterator[str]:
    cur = datetime.strptime(start, "%Y-%m-%d")
    last = datetime.strptime(end, "%Y-%m-%d")
    while cur <= last:
        yield cur.strftime("%Y-%m-%d")
        cur += timedelta(days=1)


def _us_to_s(value: str | int | float) -> float:
    """Tardis stamps are microseconds. Tolerate ms or s if a source differs."""
    v = float(value or 0.0)
    if v > 1e17:        # nanoseconds
        return v / 1e9
    if v > 1e14:        # microseconds
        return v / 1e6
    if v > 1e11:        # milliseconds
        return v / 1e3
    return v


def _out_path(out_root: Path, venue: str, stream: str, pair: str, day: str) -> Path:
    d = out_root / venue / stream / f"pair={pair}" / f"date={day}"
    d.mkdir(parents=True, exist_ok=True)
    epoch_ms = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    return d / f"{epoch_ms}.jsonl.gz"


class _JsonlGzWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = gzip.open(path, "wt", compresslevel=6)
        self.rows = 0

    def write(self, obj: dict) -> None:
        self._fh.write(json.dumps(obj, separators=(",", ":")))
        self._fh.write("\n")
        self.rows += 1

    def close(self) -> None:
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ── Download ──────────────────────────────────────────────────────────────────

def download_day(
    exchange: str,
    data_type: str,
    symbol: str,
    day: str,
    api_key: str,
    cache_dir: Path,
    timeout: int = 300,
) -> Path | None:
    """Fetch one Tardis csv.gz and cache it. Returns the cached path."""
    import requests

    y, m, d = day.split("-")
    url = f"{TARDIS_BASE}/{exchange}/{data_type}/{y}/{m}/{d}/{symbol}.csv.gz"
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / f"{exchange}_{data_type}_{symbol}_{day}.csv.gz"
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    resp = requests.get(url, headers=headers, stream=True, timeout=timeout)
    if resp.status_code == 404:
        print(f"    no data: {exchange}/{data_type}/{symbol}/{day}", file=sys.stderr)
        return None
    if resp.status_code in (401, 403):
        raise RuntimeError(
            f"Tardis auth failed ({resp.status_code}) for {url}. "
            "Set TARDIS_API_KEY, or use `normalize` if you obtain CSVs another way."
        )
    resp.raise_for_status()

    tmp = dest.with_suffix(".part")
    with tmp.open("wb") as fh:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            fh.write(chunk)
    tmp.replace(dest)
    return dest


# ── CSV readers ───────────────────────────────────────────────────────────────

def _open_csv(path: Path) -> Iterator[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", newline="") as fh:  # type: ignore[operator]
        for row in csv.DictReader(fh):
            yield row


def convert_l2(
    csv_path: Path,
    pair: str,
    day: str,
    out_root: Path,
    venue: str = "binance",
    snapshot_every_s: float = 1.0,
) -> tuple[int, int]:
    """incremental_book_L2 -> book_events + periodic book_snapshots.

    Tardis marks the bootstrap rows with is_snapshot=true; those rebuild the book
    from scratch. Every row after that is an incremental update.

    Periodic snapshots are emitted from a locally-maintained book at
    snapshot_every_s. They are not strictly required (the backtester can run on
    deltas alone) but they give the replay the same resync structure the Bybit
    feed has, which bounds book drift if a delete is ever dropped.
    """
    bids: dict[float, float] = {}
    asks: dict[float, float] = {}
    last_snap_ts = 0.0
    in_bootstrap = False

    ev_writer = _JsonlGzWriter(_out_path(out_root, venue, "book_events", pair, day))
    sn_writer = _JsonlGzWriter(_out_path(out_root, venue, "book_snapshots", pair, day))

    try:
        for row in _open_csv(csv_path):
            ts = _us_to_s(row.get("timestamp", 0))
            lts = _us_to_s(row.get("local_timestamp", 0))
            side = (row.get("side") or "").strip().lower()
            if side not in ("bid", "ask"):
                continue
            try:
                price = float(row.get("price") or 0.0)
                amount = float(row.get("amount") or 0.0)
            except ValueError:
                continue
            if price <= 0:
                continue

            is_snap = str(row.get("is_snapshot", "")).strip().lower() in ("true", "1")
            if is_snap and not in_bootstrap:
                bids.clear()
                asks.clear()
                in_bootstrap = True
            elif not is_snap:
                in_bootstrap = False

            book = bids if side == "bid" else asks
            if amount <= 0:
                book.pop(price, None)
            else:
                book[price] = amount

            if not is_snap:
                ev_writer.write({
                    "exchange": venue, "pair": pair,
                    "exchange_ts": ts, "local_ts": lts,
                    "side": side, "price": price, "volume": amount,
                    "event_type": "delete" if amount <= 0 else "modify",
                })

            if bids and asks and ts - last_snap_ts >= snapshot_every_s:
                top_bids = sorted(bids.items(), key=lambda kv: -kv[0])[:SNAPSHOT_LEVELS]
                top_asks = sorted(asks.items())[:SNAPSHOT_LEVELS]
                if top_bids and top_asks and top_bids[0][0] < top_asks[0][0]:
                    sn_writer.write({
                        "timestamp": ts, "local_ts": lts,
                        "exchange": venue, "pair": pair,
                        "best_bid": top_bids[0][0], "best_ask": top_asks[0][0],
                        "bids": [[p, s] for p, s in top_bids],
                        "asks": [[p, s] for p, s in top_asks],
                    })
                    last_snap_ts = ts
    finally:
        ev_writer.close()
        sn_writer.close()

    return ev_writer.rows, sn_writer.rows


def convert_trades(
    csv_path: Path, pair: str, day: str, out_root: Path, venue: str = "binance",
) -> int:
    """Tardis trades -> the trades stream. side is the aggressor: buy/sell."""
    writer = _JsonlGzWriter(_out_path(out_root, venue, "trades", pair, day))
    try:
        for row in _open_csv(csv_path):
            try:
                price = float(row.get("price") or 0.0)
                amount = float(row.get("amount") or 0.0)
            except ValueError:
                continue
            if price <= 0 or amount <= 0:
                continue
            side = (row.get("side") or "buy").strip().lower()
            writer.write({
                "exchange": venue, "pair": pair,
                "exchange_ts": _us_to_s(row.get("timestamp", 0)),
                "local_ts": _us_to_s(row.get("local_timestamp", 0)),
                "price": price, "volume": amount,
                "side": "b" if side.startswith("b") else "s",
            })
    finally:
        writer.close()
    return writer.rows


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_download(args) -> int:
    api_key = args.api_key or os.environ.get("TARDIS_API_KEY", "")
    if not api_key:
        print(
            "No API key. Set TARDIS_API_KEY or pass --api-key.\n"
            "If your Tardis access is through a vendored client instead, export the\n"
            "CSVs and run:  python -m new_backtest.fetch_tardis normalize --csv-dir ...",
            file=sys.stderr,
        )
        return 2

    out_root = Path(args.out_root)
    cache = Path(args.cache_dir)
    total_ev = total_sn = total_tr = 0

    for pair in args.pairs:
        symbol = args.symbol_template.format(pair=pair)
        for day in _days(args.start, args.end):
            print(f"[{pair} {day}]", flush=True)
            l2 = download_day(args.exchange, "incremental_book_L2", symbol, day, api_key, cache)
            if l2 is not None:
                ev, sn = convert_l2(l2, pair, day, out_root, args.venue, args.snapshot_every_s)
                total_ev += ev
                total_sn += sn
                print(f"    book_events={ev:,}  snapshots={sn:,}")
            tr_csv = download_day(args.exchange, "trades", symbol, day, api_key, cache)
            if tr_csv is not None:
                tr = convert_trades(tr_csv, pair, day, out_root, args.venue)
                total_tr += tr
                print(f"    trades={tr:,}")
            if not args.keep_cache:
                for p in (l2, tr_csv):
                    if p is not None and p.exists():
                        p.unlink()

    print(f"\ndone: {total_ev:,} deltas  {total_sn:,} snapshots  {total_tr:,} trades")
    print(f"written under {out_root / args.venue}")
    return 0


def cmd_normalize(args) -> int:
    """Convert Tardis CSVs already on disk. Filenames must contain the date."""
    csv_dir = Path(args.csv_dir)
    out_root = Path(args.out_root)
    files = sorted(csv_dir.glob(args.glob))
    if not files:
        print(f"no files matching {args.glob} under {csv_dir}", file=sys.stderr)
        return 2

    for path in files:
        day = _infer_day(path.name)
        if day is None:
            print(f"  skip (no YYYY-MM-DD in name): {path.name}", file=sys.stderr)
            continue
        pair = args.pair or _infer_pair(path.name)
        if pair is None:
            print(f"  skip (no pair in name, pass --pair): {path.name}", file=sys.stderr)
            continue
        name = path.name.lower()
        if "trade" in name:
            n = convert_trades(path, pair, day, out_root, args.venue)
            print(f"  {path.name}: trades={n:,}")
        elif "book" in name or "l2" in name:
            ev, sn = convert_l2(path, pair, day, out_root, args.venue, args.snapshot_every_s)
            print(f"  {path.name}: book_events={ev:,} snapshots={sn:,}")
        else:
            print(f"  skip (unknown type): {path.name}", file=sys.stderr)
    return 0


def _infer_day(name: str) -> str | None:
    import re
    m = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", name)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _infer_pair(name: str) -> str | None:
    import re
    m = re.search(r"([A-Z]{2,10}USDT?)", name.upper())
    return m.group(1) if m else None


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fetch/normalize Tardis data into the backtest layout")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("download", help="pull from the Tardis HTTP API")
    d.add_argument("--pairs", nargs="+", required=True)
    d.add_argument("--start", required=True)
    d.add_argument("--end", required=True)
    d.add_argument("--exchange", default="binance-futures",
                   help="tardis exchange id: binance-futures | binance | bybit | bybit-spot")
    d.add_argument("--venue", default="binance", help="output directory name")
    d.add_argument("--symbol-template", default="{pair}")
    d.add_argument("--out-root", default="data")
    d.add_argument("--cache-dir", default="data/_tardis_cache")
    d.add_argument("--snapshot-every-s", type=float, default=1.0)
    d.add_argument("--keep-cache", action="store_true")
    d.add_argument("--api-key", default="")
    d.set_defaults(func=cmd_download)

    n = sub.add_parser("normalize", help="convert Tardis CSVs already on disk")
    n.add_argument("--csv-dir", required=True)
    n.add_argument("--glob", default="*.csv.gz")
    n.add_argument("--pair", help="override; otherwise inferred from filename")
    n.add_argument("--venue", default="binance")
    n.add_argument("--out-root", default="data")
    n.add_argument("--snapshot-every-s", type=float, default=1.0)
    n.set_defaults(func=cmd_normalize)

    return p


def main(argv: Iterable[str] | None = None) -> int:
    args = build_arg_parser().parse_args(list(argv) if argv is not None else None)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
