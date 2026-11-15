"""Testnet clients for Bybit (spot, linear) and Binance (spot, futures).

Each client exposes the same small surface to the runner: public data, signed place / cancel / open-orders, a confirmed
private stream, and a server-time probe. Every request URL passes `assert_testnet_url`. Signed URLs contain a signature, so
errors raised from here carry only the method and path, never the query.
"""
from __future__ import annotations

import asyncio
import json
import ssl
import time
from decimal import Decimal
from typing import Awaitable, Callable
from urllib.parse import urlencode

from .endpoints import MARKETS, assert_testnet_url
from .models import Instrument, RestResult, UserEvent, fmt, stamp
from .parsers import (parse_binance_futures_ws, parse_binance_instruments, parse_binance_spot_ws, parse_bybit_instruments,
                      parse_bybit_ws)
from .signing import bybit_rest_signature, bybit_ws_auth_signature, binance_signed_query, binance_ws_api_signature

OnEvent = Callable[[UserEvent], None]


class VenueError(RuntimeError):
    pass


def ssl_context() -> ssl.SSLContext:
    import certifi   # the python.org build has no system CA bundle on this machine
    return ssl.create_default_context(cafile=certifi.where())


class _Base:
    def __init__(self, market: str, session, key: str | None = None, secret: str | None = None, ws_connect=None, network: str = "testnet"):
        if network == "mainnet":                 # real-money endpoints: its own table and allow-list (see mainnet.py); testnet is the default
            from .mainnet import MAINNET_MARKETS, assert_mainnet_url
            self.market, self.cfg, self._guard = market, MAINNET_MARKETS[market], assert_mainnet_url
        elif network == "testnet":
            self.market, self.cfg, self._guard = market, MARKETS[market], assert_testnet_url
        else:
            raise ValueError(f"unknown network {network!r}")
        self.network = network
        self.session, self._key, self._secret = session, key, secret
        self._ws_connect = ws_connect
        self.offset_ms = 0                       # server minus local, applied to signed timestamps
        self.stream_alive = False
        self.stream_error: str | None = None
        self._tasks: list[asyncio.Task] = []
        self._ws = None

    # ---- transport ------------------------------------------------------------------------------------------
    async def _http(self, method: str, url: str, headers: dict | None = None, data: str | None = None):
        self._guard(url)
        path = url.split("?", 1)[0]
        t_send = stamp()
        failure = None
        try:
            async with self.session.request(method, url, headers=headers, data=data) as resp:
                text = await resp.text()
                status = resp.status
        except Exception as exc:
            failure = type(exc).__name__
        if failure is not None:             # raised outside the except block so the original (whose text may hold the signed query) is not chained
            raise VenueError(f"{method} {path}: {failure}")
        t_ret = stamp()
        obj = None
        try:
            obj = json.loads(text) if text else {}
        except ValueError:
            pass
        if obj is None:
            raise VenueError(f"{method} {path}: non-JSON reply (HTTP {status})")
        return status, obj, t_send, t_ret

    def _now_ms(self) -> int:
        return int(time.time() * 1000) + self.offset_ms

    def _result(self, ok, status, t_send, t_ret, **kw) -> RestResult:
        return RestResult(ok=ok, http_status=status, t_send_wall_ns=t_send[0], t_send_mono_ns=t_send[1],
                          t_ret_wall_ns=t_ret[0], t_ret_mono_ns=t_ret[1], **kw)

    async def _connect(self, url: str):
        self._guard(url)
        if self._ws_connect is not None:
            return await self._ws_connect(url)
        import websockets
        return await websockets.connect(url, ssl=ssl_context(), ping_interval=None, max_size=2 ** 22)

    async def _read_loop(self, ws, on_event: OnEvent, parse) -> None:
        try:
            async for raw in ws:
                recv = stamp()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                for ev in parse(self.market, msg):
                    ev.recv_wall_ns, ev.recv_mono_ns = recv
                    on_event(ev)
        except Exception as exc:
            self.stream_error = type(exc).__name__
        finally:
            self.stream_alive = False

    async def restart_stream(self, on_event: OnEvent, timeout: float = 10.0) -> None:
        """Drop the private-stream connection and tasks and subscribe again. Only call with no order resting: events in the gap are lost."""
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except BaseException:
                pass
        self._tasks.clear()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self.stream_alive = False
        await self.start_stream(on_event, timeout)

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except BaseException:
                pass
        self._tasks.clear()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self.stream_alive = False


class BybitClient(_Base):
    recv_window = "5000"
    stream_variants = 1
    stream_variant = 0

    @property
    def category(self) -> str:
        return self.cfg["category"]

    async def _public(self, path: str, params: dict):
        status, obj, *_ = await self._http("GET", f"{self.cfg['rest']}{path}?{urlencode(params)}")
        if obj.get("retCode") != 0:
            raise VenueError(f"GET {path}: retCode {obj.get('retCode')} {obj.get('retMsg')}")
        return obj

    def _headers(self, payload: str) -> dict:
        ts = str(self._now_ms())
        return {"X-BAPI-API-KEY": self._key, "X-BAPI-SIGN": bybit_rest_signature(self._secret, ts, self._key, self.recv_window, payload),
                "X-BAPI-SIGN-TYPE": "2", "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": self.recv_window,
                "Content-Type": "application/json"}

    async def server_time_ms(self):
        t0 = stamp()
        obj = await self._public("/v5/market/time", {})
        t1 = stamp()
        return int(obj["time"]), t0[0], t1[0]

    async def instruments(self) -> dict[str, Instrument]:
        out: dict[str, Instrument] = {}
        cursor = ""
        for _ in range(10):
            params = {"category": self.category, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            obj = await self._public("/v5/market/instruments-info", params)
            out.update(parse_bybit_instruments(self.market, obj))
            cursor = obj.get("result", {}).get("nextPageCursor") or ""
            if not cursor:
                break
        return out

    async def best_bid_ask(self, symbol: str):
        obj = await self._public("/v5/market/tickers", {"category": self.category, "symbol": symbol})
        rows = obj.get("result", {}).get("list", [])
        if not rows:
            return Decimal(0), Decimal(0)
        return Decimal(rows[0].get("bid1Price") or 0), Decimal(rows[0].get("ask1Price") or 0)

    async def reference_price(self, symbol: str):
        return None

    async def place(self, symbol: str, price: Decimal, qty: Decimal, client_id: str, side: str = "Buy") -> RestResult:
        body = json.dumps({"category": self.category, "symbol": symbol, "side": side, "orderType": "Limit", "qty": fmt(qty),
                           "price": fmt(price), "timeInForce": "PostOnly", "orderLinkId": client_id}, separators=(",", ":"))
        status, obj, ts, tr = await self._http("POST", f"{self.cfg['rest']}/v5/order/create", self._headers(body), body)
        ok = obj.get("retCode") == 0
        return self._result(ok, status, ts, tr, order_id=(obj.get("result") or {}).get("orderId"), ex_time_ms=obj.get("time"),
                            code=str(obj.get("retCode")), msg=str(obj.get("retMsg", "")))

    async def market_close(self, symbol: str, side: str, qty: Decimal, client_id: str) -> RestResult:
        """Reduce-only market order that flattens a position (side = the side to trade). Used only to undo probe fills."""
        body = json.dumps({"category": self.category, "symbol": symbol, "side": side, "orderType": "Market", "qty": fmt(qty),
                           "reduceOnly": True, "orderLinkId": client_id}, separators=(",", ":"))
        status, obj, ts, tr = await self._http("POST", f"{self.cfg['rest']}/v5/order/create", self._headers(body), body)
        return self._result(obj.get("retCode") == 0, status, ts, tr, order_id=(obj.get("result") or {}).get("orderId"),
                            ex_time_ms=obj.get("time"), code=str(obj.get("retCode")), msg=str(obj.get("retMsg", "")))

    async def position_size(self, symbol: str) -> Decimal:
        """Signed one-way position size (long positive)."""
        query = urlencode({"category": self.category, "symbol": symbol})
        _, obj, *_ = await self._http("GET", f"{self.cfg['rest']}/v5/position/list?{query}", self._headers(query))
        if obj.get("retCode") != 0:
            raise VenueError(f"GET /v5/position/list: retCode {obj.get('retCode')} {obj.get('retMsg')}")
        total = Decimal(0)
        for r in obj.get("result", {}).get("list", []):
            size = Decimal(str(r.get("size") or 0))
            total += size if r.get("side") == "Buy" else -size if r.get("side") == "Sell" else Decimal(0)
        return total

    async def cancel(self, symbol: str, order_id: str | None, client_id: str) -> RestResult:
        ident = {"orderId": order_id} if order_id else {"orderLinkId": client_id}
        body = json.dumps({"category": self.category, "symbol": symbol, **ident}, separators=(",", ":"))
        status, obj, ts, tr = await self._http("POST", f"{self.cfg['rest']}/v5/order/cancel", self._headers(body), body)
        return self._result(obj.get("retCode") == 0, status, ts, tr, order_id=(obj.get("result") or {}).get("orderId"),
                            ex_time_ms=obj.get("time"), code=str(obj.get("retCode")), msg=str(obj.get("retMsg", "")))

    async def amend(self, symbol: str, order_id: str | None, client_id: str, price: Decimal, qty: Decimal | None = None, side: str = "Buy") -> RestResult:
        """Change the price (and optionally the size) of a resting order: POST /v5/order/amend. UNVERIFIED against the live API (written from the v5 docs; run it on testnet first). The order keeps its PostOnly flag."""
        ident = {"orderId": order_id} if order_id else {"orderLinkId": client_id}
        payload = {"category": self.category, "symbol": symbol, **ident, "price": fmt(price)}
        if qty is not None:
            payload["qty"] = fmt(qty)
        body = json.dumps(payload, separators=(",", ":"))
        status, obj, ts, tr = await self._http("POST", f"{self.cfg['rest']}/v5/order/amend", self._headers(body), body)
        return self._result(obj.get("retCode") == 0, status, ts, tr, order_id=(obj.get("result") or {}).get("orderId"),
                            ex_time_ms=obj.get("time"), code=str(obj.get("retCode")), msg=str(obj.get("retMsg", "")))

    async def open_orders(self, symbol: str) -> list[dict]:
        query = urlencode({"category": self.category, "symbol": symbol, "limit": 50})
        _, obj, *_ = await self._http("GET", f"{self.cfg['rest']}/v5/order/realtime?{query}", self._headers(query))
        if obj.get("retCode") != 0:
            raise VenueError(f"GET /v5/order/realtime: retCode {obj.get('retCode')} {obj.get('retMsg')}")
        return [{"order_id": r.get("orderId"), "client_id": r.get("orderLinkId", "")} for r in obj.get("result", {}).get("list", [])]

    async def start_stream(self, on_event: OnEvent, timeout: float = 10.0) -> None:
        """Connect, authenticate and subscribe to `order`; return only when both replies say success."""
        ws = self._ws = await self._connect(self.cfg["ws"])
        expires = int((time.time() + 5.0) * 1000) + self.offset_ms
        await ws.send(json.dumps({"op": "auth", "args": [self._key, expires, bybit_ws_auth_signature(self._secret, expires)]}))
        await self._await_op(ws, "auth", timeout)
        await ws.send(json.dumps({"op": "subscribe", "args": ["order"]}))
        await self._await_op(ws, "subscribe", timeout)
        self.stream_alive, self.stream_error = True, None
        self._tasks.append(asyncio.create_task(self._read_loop(ws, on_event, parse_bybit_ws)))
        self._tasks.append(asyncio.create_task(self._ping(ws)))

    async def _await_op(self, ws, op: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise VenueError(f"private stream: no {op} reply within {timeout}s")
            msg = json.loads(await asyncio.wait_for(ws.recv(), left))
            if msg.get("op") == op:
                if msg.get("success"):
                    return
                raise VenueError(f"private stream: {op} refused: {msg.get('ret_msg')}")

    async def _ping(self, ws) -> None:
        while True:
            await asyncio.sleep(20)
            await ws.send(json.dumps({"op": "ping"}))


class BinanceClient(_Base):
    recv_window = 5000
    stream_variant = 0

    @property
    def stream_variants(self) -> int:
        """Mainnet USD-M moved its streams to /public, /market and /private paths; the private form is tried first, the legacy one second."""
        return 2 if (self.network == "mainnet" and self.market == "binance_futures") else 1

    def _private_ws_url(self, listen_key: str) -> str:
        if self.stream_variants == 2 and self.stream_variant == 0:
            return f"{self.cfg['ws'].rsplit('/ws/', 1)[0]}/private/ws?listenKey={listen_key}"
        return self.cfg["ws"] + listen_key

    def _p(self, spot: str, fut: str) -> str:
        return spot if self.market == "binance_spot" else fut

    @property
    def _order_path(self) -> str:
        return self._p("/api/v3/order", "/fapi/v1/order")

    async def _public(self, path: str, params: dict | None = None):
        q = f"?{urlencode(params)}" if params else ""
        status, obj, *_ = await self._http("GET", f"{self.cfg['rest']}{path}{q}")
        if status != 200:
            raise VenueError(f"GET {path}: HTTP {status} code {obj.get('code')} {obj.get('msg')}")
        return obj

    def _signed_url(self, path: str, params: dict) -> str:
        params = {**params, "recvWindow": self.recv_window, "timestamp": self._now_ms()}
        return f"{self.cfg['rest']}{path}?{binance_signed_query(self._secret, params)}"

    @property
    def _key_header(self) -> dict:
        return {"X-MBX-APIKEY": self._key}

    async def server_time_ms(self):
        t0 = stamp()
        obj = await self._public(self._p("/api/v3/time", "/fapi/v1/time"))
        t1 = stamp()
        return int(obj["serverTime"]), t0[0], t1[0]

    async def instruments(self) -> dict[str, Instrument]:
        obj = await self._public(self._p("/api/v3/exchangeInfo", "/fapi/v1/exchangeInfo"))
        return parse_binance_instruments(self.market, obj)

    async def best_bid_ask(self, symbol: str):
        obj = await self._public(self._p("/api/v3/ticker/bookTicker", "/fapi/v1/ticker/bookTicker"), {"symbol": symbol})
        return Decimal(obj.get("bidPrice") or 0), Decimal(obj.get("askPrice") or 0)

    async def reference_price(self, symbol: str):
        if self.market == "binance_spot":
            obj = await self._public("/api/v3/avgPrice", {"symbol": symbol})
            return Decimal(obj["price"])
        obj = await self._public("/fapi/v1/premiumIndex", {"symbol": symbol})
        return Decimal(obj["markPrice"])

    async def place(self, symbol: str, price: Decimal, qty: Decimal, client_id: str, side: str = "Buy") -> RestResult:
        params = {"symbol": symbol, "side": side.upper(), "quantity": fmt(qty), "price": fmt(price), "newClientOrderId": client_id,
                  "newOrderRespType": "ACK"}
        if self.market == "binance_spot":
            params["type"] = "LIMIT_MAKER"
        else:
            params.update(type="LIMIT", timeInForce="GTX")
        status, obj, ts, tr = await self._http("POST", self._signed_url(self._order_path, params), self._key_header)
        ok = status == 200 and "orderId" in obj
        return self._result(ok, status, ts, tr, order_id=str(obj["orderId"]) if ok else None,
                            ex_time_ms=obj.get("transactTime") or obj.get("updateTime"),
                            code=str(obj.get("code", "")), msg=str(obj.get("msg", "")))

    async def market_close(self, symbol: str, side: str, qty: Decimal, client_id: str) -> RestResult:
        """Reduce-only market order that flattens a futures position. Used only to undo probe fills."""
        if self.market != "binance_futures":
            raise VenueError("market_close is for the perpetual markets only")
        params = {"symbol": symbol, "side": side.upper(), "type": "MARKET", "quantity": fmt(qty), "reduceOnly": "true",
                  "newClientOrderId": client_id, "newOrderRespType": "ACK"}
        status, obj, ts, tr = await self._http("POST", self._signed_url(self._order_path, params), self._key_header)
        ok = status == 200 and "orderId" in obj
        return self._result(ok, status, ts, tr, order_id=str(obj["orderId"]) if ok else None,
                            ex_time_ms=obj.get("transactTime") or obj.get("updateTime"), code=str(obj.get("code", "")), msg=str(obj.get("msg", "")))

    async def position_size(self, symbol: str) -> Decimal:
        status, obj, *_ = await self._http("GET", self._signed_url("/fapi/v2/positionRisk", {"symbol": symbol}), self._key_header)
        if status != 200 or not isinstance(obj, list):
            raise VenueError(f"GET /fapi/v2/positionRisk: HTTP {status} code {obj.get('code') if isinstance(obj, dict) else '?'}")
        return sum((Decimal(str(r.get("positionAmt") or 0)) for r in obj), Decimal(0))

    async def cancel(self, symbol: str, order_id: str | None, client_id: str) -> RestResult:
        ident = {"orderId": order_id} if order_id else {"origClientOrderId": client_id}
        status, obj, ts, tr = await self._http("DELETE", self._signed_url(self._order_path, {"symbol": symbol, **ident}), self._key_header)
        ok = status == 200 and "orderId" in obj
        return self._result(ok, status, ts, tr, order_id=str(obj["orderId"]) if ok else None,
                            ex_time_ms=obj.get("transactTime") or obj.get("updateTime"),
                            code=str(obj.get("code", "")), msg=str(obj.get("msg", "")))

    async def amend(self, symbol: str, order_id: str | None, client_id: str, price: Decimal, qty: Decimal | None = None, side: str = "Buy") -> RestResult:
        """Change the price and size of a resting USD-M futures LIMIT order: PUT /fapi/v1/order (side, quantity and price are all required by the venue, so `qty` is required here). UNVERIFIED against the live API (written from the docs; run it on testnet first)."""
        if self.market != "binance_futures":
            raise VenueError("amend is for the perpetual markets only")
        if qty is None:
            raise VenueError("Binance futures amend needs the quantity")
        ident = {"orderId": order_id} if order_id else {"origClientOrderId": client_id}
        params = {"symbol": symbol, "side": side.upper(), "quantity": fmt(qty), "price": fmt(price), **ident}
        status, obj, ts, tr = await self._http("PUT", self._signed_url(self._order_path, params), self._key_header)
        ok = status == 200 and "orderId" in obj
        return self._result(ok, status, ts, tr, order_id=str(obj["orderId"]) if ok else None, ex_time_ms=obj.get("updateTime"),
                            code=str(obj.get("code", "")), msg=str(obj.get("msg", "")))

    async def open_orders(self, symbol: str) -> list[dict]:
        path = self._p("/api/v3/openOrders", "/fapi/v1/openOrders")
        status, obj, *_ = await self._http("GET", self._signed_url(path, {"symbol": symbol}), self._key_header)
        if status != 200 or not isinstance(obj, list):
            raise VenueError(f"GET {path}: HTTP {status} code {obj.get('code') if isinstance(obj, dict) else '?'}")
        return [{"order_id": str(r.get("orderId")), "client_id": r.get("clientOrderId", "")} for r in obj]

    @property
    def _listen_path(self) -> str:
        return self._p("/api/v3/userDataStream", "/fapi/v1/listenKey")

    async def _start_spot_stream(self, on_event: OnEvent, timeout: float) -> None:
        """Spot: subscribe on the WebSocket API with a signed request. Returns only when the reply says status 200. Events
        arrive on the same connection wrapped as {"subscriptionId": n, "event": {...}}; the server pings every 20 s and the
        websockets library answers."""
        ws = self._ws = await asyncio.wait_for(self._connect(self.cfg["ws_api"]), timeout)
        params = {"apiKey": self._key, "timestamp": self._now_ms(), "recvWindow": int(self.recv_window)}
        params["signature"] = binance_ws_api_signature(self._secret, params)
        await ws.send(json.dumps({"id": "ollog-sub", "method": "userDataStream.subscribe.signature", "params": params}))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise VenueError(f"user data stream: no subscribe reply within {timeout}s")
            msg = json.loads(await asyncio.wait_for(ws.recv(), left))
            if msg.get("id") == "ollog-sub":
                if msg.get("status") == 200:
                    break
                err = msg.get("error") or {}
                raise VenueError(f"user data stream: subscribe refused: status {msg.get('status')} code {err.get('code')} {err.get('msg')}")
        self.stream_alive, self.stream_error = True, None
        self._tasks.append(asyncio.create_task(self._read_loop(ws, on_event, parse_binance_spot_ws)))

    async def start_stream(self, on_event: OnEvent, timeout: float = 10.0) -> None:
        """Futures: no subscribe reply, so the stream is 'confirmed' when the listen key is issued and the socket is open."""
        if self.market == "binance_spot":
            return await self._start_spot_stream(on_event, timeout)
        status, obj, *_ = await self._http("POST", f"{self.cfg['rest']}{self._listen_path}", self._key_header)
        if status != 200 or "listenKey" not in obj:
            raise VenueError(f"listen key refused: HTTP {status} code {obj.get('code')} {obj.get('msg')}")
        self._listen_key = obj["listenKey"]
        ws = self._ws = await asyncio.wait_for(self._connect(self._private_ws_url(self._listen_key)), timeout)
        self.stream_alive, self.stream_error = True, None
        self._tasks.append(asyncio.create_task(self._read_loop(ws, on_event, parse_binance_futures_ws)))
        self._tasks.append(asyncio.create_task(self._keepalive()))

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(20 * 60)
            await self._http("PUT", f"{self.cfg['rest']}{self._listen_path}?{urlencode({'listenKey': self._listen_key})}", self._key_header)


class PaperClient:
    """PAPER venue for the perp probe v2 (owner 15 Nov 2026): the private side (orders, acks, fills, cancels, amends, positions) is SIMULATED against a LIVE PUBLIC capture; nothing is sent to any venue, no keys are used.
    A PostOnly order that would cross is refused like the venue does (a stream Rejected on Bybit, Expired on Binance); a resting order joins the back of the queue at its price (the capture book's size there) and fills when
    prints at its price take that queue away (the rest of it) or when a print goes THROUGH its price (all of it). This is a crude queue model for testing the runner's plumbing, limits and logging: it is NOT fill evidence."""
    stream_variants = 1
    stream_variant = 0

    def __init__(self, market: str, capture, instruments: dict, seed: int = 1, latency_ms: tuple[float, float] = (15.0, 60.0), maker_fee_bps: float = 2.0, taker_fee_bps: float = 5.5):
        import random
        self.market, self.capture, self._instruments = market, capture, instruments
        self.rng = random.Random(f"paper:{seed}:{market}")
        self.latency_ms, self.maker_fee_bps, self.taker_fee_bps = latency_ms, maker_fee_bps, taker_fee_bps
        self.offset_ms = 0
        self.stream_alive, self.stream_error = False, None
        self._on_event = None
        self.orders: dict[str, dict] = {}
        self.position: dict[str, Decimal] = {}
        self._n = 0
        self.log: list[dict] = []
        capture.trade_hooks.append(self._on_trade)

    # ---- plumbing the runner expects -------------------------------------------------------------------------------
    async def server_time_ms(self):
        t0 = stamp()
        t1 = stamp()
        return int(time.time() * 1000), t0[0], t1[0]

    async def instruments(self) -> dict:
        return self._instruments

    async def start_stream(self, on_event, timeout: float = 10.0) -> None:
        self._on_event, self.stream_alive = on_event, True

    async def restart_stream(self, on_event, timeout: float = 10.0) -> None:
        await self.start_stream(on_event, timeout)

    async def close(self) -> None:
        self.stream_alive = False

    # ---- events ------------------------------------------------------------------------------------------------------
    def _emit(self, kind: str, o: dict, status: str, delay_s: float = 0.0, reason: str = "", fill: tuple[Decimal, Decimal] | None = None) -> None:
        ms = int(time.time() * 1000)
        if self.market.startswith("bybit"):
            raw = {"orderStatus": status, "symbol": o["symbol"], "orderId": o["oid"], "orderLinkId": o["cid"], "price": str(o["price"]), "createdTime": str(o["created_ms"]), "updatedTime": str(ms),
                   "cumExecQty": str(o["filled"]), "avgPrice": str(o["avg"]), "cumExecFee": str(o["fee"]), "category": "linear"}
        else:
            raw = {"e": "ORDER_TRADE_UPDATE", "E": ms, "T": ms, "o": {"s": o["symbol"], "c": o["cid"], "i": o["oid"], "x": {"new": "NEW", "cancelled": "CANCELED", "rejected": "REJECTED", "expired": "EXPIRED", "trade": "TRADE"}.get(kind, "AMENDMENT"),
                   "X": status, "z": str(o["filled"]), "L": str(fill[1]) if fill else "0", "l": str(fill[0]) if fill else "0", "T": ms, "n": str(o["fee"]), "p": str(o["price"])}}
        from .models import UserEvent as UE
        def deliver() -> None:
            if self._on_event is not None and self.stream_alive:
                w, m = stamp()
                self._on_event(UE(self.market, kind, o["symbol"], o["oid"], o["cid"], ms, ms, status, reason, recv_wall_ns=w, recv_mono_ns=m, raw=raw))
        try:
            asyncio.get_running_loop().call_later(delay_s, deliver)
        except RuntimeError:
            deliver()

    def _lat(self) -> float:
        return self.rng.uniform(*self.latency_ms) / 1000.0

    def _result(self, ok: bool, t0, oid=None, code="", msg="") -> RestResult:
        t1 = stamp()
        return RestResult(ok=ok, order_id=oid, ex_time_ms=int(time.time() * 1000), code=code, msg=msg, http_status=200, t_send_wall_ns=t0[0], t_send_mono_ns=t0[1], t_ret_wall_ns=t1[0], t_ret_mono_ns=t1[1])

    def _touch(self, symbol: str):
        bids, asks = self.capture.books[symbol].top(20)
        return bids, asks

    def _size_at(self, levels, price: Decimal) -> Decimal:
        for p, sz in levels:
            if abs(p - float(price)) < 1e-12:
                return Decimal(str(sz))
        return Decimal(0)

    # ---- orders ------------------------------------------------------------------------------------------------------
    async def place(self, symbol: str, price: Decimal, qty: Decimal, client_id: str, side: str = "Buy") -> RestResult:
        t0 = stamp()
        await asyncio.sleep(self._lat())
        self._n += 1
        o = {"symbol": symbol, "side": side, "price": price, "qty": qty, "filled": Decimal(0), "avg": Decimal(0), "fee": Decimal(0), "cid": client_id, "oid": f"P{self._n:08d}", "created_ms": int(time.time() * 1000), "live": True}
        bids, asks = self._touch(symbol)
        crosses = bool(bids and asks) and ((side == "Buy" and float(price) >= asks[0][0]) or (side == "Sell" and float(price) <= bids[0][0]))
        self.orders[client_id] = o
        if crosses:
            o["live"] = False
            self._emit("rejected" if self.market.startswith("bybit") else "expired", o, "Rejected" if self.market.startswith("bybit") else "EXPIRED", self._lat(), reason="EC_PostOnlyWillTakeLiquidity")
        else:
            o["ahead"] = self._size_at(bids if side == "Buy" else asks, price)
            self._emit("new", o, "New" if self.market.startswith("bybit") else "NEW", self._lat())
        self.log.append({"op": "place", "cid": client_id, "price": str(price), "crosses": crosses})
        return self._result(True, t0, o["oid"])

    async def cancel(self, symbol: str, order_id: str | None, client_id: str) -> RestResult:
        t0 = stamp()
        await asyncio.sleep(self._lat())
        o = self.orders.get(client_id)
        if o is None or not o["live"]:
            return self._result(False, t0, None, "PAPER_ORDER_GONE", "order already filled, cancelled or never rested")
        o["live"] = False
        self._emit("cancelled", o, "Cancelled" if self.market.startswith("bybit") else "CANCELED", self._lat())
        self.log.append({"op": "cancel", "cid": client_id})
        return self._result(True, t0, o["oid"])

    async def amend(self, symbol: str, order_id: str | None, client_id: str, price: Decimal, qty: Decimal | None = None, side: str = "Buy") -> RestResult:
        t0 = stamp()
        await asyncio.sleep(self._lat())
        o = self.orders.get(client_id)
        if o is None or not o["live"]:
            return self._result(False, t0, None, "PAPER_ORDER_GONE", "order already filled, cancelled or never rested")
        bids, asks = self._touch(symbol)
        if bids and asks and ((o["side"] == "Buy" and float(price) >= asks[0][0]) or (o["side"] == "Sell" and float(price) <= bids[0][0])):
            return self._result(False, t0, o["oid"], "PAPER_POSTONLY_AMEND", "amend would take liquidity; the order stays where it is")
        o["price"] = price
        o["ahead"] = self._size_at(bids if o["side"] == "Buy" else asks, price)                    # an amend of the price loses the queue place
        self._emit("new" if self.market.startswith("bybit") else "amended", o, "New" if self.market.startswith("bybit") else "NEW", self._lat())
        self.log.append({"op": "amend", "cid": client_id, "price": str(price)})
        return self._result(True, t0, o["oid"])

    async def open_orders(self, symbol: str) -> list[dict]:
        return [{"order_id": o["oid"], "client_id": o["cid"]} for o in self.orders.values() if o["live"] and o["symbol"] == symbol]

    async def position_size(self, symbol: str) -> Decimal:
        return self.position.get(symbol, Decimal(0))

    async def market_close(self, symbol: str, side: str, qty: Decimal, client_id: str) -> RestResult:
        t0 = stamp()
        await asyncio.sleep(self._lat())
        bids, asks = self._touch(symbol)
        if not bids or not asks:
            return self._result(False, t0, None, "PAPER_NO_BOOK", "no book")
        px = Decimal(str(asks[0][0] if side == "Buy" else bids[0][0]))
        self._n += 1
        o = {"symbol": symbol, "side": side, "price": px, "qty": qty, "filled": qty, "avg": px, "fee": px * qty * Decimal(str(self.taker_fee_bps)) / Decimal(10000), "cid": client_id, "oid": f"P{self._n:08d}", "created_ms": int(time.time() * 1000), "live": False}
        self.orders[client_id] = o
        self.position[symbol] = self.position.get(symbol, Decimal(0)) + (qty if side == "Buy" else -qty)
        self._emit("trade", o, "Filled" if self.market.startswith("bybit") else "FILLED", self._lat(), fill=(qty, px))
        self.log.append({"op": "market_close", "cid": client_id})
        return self._result(True, t0, o["oid"])

    # ---- fills from the live public prints ------------------------------------------------------------------------------
    def _on_trade(self, symbol: str, price: float, size: float, side: str, ts_s: float) -> None:
        taker_sell = str(side).lower().startswith("s")
        for o in list(self.orders.values()):
            if not o["live"] or o["symbol"] != symbol:
                continue
            buy = o["side"] == "Buy"
            if buy != taker_sell:                       # a resting buy is hit by a sell aggressor, a resting sell by a buy aggressor
                continue
            px = float(o["price"])
            through = price < px - 1e-12 if buy else price > px + 1e-12
            at = abs(price - px) <= 1e-12
            if not through and not at:
                continue
            remaining = o["qty"] - o["filled"]
            if through:
                take = remaining
            else:
                o["ahead"] -= Decimal(str(size))
                if o["ahead"] >= 0:
                    continue
                take = min(-o["ahead"], remaining)
                o["ahead"] = Decimal(0)
            if take <= 0:
                continue
            fill_px = o["price"]
            o["avg"] = (o["avg"] * o["filled"] + fill_px * take) / (o["filled"] + take)
            o["filled"] += take
            o["fee"] += fill_px * take * Decimal(str(self.maker_fee_bps)) / Decimal(10000)
            self.position[symbol] = self.position.get(symbol, Decimal(0)) + (take if buy else -take)
            done = o["filled"] >= o["qty"]
            o["live"] = not done
            by = self.market.startswith("bybit")
            self._emit("trade", o, ("Filled" if done else "PartiallyFilled") if by else ("FILLED" if done else "PARTIALLY_FILLED"), self._lat(), fill=(take, fill_px))
            self.log.append({"op": "fill", "cid": o["cid"], "qty": str(take)})


def make_client(market: str, session, key: str | None = None, secret: str | None = None, ws_connect=None, network: str = "testnet"):
    if network == "mainnet":
        from .mainnet import MAINNET_MARKETS
        venue = MAINNET_MARKETS[market]["venue"]
    else:
        venue = MARKETS[market]["venue"]
    cls = BybitClient if venue == "bybit" else BinanceClient
    return cls(market, session, key, secret, ws_connect, network)
