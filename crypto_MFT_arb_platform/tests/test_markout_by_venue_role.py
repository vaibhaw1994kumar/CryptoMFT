"""MarkoutTracker keeps the pooled markout_summary exactly as before and adds a (venue, maker|taker) split of the same numbers."""
from __future__ import annotations

import unittest

from new_backtest.latency_model import MarkoutTracker


class MarkoutByVenueRoleTests(unittest.TestCase):
    def _run(self, with_roles: bool):
        mt = MarkoutTracker((1.0, 5.0))
        mt.record_fill(10.0, "bybit", "buy", 100.0, 1.0, "maker" if with_roles else "")
        mt.record_fill(10.1, "binance", "sell", 100.2, 2.0, "taker" if with_roles else "")
        mt.resolve(20.0, {"bybit": 100.1, "binance": 100.0})
        return mt

    def test_pooled_summary_is_unchanged_by_the_split(self):
        self.assertEqual(self._run(True).summary(), self._run(False).summary())

    def test_split_matches_the_hand_computed_numbers(self):
        s = self._run(True).summary_by_venue_role()
        self.assertEqual(sorted(s), ["binance/taker", "bybit/maker"])
        self.assertAlmostEqual(s["bybit/maker"]["1s"]["qty_weighted_markout_bps"], 10.0)      # buy at 100, mid 100.1 -> +10 bps
        self.assertAlmostEqual(s["binance/taker"]["1s"]["qty_weighted_markout_bps"], 19.96007984, places=5)   # sell at 100.2, mid 100.0
        self.assertEqual(s["bybit/maker"]["1s"]["count"], 1.0)

    def test_pooled_is_the_qty_weighted_mean_of_the_split(self):
        mt = self._run(True)
        p, s = mt.summary()["1s"], mt.summary_by_venue_role()
        q = 1.0 * s["bybit/maker"]["1s"]["qty_weighted_markout_bps"] + 2.0 * s["binance/taker"]["1s"]["qty_weighted_markout_bps"]
        self.assertAlmostEqual(p["qty_weighted_markout_bps"], q / 3.0)
        self.assertEqual(p["count"], 2.0)

    def test_no_roles_no_split(self):
        self.assertEqual(self._run(False).summary_by_venue_role(), {})


if __name__ == "__main__":
    unittest.main()
