"""cancel_model options, explicit model settings in every result, and the fill cause tags.

Models (MakerConfig.cancel_model):  None = not chosen (behaves as all_ahead and a backtest warns),  "all_ahead",
"proportional",  "power" (share ahead = ahead**n / (ahead**n + behind**n); n = 1 is exactly "proportional").
"""
from __future__ import annotations

import json
import random
import tempfile
import unittest
import warnings
from pathlib import Path

from new_backtest.arb_strategy import MAKER_TAKER, ArbConfig
from new_backtest.backtester import ArbBacktestConfig, CrossVenueArbBacktester, DefaultModelWarning
from new_backtest.fill_engine import MakerConfig

from synthetic_days import gen
from test_fill_engine_queue import _book, _executor, _place


def grown_level(model, n=1.0):
    """Order behind a displayed level of 10; the level then grows to 30 (20 joins behind us) and drops to 24."""
    ex = _executor(MakerConfig(cancel_model=model, cancel_power_n=n))
    o = _place(ex, _book(bids=[(100.0, 10.0)]), price=100.0, size=5.0)
    o.arrival_ts = 0.0
    ex.on_delta("bybit", "bid", 100.0, 30.0, 2.0, 1.0, 0.0)
    ex.on_delta("bybit", "bid", 100.0, 24.0, 3.0, 1.0, 0.0)
    return o


class PowerModelTests(unittest.TestCase):
    def test_n_equal_one_is_exactly_proportional_on_a_random_event_stream(self):
        rng = random.Random(7)
        a = _executor(MakerConfig(cancel_model="proportional"))
        b = _executor(MakerConfig(cancel_model="power", cancel_power_n=1.0))
        orders = []
        for ex in (a, b):
            o = _place(ex, _book(bids=[(100.0, 8.0)]), price=100.0, size=6.0)
            o.arrival_ts = 0.0
            orders.append(o)
        level, t = 8.0, 1.0
        for _ in range(2000):
            t += rng.uniform(0.001, 0.2)
            if rng.random() < 0.6:
                level = max(0.0, level + rng.uniform(-6, 5))
                fa = a.on_delta("bybit", "bid", 100.0, level, t, 1.0, 0.0)
                fb = b.on_delta("bybit", "bid", 100.0, level, t, 1.0, 0.0)
            else:
                v = rng.uniform(0.01, 4.0)
                fa = a.on_trade("bybit", 100.0, v, -1, t, 0.0)
                fb = b.on_trade("bybit", 100.0, v, -1, t, 0.0)
            self.assertEqual([f.qty for f in fa], [f.qty for f in fb])
            self.assertEqual((orders[0].queue_ahead, orders[0].filled_qty), (orders[1].queue_ahead, orders[1].filled_qty))
            if not a.orders:
                break

    def test_other_exponents_change_the_share_ahead_in_the_expected_direction(self):
        # displayed 30 = 10 ahead + 20 behind, a decrease of 6: proportional drains 6 * 10/30 = 2.0
        self.assertAlmostEqual(grown_level("proportional").queue_ahead, 8.0, places=9)
        self.assertAlmostEqual(grown_level("power", 1.0).queue_ahead, 8.0, places=9)
        # n = 2: share 100/(100+400) = 0.2 -> drains 1.2 (less than proportional: the larger side gets more of the cancels)
        self.assertAlmostEqual(grown_level("power", 2.0).queue_ahead, 8.8, places=9)
        # n = 0.5: share sqrt(10)/(sqrt(10)+sqrt(20)) = 0.41421 -> drains 2.48528
        self.assertAlmostEqual(grown_level("power", 0.5).queue_ahead, 10.0 - 6.0 * (10 ** 0.5) / (10 ** 0.5 + 20 ** 0.5), places=9)

    def test_validation(self):
        with self.assertRaises(ValueError):
            MakerConfig(cancel_model="bogus")
        for bad in (0.0, -1.0):
            with self.assertRaises(ValueError):
                MakerConfig(cancel_model="power", cancel_power_n=bad)


class DefaultAndMetadataTests(unittest.TestCase):
    def test_the_default_is_proportional_and_none_behaves_identically(self):
        self.assertEqual(MakerConfig().cancel_model, "proportional")
        self.assertEqual(MakerConfig().effective_cancel_model, "proportional")
        self.assertEqual(MakerConfig(cancel_model=None).effective_cancel_model, "proportional")
        a, b = grown_level(None), grown_level("proportional")
        self.assertEqual((a.queue_ahead, a.filled_qty), (b.queue_ahead, b.filled_qty))

    def test_all_ahead_is_still_available_and_differs_from_the_default_on_a_grown_level(self):
        a, b = grown_level("all_ahead"), grown_level("proportional")
        self.assertNotEqual((a.queue_ahead, a.filled_qty), (b.queue_ahead, b.filled_qty))

    @staticmethod
    def _cfg(model, out=None, **maker):
        return ArbBacktestConfig(
            data_root="unused", venues=("bybit", "binance"), pair="BTCUSDT", start_date="2026-10-01", end_date="2026-10-01",
            latency_preset="colocated_both", bybit_fee_preset="bybit_perp_mm", binance_fee_preset="binance_perp_vip0",
            maker=MakerConfig(min_queue_ahead=0.5, cancel_model=model, **maker),
            arb=ArbConfig(mode=MAKER_TAKER, maker_max_age_s=10.0), output_dir=out)

    def test_relying_on_the_default_warns_and_an_explicit_choice_does_not(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            CrossVenueArbBacktester(self._cfg(None))
        self.assertTrue(any(issubclass(x.category, DefaultModelWarning) and "cancel_model" in str(x.message) for x in w))
        for model in ("all_ahead", "proportional", "power"):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                CrossVenueArbBacktester(self._cfg(model))
            self.assertFalse(any(issubclass(x.category, DefaultModelWarning) for x in w), model)

    def test_the_warning_is_not_a_runtime_warning_so_existing_warning_checks_are_unaffected(self):
        self.assertFalse(issubclass(DefaultModelWarning, RuntimeWarning))

    def test_model_settings_are_complete_and_distinguish_default_from_explicit(self):
        s = CrossVenueArbBacktester(self._cfg("power", cancel_power_n=2.5, delta_fill_share=0.05, snapshot_trade_explain=False)).model_settings()
        for k, v in (("cancel_model", "power"), ("cancel_model_explicit", True), ("cancel_power_n", 2.5), ("delta_fill_share", 0.05),
                     ("snapshot_netting", False), ("depth_size_fix", True)):
            self.assertEqual(s[k], v, k)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            d = CrossVenueArbBacktester(self._cfg(None)).model_settings()
        self.assertEqual((d["cancel_model"], d["cancel_model_explicit"]), ("proportional", False))

    def test_every_run_writes_its_settings_into_the_result_and_the_output_folder(self):
        with tempfile.TemporaryDirectory() as d, warnings.catch_warnings():
            warnings.simplefilter("ignore")
            root = Path(d) / "data"
            gen(root, ndays=1, seed=3, session_s=15.0)
            cfg = self._cfg("proportional", out=str(Path(d) / "out"), cancel_power_n=1.0)
            cfg.data_root = str(root)
            r = CrossVenueArbBacktester(cfg).run()
            on_disk = json.loads((Path(d) / "out" / "model_settings.json").read_text())
        self.assertEqual(r.model_settings, on_disk)
        self.assertEqual(on_disk["cancel_model"], "proportional")


class FillCauseTests(unittest.TestCase):
    def test_a_fill_from_a_print_is_tagged_trade(self):
        ex = _executor(MakerConfig(cancel_model="all_ahead"))
        o = _place(ex, _book(bids=[(100.0, 2.0)]), price=100.0, size=5.0)
        o.arrival_ts = 0.0
        fills = ex.on_trade("bybit", 100.0, 4.0, -1, 2.0, 0.0)
        self.assertEqual([f.cause for f in fills], ["trade"])

    def test_a_fill_from_the_delta_share_of_a_decrease_is_tagged_delta_share(self):
        ex = _executor(MakerConfig(cancel_model="proportional", min_queue_ahead=0.0))
        o = _place(ex, _book(bids=[(99.0, 5.0)]), price=100.0, size=5.0)       # level absent: queue 0
        o.arrival_ts = 0.0
        ex.on_delta("bybit", "bid", 100.0, 20.0, 2.0, 1.0, 0.0)
        fills = ex.on_delta("bybit", "bid", 100.0, 10.0, 3.0, 1.0, 0.0)        # 10 left the level, none of it ahead of us
        self.assertEqual([f.cause for f in fills], ["delta_share"])
        self.assertAlmostEqual(fills[0].qty, 1.5, places=9)                    # 15% of the 10

    def test_a_fill_when_the_level_vanishes_is_tagged_delta_vanish(self):
        ex = _executor(MakerConfig(cancel_model="proportional", min_queue_ahead=0.0))
        o = _place(ex, _book(bids=[(99.0, 5.0)]), price=100.0, size=5.0)
        o.arrival_ts = 0.0
        ex.on_delta("bybit", "bid", 100.0, 20.0, 2.0, 1.0, 0.0)
        fills = ex.on_delta("bybit", "bid", 100.0, 0.0, 3.0, 1.0, 0.0)
        self.assertEqual([f.cause for f in fills], ["delta_vanish"])


if __name__ == "__main__":
    unittest.main()
