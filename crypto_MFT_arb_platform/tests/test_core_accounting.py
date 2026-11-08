"""Accounting-only core shadow (new_backtest/core_accounting.py, ArbBacktestConfig.core_shadow). Synthetic fills: wiring only.
The shadow must (1) agree with the backtester when both book the same fills at the same rates, (2) report a classified difference for a wrong fee and a dropped fill, (3) carry the
same assumed rates as the backtester's headline presets (core config against FEE_PRESETS), and (4) leave a run untouched when it is off."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import crypto_stat_arb
from new_backtest.arb_strategy import InventoryBook
from new_backtest.config import FEE_PRESETS
from new_backtest.core_accounting import CoreShadow
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
        self.assertEqual(diff, {"name", "description", "fee_source", "funding_source", "margin_source", "ledger_source", "fee_presets.bybit", "fee_presets.binance", "decision_status.loss_fail_bps", "decision_status.p_fail", "decision_status.residual_limit", "decision_status.unwind_band_core", "decision_status.hedge_unknown_escalation", "decision_status.fees"})

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


class FundingFromTheCore(unittest.TestCase):
    """B2 step 5 (7 Nov): with funding_source core the core FundingEngine books the funding and its cash flow is the run's. Synthetic data: wiring only."""
    def _setup(self, tmp, mark=None, mids=(2.0, 9.0, 11.0)):
        d = Path(tmp)
        for v, rate in (("bybit", "0.0001"), ("binance", "0.0002")):
            row = {"funding_ts_ms": 10_000, "venue": v, "rate": rate, "instrument_id": "SOL-USDT-PERP"}
            if mark is not None:
                row["mark_price"] = str(mark)
            (d / f"{v}.jsonl").write_text(json.dumps(row))
        sh, inv = CoreShadow("SOLUSDT", VENUES, funding_rate_dir=str(d), start_date="1970-01-01", end_date="1970-01-01"), InventoryBook()
        inv.ensure(VENUES)
        book(sh, inv, fill("buy", "bybit", 2.0, 100.0, False, 0.0, ts=1.0))
        book(sh, inv, fill("sell", "binance", 2.0, 100.0, True, 0.0, ts=1.0, sid=2))
        for t in mids:
            sh.on_mid("bybit", t, 100.0)
            sh.on_mid("binance", t, 100.0)
        return sh, inv

    def test_the_core_books_the_settlement_into_the_runs_cash_and_flags_the_book_mid_mark(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sh, inv = self._setup(tmp)
            st = sh.funding_settler()
            cash0 = dict(inv.cash)
            st.settle(11.0, inv, None)
            self.assertAlmostEqual(inv.cash["bybit"] - cash0["bybit"], -0.02, places=12)          # long 2 on bybit pays 2 x 100 x 0.0001
            self.assertAlmostEqual(inv.cash["binance"] - cash0["binance"], 0.04, places=12)       # short 2 on binance receives 2 x 100 x 0.0002
            r = st.report()
            self.assertAlmostEqual(r["funding_paid"], -0.02, places=12)
            self.assertEqual(r["mark_sources"], {"record": 0, "book_mid": 2, "none": 0})
            self.assertEqual(r["FUNDING_MARK_FALLBACK"], 2)                                         # flagged and counted
            self.assertEqual(r["core_status"]["mark_basis"], "ESTIMATED")
            self.assertEqual(r["core_status"]["economic_status"], "COMPLETE")                       # a fallback is booked, not missing

    def test_a_row_with_odd_milliseconds_is_settled_when_the_timer_calls_at_exactly_its_time(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for v, rate, ms in (("bybit", "0.0001", 10_000), ("binance", "0.0002", 10_014)):
                (d / f"{v}.jsonl").write_text(json.dumps({"funding_ts_ms": ms, "venue": v, "rate": rate, "instrument_id": "SOL-USDT-PERP"}))
            sh, inv = CoreShadow("SOLUSDT", VENUES, funding_rate_dir=str(d), start_date="1970-01-01", end_date="1970-01-01"), InventoryBook()
            inv.ensure(VENUES)
            book(sh, inv, fill("buy", "bybit", 2.0, 100.0, False, 0.0, ts=1.0))
            book(sh, inv, fill("sell", "binance", 2.0, 100.0, True, 0.0, ts=1.0, sid=2))
            for t in (2.0, 9.0, 10.0, 10.014):
                sh.on_mid("bybit", t, 100.0)
                sh.on_mid("binance", t, 100.0)
            st = sh.funding_settler()
            st.settle(10.0, inv, None)
            st.settle(10.014, inv, None)                  # the timer wheel calls at exactly the row's time, the next event is later: nothing may be left unsettled
            st.settle(11.0, inv, None)
            self.assertEqual(sorted(r["venue"] for r in st.report()["settlements"]), ["binance", "bybit"])

    def test_a_boundary_with_no_rate_record_is_flagged_during_the_run_not_dropped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sh, inv = self._setup(tmp)                                                              # rate rows at 10 s; the 8-hour boundary at 28,800 s has none
            for t in (20_000.0, 29_000.0):
                sh.on_mid("bybit", t, 100.0)
                sh.on_mid("binance", t, 100.0)
            st = sh.funding_settler()
            st.settle(11.0, inv, None)
            cash = dict(inv.cash)
            st.settle(29_000.0, inv, None)
            self.assertEqual(inv.cash, cash)                                                        # nothing to book ...
            flags = [x for x in st.report()["settlements"] if x.get("flag") == "FUNDING_RATE_MISSING"]
            self.assertEqual(sorted(x["venue"] for x in flags), ["binance", "bybit"])               # ... and the run says so, per venue
            self.assertEqual(st.report()["core_status"]["economic_status"], "INCOMPLETE")

    def test_a_settlement_is_booked_once_however_often_the_settler_is_called(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sh, inv = self._setup(tmp)
            st = sh.funding_settler()
            st.settle(11.0, inv, None)
            cash = dict(inv.cash)
            st.settle(12.0, inv, None)
            st.settle(50.0, inv, None)
            self.assertEqual(inv.cash, cash)
            self.assertEqual(len(st.report()["settlements"]), 2)

    def test_the_records_own_mark_is_used_without_a_fallback_flag(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sh, inv = self._setup(tmp, mark=101.0)
            st = sh.funding_settler()
            cash0 = inv.cash["bybit"]
            st.settle(11.0, inv, None)
            self.assertAlmostEqual(inv.cash["bybit"] - cash0, -2.0 * 101.0 * 0.0001, places=12)
            r = st.report()
            self.assertEqual((r["mark_sources"], r["FUNDING_MARK_FALLBACK"]), ({"record": 2, "book_mid": 0, "none": 0}, 0))
            self.assertEqual(r["core_status"]["mark_basis"], "EXCHANGE_MARK")

    def test_a_settlement_with_no_mark_at_all_is_flagged_and_never_dropped_silently(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sh, inv = self._setup(tmp, mids=())
            for t in (2.0, 11.0):                                                                   # the engine saw the clock move but never a price
                for v in ("BYBIT", "BINANCE"):
                    sh.funding.observe(v, "SOL-USDT-PERP", int(t * 1e9), None)
            st = sh.funding_settler()
            cash = dict(inv.cash)
            st.settle(11.0, inv, None)
            self.assertEqual(inv.cash, cash)                                                        # nothing could be booked ...
            r = st.report()
            self.assertEqual(r["mark_sources"]["none"], 2)                                          # ... and the run says so
            self.assertTrue(all(x["flag"] == "FUNDING_MARK_MISSING" for x in r["settlements"]))
            self.assertEqual(r["core_status"]["economic_status"], "INCOMPLETE")

    def test_a_core_funding_run_never_builds_the_backtesters_own_settler(self):
        import tempfile
        from unittest import mock
        import new_backtest.funding_settler as FS
        from new_backtest.backtester import CrossVenueArbBacktester
        from new_backtest.profiles import apply_profile, load_profile
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "bybit.jsonl").write_text("")
            cfg = apply_profile(load_profile("perp_perp_maker_taker_v1_4"), dataset="mar2024", data_root="/nonexistent", pair="SOLUSDT", start_date="2024-03-08", end_date="2024-03-08",
                                output_dir="/tmp/x_fund", funding_rate_dir=tmp)
            cfg.allow_manifest_gap = "test"
            self.assertEqual(cfg.funding_source, "core")
            with mock.patch.object(FS, "FundingSettler", side_effect=AssertionError("funding booked outside the core")):
                bt = CrossVenueArbBacktester(cfg)
            self.assertEqual(type(bt.settler).__name__, "CoreFundingSettler")
        for name in ("perp_perp_maker_taker_v1_2", "perp_perp_maker_taker_v1_3"):
            self.assertEqual(apply_profile(load_profile(name), dataset="mar2024", data_root="/nonexistent", pair="SOLUSDT", start_date="2024-03-08", end_date="2024-03-08", output_dir="/tmp/x_fund").funding_source, "settler")

    def test_without_a_rate_directory_funding_is_not_modelled_exactly_as_with_the_settler(self):
        from new_backtest.backtester import CrossVenueArbBacktester
        from new_backtest.profiles import apply_profile, load_profile
        cfg = apply_profile(load_profile("perp_perp_maker_taker_v1_4"), dataset="mar2024", data_root="/nonexistent", pair="SOLUSDT", start_date="2024-03-08", end_date="2024-03-08", output_dir="/tmp/x_fund")
        cfg.allow_manifest_gap = "test"
        bt = CrossVenueArbBacktester(cfg)
        self.assertIsNone(bt.settler)
        self.assertIs(cfg.release_identity["funding_modelled"], False)                       # recorded: March has no funding file


class MarginFromTheCore(unittest.TestCase):
    """B2 step 6 (7 Nov): the core RiskEngine decides breach and admission; compared with the old MarginModel on the same fills. Synthetic data: wiring only."""
    def _cfg(self, **kw):
        return MarginShadow._cfg(self, **kw)

    def _pair(self, cfg, qty=1.0, price=100.0, fee_bps=0.0):
        from new_backtest.core_accounting import _make_core_margin_model
        from new_backtest.margin_model import MarginModel
        sh, inv = CoreShadow("SOLUSDT", VENUES, margin=cfg), InventoryBook()
        inv.ensure(VENUES)
        book(sh, inv, fill("buy", "bybit", qty, price, False, fee_bps, ts=1.0))
        book(sh, inv, fill("sell", "binance", qty, price, True, fee_bps, ts=1.0, sid=2))
        return MarginModel(cfg, VENUES), _make_core_margin_model()(cfg, VENUES, sh), inv, {v: _Book(price) for v in VENUES}, sh

    def test_a_healthy_book_orders_nothing_and_admits_a_small_clip_on_both(self):
        from types import SimpleNamespace
        old, core, inv, books, _ = self._pair(self._cfg())
        self.assertEqual(old.step(100.0, inv, books), core.step(100.0, inv, books))
        sig = SimpleNamespace(buy_venue="bybit", sell_venue="binance", qty=1.0)
        self.assertEqual((old.allows(sig, inv, books), core.allows(sig, inv, books)), (True, True))

    def test_a_breach_gives_the_same_liquidation_and_flatten_orders(self):
        old, core, inv, books, _ = self._pair(self._cfg(capital=20.0, init=0.5, maint=0.4))
        a, b = old.step(100.0, inv, books), core.step(100.0, inv, books)
        self.assertTrue(a)
        self.assertEqual(a, b)

    def test_the_gate_refuses_what_the_old_gate_refuses_when_equity_cannot_post_the_initial_margin(self):
        from types import SimpleNamespace
        old, core, inv, books, _ = self._pair(self._cfg(capital=60.0, init=0.5, maint=0.1))
        sig = SimpleNamespace(buy_venue="bybit", sell_venue="binance", qty=2.0)          # position 1 -> 3: initial margin 150 against equity 60
        self.assertEqual((old.allows(sig, inv, books), core.allows(sig, inv, books)), (False, False))
        self.assertEqual(core.stats["signals_blocked_by_margin"], 1)

    def test_above_the_last_tier_the_core_fails_closed_whatever_the_old_switch_says(self):
        old, core, inv, books, _ = self._pair(self._cfg(last=50.0, above="ignore"))
        self.assertEqual(old.step(100.0, inv, books), [])                                 # the old model ignores it under v1.1's setting
        self.assertTrue(core.step(100.0, inv, books))                                     # the core: POSITION_ABOVE_RISK_LIMIT, a breach (above_last_tier is redundant)
        old2, core2, inv2, books2, _ = self._pair(self._cfg(last=50.0, above="breach"))
        self.assertEqual(old2.step(100.0, inv2, books2), core2.step(100.0, inv2, books2))

    def test_the_core_gate_counts_exposure_worst_case_so_a_reducing_trade_is_not_netted(self):
        from types import SimpleNamespace
        old, core, inv, books, _ = self._pair(self._cfg(capital=160.0, init=0.5, maint=0.1), qty=3.0)
        sig = SimpleNamespace(buy_venue="binance", sell_venue="bybit", qty=3.0)          # closes both legs
        self.assertTrue(old.allows(sig, inv, books))                                      # old: the position after the trade is 0, initial margin 0
        self.assertFalse(core.allows(sig, inv, books))                                    # core: worst case adds the clip to the exposure (BY_DESIGN, the core rule wins)

    def test_the_old_models_decision_code_is_not_called(self):
        from unittest import mock
        from new_backtest.margin_model import MarginModel
        old, core, inv, books, _ = self._pair(self._cfg(capital=20.0, init=0.5, maint=0.4))
        with mock.patch.object(MarginModel, "_breaches", side_effect=AssertionError("margin decided outside the core")), \
             mock.patch.object(MarginModel, "equity", side_effect=AssertionError("margin decided outside the core")):
            self.assertTrue(core.step(100.0, inv, books))

    def test_what_the_core_path_does_not_model_is_refused_not_ignored(self):
        from new_backtest.core_accounting import _make_core_margin_model
        from new_backtest.margin_model import MarginModelConfig, TransferModelConfig, TransferRoute, VenueMargin
        cfg = self._cfg()
        sh = CoreShadow("SOLUSDT", VENUES, margin=cfg)
        tr = TransferModelConfig(routes=(TransferRoute("bybit", "binance", 10.0),), low_watermark=1.0, target=5.0)
        with self.assertRaises(ValueError):
            _make_core_margin_model()(MarginModelConfig(venues=cfg.venues, transfers=tr), VENUES, sh)
        with self.assertRaises(ValueError):
            _make_core_margin_model()(MarginModelConfig(venues=cfg.venues, max_utilisation=0.5), VENUES, sh)
        iso = {v: VenueMargin(initial_capital=1000.0, tiers=cfg.venues[v].tiers, mode="isolated") for v in VENUES}
        with self.assertRaises(ValueError):
            _make_core_margin_model()(MarginModelConfig(venues=iso), VENUES, sh)

    def test_a_v1_4_backtester_is_built_on_the_core_margin_model(self):
        from new_backtest.backtester import CrossVenueArbBacktester
        from new_backtest.profiles import apply_profile, load_profile
        cfg = apply_profile(load_profile("perp_perp_maker_taker_v1_4"), dataset="mar2024", data_root="/nonexistent", pair="SOLUSDT", start_date="2024-03-08", end_date="2024-03-08", output_dir="/tmp/x_marg")
        cfg.allow_manifest_gap = "test"
        self.assertEqual(type(CrossVenueArbBacktester(cfg).margin_model).__name__, "CoreMarginModel")
        old = apply_profile(load_profile("perp_perp_maker_taker_v1_3"), dataset="mar2024", data_root="/nonexistent", pair="SOLUSDT", start_date="2024-03-08", end_date="2024-03-08", output_dir="/tmp/x_marg")
        old.allow_manifest_gap = "test"
        self.assertEqual(type(CrossVenueArbBacktester(old).margin_model).__name__, "MarginModel")


class LedgerFromTheCore(unittest.TestCase):
    """B2 step 7 (7 Nov): the core's strict bridge, liquidation booking, and the run close on the core ledger. Synthetic data: wiring only."""
    def _booked(self, with_margin=True):
        from crypto_stat_arb.fees import tier_rows
        rows = tier_rows({"bybit": "bybit_perp_vip5", "binance": "binance_perp_vip9"}, "SOL-USDT-PERP")
        sh, inv = CoreShadow("SOLUSDT", VENUES, fee_rows=rows, margin=MarginShadow._cfg(self) if with_margin else None), InventoryBook()
        inv.ensure(VENUES)
        f1, f2 = fill("buy", "bybit", 1.0, 100.0, False, 1.0, ts=1.0), fill("sell", "binance", 1.0, 100.1, True, 1.7, ts=1.0, sid=2)
        book(sh, inv, f1)
        book(sh, inv, f2)
        trades = [{"ts": 1.0, "venue": f.venue, "side": f.side, "qty": f.qty, "price": f.price, "fee": f.fee, "taker": f.is_taker, "kind": None} for f in (f1, f2)]
        return sh, inv, trades

    def _bridge(self, sh, inv, trades, **kw):
        from decimal import Decimal as D
        from crypto_stat_arb.economics import strict_bridge
        marks = {"bybit": D("100.05"), "binance": D("100.05")}
        eq = sum(inv.cash.values()) + sum(inv.base[v] * 100.05 for v in VENUES)
        args = dict(trades=trades, schedule=sh.schedule, instrument_id=sh.instrument, venues=VENUES, marks=marks, reported_equity=D(repr(eq)), inventory_cash=inv.cash, inventory_base=inv.base,
                    traded_notional=D("200.1"))
        args.update(kw)
        return strict_bridge(sh.ledger, **args)

    def test_a_consistent_book_passes_every_check(self):
        sh, inv, trades = self._booked()
        r = self._bridge(sh, inv, trades)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["fee_mismatches"], r["failed"]), ([], []))
        self.assertLess(abs(float(r["equity_gap"])), 1e-12)

    def test_each_old_check_still_fails_when_its_input_is_wrong(self):
        sh, inv, trades = self._booked()
        bad_fee = [dict(trades[0], fee=trades[0]["fee"] + 0.01)] + trades[1:]
        self.assertEqual(len(self._bridge(sh, inv, bad_fee)["fee_mismatches"]), 1)                    # a fee that is not the schedule's
        self.assertFalse(self._bridge(sh, inv, trades[:1])["per_venue"]["binance"]["rebuild_ok"])     # a fill missing from the trade log
        from decimal import Decimal as D
        r = self._bridge(sh, inv, trades, reported_equity=D("5"))
        self.assertFalse(r["ok"])                                                                      # a wrong equity
        self.assertGreater(abs(float(r["equity_gap"])), 1.0)

    def test_the_float_inventory_must_match_the_ledger(self):
        sh, inv, trades = self._booked()
        inv.apply("bybit", 0.001, 0.0)                                                                 # the strategy's view drifted from what is booked
        r = self._bridge(sh, inv, trades)
        self.assertFalse(r["per_venue"]["bybit"]["inventory_ok"])
        self.assertFalse(r["ok"])

    def test_a_truncated_trade_log_skips_the_rebuild_but_not_the_inventory_check(self):
        sh, inv, trades = self._booked()
        r = self._bridge(sh, inv, trades[:1], truncated=True)
        self.assertTrue(r["per_venue"]["binance"]["rebuild_ok"])
        self.assertTrue(r["per_venue"]["binance"]["inventory_ok"])

    def test_a_liquidation_is_booked_as_one_with_the_declared_fee_and_the_bridge_accepts_it(self):
        from crypto_stat_arb.economics import EconomicEventType
        sh, inv, trades = self._booked()
        liq = fill("sell", "bybit", 1.0, 99.0, True, 3.2 + 10.0, ts=2.0, sid=0)                       # taker 3.2 plus the declared 10 bps
        inv.apply(liq.venue, liq.signed_qty, liq.cash_delta)
        sh.on_fill(liq, inv, liquidation_fee_bps=10.0)
        self.assertEqual(sum(1 for e in sh.ledger.economics.events if e.event_type == EconomicEventType.LIQUIDATION), 1)
        trades.append({"ts": 2.0, "venue": "bybit", "side": "sell", "qty": 1.0, "price": 99.0, "fee": liq.fee, "taker": True, "kind": "liquidation"})
        from decimal import Decimal as D
        r = self._bridge(sh, inv, trades, liquidation_fee_bps={"bybit": D("10")})
        self.assertEqual(r["fee_mismatches"], [])
        self.assertLess(abs(float(r["equity_gap"])), 1e-9)

    def test_the_run_close_uses_the_core_and_never_the_old_bridge(self):
        import tempfile
        from types import SimpleNamespace
        from unittest import mock
        from new_backtest.backtester import CrossVenueArbBacktester, EquityBridgeError
        from new_backtest.profiles import apply_profile, load_profile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = apply_profile(load_profile("perp_perp_maker_taker_v1_4"), dataset="mar2024", data_root="/nonexistent", pair="SOLUSDT", start_date="2024-03-08", end_date="2024-03-08", output_dir=tmp)
            cfg.allow_manifest_gap = "test"
            bt = CrossVenueArbBacktester(cfg)
            self.assertEqual(cfg.ledger_source, "core")
            val = {"available": True, "marks": {"bybit": 100.0, "binance": 100.0}, "equity": 0.0, "common_ts": 1.0}
            with mock.patch.object(CrossVenueArbBacktester, "_equity_bridge", side_effect=AssertionError("equity booked outside the core")):
                equity, doc, cash = bt._core_ledger_close(val, 0.0, 0.0, {"bybit": 100.0, "binance": 100.0}, SimpleNamespace())
                self.assertEqual((equity, cash, doc["ok"]), (0.0, 0.0, True))
                self.assertEqual(doc["economics_gate"]["decision"], "PASS")
                self.assertTrue(Path(tmp, "economic_reconciliation.json").exists())
                bt.inventory.apply("bybit", 1.0, -100.0)                                               # the float books move, the core ledger does not
                with self.assertRaises(EquityBridgeError):
                    bt._core_ledger_close(val, 0.0, 0.0, {"bybit": 100.0, "binance": 100.0}, SimpleNamespace())
                bt.config.equity_bridge = "warn"
                with self.assertWarns(RuntimeWarning):
                    bt._core_ledger_close(val, 0.0, 0.0, {"bybit": 100.0, "binance": 100.0}, SimpleNamespace())


    def test_a_float_fed_ledger_passes_the_reconciliation_only_with_a_stated_tolerance(self):
        from decimal import Decimal as D
        from crypto_stat_arb.economics import Valuation, reconcile
        sh, inv = CoreShadow("SOLUSDT", VENUES, margin=MarginShadow._cfg(self)), InventoryBook()
        inv.ensure(VENUES)
        for i, q in enumerate((0.1, 0.2, 0.30000000000000004)):                                       # binary floats: 17-digit quantities
            book(sh, inv, fill("buy", "bybit", q, 151.4575 + i, False, 1.0, ts=1.0 + i, sid=i + 1))
            book(sh, inv, fill("sell", "binance", q, 151.4575 + i, True, 1.7, ts=1.0 + i, sid=i + 11))
        closing = Valuation("USDT", {("BYBIT", sh.instrument): D("152.123456789"), ("BINANCE", sh.instrument): D("152.123456789")})
        loose = reconcile(sh.ledger.economics, opening=Valuation("USDT", {}), closing=closing, identity_tolerance=D("1e-15"))
        self.assertEqual([c["passed"] for c in loose["checks"]], [True, True])
        strict = reconcile(sh.ledger.economics, opening=Valuation("USDT", {}), closing=closing)
        self.assertEqual(all(c["passed"] for c in strict["checks"]), all(abs(D(v["identity_residual"])) == 0 for v in strict["venues"].values()))      # default stays exact: it passes only when the residual is exactly 0

    def test_ledger_core_needs_margin_and_core_funding(self):
        from new_backtest.backtester import CrossVenueArbBacktester
        from new_backtest.profiles import ProfileError, apply_profile, load_profile
        doc = load_profile("perp_perp_maker_taker_v1_4")
        kw = dict(dataset="mar2024", data_root="/nonexistent", pair="SOLUSDT", start_date="2024-03-08", end_date="2024-03-08", output_dir="/tmp/x_led", funding_rate_dir="/tmp")
        with self.assertRaises(ProfileError):                                   # the working profile refuses the old settler at load
            apply_profile(doc, funding_source="settler", **kw)
        cfg = apply_profile(doc, **kw)
        cfg.allow_manifest_gap = "test"
        cfg.funding_source, cfg.release_identity = "settler", None             # a config with no profile name: the backtester's own consistency check
        with self.assertRaises(ValueError):
            CrossVenueArbBacktester(cfg)


class WorkingProfilesUseOnlyTheCore(unittest.TestCase):
    """B2 step 8 (owner 7 Nov 2026): reference profiles keep the old accounting modules, every other profile (v1_4 and later) is refused at startup if it would call one."""

    REFERENCE = ("legacy_v0", "perp_perp_maker_taker_v1", "perp_perp_maker_taker_v1_1", "perp_perp_maker_taker_v1_2", "perp_perp_maker_taker_v1_3", "spot_perp_basis_v1", "spot_spot_maker_taker_v1")
    OLD = {"fee_source": "presets", "funding_source": "settler", "margin_source": "model", "ledger_source": "backtester"}
    KW = dict(dataset="mar2024", data_root="/nonexistent", pair="SOLUSDT", start_date="2024-03-08", end_date="2024-03-08", output_dir="/tmp/x_step8")

    def _doc(self, name="perp_perp_maker_taker_v1_4"):
        from new_backtest.profiles import load_profile
        return load_profile(name)

    def test_the_registry_names_the_seven_reference_profiles_and_v1_1_is_the_control(self):
        from new_backtest.profiles import profile_label, profile_status
        for n in self.REFERENCE:
            self.assertEqual(profile_status(n), "CONTROL" if n == "perp_perp_maker_taker_v1_1" else "REFERENCE_ONLY", n)
        for n in ("perp_perp_maker_taker_v1_4", "perp_perp_maker_taker_v1_5", "anything_new"):
            self.assertEqual(profile_status(n), "WORKING", n)
        self.assertEqual(profile_label("perp_perp_maker_taker_v1_2"), "perp_perp_maker_taker_v1_2 [REFERENCE_ONLY]")
        self.assertEqual(profile_label("perp_perp_maker_taker_v1_1"), "perp_perp_maker_taker_v1_1 [CONTROL]")
        self.assertEqual(profile_label("perp_perp_maker_taker_v1_4"), "perp_perp_maker_taker_v1_4")

    def test_every_profile_file_is_a_registered_reference_or_runs_only_on_the_core(self):
        from new_backtest.profiles import PROFILES_DIR, apply_profile, load_profile, profile_status
        names = sorted(p.stem for p in PROFILES_DIR.glob("*.yaml"))
        self.assertTrue(set(self.REFERENCE) <= set(names), "a reference profile file is missing")
        for n in names:
            cfg = apply_profile(load_profile(n), **self.KW)
            if profile_status(n) == "WORKING":
                self.assertEqual({k: getattr(cfg, k) for k in self.OLD}, {k: "core" for k in self.OLD}, n)
            else:
                self.assertEqual({k: getattr(cfg, k) for k in self.OLD}, self.OLD, f"{n}: a reference profile must reproduce byte for byte, on the old modules")
            self.assertEqual(cfg.release_identity["profile_status"], profile_status(n))

    def test_a_working_profile_is_refused_at_load_for_each_old_module(self):
        from new_backtest.profiles import ProfileError, apply_profile
        for key, old in self.OLD.items():
            with self.assertRaises(ProfileError, msg=key):
                apply_profile(self._doc(), **self.KW, **{key: old})

    def test_a_later_version_is_working_too(self):
        from new_backtest.profiles import ProfileError, apply_profile
        doc = dict(self._doc(), name="perp_perp_maker_taker_v1_5")
        self.assertEqual(apply_profile(doc, **self.KW).fee_source, "core")
        doc["margin_source"] = "model"
        with self.assertRaises(ProfileError):
            apply_profile(doc, **self.KW)

    def test_the_backtester_refuses_at_startup_when_a_working_config_is_edited_after_the_load(self):
        from new_backtest.backtester import CrossVenueArbBacktester
        from new_backtest.profiles import ProfileError, apply_profile
        for key, old in self.OLD.items():
            cfg = apply_profile(self._doc(), **self.KW)
            cfg.allow_manifest_gap = "test"
            setattr(cfg, key, old)
            with self.assertRaises(ProfileError, msg=key):
                CrossVenueArbBacktester(cfg)

    def test_a_reference_profile_still_starts_on_the_old_modules(self):
        from new_backtest.backtester import CrossVenueArbBacktester
        from new_backtest.profiles import apply_profile
        for n in ("perp_perp_maker_taker_v1_3",):                              # (v1.1 / v1_2 need the runner's venue ticks to start; their accounting fields are checked by the file test above)
            cfg = apply_profile(self._doc(n), **self.KW)
            cfg.allow_manifest_gap = "test"
            bt = CrossVenueArbBacktester(cfg)
            self.assertEqual(type(bt.margin_model).__name__, "MarginModel", n)

    def test_a_working_run_books_fees_funding_margin_and_equity_only_in_the_core(self):
        """The structural guard: with every old accounting entry point poisoned, a v1_4 backtester still builds, takes a margin step and closes the run."""
        import tempfile
        from types import SimpleNamespace
        from unittest import mock
        import new_backtest.config as C
        import new_backtest.funding_settler as FS
        from new_backtest.backtester import CrossVenueArbBacktester
        from new_backtest.margin_model import MarginModel
        from new_backtest.profiles import apply_profile
        boom = AssertionError("accounting outside the core")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as rates:
            Path(rates, "bybit.jsonl").write_text("")
            cfg = apply_profile(self._doc(), **{**self.KW, "output_dir": tmp}, funding_rate_dir=rates)
            cfg.allow_manifest_gap = "test"
            poisoned = {k: mock.Mock(side_effect=boom) for k in ("maker_bps", "taker_bps")}
            with mock.patch.object(FS, "FundingSettler", side_effect=boom), \
                 mock.patch.object(MarginModel, "_breaches", side_effect=boom), mock.patch.object(MarginModel, "equity", side_effect=boom), \
                 mock.patch.object(CrossVenueArbBacktester, "_equity_bridge", side_effect=boom), \
                 mock.patch.dict(C.FEE_PRESETS, {k: SimpleNamespace(**{"maker_bps": float("nan"), "taker_bps": float("nan")}) for k in C.FEE_PRESETS}):
                bt = CrossVenueArbBacktester(cfg)
                val = {"available": True, "marks": {"bybit": 100.0, "binance": 100.0}, "equity": 0.0, "common_ts": 1.0}
                equity, doc, cash = bt._core_ledger_close(val, 0.0, 0.0, {"bybit": 100.0, "binance": 100.0}, SimpleNamespace())
            self.assertEqual((equity, doc["ok"], type(bt.settler).__name__, type(bt.margin_model).__name__), (0.0, True, "CoreFundingSettler", "CoreMarginModel"))
            self.assertEqual([(s.fees.maker_bps, s.fees.taker_bps) for s in bt.venue_specs.values()], [(1.0, 3.2), (0.0, 1.7)])


class SessionRuntimeSwitch(unittest.TestCase):
    """Phase D (owner 8 Nov 2026): `runtime` is a profile setting, off by default. v1_5 = v1_4 + `runtime: session` (and its `resync_delay_s`, ASSUMED); the reference profiles refuse it; parity is v1_5 against v1_4. The session path itself is wired in step D5."""

    KW = WorkingProfilesUseOnlyTheCore.KW
    REFERENCE = WorkingProfilesUseOnlyTheCore.REFERENCE

    def test_off_by_default_and_on_only_in_v1_5(self):
        from new_backtest.profiles import PROFILES_DIR, apply_profile, load_profile
        for n in sorted(p.stem for p in PROFILES_DIR.glob("*.yaml")):
            cfg = apply_profile(load_profile(n), **self.KW)
            self.assertEqual(cfg.runtime, "session" if n == "perp_perp_maker_taker_v1_5" else "backtester", n)
            self.assertEqual(cfg.release_identity["runtime"], cfg.runtime)

    def test_v1_5_is_v1_4_plus_the_runtime_and_nothing_else(self):
        from new_backtest.profiles import load_profile
        a, b = load_profile("perp_perp_maker_taker_v1_4"), load_profile("perp_perp_maker_taker_v1_5")
        differ = {k for k in set(a) | set(b) if not k.startswith("_") and a.get(k) != b.get(k)}
        self.assertEqual(differ, {"name", "description", "runtime", "resync_delay_s"})            # the delay is the session runtime's own setting (ASSUMED), ignored by the backtester loop
        self.assertEqual(b["runtime"], "session")

    def test_a_reference_profile_refuses_the_session_runtime_at_load_and_at_start(self):
        from new_backtest.backtester import CrossVenueArbBacktester
        from new_backtest.profiles import ProfileError, apply_profile, load_profile
        for n in self.REFERENCE:
            with self.assertRaises(ProfileError, msg=n):
                apply_profile(load_profile(n), **self.KW, runtime="session")
        cfg = apply_profile(load_profile("perp_perp_maker_taker_v1_3"), **self.KW)
        cfg.runtime = "session"                                                               # edited after the load
        with self.assertRaises(ProfileError):
            CrossVenueArbBacktester(cfg)

    def test_an_unknown_runtime_is_refused(self):
        from new_backtest.profiles import ProfileError, apply_profile, load_profile
        with self.assertRaises(ProfileError):
            apply_profile(load_profile("perp_perp_maker_taker_v1_4"), **self.KW, runtime="paper")

    def test_the_backtester_does_not_silently_replay_a_session_profile_on_its_own_loop(self):
        from new_backtest.backtester import CrossVenueArbBacktester
        from new_backtest.profiles import apply_profile, load_profile
        cfg = apply_profile(load_profile("perp_perp_maker_taker_v1_5"), **self.KW)
        cfg.allow_manifest_gap = "test"
        with self.assertRaisesRegex(NotImplementedError, "not wired yet"):
            CrossVenueArbBacktester(cfg)


if __name__ == "__main__":
    unittest.main()
