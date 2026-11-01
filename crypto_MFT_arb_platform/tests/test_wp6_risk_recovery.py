"""WP6 risk / recovery / failure handling: reachable-exposure limits, the hedge-fault injector, the stressed preset's double count,
price-banded unwinds, the persistent ALARM, UNKNOWN status-query failures, margin utilisation."""
from __future__ import annotations

import unittest
import warnings
from types import SimpleNamespace

from new_backtest.arb_strategy import ArbConfig, InventoryBook
from new_backtest.hedge_faults import HedgeFaultConfig
from new_backtest.margin_model import MarginModel, MarginModelConfig, MarginTier, VenueMargin

from test_inflight_order_tracking import decide, market, warm
from test_maker_order_lifetime import BINANCE, BYBIT, Harness

warnings.simplefilter("ignore")


def stack(limit, clip, max_live, n=12, **arb_kw):
    h = Harness(ttl=0.0, entry_s=1.0, max_live=max_live, max_clip_base=clip, max_position_base=limit, count_pending_exposure=True,
                cfg_kw={"auto_pending_exposure": False}, **arb_kw)
    warm(h)
    sizes = []
    for i in range(n):
        t = 0.01 + i * 0.01
        market(h, t)
        sig, _ = decide(h, t)
        if sig is not None:
            sizes.append(round(sig.qty, 9))
    return h, sizes


class ReachableExposureTests(unittest.TestCase):
    def test_net_ceiling_multiple_one_limits_the_stack_to_the_documented_limit(self):
        _, legacy = stack(limit=10.0, clip=4.0, max_live=3)
        self.assertEqual(legacy, [4.0, 4.0, 4.0])                             # legacy 2x: 12 accepted (the strict xfail elsewhere)
        _, tight = stack(limit=10.0, clip=4.0, max_live=3, net_ceiling_multiple=1.0)
        self.assertLessEqual(sum(tight), 10.0)
        self.assertEqual(tight, [4.0, 4.0, 2.0])                              # the third clip is cut to the room left

    def test_gross_long_limit_counts_pending_buys(self):
        _, sizes = stack(limit=10.0, clip=4.0, max_live=3, max_gross_long_base=6.0)
        self.assertEqual(sizes, [4.0, 2.0])
        cfg = ArbConfig(max_gross_short_base=1.0)
        self.assertEqual(cfg.max_gross_short_base, 1.0)


def cycles(h: Harness, n: int, t0: float = 1.0, gap: float = 10.0) -> None:
    """n hedged maker cycles: fresh books, a 2.0 bid at 100, a sell print fills it, the hedge goes out and lands."""
    t = t0
    for _ in range(n):
        h.book(t, BYBIT); h.book(t, BINANCE)
        h.send_maker(t + 0.1)
        h.sell_trade(t + 0.5, BYBIT, volume=2.5)
        h.advance(t + gap - 0.5)
        t += gap


class InjectorTests(unittest.TestCase):
    def _run(self, p, **kw):
        h = Harness(faults=HedgeFaultConfig(reject_prob=p, response="retry_unwind", max_retries=0, seed=3, **kw))
        cycles(h, 40)
        return h

    def test_reject_prob_changes_failed_hedge_counts_and_cash(self):
        out = {p: self._run(p) for p in (0.0, 0.1, 0.3)}
        fails = {p: out[p].bt.hedge["failed_fault_rejected"] for p in out}
        self.assertEqual(fails[0.0], 0)
        self.assertGreater(fails[0.1], 0)
        self.assertGreater(fails[0.3], fails[0.1])
        cash = {p: out[p].bt.inventory.total_cash for p in out}
        self.assertNotEqual(cash[0.0], cash[0.3])
        self.assertGreater(out[0.3].bt.hedge["unwinds_sent"], 0)              # the response policy ran: unwinds on the maker venue
        self.assertEqual(out[0.0].bt.hedge["unwinds_sent"], 0)

    def test_stressed_double_count_is_off_when_by_venue_rejects_are_passive_only(self):
        both = self._run(0.3, reject_prob_by_venue=((BINANCE, 0.3),))                      # legacy: hedges can be hit twice
        once = self._run(0.3, reject_prob_by_venue=((BINANCE, 0.3),), venue_reject_applies_to_hedges=False)
        self.assertGreater(both.bt.hedge["hedges_rejected_by_venue_prob"], 0)
        self.assertEqual(once.bt.hedge["hedges_rejected_by_venue_prob"], 0)
        self.assertGreater(both.bt.hedge["failed_fault_rejected"], once.bt.hedge["failed_fault_rejected"])
        from new_backtest import fault_presets
        self.assertIn("venue_reject_applies_to_hedges=False", open(fault_presets.__file__).read())


class UnwindAndAlarmTests(unittest.TestCase):
    def test_unwind_is_price_banded_when_configured(self):
        h = Harness(faults=HedgeFaultConfig(reject_prob=1.0, response="unwind", max_retries=0, unwind_band_bps=50.0))
        h.book(1.0, BYBIT); h.book(1.0, BINANCE)
        h.send_maker(1.1)
        h.sell_trade(1.5, BYBIT, volume=2.5)
        h.advance(1.6)                                                          # hedge rejected -> unwind sent on bybit and lands
        # banded: sell limit = 100 x (1 - 50 bps) = 99.50 above the 99 bid: the unwind does not execute (price band respected)
        self.assertEqual(h.bt.hedge["unwinds_sent"], 1)
        self.assertEqual(h.bt.hedge["failed_unwind_price_moved"], 1)
        self.assertEqual(h.bt.inventory.base[BYBIT], 2.0)
        plain = Harness(faults=HedgeFaultConfig(reject_prob=1.0, response="unwind", max_retries=0))
        plain.book(1.0, BYBIT); plain.book(1.0, BINANCE)
        plain.send_maker(1.1); plain.sell_trade(1.5, BYBIT, volume=2.5); plain.advance(1.6)
        self.assertEqual(plain.bt.hedge["unwind_filled_qty"], 2.0)             # market unwind: sold into the 99 bid
        self.assertEqual(plain.bt.inventory.base[BYBIT], 0.0)

    def test_unwind_exhaustion_raises_a_persistent_alarm_that_blocks_new_risk_until_reset(self):
        h = Harness(faults=HedgeFaultConfig(reject_prob=1.0, response="unwind", max_retries=0, unwind_band_bps=1.0))
        h.book(1.0, BYBIT, bids=((99.0, 5.0),), asks=((100.0001, 5.0),))       # the unwind's band price 99.99 finds no bid: it misses
        h.book(1.0, BINANCE)
        h.send_maker(1.1)
        h.sell_trade(1.5, BYBIT, volume=2.5)
        h.advance(3.0)                                                          # hedge rejected -> unwind misses -> no retries left
        bt = h.bt
        self.assertIsNotNone(bt.alarm)
        self.assertEqual(bt.alarm["reason"], "unwind_exhausted")
        self.assertEqual(bt.inventory.base[BYBIT], 2.0)                        # exposure retained, reported
        self.assertEqual(bt.hedge["alarms_raised"], 1)
        bt.reset_alarm()
        self.assertIsNone(bt.alarm)
        self.assertEqual(len(bt.alarms), 1)                                    # history kept
        self.assertEqual(bt.hedge["alarms_reset"], 1)

    def test_unknown_status_query_failures_are_bounded_then_alarm(self):
        h = Harness(faults=HedgeFaultConfig(ack_unknown_prob=1.0, response="retry_unwind", max_retries=0, unknown_resolve_s=1.0,
                                           ack_timeout_s=1.0, unknown_query_fail_prob=1.0, unknown_query_max_attempts=2))
        h.book(1.0, BYBIT); h.book(1.0, BINANCE)
        h.send_maker(1.1)
        h.sell_trade(1.5, BYBIT, volume=2.5)
        h.advance(20.0)
        bt = h.bt
        self.assertGreaterEqual(bt.hedge["unknown_orders"], 1)               # ack_unknown_prob=1 also hits the passive order
        self.assertEqual(bt.hedge["unknown_query_failed"], 2)                  # two attempts, both failed
        self.assertEqual(bt.hedge["unknown_resolved"], 0)
        self.assertEqual(bt.alarm["reason"], "unknown_unresolved")
        ok = Harness(faults=HedgeFaultConfig(ack_unknown_prob=1.0, response="retry_unwind", max_retries=0, unknown_resolve_s=1.0, ack_timeout_s=1.0))
        ok.book(1.0, BYBIT); ok.book(1.0, BINANCE); ok.send_maker(1.1); ok.sell_trade(1.5, BYBIT, volume=2.5); ok.advance(20.0)
        self.assertEqual((ok.bt.hedge["unknown_resolved"], ok.bt.alarm), (1, None))


class MarginUtilisationTests(unittest.TestCase):
    def test_utilisation_limit_blocks_a_signal_the_bare_margin_would_allow(self):
        tiers = (MarginTier(1e6, 0.10, 0.05),)
        books = {BYBIT: SimpleNamespace(ready=True, mid=100.0, levels=lambda s, n: [(100.0, 50.0)]),
                 BINANCE: SimpleNamespace(ready=True, mid=100.0, levels=lambda s, n: [(100.0, 50.0)])}
        sig = SimpleNamespace(buy_venue=BYBIT, sell_venue=BINANCE, qty=1.0)
        cfg = MarginModelConfig(venues={v: VenueMargin(15.0, tiers) for v in (BYBIT, BINANCE)}, max_utilisation=0.5)
        m = MarginModel(cfg, (BYBIT, BINANCE))
        self.assertFalse(m.allows(sig, InventoryBook(), books))               # IM 10 / equity 15 = 0.67 > 0.5
        self.assertEqual(m.stats["signals_blocked_by_utilisation"], 1)
        loose = MarginModel(MarginModelConfig(venues={v: VenueMargin(15.0, tiers) for v in (BYBIT, BINANCE)}), (BYBIT, BINANCE))
        self.assertTrue(loose.allows(sig, InventoryBook(), books))


if __name__ == "__main__":
    unittest.main()


class BreakerWiringTests(unittest.TestCase):
    """The breaker's call sites counted in-flight orders with len() on a generator: every configured breaker crashed the first
    net update. Pinned: a configured breaker survives the run-loop calls and trips on net exposure duration."""

    def test_breaker_call_sites_run_and_the_net_exposure_trigger_trips(self):
        from new_backtest.circuit_breaker import CircuitBreakerConfig
        h = Harness(cfg_kw={"circuit_breaker": CircuitBreakerConfig(net_exposure_s=1.0, consecutive_failures=2)})
        bt = h.bt
        self.assertIsNotNone(bt._breaker)
        h.book(0.5, BYBIT); h.book(0.5, BINANCE)
        h.send_maker(1.0)
        h.sell_trade(5.0, BYBIT, volume=2.5)                                    # maker filled; hedge in flight: |net| > 0
        bt._breaker.on_net_update(5.0, abs(bt.inventory.net_base), bt.pending.in_flight_count())
        bt._breaker.check_unknown_orders(5.0, abs(bt.inventory.net_base), bt.pending.in_flight_count())
        self.assertFalse(bt._breaker.is_tripped())
        bt._breaker.on_net_update(7.0, 2.0, bt.pending.in_flight_count())        # |net| non-zero for 2 s > 1 s
        self.assertTrue(bt._breaker.is_tripped())
        self.assertFalse(bt._breaker.allow_signal(7.0, 2.0))
        self.assertEqual(bt.pending.in_flight_count(), sum(1 for _ in bt.pending.in_flight()))
