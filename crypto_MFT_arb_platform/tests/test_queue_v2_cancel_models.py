"""Queue model v2, step 5: the cancellation models by their arithmetic, including the guide's worked example."""
from __future__ import annotations

import itertools
import unittest

from qv2_support import delta, make_book, make_executor, place, sell_print

from new_backtest.fill_engine import MakerConfig
from new_backtest.queue_v2.cancel_models import ENGINE_ONLY, MODELS, cancel_ahead, clamp_feasible

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
        for model, A, B, C in itertools.product(MODELS, grid, grid, grid):
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


class EngineOnlyAndValidation(unittest.TestCase):
    def test_min_baseline_is_engine_only_and_never_ahead_first(self):
        self.assertEqual(MODELS, ("behind_first", "ahead_first", "proportional", "power"))
        self.assertEqual(ENGINE_ONLY, ("min_baseline", "all_ahead"))
        for name in ENGINE_ONLY:
            with self.assertRaises(ValueError) as cm:
                cancel_ahead(name, 4.0, 10.0, 5.0)
            self.assertIn("engine-only rule: running-minimum baseline", str(cm.exception))

    def test_power_needs_n_above_zero(self):
        for bad in (0.0, -1.0, float("nan")):
            with self.assertRaises(ValueError):
                cancel_ahead("power", 4.0, 10.0, 5.0, bad)
        self.assertGreater(cancel_ahead("power", 4.0, 10.0, 5.0, 1e-9), 0.0)                  # a tiny positive n is allowed
        self.assertEqual(cancel_ahead("proportional", 4.0, 10.0, 5.0, -3.0), cancel_ahead("proportional", 4.0, 10.0, 5.0))      # n is ignored for every model but power


def engine_c_ahead(A, B, C, model, n=1.0):
    """Run the real executor: an order behind A, B joining behind it, then a residual cancellation C; return how far the queue ahead moved."""
    ex = make_executor(cancel_model=model, cancel_power_n=n, **BASE)
    book = make_book(bids=[(100.0, A), (99.0, 100.0)])
    o = place(ex, book, price=100.0, size=1.0)
    level = A
    if B:
        delta(ex, book, "bid", 100.0, A + B, 1.5)
        level = A + B
    delta(ex, book, "bid", 100.0, level - C, 2.0)
    return A - o.queue_ahead


class EngineStaysInsideTheInterval(unittest.TestCase):
    def test_power_2_with_one_ahead_and_ten_behind_moves_at_least_the_half_that_cannot_have_come_from_behind(self):
        # A = 1, B = 10, C = 10.5: only 10 can have left from behind, so at least 0.5 left from ahead. power(2) alone gives 10.5 * 1 / 101 = 0.104.
        self.assertGreaterEqual(engine_c_ahead(1.0, 10.0, 10.5, "power", 2.0), 0.5 - 1e-12)
        self.assertGreaterEqual(cancel_ahead("power", 10.5, 1.0, 10.0, 2.0), 0.5 - 1e-12)

    def test_every_engine_model_stays_inside_max0_C_minus_B_and_min_C_A(self):
        for model, n in (("proportional", 1.0), ("power", 0.5), ("power", 2.0), ("power", 4.0), ("behind_first", 1.0), ("ahead_first", 1.0)):
            for A, B, C in itertools.product((0.5, 1.0, 3.0, 10.0), (0.0, 0.5, 1.0, 10.0), (0.25, 1.0, 3.0, 10.5)):
                if C > A + B - 1e-9:
                    continue                                                          # the level cannot lose more than it holds (and C = A + B would be a vanish)
                x = engine_c_ahead(A, B, C, model, n)
                lo, hi = max(0.0, C - B), min(C, A)
                self.assertTrue(lo - 1e-9 <= x <= hi + 1e-9, (model, n, A, B, C, x, lo, hi))

    def test_proportional_is_bit_for_bit_what_it_was(self):
        # the share the engine computed before the clamp existed: shrink * min(1, A / level_before), capped by A. It is inside the interval, so the clamp must not touch it.
        for A, B, C in itertools.product((0.7, 1.0, 3.3, 10.0, 123.456), (0.0, 0.3, 1.0, 10.0, 77.7), (0.11, 0.5, 2.9, 7.0)):
            level = A + B
            if C > level - 1e-9:
                continue
            shrink = level - (level - C)                                          # what the engine computes: the level it showed minus the new size
            old = min(A, shrink * min(1.0, A / max(level, 1e-12)))
            ex = make_executor(cancel_model="proportional", **BASE)
            book = make_book(bids=[(100.0, A), (99.0, 100.0)])
            o = place(ex, book, price=100.0, size=1.0)
            if B:
                delta(ex, book, "bid", 100.0, level, 1.5)
            before = o.queue_ahead
            delta(ex, book, "bid", 100.0, level - C, 2.0)
            self.assertEqual(o.queue_ahead, before - old, (A, B, C))

    def test_clamp_feasible_leaves_a_value_inside_unchanged_and_moves_one_outside(self):
        self.assertEqual(clamp_feasible(4.0, 10.0, 5.0, 2.5), 2.5)
        self.assertEqual(clamp_feasible(10.5, 1.0, 10.0, 0.104), 0.5)
        self.assertEqual(clamp_feasible(4.0, 3.0, 5.0, 3.5), 3.0)


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
