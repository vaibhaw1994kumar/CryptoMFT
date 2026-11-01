"""3b: a venue reconnecting repeatedly without a valid book raises an alarm."""

import asyncio
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.live_feed import LiveFeedSettings, LiveOrderBookService
from crypto_stat_arb.monitoring import MonitoringThresholds, PipelineMonitor
from test_feed_failures import logged
from test_live_feed import FakeConnector, FakeWebSocket, StepClock
from test_rest_reconcile import CONFIG, D1, SNAPSHOT


class DroppedWebSocket(FakeWebSocket):
    """A connection that drops before any data (a refused or reset socket)."""

    def __init__(self):
        super().__init__([])

    async def recv(self):
        await asyncio.sleep(0)
        raise ConnectionError("connection reset by peer")


class SecondsClock:
    """Monotonic clock advancing ``step`` seconds per reading."""

    def __init__(self, step: float):
        self.now, self.step = 0, int(step * 1_000_000_000)

    def __call__(self) -> int:
        self.now += self.step
        return self.now


async def run(failures: int, *, count=5, window=300.0, step=1.0, recover=True):
    sockets = [DroppedWebSocket() for _ in range(failures)]
    if recover:
        sockets.append(FakeWebSocket([SNAPSHOT, D1]))
    directory = tempfile.mkdtemp()
    service = LiveOrderBookService(
        CONFIG, Path(directory), "storm", LiveFeedSettings(
            venues=("BYBIT",), durable=False, reconnect_min_seconds=0.001, reconnect_max_seconds=0.002,
            reconnect_alarm_count=count, reconnect_alarm_window_seconds=window),
        websocket_connector=FakeConnector({"BYBIT": sockets}), clock_ns=StepClock(),
        monotonic_ns=SecondsClock(step))
    stats = service.stats["BYBIT"]

    async def stop_when_done():
        while stats.reconnects < failures or (recover and stats.book_syncs < 1):
            await asyncio.sleep(0.001)
        service.request_stop()

    await asyncio.wait_for(asyncio.gather(service.run(), stop_when_done()), timeout=10)
    return service, stats


class ReconnectStormTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_storm_raises_one_alarm_and_a_valid_book_clears_it(self):
        service, stats = await run(8)
        self.assertEqual(stats.reconnect_storms, 1)                    # once per episode, not per reconnect
        self.assertFalse(stats.reconnect_storm_active)                 # the VALID book cleared it
        storm = logged(service, "RECONNECT_STORM")
        self.assertEqual(len(storm), 1)
        self.assertEqual(storm[0]["level"], "ERROR")
        self.assertEqual(storm[0]["payload"]["reconnects_in_window"], 5)
        self.assertIn("connection reset by peer", storm[0]["payload"]["last_error"])
        self.assertEqual(len(logged(service, "RECONNECT_STORM_CLEARED")), 1)

    async def test_an_ongoing_storm_stays_active_and_is_a_critical_monitoring_metric(self):
        service, stats = await run(6, recover=False)
        self.assertTrue(stats.reconnect_storm_active)
        report = service._write_monitoring(service.clock_ns(), final=True)
        metric = next(m for m in report["metrics"] if m["name"] == "active_reconnect_storms")
        self.assertEqual((metric["value"], metric["breached"], metric["health_on_breach"]), (1, True, "CRITICAL"))
        self.assertIn("ACTIVE_RECONNECT_STORMS", {alert["code"] for alert in report["alerts"]})

    async def test_reconnects_spread_beyond_the_window_do_not_alarm(self):
        # One reconnect every 100 s against a 300 s window: never 5 inside it.
        _, stats = await run(8, step=100.0)
        self.assertEqual(stats.reconnect_storms, 0)

    async def test_fewer_reconnects_than_the_threshold_do_not_alarm(self):
        _, stats = await run(4)
        self.assertEqual(stats.reconnect_storms, 0)


class SettingsAndMonitorTests(unittest.TestCase):
    def test_settings_are_validated(self):
        with self.assertRaisesRegex(ValueError, "at least 2"):
            LiveFeedSettings(reconnect_alarm_count=1).validate()
        with self.assertRaisesRegex(ValueError, "must be positive"):
            LiveFeedSettings(reconnect_alarm_window_seconds=0).validate()

    def test_monitor_reports_no_storm_as_healthy(self):
        report = PipelineMonitor(MonitoringThresholds()).build(
            mode="LIVE", generated_ts_ns=1, correlation_id="c", job={}, data={}, partitions={"completeness": 1.0},
            books={}, versions={}, performance={}, live={"active_reconnect_storms": 0})
        metric = next(m for m in report["metrics"] if m["name"] == "active_reconnect_storms")
        self.assertFalse(metric["breached"])


if __name__ == "__main__":
    unittest.main()
