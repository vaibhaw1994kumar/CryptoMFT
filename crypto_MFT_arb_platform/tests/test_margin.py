from dataclasses import replace
from decimal import Decimal
import tempfile
from pathlib import Path
import unittest

from crypto_stat_arb.instruments import InstrumentMaster
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import ChildOrder, Instrument, OrderState, Side
from crypto_stat_arb.oms import OMS
from crypto_stat_arb.risk import (
    AccountRiskSnapshot,
    MarginState,
    ReconciliationDecision,
    RiskEngine,
    RiskLimits,
    VenueRiskSnapshot,
)


def limits() -> RiskLimits:
    return RiskLimits(
        max_order_notional=Decimal("100000"),
        max_gross_notional=Decimal("200000"),
        max_residual_base=Decimal("10"),
        max_book_age_ns=1_000_000_000,
    )


def master(product_type: str = "PERPETUAL") -> InstrumentMaster:
    return InstrumentMaster([Instrument(
        instrument_id="BTC-USDT-PERP", venue="BINANCE", symbol="BTCUSDT",
        base="BTC", quote="USDT", tick_size=Decimal("0.1"),
        quantity_step=Decimal("0.001"), min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"), taker_fee_bps=Decimal("4"),
        product_type=product_type,
        leverage=Decimal("1" if product_type == "SPOT" else "10"),
    )])


def ledger_with_position() -> Ledger:
    ledger = Ledger()
    order = ChildOrder(
        order_id="margin-fill", parent_id="margin-parent",
        opportunity_id="margin-opportunity", venue="BINANCE",
        instrument_id="BTC-USDT-PERP", side=Side.BUY,
        quantity=Decimal("1"), limit_price=Decimal("100"),
        state=OrderState.FILLED, filled_quantity=Decimal("1"),
        average_price=Decimal("100"), fee=Decimal("0"),
    )
    ledger.apply_fill(order, 1)
    return ledger


def account(collateral: str) -> AccountRiskSnapshot:
    return AccountRiskSnapshot(
        venue="BINANCE", margin_enabled=True,
        collateral_balance=Decimal(collateral),
        available_margin=Decimal(collateral),
        initial_margin_rate=Decimal("0.10"),
        maintenance_margin_rate=Decimal("0.05"),
        liquidation_buffer_rate=Decimal("0.25"),
    )


class MarginCollateralTests(unittest.TestCase):
    def evaluate(self, collateral: str, *, venue_balance: str | None = None):
        ledger = ledger_with_position()
        engine = RiskEngine(limits(), {"BINANCE": account(collateral)})
        venue_risk = () if venue_balance is None else (VenueRiskSnapshot(
            venue="BINANCE", instrument_id="BTC-USDT-PERP",
            position_base=Decimal("1"), balance_delta=Decimal("-100"),
            mark_price=Decimal("100"), margin_balance=Decimal(venue_balance),
        ),)
        report = engine.evaluate_margin(
            ledger=ledger, now_ns=10,
            mark_prices={("BINANCE", "BTC-USDT-PERP"): Decimal("100")},
            instruments=master(), venue_risk=venue_risk,
        )
        return engine, ledger, report

    def test_initial_maintenance_and_liquidation_buffer_are_marked(self):
        engine, _, report = self.evaluate("10")
        row = report.venues[0]
        self.assertEqual(report.state, MarginState.HEALTHY)
        self.assertEqual(row.gross_notional, Decimal("100"))
        self.assertEqual(row.initial_margin_required, Decimal("10.00"))
        self.assertEqual(row.maintenance_margin_required, Decimal("5.00"))
        self.assertEqual(row.liquidation_buffer, Decimal("5.00"))
        self.assertEqual(row.required_liquidation_buffer, Decimal("1.2500"))
        self.assertFalse(engine.kill_switch)

    def test_warning_is_emitted_before_maintenance_breach(self):
        engine, ledger, report = self.evaluate("6")
        self.assertEqual(report.state, MarginState.WARNING)
        self.assertEqual(report.venues[0].alert_codes, ("LIQUIDATION_BUFFER_LOW",))
        self.assertFalse(engine.kill_switch)
        self.assertTrue(any(
            event["event_type"] == "MARGIN_BUFFER_ALERT"
            for event in ledger.events
        ))

    def test_venue_reported_margin_can_trigger_fail_closed_control(self):
        engine, ledger, report = self.evaluate("10", venue_balance="4")
        self.assertEqual(report.state, MarginState.BREACH)
        self.assertTrue(engine.kill_switch)
        self.assertEqual(engine.kill_switch_source, "RMS_MARGIN")
        self.assertTrue(any(
            event["event_type"] == "KILL_SWITCH_STATE_TRANSITION"
            for event in ledger.events
        ))

    def test_margin_state_reconciliation_accepts_match_and_rejects_mismatch(self):
        ledger = Ledger()
        engine = RiskEngine(limits(), {"BINANCE": account("10")})
        oms = OMS(master(), engine, ledger)
        snapshot = VenueRiskSnapshot(
            venue="BINANCE", instrument_id="BTC-USDT-PERP",
            position_base=Decimal("0"), balance_delta=Decimal("0"),
            mark_price=Decimal("100"), margin_balance=Decimal("10"),
            initial_margin_required=Decimal("0"),
            maintenance_margin_required=Decimal("0"),
            liquidation_buffer=Decimal("10"),
        )
        approved = oms.reconcile_risk_state(
            now_ns=20, venue_risk=(snapshot,), require_venue_orders=False,
        )
        self.assertEqual(approved.decision, ReconciliationDecision.APPROVED)
        broken = oms.reconcile_risk_state(
            now_ns=21, venue_risk=(replace(snapshot, margin_balance=Decimal("9")),),
            require_venue_orders=False,
        )
        self.assertEqual(broken.decision, ReconciliationDecision.REJECTED)
        self.assertIn("MARGIN_BALANCE_MISMATCH", {
            issue["code"] for issue in broken.issues
        })

    def test_spot_portfolio_is_explicitly_not_applicable(self):
        ledger = ledger_with_position()
        engine = RiskEngine(limits())
        report = engine.evaluate_margin(
            ledger=ledger, now_ns=10,
            mark_prices={("BINANCE", "BTC-USDT-PERP"): Decimal("100")},
            initial_capital_by_venue={"BINANCE": Decimal("1000")},
            instruments=master(product_type="SPOT"),
        )
        self.assertEqual(report.state, MarginState.NOT_APPLICABLE)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "margin_status.json"
            report.write(path)
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
