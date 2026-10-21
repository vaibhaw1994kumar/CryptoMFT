from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.models import canonical_json
from crypto_stat_arb.quality import (
    HealthLevel,
    QualityGate,
    QualitySnapshot,
    QualityThresholds,
)
from crypto_stat_arb.runner import run_backtest


ROOT = Path(__file__).parents[1]


class QualityGateTests(unittest.TestCase):
    def test_clean_dataset_is_approved(self):
        gate = QualityGate(QualityThresholds())
        report = gate.build_report(
            QualitySnapshot(events=100),
            scope="TEST_DATASET",
            correlation_id="clean",
        )
        self.assertEqual(report["decision"], "APPROVED")
        self.assertEqual(report["health"], "HEALTHY")
        self.assertEqual(report["active_flags"], [])

    def test_required_defects_raise_realtime_health_flags(self):
        gate = QualityGate(QualityThresholds())
        flags = gate.evaluate(
            QualitySnapshot(
                events=10,
                gaps=1,
                stale_intervals=1,
                crossed_books=1,
                duplicates=1,
                schema_exceptions=1,
                rejected_partitions=1,
            ),
            scope="LIVE_FEED",
            correlation_id="session-1",
        )
        codes = {flag.code for flag in flags}
        self.assertTrue({
            "SEQUENCE_GAPS", "STALE_INTERVALS", "CROSSED_BOOKS",
            "DUPLICATE_EVENTS", "SCHEMA_EXCEPTIONS", "REJECTED_PARTITIONS",
        }.issubset(codes))
        self.assertEqual(gate.health, HealthLevel.CRITICAL)

    def test_alert_threshold_crossing_is_configurable(self):
        gate = QualityGate(QualityThresholds(max_duplicates=2))
        self.assertEqual(gate.evaluate(
            QualitySnapshot(events=10, duplicates=2),
            scope="LIVE_FEED", correlation_id="within-limit",
        ), ())
        flags = gate.evaluate(
            QualitySnapshot(events=11, duplicates=3),
            scope="LIVE_FEED", correlation_id="over-limit",
        )
        self.assertEqual([flag.code for flag in flags], ["DUPLICATE_EVENTS"])
        self.assertEqual(gate.health, HealthLevel.DEGRADED)

    def test_defect_log_is_hash_chained_and_report_attested(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            gate = QualityGate(QualityThresholds(), clock_ns=lambda: 123)
            report = gate.write_artifacts(
                base / "quality.json",
                base / "defects.jsonl",
                QualitySnapshot(events=1, gaps=1, schema_exceptions=1),
                scope="PARTITION",
                correlation_id="p1",
                limitations=("fixture",),
            )
            defects = [json.loads(line) for line in (base / "defects.jsonl").read_text().splitlines()]
        previous = "0" * 64
        for defect in defects:
            recorded_hash = defect.pop("defect_hash")
            self.assertEqual(defect["previous_defect_hash"], previous)
            self.assertEqual(recorded_hash, sha256(canonical_json(defect).encode()).hexdigest())
            previous = recorded_hash
        self.assertEqual(report["decision"], "REJECTED")
        self.assertEqual(report["terminal_defect_hash"], previous)
        self.assertTrue(report["approval_attestation_sha256"])

    def test_verified_backtest_publishes_approved_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            summary = run_backtest(
                ROOT / "config" / "research.json",
                ROOT / "data" / "samples" / "two_venue_books.jsonl",
                output,
            )
            report = json.loads((output / "data_quality_report.json").read_text())
        self.assertEqual(summary["data_quality_approval"]["decision"], "APPROVED")
        self.assertEqual(report["decision"], "APPROVED")
        self.assertEqual(report["defect_count"], 0)


if __name__ == "__main__":
    unittest.main()
