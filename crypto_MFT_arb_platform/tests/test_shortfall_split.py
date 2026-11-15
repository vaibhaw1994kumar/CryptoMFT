"""Owner 14 Nov (execution cost model, step A): the Perold split of an arb fill's shortfall. Hand-calculated example; SYNTHETIC numbers, tests only."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb.metrics import opportunity_cost, shortfall_split

ASSUMED_FEES = 2.7      # maker 1.0 + taker 1.7 bps


def row(side="buy", price=100.0, avg=99.97, gross=None, net=None, **kw):
    sgn = 1.0 if side == "buy" else -1.0
    gross = sgn * (avg - price) / price * 1e4 if gross is None else gross
    r = {"maker_intent_id": "sig1-P1", "maker_side": side, "maker_price": price, "maker_fill_ts": 1000.0, "first_hedge_fill_delay_s": 0.04, "hedge_venue": "BINANCE", "hedge_avg_price": avg, "qty": 1.0,
         "fully_hedged": True, "pair_gross_bps": gross, "pair_net_bps": gross - 2.7 if net is None else net}
    r.update(kw)
    return r


def touches(table):
    return lambda venue, ts, side: table[(round(ts, 3), side)]


class Split(unittest.TestCase):
    def test_buy_maker_hand_calculated(self):
        # quote 99.99 (signal), hedge believed at 100.05; at the maker fill (t=1000) the hedge bid is 100.03, at hedge arrival (t=1000.04) 100.01; the hedge sold at 99.99. Maker filled at 100.00.
        sig = {"buy_price": 99.99, "sell_price": 100.05, "net_edge_bps": 1.5, "opportunity_id": "o1", "qty": 1.0}
        r = row(side="buy", price=100.0, avg=99.99)
        res = shortfall_split([r], {"sig1-P1": sig}, touches({(1000.0, "bid"): 100.03, (1000.04, "bid"): 100.01}), ASSUMED_FEES)
        (x,) = res["rows"]
        k = 1e4 / 100.0
        self.assertAlmostEqual(x["delay_drift_bps"], (100.05 - 100.03) * k)                 # 2.0 bps
        self.assertAlmostEqual(x["hedge_latency_drift_bps"], (100.03 - 100.01) * k)         # 2.0 bps
        self.assertAlmostEqual(x["hedge_impact_bps"], (100.01 - 99.99) * k)                 # 2.0 bps
        self.assertAlmostEqual(x["fee_diff_bps"], 0.0)                                      # actual fees = gross - net = 2.7 = assumed
        gross_sig = (100.05 - 99.99) / 99.99 * 1e4
        self.assertAlmostEqual(x["quote_price_term_bps"], gross_sig - (100.05 - 100.0) * k)
        self.assertAlmostEqual(x["believed_allowances_bps"], gross_sig - 1.5 - ASSUMED_FEES)
        self.assertAlmostEqual(x["shortfall_bps"], 1.5 - x["realised_net_bps"])
        self.assertLess(abs(x["residual_bps"]), 1e-9)

    def test_the_parts_sum_to_the_shortfall_for_both_sides_and_with_a_fee_difference(self):
        for side in ("buy", "ask" if False else "sell"):
            sgn = 1.0 if side == "buy" else -1.0
            sig = {"buy_price": 100.0 - 0.02 * sgn, "sell_price": 100.0 + 0.05 * sgn, "net_edge_bps": 1.2, "opportunity_id": "o", "qty": 1.0}
            if side == "sell":
                sig = {"buy_price": 99.95, "sell_price": 100.02, "net_edge_bps": 1.2, "opportunity_id": "o", "qty": 1.0}       # sell maker quotes at sell_price, hedge BUYS at buy_price
            r = row(side=side, price=100.0, avg=100.0 - 0.03 * sgn, net=None)
            r["pair_net_bps"] = r["pair_gross_bps"] - 3.4                                    # actual fees 3.4 bps against 2.7 assumed
            hedge_side = "bid" if side == "buy" else "ask"
            res = shortfall_split([r], {"sig1-P1": sig}, touches({(1000.0, hedge_side): 100.04 if side == "buy" else 99.96, (1000.04, hedge_side): 100.0 if side == "buy" else 100.0}), ASSUMED_FEES)
            (x,) = res["rows"]
            self.assertAlmostEqual(x["fee_diff_bps"], 0.7)
            parts = x["delay_drift_bps"] + x["hedge_latency_drift_bps"] + x["hedge_impact_bps"] + x["fee_diff_bps"] + x["quote_price_term_bps"] - x["believed_allowances_bps"]
            self.assertAlmostEqual(parts, x["shortfall_bps"], places=9)
            self.assertLess(res["max_abs_residual_bps"], 1e-9)

    def test_rows_that_cannot_be_split_are_counted_not_dropped_silently(self):
        sig = {"buy_price": 99.99, "sell_price": 100.05, "net_edge_bps": 1.5, "opportunity_id": "o1", "qty": 1.0}
        t = touches({(1000.0, "bid"): 100.03, (1000.04, "bid"): 100.01})
        rows = [row(fully_hedged=False), row(maker_intent_id="sig9-P1"), row()]
        res = shortfall_split(rows, {"sig1-P1": sig}, t, ASSUMED_FEES)
        self.assertEqual((len(res["rows"]), res["excluded"]), (1, {"not_fully_hedged": 1, "no_signal": 1}))
        res = shortfall_split([row()], {"sig1-P1": sig}, lambda *a: None, ASSUMED_FEES)
        self.assertEqual(res["excluded"], {"touch_unknown": 1})


class Opportunity(unittest.TestCase):
    def test_unfilled_quotes_count_and_believed_edge(self):
        sigs = [{"opportunity_id": "a", "net_edge_bps": 1.0, "qty": 1.0, "buy_price": 100.0}, {"opportunity_id": "b", "net_edge_bps": 2.0, "qty": 2.0, "buy_price": 100.0},
                {"opportunity_id": "c", "net_edge_bps": 3.0, "qty": 1.0, "buy_price": 100.0}]
        o = opportunity_cost(sigs, {"a"})
        self.assertEqual((o["signals"], o["unfilled"]), (3, 2))
        self.assertAlmostEqual(o["believed_edge_quote_ccy_unfilled"], (2.0 * 2.0 * 100.0 + 3.0 * 1.0 * 100.0) / 1e4)
        self.assertAlmostEqual(o["mean_believed_edge_bps_unfilled"], 2.5)


if __name__ == "__main__":
    unittest.main()
