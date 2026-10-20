"""ECO03 spot borrowing: availability, accrual, repayment, recall and reconciliation."""

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.borrow import BorrowEngine, BorrowLine, BorrowRecall, BorrowUnavailable, HOUR_NS
from crypto_stat_arb.config import load_config
from crypto_stat_arb.economics import EconomicEventType, EventSource, Valuation, opening_balance_events, reconcile
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import ChildOrder, Side
from crypto_stat_arb.risk import InventoryModel, InventoryPolicy, RiskEngine, RiskLimits
from crypto_stat_arb.runner import run_backtest
from test_oms import opportunity


ROOT = Path(__file__).parents[1]
SPOT = "BTC-USDT-SPOT"
T0 = 10 * HOUR_NS
MID = Decimal("60000")
RATE = Decimal("0.0001")          # per hour


def line(venue="BINANCE", **kwargs) -> BorrowLine:
    terms = dict(venue=venue, asset="BTC", authorized=True, limit=Decimal("0.05"), rate_per_interval=RATE)
    terms.update(kwargs)
    return BorrowLine(**terms)


def setup(*lines, eligible=True, base="0", recalls=(), policy="FAIL", **kwargs):
    ledger = Ledger()
    for event in opening_balance_events([(v, a, Decimal(q)) for v in ("BINANCE", "BYBIT")
                                         for a, q in (("USDT", "10000"), ("BTC", base))]):
        ledger.economics.apply(event)
    closes = []
    engine = BorrowEngine(
        ledger, lines, spot_instruments={(v, SPOT): ("BTC", "USDT") for v in ("BINANCE", "BYBIT")},
        eligible={(v, "BTC"): eligible for v in ("BINANCE", "BYBIT")}, recalls=recalls,
        settle_lag_ns=0, unfinanced_short_policy=policy,
        on_forced_close=lambda reason, ts: closes.append((reason, ts)), **kwargs,
    )
    for venue in ("BINANCE", "BYBIT"):
        engine.observe(venue, SPOT, T0, MID, MID + 5)
    return ledger, engine, closes


def fill(ledger, order_id, side, qty, *, match_ts, report_ts=None, venue="BINANCE", price="60000"):
    quantity = Decimal(qty)
    ledger.apply_fill(ChildOrder(
        order_id=order_id, parent_id="p", opportunity_id="op", venue=venue, instrument_id=SPOT,
        side=side, quantity=quantity, limit_price=Decimal(price), filled_quantity=quantity,
        average_price=Decimal(price),
        state_updates=[{"event_ts_ns": match_ts, "cumulative_filled_quantity": qty}],
    ), match_ts if report_ts is None else report_ts)


def events(ledger, kind):
    return [e for e in ledger.economics.events if e.event_type == kind]


def interest_charges(ledger):
    reversed_ids = {e.reverses for e in events(ledger, EconomicEventType.REVERSAL)}
    return [-e.postings[0].quantity for e in events(ledger, EconomicEventType.BORROW_ACCRUAL)
            if e.event_id not in reversed_ids]


def bal(ledger, account, venue="BINANCE"):
    return ledger.economics.balance(venue, "BTC", account)


def risk_with(engine, base="0") -> RiskEngine:
    risk = RiskEngine(RiskLimits(
        max_order_notional=Decimal("5000"), max_gross_notional=Decimal("100000"),
        max_residual_base=Decimal("0.02"), max_book_age_ns=1_000_000_000,
    ), inventory=InventoryPolicy(
        model=InventoryModel.PRE_FUNDED, instrument_id=SPOT,
        initial_base_by_venue={"BINANCE": Decimal(base), "BYBIT": Decimal(base)},
        initial_quote_by_venue={"BINANCE": Decimal("100000"), "BYBIT": Decimal("100000")},
    ))
    risk.borrow = engine
    return risk


def nth(n):  # buy BINANCE, sell BYBIT 0.01
    return replace(opportunity("0.01"), opportunity_id=f"op-{n}", source_event_ids=(f"a{n}", f"b{n}"))


class AvailabilityTests(unittest.TestCase):
    def test_1_borrow_permitted_and_available(self):
        _, engine, _ = setup(line("BYBIT"))
        risk = risk_with(engine)
        reservation = risk.reserve(nth(1))
        self.assertEqual(reservation.borrow_by_venue, {"BYBIT": Decimal("0.01")})
        self.assertEqual(risk.inventory_snapshot()["label"], "PRE_FUNDED_INVENTORY_WITH_BORROW_LINES")

    def test_2_permitted_by_instrument_but_unavailable_to_the_account(self):
        for lines, code in (([line("BYBIT", authorized=False)], "NOT_AUTHORIZED"),
                            ([line("BINANCE")], "NOT_AUTHORIZED")):      # line on another venue only
            with self.subTest(code=code):
                _, engine, _ = setup(*lines, eligible=True)
                with self.assertRaisesRegex(ValueError, f"insufficient base inventory: BYBIT .*{code}"):
                    risk_with(engine).reserve(nth(1))

    def test_instrument_not_margin_eligible(self):
        _, engine, _ = setup(line("BYBIT"), eligible=False)
        with self.assertRaisesRegex(ValueError, "NOT_ELIGIBLE"):
            risk_with(engine).reserve(nth(1))

    def test_3_borrow_limit_exhausted_counts_reserved_capacity(self):
        _, engine, _ = setup(line("BYBIT", limit=Decimal("0.015")))
        risk = risk_with(engine)
        first = risk.reserve(nth(1))
        with self.assertRaisesRegex(ValueError, "LIMIT_EXHAUSTED"):
            risk.reserve(nth(2))
        risk.release(first)
        self.assertEqual(risk.reserve(nth(2)).borrow_by_venue, {"BYBIT": Decimal("0.01")})

    def test_held_base_is_used_before_borrowing(self):
        _, engine, _ = setup(line("BYBIT"), base="0.004")
        reservation = risk_with(engine, base="0.004").reserve(nth(1))
        self.assertEqual(reservation.borrow_by_venue, {"BYBIT": Decimal("0.006")})

    def test_8_short_without_borrow_authorization_is_refused(self):
        _, engine, _ = setup(line("BYBIT", authorized=False))
        with self.assertRaises(BorrowUnavailable) as refused:
            engine.require("BYBIT", "BTC", Decimal("0.01"))
        self.assertEqual(refused.exception.code, "NOT_AUTHORIZED")


class LifecycleTests(unittest.TestCase):
    def test_sell_without_base_opens_a_loan_at_the_match_time(self):
        ledger, engine, _ = setup(line())
        fill(ledger, "s", Side.SELL, "0.01", match_ts=T0 + 100, report_ts=T0 + 900)
        [opened] = events(ledger, EconomicEventType.BORROW_OPEN)
        self.assertEqual(opened.effective, T0 + 100)
        self.assertEqual((bal(ledger, "main"), bal(ledger, "loan")), (Decimal("0"), Decimal("-0.01")))
        # Binance-style: the first interval is charged when the loan opens.
        self.assertEqual(interest_charges(ledger), [Decimal("0.000001")])
        self.assertEqual(dict(opened.details)["borrow_quantity"], "0.01")

    def test_4_accrual_over_several_intervals(self):
        ledger, engine, _ = setup(line())
        fill(ledger, "s", Side.SELL, "0.01", match_ts=T0 + HOUR_NS // 2)
        engine.settle_until(T0 + 3 * HOUR_NS + 1)
        self.assertEqual(interest_charges(ledger), [Decimal("0.000001")] * 4)    # open + 3 boundaries
        accrual = events(ledger, EconomicEventType.BORROW_ACCRUAL)[-1]
        self.assertEqual(accrual.reporting_value, Decimal("-0.000001") * MID)
        self.assertEqual(dict(accrual.details)["accrual_start"], str(T0 + 3 * HOUR_NS))
        self.assertEqual(bal(ledger, "loan_interest"), Decimal("-0.000004"))
        state = ledger.recalculate_account_state()
        self.assertEqual(Decimal(state["venues"]["BINANCE"]["borrow_cost"]), Decimal("0.24"))

    def test_5_rate_change_during_an_open_borrow(self):
        ledger, engine, _ = setup(line(rate_changes=((T0 + 2 * HOUR_NS, Decimal("0.0003")),)))
        fill(ledger, "s", Side.SELL, "0.01", match_ts=T0 + HOUR_NS // 2)
        engine.settle_until(T0 + 3 * HOUR_NS)
        self.assertEqual(interest_charges(ledger),
                         [Decimal("0.000001"), Decimal("0.000001"), Decimal("0.000003"), Decimal("0.000003")])

    def test_capacity_change_closes_the_line(self):
        ledger, engine, _ = setup(line(limit_changes=((T0 + HOUR_NS, Decimal("0")),)))
        engine.now_ns = T0 + HOUR_NS
        self.assertEqual(engine.availability("BINANCE", "BTC")[1], "LIMIT_EXHAUSTED")

    def test_6_repayment_pays_interest_first_and_duplicates_are_ignored(self):
        ledger, engine, _ = setup(line(), repay_policy="manual")
        fill(ledger, "s", Side.SELL, "0.01", match_ts=T0 + 1)
        engine.settle_until(T0 + HOUR_NS)                       # interest 0.000002
        fill(ledger, "b", Side.BUY, "0.005", match_ts=T0 + HOUR_NS + 5)
        self.assertTrue(engine.repay("BINANCE", "BTC", Decimal("0.004"), T0 + HOUR_NS + 10, repay_id="r1"))
        self.assertFalse(engine.repay("BINANCE", "BTC", Decimal("0.004"), T0 + HOUR_NS + 11, repay_id="r1"))
        self.assertEqual(bal(ledger, "loan_interest"), Decimal("0"))
        self.assertEqual(bal(ledger, "loan"), Decimal("-0.006002"))
        self.assertEqual(bal(ledger, "main"), Decimal("0.001"))
        with self.assertRaisesRegex(ValueError, "exceeds the BTC held"):
            engine.repay("BINANCE", "BTC", Decimal("0.002"), T0 + HOUR_NS + 12, repay_id="r2")
        with self.assertRaisesRegex(ValueError, "exceeds what is owed"):
            engine.repay("BINANCE", "BTC", Decimal("1"), T0 + HOUR_NS + 12, repay_id="r3")

    def test_buy_fill_repays_automatically(self):
        ledger, engine, _ = setup(line())
        fill(ledger, "s", Side.SELL, "0.01", match_ts=T0 + 1)
        fill(ledger, "b", Side.BUY, "0.02", match_ts=T0 + 2)
        self.assertEqual((bal(ledger, "loan"), bal(ledger, "loan_interest")), (Decimal("0"), Decimal("0")))
        self.assertEqual(bal(ledger, "main"), Decimal("0.009999"))         # 0.02 - 0.01 - 0.000001 interest

    def test_late_borrow_rebooks_the_interest_boundary(self):
        ledger, engine, _ = setup(line(charge_on_open=False))
        fill(ledger, "s1", Side.SELL, "0.01", match_ts=T0 + 1)
        engine.settle_until(T0 + HOUR_NS)
        self.assertEqual(interest_charges(ledger), [Decimal("0.000001")])
        fill(ledger, "s2", Side.SELL, "0.01", match_ts=T0 + HOUR_NS - 10, report_ts=T0 + HOUR_NS + 10)
        self.assertEqual(interest_charges(ledger), [Decimal("0.000002")])
        self.assertEqual(len(events(ledger, EconomicEventType.REVERSAL)), 1)
        self.assertEqual(engine.rebooked, 1)

    def test_7_recall_with_an_open_short_forces_a_buy_in(self):
        recall = BorrowRecall("rc1", "BINANCE", "BTC", effective_ns=T0 + 10, deadline_ns=T0 + 20,
                              forced_close_price=Decimal("61000"))
        ledger, engine, closes = setup(line(), recalls=[recall], forced_close_fee_bps=Decimal("10"))
        fill(ledger, "s", Side.SELL, "0.01", match_ts=T0 + 1)
        engine.process(T0 + 10)
        self.assertEqual(engine.availability("BINANCE", "BTC")[1], "RECALLED")   # new shorts blocked now
        self.assertEqual(len(events(ledger, EconomicEventType.BORROW_RECALL)), 1)
        engine.process(T0 + 20)
        [buy_in] = events(ledger, EconomicEventType.BORROW_FORCED_CLOSE)
        self.assertEqual(buy_in.postings[0].quantity, Decimal("0.010001"))      # principal + open interest
        self.assertEqual((bal(ledger, "loan"), bal(ledger, "loan_interest"), bal(ledger, "main")),
                         (Decimal("0"), Decimal("0"), Decimal("0")))
        self.assertEqual(len(closes), 1)
        report = engine.report()
        self.assertEqual(report["forced_closes"][0]["price_source"], "OBSERVED")
        self.assertEqual(report["recalls"][0]["resolved"], True)
        self.assertEqual(ledger.positions[("BINANCE", SPOT)], Decimal("0.000001"))
        self.assertEqual(engine.availability("BINANCE", "BTC")[1], "RECALLED")

    def test_recall_repaid_from_held_base_needs_no_buy_in(self):
        recall = BorrowRecall("rc1", "BINANCE", "BTC", effective_ns=T0 + 10, deadline_ns=T0 + 20)
        ledger, engine, closes = setup(line(), recalls=[recall], repay_policy="manual")
        fill(ledger, "s", Side.SELL, "0.01", match_ts=T0 + 1)
        fill(ledger, "b", Side.BUY, "0.02", match_ts=T0 + 2)
        engine.process(T0 + 20)
        self.assertEqual(events(ledger, EconomicEventType.BORROW_FORCED_CLOSE), [])
        self.assertEqual(bal(ledger, "loan"), Decimal("0"))
        self.assertEqual(closes, [])

    def test_buy_in_without_a_price_is_flagged_not_assumed(self):
        recall = BorrowRecall("rc1", "BINANCE", "BTC", effective_ns=T0 + 10, deadline_ns=T0 + 20)
        ledger, engine, _ = setup(line(), recalls=[recall])
        engine._prices.clear()
        fill(ledger, "s", Side.SELL, "0.01", match_ts=T0 + 1)
        engine.process(T0 + 20)
        self.assertEqual(engine.flags[-1]["code"], "FORCED_CLOSE_PRICE_MISSING")
        self.assertEqual(bal(ledger, "loan"), Decimal("-0.01"))

    def test_unfinanced_short_fails_pre_funded_and_is_noted_when_unconstrained(self):
        ledger, engine, _ = setup()
        fill(ledger, "s", Side.SELL, "0.01", match_ts=T0 + 1)
        self.assertEqual(engine.flags[0]["code"], "UNFINANCED_SHORT:NOT_AUTHORIZED")
        self.assertEqual(engine.report()["status"], "FAIL")
        ledger, engine, _ = setup(policy="NOTE")
        fill(ledger, "s", Side.SELL, "0.01", match_ts=T0 + 1)
        self.assertEqual((engine.flags, engine.notes[0]["code"]), ([], "UNFINANCED_SHORT:NOT_AUTHORIZED"))


class ReconciliationTests(unittest.TestCase):
    def reconcile(self, ledger, engine, close=MID):
        marks = {(v, SPOT): close for v in ("BINANCE", "BYBIT")}
        state = ledger.recalculate_account_state(mark_prices=marks)
        prices = Valuation("USDT", {(v, "BTC"): close for v in ("BINANCE", "BYBIT")})
        return reconcile(
            ledger.economics, opening=Valuation("USDT", {(v, "BTC"): MID for v in ("BINANCE", "BYBIT")}),
            closing=prices,
            trade_pnl_by_venue={v: Decimal(r["realized_pnl"]) + Decimal(r["unrealized_pnl"])
                                for v, r in state["venues"].items()},
            legacy_fees_by_venue={v: Decimal(r["fees"]) for v, r in state["venues"].items()},
            legacy_borrow_cost_by_venue={v: Decimal(r["borrow_cost"]) for v, r in state["venues"].items()},
            borrow_status_by_venue=engine.status_by_venue(("BINANCE", "BYBIT")),
            borrow_flags_by_venue=engine.blocking_flags_by_venue(),
            plan_net_pnl=Decimal("0"),
        )

    def test_borrow_costs_reconcile_with_revaluation_shown_separately(self):
        ledger, engine, _ = setup(line())
        fill(ledger, "s", Side.SELL, "0.01", match_ts=T0 + 1)
        engine.settle_until(T0 + HOUR_NS)
        report = self.reconcile(ledger, engine, close=MID)
        row = report["venues"]["BINANCE"]
        self.assertEqual(report["status"], "RECONCILED", report["checks"])
        self.assertEqual(Decimal(row["borrow_cost_at_accrual_time"]), Decimal("0.12"))
        self.assertEqual(Decimal(row["borrow_accrual"]), Decimal("-0.12"))
        self.assertEqual(Decimal(row["identity_residual"]), 0)
        self.assertEqual(row["borrow_status"], "MODELLED")
        self.assertNotIn("BORROW", row["not_modelled"] + row["not_applicable"])
        self.assertIn("BORROW", report["venues"]["BYBIT"]["not_applicable"])
        higher = self.reconcile(ledger, engine, close=Decimal("66000"))["venues"]["BINANCE"]
        self.assertEqual(Decimal(higher["borrow_interest_revaluation"]), Decimal("-0.012"))

    def test_forced_close_reconciles_and_stays_out_of_plan_pnl(self):
        recall = BorrowRecall("rc1", "BINANCE", "BTC", effective_ns=T0 + 10, deadline_ns=T0 + 20,
                              forced_close_price=Decimal("61000"))
        ledger, engine, _ = setup(line(), recalls=[recall])
        fill(ledger, "s", Side.SELL, "0.01", match_ts=T0 + 1)
        engine.process(T0 + 20)
        report = self.reconcile(ledger, engine)
        self.assertEqual(report["status"], "RECONCILED", report["checks"])
        self.assertLess(Decimal(report["venues"]["BINANCE"]["borrow_forced_close"]), 0)

    def test_unfinanced_short_fails_the_reconciliation(self):
        ledger, engine, _ = setup()
        fill(ledger, "s", Side.SELL, "0.01", match_ts=T0 + 1)
        report = self.reconcile(ledger, engine)
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("BINANCE_no_unfinanced_short", [c["check"] for c in report["checks"] if not c["passed"]])


class ConfigAndBacktestTests(unittest.TestCase):
    def write(self, directory, mutate) -> Path:
        raw = json.loads((ROOT / "config/research.json").read_text())
        raw["risk"]["inventory"]["initial_base_by_venue"] = {"BINANCE": "0.05", "BYBIT": "0"}
        for row in raw["instruments"]:
            row["borrow_applicable"] = True
        mutate(raw)
        path = Path(directory) / "borrow.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def test_daily_rate_is_converted_per_interval_and_bad_terms_fail_at_load(self):
        line_ = BorrowLine.from_config({"venue": "BYBIT", "asset": "BTC", "authorized": True,
                                        "limit": "1", "daily_rate": "0.0024", "interval_hours": 1})
        self.assertEqual(line_.rate_per_interval, Decimal("0.0001"))
        self.assertEqual(line_.source, EventSource.ASSUMED)
        bad = {
            "no_rate": {"lines": [{"venue": "BYBIT", "asset": "BTC", "authorized": True}]},
            "recall_without_line": {"recalls": [{"recall_id": "x", "venue": "BYBIT", "asset": "BTC",
                                                  "effective_from": 1}]},
        }
        for name, section in bad.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises((ValueError, KeyError)):
                    load_config(self.write(directory, lambda raw: raw["risk"].update(borrow=section)))

    def run_sample(self, borrow):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "out"
            result = run_backtest(self.write(directory, lambda raw: raw["risk"].update(borrow=borrow)),
                                  ROOT / "data/samples/two_venue_books.jsonl", out)
            files = {name: json.loads((out / name).read_text())
                     for name in ("economic_reconciliation.json", "borrow_report.json", "account_state.json",
                                  "kill_switch_status.json")}
        return result, files

    def test_backtest_without_a_line_refuses_the_short(self):
        result, files = self.run_sample({})
        self.assertEqual(result["economics"]["status"], "RECONCILED")
        self.assertEqual(files["borrow_report.json"]["status"], "NOT_CONFIGURED")
        self.assertNotIn("BYBIT:BTC-USDT-SPOT", files["account_state.json"]["positions"])

    def test_backtest_borrows_for_the_short_leg_and_reconciles(self):
        line_ = {"venue": "BYBIT", "asset": "BTC", "authorized": True, "limit": "0.05",
                 "daily_rate": "0.0024", "interval_hours": 1, "source": "CONFIGURED"}
        result, files = self.run_sample({"lines": [line_], "settle_lag_ms": 0})
        report, borrow = files["economic_reconciliation.json"], files["borrow_report.json"]
        self.assertEqual(result["economics"]["status"], "RECONCILED", report["checks"])
        self.assertEqual(borrow["status"], "COMPLETE")
        self.assertEqual(Decimal(borrow["lines"]["BYBIT:BTC"]["principal"]), Decimal("0.01"))
        self.assertEqual(report["venues"]["BYBIT"]["borrow_status"], "MODELLED")
        self.assertGreater(Decimal(files["account_state.json"]["venues"]["BYBIT"]["borrow_cost"]), 0)
        self.assertTrue(all(check["passed"] for check in report["checks"]), report["checks"])

    def test_backtest_recall_buys_in_and_stops_trading(self):
        line_ = {"venue": "BYBIT", "asset": "BTC", "authorized": True, "limit": "0.05",
                 "daily_rate": "0.0024", "interval_hours": 1}
        recall = {"recall_id": "rc", "venue": "BYBIT", "asset": "BTC", "effective_from": 1_050_000_000,
                  "deadline_ms": 40, "forced_close_price": "60100"}
        result, files = self.run_sample({"lines": [line_], "recalls": [recall], "settle_lag_ms": 0,
                                         "forced_close_fee_bps": "10"})
        report, borrow = files["economic_reconciliation.json"], files["borrow_report.json"]
        self.assertEqual(result["economics"]["status"], "RECONCILED", report["checks"])
        self.assertEqual(len(borrow["forced_closes"]), 1)
        self.assertEqual(Decimal(borrow["lines"]["BYBIT:BTC"]["principal"]), 0)
        self.assertEqual(files["kill_switch_status.json"]["state"], "ACTIVE")
        self.assertTrue(all(check["passed"] for check in report["checks"]), report["checks"])


if __name__ == "__main__":
    unittest.main()
