"""WP8 statistical wording and checks: the IPCW assumption is stated and testable (informative_censoring_check), and the fill gate
scores the PRE-PLACEMENT expected queue when asked to (legacy: queue 0)."""
from __future__ import annotations

import unittest

from new_backtest.fill_label import FillLabelRecord, compute_ipcw_weights, informative_censoring_check
from new_backtest.order_book import OrderBook

from test_maker_order_lifetime import BINANCE, BYBIT, Harness


def rec(h, filled, censored, moved, obs):
    return FillLabelRecord(timestamp=0.0, symbol="X", side="bid", relative_to_touch_ticks=0, queue_ahead=1.0, local_depth=1.0,
                           local_depth_baseline=1.0, spread_pct=0.0, short_vol=0.0, trade_count=0.0, trade_sign=0.0, regime_bucket="calm",
                           horizon_s=h, filled=filled, terminal_reason="full_fill" if filled else "cancel", price_moved_through=moved,
                           observation_time_s=obs, is_censored=censored)


class CensoringTests(unittest.TestCase):
    def test_informative_censoring_is_flagged_when_cancels_track_adverse_moves(self):
        rows = []
        for i in range(60):                                                   # moved through: 80% cancelled
            rows.append(rec(5, filled=False, censored=(i % 5 != 0), moved=True, obs=1.0))
        for i in range(60):                                                   # not moved: 10% cancelled
            rows.append(rec(5, filled=(i % 10 != 0), censored=(i % 10 == 0), moved=False, obs=2.0))
        out = informative_censoring_check(rows, 5)
        self.assertAlmostEqual(out["censor_rate_moved_through"], 0.8, places=9)
        self.assertAlmostEqual(out["censor_rate_not_moved"], 0.1, places=9)
        self.assertTrue(out["informative_flag"])
        self.assertIn("non-informative", out["assumption"])
        same = [rec(5, filled=False, censored=(i % 2 == 0), moved=(i % 3 == 0), obs=1.0) for i in range(120)]
        self.assertFalse(informative_censoring_check(same, 5)["informative_flag"])
        self.assertEqual(informative_censoring_check([], 5)["gap"], None)

    def test_ipcw_docstring_states_the_assumption_and_not_unbiased(self):
        doc = compute_ipcw_weights.__doc__
        self.assertIn("non-informative censoring", doc)
        self.assertIn("E[min(T, H)]", doc)
        self.assertIn("CONDITIONAL on a fill", doc)
        self.assertNotIn("unbiased", doc.lower().replace("not \"unbiased\"", ""))


class FillGateQueueTests(unittest.TestCase):
    def test_gate_queue_is_zero_in_legacy_and_the_shown_size_or_floor_otherwise(self):
        h = Harness()
        st = h.bt.strategy
        b = OrderBook("bybit", "X")
        b.apply_snapshot([[99.0, 5.0]], [[101.0, 5.0]], 1.0)
        self.assertEqual(st._gate_queue(b, "bid", 99.0), 0.0)                 # legacy: front of the queue
        st.config.fill_gate_queue_floor = 25.0
        self.assertEqual(st._gate_queue(b, "bid", 99.0), 5.0)                 # the level exists: its displayed size
        self.assertEqual(st._gate_queue(b, "bid", 100.0), 25.0)               # absent level: the floor


if __name__ == "__main__":
    unittest.main()
