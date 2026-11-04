"""Perp fault presets, conditional scenarios and the perp calibration wiring (lifecycle task 2, 6, 7)."""
from __future__ import annotations

import copy
import dataclasses
import json
import unittest
import warnings
from pathlib import Path

from new_backtest import fault_presets as FP
from new_backtest import profiles
from new_backtest.hedge_faults import HedgeFaultConfig
from new_backtest.postonly import AckLatencySource

from test_maker_order_lifetime import BINANCE, BYBIT, Harness
from test_wp6_risk_recovery import cycles

warnings.simplefilter("ignore")
ROOT = Path(__file__).resolve().parents[1]
MK, HG, FM = ROOT / "config/lifecycle_calibration_perp_bybit.json", ROOT / "config/lifecycle_calibration_perp_binance.json", ROOT / "config/fast_move_thresholds.json"
KW = dict(maker_calibration=MK, hedge_calibration=HG, fast_move_path=FM, day="2022-01-13")
SOURCES = {"MEASURED_TESTNET_MAC", "MEASURED_2022_DATA", "RULE_OF_THREE_BOUND", "SCENARIO_CHOICE", "CARRIED_FROM_29_OCT", "PROFILE"}


class CalibrationFileTests(unittest.TestCase):
    def test_files_are_perp_versioned_hashed_and_list_their_source_runs(self):
        for p, venue in ((MK, "bybit"), (HG, "binance")):
            d = json.loads(p.read_text())
            self.assertEqual((d["product"], d["venue"], d["schema"]), ("perp", venue, "lifecycle_calibration_perp/v1"))
            body = {k: v for k, v in d.items() if k != "meta"}
            import hashlib
            self.assertEqual(d["meta"]["content_sha256"], hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest())
            self.assertTrue(d["provenance"]["source_runs"])
            self.assertIn("NO ORDER-EVENT DATA", d["provenance"]["mainnet"])
            self.assertEqual(sorted(d["networks"]), ["testnet"])            # no mainnet table exists

    def test_percentiles_are_withheld_below_the_sample_gates(self):
        d = json.loads(MK.read_text())["networks"]["testnet"]
        for name, m in d["latency"].items():
            if m["n"] < 30:
                self.assertIsNone(m.get("p50"), name)
            if m["n"] < 100:
                self.assertIsNone(m.get("p99"), name)
        self.assertIsNone(d["fill_report_delay"]["report_delay_fill_raw_ms"]["p50"])      # 28 fills < 30


class ProfileCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.doc = profiles.load_profile(ROOT / "profiles/perp_perp_maker_taker_v1.yaml")
        self.kw = dict(data_root="unused", pair="SOLUSDT", start_date="2022-01-13", end_date="2022-01-13")

    def test_the_perp_profile_uses_the_perp_calibration_and_records_its_hash(self):
        cfg = profiles.apply_profile(self.doc, **self.kw)
        self.assertEqual(cfg.ack_latency.source, "empirical")
        self.assertTrue(cfg.ack_latency.calibration_path.endswith("lifecycle_calibration_perp_bybit.json"))
        ident = cfg.release_identity["calibration"]
        self.assertEqual((ident["product"], ident["venue"]), ("perp", "bybit"))
        self.assertEqual(len(ident["file_sha256"]), 64)
        self.assertEqual(ident["content_sha256"], json.loads(MK.read_text())["meta"]["content_sha256"])
        self.assertGreater(AckLatencySource(cfg.ack_latency).sample_s(), 0.0)

    def test_a_spot_calibration_is_refused_by_the_perp_profile(self):
        doc = copy.deepcopy(self.doc)
        doc["calibration"]["file"] = "config/lifecycle_calibration_bybit.json"
        with self.assertRaisesRegex(profiles.ProfileError, "measured product 'spot'"):
            profiles.apply_profile(doc, **self.kw)

    def test_a_file_edited_after_it_was_written_is_refused(self):
        import tempfile
        d = json.loads(MK.read_text())
        d["networks"]["testnet"]["orders"] += 1
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "edited.json"
            p.write_text(json.dumps(d))
            doc = copy.deepcopy(self.doc)
            doc["calibration"]["file"] = str(p)
            with self.assertRaisesRegex(profiles.ProfileError, "content hash"):
                profiles.apply_profile(doc, **self.kw)

    def test_the_wrong_venue_is_refused(self):
        doc = copy.deepcopy(self.doc)
        doc["calibration"]["file"] = "config/lifecycle_calibration_perp_binance.json"
        with self.assertRaisesRegex(profiles.ProfileError, "venue"):
            profiles.apply_profile(doc, **self.kw)

    def test_postonly_model_is_mandatory_in_a_perp_profile_but_not_in_legacy(self):
        doc = copy.deepcopy(self.doc)
        doc["postonly_reject_model"] = "off"
        self.assertTrue(profiles.check_mandatory(doc))
        legacy = profiles.load_profile(ROOT / "profiles/legacy_v0.yaml")
        self.assertEqual(profiles.check_mandatory(legacy), [])


class PresetTests(unittest.TestCase):
    def test_every_parameter_cites_a_known_source(self):
        for name in FP.PERP_PRESETS:
            p = FP.perp_preset(name, seed=1, **KW)
            self.assertTrue(p["sources"])
            for k, v in p["sources"].items():
                self.assertIn(v["source"], SOURCES, (name, k))

    def test_clean_and_clean_managed_are_the_same_configuration(self):
        a, b = FP.perp_preset("clean", seed=3, **KW), FP.perp_preset("clean_managed", seed=3, **KW)
        fa, fb = dict(a["faults"]), dict(b["faults"])
        fa.pop("label"), fb.pop("label")
        self.assertEqual(fa, fb)
        self.assertEqual(a["ack_latency"], b["ack_latency"])

    def test_realistic_injects_nothing_that_was_not_measured(self):
        f = HedgeFaultConfig(**FP.perp_preset("realistic", seed=1, **KW)["faults"])
        self.assertFalse(f.disconnects or f.outages or f.ack_unknown_prob or f.partial_prob or f.fast_move_reject_prob)
        self.assertEqual(f.reject_prob, 0.0)

    def test_stressed_hedge_reject_probability_is_applied_once(self):
        p = FP.perp_preset("stressed", seed=1, **KW)
        f = HedgeFaultConfig(**{**p["faults"], "response": "retry_unwind", "max_retries": 0})
        self.assertFalse(f.venue_reject_applies_to_hedges)
        self.assertAlmostEqual(f.reject_prob, 3 / 129)
        self.assertEqual(dict(f.reject_prob_by_venue), {BYBIT: 3 / 99})
        h = Harness(faults=f)
        cycles(h, 60)
        self.assertEqual(h.bt.hedge["hedges_rejected_by_venue_prob"], 0)            # the by-venue rate never touched a hedge

    def test_stressed_fast_move_threshold_is_the_measured_p99_of_the_day(self):
        for day, p99 in (("2022-01-05", 3.57), ("2022-01-13", 5.73)):
            self.assertEqual(FP.perp_preset("stressed", seed=1, **{**KW, "day": day})["faults"]["fast_move_bps"], p99)

    def test_the_seed_changes_the_latency_stream_only(self):
        a = FP.perp_preset("realistic", seed=1, **KW)["ack_latency"]
        b = FP.perp_preset("realistic", seed=2, **KW)["ack_latency"]
        self.assertNotEqual(a.seed, b.seed)
        self.assertEqual(dataclasses.replace(a, seed=0), dataclasses.replace(b, seed=0))


class ConditionalScenarioTests(unittest.TestCase):
    def test_all_twelve_are_valid_fault_configs(self):
        self.assertEqual(len(FP.CONDITIONAL), 12)
        for n in FP.CONDITIONAL:
            HedgeFaultConfig(**FP.conditional_scenario(n, day="2022-01-05", fast_move_path=FM))

    def test_a_maker_venue_outage_goes_dark_on_the_maker_venue_not_the_hedge_venue(self):
        for scen, dark in (("outage_bybit_30s", BYBIT), ("outage_binance_30s", BINANCE)):
            h = Harness(faults=HedgeFaultConfig(response="retry_unwind", max_retries=0, **FP.conditional_scenario(scen, day="2022-01-13", fast_move_path=FM)))
            cycles(h, 1)
            self.assertEqual({o.venue for o in h.bt._dyn_outages}, {dark}, scen)

    def test_lost_ack_scope_hedge_never_makes_a_passive_order_unknown(self):
        h = Harness(faults=HedgeFaultConfig(response="retry_unwind", max_retries=0, **FP.conditional_scenario("lost_ack_hedge", day="2022-01-13", fast_move_path=FM)))
        cycles(h, 5)
        self.assertEqual(len(h.bt._maker_unknown_until), 0)
        self.assertGreater(h.bt.hedge["unknown_orders"], 0)


if __name__ == "__main__":
    unittest.main()
