import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.models import MarketEvent
from crypto_stat_arb.replay import deterministic_order
from crypto_stat_arb.time_integrity import HostClockMonitor, TimeIntegrityMonitor, TimePolicy


def event(venue: str, receive: int, exchange: int, sequence: int = 1) -> MarketEvent:
    return MarketEvent.from_dict({
        "event_type": "BOOK_DELTA",
        "venue": venue,
        "instrument_id": "BTC-USDT-SPOT",
        "source_ts_ns": exchange,
        "exchange_ts_ns": exchange,
        "receive_ts_ns": receive,
        "sequence": sequence,
    })


class TimeIntegrityTests(unittest.TestCase):
    def test_recorded_receive_time_is_primary_order(self):
        later_exchange_but_first_received = event("BYBIT", 10, 9, 1)
        earlier_exchange_but_later_received = event("BINANCE", 11, 1, 1)
        ordered = deterministic_order([earlier_exchange_but_later_received, later_exchange_but_first_received])
        self.assertEqual(ordered, [later_exchange_but_first_received, earlier_exchange_but_later_received])

    def test_negative_and_impossible_latency_are_blocking(self):
        monitor = TimeIntegrityMonitor(TimePolicy(max_event_latency_ns=100))
        negative = monitor.observe(event("BINANCE", 90, 100), now_ns=90)
        impossible = monitor.observe(event("BINANCE", 1000, 100, 2), now_ns=1000)
        self.assertTrue(negative.blocking)
        self.assertTrue(impossible.blocking)
        self.assertEqual(monitor.report()["blocked_events"], 2)

    def test_feed_age_and_cross_venue_skew_are_reported(self):
        monitor = TimeIntegrityMonitor(TimePolicy(max_feed_age_ns=10, max_cross_venue_skew_ns=5))
        first = event("BINANCE", 100, 99)
        second = event("BYBIT", 120, 119)
        monitor.observe(first, now_ns=111)
        monitor.observe(second, now_ns=120)
        report = monitor.report()
        self.assertEqual(report["venues"]["BINANCE"]["stale_feed_events"], 1)
        self.assertEqual(report["cross_venue_alignment"]["breaches"], 1)

    def test_host_clock_offset_and_drift_states(self):
        clock = HostClockMonitor(max_offset_ns=100, max_drift_ppm=100)
        self.assertEqual(clock.record_sample(local_ts_ns=1050, reference_ts_ns=1000, observed_ts_ns=1000, source="NTP"), "SYNCED")
        self.assertEqual(clock.record_sample(local_ts_ns=2250, reference_ts_ns=2000, observed_ts_ns=2000, source="NTP"), "UNSYNCED")
        self.assertEqual(clock.report()["samples"], 2)

    def test_report_is_machine_readable(self):
        monitor = TimeIntegrityMonitor(TimePolicy(), ("BINANCE", "BYBIT"))
        monitor.observe(event("BINANCE", 101, 100), now_ns=101)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "time_integrity_report.json"
            monitor.write_report(path)
            report = json.loads(path.read_text())
        self.assertEqual(report["timestamp_unit"], "nanoseconds_since_unix_epoch_utc")
        self.assertIn("host_clock", report)


if __name__ == "__main__":
    unittest.main()
