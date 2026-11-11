"""Track A item 4 (owner 11 Nov): the safe-config and mandatory-switch rules live in the core (`crypto_stat_arb.config`), once. The backtester's profile loader and the live / paper builder
(`raw_config_from_profile`) both call them: a live or paper run refuses what the backtest refuses, with the same reasons."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb import config as core
from new_backtest import profiles
from new_backtest.profiles import ProfileError, apply_profile, load_profile

PLATFORM = Path(__file__).resolve().parents[1]
ARGS = dict(pair="SOLUSDT", venues=("bybit", "binance"), ticks={"bybit": 0.005, "binance": 0.001}, quantity_steps={"bybit": 0.1, "binance": 0.01}, size_base=1.0)


def profile(name="perp_perp_maker_taker_v1_6"):
    return yaml.safe_load((PLATFORM / "profiles" / f"{name}.yaml").read_text())


def mutated(path, value, name="perp_perp_maker_taker_v1_6"):
    doc = profile(name)
    cur = doc
    keys = path.split(".")
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    if value is None and len(keys) == 1:
        doc[keys[0]] = None
    else:
        cur[keys[-1]] = value
    return doc


# (what the mutation disables, the key the reason must name)
UNSAFE = [("arb.dispatch_mode", "simultaneous"), ("enforce_book_state", False), ("arb.count_pending_exposure", False), ("arb.count_unknown_exposure", False), ("arb.maker_max_age_s", 0),
          ("timing.dead_feed_s", None), ("breaker", None), ("margin", None), ("execution.one_hedge_child", "off"), ("execution.risk_reservation", "off"), ("valuation", "last_mark")]


class TheRulesLiveInTheCoreOnce(unittest.TestCase):
    def test_the_profile_table_takes_its_mandatory_rows_from_the_core(self):
        self.assertEqual(set(profiles.MANDATORY), set(core.MANDATORY_BY_KEY))
        for key, sw in profiles.MANDATORY.items():
            c = core.MANDATORY_BY_KEY[key]
            self.assertEqual((sw.forbidden, sw.note), (c.forbidden, c.note), key)
        self.assertIs(profiles.core_check_mandatory, core.check_mandatory)

    def test_the_core_never_imports_the_backtester(self):
        text = (PLATFORM / "src" / "crypto_stat_arb" / "config.py").read_text()
        self.assertNotIn("new_backtest", text.replace("`new_backtest/profiles.py`", ""))

    def test_every_profile_gets_the_same_answer_from_both(self):
        for path in sorted((PLATFORM / "profiles").glob("*.yaml")):
            doc = load_profile(path)
            self.assertEqual(profiles.check_mandatory(doc), core.check_mandatory(doc), path.name)


class LiveRefusesWhatTheBacktestRefuses(unittest.TestCase):
    def test_a_valid_working_profile_passes_both(self):
        core.require_safe_profile(profile())
        raw, _ = core.raw_config_from_profile(profile(), **ARGS)
        self.assertTrue(raw)
        apply_profile(load_profile("perp_perp_maker_taker_v1_6"), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", data_root="", output_dir="", dataset="mar2024")

    def test_each_disabled_mandatory_control_is_refused_by_the_live_builder_and_the_backtest_with_the_same_key(self):
        for path, value in UNSAFE:
            doc = mutated(path, value)
            with self.assertRaises(core.UnsafeConfigError, msg=path) as live:
                core.raw_config_from_profile(doc, **ARGS)
            with self.assertRaises(ProfileError, msg=path) as backtest:
                apply_profile(copy.deepcopy(doc), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", data_root="", output_dir="", dataset="mar2024")
            self.assertIn(path.split(".")[-1] if path in ("breaker", "margin") else path, str(live.exception), path)
            self.assertIn("disables a MANDATORY control", str(live.exception))
            self.assertIn("disables a MANDATORY control", str(backtest.exception))

    def test_a_sensitivity_only_latency_preset_needs_a_label_in_both(self):
        doc = mutated("latency_preset", "colocated_both")
        with self.assertRaisesRegex(core.UnsafeConfigError, "physically impossible"):
            core.raw_config_from_profile(doc, **ARGS)
        raw, _ = core.raw_config_from_profile(doc, latency_sensitivity="upper bound on the hedge delay", **ARGS)
        self.assertTrue(raw)
        with self.assertRaisesRegex(ProfileError, "physically impossible"):
            apply_profile(copy.deepcopy(doc), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", data_root="", output_dir="", dataset="mar2024")

    def test_legacy_v0_stays_exempt_by_name(self):
        legacy = mutated("enforce_book_state", False, name="perp_perp_maker_taker_v1_6")      # a disabled control, exempt only by the profile's name
        legacy["name"] = "legacy_v0"
        core.require_safe_profile(legacy)
        self.assertEqual(core.check_mandatory(legacy), [])

    def test_all_reasons_are_reported_at_once(self):
        doc = mutated("breaker", None)
        doc["enforce_book_state"] = False
        doc["latency_preset"] = "colocated_both"
        with self.assertRaises(core.UnsafeConfigError) as e:
            core.require_safe_profile(doc)
        text = str(e.exception)
        self.assertTrue("breaker=None" in text and "enforce_book_state=False" in text and "physically impossible" in text, text)


class RealOrdersStartOnlyFromAProfile(unittest.TestCase):
    """Owner 11 Nov: VENUE_TESTNET (real orders) is built only from a profile, the route with the safe-config checks. A plain config/*.json is for replay and paper."""

    def test_a_profile_config_carries_its_origin_and_a_plain_file_does_not(self):
        raw, _ = core.raw_config_from_profile(profile(), **ARGS)
        cfg, _ = core.config_from_raw(raw)
        self.assertEqual(cfg.origin, "profile:perp_perp_maker_taker_v1_6")
        plain, _ = core.load_config(PLATFORM / "config" / "perp_research.json")
        self.assertEqual(plain.origin, "config_file")

    def test_the_session_refuses_real_orders_from_a_plain_config_and_accepts_them_from_a_profile(self):
        from crypto_stat_arb.sdk import TestnetVenueAdapter
        from crypto_stat_arb.session import ExecutionMode, TradingSession
        plain, _ = core.load_config(PLATFORM / "config" / "perp_research.json")
        adapters = {v: TestnetVenueAdapter(v, lambda request, secret: []) for v in plain.venues}
        with self.assertRaises(ValueError) as e:
            TradingSession(plain, execution_mode=ExecutionMode.VENUE_TESTNET, live_data=True, adapters=adapters)
        self.assertIn("profile", str(e.exception))
        raw, _ = core.raw_config_from_profile(profile(), **ARGS)
        cfg, _ = core.config_from_raw(raw)
        session = TradingSession(cfg, execution_mode=ExecutionMode.VENUE_TESTNET, live_data=True, adapters={v: TestnetVenueAdapter(v, lambda request, secret: []) for v in cfg.venues})
        self.assertEqual(session.cfg.origin, "profile:perp_perp_maker_taker_v1_6")

    def test_an_unsafe_profile_never_reaches_a_session_so_real_orders_cannot_start_from_it(self):
        with self.assertRaises(core.UnsafeConfigError):
            core.raw_config_from_profile(mutated("breaker", None), **ARGS)

    def test_a_config_file_cannot_claim_a_profile_origin_by_accident_but_paper_and_replay_still_take_files(self):
        from crypto_stat_arb.session import ExecutionMode, TradingSession
        plain, _ = core.load_config(PLATFORM / "config" / "perp_research.json")
        TradingSession(plain, execution_mode=ExecutionMode.INTERNAL_SIMULATION)
        TradingSession(plain, execution_mode=ExecutionMode.INTERNAL_PAPER, live_data=True)


if __name__ == "__main__":
    unittest.main()
