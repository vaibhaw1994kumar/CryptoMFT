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
            rows = [json.loads(line) for line in (part / "derivatives.jsonl").open()]
            trace = [json.loads(line) for line in (part / "trace.jsonl").open()]
            service.recorder.close({})
        self.assertEqual([t.venue for t in seen], ["BYBIT", "BYBIT", "BINANCE"])
        self.assertEqual(seen[1].funding_rate, Decimal("0.0001"))           # kept from the snapshot
        self.assertEqual([r["mark_price"] for r in rows], ["60000", "60005", "60001"])
        self.assertEqual([t.arrival_ordinal for t in seen], [1, 2, 3])
        self.assertEqual(sum(1 for t in trace if t.get("disposition") == "DERIVATIVE"), 3)


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
