"""Economic event ledger, ECO01 fee/rebate accounting and equity reconciliation."""

from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.economics import (
    ConflictingEventError, EconomicEvent, EconomicEventType, EconomicLedger, EventSource,
    Posting, Valuation, opening_balance_events, reconcile,
)
from crypto_stat_arb.execution import (
    DeterministicVenueAdapter, ResidualPolicy, StrategyDirectedRouter, TimeInForce,
)
from crypto_stat_arb.fees import FeeRule, FeeSchedule, FeeSource
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import ChildOrder, ParentPlan, Side
from crypto_stat_arb.oms import OMS
from crypto_stat_arb.risk import InventoryModel, InventoryPolicy, RiskEngine, RiskLimits
from crypto_stat_arb.runner import run_backtest
from test_execution_realism import books, delta, submit
from test_oms import RejectingAdapter, instrument_master, opportunity


ROOT = Path(__file__).parents[1]
INSTRUMENT = "BTC-USDT-SPOT"


def limits() -> RiskLimits:
    return RiskLimits(
        max_order_notional=Decimal("5000"), max_gross_notional=Decimal("10000"),
        max_residual_base=Decimal("0.02"), max_book_age_ns=1_000_000_000,
    )


def fee_master(**rule) -> object:
    master = instrument_master()
    rules = [FeeRule(venue=venue, **rule) for venue in ("BINANCE", "BYBIT")]
    master.fee_schedule = FeeSchedule(rules)
    return master


def oms_with(master, *, opening=None, adapters=None, residual=None) -> OMS:
    ledger = Ledger()
    if opening:
        for event in opening_balance_events(opening):
            ledger.economics.apply(event)
    risk = RiskEngine(limits(), inventory=InventoryPolicy(
        model=InventoryModel.UNCONSTRAINED, instrument_id=INSTRUMENT,
    ))
    router = StrategyDirectedRouter(master, adapters=adapters or {})
    kwargs = {"residual_policy": residual} if residual else {}
    return OMS(master, risk, ledger, router=router, **kwargs)


def child(order_id="o1", post_only=True, side=Side.BUY) -> ChildOrder:
    return ChildOrder(
        order_id=order_id, parent_id="p1", opportunity_id="op", venue="BINANCE",
        instrument_id=INSTRUMENT, side=side, quantity=Decimal("0.02"),
        limit_price=Decimal("60000"), post_only=post_only,
    )


def fill(oms: OMS, order: ChildOrder, cumulative: str, price="60000", ts=10) -> None:
    order.filled_quantity = Decimal(cumulative)
    order.average_price = Decimal(price)
    oms._price_fills(order, ts)
    oms.ledger.apply_fill(order, ts)


def fee_events(ledger: Ledger, order_id: str):
    return [e for e in ledger.economics.events if e.event_type == EconomicEventType.FEE and e.order_id == order_id]


class EconomicLedgerTests(unittest.TestCase):
    def event(self, key, qty, ts=10, kind=EconomicEventType.DEPOSIT, asset="USDT"):
        return EconomicEvent(kind, key, ts, (Posting("BINANCE", "main", asset, Decimal(qty)),), venue="BINANCE",
                             source=EventSource.CONFIGURED)

    def test_duplicate_is_ignored_and_conflicting_reuse_raises(self):
        ledger = EconomicLedger()
        self.assertTrue(ledger.apply(self.event("d1", "100")))
        self.assertFalse(ledger.apply(self.event("d1", "100")))
        self.assertEqual((ledger.balance("BINANCE", "USDT"), ledger.duplicates_ignored), (Decimal("100"), 1))
        with self.assertRaises(ConflictingEventError):
            ledger.apply(self.event("d1", "999"))

    def test_late_events_are_flagged_and_as_of_balances_reconstruct_history(self):
        ledger = EconomicLedger()
        ledger.apply(self.event("a", "100", ts=10))
        ledger.apply(self.event("b", "50", ts=30))
        ledger.apply(self.event("c", "-20", ts=20, kind=EconomicEventType.WITHDRAWAL))  # arrives late
        self.assertEqual(ledger.late_events, 1)
        self.assertEqual(ledger.balance("BINANCE", "USDT", as_of_ns=15), Decimal("100"))
        self.assertEqual(ledger.balance("BINANCE", "USDT", as_of_ns=25), Decimal("80"))
        self.assertEqual(ledger.balance("BINANCE", "USDT"), Decimal("130"))

    def test_corrections_are_reversals_not_rewrites(self):
        ledger = EconomicLedger()
        original = self.event("a", "100")
        ledger.apply(original)
        reversal = ledger.reverse(original.event_id, 20, "operator correction")
        self.assertEqual(ledger.balance("BINANCE", "USDT"), Decimal("0"))
        self.assertEqual([e.event_type for e in ledger.events],
                         [EconomicEventType.DEPOSIT, EconomicEventType.REVERSAL])
        self.assertEqual(reversal.reverses, original.event_id)
        with self.assertRaises(ValueError):
            ledger.reverse(original.event_id, 30, "again")


class FeeAccountingTests(unittest.TestCase):
    """ECO01 acceptance cases 1-9."""

    def test_1_maker_fill_with_positive_fee(self):
        oms = oms_with(fee_master(maker_bps=Decimal("2"), taker_bps=Decimal("5"), source=FeeSource.VERIFIED))
        order = child(post_only=True)
        fill(oms, order, "0.01")
        self.assertEqual((order.fee, order.liquidity, order.fee_rate_bps), (Decimal("0.12"), "MAKER", Decimal("2")))
        (event,) = fee_events(oms.ledger, "o1")
        self.assertEqual(event.postings[0].asset, "USDT")
        self.assertEqual(event.postings[0].quantity, Decimal("-0.12"))
        self.assertEqual(oms.ledger.cash_deltas["BINANCE"], Decimal("-600.12"))

    def test_2_taker_fill_pays_the_higher_rate(self):
        oms = oms_with(fee_master(maker_bps=Decimal("2"), taker_bps=Decimal("5"), source=FeeSource.VERIFIED))
        order = child(post_only=False)
        fill(oms, order, "0.01")
        self.assertEqual((order.fee, order.liquidity), (Decimal("0.30"), "TAKER"))

    def test_3_maker_rebate_is_a_credit_across_partial_fills(self):
        oms = oms_with(fee_master(maker_bps=Decimal("-1"), taker_bps=Decimal("5"), source=FeeSource.VERIFIED))
        order = child(post_only=True)
        fill(oms, order, "0.01", ts=10)
        fill(oms, order, "0.02", ts=20)   # second partial: rebate grows more negative
        self.assertEqual(order.fee, Decimal("-0.12"))
        events = fee_events(oms.ledger, "o1")
        self.assertEqual([e.postings[0].quantity for e in events], [Decimal("0.06"), Decimal("0.06")])
        self.assertEqual(oms.ledger.cash_deltas["BINANCE"], Decimal("-1200") + Decimal("0.12"))

    def test_4_fee_paid_in_base_reduces_base_not_cash(self):
        oms = oms_with(
            fee_master(maker_bps=Decimal("2"), taker_bps=Decimal("10"), fee_asset="BASE",
                       source=FeeSource.VERIFIED),
            opening=[("BINANCE", "USDT", Decimal("10000")), ("BINANCE", "BTC", Decimal("0"))],
        )
        order = child(post_only=False)
        fill(oms, order, "0.01")
        self.assertEqual((order.fee_asset, order.fee_quantity), ("BTC", Decimal("0.00001")))
        self.assertEqual(order.fee, Decimal("0.60"))       # reporting value
        economics = oms.ledger.economics
        self.assertEqual(economics.balance("BINANCE", "BTC"), Decimal("0.00999"))
        self.assertEqual(economics.balance("BINANCE", "USDT"), Decimal("9400"))
        self.assertEqual(oms.ledger.cash_deltas["BINANCE"], Decimal("-600"))   # no quote fee
        self.assertEqual(oms.risk.held_base("BINANCE"), Decimal("0.00999"))

    def test_5_fee_paid_in_quote_settlement_currency(self):
        oms = oms_with(fee_master(maker_bps=Decimal("2"), taker_bps=Decimal("4"), source=FeeSource.VERIFIED))
        order = child(post_only=False, side=Side.SELL)
        fill(oms, order, "0.01")
        self.assertEqual((order.fee_asset, order.fee_quantity), ("USDT", Decimal("0.24")))
        self.assertEqual(oms.ledger.cash_deltas["BINANCE"], Decimal("600") - Decimal("0.24"))

    def test_6_third_token_fee_needs_a_price_and_is_flagged_without_one(self):
        oms = oms_with(fee_master(maker_bps=Decimal("1"), taker_bps=Decimal("7.5"), fee_asset="BNB",
                                  source=FeeSource.VERIFIED))
        oms.asset_prices[("*", "BNB")] = Decimal("600")
        order = child(post_only=False)
        fill(oms, order, "0.01")
        self.assertEqual((order.fee_asset, order.fee), ("BNB", Decimal("0.450")))
        self.assertEqual(order.fee_quantity, Decimal("0.00075"))
        self.assertEqual(oms.ledger.economics.balance("BINANCE", "BNB"), Decimal("-0.00075"))
        self.assertEqual(oms.ledger.cash_deltas["BINANCE"], Decimal("-600"))

        unpriced = oms_with(fee_master(maker_bps=Decimal("1"), taker_bps=Decimal("7.5"), fee_asset="BNB"))
        order = child(post_only=False)
        fill(unpriced, order, "0.01")
        self.assertIn("FEE_ASSET_UNPRICED", order.fee_source)
        (event,) = fee_events(unpriced.ledger, "o1")
        self.assertEqual((event.postings, dict(event.details)["fee_quantity_unknown"]), ((), "true"))
        report = reconcile(
            unpriced.ledger.economics,
            opening=Valuation("USDT", {("BINANCE", "BTC"): Decimal("60000")}),
            closing=Valuation("USDT", {("BINANCE", "BTC"): Decimal("60000")}),
        )
        self.assertEqual(report["status"], "INCOMPLETE")

    def test_7_partial_and_duplicate_fill_reports_are_booked_once(self):
        oms = oms_with(fee_master(maker_bps=Decimal("2"), taker_bps=Decimal("5"), source=FeeSource.VERIFIED))
        order = child(post_only=False)
        fill(oms, order, "0.005")
        fill(oms, order, "0.005")       # duplicate report: no new fill, no new fee
        fill(oms, order, "0.010")
        economics = oms.ledger.economics
        trades = [e for e in economics.events if e.event_type == EconomicEventType.TRADE_FILL]
        self.assertEqual(len(trades), 2)
        self.assertEqual(len(fee_events(oms.ledger, "o1")), 2)
        self.assertEqual(order.fee, Decimal("0.30"))
        self.assertEqual(economics.balance("BINANCE", "BTC"), Decimal("0.010"))

    def test_8_emergency_unwind_is_charged_fees(self):
        oms = oms_with(
            instrument_master(),
            adapters={"BYBIT": RejectingAdapter()},
            residual=ResidualPolicy(max_residual_base=Decimal("0.01"), max_residual_age_ns=0,
                                    hedge_venue_priority=("BINANCE",)),
        )
        plan = oms.execute(opportunity(), books())
        unwinds = [o for o in plan.children if o.order_role == "UNWIND" and o.filled_quantity]
        self.assertTrue(unwinds)
        for order in unwinds:
            self.assertGreater(order.fee, 0)
            self.assertTrue(fee_events(oms.ledger, order.order_id))

    def test_9_unknown_tier_books_no_rebate_and_is_flagged(self):
        oms = oms_with(fee_master(maker_bps=Decimal("-1"), taker_bps=Decimal("5"),
                                  source=FeeSource.ASSUMED, conservative_unverified=True))
        order = child(post_only=True)
        fill(oms, order, "0.01")
        self.assertEqual((order.fee, order.fee_source), (Decimal("0"), "CONSERVATIVE_ASSUMED"))
        taker = child("o2", post_only=False)
        fill(oms, taker, "0.01")
        self.assertEqual((taker.fee, taker.fee_source), (Decimal("0.30"), "ASSUMED"))
        (event,) = fee_events(oms.ledger, "o2")
        self.assertEqual(event.source, EventSource.ASSUMED)

    def test_mixed_liquidity_order_is_priced_per_fill(self):
        oms = oms_with(fee_master(maker_bps=Decimal("2"), taker_bps=Decimal("5"), source=FeeSource.VERIFIED))
        order = child(post_only=False)
        oms._order_liquidity["o1"] = "TAKER"
        fill(oms, order, "0.01")
        oms._order_liquidity["o1"] = "MAKER"           # resting remainder fills later
        fill(oms, order, "0.02")
        self.assertEqual((order.fee, order.liquidity), (Decimal("0.30") + Decimal("0.12"), "MIXED"))

    def test_adapter_labels_liquidity_of_each_fill(self):
        adapter = DeterministicVenueAdapter("BINANCE")
        current = books()
        book = current[("BINANCE", INSTRUMENT)]
        taker = adapter.handle(submit("t1", "60000.00", "0.5"), book)
        self.assertEqual({c.liquidity for c in taker if c.cumulative_filled_quantity}, {"TAKER"})
        resting = adapter.handle(submit("m1", "59995.00", "0.5", tif=TimeInForce.GTC), book)
        self.assertEqual([c.liquidity for c in resting], [""])           # acknowledged only
        book.apply(delta("BINANCE", 2, 5, [], [["60000.00", "0"], ["59995.00", "1"]]))
        (_, callback), = adapter.on_book_update(book)
        self.assertEqual((callback.order_id, callback.liquidity), ("m1", "MAKER"))


class ReconciliationTests(unittest.TestCase):
    def test_flat_account_reconciles_with_zero_pnl(self):
        ledger = EconomicLedger()
        for event in opening_balance_events([("BINANCE", "USDT", Decimal("1000")), ("BINANCE", "BTC", Decimal("1"))]):
            ledger.apply(event)
        prices = Valuation("USDT", {("BINANCE", "BTC"): Decimal("60000")})
        report = reconcile(ledger, opening=prices, closing=prices, product_types={"BINANCE": "SPOT"})
        row = report["venues"]["BINANCE"]
        self.assertEqual(report["status"], "RECONCILED")
        self.assertEqual((row["opening_equity"], row["closing_equity"], row["strategy_equity_pnl"]),
                         ("61000", "61000", "0"))
        self.assertEqual((row["not_modelled"], row["not_applicable"]), (["BORROW", "TRANSFER"], ["FUNDING"]))

    def test_deposits_and_withdrawals_are_flows_not_pnl_and_price_drift_is_revaluation(self):
        ledger = EconomicLedger()
        for event in opening_balance_events([("BINANCE", "USDT", Decimal("1000")), ("BINANCE", "BTC", Decimal("1"))]):
            ledger.apply(event)
        ledger.apply(EconomicEvent(EconomicEventType.DEPOSIT, "dep", 5,
                                   (Posting("BINANCE", "main", "USDT", Decimal("500")),), venue="BINANCE"))
        ledger.apply(EconomicEvent(EconomicEventType.WITHDRAWAL, "wd", 6,
                                   (Posting("BINANCE", "main", "USDT", Decimal("-200")),), venue="BINANCE"))
        report = reconcile(
            ledger, opening=Valuation("USDT", {("BINANCE", "BTC"): Decimal("60000")}),
            closing=Valuation("USDT", {("BINANCE", "BTC"): Decimal("61000")}),
        )
        row = report["venues"]["BINANCE"]
        self.assertEqual((row["deposits"], row["withdrawals"], row["inventory_revaluation"]), ("500", "-200", "1000"))
        self.assertEqual((row["strategy_equity_pnl"], row["identity_residual"]), ("0", "0"))

    def test_missing_mark_is_incomplete_not_assumed(self):
        ledger = EconomicLedger()
        for event in opening_balance_events([("BYBIT", "BTC", Decimal("1"))]):
            ledger.apply(event)
        report = reconcile(ledger, opening=Valuation("USDT"), closing=Valuation("USDT"))
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertEqual(report["venues"]["BYBIT"]["missing_prices"], ["BYBIT:BTC"])

    def test_one_leg_residual_marks_into_equity_and_matches_plan_pnl(self):
        oms = oms_with(
            instrument_master(), adapters={"BYBIT": RejectingAdapter()},
            opening=[(v, a, Decimal("10000") if a == "USDT" else Decimal("0"))
                     for v in ("BINANCE", "BYBIT") for a in ("USDT", "BTC")],
            residual=ResidualPolicy(max_residual_base=Decimal("0.02"), max_residual_age_ns=0,
                                    hedge_venue_priority=("BINANCE",)),
        )
        current = books()
        plan = oms.execute(opportunity(), current)
        self.assertEqual(plan.residual_base, Decimal("0.01"))
        marks = {(v, i): (b.best_bid + b.best_ask) / 2 for (v, i), b in current.items()}
        valuation = Valuation("USDT", {(v, "BTC"): m for (v, _), m in marks.items()})
        state = oms.recalculate_account_state(mark_prices=marks)
        report = reconcile(
            oms.ledger.economics, opening=valuation, closing=valuation,
            trade_pnl_by_venue={v: Decimal(r["realized_pnl"]) + Decimal(r["unrealized_pnl"])
                                for v, r in state["venues"].items()},
            legacy_fees_by_venue={v: Decimal(r["fees"]) for v, r in state["venues"].items()},
            plan_net_pnl=plan.net_pnl,
        )
        self.assertEqual(report["status"], "RECONCILED", report["checks"])
        self.assertLess(abs(Decimal(report["consolidated"]["strategy_equity_pnl"])), Decimal("1"))

    def test_restart_rebuilds_identical_balances(self):
        oms = oms_with(fee_master(maker_bps=Decimal("2"), taker_bps=Decimal("5"), source=FeeSource.VERIFIED))
        order = child(post_only=False)
        fill(oms, order, "0.01")
        rebuilt = Ledger()
        plan = ParentPlan(parent_id="p1", opportunity=opportunity(), children=[order])
        rebuilt.rebuild_from_plans({"p1": plan}, 99)
        self.assertEqual(rebuilt.economics.balances(), oms.ledger.economics.balances())

    def test_backtest_writes_a_reconciled_economic_report(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_backtest(ROOT / "config/research.json", ROOT / "data/samples/two_venue_books.jsonl",
                                  Path(directory))
            report = json.loads((Path(directory) / "economic_reconciliation.json").read_text())
            self.assertTrue((Path(directory) / "economic_events.jsonl").exists())
        self.assertEqual(result["economics"]["status"], "RECONCILED")
        self.assertEqual(report["consolidated"]["identity_residual"], "0.00000")
        self.assertTrue(all(check["passed"] for check in report["checks"]), report["checks"])
        self.assertEqual(report["consolidated"]["strategy_equity_pnl"], result["net_pnl"])


if __name__ == "__main__":
    unittest.main()
