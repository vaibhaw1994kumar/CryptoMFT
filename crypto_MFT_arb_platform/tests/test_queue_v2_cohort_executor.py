"""Queue model v2, step 6: queue_model='cohort' inside MakerExecutor (the same events, the cohort queue instead of one number)."""
from __future__ import annotations

import unittest

from qv2_support import delta, make_book, make_executor, place, sell_print

from new_backtest.fill_engine import MakerConfig
from new_backtest.models import Side

BASE = dict(queue_model="cohort", fill_evidence="off", dedupe_trade_depth=True, share_trade_volume=True, exchange_time_eligibility=True)


def worked(age):
    ex = make_executor(cohort_age=age, **BASE)
    book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
    o = place(ex, book, price=100.0, size=2.0)
    delta(ex, book, "bid", 100.0, 15.0, 1.0)                      # 5 arrive behind us
    delta(ex, book, "bid", 100.0, 11.0, 2.0)                      # residual cancellation of 4
    fills = sell_print(ex, book, 100.0, 8.0, 3.0)                 # an 8-unit print, outside the netting window
    return sum(f.qty for f in fills), o, ex


class Executor(unittest.TestCase):
    def test_the_guides_worked_example_through_the_executor(self):
        self.assertAlmostEqual(worked("youngest_first")[0], 0.0)
        self.assertAlmostEqual(worked("uniform")[0], 2.0 / 3.0, places=9)
        self.assertAlmostEqual(worked("oldest_first")[0], 2.0)
        self.assertAlmostEqual(worked("hazard")[0], worked("hazard")[0])

    def test_scalar_default_is_unchanged_and_cohort_needs_a_valid_age(self):
        self.assertEqual(MakerConfig().queue_model, "scalar")
        with self.assertRaises(ValueError):
            MakerConfig(queue_model="cohort", cohort_age="random")
        with self.assertRaises(ValueError):
            MakerConfig(queue_model="cohort", opposite_cross_fills=True)
        with self.assertRaises(ValueError):
            MakerConfig(queue_model="bogus")

    def test_two_own_orders_at_one_level_share_one_traversal_and_one_print(self):
        ex = make_executor(cohort_age="uniform", **BASE)
        book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
        a = place(ex, book, price=100.0, size=2.0, now=1.0)
        b = place(ex, book, price=100.0, size=2.0, now=1.5)
        self.assertEqual((a.queue_ahead, b.queue_ahead), (10.0, 12.0))               # the second sits behind the first
        fills = sell_print(ex, book, 100.0, 13.0, 3.0)
        by = {f.order_id: f.qty for f in fills}
        self.assertAlmostEqual(by[a.order_id], 2.0)
        self.assertAlmostEqual(by[b.order_id], 1.0)                                    # 13 = 10 ext + 2 first + 1 into the second: one print, one budget
        self.assertAlmostEqual(sum(by.values()), 3.0)

    def test_one_print_cannot_fill_two_orders_beyond_its_quantity(self):
        ex = make_executor(cohort_age="uniform", **BASE)
        book = make_book(bids=[(100.0, 0.5), (99.0, 100.0)])
        a = place(ex, book, price=100.0, size=3.0, now=1.0)
        b = place(ex, book, price=100.0, size=3.0, now=1.5)
        total = sum(f.qty for f in sell_print(ex, book, 100.0, 4.0, 3.0))
        self.assertLessEqual(total, 4.0 - 0.5 + 1e-9)                                  # 0.5 of the 4 went to the cohort ahead of both
        self.assertGreaterEqual(min(a.remaining, b.remaining), 0.0)

    def test_a_partial_fill_keeps_priority_and_the_remaining_adds_up(self):
        ex = make_executor(cohort_age="uniform", **BASE)
        book = make_book(bids=[(100.0, 0.0001), (99.0, 100.0)])
        a = place(ex, book, price=100.0, size=3.0, now=1.0)
        b = place(ex, book, price=100.0, size=3.0, now=1.5)
        sell_print(ex, book, 100.0, 2.0, 3.0)
        self.assertGreater(a.filled_qty, 0.0)
        self.assertAlmostEqual(a.filled_qty + a.remaining, 3.0)
        self.assertAlmostEqual(b.filled_qty, 0.0, places=3)                            # the second only gets what the first leaves
        self.assertAlmostEqual(b.queue_ahead, a.remaining, places=3)

    def test_a_snapshot_reset_re_bases_the_external_size_and_never_cancels_an_own_order(self):
        ex = make_executor(cohort_age="uniform", **BASE)
        book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
        o = place(ex, book, price=100.0, size=2.0)
        book.apply_snapshot([(99.0, 100.0)], [(101.0, 50.0)], 5.0)                    # the snapshot shows no size at our price
        ex.on_snapshot(book.venue, book, 5.0, 1.0, 0.0, event_ts=5.0)
        self.assertIn(o.order_id, ex.orders)                                            # still live
        self.assertTrue(o.is_live)
        self.assertAlmostEqual(o.queue_ahead, 0.0)

    def test_book_first_and_trade_first_converge_without_double_depletion(self):
        out = []
        for first in ("book", "trade"):
            ex = make_executor(cohort_age="youngest_first", **BASE)
            book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
            o = place(ex, book, price=100.0, size=2.0)
            delta(ex, book, "bid", 100.0, 15.0, 0.5)                                  # 5 behind us
            if first == "book":
                delta(ex, book, "bid", 100.0, 11.0, 2.000)                           # 4 executed at the head ...
                sell_print(ex, book, 100.0, 4.0, 2.010)                               # ... and here is its print
            else:
                sell_print(ex, book, 100.0, 4.0, 2.000)
                delta(ex, book, "bid", 100.0, 11.0, 2.010)
            out.append((round(o.queue_ahead, 9), round(o.filled_qty, 9)))
        self.assertEqual(out[0], out[1])
        self.assertEqual(out[0], (6.0, 0.0))

    def test_a_vanished_level_alone_never_yields_a_trade_confirmed_fill(self):
        ex = make_executor(cohort_age="uniform", **{**BASE, "fill_evidence": "trade_confirm", "vanish_clears_queue": True})
        book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
        o = place(ex, book, price=100.0, size=2.0)
        for t, size in ((2.0, 6.0), (3.0, 0.0), (4.0, 0.0)):
            self.assertEqual(delta(ex, book, "bid", 100.0, size, t), [])
        self.assertEqual(o.filled_qty, 0.0)

    def test_a_vanished_level_with_size_behind_us_does_not_fill_without_a_print_and_the_legacy_rule_does(self):
        res = {}
        for ev in ("trade_confirm", "off"):
            ex = make_executor(cohort_age="uniform", **{**BASE, "fill_evidence": ev, "vanish_clears_queue": True})
            book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
            o = place(ex, book, price=100.0, size=4.0)
            delta(ex, book, "bid", 100.0, 20.0, 1.0)
            delta(ex, book, "bid", 100.0, 0.0, 3.0)
            res[ev] = o.filled_qty
        self.assertEqual(res["trade_confirm"], 0.0)
        self.assertGreater(res["off"], 0.0)

    def test_a_coarse_tick_estimate_never_merges_two_adjacent_levels(self):
        # tick inferred from a sparse first snapshot can be 2x too large; the level-match tolerance (0.5 tick) then spans two real levels
        ex = make_executor(cohort_age="uniform", **BASE)
        book = make_book(bids=[(100.0, 10.0), (99.5, 8.0), (99.0, 50.0)])
        a = place(ex, book, price=100.0, size=2.0, tick=1.0)
        b = place(ex, book, price=99.5, size=2.0, tick=1.0)
        fills = delta(ex, book, "bid", 100.0, 4.0, 2.0, tick=1.0)                       # tolerance 0.5 covers both 100.0 and 99.5 with this tick
        self.assertEqual(fills, [])
        self.assertAlmostEqual(a.queue_ahead, 4.0)                                      # a lost 6 ahead of it
        self.assertAlmostEqual(b.queue_ahead, 8.0)                                      # b, on the other level, is untouched
        ex.on_snapshot(book.venue, make_book(bids=[(100.0, 4.0), (99.5, 8.0)]), 3.0, 1.0, 0.0, event_ts=3.0)

    def test_the_public_book_is_never_touched_by_the_cohort_queue(self):
        ex = make_executor(cohort_age="uniform", **BASE)
        book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
        place(ex, book, price=100.0, size=2.0)
        before = (book.levels("bid", 10), book.levels("ask", 10))
        sell_print(ex, book, 100.0, 13.0, 3.0)                                          # (tape repair is the book's own business, not the queue's)
        self.assertEqual((book.levels("bid", 10), book.levels("ask", 10)), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
