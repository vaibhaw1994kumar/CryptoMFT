"""Audit P1: a replay of a REJECTED dataset must not pass as a usable result."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from crypto_stat_arb.economics_gate import economics_gate
from crypto_stat_arb.runner import enforce_data_quality, run_backtest

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config/research.json"
CLEAN = ROOT / "data/samples/two_venue_books.jsonl"
REJECTED = ROOT / "data/evidence/quality_gate/controlled_gap/canonical_events.jsonl"   # gap + crossed book


def cli(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run([sys.executable, "-m", "crypto_stat_arb.cli", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True, check=False)


class DataQualityEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cli_backtest_on_rejected_data_exits_nonzero_and_keeps_the_evidence(self):
        out = self.base / "run"
        done = cli("backtest", "--config", str(CONFIG), "--events", str(REJECTED), "--output", str(out))
        self.assertEqual(done.returncode, 1)
        self.assertIn("REJECTED", done.stderr)
        self.assertIn("SEQUENCE_GAPS", done.stderr)
        summary = json.loads(done.stdout)                       # still printed, for investigation
        self.assertEqual(summary["data_quality_approval"]["decision"], "REJECTED")
        for name in ("data_quality_report.json", "data_quality_defects.jsonl", "summary.json"):
            self.assertTrue((out / name).exists(), name)
        self.assertFalse((out / "data_quality_override.json").exists())

    def test_written_justification_is_recorded_and_allows_use(self):
        out = self.base / "run"
        done = cli("backtest", "--config", str(CONFIG), "--events", str(REJECTED), "--output", str(out),
                   "--allow-rejected-data", "research into the gap handling itself")
        self.assertEqual(done.returncode, 0, done.stderr)
        override = json.loads((out / "data_quality_override.json").read_text())
        self.assertEqual(override["justification"], "research into the gap handling itself")
        self.assertIn("SEQUENCE_GAPS", override["active_flag_codes"])
        self.assertEqual(json.loads(done.stdout)["data_quality_override"]["justification"],
                         "research into the gap handling itself")
        gate = economics_gate(out)
        self.assertIn("DATA_QUALITY", gate["allowed_incomplete"])
        self.assertFalse([f for f in gate["failures"] if f.startswith("DATA_QUALITY")])

    def test_blank_justification_does_not_count(self):
        summary = {"data_quality_approval": {"decision": "REJECTED", "active_flag_codes": ["SEQUENCE_GAPS"]}}
        self.assertIn("REJECTED", enforce_data_quality(summary, self.base, "   "))
        self.assertFalse((self.base / "data_quality_override.json").exists())

    def test_economics_gate_fails_a_run_on_rejected_data(self):
        out = self.base / "run"
        run_backtest(CONFIG, REJECTED, out)                      # library call: artifacts only
        gate = economics_gate(out)
        self.assertEqual(gate["data_quality"], "REJECTED")
        self.assertEqual(gate["decision"], "FAIL")
        self.assertTrue(any(f.startswith("DATA_QUALITY REJECTED: ") and "SEQUENCE_GAPS" in f
                            for f in gate["failures"]))
        allowed = economics_gate(out, allow_incomplete={"DATA_QUALITY": "known gap, studying recovery"})
        self.assertFalse([f for f in allowed["failures"] if f.startswith("DATA_QUALITY")])
        self.assertEqual(allowed["allowed_incomplete"]["DATA_QUALITY"]["justification"],
                         "known gap, studying recovery")

    def test_approved_data_is_unaffected(self):
        out = self.base / "run"
        done = cli("backtest", "--config", str(CONFIG), "--events", str(CLEAN), "--output", str(out))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["data_quality_approval"]["decision"], "APPROVED")
        self.assertEqual(economics_gate(out)["data_quality"], "APPROVED")


if __name__ == "__main__":
    unittest.main()
