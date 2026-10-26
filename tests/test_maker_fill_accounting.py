"""Volume conservation, trade/depth overlap, cancel allocation and trade de-duplication in src/new_backtest.

Data facts these tests rest on (measured, see the fill-simulator audit report):
  * Trade records carry no id or sequence number: exchange_ts (ms), price, volume, side. Nothing can be de-duplicated by id.
  * A real execution is published twice, as a trade and as a depth decrease. On Binance SOLUSDT (Tardis, 2022-01-13) a
    size-matched decrease lies within 50 ms of the trade for ~80% of trades against ~4% for trades shifted by 2 s;
    median lag +16 ms, p95 58 ms. Both feeds stamp the match time, so the overlap window runs on EXCHANGE time.
  * The feed carries no order ids, so who cancelled is unknown. 'all_ahead' (legacy) assumes the whole unexplained decrease
    was ahead of us; 'proportional' spreads it over the displayed level.
"""
from __future__ import annotations

import gzip
import json
import random
import tempfile
import unittest
from pathlib import Path

from new_backtest.event_loader import iter_venue_events
from new_backtest.fill_engine import MakerConfig
from new_backtest.models import Side

from test_fill_engine_queue import _book, _executor, _place


def queue10(**cfg):
    """One bid of 5 at 100 behind a displayed level of 10 (queue ahead 10); executor and order."""
    ex = _executor(MakerConfig(**cfg))
    order = _place(ex, _book(bids=[(100.0, 10.0)]), price=100.0, size=5.0)
    order.arrival_ts = 0.0
    return ex, order


def trade(ex, volume, receive, exchange=None, venue="bybit"):
    return ex.on_trade(venue, price=100.0, volume=volume, sign=-1, now=receive, fee_bps=0.0, event_ts=exchange)


def delta(ex, new_size, receive, exchange=None, venue="bybit"):
    return ex.on_delta(venue, side="bid", price=100.0, new_size=new_size, now=receive, tick_size=1.0, fee_bps=0.0, event_ts=exchange)


class OverlapOnExchangeTimeTests(unittest.TestCase):
    def test_a_trade_and_its_depth_decrease_drain_once_even_when_delivered_seconds_apart(self):
        ex, o = queue10()
        trade(ex, 3.0, receive=2.000, exchange=1.000)
        delta(ex, 7.0, receive=4.000, exchange=1.012)          # delivered 2 s later, executed 12 ms later
        self.assertAlmostEqual(o.queue_ahead, 7.0, places=9)

    def test_legacy_receive_basis_double_drains_in_that_case(self):
        ex, o = queue10(dedupe_time_basis="receive")
        trade(ex, 3.0, receive=2.000, exchange=1.000)
        delta(ex, 7.0, receive=4.000, exchange=1.012)
        self.assertAlmostEqual(o.queue_ahead, 4.0, places=9)

    def test_depth_before_trade_is_also_matched(self):
        ex, o = queue10()
        delta(ex, 7.0, receive=2.000, exchange=1.000)
        trade(ex, 3.0, receive=5.000, exchange=1.010)
        self.assertAlmostEqual(o.queue_ahead, 7.0, places=9)

    def test_window_boundary_49ms_is_one_execution_51ms_is_two(self):
        for gap, expected_queue in ((0.049, 7.0), (0.051, 4.0)):
            with self.subTest(gap=gap):
                ex, o = queue10()
                trade(ex, 3.0, receive=1.0, exchange=1.000)
                delta(ex, 7.0, receive=1.0, exchange=1.000 + gap)
                self.assertAlmostEqual(o.queue_ahead, expected_queue, places=9)

    def test_two_independent_trades_are_each_explained_by_their_own_depth_decrease(self):
        ex, o = queue10()
        trade(ex, 3.0, receive=1.0, exchange=1.000)
        trade(ex, 3.0, receive=1.0, exchange=1.001)
        delta(ex, 7.0, receive=1.0, exchange=1.010)
        delta(ex, 4.0, receive=1.0, exchange=1.012)
        self.assertAlmostEqual(o.queue_ahead, 4.0, places=9)    # 6 traded, 6 shrunk: drained once

    def test_a_genuine_cancel_inside_the_window_is_credited_to_the_trade_which_under_drains_the_queue(self):
        # Known, conservative error: a cancel of 3 right after an unrelated trade of 3 (whose own depth update never
        # arrives) looks like that trade's depth update. The queue then stays larger than reality, never smaller.
        ex, o = queue10()
        trade(ex, 3.0, receive=1.0, exchange=1.000)
        delta(ex, 7.0, receive=1.0, exchange=1.020)             # this is really a cancel
        self.assertGreaterEqual(o.queue_ahead, 4.0)

    def test_credits_are_per_venue_and_side(self):
        ex = _executor(MakerConfig())
        a = _place(ex, _book("bybit", bids=[(100.0, 10.0)]), price=100.0, size=5.0)
        b = _place(ex, _book("binance", bids=[(100.0, 10.0)]), price=100.0, size=5.0)
        a.arrival_ts = b.arrival_ts = 0.0
        trade(ex, 3.0, receive=1.0, exchange=1.0, venue="bybit")
        delta(ex, 7.0, receive=1.0, exchange=1.01, venue="binance")    # same price, other venue: not explained by bybit's trade
        self.assertAlmostEqual(a.queue_ahead, 7.0, places=9)
        self.assertAlmostEqual(b.queue_ahead, 7.0, places=9)           # drained by its own decrease only


class CancelModelTests(unittest.TestCase):
    def _grow_then_cancel(self, model):
        ex, o = queue10(cancel_model=model)
        delta(ex, 30.0, receive=1.0, exchange=1.0)             # 20 joined behind us
        delta(ex, 20.0, receive=2.0, exchange=2.0)             # then 10 left the level, nobody knows whose
        return o

    def test_legacy_all_ahead_never_sees_the_decrease_after_growth(self):
        self.assertAlmostEqual(self._grow_then_cancel("all_ahead").queue_ahead, 10.0, places=9)

    def test_proportional_gives_us_only_our_share_of_an_unattributed_decrease(self):
        # level 30 = 10 ahead + 20 behind; a 10-lot decrease is ahead of us with probability 10/30
        self.assertAlmostEqual(self._grow_then_cancel("proportional").queue_ahead, 10.0 - 10.0 * 10.0 / 30.0, places=9)

    def test_models_agree_when_nothing_joined_behind_us(self):
        for model in ("all_ahead", "proportional"):
            ex, o = queue10(cancel_model=model)
            delta(ex, 6.0, receive=1.0, exchange=1.0)
            self.assertAlmostEqual(o.queue_ahead, 6.0, places=9, msg=model)

    def test_proportional_cannot_fill_while_queue_remains(self):
        ex, o = queue10(cancel_model="proportional")
        delta(ex, 40.0, receive=1.0, exchange=1.0)
        fills = delta(ex, 36.0, receive=2.0, exchange=2.0)     # 4 gone of 40: 1 ahead
        self.assertEqual(fills, [])
        self.assertGreater(o.queue_ahead, 0.0)
        self.assertEqual(o.filled_qty, 0.0)

    def test_proportional_can_fill_once_the_queue_is_exhausted(self):
        ex, o = queue10(cancel_model="proportional")
        delta(ex, 0.0, receive=1.0, exchange=1.0)              # the whole level of 10 disappears
        self.assertEqual(o.queue_ahead, 0.0)
        self.assertAlmostEqual(o.filled_qty, 0.0, places=9)    # nothing left over to fill from: 10 - 10
        ex2, o2 = queue10(cancel_model="proportional")
        o2.queue_ahead = 2.0                                   # most of the queue already traded away
        delta(ex2, 0.0, receive=1.0, exchange=1.0)             # 10 gone: 2 ahead -> queue 0, 8 left for the fill share
        self.assertAlmostEqual(o2.filled_qty, 8.0 * 0.15, places=9)


class SnapshotTradeOverlapTests(unittest.TestCase):
    def _trade_then_snapshot(self, **cfg):
        ex, o = queue10(**cfg)
        trade(ex, 3.0, receive=1.0, exchange=1.0)
        ex.on_snapshot("bybit", _book(bids=[(100.0, 7.0)]), now=5.0, tick_size=1.0, fee_bps=0.0, event_ts=5.0)
        return o

    def test_a_trade_and_the_snapshot_that_shows_it_drain_once(self):
        self.assertAlmostEqual(self._trade_then_snapshot().queue_ahead, 7.0, places=9)

    def test_legacy_switch_drains_twice(self):
        self.assertAlmostEqual(self._trade_then_snapshot(dedupe_trade_depth=False).queue_ahead, 4.0, places=9)

    def test_snapshot_only_legacy_switch_drains_twice(self):
        self.assertAlmostEqual(self._trade_then_snapshot(snapshot_trade_explain=False).queue_ahead, 4.0, places=9)

    def test_a_depth_event_in_between_ends_the_credit(self):
        # A trade of 3, then a depth event that leaves the level unchanged (others refilled it), then a snapshot showing the
        # level 3 lower because someone cancelled. The old trade must not be credited with that cancel.
        ex, o = queue10()
        trade(ex, 3.0, receive=1.0, exchange=1.0)             # queue 10 -> 7
        delta(ex, 10.0, receive=2.0, exchange=2.0)            # level refilled to 10: a fresh observation
        ex.on_snapshot("bybit", _book(bids=[(100.0, 7.0)]), now=5.0, tick_size=1.0, fee_bps=0.0, event_ts=5.0)
        self.assertAlmostEqual(o.queue_ahead, 4.0, places=9)  # the 3 that left is a new decrease, drained in full

    def test_a_snapshot_decrease_larger_than_the_trade_drains_the_difference(self):
        ex, o = queue10()
        trade(ex, 3.0, receive=1.0, exchange=1.0)
        ex.on_snapshot("bybit", _book(bids=[(100.0, 5.0)]), now=5.0, tick_size=1.0, fee_bps=0.0, event_ts=5.0)
        self.assertAlmostEqual(o.queue_ahead, 5.0, places=9)   # 10 -> 5 displayed; 3 explained by the trade, 2 not


class ConservationProperty(unittest.TestCase):
    """Random trades, deltas and snapshots against several orders: the books must always balance."""

    def test_no_event_allocates_more_than_its_volume_and_state_stays_valid(self):
        rng = random.Random(20260301)
        for share in (True, False):
            ex = _executor(MakerConfig(share_trade_volume=share))
            orders = []
            for _ in range(4):
                o = _place(ex, _book(bids=[(100.0, rng.uniform(1, 8))]), price=100.0, size=rng.uniform(1, 6), now=1.0)
                o.arrival_ts = 0.0
                orders.append(o)
            level, t, trades = 20.0, 1.0, 0
            for _ in range(3000):
                t += rng.uniform(0.0005, 0.08)
                r = rng.random()
                queue_before = [o.queue_ahead for o in orders]
                if r < 0.4:
                    vol = rng.uniform(0.01, 5.0)
                    fills = trade(ex, vol, receive=t, exchange=t - rng.uniform(0, 0.02))
                    trades += 1
                    if share:
                        # volume taken out of the queues ahead of us plus volume that filled us can never exceed the print
                        # (after the part already explained by depth decreases is removed), orders sharing the one trade
                        net = ex.diag["trade_volume_net"] - getattr(self, "_net_prev", 0.0)
                        absorbed = sum(qb - o.queue_ahead for qb, o in zip(queue_before, orders))
                        self.assertLessEqual(absorbed + sum(f.qty for f in fills), net + 1e-9)
                    self._net_prev = ex.diag["trade_volume_net"]
                elif r < 0.8:
                    level = max(0.0, level + rng.uniform(-6, 5))
                    delta(ex, level, receive=t, exchange=t - rng.uniform(0, 0.02))
                else:
                    level = max(0.0, level + rng.uniform(-6, 5))
                    ex.on_snapshot("bybit", _book(bids=[(100.0, level)]), now=t, tick_size=1.0, fee_bps=0.0, event_ts=t)
                for o in orders:
                    self.assertGreaterEqual(o.queue_ahead, -1e-12)
                    self.assertGreaterEqual(o.remaining, -1e-12)
                    self.assertLessEqual(o.filled_qty, o.size + 1e-9)
                    self.assertAlmostEqual(o.remaining + o.filled_qty, o.size, places=9)
                d = ex.diag
                self.assertLessEqual(d["trade_volume_credited_to_depth"], d["trade_volume_observed"] + 1e-9)
                self.assertAlmostEqual(d["trade_volume_net"], d["trade_volume_observed"] - d["trade_volume_credited_to_depth"], places=6)
            self.assertGreater(trades, 1000)
            self._net_prev = 0.0

    def test_shared_trade_volume_across_orders_never_exceeds_the_trade(self):
        ex = _executor(MakerConfig())
        book = _book(bids=[(100.0, 0.0)])
        orders = [_place(ex, book, price=100.0, size=5.0) for _ in range(3)]
        for o in orders:
            o.arrival_ts = 0.0
        fills = trade(ex, 7.0, receive=2.0, exchange=2.0)
        self.assertAlmostEqual(sum(f.qty for f in fills), 7.0, places=9)        # 5 + 2, not 15
        self.assertAlmostEqual(sum(o.filled_qty for o in orders), 7.0, places=9)
        self.assertEqual(ex.diag["trade_volume_observed"], 7.0)
        self.assertAlmostEqual(ex.diag["trade_volume_to_fills"], 7.0, places=9)


class TradeDedupeTests(unittest.TestCase):
    """Overlapping collector files: 'set' drops any repeat of a record, 'multiset' only repeats ACROSS files."""

    REC = {"exchange": "bybit", "pair": "TESTUSDT", "exchange_ts": 1000.0, "local_ts": 1000.1, "price": 10.0, "volume": 0.002, "side": "b"}

    def _write(self, root, name, rows):
        d = root / "bybit" / "trades" / "pair=TESTUSDT" / "date=2026-01-01"
        d.mkdir(parents=True, exist_ok=True)
        with gzip.open(d / name, "wt") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def _volume(self, root, mode):
        stats = {}
        evs = list(iter_venue_events(root, "bybit", "TESTUSDT", "2026-01-01", "2026-01-01", include_snapshots=False,
                                     include_deltas=False, stats=stats, trade_dedupe=mode))
        return len(evs), stats.get("duplicates_dropped", 0)

    def test_one_file_with_identical_prints_set_collapses_them_multiset_keeps_them(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._write(root, "a.jsonl.gz", [dict(self.REC) for _ in range(3)])
            self.assertEqual(self._volume(root, "set"), (1, 2))
            self.assertEqual(self._volume(root, "multiset"), (3, 0))

    def test_two_overlapping_files_each_with_three_prints_multiset_emits_three_not_six(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in ("a.jsonl.gz", "b.jsonl.gz"):
                self._write(root, name, [dict(self.REC, local_ts=1000.1 + (name == "b.jsonl.gz")) for _ in range(3)])
            self.assertEqual(self._volume(root, "set"), (1, 5))
            self.assertEqual(self._volume(root, "multiset"), (3, 3))

    def test_a_longer_file_contributes_its_extra_prints(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._write(root, "a.jsonl.gz", [dict(self.REC) for _ in range(2)])
            self._write(root, "b.jsonl.gz", [dict(self.REC) for _ in range(4)])
            self.assertEqual(self._volume(root, "multiset")[0], 4)

    def test_distinct_records_are_unaffected_and_a_bad_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._write(root, "a.jsonl.gz", [dict(self.REC, exchange_ts=1000.0 + i) for i in range(5)])
            self.assertEqual(self._volume(root, "set"), (5, 0))
            self.assertEqual(self._volume(root, "multiset"), (5, 0))
            with self.assertRaises(ValueError):
                self._volume(root, "weird")


if __name__ == "__main__":
    unittest.main()
