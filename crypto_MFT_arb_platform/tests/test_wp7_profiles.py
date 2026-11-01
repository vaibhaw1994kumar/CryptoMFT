"""WP7 profiles and release identity; WP7b product rules (spot never pays funding, spot sells need inventory, fee/product match)."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
import warnings
from pathlib import Path

from new_backtest.arb_strategy import ArbConfig
from new_backtest.backtester import ArbBacktestConfig, CrossVenueArbBacktester
from new_backtest.fill_engine import MakerConfig
from new_backtest.profiles import PROFILES_DIR, SWITCHES, ProfileError, apply_profile, check_mandatory, load_profile, switch_table

from test_maker_order_lifetime import BINANCE, BYBIT, Harness

RUN = dict(data_root="unused", pair="SOLUSDT", start_date="2022-01-13", end_date="2022-01-13")


class ProfileLoadingTests(unittest.TestCase):
    def test_the_four_profiles_load_and_build_configs(self):
        for name in ("legacy_v0", "perp_perp_maker_taker_v1", "spot_spot_maker_taker_v1", "spot_perp_basis_v1"):
            doc = load_profile(PROFILES_DIR / f"{name}.yaml")
            cfg = apply_profile(doc, **RUN)
            self.assertIsInstance(cfg, ArbBacktestConfig)
            self.assertEqual(cfg.release_identity["profile"], name)
            self.assertEqual(len(cfg.release_identity["profile_sha256"]), 64)
            self.assertTrue(cfg.release_identity["code"]["sha256_sources"])
        v1 = apply_profile(load_profile("perp_perp_maker_taker_v1"), **RUN)
        self.assertEqual((v1.arb.profile, v1.arb.dispatch_mode, v1.valuation, v1.enforce_book_state, v1.cancel_time_basis),
                         ("perp_perp_maker_taker_v1", "fill_triggered", "common_timestamp", True, "exchange"))
        self.assertEqual((v1.maker.fill_evidence, v1.maker.evidence_window_ms, v1.maker.vanish_clears_queue, v1.maker.unknown_queue_model),
                         ("confirm_delta", 20.0, True, "depth_aware"))
        self.assertEqual((v1.arb.net_ceiling_multiple, v1.arb.count_pending_exposure, v1.arb.count_unknown_exposure), (1.0, True, True))
        self.assertIsNotNone(v1.timing); self.assertIsNotNone(v1.circuit_breaker); self.assertIsNotNone(v1.margin)
        self.assertEqual(v1.timing.equal_time_policy, "market_first")
        self.assertEqual(v1.hedge_faults.response, "retry_unwind")
        self.assertEqual(v1.margin.venues[BYBIT].mode, "cross")

    def test_legacy_profile_equals_the_engine_defaults_on_every_strategy_switch(self):
        cfg = apply_profile(load_profile("legacy_v0"), **RUN)
        d_arb, d_mk, d_bt = ArbConfig(), MakerConfig(), ArbBacktestConfig()
        for f in ("profile", "dispatch_mode", "maker_passive_side", "count_pending_exposure", "count_unknown_exposure", "net_ceiling_multiple", "p_fail", "loss_fail_bps"):
            self.assertEqual(getattr(cfg.arb, f), getattr(d_arb, f), f)
        for f in ("unknown_queue_model", "cancel_model", "vanish_clears_queue", "fill_evidence", "share_trade_volume", "dedupe_trade_depth",
                  "exchange_time_eligibility", "level_creator_trade_only"):
            self.assertEqual(getattr(cfg.maker, f), getattr(d_mk, f), f)
        for f in ("enforce_book_state", "trade_dedupe", "delta_uses_book_size", "cancel_time_basis", "postonly_reject_model"):
            self.assertEqual(getattr(cfg, f), getattr(d_bt, f), f)
        self.assertIsNone(cfg.timing); self.assertIsNone(cfg.circuit_breaker); self.assertIsNone(cfg.margin)
        self.assertEqual(cfg.valuation, "last_mark")                          # the one deliberate departure: the pre-R11 figure, flagged by the bridge

    def test_disabling_a_mandatory_control_is_rejected_outside_legacy(self):
        doc = load_profile("perp_perp_maker_taker_v1")
        for path, value in (("arb.dispatch_mode", "simultaneous"), ("valuation", "last_mark"), ("enforce_book_state", False),
                            ("maker.share_trade_volume", False), ("arb.maker_max_age_s", 0.0), ("hedge_faults.response", "legacy"),
                            ("cancel_time_basis", "receive"), ("timing.truth_books", False)):
            bad = copy.deepcopy(doc)
            cur = bad
            parts = path.split(".")
            for k in parts[:-1]:
                cur = cur[k]
            cur[parts[-1]] = value
            self.assertTrue(check_mandatory(bad), path)
            with self.assertRaises(ProfileError, msg=path):
                apply_profile(bad, **RUN)
        legacy = load_profile("legacy_v0")
        self.assertEqual(check_mandatory(legacy), [])                         # exempt by name
        no_breaker = copy.deepcopy(doc); no_breaker.pop("breaker")
        self.assertTrue(any("breaker" in x for x in check_mandatory(no_breaker)))
        no_margin = copy.deepcopy(doc); no_margin.pop("margin")
        self.assertTrue(any("margin" in x for x in check_mandatory(no_margin)))

    def test_unknown_fields_are_errors(self):
        doc = load_profile("perp_perp_maker_taker_v1")
        bad = copy.deepcopy(doc); bad["arb"]["min_edge_bpz"] = 2.0
        with self.assertRaises(ProfileError) as cm:
            apply_profile(bad, **RUN)
        self.assertIn("min_edge_bpz", str(cm.exception))
        bad2 = copy.deepcopy(doc); bad2["valuatoin"] = "x"
        with self.assertRaises(ProfileError):
            apply_profile(bad2, **RUN)

    def test_fee_preset_must_match_the_product(self):
        doc = copy.deepcopy(load_profile("perp_perp_maker_taker_v1"))
        doc["fee_presets"]["bybit"] = "bybit_spot_mm"
        cfg = apply_profile(doc, **RUN)                                         # the loader builds it...
        with self.assertRaises(ValueError) as cm:
            CrossVenueArbBacktester(cfg)                                       # ...the engine refuses to start
        self.assertIn("spot", str(cm.exception))
        basis = apply_profile(load_profile("spot_perp_basis_v1"), **RUN)
        bt = CrossVenueArbBacktester(basis)                                    # mixed on purpose: spot then perp
        self.assertEqual(bt.product_by_venue, {BYBIT: "spot", BINANCE: "perp"})

    def test_switch_table_covers_every_registered_switch(self):
        rows = switch_table(load_profile("perp_perp_maker_taker_v1"))
        self.assertEqual(len(rows), len(SWITCHES))
        self.assertEqual({r["class"] for r in rows}, {"MANDATORY", "CALIBRATED", "LEGACY_COMPAT"})


class ReleaseIdentityTests(unittest.TestCase):
    def test_identity_fields_and_stability(self):
        doc = load_profile("perp_perp_maker_taker_v1")
        a = apply_profile(doc, **RUN).release_identity
        b = apply_profile(doc, **RUN).release_identity
        for k in ("profile", "profile_sha256", "code", "data", "fee_snapshot_sha256", "fee_presets", "calibration_cutoff", "seed", "latency_preset"):
            self.assertIn(k, a)
        self.assertEqual(a["code"]["sha256_sources"], b["code"]["sha256_sources"])
        self.assertIn("git_rev", a["code"]); self.assertIn("git_dirty", a["code"])
        self.assertEqual(a["data"]["files_listed"], 0)                       # data_root 'unused'
        json.dumps(a)                                                          # serialisable into results

    def test_identity_travels_into_the_result(self):
        h = Harness(cfg_kw={"release_identity": {"profile": "test", "profile_sha256": "x" * 64}})
        h.book(0.5, BYBIT); h.book(0.5, BINANCE)
        from types import SimpleNamespace
        r = h.bt._build_result(SimpleNamespace(report=lambda: {}), {}, 0.0)
        self.assertEqual(r.release_identity["profile"], "test")
        self.assertEqual(r.product_by_venue, {BYBIT: "perp", BINANCE: "perp"})
        self.assertEqual(r.strategy_validation_label, "")


class Rig:
    """A hand-driven backtester with explicit fee presets (the shared Harness pins perp presets)."""

    def __init__(self, **cfg_kw):
        from new_backtest.arb_strategy import MAKER_TAKER
        kw = dict(data_root="unused", venues=(BYBIT, BINANCE), pair="BTCUSDT", start_date="2026-10-01", end_date="2026-10-01",
                  latency_preset="colocated_both", maker=MakerConfig(min_queue_ahead=0.5),
                  arb=ArbConfig(mode=MAKER_TAKER, maker_max_age_s=10.0, max_position_base=10.0))
        kw.update(cfg_kw)
        self.bt = CrossVenueArbBacktester(ArbBacktestConfig(**kw))
        self.bt.latency.entry_delay = lambda v: 0.003
        self._sid = 0

    def book(self, t, venue):
        from types import SimpleNamespace
        from new_backtest.event_loader import BOOK
        self.bt._advance_to(t)
        self.bt._apply_event(SimpleNamespace(kind=BOOK, venue=venue, payload={"bids": [[99.0, 5.0], [98.0, 5.0]], "asks": [[101.0, 5.0], [102.0, 5.0]]}, receive_ts=t))
        self.bt.tick_size[venue] = 1.0

    def send_maker(self, t, venue=BYBIT, qty=2.0, price=100.0):
        from new_backtest.arb_strategy import MAKER_TAKER, ArbLeg, ArbSignal
        hedge = BINANCE if venue == BYBIT else BYBIT
        self._sid += 1
        leg = ArbLeg(venue=venue, side="buy", qty=qty, limit_price=price, is_taker=False, expected_price=price, fee_bps=0.0)
        sig = ArbSignal(signal_id=self._sid, ts=t, buy_venue=venue, sell_venue=hedge, qty=qty, gross_edge_bps=5.0, fee_bps=1.0, net_edge_bps=4.0,
                        legs=[leg], mode=MAKER_TAKER, buy_price=price, sell_price=price + 1)
        self.bt._fire(sig, t)


class SpotRulesTests(unittest.TestCase):
    def _spot(self, **cfg_kw):
        base = dict(bybit_fee_preset="bybit_spot_mm", binance_fee_preset="binance_spot_vip0", product="spot", product_fee_consistency=True,
                    spot_requires_inventory=True)
        base.update(cfg_kw)
        return Rig(**base)

    def test_spot_never_pays_funding_and_results_are_labelled(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "f.jsonl").write_text(json.dumps({"venue": "bybit", "funding_ts_ns": 10 ** 9, "rate": 0.01, "mark_price": 100.0}))
            h = self._spot(funding_rate_dir=d)
            self.assertIsNone(h.bt.settler)                                    # no perp venue: no settler
            h.book(0.5, BYBIT); h.book(0.5, BINANCE)
            from types import SimpleNamespace
            r = h.bt._build_result(SimpleNamespace(report=lambda: {}), {}, 0.0)
            self.assertEqual(r.funding_paid, 0.0)
            self.assertEqual(r.strategy_validation_label, "UNVALIDATED STRATEGY - mechanics only")
            mixed = Rig(bybit_fee_preset="bybit_spot_mm", binance_fee_preset="binance_perp_vip0", product="spot_perp",
                        product_fee_consistency=True, funding_rate_dir=d)
            self.assertEqual(set(mixed.bt.settler._ticks), {BINANCE})          # funding on the perp leg only

    def test_a_spot_sell_without_inventory_is_rejected_unless_borrow_is_enabled(self):
        h = self._spot()
        h.book(0.5, BYBIT); h.book(0.5, BINANCE)
        h.send_maker(1.0)                                                      # passive BUY on bybit, hedge SELL on binance (no base there)
        self.assertEqual(h.bt.hedge["signals_blocked_spot_inventory"], 1)
        self.assertEqual(list(h.bt.pending.in_flight()), [])
        ok = self._spot(borrow_enabled=True)
        ok.book(0.5, BYBIT); ok.book(0.5, BINANCE)
        ok.send_maker(1.0)
        self.assertEqual(ok.bt.hedge.get("signals_blocked_spot_inventory", 0), 0)
        self.assertEqual(len(list(ok.bt.pending.in_flight())), 1)
        funded = self._spot()
        funded.bt.inventory.apply(BINANCE, 5.0, -500.0)                        # base held on the hedge venue: allowed
        funded.book(0.5, BYBIT); funded.book(0.5, BINANCE)
        funded.send_maker(1.0)
        self.assertEqual(len(list(funded.bt.pending.in_flight())), 1)

    def test_mismatched_fee_family_is_an_error_when_consistency_is_required_and_a_warning_otherwise(self):
        with self.assertRaises(ValueError):
            Rig(bybit_fee_preset="bybit_spot_mm", binance_fee_preset="binance_perp_vip0", product_fee_consistency=True)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Rig(bybit_fee_preset="bybit_spot_mm", binance_fee_preset="binance_perp_vip0")
        self.assertTrue(any("mismatch" in str(x.message) for x in w))          # legacy: warning only


if __name__ == "__main__":
    unittest.main()
