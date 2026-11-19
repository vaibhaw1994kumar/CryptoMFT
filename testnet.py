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

* ``account_snapshot`` (owner 13 Nov, exchange-side restart reconciliation): the perpetual transports read the
  account's orders (open and closed, keyed by the CLIENT order id this platform sent), executions since a
  timestamp and positions, read-only. Paging and time windows follow the venue documentation (Bybit v5:
  cursor paging, windows of at most 7 days, 50 orders / 100 executions a page; Binance USDⓈ-M: windows of
  less than 7 days, 1000 rows a page); a window older than the venue keeps data, a page that does not end,
  an unmapped status or a hedge-mode position raises, so the caller keeps the restart gate closed.

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

REDUCE_ONLY_NOTE = ("DERIVED_FROM_ONE_WAY_MODE (both venues accept a reduce-only order in one-way mode; Binance rejects it in hedge mode). UNVERIFIED by an order: the start-up check sends none. "
                    "Whether a reduce-only order is exempt from the minimum notional is also UNVERIFIED; both are items of the testnet drill")


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
        # (e), owner 17 Nov: an amend moves the SAME venue order (Bybit amend-order, Binance modify-order). The OMS knows the moved order under the REPLACE's own id; the venue knows it under the
        # original client id. replacement id -> {"root": the venue's client id, "base": the quantity the venue order had filled when the amend took effect, "since_ms": when}: a query or cancel of the
        # replacement goes to the root, and its fills are reported net of `base` (those belong to the old order in the OMS lineage; reporting them again would count them twice).
        self._aliases: dict[str, dict[str, Any]] = {}
        self._fill_since_ms: int | None = None

    def _root(self, order_id: str) -> tuple[str, Decimal, int | None]:
        alias = self._aliases.get(order_id)
        return (order_id, Decimal("0"), None) if alias is None else (alias["root"], alias["base"], alias["since_ms"])

    def _net_of(self, events: tuple[dict[str, Any], ...], base: Decimal) -> tuple[dict[str, Any], ...]:
        if not base:
            return events
        out = []
        for event in events:
            if "cumulative_filled_quantity" in event:
                net = max(Decimal("0"), Decimal(str(event["cumulative_filled_quantity"])) - base)
                event = {**event, "cumulative_filled_quantity": decimal_text(net), "last_fill_quantity": decimal_text(net)}
            out.append(event)
        return tuple(out)

    def amend(self, request, symbol, key, secret):
        """No amend on this transport (spot): refused, never sent as a second order. The OMS records AMEND_REJECTED and keeps the old order."""
        return ({"event_type": "REJECTED", "reason": f"amend not supported by {type(self).__name__}: refused, nothing sent"},)

    def __call__(
        self, request: dict[str, Any], secret: memoryview
    ) -> tuple[dict[str, Any], ...]:
        key, secret_bytes = split_secret(secret)
        action = str(request["action"])
        try:
            symbol = self.symbols[str(request["instrument_id"])] if action != "ACCOUNT_SNAPSHOT" else ""
        except KeyError as exc:
            raise ValueError(f"no {self.venue} symbol for {request['instrument_id']}") from exc
        if action == "ACCOUNT_SNAPSHOT":
            return (self.account_snapshot(request, key, secret_bytes),)
        if action == "ACCOUNT_FEE_RATE":
            return (self.account_fee_rate(request, symbol, key, secret_bytes),)
        if action == "ACCOUNT_RISK_TIERS":
            return (self.account_risk_tiers(request, symbol, key, secret_bytes),)
        if action == "ACCOUNT_RULES":                 # F07: read-only
            return (self.account_rules(request, symbol, key, secret_bytes),)
        if action == "VERIFY_ACCOUNT":
            return self.verify_account(request, symbol, key, secret_bytes)
        if action == "REPLACE":                     # (e): an amend in place, NEVER a second order (until 17 Nov a REPLACE was sent as a SUBMIT)
            return self.amend(request, symbol, key, secret_bytes)
        if action == "SUBMIT":
            return self.submit(request, symbol, key, secret_bytes)
        if action in ("CANCEL", "QUERY"):
            field = "target_order_id" if action == "CANCEL" and request.get("target_order_id") else "order_id"
            root, base, since_ms = self._root(str(request[field]))
            if root != request[field]:
                request = {**request, field: root, **({"order_id": root} if field == "target_order_id" else {})}
            self._fill_since_ms = since_ms
            try:
                events = self.cancel(request, symbol, key, secret_bytes) if action == "CANCEL" else self.query(request, symbol, key, secret_bytes)
            finally:
                self._fill_since_ms = None
            return self._net_of(events, base)
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

    # ---- account snapshot (owner 13 Nov): the perpetual transports implement it; the venue's data limits are constants here --------------
    SNAPSHOT_WINDOW_MS = 6 * 86_400_000        # one request window: under the 7 days both venues allow
    SNAPSHOT_RETENTION_MS = 90 * 86_400_000    # Binance keeps trades and orders 90 days (the shorter of the two); older than that is refused, not guessed
    SNAPSHOT_MAX_PAGES = 200

    def account_snapshot(self, request, key, secret) -> dict[str, Any]:  # pragma: no cover - spot has none
        raise ValueError(f"account_snapshot is built for the perpetual transports only, not {type(self).__name__}")

    def account_fee_rate(self, request, symbol, key, secret) -> dict[str, Any]:  # pragma: no cover - perpetual transports only
        raise ValueError(f"account_fee_rate is built for the perpetual transports only, not {type(self).__name__}")

    def account_risk_tiers(self, request, symbol, key, secret) -> dict[str, Any]:  # pragma: no cover - perpetual transports only
        raise ValueError(f"account_risk_tiers is built for the perpetual transports only, not {type(self).__name__}")

    def _snapshot_windows(self, since_ms: int, now_ms: int) -> list[tuple[int, int]]:
        if since_ms > now_ms:
            raise ValueError("account_snapshot since is in the future")
        if now_ms - since_ms > self.SNAPSHOT_RETENTION_MS:
            raise ValueError("account_snapshot since is older than the venue keeps orders and trades: refusing to reconcile from a partial history")
        out, start = [], since_ms
        while start <= now_ms:
            end = min(start + self.SNAPSHOT_WINDOW_MS, now_ms)
            out.append((start, end))
            start = end + 1
        return out

    def _instrument_of(self, symbol: str) -> str:
        for instrument_id, name in self.symbols.items():
            if name == symbol:
                return instrument_id
        raise ConnectionError(f"{self.venue} reported a symbol this platform does not trade: {symbol}")


def _liquidity(maker_flags: list[bool]) -> str:
    """One label per order update: MAKER only if every fill was maker (TAKER otherwise, the dearer)."""
    if not maker_flags:
        return ""
    return "MAKER" if all(maker_flags) else "TAKER"


def risk_tier_rows_binance(payload: Any, symbol: str) -> list[dict[str, str]]:
    """Owner 16 Nov (limit A6): the account's own leverage brackets from Binance USD-M `GET /fapi/v1/leverageBracket?symbol=` (signed; a list with one object per symbol, or the object itself). Each bracket:
    notionalCap (upper bound), maintMarginRatio, cum (the maintenance deduction), initialLeverage (the initial rate is 1 / leverage). SHAPE FROM THE PUBLIC DOCUMENTATION, UNVERIFIED against the live endpoint in
    this build: tested on hand-written recorded messages; confirmed in the testnet drill. Anything else raises (the start-up then refuses)."""
    items = payload if isinstance(payload, list) else [payload]
    row = next((x for x in items if isinstance(x, dict) and str(x.get("symbol")) == symbol), None)
    brackets = None if row is None else row.get("brackets")
    if not brackets:
        raise ConnectionError(f"binance leverageBracket returned no brackets for {symbol}")
    out = []
    for b in sorted(brackets, key=lambda b: Decimal(str(b["notionalCap"]))):
        leverage = Decimal(str(b["initialLeverage"]))
        if leverage <= 0:
            raise ConnectionError("binance leverageBracket: a bracket has no initial leverage")
        out.append({"max_notional": str(b["notionalCap"]), "maintenance_rate": str(b["maintMarginRatio"]), "initial_rate": str(Decimal(1) / leverage),
                    "maintenance_deduction": str(b.get("cum", "0"))})
    return out


def risk_tier_rows_bybit(payload: Any, symbol: str) -> list[dict[str, str]]:
    """Owner 16 Nov (limit A6): the risk-limit tiers of Bybit v5 `GET /v5/market/risk-limit?category=linear&symbol=` (result.list rows: riskLimitValue = the tier's upper bound, maintenanceMargin and initialMargin
    as rates, mmDeduction when present). The endpoint is the symbol's tier ladder, not the account's selected tier; the account's selected risk limit is a separate setting (UNVERIFIED: the drill reads it). SHAPE FROM THE
    PUBLIC DOCUMENTATION, UNVERIFIED against the live endpoint in this build. Anything else raises."""
    if not isinstance(payload, dict) or payload.get("retCode") != 0:
        raise ConnectionError(f"bybit risk-limit failed: {payload.get('retMsg') if isinstance(payload, dict) else payload}")
    rows = [r for r in payload.get("result", {}).get("list", []) if str(r.get("symbol")) == symbol]
    if not rows:
        raise ConnectionError(f"bybit risk-limit returned no tiers for {symbol}")
    out = []
    for r in sorted(rows, key=lambda r: Decimal(str(r["riskLimitValue"]))):
        out.append({"max_notional": str(r["riskLimitValue"]), "maintenance_rate": str(r["maintenanceMargin"]), "initial_rate": str(r["initialMargin"]),
                    "maintenance_deduction": str(r.get("mmDeduction") or "0")})
    return out


def _known(value: Any) -> str:
    """A field the venue gave, as text; "UNKNOWN" when it is missing or empty (never defaulted)."""
    return "UNKNOWN" if value is None or str(value).strip() == "" else str(value)


def account_rules_bybit(positions: Any, account_info: Any, ladder: Any, symbol: str, fetched_ts_ns: int | None) -> dict[str, Any]:
    """F07 (owner 20 Nov): the account's EFFECTIVE rules for `symbol` on Bybit v5 linear, from GET /v5/position/list (riskId, riskLimitValue, leverage, tradeMode, autoAddMargin, markPrice),
    GET /v5/account/info (marginMode) and the symbol ladder GET /v5/market/risk-limit (the row whose `id` is the position's riskId gives its maintenance and initial rates). A missing riskId is UNKNOWN, never
    defaulted; every missing field is listed in `unknown`. No balance or account id is kept. Shapes from the public documentation; confirmed against recorded testnet responses (tests/fixtures)."""
    rows = [r for r in ((positions or {}).get("result") or {}).get("list", []) if str(r.get("symbol")) == symbol] if isinstance(positions, dict) else []
    row = rows[0] if rows else {}
    risk_id = _known(row.get("riskId"))
    tier = None
    if risk_id != "UNKNOWN" and isinstance(ladder, dict):
        tier = next((r for r in (ladder.get("result") or {}).get("list", []) if str(r.get("symbol")) == symbol and str(r.get("id")) == risk_id), None)
    trade_mode = {"0": "CROSS", "1": "ISOLATED"}.get(str(row.get("tradeMode")), "UNKNOWN")
    out = {
        "venue": "BYBIT", "symbol": symbol, "source": "BYBIT /v5/position/list + /v5/account/info + /v5/market/risk-limit", "fetched_ts_ns": fetched_ts_ns,
        "risk_id": risk_id, "max_notional": _known(row.get("riskLimitValue") or (tier or {}).get("riskLimitValue")), "leverage": _known(row.get("leverage")),
        "margin_mode": _known(((account_info or {}).get("result") or {}).get("marginMode") if isinstance(account_info, dict) else None), "position_trade_mode": trade_mode,
        "auto_add_margin": _known(row.get("autoAddMargin")), "maintenance_rate": _known((tier or {}).get("maintenanceMargin")), "initial_rate": _known((tier or {}).get("initialMargin")),
        "mark_price": _known(row.get("markPrice")),
    }
    if out["initial_rate"] != "UNKNOWN" and out["leverage"] != "UNKNOWN" and Decimal(out["leverage"]) > 0:
        out["initial_rate"] = str(max(Decimal(out["initial_rate"]), Decimal(1) / Decimal(out["leverage"])))     # the initial margin is the position value / the ACCOUNT leverage (never below the tier's own rate)
    out["unknown"] = sorted(k for k, v in out.items() if v == "UNKNOWN")
    return out


def account_rules_binance(brackets: Any, position_risk: Any, symbol: str, fetched_ts_ns: int | None) -> dict[str, Any]:
    """F07 (owner 20 Nov): the account's EFFECTIVE rules for `symbol` on Binance USD-M, from GET /fapi/v1/leverageBracket (the account's brackets) and GET /fapi/v2/positionRisk (leverage, marginType,
    isAutoAddMargin, maxNotionalValue = the largest notional the account's leverage allows, markPrice). Binance has no risk id: `risk_id` is the bracket number in force at the account's leverage (the highest
    bracket whose initial leverage still allows it), UNKNOWN when it cannot be found. The maintenance rate is that bracket's; the initial rate is 1 / the ACCOUNT leverage (not the bracket's maximum)."""
    items = brackets if isinstance(brackets, list) else [brackets]
    entry = next((x for x in items if isinstance(x, dict) and str(x.get("symbol")) == symbol), None)
    rows = sorted((entry or {}).get("brackets") or [], key=lambda b: Decimal(str(b.get("notionalCap", "0"))))
    pos = next((r for r in position_risk if isinstance(r, dict) and str(r.get("symbol")) == symbol), {}) if isinstance(position_risk, list) else {}
    leverage = _known(pos.get("leverage"))
    in_force = None
    if leverage != "UNKNOWN" and rows:
        lev = Decimal(leverage)
        allowed = [b for b in rows if Decimal(str(b.get("initialLeverage", "0"))) >= lev]
        in_force = allowed[-1] if allowed else None
    out = {
        "venue": "BINANCE", "symbol": symbol, "source": "BINANCE /fapi/v1/leverageBracket + /fapi/v2/positionRisk", "fetched_ts_ns": fetched_ts_ns,
        "risk_id": _known((in_force or {}).get("bracket")), "max_notional": _known(pos.get("maxNotionalValue") or (in_force or {}).get("notionalCap")), "leverage": leverage,
        "margin_mode": _known(str(pos.get("marginType", "")).upper() or None), "position_trade_mode": _known(str(pos.get("marginType", "")).upper() or None),
        "auto_add_margin": _known(pos.get("isAutoAddMargin")), "maintenance_rate": _known((in_force or {}).get("maintMarginRatio")),
        "initial_rate": "UNKNOWN" if leverage == "UNKNOWN" or Decimal(leverage) <= 0 else str(Decimal(1) / Decimal(leverage)), "mark_price": _known(pos.get("markPrice")),
        # Binance charges maintenance by the position's notional bracket (the leverage only bounds the notional): the ladder is kept so the start-up check reads the bracket at the hard cap
        "maintenance_ladder": [[str(b.get("notionalCap")), str(b.get("maintMarginRatio"))] for b in rows],
    }
    out["unknown"] = sorted(k for k, v in out.items() if v == "UNKNOWN")
    return out


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

    def _row(self, order_link_id: str, symbol: str, key: str, secret: bytes) -> dict[str, Any] | None:
        payload = self._signed("GET", "/v5/order/realtime", key, secret, params={"category": self.category, "symbol": symbol, "orderLinkId": order_link_id})
        rows = payload.get("result", {}).get("list", []) if payload.get("retCode") == 0 else []
        return rows[0] if rows else None

    def amend(self, request, symbol, key, secret):
        """(e), owner 17 Nov: Bybit amend-order (POST /v5/order/amend) moves the SAME order: `qty` is the order's new TOTAL quantity (the REPLACE carries the quote's total) and `price` the new price; the
        venue keeps the order id and gives up its queue place. Linear perpetuals only (spot refuses, see the base class). The amend counts only when the venue shows the order at the new price:
        then REPLACED and the order live (ACKNOWLEDGED) are reported for the replacement id, which from then on is an alias of the venue order. A refused amend is REJECTED and the order is unchanged.
        An amend the venue accepted but does not show yet gives AMEND_ACCEPTED only (the OMS waits and queries; nothing is assumed)."""
        if self.category != "linear":
            return super().amend(request, symbol, key, secret)
        root, _base, _since = self._root(str(request["target_order_id"]))
        new_price = Decimal(str(request["price"]))
        payload = self._signed("POST", "/v5/order/amend", key, secret, body={
            "category": self.category, "symbol": symbol, "orderLinkId": root,
            "qty": decimal_text(request["quantity"]), "price": decimal_text(new_price),
        })
        code = payload.get("retCode")
        if code in (110001, 110008, 110010):            # order does not exist / already filled or cancelled
            return ({"event_type": "REJECTED", "reason": f"replace target is not open (bybit {code}: {payload.get('retMsg')})"},)
        if code != 0:
            return ({"event_type": "REJECTED", "reason": f"amend rejected, order unchanged: bybit {code}: {payload.get('retMsg')}"},)
        for attempt in range(self.settle_queries):
            row = self._row(root, symbol, key, secret)
            if row is not None and Decimal(str(row.get("price") or "0")) == new_price:
                executed = Decimal(str(row.get("cumExecQty") or "0"))
                self._aliases[str(request["order_id"])] = {"root": root, "base": executed, "since_ms": self.clock_ms()}
                status = self.STATUS.get(str(row.get("orderStatus", "")), "")
                live = {"event_type": "ACKNOWLEDGED", "venue_order_id": str(row.get("orderId", "")), "native_status": str(row.get("orderStatus", ""))}
                if status in ("NEW", "PARTIAL"):
                    return ({"event_type": "REPLACED"}, live)
                return ({"event_type": "REPLACED"},)    # it filled or ended right after the amend: the next query reports it
            if attempt + 1 < self.settle_queries:
                self.sleep(self.settle_interval_seconds)
        return ({"event_type": "AMEND_ACCEPTED"},)

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
        if self._fill_since_ms is not None:              # (e): an amended order's own fills only (the earlier ones belong to the old order of the lineage)
            rows = [row for row in rows if int(row.get("time") or 0) >= self._fill_since_ms]
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

    def amend(self, request, symbol, key, secret):
        """(e), owner 17 Nov: Binance USD-M modify-order (PUT /fapi/v1/order) moves the SAME order (LIMIT orders; the venue re-queues it): symbol, side, the order's new TOTAL quantity and price,
        addressed by the original client id. Counted only when the venue's answer shows the new price: then REPLACED and the order live; the replacement id becomes an alias of the venue order.
        A refused modify is REJECTED and the order is unchanged. UNVERIFIED until the drill: whether a GTX (post-only) order can be modified."""
        root, _base, _since = self._root(str(request["target_order_id"]))
        new_price = Decimal(str(request["price"]))
        status, payload = self._signed("PUT", self.ORDER_PATH, {
            "symbol": symbol, "origClientOrderId": root, "side": request["side"],
            "quantity": decimal_text(request["quantity"]), "price": decimal_text(new_price),
        }, key, secret)
        if status == 400 and payload.get("code") in (-2011, -2013):
            return ({"event_type": "REJECTED", "reason": f"replace target is not open (binance {payload.get('code')}: {payload.get('msg')})"},)
        if status != 200:
            return ({"event_type": "REJECTED", "reason": f"amend rejected, order unchanged: binance {payload.get('code')}: {payload.get('msg')}"},)
        if Decimal(str(payload.get("price") or "0")) != new_price:
            return ({"event_type": "AMEND_ACCEPTED"},)
        executed = Decimal(str(payload.get("executedQty") or "0"))
        self._aliases[str(request["order_id"])] = {"root": root, "base": executed, "since_ms": self.clock_ms()}
        kind = self.STATUS.get(str(payload.get("status", "")), "")
        live = {"event_type": "ACKNOWLEDGED", "venue_order_id": str(payload.get("orderId", "")), "native_status": str(payload.get("status", ""))}
        return ({"event_type": "REPLACED"}, live) if kind in ("NEW", "PARTIAL") else ({"event_type": "REPLACED"},)

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

    # ---- account snapshot (owner 13 Nov) --------------------------------------------------------------------------------------------------------
    POSITION_PATH = "/fapi/v2/positionRisk"       # signed positionAmt, positionSide BOTH in one-way mode: from the venue's v2 shape, to be confirmed in the testnet drill (a wrong sign shows as venue_position_differs, never as a silent pass)
    TRADES_PAGE = 1000

    def _list(self, path: str, params: dict[str, Any], key: str, secret: bytes) -> list[dict[str, Any]]:
        status, rows = self._signed("GET", path, params, key, secret)
        if status != 200 or not isinstance(rows, list):
            message = rows.get("msg") if isinstance(rows, dict) else rows
            raise ConnectionError(f"binance futures {path} failed: {message}")
        return rows

    def _trades(self, symbol: str, windows: list[tuple[int, int]], key: str, secret: bytes) -> dict[Any, dict[str, Any]]:
        """Every trade of the symbol in the windows, by the venue's trade id. A window is paged forward by moving its start to the last trade's time (the id de-duplicates the overlap); a full page that adds
        nothing new means more trades share one millisecond than a page holds, which cannot be paged past: raised, not truncated."""
        seen: dict[Any, dict[str, Any]] = {}
        for start, end in windows:
            cursor = start
            for _ in range(self.SNAPSHOT_MAX_PAGES):
                rows = self._list("/fapi/v1/userTrades", {"symbol": symbol, "startTime": cursor, "endTime": end, "limit": self.TRADES_PAGE}, key, secret)
                new = [row for row in rows if row["id"] not in seen]
                seen.update({row["id"]: row for row in rows})
                if len(rows) < self.TRADES_PAGE:
                    break
                if not new:
                    raise ConnectionError("binance futures userTrades page did not advance")
                cursor = max(int(row["time"]) for row in rows)
            else:
                raise ConnectionError("binance futures userTrades paging did not end")
        return seen

    FEE_RATE_PATH = "/fapi/v1/commissionRate"      # UNVERIFIED against the venue's docs in this session (the docs page was truncated before the section); confirmed in the testnet drill. Fails closed on any other shape.

    def account_fee_rate(self, request, symbol, key, secret) -> dict[str, Any]:
        """The account's own maker/taker commission rate for the symbol (a signed GET, read-only): fractions as text (0.0002 = 2 bps). Any status or shape other than the expected one raises."""
        status, row = self._signed("GET", self.FEE_RATE_PATH, {"symbol": symbol}, key, secret)
        if status != 200 or not isinstance(row, dict) or str(row.get("symbol")) != symbol or "makerCommissionRate" not in row or "takerCommissionRate" not in row:
            message = row.get("msg") if isinstance(row, dict) else row
            raise ConnectionError(f"binance futures {self.FEE_RATE_PATH} failed or returned an unexpected shape: {message}")
        return {"event_type": "ACCOUNT_FEE_RATE", "maker_rate": str(row["makerCommissionRate"]), "taker_rate": str(row["takerCommissionRate"])}

    def account_snapshot(self, request, key, secret) -> dict[str, Any]:
        """The account as the venue holds it: orders (open ones, and every order created in the window, closed or not) keyed by the CLIENT order id, executions since `since_ns` keyed by the venue's trade id and
        tied to the client id through the order, and the one-way position per instrument (signed). Read-only. A trade whose order the venue did not list is fetched once by its venue id; if that fails too its
        order id is `venue-order:<id>`, which the OMS flags as an execution for an unknown order (the gate stays closed)."""
        now_ms = self.clock_ms()
        windows = self._snapshot_windows(int(request["since_ns"]) // 1_000_000, now_ms)
        orders: dict[str, dict[str, Any]] = {}
        client_of: dict[Any, str] = {}
        executions: list[dict[str, Any]] = []
        positions: dict[str, str] = {}
        for instrument_id, symbol in sorted(self.symbols.items()):
            rows = list(self._list("/fapi/v1/openOrders", {"symbol": symbol}, key, secret))
            for start, end in windows:
                page = self._list("/fapi/v1/allOrders", {"symbol": symbol, "startTime": start, "endTime": end, "limit": 1000}, key, secret)
                if len(page) >= 1000:                                              # a full page may be a truncated one, and the next page is not asked for: raised, never a partial account
                    raise ConnectionError("binance futures allOrders returned a full page for one window: more orders than a page holds")
                rows += page
            by_venue_id: dict[Any, dict[str, Any]] = {}
            for row in rows:
                if str(row.get("symbol", symbol)) != symbol:
                    raise ConnectionError(f"binance futures returned an order for {row.get('symbol')} on the {symbol} query")
                client_of[row["orderId"]] = str(row["clientOrderId"])
                by_venue_id[row["orderId"]] = row                                  # the same order from openOrders and allOrders is one order
            trades = self._trades(symbol, windows, key, secret)
            fees: dict[Any, Decimal] = {}
            for trade in trades.values():
                fees[trade["orderId"]] = fees.get(trade["orderId"], Decimal("0")) + Decimal(str(trade.get("commission") or "0"))
            for venue_id, row in by_venue_id.items():
                native = str(row.get("status", ""))
                state = self.STATUS.get(native)
                if state is None:
                    raise ConnectionError(f"unmapped venue order state {native!r}")
                executed = Decimal(str(row.get("executedQty") or "0"))
                average = Decimal(str(row.get("avgPrice") or "0"))
                if not average and executed and row.get("cumQuote"):
                    average = Decimal(str(row["cumQuote"])) / executed
                orders[str(row["clientOrderId"])] = {
                    "order_id": str(row["clientOrderId"]), "venue_order_id": str(venue_id), "instrument_id": instrument_id, "state": state,
                    "filled_quantity": decimal_text(executed), "average_price": decimal_text(average), "fee": decimal_text(fees.get(venue_id, Decimal("0"))),
                    "observed_ts_ns": int(row.get("updateTime") or row.get("time") or now_ms) * 1_000_000,
                }
            for trade in sorted(trades.values(), key=lambda t: (int(t["time"]), int(t["id"]))):
                venue_id = trade["orderId"]
                if venue_id not in client_of:                                      # an order older than the window that this trade belongs to: ask for it by its venue id
                    status, payload = self._signed("GET", self.ORDER_PATH, {"symbol": symbol, "orderId": venue_id}, key, secret)
                    if status == 200 and isinstance(payload, dict) and payload.get("clientOrderId"):
                        client_of[venue_id] = str(payload["clientOrderId"])
                executions.append({
                    "execution_id": str(trade["id"]), "order_id": client_of.get(venue_id, f"venue-order:{venue_id}"), "instrument_id": instrument_id,
                    "quantity": decimal_text(trade["qty"]), "price": decimal_text(trade["price"]), "ts_ns": int(trade["time"]) * 1_000_000,
                    "fee": decimal_text(trade.get("commission") or "0"),
                })
            amount = Decimal("0")
            for row in self._list(self.POSITION_PATH, {"symbol": symbol}, key, secret):
                size = Decimal(str(row.get("positionAmt") or "0"))
                if size and str(row.get("positionSide", "BOTH")) != "BOTH":
                    raise ValueError("binance futures position is in hedge mode (positionSide LONG/SHORT); the platform trades one-way mode only")
                amount += size
            positions[instrument_id] = decimal_text(amount)
        return {"event_type": "ACCOUNT_SNAPSHOT", "orders": list(orders.values()), "executions": executions, "positions": positions, "fetched_ts_ns": now_ms * 1_000_000}

    def account_rules(self, request, symbol, key, secret) -> dict[str, Any]:
        """F07: the account's effective rules (read-only): the account's leverage brackets and the position-risk row. Raw payloads only on `record_raw` (see the Bybit transport)."""
        status, brackets = self._signed("GET", "/fapi/v1/leverageBracket", {"symbol": symbol}, key, secret)
        if status != 200:
            raise ConnectionError(f"binance futures leverageBracket failed: {brackets.get('msg') if isinstance(brackets, dict) else brackets}")
        status, rows = self._signed("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, key, secret)
        if status != 200:
            raise ConnectionError(f"binance futures positionRisk failed: {rows.get('msg') if isinstance(rows, dict) else rows}")
        fetched = max((int(r.get("updateTime", 0)) for r in rows if isinstance(r, dict)), default=0) * 1_000_000 if isinstance(rows, list) else 0
        rules = account_rules_binance(brackets, rows, symbol, fetched or None)
        return {"event_type": "ACCOUNT_RULES", "rules": rules, **({"raw": {"leverage_bracket": brackets, "position_risk": rows}} if request.get("record_raw") else {})}

    def account_risk_tiers(self, request, symbol, key, secret) -> dict[str, Any]:
        status, payload = self._signed("GET", "/fapi/v1/leverageBracket", {"symbol": symbol}, key, secret)
        if status != 200:
            raise ConnectionError(f"binance futures leverageBracket failed: {payload.get('msg') if isinstance(payload, dict) else payload}")
        return {"event_type": "ACCOUNT_RISK_TIERS", "tiers": risk_tier_rows_binance(payload, symbol), "source": "BINANCE /fapi/v1/leverageBracket (account read)"}

    def verify_account(self, request, symbol, key, secret):
        status, payload = self._signed("GET", "/fapi/v1/positionSide/dual", {}, key, secret)
        if status != 200:
            raise ConnectionError(f"binance futures position-mode query failed: {payload.get('msg')}")
        if payload.get("dualSidePosition") is not False:
            raise ValueError("binance futures account is in hedge mode; the platform trades one-way mode only "
                             "(change it in the account, not from code)")
        event = {"event_type": "ACCOUNT_VERIFIED", "market": "linear", "position_mode": "ONE_WAY",
                 "margin_mode": "NOT_VERIFIED (per-symbol margin type not checked)", "reduce_only_accepted": REDUCE_ONLY_NOTE}
        if request.get("expected_margin_mode") is not None or request.get("expected_leverage") is not None:
            # owner 16 Nov (limit A8): the per-symbol margin type and leverage from GET /fapi/v2/positionRisk (signed; a row per symbol, flat or not)
            status, rows = self._signed("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, key, secret)
            row = next((r for r in rows if str(r.get("symbol")) == symbol), None) if status == 200 and isinstance(rows, list) else None
            if row is None:
                raise ConnectionError("binance futures positionRisk returned no row for the symbol: margin type and leverage cannot be verified")
            margin = str(row.get("marginType", "")).lower()
            leverage = int(Decimal(str(row.get("leverage", "0"))))
            expected_mode, expected_leverage = request.get("expected_margin_mode"), request.get("expected_leverage")
            if expected_mode is not None and margin != str(expected_mode).lower():
                raise ValueError(f"binance futures margin type is {margin or 'unknown'}, the profile expects {expected_mode}; change it in the account, not from code")
            if expected_leverage is not None and leverage != int(expected_leverage):
                raise ValueError(f"binance futures leverage for {symbol} is {leverage}, the profile expects {expected_leverage}; change it in the account, not from code")
            event.update(margin_mode=margin.upper(), leverage=leverage)
        return (event,)


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
        if self._fill_since_ms is not None:              # (e): an amended order's own fills only
            rows = [r for r in rows if int(r.get("execTime") or 0) >= self._fill_since_ms]
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

    # ---- account snapshot (owner 13 Nov) --------------------------------------------------------------------------------------------------------
    def _all_pages(self, path: str, key: str, secret: bytes, params: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(self.SNAPSHOT_MAX_PAGES):
            payload = self._signed("GET", path, key, secret, params={**params, **({"cursor": cursor} if cursor else {})})
            if payload.get("retCode") != 0:
                raise ConnectionError(f"bybit {path} failed: {payload.get('retMsg')}")
            result = payload.get("result", {})
            rows += result.get("list", [])
            cursor = str(result.get("nextPageCursor") or "")
            if not cursor:
                return rows
        raise ConnectionError(f"bybit {path} paging did not end")

    def account_fee_rate(self, request, symbol, key, secret) -> dict[str, Any]:
        """The account's own maker/taker fee rate for the symbol: GET /v5/account/fee-rate (category, symbol; verified against the venue's docs 14 Nov 2026: result.list rows with symbol, takerFeeRate, makerFeeRate).
        The docs do not state the unit; the rates are read as fractions (0.0002 = 2 bps) and a value that is not a fraction below 1 raises. Read-only."""
        payload = self._signed("GET", "/v5/account/fee-rate", key, secret, params={"category": self.category, "symbol": symbol})
        if payload.get("retCode") != 0:
            raise ConnectionError(f"bybit /v5/account/fee-rate failed: {payload.get('retMsg')}")
        rows = [r for r in payload.get("result", {}).get("list", []) if str(r.get("symbol")) == symbol]
        if len(rows) != 1 or "makerFeeRate" not in rows[0] or "takerFeeRate" not in rows[0]:
            raise ConnectionError(f"bybit /v5/account/fee-rate returned no single row for {symbol}")
        maker, taker = str(rows[0]["makerFeeRate"]), str(rows[0]["takerFeeRate"])
        if abs(Decimal(maker)) >= 1 or abs(Decimal(taker)) >= 1:
            raise ConnectionError("bybit /v5/account/fee-rate returned a rate of 1 or more: not a fraction, the unit is unconfirmed")
        return {"event_type": "ACCOUNT_FEE_RATE", "maker_rate": maker, "taker_rate": taker}

    def account_snapshot(self, request, key, secret) -> dict[str, Any]:
        """The account as the venue holds it (see the Binance transport): orders keyed by `orderLinkId` (the client id this platform sent; an order placed another way has none and is reported as
        `venue-order:<id>`, which the OMS flags), the open orders plus the order history of each window, executions (execType Trade only) since `since_ns` keyed by `execId`, and the one-way position per
        instrument, signed (Buy positive, Sell negative). Limits used: history and executions are asked in windows under 7 days with cursor paging (50 orders / 100 executions a page); Bybit keeps a
        Cancelled, Rejected or Deactivated order with no fill only 24 hours, so a plan whose only order was cancelled with no fill more than a day before the restart cannot be shown by the venue and stays unreconciled."""
        now_ms = self.clock_ms()
        windows = self._snapshot_windows(int(request["since_ns"]) // 1_000_000, now_ms)
        orders: dict[str, dict[str, Any]] = {}
        executions: dict[str, dict[str, Any]] = {}
        positions: dict[str, str] = {}
        for instrument_id, symbol in sorted(self.symbols.items()):
            rows = self._all_pages("/v5/order/realtime", key, secret, {"category": self.category, "symbol": symbol, "limit": 50})
            for start, end in windows:
                rows += self._all_pages("/v5/order/history", key, secret, {"category": self.category, "symbol": symbol, "startTime": start, "endTime": end, "limit": 50})
            for row in rows:
                native = str(row.get("orderStatus", ""))
                state = self.STATUS.get(native)
                if state is None:
                    raise ConnectionError(f"unmapped venue order state {native!r}")
                client_id = str(row.get("orderLinkId") or f"venue-order:{row.get('orderId')}")
                updated = int(row.get("updatedTime") or row.get("createdTime") or now_ms)
                if client_id in orders and int(orders[client_id]["observed_ts_ns"]) >= updated * 1_000_000:
                    continue                                                      # the same order from the open list and the history: the later report wins
                executed = Decimal(str(row.get("cumExecQty") or "0"))
                orders[client_id] = {
                    "order_id": client_id, "venue_order_id": str(row.get("orderId", "")), "instrument_id": instrument_id,
                    "state": state,
                    "filled_quantity": decimal_text(executed), "average_price": decimal_text(row.get("avgPrice") or "0"),
                    "fee": decimal_text(row.get("cumExecFee") or "0"), "observed_ts_ns": updated * 1_000_000,
                }
            for start, end in windows:
                for row in self._all_pages("/v5/execution/list", key, secret, {"category": self.category, "symbol": symbol, "startTime": start, "endTime": end, "limit": 100}):
                    if str(row.get("execType", "Trade")) != "Trade":
                        continue                                                  # funding, fee and delivery rows are not fills
                    executions[str(row["execId"])] = {
                        "execution_id": str(row["execId"]), "order_id": str(row.get("orderLinkId") or f"venue-order:{row.get('orderId')}"), "instrument_id": instrument_id,
                        "quantity": decimal_text(row["execQty"]), "price": decimal_text(row["execPrice"]), "ts_ns": int(row["execTime"]) * 1_000_000,
                        "fee": decimal_text(row.get("execFee") or "0"),
                    }
            amount = Decimal("0")
            for row in self._all_pages("/v5/position/list", key, secret, {"category": self.category, "symbol": symbol, "limit": 200}):
                size = Decimal(str(row.get("size") or "0"))
                if size and int(row.get("positionIdx", 0)) != 0:
                    raise ValueError("bybit position is in hedge (both-side) mode; the platform trades one-way mode only")
                amount += size if str(row.get("side")) == "Buy" else -size if str(row.get("side")) == "Sell" else Decimal("0")
            positions[instrument_id] = decimal_text(amount)
        return {"event_type": "ACCOUNT_SNAPSHOT", "orders": list(orders.values()),
                "executions": sorted(executions.values(), key=lambda e: (e["ts_ns"], e["execution_id"])), "positions": positions, "fetched_ts_ns": now_ms * 1_000_000}

    def account_rules(self, request, symbol, key, secret) -> dict[str, Any]:
        """F07: the account's effective rules (read-only): position info, the account's margin mode and the symbol ladder. The raw payloads are returned beside the parsed rules ONLY when the request asks
        (`record_raw`, the drill's fixture recording, which redacts them before anything is written); they are never logged."""
        positions = self._signed("GET", "/v5/position/list", key, secret, params={"category": self.category, "symbol": symbol})
        info = self._signed("GET", "/v5/account/info", key, secret, params={})
        ladder = self._signed("GET", "/v5/market/risk-limit", key, secret, params={"category": self.category, "symbol": symbol})
        fetched = int(positions.get("time", 0)) * 1_000_000 if isinstance(positions, dict) and positions.get("time") else None
        rules = account_rules_bybit(positions, info, ladder, symbol, fetched)
        return {"event_type": "ACCOUNT_RULES", "rules": rules, **({"raw": {"position_list": positions, "account_info": info, "risk_limit": ladder}} if request.get("record_raw") else {})}

    def account_risk_tiers(self, request, symbol, key, secret) -> dict[str, Any]:
        payload = self._signed("GET", "/v5/market/risk-limit", key, secret, params={"category": self.category, "symbol": symbol})
        return {"event_type": "ACCOUNT_RISK_TIERS", "tiers": risk_tier_rows_bybit(payload, symbol), "source": "BYBIT /v5/market/risk-limit (symbol tier ladder)"}

    def verify_account(self, request, symbol, key, secret):
        info = self._signed("GET", "/v5/account/info", key, secret, params={})
        if info.get("retCode") != 0:
            raise ConnectionError(f"bybit account info failed: {info.get('retMsg')}")
        margin_mode = str(info.get("result", {}).get("marginMode", ""))
        expected = self.MARGIN_MODES.get(str(request.get("expected_margin_mode") or self.expected_margin_mode or "").upper())
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
        event = {"event_type": "ACCOUNT_VERIFIED", "market": "linear", "position_mode": "ONE_WAY",
                 "margin_mode": margin_mode or "UNKNOWN", "reduce_only_accepted": REDUCE_ONLY_NOTE}
        expected_leverage = request.get("expected_leverage")
        if expected_leverage is not None:                                  # owner 16 Nov (limit A8): the symbol's leverage from the same position list
            rows = [r for r in positions.get("result", {}).get("list", []) if str(r.get("symbol")) == symbol]
            if not rows or not str(rows[0].get("leverage", "")).strip():
                raise ConnectionError("bybit position list returned no leverage for the symbol: it cannot be verified")
            leverage = int(Decimal(str(rows[0]["leverage"])))
            if leverage != int(expected_leverage):
                raise ValueError(f"bybit leverage for {symbol} is {leverage}, the profile expects {expected_leverage}; change it in the account, not from code")
            event["leverage"] = leverage
        return (event,)


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


class AccountVerificationError(RuntimeError):
    """Owner 16 Nov (limit A8): the account is not as the profile expects (hedge mode, another margin mode or leverage, no reader, a read that failed): a paper or live session then refuses to start."""


def verify_accounts_for_start(readers: dict[str, Any], venues: tuple[str, ...], instrument_id: str, *, margin_mode: str | None, leverage: int | None) -> list[dict[str, Any]]:
    """Run `verify_account` on every venue's reader at session start-up. Collects every problem (no reader, a failed read, a mismatch) and raises once; returns one facts row per venue. Read-only: it sends no order, so
    reduce-only acceptance is DERIVED from one-way mode and labelled UNVERIFIED in the facts (REDUCE_ONLY_NOTE)."""
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    for venue in venues:
        reader = readers.get(venue)
        if reader is None or not hasattr(reader, "verify_account"):
            problems.append(f"{venue}: no account verifier (the account's settings are not checked)")
            continue
        try:
            facts = reader.verify_account(instrument_id, margin_mode, leverage)
            rows.append({"venue": venue, **{k: v for k, v in facts.items() if k != "event_type"}})
        except Exception as exc:
            problems.append(f"{venue}: {type(exc).__name__}: {exc}")
    if problems:
        raise AccountVerificationError("account settings differ from the profile or could not be verified: " + "; ".join(problems))
    return rows
