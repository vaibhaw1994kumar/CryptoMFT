"""Owner 14 Nov, execution cost model step E: the market/limit order split (Cont and Kukanov, arXiv 1210.1625) in ems/venue_selector.py. Hand-calculated values; SYNTHETIC numbers, tests only."""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb.ems.venue_selector import SplitTable, SplitVenue, _project, _quantile, expected_split_cost, split_multi_venue, split_single_venue

UNIFORM = tuple(float(i) for i in range(1, 101))                       # outflow samples 1..100


def venue(**kw):
    base = dict(venue="BYBIT", available=True, queue_ahead=20.0, outflow=UNIFORM, half_spread_bps=0.5, fee_bps=2.0, rebate_bps=0.5)
    base.update(kw)
    return SplitVenue(**base)


class Quantile(unittest.TestCase):
    def test_linear_interpolation_and_clipping(self):
        xs = sorted(UNIFORM)
        self.assertAlmostEqual(_quantile(xs, 0.5), 50.5)
        self.assertAlmostEqual(_quantile(xs, 3.5 / 6.0), 58.75)                  # index 57.75: 58 + 0.75 x 1
        self.assertEqual((_quantile(xs, -1.0), _quantile(xs, 2.0)), (1.0, 100.0))
        with self.assertRaises(ValueError):
            _quantile([], 0.5)


class ClosedForm(unittest.TestCase):
    def test_hand_calculated_split(self):
        d = split_single_venue(100.0, venue(), lambda_u_bps=5.0)
        self.assertAlmostEqual(d.p, (2 * 0.5 + 2.0 + 0.5) / (5.0 + 0.5 + 0.5))      # 3.5 / 6
        self.assertAlmostEqual(d.limit["BYBIT"], 58.75 - 20.0)                       # L* = F^-1(p) - Q
        self.assertAlmostEqual(d.market["BYBIT"], 100.0 - 38.75)                     # M* = S - L*
        self.assertEqual((d.method, d.status), ("closed_form", "OK"))

    def test_impact_and_adverse_selection_enter_the_denominator_and_the_net_rebate(self):
        d = split_single_venue(100.0, venue(impact_bps=1.0, adverse_bps=0.3), lambda_u_bps=5.0)
        r = 0.5 - 0.3
        self.assertAlmostEqual(d.p, (1.0 + 2.0 + r) / (5.0 + 1.0 + 0.5 + r))

    def test_a_queue_longer_than_the_quantile_means_everything_at_market(self):
        d = split_single_venue(100.0, venue(queue_ahead=80.0), lambda_u_bps=5.0)
        self.assertEqual((d.limit, d.market), ({}, {"BYBIT": 100.0}))

    def test_the_limit_never_exceeds_the_quantity(self):
        d = split_single_venue(30.0, venue(queue_ahead=0.0), lambda_u_bps=5.0)      # F^-1(p) - Q = 58.75 > 30
        self.assertEqual((d.limit, d.market), ({"BYBIT": 30.0}, {}))

    def test_extreme_costs_push_all_one_way(self):
        crossing_free = split_single_venue(10.0, venue(half_spread_bps=-1.0, fee_bps=-1.0, rebate_bps=0.0), lambda_u_bps=5.0)          # p <= 0
        self.assertEqual(crossing_free.limit, {})
        resting_always_wins = split_single_venue(10.0, venue(fee_bps=50.0, queue_ahead=0.0), lambda_u_bps=0.0)                           # p >= 1
        self.assertEqual(resting_always_wins.limit, {"BYBIT": 10.0})

    def test_the_market_part_respects_its_cap_and_the_rest_goes_back_to_the_limit(self):
        d = split_single_venue(100.0, venue(max_market_qty=30.0), lambda_u_bps=5.0)
        self.assertEqual((d.market, d.limit, d.status), ({"BYBIT": 30.0}, {"BYBIT": 70.0}, "MARKET_CAPPED"))

    def test_unavailable_venue_and_bad_inputs(self):
        self.assertEqual(split_single_venue(10.0, venue(available=False), 5.0).status, "NO_VENUE")
        with self.assertRaises(ValueError):
            split_single_venue(0.0, venue(), 5.0)
        with self.assertRaises(ValueError):
            split_single_venue(10.0, venue(half_spread_bps=-5.0, rebate_bps=0.0), lambda_u_bps=0.0)

    def test_the_closed_form_minimises_the_expected_cost_on_the_same_samples(self):
        v, S, lam = venue(), 100.0, 5.0
        d = split_single_venue(S, v, lam)
        costs = {L: expected_split_cost(S, [v], {"BYBIT": S - L}, {"BYBIT": L}, lam) for L in range(0, 101)}
        best = min(costs, key=costs.get)
        self.assertLessEqual(abs(best - d.limit["BYBIT"]), 1.5)
        self.assertLessEqual(costs[round(d.limit["BYBIT"])] - costs[best], 1e-9 + 0.02)

    def test_the_overfill_penalty_is_inert_when_market_plus_limit_equal_the_quantity(self):
        v, S = venue(), 100.0
        a = expected_split_cost(S, [v], {"BYBIT": 40.0}, {"BYBIT": 60.0}, 5.0, 0.0)
        b = expected_split_cost(S, [v], {"BYBIT": 40.0}, {"BYBIT": 60.0}, 5.0, 99.0)
        self.assertEqual(a, b)


class MultiVenue(unittest.TestCase):
    def test_one_venue_the_averaged_estimate_is_close_to_the_closed_form(self):
        v, S, lam = venue(), 100.0, 5.0
        cf = split_single_venue(S, v, lam).limit["BYBIT"]
        sa = split_multi_venue(S, [v], lam, epochs=20, seed=3)
        self.assertEqual(sa.method, "stochastic_approximation")
        self.assertAlmostEqual(sum(sa.market.values()) + sum(sa.limit.values()), S, places=6)
        self.assertLess(abs(sa.limit.get("BYBIT", 0.0) - cf), 0.12 * S)

    def test_deterministic_for_a_seed(self):
        vs = [venue(), venue(venue="BINANCE", rebate_bps=0.0, queue_ahead=5.0)]
        a, b = split_multi_venue(100.0, vs, 5.0, seed=7), split_multi_venue(100.0, vs, 5.0, seed=7)
        self.assertEqual((a.market, a.limit, a.expected_cost_bps_per_unit), (b.market, b.limit, b.expected_cost_bps_per_unit))

    def test_the_cheaper_resting_venue_gets_more_of_the_limit_and_caps_are_kept(self):
        good = venue(venue="BYBIT", rebate_bps=1.5, queue_ahead=5.0)
        bad = venue(venue="BINANCE", rebate_bps=-1.0, queue_ahead=60.0)
        d = split_multi_venue(100.0, [good, bad], 5.0, epochs=10, seed=1)
        self.assertGreater(d.limit.get("BYBIT", 0.0), d.limit.get("BINANCE", 0.0))
        capped = split_multi_venue(100.0, [venue(max_market_qty=0.0), venue(venue="BINANCE", max_market_qty=10.0)], 5.0, epochs=5, seed=1)
        self.assertLessEqual(capped.market.get("BYBIT", 0.0), 1e-9)
        self.assertLessEqual(capped.market.get("BINANCE", 0.0), 10.0 + 1e-9)

    def test_infeasible_caps_and_no_venue(self):
        both_capped = [venue(max_market_qty=1.0, max_limit_qty=1.0)]
        self.assertEqual(split_multi_venue(100.0, both_capped, 5.0).status, "INFEASIBLE_MARKET_CAP")
        self.assertEqual(split_multi_venue(100.0, [venue(available=False)], 5.0).status, "NO_VENUE")

    def test_the_projection_sums_to_the_total_within_the_caps(self):
        x = _project([5.0, -2.0, 9.0, 1.0], [3.0, 10.0, 4.0, 10.0], 10.0)
        self.assertAlmostEqual(sum(x), 10.0)
        self.assertTrue(all(-1e-12 <= xi <= ui + 1e-12 for xi, ui in zip(x, [3.0, 10.0, 4.0, 10.0])))
        self.assertIsNone(_project([1.0, 1.0], [1.0, 1.0], 5.0))


class Table(unittest.TestCase):
    NORM = tuple(i / 100.0 for i in range(1, 101))                                 # outflow as a share of the recent volume

    def test_the_table_equals_the_closed_form_at_the_grid_points_and_interpolates_between(self):
        v = venue(queue_ahead=0.0, outflow=())
        lam = 5.0
        tab = SplitTable.build(self.NORM, v, lam, queues=[0.0, 20.0, 40.0], volumes=[50.0, 100.0, 200.0])
        for Q in (0.0, 20.0, 40.0):
            for V in (50.0, 100.0, 200.0):
                cf = split_single_venue(1000.0, venue(queue_ahead=Q, outflow=tuple(V * x for x in self.NORM)), lam).limit.get("BYBIT", 0.0)
                self.assertAlmostEqual(tab.limit_size(Q, V, 1000.0), cf, places=6, msg=(Q, V))
        a, b = tab.limit_size(20.0, 100.0, 1000.0), tab.limit_size(20.0, 200.0, 1000.0)
        self.assertAlmostEqual(tab.limit_size(20.0, 150.0, 1000.0), 0.5 * (a + b), places=6)

    def test_clipped_to_the_quantity_and_to_zero(self):
        tab = SplitTable.build(self.NORM, venue(outflow=()), 5.0, queues=[0.0, 1000.0], volumes=[50.0, 100.0])
        self.assertEqual(tab.limit_size(1000.0, 50.0, 10.0), 0.0)
        self.assertEqual(tab.limit_size(0.0, 100.0, 10.0), 10.0)


class ChooseTacticMode(unittest.TestCase):
    @staticmethod
    def st(**kw):
        from crypto_stat_arb.ems.tactics import MarketState
        base = dict(book_age_s=0.1, spread_bps=2.0, volatility_bucket="normal", inventory_age_s=0.0, inventory_pressure=0.7, order_qty=10.0)
        base.update(kw)
        return MarketState(**base)

    @staticmethod
    def pick(state, **cfg):
        from crypto_stat_arb.ems.tactics import TacticChoiceConfig, choose_tactic
        c = choose_tactic(state, TacticChoiceConfig(**cfg), allow_market_banded=True)
        return c

    def test_off_by_default_the_split_fields_are_not_read(self):
        a = self.pick(self.st(split_market_qty=4.0, split_limit_qty=6.0))
        b = self.pick(self.st())
        self.assertEqual((a.tactic, a.reason), (b.tactic, b.reason))

    def test_the_split_becomes_a_covered_part_and_a_residual(self):
        c = self.pick(self.st(split_market_qty=4.0, split_limit_qty=6.0), order_split=True)
        self.assertEqual((c.tactic, c.reason, c.covered_qty, c.residual_qty), ("IOC_CAPPED", "SPLIT_MARKET_LIMIT", 4.0, 6.0))
        rest = self.pick(self.st(split_market_qty=0.0, split_limit_qty=10.0), order_split=True)
        self.assertEqual((rest.tactic, rest.covered_qty, rest.residual_qty), ("PASSIVE", 0.0, 10.0))

    def test_the_market_part_is_capped_by_the_effective_depth_and_the_gap(self):
        c = self.pick(self.st(split_market_qty=8.0, split_limit_qty=2.0, executable_depth_within_cap=5.0), order_split=True)
        self.assertEqual((c.covered_qty, c.residual_qty), (5.0, 5.0))
        g = self.pick(self.st(split_market_qty=8.0, split_limit_qty=2.0, depth_to_first_gap=3.0, gap_status="GAP"), order_split=True, gap_aware=True)
        self.assertEqual((g.covered_qty, g.residual_qty), (3.0, 7.0))
        ignored = self.pick(self.st(split_market_qty=8.0, split_limit_qty=2.0, depth_to_first_gap=3.0, gap_status="GAP"), order_split=True)       # gap_aware off: the gap is not read
        self.assertEqual(ignored.covered_qty, 8.0)

    def test_emergencies_are_never_split_and_a_bad_split_is_refused(self):
        c = self.pick(self.st(time_to_limit_s=1.0, split_market_qty=4.0, split_limit_qty=6.0), order_split=True)
        self.assertEqual((c.tactic, c.reason), ("MARKET_BANDED", "TIME_TO_LIMIT_FLATTEN"))
        with self.assertRaises(ValueError):
            self.pick(self.st(split_market_qty=4.0, split_limit_qty=5.0), order_split=True)                    # does not add up to 10
        with self.assertRaises(ValueError):
            self.pick(self.st(split_market_qty=4.0), order_split=True)                                         # one half only


sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_session_replay import SLICES, stream            # noqa: E402


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class JournalEvidence(unittest.TestCase):
    """Step F: with depth quality on, an impact curve and a split config, every intent and child of the replay's journal carries the depth quality, the impact estimate and what the order split would have chosen;
    the run itself is unchanged (observation only); with none of them configured the journal has no such keys."""

    def run_replay(self, depth_quality=True, impact=None, split=None):
        import json
        import tempfile
        from new_backtest.profiles import apply_profile, load_profile
        from new_backtest.session_replay import SessionReplay
        profile = load_profile("perp_perp_maker_taker_v1_9")
        cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_split_ev")
        cfg.arb.max_position_base = 1.0
        cfg.arb.participation_max = 0.0
        cfg.venue_state.depth_quality = depth_quality
        cfg.impact = impact
        if split is not None:
            f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
            json.dump([0.0, 0.05, 0.2, 0.5, 1.0, 2.0, 5.0], f)
            f.close()
            split.outflow_ratio_file = f.name
        cfg.split = split
        replay = SessionReplay(cfg, profile, stream=stream())
        return replay, replay.run()

    def test_every_decision_carries_the_evidence_and_the_run_is_unchanged(self):
        from crypto_stat_arb.ems.venue_selector import SplitConfig
        from crypto_stat_arb.simulation.impact import ImpactConfig
        base_r, base = self.run_replay()
        imp = ImpactConfig(level=2, form="volume", y=0.113, sigma_daily_bps=547.0, daily_volume=3.7e7, delta_volume=0.44)
        on_r, on = self.run_replay(impact=imp, split=SplitConfig(lambda_u_bps=2.6))
        for key in ("fills", "orders", "signals", "positions", "cash_by_venue", "ledger_event_counts"):
            self.assertEqual(base[key], on[key], key)
        journal = on_r.session.intent_observer.layer.journal
        tagged = [j for j in journal if j["event"] in ("intent", "child")]
        self.assertTrue(tagged)
        for j in tagged:
            self.assertIn("depth_quality", j)
            self.assertIn("impact", j)
            self.assertIn("order_split", j)
            (venue, sp), = j["order_split"].items()
            self.assertAlmostEqual(sp["market"] + sp["limit"], j.get("qty", sp["market"] + sp["limit"]), places=9) if "qty" in j else None
            self.assertIn("OBSERVATION ONLY", sp["label"])
            for key in ("queue_ahead", "recent_volume", "half_spread_bps", "taker_fee_bps", "rebate_bps", "impact_bps", "lambda_u_bps", "max_market_qty", "survival_mean"):
                self.assertIn(key, sp["inputs"])
            self.assertIn("peak_bps", j["impact"][venue])
        base_j = base_r.session.intent_observer.layer.journal
        self.assertTrue(all("impact" not in j and "order_split" not in j for j in base_j))
        strip = lambda rows: [{k: v for k, v in j.items() if k not in ("depth_quality", "impact", "order_split")} for j in rows]
        self.assertEqual(strip(journal), strip(base_j))

    def test_split_evidence_needs_depth_quality(self):
        from crypto_stat_arb.ems.venue_selector import SplitConfig
        with self.assertRaises(ValueError):
            self.run_replay(depth_quality=False, split=SplitConfig())

    def test_split_config_is_validated_and_off_by_default(self):
        from crypto_stat_arb.ems.venue_selector import SplitConfig
        for kw in (dict(horizon_s=0.0), dict(lambda_u_bps=-1.0), dict(volume_window_s=0.0), dict(min_recent_volume=float("nan"))):
            with self.assertRaises(ValueError, msg=kw):
                SplitConfig(**kw)
        _, res = self.run_replay(depth_quality=False)
        self.assertTrue(res)


if __name__ == "__main__":
    unittest.main()
