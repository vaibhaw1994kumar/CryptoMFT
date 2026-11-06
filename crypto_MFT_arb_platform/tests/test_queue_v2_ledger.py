"""Queue model v2, step 4: the depletion ledger fixtures (guide s4), both arrival orders. Credit = volume one feed already attributed to an execution.
The first half runs on the BASE settings below; the second half (V11Fixtures ... NettedWithoutProof) runs every fixture on the v1.1 maker settings, in both arrival orders, with 0, 10 and 30 of size behind
our order, and covers the v1_2 switch MakerConfig.depletion_ledger (off = today's behaviour, pinned; on = both orders agree when the print is at least as large as the decrease).

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


# ── depletion ledger on the v1.1 settings (owner brief, Phase B) ──────────────────────────────────────────────────────────────────────────────────────────────

V11 = dict(cancel_model="proportional", fill_evidence="trade_confirm", ledger_epoch=True, dedupe_trade_depth=True, share_trade_volume=True,
           exchange_time_eligibility=True, dedupe_window_s=0.05, dedupe_time_basis="exchange", evidence_window_ms=20.0, vanish_clears_queue=False)
BEHIND = (0, 10, 30)


def _pair(first, behind, own, ledger, D, P, ahead=10.0, ts=(2.000, 2.010), **cfg):
    """One execution published twice: a depth decrease of D at the head and a sell print of P. `first` is "book" or "trade"; `behind` is added behind us after we rest (size we cannot see move)."""
    ex = make_executor(**{**V11, "depletion_ledger": ledger, **cfg})
    book = make_book(bids=[(100.0, ahead), (99.0, 100.0)])
    o = place(ex, book, price=100.0, size=own)
    level = ahead
    if behind:
        delta(ex, book, "bid", 100.0, ahead + behind, 1.5)
        level = ahead + behind
    if first == "book":
        fills = delta(ex, book, "bid", 100.0, level - D, ts[0]) + sell_print(ex, book, 100.0, P, ts[1])
    else:
        fills = sell_print(ex, book, 100.0, P, ts[0]) + delta(ex, book, "bid", 100.0, level - D, ts[1])
    return ex, o, (round(o.queue_ahead, 6), round(sum(f.qty for f in fills), 6))


class V11Fixtures(unittest.TestCase):
    """Every fixture of the file above, now on the v1.1 settings (scalar queue, proportional, fill_evidence trade_confirm, ledger_epoch on) and with 0, 10 and 30 behind us."""
    def test_head_reduction_then_matched_print_leaves_the_queue_where_the_print_alone_would(self):
        for ledger in (False, True):
            for behind in BEHIND:
                _, o, (ahead, filled) = _pair("trade", behind, 10.0, ledger, 4.0, 4.0)
                self.assertEqual((ahead, filled), (6.0, 0.0), (ledger, behind))              # print first: the print drains 4 once, the decrease is netted, with or without the switch

    def test_an_unexplained_decrease_alone_never_fills_on_the_v1_1_settings(self):
        for ledger in (False, True):
            for behind in BEHIND:
                ex = make_executor(**{**V11, "depletion_ledger": ledger})
                book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
                o = place(ex, book, price=100.0, size=10.0)
                level = 10.0
                if behind:
                    delta(ex, book, "bid", 100.0, 10.0 + behind, 1.5)
                    level += behind
                for t, size in ((2.0, level * 0.6), (3.0, level * 0.2), (4.0, 0.0), (5.0, 0.0)):
                    self.assertEqual(delta(ex, book, "bid", 100.0, size, t), [], (ledger, behind))
                self.assertEqual(o.filled_qty, 0.0)
                self.assertEqual(ex.diag["fills_from_delta"], 0.0)

    def test_a_vanished_level_alone_never_fills_on_the_v1_1_settings(self):
        for ledger in (False, True):
            for behind in BEHIND:
                ex = make_executor(**{**V11, "depletion_ledger": ledger})
                book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
                o = place(ex, book, price=100.0, size=4.0)
                delta(ex, book, "bid", 100.0, 20.0 + behind, 1.0)
                self.assertEqual(delta(ex, book, "bid", 100.0, 0.0, 3.0), [])
                self.assertEqual(o.filled_qty, 0.0)
                self.assertAlmostEqual(o.queue_ahead, 0.0)

    def test_a_credit_is_used_once_and_expires_with_its_window(self):
        for ledger in (False, True):
            for behind in BEHIND:
                ex = make_executor(**{**V11, "depletion_ledger": ledger})
                book = make_book(bids=[(100.0, 20.0), (99.0, 100.0)])
                o = place(ex, book, price=100.0, size=10.0)
                level = 20.0
                if behind:
                    delta(ex, book, "bid", 100.0, 20.0 + behind, 1.5)
                    level += behind
                delta(ex, book, "bid", 100.0, level - 4.0, 2.000)
                sell_print(ex, book, 100.0, 4.0, 2.010)
                first = o.queue_ahead
                sell_print(ex, book, 100.0, 4.0, 2.020)                                       # a second print has no credit left: it drains for real
                self.assertAlmostEqual(first - o.queue_ahead, 4.0, msg=(ledger, behind))
                late = make_executor(**{**V11, "depletion_ledger": ledger})
                b2 = make_book(bids=[(100.0, 20.0), (99.0, 100.0)])
                o2 = place(late, b2, price=100.0, size=10.0)
                delta(late, b2, "bid", 100.0, 16.0, 2.000)
                before = o2.queue_ahead
                sell_print(late, b2, 100.0, 4.0, 2.200)                                       # outside the 50 ms window: not the same execution
                self.assertAlmostEqual(before - o2.queue_ahead, 4.0, msg=(ledger, behind))

    def test_a_snapshot_invalidates_the_credit_on_the_v1_1_settings(self):
        for ledger in (False, True):
            ex = make_executor(**{**V11, "depletion_ledger": ledger})
            book = make_book(bids=[(100.0, 20.0), (99.0, 100.0)])
            o = place(ex, book, price=100.0, size=10.0)
            delta(ex, book, "bid", 100.0, 16.0, 2.000)
            book.apply_snapshot([(100.0, 16.0), (99.0, 100.0)], [(101.0, 50.0)], 2.005)
            ex.on_snapshot(book.venue, book, 2.005, 1.0, 0.0, event_ts=2.005)
            before = o.queue_ahead
            sell_print(ex, book, 100.0, 4.0, 2.010)
            self.assertAlmostEqual(before - o.queue_ahead, 4.0, msg=ledger)

    def test_two_decreases_and_one_print_is_flagged_ambiguous_on_the_v1_1_settings(self):
        for ledger in (False, True):
            ex = make_executor(**{**V11, "depletion_ledger": ledger})
            book = make_book(bids=[(100.0, 30.0), (99.0, 100.0)])
            place(ex, book, price=100.0, size=10.0)
            delta(ex, book, "bid", 100.0, 26.0, 2.000)
            delta(ex, book, "bid", 100.0, 22.0, 2.010)
            sell_print(ex, book, 100.0, 5.0, 2.020)
            self.assertGreaterEqual(ex.diag["ledger_ambiguous"], 1)


class TodayTheTwoArrivalOrdersDisagree(unittest.TestCase):
    """The owner's expectation, measured on the v1.1 settings with the switch OFF (today's behaviour, pinned so a change to it is deliberate)."""
    def test_book_first_leaves_the_queue_further_back_when_size_sits_behind_us(self):
        got = {b: _pair("book", b, 10.0, False, 4.0, 4.0)[2][0] for b in BEHIND}
        self.assertEqual(got, {0: 6.0, 10: 8.0, 30: 9.0})                                     # trade-first gives 6.0 in all three rows (test above)

    def test_head_drop_10_then_print_12_fills_10_instead_of_2_with_nobody_behind(self):
        self.assertEqual(_pair("book", 0, 10.0, False, 10.0, 12.0)[2], (0.0, 10.0))           # trade_after_front uses the whole print although 10 of it already moved the head
        self.assertEqual(_pair("trade", 0, 10.0, False, 10.0, 12.0)[2], (0.0, 2.0))

    def test_every_pair_with_a_print_at_least_as_large_as_the_decrease_differs_between_the_orders_somewhere(self):
        diff = [(D, P, b) for D, P in ((4, 4), (10, 12), (10, 10), (4, 6), (2, 4), (3, 3)) for b in BEHIND if D <= 10 + b
                and _pair("book", b, 10.0, False, float(D), float(P))[2] != _pair("trade", b, 10.0, False, float(D), float(P))[2]]
        self.assertGreaterEqual(len(diff), 12)


class DepletionLedgerSwitch(unittest.TestCase):
    PAIRS_PRINT_AT_LEAST_DECREASE = ((4, 4), (10, 12), (10, 10), (4, 6), (2, 4), (3, 3), (20, 12), (20, 20))

    def test_the_switch_is_off_by_default_and_the_settings_hash_of_every_earlier_profile_is_unchanged(self):
        from new_backtest.fill_engine import MakerConfig
        self.assertFalse(MakerConfig().depletion_ledger)
        off, on = make_executor(**V11), make_executor(**{**V11, "depletion_ledger": True})
        self.assertNotEqual(off._settings_sha(), on._settings_sha())
        import dataclasses
        import hashlib
        import json
        legacy = {k: v for k, v in dataclasses.asdict(off.config).items() if k != "depletion_ledger"}
        self.assertEqual(off._settings_sha(), hashlib.sha256(json.dumps(legacy, sort_keys=True, default=str).encode()).hexdigest())

    def test_print_first_is_not_changed_by_the_switch(self):
        for D, P in self.PAIRS_PRINT_AT_LEAST_DECREASE + ((6, 4), (10, 6)):
            for behind in BEHIND:
                for own in (10.0, 4.0):
                    if D > 10 + behind:
                        continue
                    self.assertEqual(_pair("trade", behind, own, False, float(D), float(P))[2], _pair("trade", behind, own, True, float(D), float(P))[2], (D, P, behind, own))

    def test_both_arrival_orders_give_the_same_ahead_and_the_same_fills(self):
        for D, P in self.PAIRS_PRINT_AT_LEAST_DECREASE:
            for behind in BEHIND:
                for own in (10.0, 4.0):
                    if D > 10 + behind:
                        continue
                    b = _pair("book", behind, own, True, float(D), float(P))[2]
                    t = _pair("trade", behind, own, True, float(D), float(P))[2]
                    self.assertEqual(b, t, (D, P, behind, own))

    def test_head_drop_10_then_print_12_now_fills_2_not_10(self):
        for behind in BEHIND:
            self.assertEqual(_pair("book", behind, 10.0, True, 10.0, 12.0)[2][1], 2.0, behind)
        ex, o, _ = _pair("book", 0, 10.0, True, 10.0, 12.0)
        self.assertEqual(ex.diag["evidence_confirmed_fill_qty"], 2.0)                           # the confirming fill used only the print left after the head movement

    def test_book_first_with_10_behind_ends_at_6_not_8(self):
        self.assertEqual(_pair("book", 10, 10.0, True, 4.0, 4.0)[2], (6.0, 0.0))

    def test_a_decrease_larger_than_its_print_is_NOT_made_order_independent_residual_pinned(self):
        # print first, the unexplained rest of the decrease is shared over the level size the engine still shows (stale by the print); book first, the whole decrease was shared at once.
        # The switch does not touch this (it moves matched volume only); the gap is under 1.2 in these fixtures and is reported, not hidden.
        for D, P in ((6, 4), (10, 6)):
            for behind in BEHIND:
                if D > 10 + behind:
                    continue
                b = _pair("book", behind, 10.0, True, float(D), float(P))[2][0]
                t = _pair("trade", behind, 10.0, True, float(D), float(P))[2][0]
                self.assertLess(abs(b - t), 1.21, (D, P, behind))
        self.assertEqual((_pair("book", 0, 10.0, True, 6.0, 4.0)[2][0], _pair("trade", 0, 10.0, True, 6.0, 4.0)[2][0]), (4.0, 4.8))

    def test_a_credit_is_split_between_two_orders_resting_at_the_level_and_used_once(self):
        ex = make_executor(**{**V11, "depletion_ledger": True})
        book = make_book(bids=[(100.0, 20.0), (99.0, 100.0)])
        a = place(ex, book, price=100.0, size=2.0)
        delta(ex, book, "bid", 100.0, 40.0, 1.5)
        b = place(ex, book, price=100.0, size=2.0, now=1.6)
        delta(ex, book, "bid", 100.0, 36.0, 2.000)
        sell_print(ex, book, 100.0, 4.0, 2.010)
        self.assertGreater(a.queue_ahead, 0.0)
        self.assertGreater(b.queue_ahead, a.queue_ahead - 1e-9)
        before = (a.queue_ahead, b.queue_ahead)
        sell_print(ex, book, 100.0, 4.0, 2.020)                                               # no credit left
        self.assertLess(a.queue_ahead, before[0])

    def test_the_cohort_queue_ignores_the_movement_rule(self):
        for ledger in (False, True):
            ex = make_executor(**{**V11, "queue_model": "cohort", "cohort_age": "uniform", "depletion_ledger": ledger})
            book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
            o = place(ex, book, price=100.0, size=2.0)
            delta(ex, book, "bid", 100.0, 6.0, 2.000)
            sell_print(ex, book, 100.0, 4.0, 2.010)
            if ledger:
                self.assertEqual(o.queue_ahead, base)
            else:
                base = o.queue_ahead


class NettedWithoutProof(unittest.TestCase):
    """Guide s4 / owner item 4: a netted print or decrease with no other proof of the match (stamps more than 1 ms apart AND sizes unequal) is counted, separately from competing credits."""
    def _go(self, ledger, D, P, ts, behind=0):
        ex = make_executor(**{**V11, "depletion_ledger": ledger})
        ex.obs_enabled = True
        book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
        place(ex, book, price=100.0, size=10.0)
        level = 10.0
        if behind:
            delta(ex, book, "bid", 100.0, 10.0 + behind, 1.5)
            level += behind
        delta(ex, book, "bid", 100.0, level - D, ts[0])
        sell_print(ex, book, 100.0, P, ts[1])
        return ex

    def test_same_stamp_and_same_size_is_proof_enough(self):
        ex = self._go(True, 4.0, 4.0, (2.0, 2.0005))
        self.assertEqual(ex.ledger_stats["prints_netted_exact"], 1.0)
        self.assertNotIn("ledger_netted_prints_no_proof", ex.diag)

    def test_an_association_by_price_and_window_alone_is_flagged_and_counted(self):
        ex = self._go(True, 6.0, 4.0, (2.0, 2.030))
        self.assertEqual(ex.diag["ledger_netted_prints_no_proof"], 1.0)
        self.assertEqual(ex.diag["ledger_netted_print_volume_no_proof"], 4.0)
        self.assertEqual(ex.ledger_stats["prints_netted_no_proof"], 1.0)

    def test_a_netted_decrease_print_first_is_counted_the_same_way(self):
        ex = make_executor(**{**V11, "depletion_ledger": True})
        ex.obs_enabled = True
        book = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
        place(ex, book, price=100.0, size=10.0)
        sell_print(ex, book, 100.0, 4.0, 2.000)
        delta(ex, book, "bid", 100.0, 4.0, 2.030)                                              # a bigger decrease 30 ms later: shares the print's volume, nothing else says it is the same execution
        self.assertEqual(ex.diag["ledger_netted_decreases_no_proof"], 1.0)
        self.assertEqual(ex.ledger_stats["decreases_netted"], 1.0)

    def test_competing_credits_are_a_separate_count(self):
        ex = make_executor(**{**V11, "depletion_ledger": True})
        ex.obs_enabled = True
        book = make_book(bids=[(100.0, 30.0), (99.0, 100.0)])
        place(ex, book, price=100.0, size=10.0)
        delta(ex, book, "bid", 100.0, 26.0, 2.000)
        delta(ex, book, "bid", 100.0, 22.0, 2.010)
        sell_print(ex, book, 100.0, 5.0, 2.020)
        self.assertGreaterEqual(ex.diag["ledger_ambiguous"], 1)
        self.assertEqual(ex.diag["ledger_netted_prints_no_proof"], 1.0)

    def test_with_the_switch_off_no_diag_key_appears_and_the_book_first_share_is_still_measured(self):
        ex = self._go(False, 6.0, 4.0, (2.0, 2.030))
        self.assertFalse([k for k in ex.diag if k.startswith("ledger_netted")])
        self.assertEqual(ex.ledger_stats["print_volume_netted"], 4.0)

    def test_a_fill_that_rests_on_an_unproven_netting_carries_the_flag(self):
        ex = make_executor(**{**V11, "depletion_ledger": True, "fill_evidence": "off"})
        ex.obs_enabled = True
        book = make_book(bids=[(100.0, 4.0), (99.0, 100.0)])
        place(ex, book, price=100.0, size=10.0)
        delta(ex, book, "bid", 100.0, 0.0, 2.000)                                              # level vanishes: ahead 0
        fills = sell_print(ex, book, 100.0, 6.0, 2.030)
        self.assertTrue(fills)
        self.assertIn("netting_without_proof", fills[0].obs["flags"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
