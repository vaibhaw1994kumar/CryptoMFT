from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.execution import (
    CanonicalOrderUpdate,
    ExecutionEventType,
    OrderUpdateSource,
    RateLimitPolicy,
    StrategyDirectedRouter,
    VenueRouteStatus,
)
from crypto_stat_arb.instruments import InstrumentMaster
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import (
    ChildOrder, Instrument, MarketEvent, Opportunity, OrderState, Side,
)
from crypto_stat_arb.oms import OMS
from crypto_stat_arb.orderbook import OrderBook
from crypto_stat_arb.plan_store import VenueOrderSnapshot
from crypto_stat_arb.risk import (
    ReconciliationDecision,
    RiskEngine,
    RiskLimits,
    VenueRiskSnapshot,
)


def instruments() -> InstrumentMaster:
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
        max_residual_base=Decimal("0.002"),
        max_book_age_ns=1_000_000_000,
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


def opportunity() -> Opportunity:
    return Opportunity(
        opportunity_id="reconcile-opportunity-1", strategy_id="strategy-1",
        instrument_id="BTC-USDT-SPOT", observed_ts_ns=2,
        buy_venue="BINANCE", sell_venue="BYBIT",
        buy_price=Decimal("60000.00"), sell_price=Decimal("60060.00"),
        quantity=Decimal("0.010"), gross_edge_bps=Decimal("10"),
        estimated_net_edge_bps=Decimal("2"), source_event_ids=("a", "b"),
    )


class FailingAdapter:
    def handle(self, request, book):
        raise ConnectionError("injected connector loss before local callbacks")


class ReconciliationTests(unittest.TestCase):
    def test_missing_fill_suspends_venue_until_late_fill_is_applied(self):
        master = instruments()
        engine = risk()
        router = StrategyDirectedRouter(
            master, adapters={"BINANCE": FailingAdapter()}
        )
        oms = OMS(master, engine, Ledger(), router=router)
        with self.assertRaises(ConnectionError):
            oms.execute(opportunity(), books())
        plan = next(iter(oms.plans.values()))
        venue_orders = {
            order.order_id: VenueOrderSnapshot(
                order_id=order.order_id, venue=order.venue,
                instrument_id=order.instrument_id, state=OrderState.FILLED,
                filled_quantity=order.quantity,
                average_price=(
                    Decimal("60000") if order.side == Side.BUY
                    else Decimal("60060")
                ),
                venue_order_id=f"late-{order.order_id}", observed_ts_ns=50,
            )
            for order in plan.children
        }
        broken = oms.reconcile_risk_state(
            now_ns=51, venue_orders=venue_orders,
            require_complete_opportunities=True,
        )
        self.assertEqual(broken.decision, ReconciliationDecision.REJECTED)
        self.assertIn("MISSING_LOCAL_FILL", {
            issue["code"] for issue in broken.issues
        })
        self.assertEqual(set(broken.suspended_venues), {"BINANCE", "BYBIT"})
        self.assertFalse(engine.kill_switch)
        with self.assertRaisesRegex(ValueError, "venue suspended"):
            engine.reserve(replace(
                opportunity(), opportunity_id="blocked-by-trade-break"
            ))

        oms.reconcile_plan(plan.parent_id, venue_orders, 53)
        resolved = oms.reconcile_risk_state(
            now_ns=54, venue_orders=venue_orders,
            require_complete_opportunities=True,
        )
        self.assertEqual(resolved.decision, ReconciliationDecision.APPROVED)
        self.assertEqual(resolved.resolved_venues, ("BINANCE", "BYBIT"))
        self.assertFalse(engine.venue_is_suspended("BINANCE"))
        self.assertFalse(engine.venue_is_suspended("BYBIT"))

    def test_extra_fill_conflicting_state_and_incomplete_package_are_detected(self):
        engine = risk()
        oms = OMS(instruments(), engine, Ledger())
        closed = oms.execute(opportunity(), books())
        buy, sell = closed.children
        broken_orders = {
            buy.order_id: VenueOrderSnapshot(
                order_id=buy.order_id, venue=buy.venue,
                instrument_id=buy.instrument_id, state=buy.state,
                filled_quantity=Decimal("0.005"),
                average_price=buy.average_price,
            ),
            sell.order_id: VenueOrderSnapshot(
                order_id=sell.order_id, venue=sell.venue,
                instrument_id=sell.instrument_id,
                state=OrderState.ACKNOWLEDGED,
                filled_quantity=sell.filled_quantity,
                average_price=sell.average_price,
            ),
        }
        report = oms.reconcile_risk_state(now_ns=60, venue_orders=broken_orders)
        codes = {issue["code"] for issue in report.issues}
        self.assertIn("EXTRA_LOCAL_FILL", codes)
        self.assertIn("CONFLICTING_ORDER_STATE", codes)
        self.assertEqual(set(report.suspended_venues), {"BINANCE", "BYBIT"})
        self.assertFalse(engine.kill_switch)

        queued_engine = risk()
        queued_router = StrategyDirectedRouter(instruments(), route_status={
            venue: VenueRouteStatus(
                rate_limit_capacity=1, rate_limit_used=1,
                rate_limit_window_ns=1_000,
                rate_limit_policy=RateLimitPolicy.QUEUE,
            )
            for venue in ("BINANCE", "BYBIT")
        })
        queued_oms = OMS(
            instruments(), queued_engine, Ledger(), router=queued_router
        )
        open_plan = queued_oms.execute(
            replace(opportunity(), opportunity_id="incomplete-package"), books()
        )
        matching = {
            order.order_id: VenueOrderSnapshot(
                order_id=order.order_id, venue=order.venue,
                instrument_id=order.instrument_id, state=order.state,
                filled_quantity=order.filled_quantity,
                average_price=order.average_price,
            )
            for order in open_plan.children
        }
        incomplete = queued_oms.reconcile_risk_state(
            now_ns=61, venue_orders=matching,
            require_complete_opportunities=True,
        )
        self.assertIn("INCOMPLETE_OPPORTUNITY", {
            issue["code"] for issue in incomplete.issues
        })
        self.assertEqual(
            set(incomplete.suspended_venues), {"BINANCE", "BYBIT"}
        )

    def test_account_state_replays_average_price_pnl_balance_and_capacity(self):
        ledger = Ledger()
        fills = (
            ("buy-1", Side.BUY, "1", "100", "1"),
            ("buy-2", Side.BUY, "1", "110", "1"),
            ("sell-1", Side.SELL, "1.5", "120", "1.8"),
        )
        for ts_ns, (order_id, side, quantity, price, fee) in enumerate(fills, 1):
            order = ChildOrder(
                order_id=order_id, parent_id="parent-accounting",
                opportunity_id="opportunity-accounting", venue="BINANCE",
                instrument_id="BTC-USDT-SPOT", side=side,
                quantity=Decimal(quantity), limit_price=Decimal(price),
                state=OrderState.FILLED, filled_quantity=Decimal(quantity),
                average_price=Decimal(price), fee=Decimal(fee),
            )
            ledger.apply_fill(order, ts_ns)

        state = ledger.recalculate_account_state(
            mark_prices={
                ("BINANCE", "BTC-USDT-SPOT"): Decimal("130")
            },
            initial_capital_by_venue={"BINANCE": Decimal("1000")},
            reserved_capacity={
                "reserved_gross": Decimal("100"),
                "reserved_cash": {"BINANCE": Decimal("10")},
                "reserved_margin": {"BINANCE": Decimal("5")},
                "reserved_position": {"BINANCE": Decimal("0.2")},
                "reserved_messages": {"BINANCE": 2},
            },
        )
        position = state["positions"]["BINANCE:BTC-USDT-SPOT"]
        capital = state["venues"]["BINANCE"]
        self.assertEqual(position["quantity"], "0.5")
        self.assertEqual(position["average_price"], "105")
        self.assertEqual(position["realized_pnl"], "22.5")
        self.assertEqual(position["unrealized_pnl"], "12.5")
        self.assertEqual(position["fees"], "3.8")
        self.assertEqual(capital["cash_balance"], "966.2")
        self.assertEqual(capital["equity"], "1031.2")
        self.assertEqual(capital["available_cash"], "956.2")
        self.assertEqual(capital["available_capital"], "1016.2")
        self.assertEqual(capital["reserved_messages"], 2)

    def test_complete_order_fill_ledger_reconciles_without_orphans(self):
        ledger = Ledger()
        oms = OMS(instruments(), risk(), ledger)
        plan = oms.execute(opportunity(), books())
        lifecycle = [
            event for event in ledger.events
            if event["event_type"] == "ORDER_LIFECYCLE"
        ]
        self.assertEqual(sum(x["lifecycle_event"] == "SUBMIT" for x in lifecycle), 2)
        self.assertEqual(sum(x["lifecycle_event"] == "ACKNOWLEDGED" for x in lifecycle), 2)
        self.assertEqual(sum(x["lifecycle_event"] == "FILLED" for x in lifecycle), 2)
        self.assertEqual(len([x for x in ledger.events if x["event_type"] == "FILL"]), 2)
        self.assertTrue(all(
            event.get("parent_id") and event.get("opportunity_id")
            and event.get("order_id") and event.get("venue")
            and event.get("instrument_id")
            for event in lifecycle
        ))
        venue_orders = {
            order.order_id: VenueOrderSnapshot(
                order_id=order.order_id, venue=order.venue,
                instrument_id=order.instrument_id, state=order.state,
                filled_quantity=order.filled_quantity,
                average_price=order.average_price, fee=order.fee,
                venue_order_id=f"testnet-{order.order_id}", observed_ts_ns=20,
            )
            for order in plan.children
        }
        account_state = oms.recalculate_account_state(
            mark_prices={
                ("BINANCE", "BTC-USDT-SPOT"): Decimal("60000"),
                ("BYBIT", "BTC-USDT-SPOT"): Decimal("60060"),
            },
            initial_capital_by_venue={
                "BINANCE": Decimal("1000"), "BYBIT": Decimal("1000")
            },
        )
        venue_risk = tuple(
            VenueRiskSnapshot(
                venue=venue, instrument_id="BTC-USDT-SPOT",
                position_base=Decimal(account_state["positions"][
                    f"{venue}:BTC-USDT-SPOT"
                ]["quantity"]),
                balance_delta=ledger.cash_deltas[venue],
                mark_price=Decimal("60000" if venue == "BINANCE" else "60060"),
                average_price=Decimal(account_state["positions"][
                    f"{venue}:BTC-USDT-SPOT"
                ]["average_price"]),
                realized_pnl=Decimal(account_state["positions"][
                    f"{venue}:BTC-USDT-SPOT"
                ]["realized_pnl"]),
                unrealized_pnl=Decimal(account_state["positions"][
                    f"{venue}:BTC-USDT-SPOT"
                ]["unrealized_pnl"]),
                initial_capital=Decimal("1000"),
                available_cash=Decimal(account_state["venues"][venue]["available_cash"]),
                available_capital=Decimal(
                    account_state["venues"][venue]["available_capital"]
                ),
                reserved_cash=Decimal(account_state["venues"][venue]["reserved_cash"]),
                reserved_margin=Decimal(account_state["venues"][venue]["reserved_margin"]),
                reserved_position=Decimal(
                    account_state["venues"][venue]["reserved_position"]
                ),
                reserved_messages=int(
                    account_state["venues"][venue]["reserved_messages"]
                ),
                observed_ts_ns=20,
            )
            for venue in ("BINANCE", "BYBIT")
        )
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "rms_reconciliation.json"
            report = oms.reconcile_risk_state(
                now_ns=21, venue_orders=venue_orders,
                venue_risk=venue_risk, report_path=report_path,
            )
            self.assertEqual(report.decision, ReconciliationDecision.APPROVED)
            self.assertEqual(report.issues, ())
            self.assertEqual(json.loads(report_path.read_text())["decision"], "APPROVED")

    def test_orphan_and_account_mismatches_fail_closed(self):
        ledger = Ledger()
        engine = risk()
        oms = OMS(instruments(), engine, ledger)
        plan = oms.execute(opportunity(), books())
        venue_orders = {
            order.order_id: VenueOrderSnapshot(
                order_id=order.order_id, venue=order.venue,
                instrument_id=order.instrument_id, state=order.state,
                filled_quantity=order.filled_quantity,
                average_price=order.average_price, fee=order.fee,
            )
            for order in plan.children
        }
        venue_orders["orphan-testnet-order"] = VenueOrderSnapshot(
            order_id="orphan-testnet-order", venue="BINANCE",
            instrument_id="BTC-USDT-SPOT", state=plan.children[0].state,
            filled_quantity=Decimal("0.001"), average_price=Decimal("60000"),
        )
        report = oms.reconcile_risk_state(
            now_ns=30, venue_orders=venue_orders,
            venue_risk=(VenueRiskSnapshot(
                venue="BINANCE", instrument_id="BTC-USDT-SPOT",
                position_base=Decimal("0"), balance_delta=Decimal("0"),
                account_enabled=False, observed_ts_ns=30,
            ),),
        )
        codes = {issue["code"] for issue in report.issues}
        self.assertEqual(report.decision, ReconciliationDecision.REJECTED)
        self.assertTrue({
            "ORPHAN_VENUE_ORDER", "POSITION_MISMATCH",
            "BALANCE_MISMATCH", "CONTROL_STATE_MISMATCH",
        }.issubset(codes))
        self.assertTrue(engine.kill_switch)

    def test_private_stream_partial_fills_are_incremental_and_idempotent(self):
        master = instruments()
        router = StrategyDirectedRouter(master, route_status={
            venue: VenueRouteStatus(
                rate_limit_capacity=1, rate_limit_used=1,
                rate_limit_window_ns=1_000,
                rate_limit_policy=RateLimitPolicy.QUEUE,
            )
            for venue in ("BINANCE", "BYBIT")
        })
        ledger = Ledger()
        oms = OMS(master, risk(), ledger, router=router)
        plan = oms.execute(opportunity(), books())
        order = plan.children[0]
        partial = CanonicalOrderUpdate(
            update_id="private-partial-1", source=OrderUpdateSource.PRIVATE_STREAM,
            parent_order_id=plan.parent_id, order_id=order.order_id,
            venue=order.venue, instrument_id=order.instrument_id,
            state=ExecutionEventType.PARTIAL, event_ts_ns=10,
            observed_ts_ns=11, sequence=1,
            cumulative_filled_quantity=Decimal("0.005"),
            average_price=Decimal("60000"),
        )
        oms.ingest_private_stream_update(partial)
        oms.ingest_private_stream_update(partial)
        oms.ingest_private_stream_update(CanonicalOrderUpdate(
            update_id="private-fill-1", source=OrderUpdateSource.PRIVATE_STREAM,
            parent_order_id=plan.parent_id, order_id=order.order_id,
            venue=order.venue, instrument_id=order.instrument_id,
            state=ExecutionEventType.FILLED, event_ts_ns=12,
            observed_ts_ns=13, sequence=2,
            cumulative_filled_quantity=Decimal("0.010"),
            average_price=Decimal("60000"),
        ))
        fills = [event for event in ledger.events if event["event_type"] == "FILL"]
        self.assertEqual([event["fill_quantity"] for event in fills], [
            Decimal("0.005"), Decimal("0.005"),
        ])
        self.assertEqual(
            ledger.positions[("BINANCE", "BTC-USDT-SPOT")], Decimal("0.010")
        )
        self.assertEqual(ledger.audit_order_fills(oms.plans), [])

    def test_orphan_private_update_is_explained_and_stops_new_risk(self):
        ledger = Ledger()
        engine = risk()
        oms = OMS(instruments(), engine, ledger)
        with self.assertRaisesRegex(ValueError, "unknown parent plan"):
            oms.ingest_private_stream_update(CanonicalOrderUpdate(
                update_id="orphan-private-1",
                source=OrderUpdateSource.PRIVATE_STREAM,
                parent_order_id="unknown-parent", order_id="unknown-order",
                venue="BINANCE", instrument_id="BTC-USDT-SPOT",
                state=ExecutionEventType.FILLED, event_ts_ns=40,
                observed_ts_ns=41,
                cumulative_filled_quantity=Decimal("0.001"),
                average_price=Decimal("60000"),
            ))
        self.assertTrue(engine.kill_switch)
        event = ledger.events[-1]
        self.assertEqual(event["event_type"], "ORPHAN_ORDER_UPDATE_REJECTED")
        self.assertEqual(event["update_id"], "orphan-private-1")
        self.assertEqual(event["reason"], "unknown_parent_plan")


if __name__ == "__main__":
    unittest.main()
