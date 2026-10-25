"""In-flight maker-order tracking and duplicate-order prevention in src/new_backtest.

What is counted toward ArbConfig.maker_max_live_orders (CrossVenueArbBacktester._live_maker_orders):
  resting orders (including partially filled ones: they stay in maker.orders while remaining > 0),
  cancel-pending orders (same dict), maker legs still in flight, and orders already cancelled at the exchange whose
  cancellation the strategy cannot have heard about yet (one feed delay).
Scope of the limit: the whole pair, across both venues and both sides.

Reservation is atomic by construction. `strategy.evaluate` and `_fire` run back to back inside one loop iteration; `_fire`
puts every leg on the pending queue immediately, so the next decision sees it. The replay test below checks that nothing
runs between the two.

Not modelled by this simulator (so not tested as behaviour): exchange acknowledgements/rejections, ambiguous timeouts,
disconnects and reconnects, execution ids, duplicate or out-of-order exchange messages. The core OMS in
src/crypto_stat_arb has that machinery; see tests/test_oms_order_tracking.py.
"""
from __future__ import annotations

import tempfile
import unittest
import warnings
from pathlib import Path

from new_backtest.arb_strategy import MAKER_TAKER, ArbConfig
from new_backtest.backtester import ArbBacktestConfig, CrossVenueArbBacktester
from new_backtest.fill_engine import MakerConfig

from synthetic_days import gen
from test_maker_order_lifetime import BINANCE, BYBIT, Harness

BYBIT_BOOK = dict(bids=((100.90, 50.0), (100.89, 50.0)), asks=((101.00, 50.0), (101.01, 50.0)))
BINANCE_BOOK = dict(bids=((101.05, 50.0), (101.04, 50.0)), asks=((101.06, 50.0), (101.07, 50.0)))


def market(h: Harness, t: float) -> None:
    """Fresh books on both venues at time t; a maker bid on bybit is profitable against the binance bid."""
    h.book(t, BYBIT, **BYBIT_BOOK)
    h.book(t, BINANCE, **BINANCE_BOOK)
    for v in (BYBIT, BINANCE):
        h.bt.tick_size[v] = 0.01


def warm(h: Harness) -> None:
    """The venue state needs two book updates before the strategy treats a venue as live."""
    market(h, 0.0)


def decide(h: Harness, t: float):
    """Exactly what run() does at a touch change: evaluate with the live count, fire if a signal comes back."""
    bt = h.bt
    blocked = bt.strategy.rejects["maker_limit"]
    sig = bt.strategy.evaluate(bt.books, bt.states, bt.inventory, t, live_maker_orders=bt._live_maker_orders(t))
    if sig is not None:
        bt._fire(sig, t)
    return sig, bt.strategy.rejects["maker_limit"] - blocked


def executable(bt) -> int:
    """True number of maker orders that could still execute, straight from the two stores (no helper in between)."""
    return len(bt.maker.orders) + bt.pending.maker_legs_in_flight()


class SlotAccountingTests(unittest.TestCase):
    def test_a_pending_submission_consumes_the_slot_before_it_reaches_the_exchange(self):
        h = Harness(ttl=0.0, entry_s=0.5, max_clip_base=1.0)
        warm(h)
        market(h, 0.01)
        sig, _ = decide(h, 0.01)
        self.assertIsNotNone(sig)
        self.assertEqual((h.bt.orders_sent, h.resting, h.live(0.01)), (1, [], 1))     # in flight only
        sig2, blocked = decide(h, 0.02)
        self.assertEqual((sig2, blocked, h.bt.orders_sent), (None, 1, 1))

    def test_repeated_evaluations_cannot_generate_duplicates_while_the_first_order_is_in_flight(self):
        h = Harness(ttl=0.0, entry_s=1.0, max_clip_base=1.0)
        warm(h)
        fired, blocked_total = 0, 0
        for i in range(90):                                  # a decision every 10 ms for 0.9 s, order lands at 1.0 s
            t = 0.01 + i * 0.01
            market(h, t)
            sig, blocked = decide(h, t)
            fired += sig is not None
            blocked_total += blocked
            self.assertLessEqual(executable(h.bt), 1)
        self.assertEqual((fired, h.bt.orders_sent, blocked_total), (1, 1, 89))

    def test_acknowledgement_moves_an_order_from_in_flight_to_resting_without_double_counting(self):
        h = Harness(ttl=0.0, entry_s=0.5, max_clip_base=1.0)
        warm(h)
        market(h, 0.01)
        decide(h, 0.01)
        seen = []
        for t in (0.11, 0.5099, 0.51, 0.61):
            market(h, t)
            seen.append((len(h.resting), h.bt.pending.maker_legs_in_flight(), h.live(t)))
        self.assertEqual(seen, [(0, 1, 1), (0, 1, 1), (1, 0, 1), (1, 0, 1)])

    def test_a_missed_arrival_releases_the_slot_exactly_once(self):
        # The simulator's only rejection: the order cannot rest (no usable book). It is resolved as a miss.
        h = Harness(ttl=0.0, max_clip_base=1.0)
        h.book(0.0, BINANCE, **BINANCE_BOOK)                 # bybit book never initialised
        h.send_maker(0.0)
        self.assertEqual(h.live(0.01), 1)
        h.advance(0.013)
        for t in (0.013, 0.014, 1.0, 100.0):
            h.advance(t)
        self.assertEqual((h.live(100.01), h.bt.orders_missed, h.bt.miss_reasons["not_ready"], h.bt._trackers), (0, 1, 1, {}))

    def test_a_full_fill_frees_the_slot_at_once_and_the_next_decision_goes_through(self):
        h = Harness(ttl=0.0, max_clip_base=1.0)
        warm(h)
        market(h, 0.01)
        decide(h, 0.01)
        market(h, 0.014)
        o = h.order()
        self.assertEqual((o.size, o.remaining), (1.0, 1.0))
        h.sell_trade(0.02, BYBIT, volume=1.6, price=100.98)   # 0.5 queue + 1.0 fills the whole order
        self.assertEqual((h.resting, h.live(0.02)), ([], 0))
        market(h, 0.03)
        sig, blocked = decide(h, 0.03)
        self.assertEqual((sig is not None, blocked), (True, 0))

    def test_the_limit_is_pair_wide_not_per_venue_or_side(self):
        h = Harness(ttl=0.0, entry_s=1.0, max_live=2, max_clip_base=1.0)
        warm(h)
        fired = 0
        for i in range(10):
            market(h, 0.01 + i * 0.01)
            sig, _ = decide(h, 0.01 + i * 0.01)
            fired += sig is not None
        self.assertEqual((fired, executable(h.bt)), (2, 2))

    def test_the_legacy_switch_warns_and_still_stacks_orders(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            h = Harness(ttl=0.0, entry_s=1.0, max_clip_base=1.0, maker_count_inflight=False)
        self.assertTrue(any("maker_count_inflight" in str(x.message) and issubclass(x.category, RuntimeWarning) for x in w))
        warm(h)
        fired = 0
        for i in range(30):
            market(h, 0.01 + i * 0.01)
            sig, _ = decide(h, 0.01 + i * 0.01)
            fired += sig is not None
        self.assertEqual(fired, 30, "legacy mode repeats the same signal until the first order lands")

    def test_the_default_does_not_warn_about_inflight_counting(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Harness(ttl=10.0)
        self.assertFalse(any("maker_count_inflight" in str(x.message) for x in w))


class PartialFillTests(unittest.TestCase):
    def test_a_partial_fill_keeps_the_remainder_executable_and_the_slot_taken_then_expiry_keeps_the_fill(self):
        h = Harness(ttl=10.0, cancel_s=0.05, max_clip_base=1.0)
        feed = h.feed_s[BYBIT]
        warm(h)
        market(h, 0.01)
        decide(h, 0.01)
        market(h, 0.014)
        o = h.order()
        self.assertEqual((o.size, o.remaining, o.queue_ahead), (1.0, 1.0, 0.5))
        h.sell_trade(0.02, BYBIT, volume=0.8, price=100.98)   # 0.5 drains the queue, 0.3 fills
        self.assertAlmostEqual(o.filled_qty, 0.3, places=9)
        self.assertAlmostEqual(o.remaining, 0.7, places=9)
        self.assertEqual((len(h.resting), h.live(0.02)), (1, 1))
        for t in (0.03, 0.04):                                # the strategy keeps looking: no duplicate is allowed
            market(h, t)
            sig, blocked = decide(h, t)
            self.assertEqual((sig, blocked), (None, 1))
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], 0.3, places=9)
        h.advance(0.51)                                        # hedge for the 0.3 has landed
        self.assertAlmostEqual(h.bt.inventory.base[BINANCE], -0.3, places=9)
        h.assert_order_invariants(self, o)
        h.assert_ledger_reconciles(self)
        # expiry cancels the 0.7, keeps the 0.3, and the slot frees only after the cancel is heard about
        placed = o.placed_ts
        market(h, placed + 10.0)                              # request
        self.assertEqual(h.order().state, "cancel_pending")
        self.assertEqual((len(h.resting), h.live(placed + 10.0)), (1, 1))
        landed = placed + 10.0 + 0.05
        market(h, landed)
        self.assertEqual(h.resting, [])
        self.assertEqual(h.live(landed), 1)                    # cancelled, not yet acknowledged
        sig, blocked = decide(h, landed)
        self.assertEqual((sig, blocked), (None, 1))
        heard = landed + feed + 1e-6
        market(h, heard)
        self.assertEqual(h.live(heard), 0)
        sig, blocked = decide(h, heard)
        self.assertEqual((sig is not None, blocked), (True, 0))
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], 0.3, places=9)
        self.assertAlmostEqual(h.bt.inventory.net_base, 0.0, places=9)
        h.assert_ledger_reconciles(self)
        lt = h.lifetime()
        self.assertEqual((lt["expired_after_partial_fill"], lt["cancels_applied"]), (1.0, 1.0))


class ReplayInvariantTests(unittest.TestCase):
    """12 synthetic days with the real strategy: the limit holds at every decision and nothing is left behind."""

    @staticmethod
    def _run(root, out, ttl):
        cfg = ArbBacktestConfig(
            data_root=str(root), venues=(BYBIT, BINANCE), pair="BTCUSDT", start_date="2026-10-01", end_date="2026-10-12",
            latency_preset="colocated_both", bybit_fee_preset="bybit_perp_mm", binance_fee_preset="binance_perp_vip0",
            maker=MakerConfig(min_queue_ahead=0.8),
            arb=ArbConfig(mode=MAKER_TAKER, min_net_edge_bps=1.0, max_clip_notional=2_000.0, max_adverse_prob=0.15,
                          max_ttf_s=8.0, max_cross_venue_skew_s=0.2, fill_gate_horizon_s=5, maker_max_age_s=ttl),
            output_dir=str(out))
        bt = CrossVenueArbBacktester(cfg)
        limit = cfg.arb.maker_max_live_orders
        log, violations, gaps = [], [], []
        real_eval, real_fire = bt.strategy.evaluate, bt._fire
        state = {"pending_event": None}

        def evaluate(*a, **k):
            if executable(bt) > limit:
                violations.append(("before-evaluate", executable(bt)))
            sig = real_eval(*a, **k)
            state["pending_event"] = bt.events_processed if sig is not None else None
            log.append((round(a[3], 9), sig is not None))
            return sig

        def fire(sig, now):
            if state["pending_event"] != bt.events_processed:
                gaps.append(("event processed between evaluate and fire", bt.events_processed))
            real_fire(sig, now)
            state["pending_event"] = None
            if executable(bt) > limit:
                violations.append(("after-fire", executable(bt)))

        bt.strategy.evaluate, bt._fire = evaluate, fire
        result = bt.run()
        return bt, result, log, violations, gaps

    def test_limit_and_atomic_reservation_hold_over_twelve_days_and_nothing_is_left_behind(self):
        with tempfile.TemporaryDirectory() as d, warnings.catch_warnings():
            warnings.simplefilter("ignore")
            root = Path(d) / "data"
            gen(root, ndays=12, seed=11, session_s=30.0)
            for ttl in (30.0, 0.0):
                with self.subTest(ttl=ttl):
                    bt, r, log, violations, gaps = self._run(root, Path(d) / f"o{ttl}", ttl)
                    self.assertEqual(violations, [], "executable maker orders exceeded maker_max_live_orders")
                    self.assertEqual(gaps, [], "evaluate and _fire must run back to back")
                    self.assertGreater(sum(1 for _, fired in log if fired), 0)
                    self.assertEqual(bt.pending.pending_count, 0)
                    self.assertEqual(bt.pending._cancelled, set())
                    self.assertEqual((bt.maker.orders, bt._trackers, bt._cancel_watch), ({}, {}, {}))
                    self.assertEqual(sum(1 for o in bt.maker.orders.values()), 0)
                    # every maker order placed is accounted for: filled, or closed as a cancel / at data end
                    ms = r.maker_stats
                    self.assertEqual(ms["maker_orders_placed"], ms["maker_orders_filled"] + ms["maker_orders_cancelled"])

    def test_the_decision_sequence_is_deterministic(self):
        with tempfile.TemporaryDirectory() as d, warnings.catch_warnings():
            warnings.simplefilter("ignore")
            root = Path(d) / "data"
            gen(root, ndays=12, seed=11, session_s=30.0)
            a = self._run(root, Path(d) / "a", 30.0)
            b = self._run(root, Path(d) / "b", 30.0)
            self.assertEqual(a[2], b[2])
            self.assertEqual((a[0].orders_sent, a[0].orders_filled, a[0].fees_paid, a[0].inventory.base),
                             (b[0].orders_sent, b[0].orders_filled, b[0].fees_paid, b[0].inventory.base))


if __name__ == "__main__":
    unittest.main()
