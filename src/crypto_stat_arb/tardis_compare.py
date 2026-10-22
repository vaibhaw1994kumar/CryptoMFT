"""Independent comparison of a recorded capture against a Tardis dataset (audit Step 10).

An offline research/validation tool: it never runs on the live path and adds no
dependency to it. Neither source is treated as ground truth; every discrepancy
keeps enough evidence (our ``raw_ref``, the Tardis CSV row) to investigate both.

Inputs
    * our capture: ``<session>/part-*/canonical.jsonl`` (one venue is selected);
    * Tardis ``incremental_book_L2`` CSV (``.csv`` or ``.csv.gz``):
      ``exchange,symbol,timestamp,local_timestamp,is_snapshot,side,price,amount``
      with microsecond timestamps; ``amount`` is the level's new absolute size
      (0 deletes); a run of ``is_snapshot=true`` rows rebuilds the book.

Method
    * Both books are rebuilt independently: ours with ``OrderBook`` (sequence
      checks and all), Tardis's from its rows, which are grouped back into the
      venue messages they came from (same timestamp, local timestamp and kind).
    * Both are sampled on the **exchange** timestamp (Tardis ``timestamp`` and our
      ``exchange_ts_ns`` both carry the venue's event time; arrival times differ
      by design, so they are reported, not aligned on).
    * At each sample: best bid/ask, then level by level within our book's depth
      (Tardis often records a deeper book, e.g. Bybit 200/500 levels).

Classification of a sample
    MATCH                         touch and levels agree
    LOCAL_RECOVERY                our book was not VALID (excluded from mismatches)
    TIMESTAMP_ALIGNMENT           our touch equals Tardis's touch one message
                                  before or after the sample
    LOCAL_INGESTION_LOSS          we received nothing in the interval, Tardis did
    DEPTH_OR_SOURCE_DIFFERENCE    touch agrees, a deeper level differs
    UNRESOLVED                    none of the above
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from decimal import Decimal
import gzip
import hashlib
import heapq
import json
from pathlib import Path
from typing import Any, Iterator

from .models import BookState, MarketEvent
from .orderbook import OrderBook

US = 1_000


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(slots=True)
class TardisMessage:
    exchange_ts_ns: int
    local_ts_ns: int
    is_snapshot: bool
    first_row: int                                   # 1-based CSV data row
    updates: list[tuple[str, Decimal, Decimal]] = field(default_factory=list)


def iter_tardis_messages(path: Path, symbol: str | None = None) -> Iterator[TardisMessage]:
    opener = gzip.open if path.suffix == ".gz" else open
    current: TardisMessage | None = None
    with opener(path, "rt", newline="") as handle:          # type: ignore[operator]
        for row_number, row in enumerate(csv.DictReader(handle), start=1):
            if symbol is not None and row.get("symbol", "").upper() != symbol.upper():
                continue
            side = (row.get("side") or "").strip().lower()
            if side not in ("bid", "ask"):
                continue
            key = (int(row["timestamp"]) * US, int(row["local_timestamp"]) * US,
                   str(row.get("is_snapshot", "")).strip().lower() in ("true", "1"))
            if current is None or (current.exchange_ts_ns, current.local_ts_ns, current.is_snapshot) != key:
                if current is not None:
                    yield current
                current = TardisMessage(*key, first_row=row_number)
            current.updates.append((side, Decimal(row["price"]), Decimal(row["amount"])))
    if current is not None:
        yield current


@dataclass(slots=True)
class TardisBook:
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    in_snapshot: bool = False
    last_row: int = 0

    def apply(self, message: TardisMessage) -> None:
        if message.is_snapshot and not self.in_snapshot:
            self.bids.clear()
            self.asks.clear()
        self.in_snapshot = message.is_snapshot
        for side, price, amount in message.updates:
            levels = self.bids if side == "bid" else self.asks
            if amount <= 0:
                levels.pop(price, None)
            else:
                levels[price] = amount
        self.last_row = message.first_row

    def touch(self) -> tuple[Decimal | None, Decimal | None]:
        return (max(self.bids) if self.bids else None, min(self.asks) if self.asks else None)


def iter_capture_events(session: Path, venue: str) -> Iterator[MarketEvent]:
    for part in sorted(session.glob("part-*")):
        with (part / "canonical.jsonl").open(encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                if row.get("venue") == venue:
                    yield MarketEvent.from_dict(row)


def _top(levels: dict[Decimal, Decimal], depth: int, reverse: bool) -> dict[Decimal, Decimal]:
    pick = heapq.nlargest if reverse else heapq.nsmallest
    return {price: levels[price] for price in pick(depth, levels)}


def _level_differences(ours: OrderBook, tardis: TardisBook, depth: int) -> list[str]:
    our_bids, our_asks = _top(ours.bids, depth, True), _top(ours.asks, depth, False)
    low, high = min(our_bids), max(our_asks)                 # compare inside our depth window
    tardis_bids = {p: q for p, q in tardis.bids.items() if p >= low}
    tardis_asks = {p: q for p, q in tardis.asks.items() if p <= high}
    return sorted(
        {str(p) for p in set(our_bids) | set(tardis_bids) if our_bids.get(p) != tardis_bids.get(p)}
        | {str(p) for p in set(our_asks) | set(tardis_asks) if our_asks.get(p) != tardis_asks.get(p)}
    )


def _percentiles(values: list[int]) -> dict[str, float | None]:
    if not values:
        return {"p50_ms": None, "p99_ms": None, "max_ms": None}
    ordered = sorted(values)
    pick = lambda q: ordered[min(len(ordered) - 1, int((len(ordered) - 1) * q))] / 1e6  # noqa: E731
    return {"p50_ms": pick(0.5), "p99_ms": pick(0.99), "max_ms": ordered[-1] / 1e6}


def compare_with_tardis(
    session: Path, venue: str, tardis_csv: Path, *, symbol: str | None = None,
    interval_ns: int = 1_000_000_000, depth: int = 20, max_discrepancies: int = 200,
) -> dict[str, Any]:
    events = iter_capture_events(session, venue)
    messages = iter_tardis_messages(tardis_csv, symbol)
    ours: OrderBook | None = None
    tardis = TardisBook()
    counts: dict[str, int] = {}
    discrepancies: list[dict[str, Any]] = []
    our_latency: list[int] = []
    tardis_latency: list[int] = []
    our_events = tardis_messages = 0
    next_event = next(events, None)
    next_message = next(messages, None)
    if next_event is None or next_message is None:
        raise ValueError("nothing to compare: the capture or the Tardis file has no rows for this venue/symbol")
    start = max(next_event.exchange_ts_ns, next_message.exchange_ts_ns)
    sample_ts = start - start % interval_ns + interval_ns
    previous_touch = tardis.touch()
    last_raw_ref = ""
    while next_event is not None and next_message is not None:
        interval_events = interval_messages = 0
        while next_event is not None and next_event.exchange_ts_ns <= sample_ts:
            if ours is None:
                ours = OrderBook(venue, next_event.instrument_id, max_mid_jump_bps=None, max_spread_bps=None)
            ours.apply(next_event)
            our_latency.append(next_event.receive_ts_ns - next_event.exchange_ts_ns)
            last_raw_ref = next_event.raw_ref
            interval_events += 1
            next_event = next(events, None)
        while next_message is not None and next_message.exchange_ts_ns <= sample_ts:
            previous_touch = tardis.touch()
            tardis.apply(next_message)
            tardis_latency.append(next_message.local_ts_ns - next_message.exchange_ts_ns)
            interval_messages += 1
            next_message = next(messages, None)
        our_events += interval_events
        tardis_messages += interval_messages
        if ours is not None and tardis.bids and tardis.asks:
            if ours.state != BookState.VALID or not ours.bids or not ours.asks:
                classification, differing = "LOCAL_RECOVERY", []
            else:
                our_touch = (ours.best_bid, ours.best_ask)
                differing = _level_differences(ours, tardis, depth)
                if our_touch == tardis.touch():
                    classification = "MATCH" if not differing else "DEPTH_OR_SOURCE_DIFFERENCE"
                else:
                    following = None
                    if next_message is not None:
                        peek = TardisBook(dict(tardis.bids), dict(tardis.asks), tardis.in_snapshot)
                        peek.apply(next_message)
                        following = peek.touch()
                    if our_touch in (previous_touch, following):
                        classification = "TIMESTAMP_ALIGNMENT"
                    elif interval_events == 0 and interval_messages > 0:
                        classification = "LOCAL_INGESTION_LOSS"
                    else:
                        classification = "UNRESOLVED"
            counts[classification] = counts.get(classification, 0) + 1
            if classification not in ("MATCH", "LOCAL_RECOVERY") and len(discrepancies) < max_discrepancies:
                discrepancies.append({
                    "sample_exchange_ts_ns": sample_ts, "classification": classification,
                    "ours_touch": [str(x) for x in ((ours.best_bid, ours.best_ask) if ours.bids and ours.asks else ())],
                    "tardis_touch": [str(x) for x in tardis.touch()],
                    "differing_prices": differing[:20],
                    "ours_last_raw_ref": last_raw_ref, "tardis_last_row": tardis.last_row,
                    "events_in_interval": {"ours": interval_events, "tardis": interval_messages},
                })
        sample_ts += interval_ns
    compared = sum(v for k, v in counts.items() if k != "LOCAL_RECOVERY")
    mismatched = sum(v for k, v in counts.items() if k not in ("MATCH", "LOCAL_RECOVERY", "TIMESTAMP_ALIGNMENT"))
    return {
        "schema_version": "1.0.0",
        "venue": venue, "symbol": symbol,
        "inputs": {"capture": str(session), "tardis_csv": str(tardis_csv), "tardis_sha256": _sha256(tardis_csv),
                   "capture_parts": {p.name: _sha256(p / "canonical.jsonl") for p in sorted(session.glob("part-*"))}},
        "method": {"alignment": "exchange timestamp", "interval_ns": interval_ns, "depth": depth},
        "window_exchange_ts_ns": [start, sample_ts - interval_ns],
        "samples": counts, "compared_samples": compared,
        "mismatch_share": (mismatched / compared) if compared else None,
        "event_counts": {"ours_book_events": our_events, "tardis_messages": tardis_messages},
        # Our book is sequence-checked; Tardis L2 carries no venue sequence. With zero gaps
        # on our side, a persistent disagreement points at the other source first.
        "ours_integrity": None if ours is None else {
            "final_state": ours.state.value, "gaps": ours.stats.gaps, "resyncs": ours.stats.resyncs,
            "snapshots": ours.stats.snapshots, "crossed_or_locked": ours.stats.crossed_or_locked,
        },
        "arrival_minus_exchange": {"ours": _percentiles(our_latency), "tardis": _percentiles(tardis_latency)},
        "discrepancies": discrepancies,
        "reproduce": (f"python -m crypto_stat_arb.cli compare-tardis --capture {session} --venue {venue} "
                      f"--tardis {tardis_csv}" + (f" --symbol {symbol}" if symbol else "")
                      + f" --interval-ms {interval_ns // 1_000_000} --depth {depth}"),
    }
