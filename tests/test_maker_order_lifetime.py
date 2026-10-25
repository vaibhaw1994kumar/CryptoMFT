"""Maker-order lifetime, expiry and cancel handling in src/new_backtest.

Semantics under test (see CrossVenueArbBacktester._expire_maker_orders):
  * ArbConfig.maker_max_age_s is seconds from the order's arrival at the exchange (`placed_ts`); 0 disables expiry;
    negative or non-finite values raise. An order still in flight cannot expire and counts toward the live-order limit.
  * At placed_ts + ttl a cancel is requested. It takes effect after ONE sampled cancel latency, stored on the order.
    In between the order is `cancel_pending`: still resting, still fillable, still counted as live.
  * Tie rule: orders land, then expiries/cancels are applied, then the market event stamped `now`. A trade stamped
    exactly at the effective time therefore does not fill the order; one a hair earlier does.
  * The strategy's slot is released one feed delay after the cancel lands.

NOT modelled by this simulator, so not tested as behaviour: exchange acknowledgements and rejections, cancel
rejection, cancel timeouts and duplicate acknowledgements. Those scenarios are covered here only through the
invariants that do exist (see the *_Analog tests and the Limitations note in each).
"""
from __future__ import annotations

import math
import tempfile
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace

from new_backtest.arb_strategy import MAKER_TAKER, ArbConfig, ArbLeg, ArbSignal
from new_backtest.backtester import ArbBacktestConfig, CrossVenueArbBacktester
from new_backtest.event_loader import BOOK, TRADE
from new_backtest.fill_engine import MakerConfig
from new_backtest.fill_label import extract_labels

from synthetic_days import gen

BYBIT, BINANCE = "bybit", "binance"
DAY = 86_400.0


class Harness:
    """A backtester with pinned latencies, driven by hand through the same methods run() uses."""

    def __init__(self, ttl=10.0, entry_s=0.003, cancel_s=0.05, venues=(BYBIT, BINANCE), max_live=1, queue=0.5,
                 entry=None, cancel=None, **arb_kw):
        cfg = ArbBacktestConfig(
            data_root="unused", venues=venues, pair="BTCUSDT", start_date="2026-10-01", end_date="2026-10-01",
            latency_preset="colocated_both", bybit_fee_preset="bybit_perp_mm", binance_fee_preset="binance_perp_vip0",
            maker=MakerConfig(min_queue_ahead=queue),
            arb=ArbConfig(mode=MAKER_TAKER, maker_max_age_s=ttl, maker_max_live_orders=max_live, max_position_base=10.0, **arb_kw))
        self.bt = CrossVenueArbBacktester(cfg)
        self.entry = entry or {v: entry_s for v in venues}
        self.cancel = cancel or {v: cancel_s for v in venues}
        self.cancel_calls = 0
        self.bt.latency.entry_delay = lambda v: self.entry[v]
        self.bt.latency.cancel_delay = self._cancel_delay
        self._sid = 0
        self.feed_s = {v: self.bt.venue_specs[v].latency.feed_ms / 1000.0 for v in venues}

    def _cancel_delay(self, venue):
        self.cancel_calls += 1
        return self.cancel[venue]

    # ── driving ──
    def book(self, t, venue, bids=((99.0, 5.0), (98.0, 5.0)), asks=((101.0, 5.0), (102.0, 5.0))):
        self.event(t, BOOK, venue, {"bids": [list(b) for b in bids], "asks": [list(a) for a in asks]})
        self.bt.tick_size[venue] = 1.0

    def event(self, t, kind, venue, payload):
        self.bt._advance_to(t)
        self.bt._apply_event(SimpleNamespace(kind=kind, venue=venue, payload=payload, receive_ts=t))

    def sell_trade(self, t, venue, volume, price=100.0):
        """A sell aggressor hitting bids at `price`: reaches our bid quoted at `price`."""
        self.event(t, TRADE, venue, {"price": price, "volume": volume, "side": "s"})

    def advance(self, t):
        self.bt._advance_to(t)

    def send_maker(self, t, venue=BYBIT, qty=2.0, price=100.0, hedge_venue=None):
        hedge_venue = hedge_venue or (BINANCE if venue == BYBIT else BYBIT)
        self._sid += 1
        leg = ArbLeg(venue=venue, side="buy", qty=qty, limit_price=price, is_taker=False, expected_price=price, fee_bps=0.0)
        sig = ArbSignal(signal_id=self._sid, ts=t, buy_venue=venue, sell_venue=hedge_venue, qty=qty, gross_edge_bps=5.0,
                        fee_bps=1.0, net_edge_bps=4.0, legs=[leg], mode=MAKER_TAKER, buy_price=price, sell_price=price + 1)
        self.bt._fire(sig, t)
        return sig

    # ── observing ──
    @property
    def resting(self):
        return list(self.bt.maker.orders.values())

    def order(self, i=0):
        return self.resting[i]

    def live(self, t):
        return self.bt._live_maker_orders(t)

    def lifetime(self):
        return self.bt._lifetime_report()

    def assert_order_invariants(self, tc, order):
        tc.assertGreaterEqual(order.filled_qty, -1e-12)
        tc.assertLessEqual(order.filled_qty, order.size + 1e-9)
        tc.assertAlmostEqual(order.remaining + order.filled_qty, order.size, places=9)

    def assert_ledger_reconciles(self, tc):
        bt = self.bt
        signed = {v: 0.0 for v in bt.config.venues}
        cash = {v: 0.0 for v in bt.config.venues}
        for row in bt.trade_log:
            sgn = 1.0 if row["side"] == "buy" else -1.0
            signed[row["venue"]] += sgn * row["qty"]
            cash[row["venue"]] -= sgn * row["qty"] * row["price"]
        for v in bt.config.venues:
            tc.assertAlmostEqual(bt.inventory.base[v], signed[v], places=9, msg=f"base inventory on {v}")
        tc.assertAlmostEqual(bt.fees_paid, sum(r["fee"] for r in bt.trade_log), places=9)


def started(**kw):
    """Harness with both books up and one maker bid resting on bybit since t=0.003 (placed_ts == 0.003)."""
    h = Harness(**kw)
    h.book(0.0, BYBIT)
    h.book(0.0, BINANCE)
    h.send_maker(0.0)
    h.advance(0.003)
    return h


class ConfigTests(unittest.TestCase):
    def test_default_is_off_and_unchanged(self):
        self.assertEqual(ArbConfig().maker_max_age_s, 0.0)

    def test_negative_and_non_finite_lifetimes_are_rejected_not_silently_disabled(self):
        for bad in (-1.0, -0.001, float("nan"), float("inf"), "30", None, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ArbConfig(maker_max_age_s=bad)

    def test_a_lifetime_changed_after_construction_is_still_checked(self):
        h = Harness(ttl=5.0)
        h.bt.config.arb.maker_max_age_s = -5.0
        with self.assertRaises(ValueError):
            CrossVenueArbBacktester(h.bt.config)

    def test_zero_and_positive_are_accepted(self):
        for ok in (0, 0.0, 0.5, 30, 3600.0):
            ArbConfig(maker_max_age_s=ok)


class ExpiryLifecycleTests(unittest.TestCase):
    def test_basic_expiry_follows_request_then_effective_then_gone(self):
        h = started(ttl=10.0, cancel_s=0.05)
        o = h.order()
        self.assertEqual((o.state, o.placed_ts), ("active", 0.003))
        h.advance(10.0)                                   # one tick before the deadline: nothing happens
        self.assertEqual((h.order().state, h.lifetime()["expiries_triggered"]), ("active", 0.0))
        h.advance(10.003)                                 # deadline = placed_ts + ttl: cancel requested, not yet applied
        o = h.order()
        self.assertEqual(o.state, "cancel_pending")
        self.assertAlmostEqual(o.cancel_requested_ts, 10.003, places=9)
        self.assertAlmostEqual(o.cancel_effective_ts, 10.053, places=9)
        self.assertEqual(o.cancel_reason, "expired")
        self.assertEqual((len(h.resting), h.lifetime()["cancels_applied"]), (1, 0.0))
        h.advance(10.053)                                 # effective time: applied
        self.assertEqual(h.resting, [])
        lt = h.lifetime()
        self.assertEqual((lt["expiries_triggered"], lt["cancels_applied"]), (1.0, 1.0))
        self.assertAlmostEqual(lt["mean_cancel_pending_s"], 0.05, places=9)
        self.assertAlmostEqual(lt["mean_age_at_cancel_s"], 10.05, places=9)
        self.assertEqual(h.bt.maker.orders_cancelled, 1)

    def test_lifetime_disabled_keeps_orders_forever_and_never_touches_the_cancel_sampler(self):
        h = started(ttl=0.0)
        h.advance(30 * DAY)
        self.assertEqual((len(h.resting), h.order().state, h.cancel_calls), (1, "active", 0))
        self.assertEqual(sum(h.lifetime().values()), 0.0)

    def test_a_stuck_order_cannot_block_quoting_for_eleven_days_when_a_lifetime_is_set(self):
        for ttl, expect_blocked_after_11_days in ((30.0, False), (0.0, True)):
            with self.subTest(ttl=ttl):
                h = started(ttl=ttl)
                for day in range(1, 12):
                    h.advance(day * DAY)
                blocked = h.live(11 * DAY) >= 1
                self.assertEqual(blocked, expect_blocked_after_11_days)
                if not blocked:
                    h.send_maker(11 * DAY)                 # a replacement is allowed and rests normally
                    h.advance(11 * DAY + 0.003)
                    self.assertEqual(len(h.resting), 1)

    def test_in_flight_orders_cannot_expire_and_still_count_as_live(self):
        h = Harness(ttl=1.0, entry_s=5.0)
        h.book(0.0, BYBIT); h.book(0.0, BINANCE)
        h.send_maker(0.0)
        h.advance(3.0)                                    # well past ttl measured from SEND, still in flight
        self.assertEqual((h.resting, h.live(3.0)), ([], 1))
        self.assertEqual(h.lifetime()["expiries_triggered"], 0.0)
        h.advance(5.0)                                    # lands; clock starts here
        self.assertEqual((len(h.resting), h.order().placed_ts, h.order().state), (1, 5.0, "active"))
        h.advance(5.999)
        self.assertEqual(h.order().state, "active")
        h.advance(6.0)
        self.assertEqual(h.order().state, "cancel_pending")

    def test_late_arrival_into_an_unready_book_is_a_miss_and_leaves_no_phantom_order(self):
        # Analog of a late rejection: the simulator has no reject message, but an order that cannot rest is
        # resolved as a miss, is not counted as live, and does not leave its signal open.
        h = Harness(ttl=10.0)
        h.book(0.0, BINANCE)                              # bybit book never initialised
        h.send_maker(0.0)
        self.assertEqual(h.live(0.0), 1)
        h.advance(0.003)
        self.assertEqual((h.resting, h.live(0.003), h.bt.orders_missed), ([], 0, 1))
        self.assertEqual(h.bt._trackers, {})
        self.assertEqual(h.bt.miss_reasons["not_ready"], 1)

    def test_duplicate_expiry_processing_is_idempotent(self):
        h = started(ttl=10.0)
        h.advance(10.003)
        first = (h.order().cancel_requested_ts, h.order().cancel_effective_ts)
        for t in (10.003, 10.004, 10.01, 10.02, 10.03):
            h.bt._expire_maker_orders(t)
            h.advance(t)
        self.assertEqual((h.order().cancel_requested_ts, h.order().cancel_effective_ts), first)
        self.assertEqual((h.cancel_calls, h.lifetime()["expiries_triggered"]), (1, 1.0))
        h.advance(10.06)
        for t in (10.06, 10.07, 99.0):
            h.bt._expire_maker_orders(t)
        lt = h.lifetime()
        self.assertEqual((lt["cancels_applied"], h.bt.maker.orders_cancelled, h.cancel_calls), (1.0, 1, 1))

    def test_duplicate_cancel_and_duplicate_release_do_nothing(self):
        h = started(ttl=10.0)
        oid = h.order().order_id
        self.assertIsNotNone(h.bt.maker.cancel(oid, 1.0))
        self.assertIsNone(h.bt.maker.cancel(oid, 1.0))
        self.assertEqual(h.bt.maker.orders_cancelled, 1)
        self.assertEqual(list(h.bt.pending.release(1e9)), [])


class FillCancelRaceTests(unittest.TestCase):
    def test_fill_before_expiry_is_kept_and_nothing_expires(self):
        h = started(ttl=10.0, queue=0.5)
        h.sell_trade(5.0, BYBIT, volume=2.5)              # 0.5 drains the queue, 2.0 fills the whole order
        self.assertEqual(h.resting, [])
        h.advance(12.0)
        lt = h.lifetime()
        self.assertEqual((lt["expiries_triggered"], lt["cancels_applied"], lt["cancels_lost_to_fill"]), (0.0, 0.0, 0.0))
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], 2.0, places=9)
        h.assert_ledger_reconciles(self)

    def test_fill_during_cancel_pending_is_recorded_exactly_once(self):
        h = started(ttl=10.0, cancel_s=0.05, queue=0.5)
        h.advance(10.003)                                 # cancel requested, effective at 10.053
        self.assertEqual(h.order().state, "cancel_pending")
        h.sell_trade(10.02, BYBIT, volume=1.5)            # 0.5 queue + 1.0 fills; the order is still fillable
        o = h.order()
        self.assertAlmostEqual(o.filled_qty, 1.0, places=9)
        self.assertEqual(o.state, "cancel_pending")
        h.advance(10.2)                                   # hedge lands, cancel applied
        lt = h.lifetime()
        self.assertEqual((lt["cancels_applied"], lt["fills_while_cancel_pending"], lt["expired_after_partial_fill"]), (1.0, 1.0, 1.0))
        self.assertAlmostEqual(lt["qty_filled_while_cancel_pending"], 1.0, places=9)
        self.assertEqual(len([r for r in h.bt.trade_log if not r["taker"]]), 1)
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], 1.0, places=9)
        h.assert_ledger_reconciles(self)

    def test_fill_that_completes_the_order_during_cancel_pending_beats_the_cancel(self):
        h = started(ttl=10.0, cancel_s=0.05, queue=0.5)
        h.advance(10.003)
        h.sell_trade(10.02, BYBIT, volume=2.5)            # fills all 2.0 while the cancel is in flight
        self.assertEqual(h.resting, [])
        h.advance(10.2)
        lt = h.lifetime()
        self.assertEqual((lt["cancels_applied"], lt["cancels_lost_to_fill"], lt["fills_while_cancel_pending"]), (0.0, 1.0, 1.0))
        self.assertAlmostEqual(lt["qty_filled_while_cancel_pending"], 2.0, places=9)
        self.assertEqual(h.bt.maker.orders_cancelled, 0)
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], 2.0, places=9)
        h.assert_ledger_reconciles(self)

    def test_partial_fill_then_cancel_keeps_the_fill_and_reconciles_inventory_fees_and_hedge(self):
        h = started(ttl=10.0, cancel_s=0.05, queue=0.5)
        h.sell_trade(3.0, BYBIT, volume=1.5)              # partial: 1.0 of 2.0 filled -> hedge in flight
        o = h.order()
        h.assert_order_invariants(self, o)
        self.assertAlmostEqual(o.filled_qty, 1.0, places=9)
        self.assertAlmostEqual(o.remaining, 1.0, places=9)
        self.assertEqual(h.bt.pending.pending_count, 1)   # the hedge
        h.advance(10.003)
        h.advance(10.06)                                  # cancelled; the unfilled remainder is dead, the fill stays
        self.assertEqual(h.resting, [])
        lt = h.lifetime()
        self.assertEqual((lt["expired_after_partial_fill"], lt["cancels_applied"]), (1.0, 1.0))
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], 1.0, places=9)
        self.assertAlmostEqual(h.bt.inventory.base[BINANCE], -1.0, places=9)    # hedge sold what was bought
        self.assertAlmostEqual(h.bt.inventory.net_base, 0.0, places=9)
        h.assert_ledger_reconciles(self)
        self.assertEqual(h.bt._trackers, {})              # signal resolved once the hedge landed and the remainder died

    def test_cancel_wins_an_exact_tie_with_a_trade_and_loses_to_one_a_hair_earlier(self):
        for dt, expect_fill in ((-1e-6, 1.0), (0.0, 0.0), (+1e-6, 0.0)):
            with self.subTest(dt=dt):
                h = started(ttl=10.0, cancel_s=0.05, queue=0.5)
                h.sell_trade(10.053 + dt, BYBIT, volume=1.5)
                self.assertAlmostEqual(h.bt.inventory.base[BYBIT], expect_fill, places=9)

    def test_trade_exactly_at_the_request_time_still_fills_because_the_cancel_has_not_landed(self):
        h = started(ttl=10.0, cancel_s=0.05, queue=0.5)
        h.sell_trade(10.003, BYBIT, volume=1.5)
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], 1.0, places=9)

    def test_confirmed_cancel_makes_the_remainder_non_executable(self):
        h = started(ttl=10.0, cancel_s=0.05, queue=0.5)
        h.advance(10.003); h.advance(10.06)
        self.assertEqual(h.resting, [])
        h.sell_trade(10.1, BYBIT, volume=50.0)            # would sweep any resting bid
        self.assertEqual(h.bt.inventory.base[BYBIT], 0.0)
        self.assertEqual(h.bt.trade_log, [])

    def test_a_pending_cancel_is_never_treated_as_done_before_it_lands(self):
        # Analog of a cancel timeout / unresolved state: until the exchange applies the cancel the order stays on
        # the book, stays fillable and stays counted. Nothing in the engine removes it earlier.
        h = started(ttl=10.0, cancel_s=3600.0)
        h.advance(10.003)
        for t in (11.0, 600.0, 3000.0, 3610.0):
            h.advance(t)
            self.assertEqual((len(h.resting), h.order().state, h.live(t)), (1, "cancel_pending", 1), t)
        h.advance(3610.004)
        self.assertEqual(h.resting, [])


class CountsAndLimitsTests(unittest.TestCase):
    def test_slot_is_released_one_feed_delay_after_the_cancel_lands_not_at_the_instant_it_lands(self):
        h = started(ttl=10.0, cancel_s=0.05)
        feed = h.feed_s[BYBIT]
        h.advance(10.053)                                  # cancel just landed
        self.assertEqual((h.resting, h.live(10.053)), ([], 1))
        self.assertEqual(h.live(10.053 + feed - 1e-6), 1)
        self.assertEqual(h.live(10.053 + feed + 1e-6), 0)

    def test_replacement_cannot_exceed_the_live_limit_while_the_old_order_is_unresolved(self):
        h = started(ttl=10.0, cancel_s=0.05, max_live=1)
        limit = h.bt.config.arb.maker_max_live_orders
        for t in (10.003, 10.03, 10.053, 10.0535):
            h.advance(t)
            self.assertGreaterEqual(h.live(t), limit, f"a replacement would be allowed at t={t}")
        self.assertLess(h.live(10.06), limit)               # now it is free
        h.send_maker(10.06)
        self.assertEqual(h.live(10.06), 1)                  # the replacement is in flight and counts
        h.advance(10.07)
        self.assertEqual((len(h.resting), h.live(10.07)), (1, 1))

    def test_a_filled_order_frees_its_slot_at_once_and_an_expired_one_does_not(self):
        h = started(ttl=10.0, queue=0.5)
        h.sell_trade(1.0, BYBIT, volume=2.5)
        self.assertEqual(h.live(1.0), 0)

    def test_two_venues_expire_independently_with_their_own_cancel_latency(self):
        h = Harness(ttl=10.0, max_live=2, cancel={BYBIT: 0.05, BINANCE: 0.5})
        h.book(0.0, BYBIT); h.book(0.0, BINANCE)
        h.send_maker(0.0, venue=BYBIT, hedge_venue=BINANCE)
        h.send_maker(0.0, venue=BINANCE, hedge_venue=BYBIT)
        h.advance(0.003)
        self.assertEqual(sorted(o.venue for o in h.resting), [BINANCE, BYBIT])
        h.advance(10.003)
        h.advance(10.1)                                    # bybit's cancel (50 ms) has landed, binance's (500 ms) has not
        self.assertEqual([o.venue for o in h.resting], [BINANCE])
        self.assertEqual(h.order().state, "cancel_pending")
        h.sell_trade(10.2, BINANCE, volume=1.5)            # binance order still fills; bybit order is gone
        self.assertAlmostEqual(h.bt.inventory.base[BINANCE], 1.0, places=9)
        h.advance(10.6)
        self.assertEqual(h.resting, [])
        self.assertAlmostEqual(h.bt.inventory.base[BINANCE], 1.0, places=9)     # the bybit order never filled
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], -1.0, places=9)      # only the hedge for the binance fill
        self.assertEqual(h.lifetime()["cancels_applied"], 2.0)
        h.assert_ledger_reconciles(self)


class EndOfRunTests(unittest.TestCase):
    def test_close_out_reports_open_and_late_landing_orders_without_inventing_cancels_or_fills(self):
        h = Harness(ttl=10.0, max_live=3, entry={BYBIT: 0.003, BINANCE: 20.0})
        h.book(0.0, BYBIT); h.book(0.0, BINANCE)
        h.send_maker(0.0, venue=BYBIT)                     # lands, rests
        h.send_maker(0.0, venue=BINANCE, hedge_venue=BYBIT)  # still in flight when the data ends
        h.advance(0.003)
        h.advance(10.003)                                  # first order has a cancel pending that has not landed
        inv_before = dict(h.bt.inventory.base)
        h.bt._close_out(10.004)
        lt = h.lifetime()
        self.assertEqual(lt["resting_at_data_end"], 1.0)
        self.assertEqual(lt["cancel_pending_at_data_end"], 1.0)
        self.assertEqual(lt["maker_landed_after_data_end"], 1.0)
        self.assertEqual(lt["cancels_applied"], 0.0)       # no fabricated cancel confirmation
        self.assertEqual(h.resting, [])
        self.assertEqual(h.bt.inventory.base, inv_before)  # and no fills
        self.assertEqual(h.bt.trade_log, [])
        self.assertEqual(h.bt._trackers, {})


class LongReplayTests(unittest.TestCase):
    """>= 12 synthetic days, longer than the original 11-day failure."""

    @staticmethod
    def _cfg(root, out, ttl):
        return ArbBacktestConfig(
            data_root=str(root), venues=(BYBIT, BINANCE), pair="BTCUSDT", start_date="2026-10-01", end_date="2026-10-12",
            latency_preset="colocated_both", bybit_fee_preset="bybit_perp_mm", binance_fee_preset="binance_perp_vip0",
            maker=MakerConfig(min_queue_ahead=0.8),
            arb=ArbConfig(mode=MAKER_TAKER, min_net_edge_bps=1.0, max_clip_notional=2_000.0, max_adverse_prob=0.15,
                          max_ttf_s=8.0, max_cross_venue_skew_s=0.2, fill_gate_horizon_s=5, maker_max_age_s=ttl),
            output_dir=str(out))

    def test_with_a_lifetime_quoting_continues_to_the_last_day_and_without_one_a_stuck_order_appears(self):
        with tempfile.TemporaryDirectory() as d, warnings.catch_warnings():
            warnings.simplefilter("ignore")
            root = Path(d) / "data"
            gen(root, ndays=12, seed=11, session_s=30.0)
            res, ages, last_day = {}, {}, {}
            for ttl in (30.0, 0.0):
                r = CrossVenueArbBacktester(self._cfg(root, Path(d) / f"o{ttl}", ttl)).run()
                labels = [x for x in extract_labels(r.fill_log_path) if x.horizon_s == 5]
                res[ttl], ages[ttl] = r, max(x.observation_time_s for x in labels)
                last_day[ttl] = sum(1 for x in labels if x.timestamp >= _epoch("2026-10-12"))
            self.assertLess(ages[30.0], 40.0)
            self.assertGreater(last_day[30.0], 0, "with a lifetime, orders are still being placed on day 12")
            self.assertGreater(ages[0.0], 8 * DAY, "without one, a single order placed on day 3 is still resting on day 12")
            self.assertEqual(last_day[0.0], 0, "and blocks every later quote")
            lt = res[30.0].maker_lifetime
            self.assertGreater(lt["cancels_applied"], 0)
            self.assertEqual(sum(res[0.0].maker_lifetime[k] for k in ("expiries_triggered", "cancels_applied")), 0.0)
            self.assertEqual(res[0.0].maker_lifetime["resting_at_data_end"], 1.0)

    def test_replay_is_deterministic_for_identical_inputs(self):
        with tempfile.TemporaryDirectory() as d, warnings.catch_warnings():
            warnings.simplefilter("ignore")
            root = Path(d) / "data"
            gen(root, ndays=3, seed=5, session_s=30.0)
            out, logs = [], []
            for i in range(2):
                r = CrossVenueArbBacktester(self._cfg(root, Path(d) / f"o{i}", 30.0)).run()
                logs.append((Path(r.trades_path).read_bytes(), Path(r.fill_log_path).read_bytes()))
                r = dict(r.__dict__)
                r["feed_report"] = {k: v for k, v in r["feed_report"].items() if k != "_quality_preflight"}  # carries a creation time
                for k in ("wall_seconds", "fill_log_path", "bucket_stats_path", "trades_path"):
                    r.pop(k)
                out.append(r)
            self.assertEqual(out[0], out[1])
            self.assertEqual(logs[0], logs[1], "trade and fill logs must be byte-identical")
            self.assertGreater(len(logs[0][0]), 0)


def _epoch(day: str) -> float:
    from datetime import datetime, timezone
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()


if __name__ == "__main__":
    unittest.main()
