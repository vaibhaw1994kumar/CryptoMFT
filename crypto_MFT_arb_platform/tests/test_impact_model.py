"""Owner 14 Nov, execution cost model step C: the impact interface (levels 1 to 3). Hand-calculated values; SYNTHETIC numbers, tests only, nothing here is a profit figure."""
import math
import sys
from decimal import Decimal
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb.simulation.impact import CurveImpact, DisplayedDepthImpact, ImpactConfig, ImpactOverlay

TICKS = {"bybit": 0.01, "binance": 0.01}


class Level1(unittest.TestCase):
    LEVELS = [(100.0, 5.0), (100.1, 5.0), (100.3, 10.0)]

    def test_vwap_cost_on_displayed_depth_is_exact(self):
        m = DisplayedDepthImpact()
        self.assertAlmostEqual(m.vwap(self.LEVELS, 8.0), (5 * 100.0 + 3 * 100.1) / 8)                     # 100.0375
        self.assertAlmostEqual(m.vwap_cost_bps(self.LEVELS, 8.0), 3.75)

    def test_survival_scales_every_level_and_can_make_the_size_unfillable(self):
        m = DisplayedDepthImpact()
        self.assertAlmostEqual(m.vwap(self.LEVELS, 8.0, survival=0.5), (2.5 * 100.0 + 2.5 * 100.1 + 3.0 * 100.3) / 8)
        self.assertAlmostEqual(m.vwap_cost_bps(self.LEVELS, 8.0, survival=0.5), 14.375)
        self.assertIsNone(m.vwap(self.LEVELS, 11.0, survival=0.5))                                         # 10 effective < 11
        with self.assertRaises(ValueError):
            m.vwap(self.LEVELS, 1.0, survival=1.5)
        with self.assertRaises(ValueError):
            m.vwap(self.LEVELS, 0.0)

    def test_effective_depth_within_the_cap_for_a_buy_and_a_sell(self):
        m = DisplayedDepthImpact()
        self.assertAlmostEqual(m.effective_depth_within_cap(self.LEVELS, 100.1, True, survival=0.5), 5.0)
        self.assertAlmostEqual(m.effective_depth_within_cap(self.LEVELS, 100.1, True), 10.0)
        bids = [(100.0, 4.0), (99.9, 4.0), (99.0, 8.0)]
        self.assertAlmostEqual(m.effective_depth_within_cap(bids, 99.9, False, survival=0.75), 6.0)
        self.assertAlmostEqual(m.effective_depth_within_cap(bids, 101.0, False), 0.0)

    def test_with_survival_one_the_effective_depth_is_the_displayed_depth(self):
        m = DisplayedDepthImpact()
        self.assertEqual(m.effective_levels(self.LEVELS, 1.0), self.LEVELS)

    def test_the_effective_depth_feeds_choose_tactic(self):
        from crypto_stat_arb.ems.tactics import MarketState, TacticChoiceConfig, choose_tactic
        m = DisplayedDepthImpact()
        st = lambda surv: MarketState(book_age_s=0.1, spread_bps=2.0, volatility_bucket="normal", inventory_age_s=0.0, inventory_pressure=0.7, order_qty=10.0,
                                      executable_depth_within_cap=m.effective_depth_within_cap(self.LEVELS, 100.3, True, survival=surv))
        cfg = TacticChoiceConfig(liquidity_aware=True)
        self.assertEqual(choose_tactic(st(1.0), cfg).reason, "LQ_DEPTH_COVERS")                           # 20 effective >= 10
        self.assertEqual(choose_tactic(st(0.4), cfg).reason, "LQ_PARTIAL_RESIDUAL")                       # 8 effective < 10


class Level2(unittest.TestCase):
    def test_form_a(self):
        m = CurveImpact(ImpactConfig(level=2, form="depth", a_bps=0.5, b_bps=2.0, delta=0.5))
        self.assertAlmostEqual(m.cost_bps(25.0, depth_k=100.0), 0.5 + 2.0 * math.sqrt(0.25))             # 1.5
        self.assertIsNone(m.cost_bps(25.0))                                                                # no depth: no answer
        self.assertIsNone(m.cost_bps(25.0, depth_k=0.0))

    def test_form_b(self):
        m = CurveImpact(ImpactConfig(level=2, form="volume", y=0.8, sigma_daily_bps=200.0, daily_volume=1_000_000.0, delta_volume=0.5))
        self.assertAlmostEqual(m.cost_bps(10_000.0), 0.8 * 200.0 * 0.1)                                    # 16
        self.assertAlmostEqual(m.cost_bps(10_000.0, daily_volume=4_000_000.0), 0.8 * 200.0 * 0.05)
        self.assertIsNone(CurveImpact(ImpactConfig(level=2, form="volume")).cost_bps(1.0))

    def test_inversion_returns_the_size_that_gives_the_peak(self):
        m = CurveImpact(ImpactConfig(level=2, form="depth", a_bps=0.5, b_bps=2.0, delta=0.5))
        self.assertAlmostEqual(m.size_for_peak_bps(1.5, depth_k=100.0), 25.0)
        self.assertIsNone(m.size_for_peak_bps(0.4, depth_k=100.0))                                         # below the intercept
        b = CurveImpact(ImpactConfig(level=2, form="volume", y=0.8, sigma_daily_bps=200.0, daily_volume=1e6, delta_volume=0.5))
        self.assertAlmostEqual(b.size_for_peak_bps(16.0), 10_000.0)


class Overlay(unittest.TestCase):
    def overlay(self, **kw):
        cfg = ImpactConfig(level=3, permanent_fraction=0.25, kernel="exp", tau_s=10.0, **kw)
        return ImpactOverlay(cfg, TICKS)

    def test_a_single_order_follows_the_permanent_plus_decay_curve_exactly(self):
        o = self.overlay()
        self.assertEqual(o.add_order("bybit", 100.0, "buy", 5.0, peak_bps=4.0), 4.0)
        self.assertEqual(o.own_shift_bps("bybit", 100.0), 0.0)                                             # the order itself fills at the price it found
        self.assertEqual(o.own_shift_bps("bybit", 99.0), 0.0)
        for age in (1.0, 10.0, 30.0, 200.0):
            self.assertAlmostEqual(o.own_shift_bps("bybit", 100.0 + age), 4.0 * (0.25 + 0.75 * math.exp(-age / 10.0)), places=12)
        self.assertAlmostEqual(o.own_shift_bps("bybit", 100.0 + 10.0), 2.103638323514327, places=9)       # 4 x (0.25 + 0.75 / e)
        self.assertAlmostEqual(o.own_shift_bps("bybit", 10_000.0), 1.0, places=9)                          # phi x peak stays for good

    def test_a_sell_moves_prices_down_and_the_power_kernel_matches_its_formula(self):
        o = ImpactOverlay(ImpactConfig(level=3, permanent_fraction=0.0, kernel="power", tau_s=10.0, beta=0.5), TICKS)
        o.add_order("bybit", 0.0, "sell", 5.0, peak_bps=6.0)
        self.assertAlmostEqual(o.own_shift_bps("bybit", 30.0), -6.0 * 4.0 ** -0.5)                          # (1 + 30/10) ^ -0.5 = 0.5
        self.assertAlmostEqual(o.own_shift_bps("bybit", 0.0 + 1e-9), -6.0, places=6)

    def test_two_orders_add_and_each_decays_from_its_own_time(self):
        o = self.overlay()
        o.add_order("bybit", 100.0, "buy", 5.0, peak_bps=4.0)
        o.add_order("bybit", 105.0, "sell", 5.0, peak_bps=2.0)
        t = 110.0
        want = 4.0 * (0.25 + 0.75 * math.exp(-1.0)) - 2.0 * (0.25 + 0.75 * math.exp(-0.5))
        self.assertAlmostEqual(o.own_shift_bps("bybit", t), want, places=12)
        self.assertAlmostEqual(o.own_shift_bps("bybit", 102.0), 4.0 * (0.25 + 0.75 * math.exp(-0.2)), places=12)       # the second order is still in the future
        self.assertAlmostEqual(o.own_shift_bps("bybit", 100_000.0), 4.0 * 0.25 - 2.0 * 0.25, places=9)

    def test_the_fast_path_and_the_exact_path_agree_and_queries_in_any_order_are_right(self):
        o = self.overlay()
        times = [(100.0, "buy", 4.0), (101.0, "buy", 3.0), (160.0, "sell", 5.0), (400.0, "buy", 1.0)]
        for ts, side, peak in times:
            o.add_order("bybit", ts, side, 1.0, peak_bps=peak)
        def exact(t):
            return sum((1 if s == "buy" else -1) * p * (0.25 + 0.75 * math.exp(-(t - ts) / 10.0)) for ts, s, p in times if ts < t)
        for t in (450.0, 500.0, 120.0, 99.0, 700.0, 450.0, 2000.0):
            self.assertAlmostEqual(o.own_shift_bps("bybit", t), exact(t), places=9, msg=t)

    def test_an_order_on_one_venue_shifts_the_other_by_the_cross_coefficient_and_nothing_else(self):
        o = self.overlay(cross_bybit_to_binance=0.4, cross_binance_to_bybit=0.1)
        o.add_order("bybit", 100.0, "buy", 5.0, peak_bps=4.0)
        own = 4.0 * (0.25 + 0.75 * math.exp(-1.0))
        self.assertAlmostEqual(o.shift_bps("bybit", 110.0), own, places=12)
        self.assertAlmostEqual(o.shift_bps("binance", 110.0), 0.4 * own, places=12)
        self.assertEqual(o.own_shift_bps("binance", 110.0), 0.0)                                           # the other venue has no order of its own
        o.add_order("binance", 105.0, "sell", 5.0, peak_bps=2.0)
        own_b = -2.0 * (0.25 + 0.75 * math.exp(-0.5))
        self.assertAlmostEqual(o.shift_bps("bybit", 110.0), own + 0.1 * own_b, places=12)
        self.assertAlmostEqual(o.shift_bps("binance", 110.0), own_b + 0.4 * own, places=12)

    def test_zero_cross_coefficient_moves_nothing_on_the_other_venue(self):
        o = self.overlay()
        o.add_order("bybit", 100.0, "buy", 5.0, peak_bps=4.0)
        self.assertEqual(o.shift_bps("binance", 150.0), 0.0)

    def test_ticks_are_the_nearest_whole_tick_of_the_venues_own_grid(self):
        o = self.overlay()
        o.add_order("bybit", 0.0, "buy", 1.0, peak_bps=4.0 / 0.25)                                         # permanent part 4 bps after the transient is gone
        self.assertEqual(o.shift_ticks("bybit", 1e6, 100.0), 4)                                            # 4 bps of 100 = 0.04 = 4 ticks of 0.01
        o2 = self.overlay()
        o2.add_order("bybit", 0.0, "buy", 1.0, peak_bps=1.4 / 0.25)
        o3 = self.overlay()
        o3.add_order("bybit", 0.0, "buy", 1.0, peak_bps=0.4 / 0.25)
        self.assertEqual((o2.shift_ticks("bybit", 1e6, 100.0), o3.shift_ticks("bybit", 1e6, 100.0)), (1, 0))      # under half a tick is no shift at all

    def test_the_peak_comes_from_the_model_and_an_order_it_cannot_price_moves_nothing(self):
        cfg = ImpactConfig(level=3, form="depth", a_bps=0.5, b_bps=2.0, delta=0.5, permanent_fraction=0.25, tau_s=10.0)
        o = ImpactOverlay(cfg, TICKS)
        self.assertAlmostEqual(o.add_order("bybit", 100.0, "buy", 25.0, depth_k=100.0), 1.5)
        self.assertIsNone(o.add_order("bybit", 101.0, "buy", 25.0))                                         # no depth_k: no peak
        self.assertEqual(o.counts, {"orders": 2, "orders_without_peak": 1})
        self.assertEqual(len(o.orders), 1)
        with self.assertRaises(ValueError):
            o.add_order("kraken", 1.0, "buy", 1.0, peak_bps=1.0)

    def test_the_config_is_off_by_default_and_refuses_bad_values(self):
        self.assertEqual(ImpactConfig().level, 0)
        for kw in (dict(level=4), dict(kernel="gauss"), dict(form="x"), dict(permanent_fraction=1.2), dict(tau_s=0.0), dict(delta=-1.0), dict(b_bps=float("nan"))):
            with self.assertRaises(ValueError, msg=kw):
                ImpactConfig(**kw)


class TakerWalkShift(unittest.TestCase):
    """The taker walk reads a COPY of the core book moved by the whole ticks the overlay has applied to its venue; the real book is never touched; no shift = the same object."""

    @staticmethod
    def adapter(ticks):
        from crypto_stat_arb.simulation.queue_adapter import QueueAwareVenueAdapter
        a = QueueAwareVenueAdapter.__new__(QueueAwareVenueAdapter)
        a.impact = ImpactOverlay(ImpactConfig(level=3), TICKS)
        a.impact.applied_ticks["binance"] = ticks
        a.tick_size, a.feed_venue, a._arrival_ns = 0.01, "binance", 1_000
        return a

    @staticmethod
    def core_book():
        from decimal import Decimal
        from crypto_stat_arb.models import EventType, Level, MarketEvent, stable_id
        from crypto_stat_arb.orderbook import OrderBook
        b = OrderBook("BINANCE", "SOL-USDT-PERP")
        b.apply(MarketEvent(event_id=stable_id("t", "b"), event_type=EventType.BOOK_SNAPSHOT, venue="BINANCE", instrument_id="SOL-USDT-PERP", exchange_ts_ns=1, receive_ts_ns=1, sequence=1, source_ts_ns=1,
                            sequence_start=1, raw_ref="t", adapter_version="t", bids=(Level(Decimal("100.00"), Decimal("5")), Level(Decimal("99.99"), Decimal("7"))), asks=(Level(Decimal("100.01"), Decimal("4")),)))
        return b

    def test_the_copy_is_moved_by_whole_ticks_and_the_original_is_not(self):
        from decimal import Decimal
        book = self.core_book()
        moved = self.adapter(-3)._impact_shifted(book)
        self.assertEqual(sorted(moved.bids.items(), reverse=True), [(Decimal("99.97"), Decimal("5")), (Decimal("99.96"), Decimal("7"))])
        self.assertEqual(list(moved.asks.items()), [(Decimal("99.98"), Decimal("4"))])
        self.assertEqual(sorted(book.bids.items(), reverse=True), [(Decimal("100.00"), Decimal("5")), (Decimal("99.99"), Decimal("7"))])
        self.assertEqual(list(book.asks.items()), [(Decimal("100.01"), Decimal("4"))])

    def test_no_shift_returns_the_same_book_object(self):
        book = self.core_book()
        self.assertIs(self.adapter(0)._impact_shifted(book), book)


sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_session_replay import SLICES, stream            # noqa: E402  (the synthetic replay stream the session tests use)


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class ReplayOverlay(unittest.TestCase):
    """Level 3 in the session replay: off = identical, on = the feed of the venue (and, by the coefficient, the other one) is shifted after our taker fill; the core books are not."""

    def run_replay(self, impact):
        from new_backtest.profiles import apply_profile, load_profile
        from new_backtest.session_replay import SessionReplay
        profile = load_profile("perp_perp_maker_taker_v1_9")
        cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_impact")
        cfg.arb.max_position_base = 1.0
        cfg.arb.participation_max = 0.0
        cfg.impact = impact
        replay = SessionReplay(cfg, profile, stream=stream())
        return replay, replay.run()

    BIG = dict(level=3, form="volume", y=1.0, sigma_daily_bps=1000.0, daily_volume=10_000.0, delta_volume=0.5, permanent_fraction=1.0, kernel="exp", tau_s=10.0)

    def test_off_is_identical_whether_the_key_is_absent_or_level_zero(self):
        _, a = self.run_replay(None)
        _, b = self.run_replay(ImpactConfig(level=0, y=5.0))
        for key in ("fills", "orders", "signals", "positions", "cash_by_venue", "ledger_event_counts", "equity_at_book_mids"):
            self.assertEqual(a[key], b[key], key)

    def test_a_taker_fill_is_recorded_and_the_later_prices_of_its_venue_move_by_whole_ticks(self):
        off, _ = self.run_replay(None)
        on, res = self.run_replay(ImpactConfig(cross_bybit_to_binance=0.0, cross_binance_to_bybit=0.0, **self.BIG))
        self.assertIsNone(off.impact)
        self.assertGreaterEqual(len(on.impact.orders), 1)
        o = on.impact.orders[0]
        self.assertEqual((o.venue, o.sign), ("binance", -1))                                              # the hedge sold on the hedge venue
        k = on.impact.applied_ticks["binance"]
        self.assertEqual(k, -100)                                                                          # 10 bps of ~100.3 = 0.1003 = 100 ticks of the replay's own 0.001 grid (March dated ticks), once
        core = on.session.books[("BINANCE", on.instrument_id)]
        view = on.views["binance"]
        self.assertAlmostEqual(float(core.best_bid) + k * on.ticks["binance"], view.best_bid, places=6)                  # the view carries the shift; the core book does not
        self.assertEqual(on.impact.applied_ticks["bybit"], 0)                                              # no cross coefficient: the other venue is untouched
        self.assertAlmostEqual(float(on.session.books[("BYBIT", on.instrument_id)].best_bid), on.views["bybit"].best_bid, places=6)

    def test_the_cross_coefficient_moves_the_other_venue(self):
        on, _ = self.run_replay(ImpactConfig(cross_binance_to_bybit=0.5, **self.BIG))
        kb, ky = on.impact.applied_ticks["binance"], on.impact.applied_ticks["bybit"]
        self.assertLess(kb, 0)
        self.assertLess(ky, 0)
        self.assertLessEqual(abs(ky), abs(kb))
        self.assertAlmostEqual(ky, round(0.5 * kb), delta=1)


from crypto_stat_arb.simulation.impact import ImpactConfig   # noqa: E402




class FillRangeRule(unittest.TestCase):
    """Owner 15 Nov, CR-LQ fill range: hand-calculated; off = today's answers."""
    LEVELS = [(100.0, 5.0), (100.1, 5.0), (100.3, 10.0)]

    def st(self, **kw):
        from crypto_stat_arb.ems.tactics import MarketState
        base = dict(book_age_s=0.1, spread_bps=2.0, volatility_bucket="normal", inventory_age_s=0.0, inventory_pressure=0.7, order_qty=10.0, executable_depth_within_cap=20.0)
        base.update(kw)
        return MarketState(**base)

    def cfg(self, **kw):
        from crypto_stat_arb.ems.tactics import TacticChoiceConfig
        return TacticChoiceConfig(liquidity_aware=True, fill_range=True, **kw)

    def test_totals_at_the_three_cases(self):
        from crypto_stat_arb.simulation.impact import fill_range_totals
        r = fill_range_totals(14.0, 0.9)                                          # 14 x 0.72, 14 x 0.9, 14 x min(1, 1.08)
        self.assertAlmostEqual(r.low, 10.08)
        self.assertAlmostEqual(r.expected, 12.6)
        self.assertAlmostEqual(r.high, 14.0)
        self.assertAlmostEqual(r.expected_fill(10.0), 10.0)
        self.assertAlmostEqual(r.expected_fill(20.0), 12.6)
        r2 = fill_range_totals(14.0, 0.9, 0.5)
        self.assertAlmostEqual(r2.low, 9.58)
        self.assertAlmostEqual(fill_range_totals(1.0, 1.0, 5.0).low, 0.0)           # never below zero
        with self.assertRaises(ValueError):
            fill_range_totals(14.0, 1.2)
        with self.assertRaises(ValueError):
            fill_range_totals(14.0, 0.9, low=1.5)

    def test_own_take_refills_as_one_minus_exp(self):
        from crypto_stat_arb.simulation.impact import own_unrefilled
        self.assertAlmostEqual(own_unrefilled([(0.0, 4.0)], 0.0, 3.5), 4.0)         # just taken: all missing
        self.assertAlmostEqual(own_unrefilled([(0.0, 4.0)], 3.5, 3.5), 4.0 * math.exp(-1.0))
        self.assertAlmostEqual(own_unrefilled([(0.0, 4.0), (1.0, 2.0)], 2.0, 2.0), 4.0 * math.exp(-1.0) + 2.0 * math.exp(-0.5))
        self.assertAlmostEqual(own_unrefilled([(5.0, 4.0)], 2.0, 3.5), 0.0)         # a take in the future is ignored
        self.assertAlmostEqual(own_unrefilled([(0.0, 4.0)], 100.0, None), 4.0)      # no tau: never refills

    def test_method_on_levels_matches_the_totals(self):
        m = DisplayedDepthImpact()
        r = m.fill_range(self.LEVELS, 100.1, True, 0.9, own_takes=[(0.0, 1.0)], now=0.0, tau_s=3.5)       # 10 displayed in the cap, 1.0 missing
        self.assertAlmostEqual(r.low, 10 * 0.72 - 1.0)
        self.assertAlmostEqual(r.expected, 9.0 - 1.0)
        self.assertAlmostEqual(r.high, 10.0 - 1.0)

    def test_a_sweep_only_if_the_pessimistic_case_covers(self):
        from crypto_stat_arb.ems.tactics import choose_tactic
        c = choose_tactic(self.st(displayed_depth_in_cap=14.0, survival_mean=1.0), self.cfg())          # 14 x 0.8 = 11.2 >= 10
        self.assertEqual((c.tactic, c.reason), ("IOC_CAPPED", "LQ_DEPTH_COVERS"))
        self.assertAlmostEqual(c.fill_range[0], 11.2)
        c = choose_tactic(self.st(displayed_depth_in_cap=14.0, survival_mean=0.9, own_unrefilled_qty=0.5), self.cfg())
        self.assertEqual((c.tactic, c.reason), ("IOC_CAPPED", "LQ_PARTIAL_RESIDUAL"))                    # 14 x 0.72 - 0.5 = 9.58 < 10
        self.assertAlmostEqual(c.covered_qty, 9.58)
        self.assertAlmostEqual(c.residual_qty, 0.42)
        self.assertAlmostEqual(c.fill_range[1], 12.1)
        self.assertAlmostEqual(c.fill_range[2], 13.5)

    def test_nothing_available_in_the_pessimistic_case_is_passive_with_the_whole_order_as_residual(self):
        from crypto_stat_arb.ems.tactics import choose_tactic
        c = choose_tactic(self.st(displayed_depth_in_cap=2.0, survival_mean=1.0, own_unrefilled_qty=5.0), self.cfg())
        self.assertEqual((c.tactic, c.reason, c.covered_qty, c.residual_qty), ("PASSIVE", "LQ_PARTIAL_RESIDUAL", 0.0, 10.0))

    def test_never_a_market_banded_and_the_first_gap_still_caps(self):
        from crypto_stat_arb.ems.tactics import choose_tactic
        c = choose_tactic(self.st(displayed_depth_in_cap=100.0, survival_mean=1.0, gap_status="GAP", depth_to_first_gap=6.0), self.cfg(gap_aware=True))
        self.assertEqual((c.tactic, c.reason), ("IOC_CAPPED", "LQ_DEPTH_LIMITED"))
        self.assertAlmostEqual(c.covered_qty, 6.0)
        for qty in (1.0, 10.0, 1000.0):
            self.assertNotEqual(choose_tactic(self.st(order_qty=qty, displayed_depth_in_cap=50.0, survival_mean=0.5), self.cfg()).tactic, "MARKET_BANDED")

    def test_the_flicker_discount_is_not_applied_with_the_range(self):
        from crypto_stat_arb.ems.tactics import choose_tactic
        kw = dict(displayed_depth_in_cap=14.0, survival_mean=1.0, flicker=1.0)
        with_range = choose_tactic(self.st(**kw), self.cfg(gap_aware=True, flicker_weight=1.0))
        self.assertEqual(with_range.reason, "LQ_DEPTH_COVERS")                                          # the flicker would have taken the whole depth away
        from crypto_stat_arb.ems.tactics import TacticChoiceConfig
        old = choose_tactic(self.st(executable_depth_within_cap=14.0, flicker=1.0), TacticChoiceConfig(liquidity_aware=True, gap_aware=True, flicker_weight=1.0))
        self.assertEqual((old.tactic, old.reason), ("PASSIVE", "LQ_DEPTH_LIMITED"))

    def test_off_is_todays_answer_and_missing_inputs_change_nothing(self):
        from crypto_stat_arb.ems.tactics import TacticChoiceConfig, choose_tactic
        plain = TacticChoiceConfig(liquidity_aware=True)
        for depth in (0.0, 4.0, 10.0, 20.0):
            a = choose_tactic(self.st(executable_depth_within_cap=depth), plain)
            b = choose_tactic(self.st(executable_depth_within_cap=depth, displayed_depth_in_cap=3.0, survival_mean=0.1, own_unrefilled_qty=9.0), plain)   # fields given, switch off
            c = choose_tactic(self.st(executable_depth_within_cap=depth), self.cfg())                                                                    # switch on, fields missing
            self.assertEqual(a, b)
            self.assertEqual(a, c)
            self.assertIsNone(a.fill_range)

    def test_bad_inputs_are_refused(self):
        from crypto_stat_arb.ems.tactics import TacticChoiceConfig, choose_tactic
        for kw in (dict(survival_mean=1.5), dict(survival_mean=-0.1), dict(displayed_depth_in_cap=-1.0), dict(own_unrefilled_qty=float("nan"))):
            with self.assertRaises(ValueError):
                choose_tactic(self.st(**kw), self.cfg())
        with self.assertRaises(ValueError):
            choose_tactic(self.st(), TacticChoiceConfig(liquidity_aware=True, fill_range=True, fill_range_low=1.5))


class OwnConsumptionLedger(unittest.TestCase):
    """Owner 15 Nov, step 2: our own takes are missing from the book and refill as 1 - exp(-t / tau). Hand-calculated; SYNTHETIC."""

    def test_the_ledger_follows_the_exponential_refill_and_adds_takes(self):
        from crypto_stat_arb.simulation.impact import OwnTakeLedger
        led = OwnTakeLedger(3.5)
        led.record("ask", 100.0, 4.0, 0.0)
        self.assertAlmostEqual(led.missing("ask", 100.0, 0.0), 4.0)                         # just taken: all of it missing
        self.assertAlmostEqual(led.missing("ask", 100.0, 3.5), 4.0 * math.exp(-1.0))
        self.assertAlmostEqual(led.missing("ask", 100.0, 3.5 * math.log(2)), 2.0)           # half refilled after tau ln 2
        led.record("ask", 100.0, 1.0, 3.5)
        self.assertAlmostEqual(led.missing("ask", 100.0, 3.5), 4.0 * math.exp(-1.0) + 1.0)
        self.assertEqual(led.missing("bid", 100.0, 0.0), 0.0)                               # the other side is untouched
        self.assertEqual(led.missing("ask", 100.1, 0.0), 0.0)
        self.assertEqual(led.missing_by_price("ask", 1000.0), {})                           # long after: refilled, and the entry is dropped
        with self.assertRaises(ValueError):
            OwnTakeLedger(0.0)
        with self.assertRaises(ValueError):
            led.record("mid", 100.0, 1.0, 0.0)

    def test_float_orderbook_refill_survives_a_public_update_and_the_overlay_does_not(self):
        from crypto_stat_arb.simulation.order_book import OrderBook
        for tau, survives in ((3.5, True), (None, False)):
            b = OrderBook("X", "P", own_overlay=True, own_refill_tau=tau)
            b.apply_snapshot([[99.9, 5.0]], [[100.0, 5.0], [100.1, 5.0]], 0.0)
            w = b.consume_overlay("ask", 4.0, None, 0.0)
            self.assertAlmostEqual(w.filled_qty, 4.0)
            self.assertAlmostEqual(b.walk("ask", 5.0, None, None, 0.0).vwap, (1.0 * 100.0 + 4.0 * 100.1) / 5.0)       # only 1 left at 100.0
            b.apply_delta("ask", 100.0, 5.0, "modify", 1.0)                                                          # a public update of that level
            vw = b.walk("ask", 5.0, None, None, 1.0).vwap
            if survives:
                missing = 4.0 * math.exp(-1.0 / 3.5)
                self.assertAlmostEqual(vw, ((5.0 - missing) * 100.0 + missing * 100.1) / 5.0)                        # still missing, partly refilled
            else:
                self.assertAlmostEqual(vw, 100.0)                                                                    # the overlay was cleared by the update
            b.apply_snapshot([[99.9, 5.0]], [[100.0, 5.0], [100.1, 5.0]], 2.0)
            vs = b.walk("ask", 5.0, None, None, 2.0).vwap
            self.assertEqual(vs > 100.0 + 1e-9, survives)                                                            # a snapshot does not reset the refill ledger either

    def test_refill_needs_the_overlay_and_a_zero_default_changes_nothing(self):
        from crypto_stat_arb.simulation.order_book import OrderBook
        with self.assertRaises(ValueError):
            OrderBook("X", "P", own_overlay=False, own_refill_tau=3.5)
        b = OrderBook("X", "P", own_overlay=True)
        self.assertIsNone(b.own_ledger)


class OwnConsumptionTakerWalk(unittest.TestCase):
    """The taker walk of the queue adapter reads a COPY of the core book with our recent takes removed; the real book is never touched; two orders cannot take the same liquidity."""
    BOOK = ([["100.00", "50"], ["99.99", "100"], ["99.80", "100"]], [["100.02", "50"], ["100.03", "100"]])

    @staticmethod
    def build(tau):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import test_queue_adapter as tq
        a = tq.adapter()
        if tau:
            from crypto_stat_arb.simulation.impact import OwnTakeLedger
            a.own_ledger = OwnTakeLedger(tau)
            a.consume_liquidity = False                                                        # what the replay driver does in refill mode: the ledger REPLACES the base adapter's withheld-liquidity mechanism
        return tq, a

    @staticmethod
    def sell(tq, a, book, order_id, qty, ts_s):
        from crypto_stat_arb.execution import CanonicalOrderRequest, OrderAction, OrderType, TimeInForce
        from crypto_stat_arb.models import Side
        from decimal import Decimal
        r = CanonicalOrderRequest.create(action=OrderAction.SUBMIT, order_id=order_id, parent_opportunity_id="op", parent_order_id="p" + order_id, instrument_id=tq.INSTR, venue=tq.VENUE, side=Side.SELL,
                                         price=Decimal("99.90"), quantity=Decimal(qty), order_type=OrderType.LIMIT, time_in_force=TimeInForce.IOC, post_only=False, created_ts_ns=int(ts_s * 1e9))
        return a.handle(r, book)[-1]

    def test_one_order_removes_exactly_its_size_and_refills_on_the_curve(self):
        tq, a = self.build(3.5)
        book = tq.core_book(*self.BOOK)
        first = self.sell(tq, a, book, "a", "60", 2.0)                                         # 50 at 100.00 and 10 at 99.99
        self.assertEqual(first.cumulative_filled_quantity, Decimal("60"))
        self.assertAlmostEqual(a.own_ledger.missing("bid", 100.0, 2.0), 50.0)
        self.assertAlmostEqual(a.own_ledger.missing("bid", 99.99, 2.0), 10.0)
        self.assertAlmostEqual(a.own_ledger.missing("bid", 99.8, 2.0), 0.0)
        self.assertEqual(dict(book.bids), {Decimal("100.00"): Decimal("50"), Decimal("99.99"): Decimal("100"), Decimal("99.80"): Decimal("100")})    # the real book is not touched
        # 3.5 s later: 50 e^-1 = 18.394 still missing at 100.00, 10 e^-1 = 3.679 at 99.99: a 40 sell gets 31.606 at 100.00 and 8.394 at 99.99
        second = self.sell(tq, a, book, "b", "40", 5.5)
        miss0, miss1 = 50 * math.exp(-1.0), 10 * math.exp(-1.0)
        want = ((50 - miss0) * 100.00 + (40 - (50 - miss0)) * 99.99) / 40
        self.assertEqual(second.cumulative_filled_quantity, Decimal("40"))
        self.assertAlmostEqual(float(second.average_price), want, places=6)

    def test_two_orders_in_a_row_cannot_take_the_same_liquidity(self):
        tq, a = self.build(3.5)
        book = tq.core_book(*self.BOOK)
        self.sell(tq, a, book, "a", "50", 2.0)                                                 # takes the whole 100.00 level
        second = self.sell(tq, a, book, "b", "5", 2.0)                                         # the same instant: 100.00 is gone, the next level is the price
        self.assertEqual(second.average_price, Decimal("99.99"))
        tq2, off = self.build(None)                                                            # control: today's adapter already withholds a take until the published level shrinks or a snapshot arrives ...
        book2 = tq2.core_book(*self.BOOK)
        self.sell(tq2, off, book2, "a", "50", 2.0)
        self.assertEqual(self.sell(tq2, off, book2, "b", "5", 2.0).average_price, Decimal("99.99"))
        later = self.sell(tq2, off, book2, "c", "5", 60.0)                                     # ... and NEVER refills: a minute later the 100.00 level is still withheld
        self.assertEqual(later.average_price, Decimal("99.99"))
        self.sell(tq, a, book, "c", "5", 60.0)
        self.assertEqual(self.sell(tq, a, book, "d", "5", 100.0).average_price, Decimal("100.00"))     # with the ledger a minute and more later the level has refilled (50 e^-17 missing)

    def test_the_ledger_records_the_unshifted_frame_under_an_impact_shift(self):
        tq, a = self.build(3.5)
        a.impact = ImpactOverlay(ImpactConfig(level=3), {"bybit": 0.01, "binance": 0.01})
        a.impact.applied_ticks["bybit"] = -3                                                   # every price the walk sees is 0.03 lower
        book = tq.core_book(*self.BOOK)
        last = self.sell(tq, a, book, "a", "60", 2.0)                                          # shifted bids: 99.97 x 50, 99.96 x 100 (limit 99.90 in the shifted frame)
        self.assertEqual(last.cumulative_filled_quantity, Decimal("60"))
        self.assertAlmostEqual(a.own_ledger.missing("bid", 100.0, 2.0), 50.0)                  # recorded where the book really is
        self.assertAlmostEqual(a.own_ledger.missing("bid", 99.99, 2.0), 10.0)


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class OwnConsumptionReplay(unittest.TestCase):
    def run_replay(self, own_impact, impact):
        from new_backtest.profiles import apply_profile, load_profile
        from new_backtest.session_replay import SessionReplay
        profile = load_profile("perp_perp_maker_taker_v1_9")
        cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_refill")
        cfg.arb.max_position_base = 1.0
        cfg.arb.participation_max = 0.0
        cfg.own_impact = own_impact
        cfg.impact = impact
        replay = SessionReplay(cfg, profile, stream=stream())
        return replay, replay.run()

    def test_refill_without_an_impact_block_is_refused(self):
        with self.assertRaisesRegex(ValueError, "impact"):
            self.run_replay("refill", None)

    def test_refill_records_every_aggressive_fill_and_the_default_is_untouched(self):
        base, a = self.run_replay("overlay", None)
        on, b = self.run_replay("refill", ImpactConfig(level=0, own_refill_tau_s=3.5))
        self.assertGreaterEqual(sum(ad.own_ledger.recorded for ad in on.adapters.values()), 1)
        self.assertTrue(all(ad.own_ledger is None for ad in base.adapters.values()))
        for key in ("signals", "orders"):
            self.assertEqual(a[key], b[key], key)
        self.assertEqual(ImpactConfig().own_refill_tau_s, 3.5)
        with self.assertRaises(ValueError):
            ImpactConfig(own_refill_tau_s=0.0)


if __name__ == "__main__":
    unittest.main()
