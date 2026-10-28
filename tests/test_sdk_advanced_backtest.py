from __future__ import annotations

import gzip
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.config import load_config
from crypto_stat_arb.sdk import (
    STRATEGY_SDK_VERSION,
    AdvancedBacktestSpec,
    BacktestAPI,
    CrossVenueSDKStrategy,
    RuntimeMode,
    StrategyRuntime,
)


ROOT = Path(__file__).resolve().parents[1]


def write_snapshot(
    root: Path, venue: str, pair: str, day: str, *,
    timestamp: float, local_ts: float, bid: float, ask: float,
) -> None:
    directory = root / venue / "book_snapshots" / f"pair={pair}" / f"date={day}"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "exchange": venue,
        "timestamp": timestamp,
        "local_ts": local_ts,
        "best_bid": bid,
        "best_ask": ask,
        "bids": [[bid, 10.0], [bid - 0.1, 10.0]],
        "asks": [[ask, 10.0], [ask + 0.1, 10.0]],
    }
    with gzip.open(directory / "snapshot.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


class AdvancedSDKBacktestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.day = "2026-09-13"
        self.pair = "BTCUSDT"
        write_snapshot(
            self.root, "bybit", self.pair, self.day,
            timestamp=1.0, local_ts=1.001, bid=59_990.0, ask=60_000.0,
        )
        write_snapshot(
            self.root, "binance", self.pair, self.day,
            timestamp=1.0, local_ts=1.002, bid=61_200.0, ask=61_210.0,
        )

    def tearDown(self):
        self.temp.cleanup()

    def spec(self, output: str = "single") -> AdvancedBacktestSpec:
        return AdvancedBacktestSpec(
            pair=self.pair,
            data_root=self.root,
            start_date=self.day,
            end_date=self.day,
            latency_preset="colocated_both",
            min_net_edge_bps=1.0,
            max_clip_notional=10_000.0,
            block_on_toxic=False,
            output_dir=self.root / output,
            allow_rejected_data="synthetic test fixture, too small for the quality gate by design",
        )

    def test_sdk_runs_attached_latency_depth_and_fill_backtester(self):
        result = BacktestAPI(RuntimeMode.BACKTEST).run(self.spec())
        self.assertEqual(STRATEGY_SDK_VERSION, "1.2.0")
        self.assertEqual(result["events_processed"], 2)
        self.assertGreater(result["signals_generated"], 0)
        self.assertGreater(result["orders_filled"], 0)
        self.assertIn("bybit", result["latency_report"])
        self.assertIn("binance", result["book_stats"])
        self.assertAlmostEqual(result["net_base_position"], 0.0)
        self.assertTrue((self.root / "single" / "trades.jsonl").exists())

    def test_context_exposes_backtest_api_and_non_backtest_modes_fail_closed(self):
        config, _ = load_config(ROOT / "config/research.json")
        runtime = StrategyRuntime(
            config, CrossVenueSDKStrategy(config), mode=RuntimeMode.BACKTEST
        )
        self.assertEqual(runtime.context.backtest.run(self.spec())["events_processed"], 2)
        with self.assertRaisesRegex(RuntimeError, "only in BACKTEST mode"):
            BacktestAPI(RuntimeMode.PAPER).run(self.spec("paper"))

    def test_batch_api_uses_same_engine(self):
        result = BacktestAPI().run_batch(self.spec("batch"), [self.pair])
        self.assertEqual(result["pairs_run"], 1)
        self.assertEqual(result["pairs_skipped"], 0)
        self.assertGreater(result["total_signals"], 0)


if __name__ == "__main__":
    unittest.main()
