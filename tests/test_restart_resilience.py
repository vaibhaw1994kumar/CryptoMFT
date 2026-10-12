from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.execution import StrategyDirectedRouter
from crypto_stat_arb.instruments import InstrumentMaster
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import Instrument, MarketEvent, Opportunity, OrderState, PlanState
from crypto_stat_arb.monitoring import MonitoringThresholds, PipelineMonitor
from crypto_stat_arb.oms import OMS
from crypto_stat_arb.orderbook import OrderBook
from crypto_stat_arb.plan_store import ParentPlanStore, VenueOrderSnapshot
from crypto_stat_arb.resilience import (
    OwnershipError, RestartCoordinator, ResumeGateError, SessionState,
)
from crypto_stat_arb.risk import (
    ReconciliationDecision, ReconciliationReport, RiskEngine, RiskLimits,
)


class Clock:
    def __init__(self, now: int = 100) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def master() -> InstrumentMaster:
    return InstrumentMaster([
        Instrument(
            instrument_id="BTC-USDT-SPOT", venue=venue, symbol="BTCUSDT",
            base="BTC", quote="USDT", tick_size=Decimal("0.10"),
            quantity_step=Decimal("0.001"), min_quantity=Decimal("0.001"),
            min_notional=Decimal("5"), taker_fee_bps=Decimal("4"),
        )
        for venue in ("BINANCE", "BYBIT")
    ])


def risk() -> RiskEngine:
    return RiskEngine(RiskLimits(
        max_order_notional=Decimal("5000"),
        max_gross_notional=Decimal("10000"),
        max_residual_base=Decimal("0.02"), max_book_age_ns=1_000_000_000,
    ))


def books() -> dict[tuple[str, str], OrderBook]:
    result = {}
    for venue, bid, ask in (
        ("BINANCE", "59999.90", "60000.00"),
        ("BYBIT", "60060.00", "60060.10"),
    ):
        book = OrderBook(venue, "BTC-USDT-SPOT")
        book.apply(MarketEvent.from_dict({
            "event_type": "BOOK_SNAPSHOT", "venue": venue,
            "instrument_id": "BTC-USDT-SPOT", "exchange_ts_ns": 1,
            "receive_ts_ns": 1, "sequence": 1,
            "bids": [[bid, "1"]], "asks": [[ask, "1"]],
        }))
        result[(venue, "BTC-USDT-SPOT")] = book
    return result


def opportunity(identifier: str = "restart-opportunity") -> Opportunity:
    return Opportunity(
        opportunity_id=identifier, strategy_id="strategy-1",
        instrument_id="BTC-USDT-SPOT", observed_ts_ns=2,
        buy_venue="BINANCE", sell_venue="BYBIT",
        buy_price=Decimal("60000.00"), sell_price=Decimal("60060.00"),
        quantity=Decimal("0.010"), gross_edge_bps=Decimal("10"),
        estimated_net_edge_bps=Decimal("2"),
        source_event_ids=("binance-event", "bybit-event"),
    )


def approved_report(identifier: str = "reconciliation-initial") -> ReconciliationReport:
    return ReconciliationReport(
        reconciliation_id=identifier, ts_ns=100,
        decision=ReconciliationDecision.APPROVED, issues=(), local_orders=0,
        venue_orders=0, fills=0, venue_snapshots=0,
        suspended_venues=(), resolved_venues=(),
    )


class FailingAdapter:
    def handle(self, request, book):
        raise ConnectionError("simulated process loss after durable dispatch intent")


class RestartResilienceTests(unittest.TestCase):
    def test_heartbeat_takeover_fences_old_owner_and_requires_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resilience.sqlite"
            clock = Clock()
            first = RestartCoordinator(
                path, "paper-btc", required_venues=("BINANCE", "BYBIT"),
                lease_duration_ns=10, reconnect_base_ns=2, clock_ns=clock,
            )
            lease1 = first.acquire("process-a")
            clock.now = 105
            lease1 = first.heartbeat("process-a", lease1.fencing_token)
            self.assertEqual(lease1.lease_expires_ts_ns, 115)
            with self.assertRaises(ResumeGateError):
                first.assert_new_risk_allowed("process-a", lease1.fencing_token)
            due = first.record_disconnect(
                "process-a", lease1.fencing_token, "BINANCE", "socket reset"
            )
            self.assertEqual(due, 107)
            clock.now = 107
            self.assertTrue(first.reconnect_due("BINANCE"))

            second = RestartCoordinator(
                path, "paper-btc", required_venues=("BINANCE", "BYBIT"),
                lease_duration_ns=10, reconnect_base_ns=2, clock_ns=clock,
            )
            with self.assertRaises(OwnershipError):
                second.acquire("process-b")
            clock.now = 116
            lease2 = second.acquire("process-b")
            self.assertGreater(lease2.fencing_token, lease1.fencing_token)
            with self.assertRaises(OwnershipError):
                first.heartbeat("process-a", lease1.fencing_token)
            attempts = {"BINANCE": 0, "BYBIT": 0}

            def reconnect(venue):
                attempts[venue] += 1
                if venue == "BINANCE" and attempts[venue] == 1:
                    raise ConnectionError("temporary reconnect failure")

            outcomes = second.run_due_reconnects(
                "process-b", lease2.fencing_token, reconnect
            )
            self.assertEqual(outcomes, {"BINANCE": False, "BYBIT": True})
            clock.now = 118
            self.assertEqual(second.run_due_reconnects(
                "process-b", lease2.fencing_token, reconnect
            ), {"BINANCE": True})
            self.assertEqual(
                second.snapshot()["session"]["state"], SessionState.RECONCILING.value
            )
            second.resume_after_reconciliation(
                "process-b", lease2.fencing_token, approved_report()
            )
            second.assert_new_risk_allowed("process-b", lease2.fencing_token)
            snapshot = second.snapshot()
            self.assertEqual(snapshot["session"]["state"], "READY")
            monitored = PipelineMonitor(MonitoringThresholds()).build(
                mode="LIVE", generated_ts_ns=clock.now,
                correlation_id="paper-btc", job={"state": "RUNNING"},
                data={"event_count": 1}, partitions={"completeness": 1},
                books={}, versions={}, performance={}, live={},
                restart_resilience=snapshot,
            )
            resilience_metrics = {
                row["name"]: row["value"] for row in monitored["metrics"]
                if row["scope"] == "RESTART_RESILIENCE"
            }
            self.assertEqual(resilience_metrics, {
                "active_order_owner": 1,
                "restart_resume_ready": 1,
                "restart_disconnected_connectors": 0,
            })
            first.close()
            second.close()

    def test_checkpoint_is_exact_idempotent_and_cannot_regress(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            coordinator = RestartCoordinator(
                Path(directory) / "resilience.sqlite", "paper-btc",
                required_venues=("BINANCE",), lease_duration_ns=100,
                clock_ns=clock,
            )
            lease = coordinator.acquire("process-a")
            saved = coordinator.save_checkpoint(
                "process-a", lease.fencing_token, "book:BINANCE:BTCUSDT", 42,
                "raw/session/part.jsonl#42",
            )
            repeated = coordinator.save_checkpoint(
                "process-a", lease.fencing_token, "book:BINANCE:BTCUSDT", 42,
                "raw/session/part.jsonl#42",
            )
            self.assertEqual(saved.sequence, repeated.sequence)
            self.assertEqual(coordinator.load_checkpoint(saved.stream_id).raw_ref, saved.raw_ref)
            with self.assertRaises(ValueError):
                coordinator.save_checkpoint(
                    "process-a", lease.fencing_token, saved.stream_id, 41,
                    "raw/session/part.jsonl#41",
                )
            coordinator.close()

    def test_process_restart_recovers_open_orders_without_duplicate_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            resilience_path = base / "resilience.sqlite"
            plan_path = base / "paper_oms.sqlite"
            clock = Clock()
            first_control = RestartCoordinator(
                resilience_path, "paper-btc", required_venues=("BINANCE", "BYBIT"),
                lease_duration_ns=10, clock_ns=clock,
            )
            lease1 = first_control.acquire("process-a")
            for venue in ("BINANCE", "BYBIT"):
                first_control.mark_connected("process-a", lease1.fencing_token, venue)
            first_control.resume_after_reconciliation(
                "process-a", lease1.fencing_token, approved_report()
            )
            first_control.save_checkpoint(
                "process-a", lease1.fencing_token, "canonical-events", 91,
                "canonical/live/part-0001.jsonl#91",
            )

            first_store = ParentPlanStore(plan_path)
            first_router = StrategyDirectedRouter(
                master(), adapters={"BINANCE": FailingAdapter()}
            )
            first_oms = OMS(
                master(), risk(), Ledger(), router=first_router,
                plan_store=first_store,
                order_owner_guard=lambda: first_control.assert_owner(
                    "process-a", lease1.fencing_token
                ),
                new_risk_guard=lambda: first_control.assert_new_risk_allowed(
                    "process-a", lease1.fencing_token
                ),
            )
            with self.assertRaises(ConnectionError):
                first_oms.execute(opportunity(), books())
            parent_id = next(iter(first_oms.plans))
            self.assertEqual(first_store.get(parent_id).state, PlanState.DISPATCHED)
            first_store.close()

            clock.now = 111
            second_control = RestartCoordinator(
                resilience_path, "paper-btc", required_venues=("BINANCE", "BYBIT"),
                lease_duration_ns=20, clock_ns=clock,
            )
            lease2 = second_control.acquire("process-b")
            with self.assertRaises(OwnershipError):
                first_control.assert_owner("process-a", lease1.fencing_token)
            for venue in ("BINANCE", "BYBIT"):
                second_control.mark_connected("process-b", lease2.fencing_token, venue)

            restarted_store = ParentPlanStore(plan_path)
            restarted_router = StrategyDirectedRouter(master())
            restarted = OMS(
                master(), risk(), Ledger(), router=restarted_router,
                plan_store=restarted_store,
                order_owner_guard=lambda: second_control.assert_owner(
                    "process-b", lease2.fencing_token
                ),
                new_risk_guard=lambda: second_control.assert_new_risk_allowed(
                    "process-b", lease2.fencing_token
                ),
            )
            recovered = restarted.execute(opportunity(), books())
            self.assertEqual(recovered.parent_id, parent_id)
            self.assertEqual(restarted_router.stats_snapshot()["unique_requests"], 0)
            self.assertEqual(
                second_control.load_checkpoint("canonical-events").sequence, 91
            )

            venue_orders = {
                child.order_id: VenueOrderSnapshot(
                    order_id=child.order_id, venue=child.venue,
                    instrument_id=child.instrument_id, state=OrderState.FILLED,
                    filled_quantity=Decimal("0.010"),
                    average_price=(
                        Decimal("60000.00") if child.venue == "BINANCE"
                        else Decimal("60060.00")
                    ),
                    fee=Decimal("0.240"), observed_ts_ns=120,
                )
                for child in recovered.children
            }
            restarted.reconcile_plan(parent_id, venue_orders, 120)
            report = restarted.reconcile_risk_state(
                now_ns=121, venue_orders=venue_orders,
                require_complete_opportunities=True,
            )
            self.assertEqual(report.decision, ReconciliationDecision.APPROVED)
            second_control.resume_after_reconciliation(
                "process-b", lease2.fencing_token, report
            )
            second_control.assert_new_risk_allowed("process-b", lease2.fencing_token)
            self.assertEqual(restarted_store.get(parent_id).state, PlanState.CLOSED)
            event_types = [row["event_type"] for row in second_control.events()]
            self.assertIn("OWNER_ACQUIRED", event_types)
            self.assertIn("RESUME_APPROVED", event_types)
            restarted_store.close()
            first_control.close()
            second_control.close()


if __name__ == "__main__":
    unittest.main()
