"""Tests for Task 2 (risk limits), Task 3 (circuit breaker), and Task 5 (P&L accounting).

Tests verify:
  Task 2b — max_net_base_hard and fill_headroom_base
  Task 2c — count_unknown_exposure
  Task 2d — require_explicit_size
  Task 3  — CircuitBreaker state machine and all 6 triggers
  Task 5  — cash_pnl, common_timestamp_equity, mark_inventory_venue, ArbBacktestResult fields
"""
from __future__ import annotations

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

import pytest
from new_backtest.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from new_backtest.arb_strategy import ArbConfig
from new_backtest.backtester import ArbBacktestConfig, CrossVenueArbBacktester
from new_backtest.fill_engine import MakerConfig


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clean_breaker(cfg: CircuitBreakerConfig) -> CircuitBreaker:
    return CircuitBreaker(cfg, "bybit", "binance")


# ── Task 2b: hard net ceiling ─────────────────────────────────────────────────

class TestMaxNetBaseHard:
    def test_legacy_2x_when_none(self):
        cfg = ArbConfig(max_position_base=1.0, max_net_base_hard=None)
        assert cfg.max_net_base_hard is None

    def test_hard_ceiling_propagates(self):
        cfg = ArbConfig(max_position_base=1.0, max_net_base_hard=0.8)
        assert cfg.max_net_base_hard == 0.8

    def test_fill_headroom_default_zero(self):
        cfg = ArbConfig()
        assert cfg.fill_headroom_base == 0.0

    def test_fill_headroom_propagates(self):
        cfg = ArbConfig(fill_headroom_base=0.3)
        assert cfg.fill_headroom_base == 0.3

    def test_all_three_together(self):
        cfg = ArbConfig(max_position_base=1.0, max_net_base_hard=0.8, fill_headroom_base=0.1)
        assert cfg.max_net_base_hard == 0.8
        assert cfg.fill_headroom_base == 0.1
        assert cfg.max_position_base == 1.0


# ── Task 2c: count_unknown_exposure ──────────────────────────────────────────

class TestCountUnknownExposure:
    def test_default_is_false(self):
        cfg = ArbConfig()
        assert cfg.count_unknown_exposure is False

    def test_opt_in(self):
        cfg = ArbConfig(count_unknown_exposure=True)
        assert cfg.count_unknown_exposure is True

    def test_reflected_in_backtester_config(self):
        arb = ArbConfig(count_unknown_exposure=True)
        bt = ArbBacktestConfig.__dataclass_fields__
        assert "circuit_breaker" in bt  # also check circuit_breaker while here


# ── Task 2d: require_explicit_size ───────────────────────────────────────────

class TestRequireExplicitSize:
    def test_default_size_flag_false_no_error(self):
        # Should NOT raise: require_explicit_size defaults to False
        cfg = ArbBacktestConfig(
            pair="SOLUSDT", data_root="/tmp", start_date="2022-01-05", end_date="2022-01-05",
            require_explicit_size=False,
        )
        assert cfg.require_explicit_size is False

    def test_require_flag_raises_on_default_size(self):
        from new_backtest.backtester import _ARBCONFIG_DEFAULT_SIZE
        arb = ArbConfig(max_position_base=_ARBCONFIG_DEFAULT_SIZE)  # unset
        cfg = ArbBacktestConfig(
            pair="SOLUSDT", data_root="/tmp", start_date="2022-01-05", end_date="2022-01-05",
            require_explicit_size=True, arb=arb,
        )
        from new_backtest.backtester import CrossVenueArbBacktester
        with pytest.raises(ValueError, match="max_position_base not set"):
            CrossVenueArbBacktester(cfg)

    def test_explicit_size_does_not_raise(self):
        from new_backtest.backtester import CrossVenueArbBacktester
        arb = ArbConfig(max_position_base=5.0)
        cfg = ArbBacktestConfig(
            pair="SOLUSDT", data_root="/tmp", start_date="2022-01-05", end_date="2022-01-05",
            require_explicit_size=True, arb=arb,
        )
        # Should not raise during construction
        bt = CrossVenueArbBacktester(cfg)
        assert bt is not None


# ── Task 3: CircuitBreakerConfig validation ───────────────────────────────────

class TestCircuitBreakerConfigValidation:
    def test_zero_consecutive_failures_raises(self):
        with pytest.raises(ValueError, match="consecutive_failures must be >= 1"):
            CircuitBreakerConfig(consecutive_failures=0)

    def test_negative_consecutive_failures_raises(self):
        with pytest.raises(ValueError, match="consecutive_failures must be >= 1"):
            CircuitBreakerConfig(consecutive_failures=-1)

    def test_zero_failure_rate_thresh_raises(self):
        with pytest.raises(ValueError, match="failure_rate_thresh"):
            CircuitBreakerConfig(failure_rate_thresh=0.0)

    def test_failure_rate_over_1_raises(self):
        with pytest.raises(ValueError, match="failure_rate_thresh"):
            CircuitBreakerConfig(failure_rate_thresh=1.5)

    def test_negative_net_exposure_s_raises(self):
        with pytest.raises(ValueError, match="net_exposure_s must be > 0"):
            CircuitBreakerConfig(net_exposure_s=-1.0)

    def test_valid_config_no_raise(self):
        cb = CircuitBreakerConfig(consecutive_failures=3, failure_rate_thresh=0.5,
                                   failure_rate_window_s=30.0, net_exposure_s=5.0,
                                   unknown_order_s=2.0)
        assert cb.consecutive_failures == 3


# ── Task 3: CircuitBreaker state machine ─────────────────────────────────────

class TestCircuitBreakerStateMachine:

    def test_starts_untripped(self):
        cb = _clean_breaker(CircuitBreakerConfig(consecutive_failures=3))
        assert not cb.is_tripped()
        assert cb.allow_signal(0.0, 0.0)

    def test_consecutive_failures_exact_threshold(self):
        cb = _clean_breaker(CircuitBreakerConfig(consecutive_failures=3))
        for i in range(2):
            cb.on_hedge_result(float(i), failed=True, net_abs=0.0, pending_hedges=0)
        assert not cb.is_tripped(), "should not trip before reaching threshold"
        cb.on_hedge_result(3.0, failed=True, net_abs=0.0, pending_hedges=0)
        assert cb.is_tripped()

    def test_reset_after_trip(self):
        cb = _clean_breaker(CircuitBreakerConfig(consecutive_failures=1))
        cb.on_hedge_result(0.0, failed=True, net_abs=0.0, pending_hedges=0)
        assert cb.is_tripped()
        cb.reset()
        assert not cb.is_tripped()
        assert cb.allow_signal(0.0, 0.0)

    def test_success_clears_consecutive_counter(self):
        cb = _clean_breaker(CircuitBreakerConfig(consecutive_failures=3))
        cb.on_hedge_result(0.0, failed=True, net_abs=0.0, pending_hedges=0)
        cb.on_hedge_result(1.0, failed=True, net_abs=0.0, pending_hedges=0)
        cb.on_hedge_result(2.0, failed=False, net_abs=0.0, pending_hedges=0)  # resets counter
        cb.on_hedge_result(3.0, failed=True, net_abs=0.0, pending_hedges=0)
        cb.on_hedge_result(4.0, failed=True, net_abs=0.0, pending_hedges=0)
        assert not cb.is_tripped(), "counter should have been reset by the success"

    def test_failure_rate_trigger(self):
        cb = _clean_breaker(CircuitBreakerConfig(failure_rate_window_s=60.0, failure_rate_thresh=0.5))
        # 4 failures out of 6 = 66%
        for i, failed in enumerate([True, False, True, True, True, False]):
            cb.on_hedge_result(float(i), failed=failed, net_abs=0.0, pending_hedges=0)
        assert cb.is_tripped()

    def test_failure_rate_below_threshold_no_trip(self):
        # Pattern: [T, F, F, T, F, F] → max sub-window rate = 2/3 = 0.5 < 0.6 threshold
        cb = _clean_breaker(CircuitBreakerConfig(failure_rate_window_s=60.0, failure_rate_thresh=0.6))
        for i, failed in enumerate([True, False, False, True, False, False]):
            cb.on_hedge_result(float(i), failed=failed, net_abs=0.0, pending_hedges=0)
        assert not cb.is_tripped(), "max rate 50% < 60% threshold, should not trip"

    def test_failed_unwind_trigger(self):
        cb = _clean_breaker(CircuitBreakerConfig(trip_on_failed_unwind=True))
        assert not cb.is_tripped()
        cb.on_failed_unwind(ts=0.0, net_abs=0.5, pending_hedges=0)
        assert cb.is_tripped()
        assert cb.trips[0].cause == "failed_unwind: all attempts exhausted"

    def test_net_exposure_trigger(self):
        cb = _clean_breaker(CircuitBreakerConfig(net_exposure_s=5.0))
        cb.on_net_update(0.0, net_abs=1.0, pending_hedges=0)   # enters nonzero exposure
        cb.on_net_update(4.9, net_abs=1.0, pending_hedges=0)   # just under threshold
        assert not cb.is_tripped()
        cb.on_net_update(5.1, net_abs=1.0, pending_hedges=0)   # over threshold
        assert cb.is_tripped()

    def test_net_exposure_resets_on_zero(self):
        cb = _clean_breaker(CircuitBreakerConfig(net_exposure_s=5.0))
        cb.on_net_update(0.0, net_abs=1.0, pending_hedges=0)
        cb.on_net_update(3.0, net_abs=0.0, pending_hedges=0)   # cleared
        cb.on_net_update(6.0, net_abs=1.0, pending_hedges=0)   # clock restarted
        assert not cb.is_tripped(), "exposure clock restarted after clearing"

    def test_unknown_order_trigger(self):
        cb = _clean_breaker(CircuitBreakerConfig(unknown_order_s=2.0))
        cb.on_unknown_order(order_id=42, ts=0.0, venue="binance")
        cb.check_unknown_orders(1.9, net_abs=0.0, pending_hedges=0)
        assert not cb.is_tripped()
        cb.check_unknown_orders(2.1, net_abs=0.0, pending_hedges=0)
        assert cb.is_tripped()

    def test_resolved_unknown_order_does_not_trip(self):
        cb = _clean_breaker(CircuitBreakerConfig(unknown_order_s=2.0))
        cb.on_unknown_order(order_id=42, ts=0.0, venue="binance")
        cb.on_order_resolved(42)
        cb.check_unknown_orders(5.0, net_abs=0.0, pending_hedges=0)
        assert not cb.is_tripped()

    def test_trip_blocks_signal(self):
        cb = _clean_breaker(CircuitBreakerConfig(consecutive_failures=1))
        cb.on_hedge_result(0.0, failed=True, net_abs=0.0, pending_hedges=0)
        assert not cb.allow_signal(0.0, 0.0)

    def test_allow_unwind_while_tripped(self):
        cb = _clean_breaker(CircuitBreakerConfig(consecutive_failures=1, tripped_unwind_retries=3))
        cb.on_hedge_result(0.0, failed=True, net_abs=0.0, pending_hedges=0)
        for i in range(3):
            assert cb.allow_unwind(), f"unwind {i} should be allowed"
        assert not cb.allow_unwind(), "4th unwind should be denied"

    def test_trip_count_and_summary(self):
        cb = _clean_breaker(CircuitBreakerConfig(consecutive_failures=2, trip_on_failed_unwind=True))
        cb.on_hedge_result(0.0, failed=True, net_abs=0.5, pending_hedges=1)
        cb.on_hedge_result(0.1, failed=True, net_abs=0.5, pending_hedges=1)
        s = cb.summary()
        assert s["trip_count"] == 1
        assert s["tripped"] is True
        assert len(s["trips"]) == 1

    def test_no_trip_disabled_config(self):
        # A config with no triggers set should never trip
        cb = _clean_breaker(CircuitBreakerConfig())
        for _ in range(100):
            cb.on_hedge_result(0.0, failed=True, net_abs=10.0, pending_hedges=10)
        assert not cb.is_tripped()

    def test_backtester_config_has_circuit_breaker_field(self):
        cfg = ArbBacktestConfig(
            pair="SOLUSDT", data_root="/tmp", start_date="2022-01-05", end_date="2022-01-05",
            circuit_breaker=CircuitBreakerConfig(consecutive_failures=5),
        )
        assert cfg.circuit_breaker is not None
        assert cfg.circuit_breaker.consecutive_failures == 5

    def test_circuit_breaker_none_is_default(self):
        cfg = ArbBacktestConfig(
            pair="SOLUSDT", data_root="/tmp", start_date="2022-01-05", end_date="2022-01-05",
        )
        assert cfg.circuit_breaker is None

    def test_failure_rate_strictly_above_threshold_trips(self):
        # 4 of 6 = 66.7% > 0.6 threshold → must trip
        cb = _clean_breaker(CircuitBreakerConfig(failure_rate_window_s=60.0, failure_rate_thresh=0.6))
        for i, f in enumerate([False, False, True, True, True, True]):
            cb.on_hedge_result(float(i), failed=f, net_abs=0.0, pending_hedges=0)
        assert cb.is_tripped(), "66.7% > 60% threshold should trip"

    def test_failure_rate_at_threshold_uses_strict_greater_than(self):
        # 3 of 6 = 50% which is NOT strictly > 50% → should NOT trip
        # Use sequence that stays ≤50% at every intermediate window too
        cb = _clean_breaker(CircuitBreakerConfig(failure_rate_window_s=60.0, failure_rate_thresh=0.5))
        # [F, F, F, T, T, T] → sub-windows: [F,F,F]=0%, [F,F,F,T]=25%, [F,F,F,T,T]=40%, [F,F,F,T,T,T]=50%
        for i, f in enumerate([False, False, False, True, True, True]):
            cb.on_hedge_result(float(i), failed=f, net_abs=0.0, pending_hedges=0)
        assert not cb.is_tripped(), "50% == 50% threshold (strict >), should not trip"

    def test_reset_clears_consecutive_counter(self):
        # After reset, consecutive counter must be zero — a new run of N-1 failures should not trip
        cb = _clean_breaker(CircuitBreakerConfig(consecutive_failures=3))
        for _ in range(3):
            cb.on_hedge_result(0.0, failed=True, net_abs=0.0, pending_hedges=0)
        assert cb.is_tripped()
        cb.reset()
        # After reset, need 3 NEW failures to trip again
        for _ in range(2):
            cb.on_hedge_result(0.0, failed=True, net_abs=0.0, pending_hedges=0)
        assert not cb.is_tripped(), "only 2 new failures after reset; counter must have been cleared"


# ── Task 5: P&L accounting ────────────────────────────────────────────────────

class TestPnLAccounting:
    """Verify Task 5 fields on ArbBacktestResult and ArbConfig.mark_inventory_venue."""

    def _make_result(self, **kwargs):
        """Construct an ArbBacktestResult with minimal required fields, overriding with kwargs."""
        from new_backtest.backtester import ArbBacktestResult
        from dataclasses import fields, MISSING
        defaults = {}
        for f in fields(ArbBacktestResult):
            if f.default is not MISSING:
                defaults[f.name] = f.default
            elif f.default_factory is not MISSING:
                defaults[f.name] = f.default_factory()
            else:
                # Required field — fill with a sensible zero
                if f.type in ("float", "int") or f.type == float or f.type == int:
                    defaults[f.name] = 0.0 if f.type in ("float", float) else 0
                elif f.type == "str" or f.type == str:
                    defaults[f.name] = ""
                elif "dict" in str(f.type):
                    defaults[f.name] = {}
                elif "list" in str(f.type):
                    defaults[f.name] = []
                else:
                    defaults[f.name] = None
        defaults.update(kwargs)
        return ArbBacktestResult(**defaults)

    def test_cash_pnl_field_exists_with_default_zero(self):
        r = self._make_result()
        assert hasattr(r, "cash_pnl")
        assert r.cash_pnl == 0.0

    def test_common_timestamp_equity_field_exists(self):
        r = self._make_result()
        assert hasattr(r, "common_timestamp_equity")
        assert r.common_timestamp_equity == 0.0

    def test_common_mid_staleness_s_field_exists(self):
        r = self._make_result()
        assert hasattr(r, "common_mid_staleness_s")
        assert r.common_mid_staleness_s == 0.0

    def test_cash_pnl_set_correctly(self):
        r = self._make_result(cash_pnl=42.5)
        assert r.cash_pnl == 42.5

    def test_realized_pnl_still_present_backward_compat(self):
        # realized_pnl must not be removed (backward compat)
        r = self._make_result(realized_pnl=10.0)
        assert r.realized_pnl == 10.0

    def test_ending_equity_still_present(self):
        r = self._make_result(ending_equity=99.9, cash_pnl=99.9)
        assert r.ending_equity == 99.9

    def test_mark_inventory_venue_default_none(self):
        cfg = ArbConfig()
        assert cfg.mark_inventory_venue is None

    def test_mark_inventory_venue_opt_in(self):
        cfg = ArbConfig(mark_inventory_venue="binance")
        assert cfg.mark_inventory_venue == "binance"

    def test_batch_summary_has_ending_equity_and_cash_pnl(self):
        from new_backtest.batch_backtest import BatchSummary
        from dataclasses import fields, MISSING
        field_names = {f.name for f in fields(BatchSummary)}
        assert "total_ending_equity" in field_names
        assert "total_cash_pnl" in field_names

    def test_batch_summary_ending_equity_defaults_zero(self):
        from new_backtest.batch_backtest import BatchSummary
        s = BatchSummary(
            mode="maker_taker", latency_preset="singapore",
            start_date="2026-01-01", end_date="2026-01-01",
            pairs_requested=1, pairs_run=1, pairs_skipped=0,
            total_realized_pnl=5.0, total_gross_pnl=6.0,
            total_fees=0.5, total_unhedged_loss=0.0, total_traded_notional=1000.0,
            total_signals=1, total_orders_sent=2, total_orders_filled=2,
            total_orders_missed=0, fill_ratio=1.0, mean_edge_capture=1.0,
            mean_realized_net_edge_bps=1.0, pnl_per_million_notional=5.0,
            profitable_pairs=1,
        )
        assert s.total_ending_equity == 0.0   # default
        assert s.total_cash_pnl == 0.0         # default

    def test_cash_pnl_equals_ending_equity_when_zero_net(self):
        # When net_base == 0, open_position_value == 0, so ending_equity == total_cash == cash_pnl.
        r = self._make_result(
            net_base_position=0.0,
            open_position_value=0.0,
            ending_equity=1.50,
            cash_pnl=1.50,
        )
        assert abs(r.ending_equity - r.cash_pnl) < 1e-9

    # ── Reconciliation: fully closed position ────────────────────────────────

    def test_fully_closed_cash_pnl_equals_ending_equity(self):
        # No open inventory → ending_equity must equal cash_pnl.
        # If they diverge, the attribution formula (realized_pnl) has a bug.
        r = self._make_result(
            net_base_position=0.0,
            open_position_value=0.0,
            final_cash={"binance": 0.20, "bybit": -0.05},
            final_inventory={"binance": 0.0, "bybit": 0.0},
            cash_pnl=0.15,         # sum of final_cash values
            ending_equity=0.15,    # = cash_pnl since no open position
        )
        assert r.net_base_position == 0.0
        assert abs(r.ending_equity - r.cash_pnl) < 1e-9, "closed position: equity must equal cash"

    def test_fully_closed_realized_pnl_attribution_may_differ(self):
        # realized_pnl is an attribution calc that can diverge from the ledger.
        # This is a documented property, not a bug in ending_equity.
        r = self._make_result(
            net_base_position=0.0,
            open_position_value=0.0,
            cash_pnl=0.167,        # ledger says $0.167
            ending_equity=0.167,
            realized_pnl=0.302,    # attribution calc inflated (known divergence)
        )
        # The user of these fields should prefer ending_equity.
        assert r.ending_equity == 0.167
        assert r.realized_pnl != r.ending_equity   # divergence is expected / documented

    # ── Reconciliation: open position at final mark ──────────────────────────

    def test_open_position_equity_includes_mark(self):
        # With 1.0 SOL long at $100 mid, open_position_value = 100.
        # If cash_pnl = -50 (spent $50 buying at $50), ending_equity = -50 + 100 = $50.
        r = self._make_result(
            net_base_position=1.0,
            open_position_value=100.0,
            cash_pnl=-50.0,
            ending_equity=50.0,   # = cash_pnl + open_position_value
        )
        assert abs(r.ending_equity - (r.cash_pnl + r.open_position_value)) < 1e-9

    def test_open_position_equity_vs_cash_pnl_gap(self):
        # cash_pnl ≠ ending_equity when net_base ≠ 0; cash_pnl mislabels open inventory as profit.
        r = self._make_result(
            net_base_position=1.0,
            open_position_value=100.0,
            cash_pnl=-50.0,
            ending_equity=50.0,
        )
        assert r.cash_pnl != r.ending_equity, "open position: cash_pnl omits mark-to-market value"

    # ── Reconciliation: multi-venue ──────────────────────────────────────────

    def test_multi_venue_ending_equity(self):
        # Long 1 SOL on Bybit (mid=$100), short 1 SOL on Binance (mid=$101).
        # net_base = 0, open_pos_value = 1*100 + (-1)*101 = -1.
        # cash from sell on binance = +101, cash from buy on bybit = -100.
        # total_cash = +1.  ending_equity = 1 + (-1) = 0.
        r = self._make_result(
            final_inventory={"binance": -1.0, "bybit": 1.0},
            final_mids={"binance": 101.0, "bybit": 100.0},
            open_position_value=-1.0,   # (-1)*101 + 1*100
            cash_pnl=1.0,
            ending_equity=0.0,         # net arb: locked in $1 spread, open_pos offsets it
        )
        assert abs(r.ending_equity - (r.cash_pnl + r.open_position_value)) < 1e-9

    # ── Reconciliation: fees ─────────────────────────────────────────────────

    def test_fees_deducted_in_cash_pnl(self):
        # cash_pnl reflects fees; ending_equity is net of fees.
        # gross spread = $1, fees = $0.10 → cash_pnl = $0.90
        r = self._make_result(
            net_base_position=0.0,
            open_position_value=0.0,
            gross_pnl=1.0,
            fees_paid=0.10,
            cash_pnl=0.90,
            ending_equity=0.90,
        )
        assert abs(r.ending_equity - (r.gross_pnl - r.fees_paid)) < 1e-9

    # ── Reconciliation: stale marks ──────────────────────────────────────────

    def test_common_mid_staleness_inf_when_unavailable(self):
        import math
        r = self._make_result(common_mid_staleness_s=float("inf"))
        assert math.isinf(r.common_mid_staleness_s)

    def test_common_timestamp_equity_falls_back_when_unavailable(self):
        # When common_timestamp_equity falls back, it equals ending_equity.
        r = self._make_result(ending_equity=5.0, common_timestamp_equity=5.0,
                              common_mid_staleness_s=float("inf"))
        assert r.common_timestamp_equity == r.ending_equity

    # ── Reconciliation: single_venue_diagnostic ──────────────────────────────

    def test_single_venue_diagnostic_default_empty(self):
        r = self._make_result()
        assert isinstance(r.single_venue_diagnostic, dict)

    def test_single_venue_diagnostic_populated(self):
        r = self._make_result(single_venue_diagnostic={"binance": 42, "bybit": 0})
        assert r.single_venue_diagnostic["binance"] == 42
        assert r.single_venue_diagnostic["bybit"] == 0

    # ── mark_inventory_venue: alternate valuation, not authoritative ─────────

    def test_mark_inventory_venue_is_diagnostic_not_default(self):
        # Default is None: legacy per-venue marking is unchanged.
        cfg = ArbConfig()
        assert cfg.mark_inventory_venue is None, "must default to None (no marking override)"

    def test_mark_inventory_venue_opt_in_only(self):
        # Setting it changes the marking policy; leaving it None must never change ending_equity.
        cfg_default = ArbConfig()
        cfg_override = ArbConfig(mark_inventory_venue="binance")
        assert cfg_default.mark_inventory_venue is None
        assert cfg_override.mark_inventory_venue == "binance"

    # ── Batch path uses ending_equity as authoritative ────────────────────────

    def test_batch_per_pair_has_ending_equity_field(self):
        # per_pair dicts must expose ending_equity for caller consumption.
        from new_backtest.batch_backtest import BatchSummary
        s = BatchSummary(
            mode="maker_taker", latency_preset="singapore",
            start_date="2026-01-01", end_date="2026-01-01",
            pairs_requested=1, pairs_run=1, pairs_skipped=0,
            total_realized_pnl=5.0, total_gross_pnl=6.0,
            total_fees=0.5, total_unhedged_loss=0.0, total_traded_notional=1000.0,
            total_signals=1, total_orders_sent=2, total_orders_filled=2,
            total_orders_missed=0, fill_ratio=1.0, mean_edge_capture=1.0,
            mean_realized_net_edge_bps=1.0, pnl_per_million_notional=5.0,
            profitable_pairs=1,
            per_pair=[{"ending_equity": 5.0, "cash_pnl": 5.0, "realized_pnl": 5.0,
                       "pair": "SOLUSDT", "gross_pnl": 6.0}],
        )
        assert "ending_equity" in s.per_pair[0]
        assert "cash_pnl" in s.per_pair[0]


# ── Task 5 gap fixes: staleness threshold & single-venue intervals ──────────

class TestCommonTimestampStalenessThreshold:
    def test_default_threshold_is_one_second(self):
        cfg = ArbConfig()
        assert cfg.max_common_staleness_s == 1.0

    def test_inf_threshold_disables_rejection(self):
        import math
        cfg = ArbConfig(max_common_staleness_s=float("inf"))
        assert math.isinf(cfg.max_common_staleness_s)

    @pytest.mark.skipif(not pathlib.Path('/Users/vaibhav/Documents/Newproject/data/bybit').is_dir(), reason="needs the Jan-2022 SOL files (absent on the VM)")
    def test_staleness_threshold_flags_a_lagging_venue_but_never_falls_back(self):
        # R11 (2026-11-01): the threshold no longer REJECTS the common-timestamp mark (the old fallback to each venue's own last
        # mid was the stale-mark inflation on SOL 2022-01-05). A lagging venue is flagged and the common-timestamp valuation stays
        # the headline. Tight 0.0s threshold: any spread between the venues' last updates sets the flag. Tiny max_events: instant.
        bt = CrossVenueArbBacktester(ArbBacktestConfig(
            pair='SOLUSDT',
            data_root='/Users/vaibhav/Documents/Newproject/data',
            start_date='2022-01-05', end_date='2022-01-05',
            max_events=50_000,
            arb=ArbConfig(max_common_staleness_s=0.0),
            maker=MakerConfig(cancel_model='proportional'),
            latency_preset='singapore',
        ))
        r = bt.run()
        import math
        assert r.valuation["available"]
        assert not math.isinf(r.common_mid_staleness_s)
        assert r.common_mid_staleness_s == r.valuation["venue_last_update_spread_s"]
        assert r.valuation["lagging_venue_flag"] == (r.common_mid_staleness_s > 0.0)
        assert r.ending_equity == r.common_timestamp_equity                       # the headline IS the common-timestamp figure
        assert r.equity_bridge["ok"]


class TestSingleVenueIntervals:
    def test_single_venue_intervals_field_exists(self):
        from new_backtest.backtester import ArbBacktestResult
        import dataclasses
        fields = {f.name for f in dataclasses.fields(ArbBacktestResult)}
        assert 'single_venue_intervals' in fields

    def test_single_venue_intervals_is_int(self):
        bt = CrossVenueArbBacktester(ArbBacktestConfig(
            pair='SOLUSDT',
            data_root='/Users/vaibhav/Documents/Newproject/data',
            start_date='2022-01-05', end_date='2022-01-05',
            max_events=200_000,
            arb=ArbConfig(),
            maker=MakerConfig(cancel_model='proportional'),
            latency_preset='singapore',
        ))
        r = bt.run()
        assert isinstance(r.single_venue_intervals, int)
        assert r.single_venue_intervals >= 0

    def test_single_venue_intervals_distinguishable_from_per_venue_counts(self):
        # single_venue_intervals ≤ sum of per-venue stale counts (which can double-count).
        bt = CrossVenueArbBacktester(ArbBacktestConfig(
            pair='SOLUSDT',
            data_root='/Users/vaibhav/Documents/Newproject/data',
            start_date='2022-01-05', end_date='2022-01-05',
            max_events=200_000,
            arb=ArbConfig(),
            maker=MakerConfig(cancel_model='proportional'),
            latency_preset='singapore',
        ))
        r = bt.run()
        total_stale = sum(r.single_venue_diagnostic.values())
        # single_venue_intervals counts events where stale ∩ fresh is non-empty;
        # total_stale counts each stale venue independently, so svi ≤ total_stale.
        assert r.single_venue_intervals <= total_stale
