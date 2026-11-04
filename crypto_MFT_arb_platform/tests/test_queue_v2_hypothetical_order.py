"""Queue model v2, step 3: ONE trade-confirmed hypothetical order, fixed latency, deterministic fixtures. Pinned before any cancellation model is added.

The order is placed at venue ARRIVAL behind the full displayed size (ahead A = Q); later external additions join behind it; a print that executed before
arrival never fills it; PostOnly is judged on the arrival-time book; a fill needs an eligible print once the queue ahead is exhausted."""
from __future__ import annotations

import unittest

from qv2_support import delta, make_book, make_executor, place, sell_print

from new_backtest.event_loader import BOOK
from test_maker_order_lifetime import BINANCE, BYBIT, Harness
from test_postonly_reject import harness, poke, send, two_books

CFG = dict(cancel_model="proportional", fill_evidence="off", exchange_time_eligibility=True, dedupe_trade_depth=True, share_trade_volume=True)


class HypotheticalOrder(unittest.TestCase):
    def setUp(self):
        self.ex = make_executor(**CFG)
        self.book = make_book(bids=[(100.0, 50.0), (99.0, 100.0)])

    def test_it_joins_behind_the_full_displayed_size(self):
        o = place(self.ex, self.book, price=100.0, size=10.0)
        self.assertEqual((o.queue_ahead, o.initial_queue_ahead, o.observed_level_size), (50.0, 50.0, 50.0))

    def test_later_external_additions_join_behind_it_and_do_not_change_the_queue_ahead(self):
        o = place(self.ex, self.book, price=100.0, size=10.0)
        self.assertEqual(delta(self.ex, self.book, "bid", 100.0, 80.0, 2.0), [])
        self.assertEqual(o.queue_ahead, 50.0)                      # 30 arrived behind us
        self.assertEqual(o.level_now, 80.0)

    def test_a_print_that_executed_before_arrival_never_fills_it(self):
        o = place(self.ex, self.book, price=100.0, size=10.0, now=1.0, active_ts=1.0, arrival_ts=5.0)
        fills = sell_print(self.ex, self.book, 100.0, 500.0, now=5.1, event_ts=4.9)       # received after arrival, executed before it
        self.assertEqual(fills, [])
        self.assertEqual((o.filled_qty, o.remaining), (0.0, 10.0))

    def test_a_print_at_or_after_arrival_can_fill_but_only_once_the_queue_ahead_is_exhausted(self):
        o = place(self.ex, self.book, price=100.0, size=10.0, now=1.0, arrival_ts=5.0)
        self.assertEqual(sell_print(self.ex, self.book, 100.0, 30.0, now=6.0, event_ts=5.5), [])
        self.assertAlmostEqual(o.queue_ahead, 20.0)                # the print burned 30 of the 50 ahead
        (f,) = sell_print(self.ex, self.book, 100.0, 30.0, now=7.0, event_ts=6.5)
        self.assertAlmostEqual(f.qty, 10.0)                        # 20 exhausted the queue, the last 10 filled us
        self.assertEqual(f.cause, "trade")
        self.assertAlmostEqual(o.remaining, 0.0)

    def test_arrival_time_itself_is_eligible(self):
        place(self.ex, self.book, price=100.0, size=10.0, now=1.0, arrival_ts=5.0)
        self.ex.orders[next(iter(self.ex.orders))].queue_ahead = 0.0
        self.assertEqual(len(sell_print(self.ex, self.book, 100.0, 4.0, now=5.0, event_ts=5.0)), 1)

    def test_a_print_below_our_price_does_not_reach_a_bid_resting_above_it(self):
        place(self.ex, self.book, price=100.0, size=10.0)
        self.ex.orders[next(iter(self.ex.orders))].queue_ahead = 0.0
        self.assertEqual(sell_print(self.ex, self.book, 101.0, 4.0, now=2.0), [])           # a sell at 101 is above a bid at 100: no cross

    def test_postonly_is_judged_on_the_arrival_time_book_not_the_send_time_book(self):
        # send a bid at 100 into ask 101 (fine at send time); the ask drops to 100 BEFORE the order arrives (t=0.013): rejected
        h = harness()
        two_books(h, bids=((99.0, 5.0),), asks=((101.0, 5.0),))
        send(h, "buy", 100.0)
        h.event(0.005, BOOK, BYBIT, {"bids": [[99.0, 5.0]], "asks": [[100.0, 5.0]]}, exchange_ts=0.005)
        poke(h, 0.03)
        self.assertEqual(h.resting, [])
        self.assertEqual(h.bt.po["rejects"], 1)

    def test_a_book_that_no_longer_crosses_at_arrival_lets_the_order_rest(self):
        # a bid at 100 into ask 100 would cross at send time; the ask moves to 101 before arrival: accepted and resting
        h = harness()
        two_books(h, bids=((99.0, 5.0),), asks=((100.0, 5.0),))
        send(h, "buy", 100.0)
        h.event(0.005, BOOK, BYBIT, {"bids": [[99.0, 5.0]], "asks": [[101.0, 5.0]]}, exchange_ts=0.005)
        poke(h, 0.03)
        self.assertEqual(len(h.resting), 1)
        self.assertEqual(h.bt.po["rejects"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
