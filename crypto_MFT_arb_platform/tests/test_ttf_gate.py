"""Tests for the time-to-fill gate (max_ttf_s) and the e_ttf estimator.

Item 2 of the Jan-2022 backtest audit:
  - Verify the corrected E[T|T<=H_max] formula is bounded by H_max.
  - Verify gate ordering: adverse_prob fires before TTF, which fires before fill_prob.
  - Verify that a signal whose e_ttf exceeds max_ttf_s is rejected with 'slow_ttf'.
  - Verify that the gate is inactive when max_ttf_s=0.

Formula note:
  The old formula was rmst / p_H (= E[min(T,H)] / P(T<=H)), which overestimates
  E[T|T<=H] by H*(1/p_H - 1) and is unbounded when p_H is small.
  The corrected formula is (rmst - H_max*(1 - p_H)) / p_H, matching the definition
  E[T|T<=H] = (E[min(T,H)] - H*P(T>H)) / P(T<=H).  This is bounded by H_max.
"""
from __future__ import annotations

import math
import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace

from new_backtest.arb_strategy import ArbConfig, ArbLeg, ArbSignal, MAKER_TAKER
from new_backtest.backtester import ArbBacktestConfig, CrossVenueArbBacktester
from new_backtest.event_loader import BOOK, TRADE
from new_backtest.fill_engine import MakerConfig
from new_backtest.fill_model_v1 import FillModelV2, TTFPrediction

BYBIT, BINANCE = "bybit", "binance"


# ── Minimal mock that satisfies hasattr(model, "predict_ttf") ─────────────────

@dataclass
class StubModel:
    """Returns a fixed TTFPrediction regardless of features.  horizon_s matches gate."""
    horizon_s: int = 5
    p_fill: float = 0.8
    e_ttf: float = 1.5
    p_adverse: float = 0.05

    def predict(self, **_) -> float:
        return self.p_fill

    def predict_ttf(self, **_) -> TTFPrediction:
        return TTFPrediction(
            p_fill=self.p_fill,
            p_by_h={1: 0.5, self.horizon_s: self.p_fill, 15: min(self.p_fill + 0.05, 1.0)},
            e_ttf=self.e_ttf,
            p_adverse=self.p_adverse,
            e_fill_ratio=1.0,
        )


def _harness(max_ttf_s=2.5, max_adverse_prob=0.15, min_fill_prob=0.5,
             fill_gate_horizon_s=5, model: object = None):
    """A backtester wired with a stub fill model, driven by hand."""
    cfg = ArbBacktestConfig(
        data_root="unused", venues=(BYBIT, BINANCE), pair="BTCUSDT",
        start_date="2026-10-01", end_date="2026-10-01",
        latency_preset="colocated_both",
        bybit_fee_preset="bybit_perp_mm",
        binance_fee_preset="binance_perp_vip0",
        maker=MakerConfig(min_queue_ahead=0.0),
        arb=ArbConfig(
            mode=MAKER_TAKER,
            min_net_edge_bps=1.0,
            max_clip_notional=10_000.0,
            max_ttf_s=max_ttf_s,
            max_adverse_prob=max_adverse_prob,
            min_fill_prob=min_fill_prob,
            fill_gate_horizon_s=fill_gate_horizon_s,
            max_position_base=100.0,
        ),
    )
    bt = CrossVenueArbBacktester(cfg)
    if model is not None:
        bt.strategy.fill_model_v1 = model
    bt.latency.entry_delay = lambda _: 0.001
    return bt


def _book_event(t, venue, bid=100.0, ask=100.5, size=50.0):
    return SimpleNamespace(
        kind=BOOK, venue=venue, receive_ts=t, exchange_ts=t,
        payload={"bids": [[bid, size]], "asks": [[ask, size]]},
    )


def _evaluate_one_tick(bt, t=1.0):
    """Feed books with a valid BINANCE-maker / BYBIT-taker arb edge.

    Fee math (binance_perp_vip0 + bybit_perp_mm):
      fee_bps = 2.0 + 5.5 = 7.5;  required_bps = 7.5 + 1.0 + 0.5 = 9.0
      max_bid = 100.0 * (1 - 9.0/10000) = 99.91
    BINANCE book (maker): bid=99.8 < 99.91 < ask=100.1 → max_bid fits inside touch
    Spreads: BYBIT 30 bps, BINANCE 30 bps — both within max_spread_bps=50.
    t=1.0 (not 0.0): OrderBook.age_s returns inf when last_update_ts <= 0.
    """
    bt._advance_to(t)
    bt._apply_event(_book_event(t, BYBIT,   bid=100.0, ask=100.3, size=50.0))
    bt._advance_to(t)
    bt._apply_event(_book_event(t, BINANCE, bid=99.8,  ask=100.1, size=50.0))
    bt.tick_size.update({v: 0.5 for v in (BYBIT, BINANCE)})
    return bt.strategy.evaluate(
        now=t,
        books=bt.books,
        states=bt.states,
        inventory=bt.inventory,
        pending=bt.pending,
    )


# ── Unit tests on TTFPrediction formula ──────────────────────────────────────

class ETtfFormulaTests(unittest.TestCase):
    """Verify the corrected E[T|T<=H] formula properties on FillModelV2."""

    def _model_with_rates(self, p1, p5, p15, gate=5):
        """Fit a trivial FillModelV2 from constant predictions."""
        import copy
        from new_backtest.fill_model_v1 import FillModelV1
        def _stub_v1(p):
            m = object.__new__(FillModelV1)
            m.__dict__.update(version=1, horizon_s=1, trained_until_ts=0.0, n_train=10,
                               base_rate=p, feature_mean=[0.0]*9, feature_scale=[1.0]*9,
                               coef=[0.0]*9, intercept=math.log(p / (1 - p)))
            return m
        return FillModelV2(
            gate_horizon_s=gate, trained_until_ts=0.0,
            horizons=[1, 5, 15],
            per_horizon={"1": _stub_v1(p1), "5": _stub_v1(p5), "15": _stub_v1(p15)},
            adverse_model=None, mean_fill_ratio=1.0, n_train=10,
        )

    def _e_ttf(self, p1, p5, p15):
        return self._model_with_rates(p1, p5, p15).predict_ttf(
            queue_ahead=0, local_depth=100, local_depth_baseline=100,
            spread_pct=0.005, short_vol=0, trade_count=1, trade_sign=0,
            regime_bucket="calm",
        ).e_ttf

    def test_e_ttf_bounded_by_max_horizon(self):
        # With any fill rates, corrected formula gives e_ttf <= H_max (15s)
        for p1, p5, p15 in [
            (0.5, 0.7, 0.8),
            (0.01, 0.01, 0.02),    # very slow fills — old formula diverges here
            (0.99, 0.99, 0.999),   # very fast fills
            (0.3, 0.5, 0.6),
        ]:
            with self.subTest(p1=p1, p5=p5, p15=p15):
                e = self._e_ttf(p1, p5, p15)
                self.assertLessEqual(e, 15.0 + 1e-9, f"e_ttf={e:.3f} exceeds H_max=15")

    def test_e_ttf_non_negative(self):
        for p1, p5, p15 in [(0.5, 0.7, 0.75), (0.9, 0.95, 0.99), (0.1, 0.2, 0.3)]:
            with self.subTest(p1=p1, p5=p5, p15=p15):
                self.assertGreaterEqual(self._e_ttf(p1, p5, p15), 0.0)

    def test_faster_fills_give_smaller_e_ttf(self):
        # Both at base rates — fast fills (high p) give lower e_ttf than slow fills
        e_fast = self._e_ttf(0.8, 0.9, 0.95)
        e_slow = self._e_ttf(0.1, 0.2, 0.3)
        self.assertLess(e_fast, e_slow)

    def test_typical_base_rates_give_e_ttf_below_3s(self):
        # Old model base rates (Jan 06-09 training, no TIF): 53.8%, 67.7%, ~75%
        # With corrected formula, typical signals have e_ttf ~ 1-2s, well below max_ttf_s=2.5
        e = self._e_ttf(0.538, 0.677, 0.75)
        self.assertLess(e, 3.0, f"typical e_ttf={e:.3f} should be below 3s with corrected formula")
        self.assertGreater(e, 0.0)

    def test_old_formula_would_have_overestimated_by_factor_of_three(self):
        # Document the magnitude of the pre-fix error for audit purposes
        p1, p5, p15 = 0.538, 0.677, 0.75
        H = 15
        s = [1 - p for p in [0, p1, p5, p15]]
        s[0] = 1.0
        t = [0, 1, 5, 15]
        rmst = sum((t[i+1]-t[i])*(s[i]+s[i+1])/2 for i in range(3))
        pH = p15
        old_e_ttf = rmst / pH
        new_e_ttf = (rmst - H * (1 - pH)) / pH
        self.assertGreater(old_e_ttf / new_e_ttf, 2.5,
                           "old formula should overestimate by at least 2.5x for these rates")


# ── Gate ordering and rejection tests ────────────────────────────────────────

class TTFGateOrderingTests(unittest.TestCase):

    def test_slow_e_ttf_rejects_signal_with_slow_ttf_code(self):
        """A signal whose e_ttf > max_ttf_s is rejected (adverse_prob must be low to reach TTF gate)."""
        model = StubModel(e_ttf=5.0, p_adverse=0.05, p_fill=0.8)   # slow fill, low adverse
        bt = _harness(max_ttf_s=2.5, max_adverse_prob=0.15, min_fill_prob=0.5, model=model)
        sig = _evaluate_one_tick(bt)
        self.assertIsNone(sig, "signal with e_ttf=5.0 > max_ttf_s=2.5 should be rejected")
        self.assertEqual(bt.strategy.rejects.get("slow_ttf", 0), 1)
        self.assertEqual(bt.strategy.rejects.get("high_adverse_prob", 0), 0)

    def test_fast_e_ttf_passes_ttf_gate(self):
        """A signal with e_ttf < max_ttf_s is not rejected by the TTF gate."""
        model = StubModel(e_ttf=1.5, p_adverse=0.05, p_fill=0.8)   # fast fill, low adverse
        bt = _harness(max_ttf_s=2.5, max_adverse_prob=0.15, min_fill_prob=0.5, model=model)
        # TTF gate should pass; signal may still be rejected for other reasons but not slow_ttf
        _evaluate_one_tick(bt)
        self.assertEqual(bt.strategy.rejects.get("slow_ttf", 0), 0)

    def test_adverse_prob_gate_runs_before_ttf_gate_and_shadows_it(self):
        """When adverse_prob > max_adverse_prob AND e_ttf > max_ttf_s, only high_adverse_prob fires."""
        model = StubModel(e_ttf=10.0, p_adverse=0.30, p_fill=0.8)  # both gates would fire
        bt = _harness(max_ttf_s=2.5, max_adverse_prob=0.15, model=model)
        sig = _evaluate_one_tick(bt)
        self.assertIsNone(sig)
        # Only adverse_prob fires; TTF is never reached
        self.assertEqual(bt.strategy.rejects.get("high_adverse_prob", 0), 1)
        self.assertEqual(bt.strategy.rejects.get("slow_ttf", 0), 0)

    def test_fill_prob_gate_runs_after_ttf_gate_and_is_shadowed_by_ttf(self):
        """When e_ttf > max_ttf_s AND p_fill < min_fill_prob, only slow_ttf fires."""
        model = StubModel(e_ttf=8.0, p_adverse=0.05, p_fill=0.3)  # slow + low fill prob
        bt = _harness(max_ttf_s=2.5, max_adverse_prob=0.15, min_fill_prob=0.5, model=model)
        sig = _evaluate_one_tick(bt)
        self.assertIsNone(sig)
        self.assertEqual(bt.strategy.rejects.get("slow_ttf", 0), 1)
        self.assertEqual(bt.strategy.rejects.get("low_fill_prob", 0), 0)

    def test_ttf_gate_inactive_when_max_ttf_s_is_zero(self):
        """max_ttf_s=0 disables the TTF gate entirely."""
        model = StubModel(e_ttf=999.0, p_adverse=0.05, p_fill=0.9)
        bt = _harness(max_ttf_s=0.0, max_adverse_prob=0.15, min_fill_prob=0.5, model=model)
        _evaluate_one_tick(bt)
        self.assertEqual(bt.strategy.rejects.get("slow_ttf", 0), 0)

    def test_gate_order_is_adverse_then_ttf_then_fill_prob(self):
        """Verify the full three-gate ordering with a model that would trigger each gate individually."""
        # adverse_prob=0.20 > 0.15, e_ttf=5.0 > 2.5, p_fill=0.3 < 0.5 — only adverse_prob fires
        model = StubModel(e_ttf=5.0, p_adverse=0.20, p_fill=0.3)
        bt = _harness(max_ttf_s=2.5, max_adverse_prob=0.15, min_fill_prob=0.5, model=model)
        _evaluate_one_tick(bt)
        self.assertEqual(bt.strategy.rejects.get("high_adverse_prob", 0), 1)
        self.assertEqual(bt.strategy.rejects.get("slow_ttf", 0), 0)
        self.assertEqual(bt.strategy.rejects.get("low_fill_prob", 0), 0)


if __name__ == "__main__":
    unittest.main()
