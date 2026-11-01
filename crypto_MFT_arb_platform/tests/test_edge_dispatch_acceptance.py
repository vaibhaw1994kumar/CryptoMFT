"""Acceptance tests for the edge definition, dispatch mode and P&L accounting (docs/strategy_contract.md).

Every expected number below is plain arithmetic written in the test, never a call to the code under test. Books are the
Harness's hand-built ones: unless stated, bids 99/98 and asks 101/102 (size 5) on both venues; the maker bid rests at 100 for
2.0 with queue 0.5 ahead; the hedge sells on binance at its bid 99 (taker fee 5 bps); bybit maker fee is -0.5 bps (rebate).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from new_backtest import edge
from new_backtest.arb_strategy import MAKER_TAKER, TAKER_TAKER, ArbLeg, ArbSignal
from new_backtest.event_loader import TRADE

from test_maker_order_lifetime import BINANCE, BYBIT, Harness

V1 = "perp_perp_maker_taker_v1"


def books(h: Harness, t: float, bybit: dict, binance: dict) -> None:
    h.book(t, BYBIT, **bybit)
    h.book(t, BINANCE, **binance)


class EdgeFunctionTests(unittest.TestCase):
    def test_1_equal_books_no_signal_and_edge_is_minus_99_5_bps_before_fees(self):
        h = Harness(mode=TAKER_TAKER, profile=V1)
        same = dict(bids=((100.0, 5.0), (99.0, 5.0)), asks=((101.0, 5.0), (102.0, 5.0)))
        books(h, 1.0, same, same)
        er = edge.taker_taker_edge_bps(h.bt.books[BYBIT], h.bt.books[BINANCE], 2.0, 0.0, 0.0)
        expected = (100.0 - 101.0) / 100.5 * 10_000.0                                   # sell at 100, buy at 101, ref = buy mid 100.5
        self.assertEqual(er.status, "ok")
        self.assertAlmostEqual(er.gross_bps, expected, places=9)
        self.assertAlmostEqual(expected, -99.5024875621890, places=9)
        self.assertEqual((er.ref, er.ref_name), (100.5, "buy_mid"))
        self.assertIsNone(h.bt.strategy.evaluate(h.bt.books, h.bt.states, h.bt.inventory, 1.0))
        self.assertEqual(h.bt.strategy.rejects["wide_spread"], 2)                        # a 99.5 bps spread fails the sanity gate before any edge is computed
        tight = dict(bids=((100.0, 5.0), (99.0, 5.0)), asks=((100.2, 5.0), (102.0, 5.0)))   # the same inversion-free shape inside the spread gate
        h2 = Harness(mode=TAKER_TAKER, profile=V1)
        books(h2, 1.0, tight, tight)
        self.assertIsNone(h2.bt.strategy.evaluate(h2.bt.books, h2.bt.states, h2.bt.inventory, 1.0))
        self.assertEqual(h2.bt.strategy.rejects["no_edge"], 2)                           # both directions evaluated, both refused on edge

    def test_2_executable_inversion_signal_fires_with_the_hand_calculated_edge(self):
        h = Harness(mode=TAKER_TAKER, profile=V1)
        books(h, 1.0, dict(bids=((99.90, 5.0), (99.0, 5.0)), asks=((100.00, 5.0), (101.0, 5.0))),       # buy bybit at 100.00
              dict(bids=((100.20, 5.0), (99.0, 5.0)), asks=((100.40, 5.0), (101.0, 5.0))))              # sell binance at 100.20
        ref = (99.90 + 100.00) / 2                                                          # bybit mid
        gross = (100.20 - 100.00) / ref * 10_000.0
        fees = 5.5 + 5.0                                                                     # bybit taker + binance taker
        net = gross - fees - 0.5                                                             # safety buffer
        er = edge.taker_taker_edge_bps(h.bt.books[BYBIT], h.bt.books[BINANCE], 2.5, 5.5, 5.0)
        self.assertAlmostEqual(er.gross_bps, gross, places=9)
        self.assertAlmostEqual(er.net_bps, gross - fees, places=9)
        sig = h.bt.strategy.evaluate(h.bt.books, h.bt.states, h.bt.inventory, 1.0)
        self.assertIsNotNone(sig)
        self.assertEqual((sig.buy_venue, sig.sell_venue, sig.mode), (BYBIT, BINANCE, TAKER_TAKER))
        self.assertAlmostEqual(sig.gross_edge_bps, gross, places=9)
        self.assertAlmostEqual(sig.net_edge_bps, net, places=9)
        self.assertAlmostEqual(sig.conditional_edge_bps, net, places=9)
        self.assertAlmostEqual(sig.naive_edge_bps, (100.20 - 100.00) / 100.00 * 10_000.0, places=9)   # the old top-of-book / buy-ask figure
        self.assertEqual(sig.edge_ref, "buy_mid")
        self.assertAlmostEqual(gross, 20.0100050025, places=9)

    def test_3_depth_limited_vwap_and_insufficient_depth(self):
        h = Harness(mode=TAKER_TAKER, profile=V1)
        books(h, 1.0, dict(bids=((99.90, 5.0),), asks=((100.00, 5.0), (100.10, 5.0))),
              dict(bids=((100.20, 5.0), (100.10, 5.0)), asks=((100.40, 5.0),)))
        er = edge.taker_taker_edge_bps(h.bt.books[BYBIT], h.bt.books[BINANCE], 7.0, 0.0, 0.0)
        buy_vwap = (5 * 100.00 + 2 * 100.10) / 7                                             # walks two ask levels
        sell_vwap = (5 * 100.20 + 2 * 100.10) / 7                                            # walks two bid levels
        self.assertEqual(er.status, "ok")
        self.assertAlmostEqual(er.buy_vwap, buy_vwap, places=12)
        self.assertAlmostEqual(er.sell_vwap, sell_vwap, places=12)
        self.assertAlmostEqual(er.gross_bps, (sell_vwap - buy_vwap) / 99.95 * 10_000.0, places=9)
        self.assertAlmostEqual(buy_vwap, 100.0285714285714, places=9)
        self.assertAlmostEqual(sell_vwap, 100.1714285714286, places=9)
        too_big = edge.taker_taker_edge_bps(h.bt.books[BYBIT], h.bt.books[BINANCE], 11.0, 0.0, 0.0)
        self.assertEqual(too_big.status, "insufficient_depth")
        self.assertIsNone(too_big.gross_bps)
        self.assertEqual((too_big.buy_depth, too_big.sell_depth), (10.0, 10.0))

    def test_legacy_profile_reproduces_the_old_top_of_book_expression(self):
        for profile in ("legacy_v0", V1):
            h = Harness(mode=TAKER_TAKER, profile=profile)
            books(h, 1.0, dict(bids=((99.90, 5.0),), asks=((100.00, 5.0), (100.10, 5.0))), dict(bids=((100.20, 5.0),), asks=((100.40, 5.0),)))
            sig = h.bt.strategy.evaluate(h.bt.books, h.bt.states, h.bt.inventory, 1.0)
            self.assertIsNotNone(sig)
            if profile == "legacy_v0":
                self.assertAlmostEqual(sig.gross_edge_bps, (100.20 - 100.00) / 100.00 * 10_000.0, places=9)   # ref = buy ask
                self.assertIsNone(sig.naive_edge_bps)
            else:
                self.assertAlmostEqual(sig.gross_edge_bps, (100.20 - 100.00) / 99.95 * 10_000.0, places=9)    # ref = buy mid


def maker_signal(sid: int, t: float, qty: float = 2.0) -> ArbSignal:
    leg = ArbLeg(BYBIT, "buy", qty, 100.0, False, 100.0, 0.0)
    return ArbSignal(sid, t, BYBIT, BINANCE, qty, 5.0, 1.0, 4.0, [leg], MAKER_TAKER, 100.0, 99.0)


class DispatchTests(unittest.TestCase):
    def test_4a_fill_triggered_maker_never_fills_no_hedge_zero_inventory_zero_pnl(self):
        h = Harness(dispatch_mode="fill_triggered", ttl=10.0)
        books(h, 0.5, {}, {})
        h.bt._fire(maker_signal(1, 1.0), 1.0)
        for t in (2.0, 6.0, 10.0, 12.0, 12.2):                                              # the maker expires at ~11 s without a fill
            h.advance(t)
        bt = h.bt
        self.assertEqual(bt.hedge["hedges_sent"], 0)
        self.assertEqual(bt.trade_log, [])
        self.assertEqual(bt.inventory.base, {BYBIT: 0.0, BINANCE: 0.0})
        self.assertEqual(bt.inventory.total_cash, 0.0)
        self.assertEqual((bt.gross_pnl, bt.fees_paid, bt.unhedged_loss), (0.0, 0.0, 0.0))
        self.assertEqual(bt.hedge.get("orphan_hedges", 0), 0)

    def test_4b_simultaneous_maker_never_fills_orphan_hedge_is_reported_and_unwound(self):
        h = Harness(dispatch_mode="simultaneous", ttl=10.0)
        books(h, 0.5, {}, {})
        h.bt._fire(maker_signal(1, 1.0), 1.0)
        self.assertEqual(h.bt.hedge["hedges_sent_before_fill"], 1)
        h.advance(1.003)                                                                     # the hedge lands: sold 2 @ 99 on binance
        self.assertEqual(h.bt.inventory.base, {BYBIT: 0.0, BINANCE: -2.0})                   # naked short while the maker rests
        for t in (2.0, 6.0, 10.0, 12.0, 12.2):
            h.advance(t)
        bt = h.bt
        self.assertEqual((bt.hedge["orphan_hedges"], bt.hedge["orphan_qty"]), (1, 2.0))
        # residual policy (legacy): instant flatten at the touch -> buy 2 @ 101 on binance
        self.assertEqual(bt.inventory.base, {BYBIT: 0.0, BINANCE: 0.0})
        sold, bought = 2 * 99.0, 2 * 101.0
        fees = sold * 5.0 / 10_000.0 + bought * 5.0 / 10_000.0
        self.assertAlmostEqual(bt.inventory.total_cash, sold - bought - fees, places=12)        # -4.2: the ledger P&L
        self.assertAlmostEqual(bt.fees_paid, fees, places=12)
        self.assertAlmostEqual(bt.unhedged_loss, (101.0 - 100.0) * 2.0, places=12)             # attribution: flatten price vs mid
        self.assertEqual(bt.signals_unhedged, 1)
        h.assert_ledger_reconciles(self)

    def test_5_partial_fill_hedge_equals_filled_size_and_the_remainder_expires_without_residual(self):
        h = Harness(ttl=10.0)
        books(h, 0.5, {}, {})
        h.send_maker(1.0)
        h.sell_trade(5.0, BYBIT, volume=1.7)                                                 # 1.2 fills (queue 0.5 ahead)
        h.advance(5.003)
        for t in (8.0, 12.0, 12.2):                                                           # the remaining 0.8 expires unfilled
            h.advance(t)
        bt = h.bt
        hedge = [r for r in bt.trade_log if r["taker"]]
        self.assertEqual([(r["venue"], r["side"], r["qty"]) for r in hedge], [(BINANCE, "sell", 1.2)])
        self.assertEqual(bt.inventory.base, {BYBIT: 1.2, BINANCE: -1.2})
        self.assertEqual(bt.inventory.net_base, 0.0)
        self.assertEqual((bt.signals_unhedged, bt.hedge.get("instant_unwinds", 0)), (0, 0))   # balanced: nothing residual to unwind
        cash = -1.2 * 100.0 - (1.2 * 100.0 * -0.5 / 10_000.0) + 1.2 * 99.0 - (1.2 * 99.0 * 5.0 / 10_000.0)
        self.assertAlmostEqual(bt.inventory.total_cash, cash, places=12)
        h.assert_ledger_reconciles(self)

    def test_6_adverse_fill_with_delayed_hedge_ledger_equals_hand_calculation_markout_not_subtracted(self):
        h = Harness(entry={BYBIT: 0.003, BINANCE: 0.5})                                        # the hedge takes 0.5 s to land
        books(h, 0.5, {}, {})
        h.bt._fire(maker_signal(1, 1.0), 1.0)                                                # quote-time hedge price 99 (binance bid)
        h.sell_trade(5.0, BYBIT, volume=2.5)                                                 # maker fills 2.0 @ 100 at t=5; hedge sent at 5.0
        h.book(5.2, BINANCE, bids=((98.96, 5.0), (98.0, 5.0)), asks=((101.0, 5.0), (102.0, 5.0)))   # binance bid moves 4 bps against us
        h.advance(5.5)                                                                        # hedge lands and sells 2 @ 98.96 (inside the 5 bps walk limit)
        bt = h.bt
        hedge = [r for r in bt.trade_log if r["taker"]]
        self.assertEqual([(r["qty"], r["price"]) for r in hedge], [(2.0, 98.96)])
        maker_fee = 2.0 * 100.0 * -0.5 / 10_000.0
        hedge_fee = 2.0 * 98.96 * 5.0 / 10_000.0
        cash = -200.0 - maker_fee + 2.0 * 98.96 - hedge_fee
        self.assertAlmostEqual(bt.inventory.total_cash, cash, places=12)
        self.assertAlmostEqual(bt.gross_pnl, (98.96 - 100.0) * 2.0, places=12)
        self.assertAlmostEqual(bt.fees_paid, maker_fee + hedge_fee, places=12)
        realized = bt.gross_pnl - bt.fees_paid - bt.unhedged_loss
        self.assertAlmostEqual(realized, cash, places=12)                                     # the ledger IS the P&L: no markout term
        self.assertAlmostEqual(cash, -2.16896, places=9)
        lg = bt._legging_report()
        row = lg["rows"][0]
        self.assertAlmostEqual(row["legging_s"], 0.5, places=9)
        self.assertEqual(row["hedge_move_bps"], 0.0)                                           # the touch had not moved yet at the maker fill (5.0)
        self.assertAlmostEqual(row["hedge_exec_move_bps"], (98.96 - 99.0) / 99.0 * 10_000.0, places=9)   # quote -> executed: -4.04 bps, adverse
        self.assertAlmostEqual(lg["hedge_exec_move_bps"]["bid"]["mean"], (98.96 - 99.0) / 99.0 * 10_000.0, places=9)
        self.assertEqual(row["hedge_fill_vwap"], 98.96)
        h.assert_ledger_reconciles(self)

    def test_7_fees_and_funding_on_both_legs_across_one_settlement(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
            ts_ms = int(day.timestamp() * 1000)
            for venue, rate in ((BYBIT, "0.0001"), (BINANCE, "0.0002")):
                Path(tmp, f"{venue}.jsonl").write_text(json.dumps({"funding_ts_ms": ts_ms, "venue": venue, "rate": rate, "instrument_id": "BTC-USDT-PERP"}) + "\n")
            h = Harness(cfg_kw={"funding_rate_dir": tmp})
            books(h, 0.5, {}, {})
            h.send_maker(1.0)
            h.sell_trade(5.0, BYBIT, volume=2.5)
            h.advance(5.003)
            bt = h.bt
            self.assertIsNotNone(bt.settler)
            bt.settler.settle(day.timestamp() + 1.0, bt.inventory, bt.books)                 # the 08:00 settlement, marked at each venue's mid (100)
            bybit_pay = 2.0 * 100.0 * 0.0001                                                  # long 2 pays
            binance_pay = -2.0 * 100.0 * 0.0002                                               # short 2 receives
            self.assertAlmostEqual(bt.settler.funding_paid, bybit_pay + binance_pay, places=12)   # -0.02 net received
            fees = 2.0 * 100.0 * -0.5 / 10_000.0 + 2.0 * 99.0 * 5.0 / 10_000.0
            cash = -200.0 + 198.0 - fees - (bybit_pay + binance_pay)
            self.assertAlmostEqual(bt.inventory.total_cash, cash, places=12)
            self.assertAlmostEqual(bt.inventory.cash[BYBIT], -200.0 + 0.01 - bybit_pay, places=12)
            self.assertAlmostEqual(bt.inventory.cash[BINANCE], 198.0 - 0.099 - binance_pay, places=12)
            realized = bt.gross_pnl - bt.fees_paid - bt.unhedged_loss - bt.settler.funding_paid
            self.assertAlmostEqual(realized, cash, places=12)
            self.assertAlmostEqual(cash, -2.069, places=12)


class EquityBridgeTests(unittest.TestCase):
    def test_bridge_terms_reconcile_after_a_hedged_cycle(self):
        h = Harness()
        books(h, 0.5, {}, {})
        h.send_maker(1.0)
        h.sell_trade(5.0, BYBIT, volume=2.5)
        h.advance(5.003)
        bt = h.bt
        open_value = sum(bt.inventory.base[v] * bt.books[v].mid for v in (BYBIT, BINANCE))
        doc = bt._equity_bridge(open_value, bt.inventory.total_cash + open_value)
        self.assertTrue(doc["ok"] and doc["checked"])
        self.assertAlmostEqual(doc["cash_from_trade_log"], doc["ledger_cash"], places=12)
        self.assertAlmostEqual(doc["cash_gap"], 0.0, places=12)

    def test_bridge_fails_loudly_when_the_ledger_is_tampered(self):
        from new_backtest.backtester import EquityBridgeError
        h = Harness()
        books(h, 0.5, {}, {})
        h.send_maker(1.0)
        h.sell_trade(5.0, BYBIT, volume=2.5)
        h.advance(5.003)
        bt = h.bt
        bt.inventory.cash[BYBIT] += 0.001                                                     # 1e-3 USDT appears from nowhere
        with self.assertRaises(EquityBridgeError):
            bt._equity_bridge(0.0, bt.inventory.total_cash)
        bt.config.equity_bridge = "warn"
        import warnings
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            doc = bt._equity_bridge(0.0, bt.inventory.total_cash)
        self.assertFalse(doc["ok"])
        self.assertTrue(any("equity bridge failed" in str(x.message) for x in w))


class OpportunityCountOnRealDay(unittest.TestCase):
    """Counts taker/taker opportunities (new edge function, size = 1 SOL, perp fees, net >= 1 bps + 0.5 buffer) on SOL 2022-01-13 and
    compares with the ledger's realised trades from a backtest of that day. Opt-in: needs the data root and EDGE_REAL_DAY=1."""

    def test_count_on_sol_2022_01_13(self):
        root = Path("/Users/vaibhav/Documents/Newproject/data")
        if os.environ.get("EDGE_REAL_DAY") != "1" or not (root / "bybit").is_dir():
            self.skipTest("set EDGE_REAL_DAY=1 with the Jan-2022 data present")
        import importlib.util
        spec = importlib.util.spec_from_file_location("count_opportunities", Path(__file__).resolve().parents[1] / "scripts" / "count_opportunities.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = mod.count_day(str(root), "SOLUSDT", "2022-01-13", size=1.0)
        self.assertIn("opportunities", out)
        self.assertGreaterEqual(out["evaluations"], 1)


if __name__ == "__main__":
    unittest.main()


class ConditionalEdgeTests(unittest.TestCase):
    """maker_taker_expected_edge_bps by hand. Hedge book: binance bids 100.00 / asks 100.10 (size 5)."""

    def _hedge_book(self):
        h = Harness()
        h.book(1.0, BINANCE, bids=((100.00, 5.0), (99.0, 5.0)), asks=((100.10, 5.0), (101.0, 5.0)))
        return h.bt.books[BINANCE]

    def test_bid_side_sign_and_components(self):
        hb = self._hedge_book()
        q = 99.94                                                                             # our passive bid
        er = edge.maker_taker_expected_edge_bps(q, "bid", hb, 1.0, -0.5, 5.0)                   # no move, no extras
        self.assertEqual(er.status, "ok")
        self.assertAlmostEqual(er.gross_bps, (100.00 - q) / q * 10_000.0, places=9)            # sell high (hedge bid), buy low (q): positive
        self.assertAlmostEqual(er.naive_bps, er.gross_bps, places=12)
        self.assertAlmostEqual(er.net_bps, er.gross_bps - 4.5, places=9)
        adverse = edge.maker_taker_expected_edge_bps(q, "bid", hb, 1.0, -0.5, 5.0, expected_hedge_move_bps=-3.0)
        self.assertAlmostEqual(adverse.gross_bps, (100.00 * (1 - 3.0 / 10_000.0) - q) / q * 10_000.0, places=9)   # a lower expected bid
        self.assertLess(adverse.gross_bps, er.gross_bps)
        self.assertAlmostEqual(er.gross_bps - adverse.gross_bps, 100.0 * 3.0 / 10_000.0 / q * 10_000.0, places=9)
        full = edge.maker_taker_expected_edge_bps(q, "bid", hb, 1.0, -0.5, 5.0, expected_hedge_move_bps=-3.0, expected_hedge_slippage_bps=0.7,
                                                  funding_holding_bps=0.2, p_fail=0.07, loss_fail_bps=7.8)
        self.assertAlmostEqual(full.net_bps, adverse.gross_bps - 4.5 - 0.7 - 0.2 - 0.07 * 7.8, places=9)
        self.assertAlmostEqual(full.components["failure_cost_bps"], 0.546, places=12)

    def test_bid_sign_flip_is_detected(self):
        """Kills the 2026-11-01 surviving mutation gross = (q - expected): resting BELOW the hedge bid must be a positive edge and
        resting ABOVE it a negative one, with the quote as the buy leg and the expected hedge price as the sell leg."""
        hb = self._hedge_book()                                                               # hedge bid 100.00
        below = edge.maker_taker_expected_edge_bps(99.90, "bid", hb, 1.0, 0.0, 0.0)
        above = edge.maker_taker_expected_edge_bps(100.10, "bid", hb, 1.0, 0.0, 0.0)
        self.assertAlmostEqual(below.gross_bps, 0.10 / 99.90 * 10_000.0, places=9)
        self.assertAlmostEqual(above.gross_bps, -0.10 / 100.10 * 10_000.0, places=9)
        self.assertGreater(below.gross_bps, 0.0)
        self.assertLess(above.gross_bps, 0.0)
        self.assertEqual((below.buy_vwap, below.sell_vwap), (99.90, 100.00))
        self.assertLess(edge.maker_taker_expected_edge_bps(99.90, "bid", hb, 1.0, 0.0, 0.0, expected_hedge_move_bps=-5.0).gross_bps,
                        below.gross_bps)                                                      # an adverse move can only lower it
        ask_below = edge.maker_taker_expected_edge_bps(100.05, "ask", hb, 1.0, 0.0, 0.0)      # resting below the hedge ask 100.10
        self.assertLess(ask_below.gross_bps, 0.0)

    def test_ask_side_sign(self):
        hb = self._hedge_book()
        q = 100.16                                                                            # our passive ask; the hedge BUYS at 100.10
        er = edge.maker_taker_expected_edge_bps(q, "ask", hb, 1.0, -0.5, 5.0)
        self.assertAlmostEqual(er.gross_bps, (q - 100.10) / 100.10 * 10_000.0, places=9)        # positive: sell q above the hedge ask
        adverse = edge.maker_taker_expected_edge_bps(q, "ask", hb, 1.0, -0.5, 5.0, expected_hedge_move_bps=-3.0)
        self.assertLess(adverse.gross_bps, er.gross_bps)                                      # a negative move RAISES the expected ask
        self.assertGreater(adverse.sell_vwap, adverse.buy_vwap)

    def test_v1_strategy_logs_naive_and_conditional_edges_and_gates_on_the_conditional_one(self):
        def mk(**kw):
            h = Harness(profile=V1, **kw)
            h.book(1.0, BYBIT, bids=((99.90, 5.0), (99.0, 5.0)), asks=((100.15, 5.0), (101.0, 5.0)))
            h.book(1.0, BINANCE, bids=((100.00, 5.0), (99.0, 5.0)), asks=((100.10, 5.0), (101.0, 5.0)))
            return h, h.bt.strategy.evaluate(h.bt.books, h.bt.states, h.bt.inventory, 1.0)
        h, sig = mk(p_fail=0.0, loss_fail_bps=0.0)
        self.assertIsNotNone(sig)
        q = 100.0 * (1.0 - 6.0 / 10_000.0)                                                    # the maker bid the gate derives from the hedge bid
        naive = (100.0 - q) / q * 10_000.0
        self.assertAlmostEqual(sig.naive_edge_bps, naive, places=9)
        self.assertAlmostEqual(sig.conditional_edge_bps, naive - 4.5 - 0.5, places=9)         # no move, no failure cost: same as the old net
        self.assertEqual(sig.edge_ref, "buy_price")
        h2, sig2 = mk(p_fail=0.07, loss_fail_bps=7.8, expected_hedge_move_bps_bid=-2.0)
        self.assertIsNotNone(sig2)                                                            # the quote moves instead: deeper bid, still fires
        q2 = 100.0 * (1.0 - 2.0 / 10_000.0) * (1.0 - (6.0 + 0.07 * 7.8) / 10_000.0)            # expected hedge 99.98, less fees+edge+buffer+failure cost
        self.assertAlmostEqual(sig2.legs[0].limit_price, q2, places=9)
        self.assertLess(sig2.legs[0].limit_price, sig.legs[0].limit_price)                    # deeper than the naive quote
        self.assertGreaterEqual(sig2.conditional_edge_bps, 1.0)                               # the gate is met on the conditional edge
        self.assertGreater(sig2.naive_edge_bps, sig2.conditional_edge_bps + 2.0)              # and the naive edge overstates it by move + failure cost
