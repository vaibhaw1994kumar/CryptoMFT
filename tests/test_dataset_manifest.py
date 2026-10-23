"""Audit Step 14: every replay writes dataset_manifest.json."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from crypto_stat_arb.runner import run_backtest

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config/research.json"
CLEAN = ROOT / "data/samples/two_venue_books.jsonl"
GAPPED = ROOT / "data/evidence/quality_gate/controlled_gap/canonical_events.jsonl"
MS = 1_000_000
REQUIRED = {
    "dataset", "venues", "instrument_metadata", "replay_ordering", "message_counts", "sequence_and_recovery",
    "invalid_intervals", "silent_intervals", "timestamp_anomalies", "source_comparisons",
    "exclusions_and_repairs", "run", "timestamp_convention", "manifest_sha256",
}


def event(venue, sequence, receive_ms, kind="BOOK_DELTA", bid="100", ask="101"):
    return {"event_id": f"{venue}-{sequence}-{kind}", "event_type": kind, "venue": venue,
            "instrument_id": "BTC-USDT-SPOT", "exchange_ts_ns": receive_ms * MS, "source_ts_ns": receive_ms * MS,
            "receive_ts_ns": receive_ms * MS, "sequence": sequence, "sequence_start": sequence,
            "raw_ref": f"raw-{venue}-{sequence}", "adapter_version": "test-v1",
            "bids": [[bid, "1"]], "asks": [[ask, "1"]]}


def write_events(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def manifest(run_dir):
    return json.loads((Path(run_dir) / "dataset_manifest.json").read_text())


class DatasetManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_run_records_every_required_section(self):
        run_backtest(CONFIG, CLEAN, self.base / "run")
        m = manifest(self.base / "run")
        self.assertTrue(REQUIRED <= set(m), REQUIRED - set(m))
        events_sha = hashlib.sha256(CLEAN.read_bytes()).hexdigest()
        self.assertEqual(m["dataset"]["inputs"]["events"]["sha256"], events_sha)
        self.assertEqual(set(m["venues"]), {"BINANCE", "BYBIT"})
        for venue in m["venues"].values():
            self.assertIsNotNone(venue["first_receive_ts_utc"])
            self.assertIsNotNone(venue["reconstruction"])
        self.assertEqual(m["exclusions_and_repairs"]["repairs"], [])
        self.assertEqual(m["exclusions_and_repairs"]["data_quality_decision"], "APPROVED")
        self.assertEqual(len(m["run"]["source_sha256"]), 64)
        self.assertEqual(m["run"]["config_sha256"], hashlib.sha256(CONFIG.read_bytes()).hexdigest())
        self.assertEqual(len(m["instrument_metadata"]["sha256"]), 64)

    def test_same_input_gives_the_same_manifest(self):
        run_backtest(CONFIG, CLEAN, self.base / "a")
        run_backtest(CONFIG, CLEAN, self.base / "b")
        self.assertEqual(manifest(self.base / "a")["manifest_sha256"], manifest(self.base / "b")["manifest_sha256"])

    def test_sequence_gap_is_an_invalid_interval_and_the_rejection_is_recorded(self):
        run_backtest(CONFIG, GAPPED, self.base / "run")
        m = manifest(self.base / "run")
        reasons = [reason for rows in m["invalid_intervals"].values() for row in rows for reason in row["reasons"]]
        self.assertTrue(any(reason.startswith("sequence_gap") for reason in reasons), reasons)
        self.assertEqual(m["exclusions_and_repairs"]["data_quality_decision"], "REJECTED")
        self.assertEqual(m["exclusions_and_repairs"]["excluded_intervals"], [])     # nothing silently dropped

    def test_feed_silence_longer_than_max_book_age_is_a_silent_interval(self):
        rows = [event("BYBIT", 10, 1_000, "BOOK_SNAPSHOT"), event("BYBIT", 11, 1_100),
                event("BYBIT", 20, 4_600, "BOOK_SNAPSHOT"), event("BYBIT", 21, 4_700)]       # 3.5 s silent
        run_backtest(CONFIG, write_events(self.base / "e.jsonl", rows), self.base / "run")
        silent = manifest(self.base / "run")["silent_intervals"]["BYBIT"]
        self.assertEqual([(r["duration_ns"], r["resumed_with"]) for r in silent], [(3_500 * MS, "BOOK_SNAPSHOT")])

    def test_clock_step_reorder_is_recorded_as_a_repair(self):
        rows = [event("BYBIT", 10, 1_000, "BOOK_SNAPSHOT"), event("BYBIT", 11, 1_020), event("BYBIT", 12, 1_040),
                event("BYBIT", 13, 1_010)]                                            # stamped 30 ms early
        run_backtest(CONFIG, write_events(self.base / "e.jsonl", rows), self.base / "run")
        repairs = manifest(self.base / "run")["exclusions_and_repairs"]["repairs"]
        self.assertEqual([r["type"] for r in repairs], ["REPLAY_REORDER_WITHIN_STREAM"])
        self.assertGreater(repairs[0]["events_moved"], 0)

    def test_override_and_comparison_reports_are_recorded(self):
        report = self.base / "tardis.json"
        report.write_text(json.dumps({"venue": "BYBIT", "samples": {"MATCH": 3}, "mismatch_share": 0.0,
                                      "inputs": {"tardis_csv": "x.csv.gz"}}))
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        done = subprocess.run([sys.executable, "-m", "crypto_stat_arb.cli", "backtest", "--config", str(CONFIG),
                               "--events", str(GAPPED), "--output", str(self.base / "run"),
                               "--comparison-report", str(report), "--allow-rejected-data", "studying the gap"],
                              cwd=ROOT, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        m = manifest(self.base / "run")
        self.assertEqual(m["exclusions_and_repairs"]["data_quality_override"]["justification"], "studying the gap")
        self.assertEqual(m["source_comparisons"][0]["samples"], {"MATCH": 3})
        body = {k: v for k, v in m.items() if k != "manifest_sha256"}
        from crypto_stat_arb.models import canonical_json
        self.assertEqual(m["manifest_sha256"], hashlib.sha256(canonical_json(body).encode()).hexdigest())

    def test_live_session_provenance_is_traced(self):
        session = self.base / "live-session"
        part = session / "part-000001"
        part.mkdir(parents=True)
        (session / "live_manifest.json").write_text(json.dumps(
            {"session_id": "live-session", "session_sha256": "abc", "wire_records": 9, "rejected_frames": 1}))
        (part / "quarantine.jsonl").write_text('{"stage": "PARSE"}\n')
        events = write_events(part / "canonical.jsonl", [event("BYBIT", 10, 1_000, "BOOK_SNAPSHOT")])
        run_backtest(CONFIG, events, self.base / "run")
        provenance = manifest(self.base / "run")["dataset"]["live_provenance"]
        self.assertEqual((provenance["session_id"], provenance["quarantined_by_part"]),
                         ("live-session", {"part-000001": 1}))


if __name__ == "__main__":
    unittest.main()
