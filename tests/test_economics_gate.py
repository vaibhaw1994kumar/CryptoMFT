"""Economics acceptance gate (audit P0): P&L is accepted only if the economics reconcile."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from crypto_stat_arb.economics_gate import economics_gate
from crypto_stat_arb.runner import run_backtest


ROOT = Path(__file__).parents[1]


class EconomicsGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.run_dir = Path(cls.tmp.name) / "run"
        run_backtest(ROOT / "config/research.json", ROOT / "data/samples/two_venue_books.jsonl", cls.run_dir)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def tampered(self, mutate) -> Path:
        target = Path(tempfile.mkdtemp(dir=self.tmp.name))
        for name in ("economic_reconciliation.json", "funding_report.json", "borrow_report.json"):
            row = json.loads((self.run_dir / name).read_text())
            mutate(name, row)
            (target / name).write_text(json.dumps(row))
        return target

    def test_reconciled_run_passes(self):
        result = economics_gate(self.run_dir)
        self.assertEqual((result["decision"], result["reconciliation"]), ("PASS", "RECONCILED"))

    def test_failed_check_fails_the_gate(self):
        def break_identity(name, row):
            if name == "economic_reconciliation.json":
                row["status"] = "FAIL"
                row["checks"][0] = {**row["checks"][0], "passed": False, "detail": "residual 5"}
        result = economics_gate(self.tampered(break_identity))
        self.assertEqual(result["decision"], "FAIL")
        self.assertIn("residual 5", result["failures"][0])

    def test_missing_reconciliation_fails(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(economics_gate(Path(empty))["decision"], "FAIL")

    def test_incomplete_fails_unless_every_reason_is_justified(self):
        def missing_funding(name, row):
            if name == "economic_reconciliation.json":
                row["status"] = "INCOMPLETE"
                row["venues"]["BINANCE"]["funding_flags"] = ["FUNDING_RATE_MISSING@8"]
        run_dir = self.tampered(missing_funding)
        self.assertEqual(economics_gate(run_dir)["decision"], "FAIL")
        allowed = economics_gate(run_dir, allow_incomplete={"FUNDING_DATA": "venue history gap, ticket 12"})
        self.assertEqual(allowed["decision"], "PASS")
        self.assertEqual(allowed["allowed_incomplete"]["FUNDING_DATA"]["justification"],
                         "venue history gap, ticket 12")
        with self.assertRaisesRegex(ValueError, "justification"):
            economics_gate(run_dir, allow_incomplete={"FUNDING_DATA": " "})
        with self.assertRaisesRegex(ValueError, "unknown"):
            economics_gate(run_dir, allow_incomplete={"ANYTHING": "because"})

    def test_unclassified_incomplete_cannot_be_waved_through(self):
        def vague(name, row):
            if name == "economic_reconciliation.json":
                row["status"] = "INCOMPLETE"
        result = economics_gate(self.tampered(vague), allow_incomplete={r: "x" for r in ("MISSING_PRICE",)})
        self.assertEqual(result["decision"], "FAIL")
        self.assertIn("UNCLASSIFIED", result["incomplete_reasons"])

    def test_unfinanced_short_fails(self):
        def short(name, row):
            if name == "borrow_report.json":
                row["status"] = "FAIL"
                row["flags"] = [{"code": "UNFINANCED_SHORT:NOT_AUTHORIZED", "ts_ns": 1}]
        self.assertEqual(economics_gate(self.tampered(short))["decision"], "FAIL")

    def test_cli_exits_non_zero_on_fail(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        ok = subprocess.run([sys.executable, "-m", "crypto_stat_arb.cli", "economics-gate",
                             "--run-dir", str(self.run_dir)], env=env, capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        with tempfile.TemporaryDirectory() as empty:
            bad = subprocess.run([sys.executable, "-m", "crypto_stat_arb.cli", "economics-gate",
                                  "--run-dir", empty], env=env, capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertEqual(json.loads(bad.stdout)["decision"], "FAIL")


if __name__ == "__main__":
    unittest.main()
