"""Queue model v2, step 6: the cohort queue (LevelQueue) on its own. Cohorts are model labels; own orders are never touched by a reset."""
from __future__ import annotations

import random
import unittest

from new_backtest.queue_v2.cohort import HYPOTHESES, LevelQueue


def level(ext0=10.0, behind=5.0, t_behind=10.0):
    """Initial displayed size 10 (unknown age 30 s) -> our order joins -> 5 more arrive behind us at t=10."""
    q = LevelQueue(ext0, now=0.0, unknown_age_s=30.0)
    q.join(1, 2.0)
    q.add_external(behind, t_behind)
    return q


class Structure(unittest.TestCase):
    def test_the_initial_displayed_size_is_one_cohort_and_we_join_behind_it(self):
        q = LevelQueue(10.0, 0.0)
        self.assertEqual(q.join(1, 2.0), 10.0)
        self.assertEqual(len(q.items), 2)

    def test_later_additions_join_behind_and_never_push_us_back(self):
        q = level()
        self.assertEqual((q.ahead(1), q.behind_external(1)), (10.0, 5.0))

    def test_a_second_own_order_sits_behind_the_first_and_the_cohorts_between_them(self):
        q = level()
        q.add_external(3.0, 12.0)
        self.assertEqual(q.join(2, 4.0), 10.0 + 2.0 + 5.0 + 3.0)             # ext 10, own1 2, ext 5, ext 3 ahead of own2
        self.assertEqual(q.ahead(1), 10.0)                                    # the first is unaffected

    def test_a_reset_rebases_the_external_size_and_never_cancels_an_own_order(self):
        q = level()
        q.add_external(4.0, 11.0)
        q.rebase_external(0.0, now=20.0, hypothesis="uniform", theta=1.0, note="snapshot shows an empty level")
        self.assertEqual(q.own_ids(), [1])
        self.assertEqual((q.ext_total(), q.ahead(1)), (0.0, 0.0))
        q.rebase_external(7.0, now=21.0, hypothesis="uniform", theta=1.0)
        self.assertEqual((q.ext_total(), q.ahead(1)), (7.0, 0.0))             # growth joins behind us
        self.assertGreaterEqual(len(q.log), 2)                                 # the projections are logged


class Hypotheses(unittest.TestCase):
    """Ahead 10 (age 50 s at t=20), behind 5 (age 10 s), residual cancellation C = 4 at t=20."""
    def removed_ahead(self, hyp, theta=1.0):
        q = level()
        a0 = q.ahead(1)
        q.cancel_external(4.0, 20.0, hyp, theta)
        return a0 - q.ahead(1)

    def test_youngest_first_takes_the_behind_cohort_first(self):
        self.assertAlmostEqual(self.removed_ahead("youngest_first"), 0.0)

    def test_oldest_first_takes_the_ahead_cohort_first(self):
        self.assertAlmostEqual(self.removed_ahead("oldest_first"), 4.0)

    def test_uniform_is_proportional_to_size(self):
        self.assertAlmostEqual(self.removed_ahead("uniform"), 4.0 * 10 / 15)

    def test_hazard_interpolates_between_uniform_and_youngest_first(self):
        self.assertAlmostEqual(self.removed_ahead("hazard", 0.0), self.removed_ahead("uniform"))
        h1, h5 = self.removed_ahead("hazard", 1.0), self.removed_ahead("hazard", 5.0)
        self.assertLess(h1, self.removed_ahead("uniform"))
        self.assertLess(h5, h1)
        self.assertAlmostEqual(self.removed_ahead("hazard", 1.0), 4.0 * (10 / 51) / (10 / 51 + 5 / 11), places=9)

    def test_a_cancellation_never_removes_more_than_the_external_size_and_never_a_negative_one(self):
        for hyp in HYPOTHESES:
            q = level()
            self.assertAlmostEqual(q.cancel_external(100.0, 20.0, hyp), 15.0)
            self.assertTrue(all(i["size"] >= 0 for i in q.items))
            self.assertEqual(q.own_ids(), [1])
            self.assertEqual(LevelQueue(0.0, 0.0).cancel_external(5.0, 1.0, hyp), 0.0)

    def test_the_guides_worked_example_under_each_hypothesis(self):
        # ahead 10, behind 5, own 2, residual cancel 4, then an 8-unit print: own fill 0 / 0.6667 / 2.0
        out = {}
        for hyp in ("youngest_first", "uniform", "oldest_first"):
            q = level()
            q.cancel_external(4.0, 20.0, hyp)
            q.provisional.clear()
            out[hyp] = q.consume_print(8.0).get(1, 0.0)
        self.assertAlmostEqual(out["youngest_first"], 0.0)
        self.assertAlmostEqual(out["uniform"], 2.0 / 3.0, places=9)
        self.assertAlmostEqual(out["oldest_first"], 2.0)


class Prints(unittest.TestCase):
    def test_one_print_one_budget_across_two_own_orders(self):
        q = LevelQueue(10.0, 0.0)
        q.join(1, 2.0)
        q.add_external(3.0, 1.0)
        q.join(2, 2.0)
        fills = q.consume_print(12.0)
        self.assertEqual(fills, {1: 2.0})                                      # 10 ext + 2 own1 = 12: the budget is gone before it reaches own2
        self.assertEqual(q.ahead(2), 3.0)
        total = sum(LevelQueue(10.0, 0.0).consume_print(12.0).values())
        self.assertLessEqual(total, 12.0)

    def test_a_print_can_never_fill_more_than_its_quantity(self):
        rng = random.Random(3)
        for _ in range(200):
            q = LevelQueue(rng.uniform(0, 20), 0.0)
            for oid in range(rng.randint(1, 4)):
                q.join(oid, rng.uniform(0.1, 5))
                q.add_external(rng.uniform(0, 4), oid + 1.0)
            v = rng.uniform(0, 40)
            self.assertLessEqual(sum(q.consume_print(v).values()), v + 1e-9)
            self.assertTrue(all(i["size"] >= -1e-12 for i in q.items))

    def test_a_partial_fill_keeps_priority(self):
        q = LevelQueue(0.0, 0.0)
        q.join(1, 2.0)
        q.join(2, 2.0)
        self.assertEqual(q.consume_print(1.0), {1: 1.0})
        self.assertEqual((q.ahead(1), q.ahead(2)), (0.0, 1.0))                # own1 still first with 1 left


class BookFirstThenPrint(unittest.TestCase):
    def test_reclassify_moves_the_provisional_movement_to_the_head(self):
        q = level()
        q.cancel_external(4.0, 20.0, "youngest_first")                         # the hypothesis put it behind us: ahead is still 10
        self.assertEqual(q.ahead(1), 10.0)
        credited = q.reclassify(4.0)                                           # the matching print arrives: it was an execution at the head
        self.assertAlmostEqual(credited, 4.0)
        self.assertAlmostEqual(q.ahead(1), 6.0)
        self.assertAlmostEqual(q.behind_external(1), 5.0)                      # the cohort behind us is restored

    def test_only_movement_already_applied_can_be_credited(self):
        q = level()
        q.cancel_external(4.0, 20.0, "uniform")
        self.assertAlmostEqual(q.reclassify(12.0), 4.0)

    def test_a_print_settles_every_pending_decrease(self):
        q = level()
        q.cancel_external(4.0, 20.0, "uniform")
        q.consume_print(0.0)
        self.assertEqual(q.provisional, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
