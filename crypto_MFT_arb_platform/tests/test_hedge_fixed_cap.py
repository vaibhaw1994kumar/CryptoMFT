"""Phase 1-lite: the fixed hedge limit (execution.hedge.cap = breakeven_plus_buffer).

One limit per hedge chain = the price at which the maker fill and its hedge net zero after the maker fee and the taker fee, moved
cap_buffer_bps against us. The first IOC and every retry carry that same limit (no chasing); misses go down the existing ladder
(retries, then the banded unwind). Harness books: Binance bids 99/98, asks 101/102; a maker bid for 2.0 at 100 on Bybit fills
(bybit_perp_mm maker -0.5 bps) and the sell hedge goes to Binance (taker 5.0 bps)."""
from __future__ import annotations

import unittest

from new_backtest.execution import ExecutionConfig
from new_backtest.execution.config import ExecutionConfigError
from new_backtest.hedge_faults import HedgeFaultConfig

from test_maker_order_lifetime import BINANCE, BYBIT, Harness

FAULTS = dict(response="retry_unwind", max_retries=2, retry_backoff_s=0.25, unwind_band_bps=15.0, unwind_max_attempts=3, ack_timeout_s=2.0)
BREAK_EVEN = 100.0 * (1.0 + (-0.5 + 5.0) / 1e4)        # fill 100, maker -0.5 bps, taker 5.0 bps


def harness(buffer_bps: float | None, **faults) -> Harness:
    hedge = {"tactic": "IOC_CAPPED", "slack_bps_floor": 5.0}
    if buffer_bps is not None:
        hedge.update(cap="breakeven_plus_buffer", cap_buffer_bps=buffer_bps)
    h = Harness(faults=HedgeFaultConfig(**{**FAULTS, **faults}), cfg_kw={"execution": ExecutionConfig(hedge=hedge)}, max_walk_bps=2.0)
    h.book(0.5, BYBIT)
    h.book(0.5, BINANCE)
    h.send_maker(1.0)
    h.sell_trade(5.0, BYBIT, volume=2.5)                # fills the maker bid -> the hedge decision
    return h


def hedge_children(h: Harness, intent="sig1-H1"):
    return [o for o in h.bt.pending.in_flight() if o.is_hedge and o.kind == "order" and o.intent_id == intent]


def run_out(h: Harness):
    for t in (5.1, 5.5, 6.0, 7.0, 9.0, 12.0):
        h.bt._fire_timers_before(t)
        h.advance(t)


class ConfigTests(unittest.TestCase):
    def test_the_fixed_cap_needs_a_non_negative_buffer_and_no_policy(self):
        ExecutionConfig(hedge={"tactic": "IOC_CAPPED", "cap": "breakeven_plus_buffer", "cap_buffer_bps": 0})
        for bad in ({}, {"cap_buffer_bps": -1.0}, {"cap_buffer_bps": True}, {"cap_buffer_bps": 2.0, "policy": "capped_ioc_dp"}):
            with self.assertRaises(ExecutionConfigError):
                ExecutionConfig(hedge={"tactic": "IOC_CAPPED", "cap": "breakeven_plus_buffer", **bad})


class FixedLimitTests(unittest.TestCase):
    def test_default_keeps_todays_touch_slack_limit(self):
        h = harness(None)
        kid = hedge_children(h)[0]
        self.assertAlmostEqual(kid.limit_price, h.bt.xl.touch_slack_cap("sell", 99.0, 2.0), places=12)
        self.assertNotIn("policy", h.bt.xl.intents["sig1-H1"].meta)
        self.assertEqual(h.bt.hedge["hedges_fixed_cap"], 0)

    def test_the_limit_is_the_break_even_moved_by_the_buffer(self):
        for buf in (0.0, 1.0, 8.0, 150.0):
            h = harness(buf)
            kid = hedge_children(h)[0]
            self.assertAlmostEqual(kid.limit_price, BREAK_EVEN * (1 - buf / 1e4), places=9)
            self.assertAlmostEqual(h.bt.xl.intents["sig1-H1"].worst_price, kid.limit_price, places=12)
            self.assertEqual(h.bt.hedge["hedges_fixed_cap"], 1)

    def test_a_buy_hedge_mirrors_it(self):
        self.assertAlmostEqual(100.0 * (1.0 - 4.5e-4) * (1 + 2e-4), 100.0 * (1 - (-0.5 + 5.0) / 1e4) * (1 + 2.0 / 1e4))

    def test_a_cap_below_the_bid_fills_against_the_book_and_a_cap_above_it_misses(self):
        hit = harness(150.0)                       # cap ~98.54: the bid at 99 is inside it -> the IOC sells
        run_out(hit)
        self.assertGreater(hit.bt.hedge["hedges_filled"], 0)
        miss = harness(0.0)                        # cap 100.045 > bid 99: nothing fills, the chain retries at the same limit then unwinds
        run_out(miss)
        self.assertEqual(miss.bt.hedge["hedges_filled"], 0)
        c = miss.bt.xl.counts
        self.assertEqual(c["children_IOC_CAPPED"], 1 + FAULTS["max_retries"])
        self.assertGreaterEqual(c["children_MARKET_BANDED"], 1)
        self.assertEqual(c["one_hedge_child_violations"], 0)

    def test_every_retry_re_sends_the_same_limit_even_after_the_touch_moves(self):
        h = harness(150.0, reject_prob=1.0)        # every IOC is rejected: the retries must keep the first limit
        first = hedge_children(h)[0].limit_price
        h.book(5.05, BINANCE, bids=((90.0, 5.0), (89.0, 5.0)), asks=((91.0, 5.0), (92.0, 5.0)))    # the touch falls ~9 %
        seen = {first}
        for t in (5.1, 5.5, 6.0, 7.0):
            h.bt._fire_timers_before(t)
            h.advance(t)
            seen |= {o.limit_price for o in hedge_children(h)}
        self.assertGreaterEqual(h.bt.hedge["retries_sent"], 1)
        self.assertEqual(seen, {first})
        self.assertAlmostEqual(h.bt.xl.intents["sig1-H1"].worst_price, first, places=12)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class HedgeArrivalBookTests(unittest.TestCase):
    def test_only_view_or_truth_is_accepted_and_view_is_the_default(self):
        from new_backtest.backtester import ArbBacktestConfig
        self.assertEqual(ArbBacktestConfig.__dataclass_fields__["hedge_arrival_book"].default, "view")
        with self.assertRaises(ValueError):
            Harness(faults=HedgeFaultConfig(**FAULTS), cfg_kw={"hedge_arrival_book": "arrival"})

    def test_truth_without_truth_books_executes_against_the_same_book_as_view(self):
        res = {}
        for mode in ("view", "truth"):
            h = Harness(faults=HedgeFaultConfig(**FAULTS), cfg_kw={"execution": ExecutionConfig(hedge={"tactic": "IOC_CAPPED", "cap": "breakeven_plus_buffer",
                                                                                                   "cap_buffer_bps": 150.0}),
                                                                    "hedge_arrival_book": mode, "record_hedge_attempts": True}, max_walk_bps=2.0)
            h.book(0.5, BYBIT)
            h.book(0.5, BINANCE)
            h.send_maker(1.0)
            h.sell_trade(5.0, BYBIT, volume=2.5)
            run_out(h)
            res[mode] = [(r["filled_qty"], r["vwap"], r["limit"]) for r in h.bt.hedge_attempt_rows]
        self.assertTrue(res["view"])
        self.assertEqual(res["view"], res["truth"])           # the harness has no venue-truth books: _arrival_book falls back to the view
