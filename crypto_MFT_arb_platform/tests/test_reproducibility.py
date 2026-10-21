from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.runner import run_backtest


ROOT = Path(__file__).parents[1]


class ReproducibilityTests(unittest.TestCase):
    def test_same_inputs_produce_identical_results(self):
        config = ROOT / "config" / "research.json"
        events = ROOT / "data" / "samples" / "two_venue_books.jsonl"
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            first = run_backtest(config, events, tmp_path / "first")
            second = run_backtest(config, events, tmp_path / "second")
            self.assertEqual(first, second)
            self.assertEqual(
                (tmp_path / "first" / "ledger.jsonl").read_bytes(),
                (tmp_path / "second" / "ledger.jsonl").read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
