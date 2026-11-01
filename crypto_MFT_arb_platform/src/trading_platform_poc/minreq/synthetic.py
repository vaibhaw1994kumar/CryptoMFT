"""Deterministic synthetic multi-venue market data.

Live venues are not reachable from every environment and hermetic tests forbid
outbound sockets, so acceptance evidence is produced from a seeded generator
that emits *venue-native* raw frames. The whole raw-to-canonical path is
therefore exercised exactly as it would be for a live capture.

The price process is a common random walk plus a mean-reverting cross-venue
basis, which is what makes the stat-arb strategy have something to trade.
"""

from __future__ import annotations

import dataclasses
import random
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterator

from trading_platform_poc.minreq.securitymaster import (
    SecurityMaster,
    default_security_master,
)
from trading_platform_poc.minreq.checksums import okx_book_checksum
from trading_platform_poc.minreq.venues import (
    ADAPTER_VERSION,
    SYNTHETIC_HOST,
    RawRecord,
    venue_symbol_for,
)
from trading_platform_poc.minreq.canonical import SCHEMA_VERSION

MS_NS = 1_000_000


@dataclass(frozen=True, slots=True)
class FaultInjection:
    """Controlled defects so gap, stale and crossed handling can be proven."""

    sequence_gap_at: int | None = None
    stale_gap_at: int | None = None
    stale_gap_ms: int = 6_000
    crossed_book_at: int | None = None
    duplicate_at: int | None = None
    suspend_instrument_at: int | None = None
    resume_instrument_at: int | None = None
    checksum_mismatch_at: int | None = None
    price_jump_at: int | None = None
    price_jump_factor: Decimal = Decimal("1.2")
    invalid_trade_at: int | None = None
    schema_exception_at: int | None = None
    silent_venue_after: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence_gap_at": self.sequence_gap_at,
            "stale_gap_at": self.stale_gap_at,
            "stale_gap_ms": self.stale_gap_ms,
            "crossed_book_at": self.crossed_book_at,
            "duplicate_at": self.duplicate_at,
            "suspend_instrument_at": self.suspend_instrument_at,
            "resume_instrument_at": self.resume_instrument_at,
            "checksum_mismatch_at": self.checksum_mismatch_at,
            "price_jump_at": self.price_jump_at,
            "price_jump_factor": str(self.price_jump_factor),
            "invalid_trade_at": self.invalid_trade_at,
            "schema_exception_at": self.schema_exception_at,
            "silent_venue_after": self.silent_venue_after,
        }


@dataclass(frozen=True, slots=True)
class SyntheticConfig:
    """Every field participates in the seed-stable output."""

    seed: int = 20260817
    symbol: str = "BTCUSDT"
    venues: tuple[str, ...] = ("binance", "okx")
    start_timestamp_ns: int = 1_755_388_800_000_000_000  # 2025-08-17T00:00:00Z
    steps: int = 400
    step_interval_ms: int = 100
    base_price: Decimal = Decimal("60000")
    volatility: Decimal = Decimal("3.0")
    basis_mean_reversion: Decimal = Decimal("0.15")
    basis_volatility: Decimal = Decimal("18.0")
    half_spread: Decimal = Decimal("1.5")
    depth_levels: int = 10
    level_step: Decimal = Decimal("1.0")
    base_quantity: Decimal = Decimal("0.5")
    trade_every: int = 7
    latency_ms: int = 3
    faults: FaultInjection = field(default_factory=FaultInjection)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "symbol": self.symbol,
            "venues": list(self.venues),
            "start_timestamp_ns": self.start_timestamp_ns,
            "steps": self.steps,
            "step_interval_ms": self.step_interval_ms,
            "base_price": str(self.base_price),
            "volatility": str(self.volatility),
            "basis_mean_reversion": str(self.basis_mean_reversion),
            "basis_volatility": str(self.basis_volatility),
            "half_spread": str(self.half_spread),
            "depth_levels": self.depth_levels,
            "level_step": str(self.level_step),
            "base_quantity": str(self.base_quantity),
            "trade_every": self.trade_every,
            "latency_ms": self.latency_ms,
            "faults": self.faults.to_dict(),
        }


@dataclass
class _VenueBook:
    venue: str
    venue_symbol: str
    tick: Decimal
    sequence: int = 0
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)


def _quantize(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value() * tick


class SyntheticMarket:
    """Generates seed-stable venue-native raw frames for two or more venues."""

    def __init__(
        self,
        config: SyntheticConfig | None = None,
        security_master: SecurityMaster | None = None,
    ) -> None:
        self.config = config or SyntheticConfig()
        self.security_master = security_master or default_security_master()
        self._books: dict[str, _VenueBook] = {}
        for venue in self.config.venues:
            venue_symbol = venue_symbol_for(venue, self.config.symbol)
            record = self.security_master.resolve_venue_symbol(
                venue, venue_symbol, self.config.start_timestamp_ns
            )
            self._books[venue] = _VenueBook(
                venue=venue, venue_symbol=venue_symbol, tick=record.tick_size
            )

    # ------------------------------------------------------------- generation
    def records(self) -> Iterator[RawRecord]:
        cfg = self.config
        rng = random.Random(cfg.seed)
        mid = cfg.base_price
        basis = Decimal(0)
        stale_offset_ns = 0
        trade_id = 1_000_000
        suspended: set[str] = set()

        for step in range(cfg.steps):
            mid += Decimal(repr(round(rng.gauss(0.0, float(cfg.volatility)), 6)))
            drift = -cfg.basis_mean_reversion * basis
            shock = Decimal(repr(round(rng.gauss(0.0, float(cfg.basis_volatility)), 6)))
            basis += drift + shock
            if cfg.faults.stale_gap_at is not None and step == cfg.faults.stale_gap_at:
                stale_offset_ns += cfg.faults.stale_gap_ms * MS_NS
            exchange_ns = (
                cfg.start_timestamp_ns
                + step * cfg.step_interval_ms * MS_NS
                + stale_offset_ns
            )
            receive_ns = exchange_ns + cfg.latency_ms * MS_NS

            if (
                cfg.faults.suspend_instrument_at is not None
                and step == cfg.faults.suspend_instrument_at
            ):
                venue = cfg.venues[0]
                suspended.add(venue)
                yield self._instrument_status(
                    venue, "suspended", exchange_ns, receive_ns
                )
            if (
                cfg.faults.resume_instrument_at is not None
                and step == cfg.faults.resume_instrument_at
            ):
                venue = cfg.venues[0]
                suspended.discard(venue)
                yield self._instrument_status(
                    venue, "trading", exchange_ns, receive_ns
                )

            for index, venue in enumerate(cfg.venues):
                if self._silenced(index, step):
                    continue
                offset = basis / 2 if index else -basis / 2
                venue_mid = mid + offset
                if (
                    cfg.faults.price_jump_at is not None
                    and step == cfg.faults.price_jump_at
                    and index == 0
                ):
                    venue_mid = venue_mid * cfg.faults.price_jump_factor
                crossed = (
                    cfg.faults.crossed_book_at is not None
                    and step == cfg.faults.crossed_book_at
                    and index == 0
                )
                frames = self._book_frames(
                    venue,
                    venue_mid,
                    step,
                    exchange_ns,
                    receive_ns,
                    rng,
                    crossed=crossed,
                    corrupt_checksum=(
                        cfg.faults.checksum_mismatch_at is not None
                        and step == cfg.faults.checksum_mismatch_at
                        and venue == "okx"
                    ),
                )
                if (
                    cfg.faults.schema_exception_at is not None
                    and step == cfg.faults.schema_exception_at
                    and index == 0
                ):
                    frames.append(self._malformed_frame(venue, exchange_ns, receive_ns))
                if (
                    cfg.faults.invalid_trade_at is not None
                    and step == cfg.faults.invalid_trade_at
                    and index == 0
                ):
                    trade_id += 1
                    frames.append(
                        self._trade_frame(
                            venue,
                            trade_id,
                            Decimal("-5"),
                            Decimal("0.1"),
                            "buy",
                            exchange_ns,
                            receive_ns,
                        )
                    )
                for frame in frames:
                    yield frame
                    if (
                        cfg.faults.duplicate_at is not None
                        and step == cfg.faults.duplicate_at
                        and index == 0
                    ):
                        # Same venue sequence, later arrival: a genuine venue
                        # redelivery rather than a byte-identical raw record.
                        yield dataclasses.replace(
                            frame,
                            receive_timestamp_ns=frame.receive_timestamp_ns + 1,
                        )

            if cfg.trade_every and step % cfg.trade_every == 0 and step > 0:
                for index, venue in enumerate(cfg.venues):
                    if self._silenced(index, step):
                        continue
                    book = self._books[venue]
                    if not book.bids or not book.asks:
                        continue
                    buy = rng.random() < 0.5
                    price = min(book.asks) if buy else max(book.bids)
                    scale = Decimal(repr(round(rng.uniform(0.1, 1.0), 4)))
                    quantity = _quantize(
                        cfg.base_quantity * scale,
                        Decimal("0.00001"),
                    )
                    if quantity <= 0:
                        continue
                    trade_id += 1
                    yield self._trade_frame(
                        venue,
                        trade_id,
                        price,
                        quantity,
                        "buy" if buy else "sell",
                        exchange_ns,
                        receive_ns,
                    )

    # ---------------------------------------------------------------- frames
    def _ladder(
        self, mid: Decimal, tick: Decimal, rng: random.Random
    ) -> tuple[dict[Decimal, Decimal], dict[Decimal, Decimal]]:
        cfg = self.config
        bids: dict[Decimal, Decimal] = {}
        asks: dict[Decimal, Decimal] = {}
        for level in range(cfg.depth_levels):
            step = cfg.level_step * Decimal(level)
            bid_price = _quantize(mid - cfg.half_spread - step, tick)
            ask_price = _quantize(mid + cfg.half_spread + step, tick)
            size = _quantize(
                cfg.base_quantity
                * Decimal(repr(round(rng.uniform(0.4, 2.5), 4)))
                * (Decimal(1) + Decimal(level) / Decimal(4)),
                Decimal("0.00001"),
            )
            if size <= 0:
                size = Decimal("0.00001")
            if bid_price > 0:
                bids[bid_price] = size
            asks[ask_price] = size
        return bids, asks

    def _book_frames(
        self,
        venue: str,
        mid: Decimal,
        step: int,
        exchange_ns: int,
        receive_ns: int,
        rng: random.Random,
        *,
        crossed: bool,
        corrupt_checksum: bool = False,
    ) -> list[RawRecord]:
        cfg = self.config
        book = self._books[venue]
        bids, asks = self._ladder(mid, book.tick, rng)
        if crossed:
            best_ask = min(asks)
            crossed_bid = best_ask + book.tick
            bids = {crossed_bid: cfg.base_quantity, **bids}

        snapshot = step == 0
        gap = (
            cfg.faults.sequence_gap_at is not None
            and step == cfg.faults.sequence_gap_at
            and venue == cfg.venues[0]
        )
        # A defect must be followed by a re-sync snapshot, otherwise the book
        # can only ever be flagged, never recovered.
        resync = (
            venue == cfg.venues[0]
            and step - 1
            in {
                value
                for value in (cfg.faults.sequence_gap_at, cfg.faults.crossed_book_at)
                if value is not None
            }
        ) or (
            venue == "okx"
            and cfg.faults.checksum_mismatch_at is not None
            and step - 1 == cfg.faults.checksum_mismatch_at
        )
        previous_sequence = book.sequence
        jump = 25 if gap else 0
        book.sequence += 1 + jump
        first_sequence = previous_sequence + 1 + jump

        if snapshot or resync:
            book.bids, book.asks = dict(bids), dict(asks)
            return [
                self._encode_book(
                    venue,
                    bids,
                    asks,
                    first_sequence,
                    book.sequence,
                    exchange_ns,
                    receive_ns,
                    snapshot=True,
                    corrupt_checksum=corrupt_checksum,
                )
            ]

        delta_bids = _diff(book.bids, bids)
        delta_asks = _diff(book.asks, asks)
        book.bids, book.asks = dict(bids), dict(asks)
        return [
            self._encode_book(
                venue,
                delta_bids,
                delta_asks,
                first_sequence,
                book.sequence,
                exchange_ns,
                receive_ns,
                snapshot=False,
                corrupt_checksum=corrupt_checksum,
            )
        ]

    def _encode_book(
        self,
        venue: str,
        bids: dict[Decimal, Decimal],
        asks: dict[Decimal, Decimal],
        first_sequence: int,
        sequence: int,
        exchange_ns: int,
        receive_ns: int,
        *,
        snapshot: bool,
        corrupt_checksum: bool = False,
    ) -> RawRecord:
        book = self._books[venue]
        bid_rows = [[str(p), str(q)] for p, q in sorted(bids.items(), reverse=True)]
        ask_rows = [[str(p), str(q)] for p, q in sorted(asks.items())]
        exchange_ms = exchange_ns // MS_NS

        if venue == "binance":
            if snapshot:
                message = {
                    "lastUpdateId": sequence,
                    "bids": bid_rows,
                    "asks": ask_rows,
                    "symbol": book.venue_symbol,
                }
                channel = "depth_snapshot"
            else:
                message = {
                    "stream": f"{book.venue_symbol.lower()}@depth@100ms",
                    "data": {
                        "e": "depthUpdate",
                        "E": exchange_ms,
                        "s": book.venue_symbol,
                        "U": first_sequence,
                        "u": sequence,
                        "b": bid_rows,
                        "a": ask_rows,
                    },
                }
                channel = "depth"
        elif venue == "okx":
            # OKX's CRC32 covers the book *after* this message is applied,
            # which is the generator's own book state at this point.
            checksum = okx_book_checksum(book.bids, book.asks)
            if corrupt_checksum:
                checksum += 1
            message = {
                "arg": {"channel": "books", "instId": book.venue_symbol},
                "action": "snapshot" if snapshot else "update",
                "data": [
                    {
                        "bids": [[p, q, "0", "1"] for p, q in bid_rows],
                        "asks": [[p, q, "0", "1"] for p, q in ask_rows],
                        "ts": str(exchange_ms),
                        "seqId": sequence,
                        "prevSeqId": first_sequence - 1,
                        "checksum": checksum,
                    }
                ],
            }
            channel = "books"
        else:  # bybit
            message = {
                "topic": f"orderbook.50.{book.venue_symbol}",
                "type": "snapshot" if snapshot else "delta",
                "ts": exchange_ms,
                "data": {
                    "s": book.venue_symbol,
                    "b": bid_rows,
                    "a": ask_rows,
                    "u": sequence,
                    "seq": sequence,
                },
            }
            channel = "orderbook"

        return RawRecord(
            venue=venue,
            channel=channel,
            host=SYNTHETIC_HOST,
            receive_timestamp_ns=receive_ns,
            sequence=sequence,
            adapter_version=ADAPTER_VERSION,
            schema_version=SCHEMA_VERSION,
            message=message,
        )

    def _silenced(self, index: int, step: int) -> bool:
        faults = self.config.faults
        return (
            faults.silent_venue_after is not None
            and len(self.config.venues) > 1
            and index == len(self.config.venues) - 1
            and step > faults.silent_venue_after
        )

    def _malformed_frame(
        self, venue: str, exchange_ns: int, receive_ns: int
    ) -> RawRecord:
        """A venue frame whose price field is not a number."""

        book = self._books[venue]
        return self._encode_book(
            venue,
            {"not-a-number": Decimal("1")},  # type: ignore[dict-item]
            {},
            book.sequence,
            book.sequence,
            exchange_ns,
            receive_ns,
            snapshot=False,
        )

    def _trade_frame(
        self,
        venue: str,
        trade_id: int,
        price: Decimal,
        quantity: Decimal,
        side: str,
        exchange_ns: int,
        receive_ns: int,
    ) -> RawRecord:
        book = self._books[venue]
        exchange_ms = exchange_ns // MS_NS
        if venue == "binance":
            message = {
                "stream": f"{book.venue_symbol.lower()}@trade",
                "data": {
                    "e": "trade",
                    "E": exchange_ms,
                    "s": book.venue_symbol,
                    "t": trade_id,
                    "p": str(price),
                    "q": str(quantity),
                    "T": exchange_ms,
                    "m": side == "sell",
                },
            }
            channel = "trade"
        elif venue == "okx":
            message = {
                "arg": {"channel": "trades", "instId": book.venue_symbol},
                "data": [
                    {
                        "instId": book.venue_symbol,
                        "tradeId": str(trade_id),
                        "px": str(price),
                        "sz": str(quantity),
                        "side": side,
                        "ts": str(exchange_ms),
                    }
                ],
            }
            channel = "trades"
        else:
            message = {
                "topic": f"publicTrade.{book.venue_symbol}",
                "type": "snapshot",
                "ts": exchange_ms,
                "data": [
                    {
                        "T": exchange_ms,
                        "s": book.venue_symbol,
                        "S": side.capitalize(),
                        "v": str(quantity),
                        "p": str(price),
                        "i": str(trade_id),
                        "BT": False,
                    }
                ],
            }
            channel = "publicTrade"
        return RawRecord(
            venue=venue,
            channel=channel,
            host=SYNTHETIC_HOST,
            receive_timestamp_ns=receive_ns,
            sequence=trade_id,
            adapter_version=ADAPTER_VERSION,
            schema_version=SCHEMA_VERSION,
            message=message,
        )

    def _instrument_status(
        self, venue: str, status: str, exchange_ns: int, receive_ns: int
    ) -> RawRecord:
        book = self._books[venue]
        exchange_ms = exchange_ns // MS_NS
        if venue == "binance":
            message = {
                "symbol": book.venue_symbol,
                "status": "BREAK" if status == "suspended" else "TRADING",
                "timestamp_ns": exchange_ns,
            }
            channel = "instrument"
        elif venue == "okx":
            message = {
                "arg": {"channel": "instruments", "instType": "SPOT"},
                "data": [
                    {
                        "instId": book.venue_symbol,
                        "state": "suspend" if status == "suspended" else "live",
                        "ts": str(exchange_ms),
                    }
                ],
            }
            channel = "instruments"
        else:
            message = {
                "topic": "instrument_info",
                "ts": exchange_ms,
                "data": {
                    "s": book.venue_symbol,
                    "status": "Settling" if status == "suspended" else "Trading",
                },
            }
            channel = "instrument"
        return RawRecord(
            venue=venue,
            channel=channel,
            host=SYNTHETIC_HOST,
            receive_timestamp_ns=receive_ns,
            sequence=None,
            adapter_version=ADAPTER_VERSION,
            schema_version=SCHEMA_VERSION,
            message=message,
        )


def _diff(
    previous: dict[Decimal, Decimal], current: dict[Decimal, Decimal]
) -> dict[Decimal, Decimal]:
    """Delta that turns ``previous`` into ``current``; zero removes a level."""

    delta: dict[Decimal, Decimal] = {}
    for price, quantity in current.items():
        if previous.get(price) != quantity:
            delta[price] = quantity
    for price in previous:
        if price not in current:
            delta[price] = Decimal(0)
    return delta


def generate(config: SyntheticConfig | None = None) -> list[RawRecord]:
    return list(SyntheticMarket(config).records())
