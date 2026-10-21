import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.monitoring import (
    MonitoringThresholds, PipelineMonitor,
)
from crypto_stat_arb.runner import run_backtest


class PipelineMonitoringTests(unittest.TestCase):
    def base_report(self, **overrides):
        values = {
            "mode": "LIVE", "generated_ts_ns": 100,
            "correlation_id": "session-1",
            "job": {"state": "RUNNING", "mode": "CONTINUOUS"},
            "data": {"event_count": 10},
            "partitions": {"total": 1, "complete": 0, "completeness": 1.0},
            "books": {"BINANCE": {
                "state": "VALID", "age_ns": 10, "gaps": 0,
            }},
            "versions": {"schema_mismatches": 0},
            "performance": {"replay_events_per_second": 0},
            "live": {"reconnects": 0, "stale_disconnects": 0,
                     "persistence_lag_ns": 10},
        }
        values.update(overrides)
        return PipelineMonitor(MonitoringThresholds(
            max_data_age_ns=100,
            max_persistence_lag_ns=100,
        )).build(**values)

    def test_healthy_live_snapshot_contains_every_required_metric(self):
        report = self.base_report()
        self.assertEqual(report["health"], "HEALTHY")
        names = {row["name"] for row in report["metrics"]}
        self.assertEqual(names, {
            "data_age_ns", "partition_completeness", "sequence_gaps",
            "invalid_books", "schema_mismatches", "reconnects",
            "stale_disconnects", "persistence_lag_ns",
        })

    def test_stale_invalid_gap_is_critical_and_reconnect_is_visible(self):
        report = self.base_report(
            books={"BINANCE": {
                "state": "GAP_DETECTED", "age_ns": 101, "gaps": 1,
            }},
            live={"reconnects": 6, "stale_disconnects": 1,
                  "persistence_lag_ns": 101},
        )
        self.assertEqual(report["health"], "CRITICAL")
        codes = {row["code"] for row in report["alerts"]}
        self.assertTrue({
            "DATA_AGE_NS", "SEQUENCE_GAPS", "INVALID_BOOKS",
            "RECONNECTS", "STALE_DISCONNECTS", "PERSISTENCE_LAG_NS",
        }.issubset(codes))

    def test_replay_speed_and_partition_thresholds_are_enforced(self):
        report = self.base_report(
            mode="REPLAY",
            partitions={"total": 2, "complete": 1, "completeness": 0.5},
            performance={"replay_events_per_second": 0.5},
        )
        self.assertEqual(report["health"], "CRITICAL")
        codes = {row["code"] for row in report["alerts"]}
        self.assertIn("PARTITION_COMPLETENESS", codes)
        self.assertIn("REPLAY_EVENTS_PER_SECOND", codes)

    def test_atomic_json_and_prometheus_outputs_are_machine_readable(self):
        report = self.base_report()
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "monitor.json"
            prom_path = Path(directory) / "metrics.prom"
            PipelineMonitor.write(report, json_path, prom_path)
            self.assertEqual(json.loads(json_path.read_text())["health"], "HEALTHY")
            metrics = prom_path.read_text()
            self.assertIn("crypto_pipeline_data_age_ns", metrics)
            self.assertIn('level="HEALTHY"} 1', metrics)

    def test_strategy_thresholds_share_pipeline_health(self):
        report = self.base_report(strategy_execution={
            "orders": {"rejects": 1},
            "residual": {"open_residual_base": "0.003"},
            "latency": {"max_dispatch_duration_ns": 10_000_001},
            "rate_limits": {"max_utilization": 0.95},
            "connectors": {"unhealthy": 1},
            "kill_switch": {"state": "ACTIVE"},
        })
        self.assertEqual(report["health"], "CRITICAL")
        codes = {row["code"] for row in report["alerts"]}
        self.assertTrue({
            "ORDER_REJECTS", "OPEN_RESIDUAL_BASE", "MAX_DISPATCH_LATENCY_NS",
            "RATE_LIMIT_UTILIZATION", "UNHEALTHY_CONNECTORS",
            "ACTIVE_KILL_SWITCHES",
        }.issubset(codes))

    def test_paper_simulation_is_monitored_end_to_end(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            summary = run_backtest(
                root / "config/research.json",
                root / "data/samples/two_venue_books.jsonl",
                Path(directory),
            )
            report = json.loads(
                (Path(directory) / "pipeline_monitoring.json").read_text()
            )
            execution = report["strategy_execution"]
            self.assertEqual(report["health"], "HEALTHY")
            self.assertEqual(execution["session_mode"], "PAPER_SIMULATION")
            self.assertEqual(execution["opportunities"]["count"], 1)
            self.assertEqual(execution["routing"]["routes_by_venue"], {
                "BINANCE": 1, "BYBIT": 1,
            })
            self.assertEqual(execution["orders"]["fills"], 2)
            self.assertEqual(execution["pnl"]["net"], "0.11976")
            self.assertEqual(execution["residual"]["open_residual_base"], "0")
            self.assertEqual(execution["kill_switch"]["state"], "INACTIVE")
            self.assertEqual(summary["pipeline_monitoring"]["strategy_execution"]["orders"], 2)
            prometheus = (Path(directory) / "pipeline_metrics.prom").read_text()
            self.assertIn("crypto_strategy_opportunity_count", prometheus)
            self.assertIn("crypto_strategy_net_pnl", prometheus)


if __name__ == "__main__":
    unittest.main()
