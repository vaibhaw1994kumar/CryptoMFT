"""ECO04: risk tiers, collateral haircuts, cross/isolated margin and simulated liquidation."""

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.economics import EconomicEventType, opening_balance_events
from crypto_stat_arb.instruments import InstrumentMaster
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.margin import MarginConfig, TierSchedule, RiskTier
from crypto_stat_arb.models import ChildOrder, Instrument, MarketEvent, Opportunity, OrderState, Side
from crypto_stat_arb.plan_store import ParentPlanStore
from crypto_stat_arb.risk import AccountRiskSnapshot, MarginState, RiskEngine, RiskLimits, VenueRiskSnapshot
from crypto_stat_arb.runner import run_backtest


ROOT = Path(__file__).parents[1]
PERP = "BTC-USDT-PERP"
TIERS = [
    # Continuous at 50: 50 x 0.01 = 0.5 = 50 x 0.05 - 2.
    {"max_notional": "50", "maintenance_rate": "0.01", "initial_rate": "0.02"},
    {"max_notional": "1000", "maintenance_rate": "0.05", "initial_rate": "0.10",
     "maintenance_deduction": "2", "initial_deduction": "4"},
]


def master() -> InstrumentMaster:
    return InstrumentMaster([Instrument(
        instrument_id=PERP, venue=venue, symbol="BTCUSDT", base="BTC", quote="USDT",
        tick_size=Decimal("0.1"), quantity_step=Decimal("0.001"), min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"), taker_fee_bps=Decimal("4"), product_type="PERP",
        leverage=Decimal("10"), settlement_asset="USDT", funding_interval_ns=8 * 3_600 * 10**9,
    ) for venue in ("BINANCE", "BYBIT")])


def margin_config(**venue_overrides) -> MarginConfig:
    venues = {}
    for venue in ("BINANCE", "BYBIT"):
        row = {"mode": "CROSS", "tiers": {PERP: TIERS},
               "collateral": [{"asset": "USDT", "haircut": "0"}, {"asset": "BTC", "haircut": "0.10"}],
               "liquidation": {"enabled": True, "fee_rate": "0.0125"}}
        row.update(venue_overrides.get(venue, {}))
        venues[venue] = row
    return MarginConfig.from_config({"venues": venues})


def setup(usdt=("10", "10"), config=None, extra=()):
    ledger = Ledger()
    ledger.asset_resolver = lambda venue, instrument: (instrument, "USDT")
    rows = [("BINANCE", "USDT", Decimal(usdt[0])), ("BYBIT", "USDT", Decimal(usdt[1])), *extra]
    for event in opening_balance_events(rows):
        ledger.economics.apply(event)
    accounts = {v: AccountRiskSnapshot(venue=v, margin_enabled=True) for v in ("BINANCE", "BYBIT")}
    risk = RiskEngine(RiskLimits(
        max_order_notional=Decimal("100000"), max_gross_notional=Decimal("200000"),
        max_residual_base=Decimal("10"), max_book_age_ns=1_000_000_000,
    ), accounts)
    risk.margin_config = config or margin_config()
    risk.instruments = master()
    risk.ledger = ledger
    return ledger, risk


def fill(ledger, venue, side, qty, price="100", order_id=None):
    quantity = Decimal(qty)
    ledger.apply_fill(ChildOrder(
        order_id=order_id or f"{venue}-{side.value}-{qty}-{price}", parent_id="p", opportunity_id="op",
        venue=venue, instrument_id=PERP, side=side, quantity=quantity, limit_price=Decimal(price),
        state=OrderState.FILLED, filled_quantity=quantity, average_price=Decimal(price),
    ), 1)


def evaluate(ledger, risk, marks, **kwargs):
    report = risk.evaluate_margin(ledger=ledger, now_ns=10, mark_prices={
        (venue, PERP): Decimal(mark) for venue, mark in marks.items()}, **kwargs)
    return {row.venue: row for row in report.venues}


class TierTests(unittest.TestCase):
    def test_schedule_is_continuous_at_the_boundary_and_refuses_above_the_last_tier(self):
        schedule = margin_config().venues["BINANCE"].tiers[PERP]
        self.assertEqual(schedule.maintenance(Decimal("50")), Decimal("0.50"))
        self.assertEqual(schedule.maintenance(Decimal("50.01")), Decimal("0.5005"))   # tier 2 rate, no jump
        self.assertEqual(schedule.tier(Decimal("50.01"))[0], 1)
        self.assertIsNone(schedule.maintenance(Decimal("1000.01")))
        no_deduction = TierSchedule((RiskTier(Decimal("50"), Decimal("0.01"), Decimal("0.02")),
                                     RiskTier(Decimal("1000"), Decimal("0.05"), Decimal("0.10"))))
        self.assertEqual(no_deduction.maintenance(Decimal("50.01")), Decimal("2.5005"))  # documented jump
        with self.assertRaises(ValueError):
            TierSchedule((RiskTier(Decimal("100"), Decimal("0.01"), Decimal("0.02")),
                          RiskTier(Decimal("50"), Decimal("0.05"), Decimal("0.10"))))

    def test_position_crossing_a_risk_tier_boundary(self):
        ledger, risk = setup(usdt=("100", "10"))
        fill(ledger, "BINANCE", Side.BUY, "0.4")                 # 40 notional: tier 0
        row = evaluate(ledger, risk, {"BINANCE": "100"})["BINANCE"]
        self.assertEqual((row.details["positions"][0]["tier"], row.maintenance_margin_required),
                         (0, Decimal("0.400")))
        fill(ledger, "BINANCE", Side.BUY, "0.2", order_id="more")  # 60 notional: tier 1
        row = evaluate(ledger, risk, {"BINANCE": "100"})["BINANCE"]
        self.assertEqual((row.details["positions"][0]["tier"], row.maintenance_margin_required),
                         (1, Decimal("1.000")))                   # 60 x 0.05 - 2

    def test_healthy_warning_and_breach_states(self):
        for mark, expected in (("100", MarginState.HEALTHY), ("93", MarginState.WARNING),
                               ("92", MarginState.BREACH)):
            with self.subTest(mark=mark):
                ledger, risk = setup()
                fill(ledger, "BINANCE", Side.BUY, "1")           # long 1 at 100 with 10 USDT
                row = evaluate(ledger, risk, {"BINANCE": mark})["BINANCE"]
                self.assertEqual(row.state, expected)
                # Balance = 10 USDT - 100 paid + 1 x mark, at full value (no haircut on the negative USDT).
                self.assertEqual(row.margin_balance, Decimal(mark) - 90)
                if expected == MarginState.BREACH:
                    self.assertTrue(risk.kill_switch)
                    self.assertEqual(row.details["breached_positions"], [PERP])

    def test_position_above_the_last_tier_is_a_breach(self):
        ledger, risk = setup(usdt=("100000", "10"))
        fill(ledger, "BINANCE", Side.BUY, "20")                  # 2000 notional > 1000 cap
        row = evaluate(ledger, risk, {"BINANCE": "100"})["BINANCE"]
        self.assertEqual(row.state, MarginState.BREACH)
        self.assertIn(f"POSITION_ABOVE_RISK_LIMIT:{PERP}", row.alert_codes)


class CollateralTests(unittest.TestCase):
    def test_collateral_asset_receives_a_haircut_and_ineligible_assets_count_zero(self):
        ledger, risk = setup(usdt=("0", "0"), extra=[("BYBIT", "BTC", Decimal("1")), ("BYBIT", "ETH", Decimal("5"))])
        risk.asset_prices = {("BYBIT", "BTC"): Decimal("100"), ("BYBIT", "ETH"): Decimal("10")}
        fill(ledger, "BYBIT", Side.SELL, "0.5")                  # short 0.5 at 100: +50 USDT
        row = evaluate(ledger, risk, {"BYBIT": "100"})["BYBIT"]
        kinds = {r["asset"]: (r["kind"], r["value"]) for r in row.details["collateral"]}
        self.assertEqual((kinds["BTC"][0], Decimal(kinds["BTC"][1])), ("COLLATERAL", Decimal("90")))  # 1 x 100 x 0.9
        self.assertEqual(kinds["ETH"][0], "INELIGIBLE")
        self.assertEqual(Decimal(kinds["ETH"][1]), 0)
        self.assertEqual(kinds[PERP][0], "POSITION")                           # contract value, no haircut
        self.assertEqual(row.margin_balance, Decimal("90.0"))                  # 50 + 90 - 50

    def test_without_a_collateral_list_only_the_quote_is_eligible(self):
        config = margin_config(BYBIT={"collateral": None})
        ledger, risk = setup(usdt=("0", "10"), config=config, extra=[("BYBIT", "BTC", Decimal("1"))])
        risk.asset_prices = {("BYBIT", "BTC"): Decimal("100")}
        row = evaluate(ledger, risk, {"BYBIT": "100"})["BYBIT"]
        self.assertEqual(row.margin_balance, Decimal("10"))
        self.assertIn("quote asset only", row.details["collateral_rule"])

    def test_unpriced_liability_fails_closed(self):
        ledger, risk = setup(extra=[("BINANCE", "SOL", Decimal("-1"))])
        fill(ledger, "BINANCE", Side.BUY, "0.1")
        row = evaluate(ledger, risk, {"BINANCE": "100"})["BINANCE"]
        self.assertEqual(row.state, MarginState.BREACH)
        self.assertIn("COLLATERAL_PRICE_MISSING:SOL", row.alert_codes)


class ModeTests(unittest.TestCase):
    def test_isolated_venue_accounts_with_opposing_positions(self):
        # Long BINANCE, short BYBIT, each with 10 USDT: flat as a portfolio. The
        # price falls: BYBIT's gain cannot cover BINANCE, which breaches alone.
        ledger, risk = setup()
        fill(ledger, "BINANCE", Side.BUY, "1")
        fill(ledger, "BYBIT", Side.SELL, "1")
        rows = evaluate(ledger, risk, {"BINANCE": "90", "BYBIT": "90"})
        self.assertEqual(rows["BINANCE"].state, MarginState.BREACH)
        self.assertEqual(rows["BYBIT"].state, MarginState.HEALTHY)
        self.assertEqual(rows["BINANCE"].margin_balance + rows["BYBIT"].margin_balance, Decimal("20"))

    def test_isolated_position_breaches_while_the_wallet_is_rich(self):
        config = margin_config(BINANCE={"mode": "ISOLATED", "isolated_margin": {PERP: "5"}})
        ledger, risk = setup(usdt=("1000", "10"), config=config)
        fill(ledger, "BINANCE", Side.BUY, "1")
        row = evaluate(ledger, risk, {"BINANCE": "97"})["BINANCE"]   # PnL -3: isolated equity 2 < MM 2.85
        position = row.details["positions"][0]
        self.assertEqual((Decimal(position["isolated_equity"]), position["isolated_state"]), (Decimal("2"), "BREACH"))
        self.assertEqual(row.state, MarginState.BREACH)
        self.assertEqual(row.details["breached_positions"], [PERP])
        # The same position in CROSS mode is carried by the 1000 USDT wallet.
        ledger, risk = setup(usdt=("1000", "10"))
        fill(ledger, "BINANCE", Side.BUY, "1")
        self.assertEqual(evaluate(ledger, risk, {"BINANCE": "97"})["BINANCE"].state, MarginState.HEALTHY)

    def test_isolated_mode_needs_allocations(self):
        with self.assertRaisesRegex(ValueError, "isolated_margin"):
            margin_config(BINANCE={"mode": "ISOLATED"})

    def test_venue_reported_margin_lower_than_local(self):
        ledger, risk = setup()
        fill(ledger, "BINANCE", Side.BUY, "1")
        reported = (VenueRiskSnapshot(venue="BINANCE", instrument_id=PERP, position_base=Decimal("1"),
                                      balance_delta=Decimal("0"), margin_balance=Decimal("3.5")),)
        row = evaluate(ledger, risk, {"BINANCE": "100"}, venue_risk=reported)["BINANCE"]
        self.assertEqual((row.margin_balance, row.effective_margin_balance), (Decimal("10"), Decimal("3.5")))
        self.assertIn("VENUE_MARGIN_BELOW_LOCAL", row.alert_codes)
        self.assertEqual(row.state, MarginState.WARNING)          # 3.5 - 3 < 3 x 0.25: local alone is HEALTHY


class ReservationTests(unittest.TestCase):
    def opportunity(self, n, qty="0.3"):
        return Opportunity(
            opportunity_id=f"op{n}", strategy_id="s", instrument_id=PERP, observed_ts_ns=5,
            buy_venue="BINANCE", sell_venue="BYBIT", buy_price=Decimal("100"), sell_price=Decimal("100.5"),
            quantity=Decimal(qty), gross_edge_bps=Decimal("50"), estimated_net_edge_bps=Decimal("40"),
            source_event_ids=(f"a{n}", f"b{n}"),
        )

    def test_open_orders_and_reserved_legs_consume_margin(self):
        ledger, risk = setup(usdt=("4", "4"))
        risk.margin_marks = {(v, PERP): Decimal("100") for v in ("BINANCE", "BYBIT")}
        first = risk.reserve(self.opportunity(1))
        # 30 notional at tier 0: initial 0.6; no notional cash for a margined perp leg.
        self.assertEqual(first.margin_by_venue["BINANCE"], Decimal("0.600"))
        self.assertEqual(first.cash_by_venue, {"BINANCE": Decimal("0")})
        # The reserved 0.3 counts in the next leg's worst case: 0.6 -> 60 notional at tier 1.
        second = risk.reserve(self.opportunity(2))
        self.assertEqual(second.margin_by_venue["BINANCE"], Decimal("1.400"))   # (60x0.10-4) - 0.6
        # An open hedge order counts too; with it, free margin no longer covers another leg.
        risk.margin_open_orders = {("BINANCE", PERP): (Decimal("0.3"), Decimal("0"))}
        with self.assertRaisesRegex(ValueError, "insufficient margin: BINANCE"):
            risk.reserve(self.opportunity(3, qty="0.4"))

    def test_exposure_above_the_last_tier_is_refused(self):
        ledger, risk = setup(usdt=("100000", "100000"))
        with self.assertRaisesRegex(ValueError, "above the last risk tier"):
            risk.reserve(self.opportunity(1, qty="20"))


def snapshot(venue, ts, sequence, bids, asks):
    native = {"lastUpdateId": sequence} if venue == "BINANCE" else {"u": sequence}
    return MarketEvent.from_dict({
        "event_type": "BOOK_SNAPSHOT", "venue": venue, "instrument_id": PERP,
        "exchange_ts_ns": ts - 100_000, "receive_ts_ns": ts, "sequence": sequence,
        "bids": bids, "asks": asks, "native": native,
    })


def write_session(directory: Path, rows) -> tuple[Path, Path]:
    raw = json.loads((ROOT / "config/research.json").read_text())
    raw["strategy"]["instrument_id"] = PERP
    raw["risk"]["inventory"] = {"model": "unconstrained"}
    raw["risk"]["initial_capital_by_venue"] = {"BINANCE": "40", "BYBIT": "40"}
    for row in raw["instruments"]:
        row.update(instrument_id=PERP, product_type="PERP", funding_interval_hours=8)
    for row in raw.get("fees", []):
        row["instrument_id"] = PERP
    raw["risk"]["margin"] = {"evaluate_interval_ms": 1, "venues": {
        venue: {"mode": "CROSS", "tiers": {PERP: [{"max_notional": "100000", "maintenance_rate": "0.05",
                                                   "initial_rate": "0.06"}]},
                "liquidation": {"enabled": True, "fee_rate": "0.0125"}}
        for venue in ("BINANCE", "BYBIT")
    }}
    config, events = directory / "margin.json", directory / "events.jsonl"
    config.write_text(json.dumps(raw), encoding="utf-8")
    events.write_text("".join(json.dumps({
        "event_type": e.event_type.value, "venue": e.venue, "instrument_id": e.instrument_id,
        "exchange_ts_ns": e.exchange_ts_ns, "receive_ts_ns": e.receive_ts_ns, "sequence": e.sequence,
        "bids": [[str(l.price), str(l.quantity)] for l in e.bids],
        "asks": [[str(l.price), str(l.quantity)] for l in e.asks], "native": e.native,
    }) + "\n" for e in rows), encoding="utf-8")
    return config, events


SECOND = 1_000_000_000


class LiquidationSessionTests(unittest.TestCase):
    def rows(self):
        return [
            # Arb: buy BINANCE perp at 60000, sell BYBIT perp at 60060 (0.01 each, 40 USDT a venue).
            snapshot("BINANCE", 1 * SECOND, 100, [["59999.90", "1"]], [["60000.00", "1"]]),
            snapshot("BYBIT", 1 * SECOND + 10**6, 900, [["60060.00", "1"]], [["60060.10", "1"]]),
            # Both venues fall 2000: the hedged book is flat, but BINANCE's long breaches
            # (equity ~20 < MM 29) and the BINANCE bid only shows 0.004.
            snapshot("BINANCE", 2 * SECOND, 101, [["58000.00", "0.004"]], [["58000.10", "1"]]),
            snapshot("BYBIT", 2 * SECOND + 10**6, 901, [["58000.00", "1"]], [["58000.10", "1"]]),
            # More depth arrives: the residual is liquidated on this book update.
            snapshot("BINANCE", 3 * SECOND, 102, [["57990.00", "1"]], [["57990.10", "1"]]),
            snapshot("BYBIT", 3 * SECOND + 10**6, 902, [["57990.00", "1"]], [["57990.10", "1"]]),
        ]

    def test_breach_liquidates_with_residual_then_completes_and_reconciles(self):
        with tempfile.TemporaryDirectory() as directory:
            config, events = write_session(Path(directory), self.rows())
            out = Path(directory) / "out"
            result = run_backtest(config, events, out)
            margin = json.loads((out / "margin_status.json").read_text())
            report = json.loads((out / "economic_reconciliation.json").read_text())
            state = json.loads((out / "account_state.json").read_text())
            kill = json.loads((out / "kill_switch_status.json").read_text())
        liquidations = [e for e in margin["liquidations"] if e["code"].startswith("LIQUIDAT")]
        self.assertEqual([e["code"] for e in liquidations], ["LIQUIDATION_RESIDUAL", "LIQUIDATED"])
        first, second = liquidations
        self.assertEqual((first["venue"], first["filled"], first["residual"]), ("BINANCE", "0.004", "0.006"))
        self.assertEqual(Decimal(first["fee"]), Decimal("0.004") * Decimal("58000") * Decimal("0.0125"))
        self.assertEqual((second["filled"], Decimal(second["residual"])), ("0.006", 0))
        self.assertEqual(Decimal(state["positions"][f"BINANCE:{PERP}"]["quantity"]), 0)
        self.assertNotEqual(Decimal(state["positions"][f"BYBIT:{PERP}"]["quantity"]), 0)   # now unhedged
        self.assertEqual(kill["state"], "ACTIVE")
        self.assertTrue(margin["evaluated_during_session"])
        self.assertEqual(margin["unsupported"]["adl"], "UNSUPPORTED")
        self.assertEqual(result["economics"]["status"], "RECONCILED", report["checks"])
        binance = report["venues"]["BINANCE"]
        self.assertNotEqual(Decimal(binance["liquidation"]), 0)
        self.assertEqual(Decimal(binance["out_of_plan_fees"]), -Decimal("7.24925"))   # 2.9 + 4.34925 fees
        self.assertLess(Decimal(binance["strategy_equity_pnl"]), Decimal("-20"))
        self.assertTrue(all(check["passed"] for check in report["checks"]), report["checks"])

    def test_liquidation_fill_survives_a_restart_with_its_type(self):
        from crypto_stat_arb.config import load_config
        from crypto_stat_arb.session import TradingSession

        with tempfile.TemporaryDirectory() as directory:
            config, _ = write_session(Path(directory), self.rows())
            cfg, _ = load_config(config)
            store = ParentPlanStore(Path(directory) / "s.sqlite")
            first = TradingSession(cfg, plan_store=store)
            for event in self.rows()[:4]:
                first.on_event(event)
            before = first.ledger.economics.balances()
            store.close()
            store = ParentPlanStore(Path(directory) / "s.sqlite")
            second = TradingSession(cfg, plan_store=store)
            kinds = [e.event_type for e in second.ledger.economics.events]
            self.assertIn(EconomicEventType.LIQUIDATION, kinds)
            self.assertNotIn(EconomicEventType.BORROW_FORCED_CLOSE, kinds)
            self.assertEqual(second.ledger.economics.balances(), before)
            self.assertEqual(second.ledger.positions[("BINANCE", PERP)], first.ledger.positions[("BINANCE", PERP)])
            store.close()


if __name__ == "__main__":
    unittest.main()
