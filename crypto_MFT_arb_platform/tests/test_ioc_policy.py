"""Phase 1: the flicker law and the attempt DP (execution/ioc_policy.py), against values worked out by hand.

Law used throughout: p0 = 0.5, p+ = 0.2, p- = 0.3, eta+ = 1.0, eta- = 2.0 (per bps). Fee c = 5 bps, v0 = -7.8 bps."""
from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

import numpy as np

from new_backtest.execution import ExecutionConfig, ExecutionLayer
from new_backtest.execution.ioc_policy import (FlickerLaw, PolicyParams, attempts_from_latency, choose, limit_price, load_flicker_law,
                                               solve)

LAW = FlickerLaw(0.5, 0.2, 0.3, 1.0, 2.0)
E = math.e


class FillProbabilityTests(unittest.TestCase):
    def test_through_the_fundamental_d_ge_0(self):
        # p0 + p+ + p-(1 - exp(-eta- d))
        self.assertAlmostEqual(float(LAW.p_fill(0.0)), 0.7, places=12)
        self.assertAlmostEqual(float(LAW.p_fill(1.0)), 0.7 + 0.3 * (1 - E ** -2), places=12)          # 0.959399415...
        self.assertAlmostEqual(float(LAW.p_fill(3.0)), 0.7 + 0.3 * (1 - E ** -6), places=12)
        self.assertAlmostEqual(float(LAW.p_fill(1.0)), 0.9593994150290162, places=12)

    def test_speculative_d_lt_0(self):
        # p+ exp(-eta+ |d|)
        self.assertAlmostEqual(float(LAW.p_fill(-1.0)), 0.2 * E ** -1, places=12)                    # 0.0735758882...
        self.assertAlmostEqual(float(LAW.p_fill(-2.5)), 0.2 * E ** -2.5, places=12)

    def test_continuous_at_zero_from_the_speculative_side(self):
        self.assertAlmostEqual(float(LAW.p_fill(-1e-9)), 0.2, places=6)                              # just beyond S: only the +flicker fills

    def test_probabilities_must_be_a_distribution(self):
        with self.assertRaises(ValueError):
            FlickerLaw(0.5, 0.5, 0.5, 1.0, 1.0)
        with self.assertRaises(ValueError):
            FlickerLaw(0.5, 0.3, 0.2, 0.0, 1.0)

    def test_expected_flicker_on_fill_matches_numerical_integration(self):
        for d in (-2.0, -0.5, 0.0, 0.7, 2.0):
            z = np.linspace(-40, 40, 800001)
            dz = z[1] - z[0]
            dens = np.where(z > 0, 0.2 * 1.0 * np.exp(-1.0 * z), np.where(z < 0, 0.3 * 2.0 * np.exp(2.0 * z), 0.0))
            fill = z >= -d
            num = float(np.sum(z[fill] * dens[fill]) * dz)                                            # the point mass at 0 adds 0
            self.assertAlmostEqual(float(LAW.e_flicker_and_fill(d)), num, places=3, msg=f"d={d}")


class DpTests(unittest.TestCase):
    P = PolicyParams(fee_bps=5.0, v0_bps=-7.8, phi=0.0, q=1.0, dt_s=0.3)

    def test_one_attempt_matches_a_brute_force_search(self):
        ds = np.arange(-10.0, 6.0 + 1e-9, 0.05)
        pf = np.array([0.7 + 0.3 * (1 - math.exp(-2 * x)) if x >= 0 else 0.2 * math.exp(x) for x in ds])
        val = pf * (-ds - 5.0) + (1 - pf) * (-7.8)
        c = solve(LAW, self.P, 6.0, 1)[0]
        self.assertAlmostEqual(c.value_bps, float(val.max()), places=9)
        self.assertAlmostEqual(c.offset_bps, float(ds[np.argmax(val)]), places=9)

    def test_two_attempts_recursion(self):
        ds = np.arange(-10.0, 6.0 + 1e-9, 0.05)
        pf = np.array([0.7 + 0.3 * (1 - math.exp(-2 * x)) if x >= 0 else 0.2 * math.exp(x) for x in ds])
        v1 = (pf * (-ds - 5.0) + (1 - pf) * (-7.8)).max()
        v2 = (pf * (-ds - 5.0) + (1 - pf) * v1).max()
        self.assertAlmostEqual(solve(LAW, self.P, 6.0, 2)[1].value_bps, float(v2), places=9)

    def test_urgent_limit_crosses_to_the_cap(self):
        big = PolicyParams(fee_bps=5.0, v0_bps=-7.8, phi=1e7, q=1.0, dt_s=0.3)                       # waiting costs 3e6 bps
        for k in (1, 2, 5):
            c = solve(LAW, big, 4.0, k)[k - 1]
            self.assertAlmostEqual(c.offset_bps, 4.0, places=9)
            self.assertTrue(c.clipped)

    def test_patient_with_many_attempts_goes_speculative(self):
        c = solve(LAW, self.P, 6.0, 8)[7]
        self.assertLess(c.offset_bps, 0.0)                                                           # a limit better than the touch
        self.assertAlmostEqual(c.p_fill, float(LAW.p_fill(c.offset_bps)), places=12)

    def test_one_attempt_and_large_phi_is_the_phase0_limit_bit_for_bit(self):
        layer = ExecutionLayer(ExecutionConfig(), instrument="SOLUSDT")
        touch, walk = 100.0, 2.0
        cap_price = layer.touch_slack_cap("sell", touch, walk)                                      # phase 0: touch - max(walk, 5) bps
        d_cap = (touch - cap_price) / touch * 1e4
        big = PolicyParams(fee_bps=5.0, v0_bps=-7.8, phi=1e9, q=1.0, dt_s=0.3)
        ch = choose(LAW, big, d_cap, 1)
        self.assertTrue(ch.clipped)
        self.assertEqual(limit_price("sell", touch, ch.offset_bps, cap_price), cap_price)           # identical, not just close
        buy_cap = layer.touch_slack_cap("buy", touch, walk)
        self.assertEqual(limit_price("buy", touch, ch.offset_bps, buy_cap), buy_cap)

    def test_limit_never_passes_worst_price(self):
        for off in (-5.0, 0.0, 3.0, 50.0, 1e4):
            self.assertGreaterEqual(limit_price("sell", 100.0, off, 99.0), 99.0)
            self.assertLessEqual(limit_price("buy", 100.0, off, 101.0), 101.0)

    def test_a_cap_better_than_the_fundamental_is_respected(self):
        c = solve(LAW, self.P, -1.0, 3)[2]                                                           # the cap is 1 bp ABOVE the touch for a sell
        self.assertLessEqual(c.offset_bps, -1.0 + 1e-9)

    def test_arrival_price_model_hand_value(self):
        p = PolicyParams(fee_bps=5.0, v0_bps=-7.8, phi=0.0, price_model="arrival", d_min_bps=0.0, d_step_bps=1.0)
        # d = 0: E[F; fill] = p+/eta+ = 0.2 ; P = 0.7 ; V = 0.2 - 0.7*5 + 0.3*(-7.8) = -5.64
        ch = solve(LAW, p, 0.0, 1)[0]
        self.assertAlmostEqual(ch.value_bps, 0.2 - 3.5 - 2.34, places=9)

    def test_phi_units_are_bps_per_base2_second(self):
        # at q = 1 SOL and dt = 0.3 s the brief's phi grid is negligible next to a 5 bp fee: the chosen offsets do not move
        offs = {phi: solve(LAW, PolicyParams(fee_bps=5.0, v0_bps=-7.8, phi=phi, dt_s=0.3), 6.0, 3)[2].offset_bps
                for phi in (0.0, 1e-4, 1e-3, 1e-2)}
        self.assertEqual(len(set(offs.values())), 1, offs)

    def test_attempts_from_latency(self):
        self.assertEqual(attempts_from_latency(2.0, 0.326), 6)
        self.assertEqual(attempts_from_latency(0.1, 0.326), 1)
        self.assertEqual(attempts_from_latency(60.0, 0.1, cap=8), 8)

    def test_choose_is_cached_and_consistent_with_solve(self):
        a = choose(LAW, self.P, 6.0, 3)
        b = solve(LAW, self.P, 6.0, 3)[2]
        self.assertAlmostEqual(a.offset_bps, b.offset_bps, places=9)


class LawFileTests(unittest.TestCase):
    def test_fallback_chain_and_level_reporting(self):
        fit = {"p0": 0.5, "p_plus": 0.2, "p_minus": 0.3, "eta_plus": 1.0, "eta_minus": 2.0}
        doc = {"cells": {"sell|b0|t1": {"n": 40, "fit": fit}, "sell|t2": {"n": 90, "fit": fit}, "sell": {"n": 200, "fit": fit},
                         "pooled": {"n": 400, "fit": fit}, "buy": {"n": 12, "fit": None}}}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.json"
            p.write_text(json.dumps(doc))
            self.assertEqual(load_flicker_law(p, side="sell", hour=3, vol_tercile=1)[1]["level"], "cell")
            self.assertEqual(load_flicker_law(p, side="sell", hour=4, vol_tercile=2)[1]["level"], "side_tercile")
            self.assertEqual(load_flicker_law(p, side="sell", hour=4, vol_tercile=0)[1]["level"], "side")
            lv = load_flicker_law(p, side="buy", hour=1, vol_tercile=0)[1]                          # buy has no usable fit: pooled
            self.assertEqual((lv["level"], lv["n"]), ("pooled", 400))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
