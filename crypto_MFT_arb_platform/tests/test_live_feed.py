import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.live_feed import (
    LiveFeedSettings,
    LiveOrderBookService,
    verify_wire_file,
)


ROOT = Path(__file__).parents[1]


class StepClock:
    def __init__(self, start: int = 1_000_000_000):
        self.value = start

    def __call__(self) -> int:
        self.value += 1_000_000
        return self.value


class FakeWebSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self._never = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def recv(self):
        await asyncio.sleep(0)
        if self.messages:
            return self.messages.pop(0)
        await self._never.wait()

    async def send(self, message):
        self.sent.append(message)


class FakeConnector:
    def __init__(self, connections):
        self.connections = {
            venue: list(items) for venue, items in connections.items()
        }
        self.urls = []

    def __call__(self, venue, url):
        self.urls.append((venue, url))
        if not self.connections[venue]:
            raise RuntimeError(f"no fake connection remaining for {venue}")
        return self.connections[venue].pop(0)


class RecordingPublisher:
    def __init__(self):
        self.books = []
        self.bars = []
        self.trades = []
        self.ltp_bars = []
        self.closed = False

    def publish_orderbook(self, book, event, published_ts_ns):
        self.books.append((book.venue, book.state.value, event, published_ts_ns))
        return True

    def publish_ohlc(self, bar, published_ts_ns):
        self.bars.append((bar, published_ts_ns))
        return True

    def publish_trade(self, tick, published_ts_ns):
        self.trades.append((tick, published_ts_ns))
        return True

    def publish_ltp_ohlc(self, bar, published_ts_ns):
        self.ltp_bars.append((bar, published_ts_ns))
        return True

    def stats_snapshot(self):
        return {
            "enabled": True,
            "orderbook_messages": len(self.books),
            "ohlc_messages": len(self.bars),
            "trade_messages": len(self.trades),
            "ltp_ohlc_messages": len(self.ltp_bars),
        }

    def close(self):
        self.closed = True


class LiveFeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_venue_service_captures_normalizes_and_reconstructs(self):
        binance = FakeWebSocket([
            json.dumps({
                "e": "depthUpdate", "E": 1000, "T": 1000,
                "s": "BTCUSDT", "U": 101, "u": 101,
                "b": [["100.0", "2"]], "a": [],
            })
        ])
        bybit = FakeWebSocket([
            json.dumps({
                "topic": "orderbook.50.BTCUSDT", "type": "snapshot", "ts": 1000,
                "data": {
                    "s": "BTCUSDT", "u": 10,
                    "b": [["100.0", "1"]], "a": [["101.0", "1"]],
                },
            }),
            json.dumps({
                "topic": "orderbook.50.BTCUSDT", "type": "delta", "ts": 1001,
                "data": {"s": "BTCUSDT", "u": 11, "b": [["100.0", "2"]], "a": []},
            }),
        ])
        connector = FakeConnector({"BINANCE": [binance], "BYBIT": [bybit]})
        publisher = RecordingPublisher()

        async def http_getter(url, timeout):
            self.assertIn("/depth?symbol=BTCUSDT", url)
            self.assertGreater(timeout, 0)
            return json.dumps({
                "lastUpdateId": 100,
                "bids": [["99.9", "1"]],
                "asks": [["101.0", "1"]],
            }), 1_001_000_000

        with tempfile.TemporaryDirectory() as directory:
            service = LiveOrderBookService(
                ROOT / "config" / "research.json",
                Path(directory),
                "two-venue-live",
                LiveFeedSettings(
                    max_events=4,
                    reconnect_min_seconds=0.001,
                    reconnect_max_seconds=0.002,
                    rotate_seconds=3600,
                    rotate_bytes=10_000_000,
                    durable=False,
                ),
                websocket_connector=connector,
                http_getter=http_getter,
                clock_ns=StepClock(),
                publisher=publisher,
            )
            manifest = await asyncio.wait_for(service.run(), timeout=2)

            self.assertEqual(manifest["canonical_events"], 4)
            self.assertEqual(manifest["wire_records"], 4)
            self.assertEqual(service.books["BINANCE"].sequence, 101)
            self.assertEqual(service.books["BYBIT"].sequence, 11)
            self.assertEqual(service.books["BINANCE"].state.value, "VALID")
            self.assertEqual(service.books["BYBIT"].state.value, "VALID")
            self.assertTrue(bybit.sent)
            self.assertGreaterEqual(len(publisher.books), 4)
            self.assertEqual(len(publisher.bars), 2)
            self.assertTrue(all(not bar.is_final for bar, _ in publisher.bars))
            self.assertTrue(publisher.closed)

            part = Path(directory) / "two-venue-live" / "part-000001"
            wires = [json.loads(x) for x in (part / "wire.jsonl").read_text().splitlines()]
            raws = [json.loads(x) for x in (part / "raw.jsonl").read_text().splitlines()]
            events = [json.loads(x) for x in (part / "canonical.jsonl").read_text().splitlines()]
            traces = [json.loads(x) for x in (part / "trace.jsonl").read_text().splitlines()]
            self.assertEqual(len(wires), len(raws))
            self.assertEqual(len(events), len(traces))
            self.assertEqual({x["wire_ref"] for x in raws}, {x["wire_ref"] for x in wires})
            self.assertEqual({x["raw_ref"] for x in events}, {x["raw_ref"] for x in raws})
            verification = verify_wire_file(part / "wire.jsonl")
            self.assertTrue(verification["valid"])
            self.assertEqual(verification["record_count"], 4)
            ohlc_rows = (Path(directory) / "two-venue-live" / "ohlc_1m.csv").read_text().splitlines()
            self.assertEqual(len(ohlc_rows), 3)
            monitoring = Path(directory) / "two-venue-live" / "pipeline_monitoring.json"
            metrics = Path(directory) / "two-venue-live" / "pipeline_metrics.prom"
            self.assertTrue(monitoring.exists())
            self.assertTrue(metrics.exists())
            self.assertEqual(json.loads(monitoring.read_text())["job"]["state"], "COMPLETE")
            log_dir = Path(directory) / "two-venue-live"
            self.assertTrue((log_dir / "structured_logs.jsonl").exists())
            self.assertTrue((log_dir / "structured_logs.sqlite").exists())
            log_rows = [
                json.loads(line) for line in
                (log_dir / "structured_logs.jsonl").read_text().splitlines()
            ]
            self.assertTrue(any(row["event_type"] == "CONNECTED" for row in log_rows))
            self.assertTrue(any(
                row["event_type"] == "BOOK_STATE" and row["source_event_id"]
                and row["raw_ref"] for row in log_rows
            ))
            monitor_row = json.loads(monitoring.read_text())
            self.assertEqual(
                monitor_row["structured_logs"]["records"], len(log_rows)
            )

    async def test_sequence_gap_forces_reconnect_and_snapshot_recovery(self):
        first = FakeWebSocket([
            json.dumps({
                "topic": "orderbook.50.BTCUSDT", "type": "snapshot", "ts": 1000,
                "data": {"s": "BTCUSDT", "u": 10, "b": [["100", "1"]], "a": [["101", "1"]]},
            }),
            json.dumps({
                "topic": "orderbook.50.BTCUSDT", "type": "delta", "ts": 1001,
                "data": {"s": "BTCUSDT", "u": 12, "b": [["100", "2"]], "a": []},
            }),
        ])
        second = FakeWebSocket([
            json.dumps({
                "topic": "orderbook.50.BTCUSDT", "type": "snapshot", "ts": 1002,
                "data": {"s": "BTCUSDT", "u": 20, "b": [["100", "2"]], "a": [["101", "2"]]},
            })
        ])
        connector = FakeConnector({"BYBIT": [first, second]})
        cancellations = []

        def cancel_callback(reason, now_ns):
            cancellations.append((reason, now_ns))
            return 2

        with tempfile.TemporaryDirectory() as directory:
            service = LiveOrderBookService(
                ROOT / "config" / "research.json",
                Path(directory),
                "gap-recovery",
                LiveFeedSettings(
                    venues=("BYBIT",),
                    max_events=3,
                    reconnect_min_seconds=0.001,
                    reconnect_max_seconds=0.002,
                    durable=False,
                ),
                websocket_connector=connector,
                clock_ns=StepClock(),
                cancel_callback=cancel_callback,
            )
            await asyncio.wait_for(service.run(), timeout=2)

            self.assertEqual(service.stats["BYBIT"].resyncs, 1)
            self.assertEqual(service.stats["BYBIT"].reconnects, 1)
            self.assertEqual(service.stats["BYBIT"].connections, 2)
            self.assertEqual(service.books["BYBIT"].state.value, "VALID")
            self.assertEqual(service.books["BYBIT"].sequence, 20)
            self.assertEqual(service.book_gate.cancel_requests, 1)
            self.assertEqual(service.book_gate.cancelled_orders, 2)
            self.assertEqual(service.book_gate.resume_count, 2)
            self.assertEqual(len(cancellations), 1)

    async def test_malformed_frame_is_preserved_before_parse_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            service = LiveOrderBookService(
                ROOT / "config" / "research.json",
                Path(directory),
                "malformed-frame",
                LiveFeedSettings(venues=("BYBIT",), durable=False),
                clock_ns=StepClock(),
            )
            result = service._process_websocket_frame(
                "BYBIT", "wss://stream.bybit.com/v5/public/spot", "{not-json"
            )
            self.assertIsNone(result)
            service.ohlc_writer.close()
            service.ltp_writer.close()
            service.recorder.close(service._service_summary())

            part = Path(directory) / "malformed-frame" / "part-000001"
            wires = (part / "wire.jsonl").read_text().splitlines()
            raws = (part / "raw.jsonl").read_text().splitlines()
            rejected = (part / "quarantine.jsonl").read_text().splitlines()
            self.assertEqual(len(wires), 1)
            self.assertEqual(len(raws), 0)
            self.assertEqual(len(rejected), 1)
            self.assertEqual(json.loads(wires[0])["frame_data"], "{not-json")
            log_rows = [
                json.loads(line) for line in
                (Path(directory) / "malformed-frame" / "structured_logs.jsonl")
                .read_text().splitlines()
            ]
            self.assertEqual(log_rows[-1]["event_type"], "FRAME_PARSE_REJECTED")
            self.assertEqual(log_rows[-1]["level"], "ERROR")

    async def test_trade_ticks_are_raw_preserved_resampled_and_published(self):
        publisher = RecordingPublisher()
        clock = StepClock()
        with tempfile.TemporaryDirectory() as directory:
            service = LiveOrderBookService(
                ROOT / "config" / "research.json",
                Path(directory),
                "trade-ltp",
                LiveFeedSettings(venues=("BYBIT",), durable=False),
                clock_ns=clock,
                publisher=publisher,
            )
            count = service._process_trade_frame(
                "BYBIT",
                "wss://stream.bybit.com/v5/public/spot",
                json.dumps({
                    "topic": "publicTrade.BTCUSDT",
                    "type": "snapshot",
                    "data": [
                        {"s": "BTCUSDT", "i": "a", "T": 1000,
                         "p": "100", "v": "1", "S": "Buy"},
                        {"s": "BTCUSDT", "i": "b", "T": 1001,
                         "p": "102", "v": "2", "S": "Sell"},
                    ],
                }),
                channel="publicTrade.BTCUSDT",
            )
            self.assertEqual(count, 2)
            for bar in service.ltp_resampler.flush_partial():
                service._emit_ltp_ohlc(bar)
            service.ohlc_artifact = service.ohlc_writer.close()
            service.ltp_artifact = service.ltp_writer.close()
            manifest = service.recorder.close(service._service_summary())

            part = Path(directory) / "trade-ltp" / "part-000001"
            self.assertEqual(len((part / "wire.jsonl").read_text().splitlines()), 1)
            self.assertEqual(len((part / "raw.jsonl").read_text().splitlines()), 1)
            self.assertEqual(len((part / "trades.jsonl").read_text().splitlines()), 2)
            self.assertEqual(manifest["trade_ticks"], 2)
            self.assertEqual(len(publisher.trades), 2)
            self.assertEqual(len(publisher.ltp_bars), 1)
            bar = publisher.ltp_bars[0][0]
            self.assertEqual((str(bar.open), str(bar.high), str(bar.low), str(bar.close)),
                             ("100", "102", "100", "102"))
            csv_rows = (Path(directory) / "trade-ltp" / "ltp_ohlc_1m.csv").read_text().splitlines()
            self.assertEqual(len(csv_rows), 2)


if __name__ == "__main__":
    unittest.main()
