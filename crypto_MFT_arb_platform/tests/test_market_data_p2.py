"""Audit P2s: envelope validation on live book events, monotonic freshness, feed-delay
(backlog) blocking and host-clock offset alerts."""

import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.live_feed import LiveFeedSettings, LiveOrderBookService
from crypto_stat_arb.models import BookState, MarketEvent
from crypto_stat_arb.time_integrity import TimeIntegrityMonitor, TimePolicy

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config" / "research.json"
URL = "wss://stream.bybit.com/v5/public/spot"
TOPIC = "orderbook.50.BTCUSDT"
MS = 1_000_000


def bybit(kind, u, ts_ms, bids=(("100", "1"),), asks=(("101", "1"),)):
    return json.dumps({"topic": TOPIC, "type": kind, "ts": ts_ms,
                       "data": {"s": "BTCUSDT", "u": u, "b": [list(x) for x in bids], "a": [list(x) for x in asks]}})


class Clock:
    def __init__(self, ns):
        self.ns = ns

    def __call__(self):
        return self.ns


class Service:
    """A live service driven frame by frame, with hand-set wall and monotonic clocks."""

    def __init__(self, directory, config=CONFIG, venues=("BYBIT",)):
        self.wall, self.mono = Clock(1_700_000_000_000 * MS), Clock(10_000 * MS)
        self.service = LiveOrderBookService(
            config, Path(directory), "p2", LiveFeedSettings(venues=venues, durable=False),
            clock_ns=self.wall, monotonic_ns=self.mono,
        )

    def frame(self, payload, channel=TOPIC):
        return self.service._process_websocket_frame("BYBIT", URL, payload, channel=channel)

    def wall_ms(self):
        return self.wall.ns // MS

    def close(self):
        self.service.recorder.close({})
        self.service.ohlc_writer.close()
        self.service.ltp_writer.close()

    def logged(self, event_type):
        self.service.structured_logs  # written synchronously
        path = self.service.recorder.session_dir / "structured_logs.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        return [row for row in rows if row["event_type"] == event_type]


class LiveValidationTests(unittest.TestCase):
    def test_invalid_book_event_is_quarantined_and_never_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            live = Service(directory)
            live.frame(bybit("snapshot", 10, live.wall_ms()))
            # Normalises, but a negative update id fails the shared envelope checks.
            self.assertIsNone(live.frame(bybit("delta", -5, live.wall_ms(), bids=(("90", "9"),))))
            book = live.service.books["BYBIT"]
            self.assertEqual((book.sequence, book.state), (10, BookState.VALID))
            self.assertNotIn(90, [int(p) for p in book.bids])
            self.assertEqual(live.service.stats["BYBIT"].validation_errors, 1)
            quarantine = live.service.recorder._part_dir / "quarantine.jsonl"
            rows = [json.loads(line) for line in quarantine.read_text().splitlines()]
            self.assertEqual([row["stage"] for row in rows], ["VALIDATE"])
            self.assertIn("sequence must be non-negative", rows[0]["error"])
            live.close()


class MonotonicFreshnessTests(unittest.TestCase):
    def test_wall_clock_step_forward_does_not_make_a_fresh_book_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            live = Service(directory)
            live.frame(bybit("snapshot", 10, live.wall_ms()))
            live.wall.ns += 5_000 * MS                       # NTP step: +5 s on the wall clock
            live.mono.ns += 10 * MS                          # only 10 ms really elapsed
            gate = live.service.book_gate
            self.assertTrue(gate.allow(tuple(live.service.books.values()), live.wall.ns,
                                       live.service._book_ages()))
            self.assertEqual(live.service.books["BYBIT"].state, BookState.VALID)
            live.close()

    def test_silence_is_stale_even_if_the_wall_clock_steps_back(self):
        with tempfile.TemporaryDirectory() as directory:
            live = Service(directory)
            live.frame(bybit("snapshot", 10, live.wall_ms()))
            live.wall.ns -= 2_000 * MS                       # wall clock stepped back 2 s
            live.mono.ns += 3_000 * MS                       # but 3 s of real silence
            self.assertLess(live.service._remaining_fresh_seconds("BYBIT"), 0.01)   # recv times out now
            gate = live.service.book_gate
            self.assertFalse(gate.allow(tuple(live.service.books.values()), live.wall.ns,
                                        live.service._book_ages()))
            book = live.service.books["BYBIT"]
            self.assertEqual((book.state, book.invalid_reason), (BookState.STALE, "market_data_stale"))
            live.close()

    def test_replay_without_ages_still_uses_recorded_time(self):
        with tempfile.TemporaryDirectory() as directory:
            live = Service(directory)
            live.frame(bybit("snapshot", 10, live.wall_ms()))
            book = live.service.books["BYBIT"]
            gate = live.service.book_gate
            self.assertFalse(gate.allow((book,), book.last_receive_ts_ns + 2_000 * MS))
            self.assertEqual(book.state, BookState.STALE)
            live.close()


class BookStreamLivenessTests(unittest.TestCase):
    """Phase D, D7 (owner 8 Nov, Q-P2): a book is trusted only while its own stream is known to be alive. A quiet book on a live stream stays valid; a dead book stream is caught even while trades keep arriving."""

    @staticmethod
    def valid_after(live):
        book = live.service.books["BYBIT"]
        live.service.book_gate.allow((book,), live.wall.ns, live.service._book_ages())
        return book.state == BookState.VALID

    def test_a_quiet_book_on_a_live_stream_stays_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            live = Service(directory)
            live.frame(bybit("snapshot", 10, live.wall_ms()))
            for _ in range(5):                                     # 5 x 600 ms with no book update (limit 1 s), but a pong frame on the book connection each time
                live.mono.ns += 600 * MS
                live.wall.ns += 600 * MS
                live.frame(json.dumps({"success": True, "ret_msg": "pong", "op": "ping"}))
                self.assertTrue(self.valid_after(live))
            self.assertEqual(live.service.stats["BYBIT"].book_stream_frames, 6)
            live.close()

    def test_a_silent_book_stream_is_stale_even_while_trades_keep_arriving(self):
        with tempfile.TemporaryDirectory() as directory:
            live = Service(directory)
            live.frame(bybit("snapshot", 10, live.wall_ms()))
            for i in range(5):                                     # the trade stream is busy; the book stream says nothing (the test config's age limit is 1 s)
                live.mono.ns += 400 * MS
                live.wall.ns += 400 * MS
                live.service._process_trade_frame(
                    "BYBIT", URL, json.dumps({"topic": "publicTrade.BTCUSDT", "type": "snapshot", "data": [
                        {"s": "BTCUSDT", "i": f"t{i}", "T": live.wall_ms(), "p": "100", "v": "1", "S": "Buy"}]}),
                    channel="publicTrade.BTCUSDT")
            self.assertEqual(live.service.stats["BYBIT"].book_stream_frames, 1)
            self.assertFalse(self.valid_after(live))
            self.assertEqual(live.service.books["BYBIT"].invalid_reason, "market_data_stale")
            live.close()

    def test_a_frame_on_the_book_stream_that_is_not_a_book_update_still_proves_it_alive(self):
        with tempfile.TemporaryDirectory() as directory:
            live = Service(directory)
            live.frame(bybit("snapshot", 10, live.wall_ms()))
            live.mono.ns += 800 * MS
            live.wall.ns += 800 * MS
            live.frame("not json")                                 # garbage still came through the book connection
            live.mono.ns += 800 * MS
            live.wall.ns += 800 * MS
            self.assertTrue(self.valid_after(live))
            live.close()

    def test_a_pong_keeps_the_book_fresh_for_the_sessions_eligibility_rule_too(self):
        """D7d finding: the paper session's CrossVenueEligibilityPolicy re-applies a receive-time age rule to the live books (book.mark_stale on last_receive_ts_ns); a frame on the book stream must move that time as well."""
        from crypto_stat_arb.alignment import CrossVenueEligibilityPolicy
        with tempfile.TemporaryDirectory() as directory:
            live = Service(directory)
            live.frame(bybit("snapshot", 10, live.wall_ms()))
            policy = CrossVenueEligibilityPolicy(("BYBIT",), "BTC-USDT-SPOT", 500 * MS)
            books = {("BYBIT", "BTC-USDT-SPOT"): live.service.books["BYBIT"]}
            for _ in range(4):                                       # 4 x 400 ms of book quiet, a pong each time: eligible throughout
                live.wall.ns += 400 * MS
                live.mono.ns += 400 * MS
                live.frame(json.dumps({"success": True, "ret_msg": "pong", "op": "ping"}))
                self.assertTrue(policy.evaluate(books, live.wall.ns).eligible)
            self.assertEqual(live.service.books["BYBIT"].state, BookState.VALID)
            live.wall.ns += 700 * MS                                 # then a real silence on the stream
            self.assertFalse(policy.evaluate(books, live.wall.ns).eligible)
            live.close()

    def test_a_silence_is_logged_with_the_armed_timeout_and_the_book_state(self):
        with tempfile.TemporaryDirectory() as directory:
            live = Service(directory)
            live.frame(bybit("snapshot", 10, live.wall_ms()))
            live.mono.ns += 3_000 * MS
            live.service._mark_stale("BYBIT", armed_s=0.4, armed_state="VALID")
            rows = live.logged("BOOK_STREAM_SILENCE")
            self.assertEqual(len(rows), 1)
            payload = rows[0]["payload"]
            self.assertEqual((payload["armed_timeout_s"], payload["book_state_when_armed"]), (0.4, "VALID"))
            self.assertAlmostEqual(payload["stream_age_ms_at_fire"], 3000.0, places=3)
            live.close()

    def test_connection_start_forgets_the_old_liveness(self):
        with tempfile.TemporaryDirectory() as directory:
            live = Service(directory)
            live.frame(bybit("snapshot", 10, live.wall_ms()))
            live.service._begin_connection("BYBIT")
            self.assertIsNone(live.service._book_alive_mono_ns("BYBIT"))
            live.close()

    def test_heartbeat_setting_is_validated(self):
        with self.assertRaises(ValueError):
            LiveFeedSettings(venues=("BYBIT",), durable=False, book_heartbeat_seconds=-1.0).validate()


def event(venue, exchange_ms, receive_ms, sequence=1):
    return MarketEvent.from_dict({
        "event_type": "BOOK_DELTA", "venue": venue, "instrument_id": "BTC-USDT-SPOT",
        "exchange_ts_ns": exchange_ms * MS, "source_ts_ns": exchange_ms * MS,
        "receive_ts_ns": receive_ms * MS, "sequence": sequence,
    })


class FeedDelayTests(unittest.TestCase):
    def test_off_by_default(self):
        monitor = TimeIntegrityMonitor(TimePolicy(), ("BINANCE", "BYBIT"))
        assessment = monitor.observe(event("BINANCE", 1_000, 9_000), now_ns=9_000 * MS)
        self.assertFalse(assessment.blocking)

    def test_delayed_venue_blocks_its_own_and_its_peers_decisions_until_it_catches_up(self):
        monitor = TimeIntegrityMonitor(TimePolicy(max_feed_delay_ns=1_000 * MS), ("BINANCE", "BYBIT"))
        late = monitor.observe(event("BINANCE", 1_000, 4_000), now_ns=4_000 * MS)      # 3 s behind
        self.assertIn("FEED_DELAYED", late.flags)
        self.assertTrue(late.blocking)
        peer = monitor.observe(event("BYBIT", 4_000, 4_010), now_ns=4_010 * MS)        # fresh, but...
        self.assertEqual(peer.flags, ("PEER_FEED_DELAYED",))                            # Binance book is old
        self.assertTrue(peer.blocking)
        caught_up = monitor.observe(event("BINANCE", 4_015, 4_020, 2), now_ns=4_020 * MS)
        self.assertFalse(caught_up.blocking)
        self.assertFalse(monitor.observe(event("BYBIT", 4_020, 4_030, 2), now_ns=4_030 * MS).blocking)
        report = monitor.report()["venues"]
        self.assertEqual((report["BINANCE"]["feed_delayed_events"], report["BYBIT"]["feed_delayed_events"]), (1, 0))
        self.assertEqual(report["BINANCE"]["max_corrected_delay_ns"], 3_000 * MS)

    def test_delay_is_measured_after_the_host_offset_correction(self):
        # Host clock 300 ms behind the venue: raw latency is ~ -300 ms + network.
        policy = TimePolicy(max_feed_delay_ns=1_000 * MS, correct_host_offset=True,
                            negative_latency_tolerance_ns=5 * MS, host_offset_min_samples=20)
        monitor = TimeIntegrityMonitor(policy, ("BYBIT",))
        for i in range(60):
            monitor.observe(event("BYBIT", 10_000 + i * 10, 10_000 + i * 10 - 290, i), now_ns=0)
        # 900 ms raw latency is 1.19 s after correcting the -290 ms host offset: delayed.
        late = monitor.observe(event("BYBIT", 20_000, 20_900, 99), now_ns=20_900 * MS)
        self.assertIn("FEED_DELAYED", late.flags)


class LiveTimingAlertTests(unittest.TestCase):
    def config_with(self, directory, **time_policy):
        raw = json.loads(CONFIG.read_text())
        raw.setdefault("time", {}).update(time_policy)
        path = Path(directory) / "config.json"
        path.write_text(json.dumps(raw))
        return path

    def test_feed_delayed_and_caught_up_are_logged_and_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            live = Service(directory, config=self.config_with(directory, max_feed_delay_ms=1000))
            live.frame(bybit("snapshot", 10, live.wall_ms()))
            live.frame(bybit("delta", 11, live.wall_ms() - 3_000))        # 3 s backlog
            live.frame(bybit("delta", 12, live.wall_ms()))
            self.assertEqual([r["venue"] for r in live.logged("FEED_DELAYED")], ["BYBIT"])
            self.assertEqual(len(live.logged("FEED_CAUGHT_UP")), 1)
            summary = live.service._service_summary()["time_integrity"]
            self.assertEqual(summary["feed_delayed_events"], {"BYBIT": 1})
            self.assertEqual(summary["max_feed_delay_ns"], 1_000 * MS)
            live.close()

    def test_host_clock_far_behind_the_venue_raises_an_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            # The venue's clock is 400 ms ahead of ours on every frame (host clock behind).
            live = Service(directory, config=self.config_with(directory, correct_host_offset=True,
                                                              max_host_offset_ms=100))
            live.frame(bybit("snapshot", 10, live.wall_ms() + 400))
            for u in range(11, 220):
                live.wall.ns += 20 * MS
                live.frame(bybit("delta", u, live.wall_ms() + 400))
            alerts = live.logged("HOST_CLOCK_OFFSET")
            self.assertEqual(len(alerts), 1)                                  # once, on the transition
            self.assertEqual(alerts[0]["payload"]["state"], "UNSYNCED")
            offset = live.service._service_summary()["time_integrity"]["clock_offset_estimate_ns"]
            self.assertLessEqual(offset, -300 * MS)
            live.close()


if __name__ == "__main__":
    unittest.main()
