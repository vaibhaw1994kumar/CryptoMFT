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


class _Book:
    def __init__(self, mid):
        self.mid, self.ready = mid, True

    def levels(self, side, n):
        return [(self.mid, 1e9)]


class MarginShadow(unittest.TestCase):
    """B2.3 (7 Nov): the core RiskEngine.evaluate_margin against the backtester's MarginModel at the same check, same fills. Synthetic data: wiring only."""
    def _cfg(self, capital=1000.0, last=1e6, init=0.02, maint=0.01, above="breach"):
        from new_backtest.margin_model import MarginModelConfig, MarginTier, VenueMargin
        vm = VenueMargin(initial_capital=capital, tiers=(MarginTier(last, init, maint),))
        return MarginModelConfig(venues={"bybit": vm, "binance": vm}, above_last_tier=above)

    def _run(self, cfg, fee_bps=(0.0, 0.0), qty=1.0, mark=100.0, funding_dir=None, now=100.0):
        from new_backtest.margin_model import MarginModel
        sh = CoreShadow("SOLUSDT", VENUES, margin=cfg, margin_flat_every=1, **({"funding_rate_dir": funding_dir, "start_date": "1970-01-01", "end_date": "1970-01-01"} if funding_dir else {}))
        inv, model = InventoryBook(), MarginModel(cfg, VENUES)
        inv.ensure(VENUES)
        book(sh, inv, fill("buy", "bybit", qty, 100.0, False, fee_bps[0], ts=1.0))
        book(sh, inv, fill("sell", "binance", qty, 100.0, True, fee_bps[1], ts=1.0, sid=2))
        return sh, inv, model, {v: _Book(mark) for v in VENUES}

    def _check(self, sh, inv, model, books, now=100.0):
        model.step(now, inv, books)
        sh.on_margin_check(now, model.last_rows)

    def test_same_fills_same_rates_agree_on_balance_requirements_and_decision(self):
        sh, inv, model, books = self._run(self._cfg(), fee_bps=(FEE_PRESETS["bybit_perp_mm"].maker_bps, FEE_PRESETS["binance_perp_vip0"].taker_bps))
        self._check(sh, inv, model, books)
        r = sh.finish(inv, {"bybit": 100.0, "binance": 100.0}, None)["margin"]
        self.assertEqual((r["evaluated"], r["venue_rows_compared"]), (1, 2))
        self.assertEqual(r["breach"]["disagree"], 0)
        self.assertEqual(r["differences_by_class"], {})
        self.assertLess(float(r["max_balance_gap_after_fee_correction"]), 1e-9)
        self.assertLess(float(r["max_maintenance_diff"]), 1e-9)
        self.assertLess(float(r["max_initial_diff"]), 1e-9)

    def test_equity_below_maintenance_is_a_breach_on_both_sides_at_the_same_check(self):
        sh, inv, model, books = self._run(self._cfg(capital=20.0, init=0.5, maint=0.4))
        self._check(sh, inv, model, books)
        r = sh.finish(inv, {"bybit": 100.0, "binance": 100.0}, None)["margin"]
        self.assertEqual(r["breach"]["backtester_breach_rows"], 2)
        self.assertEqual(r["breach"]["core_breach_rows"], 2)
        self.assertEqual(r["breach"]["disagree"], 0)

    def test_above_the_last_tier_a9_breach_agrees_and_ignore_is_a_by_design_difference(self):
        sh, inv, model, books = self._run(self._cfg(last=50.0, above="breach"))
        self._check(sh, inv, model, books)
        r = sh.finish(inv, {"bybit": 100.0, "binance": 100.0}, None)["margin"]
        self.assertEqual((r["breach"]["backtester_breach_rows"], r["breach"]["core_breach_rows"], r["breach"]["disagree"]), (2, 2, 0))
        sh, inv, model, books = self._run(self._cfg(last=50.0, above="ignore"))
        self._check(sh, inv, model, books)
        res = sh.finish(inv, {"bybit": 100.0, "binance": 100.0}, None)
        self.assertEqual(res["margin"]["breach"]["disagree"], 2)
        self.assertEqual(res["margin"]["differences_by_class"], {"BREACH/BY_DESIGN": 2})
        self.assertEqual([d["class"] for d in res["differences"] if d["id"].startswith("MARGIN")], ["BY_DESIGN"])

    def test_a_fee_difference_is_corrected_in_the_balance_and_flips_a_decision_only_as_c1(self):
        # the backtester books no fee, the core charges 5 bps taker on 100 = 0.05: maintenance 40 against equity 40.02 (backtester) and 39.97 (core)
        sh, inv, model, books = self._run(self._cfg(capital=40.02, init=0.5, maint=0.4), fee_bps=(0.0, 0.0))      # the backtester side sits just above maintenance
        self._check(sh, inv, model, books)
        m = sh.finish(inv, {"bybit": 100.0, "binance": 100.0}, None)["margin"]
        self.assertLess(float(m["max_balance_gap_after_fee_correction"]), 1e-9)     # the core's fee on the same trade is a rate difference, removed from the balance
        self.assertEqual(m["breach"]["disagree"], 1)                               # but the core's own decision still sees its own fee: that is the C1 difference
        self.assertEqual(m["differences_by_class"], {"BREACH/BY_DESIGN": 1})

    def test_a_wrong_backtester_cash_is_an_unclassified_balance_difference(self):
        sh, inv, model, books = self._run(self._cfg())
        inv.apply("bybit", 0.0, -5.0)
        self._check(sh, inv, model, books)
        res = sh.finish(inv, {"bybit": 100.0, "binance": 100.0}, None)
        self.assertEqual(res["margin"]["differences_by_class"].get("BALANCE/UNCLASSIFIED"), 1)
        self.assertFalse(res["ok"])

    def test_the_evaluation_is_read_only(self):
        sh, inv, model, books = self._run(self._cfg())
        before = ({k: v for k, v in sh.ledger.positions.items()}, dict(sh.ledger.cash_deltas), len(sh.ledger.economics.events))
        for t in (100.0, 101.0, 102.0):
            self._check(sh, inv, model, books, now=t)
        self.assertEqual(before, ({k: v for k, v in sh.ledger.positions.items()}, dict(sh.ledger.cash_deltas), len(sh.ledger.economics.events)))

    def test_funding_settled_mid_run_is_in_the_core_balance(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for v, rate in (("bybit", "0.0001"), ("binance", "0.0002")):
                (d / f"{v}.jsonl").write_text(json.dumps({"funding_ts_ms": 10_000, "venue": v, "rate": rate, "instrument_id": "SOL-USDT-PERP"}))
            sh, inv, model, books = self._run(self._cfg(), qty=2.0, funding_dir=str(d))
            for t in (2.0, 9.0, 11.0):
                sh.on_mid("bybit", t, 100.0)
                sh.on_mid("binance", t, 100.0)
            inv.apply("bybit", 0.0, -0.02)                   # what the settler books: long 2 on bybit pays 0.02, short 2 on binance receives 0.04
            inv.apply("binance", 0.0, 0.04)
            self._check(sh, inv, model, books, now=12.0)
            m = sh.finish(inv, {"bybit": 100.0, "binance": 100.0}, None, funding_paid={"bybit": 0.02, "binance": -0.04},
                          funding_settlements=[{"venue": "bybit", "ts": 10.0, "position_base": 2.0, "mark": 100.0, "rate": 0.0001, "payment": 0.02},
                                               {"venue": "binance", "ts": 10.0, "position_base": -2.0, "mark": 100.0, "rate": 0.0002, "payment": -0.04}])["margin"]
            self.assertLess(float(m["max_balance_gap_after_fee_correction"]), 1e-9)
            self.assertEqual(m["differences_by_class"], {})


class FeesFromTheCore(unittest.TestCase):
    """B2 step 4 (7 Nov): with fee_source core every rate a run uses is the core FeeSchedule's booked rate, selected by tier name."""
    def _cfg(self, profile="perp_perp_maker_taker_v1_4", **kw):
        from new_backtest.profiles import apply_profile, load_profile
        return apply_profile(load_profile(profile), dataset="mar2024", data_root="/nonexistent", pair="SOLUSDT", start_date="2024-03-08", end_date="2024-03-08", output_dir="/tmp/x_fee", **kw)

    def test_the_tiers_book_the_rates_the_owner_gave_and_carry_their_labels(self):
        from decimal import Decimal
        from crypto_stat_arb.fees import Liquidity, schedule_for
        s = schedule_for({"bybit": "bybit_perp_vip5", "binance": "binance_perp_vip9"}, "SOL-USDT-PERP")
        got = {(v, l): s.rate_bps(v, "SOL-USDT-PERP", l) for v in ("BYBIT", "BINANCE") for l in Liquidity}
        self.assertEqual(got, {("BYBIT", Liquidity.MAKER): Decimal("1.0"), ("BYBIT", Liquidity.TAKER): Decimal("3.2"), ("BINANCE", Liquidity.MAKER): Decimal("0.0"), ("BINANCE", Liquidity.TAKER): Decimal("1.7")})
        label = s.rule("BYBIT", "SOL-USDT-PERP").effective_bps(Liquidity.MAKER)[1]
        self.assertIn("PUBLIC_SCHEDULE_UNVERIFIED", label)
        self.assertIn("ASSUMED", label)                                  # the ledger classes the fee as ASSUMED, never as simulated
        self.assertEqual(got[("BYBIT", Liquidity.MAKER)] + got[("BINANCE", Liquidity.TAKER)], Decimal("2.7"))

    def test_an_unverified_negative_maker_fee_books_zero_and_the_scenario_rebate_is_credited_on_purpose(self):
        from decimal import Decimal
        from crypto_stat_arb.fees import FeeRule, FeeSource, Liquidity, schedule_for
        rule = FeeRule("BYBIT", Decimal("-0.5"), Decimal("3.2"), source=FeeSource.PUBLIC_SCHEDULE_UNVERIFIED, conservative_unverified=True)
        self.assertEqual(rule.effective_bps(Liquidity.MAKER), (Decimal("0"), "CONSERVATIVE_ASSUMED"))
        mm = schedule_for({"bybit": "bybit_perp_mm", "binance": "binance_perp_vip9"}, "SOL-USDT-PERP")
        self.assertEqual(mm.rate_bps("BYBIT", "SOL-USDT-PERP", Liquidity.MAKER) + mm.rate_bps("BINANCE", "SOL-USDT-PERP", Liquidity.TAKER), Decimal("1.2"))
        self.assertIn("SCENARIO", mm.rule("BYBIT", "SOL-USDT-PERP").tier)
        old = schedule_for({"bybit": "bybit_perp_mm", "binance": "binance_perp_vip0"}, "SOL-USDT-PERP")
        self.assertEqual(old.rate_bps("BYBIT", "SOL-USDT-PERP", Liquidity.MAKER) + old.rate_bps("BINANCE", "SOL-USDT-PERP", Liquidity.TAKER), Decimal("4.5"))

    def test_a_tier_cannot_be_used_on_the_wrong_venue_or_by_a_wrong_name(self):
        from crypto_stat_arb.fees import tier_rows
        with self.assertRaises(ValueError):
            tier_rows({"binance": "bybit_perp_vip5"}, "SOL-USDT-PERP")
        with self.assertRaises(ValueError):
            tier_rows({"bybit": "bybit_perp_vip99"}, "SOL-USDT-PERP")

    def test_v1_4_differs_from_v1_3_only_in_the_fee_switch_and_its_labels(self):
        from new_backtest.profiles import load_profile
        def flat(o, p=""):
            out = {}
            if isinstance(o, dict):
                for k, v in o.items():
                    if not k.startswith("_"):
                        out.update(flat(v, p + k + "."))
            else:
                out[p[:-1]] = o
            return out
        a, b = flat(load_profile("perp_perp_maker_taker_v1_3")), flat(load_profile("perp_perp_maker_taker_v1_4"))
        diff = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
        self.assertEqual(diff, {"name", "description", "fee_source", "fee_presets.bybit", "fee_presets.binance", "decision_status.fees"})

    def test_the_earlier_profiles_keep_their_own_presets(self):
        for name in ("perp_perp_maker_taker_v1_2", "perp_perp_maker_taker_v1_3"):
            self.assertEqual(self._cfg(name).fee_source, "presets", name)
        self.assertEqual(self._cfg().fee_source, "core")

    def test_a_v1_4_run_reads_no_backtester_fee_preset(self):
        from unittest import mock
        import new_backtest.config as C
        from new_backtest.backtester import CrossVenueArbBacktester
        cfg = self._cfg()
        cfg.allow_manifest_gap = "test"
        nonsense = {k: C.FeeSpec(maker_bps=99.0, taker_bps=99.0) for k in C.FEE_PRESETS}
        with mock.patch.dict(C.FEE_PRESETS, nonsense):
            bt = CrossVenueArbBacktester(cfg)
        got = {v: (s.fees.maker_bps, s.fees.taker_bps) for v, s in bt.venue_specs.items()}
        self.assertEqual(got, {"bybit": (1.0, 3.2), "binance": (0.0, 1.7)})
        self.assertEqual([r["venue"] for r in bt.fee_rows], ["BYBIT", "BINANCE"])

    def test_the_identity_tiers_reproduce_todays_presets_exactly(self):
        import new_backtest.config as C
        specs, _ = C.core_fee_specs("SOLUSDT", "bybit_perp_mm", "binance_perp_vip0")
        self.assertEqual((specs["bybit"].maker_bps, specs["bybit"].taker_bps), (C.FEE_PRESETS["bybit_perp_mm"].maker_bps, C.FEE_PRESETS["bybit_perp_mm"].taker_bps))
        self.assertEqual((specs["binance"].maker_bps, specs["binance"].taker_bps), (C.FEE_PRESETS["binance_perp_vip0"].maker_bps, C.FEE_PRESETS["binance_perp_vip0"].taker_bps))

    def test_the_shadow_built_on_the_run_rows_has_no_fee_rate_difference(self):
        from crypto_stat_arb.fees import tier_rows
        rows = tier_rows({"bybit": "bybit_perp_vip5", "binance": "binance_perp_vip9"}, "SOL-USDT-PERP")
        sh, inv = CoreShadow("SOLUSDT", VENUES, fee_rows=rows), InventoryBook()
        inv.ensure(VENUES)
        book(sh, inv, fill("buy", "bybit", 1.0, 100.0, False, 1.0))
        book(sh, inv, fill("sell", "binance", 1.0, 100.1, True, 1.7, sid=2))
        r = sh.finish(inv, {"bybit": 100.0, "binance": 100.1}, bt_ending_equity=sum(inv.cash.values()) + inv.base["bybit"] * 100.0 + inv.base["binance"] * 100.1)
        self.assertLess(abs(float(r["fees"]["diff"])), 1e-12)
        self.assertEqual(r["differences"], [])


if __name__ == "__main__":
    unittest.main()
