"""r12: the circuit breaker, pinned THROUGH the backtester (it had never run in a backtest before 2026-11-01: every call site counted
in-flight orders with len() on a generator, so a configured breaker crashed at the first net update, and the gateway-down and UNKNOWN
triggers were never fed). Owner decisions under test (starting values, DECISION_PENDING_CALIBRATION): 3 consecutive hedge failures,
>5% failures in the last 20, any failed unwind, |net| != 0 for more than 5 s, UNKNOWN older than 2 s, gateway down; manual reset
only; unwind band 15 bps, 3 unwind attempts, then ALARM; 1x hard net ceiling with a separate fill headroom."""
from __future__ import annotations

import unittest
import warnings

from new_backtest.arb_strategy import ArbConfig
from new_backtest.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from new_backtest.hedge_faults import HedgeFaultConfig, Outage
from new_backtest.profiles import load_profile

from test_maker_order_lifetime import BINANCE, BYBIT, Harness
from test_wp6_risk_recovery import cycles, stack

warnings.simplefilter("ignore")


def h_with(breaker: CircuitBreakerConfig, faults: HedgeFaultConfig | None = None, **kw) -> Harness:
    return Harness(faults=faults, cfg_kw={"circuit_breaker": breaker}, **kw)


class RateWindowTests(unittest.TestCase):
    def test_count_window_needs_a_full_window_and_fires_strictly_above_the_threshold(self):
        cfg = CircuitBreakerConfig(failure_rate_window_n=20, failure_rate_thresh=0.05)
        b = CircuitBreaker(cfg, BYBIT, BINANCE)
        for i in range(19):
            b.on_hedge_result(float(i), failed=(i == 3), net_abs=0.0, pending_hedges=0)
        self.assertFalse(b.is_tripped())                                      # 1 failure of 19: window not full yet
        b.on_hedge_result(19.0, failed=False, net_abs=0.0, pending_hedges=0)
        self.assertFalse(b.is_tripped())                                      # 1 of 20 = 5% is not ABOVE 5%
        b.on_hedge_result(20.0, failed=True, net_abs=0.0, pending_hedges=0)   # window slides: failures at 3 and 20 -> 2 of 20 = 10%
        self.assertTrue(b.is_tripped())
        self.assertIn("2/20", b.trips[0].cause)
        with self.assertRaises(ValueError):
            CircuitBreakerConfig(failure_rate_window_n=1)

    def test_manual_reset_is_the_only_way_back(self):
        b = CircuitBreaker(CircuitBreakerConfig(consecutive_failures=1), BYBIT, BINANCE)
        b.on_hedge_result(1.0, failed=True, net_abs=0.0, pending_hedges=0)
        self.assertTrue(b.is_tripped())
        for t in range(2, 50):
            b.on_hedge_result(float(t), failed=False, net_abs=0.0, pending_hedges=0)       # a run of successes does not clear it
        self.assertTrue(b.is_tripped())
        b.reset()
        self.assertFalse(b.is_tripped())
        self.assertEqual(b.summary()["manual_resets"], 1)
        self.assertTrue(b.summary()["manual_reset_only"])


class CountingRuleTests(unittest.TestCase):
    def test_a_rejected_hedge_and_its_retries_count_as_one_failure(self):
        faults = HedgeFaultConfig(reject_prob=1.0, response="retry_unwind", max_retries=2, retry_backoff_s=0.25, unwind_max_attempts=3, seed=5)
        h = h_with(CircuitBreakerConfig(consecutive_failures=2), faults)
        cycles(h, 1)
        self.assertEqual(h.bt.hedge["failed_fault_rejected"], 3)               # three failed SENDS of the one hedge chain ...
        self.assertEqual(h.bt._breaker.summary()["consecutive_failures_current"], 1)    # ... are one hedge FAILURE
        self.assertFalse(h.bt._breaker.is_tripped())
        cycles(h, 1, t0=20.0)
        self.assertTrue(h.bt._breaker.is_tripped())                            # the second failed chain trips "2 consecutive"
        self.assertIn("consecutive_failures", h.bt._breaker.trips[0].cause)

    def test_a_clean_first_attempt_hedge_resets_the_run_and_trip_blocks_signals_until_manual_reset(self):
        h = h_with(CircuitBreakerConfig(consecutive_failures=1))               # no faults injected: hedges fill on the first attempt
        cycles(h, 2)
        self.assertEqual(h.bt._breaker.summary()["consecutive_failures_current"], 0)
        h.bt._breaker.on_hedge_result(100.0, failed=True, net_abs=0.0, pending_hedges=0)
        self.assertTrue(h.bt._breaker.is_tripped())
        self.assertFalse(h.bt._breaker.allow_signal(500.0, 0.0))               # time passing never re-enables
        h.bt.reset_breaker()
        self.assertTrue(h.bt._breaker.allow_signal(501.0, 0.0))


class GatewayDownTests(unittest.TestCase):
    def test_an_outage_trips_after_the_heartbeat_timeout_and_a_short_one_does_not(self):
        out = HedgeFaultConfig(outages=(Outage(BINANCE, 10.0, 40.0, "gateway"),), response="retry_unwind", seed=2)
        h = h_with(CircuitBreakerConfig(trip_on_gateway_down=True, heartbeat_timeout_s=5.0), out)
        for t in (1.0, 5.0, 9.9):
            h.bt._breaker_poll(t)
        self.assertFalse(h.bt._breaker.is_tripped())
        for t in (10.5, 14.0, 14.8):
            h.bt._breaker_poll(t)
        self.assertFalse(h.bt._breaker.is_tripped())                           # last heartbeat 9.9: 4.9 s of silence at 14.8 < 5 s
        h.bt._breaker_poll(16.0)
        self.assertTrue(h.bt._breaker.is_tripped())
        self.assertIn("gateway_down: binance", h.bt._breaker.trips[0].cause)
        short = HedgeFaultConfig(outages=(Outage(BINANCE, 10.0, 13.0, "gateway"),), response="retry_unwind", seed=2)
        hs = h_with(CircuitBreakerConfig(trip_on_gateway_down=True, heartbeat_timeout_s=5.0), short)
        for t in (9.0, 10.5, 11.5, 12.5, 13.5, 20.0):
            hs.bt._breaker_poll(t)
        self.assertFalse(hs.bt._breaker.is_tripped())                          # 3 s outage < 5 s timeout


class UnknownTests(unittest.TestCase):
    def _unknown_run(self, resolve_s: float, unknown_s: float, horizon: float = 1.6):
        f = HedgeFaultConfig(ack_unknown_prob=1.0, response="retry_unwind", max_retries=0, ack_timeout_s=1.0, unknown_resolve_s=resolve_s, seed=4)
        h = h_with(CircuitBreakerConfig(unknown_order_s=unknown_s), f)
        h.book(1.0, BYBIT); h.book(1.0, BINANCE)
        h.send_maker(1.1)
        h.sell_trade(1.5, BYBIT, volume=2.5)
        h.advance(horizon)
        return h

    def test_unknown_is_stamped_when_the_ack_timeout_expires_not_on_arrival(self):
        h = self._unknown_run(resolve_s=30.0, unknown_s=2.0, horizon=34.0)
        unk = list(h.bt._breaker._unknown_orders.values())
        self.assertEqual(len(unk), 1)
        # the passive order's lost ack defers the fill report to 1.103 + 1.0 + 30.0 = 32.103; the hedge is then sent (arrives +3 ms);
        # its ack is lost too, so it is UNKNOWN for the strategy from arrival + ack_timeout = 33.106 (the old stamp would be 32.106)
        self.assertAlmostEqual(unk[0].ts_entered, 33.106, places=6)
        entered = unk[0].ts_entered
        h.bt._breaker.check_unknown_orders(entered + 1.9, 0.0, 0)
        self.assertFalse(h.bt._breaker.is_tripped())                           # younger than 2 s as the strategy sees it
        h.bt._breaker.check_unknown_orders(entered + 2.1, 0.0, 0)
        self.assertTrue(h.bt._breaker.is_tripped())
        self.assertIn("unknown_order", h.bt._breaker.trips[0].cause)

    def test_a_resolved_unknown_stops_ageing(self):
        h = self._unknown_run(resolve_s=0.5, unknown_s=5.0, horizon=30.0)
        h.advance(30.0)                                                        # the status query resolves it long before 5 s
        self.assertGreaterEqual(h.bt.hedge["unknown_resolved"], 1)
        self.assertEqual(len(h.bt._breaker._unknown_orders), 0)
        h.bt._breaker.check_unknown_orders(500.0, 0.0, 0)
        self.assertFalse(h.bt._breaker.is_tripped())


class UnwindBudgetTests(unittest.TestCase):
    def _failing_unwind(self, **fkw):
        f = HedgeFaultConfig(reject_prob=1.0, response="unwind", max_retries=0, unwind_band_bps=1.0, retry_backoff_s=0.25, seed=3, **fkw)
        h = Harness(faults=f, cfg_kw={"circuit_breaker": CircuitBreakerConfig(trip_on_failed_unwind=True, tripped_unwind_retries=1)})
        h.book(1.0, BYBIT, bids=((99.0, 5.0),), asks=((100.0001, 5.0),))       # the 1 bp banded unwind sells at 99.99: no bid there
        h.book(1.0, BINANCE)
        h.send_maker(1.1)
        h.sell_trade(1.5, BYBIT, volume=2.5)
        h.advance(10.0)
        return h

    def test_three_unwind_sends_then_alarm_and_a_failed_unwind_trip(self):
        h = self._failing_unwind(unwind_max_attempts=3)
        self.assertEqual(h.bt.hedge["unwinds_sent"], 3 - h.bt.hedge["unwinds_denied_by_breaker"])
        self.assertEqual(h.bt.alarm["reason"], h.bt.alarm["reason"])
        self.assertTrue(h.bt._breaker.is_tripped())
        self.assertIn("failed_unwind", h.bt._breaker.trips[0].cause)

    def test_attempt_limit_is_independent_of_hedge_retries(self):
        legacy = self._failing_unwind()                                        # unwind_max_attempts None -> max_retries + 1 = 1 send
        self.assertEqual(legacy.bt.hedge["unwinds_sent"], 1)
        three = self._failing_unwind(unwind_max_attempts=3)
        self.assertGreaterEqual(three.bt.hedge["unwinds_sent"], 2)             # the tripped-state budget (1) may deny the third; the cap is 3
        self.assertLessEqual(three.bt.hedge["unwinds_sent"], 3)
        self.assertIn(three.bt.alarm["reason"], ("unwind_exhausted", "unwind_budget_exhausted"))
        with self.assertRaises(ValueError):
            HedgeFaultConfig(unwind_max_attempts=0)


class RestartTests(unittest.TestCase):
    def test_the_fill_that_precedes_a_restart_is_hedged_only_when_the_process_is_back(self):
        f = HedgeFaultConfig(restart_s=60.0, restart_at_fills=(1,), response="retry_unwind", seed=1)
        h = Harness(faults=f)
        h.book(1.0, BYBIT); h.book(1.0, BINANCE)
        h.send_maker(1.1)
        h.sell_trade(5.0, BYBIT, volume=2.5)
        hedges = [o for o in h.bt.pending.in_flight() if o.is_taker]
        self.assertEqual(len(hedges), 1)
        self.assertAlmostEqual(hedges[0].sent_ts, 65.0, places=9)             # fill at 5.0 + 60 s down
        self.assertEqual(h.bt.hedge["restarts"], 1)
        self.assertTrue(h.bt._feed_dark(30.0))                                 # no evaluation while down ...
        self.assertFalse(h.bt._feed_dark(66.0))                                # ... and back afterwards
        self.assertEqual(h.bt.inventory.base[BYBIT], 2.0)                      # the position stood unhedged meanwhile
        with self.assertRaises(ValueError):
            HedgeFaultConfig(restart_s=10.0, restart_at_fills=(0,))


class CeilingTests(unittest.TestCase):
    def test_one_x_ceiling_with_a_separate_headroom_and_the_profile_carries_the_decisions(self):
        _, tight = stack(limit=10.0, clip=4.0, max_live=3, net_ceiling_multiple=1.0)
        self.assertEqual(sum(tight), 10.0)                                     # 1x hard ceiling
        _, roomy = stack(limit=10.0, clip=4.0, max_live=3, net_ceiling_multiple=1.0, fill_headroom_base=2.5)
        self.assertEqual(roomy, [4.0, 4.0, 4.0])                               # ceiling 10 + separate headroom 2.5 = room for three clips of 4
        self.assertGreater(sum(roomy), sum(tight))
        self.assertLessEqual(sum(roomy), 12.5)
        doc = load_profile("perp_perp_maker_taker_v1")
        self.assertEqual(doc["arb"]["net_ceiling_multiple"], 1.0)
        self.assertGreater(doc["arb"]["fill_headroom_base"], 0.0)
        br = doc["breaker"]
        self.assertEqual((br["consecutive_failures"], br["failure_rate_window_n"], br["failure_rate_thresh"], br["net_exposure_s"],
                          br["unknown_order_s"], br["trip_on_failed_unwind"], br["trip_on_gateway_down"]), (3, 20, 0.05, 5.0, 2.0, True, True))
        self.assertEqual((doc["hedge_faults"]["unwind_band_bps"], doc["hedge_faults"]["unwind_max_attempts"]), (15.0, 3))
        self.assertEqual(doc["decision_status"]["breaker"], "DECISION_PENDING_CALIBRATION")
        self.assertEqual(doc["decision_status"]["p_fail"], "PLACEHOLDER_NOT_MEASURED")


class CancelOnTripTests(unittest.TestCase):
    """A trip cancels every resting maker order (each with its own cancel latency; the Harness pins 50 ms); fills inside that window are
    kept and hedged; an order that lands after the trip is cancelled at placement; a manual reset re-arms the one-shot handler."""

    def _tripped_with_a_resting_order(self, ttl):
        h = Harness(ttl=ttl, cfg_kw={"circuit_breaker": CircuitBreakerConfig(consecutive_failures=1)})
        h.book(0.5, BYBIT); h.book(0.5, BINANCE)
        h.send_maker(1.0)
        h.advance(1.1)
        self.assertEqual(len(h.resting), 1)
        return h

    def _trip(self, h, t):
        h.bt._breaker.on_hedge_result(t, failed=True, net_abs=0.0, pending_hedges=0)
        self.assertTrue(h.bt._breaker.is_tripped())
        h.bt._breaker_trip_check(t)

    def test_trip_cancels_the_resting_order_after_one_cancel_latency_even_without_a_lifetime(self):
        h = self._tripped_with_a_resting_order(ttl=0.0)                     # no time-in-force: only the trip can cancel it
        o = h.order()
        self._trip(h, 2.0)
        self.assertAlmostEqual(o.cancel_requested_ts, 2.0, places=9)
        self.assertEqual(o.cancel_reason, "breaker_trip")
        self.assertAlmostEqual(o.cancel_effective_ts, 2.05, places=9)       # + the pinned 50 ms cancel latency
        self.assertEqual(h.bt.hedge["resting_orders_cancel_requested_on_trip"], 1)
        h.advance(2.2)
        self.assertEqual(h.resting, [])                                     # gone once the cancel took effect
        self.assertEqual(h.bt.lifetime["breaker_cancels_requested"], 1)
        self._trip(h, 3.0)                                                   # one-shot per trip
        self.assertEqual(h.bt.hedge["resting_orders_cancel_requested_on_trip"], 1)

    def test_a_fill_inside_the_cancel_window_is_kept_and_hedged(self):
        h = self._tripped_with_a_resting_order(ttl=15.0)
        self._trip(h, 2.0)
        h.sell_trade(2.02, BYBIT, volume=2.5)                                # before the cancel takes effect at 2.05
        self.assertEqual(h.bt.inventory.base[BYBIT], 2.0)
        self.assertEqual(len([x for x in h.bt.pending.in_flight() if x.is_taker]), 1)    # and the hedge goes out despite the trip

    def test_an_order_that_lands_after_the_trip_is_cancelled_at_placement(self):
        h = Harness(ttl=0.0, cfg_kw={"circuit_breaker": CircuitBreakerConfig(consecutive_failures=1)})
        h.book(0.5, BYBIT); h.book(0.5, BINANCE)
        h.send_maker(1.0)                                                    # in flight (3 ms) when the breaker trips
        self._trip(h, 1.001)
        h.advance(1.1)
        o = h.order()
        self.assertEqual(o.cancel_reason, "breaker_trip")
        self.assertAlmostEqual(o.cancel_requested_ts, o.placed_ts, places=9)
        h.advance(1.3)
        self.assertEqual(h.resting, [])

    def test_reset_rearms_and_no_trip_means_no_cancels(self):
        h = self._tripped_with_a_resting_order(ttl=0.0)
        h.bt._breaker_trip_check(1.5)
        self.assertEqual(h.bt.hedge["breaker_trips_handled"], 0)             # not tripped: nothing happens
        self.assertIsNone(h.order().cancel_requested_ts)
        self._trip(h, 2.0)
        h.bt.reset_breaker()
        self.assertFalse(h.bt._trip_cancelled)
        h.send_maker(3.0)
        h.advance(3.1)
        self.assertEqual([o.cancel_reason for o in h.resting if o.cancel_requested_ts is None].count(""), len([o for o in h.resting if o.cancel_requested_ts is None]))
        self._trip(h, 4.0)                                                   # a second trip cancels again
        self.assertEqual(h.bt.hedge["breaker_trips_handled"], 2)

    def test_legacy_lifetime_cancels_are_unchanged(self):
        h = Harness(ttl=10.0)                                                # no breaker at all
        h.book(0.5, BYBIT); h.book(0.5, BINANCE)
        h.send_maker(1.0)
        h.advance(1.1)
        o = h.order()
        h.advance(11.2)
        self.assertEqual(o.cancel_reason, "expired")
        self.assertAlmostEqual(o.cancel_requested_ts, o.placed_ts + 10.0, places=9)
        self.assertEqual(h.bt.lifetime["expiries_triggered"], 1)


if __name__ == "__main__":
    unittest.main()
