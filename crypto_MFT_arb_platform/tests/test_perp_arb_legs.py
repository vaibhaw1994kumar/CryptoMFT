"""Perp-to-perp cross-venue arb: how the two legs are fired, and the passive-ask direction.

Pins the leg sequencing the engine already has, so a later change cannot move it silently:
  * TAKER_TAKER: both taker legs are sent in the same call, at the same timestamp, each with its own latency draw.
  * MAKER_TAKER: only the passive leg is sent on the signal; the taker hedge is sent when (and only after) the passive leg fills.
  * Both legs use the perpetual fee presets.
And covers the opt-in passive-ask direction (ArbConfig.maker_passive_side="both"): rest a sell on the maker venue, hedge by
BUYING on the taker venue. The default ("bid") keeps every existing result bit-identical.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from new_backtest.arb_strategy import MAKER_TAKER, TAKER_TAKER, ArbConfig, ArbLeg, ArbSignal, CrossVenueArbStrategy
from new_backtest.event_loader import TRADE
from new_backtest.config import FEE_PRESETS

from test_maker_order_lifetime import BINANCE, BYBIT, Harness

ROOT = Path(__file__).resolve().parents[1]


def taker_signal(sid: int, t: float, qty: float = 2.0) -> ArbSignal:
    """Buy bybit at its ask 101, sell binance at its bid 99 (the Harness book), both taker."""
    return ArbSignal(signal_id=sid, ts=t, buy_venue=BYBIT, sell_venue=BINANCE, qty=qty, gross_edge_bps=5.0, fee_bps=1.0, net_edge_bps=4.0,
                     mode=TAKER_TAKER, buy_price=101.0, sell_price=99.0,
                     legs=[ArbLeg(BYBIT, "buy", qty, 101.5, True, 101.0, 5.5), ArbLeg(BINANCE, "sell", qty, 98.5, True, 99.0, 5.0)])


class LegTimingTests(unittest.TestCase):
    def test_taker_taker_sends_both_legs_concurrently_with_independent_latencies(self):
        h = Harness(mode=TAKER_TAKER, entry={BYBIT: 0.009, BINANCE: 0.004})
        h.book(0.0, BYBIT)
        h.book(0.0, BINANCE)
        h.bt._fire(taker_signal(1, 1.0), 1.0)
        inflight = sorted(h.bt.pending.in_flight(), key=lambda o: o.venue)
        self.assertEqual([(o.venue, o.side, o.sent_ts, round(o.release_ts, 6)) for o in inflight],
                         [(BINANCE, "sell", 1.0, 1.004), (BYBIT, "buy", 1.0, 1.009)])     # same send instant, own flight each
        self.assertEqual(h.bt.orders_sent, 2)
        h.advance(1.02)                                                                     # both have landed
        rows = sorted(h.bt.trade_log, key=lambda r: r["venue"])
        self.assertEqual([(r["venue"], r["side"], r["taker"], r["qty"]) for r in rows], [(BINANCE, "sell", True, 2.0), (BYBIT, "buy", True, 2.0)])
        self.assertEqual(h.bt.inventory.net_base, 0.0)
        h.assert_ledger_reconciles(self)

    def test_maker_taker_sends_only_the_passive_leg_and_the_hedge_strictly_after_its_fill(self):
        h = Harness()
        h.book(0.0, BYBIT)
        h.book(0.0, BINANCE)
        h.send_maker(1.0)
        self.assertEqual(h.bt.orders_sent, 1)
        self.assertEqual(h.bt.hedge["hedges_sent"], 0)
        h.advance(4.0)
        self.assertEqual(h.bt.hedge["hedges_sent"], 0)                                      # resting, unfilled: no hedge, no exposure
        self.assertEqual([r for r in h.bt.trade_log if r["taker"]], [])
        h.sell_trade(5.0, BYBIT, volume=2.5)                                               # the passive bid fills at t=5
        self.assertEqual(h.bt.hedge["hedges_sent"], 1)
        hedge = [o for o in h.bt.pending.in_flight() if o.is_hedge]
        self.assertEqual([(o.venue, o.side, o.qty, o.sent_ts) for o in hedge], [(BINANCE, "sell", 2.0, 5.0)])   # sent at the fill, not before
        h.advance(5.01)
        taker = [r for r in h.bt.trade_log if r["taker"]]
        self.assertEqual([(r["venue"], r["side"], r["qty"]) for r in taker], [(BINANCE, "sell", 2.0)])
        self.assertGreaterEqual(taker[0]["ts"], 5.0)
        self.assertEqual(h.bt.inventory.net_base, 0.0)
        h.assert_ledger_reconciles(self)

    def test_both_legs_are_priced_with_perpetual_fee_presets(self):
        h = Harness()
        fees = {v: (s.fees.maker_bps, s.fees.taker_bps) for v, s in h.bt.venue_specs.items()}
        self.assertEqual(fees, {BYBIT: (FEE_PRESETS["bybit_perp_mm"].maker_bps, FEE_PRESETS["bybit_perp_mm"].taker_bps),
                                BINANCE: (FEE_PRESETS["binance_perp_vip0"].maker_bps, FEE_PRESETS["binance_perp_vip0"].taker_bps)})
        text = (ROOT.parent / "discarded" / "v1.0" / "scripts" / "run_sol_wf.py").read_text()
        self.assertIn('bybit_fee_preset="bybit_perp_', text)
        self.assertIn('binance_fee_preset="binance_perp_', text)


def two_sided_books(h: Harness, t: float = 1.0) -> None:
    """Only the passive-ASK direction on bybit has an edge: hedge = buy binance at 100.00; a bybit sell at 100.06 (fees 4.5 +
    edge 1 + buffer 0.5 bps over the hedge) improves bybit's 100.30 ask without crossing its 99.95 bid. The other three
    directions fall outside their maker venue's touch (bybit bid: 99.90 x (1 - 6 bps) = 99.84 <= 99.95; binance bid: 99.95 x
    (1 - 9 bps) <= 99.90; binance ask: 100.30 / (1 - 9 bps) >= 100.00). Both spreads are inside max_spread_bps (50)."""
    h.book(t, BYBIT, bids=((99.95, 5.0), (99.0, 5.0)), asks=((100.30, 5.0), (101.0, 5.0)))
    h.book(t, BINANCE, bids=((99.90, 5.0), (99.0, 5.0)), asks=((100.00, 5.0), (101.0, 5.0)))


class PassiveAskTests(unittest.TestCase):
    def test_default_rests_bids_only_so_the_ask_opportunity_is_not_taken(self):
        h = Harness()
        two_sided_books(h)
        self.assertIsNone(h.bt.strategy.evaluate(h.bt.books, h.bt.states, h.bt.inventory, 1.0))
        self.assertEqual(h.bt.strategy.rejects["stale"], 0)                                 # the books were fresh: rejected for edge, not age
        self.assertEqual(h.bt.strategy.config.maker_passive_side, "bid")

    def test_both_sides_rests_a_sell_on_the_maker_venue_hedged_by_a_buy(self):
        h = Harness(maker_passive_side="both")
        two_sided_books(h)
        sig = h.bt.strategy.evaluate(h.bt.books, h.bt.states, h.bt.inventory, 1.0)
        self.assertIsNotNone(sig)
        (leg,) = sig.legs
        self.assertEqual((leg.venue, leg.side, leg.is_taker), (BYBIT, "sell", False))
        self.assertEqual((sig.maker_venue, sig.hedge_venue, sig.hedge_side), (BYBIT, BINANCE, "buy"))
        self.assertEqual((sig.sell_venue, sig.buy_venue, sig.buy_price), (BYBIT, BINANCE, 100.0))
        self.assertGreater(leg.limit_price, 100.0)
        self.assertLess(leg.limit_price, 100.30)
        self.assertGreaterEqual(sig.net_edge_bps, 1.0)
        self.assertAlmostEqual(leg.limit_price, 100.0 / (1.0 - 6.0 / 10_000.0), places=9)   # fees 4.5 + edge 1 + buffer 0.5 bps

    def test_invalid_passive_side_is_refused(self):
        with self.assertRaises(ValueError):
            Harness(maker_passive_side="down")

    def test_ask_only_rests_a_sell_and_never_a_bid(self):
        h = Harness(maker_passive_side="ask")
        h.book(1.0, BYBIT, bids=((99.90, 5.0), (99.0, 5.0)), asks=((100.15, 5.0), (101.0, 5.0)))
        h.book(1.0, BINANCE, bids=((100.00, 5.0), (99.0, 5.0)), asks=((100.10, 5.0), (101.0, 5.0)))
        sig = h.bt.strategy.evaluate(h.bt.books, h.bt.states, h.bt.inventory, 1.0)
        if sig is not None:
            self.assertEqual([leg.side for leg in sig.legs], ["sell"])
            self.assertEqual(sig.hedge_side, "buy")
        both = Harness(maker_passive_side="both")
        both.book(1.0, BYBIT, bids=((99.90, 5.0), (99.0, 5.0)), asks=((100.15, 5.0), (101.0, 5.0)))
        both.book(1.0, BINANCE, bids=((100.00, 5.0), (99.0, 5.0)), asks=((100.10, 5.0), (101.0, 5.0)))
        sig_both = both.bt.strategy.evaluate(both.bt.books, both.bt.states, both.bt.inventory, 1.0)
        self.assertIsNotNone(sig_both)                                        # these books carry a bid-side edge; ask-only must not take it as a bid
        self.assertEqual([leg.side for leg in sig_both.legs], ["buy"])

    def test_passive_ask_fills_on_a_buy_trade_and_the_hedge_buys_on_the_other_venue(self):
        h = Harness(maker_passive_side="both")
        h.book(0.0, BYBIT)
        h.book(0.0, BINANCE)
        leg = ArbLeg(venue=BYBIT, side="sell", qty=2.0, limit_price=100.0, is_taker=False, expected_price=100.0, fee_bps=0.0)
        sig = ArbSignal(signal_id=1, ts=1.0, buy_venue=BINANCE, sell_venue=BYBIT, qty=2.0, gross_edge_bps=5.0, fee_bps=1.0, net_edge_bps=4.0,
                        legs=[leg], mode=MAKER_TAKER, buy_price=101.0, sell_price=100.0)
        h.bt._fire(sig, 1.0)
        h.advance(2.0)
        self.assertEqual([(o.side, o.price) for o in h.resting], [("ask", 100.0)] if hasattr(h.order(), "price") else [("ask", 100.0)])
        self.assertEqual(h.bt.hedge["hedges_sent"], 0)
        h.event(5.0, TRADE, BYBIT, {"price": 100.0, "volume": 2.5, "side": "b"})           # a buyer lifts our ask
        self.assertEqual(h.bt.hedge["hedges_sent"], 1)
        hedge = [o for o in h.bt.pending.in_flight() if o.is_hedge]
        self.assertEqual([(o.venue, o.side, o.qty) for o in hedge], [(BINANCE, "buy", 2.0)])
        h.advance(5.01)
        rows = sorted(h.bt.trade_log, key=lambda r: r["taker"])
        self.assertEqual([(r["venue"], r["side"], r["taker"], r["qty"]) for r in rows], [(BYBIT, "sell", False, 2.0), (BINANCE, "buy", True, 2.0)])
        self.assertAlmostEqual(rows[1]["price"], 101.0, places=6)                           # binance best ask
        self.assertEqual(h.bt.inventory.base, {BYBIT: -2.0, BINANCE: 2.0})
        self.assertEqual(h.bt.inventory.net_base, 0.0)
        h.assert_ledger_reconciles(self)

    def test_bid_direction_results_are_unchanged_by_the_option(self):
        """A passive bid signal fired by hand behaves the same whether or not the option is on."""
        out = []
        for side in ("bid", "both"):
            h = Harness(maker_passive_side=side)
            h.book(0.0, BYBIT)
            h.book(0.0, BINANCE)
            h.send_maker(1.0)
            h.sell_trade(5.0, BYBIT, volume=2.5)
            h.advance(5.01)
            out.append([(r["venue"], r["side"], r["taker"], r["qty"], r["price"]) for r in h.bt.trade_log])
        self.assertEqual(out[0], out[1])


if __name__ == "__main__":
    unittest.main()


def bid_only_books(h: Harness, t: float = 1.0) -> None:
    """Only the passive-BID direction on bybit has an edge: hedge = sell binance at 100.00; a bybit buy at 99.94 improves its 99.90 bid
    without crossing its 100.15 ask. The ask direction (100.10 / (1 - 6 bps) = 100.16 >= 100.15) and both binance-maker directions are dead."""
    h.book(t, BYBIT, bids=((99.90, 5.0), (99.0, 5.0)), asks=((100.15, 5.0), (101.0, 5.0)))
    h.book(t, BINANCE, bids=((100.00, 5.0), (99.0, 5.0)), asks=((100.10, 5.0), (101.0, 5.0)))


class AccountingTests(unittest.TestCase):
    """Hand-calculated: maker bid 2.0 @ 100 on bybit (maker fee -0.5 bps), hedges sell on binance @ 99 (taker fee 5 bps)."""

    def test_partial_fills_are_hedged_for_the_confirmed_quantity_and_fees_are_charged_once(self):
        h = Harness()
        h.book(0.5, BYBIT)
        h.book(0.5, BINANCE)
        h.send_maker(1.0)
        h.sell_trade(5.0, BYBIT, volume=1.7)                       # queue ahead 0.5: 1.2 of our 2.0 fills
        h.advance(5.003)                                           # the hedge lands 3 ms later (the harness executes at the advance instant)
        h.sell_trade(7.0, BYBIT, volume=1.0)                       # the remaining 0.8 fills
        h.advance(7.003)
        bt = h.bt
        maker = [r for r in bt.trade_log if not r["taker"]]
        hedge = [r for r in bt.trade_log if r["taker"]]
        self.assertEqual([(r["ts"], r["qty"], r["price"]) for r in maker], [(5.0, 1.2, 100.0), (7.0, 0.8, 100.0)])
        self.assertEqual([(round(r["ts"], 3), r["qty"], r["price"]) for r in hedge], [(5.003, 1.2, 99.0), (7.003, 0.8, 99.0)])   # the filled quantity, each time
        self.assertAlmostEqual(maker[0]["fee"], 1.2 * 100.0 * -0.5 / 10_000.0, places=12)
        self.assertAlmostEqual(hedge[0]["fee"], 1.2 * 99.0 * 5.0 / 10_000.0, places=12)
        self.assertAlmostEqual(bt.fees_paid, (-0.006 - 0.004) + (0.0594 + 0.0396), places=12)              # once per execution
        self.assertAlmostEqual(bt.fees_paid, sum(r["fee"] for r in bt.trade_log), places=12)
        self.assertAlmostEqual(bt.inventory.cash[BYBIT], -200.0 + 0.010, places=12)                           # paid 200, rebate 0.01
        self.assertAlmostEqual(bt.inventory.cash[BINANCE], 198.0 - 0.099, places=12)                          # received 198, fee 0.099
        self.assertEqual(bt.inventory.base, {BYBIT: 2.0, BINANCE: -2.0})
        self.assertEqual(bt.signals_completed, 1)
        self.assertAlmostEqual(bt.gross_pnl, (99.0 - 100.0) * 2.0, places=12)
        realized = bt.gross_pnl - bt.fees_paid - bt.unhedged_loss - 0.0                                       # no funding here
        self.assertAlmostEqual(realized, bt.inventory.total_cash, places=12)                                   # cash and realized agree
        self.assertAlmostEqual(realized, -2.089, places=12)
        lg = bt._legging_report()
        self.assertEqual((lg["signals_with_maker_fill"], lg["hedged_signals"], lg["unhedged_after_maker_fill"]), (1, 1, 0))
        row = lg["rows"][0]
        self.assertEqual((row["maker_first_fill_ts"], row["maker_last_fill_ts"], row["hedge_first_sent_ts"]), (5.0, 7.0, 5.0))
        self.assertAlmostEqual(row["hedge_last_fill_ts"], 7.003, places=9)
        self.assertAlmostEqual(row["legging_s"], 2.003, places=9)                                               # first maker fill -> last hedge fill
        self.assertEqual((row["maker_qty"], row["hedge_fill_qty"]), (2.0, 2.0))
        h.assert_ledger_reconciles(self)

    def test_markouts_are_diagnostics_and_never_enter_realized_pnl(self):
        import inspect
        from new_backtest import backtester as bt_mod
        src = inspect.getsource(bt_mod.CrossVenueArbBacktester._build_result)
        formula = src[src.index("realized_pnl=("):src.index("realized_pnl=(") + 200]
        self.assertIn("self.gross_pnl - self.fees_paid - self.unhedged_loss", formula)
        self.assertNotIn("markout", formula)

    def test_bid_direction_prices_the_passive_buy_off_the_hedge_bid(self):
        h = Harness()
        bid_only_books(h)
        sig = h.bt.strategy.evaluate(h.bt.books, h.bt.states, h.bt.inventory, 1.0)
        self.assertIsNotNone(sig)
        (leg,) = sig.legs
        self.assertEqual((leg.venue, leg.side, sig.hedge_venue, sig.hedge_side), (BYBIT, "buy", BINANCE, "sell"))
        self.assertAlmostEqual(leg.limit_price, 100.0 * (1.0 - 6.0 / 10_000.0), places=9)                    # below the hedge bid by fees+edge+buffer
        self.assertGreater(sig.sell_price, leg.limit_price)                                                   # sell high (hedge), buy low (maker)
        self.assertGreaterEqual(sig.net_edge_bps, 1.0)
