"""Phase E (owner 9 Nov): flatten on a breaker trip: cancel the resting PASSIVE children, then flatten the residual with MARKET_BANDED inside the unwind band, within a retry budget, then a persistent ALARM."""
from __future__ import annotations

import unittest

from crypto_stat_arb.ems.risk import FlattenStep, RestingChild, plan_flatten_on_trip

MID = {"BYBIT": 100.0, "BINANCE": 100.0}


def plan(resting=(), base=None, **kw):
    kw.setdefault("band_bps", 15.0)
    kw.setdefault("attempt", 0)
    kw.setdefault("retry_budget", 3)
    return plan_flatten_on_trip(list(resting), base or {}, MID, **kw)


class FlattenOnTripTests(unittest.TestCase):
    def test_cancels_come_first_then_one_banded_flatten_of_the_net(self):
        steps = plan([RestingChild("o1", "BYBIT"), RestingChild("o2", "BYBIT")], {"BYBIT": 1.0, "BINANCE": -0.7})
        self.assertEqual([s.kind for s in steps], ["CANCEL", "CANCEL", "FLATTEN"])
        self.assertEqual([s.order_id for s in steps[:2]], ["o1", "o2"])
        f = steps[2]
        self.assertEqual((f.venue, f.side, f.tactic), ("BYBIT", "sell", "MARKET_BANDED"))     # the net is +0.3: sell it where the long is held
        self.assertAlmostEqual(f.qty, 0.3)
        self.assertAlmostEqual(f.worst_price, 100.0 * (1 - 15 / 1e4))                          # inside the unwind band, below the mid for a sell

    def test_a_net_short_is_bought_back_above_the_mid_within_the_band(self):
        f = plan(base={"BYBIT": -0.4, "BINANCE": 0.1})[-1]
        self.assertEqual((f.venue, f.side), ("BYBIT", "buy"))
        self.assertAlmostEqual(f.qty, 0.3)
        self.assertAlmostEqual(f.worst_price, 100.0 * (1 + 15 / 1e4))

    def test_with_a_touch_the_band_is_read_from_the_touch_the_order_crosses(self):
        """Owner 11 Nov: MARKET_BANDED prices around the touch (a sell from the bid, a buy from the ask), as the core's unwind does."""
        touch = {"BYBIT": (99.9, 100.1), "BINANCE": (99.95, 100.05)}
        sell = plan_flatten_on_trip([], {"BYBIT": 0.3}, MID, band_bps=15.0, attempt=0, retry_budget=3, touch=touch)[-1]
        self.assertAlmostEqual(sell.worst_price, 99.9 * (1 - 15 / 1e4))
        buy = plan_flatten_on_trip([], {"BYBIT": -0.3}, MID, band_bps=15.0, attempt=0, retry_budget=3, touch=touch)[-1]
        self.assertAlmostEqual(buy.worst_price, 100.1 * (1 + 15 / 1e4))
        self.assertAlmostEqual(plan(base={"BYBIT": 0.3})[-1].worst_price, 100.0 * (1 - 15 / 1e4))          # without a touch: the mid, as before

    def test_mode_all_closes_every_venues_base(self):
        steps = plan(base={"BYBIT": 1.0, "BINANCE": -1.0}, mode="all")
        self.assertEqual({(s.venue, s.side, s.qty) for s in steps}, {("BYBIT", "sell", 1.0), ("BINANCE", "buy", 1.0)})

    def test_flat_means_only_cancels_and_never_a_flatten(self):
        steps = plan([RestingChild("o1", "BINANCE")], {"BYBIT": 0.5, "BINANCE": -0.5})
        self.assertEqual([s.kind for s in steps], ["CANCEL"])

    def test_only_passive_children_are_cancelled(self):
        steps = plan([RestingChild("a", "BYBIT", "PASSIVE"), RestingChild("b", "BINANCE", "IOC_CAPPED")])
        self.assertEqual([s.order_id for s in steps], ["a"])

    def test_after_the_retry_budget_there_are_no_more_sends_only_a_persistent_alarm(self):
        for attempt in (3, 4, 10):
            steps = plan(base={"BYBIT": 0.3}, attempt=attempt)
            self.assertEqual([(s.kind, s.reason) for s in steps], [("ALARM", "FLATTEN_RETRY_BUDGET_EXHAUSTED")])
        self.assertEqual(plan(base={"BYBIT": 0.3}, attempt=2)[-1].kind, "FLATTEN")              # the last attempt inside the budget still sends

    def test_a_venue_that_is_down_is_not_flattened_on_another_holder_is_used_else_an_alarm(self):
        both = plan(base={"BYBIT": 1.0, "BINANCE": 0.5}, available={"BYBIT": False, "BINANCE": True})
        self.assertEqual((both[-1].kind, both[-1].venue, both[-1].qty), ("FLATTEN", "BINANCE", 1.5))
        none = plan(base={"BYBIT": 1.0, "BINANCE": -0.4}, available={"BYBIT": False})
        self.assertEqual([(s.kind, s.reason) for s in none], [("ALARM", "NO_AVAILABLE_VENUE_TO_FLATTEN_ON")])
        down_all = plan(base={"BYBIT": 1.0}, mode="all", available={"BYBIT": False})
        self.assertEqual([(s.kind, s.reason) for s in down_all], [("ALARM", "VENUE_DOWN_CANNOT_FLATTEN")])

    def test_only_the_banded_market_tactic_is_ever_planned_and_bad_input_is_refused(self):
        for base in ({"BYBIT": 1.0}, {"BYBIT": -2.0, "BINANCE": 0.5}):
            for s in plan(base=base):
                if s.kind == "FLATTEN":
                    self.assertEqual(s.tactic, "MARKET_BANDED")
        with self.assertRaises(ValueError):
            plan(retry_budget=0)
        with self.assertRaises(ValueError):
            plan(mode="half")


if __name__ == "__main__":
    unittest.main()
