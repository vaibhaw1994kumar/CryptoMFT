"""E3 participation cap (owner 13 Nov). Item 1 (print spent once across own orders) is tested in tests/test_fill_engine_queue.py::TestParticipationCap; this file adds the order's remaining tests.
SYNTHETIC books and prints, tests only: nothing here is a profit figure."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from crypto_stat_arb.execution import DeterministicVenueAdapter, ExecutionEventType, OrderType, TimeInForce
from crypto_stat_arb.models import Side
from test_queue_adapter import INSTR, VENUE, S, core_book, request

BIDS = [["100.00", "50"], ["99.99", "100"]]
ASKS = [["100.02", "50"], ["100.03", "100"]]


def ioc_sell(order_id, qty, limit="99.99", ts=2 * S):
    return request(order_id, Side.SELL, limit, qty, tif=TimeInForce.IOC, post_only=False, ts=ts)


class OwnTakerDepthConsumption(unittest.TestCase):
    """Item 2: an own IOC removes the depth it took from the hedge venue's book until a real book update reflects it; a following own order cannot reuse it."""

    def fills(self, callbacks):
        return [c for c in callbacks if c.event_type in (ExecutionEventType.FILLED, ExecutionEventType.PARTIAL)]

    def test_hedge_then_immediate_retry_at_the_same_level_sees_the_reduced_depth(self):
        book = core_book(BIDS, ASKS)
        a = DeterministicVenueAdapter(VENUE, consume_liquidity=True)
        first = self.fills(a.handle(ioc_sell("h1", "40"), book))[-1]
        retry = self.fills(a.handle(ioc_sell("h2", "40", ts=2 * S + 1), book))[-1]          # the same public book object: no update in between
        self.assertEqual((first.cumulative_filled_quantity, first.average_price), (Decimal("40"), Decimal("100.00")))
        # 10 of the 50 at 100.00 are left; the other 30 come from 99.99: (10 x 100.00 + 30 x 99.99) / 40
        self.assertEqual(retry.cumulative_filled_quantity, Decimal("40"))
        self.assertEqual(retry.average_price, (Decimal("10") * Decimal("100.00") + Decimal("30") * Decimal("99.99")) / Decimal("40"))
        self.assertLess(retry.average_price, first.average_price)

    def test_without_consumption_the_retry_reuses_the_same_depth_so_the_test_can_fail(self):
        book = core_book(BIDS, ASKS)
        a = DeterministicVenueAdapter(VENUE, consume_liquidity=False)
        self.fills(a.handle(ioc_sell("h1", "40"), book))
        retry = self.fills(a.handle(ioc_sell("h2", "40", ts=2 * S + 1), book))[-1]
        self.assertEqual(retry.average_price, Decimal("100.00"))                           # the 40 taken first are still there: depth used twice

    def test_a_real_book_update_that_reflects_the_take_releases_it_and_a_new_snapshot_clears_it(self):
        a = DeterministicVenueAdapter(VENUE, consume_liquidity=True)
        book = core_book(BIDS, ASKS)
        self.fills(a.handle(ioc_sell("h1", "40"), book))
        # the venue's book now shows the level reduced by the same 40: our take is reflected, the retry sees 10 at 100.00 (not 10 minus 40 again)
        reflected = core_book([["100.00", "10"], ["99.99", "100"]], ASKS, ts=3 * S, seq=2)
        reflected_retry = self.fills(a.handle(ioc_sell("h2", "10", ts=3 * S + 1), reflected))[-1]
        self.assertEqual((reflected_retry.cumulative_filled_quantity, reflected_retry.average_price), (Decimal("10"), Decimal("100.00")))
        # a fresh snapshot (resync marker) releases every withheld take
        b = DeterministicVenueAdapter(VENUE, consume_liquidity=True)
        book2 = core_book(BIDS, ASKS)
        self.fills(b.handle(ioc_sell("h1", "40"), book2))
        book2.apply(__import__("crypto_stat_arb.models", fromlist=["MarketEvent"]).MarketEvent.from_dict({
            "event_type": "BOOK_SNAPSHOT", "venue": VENUE, "instrument_id": INSTR, "exchange_ts_ns": 4 * S, "receive_ts_ns": 4 * S, "sequence": 3, "bids": BIDS, "asks": ASKS}))
        after = self.fills(b.handle(ioc_sell("h3", "40", ts=4 * S + 1), book2))[-1]
        self.assertEqual(after.average_price, Decimal("100.00"))


if __name__ == "__main__":
    unittest.main()
