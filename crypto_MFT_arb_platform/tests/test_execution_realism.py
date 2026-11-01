"""Execution-realism, data-sanity and time-integrity regressions.

Each test pins one audit finding: latency never moved the market, liquidity
was sold twice, a lost report closed a plan with a hidden position, residual
breaches never escalated, 100x price jumps were VALID, and host-clock skew
blocked almost every live event.
"""

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.execution import (
    CanonicalOrderRequest,
    DeterministicVenueAdapter,
    ExecutionCallback,
    ExecutionEventType,
    OrderAction,
    OrderTimeoutPolicy,
    OrderType,
    ResidualPolicy,
    StrategyDirectedRouter,
    TimeInForce,
)
from crypto_stat_arb.fees import FeeRule, FeeSchedule, Liquidity
from crypto_stat_arb.instruments import InstrumentMaster
from crypto_stat_arb.latency import (
    LatencyDistribution, LatencyModel, VenueLatencyProfile,
    latency_model_from_config,
)
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import (
    BookState, Instrument, MarketEvent, OrderState, PlanState, Side,
)
from crypto_stat_arb.oms import OMS
from crypto_stat_arb.orderbook import OrderBook
from crypto_stat_arb.plan_store import VenueOrderSnapshot
from crypto_stat_arb.runner import run_backtest
from crypto_stat_arb.time_integrity import TimeIntegrityMonitor, TimePolicy
from test_oms import RejectingAdapter, instrument_master, make_book, opportunity, risk


ROOT = Path(__file__).parents[1]
INSTRUMENT = "BTC-USDT-SPOT"
MS = 1_000_000


def fixed(ms: float) -> LatencyDistribution:
    return LatencyDistribution(base_ns=int(ms * MS))


def fixed_model(entry_ms=10, ack_ms=5, fill_ms=5, cancel_ms=10) -> LatencyModel:
    return LatencyModel({
        venue: VenueLatencyProfile(
            venue=venue, market_data=fixed(0), strategy=fixed(0), risk=fixed(0),
            entry=fixed(entry_ms), submit_ack=fixed(ack_ms), fill=fixed(fill_ms),
            cancel=fixed(cancel_ms),
        )
        for venue in ("BINANCE", "BYBIT")
    }, seed=1, source="test-fixed")


def delta(venue, sequence, ts, bids, asks, event_type="BOOK_DELTA") -> MarketEvent:
    return MarketEvent.from_dict({
        "event_type": event_type, "venue": venue, "instrument_id": INSTRUMENT,
        "exchange_ts_ns": ts, "receive_ts_ns": ts, "sequence": sequence,
        "bids": bids, "asks": asks,
    })


def books():
    return {
        ("BINANCE", INSTRUMENT): make_book("BINANCE", "59990.00", "60000.00"),
        ("BYBIT", INSTRUMENT): make_book("BYBIT", "60060.00", "60070.00"),
    }


def submit(order_id, price, quantity, created=2, tif=TimeInForce.IOC):
    return CanonicalOrderRequest.create(
        action=OrderAction.SUBMIT, order_id=order_id,
        parent_opportunity_id="op", parent_order_id="parent",
        instrument_id=INSTRUMENT, venue="BINANCE", side=Side.BUY,
        price=Decimal(price), quantity=Decimal(quantity),
        order_type=OrderType.LIMIT, time_in_force=tif, created_ts_ns=created,
    )


class LosingReportAdapter(DeterministicVenueAdapter):
    """Venue executes normally but every fill/reject report is lost."""

    def handle(self, request, book):
        callbacks = super().handle(request, book)
        return tuple(
            item for item in callbacks
            if item.event_type == ExecutionEventType.ACKNOWLEDGED
        )


class UnreachableAdapter(DeterministicVenueAdapter):
    """No reports at all, and order queries fail."""

    def handle(self, request, book):
        super().handle(request, book)
        return ()

    def query_order(self, order_id):
        raise TimeoutError("venue unreachable")


class LatencyExecutionTests(unittest.TestCase):
    def test_orders_travel_and_execute_against_the_book_at_arrival(self):
        current = books()
        router = StrategyDirectedRouter(instrument_master(), latency_model=fixed_model())
        oms = OMS(instrument_master(), risk(), Ledger(), router=router)
        plan = oms.execute(opportunity(), current)
        self.assertEqual(plan.state, PlanState.DISPATCHED)
        # OMS02: dispatched and travelling, nothing heard back yet.
        self.assertTrue(all(child.state == OrderState.SENT for child in plan.children))

        # The Binance ask is lifted by someone else while our order travels.
        current[("BINANCE", INSTRUMENT)].apply(delta(
            "BINANCE", 2, 5 * MS, [], [["60000.00", "0"], ["60005.00", "1"]],
        ))
        oms.advance_time(12 * MS, current)
        self.assertTrue(all(child.state == OrderState.SENT for child in plan.children))
        oms.advance_time(20 * MS, current)
        buy, sell = plan.children[:2]
        self.assertEqual(buy.state, OrderState.REJECTED)
        self.assertEqual(sell.state, OrderState.FILLED)

        arrivals = {
            row["order_id"]: row for row in router.trace
            if row["record_type"] == "ORDER_ARRIVED"
        }
        sends = {
            row["order_id"]: row for row in router.trace
            if row["record_type"] == "ORDER_IN_FLIGHT"
        }
        self.assertEqual(arrivals[buy.order_id]["ts_ns"] - sends[buy.order_id]["ts_ns"], 10 * MS)
        delivered = [
            row for row in router.trace
            if row["record_type"] == "EXECUTION_CALLBACK_DELIVERED"
            and row["order_id"] == sell.order_id
        ]
        self.assertTrue(all(row["report_latency_ns"] == 5 * MS for row in delivered))

        oms.drain(1_000 * MS)
        self.assertEqual(plan.state, PlanState.CLOSED)
        self.assertEqual(plan.residual_base, Decimal("0"))
        unwind = next(child for child in plan.children if child.order_role == "UNWIND")
        self.assertEqual(unwind.average_price, Decimal("60005.00"))

    def test_same_displayed_liquidity_is_not_sold_twice(self):
        router = StrategyDirectedRouter(instrument_master())
        book = make_book("BINANCE", "59990.00", "60000.00", "0.010")
        first = router.route(submit("o1", "60000.00", "0.010"), book)
        self.assertEqual(first[-1].event_type, ExecutionEventType.FILLED)
        second = router.route(submit("o2", "60000.00", "0.010"), book)
        self.assertEqual([x.event_type for x in second], [ExecutionEventType.REJECTED])
        # Venue publishes a partial reduction: our take is still not reflected.
        book.apply(delta("BINANCE", 2, 3, [], [["60000.00", "0.004"]]))
        third = router.route(submit("o3", "60000.00", "0.004"), book)
        self.assertEqual(third[-1].event_type, ExecutionEventType.REJECTED)
        # Level removed and new liquidity published: withheld quantity released.
        book.apply(delta("BINANCE", 3, 4, [], [["60000.00", "0"], ["60000.10", "1"]]))
        fourth = router.route(submit("o4", "60000.10", "0.010"), book)
        self.assertEqual(fourth[-1].event_type, ExecutionEventType.FILLED)

    def test_ioc_remainder_is_cancelled_explicitly(self):
        router = StrategyDirectedRouter(instrument_master())
        book = make_book("BINANCE", "59990.00", "60000.00", "0.004")
        callbacks = router.route(submit("o1", "60000.00", "0.010"), book)
        self.assertEqual([x.event_type for x in callbacks], [
            ExecutionEventType.ACKNOWLEDGED, ExecutionEventType.PARTIAL,
            ExecutionEventType.CANCELLED,
        ])
        self.assertEqual(callbacks[-1].cumulative_filled_quantity, Decimal("0.004"))

    def test_resting_order_fills_when_book_trades_through(self):
        router = StrategyDirectedRouter(instrument_master())
        book = make_book("BINANCE", "59990.00", "60000.00", "1")
        router.route(submit("rest", "59995.00", "0.010", tif=TimeInForce.GTC), book)
        book.apply(delta("BINANCE", 2, 3, [["59990.00", "0"], ["59980.00", "1"]],
                         [["60000.00", "0"], ["59995.00", "1"]]))
        self.assertEqual(router.on_book_update("BINANCE", book, 3), 1)
        results = router.process_throttled(3)
        self.assertEqual(results[0].callbacks[0].event_type, ExecutionEventType.FILLED)
        self.assertEqual(results[0].callbacks[0].average_price, Decimal("59995.00"))


class UnknownOrderStateTests(unittest.TestCase):
    def test_lost_report_is_queried_before_hedging_and_never_resent(self):
        current = books()
        router = StrategyDirectedRouter(
            instrument_master(), adapters={"BYBIT": LosingReportAdapter("BYBIT")},
            latency_model=fixed_model(),
        )
        ledger = Ledger()
        oms = OMS(
            instrument_master(), risk(), ledger, router=router,
            order_timeout_policy=OrderTimeoutPolicy(
                report_timeout_ns=100 * MS, query_interval_ns=50 * MS, max_queries=3,
            ),
        )
        plan = oms.execute(opportunity(), current)
        oms.advance_time(50 * MS, current)
        sell = plan.children[1]
        self.assertEqual(sell.state, OrderState.ACKNOWLEDGED)
        self.assertNotEqual(plan.state, PlanState.CLOSED)
        self.assertFalse(any(child.order_role == "UNWIND" for child in plan.children))

        oms.advance_time(500 * MS, current)
        self.assertEqual(sell.state, OrderState.FILLED)
        self.assertEqual(plan.state, PlanState.CLOSED)
        self.assertEqual(plan.residual_base, Decimal("0"))
        self.assertIn("REST_QUERY", {row["source"] for row in sell.state_updates})
        self.assertEqual(sum(
            row["record_type"] == "ORDER_IN_FLIGHT" and row["order_id"] == sell.order_id
            for row in router.trace
        ), 1)
        self.assertTrue(any(e["event_type"] == "VENUE_ORDER_QUERY" for e in ledger.events))

    def test_unanswered_queries_time_out_block_close_and_trip_kill_switch(self):
        current = books()
        router = StrategyDirectedRouter(
            instrument_master(), adapters={"BYBIT": UnreachableAdapter("BYBIT")},
            latency_model=fixed_model(),
        )
        ledger = Ledger()
        engine = risk()
        oms = OMS(
            instrument_master(), engine, ledger, router=router,
            order_timeout_policy=OrderTimeoutPolicy(
                report_timeout_ns=100 * MS, query_interval_ns=50 * MS, max_queries=2,
            ),
        )
        plan = oms.execute(opportunity(), current)
        oms.advance_time(2_000 * MS, current)
        buy, sell = plan.children[:2]
        self.assertEqual(sell.state, OrderState.TIMED_OUT)
        self.assertTrue(engine.kill_switch)
        self.assertNotEqual(plan.state, PlanState.CLOSED)
        self.assertFalse(any(child.order_role == "UNWIND" for child in plan.children))
        self.assertTrue(any(
            e["event_type"] == "PLAN_CLOSE_DEFERRED"
            and e["code"] == "ORDER_TIMED_OUT_UNCONFIRMED"
            for e in ledger.events
        ))

        oms.reconcile_plan(plan.parent_id, {
            buy.order_id: VenueOrderSnapshot(
                buy.order_id, "BINANCE", INSTRUMENT, OrderState.FILLED,
                Decimal("0.01"), Decimal("60000.00"),
            ),
            sell.order_id: VenueOrderSnapshot(
                sell.order_id, "BYBIT", INSTRUMENT, OrderState.FILLED,
                Decimal("0.01"), Decimal("60060.00"),
            ),
        }, 2_100 * MS)
        self.assertEqual(sell.state, OrderState.FILLED)
        self.assertEqual(plan.state, PlanState.CLOSED)

    def test_residual_breach_holds_new_risk_and_escalates_to_kill(self):
        current = books()
        ledger = Ledger()
        engine = risk()
        oms = OMS(
            instrument_master(), engine, ledger,
            router=StrategyDirectedRouter(
                instrument_master(), adapters={"BYBIT": RejectingAdapter()}
            ),
            residual_policy=ResidualPolicy(
                max_residual_base=Decimal("0.002"), max_residual_age_ns=1_000 * MS,
                hedge_venue_priority=("BYBIT",), max_unwind_attempts=1,
                kill_after_ns=2_000 * MS,
            ),
        )
        plan = oms.execute(opportunity(), current)
        self.assertEqual(plan.state, PlanState.HEDGE_UNWIND)
        held = oms.execute(replace(opportunity(), opportunity_id="op-2"), current)
        self.assertEqual(held.state, PlanState.CLOSED)
        self.assertEqual(held.terminal_outcome, "REJECTED")
        self.assertTrue(any(
            e["event_type"] == "NEW_RISK_HELD_RESIDUAL_BREACH" for e in ledger.events
        ))
        self.assertFalse(engine.kill_switch)
        oms.enforce_residual_limits(3_000 * MS, current)
        self.assertTrue(engine.kill_switch)
        self.assertTrue(any(
            e["event_type"] == "RESIDUAL_BREACH_KILL_ESCALATION" for e in ledger.events
        ))


class DataSanityTests(unittest.TestCase):
    def setUp(self):
        self.book = OrderBook("BINANCE", INSTRUMENT)
        self.book.apply(delta(
            "BINANCE", 1, 1, [["59999", "1"]], [["60001", "1"]], "BOOK_SNAPSHOT",
        ))

    def test_hundred_x_jump_is_an_anomaly_until_it_reverts(self):
        state = self.book.apply(delta(
            "BINANCE", 2, 2, [["59999", "0"], ["5999900", "1"]],
            [["60001", "0"], ["6000100", "1"]],
        ))
        self.assertEqual(state, BookState.PRICE_ANOMALY)
        self.assertFalse(self.book.is_tradable)
        self.assertEqual(self.book.stats.price_jumps, 1)
        state = self.book.apply(delta(
            "BINANCE", 3, 3, [["5999900", "0"], ["59999", "1"]],
            [["6000100", "0"], ["60001", "1"]],
        ))
        self.assertEqual(state, BookState.VALID)

    def test_persistent_move_is_accepted_after_confirmation(self):
        states = [
            self.book.apply(delta(
                "BINANCE", 2, 2, [["59999", "0"], ["66000", "1"]],
                [["60001", "0"], ["66002", "1"]],
            )),
            self.book.apply(delta("BINANCE", 3, 3, [["66000", "2"]], [])),
            self.book.apply(delta("BINANCE", 4, 4, [["66000", "3"]], [])),
        ]
        self.assertEqual(states, [
            BookState.PRICE_ANOMALY, BookState.PRICE_ANOMALY, BookState.VALID,
        ])
        self.assertEqual(self.book.stats.price_jumps, 1)
        self.assertEqual(self.book.stats.confirmed_price_moves, 1)

    def test_wide_spread_and_invalid_levels_fail_closed(self):
        state = self.book.apply(delta(
            "BINANCE", 2, 2, [], [["60001", "0"], ["120000", "1"]],
        ))
        self.assertEqual(state, BookState.PRICE_ANOMALY)
        self.assertEqual(self.book.invalid_reason, "wide_spread")
        state = self.book.apply(delta("BINANCE", 3, 3, [["-1", "1"]], []))
        self.assertEqual(state, BookState.INVALID_DATA)
        self.assertEqual(self.book.apply(delta("BINANCE", 4, 4, [], [])), BookState.INVALID_DATA)
        recovered = self.book.apply(delta(
            "BINANCE", 5, 5, [["59999", "1"]], [["60001", "1"]], "BOOK_SNAPSHOT",
        ))
        self.assertEqual(recovered, BookState.VALID)
        self.assertEqual(self.book.stats.invalid_levels, 1)


class TimeIntegrityCorrectionTests(unittest.TestCase):
    def test_host_offset_is_corrected_but_true_negative_latency_blocks(self):
        monitor = TimeIntegrityMonitor(TimePolicy(
            correct_host_offset=True, negative_latency_tolerance_ns=5 * MS,
            host_offset_min_samples=5, host_offset_window=100,
        ), ("BINANCE",))
        base = 1_700_000_000_000_000_000
        results = []
        for index in range(30):
            exchange = base + index * 100 * MS
            receive = exchange - 230 * MS + 2 * MS + (index % 3) * MS
            event = MarketEvent.from_dict({
                "event_type": "BOOK_DELTA", "venue": "BINANCE",
                "instrument_id": INSTRUMENT, "exchange_ts_ns": exchange,
                "receive_ts_ns": receive, "sequence": index + 1,
            })
            results.append(monitor.observe(event, now_ns=receive))
        self.assertTrue(results[0].blocking)
        self.assertTrue(all(not item.blocking for item in results[5:]))
        self.assertIn("NEGATIVE_RECEIVE_LATENCY_CORRECTED", results[-1].flags)
        report = monitor.report()
        self.assertEqual(report["host_offset_correction"]["clock_offset_estimate_ns"], -228 * MS)

        exchange = base + 31 * 100 * MS
        bad = MarketEvent.from_dict({
            "event_type": "BOOK_DELTA", "venue": "BINANCE",
            "instrument_id": INSTRUMENT, "exchange_ts_ns": exchange,
            "receive_ts_ns": exchange - 1_000 * MS, "sequence": 99,
        })
        self.assertTrue(monitor.observe(bad, now_ns=bad.receive_ts_ns).blocking)


class EffectiveDatingTests(unittest.TestCase):
    def test_instrument_status_and_fee_schedule_are_effective_dated(self):
        master = InstrumentMaster([
            Instrument(
                INSTRUMENT, "BINANCE", "BTCUSDT", "BTC", "USDT", Decimal("0.10"),
                Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("4"),
            )
        ], FeeSchedule([
            FeeRule("BINANCE", Decimal("1"), Decimal("4"), 0),
            FeeRule("BINANCE", Decimal("-0.5"), Decimal("3"), 1_000),
        ]))
        master.apply_status(
            "BINANCE", INSTRUMENT, effective_from_ns=500, source="test", status="HALT"
        )
        self.assertEqual(master.get("BINANCE", INSTRUMENT, 100).status, "TRADING")
        self.assertEqual(master.get("BINANCE", INSTRUMENT, 600).status, "HALT")
        self.assertEqual(master.fee_bps("BINANCE", INSTRUMENT, at_ns=999), Decimal("4"))
        self.assertEqual(master.fee_bps("BINANCE", INSTRUMENT, at_ns=1_000), Decimal("3"))
        self.assertEqual(
            master.fee_bps("BINANCE", INSTRUMENT, Liquidity.MAKER, 1_000), Decimal("-0.5")
        )
        router = StrategyDirectedRouter(master)
        book = make_book("BINANCE", "59990.00", "60000.00")
        self.assertEqual(
            router.route(submit("before-halt", "60000.00", "0.001", created=100), book)[-1].event_type,
            ExecutionEventType.FILLED,
        )
        with self.assertRaisesRegex(ValueError, "not trading"):
            router.route(submit("after-halt", "60000.00", "0.001", created=600), book)


class LatencyConfigAndBacktestTests(unittest.TestCase):
    def test_preset_with_per_stage_overrides(self):
        model = latency_model_from_config({
            "seed": 3, "preset": "singapore",
            "profiles": {"BINANCE": {"entry": {"base_ms": 1}}},
        }, ("BINANCE", "BYBIT", "OKX"))
        self.assertEqual(model.profile("BINANCE").entry.base_ns, 1 * MS)
        self.assertEqual(model.profile("BYBIT").entry.model, "lognormal")
        self.assertIn("OKX", model.to_dict()["defaulted_venues"])
        for _ in range(50):
            model.draw("BYBIT", "entry")
        stage = model.report()["stages"]["BYBIT.entry"]
        self.assertEqual(stage["event_pair"], ["order_send_ts", "venue_match_ts"])
        self.assertEqual(stage["count"], 50)

    def test_backtest_runs_with_latency_and_reports_it(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            summary = run_backtest(
                ROOT / "config" / "research.json",
                ROOT / "data" / "samples" / "two_venue_books.jsonl", output,
            )
            report = json.loads((output / "latency_report.json").read_text())
            rows = [
                json.loads(line)
                for line in (output / "order_timestamps.jsonl").read_text().splitlines()
            ]
        self.assertEqual(summary["latency"]["model_source"], "preset:singapore")
        self.assertEqual(summary["execution_mode"], "INTERNAL_SIMULATION")
        self.assertEqual(summary["execution_realism"]["open_plans"], 0)
        self.assertEqual(summary["closed_plans"], 1)
        self.assertIn("BINANCE.entry", report["model"]["stages"])
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertLess(row["order_send_ts_ns"], row["venue_match_ts_ns"])
            self.assertLess(row["venue_match_ts_ns"], row["fill_observed_ts_ns"])
        spans = [row["venue_match_ts_ns"] - row["order_send_ts_ns"] for row in rows]
        self.assertGreater(min(spans), 1 * MS)


class ResidualBoundaryTests(unittest.TestCase):
    """Regression for a live-paper defect: a residual sitting exactly at
    ``max_residual_base`` was treated as "within bound" (a strict ``>``/``<=``
    check) and the plan closed with a real naked position that was never
    hedged. Booked as gross_pnl = -(filled_leg_notional), this produced large
    apparent "losses" (~$150 per occurrence) with no actual unwind."""

    def test_residual_exactly_at_the_cap_still_triggers_unwind(self):
        current = books()
        ledger = Ledger()
        oms = OMS(
            instrument_master(), risk(), ledger,
            router=StrategyDirectedRouter(
                instrument_master(), adapters={"BYBIT": RejectingAdapter()}
            ),
            residual_policy=ResidualPolicy(
                # opportunity() trades 0.01 BTC; a full single-leg reject
                # leaves a residual of exactly 0.01 == max_residual_base.
                max_residual_base=Decimal("0.01"), max_residual_age_ns=0,
                hedge_venue_priority=("BINANCE",),
            ),
        )
        plan = oms.execute(opportunity(), current)
        # Before the fix, 0.01 == 0.01 was treated as "within bound" (a
        # strict `>` check) and the plan closed immediately with a naked
        # 0.01 BTC position that was never hedged. Now the boundary itself
        # is a breach: the residual must be unwound (here, synchronously,
        # since the hedge venue has no latency model and no rejection), and
        # the plan must show real unwind work, not a silent naked close.
        self.assertEqual(plan.residual_base, Decimal("0"))
        self.assertEqual(plan.state, PlanState.CLOSED)
        unwinds = [order for order in plan.children if order.order_role == "UNWIND"]
        self.assertTrue(unwinds, "a residual at the cap must be unwound, not left naked")
        self.assertTrue(all(order.state == OrderState.FILLED for order in unwinds))

    def test_residual_below_the_cap_is_accepted_without_unwind(self):
        current = books()
        ledger = Ledger()
        oms = OMS(
            instrument_master(), risk(), ledger,
            router=StrategyDirectedRouter(
                instrument_master(), adapters={"BYBIT": RejectingAdapter()}
            ),
            residual_policy=ResidualPolicy(
                max_residual_base=Decimal("0.02"), max_residual_age_ns=0,
                hedge_venue_priority=("BINANCE",),
            ),
        )
        plan = oms.execute(opportunity(), current)
        self.assertEqual(plan.residual_base, Decimal("0.01"))
        self.assertEqual(plan.state, PlanState.CLOSED)
        self.assertFalse(any(order.order_role == "UNWIND" for order in plan.children))

    def test_zero_residual_with_zero_tolerance_closes_cleanly(self):
        # Edge case the boundary fix must not break: a plan that unwinds all
        # the way back to flat must still close, even when the configured
        # tolerance is exactly zero (residual == cap == 0 is not a breach;
        # it is the fully-flat state the cap of zero demands).
        current = books()
        ledger = Ledger()
        oms = OMS(
            instrument_master(), risk(), ledger,
            residual_policy=ResidualPolicy(
                max_residual_base=Decimal("0"), max_residual_age_ns=0,
            ),
        )
        plan = oms.execute(opportunity(), current)
        self.assertEqual(plan.residual_base, Decimal("0"))
        self.assertEqual(plan.state, PlanState.CLOSED)


class LateReportAfterCloseTests(unittest.TestCase):
    """Regression: every late report for an already-closed plan re-fired the
    kill switch (71 activations / 40 ERROR log lines in one 30-minute live
    cycle), even when the report was a benign reject/cancel confirmation
    carrying no new fill information."""

    @staticmethod
    def _deliver_late_report(router, plan, order, event_type, ts_ns, suffix):
        import crypto_stat_arb.execution as execution_module

        request = execution_module.CanonicalOrderRequest.create(
            action=execution_module.OrderAction.CANCEL, order_id=order.order_id,
            target_order_id=order.order_id, parent_opportunity_id=order.opportunity_id,
            parent_order_id=plan.parent_id, instrument_id=order.instrument_id,
            venue=order.venue, side=order.side, price=order.limit_price,
            quantity=order.quantity, order_type=execution_module.OrderType.LIMIT,
            time_in_force=execution_module.TimeInForce.GTC, created_ts_ns=ts_ns,
            idempotency_key=f"late-report-{suffix}",
        )
        callback = ExecutionCallback(
            schema_version="1.0.0", callback_id=f"late-{suffix}", request_id=request.request_id,
            order_id=order.order_id, parent_opportunity_id=order.opportunity_id,
            parent_order_id=plan.parent_id, venue=order.venue, instrument_id=order.instrument_id,
            event_type=event_type, event_ts_ns=ts_ns,
            cumulative_filled_quantity=(
                order.quantity if event_type == ExecutionEventType.FILLED
                else order.filled_quantity
            ),
            average_price=order.average_price or order.limit_price,
            reason="cancel target is not open" if event_type == ExecutionEventType.REJECTED else "",
        )
        router._deliveries.append(execution_module.PendingDelivery(
            deliver_ts_ns=ts_ns, ordinal=len(router._deliveries) + 1, route_id=f"late-{suffix}",
            request=request, callback=callback, venue_ts_ns=ts_ns,
        ))

    def test_benign_late_report_does_not_trip_the_kill_switch(self):
        current = books()
        ledger = Ledger()
        engine = risk()
        router = StrategyDirectedRouter(instrument_master(), latency_model=fixed_model())
        oms = OMS(instrument_master(), engine, ledger, router=router)
        plan = oms.execute(opportunity(), current)
        oms.drain(1_000 * 1_000_000)
        self.assertEqual(plan.state, PlanState.CLOSED)
        order = plan.children[0]

        # A cancel for an order that is already terminal is rejected by the
        # venue and carries no new fill; it must not re-open the incident.
        self._deliver_late_report(
            router, plan, order, ExecutionEventType.REJECTED, 2_000 * 1_000_000, "benign"
        )
        oms.process_throttled_routes(2_000 * 1_000_000 + 1)

        self.assertFalse(engine.kill_switch)
        self.assertTrue(any(
            event["event_type"] == "LATE_REPORT_FOR_CLOSED_PLAN" and not event["reveals_new_fill"]
            for event in ledger.events
        ))
        self.assertFalse(any(
            event["event_type"] == "KILL_SWITCH_STATE_TRANSITION" for event in ledger.events
        ))

    def test_late_fill_after_close_trips_the_kill_switch_once_only(self):
        current = books()
        ledger = Ledger()
        engine = risk()
        # BYBIT rejects outright (filled_quantity stays 0) and the cap is
        # generous, so the plan closes normally with that leg unfilled and
        # no unwind attempted -- exactly the situation where a late report
        # claiming that leg actually filled is genuinely new information.
        router = StrategyDirectedRouter(
            instrument_master(), adapters={"BYBIT": RejectingAdapter()},
        )
        oms = OMS(
            instrument_master(), engine, ledger, router=router,
            residual_policy=ResidualPolicy(
                max_residual_base=Decimal("1"), max_residual_age_ns=0,
            ),
        )
        plan = oms.execute(opportunity(), current)
        self.assertEqual(plan.state, PlanState.CLOSED)
        order = next(x for x in plan.children if x.venue == "BYBIT")
        self.assertEqual(order.state, OrderState.REJECTED)
        self.assertEqual(order.filled_quantity, Decimal("0"))

        # The same late fill, duplicated three times (a realistic retry or
        # redelivery pattern), must escalate exactly once.
        for index in range(3):
            self._deliver_late_report(
                router, plan, order, ExecutionEventType.FILLED,
                2_000 * 1_000_000 + index, f"fill-{index}",
            )
            oms.process_throttled_routes(2_000 * 1_000_000 + index + 1)

        activations = sum(
            event["event_type"] == "KILL_SWITCH_STATE_TRANSITION" for event in ledger.events
        )
        self.assertTrue(engine.kill_switch)
        self.assertEqual(activations, 1)
        self.assertEqual(engine.kill_switch_reason, "fill discovered after plan close")

if __name__ == "__main__":
    unittest.main()
