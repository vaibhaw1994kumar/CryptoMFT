"""Accounting-only core shadow (new_backtest/core_shadow.py, ArbBacktestConfig.core_shadow). Synthetic fills: wiring only.
The shadow must (1) agree with the backtester when both book the same fills at the same rates, (2) report a classified difference for a wrong fee and a dropped fill, (3) carry the
same assumed rates as the backtester's headline presets (core config against FEE_PRESETS), and (4) leave a run untouched when it is off."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import crypto_stat_arb
from new_backtest.arb_strategy import InventoryBook
from new_backtest.config import FEE_PRESETS
from new_backtest.core_shadow import CoreShadow
from new_backtest.fill_engine import Fill

VENUES = ("bybit", "binance")


def fill(side, venue, qty, price, taker, fee_bps, ts=1.0, sid=1):
    notional = qty * price
    return Fill(ts=ts, venue=venue, side=side, qty=qty, price=price, fee=notional * fee_bps / 1e4, is_taker=taker, order_id=sid, signal_id=sid)


def book(shadow, inv, f):
    inv.apply(f.venue, f.signed_qty, f.cash_delta)
    shadow.on_fill(f, inv)


class Agree(unittest.TestCase):
    def test_same_fills_at_the_headline_rates_agree_and_the_only_difference_left_is_the_valuation(self):
        sh, inv = CoreShadow("SOLUSDT", VENUES), InventoryBook()
        inv.ensure(VENUES)
        book(sh, inv, fill("buy", "bybit", 1.0, 100.0, False, FEE_PRESETS["bybit_perp_mm"].maker_bps))              # maker rebate -0.5 bps
        book(sh, inv, fill("sell", "binance", 1.0, 100.1, True, FEE_PRESETS["binance_perp_vip0"].taker_bps))
        r = sh.finish(inv, {"bybit": 100.0, "binance": 100.1}, bt_ending_equity=sum(inv.cash.values()) + inv.base["bybit"] * 100.0 + inv.base["binance"] * 100.1)       # equity at the same marks: no valuation difference
        self.assertEqual(r["fills"], 2)
        self.assertTrue(all(p["ok"] for p in r["positions"].values()))
        self.assertEqual(r["differences"], [])
        self.assertLess(abs(float(r["fees"]["diff"])), 1e-9)
        self.assertEqual(r["mismatches_per_fill"], [])

    def test_the_core_config_carries_the_backtester_headline_rates_for_solusdt(self):
        rows = json.loads((Path(crypto_stat_arb.__file__).parent / "config" / "perp_research.json").read_text())["fees"]
        sol = {(r["venue"], r["instrument_id"]): (float(r["maker_fee_bps"]), float(r["taker_fee_bps"])) for r in rows if r["instrument_id"] == "SOL-USDT-PERP"}
        self.assertEqual(sol[("BYBIT", "SOL-USDT-PERP")], (FEE_PRESETS["bybit_perp_mm"].maker_bps, FEE_PRESETS["bybit_perp_mm"].taker_bps))
        self.assertEqual(sol[("BINANCE", "SOL-USDT-PERP")], (FEE_PRESETS["binance_perp_vip0"].maker_bps, FEE_PRESETS["binance_perp_vip0"].taker_bps))
        self.assertTrue(all(r.get("fee_source") == "ASSUMED" for r in rows if r["instrument_id"] == "SOL-USDT-PERP"))      # every rate stays ASSUMED until the owner gives the account's own


class Mutations(unittest.TestCase):
    def test_a_wrong_fee_is_reported_as_a_fee_difference(self):
        sh, inv = CoreShadow("SOLUSDT", VENUES), InventoryBook()
        inv.ensure(VENUES)
        book(sh, inv, fill("buy", "bybit", 1.0, 100.0, False, 2.0))                                                    # the backtester booked +2.0 bps, the core rate is -0.5
        r = sh.finish(inv, {"bybit": 100.0}, bt_ending_equity=None)
        self.assertEqual([d["id"] for d in r["differences"]], ["C1_FEE_RATE"])
        self.assertEqual(r["differences"][0]["class"], "BY_DESIGN")
        self.assertAlmostEqual(float(r["fees"]["diff"]), -0.025, places=9)

    def test_a_dropped_fill_is_a_position_mismatch(self):
        sh, inv = CoreShadow("SOLUSDT", VENUES), InventoryBook()
        inv.ensure(VENUES)
        book(sh, inv, fill("buy", "bybit", 1.0, 100.0, False, -0.5))
        inv.apply("binance", -1.0, 100.1)                                                                              # the backtester booked a hedge the shadow never saw
        r = sh.finish(inv, {"bybit": 100.0, "binance": 100.1}, bt_ending_equity=None)
        self.assertFalse(r["positions"]["binance"]["ok"])
        self.assertIn("POSITION", [d["id"] for d in r["differences"]])

    def test_a_missing_fee_rule_is_a_core_bug_not_a_zero_fee(self):
        sh, inv = CoreShadow("SOLUSDT", VENUES, fee_rows=[]), InventoryBook()
        inv.ensure(VENUES)
        book(sh, inv, fill("buy", "bybit", 1.0, 100.0, False, -0.5))
        self.assertEqual([d["class"] for d in sh.finish(inv, {}, None)["differences"]][:1], ["BUG_CORE"])


class OffByDefault(unittest.TestCase):
    def test_the_switch_defaults_to_off_and_the_backtester_builds_no_shadow(self):
        from test_maker_order_lifetime import Harness
        h = Harness()
        self.assertIsNone(getattr(h.bt, "_shadow", "missing"))
        self.assertFalse(h.bt.config.core_shadow)


class CommonTimestampMarks(unittest.TestCase):
    """B2.1 (7 Nov): the core's own common_timestamp_valuation, applied to the core ledger, resolves A2; without it the per-venue last-mid shortcut still differs."""
    def _shadow(self):
        sh, inv = CoreShadow("SOLUSDT", VENUES), InventoryBook()
        inv.ensure(VENUES)
        book(sh, inv, fill("buy", "bybit", 1.0, 100.0, False, FEE_PRESETS["bybit_perp_mm"].maker_bps))
        return sh, inv

    def test_the_per_venue_last_mid_differs_and_the_core_function_marks_resolve_it(self):
        sh, inv = self._shadow()
        bt_eq = sum(inv.cash.values()) + inv.base["bybit"] * 99.0                                   # the backtester marks the long at the common-timestamp mid 99.0
        r0 = sh.finish(inv, {"bybit": 100.0, "binance": 100.1}, bt_ending_equity=bt_eq)
        self.assertIn("A2_VALUATION", [d["id"] for d in r0["differences"]])
        sh2, inv2 = self._shadow()
        val = {"available": True, "common_ts": 5.0, "marks": {"bybit": 99.0, "binance": 99.1}}
        r1 = sh2.finish(inv2, {"bybit": 100.0, "binance": 100.1}, bt_ending_equity=bt_eq, valuation=val)
        self.assertNotIn("A2_VALUATION", [d["id"] for d in r1["differences"]])
        self.assertIn("RESOLVED", r1["equity"]["a2"])

    def test_a_wrong_backtester_equity_is_still_reported_with_the_core_marks(self):
        sh, inv = self._shadow()
        val = {"available": True, "common_ts": 5.0, "marks": {"bybit": 99.0, "binance": 99.1}}
        r = sh.finish(inv, {"bybit": 100.0, "binance": 100.1}, bt_ending_equity=sum(inv.cash.values()) + inv.base["bybit"] * 98.0, valuation=val)
        self.assertIn("A2_VALUATION", [d["id"] for d in r["differences"]])

    def test_an_unavailable_valuation_changes_nothing(self):
        sh, inv = self._shadow()
        r = sh.finish(inv, {"bybit": 100.0, "binance": 100.1}, bt_ending_equity=sum(inv.cash.values()) + inv.base["bybit"] * 99.0, valuation={"available": False, "reason": "no_price_on_venue"})
        self.assertIn("A2_VALUATION", [d["id"] for d in r["differences"]])


class FundingShadow(unittest.TestCase):
    """B2.2 (7 Nov): the core FundingEngine, fed the same rate files and fills, against the backtester's funding_settler. Synthetic data: wiring only."""
    def _setup(self, tmp):
        import tempfile
        d = Path(tmp)
        rows = {"bybit": [{"funding_ts_ms": 10_000, "venue": "bybit", "rate": "0.0001", "instrument_id": "SOL-USDT-PERP"}],
                "binance": [{"funding_ts_ms": 10_000, "venue": "binance", "rate": "0.0002", "instrument_id": "SOL-USDT-PERP"}]}
        for v, r in rows.items():
            (d / f"{v}.jsonl").write_text("\n".join(json.dumps(x) for x in r))
        sh, inv = CoreShadow("SOLUSDT", VENUES, funding_rate_dir=str(d), start_date="1970-01-01", end_date="1970-01-01"), InventoryBook()
        inv.ensure(VENUES)
        book(sh, inv, fill("buy", "bybit", 2.0, 100.0, False, 0.0, ts=1.0))
        book(sh, inv, fill("sell", "binance", 2.0, 100.0, True, 0.0, ts=1.0, sid=2))
        for t in (2.0, 9.0, 11.0):
            sh.on_mid("bybit", t, 100.0)
            sh.on_mid("binance", t, 100.0)
        return sh, inv

    def _bt(self, sign=1.0):
        # the backtester pays position x mark x rate: long 2 on bybit pays 2 x 100 x 0.0001 = 0.02; short 2 on binance receives 0.04
        return [{"venue": "bybit", "ts": 10.0, "position_base": 2.0, "mark": 100.0, "mark_source": "book_mid", "rate": 0.0001, "payment": sign * 0.02},
                {"venue": "binance", "ts": 10.0, "position_base": -2.0, "mark": 100.0, "mark_source": "book_mid", "rate": 0.0002, "payment": sign * -0.04}]

    def test_the_core_engine_and_the_settler_agree_on_sign_position_and_amount(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sh, inv = self._setup(tmp)
            inv.apply("bybit", 0.0, -0.02)
            inv.apply("binance", 0.0, 0.04)
            r = sh.finish(inv, {"bybit": 100.0, "binance": 100.0}, None, funding_paid={"bybit": 0.02, "binance": -0.04}, funding_settlements=self._bt())
            f = r["funding"]
            self.assertEqual(f["settlements_compared"], 2)
            self.assertLess(abs(float(f["max_abs_payment_diff"])), 1e-12)
            self.assertEqual(f["unmatched_backtester"], [])
            self.assertEqual(f["core_only"], [])
            self.assertLess(abs(float(f["by_venue"]["bybit"]["diff"])), 1e-12)

    def test_a_wrong_sign_in_the_backtester_is_reported(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sh, inv = self._setup(tmp)
            r = sh.finish(inv, {"bybit": 100.0, "binance": 100.0}, None, funding_paid={"bybit": -0.02, "binance": 0.04}, funding_settlements=self._bt(sign=-1.0))
            self.assertGreater(float(r["funding"]["max_abs_payment_diff"]), 0.03)


if __name__ == "__main__":
    unittest.main()
