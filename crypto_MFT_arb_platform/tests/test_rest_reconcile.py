"""Audit P2: periodic REST cross-check of live books (rest_reconcile + LiveOrderBookService)."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.live_feed import LiveFeedSettings, LiveOrderBookService
from crypto_stat_arb.models import BookState
from test_live_feed import FakeConnector, FakeWebSocket, StepClock

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config" / "research.json"
BYBIT_URL, TOPIC = "wss://stream.bybit.com/v5/public/spot", "orderbook.50.BTCUSDT"
BINANCE_URL = "wss://stream.binance.com:9443/ws/btcusdt@depth@100ms"


def bybit(kind, u, seq, bids, asks):
    return json.dumps({"topic": TOPIC, "type": kind, "ts": 1_000 + u,
                       "data": {"s": "BTCUSDT", "u": u, "seq": seq, "b": bids, "a": asks}})


def bybit_rest(seq, bids, asks, ret_code=0):
    return json.dumps({"retCode": ret_code, "retMsg": "OK" if ret_code == 0 else "bad",
                       "result": {"s": "BTCUSDT", "b": bids, "a": asks, "ts": 1, "u": 999, "seq": seq}})


SNAPSHOT = bybit("snapshot", 10, 1_000, [["100", "1"], ["99", "2"]], [["101", "1"], ["102", "2"]])
D1 = bybit("delta", 11, 1_010, [["100", "3"]], [])            # state after seq 1010
D2 = bybit("delta", 12, 1_020, [["99", "0"]], [["101.5", "4"]])  # state after seq 1020
STATE_1010 = ([["100", "3"], ["99", "2"]], [["101", "1"], ["102", "2"]])


class Live:
    def __init__(self, directory, venues=("BYBIT",)):
        self.service = LiveOrderBookService(CONFIG, Path(directory), "rc",
                                            LiveFeedSettings(venues=venues, durable=False), clock_ns=StepClock())

    def bybit(self, frame):
        return self.service._process_websocket_frame("BYBIT", BYBIT_URL, frame, channel=TOPIC)

    def rest(self, venue, body):
        self.service.on_reconcile_snapshot(venue, "https://rest", body, 1)

    def logged(self, event_type):
        path = self.service.recorder.session_dir / "structured_logs.jsonl"
        return [r for r in (json.loads(l) for l in path.read_text().splitlines()) if r["event_type"] == event_type]

    def close(self):
        self.service.recorder.close({})
        self.service.ohlc_writer.close()
        self.service.ltp_writer.close()


class BybitReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.live = Live(self.tmp.name)
        self.stats = self.live.service.stats["BYBIT"]

    def tearDown(self):
        self.live.close()
        self.tmp.cleanup()

    def test_match_while_the_feed_has_moved_on(self):
        # Request sent at seq 1000; the REST book (seq 1015) arrives after the feed passed it.
        self.live.bybit(SNAPSHOT)
        self.live.service._start_history("BYBIT")
        self.live.bybit(D1)
        self.live.bybit(D2)
        self.live.rest("BYBIT", bybit_rest(1_015, *STATE_1010))
        self.assertEqual(self.stats.reconciles_ok, 1)
        self.assertEqual(self.live.service.books["BYBIT"].state, BookState.VALID)
        self.assertEqual(len(self.live.logged("RECONCILE_OK")), 1)

    def test_rest_book_ahead_of_the_feed_is_finished_on_the_crossing_delta(self):
        self.live.bybit(SNAPSHOT)
        self.live.service._start_history("BYBIT")
        self.live.rest("BYBIT", bybit_rest(1_015, *STATE_1010))
        self.assertEqual(self.stats.reconciles_ok, 0)                   # pending: feed at 1000
        self.live.bybit(D1)
        self.assertEqual(self.stats.reconciles_ok, 0)                   # 1010 <= 1015: not crossed yet
        self.live.bybit(D2)
        self.assertEqual(self.stats.reconciles_ok, 1)

    def test_best_bid_ask_disagreement_forces_a_resync(self):
        self.live.bybit(SNAPSHOT)
        self.live.service._start_history("BYBIT")
        self.live.bybit(D1)
        self.live.rest("BYBIT", bybit_rest(1_015, [["100.5", "1"]], [["101", "1"]]))   # touch differs
        state = self.live.bybit(D2)
        self.assertEqual(self.stats.reconcile_mismatches, 1)
        book = self.live.service.books["BYBIT"]
        self.assertEqual((state, book.invalid_reason), (BookState.GAP_DETECTED, "rest_reconcile_touch_mismatch"))
        self.assertTrue(self.live.service.book_gate.suspended)        # trading blocked
        mismatch = self.live.logged("RECONCILE_MISMATCH")[0]
        self.assertIn("100.5", mismatch["payload"]["differing_prices"])

    def test_deeper_level_difference_is_reported_not_resynced(self):
        self.live.bybit(SNAPSHOT)
        self.live.service._start_history("BYBIT")
        self.live.bybit(D1)
        self.live.bybit(D2)
        bids, asks = STATE_1010
        self.live.rest("BYBIT", bybit_rest(1_015, bids, [["101", "1"], ["102", "9"]]))  # 102 qty differs
        self.assertEqual(self.stats.reconcile_deep_diffs, 1)
        self.assertEqual(self.live.service.books["BYBIT"].state, BookState.VALID)
        self.assertEqual(self.live.logged("RECONCILE_DEEP_DIFF")[0]["payload"]["differing_prices"], ["102"])

    def test_snapshot_older_than_the_history_is_skipped(self):
        self.live.bybit(SNAPSHOT)
        self.live.bybit(D1)
        self.live.service._start_history("BYBIT")                      # history starts at 1010
        self.live.rest("BYBIT", bybit_rest(1_005, *STATE_1010))
        self.assertEqual((self.stats.reconcile_skipped, self.stats.reconciles_ok), (1, 0))
        self.assertEqual(self.live.logged("RECONCILE_SKIPPED")[0]["message"], "snapshot_older_than_history")

    def test_resync_while_waiting_skips_the_check(self):
        self.live.bybit(SNAPSHOT)
        self.live.service._start_history("BYBIT")
        self.live.rest("BYBIT", bybit_rest(1_015, *STATE_1010))
        self.live.bybit(bybit("snapshot", 1, 5, [["100", "1"]], [["101", "1"]]))   # venue restart
        self.assertEqual(self.stats.reconcile_skipped, 1)
        self.assertEqual(self.live.logged("RECONCILE_SKIPPED")[0]["message"], "book_resynchronised")
        self.live.bybit(bybit("delta", 2, 2_000, [["100", "2"]], []))
        self.assertEqual(self.stats.reconciles_ok + self.stats.reconcile_mismatches, 0)

    def test_unusable_rest_reply_is_counted(self):
        self.live.bybit(SNAPSHOT)
        self.live.rest("BYBIT", bybit_rest(1_015, [], [], ret_code=10001))
        self.assertEqual(self.stats.reconcile_fetch_errors, 1)
        self.assertEqual(self.live.service.books["BYBIT"].state, BookState.VALID)


class BinanceReconcileTests(unittest.TestCase):
    def test_last_update_id_is_on_the_websocket_counter(self):
        with tempfile.TemporaryDirectory() as directory:
            live = Live(directory, venues=("BINANCE",))
            service = live.service
            service._process_binance_snapshot(
                "https://api.binance.com/api/v3/depth", json.dumps(
                    {"lastUpdateId": 100, "bids": [["100", "1"]], "asks": [["101", "1"]]}), 1)
            service._start_history("BINANCE")
            for first, last, bid in ((101, 105, "2"), (106, 110, "3")):
                service._process_websocket_frame("BINANCE", BINANCE_URL, json.dumps({
                    "e": "depthUpdate", "E": 1000 + last, "s": "BTCUSDT", "U": first, "u": last,
                    "b": [["100", bid]], "a": []}), channel="depth@100ms")
            live.rest("BINANCE", json.dumps({"lastUpdateId": 105, "bids": [["100", "2"]], "asks": [["101", "1"]]}))
            self.assertEqual(service.stats["BINANCE"].reconciles_ok, 1)       # exact at u=105
            live.close()


class ReconcileLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_fetches_on_its_interval_and_reports_fetch_failures(self):
        calls = []
        holder = {}

        async def http_getter(url, timeout):
            calls.append(url)
            if len(calls) == 3:
                holder["service"].request_stop()
            raise ConnectionError("HTTP 503")

        socket = FakeWebSocket([SNAPSHOT, D1])
        with tempfile.TemporaryDirectory() as directory:
            service = LiveOrderBookService(
                CONFIG, Path(directory), "rc-loop",
                LiveFeedSettings(venues=("BYBIT",), durable=False, rest_reconcile_seconds=0.01),
                websocket_connector=FakeConnector({"BYBIT": [socket]}), http_getter=http_getter,
                clock_ns=StepClock())
            holder["service"] = service
            await asyncio.wait_for(service.run(), timeout=5)
            self.assertTrue(calls)
            self.assertTrue(all("api.bybit.com/v5/market/orderbook?category=spot&symbol=BTCUSDT&limit=50" in u
                                for u in calls))
            self.assertEqual(service.stats["BYBIT"].reconcile_fetch_errors, len(calls))
            self.assertEqual(service.books["BYBIT"].state, BookState.VALID)     # a failed check is not a fault

    def test_setting_must_be_positive(self):
        with self.assertRaises(ValueError):
            LiveFeedSettings(rest_reconcile_seconds=0).validate()


if __name__ == "__main__":
    unittest.main()
