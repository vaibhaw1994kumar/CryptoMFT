"""Audit P1s: recording failures stop fail-closed; refused Bybit subscriptions are named and back off."""

import asyncio
import errno
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.cli import _print_live_result
from crypto_stat_arb.live_feed import LiveFeedSettings, LiveOrderBookService, PersistenceFailure
from test_live_feed import FakeConnector, FakeWebSocket, StepClock

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config" / "research.json"


def bybit(kind, u, bids, asks, ts=1000):
    return json.dumps({"topic": "orderbook.50.BTCUSDT", "type": kind, "ts": ts,
                       "data": {"s": "BTCUSDT", "u": u, "b": bids, "a": asks}})


SNAPSHOT = bybit("snapshot", 10, [["100", "1"]], [["101", "1"]])
DELTA = bybit("delta", 11, [["100", "2"]], [], ts=1001)
ACK = json.dumps({"success": True, "ret_msg": "", "conn_id": "c1", "req_id": "", "op": "subscribe"})
REFUSED = json.dumps({"success": False, "ret_msg": "Invalid symbol :[orderbook.50.BTCUSDT]",
                      "conn_id": "c1", "req_id": "", "op": "subscribe"})


class ClosedSocket(FakeWebSocket):
    """Delivers its messages, then drops the connection (as the venue would)."""

    async def recv(self):
        await asyncio.sleep(0)
        if self.messages:
            return self.messages.pop(0)
        raise ConnectionResetError("peer closed")


def service_for(directory, connections, **settings):
    cancels = []
    service = LiveOrderBookService(
        CONFIG, Path(directory), "failures",
        LiveFeedSettings(venues=("BYBIT",), reconnect_min_seconds=0.001,
                         reconnect_max_seconds=0.05, durable=False, **settings),
        websocket_connector=FakeConnector({"BYBIT": connections}),
        clock_ns=StepClock(),
        cancel_callback=lambda reason, now_ns: cancels.append(reason) or 1,
    )
    return service, cancels


def logged(service, event_type):
    path = service.recorder.session_dir / "structured_logs.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()
            if json.loads(line)["event_type"] == event_type]


class PersistenceFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_disk_full_stops_fail_closed_instead_of_reconnecting(self):
        with tempfile.TemporaryDirectory() as directory:
            # One connection only: a reconnect attempt would hit "no fake connection remaining".
            service, cancels = service_for(directory, [FakeWebSocket([SNAPSHOT, DELTA, DELTA])])
            recorder = service.recorder
            flushes = {"n": 0}
            real_flush = recorder._flush

            def flush(handle):
                flushes["n"] += 1
                if flushes["n"] > 6:                    # after the book became valid
                    raise OSError(errno.ENOSPC, "No space left on device")
                real_flush(handle)

            recorder._flush = flush
            result = await asyncio.wait_for(service.run(), timeout=5)

            stats = service.stats["BYBIT"]
            self.assertEqual((stats.connections, stats.reconnects), (1, 0))          # no reconnect loop
            self.assertIn("No space left on device", service.persistence_failure)
            book = service.books["BYBIT"]
            self.assertNotEqual(book.state.value, "VALID")
            self.assertEqual(book.invalid_reason, "persistence_failure")
            self.assertTrue(service.book_gate.suspended)
            self.assertEqual(len(cancels), 1)                                       # open orders cancelled once
            failures = logged(service, "PERSISTENCE_FAILURE")
            self.assertEqual(len(failures), 1)
            self.assertIn("No space left on device", failures[0]["message"])
            summary = result.get("service", result)
            self.assertIn("No space left on device", summary["persistence_failure"])
            with self.assertRaises(PersistenceFailure):                             # stays failed
                recorder.record_control(None, None)

    async def test_structured_log_failure_is_a_persistence_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = service_for(directory, [FakeWebSocket([SNAPSHOT, DELTA])])
            store, real_append, calls = service.structured_logs, service.structured_logs.append, {"n": 0}

            def append(record):
                calls["n"] += 1
                if record.event_type == "BOOK_STATE":
                    raise OSError(errno.EIO, "Input/output error")
                real_append(record)

            store.append = append
            await asyncio.wait_for(service.run(), timeout=5)
            self.assertIn("structured log", service.persistence_failure)
            self.assertEqual(service.stats["BYBIT"].reconnects, 0)

    async def test_network_errors_still_reconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = service_for(
                directory, [ClosedSocket([SNAPSHOT]), FakeWebSocket([SNAPSHOT, DELTA])], max_events=3)
            await asyncio.wait_for(service.run(), timeout=5)
            self.assertEqual(service.persistence_failure, "")
            self.assertEqual(service.stats["BYBIT"].reconnects, 1)
            self.assertEqual(service.books["BYBIT"].state.value, "VALID")

    def test_live_commands_exit_nonzero_on_a_recording_failure(self):
        with self.assertRaises(SystemExit) as stop:
            _print_live_result({"service": {"persistence_failure": "capture_wire: OSError: disk full"}})
        self.assertEqual(stop.exception.code, 2)
        with self.assertRaises(SystemExit):                                         # live-paper shape
            _print_live_result({"live_feed": {"persistence_failure": "x"}})
        _print_live_result({"service": {"persistence_failure": None}})              # clean run: no exit


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_ack_is_counted_and_book_syncs(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = service_for(directory, [FakeWebSocket([ACK, SNAPSHOT, DELTA])], max_events=2)
            await asyncio.wait_for(service.run(), timeout=5)
            stats = service.stats["BYBIT"]
            self.assertEqual((stats.subscription_acks, stats.subscription_rejections), (1, 0))
            self.assertEqual(service.books["BYBIT"].state.value, "VALID")

    async def test_refused_subscription_is_named_and_backs_off(self):
        with tempfile.TemporaryDirectory() as directory:
            refusals = [FakeWebSocket([REFUSED]) for _ in range(3)]
            service, _ = service_for(directory, [*refusals, FakeWebSocket([ACK, SNAPSHOT, DELTA])],
                                     max_events=2)
            await asyncio.wait_for(service.run(), timeout=5)
            stats = service.stats["BYBIT"]
            self.assertEqual(stats.subscription_rejections, 3)
            self.assertIn("Invalid symbol", stats.last_error)
            rejected = logged(service, "SUBSCRIPTION_REJECTED")
            self.assertEqual(len(rejected), 3)
            self.assertIn("Invalid symbol", rejected[0]["message"])
            waits = [row["payload"]["backoff_seconds"] for row in logged(service, "RECONNECT_SCHEDULED")]
            self.assertEqual(waits[:3], [0.001, 0.002, 0.004])     # grows; not reset by the connect
            self.assertEqual(service.books["BYBIT"].state.value, "VALID")   # recovers once accepted

    async def test_a_normal_drop_after_data_still_resets_the_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            drops = [ClosedSocket([SNAPSHOT]) for _ in range(3)]
            service, _ = service_for(directory, [*drops, FakeWebSocket([SNAPSHOT, DELTA])], max_events=5)
            # A hang guard, not a speed limit (windows-latest: one 3.45 s loop step).
            await asyncio.wait_for(service.run(), timeout=30)
            waits = [row["payload"]["backoff_seconds"] for row in logged(service, "RECONNECT_SCHEDULED")]
            self.assertEqual(waits[:3], [0.001, 0.001, 0.001])


if __name__ == "__main__":
    unittest.main()
