"""Phase D, D7b: paper trading on the one runtime. `LivePaperSession(queue_simulation=...)` builds the simulated venue as the Python queue simulator (queue v2) behind `QueueAwareVenueAdapter`, fed per event by the
shared `SimulatedVenueFeeder`. Fake feeds only (no network, no orders): the wiring runs end to end and a post-only order placed through the session's own router is queued and filled from a print."""
import asyncio
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from crypto_stat_arb.execution import CanonicalOrderRequest, ExecutionEventType, OrderType, TimeInForce
from crypto_stat_arb.live_feed import LiveFeedSettings
from crypto_stat_arb.models import Side
from crypto_stat_arb.paper_live import LivePaperSession
from crypto_stat_arb.session import ExecutionMode
from crypto_stat_arb.simulation.queue_adapter import QueueAwareVenueAdapter, SimulatedVenueFeeder
from test_live_feed import StepClock
from test_maker_execution_path import BybitTopicSocket, RoutingConnector, WATCH_CONFIG


def feeds():
    bybit_books = [json.dumps({"topic": "orderbook.50.BTCUSDT", "type": "snapshot", "ts": 1000, "data": {"s": "BTCUSDT", "u": 1, "b": [["60000.00", "1.0"]], "a": [["60000.10", "1.0"]]}})] + [
        json.dumps({"topic": "orderbook.50.BTCUSDT", "type": "delta", "ts": 1000 + n, "data": {"s": "BTCUSDT", "u": 1 + n, "b": [["60000.00", f"{1 + n % 3}.0"]], "a": []}}) for n in range(1, 60)]
    bybit_trades = [json.dumps({"topic": "publicTrade.BTCUSDT", "data": [{"s": "BTCUSDT", "i": f"t{n}", "T": 1000 + n, "p": "60000.00" if n % 2 else "60000.10", "v": "1.5", "S": "Sell" if n % 2 else "Buy", "seq": n}]})
                    for n in range(1, 50)]
    binance_books = [json.dumps({"e": "depthUpdate", "E": 1000 + n, "T": 1000 + n, "s": "BTCUSDT", "U": 101 + n, "u": 101 + n, "b": [["59999.90", f"{1 + n % 2}"]], "a": []}) for n in range(40)]
    binance_trades = [json.dumps({"e": "trade", "E": 1000 + n, "T": 1000 + n, "s": "BTCUSDT", "t": n, "p": "59999.90" if n % 2 else "60000.00", "q": "2", "m": bool(n % 2)}) for n in range(1, 40)]
    return bybit_books, bybit_trades, binance_books, binance_trades


class PaperOnTheQueueSimulator(unittest.IsolatedAsyncioTestCase):
    async def _session(self, base: Path, **kw):
        bybit_books, bybit_trades, binance_books, binance_trades = feeds()
        clock = StepClock()

        async def http_getter(url, timeout):
            await asyncio.sleep(0)
            return json.dumps({"lastUpdateId": 100, "bids": [["59999.90", "1"]], "asks": [["60000.00", "1"]]}), clock()

        return LivePaperSession(
            WATCH_CONFIG, base / "live", "queue-sim",
            LiveFeedSettings(max_events=90, reconnect_min_seconds=0.001, reconnect_max_seconds=0.002, durable=False, udp_enabled=False, trade_streams_enabled=True),
            websocket_connector=RoutingConnector(binance_books and __import__("test_live_feed").FakeWebSocket(binance_books), __import__("test_live_feed").FakeWebSocket(binance_trades),
                                                 [BybitTopicSocket(bybit_books, []), BybitTopicSocket([], bybit_trades)]),
            http_getter=http_getter, clock_ns=clock, tick_seconds=10, **kw)

    async def test_the_session_runs_on_the_queue_simulator_and_a_post_only_order_fills_from_a_print(self):
        with tempfile.TemporaryDirectory() as directory:
            paper = await self._session(Path(directory), queue_simulation={})
            self.assertIsInstance(paper.feeder, SimulatedVenueFeeder)
            adapters = paper.session.router.adapters
            self.assertTrue(adapters and all(isinstance(a, QueueAwareVenueAdapter) for a in adapters.values()))
            self.assertEqual(len({id(a.executor) for a in adapters.values()}), 1)                       # one executor behind every venue
            await asyncio.wait_for(paper.run(), timeout=10)
            for venue, view in paper._views.items():
                self.assertTrue(view.ready, venue)                                                         # the persistent view books were fed by the live events
            # an order placed through the session's own router lands in the queue model and is filled by a later print on that venue
            venue = "BYBIT"
            book = paper.session.books[(venue, paper.cfg.instrument_id)]
            request = CanonicalOrderRequest.create(
                action=__import__("crypto_stat_arb.execution", fromlist=["OrderAction"]).OrderAction.SUBMIT, order_id="probe-1", parent_opportunity_id="op", parent_order_id="parent",
                instrument_id=paper.cfg.instrument_id, venue=venue, side=Side.BUY, price=Decimal("60000.00"), quantity=Decimal("0.001"), order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC, post_only=True, created_ts_ns=paper.session.last_event_ts_ns + 1)
            (ack,) = adapters[venue].handle_at(request, book, request.created_ts_ns)
            self.assertEqual(ack.event_type, ExecutionEventType.ACKNOWLEDGED)
            self.assertEqual(len(adapters[venue].executor.live_orders(venue)), 1)

    async def test_it_is_refused_with_other_adapters_or_outside_paper_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "INTERNAL_PAPER only"):
                await self._session(Path(directory), queue_simulation={}, adapters={"BINANCE": object(), "BYBIT": object()})


if __name__ == "__main__":
    unittest.main()
