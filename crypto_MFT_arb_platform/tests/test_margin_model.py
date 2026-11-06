"""Opt-in margin / liquidation / transfer model (src/new_backtest/margin_model.py) and its wiring into the backtester.

Numbers below are checked by hand against the Harness book (bybit/binance bids 99/98, asks 101/102, size 5): the maker bid
at 100 for 2.0 fills at t=5 (cost 200), the hedge sells 2.0 on binance at 99 (proceeds 198).
"""
from __future__ import annotations

import tempfile
import unittest
import warnings
from pathlib import Path

from new_backtest.arb_strategy import MAKER_TAKER, ArbConfig
from new_backtest.backtester import ArbBacktestConfig, CrossVenueArbBacktester
from new_backtest.fill_engine import MakerConfig
from new_backtest.margin_model import MarginModel, MarginModelConfig, MarginTier, TransferModelConfig, TransferRoute, VenueMargin

from synthetic_days import gen
from test_maker_order_lifetime import BINANCE, BYBIT, Harness

TIERS = (MarginTier(max_notional=1_000.0, initial_rate=0.10, maintenance_rate=0.05),
         MarginTier(max_notional=10_000.0, initial_rate=0.20, maintenance_rate=0.10))


def margin_cfg(bybit_capital=1_000.0, binance_capital=1_000.0, liquidate=True, transfers=None, flatten=True, gate=True, fee_bps=10.0):
    return MarginModelConfig(
        venues={BYBIT: VenueMargin(bybit_capital, TIERS, liquidation_fee_bps=fee_bps), BINANCE: VenueMargin(binance_capital, TIERS, liquidation_fee_bps=fee_bps)},
        check_every_s=1.0, gate_new_signals=gate, liquidate=liquidate, flatten_other_venue=flatten, transfers=transfers)


class ModelUnitTests(unittest.TestCase):
    def test_tier_arithmetic(self):
        vm = VenueMargin(100.0, TIERS)
        self.assertEqual((vm.initial(500.0), vm.maintenance(500.0)), (50.0, 25.0))
        self.assertEqual((vm.initial(5_000.0), vm.maintenance(5_000.0)), (1_000.0, 500.0))
        self.assertIsNone(vm.initial(20_000.0))                                             # above the last tier: not accepted

    def test_validation(self):
        bad = [dict(venues={BYBIT: VenueMargin(0.0, TIERS), BINANCE: VenueMargin(1.0, TIERS)}),
               dict(venues={BYBIT: VenueMargin(1.0, (MarginTier(10.0, 0.05, 0.10),)), BINANCE: VenueMargin(1.0, TIERS)}),   # maintenance > initial
               dict(venues={BYBIT: VenueMargin(1.0, (MarginTier(10.0, 0.1, 0.05), MarginTier(5.0, 0.1, 0.05))), BINANCE: VenueMargin(1.0, TIERS)}),
               dict(venues={BYBIT: VenueMargin(1.0, TIERS)}),
               dict(venues={BYBIT: VenueMargin(1.0, TIERS), BINANCE: VenueMargin(1.0, TIERS)}, check_every_s=0.0)]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                MarginModel(MarginModelConfig(**kw), (BYBIT, BINANCE))
        with self.assertRaises(ValueError):
            MarginModel(margin_cfg(transfers=TransferModelConfig(routes=(TransferRoute(BINANCE, BYBIT, delay_s=0.0),), low_watermark=1.0, target=2.0)), (BYBIT, BINANCE))
        with self.assertRaises(ValueError):
            MarginModel(margin_cfg(transfers=TransferModelConfig(routes=(TransferRoute(BINANCE, BYBIT, delay_s=5.0),), low_watermark=5.0, target=2.0)), (BYBIT, BINANCE))


class AboveLastTier(unittest.TestCase):
    """A9 (Phase B): a position above the venue's last tier. 'ignore' (v1, v1.1) never liquidates it; 'breach' (v1_2, the core rule POSITION_ABOVE_RISK_LIMIT) treats it as a breach."""
    def _step(self, mode):
        cfg = margin_cfg(bybit_capital=100_000.0, binance_capital=100_000.0)                  # ample equity: only the tier rule can breach
        cfg = MarginModelConfig(venues=cfg.venues, check_every_s=1.0, gate_new_signals=False, above_last_tier=mode)
        h = Harness(cfg_kw={"margin": cfg})
        h.book(1.0, BYBIT)
        h.book(1.0, BINANCE)
        h.bt.inventory.base[BYBIT] = 120.0                                                    # 120 x 100 mid = 12,000 > the last tier (10,000)
        return h.bt.margin_model, h.bt.margin_model.step(10.0, h.bt.inventory, h.bt.books), h

    def test_ignore_never_liquidates_and_breach_does(self):
        m, out, _ = self._step("ignore")
        self.assertEqual(out, [])
        self.assertNotIn("above_last_tier_breaches", m.stats)
        m, out, _ = self._step("breach")
        self.assertEqual([(v, side, qty) for v, side, qty, _ in out][0], (BYBIT, "sell", 120.0))
        self.assertEqual(m.stats["above_last_tier_breaches"], 1)
        self.assertEqual(m.events[-1]["type"], "liquidation")

    def test_a_position_inside_the_tiers_is_unaffected_by_the_setting(self):
        for mode in ("ignore", "breach"):
            cfg = margin_cfg(bybit_capital=100_000.0, binance_capital=100_000.0)
            h = Harness(cfg_kw={"margin": MarginModelConfig(venues=cfg.venues, check_every_s=1.0, gate_new_signals=False, above_last_tier=mode)})
            h.book(1.0, BYBIT)
            h.book(1.0, BINANCE)
            h.bt.inventory.base[BYBIT] = 20.0                                                 # 2,000: inside tier 2
            self.assertEqual(h.bt.margin_model.step(10.0, h.bt.inventory, h.bt.books), [])

    def test_the_setting_is_validated(self):
        with self.assertRaises(ValueError):
            MarginModelConfig(venues=margin_cfg().venues, above_last_tier="liquidate")


class GateTests(unittest.TestCase):
    def _sig(self, h, qty=2.0):
        from new_backtest.arb_strategy import ArbLeg, ArbSignal
        leg = ArbLeg(BYBIT, "buy", qty, 100.0, False, 100.0, 0.0)
        return ArbSignal(1, 1.0, BYBIT, BINANCE, qty, 5.0, 1.0, 4.0, [leg], MAKER_TAKER, 100.0, 101.0)

    def test_clip_is_allowed_when_both_venues_can_post_initial_margin_and_blocked_otherwise(self):
        h = Harness(cfg_kw={"margin": margin_cfg(bybit_capital=100.0, binance_capital=100.0)})
        h.book(1.0, BYBIT)
        h.book(1.0, BINANCE)
        m = h.bt.margin_model
        self.assertTrue(m.allows(self._sig(h, 2.0), h.bt.inventory, h.bt.books))            # 2 x 100 mid = 200 notional -> IM 20 <= 100
        self.assertFalse(m.allows(self._sig(h, 12.0), h.bt.inventory, h.bt.books))          # 1,200 notional -> tier 2, IM 240 > 100
        self.assertEqual(m.stats["signals_blocked_by_margin"], 1)
        h2 = Harness(cfg_kw={"margin": margin_cfg(bybit_capital=15.0, binance_capital=100.0)})
        h2.book(1.0, BYBIT)
        h2.book(1.0, BINANCE)
        self.assertFalse(h2.bt.margin_model.allows(self._sig(h2, 2.0), h2.bt.inventory, h2.bt.books))   # bybit: IM 20 > 15 equity

    def test_gate_can_be_switched_off(self):
        h = Harness(cfg_kw={"margin": margin_cfg(bybit_capital=1.0, binance_capital=1.0, gate=False)})
        h.book(1.0, BYBIT)
        h.book(1.0, BINANCE)
        self.assertTrue(h.bt.margin_model.allows(self._sig(h), h.bt.inventory, h.bt.books))


def hedged_position(h: Harness) -> None:
    """Maker bid fills at t=5 (bybit +2 @100), hedge sells 2 @99 on binance (lands at 5.003)."""
    h.book(1.0, BYBIT)
    h.book(1.0, BINANCE)
    h.send_maker(1.0)
    h.sell_trade(5.0, BYBIT, volume=2.5)
    h.advance(6.0)


class LiquidationTests(unittest.TestCase):
    def test_equity_accounting_per_venue(self):
        h = Harness(cfg_kw={"margin": margin_cfg(bybit_capital=30.0, binance_capital=1_000.0)})
        hedged_position(h)
        m = h.bt.margin_model
        self.assertEqual(h.bt.inventory.base, {BYBIT: 2.0, BINANCE: -2.0})
        eq_bybit = m.equity(BYBIT, h.bt.inventory, h.bt.books)
        self.assertAlmostEqual(eq_bybit, 30.0 + h.bt.inventory.cash[BYBIT] + 2.0 * 100.0, places=9)   # mid 100 on bybit
        self.assertAlmostEqual(m.notional(BINANCE, h.bt.inventory, h.bt.books), 200.0, places=9)

    def test_breach_liquidates_at_market_charges_the_fee_and_flattens_the_other_venue(self):
        h = Harness(cfg_kw={"margin": margin_cfg(bybit_capital=30.0, binance_capital=1_000.0, fee_bps=10.0)})
        hedged_position(h)
        self.assertEqual(h.bt.margin_model.stats["liquidations"], 0)
        # bybit mid drops to 80.5: equity = 30 + (-200 - fees) + 2 x 80.5 < 0 < maintenance 8.05
        h.book(10.0, BYBIT, bids=((80.0, 5.0), (79.0, 5.0)), asks=((81.0, 5.0), (82.0, 5.0)))
        h.bt._margin_step(10.0)
        m = h.bt.margin_model
        self.assertEqual((m.stats["liquidations"], m.stats["flattened_after_liquidation"]), (1, 1))
        rows = [r for r in h.bt.trade_log if r["ts"] == 10.0]
        self.assertEqual([(r["venue"], r["side"], r["qty"], r["price"]) for r in rows], [(BYBIT, "sell", 2.0, 80.0), (BINANCE, "buy", 2.0, 101.0)])
        self.assertAlmostEqual(m.stats["liquidation_fees"], 2.0 * 80.0 * 10.0 / 10_000.0, places=9)
        self.assertEqual(h.bt.inventory.base, {BYBIT: 0.0, BINANCE: 0.0})
        self.assertEqual(m.events[0]["type"], "liquidation")
        h.assert_ledger_reconciles(self)
        rep = m.report(h.bt.inventory, h.bt.books)
        self.assertLess(rep["min_margin_ratio"], 1.0)
        self.assertEqual(rep["liquidated_qty"], 2.0)

    def test_no_breach_no_liquidation_and_the_step_is_rate_limited(self):
        h = Harness(cfg_kw={"margin": margin_cfg(bybit_capital=1_000.0, binance_capital=1_000.0)})
        hedged_position(h)
        h.bt._margin_step(10.0)
        h.bt._margin_step(10.5)                                                               # inside check_every_s: not counted
        m = h.bt.margin_model
        self.assertEqual((m.stats["liquidations"], m.stats["checks"]), (0, 1))
        self.assertGreater(m.stats["min_margin_ratio"], 1.0)
        self.assertAlmostEqual(m.stats["peak_initial_margin_usage"], 20.0 / m.equity(BINANCE, h.bt.inventory, h.bt.books), places=6)

    def test_liquidate_off_only_records_the_breach(self):
        h = Harness(cfg_kw={"margin": margin_cfg(bybit_capital=30.0, binance_capital=1_000.0, liquidate=False)})
        hedged_position(h)
        h.book(10.0, BYBIT, bids=((80.0, 5.0),), asks=((81.0, 5.0),))
        h.bt._margin_step(10.0)
        self.assertEqual(h.bt.margin_model.stats["liquidations"], 0)
        self.assertLess(h.bt.margin_model.stats["min_margin_ratio"], 1.0)
        self.assertEqual(h.bt.inventory.base[BYBIT], 2.0)


class TransferTests(unittest.TestCase):
    def test_a_venue_below_the_watermark_is_topped_up_after_the_route_delay(self):
        tr = TransferModelConfig(routes=(TransferRoute(BINANCE, BYBIT, delay_s=30.0, fee=1.0, min_quantity=5.0),), low_watermark=50.0, target=100.0)
        h = Harness(cfg_kw={"margin": margin_cfg(bybit_capital=60.0, binance_capital=1_000.0, liquidate=False, transfers=tr)})
        hedged_position(h)
        m = h.bt.margin_model
        free_before = m.free_equity(BYBIT, h.bt.inventory, h.bt.books)
        self.assertLess(free_before, 50.0)                                                    # 60 - 200 - fees + 200 - IM 20 = ~38
        h.bt._margin_step(10.0)
        self.assertEqual((m.stats["transfers_initiated"], m.stats["transfers_completed"], len(m.pending)), (1, 0, 1))
        amount = m.pending[0].amount
        self.assertAlmostEqual(amount, 100.0 - free_before, places=6)
        self.assertAlmostEqual(m.transfer_cash[BINANCE], -(amount + 1.0), places=9)         # left the source at once, with the fee
        self.assertEqual(m.transfer_cash[BYBIT], 0.0)                                         # not arrived yet
        h.bt._margin_step(20.0)
        self.assertEqual(m.stats["transfers_completed"], 0)                                   # still in transit; no second request on the same route
        self.assertEqual(m.stats["transfers_initiated"], 1)
        h.bt._margin_step(41.0)
        self.assertEqual((m.stats["transfers_completed"], len(m.pending)), (1, 0))
        self.assertAlmostEqual(m.transfer_cash[BYBIT], amount, places=9)
        self.assertGreaterEqual(m.free_equity(BYBIT, h.bt.inventory, h.bt.books), 50.0)
        self.assertAlmostEqual(m.stats["seconds_below_watermark"], 31.0, places=6)            # below from t=10 until the t=41 check
        self.assertEqual([e["type"] for e in m.events], ["transfer_initiated", "transfer_confirmed"])
        self.assertAlmostEqual(sum(m.transfer_cash.values()), -1.0, places=9)                 # conservation: only the fee leaves the system

    def test_no_transfer_when_the_source_has_no_spare_or_below_minimum(self):
        tr = TransferModelConfig(routes=(TransferRoute(BINANCE, BYBIT, delay_s=30.0, min_quantity=5_000.0),), low_watermark=50.0, target=100.0)
        h = Harness(cfg_kw={"margin": margin_cfg(bybit_capital=60.0, binance_capital=1_000.0, liquidate=False, transfers=tr)})
        hedged_position(h)
        h.bt._margin_step(10.0)
        self.assertEqual(h.bt.margin_model.stats["transfers_initiated"], 0)


class RunIntegrationTests(unittest.TestCase):
    def test_a_synthetic_run_with_the_model_on_reports_and_a_run_with_it_off_has_no_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            dates = gen(tmp, ndays=1)
            base = dict(data_root=tmp, venues=(BYBIT, BINANCE), pair="BTCUSDT", start_date=dates[0], end_date=dates[0],
                        latency_preset="colocated_both", maker=MakerConfig(min_queue_ahead=0.5),
                        arb=ArbConfig(mode=MAKER_TAKER, maker_max_age_s=10.0, max_position_base=0.01))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                off = CrossVenueArbBacktester(ArbBacktestConfig(**base)).run()
                tiers = (MarginTier(1e9, 0.05, 0.025),)
                on = CrossVenueArbBacktester(ArbBacktestConfig(**base, margin=MarginModelConfig(
                    venues={BYBIT: VenueMargin(10_000.0, tiers), BINANCE: VenueMargin(10_000.0, tiers)}))).run()
            self.assertEqual(off.margin_report, {})
            self.assertTrue(on.margin_report["enabled"])
            self.assertGreater(on.margin_report["checks"], 0)
            self.assertEqual(on.margin_report["liquidations"], 0)
            self.assertEqual((on.signals_generated, on.orders_filled, round(on.realized_pnl, 9)), (off.signals_generated, off.orders_filled, round(off.realized_pnl, 9)))
            self.assertEqual(set(on.margin_report["ending_equity_by_venue"]), {BYBIT, BINANCE})
            self.assertGreater(off.signals_generated, 0, "the synthetic day must produce signals for the gate test below to mean anything")

    def test_a_venue_that_cannot_post_initial_margin_blocks_every_signal_in_the_run_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            dates = gen(tmp, ndays=1)
            tiers = (MarginTier(1e9, 0.05, 0.025),)
            cfg = ArbBacktestConfig(data_root=tmp, venues=(BYBIT, BINANCE), pair="BTCUSDT", start_date=dates[0], end_date=dates[0],
                                    latency_preset="colocated_both", maker=MakerConfig(min_queue_ahead=0.5),
                                    arb=ArbConfig(mode=MAKER_TAKER, maker_max_age_s=10.0, max_position_base=0.01),
                                    margin=MarginModelConfig(venues={BYBIT: VenueMargin(1.0, tiers), BINANCE: VenueMargin(10_000.0, tiers)}))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = CrossVenueArbBacktester(cfg).run()
            self.assertGreater(r.signals_generated, 0)
            self.assertEqual(r.orders_sent, 0)                                                     # bybit has 1 USDT: no clip can post margin
            self.assertEqual(r.margin_report["signals_blocked_by_margin"], r.signals_generated)
            self.assertEqual(r.hedge_report.get("signals_blocked_by_margin", r.margin_report["signals_blocked_by_margin"]), r.signals_generated)


if __name__ == "__main__":
    unittest.main()
