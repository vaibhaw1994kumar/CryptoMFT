"""Defect #6: position limits ignore pending orders when several live orders are allowed.

Backtests now switch ArbConfig.count_pending_exposure on by themselves whenever maker_max_live_orders > 1
(ArbBacktestConfig.auto_pending_exposure, default True). ArbConfig's own default stays False: anything that builds an ArbConfig
outside a backtest sees no change. Tests that exercise the legacy behaviour pin auto_pending_exposure=False.

Limit semantics, measured here rather than assumed. The field comment says max_position_base is "|net base across venues|", but
CrossVenueArbStrategy._inventory_room allows twice that on the net (`net_room = 2*max_position_base - ...`) and twice that per
venue. test_inflight_order_tracking.py already treats "a net limit of 2.0" for max_position_base=1.0 as the intended rule.
"""
from __future__ import annotations

import warnings

import pytest

from new_backtest.arb_strategy import ArbConfig

from test_inflight_order_tracking import BINANCE, BYBIT, decide, market, warm
from test_maker_order_lifetime import Harness

warnings.simplefilter("ignore")


def stack(*, limit, clip, max_live, flag, auto=False, n=12):
    """Up to `n` fresh signals, one every 10 ms, entry latency 1 s: every order is still in flight (pending) when the next is evaluated."""
    h = Harness(ttl=0.0, entry_s=1.0, max_live=max_live, max_clip_base=clip, max_position_base=limit, count_pending_exposure=flag,
                cfg_kw={"auto_pending_exposure": auto})
    warm(h)
    sizes = []
    for i in range(n):
        t = 0.01 + i * 0.01
        market(h, t)
        sig, _ = decide(h, t)
        if sig is not None:
            sizes.append(round(sig.qty, 9))
    return h, sizes


def fill_all_together(h):
    """Every order lands (t=1.0+) and one large sell print fills all of them at the same instant; returns the long base on bybit."""
    h.advance(1.2)
    market(h, 1.3)
    h.sell_trade(1.31, BYBIT, volume=1_000.0, price=100.91)
    return h.bt.inventory.base[BYBIT]


# ── the switch ───────────────────────────────────────────────────────────────

def test_backtests_that_allow_several_live_orders_switch_the_check_on_by_themselves():
    h = Harness(ttl=0.0, max_live=3)
    assert h.bt.config.arb.count_pending_exposure is True and h.bt.pending_exposure_auto_enabled is True
    assert h.bt.model_settings()["pending_exposure_auto_enabled"] is True


def test_one_live_order_changes_nothing():
    h = Harness(ttl=0.0, max_live=1)
    assert h.bt.config.arb.count_pending_exposure is False and h.bt.pending_exposure_auto_enabled is False


def test_the_legacy_setting_can_be_pinned():
    h = Harness(ttl=0.0, max_live=3, cfg_kw={"auto_pending_exposure": False})
    assert h.bt.config.arb.count_pending_exposure is False


def test_an_explicit_true_is_left_alone_and_not_reported_as_automatic():
    h = Harness(ttl=0.0, max_live=3, count_pending_exposure=True)
    assert h.bt.config.arb.count_pending_exposure is True and h.bt.pending_exposure_auto_enabled is False


def test_the_strategy_default_that_other_callers_see_is_unchanged():
    assert ArbConfig().count_pending_exposure is False


# ── the scenario from the brief: limit 10, three resting buys of 4 that all fill together ─────────

@pytest.mark.xfail(strict=True, reason="the check is on, but the net it enforces is 2 x max_position_base = 20, not the documented 10: "
                                       "three buys of 4 (12) are all accepted. Decision for the owner: is 2x intended? (listed in the report)")
def test_limit_10_three_buys_of_4_are_limited_to_the_documented_limit_when_the_check_is_on():
    h, sizes = stack(limit=10.0, clip=4.0, max_live=3, flag=True)
    assert sum(sizes) <= 10.0, f"accepted {sizes}"


def test_limit_10_three_buys_of_4_with_the_check_on_stay_inside_the_net_limit_the_code_enforces():
    h, sizes = stack(limit=10.0, clip=4.0, max_live=3, flag=True)
    assert sizes == [4.0, 4.0, 4.0]
    assert sum(sizes) <= 2 * 10.0
    assert fill_all_together(h) == pytest.approx(12.0)


def test_with_the_check_off_the_same_stack_is_a_documented_breach_of_what_the_code_would_otherwise_allow():
    """Check off, room for six orders: six buys of 4 are accepted (24) and all fill together, past even the 2 x 10 the check allows."""
    h, sizes = stack(limit=10.0, clip=4.0, max_live=6, flag=False)
    assert sizes == [4.0] * 6
    long_base = fill_all_together(h)
    assert long_base == pytest.approx(24.0)
    assert long_base > 2 * 10.0, "BREACH with the check off"


def test_with_the_check_on_the_same_stack_is_cut_at_the_net_limit_the_code_enforces():
    h, sizes = stack(limit=10.0, clip=4.0, max_live=6, flag=True)
    assert sizes == [4.0] * 5, sizes
    long_base = fill_all_together(h)
    assert long_base == pytest.approx(20.0)
    assert long_base <= 2 * 10.0 + 1e-9


def test_the_automatic_switch_gives_the_same_stack_as_setting_the_flag_by_hand():
    _, by_hand = stack(limit=10.0, clip=4.0, max_live=6, flag=True)
    _, auto = stack(limit=10.0, clip=4.0, max_live=6, flag=False, auto=True)
    assert auto == by_hand
