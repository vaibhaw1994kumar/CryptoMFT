"""Opt-in through-print rule and the depth-event size fix.

through-print rule (MakerConfig.fill_through_prints, default off): a public print strictly through our price means the level
we rest on was consumed, so the order fills in full (price priority), as in the core queue engines. The print's volume is
not charged to the shared budget, and an event that executed before the order arrived still cannot fill it.

depth-event size (ArbBacktestConfig.delta_uses_book_size, default on): the maker engine must be told the size LEFT at the
level. In the hybrid collector format a 'cancel' carries the size that was removed, so passing the raw field made level
removals invisible and partial cancels look like larger drains.
"""
from __future__ import annotations

import unittest

from new_backtest.event_loader import DELTA
from new_backtest.fill_engine import MakerConfig

from test_fill_engine_queue import _book, _executor, _place
from test_maker_order_lifetime import BINANCE, BYBIT, Harness


def bid_order(**cfg):
    ex = _executor(MakerConfig(**cfg))
    o = _place(ex, _book(bids=[(100.0, 10.0)]), price=100.0, size=5.0)
    o.arrival_ts = 0.0
    return ex, o


class ThroughPrintTests(unittest.TestCase):
    def test_default_a_print_through_our_price_only_burns_queue(self):
        ex, o = bid_order()
        fills = ex.on_trade("bybit", 99.0, 1.0, -1, 2.0, 0.0, event_ts=2.0)
        self.assertEqual((fills, o.queue_ahead, o.filled_qty), ([], 9.0, 0.0))

    def test_opt_in_fills_in_full_and_clears_the_queue(self):
        ex, o = bid_order(fill_through_prints=True)
        fills = ex.on_trade("bybit", 99.0, 1.0, -1, 2.0, 0.0, event_ts=2.0)
        self.assertEqual((len(fills), fills[0].qty, o.queue_ahead, o.filled_qty, ex.orders), (1, 5.0, 0.0, 5.0, {}))
        self.assertEqual(ex.diag["fills_from_through_print"], 5.0)

    def test_a_print_at_our_own_price_is_not_through(self):
        ex, o = bid_order(fill_through_prints=True)
        fills = ex.on_trade("bybit", 100.0, 4.0, -1, 2.0, 0.0, event_ts=2.0)
        self.assertEqual((fills, o.queue_ahead), ([], 6.0))

    def test_a_print_better_for_the_aggressor_on_the_ask_side_mirrors(self):
        ex = _executor(MakerConfig(fill_through_prints=True))
        o = _place(ex, _book(asks=[(101.0, 10.0)]), side=__import__("new_backtest.models", fromlist=["Side"]).Side.ASK,
                   price=101.0, size=5.0)
        o.arrival_ts = 0.0
        fills = ex.on_trade("bybit", 102.0, 0.5, +1, 2.0, 0.0, event_ts=2.0)       # a buy aggressor lifting above our offer
        self.assertEqual((len(fills), fills[0].qty), (1, 5.0))

    def test_it_does_not_consume_the_shared_print_budget(self):
        ex = _executor(MakerConfig(fill_through_prints=True))
        book = _book(bids=[(100.0, 10.0)])
        a = _place(ex, book, price=100.0, size=5.0)
        b = _place(ex, book, price=100.0, size=5.0)
        a.arrival_ts = b.arrival_ts = 0.0
        fills = ex.on_trade("bybit", 99.0, 0.1, -1, 2.0, 0.0, event_ts=2.0)
        self.assertEqual(sorted(f.qty for f in fills), [5.0, 5.0])

    def test_an_event_before_the_order_arrived_still_cannot_fill_it(self):
        ex, o = bid_order(fill_through_prints=True)
        o.arrival_ts = 5.0
        fills = ex.on_trade("bybit", 99.0, 1.0, -1, 6.0, 0.0, event_ts=4.0)        # executed at t=4, order arrived t=5
        self.assertEqual((fills, o.filled_qty), ([], 0.0))
        self.assertEqual(o.queue_ahead, 9.0)


class DepthEventSizeTests(unittest.TestCase):
    def _setup(self, legacy):
        h = Harness(ttl=0.0, queue=0.5)
        h.bt.config.delta_uses_book_size = not legacy
        h.book(0.0, BYBIT)                           # bids 99 x5, 98 x5; asks 101 x5, 102 x5
        h.book(0.0, BINANCE)
        h.send_maker(0.0, price=99.0)                # joins an existing level of 5
        h.advance(0.003)
        o = h.order()
        self.assertEqual((o.queue_ahead, o.observed_level_size), (5.0, 5.0))
        return h, o

    def _cancel(self, h, t, volume):
        h.event(t, DELTA, BYBIT, {"side": "bid", "price": 99.0, "volume": volume, "event_type": "cancel"})

    def test_a_cancel_that_removes_the_whole_level_drains_the_queue(self):
        h, o = self._setup(legacy=False)
        self._cancel(h, 0.1, 5.0)                    # hybrid format: volume = the size that was removed
        self.assertAlmostEqual(o.queue_ahead, 0.0, places=9)

    def test_legacy_ignores_the_removal(self):
        h, o = self._setup(legacy=True)
        self._cancel(h, 0.1, 5.0)
        self.assertAlmostEqual(o.queue_ahead, 5.0, places=9)

    def test_a_partial_cancel_drains_only_what_was_removed(self):
        h, o = self._setup(legacy=False)
        self._cancel(h, 0.1, 2.0)                    # 5 -> 3
        self.assertAlmostEqual(o.queue_ahead, 3.0, places=9)

    def test_legacy_over_drains_a_partial_cancel(self):
        h, o = self._setup(legacy=True)
        self._cancel(h, 0.1, 2.0)                    # raw volume 2 is read as the new size: a shrink of 3
        self.assertAlmostEqual(o.queue_ahead, 2.0, places=9)

    def test_absolute_feeds_are_unchanged(self):
        for legacy in (False, True):
            h, o = self._setup(legacy)
            h.event(0.1, DELTA, BYBIT, {"side": "bid", "price": 99.0, "volume": 3.0, "event_type": "modify"})
            self.assertAlmostEqual(o.queue_ahead, 3.0, places=9, msg=f"legacy={legacy}")


if __name__ == "__main__":
    unittest.main()
