"""Audit Step 13: behaviour under load and degraded conditions (deterministic, CI-sized).

Throughput and latency numbers are host-specific and come from scripts/load_test.py;
these tests assert what must hold regardless of speed.
"""

import asyncio
import json
from pathlib import Path
import tempfile
import tracemalloc
import unittest

from crypto_stat_arb.live_feed import LiveFeedSettings, LiveOrderBookService
from crypto_stat_arb.models import BookState
from test_live_feed import FakeConnector, FakeWebSocket, StepClock

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config/research.json"          # time.max_feed_delay_ms = 2000
MS = 1_000_000


def bybit(kind, u, ts_ms, bid="100", ask="101"):
    return json.dumps({"topic": "orderbook.50.BTCUSDT", "type": kind, "ts": ts_ms,
                       "data": {"s": "BTCUSDT", "u": u, "b": [[bid, str(1 + u % 5)]], "a": [[ask, "1"]]}})


def binance(first, last, ts_ms=1_000):
    return json.dumps({"e": "depthUpdate", "E": ts_ms, "T": ts_ms, "s": "BTCUSDT", "U": first, "u": last,
                       "b": [["100", str(1 + last % 5)]], "a": [["101", "1"]]})


class ClosedSocket(FakeWebSocket):
    async def recv(self):
        await asyncio.sleep(0)
        if self.messages:
            return self.messages.pop(0)
        raise ConnectionResetError("peer closed")


def logged(service, kind):
    rows = (json.loads(l) for l in (service.recorder.session_dir / "structured_logs.jsonl").read_text().splitlines())
    return [r for r in rows if r["event_type"] == kind]


def close(service):
    service.recorder.close({})
    service.ohlc_writer.close()
    service.ltp_writer.close()


class BurstBacklogTests(unittest.TestCase):
    def test_a_backlog_is_flagged_delayed_without_a_resync_storm(self):
        wall = {"ns": 1_700_000_100_000 * MS}
        with tempfile.TemporaryDirectory() as directory:
            service = LiveOrderBookService(CONFIG, Path(directory), "burst",
                                           LiveFeedSettings(venues=("BYBIT",), durable=False),
                                           clock_ns=lambda: wall["ns"])
            now_ms = wall["ns"] // MS
            process = lambda frame: service._process_websocket_frame(  # noqa: E731
                "BYBIT", "wss://x", frame, channel="orderbook.50.BTCUSDT")
            process(bybit("snapshot", 10, now_ms))
            # 3,000 updates drained in one burst, each generated 3 s before we read it.
            for u in range(11, 3_011):
                process(bybit("delta", u, now_ms - 3_000))
            process(bybit("delta", 3_011, now_ms))                       # caught up
            book = service.books["BYBIT"]
            self.assertEqual((book.state, book.stats.gaps, service.stats["BYBIT"].reconnects),
                             (BookState.VALID, 0, 0))                     # data is intact: no resync
            self.assertEqual(service.stats["BYBIT"].feed_delayed_events, 3_000)
            self.assertEqual((len(logged(service, "FEED_DELAYED")), len(logged(service, "FEED_CAUGHT_UP"))), (1, 1))
            self.assertLessEqual(len(logged(service, "BOOK_STATE")), 2)   # transitions only, not per update
            close(service)


class BoundedTimingMemoryTests(unittest.TestCase):
    def test_latency_statistics_are_exact_and_the_percentile_window_is_bounded(self):
        from unittest import mock
        from crypto_stat_arb import time_integrity
        from crypto_stat_arb.models import MarketEvent
        with mock.patch.object(time_integrity, "LATENCY_WINDOW", 100):
            monitor = time_integrity.TimeIntegrityMonitor(time_integrity.TimePolicy(), ("BYBIT",))
            for i in range(1_000):                                   # latency i ms
                monitor.observe(MarketEvent.from_dict({
                    "event_type": "BOOK_DELTA", "venue": "BYBIT", "instrument_id": "BTC-USDT-SPOT",
                    "exchange_ts_ns": 10_000 * MS, "receive_ts_ns": (10_000 + i) * MS, "sequence": i + 1,
                }), now_ns=(10_000 + i) * MS)
        stats = monitor._venues["BYBIT"]
        self.assertEqual(len(stats.latency_window_ns), 100)           # bounded, not 1,000
        report = monitor.report()["venues"]["BYBIT"]
        self.assertEqual((report["events"], report["latency_ns_min"], report["latency_ns_max"]),
                         (1_000, 0, 999 * MS))                       # exact over all events
        self.assertEqual(report["latency_ns_mean"], int(499.5 * MS))
        self.assertEqual(report["latency_ns_p50"], 949 * MS)          # median of the last 100
        self.assertEqual(report["latency_percentile_window"], 100)


class LogRecordCollisionTests(unittest.TestCase):
    def test_distinct_records_with_the_same_timestamp_are_all_kept(self):
        from crypto_stat_arb.structured_logging import StructuredLogLevel, StructuredLogStore
        with tempfile.TemporaryDirectory() as directory:
            store = StructuredLogStore(Path(directory) / "logs.jsonl", Path(directory) / "logs.sqlite")

            def record(source, payload):
                return StructuredLogStore.connector_record(
                    session_id="s", ts_ns=1_000, event_type="RESYNC_REQUIRED", level=StructuredLogLevel.WARNING,
                    message="sequence gap", venue="BYBIT", source_event_id=source, payload=payload)

            self.assertTrue(store.append(record("e1", {"sequence": 11})))
            self.assertTrue(store.append(record("e2", {"sequence": 12})))      # same tick, different event
            self.assertFalse(store.append(record("e1", {"sequence": 11})))     # true re-delivery: deduplicated
            rows = (Path(directory) / "logs.jsonl").read_text().splitlines()
            self.assertEqual(len(rows), 2)


class SlowSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def run_with_buffered(self, buffered, second_connection, connections_expected=1):
        released = asyncio.Event()

        async def slow_snapshot(url, timeout):
            await released.wait()
            return json.dumps({"lastUpdateId": 100, "bids": [["99", "1"]], "asks": [["102", "1"]]}), 1

        frames = [binance(101 + i, 101 + i) for i in range(buffered)]
        sockets = [FakeWebSocket(frames), *second_connection]
        with tempfile.TemporaryDirectory() as directory:
            service = LiveOrderBookService(
                CONFIG, Path(directory), "slow", LiveFeedSettings(
                    venues=("BINANCE",), durable=False, reconnect_min_seconds=0.001,
                    reconnect_max_seconds=0.002),
                websocket_connector=FakeConnector({"BINANCE": sockets}), http_getter=slow_snapshot,
                clock_ns=StepClock())

            async def release_when_drained():
                while service.stats["BINANCE"].canonical_events < min(buffered, 10_000):
                    await asyncio.sleep(0)
                await asyncio.sleep(0.05)
                released.set()
                stats = service.stats["BINANCE"]        # stop once the expected connection is VALID
                while not (stats.connections >= connections_expected and stats.book_syncs >= 1):
                    await asyncio.sleep(0.001)
                service.request_stop()

            await asyncio.wait_for(asyncio.gather(service.run(), release_when_drained()), timeout=60)
            service.resync_reasons = [r["message"] for r in logged(service, "RESYNC_REQUIRED")]
            return service

    async def test_snapshot_arriving_after_5000_buffered_updates_recovers_them_all(self):
        service = await self.run_with_buffered(5_000, [])
        book = service.books["BINANCE"]
        self.assertEqual((book.state, book.sequence, book.stats.gaps), (BookState.VALID, 5_100, 0))
        self.assertEqual(book.stats.deltas_applied, 5_000)

    async def test_buffer_overflow_while_the_snapshot_is_slow_resyncs_instead_of_trusting_it(self):
        recovery = [FakeWebSocket([binance(101, 101), binance(102, 102), binance(103, 103)])]
        service = await self.run_with_buffered(12_000, recovery, connections_expected=2)
        reasons = service.resync_reasons
        self.assertTrue(reasons and reasons[0].startswith("snapshot_does_not_cover_dropped_deltas"), reasons)
        self.assertEqual(service.stats["BINANCE"].connections, 2)
        self.assertEqual(service.stats["BINANCE"].book_syncs, 1)          # VALID only on the second connection
        book = service.books["BINANCE"]
        self.assertEqual((book.sequence, book.dropped_through), (103, None))


class ReconnectCycleTests(unittest.IsolatedAsyncioTestCase):
    async def cycles(self, count):
        sockets = [ClosedSocket([bybit("snapshot", 1_000 * c, 1_000)] +
                                [bybit("delta", 1_000 * c + i, 1_000 + i) for i in range(1, 21)])
                   for c in range(1, count + 1)]
        sockets.append(FakeWebSocket([bybit("snapshot", 999_000, 2_000), bybit("delta", 999_001, 2_001)]))
        with tempfile.TemporaryDirectory() as directory:
            service = LiveOrderBookService(
                CONFIG, Path(directory), "cycles", LiveFeedSettings(
                    venues=("BYBIT",), durable=False, reconnect_min_seconds=0.0005,
                    reconnect_max_seconds=0.001, max_events=count * 21 + 2),
                websocket_connector=FakeConnector({"BYBIT": sockets}), clock_ns=StepClock())
            await asyncio.wait_for(service.run(), timeout=60)
            return service

    async def test_sixty_reconnect_cycles_leak_neither_tasks_nor_memory(self):
        import gc

        def retained():
            gc.collect()                                       # count only what survives a collection
            return tracemalloc.get_traced_memory()[0]

        await self.cycles(5)                                   # warm caches and imports
        tracemalloc.start()
        try:
            base = retained()
            service = await self.cycles(20)
            after_20 = retained() - base
            del service
            service = await self.cycles(60)
            after_60 = retained() - base
        finally:
            tracemalloc.stop()
        stats = service.stats["BYBIT"]
        self.assertEqual((stats.connections, stats.reconnects, stats.book_syncs), (61, 60, 61))
        self.assertEqual(service.books["BYBIT"].state, BookState.VALID)
        current = asyncio.current_task()
        self.assertEqual([t for t in asyncio.all_tasks() if t is not current], [])    # nothing left running
        # Memory retained after the run must not grow with the number of cycles.
        self.assertLess(abs(after_60 - after_20), 512 * 1024, (after_20, after_60))


class SimultaneousRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_venues_drop_together_and_trading_waits_for_both(self):
        snapshots = iter((100, 200))

        async def snapshot(url, timeout):
            return json.dumps({"lastUpdateId": next(snapshots), "bids": [["99", "1"]], "asks": [["102", "1"]]}), 1

        connector = FakeConnector({
            "BINANCE": [ClosedSocket([binance(101, 101)]), FakeWebSocket([binance(201, 201)])],
            "BYBIT": [ClosedSocket([bybit("snapshot", 10, 1_000), bybit("delta", 11, 1_001)]),
                      FakeWebSocket([bybit("snapshot", 20, 1_002), bybit("delta", 21, 1_003)])],
        })
        with tempfile.TemporaryDirectory() as directory:
            service = LiveOrderBookService(
                CONFIG, Path(directory), "both", LiveFeedSettings(
                    durable=False, reconnect_min_seconds=0.001, reconnect_max_seconds=0.002, max_events=8),
                websocket_connector=connector, http_getter=snapshot, clock_ns=StepClock())
            decisions = []

            class RecordingGate:
                """Every gate decision with the book states it was made on."""

                def __init__(self, gate):
                    self._gate = gate

                def allow(self, books, now_ns, ages_ns=None):
                    allowed = self._gate.allow(books, now_ns, ages_ns)
                    decisions.append((allowed, {b.venue: b.state for b in books}))
                    return allowed

                def __getattr__(self, name):
                    return getattr(self._gate, name)

            service.book_gate = RecordingGate(service.book_gate)
            await asyncio.wait_for(service.run(), timeout=30)
            # Invariant: the gate never allowed trading while either venue was not VALID.
            self.assertFalse([d for d in decisions if d[0] and set(d[1].values()) != {BookState.VALID}])
            self.assertEqual({v: b.state for v, b in service.books.items()},
                             {"BINANCE": BookState.VALID, "BYBIT": BookState.VALID})
            self.assertEqual({v: s.connections for v, s in service.stats.items()}, {"BINANCE": 2, "BYBIT": 2})
            self.assertTrue(decisions[-1][0])                       # trading resumed with both valid
            self.assertTrue(any(not d[0] for d in decisions))       # and was blocked during the drop


if __name__ == "__main__":
    unittest.main()
