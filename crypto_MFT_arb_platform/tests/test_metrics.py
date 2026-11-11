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


class DistributionStatsAndPairEconomics(unittest.TestCase):
    """The 09 Nov markout note, sections 2 and 7: the statistics every result carries, and the linked pair economics per maker fill."""

    def test_quantile_is_linear_interpolation(self):
        v = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertEqual([metrics.quantile(v, q) for q in (0.0, 0.5, 1.0)], [1.0, 3.0, 5.0])
        self.assertAlmostEqual(metrics.quantile(v, 0.05), 1.2)
        self.assertEqual(metrics.quantile([7.0], 0.95), 7.0)

    def test_distribution_stats_says_why_something_is_missing(self):
        self.assertEqual(metrics.distribution_stats([])["status"], "unavailable")
        one = metrics.distribution_stats([2.0], [0], min_n=5)
        self.assertEqual((one["status"], one["n"], one["ci_status"], one["ci95"]), ("insufficient_support", 1, "insufficient_blocks", None))
        ok = metrics.distribution_stats([1.0, 2.0, 3.0, 4.0], [0, 0, 1, 1], reps=100)
        self.assertEqual((ok["status"], ok["median"], ok["blocks"], ok["ci_status"]), ("ok", 2.5, 2, "ok"))
        self.assertIsNone(metrics.distribution_stats([1.0, 2.0])["ci95"])               # no block ids: no interval

    def test_the_block_interval_is_deterministic_and_brackets_a_known_mean(self):
        vals = [float(i % 7) for i in range(210)]
        blocks = [i // 30 for i in range(210)]
        a = metrics.block_bootstrap_ci(vals, blocks, reps=400, seed=5)
        self.assertEqual(a, metrics.block_bootstrap_ci(vals, blocks, reps=400, seed=5))
        self.assertLess(a[0], sum(vals) / len(vals))
        self.assertGreater(a[1], sum(vals) / len(vals))
        self.assertIsNone(metrics.block_bootstrap_ci(vals, [0] * 210))

    @staticmethod
    def _journal(maker_side="buy", hedge=True, qty=1.0, hedge_created=100.0):
        hs = "sell" if maker_side == "buy" else "buy"
        j = [{"event": "intent", "intent_id": "sig1-P1", "role": "primary", "reason": "maker leg", "signal_id": 1, "venues": ["BYBIT"], "qty": qty, "created_ts": 99.0, "parent_id": ""},
             {"event": "child", "intent_id": "sig1-P1", "order_id": "m1", "ts": 99.0}]
        if hedge:
            j += [{"event": "intent", "intent_id": "sig1-H1", "role": "hedge", "reason": "hedge after maker fill", "signal_id": 1, "venues": ["BINANCE"], "qty": qty, "worst_price": 99.5,
                   "created_ts": hedge_created, "parent_id": "sig1-P1"},
                  {"event": "child", "intent_id": "sig1-H1", "order_id": "h1", "ts": hedge_created + 0.004}]
        return j, hs

    @staticmethod
    def _fill(oid, venue, side, qty, price, ts, fee):
        return {"order_id": oid, "venue": venue, "side": side.upper(), "fill_quantity": str(qty), "fill_price": str(price), "ts_ns": int(round(ts * 1e9)), "fee": str(fee)}

    def test_a_maker_buy_hedged_by_a_sell_gross_net_and_the_unhedged_interval(self):
        j, _ = self._journal("buy")
        fills = [self._fill("m1", "BYBIT", "buy", 1.0, 100.00, 100.0, -0.005), self._fill("h1", "BINANCE", "sell", 1.0, 100.05, 100.031, 0.05)]
        r = metrics.pair_economics(fills, j, reps=50)
        (row,) = r["rows"]
        self.assertTrue(row["fully_hedged"])
        self.assertAlmostEqual(row["pair_gross_bps"], 5.0)                                  # sold 5 bps above where it bought
        self.assertAlmostEqual(row["pair_net_bps"], 5.0 - (-0.005 + 0.05) / 100.0 * 1e4)   # a rebate on the maker leg, the taker fee on the hedge
        self.assertAlmostEqual(row["unhedged_interval_s"], 0.031, places=6)
        self.assertAlmostEqual(row["hedge_send_delay_s"], 0.004, places=6)
        self.assertEqual((row["hedge_children"], row["hedge_cap"]), (1, 99.5))
        self.assertEqual(r["metadata"]["coverage"], {"rows_total": 1, "rows_kept": 1, "share_kept": 1.0})
        self.assertEqual(r["summary"]["pair_net_bps"]["n"], 1)

    def test_a_maker_sell_is_the_mirror_image(self):
        j, _ = self._journal("sell")
        fills = [self._fill("m1", "BYBIT", "sell", 1.0, 100.00, 100.0, 0.0), self._fill("h1", "BINANCE", "buy", 1.0, 99.97, 100.02, 0.0)]
        (row,) = metrics.pair_economics(fills, j)["rows"]
        self.assertAlmostEqual(row["pair_gross_bps"], 3.0)                                   # bought 3 bps below where it sold

    def test_an_unhedged_or_partly_hedged_fill_is_counted_not_hidden(self):
        j, _ = self._journal("buy", hedge=False)
        r = metrics.pair_economics([self._fill("m1", "BYBIT", "buy", 1.0, 100.0, 100.0, 0.0)], j)
        self.assertEqual((r["rows"][0]["hedged_qty"], r["rows"][0]["pair_net_bps"]), (0.0, None))
        self.assertEqual(r["metadata"]["excluded_by_reason"]["no_hedge_intent"], 1)
        self.assertEqual(r["summary"]["pair_net_bps"]["status"], "unavailable")
        j, _ = self._journal("buy")
        part = metrics.pair_economics([self._fill("m1", "BYBIT", "buy", 1.0, 100.0, 100.0, 0.0), self._fill("h1", "BINANCE", "sell", 0.4, 100.02, 100.02, 0.0)], j)
        row = part["rows"][0]
        self.assertEqual((row["fully_hedged"], row["hedged_qty"], row["pair_net_bps"], row["unhedged_interval_s"]), (False, 0.4, None, None))
        self.assertAlmostEqual(row["pair_gross_bps"], 2.0)
        self.assertEqual(part["metadata"]["excluded_by_reason"]["partly_hedged"], 1)

    def test_two_maker_fills_of_one_order_pair_with_their_own_hedges(self):
        j = [{"event": "intent", "intent_id": "sig1-P1", "role": "primary", "reason": "maker leg", "signal_id": 1, "venues": ["BYBIT"], "qty": 1.0, "created_ts": 1.0, "parent_id": ""},
             {"event": "child", "intent_id": "sig1-P1", "order_id": "m1", "ts": 1.0}]
        fills = []
        for k, (mt, q, mp, hp) in enumerate(((10.0, 0.4, 100.0, 100.01), (20.0, 0.6, 100.1, 100.12)), start=1):
            j += [{"event": "intent", "intent_id": f"sig1-H{k}", "role": "hedge", "reason": "hedge after maker fill", "signal_id": 1, "venues": ["BINANCE"], "qty": q,
                   "worst_price": 99.0, "created_ts": mt, "parent_id": "sig1-P1"}, {"event": "child", "intent_id": f"sig1-H{k}", "order_id": f"h{k}", "ts": mt + 0.01}]
            fills += [self._fill("m1", "BYBIT", "buy", q, mp, mt, 0.0), self._fill(f"h{k}", "BINANCE", "sell", q, hp, mt + 0.05, 0.0)]
        rows = metrics.pair_economics(fills, j)["rows"]
        self.assertEqual([(r["qty"], r["hedge_intent_id"], r["fully_hedged"]) for r in rows], [(0.4, "sig1-H1", True), (0.6, "sig1-H2", True)])
        self.assertAlmostEqual(rows[1]["pair_gross_bps"], (100.12 - 100.1) / 100.1 * 1e4)

    def test_the_fee_of_a_partial_fill_is_the_fee_delta_not_the_cumulative_fee(self):
        j = [{"event": "intent", "intent_id": "sig1-P1", "role": "primary", "reason": "maker leg", "signal_id": 1, "venues": ["BYBIT"], "qty": 1.0, "created_ts": 1.0, "parent_id": ""},
             {"event": "child", "intent_id": "sig1-P1", "order_id": "m1", "ts": 1.0}]
        fills = []
        for k, (mt, q, cum) in enumerate(((10.0, 0.6, 0.6), (20.0, 0.4, 1.0)), start=1):
            j += [{"event": "intent", "intent_id": f"sig1-H{k}", "role": "hedge", "reason": "hedge after maker fill", "signal_id": 1, "venues": ["BINANCE"], "qty": q,
                   "worst_price": 99.0, "created_ts": mt, "parent_id": "sig1-P1"}, {"event": "child", "intent_id": f"sig1-H{k}", "order_id": f"h{k}", "ts": mt + 0.01}]
            fills += [{**self._fill("m1", "BYBIT", "buy", q, 100.0, mt, cum * 0.01), "fee_delta": str(q * 0.01)}, self._fill(f"h{k}", "BINANCE", "sell", q, 100.0, mt + 0.05, 0.0)]
        rows = metrics.pair_economics(fills, j)["rows"]
        self.assertAlmostEqual(rows[0]["pair_net_bps"], -0.01 / 100.0 * 1e4)       # 0.006 / (100 * 0.6) of fee, per unit: 1 bp
        self.assertAlmostEqual(rows[1]["pair_net_bps"], rows[0]["pair_net_bps"])    # the same fee rate: the second row must not carry the first fill's fee

    def test_the_dictionary_defines_the_pair_metrics(self):
        for name in ("pair_gross_bps", "pair_net_bps", "unhedged_interval_s"):
            self.assertIn(name, metrics.DICTIONARY)


if __name__ == "__main__":
    unittest.main()
