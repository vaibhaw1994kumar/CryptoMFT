"""Core OMS (src/crypto_stat_arb/oms.py): order-state reducer, ambiguous submissions, and the cancel sweep.

Brief state -> OMS mapping used in these tests
  PENDING_SUBMIT / SUBMITTED_UNACKNOWLEDGED -> OrderState.CREATED, plan DISPATCHED (a report deadline is armed)
  LIVE                                       -> OrderState.ACKNOWLEDGED
  PARTIALLY_FILLED                           -> OrderState.PARTIAL
  PENDING_CANCEL                             -> no state of its own: the order stays ACKNOWLEDGED/PARTIAL until the venue
                                                reports CANCELLED (a cancel request never sets CANCELLED by itself)
  REJECTED / CANCELLED / FILLED              -> OrderState.REJECTED / CANCELLED / FILLED
  UNKNOWN                                    -> OrderState.TIMED_OUT without venue/private-stream confirmation, or a
                                                CREATED order past its report deadline; the plan close is deferred
Already covered by older tests and not repeated here: idempotent private-stream partial fills, duplicate update ids,
terminal-claim conflicts, orphan updates, restart without duplicate dispatch, REST/private-stream convergence,
unanswered queries, parent idempotency, fills during the cancel window.
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from crypto_stat_arb.execution import (
    CanonicalOrderUpdate, ExecutionCallback, ExecutionEventType, OrderAction, OrderUpdateSource,
    StrategyDirectedRouter, VenueOrderStatus,
)
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import OrderState, PlanState
from crypto_stat_arb.oms import OMS
from crypto_stat_arb.plan_store import VenueOrderSnapshot

from test_oms import instrument_master, make_book, opportunity, risk

MS = 1_000_000
INSTRUMENT = "BTC-USDT-SPOT"
PRICE = Decimal("60000")


def books():
    return {
        ("BINANCE", INSTRUMENT): make_book("BINANCE", "59999.90", "60000.00"),
        ("BYBIT", INSTRUMENT): make_book("BYBIT", "60060.00", "60060.10"),
    }


def callback(request, kind, reason=""):
    return ExecutionCallback(
        schema_version="1.0.0", callback_id=f"{kind.value}-{request.request_id}", request_id=request.request_id,
        order_id=request.order_id, parent_opportunity_id=request.parent_opportunity_id,
        parent_order_id=request.parent_order_id, venue=request.venue, instrument_id=request.instrument_id,
        event_type=kind, event_ts_ns=request.created_ts_ns, reason=reason)


class RestingAdapter:
    """Acknowledges every submit (the order rests) and cancels on request; optionally raises on cancel."""

    def __init__(self, raise_on_cancel=False):
        self.raise_on_cancel = raise_on_cancel
        self.cancel_calls = 0

    def handle(self, request, book):
        if request.action == OrderAction.CANCEL:
            self.cancel_calls += 1
            if self.raise_on_cancel:
                raise ConnectionError("cancel socket reset")
            return (callback(request, ExecutionEventType.CANCELLED),)
        return (callback(request, ExecutionEventType.ACKNOWLEDGED),)


class DroppedSubmitAdapter:
    """submit() raises after the request may or may not have reached the venue. `venue_state` is what a later query says:
    None = the venue never saw it, FILLED = it executed, 'no-query' = the adapter cannot be queried at all."""

    def __init__(self, venue, venue_state="no-query"):
        self.venue = venue
        self.submits = 0
        self.queries = 0
        if venue_state != "no-query":
            self.query_order = self._query
        self.venue_state = venue_state

    def handle(self, request, book):
        self.submits += 1
        raise ConnectionError("socket reset after send")

    def _query(self, order_id):
        self.queries += 1
        executed = self.venue_state == ExecutionEventType.FILLED
        return VenueOrderStatus(
            order_id=order_id, venue=self.venue, instrument_id=INSTRUMENT, state=self.venue_state,
            cumulative_filled_quantity=Decimal("0.01") if executed else Decimal("0"),
            average_price=Decimal("60060") if executed else Decimal("0"), reason="" if executed else "order unknown to venue")


def update(plan, order, uid, state, cumulative, ts, seq, source=OrderUpdateSource.PRIVATE_STREAM):
    return CanonicalOrderUpdate(
        update_id=uid, source=source, parent_order_id=plan.parent_id, order_id=order.order_id, venue=order.venue,
        instrument_id=order.instrument_id, state=state, event_ts_ns=ts, observed_ts_ns=ts + 1, sequence=seq,
        cumulative_filled_quantity=Decimal(cumulative), average_price=PRICE)


def fills(ledger):
    return [e["fill_quantity"] for e in ledger.events if e["event_type"] == "FILL"]


class ReducerOrderingTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger()
        self.oms = OMS(instrument_master(), risk(), self.ledger,
                       router=StrategyDirectedRouter(instrument_master(), adapters={
                           "BINANCE": RestingAdapter(), "BYBIT": RestingAdapter()}))
        self.plan = self.oms.execute(opportunity(), books())
        self.order = self.plan.children[0]

    def test_a_fill_reported_after_the_cancel_is_kept_and_applied_exactly_once(self):
        o, plan = self.order, self.plan
        self.oms.ingest_order_update(update(plan, o, "cxl", ExecutionEventType.CANCELLED, "0", 20, 2))
        self.assertEqual((o.state, o.filled_quantity), (OrderState.CANCELLED, Decimal("0")))
        late = update(plan, o, "late-partial", ExecutionEventType.PARTIAL, "0.004", 15, 3)
        self.oms.ingest_order_update(late)
        self.oms.ingest_order_update(late)                                   # same update id
        self.oms.ingest_order_update(update(plan, o, "late-partial-b", ExecutionEventType.PARTIAL, "0.004", 15, 4))  # same cumulative
        self.assertEqual((o.state, o.filled_quantity), (OrderState.CANCELLED, Decimal("0.004")))   # fill kept, terminal state kept
        self.assertEqual(fills(self.ledger), [Decimal("0.004")])
        self.assertEqual(self.ledger.positions[("BINANCE", INSTRUMENT)], Decimal("0.004"))
        self.assertEqual(self.ledger.audit_order_fills(self.oms.plans), [])

    def test_an_acknowledgement_or_stale_partial_arriving_after_the_fill_cannot_regress_the_order(self):
        o, plan = self.order, self.plan
        self.oms.ingest_order_update(update(plan, o, "fill", ExecutionEventType.FILLED, "0.010", 30, 5))
        self.oms.ingest_order_update(update(plan, o, "late-ack", ExecutionEventType.ACKNOWLEDGED, "0", 10, 1))
        self.oms.ingest_order_update(update(plan, o, "stale-partial", ExecutionEventType.PARTIAL, "0.004", 20, 3))
        self.assertEqual((o.state, o.filled_quantity), (OrderState.FILLED, Decimal("0.010")))
        self.assertEqual(fills(self.ledger), [Decimal("0.010")])
        self.assertEqual(self.ledger.audit_order_fills(self.oms.plans), [])

    def test_cumulative_reports_are_converted_to_incremental_fills(self):
        o, plan = self.order, self.plan
        for uid, cum, seq in (("p1", "0.003", 1), ("p2", "0.007", 2), ("p3", "0.007", 3), ("p4", "0.010", 4)):
            state = ExecutionEventType.FILLED if cum == "0.010" else ExecutionEventType.PARTIAL
            self.oms.ingest_order_update(update(plan, o, uid, state, cum, 10 + seq, seq))
        self.assertEqual(fills(self.ledger), [Decimal("0.003"), Decimal("0.004"), Decimal("0.003")])
        self.assertEqual(self.ledger.positions[("BINANCE", INSTRUMENT)], Decimal("0.010"))


class AmbiguousSubmissionTests(unittest.TestCase):
    """The adapter raises while submitting the SECOND leg. The first leg (Binance) fills normally."""

    def build(self, venue_state="no-query"):
        self.adapter = DroppedSubmitAdapter("BYBIT", venue_state)
        self.ledger = Ledger()
        self.engine = risk()
        self.oms = OMS(instrument_master(), self.engine, self.ledger,
                       router=StrategyDirectedRouter(instrument_master(), adapters={"BYBIT": self.adapter}))
        self.opp = opportunity()
        with self.assertRaises(ConnectionError):
            self.oms.execute(self.opp, books())
        self.plan = next(iter(self.oms.plans.values()))
        self.buy, self.sell = self.plan.children[:2]

    def reservation_active(self):
        return [r.active for r in self.engine.reservations.values()]

    def test_an_exception_after_send_is_neither_a_rejection_nor_a_release(self):
        self.build()
        self.assertEqual((self.buy.state, self.sell.state), (OrderState.FILLED, OrderState.CREATED))
        self.assertEqual(self.reservation_active(), [True])
        self.assertIsNotNone(self.oms.next_timer_ts(), "the ambiguous order must be under a report deadline")
        self.assertEqual(self.oms.realism_counters["ambiguous_dispatch_errors"], 1)
        ev = [e for e in self.ledger.events if e["event_type"] == "DISPATCH_EXCEPTION_AMBIGUOUS"]
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["order_id"], ev[0]["action"], ev[0]["local_state"]), (self.sell.order_id, "SUBMIT", "CREATED"))
        self.assertIn("ConnectionError", ev[0]["error"])

    def test_retrying_the_same_opportunity_does_not_dispatch_again(self):
        self.build()
        submits = self.adapter.submits
        again = self.oms.execute(self.opp, books())
        self.assertIs(again, self.plan)
        self.assertEqual((len(self.oms.plans), self.adapter.submits), (1, submits))

    def test_unanswerable_venue_keeps_the_order_unresolved_trips_the_kill_switch_and_holds_the_reservation(self):
        self.build("no-query")
        self.oms.advance_time(120_000 * MS, books())
        self.assertEqual(self.sell.state, OrderState.TIMED_OUT)
        self.assertTrue(self.engine.kill_switch)
        self.assertNotEqual(self.plan.state, PlanState.CLOSED)
        self.assertEqual(self.reservation_active(), [True])
        self.assertTrue(any(e["event_type"] == "PLAN_CLOSE_DEFERRED" and e["code"] == "ORDER_TIMED_OUT_UNCONFIRMED"
                            for e in self.ledger.events))
        self.assertEqual(self.adapter.submits, 1, "the order is queried, never resent")
        # later authoritative evidence resolves it, and only then is the reservation released
        self.oms.reconcile_plan(self.plan.parent_id, {
            self.buy.order_id: VenueOrderSnapshot(self.buy.order_id, "BINANCE", INSTRUMENT, OrderState.FILLED, Decimal("0.01"), PRICE),
            self.sell.order_id: VenueOrderSnapshot(self.sell.order_id, "BYBIT", INSTRUMENT, OrderState.FILLED, Decimal("0.01"), Decimal("60060")),
        }, 130_000 * MS)
        self.assertEqual((self.sell.state, self.plan.state), (OrderState.FILLED, PlanState.CLOSED))
        self.assertEqual(self.reservation_active(), [False])
        self.assertEqual(self.ledger.positions[("BYBIT", INSTRUMENT)], Decimal("-0.01"))
        self.assertEqual(fills(self.ledger).count(Decimal("0.01")), 2)

    def test_when_the_venue_says_it_executed_the_fill_is_booked_once_and_nothing_is_resent(self):
        self.build(ExecutionEventType.FILLED)
        self.oms.advance_time(120_000 * MS, books())
        self.assertEqual((self.sell.state, self.sell.filled_quantity), (OrderState.FILLED, Decimal("0.01")))
        self.assertEqual(self.adapter.submits, 1)
        self.assertEqual(self.adapter.queries, 1)
        self.assertEqual(fills(self.ledger).count(Decimal("0.01")), 2)               # one per leg
        self.assertEqual(self.ledger.positions[("BINANCE", INSTRUMENT)] + self.ledger.positions[("BYBIT", INSTRUMENT)], 0)
        self.assertEqual(self.ledger.audit_order_fills(self.oms.plans), [])
        self.assertFalse(self.engine.kill_switch)
        self.assertEqual(self.reservation_active(), [False])

    def test_only_a_venue_answer_of_unknown_order_turns_the_ambiguity_into_a_rejection(self):
        self.build(None)
        self.assertEqual(self.sell.state, OrderState.CREATED)                        # not rejected on the exception alone
        self.oms.advance_time(120_000 * MS, books())
        queries = [e for e in self.ledger.events if e["event_type"] == "VENUE_ORDER_QUERY" and e["order_id"] == self.sell.order_id]
        self.assertTrue(queries and queries[0]["venue_state"] == "UNKNOWN_TO_VENUE")
        self.assertEqual(self.sell.state, OrderState.REJECTED)
        self.assertEqual(self.sell.filled_quantity, Decimal("0"))
        self.assertEqual(self.adapter.submits, 1)
        self.assertEqual(self.buy.filled_quantity, Decimal("0.01"))                  # the executed leg is kept and handled as residual
        self.assertEqual(fills(self.ledger)[0], Decimal("0.01"))


class ReconnectReconciliationTests(unittest.TestCase):
    """After a disconnect the local view may be stale. Recovery goes through reconcile_plan (REST-style snapshots)."""

    def setUp(self):
        self.ledger = Ledger()
        self.engine = risk()
        self.oms = OMS(instrument_master(), self.engine, self.ledger,
                       router=StrategyDirectedRouter(instrument_master(), adapters={
                           "BINANCE": RestingAdapter(), "BYBIT": RestingAdapter()}))
        self.plan = self.oms.execute(opportunity(), books())
        self.a, self.b = self.plan.children[:2]

    def snap(self, order, state, qty, ts=0):
        return VenueOrderSnapshot(order.order_id, order.venue, INSTRUMENT, state, Decimal(qty), PRICE, observed_ts_ns=ts)

    def reconcile(self, a_state, a_qty, b_state, b_qty, now, a_ts=10, b_ts=10):
        listing = {}
        if a_state is not None:
            listing[self.a.order_id] = self.snap(self.a, a_state, a_qty, a_ts)
        if b_state is not None:
            listing[self.b.order_id] = self.snap(self.b, b_state, b_qty, b_ts)
        return self.oms.reconcile_plan(self.plan.parent_id, listing, now)

    def reservation_active(self):
        return [r.active for r in self.engine.reservations.values()]

    def test_changes_that_happened_while_disconnected_are_recovered_and_booked_once(self):
        self.assertEqual((self.a.state, self.b.state), (OrderState.ACKNOWLEDGED, OrderState.ACKNOWLEDGED))
        self.reconcile(OrderState.PARTIAL, "0.004", OrderState.CANCELLED, "0", 100)
        self.assertEqual((self.a.state, self.a.filled_quantity, self.b.state), (OrderState.PARTIAL, Decimal("0.004"), OrderState.CANCELLED))
        self.assertEqual(self.ledger.positions[("BINANCE", INSTRUMENT)], Decimal("0.004"))
        for now in (110, 120, 130):                                       # the reconnect handler runs again and again
            self.reconcile(OrderState.PARTIAL, "0.004", OrderState.CANCELLED, "0", now)
        self.assertEqual(fills(self.ledger), [Decimal("0.004")])
        self.assertEqual(self.ledger.positions[("BINANCE", INSTRUMENT)], Decimal("0.004"))
        self.assertEqual(self.ledger.audit_order_fills(self.oms.plans), [])
        self.assertNotEqual(self.plan.state, PlanState.CLOSED)             # a is still working: nothing is released yet
        self.assertEqual(self.reservation_active(), [True])

    def test_a_stale_snapshot_never_lowers_a_recovered_fill(self):
        self.reconcile(OrderState.PARTIAL, "0.004", OrderState.CANCELLED, "0", 100, a_ts=10)
        self.reconcile(OrderState.PARTIAL, "0.002", OrderState.CANCELLED, "0", 110, a_ts=5)    # older view of the venue
        self.assertEqual((self.a.state, self.a.filled_quantity), (OrderState.PARTIAL, Decimal("0.004")))
        self.assertEqual(fills(self.ledger), [Decimal("0.004")])
        self.assertEqual(self.ledger.positions[("BINANCE", INSTRUMENT)], Decimal("0.004"))

    def test_a_later_snapshot_books_only_the_increment_and_the_plan_then_moves_to_unwind(self):
        self.reconcile(OrderState.PARTIAL, "0.004", OrderState.CANCELLED, "0", 100)
        self.reconcile(OrderState.FILLED, "0.010", OrderState.CANCELLED, "0", 140, a_ts=20)
        self.assertEqual(fills(self.ledger), [Decimal("0.004"), Decimal("0.006")])
        self.assertEqual((self.a.state, self.a.filled_quantity), (OrderState.FILLED, Decimal("0.010")))
        self.assertEqual(self.ledger.positions[("BINANCE", INSTRUMENT)], Decimal("0.010"))
        self.assertEqual(self.plan.state, PlanState.HEDGE_UNWIND)           # one leg filled, the other cancelled: residual
        self.assertEqual(self.plan.residual_base, Decimal("0.010"))
        self.assertEqual(self.reservation_active(), [True])                 # still held until the residual is dealt with

    def test_an_order_missing_from_the_venue_listing_is_not_assumed_cancelled(self):
        self.reconcile(OrderState.FILLED, "0.010", None, "0", 100)
        self.assertEqual((self.a.state, self.b.state), (OrderState.FILLED, OrderState.ACKNOWLEDGED))
        self.assertNotEqual(self.plan.state, PlanState.CLOSED)
        self.assertEqual(self.reservation_active(), [True])
        self.assertEqual(self.b.order_id in self.oms._unknown_state_children(self.plan, 100), True)  # still being chased
        # the venue answers on the next reconnect pass: only now is it resolved
        self.reconcile(OrderState.FILLED, "0.010", OrderState.FILLED, "0.010", 200, b_ts=15)
        self.assertEqual((self.b.state, self.plan.state), (OrderState.FILLED, PlanState.CLOSED))
        self.assertEqual(self.reservation_active(), [False])
        self.assertEqual(self.ledger.positions[("BINANCE", INSTRUMENT)] + self.ledger.positions[("BYBIT", INSTRUMENT)], 0)
        self.assertEqual(self.ledger.audit_order_fills(self.oms.plans), [])

    def test_extra_venue_orders_in_a_per_plan_reconcile_are_ignored_not_adopted(self):
        stranger = VenueOrderSnapshot("not-ours", "BINANCE", INSTRUMENT, OrderState.ACKNOWLEDGED, Decimal("0"), PRICE)
        self.oms.reconcile_plan(self.plan.parent_id, {self.a.order_id: self.snap(self.a, OrderState.ACKNOWLEDGED, "0"),
                                                       "not-ours": stranger}, 100)
        # extra venue orders are ignored by the per-plan path; the account-level path (reconcile_risk_state) owns orphans
        self.assertEqual((self.a.state, self.b.state), (OrderState.ACKNOWLEDGED, OrderState.ACKNOWLEDGED))
        self.assertFalse(self.engine.kill_switch)


class CancelSweepTests(unittest.TestCase):
    def test_one_venue_failing_to_cancel_does_not_stop_the_sweep(self):
        binance, bybit = RestingAdapter(raise_on_cancel=True), RestingAdapter()
        ledger = Ledger()
        oms = OMS(instrument_master(), risk(), ledger,
                  router=StrategyDirectedRouter(instrument_master(), adapters={"BINANCE": binance, "BYBIT": bybit}))
        plan = oms.execute(opportunity(), books())
        first, second = plan.children[:2]
        self.assertEqual((first.venue, first.state, second.state), ("BINANCE", OrderState.ACKNOWLEDGED, OrderState.ACKNOWLEDGED))
        cancelled = oms.cancel_open_orders("kill sweep", 1_000)
        self.assertEqual(cancelled, 1)
        self.assertEqual((first.state, second.state), (OrderState.ACKNOWLEDGED, OrderState.CANCELLED))
        self.assertEqual((binance.cancel_calls, bybit.cancel_calls), (1, 1))
        sweep = [e for e in ledger.events if e["event_type"] == "BOOK_INTEGRITY_CANCEL_ALL"]
        self.assertEqual(sweep[-1]["cancel_errors"], 1)
        # the order whose cancel failed is not treated as cancelled: it is queued for an immediate venue query
        self.assertIn(first.order_id, oms._requery_orders)
        self.assertEqual(oms.next_timer_ts(), 1_000)

    def test_a_clean_sweep_reports_no_errors(self):
        ledger = Ledger()
        oms = OMS(instrument_master(), risk(), ledger,
                  router=StrategyDirectedRouter(instrument_master(), adapters={"BINANCE": RestingAdapter(), "BYBIT": RestingAdapter()}))
        oms.execute(opportunity(), books())
        self.assertEqual(oms.cancel_open_orders("sweep", 1_000), 2)
        sweep = [e for e in ledger.events if e["event_type"] == "BOOK_INTEGRITY_CANCEL_ALL"]
        self.assertNotIn("cancel_errors", sweep[-1])


if __name__ == "__main__":
    unittest.main()
