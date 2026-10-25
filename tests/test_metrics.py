"""GAP02: one metric dictionary; every report computes its metrics through it."""

from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb import metrics
from crypto_stat_arb.models import ChildOrder, Opportunity, OrderState, ParentPlan, Side
from crypto_stat_arb.runner import run_backtest

ROOT = Path(__file__).parents[1]
D = Decimal


def plan(*, buy_avg="100.10", sell_avg="100.90", buy_filled="1", sell_filled="1", fees="0.2",
         net_pnl=None, inherited="0"):
    opportunity = Opportunity("op", "s", "BTC-USDT-SPOT", 1, "BINANCE", "BYBIT", D("100"), D("101"), D("1"),
                              D("100"), D("80"), ("e1", "e2"))
    children = [
        ChildOrder("b", "p", "op", "BINANCE", "BTC-USDT-SPOT", Side.BUY, D("1"), D("100"),
                   OrderState.FILLED, D(buy_filled), D(buy_avg)),
        ChildOrder("s", "p", "op", "BYBIT", "BTC-USDT-SPOT", Side.SELL, D("1"), D("101"),
                   OrderState.FILLED, D(sell_filled), D(sell_avg)),
        ChildOrder("u", "p", "op", "BYBIT", "BTC-USDT-SPOT", Side.SELL, D("5"), D("90"),
                   OrderState.FILLED, D("5"), D("90"), order_role="UNWIND"),
    ]
    gross = D(sell_avg) * D(sell_filled) - D(buy_avg) * D(buy_filled)
    return ParentPlan("p", opportunity, children, gross_pnl=gross, fees=D(fees),
                      net_pnl=D(net_pnl) if net_pnl is not None else gross - D(fees), inherited_base=D(inherited))


class SignConventionTests(unittest.TestCase):
    def test_slippage_is_positive_when_adverse(self):
        self.assertEqual(metrics.slippage_per_unit(Side.BUY, D("100"), D("100.5")), D("0.5"))   # paid more
        self.assertEqual(metrics.slippage_per_unit(Side.SELL, D("100"), D("99.5")), D("0.5"))   # received less
        self.assertEqual(metrics.slippage_per_unit(Side.BUY, D("100"), D("99.5")), D("-0.5"))  # improvement
        self.assertEqual(metrics.weighted_slippage_bps(D("0.5"), D("1000")), D("5.0000"))
        self.assertEqual(metrics.weighted_slippage_bps(D("1"), D("0")), D("0"))

    def test_markout_is_positive_when_favourable(self):
        self.assertAlmostEqual(metrics.markout_bps("BUY", 100.0, 100.1), 10.0)     # rose after a buy
        self.assertAlmostEqual(metrics.markout_bps("SELL", 100.0, 100.1), -10.0)   # rose after a sell
        self.assertAlmostEqual(metrics.markout_bps("SELL", 100.0, 99.9), 10.0)

    def test_every_metric_states_unit_sign_and_where_it_is_reported(self):
        for name, metric in metrics.DICTIONARY.items():
            self.assertEqual(name, metric.name)
            for field in ("unit", "definition", "numerator", "denominator", "sign"):
                self.assertTrue(getattr(metric, field), f"{name}.{field}")
            self.assertTrue(metric.reports, name)

    def test_the_three_net_pnls_have_distinct_names(self):
        for name in ("plan_net_pnl", "strategy_equity_pnl", "maker_mark_to_market_pnl"):
            self.assertIn(name, metrics.DICTIONARY)
        self.assertNotIn("net_pnl", metrics.DICTIONARY)          # ambiguous on its own


class ImplementationShortfallTests(unittest.TestCase):
    def test_decomposition_by_hand(self):
        # Decision: buy 1 @ 100, sell 1 @ 101 -> paper P&L 1.0. Fills 100.10 / 100.90:
        # execution cost 0.10 + 0.10 = 0.20; fees 0.2; realized 0.8 - 0.2 = 0.6; shortfall 0.4.
        row = metrics.implementation_shortfall(plan())
        self.assertEqual(row["paper_pnl"], D("1"))
        self.assertEqual(row["execution_cost"], D("0.20"))
        self.assertEqual(row["implementation_shortfall"], D("0.40"))
        self.assertEqual(row["unfilled_unwind_and_residual_cost"], D("0"))
        self.assertEqual(row["decision_notional"], D("100"))

    def test_unfilled_and_unwind_cost_is_the_remainder(self):
        # Sell leg unfilled; the unwind lost 0.5 more than the plan would have at decision prices.
        row = metrics.implementation_shortfall(plan(sell_filled="0", net_pnl="-0.8"))
        self.assertEqual(row["execution_cost"], D("0.10"))        # only the filled buy
        self.assertEqual(row["implementation_shortfall"], D("1.8"))
        self.assertEqual(row["unfilled_unwind_and_residual_cost"], D("1.5"))   # 1.8 - 0.10 - 0.2

    def test_risk_flatten_plans_are_excluded(self):
        self.assertIsNone(metrics.implementation_shortfall(plan(inherited="0.01")))
        report = metrics.metrics_report(plans=[plan(inherited="0.01")], ledger_events=[], start_ns=0,
                                        end_ns=1, initial_capital=D("1"))
        self.assertEqual(report["implementation_shortfall"]["plans_excluded_risk_flatten"], 1)
        self.assertIsNone(report["implementation_shortfall"]["implementation_shortfall_bps"])


class FillRateTests(unittest.TestCase):
    def test_quantity_and_order_fill_rate_count_primary_children_only(self):
        rates = metrics.fill_rates([plan(sell_filled="0.25")])
        self.assertEqual(rates["ordered_quantity"], D("2"))            # the 5-unit unwind is excluded
        self.assertEqual(rates["quantity_fill_rate"], D("0.625"))
        self.assertEqual(rates["order_fill_rate"], D("1"))
        self.assertEqual(metrics.fill_rates([plan(sell_filled="0")])["order_fill_rate"], D("0.5"))
        self.assertIsNone(metrics.fill_rates([])["quantity_fill_rate"])


class TimeWeightedTests(unittest.TestCase):
    def fill(self, ts, venue, side, quantity):
        return {"event_type": "FILL", "ts_ns": ts, "venue": venue, "instrument_id": "BTC-USDT-SPOT",
                "side": side, "fill_quantity": quantity}

    def test_exposure_by_hand(self):
        # Buy 1 on BINANCE at t=2, hedge with a sell on BYBIT at t=6, run [0, 10]:
        # gross |pos| = 1 on [2,6), 2 on [6,10] -> (4 + 8) / 10 = 1.2; net = 1 on [2,6) -> 0.4.
        gross, net = metrics.exposure_steps([self.fill(6, "BYBIT", "SELL", "1"),
                                             self.fill(2, "BINANCE", "BUY", "1")])
        self.assertEqual(metrics.time_weighted_average(gross, 0, 10), D("1.2"))
        self.assertEqual(metrics.time_weighted_average(net, 0, 10), D("0.4"))
        self.assertIsNone(metrics.time_weighted_average(gross, 5, 5))

    def test_return_is_chain_linked(self):
        # Day 1: +10 on 100 (10%); day 2: -11 on 110 (-10%) -> 1.1 x 0.9 - 1 = -0.01.
        self.assertEqual(metrics.time_weighted_return({"2026-10-02": D("-11"), "2026-10-01": D("10")}, D("100")),
                         D("-0.01"))
        self.assertIsNone(metrics.time_weighted_return({"d": D("1")}, D("0")))


class ReportsUseTheDictionaryTests(unittest.TestCase):
    def test_backtest_reports_agree_with_the_metric_functions(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            summary = run_backtest(ROOT / "config/research.json", ROOT / "data/samples/two_venue_books.jsonl", out)
            report = json.loads((out / "metrics_report.json").read_text())
            tca = json.loads((out / "tca_report.json").read_text())["summary"]
            monitoring = json.loads((out / "pipeline_monitoring.json").read_text())["strategy_execution"]["pnl"]
            self.assertNotIn("metrics_report", summary)                  # summary hash unaffected
        self.assertGreater(report["implementation_shortfall"]["plans_measured"], 0)
        for net in (summary["net_pnl"], tca["net_pnl"], monitoring["net"]):
            self.assertEqual(net, report["pnl"]["plan_net_pnl"])
        self.assertEqual(summary["gross_pnl"], report["pnl"]["plan_gross_pnl"])
        self.assertEqual(tca["weighted_slippage_bps"], str(metrics.weighted_slippage_bps(
            D(tca["slippage_quote"]), D(tca["benchmark_notional"]))))
        shortfall = report["implementation_shortfall"]
        self.assertEqual(D(shortfall["implementation_shortfall"]),
                         D(shortfall["execution_cost"]) + D(shortfall["fees"])
                         + D(shortfall["unfilled_unwind_and_residual_cost"]))
        self.assertEqual(set(report["definitions"]), set(metrics.DICTIONARY))


if __name__ == "__main__":
    unittest.main()
