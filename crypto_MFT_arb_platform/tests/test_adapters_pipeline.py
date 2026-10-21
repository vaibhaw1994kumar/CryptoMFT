import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.adapters import normalize_binance_depth, normalize_bybit_depth
from crypto_stat_arb.pipeline import normalize_raw_file


ROOT = Path(__file__).parents[1]


class AdapterPipelineTests(unittest.TestCase):
    @staticmethod
    def resolve(venue: str, symbol: str) -> str:
        if symbol != "BTCUSDT":
            raise ValueError(symbol)
        return "BTC-USDT-SPOT"

    def test_native_payload_is_preserved(self):
        payload = {
            "s": "BTCUSDT", "u": 11, "E": 1000,
            "b": [["100", "1"]], "a": [["101", "1"]], "venueExtra": "kept",
        }
        event = normalize_binance_depth(payload, 1_000_100_000, self.resolve)
        self.assertEqual(event.sequence, 11)
        self.assertEqual(event.native["venueExtra"], "kept")

    def test_bybit_snapshot_mapping(self):
        payload = {
            "topic": "orderbook.50.BTCUSDT", "type": "snapshot", "ts": 1000,
            "data": {"s": "BTCUSDT", "u": 20, "b": [["100", "1"]], "a": [["101", "1"]]},
        }
        event = normalize_bybit_depth(payload, 1_000_200_000, self.resolve)
        self.assertEqual(event.event_type.value, "BOOK_SNAPSHOT")
        self.assertEqual(event.venue, "BYBIT")

    def test_pipeline_writes_manifest_and_canonical_records(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "canonical.jsonl"
            manifest = normalize_raw_file(
                ROOT / "config" / "research.json",
                ROOT / "data" / "samples" / "raw_depth.jsonl",
                output,
            )
            self.assertEqual(manifest["normalized_records"], 2)
            self.assertEqual(manifest["rejected_records"], 0)
            self.assertTrue(Path(manifest["trace"]).exists())
            self.assertTrue(Path(manifest["quarantine"]).exists())
            rows = [json.loads(x) for x in output.read_text().splitlines()]
            self.assertEqual({x["venue"] for x in rows}, {"BINANCE", "BYBIT"})
            self.assertTrue(output.with_suffix(".jsonl.manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
