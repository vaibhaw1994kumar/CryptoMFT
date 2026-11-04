"""Queue model v2, step 4: the depletion ledger fixtures (guide s4), both arrival orders. Credit = volume one feed already attributed to an execution.

Trade-first: the print reduces the queue ahead once and stores explained-volume credit; the later depth decrease is netted against it.
Book-first: the depth decrease moves the head provisionally with no fill; when the matching print arrives, only the part the print adds beyond the movement
already applied can reach our order."""
from __future__ import annotations

import unittest

from qv2_support import delta, make_book, make_executor, place, sell_print

from new_backtest.models import Side
from new_backtest.order_book import OrderBook

BASE = dict(cancel_model="proportional", fill_evidence="off", dedupe_trade_depth=True, share_trade_volume=True, exchange_time_eligibility=True,
            dedupe_window_s=0.05, dedupe_time_basis="exchange")


def _setup(ahead=10.0, own=10.0, **cfg):
    ex = make_executor(**{**BASE, **cfg})
    book = make_book(bids=[(100.0, ahead), (99.0, 100.0)])
    o = place(ex, book, price=100.0, size=own)
    return ex, book, o


class BookFirst(unittest.TestCase):
    def test_head_reduction_4_then_matched_print_4_leaves_ahead_at_6(self):
        ex, book, o = _setup()
        self.assertEqual(delta(ex, book, "bid", 100.0, 6.0, 2.000), [])                # provisional head movement: 10 -> 6, no fill
        self.assertAlmostEqual(o.queue_ahead, 6.0)
        self.assertEqual(sell_print(ex, book, 100.0, 4.0, 2.010), [])                  # the same execution reported as a print: credited, not drained again
        self.assertAlmostEqual(o.queue_ahead, 6.0)
        self.assertEqual(o.filled_qty, 0.0)

    def test_head_reduction_10_then_matched_print_12_credits_10_and_lets_2_reach_the_order(self):
        ex, book, o = _setup()
        self.assertEqual(delta(ex, book, "bid", 100.0, 0.0, 2.000), [])                # the whole level goes: ahead 10 -> 0, no fill from the decrease alone
        self.assertAlmostEqual(o.queue_ahead, 0.0)
        (f,) = sell_print(ex, book, 100.0, 12.0, 2.010)
        self.assertAlmostEqual(f.qty, 2.0)                                              # 12 printed, 10 already explained by the head movement
        self.assertEqual(f.cause, "trade")

    def test_an_unexplained_decrease_alone_never_produces_a_trade_confirmed_fill(self):
        for ev in ("trade_confirm", "confirm_delta"):
            ex, book, o = _setup(fill_evidence=ev, vanish_clears_queue=True)
            for t, size in ((2.0, 6.0), (3.0, 2.0), (4.0, 0.0), (5.0, 0.0)):         # the queue empties, the level vanishes, nothing prints
                self.assertEqual(delta(ex, book, "bid", 100.0, size, t), [], ev)
            self.assertEqual(o.filled_qty, 0.0, ev)
            self.assertEqual(ex.diag["fills_from_delta"], 0.0)


class VanishedLevel(unittest.TestCase):
    """A level that disappears (size 10 behind us as well) is NOT a trade: with evidence required it moves us to the front and fills nothing; without it, the legacy rule fills."""
    def _vanish(self, **cfg):
        ex, book, o = _setup(ahead=10.0, own=4.0, vanish_clears_queue=True, **cfg)
        delta(ex, book, "bid", 100.0, 20.0, 1.0)                                         # 10 more arrive BEHIND us
        fills = delta(ex, book, "bid", 100.0, 0.0, 3.0)                                  # the whole level vanishes, no print
        return fills, o

    def test_with_evidence_required_a_vanished_level_alone_never_fills(self):
        for ev in ("trade_confirm", "trade_confirm+fractional"):
            fills, o = self._vanish(fill_evidence=ev)
            self.assertEqual((fills, o.filled_qty), ([], 0.0), ev)
            self.assertAlmostEqual(o.queue_ahead, 0.0)                                   # it is at the front, waiting for a print

    def test_confirm_delta_the_v1_profile_setting_does_NOT_gate_a_vanished_level(self):
        # documented in MakerConfig.fill_evidence ("a level that vanished is NOT gated and behaves as off"): the shipped v1 profile therefore lets a vanished level fill
        fills, o = self._vanish(fill_evidence="confirm_delta")
        self.assertGreater(o.filled_qty, 0.0)

    def test_the_legacy_rule_does_fill_on_the_vanish_and_that_is_why_it_is_not_trade_confirmed(self):
        fills, o = self._vanish(fill_evidence="off")
        self.assertGreater(o.filled_qty, 0.0)


class TradeFirst(unittest.TestCase):
    def test_print_4_then_matched_decrease_4_leaves_ahead_at_6(self):
        ex, book, o = _setup()
        self.assertEqual(sell_print(ex, book, 100.0, 4.0, 2.000), [])
        self.assertAlmostEqual(o.queue_ahead, 6.0)                                      # the print reduced the queue ahead once
        self.assertEqual(delta(ex, book, "bid", 100.0, 6.0, 2.010), [])                # the depth decrease is the same execution: netted against the credit
        self.assertAlmostEqual(o.queue_ahead, 6.0)

    def test_print_12_then_matched_decrease_10_fills_2_once(self):
        ex, book, o = _setup()
        (f,) = sell_print(ex, book, 100.0, 12.0, 2.000)
        self.assertAlmostEqual(f.qty, 2.0)
        self.assertEqual(delta(ex, book, "bid", 100.0, 0.0, 2.010), [])                # 10 explained: no second fill, no second drain
        self.assertAlmostEqual(o.filled_qty, 2.0)
        self.assertAlmostEqual(o.queue_ahead, 0.0)

    def test_the_two_arrival_orders_converge_to_the_same_state(self):
        a, bookA, oa = _setup()
        delta(a, bookA, "bid", 100.0, 0.0, 2.000)
        fa = sell_print(a, bookA, 100.0, 12.0, 2.010)
        b, bookB, ob = _setup()
        fb = sell_print(b, bookB, 100.0, 12.0, 2.000)
        delta(b, bookB, "bid", 100.0, 0.0, 2.010)
        self.assertAlmostEqual(sum(f.qty for f in fa), sum(f.qty for f in fb))
        self.assertAlmostEqual(oa.queue_ahead, ob.queue_ahead)
        self.assertAlmostEqual(oa.remaining, ob.remaining)


class Credits(unittest.TestCase):
    def test_a_credit_is_used_once(self):
        ex, book, o = _setup(ahead=20.0)
        delta(ex, book, "bid", 100.0, 16.0, 2.000)                                      # 4 left the level (ahead 20 -> 16)
        sell_print(ex, book, 100.0, 4.0, 2.010)                                         # uses the 4 of credit
        self.assertAlmostEqual(o.queue_ahead, 16.0)
        sell_print(ex, book, 100.0, 4.0, 2.020)                                         # a SECOND print has no credit left: it drains for real
        self.assertAlmostEqual(o.queue_ahead, 12.0)

    def test_a_credit_expires_with_its_window(self):
        ex, book, o = _setup(ahead=20.0)
        delta(ex, book, "bid", 100.0, 16.0, 2.000)
        sell_print(ex, book, 100.0, 4.0, 2.200)                                         # 200 ms later: outside the 50 ms window, not the same execution
        self.assertAlmostEqual(o.queue_ahead, 12.0)

    def test_a_snapshot_or_reconnect_invalidates_old_credit(self):
        ex, book, o = _setup(ahead=20.0, ledger_epoch=True)
        delta(ex, book, "bid", 100.0, 16.0, 2.000)
        book.apply_snapshot([(100.0, 16.0), (99.0, 100.0)], [(101.0, 50.0)], 2.005)
        ex.on_snapshot(book.venue, book, 2.005, 1.0, 0.0, event_ts=2.005)
        sell_print(ex, book, 100.0, 4.0, 2.010)                                         # inside the window, but the connection epoch changed
        self.assertAlmostEqual(o.queue_ahead, 12.0)

    def test_without_the_epoch_switch_the_legacy_behaviour_is_unchanged(self):
        ex, book, o = _setup(ahead=20.0)
        delta(ex, book, "bid", 100.0, 16.0, 2.000)
        book.apply_snapshot([(100.0, 16.0), (99.0, 100.0)], [(101.0, 50.0)], 2.005)
        ex.on_snapshot(book.venue, book, 2.005, 1.0, 0.0, event_ts=2.005)
        sell_print(ex, book, 100.0, 4.0, 2.010)
        self.assertAlmostEqual(o.queue_ahead, 16.0)                                     # legacy: the credit survives the snapshot (documented gap)

    def test_ambiguous_association_is_flagged_not_silently_resolved(self):
        ex, book, o = _setup(ahead=30.0, ledger_epoch=True)
        ex.obs_enabled = True
        delta(ex, book, "bid", 100.0, 26.0, 2.000)                                      # two separate decreases at one level inside one window ...
        delta(ex, book, "bid", 100.0, 22.0, 2.010)
        sell_print(ex, book, 100.0, 5.0, 2.020)                                         # ... one print: which decrease was it?
        self.assertGreaterEqual(ex.diag["ledger_ambiguous"], 1)
        cc = ex._obs_trig["competing_credits"]                                           # the competing readings are kept with the event, newest and oldest pending credit
        self.assertEqual(len(cc), 2)
        self.assertAlmostEqual(sum(q for _, q in cc), 8.0)


class AlternateScenariosAreKeptWithTheFill(unittest.TestCase):
    """Guide s4: where the association is ambiguous, retain the alternate reading (or flag). The fill record keeps both."""
    def test_a_print_fill_without_ambiguity_has_no_alternates_and_is_association_robust(self):
        ex, book, o = _setup(ahead=0.0, own=2.0, fill_evidence="off")
        ex.obs_enabled = True
        (f,) = sell_print(ex, book, 100.0, 5.0, 2.0)
        self.assertEqual(f.obs["alternates"], [])
        self.assertTrue(f.obs["association_robust"])
        self.assertNotIn("ambiguous_trade_depth", f.obs["flags"])

    def test_a_depth_decrease_fill_keeps_the_reading_that_it_was_a_cancellation(self):
        ex, book, o = _setup(ahead=0.0, own=2.0, fill_evidence="off")
        ex.obs_enabled = True
        ex._obs_begin("delta", price=100.0, side="bid", new_size=0.0, event_ts=2.0, ambiguous=True)
        f = ex._fill_from(o, 2.0, 2.0, 1.0, "delta", 0.0, False, cause="delta_vanish")
        self.assertIn("ambiguous_trade_depth", f.obs["flags"])
        self.assertFalse(f.obs["association_robust"])
        (alt,) = f.obs["alternates"]
        self.assertEqual((alt["scenario"], alt["own_fill_qty"]), ("depletion_was_cancellation_not_execution", 0.0))

    def test_an_ambiguous_credit_choice_is_kept_with_the_fill_it_touches(self):
        ex, book, o = _setup(ahead=0.0, own=2.0, ledger_epoch=True, fill_evidence="off")
        ex.obs_enabled = True
        ex._obs_begin("delta", price=100.0, side="bid", new_size=0.0, event_ts=2.0)
        ex._obs_trig["competing_credits"] = [[0.02, 4.0], [0.01, 4.0]]
        f = ex._fill_from(o, 2.0, 2.0, 1.0, "trade", 0.0, False, cause="trade")
        self.assertEqual([a["scenario"] for a in f.obs["alternates"]], ["decrease_explained_by_a_different_pending_print"])
        self.assertEqual(f.obs["alternates"][0]["competing_credits_age_s_qty"], [[0.02, 4.0], [0.01, 4.0]])


class DoubleDepletionAtTheBook(unittest.TestCase):
    """The user's case: a depth decrease first, then the print of the same execution. The engine book's tape repair subtracts the print AGAIN."""
    def _book(self, repair):
        b = OrderBook("bybit", "SOLUSDT", delta_semantics="absolute", tape_repair=repair)
        b.apply_snapshot([(100.0, 10.0), (99.0, 50.0)], [(101.0, 20.0)], 0.0)
        return b

    def test_with_tape_repair_the_level_is_reduced_twice(self):
        b = self._book(True)
        b.apply_delta("bid", 100.0, 6.0, "modify", 1.0)                                 # 4 executed at the head (depth message)
        b.on_trade(100.0, 4.0, -1, 1.01)                                                # the print of the same 4
        self.assertAlmostEqual(b.level_size("bid", 100.0), 2.0)                         # displayed 2, the exchange still has 6: double depletion
        self.assertEqual(b.trade_repairs, 0)

    def test_without_tape_repair_the_level_stays_at_the_exchange_value(self):
        b = self._book(False)
        b.apply_delta("bid", 100.0, 6.0, "modify", 1.0)
        b.on_trade(100.0, 4.0, -1, 1.01)
        self.assertAlmostEqual(b.level_size("bid", 100.0), 6.0)
        self.assertEqual(b.trades_applied, 1)

    def test_trade_first_the_next_absolute_message_overwrites_the_repair(self):
        b = self._book(True)
        b.on_trade(100.0, 4.0, -1, 1.0)
        self.assertAlmostEqual(b.level_size("bid", 100.0), 6.0)
        b.apply_delta("bid", 100.0, 6.0, "modify", 1.01)
        self.assertAlmostEqual(b.level_size("bid", 100.0), 6.0)                         # no double depletion in this order


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
