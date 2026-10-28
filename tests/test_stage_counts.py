"""3c: received, applied and persisted counts agree; a frame with no disposition is caught."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.live_feed import LiveFeedSettings, LiveOrderBookService
from test_feed_failures import logged
from test_live_feed import FakeConnector, FakeWebSocket, StepClock
from test_rest_reconcile import CONFIG, D1, SNAPSHOT

SUBSCRIBED = json.dumps({"op": "subscribe", "success": True, "conn_id": "c"})
MALFORMED = "{not json"


def service_for(directory, frames, **settings):
    return LiveOrderBookService(
        CONFIG, Path(directory), "stages",
        LiveFeedSettings(venues=("BYBIT",), durable=False, **settings),
        websocket_connector=FakeConnector({"BYBIT": [FakeWebSocket(frames)]}), clock_ns=StepClock())


def release(service):
    """Close the files a service opens at construction when it is never run (Windows
    cannot delete a temporary directory holding open files)."""
    service.recorder.abort()
    service.ohlc_writer.close()
    service.ltp_writer.close()


async def run_until(service, frames):
    stats = service.stats["BYBIT"]

    async def stop():
        while stats.raw_frames < frames:
            await asyncio.sleep(0.001)
        service.request_stop()

    await asyncio.wait_for(asyncio.gather(service.run(), stop()), timeout=10)


class StageCountTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_frame_has_one_disposition_and_every_event_is_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            service = service_for(directory, [SUBSCRIBED, SNAPSHOT, MALFORMED, D1])
            await run_until(service, 4)
            manifest = json.loads((service.recorder.session_dir / "live_manifest.json").read_text())
            frames = manifest["frame_accounting"]
            self.assertEqual(frames["captured"], 4)
            self.assertEqual(frames["dispositions"], {"CANONICAL": 2, "CONTROL": 1, "REJECTED": 1})
            self.assertEqual((frames["unresolved"], frames["consistent"]), (0, True))
            stages = manifest["service"]["stage_counts"]
            self.assertTrue(stages["consistent"])
            self.assertEqual(stages["applied_vs_persisted"]["canonical"], {"applied": 2, "persisted": 2})
            part = json.loads((service.recorder.session_dir / "part-000001" / "live_part_manifest.json").read_text())
            self.assertEqual(part["stage_counts"]["canonical"], {"recorded": 2, "on_disk": 2})
            self.assertTrue(part["stage_counts"]["consistent"])
            self.assertEqual(logged(service, "STAGE_COUNT_MISMATCH"), [])

    async def test_a_frame_received_but_never_applied_or_rejected_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            service = service_for(directory, [SNAPSHOT, D1])
            real = service._normalize

            def lose_the_delta(venue, payload, *args):
                # An exception outside the handled ones: the connection drops and the frame,
                # already captured, gets no disposition (the silent loss 3c exists to catch).
                if payload.get("type") == "delta":
                    raise LookupError("unexpected")
                return real(venue, payload, *args)

            service._normalize = lose_the_delta
            await run_until(service, 2)
            frames = service.recorder.frame_accounting()
            self.assertEqual((frames["captured"], frames["unresolved"], frames["consistent"]), (2, 1, False))
            mismatch = logged(service, "STAGE_COUNT_MISMATCH")
            self.assertEqual(len(mismatch), 1)
            self.assertEqual(mismatch[0]["level"], "ERROR")
            self.assertEqual(mismatch[0]["payload"]["frames"]["unresolved"], 1)

    async def test_applied_but_not_persisted_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            service = service_for(directory, [SNAPSHOT, D1])
            await run_until(service, 2)
        service.stats["BYBIT"].canonical_events += 1          # applied once more than recorded
        counts = service.stage_counts()
        self.assertFalse(counts["consistent"])
        self.assertEqual(counts["applied_vs_persisted"]["canonical"], {"applied": 3, "persisted": 2})

    async def test_a_part_whose_file_disagrees_with_its_count_is_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            service = service_for(directory, [SNAPSHOT, D1])
            recorder = service.recorder
            close_part = recorder._close_part

            def lose_a_line():                                 # e.g. a write that never reached the file
                if recorder._part_dir is not None:
                    recorder._canonical_handle.flush()
                    path = recorder._part_dir / "canonical.jsonl"
                    lines = path.read_text().splitlines(keepends=True)
                    path.write_text("".join(lines[:-1]))
                close_part()

            recorder._close_part = lose_a_line
            await run_until(service, 2)
            part = json.loads((recorder.session_dir / "part-000001" / "live_part_manifest.json").read_text())
            self.assertEqual(part["stage_counts"]["canonical"], {"recorded": 2, "on_disk": 1})
            self.assertFalse(part["stage_counts"]["consistent"])


class RecorderAccountingTests(unittest.TestCase):
    def test_a_frame_counts_once_however_many_dispositions_it_gets(self):
        # A trade frame can carry several ticks (one record each, or a rejection per tick).
        with tempfile.TemporaryDirectory() as directory:
            service = service_for(directory, [])
            recorder = service.recorder
            wire = recorder.capture_wire(venue="BYBIT", channel="publicTrade", host="h", transport="WEBSOCKET",
                                         receive_ts_ns=1, original_frame="{}")
            for disposition in ("TRADE", "TRADE", "REJECTED"):
                recorder._resolve_frame(wire, disposition)
            frames = recorder.frame_accounting()
            self.assertEqual(frames["dispositions"], {"TRADE": 1})
            self.assertTrue(frames["consistent"])
            release(service)

    def test_a_trade_message_without_prints_is_a_control_frame_not_a_lost_one(self):
        with tempfile.TemporaryDirectory() as directory:
            service = service_for(directory, [])
            frame = json.dumps({"topic": "publicTrade.BTCUSDT", "type": "snapshot", "ts": 1, "data": []})
            self.assertEqual(service._process_trade_frame("BYBIT", "wss://x", frame, channel="publicTrade"), 0)
            frames = service.recorder.frame_accounting()
            self.assertEqual((frames["dispositions"], frames["consistent"]), ({"CONTROL": 1}, True))
            release(service)


if __name__ == "__main__":
    unittest.main()
