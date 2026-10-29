"""OMS02: explicit SENT / working / CANCEL_PENDING order states, resolved from the facts."""

from decimal import Decimal
import unittest

from crypto_stat_arb.execution import (
    CanonicalOrderUpdate, ExecutionEventType, OrderUpdateSource, StrategyDirectedRouter,
)
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import OrderState, PlanState
from crypto_stat_arb.oms import LIVE_ORDER_STATES, OMS
from test_oms import instrument_master, make_book, opportunity, risk

INSTRUMENT = "BTC-USDT-SPOT"


class SilentAdapter:
    """Accepts every request and reports nothing: every venue answer is injected by the test."""

    def handle(self, request, book):
        return ()


class OrderStateTests(unittest.TestCase):
    def setUp(self):
        self.books = {("BINANCE", INSTRUMENT): make_book("BINANCE", "59999.90", "60000.00"),
                      ("BYBIT", INSTRUMENT): make_book("BYBIT", "60060.00", "60060.10")}
        self.ledger = Ledger()
        self.oms = OMS(instrument_master(), risk(), self.ledger, router=StrategyDirectedRouter(
            instrument_master(), adapters={"BINANCE": SilentAdapter(), "BYBIT": SilentAdapter()}))
        self.plan = self.oms.execute(opportunity(), self.books)
        self.order = self.plan.children[0]
        self.n = 0

    def venue(self, state, *, cum="0", source=OrderUpdateSource.PRIVATE_STREAM, ts=1_000):
        self.n += 1
        return self.oms.ingest_order_update(CanonicalOrderUpdate(
            update_id=f"u{self.n}", source=source, parent_order_id=self.plan.parent_id,
            order_id=self.order.order_id, venue=self.order.venue, instrument_id=self.order.instrument_id,
            state=state, event_ts_ns=ts, observed_ts_ns=ts + self.n, sequence=self.n,
            cumulative_filled_quantity=Decimal(cum) * self.order.quantity, average_price=Decimal("60000")))

    def cancel(self, ts=2_000):
        self.oms._cancel_working_orders(self.plan, [self.order], ts)

    def logged(self, kind):
        return [row for row in self.ledger.events if row["event_type"] == kind]

    # -- SENT / working --------------------------------------------------------------------------
    def test_a_dispatched_order_is_sent_until_the_venue_answers(self):
        self.assertEqual(self.order.state, OrderState.SENT)
        self.assertIn(OrderState.SENT, LIVE_ORDER_STATES)                  # counted as live for risk
        self.venue(ExecutionEventType.ACKNOWLEDGED)
        self.assertEqual(self.order.state, OrderState.ACKNOWLEDGED)

    # -- CANCEL_PENDING (D-05) -----------------------------------------------------------------
    def test_a_cancel_in_flight_is_pending_live_and_can_still_fill(self):
        self.venue(ExecutionEventType.ACKNOWLEDGED)
        self.oms._report_deadlines.pop(self.order.order_id, None)      # a quiet working order is not watched
        self.cancel()
        self.assertEqual(self.order.state, OrderState.CANCEL_PENDING)
        self.assertIn(OrderState.CANCEL_PENDING, LIVE_ORDER_STATES)
        self.assertIn(self.order.order_id, self.oms._report_deadlines)    # an unanswered cancel gets queried
        self.venue(ExecutionEventType.PARTIAL, cum="0.4")                  # fill executed before the cancel landed
        self.assertEqual((self.order.state, self.order.filled_quantity),
                         (OrderState.CANCEL_PENDING, Decimal("0.4") * self.order.quantity))
        self.venue(ExecutionEventType.CANCELLED, cum="0.4")
        self.assertEqual(self.order.state, OrderState.CANCELLED)

    def test_a_venue_query_after_the_cancel_ends_the_pending_state(self):
        self.venue(ExecutionEventType.ACKNOWLEDGED)
        self.cancel()
        # The cancel was refused (order unknown / too late); the query answers that it is still working.
        self.venue(ExecutionEventType.ACKNOWLEDGED, source=OrderUpdateSource.REST_QUERY, ts=3_000)
        self.assertEqual(self.order.state, OrderState.ACKNOWLEDGED)

    # -- timeouts (D-04) ------------------------------------------------------------------------
    def test_a_late_ack_clears_a_timeout_and_the_unwinding_plan_pulls_the_order(self):
        self.oms.mark_order_timed_out(self.plan.parent_id, self.order.order_id, 2_000)
        self.assertEqual((self.order.state, self.plan.state), (OrderState.TIMED_OUT, PlanState.HEDGE_UNWIND))
        self.venue(ExecutionEventType.ACKNOWLEDGED, ts=3_000)            # the venue had it all along
        self.assertEqual(self.order.state, OrderState.CANCEL_PENDING)    # live, and being pulled
        self.assertEqual(len(self.logged("LATE_EVIDENCE_AFTER_TIMEOUT")), 1)
        self.assertEqual(self.oms.realism_counters["late_evidence_after_timeout"], 1)
        self.venue(ExecutionEventType.PARTIAL, cum="0.5", ts=3_100)      # a fill that raced the cancel is kept
        self.assertEqual(self.order.filled_quantity, Decimal("0.5") * self.order.quantity)
        self.assertIn(self.order.state, LIVE_ORDER_STATES)

    def test_a_timeout_after_the_last_venue_report_still_gives_the_order_up(self):
        self.venue(ExecutionEventType.ACKNOWLEDGED, ts=1_000)            # an IOC acknowledged, then silence
        self.oms.mark_order_timed_out(self.plan.parent_id, self.order.order_id, 9_000)
        self.assertEqual(self.order.state, OrderState.TIMED_OUT)         # the query loop's last resort works

    # -- reject after fill (D-06) --------------------------------------------------------------
    def test_a_reject_after_a_fill_keeps_the_fill_and_queries_the_venue(self):
        self.venue(ExecutionEventType.ACKNOWLEDGED)
        self.venue(ExecutionEventType.PARTIAL, cum="0.4")
        self.venue(ExecutionEventType.REJECTED, cum="0")
        self.assertEqual(self.order.state, OrderState.PARTIAL)
        self.assertEqual(self.order.filled_quantity, Decimal("0.4") * self.order.quantity)
        self.assertEqual(len(self.logged("REJECT_AFTER_FILL_REQUERIED")), 1)
        self.assertIn(self.order.order_id, self.oms._requery_orders)

    def test_a_reject_with_no_fill_is_terminal(self):
        self.venue(ExecutionEventType.REJECTED)
        self.assertEqual(self.order.state, OrderState.REJECTED)

    # -- transitions ------------------------------------------------------------------------
    def test_an_unexpected_step_is_recorded_not_raised(self):
        self.venue(ExecutionEventType.ACKNOWLEDGED)
        self.venue(ExecutionEventType.REJECTED)                           # a reject of a working order
        self.assertEqual(self.order.state, OrderState.REJECTED)
        unexpected = self.logged("ORDER_TRANSITION_UNEXPECTED")
        self.assertEqual([(r["from_state"], r["to_state"]) for r in unexpected], [("ACKNOWLEDGED", "REJECTED")])
        self.assertEqual(self.oms.realism_counters["unexpected_order_transitions"], 1)

    def test_the_end_state_does_not_depend_on_arrival_order_for_terminal_facts(self):
        self.venue(ExecutionEventType.FILLED, cum="1")
        self.venue(ExecutionEventType.ACKNOWLEDGED)                       # an ack that overtook nothing
        self.assertEqual(self.order.state, OrderState.FILLED)


if __name__ == "__main__":
    unittest.main()
