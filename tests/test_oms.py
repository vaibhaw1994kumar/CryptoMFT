from dataclasses import replace
from decimal import Decimal
import unittest

from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import MarketEvent, Opportunity, PlanState
from crypto_stat_arb.oms import OMS
from crypto_stat_arb.execution import (
    DeterministicVenueAdapter,
    ExecutionCallback,
    ExecutionEventType,
    RateLimitPolicy,
    ResidualPolicy,
    StrategyDirectedRouter,
    UnwindUrgency,
    VenueRouteStatus,
)
from crypto_stat_arb.orderbook import OrderBook
from crypto_stat_arb.risk import AccountRiskSnapshot, RiskEngine, RiskLimits


def instrument_master():
    from crypto_stat_arb.instruments import InstrumentMaster
    from crypto_stat_arb.models import Instrument

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


def make_book(venue: str, bid: str, ask: str, qty: str = "1") -> OrderBook:
    book = OrderBook(venue, "BTC-USDT-SPOT")
    book.apply(MarketEvent.from_dict({
        "event_type": "BOOK_SNAPSHOT",
        "venue": venue,
        "instrument_id": "BTC-USDT-SPOT",
        "exchange_ts_ns": 1,
        "receive_ts_ns": 1,
        "sequence": 1,
        "bids": [[bid, qty]],
        "asks": [[ask, qty]],
    }))
    return book


def opportunity(qty: str = "0.01") -> Opportunity:
    return Opportunity(
        opportunity_id="op-1",
        strategy_id="s-1",
        instrument_id="BTC-USDT-SPOT",
        observed_ts_ns=2,
        buy_venue="BINANCE",
        sell_venue="BYBIT",
        buy_price=Decimal("60000.00"),
        sell_price=Decimal("60060.00"),
        quantity=Decimal(qty),
        gross_edge_bps=Decimal("10"),
        estimated_net_edge_bps=Decimal("2"),
        source_event_ids=("a", "b"),
    )


def risk() -> RiskEngine:
    return RiskEngine(RiskLimits(
        max_order_notional=Decimal("5000"),
        max_gross_notional=Decimal("10000"),
        max_residual_base=Decimal("0.002"),
        max_book_age_ns=1_000_000_000,
    ))


class RejectingAdapter:
    def handle(self, request, book):
        return (ExecutionCallback(
            schema_version="1.0.0",
            callback_id=f"reject-{request.request_id}",
            request_id=request.request_id,
            order_id=request.order_id,
            parent_opportunity_id=request.parent_opportunity_id,
            parent_order_id=request.parent_order_id,
            venue=request.venue,
            instrument_id=request.instrument_id,
            event_type=ExecutionEventType.REJECTED,
            event_ts_ns=request.created_ts_ns,
            reason="injected venue rejection",
        ),)


class OMSTests(unittest.TestCase):
    def setUp(self):
        self.instruments = instrument_master()
        self.books = {
            ("BINANCE", "BTC-USDT-SPOT"): make_book("BINANCE", "59999.90", "60000.00"),
            ("BYBIT", "BTC-USDT-SPOT"): make_book("BYBIT", "60060.00", "60060.10"),
        }

    def test_normal_two_leg_execution_reconciles(self):
        ledger = Ledger()
        oms = OMS(self.instruments, risk(), ledger)
        plan = oms.execute(opportunity(), self.books)
        self.assertEqual(plan.state, PlanState.CLOSED)
        self.assertEqual(plan.residual_base, 0)
        self.assertEqual(plan.gross_pnl, Decimal("0.6000"))
        self.assertEqual(plan.net_pnl, Decimal("0.119760"))
        self.assertEqual(sum(ledger.positions.values()), 0)

    def test_two_leg_dispatch_uses_stable_sequence_and_independent_timestamps(self):
        ledger = Ledger()
        oms = OMS(self.instruments, risk(), ledger)
        oms.execute(opportunity(), self.books)
        starts = [
            event for event in ledger.events
            if event["event_type"] == "CHILD_DISPATCH_STARTED"
        ]
        callbacks = [
            event for event in ledger.events
            if event["event_type"] == "CHILD_CALLBACK_OBSERVED"
        ]
        self.assertEqual(
            [(event["leg_index"], event["venue"]) for event in starts],
            [(0, "BINANCE"), (1, "BYBIT")],
        )
        self.assertEqual(len(callbacks), 4)
        timestamps = [event["ts_ns"] for event in starts + callbacks]
        self.assertEqual(len(timestamps), len(set(timestamps)))
        measurements = oms.dispatch_stats_snapshot()
        self.assertEqual(measurements["policy"], "DETERMINISTIC_SEQUENTIAL")
        self.assertEqual(measurements["children_dispatched"], 2)
        self.assertEqual(measurements["callbacks_observed"], 4)

    def test_throttled_package_stays_open_until_both_children_release(self):
        ledger = Ledger()
        router = StrategyDirectedRouter(
            self.instruments,
            route_status={
                venue: VenueRouteStatus(
                    rate_limit_capacity=1, rate_limit_used=1,
                    rate_limit_window_ns=1_000,
                    rate_limit_policy=RateLimitPolicy.QUEUE,
                )
                for venue in ("BINANCE", "BYBIT")
            },
        )
        engine = risk()
        oms = OMS(self.instruments, engine, ledger, router=router)
        plan = oms.execute(opportunity(), self.books)
        self.assertNotEqual(plan.state, PlanState.CLOSED)
        self.assertEqual(router.rate_limit_snapshot()["pending_requests"], 2)
        self.assertEqual(engine.stats_snapshot()["active_reservations"], 1)
        self.assertEqual(oms.process_throttled_routes(999), ())
        released = oms.process_throttled_routes(1_000)
        self.assertEqual(len(released), 2)
        self.assertEqual(plan.state, PlanState.CLOSED)
        self.assertEqual(plan.terminal_outcome, PlanState.FILLED.value)
        self.assertEqual(plan.residual_base, Decimal("0"))
        self.assertEqual(sum(ledger.positions.values()), Decimal("0"))
        self.assertEqual(engine.stats_snapshot()["active_reservations"], 0)
        event_types = [event["event_type"] for event in ledger.events]
        self.assertIn("PACKAGE_DISPATCH_THROTTLED", event_types)
        self.assertEqual(event_types.count("OMS_THROTTLED_ROUTE_RELEASED"), 2)

    def test_pm_kill_cancels_queued_orders_and_reconciles_before_recovery(self):
        ledger = Ledger()
        route_status = {
            venue: VenueRouteStatus(
                rate_limit_capacity=1, rate_limit_used=1,
                rate_limit_window_ns=1_000,
                rate_limit_policy=RateLimitPolicy.QUEUE,
            )
            for venue in ("BINANCE", "BYBIT")
        }
        engine = risk()
        router = StrategyDirectedRouter(
            self.instruments, route_status=route_status
        )
        oms = OMS(self.instruments, engine, ledger, router=router)
        queued = oms.execute(opportunity(), self.books)
        self.assertEqual(router.rate_limit_snapshot()["pending_requests"], 2)

        active = oms.activate_kill_switch(
            reason="PM emergency stop", now_ns=10,
            operator_id="pm-001",
        )
        self.assertEqual(active["state"], "ACTIVE")
        self.assertFalse(active["strategy_new_risk_allowed"])
        self.assertTrue(active["risk_reducing_orders_allowed"])
        self.assertTrue(active["reconciliation_required_before_resume"])
        self.assertEqual(router.rate_limit_snapshot()["pending_requests"], 0)
        self.assertEqual(queued.state, PlanState.CLOSED)
        cancel_event = next(
            event for event in ledger.events
            if event["event_type"] == "KILL_SWITCH_CANCEL_ALL_COMPLETE"
        )
        self.assertEqual(cancel_event["cancelled_orders"], 2)

        blocked = oms.execute(replace(
            opportunity(), opportunity_id="op-blocked", observed_ts_ns=20
        ), self.books)
        self.assertEqual(blocked.terminal_outcome, PlanState.REJECTED.value)

        recovered = oms.recover_kill_switch(
            now_ns=30, operator_id="pm-001", require_venue_orders=False,
        )
        self.assertEqual(recovered["state"], "INACTIVE")
        self.assertFalse(recovered["reconciliation_required_before_resume"])
        self.assertTrue(recovered["last_reset_reconciliation_id"])
        self.assertEqual(
            [event["action"] for event in recovered["events"]],
            ["ACTIVATED", "RESET"],
        )

        for status in route_status.values():
            status.rate_limit_capacity = 1_000
            status.rate_limit_used = 0
            status.rate_limit_policy = RateLimitPolicy.REJECT
        after_reset = oms.execute(replace(
            opportunity(), opportunity_id="op-after-reset", observed_ts_ns=40
        ), self.books)
        self.assertEqual(after_reset.state, PlanState.CLOSED)
        self.assertEqual(after_reset.terminal_outcome, PlanState.FILLED.value)

    def test_failed_leg_dispatches_reduce_only_unwind_and_finishes_flat(self):
        bad_books = dict(self.books)
        ledger = Ledger()
        engine = risk()
        oms = OMS(
            self.instruments, engine, ledger,
            router=StrategyDirectedRouter(
                self.instruments, adapters={"BYBIT": RejectingAdapter()}
            ),
            residual_policy=ResidualPolicy(
                max_residual_base=Decimal("0.002"),
                max_residual_age_ns=5_000_000_000,
                hedge_venue_priority=("BINANCE", "BYBIT"),
                unwind_urgency=UnwindUrgency.NORMAL,
                unwind_price_slippage_bps=Decimal("10"),
            ),
        )
        plan = oms.execute(opportunity(), bad_books)
        unwind = [order for order in plan.children if order.order_role == "UNWIND"]
        self.assertEqual(plan.state, PlanState.CLOSED)
        self.assertEqual(plan.terminal_outcome, PlanState.REJECTED.value)
        self.assertEqual(plan.residual_base, Decimal("0"))
        self.assertEqual(len(unwind), 1)
        self.assertTrue(unwind[0].reduce_only)
        self.assertEqual(unwind[0].side.value, "SELL")
        self.assertEqual(engine.stats_snapshot()["active_reservations"], 0)
        self.assertEqual(oms.residual_risk_snapshot()["open_residual_plans"], 0)
        self.assertEqual(oms.residual_risk_snapshot()["unwind_children"], 1)
        self.assertIn(
            "UNWIND_INTENT", [event["event_type"] for event in ledger.events]
        )

    def test_risk_reducing_unwind_continues_under_strategy_stop(self):
        bad_books = dict(self.books)
        engine = risk()

        class StopAfterFillAdapter:
            def __init__(self):
                self.adapter = DeterministicVenueAdapter("BINANCE")

            def handle(self, request, book):
                callbacks = self.adapter.handle(request, book)
                engine.kill_switch = True
                return callbacks

        ledger = Ledger()
        oms = OMS(
            self.instruments, engine, ledger,
            router=StrategyDirectedRouter(
                self.instruments, adapters={
                    "BINANCE": StopAfterFillAdapter(),
                    "BYBIT": RejectingAdapter(),
                }
            ),
            residual_policy=ResidualPolicy(
                max_residual_base=Decimal("0"),
                max_residual_age_ns=1_000,
                hedge_venue_priority=("BINANCE",),
                allow_risk_reducing_under_stop=True,
            ),
        )
        plan = oms.execute(opportunity(), bad_books)
        self.assertTrue(engine.kill_switch)
        self.assertEqual(plan.state, PlanState.CLOSED)
        self.assertEqual(plan.residual_base, Decimal("0"))
        self.assertTrue(any(
            event["event_type"] == "RISK_REDUCING_STOP_OVERRIDE"
            for event in ledger.events
        ))

    def test_binding_one_sided_leg_hedges_through_global_and_venue_controls(self):
        engine = risk()
        route_status = {
            "BINANCE": VenueRouteStatus(),
            "BYBIT": VenueRouteStatus(),
        }

        class BindControlsAfterFirstLeg:
            def __init__(self):
                self.adapter = DeterministicVenueAdapter("BINANCE")

            def handle(self, request, book):
                callbacks = self.adapter.handle(request, book)
                engine.kill_switch = True
                route_status["BYBIT"].venue_enabled = False
                route_status["BYBIT"].rate_limit_capacity = 0
                return callbacks

        ledger = Ledger()
        oms = OMS(
            self.instruments, engine, ledger,
            router=StrategyDirectedRouter(
                self.instruments,
                adapters={"BINANCE": BindControlsAfterFirstLeg()},
                route_status=route_status,
            ),
            residual_policy=ResidualPolicy(
                max_residual_base=Decimal("0.002"),
                max_residual_age_ns=5_000_000_000,
                hedge_venue_priority=("BINANCE", "BYBIT"),
                max_one_sided_notional=Decimal("500"),
            ),
        )
        plan = oms.execute(opportunity(), self.books)

        self.assertEqual(plan.state, PlanState.CLOSED)
        self.assertEqual(plan.residual_base, Decimal("0"))
        bound = next(
            event for event in ledger.events
            if event["event_type"] == "LEGGING_LIMIT_BOUND"
        )
        self.assertEqual(bound["action"], "DISPATCH_REMAINING_HEDGE")
        self.assertEqual(set(bound["binding_codes"]), {
            "MAX_RESIDUAL_BASE", "MAX_ONE_SIDED_NOTIONAL",
        })
        override = next(
            event for event in ledger.events
            if event["event_type"] == "LEGGING_CONTROL_OVERRIDE"
        )
        self.assertEqual(override["action"], "ALLOW_RISK_REDUCING_HEDGE")
        self.assertIn("GLOBAL:kill_switch", override["controls"])
        self.assertIn("BYBIT:venue_disabled", override["controls"])
        self.assertIn("BYBIT:rate_limit_exhausted", override["controls"])
        self.assertTrue(any(
            event["event_type"] == "LEGGING_LIMIT_CLEARED"
            for event in ledger.events
        ))

    def test_out_of_bound_residual_cannot_close_when_unwind_fails(self):
        bad_books = dict(self.books)
        engine = risk()
        ledger = Ledger()
        oms = OMS(
            self.instruments, engine, ledger,
            router=StrategyDirectedRouter(
                self.instruments, adapters={"BYBIT": RejectingAdapter()}
            ),
            residual_policy=ResidualPolicy(
                max_residual_base=Decimal("0.002"),
                max_residual_age_ns=1,
                hedge_venue_priority=("BYBIT",),
                unwind_urgency=UnwindUrgency.PASSIVE,
                max_unwind_attempts=1,
            ),
        )
        plan = oms.execute(opportunity(), bad_books)
        self.assertEqual(plan.state, PlanState.HEDGE_UNWIND)
        self.assertGreater(abs(plan.residual_base), Decimal("0.002"))
        self.assertEqual(engine.stats_snapshot()["active_reservations"], 1)
        self.assertTrue(any(
            event["event_type"] == "RESIDUAL_LIMIT_BREACH"
            for event in ledger.events
        ))

    def test_residual_age_deadline_escalates_unwind_to_market(self):
        bad_books = dict(self.books)
        ledger = Ledger()
        oms = OMS(
            self.instruments, risk(), ledger,
            router=StrategyDirectedRouter(
                self.instruments, adapters={"BYBIT": RejectingAdapter()}
            ),
            residual_policy=ResidualPolicy(
                max_residual_base=Decimal("0"),
                max_residual_age_ns=0,
                hedge_venue_priority=("BINANCE",),
                unwind_urgency=UnwindUrgency.PASSIVE,
            ),
        )
        oms.execute(opportunity(), bad_books)
        intent = next(
            event for event in ledger.events
            if event["event_type"] == "UNWIND_INTENT"
        )
        self.assertEqual(intent["urgency"], UnwindUrgency.AGGRESSIVE.value)
        self.assertEqual(intent["order_type"], "MARKET")
        self.assertIsNone(intent["price"])

    def test_passive_unwind_is_cancelled_and_escalated_at_deadline(self):
        bad_books = dict(self.books)
        ledger = Ledger()
        oms = OMS(
            self.instruments, risk(), ledger,
            router=StrategyDirectedRouter(
                self.instruments, adapters={"BYBIT": RejectingAdapter()}
            ),
            residual_policy=ResidualPolicy(
                max_residual_base=Decimal("0"),
                max_residual_age_ns=100,
                hedge_venue_priority=("BINANCE",),
                unwind_urgency=UnwindUrgency.PASSIVE,
                max_unwind_attempts=2,
            ),
        )
        plan = oms.execute(opportunity(), bad_books)
        self.assertEqual(plan.state, PlanState.HEDGE_UNWIND)
        self.assertEqual(plan.children[-1].state.value, "ACKNOWLEDGED")
        oms.enforce_residual_limits(1_000, bad_books)
        self.assertEqual(plan.state, PlanState.CLOSED)
        self.assertEqual(plan.residual_base, Decimal("0"))
        intents = [
            event for event in ledger.events if event["event_type"] == "UNWIND_INTENT"
        ]
        self.assertEqual([event["urgency"] for event in intents], [
            UnwindUrgency.PASSIVE.value, UnwindUrgency.AGGRESSIVE.value,
        ])
        self.assertTrue(any(
            event["event_type"] == "UNWIND_DEADLINE_CANCEL_CALLBACK"
            for event in ledger.events
        ))

    def test_invalid_feed_cancels_ordinary_risk_but_preserves_unwind(self):
        ledger = Ledger()
        oms = OMS(
            self.instruments, risk(), ledger,
            router=StrategyDirectedRouter(
                self.instruments, adapters={"BYBIT": RejectingAdapter()}
            ),
            residual_policy=ResidualPolicy(
                max_residual_base=Decimal("0"),
                max_residual_age_ns=100,
                hedge_venue_priority=("BINANCE",),
                unwind_urgency=UnwindUrgency.PASSIVE,
                max_unwind_attempts=2,
            ),
        )
        plan = oms.execute(opportunity(), self.books)
        unwind = next(
            order for order in plan.children if order.order_role == "UNWIND"
        )
        self.assertEqual(unwind.state.value, "ACKNOWLEDGED")

        cancelled = oms.apply_feed_control(
            "BINANCE", False, "STALE:market_data_stale", 100
        )
        self.assertEqual(cancelled, 0)
        self.assertEqual(unwind.state.value, "ACKNOWLEDGED")
        self.assertFalse(oms.router.route_status["BINANCE"].venue_enabled)
        transition = next(
            event for event in ledger.events
            if event["event_type"] == "FEED_CONTROL_STATE_TRANSITION"
        )
        self.assertEqual(transition["mode"], "RISK_REDUCING_ONLY")

        oms.apply_feed_control("BINANCE", True, "", 101)
        self.assertTrue(oms.router.route_status["BINANCE"].venue_enabled)
        self.assertEqual([
            event["mode"] for event in ledger.events
            if event["event_type"] == "FEED_CONTROL_STATE_TRANSITION"
        ][-2:], ["RISK_REDUCING_ONLY", "ACTIVE"])

    def test_parent_state_transitions_are_timestamped_in_ledger(self):
        ledger = Ledger()
        OMS(self.instruments, risk(), ledger).execute(opportunity(), self.books)
        transitions = [
            event for event in ledger.events
            if event["event_type"] == "PLAN_STATE_TRANSITION"
        ]
        self.assertEqual([event["state"] for event in transitions], [
            "CREATED", "RISK_RESERVED", "DISPATCHED", "ACKNOWLEDGED",
            "PARTIAL", "FILLED",
            "RECONCILED", "CLOSED",
        ])
        self.assertTrue(all(isinstance(event["ts_ns"], int) for event in transitions))
        self.assertTrue(all(event["parent_id"] for event in transitions))
        state_machine = OMS(self.instruments, risk(), Ledger())
        state_machine.execute(opportunity(), self.books)
        stats = state_machine.state_machine_stats_snapshot()
        self.assertEqual(stats["terminal_parent_state"], "CLOSED")
        self.assertEqual(stats["terminal_outcomes"], {"FILLED": 1})
        self.assertEqual(stats["updates_by_source"]["ADAPTER_CALLBACK"], 4)

    def test_parent_idempotency_prevents_duplicate_orders(self):
        ledger = Ledger()
        oms = OMS(self.instruments, risk(), ledger)
        first = oms.execute(opportunity(), self.books)
        second = oms.execute(opportunity(), self.books)
        self.assertIs(first, second)
        self.assertEqual(len([x for x in ledger.events if x["event_type"] == "FILL"]), 2)

    def test_package_risk_rejects_before_dispatch(self):
        limited = RiskEngine(RiskLimits(
            max_order_notional=Decimal("100"),
            max_gross_notional=Decimal("200"),
            max_residual_base=Decimal("0.002"),
            max_book_age_ns=1_000_000_000,
        ))
        oms = OMS(self.instruments, limited, Ledger())
        plan = oms.execute(opportunity(), self.books)
        self.assertEqual(plan.state, PlanState.CLOSED)
        self.assertEqual(plan.terminal_outcome, PlanState.REJECTED.value)
        self.assertTrue(all(order.filled_quantity == 0 for order in plan.children))

    def test_package_reserves_and_releases_cash_margin_position_and_messages(self):
        engine = RiskEngine(risk().limits, {
            "BINANCE": AccountRiskSnapshot(
                venue="BINANCE",
                available_cash=Decimal("1000"),
                available_margin=Decimal("1000"),
                max_abs_position_base=Decimal("0.02"),
                message_capacity=10,
                initial_margin_rate=Decimal("0.10"),
            ),
            "BYBIT": AccountRiskSnapshot(
                venue="BYBIT",
                available_cash=Decimal("0"),
                available_margin=Decimal("1000"),
                max_abs_position_base=Decimal("0.02"),
                message_capacity=10,
                initial_margin_rate=Decimal("0.10"),
            ),
        })
        reservation = engine.reserve(opportunity())
        self.assertEqual(reservation.cash_by_venue["BINANCE"], Decimal("600.0000"))
        self.assertEqual(reservation.margin_by_venue["BYBIT"], Decimal("60.06000"))
        self.assertEqual(reservation.position_delta_by_venue["BINANCE"], Decimal("0.01"))
        self.assertEqual(reservation.position_delta_by_venue["BYBIT"], Decimal("-0.01"))
        self.assertEqual(reservation.messages_by_venue, {"BINANCE": 1, "BYBIT": 1})
        engine.release(reservation)
        self.assertEqual(engine.stats_snapshot()["active_reservations"], 0)
        self.assertEqual(engine.reserved_gross, Decimal("0"))

    def test_margin_failure_does_not_mutate_any_reservation_bucket(self):
        engine = RiskEngine(risk().limits, {
            "BINANCE": AccountRiskSnapshot(
                venue="BINANCE", available_cash=Decimal("1000"),
                available_margin=Decimal("1000"), initial_margin_rate=Decimal("0.1")
            ),
            "BYBIT": AccountRiskSnapshot(
                venue="BYBIT", available_margin=Decimal("1"),
                initial_margin_rate=Decimal("0.1")
            ),
        })
        with self.assertRaisesRegex(ValueError, "insufficient margin: BYBIT"):
            engine.reserve(opportunity())
        self.assertEqual(engine.reserved_gross, Decimal("0"))
        self.assertEqual(engine.reserved_cash, {})
        self.assertEqual(engine.reserved_margin, {})
        self.assertEqual(engine.reserved_position, {})
        self.assertEqual(engine.reserved_messages, {})

    def test_message_limit_rejects_entire_plan_before_either_child_dispatches(self):
        class CountingAdapter:
            def __init__(self):
                self.calls = 0

            def handle(self, request, book):
                self.calls += 1
                return ()

        binance_adapter = CountingAdapter()
        bybit_adapter = CountingAdapter()
        engine = RiskEngine(risk().limits, {
            "BINANCE": AccountRiskSnapshot(
                venue="BINANCE", available_cash=Decimal("1000"),
                message_capacity=10
            ),
            "BYBIT": AccountRiskSnapshot(
                venue="BYBIT", message_capacity=0
            ),
        })
        router = StrategyDirectedRouter(
            self.instruments,
            adapters={"BINANCE": binance_adapter, "BYBIT": bybit_adapter},
        )
        ledger = Ledger()
        plan = OMS(
            self.instruments, engine, ledger, router=router
        ).execute(opportunity(), self.books)
        self.assertEqual(plan.state, PlanState.CLOSED)
        self.assertEqual(plan.terminal_outcome, PlanState.REJECTED.value)
        self.assertEqual(binance_adapter.calls, 0)
        self.assertEqual(bybit_adapter.calls, 0)
        self.assertEqual(router.stats_snapshot()["unique_requests"], 0)
        self.assertEqual(engine.reserved_messages, {})
        rejected = [
            item for item in ledger.events
            if item["event_type"] == "PACKAGE_RISK_REJECTED"
        ]
        self.assertEqual(rejected[0]["dispatched_children"], 0)


if __name__ == "__main__":
    unittest.main()
