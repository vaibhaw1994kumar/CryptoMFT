"""Unit tests for fill_label.py and batch_backtest.py calibration_days.

Covers:
- extract_labels: basic extraction, until_ts leakage guard, censoring
- WalkForwardCalibrator: fold generation, maturity filter, empty/thin log
- _date_to_epoch: round-trip conversion
- run_pair / run_batch with calibration_days > 0: walk-forward split,
  warm_start_until_ts wired correctly, falls back to two-pass when too few days
"""
from __future__ import annotations

import gzip
import json
import math
import tempfile
import unittest
from pathlib import Path

from new_backtest.fill_label import (
    FillLabelRecord,
    WalkForwardCalibrator,
    WalkForwardFold,
    extract_labels,
)
from new_backtest.batch_backtest import (
    _date_to_epoch,
    run_batch,
    run_pair,
)
from new_backtest.backtester import ArbBacktestConfig
from new_backtest.arb_strategy import ArbConfig, TAKER_TAKER


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_event(
    ts: float,
    horizon_s: int,
    filled: bool,
    terminal_reason: str = "full_fill",
    regime: str = "calm",
) -> dict:
    return {
        "timestamp": ts,
        "symbol": "BTCUSDT",
        "side": "bid",
        "relative_to_touch_ticks": 0,
        "queue_ahead": 100.0,
        "local_depth": 500.0,
        "local_depth_baseline": 800.0,
        "spread_pct": 0.001,
        "short_vol": 0.0002,
        "trade_count": 5.0,
        "trade_sign": 0.1,
        "regime_bucket": regime,
        "horizon_s": horizon_s,
        "filled": filled,
        "price_moved_through": False,
        "terminal_reason": terminal_reason,
    }


def _write_events(path: Path, events: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


def _write_snapshot(
    root: Path, venue: str, pair: str, day: str,
    bid: float = 59_990.0, ask: float = 60_000.0,
) -> None:
    directory = root / venue / "book_snapshots" / f"pair={pair}" / f"date={day}"
    directory.mkdir(parents=True, exist_ok=True)
    ts = _date_to_epoch(day) + 1.0
    payload = {
        "exchange": venue,
        "timestamp": ts,
        "local_ts": ts + 0.001,
        "best_bid": bid,
        "best_ask": ask,
        "bids": [[bid, 10.0], [bid - 0.1, 10.0]],
        "asks": [[ask, 10.0], [ask + 0.1, 10.0]],
    }
    with gzip.open(directory / "snapshot.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")


# ── extract_labels ────────────────────────────────────────────────────────────

class TestExtractLabels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "fill_events.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_basic_extraction(self):
        events = [
            _make_event(ts=1000.0, horizon_s=10, filled=True, terminal_reason="full_fill"),
            _make_event(ts=2000.0, horizon_s=30, filled=False, terminal_reason="cancel"),
        ]
        _write_events(self.log, events)
        records = extract_labels(self.log)
        self.assertEqual(len(records), 2)
        self.assertIsInstance(records[0], FillLabelRecord)
        self.assertTrue(records[0].filled)
        self.assertFalse(records[1].filled)

    def test_fields_mapped_correctly(self):
        _write_events(self.log, [_make_event(
            ts=5000.0, horizon_s=60, filled=True,
            terminal_reason="partial_then_cancel", regime="toxic",
        )])
        rec = extract_labels(self.log)[0]
        self.assertEqual(rec.timestamp, 5000.0)
        self.assertEqual(rec.horizon_s, 60)
        self.assertEqual(rec.regime_bucket, "toxic")
        self.assertEqual(rec.terminal_reason, "partial_then_cancel")
        self.assertAlmostEqual(rec.queue_ahead, 100.0)

    def test_until_ts_excludes_immature_labels(self):
        # Event at ts=1000, horizon=30 → matures at 1030
        # until_ts=1020 → label not yet mature → skip
        _write_events(self.log, [
            _make_event(ts=1000.0, horizon_s=30, filled=True),  # matures at 1030
            _make_event(ts=900.0, horizon_s=10, filled=False),   # matures at 910
        ])
        records = extract_labels(self.log, until_ts=1020.0)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].timestamp, 900.0)

    def test_until_ts_boundary_is_inclusive(self):
        # ts=1000, horizon=20 → matures at exactly 1020.
        # until_ts=1020 → label IS mature (matures at the boundary, not after).
        # The filter is ts + horizon_s > until_ts, so equality is kept.
        _write_events(self.log, [
            _make_event(ts=1000.0, horizon_s=20, filled=True),   # matures at 1020 → keep
            _make_event(ts=1000.0, horizon_s=21, filled=False),  # matures at 1021 → skip
        ])
        records = extract_labels(self.log, until_ts=1020.0)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].horizon_s, 20)

    def test_empty_log_returns_empty_list(self):
        self.log.write_text("", encoding="utf-8")
        self.assertEqual(extract_labels(self.log), [])

    def test_missing_log_returns_empty_list(self):
        self.assertEqual(extract_labels(Path(self.tmp.name) / "nonexistent.jsonl"), [])

    def test_comment_and_blank_lines_skipped(self):
        with self.log.open("w", encoding="utf-8") as fh:
            fh.write("# this is a comment\n")
            fh.write("\n")
            fh.write(json.dumps(_make_event(ts=1.0, horizon_s=5, filled=True)) + "\n")
        records = extract_labels(self.log)
        self.assertEqual(len(records), 1)

    def test_no_until_ts_returns_all_records(self):
        events = [_make_event(ts=float(i), horizon_s=10, filled=i % 2 == 0)
                  for i in range(10)]
        _write_events(self.log, events)
        records = extract_labels(self.log)
        self.assertEqual(len(records), 10)


# ── WalkForwardCalibrator ─────────────────────────────────────────────────────

class TestWalkForwardCalibrator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "fill_events.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _populate(self, n_events: int = 200, span_days: float = 70.0) -> None:
        """Write n_events uniformly distributed over span_days worth of seconds."""
        day_s = 86_400.0
        events = []
        for i in range(n_events):
            ts = i * (span_days * day_s / n_events)
            events.append(_make_event(
                ts=ts, horizon_s=10,
                filled=i % 3 != 0,  # ~66% fill rate
                terminal_reason="full_fill" if i % 3 != 0 else "cancel",
            ))
        _write_events(self.log, events)

    def test_yields_folds_for_sufficient_data(self):
        self._populate(n_events=300, span_days=90.0)
        cal = WalkForwardCalibrator(self.log, window_days=30, step_days=7, min_cal_records=10)
        folds = list(cal.folds())
        self.assertGreater(len(folds), 0)
        for fold in folds:
            self.assertIsInstance(fold, WalkForwardFold)
            self.assertGreaterEqual(len(fold.cal_records), 10)

    def test_fold_ids_are_sequential(self):
        self._populate(n_events=300, span_days=90.0)
        cal = WalkForwardCalibrator(self.log, window_days=30, step_days=7, min_cal_records=1)
        folds = list(cal.folds())
        ids = [f.fold_id for f in folds]
        self.assertEqual(ids, list(range(len(folds))))

    def test_cal_records_only_contain_matured_labels(self):
        """Cal records must have ts + horizon_s <= cal_end_ts."""
        self._populate(n_events=300, span_days=90.0)
        cal = WalkForwardCalibrator(self.log, window_days=30, step_days=7, min_cal_records=1)
        for fold in cal.folds():
            for r in fold.cal_records:
                self.assertLessEqual(
                    r.timestamp + r.horizon_s, fold.cal_end_ts + 1e-6,
                    msg=f"Label not matured at cal boundary in fold {fold.fold_id}",
                )

    def test_eval_records_are_within_eval_window(self):
        self._populate(n_events=300, span_days=90.0)
        cal = WalkForwardCalibrator(self.log, window_days=30, step_days=7, min_cal_records=1)
        for fold in cal.folds():
            for r in fold.eval_records:
                self.assertGreaterEqual(r.timestamp, fold.cal_end_ts - 1e-6)
                self.assertLess(r.timestamp, fold.eval_end_ts + 1e-6)

    def test_no_folds_for_empty_log(self):
        self.log.write_text("", encoding="utf-8")
        folds = list(WalkForwardCalibrator(self.log, window_days=30).folds())
        self.assertEqual(folds, [])

    def test_no_folds_when_data_too_short(self):
        # Only 5 days of data, window=30 → no complete fold
        self._populate(n_events=50, span_days=5.0)
        folds = list(WalkForwardCalibrator(self.log, window_days=30, step_days=7).folds())
        self.assertEqual(folds, [])

    def test_min_cal_records_filters_sparse_folds(self):
        self._populate(n_events=100, span_days=90.0)
        # Very high min_cal_records → no fold qualifies
        folds = list(WalkForwardCalibrator(
            self.log, window_days=30, step_days=7, min_cal_records=10_000
        ).folds())
        self.assertEqual(folds, [])

    def test_feature_matrix_shape(self):
        self._populate(n_events=200, span_days=90.0)
        cal = WalkForwardCalibrator(self.log, window_days=30, step_days=7, min_cal_records=5)
        folds = list(cal.folds())
        self.assertTrue(folds)
        fold = folds[0]
        import numpy as np
        X, y = fold.feature_matrix(fold.cal_records)
        self.assertEqual(X.shape[1], 9)  # FEATURE_DIM = 9
        self.assertEqual(len(X), len(fold.cal_records))
        self.assertEqual(len(y), len(fold.cal_records))
        self.assertTrue(set(y).issubset({0, 1}))


# ── _date_to_epoch ────────────────────────────────────────────────────────────

class TestDateToEpoch(unittest.TestCase):
    def test_known_date(self):
        import datetime as dt
        epoch = _date_to_epoch("2026-01-01")
        expected = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp()
        self.assertAlmostEqual(epoch, expected, places=3)

    def test_different_dates_produce_different_epochs(self):
        e1 = _date_to_epoch("2026-04-13")
        e2 = _date_to_epoch("2026-04-14")
        self.assertAlmostEqual(e2 - e1, 86_400.0, places=0)

    def test_epoch_increases_with_date(self):
        dates = ["2026-01-01", "2026-06-15", "2026-12-31"]
        epochs = [_date_to_epoch(d) for d in dates]
        self.assertEqual(epochs, sorted(epochs))


# ── run_pair / run_batch with calibration_days ────────────────────────────────

class TestCalibrationDaysBacktest(unittest.TestCase):
    """Integration-level tests: calibration_days wires correctly through run_pair."""

    PAIR = "BTCUSDT"
    # Calendar days to write snapshots for
    DAYS = [
        "2026-09-01", "2026-09-02", "2026-09-03",
        "2026-09-04", "2026-09-05",
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "data"
        for day in self.DAYS:
            _write_snapshot(self.root, "bybit", self.PAIR, day,
                            bid=59_990.0, ask=60_000.0)
            _write_snapshot(self.root, "binance", self.PAIR, day,
                            bid=61_200.0, ask=61_210.0)

    def tearDown(self):
        self.tmp.cleanup()

    def _base_config(self) -> ArbBacktestConfig:
        return ArbBacktestConfig(
            data_root=self.root,
            venues=("bybit", "binance"),
            start_date=self.DAYS[0],
            end_date=self.DAYS[-1],
            latency_preset="colocated_both",
            arb=ArbConfig(
                mode=TAKER_TAKER,
                min_net_edge_bps=1.0,
                max_clip_notional=10_000.0,
                block_on_toxic=False,
            ),
            include_deltas=False,
        )

    def test_calibration_days_zero_uses_two_pass(self):
        """calibration_days=0 → standard pass1/pass2 structure."""
        out = Path(self.tmp.name) / "out_2pass"
        outcome = run_pair(
            self.PAIR, self._base_config(), out,
            two_pass=True, calibration_days=0,
        )
        self.assertEqual(outcome.status, "ok")
        self.assertIsNotNone(outcome.pass1)
        self.assertIsNotNone(outcome.pass2)
        # Two-pass: same date window for both passes
        self.assertEqual(outcome.pass1["start_date"], outcome.pass2["start_date"])

    def test_calibration_days_splits_window(self):
        """calibration_days=2 → cal on first 2 days, eval on remaining 3."""
        out = Path(self.tmp.name) / "out_cal"
        outcome = run_pair(
            self.PAIR, self._base_config(), out,
            two_pass=True, calibration_days=2,
        )
        self.assertEqual(outcome.status, "ok", msg=outcome.detail)
        # pass1 = calibration window, pass2 = eval window
        self.assertIsNotNone(outcome.pass1)
        self.assertIsNotNone(outcome.pass2)
        # Calibration window ends before eval window starts
        cal_end = outcome.pass1["end_date"]
        eval_start = outcome.pass2["start_date"]
        self.assertLess(cal_end, eval_start)

    def test_calibration_days_detail_contains_both_windows(self):
        out = Path(self.tmp.name) / "out_detail"
        outcome = run_pair(
            self.PAIR, self._base_config(), out,
            two_pass=True, calibration_days=2,
        )
        self.assertIn("cal=", outcome.detail)
        self.assertIn("eval=", outcome.detail)

    def test_calibration_days_fallback_when_too_few_days(self):
        """calibration_days >= available days → falls back to standard two-pass."""
        out = Path(self.tmp.name) / "out_fallback"
        # 5 days available; calibration_days=5 → not (5 > 5), falls back
        outcome = run_pair(
            self.PAIR, self._base_config(), out,
            two_pass=True, calibration_days=5,
        )
        self.assertEqual(outcome.status, "ok")
        # Falls back: both passes should cover the same window
        if outcome.pass1 and outcome.pass2:
            self.assertEqual(outcome.pass1["start_date"], outcome.pass2["start_date"])

    def test_calibration_days_in_run_batch(self):
        """run_batch passes calibration_days through to run_pair."""
        out = Path(self.tmp.name) / "batch_out"
        base = self._base_config()
        summary = run_batch(
            [self.PAIR], base, str(out),
            two_pass=True, verbose=False, calibration_days=2,
        )
        self.assertEqual(summary.pairs_run, 1)
        self.assertEqual(summary.pairs_skipped, 0)

    def test_result_has_new_a10_fields(self):
        """ArbBacktestResult exposes open_position_value and ending_equity."""
        out = Path(self.tmp.name) / "out_a10"
        outcome = run_pair(
            self.PAIR, self._base_config(), out,
            two_pass=False, calibration_days=0,
        )
        self.assertEqual(outcome.status, "ok")
        r = outcome.pass1
        self.assertIn("open_position_value", r)
        self.assertIn("ending_equity", r)
        # ending_equity == mark_to_market_pnl (legacy alias)
        self.assertAlmostEqual(r["ending_equity"], r["mark_to_market_pnl"], places=6)


if __name__ == "__main__":
    unittest.main()
