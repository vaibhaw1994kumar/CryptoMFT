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

from .event_loader import BOOK, DELTA, delta_fields, iter_venue_events, snapshot_levels, trade_fields

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


def _cached_scan_day(root: Path, venue: str, pair: str, day: str, loader_stats: dict[str, int]) -> dict[str, int]:
    """_scan_day with an optional on-disk cache (env NEW_BACKTEST_PREFLIGHT_CACHE=<dir>).

    The scan is a pure function of the day's book files and of the scanning code, so its raw defect counts can be reused by the runs of a grid that
    replay the same day. The key covers the venue/pair/day, the name, size and mtime of every book snapshot/event file of that day and the source of
    the scanner itself; thresholds are applied afterwards, so the gate is evaluated exactly as without the cache. No env var: no cache, no change."""
    import hashlib
    import inspect
    import json
    import os

    cache_dir = os.environ.get("NEW_BACKTEST_PREFLIGHT_CACHE")
    if not cache_dir:
        return _scan_day(root, venue, pair, day, loader_stats)
    from .event_loader import venue_root
    h = hashlib.sha256(f"{venue}|{pair}|{day}|{_QUALITY_SOURCE}".encode())
    h.update(inspect.getsource(_BookAccumulator).encode())
    h.update(inspect.getsource(_scan_day).encode())
    vr = venue_root(root, venue)
    for stream in ("book_snapshots", "book_events"):
        d = vr / stream / f"pair={pair}" / f"date={day}"
        if d.is_dir():
            for f in sorted(d.glob("*.jsonl.gz")):
                st = f.stat()
                h.update(f"{stream}/{f.name}:{st.st_size}:{st.st_mtime_ns}".encode())
    path = Path(cache_dir) / f"preflight_{venue}_{pair}_{day}_{h.hexdigest()[:20]}.json"
    if path.exists():
        try:
            doc = json.loads(path.read_text())
            loader_stats.update(doc["loader_stats"])
            return {k: int(v) for k, v in doc["day_counts"].items()}
        except Exception:
            pass
    counts = _scan_day(root, venue, pair, day, loader_stats)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"day_counts": counts, "loader_stats": dict(loader_stats), "key": h.hexdigest()}))
        tmp.replace(path)
    except OSError:
        pass
    return counts


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
    require_rules: bool = False,
) -> dict[str, Any]:
    """Run quality preflight for a venue/pair/date range.

    Returns the QualityGate report dict.  With ``report_only=True`` (the
    default) a CRITICAL gate does not raise; set ``report_only=False`` to abort
    replay when data quality is CRITICAL.

    When crypto_stat_arb is not importable the function returns immediately
    with ``{"skipped": True, "reason": "..."}``.
    """
    if QualityGate is None:
        if require_rules or not report_only:
            # Blocking mode must not silently pass data it could not check.
            raise RuntimeError(
                "Quality preflight could not run (crypto_stat_arb.quality not importable; "
                "run with PYTHONPATH=src); refusing to replay unchecked data"
            )
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
        day_counts = _cached_scan_day(root, venue, pair, day, loader_stats)
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


# ── Phase B data checks: observation only (nothing here feeds the gate, the replay or any result used for a byte comparison) ───────────────────────────────────────────────────────

# Every defect type quality.py defines. The preflight report states for each one whether this scan measures it; a defect it does not measure is NOT_MEASURED, never a plain 0.
_SEQUENCE_KEYS = ("u", "U", "pu", "seq", "last_update_id", "first_update_id", "prev_update_id", "lastUpdateId", "sequence")
_DELTA_SEMANTICS = {"bybit": "hybrid", "binance": "absolute"}      # as new_backtest.config.make_venues
_INLINE_LIMITS = {"max_book_age_s": 0.5, "dead_feed_s": 5.0, "max_cross_venue_skew_s": 0.2,
                  "note": "inline rules judge one moment (the book at that moment); preflight limits judge one day (a count over the day)"}


def _defect_table(day_counts: dict, check: dict) -> dict[str, dict]:
    seq = check["sequence_fields_present"]
    t = lambda status, count=None, **kw: {"status": status, "count": count, **kw}
    nm = lambda why: t("NOT_MEASURED", None, reason=why)
    return {
        "stale_intervals": t("MEASURED", day_counts.get("stale_intervals"), rule="no book event for more than 30 s, judged at the next snapshot"),
        "crossed_books": t("MEASURED", day_counts.get("crossed_books"), rule="crossed at a snapshot; the crossed-at-message-end count is in `observation`"),
        "price_jumps": t("MEASURED", day_counts.get("price_jumps")), "wide_spreads": t("MEASURED", day_counts.get("wide_spreads")),
        "invalid_levels": t("MEASURED", day_counts.get("invalid_levels")), "silent_feeds": t("MEASURED", day_counts.get("silent_feeds")),
        "out_of_order": t("MEASURED", check["timestamps_backwards_exchange"] + check["timestamps_backwards_receive"],
                          rule="a record whose exchange or receive time is below the previous record's, in the loader's merged order"),
        "invalid_trades": (t("MEASURED", check["invalid_trades"], rule="price or size not finite and positive, side not buy/sell", scanned=check["events"]["trade"])
                           if check["events"]["trade"] else nm("no trade records for this venue-day")),
        "gaps": nm("this scan does not read sequence ids" + ("; ids ARE present in these files, the replay checks them inline (apply_sequence)" if seq else
                                                                "; the files carry no sequence ids, so nothing could be checked")),
        "duplicates": nm("book-level duplicates need sequence ids; file-level duplicates dropped by the loader are in `loader_duplicates_dropped`"),
        "negative_latency": nm("clock analysis not performed (the receive-minus-exchange gap is reported in the Phase B timestamp report)"),
        "impossible_latency": nm("clock analysis not performed"),
        "schema_exceptions": nm("live-runner defect, not defined for partition files"), "rejected_records": nm("live-runner defect, not defined for partition files"),
        "rejected_partitions": nm("not defined for partition files"), "unknown_instrument_rules": nm("no instrument master is read by the preflight"),
        "persistence_lag_ns": nm("live-runner defect"), "unpersisted_bytes": nm("live-runner defect"),
    }


def scan_venue_day_checks(data_root: Path | str, venue: str, pair: str, day: str, *, tick: float | None = None, lot: float | None = None,
                          max_book_age_s: float = 0.5, dead_feed_s: float = 5.0, gap_limits: tuple[float, ...] = (5.0, 30.0)) -> dict[str, Any]:
    """One pass over a venue-day (snapshots, deltas and trades in the loader's merged order). Counts, never repairs or drops. The book is the engine's own OrderBook in
    message-atomic mode, so 'crossed at message end' is the engine's rule: a message is the run of consecutive deltas with one (exchange_ts, receive time)."""
    import math
    import time

    from crypto_stat_arb.dataset_manifest import ArrivalChecks
    from crypto_stat_arb.simulation.order_book import OrderBook

    t0 = time.perf_counter()
    book = OrderBook(venue, pair, _DELTA_SEMANTICS.get(venue, "hybrid"), tape_repair=False, message_atomic=True)
    arrival = ArrivalChecks(tick=tick, lot=lot)
    loader_stats: dict[str, int] = {}
    ev_n = {"book": 0, "delta": 0, "trade": 0}
    back_x = back_l = 0
    prev_x = prev_l = None
    last_book_t = None
    gaps = {g: [0, 0.0] for g in gap_limits}
    stale_s = dead_s = 0.0
    crossed_msgs = multi_msgs = msgs = 0
    neg_sizes = bad_price = invalid_trades = 0
    msg_key = None
    msg_n = 0
    seq_seen = False

    def close_message() -> None:
        nonlocal crossed_msgs, multi_msgs, msgs, msg_key, msg_n
        before = book.crossed_events
        book.end_message()
        crossed_msgs += book.crossed_events - before
        msgs += 1
        multi_msgs += msg_n > 1
        msg_key, msg_n = None, 0

    for ev in iter_venue_events(data_root, venue, pair, day, day, include_trades=True, stats=loader_stats, trade_dedupe="multiset"):
        if prev_x is not None and ev.exchange_ts < prev_x:
            back_x += 1
        prev_x = ev.exchange_ts
        if ev.local_ts > 0:
            if prev_l is not None and ev.local_ts < prev_l:
                back_l += 1
            prev_l = ev.local_ts
        t = ev.local_ts if ev.local_ts > 0 else ev.exchange_ts
        if ev.kind == DELTA:
            ev_n["delta"] += 1
            key = (ev.exchange_ts, ev.local_ts)
            if msg_key is not None and key != msg_key:
                close_message()
            msg_key = key
            msg_n += 1
            if not seq_seen and ev_n["delta"] <= 200 and any(k in ev.payload for k in _SEQUENCE_KEYS):
                seq_seen = True
            side, price, vol, etype = delta_fields(ev.payload)
            arrival.book_side(side)
            if price <= 0:
                bad_price += 1
            else:
                arrival.price(price)
                arrival.size(vol, may_be_zero=True)
                if vol < 0:
                    neg_sizes += 1
                book.apply_delta(side, price, vol, etype, ev.exchange_ts)
        else:
            if msg_key is not None:
                close_message()
            if ev.kind == BOOK:
                ev_n["book"] += 1
                if not seq_seen and any(k in ev.payload for k in _SEQUENCE_KEYS):
                    seq_seen = True
                bids, asks = snapshot_levels(ev.payload)
                for lv in list(bids) + list(asks):
                    try:
                        pr, sz = float(lv[0]), float(lv[1])
                    except (TypeError, ValueError, IndexError):
                        bad_price += 1
                        continue
                    arrival.price(pr)
                    arrival.size(sz, may_be_zero=True)
                    neg_sizes += sz < 0
                book.apply_snapshot([[float(l[0]), max(0.0, float(l[1]))] for l in bids if len(l) >= 2], [[float(l[0]), max(0.0, float(l[1]))] for l in asks if len(l) >= 2], ev.exchange_ts)
            else:
                ev_n["trade"] += 1
                pr, vol, _sign = trade_fields(ev.payload)
                raw_side = ev.payload.get("side", "")
                arrival.trade_side(raw_side if isinstance(raw_side, str) else str(raw_side))
                arrival.price(pr)
                arrival.size(vol)
                if not (math.isfinite(pr) and math.isfinite(vol)) or pr <= 0 or vol <= 0 or not str(raw_side).strip():
                    invalid_trades += 1                  # an unusual side word is counted by the arrival check, not here; only an empty side makes the trade unusable
        if ev.kind in (BOOK, DELTA):                       # book age: time between consecutive book events
            if last_book_t is not None and t > last_book_t:
                g = t - last_book_t
                stale_s += max(0.0, g - max_book_age_s)
                dead_s += max(0.0, g - dead_feed_s)
                for lim in gap_limits:
                    if g > lim:
                        gaps[lim][0] += 1
                        gaps[lim][1] += g
            last_book_t = t if last_book_t is None else max(last_book_t, t)
    if msg_key is not None:
        close_message()
    return {
        "venue": venue, "day": day, "events": ev_n, "loader_duplicates_dropped": loader_stats.get("duplicates_dropped", 0),
        "sequence_fields_present": seq_seen,
        "timestamps_backwards_exchange": back_x, "timestamps_backwards_receive": back_l, "invalid_trades": int(invalid_trades),
        "stale_gaps": {f"gt_{g:g}s": {"count": c, "seconds_in_gaps": round(sec, 3)} for g, (c, sec) in gaps.items()},
        "observation": {
            "messages": msgs, "multi_delta_messages": multi_msgs, "crossed_books_at_message_end": book.crossed_events,
            "negative_sizes": int(neg_sizes), "invalid_prices": bad_price,
            "feed_silent_beyond_dead_feed_s": {"limit_s": dead_feed_s, "count": gaps.get(dead_feed_s, [None])[0], "seconds_beyond_limit": round(dead_s, 3)},
            "non_valid_book_seconds": {"book_older_than_max_book_age_s": round(stale_s, 3), "max_book_age_s": max_book_age_s,
                                       "note": "the engine marks a book stale beyond max_book_age_s; a cross at a message end is repaired at once (zero duration); invalid/recovering states need sequence ids"},
        },
        "arrival_checks": arrival.report(), "scan_seconds": round(time.perf_counter() - t0, 3),
    }


def data_checks_report(data_root: Path | str, venues: tuple[str, ...], pair: str, days: tuple[str, ...], *, ticks: dict[str, float] | None = None,
                       lots: dict[str, float] | None = None, thresholds: "QualityThresholds | None" = None) -> dict[str, Any]:
    """The preflight report with the Phase B additions, per venue-day: the existing preflight counts (unchanged), the new scan, the MEASURED / NOT_MEASURED status of every
    quality.py defect type, the preflight (per day) limits next to the inline (per moment) limits, and the scan time each part took. Observation only."""
    import time

    thr = thresholds if thresholds is not None else _default_thresholds()
    rows = []
    for venue in venues:
        for day in days:
            t0 = time.perf_counter()
            base = _scan_day(Path(data_root), venue, pair, day, {})
            base_s = time.perf_counter() - t0
            chk = scan_venue_day_checks(data_root, venue, pair, day, tick=(ticks or {}).get(venue), lot=(lots or {}).get(venue))
            rows.append({"venue": venue, "day": day, "preflight_counts": base, "preflight_scan_seconds": round(base_s, 3), "added_scan_seconds": chk["scan_seconds"],
                         "defects": _defect_table(base, chk), "checks": chk})
    return {"label": "Phase B data checks, observation only", "pair": pair, "rows": rows, "quality_source": _QUALITY_SOURCE,
            "preflight_limits_per_day": (None if thr is None else {k: getattr(thr, k) for k in thr.__dataclass_fields__}), "inline_limits_per_moment": _INLINE_LIMITS,
            "stale_thresholds_s": {"preflight": _STALE_THRESHOLD_S, "engine_dead_feed": _INLINE_LIMITS["dead_feed_s"]}}
