from __future__ import annotations

from collections import Counter, OrderedDict
from decimal import Decimal
from enum import Enum
from queue import Empty, Full, Queue
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog

from trading_platform_poc.config import DataQualityConfig
from trading_platform_poc.models import (
    BookDelta,
    BookSnapshot,
    EventType,
    MarketEvent,
    OrderState,
    PriceLevel,
    Side,
    TradeTick,
)
from trading_platform_poc.order_book import BinanceOrderBook

if TYPE_CHECKING:
    from trading_platform_poc.oms import OrderManagementSystem


logger = structlog.get_logger()


class AnomalySeverity(str, Enum):
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AnomalyCode(str, Enum):
    INVALID_TIMESTAMP = "invalid_timestamp"
    FUTURE_TIMESTAMP = "future_timestamp"
    STALE_MARKET_DATA = "stale_market_data"
    TIMESTAMP_REGRESSION = "timestamp_regression"
    TIMESTAMP_ORDERING = "timestamp_ordering"
    SYMBOL_MISMATCH = "symbol_mismatch"
    INVALID_SEQUENCE = "invalid_sequence"
    TRADE_TIMESTAMP_MISMATCH = "trade_timestamp_mismatch"
    DUPLICATE_TRADE = "duplicate_trade"
    OUT_OF_ORDER_TRADE = "out_of_order_trade"
    OUT_OF_ORDER_BOOK_UPDATE = "out_of_order_book_update"
    INVALID_PRICE = "invalid_price"
    INVALID_QUANTITY = "invalid_quantity"
    ABNORMAL_PRICE_MOVE = "abnormal_price_move"
    INSUFFICIENT_BOOK_DEPTH = "insufficient_book_depth"
    EXCESSIVE_SPREAD = "excessive_spread"
    CROSSED_BOOK = "crossed_book"
    BOOK_LEVEL_ORDERING = "book_level_ordering"
    DUPLICATE_BOOK_LEVEL = "duplicate_book_level"
    EXCESSIVE_PROCESSING_LATENCY = "excessive_processing_latency"
    FEED_HEARTBEAT_TIMEOUT = "feed_heartbeat_timeout"
    BOOK_CHECKSUM_MISMATCH = "book_checksum_mismatch"
    SEQUENCE_GAP = "sequence_gap"
    INVALID_BOOK = "invalid_book"
    FEED_DISCONNECTED = "feed_disconnected"
    STRATEGY_BLOCKED = "strategy_blocked"
    # P0 production checks
    DELTA_WITHOUT_SNAPSHOT = "delta_without_snapshot"
    SNAPSHOT_DELTA_SYNC = "snapshot_delta_sync"
    RECONSTRUCTED_BOOK_INVALID = "reconstructed_book_invalid"
    CROSS_VENUE_PRICE_DIVERGENCE = "cross_venue_price_divergence"
    POSITION_RECONCILIATION_MISMATCH = "position_reconciliation_mismatch"


class DataQualityAnomaly:
    def __init__(
        self,
        *,
        code: AnomalyCode,
        severity: AnomalySeverity,
        message: str,
        timestamp_ns: int,
        symbol: str,
        event_type: str,
        blocking: bool,
        details: dict[str, str] | None = None,
        sequence: int = 0,
    ) -> None:
        self.code = code
        self.severity = severity
        self.message = message
        self.timestamp_ns = timestamp_ns
        self.symbol = symbol
        self.event_type = event_type
        self.blocking = blocking
        self.details = details or {}
        self.sequence = sequence

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "severity": self.severity.value,
            "message": self.message,
            "timestamp_ns": self.timestamp_ns,
            "symbol": self.symbol,
            "event_type": self.event_type,
            "blocking": self.blocking,
            "details": self.details,
            "sequence": self.sequence,
        }


class DataQualityState:
    def __init__(
        self,
        *,
        last_exchange_timestamp: dict[tuple[str, EventType], int],
        last_trade_id: dict[str, int],
        last_trade_price: dict[str, Decimal],
        last_mid_price: dict[str, Decimal],
        book_checksums: OrderedDict[tuple[str, int], str],
        last_heartbeat_ns: dict[str, int] | None = None,
        snapshot_seq: dict[str, int] | None = None,
        delta_synced: dict[str, bool] | None = None,
        last_delta_final: dict[str, int] | None = None,
        venue_mid_price: dict[tuple[str, str], Decimal] | None = None,
    ) -> None:
        self.last_exchange_timestamp = last_exchange_timestamp
        self.last_trade_id = last_trade_id
        self.last_trade_price = last_trade_price
        self.last_mid_price = last_mid_price
        self.book_checksums = book_checksums
        self.last_heartbeat_ns = last_heartbeat_ns or {}
        self.snapshot_seq = snapshot_seq or {}
        self.delta_synced = delta_synced or {}
        self.last_delta_final = last_delta_final or {}
        self.venue_mid_price = venue_mid_price or {}


class DataQualityValidator:
    """Stateful, deterministic market-data validation and anomaly tracking."""

    def __init__(
        self,
        config: DataQualityConfig,
    ) -> None:
        self.config = config
        self.anomalies: list[DataQualityAnomaly] = []
        self.alerts: Queue[DataQualityAnomaly] = Queue(
            maxsize=config.max_retained_anomalies
        )
        self._last_exchange_timestamp: dict[
            tuple[str, EventType], int
        ] = {}
        self._last_trade_id: dict[str, int] = {}
        self._last_trade_price: dict[str, Decimal] = {}
        self._last_mid_price: dict[str, Decimal] = {}
        self._book_checksums: OrderedDict[
            tuple[str, int], str
        ] = OrderedDict()
        self._last_heartbeat_ns: dict[str, int] = {}
        # P0.1 / P0.2 sequence tracking
        self._snapshot_seq: dict[str, int] = {}
        self._delta_synced: dict[str, bool] = {}
        self._last_delta_final: dict[str, int] = {}
        # P0.4 cross-venue mid prices
        self._venue_mid_price: dict[tuple[str, str], Decimal] = {}
        self._total_count = 0
        self._blocking_count = 0
        self._by_code: Counter[str] = Counter()
        self._by_severity: Counter[str] = Counter()

    def marker(self) -> int:
        return self._total_count

    def since(self, marker: int) -> list[DataQualityAnomaly]:
        return [
            anomaly
            for anomaly in self.anomalies
            if anomaly.sequence > marker
        ]

    def capture_state(self) -> DataQualityState:
        return DataQualityState(
            last_exchange_timestamp=dict(
                self._last_exchange_timestamp
            ),
            last_trade_id=dict(self._last_trade_id),
            last_trade_price=dict(self._last_trade_price),
            last_mid_price=dict(self._last_mid_price),
            book_checksums=OrderedDict(self._book_checksums),
            last_heartbeat_ns=dict(self._last_heartbeat_ns),
            snapshot_seq=dict(self._snapshot_seq),
            delta_synced=dict(self._delta_synced),
            last_delta_final=dict(self._last_delta_final),
            venue_mid_price=dict(self._venue_mid_price),
        )

    def restore_state(self, state: DataQualityState) -> None:
        self._last_exchange_timestamp = state.last_exchange_timestamp
        self._last_trade_id = state.last_trade_id
        self._last_trade_price = state.last_trade_price
        self._last_mid_price = state.last_mid_price
        self._book_checksums = state.book_checksums
        self._last_heartbeat_ns = state.last_heartbeat_ns
        self._snapshot_seq = state.snapshot_seq
        self._delta_synced = state.delta_synced
        self._last_delta_final = state.last_delta_final
        self._venue_mid_price = state.venue_mid_price

    def reset_book_state(self, symbol: str) -> None:
        normalized_symbol = symbol.upper()
        self._last_mid_price.pop(normalized_symbol, None)
        self._book_checksums = OrderedDict(
            (key, checksum)
            for key, checksum in self._book_checksums.items()
            if key[0].upper() != normalized_symbol
        )
        self._snapshot_seq.pop(normalized_symbol, None)
        self._delta_synced.pop(normalized_symbol, None)
        self._last_delta_final.pop(normalized_symbol, None)

    def validate_event(self, event: MarketEvent) -> bool:
        blocking = False
        self.record_heartbeat(event.symbol, event.receive_timestamp_ns)
        if (
            event.exchange_timestamp_ns <= 0
            or event.receive_timestamp_ns <= 0
            or event.process_timestamp_ns <= 0
        ):
            blocking |= self.report(
                AnomalyCode.INVALID_TIMESTAMP,
                AnomalySeverity.ERROR,
                "market event contains a non-positive timestamp",
                event=event,
                blocking=True,
            )
        if event.process_timestamp_ns < event.receive_timestamp_ns:
            blocking |= self.report(
                AnomalyCode.TIMESTAMP_ORDERING,
                AnomalySeverity.ERROR,
                "processing timestamp precedes receive timestamp",
                event=event,
                blocking=True,
            )
        if (
            event.exchange_sequence is None
            or event.exchange_sequence < 0
        ):
            blocking |= self.report(
                AnomalyCode.INVALID_SEQUENCE,
                AnomalySeverity.ERROR,
                "market event sequence must be non-negative",
                event=event,
                blocking=True,
            )

        future_skew_ns = (
            event.exchange_timestamp_ns - event.receive_timestamp_ns
        )
        if future_skew_ns > self.config.max_future_skew_ms * 1_000_000:
            blocking |= self.report(
                AnomalyCode.FUTURE_TIMESTAMP,
                AnomalySeverity.ERROR,
                "exchange timestamp is too far ahead of local receive time",
                event=event,
                blocking=True,
                details={"future_skew_ns": str(future_skew_ns)},
            )

        delay_ns = event.receive_timestamp_ns - event.exchange_timestamp_ns
        if delay_ns > self.config.max_market_data_delay_ms * 1_000_000:
            blocking |= self.report(
                AnomalyCode.STALE_MARKET_DATA,
                AnomalySeverity.ERROR,
                "market event exceeded the configured receive delay",
                event=event,
                blocking=self.config.reject_stale_market_data,
                details={"delay_ns": str(delay_ns)},
            )

        processing_ns = (
            event.process_timestamp_ns - event.receive_timestamp_ns
        )
        if processing_ns > self.config.max_processing_latency_ms * 1_000_000:
            blocking |= self.report(
                AnomalyCode.EXCESSIVE_PROCESSING_LATENCY,
                AnomalySeverity.WARNING,
                "internal processing latency exceeded the threshold",
                event=event,
                blocking=self.config.reject_processing_latency,
                details={
                    "processing_latency_ns": str(processing_ns),
                    "maximum_processing_latency_ms": str(
                        self.config.max_processing_latency_ms
                    ),
                },
            )

        timestamp_key = (event.symbol, event.event_type)
        previous_timestamp = self._last_exchange_timestamp.get(timestamp_key)
        allowed_regression_ns = (
            self.config.max_timestamp_regression_ms * 1_000_000
        )
        if (
            previous_timestamp is not None
            and previous_timestamp - event.exchange_timestamp_ns
            > allowed_regression_ns
        ):
            blocking |= self.report(
                AnomalyCode.TIMESTAMP_REGRESSION,
                AnomalySeverity.ERROR,
                "exchange timestamp moved backwards",
                event=event,
                blocking=True,
                details={
                    "previous_timestamp_ns": str(previous_timestamp),
                    "current_timestamp_ns": str(
                        event.exchange_timestamp_ns
                    ),
                },
            )

        blocking |= self._validate_payload(event)
        if blocking:
            return False

        self._last_exchange_timestamp[timestamp_key] = max(
            event.exchange_timestamp_ns,
            previous_timestamp or event.exchange_timestamp_ns,
        )
        if (
            event.event_type is EventType.TRADE
            and isinstance(event.payload, TradeTick)
        ):
            self._last_trade_id[event.payload.symbol] = (
                event.payload.trade_id
            )
            self._last_trade_price[event.payload.symbol] = (
                event.payload.price
            )
        return True

    def validate_book(
        self,
        book: BinanceOrderBook,
        event: MarketEvent,
    ) -> bool:
        blocking = False
        if (
            book.bid_depth < self.config.min_book_depth
            or book.ask_depth < self.config.min_book_depth
        ):
            blocking |= self.report(
                AnomalyCode.INSUFFICIENT_BOOK_DEPTH,
                AnomalySeverity.ERROR,
                "order book depth is below the configured minimum",
                event=event,
                blocking=True,
                details={
                    "bid_depth": str(book.bid_depth),
                    "ask_depth": str(book.ask_depth),
                    "minimum_depth": str(self.config.min_book_depth),
                },
            )

        spread_bps = book.spread_bps()
        if (
            spread_bps is not None
            and spread_bps > self.config.max_spread_bps
        ):
            blocking |= self.report(
                AnomalyCode.EXCESSIVE_SPREAD,
                AnomalySeverity.ERROR,
                "order book spread exceeds the configured threshold",
                event=event,
                blocking=self.config.reject_wide_spreads,
                details={
                    "spread_bps": str(spread_bps),
                    "maximum_spread_bps": str(
                        self.config.max_spread_bps
                    ),
                },
            )

        mid_price = book.mid_price
        previous_mid = self._last_mid_price.get(book.symbol)
        if mid_price is not None and previous_mid is not None:
            movement_bps = (
                abs(mid_price - previous_mid)
                / previous_mid
                * Decimal(10_000)
            )
            if movement_bps > self.config.max_price_move_bps:
                blocking |= self.report(
                    AnomalyCode.ABNORMAL_PRICE_MOVE,
                    AnomalySeverity.WARNING,
                    "order book mid-price moved beyond the threshold",
                    event=event,
                    blocking=self.config.reject_abnormal_price_moves,
                    details={
                        "previous_mid_price": str(previous_mid),
                        "mid_price": str(mid_price),
                        "movement_bps": str(movement_bps),
                    },
                )
        checksum_key: tuple[str, int] | None = None
        checksum: str | None = None
        if book.last_update_id is not None:
            checksum_key = (book.symbol, book.last_update_id)
            checksum = book.checksum()
            previous_checksum = self._book_checksums.get(checksum_key)
            if previous_checksum is not None and previous_checksum != checksum:
                blocking |= self.report(
                    AnomalyCode.BOOK_CHECKSUM_MISMATCH,
                    AnomalySeverity.CRITICAL,
                    "the same book sequence produced a different checksum",
                    event=event,
                    blocking=True,
                    details={
                        "previous_checksum": previous_checksum,
                        "current_checksum": checksum,
                    },
                )
        if not blocking:
            if mid_price is not None:
                self._last_mid_price[book.symbol] = mid_price
            if checksum_key is not None and checksum is not None:
                self._book_checksums[checksum_key] = checksum
                self._book_checksums.move_to_end(checksum_key)
                while (
                    len(self._book_checksums)
                    > self.config.max_retained_book_checksums
                ):
                    self._book_checksums.popitem(last=False)
        return not blocking

    def report(
        self,
        code: AnomalyCode,
        severity: AnomalySeverity,
        message: str,
        *,
        event: MarketEvent | None = None,
        symbol: str = "",
        event_type: str = "",
        timestamp_ns: int = 0,
        blocking: bool,
        details: dict[str, str] | None = None,
    ) -> bool:
        self._total_count += 1
        anomaly = DataQualityAnomaly(
            code=code,
            severity=severity,
            message=message,
            timestamp_ns=(
                event.receive_timestamp_ns if event else timestamp_ns
            ),
            symbol=event.symbol if event else symbol,
            event_type=(
                event.event_type.value if event else event_type
            ),
            blocking=blocking,
            details=details,
            sequence=self._total_count,
        )
        self.anomalies.append(anomaly)
        overflow = (
            len(self.anomalies) - self.config.max_retained_anomalies
        )
        if overflow > 0:
            del self.anomalies[:overflow]
        self._blocking_count += int(blocking)
        self._by_code[code.value] += 1
        self._by_severity[severity.value] += 1
        try:
            self.alerts.put_nowait(anomaly)
        except Full:
            try:
                self.alerts.get_nowait()
            except Empty:
                pass
            self.alerts.put_nowait(anomaly)
        log = (
            logger.error
            if severity in {AnomalySeverity.ERROR, AnomalySeverity.CRITICAL}
            else logger.warning
        )
        log(
            "market_data_anomaly",
            **anomaly.to_dict(),
        )
        return blocking

    def summary(self) -> dict[str, Any]:
        return {
            "count": self._total_count,
            "blocking_count": self._blocking_count,
            "retained_count": len(self.anomalies),
            "by_code": dict(sorted(self._by_code.items())),
            "by_severity": dict(sorted(self._by_severity.items())),
        }

    def _validate_payload(self, event: MarketEvent) -> bool:
        if event.event_type is EventType.TRADE:
            if not isinstance(event.payload, TradeTick):
                return self.report(
                    AnomalyCode.INVALID_QUANTITY,
                    AnomalySeverity.ERROR,
                    "trade event payload has an invalid type",
                    event=event,
                    blocking=True,
                )
            if event.payload.symbol != event.symbol:
                return self.report(
                    AnomalyCode.SYMBOL_MISMATCH,
                    AnomalySeverity.ERROR,
                    "trade payload symbol does not match event symbol",
                    event=event,
                    blocking=True,
                    details={"payload_symbol": event.payload.symbol},
                )
            return self._validate_trade(event, event.payload)
        if event.event_type is EventType.BOOK_SNAPSHOT:
            if not isinstance(event.payload, BookSnapshot):
                return self.report(
                    AnomalyCode.INVALID_BOOK,
                    AnomalySeverity.ERROR,
                    "book snapshot payload has an invalid type",
                    event=event,
                    blocking=True,
                )
            if event.payload.symbol != event.symbol:
                return self.report(
                    AnomalyCode.SYMBOL_MISMATCH,
                    AnomalySeverity.ERROR,
                    "snapshot symbol does not match event symbol",
                    event=event,
                    blocking=True,
                    details={"payload_symbol": event.payload.symbol},
                )
            if (
                event.payload.last_update_id < 0
            ):
                return self.report(
                    AnomalyCode.INVALID_SEQUENCE,
                    AnomalySeverity.ERROR,
                    "snapshot sequence is invalid",
                    event=event,
                    blocking=True,
                )
            blocking = self._validate_levels(
                event,
                event.payload.bids + event.payload.asks,
                allow_zero_quantity=False,
            ) | self._validate_book_sides(
                event,
                event.payload.bids,
                event.payload.asks,
                require_monotonic=True,
            )
            if not blocking:
                # P0.2: record snapshot baseline, reset delta sync state
                sym = event.payload.symbol.upper()
                self._snapshot_seq[sym] = event.payload.last_update_id
                self._delta_synced[sym] = False
                self._last_delta_final.pop(sym, None)
            return blocking
        if event.event_type is EventType.BOOK_DELTA:
            if not isinstance(event.payload, BookDelta):
                return self.report(
                    AnomalyCode.INVALID_BOOK,
                    AnomalySeverity.ERROR,
                    "book delta payload has an invalid type",
                    event=event,
                    blocking=True,
                )
            if event.payload.symbol != event.symbol:
                return self.report(
                    AnomalyCode.SYMBOL_MISMATCH,
                    AnomalySeverity.ERROR,
                    "book delta symbol does not match event symbol",
                    event=event,
                    blocking=True,
                    details={"payload_symbol": event.payload.symbol},
                )
            if (
                event.payload.first_update_id < 0
                or event.payload.final_update_id
                < event.payload.first_update_id
            ):
                return self.report(
                    AnomalyCode.INVALID_SEQUENCE,
                    AnomalySeverity.ERROR,
                    "book delta sequence range is invalid",
                    event=event,
                    blocking=True,
                )
            blocking = self._validate_levels(
                event,
                event.payload.bids + event.payload.asks,
                allow_zero_quantity=True,
            ) | self._validate_book_sides(
                event,
                event.payload.bids,
                event.payload.asks,
                require_monotonic=False,
            )
            # P0.1 + P0.2: sequence gap and snapshot-delta sync check
            blocking |= self._validate_delta_sequence(event, event.payload)
            return blocking
        return False

    def _validate_trade(
        self,
        event: MarketEvent,
        trade: TradeTick,
    ) -> bool:
        blocking = False
        blocking |= self._validate_price(event, trade.price)
        if trade.trade_timestamp_ns <= 0:
            blocking |= self.report(
                AnomalyCode.INVALID_TIMESTAMP,
                AnomalySeverity.ERROR,
                "trade timestamp must be positive",
                event=event,
                blocking=True,
            )
        trade_timestamp_difference = abs(
            trade.trade_timestamp_ns - event.exchange_timestamp_ns
        )
        if (
            trade_timestamp_difference
            > self.config.max_trade_timestamp_difference_ms * 1_000_000
        ):
            blocking |= self.report(
                AnomalyCode.TRADE_TIMESTAMP_MISMATCH,
                AnomalySeverity.ERROR,
                "trade and event timestamps differ beyond the threshold",
                event=event,
                blocking=True,
                details={
                    "trade_timestamp_ns": str(
                        trade.trade_timestamp_ns
                    ),
                    "event_timestamp_ns": str(
                        event.exchange_timestamp_ns
                    ),
                    "difference_ns": str(trade_timestamp_difference),
                },
            )
        if not trade.quantity.is_finite():
            blocking |= self.report(
                AnomalyCode.INVALID_QUANTITY,
                AnomalySeverity.ERROR,
                "trade quantity must be finite",
                event=event,
                blocking=True,
                details={"quantity": str(trade.quantity)},
            )
        elif not (
            self.config.min_trade_quantity
            <= trade.quantity
            <= self.config.max_trade_quantity
        ):
            blocking |= self.report(
                AnomalyCode.INVALID_QUANTITY,
                AnomalySeverity.ERROR,
                "trade quantity is outside configured bounds",
                event=event,
                blocking=True,
                details={
                    "quantity": str(trade.quantity),
                    "minimum_quantity": str(
                        self.config.min_trade_quantity
                    ),
                    "maximum_quantity": str(
                        self.config.max_trade_quantity
                    ),
                },
            )

        previous_trade_id = self._last_trade_id.get(trade.symbol)
        if previous_trade_id is not None and trade.trade_id <= previous_trade_id:
            code = (
                AnomalyCode.DUPLICATE_TRADE
                if trade.trade_id == previous_trade_id
                else AnomalyCode.OUT_OF_ORDER_TRADE
            )
            blocking |= self.report(
                code,
                AnomalySeverity.ERROR,
                "trade identifier is duplicate or out of order",
                event=event,
                blocking=True,
                details={
                    "previous_trade_id": str(previous_trade_id),
                    "trade_id": str(trade.trade_id),
                },
            )

        previous_price = self._last_trade_price.get(trade.symbol)
        if previous_price is not None and trade.price.is_finite():
            movement_bps = (
                abs(trade.price - previous_price)
                / previous_price
                * Decimal(10_000)
            )
            if movement_bps > self.config.max_price_move_bps:
                blocking |= self.report(
                    AnomalyCode.ABNORMAL_PRICE_MOVE,
                    AnomalySeverity.WARNING,
                    "trade price moved beyond the configured threshold",
                    event=event,
                    blocking=self.config.reject_abnormal_price_moves,
                    details={
                        "previous_price": str(previous_price),
                        "price": str(trade.price),
                        "movement_bps": str(movement_bps),
                    },
                )
        return blocking

    def _validate_book_sides(
        self,
        event: MarketEvent,
        bids: tuple[PriceLevel, ...],
        asks: tuple[PriceLevel, ...],
        *,
        require_monotonic: bool,
    ) -> bool:
        """Check intra-message book consistency.

        Duplicate prices are always contradictory. Monotonic ordering and
        the crossed-market test only apply to snapshots, because a delta
        legitimately carries removals and partial updates.
        """

        blocking = self._validate_side_ordering(
            event,
            bids,
            side="bid",
            descending=True,
            require_monotonic=require_monotonic,
        )
        blocking |= self._validate_side_ordering(
            event,
            asks,
            side="ask",
            descending=False,
            require_monotonic=require_monotonic,
        )
        if not require_monotonic or not bids or not asks:
            return blocking
        best_bid = max(level.price for level in bids)
        best_ask = min(level.price for level in asks)
        if best_bid >= best_ask:
            blocking |= self.report(
                AnomalyCode.CROSSED_BOOK,
                AnomalySeverity.CRITICAL,
                "book snapshot is crossed or locked",
                event=event,
                blocking=self.config.reject_crossed_books,
                details={
                    "best_bid": str(best_bid),
                    "best_ask": str(best_ask),
                },
            )
        return blocking

    def _validate_side_ordering(
        self,
        event: MarketEvent,
        levels: tuple[PriceLevel, ...],
        *,
        side: str,
        descending: bool,
        require_monotonic: bool,
    ) -> bool:
        blocking = False
        seen: set[Decimal] = set()
        previous: Decimal | None = None
        for position, level in enumerate(levels):
            if level.price in seen:
                blocking |= self.report(
                    AnomalyCode.DUPLICATE_BOOK_LEVEL,
                    AnomalySeverity.ERROR,
                    "book side repeats a price level",
                    event=event,
                    blocking=True,
                    details={"side": side, "price": str(level.price)},
                )
            seen.add(level.price)
            if require_monotonic and previous is not None:
                ordered = (
                    previous > level.price
                    if descending
                    else previous < level.price
                )
                if not ordered:
                    blocking |= self.report(
                        AnomalyCode.BOOK_LEVEL_ORDERING,
                        AnomalySeverity.ERROR,
                        "book side is not monotonically ordered",
                        event=event,
                        blocking=True,
                        details={
                            "side": side,
                            "position": str(position),
                            "previous_price": str(previous),
                            "price": str(level.price),
                        },
                    )
            previous = level.price
        return blocking

    def record_heartbeat(self, symbol: str, timestamp_ns: int) -> None:
        """Record liveness for a feed so gaps can be detected."""

        self._last_heartbeat_ns[symbol.upper()] = timestamp_ns

    def check_heartbeats(
        self,
        now_ns: int,
        *,
        symbols: tuple[str, ...] | None = None,
    ) -> tuple[DataQualityAnomaly, ...]:
        """Report feeds that have gone quiet past the configured interval.

        Heartbeats are wall-clock rather than event driven, so silence is
        detectable even when no message arrives to validate.
        """

        threshold_ns = self.config.max_heartbeat_interval_ms * 1_000_000
        candidates = (
            tuple(symbol.upper() for symbol in symbols)
            if symbols is not None
            else tuple(sorted(self._last_heartbeat_ns))
        )
        reported: list[DataQualityAnomaly] = []
        for symbol in candidates:
            last_seen = self._last_heartbeat_ns.get(symbol)
            if last_seen is None:
                continue
            silence_ns = now_ns - last_seen
            if silence_ns <= threshold_ns:
                continue
            self.report(
                AnomalyCode.FEED_HEARTBEAT_TIMEOUT,
                AnomalySeverity.CRITICAL,
                "feed produced no messages within the heartbeat interval",
                symbol=symbol,
                event_type=EventType.FEED_STATUS.value,
                timestamp_ns=now_ns,
                blocking=True,
                details={
                    "silence_ns": str(silence_ns),
                    "maximum_interval_ms": str(
                        self.config.max_heartbeat_interval_ms
                    ),
                    "last_seen_ns": str(last_seen),
                },
            )
            reported.append(self.anomalies[-1])
        return tuple(reported)

    def _validate_levels(
        self,
        event: MarketEvent,
        levels: tuple[PriceLevel, ...],
        *,
        allow_zero_quantity: bool,
    ) -> bool:
        blocking = False
        for level in levels:
            blocking |= self._validate_price(event, level.price)
            if not level.quantity.is_finite():
                blocking |= self.report(
                    AnomalyCode.INVALID_QUANTITY,
                    AnomalySeverity.ERROR,
                    "book level quantity must be finite",
                    event=event,
                    blocking=True,
                    details={"quantity": str(level.quantity)},
                )
                continue
            quantity_valid = (
                level.quantity >= 0
                if allow_zero_quantity
                else level.quantity > 0
            )
            if not quantity_valid:
                blocking |= self.report(
                    AnomalyCode.INVALID_QUANTITY,
                    AnomalySeverity.ERROR,
                    "book level quantity is invalid",
                    event=event,
                    blocking=True,
                    details={"quantity": str(level.quantity)},
                )
        return blocking

    def _validate_price(
        self,
        event: MarketEvent,
        price: Decimal,
    ) -> bool:
        if not price.is_finite():
            return self.report(
                AnomalyCode.INVALID_PRICE,
                AnomalySeverity.ERROR,
                "price must be finite",
                event=event,
                blocking=True,
                details={"price": str(price)},
            )
        if self.config.min_price <= price <= self.config.max_price:
            return False
        return self.report(
            AnomalyCode.INVALID_PRICE,
            AnomalySeverity.ERROR,
            "price is outside configured bounds",
            event=event,
            blocking=True,
            details={
                "price": str(price),
                "minimum_price": str(self.config.min_price),
                "maximum_price": str(self.config.max_price),
            },
        )

    # ------------------------------------------------------------------ #
    # P0.1 + P0.2 — sequence-gap detection and snapshot↔delta sync        #
    # ------------------------------------------------------------------ #

    def _validate_delta_sequence(
        self,
        event: MarketEvent,
        delta: BookDelta,
    ) -> bool:
        """P0.2: snapshot↔websocket delta synchronisation checks.

        P0.1 (sequence-gap detection) is handled by the engine via
        BinanceOrderBook.apply_delta() raising SequenceGapError and reporting
        AnomalyCode.SEQUENCE_GAP through this validator's report() method.
        That path also drives _minimum_recovery_snapshot_sequence, so the
        engine's stale-snapshot gate remains intact.  Intercepting gaps here
        would short-circuit that gate and allow stale snapshots to be accepted.
        """
        sym = delta.symbol.upper()
        snapshot_seq = self._snapshot_seq.get(sym)

        if snapshot_seq is None:
            # Delta arrived with no snapshot baseline — cannot safely apply it.
            return self.report(
                AnomalyCode.DELTA_WITHOUT_SNAPSHOT,
                AnomalySeverity.ERROR,
                "book delta received before any snapshot was established",
                event=event,
                blocking=True,
                details={
                    "delta_first_update_id": str(delta.first_update_id),
                    "delta_final_update_id": str(delta.final_update_id),
                },
            )

        if not self._delta_synced.get(sym, False):
            # P0.2: First delta after snapshot must satisfy:
            # first_update_id <= snapshot.last_update_id + 1 <= final_update_id
            sync_target = snapshot_seq + 1
            if delta.first_update_id > sync_target:
                return self.report(
                    AnomalyCode.SNAPSHOT_DELTA_SYNC,
                    AnomalySeverity.ERROR,
                    "first delta after snapshot does not overlap snapshot sequence",
                    event=event,
                    blocking=True,
                    details={
                        "snapshot_last_update_id": str(snapshot_seq),
                        "delta_first_update_id": str(delta.first_update_id),
                        "delta_final_update_id": str(delta.final_update_id),
                        "required_overlap_id": str(sync_target),
                    },
                )
            # Sync established.
            self._delta_synced[sym] = True

        # Track last-seen final_update_id for use by validate_reconstructed_book
        # and external callers.  The engine's restore_state() will roll this
        # back if apply_delta() raises SequenceGapError, keeping the tracker
        # consistent with the book's actual state.
        self._last_delta_final[sym] = delta.final_update_id
        return False

    # ------------------------------------------------------------------ #
    # P0.3 — post-delta reconstructed-book validation                      #
    # ------------------------------------------------------------------ #

    def validate_reconstructed_book(
        self,
        book: BinanceOrderBook,
        event: MarketEvent,
    ) -> bool:
        """Validate that the in-memory book is internally consistent after a delta.

        Call this immediately after BinanceOrderBook.apply_delta() succeeds.
        Returns True if the book passed all checks (i.e. no blocking anomaly).
        """
        blocking = False

        # Depth must still meet the minimum after level removals.
        if (
            book.bid_depth < self.config.min_book_depth
            or book.ask_depth < self.config.min_book_depth
        ):
            blocking |= self.report(
                AnomalyCode.INSUFFICIENT_BOOK_DEPTH,
                AnomalySeverity.ERROR,
                "reconstructed book depth is below the configured minimum after delta",
                event=event,
                blocking=True,
                details={
                    "bid_depth": str(book.bid_depth),
                    "ask_depth": str(book.ask_depth),
                    "minimum_depth": str(self.config.min_book_depth),
                },
            )

        # Crossed book after delta (should be caught by BinanceOrderBook, but
        # guard here too in case the book implementation changes).
        best_bid = book.best_bid
        best_ask = book.best_ask
        if best_bid is not None and best_ask is not None and best_bid >= best_ask:
            blocking |= self.report(
                AnomalyCode.RECONSTRUCTED_BOOK_INVALID,
                AnomalySeverity.CRITICAL,
                "reconstructed book is crossed or locked after delta applied",
                event=event,
                blocking=True,
                details={
                    "best_bid": str(best_bid),
                    "best_ask": str(best_ask),
                    "last_update_id": str(book.last_update_id),
                },
            )

        if not blocking:
            mid = book.mid_price
            if mid is not None:
                self._last_mid_price[book.symbol] = mid
            if book.last_update_id is not None:
                checksum_key = (book.symbol, book.last_update_id)
                checksum = book.checksum()
                self._book_checksums[checksum_key] = checksum
                self._book_checksums.move_to_end(checksum_key)
                while (
                    len(self._book_checksums)
                    > self.config.max_retained_book_checksums
                ):
                    self._book_checksums.popitem(last=False)

        return not blocking

    # ------------------------------------------------------------------ #
    # P0.4 — cross-exchange / reference-price sanity                       #
    # ------------------------------------------------------------------ #

    def record_venue_mid(
        self,
        venue: str,
        symbol: str,
        mid_price: Decimal,
        timestamp_ns: int,
    ) -> None:
        """Register the current mid price for a (venue, symbol) pair.

        Call this whenever a book update produces a new mid price.
        """
        self._venue_mid_price[(venue.lower(), symbol.upper())] = mid_price
        self.record_heartbeat(symbol, timestamp_ns)

    def check_cross_venue_divergence(
        self,
        symbol: str,
        now_ns: int,
    ) -> tuple[DataQualityAnomaly, ...]:
        """Report (venue, symbol) pairs whose mid diverges beyond the configured threshold.

        Returns the anomalies emitted (empty when all venues are within band).
        Requires at least config.min_venues_for_cross_venue_check venues to have
        reported a mid price before any check is performed.
        """
        sym = symbol.upper()
        venue_mids = {
            venue: mid
            for (venue, s), mid in self._venue_mid_price.items()
            if s == sym
        }
        if len(venue_mids) < self.config.min_venues_for_cross_venue_check:
            return ()

        prices = list(venue_mids.values())
        mean_price = sum(prices) / len(prices)
        if mean_price == 0:
            return ()

        threshold = self.config.max_cross_venue_divergence_bps
        reported: list[DataQualityAnomaly] = []
        for venue, mid in venue_mids.items():
            divergence_bps = abs(mid - mean_price) / mean_price * Decimal(10_000)
            if divergence_bps > threshold:
                self.report(
                    AnomalyCode.CROSS_VENUE_PRICE_DIVERGENCE,
                    AnomalySeverity.ERROR,
                    "venue mid price diverges from cross-venue mean beyond threshold",
                    symbol=sym,
                    event_type=EventType.BOOK_SNAPSHOT.value,
                    timestamp_ns=now_ns,
                    blocking=self.config.reject_cross_venue_divergence,
                    details={
                        "venue": venue,
                        "venue_mid": str(mid),
                        "cross_venue_mean": str(mean_price),
                        "divergence_bps": str(divergence_bps),
                        "threshold_bps": str(threshold),
                        "venue_count": str(len(venue_mids)),
                    },
                )
                reported.append(self.anomalies[-1])
        return tuple(reported)

    # ------------------------------------------------------------------ #
    # P0.5 — OMS ↔ exchange position/balance reconciliation                #
    # ------------------------------------------------------------------ #

    def validate_oms_reconciliation(
        self,
        oms: "OrderManagementSystem",
        symbol: str,
        exchange_position: Decimal,
        timestamp_ns: int,
    ) -> bool:
        """Compare OMS-computed net position against exchange-reported position.

        Returns True if a blocking anomaly was emitted.
        Tolerance is relative: abs(oms - exchange) / max(|oms|, |exchange|, 1e-12).
        """
        oms_position = self._compute_oms_position(oms, symbol)
        diff = abs(oms_position - exchange_position)
        reference = max(abs(oms_position), abs(exchange_position), Decimal("1e-12"))
        rel_diff = diff / reference

        tolerance = self.config.position_reconciliation_tolerance
        if rel_diff <= tolerance:
            return False

        return self.report(
            AnomalyCode.POSITION_RECONCILIATION_MISMATCH,
            AnomalySeverity.CRITICAL,
            "OMS net position does not match exchange-reported position",
            symbol=symbol,
            event_type=EventType.ORDER_UPDATE.value,
            timestamp_ns=timestamp_ns,
            blocking=self.config.reject_position_mismatch,
            details={
                "oms_position": str(oms_position),
                "exchange_position": str(exchange_position),
                "absolute_difference": str(diff),
                "relative_difference_pct": str((rel_diff * 100).quantize(Decimal("0.0001"))),
                "tolerance_pct": str((tolerance * 100).quantize(Decimal("0.0001"))),
            },
        )

    @staticmethod
    def _compute_oms_position(
        oms: "OrderManagementSystem",
        symbol: str,
    ) -> Decimal:
        """Derive net position from order updates stored in the OMS."""
        sym = symbol.upper()
        # Last update per order wins (list is append-only, so later index = latest).
        from trading_platform_poc.models import OrderUpdate  # local import avoids top-level cycle risk

        latest: dict[UUID, "OrderUpdate"] = {}
        for update in oms.updates:
            latest[update.order_id] = update

        net = Decimal(0)
        for order in oms.orders:
            if order.symbol.upper() != sym:
                continue
            update = latest.get(order.order_id)
            if update is None:
                continue
            if update.state not in {OrderState.FILLED, OrderState.PARTIALLY_FILLED}:
                continue
            sign = Decimal(1) if order.side == Side.BUY else Decimal(-1)
            net += sign * update.cumulative_filled_quantity
        return net
