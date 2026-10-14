"""Pre-flight quality pass on partition data before backtest replay.

Scans book_snapshots and book_events for a (venue, pair, day) range and
populates a QualitySnapshot with every defect that can be detected from
the pre-processed partition format.  The result is fed to QualityGate so
the backtest can gate on the same quality standard as the live runner.

Import chain (standalone-friendly):
  1. Try relative import  →  works when loaded as crypto_stat_arb.advanced_backtest.*
  2. Fall back to absolute →  works when loaded as new_backtest.*
  3. Fall back to None     →  truly standalone; gate is skipped, report says so

Limitations (recorded in every report):
  * Sequence gaps are NOT tracked: the partition format does not preserve
    exchange sequence numbers.  Raw .jsonl.gz files contain ``seq``/``u``
    fields; wire normalize_bybit_depth / normalize_binance_depth if that
    level of rigour is needed.
  * Negative/impossible latency is NOT tracked: requires full clock_drift
    analysis across all records, which is out of scope for a light pre-pass.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# ── Quality imports (standalone-safe) ────────────────────────────────────────

try:
    from ..quality import QualityGate, QualitySnapshot, QualityThresholds  # advanced_backtest
    _QUALITY_SOURCE = "relative"
except ImportError:
    try:
        from crypto_stat_arb.quality import QualityGate, QualitySnapshot, QualityThresholds
        _QUALITY_SOURCE = "absolute"
    except ImportError:
        QualityGate = QualitySnapshot = QualityThresholds = None  # type: ignore[assignment,misc]
        _QUALITY_SOURCE = "unavailable"

from .event_loader import BOOK, DELTA, delta_fields, iter_venue_events, snapshot_levels

# ── Detection thresholds ──────────────────────────────────────────────────────

_MAX_MID_JUMP_BPS = 500.0    # flag as price_jump
_MAX_SPREAD_BPS = 500.0      # flag as wide_spread
_STALE_THRESHOLD_S = 30.0    # no event for 30 s = stale_interval
_SILENT_THRESHOLD_S = 60.0   # zero events for entire scan = silent_feed

_LIMITATIONS: tuple[str, ...] = (
    "Sequence gaps not tracked: partition format does not preserve exchange sequence numbers.",
    "Negative/impossible latency not tracked: clock_drift analysis not performed.",
)

# Backtest-appropriate default thresholds — more lenient than live defaults
# (which are all-zero) because historical data routinely has a handful of
# wide-spread ticks and stale intervals that live infra can recover from in
# real time but that are harmless in replay.
_BACKTEST_THRESHOLDS: "QualityThresholds | None" = None  # built lazily


def _default_thresholds() -> "QualityThresholds":
    global _BACKTEST_THRESHOLDS
    if _BACKTEST_THRESHOLDS is None and QualityThresholds is not None:
        _BACKTEST_THRESHOLDS = QualityThresholds(
            min_events=100,
            max_gaps=0,
            max_stale_intervals=30,   # collector restarts create natural gaps
            max_crossed_books=0,      # snapshot-level crossings are always bad
            max_duplicates=0,         # file-level dupes already removed by dedup
            max_out_of_order=0,
            max_price_jumps=3,
            max_wide_spreads=5,
            max_invalid_levels=0,
            max_invalid_trades=0,
            max_silent_feeds=0,
        )
    return _BACKTEST_THRESHOLDS  # type: ignore[return-value]


# ── Per-pair-day scanner ──────────────────────────────────────────────────────

class _BookAccumulator:
    """Minimal float order book for quality counting only.

    Crossed-book and wide-spread checks run ONLY at snapshot boundaries.
    Checking after every individual delta produces false positives because
    multi-level bursts are stored as separate records; the replay engine's
    ``_uncross()`` already repairs transient inter-delta crossings.

    Price-jump detection compares consecutive snapshot mids, not delta mids,
    for the same reason: a burst of deltas produces a sequence of intermediate
    mids that are not individually meaningful.
    """

    __slots__ = (
        "bids", "asks", "last_mid", "last_ts",
        "price_jumps", "wide_spreads", "invalid_levels", "crossed_books",
        "stale_intervals", "events",
    )

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_mid: float = 0.0
        self.last_ts: float = 0.0
        self.price_jumps: int = 0
        self.wide_spreads: int = 0
        self.invalid_levels: int = 0
        self.crossed_books: int = 0
        self.stale_intervals: int = 0
        self.events: int = 0

    def best_bid(self) -> float:
        return max(self.bids, default=0.0)

    def best_ask(self) -> float:
        return min(self.asks, default=float("inf"))

    def _mid(self) -> float:
        bb, ba = self.best_bid(), self.best_ask()
        return (bb + ba) / 2 if bb > 0 and ba < float("inf") else 0.0

    def _check_snapshot_quality(self, ts: float) -> None:
        """Check spread, cross, jump and stale only at snapshot boundaries."""
        new_mid = self._mid()
        bb, ba = self.best_bid(), self.best_ask()

        # Stale interval: no events for an extended period before this snapshot
        if self.last_ts > 0 and ts - self.last_ts > _STALE_THRESHOLD_S:
            self.stale_intervals += 1

        # Crossed / wide spread — meaningful only on a full book state
        if bb > 0 and ba < float("inf"):
            if bb >= ba:
                self.crossed_books += 1
            elif (ba - bb) / ((bb + ba) / 2) * 10_000 > _MAX_SPREAD_BPS:
                self.wide_spreads += 1

        # Price jump: compare to the mid at the PREVIOUS snapshot
        if self.last_mid > 0 and new_mid > 0:
            if abs(new_mid - self.last_mid) / self.last_mid * 10_000 > _MAX_MID_JUMP_BPS:
                self.price_jumps += 1

        if new_mid > 0:
            self.last_mid = new_mid
        self.last_ts = ts

    def apply_snapshot(self, bids_raw: list, asks_raw: list, ts: float) -> None:
        self.bids = {float(p): float(s) for p, s in bids_raw if float(p) > 0 and float(s) >= 0}
        self.asks = {float(p): float(s) for p, s in asks_raw if float(p) > 0 and float(s) >= 0}
        self._check_snapshot_quality(ts)
        self.events += 1

    def apply_delta(self, side: str, price: float, vol: float, etype: str, ts: float) -> None:
        if price <= 0:
            self.invalid_levels += 1
            return
        if vol < 0:
            self.invalid_levels += 1
            return
        target = self.bids if side == "bid" else self.asks
        if vol == 0 or etype in ("cancel", "delete"):
            target.pop(price, None)
        else:
            target[price] = vol
        # Track last event timestamp for stale-interval detection at next snapshot
        self.last_ts = ts
        self.events += 1


def _scan_day(
    data_root: Path,
    venue: str,
    pair: str,
    day: str,
    loader_stats: dict[str, int],
) -> dict[str, int]:
    """Scan one (venue, pair, day) and return raw defect counts."""
    book = _BookAccumulator()
    for ev in iter_venue_events(
        data_root, venue, pair, day, day,
        include_trades=False,
        stats=loader_stats,
    ):
        if ev.kind == BOOK:
            bids, asks = snapshot_levels(ev.payload)
            book.apply_snapshot(bids, asks, ev.exchange_ts)
        elif ev.kind == DELTA:
            side, price, vol, etype = delta_fields(ev.payload)
            book.apply_delta(side, price, vol, etype, ev.exchange_ts)

    return {
        "events": book.events,
        "price_jumps": book.price_jumps,
        "wide_spreads": book.wide_spreads,
        "invalid_levels": book.invalid_levels,
        "crossed_books": book.crossed_books,
        "stale_intervals": book.stale_intervals,
        "silent_feeds": 1 if book.events == 0 else 0,
    }


# ── Public API ────────────────────────────────────────────────────────────────

def run_preflight(
    data_root: Path | str,
    venue: str,
    pair: str,
    start_day: str,
    end_day: str,
    thresholds: "QualityThresholds | None" = None,
    *,
    report_only: bool = True,
    scope: str | None = None,
) -> dict[str, Any]:
    """Run quality preflight for a venue/pair/date range.

    Returns the QualityGate report dict.  With ``report_only=True`` (the
    default) a CRITICAL gate does not raise; set ``report_only=False`` to abort
    replay when data quality is CRITICAL.

    When crypto_stat_arb is not importable the function returns immediately
    with ``{"skipped": True, "reason": "..."}``.
    """
    if QualityGate is None:
        return {
            "skipped": True,
            "reason": "crypto_stat_arb.quality not importable; run with PYTHONPATH=src",
            "quality_source": _QUALITY_SOURCE,
        }

    from .event_loader import _day_range

    counts: dict[str, int] = {
        "events": 0,
        "price_jumps": 0,
        "wide_spreads": 0,
        "invalid_levels": 0,
        "crossed_books": 0,
        "stale_intervals": 0,
        "silent_feeds": 0,
        "duplicates_dropped": 0,
    }

    root = Path(data_root)
    for day in _day_range(start_day, end_day):
        loader_stats: dict[str, int] = {}
        day_counts = _scan_day(root, venue, pair, day, loader_stats)
        for k, v in day_counts.items():
            counts[k] = counts.get(k, 0) + v
        counts["duplicates_dropped"] += loader_stats.get("duplicates_dropped", 0)

    thr = thresholds if thresholds is not None else _default_thresholds()
    # duplicates_dropped are file-level overlaps already removed by the dedup
    # merge; they do not reach the replay engine and are NOT a quality defect.
    # Book-level sequence duplicates require the raw WS format to detect.
    snapshot = QualitySnapshot(
        events=counts["events"],
        price_jumps=counts["price_jumps"],
        wide_spreads=counts["wide_spreads"],
        invalid_levels=counts["invalid_levels"],
        crossed_books=counts["crossed_books"],
        stale_intervals=counts["stale_intervals"],
        silent_feeds=counts["silent_feeds"],
        duplicates=0,
    )

    gate = QualityGate(thr)
    report = gate.build_report(
        snapshot,
        scope=scope or f"{venue}/{pair}/{start_day}:{end_day}",
        correlation_id=f"backtest-preflight-{venue}-{pair}-{start_day}",
        limitations=_LIMITATIONS,
    )
    report["quality_source"] = _QUALITY_SOURCE
    report["raw_duplicates_dropped_by_dedup"] = counts["duplicates_dropped"]

    if not report_only and gate.health.value == "CRITICAL":
        flags = ", ".join(f.code for f in gate.active_flags)
        raise RuntimeError(
            f"Quality preflight CRITICAL for {venue}/{pair}/{start_day}: {flags}"
        )
    return report
