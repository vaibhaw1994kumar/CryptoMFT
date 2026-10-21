import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.runner import run_backtest
from crypto_stat_arb.structured_logging import StructuredLogStore


class StructuredLoggingTests(unittest.TestCase):
    def test_published_failed_trace_contains_complete_correlation_chain(self):
        root = Path(__file__).resolve().parents[1]
        sample = json.loads(
            (root / "data/samples/logs/failed_opportunity_trace.json").read_text()
        )
        self.assertEqual(sample["record_count"], len(sample["records"]))
        self.assertEqual(sample["terminal_outcome"], "REJECTED")
        event_types = {row["event_type"] for row in sample["records"]}
        self.assertTrue({
            "SOURCE_EVENT", "FEATURE_STATE", "OPPORTUNITY",
            "CHILD_ORDER_CREATED", "PRE_TRADE_VALIDATION",
            "OPPORTUNITY_OUTCOME",
        }.issubset(event_types))
        self.assertEqual(len({
            row["child_order_id"] for row in sample["records"]
            if row.get("child_order_id")
        }), 2)

    def test_central_store_is_searchable_by_every_correlation_layer(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            run_backtest(
                root / "config/research.json",
                root / "data/samples/two_venue_books.jsonl",
                output,
            )
            store = StructuredLogStore(
                output / "structured_logs.jsonl",
                output / "structured_logs.sqlite",
            )
            feature = store.query(event_type="FEATURE_STATE")[0]
            child = store.query(event_type="CHILD_ORDER_CREATED")[0]
            risk = store.query(event_type="PRE_TRADE_VALIDATION")[0]
            self.assertTrue(feature["source_event_ids"])
            self.assertTrue(feature["feature_state_id"])
            self.assertTrue(feature["opportunity_id"])
            self.assertTrue(feature["parent_plan_id"])
            self.assertTrue(child["child_order_id"])
            self.assertEqual(child["feature_state_id"], feature["feature_state_id"])
            self.assertTrue(risk["risk_decision_id"])
            self.assertEqual(
                store.query(risk_decision_id=risk["risk_decision_id"])[0]["opportunity_id"],
                feature["opportunity_id"],
            )
            self.assertTrue(store.query(component="EXECUTION_CONNECTOR", venue="BINANCE"))
            self.assertTrue(store.query(text="opportunity completed"))

    def test_failed_opportunity_trace_links_source_feature_risk_parent_and_children(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            config = json.loads((root / "config/research.json").read_text())
            config["risk"]["max_order_notional"] = "100"
            config_path = temporary / "rejecting_config.json"
            config_path.write_text(json.dumps(config))
            output = temporary / "run"
            summary = run_backtest(
                config_path,
                root / "data/samples/two_venue_books.jsonl",
                output,
            )
            trace_rows = [
                json.loads(line) for line in
                (output / "failed_opportunity_traces.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(trace_rows), 1)
            trace = trace_rows[0]
            records = trace["records"]
            event_types = {row["event_type"] for row in records}
            self.assertTrue({
                "SOURCE_EVENT", "FEATURE_STATE", "OPPORTUNITY",
                "CHILD_ORDER_CREATED", "PRE_TRADE_VALIDATION",
                "OPPORTUNITY_OUTCOME",
            }.issubset(event_types))
            self.assertEqual(trace["terminal_outcome"], "REJECTED")
            feature_ids = {row["feature_state_id"] for row in records if row["feature_state_id"]}
            risk_ids = {row["risk_decision_id"] for row in records if row["risk_decision_id"]}
            parent_ids = {row["parent_plan_id"] for row in records if row["parent_plan_id"]}
            child_ids = {row["child_order_id"] for row in records if row["child_order_id"]}
            source_ids = {row["source_event_id"] for row in records if row["source_event_id"]}
            self.assertEqual(len(feature_ids), 1)
            self.assertEqual(len(risk_ids), 1)
            self.assertEqual(len(parent_ids), 1)
            self.assertEqual(len(child_ids), 2)
            self.assertEqual(len(source_ids), 2)
            self.assertEqual(summary["structured_logs"]["failed_opportunities"], 1)
            self.assertGreater(summary["structured_logs"]["levels"]["ERROR"], 0)


if __name__ == "__main__":
    unittest.main()
