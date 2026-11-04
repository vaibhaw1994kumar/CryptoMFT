"""e2e perp audit (Task 1a): a tight equal book 100.00 / 100.01 on both venues passes the spread sanity gate and is refused by the
EDGE gate in both directions. Expected numbers are hand arithmetic."""
from __future__ import annotations

import unittest

from new_backtest import edge
from new_backtest.arb_strategy import TAKER_TAKER

from test_maker_order_lifetime import BINANCE, BYBIT, Harness

V1 = "perp_perp_maker_taker_v1"


class TightEqualBookTests(unittest.TestCase):
    def test_tight_equal_books_are_refused_by_the_edge_gate_not_the_spread_gate(self):
        h = Harness(mode=TAKER_TAKER, profile=V1)
        same = dict(bids=((100.00, 5.0), (99.99, 5.0)), asks=((100.01, 5.0), (100.02, 5.0)))
        h.book(1.0, BYBIT, **same)
        h.book(1.0, BINANCE, **same)
        er = edge.taker_taker_edge_bps(h.bt.books[BYBIT], h.bt.books[BINANCE], 2.0, 0.0, 0.0)
        expected = (100.00 - 100.01) / 100.005 * 10_000.0          # sell at 100.00, buy at 100.01, ref = buy mid 100.005
        self.assertAlmostEqual(expected, -0.99995000249987, places=9)
        self.assertEqual(er.status, "ok")
        self.assertAlmostEqual(er.gross_bps, expected, places=9)
        self.assertIsNone(h.bt.strategy.evaluate(h.bt.books, h.bt.states, h.bt.inventory, 1.0))
        self.assertEqual(h.bt.strategy.rejects["wide_spread"], 0)  # 1 bps spread is well inside max_spread_bps
        self.assertEqual(h.bt.strategy.rejects["no_edge"], 2)      # both directions computed and refused on edge


if __name__ == "__main__":
    unittest.main()
