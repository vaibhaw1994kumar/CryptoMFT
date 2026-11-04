"""MakerConfig.opposite_cross_cap_to_print (v1.1 step 9): a confirmed cross fills min(order remaining, reaching print volume), each print is spent once.
Opt-in; off keeps the step-8 fill (the cross's full remaining size)."""
from __future__ import annotations

import unittest

from new_backtest.fill_engine import MakerConfig

from test_fill_engine_queue import _book, _executor, _place

CAP = MakerConfig(cancel_model="proportional", opposite_cross_fills=True, opposite_cross_confirm_ms=500.0, opposite_cross_cap_to_print=True)


def _setup(cfg=CAP, n=1, size=1.0):
    ex = _executor(cfg)
    book = _book(bids=[(100.0, 50.0)], asks=[(101.0, 50.0)])
    orders = [_place(ex, book, price=100.0, size=size) for _ in range(n)]
    return ex, book, orders


def _cross(ex, book, t, px=99.5):
    book.apply_delta("ask", px, 3.0, "modify", t)
    return ex.on_opposite_cross("bybit", book, t, 0.0, event_ts=t)


class CapToPrintTests(unittest.TestCase):
    def test_off_by_default_and_needs_confirm(self):
        self.assertFalse(MakerConfig().opposite_cross_cap_to_print)
        with self.assertRaises(ValueError):
            MakerConfig(opposite_cross_fills=True, opposite_cross_cap_to_print=True)

    def test_print_after_fills_only_the_print_size(self):
        ex, book, (o,) = _setup()
        self.assertEqual(_cross(ex, book, 3.0), [])
        fills = ex.on_trade("bybit", 100.0, 0.3, -1, 3.2, 0.0, event_ts=3.2)
        self.assertEqual([(f.cause, round(f.qty, 9), f.ts) for f in fills], [("trade_confirmed_cross_after", 0.3, 3.2)])
        self.assertTrue(o.is_live)
        self.assertAlmostEqual(o.remaining, 0.7)

    def test_second_print_in_window_tops_up_to_the_sum(self):
        ex, book, (o,) = _setup()
        _cross(ex, book, 3.0)
        f1 = ex.on_trade("bybit", 100.0, 0.3, -1, 3.1, 0.0, event_ts=3.1)
        f2 = ex.on_trade("bybit", 100.0, 0.4, -1, 3.3, 0.0, event_ts=3.3)
        self.assertAlmostEqual(sum(f.qty for f in f1 + f2), 0.7)
        self.assertAlmostEqual(o.remaining, 0.3)
        self.assertEqual(ex.on_trade("bybit", 100.0, 0.5, -1, 3.7, 0.0, event_ts=3.7), [])   # after the window (cross at 3.0 + 0.5 s): nothing more
        self.assertAlmostEqual(o.remaining, 0.3)

    def test_print_larger_than_order_fills_the_order_once(self):
        ex, book, (o,) = _setup(size=0.2)
        _cross(ex, book, 3.0)
        fills = ex.on_trade("bybit", 100.0, 5.0, -1, 3.2, 0.0, event_ts=3.2)
        self.assertEqual([round(f.qty, 9) for f in fills], [0.2])
        self.assertFalse(o.is_live)

    def test_print_before_cross_fills_print_size_and_is_not_reused(self):
        ex, book, (o,) = _setup()
        ex.on_trade("bybit", 100.0, 0.3, -1, 3.0, 0.0, event_ts=3.0)
        fills = _cross(ex, book, 3.1)
        self.assertEqual([(f.cause, round(f.qty, 9)) for f in fills], [("trade_confirmed_cross_before", 0.3)])
        for t in (3.15, 3.2, 3.3):                                           # the book is still crossed: the same print cannot fill it again
            book.apply_delta("ask", 99.5, 3.0, "modify", t)
            self.assertEqual(ex.on_opposite_cross("bybit", book, t, 0.0, event_ts=t), [])
        self.assertAlmostEqual(o.remaining, 0.7)

    def test_two_orders_share_one_print(self):
        ex, book, (a, b) = _setup(n=2, size=1.0)
        _cross(ex, book, 3.0)
        fills = ex.on_trade("bybit", 100.0, 0.5, -1, 3.2, 0.0, event_ts=3.2)
        self.assertAlmostEqual(sum(f.qty for f in fills), 0.5)               # one print of 0.5 fills 0.5 in total, not 0.5 per order

    def test_two_orders_share_one_print_before_the_cross(self):
        ex, book, (a, b) = _setup(n=2, size=1.0)
        ex.on_trade("bybit", 100.0, 0.5, -1, 3.0, 0.0, event_ts=3.0)
        fills = _cross(ex, book, 3.1)
        self.assertAlmostEqual(sum(f.qty for f in fills), 0.5)

    def test_no_print_still_drops_with_no_side_effect(self):
        ex, book, (o,) = _setup()
        _cross(ex, book, 3.0)
        self.assertEqual(ex.on_opposite_cross("bybit", book, 3.6, 0.0, event_ts=3.6), [])
        self.assertEqual((o.queue_ahead, o.remaining, o.is_live), (50.0, 1.0, True))

    def test_off_keeps_the_full_fill(self):
        cfg = MakerConfig(cancel_model="proportional", opposite_cross_fills=True, opposite_cross_confirm_ms=500.0)
        ex, book, (o,) = _setup(cfg)
        _cross(ex, book, 3.0)
        fills = ex.on_trade("bybit", 100.0, 0.3, -1, 3.2, 0.0, event_ts=3.2)
        self.assertEqual([round(f.qty, 9) for f in fills], [1.0])


if __name__ == "__main__":
    unittest.main()
