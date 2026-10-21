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
    _aggregate_fold_results,
    _date_to_epoch,
    _days_in_window,
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


# ── Censoring fix ─────────────────────────────────────────────────────────────

class TestCensoringFix(unittest.TestCase):
    """Cancelled orders must NOT generate fill-label records.

    Before the fix, _record_outcome() received end_ts=None for cancelled orders,
    so life_s was always None, the censoring guard was bypassed, and every horizon
    got a 'not filled' label — inflating the false-negative rate.

    After the fix, cancel() passes end_ts=now so life_s = now - placed_ts, and
    only horizons where h > life_s fire (order was still open when cancelled).
    A cancel at t+0.5s must produce zero labels for h=1, h=5, h=15 (all larger).
    """

    def test_cancel_at_half_second_produces_no_label_records(self):
        from new_backtest.fill_engine import MakerExecutor, MakerConfig
        from new_backtest.fill_model import EmpiricalFillModel
        from new_backtest.order_book import OrderBook
        from new_backtest.models import MarketState, LambdaBucket, Side

        model = EmpiricalFillModel()
        executor = MakerExecutor(model, MakerConfig())

        now = 1_000.0
        book = OrderBook("bybit", "BTCUSDT", delta_semantics=False)
        book._bids = {59_990.0: 1.0}
        book._asks = {60_000.0: 1.0}
        # Heaps must be populated too so level lookups work
        import heapq
        book._bid_heap = [-59_990.0]
        book._ask_heap = [60_000.0]
        book.last_update_ts = now

        state = MarketState(
            timestamp=now, best_bid=59_990.0, best_ask=60_000.0,
            bid_size=1.0, ask_size=1.0, mid=59_995.0,
            microprice=59_995.0, spread_bps=1.67, imbalance=0.0,
            top5_imbalance=0.0, depth_weighted_imbalance=0.0,
            spread_pct=0.000167, microprice_deviation_bps=0.0,
            microprice_drift_bps=0.0, short_vol=0.0002, short_vol_bps=2.0,
            trade_sign=0.1, impact_trade_sign=0.1, flow_toxicity=0.1,
            recent_trade_rate=5.0, recent_signed_volume=0.05,
            trade_burst_flag=False, trade_cluster_score=0.0,
            depth_collapse=0.0, liquidity_evaporation=0.0,
            cancel_burst=0.0, cancel_imbalance=0.0,
            replenish_imbalance=0.0, queue_fragility=0.0,
            book_hole=False, recent_sweep_flag=False, sweep_risk=False,
            top_depth=2.0, top_depth_baseline=3.0, total_depth=20.0,
            tick_size=0.1,
        )

        initial_count = len(model.order_event_log)

        order = executor.place(
            venue="bybit", pair="BTCUSDT", symbol="BTCUSDT",
            side=Side.BID, price=59_985.0, size=0.01,
            book=book, state=state, regime_bucket=LambdaBucket.CALM,
            now=now, active_ts=now, tick_size=0.1,
        )

        # Cancel 0.5 s later — all horizons (1, 5, 15) are still open
        executor.cancel(order.order_id, now=now + 0.5)

        # No new label records (all horizons > life_s=0.5 → censored out)
        new_count = len(model.order_event_log)
        self.assertEqual(new_count, initial_count,
                         "Cancelled orders must not generate fill-label records "
                         "when cancelled before the shortest horizon")


# ── Fill gate stub tests ───────────────────────────────────────────────────────

class TestFillGate(unittest.TestCase):
    """Verify min_fill_prob gate behaviour with stub models."""

    def _make_strategy(self, min_fill_prob: float, model=None):
        from new_backtest.arb_strategy import CrossVenueArbStrategy, ArbConfig, MAKER_TAKER
        from new_backtest.config import make_venues, BYBIT, BINANCE
        specs = make_venues("neutral")
        venue_specs = {BYBIT: specs[BYBIT], BINANCE: specs[BINANCE]}
        cfg = ArbConfig(
            mode=MAKER_TAKER,
            min_fill_prob=min_fill_prob,
            fill_gate_horizon_s=15,
        )
        return CrossVenueArbStrategy("BTCUSDT", venue_specs, cfg, fill_model_v1=model)

    def _make_books_and_states(self):
        """Produce books where Bybit has a wide spread so a passive bid derived
        from Binance's price lands inside it — needed for MAKER_TAKER signals.

        Bybit:   bid=59900  ask=60100   (spread ≈ 33bps, wide)
        Binance: bid=60010  ask=60020   (tight)
        max_bid = 60010 * (1 - 11.5bps) ≈ 59941 — inside Bybit's spread ✓
        """
        import heapq
        from new_backtest.order_book import OrderBook
        from new_backtest.venue_state import VenueState
        from new_backtest.config import BYBIT, BINANCE
        now = 1_000.0
        books = {}
        states = {}
        for venue, bid, ask in [(BYBIT, 59_900.0, 60_100.0), (BINANCE, 60_010.0, 60_020.0)]:
            ob = OrderBook(venue, "BTCUSDT", delta_semantics=False)
            ob._bids = {bid: 5.0, bid - 1: 5.0}
            ob._asks = {ask: 5.0, ask + 1: 5.0}
            ob._bid_heap = [-bid, -(bid - 1)]
            ob._ask_heap = [ask, ask + 1]
            heapq.heapify(ob._bid_heap)
            heapq.heapify(ob._ask_heap)
            # touch_age_s returns inf when conf dict is empty → stale_touch gate
            # fires.  Stamp both touches as confirmed at `now`.
            ob._bid_conf = {bid: now}
            ob._ask_conf = {ask: now}
            ob.last_update_ts = now
            ob.last_snapshot_ts = now
            books[venue] = ob
            states[venue] = VenueState(venue)
        return books, states, now

    def test_gate_disabled_by_default_no_model(self):
        """Without a model, min_fill_prob=0 should pass all signals through."""
        from new_backtest.arb_strategy import InventoryBook
        strat = self._make_strategy(min_fill_prob=0.0)
        books, states, now = self._make_books_and_states()
        inv = InventoryBook()
        inv.ensure(tuple(books.keys()))
        # Should evaluate without error (gate is disabled)
        _ = strat.evaluate(books, states, inv, now)
        self.assertEqual(strat.rejects.get("low_fill_prob", 0), 0)

    def test_gate_blocks_when_model_returns_zero(self):
        """A stub returning p=0 with min_fill_prob=0.5 must block all maker signals."""
        from new_backtest.fill_model_v1 import FillModelV1
        from new_backtest.arb_strategy import InventoryBook

        # Build a model that always predicts 0
        zero_model = FillModelV1(
            horizon_s=15,
            trained_until_ts=0.0,
            n_train=100,
            base_rate=0.5,
            feature_mean=[0.0] * 9,
            feature_scale=[1.0] * 9,
            coef=[0.0] * 9,
            intercept=-1000.0,  # sigmoid(-1000) ≈ 0
        )
        strat = self._make_strategy(min_fill_prob=0.5, model=zero_model)
        books, states, now = self._make_books_and_states()
        inv = InventoryBook()
        inv.ensure(tuple(books.keys()))

        sig = strat.evaluate(books, states, inv, now)
        self.assertIsNone(sig)
        self.assertGreater(strat.rejects["low_fill_prob"], 0)

    def test_gate_passes_when_model_returns_one(self):
        """A stub returning p≈1 with min_fill_prob=0.5 must not block maker signals."""
        from new_backtest.fill_model_v1 import FillModelV1
        from new_backtest.arb_strategy import InventoryBook

        one_model = FillModelV1(
            horizon_s=15,
            trained_until_ts=0.0,
            n_train=100,
            base_rate=0.5,
            feature_mean=[0.0] * 9,
            feature_scale=[1.0] * 9,
            coef=[0.0] * 9,
            intercept=1000.0,  # sigmoid(1000) ≈ 1
        )
        strat = self._make_strategy(min_fill_prob=0.5, model=one_model)
        books, states, now = self._make_books_and_states()
        inv = InventoryBook()
        inv.ensure(tuple(books.keys()))

        _ = strat.evaluate(books, states, inv, now)
        self.assertEqual(strat.rejects.get("low_fill_prob", 0), 0)

    def test_leakage_guard_raises_on_future_model(self):
        """backtester must raise ValueError if model trained_until_ts > start epoch."""
        import datetime as dt
        from new_backtest.fill_model_v1 import FillModelV1
        from new_backtest.backtester import ArbBacktestConfig, CrossVenueArbBacktester
        from new_backtest.arb_strategy import ArbConfig, MAKER_TAKER
        import tempfile, json, os

        model = FillModelV1(
            horizon_s=15, trained_until_ts=9_999_999_999.0, n_train=1,
            base_rate=0.5, feature_mean=[0.0]*9, feature_scale=[1.0]*9,
            coef=[0.0]*9, intercept=0.0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "model.json")
            model.save_json(path)
            cfg = ArbBacktestConfig(
                data_root=tmp, start_date="2026-01-01", end_date="2026-01-02",
                arb=ArbConfig(mode=MAKER_TAKER),
                fill_model_v1_path=path,
            )
            with self.assertRaises(ValueError):
                CrossVenueArbBacktester(cfg)


# ── WalkForwardFold.fit() horizon filter ──────────────────────────────────────

class TestWalkForwardFoldHorizonFilter(unittest.TestCase):
    """fit(horizon_s=h) must train only on records with that horizon."""

    def _make_fold(self, n_short: int = 60, n_long: int = 60) -> WalkForwardFold:
        """Build a fold with two horizon groups having different fill rates."""
        day_s = 86_400.0
        records = []
        # horizon=1: 20% fill rate (hard to fill quickly)
        for i in range(n_short):
            ts = float(i) * day_s / n_short
            records.append(FillLabelRecord(
                timestamp=ts, symbol="BTCUSDT", side="bid", horizon_s=1,
                filled=(i % 5 == 0),  # 20%
                relative_to_touch_ticks=0, queue_ahead=100.0, local_depth=500.0,
                local_depth_baseline=800.0, spread_pct=0.001, short_vol=0.0002,
                trade_count=5.0, trade_sign=0.1, regime_bucket="calm",
                terminal_reason="full_fill" if (i % 5 == 0) else "cancel",
            ))
        # horizon=15: 80% fill rate (easy over 15s)
        for i in range(n_long):
            ts = float(i) * day_s / n_long
            records.append(FillLabelRecord(
                timestamp=ts, symbol="BTCUSDT", side="bid", horizon_s=15,
                filled=(i % 5 != 0),  # 80%
                relative_to_touch_ticks=0, queue_ahead=100.0, local_depth=500.0,
                local_depth_baseline=800.0, spread_pct=0.001, short_vol=0.0002,
                trade_count=5.0, trade_sign=0.1, regime_bucket="calm",
                terminal_reason="full_fill" if (i % 5 != 0) else "cancel",
            ))
        return WalkForwardFold(
            fold_id=0,
            cal_start_ts=0.0,
            cal_end_ts=day_s,
            eval_end_ts=2 * day_s,
            cal_records=records,
            eval_records=records,
        )

    def test_fit_with_horizon_filter_uses_only_matching_records(self):
        """fit(horizon_s=1) trains only on horizon=1 records; fit(horizon_s=15) on horizon=15."""
        fold = self._make_fold()

        model_1s  = fold.fit(horizon_s=1)
        model_15s = fold.fit(horizon_s=15)

        self.assertIsNotNone(model_1s)
        self.assertIsNotNone(model_15s)
        self.assertEqual(model_1s._horizon_s, 1)
        self.assertEqual(model_15s._horizon_s, 15)

        # 1s model should predict low probability (20% fill rate)
        # 15s model should predict high probability (80% fill rate)
        # Compare base rates via intercept sign (intercept negative → low p)
        import numpy as np
        dummy = np.zeros((1, 9), dtype=np.float32)
        p_1s  = model_1s.predict_proba(model_1s._scaler.transform(dummy))[0, 1]
        p_15s = model_15s.predict_proba(model_15s._scaler.transform(dummy))[0, 1]
        self.assertLess(p_1s, p_15s,
                        "1s model should predict lower fill probability than 15s model")

    def test_fit_without_horizon_uses_all_records(self):
        """fit() without horizon_s keeps existing behaviour (mixes all horizons)."""
        fold = self._make_fold(n_short=60, n_long=60)
        model_mixed = fold.fit()
        self.assertIsNotNone(model_mixed)
        self.assertIsNone(model_mixed._horizon_s)

    def test_fit_unknown_horizon_returns_none(self):
        """fit(horizon_s=999) finds no records → returns None."""
        fold = self._make_fold()
        result = fold.fit(horizon_s=999)
        self.assertIsNone(result)

    def test_predict_filters_eval_records_by_horizon(self):
        """predict(model, horizon_s=h) scores only eval records with matching horizon."""
        import numpy as np
        fold = self._make_fold()
        model_1s = fold.fit(horizon_s=1)
        self.assertIsNotNone(model_1s)

        y_all, p_all = fold.predict(model_1s)
        y_1s, p_1s   = fold.predict(model_1s, horizon_s=1)
        y_15s, p_15s  = fold.predict(model_1s, horizon_s=15)

        # horizon-filtered result is a strict subset of the unfiltered result
        self.assertLessEqual(len(y_1s), len(y_all))
        self.assertLessEqual(len(y_15s), len(y_all))
        self.assertEqual(len(y_1s) + len(y_15s), len(y_all))

    def test_predict_unknown_horizon_returns_empty(self):
        """predict(model, horizon_s=999) finds no records → empty arrays."""
        import numpy as np
        fold = self._make_fold()
        model = fold.fit(horizon_s=1)
        y, p = fold.predict(model, horizon_s=999)
        self.assertEqual(len(y), 0)
        self.assertEqual(len(p), 0)


# ── Stage 3 helpers ───────────────────────────────────────────────────────────

class TestDaysInWindow(unittest.TestCase):
    """_days_in_window maps calendar days to timestamp windows."""

    DAYS = ["2026-09-01", "2026-09-02", "2026-09-03",
            "2026-09-04", "2026-09-05"]

    def test_all_days_in_wide_window(self):
        start = _date_to_epoch("2026-09-01")
        end   = _date_to_epoch("2026-09-06")
        result = _days_in_window(self.DAYS, start, end)
        self.assertEqual(result, self.DAYS)

    def test_single_day_window(self):
        start = _date_to_epoch("2026-09-03")
        end   = _date_to_epoch("2026-09-04")
        result = _days_in_window(self.DAYS, start, end)
        self.assertEqual(result, ["2026-09-03"])

    def test_empty_when_window_before_all_days(self):
        start = _date_to_epoch("2026-08-01")
        end   = _date_to_epoch("2026-08-31")
        self.assertEqual(_days_in_window(self.DAYS, start, end), [])

    def test_end_is_exclusive(self):
        # end == epoch("2026-09-03") excludes that day
        start = _date_to_epoch("2026-09-01")
        end   = _date_to_epoch("2026-09-03")
        result = _days_in_window(self.DAYS, start, end)
        self.assertNotIn("2026-09-03", result)
        self.assertIn("2026-09-01", result)
        self.assertIn("2026-09-02", result)


class TestAggregateFoldResults(unittest.TestCase):
    """_aggregate_fold_results sums additive metrics, weights ratios."""

    def _fold(self, pnl, signals_completed, capture):
        return {
            "realized_pnl": pnl,
            "gross_pnl": pnl * 1.1,
            "fees_paid": abs(pnl) * 0.1,
            "unhedged_loss": 0.0,
            "traded_notional": 100_000.0,
            "signals_generated": 200,
            "orders_sent": 180,
            "orders_filled": 120,
            "orders_missed": 60,
            "orders_cancelled": 0,
            "signals_completed": signals_completed,
            "signals_unhedged": 5,
            "edge_capture_ratio": capture,
            "mean_realized_net_edge_bps": capture * 2.0,
            "fill_log_path": "/tmp/fill_events.jsonl",
            "start_date": "2026-09-01",
            "end_date": "2026-09-07",
            "strategy_report": {"reject_low_fill_prob": 10, "reject_stale_book": 5},
        }

    def test_additive_sums(self):
        folds = [self._fold(100.0, 50, 0.8), self._fold(200.0, 100, 0.6)]
        combined = _aggregate_fold_results(folds)
        self.assertAlmostEqual(combined["realized_pnl"], 300.0)
        self.assertEqual(combined["orders_filled"], 240)
        self.assertEqual(combined["signals_completed"], 150)

    def test_weighted_capture(self):
        # fold0: signals_completed=50, capture=1.0
        # fold1: signals_completed=100, capture=0.0
        # weighted avg = (50*1.0 + 100*0.0) / 150 = 1/3
        folds = [self._fold(100.0, 50, 1.0), self._fold(0.0, 100, 0.0)]
        combined = _aggregate_fold_results(folds)
        self.assertAlmostEqual(combined["edge_capture_ratio"], 50/150, places=6)

    def test_strategy_report_merged(self):
        folds = [self._fold(0.0, 10, 0.5), self._fold(0.0, 10, 0.5)]
        combined = _aggregate_fold_results(folds)
        self.assertEqual(combined["strategy_report"]["reject_low_fill_prob"], 20)
        self.assertEqual(combined["strategy_report"]["reject_stale_book"], 10)

    def test_fold_count_recorded(self):
        folds = [self._fold(0.0, 1, 0.0)] * 3
        combined = _aggregate_fold_results(folds)
        self.assertEqual(combined["wf_fold_count"], 3)

    def test_empty_input_returns_empty(self):
        self.assertEqual(_aggregate_fold_results([]), {})

    def test_zero_signals_completed_gives_zero_capture(self):
        folds = [self._fold(0.0, 0, 0.9)]
        combined = _aggregate_fold_results(folds)
        self.assertEqual(combined["edge_capture_ratio"], 0.0)


class TestWalkForwardBacktest(unittest.TestCase):
    """Integration: wf_window_days path in run_pair doesn't crash."""

    PAIR = "BTCUSDT"
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

    def test_wf_path_runs_without_exception(self):
        """wf_window_days > 0 in TAKER_TAKER: cold pass runs; no folds (no fill log)."""
        out = Path(self.tmp.name) / "out_wf"
        outcome = run_pair(
            self.PAIR, self._base_config(), out,
            two_pass=False,
            wf_window_days=2, wf_step_days=1,
        )
        # In TAKER_TAKER there are no passive orders → no fill_log_path from cold pass
        # → status should be "ok" (cold pass ran) or "error" (no log / no folds), never an exception
        self.assertIn(outcome.status, ("ok", "error"))

    def test_wf_supersedes_calibration_days(self):
        """When both wf_window_days>0 and calibration_days>0, wf path runs first."""
        out = Path(self.tmp.name) / "out_wf_cal"
        outcome = run_pair(
            self.PAIR, self._base_config(), out,
            two_pass=False,
            calibration_days=2,
            wf_window_days=2, wf_step_days=1,
        )
        # Detail from wf path contains "wf" or "wf_fold" not "cal="
        # (It may be "error" if no folds, but not the static-split path)
        if outcome.status == "ok":
            self.assertIn("wf", outcome.detail)
            self.assertNotIn("cal=", outcome.detail)

    def test_wf_not_taken_when_zero(self):
        """wf_window_days=0 falls through to calibration_days branch."""
        out = Path(self.tmp.name) / "out_no_wf"
        outcome = run_pair(
            self.PAIR, self._base_config(), out,
            two_pass=True,
            calibration_days=2, wf_window_days=0,
        )
        self.assertEqual(outcome.status, "ok")
        self.assertIn("cal=", outcome.detail)

    def test_run_batch_wf_params_accepted(self):
        """run_batch accepts wf_window_days / wf_step_days without error."""
        out = Path(self.tmp.name) / "batch_wf"
        summary = run_batch(
            [self.PAIR], self._base_config(), str(out),
            two_pass=False, verbose=False,
            wf_window_days=2, wf_step_days=1,
        )
        self.assertEqual(summary.pairs_requested, 1)


if __name__ == "__main__":
    unittest.main()
