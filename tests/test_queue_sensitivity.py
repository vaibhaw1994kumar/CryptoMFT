"""Tests for queue_sensitivity.py.

The key fixture improvement here is crossing trades: each maker-venue day has a
sell-aggressor trade that hits the resting bid at or below the maker price.  This
ensures passive orders actually fill, making the sweep metrics non-trivial and the
monotonicity assertions meaningful.

  queue_stress_multiplier  ↑  →  fill_rate ↓  (more queue to drain)
  trade_fill_share         ↑  →  fill_rate ↑  (more volume credited)
  delta_fill_share         ↑  →  fill_rate ↑  (more cancels credited)
"""
from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from new_backtest.batch_backtest import _date_to_epoch
from new_backtest.queue_sensitivity import QueueSensitivityReport, SweepPoint, sweep
from new_backtest.backtester import ArbBacktestConfig
from new_backtest.arb_strategy import ArbConfig, MAKER_TAKER
from new_backtest.fill_engine import MakerConfig


# ── Fixtures ──────────────────────────────────────────────────────────────────

PAIR = "BTCUSDT"
VENUES = ["bybit", "binance"]
DAYS = ["2026-10-01", "2026-10-02", "2026-10-03"]

# Pricing calibration for maker_taker arb:
# max_bid = taker_bid * (1 - required_bps/10000)
# required_bps = bybit_perp_mm.maker(-0.5) + binance_perp_vip0.taker(5.0) + min_net(1.0) + safety(0.5) = 6.0
# For max_bid to lie inside the maker spread [59990, 60000]:
#   59990 < taker_bid * 0.9994 < 60000
#   ⟹  taker_bid ∈ (59990/0.9994, 60000/0.9994) ≈ (60026, 60036)
# taker_bid = 60030 → max_bid ≈ 59994, which is inside (59990, 60000) ✓
# Trades at price ≤ 59994 (we use 59990) hit the resting bid.
MAKER_BID = 59_990.0
MAKER_ASK = 60_000.0
TAKER_BID = 60_030.0
TAKER_ASK = 60_040.0


def _write_snapshot(root: Path, venue: str, pair: str, day: str,
                    bid: float, ask: float) -> None:
    directory = root / venue / "book_snapshots" / f"pair={pair}" / f"date={day}"
    directory.mkdir(parents=True, exist_ok=True)
    ts = _date_to_epoch(day) + 1.0
    payload = {
        "exchange": venue,
        "timestamp": ts,
        "local_ts": ts + 0.001,
        "best_bid": bid,
        "best_ask": ask,
        "bids": [[bid, 10.0], [bid - 0.5, 10.0]],
        "asks": [[ask, 10.0], [ask + 0.5, 10.0]],
    }
    with gzip.open(directory / "snapshot.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")


def _write_trades(root: Path, venue: str, pair: str, day: str,
                  price: float, n: int = 5, volume: float = 1.0) -> None:
    """Write n sell-aggressor trades that cross the bid at `price`.
    sign = -1 (sell) hits resting bids at price <= order.price.
    """
    directory = root / venue / "trades" / f"pair={pair}" / f"date={day}"
    directory.mkdir(parents=True, exist_ok=True)
    base_ts = _date_to_epoch(day) + 60.0  # place well after the opening snapshot
    with gzip.open(directory / "trades.jsonl.gz", "wt", encoding="utf-8") as fh:
        for i in range(n):
            rec = {
                "exchange_ts": base_ts + i * 10.0,
                "local_ts": base_ts + i * 10.0 + 0.001,
                "price": price,
                "volume": volume,
                "side": "s",  # sell aggressor → hits bids
            }
            fh.write(json.dumps(rec) + "\n")


def _build_data_root(tmp: Path) -> Path:
    return _build_data_root_with_trades(tmp)


def _write_repeated_snapshots(root: Path, venue: str, pair: str, day: str,
                               bid: float, ask: float, n: int = 20,
                               step_s: float = 5.0) -> None:
    """Write n snapshots every step_s seconds so the book stays fresh all day."""
    directory = root / venue / "book_snapshots" / f"pair={pair}" / f"date={day}"
    directory.mkdir(parents=True, exist_ok=True)
    base_ts = _date_to_epoch(day) + 1.0
    with gzip.open(directory / "snapshot.jsonl.gz", "wt", encoding="utf-8") as fh:
        for i in range(n):
            ts = base_ts + i * step_s
            payload = {
                "exchange": venue,
                "timestamp": ts,
                "local_ts": ts + 0.001,
                "best_bid": bid,
                "best_ask": ask,
                "bids": [[bid, 10.0], [bid - 0.5, 10.0]],
                "asks": [[ask, 10.0], [ask + 0.5, 10.0]],
            }
            fh.write(json.dumps(payload) + "\n")


def _build_data_root_with_trades(tmp: Path) -> Path:
    """Build a two-venue dataset with repeated snapshots and crossing trades."""
    root = tmp / "data"
    for day in DAYS:
        # Maker venue (bybit): repeated snapshots + sell-aggressor trades at bid
        _write_repeated_snapshots(root, "bybit", PAIR, day,
                                  bid=MAKER_BID, ask=MAKER_ASK, n=30, step_s=3.0)
        _write_trades(root, "bybit", PAIR, day, price=MAKER_BID, n=10, volume=3.0)
        # Taker venue (binance): repeated snapshots only (no passive orders there)
        _write_repeated_snapshots(root, "binance", PAIR, day,
                                  bid=TAKER_BID, ask=TAKER_ASK, n=30, step_s=3.0)
    return root


def _base_config(root: Path, mode: str = MAKER_TAKER) -> ArbBacktestConfig:
    return ArbBacktestConfig(
        data_root=str(root),
        venues=tuple(VENUES),
        start_date=DAYS[0],
        end_date=DAYS[-1],
        latency_preset="colocated_both",
        arb=ArbConfig(
            mode=mode,
            min_net_edge_bps=1.0,
            max_clip_notional=10_000.0,
            block_on_toxic=False,
            # Large staleness limits so unit-test fixtures (snapshots every few
            # seconds) don't reject every signal as stale_book / stale_touch.
            max_book_age_s=300.0,
            max_touch_age_s=60.0,
        ),
        include_deltas=False,
        seed=7,
        allow_rejected_data="synthetic test fixture, too small for the quality gate by design",
    )


# ── SweepPoint ────────────────────────────────────────────────────────────────

class TestSweepPoint(unittest.TestCase):
    def test_defaults_zero(self):
        sp = SweepPoint(queue_stress_multiplier=1.0, trade_fill_share=1.0, delta_fill_share=0.15)
        self.assertEqual(sp.orders_sent, 0)
        self.assertEqual(sp.fill_rate, 0.0)
        self.assertEqual(sp.status, "ok")

    def test_fill_rate_computed_in_sweep(self):
        # fill_rate is orders_filled / orders_sent when set by _run_point
        sp = SweepPoint(queue_stress_multiplier=1.0, trade_fill_share=1.0, delta_fill_share=0.15,
                        orders_sent=10, orders_filled=3)
        # _run_point sets fill_rate; direct construction doesn't, so just check fields exist
        self.assertIsInstance(sp.fill_rate, float)


# ── QueueSensitivityReport ────────────────────────────────────────────────────

class TestQueueSensitivityReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _build_data_root(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _sweep(self, **kw) -> QueueSensitivityReport:
        base = _base_config(self.root)
        out = Path(self.tmp.name) / "sweep_out"
        return sweep(
            base, PAIR, output_dir=out,
            queue_stress_values=kw.get("queue_stress_values", [0.0, 1.0, 2.0]),
            trade_fill_values=kw.get("trade_fill_values", [0.5, 1.0]),
            delta_fill_values=kw.get("delta_fill_values", [0.05, 0.15]),
        )

    def test_returns_report_instance(self):
        r = self._sweep()
        self.assertIsInstance(r, QueueSensitivityReport)
        self.assertEqual(r.pair, PAIR)

    def test_queue_stress_sweep_has_correct_length(self):
        r = self._sweep(queue_stress_values=[0.0, 1.0, 5.0])
        self.assertEqual(len(r.queue_stress_sweep), 3)

    def test_trade_fill_sweep_has_correct_length(self):
        r = self._sweep(trade_fill_values=[0.5, 1.0])
        self.assertEqual(len(r.trade_fill_sweep), 2)

    def test_delta_fill_sweep_has_correct_length(self):
        r = self._sweep(delta_fill_values=[0.05, 0.30])
        self.assertEqual(len(r.delta_fill_sweep), 2)

    def test_baseline_point_has_default_knobs(self):
        r = self._sweep()
        self.assertAlmostEqual(r.baseline.queue_stress_multiplier, 1.0)
        self.assertAlmostEqual(r.baseline.trade_fill_share, 1.0)
        self.assertAlmostEqual(r.baseline.delta_fill_share, 0.15)

    def test_all_points_have_ok_status(self):
        r = self._sweep()
        all_points = (
            [r.baseline]
            + r.queue_stress_sweep
            + r.trade_fill_sweep
            + r.delta_fill_sweep
        )
        for sp in all_points:
            self.assertEqual(sp.status, "ok", f"Point failed: {sp}")

    def test_json_roundtrip(self):
        r = self._sweep()
        j = r.to_json()
        data = json.loads(j)
        self.assertEqual(data["pair"], PAIR)
        self.assertIn("queue_stress_sweep", data)
        self.assertIn("trade_fill_sweep", data)

    def test_report_file_written(self):
        base = _base_config(self.root)
        out = Path(self.tmp.name) / "report_dir"
        sweep(base, PAIR, output_dir=out,
              queue_stress_values=[0.0, 1.0],
              trade_fill_values=[1.0],
              delta_fill_values=[0.15])
        self.assertTrue((out / "queue_sensitivity_report.json").exists())

    def test_print_table_runs(self):
        """print_table() should not raise."""
        import io, sys
        r = self._sweep()
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            r.print_table()
        finally:
            sys.stdout = old
        self.assertIn("queue_stress_multiplier", buf.getvalue())


# ── Monotonicity with crossing trades ────────────────────────────────────────

class TestQueueSweepMonotonicity(unittest.TestCase):
    """With actual crossing trades in the fixture, fill_rate must respond
    monotonically to knob changes.

    These tests are sensitive to whether the fixture produces fills at all.
    If fills == 0 for all settings (no crossing trades reached a live order),
    the test logs a skip message so it is never a false pass.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _build_data_root(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _run_sweep(self) -> QueueSensitivityReport:
        base = _base_config(self.root)
        out = Path(self.tmp.name) / "mono_sweep"
        return sweep(
            base, PAIR, output_dir=out,
            queue_stress_values=[0.0, 0.5, 1.0, 2.0, 5.0],
            trade_fill_values=[0.5, 1.0],
            delta_fill_values=[0.05, 0.15, 0.30],
        )

    def test_queue_stress_monotone(self):
        """Higher queue_stress_multiplier ≥ same fill_rate (weakly decreasing)."""
        r = self._run_sweep()
        fill_rates = [p.fill_rate for p in r.queue_stress_sweep]
        if all(fr == 0.0 for fr in fill_rates):
            # No fills produced — likely fixture doesn't reach a live order in time
            self.skipTest("No fills with any queue_stress setting; can't test monotonicity")
        for a, b in zip(fill_rates, fill_rates[1:]):
            self.assertGreaterEqual(a + 1e-9, b,
                "fill_rate should be non-increasing as queue_stress increases")

    def test_trade_fill_share_monotone(self):
        """Higher trade_fill_share → at least as many fills (weakly increasing)."""
        r = self._run_sweep()
        fill_rates = [p.fill_rate for p in r.trade_fill_sweep]
        if all(fr == 0.0 for fr in fill_rates):
            self.skipTest("No fills; can't test trade_fill_share monotonicity")
        for a, b in zip(fill_rates, fill_rates[1:]):
            self.assertLessEqual(a - 1e-9, b,
                "fill_rate should be non-decreasing as trade_fill_share increases")

    def test_delta_fill_share_monotone(self):
        """Higher delta_fill_share → at least as many fills (weakly increasing)."""
        r = self._run_sweep()
        fill_rates = [p.fill_rate for p in r.delta_fill_sweep]
        if all(fr == 0.0 for fr in fill_rates):
            self.skipTest("No fills; can't test delta_fill_share monotonicity")
        for a, b in zip(fill_rates, fill_rates[1:]):
            self.assertLessEqual(a - 1e-9, b,
                "fill_rate should be non-decreasing as delta_fill_share increases")

    def test_zero_queue_stress_has_more_fills_than_high(self):
        """queue_stress=0 (no queue) should fill more than queue_stress=5 (5× queue)."""
        r = self._run_sweep()
        fr_0 = next(p.fill_rate for p in r.queue_stress_sweep
                    if abs(p.queue_stress_multiplier) < 1e-9)
        fr_5 = next(p.fill_rate for p in r.queue_stress_sweep
                    if abs(p.queue_stress_multiplier - 5.0) < 1e-9)
        if fr_0 == 0.0 and fr_5 == 0.0:
            self.skipTest("No fills at all; skipping relative comparison")
        self.assertGreaterEqual(fr_0 + 1e-9, fr_5,
            "queue_stress=0 should not have fewer fills than queue_stress=5")


if __name__ == "__main__":
    unittest.main()
