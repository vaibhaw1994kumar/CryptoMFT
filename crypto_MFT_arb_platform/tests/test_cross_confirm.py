"""MakerConfig.opposite_cross_confirm_ms (v1.1 step 8): an opposite-cross fill needs a public print that reaches the order within +-W of the cross.
Print before the cross -> fill at the cross; print after (inside W) -> fill at the print's time; none -> the cross is dropped with no side effect.
Opt-in; 0 leaves every result unchanged."""
from __future__ import annotations

import unittest

from new_backtest.fill_engine import MakerConfig

from test_fill_engine_queue import _book, _executor, _place

ON = MakerConfig(cancel_model="proportional", opposite_cross_fills=True, opposite_cross_confirm_ms=500.0)


def _setup(cfg=ON):
    ex = _executor(cfg)
    book = _book(bids=[(100.0, 50.0)], asks=[(101.0, 50.0)])
    o = _place(ex, book, price=100.0, size=2.0)
    return ex, book, o


def _cross(ex, book, t, px=99.5):
    book.apply_delta("ask", px, 3.0, "modify", t)
    return ex.on_opposite_cross("bybit", book, t, 0.0, event_ts=t)


class CrossConfirmTests(unittest.TestCase):
    def test_off_by_default_and_needs_cross_fills(self):
        self.assertEqual(MakerConfig().opposite_cross_confirm_ms, 0.0)
        with self.assertRaises(ValueError):
            MakerConfig(opposite_cross_confirm_ms=500.0)
        ex, book, o = _setup(MakerConfig(cancel_model="proportional", opposite_cross_fills=True))
        f = _cross(ex, book, 3.0)
        self.assertEqual([x.cause for x in f], ["opposite_cross"])          # the unconfirmed behaviour is unchanged

    def test_print_after_the_cross_fills_at_the_prints_time(self):
        ex, book, o = _setup()
        self.assertEqual(_cross(ex, book, 3.0), [])                          # no print yet: waiting, nothing happens
        self.assertTrue(o.is_live)
        self.assertEqual(o.queue_ahead, 50.0)                                # a wait does not move the order up the queue
        fills = ex.on_trade("bybit", 100.0, 0.3, -1, 3.2, 0.0, event_ts=3.2)
        self.assertEqual([(f.cause, f.qty, f.ts, f.price) for f in fills], [("trade_confirmed_cross_after", 2.0, 3.2, 100.0)])
        self.assertFalse(o.is_live)
        self.assertEqual(ex.diag["cross_confirmed_after"], 1.0)
        self.assertEqual(ex.diag["cross_print_smaller_than_fill"], 1.0)      # the print (0.3) is evidence, not the fill size (2.0): counted

    def test_print_before_the_cross_fills_at_the_cross(self):
        ex, book, o = _setup()
        self.assertEqual(ex.on_trade("bybit", 100.0, 0.3, -1, 3.0, 0.0, event_ts=3.0), [])   # drains queue ahead only (50 -> 49.7)
        fills = _cross(ex, book, 3.3)
        self.assertEqual([(f.cause, f.qty, f.ts) for f in fills], [("trade_confirmed_cross_before", 2.0, 3.3)])
        self.assertEqual(ex.diag["cross_confirmed_before"], 1.0)

    def test_no_print_drops_the_cross_with_no_side_effect(self):
        ex, book, o = _setup()
        self.assertEqual(_cross(ex, book, 3.0), [])
        self.assertEqual(ex.on_opposite_cross("bybit", book, 3.6, 0.0, event_ts=3.6), [])    # window (to 3.5) passed with no print
        self.assertEqual(ex.diag["cross_dropped"], 1.0)
        self.assertEqual((o.queue_ahead, o.remaining, o.is_live), (50.0, 2.0, True))        # queue untouched, order unchanged
        self.assertEqual(ex.on_opposite_cross("bybit", book, 4.0, 0.0, event_ts=4.0), [])    # still crossed: stays dropped, no new window
        self.assertEqual(ex.diag["cross_dropped"], 1.0)

    def test_a_print_after_the_window_does_not_confirm(self):
        ex, book, o = _setup()
        _cross(ex, book, 3.0)
        fills = ex.on_trade("bybit", 100.0, 0.3, -1, 4.5, 0.0, event_ts=4.5)                 # +1.5 s: outside +-500 ms
        self.assertEqual([f.cause for f in fills], [])
        self.assertTrue(o.is_live)
        self.assertEqual(ex.diag["cross_dropped"], 1.0)

    def test_a_print_netted_against_a_depth_credit_still_confirms(self):
        cfg = MakerConfig(cancel_model="proportional", opposite_cross_fills=True, opposite_cross_confirm_ms=500.0, dedupe_trade_depth=True)
        ex, book, o = _setup(cfg)
        _cross(ex, book, 3.0)
        ex.on_delta("bybit", "bid", 100.0, 49.0, 3.05, 1.0, 0.0, event_ts=3.05)      # a decrease of 1 at our level: it takes the print's credit
        fills = ex.on_trade("bybit", 100.0, 1.0, -1, 3.08, 0.0, event_ts=3.08)       # the print is netted away against that decrease (within 50 ms)
        self.assertEqual([f.cause for f in fills], ["trade_confirmed_cross_after"])
        self.assertEqual(ex.diag["trade_volume_credited_to_depth"], 1.0)

    def test_wrong_aggressor_side_and_a_better_price_do_not_confirm(self):
        ex, book, o = _setup()
        _cross(ex, book, 3.0)
        self.assertEqual(ex.on_trade("bybit", 100.0, 5.0, +1, 3.1, 0.0, event_ts=3.1), [])   # a buy aggressor does not reach a bid
        self.assertEqual(ex.on_trade("bybit", 100.5, 5.0, -1, 3.2, 0.0, event_ts=3.2), [])   # a sell above our price does not reach it
        self.assertTrue(o.is_live)

    def test_a_print_that_executed_before_the_order_arrived_does_not_confirm(self):
        ex, book, o = _setup()
        o.arrival_ts = 3.0
        self.assertEqual(ex.on_trade("bybit", 100.0, 0.3, -1, 3.1, 0.0, event_ts=2.9), [])   # executed at 2.9, we arrived at 3.0
        self.assertEqual(_cross(ex, book, 3.2), [])
        self.assertTrue(o.is_live)

    def test_the_wait_survives_an_uncross_inside_the_window_and_a_new_episode_gets_a_new_window(self):
        ex, book, o = _setup()
        _cross(ex, book, 3.0)
        book.apply_delta("ask", 99.5, 0.0, "delete", 3.1)                                    # the quote moves away
        self.assertEqual(ex.on_opposite_cross("bybit", book, 3.1, 0.0, event_ts=3.1), [])
        self.assertEqual(o.cross_state, "pending")
        self.assertEqual(ex.on_opposite_cross("bybit", book, 3.7, 0.0, event_ts=3.7), [])    # the window has passed while uncrossed
        self.assertIsNone(o.cross_state)
        self.assertEqual(ex.diag["cross_dropped"], 1.0)
        self.assertEqual(_cross(ex, book, 5.0), [])                                          # a new episode: a new window
        fills = ex.on_trade("bybit", 100.0, 0.3, -1, 5.2, 0.0, event_ts=5.2)
        self.assertEqual([f.cause for f in fills], ["trade_confirmed_cross_after"])

    def test_one_fill_only(self):
        ex, book, o = _setup()
        _cross(ex, book, 3.0)
        a = ex.on_trade("bybit", 100.0, 5.0, -1, 3.1, 0.0, event_ts=3.1)
        b = ex.on_trade("bybit", 100.0, 5.0, -1, 3.2, 0.0, event_ts=3.2)
        self.assertEqual((len(a), len(b)), (1, 0))
        self.assertAlmostEqual(o.filled_qty, 2.0)


if __name__ == "__main__":
    unittest.main()
