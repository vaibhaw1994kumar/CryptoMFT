import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.pipeline import normalize_raw_file
from crypto_stat_arb.raw_capture import capture_envelopes, verify_raw_file


ROOT = Path(__file__).parents[1]


def sample_envelopes():
    with (ROOT / "data" / "samples" / "raw_depth.jsonl").open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class RawCaptureTests(unittest.TestCase):
    def test_capture_is_hash_chained_and_verifiable(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            manifest = capture_envelopes(sample_envelopes(), output, "session-001")
            raw_path = output / "session-001" / "raw.jsonl"
            verification = verify_raw_file(raw_path)
            self.assertEqual(manifest["record_count"], 2)
            self.assertTrue(verification["valid"])
            self.assertEqual(
                verification["terminal_record_hash"], manifest["terminal_record_hash"]
            )
            records = [json.loads(line) for line in raw_path.read_text().splitlines()]
            self.assertEqual(records[0]["host"], "fstream.binance.com")
            self.assertEqual(records[0]["vendor"], "BINANCE")
            self.assertEqual(records[0]["adapter_version"], "binance-depth-v1")
            self.assertEqual(records[0]["frame_capture_mode"], "WIRE_TEXT")
            self.assertEqual(json.loads(records[0]["frame_data"]), records[0]["payload"])
            self.assertEqual(records[1]["previous_record_hash"], records[0]["record_hash"])

    def test_payload_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            capture_envelopes(sample_envelopes(), output, "session-002")
            raw_path = output / "session-002" / "raw.jsonl"
            tampered = output / "tampered.jsonl"
            tampered.write_text(
                raw_path.read_text().replace("59999.90", "59998.90", 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError, "(payload|original frame) checksum mismatch"
            ):
                verify_raw_file(tampered)

    def test_raw_to_canonical_trace_resolves_exact_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            capture_envelopes(sample_envelopes(), output, "representative-session")
            raw_path = output / "representative-session" / "raw.jsonl"
            canonical = output / "events.jsonl"
            manifest = normalize_raw_file(
                ROOT / "config" / "research.json", raw_path, canonical
            )
            raw_records = [json.loads(line) for line in raw_path.read_text().splitlines()]
            events = [json.loads(line) for line in canonical.read_text().splitlines()]
            traces = [
                json.loads(line)
                for line in Path(manifest["trace"]).read_text().splitlines()
            ]
            self.assertEqual(len(traces), 2)
            self.assertEqual(events[0]["raw_ref"], raw_records[0]["raw_ref"])
            self.assertEqual(traces[0]["raw_ref"], raw_records[0]["raw_ref"])
            self.assertEqual(traces[0]["canonical_event_id"], events[0]["event_id"])


if __name__ == "__main__":
    unittest.main()
