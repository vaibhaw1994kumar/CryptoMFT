"""Audit Step 10: offline capture vs Tardis comparison (tardis_compare + compare-tardis CLI)."""

import csv
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from crypto_stat_arb.tardis_compare import compare_with_tardis, iter_tardis_messages

ROOT = Path(__file__).parents[1]
SEC = 1_000_000_000
HEADER = ["exchange", "symbol", "timestamp", "local_timestamp", "is_snapshot", "side", "price", "amount"]


def message(ts_ms, u, kind, bids, asks):
    """One venue message as (our canonical event, Tardis rows)."""
    event = {"event_id": f"e{u}", "event_type": "BOOK_SNAPSHOT" if kind == "snapshot" else "BOOK_DELTA",
             "venue": "BYBIT", "instrument_id": "BTC-USDT-SPOT", "exchange_ts_ns": ts_ms * 1_000_000,
             "source_ts_ns": ts_ms * 1_000_000, "receive_ts_ns": ts_ms * 1_000_000 + 30_000_000,
             "sequence": u, "sequence_start": u, "raw_ref": f"raw-{u}",
             "bids": [[p, q] for p, q in bids], "asks": [[p, q] for p, q in asks]}
    rows = [["bybit", "BTCUSDT", ts_ms * 1000, ts_ms * 1000 + 20_000, "true" if kind == "snapshot" else "false",
             side, p, q] for side, levels in (("bid", bids), ("ask", asks)) for p, q in levels]
    return event, rows


def stream(seconds=5):
    """A snapshot, then one delta every 250 ms that walks the touch up a tick."""
    out = [message(1_000, 10, "snapshot", [("100.0", "1"), ("99.9", "2")], [("100.1", "1"), ("100.2", "2")])]
    bid, ask, u = 100.0, 100.1, 11
    for i in range(1, seconds * 4):
        new_bid, new_ask = round(bid + 0.1, 1), round(ask + 0.1, 1)
        out.append(message(1_000 + i * 250, u, "delta", [(f"{new_bid:.1f}", "1"), (f"{bid:.1f}", "0")],
                           [(f"{new_ask:.1f}", "1"), (f"{ask:.1f}", "0")]))
        bid, ask, u = new_bid, new_ask, u + 1
    return out


def write(directory, ours, tardis_rows, *, gz=True):
    session = Path(directory) / "session"
    (session / "part-000001").mkdir(parents=True)
    with (session / "part-000001" / "canonical.jsonl").open("w") as handle:
        for event in ours:
            handle.write(json.dumps(event) + "\n")
    path = Path(directory) / ("tardis.csv.gz" if gz else "tardis.csv")
    with (gzip.open(path, "wt", newline="") if gz else path.open("w", newline="")) as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        writer.writerows(tardis_rows)
    return session, path


class TardisCompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stream = stream()

    def tearDown(self):
        self.tmp.cleanup()

    def run_compare(self, ours, rows, **kwargs):
        session, path = write(self.tmp.name, ours, rows, **{k: kwargs.pop(k) for k in ("gz",) if k in kwargs})
        return compare_with_tardis(session, "BYBIT", path, symbol="BTCUSDT", **kwargs)

    def test_identical_sources_match_everywhere(self):
        report = self.run_compare([e for e, _ in self.stream], [r for _, rows in self.stream for r in rows])
        self.assertEqual(set(report["samples"]), {"MATCH"})
        self.assertEqual(report["mismatch_share"], 0.0)
        self.assertEqual(report["event_counts"], {"ours_book_events": 20, "tardis_messages": 20})
        self.assertEqual(report["arrival_minus_exchange"]["ours"]["p50_ms"], 30.0)
        self.assertEqual(report["arrival_minus_exchange"]["tardis"]["p50_ms"], 20.0)
        self.assertEqual(report["ours_integrity"]["gaps"], 0)
        self.assertIn("compare-tardis --capture", report["reproduce"])
        self.assertEqual(len(report["inputs"]["tardis_sha256"]), 64)

    def test_deeper_tardis_book_is_compared_only_inside_our_depth(self):
        rows = [r for _, rows in self.stream for r in rows]
        rows.insert(1, ["bybit", "BTCUSDT", 1_000_000, 1_020_000, "true", "bid", "50.0", "9"])   # far below
        report = self.run_compare([e for e, _ in self.stream], rows, depth=2)
        self.assertEqual(set(report["samples"]), {"MATCH"})

    def test_a_level_we_lost_below_the_touch_is_a_depth_or_source_difference(self):
        ours = [e for e, _ in self.stream]
        rows = [r for _, rows in self.stream for r in rows]
        # Tardis has a bid at 99.95 (below the touch, inside our depth window) that we never saw.
        rows.append(["bybit", "BTCUSDT", 1_100 * 1000, 1_120 * 1000, "false", "bid", "99.95", "5"])
        report = self.run_compare(ours, sorted(rows, key=lambda r: r[2]))
        self.assertIn("DEPTH_OR_SOURCE_DIFFERENCE", report["samples"])
        first = next(d for d in report["discrepancies"] if d["classification"] == "DEPTH_OR_SOURCE_DIFFERENCE")
        self.assertIn("99.95", first["differing_prices"])
        self.assertTrue(first["ours_last_raw_ref"].startswith("raw-"))
        self.assertGreater(first["tardis_last_row"], 0)

    def test_our_gap_is_a_recovery_interval_not_a_mismatch(self):
        ours = [e for i, (e, _) in enumerate(self.stream) if i != 5]       # we lost one delta
        report = self.run_compare(ours, [r for _, rows in self.stream for r in rows])
        self.assertIn("LOCAL_RECOVERY", report["samples"])
        self.assertGreater(report["ours_integrity"]["gaps"], 0)
        self.assertEqual(report["samples"].get("UNRESOLVED", 0), 0)

    def test_silent_interval_on_our_side_is_ingestion_loss(self):
        # We stop receiving after 2 s while Tardis keeps going; our book stays VALID (no gap seen).
        ours = [e for e, _ in self.stream if e["exchange_ts_ns"] <= 2_000 * 1_000_000]
        report = self.run_compare(ours + [self.stream[-1][0] | {"exchange_ts_ns": 9_000 * 1_000_000,
                                                                  "event_type": "BOOK_SNAPSHOT"}],
                                  [r for _, rows in self.stream for r in rows])
        self.assertIn("LOCAL_INGESTION_LOSS", report["samples"])

    def test_one_message_late_is_timestamp_alignment(self):
        # Tardis stamps each delta 1 ms later than our capture did: ours is one message ahead at the sample.
        shifted = []
        for event, rows in self.stream:
            shifted += [r[:2] + [r[2] + (1_000 if r[4] == "false" else 0)] + r[3:] for r in rows]
        report = self.run_compare([e for e, _ in self.stream], shifted)
        self.assertIn("TIMESTAMP_ALIGNMENT", report["samples"])
        self.assertEqual(report["samples"].get("UNRESOLVED", 0), 0)

    def test_snapshot_rows_rebuild_the_tardis_book_and_plain_csv_reads(self):
        rows = [r for _, rows in self.stream for r in rows]
        rows += [["bybit", "BTCUSDT", 7_000_000, 7_020_000, "true", "bid", "200.0", "1"],
                 ["bybit", "BTCUSDT", 7_000_000, 7_020_000, "true", "ask", "200.1", "1"]]
        _, path = write(self.tmp.name, [], rows, gz=False)
        messages = list(iter_tardis_messages(path, "BTCUSDT"))
        self.assertEqual(len(messages), 21)                       # rows grouped back into messages
        self.assertTrue(messages[-1].is_snapshot)
        self.assertEqual(len(messages[-1].updates), 2)

    def test_cli_writes_the_report(self):
        session, path = write(self.tmp.name, [e for e, _ in self.stream], [r for _, rows in self.stream for r in rows])
        out = Path(self.tmp.name) / "report.json"
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        done = subprocess.run([sys.executable, "-m", "crypto_stat_arb.cli", "compare-tardis", "--capture", str(session),
                               "--venue", "BYBIT", "--tardis", str(path), "--symbol", "BTCUSDT", "--output", str(out)],
                              cwd=ROOT, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(out.read_text())["mismatch_share"], 0.0)


if __name__ == "__main__":
    unittest.main()
