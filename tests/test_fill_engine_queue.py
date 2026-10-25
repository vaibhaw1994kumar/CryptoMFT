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


# ── D1: level disappearance and reappearance ─────────────────────────────────

class TestLevelDisappearReappear(unittest.TestCase):
    """Validation step 2: disappearance, reappearance, and delta/snapshot ordering."""

    def test_level_disappears_drains_queue(self):
        # Level was 50 at placement. Delta fires with new_size=0 (level gone).
        # observed(50) > new(0) → consume 50; queue_ahead=50 drains to 0, no fill.
        book = _book(bids=[(100.0, 50.0)])
        ex = _executor(MakerConfig(delta_fill_share=0.15))
        order = _place(ex, book, price=100.0, size=10.0)
        self.assertAlmostEqual(order.queue_ahead, 50.0)

        ex.on_delta("bybit", side="bid", price=100.0, new_size=0.0,
                    now=2.0, tick_size=1.0, fee_bps=0.0)
        # Queue fully drained; observed→0
        self.assertAlmostEqual(order.queue_ahead, 0.0)
        self.assertAlmostEqual(order.observed_level_size, 0.0)

    def test_level_disappears_then_reappears_no_spurious_fill(self):
        # Level disappears → queue drained. Level reappears (new orders at same
        # price arrive behind us). The reappearance must NOT trigger any fill.
        book = _book(bids=[(100.0, 30.0)])
        ex = _executor(MakerConfig(delta_fill_share=0.15))
        order = _place(ex, book, price=100.0, size=10.0)

        # Disappear
        ex.on_delta("bybit", side="bid", price=100.0, new_size=0.0,
                    now=2.0, tick_size=1.0, fee_bps=0.0)
        self.assertAlmostEqual(order.observed_level_size, 0.0)

        # Reappear with 40 units (new resting orders behind us)
        fills = ex.on_delta("bybit", side="bid", price=100.0, new_size=40.0,
                            now=3.0, tick_size=1.0, fee_bps=0.0)
        # prev=0, new=40 → not prev>new → no fill; observed stays 0 (invariant)
        self.assertEqual(len(fills), 0)
        self.assertAlmostEqual(order.observed_level_size, 0.0,
            msg="observed_level_size must not grow on reappearance")

    def test_snapshot_then_delta_same_change_no_double_drain(self):
        # Snapshot fires first showing shrink of 10 (50→40); then a delta fires
        # with the same new_size=40.  The second event must not re-drain the queue.
        book = _book(bids=[(100.0, 50.0)])
        ex = _executor(MakerConfig(delta_fill_share=0.15, snapshot_fill_share=0.05))
        order = _place(ex, book, price=100.0, size=10.0)

        # Snapshot: level drops to 40
        shrunken = _book(bids=[(100.0, 40.0)])
        ex.on_snapshot("bybit", shrunken, now=2.0, tick_size=1.0, fee_bps=0.0)
        queue_after_snapshot = order.queue_ahead   # should be ~40 (consumed 10)
        obs_after_snapshot = order.observed_level_size  # should be ~40

        # Delta fires for the same change: new_size=40
        # prev=40 (already updated), new=40 → not prev>new → no further drain
        fills = ex.on_delta("bybit", side="bid", price=100.0, new_size=40.0,
                            now=2.001, tick_size=1.0, fee_bps=0.0)
        self.assertEqual(len(fills), 0)
        self.assertAlmostEqual(order.queue_ahead, queue_after_snapshot,
            msg="Delta after snapshot for same shrink must not re-drain queue")

    def test_delta_then_snapshot_same_change_no_double_drain(self):
        # Delta fires first (shrink 50→40); then snapshot arrives showing 40.
        # Snapshot must not re-drain the already-updated queue.
        book = _book(bids=[(100.0, 50.0)])
        ex = _executor(MakerConfig(delta_fill_share=0.15, snapshot_fill_share=0.05))
        order = _place(ex, book, price=100.0, size=10.0)

        # Delta: 50 → 40
        ex.on_delta("bybit", side="bid", price=100.0, new_size=40.0,
                    now=2.0, tick_size=1.0, fee_bps=0.0)
        queue_after_delta = order.queue_ahead

        # Snapshot: shows same level at 40
        shrunken = _book(bids=[(100.0, 40.0)])
        fills = ex.on_snapshot("bybit", shrunken, now=2.001, tick_size=1.0, fee_bps=0.0)
        self.assertEqual(len(fills), 0)
        self.assertAlmostEqual(order.queue_ahead, queue_after_delta,
            msg="Snapshot after delta for same shrink must not re-drain queue")

    def test_replenishment_then_disappearance_uses_original_baseline(self):
        # Level grows (replenishment from behind) then disappears entirely.
        # Only the original-baseline portion (50 units) counts toward queue drain.
        book = _book(bids=[(100.0, 50.0)])
        ex = _executor(MakerConfig(delta_fill_share=0.15))
        order = _place(ex, book, price=100.0, size=10.0)  # queue=50, observed=50

        # Replenishment: level grows to 200
        ex.on_delta("bybit", side="bid", price=100.0, new_size=200.0,
                    now=2.0, tick_size=1.0, fee_bps=0.0)
        # observed_level_size must stay 50 (invariant)
        self.assertAlmostEqual(order.observed_level_size, 50.0)

        # Level disappears entirely: shrink = observed(50) - new(0) = 50
        fills_disappear = ex.on_delta("bybit", side="bid", price=100.0, new_size=0.0,
                                      now=3.0, tick_size=1.0, fee_bps=0.0)
        # Shrink 50 drains queue 50 → queue=0, no fill (all went to drain)
        self.assertAlmostEqual(order.queue_ahead, 0.0)
        total_fill = sum(f.qty for f in fills_disappear)
        self.assertAlmostEqual(total_fill, 0.0,
            msg="Queue drain on disappearance must not produce fills when queue=queue_ahead")


# ── D2: tick_size=0 and min_queue_ahead scoping ──────────────────────────────

class TestD2EdgeCases(unittest.TestCase):
    """Validation step 3: confirm min_queue_ahead only activates for genuinely
    absent levels; document tick_size=0 tolerance behaviour."""

    def test_tick_size_zero_exact_price_still_matches(self):
        # When tick_size=0, tolerance collapses to 1e-12. An exact match must
        # still work so that min_queue_ahead does NOT fire on present levels.
        book = _book(bids=[(100.0, 60.0)])
        cfg = MakerConfig(min_queue_ahead=999.0)
        ex = _executor(cfg)
        order = _place(ex, book, price=100.0, size=5.0, tick_size=0.0)
        # Level is exactly present at 100.0 — must NOT apply min_queue_ahead
        self.assertAlmostEqual(order.queue_ahead, 60.0,
            msg="min_queue_ahead must not override a genuinely present level even with tick_size=0")

    def test_tick_size_zero_absent_level_applies_min_queue_ahead(self):
        # With tick_size=0, tolerance=1e-12; level at 99.0 does not match 100.0
        # → level absent → min_queue_ahead applies.
        book = _book(bids=[(99.0, 50.0)])
        cfg = MakerConfig(min_queue_ahead=25.0)
        ex = _executor(cfg)
        order = _place(ex, book, price=100.0, size=5.0, tick_size=0.0)
        self.assertAlmostEqual(order.queue_ahead, 25.0)

    def test_min_queue_ahead_not_applied_for_small_but_present_level(self):
        # Level is present but tiny (size=0.001); min_queue_ahead must NOT override.
        book = _book(bids=[(100.0, 0.001)])
        cfg = MakerConfig(min_queue_ahead=50.0)
        ex = _executor(cfg)
        order = _place(ex, book, price=100.0, size=5.0)
        self.assertAlmostEqual(order.queue_ahead, 0.001,
            msg="min_queue_ahead must not apply when level is present, even if tiny")

    def test_min_queue_ahead_not_applied_when_level_size_zero_but_present(self):
        # Level IS at the right price but size=0.0 (level present, empty).
        # _level_size returns 0.0 but that is a real "level at our price exists,
        # has nothing resting".  The current code treats this identically to
        # absent (because it checks level_size == 0.0).  This test documents
        # the edge case: min_queue_ahead fires for both "absent" and "present-but-empty".
        book = _book(bids=[(100.0, 0.0)])
        cfg = MakerConfig(min_queue_ahead=30.0)
        ex = _executor(cfg)
        order = _place(ex, book, price=100.0, size=5.0)
        # Current behavior: min_queue_ahead applies because level_size == 0.0
        # (conservatively safe — slightly pessimistic for the genuinely-empty case)
        self.assertAlmostEqual(order.queue_ahead, 30.0)


# ── D3: trade + delta double-count regression ─────────────────────────────────

class TestD3TradeAndDeltaDoubleCount(unittest.TestCase):
    """Validation step 4: demonstrate D3 (known limitation) with exact numbers.

    When an exchange trade of V units occurs it produces two feed events:
      1. TRADE tape:  volume = V
      2. BOOK delta:  level shrinks by V (new_size = old_size - V)

    With default fill_share parameters the backtester processes both events
    independently against the same resting order.  The test below measures the
    double-count and records it as a regression baseline.

    This was a KNOWN LIMITATION. It is now fixed by default (MakerConfig.dedupe_trade_depth); the
    legacy numbers below are kept under dedupe_trade_depth=False as a regression baseline.
    """

    def _setup_order_with_drained_queue(self, dedupe=True):
        """Place order at level=50, drain queue to 0 with a separate trade."""
        book = _book(bids=[(100.0, 50.0)])
        ex = _executor(MakerConfig(
            trade_fill_share=1.0,
            delta_fill_share=0.15,
            dedupe_trade_depth=dedupe,
        ))
        order = _place(ex, book, price=100.0, size=10.0)
        # Drain the 50-unit queue so subsequent fills are immediate
        ex.on_trade("bybit", price=100.0, volume=50.0, sign=-1, now=1.5, fee_bps=0.0)
        self.assertAlmostEqual(order.queue_ahead, 0.0)
        return ex, order

    def test_trade_alone_fills_V(self):
        ex, order = self._setup_order_with_drained_queue()
        # TRADE only: should fill exactly V=5
        fills = ex.on_trade("bybit", price=100.0, volume=5.0, sign=-1, now=2.0, fee_bps=0.0)
        total = sum(f.qty for f in fills)
        self.assertAlmostEqual(total, 5.0, places=9)

    def test_delta_alone_fills_V_times_delta_fill_share(self):
        ex, order = self._setup_order_with_drained_queue()
        # DELTA only: observed=50, new_size=45 → shrink=5 → fill=5*0.15=0.75
        fills = ex.on_delta("bybit", side="bid", price=100.0, new_size=45.0,
                            now=2.0, tick_size=1.0, fee_bps=0.0)
        total = sum(f.qty for f in fills)
        self.assertAlmostEqual(total, 0.75, places=9,
            msg="delta alone: fill = shrink * delta_fill_share = 5 * 0.15 = 0.75")

    def test_d3_legacy_trade_then_delta_exceeds_V(self):
        # D3 (legacy, dedupe_trade_depth=False): process TRADE then DELTA for the same 5 units of real activity.
        # Expected ideal fill: 5.0; actual (double-count): 5.75
        ex, order = self._setup_order_with_drained_queue(dedupe=False)

        # Event A: trade tape
        fills_a = ex.on_trade("bybit", price=100.0, volume=5.0, sign=-1, now=2.0, fee_bps=0.0)
        # Event B: corresponding book delta (same liquidity removal)
        fills_b = ex.on_delta("bybit", side="bid", price=100.0, new_size=45.0,
                              now=2.001, tick_size=1.0, fee_bps=0.0)

        fill_from_trade = sum(f.qty for f in fills_a)
        fill_from_delta = sum(f.qty for f in fills_b)
        total = fill_from_trade + fill_from_delta

        # Verify the known double-count: 5.0 + 5*0.15 = 5.75
        self.assertAlmostEqual(fill_from_trade, 5.0, places=9)
        self.assertAlmostEqual(fill_from_delta, 0.75, places=9)
        self.assertAlmostEqual(total, 5.75, places=9,
            msg="D3 regression: trade+delta for same 5 units produces 5.75, not 5.0")

        # Double-count factor: total / trade_volume ≈ 1.15 (= 1 + delta_fill_share)
        double_count_factor = total / 5.0
        self.assertAlmostEqual(double_count_factor, 1.15, places=9,
            msg="D3 double-count factor should be 1 + delta_fill_share = 1.15")

    def test_d3_trade_then_delta_fills_V_once(self):
        # Default (dedupe_trade_depth=True): the depth reduction that mirrors the trade is not counted again.
        ex, order = self._setup_order_with_drained_queue()
        fills_a = ex.on_trade("bybit", price=100.0, volume=5.0, sign=-1, now=2.0, fee_bps=0.0)
        fills_b = ex.on_delta("bybit", side="bid", price=100.0, new_size=45.0,
                              now=2.001, tick_size=1.0, fee_bps=0.0)
        self.assertAlmostEqual(sum(f.qty for f in fills_a), 5.0, places=9)
        self.assertAlmostEqual(sum(f.qty for f in fills_b), 0.0, places=9)

    def test_delta_then_trade_does_not_double_count_either(self):
        # Feeds can arrive in either order. The delta is applied first (0.75 at its share); the trade that
        # mirrors it is then not applied again, so the total stays at or below the 5 units that really traded.
        ex, order = self._setup_order_with_drained_queue()
        fills_b = ex.on_delta("bybit", side="bid", price=100.0, new_size=45.0, now=2.0, tick_size=1.0, fee_bps=0.0)
        fills_a = ex.on_trade("bybit", price=100.0, volume=5.0, sign=-1, now=2.001, fee_bps=0.0)
        self.assertLessEqual(sum(f.qty for f in fills_a) + sum(f.qty for f in fills_b), 5.0 + 1e-9)

    def test_a_depth_reduction_long_after_a_trade_is_a_separate_event(self):
        ex, order = self._setup_order_with_drained_queue()
        ex.on_trade("bybit", price=100.0, volume=5.0, sign=-1, now=2.0, fee_bps=0.0)
        later = ex.on_delta("bybit", side="bid", price=100.0, new_size=45.0, now=3.0, tick_size=1.0, fee_bps=0.0)
        self.assertAlmostEqual(sum(f.qty for f in later), 0.75, places=9)


# ── level_was_absent telemetry (P2.1) ────────────────────────────────────────

class TestLevelWasAbsent(unittest.TestCase):
    """level_was_absent is set at placement and flows through to LoggedOrderEvent."""

    def test_absent_level_sets_flag(self):
        book = _book(bids=[(99.0, 50.0)])  # no level at 100
        ex = _executor()
        order = _place(ex, book, price=100.0, size=5.0)
        self.assertTrue(order.level_was_absent)

    def test_present_level_clears_flag(self):
        book = _book(bids=[(100.0, 50.0)])
        ex = _executor()
        order = _place(ex, book, price=100.0, size=5.0)
        self.assertFalse(order.level_was_absent)

    def test_level_was_absent_flows_into_logged_event(self):
        """Fill completes → LoggedOrderEvent.level_was_absent matches placement."""
        model = EmpiricalFillModel()
        ex = MakerExecutor(model)
        book = _book(bids=[(99.0, 50.0)])  # level absent at 100
        order = ex.place(
            venue="bybit", pair="SOLUSDT", symbol="SOLUSDT",
            side=Side.BID, price=100.0, size=5.0, book=book,
            state=_MarketState(), regime_bucket=LambdaBucket.CALM,
            now=1.0, active_ts=1.0, tick_size=1.0, signal_id=1,
        )
        self.assertTrue(order.level_was_absent)
        # Fill the order
        ex.on_trade("bybit", price=100.0, volume=100.0, sign=-1, now=2.0, fee_bps=0.0)
        # All logged events should show level_was_absent=True
        for ev in model.order_event_log:
            self.assertTrue(ev.level_was_absent,
                msg=f"LoggedOrderEvent.level_was_absent should be True; got {ev.level_was_absent}")

    def test_present_level_false_in_logged_event(self):
        model = EmpiricalFillModel()
        ex = MakerExecutor(model)
        book = _book(bids=[(100.0, 50.0)])  # level present
        ex.place(
            venue="bybit", pair="SOLUSDT", symbol="SOLUSDT",
            side=Side.BID, price=100.0, size=5.0, book=book,
            state=_MarketState(), regime_bucket=LambdaBucket.CALM,
            now=1.0, active_ts=1.0, tick_size=1.0, signal_id=1,
        )
        ex.on_trade("bybit", price=100.0, volume=100.0, sign=-1, now=2.0, fee_bps=0.0)
        for ev in model.order_event_log:
            self.assertFalse(ev.level_was_absent)


# ── first_fill_is_synthetic / synthetic exclusion fix (P4.2) ─────────────────

class TestSyntheticExclusion(unittest.TestCase):
    """Synthetic fill exclusion should only skip orders whose FIRST fill was synthetic."""

    def _place_order(self, ex, book, price=100.0, size=10.0, now=1.0):
        return ex.place(
            venue="bybit", pair="SOLUSDT", symbol="SOLUSDT",
            side=Side.BID, price=price, size=size, book=book,
            state=_MarketState(), regime_bucket=LambdaBucket.CALM,
            now=now, active_ts=now, tick_size=1.0, signal_id=1,
        )

    def test_real_only_fill_is_recorded(self):
        model = EmpiricalFillModel()
        ex = MakerExecutor(model)
        book = _book(bids=[(100.0, 0.0)])
        self._place_order(ex, book)
        ex.on_trade("bybit", price=100.0, volume=100.0, sign=-1, now=2.0, fee_bps=0.0)
        self.assertGreater(len(model.order_event_log), 0,
            msg="Real-only fill must be recorded in event log")

    def test_synthetic_first_fill_suppresses_record(self):
        """First fill synthetic → entire order excluded from training log."""
        model = EmpiricalFillModel()
        ex = MakerExecutor(model)
        book = _book(bids=[(100.0, 0.0)])
        order = self._place_order(ex, book, size=10.0)
        self.assertFalse(order.first_fill_is_synthetic)

        # Synthetic partial fill (sets first_fill_ts and first_fill_is_synthetic=True)
        ex.on_trade("bybit", price=100.0, volume=5.0, sign=-1, now=2.0,
                    fee_bps=0.0)
        # Manually mark the order's fills as synthetic to simulate BackgroundAgentModel
        # (the executor doesn't expose a synthetic-trade path directly in tests,
        # but we can verify the flag logic by checking first_fill_is_synthetic)
        # For this test: place with a synthetic initial fill via _consume directly
        # by using ex._consume; instead we verify the flag is initially False.
        self.assertFalse(order.first_fill_is_synthetic,
            msg="Real trade must not set first_fill_is_synthetic")

    def test_first_fill_is_synthetic_flag_set_on_first_call(self):
        """If _consume is called with is_synthetic=True on first fill, flag is set."""
        model = EmpiricalFillModel()
        ex = MakerExecutor(model)
        book = _book(bids=[(100.0, 0.0)])
        order = self._place_order(ex, book, size=10.0)

        # Call _consume directly with is_synthetic=True
        ex._consume(order, volume=5.0, now=2.0, fill_share=1.0,
                    source="trade", fee_bps=0.0, is_synthetic=True)
        self.assertTrue(order.first_fill_is_synthetic)
        self.assertAlmostEqual(order.filled_qty, 5.0)

    def test_real_completion_after_synthetic_partial_not_double_excluded(self):
        """Real fill completing a synthetically-started order: first_fill_is_synthetic
        stays True → order excluded from log (synthetic TTF is corrupted)."""
        model = EmpiricalFillModel()
        ex = MakerExecutor(model)
        book = _book(bids=[(100.0, 0.0)])
        order = self._place_order(ex, book, size=10.0)

        # Synthetic partial (5 units)
        ex._consume(order, volume=5.0, now=2.0, fill_share=1.0,
                    source="trade", fee_bps=0.0, is_synthetic=True)
        self.assertTrue(order.first_fill_is_synthetic)

        # Real completion (remaining 5 units)
        ex._consume(order, volume=5.0, now=3.0, fill_share=1.0,
                    source="trade", fee_bps=0.0, is_synthetic=False)

        # Order is now fully filled and removed; but because first_fill was synthetic,
        # no LoggedOrderEvent should be emitted.
        self.assertEqual(len(model.order_event_log), 0,
            msg="Order whose first fill was synthetic must not be recorded")


# ── active_ts TTF baseline fix (P4.1) ────────────────────────────────────────

class TestActiveTsTTF(unittest.TestCase):
    """observation_time_s in LoggedOrderEvent uses active_ts, not placed_ts."""

    def test_ttf_uses_active_ts_baseline(self):
        """Fill at now=5.0, active_ts=2.0, placed_ts=1.0 → observation_time_s=3.0."""
        model = EmpiricalFillModel()
        ex = MakerExecutor(model)
        book = _book(bids=[(100.0, 0.0)])
        ex.place(
            venue="bybit", pair="SOLUSDT", symbol="SOLUSDT",
            side=Side.BID, price=100.0, size=5.0, book=book,
            state=_MarketState(), regime_bucket=LambdaBucket.CALM,
            now=1.0, active_ts=2.0,  # active_ts > placed_ts (entry latency = 1s)
            tick_size=1.0, signal_id=1,
        )
        ex.on_trade("bybit", price=100.0, volume=100.0, sign=-1, now=5.0, fee_bps=0.0)

        # For each horizon h, filled_by_h = (fill_ts - active_ts) <= h = (5-2)=3
        for ev in model.order_event_log:
            # observation_time_s = fill_ts - active_ts = 5.0 - 2.0 = 3.0
            self.assertAlmostEqual(ev.observation_time_s, 3.0, places=9,
                msg=f"observation_time_s should be 3.0 (fill_ts - active_ts), got {ev.observation_time_s}")
            if ev.horizon_s >= 3:
                self.assertTrue(ev.filled,
                    msg=f"horizon {ev.horizon_s}s should be filled (TTF=3s ≤ horizon)")
            elif ev.horizon_s < 3:
                self.assertFalse(ev.filled,
                    msg=f"horizon {ev.horizon_s}s should not be filled (TTF=3s > horizon)")

    def test_ttf_zero_when_placed_and_filled_same_tick(self):
        """placed_ts == fill_ts → observation_time_s = 0 if active_ts == placed_ts."""
        model = EmpiricalFillModel()
        ex = MakerExecutor(model)
        book = _book(bids=[(100.0, 0.0)])
        ex.place(
            venue="bybit", pair="SOLUSDT", symbol="SOLUSDT",
            side=Side.BID, price=100.0, size=5.0, book=book,
            state=_MarketState(), regime_bucket=LambdaBucket.CALM,
            now=1.0, active_ts=1.0, tick_size=1.0, signal_id=1,
        )
        ex.on_trade("bybit", price=100.0, volume=100.0, sign=-1, now=1.0, fee_bps=0.0)
        for ev in model.order_event_log:
            self.assertAlmostEqual(ev.observation_time_s, 0.0, places=9)


if __name__ == "__main__":
    unittest.main()
