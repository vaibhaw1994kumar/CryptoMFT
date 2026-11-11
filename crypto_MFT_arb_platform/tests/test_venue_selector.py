"""Phase E (owner 9 Nov): the venue selector. Costs are worked out by hand in each test; a venue that is down or whose book is not valid is never chosen; fixed mode never changes the venue."""
from __future__ import annotations

import math
import unittest

from crypto_stat_arb.ems.venue_selector import RestHedgeVenue, TakerVenue, assign_rest_hedge, assignment_cost, split_taker


def bybit(**kw):
    base = dict(venue="BYBIT", available=True, maker_bps=-0.5, taker_bps=1.8, hedge_slippage_bps=0.4, adverse_bps=1.0, miss_rate=0.02, miss_loss_bps=20.0, fill_probability=0.30)
    base.update(kw)
    return RestHedgeVenue(**base)


def binance(**kw):
    base = dict(venue="BINANCE", available=True, maker_bps=0.9, taker_bps=0.9, hedge_slippage_bps=0.2, adverse_bps=0.6, miss_rate=0.01, miss_loss_bps=20.0, fill_probability=0.25)
    base.update(kw)
    return RestHedgeVenue(**base)


class AssignmentCostTests(unittest.TestCase):
    def test_cost_is_the_hand_computed_sum_of_its_parts(self):
        c = assignment_cost(bybit(), binance())             # Bybit rests, Binance hedges
        # maker -0.5 + hedge taker 0.9 + hedge slippage 0.2 + adverse 1.0 + miss 0.01 x 20 = 0.2
        self.assertAlmostEqual(c.cost_bps, -0.5 + 0.9 + 0.2 + 1.0 + 0.2)
        self.assertEqual((c.rest, c.hedge), ("BYBIT", "BINANCE"))
        r = assignment_cost(binance(), bybit())             # Binance rests, Bybit hedges
        # maker 0.9 + hedge taker 1.8 + slippage 0.4 + adverse 0.6 + miss 0.02 x 20 = 0.4
        self.assertAlmostEqual(r.cost_bps, 0.9 + 1.8 + 0.4 + 0.6 + 0.4)

    def test_fixed_mode_keeps_the_assignment_and_only_reports_what_dynamic_would_choose(self):
        d = assign_rest_hedge([bybit(), binance()], mode="fixed", fixed=("BINANCE", "BYBIT"))        # the dearer assignment is fixed
        self.assertEqual((d.mode, d.reason, (d.chosen.rest, d.chosen.hedge)), ("fixed", "FIXED", ("BINANCE", "BYBIT")))
        self.assertEqual((d.dynamic_choice.rest, d.dynamic_choice.hedge), ("BYBIT", "BINANCE"))
        self.assertAlmostEqual(d.cost_difference_bps, 4.1 - 1.8)                                      # chosen 4.1, dynamic 1.8
        self.assertEqual(len(d.alternatives), 1)
        same = assign_rest_hedge([bybit(), binance()])                                                # the default: Bybit rests, Binance hedges
        self.assertEqual((same.chosen.rest, same.reason, same.cost_difference_bps), ("BYBIT", "FIXED", 0.0))

    def test_dynamic_mode_takes_the_cheaper_assignment_and_lists_the_other_with_its_cost(self):
        d = assign_rest_hedge([bybit(), binance()], mode="dynamic")
        self.assertEqual((d.reason, d.chosen.rest), ("DYNAMIC_CHEAPEST", "BYBIT"))
        self.assertAlmostEqual(d.alternatives[0].cost_bps, 4.1)
        flipped = assign_rest_hedge([bybit(maker_bps=3.0), binance()], mode="dynamic")               # a dear Bybit maker fee flips it
        self.assertEqual(flipped.chosen.rest, "BINANCE")

    def test_a_venue_that_is_down_or_has_no_valid_book_is_never_chosen(self):
        d = assign_rest_hedge([bybit(), binance(available=False)], mode="dynamic")
        self.assertEqual((d.chosen, d.reason), (None, "NO_AVAILABLE_PAIR"))
        f = assign_rest_hedge([bybit(available=False), binance()], mode="fixed")                      # even a fixed assignment is refused
        self.assertEqual((f.chosen, f.reason), (None, "VENUE_UNAVAILABLE"))
        self.assertIsNone(f.dynamic_choice)

    def test_fill_probability_is_reported_not_folded_into_the_cost(self):
        low = assignment_cost(bybit(fill_probability=0.01), binance())
        high = assignment_cost(bybit(fill_probability=0.90), binance())
        self.assertEqual(low.cost_bps, high.cost_bps)
        self.assertEqual((low.fill_probability, high.fill_probability), (0.01, 0.90))

    def test_bad_input_is_refused(self):
        with self.assertRaises(ValueError):
            assign_rest_hedge([bybit()], mode="fixed")
        with self.assertRaises(ValueError):
            assign_rest_hedge([bybit(), binance()], mode="sometimes")
        with self.assertRaises(ValueError):
            assign_rest_hedge([bybit(), binance()], mode="fixed", fixed=("BYBIT", "OKX"))


class SplitTests(unittest.TestCase):
    def venues(self, **kw):
        a = TakerVenue("A", True, 2.0, ((100.00, 4.0), (100.10, 10.0)), **kw.get("a", {}))
        b = TakerVenue("B", True, 5.0, ((100.05, 8.0),), **kw.get("b", {}))
        return [a, b]

    def test_a_buy_takes_the_next_unit_where_price_plus_fee_is_lowest_and_the_cost_is_hand_computed(self):
        r = split_taker("buy", 10.0, self.venues())
        # effective prices: A 100.00 x 1.0002 = 100.0200 (4), B 100.05 x 1.0005 = 100.100025 (8), A 100.10 x 1.0002 = 100.12002 (10)
        self.assertEqual(r.allocation, {"A": 4.0, "B": 6.0})
        self.assertAlmostEqual(r.average_effective_price, (4 * 100.0200 + 6 * 100.100025) / 10.0, places=9)
        self.assertEqual(r.unfilled, 0.0)
        self.assertEqual(set(r.alternatives), {"A", "B"})
        self.assertAlmostEqual(r.alternatives["A"][0], (4 * 100.0200 + 6 * 100.12002) / 10.0, places=9)     # A alone is dearer
        self.assertLess(r.average_effective_price, r.alternatives["A"][0])

    def test_a_sell_moves_the_price_down_by_the_fee_and_takes_the_highest(self):
        a = TakerVenue("A", True, 2.0, ((100.00, 4.0), (99.90, 10.0)))
        b = TakerVenue("B", True, 5.0, ((99.96, 8.0),))
        r = split_taker("sell", 10.0, [a, b])
        # effective: A 100.00 x 0.9998 = 99.98 (4), B 99.96 x 0.9995 = 99.910020 (8), A 99.90 x 0.9998 = 99.88002
        self.assertEqual(r.allocation, {"A": 4.0, "B": 6.0})
        self.assertAlmostEqual(r.average_effective_price, (4 * 99.98 + 6 * 99.91002) / 10.0, places=9)

    def test_a_notional_limit_moves_the_rest_to_the_other_venue(self):
        r = split_taker("buy", 10.0, self.venues(b={"max_notional": 100.05 * 2.0}))                      # B may take 2 units only
        self.assertAlmostEqual(r.allocation["B"], 2.0)
        self.assertAlmostEqual(r.allocation["A"], 8.0)

    def test_an_unavailable_venue_is_skipped_and_short_depth_is_reported_as_unfilled(self):
        r = split_taker("buy", 10.0, [TakerVenue("A", True, 2.0, ((100.0, 3.0),)), TakerVenue("B", False, 0.0, ((99.0, 50.0),))])
        self.assertEqual((r.allocation, r.unfilled), ({"A": 3.0}, 7.0))
        self.assertNotIn("B", r.alternatives)
        none = split_taker("buy", 1.0, [TakerVenue("A", False, 2.0, ((100.0, 3.0),))])
        self.assertTrue(math.isnan(none.average_effective_price))
        self.assertEqual(none.unfilled, 1.0)

    def test_bad_side_is_refused(self):
        with self.assertRaises(ValueError):
            split_taker("hold", 1.0, self.venues())


if __name__ == "__main__":
    unittest.main()
