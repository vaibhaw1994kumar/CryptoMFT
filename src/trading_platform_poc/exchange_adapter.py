from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from trading_platform_poc.models import Order, OrderState, Side
from trading_platform_poc.oms import OrderManagementSystem


class ExchangeAdapterError(RuntimeError):
    """Base class for guarded exchange adapter failures."""


class ExchangeCredentialsError(ExchangeAdapterError):
    pass


class ExchangeEndpointError(ExchangeAdapterError):
    pass


class ExchangeInvalidResponseError(ExchangeAdapterError):
    def __init__(self, venue: str, status: int, body: str) -> None:
        super().__init__(
            f"{venue} returned an invalid response body with status {status}"
        )
        self.venue = venue
        self.status = status
        self.body = body


class ExchangeAPIError(ExchangeAdapterError):
    def __init__(
        self,
        *,
        venue: str,
        status: int,
        code: str | int | None,
        message: str,
        payload: Any,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(f"{venue} API error {code}: {message}")
        self.venue = venue
        self.status = status
        self.code = code
        self.message = message
        self.payload = payload
        self.retry_after = retry_after


class AmbiguousOrderError(ExchangeAdapterError):
    def __init__(self, venue: str, client_order_id: str) -> None:
        super().__init__(
            f"{venue} order outcome is ambiguous; reconcile client identifier "
            f"before retrying: {client_order_id}"
        )
        self.venue = venue
        self.client_order_id = client_order_id


class QuantityUnit(str, Enum):
    BASE = "base"
    QUOTE = "quote"
    CONTRACTS = "contracts"


@dataclass(frozen=True, slots=True)
class InstrumentRules:
    venue: str
    symbol: str
    tick_size: Decimal
    quantity_step: Decimal
    minimum_quantity: Decimal
    maximum_limit_quantity: Decimal | None = None
    maximum_market_quantity: Decimal | None = None
    base_currency: str | None = None
    quote_currency: str | None = None
    settlement_currency: str | None = None
    quantity_unit: QuantityUnit = QuantityUnit.BASE
    instrument_type: str | None = None
    contract_size: Decimal | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class NormalizedOrder:
    quantity: Decimal
    price: Decimal | None
    client_order_id: str


@dataclass(frozen=True, slots=True)
class ExchangeOrder:
    venue: str
    symbol: str
    exchange_order_id: str
    client_order_id: str
    status: str
    side: str
    order_type: str
    quantity: Decimal
    filled_quantity: Decimal
    price: Decimal | None
    average_fill_price: Decimal | None
    timestamp_ms: int | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExchangeTrade:
    venue: str
    symbol: str
    trade_id: str
    exchange_order_id: str
    side: str
    price: Decimal
    quantity: Decimal
    fee: Decimal | None
    fee_currency: str | None
    timestamp_ms: int
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    venue: str
    currency: str | None
    timestamp_ms: int | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RequestTelemetry:
    venue: str
    method: str
    path: str
    status: int
    rate_limit_headers: dict[str, str]
    retry_after: str | None
    exchange_received_us: int | None = None
    exchange_sent_us: int | None = None
    exchange_elapsed_us: int | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationSnapshot:
    venue: str
    symbol: str
    captured_at_ms: int
    exchange_open_orders: tuple[ExchangeOrder, ...]
    recent_trades: tuple[ExchangeTrade, ...]
    account: AccountSnapshot
    missing_on_exchange: tuple[str, ...]
    unknown_on_exchange: tuple[str, ...]


@runtime_checkable
class ExchangeAdapter(Protocol):
    venue: str
    last_request: RequestTelemetry | None

    async def close(self) -> None: ...

    def client_order_id(self, order: Order) -> str: ...

    async def instrument_rules(self, symbol: str) -> InstrumentRules: ...

    async def normalize_order(
        self,
        order: Order,
        *,
        reference_price: Decimal | None = None,
    ) -> NormalizedOrder: ...

    async def submit_order(self, order: Order) -> ExchangeOrder: ...

    async def cancel_order(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> ExchangeOrder: ...

    async def query_order(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> ExchangeOrder: ...

    async def open_orders(
        self,
        symbol: str | None = None,
    ) -> tuple[ExchangeOrder, ...]: ...

    async def trades(
        self,
        symbol: str,
        *,
        limit: int = 100,
    ) -> tuple[ExchangeTrade, ...]: ...

    async def account(self, currency: str | None = None) -> AccountSnapshot: ...

    async def reconcile(
        self,
        oms: OrderManagementSystem,
        symbol: str,
    ) -> ReconciliationSnapshot: ...


def deterministic_client_id(
    venue: str,
    source_id: str,
    *,
    maximum_length: int,
    alphanumeric_only: bool,
) -> str:
    prefix = "".join(character for character in venue.lower() if character.isalnum())
    digest = hashlib.sha256(source_id.encode()).hexdigest()
    separator = "" if alphanumeric_only else "-"
    value = f"{prefix}{separator}{digest}"
    return value[:maximum_length]


def round_quantity(quantity: Decimal, step: Decimal) -> Decimal:
    if quantity <= 0 or step <= 0:
        raise ExchangeAdapterError("quantity and quantity step must be positive")
    return (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_price(price: Decimal, tick: Decimal, side: Side) -> Decimal:
    if price <= 0 or tick <= 0:
        raise ExchangeAdapterError("price and tick size must be positive")
    rounding = ROUND_DOWN if side is Side.BUY else ROUND_UP
    return (price / tick).to_integral_value(rounding=rounding) * tick


def decimal_string(value: Decimal | None) -> str:
    if value is None:
        raise ExchangeAdapterError("required decimal value is missing")
    return format(value, "f")


def local_open_client_ids(
    adapter: ExchangeAdapter,
    oms: OrderManagementSystem,
    symbol: str,
) -> set[str]:
    exchange_owned_states = {
        OrderState.SUBMITTED,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.RECONCILIATION_REQUIRED,
    }
    venue = adapter.venue.strip().casefold()
    return {
        adapter.client_order_id(order)
        for order in oms.open_orders()
        if (
            order.symbol.upper() == symbol.upper()
            and order.venue is not None
            and order.venue.strip().casefold() == venue
            and order.state in exchange_owned_states
        )
    }
