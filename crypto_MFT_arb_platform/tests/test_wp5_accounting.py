"""WP5 funding / margin / accounting: fee reconciliation, per-venue identities, attribution_pnl, margin modes, liquidation price,
reserved margin, funding multiplier. Harness books: bids 99/98, asks 101/102 (size 5) on both venues; maker fee -0.5 bps (bybit
perp mm), binance taker 5 bps."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from new_backtest.arb_strategy import InventoryBook
from new_backtest.backtester import EquityBridgeError
from new_backtest.funding_settler import FundingSettler
from new_backtest.margin_model import MarginModel, MarginModelConfig, MarginTier, VenueMargin

from test_maker_order_lifetime import BINANCE, BYBIT, Harness


def hedged_pair(**cfg_kw) -> Harness:
    h = Harness(cfg_kw=cfg_kw or None)
    h.book(0.5, BYBIT); h.book(0.5, BINANCE)
    h.send_maker(1.0)
    h.sell_trade(5.0, BYBIT, volume=2.5)
    h.advance(5.003)
    return h


class FeeReconciliationTests(unittest.TestCase):
    def test_every_fill_fee_matches_the_schedule_and_the_rows_carry_rate_and_kind(self):
        h = hedged_pair()
        bt = h.bt
        rows = bt.trade_log
        self.assertEqual(sorted(r["kind"] for r in rows), ["maker", "taker"])
        maker = next(r for r in rows if r["kind"] == "maker")
        taker = next(r for r in rows if r["kind"] == "taker")
        self.assertAlmostEqual(maker["fee"], 2.0 * 100.0 * -0.5 / 10_000.0, places=12)       # rebate
        self.assertAlmostEqual(taker["fee"], 2.0 * 99.0 * 5.0 / 10_000.0, places=12)
        self.assertAlmostEqual(maker["fee_bps"], -0.5, places=9)
        val = bt._common_valuation()
        doc = bt._equity_bridge(val["open_position_value"], bt.inventory.total_cash + val["open_position_value"])
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["fee_mismatches"], [])

    def test_a_fee_that_does_not_match_the_schedule_fails_the_bridge(self):
        h = hedged_pair()
        bt = h.bt
        row = next(r for r in bt.trade_log if r["kind"] == "taker")
        bt.inventory.cash[BINANCE] -= 0.01                                                     # the ledger charged 1 cent more...
        row["fee"] += 0.01                                                                      # ...and the log agrees, so cash reconciles
        val = bt._common_valuation()
        with self.assertRaises(EquityBridgeError) as cm:
            bt._equity_bridge(val["open_position_value"], bt.inventory.total_cash + val["open_position_value"])
        self.assertIn("fee mismatches 1", str(cm.exception))


class PerVenueIdentityTests(unittest.TestCase):
    def test_cash_and_base_reconcile_per_venue(self):
        h = hedged_pair()
        bt = h.bt
        val = bt._common_valuation()
        doc = bt._equity_bridge(val["open_position_value"], bt.inventory.total_cash + val["open_position_value"])
        pv = doc["per_venue"]
        self.assertEqual(set(pv), {BYBIT, BINANCE})
        self.assertEqual((pv[BYBIT]["ledger_base"], pv[BINANCE]["ledger_base"]), (2.0, -2.0))
        for v in (BYBIT, BINANCE):
            self.assertAlmostEqual(pv[v]["cash_gap"], 0.0, places=12)
            self.assertAlmostEqual(pv[v]["base_gap"], 0.0, places=12)
            self.assertTrue(pv[v]["ok"])

    def test_a_venue_cash_leak_that_nets_to_zero_in_total_is_still_caught(self):
        h = hedged_pair()
        bt = h.bt
        bt.inventory.cash[BYBIT] += 0.5
        bt.inventory.cash[BINANCE] -= 0.5                                                      # total cash unchanged
        val = bt._common_valuation()
        with self.assertRaises(EquityBridgeError) as cm:
            bt._equity_bridge(val["open_position_value"], bt.inventory.total_cash + val["open_position_value"])
        self.assertIn("per-venue failures", str(cm.exception))


class AttributionTests(unittest.TestCase):
    def test_attribution_pnl_is_the_old_realized_pnl_and_the_headline_is_the_ledger(self):
        h = hedged_pair()
        r = h.bt._build_result(SimpleNamespace(report=lambda: {}), {}, 0.0)
        self.assertAlmostEqual(r.attribution_pnl, r.realized_pnl, places=12)
        self.assertAlmostEqual(r.attribution_pnl, r.gross_pnl - r.fees_paid - r.unhedged_loss - r.funding_paid, places=12)
        self.assertAlmostEqual(r.ending_equity, r.cash_pnl + r.valuation["open_position_value"], places=12)
        self.assertEqual(r.equity_bridge["funding_predicted_at_signal"], 0.0)


def _margin(mode="cross", capital=50.0, reserve=True):
    tiers = (MarginTier(max_notional=1e6, initial_rate=0.10, maintenance_rate=0.05),)
    return MarginModelConfig(venues={BYBIT: VenueMargin(capital, tiers, liquidation_fee_bps=10.0, mode=mode, reserve_open_orders=reserve),
                                     BINANCE: VenueMargin(capital, tiers, liquidation_fee_bps=10.0, mode=mode, reserve_open_orders=reserve)},
                             check_every_s=0.5, flatten_other_venue=False)


class MarginModeTests(unittest.TestCase):
    def _books(self):
        b = SimpleNamespace(ready=True, mid=100.0, levels=lambda side, n: [(100.0, 50.0)])
        return {BYBIT: b, BINANCE: b}

    def test_liquidation_price_cross_and_isolated_by_hand(self):
        m = MarginModel(_margin("cross", capital=5.0), (BYBIT, BINANCE))
        inv = InventoryBook()
        inv.apply(BYBIT, 1.0, -100.0)                                                           # long 1 at 100 with 5 of capital
        m.note_fill(BYBIT, 1.0, 100.0, inv)
        books = self._books()
        # cross: equity = 5 - 100 + p ; maintenance = 0.05 p  ->  p = 95 / 0.95 = 100.0
        self.assertAlmostEqual(m.liquidation_price(BYBIT, inv, books), 95.0 / 0.95, places=9)
        self.assertIsNone(m.liquidation_price(BINANCE, inv, books))                            # flat
        iso = MarginModel(_margin("isolated", capital=5.0), (BYBIT, BINANCE))
        iso.note_fill(BYBIT, 1.0, 100.0, inv)
        # isolated: buffer = initial - maintenance = 10 - 5 = 5 per 1 unit -> liquidation at 100 - 5 = 95
        self.assertAlmostEqual(iso.liquidation_price(BYBIT, inv, books), 95.0, places=9)
        self.assertAlmostEqual(iso.unrealised(BYBIT, inv, books), 0.0, places=9)

    def test_minimum_liquidation_distance_and_blocked_reasons_are_recorded(self):
        inv = InventoryBook()
        inv.apply(BYBIT, 1.0, -100.0)
        m = MarginModel(_margin("cross", capital=20.0), (BYBIT, BINANCE))
        m.note_fill(BYBIT, 1.0, 100.0, inv)
        far = SimpleNamespace(ready=True, mid=100.0, levels=lambda side, n: [(100.0, 50.0)])
        m.step(1.0, inv, {BYBIT: far, BINANCE: far})
        # cross liquidation price: equity 20 - 100 + p = 0.05 p -> p = 80 / 0.95 = 84.2105; distance from the 100 mark in bps
        d = m.liq_distance[BYBIT]
        self.assertAlmostEqual(d["liquidation_price"], 80.0 / 0.95, places=9)
        self.assertAlmostEqual(d["min_bps"], (100.0 - 80.0 / 0.95) / 100.0 * 1e4, places=6)
        nearer = SimpleNamespace(ready=True, mid=90.0, levels=lambda side, n: [(90.0, 50.0)])
        m.step(2.0, inv, {BYBIT: nearer, BINANCE: nearer})
        self.assertLess(m.liq_distance[BYBIT]["min_bps"], d["min_bps"])         # closer approach replaces it
        self.assertEqual(m.liq_distance[BYBIT]["ts"], 2.0)
        self.assertIsNone(m.liq_distance[BINANCE])                              # flat venue: never measured
        rep = m.report(inv, {BYBIT: nearer, BINANCE: nearer})
        self.assertIn("min_liquidation_distance", rep)
        self.assertEqual(set(rep["blocked_signals_by_reason"]), {"initial_margin_or_equity", "reserved_for_resting_orders", "utilisation_limit", "tier_exceeded"})

    def test_isolated_mode_liquidates_on_the_position_s_own_margin_even_when_the_account_is_rich(self):
        inv = InventoryBook()
        inv.apply(BYBIT, 1.0, -100.0)
        rich_cross = MarginModel(_margin("cross", capital=1_000.0), (BYBIT, BINANCE))
        rich_iso = MarginModel(_margin("isolated", capital=1_000.0), (BYBIT, BINANCE))
        for m in (rich_cross, rich_iso):
            m.note_fill(BYBIT, 1.0, 100.0, inv)
        down = SimpleNamespace(ready=True, mid=90.0, levels=lambda side, n: [(90.0, 50.0)])    # -10: more than the 10 - 4.5 buffer
        books = {BYBIT: down, BINANCE: down}
        self.assertEqual(rich_cross.step(1.0, inv, books), [])                                 # 1000 of equity: no cross liquidation
        out = rich_iso.step(1.0, inv, books)
        self.assertEqual([(v, side, qty) for v, side, qty, _ in out], [(BYBIT, "sell", 1.0)])
        self.assertEqual(rich_iso.stats["isolated_liquidations"], 1)
        self.assertFalse(rich_iso.events[-1]["adl_exposure"])

    def test_adl_exposure_is_flagged_when_the_book_cannot_absorb_the_liquidation(self):
        inv = InventoryBook()
        inv.apply(BYBIT, 10.0, -1_000.0)
        m = MarginModel(_margin("cross", capital=30.0), (BYBIT, BINANCE))
        m.note_fill(BYBIT, 10.0, 100.0, inv)
        thin = SimpleNamespace(ready=True, mid=97.5, levels=lambda side, n: [(97.5, 2.0)])    # equity 30-1000+975=5 < mm 48.75
        out = m.step(1.0, inv, {BYBIT: thin, BINANCE: thin})
        self.assertEqual(len(out), 1)
        self.assertTrue(m.events[-1]["adl_exposure"])
        self.assertEqual(m.stats["adl_exposure_events"], 1)

    def test_reserved_margin_for_resting_orders_blocks_a_signal_the_bare_position_would_allow(self):
        inv = InventoryBook()
        books = self._books()
        sig = SimpleNamespace(buy_venue=BYBIT, sell_venue=BINANCE, qty=1.0)
        m = MarginModel(_margin("cross", capital=15.0), (BYBIT, BINANCE))                      # IM for 1 @ 100 = 10 < 15: allowed
        self.assertTrue(m.allows(sig, inv, books))
        self.assertFalse(m.allows(sig, inv, books, reserved={BYBIT: 100.0}))                   # a resting 1 @ 100 already reserves 10 -> 20 > 15
        self.assertEqual(m.stats["signals_blocked_by_reserved_margin"], 1)
        off = MarginModel(_margin("cross", capital=15.0, reserve=False), (BYBIT, BINANCE))
        self.assertTrue(off.allows(sig, inv, books, reserved={BYBIT: 100.0}))                  # legacy: resting orders reserve nothing
        with self.assertRaises(ValueError):
            VenueMargin(10.0, (MarginTier(1e6, 0.1, 0.05),), mode="hedged")

    def test_backtester_passes_resting_notional_to_the_gate(self):
        h = Harness()
        h.book(0.5, BYBIT); h.book(0.5, BINANCE)
        h.send_maker(1.0)
        h.advance(1.1)
        self.assertEqual(h.bt._reserved_notional(), {BYBIT: 200.0})


class FundingContractTests(unittest.TestCase):
    def test_multiplier_scales_the_payment_and_inverse_contracts_are_refused(self):
        import json, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            Path(d, "f.jsonl").write_text(json.dumps({"venue": "bybit", "funding_ts_ns": 10 ** 12, "rate": 0.0001, "mark_price": 100.0}))
            inv = InventoryBook()
            inv.apply("bybit", 2.0, -200.0)
            s = FundingSettler(d, ("bybit",), contract_multiplier=10.0)
            s.settle(2_000.0, inv, {})
            self.assertAlmostEqual(s.funding_paid, 2.0 * 10.0 * 100.0 * 0.0001, places=12)
            rep = s.report()
            self.assertEqual((rep["contract_multiplier"], rep["settlement_currency"], rep["mark_sources"]["record"]), (10.0, "USDT", 1))
            with self.assertRaises(ValueError):
                FundingSettler(d, ("bybit",), settlement_currency="BTC")


if __name__ == "__main__":
    unittest.main()
