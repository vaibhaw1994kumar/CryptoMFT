"""Step 11 add-on: Kaplan-Meier, IPCW, RMST and the support diagnostics (guide s8)."""
from __future__ import annotations

import unittest

import numpy as np

from new_backtest.queue_v2 import survival as sv


class SurvivalTests(unittest.TestCase):
    def test_without_censoring_km_is_the_empirical_curve_and_rmst_is_the_mean_of_min(self):
        t = np.array([1.0, 2.0, 2.0, 4.0, 8.0])
        e = np.ones(5, int)
        self.assertAlmostEqual(sv.rmst(t, e, 5.0), float(np.minimum(t, 5.0).mean()), places=12)
        d = sv.diagnostics(t, e, horizons=(2.0,), min_at_risk=1)["horizons"]["2s"]
        self.assertAlmostEqual(d["km_cdf"], 3 / 5)
        self.assertAlmostEqual(d["cdf"], 3 / 5)               # all weights are 1
        self.assertFalse(d["identifiable"])                    # no censoring at all: the cancel policy cannot be learned from this set

    def test_hand_computed_km_with_a_censoring(self):
        # times 1(fill) 2(cens) 3(fill) 4(fill): S(1)=4/5? n=4 -> S(1)=3/4, at t=3 risk=2 -> S=3/4*1/2=3/8, at t=4 risk=1 -> 0
        t, e = np.array([1.0, 2.0, 3.0, 4.0]), np.array([1, 0, 1, 1])
        tt, s = sv.km_survival(t, e)
        self.assertEqual(list(np.round(s, 6)), [0.75, 0.75, 0.375, 0.0])
        self.assertAlmostEqual(sv.rmst(t, e, 4.0), 1.0 + 0.75 * 2.0 + 0.375 * 1.0)

    def test_ipcw_and_km_recover_the_truth_where_the_naive_share_is_biased(self):
        rng = np.random.default_rng(7)
        n = 40000
        T = rng.exponential(10.0, n)                           # true P(T <= 5) = 1 - exp(-0.5)
        C = rng.exponential(6.0, n)                            # independent cancellation
        x, e = np.minimum(T, C), (T <= C).astype(int)
        truth = 1.0 - np.exp(-0.5)
        d = sv.diagnostics(x, e, horizons=(5.0,))["horizons"]["5s"]
        self.assertGreater(abs(d["naive"] - truth), 0.04)      # the share of orders filled by H under the actual cancel policy is lower
        self.assertLess(abs(d["cdf"] - truth), 0.015)
        self.assertLess(abs(d["km_cdf"] - truth), 0.015)
        self.assertAlmostEqual(sv.rmst(x, e, 5.0), float(10.0 * (1 - np.exp(-0.5))), delta=0.06)
        self.assertTrue(d["identifiable"])

    def test_stratified_censoring_fixes_censoring_that_depends_on_a_covariate(self):
        rng = np.random.default_rng(11)
        n = 60000
        z = rng.integers(0, 2, n)                              # stratum 1 fills fast and is cancelled fast; stratum 0 fills slowly and is rarely cancelled
        T = rng.exponential(np.where(z == 1, 3.0, 20.0))
        C = rng.exponential(np.where(z == 1, 2.0, 60.0))
        x, e = np.minimum(T, C), (T <= C).astype(int)
        truth = float(np.mean(1.0 - np.exp(-5.0 / np.where(z == 1, 3.0, 20.0))))
        plain = sv.ipcw_cdf(x, e, 5.0)["cdf"]
        strat = sv.ipcw_cdf(x, e, 5.0, strata=z)["cdf"]
        self.assertLess(abs(strat - truth), abs(plain - truth))
        self.assertLess(abs(strat - truth), 0.015)

    def test_administrative_censoring_before_the_horizon_leaves_the_tail_unidentified(self):
        t = np.full(200, 15.0)                                 # every order expired at a 15 s time-in-force, none filled
        e = np.zeros(200, int)
        self.assertTrue(np.isnan(sv.rmst(t, e, 60.0)))        # nobody observable at 60 s
        d = sv.diagnostics(t, e, horizons=(60.0,))["horizons"]["60s"]
        self.assertFalse(d["identifiable"])
        self.assertEqual(d["at_risk_at_H"], 0)

    def test_heavy_censoring_is_flagged_by_the_weight_diagnostics(self):
        rng = np.random.default_rng(3)
        n = 5000
        T = rng.exponential(30.0, n)
        C = rng.exponential(1.0, n)                            # almost everything is cancelled within ~1 s
        x, e = np.minimum(T, C), (T <= C).astype(int)
        d = sv.ipcw_cdf(x, e, 20.0)
        self.assertGreater(d["max_weight"], 20.0)
        self.assertLess(d["effective_sample_size_of_weights"], d["n_filled_by_H"])
        self.assertFalse(d["identifiable"])                    # G falls below the support floor

    def test_an_almost_uncensored_set_reports_its_censoring_support(self):
        e = np.ones(1000, int); e[:2] = 0
        x = np.linspace(1.0, 100.0, 1000)
        d = sv.diagnostics(x, e, horizons=(5.0,))["horizons"]["5s"]
        self.assertEqual(d["n_censored_before_H"], 2)          # both censored orders (t = 1.0 and 1.099) fall inside the 5 s horizon
        self.assertAlmostEqual(d["share_censored_before_H"], 0.002)


if __name__ == "__main__":
    unittest.main()
