"""Owner 17 Nov, A9 fixes: (a) the live recorder's bounded-interval flush writes byte-identical files, and (b) frames stamped at ARRIVAL make a backlog show up as book age and a resync.
The golden hashes in RecorderBytes are of the files the recorder wrote BEFORE the change (per-record flush, per-frame stat, asdict copies) for the same synthetic input."""
import asyncio
import hashlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb.adapters import normalize_binance_depth, normalize_bybit_depth
from crypto_stat_arb.live_feed import LiveFeedSettings, LiveOrderBookService, RotatingLiveRecorder

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_SINGLE_PART = {
    "canonical.jsonl": "2e893809cd0dd41ecda3559c24adc6abc3c6343dc51024062a89439c301fb990",
    "quarantine.jsonl": "6cdeb30d64be76602d33ec413aa47314983ecf946014ea6c3f1634c7b7421c3a",
    "raw.jsonl": "edd9151c833bdc04535ec3527db2be24d070e614527c998a8196818ee53464fe",
    "trace.jsonl": "529e20ac409f04c62e5f6ac2575503117332e821346ab450416b3e66743ea665",
    "trades.jsonl": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "wire.jsonl": "0f4491092c406361e418664c6248dcb391a579ccd858a5e788d94ef20643546d",
}
GOLDEN_ROTATED = (13, "7dc2d59f5da0dc9952df62be61c269dbc5263d6cc87e5713699820feace2809c")        # parts, sha256 of "name:hash" lines for rotate_bytes 60000


def drive(out: Path, **kw) -> RotatingLiveRecorder:
    """The fixed synthetic input: 300 frames on two venues (some binary, one control and one rejected every so often) through capture, raw append and the canonical record."""
    clock = iter(range(1_700_000_000_000_000_000, 1_700_000_000_000_000_000 + 10 ** 9, 1000))
    rec = RotatingLiveRecorder(out, "golden", rotate_seconds=3600.0, rotate_bytes=kw.pop("rotate_bytes", 256 * 1024 * 1024), durable=False, clock_ns=lambda: next(clock), **kw)
    resolve = lambda venue, symbol: "SOL-USDT-PERP"
    for i in range(300):
        ts = 1_700_000_000_000_000_000 + i * 1_000_000
        if i % 2 == 0:
            frame = json.dumps({"e": "depthUpdate", "E": 1_700_000_000_000 + i, "T": 1_700_000_000_000 + i, "s": "SOLUSDT", "U": 10 + i, "u": 10 + i, "pu": 9 + i,
                                "b": [[f"{100 - 0.01 * (i % 7):.2f}", str(1 + i % 5)]], "a": [[f"{100.1 + 0.01 * (i % 7):.2f}", "3.5"]]})
            wire = rec.capture_wire(venue="BINANCE", channel="depth@100ms", host="fstream.binance.com", transport="WEBSOCKET", receive_ts_ns=ts, original_frame=frame)
            payload = json.loads(frame)
            raw = rec.append_payload(wire, payload=payload, vendor="binance", adapter_version="binance-depth-v1")
            ev = normalize_binance_depth(payload, ts, resolve, raw_ref=raw.raw_ref)
        else:
            frame = json.dumps({"topic": "orderbook.50.SOLUSDT", "type": "delta", "ts": 1_700_000_000_000 + i, "data": {"s": "SOLUSDT", "u": i, "seq": i, "b": [["99.9", "2"]], "a": [["100.2", "1.25"]]}})
            wire = rec.capture_wire(venue="BYBIT", channel="orderbook.50.SOLUSDT", host="stream.bybit.com", transport="WEBSOCKET", receive_ts_ns=ts, original_frame=frame.encode() if i % 11 == 1 else frame)
            payload = json.loads(frame)
            raw = rec.append_payload(wire, payload=payload, vendor="bybit", adapter_version="bybit-depth-v1")
            ev = normalize_bybit_depth(payload, ts, resolve, raw_ref=raw.raw_ref)
        if i % 17 == 5:
            rec.record_control(wire, raw)
        elif i % 23 == 7:
            rec.record_rejection(wire, ValueError("bad"), raw_ref=raw.raw_ref, stage="normalize")
        else:
            rec.record_event(wire, raw, ev, i)
    rec.close({"x": 1})
    return rec


def file_hashes(out: Path) -> dict[str, str]:
    return {str(p.relative_to(out / "golden")): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((out / "golden").rglob("*"))
            if p.is_file() and p.name not in ("live_manifest.json", "live_part_manifest.json", "raw_manifest.json")}


class RecorderBytes(unittest.TestCase):
    def check(self, **kw):
        with tempfile.TemporaryDirectory() as d:
            drive(Path(d), **kw)
            got = file_hashes(Path(d))
        return {k.split("/")[-1]: v for k, v in got.items() if k.startswith("part-000001/")}, got

    def test_the_bounded_flush_writes_the_same_bytes_as_the_per_record_flush(self):
        for kw in ({}, {"flush_every": 200, "flush_interval_ns": 250_000_000}, {"flush_every": 7, "flush_interval_ns": 1}):
            single, _ = self.check(**kw)
            self.assertEqual(single, GOLDEN_SINGLE_PART, kw)

    def test_rotation_points_and_every_part_are_the_same_with_the_byte_counters(self):
        for kw in ({"rotate_bytes": 60000}, {"rotate_bytes": 60000, "flush_every": 200, "flush_interval_ns": 250_000_000}):
            with tempfile.TemporaryDirectory() as d:
                rec = drive(Path(d), **kw)
                got = file_hashes(Path(d))
            self.assertEqual(len(rec.parts), GOLDEN_ROTATED[0], kw)
            self.assertEqual(hashlib.sha256("\n".join(f"golden/{k}:{v}" for k, v in sorted(got.items())).encode()).hexdigest(), GOLDEN_ROTATED[1], kw)

    def test_buffered_writes_reach_the_disk_on_the_count_the_interval_the_monitor_tick_and_close(self):
        with tempfile.TemporaryDirectory() as d:
            clock = iter(range(1, 10 ** 9))
            rec = RotatingLiveRecorder(Path(d), "s", rotate_seconds=3600.0, rotate_bytes=10 ** 9, durable=False, clock_ns=lambda: next(clock), flush_every=1000, flush_interval_ns=10 ** 12)
            wire = rec.capture_wire(venue="BINANCE", channel="c", host="h", transport="WEBSOCKET", receive_ts_ns=1, original_frame="{}")
            path = rec.session_dir / "part-000001" / "wire.jsonl"
            self.assertEqual(path.stat().st_size, 0)                      # buffered: nothing flushed yet
            rec.flush_now()
            self.assertGreater(path.stat().st_size, 0)                    # the explicit flush (and part close, and shutdown) writes it
            self.assertEqual(rec.flush_count, 1)
            rec.close({})
            self.assertEqual(rec.parts[0]["stage_counts"]["consistent"], True)
        with tempfile.TemporaryDirectory() as d:                          # the count bound
            rec = RotatingLiveRecorder(Path(d), "s", rotate_seconds=3600.0, rotate_bytes=10 ** 9, durable=False, flush_every=4, flush_interval_ns=10 ** 12)
            path = None
            for i in range(3):
                rec.capture_wire(venue="B", channel="c", host="h", transport="W", receive_ts_ns=i + 1, original_frame="{}")
            path = rec.session_dir / "part-000001" / "wire.jsonl"
            self.assertEqual(path.stat().st_size, 0)
            rec.capture_wire(venue="B", channel="c", host="h", transport="W", receive_ts_ns=9, original_frame="{}")        # the fourth write call reaches the bound
            self.assertGreater(path.stat().st_size, 0)
            rec.close({})
        with tempfile.TemporaryDirectory() as d:                          # the time bound, found by the monitor tick on a quiet feed
            rec = RotatingLiveRecorder(Path(d), "s", rotate_seconds=3600.0, rotate_bytes=10 ** 9, durable=False, flush_every=1000, flush_interval_ns=20_000_000)
            rec.capture_wire(venue="B", channel="c", host="h", transport="W", receive_ts_ns=1, original_frame="{}")
            path = rec.session_dir / "part-000001" / "wire.jsonl"
            self.assertEqual(path.stat().st_size, 0)
            rec.flush_if_due()                                            # not yet due
            self.assertEqual(path.stat().st_size, 0)
            time.sleep(0.03)
            rec.flush_if_due()
            self.assertGreater(path.stat().st_size, 0)
            rec.close({})

    def test_the_default_of_the_class_is_the_old_per_record_flush(self):
        with tempfile.TemporaryDirectory() as d:
            rec = RotatingLiveRecorder(Path(d), "s", rotate_seconds=3600.0, rotate_bytes=10 ** 9, durable=False)
            rec.capture_wire(venue="B", channel="c", host="h", transport="W", receive_ts_ns=1, original_frame="{}")
            self.assertGreater((rec.session_dir / "part-000001" / "wire.jsonl").stat().st_size, 0)
            rec.close({})


class TimedBybitSocket:
    """A Bybit book connection that hands frame i over at start + i / rate (wall clock) and, once a frame has arrived, hands it over at once, as a buffered websocket does."""

    def __init__(self, n, rate):
        self.n, self.rate, self.i, self.start = n, rate, 0, None
        self._never = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def send(self, message):
        return None

    async def recv(self):
        if self.i >= self.n:
            await self._never.wait()
        if self.start is None:
            self.start = time.perf_counter()
        wait = self.start + self.i / self.rate - time.perf_counter()
        if wait > 0:
            await asyncio.sleep(wait)
        i, self.i = self.i, self.i + 1
        if i == 0:
            return json.dumps({"topic": "orderbook.50.BTCUSDT", "type": "snapshot", "ts": 1000, "data": {"s": "BTCUSDT", "u": 1, "seq": 1, "b": [["100.0", "1"]], "a": [["101.0", "1"]]}})
        return json.dumps({"topic": "orderbook.50.BTCUSDT", "type": "delta", "ts": 1000 + i, "data": {"s": "BTCUSDT", "u": 1 + i, "seq": 1 + i, "b": [["100.0", str(1 + i % 9)]], "a": []}})


class ArrivalStamp(unittest.IsolatedAsyncioTestCase):
    """A processor that falls behind must show it: with arrival stamping the book ages, and the backlog beyond the age limit resynchronises the connection; without it the same run looks perfectly healthy."""

    async def run_service(self, arrival_stamp: bool, per_frame_cost_s: float, health_block: bool = False, keep: dict | None = None):
        ages = []
        with tempfile.TemporaryDirectory() as d:
            service = LiveOrderBookService(ROOT / "config" / "research.json", Path(d), "a9", LiveFeedSettings(venues=("BYBIT",), durable=False, reconnect_min_seconds=0.01, reconnect_max_seconds=0.02,
                                                                                                           arrival_stamp=arrival_stamp, trade_streams_enabled=False, udp_enabled=False, feed_health_block=health_block))
            sockets = []

            def connector(venue, url):
                sockets.append(TimedBybitSocket(1700, 1000.0))
                return sockets[-1]
            service.websocket_connector = connector
            real = service._apply_and_record

            def slow(wire, raw, event):
                ages.append((time.time_ns() - event.receive_ts_ns) / 1e6)         # how old the frame is, by the stamp the service gave it, when its turn comes
                end = time.perf_counter() + per_frame_cost_s
                while time.perf_counter() < end:
                    pass                                                  # a processor that costs per_frame_cost_s of CPU per frame
                return real(wire, raw, event)
            service._apply_and_record = slow

            runner = asyncio.create_task(service.run())
            await asyncio.sleep(2.6)
            service.request_stop()
            await asyncio.wait_for(runner, timeout=10)
            if keep is not None:                                          # the recorded files of the first part and the service's own time report, read before the folder goes
                part = Path(d) / "a9" / "part-000001"
                keep["wire"] = [json.loads(line) for line in (part / "wire.jsonl").read_text().splitlines()]
                keep["trace"] = [json.loads(line) for line in (part / "trace.jsonl").read_text().splitlines()]
                keep["manifest"] = json.loads((part / "live_part_manifest.json").read_text())
                keep["timing"] = service.time_monitor.report()
                keep["stats"] = service.stats["BYBIT"]
            return {"max_age_ms": max(ages) if ages else None, "arrival": dict(service.arrival_stats.get("BYBIT", {})), "stale": service.books["BYBIT"].stats.stale_transitions,
                    "stale_disconnects": service.stats["BYBIT"].stale_disconnects, "frames": service.stats["BYBIT"].book_stream_frames, "connections": len(sockets)}

    async def test_a_backlog_is_invisible_without_arrival_stamps_and_visible_with_them(self):
        off = await self.run_service(False, 0.003)                        # frames arrive at 1000/s, the processor handles about 330/s
        on = await self.run_service(True, 0.003)
        self.assertLess(off["max_age_ms"], 200.0, off)                    # the old stamp is taken at processing: the book always looks fresh
        self.assertEqual(off["arrival"], {})
        self.assertGreater(on["max_age_ms"], 500.0, on)                   # arrival stamps: the book age grows with the backlog
        self.assertGreaterEqual(on["arrival"]["backlog_resyncs"], 1, on)  # and past the book age limit (1 s) the backlog is discarded and the connection resynchronised
        self.assertGreater(on["arrival"]["backlog_frames_discarded"], 0, on)
        self.assertGreaterEqual(on["stale_disconnects"], 1, on)
        self.assertGreater(on["connections"], 1, on)

    async def test_a_processor_that_keeps_up_changes_nothing(self):
        on = await self.run_service(True, 0.0)
        self.assertEqual(on["arrival"].get("backlog_resyncs"), 0, on)
        self.assertEqual(on["connections"], 1, on)
        self.assertLess(on["max_age_ms"], 200.0, on)


class W1LiveRecords(unittest.IsolatedAsyncioTestCase):
    """W1 (owner 17 Nov), the live side: the monotonic receive stamp on the wire and trace records (additive, only with arrival stamping on), the recorder's health block in the part manifest
    (off by default), and the service-side time monitor whose feed age was always zero."""

    async def test_with_arrival_stamps_every_book_wire_record_and_its_trace_row_carry_the_monotonic_stamp_and_the_chain_still_verifies(self):
        keep = {}
        await ArrivalStamp.run_service(self, True, 0.0, keep=keep)
        wire = keep["wire"]
        self.assertGreater(len(wire), 100)
        self.assertTrue(all(isinstance(r.get("receive_mono_ns"), int) and r["wire_schema_version"] == "1.1.0" for r in wire))
        monos = [r["receive_mono_ns"] for r in wire]
        self.assertEqual(monos, sorted(monos))                                           # arrival order
        previous = wire[0]["previous_record_hash"]
        for r in wire:                                                                   # the hash chain covers the new field
            self.assertEqual(r["previous_record_hash"], previous)
            body = {k: v for k, v in r.items() if k != "record_hash"}
            self.assertEqual(hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), r["record_hash"])
            previous = r["record_hash"]
        by_ref = {r["wire_ref"]: r["receive_mono_ns"] for r in wire}
        canonical = [t for t in keep["trace"] if t["disposition"] == "CANONICAL"]
        self.assertTrue(canonical and all(t["receive_mono_ns"] == by_ref[t["wire_ref"]] for t in canonical))

    async def test_without_arrival_stamps_the_records_have_no_such_field_and_the_old_schema_version(self):
        keep = {}
        await ArrivalStamp.run_service(self, False, 0.0, keep=keep)
        self.assertTrue(all("receive_mono_ns" not in r and r["wire_schema_version"] == "1.0.0" for r in keep["wire"]))
        self.assertTrue(all("receive_mono_ns" not in t for t in keep["trace"]))
        self.assertNotIn("feed_health", keep["manifest"])

    async def test_the_part_manifest_carries_the_feed_health_block_only_when_asked(self):
        keep = {}
        await ArrivalStamp.run_service(self, True, 0.0, health_block=True, keep=keep)
        block = keep["manifest"]["feed_health"]
        venue = block["venues"]["BYBIT"]
        self.assertEqual((venue["book_stream_frames"], venue["canonical_events"]), (keep["stats"].book_stream_frames, keep["stats"].canonical_events))
        self.assertEqual(venue["timing"]["events"], keep["timing"]["venues"]["BYBIT"]["events"])
        self.assertEqual(venue["arrival"]["backlog_resyncs"], 0)
        self.assertTrue(block["arrival_stamp"])
        self.assertIn("suspend_count", block["strategy_gate"])

    async def test_the_service_side_feed_age_is_real_with_arrival_stamps_and_zero_without(self):
        """Audit A1 1.4 f4: the service called its time monitor with now = the event's receive time, so the feed age was always 0 and STALE_FEED could not fire."""
        on, off = {}, {}
        await ArrivalStamp.run_service(self, True, 0.003, keep=on)                       # a processor three times too slow: frames wait
        await ArrivalStamp.run_service(self, False, 0.003, keep=off)
        self.assertGreater(on["timing"]["venues"]["BYBIT"]["feed_age_ns_max"], 300_000_000)
        self.assertEqual(off["timing"]["venues"]["BYBIT"]["feed_age_ns_max"], 0)           # LEGACY without arrival stamps: unchanged


if __name__ == "__main__":
    unittest.main()
