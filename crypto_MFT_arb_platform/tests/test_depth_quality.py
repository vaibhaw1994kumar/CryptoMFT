"""Owner 14 Nov (integration session): the depth-quality tracker in the core VenueState (gap depth and flicker per side), OFF by default.
SYNTHETIC books and prints, tests only: nothing here is a profit figure. The flicker split is cross-checked against the depletion ledger (MakerExecutor, depletion_ledger=True) on the same sequence."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from qv2_support import delta, make_book, make_executor, place, sell_print

from crypto_stat_arb.simulation.order_book import OrderBook
from crypto_stat_arb.simulation.venue_state import VenueState, VenueStateConfig


def state(**kw):
    return VenueState("bybit", VenueStateConfig(depth_quality=True, **kw))


def book(bids, asks, ts=0.0):
    b = OrderBook("bybit", "SOLUSDT")
    b.apply_snapshot([list(x) for x in bids], [list(x) for x in asks], ts)
    return b


LADDER = [(101.00, 5.0), (101.01, 3.0), (101.02, 2.0), (103.00, 50.0)]       # a 196 bps hole after 10 base
DENSE = [(101.0 + 0.01 * i, 1.0) for i in range(30)]


class Off(unittest.TestCase):
    def test_off_by_default_nothing_is_tracked_and_nothing_is_returned(self):
        st = VenueState("bybit")
        b = book([(100.0, 5.0)], LADDER)
        st.on_book(b, 1.0)
        st.on_trade(1.0, 101.0, 1.0, 1)
        self.assertFalse(st._dq_on)
        self.assertFalse(hasattr(st, "_dq_prev"))
        self.assertIsNone(st.depth_quality(b, 1.0))
        self.assertIsNone(st.flicker("bid", 1.0))
        self.assertEqual(st.gap_depth(b, "ask", 1.0)["status"], "NOT_EVALUABLE")

    def test_new_backtest_shares_the_one_class(self):
        import new_backtest.venue_state as nb
        self.assertIs(nb.VenueState, VenueState)


class GapDepth(unittest.TestCase):
    def test_the_size_before_the_first_hole_and_where_it_starts(self):
        g = state().gap_depth(book([(100.0, 5.0)], LADDER), "ask", 1.0)
        self.assertEqual(g["status"], "GAP")
        self.assertAlmostEqual(g["depth"], 10.0)
        self.assertAlmostEqual(g["distance_bps"], 0.02 / 101.0 * 1e4, places=6)

    def test_a_dense_ladder_has_no_gap_and_the_book_edge_is_not_a_cliff(self):
        g = state().gap_depth(book([(100.0, 5.0)], DENSE), "ask", 1.0)
        self.assertEqual(g["status"], "NO_GAP_WITHIN_CAPTURE")
        self.assertIsNone(g["depth"])
        self.assertAlmostEqual(g["captured_depth"], 30.0)

    def test_one_level_is_not_evaluable(self):
        g = state().gap_depth(book([(100.0, 5.0)], [(101.0, 5.0)]), "ask", 1.0)
        self.assertEqual((g["status"], g["depth"]), ("NOT_EVALUABLE", None))

    def test_the_threshold_is_in_bps_so_the_venue_tick_does_not_matter(self):
        coarse = [(101.00, 5.0), (101.10, 5.0), (101.20, 5.0)]               # 10 bps steps on a 0.1 tick
        fine = [(101.000, 5.0), (101.001, 5.0), (101.002, 5.0), (101.003, 5.0)]
        self.assertEqual(state().gap_depth(book([(100.0, 1.0)], coarse), "ask", 1.0)["status"], "GAP")             # 10 bps > 3 bps
        self.assertEqual(state().gap_depth(book([(100.0, 1.0)], fine), "ask", 1.0)["status"], "NO_GAP_WITHIN_CAPTURE")
        self.assertEqual(state(dq_gap_stretch_bps=11.0).gap_depth(book([(100.0, 1.0)], coarse), "ask", 1.0)["status"], "NO_GAP_WITHIN_CAPTURE")

    def test_near_empty_levels_do_not_bridge_a_stretch(self):
        asks = [(101.00, 5.0), (101.01, 0.01), (101.02, 0.01), (101.05, 5.0)]
        self.assertEqual(state().gap_depth(book([(100.0, 1.0)], asks), "ask", 1.0)["status"], "NO_GAP_WITHIN_CAPTURE")      # 4.95 bps from 101.00 but 101.01 counts as a level
        g = state(dq_gap_level_size=0.5).gap_depth(book([(100.0, 1.0)], asks), "ask", 1.0)
        self.assertEqual(g["status"], "GAP")
        self.assertAlmostEqual(g["depth"], 5.02)

    def test_bid_side_walks_downward(self):
        bids = [(100.00, 4.0), (99.99, 4.0), (98.00, 40.0)]
        g = state().gap_depth(book(bids, [(101.0, 1.0), (101.01, 1.0)]), "bid", 1.0)
        self.assertEqual((g["status"], g["depth"]), ("GAP", 8.0))

    def test_a_gap_that_coincides_with_a_sequence_gap_is_a_feed_fault_not_a_cliff(self):
        st, b = state(), book([(100.0, 5.0)], LADDER)
        st.note_feed_gap(10.0)
        near = st.gap_depth(b, "ask", 11.0)
        self.assertEqual((near["status"], near["depth"]), ("FEED_FAULT", None))
        far = st.gap_depth(b, "ask", 13.0)                                    # past dq_feed_fault_window_s (2 s)
        self.assertEqual(far["status"], "GAP")

    def test_a_recovering_book_is_a_feed_fault(self):
        st, b = state(), book([(100.0, 5.0)], LADDER)
        b.state = "recovering"
        self.assertEqual(st.gap_depth(b, "ask", 1.0)["status"], "FEED_FAULT")

    def test_note_feed_gap_is_a_no_op_when_off(self):
        st = VenueState("bybit")
        st.note_feed_gap(1.0)
        self.assertFalse(hasattr(st, "_dq_last_feed_gap_ts"))


def _feed_flicker(st, b, steps):
    """steps: ("book", side, price, size, ts) or ("print", price, volume, ts) (a sell aggressor, hits bids)."""
    for s in steps:
        if s[0] == "book":
            _, side, price, size, ts = s
            b.apply_delta(side, price, size, "modify", ts)
            st.on_book(b, ts)
        else:
            _, price, vol, ts = s
            st.on_trade(ts, price, vol, -1)


class Flicker(unittest.TestCase):
    def setUp(self):
        self.st = state(dq_flicker_window_s=10.0)
        self.b = book([(100.0, 10.0), (99.0, 100.0)], [(101.0, 10.0), (102.0, 100.0)])
        self.st.on_book(self.b, 0.0)

    def test_none_before_one_window_of_data(self):
        _feed_flicker(self.st, self.b, [("book", "bid", 100.0, 4.0, 1.0)])
        self.assertIsNone(self.st.flicker("bid", 5.0))

    def test_a_decrease_no_print_explains_is_flicker_over_the_traded_floor(self):
        _feed_flicker(self.st, self.b, [("book", "bid", 100.0, 4.0, 1.0), ("print", 99.0, 2.0, 3.0)])        # 6 vanished, 2 printed elsewhere
        self.assertAlmostEqual(self.st.flicker("bid", 10.5), 6.0 / 2.0)
        self.assertEqual(self.st.flicker("ask", 10.5), 0.0)                                                   # nothing happened on the ask side

    def test_a_print_that_explains_the_decrease_leaves_zero_flicker(self):
        _feed_flicker(self.st, self.b, [("print", 100.0, 6.0, 1.000), ("book", "bid", 100.0, 4.0, 1.010)])    # trade-first
        _feed_flicker(self.st, self.b, [("book", "bid", 100.0, 1.0, 2.000), ("print", 100.0, 3.0, 2.010)])    # book-first
        self.assertAlmostEqual(self.st.flicker("bid", 10.5), 0.0)

    def test_a_print_outside_the_match_window_does_not_explain(self):
        _feed_flicker(self.st, self.b, [("book", "bid", 100.0, 4.0, 1.0), ("print", 100.0, 6.0, 1.5)])
        self.assertAlmostEqual(self.st.flicker("bid", 10.5), 6.0 / 6.0)

    def test_the_traded_volume_floor_keeps_a_pure_pull_defined(self):
        _feed_flicker(self.st, self.b, [("book", "bid", 100.0, 4.0, 1.0)])                                    # 6 pulled, nothing printed
        self.assertAlmostEqual(self.st.flicker("bid", 10.5), 6.0 / 1.0)                                       # floor = dq_flicker_min_volume 1.0

    def test_a_print_hitting_the_ask_side_key_does_not_explain_a_bid_decrease(self):
        st = state(dq_flicker_window_s=10.0)
        b = book([(100.0, 10.0), (99.0, 100.0)], [(101.0, 10.0), (102.0, 100.0)])
        st.on_book(b, 0.0)
        b.apply_delta("bid", 100.0, 4.0, "modify", 1.0)
        st.on_book(b, 1.0)
        st.on_trade(1.01, 100.0, 6.0, +1)                       # a BUY aggressor cannot deplete a bid
        self.assertAlmostEqual(st.flicker("bid", 10.5), 6.0 / 1.0)

    def test_only_the_top_n_levels_count(self):
        st = state(dq_top_levels=2, dq_flicker_window_s=10.0)
        b = book([(100.0, 10.0), (99.0, 10.0), (98.0, 100.0)], [(101.0, 10.0), (102.0, 10.0)])
        st.on_book(b, 0.0)
        b.apply_delta("bid", 98.0, 1.0, "modify", 1.0)          # the third level: outside the window
        st.on_book(b, 1.0)
        self.assertAlmostEqual(st.flicker("bid", 10.5), 0.0)

    def test_a_level_that_leaves_the_window_because_the_touch_moved_is_not_a_decrease(self):
        st = state(dq_top_levels=2, dq_flicker_window_s=10.0)
        b = book([(100.0, 10.0), (99.0, 10.0), (98.0, 10.0)], [(101.0, 10.0), (102.0, 10.0)])
        st.on_book(b, 0.0)
        b.apply_delta("bid", 100.0, 10.0, "modify", 1.0)
        b.apply_delta("bid", 100.5, 10.0, "modify", 1.0)        # a new better bid pushes 99 out of the top 2
        st.on_book(b, 1.0)
        self.assertAlmostEqual(st.flicker("bid", 10.5), 0.0)


class AgreesWithTheDepletionLedger(unittest.TestCase):
    """The tracker's explained volumes equal the ledger's on the same sequence at a resting level (the fill engine is not shared or changed: this is a cross-check)."""
    BASE = dict(cancel_model="proportional", fill_evidence="off", dedupe_trade_depth=True, share_trade_volume=True, exchange_time_eligibility=True,
                dedupe_window_s=0.05, dedupe_time_basis="exchange", depletion_ledger=True)

    def run_both(self, steps):
        ex = make_executor(**self.BASE)
        eb = make_book(bids=[(100.0, 10.0), (99.0, 100.0)])
        place(ex, eb, price=100.0, size=10.0)
        tb = book([(100.0, 10.0), (99.0, 100.0)], [(101.0, 50.0), (102.0, 100.0)])
        st = state(dq_match_window_s=0.05)
        st.on_book(tb, 0.0)
        for s in steps:
            if s[0] == "book":
                _, size, ts = s
                delta(ex, eb, "bid", 100.0, size, ts)
                tb.apply_delta("bid", 100.0, size, "modify", ts)
                st.on_book(tb, ts)
            else:
                _, vol, ts = s
                sell_print(ex, eb, 100.0, vol, ts)
                st.on_trade(ts, 100.0, vol, -1)
        return ex.ledger_stats, st.dq_stats

    def check(self, steps):
        led, dq = self.run_both(steps)
        for k in ("print_volume_netted", "decrease_volume_netted"):
            self.assertAlmostEqual(dq.get(k, 0.0), led.get(k, 0.0), places=9, msg=(k, led, dq))
        return led, dq

    def test_book_first(self):
        led, dq = self.check([("book", 6.0, 2.000), ("print", 4.0, 2.010)])
        self.assertAlmostEqual(dq["print_volume_netted"], 4.0)

    def test_trade_first(self):
        led, dq = self.check([("print", 4.0, 2.000), ("book", 6.0, 2.010)])
        self.assertAlmostEqual(dq["decrease_volume_netted"], 4.0)

    def test_print_larger_than_the_decrease(self):
        led, dq = self.check([("book", 0.0, 2.000), ("print", 12.0, 2.010)])
        self.assertAlmostEqual(dq["print_volume_netted"], 10.0)

    def test_outside_the_window_nothing_nets(self):
        led, dq = self.check([("book", 6.0, 2.000), ("print", 4.0, 2.200)])
        self.assertEqual((led.get("print_volume_netted", 0.0), dq.get("print_volume_netted", 0.0)), (0.0, 0.0))

    def test_two_decreases_one_print(self):
        self.check([("book", 8.0, 2.000), ("book", 5.0, 2.010), ("print", 4.0, 2.020)])


class TacticGap(unittest.TestCase):
    """choose_tactic with gap_aware: a market-style answer is capped when the size before the gap is smaller than the order; flicker discounts the executable depth; OFF by default, and an unknown field changes nothing."""

    @staticmethod
    def st(**kw):
        from crypto_stat_arb.ems.tactics import MarketState
        base = dict(book_age_s=0.1, spread_bps=2.0, volatility_bucket="normal", inventory_age_s=0.0, inventory_pressure=0.2, time_to_limit_s=1.0, breaker_flatten=False, order_qty=10.0)
        base.update(kw)
        return MarketState(**base)

    @staticmethod
    def pick(state, **cfg):
        from crypto_stat_arb.ems.tactics import TacticChoiceConfig, choose_tactic
        c = choose_tactic(state, TacticChoiceConfig(**cfg), allow_market_banded=True)
        return c.tactic, c.reason

    GAP = dict(depth_to_first_gap=4.0, gap_status="GAP", gap_distance_bps=2.0)

    def test_off_by_default_the_gap_fields_are_not_read(self):
        self.assertEqual(self.pick(self.st(**self.GAP)), ("MARKET_BANDED", "TIME_TO_LIMIT_FLATTEN"))
        with_fields = self.st(inventory_pressure=0.7, time_to_limit_s=None, executable_depth_within_cap=10.0, flicker=0.9, **self.GAP)
        without = self.st(inventory_pressure=0.7, time_to_limit_s=None, executable_depth_within_cap=10.0)
        self.assertEqual(self.pick(with_fields, liquidity_aware=True), self.pick(without, liquidity_aware=True))

    def test_a_gap_smaller_than_the_order_caps_the_emergency_market_order(self):
        self.assertEqual(self.pick(self.st(**self.GAP), gap_aware=True), ("IOC_CAPPED", "GAP_NO_MARKET_HEDGE"))

    def test_a_gap_that_holds_the_whole_order_changes_nothing(self):
        self.assertEqual(self.pick(self.st(depth_to_first_gap=10.0, gap_status="GAP"), gap_aware=True), ("MARKET_BANDED", "TIME_TO_LIMIT_FLATTEN"))

    def test_unknown_statuses_never_block(self):
        for status, depth in (("FEED_FAULT", None), ("NO_GAP_WITHIN_CAPTURE", None), ("NOT_EVALUABLE", None), (None, None), ("FEED_FAULT", 1.0)):
            self.assertEqual(self.pick(self.st(gap_status=status, depth_to_first_gap=depth), gap_aware=True), ("MARKET_BANDED", "TIME_TO_LIMIT_FLATTEN"), (status, depth))
        self.assertEqual(self.pick(self.st(gap_status="GAP", depth_to_first_gap=4.0, order_qty=None), gap_aware=True), ("MARKET_BANDED", "TIME_TO_LIMIT_FLATTEN"))

    def test_the_breaker_flatten_is_exempt_unless_the_owner_says_otherwise(self):
        brk = self.st(breaker_flatten=True, **self.GAP)
        self.assertEqual(self.pick(brk, gap_aware=True), ("MARKET_BANDED", "BREAKER_FLATTEN"))
        self.assertEqual(self.pick(brk, gap_aware=True, gap_exempt_breaker=False), ("IOC_CAPPED", "GAP_NO_MARKET_HEDGE"))

    def test_the_gap_rule_never_makes_a_market_order_where_there_was_none(self):
        self.assertEqual(self.pick(self.st(time_to_limit_s=None, inventory_pressure=0.0, **self.GAP), gap_aware=True), ("PASSIVE", "DEFAULT_PASSIVE"))

    def test_flicker_discounts_the_executable_depth(self):
        base = dict(inventory_pressure=0.7, time_to_limit_s=None, order_qty=10.0, executable_depth_within_cap=12.0)
        from crypto_stat_arb.ems.tactics import TacticChoiceConfig, choose_tactic
        cfg = TacticChoiceConfig(liquidity_aware=True, gap_aware=True, flicker_weight=1.0)
        self.assertEqual(choose_tactic(self.st(**base), cfg).reason, "LQ_DEPTH_COVERS")
        c = choose_tactic(self.st(flicker=0.5, **base), cfg)                           # 12 x (1 - 0.5) = 6 of 10
        self.assertEqual((c.tactic, c.reason), ("IOC_CAPPED", "LQ_DEPTH_LIMITED"))
        self.assertAlmostEqual(c.covered_qty, 6.0)
        self.assertAlmostEqual(c.residual_qty, 4.0)
        full = choose_tactic(self.st(flicker=5.0, **base), cfg)                        # the discount never goes below nothing
        self.assertEqual((full.tactic, full.reason, full.covered_qty), ("PASSIVE", "LQ_DEPTH_LIMITED", 0.0))

    def test_the_covered_quantity_never_exceeds_the_size_before_the_gap(self):
        from crypto_stat_arb.ems.tactics import TacticChoiceConfig, choose_tactic
        c = choose_tactic(self.st(inventory_pressure=0.7, time_to_limit_s=None, executable_depth_within_cap=50.0, **self.GAP), TacticChoiceConfig(liquidity_aware=True, gap_aware=True))
        self.assertEqual((c.tactic, c.reason), ("IOC_CAPPED", "LQ_DEPTH_LIMITED"))
        self.assertAlmostEqual(c.covered_qty, 4.0)

    def test_with_gap_aware_on_and_every_new_field_none_the_answer_is_the_old_answer(self):
        import itertools
        from crypto_stat_arb.ems.tactics import TacticChoiceConfig, choose_tactic
        for age, spread, pressure, ttl, brk, qty, depth in itertools.product((0.1, 0.6), (2.0, 20.0, 40.0), (0.0, 0.3, 0.7, 1.2), (None, 5.0), (False, True), (None, 10.0), (None, 4.0, 12.0)):
            s = self.st(book_age_s=age, spread_bps=spread, inventory_pressure=pressure, time_to_limit_s=ttl, breaker_flatten=brk, order_qty=qty, executable_depth_within_cap=depth)
            for mm in (True, False):
                self.assertEqual(choose_tactic(s, TacticChoiceConfig(liquidity_aware=True, gap_aware=True), allow_market_banded=mm),
                                 choose_tactic(s, TacticChoiceConfig(liquidity_aware=True), allow_market_banded=mm), (s, mm))

    def test_gap_aware_never_adds_a_market_banded_outside_the_emergency_reasons(self):
        import itertools
        from crypto_stat_arb.ems.tactics import MARKET_BANDED_REASONS, TacticChoiceConfig, choose_tactic
        for pressure, ttl, brk, status, depth, flick in itertools.product((0.0, 0.7, 1.2), (None, 5.0), (False, True), ("GAP", "FEED_FAULT", None), (None, 2.0, 50.0), (None, 0.0, 0.9)):
            s = self.st(inventory_pressure=pressure, time_to_limit_s=ttl, breaker_flatten=brk, gap_status=status, depth_to_first_gap=depth, flicker=flick, executable_depth_within_cap=20.0)
            c = choose_tactic(s, TacticChoiceConfig(liquidity_aware=True, gap_aware=True), allow_market_banded=True)
            if c.tactic == "MARKET_BANDED":
                self.assertIn(c.reason, MARKET_BANDED_REASONS, s)

    def test_bad_values_are_refused(self):
        from crypto_stat_arb.ems.tactics import TacticChoiceConfig, choose_tactic
        for kw in (dict(flicker=-0.1), dict(depth_to_first_gap=float("nan")), dict(gap_status="CLIFF"), dict(gap_distance_bps=True)):
            with self.assertRaises(ValueError, msg=kw):
                choose_tactic(self.st(**kw))
        with self.assertRaises(ValueError):
            choose_tactic(self.st(), TacticChoiceConfig(gap_aware="yes"))
        with self.assertRaises(ValueError):
            choose_tactic(self.st(), TacticChoiceConfig(gap_aware=True, flicker_weight=-1.0))


class JournalEvidence(unittest.TestCase):
    """Routing evidence (item 3): with a provider the layer journals gap depth and flicker with every intent and child; without one the records have no such key (every existing journal is unchanged)."""

    @staticmethod
    def layer():
        from new_backtest.execution import ExecutionConfig, ExecutionLayer
        return ExecutionLayer(ExecutionConfig(), instrument="SOLUSDT")

    @staticmethod
    def open(xl, side="buy"):
        from new_backtest.execution import Role
        return xl.open_intent(role=Role.PRIMARY, side=side, qty=2.0, venues=("BYBIT",), worst_price=100.0, now=1.0, signal_id=7, reason="maker leg")

    def test_no_provider_no_key(self):
        xl = self.layer()
        it = self.open(xl)
        xl.child_sent(it, 11, 11, "PASSIVE", 1.0)
        self.assertTrue(all("depth_quality" not in j for j in xl.journal))

    def test_a_provider_adds_the_evidence_for_the_side_the_order_consumes(self):
        xl, calls = self.layer(), []

        def provider(venue, side, now):
            calls.append((venue, side, now))
            return {"gap": {side: {"status": "GAP", "depth": 4.0}}, "flicker": {side: 0.2}}
        xl.set_evidence_provider(provider)
        it = self.open(xl, "buy")
        xl.child_sent(it, 11, 11, "PASSIVE", 1.5)
        rows = {j["event"]: j for j in xl.journal if j["event"] in ("intent", "child")}
        self.assertEqual(rows["intent"]["depth_quality"]["BYBIT"]["gap"]["ask"]["depth"], 4.0)          # a buy consumes the asks
        self.assertEqual(rows["child"]["depth_quality"]["BYBIT"]["flicker"]["ask"], 0.2)
        self.assertEqual(calls, [("BYBIT", "ask", 1.0), ("BYBIT", "ask", 1.5)])
        sell = self.open(xl, "sell")
        self.assertEqual(calls[-1][1], "bid")
        self.assertIsNotNone(sell)

    def test_a_provider_with_nothing_to_say_adds_nothing(self):
        xl = self.layer()
        xl.set_evidence_provider(lambda *a: None)
        self.open(xl)
        self.assertTrue(all("depth_quality" not in j for j in xl.journal))


from test_session_replay import SLICES, stream            # noqa: E402  (the synthetic replay stream the other session tests use)


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class ReplayEvidence(unittest.TestCase):
    """With VenueStateConfig.depth_quality the replay journals the evidence and the run itself is unchanged (observation only)."""

    def run_replay(self, on):
        from new_backtest.profiles import apply_profile, load_profile
        from new_backtest.session_replay import SessionReplay
        profile = load_profile("perp_perp_maker_taker_v1_9")
        cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_dq")
        cfg.arb.max_position_base = 1.0
        cfg.arb.participation_max = 0.0
        cfg.venue_state.depth_quality = on
        replay = SessionReplay(cfg, profile, stream=stream())
        result = replay.run()
        return replay, result

    def test_the_run_is_identical_and_the_journal_carries_the_evidence_only_when_on(self):
        off_r, off = self.run_replay(False)
        on_r, on = self.run_replay(True)
        for key in ("fills", "orders", "signals", "positions", "cash_by_venue", "ledger_event_counts"):
            self.assertEqual(off[key], on[key], key)
        off_j, on_j = off_r.session.intent_observer.layer.journal, on_r.session.intent_observer.layer.journal
        self.assertTrue(all("depth_quality" not in j for j in off_j))
        self.assertEqual([{k: v for k, v in j.items() if k != "depth_quality"} for j in on_j], off_j)
        tagged = [j for j in on_j if "depth_quality" in j]
        self.assertTrue(tagged)
        self.assertTrue(all(j["event"] in ("intent", "child") for j in tagged))
        ev = next(iter(tagged[0]["depth_quality"].values()))
        self.assertIn("APPROXIMATE", ev["approx"])
        self.assertEqual(len(ev["gap"]), 1)                       # one side: the one the order consumes


class FeedGapHook(unittest.TestCase):
    """The session tells the owner of the VenueStates when a venue's core book counts a sequence gap (once per gap), and does nothing when no hook is set."""

    def make(self):
        from crypto_stat_arb.config import load_config
        from crypto_stat_arb.session import ExecutionMode, TradingSession
        cfg, _ = load_config(Path(__file__).resolve().parents[1] / "config/perp_research.json")
        return cfg, TradingSession(cfg, execution_mode=ExecutionMode.INTERNAL_SIMULATION, strategies=[])

    @staticmethod
    def ev(cfg, venue, seq, kind):
        from crypto_stat_arb.models import MarketEvent
        return MarketEvent.from_dict({"event_type": kind, "venue": venue, "instrument_id": cfg.instrument_id, "exchange_ts_ns": seq * 1_000_000_000, "receive_ts_ns": seq * 1_000_000_000,
                                      "sequence": seq, "bids": [["100.0", "2"]], "asks": [["101.0", "2"]]})

    def test_the_hook_fires_once_per_sequence_gap_with_the_time_in_seconds(self):
        cfg, session = self.make()
        venue = cfg.venues[0]
        calls = []
        session.feed_gap_hook = lambda v, now_s: calls.append((v, now_s))
        session.on_event(self.ev(cfg, venue, 10, "BOOK_SNAPSHOT"))
        session.on_event(self.ev(cfg, venue, 11, "BOOK_DELTA"))
        self.assertEqual(calls, [])
        session.on_event(self.ev(cfg, venue, 15, "BOOK_DELTA"))                       # 12..14 never arrived
        self.assertEqual(calls, [(venue, 15.0)])
        session.on_event(self.ev(cfg, venue, 16, "BOOK_DELTA"))                       # the book is waiting for a snapshot: no second count
        self.assertEqual(len(calls), 1)

    def test_with_no_hook_nothing_is_called_or_stored(self):
        cfg, session = self.make()
        venue = cfg.venues[0]
        session.on_event(self.ev(cfg, venue, 10, "BOOK_SNAPSHOT"))
        session.on_event(self.ev(cfg, venue, 15, "BOOK_DELTA"))
        self.assertIsNone(session.feed_gap_hook)
        self.assertEqual(session._seen_gaps, {})


if __name__ == "__main__":
    unittest.main()
