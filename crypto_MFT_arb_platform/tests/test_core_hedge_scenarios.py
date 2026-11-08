"""Phase C: the hedge scenarios of tests/test_integration_scenarios.py (S04, S05, S16, S17, S18, S19 and the non-valid-book case) run through the CORE path
(OMS under FILL_TRIGGERED, scripted venues) with the backtester's hand-calculated result written beside each.

Same shape as the backtester scripts: maker BUY 2.0 @100.0 on BYBIT, hedge SELL on BINANCE at 99.0, unwinds sell on BYBIT at 99.0. Compared: the fills (venue, side, quantity, price),
the final positions, and the counts (retries, rejected attempts, unwinds, queries). NOT compared: fees and cash. The core prices fees with its own schedule, so the cash lines of
the old scenarios are a Phase D comparison. Where the old scenario is a `test_target_*` that fails today on the backtester (S05 ghost, S18 blind unwind), the core must pass it.

The delays and outages of the old scripts are simulation: here a venue simply answers late, never, or only after the query, which is what the core rules react to."""
from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb.execution import (DispatchPolicy, ExecutionEventType, OrderTimeoutPolicy, StrategyDirectedRouter, VenueOrderStatus)
from crypto_stat_arb.hedge_lifecycle import HedgePolicy, HedgeState as H
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import OrderState
from crypto_stat_arb.oms import OMS

from test_fill_triggered import INSTR, MS, S, Venue, book_at, maker_update
from test_oms import instrument_master, make_book, opportunity, risk


def scenario_books():
    return {("BYBIT", INSTR): make_book("BYBIT", "99.0", "101.0", "5"), ("BINANCE", INSTR): make_book("BINANCE", "99.0", "101.0", "5")}


class Scenario(unittest.TestCase):
    def build(self, hedge=(), *, policy=None, timeout=None, qty="2.0", query=None):
        self.ledger = Ledger()
        self.bybit = Venue("BYBIT", ["ack"], price="99.0")             # the maker rests; an unwind sells here at 99.0
        self.up = False
        self.binance = Venue("BINANCE", hedge, price="99.0", query=query)
        self.books = scenario_books()
        self.oms = OMS(instrument_master(), risk(), self.ledger, router=StrategyDirectedRouter(instrument_master(), adapters={"BYBIT": self.bybit, "BINANCE": self.binance}),
                       dispatch_policy=DispatchPolicy.FILL_TRIGGERED, hedge_policy=policy, order_timeout_policy=timeout)
        opp = replace(opportunity(qty), buy_venue="BYBIT", sell_venue="BINANCE", buy_price=Decimal("100.0"), sell_price=Decimal("99.0"))
        self.plan = self.oms.execute(opp, self.books, maker_venue="BYBIT")
        self.maker = self.plan.children[0]
        self.n = 0
        return self

    def maker_fill(self, cum, ts, state=ExecutionEventType.PARTIAL):
        self.n += 1
        u = maker_update(self.plan, self.maker, self.n, state, cum, ts)
        self.oms.ingest_order_update(replace(u, average_price=Decimal("100.0")))

    @property
    def chain(self):
        (c,) = self.oms.hedge_chains[self.plan.parent_id].values()
        return c

    def fills(self):
        out = []
        for o in self.plan.children:
            if o.filled_quantity > 0:
                out.append((o.venue, o.side.value, float(o.filled_quantity), float(o.average_price)))
        return out

    def position(self, venue):
        return float(sum(((o.filled_quantity if o.side.value == "BUY" else -o.filled_quantity) for o in self.plan.children if o.venue == venue), Decimal("0")))

    def unwinds(self):
        return [o for o in self.plan.children if o.order_role == "UNWIND"]


class S04HedgeRejectedRetryThenUnwind(Scenario):
    """OLD (backtester): retries 2, rejected hedge attempts 3, unwinds 1; fills maker buy 2.0 @100 bybit, unwind sell 2.0 @99.0 bybit; positions 0 / 0; no hedge fill on binance."""

    def test_core_path(self):
        self.build(hedge=[("reject", "-1003")] * 3, policy=HedgePolicy(retry_limit=2, retry_backoff_ns=250 * MS))
        self.maker_fill("2.0", 5 * S, ExecutionEventType.FILLED)
        for t in (5.3, 5.6, 6.0):
            self.oms.advance_time(int(t * S))
        c = self.chain
        self.assertEqual((c.retries, len(c.reject_labels)), (2, 3))
        self.assertEqual(len(self.unwinds()), 1)
        self.assertEqual(self.fills(), [("BYBIT", "BUY", 2.0, 100.0), ("BYBIT", "SELL", 2.0, 99.0)])
        self.assertEqual((self.position("BYBIT"), self.position("BINANCE")), (0.0, 0.0))
        self.assertEqual(c.outcome, "unwound_reconciled")


class S05LostAckThenGhostFillAtReconnect(Scenario):
    """OLD target (fails on the backtester): maker 1.2 of 2.0 filled, hedge into a gateway outage, written off at the ack timeout, delivered at reconnect. Requirement: query before
    replacing, exactly ONE hedge ever exists. Final bybit +1.2, binance -1.2, net 0, no unwind, no ghost over-hedge, the 1.2 stays reserved until the order is found.
    OLD today: unwinds_sent 1 at 7.0, then the ghost sells 1.2: net -1.2."""

    def test_core_path_one_hedge_flat_after_reconnect(self):
        timeout = OrderTimeoutPolicy(report_timeout_ns=2 * S, query_interval_ns=1 * S, max_queries=6, kill_on_unresolved=True)
        down = {"v": True}
        self.build(hedge=["silent"], timeout=timeout, query=lambda oid: None if down["v"] else VenueOrderStatus(
            order_id=oid, venue="BINANCE", instrument_id=INSTR, state=ExecutionEventType.FILLED, cumulative_filled_quantity=Decimal("1.2"), average_price=Decimal("99.0")))
        self.maker_fill("1.2", 5 * S)
        self.oms.advance_time(int(7.5 * S))
        c = self.chain
        self.assertEqual(c.state, H.UNKNOWN)                                         # not written off, not replaced, not unwound
        self.assertEqual(len(self.unwinds()), 0)
        self.assertEqual(len(self.binance.submits), 1)
        self.assertEqual(self.oms.risk.stats_snapshot()["active_reservations"], 1)   # the exposure is still reserved while the hedge is unresolved
        self.assertEqual(self.position("BYBIT"), 1.2)
        self.oms.advance_time(int(9.5 * S))
        self.assertEqual(c.state, H.UNKNOWN)
        down["v"] = False                                                            # reconnect: the query is answered
        self.oms.advance_time(int(10.5 * S))
        self.assertEqual(c.state, H.HEDGED)
        self.assertEqual(len(self.binance.submits), 1)                               # one hedge order ever
        self.assertEqual(len(self.unwinds()), 0)
        self.assertEqual((self.position("BYBIT"), self.position("BINANCE")), (1.2, -1.2))


class S16DelayedHedgeMissesWhenTheBookMoves(Scenario):
    """OLD (a) book unchanged: fills maker buy 2.0 @100 and sell 2.0 @99.0 binance, positions +2.0 / -2.0, open 1.003 s.
    OLD (b) bids fall through the hedge limit: failed_price_moved 1, the strategy unwinds: fills maker buy 2.0 @100 and unwind sell 2.0 @99.0 bybit, positions 0 / 0, outcome unwound."""

    def test_a_book_unchanged(self):
        self.build(hedge=["ack"])
        self.maker_fill("2.0", 5 * S, ExecutionEventType.FILLED)
        hedge = [o for o in self.plan.children if o.order_role == "HEDGE"][0]
        self.n += 1
        from crypto_stat_arb.execution import CanonicalOrderUpdate, OrderUpdateSource
        self.oms.ingest_order_update(CanonicalOrderUpdate(update_id="late-fill", source=OrderUpdateSource.PRIVATE_STREAM, parent_order_id=self.plan.parent_id, order_id=hedge.order_id,
                                                         venue="BINANCE", instrument_id=INSTR, state=ExecutionEventType.FILLED, event_ts_ns=int(6.003 * S), observed_ts_ns=int(6.003 * S),
                                                         sequence=1, cumulative_filled_quantity=Decimal("2.0"), average_price=Decimal("99.0")))
        self.assertEqual(self.fills(), [("BYBIT", "BUY", 2.0, 100.0), ("BINANCE", "SELL", 2.0, 99.0)])
        self.assertEqual((self.position("BYBIT"), self.position("BINANCE")), (2.0, -2.0))
        self.assertAlmostEqual(self.chain.residual_exposure_duration_s, 1.003, places=3)       # open for the 1.003 s the old scenario measured

    def test_b_book_moves_through_the_limit(self):
        self.build(hedge=["ioc_nofill"])                                              # arrives late, the bids are 98 / 97: nothing inside the limit
        self.maker_fill("2.0", 5 * S, ExecutionEventType.FILLED)
        c = self.chain
        self.assertEqual(c.reject_labels, ["ioc_unfilled"])                           # the engine refusal (old counter: failed_price_moved 1)
        self.assertEqual(self.fills(), [("BYBIT", "BUY", 2.0, 100.0), ("BYBIT", "SELL", 2.0, 99.0)])
        self.assertEqual((self.position("BYBIT"), self.position("BINANCE")), (0.0, 0.0))
        self.assertEqual(c.outcome, "unwound_reconciled")


class S17PartialHedgeFill(Scenario):
    """OLD: the hedge fills 1.0 of 2.0, the rest fails like a rejection, response unwind. Fills: maker buy 2.0 @100, sell 1.0 @99.0 binance, unwind sell 1.0 @99.0 bybit.
    Positions bybit +1.0, binance -1.0, net 0; unwound_qty 1.0."""

    def test_core_path(self):
        self.build(hedge=[("ioc_partial", "1.0")])
        self.maker_fill("2.0", 5 * S, ExecutionEventType.FILLED)
        c = self.chain
        self.assertEqual(self.fills(), [("BYBIT", "BUY", 2.0, 100.0), ("BINANCE", "SELL", 1.0, 99.0), ("BYBIT", "SELL", 1.0, 99.0)])
        self.assertEqual((self.position("BYBIT"), self.position("BINANCE")), (1.0, -1.0))
        self.assertEqual((c.partial_fills, c.unwound_qty), (1, 1.0))


class S18OutageBlackholeThenUnwind(Scenario):
    """OLD today: unwind sent at the ack timeout (7.0) with no query. OLD target (fails on the backtester): at 7.5 no unwind yet, maker fill still open (bybit +2.0, binance 0),
    the 2.0 still reserved. After the query finds nothing the replacement is allowed (here: no retry budget, so the unwind: sell 2.0 on bybit @99.0, positions 0 / 0)."""

    def test_core_path_no_unwind_before_the_query_and_the_unwind_after_it_finds_nothing(self):
        timeout = OrderTimeoutPolicy(report_timeout_ns=2 * S, query_interval_ns=1 * S, max_queries=6, kill_on_unresolved=True)
        down = {"v": True}
        self.build(hedge=["silent"], timeout=timeout, query=lambda oid: None if down["v"] else VenueOrderStatus(
            order_id=oid, venue="BINANCE", instrument_id=INSTR, state=None, reason="order unknown to venue"))
        self.maker_fill("2.0", 5 * S, ExecutionEventType.FILLED)
        self.oms.advance_time(int(7.5 * S))
        c = self.chain
        self.assertEqual((c.state, len(self.unwinds())), (H.UNKNOWN, 0))
        self.assertEqual((self.position("BYBIT"), self.position("BINANCE")), (2.0, 0.0))
        self.assertEqual(self.oms.risk.stats_snapshot()["active_reservations"], 1)
        down["v"] = True
        self.oms.advance_time(int(9.5 * S))
        down["v"] = False
        self.oms.advance_time(int(10.5 * S))
        self.assertEqual(len(self.unwinds()), 1)
        self.assertEqual(self.fills(), [("BYBIT", "BUY", 2.0, 100.0), ("BYBIT", "SELL", 2.0, 99.0)])
        self.assertEqual((self.position("BYBIT"), self.position("BINANCE")), (0.0, 0.0))


class S19LostAckOnTheHedgeIsResolvedByQueryNotRetry(Scenario):
    """OLD: the hedge executes but its acknowledgement is lost; the strategy is UNKNOWN and resolves it by a query, never a retry. Fills maker buy 2.0 @100, sell 2.0 @99.0 binance;
    retries_sent 0; positions +2.0 / -2.0."""

    def test_core_path(self):
        self.build(hedge=["silent"], policy=HedgePolicy(retry_limit=3), query=lambda oid: VenueOrderStatus(
            order_id=oid, venue="BINANCE", instrument_id=INSTR, state=ExecutionEventType.FILLED, cumulative_filled_quantity=Decimal("2.0"), average_price=Decimal("99.0")))
        self.maker_fill("2.0", 5 * S, ExecutionEventType.FILLED)
        self.oms.advance_time(int(8 * S))
        c = self.chain
        self.assertEqual(len(self.binance.submits), 1)
        self.assertEqual((c.retries, c.state), (0, H.RECONCILED))
        self.assertEqual(self.fills(), [("BYBIT", "BUY", 2.0, 100.0), ("BINANCE", "SELL", 2.0, 99.0)])
        self.assertEqual((self.position("BYBIT"), self.position("BINANCE")), (2.0, -2.0))


class HedgeOnANonValidBook(Scenario):
    """OLD today: without faults the hedge is refused on a stale book and the fill is flattened at once; with faults the hedge does not look at the book state (the Phase B finding).
    CORE RULE: the hedge is held (HEDGE_PENDING) while its book is not valid; it is sent when the book is valid, or the residual age limit escalates it to the unwind. Never sent
    blind, never flattened at once, never dropped. The old 'flatten at once' is the escalation here, after the age limit."""

    def test_held_then_sent_when_the_book_is_valid(self):
        self.build(hedge=[("fill", None)])
        self.books[("BINANCE", INSTR)].start_resync("stale")
        self.maker_fill("2.0", 5 * S, ExecutionEventType.FILLED)
        self.assertEqual((self.chain.state, len(self.binance.submits)), (H.HEDGE_PENDING, 0))
        fresh = book_at("BINANCE", "99.0", "101.0", 5_500 * MS)
        self.oms._books[("BINANCE", INSTR)] = fresh
        self.oms.on_book_update("BINANCE", INSTR, 5_600 * MS)
        self.assertEqual(self.fills(), [("BYBIT", "BUY", 2.0, 100.0), ("BINANCE", "SELL", 2.0, 99.0)])

    def test_held_until_the_age_limit_then_unwound(self):
        self.build(hedge=[("fill", None)])
        self.books[("BINANCE", INSTR)].start_resync("stale")
        self.maker_fill("2.0", 5 * S, ExecutionEventType.FILLED)
        self.oms.advance_time(11 * S)
        self.assertEqual(len([o for o in self.plan.children if o.order_role == "HEDGE"]), 0)
        self.assertTrue(any(e["to"] == "ESCALATED" for e in self.chain.events))
        self.assertEqual((self.position("BYBIT"), self.position("BINANCE")), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
