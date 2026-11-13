import sys
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from ems_evidence import write_case
from crypto_stat_arb.execution import (
    CanonicalOrderUpdate, DispatchPolicy, ExecutionCallback, ExecutionEventType,
    OrderAction, OrderUpdateSource, ResidualPolicy, StrategyDirectedRouter,
)
from crypto_stat_arb.hedge_lifecycle import HedgePolicy
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


MS = 1_000_000
INSTR_FT = "BTC-USDT-SPOT"


def _ft_instruments() -> InstrumentMaster:
    return InstrumentMaster([
        Instrument(
            instrument_id=INSTR_FT, venue=v, symbol="BTCUSDT", base="BTC", quote="USDT",
            tick_size=Decimal("0.10"), quantity_step=Decimal("0.001"), min_quantity=Decimal("0.001"),
            min_notional=Decimal("5"), taker_fee_bps=Decimal("4"),
        )
        for v in ("BINANCE", "BYBIT")
    ])


def _ft_opportunity(oid: str = "opp-restart") -> Opportunity:
    return Opportunity(
        opportunity_id=oid, strategy_id="strategy-1", instrument_id=INSTR_FT, observed_ts_ns=1,
        buy_venue="BINANCE", sell_venue="BYBIT",
        buy_price=Decimal("60000"), sell_price=Decimal("60060"),
        quantity=Decimal("0.010"), gross_edge_bps=Decimal("10"), estimated_net_edge_bps=Decimal("2"),
        source_event_ids=("e1", "e2"),
    )


def _ft_books() -> dict:
    result = {}
    for venue, bid, ask in (("BINANCE", "59999.90", "60000.00"), ("BYBIT", "60060.00", "60060.10")):
        book = OrderBook(venue, INSTR_FT)
        book.apply(MarketEvent.from_dict({"event_type": "BOOK_SNAPSHOT", "venue": venue,
            "instrument_id": INSTR_FT, "exchange_ts_ns": 1, "receive_ts_ns": 1, "sequence": 1,
            "bids": [[bid, "5"]], "asks": [[ask, "5"]],
        }))
        result[(venue, INSTR_FT)] = book
    return result


def _cb(req, kind, cum="0", last="0", price="0", reason=""):
    return ExecutionCallback(
        schema_version="1.0.0", callback_id=f"{kind.value}-{req.request_id}",
        request_id=req.request_id, order_id=req.order_id,
        parent_opportunity_id=req.parent_opportunity_id, parent_order_id=req.parent_order_id,
        venue=req.venue, instrument_id=req.instrument_id, event_type=kind,
        event_ts_ns=req.created_ts_ns, cumulative_filled_quantity=Decimal(cum),
        last_fill_quantity=Decimal(last), average_price=Decimal(price), reason=reason,
    )


class ScriptedVenue:
    """Answers SUBMITs from a fixed script; repeats the last item once depleted."""
    def __init__(self, venue: str, script: list, price: str = "60000"):
        self.venue, self.price = venue, price
        self.script = list(script)
        self.submits: list = []
        self.cancels = 0

    def handle(self, req, book):
        if req.action == OrderAction.CANCEL:
            self.cancels += 1
            return (_cb(req, ExecutionEventType.CANCELLED),)
        self.submits.append(req)
        what = self.script.pop(0) if self.script else ("fill", None)
        if what == "ack":
            return (_cb(req, ExecutionEventType.ACKNOWLEDGED),)
        kind, arg = what if isinstance(what, tuple) else (what, None)
        if kind == "reject":
            return (_cb(req, ExecutionEventType.REJECTED, reason=arg or "simulated"),)
        qty = Decimal(arg) if arg is not None else req.quantity
        return (_cb(req, ExecutionEventType.FILLED, cum=str(qty), last=str(qty), price=self.price),)


def _maker_update(plan, maker, n, state, cum, ts):
    return CanonicalOrderUpdate(
        update_id=f"mk-{n}", source=OrderUpdateSource.PRIVATE_STREAM,
        parent_order_id=plan.parent_id, order_id=maker.order_id,
        venue=maker.venue, instrument_id=maker.instrument_id,
        state=state, event_ts_ns=ts, observed_ts_ns=ts + 1, sequence=n,
        cumulative_filled_quantity=Decimal(cum), average_price=Decimal("60060"),
    )


class _SilentMakerVenue:
    """A maker venue whose gateway lost the submit: nothing comes back, and a status query is not answered (None)."""
    def __init__(self, venue):
        self.venue, self.submits = venue, []

    def handle(self, req, book):
        self.submits.append(req)
        return ()

    def query_order(self, order_id):
        return None


class MakerAckDeadlineTests(unittest.TestCase):
    """Case 2l finding: a post-only quote whose submit or ack is lost stayed SENT with no deadline (565 s in the replay). `oms.maker_ack_deadline` (off by default) watches it from the send: queried
    after the report timeout, never resent, timed out and the kill switch once the queries go unanswered."""
    def run_oms(self, enabled):
        inst = _ft_instruments()
        venue = _SilentMakerVenue("BYBIT")
        oms = OMS(inst, risk(), Ledger(), router=StrategyDirectedRouter(inst, adapters={"BYBIT": venue, "BINANCE": ScriptedVenue("BINANCE", ["fill"], price="60000")}),
                  dispatch_policy=DispatchPolicy.FILL_TRIGGERED, hedge_policy=HedgePolicy(retry_limit=1))
        oms.maker_ack_deadline = enabled
        plan = oms.execute(_ft_opportunity(), _ft_books(), maker_venue="BYBIT")
        sent = max(e["ts_ns"] for e in oms.ledger.events)
        return oms, plan, venue, sent

    def test_off_by_default_a_lost_submit_is_not_watched(self):
        oms, plan, venue, sent = self.run_oms(False)
        self.assertEqual(plan.children[0].state, OrderState.SENT)
        oms.advance_time(sent + 30_000 * MS, _ft_books())
        self.assertEqual([e for e in oms.ledger.events if e["event_type"] == "VENUE_ORDER_QUERY"], [])
        self.assertEqual(plan.children[0].state, OrderState.SENT)

    def test_on_the_lost_submit_is_queried_never_resent_then_timed_out_with_the_kill_switch(self):
        oms, plan, venue, sent = self.run_oms(True)
        oms.advance_time(sent + 1_900 * MS, _ft_books())
        self.assertEqual([e for e in oms.ledger.events if e["event_type"] == "VENUE_ORDER_QUERY"], [])          # inside the report timeout (2 s) nothing is asked
        oms.advance_time(sent + 10_000 * MS, _ft_books())
        queries = [e for e in oms.ledger.events if e["event_type"] == "VENUE_ORDER_QUERY"]
        self.assertEqual(len(queries), 3)                                                                       # max_queries unanswered
        self.assertTrue(all(q["answered"] is False for q in queries))
        self.assertEqual(len(venue.submits), 1)                                                                 # never resent
        self.assertEqual(plan.children[0].state, OrderState.TIMED_OUT)
        self.assertTrue(oms.risk.kill_switch)


class HedgeRecoveryAfterRestartTests(unittest.TestCase):
    """Track B Item 2 — Test A: open hedge at session death is recovered without duplicate dispatch."""

    def test_open_hedge_recovered_across_restart_no_alarm(self):
        """Fill triggers a hedge; OMS is replaced before the hedge fills; second OMS recovers the
        plan from plan_store with the hedge in SUBMITTED state; reconciliation passes; no alarm."""
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "oms.sqlite"
            inst = _ft_instruments()
            bk = _ft_books()
            opp = _ft_opportunity()

            # --- session 1: open hedge, die before it fills -------------------------------------------------------
            bybit1 = ScriptedVenue("BYBIT", ["ack"], price="60060")
            # hedge on BINANCE: ack (stays open, never fills during session 1)
            binance1 = ScriptedVenue("BINANCE", ["ack"], price="60000")
            store1 = ParentPlanStore(plan_path)
            oms1 = OMS(inst, risk(), Ledger(), plan_store=store1,
                       router=StrategyDirectedRouter(inst, adapters={"BYBIT": bybit1, "BINANCE": binance1}),
                       dispatch_policy=DispatchPolicy.FILL_TRIGGERED,
                       hedge_policy=HedgePolicy(retry_limit=1))
            plan1 = oms1.execute(opp, bk, maker_venue="BYBIT")
            parent_id = plan1.parent_id
            maker1 = plan1.children[0]
            # trigger maker fill → hedge dispatched
            oms1.ingest_order_update(_maker_update(plan1, maker1, 1, ExecutionEventType.FILLED, "0.010", 100 * MS))
            # hedge has been submitted (ack'd) and is open
            hedge_children = [o for o in oms1.plans[parent_id].children if o.order_role == "HEDGE"]
            self.assertTrue(hedge_children, "hedge must have been dispatched")
            # session dies here: plan in an open state
            store1.close()

            # --- session 2: restart, recover plan -----------------------------------------------------------------
            store2 = ParentPlanStore(plan_path)
            bybit2 = ScriptedVenue("BYBIT", [], price="60060")
            binance2 = ScriptedVenue("BINANCE", [], price="60000")
            oms2 = OMS(inst, risk(), Ledger(), plan_store=store2,
                       router=StrategyDirectedRouter(inst, adapters={"BYBIT": bybit2, "BINANCE": binance2}),
                       dispatch_policy=DispatchPolicy.FILL_TRIGGERED)
            # plan was loaded from store; executing the same opportunity returns the recovered plan
            recovered = oms2.execute(opp, bk, maker_venue="BYBIT")
            self.assertEqual(recovered.parent_id, parent_id, "must recover the same parent_id")
            # second OMS must NOT have re-dispatched anything (recovery, no duplicate)
            self.assertEqual(len(bybit2.submits), 0, "no new maker orders on restart")
            self.assertEqual(len(binance2.submits), 0, "no new hedge orders on restart (hedge already open)")
            # no unwind-exhausted alarm on restart
            self.assertFalse(oms2.unwind_exhausted_alarms, "no alarm on clean restart with open hedge")
            store2.close()


class UnwindExhaustedAlarmPersistsTests(unittest.TestCase):
    """Track B Item 2 — Test B: unwind-exhausted alarm persists across OMS restart via plan_store."""

    def test_alarm_loads_on_second_oms_and_blocks_new_risk(self):
        """Raise the unwind-exhausted alarm via the OMS private API; verify it is persisted in
        plan_store; a second OMS that loads the same store has the alarm active and rejects new risk."""
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "oms.sqlite"
            inst = _ft_instruments()
            bk = _ft_books()
            opp = _ft_opportunity("opp-alarm")
            # max_unwind_attempts=0 makes _raise_unwind_exhausted_alarm fire without needing actual unwind attempts
            rp = ResidualPolicy(max_unwind_attempts=0, max_residual_base=Decimal("0.02"), max_residual_age_ns=5_000_000_000)

            # --- session 1: execute plan, manually raise the alarm -----------------------------------------------
            bybit1 = ScriptedVenue("BYBIT", ["ack"], price="60060")
            binance1 = ScriptedVenue("BINANCE", ["ack"], price="60000")
            store1 = ParentPlanStore(plan_path)
            oms1 = OMS(inst, risk(), Ledger(), plan_store=store1,
                       router=StrategyDirectedRouter(inst, adapters={"BYBIT": bybit1, "BINANCE": binance1}),
                       dispatch_policy=DispatchPolicy.FILL_TRIGGERED, residual_policy=rp)
            plan1 = oms1.execute(opp, bk, maker_venue="BYBIT")
            parent_id = plan1.parent_id
            # Directly raise the alarm (testing the persistence path, not the trigger path)
            oms1._raise_unwind_exhausted_alarm(oms1.plans[parent_id], ts_ns=200 * MS)
            self.assertIn(parent_id, oms1.unwind_exhausted_alarms, "alarm must be active in OMS1")
            store1.close()

            # --- session 2: restart, alarm must be loaded --------------------------------------------------------
            store2 = ParentPlanStore(plan_path)
            oms2 = OMS(inst, risk(), Ledger(), plan_store=store2,
                       router=StrategyDirectedRouter(inst),
                       dispatch_policy=DispatchPolicy.FILL_TRIGGERED)
            # alarm is loaded from plan_store events at OMS init
            self.assertIn(parent_id, oms2.unwind_exhausted_alarms,
                          "alarm must survive restart and be loaded from plan_store")

            # new opportunity must be blocked by the alarm (terminal_outcome="REJECTED", plan closes immediately)
            opp2 = _ft_opportunity("opp-new")
            plan2 = oms2.execute(opp2, bk, maker_venue="BYBIT")
            self.assertEqual(plan2.terminal_outcome, "REJECTED",
                             "new opportunity must have terminal_outcome=REJECTED while alarm is active")
            self.assertGreater(oms2.realism_counters.get("new_risk_held_unwind_exhausted", 0), 0,
                               "new_risk_held_unwind_exhausted counter must be incremented")
            store2.close()

    def test_alarm_reset_clears_block_and_does_not_persist(self):
        """After reset_unwind_exhausted_alarm, the RESET event is persisted; a third OMS does NOT
        reload the alarm and new risk passes."""
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "oms.sqlite"
            inst = _ft_instruments()
            bk = _ft_books()
            opp = _ft_opportunity("opp-reset")
            rp = ResidualPolicy(max_unwind_attempts=0, max_residual_base=Decimal("0.02"), max_residual_age_ns=5_000_000_000)

            # session 1: raise alarm
            store1 = ParentPlanStore(plan_path)
            bybit1 = ScriptedVenue("BYBIT", ["ack"], price="60060")
            binance1 = ScriptedVenue("BINANCE", ["ack"], price="60000")
            oms1 = OMS(inst, risk(), Ledger(), plan_store=store1,
                       router=StrategyDirectedRouter(inst, adapters={"BYBIT": bybit1, "BINANCE": binance1}),
                       dispatch_policy=DispatchPolicy.FILL_TRIGGERED, residual_policy=rp)
            plan1 = oms1.execute(opp, bk, maker_venue="BYBIT")
            parent_id = plan1.parent_id
            oms1._raise_unwind_exhausted_alarm(oms1.plans[parent_id], ts_ns=200 * MS)
            # operator resets the alarm
            reset_ids = oms1.reset_unwind_exhausted_alarm(ts_ns=300 * MS, operator="operator-1")
            self.assertIn(parent_id, reset_ids)
            self.assertFalse(oms1.unwind_exhausted_alarms, "alarm must be cleared after reset")
            store1.close()

            # session 3: alarm is not reloaded
            store3 = ParentPlanStore(plan_path)
            bybit3 = ScriptedVenue("BYBIT", ["ack"], price="60060")
            binance3 = ScriptedVenue("BINANCE", ["ack"], price="60000")
            oms3 = OMS(inst, risk(), Ledger(), plan_store=store3,
                       router=StrategyDirectedRouter(inst, adapters={"BYBIT": bybit3, "BINANCE": binance3}),
                       dispatch_policy=DispatchPolicy.FILL_TRIGGERED)
            self.assertFalse(oms3.unwind_exhausted_alarms,
                             "reset must persist: alarm must NOT be reloaded after RESET event")
            # new risk is not blocked by the alarm (plan dispatches the maker normally, ACK'd)
            opp3 = _ft_opportunity("opp-after-reset")
            plan3 = oms3.execute(opp3, bk, maker_venue="BYBIT")
            self.assertNotEqual(plan3.terminal_outcome, "REJECTED",
                                "new risk must not be blocked after alarm is reset")
            self.assertEqual(oms3.realism_counters.get("new_risk_held_unwind_exhausted", 0), 0,
                             "counter must be 0 when alarm is cleared")
            store3.close()


if __name__ == "__main__":
    unittest.main()


class RestartAgainstTheVenueTests(unittest.TestCase):
    """Owner 12 Nov item 2: the session was down while the venue filled its resting maker order. On restart the OMS takes the venue's account of the order, books the missed execution ONCE, sends the hedge it
    triggers, and takes no new risk until the recovered plan is reconciled (restart_reconcile_gate, default off). A second restart books nothing new."""

    def _first_session(self, plan_path, maker_script=("ack",)):
        inst, bk, opp = _ft_instruments(), _ft_books(), _ft_opportunity()
        store = ParentPlanStore(plan_path)
        oms = OMS(inst, risk(), Ledger(), plan_store=store,
                  router=StrategyDirectedRouter(inst, adapters={"BYBIT": ScriptedVenue("BYBIT", list(maker_script), price="60060"), "BINANCE": ScriptedVenue("BINANCE", ["fill"], price="60000")}),
                  dispatch_policy=DispatchPolicy.FILL_TRIGGERED, hedge_policy=HedgePolicy(retry_limit=1))
        plan = oms.execute(opp, bk, maker_venue="BYBIT")
        maker = plan.children[0]
        store.close()
        return plan.parent_id, maker

    def _restart(self, plan_path, gate=True):
        inst = _ft_instruments()
        store = ParentPlanStore(plan_path)
        binance = ScriptedVenue("BINANCE", ["fill"], price="60000")
        bybit = ScriptedVenue("BYBIT", [], price="60060")
        oms = OMS(inst, risk(), Ledger(), plan_store=store,
                  router=StrategyDirectedRouter(inst, adapters={"BYBIT": bybit, "BINANCE": binance}),
                  dispatch_policy=DispatchPolicy.FILL_TRIGGERED, hedge_policy=HedgePolicy(retry_limit=1))
        oms.restart_reconcile_gate = gate
        return oms, store, bybit, binance

    def _snapshot(self, maker, state, filled):
        from crypto_stat_arb.plan_store import VenueOrderSnapshot
        return VenueOrderSnapshot(order_id=maker.order_id, venue=maker.venue, instrument_id=maker.instrument_id, state=state, filled_quantity=Decimal(filled),
                                  average_price=Decimal("60060"), venue_order_id="v-1", observed_ts_ns=500 * MS)

    def _held(self, oms):
        return [e["opportunity_id"] for e in oms.ledger.events if e["event_type"] == "NEW_RISK_HELD_RESTART_RECONCILIATION"]

    def _fills(self, oms):
        return [e for e in oms.ledger.events if e["event_type"] == "FILL"]

    def test_missed_execution_is_booked_once_hedged_once_and_new_risk_waits_for_it(self):
        from crypto_stat_arb.models import OrderState
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "oms.sqlite"
            parent_id, maker = self._first_session(plan_path)
            oms, store, bybit, binance = self._restart(plan_path)
            self.assertEqual(oms.recovered_plans, 1)
            self.assertEqual(self._fills(oms), [])                                              # nothing booked yet: the execution happened while the session was down
            held = oms.execute(_ft_opportunity("opp-new-1"), _ft_books(), maker_venue="BYBIT")
            self.assertEqual(held.children[0].state.value, "CREATED")                           # before reconciliation: no new maker exposure (the plan closes without a single order sent)
            self.assertEqual(bybit.submits, [])
            self.assertEqual(self._held(oms), ["opp-new-1"])
            snap = self._snapshot(maker, OrderState.FILLED, "0.010")
            plan = oms.reconcile_plan(parent_id, {maker.order_id: snap}, 600 * MS)
            maker_fills = [e for e in self._fills(oms) if e.get("order_id") == maker.order_id]
            self.assertEqual(len(maker_fills), 1)                                               # booked once
            self.assertEqual(len(binance.submits), 1)                                           # the residual hedged once
            self.assertEqual(plan.state.value, "CLOSED")
            self.assertEqual(oms.carried_residual_base(), Decimal("0"))
            self.assertEqual(oms.ledger.positions[("BYBIT", INSTR_FT)], Decimal("-0.010"))     # the sell maker fill
            self.assertEqual(oms.ledger.positions[("BINANCE", INSTR_FT)], Decimal("0.010"))     # and its hedge: flat net
            oms.execute(_ft_opportunity("opp-new-2"), _ft_books(), maker_venue="BYBIT")          # reconciled: new risk is allowed again
            self.assertEqual(self._held(oms), ["opp-new-1"])                                      # not held a second time
            self.assertEqual(len(bybit.submits), 1)
            # a SECOND restart on the same store: the plan is closed, nothing is booked or sent again
            store.close()
            oms3, store3, bybit3, binance3 = self._restart(plan_path)
            self.assertEqual(oms3.recovered_plans, 1 if any(p.state.value != "CLOSED" for p in oms3.plans.values()) else 0)
            again = oms3.reconcile_plan(parent_id, {maker.order_id: snap}, 700 * MS)
            self.assertEqual(binance3.submits, [])
            self.assertEqual(len([e for e in self._fills(oms3) if e.get("order_id") == maker.order_id]), len(maker_fills))
            self.assertEqual(again.children[0].filled_quantity, Decimal("0.010"))
            write_case("case6_restart_against_the_venue_missed_execution_booked_once_hedged_once", oms3.ledger.events + oms.ledger.events)
            store3.close()

    def test_late_success_four_way_agreement_before_anything_new_is_sent(self):
        """Owner 12 Nov: the venue executed while the OMS had lost the answer. The plan is released only when client order id, exchange order id, execution reports and venue position ALL agree."""
        from crypto_stat_arb.models import OrderState
        def attempt(executions, positions):
            with tempfile.TemporaryDirectory() as directory:
                plan_path = Path(directory) / "oms.sqlite"
                parent_id, maker = self._first_session(plan_path)
                oms, store, bybit, binance = self._restart(plan_path)
                oms.execute(_ft_opportunity("opp-held"), _ft_books(), maker_venue="BYBIT")                 # books known to the OMS, as in a running session
                snap = self._snapshot(maker, OrderState.FILLED, "0.010")
                oms.reconcile_plan(parent_id, {maker.order_id: snap}, 600 * MS, venue_executions=executions(maker), venue_positions=positions)
                oms.execute(_ft_opportunity("opp-after"), _ft_books(), maker_venue="BYBIT")
                result = (self._held(oms), len(bybit.submits), [e["disagreements"] for e in oms.ledger.events if e["event_type"] == "RECONCILIATION_DISAGREEMENT"])
                write_case("case9_late_success_reconciliation_" + ("agree" if not result[2] else "disagree"), oms.ledger.events)
                store.close()
                return result
        good_pos = {("BYBIT", INSTR_FT): Decimal("-0.010"), ("BINANCE", INSTR_FT): Decimal("0.010")}
        held, maker_submits, disagreements = attempt(lambda m: {m.order_id: Decimal("0.010")}, good_pos)
        self.assertEqual((disagreements, maker_submits), ([], 1))                                    # all four agree: released, the new quote is sent
        self.assertEqual(held, ["opp-held"])                                                          # (only the quote tried BEFORE the reconciliation was held)
        held, maker_submits, disagreements = attempt(lambda m: {m.order_id: Decimal("0.010")}, {**good_pos, ("BYBIT", INSTR_FT): Decimal("0")})
        self.assertEqual(maker_submits, 0)                                                            # the venue position disagrees: nothing new is sent
        self.assertEqual(held, ["opp-held", "opp-after"]); self.assertEqual(disagreements[0], ["venue_position_differs:BYBIT"])
        held, maker_submits, disagreements = attempt(lambda m: {m.order_id: Decimal("0.004")}, good_pos)
        self.assertEqual(maker_submits, 0)                                                            # the execution reports do not add up to the order state
        self.assertTrue(disagreements[0][0].startswith("executions_differ"))
        held, maker_submits, disagreements = attempt(lambda m: {"someone-elses-order": Decimal("0.010")}, good_pos)
        self.assertEqual(maker_submits, 0)
        self.assertTrue(disagreements[0][0].startswith("execution_for_unknown_order"))

    def test_an_order_the_venue_does_not_know_keeps_new_risk_held(self):
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "oms.sqlite"
            parent_id, maker = self._first_session(plan_path)                                    # sent and persisted, then the session died: the venue's reply says nothing about it
            oms, store, bybit, binance = self._restart(plan_path)
            plan = oms.reconcile_plan(parent_id, {}, 600 * MS)                                    # unresolved: pending, not closed
            self.assertNotEqual(plan.state.value, "CLOSED")
            oms.execute(_ft_opportunity("opp-new-3"), _ft_books(), maker_venue="BYBIT")
            self.assertEqual(self._held(oms), ["opp-new-3"])
            self.assertEqual(bybit.submits, [])
            store.close()

    def test_the_gate_is_off_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "oms.sqlite"
            self._first_session(plan_path)
            oms, store, bybit, _ = self._restart(plan_path, gate=False)
            self.assertFalse(oms.restart_reconcile_gate)
            oms.execute(_ft_opportunity("opp-new-4"), _ft_books(), maker_venue="BYBIT")
            self.assertEqual(len(bybit.submits), 1)                                              # every earlier caller behaves as before
            store.close()


class ScriptedAccount:
    """A venue's account endpoints, scripted: `account_snapshot(since_ns)` returns the snapshot given, or fails."""

    def __init__(self, snapshot=None, fail=False):
        self.snapshot, self.fail, self.calls = snapshot, fail, []

    def account_snapshot(self, since_ns):
        self.calls.append(since_ns)
        if self.fail:
            raise ConnectionError("account endpoint down")
        return self.snapshot


class ExchangeSideRestartReconciliationTests(RestartAgainstTheVenueTests):
    """Owner 13 Nov: after a restart the OMS fetches every venue's open orders, executions and positions, books what happened while it was down once, and takes no new maker exposure until they agree.
    Scripted venues only; a testnet restart drill comes later."""

    def _account(self, maker, state, filled, *, executions=None, positions=None, extra_orders=(), extra_executions=()):
        from crypto_stat_arb.plan_store import VenueAccountSnapshot, VenueExecution
        snap = self._snapshot(maker, state, filled)
        rows = tuple(VenueExecution(eid, maker.order_id, "BYBIT", INSTR_FT, Decimal(q), Decimal("60060"), 400 * MS + i) for i, (eid, q) in enumerate(executions or [])) + tuple(extra_executions)
        bybit = VenueAccountSnapshot("BYBIT", (snap, *extra_orders), rows, positions if positions is not None else {}, 550 * MS)
        binance = VenueAccountSnapshot("BINANCE", (), (), {}, 550 * MS)
        return ScriptedAccount(bybit), ScriptedAccount(binance)

    def test_missed_fill_is_booked_once_hedged_once_and_the_gate_opens_only_when_everything_agrees(self):
        from crypto_stat_arb.models import OrderState
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "oms.sqlite"
            parent_id, maker = self._first_session(plan_path)
            oms, store, bybit, binance = self._restart(plan_path, gate=False)
            by_acct, bn_acct = self._account(maker, OrderState.FILLED, "0.010", executions=[("e1", "0.004"), ("e2", "0.006"), ("e2", "0.006")], positions={INSTR_FT: Decimal("-0.010")})    # e2 listed twice: one execution
            report = oms.reconcile_account_after_restart({"BYBIT": by_acct, "BINANCE": bn_acct}, 600 * MS, books=_ft_books())
            self.assertEqual(report["problems"], {}); self.assertTrue(report["gate_open"])
            self.assertTrue(oms.restart_reconcile_gate)                                          # the call closes the gate itself, whatever the caller set
            self.assertEqual(len([e for e in self._fills(oms) if e.get("order_id") == maker.order_id]), 1)    # booked once
            self.assertEqual(len(binance.submits), 1)                                           # the hedge, sent once
            self.assertEqual(oms.ledger.positions[("BYBIT", INSTR_FT)], Decimal("-0.010"))
            self.assertEqual(oms.ledger.positions[("BINANCE", INSTR_FT)], Decimal("0.010"))     # flat net
            # the same account fetched again (a second restart, a repeated call): nothing booked, nothing sent
            again_by, again_bn = self._account(maker, OrderState.FILLED, "0.010", executions=[("e1", "0.004"), ("e2", "0.006")], positions={INSTR_FT: Decimal("-0.010")})
            again_bn.snapshot = type(again_bn.snapshot)("BINANCE", (), (), {INSTR_FT: Decimal("0.010")}, 650 * MS)
            report = oms.reconcile_account_after_restart({"BYBIT": again_by, "BINANCE": again_bn}, 700 * MS, books=_ft_books())
            self.assertTrue(report["gate_open"], report)
            self.assertEqual(len([e for e in self._fills(oms) if e.get("order_id") == maker.order_id]), 1)
            self.assertEqual(len(binance.submits), 1)
            oms.execute(_ft_opportunity("opp-new-1"), _ft_books(), maker_venue="BYBIT")          # agreed: new maker exposure may go out
            self.assertEqual((len(bybit.submits), self._held(oms)), (1, []))
            write_case("case10_exchange_side_restart_reconciliation_missed_fill", oms.ledger.events)
            store.close()

    def test_the_live_adapters_snapshot_from_recorded_bybit_messages_reconciles_the_plan_by_client_id(self):
        """The snapshot comes through `TestnetVenueAdapter.account_snapshot` and the Bybit linear transport from hand-written v5 messages (orderLinkId = the client id the OMS sent): the missed fill is booked once,
        hedged once, and the gate opens. No network."""
        from crypto_stat_arb.sdk import TestnetVenueAdapter
        from crypto_stat_arb.plan_store import VenueAccountSnapshot
        from crypto_stat_arb.testnet import BybitV5LinearTestnetTransport
        from test_forward_testnet_calibration import FakeHttp, SECRET
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "oms.sqlite"
            parent_id, maker = self._first_session(plan_path)
            oms, store, bybit, binance = self._restart(plan_path, gate=False)
            link = maker.order_id
            order = {"orderLinkId": link, "orderId": "v-1", "orderStatus": "Filled", "cumExecQty": "0.010", "avgPrice": "60060", "cumExecFee": "0", "updatedTime": "500", "createdTime": "100"}
            execution = lambda eid, q: {"execId": eid, "orderId": "v-1", "orderLinkId": link, "execPrice": "60060", "execQty": q, "execFee": "0", "execTime": "400", "isMaker": True, "execType": "Trade"}
            http = FakeHttp([
                (200, {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}),                                               # order/realtime: nothing open any more
                (200, {"retCode": 0, "result": {"list": [order], "nextPageCursor": ""}}),                                          # order/history
                (200, {"retCode": 0, "result": {"list": [execution("e1", "0.004"), execution("e2", "0.006")], "nextPageCursor": ""}}),
                (200, {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "side": "Sell", "size": "0.010", "positionIdx": 0}], "nextPageCursor": ""}}),
            ])
            transport = BybitV5LinearTestnetTransport(symbols={INSTR_FT: "BTCUSDT"}, http=http, clock_ms=lambda: 1000)
            live = TestnetVenueAdapter("BYBIT", lambda req: transport(req, SECRET))
            quiet = ScriptedAccount(VenueAccountSnapshot("BINANCE", (), (), {}, 550 * MS))
            report = oms.reconcile_account_after_restart({"BYBIT": live, "BINANCE": quiet}, 600 * MS, books=_ft_books())
            self.assertEqual(report["problems"], {})
            self.assertTrue(report["gate_open"], report)
            self.assertEqual(len([e for e in self._fills(oms) if e.get("order_id") == maker.order_id]), 1)
            self.assertEqual(len(binance.submits), 1)
            self.assertEqual(oms.ledger.positions[("BYBIT", INSTR_FT)], Decimal("-0.010"))
            store.close()

    def test_a_restart_with_no_persisted_state_asks_the_venue_for_the_default_lookback_not_the_epoch(self):
        """No open plan and nothing persisted: `since` would be 0, which a real venue cannot answer (the adapter refuses anything older than the venue keeps). The OMS asks for the last 24 hours instead, and the gate
        opens on an empty, agreeing account. Epoch clock, recorded messages."""
        from urllib.parse import parse_qs, urlparse
        from crypto_stat_arb.sdk import TestnetVenueAdapter
        from crypto_stat_arb.plan_store import VenueAccountSnapshot
        from crypto_stat_arb.testnet import BybitV5LinearTestnetTransport
        from test_forward_testnet_calibration import FakeHttp, SECRET
        now_ms = 1_700_000_100_000
        empty = (200, {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}})
        http = FakeHttp([empty, empty, empty, (200, {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "side": "", "size": "0", "positionIdx": 0}], "nextPageCursor": ""}})])
        transport = BybitV5LinearTestnetTransport(symbols={INSTR_FT: "BTCUSDT"}, http=http, clock_ms=lambda: now_ms)
        live = TestnetVenueAdapter("BYBIT", lambda req: transport(req, SECRET))
        quiet = ScriptedAccount(VenueAccountSnapshot("BINANCE", (), (), {}, 1))
        oms = OMS(_ft_instruments(), risk(), Ledger(), router=StrategyDirectedRouter(_ft_instruments(), adapters={}), dispatch_policy=DispatchPolicy.FILL_TRIGGERED, hedge_policy=HedgePolicy(retry_limit=1))
        self.assertEqual(oms._last_execution_ts_ns, 0)
        report = oms.reconcile_account_after_restart({"BYBIT": live, "BINANCE": quiet}, now_ms * 1_000_000)
        self.assertEqual((report["since_basis"], report["problems"], report["gate_open"]), ("default_lookback", {}, True))
        self.assertEqual(parse_qs(urlparse(http.calls[1][1]).query)["startTime"], [str(now_ms - 24 * 3_600_000)])

    def test_an_order_still_resting_at_the_venue_is_reconciled_without_being_finished(self):
        from crypto_stat_arb.models import OrderState
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "oms.sqlite"
            parent_id, maker = self._first_session(plan_path)
            oms, store, bybit, binance = self._restart(plan_path, gate=False)
            by_acct, bn_acct = self._account(maker, OrderState.ACKNOWLEDGED, "0", positions={INSTR_FT: Decimal("0")})
            report = oms.reconcile_account_after_restart({"BYBIT": by_acct, "BINANCE": bn_acct}, 600 * MS, books=_ft_books())
            self.assertTrue(report["gate_open"], report)
            self.assertEqual(binance.submits, [])                                               # nothing filled, nothing to hedge
            oms.execute(_ft_opportunity("opp-new-2"), _ft_books(), maker_venue="BYBIT")
            self.assertEqual(len(bybit.submits), 1)
            store.close()

    def test_an_unknown_order_a_position_that_differs_or_a_failed_fetch_each_keep_new_risk_held(self):
        from crypto_stat_arb.models import OrderState
        from crypto_stat_arb.plan_store import VenueOrderSnapshot
        stray = VenueOrderSnapshot(order_id="someone-elses-order", venue="BYBIT", instrument_id=INSTR_FT, state=OrderState.ACKNOWLEDGED, filled_quantity=Decimal("0"), average_price=Decimal("0"), venue_order_id="v-9")
        cases = {
            "unknown_venue_order": lambda m: self._account(m, OrderState.ACKNOWLEDGED, "0", positions={INSTR_FT: Decimal("0")}, extra_orders=(stray,)),
            "venue_position_differs": lambda m: self._account(m, OrderState.ACKNOWLEDGED, "0", positions={INSTR_FT: Decimal("-0.010")}),
            "account_fetch_failed": lambda m: (ScriptedAccount(fail=True), ScriptedAccount(self._account(m, OrderState.ACKNOWLEDGED, "0")[1].snapshot)),
        }
        for label, build in cases.items():
            with tempfile.TemporaryDirectory() as directory:
                plan_path = Path(directory) / "oms.sqlite"
                parent_id, maker = self._first_session(plan_path)
                oms, store, bybit, binance = self._restart(plan_path, gate=False)
                by_acct, bn_acct = build(maker)
                report = oms.reconcile_account_after_restart({"BYBIT": by_acct, "BINANCE": bn_acct}, 600 * MS, books=_ft_books())
                self.assertFalse(report["gate_open"], label)
                self.assertTrue(any(label in p for p in report["problems"]["BYBIT"]), (label, report))
                oms.execute(_ft_opportunity("opp-held"), _ft_books(), maker_venue="BYBIT")
                self.assertEqual((bybit.submits, self._held(oms)), ([], ["opp-held"]), label)          # no new maker exposure
                self.assertFalse(any(e["event_type"] == "ORDER_CANCEL_REQUESTED" for e in oms.ledger.events), label)   # an order that may not be ours is flagged, never cancelled
                store.close()

    def test_a_failed_fetch_books_nothing_and_a_later_good_fetch_opens_the_gate(self):
        from crypto_stat_arb.models import OrderState
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "oms.sqlite"
            parent_id, maker = self._first_session(plan_path)
            oms, store, bybit, binance = self._restart(plan_path, gate=False)
            good_by, good_bn = self._account(maker, OrderState.FILLED, "0.010", executions=[("e1", "0.010")], positions={INSTR_FT: Decimal("-0.010")})
            report = oms.reconcile_account_after_restart({"BYBIT": ScriptedAccount(fail=True), "BINANCE": good_bn}, 600 * MS, books=_ft_books())
            self.assertFalse(report["gate_open"])
            self.assertEqual((self._fills(oms), binance.submits), ([], []))
            report = oms.reconcile_account_after_restart({"BYBIT": good_by, "BINANCE": good_bn}, 700 * MS, books=_ft_books())
            self.assertTrue(report["gate_open"], report)
            self.assertEqual(len(binance.submits), 1)
            store.close()

    def test_executions_that_do_not_add_up_to_the_order_state_keep_the_gate_closed(self):
        from crypto_stat_arb.models import OrderState
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "oms.sqlite"
            parent_id, maker = self._first_session(plan_path)
            oms, store, bybit, binance = self._restart(plan_path, gate=False)
            by_acct, bn_acct = self._account(maker, OrderState.FILLED, "0.010", executions=[("e1", "0.004")], positions={INSTR_FT: Decimal("-0.010")})
            report = oms.reconcile_account_after_restart({"BYBIT": by_acct, "BINANCE": bn_acct}, 600 * MS, books=_ft_books())
            self.assertFalse(report["gate_open"])
            self.assertTrue(any(p.startswith("plan_unreconciled") for p in report["problems"]["BYBIT"]), report)
            store.close()
