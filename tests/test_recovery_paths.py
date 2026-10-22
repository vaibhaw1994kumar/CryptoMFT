"""Audit Phase 2: recovery paths that had no tests (snapshot failure/staleness, races,
buffer overflow, empty sides, sequence resets, repeated recovery failure)."""

import asyncio
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.live_feed import LiveFeedSettings, LiveOrderBookService
from crypto_stat_arb.models import BookState, MarketEvent
from crypto_stat_arb.orderbook import OrderBook
from test_live_feed import FakeConnector, FakeWebSocket, StepClock

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config" / "research.json"


def binance_delta(first, last, bids=(("100.0", "2"),), asks=()):
    return json.dumps({"e": "depthUpdate", "E": 1000 + last, "T": 1000 + last, "s": "BTCUSDT",
                       "U": first, "u": last, "b": [list(x) for x in bids], "a": [list(x) for x in asks]})


def binance_snapshot(last_update_id, bids=(("99.9", "1"),), asks=(("101.0", "1"),)):
    return json.dumps({"lastUpdateId": last_update_id, "bids": [list(x) for x in bids],
                       "asks": [list(x) for x in asks]})


def bybit(kind, u, bids, asks):
    return json.dumps({"topic": "orderbook.50.BTCUSDT", "type": kind, "ts": 1000 + u,
                       "data": {"s": "BTCUSDT", "u": u, "b": bids, "a": asks}})


class SnapshotScript:
    """REST snapshot replies in order; an Exception instance is raised, ``None`` never returns."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def __call__(self, url, timeout):
        self.calls += 1
        reply = self.replies.pop(0)
        if reply is None:
            await asyncio.Event().wait()
        if isinstance(reply, BaseException):
            raise reply
        return reply, 1_000_000_000 + self.calls


def make_service(directory, venues, connections, *, http=None, **settings):
    cancels = []
    defaults = dict(reconnect_min_seconds=0.001, reconnect_max_seconds=0.05, durable=False)
    service = LiveOrderBookService(
        CONFIG, Path(directory), "recovery", LiveFeedSettings(venues=venues, **{**defaults, **settings}),
        websocket_connector=FakeConnector(connections), http_getter=http or SnapshotScript([]),
        clock_ns=StepClock(), cancel_callback=lambda reason, now: cancels.append(reason) or 0,
    )
    return service, cancels


def logged(service, event_type):
    path = service.recorder.session_dir / "structured_logs.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    return [row for row in rows if row["event_type"] == event_type]


def book_states(service, venue):
    return [row["payload"]["state"] for row in logged(service, "BOOK_STATE") if row["venue"] == venue]


def states_before_last_resync(service, venue):
    """Book states logged before the last failure/resync of ``venue`` (i.e. before recovery)."""
    path = service.recorder.session_dir / "structured_logs.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    failures = [i for i, row in enumerate(rows) if row["venue"] == venue
                and row["event_type"] in ("RESYNC_REQUIRED", "CONNECTOR_ERROR")]
    return [row["payload"]["state"] for row in rows[:failures[-1]]
            if row["event_type"] == "BOOK_STATE" and row["venue"] == venue]


class BinanceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_request_failures_back_off_and_recover(self):
        http = SnapshotScript([ConnectionError("HTTP 503"), TimeoutError("snapshot timed out"),
                               ConnectionError("HTTP 503"), binance_snapshot(100)])
        sockets = [FakeWebSocket([binance_delta(101, 101)]) for _ in range(4)]
        with tempfile.TemporaryDirectory() as directory:
            # events: one delta per failed connection (3) + snapshot and delta on the 4th
            service, _ = make_service(directory, ("BINANCE",), {"BINANCE": sockets}, http=http, max_events=5)
            await asyncio.wait_for(service.run(), timeout=5)
            book = service.books["BINANCE"]
            self.assertEqual((book.state, book.sequence), (BookState.VALID, 101))
            self.assertEqual(http.calls, 4)
            self.assertEqual(len(logged(service, "CONNECTOR_ERROR")), 3)
            self.assertEqual(states_before_last_resync(service, "BINANCE").count("VALID"), 0)
            # Connections that never produced a valid book are not progress: the backoff grows.
            waits = [row["payload"]["backoff_seconds"] for row in logged(service, "RECONNECT_SCHEDULED")]
            self.assertEqual(waits[:3], [0.001, 0.002, 0.004])

    async def test_silent_feed_while_snapshot_hangs_resyncs(self):
        http = SnapshotScript([None, binance_snapshot(100)])
        sockets = [FakeWebSocket([]), FakeWebSocket([binance_delta(101, 101)])]
        with tempfile.TemporaryDirectory() as directory:
            service, _ = make_service(directory, ("BINANCE",), {"BINANCE": sockets}, http=http,
                                      initial_sync_timeout_seconds=0.05, max_events=2)
            await asyncio.wait_for(service.run(), timeout=5)
            reasons = [row["message"] for row in logged(service, "RESYNC_REQUIRED")]
            self.assertEqual(reasons[0], "market_data_stale_while_fetching_snapshot")
            self.assertEqual(service.books["BINANCE"].state, BookState.VALID)

    async def test_snapshot_older_than_buffered_deltas_is_a_gap_not_a_valid_book(self):
        # Buffered deltas start at U=105, but the snapshot only reaches lastUpdateId=100:
        # updates 101..104 are missing, so the book must not go VALID.
        http = SnapshotScript([binance_snapshot(100), binance_snapshot(200)])
        sockets = [FakeWebSocket([binance_delta(105, 110)]),
                   FakeWebSocket([binance_delta(195, 201), binance_delta(202, 202)])]
        with tempfile.TemporaryDirectory() as directory:
            service, _ = make_service(directory, ("BINANCE",), {"BINANCE": sockets}, http=http, max_events=5)
            await asyncio.wait_for(service.run(), timeout=5)
            first = logged(service, "RESYNC_REQUIRED")[0]["message"]
            self.assertTrue(first.startswith("buffered_sequence_gap:100->105"), first)
            self.assertNotIn("VALID", states_before_last_resync(service, "BINANCE"))  # only after recovery
            book = service.books["BINANCE"]
            self.assertEqual((book.state, book.sequence), (BookState.VALID, 202))

    async def test_snapshot_with_an_empty_side_resyncs(self):
        http = SnapshotScript([binance_snapshot(100, asks=()), binance_snapshot(200)])
        sockets = [FakeWebSocket([binance_delta(101, 101)]), FakeWebSocket([binance_delta(201, 201)])]
        with tempfile.TemporaryDirectory() as directory:
            service, _ = make_service(directory, ("BINANCE",), {"BINANCE": sockets}, http=http, max_events=4)
            await asyncio.wait_for(service.run(), timeout=5)
            self.assertEqual(logged(service, "RESYNC_REQUIRED")[0]["message"], "empty_book_side")
            self.assertEqual(service.books["BINANCE"].state, BookState.VALID)


class BybitRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_side_blocks_trading_until_refilled_without_losing_sequence(self):
        frames = [bybit("snapshot", 10, [["100", "1"]], [["101", "1"]]),
                  bybit("delta", 11, [], [["101", "0"]]),                 # last ask removed
                  bybit("delta", 12, [], [["101.5", "3"]])]               # ask side refilled
        with tempfile.TemporaryDirectory() as directory:
            service, cancels = make_service(directory, ("BYBIT",), {"BYBIT": [FakeWebSocket(frames)]},
                                            max_events=3)
            await asyncio.wait_for(service.run(), timeout=5)
            self.assertEqual(book_states(service, "BYBIT"), ["VALID", "SYNCING", "VALID"])
            gate = service.book_gate
            self.assertEqual((gate.suspend_count, gate.resume_count), (1, 2))   # blocked, then resumed
            self.assertEqual(len(cancels), 1)
            book = service.books["BYBIT"]
            self.assertEqual((book.sequence, book.stats.gaps, service.stats["BYBIT"].reconnects), (12, 0, 0))

    async def test_update_id_reset_by_a_new_snapshot(self):
        # Bybit: "u"=1 arrives as a snapshot after a service restart; it replaces the book.
        frames = [bybit("snapshot", 500, [["100", "1"], ["99", "1"]], [["101", "1"]]),
                  bybit("delta", 501, [["100", "2"]], []),
                  bybit("snapshot", 1, [["100", "7"]], [["101", "1"]]),          # 99 no longer listed
                  bybit("delta", 2, [["100", "5"]], [])]
        with tempfile.TemporaryDirectory() as directory:
            service, _ = make_service(directory, ("BYBIT",), {"BYBIT": [FakeWebSocket(frames)]}, max_events=4)
            await asyncio.wait_for(service.run(), timeout=5)
            book = service.books["BYBIT"]
            self.assertEqual((book.state, book.sequence, book.stats.gaps), (BookState.VALID, 2, 0))
            self.assertEqual(dict(book.bids), {Decimal("100"): Decimal("5")})     # 99 removed by the snapshot

    async def test_repeated_gaps_back_off_until_a_valid_book(self):
        gap = [bybit("snapshot", 10, [["100", "1"]], [["101", "1"]]), bybit("delta", 12, [], [])]
        sockets = [FakeWebSocket(gap) for _ in range(2)]
        sockets.append(FakeWebSocket([bybit("snapshot", 30, [["100", "1"]], [["101", "1"]]),
                                      bybit("delta", 31, [["100", "2"]], [])]))
        with tempfile.TemporaryDirectory() as directory:
            service, _ = make_service(directory, ("BYBIT",), {"BYBIT": sockets}, max_events=6)
            await asyncio.wait_for(service.run(), timeout=5)
            self.assertEqual(service.stats["BYBIT"].resyncs, 2)
            self.assertEqual(service.books["BYBIT"].state, BookState.VALID)
            # These connections did reach VALID before the gap, so each recovery starts from the minimum.
            waits = [row["payload"]["backoff_seconds"] for row in logged(service, "RECONNECT_SCHEDULED")]
            self.assertEqual(waits[:2], [0.001, 0.001])


def delta(sequence, start=None):
    return MarketEvent.from_dict({
        "event_id": f"d{sequence}", "event_type": "BOOK_DELTA", "venue": "BINANCE",
        "instrument_id": "BTC-USDT-SPOT", "exchange_ts_ns": sequence, "receive_ts_ns": sequence,
        "sequence": sequence, "sequence_start": start if start is not None else sequence,
        "bids": [["100", "1"]], "asks": [["101", "1"]],
    })


def snapshot(sequence):
    return MarketEvent.from_dict({
        "event_id": f"s{sequence}", "event_type": "BOOK_SNAPSHOT", "venue": "BINANCE",
        "instrument_id": "BTC-USDT-SPOT", "exchange_ts_ns": sequence, "receive_ts_ns": sequence,
        "sequence": sequence, "bids": [["100", "1"]], "asks": [["101", "1"]],
    })


class BufferOverflowTests(unittest.TestCase):
    def test_overflow_is_a_gap(self):
        book = OrderBook("BINANCE", "BTC-USDT-SPOT", max_pending_deltas=3)
        for sequence in range(101, 106):
            book.apply(delta(sequence))
        self.assertEqual((book.state, book.invalid_reason), (BookState.GAP_DETECTED, "delta_buffer_overflow"))
        self.assertEqual(book.stats.gaps, 1)

    def test_snapshot_that_does_not_cover_dropped_deltas_is_not_valid(self):
        # 101..103 kept, 104..105 dropped on overflow. A snapshot at 100 would replay 101..103 and
        # report VALID while 104..105 are known to be missing.
        book = OrderBook("BINANCE", "BTC-USDT-SPOT", max_pending_deltas=3)
        for sequence in range(101, 106):
            book.apply(delta(sequence))
        self.assertNotEqual(book.apply(snapshot(100)), BookState.VALID)
        self.assertNotEqual(book.state, BookState.VALID)

    def test_snapshot_that_covers_dropped_deltas_recovers(self):
        book = OrderBook("BINANCE", "BTC-USDT-SPOT", max_pending_deltas=3)
        for sequence in range(101, 106):
            book.apply(delta(sequence))
        self.assertEqual(book.apply(snapshot(105)), BookState.VALID)
        self.assertEqual(book.apply(delta(106)), BookState.VALID)


if __name__ == "__main__":
    unittest.main()
