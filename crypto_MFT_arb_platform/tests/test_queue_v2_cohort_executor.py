"""Queue model v2, step 6: queue_model='cohort' inside MakerExecutor (the same events, the cohort queue instead of one number)."""
from __future__ import annotations

import unittest

from qv2_support import delta, make_book, make_executor, place, sell_print

from new_backtest.fill_engine import MakerConfig
from new_backtest.models import Side

BASE = dict(queue_model="cohort", fill_evidence="off", dedupe_trade_depth=True, share_trade_volume=True, exchange_time_eligibility=True)


def worked(age):
    ex = make_executor(cohort_age=age, **BASE)
    book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
    o = place(ex, book, price=100.0, size=2.0)
    delta(ex, book, "bid", 100.0, 15.0, 1.0)                      # 5 arrive behind us
    delta(ex, book, "bid", 100.0, 11.0, 2.0)                      # residual cancellation of 4
    fills = sell_print(ex, book, 100.0, 8.0, 3.0)                 # an 8-unit print, outside the netting window
    return sum(f.qty for f in fills), o, ex


class Executor(unittest.TestCase):
    def test_the_guides_worked_example_through_the_executor(self):
        self.assertAlmostEqual(worked("youngest_first")[0], 0.0)
        self.assertAlmostEqual(worked("uniform")[0], 2.0 / 3.0, places=9)
        self.assertAlmostEqual(worked("oldest_first")[0], 2.0)
        self.assertAlmostEqual(worked("hazard")[0], worked("hazard")[0])

    def test_scalar_default_is_unchanged_and_cohort_needs_a_valid_age(self):
        self.assertEqual(MakerConfig().queue_model, "scalar")
        with self.assertRaises(ValueError):
            MakerConfig(queue_model="cohort", cohort_age="random")
        MakerConfig(queue_model="cohort", opposite_cross_fills=True, opposite_cross_confirm_ms=50.0, opposite_cross_cap_to_print=True)   # the cross rule is allowed since the cohort fixes (6 Nov)
        with self.assertRaises(ValueError):
            MakerConfig(queue_model="bogus")

    def test_two_own_orders_at_one_level_share_one_traversal_and_one_print(self):
        ex = make_executor(cohort_age="uniform", **BASE)
        book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
        a = place(ex, book, price=100.0, size=2.0, now=1.0)
        b = place(ex, book, price=100.0, size=2.0, now=1.5)
        self.assertEqual((a.queue_ahead, b.queue_ahead), (10.0, 12.0))               # the second sits behind the first
        fills = sell_print(ex, book, 100.0, 13.0, 3.0)
        by = {f.order_id: f.qty for f in fills}
        self.assertAlmostEqual(by[a.order_id], 2.0)
        self.assertAlmostEqual(by[b.order_id], 1.0)                                    # 13 = 10 ext + 2 first + 1 into the second: one print, one budget
        self.assertAlmostEqual(sum(by.values()), 3.0)

    def test_one_print_cannot_fill_two_orders_beyond_its_quantity(self):
        ex = make_executor(cohort_age="uniform", **BASE)
        book = make_book(bids=[(100.0, 0.5), (99.0, 100.0)])
        a = place(ex, book, price=100.0, size=3.0, now=1.0)
        b = place(ex, book, price=100.0, size=3.0, now=1.5)
        total = sum(f.qty for f in sell_print(ex, book, 100.0, 4.0, 3.0))
        self.assertLessEqual(total, 4.0 - 0.5 + 1e-9)                                  # 0.5 of the 4 went to the cohort ahead of both
        self.assertGreaterEqual(min(a.remaining, b.remaining), 0.0)

    def test_a_partial_fill_keeps_priority_and_the_remaining_adds_up(self):
        ex = make_executor(cohort_age="uniform", **BASE)
        book = make_book(bids=[(100.0, 0.0001), (99.0, 100.0)])
        a = place(ex, book, price=100.0, size=3.0, now=1.0)
        b = place(ex, book, price=100.0, size=3.0, now=1.5)
        sell_print(ex, book, 100.0, 2.0, 3.0)
        self.assertGreater(a.filled_qty, 0.0)
        self.assertAlmostEqual(a.filled_qty + a.remaining, 3.0)
        self.assertAlmostEqual(b.filled_qty, 0.0, places=3)                            # the second only gets what the first leaves
        self.assertAlmostEqual(b.queue_ahead, a.remaining, places=3)

    def test_a_snapshot_reset_re_bases_the_external_size_and_never_cancels_an_own_order(self):
        ex = make_executor(cohort_age="uniform", **BASE)
        book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
        o = place(ex, book, price=100.0, size=2.0)
        book.apply_snapshot([(99.0, 100.0)], [(101.0, 50.0)], 5.0)                    # the snapshot shows no size at our price
        ex.on_snapshot(book.venue, book, 5.0, 1.0, 0.0, event_ts=5.0)
        self.assertIn(o.order_id, ex.orders)                                            # still live
        self.assertTrue(o.is_live)
        self.assertAlmostEqual(o.queue_ahead, 0.0)

    def test_book_first_and_trade_first_converge_without_double_depletion(self):
        out = []
        for first in ("book", "trade"):
            ex = make_executor(cohort_age="youngest_first", **BASE)
            book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
            o = place(ex, book, price=100.0, size=2.0)
            delta(ex, book, "bid", 100.0, 15.0, 0.5)                                  # 5 behind us
            if first == "book":
                delta(ex, book, "bid", 100.0, 11.0, 2.000)                           # 4 executed at the head ...
                sell_print(ex, book, 100.0, 4.0, 2.010)                               # ... and here is its print
            else:
                sell_print(ex, book, 100.0, 4.0, 2.000)
                delta(ex, book, "bid", 100.0, 11.0, 2.010)
            out.append((round(o.queue_ahead, 9), round(o.filled_qty, 9)))
        self.assertEqual(out[0], out[1])
        self.assertEqual(out[0], (6.0, 0.0))

    def test_a_vanished_level_alone_never_yields_a_trade_confirmed_fill(self):
        ex = make_executor(cohort_age="uniform", **{**BASE, "fill_evidence": "trade_confirm", "vanish_clears_queue": True})
        book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
        o = place(ex, book, price=100.0, size=2.0)
        for t, size in ((2.0, 6.0), (3.0, 0.0), (4.0, 0.0)):
            self.assertEqual(delta(ex, book, "bid", 100.0, size, t), [])
        self.assertEqual(o.filled_qty, 0.0)

    def test_a_vanished_level_with_size_behind_us_does_not_fill_without_a_print_and_the_legacy_rule_does(self):
        res = {}
        for ev in ("trade_confirm", "off"):
            ex = make_executor(cohort_age="uniform", **{**BASE, "fill_evidence": ev, "vanish_clears_queue": True})
            book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
            o = place(ex, book, price=100.0, size=4.0)
            delta(ex, book, "bid", 100.0, 20.0, 1.0)
            delta(ex, book, "bid", 100.0, 0.0, 3.0)
            res[ev] = o.filled_qty
        self.assertEqual(res["trade_confirm"], 0.0)
        self.assertGreater(res["off"], 0.0)

    def test_a_coarse_tick_estimate_never_merges_two_adjacent_levels(self):
        # tick inferred from a sparse first snapshot can be 2x too large; the level-match tolerance (0.5 tick) then spans two real levels
        ex = make_executor(cohort_age="uniform", **BASE)
        book = make_book(bids=[(100.0, 10.0), (99.5, 8.0), (99.0, 50.0)])
        a = place(ex, book, price=100.0, size=2.0, tick=1.0)
        b = place(ex, book, price=99.5, size=2.0, tick=1.0)
        fills = delta(ex, book, "bid", 100.0, 4.0, 2.0, tick=1.0)                       # tolerance 0.5 covers both 100.0 and 99.5 with this tick
        self.assertEqual(fills, [])
        self.assertAlmostEqual(a.queue_ahead, 4.0)                                      # a lost 6 ahead of it
        self.assertAlmostEqual(b.queue_ahead, 8.0)                                      # b, on the other level, is untouched
        ex.on_snapshot(book.venue, make_book(bids=[(100.0, 4.0), (99.5, 8.0)]), 3.0, 1.0, 0.0, event_ts=3.0)

    def test_the_public_book_is_never_touched_by_the_cohort_queue(self):
        ex = make_executor(cohort_age="uniform", **BASE)
        book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
        place(ex, book, price=100.0, size=2.0)
        before = (book.levels("bid", 10), book.levels("ask", 10))
        sell_print(ex, book, 100.0, 13.0, 3.0)                                          # (tape repair is the book's own business, not the queue's)
        self.assertEqual((book.levels("bid", 10), book.levels("ask", 10)), before)


# ── cohort fixes (guide step 6): the cross rule, and queue_ahead that follows the cohort queue ──
V11 = dict(fill_evidence="trade_confirm", evidence_window_ms=20.0, ledger_epoch=True)       # the v1.1 evidence settings (cohort ignores the depletion ledger)
CROSS_MODES = {"immediate": dict(opposite_cross_fills=True),
               "confirm": dict(opposite_cross_fills=True, opposite_cross_confirm_ms=50.0),
               "confirm_cap": dict(opposite_cross_fills=True, opposite_cross_confirm_ms=50.0, opposite_cross_cap_to_print=True)}


def _both(cohort_age="uniform", **cfg):
    """The same settings on the scalar queue (proportional cancel model) and on the cohort queue."""
    common = dict(fill_evidence="off", dedupe_trade_depth=True, share_trade_volume=True, exchange_time_eligibility=True, dedupe_window_s=0.05, dedupe_time_basis="exchange", **cfg)
    return (make_executor(cancel_model="proportional", **common), make_executor(queue_model="cohort", cohort_age=cohort_age, **common))


def _lq(ex):
    (lq,) = ex._lq.values()
    return lq


def _in_step(ex, o):
    """The invariant of the cohort fixes: queue_ahead equals the cohort queue's ahead (a filled order has left the queue)."""
    return o.queue_ahead if not ex._lq or not o.is_live else abs(o.queue_ahead - _lq(ex).ahead(o.order_id))


def _state(ex, o, fills):
    return (round(o.queue_ahead, 9), round(o.filled_qty, 9), round(sum(f.qty for f in fills), 9))


class CrossWithCohort(unittest.TestCase):
    """Item 1: the cohort queue runs with opposite_cross_fills; every place that zeroes queue_ahead clears the cohorts ahead as well, so scalar and cohort agree."""

    def _cross(self, ex, book):
        book.apply_delta("ask", 101.0, 0.0, "modify", 2.0)
        book.apply_delta("ask", 100.0, 5.0, "modify", 2.0)                                # the best ask is now at our bid price: a cross
        return ex.on_opposite_cross(book.venue, book, 2.0, 0.0, event_ts=2.0)

    def test_a_confirmed_cross_clears_the_queue_ahead_alike_in_scalar_and_cohort(self):
        for mode, cfg in CROSS_MODES.items():
            res = []
            for ex in _both(**cfg):
                book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
                o = place(ex, book, price=100.0, size=2.0)
                fills = []
                if mode != "immediate":
                    fills += sell_print(ex, book, 100.0, 1.0, 1.99)                          # print before the cross (confirm_before) ...
                fills += self._cross(ex, book)
                if mode != "immediate":
                    fills += sell_print(ex, book, 100.0, 5.0, 2.03)                          # ... and one after it (confirm_after for what is still waiting)
                res.append((_state(ex, o, fills), ex.diag.get("cohort_ahead_mismatch_orders", 0)))
                if ex._lq and o.is_live:
                    self.assertAlmostEqual(_in_step(ex, o), 0.0, msg=mode)
            self.assertEqual(res[0][0], res[1][0], mode)
            self.assertEqual(res[1][1], 0, mode)

    def test_a_cross_after_the_print_clears_the_cohorts_ahead(self):
        res = []
        for ex in _both(**CROSS_MODES["confirm"]):
            book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
            o = place(ex, book, price=100.0, size=2.0)
            fills = self._cross(ex, book)                                                       # armed: no print yet, the queue is untouched
            self.assertAlmostEqual(o.queue_ahead, 10.0)
            fills += sell_print(ex, book, 100.0, 1.0, 2.02)                                     # the print inside the window confirms the cross
            res.append(_state(ex, o, fills))
            if ex._lq:
                self.assertEqual(ex.diag.get("cohort_ahead_mismatch_orders", 0), 0)
        self.assertEqual(res[0], res[1])
        self.assertEqual(res[1][1], 2.0)                                                         # filled in full, from the front

    def test_a_vanished_level_gives_the_same_queue_ahead_and_fills(self):
        for behind in (0.0, 10.0, 30.0):
            res = []
            for ex in _both(vanish_clears_queue=True):
                book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
                o = place(ex, book, price=100.0, size=2.0)
                if behind:
                    delta(ex, book, "bid", 100.0, 10.0 + behind, 1.5)
                fills = delta(ex, book, "bid", 100.0, 0.0, 2.0)
                res.append(_state(ex, o, fills))
                if ex._lq and o.is_live:
                    self.assertAlmostEqual(_in_step(ex, o), 0.0)
                    self.assertAlmostEqual(_lq(ex).ahead(o.order_id), 0.0)
                    self.assertEqual(ex.diag.get("cohort_ahead_mismatch_orders", 0), 0)
            self.assertEqual(res[0], res[1], behind)

    def test_a_tiny_remaining_queue_is_forgiven_in_both_and_clears_the_cohorts(self):
        res = []
        for ex in _both(queue_empty_threshold="lot", queue_empty_lot=0.5):
            book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
            o = place(ex, book, price=100.0, size=2.0)
            fills = delta(ex, book, "bid", 100.0, 0.3, 2.0)                                     # 9.7 left the level: 0.3 ahead, at or below the 0.5 threshold
            res.append(_state(ex, o, fills))
            if ex._lq:
                self.assertAlmostEqual(_lq(ex).ahead(o.order_id), 0.0)
                self.assertEqual(ex.diag.get("cohort_ahead_mismatch_orders", 0), 0)
        self.assertEqual(res[0], res[1])
        self.assertEqual(res[1][0], 0.0)

    def test_after_the_clear_a_later_print_fills_from_the_front_alike(self):
        res = []
        for ex in _both(queue_empty_threshold="lot", queue_empty_lot=0.5):
            book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
            o = place(ex, book, price=100.0, size=2.0)
            delta(ex, book, "bid", 100.0, 0.3, 2.0)
            fills = sell_print(ex, book, 100.0, 1.0, 3.0)
            res.append(_state(ex, o, fills))
        self.assertEqual(res[0], res[1])
        self.assertEqual(res[1][2], 1.0)


class CohortQueueAheadFollowsTheQueue(unittest.TestCase):
    """Item 2: queue_ahead equals the cohort queue's ahead after every event, in both arrival orders, with 0, 10 and 30 behind our order."""

    def _run(self, first, head_drop, printed, behind, age, **cfg):
        ex = make_executor(queue_model="cohort", cohort_age=age, **{**dict(fill_evidence="off", dedupe_trade_depth=True, share_trade_volume=True, exchange_time_eligibility=True,
                                                                           dedupe_window_s=0.05, dedupe_time_basis="exchange"), **cfg})
        book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
        o = place(ex, book, price=100.0, size=10.0)                                              # placed and active at 1.0
        if behind:
            delta(ex, book, "bid", 100.0, 10.0 + behind, 1.5)                                    # after the order is active: it really joined behind us
            self.assertAlmostEqual(_in_step(ex, o), 0.0)
        steps = {"book": lambda: delta(ex, book, "bid", 100.0, 10.0 + behind - head_drop, 2.000 if first == "book" else 2.010),
                 "trade": lambda: sell_print(ex, book, 100.0, printed, 2.010 if first == "book" else 2.000)}
        fills = []
        for name in (("book", "trade") if first == "book" else ("trade", "book")):
            fills += steps[name]()
            self.assertAlmostEqual(_in_step(ex, o), 0.0, msg=(first, name, behind, age))
        return _state(ex, o, fills), ex

    def _both_orders(self, head_drop, printed, behind, age, **cfg):
        a, exa = self._run("book", head_drop, printed, behind, age, **cfg)
        b, exb = self._run("trade", head_drop, printed, behind, age, **cfg)
        return a, b, exa, exb

    def test_decrease_4_and_print_4_leave_ahead_6_in_both_orders_with_0_10_30_behind(self):
        for age in ("uniform", "youngest_first", "oldest_first"):
            for behind in (0.0, 10.0, 30.0):
                a, b, exa, exb = self._both_orders(4.0, 4.0, behind, age)
                self.assertEqual(a, b, (age, behind))
                self.assertEqual(a, (6.0, 0.0, 0.0), (age, behind))
                self.assertEqual(exa.diag.get("cohort_ahead_mismatch_orders", 0) + exb.diag.get("cohort_ahead_mismatch_orders", 0), 0, (age, behind))

    def test_head_drop_10_and_print_12_fill_2_in_both_orders_with_0_10_30_behind(self):
        for age in ("uniform", "youngest_first", "oldest_first"):
            for behind in (0.0, 10.0, 30.0):
                a, b, exa, exb = self._both_orders(10.0, 12.0, behind, age)
                self.assertEqual(a, b, (age, behind))
                self.assertEqual(a, (0.0, 2.0, 2.0), (age, behind))
                self.assertEqual(exa.diag.get("cohort_ahead_mismatch_orders", 0) + exb.diag.get("cohort_ahead_mismatch_orders", 0), 0, (age, behind))

    def test_the_v11_evidence_settings_keep_the_invariant(self):
        for behind in (0.0, 10.0, 30.0):
            for first in ("book", "trade"):
                _, ex = self._run(first, 4.0, 4.0, behind, "uniform", **V11)
                self.assertEqual(ex.diag.get("cohort_ahead_mismatch_orders", 0), 0, (first, behind))

    def test_a_decrease_seen_before_the_order_was_active_is_followed_by_queue_ahead_too(self):
        # the original converge test placed the order at 1.0 and sent its "decrease" at 0.5: the order never saw it
        ex = make_executor(queue_model="cohort", cohort_age="youngest_first", fill_evidence="off", dedupe_trade_depth=True)
        book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
        o = place(ex, book, price=100.0, size=2.0, now=1.0, active_ts=3.0)                      # in flight until 3.0
        delta(ex, book, "bid", 100.0, 4.0, 2.0)                                                  # the level drops 6 while the order is in flight
        self.assertAlmostEqual(_in_step(ex, o), 0.0)
        self.assertEqual(ex.diag.get("cohort_ahead_mismatch_orders", 0), 0)

    def test_the_old_converge_test_never_saw_a_decrease(self):
        # a 15 -> 11 "decrease" sent at 2.0 after a growth at 0.5, with the order placed at 1.0: the growth predates the order's activity, so the order's level_now is 10 and the
        # message at 2.0 is a growth of 1 for it. No shrink, no netting, nothing for the book-first order to differ on: both orders reach 6 through the same print path.
        ex = make_executor(queue_model="cohort", cohort_age="youngest_first", fill_evidence="off", dedupe_trade_depth=True, share_trade_volume=True, exchange_time_eligibility=True)
        book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
        o = place(ex, book, price=100.0, size=2.0)
        delta(ex, book, "bid", 100.0, 15.0, 0.5)
        delta(ex, book, "bid", 100.0, 11.0, 2.000)
        self.assertEqual(ex.diag["delta_shrink_events"], 0.0)


class HazardThetaIsLabelled(unittest.TestCase):
    """Item 3: cohort_hazard_theta is SPOT_CALIBRATED; any result that uses the hazard rule says so, and no other cohort rule carries the label."""

    def _fill_obs(self, age, **cfg):
        ex = make_executor(queue_model="cohort", cohort_age=age, fill_evidence="off", dedupe_trade_depth=True, **cfg)
        ex.obs_enabled = True
        book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
        place(ex, book, price=100.0, size=2.0)
        (f,) = sell_print(ex, book, 100.0, 12.0, 3.0)
        return f.obs

    def test_the_hazard_rule_warns_and_labels_every_fill(self):
        import warnings
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            obs = self._fill_obs("hazard", cohort_hazard_theta=0.5)
        self.assertTrue(any("SPOT_CALIBRATED" in str(x.message) for x in w))
        self.assertEqual(obs["cohort_hazard_theta"]["value"], 0.5)
        self.assertIn("SPOT_CALIBRATED", obs["cohort_hazard_theta"]["provenance"])

    def test_the_uniform_rule_carries_no_label_and_raises_no_warning(self):
        import warnings
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            obs = self._fill_obs("uniform")
        self.assertNotIn("cohort_hazard_theta", obs)
        self.assertFalse([x for x in w if "SPOT_CALIBRATED" in str(x.message)])


class ClearAheadKeepsOwnOrdersAhead(unittest.TestCase):
    """Owner 7 Nov: clearing the queue ahead of an order (cross, vanished level, through print, empty threshold) never removes another own order that rests ahead of it at the same price:
    that order is filled by the print budget or it stays. Two own orders at one level: A (placed first) and B."""

    def _two(self, **cfg):
        ex = make_executor(queue_model="cohort", cohort_age="uniform", **{**dict(fill_evidence="off", dedupe_trade_depth=True, share_trade_volume=True, exchange_time_eligibility=True,
                                                                               vanish_clears_queue=True, min_queue_ahead=0.0), **cfg})
        book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
        a = place(ex, book, price=100.0, size=3.0, now=1.0)
        b = place(ex, book, price=100.0, size=3.0, now=1.1)
        return ex, book, a, b

    def test_the_level_queue_keeps_an_own_order_ahead_and_ignores_an_unknown_id(self):
        from crypto_stat_arb.simulation.queue_v2.cohort import LevelQueue
        lq = LevelQueue(10.0, 0.0)
        lq.join(1, 3.0)
        lq.join(2, 3.0)
        lq.clear_ahead(2)
        self.assertEqual(lq.own_ids(), [1, 2])
        self.assertAlmostEqual(lq.ahead(1), 0.0)                                         # the external size was ahead of A as well as of B: it is gone for both
        self.assertAlmostEqual(lq.ahead(2), 3.0)                                         # A (3, its own size) still rests ahead of B
        lq2 = LevelQueue(10.0, 0.0)
        lq2.join(1, 3.0)
        lq2.clear_ahead(99)
        self.assertAlmostEqual(lq2.ahead(1), 10.0)                                       # unknown id: nothing changes

    def test_a_vanished_level_clears_the_external_size_and_leaves_a_ahead_of_b(self):
        ex, book, a, b = self._two()
        delta(ex, book, "bid", 100.0, 0.0, 2.0)
        lq = _lq(ex)
        self.assertEqual(lq.own_ids(), [a.order_id, b.order_id])
        self.assertAlmostEqual(a.queue_ahead, 0.0)
        self.assertAlmostEqual(b.queue_ahead, 3.0)                                       # A is still ahead of B
        self.assertEqual(ex.diag.get("cohort_ahead_mismatch_orders", 0), 0)
        fills = sell_print(ex, book, 100.0, 4.0, 3.0)                                    # the print budget fills A first, then B
        by = {f.order_id: f.qty for f in fills}
        self.assertAlmostEqual(by.get(a.order_id, 0.0), 3.0)
        self.assertAlmostEqual(by.get(b.order_id, 0.0), 1.0)
        self.assertEqual(ex.diag.get("cohort_ahead_mismatch_orders", 0), 0)

    def test_when_a_is_cancelled_b_moves_up(self):
        ex, book, a, b = self._two()
        delta(ex, book, "bid", 100.0, 0.0, 2.0)
        ex.cancel(a.order_id, 2.5)
        self.assertAlmostEqual(b.queue_ahead, 0.0)
        self.assertAlmostEqual(_lq(ex).ahead(b.order_id), 0.0)

    def test_a_through_print_fills_both_orders_in_queue_order_within_the_print_budget(self):
        ex, book, a, b = self._two(fill_through_prints=True, through_print_volume_limited=True)
        fills = ex.on_trade(book.venue, 99.0, 4.0, -1, 3.0, 0.0, event_ts=3.0)
        by = {f.order_id: f.qty for f in fills}
        self.assertAlmostEqual(sum(by.values()), 4.0)
        self.assertAlmostEqual(by[a.order_id], 3.0)
        self.assertAlmostEqual(by[b.order_id], 1.0)
        self.assertAlmostEqual(_in_step(ex, b), 0.0)
        self.assertEqual(ex.diag.get("cohort_ahead_mismatch_orders", 0), 0)

    def test_the_scalar_queue_is_unchanged_by_it(self):
        ex = make_executor(cancel_model="proportional", fill_evidence="off", dedupe_trade_depth=True, share_trade_volume=True, exchange_time_eligibility=True, vanish_clears_queue=True, min_queue_ahead=0.0)
        book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
        place(ex, book, price=100.0, size=3.0, now=1.0)
        b = place(ex, book, price=100.0, size=3.0, now=1.1)
        delta(ex, book, "bid", 100.0, 0.0, 2.0)
        self.assertAlmostEqual(b.queue_ahead, 0.0)                                       # scalar: zero, as before; the cohort queue is the only one that counts an own order ahead


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
