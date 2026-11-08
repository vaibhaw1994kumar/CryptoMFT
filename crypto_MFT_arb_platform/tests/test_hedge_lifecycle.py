"""Phase C: the hedge lifecycle state machine (crypto_stat_arb/hedge_lifecycle.py). Rules only: nothing here uses a venue, a clock or a simulator."""
import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb.ems.intent import TRANSITIONS as INTENT_TRANSITIONS, IntentState
from crypto_stat_arb.hedge_lifecycle import (EXPOSED, INTENT_TO_HEDGE, PLAN_TO_HEDGE, TERMINAL, TRANSITIONS, HedgeLifecycle, HedgeState as H,
                                             IllegalHedgeTransition, hedge_state_of_plan, hedge_states_of_intent)
from crypto_stat_arb.models import PlanState


def chain(**kw):
    base = dict(chain_id="c1", exposure_qty=1.0, retry_limit=2, unwind_attempt_limit=3, max_residual_age_s=5.0)
    base.update(kw)
    return HedgeLifecycle(**base)


def to_submitted(c, t=0.0):
    c.move(H.HEDGE_PENDING, t)
    c.move(H.SUBMITTED, t + 0.01)


# ---- the table -------------------------------------------------------------------------------------------

def test_every_state_has_a_row_and_only_reconciled_is_terminal():
    assert set(TRANSITIONS) == set(H)
    assert TERMINAL == {H.RECONCILED}
    assert TRANSITIONS[H.RECONCILED] == frozenset()
    for frm, tos in TRANSITIONS.items():
        assert frm not in tos or frm in (H.PARTIALLY_FILLED, H.UNWINDING)      # only a partial fill and an unwind attempt may repeat


@pytest.mark.parametrize("frm,to", [(a, b) for a, b in itertools.product(H, H) if b not in TRANSITIONS[a]])
def test_every_move_not_in_the_table_raises(frm, to):
    c = chain()
    c.state = frm
    with pytest.raises(IllegalHedgeTransition):
        c.move(to, 1.0)
    assert c.state is frm                                                      # a refused move changes nothing


def test_the_normal_path_hedges_reconciles_and_measures_the_exposure():
    c = chain()
    to_submitted(c)
    c.move(H.PARTIALLY_FILLED, 0.05, filled_qty=0.4)
    c.move(H.PARTIALLY_FILLED, 0.08, filled_qty=0.7)
    c.move(H.HEDGED, 0.20, filled_qty=1.0)
    c.move(H.RECONCILED, 0.30)
    r = c.as_record()
    assert (r["outcome"], r["partial_fills"], r["retries"], r["timeouts"]) == ("hedged_reconciled", 2, 0, 0)
    assert r["residual_exposure_duration_s"] == pytest.approx(0.20)           # from the maker fill to the moment the last of it was hedged


def test_hedged_with_a_residual_and_a_quantity_past_the_exposure_are_refused_and_change_nothing():
    c = chain()
    to_submitted(c)
    with pytest.raises(IllegalHedgeTransition):
        c.move(H.HEDGED, 0.1, filled_qty=0.6)
    with pytest.raises(IllegalHedgeTransition):
        c.move(H.PARTIALLY_FILLED, 0.1, filled_qty=1.5)
    c.move(H.PARTIALLY_FILLED, 0.1, filled_qty=0.5)
    with pytest.raises(IllegalHedgeTransition):
        c.move(H.PARTIALLY_FILLED, 0.2, filled_qty=0.3)                        # a fill can never un-fill
    assert c.hedged_qty == pytest.approx(0.5) and c.retries == 0


# ---- retry needs a classified reject that permits it (item 3) and has a budget (item 5) --------------------

def test_a_retry_needs_a_reject_that_permits_it():
    c = chain()
    to_submitted(c)
    c.move(H.REJECTED, 0.1)
    c.note_reject(0.1, "margin_insufficient", False)
    with pytest.raises(IllegalHedgeTransition, match="does not permit"):
        c.move(H.RETRYING, 0.35)
    c.move(H.UNWINDING, 0.2)                                                   # not retryable: the hedge goes to the unwind path, it is not dropped
    assert c.retries == 0


def test_retries_are_bounded_and_the_intervals_are_recorded():
    c = chain(retry_limit=2)
    to_submitted(c)
    c.move(H.REJECTED, 1.0); c.note_reject(1.0, "rate_limit", True)
    c.move(H.RETRYING, 1.25); c.move(H.SUBMITTED, 1.26)
    c.move(H.REJECTED, 1.5); c.note_reject(1.5, "rate_limit", True)
    c.move(H.RETRYING, 1.75); c.move(H.SUBMITTED, 1.76)
    c.move(H.REJECTED, 2.0); c.note_reject(2.0, "rate_limit", True)
    with pytest.raises(IllegalHedgeTransition, match="retry limit"):
        c.move(H.RETRYING, 2.25)
    assert c.retry_intervals_s == pytest.approx([0.26, 0.26]) and c.retries == 2      # failure known -> retry sent
    assert c.reject_labels == ["rate_limit"] * 3
    c.move(H.UNWINDING, 2.1)


# ---- unknown: query first; an unacknowledged order is never assumed failed (item 4) ------------------------

def test_unknown_resolves_only_through_a_query_result_that_says_so():
    c = chain()
    to_submitted(c)
    c.move(H.UNKNOWN, 1.0)
    for to in (H.RETRYING, H.REJECTED, H.HEDGED, H.SUBMITTED):
        with pytest.raises(IllegalHedgeTransition):
            c.move(to, 1.1, filled_qty=1.0 if to is H.HEDGED else None)       # no query yet: nothing may be assumed
    c.note_query("found_executed")
    with pytest.raises(IllegalHedgeTransition):
        c.move(H.RETRYING, 1.2)                                                # the venue has it: a resend would double the hedge
    c.move(H.HEDGED, 1.2, filled_qty=1.0)
    assert c.timeouts == 1 and c.retries == 0


def test_a_query_that_finds_nothing_allows_exactly_one_kind_of_replacement():
    c = chain()
    to_submitted(c)
    c.move(H.UNKNOWN, 1.0)
    c.note_query("not_found")
    with pytest.raises(IllegalHedgeTransition):
        c.move(H.HEDGED, 1.1, filled_qty=1.0)
    c.move(H.RETRYING, 1.1)
    c.move(H.SUBMITTED, 1.11)


def test_a_live_order_found_by_the_query_goes_back_to_submitted_not_to_a_new_order():
    c = chain()
    to_submitted(c)
    c.move(H.UNKNOWN, 1.0)
    c.note_query("found_live")
    with pytest.raises(IllegalHedgeTransition):
        c.move(H.RETRYING, 1.1)
    c.move(H.SUBMITTED, 1.1)


def test_unknown_goes_to_failed_only_after_the_query_attempts_are_used_up():
    c = chain(query_attempt_limit=3)
    to_submitted(c)
    c.move(H.UNKNOWN, 1.0)
    for i in range(2):
        c.note_query("failed")
    with pytest.raises(IllegalHedgeTransition, match="failed venue queries"):
        c.move(H.FAILED, 2.0)
    c.note_query("failed")
    c.move(H.FAILED, 3.0)
    assert c.outcome == "failed_alarm"


# ---- escalation, unwind budget, alarm, reset (items 5 and 6) ------------------------------------------------

def test_the_residual_age_limit_is_a_rule_of_the_chain():
    c = chain(max_residual_age_s=5.0, opened_ts=10.0)
    to_submitted(c, 10.0)
    assert not c.residual_age_breached(14.9)
    assert c.residual_age_breached(15.1)
    c.move(H.ESCALATED, 15.1, "residual age")
    c.move(H.UNWINDING, 15.2)
    c.move(H.RECONCILED, 15.5, unwound_qty=1.0)
    assert c.residual_exposure_duration_s == pytest.approx(5.5) and c.outcome == "unwound_reconciled"
    assert not c.residual_age_breached(99.0)                                   # closed exposure is never "aged"


def test_a_chain_without_an_age_limit_never_reports_a_breach():
    c = chain(max_residual_age_s=None)
    to_submitted(c)
    assert not c.residual_age_breached(10_000.0)


def test_the_unwind_budget_is_one_setting_and_its_exhaustion_is_the_alarm_path():
    c = chain(unwind_attempt_limit=2)
    to_submitted(c)
    c.move(H.REJECTED, 0.1); c.note_reject(0.1, "unclassified", False)
    c.move(H.UNWINDING, 0.2)
    c.move(H.UNWINDING, 0.5)                                                   # second attempt
    with pytest.raises(IllegalHedgeTransition, match="unwind budget"):
        c.move(H.UNWINDING, 0.8)
    c.move(H.FAILED, 0.8)
    assert c.unwind_attempts == 2
    c.move(H.RECONCILED, 9.0)                                                  # operator reset after reconciliation: the only way out of FAILED


def test_reconciled_with_an_open_residual_is_refused_except_by_the_operator_reset():
    c = chain()
    to_submitted(c)
    c.move(H.REJECTED, 0.1); c.note_reject(0.1, "unclassified", False)
    c.move(H.UNWINDING, 0.2)
    with pytest.raises(IllegalHedgeTransition, match="open residual"):
        c.move(H.RECONCILED, 0.4)
    c.move(H.RECONCILED, 0.4, unwound_qty=1.0)
    assert c.residual_qty == 0.0


def test_exposed_states_are_exactly_those_with_something_still_open():
    assert H.HEDGED not in EXPOSED and H.RECONCILED not in EXPOSED
    assert {H.MAKER_FILLED, H.HEDGE_PENDING, H.UNKNOWN, H.UNWINDING, H.FAILED} <= EXPOSED


# ---- the older vocabularies map onto this one ---------------------------------------------------------------

def test_every_hedge_stage_plan_state_maps_and_the_map_is_a_function_into_the_table():
    pre_hedge = {PlanState.CREATED, PlanState.RISK_RESERVED, PlanState.DISPATCHED}      # before any fill: no hedge meaning yet
    assert set(PlanState) - set(PLAN_TO_HEDGE) == pre_hedge
    assert all(hedge_state_of_plan(p) in set(H) for p in PLAN_TO_HEDGE)
    assert hedge_state_of_plan(PlanState.HEDGE_UNWIND) is H.UNWINDING and hedge_state_of_plan(PlanState.TIMED_OUT) is H.UNKNOWN


def test_every_intent_state_maps_and_every_intent_transition_has_a_path_in_the_hedge_table():
    assert set(INTENT_TO_HEDGE) == {s.value for s in IntentState}

    def reach(a, b, depth):
        return a is b or any(reach(m, b, depth - 1) for m in TRANSITIONS[a]) if depth else a is b

    for frm, tos in INTENT_TRANSITIONS.items():
        for to in tos:
            assert any(reach(ha, hb, 2) for ha in hedge_states_of_intent(frm) for hb in hedge_states_of_intent(to)), (frm, to)


# ---- the 40 catalogue cases (tests/integration_hedge_failure_catalogue.json) checked against the transition table -------------------------------------
# Each case that is a hedge-chain event has the path the core takes (evidence supplied where the table asks for it); every other case is outside the hedge chain,
# with the reason and the place that owns it. Cases that describe a backtester-only behaviour (a carried hedge, the instant-flatten oracle) are written as the core path.
import json

P = [H.MAKER_FILLED, H.HEDGE_PENDING, H.SUBMITTED]
CASE_PATHS = {
    "H01": P + [H.REJECTED, H.UNWINDING, H.RECONCILED],                       # the core never carries a failed leg: it unwinds
    "H02": P + [H.REJECTED, H.UNWINDING, H.RECONCILED],
    "H03": P + [H.REJECTED, H.RETRYING, H.SUBMITTED, H.REJECTED, H.RETRYING, H.SUBMITTED, H.REJECTED, H.UNWINDING, H.RECONCILED],
    "H04": P + [H.REJECTED, H.RETRYING, H.SUBMITTED, H.HEDGED, H.RECONCILED],
    "H06": [H.MAKER_FILLED, H.HEDGE_PENDING, H.ESCALATED, H.UNWINDING, H.RECONCILED],      # sent too late: the book moved through the limit, the age limit escalates
    "H07": P + [H.PARTIALLY_FILLED, H.UNWINDING, H.RECONCILED],
    "H08": P + [H.UNKNOWN, H.RETRYING, H.SUBMITTED, H.HEDGED, H.RECONCILED],                # blackhole: the query found nothing, one replacement
    "H09": P + [H.UNKNOWN, H.HEDGED, H.RECONCILED],                                          # the 'ghost' is found by the query, never over-hedged
    "H10": P + [H.HEDGED, H.RECONCILED],
    "H11": [H.MAKER_FILLED, H.HEDGE_PENDING, H.REJECTED, H.RETRYING, H.SUBMITTED, H.HEDGED, H.RECONCILED],
    "H12": P + [H.HEDGED, H.RECONCILED],
    "H15": P + [H.UNKNOWN, H.HEDGED, H.RECONCILED],
    "H16": P + [H.UNKNOWN, H.FAILED],
    "H18": P + [H.REJECTED, H.UNWINDING, H.UNWINDING, H.UNWINDING, H.FAILED],
    "H19": P + [H.REJECTED, H.UNWINDING, H.FAILED],
    "H20": P + [H.REJECTED, H.UNWINDING, H.UNWINDING, H.RECONCILED],
    "H21": P + [H.REJECTED, H.UNWINDING, H.FAILED],
    "H22": P + [H.ESCALATED, H.UNWINDING, H.FAILED],
    "H24": P + [H.HEDGED, H.RECONCILED],                                                     # a fill inside the cancel window is kept and hedged
    "H26": P + [H.REJECTED, H.RETRYING, H.SUBMITTED, H.HEDGED, H.RECONCILED],
    "H27": P + [H.REJECTED, H.RETRYING, H.SUBMITTED, H.HEDGED, H.RECONCILED],
    "H29": P + [H.PARTIALLY_FILLED, H.RETRYING, H.SUBMITTED, H.HEDGED, H.RECONCILED],
    "H30": P + [H.HEDGED, H.RECONCILED],                                                     # the late maker fill opens a chain like any other
    "H31": P + [H.UNKNOWN, H.SUBMITTED, H.HEDGED, H.RECONCILED],                             # the late ack clears the unknown: the venue has it live
    "H32": P + [H.PARTIALLY_FILLED, H.PARTIALLY_FILLED, H.HEDGED, H.RECONCILED],
    "H34": [H.MAKER_FILLED, H.HEDGE_PENDING, H.SUBMITTED, H.HEDGED, H.RECONCILED],           # held on a bad book, then sent when it is valid
    "H36": P + [H.UNKNOWN, H.HEDGED, H.RECONCILED],                                          # recovered after a restart: queried, not resent
    "H37": [H.MAKER_FILLED, H.HEDGE_PENDING, H.ESCALATED, H.UNWINDING, H.RECONCILED],        # a dead feed: held, then the age limit escalates
}
OUTSIDE = {
    "H05": "SIM_ONLY instant-flatten oracle, forbidden in the perp profile; the core has no such path (simulation/)",
    "H13": "no exposure: the entry gate (book_gate / new-risk guard) opens nothing while blind",
    "H14": "entry guard before any signal: no maker order, so no hedge chain",
    "H17": "a maker order's own lifecycle (OrderState UNKNOWN, query), resolved before any chain opens",
    "H23": "circuit breaker (Phase E)",
    "H25": "flatten on trip (Phase E)",
    "H28": "PostOnly reject of the maker leg: no exposure, no chain",
    "H33": "pre-trade position and reservation limits (risk.py)",
    "H35": "ledger and equity bridge accounting, not a hedge state",
    "H38": "margin and liquidation (margin.py)",
    "H39": "venue selector (Phase E)",
    "H40": "a stated limit of the simulator, not a handler",
}


def walk(path):
    c = chain(retry_limit=5, unwind_attempt_limit=5, query_attempt_limit=3, max_residual_age_s=5.0)
    assert path[0] is H.MAKER_FILLED
    t = 0.0
    for prev, nxt in zip(path, path[1:]):
        t += 0.25
        kw = {}
        if nxt is H.REJECTED and prev is not H.UNKNOWN:
            pass
        if prev is H.REJECTED:
            c.note_reject(t - 0.1, "rate_limit", True) if nxt is H.RETRYING else None
        if prev is H.UNKNOWN:
            c.note_query({H.HEDGED: "found_executed", H.PARTIALLY_FILLED: "found_partial", H.SUBMITTED: "found_live", H.RETRYING: "not_found", H.REJECTED: "not_found"}.get(nxt, "failed"))
            if nxt is H.FAILED:
                c.note_query("failed"); c.note_query("failed")
        if nxt is H.REJECTED and prev in (H.SUBMITTED, H.HEDGE_PENDING):
            c.note_reject(t, "rate_limit", True)
        if nxt is H.HEDGED:
            kw["filled_qty"] = c.exposure_qty
        if nxt is H.PARTIALLY_FILLED:
            kw["filled_qty"] = min(c.exposure_qty - 0.1, c.hedged_qty + 0.3)
        if nxt is H.RECONCILED and prev is H.UNWINDING:
            kw["unwound_qty"] = c.exposure_qty - c.hedged_qty
        if nxt is H.RECONCILED and prev is H.HEDGED:
            pass
        if prev is H.PARTIALLY_FILLED and nxt in (H.UNWINDING, H.RETRYING):
            pass
        if nxt is H.UNWINDING and prev is H.PARTIALLY_FILLED:
            pass
        c.move(nxt, t, "case", **kw)
    return c


def test_all_forty_catalogue_cases_are_covered_exactly_once():
    ids = [x["id"] for x in json.load(open(Path(__file__).parent / "integration_hedge_failure_catalogue.json"))["cases"]]
    assert ids == [f"H{n:02d}" for n in range(1, 41)]
    assert set(CASE_PATHS) | set(OUTSIDE) == set(ids)
    assert not set(CASE_PATHS) & set(OUTSIDE)
    assert all(OUTSIDE.values())                                              # every case outside the chain says why and who owns it


@pytest.mark.parametrize("case", sorted(CASE_PATHS))
def test_each_hedge_chain_case_is_a_legal_path_through_the_table(case):
    c = walk(CASE_PATHS[case])
    assert c.state is CASE_PATHS[case][-1]
    assert c.state in (H.RECONCILED, H.FAILED)                               # every case ends reconciled or in the alarm, never in an open state
    assert all(e["to"] in {s.value for s in TRANSITIONS[H(e["from"])] | {H(e["to"])}} for e in c.events)


def test_the_cases_that_the_core_must_not_carry_or_assume_are_refused_by_the_table():
    """H01 as a backtester study option (carry the failed hedge) and H08 as the backtester acts (unwind after the ack timeout, no query) are not paths in the table."""
    with pytest.raises(IllegalHedgeTransition):
        walk(P + [H.REJECTED, H.RECONCILED])                                   # carried: reconciled with the residual still open
    c = chain()
    to_submitted(c)
    c.move(H.UNKNOWN, 1.0)
    with pytest.raises(IllegalHedgeTransition):
        c.move(H.RETRYING, 1.1)                                                # unknown -> retry with no query: the backtester's blackhole handling is not a path
