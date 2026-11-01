"""What the 2026-11-01 local order-state design keeps on top of OMS02: the reject-after-fill alarm,
note_cancel_requested, and old ledgers that spell CANCEL_PENDING as CANCEL_REQUESTED."""

from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.execution import (
    CanonicalOrderUpdate, ExecutionEventType, OrderUpdateSource, StrategyDirectedRouter,
)
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import OrderState
from crypto_stat_arb.oms import OMS
from crypto_stat_arb.plan_store import ParentPlanStore, restore_child
from test_oms import instrument_master, make_book, opportunity, risk

INSTRUMENT = "BTC-USDT-SPOT"


class SilentAdapter:
    def handle(self, request, book):
        return ()


class Harness:
    """One sent order on silent venues; every venue answer is injected."""

    def __init__(self, plan_store=None):
        books = {("BINANCE", INSTRUMENT): make_book("BINANCE", "59999.90", "60000.00"),
                 ("BYBIT", INSTRUMENT): make_book("BYBIT", "60060.00", "60060.10")}
        self.ledger = Ledger()
        self.oms = OMS(instrument_master(), risk(), self.ledger, plan_store=plan_store, router=StrategyDirectedRouter(
            instrument_master(), adapters={"BINANCE": SilentAdapter(), "BYBIT": SilentAdapter()}))
        self.plan = self.oms.execute(opportunity(), books) if not self.oms.plans else None
        self.order = self.plan.children[0] if self.plan else None
        self.n = 0

    def venue(self, state, *, cum="0", ts=1_000):
        self.n += 1
        self.oms.ingest_order_update(CanonicalOrderUpdate(
            update_id=f"u{self.n}", source=OrderUpdateSource.PRIVATE_STREAM, parent_order_id=self.plan.parent_id,
            order_id=self.order.order_id, venue=self.order.venue, instrument_id=self.order.instrument_id,
            state=state, event_ts_ns=ts, observed_ts_ns=ts + self.n, sequence=self.n,
            cumulative_filled_quantity=Decimal(cum) * self.order.quantity, average_price=Decimal("60000")))

    def logged(self, kind):
        return [row for row in self.ledger.events if row["event_type"] == kind]


class RejectAfterFillAlarmTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def test_a_reject_after_a_partial_fill_alarms_once_and_still_requeries(self):
        h = self.h
        h.venue(ExecutionEventType.ACKNOWLEDGED)
        h.venue(ExecutionEventType.PARTIAL, cum="0.5")
        h.venue(ExecutionEventType.REJECTED, cum="0.5")
        h.venue(ExecutionEventType.REJECTED, cum="0.5")
        self.assertEqual(h.order.state, OrderState.PARTIAL)
        self.assertEqual(len(h.logged("REJECT_AFTER_FILL_REQUERIED")), 2)
        alarms = h.logged("REJECT_AFTER_FILL_ALARM")
        self.assertEqual(len(alarms), 1)
        self.assertEqual(Decimal(alarms[0]["filled_quantity"]), Decimal("0.5") * h.order.quantity)

    def test_a_reject_after_a_full_fill_alarms_without_a_requery(self):
        h = self.h
        h.venue(ExecutionEventType.FILLED, cum="1")
        h.venue(ExecutionEventType.REJECTED, cum="1")
        self.assertEqual(h.order.state, OrderState.FILLED)
        self.assertEqual(len(h.logged("REJECT_AFTER_FILL_ALARM")), 1)
        self.assertEqual(h.logged("REJECT_AFTER_FILL_REQUERIED"), [])

    def test_a_reject_with_no_fill_raises_no_alarm(self):
        self.h.venue(ExecutionEventType.REJECTED)
        self.assertEqual(self.h.order.state, OrderState.REJECTED)
        self.assertEqual(self.h.logged("REJECT_AFTER_FILL_ALARM"), [])

    def test_the_alarm_is_persisted_and_not_raised_again_after_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            store = ParentPlanStore(Path(d) / "plans.sqlite")
            h = Harness(store)
            h.venue(ExecutionEventType.PARTIAL, cum="0.5")
            h.venue(ExecutionEventType.REJECTED, cum="0.5")
            events = [e["event_type"] for e in store.events(h.plan.parent_id)]
            self.assertEqual(events.count("REJECT_AFTER_FILL_ALARM"), 1)
            store.close()
            store = ParentPlanStore(Path(d) / "plans.sqlite")
            recovered = OMS(instrument_master(), risk(), Ledger(), plan_store=store, router=StrategyDirectedRouter(
                instrument_master(), adapters={"BINANCE": SilentAdapter(), "BYBIT": SilentAdapter()}))
            self.assertIn(h.order.order_id, recovered._reject_after_fill_alarmed)
            store.close()


class NoteCancelRequestedTests(unittest.TestCase):
    def test_it_is_the_cancel_paths_internal_record_and_the_order_stays_live(self):
        h = Harness()
        h.venue(ExecutionEventType.ACKNOWLEDGED)
        h.oms.note_cancel_requested(h.order.order_id, 2_000)
        self.assertEqual(h.order.state, OrderState.CANCEL_PENDING)
        internal = [r for r in h.order.state_updates if r["source"] == OrderUpdateSource.INTERNAL.value]
        self.assertEqual(internal[-1]["state"], ExecutionEventType.CANCEL_REQUESTED.value)
        h.venue(ExecutionEventType.PARTIAL, cum="0.3")
        self.assertEqual((h.order.state, h.order.filled_quantity),
                         (OrderState.CANCEL_PENDING, Decimal("0.3") * h.order.quantity))
        h.venue(ExecutionEventType.CANCELLED, cum="0.3")
        self.assertEqual(h.order.state, OrderState.CANCELLED)

    def test_an_unknown_order_id_raises(self):
        with self.assertRaises(KeyError):
            Harness().oms.note_cancel_requested("no-such-order", 2_000)


class LegacyStateNameTests(unittest.TestCase):
    def test_the_old_cancel_requested_spelling_reads_as_cancel_pending(self):
        self.assertIs(OrderState("CANCEL_REQUESTED"), OrderState.CANCEL_PENDING)
        self.assertIs(OrderState("CANCEL_PENDING"), OrderState.CANCEL_PENDING)
        with self.assertRaises(ValueError):
            OrderState("NOT_A_STATE")

    def test_a_stored_child_with_the_old_spelling_restores(self):
        child = {"order_id": "o1", "parent_id": "p1", "opportunity_id": "x", "venue": "BINANCE",
                 "instrument_id": INSTRUMENT, "side": "BUY", "quantity": "1", "limit_price": "60000",
                 "state": "CANCEL_REQUESTED", "filled_quantity": "0", "average_price": "0", "fee": "0"}
        self.assertEqual(restore_child(child).state, OrderState.CANCEL_PENDING)


if __name__ == "__main__":
    unittest.main()
