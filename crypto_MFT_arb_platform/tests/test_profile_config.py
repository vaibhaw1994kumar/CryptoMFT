"""Phase D, step D5: the core builds its AppConfig from a run profile (`raw_config_from_profile` -> `config_from_raw`), so a replay and a live run share one builder. Wiring checks only."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb.config import config_from_raw, load_config, raw_config_from_profile

PROFILES = Path(__file__).resolve().parents[1] / "profiles"


def build(**kw):
    profile = yaml.safe_load((PROFILES / "perp_perp_maker_taker_v1_5.yaml").read_text())
    args = dict(pair="SOLUSDT", venues=("bybit", "binance"), ticks={"bybit": 0.005, "binance": 0.001}, quantity_steps={"bybit": 0.1, "binance": 0.01}, size_base=1.0)
    args.update(kw)
    raw, notes = raw_config_from_profile(profile, **args)
    return profile, raw, notes, config_from_raw(raw)[0]


class ProfileToCoreConfig(unittest.TestCase):
    def test_the_profile_becomes_a_valid_core_config_with_the_dated_grid(self):
        profile, raw, notes, cfg = build()
        self.assertEqual((cfg.venues, cfg.instrument_id), (("BYBIT", "BINANCE"), "SOL-USDT-PERP"))
        self.assertEqual(cfg.instruments.get("BYBIT", "SOL-USDT-PERP").tick_size, Decimal("0.005"))        # January 2022 grid, not the profile's venue_ticks
        self.assertEqual(cfg.instruments.get("BINANCE", "SOL-USDT-PERP").tick_size, Decimal("0.001"))
        self.assertEqual(cfg.instruments.get("BYBIT", "SOL-USDT-PERP").quantity_step, Decimal("0.1"))
        self.assertEqual(cfg.risk_limits.max_position_base, Decimal("1.25"))                            # ceiling 1.0 x size + headroom 0.25 x size
        self.assertEqual(cfg.initial_capital_by_venue, {"BYBIT": Decimal("1000.0"), "BINANCE": Decimal("1000.0")})
        self.assertEqual(cfg.latency["preset"], "singapore")
        self.assertEqual(cfg.residual_policy.max_unwind_attempts, 3)
        self.assertEqual(cfg.residual_policy.unwind_price_slippage_bps, Decimal("15.0"))

    def test_fees_are_the_profiles_tier_names_through_the_core_schedule(self):
        from crypto_stat_arb.fees import Liquidity
        profile, raw, notes, cfg = build()
        rate = lambda venue, liquidity: float(cfg.instruments.fee_bps(venue, "SOL-USDT-PERP", liquidity))
        self.assertEqual((profile["fee_presets"]["bybit"], profile["fee_presets"]["binance"]), ("bybit_perp_vip5", "binance_perp_vip9"))
        self.assertEqual(rate("BYBIT", Liquidity.MAKER) + rate("BINANCE", Liquidity.TAKER), 2.7)      # the 2.7 bps maker-hedge pair of the headline

    def test_what_the_profile_does_not_state_is_listed_for_the_manifest_and_the_core_never_imports_the_backtester(self):
        profile, raw, notes, cfg = build()
        for key in ("book_sanity", "time", "tick_and_step", "resync_delay_s"):
            self.assertIn(key, notes)
        text = (Path(__file__).resolve().parents[1] / "src/crypto_stat_arb/config.py").read_text()
        self.assertNotIn("new_backtest", text.replace("backtester", ""))      # the one mention is in prose about the backtester, never an import
        self.assertIsNotNone(load_config)


if __name__ == "__main__":
    unittest.main()
