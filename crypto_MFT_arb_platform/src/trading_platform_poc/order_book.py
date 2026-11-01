from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import TypeAlias

from trading_platform_poc.models import BookDelta, BookSnapshot, PriceLevel


class OrderBookError(Exception):
    pass


class SequenceGapError(OrderBookError):
    pass


class InvalidBookError(OrderBookError):
    pass


OrderBookState: TypeAlias = tuple[BookSnapshot, int | None] | None


class BinanceOrderBook:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol.upper()
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self.last_update_id: int | None = None
        self.last_receive_timestamp_ns: int | None = None

    @property
    def initialized(self) -> bool:
        return self.last_update_id is not None

    @property
    def best_bid(self) -> Decimal | None:
        return max(self._bids, default=None)

    @property
    def best_ask(self) -> Decimal | None:
        return min(self._asks, default=None)

    @property
    def mid_price(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / Decimal(2)

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    def spread_bps(self) -> Decimal | None:
        spread = self.spread
        mid_price = self.mid_price
        if spread is None or mid_price is None or mid_price == 0:
            return None
        return spread / mid_price * Decimal(10_000)

    @property
    def bid_depth(self) -> int:
        return len(self._bids)

    @property
    def ask_depth(self) -> int:
        return len(self._asks)

    def load_snapshot(
        self,
        snapshot: BookSnapshot,
        receive_timestamp_ns: int,
    ) -> None:
        self._require_symbol(snapshot.symbol)
        bids = self._build_side(snapshot.bids)
        asks = self._build_side(snapshot.asks)
        self._validate_sides(bids, asks)
        self._bids = bids
        self._asks = asks
        self.last_update_id = snapshot.last_update_id
        self.last_receive_timestamp_ns = receive_timestamp_ns

    def apply_delta(
        self,
        delta: BookDelta,
        receive_timestamp_ns: int,
    ) -> bool:
        self._require_symbol(delta.symbol)
        if self.last_update_id is None:
            raise OrderBookError("snapshot must be loaded before deltas")
        expected_update_id = self.last_update_id + 1
        if delta.final_update_id < expected_update_id:
            return False
        if delta.first_update_id > expected_update_id:
            raise SequenceGapError(
                f"expected update {expected_update_id}, "
                f"received {delta.first_update_id}-{delta.final_update_id}"
            )
        bids = dict(self._bids)
        asks = dict(self._asks)
        self._apply_levels(bids, delta.bids)
        self._apply_levels(asks, delta.asks)
        self._validate_sides(bids, asks)
        self._bids = bids
        self._asks = asks
        self.last_update_id = delta.final_update_id
        self.last_receive_timestamp_ns = receive_timestamp_ns
        return True

    def is_stale(self, now_ns: int, stale_after_ms: int) -> bool:
        if self.last_receive_timestamp_ns is None:
            return True
        threshold_ns = stale_after_ms * 1_000_000
        return now_ns - self.last_receive_timestamp_ns > threshold_ns

    def top_bids(self, depth: int = 10) -> tuple[PriceLevel, ...]:
        return tuple(
            PriceLevel(price=price, quantity=self._bids[price])
            for price in sorted(self._bids, reverse=True)[:depth]
        )

    def top_asks(self, depth: int = 10) -> tuple[PriceLevel, ...]:
        return tuple(
            PriceLevel(price=price, quantity=self._asks[price])
            for price in sorted(self._asks)[:depth]
        )

    def checksum(self) -> str:
        digest = hashlib.sha256()
        for side, levels in (("B", self.top_bids(len(self._bids))), ("A", self.top_asks(len(self._asks)))):
            for level in levels:
                digest.update(
                    f"{side}:{level.price}:{level.quantity}\n".encode("ascii")
                )
        return digest.hexdigest()

    def snapshot(self) -> BookSnapshot:
        if self.last_update_id is None:
            raise OrderBookError("book is not initialized")
        return BookSnapshot(
            symbol=self.symbol,
            last_update_id=self.last_update_id,
            bids=self.top_bids(len(self._bids)),
            asks=self.top_asks(len(self._asks)),
        )

    def capture_state(self) -> OrderBookState:
        if not self.initialized:
            return None
        return self.snapshot(), self.last_receive_timestamp_ns

    def restore_state(self, state: OrderBookState) -> None:
        if state is None:
            self._bids.clear()
            self._asks.clear()
            self.last_update_id = None
            self.last_receive_timestamp_ns = None
            return
        snapshot, receive_timestamp_ns = state
        self.load_snapshot(
            snapshot,
            receive_timestamp_ns or 0,
        )

    def _validate(self) -> None:
        self._validate_sides(self._bids, self._asks)

    @staticmethod
    def _validate_sides(
        bids: dict[Decimal, Decimal],
        asks: dict[Decimal, Decimal],
    ) -> None:
        best_bid = max(bids, default=None)
        best_ask = min(asks, default=None)
        if best_bid is None or best_ask is None:
            raise InvalidBookError("both sides of the order book must be populated")
        if best_bid >= best_ask:
            raise InvalidBookError(
                f"crossed book: best bid {best_bid}, best ask {best_ask}"
            )

    def _require_symbol(self, symbol: str) -> None:
        if symbol.upper() != self.symbol:
            raise ValueError(
                f"book symbol {self.symbol} does not match event symbol {symbol}"
            )

    @staticmethod
    def _build_side(levels: tuple[PriceLevel, ...]) -> dict[Decimal, Decimal]:
        side: dict[Decimal, Decimal] = {}
        for level in levels:
            if (
                not level.price.is_finite()
                or not level.quantity.is_finite()
                or level.price <= 0
                or level.quantity <= 0
            ):
                raise InvalidBookError(
                    "snapshot prices and quantities must be finite and positive"
                )
            if level.price in side:
                raise InvalidBookError(
                    f"duplicate snapshot price level: {level.price}"
                )
            side[level.price] = level.quantity
        return side

    @staticmethod
    def _apply_levels(
        side: dict[Decimal, Decimal],
        levels: tuple[PriceLevel, ...],
    ) -> None:
        for level in levels:
            if (
                not level.price.is_finite()
                or not level.quantity.is_finite()
                or level.price <= 0
                or level.quantity < 0
            ):
                raise InvalidBookError(
                    "delta prices and quantities must be finite; prices must "
                    "be positive and quantities non-negative"
                )
            if level.quantity == 0:
                side.pop(level.price, None)
            else:
                side[level.price] = level.quantity
