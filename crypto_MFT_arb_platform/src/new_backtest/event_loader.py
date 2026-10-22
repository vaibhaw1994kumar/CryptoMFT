"""Venue-agnostic Tardis-layout event loader.

Expects the partitioned layout already used by data/bybit/, replicated per venue:

    <root>/<venue>/book_snapshots/pair=<PAIR>/date=YYYY-MM-DD/*.jsonl.gz
    <root>/<venue>/book_events/   pair=<PAIR>/date=YYYY-MM-DD/*.jsonl.gz
    <root>/<venue>/trades/        pair=<PAIR>/date=YYYY-MM-DD/*.jsonl.gz

fetch_tardis.py writes Binance into exactly this shape, so one loader serves
both venues.

Record fields (as written by the existing Bybit collector):
    snapshot    timestamp, best_bid, best_ask, bids[[px,sz]], asks[[px,sz]]
    book_event  exchange_ts, local_ts, side, price, volume, event_type
    trade       exchange_ts, local_ts, price, volume, side ("b"/"s")

Corrupt gzip tails are tolerated — data/bybit/ contains at least one truncated
file, and a collector killed mid-write is normal operationally. A truncated file
yields the records it managed to write and then stops.
"""
from __future__ import annotations

import gzip
import itertools
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

BOOK = "book"
DELTA = "book_event"
TRADE = "trade"


@dataclass(slots=True)
class RawEvent:
    """One record, before receive-time projection."""
    exchange_ts: float
    local_ts: float          # 0.0 when the collector did not record one
    venue: str
    pair: str
    kind: str                # BOOK | DELTA | TRADE
    payload: dict


def _iter_jsonl_gz(path: Path) -> Iterator[dict]:
    try:
        with gzip.open(path, "rt") as fh:
            while True:
                try:
                    line = fh.readline()
                except (EOFError, OSError):
                    return          # truncated tail: keep what we read
                if not line:
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except (OSError, EOFError):
        return


def _day_range(start_day: str, end_day: str) -> Iterator[str]:
    cur = datetime.strptime(start_day, "%Y-%m-%d")
    end = datetime.strptime(end_day, "%Y-%m-%d")
    while cur <= end:
        yield cur.strftime("%Y-%m-%d")
        cur += timedelta(days=1)


# Content fields that identify an exchange event, per stream. local_ts and
# clock_drift are excluded on purpose: two collectors see the same event with
# different host stamps.
_DEDUPE_FIELDS: dict[str, tuple[str, ...]] = {
    "book_snapshots": ("timestamp",),
    "book_events": ("exchange_ts", "side", "price", "volume", "event_type"),
    "trades": ("exchange_ts", "price", "volume", "side"),
}
_DEDUPE_WINDOW_S = 3.0


def _merge_sorted(
    iters: list[Iterator[dict]], key: str,
    dedupe_fields: tuple[str, ...] | None = None,
    stats: dict[str, int] | None = None,
) -> Iterator[dict]:
    """K-way merge of already-sorted record iterators on `key`, dropping
    exact duplicates seen within _DEDUPE_WINDOW_S.

    Duplicates are real: on 2026-04-16 two Bybit collector instances ran side by
    side for ~11 hours, so every exchange event appears in two files. Merged
    without dedup, every trade would consume resting queues twice and every
    hybrid-semantics cancel would subtract twice.
    """
    import heapq
    from collections import deque

    heap: list[tuple[float, int, dict, Iterator[dict]]] = []
    for i, it in enumerate(iters):
        first = next(it, None)
        if first is not None:
            heapq.heappush(heap, (float(first.get(key, 0.0) or 0.0), i, first, it))

    recent: dict[tuple, float] = {}
    order: deque[tuple] = deque()

    while heap:
        ts, i, rec, it = heapq.heappop(heap)
        nxt = next(it, None)
        if nxt is not None:
            heapq.heappush(heap, (float(nxt.get(key, 0.0) or 0.0), i, nxt, it))

        if dedupe_fields is not None:
            k = tuple(rec.get(f) for f in dedupe_fields)
            if k in recent:
                if stats is not None:
                    stats["duplicates_dropped"] = stats.get("duplicates_dropped", 0) + 1
                continue
            recent[k] = ts
            order.append(k)
            cutoff = ts - _DEDUPE_WINDOW_S
            while order and recent.get(order[0], ts) < cutoff:
                recent.pop(order.popleft(), None)
        yield rec


def _stream_dir(
    root: Path, stream: str, pair: str, day: str, key: str,
    stats: dict[str, int] | None = None,
) -> Iterator[dict]:
    """All files for one stream/day, merged on `key`.

    A day directory typically holds several files — one per collector
    (re)start — and their time ranges overlap; on 2026-04-16 one file starts a
    full 40,683 s before the previous one ends. Concatenating in filename order
    would replay that as a backward time jump, applying hours of stale deltas
    on top of current state. Merging on timestamp keeps the stream monotone.
    """
    d = root / stream / f"pair={pair}" / f"date={day}"
    if not d.exists():
        return iter(())
    files = sorted(d.glob("*.jsonl.gz"))
    if not files:
        return iter(())
    return _merge_sorted(
        [_iter_jsonl_gz(f) for f in files], key,
        dedupe_fields=_DEDUPE_FIELDS.get(stream), stats=stats,
    )


def venue_root(data_root: Path | str, venue: str) -> Path:
    """data/ + bybit -> data/bybit. Accepts a path that already points at the venue."""
    root = Path(data_root)
    if (root / "book_snapshots").exists():
        return root
    return root / venue


def has_data(data_root: Path | str, venue: str, pair: str, day: str) -> bool:
    root = venue_root(data_root, venue)
    for stream in ("book_snapshots", "book_events", "trades"):
        d = root / stream / f"pair={pair}" / f"date={day}"
        if d.exists() and any(d.glob("*.jsonl.gz")):
            return True
    return False


def available_days(data_root: Path | str, venue: str, pair: str) -> list[str]:
    root = venue_root(data_root, venue)
    days: set[str] = set()
    for stream in ("book_snapshots", "book_events", "trades"):
        d = root / stream / f"pair={pair}"
        if not d.exists():
            continue
        for sub in d.iterdir():
            if sub.is_dir() and sub.name.startswith("date="):
                days.add(sub.name[5:])
    return sorted(days)


def iter_venue_events(
    data_root: Path | str,
    venue: str,
    pair: str,
    start_day: str,
    end_day: str,
    include_snapshots: bool = True,
    include_deltas: bool = True,
    include_trades: bool = True,
    stats: dict[str, int] | None = None,
) -> Iterator[RawEvent]:
    """Chronological (exchange_ts) merge of one venue's three streams.

    Day-by-day so memory stays flat: only the current day's file handles are
    open regardless of how long the date range is. `stats`, if given, receives
    loader counters such as duplicates_dropped.
    """
    root = venue_root(data_root, venue)

    for day in _day_range(start_day, end_day):
        snaps = _stream_dir(root, "book_snapshots", pair, day, "timestamp", stats) if include_snapshots else iter(())
        deltas = _stream_dir(root, "book_events", pair, day, "exchange_ts", stats) if include_deltas else iter(())
        trades = _stream_dir(root, "trades", pair, day, "exchange_ts", stats) if include_trades else iter(())

        ns = next(snaps, None)
        nd = next(deltas, None)
        nt = next(trades, None)

        while ns is not None or nd is not None or nt is not None:
            ts_s = float(ns.get("timestamp", float("inf"))) if ns is not None else float("inf")
            ts_d = float(nd.get("exchange_ts", float("inf"))) if nd is not None else float("inf")
            ts_t = float(nt.get("exchange_ts", float("inf"))) if nt is not None else float("inf")

            # Snapshot first on ties: it is a full resync, so applying it before
            # a same-instant delta keeps the delta from being overwritten.
            if ts_s <= ts_d and ts_s <= ts_t:
                yield RawEvent(ts_s, float(ns.get("local_ts", 0.0) or 0.0), venue, pair, BOOK, ns)
                ns = next(snaps, None)
            elif ts_d <= ts_t:
                yield RawEvent(ts_d, float(nd.get("local_ts", 0.0) or 0.0), venue, pair, DELTA, nd)
                nd = next(deltas, None)
            else:
                yield RawEvent(ts_t, float(nt.get("local_ts", 0.0) or 0.0), venue, pair, TRADE, nt)
                nt = next(trades, None)


# ── Payload normalisation ─────────────────────────────────────────────────────

def snapshot_levels(p: dict) -> tuple[list, list]:
    return p.get("bids") or [], p.get("asks") or []


def delta_fields(p: dict) -> tuple[str, float, float, str]:
    return (
        str(p.get("side", "bid")).lower(),
        float(p.get("price", 0.0) or 0.0),
        float(p.get("volume", p.get("size", 0.0)) or 0.0),
        str(p.get("event_type", "modify")).lower(),
    )


def trade_fields(p: dict) -> tuple[float, float, int]:
    """Returns (price, volume, sign) where sign=+1 buy aggressor, -1 sell."""
    side = p.get("side", "b")
    if isinstance(side, str):
        sign = 1 if side.lower().startswith("b") else -1
    else:
        sign = 1 if float(side) > 0 else -1
    return (
        float(p.get("price", 0.0) or 0.0),
        float(p.get("volume", p.get("size", 0.0)) or 0.0),
        sign,
    )
