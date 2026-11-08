"""Phase C: FILL_TRIGGERED in the core OMS, the hedge of a maker fill (oms.py, hedge_lifecycle.py). Scripted venues only: the rules are the core's, the venue just answers.

Maker leg on BYBIT (sell 0.01 at 60060), hedge on BINANCE (buy at 60000). Idempotency tests live here, in the normal tests tree: the native EMS module is not built."""
from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb.execution import (CanonicalOrderUpdate, DispatchPolicy, ExecutionCallback, ExecutionEventType, OrderAction, OrderUpdateSource,
                                       StrategyDirectedRouter, VenueOrderStatus)
from crypto_stat_arb.hedge_lifecycle import HedgePolicy, HedgeState as H
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import OrderState, PlanState
from crypto_stat_arb.oms import OMS
from crypto_stat_arb.orderbook import BookState
from crypto_stat_arb.plan_store import ParentPlanStore, VenueOrderSnapshot

from test_oms import instrument_master, make_book, opportunity, risk

MS = 1_000_000
S = 1_000_000_000
INSTR = "BTC-USDT-SPOT"


def cb(req, kind, cum="0", last="0", price="0", reason=""):
    return ExecutionCallback(schema_version="1.0.0", callback_id=f"{kind.value}-{req.request_id}", request_id=req.request_id, order_id=req.order_id,
                             parent_opportunity_id=req.parent_opportunity_id, parent_order_id=req.parent_order_id, venue=req.venue, instrument_id=req.instrument_id,
                             event_type=kind, event_ts_ns=req.created_ts_ns, cumulative_filled_quantity=Decimal(cum), last_fill_quantity=Decimal(last),
                             average_price=Decimal(price), reason=reason)


class Venue:
    """Answers SUBMITs from a script, then fills in full (an unwind, a later hedge). Behaviours: "ack", "silent" (no answer at all), ("fill", qty), ("ioc_partial", qty),
    "ioc_nofill", ("reject", reason). `status` is what a query answers: a VenueOrderStatus per order id, or None = unknown to the venue."""

    def __init__(self, venue, script=(), price="60000", query=None):
        self.venue, self.script, self.price = venue, list(script), price
        self.submits: list = []
        self.cancels = 0
        self.queries = 0
        self._query = query
        if query is not None:
            self.query_order = self._do_query

    def _do_query(self, order_id):
        self.queries += 1
        return self._query(order_id)

    def handle(self, req, book):
        if req.action == OrderAction.CANCEL:
            self.cancels += 1
            return (cb(req, ExecutionEventType.CANCELLED),)
        self.submits.append(req)
        what = self.script.pop(0) if self.script else ("fill", None)
        if what == "silent":
            return ()
        if what == "ack":
            return (cb(req, ExecutionEventType.ACKNOWLEDGED),)
        if what == "ioc_nofill":
            return (cb(req, ExecutionEventType.CANCELLED, reason="ioc"),)
        kind, arg = what if isinstance(what, tuple) else (what, None)
        if kind == "reject":
            return (cb(req, ExecutionEventType.REJECTED, reason=arg),)
        qty = Decimal(arg) if arg is not None else req.quantity
        if kind == "ioc_partial":
            return (cb(req, ExecutionEventType.PARTIAL, cum=str(qty), last=str(qty), price=self.price), cb(req, ExecutionEventType.CANCELLED, cum=str(qty), price=self.price))
        return (cb(req, ExecutionEventType.FILLED, cum=str(qty), last=str(qty), price=self.price),)


def books():
    return {("BINANCE", INSTR): make_book("BINANCE", "59999.90", "60000.00"), ("BYBIT", INSTR): make_book("BYBIT", "60060.00", "60060.10")}


def maker_update(plan, maker, n, state, cum, ts, uid=None):
    return CanonicalOrderUpdate(update_id=uid or f"mk-{n}", source=OrderUpdateSource.PRIVATE_STREAM, parent_order_id=plan.parent_id, order_id=maker.order_id,
                                venue=maker.venue, instrument_id=maker.instrument_id, state=state, event_ts_ns=ts, observed_ts_ns=ts + 1, sequence=n,
                                cumulative_filled_quantity=Decimal(cum), average_price=Decimal("60060"))


class Base(unittest.TestCase):
    def build(self, hedge=(), *, policy=None, query=None, store=None, qty="0.01", bybit=("ack",), binance_price="60000"):
        self.ledger = Ledger()
        self.bybit = Venue("BYBIT", bybit, price="60060")
        self.binance = Venue("BINANCE", hedge, price=binance_price, query=query)
        self.books = books()
        self.oms = OMS(instrument_master(), risk(), self.ledger,
                       router=StrategyDirectedRouter(instrument_master(), adapters={"BYBIT": self.bybit, "BINANCE": self.binance}),
                       dispatch_policy=DispatchPolicy.FILL_TRIGGERED, hedge_policy=policy, plan_store=store)
        self.plan = self.oms.execute(opportunity(qty), self.books, maker_venue="BYBIT")
        self.maker = self.plan.children[0]
        self.n = 0
        return self

    def fill(self, cum, state=ExecutionEventType.PARTIAL, ts=None, uid=None):
        self.n += 1
        self.oms.ingest_order_update(maker_update(self.plan, self.maker, self.n, state, cum, ts or (10 + self.n) * MS, uid))

    @property
    def chains(self):
        return list(self.oms.hedge_chains.get(self.plan.parent_id, {}).values())

    def hedge_children(self):
        return [o for o in self.plan.children if o.order_role == "HEDGE"]


class FillTriggeredBasics(Base):
    def test_execute_sends_only_the_maker_and_reserves_the_whole_package(self):
        self.build()
        self.assertEqual([o.venue for o in self.plan.children], ["BYBIT"])
        self.assertEqual((len(self.bybit.submits), len(self.binance.submits)), (1, 0))
        self.assertTrue(self.bybit.submits[0].post_only)                              # the maker rests: post-only, GTC
        self.assertEqual(self.bybit.submits[0].time_in_force.value, "GTC")
        self.assertEqual(self.chains, [])
        self.assertIn(self.plan.parent_id, self.oms._hedge_specs)
        reserved = [e for e in self.ledger.events if e["event_type"] == "PACKAGE_RISK_RESERVED"]
        self.assertEqual(len(reserved), 1)                                            # atomic: both legs, before the maker is sent

    def test_the_deterministic_policy_is_untouched(self):
        ledger = Ledger()
        a = {"BYBIT": Venue("BYBIT"), "BINANCE": Venue("BINANCE")}
        oms = OMS(instrument_master(), risk(), ledger, router=StrategyDirectedRouter(instrument_master(), adapters=a))
        plan = oms.execute(opportunity(), books())
        self.assertEqual(len(plan.children), 2)
        self.assertEqual(oms.dispatch_policy, DispatchPolicy.DETERMINISTIC_SEQUENTIAL)
        self.assertEqual(oms._hedge_specs, {})
        self.assertEqual(plan.state, PlanState.CLOSED)

    def test_a_maker_fill_is_hedged_for_the_filled_quantity_and_the_chain_reconciles(self):
        self.build(hedge=[("fill", None)])
        self.fill("0.01", ExecutionEventType.FILLED)
        (chain,) = self.chains
        (hedge,) = self.hedge_children()
        self.assertEqual((hedge.venue, hedge.quantity, hedge.state), ("BINANCE", Decimal("0.01"), OrderState.FILLED))
        self.assertEqual(chain.state, H.RECONCILED)
        self.assertEqual(chain.outcome, "hedged_reconciled")
        self.assertEqual(self.plan.state, PlanState.CLOSED)
        self.assertEqual(self.plan.residual_base, 0)
        submit = [e for e in self.ledger.events if e["event_type"] == "HEDGE_SUBMIT"]
        self.assertEqual([e["client_order_id"] for e in submit], [hedge.order_id])      # the idempotent client order id, recorded before the send
        self.assertEqual(self.binance.submits[0].idempotency_key, self.binance.submits[0].idempotency_key)
        self.assertIsNotNone(chain.residual_exposure_duration_s)

    def test_only_what_has_filled_is_hedged_each_fill_in_its_own_chain(self):
        self.build(hedge=[("fill", None), ("fill", None)])
        self.fill("0.004")
        self.assertEqual([c.exposure_qty for c in self.chains], [0.004])
        self.assertEqual([r.quantity for r in self.binance.submits], [Decimal("0.004")])
        self.fill("0.010", ExecutionEventType.FILLED)
        self.assertEqual([r.quantity for r in self.binance.submits], [Decimal("0.004"), Decimal("0.006")])
        self.assertEqual(sum(o.quantity for o in self.hedge_children()), Decimal("0.01"))      # never more than the maker size
        self.assertTrue(all(c.state == H.RECONCILED for c in self.chains))
        self.assertEqual(self.plan.state, PlanState.CLOSED)

    def test_a_second_partial_fill_while_a_hedge_is_in_flight_gets_its_own_chain_no_queue_no_netting(self):
        self.build(hedge=["ack", ("fill", None)])                                   # the first hedge is acknowledged and rests unfilled
        self.fill("0.004")
        c1 = self.chains[0]
        self.assertEqual(c1.state, H.SUBMITTED)
        self.fill("0.010", ExecutionEventType.FILLED)
        self.assertEqual(len(self.chains), 2)
        self.assertEqual(self.chains[1].state, H.HEDGED)                             # the second hedge did not wait for the first (it reconciles when the plan closes)
        self.assertEqual(c1.state, H.SUBMITTED)                                      # one live child per chain: the first is untouched
        live = [o for o in self.hedge_children() if o.state == OrderState.ACKNOWLEDGED]
        self.assertEqual(len(live), 1)

    def test_duplicate_submission_a_redelivered_maker_report_sends_no_second_hedge(self):
        self.build(hedge=[("fill", None)])
        self.fill("0.004", uid="same")
        self.fill("0.004", uid="same")                                               # the same report again
        self.fill("0.004", uid="other")                                              # a different report carrying the same cumulative quantity
        self.assertEqual(len(self.binance.submits), 1)
        self.assertEqual(len(self.hedge_children()), 1)
        again = self.oms.execute(opportunity(), self.books, maker_venue="BYBIT")     # the same opportunity again: the same plan, nothing new
        self.assertIs(again, self.plan)
        self.assertEqual((len(self.bybit.submits), len(self.binance.submits)), (1, 1))


class CarriedOverFromTheOpportunityTests(Base):
    """Gate 1 decision 4: the cases of platform_kit/test_opportunity_fill_triggered.py and test_opportunity_hardening.py that describe the hedge of a maker fill, carried over
    to the core OMS (opportunity.py itself is never copied). Cases of that package's own order store, fee currencies or message budget are not hedge rules and are not carried."""

    def test_maker_never_fills_no_hedge_order_is_ever_sent_and_the_package_is_released(self):
        self.build()
        self.oms.cancel_open_orders("test: the quote ages out", 20 * S)
        self.assertEqual(len(self.binance.submits), 0)
        self.assertEqual(self.chains, [])
        self.assertEqual(self.plan.state, PlanState.CLOSED)
        self.assertEqual(self.oms.risk.stats_snapshot()["active_reservations"], 0)

    def test_the_request_must_name_the_maker_leg(self):
        self.build()
        with self.assertRaises(ValueError):
            self.oms.execute(replace(opportunity("0.02"), opportunity_id="op-2"), self.books, maker_venue=None)
        with self.assertRaises(ValueError):
            self.oms.execute(replace(opportunity("0.03"), opportunity_id="op-3"), self.books, maker_venue="KRAKEN")

    def test_a_maker_dispatch_that_fails_ambiguously_sends_no_hedge_and_keeps_the_reservation(self):
        self.ledger = Ledger()
        raising = Venue("BYBIT")
        raising.handle = lambda req, book: (_ for _ in ()).throw(ConnectionError("socket reset after send"))
        binance = Venue("BINANCE")
        oms = OMS(instrument_master(), risk(), self.ledger, router=StrategyDirectedRouter(instrument_master(), adapters={"BYBIT": raising, "BINANCE": binance}),
                  dispatch_policy=DispatchPolicy.FILL_TRIGGERED)
        with self.assertRaises(ConnectionError):
            oms.execute(opportunity(), books(), maker_venue="BYBIT")
        self.assertEqual(binance.submits, [])                                         # the other leg is not orphaned: it was never created
        self.assertEqual(oms.risk.stats_snapshot()["active_reservations"], 1)         # the maker may exist at the venue: the reservation is kept, nothing is resent

    def test_the_kill_switch_stops_new_risk_but_never_the_hedge_of_a_fill_that_already_happened(self):
        self.build(hedge=[("fill", None)])
        self.oms.risk.activate_kill_switch(now_ns=5 * S, reason="test", source="TEST", ledger=self.ledger)
        self.fill("0.01", ExecutionEventType.FILLED, ts=6 * S)
        self.assertEqual(len(self.binance.submits), 1)
        self.assertEqual(self.chains[0].state, H.RECONCILED)

    def test_a_resumed_plan_never_submits_a_second_order_for_a_quantity_already_hedged(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "plans.sqlite"
            store = ParentPlanStore(path)
            self.build(hedge=[("fill", None), ("fill", None)], store=store)
            self.fill("0.004")
            store.close()
            store2 = ParentPlanStore(path)
            binance2 = Venue("BINANCE", [("fill", None)])
            oms2 = OMS(instrument_master(), risk(), Ledger(), router=StrategyDirectedRouter(instrument_master(), adapters={"BYBIT": Venue("BYBIT"), "BINANCE": binance2}),
                       dispatch_policy=DispatchPolicy.FILL_TRIGGERED, plan_store=store2)
            oms2._books.update(books())
            self.assertEqual(oms2._hedge_specs[self.plan.parent_id]["requested_qty"], Decimal("0.004"))
            maker = next(o for o in oms2.plans[self.plan.parent_id].children if o.order_id == self.maker.order_id)
            oms2.ingest_order_update(maker_update(oms2.plans[self.plan.parent_id], maker, 1, ExecutionEventType.PARTIAL, "0.004", 50 * MS, "after-restart"))   # the same 0.004 again
            self.assertEqual(binance2.submits, [])                                    # already hedged before the restart: nothing is sent again
            oms2.ingest_order_update(maker_update(oms2.plans[self.plan.parent_id], maker, 2, ExecutionEventType.FILLED, "0.010", 60 * MS))
            self.assertEqual([r.quantity for r in binance2.submits], [Decimal("0.006")])   # only the new 0.006
            store2.close()


class RejectsRetryAndUnwind(Base):
    def test_repeated_rejection_is_retried_within_the_budget_and_the_intervals_are_recorded(self):
        self.build(hedge=[("reject", "-1003"), ("reject", "-1003"), ("fill", None)], policy=HedgePolicy(retry_limit=2, retry_backoff_ns=250 * MS))
        self.fill("0.01", ExecutionEventType.FILLED, ts=1 * S)
        (chain,) = self.chains
        self.assertEqual((chain.state, chain.retries), (H.RETRYING, 1))               # first reject: classified rate_limit, retry permitted, waiting for the backoff
        self.assertEqual(len(self.binance.submits), 1)
        self.oms.advance_time(1 * S + 300 * MS)
        self.oms.advance_time(1 * S + 600 * MS)
        self.assertEqual(len(self.binance.submits), 3)
        ids = [h.order_id for h in self.hedge_children()]
        self.assertEqual(len(set(ids)), 3)                                            # every attempt has its own client order id
        self.assertEqual(chain.state, H.RECONCILED)
        self.assertEqual(chain.reject_labels, ["rate_limit", "rate_limit"])
        self.assertTrue(all(0.24 < x < 0.26 for x in chain.retry_intervals_s), chain.retry_intervals_s)
        self.assertEqual([r["retry_permitted"] for r in self.oms.reject_records], [True, True])

    def test_a_reject_that_does_not_permit_a_retry_goes_to_the_unwind_path(self):
        self.build(hedge=[("reject", "-2019")], policy=HedgePolicy(retry_limit=5))
        self.fill("0.01", ExecutionEventType.FILLED, ts=1 * S)
        (chain,) = self.chains
        self.assertEqual(len(self.hedge_children()), 1)                               # margin refusal: the same hedge request is never sent again
        self.assertIn(chain.state, (H.UNWINDING, H.RECONCILED, H.FAILED))
        self.assertEqual(chain.reject_labels, ["margin_insufficient"])
        self.assertEqual(chain.retries, 0)
        self.assertTrue(any(h["to"] == "UNWINDING" and "not retryable" in h["reason"] for h in chain.events))

    def test_retry_exhaustion_unwinds_after_the_budget_and_never_resends_beyond_it(self):
        self.build(hedge=[("reject", "-1003")] * 3, policy=HedgePolicy(retry_limit=1, retry_backoff_ns=100 * MS))
        self.fill("0.01", ExecutionEventType.FILLED, ts=1 * S)
        self.oms.advance_time(1 * S + 200 * MS)
        (chain,) = self.chains
        hedges = [r for r in self.binance.submits if r.side.value == "BUY"]
        self.assertEqual(len(hedges[:2]), 2)
        self.assertEqual(chain.retries, 1)
        self.assertTrue(any("retry budget 1 used" in e["reason"] for e in chain.events))
        self.assertEqual(chain.reject_labels, ["rate_limit", "rate_limit"])

    def test_an_ioc_that_finds_nothing_is_a_retryable_engine_refusal(self):
        self.build(hedge=["ioc_nofill", ("fill", None)], policy=HedgePolicy(retry_limit=1, retry_backoff_ns=50 * MS))
        self.fill("0.01", ExecutionEventType.FILLED, ts=1 * S)
        self.oms.advance_time(1 * S + 100 * MS)
        (chain,) = self.chains
        self.assertEqual(chain.reject_labels, ["ioc_unfilled"])
        self.assertEqual(chain.state, H.RECONCILED)

    def test_a_partly_filled_hedge_retries_only_the_remainder(self):
        self.build(hedge=[("ioc_partial", "0.004"), ("fill", None)], policy=HedgePolicy(retry_limit=1, retry_backoff_ns=50 * MS))
        self.fill("0.01", ExecutionEventType.FILLED, ts=1 * S)
        self.oms.advance_time(1 * S + 100 * MS)
        (chain,) = self.chains
        self.assertEqual([r.quantity for r in self.binance.submits], [Decimal("0.01"), Decimal("0.006")])      # the retry covers the unfilled remainder only
        self.assertEqual((chain.partial_fills, chain.state), (1, H.RECONCILED))


class UnknownAndQuery(Base):
    def test_lost_acknowledgement_is_resolved_by_a_query_and_the_hedge_is_never_resent(self):
        def answer(order_id):
            return VenueOrderStatus(order_id=order_id, venue="BINANCE", instrument_id=INSTR, state=ExecutionEventType.FILLED,
                                    cumulative_filled_quantity=Decimal("0.01"), average_price=Decimal("60000"))
        self.build(hedge=["silent"], query=answer)
        self.fill("0.01", ExecutionEventType.FILLED, ts=1 * S)
        (chain,) = self.chains
        self.assertEqual(chain.state, H.SUBMITTED)                                    # nothing heard: not assumed failed, not resent
        self.oms.advance_time(1 * S + 130 * S)
        self.assertEqual(self.binance.queries >= 1, True)
        self.assertEqual(len(self.binance.submits), 1)
        self.assertEqual(chain.state, H.RECONCILED)
        self.assertGreaterEqual(chain.timeouts, 1)
        self.assertEqual(chain.hedged_qty, 0.01)

    def test_unknown_to_the_venue_allows_exactly_one_replacement_after_the_query(self):
        self.build(hedge=["silent", ("fill", None)], query=lambda oid: VenueOrderStatus(order_id=oid, venue="BINANCE", instrument_id=INSTR, state=None, reason="order unknown to venue"), policy=HedgePolicy(retry_limit=1))
        self.fill("0.01", ExecutionEventType.FILLED, ts=1 * S)
        self.oms.advance_time(1 * S + 130 * S)
        (chain,) = self.chains
        self.assertEqual(len(self.binance.submits), 2)
        self.assertEqual(chain.state, H.RECONCILED)
        queries = [e for e in self.ledger.events if e["event_type"] == "VENUE_ORDER_QUERY"]
        first_replacement = next(e for e in self.ledger.events if e["event_type"] == "HEDGE_SUBMIT" and e["attempt"] == 1)
        self.assertTrue(queries and queries[0]["ts_ns"] <= first_replacement["ts_ns"])    # the query came first

    def test_status_queries_that_keep_failing_end_in_the_alarm_and_the_kill_switch_h16(self):
        self.build(hedge=["silent"], query=None)
        self.fill("0.01", ExecutionEventType.FILLED, ts=1 * S)
        self.oms.advance_time(1 * S + 600 * S)
        (chain,) = self.chains
        self.assertEqual(chain.state, H.FAILED)
        self.assertTrue(self.oms.risk.kill_switch)
        self.assertIn(self.plan.parent_id, self.oms.unwind_exhausted_alarms)
        self.assertTrue(any(e["event_type"] == "HEDGE_ALARM" for e in self.ledger.events))
        self.assertEqual(len(self.binance.submits), 1)

    def test_a_delayed_hedge_report_breaches_the_residual_age_and_escalates(self):
        live = lambda oid: VenueOrderStatus(order_id=oid, venue="BINANCE", instrument_id=INSTR, state=ExecutionEventType.ACKNOWLEDGED)
        self.build(hedge=["ack"], query=live)                                        # the venue answers every query: the hedge is live and not yet filled
        self.fill("0.01", ExecutionEventType.FILLED, ts=1 * S)
        (chain,) = self.chains
        self.assertEqual(chain.state, H.SUBMITTED)
        self.oms.advance_time(1 * S + 6 * S)                                         # the default residual age limit is 5 s
        self.assertIn(chain.state, (H.ESCALATED, H.UNWINDING, H.RECONCILED, H.FAILED))
        self.assertTrue(any(e["to"] == "ESCALATED" for e in chain.events))
        self.assertEqual(self.oms.realism_counters["hedge_residual_age_breaches"], 1)


class BadBook(Base):
    def test_a_hedge_on_a_book_that_is_not_valid_waits_and_is_sent_when_the_book_is_valid_again(self):
        self.build(hedge=[("fill", None)])
        hedge_book = self.books[("BINANCE", INSTR)]
        hedge_book.start_resync("test resync")                                        # the hedge venue's book is not valid when the maker fills
        self.assertNotEqual(hedge_book.state, BookState.VALID)
        self.fill("0.01", ExecutionEventType.FILLED, ts=1 * S)
        (chain,) = self.chains
        self.assertEqual((chain.state, len(self.binance.submits)), (H.HEDGE_PENDING, 0))      # held, not sent blind, not dropped
        self.assertEqual(self.oms.realism_counters["hedge_dispatch_held"], 1)
        fresh = make_book("BINANCE", "59999.90", "60000.00")
        self.books[("BINANCE", INSTR)] = fresh
        self.oms._books[("BINANCE", INSTR)] = fresh
        self.oms.on_book_update("BINANCE", INSTR, 1 * S + 500 * MS)
        self.assertEqual(len(self.binance.submits), 1)
        self.assertEqual(chain.state, H.RECONCILED)

    def test_a_book_that_never_recovers_ends_in_the_escalation_after_the_age_limit(self):
        self.build(hedge=[("fill", None)])
        self.books[("BINANCE", INSTR)].start_resync("test resync")
        self.fill("0.01", ExecutionEventType.FILLED, ts=1 * S)
        (chain,) = self.chains
        self.oms.advance_time(1 * S + 6 * S)
        self.assertTrue(any(e["to"] == "ESCALATED" for e in chain.events))
        self.assertEqual(len(self.binance.submits), 0)                                # still nothing sent blind


class RecoveryAfterRestart(Base):
    def test_a_restart_with_a_hedge_awaiting_its_answer_never_dispatches_a_second_hedge(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "plans.sqlite"
            store = ParentPlanStore(path)
            self.build(hedge=["silent"], store=store)
            self.fill("0.01", ExecutionEventType.FILLED, ts=1 * S)
            (chain,) = self.chains
            hedge = self.hedge_children()[0]
            self.assertEqual(chain.state, H.SUBMITTED)
            parent_id = self.plan.parent_id
            store.close()

            store2 = ParentPlanStore(path)                                            # the process restarts
            binance2 = Venue("BINANCE", [])
            oms2 = OMS(instrument_master(), risk(), Ledger(), router=StrategyDirectedRouter(instrument_master(), adapters={"BYBIT": Venue("BYBIT"), "BINANCE": binance2}),
                       dispatch_policy=DispatchPolicy.FILL_TRIGGERED, plan_store=store2)
            chain2 = oms2.hedge_chains[parent_id][chain.chain_id]
            self.assertEqual((chain2.state, chain2.exposure_qty), (H.SUBMITTED, 0.01))
            self.assertEqual(oms2._chain_children[chain.chain_id], [hedge.order_id])
            oms2.books_seed = None
            oms2._books.update(books())
            recovered = oms2.reconcile_plan(parent_id, {
                hedge.order_id: VenueOrderSnapshot(order_id=hedge.order_id, venue="BINANCE", instrument_id=INSTR, state=OrderState.FILLED, filled_quantity=Decimal("0.01"),
                                                   average_price=Decimal("60000"), observed_ts_ns=2 * S),
                self.maker.order_id: VenueOrderSnapshot(order_id=self.maker.order_id, venue="BYBIT", instrument_id=INSTR, state=OrderState.FILLED, filled_quantity=Decimal("0.01"),
                                                        average_price=Decimal("60060"), observed_ts_ns=2 * S)}, 2 * S)
            self.assertEqual(len(binance2.submits), 0)                                # nothing was sent again
            self.assertEqual(chain2.state, H.RECONCILED)
            self.assertEqual(recovered.residual_base, 0)
            store2.close()



def book_at(venue, bid, ask, ts_ns):
    from crypto_stat_arb.models import MarketEvent
    from crypto_stat_arb.orderbook import OrderBook
    b = OrderBook(venue, INSTR)
    b.apply(MarketEvent.from_dict({"event_type": "BOOK_SNAPSHOT", "venue": venue, "instrument_id": INSTR, "exchange_ts_ns": ts_ns, "receive_ts_ns": ts_ns, "sequence": 1,
                                   "bids": [[bid, "1"]], "asks": [[ask, "1"]]}))
    return b


class BinanceFeedStall(Base):
    """The Binance feed goes quiet for 1.6 s just after the Bybit maker fills (the scenario the owner named). What the hedge does depends on one setting, the book age limit,
    which is DECISION_PENDING_CALIBRATION: with no limit the hedge goes on the last valid book at once; with a limit below the stall it waits for the next update."""

    def stalled(self, policy):
        self.build(hedge=[("fill", None)], policy=policy)
        t0 = 100 * MS
        fresh = book_at("BINANCE", "59999.90", "60000.00", t0)               # the last Binance update before the stall
        self.books[("BINANCE", INSTR)] = fresh
        self.oms._books[("BINANCE", INSTR)] = fresh
        return t0

    def test_with_no_age_limit_the_hedge_goes_out_on_the_last_valid_book(self):
        t0 = self.stalled(HedgePolicy())
        self.fill("0.01", ExecutionEventType.FILLED, ts=t0 + 50 * MS)
        self.assertEqual(len(self.binance.submits), 1)

    def test_with_an_age_limit_below_the_stall_the_hedge_waits_for_the_next_update(self):
        t0 = self.stalled(HedgePolicy(max_book_age_ns=1 * S))
        self.fill("0.01", ExecutionEventType.FILLED, ts=t0 + 1_600 * MS)      # the maker fill is reported 1.6 s into the stall
        (chain,) = self.chains
        self.assertEqual((chain.state, len(self.binance.submits)), (H.HEDGE_PENDING, 0))
        self.assertEqual(self.oms._hedge_held[chain.chain_id], "hedge_book_stale")
        fresh = book_at("BINANCE", "59999.90", "60000.00", t0 + 1_650 * MS)   # the feed is back
        self.oms._books[("BINANCE", INSTR)] = fresh
        self.oms.on_book_update("BINANCE", INSTR, t0 + 1_660 * MS)
        self.assertEqual(len(self.binance.submits), 1)
        self.assertEqual(chain.state, H.RECONCILED)

    def test_a_stall_longer_than_the_residual_age_limit_ends_in_the_escalation(self):
        t0 = self.stalled(HedgePolicy(max_book_age_ns=1 * S))
        self.fill("0.01", ExecutionEventType.FILLED, ts=t0 + 1_600 * MS)
        (chain,) = self.chains
        self.oms.advance_time(t0 + 1_600 * MS + 6 * S)
        self.assertTrue(any(e["to"] == "ESCALATED" for e in chain.events))
        self.assertEqual(len(self.hedge_children()), 0)                        # no hedge was ever sent on a book it could not trust


class SkippedRiskCheckMutation(Base):
    """Phase C mutation check (the risk check was not in the Phase B shadow). With the pre-trade check intact a package whose books are stale is refused and nothing is sent;
    with the check mutated away the same package is sent. The test fails if the mutant survives, i.e. if the stale package were refused by something else."""

    def run_package(self, mutate):
        from types import SimpleNamespace
        from dataclasses import replace as dc_replace
        from crypto_stat_arb.risk import PreTradeDecision
        ledger = Ledger()
        bybit, binance = Venue("BYBIT", ["ack"]), Venue("BINANCE")
        oms = OMS(instrument_master(), risk(), ledger, router=StrategyDirectedRouter(instrument_master(), adapters={"BYBIT": bybit, "BINANCE": binance}),
                  dispatch_policy=DispatchPolicy.FILL_TRIGGERED)
        if mutate:
            oms.risk.validate_pretrade = lambda **kw: SimpleNamespace(decision=PreTradeDecision.APPROVED, failures=(), validation_id="mutant", to_dict=lambda: {})
        stale = dc_replace(opportunity(), observed_ts_ns=5 * S)               # the books were received at 1 ns: 5 s old, the limit is 1 s
        plan = oms.execute(stale, books(), maker_venue="BYBIT")
        return plan, bybit, ledger

    def test_the_real_check_refuses_the_stale_package_and_the_mutant_does_not(self):
        plan, bybit, ledger = self.run_package(mutate=False)
        self.assertEqual((plan.terminal_outcome, len(bybit.submits)), (PlanState.REJECTED.value, 0))
        self.assertTrue(any(e["event_type"] == "PRE_TRADE_REJECTED" for e in ledger.events))
        mplan, mbybit, _ = self.run_package(mutate=True)
        self.assertEqual(len(mbybit.submits), 1, "the mutant must be caught: without the risk check the stale package is sent")
        self.assertNotEqual(mplan.terminal_outcome, PlanState.REJECTED.value)


class ReportAndSettings(Base):
    def test_the_report_has_one_value_per_decision_pending_setting_and_the_per_chain_figures(self):
        self.build(hedge=[("reject", "-1003"), ("fill", None)], policy=HedgePolicy(retry_limit=1, retry_backoff_ns=100 * MS))
        self.fill("0.01", ExecutionEventType.FILLED, ts=1 * S)
        self.oms.advance_time(1 * S + 200 * MS)
        rep = self.oms.hedge_report()
        for name in ("unwind_price_slippage_bps", "max_unwind_attempts", "max_residual_age_ns", "kill_after_ns", "retry_limit", "unknown_query_budget"):
            self.assertEqual(rep["settings"][name]["label"], "DECISION_PENDING_CALIBRATION", name)
        self.assertEqual(rep["settings"]["max_unwind_attempts"]["core"], 3)
        (c,) = rep["chains"]
        self.assertEqual((c["retries"], c["reject_labels"], c["outcome"]), (1, ["rate_limit"], "hedged_reconciled"))
        self.assertEqual(len(c["retry_intervals_s"]), 1)
        self.assertIsNotNone(c["residual_exposure_duration_s"])
        self.assertEqual(rep["p_fail_and_loss_fail_bps"].split(" ")[0], "SCENARIO")

    def test_the_core_config_copies_agree_on_the_unwind_attempt_budget(self):
        import json
        root = Path(__file__).resolve().parents[1]
        names = ("src/crypto_stat_arb/config/research.json", "src/crypto_stat_arb/config/perp_research.json", "src/crypto_stat_arb/config/paper_watch.json",
                 "config/research.json", "config/perp_research.json", "config/paper_watch.json")
        values = {p: json.load(open(root / p))["oms"]["residual"]["max_unwind_attempts"] for p in names}
        self.assertEqual(set(values.values()), {3}, values)


if __name__ == "__main__":
    unittest.main()
