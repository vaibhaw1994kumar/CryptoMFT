"""Config load rejects a cross-venue strategy without two distinct venues."""

import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.config import load_config


ROOT = Path(__file__).parents[1]


def _strip_venue(value, venue: str):
    """Drop every list entry tied to ``venue`` and every dict key named ``venue``."""
    if isinstance(value, dict):
        return {k: _strip_venue(v, venue) for k, v in value.items() if k != venue}
    if isinstance(value, list):
        return [
            _strip_venue(v, venue) for v in value
            if not (isinstance(v, dict) and v.get("venue") == venue)
        ]
    return value


class StrategyVenuesConfigTest(unittest.TestCase):
    def load(self, raw: dict):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            return load_config(path)

    def base(self) -> dict:
        return json.loads((ROOT / "config" / "perp_research.json").read_text(encoding="utf-8"))

    def test_two_venues_load(self):
        cfg, _ = self.load(self.base())
        self.assertEqual(len(set(cfg.venues)), 2)

    def test_single_venue_rejected(self):
        raw = _strip_venue(self.base(), "BINANCE")
        raw["strategy"]["venues"] = ["BYBIT"]
        with self.assertRaisesRegex(ValueError, "at least two distinct venues"):
            self.load(raw)

    def test_duplicate_venue_rejected(self):
        raw = self.base()
        raw["strategy"]["venues"] = ["BYBIT", "BYBIT"]
        with self.assertRaisesRegex(ValueError, "at least two distinct venues"):
            self.load(raw)


if __name__ == "__main__":
    unittest.main()
