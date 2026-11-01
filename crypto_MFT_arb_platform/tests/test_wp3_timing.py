"""WP3 acceptance fixtures: the timing model wired into the backtester (ArbBacktestConfig.timing).

Every timeline is fixed (jitter 0) so each expected time is arithmetic written here. Harness books: bids 99/98, asks 101/102 on
both venues; the maker bid rests at 100 for 2.0 with 0.5 ahead in the queue; a sell print of 2.5 at 100 fills it.
"""
from __future__ import annotations

import unittest

from new_backtest.timing import ClockConfig, TimelineSpec, TimingConfig, VenueTimelines

from test_maker_order_lifetime import BINANCE, BYBIT, Harness

MS = 1e-3


def fixed(entry=6.0, ack=2.0, fill=2.0, cancel=6.0, cancel_rep=2.0, feed=2.0) -> VenueTimelines:
    return VenueTimelines(entry=TimelineSpec(entry), ack_report=TimelineSpec(ack), fill_report=TimelineSpec(fill), cancel=TimelineSpec(cancel),
                         cancel_report=TimelineSpec(cancel_rep), feed=TimelineSpec(feed))


def timing(policy="market_first", bybit=None, binance=None, **kw) -> TimingConfig:
    return TimingConfig(venues={BYBIT: bybit or fixed(), BINANCE: binance or fixed()}, equal_time_policy=policy, **kw)


def harness(tc: TimingConfig | None, ttl=10.0, **arb_kw) -> Harness:
    h = Harness(ttl=ttl, cfg_kw={"timing": tc} if tc is not None else None, **arb_kw)
    h.book(0.5, BYBIT)
    h.book(0.5, BINANCE)
    return h


class TimelineWiringTests(unittest.TestCase):
    def test_entry_uses_the_venue_entry_timeline_and_asymmetric_latency_is_per_venue(self):
        h = harness(timing(bybit=fixed(entry=6.0), binance=fixed(entry=60.0)))
        h.send_maker(1.0, venue=BYBIT)
        h.send_maker(1.0, venue=BINANCE)
        rel = sorted(o.release_ts for o in h.bt.pending.in_flight())
        self.assertAlmostEqual(rel[0], 1.0 + 6 * MS, places=9)
        self.assertAlmostEqual(rel[1], 1.0 + 60 * MS, places=9)
        draws = h.bt.timelines.draws
        self.assertEqual((draws[(BYBIT, "entry")], draws[(BINANCE, "entry")]), (1, 1))
        self.assertEqual(sum(v for (venue, name), v in draws.items() if name != "entry"), 0)   # no report draw charged on entry

    def test_hedge_is_sent_one_fill_report_after_the_venue_matched_the_maker(self):
        h = harness(timing(bybit=fixed(fill=40.0), binance=fixed()))
        h.send_maker(1.0)
        h.sell_trade(5.0, BYBIT, volume=2.5, exchange_ts=5.0)                   # host clock == exchange clock (offset 0)
        hedges = [o for o in h.bt.pending.in_flight() if o.is_taker]
        self.assertEqual(len(hedges), 1)
        self.assertAlmostEqual(hedges[0].sent_ts, 5.0 + 40 * MS, places=9)      # learned 40 ms after the match, then dispatched
        self.assertAlmostEqual(hedges[0].release_ts, 5.0 + 40 * MS + 6 * MS, places=9)
        self.assertEqual(h.bt.timelines.draws[(BYBIT, "fill_report")], 1)

    def test_legacy_profile_is_untouched_without_timing(self):
        h = harness(None)
        self.assertIsNone(h.bt.timelines)
        h.send_maker(1.0)
        self.assertAlmostEqual(list(h.bt.pending.in_flight())[0].release_ts, 1.0 + 0.003, places=9)   # the Harness's pinned entry delay
        h.sell_trade(5.0, BYBIT, volume=2.5)
        self.assertAlmostEqual([o for o in h.bt.pending.in_flight() if o.is_taker][0].sent_ts, 5.0, places=9)
        self.assertEqual(h.bt._timing_report(), {})


class TimerWheelTests(unittest.TestCase):
    def test_lifetime_expires_at_its_own_time_between_sparse_events(self):
        h = harness(timing(), ttl=10.0)
        h.send_maker(1.0)
        h.bt._fire_timers_before(1.1)                                        # the arrival timer fires at 1.006, not at the next event
        order = h.order()
        self.assertAlmostEqual(order.placed_ts, 1.0 + 6 * MS, places=9)
        self.assertIsNone(order.cancel_requested_ts)
        h.bt._fire_timers_before(100.0)                                      # next market event is at t=100
        self.assertAlmostEqual(order.cancel_requested_ts, 1.0 + 6 * MS + 10.0, places=9)   # placed_ts + ttl, not 100
        self.assertAlmostEqual(order.cancel_effective_ts, 11.006 + 6 * MS, places=9)         # + the cancel timeline
        self.assertEqual(h.resting, [])                                                      # and the effective cancel fired too
        self.assertEqual(h.bt.timing_stats["timers_fired_between_events"], 3)                # arrival, request, effective
        legacy = harness(None, ttl=10.0)
        legacy.send_maker(1.0)
        legacy.advance(100.0)
        self.assertAlmostEqual(legacy.order().placed_ts, 100.0, places=9)                  # legacy: the order lands only at the next event
        self.assertIsNone(legacy.order().cancel_requested_ts)                               # so its lifetime has not even started

    def test_next_deadline_sees_in_flight_orders_and_funding(self):
        h = harness(timing())
        self.assertIsNone(h.bt._next_deadline())
        h.send_maker(1.0)
        h.bt._now = 1.0
        self.assertAlmostEqual(h.bt._next_deadline(), 1.0 + 6 * MS, places=9)


class EqualTimePolicyTests(unittest.TestCase):
    def _cancel_then_print_at_the_same_time(self, policy):
        h = harness(timing(policy=policy), ttl=10.0)
        h.send_maker(1.0)
        h.advance(1.1)
        order = h.order()
        order.cancel_requested_ts = 4.0
        order.cancel_effective_ts = 5.0
        h.sell_trade(5.0, BYBIT, volume=2.5)                                   # the print is stamped exactly at the effective time
        return h

    def test_market_first_fills_the_order_the_print_reaches_at_the_cancel_instant(self):
        h = self._cancel_then_print_at_the_same_time("market_first")
        self.assertEqual(h.bt.inventory.base[BYBIT], 2.0)                     # filled by the print; the cancel finds nothing
        self.assertEqual(h.resting, [])
        self.assertEqual(len([o for o in h.bt.pending.in_flight() if o.is_taker]), 1)   # and the hedge went out

    def test_cancel_first_removes_the_order_before_the_print(self):
        h = self._cancel_then_print_at_the_same_time("cancel_first")
        self.assertEqual(h.bt.inventory.base[BYBIT], 0.0)                     # legacy tie rule: cancelled before the print
        self.assertEqual(h.resting, [])
        self.assertEqual(list(h.bt.pending.in_flight()), [])
        self.assertEqual(h.bt.lifetime["cancels_applied"], 1)


class DeadFeedTests(unittest.TestCase):
    def _h(self):
        return harness(timing(dead_feed_s=5.0, max_book_age_s=0.5))

    def test_valid_quiet_and_dead_are_absolute_ages_and_block_accordingly(self):
        h = self._h()
        bt = h.bt
        self.assertFalse(bt._dead_feed_blocks(0.9))                            # both 0.4 s old: valid
        self.assertTrue(bt._dead_feed_blocks(1.2))                             # both 0.7 s old: stale on both -> entry blocked
        self.assertEqual(bt.timing_stats["entry_blocked_both_stale"], 1)
        h.book(1.3, BINANCE)                                                   # binance fresh again, bybit 0.8 s old: quiet, not dead
        self.assertFalse(bt._dead_feed_blocks(1.3))
        self.assertEqual(bt.timing_stats["signals_blocked_by_dead_feed"], 0)
        h.book(6.0, BINANCE)                                                   # bybit now 5.5 s old: dead
        self.assertTrue(bt._dead_feed_blocks(6.0))
        self.assertEqual(bt.timing_stats["signals_blocked_by_dead_feed"], 1)
        self.assertEqual(bt._feed_state, {BYBIT: "dead", BINANCE: "valid"})
        rep = bt._timing_report()
        self.assertEqual(rep["feed_state_final"][BYBIT], "dead")
        self.assertEqual(rep["stats"]["feed_dead_transitions_bybit"], 1)


class ClockTests(unittest.TestCase):
    def test_measured_host_offset_replaces_the_median_aligner(self):
        h = harness(timing(clock=ClockConfig(host_offset_ns=-78_000_000, uncertainty_ns=5_000_000, source="MEASURED:test")))
        bt = h.bt
        self.assertTrue(bt.latency.aligner.ready)
        self.assertAlmostEqual(bt.latency.aligner.to_host(1000.0), 999.922, places=9)
        for vl in bt.latency.venues.values():
            self.assertIs(vl.aligner, bt.latency.aligner)
        self.assertEqual(bt._timing_report()["clock"]["source"], "MEASURED:test")
        h.send_maker(1.0)
        h.sell_trade(5.0, BYBIT, volume=2.5, exchange_ts=5.0)                  # exchange 5.0 = host 4.922; received at host 5.0 (78 ms feed delay)
        self.assertAlmostEqual(bt._current_ets, 5.0 - 0.078, places=9)
        hedge = [o for o in bt.pending.in_flight() if o.is_taker][0]
        # The fill report (4.922 + 2 ms) would precede the public print; the strategy still cannot act before the event that
        # revealed the fill to the simulator, so dispatch is max(receive 5.0, report 4.924) = 5.0.
        self.assertAlmostEqual(hedge.sent_ts, 5.0, places=9)
        h2 = harness(timing(clock=ClockConfig(host_offset_ns=-78_000_000), bybit=fixed(fill=100.0), binance=fixed()))
        h2.send_maker(1.0)
        h2.sell_trade(5.0, BYBIT, volume=2.5, exchange_ts=5.0)
        hedge2 = [o for o in h2.bt.pending.in_flight() if o.is_taker][0]
        self.assertAlmostEqual(hedge2.sent_ts, 5.0 - 0.078 + 0.100, places=9)   # a slow report: learned 22 ms after the print


class FutureDataTests(unittest.TestCase):
    def _run(self, late_books):
        h = harness(timing())
        h.send_maker(1.0)
        h.sell_trade(5.0, BYBIT, volume=2.5, exchange_ts=5.0)
        h.advance(5.1)
        snapshot = ([dict(r) for r in h.bt.trade_log], [dict(r) for r in h.bt.signal_log], h.bt.inventory.total_cash)
        for t, venue, bids, asks in late_books:
            h.book(t, venue, bids=bids, asks=asks)
        return snapshot, h

    def test_perturbing_later_data_leaves_earlier_decisions_unchanged(self):
        a, _ = self._run([(20.0, BYBIT, ((109.0, 5.0),), ((111.0, 5.0),))])
        b, _ = self._run([(20.0, BYBIT, ((89.0, 5.0),), ((91.0, 5.0),)), (21.0, BINANCE, ((50.0, 5.0),), ((51.0, 5.0),))])
        self.assertEqual(a, b)
        self.assertEqual(len(a[0]), 2)                                         # maker fill + hedge fill, both before t=5.1


class VenueTruthTests(unittest.TestCase):
    """Delayed pre-arrival event: binance's bid drops to 98 at exchange time 1.004, but the collector delivers that book only at
    1.050. A taker sell sent at 1.0 arrives at exchange time 1.006: the venue matches it at 98 (truth), while the strategy's own
    view still shows 99 until 1.050. legacy_v0 (no timing) fills at the view's 99."""

    def _taker_sell(self, h: Harness, t: float):
        from new_backtest.arb_strategy import TAKER_TAKER, ArbLeg, ArbSignal
        leg = ArbLeg(venue=BINANCE, side="sell", qty=1.0, limit_price=0.0, is_taker=True, expected_price=99.0, fee_bps=5.0)
        sig = ArbSignal(signal_id=99, ts=t, buy_venue=BYBIT, sell_venue=BINANCE, qty=1.0, gross_edge_bps=5.0, fee_bps=1.0, net_edge_bps=4.0,
                        legs=[leg], mode=TAKER_TAKER, buy_price=98.0, sell_price=99.0)
        h.bt._send_leg(leg, sig, t, is_hedge=True)

    def _late_book(self):
        from types import SimpleNamespace
        from new_backtest.event_loader import BOOK
        return SimpleNamespace(kind=BOOK, venue=BINANCE, pair="BTCUSDT", receive_ts=1.050, exchange_ts=1.004,
                               payload={"bids": [[98.0, 5.0], [97.0, 5.0]], "asks": [[101.0, 5.0], [102.0, 5.0]]})

    def test_taker_arrival_executes_against_the_exchange_time_book(self):
        h = harness(timing())
        bt = h.bt
        bt._truth_ingest(self._late_book())
        self._taker_sell(h, 1.0)
        h.advance(1.1)                                                         # arrival at 1.006 (exchange time 1.006, offset 0)
        self.assertEqual(bt.books[BINANCE].best_bid, 99.0)                     # the strategy view has not seen the drop
        self.assertEqual(bt.truth_books[BINANCE].best_bid, 98.0)
        self.assertEqual([r["price"] for r in bt.trade_log], [98.0])            # filled where the venue actually was
        st = bt._timing_report()["stats"]
        self.assertEqual((st["truth_arrivals"], st["truth_events_applied_before_receipt"]), (1, 1))
        self.assertAlmostEqual(st["truth_vs_view_mid_bps_max"], (100.0 - 99.5) / 100.0 * 1e4, places=9)
        h.book(1.050, BINANCE, bids=((98.0, 5.0), (97.0, 5.0)), asks=((101.0, 5.0), (102.0, 5.0)))
        self.assertEqual(bt.books[BINANCE].best_bid, 98.0)                     # the view catches up when the event is received
        self.assertEqual(list(bt._ahead[BINANCE]), [])

    def test_legacy_fills_at_the_delayed_view(self):
        h = harness(None)
        self._taker_sell(h, 1.0)
        h.advance(1.1)
        self.assertEqual([r["price"] for r in h.bt.trade_log], [99.0])

    def test_truth_book_tracks_the_view_when_driven_by_hand(self):
        h = harness(timing())
        h.book(2.0, BYBIT, bids=((97.0, 5.0),), asks=((103.0, 5.0),))
        self.assertEqual(h.bt.truth_books[BYBIT].best_bid, 97.0)
        self.assertEqual(h.bt.truth_books[BINANCE].best_bid, 99.0)


if __name__ == "__main__":
    unittest.main()
