"""Queue model v2, step 5: the cancellation models by their arithmetic, including the guide's worked example."""
from __future__ import annotations

import itertools
import unittest

from qv2_support import delta, make_book, make_executor, place, sell_print

from new_backtest.fill_engine import MakerConfig
from new_backtest.queue_v2.cancel_models import MODELS, cancel_ahead

BASE = dict(fill_evidence="off", dedupe_trade_depth=True, share_trade_volume=True, exchange_time_eligibility=True)


class Arithmetic(unittest.TestCase):
    def test_the_four_formulas(self):
        A, B, C = 10.0, 5.0, 4.0
        self.assertAlmostEqual(cancel_ahead("behind_first", C, A, B), 0.0)                 # min(A, max(0, C - B)) = max(0, -1) = 0
        self.assertAlmostEqual(cancel_ahead("ahead_first", C, A, B), 4.0)                  # min(C, A)
        self.assertAlmostEqual(cancel_ahead("proportional", C, A, B), 4.0 * 10 / 15)       # C A / (A + B)
        self.assertAlmostEqual(cancel_ahead("power", C, A, B, 2.0), 4.0 * 100 / (100 + 25))
        self.assertAlmostEqual(cancel_ahead("power", C, A, B, 1.0), cancel_ahead("proportional", C, A, B))
        self.assertAlmostEqual(cancel_ahead("behind_first", 8.0, 10.0, 5.0), 3.0)         # the 5 behind go first, 3 more come from ahead

    def test_every_model_stays_inside_the_feasibility_interval_everywhere(self):
        grid = [0.0, 0.5, 1.0, 3.0, 10.0, 100.0]
        for model, A, B, C in itertools.product(("behind_first", "ahead_first", "proportional", "power", "min_baseline"), grid, grid, grid):
            x = cancel_ahead(model, C, A, B, 0.5 if model == "power" else 1.0)
            Ceff = min(C, A + B)
            lo, hi = (max(0.0, Ceff - B), min(Ceff, A)) if A > 0 and C > 0 else (0.0, 0.0)
            self.assertTrue(lo - 1e-12 <= x <= hi + 1e-12, (model, A, B, C, x, lo, hi))

    def test_zero_totals_do_not_divide(self):
        for model in ("proportional", "power", "behind_first", "ahead_first"):
            self.assertEqual(cancel_ahead(model, 4.0, 0.0, 0.0), 0.0)
            self.assertEqual(cancel_ahead(model, 0.0, 5.0, 5.0), 0.0)
            self.assertEqual(cancel_ahead(model, 4.0, 0.0, 6.0), 0.0)                     # nobody ahead: nothing to move
        self.assertAlmostEqual(cancel_ahead("proportional", 4.0, 6.0, 0.0), 4.0)           # nobody behind: all of it came from ahead

    def test_unknown_model_is_an_error(self):
        with self.assertRaises(ValueError):
            cancel_ahead("youngest_first", 1.0, 1.0, 1.0)

    def test_names_and_the_deprecated_alias(self):
        self.assertEqual(MakerConfig(cancel_model="min_baseline").effective_cancel_model, "all_ahead")     # the engine keeps its canonical name
        self.assertEqual(MakerConfig(cancel_model="all_ahead").effective_cancel_model, "all_ahead")        # deprecated alias still accepted
        for m in ("behind_first", "ahead_first", "proportional", "power"):
            self.assertEqual(MakerConfig(cancel_model=m).effective_cancel_model, m)
        with self.assertRaises(ValueError):
            MakerConfig(cancel_model="bogus")


class WorkedExample(unittest.TestCase):
    """Ahead 10, behind 5, own 2, a residual cancellation of 4, then an 8-unit print: own fill 0 / 0.6667 / 2.0."""

    def run_model(self, model):
        ex = make_executor(cancel_model=model, **BASE)
        book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
        o = place(ex, book, price=100.0, size=2.0)
        delta(ex, book, "bid", 100.0, 15.0, 1.0)                    # 5 join behind us: the level is 15
        delta(ex, book, "bid", 100.0, 11.0, 2.0)                    # residual cancellation of 4, no print explains it
        fills = sell_print(ex, book, 100.0, 8.0, 3.0)              # later (outside the netting window), an 8-unit print
        return sum(f.qty for f in fills), o

    def test_behind_first_fills_nothing(self):
        q, o = self.run_model("behind_first")
        self.assertAlmostEqual(q, 0.0)
        self.assertAlmostEqual(o.queue_ahead, 2.0)                  # 10 ahead, cancel took only behind, the print used 8

    def test_proportional_fills_two_thirds(self):
        q, _ = self.run_model("proportional")
        self.assertAlmostEqual(q, 2.0 / 3.0, places=9)

    def test_ahead_first_fills_the_whole_order(self):
        q, o = self.run_model("ahead_first")
        self.assertAlmostEqual(q, 2.0)
        self.assertAlmostEqual(o.remaining, 0.0)

    def test_the_ordering_of_the_three_is_the_ordering_of_the_assumption(self):
        a, b, c = (self.run_model(m)[0] for m in ("behind_first", "proportional", "ahead_first"))
        self.assertLess(a, b)
        self.assertLess(b, c)

    def test_power_n_1_is_bit_identical_to_proportional(self):
        ex1 = make_executor(cancel_model="power", cancel_power_n=1.0, **BASE)
        ex2 = make_executor(cancel_model="proportional", **BASE)
        out = []
        for ex in (ex1, ex2):
            book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
            o = place(ex, book, price=100.0, size=2.0)
            delta(ex, book, "bid", 100.0, 15.0, 1.0)
            delta(ex, book, "bid", 100.0, 11.0, 2.0)
            out.append(o.queue_ahead)
        self.assertEqual(out[0], out[1])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
