"""Offline tests: the URL guard, signing vectors, parsers, pricing, derived-latency math, universe choice, request building."""
import asyncio
import hashlib
import hmac
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ollog import endpoints, parsers, pricing, signing, universe, venues  # noqa: E402
from ollog.derive import derive  # noqa: E402
from ollog.models import Instrument, fmt  # noqa: E402
from ollog.summary import quantiles  # noqa: E402


# ---- guard -------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "https://api.bybit.com/v5/order/create", "https://api.binance.com/api/v3/order", "https://fapi.binance.com/fapi/v1/order",
    "wss://stream.bybit.com/v5/private", "wss://fstream.binance.com/ws/x", "wss://stream.binance.com:9443/ws/x",
    "https://api-testnet.bybit.com.evil.com/x", "https://evil.com/?h=api-testnet.bybit.com", "https://testnet.binance.vision@evil.com/",
    "http://api-testnet.bybit.com/v5/market/time", "https://api-testnet.bybit.com:8443/x", "https://user:pw@api-testnet.bybit.com/x",
    "https://API.BYBIT.COM/x", "ws://stream-testnet.bybit.com/v5/private",
    "wss://ws-api.binance.com:443/ws-api/v3", "wss://ws-api.testnet.binance.vision.evil.com/ws-api/v3",
    "wss://ws-api.testnet.binance.vision:9443/ws-api/v3",
])
def test_guard_refuses_everything_that_is_not_an_exact_testnet_host(url):
    with pytest.raises(endpoints.NotTestnet):
        endpoints.assert_testnet_url(url)


def test_guard_accepts_the_configured_testnet_endpoints():
    for m in endpoints.MARKETS.values():
        endpoints.assert_testnet_url(m["rest"])
        endpoints.assert_testnet_url(m["ws"] + "listenkey")
        if "ws_api" in m:
            endpoints.assert_testnet_url(m["ws_api"])
    endpoints.assert_testnet_url("https://API-TESTNET.bybit.com/v5/market/time")


def test_every_client_request_goes_through_the_guard():
    class Boom:
        def request(self, *a, **k):
            raise AssertionError("a request was attempted")
    c = venues.make_client("bybit_spot", Boom(), "k", "s")
    c.cfg = {**c.cfg, "rest": "https://api.bybit.com"}          # even a tampered table cannot reach a mainnet host
    with pytest.raises(endpoints.NotTestnet):
        asyncio.run(c.server_time_ms())


# ---- signing -----------------------------------------------------------------------------------------------------
def test_binance_signature_matches_the_documented_example():
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    query = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"
    assert signing.binance_signature(secret, query) == "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"


def test_binance_signed_query_appends_the_signature_of_the_exact_query():
    q = signing.binance_signed_query("sec", {"a": 1, "b": "x y"})
    body, sig = q.rsplit("&signature=", 1)
    assert body == "a=1&b=x+y"
    assert sig == hmac.new(b"sec", body.encode(), hashlib.sha256).hexdigest()


def test_bybit_signatures_follow_the_documented_string_layout():
    expect = hmac.new(b"S", b"1658384314791" + b"KEY" + b"5000" + b'{"a":1}', hashlib.sha256).hexdigest()
    assert signing.bybit_rest_signature("S", "1658384314791", "KEY", "5000", '{"a":1}') == expect
    assert signing.bybit_ws_auth_signature("S", 1662350400000) == hmac.new(b"S", b"GET/realtime1662350400000", hashlib.sha256).hexdigest()


# ---- parsers -----------------------------------------------------------------------------------------------------
def test_bybit_order_topic_maps_status_and_both_exchange_times():
    msg = {"topic": "order", "creationTime": 1672364262474, "data": [
        {"category": "spot", "symbol": "SOLUSDT", "orderId": "1", "orderLinkId": "ollogA000001", "orderStatus": "New", "updatedTime": "1672364262444"},
        {"category": "linear", "symbol": "SOLUSDT", "orderId": "2", "orderLinkId": "x", "orderStatus": "New", "updatedTime": "1"}]}
    evs = parsers.parse_bybit_ws("bybit_spot", msg)
    assert len(evs) == 1 and evs[0].kind == "new" and evs[0].client_id == "ollogA000001"
    assert (evs[0].ex_event_ms, evs[0].ex_txn_ms) == (1672364262444, 1672364262474)
    cancelled = dict(msg, data=[{**msg["data"][0], "orderStatus": "Cancelled"}])
    rejected = dict(msg, data=[{**msg["data"][0], "orderStatus": "Rejected", "rejectReason": "EC_PostOnlyWillTakeLiquidity"}])
    assert parsers.parse_bybit_ws("bybit_spot", cancelled)[0].kind == "cancelled"
    r = parsers.parse_bybit_ws("bybit_spot", rejected)[0]
    assert r.kind == "rejected" and r.reason == "EC_PostOnlyWillTakeLiquidity"
    assert parsers.parse_bybit_ws("bybit_spot", {"op": "pong"}) == []


def test_binance_spot_and_futures_events_including_wrapped_form():
    spot = {"e": "executionReport", "E": 1499405658658, "s": "SOLUSDT", "c": "ollogA000001", "x": "CANCELED", "X": "CANCELED",
            "r": "NONE", "i": 4293153, "T": 1499405658657}
    ev = parsers.parse_binance_spot_ws("binance_spot", spot)[0]
    assert (ev.kind, ev.client_id, ev.order_id, ev.ex_event_ms, ev.ex_txn_ms, ev.reason) == ("cancelled", "ollogA000001", "4293153", 1499405658658, 1499405658657, "")
    assert parsers.parse_binance_spot_ws("binance_spot", {"event": spot})[0].kind == "cancelled"
    assert parsers.parse_binance_spot_ws("binance_spot", {"e": "outboundAccountPosition"}) == []
    fut = {"e": "ORDER_TRADE_UPDATE", "E": 1568879465651, "T": 1568879465650, "o": {"s": "SOLUSDT", "c": "ollogA000002", "x": "EXPIRED", "X": "EXPIRED", "i": 8886774}}
    fe = parsers.parse_binance_futures_ws("binance_futures", fut)[0]
    assert (fe.kind, fe.ex_event_ms, fe.ex_txn_ms) == ("expired", 1568879465651, 1568879465650)


def test_instrument_parsers_read_filters_and_band():
    b = parsers.parse_binance_instruments("binance_spot", {"symbols": [{
        "symbol": "SOLUSDT", "status": "TRADING", "quoteAsset": "USDT", "baseAsset": "SOL", "orderTypes": ["LIMIT", "LIMIT_MAKER"],
        "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"}, {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "NOTIONAL", "minNotional": "5"}, {"filterType": "PERCENT_PRICE_BY_SIDE", "bidMultiplierDown": "0.2"}]},
        {"symbol": "XUSDT", "status": "BREAK", "quoteAsset": "USDT", "orderTypes": ["LIMIT_MAKER"], "filters": []}]})
    assert list(b) == ["SOLUSDT"] and b["SOLUSDT"].min_notional == 5 and b["SOLUSDT"].pct_down == Decimal("0.2")
    y = parsers.parse_bybit_instruments("bybit_linear", {"result": {"list": [
        {"symbol": "SOLUSDT", "status": "Trading", "contractType": "LinearPerpetual", "quoteCoin": "USDT", "baseCoin": "SOL",
         "lotSizeFilter": {"qtyStep": "0.1", "minOrderQty": "0.1", "minNotionalValue": "5"}, "priceFilter": {"tickSize": "0.01"}},
        {"symbol": "SOLUSDT-26DEC", "status": "Trading", "contractType": "LinearFutures", "quoteCoin": "USDT",
         "lotSizeFilter": {"qtyStep": "1"}, "priceFilter": {"tickSize": "1"}}]}})
    assert list(y) == ["SOLUSDT"] and y["SOLUSDT"].step == Decimal("0.1")


# ---- pricing -----------------------------------------------------------------------------------------------------
def inst(**kw):
    base = dict(market="m", symbol="SOLUSDT", base="SOL", quote="USDT", tick=Decimal("0.01"), step=Decimal("0.001"),
                min_qty=Decimal("0.001"), min_notional=Decimal("5"), pct_down=None)
    return Instrument(**{**base, **kw})


def test_passive_price_is_below_the_bid_on_the_tick_grid():
    p = pricing.passive_buy_price(inst(), Decimal("100.47"), Decimal(50))
    assert p == Decimal("99.96") and p < Decimal("100.47")


def test_price_is_clamped_inside_the_lower_band_or_refused():
    i = inst(pct_down=Decimal("0.9"))
    p = pricing.passive_buy_price(i, Decimal("100"), Decimal(2000), ref=Decimal("100"))     # 20% below would leave the band
    assert p == Decimal("92.00")                                                           # floor = ref * (0.9 + 0.02)
    with pytest.raises(pricing.NoOrder):                        # band floor above the bid: cannot sit below the bid inside the band
        pricing.passive_buy_price(inst(pct_down=Decimal("0.99")), Decimal("90"), Decimal(50), ref=Decimal("100"))
    with pytest.raises(pricing.NoOrder):
        pricing.passive_buy_price(inst(), Decimal(0), Decimal(50))


def test_minimum_quantity_meets_notional_and_respects_the_cap():
    q = pricing.min_quantity(inst(), Decimal("99.96"), Decimal(100))
    assert q * Decimal("99.96") >= 5 and q % Decimal("0.001") == 0 and q == Decimal("0.053")
    with pytest.raises(pricing.NoOrder):
        pricing.min_quantity(inst(min_notional=Decimal(500)), Decimal("99.96"), Decimal(100))


def test_crossing_price_is_at_or_above_the_ask():
    assert pricing.crossing_buy_price(inst(), Decimal("100.485")) == Decimal("100.49")
    assert fmt(Decimal("0.05300000")) == "0.053" and fmt(Decimal("10")) == "10" and fmt(Decimal("1E-7")) == "0.0000001"


# ---- derived latency math ----------------------------------------------------------------------------------------
def test_derived_latencies_from_a_scripted_event_sequence():
    ms = 1_000_000
    rec = {"t": {"send_wall_ns": 1_000_000 * ms, "send_mono_ns": 5_000 * ms, "rest_mono_ns": 5_040 * ms,
                 "wsnew_wall_ns": 1_000_090 * ms, "wsnew_mono_ns": 5_090 * ms,
                 "csend_wall_ns": 1_002_000 * ms, "csend_mono_ns": 7_000 * ms, "crest_mono_ns": 7_030 * ms,
                 "wscan_wall_ns": 1_002_080 * ms, "wscan_mono_ns": 7_080 * ms},
           "ex": {"rest_ms": 1_000_050 + 100, "wsnew_event_ms": 1_000_060 + 100, "wscan_event_ms": 1_002_040 + 100},
           "clock": {"offset_ms": 100.0, "uncertainty_ms": 7.5}}
    d = derive(rec)
    loc, exr = d["local_ms"], d["exchange_referenced_ms"]
    assert loc["place_rest_rtt"] == 40 and loc["place_send_to_ws_new"] == 90 and loc["ws_new_after_rest_return"] == 50
    assert loc["cancel_rest_rtt"] == 30 and loc["cancel_send_to_ws_cancelled"] == 80 and loc["ws_cancelled_after_cancel_rest_return"] == 50
    assert loc["place_send_to_cancel_confirmed"] == 2080
    # server = local + 100: client send is 1_000_100 in server time, exchange stamped the new event at 1_000_160
    assert exr["place_send_to_exchange_new_event"] == 60 and exr["place_send_to_exchange_rest_time"] == 50
    assert exr["exchange_new_event_to_ws_recv"] == 1_000_190 - 1_000_160 == 30
    assert exr["cancel_send_to_exchange_cancel_event"] == 40 and exr["exchange_cancel_event_to_ws_recv"] == 1_002_180 - 1_002_140 == 40
    assert d["exchange_referenced_uncertainty_ms"] == 7.5


def test_missing_inputs_give_none_not_zero():
    d = derive({"t": {"send_mono_ns": 1}, "ex": {}, "clock": {}})
    assert all(v is None for v in d["local_ms"].values()) and all(v is None for v in d["exchange_referenced_ms"].values())


def test_quantiles_suppress_thin_percentiles():
    q = quantiles(list(range(1, 11)))
    assert q["n"] == 10 and q["p50"] is None and q["suppressed_below_n"]["p50"] == 30
    big = quantiles([float(i) for i in range(1, 201)])
    assert big["p50"] == 100.0 and big["p90"] == 180.0 and big["p99"] == 198.0


# ---- universe ----------------------------------------------------------------------------------------------------
def test_choose_keeps_only_coins_on_every_market_ranked_by_activity():
    tables = {"a": {"X": 1, "Y": 1, "Z": 1}, "b": {"X": 1, "Z": 1, "W": 1}}
    chosen, dropped = universe.choose(tables, {"X": 10, "Y": 99, "Z": 50, "W": 5}, 1)
    assert chosen == ["Z"] and "not tradable" in dropped["Y"][0] and "b" in dropped["Y"][0]
    assert dropped["X"] == ["eligible but beyond the requested count"]


# ---- request building and secrecy ---------------------------------------------------------------------------------
class _Resp:
    def __init__(self, status, text):
        self.status, self._text = status, text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self._text


class FakeSession:
    def __init__(self, status=200, text="{}", exc=None):
        self.calls, self.status, self.text, self.exc = [], status, text, exc

    def request(self, method, url, headers=None, data=None):
        self.calls.append((method, url, headers, data))
        if self.exc:
            raise self.exc
        return _Resp(self.status, self.text)


def test_bybit_place_is_a_signed_post_only_buy_with_json_body():
    s = FakeSession(text=json.dumps({"retCode": 0, "retMsg": "OK", "result": {"orderId": "9", "orderLinkId": "c"}, "time": 1700000000123}))
    c = venues.make_client("bybit_linear", s, "KEY", "SECRET")
    r = asyncio.run(c.place("SOLUSDT", Decimal("99.96"), Decimal("0.1"), "ollogA000001"))
    method, url, headers, body = s.calls[0]
    assert (method, url) == ("POST", "https://api-testnet.bybit.com/v5/order/create")
    assert json.loads(body) == {"category": "linear", "symbol": "SOLUSDT", "side": "Buy", "orderType": "Limit", "qty": "0.1",
                                "price": "99.96", "timeInForce": "PostOnly", "orderLinkId": "ollogA000001"}
    assert headers["X-BAPI-SIGN"] == signing.bybit_rest_signature("SECRET", headers["X-BAPI-TIMESTAMP"], "KEY", "5000", body)
    assert r.ok and r.order_id == "9" and r.ex_time_ms == 1700000000123 and r.t_ret_mono_ns >= r.t_send_mono_ns


def test_binance_place_uses_limit_maker_on_spot_and_gtx_on_futures():
    ok = json.dumps({"orderId": 77, "transactTime": 1700000000999})
    spot = venues.make_client("binance_spot", FakeSession(text=ok), "KEY", "SECRET")
    fut = venues.make_client("binance_futures", FakeSession(text=ok), "KEY", "SECRET")
    for c in (spot, fut):
        r = asyncio.run(c.place("SOLUSDT", Decimal("99.96"), Decimal("0.06"), "ollogA000001"))
        assert r.ok and r.order_id == "77"
    su, fu = spot.session.calls[0][1], fut.session.calls[0][1]
    assert su.startswith("https://testnet.binance.vision/api/v3/order?") and "type=LIMIT_MAKER" in su and "timeInForce" not in su
    assert fu.startswith("https://testnet.binancefuture.com/fapi/v1/order?") and "type=LIMIT" in fu and "timeInForce=GTX" in fu
    query, sig = su.split("?", 1)[1].rsplit("&signature=", 1)
    assert sig == signing.binance_signature("SECRET", query) and spot.session.calls[0][2] == {"X-MBX-APIKEY": "KEY"}


def test_errors_never_carry_the_signed_query_or_keys():
    s = FakeSession(exc=RuntimeError("cannot connect to https://testnet.binance.vision/api/v3/order?signature=DEADBEEF&timestamp=1"))
    c = venues.make_client("binance_spot", s, "KEYKEYKEY", "SECRETSECRET")
    with pytest.raises(venues.VenueError) as ei:
        asyncio.run(c.place("SOLUSDT", Decimal("1"), Decimal("1"), "c"))
    text = str(ei.value) + repr(ei.value.__cause__) + repr(ei.value.__context__)
    assert "DEADBEEF" not in text and "signature" not in text and "SECRETSECRET" not in text and "KEYKEYKEY" not in text
    assert "POST" in str(ei.value) and "/api/v3/order" in str(ei.value)


def test_bybit_stream_waits_for_auth_and_subscribe_success_before_returning():
    class WS:
        def __init__(self, replies):
            self.sent, self.replies = [], list(replies)

        async def send(self, m):
            self.sent.append(json.loads(m))

        async def recv(self):
            return json.dumps(self.replies.pop(0))

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(3600)

        async def close(self):
            pass

    async def go(replies):
        ws = WS(replies)

        async def connect(url):
            return ws
        c = venues.make_client("bybit_spot", FakeSession(), "KEY", "SECRET", ws_connect=connect)
        await c.start_stream(lambda e: None)
        alive = c.stream_alive
        await c.close()
        return ws, alive

    ws, alive = asyncio.run(go([{"op": "auth", "success": True}, {"op": "subscribe", "success": True}]))
    assert alive and [m["op"] for m in ws.sent] == ["auth", "subscribe"] and ws.sent[1]["args"] == ["order"]
    with pytest.raises(venues.VenueError):
        asyncio.run(go([{"op": "auth", "success": False, "ret_msg": "bad key"}]))


def test_buy_price_cap_is_read_only_from_a_170193_reply():
    from ollog.pricing import parse_buy_price_cap
    assert parse_buy_price_cap("170193", "Buy order price cannot be higher than 1.01USDT.") == Decimal("1.01")
    assert parse_buy_price_cap("170193", "Buy order price cannot be higher than 0.14434USDT.") == Decimal("0.14434")
    assert parse_buy_price_cap("170131", "Buy order price cannot be higher than 1.01USDT.") is None      # other code
    assert parse_buy_price_cap("170193", "Sell order price cannot be lower than 1.01USDT.") is None      # other wording
    assert parse_buy_price_cap("170193", "Buy order price cannot be higher than 0USDT.") is None
    assert parse_buy_price_cap("170193", "") is None


def test_passive_price_never_exceeds_a_learned_cap_and_stays_on_the_tick():
    i = inst()
    uncapped = pricing.passive_buy_price(i, Decimal("100.47"), Decimal(50))
    assert pricing.passive_buy_price(i, Decimal("100.47"), Decimal(50), cap=Decimal("200")) == uncapped     # a loose cap changes nothing
    p = pricing.passive_buy_price(i, Decimal("100.47"), Decimal(50), cap=Decimal("90.019"))
    assert p == Decimal("90.01") and p < Decimal("100.47")
    with pytest.raises(pricing.NoOrder):
        pricing.passive_buy_price(i, Decimal("100.47"), Decimal(50), cap=Decimal("0.001"))                  # rounds to a zero price


def test_post_only_reject_classifier_per_venue():
    f = pricing.is_post_only_reject
    assert f("bybit_spot", "0", "", "EC_PostOnlyWillTakeLiquidity", "rejected")
    assert f("binance_spot", "-2010", "Order would immediately match and take.")
    assert f("binance_futures", "-5022", "Due to the order could not be executed as maker, the Post Only order will be rejected.")
    assert f("binance_futures", "", "", "", "expired")
    assert not f("binance_spot", "", "", "", "expired")
    assert not f("bybit_spot", "170193", "Buy order price cannot be higher than 0.11USDT.")
    assert not f("bybit_linear", "110094", "Order does not meet minimum order value 5USDT")
    assert not f("bybit_spot", "10005", "Permission denied")


def test_binance_ws_api_signature_matches_the_documented_hmac_example():
    p = dict(apiKey="vmPUZE6mv9SD5VNHk4HlWFsOr6aKE2zvsw0MuIgwCIPy6utIco14y7Ju91duEh8A", symbol="BTCUSDT", side="SELL", type="LIMIT",
             timeInForce="GTC", quantity="0.01000000", price="52000.00", recvWindow=100, timestamp=1645423376532)
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    sig = "aa1b5712c094bc4e57c05a1a5c1fd8d88dcd628338ea863fec7b88e59fe2db24"
    assert signing.binance_ws_api_signature(secret, p) == sig
    assert signing.binance_ws_api_signature(secret, {**p, "signature": "ignored"}) == sig          # an existing signature is not signed


class _SpotWS:
    def __init__(self, reply, events=()):
        self.sent, self.reply, self.events, self.closed = [], reply, list(events), False

    async def send(self, m):
        self.sent.append(json.loads(m))

    async def recv(self):
        r = dict(self.reply)
        r["id"] = self.sent[-1]["id"]
        return json.dumps(r)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.events:
            return json.dumps(self.events.pop(0))
        await asyncio.sleep(3600)

    async def close(self):
        self.closed = True


def _spot_stream(reply, events=()):
    ws = _SpotWS(reply, events)
    urls, got = [], []

    async def connect(url):
        urls.append(url)
        return ws

    async def go():
        c = venues.make_client("binance_spot", FakeSession(), "KEY", "SECRET", ws_connect=connect)
        await c.start_stream(got.append)
        alive = c.stream_alive
        await asyncio.sleep(0.05)
        await c.close()
        return alive
    return ws, urls, got, go


def test_binance_spot_stream_subscribes_with_a_signed_request_and_reads_wrapped_events():
    ev = {"subscriptionId": 0, "event": {"e": "executionReport", "x": "NEW", "X": "NEW", "s": "SOLUSDT", "i": 7, "c": "ollogt1", "E": 5, "T": 4}}
    ws, urls, got, go = _spot_stream({"status": 200, "result": {"subscriptionId": 0}}, [{"subscriptionId": 0, "event": {"e": "balanceUpdate"}}, ev])
    assert asyncio.run(go()) is True
    assert urls == ["wss://ws-api.testnet.binance.vision/ws-api/v3"]
    (req,) = ws.sent
    assert req["method"] == "userDataStream.subscribe.signature"
    prm = dict(req["params"])
    assert prm["apiKey"] == "KEY" and set(prm) == {"apiKey", "timestamp", "recvWindow", "signature"}
    assert prm["signature"] == signing.binance_ws_api_signature("SECRET", prm) and "SECRET" not in json.dumps(req)
    assert [(e.kind, e.client_id) for e in got] == [("new", "ollogt1")]                 # the balance event is ignored


def test_binance_spot_stream_refusal_raises_and_never_reports_alive():
    ws, urls, got, go = _spot_stream({"status": 401, "error": {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}})
    with pytest.raises(venues.VenueError) as ei:
        asyncio.run(go())
    assert "-2015" in str(ei.value) and "SECRET" not in str(ei.value) and "KEY" not in str(ei.value).replace("-2015", "")


def test_binance_spot_never_calls_the_retired_listen_key_endpoint():
    ws = _SpotWS({"status": 200, "result": {"subscriptionId": 0}})

    async def connect(u):
        return ws

    async def go():
        c = venues.make_client("binance_spot", FakeSession(), "KEY", "SECRET", ws_connect=connect)
        await c.start_stream(lambda e: None)
        await c.close()
        return c.session.calls
    assert asyncio.run(go()) == []


def test_binance_spot_cancel_event_is_matched_by_the_original_client_id_not_the_cancel_request_id():
    cancel = {"e": "executionReport", "E": 5, "s": "SOLUSDT", "c": "qpPrwLjxDXPGwqTc6BhYfk", "C": "ollogA000001", "x": "CANCELED",
              "X": "CANCELED", "r": "NONE", "i": 1567872, "T": 4}
    assert parsers.parse_binance_spot_ws("binance_spot", {"subscriptionId": 0, "event": cancel})[0].client_id == "ollogA000001"
    new = {**cancel, "x": "NEW", "X": "NEW", "c": "ollogA000001", "C": ""}
    assert parsers.parse_binance_spot_ws("binance_spot", new)[0].client_id == "ollogA000001"
    assert parsers.parse_binance_spot_ws("binance_spot", {**cancel, "C": "null"})[0].client_id == "qpPrwLjxDXPGwqTc6BhYfk"
