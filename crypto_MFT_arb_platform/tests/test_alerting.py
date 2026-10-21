import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.alerting import AlertType
from crypto_stat_arb.monitoring import MonitoringThresholds, PipelineMonitor


class AlertOwnershipTests(unittest.TestCase):
    def report(self, **overrides):
        values = {
            "mode": "LIVE", "generated_ts_ns": 100,
            "correlation_id": "forced-alert-test",
            "job": {"state": "RUNNING", "mode": "CONTINUOUS"},
            "data": {"event_count": 1},
            "partitions": {"total": 1, "complete": 1, "completeness": 1.0,
                           "rejected": 0},
            "books": {"BINANCE": {"state": "VALID", "age_ns": 1, "gaps": 0}},
            "versions": {"schema_mismatches": 0},
            "performance": {"replay_events_per_second": 0},
            "live": {"reconnects": 0, "stale_disconnects": 0,
                     "persistence_lag_ns": 1},
        }
        values.update(overrides)
        return PipelineMonitor(MonitoringThresholds()).build(**values)

    def test_forced_alert_matrix_assigns_owner_severity_and_response(self):
        forced = tuple(x.value for x in (
            AlertType.FAILED_INGESTION, AlertType.INVALID_BOOK,
            AlertType.REPLAY_FAILURE, AlertType.RISK_BREACH,
        ))
        report = self.report(forced_alert_types=forced)
        by_type = {row["alert_type"]: row for row in report["alerts"]}
        self.assertEqual(set(by_type), set(forced))
        for row in by_type.values():
            self.assertTrue(row["owner"].endswith("-owner"))
            self.assertIn(row["severity"], {"HIGH", "CRITICAL"})
            self.assertGreater(row["response_target_minutes"], 0)
            self.assertTrue(row["response_expectation"])
            self.assertEqual(row["coverage"], "RESEARCH_HOURS")
            self.assertTrue(row["forced_test"])
        self.assertEqual(report["health"], "CRITICAL")

    def test_failed_job_is_classified_by_ingestion_or_replay_mode(self):
        live = self.report(job={"state": "FAILED", "error": "socket exhausted"})
        replay = self.report(
            mode="REPLAY", job={"state": "FAILED", "error": "bad checkpoint"},
            performance={"replay_events_per_second": 2},
        )
        self.assertIn("FAILED_INGESTION", {x["alert_type"] for x in live["alerts"]})
        self.assertIn("REPLAY_FAILURE", {x["alert_type"] for x in replay["alerts"]})

    def test_invalid_book_and_risk_breach_have_named_owners(self):
        report = self.report(
            books={"BINANCE": {"state": "GAP_DETECTED", "age_ns": 1, "gaps": 1}},
            strategy_execution={
                "orders": {}, "residual": {}, "latency": {}, "rate_limits": {},
                "connectors": {}, "kill_switch": {"state": "ACTIVE"},
            },
        )
        relevant = [x for x in report["alerts"] if x["alert_type"] in {
            "INVALID_BOOK", "RISK_BREACH",
        }]
        self.assertTrue(relevant)
        self.assertTrue(all(x["owner"] for x in relevant))

    def test_forced_alert_is_machine_readable_and_exported(self):
        report = self.report(forced_alert_types=("RISK_BREACH",))
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "pipeline_monitoring.json"
            prom_path = Path(directory) / "pipeline_metrics.prom"
            PipelineMonitor.write(report, json_path, prom_path)
            loaded = json.loads(json_path.read_text())
            self.assertTrue(loaded["alerts"][0]["forced_test"])
            prometheus = prom_path.read_text()
            self.assertIn("crypto_pipeline_alerts{", prometheus)
            self.assertIn('owner="paper-risk-owner"', prometheus)


if __name__ == "__main__":
    unittest.main()
