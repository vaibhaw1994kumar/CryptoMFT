"""Venue testnet execution: Binance Spot / USDⓈ-M Futures testnets and Bybit v5 testnet.

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
* perpetuals (Binance USDⓈ-M futures testnet, Bybit ``category=linear``):
  reduce-only is sent, post-only is ``GTX`` / ``PostOnly``, maker/taker and
  the venue commission of each fill come from the venue's trade/execution
  lists (``/fapi/v1/userTrades``, ``/v5/execution/list``), and
  ``verify_account`` refuses to trade unless the account is in one-way
  position mode (and, on Bybit, in the expected margin mode). Account
  settings are only read, never changed.

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
BINANCE_FUTURES_TESTNET_URL = "https://testnet.binancefuture.com"
BYBIT_TESTNET_URL = "https://api-testnet.bybit.com"
TESTNET_HOSTS = frozenset({"testnet.binance.vision", "testnet.binancefuture.com", "api-testnet.bybit.com"})

HttpCall = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, str]]


def verified_ssl_context() -> ssl.SSLContext:
    """Certificate verification always on. A Python whose default CA store is
    empty (e.g. a python.org macOS build before "Install Certificates") uses
    the certifi bundle instead of failing; verification is never disabled."""
    context = ssl.create_default_context()
    if not context.cert_store_stats().get("x509_ca"):
        try:
            import certifi
        except ImportError:
            return context
        context.load_verify_locations(cafile=certifi.where())
    return context


def urllib_http(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> tuple[int, str]:
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout, context=verified_ssl_context()) as response:
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
        if action == "VERIFY_ACCOUNT":
            return self.verify_account(request, symbol, key, secret_bytes)
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

    def verify_account(self, request, symbol, key, secret) -> tuple[dict[str, Any], ...]:
        """Spot accounts have no position mode or margin settings to check."""
        return ({"event_type": "ACCOUNT_VERIFIED", "market": "spot"},)


def _liquidity(maker_flags: list[bool]) -> str:
    """One label per order update: MAKER only if every fill was maker (TAKER otherwise, the dearer)."""
    if not maker_flags:
        return ""
    return "MAKER" if all(maker_flags) else "TAKER"


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
        body.update(self._order_extras(request))
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

    def _order_extras(self, request: dict[str, Any]) -> dict[str, Any]:
        """Category-specific order fields (none for spot)."""
        return {}

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


class BinanceFuturesTestnetTransport(BinanceSpotTestnetTransport):
    """Binance USDⓈ-M futures testnet (one-way position mode only)."""

    ORDER_PATH = "/fapi/v1/order"

    def __init__(self, *, base_url: str = BINANCE_FUTURES_TESTNET_URL, **kwargs: Any) -> None:
        super().__init__(base_url=base_url, **kwargs)

    def _events(self, payload: dict[str, Any], *, submit: bool) -> tuple[dict[str, Any], ...]:
        native_status = str(payload.get("status", ""))
        executed = Decimal(str(payload.get("executedQty") or "0"))
        average = Decimal(str(payload.get("avgPrice") or "0"))
        if not average and executed and payload.get("cumQuote"):
            average = Decimal(str(payload["cumQuote"])) / executed
        common: dict[str, Any] = {
            "venue_order_id": str(payload.get("orderId", "")),
            "native_status": native_status,
        }
        venue_ms = payload.get("updateTime") or payload.get("time")
        if venue_ms:
            common["event_ts_ns"] = int(venue_ms) * 1_000_000
        if executed and getattr(self, "_fills", None) is not None:
            common.update(self._fills)
        return map_order_state(
            self.STATUS.get(native_status, native_status), executed, average, common, submit=submit,
        )

    def _fill_details(self, symbol: str, order_id: Any, key: str, secret: bytes) -> dict[str, Any]:
        """Maker/taker and venue commission per fill (``/fapi/v1/userTrades``)."""
        status, rows = self._signed("GET", "/fapi/v1/userTrades",
                                    {"symbol": symbol, "orderId": order_id}, key, secret)
        if status != 200 or not isinstance(rows, list):
            return {}
        return {
            "liquidity": _liquidity([bool(row.get("maker")) for row in rows]),
            "venue_commission": [
                {"commission": str(row.get("commission")), "asset": str(row.get("commissionAsset")),
                 "qty": str(row.get("qty")), "maker": bool(row.get("maker"))}
                for row in rows
            ],
        }

    def _with_fills(self, payload: dict[str, Any], symbol: str, key: str, secret: bytes, *, submit: bool):
        self._fills = (
            self._fill_details(symbol, payload.get("orderId"), key, secret)
            if Decimal(str(payload.get("executedQty") or "0")) else None
        )
        try:
            return self._events(payload, submit=submit)
        finally:
            self._fills = None

    def submit(self, request, symbol, key, secret):
        params: dict[str, Any] = {
            "symbol": symbol, "side": request["side"],
            "quantity": decimal_text(request["quantity"]),
            "newClientOrderId": request["order_id"], "newOrderRespType": "RESULT",
        }
        if request.get("reduce_only"):
            params["reduceOnly"] = "true"          # one-way mode only (verified at start-up)
        if request["order_type"] == "MARKET":
            params["type"] = "MARKET"
        else:
            params.update(
                type="LIMIT", price=decimal_text(request["price"]),
                timeInForce="GTX" if request.get("post_only") else request["time_in_force"],
            )
        status, payload = self._signed("POST", self.ORDER_PATH, params, key, secret)
        if status != 200:
            return ({"event_type": "REJECTED",
                     "reason": f"binance {payload.get('code')}: {payload.get('msg')}"},)
        return self._with_fills(payload, symbol, key, secret, submit=True)

    def query(self, request, symbol, key, secret):
        status, payload = self._signed("GET", self.ORDER_PATH, {
            "symbol": symbol, "origClientOrderId": request["order_id"],
        }, key, secret)
        if status == 400 and payload.get("code") == -2013:
            return ({"event_type": None, "reason": "order does not exist"},)
        if status != 200:
            raise ConnectionError(f"binance futures order query failed: {payload.get('msg')}")
        return self._with_fills(payload, symbol, key, secret, submit=False)

    def cancel(self, request, symbol, key, secret):
        status, payload = self._signed("DELETE", self.ORDER_PATH, {
            "symbol": symbol,
            "origClientOrderId": request.get("target_order_id") or request["order_id"],
        }, key, secret)
        if status == 400 and payload.get("code") in {-2011, -2013}:
            return ({"event_type": "REJECTED", "reason": "cancel target is not open"},)
        if status != 200:
            raise ConnectionError(f"binance futures cancel failed: {payload.get('msg')}")
        return self._events(payload, submit=False)

    def verify_account(self, request, symbol, key, secret):
        status, payload = self._signed("GET", "/fapi/v1/positionSide/dual", {}, key, secret)
        if status != 200:
            raise ConnectionError(f"binance futures position-mode query failed: {payload.get('msg')}")
        if payload.get("dualSidePosition") is not False:
            raise ValueError("binance futures account is in hedge mode; the platform trades one-way mode only "
                             "(change it in the account, not from code)")
        return ({"event_type": "ACCOUNT_VERIFIED", "market": "linear", "position_mode": "ONE_WAY",
                 "margin_mode": "NOT_VERIFIED (per-symbol margin type not checked)"},)


class BybitV5LinearTestnetTransport(BybitV5TestnetTransport):
    """Bybit v5 linear perpetuals on the testnet (one-way position mode only)."""

    MARGIN_MODES = {"CROSS": "REGULAR_MARGIN", "ISOLATED": "ISOLATED_MARGIN"}

    def __init__(self, *, expected_margin_mode: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("category", "linear")
        super().__init__(**kwargs)
        self.expected_margin_mode = expected_margin_mode

    def _order_extras(self, request: dict[str, Any]) -> dict[str, Any]:
        extras: dict[str, Any] = {"positionIdx": 0}          # one-way mode (verified at start-up)
        if request.get("reduce_only"):
            extras["reduceOnly"] = True
        return extras

    def submit(self, request, symbol, key, secret):
        events = super().submit(request, symbol, key, secret)
        return self._add_liquidity(events, request["order_id"], key, secret)

    def query(self, request, symbol, key, secret):
        return self._add_liquidity(super().query(request, symbol, key, secret), request["order_id"], key, secret)

    def _add_liquidity(self, events, order_link_id: str, key: str, secret: bytes):
        if not any(Decimal(str(e.get("cumulative_filled_quantity") or "0")) for e in events):
            return events
        payload = self._signed("GET", "/v5/execution/list", key, secret, params={
            "category": self.category, "orderLinkId": order_link_id,
        })
        rows = payload.get("result", {}).get("list", []) if payload.get("retCode") == 0 else []
        if not rows:
            return events
        liquidity = _liquidity([str(r.get("isMaker")).lower() == "true" for r in rows])
        commission = [{"commission": str(r.get("execFee")), "asset": str(r.get("feeCurrency", "")),
                       "qty": str(r.get("execQty")), "maker": str(r.get("isMaker")).lower() == "true"}
                      for r in rows]
        return tuple(
            {**e, "liquidity": liquidity, "venue_commission": commission}
            if Decimal(str(e.get("cumulative_filled_quantity") or "0")) else e
            for e in events
        )

    def verify_account(self, request, symbol, key, secret):
        info = self._signed("GET", "/v5/account/info", key, secret, params={})
        if info.get("retCode") != 0:
            raise ConnectionError(f"bybit account info failed: {info.get('retMsg')}")
        margin_mode = str(info.get("result", {}).get("marginMode", ""))
        expected = self.MARGIN_MODES.get(str(self.expected_margin_mode or "").upper())
        if expected and margin_mode != expected:
            raise ValueError(f"bybit account margin mode {margin_mode} does not match the configured "
                             f"{self.expected_margin_mode} ({expected}); change it in the account, not from code")
        positions = self._signed("GET", "/v5/position/list", key, secret,
                                 params={"category": self.category, "symbol": symbol})
        if positions.get("retCode") != 0:
            raise ConnectionError(f"bybit position list failed: {positions.get('retMsg')}")
        modes = {int(row.get("positionIdx", 0)) for row in positions.get("result", {}).get("list", [])}
        if modes - {0}:
            raise ValueError("bybit position is in hedge (both-side) mode; the platform trades one-way mode only")
        return ({"event_type": "ACCOUNT_VERIFIED", "market": "linear", "position_mode": "ONE_WAY",
                 "margin_mode": margin_mode or "UNKNOWN"},)


TRANSPORTS = {
    ("BINANCE", "spot"): BinanceSpotTestnetTransport,
    ("BINANCE", "linear"): BinanceFuturesTestnetTransport,
    ("BYBIT", "spot"): BybitV5TestnetTransport,
    ("BYBIT", "linear"): BybitV5LinearTestnetTransport,
}


def build_testnet_adapters(
    broker: CredentialBroker,
    credential_ids: dict[str, str],
    symbols: dict[str, dict[str, str]],
    *,
    http: HttpCall = urllib_http,
    clock_ns: Callable[[], int] = time.time_ns,
    markets: dict[str, str] | None = None,
    margin_modes: dict[str, str] | None = None,
    verify_accounts: bool = True,
) -> dict[str, TestnetVenueAdapter]:
    """One credentialed testnet adapter per venue (fails closed on gaps).

    ``markets`` (venue -> ``spot`` | ``linear``) selects the transport; take it
    from the instruments' product types with ``markets_for``. A futures
    account is verified (one-way mode; Bybit margin mode) before any order.
    """
    adapters = {}
    for venue, credential_id in sorted(credential_ids.items()):
        market = (markets or {}).get(venue, "spot")
        transport_cls = TRANSPORTS.get((venue, market))
        if transport_cls is None:
            raise ValueError(f"no testnet transport for {venue} {market}")
        kwargs: dict[str, Any] = {"symbols": symbols[venue], "http": http}
        if transport_cls is BybitV5LinearTestnetTransport:
            kwargs["expected_margin_mode"] = (margin_modes or {}).get(venue)
        transport = transport_cls(**kwargs)
        credentialed = CredentialedTestnetTransport(broker, credential_id, venue, transport)
        if verify_accounts and market == "linear":
            instrument_id = next(iter(symbols[venue]))
            credentialed({"action": "VERIFY_ACCOUNT", "instrument_id": instrument_id})
        adapters[venue] = TestnetVenueAdapter(venue, credentialed, clock_ns=clock_ns)
    return adapters


def markets_for(cfg) -> dict[str, str]:
    """venue -> ``linear`` for perpetual instruments, ``spot`` otherwise."""
    from .funding import is_perpetual

    return {
        venue: "linear" if is_perpetual(cfg.instruments.get(venue, cfg.instrument_id).product_type) else "spot"
        for venue in cfg.venues
    }
