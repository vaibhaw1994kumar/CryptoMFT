"""Phase C: the hedge scenarios of tests/test_integration_scenarios.py (S04, S05, S16, S17, S18, S19 and the non-valid-book case) run through the CORE path
(OMS under FILL_TRIGGERED, scripted venues) with the backtester's hand-calculated result written beside each.

Same shape as the backtester scripts: maker BUY 2.0 @100.0 on BYBIT, hedge SELL on BINANCE at 99.0, unwinds sell on BYBIT at 99.0. Compared: the fills (venue, side, quantity, price),
the final positions, the counts (retries, rejected attempts, unwinds, queries), AND fees and cash per venue. The core FeeSchedule is set to the old path's rates: bybit maker -0.5 bps
taker 5.5 (bybit_perp_mm), binance maker 2.0 taker 5.0 (binance_perp_vip0), so the old cash lines apply unchanged: cash(v) = sell notional - buy notional - fees on v. Where the old scenario is a `test_target_*` that fails today on the backtester (S05 ghost, S18 blind unwind), the core must pass it.

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

from test_fill_triggered import INSTR, MS, S, Venue, book_at, cb, maker_update
from test_oms import instrument_master, make_book, opportunity, risk


def scenario_instruments():
    """The instrument master with the old path's fee rates (bybit_perp_mm, binance_perp_vip0): the same numbers the backtester scenarios were hand-calculated with."""
    from crypto_stat_arb.instruments import InstrumentMaster
    from crypto_stat_arb.models import Instrument
    rates = {"BYBIT": ("-0.5", "5.5"), "BINANCE": ("2.0", "5.0")}
    return InstrumentMaster([Instrument(instrument_id=INSTR, venue=v, symbol="BTCUSDT", base="BTC", quote="USDT", tick_size=Decimal("0.10"), quantity_step=Decimal("0.001"),
                                        min_quantity=Decimal("0.001"), min_notional=Decimal("5"), maker_fee_bps=Decimal(rates[v][0]), taker_fee_bps=Decimal(rates[v][1]))
                             for v in ("BINANCE", "BYBIT")])


def scenario_books():
    return {("BYBIT", INSTR): make_book("BYBIT", "99.0", "101.0", "5"), ("BINANCE", INSTR): make_book("BINANCE", "99.0", "101.0", "5")}


class Scenario(unittest.TestCase):
    def build(self, hedge=(), *, policy=None, timeout=None, qty="2.0", query=None, residual=None):
        self.ledger = Ledger()
        self.bybit = Venue("BYBIT", ["ack"], price="99.0")             # the maker rests; an unwind sells here at 99.0
        self.up = False
        self.binance = Venue("BINANCE", hedge, price="99.0", query=query)
        self.books = scenario_books()
        inst = scenario_instruments()
        self.oms = OMS(inst, risk(), self.ledger, router=StrategyDirectedRouter(inst, adapters={"BYBIT": self.bybit, "BINANCE": self.binance}),
                       dispatch_policy=DispatchPolicy.FILL_TRIGGERED, hedge_policy=policy, order_timeout_policy=timeout, residual_policy=residual)
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

    def cash(self, venue):
        """cash(v) = sell notional - buy notional - fees booked on v (a rebate is a negative fee), the same definition the backtester scenarios use."""
        c = Decimal("0")
        for o in self.plan.children:
            if o.venue == venue:
                n = o.filled_quantity * o.average_price
                c += (n if o.side.value == "SELL" else -n) - o.fee
        return float(c)

    def fees(self):
        return float(sum((o.fee for o in self.plan.children), Decimal("0")))

    def assert_money(self, bybit, binance, fees):
        """Cash per venue and total fees against the old path's hand-calculated numbers (9 decimals)."""
        self.assertAlmostEqual(self.cash("BYBIT"), bybit, places=9)
        self.assertAlmostEqual(self.cash("BINANCE"), binance, places=9)
        self.assertAlmostEqual(self.fees(), fees, places=9)

    def unwinds(self):
        return [o for o in self.plan.children if o.order_role == "UNWIND"]


class S01CleanFillAndHedge(Scenario):
    """OLD: maker buy 2.0 @100 bybit, hedge sell 2.0 @99.0 binance; position +2.0 / -2.0; orders sent 2; hedges sent 1, filled 1; one signal, fully hedged;
    fees 0.089, cash bybit -199.99, binance +197.901, total -2.089."""

    def test_core_path(self):
        self.build(hedge=[("fill", None)])
        self.maker_fill("2.0", 5 * S, ExecutionEventType.FILLED)
        self.assertEqual(self.fills(), [("BYBIT", "BUY", 2.0, 100.0), ("BINANCE", "SELL", 2.0, 99.0)])
        self.assertEqual((len(self.bybit.submits) + len(self.binance.submits), len(self.binance.submits)), (2, 1))
        self.assertEqual((self.position("BYBIT"), self.position("BINANCE")), (2.0, -2.0))
        self.assert_money(-199.99, 197.901, 0.089)
        self.assertAlmostEqual(self.cash("BYBIT") + self.cash("BINANCE"), -2.089, places=9)
        self.assertEqual(self.chain.state, H.RECONCILED)
        self.assertEqual(self.plan.terminal_outcome, "FILLED")


class S02MakerPartialFill(Scenario):
    """OLD: print 1.7 fills 1.2 of the maker; hedge sell 1.2 @99.0 for the filled quantity only; fees -0.006 + 0.0594 = 0.0534; cash bybit -119.994, binance +118.7406; position +1.2 / -1.2;
    the other 0.8 rests until its lifetime ends and is cancelled; still exactly one hedge, position unchanged."""

    def test_core_path(self):
        self.build(hedge=[("fill", None)])
        self.maker_fill("1.2", 5 * S)
        self.assertEqual(self.fills(), [("BYBIT", "BUY", 1.2, 100.0), ("BINANCE", "SELL", 1.2, 99.0)])
        self.assert_money(-119.994, 118.7406, 0.0534)
        self.assertEqual(self.maker.state, OrderState.PARTIAL)                         # the other 0.8 is still resting
        self.oms.cancel_open_orders("lifetime ended", 15 * S)                          # the quote's time in force ends
        self.assertEqual(self.maker.state, OrderState.CANCELLED)
        self.assertEqual(len(self.binance.submits), 1)                                 # still exactly one hedge
        self.assertEqual(len(self.fills()), 2)
        self.assertEqual((self.position("BYBIT"), self.position("BINANCE")), (1.2, -1.2))
        self.assert_money(-119.994, 118.7406, 0.0534)
        self.assertEqual(self.oms.risk.stats_snapshot()["active_reservations"], 0)


class S03MakerNeverFills(Scenario):
    """OLD: the maker never fills: no hedge, no position, fees 0, cash 0 / 0, orders sent 1, nothing left resting. (3b, depth-only fills from the legacy fill setting, is the maker
    simulator's behaviour: Phase D.)"""

    def test_core_path(self):
        self.build(hedge=[("fill", None)])
        self.oms.cancel_open_orders("lifetime ended", 15 * S)
        self.assertEqual(self.fills(), [])
        self.assertEqual((len(self.bybit.submits) + len(self.binance.submits), len(self.binance.submits)), (1, 0))
        self.assertEqual((self.position("BYBIT"), self.position("BINANCE")), (0.0, 0.0))
        self.assert_money(0.0, 0.0, 0.0)
        self.assertEqual(self.maker.state, OrderState.CANCELLED)


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
        self.assert_money(-2.0989, 0.0, 0.0989)                                       # OLD: cash bybit -2.0989, binance 0, fees 0.0989


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
        self.assert_money(-119.994, 118.7406, 0.0534)                                 # hand-calculated as S02: -1.2x100x0.5/1e4 + 1.2x99x5/1e4


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
        self.assert_money(-199.99, 197.901, 0.089)                                    # OLD: cash -199.99 / +197.901, fees 0.089

    def test_b_book_moves_through_the_limit(self):
        self.build(hedge=["ioc_nofill"])                                              # arrives late, the bids are 98 / 97: nothing inside the limit
        self.maker_fill("2.0", 5 * S, ExecutionEventType.FILLED)
        c = self.chain
        self.assertEqual(c.reject_labels, ["ioc_unfilled"])                           # the engine refusal (old counter: failed_price_moved 1)
        self.assertEqual(self.fills(), [("BYBIT", "BUY", 2.0, 100.0), ("BYBIT", "SELL", 2.0, 99.0)])
        self.assertEqual((self.position("BYBIT"), self.position("BINANCE")), (0.0, 0.0))
        self.assertEqual(c.outcome, "unwound_reconciled")
        self.assert_money(-2.0989, 0.0, 0.0989)                                       # OLD: -2.0989 / 0, fees 0.0989


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
        self.assert_money(-101.04445, 98.9505, 0.09395)                               # OLD: bybit -101.04445, binance +98.9505, fees 0.09395


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
        self.assert_money(-2.0989, 0.0, 0.0989)                                       # OLD (today's blind unwind) ends with the same cash: the unwind itself is the same


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
        self.assert_money(-199.99, 197.901, 0.089)                                    # OLD: -199.99 / +197.901, fees 0.089


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



class FailureCasesFromTheReportsNotInH01ToH40OrS1ToS19(Scenario):
    """Phase C review (8 Nov), item 2b. Cases found in the five source reports that neither the H01-H40 catalogue nor the S1-S19 scripts has, each as a core-path scenario:
      U1  an UNWIND that itself fills only partly (hedge_failure_study section 2: 'a partial unwind is never completed')
      U2  an unwind needed while the unwind venue's book is not valid (study section 2: 'silent skips')
      U3  a new package while an earlier hedge is UNKNOWN (deep dive: count_unknown_exposure, 'double exposure up to 2x')
      U4  every unwind order lost: attempts exhausted, the persistent alarm, no new risk, and the kill switch 30 s into the breach (deep dive: 'stuck unwind', O30 / O120)
      U5  the cancel loses the race: the venue answers the cancel with 'order does not exist' because the order filled, then the fill report arrives (lifecycle report: cancel found nothing)
      U6  a reject report after a maker fill (lifecycle report D-06)
    The old results are the reports' own words; the core's behaviour is asserted. One difference worth knowing: the core completes an unwind on the next venue in its priority list, so a
    partly filled unwind leaves the pair flat NET but not flat per venue (the backtester unwinds on the maker venue only)."""

    def residual(self, kill=True):
        from crypto_stat_arb.execution import ResidualPolicy
        return ResidualPolicy(max_residual_base=Decimal("0.002"), max_residual_age_ns=5 * S, kill_after_ns=30 * S if kill else None, hedge_venue_priority=("BYBIT", "BINANCE"))

    def second_package(self, oid="op-2"):
        opp = replace(opportunity("2.0"), opportunity_id=oid, buy_venue="BYBIT", sell_venue="BINANCE", buy_price=Decimal("100.0"), sell_price=Decimal("99.0"))
        return self.oms.execute(opp, self.books, maker_venue="BYBIT")

    def test_u1_a_partly_filled_unwind_is_completed_not_dropped(self):
        self.build(hedge=[("reject", "-2019")], residual=self.residual())
        self.bybit.script = [("ioc_partial", "1.2"), ("fill", None)]
        self.maker_fill("2.0", 5 * S, ExecutionEventType.FILLED)
        for k in range(1, 8):
            self.oms.advance_time(int((5 + 0.2 * k) * S))
        u = self.unwinds()
        self.assertEqual([(o.venue, o.quantity, o.filled_quantity) for o in u], [("BYBIT", Decimal("2.0"), Decimal("1.2")), ("BINANCE", Decimal("0.8"), Decimal("0.8"))])
        self.assertEqual(sum(o.filled_quantity for o in u), Decimal("2.0"))               # the whole 2.0 was unwound: the remainder was sent, not left open
        self.assertEqual(self.position("BYBIT") + self.position("BINANCE"), 0.0)           # net flat
        self.assertEqual(self.chain.state, H.RECONCILED)

    def test_u2_an_unwind_on_a_non_valid_book_is_recorded_as_skipped_and_goes_to_the_next_venue(self):
        self.build(hedge=[("reject", "-2019")], residual=self.residual())
        self.bybit.script = [("fill", None)]
        self.books[("BYBIT", INSTR)].start_resync("x")
        self.maker_fill("2.0", 5 * S, ExecutionEventType.FILLED)
        for k in range(1, 8):
            self.oms.advance_time(int((5 + 0.2 * k) * S))
        events = [e["event_type"] for e in self.ledger.events]
        self.assertIn("UNWIND_VENUE_SKIPPED", events)                                       # not silent
        self.assertEqual([(o.venue, o.filled_quantity) for o in self.unwinds()], [("BINANCE", Decimal("2.0"))])
        self.assertEqual(self.position("BYBIT") + self.position("BINANCE"), 0.0)

    def test_u3_no_new_package_while_a_hedge_is_unknown_and_one_is_admitted_after_the_answer(self):
        down = {"v": True}
        timeout = OrderTimeoutPolicy(report_timeout_ns=2 * S, query_interval_ns=1 * S, max_queries=6, kill_on_unresolved=True)
        self.build(hedge=["silent"], timeout=timeout, query=lambda oid: None if down["v"] else VenueOrderStatus(
            order_id=oid, venue="BINANCE", instrument_id=INSTR, state=ExecutionEventType.FILLED, cumulative_filled_quantity=Decimal("2.0"), average_price=Decimal("99.0")))
        self.maker_fill("2.0", 5 * S, ExecutionEventType.FILLED)
        self.oms.advance_time(int(7.5 * S))
        self.assertEqual(self.chain.state, H.UNKNOWN)
        sent_before = len(self.bybit.submits)
        blocked = self.second_package("op-2")
        self.assertEqual((blocked.terminal_outcome, len(self.bybit.submits)), ("REJECTED", sent_before))        # refused, nothing sent
        self.assertTrue(any(e["event_type"] == "NEW_RISK_HELD_HEDGE_UNKNOWN" for e in self.ledger.events))
        down["v"] = False
        self.oms.advance_time(int(10.5 * S))
        self.assertIn(self.chain.state, (H.HEDGED, H.RECONCILED))                              # the query found it executed; the plan has closed, so RECONCILED
        self.bybit.script = ["ack"]
        admitted = self.second_package("op-3")
        self.assertNotEqual(admitted.terminal_outcome, "REJECTED")                           # the venue answered: new risk is allowed again

    def test_u4_every_unwind_lost_ends_in_the_alarm_no_new_risk_and_the_kill_switch_30_s_into_the_breach(self):
        self.build(hedge=[("reject", "-2019")], residual=self.residual())
        self.bybit.script = [("reject", "-2019")] * 6
        self.binance.script = [("reject", "-2019")] * 6
        self.maker_fill("2.0", 5 * S, ExecutionEventType.FILLED)
        killed_at = None
        for k in range(1, 120):
            self.oms.advance_time(int((5 + 0.5 * k) * S))
            if self.oms.risk.kill_switch:
                killed_at = 5 + 0.5 * k
                break
        self.assertEqual(self.chain.state, H.FAILED)
        self.assertEqual(len(self.unwinds()), 3)                                              # the budget of three, then the alarm; nothing silent
        self.assertIn(self.plan.parent_id, self.oms.unwind_exhausted_alarms)
        self.assertEqual(self.second_package("op-2").terminal_outcome, "REJECTED")           # no new risk until an operator resets the alarm
        self.assertIsNotNone(killed_at)
        self.assertAlmostEqual(killed_at, 5.0 + 30.5, delta=1.0)                              # stage 2 of the residual limit: kill at 30 s after the breach began

    def test_u5_a_cancel_that_loses_the_race_to_a_fill_still_gets_its_hedge(self):
        self.build(hedge=[("fill", None), ("fill", None)])
        self.maker_fill("1.0", 5 * S)
        real = self.bybit.handle
        from crypto_stat_arb.execution import OrderAction
        from test_fill_triggered import cb

        def handle(req, book):
            if req.action == OrderAction.CANCEL:
                return (cb(req, ExecutionEventType.REJECTED, reason="EC_OrigClOrdIDDoesNotExist"),)       # the venue: no such open order, it filled
            return real(req, book)
        self.bybit.handle = handle
        self.oms.cancel_open_orders("ttl", 6 * S)
        self.assertEqual(self.maker.state, OrderState.CANCEL_PENDING)                          # a rejected cancel says nothing reliable about the order
        self.maker_fill("2.0", 6 * S + 1000, ExecutionEventType.FILLED)
        chains = list(self.oms.hedge_chains[self.plan.parent_id].values())
        self.assertEqual([c.exposure_qty for c in chains], [1.0, 1.0])
        self.assertEqual((self.position("BYBIT"), self.position("BINANCE")), (2.0, -2.0))      # the late fill was hedged
        self.assertTrue(all(c.state == H.RECONCILED for c in chains))

    def test_u6_a_reject_report_after_a_maker_fill_keeps_the_fill_hedges_it_and_alarms_once(self):
        self.build(hedge=[("fill", None)])
        self.maker_fill("1.0", 5 * S)
        self.n += 1
        self.oms.ingest_order_update(replace(maker_update(self.plan, self.maker, self.n, ExecutionEventType.REJECTED, "1.0", 6 * S), average_price=Decimal("100.0")))
        self.assertEqual(self.maker.state, OrderState.PARTIAL)                                 # the fill is kept
        self.assertEqual([(c.exposure_qty, c.state) for c in self.oms.hedge_chains[self.plan.parent_id].values()], [(1.0, H.HEDGED)])
        events = [e["event_type"] for e in self.ledger.events]
        self.assertEqual((events.count("REJECT_AFTER_FILL_ALARM"), events.count("REJECT_AFTER_FILL_REQUERIED")), (1, 1))


class _LotVenues:
    """Binance lot 0.1, Bybit 0.001: a part-lot residual is possible on the hedge venue."""


    def build(self, *a, **k):
        import test_core_hedge_scenarios as me
        real = me.scenario_instruments

        def lots():
            from crypto_stat_arb.instruments import InstrumentMaster
            from crypto_stat_arb.models import Instrument
            rows = {"BYBIT": ("-0.5", "5.5", "0.001"), "BINANCE": ("2.0", "5.0", "0.1")}
            return InstrumentMaster([Instrument(instrument_id=INSTR, venue=v, symbol="BTCUSDT", base="BTC", quote="USDT", tick_size=Decimal("0.10"), quantity_step=Decimal(rows[v][2]),
                                                min_quantity=Decimal(rows[v][2]), min_notional=Decimal("5"), maker_fee_bps=Decimal(rows[v][0]), taker_fee_bps=Decimal(rows[v][1]))
                                     for v in ("BINANCE", "BYBIT")])
        me.scenario_instruments = lots
        try:
            return super().build(*a, **k)
        finally:
            me.scenario_instruments = real

    def hedge_qty(self):
        return [float(o.quantity) for o in self.plan.children if o.order_role == "HEDGE"]

    def residual_events(self):
        return [e for e in self.ledger.events if e["event_type"] == "HEDGE_LOT_RESIDUAL"]


class HedgeLotRounding(_LotVenues, Scenario):
    """11 Nov (owner): the hedge is a whole number of the hedge venue's lots. Cumulative target floored to the lot; a part-lot residual is carried to the next maker fill and
    is on record while it is open; a quantity already on the lot behaves exactly as before. Binance lot 0.1 here (Bybit 0.001), so a maker fill of 0.25 hedges 0.2."""

    def test_a_part_lot_fill_hedges_the_whole_lots_and_records_the_residual(self):
        self.build(qty="1.0")
        self.maker_fill("0.25", 5 * S)
        self.assertEqual(self.hedge_qty(), [0.2])
        (ev,) = self.residual_events()
        self.assertEqual((ev["residual"], ev["hedged_target"], ev["lot"], ev["venue"]), ("0.05", "0.2", "0.1", "BINANCE"))
        self.assertEqual(self.oms.realism_counters["hedge_lot_residual"], 1)

    def test_the_residual_is_carried_to_the_next_fill_never_lost(self):
        self.build(qty="1.0")
        self.maker_fill("0.25", 5 * S)
        self.maker_fill("0.40", 6 * S)                       # cumulative 0.40 -> target 0.4: the next hedge is 0.2, the residual is gone
        self.assertEqual(self.hedge_qty(), [0.2, 0.2])
        self.assertEqual(len(self.residual_events()), 1)      # one event for the one residual that opened; closing it is silent
        self.maker_fill("0.43", 7 * S)                        # a new part-lot residual of 0.03
        self.assertEqual(self.hedge_qty(), [0.2, 0.2])         # below one lot: nothing to send
        self.assertEqual([e["residual"] for e in self.residual_events()], ["0.05", "0.03"])

    def test_a_fill_below_one_lot_sends_no_hedge_and_leaves_the_exposure_on_record(self):
        self.build(qty="1.0")
        self.maker_fill("0.06", 5 * S)
        self.assertEqual(self.hedge_qty(), [])
        self.assertEqual(len(self.oms.hedge_chains.get(self.plan.parent_id, {})), 0)
        self.assertEqual([e["residual"] for e in self.residual_events()], ["0.06"])

    def test_a_fill_already_on_the_lot_is_exactly_as_before(self):
        self.build(qty="1.0")
        self.maker_fill("0.30", 5 * S)
        self.assertEqual(self.hedge_qty(), [0.3])
        self.assertEqual(self.residual_events(), [])
        self.assertNotIn("hedge_lot_residual", self.oms.realism_counters)


class HedgeLotResidualLimits(_LotVenues, Scenario):
    """D1 (owner 11 Nov): a part-lot residual left by the lot rounding is real exposure. It counts in the plan, the residual snapshot and the venue positions; inside the approved bound the plan closes with
    it carried (no unwind: it is below every venue's minimum, nothing is sent, nothing alarms: the rule, as it stands, does not flatten it at the end of the day); a carried residual at the risk limit refuses
    the next package; above the OMS bound it is a limit breach that cannot be unwound (below the venue minimum), so the 5 s hold and the 30 s kill switch of the residual rule are what alarm."""

    def policy(self, cap, kill_s=30):
        from crypto_stat_arb.execution import ResidualPolicy
        return ResidualPolicy(max_residual_base=Decimal(cap), max_residual_age_ns=5 * S, hold_new_risk_on_breach=True, kill_after_ns=kill_s * S)

    def kinds(self):
        out = {}
        for e in self.ledger.events:
            out[e["event_type"]] = out.get(e["event_type"], 0) + 1
        return out

    def test_the_residual_counts_in_the_plan_the_snapshot_and_the_positions(self):
        self.build(qty="1.0", residual=self.policy("1.0"))
        self.maker_fill("0.25", 5 * S)
        self.assertEqual(self.hedge_qty(), [0.2])
        self.assertEqual(self.plan.residual_base, Decimal("0.05"))
        snap = self.oms.residual_risk_snapshot()
        self.assertEqual((snap["open_residual_plans"], snap["total_residual_base"]), (1, "0.05"))
        self.assertAlmostEqual(self.position("BYBIT") + self.position("BINANCE"), 0.05)           # long 0.25 on the maker venue, short 0.2 on the hedge venue

    def test_inside_the_bound_the_plan_closes_with_the_residual_carried_and_nothing_alarms(self):
        self.build(qty="1.0", residual=self.policy("1.0"))
        self.maker_fill("0.25", 5 * S)
        self.oms.cancel_open_orders("end of the quote", 8 * S)
        self.assertEqual(self.plan.state.value, "CLOSED")
        self.assertEqual(self.oms.carried_residual_base(), Decimal("0.05"))
        self.oms.advance_time(120 * S)                                                              # a long idle time to the end of the day
        kinds = self.kinds()
        self.assertEqual(self.oms.carried_residual_base(), Decimal("0.05"))
        self.assertEqual(self.oms.residual_risk_snapshot()["carried_residual_base"], "0.05")
        self.assertEqual((kinds.get("RESIDUAL_WITHIN_APPROVED_BOUND"), kinds.get("UNWIND_VENUE_SKIPPED"), kinds.get("RESIDUAL_LIMIT_BREACH")), (1, None, None))
        self.assertFalse(self.oms.risk.kill_switch)
        self.assertEqual(len(self.binance.submits), 1)                                              # only the one 0.2 hedge: no unwind, no sub-lot order

    def test_a_carried_residual_at_the_risk_limit_refuses_the_next_package(self):
        self.build(qty="1.0", residual=self.policy("1.0"))                                          # risk() limit max_residual_base = 0.002
        self.maker_fill("0.25", 5 * S)
        self.oms.cancel_open_orders("end of the quote", 8 * S)
        opp = replace(opportunity("1.0"), opportunity_id="op-2", buy_venue="BYBIT", sell_venue="BINANCE", buy_price=Decimal("100.0"), sell_price=Decimal("99.0"))
        plan2 = self.oms.execute(opp, self.books, maker_venue="BYBIT")
        self.assertEqual(plan2.terminal_outcome, "REJECTED")
        self.assertTrue(any(e["event_type"] == "PACKAGE_RISK_REJECTED" and e.get("reason") == "carried residual at limit" for e in self.ledger.events))

    def test_above_the_bound_it_cannot_be_unwound_so_the_hold_and_the_kill_switch_alarm(self):
        self.build(qty="1.0", residual=self.policy("0.04"))
        self.maker_fill("0.25", 5 * S)
        self.oms.cancel_open_orders("end of the quote", 8 * S)
        kinds = self.kinds()
        self.assertEqual(self.plan.state.value, "HEDGE_UNWIND")
        self.assertGreaterEqual(kinds.get("RESIDUAL_LIMIT_BREACH", 0), 1)
        skipped = {e["reason"] for e in self.ledger.events if e["event_type"] == "UNWIND_VENUE_SKIPPED"}
        self.assertEqual(skipped, {"residual_below_venue_minimum"})                                 # nothing is sent: it is below the venue minimum on both venues
        self.assertEqual(len(self.binance.submits), 1)
        self.oms.advance_time(20 * S)
        self.assertFalse(self.oms.risk.kill_switch)                                                 # the second stage waits for 30 s
        self.oms.advance_time(45 * S)
        self.assertTrue(self.oms.risk.kill_switch)
        self.assertEqual(self.oms.realism_counters["residual_breach_kill_escalations"], 1)


class _QuietBybit(Venue):
    """Bybit that does not answer the submit; a CANCEL is answered CANCELLED only when `cancel_answers`, else it stays in flight (the venue answers later, scripted by the test)."""
    cancel_answers = False

    def handle(self, req, book):
        from crypto_stat_arb.execution import OrderAction
        if req.action == OrderAction.CANCEL:
            self.cancels += 1
            return (cb(req, ExecutionEventType.CANCELLED),) if self.cancel_answers else ()
        self.submits.append(req)
        return ()


class CancelWhileSubmitted(Scenario):
    """D2 (owner 11 Nov, rule R5): a cancel that leaves while the order is SUBMITTED (SENT, no venue answer yet). The four outcomes are the venue's: the cancel wins (CANCELLED); the submit is refused (REJECTED, no
    longer an unexpected transition); a fill raced the cancel (booked once, the order ends FILLED and the hedge is sent once); a partial fill then the cancel (CANCELLED with the partial booked). Every cancel
    sent in SENT is counted (`cancel_while_submitted`) and logged (CANCEL_WHILE_SUBMITTED)."""

    def go(self, cancel_answers):
        self.ledger = Ledger()
        self.bybit = _QuietBybit("BYBIT", [], price="99.0")
        self.bybit.cancel_answers = cancel_answers
        self.binance = Venue("BINANCE", [("fill", None)], price="99.0")
        self.books = scenario_books()
        inst = scenario_instruments()
        self.oms = OMS(inst, risk(), self.ledger, router=StrategyDirectedRouter(inst, adapters={"BYBIT": self.bybit, "BINANCE": self.binance}), dispatch_policy=DispatchPolicy.FILL_TRIGGERED)
        opp = replace(opportunity("1.0"), buy_venue="BYBIT", sell_venue="BINANCE", buy_price=Decimal("100.0"), sell_price=Decimal("99.0"))
        self.plan = self.oms.execute(opp, self.books, maker_venue="BYBIT")
        self.maker = self.plan.children[0]
        self.n = 0
        self.assertEqual(self.maker.state, OrderState.SENT)

    def events(self, name):
        return [e for e in self.ledger.events if e["event_type"] == name]

    def update(self, state, cum, ts, n=None):
        self.n = n or self.n + 1
        self.oms.ingest_order_update(replace(maker_update(self.plan, self.maker, self.n, state, cum, ts), average_price=Decimal("100.0")))

    def test_the_cancel_wins(self):
        self.go(True)
        self.oms.cancel_open_orders("stale venue", 2 * S)
        self.assertEqual(self.maker.state, OrderState.CANCELLED)
        self.assertEqual((self.position("BYBIT"), self.position("BINANCE")), (0.0, 0.0))
        self.assertEqual(len(self.binance.submits), 0)
        self.assertEqual(self.oms.realism_counters["cancel_while_submitted"], 1)
        (ev,) = self.events("CANCEL_WHILE_SUBMITTED")
        self.assertEqual((ev["venue"], ev["order_id"]), ("BYBIT", self.maker.order_id))

    def test_the_submit_is_refused_after_the_cancel_left(self):
        self.go(False)
        self.oms.cancel_open_orders("stale venue", 2 * S)
        self.assertEqual(self.maker.state, OrderState.CANCEL_PENDING)
        self.update(ExecutionEventType.REJECTED, "0", 3 * S)
        self.assertEqual(self.maker.state, OrderState.REJECTED)
        self.assertEqual(self.events("ORDER_TRANSITION_UNEXPECTED"), [])
        self.assertEqual((self.position("BYBIT"), self.position("BINANCE")), (0.0, 0.0))
        self.assertEqual(self.oms.realism_counters["cancel_while_submitted"], 1)

    def test_a_fill_that_raced_the_cancel_is_booked_once_and_hedged_once(self):
        self.go(False)
        self.oms.cancel_open_orders("stale venue", 2 * S)
        self.update(ExecutionEventType.FILLED, "1.0", 3 * S, n=1)
        self.update(ExecutionEventType.FILLED, "1.0", 3 * S, n=1)                                   # the same report again: suppressed
        self.assertEqual(self.maker.state, OrderState.FILLED)
        self.assertEqual((self.position("BYBIT"), self.position("BINANCE")), (1.0, -1.0))
        self.assertEqual(len([e for e in self.ledger.events if e["event_type"] == "FILL" and e["venue"] == "BYBIT"]), 1)
        self.assertEqual(len(self.binance.submits), 1)
        self.assertEqual(len(self.events("ORDER_UPDATE_DUPLICATE_SUPPRESSED")), 1)
        self.assertEqual(self.events("ORDER_TRANSITION_UNEXPECTED"), [])
        with self.assertRaisesRegex(ValueError, "immutable"):                                       # a late report after the plan closed is recorded and refused, never applied
            self.update(ExecutionEventType.CANCELLED, "1.0", 4 * S)
        self.assertEqual(len(self.events("CLOSED_PLAN_UPDATE_REJECTED")), 1)
        self.assertEqual(self.maker.state, OrderState.FILLED)

    def test_a_partial_fill_then_the_cancel_ends_cancelled_with_the_partial_booked(self):
        self.go(False)
        self.update(ExecutionEventType.PARTIAL, "0.25", 2 * S)
        self.oms.cancel_open_orders("stale venue", 3 * S)
        self.update(ExecutionEventType.CANCELLED, "0.25", 4 * S)
        self.assertEqual(self.maker.state, OrderState.CANCELLED)
        self.assertEqual(self.maker.filled_quantity, Decimal("0.25"))
        self.assertEqual(self.position("BYBIT"), 0.25)
        self.assertEqual(len(self.binance.submits), 1)                                              # the partial was hedged once
        self.assertEqual(self.oms.realism_counters.get("cancel_while_submitted", 0), 0)             # the order was PARTIAL, not SUBMITTED, when the cancel left


class MakerLifetimeSilentFeed(unittest.TestCase):
    """E1 (owner 10/11 Nov): the maker lifetime is a timer, not a check on market events. With NO market event after the venue accepted the order, advancing time to the
    deadline (what `TradingSession.tick` does in a live idle period) cancels it, at the deadline, from the venue's acceptance time."""

    def _build(self, lifetime_s):
        self.ledger = Ledger()
        self.bybit = Venue("BYBIT", ["ack"], price="99.0")
        self.binance = Venue("BINANCE", [], price="99.0")
        inst = scenario_instruments()
        self.oms = OMS(inst, risk(), self.ledger, router=StrategyDirectedRouter(inst, adapters={"BYBIT": self.bybit, "BINANCE": self.binance}), dispatch_policy=DispatchPolicy.FILL_TRIGGERED)
        self.oms.order_lifetime_ns = int(lifetime_s * S)
        opp = replace(opportunity("2.0"), buy_venue="BYBIT", sell_venue="BINANCE", buy_price=Decimal("100.0"), sell_price=Decimal("99.0"))
        self.plan = self.oms.execute(opp, scenario_books(), maker_venue="BYBIT")
        self.maker = self.plan.children[0]
        return next(e["update"]["event_ts_ns"] for e in self.ledger.events if e["event_type"] == "ORDER_STATE_UPDATE_APPLIED" and e["order_id"] == self.maker.order_id
                    and e["update"]["state"] == "ACKNOWLEDGED")

    def expired(self):
        return [e for e in self.ledger.events if e["event_type"] == "ORDER_LIFETIME_EXPIRED"]

    def test_no_market_event_the_timer_alone_cancels_at_the_deadline(self):
        ack = self._build(15.0)
        self.assertEqual(self.oms.next_timer_ts(), ack + 15 * S)                       # the deadline is a timer the session can sleep until
        self.oms.advance_time(ack + 14 * S)
        self.assertEqual((self.expired(), self.bybit.cancels), ([], 0))                  # not before
        self.oms.advance_time(ack + 20 * S)                                              # one jump, no events in between
        (ev,) = self.expired()
        self.assertEqual((ev["ts_ns"], self.bybit.cancels), (ack + 15 * S, 1))           # stamped at its own deadline, not at the time of the jump
        self.assertEqual(self.maker.state, OrderState.CANCELLED)

    def test_off_when_the_lifetime_is_unset_and_not_after_a_fill(self):
        self.ledger = Ledger()
        ack = self._build(15.0)
        u = maker_update(self.plan, self.maker, 1, ExecutionEventType.FILLED, "2.0", ack + 5 * S)
        self.oms.ingest_order_update(replace(u, average_price=Decimal("100.0")))
        self.oms.advance_time(ack + 30 * S)
        self.assertEqual((self.expired(), self.bybit.cancels), ([], 0))                  # a filled order is not cancelled


if __name__ == "__main__":
    unittest.main()
