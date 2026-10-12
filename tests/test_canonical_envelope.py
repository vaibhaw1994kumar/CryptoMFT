import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.canonical import (
    CallbackEventSink,
    CanonicalEventDispatcher,
    CanonicalEventRecorder,
    validate_canonical_event,
)
from crypto_stat_arb.adapters import normalize_binance_depth
from crypto_stat_arb.models import MarketEvent


ROOT = Path(__file__).parents[1]


class CanonicalEnvelopeTests(unittest.TestCase):
    def load_sample(self, venue: str) -> MarketEvent:
        row = json.loads(
            (ROOT / "data" / "samples" / "canonical" / f"{venue}_book_snapshot.json").read_text()
        )
        return MarketEvent.from_dict(row)

    def test_published_samples_have_complete_envelopes(self):
        for venue in ("binance", "bybit"):
            event = self.load_sample(venue)
            validate_canonical_event(event)
            self.assertEqual(event.schema_version, "1.2.0")
            self.assertGreater(event.source_ts_ns, 0)
            self.assertTrue(event.raw_ref)

    def test_same_immutable_object_reaches_strategy_and_recorder(self):
        event = self.load_sample("binance")
        strategy_object_ids = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            with CanonicalEventRecorder(path) as recorder:
                recorder_object_ids = []
                original = recorder.on_event

                def observed(candidate):
                    recorder_object_ids.append(id(candidate))
                    original(candidate)

                recorder.on_event = observed
                dispatcher = CanonicalEventDispatcher((
                    CallbackEventSink(lambda candidate: strategy_object_ids.append(id(candidate))),
                    recorder,
                ))
                dispatcher.dispatch(event)
            self.assertEqual(strategy_object_ids, [id(event)])
            self.assertEqual(recorder_object_ids, [id(event)])
            recorded = json.loads(path.read_text())
            self.assertEqual(recorded["event_id"], event.event_id)
            self.assertEqual(recorded["raw_ref"], event.raw_ref)

    def test_live_or_paper_delivery_requires_raw_reference(self):
        event = MarketEvent.from_dict({
            "event_type": "CONTROL", "venue": "BINANCE", "instrument_id": "BTC-USDT-SPOT",
            "source_ts_ns": 1, "exchange_ts_ns": 1, "receive_ts_ns": 1, "sequence": 1,
            "adapter_version": "test-v1",
        })
        with self.assertRaisesRegex(ValueError, "raw_ref"):
            validate_canonical_event(event)

    def test_negative_receive_latency_is_flagged(self):
        event = normalize_binance_depth(
            {"s": "BTCUSDT", "u": 7, "E": 2000, "b": [], "a": []},
            1_000_000_000,
            lambda _venue, _symbol: "BTC-USDT-SPOT",
            raw_ref="raw-7",
        )
        self.assertIn("NEGATIVE_RECEIVE_LATENCY", event.quality_flags)
        schema = json.loads((ROOT / "docs" / "schemas" / "canonical_event_envelope.schema.json").read_text())
        self.assertIn("quality_flags", schema["required"])
        self.assertEqual(schema["properties"]["schema_version"]["const"], "1.2.0")


if __name__ == "__main__":
    unittest.main()
