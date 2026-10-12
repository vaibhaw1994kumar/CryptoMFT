"""Pre-funded spot inventory: a sell needs base held on that venue.

Before, a spot sell was admitted against the position limit only, so a venue
holding 0 BTC could "sell" 1 BTC (an implicit short the venue would reject).
"""

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.config import load_config
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.oms import OMS
from crypto_stat_arb.risk import (
    AccountRiskSnapshot, InventoryModel, InventoryPolicy, RiskEngine, RiskLimits,
)
from test_execution_realism import books
from test_oms import instrument_master, opportunity


ROOT = Path(__file__).parents[1]
INSTRUMENT = "BTC-USDT-SPOT"


def limits() -> RiskLimits:
    return RiskLimits(
        max_order_notional=Decimal("5000"), max_gross_notional=Decimal("10000"),
        max_residual_base=Decimal("0.02"), max_book_age_ns=1_000_000_000,
    )


def engine(base: dict, quote: dict, accounts: dict | None = None) -> RiskEngine:
    return RiskEngine(limits(), accounts, inventory=InventoryPolicy(
        model=InventoryModel.PRE_FUNDED, instrument_id=INSTRUMENT,
        initial_base_by_venue={k: Decimal(v) for k, v in base.items()},
        initial_quote_by_venue={k: Decimal(v) for k, v in quote.items()},
    ))


def nth(n: int, qty: str = "0.01", reverse: bool = False):
    opp = replace(opportunity(qty), opportunity_id=f"op-{n}", source_event_ids=(f"a{n}", f"b{n}"))
    if reverse:
        opp = replace(opp, buy_venue="BYBIT", sell_venue="BINANCE",
                      buy_price=Decimal("60070.00"), sell_price=Decimal("59990.00"))
    return opp


class AdmissionTests(unittest.TestCase):
    def test_sell_without_base_on_the_sell_venue_is_rejected(self):
        risk = engine({"BINANCE": "1", "BYBIT": "0"}, {"BINANCE": "100000", "BYBIT": "100000"})
        with self.assertRaisesRegex(ValueError, "insufficient base inventory: BYBIT"):
            risk.reserve(nth(1))

    def test_buy_without_quote_on_the_buy_venue_is_rejected(self):
        risk = engine({"BINANCE": "1", "BYBIT": "1"}, {"BINANCE": "500", "BYBIT": "100000"})
        with self.assertRaisesRegex(ValueError, "insufficient quote inventory: BINANCE"):
            risk.reserve(nth(1))  # buys 0.01 x 60000 = 600 USDT on BINANCE

    def test_reserved_base_is_held_until_release(self):
        risk = engine({"BINANCE": "0", "BYBIT": "0.015"}, {"BINANCE": "100000", "BYBIT": "100000"})
        first = risk.reserve(nth(1))
        self.assertEqual(first.base_by_venue, {"BYBIT": Decimal("0.01")})
        with self.assertRaisesRegex(ValueError, "insufficient base inventory: BYBIT"):
            risk.reserve(nth(2))
        risk.release(first)
        self.assertEqual(risk.reserve(nth(2)).base_by_venue, {"BYBIT": Decimal("0.01")})
        venue = risk.inventory_snapshot()["venues"]["BYBIT"]
        self.assertEqual((venue["reserved_base"], venue["available_base"]), ("0.01", "0.005"))

    def test_margin_enabled_sell_venue_needs_no_base(self):
        accounts = {"BYBIT": AccountRiskSnapshot(venue="BYBIT", margin_enabled=True)}
        risk = engine({"BINANCE": "0", "BYBIT": "0"}, {"BINANCE": "100000", "BYBIT": "100000"}, accounts)
        self.assertEqual(risk.reserve(nth(1)).base_by_venue, {})

    def test_unconstrained_model_keeps_the_legacy_behaviour(self):
        risk = RiskEngine(limits())
        self.assertEqual(risk.reserve(nth(1)).base_by_venue, {})
        self.assertEqual(risk.inventory_snapshot()["label"], "UNCONSTRAINED_SPOT_SHORTING_ALLOWED")


class LedgerDrivenBalanceTests(unittest.TestCase):
    def test_fills_move_inventory_and_one_way_trading_runs_out_of_base(self):
        risk = engine({"BINANCE": "0", "BYBIT": "0.01"}, {"BINANCE": "5000", "BYBIT": "1000"})
        oms = OMS(instrument_master(), risk, Ledger())
        plan = oms.execute(nth(1), books())  # buy BINANCE, sell BYBIT, 0.01 each
        self.assertEqual(plan.residual_base, Decimal("0"))
        self.assertEqual(risk.held_base("BYBIT"), Decimal("0"))
        self.assertEqual(risk.held_base("BINANCE"), Decimal("0.01"))
        self.assertLess(risk.held_quote("BINANCE"), Decimal("5000") - Decimal("599"))
        # Same direction again: BYBIT has no BTC left, so it is refused...
        refused = oms.execute(nth(2), books())
        self.assertIn("REJECTED", [row["state"] for row in refused.transitions])
        self.assertTrue(all(child.filled_quantity == 0 for child in refused.children))
        self.assertTrue(any(
            row["event_type"] == "PACKAGE_RISK_REJECTED"
            and "insufficient base inventory: BYBIT" in row["reason"]
            for row in oms.ledger.events
        ))
        # ...while the reverse trade, selling the BTC bought on BINANCE, is allowed.
        back = oms.execute(nth(3, reverse=True), books())
        self.assertNotIn("REJECTED", [row["state"] for row in back.transitions])
        self.assertEqual(risk.held_base("BINANCE"), Decimal("0"))
        self.assertEqual(risk.held_base("BYBIT"), Decimal("0.01"))


class SharedInventoryTests(unittest.TestCase):
    def test_one_directional_arb_drains_a_venue_shared_with_mean_reversion(self):
        from crypto_stat_arb.benchmark import synthetic_book_events
        from crypto_stat_arb.models import MarketEvent
        from crypto_stat_arb.replay import deterministic_order
        from crypto_stat_arb.session import TradingSession

        cfg, _ = load_config(ROOT / "config" / "paper_watch.json")
        self.assertEqual(cfg.inventory_policy.model, InventoryModel.PRE_FUNDED)
        session = TradingSession(cfg)
        for event in deterministic_order(
            [MarketEvent.from_dict(row) for row in synthetic_book_events(6_000, seed=11)]
        ):
            session.on_event(event)
        session.drain()
        venues = session.risk.inventory_snapshot()["venues"]
        # Base only moves between the two venues; it never goes negative.
        self.assertGreaterEqual(Decimal(venues["BYBIT"]["held_base"]), 0)
        self.assertGreaterEqual(Decimal(venues["BINANCE"]["held_base"]), 0)
        self.assertEqual(
            Decimal(venues["BYBIT"]["held_base"]) + Decimal(venues["BINANCE"]["held_base"]),
            Decimal("0.10"),
        )
        refusals = [
            row["reason"] for row in session.ledger.events
            if row["event_type"] == "PACKAGE_RISK_REJECTED"
        ]
        self.assertTrue(refusals)
        self.assertTrue(all(reason == "insufficient base inventory: BYBIT" for reason in refusals))


class ConfigTests(unittest.TestCase):
    def write(self, inventory: dict | None) -> Path:
        raw = json.loads((ROOT / "config" / "research.json").read_text())
        raw["risk"].pop("inventory", None)
        if inventory is not None:
            raw["risk"]["inventory"] = inventory
        directory = Path(tempfile.mkdtemp())
        (directory / "c.json").write_text(json.dumps(raw))
        return directory / "c.json"

    def test_pre_funded_config_is_parsed_and_labelled(self):
        cfg, _ = load_config(self.write({
            "model": "pre_funded", "initial_base_by_venue": {"BINANCE": "0.05", "BYBIT": "0.05"},
        }))
        policy = cfg.inventory_policy
        self.assertEqual(policy.model, InventoryModel.PRE_FUNDED)
        self.assertEqual(policy.initial_base_by_venue["BYBIT"], Decimal("0.05"))
        self.assertEqual(policy.initial_quote_by_venue["BINANCE"], Decimal("10000"))
        self.assertEqual(policy.label, "PRE_FUNDED_INVENTORY_NO_REBALANCING")

    def test_pre_funded_needs_a_base_balance_for_every_venue(self):
        with self.assertRaisesRegex(ValueError, "initial_base_by_venue"):
            load_config(self.write({"model": "pre_funded", "initial_base_by_venue": {"BINANCE": "1"}}))

    def test_missing_section_is_unconstrained(self):
        cfg, _ = load_config(self.write(None))
        self.assertEqual(cfg.inventory_policy.model, InventoryModel.UNCONSTRAINED)


if __name__ == "__main__":
    unittest.main()
