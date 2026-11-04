"""Phase 1: the offline hedge evaluator reproduces the engine on a scripted capture, and its pieces behave as stated."""
from __future__ import annotations

import unittest

from new_backtest.execution.ioc_policy import FlickerLaw
from new_backtest.execution import offline_hedge as oh
from new_backtest.hedge_faults import HedgeFaultConfig

from test_maker_order_lifetime import BINANCE, BYBIT, Harness


def captured(faults=None, binance_after=()):
    h = Harness(faults=faults, cfg_kw={"record_hedge_attempts": True})
    h.book(0.5, BYBIT)
    h.book(0.5, BINANCE)
    h.send_maker(1.0)
    h.sell_trade(5.0, BYBIT, volume=2.5)                                           # fill -> hedge decision at 5.0
    for t, bids in binance_after:
        h.book(t, BINANCE, bids=bids, asks=((101.0, 5.0), (102.0, 5.0)))
    h.advance(20.0)
    bt = h.bt
    return bt.hedge_attempt_rows, bt.hedge_paths


class ValidationTests(unittest.TestCase):
    def test_a_full_fill_is_reproduced(self):
        rows, paths = captured(binance_after=[(5.2, ((99.0, 5.0), (98.0, 5.0)))])
        v = oh.validate(rows, paths)
        self.assertEqual((v["attempts"], v["mismatched"]), (1, 0), v)

    def test_a_partial_fill_against_thin_depth_is_reproduced(self):
        # the hedge for 2.0 meets only 1.2 inside the limit (98.9505): 99 x 1.2 then 98 is outside the limit
        rows, paths = captured(binance_after=[(5.001, ((99.0, 1.2), (98.0, 5.0)))])
        v = oh.validate(rows, paths)
        self.assertEqual(v["mismatched"], 0, v)
        self.assertLess(rows[0]["filled_qty"], rows[0]["qty"])

    def test_a_miss_when_the_book_moved_through_the_limit_is_reproduced(self):
        rows, paths = captured(binance_after=[(5.001, ((97.0, 5.0), (96.0, 5.0)))])
        v = oh.validate(rows, paths)
        self.assertEqual(v["mismatched"], 0, v)
        self.assertEqual(rows[0]["filled_qty"], 0.0)

    def test_the_fault_path_is_reproduced_too(self):
        f = HedgeFaultConfig(response="retry", max_retries=2, retry_backoff_s=0.25, ack_timeout_s=2.0)
        rows, paths = captured(faults=f, binance_after=[(5.001, ((97.0, 5.0), (96.0, 5.0))), (5.5, ((99.0, 5.0), (98.0, 5.0)))])
        v = oh.validate(rows, paths)
        self.assertEqual(v["mismatched"], 0, v)
        self.assertGreaterEqual(v["attempts"], 2)                                  # the miss and a retry


class CaptureTests(unittest.TestCase):
    def test_capture_is_off_by_default_and_records_nothing(self):
        h = Harness()
        h.book(0.5, BYBIT); h.book(0.5, BINANCE); h.send_maker(1.0); h.sell_trade(5.0, BYBIT, volume=2.5)
        self.assertEqual(h.bt.hedge_attempt_rows, [])
        self.assertEqual({v: len(p) for v, p in h.bt.hedge_paths.items()}, {BYBIT: 0, BINANCE: 0})

    def test_the_row_carries_the_decision_context(self):
        rows, _ = captured()
        r = rows[0]
        self.assertEqual((r["side"], r["venue"], r["attempt"]), ("sell", BINANCE, 0))
        self.assertEqual((r["dec_bid"], r["fund_bid"]), (99.0, 99.0))
        self.assertAlmostEqual(r["decision_ts"], 5.0)
        self.assertGreater(r["arrival_ts"], r["decision_ts"])


class PathTests(unittest.TestCase):
    def test_book_lookups_are_strict_before_arrival_and_inclusive_at_decision(self):
        e = [(1.0, [(99.0, 1.0)], [(101.0, 1.0)]), (2.0, [(98.0, 1.0)], [(100.0, 1.0)])]
        pb = oh.Path5(e)
        self.assertEqual(pb.strictly_before(2.0).best_bid, 99.0)                    # an event AT the arrival is applied after the order
        self.assertEqual(pb.at_or_before(2.0).best_bid, 98.0)                       # the decision sees the event at its own time
        self.assertIsNone(pb.strictly_before(1.0))

    def test_metrics_slippage_sign_and_unwind_penalty(self):
        chains = [{"side": "sell", "dec_touch": 100.0}]
        res = [{"filled": 1.0, "vwap": 99.99, "attempts": 2, "first_full": 0, "exhausted": False, "legging_s": 0.4, "remaining": 0.0}]
        m = oh.metrics(chains, res)
        self.assertAlmostEqual(m["slippage_bps"]["mean"], -1.0, places=6)           # sold 1 bp below the decision touch: adverse
        self.assertEqual(m["misses"], 1)
        ex = oh.metrics([{"side": "sell", "dec_touch": 100.0}], [{"filled": 0.0, "vwap": None, "attempts": 3, "first_full": 0, "exhausted": True, "legging_s": None, "remaining": 1.0}], loss_fail_bps=7.8)
        self.assertAlmostEqual(ex["unwind_penalty_usdt"], -7.8 / 1e4 * 100.0 * 1.0, places=9)                 # 7.8 bps of 100 x 1.0 = 0.078


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
