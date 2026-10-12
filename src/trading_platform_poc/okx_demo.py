from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
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
    ReconciliationSnapshot,
    RequestTelemetry,
    decimal_string,
    deterministic_client_id,
    local_open_client_ids,
    round_price,
    round_quantity,
)
from trading_platform_poc.models import Order, OrderType
from trading_platform_poc.oms import OrderManagementSystem


OKX_DEMO_BASE_URLS = frozenset(
    {
        "https://us.okx.com",
    }
)

_OKX_ENDPOINTS = frozenset(
    {
        ("GET", "/api/v5/public/time"),
        ("GET", "/api/v5/public/instruments"),
        ("POST", "/api/v5/trade/order"),
        ("POST", "/api/v5/trade/cancel-order"),
        ("GET", "/api/v5/trade/order"),
        ("GET", "/api/v5/trade/orders-pending"),
        ("GET", "/api/v5/trade/fills"),
        ("GET", "/api/v5/account/balance"),
    }
)


@dataclass(frozen=True, slots=True)
class OKXDemoConfig:
    rest_base_url: str = "https://us.okx.com"
    api_key_environment_variable: str = "OKX_DEMO_API_KEY"
    api_secret_environment_variable: str = "OKX_DEMO_API_SECRET"
    passphrase_environment_variable: str = "OKX_DEMO_PASSPHRASE"
    request_timeout_seconds: float = 10.0
    transaction_expiry_ms: int = 5_000
    cancel_reconciliation_attempts: int = 3
    cancel_reconciliation_interval_seconds: float = 0.1
    instrument_type: str = "SPOT"
    trade_mode: str = "cash"

    def __post_init__(self) -> None:
        _validate_base_url(self.rest_base_url)
        if self.instrument_type != "SPOT" or self.trade_mode != "cash":
            raise ValueError(
                "OKXDemoAdapter currently supports guarded SPOT cash orders only"
            )
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


@dataclass(frozen=True, slots=True)
class OKXCredentials:
    api_key: str
    api_secret: str
    passphrase: str

    @classmethod
    def from_environment(cls, config: OKXDemoConfig) -> OKXCredentials:
        values = (
            os.environ.get(config.api_key_environment_variable, ""),
            os.environ.get(config.api_secret_environment_variable, ""),
            os.environ.get(config.passphrase_environment_variable, ""),
        )
        if not all(values):
            raise ExchangeCredentialsError(
                "OKX Demo credentials are missing. Set "
                f"{config.api_key_environment_variable}, "
                f"{config.api_secret_environment_variable}, and "
                f"{config.passphrase_environment_variable}."
            )
        return cls(*values)


def sign_okx_request(
    timestamp: str,
    method: str,
    request_path: str,
    body: str,
    secret: str,
) -> str:
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(
        secret.encode(),
        prehash.encode(),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode()


class OKXDemoAdapter:
    """Guarded OKX V5 REST adapter that always sends simulated-trading mode."""

    venue = "okx"

    def __init__(
        self,
        config: OKXDemoConfig | None = None,
        credentials: OKXCredentials | None = None,
        *,
        session: aiohttp.ClientSession | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.config = config or OKXDemoConfig()
        _validate_base_url(self.config.rest_base_url)
        self.credentials = credentials
        self._session = session
        self._owns_session = False
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._time_offset_ms = 0
        self._rules: dict[str, InstrumentRules] = {}
        self.last_request: RequestTelemetry | None = None

    async def __aenter__(self) -> OKXDemoAdapter:
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
            "okx",
            order.client_order_id,
            maximum_length=32,
            alphanumeric_only=True,
        )

    async def sync_time(self) -> int:
        payload = await self._request(
            "GET",
            "/api/v5/public/time",
            signed=False,
        )
        rows = _require_list(payload, "data", self.venue)
        if len(rows) != 1 or not isinstance(rows[0], dict) or "ts" not in rows[0]:
            raise ExchangeInvalidResponseError(
                self.venue,
                200,
                json.dumps(payload),
            )
        server_ms = int(rows[0]["ts"])
        self._time_offset_ms = server_ms - self._clock_ms()
        return self._time_offset_ms

    async def instrument_rules(self, symbol: str) -> InstrumentRules:
        normalized_symbol = symbol.upper()
        cached = self._rules.get(normalized_symbol)
        if cached is not None:
            return cached
        payload = await self._request(
            "GET",
            "/api/v5/public/instruments",
            {
                "instType": self.config.instrument_type,
                "instId": normalized_symbol,
            },
            signed=False,
        )
        rows = _require_list(payload, "data", self.venue)
        matching = [
            row
            for row in rows
            if isinstance(row, dict) and row.get("instId") == normalized_symbol
        ]
        if len(matching) != 1:
            raise ExchangeAdapterError(
                f"OKX instrument metadata missing for {normalized_symbol}"
            )
        row = matching[0]
        if row.get("state") not in {"live", None, ""}:
            raise ExchangeAdapterError(
                f"OKX instrument {normalized_symbol} is not live"
            )
        try:
            rules = InstrumentRules(
                venue=self.venue,
                symbol=normalized_symbol,
                tick_size=Decimal(str(row["tickSz"])),
                quantity_step=Decimal(str(row["lotSz"])),
                minimum_quantity=Decimal(str(row["minSz"])),
                maximum_limit_quantity=_optional_positive_decimal(
                    row.get("maxLmtSz")
                ),
                maximum_market_quantity=_optional_positive_decimal(
                    row.get("maxMktSz")
                ),
                base_currency=str(row.get("baseCcy") or "") or None,
                quote_currency=str(row.get("quoteCcy") or "") or None,
                contract_size=_optional_positive_decimal(row.get("ctVal")),
                raw=row,
            )
        except (KeyError, ValueError, ArithmeticError) as error:
            raise ExchangeInvalidResponseError(
                self.venue,
                200,
                json.dumps(payload),
            ) from error
        if (
            rules.tick_size <= 0
            or rules.quantity_step <= 0
            or rules.minimum_quantity <= 0
        ):
            raise ExchangeInvalidResponseError(
                self.venue,
                200,
                json.dumps(payload),
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
        maximum = (
            rules.maximum_market_quantity
            if order.order_type is OrderType.MARKET
            else rules.maximum_limit_quantity
        )
        if quantity < rules.minimum_quantity:
            raise ExchangeAdapterError(
                f"OKX quantity {quantity} is below minimum "
                f"{rules.minimum_quantity}"
            )
        if maximum is not None and quantity > maximum:
            raise ExchangeAdapterError(
                f"OKX quantity {quantity} exceeds maximum {maximum}"
            )
        price: Decimal | None = None
        if order.order_type is not OrderType.MARKET:
            if order.price is None:
                raise ExchangeAdapterError(
                    "OKX limit and post-only orders require a price"
                )
            price = round_price(order.price, rules.tick_size, order.side)
        return NormalizedOrder(
            quantity=quantity,
            price=price,
            client_order_id=self.client_order_id(order),
        )

    async def submit_order(self, order: Order) -> ExchangeOrder:
        normalized = await self.normalize_order(order)
        body: dict[str, Any] = {
            "instId": order.symbol.upper(),
            "tdMode": self.config.trade_mode,
            "clOrdId": normalized.client_order_id,
            "side": order.side.value,
            "ordType": order.order_type.value,
            "sz": decimal_string(normalized.quantity),
        }
        if normalized.price is not None:
            body["px"] = decimal_string(normalized.price)
        if order.order_type is OrderType.MARKET:
            body["tgtCcy"] = "base_ccy"
            body["banAmend"] = True
        try:
            payload = await self._request(
                "POST",
                "/api/v5/trade/order",
                body,
                signed=True,
                transaction=True,
            )
            row = _single_okx_result(payload, self.venue)
            return _okx_acknowledgement(
                order,
                normalized,
                row,
                status="accepted",
            )
        except ExchangeAPIError as error:
            if not _is_ambiguous_order_api_error(error):
                raise
        except ExchangeInvalidResponseError:
            pass
        except _TRANSPORT_ERRORS:
            pass
        return await self._resolve_ambiguous_submission(
            order,
            normalized.client_order_id,
        )

    async def _resolve_ambiguous_submission(
        self,
        order: Order,
        client_order_id: str,
    ) -> ExchangeOrder:
        try:
            return await self.query_order(
                order.symbol,
                client_order_id=client_order_id,
            )
        except (
            ExchangeAPIError,
            ExchangeInvalidResponseError,
            asyncio.TimeoutError,
            aiohttp.ClientConnectionError,
            aiohttp.ServerDisconnectedError,
        ) as error:
            raise AmbiguousOrderError(self.venue, client_order_id) from error

    async def cancel_order(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> ExchangeOrder:
        body = _okx_lookup(symbol, exchange_order_id, client_order_id)
        try:
            payload = await self._request(
                "POST",
                "/api/v5/trade/cancel-order",
                body,
                signed=True,
                transaction=True,
            )
            row = _single_okx_result(payload, self.venue)
            return ExchangeOrder(
                venue=self.venue,
                symbol=symbol.upper(),
                exchange_order_id=str(row.get("ordId") or exchange_order_id or ""),
                client_order_id=str(
                    row.get("clOrdId") or client_order_id or ""
                ),
                status="cancel_requested",
                side="",
                order_type="",
                quantity=Decimal(0),
                filled_quantity=Decimal(0),
                price=None,
                average_fill_price=None,
                timestamp_ms=_optional_int(row.get("ts")),
                raw=row,
            )
        except ExchangeAPIError as error:
            if not _is_ambiguous_order_api_error(error):
                raise
        except ExchangeInvalidResponseError:
            pass
        except _TRANSPORT_ERRORS:
            pass
        return await self._resolve_ambiguous_cancellation(
            symbol,
            exchange_order_id=exchange_order_id,
            client_order_id=client_order_id,
        )

    async def _resolve_ambiguous_cancellation(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None,
        client_order_id: str | None,
    ) -> ExchangeOrder:
        last_error: BaseException | None = None
        for attempt in range(self.config.cancel_reconciliation_attempts):
            try:
                result = await self.query_order(
                    symbol,
                    exchange_order_id=exchange_order_id,
                    client_order_id=client_order_id,
                )
            except _RECONCILIATION_ERRORS as error:
                last_error = error
            else:
                if result.status in {"canceled", "filled", "mmp_canceled"}:
                    return result
                last_error = ExchangeAdapterError(
                    "OKX cancellation remains non-terminal with state "
                    f"{result.status}"
                )
            if (
                attempt + 1 < self.config.cancel_reconciliation_attempts
                and self.config.cancel_reconciliation_interval_seconds
            ):
                await asyncio.sleep(
                    self.config.cancel_reconciliation_interval_seconds
                )
        identifier = client_order_id or exchange_order_id or ""
        raise AmbiguousOrderError(self.venue, identifier) from last_error

    async def query_order(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> ExchangeOrder:
        payload = await self._request(
            "GET",
            "/api/v5/trade/order",
            _okx_lookup(symbol, exchange_order_id, client_order_id),
            signed=True,
        )
        rows = _require_list(payload, "data", self.venue)
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise ExchangeInvalidResponseError(
                self.venue,
                200,
                json.dumps(payload),
            )
        return _parse_okx_order(rows[0])

    async def open_orders(
        self,
        symbol: str | None = None,
    ) -> tuple[ExchangeOrder, ...]:
        params: dict[str, Any] = {"instType": self.config.instrument_type}
        if symbol:
            params["instId"] = symbol.upper()
        payload = await self._request(
            "GET",
            "/api/v5/trade/orders-pending",
            params,
            signed=True,
        )
        rows = _require_list(payload, "data", self.venue)
        return tuple(_parse_okx_order(_require_dict(row, self.venue)) for row in rows)

    async def trades(
        self,
        symbol: str,
        *,
        limit: int = 100,
    ) -> tuple[ExchangeTrade, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("OKX fills limit must be 1..100")
        payload = await self._request(
            "GET",
            "/api/v5/trade/fills",
            {
                "instType": self.config.instrument_type,
                "instId": symbol.upper(),
                "limit": limit,
            },
            signed=True,
        )
        rows = _require_list(payload, "data", self.venue)
        return tuple(_parse_okx_trade(_require_dict(row, self.venue)) for row in rows)

    async def account(self, currency: str | None = None) -> AccountSnapshot:
        params = {"ccy": currency.upper()} if currency else {}
        payload = await self._request(
            "GET",
            "/api/v5/account/balance",
            params,
            signed=True,
        )
        rows = _require_list(payload, "data", self.venue)
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise ExchangeInvalidResponseError(
                self.venue,
                200,
                json.dumps(payload),
            )
        row = rows[0]
        return AccountSnapshot(
            venue=self.venue,
            currency=currency.upper() if currency else None,
            timestamp_ms=_optional_int(row.get("uTime")),
            raw=row,
        )

    async def reconcile(
        self,
        oms: OrderManagementSystem,
        symbol: str,
    ) -> ReconciliationSnapshot:
        rules = await self.instrument_rules(symbol)
        currency = rules.quote_currency
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

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool,
        transaction: bool = False,
    ) -> dict[str, Any]:
        method = method.upper()
        if (method, path) not in _OKX_ENDPOINTS:
            raise ExchangeEndpointError(
                f"OKX endpoint is not in the canonical allowlist: {method} {path}"
            )
        session = await self._get_session()
        values = dict(params or {})
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-simulated-trading": "1",
        }
        body = ""
        request_path = path
        kwargs: dict[str, Any] = {}
        if method == "GET":
            query = urlencode(
                [(key, _parameter_string(value)) for key, value in values.items()]
            )
            if query:
                request_path = f"{path}?{query}"
        else:
            body = json.dumps(values, separators=(",", ":"), ensure_ascii=True)
            kwargs["data"] = body
        if signed:
            credentials = self.credentials or OKXCredentials.from_environment(
                self.config
            )
            timestamp = self._iso_timestamp()
            headers.update(
                {
                    "OK-ACCESS-KEY": credentials.api_key,
                    "OK-ACCESS-SIGN": sign_okx_request(
                        timestamp,
                        method,
                        request_path,
                        body,
                        credentials.api_secret,
                    ),
                    "OK-ACCESS-TIMESTAMP": timestamp,
                    "OK-ACCESS-PASSPHRASE": credentials.passphrase,
                }
            )
        if transaction:
            headers["expTime"] = str(
                self._server_time_ms() + self.config.transaction_expiry_ms
            )
        url = f"{self.config.rest_base_url}{request_path}"
        async with session.request(method, url, headers=headers, **kwargs) as response:
            text = await response.text()
            self.last_request = _okx_telemetry(
                method,
                path,
                response.status,
                response.headers,
            )
            payload = _load_json(self.venue, response.status, text)
            self.last_request = _okx_telemetry(
                method,
                path,
                response.status,
                response.headers,
                payload,
            )
            code = payload.get("code")
            if not 200 <= response.status < 300 or code != "0":
                raise ExchangeAPIError(
                    venue=self.venue,
                    status=response.status,
                    code=code,
                    message=str(payload.get("msg") or text),
                    payload=payload,
                    retry_after=response.headers.get("Retry-After"),
                )
            if "data" not in payload:
                raise ExchangeInvalidResponseError(
                    self.venue,
                    response.status,
                    text,
                )
            return payload

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

    def _iso_timestamp(self) -> str:
        timestamp = datetime.fromtimestamp(
            self._server_time_ms() / 1000,
            tz=UTC,
        )
        return timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


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


def _is_ambiguous_order_api_error(error: ExchangeAPIError) -> bool:
    return error.status >= 500 or str(error.code) == "50004"


def _validate_base_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        value not in OKX_DEMO_BASE_URLS
        or parsed.scheme != "https"
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or parsed.port is not None
    ):
        raise ValueError(
            "this US-targeted OKX Demo adapter requires the exact official "
            "https://us.okx.com origin; regional origins are not interchangeable "
            "and x-simulated-trading is mandatory"
        )


def _load_json(venue: str, status: int, text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ExchangeInvalidResponseError(venue, status, text) from error
    if not isinstance(payload, dict):
        raise ExchangeInvalidResponseError(venue, status, text)
    return payload


def _single_okx_result(payload: dict[str, Any], venue: str) -> dict[str, Any]:
    rows = _require_list(payload, "data", venue)
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise ExchangeInvalidResponseError(venue, 200, json.dumps(payload))
    row = rows[0]
    if str(row.get("sCode", "0")) != "0":
        raise ExchangeAPIError(
            venue=venue,
            status=200,
            code=str(row.get("sCode")),
            message=str(row.get("sMsg") or "request rejected"),
            payload=payload,
        )
    if not row.get("ordId") and not row.get("clOrdId"):
        raise ExchangeInvalidResponseError(venue, 200, json.dumps(payload))
    return row


def _okx_acknowledgement(
    order: Order,
    normalized: NormalizedOrder,
    row: dict[str, Any],
    *,
    status: str,
) -> ExchangeOrder:
    return ExchangeOrder(
        venue="okx",
        symbol=order.symbol.upper(),
        exchange_order_id=str(row.get("ordId") or ""),
        client_order_id=str(row.get("clOrdId") or normalized.client_order_id),
        status=status,
        side=order.side.value,
        order_type=order.order_type.value,
        quantity=normalized.quantity,
        filled_quantity=Decimal(0),
        price=normalized.price,
        average_fill_price=None,
        timestamp_ms=_optional_int(row.get("ts")),
        raw=row,
    )


def _parse_okx_order(row: dict[str, Any]) -> ExchangeOrder:
    try:
        quantity = Decimal(str(row["sz"]))
        filled = Decimal(str(row.get("accFillSz") or "0"))
        price = _optional_decimal(row.get("px"))
        average = _optional_decimal(row.get("avgPx"))
        return ExchangeOrder(
            venue="okx",
            symbol=str(row["instId"]),
            exchange_order_id=str(row["ordId"]),
            client_order_id=str(row.get("clOrdId") or ""),
            status=str(row["state"]),
            side=str(row["side"]),
            order_type=str(row["ordType"]),
            quantity=quantity,
            filled_quantity=filled,
            price=price,
            average_fill_price=average,
            timestamp_ms=_optional_int(row.get("uTime") or row.get("cTime")),
            raw=row,
        )
    except (KeyError, ValueError, ArithmeticError) as error:
        raise ExchangeInvalidResponseError("okx", 200, json.dumps(row)) from error


def _parse_okx_trade(row: dict[str, Any]) -> ExchangeTrade:
    try:
        return ExchangeTrade(
            venue="okx",
            symbol=str(row["instId"]),
            trade_id=str(row["tradeId"]),
            exchange_order_id=str(row["ordId"]),
            side=str(row["side"]),
            price=Decimal(str(row["fillPx"])),
            quantity=Decimal(str(row["fillSz"])),
            fee=_optional_decimal(row.get("fee")),
            fee_currency=str(row.get("feeCcy") or "") or None,
            timestamp_ms=int(row["ts"]),
            raw=row,
        )
    except (KeyError, ValueError, ArithmeticError) as error:
        raise ExchangeInvalidResponseError("okx", 200, json.dumps(row)) from error


def _okx_lookup(
    symbol: str,
    exchange_order_id: str | None,
    client_order_id: str | None,
) -> dict[str, str]:
    if bool(exchange_order_id) == bool(client_order_id):
        raise ValueError(
            "provide exactly one of exchange_order_id or client_order_id"
        )
    result = {"instId": symbol.upper()}
    if exchange_order_id:
        result["ordId"] = exchange_order_id
    else:
        result["clOrdId"] = str(client_order_id)
    return result


def _okx_telemetry(
    method: str,
    path: str,
    status: int,
    headers: Any,
    payload: dict[str, Any] | None = None,
) -> RequestTelemetry:
    payload = payload or {}
    rate_headers = {
        str(key): str(value)
        for key, value in headers.items()
        if str(key).lower().startswith("x-ratelimit")
    }
    return RequestTelemetry(
        venue="okx",
        method=method,
        path=path,
        status=status,
        rate_limit_headers=rate_headers,
        retry_after=headers.get("Retry-After"),
        exchange_received_us=_optional_int(payload.get("inTime")),
        exchange_sent_us=_optional_int(payload.get("outTime")),
    )


def _require_list(
    payload: dict[str, Any],
    key: str,
    venue: str,
) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ExchangeInvalidResponseError(venue, 200, json.dumps(payload))
    return value


def _require_dict(value: Any, venue: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExchangeInvalidResponseError(venue, 200, json.dumps(value))
    return value


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
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
