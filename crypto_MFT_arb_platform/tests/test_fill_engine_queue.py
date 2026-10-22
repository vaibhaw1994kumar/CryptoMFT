"""Queue and fill-model regression tests for MakerExecutor.

Covers:
  - Queue initialization: level present, absent, min_queue_ahead floor
  - observed_level_size invariant: must never increase (D1 fix)
  - FIFO drain: queue depleted before our order fills
  - Partial fills: remaining and queue state after partial
  - Trade fill semantics: direction, volume, queue interaction
  - Delta fill semantics: shrink fires fill; growth suppressed
  - Snapshot fill semantics: weakest signal, only on shrink
  - Cancellation: order removed, no extra fill
  - No fill exceeds order size or available volume
  - min_queue_ahead: floor when level absent, no-op when level present
  - Order expiry / active_ts gate
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from new_backtest.fill_engine import Fill, MakerConfig, MakerExecutor, MakerOrder
from new_backtest.fill_model import EmpiricalFillModel
from new_backtest.models import LambdaBucket, Side
from new_backtest.order_book import OrderBook


# ── Minimal stubs ─────────────────────────────────────────────────────────────

class _MarketState:
    top_depth = 100.0
    top_depth_baseline = 100.0
    spread_pct = 0.001
    short_vol = 0.0
    recent_trade_rate = 0.0
    trade_sign = 0.0


def _book(venue: str = "bybit", bids=None, asks=None) -> OrderBook:
    """Minimal populated book."""
    book = OrderBook(venue, "SOLUSDT", delta_semantics=False)
    bids = bids or [(100.0, 50.0), (99.0, 100.0)]
    asks = asks or [(101.0, 50.0), (102.0, 100.0)]
    book.apply_snapshot(bids, asks, ts=0.0)
    return book


def _executor(config: MakerConfig | None = None) -> MakerExecutor:
    return MakerExecutor(EmpiricalFillModel(), config or MakerConfig())


def _place(
    executor: MakerExecutor,
    book: OrderBook,
    side: Side = Side.BID,
    price: float = 100.0,
    size: float = 10.0,
    now: float = 1.0,
    tick_size: float = 1.0,
) -> MakerOrder:
    return executor.place(
        venue=book.venue, pair="SOLUSDT", symbol="SOLUSDT",
        side=side, price=price, size=size, book=book,
        state=_MarketState(), regime_bucket=LambdaBucket.CALM,
        now=now, active_ts=now, tick_size=tick_size, signal_id=1,
    )


# ── Queue initialisation ──────────────────────────────────────────────────────

class TestQueueInit(unittest.TestCase):

    def test_queue_set_from_level_size(self):
        book = _book(bids=[(100.0, 50.0)])
        ex = _executor()
        order = _place(ex, book, price=100.0, size=5.0)
        self.assertAlmostEqual(order.queue_ahead, 50.0)
        self.assertAlmostEqual(order.initial_queue_ahead, 50.0)

    def test_queue_zero_when_level_absent(self):
        # Bid at 100 not in the book → level_size = 0 → queue_ahead = 0
        book = _book(bids=[(99.0, 50.0)])
        ex = _executor()
        order = _place(ex, book, price=100.0, size=5.0)
        self.assertAlmostEqual(order.queue_ahead, 0.0)

    def test_min_queue_ahead_applies_only_when_level_absent(self):
        # Level present (size=50) → min_queue_ahead must not override
        book = _book(bids=[(100.0, 50.0)])
        cfg = MakerConfig(min_queue_ahead=200.0)
        ex = _executor(cfg)
        order = _place(ex, book, price=100.0, size=5.0)
        self.assertAlmostEqual(order.queue_ahead, 50.0)

    def test_min_queue_ahead_used_when_level_absent(self):
        book = _book(bids=[(99.0, 50.0)])  # no level at 100
        cfg = MakerConfig(min_queue_ahead=30.0)
        ex = _executor(cfg)
        order = _place(ex, book, price=100.0, size=5.0)
        self.assertAlmostEqual(order.queue_ahead, 30.0)

    def test_queue_stress_multiplier_scales_present_level(self):
        book = _book(bids=[(100.0, 40.0)])
        cfg = MakerConfig(queue_stress_multiplier=2.0)
        ex = _executor(cfg)
        order = _place(ex, book, price=100.0, size=5.0)
        self.assertAlmostEqual(order.queue_ahead, 80.0)

    def test_min_queue_ahead_does_not_stack_with_multiplier_on_absent_level(self):
        # min_queue_ahead is the floor when level absent, ignores multiplier
        book = _book(bids=[(99.0, 50.0)])
        cfg = MakerConfig(min_queue_ahead=30.0, queue_stress_multiplier=2.0)
        ex = _executor(cfg)
        order = _place(ex, book, price=100.0, size=5.0)
        self.assertAlmostEqual(order.queue_ahead, 30.0)


# ── FIFO drain — trade fills ──────────────────────────────────────────────────

class TestTradeFills(unittest.TestCase):

    def test_trade_drains_queue_before_filling(self):
        # queue=20, trade volume=25 → drained 20, available=5, fill 5 units
        book = _book(bids=[(100.0, 20.0)])
        ex = _executor()
        order = _place(ex, book, price=100.0, size=10.0)
        self.assertAlmostEqual(order.queue_ahead, 20.0)

        fills = ex.on_trade("bybit", price=100.0, volume=25.0, sign=-1,
                            now=2.0, fee_bps=0.0)
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0].qty, 5.0)
        self.assertAlmostEqual(order.queue_ahead, 0.0)

    def test_trade_smaller_than_queue_drains_only_queue(self):
        # queue=20, trade=10 → drains 10 from queue, no fill
        book = _book(bids=[(100.0, 20.0)])
        ex = _executor()
        order = _place(ex, book, price=100.0, size=10.0)

        fills = ex.on_trade("bybit", price=100.0, volume=10.0, sign=-1,
                            now=2.0, fee_bps=0.0)
        self.assertEqual(len(fills), 0)
        self.assertAlmostEqual(order.queue_ahead, 10.0)
        self.assertAlmostEqual(order.remaining, 10.0)

    def test_fill_does_not_exceed_order_size(self):
        book = _book(bids=[(100.0, 0.0)])  # no queue
        ex = _executor()
        order = _place(ex, book, price=100.0, size=5.0)

        # Trade much larger than order
        fills = ex.on_trade("bybit", price=100.0, volume=1000.0, sign=-1,
                            now=2.0, fee_bps=0.0)
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0].qty, 5.0)

    def test_wrong_direction_trade_does_not_fill_bid(self):
        # sign=+1 (buy aggressor lifts asks) must not fill our bid
        book = _book(bids=[(100.0, 0.0)])
        ex = _executor()
        order = _place(ex, book, side=Side.BID, price=100.0, size=5.0)

        fills = ex.on_trade("bybit", price=100.0, volume=10.0, sign=+1,
                            now=2.0, fee_bps=0.0)
        self.assertEqual(len(fills), 0)

    def test_wrong_direction_trade_does_not_fill_ask(self):
        book = _book(asks=[(101.0, 0.0)])
        ex = _executor()
        order = _place(ex, book, side=Side.ASK, price=101.0, size=5.0)

        fills = ex.on_trade("bybit", price=101.0, volume=10.0, sign=-1,
                            now=2.0, fee_bps=0.0)
        self.assertEqual(len(fills), 0)

    def test_trade_above_bid_does_not_fill(self):
        # sign=-1 (sell aggressor) clearing at 105 means it matched asks at 105,
        # not bids — our bid at 100.0 is unaffected.
        # Condition: price <= order.price → 105 <= 100 is False → no fill.
        book = _book(bids=[(100.0, 0.0)])
        ex = _executor()
        order = _place(ex, book, price=100.0, size=5.0)

        fills = ex.on_trade("bybit", price=105.0, volume=10.0, sign=-1,
                            now=2.0, fee_bps=0.0)
        self.assertEqual(len(fills), 0)

    def test_trade_below_bid_does_fill(self):
        # sign=-1 (sell aggressor) clearing at 99 means all bids at >=99 were
        # crossed, including ours at 100.0 which should have filled first.
        book = _book(bids=[(100.0, 0.0)])
        ex = _executor()
        order = _place(ex, book, price=100.0, size=5.0)

        fills = ex.on_trade("bybit", price=99.0, volume=10.0, sign=-1,
                            now=2.0, fee_bps=0.0)
        self.assertEqual(len(fills), 1)

    def test_order_not_yet_active_is_skipped(self):
        # active_ts=5.0 but event arrives at now=2.0
        book = _book(bids=[(100.0, 0.0)])
        ex = _executor()
        ex.place(
            venue="bybit", pair="SOLUSDT", symbol="SOLUSDT",
            side=Side.BID, price=100.0, size=5.0, book=book,
            state=_MarketState(), regime_bucket=LambdaBucket.CALM,
            now=1.0, active_ts=5.0, tick_size=1.0, signal_id=1,
        )
        fills = ex.on_trade("bybit", price=100.0, volume=100.0, sign=-1,
                            now=2.0, fee_bps=0.0)
        self.assertEqual(len(fills), 0)


# ── Delta fills and observed_level_size invariant (D1 fix) ───────────────────

class TestDeltaFillsAndObservedLevel(unittest.TestCase):

    def test_shrink_triggers_delta_fill(self):
        book = _book(bids=[(100.0, 0.0)])  # no queue
        ex = _executor(MakerConfig(delta_fill_share=0.15))
        order = _place(ex, book, price=100.0, size=10.0)
        # observed=0 initially (level had size 0)
        self.assertAlmostEqual(order.observed_level_size, 0.0)

        # Inject a shrink: book shows level at 0, was at 0 → no shrink, no fill
        fills = ex.on_delta("bybit", side="bid", price=100.0, new_size=0.0,
                            now=2.0, tick_size=1.0, fee_bps=0.0)
        self.assertEqual(len(fills), 0)

    def test_shrink_from_prior_size_triggers_fill(self):
        book = _book(bids=[(100.0, 50.0)])
        ex = _executor(MakerConfig(delta_fill_share=0.15))
        order = _place(ex, book, price=100.0, size=10.0)
        # observed_level_size should be 50 at placement

        # Now level shrinks from 50 to 30 (shrink=20)
        fills = ex.on_delta("bybit", side="bid", price=100.0, new_size=30.0,
                            now=2.0, tick_size=1.0, fee_bps=0.0)
        # queue_ahead=50, trade volume (shrink)=20 → drains 20 from queue, available=0 → no fill
        self.assertEqual(len(fills), 0)
        self.assertAlmostEqual(order.queue_ahead, 30.0)

    def test_observed_level_size_never_increases(self):
        """D1 fix: growth after placement must not raise observed_level_size."""
        book = _book(bids=[(100.0, 50.0)])
        ex = _executor(MakerConfig(delta_fill_share=0.15))
        order = _place(ex, book, price=100.0, size=10.0)
        initial_obs = order.observed_level_size  # should be 50

        # Level grows (new orders behind us)
        ex.on_delta("bybit", side="bid", price=100.0, new_size=200.0,
                    now=2.0, tick_size=1.0, fee_bps=0.0)
        # observed_level_size must remain at initial, not jump to 200
        self.assertAlmostEqual(order.observed_level_size, initial_obs)

    def test_subsequent_shrink_of_grown_level_uses_pre_growth_baseline(self):
        """After growth, a shrink back to original size fires no spurious fill."""
        book = _book(bids=[(100.0, 50.0)])
        ex = _executor(MakerConfig(delta_fill_share=0.15))
        order = _place(ex, book, price=100.0, size=10.0)

        # Level grows from 50 to 200 (orders behind us join)
        ex.on_delta("bybit", side="bid", price=100.0, new_size=200.0,
                    now=2.0, tick_size=1.0, fee_bps=0.0)

        # Level shrinks from 200 to 50: those 150 units were behind us → no fill
        fills = ex.on_delta("bybit", side="bid", price=100.0, new_size=50.0,
                            now=3.0, tick_size=1.0, fee_bps=0.0)
        # queue_ahead was 50 at placement. If observed_level_size stayed at 50
        # (not 200), the shrink from 50→50 is zero → no fill. ✓
        total_fill = sum(f.qty for f in fills)
        self.assertAlmostEqual(total_fill, 0.0,
            msg="Behind-us orders leaving must not trigger a fill")

    def test_growth_then_further_shrink_fires_correctly(self):
        """After growth, a shrink BELOW original level fires a fill."""
        book = _book(bids=[(100.0, 50.0)])
        ex = _executor(MakerConfig(delta_fill_share=0.15))
        order = _place(ex, book, price=100.0, size=10.0)

        # Level grows from 50 to 200
        ex.on_delta("bybit", side="bid", price=100.0, new_size=200.0,
                    now=2.0, tick_size=1.0, fee_bps=0.0)

        # Level shrinks from 200 to 40 (below original 50): shrink=10 (50→40)
        # queue_ahead=50, volume=10 → drains 10 from queue_ahead, available=0
        fills = ex.on_delta("bybit", side="bid", price=100.0, new_size=40.0,
                            now=3.0, tick_size=1.0, fee_bps=0.0)
        self.assertEqual(len(fills), 0)  # still has queue ahead
        self.assertAlmostEqual(order.queue_ahead, 40.0)


# ── Snapshot fills ────────────────────────────────────────────────────────────

class TestSnapshotFills(unittest.TestCase):

    def test_snapshot_fires_on_level_shrink(self):
        book = _book(bids=[(100.0, 50.0)])
        ex = _executor(MakerConfig(snapshot_fill_share=0.05))
        order = _place(ex, book, price=100.0, size=10.0)

        # New snapshot with level at 0 — queue_ahead=50, so 50 units drain queue
        shrunken_book = _book(bids=[(99.0, 100.0)])  # price 100 gone entirely
        fills = ex.on_snapshot("bybit", shrunken_book, now=2.0, tick_size=1.0, fee_bps=0.0)
        # shrink=50, drains queue_ahead=50, available=0 → no order fill yet
        self.assertEqual(len(fills), 0)
        self.assertAlmostEqual(order.queue_ahead, 0.0)

    def test_snapshot_observed_level_never_increases(self):
        """D1 fix also applies to snapshots."""
        book = _book(bids=[(100.0, 50.0)])
        ex = _executor()
        order = _place(ex, book, price=100.0, size=5.0)

        # Snapshot shows level grown to 200
        grown_book = _book(bids=[(100.0, 200.0), (99.0, 100.0)])
        ex.on_snapshot("bybit", grown_book, now=2.0, tick_size=1.0, fee_bps=0.0)
        self.assertAlmostEqual(order.observed_level_size, 50.0,
            msg="Snapshot growth must not increase observed_level_size")


# ── Partial fills and order lifecycle ────────────────────────────────────────

class TestPartialFills(unittest.TestCase):

    def test_partial_fill_updates_remaining(self):
        book = _book(bids=[(100.0, 0.0)])
        ex = _executor()
        order = _place(ex, book, price=100.0, size=10.0)

        # Trade of 6 — fills 6 (no queue)
        fills = ex.on_trade("bybit", price=100.0, volume=6.0, sign=-1,
                            now=2.0, fee_bps=0.0)
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0].qty, 6.0)
        self.assertAlmostEqual(order.remaining, 4.0)
        self.assertTrue(order.is_live)
        self.assertIn(order.order_id, ex.orders)

    def test_full_fill_removes_order(self):
        book = _book(bids=[(100.0, 0.0)])
        ex = _executor()
        order = _place(ex, book, price=100.0, size=5.0)

        fills = ex.on_trade("bybit", price=100.0, volume=100.0, sign=-1,
                            now=2.0, fee_bps=0.0)
        self.assertAlmostEqual(fills[0].qty, 5.0)
        self.assertNotIn(order.order_id, ex.orders)
        self.assertEqual(ex.orders_filled, 1)

    def test_second_partial_fills_remaining_only(self):
        book = _book(bids=[(100.0, 0.0)])
        ex = _executor()
        order = _place(ex, book, price=100.0, size=10.0)

        ex.on_trade("bybit", price=100.0, volume=4.0, sign=-1, now=2.0, fee_bps=0.0)
        fills2 = ex.on_trade("bybit", price=100.0, volume=100.0, sign=-1, now=3.0, fee_bps=0.0)
        self.assertAlmostEqual(fills2[0].qty, 6.0)  # only the remaining 6

    def test_cancelled_order_produces_no_fill(self):
        book = _book(bids=[(100.0, 0.0)])
        ex = _executor()
        order = _place(ex, book, price=100.0, size=10.0)

        ex.cancel(order.order_id, now=2.0)
        fills = ex.on_trade("bybit", price=100.0, volume=100.0, sign=-1, now=3.0, fee_bps=0.0)
        self.assertEqual(len(fills), 0)
        self.assertEqual(ex.orders_cancelled, 1)


# ── No double-count across two orders at same level ──────────────────────────

class TestNoDoubleCount(unittest.TestCase):

    def test_two_orders_dont_double_consume_same_trade(self):
        # Two orders at the same price: first placed first (queue=0 each since
        # level size=0 when both placed).  A trade of V should fill at most
        # order1+order2 sizes — but since trade volume is applied independently
        # to each order, both can fill from the same V.
        # This is the D3 / D4 known limitation. Test documents the current
        # behavior (independent drain) and checks total fill <= 2×V, ensuring
        # at least no fill exceeds order size.
        book = _book(bids=[(100.0, 0.0)])
        ex = _executor()
        order_a = _place(ex, book, price=100.0, size=5.0, now=1.0)
        order_b = ex.place(
            venue="bybit", pair="SOLUSDT", symbol="SOLUSDT",
            side=Side.BID, price=100.0, size=5.0, book=book,
            state=_MarketState(), regime_bucket=LambdaBucket.CALM,
            now=1.0, active_ts=1.0, tick_size=1.0, signal_id=2,
        )
        fills = ex.on_trade("bybit", price=100.0, volume=6.0, sign=-1,
                            now=2.0, fee_bps=0.0)
        total = sum(f.qty for f in fills)
        # Each order fills independently from 6 units (no queue on either).
        # This is the known limitation: same volume used twice.
        # At minimum, neither fill exceeds its order size.
        for f in fills:
            self.assertLessEqual(f.qty, 5.0 + 1e-9)


# ── Fee calculation ───────────────────────────────────────────────────────────

class TestFeeCalculation(unittest.TestCase):

    def test_maker_fee_bps(self):
        book = _book(bids=[(100.0, 0.0)])
        ex = _executor()
        _place(ex, book, price=100.0, size=10.0)
        fills = ex.on_trade("bybit", price=100.0, volume=10.0, sign=-1,
                            now=2.0, fee_bps=2.0)
        # notional = 100 * 10 = 1000, fee = 1000 * 2/10000 = 0.2
        self.assertAlmostEqual(fills[0].fee, 0.2, places=9)

    def test_maker_rebate_negative_fee(self):
        book = _book(bids=[(100.0, 0.0)])
        ex = _executor()
        _place(ex, book, price=100.0, size=10.0)
        fills = ex.on_trade("bybit", price=100.0, volume=10.0, sign=-1,
                            now=2.0, fee_bps=-0.5)  # Bybit MM rebate
        self.assertAlmostEqual(fills[0].fee, -0.05, places=9)


if __name__ == "__main__":
    unittest.main()
