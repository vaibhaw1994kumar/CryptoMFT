"""ECO05 transfers: delayed lifecycle, failure, fees, horizon, restart and rebalancing."""

from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.config import load_config
from crypto_stat_arb.economics import EconomicEventType, Valuation, opening_balance_events, reconcile
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import MarketEvent
from crypto_stat_arb.plan_store import ParentPlanStore
from crypto_stat_arb.risk import InventoryModel, InventoryPolicy, RiskEngine, RiskLimits
from crypto_stat_arb.runner import run_backtest
from crypto_stat_arb.session import TradingSession
from crypto_stat_arb.transfers import TransferEngine, TransferRoute


ROOT = Path(__file__).parents[1]
MINUTE = 60 * 10**9
T0 = 10 * 60 * MINUTE


def route(**overrides) -> TransferRoute:
    row = {"from": "BINANCE", "to": "BYBIT", "asset": "BTC", "delay_ms": 600_000, "fee": "0.0001"}
    row.update(overrides)
    return TransferRoute.from_config(row)


def setup(*routes, btc=("0.05", "0")):
    ledger = Ledger()
    rows = [("BINANCE", "BTC", Decimal(btc[0])), ("BYBIT", "BTC", Decimal(btc[1])),
            ("BINANCE", "USDT", Decimal("1000")), ("BYBIT", "USDT", Decimal("1000"))]
    for event in opening_balance_events(rows):
        ledger.economics.apply(event)
    risk = RiskEngine(RiskLimits(max_order_notional=Decimal("5000"), max_gross_notional=Decimal("100000"),
                                 max_residual_base=Decimal("0.02"), max_book_age_ns=10**9),
                      inventory=InventoryPolicy(model=InventoryModel.PRE_FUNDED, instrument_id="BTC-USDT-SPOT"))
    risk.ledger = ledger
    engine = TransferEngine(ledger, routes or [route()],
                            free_balance=lambda v, a: risk.balance_view(v, a)["free"],
                            price=lambda v, a: Decimal("60000"))
    risk.unavailable_provider = engine.in_transit_out
    return ledger, risk, engine


def btc(ledger, venue, account="main"):
    return ledger.economics.balance(venue, "BTC", account)


class LifecycleTests(unittest.TestCase):
    def test_delayed_transfer_is_unavailable_at_both_ends_until_confirmed(self):
        ledger, risk, engine = setup()
        engine.initiate("BINANCE", "BYBIT", "BTC", Decimal("0.01"), T0)
        self.assertEqual((btc(ledger, "BINANCE"), btc(ledger, "BINANCE", "in_transit"), btc(ledger, "BYBIT")),
                         (Decimal("0.0399"), Decimal("0.01"), Decimal("0")))
        source = risk.balance_view("BINANCE", "BTC")
        self.assertEqual((source["total"], source["unavailable"], source["free"]),
                         (Decimal("0.0499"), Decimal("0.01"), Decimal("0.0399")))
        self.assertEqual(risk.balance_view("BYBIT", "BTC")["free"], 0)    # not usable at the destination yet
        engine.process(T0 + 10 * MINUTE - 1)
        self.assertEqual(len(engine.pending), 1)
        engine.process(T0 + 10 * MINUTE)
        self.assertEqual((btc(ledger, "BYBIT"), btc(ledger, "BINANCE", "in_transit")), (Decimal("0.01"), 0))
        self.assertEqual(engine.completed[0]["outcome"], "CONFIRMED")

    def test_failed_transfer_returns_the_principal_and_keeps_the_fee(self):
        for refunded, expected in ((False, Decimal("0.0499")), (True, Decimal("0.05"))):
            with self.subTest(refunded=refunded):
                ledger, risk, engine = setup(route(outcomes=["FAILED"], fee_refunded_on_failure=refunded))
                engine.initiate("BINANCE", "BYBIT", "BTC", Decimal("0.01"), T0)
                engine.process(T0 + 10 * MINUTE)
                self.assertEqual((btc(ledger, "BINANCE"), btc(ledger, "BYBIT")), (expected, 0))
                self.assertEqual(engine.flags[0]["code"], "TRANSFER_FAILED")

    def test_insufficient_free_balance_including_the_fee_is_refused(self):
        ledger, risk, engine = setup(btc=("0.01", "0"))
        with self.assertRaisesRegex(ValueError, "insufficient free BTC on BINANCE"):
            engine.initiate("BINANCE", "BYBIT", "BTC", Decimal("0.01"), T0)

    def test_instantaneous_transfers_are_rejected_at_load(self):
        with self.assertRaisesRegex(ValueError, "never instantaneous"):
            route(delay_ms=0)

    def test_nothing_is_confirmed_past_the_data(self):
        ledger, risk, engine = setup()
        engine.initiate("BINANCE", "BYBIT", "BTC", Decimal("0.01"), T0)
        engine.process(T0 + 5 * MINUTE)                 # last market event
        report = engine.report()
        self.assertEqual(len(report["pending"]), 1)
        self.assertEqual(btc(ledger, "BYBIT"), 0)

    def test_transfers_reconcile_as_internal_flows_and_the_fee_as_a_cost(self):
        ledger, risk, engine = setup()
        engine.initiate("BINANCE", "BYBIT", "BTC", Decimal("0.01"), T0)
        engine.process(T0 + 10 * MINUTE)
        prices = Valuation("USDT", {(v, "BTC"): Decimal("60000") for v in ("BINANCE", "BYBIT")})
        state = ledger.recalculate_account_state()
        report = reconcile(ledger.economics, opening=prices, closing=prices, transfer_mode="MODELLED",
                           legacy_transfer_fees_by_venue={v: Decimal(r["transfer_fees"]) for v, r in state["venues"].items()},
                           plan_net_pnl=Decimal("0"))
        self.assertEqual(report["status"], "RECONCILED", report["checks"])
        binance, bybit = report["venues"]["BINANCE"], report["venues"]["BYBIT"]
        self.assertEqual((Decimal(binance["transfers"]), Decimal(bybit["transfers"])), (Decimal("-600"), Decimal("600")))
        self.assertEqual(Decimal(binance["strategy_equity_pnl"]), Decimal("-6"))   # only the 0.0001 BTC fee
        self.assertEqual(Decimal(bybit["strategy_equity_pnl"]), 0)
        self.assertNotIn("TRANSFER", binance["not_modelled"])
        self.assertEqual(Decimal(state["venues"]["BINANCE"]["transfer_fees"]), Decimal("6"))

    def test_no_rebalancing_mode_is_reported_as_such(self):
        ledger, _, _ = setup()
        report = reconcile(ledger.economics, opening=Valuation("USDT"), closing=Valuation("USDT"),
                           transfer_mode="NO_REBALANCING")
        self.assertEqual(report["venues"]["BINANCE"]["transfer_mode"], "NO_REBALANCING")
        self.assertNotIn("TRANSFER", report["venues"]["BINANCE"]["not_modelled"])


def snapshot(venue, ts, sequence, bid, ask):
    native = {"lastUpdateId": sequence} if venue == "BINANCE" else {"u": sequence}
    return {"event_type": "BOOK_SNAPSHOT", "venue": venue, "instrument_id": "BTC-USDT-SPOT",
            "exchange_ts_ns": ts - 100_000, "receive_ts_ns": ts, "sequence": sequence,
            "bids": [[bid, "1.000"]], "asks": [[ask, "1.000"]], "native": native}


def edge_rows(periods=10):
    """Every 10 minutes the same edge: buy BINANCE at 60000, sell BYBIT at 60060."""
    rows = []
    for n in range(periods):
        ts = T0 + n * 10 * MINUTE
        rows.append(snapshot("BINANCE", ts, 100 + n, "59999.90", "60000.00"))
        rows.append(snapshot("BYBIT", ts + 10**6, 900 + n, "60060.00", "60060.10"))
    return rows


def write_config(directory: Path, *, rebalance=True, delay_ms=900_000) -> Path:
    raw = json.loads((ROOT / "config/research.json").read_text())
    raw["risk"]["inventory"]["initial_base_by_venue"] = {"BINANCE": "0.05", "BYBIT": "0.02"}
    raw["risk"]["transfers"] = {
        "routes": [{"from": "BINANCE", "to": "BYBIT", "asset": "BTC", "delay_ms": delay_ms, "fee": "0.0002"}],
        "rebalance": {"enabled": rebalance, "cooldown_ms": 60_000,
                      "assets": {"BTC": {"low_watermark": "0.01", "target": "0.03"}}},
    }
    path = directory / "transfers.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


class RebalancingSessionTests(unittest.TestCase):
    def run_session(self, *, rebalance=True):
        with tempfile.TemporaryDirectory() as directory:
            config = write_config(Path(directory), rebalance=rebalance)
            events = Path(directory) / "events.jsonl"
            events.write_text("".join(json.dumps(row) + "\n" for row in edge_rows()), encoding="utf-8")
            out = Path(directory) / "out"
            result = run_backtest(config, events, out)
            files = {name: json.loads((out / name).read_text()) for name in (
                "transfers_report.json", "economic_reconciliation.json", "account_state.json")}
            files["ledger"] = [json.loads(line) for line in (out / "ledger.jsonl").open()]
        return result, files

    def test_one_directional_strategy_is_refilled_by_a_delayed_transfer(self):
        result, files = self.run_session()
        transfers, report = files["transfers_report.json"], files["economic_reconciliation.json"]
        self.assertEqual(transfers["rebalancing"], "ENABLED")
        # Sustained one-way flow: BYBIT runs out, is refilled, runs out again.
        self.assertEqual([t["outcome"] for t in transfers["completed"]], ["CONFIRMED", "CONFIRMED"])
        sells = [int(e["ts_ns"]) for e in files["ledger"] if e["event_type"] == "FILL" and e["venue"] == "BYBIT"]
        refusals = [int(e["ts_ns"]) for e in files["ledger"] if e["event_type"] == "PACKAGE_RISK_REJECTED"]
        windows = [(t["initiated_ns"], t["completed_ns"]) for t in transfers["completed"]]
        self.assertEqual(len(refusals), 4)
        # Every refusal happened while a refill was in transit, never after it landed...
        self.assertTrue(all(any(start <= ts < end for start, end in windows) for ts in refusals))
        # ...and BYBIT sold again after each confirmation.
        self.assertTrue(all(any(ts > end for ts in sells) for _, end in windows))
        self.assertEqual(windows[0][1] - windows[0][0], 15 * MINUTE)          # the route's delay
        self.assertEqual(result["economics"]["status"], "RECONCILED", report["checks"])
        self.assertTrue(all(check["passed"] for check in report["checks"]), report["checks"])
        self.assertEqual(Decimal(report["venues"]["BINANCE"]["transfer_fees_at_initiation"]),
                         2 * Decimal("0.0002") * Decimal("59999.95"))           # two transfers
        # 0.06 BTC moved to where it is marked 60.1 higher: realised at the destination.
        self.assertEqual(Decimal(report["venues"]["BYBIT"]["transfer_valuation"]),
                         Decimal("0.060") * (Decimal("60060.05") - Decimal("59999.95")))
        self.assertEqual(Decimal(report["venues"]["BINANCE"]["transfer_valuation"]), 0)
        self.assertEqual(result["inventory"]["label"], "PRE_FUNDED_INVENTORY_WITH_REBALANCING")

    def test_without_rebalancing_the_venue_stays_exhausted(self):
        result, files = self.run_session(rebalance=False)
        self.assertEqual(files["transfers_report.json"]["completed"], [])
        self.assertIn("BASE_EXHAUSTED", result["inventory"]["exhausted"]["BYBIT"])

    def test_a_pending_transfer_survives_a_restart(self):
        rows = [MarketEvent.from_dict(row) for row in edge_rows()]
        with tempfile.TemporaryDirectory() as directory:
            cfg, _ = load_config(write_config(Path(directory)))
            store = ParentPlanStore(Path(directory) / "t.sqlite")
            first = TradingSession(cfg, plan_store=store)
            for event in rows[:8]:
                first.on_event(event)
            self.assertEqual(len(first.transfers.pending), 1)        # initiated, not yet confirmed
            pending_id = next(iter(first.transfers.pending))
            store.close()
            store = ParentPlanStore(Path(directory) / "t.sqlite")
            second = TradingSession(cfg, plan_store=store)
            self.assertEqual(list(second.transfers.pending), [pending_id])
            self.assertEqual(second.ledger.economics.balance("BINANCE", "BTC", "in_transit"), Decimal("0.03"))
            for event in rows[8:]:
                second.on_event(event)
            self.assertEqual(second.transfers.pending, {})
            kinds = [e.event_type for e in second.ledger.economics.events if e.reference_id == pending_id]
            self.assertEqual(kinds.count(EconomicEventType.TRANSFER), 2)   # initiated once, confirmed once
            store.close()


if __name__ == "__main__":
    unittest.main()
