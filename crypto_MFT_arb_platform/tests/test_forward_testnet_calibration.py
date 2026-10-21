"""Live paper, venue testnet, instrument status, calibration and benchmark."""

import asyncio
from decimal import Decimal
import hashlib
import hmac
import json
from pathlib import Path
import random
import tempfile
from urllib.parse import urlparse, parse_qs
import unittest
from unittest.mock import patch

from crypto_stat_arb.benchmark import run_benchmark
from crypto_stat_arb.calibration import calibrate_latency, compare_runs
from crypto_stat_arb.config import load_config
from crypto_stat_arb.credentials import CredentialPermission, CredentialedTestnetTransport
from crypto_stat_arb.execution import (
    CanonicalOrderRequest, OrderAction, OrderTimeoutPolicy, OrderType,
    StrategyDirectedRouter, TimeInForce,
)
from crypto_stat_arb.instrument_status import InstrumentStatusPoller
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.live_feed import LiveFeedSettings
from crypto_stat_arb.models import MarketEvent, OrderState, PlanState, Side
from crypto_stat_arb.oms import OMS
from crypto_stat_arb.paper_live import LivePaperSession
from crypto_stat_arb.runner import run_backtest
from crypto_stat_arb.sdk import (
    FeatureAPI, LookaheadError, TestnetVenueAdapter, immediate_fill_testnet_transport,
)
from crypto_stat_arb.session import ExecutionMode, TradingSession
from crypto_stat_arb.testnet import (
    BinanceSpotTestnetTransport, BybitV5TestnetTransport,
)
from test_live_feed import FakeConnector, FakeWebSocket, StepClock
from test_oms import instrument_master, make_book, opportunity, risk


ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config" / "research.json"
INSTRUMENT = "BTC-USDT-SPOT"
SECRET = memoryview(b"test-key:test-secret")


def canonical_submit(venue="BINANCE", side=Side.BUY, price="60000.00", tif=TimeInForce.IOC):
    return CanonicalOrderRequest.create(
        action=OrderAction.SUBMIT, order_id="3f1d2c9e-0000-4000-8000-000000000001",
        parent_opportunity_id="op", parent_order_id="parent",
        instrument_id=INSTRUMENT, venue=venue, side=side,
        price=Decimal(price), quantity=Decimal("0.010"),
        order_type=OrderType.LIMIT, time_in_force=tif, created_ts_ns=1,
    ).to_dict()


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        status, payload = response
        return status, json.dumps(payload)


class LivePaperTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_books_drive_the_shared_session_and_replay_matches(self):
        binance = FakeWebSocket([json.dumps({
            "e": "depthUpdate", "E": 1000, "T": 1000, "s": "BTCUSDT",
            "U": 101, "u": 101, "b": [["59999.90", "2"]], "a": [],
        })])
        bybit = FakeWebSocket([
            json.dumps({
                "topic": "orderbook.50.BTCUSDT", "type": "snapshot", "ts": 1000,
                "data": {"s": "BTCUSDT", "u": 10,
                         "b": [["60060.00", "1"]], "a": [["60060.10", "1"]]},
            }),
            json.dumps({
                "topic": "orderbook.50.BTCUSDT", "type": "delta", "ts": 1001,
                "data": {"s": "BTCUSDT", "u": 11, "b": [["60060.00", "2"]], "a": []},
            }),
        ])

        clock = StepClock()

        async def http_getter(url, timeout):
            # Let the websocket frames drain first so the snapshot's receive
            # time is later than every frame processed before it; replay
            # orders by receive time and must see the same sequence.
            await asyncio.sleep(0.05)
            return json.dumps({
                "lastUpdateId": 100,
                "bids": [["59999.90", "1"]], "asks": [["60000.00", "1"]],
            }), clock()

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paper = LivePaperSession(
                CONFIG, base / "live", "paper-test",
                LiveFeedSettings(
                    max_events=4, reconnect_min_seconds=0.001,
                    reconnect_max_seconds=0.002, durable=False,
                    udp_enabled=False, trade_streams_enabled=False,
                ),
                websocket_connector=FakeConnector({"BINANCE": [binance], "BYBIT": [bybit]}),
                http_getter=http_getter, clock_ns=clock, tick_seconds=10,
            )
            summary = await asyncio.wait_for(paper.run(), timeout=5)
            paper_dir = paper.output_dir
            rows = [
                json.loads(line)
                for line in (paper_dir / "paper_timestamps.jsonl").read_text().splitlines()
            ]
            decisions = [row for row in rows if row["record_type"] == "DECISION"]

            self.assertEqual(summary["execution_mode"], "INTERNAL_PAPER")
            self.assertEqual(summary["opportunities"], 1)
            self.assertEqual(summary["closed_plans"], 1)
            self.assertEqual(summary["execution_realism"]["open_plans"], 0)
            self.assertEqual(len(decisions), 1)
            self.assertGreaterEqual(decisions[0]["decision_ts_ns"], decisions[0]["receive_ts_ns"])
            self.assertTrue(any(row["record_type"] == "EVENT" for row in rows))

            replay = run_backtest(CONFIG, paper_dir / "canonical_events.jsonl", base / "replay")
            self.assertEqual(replay["opportunities"], 1)
            report = compare_runs(base / "replay", paper_dir, base / "compare.json")
            self.assertEqual(report["decision"], "INSUFFICIENT_SAMPLE")
            self.assertTrue(report["metrics"]["opportunities"]["within_tolerance"])
            self.assertTrue(report["metrics"]["fills"]["within_tolerance"])

    def test_session_refuses_mislabelled_execution_modes(self):
        cfg, _ = load_config(CONFIG)
        with self.assertRaises(ValueError):
            TradingSession(cfg, execution_mode=ExecutionMode.INTERNAL_PAPER, live_data=False)
        with self.assertRaises(ValueError):
            TradingSession(cfg, execution_mode=ExecutionMode.INTERNAL_SIMULATION, live_data=True)
        with self.assertRaises(ValueError):
            TradingSession(cfg, execution_mode=ExecutionMode.VENUE_TESTNET, live_data=True)
        adapters = {
            venue: TestnetVenueAdapter(venue, immediate_fill_testnet_transport)
            for venue in cfg.venues
        }
        with self.assertRaises(ValueError):
            TradingSession(cfg, live_data=False, adapters=adapters)
        session = TradingSession(
            cfg, execution_mode=ExecutionMode.VENUE_TESTNET, live_data=True,
            adapters=adapters,
        )
        self.assertIsNone(session.router.latency_model)


class TestnetTransportTests(unittest.TestCase):
    def test_binance_submit_is_signed_and_ioc_remainder_is_explicit(self):
        http = FakeHttp([(200, {
            "orderId": 7, "status": "EXPIRED", "executedQty": "0.004",
            "cummulativeQuoteQty": "240.0", "transactTime": 1_700_000_000_001,
        })])
        transport = BinanceSpotTestnetTransport(
            symbols={INSTRUMENT: "BTCUSDT"}, http=http, clock_ms=lambda: 1_700_000_000_000,
        )
        events = transport(canonical_submit(), SECRET)
        self.assertEqual([item["event_type"] for item in events],
                         ["ACKNOWLEDGED", "PARTIAL", "CANCELLED"])
        self.assertEqual(events[1]["average_price"], "60000")
        method, url, headers, body = http.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(urlparse(url).hostname, "testnet.binance.vision")
        query, signature = urlparse(url).query.rsplit("&signature=", 1)
        self.assertEqual(signature, hmac.new(b"test-secret", query.encode(), hashlib.sha256).hexdigest())
        params = parse_qs(query)
        self.assertEqual(params["timeInForce"], ["IOC"])
        self.assertEqual(params["price"], ["60000"])
        self.assertEqual(headers["X-MBX-APIKEY"], "test-key")
        self.assertNotIn("test-secret", url + json.dumps(headers))

    def test_binance_query_distinguishes_missing_orders_and_refuses_production(self):
        http = FakeHttp([(400, {"code": -2013, "msg": "Order does not exist."})])
        transport = BinanceSpotTestnetTransport(symbols={INSTRUMENT: "BTCUSDT"}, http=http)
        events = transport({**canonical_submit(), "action": "QUERY"}, SECRET)
        self.assertIsNone(events[0]["event_type"])
        with self.assertRaisesRegex(ValueError, "non-testnet"):
            BinanceSpotTestnetTransport(
                base_url="https://api.binance.com", symbols={INSTRUMENT: "BTCUSDT"}
            )

    def test_bybit_create_then_query_for_fills_with_v5_signature(self):
        http = FakeHttp([
            (200, {"retCode": 0, "result": {"orderId": "b1"}}),
            (200, {"retCode": 0, "result": {"list": [{
                "orderId": "b1", "orderStatus": "Filled", "cumExecQty": "0.010",
                "avgPrice": "60060", "updatedTime": "1700000000002",
            }]}}),
        ])
        transport = BybitV5TestnetTransport(
            symbols={INSTRUMENT: "BTCUSDT"}, http=http, clock_ms=lambda: 1_700_000_000_000,
            sleep=lambda _seconds: None,
        )
        events = transport(canonical_submit("BYBIT", Side.SELL, "60060.00"), SECRET)
        self.assertEqual([item["event_type"] for item in events], ["ACKNOWLEDGED", "FILLED"])
        method, url, headers, body = http.calls[0]
        expected = hmac.new(
            b"test-secret",
            (headers["X-BAPI-TIMESTAMP"] + "test-key" + headers["X-BAPI-RECV-WINDOW"]
             + body.decode()).encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(headers["X-BAPI-SIGN"], expected)
        self.assertEqual(json.loads(body)["timeInForce"], "IOC")
        self.assertEqual(urlparse(url).hostname, "api-testnet.bybit.com")

    def test_query_permission_is_read_account(self):
        class RecordingBroker:
            def __init__(self):
                self.permissions = []

            def inject(self, credential_id, *, actor, venue, environment,
                       required_permissions, consumer):
                self.permissions.append(tuple(required_permissions))
                return consumer(memoryview(b"k:s"))

        broker = RecordingBroker()
        transport = CredentialedTestnetTransport(
            broker, "cred", "BINANCE", lambda request, secret: ({"event_type": None},),
        )
        transport({"action": "QUERY"})
        self.assertEqual(broker.permissions, [(CredentialPermission.READ_ACCOUNT,)])

    def test_network_failure_leaves_order_unknown_then_query_resolves(self):
        calls = {"SUBMIT": 0, "QUERY": 0}

        def flaky(request):
            calls[request["action"]] += 1
            if request["action"] == "SUBMIT":
                raise ConnectionError("timeout after send")
            return ({
                "event_type": "FILLED", "cumulative_filled_quantity": request["quantity"],
                "average_price": request["price"],
            },)

        adapters = {
            "BINANCE": TestnetVenueAdapter("BINANCE", immediate_fill_testnet_transport),
            "BYBIT": TestnetVenueAdapter("BYBIT", flaky),
        }
        books = {
            ("BINANCE", INSTRUMENT): make_book("BINANCE", "59990.00", "60000.00"),
            ("BYBIT", INSTRUMENT): make_book("BYBIT", "60060.00", "60070.00"),
        }
        oms = OMS(
            instrument_master(), risk(), Ledger(),
            router=StrategyDirectedRouter(instrument_master(), adapters=adapters),
            order_timeout_policy=OrderTimeoutPolicy(
                report_timeout_ns=100, query_interval_ns=50, max_queries=3,
            ),
        )
        plan = oms.execute(opportunity(), books)
        self.assertNotEqual(plan.state, PlanState.CLOSED)
        self.assertEqual(plan.children[1].state, OrderState.CREATED)
        oms.advance_time(1_000, books)
        self.assertEqual(plan.children[1].state, OrderState.FILLED)
        self.assertEqual(plan.state, PlanState.CLOSED)
        self.assertEqual(calls, {"SUBMIT": 1, "QUERY": 1})
        self.assertEqual(adapters["BYBIT"].transport_errors[0]["error"], "ConnectionError")


class InstrumentStatusReplayTests(unittest.TestCase):
    """Regression: a live session's public instrument-status poller can
    append a new effective-dated instrument version mid-session (a real
    Binance/Bybit tick, lot or minimum that differs from the static config).
    A backtest replay of that same period, built from the config file alone,
    then validates orders against the wrong (stale) tick size: the package
    is silently rejected even though the live session traded those exact
    prices correctly under the rules actually in force (and, on the
    residual-unwind path, an equivalent mismatch can raise "price violates
    tick" uncaught -- see the unwind ValueError guard in oms.py)."""

    def test_replay_applies_recorded_instrument_status_before_validating_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            # The static config ships tick_size 0.10; the "live" session
            # discovers the real exchange tick is 0.01 partway through.
            status_log = base / "paper_timestamps.jsonl"
            status_log.write_text(json.dumps({
                "record_type": "INSTRUMENT_STATUS", "venue": "BINANCE",
                "instrument_id": INSTRUMENT, "effective_from_ns": 500,
                "source": "BINANCE_PUBLIC_REST",
                "changes": {"tick_size": "0.01"},
            }) + "\n")
            events_path = base / "events.jsonl"
            rows = [
                {
                    "event_type": "BOOK_SNAPSHOT", "venue": "BINANCE",
                    "instrument_id": INSTRUMENT, "exchange_ts_ns": 1_000,
                    "receive_ts_ns": 1_000, "sequence": 1,
                    "bids": [["59999.90", "1"]], "asks": [["60000.01", "1"]],
                },
                {
                    "event_type": "BOOK_SNAPSHOT", "venue": "BYBIT",
                    "instrument_id": INSTRUMENT, "exchange_ts_ns": 1_000,
                    "receive_ts_ns": 1_000, "sequence": 1,
                    "bids": [["61000.02", "1"]], "asks": [["61000.10", "1"]],
                },
            ]
            events_path.write_text("".join(json.dumps(row) + "\n" for row in rows))

            # Without the recorded instrument status, replay validates the
            # order against the stale 0.10 tick from the static config and
            # rejects a package the live session filled correctly: a silent
            # replay-vs-paper mismatch, not merely a missed opportunity.
            stale = run_backtest(CONFIG, events_path, base / "without_status")
            self.assertEqual(stale["opportunities"], 1)
            self.assertEqual(stale["closed_plans"], 1)
            self.assertEqual(
                stale["strategies"]["strategies"]["cross_venue_btc_v1"]["outcomes"],
                {"REJECTED": 1},
            )
            self.assertEqual(stale["instrument_status_changes_applied"], 0)

            summary = run_backtest(
                CONFIG, events_path, base / "with_status",
                instrument_status_log=status_log,
            )
        self.assertEqual(summary["instrument_status_changes_applied"], 1)
        self.assertEqual(summary["opportunities"], 1)
        self.assertEqual(summary["closed_plans"], 1)


class EventTimingReplayTests(unittest.TestCase):
    """Regression: replay timed every decision at an event's own
    ``receive_ts_ns``, implicitly assuming zero processing latency. Live
    paper measures real wall-clock processing time, which can run seconds
    behind the event stream under load or a reconnect storm; time-sensitive
    strategy logic (a cooldown window, a report-timeout timer) sees a
    different "now" as a result, so replay and live paper can make a
    different number of trading decisions from the identical event stream.
    Live's recorded ``processed_ts_ns`` lets replay reproduce that "now"."""

    def test_load_recorded_event_timing_parses_only_event_rows(self):
        from crypto_stat_arb.runner import load_recorded_event_timing

        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "paper_timestamps.jsonl"
            log.write_text("".join(json.dumps(row) + "\n" for row in (
                {"record_type": "EVENT", "event_id": "e1", "processed_ts_ns": 5_000},
                {"record_type": "EVENT", "event_id": "e2", "processed_ts_ns": 9_000},
                {"record_type": "DECISION", "event_id": "e1", "processed_ts_ns": 999},
                {"record_type": "INSTRUMENT_STATUS", "venue": "BINANCE"},
            )))
            timing = load_recorded_event_timing(log)
        self.assertEqual(timing, {"e1": 5_000, "e2": 9_000})

    def test_missing_event_timing_log_is_a_silent_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = run_backtest(
                CONFIG, ROOT / "data" / "samples" / "two_venue_books.jsonl",
                Path(directory) / "out",
                event_timing_log=Path(directory) / "does_not_exist.jsonl",
            )
        self.assertEqual(summary["recorded_event_timing_used"], 0)

    def test_replay_uses_recorded_live_processing_time_per_event(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            events_path = ROOT / "data" / "samples" / "two_venue_books.jsonl"
            rows = [
                json.loads(line)
                for line in events_path.read_text().splitlines() if line.strip()
            ]
            # Record every event as processed 50ms after it was received:
            # a stand-in for real (if somewhat elevated) live processing
            # lag, well under this config's 1000ms book-age gate.
            timing_log = base / "paper_timestamps.jsonl"
            timing_log.write_text("".join(
                json.dumps({
                    "record_type": "EVENT",
                    "event_id": row.get("event_id") or MarketEvent.from_dict(row).event_id,
                    "processed_ts_ns": int(row["receive_ts_ns"]) + 50_000_000,
                }) + "\n"
                for row in rows
            ))
            summary = run_backtest(
                CONFIG, events_path, base / "out", event_timing_log=timing_log,
            )
        self.assertEqual(summary["recorded_event_timing_used"], len(rows))
        self.assertEqual(summary["opportunities"], 1)
        self.assertEqual(summary["closed_plans"], 1)

    def test_lag_large_enough_to_breach_the_book_age_gate_blocks_the_trade(self):
        # The mirror case: proves the recorded time is not merely accepted
        # and ignored. A lag well past this config's 1000ms book-age gate
        # makes every book look stale by the time it is evaluated, exactly
        # as it would live under the same real delay.
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            events_path = ROOT / "data" / "samples" / "two_venue_books.jsonl"
            rows = [
                json.loads(line)
                for line in events_path.read_text().splitlines() if line.strip()
            ]
            timing_log = base / "paper_timestamps.jsonl"
            timing_log.write_text("".join(
                json.dumps({
                    "record_type": "EVENT",
                    "event_id": row.get("event_id") or MarketEvent.from_dict(row).event_id,
                    "processed_ts_ns": int(row["receive_ts_ns"]) + 5_000_000_000,
                }) + "\n"
                for row in rows
            ))
            summary = run_backtest(
                CONFIG, events_path, base / "out", event_timing_log=timing_log,
            )
        self.assertEqual(summary["recorded_event_timing_used"], len(rows))
        self.assertEqual(summary["opportunities"], 0)


class InstrumentStatusTests(unittest.TestCase):
    def test_public_status_change_becomes_new_effective_version(self):
        cfg, _ = load_config(CONFIG)

        def http_get(url):
            if "binance" in url:
                return {"symbols": [{"status": "BREAK", "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.00001000", "minQty": "0.00001000"},
                    {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
                ]}]}
            return {"retCode": 0, "result": {"list": [{
                "status": "Trading", "priceFilter": {"tickSize": "0.1"},
                "lotSizeFilter": {"basePrecision": "0.001", "minOrderQty": "0.001",
                                  "minOrderAmt": "5"},
            }]}}

        poller = InstrumentStatusPoller(cfg.instruments, {
            "BINANCE": (INSTRUMENT, "BTCUSDT"), "BYBIT": (INSTRUMENT, "BTCUSDT"),
        }, http_get=http_get)
        changes = poller.poll(2_000)
        self.assertEqual([change["venue"] for change in changes], ["BINANCE"])
        self.assertEqual(cfg.instruments.get("BINANCE", INSTRUMENT, 1_999).status, "TRADING")
        current = cfg.instruments.get("BINANCE", INSTRUMENT, 2_000)
        self.assertEqual(current.status, "BREAK")
        self.assertEqual(current.tick_size, Decimal("0.01"))
        self.assertEqual(cfg.instruments.version_snapshot()["status_event_count"], 1)

    def test_transient_poll_failure_is_retried_before_giving_up(self):
        # Regression: a single flaky HTTP call (timeout, DNS blip) used to
        # fail the whole poll immediately; it must now be retried.
        cfg, _ = load_config(CONFIG)
        calls = {"count": 0}

        def flaky_then_ok(url):
            calls["count"] += 1
            if calls["count"] < 3:
                raise TimeoutError("transient")
            if "binance" in url:
                return {"symbols": [{"status": "TRADING", "filters": []}]}
            return {"retCode": 0, "result": {"list": [{
                "status": "Trading", "priceFilter": {}, "lotSizeFilter": {},
            }]}}

        poller = InstrumentStatusPoller(
            cfg.instruments, {"BINANCE": (INSTRUMENT, "BTCUSDT")}, http_get=flaky_then_ok,
        )
        with patch("crypto_stat_arb.instrument_status.time.sleep"):
            fetched = poller.fetch()
        self.assertEqual(fetched["BINANCE"]["status"], "TRADING")
        self.assertEqual(calls["count"], 3)

    def test_persistent_poll_failure_still_raises_after_retries(self):
        cfg, _ = load_config(CONFIG)
        calls = {"count": 0}

        def always_fails(url):
            calls["count"] += 1
            raise TimeoutError("still down")

        poller = InstrumentStatusPoller(
            cfg.instruments, {"BINANCE": (INSTRUMENT, "BTCUSDT")}, http_get=always_fails,
        )
        with patch("crypto_stat_arb.instrument_status.time.sleep"):
            with self.assertRaises(TimeoutError):
                poller.fetch()
        self.assertEqual(calls["count"], 3)


class CalibrationAndBenchmarkTests(unittest.TestCase):
    def test_fit_from_paper_and_testnet_timestamps(self):
        rng = random.Random(4)
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            rows = []
            for index in range(400):
                exchange = 1_700_000_000_000_000_000 + index * 100_000_000
                feed = int(rng.lognormvariate(15.4, 0.3))  # ~5 ms
                rows.append({
                    "record_type": "EVENT", "venue": "BINANCE",
                    "exchange_ts_ns": exchange,
                    "receive_ts_ns": exchange - 230_000_000 + feed,
                })
            (run / "paper_timestamps.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )
            (run / "testnet_order_timestamps.jsonl").write_text("".join(
                json.dumps({
                    "venue": "BYBIT", "action": "SUBMIT", "order_send_ts_ns": 0,
                    "response_ts_ns": int(rng.gauss(80_000_000, 5_000_000)),
                }) + "\n"
                for _ in range(100)
            ))
            report = calibrate_latency(run, run / "calibrated.json", min_samples=30)
        market = report["fits"]["BINANCE.market_data"]
        self.assertLess(market["goodness_of_fit"]["p50"]["relative_error"], 0.25)
        entry = report["latency"]["profiles"]["BYBIT"]["entry"]
        self.assertAlmostEqual(entry["base_ns"] / 1_000_000, 40, delta=3)
        self.assertIn("round trip", report["notes"]["order_path"])

    def test_compare_flags_divergence(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name, mode, fills in (
                ("replay", "INTERNAL_SIMULATION", 100), ("paper", "INTERNAL_PAPER", 60),
            ):
                run = base / name
                run.mkdir()
                (run / "summary.json").write_text(json.dumps({
                    "opportunities": 50, "closed_plans": 50, "net_pnl": "10",
                    "fees": "5", "execution_mode": mode,
                }))
                (run / "tca_report.json").write_text(json.dumps({"summary": {
                    "orders": 100, "fills": fills, "rejects": 0, "cancels": 0,
                    "missed_opportunities": 0, "weighted_slippage_bps": "0.5",
                    "fees": "5",
                }}))
            report = compare_runs(base / "replay", base / "paper")
        self.assertEqual(report["decision"], "DIVERGED")
        self.assertIn("fills", report["diverged_metrics"])
        self.assertTrue(report["metrics"]["opportunities"]["within_tolerance"])

    def test_benchmark_reports_throughput_and_stage_latency(self):
        with tempfile.TemporaryDirectory() as directory:
            report = run_benchmark(CONFIG, Path(directory), synthetic_count=600)
            self.assertTrue((Path(directory) / "benchmark_report.json").exists())
        self.assertEqual(report["events"], 600)
        self.assertGreater(report["replay"]["events_per_second"], 0)
        self.assertGreater(report["replay"]["opportunities"], 0)
        self.assertIn("paper_frame_overhead_ns", report["paper"]["stages"])
        self.assertEqual(len(report["checks"]), 4)


class FeatureLookaheadTests(unittest.TestCase):
    def test_versioned_point_in_time_features_reject_lookahead(self):
        now = [100]
        features = FeatureAPI(clock_ns=lambda: now[0])
        features.register("spread", "1.0.0")
        with self.assertRaises(LookaheadError):
            features.publish("spread", 1, ts_ns=101)
        with self.assertRaisesRegex(ValueError, "version"):
            features.publish("spread", 1, ts_ns=90, version="2.0.0")
        features.publish("spread", 1, ts_ns=90)
        now[0] = 200
        future_event = MarketEvent.from_dict({
            "event_type": "BOOK_DELTA", "venue": "BINANCE", "instrument_id": INSTRUMENT,
            "exchange_ts_ns": 150, "receive_ts_ns": 150, "sequence": 1,
        })
        features.observe_event(future_event)
        with self.assertRaises(LookaheadError):
            features.publish("spread", 2, ts_ns=120, source_event_ids=(future_event.event_id,))
        features.publish("spread", 3, ts_ns=160, source_event_ids=(future_event.event_id,))
        self.assertEqual(features.latest("spread", as_of_ns=100), 1)
        self.assertEqual(features.latest("spread"), 3)
        self.assertEqual(features.snapshot()["spread"]["version"], "1.0.0")


if __name__ == "__main__":
    unittest.main()
