"""ECO05 balances: fee-inclusive reservations, consumption, locked funds, venue balance reconciliation."""

from dataclasses import replace
from decimal import Decimal
import tempfile
from pathlib import Path
import unittest

from crypto_stat_arb.economics import opening_balance_events
from crypto_stat_arb.fees import FeeRule, FeeSchedule
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import ChildOrder, OrderState, ParentPlan, PlanState, Side
from crypto_stat_arb.oms import OMS
from crypto_stat_arb.plan_store import ParentPlanStore
from crypto_stat_arb.risk import (
    InventoryModel, InventoryPolicy, ReconciliationDecision, RiskEngine, RiskLimits, VenueBalanceSnapshot,
)
from test_oms import instrument_master, make_book, opportunity


SPOT = "BTC-USDT-SPOT"


def risk_engine(base, quote) -> RiskEngine:
    return RiskEngine(RiskLimits(
        max_order_notional=Decimal("5000"), max_gross_notional=Decimal("100000"),
        max_residual_base=Decimal("0.02"), max_book_age_ns=1_000_000_000,
    ), inventory=InventoryPolicy(
        model=InventoryModel.PRE_FUNDED, instrument_id=SPOT,
        initial_base_by_venue={k: Decimal(v) for k, v in base.items()},
        initial_quote_by_venue={k: Decimal(v) for k, v in quote.items()},
    ))


def oms_with(base, quote, *, master=None, adapters=None, store=None) -> OMS:
    ledger = Ledger()
    for event in opening_balance_events(
        [(v, "BTC", Decimal(q)) for v, q in base.items()] + [(v, "USDT", Decimal(q)) for v, q in quote.items()]
    ):
        ledger.economics.apply(event)
    from crypto_stat_arb.execution import StrategyDirectedRouter
    master = master or instrument_master()
    return OMS(master, risk_engine(base, quote), ledger,
               router=StrategyDirectedRouter(master, adapters=adapters or {}), plan_store=store)


def nth(n, qty="0.01"):
    return replace(opportunity(qty), opportunity_id=f"op-{n}", source_event_ids=(f"a{n}", f"b{n}"))


def books(bybit_bid_qty="1"):
    return {("BINANCE", SPOT): make_book("BINANCE", "59990.00", "60000.00"),
            ("BYBIT", SPOT): make_book("BYBIT", "60060.00", "60070.00", qty=bybit_bid_qty)}


class SilentVenue:
    """Accepts the order and never reports: the plan stays open."""

    def handle(self, request, book):
        return ()


class FeeInclusiveReservationTests(unittest.TestCase):
    def test_fees_push_a_buy_reservation_over_the_quote_held(self):
        # 0.01 x 60000 = 600 USDT; the 4 bps taker fee makes it 600.24.
        oms = oms_with({"BINANCE": "0", "BYBIT": "1"}, {"BINANCE": "600.10", "BYBIT": "0"})
        with self.assertRaisesRegex(ValueError, "insufficient quote inventory: BINANCE"):
            oms.risk.reserve(nth(1))
        enough = oms_with({"BINANCE": "0", "BYBIT": "1"}, {"BINANCE": "600.24", "BYBIT": "0"})
        reservation = enough.risk.reserve(nth(1))
        self.assertEqual(reservation.cash_by_venue["BINANCE"], Decimal("600.240000"))

    def test_a_fee_charged_in_base_is_reserved_on_the_sell_leg(self):
        master = instrument_master()
        master.fee_schedule = FeeSchedule([FeeRule(v, Decimal("2"), Decimal("10"), instrument_id=SPOT,
                                                   fee_asset="BASE") for v in ("BINANCE", "BYBIT")])
        oms = oms_with({"BINANCE": "0", "BYBIT": "0.01"}, {"BINANCE": "1000", "BYBIT": "0"}, master=master)
        with self.assertRaisesRegex(ValueError, "insufficient base inventory: BYBIT"):
            oms.risk.reserve(nth(1))                       # needs 0.01 + 10 bps = 0.01001 BTC
        oms = oms_with({"BINANCE": "0", "BYBIT": "0.01001"}, {"BINANCE": "1000", "BYBIT": "0"}, master=master)
        self.assertEqual(oms.risk.reserve(nth(1)).base_by_venue["BYBIT"], Decimal("0.0100100"))

    def test_a_fee_token_must_be_priced_and_held(self):
        master = instrument_master()
        master.fee_schedule = FeeSchedule([FeeRule(v, Decimal("2"), Decimal("4"), instrument_id=SPOT,
                                                   fee_asset="BNB") for v in ("BINANCE", "BYBIT")])
        oms = oms_with({"BINANCE": "0", "BYBIT": "1"}, {"BINANCE": "1000", "BYBIT": "0"}, master=master)
        with self.assertRaisesRegex(ValueError, "fee token unpriced: BINANCE BNB"):
            oms.risk.reserve(nth(1))
        oms.risk.asset_prices = {("*", "BNB"): Decimal("600")}
        with self.assertRaisesRegex(ValueError, "insufficient fee token: BINANCE BNB"):
            oms.risk.reserve(nth(1))                       # 0.24 USDT of fee = 0.0004 BNB, none held

    def test_a_rebate_never_reduces_a_reservation(self):
        master = instrument_master()
        from crypto_stat_arb.fees import FeeSource
        master.fee_schedule = FeeSchedule([FeeRule(v, Decimal("-1"), Decimal("-1"), instrument_id=SPOT,
                                                   source=FeeSource.VERIFIED) for v in ("BINANCE", "BYBIT")])
        oms = oms_with({"BINANCE": "0", "BYBIT": "1"}, {"BINANCE": "1000", "BYBIT": "0"}, master=master)
        self.assertEqual(oms.risk.reserve(nth(1)).cash_by_venue["BINANCE"], Decimal("600.00"))

    def test_two_strategies_compete_for_one_balance(self):
        oms = oms_with({"BINANCE": "0", "BYBIT": "0.015"}, {"BINANCE": "100000", "BYBIT": "0"})
        oms.risk.reserve(replace(nth(1), strategy_id="cross_venue"))
        with self.assertRaisesRegex(ValueError, "insufficient base inventory: BYBIT"):
            oms.risk.reserve(replace(nth(2), strategy_id="mean_reversion"))
        self.assertEqual(oms.risk.refusals, {"insufficient base inventory": 1})


class ConsumptionTests(unittest.TestCase):
    def test_partial_fill_then_cancel_holds_only_the_remainder_then_releases_it(self):
        # BYBIT shows only 0.004 to sell into: the IOC sell fills 0.004 and the
        # rest is cancelled; the BINANCE buy never answers, so the plan stays open.
        oms = oms_with({"BINANCE": "0", "BYBIT": "0.05"}, {"BINANCE": "100000", "BYBIT": "0"},
                       adapters={"BINANCE": SilentVenue()})
        plan = oms.execute(nth(1), books(bybit_bid_qty="0.004"))
        sell = next(o for o in plan.children if o.venue == "BYBIT")
        self.assertEqual((sell.filled_quantity, sell.state), (Decimal("0.004"), OrderState.CANCELLED))
        self.assertNotEqual(plan.state, PlanState.CLOSED)
        view = oms.risk.balance_view("BYBIT", "BTC")
        # Total fell by the fill; only the unfilled 0.006 (+ no base fee) is still reserved.
        self.assertEqual(view["total"], Decimal("0.046"))
        self.assertEqual(view["reserved"], Decimal("0.0060"))
        self.assertEqual(view["free"], Decimal("0.0400"))       # 0.05 - 0.01: nothing counted twice
        # The BINANCE leg is unfilled: its quote reservation is intact.
        self.assertEqual(oms.risk.reserved_cash["BINANCE"], Decimal("600.240000"))
        oms.risk.release(oms.risk.reservations[plan.opportunity.opportunity_id])
        self.assertEqual(oms.risk.balance_view("BYBIT", "BTC")["free"], Decimal("0.046"))
        self.assertEqual(oms.risk.reserved_cash["BINANCE"], Decimal("0"))

    def test_rehydrating_a_partly_filled_plan_reserves_only_the_remainder(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ParentPlanStore(Path(directory) / "p.sqlite")
            oms = oms_with({"BINANCE": "0", "BYBIT": "0.05"}, {"BINANCE": "100000", "BYBIT": "0"},
                           adapters={"BINANCE": SilentVenue()}, store=store)
            oms.execute(nth(1), books(bybit_bid_qty="0.004"))
            store.close()
            store = ParentPlanStore(Path(directory) / "p.sqlite")
            restarted = oms_with({"BINANCE": "0", "BYBIT": "0.05"}, {"BINANCE": "100000", "BYBIT": "0"}, store=store)
            self.assertEqual(restarted.risk.reserved_base["BYBIT"], Decimal("0.0060"))
            self.assertEqual(restarted.risk.balance_view("BYBIT", "BTC")["free"], Decimal("0.0400"))
            store.close()


class LockedFundsTests(unittest.TestCase):
    def test_open_hedge_orders_lock_funds_and_block_overspending(self):
        oms = oms_with({"BINANCE": "0", "BYBIT": "0.015"}, {"BINANCE": "100000", "BYBIT": "0"})
        hedge = ChildOrder(order_id="h1", parent_id="p0", opportunity_id="op0", venue="BYBIT",
                           instrument_id=SPOT, side=Side.SELL, quantity=Decimal("0.01"),
                           limit_price=Decimal("60000"), state=OrderState.ACKNOWLEDGED, order_role="UNWIND")
        oms.plans["p0"] = ParentPlan(parent_id="p0", opportunity=nth(0), children=[hedge],
                                     state=PlanState.HEDGE_UNWIND)
        self.assertEqual(oms.locked_balances(), {("BYBIT", "BTC"): Decimal("0.01")})
        self.assertEqual(oms.risk.balance_view("BYBIT", "BTC")["free"], Decimal("0.005"))
        with self.assertRaisesRegex(ValueError, "insufficient base inventory: BYBIT"):
            oms.risk.reserve(nth(1))                       # 0.015 held, 0.01 locked by the hedge


class VenueBalanceReconciliationTests(unittest.TestCase):
    def reconcile(self, oms, *snapshots):
        return oms.reconcile_risk_state(now_ns=10, require_venue_orders=False, venue_balances=snapshots)

    def test_matching_mismatching_total_and_free(self):
        oms = oms_with({"BINANCE": "0.02", "BYBIT": "0"}, {"BINANCE": "1000", "BYBIT": "0"})
        ok = self.reconcile(oms, VenueBalanceSnapshot("BINANCE", "BTC", Decimal("0.02"), Decimal("0.02")))
        self.assertEqual(ok.decision, ReconciliationDecision.APPROVED)
        bad = self.reconcile(oms, VenueBalanceSnapshot("BINANCE", "BTC", Decimal("0.019")),
                             VenueBalanceSnapshot("BINANCE", "USDT", Decimal("1000"), Decimal("900")))
        self.assertEqual(bad.decision, ReconciliationDecision.REJECTED)
        self.assertEqual({i["code"] for i in bad.issues}, {"BALANCE_TOTAL_MISMATCH", "BALANCE_FREE_MISMATCH"})


class ExhaustionVisibilityTests(unittest.TestCase):
    def test_one_directional_trading_exhausts_a_venue_visibly(self):
        oms = oms_with({"BINANCE": "0", "BYBIT": "0.01"}, {"BINANCE": "5000", "BYBIT": "0"})
        oms.execute(nth(1), books())                       # sells BYBIT's only 0.01 BTC
        oms.execute(nth(2), books())                       # refused: nothing left to sell
        snapshot = oms.risk.inventory_snapshot()
        self.assertIn("BASE_EXHAUSTED", snapshot["exhausted"]["BYBIT"])
        self.assertEqual(snapshot["refusals_by_reason"], {"insufficient base inventory": 1})
        self.assertEqual(Decimal(snapshot["balances"]["BYBIT"]["BTC"]["free"]), 0)


if __name__ == "__main__":
    unittest.main()
