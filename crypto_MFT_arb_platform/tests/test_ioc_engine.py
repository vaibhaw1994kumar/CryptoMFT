"""Phase 1: the capped-IOC hedge policy wired into the backtester (execution.hedge.policy = capped_ioc_dp).

Harness books: Binance bids 99/98, asks 101/102; a maker bid for 2.0 at 100 on Bybit fills (bybit_perp_mm maker -0.5 bps) and the
sell hedge goes to Binance (taker 5.0 bps). Law: p0 .5, p+ .2, p- .3, eta+ 1, eta- 2 (bps)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from new_backtest.execution import ExecutionConfig
from new_backtest.execution.ioc_policy import FlickerLaw, PolicyParams, attempts_from_latency, choose, limit_price
from new_backtest.hedge_faults import HedgeFaultConfig

from test_maker_order_lifetime import BINANCE, BYBIT, Harness

LAW = {"p0": 0.5, "p_plus": 0.2, "p_minus": 0.3, "eta_plus": 1.0, "eta_minus": 2.0}
TMP = tempfile.mkdtemp(prefix="flicker_")
FLICKER = Path(TMP) / "f.json"
FLICKER.write_text(json.dumps({"sets": {"t": {"vol_edges_bps": [0.0, 0.0], "cells": {"pooled": {"n": 100, "fit": LAW}}}}}))
FAULTS = dict(response="retry_unwind", max_retries=2, retry_backoff_s=0.25, unwind_band_bps=15.0, unwind_max_attempts=3, ack_timeout_s=2.0)


def harness(hedge: dict, **faults) -> Harness:
    ex = ExecutionConfig(hedge={"tactic": "IOC_CAPPED", "policy": "capped_ioc_dp", "flicker_model": str(FLICKER), "flicker_set": "t",
                                "slack_bps_floor": 5.0, **hedge})
    h = Harness(faults=HedgeFaultConfig(**{**FAULTS, **faults}), cfg_kw={"execution": ex}, max_walk_bps=2.0)
    h.book(0.5, BYBIT)
    h.book(0.5, BINANCE)
    h.send_maker(1.0)
    h.sell_trade(5.0, BYBIT, volume=2.5)                                         # fills the maker bid -> the hedge decision
    return h


def children(h: Harness, intent="sig1-H1"):
    return [o for o in h.bt.pending.in_flight() if o.is_hedge and o.kind == "order" and o.intent_id == intent]


class PolicyIdentityTests(unittest.TestCase):
    def test_touch_slack_cap_with_huge_phi_reproduces_the_phase0_limit_exactly(self):
        h = harness({"cap": "touch_slack", "urgency_phi": 1e9, "attempts": 3})
        kids = children(h)
        self.assertEqual(len(kids), 1)
        phase0 = h.bt.strategy.hedge_leg(h.bt._trackers[1].signal, 2.0, h.bt.books[BINANCE]).limit_price
        self.assertEqual(kids[0].limit_price, phase0)                           # bit for bit
        self.assertEqual(kids[0].limit_price, h.bt.xl.touch_slack_cap("sell", 99.0, 2.0))


class BreakEvenCapTests(unittest.TestCase):
    def test_worst_price_is_the_break_even_moved_by_the_buffer_and_the_limit_sits_inside_it(self):
        h = harness({"cap": "conditional_edge_breakeven", "cap_buffer_bps": 150.0, "urgency_phi": 0.0, "deadline_s": 2.0})
        hed = h.bt.xl.intents["sig1-H1"]
        be = 100.0 * (1.0 + (-0.5 + 5.0) / 1e4)                                # fill price 100, maker -0.5 bps, taker 5.0 bps
        self.assertAlmostEqual(hed.worst_price, be * (1 - 150.0 / 1e4), places=9)
        kid = children(h)[0]
        self.assertGreaterEqual(kid.limit_price, hed.worst_price)
        k = h.bt._ioc.attempts(h.bt._ioc_cycle_s(BINANCE))
        self.assertEqual(k, attempts_from_latency(2.0, h.bt._ioc_cycle_s(BINANCE)))
        d_cap = (99.0 - hed.worst_price) / 99.0 * 1e4
        ch = choose(FlickerLaw(**LAW), PolicyParams(fee_bps=5.0, v0_bps=-h.bt.config.arb.loss_fail_bps, phi=0.0, q=2.0,
                                                    dt_s=round(h.bt._ioc_cycle_s(BINANCE), 4)), d_cap, k)
        self.assertEqual(kid.limit_price, hed.worst_price if ch.clipped else limit_price("sell", 99.0, ch.offset_bps, hed.worst_price))
        self.assertLess(ch.offset_bps, 0.0)                                      # phi = 0 with several attempts left: a speculative limit, above the touch
        self.assertGreater(kid.limit_price, 99.0)
        self.assertEqual(h.bt.xl.counts["max_live_hedge_children"], 1)

    def test_an_unreachable_cap_sends_nothing_and_is_counted(self):
        h = harness({"cap": "conditional_edge_breakeven", "cap_buffer_bps": 0.0})   # break-even 100.045 vs a bid at 99: ~105 bps away
        self.assertEqual(children(h), [])
        self.assertEqual(h.bt.hedge["hedge_cap_unreachable"], 1)
        self.assertEqual(h.bt._ioc.counts["cap_unreachable"], 1)
        hed = h.bt.xl.intents["sig1-H1"]
        self.assertEqual(hed.children, [])                                      # the intent exists (journaled) but never sent a child
        self.assertEqual(h.bt.hedge["failed_hedge_cap_unreachable"], 1)
        self.assertEqual(h.bt.xl.intent_for(2, "hedge").intent_id, "sig1-H1")   # the failed chain is bound to its intent for the retries

    def test_the_attempt_budget_replaces_max_retries_then_the_ladder_unwinds(self):
        # every hedge is rejected at the exchange: the chain makes attempts_total attempts, then the banded unwind
        h = harness({"cap": "touch_slack", "urgency_phi": 1e9, "attempts": 2}, reject_prob=1.0)
        for t in (5.1, 5.5, 6.0, 7.0, 9.0, 12.0):
            h.bt._fire_timers_before(t)
            h.advance(t)
        c = h.bt.xl.counts
        self.assertEqual(c["children_IOC_CAPPED"], 2)                           # attempts: 2 (max_retries would have allowed 3)
        self.assertGreaterEqual(c["children_MARKET_BANDED"], 1)                 # then the unwind
        self.assertEqual(c["one_hedge_child_violations"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
