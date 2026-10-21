from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from crypto_stat_arb.models import (
    ChildOrder, EventType, MarketEvent, Opportunity, OrderState,
    ParentPlan, PlanState, Side,
)
from crypto_stat_arb.runner import run_backtest
from crypto_stat_arb.tca import BasicTCA


class BasicTCATests(unittest.TestCase):
    def test_signed_slippage_and_raw_lineage_are_reproducible(self):
        opportunity = Opportunity(
            "opp", "strategy", "BTC-USDT-SPOT", 2_000_000_000,
            "BINANCE", "BYBIT", Decimal("100"), Decimal("100"), Decimal("1"),
            Decimal("10"), Decimal("8"), ("event-1", "event-2"),
        )
        children = [
            ChildOrder(
                "buy", "parent", "opp", "BINANCE", "BTC-USDT-SPOT",
                Side.BUY, Decimal("2"), Decimal("100"), OrderState.FILLED,
                Decimal("2"), Decimal("101"), Decimal("0.2"),
            ),
            ChildOrder(
                "sell", "parent", "opp", "BYBIT", "BTC-USDT-SPOT",
                Side.SELL, Decimal("1"), Decimal("100"), OrderState.FILLED,
                Decimal("1"), Decimal("99"), Decimal("0.1"),
            ),
        ]
        plan = ParentPlan(
            "parent", opportunity, children, PlanState.CLOSED,
            terminal_outcome="FILLED", net_pnl=Decimal("-3.3"),
        )
        trace = [
            {"record_type": "ORDER_REQUEST", "request": {
                "action": "SUBMIT", "order_id": order.order_id,
                "order_type": "LIMIT",
            }} for order in children
        ]
        oms = SimpleNamespace(
            plans={"parent": plan}, router=SimpleNamespace(trace=trace),
            residual_policy=SimpleNamespace(max_residual_base=Decimal("0.002")),
        )
        events = [
            MarketEvent(
                "event-1", EventType.BOOK_SNAPSHOT, "BINANCE", "BTC-USDT-SPOT",
                1, 1, 1, raw_ref="raw://binance/1",
            ),
            MarketEvent(
                "event-2", EventType.BOOK_SNAPSHOT, "BYBIT", "BTC-USDT-SPOT",
                2, 2, 1, raw_ref="raw://bybit/1",
            ),
        ]
        first = BasicTCA.build(
            oms=oms, events=events, correlation_id="session",
            source_path="canonical.jsonl", source_sha256="abc",
        )
        second = BasicTCA.build(
            oms=oms, events=events, correlation_id="session",
            source_path="canonical.jsonl", source_sha256="abc",
        )
        self.assertEqual(first, second)
        self.assertEqual(first["summary"]["slippage_quote"], "3")
        self.assertEqual(first["summary"]["weighted_slippage_bps"], "100.00")
        self.assertEqual(first["summary"]["fees"], "0.3")
        self.assertEqual(first["lineage"]["raw_reference_coverage"], 1.0)
        self.assertEqual(first["lineage"]["raw_refs"], [
            "raw://binance/1", "raw://bybit/1",
        ])
        self.assertEqual(first["lineage"]["source_event_raw_refs"]["event-1"],
                         "raw://binance/1")

    def test_published_sample_has_recomputable_weighted_slippage(self):
        root = Path(__file__).resolve().parents[1]
        sample = json.loads(
            (root / "data/samples/orders/basic_tca_report.json").read_text()
        )
        quote = Decimal(sample["summary"]["slippage_quote"])
        benchmark = Decimal(sample["summary"]["benchmark_notional"])
        expected = quote / benchmark * Decimal("10000") if benchmark else Decimal("0")
        self.assertEqual(expected, Decimal(sample["summary"]["weighted_slippage_bps"]))
        self.assertEqual(sample["lineage"]["raw_reference_coverage"], 1.0)

    def test_run_automatically_writes_daily_tca_and_pipeline_metrics(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first_summary = run_backtest(
                root / "config/research.json",
                root / "data/samples/two_venue_books.jsonl",
                Path(first_dir),
            )
            run_backtest(
                root / "config/research.json",
                root / "data/samples/two_venue_books.jsonl",
                Path(second_dir),
            )
            first = json.loads((Path(first_dir) / "tca_report.json").read_text())
            second = json.loads((Path(second_dir) / "tca_report.json").read_text())
            self.assertEqual(first, second)
            self.assertEqual(first["summary"]["fills"], 2)
            self.assertEqual(first["summary"]["weighted_slippage_bps"], "0")
            self.assertEqual(first["summary"]["fees"], "0.48024")
            self.assertEqual(first["summary"]["missed_opportunities"], 0)
            self.assertEqual(len(first["groups"]), 2)
            daily = Path(first_dir) / "tca_daily/1970-01-01.json"
            self.assertEqual(json.loads(daily.read_text())["scope"], "DAILY_PAPER")
            pipeline = json.loads(
                (Path(first_dir) / "pipeline_monitoring.json").read_text()
            )
            self.assertEqual(pipeline["tca"]["report_id"], first["report_id"])
            metrics = (Path(first_dir) / "pipeline_metrics.prom").read_text()
            self.assertIn("crypto_tca_weighted_slippage_bps", metrics)
            self.assertEqual(first_summary["tca"]["daily_reports"], [
                "tca_daily/1970-01-01.json",
            ])


if __name__ == "__main__":
    unittest.main()
