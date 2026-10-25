"""Replay-integrity regression tests for src/new_backtest, built on multi-day synthetic Binance + Bybit data.

Covers: one trade filling our own stacked orders only once, the live-order limit counting orders in flight,
resting-order expiry, the collector-clock-offset warning, the documented queue double-drain limitation, and
that walk-forward calibration cannot see the future.
"""
from __future__ import annotations

import json
import tempfile
import unittest
import warnings
from pathlib import Path

from new_backtest.arb_strategy import ArbConfig
from new_backtest.backtester import ArbBacktestConfig, CrossVenueArbBacktester
from new_backtest.batch_backtest import run_pair
from new_backtest.fill_engine import MakerConfig
from new_backtest.fill_label import extract_labels
from new_backtest.models import LambdaBucket, Side

from synthetic_days import gen
from test_fill_engine_queue import _MarketState, _book, _executor, _place


def make_cfg(root, day0, day1, out, *, ttl=30.0, inflight=True, min_fill_prob=0.0, queue=0.8):
    return ArbBacktestConfig(
        data_root=str(root), venues=("bybit", "binance"), pair="BTCUSDT", start_date=day0, end_date=day1,
        latency_preset="colocated_both", bybit_fee_preset="bybit_perp_mm", binance_fee_preset="binance_perp_vip0",
        maker=MakerConfig(min_queue_ahead=queue),
        arb=ArbConfig(mode="maker_taker", min_net_edge_bps=1.0, max_clip_notional=2_000.0, min_fill_prob=min_fill_prob,
                      max_adverse_prob=0.15, max_ttf_s=8.0, max_cross_venue_skew_s=0.2, fill_gate_horizon_s=5,
                      maker_max_age_s=ttl, maker_count_inflight=inflight),
        output_dir=str(out))


class SharedTradeVolumeTests(unittest.TestCase):
    def _two_orders_then_one_trade(self, config):
        book = _book(bids=[(100.0, 0.0)])
        ex = _executor(config)
        _place(ex, book, price=100.0, size=5.0, now=1.0)
        ex.place(venue="bybit", pair="SOLUSDT", symbol="SOLUSDT", side=Side.BID, price=100.0, size=5.0, book=book,
                 state=_MarketState(), regime_bucket=LambdaBucket.CALM, now=1.0, active_ts=1.0, tick_size=1.0, signal_id=2)
        fills = ex.on_trade("bybit", price=100.0, volume=6.0, sign=-1, now=2.0, fee_bps=0.0)
        return sum(f.qty for f in fills)

    def test_one_trade_fills_our_orders_once_in_total(self):
        self.assertAlmostEqual(self._two_orders_then_one_trade(MakerConfig()), 6.0, places=9)

    def test_legacy_switch_reproduces_the_independent_drain(self):
        self.assertAlmostEqual(self._two_orders_then_one_trade(MakerConfig(share_trade_volume=False)), 10.0, places=9)


class QueueDoubleDrainTests(unittest.TestCase):
    """One real 3 BTC execution reported by BOTH the trade tape and the depth feed must drain 3 BTC of queue, not 6."""

    def _drained(self, config):
        book = _book(bids=[(100.0, 10.0)])
        ex = _executor(config)
        order = _place(ex, book, price=100.0, size=10.0)
        ex.on_trade("bybit", price=100.0, volume=3.0, sign=-1, now=2.0, fee_bps=0.0)
        ex.on_delta("bybit", side="bid", price=100.0, new_size=7.0, now=2.001, tick_size=1.0, fee_bps=0.0)
        return 10.0 - order.queue_ahead

    def test_trade_plus_matching_depth_reduction_drains_once(self):
        self.assertAlmostEqual(self._drained(MakerConfig(trade_fill_share=1.0, delta_fill_share=0.15)), 3.0, places=9)

    def test_legacy_switch_reproduces_the_double_drain(self):
        self.assertAlmostEqual(
            self._drained(MakerConfig(trade_fill_share=1.0, delta_fill_share=0.15, dedupe_trade_depth=False)), 6.0, places=9)


class ReplayOnSyntheticDaysTests(unittest.TestCase):
    def test_in_flight_orders_count_toward_the_live_order_limit(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "data"; gen(root, ndays=1, seed=11, session_s=60.0)
            peaks = {}
            for inflight in (True, False):
                r = CrossVenueArbBacktester(make_cfg(root, "2026-10-01", "2026-10-01", Path(d) / f"o{inflight}", inflight=inflight)).run()
                stamps = [round(x.timestamp, 6) for x in extract_labels(r.fill_log_path) if x.horizon_s == 5]
                peaks[inflight] = max(stamps.count(s) for s in set(stamps))
        self.assertEqual(peaks[True], 1)
        self.assertGreater(peaks[False], 1, "legacy mode must still stack, otherwise this test proves nothing")

    def test_a_stale_maker_order_expires_instead_of_blocking_every_later_quote(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "data"; gen(root, ndays=2, seed=11, session_s=150.0)
            ages = {}
            for ttl in (0.0, 30.0):
                r = CrossVenueArbBacktester(make_cfg(root, "2026-10-01", "2026-10-02", Path(d) / f"t{ttl}", ttl=ttl, inflight=False)).run()
                ages[ttl] = max(x.observation_time_s for x in extract_labels(r.fill_log_path) if x.horizon_s == 5)
        self.assertGreater(ages[0.0], 3600.0, "without expiry an order that never fills lives across the day boundary")
        self.assertLess(ages[30.0], 40.0)

    def test_collector_clock_offset_is_flagged_and_an_honest_clock_is_not(self):
        with tempfile.TemporaryDirectory() as d:
            gen(Path(d) / "off", ndays=1, seed=11, session_s=40.0, clock_offset={"bybit": -0.040})
            gen(Path(d) / "ok", ndays=1, seed=11, session_s=40.0)
            seen = {}
            for name in ("off", "ok"):
                with warnings.catch_warnings(record=True) as w:
                    warnings.simplefilter("always")
                    CrossVenueArbBacktester(make_cfg(Path(d) / name, "2026-10-01", "2026-10-01", Path(d) / f"c{name}")).run()
                seen[name] = [str(x.message) for x in w if issubclass(x.category, RuntimeWarning)]
        self.assertEqual(len(seen["off"]), 1)
        self.assertIn("bybit", seen["off"][0])
        self.assertEqual(seen["ok"], [])


class QueueFloorWarningTests(unittest.TestCase):
    def _warnings(self, queue, mode="maker_taker"):
        cfg = make_cfg("unused", "2026-10-01", "2026-10-01", "unused", queue=queue)
        cfg.arb.mode = mode
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            CrossVenueArbBacktester(cfg)
        return [str(x.message) for x in w if issubclass(x.category, RuntimeWarning)]

    def test_zero_queue_floor_in_maker_taker_mode_is_flagged(self):
        msgs = self._warnings(0.0)
        self.assertEqual(len(msgs), 1)
        self.assertIn("min_queue_ahead", msgs[0])

    def test_a_configured_floor_is_not_flagged(self):
        self.assertEqual(self._warnings(0.8), [])


class WalkForwardCannotSeeTheFutureTests(unittest.TestCase):
    """Dataset B equals A except the LAST day. Every fold that evaluates an earlier day must be bit-identical."""

    @staticmethod
    def _wf(root, out):
        cfg = make_cfg(root, "2026-10-01", "2026-10-09", Path(out) / "BTCUSDT", min_fill_prob=0.5)
        outcome = run_pair("BTCUSDT", cfg, Path(out), two_pass=True, wf_window_days=7, wf_step_days=1)
        assert outcome.status == "ok" and "wf 2 folds" in outcome.detail, outcome.detail
        return Path(out) / "BTCUSDT"

    @staticmethod
    def _rows(path):
        return [json.loads(line) for line in open(path)]

    def test_changing_only_the_last_day_leaves_the_earlier_fold_identical(self):
        shifted = {8: {"p_trade": 0.02, "vol": 3.0, "wide": 25}}
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "A", Path(d) / "B"
            gen(a, ndays=9, seed=11, session_s=40.0, bursty=True)
            gen(b, ndays=9, seed=11, session_s=40.0, bursty=True, overrides=shifted)
            ra, rb = self._wf(a, Path(d) / "wfA"), self._wf(b, Path(d) / "wfB")

            def scrub(o):
                if isinstance(o, dict):
                    return {k: scrub(v) for k, v in o.items() if not (isinstance(v, str) and "/" in v)}
                return [scrub(x) for x in o] if isinstance(o, list) else o

            f0a, f0b = ra / "wf_fold_000", rb / "wf_fold_000"
            self.assertEqual(scrub(json.loads((f0a / "fill_model_v1.json").read_text())), scrub(json.loads((f0b / "fill_model_v1.json").read_text())))
            self.assertEqual(scrub(self._rows(f0a / "fill_events.jsonl")), scrub(self._rows(f0b / "fill_events.jsonl")))
            self.assertEqual(scrub(self._rows(f0a / "trades.jsonl")), scrub(self._rows(f0b / "trades.jsonl")))
            # control: the fold that DOES evaluate the changed day must differ, otherwise the comparison has no power
            f1a, f1b = ra / "wf_fold_001", rb / "wf_fold_001"
            self.assertNotEqual(scrub(self._rows(f1a / "fill_events.jsonl")), scrub(self._rows(f1b / "fill_events.jsonl")))
            # the cold pass is causal: its log before the changed day is identical
            cut = _epoch("2026-10-09")
            ca = [r for r in self._rows(ra / "wf_cold" / "fill_events.jsonl") if r["timestamp"] < cut]
            cb = [r for r in self._rows(rb / "wf_cold" / "fill_events.jsonl") if r["timestamp"] < cut]
            self.assertGreater(len(ca), 0)
            self.assertEqual(scrub(ca), scrub(cb))


def _epoch(day: str) -> float:
    from datetime import datetime, timezone
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()


if __name__ == "__main__":
    unittest.main()
