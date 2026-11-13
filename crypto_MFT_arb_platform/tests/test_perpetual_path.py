"""Perpetual futures path: derivatives stream, futures testnet transports,
realised funding history and a perpetual session that settles funding at the exchange mark."""

from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

from crypto_stat_arb.config import load_config
from crypto_stat_arb.derivatives import (
    BybitTickerMerger, DerivativeTicker, normalize_binance_mark_price, validate_derivative_ticker,
)
from crypto_stat_arb.funding_history import fetch_binance, fetch_bybit
from crypto_stat_arb.live_feed import LiveFeedSettings, LiveOrderBookService
from crypto_stat_arb.paper_live import perpetual_feed_settings
from crypto_stat_arb.runner import run_backtest
from crypto_stat_arb.testnet import (
    BinanceFuturesTestnetTransport, BybitV5LinearTestnetTransport, markets_for,
)
from test_forward_testnet_calibration import FakeHttp, SECRET, canonical_submit


ROOT = Path(__file__).parents[1]
PERP = "BTC-USDT-PERP"
PERP_CONFIG = ROOT / "config/perp_research.json"
HOUR_MS = 3_600_000


def perp_request(**overrides):
    return {**canonical_submit(), "instrument_id": PERP, **overrides}


class DerivativeNormalizationTests(unittest.TestCase):
    def test_binance_mark_price_frame(self):
        frame = {"e": "markPriceUpdate", "E": 1_700_000_000_000, "s": "BTCUSDT", "p": "60010.5",
                 "i": "60008.1", "P": "60011.0", "r": "0.00010000", "T": 1_700_006_400_000}
        ticker = normalize_binance_mark_price(frame, 1_700_000_000_005_000_000, PERP, "raw#1")
        self.assertEqual((ticker.mark_price, ticker.index_price, ticker.funding_rate),
                         (Decimal("60010.5"), Decimal("60008.1"), Decimal("0.0001")))
        self.assertEqual(ticker.next_funding_ts_ns, 1_700_006_400_000 * 1_000_000)
        validate_derivative_ticker(ticker)
        # A delivery contract: empty rate and zero next funding time.
        delivery = normalize_binance_mark_price({**frame, "r": "", "T": 0}, 1, PERP, "raw#2")
        self.assertEqual((delivery.funding_rate, delivery.next_funding_ts_ns), (None, None))

    def test_bybit_delta_keeps_omitted_fields(self):
        merger = BybitTickerMerger()
        snapshot = {"topic": "tickers.BTCUSDT", "type": "snapshot", "ts": 1000, "cs": 7, "data": {
            "symbol": "BTCUSDT", "markPrice": "60000", "indexPrice": "59990", "fundingRate": "0.0001",
            "nextFundingTime": "28800000", "fundingIntervalHour": "8"}}
        first = merger.normalize(snapshot, 1, PERP, "raw#1")
        delta = {"topic": "tickers.BTCUSDT", "type": "delta", "ts": 2000, "cs": 8,
                 "data": {"symbol": "BTCUSDT", "fundingRate": "0.0002"}}        # mark omitted: unchanged
        second = merger.normalize(delta, 2, PERP, "raw#2")
        self.assertEqual((second.mark_price, second.funding_rate, second.funding_interval_hours),
                         (Decimal("60000"), Decimal("0.0002"), Decimal("8")))
        self.assertEqual(second.quality_flags, ("MERGED_DELTA",))
        self.assertEqual(first.mark_price, second.mark_price)
        merger.reset()
        with self.assertRaisesRegex(ValueError, "delta before a snapshot"):
            merger.normalize(delta, 3, PERP, "raw#3")

    def test_invalid_tickers_are_refused(self):
        frame = {"e": "markPriceUpdate", "E": 1, "s": "BTCUSDT", "p": "0", "i": "1", "r": "0.0001", "T": 5}
        with self.assertRaisesRegex(ValueError, "mark_price must be positive"):
            validate_derivative_ticker(normalize_binance_mark_price(frame, 1, PERP, "raw"))

    def test_round_trip(self):
        ticker = normalize_binance_mark_price(
            {"e": "markPriceUpdate", "E": 5, "s": "BTCUSDT", "p": "1", "i": "1", "r": "0.0001", "T": 9},
            7, PERP, "raw")
        self.assertEqual(DerivativeTicker.from_dict(json.loads(json.dumps(ticker.to_dict()))), ticker)


class LiveDerivativeCaptureTests(unittest.TestCase):
    def test_perpetual_configs_need_linear_feeds_and_get_the_derivatives_stream(self):
        cfg, _ = load_config(PERP_CONFIG)
        with self.assertRaisesRegex(ValueError, "need linear market feeds"):
            perpetual_feed_settings(cfg, LiveFeedSettings())
        settings = perpetual_feed_settings(cfg, LiveFeedSettings(binance_market="linear", bybit_market="linear"))
        self.assertTrue(settings.derivative_streams_enabled)
        spot, _ = load_config(ROOT / "config/research.json")
        self.assertFalse(perpetual_feed_settings(spot, LiveFeedSettings()).derivative_streams_enabled)
        with self.assertRaisesRegex(ValueError, "linear"):
            LiveFeedSettings(derivative_streams_enabled=True).validate()

    def test_frames_are_captured_merged_recorded_and_traced(self):
        seen = []
        with tempfile.TemporaryDirectory() as directory:
            service = LiveOrderBookService(
                PERP_CONFIG, Path(directory), "perp-capture",
                LiveFeedSettings(binance_market="linear", bybit_market="linear",
                                 derivative_streams_enabled=True, durable=False),
                derivative_listener=seen.append,
            )
            url = "wss://stream.bybit.com/v5/public/linear"
            service._process_derivative_frame("BYBIT", url, json.dumps({
                "topic": "tickers.BTCUSDT", "type": "snapshot", "ts": 1000, "cs": 1,
                "data": {"symbol": "BTCUSDT", "markPrice": "60000", "indexPrice": "59990",
                         "fundingRate": "0.0001", "nextFundingTime": "28800000"}}), channel="tickers.BTCUSDT")
            service._process_derivative_frame("BYBIT", url, json.dumps({
                "topic": "tickers.BTCUSDT", "type": "delta", "ts": 2000, "cs": 2,
                "data": {"symbol": "BTCUSDT", "markPrice": "60005"}}), channel="tickers.BTCUSDT")
            service._process_derivative_frame("BINANCE", "wss://fstream.binance.com/ws/btcusdt@markPrice@1s",
                                              json.dumps({"e": "markPriceUpdate", "E": 3000, "s": "BTCUSDT",
                                                          "p": "60001", "i": "60000", "r": "0.0002",
                                                          "T": 28_800_000}), channel="markPrice")
            service._process_derivative_frame("BINANCE", "wss://x", "not json", channel="markPrice")
            part = service.recorder._part_dir
            rows = [json.loads(line) for line in (part / "derivatives.jsonl").read_text().splitlines()]
            trace = [json.loads(line) for line in (part / "trace.jsonl").read_text().splitlines()]
            service.recorder.close({})
            service.ohlc_writer.close()
            service.ltp_writer.close()
        self.assertEqual([t.venue for t in seen], ["BYBIT", "BYBIT", "BINANCE"])
        self.assertEqual(seen[1].funding_rate, Decimal("0.0001"))           # kept from the snapshot
        self.assertEqual([r["mark_price"] for r in rows], ["60000", "60005", "60001"])
        self.assertEqual([t.arrival_ordinal for t in seen], [1, 2, 3])
        self.assertEqual(sum(1 for t in trace if t.get("disposition") == "DERIVATIVE"), 3)


    def test_binance_futures_routes_and_non_market_trades(self):
        with tempfile.TemporaryDirectory() as directory:
            service = LiveOrderBookService(
                PERP_CONFIG, Path(directory), "perp-routes",
                LiveFeedSettings(binance_market="linear", bybit_market="linear",
                                 derivative_streams_enabled=True, trade_streams_enabled=True, durable=False),
            )
            # markPrice is only served on /market; depth and trades stay on /ws.
            self.assertEqual(service._binance_mark_price_url(),
                             "wss://fstream.binance.com/market/ws/btcusdt@markPrice@1s")
            self.assertTrue(service._binance_urls()[0].startswith("wss://fstream.binance.com/ws/"))
            url = service._binance_trade_url()
            na = {"e": "trade", "E": 1, "T": 1, "s": "BTCUSDT", "t": 7, "p": "0", "q": "0", "X": "NA", "m": False}
            self.assertEqual(service._process_trade_frame("BINANCE", url, json.dumps(na), channel="trade"), 0)
            self.assertEqual(service.stats["BINANCE"].control_frames, 1)
            market = dict(na, t=8, p="60000", q="0.01", X="MARKET")
            self.assertEqual(service._process_trade_frame("BINANCE", url, json.dumps(market), channel="trade"), 1)
            part = service.recorder._part_dir
            quarantined = (part / "quarantine.jsonl").exists() and (part / "quarantine.jsonl").read_text().strip()
            service.recorder.close({})
            service.ohlc_writer.close()
            service.ltp_writer.close()
        self.assertFalse(quarantined)


class FuturesTestnetTransportTests(unittest.TestCase):
    def test_binance_futures_submit_reduce_only_post_only_and_fill_liquidity(self):
        http = FakeHttp([
            (200, {"orderId": 9, "status": "FILLED", "executedQty": "0.010", "avgPrice": "60000",
                   "cumQuote": "600", "updateTime": 1_700_000_000_003}),
            (200, [{"maker": False, "commission": "0.3", "commissionAsset": "USDT", "qty": "0.010"}]),
        ])
        transport = BinanceFuturesTestnetTransport(symbols={PERP: "BTCUSDT"}, http=http,
                                                   clock_ms=lambda: 1_700_000_000_000)
        events = transport(perp_request(reduce_only=True), SECRET)
        self.assertEqual([e["event_type"] for e in events], ["ACKNOWLEDGED", "FILLED"])
        self.assertEqual((events[1]["average_price"], events[1]["liquidity"]), ("60000", "TAKER"))
        self.assertEqual(events[1]["venue_commission"][0]["commission"], "0.3")
        url = http.calls[0][1]
        self.assertEqual((urlparse(url).hostname, urlparse(url).path),
                         ("testnet.binancefuture.com", "/fapi/v1/order"))
        params = parse_qs(urlparse(url).query.rsplit("&signature=", 1)[0])
        self.assertEqual((params["reduceOnly"], params["timeInForce"]), (["true"], ["IOC"]))
        self.assertEqual(urlparse(http.calls[1][1]).path, "/fapi/v1/userTrades")
        post_only = FakeHttp([(200, {"orderId": 10, "status": "NEW", "executedQty": "0"})])
        BinanceFuturesTestnetTransport(symbols={PERP: "BTCUSDT"}, http=post_only)(
            perp_request(post_only=True, time_in_force="GTC"), SECRET)
        self.assertEqual(parse_qs(urlparse(post_only.calls[0][1]).query)["timeInForce"], ["GTX"])

    def test_binance_futures_account_must_be_one_way(self):
        hedge = FakeHttp([(200, {"dualSidePosition": True})])
        with self.assertRaisesRegex(ValueError, "hedge mode"):
            BinanceFuturesTestnetTransport(symbols={PERP: "BTCUSDT"}, http=hedge)(
                {"action": "VERIFY_ACCOUNT", "instrument_id": PERP}, SECRET)
        one_way = FakeHttp([(200, {"dualSidePosition": False})])
        (result,) = BinanceFuturesTestnetTransport(symbols={PERP: "BTCUSDT"}, http=one_way)(
            {"action": "VERIFY_ACCOUNT", "instrument_id": PERP}, SECRET)
        self.assertEqual(result["position_mode"], "ONE_WAY")
        self.assertEqual(urlparse(one_way.calls[0][1]).path, "/fapi/v1/positionSide/dual")

    def test_bybit_linear_order_fields_maker_flag_and_account_checks(self):
        http = FakeHttp([
            (200, {"retCode": 0, "result": {"orderId": "b1"}}),
            (200, {"retCode": 0, "result": {"list": [{"orderId": "b1", "orderStatus": "Filled",
                                                       "cumExecQty": "0.010", "avgPrice": "60060",
                                                       "updatedTime": "1700000000002"}]}}),
            (200, {"retCode": 0, "result": {"list": [{"isMaker": True, "execFee": "0.12",
                                                       "feeCurrency": "USDT", "execQty": "0.010"}]}}),
        ])
        transport = BybitV5LinearTestnetTransport(symbols={PERP: "BTCUSDT"}, http=http,
                                                  clock_ms=lambda: 1_700_000_000_000)
        events = transport(perp_request(venue="BYBIT", reduce_only=True), SECRET)
        body = json.loads(http.calls[0][3])
        self.assertEqual((body["category"], body["positionIdx"], body["reduceOnly"]), ("linear", 0, True))
        filled = [e for e in events if e["event_type"] == "FILLED"][0]
        self.assertEqual(filled["liquidity"], "MAKER")
        self.assertIn("/v5/execution/list", http.calls[2][1])
        wrong_mode = FakeHttp([(200, {"retCode": 0, "result": {"marginMode": "ISOLATED_MARGIN"}})])
        with self.assertRaisesRegex(ValueError, "margin mode"):
            BybitV5LinearTestnetTransport(symbols={PERP: "BTCUSDT"}, http=wrong_mode,
                                          expected_margin_mode="CROSS")(
                {"action": "VERIFY_ACCOUNT", "instrument_id": PERP}, SECRET)
        hedge = FakeHttp([(200, {"retCode": 0, "result": {"marginMode": "REGULAR_MARGIN"}}),
                          (200, {"retCode": 0, "result": {"list": [{"positionIdx": 1}, {"positionIdx": 2}]}})])
        with self.assertRaisesRegex(ValueError, "hedge"):
            BybitV5LinearTestnetTransport(symbols={PERP: "BTCUSDT"}, http=hedge, expected_margin_mode="CROSS")(
                {"action": "VERIFY_ACCOUNT", "instrument_id": PERP}, SECRET)

    def test_markets_follow_the_instruments_and_production_is_refused(self):
        cfg, _ = load_config(PERP_CONFIG)
        self.assertEqual(markets_for(cfg), {"BINANCE": "linear", "BYBIT": "linear"})
        spot, _ = load_config(ROOT / "config/research.json")
        self.assertEqual(markets_for(spot), {"BINANCE": "spot", "BYBIT": "spot"})
        with self.assertRaisesRegex(ValueError, "non-testnet"):
            BinanceFuturesTestnetTransport(base_url="https://fapi.binance.com", symbols={PERP: "BTCUSDT"})


class AccountSnapshotTests(unittest.TestCase):
    """Owner 13 Nov: the perpetual transports' `account_snapshot` (orders open and closed keyed by the CLIENT order id, executions since a timestamp, positions), through `TestnetVenueAdapter.account_snapshot`.
    The messages are written by hand in each venue's documented response shape (no account responses, no real ids); there is no network."""
    NOW_MS = 1_700_000_100_000
    SINCE_NS = 1_700_000_000_000 * 1_000_000

    def adapter(self, transport):
        from crypto_stat_arb.sdk import TestnetVenueAdapter
        return TestnetVenueAdapter(transport.venue, lambda req: transport(req, SECRET))

    def binance(self, responses, **kw):
        http = FakeHttp(responses)
        return http, BinanceFuturesTestnetTransport(symbols={PERP: "BTCUSDT"}, http=http, clock_ms=lambda: self.NOW_MS, **kw)

    def bybit(self, responses, **kw):
        http = FakeHttp(responses)
        return http, BybitV5LinearTestnetTransport(symbols={PERP: "BTCUSDT"}, http=http, clock_ms=lambda: self.NOW_MS, **kw)

    @staticmethod
    def order(oid, client, status, executed="0", average="0", t=1_700_000_050_000):
        return {"orderId": oid, "clientOrderId": client, "symbol": "BTCUSDT", "status": status, "executedQty": executed, "avgPrice": average, "cumQuote": "0", "updateTime": t, "time": t - 1000}

    def test_binance_orders_are_keyed_by_the_client_id_executions_follow_their_order_and_the_position_is_signed(self):
        http, transport = self.binance([
            (200, [self.order(11, "c-open", "NEW")]),                                                       # openOrders
            (200, [self.order(11, "c-open", "NEW"), self.order(12, "c-filled", "FILLED", "0.010", "60060.1"),
                   self.order(13, "c-cancel", "CANCELED", "0.004", "60060.0")]),                            # allOrders (the open order is in both lists: one order)
            (200, [{"id": 701, "orderId": 12, "price": "60060.1", "qty": "0.006", "commission": "0.0144", "commissionAsset": "USDT", "time": 1_700_000_040_000, "maker": True},
                   {"id": 702, "orderId": 12, "price": "60060.1", "qty": "0.004", "commission": "0.0096", "commissionAsset": "USDT", "time": 1_700_000_041_000, "maker": True},
                   {"id": 703, "orderId": 13, "price": "60060.0", "qty": "0.004", "commission": "0.0096", "commissionAsset": "USDT", "time": 1_700_000_042_000, "maker": True},
                   {"id": 704, "orderId": 99, "price": "60000.0", "qty": "0.001", "commission": "0.0024", "commissionAsset": "USDT", "time": 1_700_000_043_000, "maker": False}]),   # userTrades
            (200, {"orderId": 99, "clientOrderId": "c-older", "symbol": "BTCUSDT", "status": "FILLED", "executedQty": "0.001"}),      # an older order the trade belongs to, asked for by its venue id
            (200, [{"symbol": "BTCUSDT", "positionAmt": "-0.010", "positionSide": "BOTH", "entryPrice": "60060.1"}]),                  # positionRisk
        ])
        snap = self.adapter(transport).account_snapshot(self.SINCE_NS)
        paths = [urlparse(c[1]).path for c in http.calls]
        self.assertEqual(paths, ["/fapi/v1/openOrders", "/fapi/v1/allOrders", "/fapi/v1/userTrades", "/fapi/v1/order", "/fapi/v2/positionRisk"])
        self.assertEqual({k: v[0] for k, v in parse_qs(urlparse(http.calls[1][1]).query).items() if k in ("startTime", "endTime", "limit")},
                         {"startTime": "1700000000000", "endTime": str(self.NOW_MS), "limit": "1000"})
        self.assertEqual(sorted(o.order_id for o in snap.orders), ["c-cancel", "c-filled", "c-open"])           # the client id, never the venue's number
        by = {o.order_id: o for o in snap.orders}
        self.assertEqual((by["c-open"].state.value, by["c-filled"].state.value, by["c-cancel"].state.value), ("ACKNOWLEDGED", "FILLED", "CANCELLED"))
        self.assertEqual((by["c-filled"].filled_quantity, by["c-filled"].average_price, by["c-filled"].venue_order_id), (Decimal("0.010"), Decimal("60060.1"), "12"))
        self.assertEqual(by["c-filled"].fee, Decimal("0.0240"))                                              # the commissions of its two trades
        self.assertEqual(by["c-cancel"].filled_quantity, Decimal("0.004"))                                   # a partial fill then a cancel keeps its fill
        self.assertEqual([(e.execution_id, e.order_id, e.quantity) for e in snap.executions],
                         [("701", "c-filled", Decimal("0.006")), ("702", "c-filled", Decimal("0.004")), ("703", "c-cancel", Decimal("0.004")), ("704", "c-older", Decimal("0.001"))])
        self.assertEqual(snap.positions, {PERP: Decimal("-0.010")})                                          # signed: short
        self.assertEqual(sum(e.quantity for e in snap.executions if e.order_id == "c-filled"), by["c-filled"].filled_quantity)

    def test_binance_a_trade_of_an_order_the_venue_will_not_show_is_flagged_not_guessed(self):
        http, transport = self.binance([
            (200, []), (200, []),
            (200, [{"id": 5, "orderId": 77, "price": "60000", "qty": "0.002", "commission": "0", "commissionAsset": "USDT", "time": 1_700_000_040_000, "maker": True}]),
            (400, {"code": -2013, "msg": "Order does not exist."}),
            (200, [{"symbol": "BTCUSDT", "positionAmt": "0.000", "positionSide": "BOTH"}]),
        ])
        snap = self.adapter(transport).account_snapshot(self.SINCE_NS)
        self.assertEqual([e.order_id for e in snap.executions], ["venue-order:77"])                          # the OMS reads this as an execution for an unknown order and keeps its gate closed
        self.assertEqual(snap.positions, {PERP: Decimal("0")})

    def test_binance_windows_are_under_seven_days_and_trade_pages_move_forward_without_double_counting(self):
        since_ns = (self.NOW_MS - 13 * 86_400_000) * 1_000_000
        trades1 = [{"id": i, "orderId": 1, "price": "1", "qty": "1", "commission": "0", "commissionAsset": "USDT", "time": 1_699_000_000_000 + i, "maker": True} for i in range(1, 4)]
        http, transport = self.binance([])
        transport.TRADES_PAGE = 3                                                                            # a full page: the next request starts at the last trade's time and the overlap is counted once
        trades2 = [trades1[-1], {"id": 4, "orderId": 1, "price": "1", "qty": "1", "commission": "0", "commissionAsset": "USDT", "time": 1_699_000_000_004, "maker": True}]
        http.responses = [(200, [])] + [(200, [])] * 3 + [(200, trades1), (200, trades2), (200, []), (200, []),
                                                           (200, {"orderId": 1, "clientOrderId": "c1", "symbol": "BTCUSDT"}), (200, [{"symbol": "BTCUSDT", "positionAmt": "0", "positionSide": "BOTH"}])]
        # script: openOrders, allOrders x3 (3 windows), userTrades window 1 (full page) and its next page, userTrades windows 2 and 3 (empty), the trades' order by id, positionRisk
        snap = self.adapter(transport).account_snapshot(since_ns)
        calls = [urlparse(c[1]) for c in http.calls]
        windows = [(int(parse_qs(u.query)["startTime"][0]), int(parse_qs(u.query)["endTime"][0])) for u in calls if u.path == "/fapi/v1/allOrders"]
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0][0], since_ns // 1_000_000)
        self.assertEqual(windows[-1][1], self.NOW_MS)
        for (a, b), (c, _) in zip(windows, windows[1:]):
            self.assertLess(b - a, 7 * 86_400_000)
            self.assertEqual(c, b + 1)                                                                       # contiguous: nothing falls between two windows
        self.assertEqual([e.execution_id for e in snap.executions], ["1", "2", "3", "4"])                    # the repeated trade 3 is one execution

    def test_binance_a_full_page_of_orders_raises_instead_of_truncating(self):
        full = [self.order(i, f"c{i}", "FILLED", "0.001", "60000") for i in range(1, 1001)]
        _, t = self.binance([(200, []), (200, full)])
        with self.assertRaisesRegex(ConnectionError, "full page"):
            self.adapter(t).account_snapshot(self.SINCE_NS)

    def test_binance_failures_close_the_gate_instead_of_returning_a_partial_account(self):
        _, t = self.binance([(500, {"msg": "down"})])
        with self.assertRaises(ConnectionError):
            self.adapter(t).account_snapshot(self.SINCE_NS)
        _, t = self.binance([(200, [self.order(1, "c", "SOMETHING_NEW")]), (200, []), (200, [])])
        with self.assertRaisesRegex(ConnectionError, "unmapped"):
            self.adapter(t).account_snapshot(self.SINCE_NS)
        _, t = self.binance([(200, []), (200, []), (200, []), (200, [{"symbol": "BTCUSDT", "positionAmt": "0.01", "positionSide": "LONG"}])])
        with self.assertRaisesRegex(ValueError, "hedge mode"):
            self.adapter(t).account_snapshot(self.SINCE_NS)
        _, t = self.binance([])
        with self.assertRaisesRegex(ValueError, "older than the venue keeps"):
            self.adapter(t).account_snapshot((self.NOW_MS - 91 * 86_400_000) * 1_000_000)

    def test_bybit_orders_executions_and_position_with_cursor_paging(self):
        row = lambda link, oid, status, qty="0", avg="0", t="1700000050000": {"orderLinkId": link, "orderId": oid, "orderStatus": status, "cumExecQty": qty, "avgPrice": avg, "cumExecFee": "0.01", "updatedTime": t, "createdTime": "1700000040000"}
        ex = lambda eid, oid, link, qty, typ="Trade": {"execId": eid, "orderId": oid, "orderLinkId": link, "execPrice": "60050.5", "execQty": qty, "execFee": "0.0072", "execTime": "1700000045000", "isMaker": True, "execType": typ}
        http, transport = self.bybit([
            (200, {"retCode": 0, "result": {"list": [row("c-open", "b1", "New")], "nextPageCursor": "o2"}}),          # order/realtime page 1
            (200, {"retCode": 0, "result": {"list": [row("c-part", "b2", "PartiallyFilled", "0.004", "60050.5")], "nextPageCursor": ""}}),   # page 2
            (200, {"retCode": 0, "result": {"list": [row("c-filled", "b3", "Filled", "0.010", "60050.5"), row("c-gone", "b4", "Cancelled"),
                                                       row("c-part", "b2", "PartiallyFilled", "0.004", "60050.5", t="1700000040000")], "nextPageCursor": ""}}),   # order/history (an older copy of c-part: the later report wins)
            (200, {"retCode": 0, "result": {"list": [ex("e1", "b3", "c-filled", "0.006"), ex("e2", "b3", "c-filled", "0.004")], "nextPageCursor": "x2"}}),   # execution/list page 1
            (200, {"retCode": 0, "result": {"list": [ex("e3", "b2", "c-part", "0.004"), ex("f1", "", "", "0", "Funding"), ex("e2", "b3", "c-filled", "0.004")], "nextPageCursor": ""}}),   # page 2: a funding row, a repeated e2
            (200, {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "side": "Sell", "size": "0.014", "positionIdx": 0, "avgPrice": "60050.5"}], "nextPageCursor": ""}}),
        ])
        snap = self.adapter(transport).account_snapshot(self.SINCE_NS)
        calls = [(urlparse(c[1]).path, parse_qs(urlparse(c[1]).query)) for c in http.calls]
        self.assertEqual([p for p, _ in calls], ["/v5/order/realtime", "/v5/order/realtime", "/v5/order/history", "/v5/execution/list", "/v5/execution/list", "/v5/position/list"])
        self.assertEqual(calls[1][1]["cursor"], ["o2"])                                                      # the page cursor is passed back
        self.assertEqual((calls[2][1]["startTime"], calls[2][1]["endTime"], calls[2][1]["limit"]), (["1700000000000"], [str(self.NOW_MS)], ["50"]))
        self.assertEqual(calls[3][1]["limit"], ["100"])
        by = {o.order_id: o for o in snap.orders}
        self.assertEqual(sorted(by), ["c-filled", "c-gone", "c-open", "c-part"])                              # keyed by orderLinkId
        self.assertEqual((by["c-open"].state.value, by["c-part"].state.value, by["c-filled"].state.value, by["c-gone"].state.value), ("ACKNOWLEDGED", "PARTIAL", "FILLED", "CANCELLED"))
        self.assertEqual(by["c-part"].observed_ts_ns, 1_700_000_050_000 * 1_000_000)                          # the later report of c-part won
        self.assertEqual(sorted((e.execution_id, e.order_id) for e in snap.executions), [("e1", "c-filled"), ("e2", "c-filled"), ("e3", "c-part")])      # the funding row is not a fill, e2 counts once
        self.assertEqual(snap.positions, {PERP: Decimal("-0.014")})                                           # Sell 0.014 is -0.014
        self.assertEqual(sum(e.quantity for e in snap.executions if e.order_id == "c-filled"), by["c-filled"].filled_quantity)
        self.assertTrue(all(c[2].get("X-BAPI-API-KEY") == "test-key" for c in http.calls))                    # read-only GETs, signed

    def test_bybit_an_order_without_a_client_id_is_flagged_and_failures_close_the_gate(self):
        row = {"orderLinkId": "", "orderId": "b9", "orderStatus": "Filled", "cumExecQty": "0.001", "avgPrice": "60000", "updatedTime": "1700000050000"}
        _, t = self.bybit([(200, {"retCode": 0, "result": {"list": [row]}}), (200, {"retCode": 0, "result": {"list": []}}), (200, {"retCode": 0, "result": {"list": []}}),
                           (200, {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "side": "", "size": "0", "positionIdx": 0}]}})])
        snap = self.adapter(t).account_snapshot(self.SINCE_NS)
        self.assertEqual([o.order_id for o in snap.orders], ["venue-order:b9"])                               # not ours: the OMS flags it and never cancels it
        self.assertEqual(snap.positions, {PERP: Decimal("0")})
        _, t = self.bybit([(200, {"retCode": 10001, "retMsg": "params error"})])
        with self.assertRaisesRegex(ConnectionError, "params error"):
            self.adapter(t).account_snapshot(self.SINCE_NS)
        _, t = self.bybit([(200, {"retCode": 0, "result": {"list": [dict(row, orderStatus="Weird")]}}), (200, {"retCode": 0, "result": {"list": []}})])
        with self.assertRaisesRegex(ConnectionError, "unmapped"):
            self.adapter(t).account_snapshot(self.SINCE_NS)
        _, t = self.bybit([(200, {"retCode": 0, "result": {"list": []}})] * 3 + [(200, {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "side": "Buy", "size": "0.01", "positionIdx": 1}]}})])
        with self.assertRaisesRegex(ValueError, "hedge"):
            self.adapter(t).account_snapshot(self.SINCE_NS)

    def test_a_cursor_that_never_ends_raises_instead_of_looping(self):
        calls = []
        def http(method, url, headers, body, timeout):
            calls.append(url)
            return 200, json.dumps({"retCode": 0, "result": {"list": [], "nextPageCursor": "again"}})
        transport = BybitV5LinearTestnetTransport(symbols={PERP: "BTCUSDT"}, http=http, clock_ms=lambda: self.NOW_MS)
        transport.SNAPSHOT_MAX_PAGES = 4
        with self.assertRaisesRegex(ConnectionError, "did not end"):
            self.adapter(transport).account_snapshot(self.SINCE_NS)
        self.assertEqual(len(calls), 4)

    def test_the_snapshot_read_asks_for_the_read_permission_never_the_trade_one(self):
        from crypto_stat_arb.credentials import CredentialPermission, CredentialedTestnetTransport

        class Broker:
            def inject(self, credential_id, **kw):
                self.permissions = kw["required_permissions"]
                return ({"event_type": "ACCOUNT_SNAPSHOT", "orders": [], "executions": [], "positions": {}},)
        broker = Broker()
        CredentialedTestnetTransport(broker, "cred", "BYBIT", lambda *a: ())({"action": "ACCOUNT_SNAPSHOT", "since_ns": 1})
        self.assertEqual(broker.permissions, (CredentialPermission.READ_ACCOUNT,))

    def test_a_spot_transport_has_no_snapshot_and_says_so(self):
        from crypto_stat_arb.testnet import BybitV5TestnetTransport
        transport = BybitV5TestnetTransport(symbols={PERP: "BTCUSDT"}, http=FakeHttp([]), clock_ms=lambda: self.NOW_MS)
        with self.assertRaisesRegex(ValueError, "perpetual transports only"):
            transport({"action": "ACCOUNT_SNAPSHOT", "since_ns": 1}, SECRET)


class FundingHistoryTests(unittest.TestCase):
    def fake(self, routes):
        calls = []

        def http(url):
            calls.append(url)
            path = urlparse(url).path
            query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
            return 200, json.dumps(routes[path](query))
        return http, calls

    def test_binance_pages_forward_until_the_end(self):
        stamps = [k * 8 * HOUR_MS for k in range(1, 6)]

        def rates(q):
            start, end, limit = int(q["startTime"]), int(q["endTime"]), int(q["limit"])
            rows = [{"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingTime": ts, "markPrice": "60000"}
                    for ts in stamps if start <= ts <= end]
            return rows[:limit]
        http, calls = self.fake({"/fapi/v1/fundingRate": rates})
        records = fetch_binance("BTCUSDT", PERP, 0, 10**13, http=http, limit=2)
        self.assertEqual([r["funding_ts_ms"] for r in records], stamps)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(r["settlement_history"] and r["mark_price"] == "60000" for r in records))

    def test_bybit_pages_backwards_and_takes_the_mark_from_the_kline(self):
        stamps = [k * 8 * HOUR_MS for k in range(1, 6)]

        def history(q):
            start, end, limit = int(q["startTime"]), int(q["endTime"]), int(q["limit"])
            rows = sorted((ts for ts in stamps if start <= ts <= end), reverse=True)[:limit]
            return {"retCode": 0, "result": {"list": [
                {"symbol": "BTCUSDT", "fundingRate": "-0.0001", "fundingRateTimestamp": str(ts)} for ts in rows]}}

        def kline(q):
            return {"retCode": 0, "result": {"list": [[q["start"], "60123.4", "1", "1", "1"]]}}
        http, calls = self.fake({"/v5/market/funding/history": history, "/v5/market/mark-price-kline": kline})
        records = fetch_bybit("BTCUSDT", PERP, 0, 10**13, http=http, limit=2)
        self.assertEqual([r["funding_ts_ms"] for r in records], stamps)
        self.assertEqual({r["mark_price"] for r in records}, {"60123.4"})
        self.assertIn("kline", records[0]["mark_source"])

    def test_only_public_hosts(self):
        from crypto_stat_arb import funding_history
        with self.assertRaisesRegex(ValueError, "non-public"):
            funding_history._get_json(lambda url: (200, "[]"), "https://evil.example", "/x", {})


def snapshot(venue, ts, sequence, bid, ask):
    native = {"lastUpdateId": sequence} if venue == "BINANCE" else {"u": sequence}
    return {"event_type": "BOOK_SNAPSHOT", "venue": venue, "instrument_id": PERP,
            "exchange_ts_ns": ts - 100_000, "receive_ts_ns": ts, "sequence": sequence,
            "bids": [[bid, "1.000"]], "asks": [[ask, "1.000"]], "native": native}


class PerpetualSessionTests(unittest.TestCase):
    def test_funding_settles_at_the_exchange_mark_and_forecasts_use_the_stream(self):
        settle = 8 * HOUR_MS * 1_000_000
        start = settle - 120 * 10**9                                   # 07:58:00
        events = [snapshot("BINANCE", start, 100, "59999.90", "60000.00"),
                  snapshot("BYBIT", start + 10**6, 900, "60120.00", "60120.10")]     # 20 bps: the arb trades
        for n in range(1, 5):                                          # quiet books to 08:02
            ts = start + n * 60 * 10**9
            events += [snapshot("BINANCE", ts, 100 + n, "60050.00", "60050.10"),
                       snapshot("BYBIT", ts + 10**6, 900 + n, "60050.00", "60050.10")]
        tickers = []
        for k, ts in enumerate((start + 30 * 10**9, settle - 2 * 10**9, settle + 5 * 10**9)):
            for venue, mark in (("BINANCE", "60040"), ("BYBIT", "60045")):
                tickers.append(DerivativeTicker(
                    event_id=f"{venue}-{k}", venue=venue, instrument_id=PERP, symbol="BTCUSDT",
                    exchange_ts_ns=ts, source_ts_ns=ts, receive_ts_ns=ts, mark_price=Decimal(mark) + k,
                    index_price=Decimal("60030"), funding_rate=Decimal("0.0002"),
                    next_funding_ts_ns=settle + 8 * HOUR_MS * 1_000_000 if k == 2 else settle,
                    raw_ref=f"raw#{venue}{k}", adapter_version="test", arrival_ordinal=k))
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            raw = json.loads(PERP_CONFIG.read_text())
            raw["funding"]["settle_lag_ms"] = 0
            raw["funding"]["rates"] = [   # realised history: no mark (as Bybit's) -> exchange mark used
                {"venue": v, "instrument_id": PERP, "funding_ts_ns": settle, "rate": "0.0001",
                 "settlement_history": True} for v in ("BINANCE", "BYBIT")]
            config = base / "perp.json"
            config.write_text(json.dumps(raw))
            (base / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
            (base / "derivatives.jsonl").write_text("".join(json.dumps(t.to_dict()) + "\n" for t in tickers))
            out = base / "out"
            result = run_backtest(config, base / "events.jsonl", out, derivatives_log=base / "derivatives.jsonl")
            funding = json.loads((out / "funding_report.json").read_text())
            report = json.loads((out / "economic_reconciliation.json").read_text())
            ledger = [json.loads(line) for line in (out / "economic_events.jsonl").open()]
        booked = [e for e in ledger if e["event_type"] == "FUNDING"]
        self.assertEqual(len(booked), 2)
        marks = {e["venue"]: e["valuation_price"] for e in booked}
        self.assertEqual(marks, {"BINANCE": "60041", "BYBIT": "60046"})   # last venue mark before 08:00
        self.assertTrue(all(e["valuation_source"].startswith("exchange mark") for e in booked))
        self.assertEqual(funding["mark_basis"], "EXCHANGE_MARK")
        self.assertEqual(funding["rules"]["predictions_observed"], 6)
        predicted = {p["venue"]: p for p in funding["predicted"]}
        self.assertEqual(predicted["BINANCE"]["predicted_rate"], "0.0002")
        self.assertEqual(predicted["BINANCE"]["next_funding_ts_ns"], settle + 8 * HOUR_MS * 1_000_000)
        self.assertEqual(result["economics"]["status"], "RECONCILED", report["checks"])
        self.assertTrue(all(check["passed"] for check in report["checks"]), report["checks"])


if __name__ == "__main__":
    unittest.main()
