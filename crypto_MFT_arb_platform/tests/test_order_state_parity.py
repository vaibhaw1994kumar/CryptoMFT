"""Lifecycle parity: the same event script through the written specification, the core OMS and the backtester's maker lifecycle.

Each (scenario, system) pair is compared with the specification (new_backtest.order_state_machine). A known difference is
listed in DISAGREEMENTS with an id and is an xfail(strict=True): the test FAILS if the two ever start to agree (someone
resolved it: delete the entry) and also fails for any difference that is not listed. Nothing here changes either
implementation; resolving each item is the owner's decision (see the report).

Baseline: measured on the unmodified OMS and MakerExecutor before the PostOnly-reject and fault-injection work.
"""
from __future__ import annotations

import functools
import itertools
import warnings
from pathlib import Path

import pytest

from new_backtest.order_state_machine import ALLOWED_TRANSITIONS, TERMINAL, Kind, State, replay, resolve
from parity_adapters import NotRepresentable, Outcome, run_maker, run_oms, run_spec
from parity_scenarios import SCENARIOS

def _perp_cfg():
    """The maker lifecycle settings of the perp profile that matter here (read from the profile, so the test follows it)."""
    from new_backtest.profiles import load_profile
    doc = load_profile(Path(__file__).resolve().parents[1] / "profiles" / "perp_perp_maker_taker_v1.yaml")
    assert doc["postonly_reject_model"] == "on" and doc["cancel_time_basis"] == "exchange"
    return {"postonly_reject_model": doc["postonly_reject_model"], "cancel_time_basis": doc["cancel_time_basis"],
            "postonly_notice_s": 0.0}      # the script pins the time the strategy learns of a reject


# "maker"      = the engine with its defaults, which is what legacy_v0 runs (kept bit-identical on purpose)
# "maker_perp" = the same lifecycle code with the settings perp_perp_maker_taker_v1 turns on: PostOnly rejects (D-01), exchange-time cancel
#                basis (D-03) and the explicit UNKNOWN state (D-02). No strict xfail may remain in this column.
SYSTEMS = {"oms": run_oms, "maker": run_maker, "maker_perp": functools.partial(run_maker, cfg_kw=_perp_cfg(), unknown_model=True)}

# (scenario, system) -> (id, what differs).  Only the legacy-default column ("maker") keeps any: they are the documented reasons the perp profile
# switches the corresponding features on, not open defects.
DISAGREEMENTS = {
    ("reject", "maker"): ("D-01", "MakerExecutor/backtester cannot reject a passive order: every order that arrives is accepted and rests"),
    ("reject_after_partial_fill", "maker"): ("D-01", "MakerExecutor/backtester cannot reject a passive order, so a late reject cannot even be expressed"),
    ("ack_timeout_then_fill", "maker"): ("D-02", "no acknowledgement exists in the simulator, so no acknowledgement timeout and no unknown state (an injected ack loss exists, hedge_faults.ack_unknown_prob, but it has no explicit state)"),
    ("ack_timeout_then_ack", "maker"): ("D-02", "no acknowledgement exists in the simulator, so no acknowledgement timeout and no unknown state"),
    ("ack_timeout_unresolved", "maker"): ("D-02", "no acknowledgement exists in the simulator, so no acknowledgement timeout and no unknown state"),
    ("late_fill_after_cancel", "maker"): ("D-03", "a fill executed before the cancel took effect but received after it is dropped: the order is removed on receive time"),
}
# Resolved by OMS02 (crypto_stat_arb.oms._resolve_order_state, ported 2026-11-01): D-04 (a timeout that is older than the venue's
# evidence does not win: a late ack clears 'unknown'), D-05 (OMS.note_cancel_requested -> OrderState.CANCEL_PENDING), D-06 (a reject
# after a fill keeps the fill and re-queries; ledger REJECT_AFTER_FILL_ALARM once per order). Their scenarios now have to agree with
# the specification: a regression here fails outright.


def _params():
    for name in SCENARIOS:
        for system in SYSTEMS:
            mark = []
            if (name, system) in DISAGREEMENTS:
                did, why = DISAGREEMENTS[(name, system)]
                mark = [pytest.mark.xfail(strict=True, reason=f"{did}: {why}")]
            yield pytest.param(name, system, marks=mark, id=f"{name}-{system}")


@pytest.mark.parametrize("scenario,system", list(_params()))
def test_matches_specification(scenario, system):
    events = SCENARIOS[scenario]
    expected = run_spec(events)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            got = SYSTEMS[system](events)
        except NotRepresentable as exc:
            pytest.fail(f"not representable: {exc}")
    assert (got.state, got.booked, got.fills) == (expected.state, expected.booked, expected.fills)
    assert got.booked_at == expected.booked_at, "same fills, booked at different times"


def _maker_with(cfg_kw, scenario):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return run_maker(SCENARIOS[scenario], cfg_kw=cfg_kw)


def test_d03_is_resolved_by_the_exchange_time_cancel_basis():
    """cancel_time_basis="exchange": a fill that executed before the cancel took effect is kept even if delivered after it."""
    expected = run_spec(SCENARIOS["late_fill_after_cancel"])
    got = _maker_with({"cancel_time_basis": "exchange"}, "late_fill_after_cancel")
    assert (got.state, got.booked, got.fills) == (expected.state, expected.booked, expected.fills)


def test_d03_stays_when_the_switch_is_off():
    assert _maker_with({}, "late_fill_after_cancel").booked == 0.0


def test_d01_a_postonly_reject_is_now_expressible_with_the_postonly_model_on():
    expected = run_spec(SCENARIOS["reject"])
    got = _maker_with({"postonly_reject_model": "on", "postonly_notice_s": 0.0}, "reject")
    assert (got.state, got.booked, got.fills) == (expected.state, expected.booked, expected.fills)


def test_d01_reject_after_a_fill_is_expressible_in_the_perp_configuration_and_keeps_the_fill():
    expected = run_spec(SCENARIOS["reject_after_partial_fill"])
    got = SYSTEMS["maker_perp"](SCENARIOS["reject_after_partial_fill"])
    assert (got.state, got.booked, got.fills) == (expected.state, expected.booked, expected.fills) == ("partially_filled", 0.4, (0.4,))


def test_d01_with_the_postonly_model_off_a_reject_is_not_representable():
    with pytest.raises(NotRepresentable):
        run_maker(SCENARIOS["reject_after_partial_fill"], cfg_kw={})


@pytest.mark.parametrize("scenario,state", [("ack_timeout_then_fill", "filled"), ("ack_timeout_then_ack", "resting"), ("ack_timeout_unresolved", "unknown")])
def test_d02_unknown_is_an_explicit_state_and_agrees_with_the_specification(scenario, state):
    got = SYSTEMS["maker_perp"](SCENARIOS[scenario])
    assert got.state == state == run_spec(SCENARIOS[scenario]).state


def test_d02_without_the_unknown_model_a_timeout_is_not_representable():
    with pytest.raises(NotRepresentable):
        run_maker(SCENARIOS["ack_timeout_unresolved"])


def test_the_perp_column_has_no_listed_disagreement():
    assert not [k for k in DISAGREEMENTS if k[1] == "maker_perp"]


def test_a_reject_notice_is_counted_once_and_does_not_double_book():
    """venue_reject_notice twice for the same order: one alarm, the fill booked once."""
    from test_maker_order_lifetime import BINANCE, BYBIT, Harness
    from new_backtest.event_loader import TRADE
    h = Harness(ttl=0.0, entry_s=0.003, cancel_s=1e6, queue=0.0, max_live=4, cfg_kw=_perp_cfg())
    h.bt.latency.aligner.samples = 1
    h.book(-1.0, BYBIT); h.book(-1.0, BINANCE)
    h.send_maker(0.0, qty=2.0, price=100.0)
    h.event(0.003, TRADE, BYBIT, {"price": 150.0, "volume": 0.001, "side": "b"}, exchange_ts=0.003)
    h.sell_trade(0.5, BYBIT, volume=0.8, price=100.0, exchange_ts=0.5)
    mo = next(iter(h.bt.maker.orders.values()))
    assert h.bt.venue_reject_notice(mo.order_id, 1.0) == "partially_filled"
    assert h.bt.venue_reject_notice(mo.order_id, 1.1) == "partially_filled"
    assert h.bt.hedge["reject_after_fill_alarms"] == 1
    assert sum(r["qty"] for r in h.bt.trade_log if r["venue"] == BYBIT and not r["taker"]) == pytest.approx(0.8)


def test_every_disagreement_refers_to_a_real_scenario():
    for scenario, system in DISAGREEMENTS:
        assert scenario in SCENARIOS and system in SYSTEMS


# ── the specification itself ─────────────────────────────────────────────────

def test_terminal_states_have_no_exits_except_a_late_fill_completing_a_cancel():
    assert ALLOWED_TRANSITIONS[State.FILLED] == frozenset() and ALLOWED_TRANSITIONS[State.REJECTED] == frozenset()
    assert ALLOWED_TRANSITIONS[State.CANCELLED] == frozenset({State.FILLED})
    assert TERMINAL == {State.FILLED, State.REJECTED, State.CANCELLED}


def test_no_state_transitions_to_a_state_the_table_does_not_know():
    for src, dsts in ALLOWED_TRANSITIONS.items():
        assert set(dsts) <= set(State)


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_end_state_and_booked_quantity_do_not_depend_on_arrival_order(scenario):
    events = SCENARIOS[scenario]
    base = (resolve(events), replay(events).booked)
    for perm in itertools.islice(itertools.permutations(events), 720):
        assert (resolve(perm), replay(perm).booked) == base


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_scripts_in_arrival_order_take_only_allowed_transitions(scenario):
    events = sorted(SCENARIOS[scenario], key=lambda e: e.arrive)
    bad = replay(events).violations
    allowed_violations = {"reject_after_partial_fill"}      # a deliberate protocol violation
    if scenario not in allowed_violations:
        assert bad == [], bad


def test_fills_are_booked_once_however_often_they_are_reported():
    from new_backtest.order_state_machine import Event
    ev = [Event(Kind.FILL, 0.5, 0.5, cum=0.4)] * 5 + [Event(Kind.FILL, 0.4, 0.9, cum=0.3)]
    r = replay(ev)
    assert r.booked == 0.4 and r.fills == [0.4]


def test_timeout_never_overrides_evidence_already_held():
    from new_backtest.order_state_machine import Event
    ev = [Event(Kind.SUBMIT, 0, 0), Event(Kind.ACK, 0.1, 0.1), Event(Kind.TIMEOUT, 2.0, 2.0)]
    assert resolve(ev) == State.RESTING


def test_oms_and_maker_agree_with_each_other_wherever_both_can_express_the_script():
    rows = []
    for name, ev in SCENARIOS.items():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                a, b = run_oms(ev), run_maker(ev)
            except NotRepresentable:
                continue
        if (a.state, a.booked, a.fills) != (b.state, b.booked, b.fills):
            rows.append((name, a, b))
    listed = {s for (s, sys_) in DISAGREEMENTS if sys_ in ("oms", "maker")}
    assert {r[0] for r in rows} <= listed, rows
