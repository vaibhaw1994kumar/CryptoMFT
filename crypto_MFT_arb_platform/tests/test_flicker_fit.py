"""Phase 1: the flicker measurement rule and the mixture fit, on synthetic data with a known law."""
from __future__ import annotations

import unittest

import numpy as np

from new_backtest.execution import flicker_fit as ff
from new_backtest.execution.ioc_policy import FlickerLaw


def draw(law: FlickerLaw, n: int, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    u = rng.random(n)
    out = np.zeros(n)
    plus = (u >= law.p0) & (u < law.p0 + law.p_plus)
    minus = u >= law.p0 + law.p_plus
    out[plus] = rng.exponential(1 / law.eta_plus, plus.sum())
    out[minus] = -rng.exponential(1 / law.eta_minus, minus.sum())
    return out


class MeasurementRuleTests(unittest.TestCase):
    def test_flicker_is_favourable_bps_of_the_fundamental(self):
        sell = {"side": "sell", "fund_bid": 100.0, "arr_bid": 100.02}
        self.assertAlmostEqual(ff.flicker_bps(sell), 2.0, places=9)                    # bid up 2 bps: favourable for a sell
        sell_adverse = {"side": "sell", "fund_bid": 100.0, "arr_bid": 99.97}
        self.assertAlmostEqual(ff.flicker_bps(sell_adverse), -3.0, places=9)
        buy = {"side": "buy", "fund_ask": 100.0, "arr_ask": 99.99}
        self.assertAlmostEqual(ff.flicker_bps(buy), 1.0, places=9)                      # ask down 1 bp: favourable for a buy
        self.assertIsNone(ff.flicker_bps({"side": "sell", "fund_bid": None, "arr_bid": 1.0}))

    def test_rows_without_a_fundamental_or_volatility_are_dropped(self):
        rows = [{"side": "sell", "fund_bid": 100.0, "arr_bid": 100.0, "vol_bps_1s": 1.0, "hour_utc": 5},
                {"side": "sell", "fund_bid": 100.0, "arr_bid": 100.0, "vol_bps_1s": None, "hour_utc": 5},
                {"side": "sell", "fund_bid": 0, "arr_bid": 100.0, "vol_bps_1s": 1.0, "hour_utc": 5}]
        self.assertEqual(len(ff.samples(rows)), 1)
        self.assertEqual(ff.samples(rows)[0]["bucket"], 0)                              # hour 5 -> 8-hour bucket 0


class FitTests(unittest.TestCase):
    LAW = FlickerLaw(0.5, 0.2, 0.3, 1.0, 2.0)

    def test_parameters_are_recovered_from_a_large_synthetic_sample(self):
        fit = ff.fit_mixture(draw(self.LAW, 60000))
        self.assertAlmostEqual(fit["p0"], 0.5, delta=0.01)
        self.assertAlmostEqual(fit["p_plus"], 0.2, delta=0.01)
        self.assertAlmostEqual(fit["p_minus"], 0.3, delta=0.01)
        self.assertAlmostEqual(fit["eta_plus"], 1.0, delta=0.05)
        self.assertAlmostEqual(fit["eta_minus"], 2.0, delta=0.1)

    def test_ks_is_small_for_the_true_law_and_large_for_a_wrong_one(self):
        F = draw(self.LAW, 20000, seed=2)
        good = ff.fit_mixture(F)
        self.assertLess(ff.ks(good, F), 0.02)
        wrong = dict(good, eta_plus=good["eta_plus"] * 4, p_plus=0.1, p0=good["p0"] + good["p_plus"] - 0.1)
        self.assertGreater(ff.ks(wrong, F), 0.05)

    def test_cdf_and_quantile_are_inverse_on_the_continuous_parts(self):
        fit = self.LAW.as_dict()
        for p in (0.05, 0.1, 0.9, 0.95):
            x = ff.quantile(fit, p)
            self.assertAlmostEqual(float(ff.cdf(fit, np.array([x]))[0]), p, places=9)
        self.assertEqual(ff.quantile(fit, 0.5), 0.0)                                    # inside the atom

    def test_too_few_samples_or_a_missing_tail_gives_no_fit(self):
        self.assertIsNone(ff.fit_mixture(draw(self.LAW, 20)))
        self.assertIsNone(ff.fit_mixture(np.zeros(100)))                                # no tails at all

    def test_qq_table_has_the_requested_quantiles(self):
        F = draw(self.LAW, 5000, seed=3)
        t = ff.qq(ff.fit_mixture(F), F)
        self.assertEqual([r["p"] for r in t], list(ff.QQ_P))
        self.assertAlmostEqual(t[-1]["empirical"], t[-1]["fitted"], delta=0.35)

    def test_fit_set_reports_every_level_with_its_n(self):
        rows = []
        rng = np.random.default_rng(4)
        for i, f in enumerate(draw(self.LAW, 900, seed=5)):
            rows.append({"side": "sell" if i % 2 else "buy", "fund_bid": 100.0, "arr_bid": 100.0 * (1 + f / 1e4), "fund_ask": 100.0,
                         "arr_ask": 100.0 * (1 - f / 1e4), "vol_bps_1s": float(rng.random() * 3), "hour_utc": int(rng.integers(0, 24))})
        s = ff.fit_set(rows)
        self.assertEqual(s["n_samples"], 900)
        self.assertEqual(s["cells"]["pooled"]["n"], 900)
        self.assertEqual(s["cells"]["sell"]["n"] + s["cells"]["buy"]["n"], 900)
        self.assertTrue(any(k.count("|") == 2 for k in s["cells"]))                     # side|bucket|tercile cells exist
        self.assertEqual(len(s["vol_edges_bps"]), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
