"""WP3 timing primitives (src/new_backtest/timing.py): timelines are separate streams, the clock offset is a fixed measured input,
the timer wheel fires in deadline order with an explicit equal-time policy, and the dead-feed classifier works on absolute age."""
from __future__ import annotations

import unittest

from new_backtest.config import LATENCY_PRESETS
from new_backtest.timing import (NS, TIMELINES, ClockConfig, FixedOffsetAligner, TimelineSpec, Timelines, TimerWheel, TimingConfig,
                                 VenueTimelines, feed_state, s_to_ns)


def cfg(**kw) -> TimingConfig:
    return TimingConfig.from_latency_preset(LATENCY_PRESETS["singapore"], **kw)


class TimelineTests(unittest.TestCase):
    def test_preset_maps_each_timeline_once_and_reports_sources(self):
        c = cfg()
        b = c.venues["bybit"]
        self.assertEqual((b.entry.mean_ms, b.cancel.mean_ms, b.feed.mean_ms, b.ack_report.mean_ms, b.fill_report.mean_ms, b.cancel_report.mean_ms),
                         (6.0, 6.0, 2.0, 2.0, 2.0, 2.0))
        self.assertTrue(all(t.get(n).source.startswith("ASSUMED") for t in c.venues.values() for n in TIMELINES))
        d = c.describe()
        self.assertEqual(set(d["venues"]["binance"]), set(TIMELINES))

    def test_each_timeline_is_its_own_stream(self):
        a, b = Timelines(cfg()), Timelines(cfg())
        xs = [a.sample_ns("bybit", "entry") for _ in range(5)]
        b.sample_ns("bybit", "fill_report")                       # a draw on another timeline...
        ys = [b.sample_ns("bybit", "entry") for _ in range(5)]
        self.assertEqual(xs, ys)                                   # ...does not move this one
        self.assertNotEqual(xs, [a.sample_ns("binance", "entry") for _ in range(5)])
        self.assertTrue(all(isinstance(x, int) and x > 0 for x in xs))

    def test_zero_jitter_is_a_fixed_delay_and_the_cap_holds(self):
        t = Timelines(TimingConfig(venues={"v": VenueTimelines(*(TimelineSpec(7.0, 0.0),) * 6)}))
        self.assertEqual({t.sample_ns("v", "entry") for _ in range(10)}, {7_000_000})
        capped = Timelines(TimingConfig(venues={"v": VenueTimelines(*(TimelineSpec(50.0, 500.0, max_ms=60.0),) * 6)}))
        self.assertTrue(all(capped.sample_ns("v", "feed") <= 60_000_000 for _ in range(200)))

    def test_validation(self):
        with self.assertRaises(ValueError):
            TimelineSpec(-1.0)
        with self.assertRaises(ValueError):
            cfg(equal_time_policy="whoever")
        with self.assertRaises(ValueError):
            cfg(dead_feed_s=0.0)
        with self.assertRaises(ValueError):
            ClockConfig(uncertainty_ns=-1)


class ClockTests(unittest.TestCase):
    def test_fixed_offset_round_trips_and_never_learns_from_the_data(self):
        a = FixedOffsetAligner(ClockConfig(host_offset_ns=-78_000_000, uncertainty_ns=5_000_000, source="MEASURED:ollog"))
        self.assertTrue(a.ready)
        self.assertAlmostEqual(a.to_host(1000.0), 999.922, places=9)
        self.assertAlmostEqual(a.to_exchange(a.to_host(1000.0)), 1000.0, places=12)
        for _ in range(100):
            a.observe(1000.0, 1000.5)                              # a stamp 578 ms off the offset: backlogged, rejected, offset untouched
        self.assertEqual((a.samples, a.rejected), (0, 100))
        self.assertAlmostEqual(a.offset_s, -0.078, places=12)
        self.assertTrue(a.observe(1000.0, 1000.0 - 0.078 + 0.1))    # inside plausible_ns: a genuine receive time
        self.assertAlmostEqual(a.offset_s, -0.078, places=12)       # still untouched
        r = a.report()
        self.assertEqual((r["uncertainty_ms"], r["source"]), (5.0, "MEASURED:ollog"))


class TimerWheelTests(unittest.TestCase):
    def test_fires_in_deadline_order_fifo_within_a_deadline_and_respects_cancel(self):
        w = TimerWheel()
        w.schedule(s_to_ns(3.0), "expiry", "a")
        w.schedule(s_to_ns(1.0), "funding", "f")
        t = w.schedule(s_to_ns(2.0), "cancel", "c")
        w.schedule(s_to_ns(3.0), "unknown", "b")
        w.cancel(t)
        self.assertEqual([(x.kind, x.payload) for x in w.due(s_to_ns(3.0))], [("funding", "f")])        # strictly before 3.0
        self.assertEqual([(x.kind, x.payload) for x in w.due_at(s_to_ns(3.0))], [("expiry", "a"), ("unknown", "b")])
        self.assertEqual(len(w), 0)
        with self.assertRaises(TypeError):
            w.schedule(3.0, "float")                               # integer nanoseconds only

    def test_peek_skips_cancelled(self):
        w = TimerWheel()
        t = w.schedule(10, "x")
        w.schedule(20, "y")
        w.cancel(t)
        self.assertEqual(w.peek_ns(), 20)


class FeedStateTests(unittest.TestCase):
    def test_absolute_age_classes(self):
        self.assertEqual(feed_state(0.1, 0.5, 5.0), "valid")
        self.assertEqual(feed_state(0.6, 0.5, 5.0), "stale")
        self.assertEqual(feed_state(5.1, 0.5, 5.0), "dead")
        self.assertEqual(feed_state(float("inf"), 0.5, 5.0), "dead")


if __name__ == "__main__":
    unittest.main()
