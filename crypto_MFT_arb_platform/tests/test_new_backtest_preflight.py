"""11a: the new_backtest data-quality preflight blocks rejected data unless a justification is recorded."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from new_backtest import quality_preflight
from new_backtest.arb_strategy import ArbConfig, TAKER_TAKER
from new_backtest.backtester import ArbBacktestConfig, CrossVenueArbBacktester
from new_backtest.batch_backtest import _clone
from test_fill_label_and_calibration import _write_snapshot

DAY = "2026-09-01"


class PreflightBlockingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "data"
        # One snapshot per venue: the quality gate rejects it (INSUFFICIENT_EVENTS).
        _write_snapshot(self.root, "bybit", "BTCUSDT", DAY, bid=59_990.0, ask=60_000.0)
        _write_snapshot(self.root, "binance", "BTCUSDT", DAY, bid=61_200.0, ask=61_210.0)

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, **overrides):
        return ArbBacktestConfig(
            data_root=str(self.root), venues=("bybit", "binance"), start_date=DAY, end_date=DAY,
            latency_preset="colocated_both", include_deltas=False,
            arb=ArbConfig(mode=TAKER_TAKER, min_net_edge_bps=1.0, max_clip_notional=10_000.0,
                          block_on_toxic=False),
            **overrides)

    def test_rejected_data_blocks_the_run_by_default(self):
        with self.assertRaisesRegex(RuntimeError, "Quality preflight CRITICAL for .*INSUFFICIENT_EVENTS"):
            CrossVenueArbBacktester(self.config()).run()

    def test_a_justification_runs_it_and_is_recorded_with_the_decisions(self):
        result = CrossVenueArbBacktester(self.config(allow_rejected_data="  fixture: one snapshot  ")).run()
        quality = result.data_quality
        self.assertFalse(quality["blocking"])
        self.assertEqual(quality["rejected_venues"], ["binance", "bybit"])
        self.assertEqual(quality["override"], {"justification": "fixture: one snapshot",
                                               "rejected_venues": ["binance", "bybit"]})
        for venue in ("binance", "bybit"):
            self.assertEqual(quality["preflight"][venue]["decision"], "REJECTED")
            self.assertIn("INSUFFICIENT_EVENTS", quality["preflight"][venue]["active_flag_codes"])

    def test_a_blank_justification_is_refused(self):
        with self.assertRaisesRegex(ValueError, "written justification"):
            CrossVenueArbBacktester(self.config(allow_rejected_data="   ")).run()

    def test_an_unavailable_quality_gate_blocks_instead_of_skipping(self):
        with mock.patch.object(quality_preflight, "QualityGate", None):
            with self.assertRaisesRegex(RuntimeError, "could not run"):
                quality_preflight.run_preflight(self.root, "bybit", "BTCUSDT", DAY, DAY, report_only=False)
            report = quality_preflight.run_preflight(self.root, "bybit", "BTCUSDT", DAY, DAY, report_only=True)
            self.assertTrue(report["skipped"])                 # report-only callers keep the old behaviour

    def test_batch_runs_carry_the_justification_into_every_pass(self):
        base = self.config(allow_rejected_data="fixture")
        cloned = _clone(base, pair="BTCUSDT", start=DAY, end=DAY, output_dir=str(self.root / "out"),
                        warm_start=None)
        self.assertEqual(cloned.allow_rejected_data, "fixture")

    def test_the_sdk_passes_the_justification_to_the_engine(self):
        from crypto_stat_arb.sdk import AdvancedBacktestSpec, BacktestAPI, RuntimeMode

        spec = AdvancedBacktestSpec(pair="BTCUSDT", data_root=self.root, start_date=DAY, end_date=DAY,
                                    allow_rejected_data="fixture")
        self.assertEqual(BacktestAPI(RuntimeMode.BACKTEST)._config(spec).allow_rejected_data, "fixture")
        blocking = AdvancedBacktestSpec(pair="BTCUSDT", data_root=self.root, start_date=DAY, end_date=DAY)
        self.assertIsNone(BacktestAPI(RuntimeMode.BACKTEST)._config(blocking).allow_rejected_data)

    def test_the_queue_sensitivity_sweep_carries_the_justification(self):
        from new_backtest import queue_sensitivity
        from new_backtest.fill_engine import MakerConfig

        seen = []

        def recording_run(cfg):
            seen.append(cfg.allow_rejected_data)
            raise RuntimeError("stop after capturing the config")

        with mock.patch.object(queue_sensitivity, "_run_once", recording_run):
            point = queue_sensitivity._run_point(self.config(allow_rejected_data="fixture"), "BTCUSDT",
                                                 self.root / "out", MakerConfig(), "p")
        self.assertEqual(seen, ["fixture"])
        self.assertEqual(point.status, "error")

if __name__ == "__main__":
    unittest.main()
