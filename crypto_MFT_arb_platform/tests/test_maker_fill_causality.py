"""Maker fills must be causal in EXCHANGE time.

An order sent at host time s reaches the exchange at host time a = s + entry latency (InFlightOrder.release_ts). A public trade
that executed at the exchange before `a` cannot have filled it, even if the feed delivers that trade to us after `a`. Such an
event may still correct the (stale) queue-ahead estimate taken from the local book, so it drains the queue but never fills.
Events without an exchange time, or before the clock aligner has a sample, fall back to receive time (the legacy rule).
"""
from __future__ import annotations

import unittest

from new_backtest.event_loader import BOOK
from new_backtest.fill_engine import MakerConfig

from test_maker_order_lifetime import BINANCE, BYBIT, Harness


def arrived(**maker_kw):
    """Order sent at 0.0, lands at 0.003 (placed_ts == 0.003); queue ahead 0.5; exchange clock == host clock."""
    h = Harness(ttl=0.0, queue=0.5)
    for k, v in maker_kw.items():
        setattr(h.bt.maker.config, k, v)
    h.bt.latency.aligner.samples = 1            # aligner ready, offset 0
    h.book(0.0, BYBIT)
    h.book(0.0, BINANCE)
    h.send_maker(0.0)
    h.advance(0.003)
    return h


class ExchangeTimeEligibilityTests(unittest.TestCase):
    def test_a_trade_that_executed_before_the_order_arrived_cannot_fill_it(self):
        h = arrived()
        o = h.order()
        self.assertEqual((o.arrival_ts, o.queue_ahead), (0.003, 0.5))
        # executed at 0.002 (before arrival), delivered at 0.010 (after placement)
        h.sell_trade(0.010, BYBIT, volume=2.5, exchange_ts=0.002)
        self.assertEqual((o.filled_qty, o.remaining), (0.0, 2.0))
        self.assertEqual(h.bt.trade_log, [])
        self.assertEqual(h.bt.inventory.base[BYBIT], 0.0)

    def test_it_still_corrects_the_stale_queue_estimate(self):
        h = arrived()
        o = h.order()
        h.sell_trade(0.010, BYBIT, volume=0.3, exchange_ts=0.002)
        self.assertAlmostEqual(o.queue_ahead, 0.2, places=9)            # 0.5 - 0.3, no fill
        self.assertEqual(o.filled_qty, 0.0)

    def test_a_trade_that_executed_after_arrival_fills_normally(self):
        h = arrived()
        h.sell_trade(0.010, BYBIT, volume=2.5, exchange_ts=0.0035)
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], 2.0, places=9)

    def test_arrival_time_itself_is_eligible(self):
        h = arrived()
        h.sell_trade(0.010, BYBIT, volume=2.5, exchange_ts=0.003)
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], 2.0, places=9)

    def test_just_before_arrival_is_not(self):
        h = arrived()
        h.sell_trade(0.010, BYBIT, volume=2.5, exchange_ts=0.003 - 1e-6)
        self.assertEqual(h.bt.inventory.base[BYBIT], 0.0)

    def test_legacy_switch_restores_receive_time_eligibility(self):
        h = arrived(exchange_time_eligibility=False)
        h.sell_trade(0.010, BYBIT, volume=2.5, exchange_ts=0.002)
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], 2.0, places=9)

    def test_without_an_exchange_time_or_a_ready_clock_receive_time_is_used(self):
        h = arrived()
        h.sell_trade(0.010, BYBIT, volume=2.5)                              # no exchange_ts on the event
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], 2.0, places=9)
        h2 = arrived()
        h2.bt.latency.aligner.samples = 0                                    # clock not calibrated yet
        h2.sell_trade(0.010, BYBIT, volume=2.5, exchange_ts=0.002)
        self.assertAlmostEqual(h2.bt.inventory.base[BYBIT], 2.0, places=9)

    def test_the_host_clock_offset_is_applied_to_the_exchange_time(self):
        h = arrived()
        h.bt.latency.aligner.offset_s = 0.010                                # host runs 10 ms ahead of the exchange
        # exchange stamp 0.0 -> host 0.010 (after arrival at 0.003): eligible
        h.sell_trade(0.020, BYBIT, volume=2.5, exchange_ts=0.0)
        self.assertAlmostEqual(h.bt.inventory.base[BYBIT], 2.0, places=9)
        h2 = arrived()
        h2.bt.latency.aligner.offset_s = -0.010                              # host behind: 0.0 -> -0.010, before arrival
        h2.sell_trade(0.020, BYBIT, volume=2.5, exchange_ts=0.0)
        self.assertEqual(h2.bt.inventory.base[BYBIT], 0.0)


if __name__ == "__main__":
    unittest.main()
