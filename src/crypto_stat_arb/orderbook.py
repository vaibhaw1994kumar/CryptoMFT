from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from .models import BookState, EventType, Level, MarketEvent, Side, object_hash


ChecksumValidator = Callable[["OrderBook", MarketEvent], bool]


@dataclass(frozen=True, slots=True)
class BookSanityPolicy:
    """Configurable price-sanity thresholds applied to every order book."""

    max_mid_jump_bps: Decimal | None = Decimal("500")
    max_spread_bps: Decimal | None = Decimal("500")
    jump_confirmation_updates: int = 3

    def __post_init__(self) -> None:
        if self.jump_confirmation_updates < 1:
            raise ValueError("jump confirmation needs at least one update")

    def new_book(self, venue: str, instrument_id: str, **kwargs) -> "OrderBook":
        return OrderBook(
            venue, instrument_id, max_mid_jump_bps=self.max_mid_jump_bps,
            max_spread_bps=self.max_spread_bps,
            jump_confirmation_updates=self.jump_confirmation_updates, **kwargs,
        )


@dataclass(slots=True)
class BookIntegrityStats:
    snapshots: int = 0
    deltas_applied: int = 0
    buffered_deltas: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    gaps: int = 0
    checksum_failures: int = 0
    crossed_or_locked: int = 0
    stale_transitions: int = 0
    resyncs: int = 0
    price_jumps: int = 0
    confirmed_price_moves: int = 0
    wide_spreads: int = 0
    invalid_levels: int = 0


@dataclass(slots=True)
class OrderBook:
    venue: str
    instrument_id: str
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    sequence: int | None = None
    last_receive_ts_ns: int | None = None
    last_event_id: str | None = None
    state: BookState = BookState.SYNCING
    checksum_validator: ChecksumValidator | None = None
    max_pending_deltas: int = 10_000
    pending_deltas: dict[int, MarketEvent] = field(default_factory=dict)
    stats: BookIntegrityStats = field(default_factory=BookIntegrityStats)
    invalid_reason: str = "awaiting_snapshot"
    recovery_pending: bool = False
    # Price-sanity policy (MR-02B). ``None`` disables a check. A mid move beyond
    # ``max_mid_jump_bps`` from the last valid mid marks the book
    # PRICE_ANOMALY until it reverts or persists for
    # ``jump_confirmation_updates`` consecutive updates (a real move).
    max_mid_jump_bps: Decimal | None = Decimal("500")
    max_spread_bps: Decimal | None = Decimal("500")
    jump_confirmation_updates: int = 3
    reference_mid: Decimal | None = None
    jump_streak: int = 0
    chained_delta: bool = False         # a delta has been applied since the last snapshot
    # Highest sequence dropped because the delta buffer was full: a snapshot older than
    # this cannot restore the book (those updates are known to be missing).
    dropped_through: int | None = None

    def start_resync(self, reason: str = "connection_resynchronizing") -> None:
        """Discard an unsafe native state and require a new venue snapshot."""
        self.recovery_pending = self.recovery_pending or self.sequence is not None
        self.bids.clear()
        self.asks.clear()
        self.sequence = None
        self.chained_delta = False
        self.dropped_through = None
        self.last_receive_ts_ns = None
        self.last_event_id = None
        self.pending_deltas.clear()
        self.state = BookState.SYNCING
        self.invalid_reason = reason

    def apply(self, event: MarketEvent) -> BookState:
        if (event.venue, event.instrument_id) != (self.venue, self.instrument_id):
            raise ValueError("event routed to wrong book")
        if event.event_id == self.last_event_id:
            self.stats.duplicates += 1
            return self.state
        if event.event_type in (EventType.BOOK_SNAPSHOT, EventType.BOOK_DELTA):
            invalid = self._invalid_level(event)
            if invalid:
                # Never apply a malformed update: the sequence does not advance,
                # so the book stays invalid until a fresh snapshot arrives.
                self.stats.invalid_levels += 1
                self.state = BookState.INVALID_DATA
                self.invalid_reason = invalid
                return self.state
        if event.event_type == EventType.BOOK_SNAPSHOT:
            return self._apply_snapshot(event)
        elif event.event_type == EventType.BOOK_DELTA:
            if self.sequence is None:
                self._buffer(event)
                if self.dropped_through is None:     # an overflow stays a GAP
                    self.state = BookState.SYNCING
                    self.invalid_reason = "awaiting_snapshot"
                return self.state
            if event.sequence <= self.sequence:
                if event.sequence == self.sequence:
                    self.stats.duplicates += 1
                else:
                    self.stats.out_of_order += 1
                return self.state
            if self.state in (
                BookState.GAP_DETECTED, BookState.CHECKSUM_FAILED,
                BookState.INVALID_DATA,
            ):
                self._buffer(event)
                return self.state
            if self._breaks_chain(event):
                self.state = BookState.GAP_DETECTED
                self.invalid_reason = (
                    f"sequence_gap:{self.sequence}->{event.sequence_start}-{event.sequence}"
                )
                self.stats.gaps += 1
                self._buffer(event)
                return self.state
            return self._apply_delta(event)
        else:
            return self.state

    def _apply_snapshot(self, event: MarketEvent) -> BookState:
        was_recovery = (
            self.recovery_pending or self.sequence is not None or bool(self.pending_deltas)
        )
        self.recovery_pending = False
        self.bids = {x.price: x.quantity for x in event.bids if x.quantity > 0}
        self.asks = {x.price: x.quantity for x in event.asks if x.quantity > 0}
        self.sequence = event.sequence
        self.chained_delta = False
        self.last_receive_ts_ns = event.receive_ts_ns
        self.last_event_id = event.event_id
        self.stats.snapshots += 1
        if was_recovery:
            self.stats.resyncs += 1
        self.pending_deltas = {
            sequence: delta for sequence, delta in self.pending_deltas.items()
            if sequence > event.sequence
        }
        if self.dropped_through is not None:
            if event.sequence < self.dropped_through:
                self.state = BookState.GAP_DETECTED
                self.invalid_reason = (
                    f"snapshot_does_not_cover_dropped_deltas:{event.sequence}<{self.dropped_through}"
                )
                self.stats.gaps += 1
                return self.state
            self.dropped_through = None
        self.state = self._validate(event)
        if self.state != BookState.VALID:
            return self.state
        for sequence in sorted(self.pending_deltas):
            delta = self.pending_deltas[sequence]
            if self._breaks_chain(delta):
                self.state = BookState.GAP_DETECTED
                self.invalid_reason = (
                    f"buffered_sequence_gap:{self.sequence}->{delta.sequence_start}-{sequence}"
                )
                self.stats.gaps += 1
                return self.state
            self._apply_delta(delta)
            if self.state != BookState.VALID:
                return self.state
        self.pending_deltas.clear()
        return self.state

    def _breaks_chain(self, delta: MarketEvent) -> bool:
        """Binance USDⓈ-M deltas carry ``pu`` (the previous delta's final id) and
        their ``U`` is not ``pu + 1``: after the first delta that bridges the
        snapshot, continuity is ``pu == last applied u``. Other feeds: ``U <= last + 1``.
        """
        previous = delta.native.get("pu") if isinstance(delta.native, dict) else None
        if previous is not None and self.chained_delta:
            return int(previous) != self.sequence
        return delta.sequence_start > (self.sequence or 0) + 1

    def _apply_delta(self, event: MarketEvent) -> BookState:
        self._update(self.bids, event.bids)
        self._update(self.asks, event.asks)
        self.sequence = event.sequence
        self.chained_delta = True
        self.last_receive_ts_ns = event.receive_ts_ns
        self.last_event_id = event.event_id
        self.stats.deltas_applied += 1
        self.pending_deltas.pop(event.sequence, None)
        self.state = self._validate(event)
        return self.state

    def _buffer(self, event: MarketEvent) -> None:
        if event.sequence in self.pending_deltas:
            self.stats.duplicates += 1
            return
        if len(self.pending_deltas) >= self.max_pending_deltas:
            if self.dropped_through is None:
                self.stats.gaps += 1                  # one gap per overflow episode
            self.dropped_through = max(self.dropped_through or 0, event.sequence)
            self.state = BookState.GAP_DETECTED
            self.invalid_reason = "delta_buffer_overflow"
            return
        self.pending_deltas[event.sequence] = event
        self.stats.buffered_deltas += 1

    @staticmethod
    def _invalid_level(event: MarketEvent) -> str:
        for level in (*event.bids, *event.asks):
            if not (level.price.is_finite() and level.quantity.is_finite()):
                return "non_finite_level"
            if level.price <= 0:
                return "non_positive_price"
            if level.quantity < 0:
                return "negative_quantity"
        return ""

    @staticmethod
    def _update(side: dict[Decimal, Decimal], levels: tuple[Level, ...]) -> None:
        for level in levels:
            if level.quantity <= 0:
                side.pop(level.price, None)
            else:
                side[level.price] = level.quantity

    def _validate(self, event: MarketEvent) -> BookState:
        if not self.bids or not self.asks:
            self.invalid_reason = "empty_book_side"
            return BookState.SYNCING
        if self.best_bid >= self.best_ask:
            self.stats.crossed_or_locked += 1
            self.invalid_reason = "crossed_or_locked_book"
            return BookState.CHECKSUM_FAILED
        if event.checksum:
            if self.checksum_validator is None or not self.checksum_validator(self, event):
                self.stats.checksum_failures += 1
                self.invalid_reason = "checksum_failed_or_unsupported"
                return BookState.CHECKSUM_FAILED
        mid = (self.best_bid + self.best_ask) / Decimal("2")
        if self.max_spread_bps is not None:
            spread_bps = (self.best_ask - self.best_bid) / mid * Decimal("10000")
            if spread_bps > self.max_spread_bps:
                if self.invalid_reason != "wide_spread":
                    self.stats.wide_spreads += 1
                self.invalid_reason = "wide_spread"
                return BookState.PRICE_ANOMALY
        if self.max_mid_jump_bps is not None and self.reference_mid is not None:
            jump_bps = abs(mid - self.reference_mid) / self.reference_mid * Decimal("10000")
            if jump_bps > self.max_mid_jump_bps:
                self.jump_streak += 1
                if self.jump_streak < self.jump_confirmation_updates:
                    if self.jump_streak == 1:
                        self.stats.price_jumps += 1
                    self.invalid_reason = "price_jump"
                    return BookState.PRICE_ANOMALY
                self.stats.confirmed_price_moves += 1
        self.jump_streak = 0
        self.reference_mid = mid
        self.invalid_reason = ""
        return BookState.VALID

    @property
    def best_bid(self) -> Decimal:
        return max(self.bids)

    @property
    def best_ask(self) -> Decimal:
        return min(self.asks)

    def mark_stale(self, now_ns: int, max_age_ns: int) -> None:
        if self.last_receive_ts_ns is not None:
            self.mark_stale_age(now_ns - self.last_receive_ts_ns, max_age_ns)

    def mark_stale_age(self, age_ns: int, max_age_ns: int) -> None:
        """Stale when the last applied update is older than ``max_age_ns``.

        Live callers measure ``age_ns`` on a monotonic clock, so a wall-clock
        step cannot make a silent book look fresh (or a fresh one look stale).
        """
        if self.state == BookState.VALID and age_ns > max_age_ns:
            self.stats.stale_transitions += 1
            self.state = BookState.STALE
            self.invalid_reason = "market_data_stale"

    @property
    def is_tradable(self) -> bool:
        return self.state == BookState.VALID

    def available(self, side: Side, limit_price: Decimal) -> Decimal:
        levels = self.asks if side == Side.BUY else self.bids
        if side == Side.BUY:
            return sum((qty for price, qty in levels.items() if price <= limit_price), Decimal("0"))
        return sum((qty for price, qty in levels.items() if price >= limit_price), Decimal("0"))

    def walk(self, side: Side, quantity: Decimal, limit_price: Decimal) -> tuple[Decimal, Decimal]:
        levels = self.asks if side == Side.BUY else self.bids
        prices = sorted(levels) if side == Side.BUY else sorted(levels, reverse=True)
        remaining = quantity
        notional = Decimal("0")
        filled = Decimal("0")
        for price in prices:
            if side == Side.BUY and price > limit_price:
                break
            if side == Side.SELL and price < limit_price:
                break
            take = min(remaining, levels[price])
            filled += take
            notional += take * price
            remaining -= take
            if remaining == 0:
                break
        return filled, (notional / filled if filled else Decimal("0"))

    def snapshot_hash(self) -> str:
        return object_hash({
            "venue": self.venue,
            "instrument_id": self.instrument_id,
            "sequence": self.sequence,
            "bids": sorted((str(k), str(v)) for k, v in self.bids.items()),
            "asks": sorted((str(k), str(v)) for k, v in self.asks.items()),
            "state": self.state,
        })
