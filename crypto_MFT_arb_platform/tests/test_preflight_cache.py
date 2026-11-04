"""The opt-in per-day preflight scan cache returns exactly the uncached counts and is invalidated by a changed file."""
import gzip, json, os, tempfile, unittest
from pathlib import Path
from unittest import mock

from new_backtest import quality_preflight as qp


class PreflightCacheTests(unittest.TestCase):
    def _data(self, root):
        d = Path(root) / "bybit" / "book_snapshots" / "pair=SOLUSDT" / "date=2022-01-13"
        d.mkdir(parents=True)
        return d

    def test_cache_hit_skips_the_scan_and_returns_the_same_counts(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as cache:
            self._data(root)
            calls = []

            def fake(r, v, p, day, stats):
                calls.append(1)
                stats["duplicates_dropped"] = 3
                return {"events": 5, "price_jumps": 1}
            with mock.patch.dict(os.environ, {"NEW_BACKTEST_PREFLIGHT_CACHE": cache}), mock.patch.object(qp, "_scan_day", fake):
                s1, s2 = {}, {}
                a = qp._cached_scan_day(Path(root), "bybit", "SOLUSDT", "2022-01-13", s1)
                b = qp._cached_scan_day(Path(root), "bybit", "SOLUSDT", "2022-01-13", s2)
            self.assertEqual((a, s1), (b, s2))
            self.assertEqual(len(calls), 1)

    def test_a_changed_data_file_invalidates_the_entry_and_no_env_means_no_cache(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as cache:
            d = self._data(root)
            (d / "a.jsonl.gz").write_bytes(gzip.compress(b"{}\n"))
            calls = []
            fake = lambda r, v, p, day, stats: (calls.append(1), {"events": len(calls)})[1]
            with mock.patch.dict(os.environ, {"NEW_BACKTEST_PREFLIGHT_CACHE": cache}), mock.patch.object(qp, "_scan_day", fake):
                qp._cached_scan_day(Path(root), "bybit", "SOLUSDT", "2022-01-13", {})
                (d / "a.jsonl.gz").write_bytes(gzip.compress(b"{}\n{}\n"))
                self.assertEqual(qp._cached_scan_day(Path(root), "bybit", "SOLUSDT", "2022-01-13", {})["events"], 2)
            env = {k: v for k, v in os.environ.items() if k != "NEW_BACKTEST_PREFLIGHT_CACHE"}
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(qp, "_scan_day", fake):
                qp._cached_scan_day(Path(root), "bybit", "SOLUSDT", "2022-01-13", {})
                qp._cached_scan_day(Path(root), "bybit", "SOLUSDT", "2022-01-13", {})
            self.assertEqual(len(calls), 4)


if __name__ == "__main__":
    unittest.main()
