"""Startup refuses the physically impossible colocated_both latency preset outside a labelled sensitivity run (owner decision
2026-11-02). legacy_v0 (the frozen comparison baseline) is exempt; every other profile defaults to singapore."""
import copy
import unittest

from new_backtest.profiles import PROFILES_DIR, ProfileError, apply_profile, load_profile

KW = dict(data_root="unused", pair="SOLUSDT", start_date="2022-01-13", end_date="2022-01-13")
NON_LEGACY = ("perp_perp_maker_taker_v1", "spot_spot_maker_taker_v1", "spot_perp_basis_v1")


class LatencyPresetGuardTests(unittest.TestCase):
    def test_every_non_legacy_profile_defaults_to_singapore(self):
        for name in NON_LEGACY:
            doc = load_profile(PROFILES_DIR / f"{name}.yaml")
            self.assertEqual(doc["latency_preset"], "singapore", name)
            self.assertEqual(doc["timing"]["from_latency_preset"], "singapore", name)
            cfg = apply_profile(doc, **KW)
            self.assertEqual(cfg.latency_preset, "singapore")
            self.assertIsNone(cfg.release_identity["latency_sensitivity"])

    def test_colocated_both_is_refused_through_the_yaml_the_override_or_the_timing_model(self):
        doc = load_profile(PROFILES_DIR / "perp_perp_maker_taker_v1.yaml")
        yaml_both = copy.deepcopy(doc); yaml_both["latency_preset"] = "colocated_both"; yaml_both["timing"]["from_latency_preset"] = "colocated_both"
        timing_only = copy.deepcopy(doc); timing_only["timing"]["from_latency_preset"] = "colocated_both"
        for d, kw in ((yaml_both, {}), (doc, {"latency_preset": "colocated_both"}), (timing_only, {})):
            with self.assertRaisesRegex(ProfileError, "physically impossible"):
                apply_profile(copy.deepcopy(d), **KW, **kw)
        with self.assertRaises(ProfileError):                                  # an empty label is not a label
            apply_profile(copy.deepcopy(yaml_both), **KW, latency_sensitivity="  ")

    def test_a_labelled_sensitivity_run_is_allowed_and_recorded(self):
        doc = load_profile(PROFILES_DIR / "perp_perp_maker_taker_v1.yaml")
        doc["latency_preset"] = "colocated_both"; doc["timing"]["from_latency_preset"] = "colocated_both"
        cfg = apply_profile(doc, **KW, latency_sensitivity="upper bound: zero-distance host")
        self.assertEqual(cfg.latency_preset, "colocated_both")
        self.assertEqual(cfg.release_identity["latency_sensitivity"], "upper bound: zero-distance host")

    def test_legacy_v0_keeps_its_historical_preset(self):
        cfg = apply_profile(load_profile(PROFILES_DIR / "legacy_v0.yaml"), **KW)
        self.assertEqual(cfg.latency_preset, "colocated_both")


if __name__ == "__main__":
    unittest.main()
