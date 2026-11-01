"""Hedge-leg failure injection (src/new_backtest/hedge_faults.py + CrossVenueArbBacktester).

Scenario: a maker bid on bybit fills, the taker hedge (a sell on binance) is rejected, arrives late, or is sent while the
order gateway is down. Each test drives the backtester by hand through the same methods run() uses, with pinned latencies,
so every price, time and quantity below can be checked against the book by eye.

Book used by the Harness: bybit bids 99/98, asks 101/102 (size 5); binance the same. The maker bid rests at 100.0 for 2.0
(queue ahead 0.5); `sell_trade(t, BYBIT, 2.5)` drains the queue and fills all 2.0 at t. The hedge then sells 2.0 on binance
at its bid 99.0 (limit 99.0 less 5 bps).
"""
from __future__ import annotations

import tempfile
import unittest
import warnings
from pathlib import Path

from new_backtest.arb_strategy import MAKER_TAKER, ArbConfig
from new_backtest.backtester import ArbBacktestConfig, CrossVenueArbBacktester
from new_backtest.fill_engine import MakerConfig
from new_backtest.hedge_faults import ExposureTracker, HedgeFaultConfig, Outage, periodic_outages

from synthetic_days import gen
from test_maker_order_lifetime import BINANCE, BYBIT, Harness, started


def fill(h: Harness, t: float = 5.0) -> None:
    """The maker bid fills completely at t; the hedge is sent at t and lands at t + 3 ms (unless a fault says otherwise)."""
    h.sell_trade(t, BYBIT, volume=2.5)
    mark(h, t)


def mark(h: Harness, t: float) -> None:
    """What run() does after every event."""
    bt = h.bt
    if bt.exposure is None:
        return
    bt.exposure.update(t, bt.inventory.base, bt.inventory.total_cash, {v: b.mid for v, b in bt.books.items()}, bt._ref_mid())


def walk(h: Harness, times) -> None:
    for t in times:
        h.advance(t)
        mark(h, t)


def rows(h, taker=None):
    return [r for r in h.bt.trade_log if taker is None or r["taker"] == taker]


def fh(**kw) -> HedgeFaultConfig:
    return HedgeFaultConfig(**kw)


class ConfigTests(unittest.TestCase):
    def test_defaults_inject_nothing(self):
        c = HedgeFaultConfig()
        self.assertFalse(c.injects)
        self.assertEqual(c.response, "legacy")

    def test_validation(self):
        for kw in (dict(reject_prob=1.5), dict(delay_prob=-0.1), dict(partial_prob=2.0), dict(partial_fraction=1.0), dict(partial_fraction=0.0),
                   dict(extra_delay_s=-1.0), dict(ack_timeout_s=-1.0), dict(outage_policy="nope"), dict(response="nope"),
                   dict(outage_on_fill_kind="nope"), dict(max_retries=-1)):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                HedgeFaultConfig(**kw)
        with self.assertRaises(ValueError):
            Outage(BINANCE, 5.0, 5.0)
        with self.assertRaises(ValueError):
            Outage(BINANCE, 1.0, 2.0, kind="nope")

    def test_an_outage_on_a_venue_that_is_not_in_the_run_is_rejected(self):
        with self.assertRaises(ValueError):
            Harness(faults=fh(outages=(Outage("kraken", 0.0, 1.0),)))

    def test_periodic_outages(self):
        o = periodic_outages(BINANCE, 100.0, 400.0, period_s=100.0, duration_s=10.0)
        self.assertEqual([(x.start_ts, x.end_ts) for x in o], [(100.0, 110.0), (200.0, 210.0), (300.0, 310.0)])
        with self.assertRaises(ValueError):
            periodic_outages(BINANCE, 0.0, 10.0, period_s=5.0, duration_s=5.0)


class RejectedHedgeTests(unittest.TestCase):
    def test_baseline_without_faults_hedges_and_is_flat(self):
        h = started()
        fill(h)
        h.advance(5.003)
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], 2.0)
        self.assertAlmostEqual(h.bt.inventory.base[BINANCE], -2.0)
        self.assertEqual(h.bt.hedge["hedges_sent"], 1)
        self.assertEqual(h.bt.hedge["hedges_filled"], 1)

    def test_a_rejected_hedge_is_carried_when_the_response_is_none(self):
        h = Harness(ttl=10.0, faults=fh(reject_prob=1.0, response="none"))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        fill(h)
        walk(h, [5.003])                                       # hedge reaches binance and is rejected: nothing traded there
        self.assertEqual(rows(h, taker=True), [])
        detect = 5.003 + h.feed_s[BINANCE]
        walk(h, [detect - 0.0005])
        self.assertIsNone(h.bt.hedge_events[0]["outcome"])     # the strategy does not know yet
        walk(h, [detect, detect + 1.0])
        ev = h.bt.hedge_events[0]
        self.assertEqual((ev["outcome"], ev["first_reason"], ev["reasons"]), ("carried", "fault_rejected", ["fault_rejected"]))
        self.assertAlmostEqual(h.bt.inventory.net_base, 2.0)
        self.assertAlmostEqual(h.bt.inventory.base[BINANCE], 0.0)
        self.assertEqual((h.bt.signals_unhedged, h.bt.hedge["residual_qty"], h.bt.hedge["carried_qty"]), (1, 2.0, 2.0))
        self.assertEqual(h.bt.miss_reasons["fault_rejected"], 1)
        self.assertEqual(h.bt.hedge["instant_unwinds"], 0)
        self.assertAlmostEqual(h.bt.exposure.peak_abs_net, 2.0)
        self.assertGreater(h.bt.exposure.longest_net_nonzero_s, 0.9)
        h.assert_ledger_reconciles(self)

    def test_unwind_response_sells_back_on_the_maker_venue_after_detection_and_entry_latency_at_the_book_of_that_moment(self):
        h = Harness(ttl=10.0, faults=fh(reject_prob=1.0, response="unwind"))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        fill(h)
        detect = 5.003 + h.feed_s[BINANCE]
        walk(h, [5.003, detect])
        self.assertEqual(h.bt.hedge["unwinds_sent"], 1)
        self.assertEqual(rows(h, taker=True), [])               # sent, still in flight (3 ms)
        h.book(detect + 0.001, BYBIT, bids=((98.0, 5.0), (97.0, 5.0)), asks=((101.0, 5.0), (102.0, 5.0)))   # the market moves first
        walk(h, [detect + 0.003, detect + 0.01])
        (u,) = rows(h, taker=True)
        self.assertEqual((u["venue"], u["side"], u["qty"]), (BYBIT, "sell", 2.0))
        self.assertAlmostEqual(u["price"], 98.0)                # the bid of the moment, not the 99.0 of the miss and not the 100.0 mid
        self.assertAlmostEqual(h.bt.inventory.net_base, 0.0)
        self.assertEqual(h.bt.hedge_events[0]["outcome"], "unwound")
        self.assertAlmostEqual(h.bt.hedge_events[0]["unwound_qty"], 2.0)
        self.assertEqual(h.bt.hedge["residual_qty"], 0.0)
        # 100.0 bought, 98.0 sold: the loss is in the cash, not only in the half-spread `unhedged_loss`
        self.assertAlmostEqual(h.bt.inventory.total_cash + 0.0, -200.0 + 196.0 - h.bt.fees_paid, places=9)
        h.assert_ledger_reconciles(self)

    def test_the_legacy_response_flattens_instantly_at_the_touch_with_no_latency(self):
        h = Harness(ttl=10.0, faults=fh(reject_prob=1.0, response="legacy"))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        fill(h)
        walk(h, [5.003])                                        # reaches binance, rejected, known at once, flattened at this event
        (u,) = rows(h, taker=True)
        self.assertEqual((u["venue"], u["side"], u["ts"]), (BYBIT, "sell", 5.003))
        self.assertEqual((h.bt.hedge["instant_unwinds"], h.bt.hedge["instant_unwind_qty"]), (1, 2.0))
        self.assertAlmostEqual(h.bt.inventory.net_base, 0.0)

    def test_retries_then_unwind(self):
        h = Harness(ttl=10.0, faults=fh(reject_prob=1.0, response="retry_unwind", max_retries=2, retry_backoff_s=0.25))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        fill(h)
        t, step = 5.003, 0.01
        while t < 8.0:
            walk(h, [t]); t += step
        c = h.bt.hedge
        self.assertEqual((c["retries_sent"], c["failed_fault_rejected"], c["unwinds_sent"]), (2, 3, 1))
        self.assertEqual(h.bt.hedge_events[0]["outcome"], "unwound")
        self.assertEqual(h.bt.hedge_events[0]["reasons"], ["fault_rejected"] * 3)
        self.assertAlmostEqual(h.bt.inventory.net_base, 0.0)
        self.assertEqual(h.bt._trackers, {})

    def test_a_retry_that_gets_through_hedges_the_position(self):
        out = Outage(BINANCE, 4.0, 5.2)                         # gateway down when the hedge is sent, back before the retry
        h = Harness(ttl=10.0, faults=fh(outages=(out,), outage_policy="reject", response="retry", retry_backoff_s=0.25))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        fill(h)
        t = 5.0
        while t < 6.0:
            walk(h, [t]); t += 0.01
        ev = h.bt.hedge_events[0]
        self.assertEqual((ev["outcome"], ev["reasons"]), ("hedged_on_retry", ["fault_outage_reject"]))
        self.assertAlmostEqual(h.bt.inventory.base[BINANCE], -2.0)
        self.assertAlmostEqual(h.bt.inventory.net_base, 0.0)
        self.assertEqual(h.bt.hedge["retries_sent"], 1)
        self.assertEqual(h.bt.signals_fully_hedged, 1)
        self.assertEqual(h.bt._trackers, {})
        # the position was naked from the fill until the retry landed (5.25 + 3 ms)
        self.assertGreater(h.bt.exposure.longest_net_nonzero_s, 0.25)
        self.assertLess(h.bt.exposure.longest_net_nonzero_s, 0.45)


class DelayedHedgeTests(unittest.TestCase):
    def test_a_delayed_hedge_leaves_the_position_open_for_the_delay_and_prices_off_the_later_book(self):
        h = Harness(ttl=10.0, faults=fh(delay_prob=1.0, extra_delay_s=1.0, response="unwind"))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        mark(h, 0.003)
        fill(h)
        walk(h, [5.003, 5.5])
        self.assertEqual(rows(h, taker=True), [])
        self.assertAlmostEqual(h.bt.inventory.net_base, 2.0)
        walk(h, [6.003])                                        # lands 1.0 s late
        (r,) = rows(h, taker=True)
        self.assertEqual((r["venue"], r["side"], r["qty"]), (BINANCE, "sell", 2.0))
        self.assertAlmostEqual(h.bt.inventory.net_base, 0.0)
        self.assertEqual(h.bt.hedge["hedges_delayed"], 1)
        self.assertAlmostEqual(h.bt.exposure.longest_net_nonzero_s, 1.003, places=3)
        self.assertAlmostEqual(h.bt.hedge["hedge_flight_s_sum"], 1.003, places=3)

    def test_when_the_book_moves_through_the_hedge_limit_during_the_delay_the_hedge_misses_and_the_response_takes_over(self):
        h = Harness(ttl=10.0, faults=fh(delay_prob=1.0, extra_delay_s=1.0, response="unwind"))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        fill(h)
        walk(h, [5.003])
        h.book(5.5, BINANCE, bids=((98.0, 5.0), (97.0, 5.0)), asks=((101.0, 5.0), (102.0, 5.0)))   # bid now below the 98.95 limit
        walk(h, [6.003])
        self.assertEqual(rows(h, taker=True), [])               # the hedge found nothing it could sell into
        self.assertEqual(h.bt.hedge["failed_price_moved"], 1)
        t = 6.003
        while t < 6.2:
            t += 0.002; walk(h, [t])
        self.assertEqual(h.bt.hedge_events[0]["outcome"], "unwound")
        self.assertAlmostEqual(h.bt.inventory.net_base, 0.0)


class OutageTests(unittest.TestCase):
    def _fill_in_outage(self, policy, response="unwind", end=20.0, **kw):
        h = Harness(ttl=10.0, faults=fh(outages=(Outage(BINANCE, 4.0, end),), outage_policy=policy, response=response,
                                        ack_timeout_s=2.0, **kw))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        mark(h, 0.003)
        fill(h)
        return h

    def test_blackhole_the_order_is_lost_and_the_strategy_finds_out_after_the_ack_timeout(self):
        h = self._fill_in_outage("blackhole")
        walk(h, [5.003, 6.9])
        self.assertAlmostEqual(h.bt.inventory.net_base, 2.0)
        self.assertEqual((h.bt.hedge["sends_into_outage"], h.bt.hedge["blackholed_orders"]), (1, 1))
        self.assertEqual(h.bt.hedge["unwinds_sent"], 0)         # still waiting for the acknowledgement
        walk(h, [7.0, 7.003, 7.01])
        self.assertEqual(h.bt.hedge["unwinds_sent"], 1)
        self.assertAlmostEqual(h.bt.inventory.net_base, 0.0)
        self.assertAlmostEqual(h.bt.inventory.base[BINANCE], 0.0)    # the hedge never traded
        self.assertAlmostEqual(h.bt.exposure.longest_net_nonzero_s, 2.003, places=3)

    def test_ghost_the_written_off_hedge_lands_at_reconnect_and_over_hedges(self):
        h = self._fill_in_outage("ghost", end=10.0)
        walk(h, [5.003, 7.0, 7.003, 7.01])
        self.assertAlmostEqual(h.bt.inventory.net_base, 0.0)    # unwound at 7.0
        walk(h, [10.003, 10.01])
        self.assertAlmostEqual(h.bt.inventory.base[BINANCE], -2.0)
        self.assertAlmostEqual(h.bt.inventory.net_base, -2.0)   # short 2.0 that nobody asked for
        self.assertEqual((h.bt.hedge["ghost_fills"], h.bt.hedge["ghost_fill_qty"]), (1, 2.0))
        self.assertAlmostEqual(h.bt.hedge_events[0]["ghost_qty"], 2.0)
        self.assertAlmostEqual(h.bt.exposure.peak_abs_net, 2.0)
        h.assert_ledger_reconciles(self)

    def test_queue_the_order_is_delivered_at_reconnect_and_the_strategy_just_waits(self):
        h = self._fill_in_outage("queue", end=10.0)
        walk(h, [5.003, 9.0])
        self.assertAlmostEqual(h.bt.inventory.net_base, 2.0)
        self.assertEqual(h.bt.hedge["queued_through_outage"], 1)
        walk(h, [10.003, 10.01])
        self.assertAlmostEqual(h.bt.inventory.net_base, 0.0)
        self.assertAlmostEqual(h.bt.exposure.longest_net_nonzero_s, 5.003, places=3)
        self.assertEqual(h.bt.hedge_events, [])                  # nothing failed: it was only late

    def test_reject_the_send_fails_locally_and_is_known_at_once(self):
        h = self._fill_in_outage("reject", response="none")
        walk(h, [5.0001, 5.01])
        self.assertEqual(h.bt.hedge_events[0]["outcome"], "carried")
        self.assertEqual(h.bt.hedge_events[0]["reasons"], ["fault_outage_reject"])
        self.assertAlmostEqual(h.bt.inventory.net_base, 2.0)

    def test_an_order_sent_before_the_outage_is_not_affected(self):
        h = Harness(ttl=10.0, faults=fh(outages=(Outage(BINANCE, 5.0005, 20.0),), outage_policy="blackhole", response="unwind"))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        fill(h)                                                  # hedge sent at 5.0, outage starts 0.5 ms later
        walk(h, [5.003])
        self.assertAlmostEqual(h.bt.inventory.base[BINANCE], -2.0)
        self.assertEqual(h.bt.hedge["sends_into_outage"], 0)

    def test_the_outage_window_is_half_open(self):
        o = Outage(BINANCE, 4.0, 5.0)
        self.assertTrue(o.covers(4.0)); self.assertTrue(o.covers(4.999)); self.assertFalse(o.covers(5.0)); self.assertFalse(o.covers(3.999))

    def test_an_outage_on_the_maker_venue_holds_back_the_fill_report_and_so_the_hedge(self):
        h = Harness(ttl=10.0, faults=fh(outages=(Outage(BYBIT, 4.0, 8.0),), outage_policy="blackhole", response="unwind"))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        fill(h)                                                  # the fill happens at the exchange at 5.0
        walk(h, [5.003, 7.9])
        self.assertAlmostEqual(h.bt.inventory.base[BINANCE], 0.0)    # the strategy has not heard of the fill
        self.assertEqual(h.bt.hedge["fill_reports_deferred"], 1)
        walk(h, [8.003, 8.01])
        self.assertAlmostEqual(h.bt.inventory.base[BINANCE], -2.0)
        self.assertAlmostEqual(h.bt.exposure.longest_net_nonzero_s, 3.003, places=3)

    def test_full_outage_means_the_strategy_is_blind_gateway_outage_does_not(self):
        h = Harness(faults=fh(outages=(Outage(BINANCE, 4.0, 8.0, "full"), Outage(BYBIT, 20.0, 30.0, "gateway"))))
        self.assertFalse(h.bt._feed_dark(3.0))
        self.assertTrue(h.bt._feed_dark(5.0))
        self.assertFalse(h.bt._feed_dark(8.0))
        self.assertFalse(h.bt._feed_dark(25.0))

    def test_a_maker_fill_can_start_an_outage_on_the_hedge_venue(self):
        h = Harness(ttl=10.0, faults=fh(outage_on_fill_s=30.0, outage_policy="blackhole", response="none"))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        fill(h)
        walk(h, [5.003])
        self.assertEqual(h.bt.hedge["outages_triggered_by_fills"], 1)
        self.assertEqual(h.bt.hedge["blackholed_orders"], 1)
        self.assertTrue(h.bt._active_outage(BINANCE, 20.0) is not None)
        self.assertTrue(h.bt._active_outage(BINANCE, 36.0) is None)


class OtherFaultTests(unittest.TestCase):
    def test_partial_hedge_fill_unwinds_the_remainder(self):
        h = Harness(ttl=10.0, faults=fh(partial_prob=1.0, partial_fraction=0.5, response="unwind"))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        fill(h)
        t = 5.003
        while t < 5.2:
            walk(h, [t]); t += 0.002
        self.assertAlmostEqual(h.bt.inventory.base[BINANCE], -1.0)
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], 1.0)            # 2.0 bought, 1.0 sold back
        self.assertAlmostEqual(h.bt.inventory.net_base, 0.0)
        self.assertEqual(h.bt.hedge_events[0]["outcome"], "unwound")
        self.assertAlmostEqual(h.bt.hedge_events[0]["unwound_qty"], 1.0)

    def test_no_hedge_could_be_built_legacy_leaves_the_signal_hanging_and_the_new_response_handles_it(self):
        legacy = started()
        legacy.bt.strategy.hedge_leg = lambda *a, **k: None
        fill(legacy)
        legacy.advance(6.0)
        self.assertEqual(legacy.bt.hedge["hedge_leg_unavailable"], 1)
        self.assertAlmostEqual(legacy.bt.inventory.net_base, 2.0)           # naked
        self.assertEqual(len(legacy.bt._trackers), 1)                       # and never resolved until the end of data
        h = Harness(ttl=10.0, faults=fh(response="unwind"))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        h.bt.strategy.hedge_leg = lambda *a, **k: None
        fill(h)
        t = 5.0
        while t < 5.1:
            walk(h, [t]); t += 0.002
        self.assertAlmostEqual(h.bt.inventory.net_base, 0.0)
        self.assertEqual(h.bt.hedge_events[0]["first_reason"], "hedge_unavailable")


class RandomDrawTests(unittest.TestCase):
    """The injector's draws come from its own stream and the three faults are drawn independently."""

    @staticmethod
    def _decisions(**kw):
        from new_backtest.latency_model import InFlightOrder
        h = Harness(faults=fh(seed=5, response="unwind", **kw))
        out = []
        for i in range(2000):
            o = InFlightOrder(release_ts=1.0, order_id=i + 1, venue=BINANCE, side="sell", qty=1.0, limit_price=99.0, is_taker=True,
                              sent_ts=0.0, signal_id=1, is_hedge=True)
            h.bt._inject_send_faults(o, 0.0)
            out.append((o.release_ts > 1.0, o.fault == "reject", o.fault == "partial"))
        return out

    def test_changing_one_probability_does_not_move_the_others_decisions(self):
        a = self._decisions(delay_prob=0.5, extra_delay_s=1.0, reject_prob=0.0, partial_prob=0.0)
        b = self._decisions(delay_prob=0.5, extra_delay_s=1.0, reject_prob=0.5, partial_prob=0.3)
        self.assertEqual([x[0] for x in a], [x[0] for x in b])

    def test_delay_and_reject_are_independent(self):
        d = self._decisions(delay_prob=0.5, extra_delay_s=1.0, reject_prob=0.5)
        both = sum(1 for x in d if x[0] and x[1]) / len(d)
        self.assertGreater(both, 0.20)
        self.assertLess(both, 0.30)
        self.assertAlmostEqual(sum(1 for x in d if x[1]) / len(d), 0.5, delta=0.05)

    def test_the_injector_does_not_touch_the_latency_samplers(self):
        h = Harness(faults=fh(reject_prob=0.5, delay_prob=0.5, extra_delay_s=0.1, response="unwind"))
        calls = []
        h.bt.latency.entry_delay = lambda v: calls.append(v) or 0.003
        self._decisions(reject_prob=0.5)
        self.assertEqual(calls, [])


class AccountingAndSafetyTests(unittest.TestCase):
    def test_timers_and_written_off_orders_are_not_pending_exposure(self):
        h = Harness(ttl=10.0, faults=fh(outages=(Outage(BINANCE, 4.0, 20.0),), outage_policy="ghost", response="none"))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        fill(h)
        self.assertGreater(h.bt.pending.pending_count, 0)                   # a ghost and a timer are queued
        e = h.bt._pending_exposure()
        self.assertEqual((e.buys, e.sells), ({}, {}))

    def test_data_ending_with_a_recovery_pending_does_not_flatten_the_position_for_free(self):
        h = Harness(ttl=10.0, faults=fh(reject_prob=1.0, response="unwind"))
        h.book(0.0, BYBIT); h.book(0.0, BINANCE); h.send_maker(0.0); h.advance(0.003)
        fill(h)
        walk(h, [5.003])
        h.bt._close_out(5.01)                                               # before the strategy even hears of the rejection
        self.assertAlmostEqual(h.bt.inventory.net_base, 2.0)
        self.assertEqual(h.bt.hedge["recovery_timers_pending_at_data_end"], 1)
        self.assertEqual(h.bt.hedge["instant_unwinds"], 0)
        rep = h.bt._hedge_report(5.01)
        self.assertEqual(rep["unresolved_chains_at_data_end"], 1)

    def test_the_instant_unwind_of_the_original_code_is_counted_and_has_no_latency(self):
        h = started()                                                       # injection off: the original code path
        fill(h)
        h.book(5.001, BINANCE, bids=((90.0, 5.0), (89.0, 5.0)), asks=((101.0, 5.0), (102.0, 5.0)))   # bid falls through the hedge limit
        h.advance(5.003)                                                    # hedge lands, finds nothing, signal is flattened at once
        (u,) = rows(h, taker=True)
        self.assertEqual((u["venue"], u["side"], u["qty"], u["ts"]), (BYBIT, "sell", 2.0, 5.003))
        self.assertEqual((h.bt.hedge["instant_unwinds"], h.bt.hedge["instant_unwind_qty"]), (1, 2.0))
        self.assertAlmostEqual(h.bt.inventory.net_base, 0.0)

    def test_exposure_tracker_arithmetic(self):
        e = ExposureTracker()
        mids = {BYBIT: 100.0, BINANCE: 100.0}
        e.update(0.0, {BYBIT: 0.0, BINANCE: 0.0}, 0.0, mids)
        e.update(1.0, {BYBIT: 2.0, BINANCE: 0.0}, -200.0, mids)             # long 2 from t=1
        e.update(4.0, {BYBIT: 2.0, BINANCE: 0.0}, -200.0, {BYBIT: 99.0, BINANCE: 99.0})   # mark falls 1: equity -2
        e.update(5.0, {BYBIT: 2.0, BINANCE: -2.0}, -2.0, {BYBIT: 99.0, BINANCE: 99.0})    # hedged at 99 (cash -200 + 198)
        e.update(9.0, {BYBIT: 2.0, BINANCE: -2.0}, -2.0, {BYBIT: 99.0, BINANCE: 99.0})
        r = e.report()
        self.assertEqual(r["peak_abs_net_base"], 2.0)
        self.assertAlmostEqual(r["longest_net_nonzero_s"], 4.0)             # exposed from t=1 until the update at t=5 that finds it flat
        self.assertAlmostEqual(r["time_net_nonzero_s"], 4.0)                # state held from t=1 to t=5
        self.assertAlmostEqual(r["mean_abs_net_base"], 2.0 * 4.0 / 9.0)
        self.assertAlmostEqual(r["max_equity_drawdown"], 2.0)
        self.assertAlmostEqual(r["min_equity"], -2.0)
        self.assertEqual(r["peak_net_long_base"], 2.0)
        self.assertEqual(r["peak_net_short_base"], 0.0)


class ReplayTests(unittest.TestCase):
    """12 synthetic days with the real strategy and hedging on."""

    @staticmethod
    def _cfg(root, out=None, **kw):
        return ArbBacktestConfig(
            data_root=str(root), venues=(BYBIT, BINANCE), pair="BTCUSDT", start_date="2026-10-01", end_date="2026-10-12",
            latency_preset="colocated_both", bybit_fee_preset="bybit_perp_mm", binance_fee_preset="binance_perp_vip0",
            maker=MakerConfig(min_queue_ahead=0.8),
            arb=ArbConfig(mode=MAKER_TAKER, min_net_edge_bps=1.0, max_clip_notional=2_000.0, max_adverse_prob=0.15, max_ttf_s=8.0,
                          max_cross_venue_skew_s=0.2, fill_gate_horizon_s=5, maker_max_age_s=30.0),
            output_dir=str(out) if out else None, **kw)

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / "data"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            gen(cls.root, ndays=12, seed=11, session_s=30.0)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def run_cfg(self, **kw):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            bt = CrossVenueArbBacktester(self._cfg(self.root, **kw))
            r = bt.run()
        return bt, r

    @staticmethod
    def key(bt, r):
        return (r.orders_sent, r.orders_filled, r.orders_missed, r.signals_generated, r.gross_pnl, r.fees_paid, r.unhedged_loss,
                r.ending_equity, tuple(sorted(r.final_inventory.items())), tuple((x["ts"], x["venue"], x["side"], x["qty"], x["price"]) for x in bt.trade_log))

    def test_injection_off_and_an_injector_with_nothing_to_inject_both_reproduce_the_plain_run_exactly(self):
        a = self.run_cfg()
        b = self.run_cfg(record_exposure=True)
        c = self.run_cfg(hedge_faults=fh(response="legacy"))
        d = self.run_cfg(hedge_faults=fh(response="unwind", entry_guard=True))
        self.assertGreater(a[1].orders_filled, 0)
        self.assertEqual(self.key(*a), self.key(*b))
        self.assertEqual(self.key(*a), self.key(*c))
        self.assertEqual(self.key(*a), self.key(*d))
        self.assertEqual(a[1].hedge_report, {})
        self.assertGreater(b[1].hedge_report["exposure"]["peak_abs_net_base"], 0.0)

    def test_the_same_seed_gives_the_same_run_and_another_seed_a_different_one(self):
        kw = dict(reject_prob=0.5, response="unwind")
        a = self.run_cfg(hedge_faults=fh(seed=3, **kw))
        b = self.run_cfg(hedge_faults=fh(seed=3, **kw))
        c = self.run_cfg(hedge_faults=fh(seed=4, **kw))
        self.assertEqual(self.key(*a), self.key(*b))
        self.assertEqual(a[1].hedge_report["counters"], b[1].hedge_report["counters"])
        self.assertNotEqual(a[1].hedge_report["counters"], c[1].hedge_report["counters"])

    def test_the_injector_does_not_move_the_latency_stream(self):
        a = self.run_cfg()
        b = self.run_cfg(hedge_faults=fh(reject_prob=0.0, delay_prob=0.0, response="unwind"))
        self.assertEqual(a[0].latency.report(), b[0].latency.report())

    def test_every_hedge_rejected_the_whole_run_unwinds_and_the_ledger_balances(self):
        bt, r = self.run_cfg(hedge_faults=fh(reject_prob=1.0, response="unwind"))
        c = r.hedge_report["counters"]
        self.assertGreater(c["failed_fault_rejected"], 0)
        self.assertEqual(c.get("hedges_filled", 0), 0)
        self.assertGreater(c["unwinds_sent"], 0)
        # every maker fill is flattened through a real order; whatever is left is accounted for
        left = abs(r.net_base_position)
        self.assertAlmostEqual(left, c.get("residual_qty", 0.0) + c.get("instant_unwind_not_done_qty", 0.0)
                               + sum(e["qty"] - e["unwound_qty"] for e in r.hedge_report["events"] if e["outcome"] is None), places=6)
        signed = {v: 0.0 for v in (BYBIT, BINANCE)}
        for x in bt.trade_log:
            signed[x["venue"]] += (1 if x["side"] == "buy" else -1) * x["qty"]
        for v in signed:
            self.assertAlmostEqual(r.final_inventory[v], signed[v], places=9)

    def test_no_recovery_the_position_cap_bounds_the_inventory_that_piles_up(self):
        bt, r = self.run_cfg(hedge_faults=fh(reject_prob=1.0, response="none"))
        cap = 2.0 * bt.config.arb.max_position_base
        self.assertLessEqual(r.hedge_report["exposure"]["peak_abs_net_base"], cap + 1e-9)

    def test_entry_guard_stops_new_signals_during_a_gateway_outage_that_covers_the_whole_run(self):
        out = (Outage(BINANCE, 0.0, 1e12),)
        free = self.run_cfg(hedge_faults=fh(outages=out, outage_policy="blackhole", response="none"))
        guarded = self.run_cfg(hedge_faults=fh(outages=out, outage_policy="blackhole", response="none", entry_guard=True))
        self.assertGreater(free[1].orders_sent, 0)
        self.assertEqual(guarded[1].orders_sent, 0)
        self.assertGreater(guarded[1].hedge_report["counters"]["signals_blocked_by_entry_guard"], 0)

    def test_full_outage_stops_the_strategy_from_evaluating(self):
        bt, r = self.run_cfg(hedge_faults=fh(outages=(Outage(BYBIT, 0.0, 1e12, "full"),), response="none"))
        self.assertEqual(r.signals_generated, 0)
        self.assertGreater(r.hedge_report["counters"]["evaluations_skipped_feed_dark"], 0)


if __name__ == "__main__":
    unittest.main()
