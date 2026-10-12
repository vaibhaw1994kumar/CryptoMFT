"""Venue testnet execution: Binance Spot Testnet and Bybit v5 testnet.

Transports implement the ``CredentialedTestnetTransport`` contract
``(canonical_request_dict, secret) -> native events``. The secret is injected
per request by ``CredentialBroker`` and is never stored; strategy code only
ever sees canonical callbacks through ``TestnetVenueAdapter``.

Safety properties:

* only the two public testnet hosts are accepted (no production endpoint can
  be configured by mistake);
* TLS verification uses the default trust store and cannot be disabled here;
* a transport/network failure raises, which the adapter turns into "no
  answer" -> the OMS queries the order later and never resends it;
* Bybit's create response carries no fill, so fills come from order queries.

These transports are verified against recorded/fake HTTP responses in the
test suite only; running them against the real testnets requires testnet
API keys registered in the credential broker.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import hmac
import json
import ssl
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .credentials import CredentialBroker, CredentialedTestnetTransport
from .sdk import TestnetVenueAdapter


BINANCE_SPOT_TESTNET_URL = "https://testnet.binance.vision"
BYBIT_TESTNET_URL = "https://api-testnet.bybit.com"
TESTNET_HOSTS = frozenset({"testnet.binance.vision", "api-testnet.bybit.com"})

HttpCall = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, str]]


def urllib_http(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> tuple[int, str]:
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except URLError as exc:
        raise ConnectionError(str(exc.reason)) from exc


def split_secret(secret: memoryview) -> tuple[str, bytes]:
    """Secret material is ``api_key:api_secret`` (ASCII)."""
    raw = bytes(secret)
    key, separator, value = raw.partition(b":")
    if not separator or not key or not value:
        raise ValueError("testnet secret must be formatted as api_key:api_secret")
    return key.decode("ascii"), value


def decimal_text(value: Any) -> str:
    text = format(Decimal(str(value)).normalize(), "f")
    return text if text != "-0" else "0"


def map_order_state(
    kind: str, executed: Decimal, average: Decimal, common: dict[str, Any], *,
    submit: bool,
) -> tuple[dict[str, Any], ...]:
    """Translate a normalized venue state into canonical native events.

    ``submit=True`` yields the full lifecycle the OMS expects after a submit
    (ACK, fills, explicit remainder cancel; IOC expiry with no fill is a
    reject). ``submit=False`` yields the single current state for a query.
    """
    fill = {
        "cumulative_filled_quantity": decimal_text(executed),
        "last_fill_quantity": decimal_text(executed),
        "average_price": decimal_text(average),
    }
    ack = {"event_type": "ACKNOWLEDGED", **common}
    if kind == "NEW":
        return (ack,)
    if kind == "PARTIAL":
        event = {"event_type": "PARTIAL", **fill, **common}
        return (ack, event) if submit else (event,)
    if kind == "FILLED":
        event = {"event_type": "FILLED", **fill, **common}
        return (ack, event) if submit else (event,)
    if kind == "CANCELLED":
        if executed > 0:
            cancelled = {
                "event_type": "CANCELLED", **fill, **common,
                "reason": "remainder cancelled by venue",
            }
            if submit:
                return (ack, {"event_type": "PARTIAL", **fill, **common}, cancelled)
            return (cancelled,)
        if submit:
            return ({"event_type": "REJECTED", "reason": "expired without fill", **common},)
        return ({"event_type": "CANCELLED", **fill, **common},)
    if kind == "REJECTED":
        return ({
            "event_type": "REJECTED",
            "reason": str(common.get("native_status") or "rejected by venue"),
            **common,
        },)
    raise ConnectionError(f"unmapped venue order state {kind!r}")


class VenueTestnetTransport:
    venue = ""

    def __init__(
        self, *, base_url: str, symbols: dict[str, str], http: HttpCall = urllib_http,
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        recv_window_ms: int = 5_000, timeout_seconds: float = 10.0,
        settle_queries: int = 5, settle_interval_seconds: float = 0.2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname not in TESTNET_HOSTS:
            raise ValueError(f"refusing non-testnet endpoint {base_url!r}")
        self.base_url = base_url.rstrip("/")
        self.symbols = dict(symbols)
        self.http = http
        self.clock_ms = clock_ms
        self.recv_window_ms = recv_window_ms
        self.timeout_seconds = timeout_seconds
        self.settle_queries = settle_queries
        self.settle_interval_seconds = settle_interval_seconds
        self.sleep = sleep
        self.requests_sent = 0

    def __call__(
        self, request: dict[str, Any], secret: memoryview
    ) -> tuple[dict[str, Any], ...]:
        key, secret_bytes = split_secret(secret)
        try:
            symbol = self.symbols[str(request["instrument_id"])]
        except KeyError as exc:
            raise ValueError(f"no {self.venue} symbol for {request['instrument_id']}") from exc
        action = str(request["action"])
        if action in {"SUBMIT", "REPLACE"}:
            return self.submit(request, symbol, key, secret_bytes)
        if action == "CANCEL":
            return self.cancel(request, symbol, key, secret_bytes)
        if action == "QUERY":
            return self.query(request, symbol, key, secret_bytes)
        raise ValueError(f"unsupported testnet action {action}")

    def submit(self, request, symbol, key, secret):  # pragma: no cover - abstract
        raise NotImplementedError

    def cancel(self, request, symbol, key, secret):  # pragma: no cover - abstract
        raise NotImplementedError

    def query(self, request, symbol, key, secret):  # pragma: no cover - abstract
        raise NotImplementedError


class BinanceSpotTestnetTransport(VenueTestnetTransport):
    venue = "BINANCE"
    STATUS = {
        "NEW": "NEW", "PARTIALLY_FILLED": "PARTIAL", "FILLED": "FILLED",
        "CANCELED": "CANCELLED", "PENDING_CANCEL": "CANCELLED",
        "EXPIRED": "CANCELLED", "EXPIRED_IN_MATCH": "CANCELLED",
        "REJECTED": "REJECTED",
    }

    def __init__(self, *, base_url: str = BINANCE_SPOT_TESTNET_URL, **kwargs: Any) -> None:
        super().__init__(base_url=base_url, **kwargs)

    def _signed(
        self, method: str, path: str, params: dict[str, Any], key: str, secret: bytes
    ) -> tuple[int, dict[str, Any]]:
        query = urlencode({
            **params, "recvWindow": self.recv_window_ms, "timestamp": self.clock_ms(),
        })
        signature = hmac.new(secret, query.encode(), hashlib.sha256).hexdigest()
        status, text = self.http(
            method, f"{self.base_url}{path}?{query}&signature={signature}",
            {"X-MBX-APIKEY": key}, None, self.timeout_seconds,
        )
        self.requests_sent += 1
        if status >= 500:
            raise ConnectionError(f"binance testnet HTTP {status}")
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError as exc:
            raise ConnectionError("binance testnet returned non-JSON") from exc
        return status, payload

    def _events(self, payload: dict[str, Any], *, submit: bool) -> tuple[dict[str, Any], ...]:
        native_status = str(payload.get("status", ""))
        executed = Decimal(str(payload.get("executedQty", "0")))
        quote = Decimal(str(payload.get("cummulativeQuoteQty", "0")))
        average = quote / executed if executed else Decimal("0")
        common: dict[str, Any] = {
            "venue_order_id": str(payload.get("orderId", "")),
            "native_status": native_status,
        }
        venue_ms = payload.get("transactTime") or payload.get("updateTime")
        if venue_ms:
            common["event_ts_ns"] = int(venue_ms) * 1_000_000
        return map_order_state(
            self.STATUS.get(native_status, native_status), executed, average, common,
            submit=submit,
        )

    def submit(self, request, symbol, key, secret):
        params: dict[str, Any] = {
            "symbol": symbol, "side": request["side"],
            "quantity": decimal_text(request["quantity"]),
            "newClientOrderId": request["order_id"], "newOrderRespType": "FULL",
        }
        if request["order_type"] == "MARKET":
            params["type"] = "MARKET"
        elif request.get("post_only"):
            params.update(type="LIMIT_MAKER", price=decimal_text(request["price"]))
        else:
            params.update(
                type="LIMIT", timeInForce=request["time_in_force"],
                price=decimal_text(request["price"]),
            )
        status, payload = self._signed("POST", "/api/v3/order", params, key, secret)
        if status != 200:
            return ({
                "event_type": "REJECTED",
                "reason": f"binance {payload.get('code')}: {payload.get('msg')}",
            },)
        return self._events(payload, submit=True)

    def query(self, request, symbol, key, secret):
        status, payload = self._signed("GET", "/api/v3/order", {
            "symbol": symbol, "origClientOrderId": request["order_id"],
        }, key, secret)
        if status == 400 and payload.get("code") == -2013:
            return ({"event_type": None, "reason": "order does not exist"},)
        if status != 200:
            raise ConnectionError(f"binance order query failed: {payload.get('msg')}")
        return self._events(payload, submit=False)

    def cancel(self, request, symbol, key, secret):
        status, payload = self._signed("DELETE", "/api/v3/order", {
            "symbol": symbol,
            "origClientOrderId": request.get("target_order_id") or request["order_id"],
        }, key, secret)
        if status == 400 and payload.get("code") in {-2011, -2013}:
            return ({"event_type": "REJECTED", "reason": "cancel target is not open"},)
        if status != 200:
            raise ConnectionError(f"binance cancel failed: {payload.get('msg')}")
        return self._events(payload, submit=False)


class BybitV5TestnetTransport(VenueTestnetTransport):
    venue = "BYBIT"
    STATUS = {
        "New": "NEW", "Created": "NEW", "Untriggered": "NEW",
        "PartiallyFilled": "PARTIAL", "Filled": "FILLED",
        "Cancelled": "CANCELLED", "PartiallyFilledCanceled": "CANCELLED",
        "Deactivated": "CANCELLED", "Rejected": "REJECTED",
    }

    def __init__(
        self, *, base_url: str = BYBIT_TESTNET_URL, category: str = "spot", **kwargs: Any
    ) -> None:
        super().__init__(base_url=base_url, **kwargs)
        self.category = category

    def _signed(
        self, method: str, path: str, key: str, secret: bytes, *,
        params: dict[str, Any] | None = None, body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        timestamp = str(self.clock_ms())
        recv_window = str(self.recv_window_ms)
        if method == "GET":
            payload_text = urlencode(params or {})
            url = f"{self.base_url}{path}?{payload_text}"
            data = None
        else:
            payload_text = json.dumps(body or {}, separators=(",", ":"))
            url = f"{self.base_url}{path}"
            data = payload_text.encode()
        signature = hmac.new(
            secret, (timestamp + key + recv_window + payload_text).encode(),
            hashlib.sha256,
        ).hexdigest()
        status, text = self.http(method, url, {
            "X-BAPI-API-KEY": key, "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window, "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2", "Content-Type": "application/json",
        }, data, self.timeout_seconds)
        self.requests_sent += 1
        if status >= 500 or status == 0:
            raise ConnectionError(f"bybit testnet HTTP {status}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConnectionError("bybit testnet returned non-JSON") from exc

    def _state(self, order_link_id: str, symbol: str, key: str, secret: bytes):
        for path in ("/v5/order/realtime", "/v5/order/history"):
            payload = self._signed("GET", path, key, secret, params={
                "category": self.category, "symbol": symbol,
                "orderLinkId": order_link_id,
            })
            if payload.get("retCode") != 0:
                raise ConnectionError(f"bybit order query failed: {payload.get('retMsg')}")
            rows = payload.get("result", {}).get("list", [])
            if rows:
                row = rows[0]
                executed = Decimal(str(row.get("cumExecQty") or "0"))
                average = Decimal(str(row.get("avgPrice") or "0"))
                native_status = str(row.get("orderStatus", ""))
                common: dict[str, Any] = {
                    "venue_order_id": str(row.get("orderId", "")),
                    "native_status": native_status,
                }
                if row.get("updatedTime"):
                    common["event_ts_ns"] = int(row["updatedTime"]) * 1_000_000
                return self.STATUS.get(native_status, native_status), executed, average, common
        return None

    def submit(self, request, symbol, key, secret):
        body: dict[str, Any] = {
            "category": self.category, "symbol": symbol,
            "side": "Buy" if request["side"] == "BUY" else "Sell",
            "orderType": "Market" if request["order_type"] == "MARKET" else "Limit",
            "qty": decimal_text(request["quantity"]),
            "orderLinkId": request["order_id"],
        }
        if request["order_type"] != "MARKET":
            body["price"] = decimal_text(request["price"])
            body["timeInForce"] = "PostOnly" if request.get("post_only") else request["time_in_force"]
        payload = self._signed("POST", "/v5/order/create", key, secret, body=body)
        if payload.get("retCode") != 0:
            return ({
                "event_type": "REJECTED",
                "reason": f"bybit {payload.get('retCode')}: {payload.get('retMsg')}",
            },)
        venue_order_id = str(payload.get("result", {}).get("orderId", ""))
        events: list[dict[str, Any]] = [
            {"event_type": "ACKNOWLEDGED", "venue_order_id": venue_order_id}
        ]
        resting = request["time_in_force"] == "GTC"
        for attempt in range(self.settle_queries):
            state = self._state(request["order_id"], symbol, key, secret)
            if state is not None:
                kind, executed, average, common = state
                terminal = kind in {"FILLED", "CANCELLED", "REJECTED"}
                if terminal or (resting and kind in {"NEW", "PARTIAL"}):
                    mapped = map_order_state(kind, executed, average, common, submit=True)
                    events.extend(item for item in mapped if item["event_type"] != "ACKNOWLEDGED")
                    return tuple(events)
            if attempt + 1 < self.settle_queries:
                self.sleep(self.settle_interval_seconds)
        return tuple(events)

    def query(self, request, symbol, key, secret):
        state = self._state(request["order_id"], symbol, key, secret)
        if state is None:
            # Bybit order visibility is eventually consistent: absence right
            # after a submit is not proof the venue never received the order.
            raise ConnectionError("order not visible at venue yet")
        kind, executed, average, common = state
        return map_order_state(kind, executed, average, common, submit=False)

    def cancel(self, request, symbol, key, secret):
        target = request.get("target_order_id") or request["order_id"]
        payload = self._signed("POST", "/v5/order/cancel", key, secret, body={
            "category": self.category, "symbol": symbol, "orderLinkId": target,
        })
        if payload.get("retCode") == 110001:
            return ({"event_type": "REJECTED", "reason": "cancel target is not open"},)
        if payload.get("retCode") != 0:
            raise ConnectionError(f"bybit cancel failed: {payload.get('retMsg')}")
        state = self._state(target, symbol, key, secret)
        if state is None:
            return ({"event_type": "CANCELLED"},)
        kind, executed, average, common = state
        return map_order_state(kind, executed, average, common, submit=False)


TRANSPORTS = {"BINANCE": BinanceSpotTestnetTransport, "BYBIT": BybitV5TestnetTransport}


def build_testnet_adapters(
    broker: CredentialBroker,
    credential_ids: dict[str, str],
    symbols: dict[str, dict[str, str]],
    *,
    http: HttpCall = urllib_http,
    clock_ns: Callable[[], int] = time.time_ns,
) -> dict[str, TestnetVenueAdapter]:
    """One credentialed testnet adapter per venue (fails closed on gaps)."""
    adapters = {}
    for venue, credential_id in sorted(credential_ids.items()):
        transport_cls = TRANSPORTS.get(venue)
        if transport_cls is None:
            raise ValueError(f"no testnet transport for venue {venue}")
        transport = transport_cls(symbols=symbols[venue], http=http)
        adapters[venue] = TestnetVenueAdapter(
            venue, CredentialedTestnetTransport(broker, credential_id, venue, transport),
            clock_ns=clock_ns,
        )
    return adapters
