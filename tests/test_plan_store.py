from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.execution import (
    CanonicalOrderUpdate,
    ExecutionEventType,
    OrderUpdateSource,
    StrategyDirectedRouter,
)
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import Instrument, MarketEvent, Opportunity, OrderState, PlanState
from crypto_stat_arb.instruments import InstrumentMaster
from crypto_stat_arb.oms import OMS
from crypto_stat_arb.orderbook import OrderBook
from crypto_stat_arb.plan_store import ParentPlanStore, VenueOrderSnapshot
from crypto_stat_arb.risk import (
    ReconciliationDecision, RiskEngine, RiskLimits, VenueRiskSnapshot,
)


def instrument_master() -> InstrumentMaster:
    return InstrumentMaster([
        Instrument(
            instrument_id="BTC-USDT-SPOT",
            venue=venue,
            symbol="BTCUSDT",
            base="BTC",
            quote="USDT",
            tick_size=Decimal("0.10"),
            quantity_step=Decimal("0.001"),
            min_quantity=Decimal("0.001"),
            min_notional=Decimal("5"),
            taker_fee_bps=Decimal("4"),
        )
        for venue in ("BINANCE", "BYBIT")
    ])


def risk() -> RiskEngine:
    return RiskEngine(RiskLimits(
        max_order_notional=Decimal("5000"),
        max_gross_notional=Decimal("10000"),
        max_residual_base=Decimal("0.02"),
        max_book_age_ns=1_000_000_000,
    ))


def books(quantity: str = "1") -> dict[tuple[str, str], OrderBook]:
    result = {}
    for venue, bid, ask in (
        ("BINANCE", "59999.90", "60000.00"),
        ("BYBIT", "60060.00", "60060.10"),
    ):
        book = OrderBook(venue, "BTC-USDT-SPOT")
        book.apply(MarketEvent.from_dict({
            "event_type": "BOOK_SNAPSHOT",
            "venue": venue,
            "instrument_id": "BTC-USDT-SPOT",
            "exchange_ts_ns": 1,
            "receive_ts_ns": 1,
            "sequence": 1,
            "bids": [[bid, quantity]],
            "asks": [[ask, quantity]],
        }))
        result[(venue, "BTC-USDT-SPOT")] = book
    return result


def opportunity() -> Opportunity:
    return Opportunity(
        opportunity_id="opportunity-persistent-1",
        strategy_id="strategy-1",
        instrument_id="BTC-USDT-SPOT",
        observed_ts_ns=2,
        buy_venue="BINANCE",
        sell_venue="BYBIT",
        buy_price=Decimal("60000.00"),
        sell_price=Decimal("60060.00"),
        quantity=Decimal("0.010"),
        gross_edge_bps=Decimal("10"),
        estimated_net_edge_bps=Decimal("2"),
        source_event_ids=("binance-event", "bybit-event"),
    )


class FailingAdapter:
    def handle(self, request, book):
        raise ConnectionError("simulated connector loss after durable dispatch intent")


class ParentPlanPersistenceTests(unittest.TestCase):
    def test_normal_plan_persists_complete_parent_child_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ParentPlanStore(Path(directory) / "paper_oms.sqlite")
            oms = OMS(instrument_master(), risk(), Ledger(), plan_store=store)
            plan = oms.execute(opportunity(), books())
            restored = store.get(plan.parent_id)
            events = store.events(plan.parent_id)
            self.assertEqual(restored.state, PlanState.CLOSED)
            self.assertEqual(len(restored.children), 2)
            self.assertIn("DISPATCH_INTENT", [event["event_type"] for event in events])
            self.assertIn("ORDER_LIFECYCLE", [event["event_type"] for event in events])
            self.assertIn(
                "RMS_INTERNAL_RECONCILIATION",
                [event["event_type"] for event in events],
            )
            self.assertEqual(store.stats_snapshot()["nonterminal_plans"], 0)
            store.close()

            restarted_store = ParentPlanStore(Path(directory) / "paper_oms.sqlite")
            restarted = OMS(
                instrument_master(), risk(), Ledger(), plan_store=restarted_store
            )
            self.assertEqual(restarted.recovered_plans, 0)
            rebuilt = restarted.recalculate_account_state(
                mark_prices={
                    ("BINANCE", "BTC-USDT-SPOT"): Decimal("60000"),
                    ("BYBIT", "BTC-USDT-SPOT"): Decimal("60060"),
                },
                initial_capital_by_venue={
                    "BINANCE": Decimal("1000"), "BYBIT": Decimal("1000")
                },
            )
            self.assertEqual(
                rebuilt["positions"]["BINANCE:BTC-USDT-SPOT"]["quantity"],
                "0.010",
            )
            self.assertEqual(
                rebuilt["positions"]["BYBIT:BTC-USDT-SPOT"]["quantity"],
                "-0.010",
            )
            restarted_store.close()

    def test_partial_fill_state_and_residual_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ParentPlanStore(Path(directory) / "paper_oms.sqlite")
            oms = OMS(instrument_master(), risk(), Ledger(), plan_store=store)
            plan = oms.execute(opportunity(), books(quantity="0.005"))
            restored = store.get(plan.parent_id)
            states = [item["state"] for item in restored.transitions]
            self.assertIn(PlanState.PARTIAL.value, states)
            self.assertIn(PlanState.HEDGE_UNWIND.value, states)
            self.assertEqual(
                [child.filled_quantity for child in restored.children],
                [Decimal("0.005"), Decimal("0.005")],
            )
            store.close()

    def test_restart_restores_dispatch_intent_and_reconciles_venue_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper_oms.sqlite"
            first_store = ParentPlanStore(path)
            first_router = StrategyDirectedRouter(
                instrument_master(), adapters={"BINANCE": FailingAdapter()}
            )
            first_oms = OMS(
                instrument_master(), risk(), Ledger(),
                router=first_router, plan_store=first_store,
            )
            with self.assertRaises(ConnectionError):
                first_oms.execute(opportunity(), books())
            parent_id = next(iter(first_oms.plans))
            self.assertEqual(first_store.get(parent_id).state, PlanState.DISPATCHED)
            first_store.close()

            restarted_store = ParentPlanStore(path)
            restarted_router = StrategyDirectedRouter(instrument_master())
            restarted_oms = OMS(
                instrument_master(), risk(), Ledger(),
                router=restarted_router, plan_store=restarted_store,
            )
            self.assertEqual(restarted_oms.recovered_plans, 1)
            recovered = restarted_oms.execute(opportunity(), books())
            self.assertEqual(recovered.parent_id, parent_id)
            self.assertEqual(restarted_router.stats_snapshot()["unique_requests"], 0)

            venue_state = {
                child.order_id: VenueOrderSnapshot(
                    order_id=child.order_id,
                    venue=child.venue,
                    instrument_id=child.instrument_id,
                    state=OrderState.FILLED,
                    filled_quantity=Decimal("0.010"),
                    average_price=(
                        Decimal("60000.00")
                        if child.venue == "BINANCE"
                        else Decimal("60060.00")
                    ),
                    fee=Decimal("0.240"),
                    venue_order_id=f"venue-{child.order_id}",
                    observed_ts_ns=10,
                )
                for child in recovered.children
            }
            reconciled = restarted_oms.reconcile_plan(parent_id, venue_state, 10)
            self.assertEqual(reconciled.state, PlanState.CLOSED)
            self.assertEqual(reconciled.residual_base, Decimal("0"))
            self.assertEqual(restarted_store.stats_snapshot()["nonterminal_plans"], 0)
            restarted_store.close()

    def test_private_stream_timeout_and_rest_query_converge_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper_oms.sqlite"
            store = ParentPlanStore(path)
            ledger = Ledger()
            oms = OMS(
                instrument_master(), risk(), ledger,
                router=StrategyDirectedRouter(
                    instrument_master(), adapters={"BINANCE": FailingAdapter()}
                ),
                plan_store=store,
            )
            with self.assertRaises(ConnectionError):
                oms.execute(opportunity(), books())
            plan = next(iter(oms.plans.values()))
            buy, sell = plan.children
            oms.ingest_private_stream_update(CanonicalOrderUpdate(
                update_id="private-ack-buy", source=OrderUpdateSource.PRIVATE_STREAM,
                parent_order_id=plan.parent_id, order_id=buy.order_id,
                venue=buy.venue, instrument_id=buy.instrument_id,
                state=ExecutionEventType.ACKNOWLEDGED,
                event_ts_ns=10, observed_ts_ns=11, sequence=1,
            ))
            oms.ingest_private_stream_update(CanonicalOrderUpdate(
                update_id="private-partial-buy", source=OrderUpdateSource.PRIVATE_STREAM,
                parent_order_id=plan.parent_id, order_id=buy.order_id,
                venue=buy.venue, instrument_id=buy.instrument_id,
                state=ExecutionEventType.PARTIAL,
                event_ts_ns=12, observed_ts_ns=13, sequence=2,
                cumulative_filled_quantity=Decimal("0.005"),
                average_price=Decimal("60000.00"),
            ))
            oms.mark_order_timed_out(plan.parent_id, sell.order_id, 14)
            self.assertEqual(plan.state, PlanState.HEDGE_UNWIND)
            store.close()

            restarted_store = ParentPlanStore(path)
            restarted = OMS(
                instrument_master(), risk(), Ledger(), plan_store=restarted_store
            )
            recovered = restarted.plans[plan.parent_id]
            self.assertEqual(recovered.state, PlanState.HEDGE_UNWIND)
            self.assertEqual(len(recovered.children[0].state_updates), 2)
            rebuilt = restarted.recalculate_account_state(
                mark_prices={
                    ("BINANCE", "BTC-USDT-SPOT"): Decimal("60000")
                },
                initial_capital_by_venue={"BINANCE": Decimal("1000")},
            )
            self.assertEqual(
                rebuilt["positions"]["BINANCE:BTC-USDT-SPOT"]["quantity"],
                "0.005",
            )
            self.assertEqual(
                rebuilt["positions"]["BINANCE:BTC-USDT-SPOT"]["average_price"],
                "60000.00",
            )
            self.assertEqual(restarted.risk.stats_snapshot()["active_reservations"], 1)
            self.assertTrue(any(
                row["event_type"] == "RECOVERY_ACCOUNT_STATE_REBUILT"
                for row in restarted.ledger.events
            ))
            reconnect_orders = {
                child.order_id: VenueOrderSnapshot(
                    order_id=child.order_id, venue=child.venue,
                    instrument_id=child.instrument_id, state=child.state,
                    filled_quantity=child.filled_quantity,
                    average_price=child.average_price, fee=child.fee,
                    venue_order_id=f"venue-{child.order_id}", observed_ts_ns=18,
                )
                for child in recovered.children
            }
            reconnect_state = restarted.recalculate_account_state(
                mark_prices={
                    ("BINANCE", "BTC-USDT-SPOT"): Decimal("60000"),
                    ("BYBIT", "BTC-USDT-SPOT"): Decimal("60060"),
                },
                initial_capital_by_venue={
                    "BINANCE": Decimal("1000"), "BYBIT": Decimal("1000")
                },
            )
            reconnect_risk = tuple(
                VenueRiskSnapshot(
                    venue=venue, instrument_id="BTC-USDT-SPOT",
                    position_base=Decimal(reconnect_state["positions"].get(
                        f"{venue}:BTC-USDT-SPOT", {"quantity": "0"}
                    )["quantity"]),
                    balance_delta=restarted.ledger.cash_deltas.get(
                        venue, Decimal("0")
                    ),
                    mark_price=Decimal("60000" if venue == "BINANCE" else "60060"),
                    initial_capital=Decimal("1000"),
                    available_cash=Decimal(
                        reconnect_state["venues"][venue]["available_cash"]
                    ),
                    available_capital=Decimal(
                        reconnect_state["venues"][venue]["available_capital"]
                    ),
                    reserved_cash=Decimal(
                        reconnect_state["venues"][venue]["reserved_cash"]
                    ),
                    reserved_margin=Decimal(
                        reconnect_state["venues"][venue]["reserved_margin"]
                    ),
                    reserved_position=Decimal(
                        reconnect_state["venues"][venue]["reserved_position"]
                    ),
                    reserved_messages=int(
                        reconnect_state["venues"][venue]["reserved_messages"]
                    ),
                    observed_ts_ns=18,
                )
                for venue in ("BINANCE", "BYBIT")
            )
            reconnect_report = restarted.reconcile_risk_state(
                now_ns=19, venue_orders=reconnect_orders,
                venue_risk=reconnect_risk,
            )
            self.assertEqual(
                reconnect_report.decision, ReconciliationDecision.APPROVED
            )
            snapshots = {
                child.order_id: VenueOrderSnapshot(
                    order_id=child.order_id, venue=child.venue,
                    instrument_id=child.instrument_id, state=OrderState.FILLED,
                    filled_quantity=Decimal("0.010"),
                    average_price=(Decimal("60000.00") if child.side.value == "BUY"
                                   else Decimal("60060.00")),
                    fee=Decimal("0.240"), venue_order_id=f"venue-{child.order_id}",
                    observed_ts_ns=20,
                )
                for child in recovered.children
            }
            closed = restarted.reconcile_plan(plan.parent_id, snapshots, 21)
            self.assertEqual(closed.state, PlanState.CLOSED)
            self.assertEqual(closed.terminal_outcome, PlanState.FILLED.value)
            self.assertEqual(closed.residual_base, Decimal("0"))
            event_types = [x["event_type"] for x in restarted_store.events(plan.parent_id)]
            self.assertIn("ORDER_STATE_UPDATE_APPLIED", event_types)
            self.assertIn("VENUE_RECONCILED", event_types)
            restarted_store.close()

    def test_terminal_claim_conflict_has_one_deterministic_winner(self):
        store = None
        ledger = Ledger()
        oms = OMS(
            instrument_master(), risk(), ledger,
            router=StrategyDirectedRouter(
                instrument_master(), adapters={"BINANCE": FailingAdapter()}
            ),
        )
        with self.assertRaises(ConnectionError):
            oms.execute(opportunity(), books())
        plan = next(iter(oms.plans.values()))
        order = plan.children[0]
        updates = (
            CanonicalOrderUpdate(
                update_id="timer-terminal", source=OrderUpdateSource.TIMER,
                parent_order_id=plan.parent_id, order_id=order.order_id,
                venue=order.venue, instrument_id=order.instrument_id,
                state=ExecutionEventType.TIMED_OUT,
                event_ts_ns=30, observed_ts_ns=31,
            ),
            CanonicalOrderUpdate(
                update_id="rest-terminal", source=OrderUpdateSource.REST_QUERY,
                parent_order_id=plan.parent_id, order_id=order.order_id,
                venue=order.venue, instrument_id=order.instrument_id,
                state=ExecutionEventType.FILLED,
                event_ts_ns=29, observed_ts_ns=32,
                cumulative_filled_quantity=order.quantity,
                average_price=order.limit_price,
            ),
        )
        for update in updates:
            oms.ingest_order_update(update)
        self.assertEqual(order.state, OrderState.FILLED)
        self.assertTrue(any(
            row["event_type"] == "ORDER_TERMINAL_CONFLICT_RESOLVED"
            and row["winner"] == OrderState.FILLED.value
            for row in ledger.events
        ))
        before = len(order.state_updates)
        oms.ingest_order_update(updates[-1])
        self.assertEqual(len(order.state_updates), before)
        self.assertTrue(any(
            row["event_type"] == "ORDER_UPDATE_DUPLICATE_SUPPRESSED"
            for row in ledger.events
        ))


if __name__ == "__main__":
    unittest.main()
