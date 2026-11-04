"""Queue model v2, step 1: the data manifest and the truncated-day gate."""
from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from new_backtest.backtester import ArbBacktestConfig, CrossVenueArbBacktester
from new_backtest.data_manifest import DataManifestError, gate
from new_backtest.event_loader import iter_venue_events

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("queue_v2_manifest", ROOT / "scripts" / "queue_v2_manifest.py")
man = importlib.util.module_from_spec(_spec)
sys.modules["queue_v2_manifest"] = man
_spec.loader.exec_module(man)

DAY = "2022-01-05"


def write_gz(path: Path, lines: list[str], truncate: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = gzip.compress(("\n".join(lines) + "\n").encode())
    path.write_bytes(data[: len(data) - 12] if truncate else data)


def write(root: Path, venue: str, ch: str, day: str, recs: list, name='1641340800000.jsonl.gz') -> None:
    write_gz(root / venue / ch / 'pair=SOLUSDT' / f'date={day}' / name, [json.dumps(r) for r in recs])


def build(root: Path, truncated: bool = False) -> None:
    base = 1641340800.0
    # raw text on purpose: json.dumps(152.40) would write 152.4 and lose the trailing zero the manifest has to keep
    ev = ['{"exchange_ts": %s, "local_ts": %s, "side": "bid", "price": 152.40, "volume": 3.5, "event_type": "modify", "u": %d}'
          % (base + (i if i < 20 else i + 100), base + i, i) for i in range(50)]
    snaps = [json.dumps({"timestamp": base + i, "local_ts": base + i, "bids": [[152.4, 5.5], [152.3, 7.25]], "asks": [[152.5, 4.0]]}) for i in range(4)]
    trades = [json.dumps({"exchange_ts": base + i, "local_ts": base + i, "price": 152.4, "volume": 1.0, "side": "b", "trade_id": 1000 + i}) for i in range(30)]
    for ch, lines in (("book_events", ev), ("book_snapshots", snaps), ("trades", trades)):
        write_gz(root / "bybit" / ch / "pair=SOLUSDT" / f"date={DAY}" / "1641340800000.jsonl.gz", lines, truncate=(truncated and ch == "trades"))


def scan_tree(root: Path, out: Path) -> dict:
    man.DATASETS["t"] = {"venues": {"bybit": ("bybit", "perp_linear")}, "symbols": ["SOLUSDT"], "days": (DAY, DAY), "provider": "test"}
    man.run(SimpleNamespace(platform_root=str(ROOT), dataset="t", data_root=str(root), out_dir=str(out), symbols=""))
    man.merge(SimpleNamespace(merge=str(out), out=str(out / "manifest.json")))
    return json.loads((out / "manifest.json").read_text())


class ManifestTests(unittest.TestCase):
    def test_every_file_carries_the_contract_fields(self):
        with tempfile.TemporaryDirectory() as d:
            root, out = Path(d) / "data", Path(d) / "out"
            build(root)
            m = scan_tree(root, out)
            f = {x["channel"]: x for x in m["files"]}
            self.assertEqual(set(f), {"book_events", "book_snapshots", "trades"})
            for x in f.values():
                for k in ("provider", "exchange_id", "product", "symbol", "date", "channel", "sha256", "parser", "schema_keys", "price_decimals",
                          "size_decimals", "gaps_over_5s", "rows", "truncated", "gzip_ok"):
                    self.assertIn(k, x)
                self.assertEqual((x["exchange_id"], x["product"], x["symbol"], x["date"]), ("bybit", "perp_linear", "SOLUSDT", DAY))
                self.assertEqual(x["sha256"], hashlib.sha256((root / x["path"]).read_bytes()).hexdigest())
                self.assertFalse(x["truncated"])
            self.assertEqual(f["book_snapshots"]["snapshot_count"], 4)
            self.assertEqual(f["trades"]["trade_id_field"], "trade_id")                  # id availability
            self.assertEqual(f["book_events"]["update_id_field"], "u")
            self.assertIsNone(f["trades"]["update_id_field"])
            self.assertEqual((f["book_events"]["price_decimals"], f["book_events"]["size_decimals"]), (2, 1))   # '152.40' keeps its trailing zero
            self.assertEqual(f["book_snapshots"]["size_decimals"], 2)                      # 7.25
            self.assertEqual(f["book_events"]["gaps_over_5s"], 1)                          # the 100 s hole in the fixture
            self.assertEqual(m["days"][f"bybit/SOLUSDT/{DAY}"]["status"], "OK")

    def test_capture_region_and_client_version_are_declared_and_unrecorded_ones_are_listed_not_blocking(self):
        with tempfile.TemporaryDirectory() as d:
            root, out = Path(d) / "data", Path(d) / "out"
            build(root)
            m = scan_tree(root, out)
            for x in m["files"]:
                self.assertIn("capture_region", x)
                self.assertIn("client_version", x)
                self.assertEqual(x["provenance_unrecorded"], ["capture_region", "client_version"])     # the fixture dataset does not declare them
            doc = gate(str(out / "manifest.json"), ("bybit",), "SOLUSDT", DAY, DAY, None)       # listed, never blocking
            self.assertEqual(doc["provenance_unrecorded_venue_days"], [f"bybit/SOLUSDT/{DAY}"])

    def test_annotate_adds_the_fields_to_an_old_manifest_without_rescanning(self):
        with tempfile.TemporaryDirectory() as d:
            root, out = Path(d) / "data", Path(d) / "out"
            build(root)
            m = scan_tree(root, out)
            for f in m["files"]:
                f.pop("capture_region"); f.pop("client_version"); f.pop("provenance_unrecorded")        # a manifest from before the fields existed
            old = out / "old.json"
            old.write_text(json.dumps(m))
            man.annotate(SimpleNamespace(annotate=str(old), out=str(out / "new.json")))
            new = json.loads((out / "new.json").read_text())
            self.assertEqual([f["sha256"] for f in new["files"]], [f["sha256"] for f in m["files"]])   # nothing rescanned
            for f in new["files"]:
                self.assertIn("UNKNOWN", f["capture_region"])
                self.assertEqual(f["provenance_unrecorded"], ["capture_region", "client_version"])

    def test_a_truncated_gzip_is_flagged_and_the_day_is_truncated(self):
        with tempfile.TemporaryDirectory() as d:
            root, out = Path(d) / "data", Path(d) / "out"
            build(root, truncated=True)
            m = scan_tree(root, out)
            tr = next(x for x in m["files"] if x["channel"] == "trades")
            self.assertTrue(tr["truncated"] and not tr["gzip_ok"])
            self.assertIn("truncated", tr["error"])
            self.assertEqual(m["days"][f"bybit/SOLUSDT/{DAY}"]["status"], "TRUNCATED")
            self.assertTrue(m["days"][f"bybit/SOLUSDT/{DAY}"]["channels"]["trades"]["truncated"])

    def test_an_empty_channel_marks_the_day_empty(self):
        with tempfile.TemporaryDirectory() as d:
            root, out = Path(d) / "data", Path(d) / "out"
            build(root)
            write_gz(root / "bybit" / "trades" / "pair=SOLUSDT" / f"date={DAY}" / "1641340800000.jsonl.gz", [])
            m = scan_tree(root, out)
            self.assertEqual(m["days"][f"bybit/SOLUSDT/{DAY}"]["status"], "EMPTY")


class GateTests(unittest.TestCase):
    def _manifest(self, d, truncated):
        root, out = Path(d) / "data", Path(d) / "out"
        build(root, truncated=truncated)
        scan_tree(root, out)
        return str(out / "manifest.json")

    def test_a_clean_day_passes_and_records_the_manifest_hash(self):
        with tempfile.TemporaryDirectory() as d:
            doc = gate(self._manifest(d, False), ("bybit",), "SOLUSDT", DAY, DAY, None)
            self.assertEqual(doc["truncated_days"], [])
            self.assertEqual(len(doc["manifest_sha256"]), 64)
            self.assertIsNone(doc["override_reason"])

    def test_a_truncated_day_blocks_the_run_unless_a_reason_is_given_and_the_reason_is_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._manifest(d, True)
            with self.assertRaises(DataManifestError):
                gate(path, ("bybit",), "SOLUSDT", DAY, DAY, None)
            with self.assertRaises(DataManifestError):
                gate(path, ("bybit",), "SOLUSDT", DAY, DAY, "   ")                              # a blank reason is not a reason
            doc = gate(path, ("bybit",), "SOLUSDT", DAY, DAY, "exploratory: partial trade tape")
            self.assertEqual(doc["truncated_days"], [f"bybit/{DAY}"])
            self.assertEqual(doc["override_reason"], "exploratory: partial trade tape")

    def test_a_day_whose_rebuild_acceptance_is_below_99_percent_is_flagged_in_the_result(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._manifest(d, False)
            acc = Path(path).parent / "rebuild_acceptance"
            acc.mkdir()
            (acc / f"bybit_{DAY}.json").write_text(json.dumps({"flag_below_99": True, "flag_note": "touch price match below 99%",
                                                              "variants": {"engine_tape_repair_on": {"touch_price_match_pct": 98.7, "touch_size_match_pct": 82.6}}}))
            doc = gate(path, ("bybit",), "SOLUSDT", DAY, DAY, None)
            self.assertEqual(doc["rebuild_acceptance_flags"][0]["touch_price_match_pct"], 98.7)
            (acc / f"bybit_{DAY}.json").write_text(json.dumps({"flag_below_99": False, "variants": {}}))
            self.assertEqual(gate(path, ("bybit",), "SOLUSDT", DAY, DAY, None)["rebuild_acceptance_flags"], [])

    def test_placeholder_snapshots_are_detected_block_the_run_and_can_be_skipped_for_that_venue(self):
        with tempfile.TemporaryDirectory() as d:
            root, out = Path(d) / "data", Path(d) / "out"
            build(root)
            t = 1641340800.0
            snaps = [json.dumps({"timestamp": t + i, "local_ts": t + i, "bids": [[100.0 - k * 0.01, 50.0] for k in range(5)], "asks": [[100.05 + k * 0.01, 50.0] for k in range(5)]})
                     for i in range(80)]                                                    # every level exactly 50.0: a placeholder, not a real book
            write_gz(root / "bybit" / "book_snapshots" / "pair=SOLUSDT" / f"date={DAY}" / "1641340800000.jsonl.gz", snaps)
            m = scan_tree(root, out)
            self.assertTrue(m["days"][f"bybit/SOLUSDT/{DAY}"]["channels"]["book_snapshots"]["placeholder"])
            path = str(out / "manifest.json")
            with self.assertRaises(DataManifestError):
                gate(path, ("bybit",), "SOLUSDT", DAY, DAY, None)
            doc = gate(path, ("bybit",), "SOLUSDT", DAY, DAY, None, skipped_snapshot_venues=("bybit",))
            self.assertEqual((doc["placeholder_snapshot_days"], doc["snapshots_ignored_for"]), ([f"bybit/{DAY}"], ["bybit"]))

    def test_book_events_that_are_a_copy_of_the_trade_tape_are_detected_and_block_the_run(self):
        with tempfile.TemporaryDirectory() as d:
            root, out = Path(d) / "data", Path(d) / "out"
            build(root)
            t = 1641340800.0
            prints = [{"exchange_ts": t + i, "local_ts": t + i, "price": 100.0 + (i % 3), "volume": 1.0 + i, "side": "sell"} for i in range(80)]
            copies = [{"exchange_ts": r["exchange_ts"], "local_ts": r["local_ts"], "side": "bid", "price": r["price"], "volume": r["volume"], "event_type": "modify"} for r in prints]
            write_gz(root / "bybit" / "trades" / "pair=SOLUSDT" / f"date={DAY}" / "1641340800000.jsonl.gz", [json.dumps(r) for r in prints])
            write_gz(root / "bybit" / "book_events" / "pair=SOLUSDT" / f"date={DAY}" / "1641340800000.jsonl.gz", [json.dumps(r) for r in copies])
            m = scan_tree(root, out)
            self.assertTrue(m["days"][f"bybit/SOLUSDT/{DAY}"]["book_events_is_trade_copy"])
            with self.assertRaises(DataManifestError):
                gate(str(out / "manifest.json"), ("bybit",), "SOLUSDT", DAY, DAY, "any written reason")        # no reason can make a trade tape a book

    def test_the_feed_can_ignore_one_venues_snapshots(self):
        with tempfile.TemporaryDirectory() as d:
            root, t = Path(d), 1641340800.0
            write(root, "bybit", "book_snapshots", DAY, [{"timestamp": t + 1, "local_ts": t + 1, "bids": [[100.0, 50.0]], "asks": [[101.0, 50.0]]}])
            write(root, "bybit", "book_events", DAY, [{"exchange_ts": t + 2, "local_ts": t + 2, "side": "bid", "price": 100.0, "volume": 2.0, "event_type": "modify"}])
            kinds = lambda **kw: [e.kind for e in iter_venue_events(root, "bybit", "SOLUSDT", DAY, DAY, **kw)]
            self.assertEqual(kinds(), ["book", "book_event"])
            self.assertEqual(kinds(include_snapshots=False), ["book_event"])

    def test_a_day_missing_from_the_manifest_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(DataManifestError):
                gate(self._manifest(d, False), ("bybit",), "SOLUSDT", "2022-01-06", "2022-01-06", None)

    def test_the_engine_refuses_to_start_a_run_on_a_truncated_day(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._manifest(d, True)
            cfg = ArbBacktestConfig(data_root=str(Path(d) / "data"), venues=("bybit", "binance"), pair="SOLUSDT", start_date=DAY, end_date=DAY,
                                    data_manifest=path)
            with self.assertRaises(DataManifestError):
                CrossVenueArbBacktester(cfg).run()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
