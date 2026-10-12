from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

import msgspec


class EventType(str, Enum):
    BOOK_SNAPSHOT = "book_snapshot"
    BOOK_DELTA = "book_delta"
    TRADE = "trade"
    FEED_STATUS = "feed_status"
    ORDER_INTENT = "order_intent"
    ORDER_UPDATE = "order_update"
    FILL = "fill"
    RISK_DECISION = "risk_decision"
    CONTROL = "control"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    POST_ONLY = "post_only"


class OrderState(str, Enum):
    CREATED = "created"
    RISK_APPROVED = "risk_approved"
    RISK_REJECTED = "risk_rejected"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class FeedState(str, Enum):
    CONNECTING = "connecting"
    SYNCHRONIZING = "synchronizing"
    HEALTHY = "healthy"
    STALE = "stale"
    DISCONNECTED = "disconnected"
    INVALID = "invalid"


class RiskDecision(msgspec.Struct, frozen=True):
    approved: bool
    reason: str
    decision_timestamp_ns: int


class PriceLevel(msgspec.Struct, frozen=True):
    price: Decimal
    quantity: Decimal


class BookSnapshot(msgspec.Struct, frozen=True):
    symbol: str
    last_update_id: int
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]


class BookDelta(msgspec.Struct, frozen=True):
    symbol: str
    first_update_id: int
    final_update_id: int
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]


class TradeTick(msgspec.Struct, frozen=True):
    symbol: str
    trade_id: int
    price: Decimal
    quantity: Decimal
    buyer_is_maker: bool
    trade_timestamp_ns: int


class FeedStatus(msgspec.Struct, frozen=True):
    state: FeedState
    reason: str
    last_sequence: int | None = None
    age_ms: int | None = None


class OrderIntent(msgspec.Struct, frozen=True):
    intent_id: UUID
    strategy_id: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal
    price: Decimal | None
    created_timestamp_ns: int


class Order(msgspec.Struct, frozen=True):
    order_id: UUID
    intent_id: UUID
    client_order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal
    price: Decimal | None
    state: OrderState
    created_timestamp_ns: int
    venue: str | None = None


class OrderUpdate(msgspec.Struct, frozen=True):
    order_id: UUID
    state: OrderState
    cumulative_filled_quantity: Decimal
    average_fill_price: Decimal | None
    reason: str | None
    update_timestamp_ns: int


class Fill(msgspec.Struct, frozen=True):
    fill_id: UUID
    order_id: UUID
    symbol: str
    side: Side
    price: Decimal
    quantity: Decimal
    fee: Decimal
    fee_asset: str
    fill_timestamp_ns: int


class MarketEvent(msgspec.Struct, frozen=True):
    event_id: UUID
    run_id: UUID
    venue: str
    symbol: str
    event_type: EventType
    exchange_sequence: int | None
    exchange_timestamp_ns: int
    receive_timestamp_ns: int
    process_timestamp_ns: int
    schema_version: int
    payload: Any
