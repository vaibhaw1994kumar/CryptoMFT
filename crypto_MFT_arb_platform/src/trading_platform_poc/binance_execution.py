from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import aiohttp

from trading_platform_poc.config import BinanceExecutionConfig
from trading_platform_poc.models import (
    Order,
    OrderState,
    OrderType,
    Side,
)
from trading_platform_poc.oms import OrderManagementSystem


class BinanceGatewayError(RuntimeError):
    pass


class BinanceCredentialsError(BinanceGatewayError):
    pass


class BinanceAPIError(BinanceGatewayError):
    def __init__(
        self,
        *,
        status: int,
        code: int | None,
        message: str,
        payload: Any,
    ) -> None:
        super().__init__(f"Binance API error {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.payload = payload


class BinanceAmbiguousOrderError(BinanceGatewayError):
    def __init__(self, client_order_id: str) -> None:
        super().__init__(
            "order operation outcome is ambiguous; query by client order ID "
            f"before retrying: {client_order_id}"
        )
        self.client_order_id = client_order_id


class BinanceInvalidResponseError(BinanceGatewayError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(
            f"Binance returned an invalid response body with status {status}"
        )
        self.status = status
        self.body = body


_UNKNOWN_EXECUTION_STATUS_CODES = frozenset({-1006, -1007})


@dataclass(frozen=True, slots=True)
class BinanceCredentials:
    api_key: str
    api_secret: str

    @classmethod
    def from_environment(
        cls,
        config: BinanceExecutionConfig,
    ) -> BinanceCredentials:
        api_key = os.environ.get(
            config.api_key_environment_variable,
            "",
        )
        api_secret = os.environ.get(
            config.api_secret_environment_variable,
            "",
        )
        if not api_key or not api_secret:
            raise BinanceCredentialsError(
                "Binance Testnet credentials are missing. Set "
                f"{config.api_key_environment_variable} and "
                f"{config.api_secret_environment_variable}."
            )
        return cls(api_key=api_key, api_secret=api_secret)


@dataclass(frozen=True, slots=True)
class BinanceSymbolRules:
    symbol: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    minimum_price: Decimal
    maximum_price: Decimal
    step_size: Decimal
    minimum_quantity: Decimal
    maximum_quantity: Decimal
    market_step_size: Decimal | None
    market_minimum_quantity: Decimal
    market_maximum_quantity: Decimal
    minimum_notional: Decimal
    maximum_notional: Decimal | None
    minimum_notional_applies_to_market: bool = True
    maximum_notional_applies_to_market: bool = True


@dataclass(frozen=True, slots=True)
class NormalizedBinanceOrder:
    quantity: Decimal
    price: Decimal | None


@dataclass(frozen=True, slots=True)
class BinanceOrderResult:
    symbol: str
    order_id: int
    client_order_id: str
    status: str
    side: str
    order_type: str
    original_quantity: Decimal
    executed_quantity: Decimal
    cumulative_quote_quantity: Decimal
    price: Decimal
    transact_time_ms: int | None
    raw: dict[str, Any]

    @property
    def average_fill_price(self) -> Decimal | None:
        if self.executed_quantity <= 0:
            return None
        return self.cumulative_quote_quantity / self.executed_quantity

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
    ) -> BinanceOrderResult:
        return cls(
            symbol=str(payload["symbol"]),
            order_id=int(payload["orderId"]),
            client_order_id=str(
                payload.get("origClientOrderId")
                or payload.get("clientOrderId")
            ),
            status=str(payload["status"]),
            side=str(payload["side"]),
            order_type=str(payload["type"]),
            original_quantity=Decimal(
                str(payload.get("origQty", "0"))
            ),
            executed_quantity=Decimal(
                str(payload.get("executedQty", "0"))
            ),
            cumulative_quote_quantity=Decimal(
                str(payload.get("cummulativeQuoteQty", "0"))
            ),
            price=Decimal(str(payload.get("price", "0"))),
            transact_time_ms=(
                int(
                    payload.get("transactTime")
                    or payload.get("updateTime")
                )
                if payload.get("transactTime")
                or payload.get("updateTime")
                else None
            ),
            raw=payload,
        )


@dataclass(frozen=True, slots=True)
class BinanceReconciliationReport:
    symbol: str
    exchange_open_orders: tuple[BinanceOrderResult, ...]
    missing_on_exchange: tuple[str, ...]
    unknown_on_exchange: tuple[str, ...]
    account: dict[str, Any]
    recent_trades: tuple[dict[str, Any], ...]
    reconciliation_required: tuple[str, ...] = ()


class BinanceSpotTestnetGateway:
    """Signed Binance Spot Testnet REST gateway.

    This gateway refuses non-Testnet URLs unless explicitly allowed in config.
    """

    def __init__(
        self,
        config: BinanceExecutionConfig | None = None,
        credentials: BinanceCredentials | None = None,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.config = config or BinanceExecutionConfig()
        if self.config.environment != "testnet":
            raise ValueError(
                "BinanceSpotTestnetGateway only accepts environment=testnet"
            )
        self.credentials = credentials
        self._session = session
        self._owns_session = False
        self._time_offset_ms = 0
        self.last_rate_limits: dict[str, str] = {}
        self._symbol_rules: dict[str, BinanceSymbolRules] = {}

    async def __aenter__(self) -> BinanceSpotTestnetGateway:
        await self._get_session()
        return self

    async def __aexit__(self, *_args) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
            self._owns_session = False

    async def ping(self) -> None:
        await self._request("GET", "/api/v3/ping", signed=False)

    async def sync_time(self) -> int:
        started_ms = time.time_ns() // 1_000_000
        payload = await self._request(
            "GET",
            "/api/v3/time",
            signed=False,
        )
        if not isinstance(payload, dict) or "serverTime" not in payload:
            raise BinanceInvalidResponseError(200, repr(payload))
        completed_ms = time.time_ns() // 1_000_000
        server_time = int(payload["serverTime"])
        local_midpoint_ms = (started_ms + completed_ms) // 2
        self._time_offset_ms = server_time - local_midpoint_ms
        return self._time_offset_ms

    async def ticker_price(self, symbol: str) -> Decimal:
        payload = await self._request(
            "GET",
            "/api/v3/ticker/price",
            {"symbol": symbol.upper()},
            signed=False,
        )
        if not isinstance(payload, dict) or "price" not in payload:
            raise BinanceInvalidResponseError(200, repr(payload))
        price = Decimal(str(payload["price"]))
        if not price.is_finite() or price <= 0:
            raise BinanceInvalidResponseError(200, repr(payload))
        return price

    async def symbol_rules(self, symbol: str) -> BinanceSymbolRules:
        normalized_symbol = symbol.upper()
        cached = self._symbol_rules.get(normalized_symbol)
        if cached is not None:
            return cached
        payload = await self._request(
            "GET",
            "/api/v3/exchangeInfo",
            {"symbol": normalized_symbol},
            signed=False,
        )
        if not isinstance(payload, dict):
            raise BinanceInvalidResponseError(200, repr(payload))
        symbols = payload.get("symbols", [])
        if len(symbols) != 1:
            raise BinanceGatewayError(
                f"exchange metadata missing for {normalized_symbol}"
            )
        if symbols[0].get("status") != "TRADING":
            raise BinanceGatewayError(
                f"symbol {normalized_symbol} is not in TRADING status"
            )
        filters = {
            item["filterType"]: item
            for item in symbols[0].get("filters", [])
        }
        price_filter = filters["PRICE_FILTER"]
        lot_filter = filters["LOT_SIZE"]
        market_lot_filter = filters.get(
            "MARKET_LOT_SIZE",
            lot_filter,
        )
        notional_filter = filters.get("NOTIONAL") or filters.get(
            "MIN_NOTIONAL", {}
        )
        notional_filter_type = notional_filter.get("filterType")
        raw_market_step_size = Decimal(
            market_lot_filter["stepSize"]
        )
        market_step_size = (
            raw_market_step_size
            if raw_market_step_size > 0
            else None
        )
        market_minimum_quantity = Decimal(market_lot_filter["minQty"])
        market_maximum_quantity = Decimal(market_lot_filter["maxQty"])
        if market_maximum_quantity <= 0:
            market_minimum_quantity = Decimal(lot_filter["minQty"])
            market_maximum_quantity = Decimal(lot_filter["maxQty"])
        rules = BinanceSymbolRules(
            symbol=normalized_symbol,
            base_asset=str(symbols[0]["baseAsset"]),
            quote_asset=str(symbols[0]["quoteAsset"]),
            tick_size=Decimal(price_filter["tickSize"]),
            minimum_price=Decimal(price_filter["minPrice"]),
            maximum_price=Decimal(price_filter["maxPrice"]),
            step_size=Decimal(lot_filter["stepSize"]),
            minimum_quantity=Decimal(lot_filter["minQty"]),
            maximum_quantity=Decimal(lot_filter["maxQty"]),
            market_step_size=market_step_size,
            market_minimum_quantity=market_minimum_quantity,
            market_maximum_quantity=market_maximum_quantity,
            minimum_notional=Decimal(
                notional_filter.get(
                    "minNotional",
                    "0",
                )
            ),
            maximum_notional=(
                Decimal(notional_filter["maxNotional"])
                if Decimal(
                    notional_filter.get("maxNotional", "0")
                )
                > 0
                else None
            ),
            minimum_notional_applies_to_market=(
                bool(notional_filter.get("applyMinToMarket", False))
                if notional_filter_type == "NOTIONAL"
                else bool(notional_filter.get("applyToMarket", False))
            ),
            maximum_notional_applies_to_market=bool(
                notional_filter.get("applyMaxToMarket", False)
            ),
        )
        self._symbol_rules[normalized_symbol] = rules
        return rules

    async def normalize_order(
        self,
        order: Order,
        reference_price: Decimal | None = None,
    ) -> NormalizedBinanceOrder:
        if not order.quantity.is_finite() or order.quantity <= 0:
            raise BinanceGatewayError(
                "order quantity must be finite and positive"
            )
        if (
            order.price is not None
            and (not order.price.is_finite() or order.price <= 0)
        ):
            raise BinanceGatewayError(
                "order price must be finite and positive"
            )
        if (
            reference_price is not None
            and (
                not reference_price.is_finite()
                or reference_price <= 0
            )
        ):
            raise BinanceGatewayError(
                "reference price must be finite and positive"
            )
        rules = await self.symbol_rules(order.symbol)
        if order.order_type is OrderType.MARKET:
            step_size = rules.market_step_size
            minimum_quantity = rules.market_minimum_quantity
            maximum_quantity = rules.market_maximum_quantity
        else:
            step_size = rules.step_size
            minimum_quantity = rules.minimum_quantity
            maximum_quantity = rules.maximum_quantity
        quantity = (
            order.quantity
            if step_size is None
            else _round_to_step(
                order.quantity,
                step_size,
                ROUND_DOWN,
            )
        )
        if not (
            minimum_quantity
            <= quantity
            <= maximum_quantity
        ):
            raise BinanceGatewayError(
                f"quantity {quantity} is outside Binance limits "
                f"{minimum_quantity}..{maximum_quantity}"
            )
        price: Decimal | None = None
        if order.order_type is not OrderType.MARKET:
            if order.price is None:
                raise BinanceGatewayError(
                    "limit and post-only orders require a price"
                )
            rounding = (
                ROUND_DOWN if order.side is Side.BUY else ROUND_UP
            )
            price = (
                _round_to_step(
                    order.price,
                    rules.tick_size,
                    rounding,
                )
                if rules.tick_size > 0
                else order.price
            )
            if (
                rules.minimum_price > 0
                and price < rules.minimum_price
            ) or (
                rules.maximum_price > 0
                and price > rules.maximum_price
            ):
                raise BinanceGatewayError(
                    f"price {price} is outside Binance limits "
                    f"{rules.minimum_price}..{rules.maximum_price}"
                )
        notional_price = price or reference_price
        apply_minimum_notional = (
            order.order_type is not OrderType.MARKET
            or rules.minimum_notional_applies_to_market
        )
        apply_maximum_notional = (
            order.order_type is not OrderType.MARKET
            or rules.maximum_notional_applies_to_market
        )
        if (
            notional_price is not None
            and apply_minimum_notional
            and notional_price * quantity < rules.minimum_notional
        ):
            raise BinanceGatewayError(
                "order notional is below Binance minimum: "
                f"{notional_price * quantity} < {rules.minimum_notional}"
            )
        if (
            notional_price is not None
            and apply_maximum_notional
            and rules.maximum_notional is not None
            and notional_price * quantity > rules.maximum_notional
        ):
            raise BinanceGatewayError(
                "order notional exceeds Binance maximum: "
                f"{notional_price * quantity} > {rules.maximum_notional}"
            )
        return NormalizedBinanceOrder(quantity=quantity, price=price)

    async def submit_order(
        self,
        order: Order,
        *,
        reference_price: Decimal | None = None,
        test_only: bool = False,
    ) -> BinanceOrderResult:
        normalized = await self.normalize_order(order, reference_price)
        params: dict[str, Any] = {
            "symbol": order.symbol.upper(),
            "side": order.side.value.upper(),
            "quantity": _decimal_string(normalized.quantity),
            "newClientOrderId": order.client_order_id,
            "newOrderRespType": "FULL",
        }
        if order.order_type is OrderType.MARKET:
            params["type"] = "MARKET"
        elif order.order_type is OrderType.LIMIT:
            params.update(
                {
                    "type": "LIMIT",
                    "timeInForce": "GTC",
                    "price": _decimal_string(normalized.price),
                }
            )
        elif order.order_type is OrderType.POST_ONLY:
            params.update(
                {
                    "type": "LIMIT_MAKER",
                    "price": _decimal_string(normalized.price),
                }
            )
        else:
            raise BinanceGatewayError(
                f"unsupported Binance order type: {order.order_type}"
            )
        endpoint = (
            "/api/v3/order/test" if test_only else "/api/v3/order"
        )
        try:
            payload = await self._request(
                "POST",
                endpoint,
                params,
                signed=True,
            )
        except BinanceAPIError as error:
            if (
                test_only
                or (
                    error.code not in _UNKNOWN_EXECUTION_STATUS_CODES
                    and error.status < 500
                )
            ):
                raise
            return await self._resolve_ambiguous_submission(order)
        except BinanceInvalidResponseError:
            if test_only:
                raise
            return await self._resolve_ambiguous_submission(order)
        except (
            asyncio.TimeoutError,
            aiohttp.ClientConnectionError,
            aiohttp.ServerDisconnectedError,
        ):
            if test_only:
                raise
            return await self._resolve_ambiguous_submission(order)
        if test_only:
            return BinanceOrderResult(
                symbol=order.symbol.upper(),
                order_id=0,
                client_order_id=order.client_order_id,
                status="TEST_ACCEPTED",
                side=order.side.value.upper(),
                order_type=order.order_type.value.upper(),
                original_quantity=normalized.quantity,
                executed_quantity=Decimal(0),
                cumulative_quote_quantity=Decimal(0),
                price=normalized.price or Decimal(0),
                transact_time_ms=None,
                raw=payload,
            )
        try:
            return _parse_order_result(payload)
        except BinanceInvalidResponseError:
            return await self._resolve_ambiguous_submission(order)

    async def _resolve_ambiguous_submission(
        self,
        order: Order,
    ) -> BinanceOrderResult:
        try:
            return await self.query_order(
                order.symbol,
                client_order_id=order.client_order_id,
            )
        except (
            BinanceAPIError,
            BinanceInvalidResponseError,
            asyncio.TimeoutError,
            aiohttp.ClientConnectionError,
            aiohttp.ServerDisconnectedError,
        ) as error:
            raise BinanceAmbiguousOrderError(
                order.client_order_id
            ) from error

    async def replace_order(
        self,
        order: Order,
        *,
        cancel_order_id: int | None = None,
        cancel_client_order_id: str | None = None,
        reference_price: Decimal | None = None,
    ) -> BinanceOrderResult:
        """Atomically cancel a resting order and place its replacement.

        Binance exposes this as a single ``cancelReplace`` call. It is run
        in ``STOP_ON_FAILURE`` mode so that a failed cancel never leaves the
        replacement resting alongside the original: the account can never
        end up with two live orders for one logical intent.

        The returned result identifies the order that is live on the
        exchange afterwards. Compare its ``client_order_id`` with the
        requested one to tell a completed replacement from an original that
        survived an unknown outcome.
        """
        if cancel_order_id is None and cancel_client_order_id is None:
            raise BinanceGatewayError(
                "replace requires cancel_order_id or cancel_client_order_id"
            )
        normalized = await self.normalize_order(order, reference_price)
        params: dict[str, Any] = {
            "symbol": order.symbol.upper(),
            "side": order.side.value.upper(),
            "cancelReplaceMode": "STOP_ON_FAILURE",
            "quantity": _decimal_string(normalized.quantity),
            "newClientOrderId": order.client_order_id,
            "newOrderRespType": "FULL",
        }
        if cancel_order_id is not None:
            params["cancelOrderId"] = cancel_order_id
        else:
            params["cancelOrigClientOrderId"] = cancel_client_order_id
        if order.order_type is OrderType.MARKET:
            params["type"] = "MARKET"
        elif order.order_type is OrderType.LIMIT:
            params.update(
                {
                    "type": "LIMIT",
                    "timeInForce": "GTC",
                    "price": _decimal_string(normalized.price),
                }
            )
        elif order.order_type is OrderType.POST_ONLY:
            params.update(
                {
                    "type": "LIMIT_MAKER",
                    "price": _decimal_string(normalized.price),
                }
            )
        else:
            raise BinanceGatewayError(
                f"unsupported Binance order type: {order.order_type}"
            )
        try:
            payload = await self._request(
                "POST",
                "/api/v3/order/cancelReplace",
                params,
                signed=True,
            )
        except BinanceAPIError as error:
            if (
                error.code not in _UNKNOWN_EXECUTION_STATUS_CODES
                and error.status < 500
            ):
                raise
            return await self._resolve_ambiguous_replacement(
                order,
                cancel_order_id=cancel_order_id,
                cancel_client_order_id=cancel_client_order_id,
            )
        except (
            BinanceInvalidResponseError,
            asyncio.TimeoutError,
            aiohttp.ClientConnectionError,
            aiohttp.ServerDisconnectedError,
        ):
            return await self._resolve_ambiguous_replacement(
                order,
                cancel_order_id=cancel_order_id,
                cancel_client_order_id=cancel_client_order_id,
            )
        try:
            return _parse_cancel_replace_result(payload)
        except BinanceInvalidResponseError:
            return await self._resolve_ambiguous_replacement(
                order,
                cancel_order_id=cancel_order_id,
                cancel_client_order_id=cancel_client_order_id,
            )

    async def _resolve_ambiguous_replacement(
        self,
        order: Order,
        *,
        cancel_order_id: int | None,
        cancel_client_order_id: str | None,
    ) -> BinanceOrderResult:
        """Ask the exchange which of the two orders actually exists."""
        try:
            return await self.query_order(
                order.symbol,
                client_order_id=order.client_order_id,
            )
        except (
            BinanceAPIError,
            BinanceInvalidResponseError,
            asyncio.TimeoutError,
            aiohttp.ClientConnectionError,
            aiohttp.ServerDisconnectedError,
        ):
            pass
        try:
            return await self.query_order(
                order.symbol,
                order_id=cancel_order_id,
                client_order_id=cancel_client_order_id,
            )
        except (
            BinanceAPIError,
            BinanceInvalidResponseError,
            asyncio.TimeoutError,
            aiohttp.ClientConnectionError,
            aiohttp.ServerDisconnectedError,
        ) as error:
            raise BinanceAmbiguousOrderError(
                order.client_order_id
            ) from error

    async def cancel_order(
        self,
        symbol: str,
        *,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> BinanceOrderResult:
        params = _order_lookup_params(
            symbol,
            order_id,
            client_order_id,
        )
        try:
            payload = await self._request(
                "DELETE",
                "/api/v3/order",
                params,
                signed=True,
            )
        except BinanceAPIError as error:
            if (
                error.code not in _UNKNOWN_EXECUTION_STATUS_CODES
                and error.status < 500
            ):
                raise
            return await self._resolve_ambiguous_order_operation(
                symbol,
                order_id=order_id,
                client_order_id=client_order_id,
            )
        except (
            BinanceInvalidResponseError,
            asyncio.TimeoutError,
            aiohttp.ClientConnectionError,
            aiohttp.ServerDisconnectedError,
        ):
            return await self._resolve_ambiguous_order_operation(
                symbol,
                order_id=order_id,
                client_order_id=client_order_id,
            )
        try:
            return _parse_order_result(payload)
        except BinanceInvalidResponseError:
            return await self._resolve_ambiguous_order_operation(
                symbol,
                order_id=order_id,
                client_order_id=client_order_id,
            )

    async def _resolve_ambiguous_order_operation(
        self,
        symbol: str,
        *,
        order_id: int | None,
        client_order_id: str | None,
    ) -> BinanceOrderResult:
        try:
            return await self.query_order(
                symbol,
                order_id=order_id,
                client_order_id=client_order_id,
            )
        except (
            BinanceAPIError,
            BinanceInvalidResponseError,
            asyncio.TimeoutError,
            aiohttp.ClientConnectionError,
            aiohttp.ServerDisconnectedError,
        ) as error:
            identifier = client_order_id or str(order_id)
            raise BinanceAmbiguousOrderError(identifier) from error

    async def query_order(
        self,
        symbol: str,
        *,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> BinanceOrderResult:
        params = _order_lookup_params(
            symbol,
            order_id,
            client_order_id,
        )
        payload = await self._request(
            "GET",
            "/api/v3/order",
            params,
            signed=True,
        )
        return _parse_order_result(payload)

    async def account(self) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            "/api/v3/account",
            signed=True,
        )
        if not isinstance(payload, dict):
            raise BinanceInvalidResponseError(200, repr(payload))
        return payload

    async def open_orders(
        self,
        symbol: str | None = None,
    ) -> tuple[BinanceOrderResult, ...]:
        params = {"symbol": symbol.upper()} if symbol else {}
        payload = await self._request(
            "GET",
            "/api/v3/openOrders",
            params,
            signed=True,
        )
        if not isinstance(payload, list):
            raise BinanceInvalidResponseError(200, repr(payload))
        return tuple(
            _parse_order_result(item) for item in payload
        )

    async def my_trades(
        self,
        symbol: str,
        *,
        limit: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("myTrades limit must be between 1 and 1000")
        payload = await self._request(
            "GET",
            "/api/v3/myTrades",
            {
                "symbol": symbol.upper(),
                "limit": limit,
            },
            signed=True,
        )
        if not isinstance(payload, list):
            raise BinanceInvalidResponseError(200, repr(payload))
        return tuple(payload)

    async def reconcile(
        self,
        oms: OrderManagementSystem,
        symbol: str,
    ) -> BinanceReconciliationReport:
        exchange_orders = await self.open_orders(symbol)
        account, recent_trades = await asyncio.gather(
            self.account(),
            self.my_trades(symbol),
        )
        locally_tracked = {
            order.client_order_id: order
            for order in oms.open_orders()
            if (
                order.symbol == symbol.upper()
                and order.venue == "binance"
                and order.state
                in {
                    OrderState.SUBMITTED,
                    OrderState.ACKNOWLEDGED,
                    OrderState.PARTIALLY_FILLED,
                    OrderState.RECONCILIATION_REQUIRED,
                }
            )
        }
        local_open = {
            client_order_id: order
            for client_order_id, order in locally_tracked.items()
            if order.state is not OrderState.RECONCILIATION_REQUIRED
        }
        exchange_by_client = {
            order.client_order_id: order
            for order in exchange_orders
        }
        missing = tuple(
            sorted(set(local_open) - set(exchange_by_client))
        )
        unknown = tuple(
            sorted(set(exchange_by_client) - set(locally_tracked))
        )
        reconciliation_required = tuple(
            sorted(
                client_order_id
                for client_order_id, order in locally_tracked.items()
                if order.state is OrderState.RECONCILIATION_REQUIRED
            )
        )
        return BinanceReconciliationReport(
            symbol=symbol.upper(),
            exchange_open_orders=exchange_orders,
            missing_on_exchange=missing,
            unknown_on_exchange=unknown,
            account=account,
            recent_trades=recent_trades,
            reconciliation_required=reconciliation_required,
        )

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool,
        _timestamp_retry: bool = True,
    ) -> Any:
        session = await self._get_session()
        request_params = dict(params or {})
        headers: dict[str, str] = {}
        if signed:
            credentials = self.credentials or BinanceCredentials.from_environment(
                self.config
            )
            headers["X-MBX-APIKEY"] = credentials.api_key
            request_params["timestamp"] = self._timestamp_ms()
            request_params["recvWindow"] = self.config.recv_window_ms
            query = urlencode(
                [
                    (key, _parameter_string(value))
                    for key, value in request_params.items()
                ]
            )
            signature = hmac.new(
                credentials.api_secret.encode(),
                query.encode(),
                hashlib.sha256,
            ).hexdigest()
            url = (
                f"{self.config.rest_base_url}{path}?"
                f"{query}&signature={signature}"
            )
            request_kwargs: dict[str, Any] = {}
        else:
            url = f"{self.config.rest_base_url}{path}"
            request_kwargs = {"params": request_params}
        async with session.request(
            method,
            url,
            headers=headers,
            **request_kwargs,
        ) as response:
            text = await response.text()
            if text:
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    if 200 <= response.status < 300:
                        raise BinanceInvalidResponseError(
                            response.status,
                            text,
                        )
                    payload = {"msg": text}
            else:
                payload = {}
            self.last_rate_limits = {
                key: value
                for key, value in response.headers.items()
                if (
                    key.upper().startswith("X-MBX-")
                    or key.upper() == "RETRY-AFTER"
                )
            }
            if 200 <= response.status < 300:
                return payload
            code = (
                int(payload["code"])
                if isinstance(payload, dict) and "code" in payload
                else None
            )
            message = (
                str(payload.get("msg", text))
                if isinstance(payload, dict)
                else text
            )
            if code == -1021 and signed and _timestamp_retry:
                await self.sync_time()
                return await self._request(
                    method,
                    path,
                    params,
                    signed=True,
                    _timestamp_retry=False,
                )
            raise BinanceAPIError(
                status=response.status,
                code=code,
                message=message,
                payload=payload,
            )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(
                total=self.config.request_timeout_seconds
            )
            self._session = aiohttp.ClientSession(timeout=timeout)
            self._owns_session = True
        return self._session

    def _timestamp_ms(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms


class BinanceExecutionService:
    """Applies Binance Testnet responses to the local OMS."""

    def __init__(
        self,
        gateway: BinanceSpotTestnetGateway,
        oms: OrderManagementSystem,
    ) -> None:
        self.gateway = gateway
        self.oms = oms

    async def submit(
        self,
        order_id: UUID,
        *,
        reference_price: Decimal | None = None,
        test_only: bool = False,
    ) -> BinanceOrderResult:
        order = _find_order(self.oms, order_id)
        if order.state is not OrderState.RISK_APPROVED:
            raise BinanceGatewayError(
                "OMS order must be risk-approved before submission"
            )
        order = self.oms.assign_venue(order.order_id, "binance")
        normalized = await self.gateway.normalize_order(
            order,
            reference_price=reference_price,
        )
        order = self.oms.update_order_terms(
            order.order_id,
            quantity=normalized.quantity,
            price=normalized.price,
        )
        order = self.oms.transition(
            order.order_id,
            OrderState.SUBMITTED,
            time.time_ns(),
        )
        try:
            result = await self.gateway.submit_order(
                order,
                reference_price=reference_price,
                test_only=test_only,
            )
        except BinanceAmbiguousOrderError:
            raise
        except BinanceAPIError as error:
            self.oms.transition(
                order.order_id,
                OrderState.REJECTED,
                time.time_ns(),
                reason=f"binance_{error.code}",
            )
            raise
        except BinanceGatewayError:
            self.oms.transition(
                order.order_id,
                OrderState.REJECTED,
                time.time_ns(),
                reason="binance_local_validation",
            )
            raise
        if test_only:
            self.oms.transition(
                order.order_id,
                OrderState.REJECTED,
                time.time_ns(),
                reason="binance_test_order_accepted",
            )
            return result
        self.apply_result(order.order_id, result)
        return result

    async def cancel(self, order_id: UUID) -> BinanceOrderResult:
        order = _find_order(self.oms, order_id)
        try:
            result = await self.gateway.cancel_order(
                order.symbol,
                client_order_id=order.client_order_id,
            )
        except BinanceAmbiguousOrderError:
            current = _find_order(self.oms, order.order_id)
            if current.state in {
                OrderState.SUBMITTED,
                OrderState.ACKNOWLEDGED,
                OrderState.PARTIALLY_FILLED,
            }:
                self.oms.transition(
                    current.order_id,
                    OrderState.RECONCILIATION_REQUIRED,
                    time.time_ns(),
                    cumulative_filled_quantity=(
                        _latest_cumulative_filled_quantity(
                            self.oms,
                            current.order_id,
                        )
                    ),
                    average_fill_price=_latest_average_fill_price(
                        self.oms,
                        current.order_id,
                    ),
                    reason="binance_cancel_outcome_ambiguous",
                )
            raise
        self.apply_result(order.order_id, result)
        return result

    async def replace(
        self,
        order_id: UUID,
        *,
        cancel_order_id: int | None = None,
        cancel_client_order_id: str | None = None,
        replaced_order_id: UUID | None = None,
        reference_price: Decimal | None = None,
    ) -> BinanceOrderResult:
        """Replace a resting order with a risk-approved new order.

        The replacement is a new order in its own right, so it carries its
        own client order ID and must clear risk before it is sent. That is
        what keeps a modification from becoming a way to bypass limits.
        """
        order = _find_order(self.oms, order_id)
        if order.state is not OrderState.RISK_APPROVED:
            raise BinanceGatewayError(
                "OMS order must be risk-approved before replacement"
            )
        if cancel_order_id is None and cancel_client_order_id is None:
            if replaced_order_id is None:
                raise BinanceGatewayError(
                    "replacement requires the order being replaced"
                )
            cancel_client_order_id = _find_order(
                self.oms,
                replaced_order_id,
            ).client_order_id
        order = self.oms.assign_venue(order.order_id, "binance")
        normalized = await self.gateway.normalize_order(
            order,
            reference_price=reference_price,
        )
        order = self.oms.update_order_terms(
            order.order_id,
            quantity=normalized.quantity,
            price=normalized.price,
        )
        order = self.oms.transition(
            order.order_id,
            OrderState.SUBMITTED,
            time.time_ns(),
        )
        try:
            result = await self.gateway.replace_order(
                order,
                cancel_order_id=cancel_order_id,
                cancel_client_order_id=cancel_client_order_id,
                reference_price=reference_price,
            )
        except BinanceAmbiguousOrderError:
            self.oms.transition(
                order.order_id,
                OrderState.RECONCILIATION_REQUIRED,
                time.time_ns(),
                reason="binance_replace_outcome_ambiguous",
            )
            raise
        except BinanceAPIError as error:
            self.oms.transition(
                order.order_id,
                OrderState.REJECTED,
                time.time_ns(),
                reason=f"binance_{error.code}",
            )
            raise
        except BinanceGatewayError:
            self.oms.transition(
                order.order_id,
                OrderState.REJECTED,
                time.time_ns(),
                reason="binance_local_validation",
            )
            raise
        if result.client_order_id != order.client_order_id:
            # The exchange resolved an unknown outcome to the original
            # order, so the replacement never became live.
            self.oms.transition(
                order.order_id,
                OrderState.RECONCILIATION_REQUIRED,
                time.time_ns(),
                reason="binance_replacement_not_live",
            )
            return result
        self.apply_result(order.order_id, result)
        if replaced_order_id is not None:
            replaced = _find_order(self.oms, replaced_order_id)
            if replaced.state is OrderState.SUBMITTED:
                # The order was never acknowledged locally, so the OMS
                # cannot jump straight to a terminal state. The exchange
                # confirmed the cancel leg, so reconcile first and then
                # record the outcome it reported.
                replaced = self.oms.transition(
                    replaced.order_id,
                    OrderState.RECONCILIATION_REQUIRED,
                    time.time_ns(),
                    reason="binance_replaced",
                )
            if replaced.state in {
                OrderState.ACKNOWLEDGED,
                OrderState.PARTIALLY_FILLED,
                OrderState.RECONCILIATION_REQUIRED,
            }:
                self.oms.transition(
                    replaced.order_id,
                    OrderState.CANCELED,
                    time.time_ns(),
                    cumulative_filled_quantity=(
                        _latest_cumulative_filled_quantity(
                            self.oms,
                            replaced.order_id,
                        )
                    ),
                    average_fill_price=_latest_average_fill_price(
                        self.oms,
                        replaced.order_id,
                    ),
                    reason="binance_replaced",
                )
        return result

    async def query_and_apply(
        self,
        order_id: UUID,
    ) -> BinanceOrderResult:
        order = _find_order(self.oms, order_id)
        result = await self.gateway.query_order(
            order.symbol,
            client_order_id=order.client_order_id,
        )
        self.apply_result(order.order_id, result)
        return result

    async def reconcile(
        self,
        symbol: str,
    ) -> BinanceReconciliationReport:
        report = await self.gateway.reconcile(self.oms, symbol)
        local_by_client = {
            order.client_order_id: order
            for order in self.oms.open_orders()
            if (
                order.symbol == symbol.upper()
                and order.venue == "binance"
            )
        }
        for exchange_order in report.exchange_open_orders:
            local = local_by_client.get(
                exchange_order.client_order_id
            )
            if local is not None:
                self.apply_result(local.order_id, exchange_order)
        for client_order_id in report.missing_on_exchange:
            local = local_by_client.get(client_order_id)
            if local is None:
                continue
            try:
                result = await self.gateway.query_order(
                    local.symbol,
                    client_order_id=client_order_id,
                )
            except BinanceAPIError as error:
                if error.code == -2013:
                    self.oms.transition(
                        local.order_id,
                        OrderState.RECONCILIATION_REQUIRED,
                        time.time_ns(),
                        cumulative_filled_quantity=(
                            _latest_cumulative_filled_quantity(
                                self.oms,
                                local.order_id,
                            )
                        ),
                        average_fill_price=_latest_average_fill_price(
                            self.oms,
                            local.order_id,
                        ),
                        reason=(
                            "binance_order_not_found_requires_"
                            "operator_recovery"
                        ),
                    )
                    continue
                raise
            self.apply_result(local.order_id, result)
        reconciliation_required = tuple(
            sorted(
                order.client_order_id
                for order in self.oms.open_orders()
                if (
                    order.symbol == symbol.upper()
                    and order.venue == "binance"
                    and order.state
                    is OrderState.RECONCILIATION_REQUIRED
                )
            )
        )
        return replace(
            report,
            reconciliation_required=reconciliation_required,
        )

    def apply_result(
        self,
        order_id: UUID,
        result: BinanceOrderResult,
    ) -> None:
        order = _find_order(self.oms, order_id)
        if result.symbol.upper() != order.symbol.upper():
            raise BinanceGatewayError(
                "Binance result symbol does not match the OMS order"
            )
        if (
            not result.executed_quantity.is_finite()
            or not result.cumulative_quote_quantity.is_finite()
            or result.executed_quantity < 0
            or result.cumulative_quote_quantity < 0
            or result.executed_quantity > order.quantity
        ):
            raise BinanceGatewayError(
                "Binance cumulative executed quantity is invalid"
            )
        previous_filled = _latest_cumulative_filled_quantity(
            self.oms,
            order_id,
        )
        if result.executed_quantity < previous_filled:
            raise BinanceGatewayError(
                "Binance cumulative executed quantity regressed"
            )
        target = _binance_status_to_order_state(result.status)
        timestamp_ns = (
            result.transact_time_ms * 1_000_000
            if result.transact_time_ms is not None
            else time.time_ns()
        )
        if (
            order.state is OrderState.SUBMITTED
            and target
            in {
                OrderState.ACKNOWLEDGED,
                OrderState.PARTIALLY_FILLED,
                OrderState.FILLED,
                OrderState.CANCELED,
            }
        ):
            order = self.oms.transition(
                order.order_id,
                OrderState.ACKNOWLEDGED,
                timestamp_ns,
            )
        if (
            order.state is target
            and target is not OrderState.PARTIALLY_FILLED
        ):
            return
        if (
            order.state is OrderState.PARTIALLY_FILLED
            and target is OrderState.PARTIALLY_FILLED
            and result.executed_quantity == previous_filled
        ):
            return
        if (
            order.state is OrderState.PARTIALLY_FILLED
            and target is OrderState.ACKNOWLEDGED
        ):
            return
        self.oms.transition(
            order.order_id,
            target,
            timestamp_ns,
            cumulative_filled_quantity=result.executed_quantity,
            average_fill_price=result.average_fill_price,
            reason=None,
        )


def sign_query(
    params: dict[str, Any],
    secret: str,
) -> tuple[str, str]:
    query = urlencode(
        [
            (key, _parameter_string(value))
            for key, value in params.items()
        ]
    )
    signature = hmac.new(
        secret.encode(),
        query.encode(),
        hashlib.sha256,
    ).hexdigest()
    return query, signature


def _round_to_step(
    value: Decimal,
    step: Decimal,
    rounding: str,
) -> Decimal:
    units = (value / step).to_integral_value(rounding=rounding)
    return units * step


def _decimal_string(value: Decimal | None) -> str:
    if value is None:
        raise ValueError("decimal value is required")
    return format(value, "f")


def _parameter_string(value: Any) -> str:
    if isinstance(value, Decimal):
        return _decimal_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _order_lookup_params(
    symbol: str,
    order_id: int | None,
    client_order_id: str | None,
) -> dict[str, Any]:
    if (order_id is None) == (client_order_id is None):
        raise ValueError(
            "provide exactly one of order_id or client_order_id"
        )
    params: dict[str, Any] = {"symbol": symbol.upper()}
    if order_id is not None:
        params["orderId"] = order_id
    else:
        params["origClientOrderId"] = client_order_id
    return params


def _find_order(
    oms: OrderManagementSystem,
    order_id: UUID,
) -> Order:
    for order in oms.orders:
        if order.order_id == order_id:
            return order
    raise KeyError(f"OMS order does not exist: {order_id}")


def _latest_cumulative_filled_quantity(
    oms: OrderManagementSystem,
    order_id: UUID,
) -> Decimal:
    quantities = [
        update.cumulative_filled_quantity
        for update in oms.updates
        if update.order_id == order_id
    ]
    return max(quantities, default=Decimal(0))


def _latest_average_fill_price(
    oms: OrderManagementSystem,
    order_id: UUID,
) -> Decimal | None:
    for update in reversed(oms.updates):
        if (
            update.order_id == order_id
            and update.average_fill_price is not None
        ):
            return update.average_fill_price
    return None


def _binance_status_to_order_state(status: str) -> OrderState:
    normalized = status.upper()
    mapping = {
        "NEW": OrderState.ACKNOWLEDGED,
        "PENDING_NEW": OrderState.ACKNOWLEDGED,
        "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
        "FILLED": OrderState.FILLED,
        "CANCELED": OrderState.CANCELED,
        "PENDING_CANCEL": OrderState.ACKNOWLEDGED,
        "REJECTED": OrderState.REJECTED,
        "EXPIRED": OrderState.EXPIRED,
        "EXPIRED_IN_MATCH": OrderState.EXPIRED,
    }
    try:
        return mapping[normalized]
    except KeyError as error:
        raise BinanceGatewayError(
            f"unsupported Binance order status: {status}"
        ) from error


def _parse_cancel_replace_result(payload: Any) -> BinanceOrderResult:
    """Extract the replacement order from a cancelReplace response.

    Binance nests the two outcomes under ``cancelResponse`` and
    ``newOrderResponse`` rather than returning a bare order object.
    """
    if not isinstance(payload, dict):
        raise BinanceInvalidResponseError(200, repr(payload))
    new_order = payload.get("newOrderResponse")
    if not isinstance(new_order, dict):
        raise BinanceInvalidResponseError(
            200,
            json.dumps(payload, default=str),
        )
    return _parse_order_result(new_order)


def _parse_order_result(payload: Any) -> BinanceOrderResult:
    if not isinstance(payload, dict):
        raise BinanceInvalidResponseError(200, repr(payload))
    try:
        return BinanceOrderResult.from_payload(payload)
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise BinanceInvalidResponseError(
            200,
            json.dumps(payload, default=str),
        ) from error
