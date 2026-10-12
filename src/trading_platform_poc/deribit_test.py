from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

import aiohttp

from trading_platform_poc.exchange_adapter import (
    AccountSnapshot,
    AmbiguousOrderError,
    ExchangeAPIError,
    ExchangeAdapterError,
    ExchangeCredentialsError,
    ExchangeEndpointError,
    ExchangeInvalidResponseError,
    ExchangeOrder,
    ExchangeTrade,
    InstrumentRules,
    NormalizedOrder,
    QuantityUnit,
    ReconciliationSnapshot,
    RequestTelemetry,
    decimal_string,
    deterministic_client_id,
    local_open_client_ids,
    round_price,
    round_quantity,
)
from trading_platform_poc.models import Order, OrderType, Side
from trading_platform_poc.oms import OrderManagementSystem


DERIBIT_TEST_BASE_URL = "https://test.deribit.com/api/v2"

_DERIBIT_ENDPOINTS = frozenset(
    {
        ("GET", "/public/get_time"),
        ("GET", "/public/get_instruments"),
        ("GET", "/private/buy"),
        ("GET", "/private/sell"),
        ("GET", "/private/cancel"),
        ("GET", "/private/get_order_state"),
        ("GET", "/private/get_open_orders"),
        ("GET", "/private/get_open_orders_by_label"),
        ("GET", "/private/get_order_history_by_currency"),
        ("GET", "/private/get_user_trades_by_instrument"),
        ("GET", "/private/get_account_summary"),
    }
)


@dataclass(frozen=True, slots=True)
class DeribitTestConfig:
    rest_base_url: str = DERIBIT_TEST_BASE_URL
    client_id_environment_variable: str = "DERIBIT_TEST_CLIENT_ID"
    client_secret_environment_variable: str = "DERIBIT_TEST_CLIENT_SECRET"
    request_timeout_seconds: float = 10.0
    transaction_expiry_ms: int = 5_000
    cancel_reconciliation_attempts: int = 3
    cancel_reconciliation_interval_seconds: float = 0.1
    history_page_size: int = 1_000
    instrument_kind: str = "future"

    def __post_init__(self) -> None:
        _validate_base_url(self.rest_base_url)
        if self.request_timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")
        if not 1_000 <= self.transaction_expiry_ms <= 30_000:
            raise ValueError("transaction expiry must be 1000..30000 ms")
        if self.cancel_reconciliation_attempts < 1:
            raise ValueError("cancel reconciliation attempts must be positive")
        if self.cancel_reconciliation_interval_seconds < 0:
            raise ValueError(
                "cancel reconciliation interval cannot be negative"
            )
        if not 1 <= self.history_page_size <= 1_000:
            raise ValueError("history page size must be 1..1000")
        if self.instrument_kind in {"future_combo", "option_combo"}:
            raise ValueError(
                "Deribit combo instruments are unsupported because the "
                "execution model does not track leg-level exposure"
            )
        if self.instrument_kind not in {"future", "option", "spot"}:
            raise ValueError("unsupported Deribit instrument kind")


@dataclass(frozen=True, slots=True)
class DeribitCredentials:
    client_id: str
    client_secret: str

    @classmethod
    def from_environment(
        cls,
        config: DeribitTestConfig,
    ) -> DeribitCredentials:
        client_id = os.environ.get(config.client_id_environment_variable, "")
        client_secret = os.environ.get(
            config.client_secret_environment_variable,
            "",
        )
        if not client_id or not client_secret:
            raise ExchangeCredentialsError(
                "Deribit Test credentials are missing. Set "
                f"{config.client_id_environment_variable} and "
                f"{config.client_secret_environment_variable}."
            )
        return cls(client_id, client_secret)


def sign_deribit_http_request(
    method: str,
    uri: str,
    body: str,
    timestamp_ms: int,
    nonce: str,
    secret: str,
) -> str:
    request_data = f"{method.upper()}\n{uri}\n{body}\n"
    string_to_sign = f"{timestamp_ms}\n{nonce}\n{request_data}"
    return hmac.new(
        secret.encode(),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()


def sign_deribit_client_auth(
    timestamp_ms: int,
    nonce: str,
    data: str,
    secret: str,
) -> str:
    message = f"{timestamp_ms}\n{nonce}\n{data}"
    return hmac.new(
        secret.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()


class DeribitTestAdapter:
    """Deribit HTTP API adapter locked to the official test environment."""

    venue = "deribit"

    def __init__(
        self,
        config: DeribitTestConfig | None = None,
        credentials: DeribitCredentials | None = None,
        *,
        session: aiohttp.ClientSession | None = None,
        clock_ms: Callable[[], int] | None = None,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self.config = config or DeribitTestConfig()
        _validate_base_url(self.config.rest_base_url)
        self.credentials = credentials
        self._session = session
        self._owns_session = False
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._nonce_factory = nonce_factory or (lambda: secrets.token_hex(8))
        self._time_offset_ms = 0
        self._rules: dict[str, InstrumentRules] = {}
        self.last_request: RequestTelemetry | None = None

    async def __aenter__(self) -> DeribitTestAdapter:
        await self._get_session()
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
            self._owns_session = False

    def client_order_id(self, order: Order) -> str:
        return deterministic_client_id(
            "deribit",
            order.client_order_id,
            maximum_length=64,
            alphanumeric_only=False,
        )

    async def sync_time(self) -> int:
        server_ms = await self._request("GET", "/public/get_time", signed=False)
        if not isinstance(server_ms, int):
            raise ExchangeInvalidResponseError(
                self.venue,
                200,
                json.dumps(server_ms),
            )
        self._time_offset_ms = server_ms - self._clock_ms()
        return self._time_offset_ms

    async def instrument_rules(self, symbol: str) -> InstrumentRules:
        normalized_symbol = symbol.upper()
        cached = self._rules.get(normalized_symbol)
        if cached is not None:
            return cached
        currency = _instrument_currency(normalized_symbol)
        # Linear instruments are indexed under their settlement currency
        # (ETH_USDC-PERPETUAL lives under USDC, not ETH), so the symbol-derived
        # currency cannot be used to look them up. ``any`` is the documented
        # cross-currency selector; the exact instrument name is filtered below.
        rows = await self._request(
            "GET",
            "/public/get_instruments",
            {
                "currency": "any",
                "kind": self.config.instrument_kind,
                "expired": False,
            },
            signed=False,
        )
        if not isinstance(rows, list):
            raise ExchangeInvalidResponseError(
                self.venue,
                200,
                json.dumps(rows),
            )
        matching = [
            row
            for row in rows
            if isinstance(row, dict)
            and row.get("instrument_name") == normalized_symbol
        ]
        if len(matching) != 1:
            raise ExchangeAdapterError(
                f"Deribit instrument metadata missing for {normalized_symbol}"
            )
        row = matching[0]
        if row.get("kind") != self.config.instrument_kind:
            raise ExchangeInvalidResponseError(
                self.venue,
                200,
                json.dumps(row),
            )
        if row.get("is_active") is not True:
            raise ExchangeAdapterError(
                f"Deribit instrument {normalized_symbol} is not active"
            )
        try:
            minimum = Decimal(str(row["min_trade_amount"]))
            quantity_step = Decimal(
                str(row.get("qty_tick_size") or row["min_trade_amount"])
            )
            quantity_unit = _deribit_amount_unit(row)
            rules = InstrumentRules(
                venue=self.venue,
                symbol=normalized_symbol,
                tick_size=Decimal(str(row["tick_size"])),
                quantity_step=quantity_step,
                minimum_quantity=minimum,
                base_currency=str(row.get("base_currency") or currency),
                quote_currency=(
                    str(
                        row.get("quote_currency")
                        or row.get("counter_currency")
                    )
                    if row.get("quote_currency")
                    or row.get("counter_currency")
                    else None
                ),
                settlement_currency=(
                    str(row["settlement_currency"])
                    if row.get("settlement_currency")
                    else None
                ),
                quantity_unit=quantity_unit,
                instrument_type=(
                    str(row["instrument_type"])
                    if row.get("instrument_type")
                    else None
                ),
                contract_size=_optional_positive_decimal(
                    row.get("contract_size")
                ),
                raw=row,
            )
        except (KeyError, ValueError, ArithmeticError) as error:
            raise ExchangeInvalidResponseError(
                self.venue,
                200,
                json.dumps(rows),
            ) from error
        if (
            rules.tick_size <= 0
            or rules.quantity_step <= 0
            or rules.minimum_quantity <= 0
        ):
            raise ExchangeInvalidResponseError(
                self.venue,
                200,
                json.dumps(rows),
            )
        self._rules[normalized_symbol] = rules
        return rules

    async def normalize_order(
        self,
        order: Order,
        *,
        reference_price: Decimal | None = None,
    ) -> NormalizedOrder:
        del reference_price
        rules = await self.instrument_rules(order.symbol)
        quantity = round_quantity(order.quantity, rules.quantity_step)
        if quantity < rules.minimum_quantity:
            raise ExchangeAdapterError(
                f"Deribit quantity {quantity} is below minimum "
                f"{rules.minimum_quantity}"
            )
        price: Decimal | None = None
        if order.order_type is not OrderType.MARKET:
            if order.price is None:
                raise ExchangeAdapterError(
                    "Deribit limit and post-only orders require a price"
                )
            tick = _deribit_tick_size(rules, order.price)
            price = round_price(order.price, tick, order.side)
        return NormalizedOrder(
            quantity=quantity,
            price=price,
            client_order_id=self.client_order_id(order),
        )

    async def submit_order(self, order: Order) -> ExchangeOrder:
        normalized = await self.normalize_order(order)
        endpoint = "/private/buy" if order.side is Side.BUY else "/private/sell"
        params: dict[str, Any] = {
            "instrument_name": order.symbol.upper(),
            "amount": decimal_string(normalized.quantity),
            "type": (
                "market"
                if order.order_type is OrderType.MARKET
                else "limit"
            ),
            "label": normalized.client_order_id,
            "time_in_force": "good_til_cancelled",
            "post_only": order.order_type is OrderType.POST_ONLY,
            "reject_post_only": order.order_type is OrderType.POST_ONLY,
            "valid_until": self._server_time_ms()
            + self.config.transaction_expiry_ms,
        }
        if normalized.price is not None:
            params["price"] = decimal_string(normalized.price)
        try:
            result = await self._request(
                "GET",
                endpoint,
                params,
                signed=True,
            )
            if not isinstance(result, dict) or not isinstance(
                result.get("order"),
                dict,
            ):
                raise ExchangeInvalidResponseError(
                    self.venue,
                    200,
                    json.dumps(result),
                )
            return self._validated_order(
                result["order"],
                symbol=order.symbol,
                client_order_id=normalized.client_order_id,
            )
        except ExchangeAPIError as error:
            if error.status < 500:
                raise
        except ExchangeInvalidResponseError:
            pass
        except _TRANSPORT_ERRORS:
            pass
        return await self._resolve_ambiguous_submission(
            order.symbol,
            normalized.client_order_id,
        )

    async def _resolve_ambiguous_submission(
        self,
        symbol: str,
        label: str,
    ) -> ExchangeOrder:
        try:
            currency = await self._reconciliation_currency(symbol)
            open_matches = await self._orders_by_label(
                symbol,
                currency,
                label,
            )
            if len(open_matches) == 1:
                return open_matches[0]
            matches = await self._history_matches_by_label(
                symbol,
                currency,
                label,
            )
            if len(matches) == 1:
                return matches[0]
        except _RECONCILIATION_ERRORS as error:
            raise AmbiguousOrderError(self.venue, label) from error
        raise AmbiguousOrderError(self.venue, label)

    async def cancel_order(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> ExchangeOrder:
        order_id = exchange_order_id
        if order_id is None:
            if not client_order_id:
                raise ValueError(
                    "provide exchange_order_id or client_order_id"
                )
            matched = await self.query_order(
                symbol,
                client_order_id=client_order_id,
            )
            order_id = matched.exchange_order_id
        try:
            result = await self._request(
                "GET",
                "/private/cancel",
                {"order_id": order_id},
                signed=True,
            )
            if not isinstance(result, dict):
                raise ExchangeInvalidResponseError(
                    self.venue,
                    200,
                    json.dumps(result),
                )
            return self._validated_order(
                result,
                symbol=symbol,
                exchange_order_id=order_id,
                client_order_id=client_order_id,
            )
        except ExchangeAPIError as error:
            if error.status < 500:
                raise
        except ExchangeInvalidResponseError:
            pass
        except _TRANSPORT_ERRORS:
            pass
        return await self._resolve_ambiguous_cancellation(
            symbol,
            order_id,
            client_order_id=client_order_id,
        )

    async def _resolve_ambiguous_cancellation(
        self,
        symbol: str,
        order_id: str,
        *,
        client_order_id: str | None,
    ) -> ExchangeOrder:
        last_error: BaseException | None = None
        for attempt in range(self.config.cancel_reconciliation_attempts):
            try:
                result = await self.query_order(
                    symbol,
                    exchange_order_id=order_id,
                )
            except _RECONCILIATION_ERRORS as error:
                last_error = error
            else:
                if result.status in {"cancelled", "filled"}:
                    return result
                last_error = ExchangeAdapterError(
                    "Deribit cancellation remains non-terminal with state "
                    f"{result.status}"
                )
            if (
                attempt + 1 < self.config.cancel_reconciliation_attempts
                and self.config.cancel_reconciliation_interval_seconds
            ):
                await asyncio.sleep(
                    self.config.cancel_reconciliation_interval_seconds
                )
        raise AmbiguousOrderError(
            self.venue,
            client_order_id or order_id,
        ) from last_error

    async def query_order(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> ExchangeOrder:
        if bool(exchange_order_id) == bool(client_order_id):
            raise ValueError(
                "provide exactly one of exchange_order_id or client_order_id"
            )
        if exchange_order_id:
            result = await self._request(
                "GET",
                "/private/get_order_state",
                {"order_id": exchange_order_id},
                signed=True,
            )
            if not isinstance(result, dict):
                raise ExchangeInvalidResponseError(
                    self.venue,
                    200,
                    json.dumps(result),
                )
            return self._validated_order(
                result,
                symbol=symbol,
                exchange_order_id=exchange_order_id,
            )
        label = str(client_order_id)
        currency = await self._reconciliation_currency(symbol)
        open_matches = await self._orders_by_label(symbol, currency, label)
        if len(open_matches) == 1:
            return open_matches[0]
        matches = await self._history_matches_by_label(
            symbol,
            currency,
            label,
        )
        if len(matches) != 1:
            raise ExchangeAPIError(
                venue=self.venue,
                status=404,
                code=10004,
                message="order_not_found",
                payload={"label": label},
            )
        return matches[0]

    async def open_orders(
        self,
        symbol: str | None = None,
    ) -> tuple[ExchangeOrder, ...]:
        result = await self._request(
            "GET",
            "/private/get_open_orders",
            {"kind": self.config.instrument_kind, "type": "all"},
            signed=True,
        )
        if not isinstance(result, list):
            raise ExchangeInvalidResponseError(
                self.venue,
                200,
                json.dumps(result),
            )
        orders = tuple(
            _parse_deribit_order(_require_dict(row)) for row in result
        )
        if symbol:
            normalized_symbol = symbol.upper()
            return tuple(
                order for order in orders if order.symbol == normalized_symbol
            )
        return orders

    async def trades(
        self,
        symbol: str,
        *,
        limit: int = 100,
    ) -> tuple[ExchangeTrade, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("Deribit trades limit must be 1..1000")
        result = await self._request(
            "GET",
            "/private/get_user_trades_by_instrument",
            {
                "instrument_name": symbol.upper(),
                "count": limit,
                "sorting": "desc",
            },
            signed=True,
        )
        if not isinstance(result, dict) or not isinstance(
            result.get("trades"),
            list,
        ):
            raise ExchangeInvalidResponseError(
                self.venue,
                200,
                json.dumps(result),
            )
        return tuple(
            _parse_deribit_trade(_require_dict(row))
            for row in result["trades"]
        )

    async def account(self, currency: str | None = None) -> AccountSnapshot:
        if not currency:
            raise ValueError("Deribit account summary requires a currency")
        result = await self._request(
            "GET",
            "/private/get_account_summary",
            {"currency": currency.upper(), "extended": True},
            signed=True,
        )
        if not isinstance(result, dict):
            raise ExchangeInvalidResponseError(
                self.venue,
                200,
                json.dumps(result),
            )
        return AccountSnapshot(
            venue=self.venue,
            currency=currency.upper(),
            timestamp_ms=self._server_time_ms(),
            raw=result,
        )

    async def reconcile(
        self,
        oms: OrderManagementSystem,
        symbol: str,
    ) -> ReconciliationSnapshot:
        currency = await self._reconciliation_currency(symbol)
        exchange_orders, recent_trades, account = await asyncio.gather(
            self.open_orders(symbol),
            self.trades(symbol),
            self.account(currency),
        )
        local_ids = local_open_client_ids(self, oms, symbol)
        exchange_ids = {
            order.client_order_id
            for order in exchange_orders
            if order.client_order_id
        }
        return ReconciliationSnapshot(
            venue=self.venue,
            symbol=symbol.upper(),
            captured_at_ms=self._server_time_ms(),
            exchange_open_orders=exchange_orders,
            recent_trades=recent_trades,
            account=account,
            missing_on_exchange=tuple(sorted(local_ids - exchange_ids)),
            unknown_on_exchange=tuple(sorted(exchange_ids - local_ids)),
        )

    async def _reconciliation_currency(self, symbol: str) -> str:
        """Resolve the currency Deribit indexes ``symbol`` under.

        Linear instruments are grouped by settlement currency (USDC/USDT), so
        deriving the currency from the symbol would query the wrong bucket and
        silently return zero matches during reconciliation.
        """
        rules = await self.instrument_rules(symbol)
        return (
            rules.settlement_currency
            or rules.quote_currency
            or _instrument_currency(symbol)
        )

    async def _orders_by_label(
        self,
        symbol: str,
        currency: str,
        label: str,
    ) -> tuple[ExchangeOrder, ...]:
        result = await self._request(
            "GET",
            "/private/get_open_orders_by_label",
            {"currency": currency, "label": label},
            signed=True,
        )
        if not isinstance(result, list):
            raise ExchangeInvalidResponseError(
                self.venue,
                200,
                json.dumps(result),
            )
        normalized_symbol = symbol.upper()
        orders = tuple(
            _parse_deribit_order(_require_dict(row)) for row in result
        )
        return tuple(
            order
            for order in orders
            if order.venue == self.venue
            and order.symbol == normalized_symbol
            and order.client_order_id == label
        )

    async def _history_matches_by_label(
        self,
        symbol: str,
        currency: str,
        label: str,
    ) -> tuple[ExchangeOrder, ...]:
        normalized_symbol = symbol.upper()
        for historical in (False, True):
            matches: dict[str, ExchangeOrder] = {}
            offset = 0
            previous_page_ids: tuple[str, ...] | None = None
            while True:
                page = await self._order_history_page(
                    currency,
                    offset=offset,
                    historical=historical,
                )
                page_ids = tuple(
                    order.exchange_order_id for order in page
                )
                if page and page_ids == previous_page_ids:
                    raise ExchangeInvalidResponseError(
                        self.venue,
                        200,
                        "Deribit order history pagination repeated a page",
                    )
                previous_page_ids = page_ids
                for order in page:
                    if (
                        order.venue == self.venue
                        and order.symbol == normalized_symbol
                        and order.client_order_id == label
                    ):
                        matches[order.exchange_order_id] = order
                if len(page) < self.config.history_page_size:
                    break
                offset += len(page)
            if matches:
                return tuple(matches.values())
        return ()

    async def _order_history_page(
        self,
        currency: str,
        *,
        offset: int,
        historical: bool,
    ) -> tuple[ExchangeOrder, ...]:
        result = await self._request(
            "GET",
            "/private/get_order_history_by_currency",
            {
                "currency": currency,
                "kind": self.config.instrument_kind,
                "count": self.config.history_page_size,
                "offset": offset,
                "include_old": True,
                "include_unfilled": True,
                "historical": historical,
            },
            signed=True,
        )
        if not isinstance(result, list):
            raise ExchangeInvalidResponseError(
                self.venue,
                200,
                json.dumps(result),
            )
        return tuple(
            _parse_deribit_order(_require_dict(row)) for row in result
        )

    def _validated_order(
        self,
        row: dict[str, Any],
        *,
        symbol: str,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> ExchangeOrder:
        order = _parse_deribit_order(row)
        if (
            order.venue != self.venue
            or order.symbol != symbol.upper()
            or (
                exchange_order_id is not None
                and order.exchange_order_id != exchange_order_id
            )
            or (
                client_order_id is not None
                and order.client_order_id != client_order_id
            )
        ):
            raise ExchangeInvalidResponseError(
                self.venue,
                200,
                json.dumps(row),
            )
        return order

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool,
    ) -> Any:
        method = method.upper()
        if (method, endpoint) not in _DERIBIT_ENDPOINTS:
            raise ExchangeEndpointError(
                "Deribit endpoint is not in the canonical allowlist: "
                f"{method} {endpoint}"
            )
        values = dict(params or {})
        query = urlencode(
            [(key, _parameter_string(value)) for key, value in values.items()]
        )
        uri = f"/api/v2{endpoint}"
        if query:
            uri = f"{uri}?{query}"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if signed:
            credentials = self.credentials or DeribitCredentials.from_environment(
                self.config
            )
            timestamp = self._server_time_ms()
            nonce = self._nonce_factory()
            if not nonce or "," in nonce or any(character.isspace() for character in nonce):
                raise ExchangeAdapterError(
                    "Deribit nonce must be non-empty and contain no commas or whitespace"
                )
            signature = sign_deribit_http_request(
                method,
                uri,
                "",
                timestamp,
                nonce,
                credentials.client_secret,
            )
            headers["Authorization"] = (
                "deri-hmac-sha256 "
                f"id={credentials.client_id},ts={timestamp},"
                f"nonce={nonce},sig={signature}"
            )
        session = await self._get_session()
        url = f"{self.config.rest_base_url}{endpoint}"
        if query:
            url = f"{url}?{query}"
        async with session.request(method, url, headers=headers) as response:
            text = await response.text()
            self.last_request = _deribit_telemetry(
                method,
                endpoint,
                response.status,
                response.headers,
            )
            payload = _load_json(response.status, text)
            self.last_request = _deribit_telemetry(
                method,
                endpoint,
                response.status,
                response.headers,
                payload,
            )
            error = payload.get("error")
            if not 200 <= response.status < 300 or error is not None:
                error_payload = error if isinstance(error, dict) else {}
                raise ExchangeAPIError(
                    venue=self.venue,
                    status=response.status,
                    code=error_payload.get("code"),
                    message=str(
                        error_payload.get("message")
                        or payload.get("message")
                        or text
                    ),
                    payload=payload,
                    retry_after=response.headers.get("Retry-After"),
                )
            if payload.get("jsonrpc") != "2.0" or "result" not in payload:
                raise ExchangeInvalidResponseError(
                    self.venue,
                    response.status,
                    text,
                )
            return payload["result"]

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=self.config.request_timeout_seconds
                )
            )
            self._owns_session = True
        return self._session

    def _server_time_ms(self) -> int:
        return self._clock_ms() + self._time_offset_ms


_TRANSPORT_ERRORS = (
    asyncio.TimeoutError,
    aiohttp.ClientConnectionError,
    aiohttp.ServerDisconnectedError,
)

_RECONCILIATION_ERRORS = (
    ExchangeAPIError,
    ExchangeInvalidResponseError,
    asyncio.TimeoutError,
    aiohttp.ClientConnectionError,
    aiohttp.ServerDisconnectedError,
)


def _validate_base_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        value != DERIBIT_TEST_BASE_URL
        or parsed.scheme != "https"
        or parsed.netloc != "test.deribit.com"
        or parsed.path != "/api/v2"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or parsed.port is not None
    ):
        raise ValueError(
            "DeribitTestAdapter only accepts the exact official test endpoint "
            "https://test.deribit.com/api/v2; live endpoints are forbidden"
        )


def _load_json(status: int, text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ExchangeInvalidResponseError(
            "deribit",
            status,
            text,
        ) from error
    if not isinstance(payload, dict):
        raise ExchangeInvalidResponseError("deribit", status, text)
    return payload


def _parse_deribit_order(row: dict[str, Any]) -> ExchangeOrder:
    try:
        filled_quantity = Decimal(str(row.get("filled_amount") or "0"))
        average_fill_price = (
            _optional_decimal(row.get("average_price"))
            if filled_quantity > 0
            else None
        )
        return ExchangeOrder(
            venue="deribit",
            symbol=str(row["instrument_name"]),
            exchange_order_id=str(row["order_id"]),
            client_order_id=str(row.get("label") or ""),
            status=str(row["order_state"]),
            side=str(row["direction"]),
            order_type=str(row["order_type"]),
            quantity=Decimal(str(row.get("amount") or "0")),
            filled_quantity=filled_quantity,
            price=_optional_decimal(row.get("price")),
            average_fill_price=average_fill_price,
            timestamp_ms=_optional_int(
                row.get("last_update_timestamp")
                or row.get("creation_timestamp")
            ),
            raw=row,
        )
    except (KeyError, ValueError, ArithmeticError) as error:
        raise ExchangeInvalidResponseError(
            "deribit",
            200,
            json.dumps(row),
        ) from error


def _parse_deribit_trade(row: dict[str, Any]) -> ExchangeTrade:
    try:
        fee_currency = (
            str(row.get("fee_currency"))
            if row.get("fee_currency")
            else None
        )
        return ExchangeTrade(
            venue="deribit",
            symbol=str(row["instrument_name"]),
            trade_id=str(row["trade_id"]),
            exchange_order_id=str(row["order_id"]),
            side=str(row["direction"]),
            price=Decimal(str(row["price"])),
            quantity=Decimal(str(row["amount"])),
            fee=_optional_decimal(row.get("fee")),
            fee_currency=fee_currency,
            timestamp_ms=int(row["timestamp"]),
            raw=row,
        )
    except (KeyError, ValueError, ArithmeticError) as error:
        raise ExchangeInvalidResponseError(
            "deribit",
            200,
            json.dumps(row),
        ) from error


def _deribit_tick_size(rules: InstrumentRules, price: Decimal) -> Decimal:
    tick = rules.tick_size
    raw = rules.raw or {}
    steps = raw.get("tick_size_steps")
    if not isinstance(steps, list):
        return tick
    thresholds: list[tuple[Decimal, Decimal]] = []
    for step in steps:
        if not isinstance(step, dict):
            raise ExchangeInvalidResponseError(
                "deribit",
                200,
                json.dumps(raw),
            )
        try:
            thresholds.append(
                (
                    Decimal(str(step["above_price"])),
                    Decimal(str(step["tick_size"])),
                )
            )
        except (KeyError, ValueError, ArithmeticError) as error:
            raise ExchangeInvalidResponseError(
                "deribit",
                200,
                json.dumps(raw),
            ) from error
    for threshold, step_tick in sorted(thresholds):
        if price > threshold:
            tick = step_tick
    return tick


def _deribit_amount_unit(row: dict[str, Any]) -> QuantityUnit:
    kind = str(row.get("kind") or "")
    instrument_type = str(
        row.get("instrument_type") or row.get("future_type") or ""
    )
    if kind == "future":
        # ``instrument_type`` is the only valid discriminator: linear futures
        # (including linear perpetuals such as ETH_USDC-PERPETUAL) express
        # ``amount`` in the base currency, while reversed/inverse contracts
        # express it in USD. ``settlement_period`` must never be used here
        # because linear perpetuals would be misread as USD-denominated.
        if instrument_type == "linear":
            return QuantityUnit.BASE
        if instrument_type == "reversed":
            return QuantityUnit.QUOTE
    elif kind in {"option", "spot"}:
        return QuantityUnit.BASE
    raise ValueError("unsupported or missing Deribit amount-unit metadata")


def _deribit_telemetry(
    method: str,
    endpoint: str,
    status: int,
    headers: Any,
    payload: dict[str, Any] | None = None,
) -> RequestTelemetry:
    payload = payload or {}
    return RequestTelemetry(
        venue="deribit",
        method=method,
        path=endpoint,
        status=status,
        rate_limit_headers={
            str(key): str(value)
            for key, value in headers.items()
            if "rate" in str(key).lower() or "credit" in str(key).lower()
        },
        retry_after=headers.get("Retry-After"),
        exchange_received_us=_optional_int(payload.get("usIn")),
        exchange_sent_us=_optional_int(payload.get("usOut")),
        exchange_elapsed_us=_optional_int(payload.get("usDiff")),
    )


def _instrument_currency(symbol: str) -> str:
    currency = symbol.upper().split("-", 1)[0].split("_", 1)[0]
    if not currency or not currency.isalnum():
        raise ValueError(f"cannot derive Deribit currency from {symbol!r}")
    return currency


def _require_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExchangeInvalidResponseError(
            "deribit",
            200,
            json.dumps(value),
        )
    return value


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, "", "market_price"):
        return None
    return Decimal(str(value))


def _optional_positive_decimal(value: Any) -> Decimal | None:
    result = _optional_decimal(value)
    return result if result is not None and result > 0 else None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _parameter_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
