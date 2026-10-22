"""Tests for new_backtest/latency_model.py — covering the four audit questions.

Audit questions:
  Q1. Configured distributions match measured latency.
  Q2. Feed latency is applied exactly once.
  Q3. Exchange and local clocks are aligned correctly.
  Q4. Ties and delayed events are ordered predictably.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

import pytest

from crypto_MFT_arb_platform.src.new_backtest.config import VenueLatencySpec
from crypto_MFT_arb_platform.src.new_backtest.latency_model import (
    HostClockAligner,
    InFlightOrder,
    LatencyBook,
    LatencySampler,
    PendingOrderQueue,
    VenueLatency,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _spec(feed=5.0, entry=10.0, cancel=10.0, jitter=2.0) -> VenueLatencySpec:
    return VenueLatencySpec(feed_ms=feed, entry_ms=entry, cancel_ms=cancel, jitter_ms=jitter)


def _inflight(release_ts: float, order_id: int = 1, signal_id: int = 0) -> InFlightOrder:
    return InFlightOrder(
        release_ts=release_ts, order_id=order_id, venue="bybit",
        side="buy", qty=1.0, limit_price=100.0, is_taker=True,
        sent_ts=release_ts - 0.01, signal_id=signal_id,
    )


# ── Q1: Configured distributions match measured latency ──────────────────────

class TestLatencySamplerDistribution:
    """The lognormal parameterisation must produce samples whose empirical mean
    and std are close to the configured values, and every sample must be positive."""

    N = 30_000

    def test_lognormal_mean_within_5pct(self):
        sampler = LatencySampler(mean_ms=10.0, std_ms=3.0, min_ms=0.0, max_ms=9999.0, seed=1)
        samples = [sampler.sample_ms() for _ in range(self.N)]
        assert abs(statistics.mean(samples) - 10.0) / 10.0 < 0.05

    def test_lognormal_std_within_10pct(self):
        sampler = LatencySampler(mean_ms=10.0, std_ms=3.0, min_ms=0.0, max_ms=9999.0, seed=2)
        samples = [sampler.sample_ms() for _ in range(self.N)]
        assert abs(statistics.stdev(samples) - 3.0) / 3.0 < 0.10

    def test_all_samples_positive(self):
        sampler = LatencySampler(mean_ms=5.0, std_ms=4.0, min_ms=0.01, max_ms=9999.0, seed=3)
        for _ in range(1000):
            assert sampler.sample_ms() > 0

    def test_fixed_model_returns_mean(self):
        sampler = LatencySampler(mean_ms=7.5, std_ms=0.0, model="fixed", seed=4)
        for _ in range(100):
            assert sampler.sample_ms() == pytest.approx(7.5)

    def test_zero_std_collapses_to_mean(self):
        """std=0 should behave like fixed, not raise."""
        sampler = LatencySampler(mean_ms=12.0, std_ms=0.0, seed=5)
        for _ in range(50):
            assert sampler.sample_ms() == pytest.approx(12.0)

    def test_sample_returns_seconds(self):
        sampler = LatencySampler(mean_ms=10.0, std_ms=0.0, seed=6)
        assert sampler.sample() == pytest.approx(0.010)

    def test_min_clamp_respected(self):
        sampler = LatencySampler(mean_ms=1.0, std_ms=0.1, min_ms=5.0, max_ms=9999.0, seed=7)
        for _ in range(200):
            assert sampler.sample_ms() >= 5.0

    def test_max_clamp_respected(self):
        sampler = LatencySampler(mean_ms=100.0, std_ms=100.0, min_ms=0.0, max_ms=50.0, seed=8)
        for _ in range(200):
            assert sampler.sample_ms() <= 50.0

    def test_percentiles_shape(self):
        sampler = LatencySampler(mean_ms=20.0, std_ms=5.0, seed=9)
        p = sampler.percentiles((10, 50, 90, 99))
        assert p[10] < p[50] < p[90] < p[99], "percentiles must be monotone"

    def test_per_venue_samplers_independent(self):
        """Feed and entry samplers must not share state; their sequences differ."""
        spec = _spec(feed=5.0, entry=50.0, jitter=0.0)
        vl = VenueLatency("bybit", spec, seed=42)
        feed_samples = [vl.feed.sample_ms() for _ in range(100)]
        entry_samples = [vl.entry.sample_ms() for _ in range(100)]
        assert feed_samples != entry_samples, "feed and entry must use independent samplers"
        # Means should reflect their configured values
        assert abs(statistics.mean(feed_samples) - 5.0) < 1.0
        assert abs(statistics.mean(entry_samples) - 50.0) < 5.0


# ── Q2: Feed latency applied exactly once ─────────────────────────────────────

class TestFeedLatencyAppliedOnce:
    """project_receive_ts adds feed latency to the host-adjusted exchange_ts.
    Calling it a second time on the same output must add MORE latency (not zero),
    proving the caller must not call it twice on the same event."""

    def test_project_adds_positive_latency(self):
        spec = _spec(feed=10.0, jitter=0.0)
        vl = VenueLatency("bybit", spec, seed=10)
        exchange_ts = 1_700_000_000.0
        recv = vl.project_receive_ts(exchange_ts)
        # Feed latency is 10ms = 0.010s, clock offset 0
        assert recv == pytest.approx(exchange_ts + 0.010, rel=1e-6)

    def test_project_latency_equals_exactly_one_feed_sample(self):
        """With std=0, every call adds exactly feed_ms. Two calls on successive
        records each add feed_ms — never double-counted."""
        spec = _spec(feed=20.0, jitter=0.0)
        vl = VenueLatency("bybit", spec, seed=11)
        t0, t1 = 1_700_000_000.0, 1_700_000_001.0
        r0 = vl.project_receive_ts(t0)
        r1 = vl.project_receive_ts(t1)
        # Each record independently shifted by 20ms
        assert r0 == pytest.approx(t0 + 0.020, rel=1e-6)
        assert r1 == pytest.approx(t1 + 0.020, rel=1e-6)

    def test_observe_receive_ts_measured_path_uses_local_ts_not_projection(self):
        """When a plausible local_ts is available, the output IS local_ts.
        No additional feed latency is added on top."""
        spec = _spec(feed=100.0, jitter=0.0)  # huge feed latency to make any error obvious
        aligner = HostClockAligner(warmup=1)   # warmup=1 so it seeds after first record
        vl = VenueLatency("bybit", spec, seed=12, aligner=aligner)

        # Warm up the aligner with a plausible record.
        # Offset must be within hard_cap_s=30s; use -0.05s (50ms behind exchange).
        exchange_ts0, local_ts0 = 100.0, 99.95   # offset = -0.05s
        vl.observe_receive_ts(exchange_ts0, local_ts0)
        assert aligner.ready, "aligner should be seeded after warmup=1 record"

        # Feed a record whose local_ts is exactly at the modelled offset.
        exchange_ts1 = 200.0
        local_ts1 = exchange_ts1 + aligner.offset_s
        recv, measured = vl.observe_receive_ts(exchange_ts1, local_ts1)

        assert measured is True
        assert recv == pytest.approx(local_ts1, rel=1e-9), (
            "measured path must return local_ts, not local_ts + feed_latency"
        )

    def test_observe_receive_ts_backlogged_falls_back_to_projection(self):
        """An implausible local_ts (collector backlog) must route through
        project_receive_ts, not use the raw local_ts."""
        spec = _spec(feed=5.0, jitter=0.0)
        aligner = HostClockAligner(warmup=1, tolerance_s=0.10)
        vl = VenueLatency("bybit", spec, seed=13, aligner=aligner)

        # Seed the aligner: host is 0.05s behind exchange (negative offset)
        vl.observe_receive_ts(1_700_000_000.0, 1_699_999_950.0)

        # Backlogged record: local_ts is 60s after exchange_ts (far outside tolerance)
        exchange_ts = 1_700_000_001.0
        local_ts_backlogged = exchange_ts + 60.0
        recv, measured = vl.observe_receive_ts(exchange_ts, local_ts_backlogged)

        assert measured is False
        # recv must be based on exchange_ts + aligner.offset + feed, not local_ts_backlogged
        assert recv < local_ts_backlogged - 50.0, "backlogged local_ts must be discarded"

    def test_entry_latency_separate_from_feed_latency(self):
        """entry_delay() uses the *entry* sampler, not the feed sampler."""
        spec = _spec(feed=5.0, entry=50.0, jitter=0.0)
        book = LatencyBook({"bybit": spec}, seed=100)
        # With zero jitter: entry_delay must be 50ms, not 5ms
        delay = book.entry_delay("bybit")
        assert delay == pytest.approx(0.050, rel=1e-6)


# ── Q3: Exchange and local clock alignment ────────────────────────────────────

class TestHostClockAligner:
    """HostClockAligner must estimate the (host - exchange) offset correctly
    after warmup, reject backlogged records, and reseed when the offset drifts."""

    def _feed(self, aligner: HostClockAligner, n: int, offset: float) -> None:
        # Use small timestamps to avoid float precision loss when computing
        # d = local_ts - exchange_ts at large magnitudes like 1.7e9.
        for i in range(n):
            exchange_ts = float(i)
            local_ts = exchange_ts + offset
            aligner.observe(exchange_ts, local_ts)

    def test_offset_zero_before_warmup(self):
        aligner = HostClockAligner(warmup=500)
        assert aligner.offset_s == 0.0
        assert not aligner.ready

    def test_offset_converges_after_warmup(self):
        aligner = HostClockAligner(warmup=100)
        TRUE_OFFSET = -0.078   # host 78ms behind exchange (realistic Bybit observation)
        self._feed(aligner, 110, TRUE_OFFSET)
        assert aligner.ready
        assert aligner.offset_s == pytest.approx(TRUE_OFFSET, abs=1e-9)

    def test_to_host_applies_offset(self):
        aligner = HostClockAligner(warmup=10)
        self._feed(aligner, 20, -0.05)
        exchange_ts = 999.0
        # offset_s should be very close to -0.05 after 20 records
        assert aligner.to_host(exchange_ts) == pytest.approx(exchange_ts + aligner.offset_s, rel=1e-9)
        assert abs(aligner.offset_s - (-0.05)) < 1e-9

    def test_hard_cap_rejects_extreme_records(self):
        aligner = HostClockAligner(warmup=10, hard_cap_s=30.0)
        exchange_ts = 1000.0
        accepted = aligner.observe(exchange_ts, exchange_ts + 60.0)  # 60s > 30s cap
        assert not accepted
        assert aligner.rejected == 1

    def test_backlogged_records_rejected_after_warmup(self):
        aligner = HostClockAligner(warmup=50, tolerance_s=0.10)
        self._feed(aligner, 60, -0.08)        # establish offset ~ -0.08
        # Backlogged: +5s local offset vs exchange, way outside tolerance=0.10
        # and within hard_cap=30s, so it goes through the tolerance check
        result = aligner.observe(1000.0, 1000.0 + 5.0)
        assert not result

    def test_warmup_records_always_return_false(self):
        aligner = HostClockAligner(warmup=200)
        for i in range(199):
            result = aligner.observe(float(i), float(i) - 0.05)
            assert not result, f"record {i} should not be accepted during warmup"

    def test_ready_after_warmup_complete(self):
        aligner = HostClockAligner(warmup=50)
        for i in range(50):
            aligner.observe(float(i), float(i) - 0.05)
        assert aligner.ready

    def test_window_eviction_keeps_estimate_stable(self):
        """After window is full, evicted old values must not corrupt offset_s."""
        aligner = HostClockAligner(warmup=10, window=20)
        TRUE_OFFSET = -0.030
        self._feed(aligner, 200, TRUE_OFFSET)   # far exceeds window
        assert aligner.offset_s == pytest.approx(TRUE_OFFSET, abs=1e-9)

    def test_reseed_after_consecutive_rejects(self):
        aligner = HostClockAligner(warmup=50, reseed_after=20, tolerance_s=0.10)
        self._feed(aligner, 60, -0.05)  # warmup + settle; offset_s ≈ -0.05
        initial_reseeds = aligner.reseeds
        # Feed 21 records with offset +1.0s — within hard_cap=30s but
        # |1.0 - (-0.05)| = 1.05 > tolerance_s=0.10 → rejected as out-of-tolerance
        for i in range(21):
            aligner.observe(float(1000 + i), float(1000 + i) + 1.0)
        assert aligner.reseeds > initial_reseeds, "reseed must fire after consecutive rejections"

    def test_is_plausible_accepts_within_tolerance(self):
        aligner = HostClockAligner(warmup=10, tolerance_s=0.10)
        self._feed(aligner, 15, -0.05)
        # local_ts at exactly the measured offset → d == offset_s → |d - offset_s| = 0
        exchange_ts = 999.0
        local_ts = exchange_ts + aligner.offset_s
        assert aligner.is_plausible(exchange_ts, local_ts)

    def test_is_plausible_rejects_outside_tolerance(self):
        aligner = HostClockAligner(warmup=10, tolerance_s=0.10)
        self._feed(aligner, 15, -0.05)
        # local_ts +1.0s offset: within hard_cap=30s, but |1.0 - (-0.05)| = 1.05 > 0.10
        assert not aligner.is_plausible(999.0, 999.0 + 1.0)


# ── Q4: Tie and ordering in the pending queue ─────────────────────────────────

class TestPendingOrderQueueOrdering:
    """Q4: in-flight orders must be released in release_ts order; ties (same
    release_ts) must be FIFO by insertion; orders arriving at exactly the
    current event time (release_ts == now) must land before the event is
    applied (checked here by confirming they ARE released)."""

    def test_release_before_event_same_ts(self):
        """An order whose release_ts == now must be yielded by release(now),
        modelling arrival just before the event is applied."""
        q = PendingOrderQueue()
        now = 1_700_000_000.0
        q.enqueue(_inflight(release_ts=now, order_id=1))
        released = list(q.release(now))
        assert len(released) == 1
        assert released[0].order_id == 1

    def test_future_order_not_released_yet(self):
        q = PendingOrderQueue()
        now = 1_700_000_000.0
        q.enqueue(_inflight(release_ts=now + 0.001, order_id=2))
        released = list(q.release(now))
        assert released == []

    def test_release_order_is_chronological(self):
        q = PendingOrderQueue()
        t0 = 1_700_000_000.0
        for i, delta in enumerate([0.030, 0.010, 0.020]):
            q.enqueue(_inflight(release_ts=t0 + delta, order_id=i + 1))
        released = list(q.release(t0 + 0.050))
        assert [o.order_id for o in released] == [2, 3, 1]  # 10ms, 20ms, 30ms

    def test_tie_broken_fifo(self):
        """Two orders with identical release_ts must come out in insertion order."""
        q = PendingOrderQueue()
        t = 1_700_000_000.0
        q.enqueue(_inflight(release_ts=t, order_id=10))
        q.enqueue(_inflight(release_ts=t, order_id=20))
        released = list(q.release(t))
        assert [o.order_id for o in released] == [10, 20]

    def test_cancelled_order_not_released(self):
        q = PendingOrderQueue()
        t = 1_700_000_000.0
        q.enqueue(_inflight(release_ts=t, order_id=5))
        q.cancel(5)
        released = list(q.release(t))
        assert released == []

    def test_drain_empties_queue(self):
        q = PendingOrderQueue()
        t = 1_700_000_000.0
        for i in range(5):
            q.enqueue(_inflight(release_ts=t + i * 10.0, order_id=i))
        drained = list(q.drain())
        assert len(drained) == 5
        assert q.pending_count == 0

    def test_pending_count_tracks_live_orders(self):
        q = PendingOrderQueue()
        t = 1_700_000_000.0
        q.enqueue(_inflight(release_ts=t + 1, order_id=1))
        q.enqueue(_inflight(release_ts=t + 2, order_id=2))
        assert q.pending_count == 2
        q.cancel(1)
        assert q.pending_count == 1

    def test_cancel_signal_removes_all_legs(self):
        q = PendingOrderQueue()
        t = 1_700_000_000.0
        q.enqueue(_inflight(release_ts=t + 0.1, order_id=1, signal_id=42))
        q.enqueue(_inflight(release_ts=t + 0.2, order_id=2, signal_id=42))
        q.enqueue(_inflight(release_ts=t + 0.3, order_id=3, signal_id=99))
        n = q.cancel_signal(42)
        assert n == 2
        released = list(q.release(t + 1.0))
        assert len(released) == 1
        assert released[0].signal_id == 99

    def test_monotonicity_enforced_across_events(self):
        """project_receive_ts must never return a timestamp <= the previous one,
        even if the sampled feed latency would naturally produce a smaller value."""
        spec = _spec(feed=5.0, jitter=100.0)   # huge jitter to provoke inversions
        vl = VenueLatency("bybit", spec, seed=99)
        prev = float("-inf")
        for i in range(500):
            recv = vl.project_receive_ts(1_700_000_000.0 + i * 0.001)
            assert recv > prev, f"monotonicity violated at step {i}: {recv} <= {prev}"
            prev = recv
