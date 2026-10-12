"""Book reconstruction, deterministic hashing and the data-quality gate.

AT-03 requires repeated reconstruction to produce identical book hashes and
requires gaps, stale periods and invalid books to be flagged and recovered.

Beyond sequencing, the gate also rejects data that is sequence-valid but
economically impossible: non-positive prices, a mid that jumps further than a
configured band in one update, a spread wider than a configured band, trades
printed far outside the book, venue checksum mismatches, and feeds that go
silent. Time integrity (negative or impossible receive latency, cross-venue
receive skew, host-clock offset) reuses the stat-arb layer's
``TimeIntegrityMonitor`` and ``new_backtest``'s ``HostClockAligner`` so the
two code bases grade time the same way.
"""

from __future__ import annotations

import hashlib
from collections import Counter, deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from crypto_stat_arb.time_integrity import TimeIntegrityMonitor, TimePolicy
from new_backtest.latency_model import HostClockAligner
from trading_platform_poc.minreq.canonical import (
    BookPayload,
    BookValidity,
    CanonicalEvent,
    CanonicalEventType,
    DataQualityPayload,
    PriceLevel,
    QualityFlag,
    TradePayload,
    make_event_id,
)
from trading_platform_poc.minreq.checksums import verify_book_checksum

DEFAULT_HASH_DEPTH = 10
BPS = Decimal(10_000)
RECENT_SEQUENCE_WINDOW = 4096
DEFAULT_MAX_PRICE_JUMP_BPS = Decimal("500")
DEFAULT_MAX_SPREAD_BPS = Decimal("500")
DEFAULT_MAX_TRADE_DEVIATION_BPS = Decimal("300")

# Flags that describe where data came from rather than a defect in it. They
# are reported separately so a clean synthetic dataset still has no anomalies.
PROVENANCE_FLAGS = frozenset({QualityFlag.SYNTHETIC})


class Severity:
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(slots=True)
class BookApplyResult:
    applied: bool
    validity: BookValidity
    flags: tuple[QualityFlag, ...] = ()
    reason: str = ""


class CanonicalBook:
    """Venue-agnostic L2 book with explicit valid/stale/invalid state."""

    def __init__(
        self,
        venue: str,
        instrument_id: str,
        *,
        stale_after_ns: int = 2_000_000_000,
        hash_depth: int = DEFAULT_HASH_DEPTH,
        strict_sequencing: bool = True,
        max_price_jump_bps: Decimal | None = DEFAULT_MAX_PRICE_JUMP_BPS,
        max_spread_bps: Decimal | None = DEFAULT_MAX_SPREAD_BPS,
        verify_checksums: bool = True,
    ) -> None:
        self.venue = venue
        self.instrument_id = instrument_id
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self._stale_after_ns = stale_after_ns
        self._hash_depth = hash_depth
        self._strict = strict_sequencing
        self._max_price_jump_bps = max_price_jump_bps
        self._max_spread_bps = max_spread_bps
        self._verify_checksums = verify_checksums
        self._recent_sequences: deque[int] = deque()
        self._recent_set: set[int] = set()
        self.last_sequence: int | None = None
        self.last_update_ns: int = 0
        self.last_valid_mid: Decimal | None = None
        self.initialized = False
        self.validity = BookValidity.INVALID
        self.awaiting_snapshot = True
        self.gap_count = 0
        self.duplicate_count = 0
        self.out_of_order_count = 0
        self.crossed_count = 0
        self.snapshot_count = 0
        self.applied_count = 0
        self.checksum_failures = 0
        self.price_jumps = 0
        self.wide_spreads = 0
        self.schema_rejections = 0

    # ------------------------------------------------------------------ view
    @property
    def bids(self) -> tuple[PriceLevel, ...]:
        return tuple(
            PriceLevel(price=price, quantity=self._bids[price])
            for price in sorted(self._bids, reverse=True)
        )

    @property
    def asks(self) -> tuple[PriceLevel, ...]:
        return tuple(
            PriceLevel(price=price, quantity=self._asks[price])
            for price in sorted(self._asks)
        )

    @property
    def best_bid(self) -> Decimal | None:
        return max(self._bids) if self._bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return min(self._asks) if self._asks else None

    @property
    def mid_price(self) -> Decimal | None:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2

    @property
    def spread_bps(self) -> Decimal | None:
        bid, ask = self.best_bid, self.best_ask
        mid = self.mid_price
        if bid is None or ask is None or mid is None or mid <= 0:
            return None
        return (ask - bid) / mid * BPS

    def depth(self, side: str, levels: int | None = None) -> tuple[PriceLevel, ...]:
        book = self.asks if side.lower() == "buy" else self.bids
        return book if levels is None else book[:levels]

    def level_quantity(self, book_side: str, price: Decimal) -> Decimal:
        """Published size at one price. ``book_side`` is ``bid`` or ``ask``."""

        levels = self._bids if book_side == "bid" else self._asks
        return levels.get(price, Decimal(0))

    def is_stale(self, now_ns: int) -> bool:
        if not self.initialized:
            return True
        return now_ns - self.last_update_ns > self._stale_after_ns

    def book_hash(self, depth: int | None = None) -> str:
        """Deterministic digest of the top of book, stable across runs."""

        levels = depth or self._hash_depth
        parts = [self.venue, self.instrument_id, str(self.last_sequence)]
        for side_name, side_levels in (
            ("b", self.bids[:levels]),
            ("a", self.asks[:levels]),
        ):
            parts.append(side_name)
            parts.extend(
                f"{_norm(level.price)}:{_norm(level.quantity)}"
                for level in side_levels
            )
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    # ----------------------------------------------------------------- apply
    def apply(self, event: CanonicalEvent) -> BookApplyResult:
        payload = event.payload
        if not isinstance(payload, BookPayload):
            return BookApplyResult(False, self.validity, reason="not_a_book_event")
        invalid = _invalid_level(payload)
        if invalid:
            self.schema_rejections += 1
            return BookApplyResult(
                False,
                self.validity,
                (QualityFlag.SCHEMA_EXCEPTION,),
                invalid,
            )
        if payload.is_snapshot:
            return self._apply_snapshot(event, payload)
        return self._apply_delta(event, payload)

    def _remember(self, sequence: int | None) -> None:
        if sequence is None or sequence in self._recent_set:
            return
        self._recent_sequences.append(sequence)
        self._recent_set.add(sequence)
        while len(self._recent_sequences) > RECENT_SEQUENCE_WINDOW:
            self._recent_set.discard(self._recent_sequences.popleft())

    def _apply_snapshot(
        self, event: CanonicalEvent, payload: BookPayload
    ) -> BookApplyResult:
        sequence = payload.final_sequence if payload.final_sequence is not None else 0
        if (
            self._strict
            and self.last_sequence is not None
            and sequence < self.last_sequence
            and not self.awaiting_snapshot
        ):
            self.out_of_order_count += 1
            return BookApplyResult(
                False,
                self.validity,
                (QualityFlag.OUT_OF_ORDER,),
                "snapshot_older_than_book",
            )
        self._bids = {
            level.price: level.quantity
            for level in payload.bids
            if level.quantity > 0
        }
        self._asks = {
            level.price: level.quantity
            for level in payload.asks
            if level.quantity > 0
        }
        self.last_sequence = sequence
        self._remember(sequence)
        self.last_update_ns = event.receive_timestamp_ns
        self.initialized = True
        self.snapshot_count += 1
        self.applied_count += 1
        flags: tuple[QualityFlag, ...] = ()
        if self.awaiting_snapshot and self.snapshot_count > 1:
            flags = (QualityFlag.SNAPSHOT_RESYNC,)
        self.awaiting_snapshot = False
        return self._finalize(event, payload, flags)

    def _apply_delta(
        self, event: CanonicalEvent, payload: BookPayload
    ) -> BookApplyResult:
        if not self.initialized:
            # Initial synchronisation, not a defect: a live diff-depth stream
            # delivers updates before the REST snapshot that anchors them.
            return BookApplyResult(
                False,
                BookValidity.INVALID,
                (),
                "awaiting_initial_snapshot",
            )
        if self.awaiting_snapshot:
            return BookApplyResult(
                False,
                BookValidity.INVALID,
                (QualityFlag.SEQUENCE_GAP,),
                "awaiting_snapshot",
            )
        final = payload.final_sequence
        first = payload.first_sequence
        if (
            final is not None
            and final == self.last_sequence
            and not payload.bids
            and not payload.asks
        ):
            # A keep-alive (OKX sends seqId == prevSeqId with no levels when
            # nothing changed): proof the feed is alive, not a redelivery.
            self.last_update_ns = event.receive_timestamp_ns
            return BookApplyResult(True, self.validity, (), "keepalive")
        if final is not None and self.last_sequence is not None:
            if final <= self.last_sequence:
                # A redelivery of an update already applied is a duplicate; an
                # older update that was never applied arrived out of order.
                if final in self._recent_set:
                    self.duplicate_count += 1
                    return BookApplyResult(
                        False,
                        self.validity,
                        (QualityFlag.DUPLICATE,),
                        "duplicate_update",
                    )
                self.out_of_order_count += 1
                return BookApplyResult(
                    False,
                    self.validity,
                    (QualityFlag.OUT_OF_ORDER,),
                    f"out_of_order {final}<={self.last_sequence}",
                )
            if (
                self._strict
                and first is not None
                and first > self.last_sequence + 1
            ):
                self.gap_count += 1
                self.awaiting_snapshot = True
                self.validity = BookValidity.INVALID
                return BookApplyResult(
                    False,
                    BookValidity.INVALID,
                    (QualityFlag.SEQUENCE_GAP,),
                    f"gap {self.last_sequence}->{first}",
                )
        for level in payload.bids:
            if level.quantity <= 0:
                self._bids.pop(level.price, None)
            else:
                self._bids[level.price] = level.quantity
        for level in payload.asks:
            if level.quantity <= 0:
                self._asks.pop(level.price, None)
            else:
                self._asks[level.price] = level.quantity
        if final is not None:
            self.last_sequence = final
            self._remember(final)
        self.last_update_ns = event.receive_timestamp_ns
        self.applied_count += 1
        return self._finalize(event, payload, ())

    def _finalize(
        self,
        event: CanonicalEvent,
        payload: BookPayload,
        flags: tuple[QualityFlag, ...],
    ) -> BookApplyResult:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            self.validity = BookValidity.INVALID
            return BookApplyResult(
                True, self.validity, (*flags, QualityFlag.EMPTY_BOOK), "empty_side"
            )
        if bid >= ask:
            self.crossed_count += 1
            self.validity = BookValidity.INVALID
            self.awaiting_snapshot = True
            return BookApplyResult(
                True,
                self.validity,
                (*flags, QualityFlag.CROSSED_BOOK),
                f"crossed {bid}>={ask}",
            )
        if self._verify_checksums and payload.checksum is not None:
            verified = verify_book_checksum(
                self.venue, self._bids, self._asks, payload.checksum
            )
            if verified is False:
                self.checksum_failures += 1
                self.validity = BookValidity.INVALID
                self.awaiting_snapshot = True
                return BookApplyResult(
                    True,
                    self.validity,
                    (*flags, QualityFlag.CHECKSUM_MISMATCH),
                    f"checksum_mismatch expected={payload.checksum}",
                )
        mid = (bid + ask) / 2
        spread = (ask - bid) / mid * BPS
        if self._max_spread_bps is not None and spread > self._max_spread_bps:
            self.wide_spreads += 1
            self.validity = BookValidity.INVALID
            return BookApplyResult(
                True,
                self.validity,
                (*flags, QualityFlag.WIDE_SPREAD),
                f"spread {spread:.2f}bps>{self._max_spread_bps}bps",
            )
        previous = self.last_valid_mid
        self.last_valid_mid = mid
        if (
            self._max_price_jump_bps is not None
            and previous is not None
            and previous > 0
        ):
            jump = abs(mid - previous) / previous * BPS
            if jump > self._max_price_jump_bps:
                # The new level is remembered, so a genuine move is confirmed by
                # the next consistent update instead of blocking forever.
                self.price_jumps += 1
                self.validity = BookValidity.INVALID
                return BookApplyResult(
                    True,
                    self.validity,
                    (*flags, QualityFlag.PRICE_JUMP),
                    f"mid jumped {jump:.2f}bps>{self._max_price_jump_bps}bps",
                )
        self.validity = BookValidity.VALID
        return BookApplyResult(True, self.validity, flags, "")

    def mark_stale(self) -> None:
        if self.validity is BookValidity.VALID:
            self.validity = BookValidity.STALE

    def invalidate(self, reason: str = "") -> None:
        self.validity = BookValidity.INVALID
        self.awaiting_snapshot = True

    def state(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "instrument_id": self.instrument_id,
            "validity": self.validity.value,
            "last_sequence": self.last_sequence,
            "last_update_ns": self.last_update_ns,
            "best_bid": None if self.best_bid is None else str(self.best_bid),
            "best_ask": None if self.best_ask is None else str(self.best_ask),
            "bid_levels": len(self._bids),
            "ask_levels": len(self._asks),
            "book_hash": self.book_hash() if self.initialized else "",
            "gap_count": self.gap_count,
            "duplicate_count": self.duplicate_count,
            "out_of_order_count": self.out_of_order_count,
            "crossed_count": self.crossed_count,
            "snapshot_count": self.snapshot_count,
            "applied_count": self.applied_count,
            "checksum_failures": self.checksum_failures,
            "price_jumps": self.price_jumps,
            "wide_spreads": self.wide_spreads,
            "schema_rejections": self.schema_rejections,
        }


def _invalid_level(payload: BookPayload) -> str:
    for side, levels in (("bid", payload.bids), ("ask", payload.asks)):
        for level in levels:
            if level.price <= 0:
                return f"non_positive_{side}_price:{level.price}"
            if level.quantity < 0:
                return f"negative_{side}_quantity:{level.quantity}"
    return ""


def _norm(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        try:
            normalized = normalized.quantize(Decimal(1))
        except Exception:  # pragma: no cover - exotic exponents only
            pass
    return format(normalized, "f")


@dataclass
class QualityCounters:
    events: int = 0
    accepted: int = 0
    rejected: int = 0
    by_code: Counter[str] = field(default_factory=Counter)
    by_event_type: Counter[str] = field(default_factory=Counter)
    provenance: Counter[str] = field(default_factory=Counter)
    stale_intervals_ns: int = 0
    max_gap_ns: int = 0

    def merge(self, other: QualityCounters) -> None:
        self.events += other.events
        self.accepted += other.accepted
        self.rejected += other.rejected
        self.by_code.update(other.by_code)
        self.by_event_type.update(other.by_event_type)
        self.provenance.update(other.provenance)
        self.stale_intervals_ns += other.stale_intervals_ns
        self.max_gap_ns = max(self.max_gap_ns, other.max_gap_ns)


@dataclass(frozen=True, slots=True)
class QualityThresholds:
    """Approval gate and detection bands for a partition or a run."""

    max_sequence_gaps: int = 0
    max_crossed_books: int = 0
    max_schema_exceptions: int = 0
    max_stale_ratio: Decimal = Decimal("0.05")
    max_duplicate_ratio: Decimal = Decimal("0.05")
    min_events: int = 1
    max_out_of_order_ratio: Decimal = Decimal("0.05")
    max_checksum_mismatches: int = 0
    max_price_jumps: int = 0
    max_wide_spread_ratio: Decimal = Decimal("0.01")
    max_invalid_trades: int = 0
    max_stale_time_ratio: Decimal = Decimal("0.05")
    max_silent_streams: int = 0
    max_negative_latency_ratio: Decimal = Decimal("0.01")
    max_impossible_latency_events: int = 0
    max_price_jump_bps: Decimal = DEFAULT_MAX_PRICE_JUMP_BPS
    max_spread_bps: Decimal = DEFAULT_MAX_SPREAD_BPS
    max_trade_deviation_bps: Decimal = DEFAULT_MAX_TRADE_DEVIATION_BPS
    max_event_latency_ns: int = 60_000_000_000
    max_cross_venue_skew_ns: int = 1_000_000_000

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_sequence_gaps": self.max_sequence_gaps,
            "max_crossed_books": self.max_crossed_books,
            "max_schema_exceptions": self.max_schema_exceptions,
            "max_stale_ratio": str(self.max_stale_ratio),
            "max_duplicate_ratio": str(self.max_duplicate_ratio),
            "min_events": self.min_events,
            "max_out_of_order_ratio": str(self.max_out_of_order_ratio),
            "max_checksum_mismatches": self.max_checksum_mismatches,
            "max_price_jumps": self.max_price_jumps,
            "max_wide_spread_ratio": str(self.max_wide_spread_ratio),
            "max_invalid_trades": self.max_invalid_trades,
            "max_stale_time_ratio": str(self.max_stale_time_ratio),
            "max_silent_streams": self.max_silent_streams,
            "max_negative_latency_ratio": str(self.max_negative_latency_ratio),
            "max_impossible_latency_events": self.max_impossible_latency_events,
            "max_price_jump_bps": str(self.max_price_jump_bps),
            "max_spread_bps": str(self.max_spread_bps),
            "max_trade_deviation_bps": str(self.max_trade_deviation_bps),
            "max_event_latency_ns": self.max_event_latency_ns,
            "max_cross_venue_skew_ns": self.max_cross_venue_skew_ns,
        }


@dataclass(frozen=True, slots=True)
class _TimeView:
    """The four fields ``TimeIntegrityMonitor.observe`` reads, keyed so venues
    quoting the same economic instrument are aligned against each other."""

    venue: str
    instrument_id: str
    exchange_ts_ns: int
    receive_ts_ns: int


def economic_key(instrument_id: str) -> str:
    """``BTC-USDT.BINANCE.SPOT`` -> ``BTC-USDT.SPOT``."""

    parts = instrument_id.split(".")
    if len(parts) >= 3:
        return f"{parts[0]}.{parts[-1]}"
    return instrument_id


class QualityMonitor:
    """Reconstructs books for every (venue, instrument) and grades the stream."""

    def __init__(
        self,
        *,
        stale_after_ns: int = 2_000_000_000,
        hash_depth: int = DEFAULT_HASH_DEPTH,
        thresholds: QualityThresholds | None = None,
        strict_sequencing: bool = True,
    ) -> None:
        self._stale_after_ns = stale_after_ns
        self._hash_depth = hash_depth
        self._strict = strict_sequencing
        self.thresholds = thresholds or QualityThresholds()
        self.books: dict[tuple[str, str], CanonicalBook] = {}
        self.counters = QualityCounters()
        self.per_stream: dict[str, QualityCounters] = {}
        self.anomalies: list[dict[str, Any]] = []
        self.last_trade: dict[tuple[str, str], TradePayload] = {}
        self._last_receive_ns: int = 0
        self._first_receive_ns: int = 0
        self._stale_since: dict[tuple[str, str], int] = {}
        self.invalid_trades = 0
        self.market_data_events = 0
        self.time_integrity = TimeIntegrityMonitor(
            TimePolicy(
                max_event_latency_ns=self.thresholds.max_event_latency_ns,
                max_feed_age_ns=stale_after_ns,
                max_cross_venue_skew_ns=self.thresholds.max_cross_venue_skew_ns,
            )
        )
        self.host_clock = HostClockAligner()

    def book(self, venue: str, instrument_id: str) -> CanonicalBook:
        key = (venue, instrument_id)
        book = self.books.get(key)
        if book is None:
            book = CanonicalBook(
                venue,
                instrument_id,
                stale_after_ns=self._stale_after_ns,
                hash_depth=self._hash_depth,
                strict_sequencing=self._strict,
                max_price_jump_bps=self.thresholds.max_price_jump_bps,
                max_spread_bps=self.thresholds.max_spread_bps,
            )
            self.books[key] = book
        return book

    def process(self, event: CanonicalEvent) -> tuple[CanonicalEvent, ...]:
        """Grade one event. Returns the event plus any data-quality events."""

        key = (event.venue, event.instrument_id)
        stream = f"{event.venue}:{event.instrument_id}"
        counters = self.per_stream.setdefault(stream, QualityCounters())
        counters.events += 1
        counters.by_event_type[event.event_type.value] += 1
        self.counters.events += 1
        self.counters.by_event_type[event.event_type.value] += 1
        now = event.receive_timestamp_ns
        if not self._first_receive_ns:
            self._first_receive_ns = now

        emitted: list[CanonicalEvent] = []
        graded = event

        if now < self._last_receive_ns:
            graded = graded.with_flags(QualityFlag.CLOCK_REGRESSION)
            self._record(counters, QualityFlag.CLOCK_REGRESSION.value)
        advanced = now > self._last_receive_ns
        self._last_receive_ns = max(self._last_receive_ns, now)

        if event.event_type is CanonicalEventType.DATA_QUALITY:
            for flag in event.quality_flags:
                self._record(counters, flag.value)
            payload = event.payload
            if isinstance(payload, DataQualityPayload):
                self._anomaly(event, payload.code, payload.severity, payload.message, payload.blocking)
            counters.rejected += 1
            self.counters.rejected += 1
            return (graded,)

        if event.is_market_data:
            graded = self._grade_time(event, graded, counters)
            if advanced:
                emitted.extend(self._heartbeat(event))

        if event.event_type in {
            CanonicalEventType.BOOK_SNAPSHOT,
            CanonicalEventType.BOOK_DELTA,
        }:
            book = self.book(event.venue, event.instrument_id)
            # Staleness is measured against the *previous* update, so it must
            # be sampled before the book absorbs this event.
            previous_update_ns = book.last_update_ns
            was_initialized = book.initialized
            result = book.apply(event)
            graded = graded.with_validity(result.validity)
            if result.flags:
                graded = graded.with_flags(*result.flags)
                for flag in result.flags:
                    self._record(counters, flag.value)
                    emitted.append(
                        self._quality_event(
                            event,
                            code=flag.value,
                            severity=(
                                Severity.ERROR
                                if flag
                                in {
                                    QualityFlag.SEQUENCE_GAP,
                                    QualityFlag.CROSSED_BOOK,
                                    QualityFlag.CHECKSUM_MISMATCH,
                                    QualityFlag.SCHEMA_EXCEPTION,
                                    QualityFlag.PRICE_JUMP,
                                }
                                else Severity.WARNING
                            ),
                            message=result.reason or flag.value,
                            blocking=result.validity is BookValidity.INVALID,
                        )
                    )
            if result.applied:
                counters.accepted += 1
                self.counters.accepted += 1
            else:
                counters.rejected += 1
                self.counters.rejected += 1
            stale_ns = now - previous_update_ns if was_initialized else 0
            if stale_ns > self._stale_after_ns:
                graded = graded.with_flags(QualityFlag.STALE)
                if key not in self._stale_since:
                    # One episode per silence: a heartbeat may already have
                    # opened it, and rejected updates must not reopen it.
                    self._stale_since[key] = previous_update_ns
                    self._record(counters, QualityFlag.STALE.value)
                    emitted.append(
                        self._quality_event(
                            event,
                            code=QualityFlag.STALE.value,
                            severity=Severity.WARNING,
                            message=(
                                f"book was stale for {stale_ns / 1_000_000:.1f} ms"
                            ),
                            blocking=True,
                            details={"stale_ns": str(stale_ns)},
                        )
                    )
            if result.applied and key in self._stale_since:
                # The episode ends when the book is actually updated again.
                duration = now - self._stale_since.pop(key)
                counters.stale_intervals_ns += duration
                self.counters.stale_intervals_ns += duration
                self.counters.max_gap_ns = max(self.counters.max_gap_ns, duration)
        elif event.event_type is CanonicalEventType.TRADE:
            problem, flag = self._trade_problem(event)
            if problem:
                self.invalid_trades += 1
                graded = graded.with_flags(flag)
                self._record(counters, flag.value)
                emitted.append(
                    self._quality_event(
                        event,
                        code=flag.value,
                        severity=Severity.ERROR,
                        message=problem,
                        blocking=False,
                    )
                )
                counters.rejected += 1
                self.counters.rejected += 1
            else:
                if isinstance(event.payload, TradePayload):
                    self.last_trade[key] = event.payload
                counters.accepted += 1
                self.counters.accepted += 1
        else:
            counters.accepted += 1
            self.counters.accepted += 1

        for flag in event.quality_flags:
            if flag in PROVENANCE_FLAGS:
                counters.provenance[flag.value] += 1
                self.counters.provenance[flag.value] += 1
            else:
                self._record(counters, flag.value)

        return (graded, *emitted)

    # ------------------------------------------------------------- checks
    def _grade_time(
        self,
        event: CanonicalEvent,
        graded: CanonicalEvent,
        counters: QualityCounters,
    ) -> CanonicalEvent:
        self.market_data_events += 1
        assessment = self.time_integrity.observe(
            _TimeView(
                venue=event.venue,
                instrument_id=economic_key(event.instrument_id),
                exchange_ts_ns=event.exchange_timestamp_ns,
                receive_ts_ns=event.receive_timestamp_ns,
            ),
            now_ns=event.receive_timestamp_ns,
        )
        self.host_clock.observe(
            event.exchange_timestamp_ns / 1e9, event.receive_timestamp_ns / 1e9
        )
        flags = set(assessment.flags)
        if (
            "NEGATIVE_RECEIVE_LATENCY" in flags
            and QualityFlag.NEGATIVE_LATENCY not in event.quality_flags
        ):
            graded = graded.with_flags(QualityFlag.NEGATIVE_LATENCY)
            self._record(counters, QualityFlag.NEGATIVE_LATENCY.value)
        if "IMPOSSIBLE_EVENT_LATENCY" in flags:
            graded = graded.with_flags(QualityFlag.IMPOSSIBLE_LATENCY)
            self._record(counters, QualityFlag.IMPOSSIBLE_LATENCY.value)
        return graded

    def _trade_problem(self, event: CanonicalEvent) -> tuple[str, QualityFlag]:
        payload = event.payload
        if not isinstance(payload, TradePayload):
            return "trade_payload_missing", QualityFlag.SCHEMA_EXCEPTION
        if payload.price <= 0:
            return f"non_positive_trade_price:{payload.price}", QualityFlag.SCHEMA_EXCEPTION
        if payload.quantity <= 0:
            return (
                f"non_positive_trade_quantity:{payload.quantity}",
                QualityFlag.SCHEMA_EXCEPTION,
            )
        if payload.aggressor_side not in {None, "buy", "sell"}:
            return (
                f"unknown_aggressor_side:{payload.aggressor_side}",
                QualityFlag.SCHEMA_EXCEPTION,
            )
        book = self.books.get((event.venue, event.instrument_id))
        if book is not None and book.validity is BookValidity.VALID:
            mid = book.mid_price
            if mid is not None and mid > 0:
                deviation = abs(payload.price - mid) / mid * BPS
                if deviation > self.thresholds.max_trade_deviation_bps:
                    return (
                        f"trade {payload.price} is {deviation:.2f}bps from mid {mid}",
                        QualityFlag.TRADE_OUTSIDE_BOOK,
                    )
        return "", QualityFlag.OK

    def _heartbeat(self, source: CanonicalEvent) -> list[CanonicalEvent]:
        """Detect silence on *other* streams while time is proven to advance.

        Waiting for the silent stream's next update would mean a feed that dies
        and never returns is never flagged, so every event's receive time is
        used as a clock for every book.
        """

        emitted: list[CanonicalEvent] = []
        now = source.receive_timestamp_ns
        own = (source.venue, source.instrument_id)
        for key, book in self.books.items():
            if key == own or not book.initialized or key in self._stale_since:
                continue
            silent_ns = now - book.last_update_ns
            if silent_ns <= self._stale_after_ns:
                continue
            self._stale_since[key] = book.last_update_ns
            book.mark_stale()
            stream = f"{key[0]}:{key[1]}"
            counters = self.per_stream.setdefault(stream, QualityCounters())
            self._record(counters, QualityFlag.STALE.value)
            emitted.append(
                self._quality_event_for(
                    venue=key[0],
                    instrument_id=key[1],
                    venue_symbol=source.venue_symbol if key[0] == source.venue else key[1],
                    sequence=book.last_sequence,
                    timestamp_ns=now,
                    correlation=source.correlation,
                    code=QualityFlag.STALE.value,
                    severity=Severity.WARNING,
                    message=f"no update for {silent_ns / 1_000_000:.1f} ms",
                    blocking=True,
                    details={"silent_ns": str(silent_ns), "detected_by": "heartbeat"},
                )
            )
        return emitted

    def _record(self, counters: QualityCounters, code: str) -> None:
        counters.by_code[code] += 1
        self.counters.by_code[code] += 1

    def _anomaly(
        self,
        source: CanonicalEvent,
        code: str,
        severity: str,
        message: str,
        blocking: bool,
    ) -> None:
        self.anomalies.append(
            {
                "venue": source.venue,
                "instrument_id": source.instrument_id,
                "code": code,
                "severity": severity,
                "message": message,
                "blocking": blocking,
                "receive_timestamp_ns": source.receive_timestamp_ns,
                "source_event_id": source.event_id,
            }
        )

    def _quality_event(
        self,
        source: CanonicalEvent,
        *,
        code: str,
        severity: str,
        message: str,
        blocking: bool,
        details: dict[str, str] | None = None,
    ) -> CanonicalEvent:
        self._anomaly(source, code, severity, message, blocking)
        return CanonicalEvent(
            event_id=make_event_id(
                source.venue,
                source.instrument_id,
                CanonicalEventType.DATA_QUALITY,
                source.sequence,
                source.receive_timestamp_ns,
                code,
            ),
            event_type=CanonicalEventType.DATA_QUALITY,
            venue=source.venue,
            instrument_id=source.instrument_id,
            venue_symbol=source.venue_symbol,
            exchange_timestamp_ns=source.exchange_timestamp_ns,
            receive_timestamp_ns=source.receive_timestamp_ns,
            sequence=source.sequence,
            ingest_timestamp_ns=source.ingest_timestamp_ns,
            payload=DataQualityPayload(
                code=code,
                severity=severity,
                message=message,
                blocking=blocking,
                details=details or {},
            ),
            correlation=source.correlation,
        )

    def _quality_event_for(
        self,
        *,
        venue: str,
        instrument_id: str,
        venue_symbol: str,
        sequence: int | None,
        timestamp_ns: int,
        correlation: Any,
        code: str,
        severity: str,
        message: str,
        blocking: bool,
        details: dict[str, str],
    ) -> CanonicalEvent:
        event = CanonicalEvent(
            event_id=make_event_id(
                venue,
                instrument_id,
                CanonicalEventType.DATA_QUALITY,
                sequence,
                timestamp_ns,
                f"{code}:heartbeat",
            ),
            event_type=CanonicalEventType.DATA_QUALITY,
            venue=venue,
            instrument_id=instrument_id,
            venue_symbol=venue_symbol,
            exchange_timestamp_ns=timestamp_ns,
            receive_timestamp_ns=timestamp_ns,
            sequence=sequence,
            ingest_timestamp_ns=timestamp_ns,
            payload=DataQualityPayload(
                code=code,
                severity=severity,
                message=message,
                blocking=blocking,
                details=details,
            ),
            correlation=correlation,
        )
        self._anomaly(event, code, severity, message, blocking)
        return event

    # ------------------------------------------------------------- report
    def book_hashes(self) -> dict[str, str]:
        return {
            f"{venue}:{instrument}": book.book_hash()
            for (venue, instrument), book in sorted(self.books.items())
            if book.initialized
        }

    def silent_streams(self, as_of_ns: int | None = None) -> list[str]:
        end = self._last_receive_ns if as_of_ns is None else as_of_ns
        return sorted(
            f"{venue}:{instrument}"
            for (venue, instrument), book in self.books.items()
            if book.initialized and end - book.last_update_ns > self._stale_after_ns
        )

    def time_report(self) -> dict[str, Any]:
        report = self.time_integrity.report()
        # Wall-clock fields would make the dataset checksum non-reproducible.
        report.pop("created_at", None)
        report["host_clock_alignment"] = self.host_clock.report()
        return report

    def report(self, thresholds: QualityThresholds | None = None) -> QualityReport:
        gate = thresholds or self.thresholds
        events = self.counters.events
        codes = self.counters.by_code

        def ratio(count: int, total: int) -> Decimal:
            return Decimal(count) / Decimal(total) if total else Decimal(0)

        stale_ratio = ratio(codes.get(QualityFlag.STALE.value, 0), events)
        duplicate_ratio = ratio(codes.get(QualityFlag.DUPLICATE.value, 0), events)
        out_of_order_ratio = ratio(codes.get(QualityFlag.OUT_OF_ORDER.value, 0), events)
        book_events = sum(
            count
            for name, count in self.counters.by_event_type.items()
            if name in {"book_snapshot", "book_delta"}
        )
        wide_spread_ratio = ratio(codes.get(QualityFlag.WIDE_SPREAD.value, 0), book_events)
        negative_latency_ratio = ratio(
            codes.get(QualityFlag.NEGATIVE_LATENCY.value, 0),
            self.market_data_events,
        )
        silent = self.silent_streams()
        open_stale_ns = sum(
            self._last_receive_ns - since for since in self._stale_since.values()
        )
        span_ns = max(0, self._last_receive_ns - self._first_receive_ns)
        stale_time_ratio = (
            Decimal(self.counters.stale_intervals_ns + open_stale_ns)
            / Decimal(span_ns * max(1, len(self.books)))
            if span_ns
            else Decimal(0)
        )

        failures: list[str] = []
        gaps = codes.get(QualityFlag.SEQUENCE_GAP.value, 0)
        crossed = codes.get(QualityFlag.CROSSED_BOOK.value, 0)
        schema = codes.get(QualityFlag.SCHEMA_EXCEPTION.value, 0)
        checksum = codes.get(QualityFlag.CHECKSUM_MISMATCH.value, 0)
        jumps = codes.get(QualityFlag.PRICE_JUMP.value, 0)
        impossible = codes.get(QualityFlag.IMPOSSIBLE_LATENCY.value, 0)
        if events < gate.min_events:
            failures.append(f"insufficient_events:{events}<{gate.min_events}")
        if gaps > gate.max_sequence_gaps:
            failures.append(f"sequence_gaps:{gaps}>{gate.max_sequence_gaps}")
        if crossed > gate.max_crossed_books:
            failures.append(f"crossed_books:{crossed}>{gate.max_crossed_books}")
        if schema > gate.max_schema_exceptions:
            failures.append(
                f"schema_exceptions:{schema}>{gate.max_schema_exceptions}"
            )
        if stale_ratio > gate.max_stale_ratio:
            failures.append(f"stale_ratio:{stale_ratio}>{gate.max_stale_ratio}")
        if duplicate_ratio > gate.max_duplicate_ratio:
            failures.append(
                f"duplicate_ratio:{duplicate_ratio}>{gate.max_duplicate_ratio}"
            )
        if out_of_order_ratio > gate.max_out_of_order_ratio:
            failures.append(
                f"out_of_order_ratio:{out_of_order_ratio}>{gate.max_out_of_order_ratio}"
            )
        if checksum > gate.max_checksum_mismatches:
            failures.append(
                f"checksum_mismatches:{checksum}>{gate.max_checksum_mismatches}"
            )
        if jumps > gate.max_price_jumps:
            failures.append(f"price_jumps:{jumps}>{gate.max_price_jumps}")
        if wide_spread_ratio > gate.max_wide_spread_ratio:
            failures.append(
                f"wide_spread_ratio:{wide_spread_ratio}>{gate.max_wide_spread_ratio}"
            )
        if self.invalid_trades > gate.max_invalid_trades:
            failures.append(
                f"invalid_trades:{self.invalid_trades}>{gate.max_invalid_trades}"
            )
        if stale_time_ratio > gate.max_stale_time_ratio:
            failures.append(
                f"stale_time_ratio:{stale_time_ratio:.6f}>{gate.max_stale_time_ratio}"
            )
        if len(silent) > gate.max_silent_streams:
            failures.append(
                f"silent_streams:{','.join(silent)}"
            )
        if negative_latency_ratio > gate.max_negative_latency_ratio:
            failures.append(
                f"negative_latency_ratio:{negative_latency_ratio}"
                f">{gate.max_negative_latency_ratio}"
            )
        if impossible > gate.max_impossible_latency_events:
            failures.append(
                f"impossible_latency:{impossible}>{gate.max_impossible_latency_events}"
            )
        return QualityReport(
            approved=not failures,
            failures=tuple(failures),
            thresholds=gate,
            event_count=events,
            accepted=self.counters.accepted,
            rejected=self.counters.rejected,
            by_code=dict(sorted(codes.items())),
            by_event_type=dict(sorted(self.counters.by_event_type.items())),
            per_stream={
                stream: {
                    "events": counters.events,
                    "accepted": counters.accepted,
                    "rejected": counters.rejected,
                    "by_code": dict(sorted(counters.by_code.items())),
                    "by_event_type": dict(sorted(counters.by_event_type.items())),
                    "stale_intervals_ns": counters.stale_intervals_ns,
                }
                for stream, counters in sorted(self.per_stream.items())
            },
            book_hashes=self.book_hashes(),
            book_states={
                f"{venue}:{instrument}": book.state()
                for (venue, instrument), book in sorted(self.books.items())
            },
            anomalies=tuple(self.anomalies),
            provenance=dict(sorted(self.counters.provenance.items())),
            silent_streams=tuple(silent),
            stale_time_ratio=stale_time_ratio,
            time_integrity=self.time_report(),
        )

    def record_schema_exception(
        self,
        *,
        venue: str,
        channel: str,
        receive_timestamp_ns: int,
        reason: str,
        record_digest: str = "",
    ) -> CanonicalEvent:
        """Grade a raw record that could not be normalized at all."""

        event = CanonicalEvent(
            event_id=make_event_id(
                venue,
                f"{venue.upper()}.UNNORMALIZED",
                CanonicalEventType.DATA_QUALITY,
                None,
                receive_timestamp_ns,
                f"schema_exception:{record_digest}",
            ),
            event_type=CanonicalEventType.DATA_QUALITY,
            venue=venue,
            instrument_id=f"{venue.upper()}.UNNORMALIZED",
            venue_symbol=channel,
            exchange_timestamp_ns=receive_timestamp_ns,
            receive_timestamp_ns=receive_timestamp_ns,
            sequence=None,
            ingest_timestamp_ns=receive_timestamp_ns,
            quality_flags=(QualityFlag.SCHEMA_EXCEPTION,),
            book_validity=BookValidity.INVALID,
            payload=DataQualityPayload(
                code=QualityFlag.SCHEMA_EXCEPTION.value,
                severity=Severity.ERROR,
                message=reason,
                blocking=False,
                details={"channel": channel, "raw_sha256": record_digest},
            ),
        )
        return event


@dataclass(frozen=True, slots=True)
class QualityReport:
    approved: bool
    failures: tuple[str, ...]
    thresholds: QualityThresholds
    event_count: int
    accepted: int
    rejected: int
    by_code: dict[str, int]
    by_event_type: dict[str, int]
    per_stream: dict[str, dict[str, Any]]
    book_hashes: dict[str, str]
    book_states: dict[str, dict[str, Any]]
    anomalies: tuple[dict[str, Any], ...]
    provenance: dict[str, int] = field(default_factory=dict)
    silent_streams: tuple[str, ...] = ()
    stale_time_ratio: Decimal = Decimal(0)
    time_integrity: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "failures": list(self.failures),
            "thresholds": self.thresholds.to_dict(),
            "event_count": self.event_count,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "by_code": self.by_code,
            "by_event_type": self.by_event_type,
            "per_stream": self.per_stream,
            "book_hashes": self.book_hashes,
            "book_states": self.book_states,
            "anomaly_count": len(self.anomalies),
            "anomalies": list(self.anomalies[:200]),
            "provenance": self.provenance,
            "silent_streams": list(self.silent_streams),
            "stale_time_ratio": str(self.stale_time_ratio),
            "time_integrity": self.time_integrity,
        }


def reconstruct(
    events: Iterable[CanonicalEvent],
    *,
    stale_after_ns: int = 2_000_000_000,
    thresholds: QualityThresholds | None = None,
    strict_sequencing: bool = True,
) -> QualityReport:
    """Convenience: run a full pass and return the graded report."""

    monitor = QualityMonitor(
        stale_after_ns=stale_after_ns,
        thresholds=thresholds,
        strict_sequencing=strict_sequencing,
    )
    for event in events:
        monitor.process(event)
    return monitor.report()
