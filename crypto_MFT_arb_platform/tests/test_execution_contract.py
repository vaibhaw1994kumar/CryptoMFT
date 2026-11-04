"""Canonical execution layer, phase 0 hardening (strategy_contract 4h): the intent lifecycle table, atomic risk reservation, IOC cap
semantics, the one-live-hedge-child contract under a ghost fault, and the independent reconciliation. Each rule has a mutation
test: the mutated layer or engine must be caught by the same check the rule is tested with."""
from __future__ import annotations

import dataclasses
import itertools
import unittest
from unittest import mock

from new_backtest.execution import ExecutionConfig, ExecutionLayer, IntentError, IntentState, Role
from new_backtest.execution.intent import TRANSITIONS, ExecutionIntent, RiskRefused
from new_backtest.execution.reconcile import reconcile
from new_backtest.execution.risk import ReservationBook, RiskLimits
from new_backtest.hedge_faults import HedgeFaultConfig, Outage

from test_execution_layer import hedge_leg, maker_filled
from test_maker_order_lifetime import BINANCE, BYBIT, Harness

S = IntentState
LIMITS = RiskLimits(venue_cap=2.0, net_ceiling=2.0)


def layer(limits=LIMITS, base=None, **cfg) -> ExecutionLayer:
    xl = ExecutionLayer(ExecutionConfig(**cfg), instrument="SOLUSDT")
    xl.set_risk(lambda: limits, lambda: dict(base or {}))
    return xl


def prim(xl, sid, qty=1.0, side="buy", venue=BYBIT):
    return xl.open_intent(role=Role.PRIMARY, side=side, qty=qty, venues=(venue,), worst_price=100.0, now=1.0, signal_id=sid)


def hedged(xl, sid=1, qty=1.0):
    p = prim(xl, sid, qty)
    xl.child_sent(p, 10 * sid, 10 * sid, "PASSIVE", 1.0)
    xl.note_filled(p, qty)
    h = xl.open_intent(role=Role.HEDGE, side="sell", qty=qty, venues=(BINANCE,), worst_price=98.0, now=2.0, signal_id=sid, parent=p,
                       parent_filled_qty=qty)
    return p, h


# ── 4h.1 lifecycle ────────────────────────────────────────────────────────────
class LifecycleTests(unittest.TestCase):
    def _intent(self, state):
        it = ExecutionIntent(intent_id="x", role=Role.PRIMARY, instrument="SOLUSDT", side="buy", qty=1.0, venues=(BYBIT,), worst_price=1.0)
        it.state = state
        return it

    def test_every_pair_of_states_is_either_in_the_table_or_refused(self):
        legal = illegal = 0
        for a, b in itertools.product(S, S):
            it = self._intent(a)
            if b in TRANSITIONS[a]:
                it.move(b)
                self.assertEqual(it.state, b)
                legal += 1
            else:
                with self.assertRaises(IntentError, msg=f"{a.value}->{b.value}"):
                    it.move(b)
                self.assertEqual(it.state, a)                      # a refused move changes nothing
                illegal += 1
        self.assertEqual(legal + illegal, 49)
        self.assertGreater(illegal, 20)

    def test_terminal_states_have_no_exit_and_accepted_cannot_skip_to_a_fill(self):
        for t in (S.DONE, S.FAILED):
            self.assertEqual(TRANSITIONS[t], frozenset())
        self.assertNotIn(S.PARTIALLY_DONE, TRANSITIONS[S.ACCEPTED])
        self.assertNotIn(S.DONE, TRANSITIONS[S.ACCEPTED])

    def test_a_fill_on_an_intent_with_no_child_is_a_bug_not_a_silent_transition(self):
        xl = layer()
        p = prim(xl, 1, 2.0)
        with self.assertRaises(IntentError):
            xl.note_filled(p, 0.5)

    def test_settle_is_done_when_covered_failed_when_closed_short_and_a_noop_with_a_live_child(self):
        xl = layer()
        p, h = hedged(xl)
        xl.child_sent(h, 21, 21, "IOC_CAPPED", 2.0)
        xl.settle(h, 3.0)                                          # a live child: unknown, not terminal
        self.assertEqual(h.state, S.UNKNOWN)
        xl.child_closed(21)
        xl.settle(h, 4.0)
        self.assertEqual(h.state, S.FAILED)                        # nothing filled
        q = prim(xl, 2, 2.0)
        xl.child_sent(q, 30, 30, "PASSIVE", 1.0)
        xl.note_filled(q, 0.5)
        xl.settle(q, 5.0)
        self.assertEqual((q.state, q.filled_qty), (S.FAILED, 0.5))   # closed short, partial

    def test_a_fill_after_a_terminal_state_is_an_overfill_never_a_transition(self):
        xl = layer()
        p, h = hedged(xl)
        xl.child_sent(h, 21, 21, "IOC_CAPPED", 2.0)
        xl.note_filled(h, 1.0)
        self.assertEqual(h.state, S.DONE)
        xl.note_filled(h, 1.0)                                     # a ghost over-hedge
        self.assertEqual((h.state, xl.counts["overfills"]), (S.DONE, 1))
        self.assertAlmostEqual(h.filled_qty, 2.0)

    def test_a_ghost_makes_the_intent_unknown_and_blocks_a_second_child(self):
        for mode, blocked in (("enforce", True), ("count", False)):
            xl = layer(one_hedge_child=mode)
            p, h = hedged(xl)
            xl.child_sent(h, 21, 21, "IOC_CAPPED", 2.0)
            xl.child_closed(21)                                    # the engine gave the order up ...
            xl.child_ghosted(21, 9.0)                              # ... but a copy is still live at the venue
            self.assertEqual(h.state, S.UNKNOWN)
            self.assertTrue(xl.hedge_blocked(h))
            self.assertEqual(xl.ghost_release_ts(h), 9.0)
            leg = hedge_leg(qty=1.0, px=98.0)
            if blocked:
                with self.assertRaises(IntentError):
                    xl.check(h, leg)
            else:
                xl.check(h, leg)                                   # legacy: counted, allowed
                self.assertEqual(xl.counts["one_hedge_child_violations"], 1)
            xl.child_closed(21)                                    # the ghost executed or was rejected
            self.assertEqual(h.state, S.WORKING)
            self.assertFalse(xl.hedge_blocked(h))

    def test_mutation_a_ghost_that_leaves_the_intent_working_is_caught(self):
        with mock.patch.object(ExecutionLayer, "child_ghosted", lambda self, order_id, release_ts=None: None):
            xl = layer(one_hedge_child="enforce")
            p, h = hedged(xl)
            xl.child_sent(h, 21, 21, "IOC_CAPPED", 2.0)
            xl.child_closed(21)
            xl.child_ghosted(21, 9.0)
            self.assertNotEqual(h.state, S.UNKNOWN)                # the lifecycle test above would fail on this layer


# ── 4h.2 atomic risk reservation ──────────────────────────────────────────────
class ReservationTests(unittest.TestCase):
    def test_two_intents_racing_for_the_last_capacity_only_one_gets_it(self):
        xl = layer(risk_reservation="enforce", limits=RiskLimits(venue_cap=1.5, net_ceiling=1.5))
        a = prim(xl, 1, 1.0)
        with self.assertRaises(RiskRefused):
            prim(xl, 2, 1.0)                                       # 1.0 reserved + 1.0 > 1.5
        self.assertEqual(xl.counts["risk_refusals"], 1)
        self.assertEqual(list(xl.reservations.held), [a.intent_id])

    def test_a_refusal_changes_nothing(self):
        xl = layer(risk_reservation="enforce", limits=RiskLimits(venue_cap=1.5, net_ceiling=1.5))
        prim(xl, 1, 1.0)
        before = (dict(xl._serial), len(xl.journal), {k: v.remaining for k, v in xl.reservations.held.items()}, set(xl.intents))
        with self.assertRaises(RiskRefused):
            prim(xl, 2, 1.0)
        after = (dict(xl._serial), len(xl.journal), {k: v.remaining for k, v in xl.reservations.held.items()}, set(xl.intents))
        self.assertEqual(before, after)

    def test_a_signal_with_two_legs_is_reserved_all_or_none(self):
        xl = layer(risk_reservation="enforce", limits=RiskLimits(venue_cap=1.5, net_ceiling=10.0), base={BINANCE: -1.0})
        specs = [dict(side="buy", qty=1.0, venues=(BYBIT,), worst_price=100.0),
                 dict(side="sell", qty=1.0, venues=(BINANCE,), worst_price=99.0)]       # the sell breaches Binance's -1.5 floor
        with self.assertRaises(RiskRefused):
            xl.open_primary_group(specs, now=1.0, signal_id=3)
        self.assertEqual((len(xl.intents), len(xl.reservations.held), len(xl.journal)), (0, 0, 0))   # the first leg was not left behind

    def test_the_check_reads_the_layers_own_reservations_plus_inventory(self):
        xl = layer(risk_reservation="enforce", limits=RiskLimits(venue_cap=2.0, net_ceiling=10.0), base={BYBIT: 1.5})
        with self.assertRaises(RiskRefused):
            prim(xl, 1, 1.0)                                       # inventory 1.5 + 1.0 > 2.0 on the buy venue
        prim(xl, 2, 0.5)                                           # exactly the cap is allowed

    def test_count_mode_records_what_would_be_refused_and_allows_it(self):
        xl = layer(risk_reservation="count", limits=RiskLimits(venue_cap=1.5, net_ceiling=1.5))
        prim(xl, 1, 1.0)
        prim(xl, 2, 1.0)
        self.assertEqual((xl.counts["risk_would_refuse"], xl.counts["risk_refusals"], len(xl.intents)), (1, 0, 2))

    def test_fills_consume_and_terminal_states_release_every_reservation(self):
        xl = layer(risk_reservation="enforce")
        p = prim(xl, 1, 1.0)
        xl.child_sent(p, 10, 10, "PASSIVE", 1.0)
        xl.note_filled(p, 0.4)
        self.assertAlmostEqual(xl.reservations.held[p.intent_id].remaining, 0.6)
        xl.settle(p, 2.0)                                          # closed short: failed, the 0.6 goes back
        self.assertEqual((p.state, len(xl.reservations.held)), (S.FAILED, 0))
        q = prim(xl, 2, 1.0)
        xl.child_sent(q, 11, 11, "PASSIVE", 1.0)
        xl.note_filled(q, 1.0)                                     # filled in full: done and released at once
        self.assertEqual((q.state, len(xl.reservations.held)), (S.DONE, 0))
        r = prim(xl, 3, 1.0)                                       # never sent, signal resolved
        xl.settle_signal(3, 3.0)
        self.assertEqual((r.state, len(xl.reservations.held), xl.leaked_reservations()), (S.FAILED, 0, []))

    def test_a_failed_unwind_or_hedge_leaves_no_reservation_behind(self):
        xl = layer(risk_reservation="enforce")
        p, h = hedged(xl)
        xl.child_sent(h, 21, 21, "IOC_CAPPED", 2.0)
        xl.child_closed(21)
        xl.escalate(h, 3.0, "unwind sent")
        xl.settle_signal(1, 4.0)
        self.assertEqual((h.state, xl.reservations.open_total(), xl.leaked_reservations()), (S.FAILED, 0.0, []))

    def test_mutation_splitting_check_from_reserve_is_caught_by_the_race_test(self):
        # the broken layer reserves AFTER the intent is created and journaled and lets a second open slip between: simulated by a
        # reserve that is deferred, which is exactly what a check-then-reserve split looks like to the next caller
        with mock.patch.object(ReservationBook, "reserve", lambda self, *a, **k: None):
            xl = layer(risk_reservation="enforce", limits=RiskLimits(venue_cap=1.5, net_ceiling=1.5))
            prim(xl, 1, 1.0)
            prim(xl, 2, 1.0)                                       # must have been refused; the mutant accepts it
            self.assertEqual(len(xl.intents), 2)
            self.assertEqual(xl.counts["risk_refusals"], 0)        # test_two_intents_racing... asserts 1 and would fail

    def test_mutation_a_release_that_is_skipped_is_reported_as_a_leak(self):
        with mock.patch.object(ReservationBook, "release", lambda self, iid: 0.0):
            xl = layer(risk_reservation="enforce")
            p = prim(xl, 1, 1.0)
            xl.child_sent(p, 10, 10, "PASSIVE", 1.0)
            xl.note_filled(p, 1.0)
            self.assertEqual(xl.leaked_reservations(), [p.intent_id])      # reconciliation turns this into a failed run

    def test_the_net_ceiling_blocks_a_second_same_direction_clip_but_not_a_reducing_one(self):
        xl = layer(risk_reservation="enforce", limits=RiskLimits(venue_cap=10.0, net_ceiling=1.5), base={BYBIT: 1.0})
        with self.assertRaises(RiskRefused):
            prim(xl, 1, 1.0, side="buy")
        prim(xl, 2, 1.0, side="sell", venue=BINANCE)               # reduces the net long


# ── 4h.3 cap semantics ───────────────────────────────────────────────────────
class CapTests(unittest.TestCase):
    def test_a_retarget_cannot_be_looser_than_the_touch_slack_rule(self):
        xl = layer()
        p, h = hedged(xl)
        touch, walk = 98.0, 2.0
        rule = xl.touch_slack_cap("sell", touch, walk)
        xl.retarget(h, rule, qty=1.0, reason="retry", touch=touch, max_walk_bps=walk)            # the rule itself is fine
        with self.assertRaises(IntentError):
            xl.retarget(h, rule - 0.5, qty=1.0, reason="loose", touch=touch, max_walk_bps=walk)   # a sell cap further below the touch
        h2 = xl.open_intent(role=Role.HEDGE, side="buy", qty=1.0, venues=(BINANCE,), worst_price=102.0, now=2.0, signal_id=1,
                            parent=p, parent_filled_qty=1.0)
        rb = xl.touch_slack_cap("buy", 101.0, walk)
        with self.assertRaises(IntentError):
            xl.retarget(h2, rb + 0.5, qty=1.0, reason="loose", touch=101.0, max_walk_bps=walk)

    def test_a_fixed_cap_rule_refuses_any_retarget(self):
        xl = layer(hedge={"tactic": "IOC_CAPPED", "cap": "conditional_edge_breakeven"})
        p, h = hedged(xl)
        with self.assertRaises(IntentError):
            xl.retarget(h, 97.0, qty=1.0, reason="retry")

    def test_a_partial_fill_remainder_is_retried_for_the_remainder_only(self):
        for mode in ("enforce", "count"):
            xl = layer(one_hedge_child=mode)
            p, h = hedged(xl, qty=2.0)
            xl.child_sent(h, 21, 21, "IOC_CAPPED", 2.0)
            xl.note_filled(h, 0.5)
            xl.child_closed(21)
            xl.retarget(h, 98.0, qty=1.5, reason="remainder")                     # the remainder: fine
            if mode == "enforce":
                with self.assertRaises(IntentError):
                    xl.retarget(h, 98.0, qty=2.0, reason="full size again")        # would double-hedge 0.5
            else:
                xl.retarget(h, 98.0, qty=2.0, reason="legacy")
                self.assertEqual(xl.counts["retry_exceeds_remaining"], 1)

    def test_every_taker_child_stays_inside_the_cap_it_was_sent_with(self):
        xl = layer()
        p, h = hedged(xl)
        with self.assertRaises(IntentError):
            xl.check(h, hedge_leg(px=97.0))                        # a sell below the 98.0 cap


# ── the engine under the ghost fault (4h.4) ──────────────────────────────────
def ghost_run(mode: str, outage_s: float = 30.0):
    faults = HedgeFaultConfig(outages=(Outage(BINANCE, 5.0, 5.0 + outage_s, "gateway"),), outage_policy="ghost", response="retry",
                              max_retries=3, retry_backoff_s=0.5, ack_timeout_s=2.0)
    from new_backtest.execution import ExecutionConfig as EC
    h = Harness(faults=faults, cfg_kw={"execution": EC(one_hedge_child=mode, risk_reservation="count")})
    h.book(0.5, BYBIT)
    h.book(0.5, BINANCE)
    h.send_maker(1.0)
    h.sell_trade(5.5, BYBIT, volume=2.5)                           # the maker fill inside the outage: the hedge goes to the ghost path
    for t in (6.0, 8.0, 12.0, 20.0, 40.0, 60.0):
        h.book(t, BINANCE)
        h.advance(t)
    h.advance(100.0)
    return h


class GhostContractTests(unittest.TestCase):
    def test_legacy_count_mode_overlaps_the_retry_with_the_ghost_enforce_does_not(self):
        legacy, canon = ghost_run("count"), ghost_run("enforce")
        self.assertGreater(legacy.bt.xl.counts["max_live_hedge_children"], 1)            # the finding of the 2026-11-03 probe
        self.assertLessEqual(canon.bt.xl.counts["max_live_hedge_children"], 1)
        self.assertEqual(canon.bt.xl.counts["one_hedge_child_violations"], 0)
        self.assertEqual(canon.bt.xl.counts["overfills"], 0)                              # no over-hedge once the retry waits
        self.assertGreater(canon.bt.hedge["retries_deferred_for_ghost"], 0)

    def test_the_hedged_quantity_after_a_ghost_never_exceeds_the_maker_fill(self):
        canon = ghost_run("enforce")
        hedge_fills = sum(f["qty"] for f in canon.bt.xl.fills if f["side"] == "sell" and f["venue"] == BINANCE)
        self.assertLessEqual(hedge_fills, 2.0 + 1e-9)

    def test_no_intent_is_left_working_and_nothing_leaks_after_the_ghost_resolves(self):
        canon = ghost_run("enforce")
        canon.bt.xl.finalize(100.0)
        states = {i.state for i in canon.bt.xl.intents.values()}
        self.assertTrue(states <= {S.DONE, S.FAILED, S.UNKNOWN, S.ESCALATED}, states)
        self.assertEqual(canon.bt.xl.leaked_reservations(), [])

    def test_mutation_enforce_without_the_deferral_overlaps_again(self):
        with mock.patch("new_backtest.backtester.CrossVenueArbBacktester._ghost_wait_ts", lambda self, order: None):
            try:
                mutant = ghost_run("enforce")
            except IntentError:
                return                                              # the backstop in check() refused the second child: also caught
            self.assertGreater(mutant.bt.xl.counts["one_hedge_child_violations"], 0)


# ── 4h.5 independent reconciliation ──────────────────────────────────────────
FEES = {BYBIT: (2.0, 5.5), BINANCE: (2.0, 5.0)}


def _fill(seq, venue, side, qty, price, taker, kind="child", intent="sig1-P1", cap=None, order_id=1, ts=1.0):
    bps = FEES[venue][1 if taker else 0]
    return {"seq": seq, "ts": ts, "order_id": order_id, "intent_id": intent if kind == "child" else "", "kind": kind, "venue": venue,
            "side": side, "qty": qty, "price": price, "fee": qty * price * bps / 10_000.0, "taker": taker, "signal_id": 1, "cap": cap}


def _books(fills):
    base, cash, fees = {}, {}, 0.0
    for f in fills:
        s = f["qty"] if f["side"] == "buy" else -f["qty"]
        base[f["venue"]] = base.get(f["venue"], 0.0) + s
        cash[f["venue"]] = cash.get(f["venue"], 0.0) - s * f["price"] - f["fee"]
        fees += f["fee"]
    return base, cash, fees


def _tl(fills):
    return [{"venue": f["venue"], "side": f["side"], "qty": f["qty"], "price": f["price"]} for f in fills]


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.xl = layer()
        self.p, self.h = hedged(self.xl)
        self.fills = [_fill(0, BYBIT, "buy", 1.0, 100.0, False, intent=self.p.intent_id),
                      _fill(1, BINANCE, "sell", 1.0, 100.4, True, intent=self.h.intent_id, cap=98.0, order_id=21)]

    def run_rec(self, fills=None, **over):
        fills = self.fills if fills is None else fills
        base, cash, fees = _books(self.fills)
        kw = dict(fills=fills, intents=self.xl.intents, fee_bps=FEES, liquidation_fee_bps=None, inventory_base=base, inventory_cash=cash,
                  fees_paid=fees, funding_by_venue={}, trade_log=_tl(self.fills), cash_pnl=sum(cash.values()))
        kw.update(over)
        return reconcile(**kw)

    def test_a_clean_book_reconciles(self):
        r = self.run_rec()
        self.assertTrue(r["ok"], r["problems"])
        self.assertEqual(r["fill_kinds"], {"child": 2})

    def test_mutation_a_fill_the_engine_dropped_is_caught(self):
        base, cash, fees = _books(self.fills[:1])                  # the engine booked only the maker fill
        r = self.run_rec(inventory_base=base, inventory_cash=cash, fees_paid=fees, cash_pnl=sum(cash.values()), trade_log=_tl(self.fills[:1]))
        self.assertFalse(r["ok"])
        self.assertIn("cash", r["problems"])
        self.assertIn("trade_log_count", r["problems"])

    def test_mutation_a_wrong_fee_on_the_engine_side_is_caught(self):
        bad = [dict(self.fills[0]), dict(self.fills[1], fee=self.fills[1]["fee"] * 2)]
        r = self.run_rec(fills=bad)
        self.assertFalse(r["ok"])
        self.assertIn("fee", r["problems"])

    def test_mutation_a_double_booked_fill_is_caught(self):
        r = self.run_rec(fills=self.fills + [dict(self.fills[1], seq=2)])
        self.assertFalse(r["ok"])
        self.assertIn("cash", r["problems"])

    def test_a_fill_with_no_intent_and_no_explicit_kind_is_unattributed(self):
        r = self.run_rec(fills=[self.fills[0], dict(self.fills[1], intent_id="")])
        self.assertIn("unattributed", r["problems"])

    def test_a_fill_on_the_wrong_side_or_venue_for_its_intent_is_caught(self):
        r = self.run_rec(fills=[self.fills[0], dict(self.fills[1], side="buy")])
        self.assertIn("intent_mismatch", r["problems"])

    def test_mutation_a_taker_fill_beyond_its_cap_is_caught(self):
        breach = dict(self.fills[1], price=97.5, fee=97.5 * 5.0 / 10_000.0)                       # a sell at 97.5 under a 98.0 cap
        base, cash, fees = _books([self.fills[0], breach])
        r = self.run_rec(fills=[self.fills[0], breach], inventory_base=base, inventory_cash=cash, fees_paid=fees,
                         trade_log=_tl([self.fills[0], breach]), cash_pnl=sum(cash.values()))
        self.assertIn("cap_breach", r["problems"])
        self.assertNotIn("cash", r["problems"])                                                    # only the cap rule fired

    def test_explicit_kinds_are_accepted_without_an_intent(self):
        f = _fill(2, BINANCE, "sell", 0.3, 100.0, True, kind="instant_unwind")
        self.fills.append(f)
        r = self.run_rec()
        self.assertTrue(r["ok"], r["problems"])
        self.assertEqual(r["fill_kinds"]["instant_unwind"], 1)

    def test_funding_is_part_of_the_cash_identity(self):
        base, cash, fees = _books(self.fills)
        paid = {BYBIT: 0.25}
        ledger = dict(cash, **{BYBIT: cash[BYBIT] - 0.25})
        ok = self.run_rec(funding_by_venue=paid, inventory_cash=ledger, cash_pnl=sum(ledger.values()))
        self.assertTrue(ok["ok"], ok["problems"])
        bad = self.run_rec(funding_by_venue=paid)                  # the ledger forgot the funding
        self.assertFalse(bad["ok"])

    def test_an_overfill_is_reported_and_fails_only_in_enforce(self):
        over = [self.fills[0], self.fills[1], dict(self.fills[1], seq=2, order_id=22)]
        base, cash, fees = _books(over)
        kw = dict(fills=over, inventory_base=base, inventory_cash=cash, fees_paid=fees, trade_log=_tl(over), cash_pnl=sum(cash.values()))
        legacy, canon = self.run_rec(mode="count", **kw), self.run_rec(mode="enforce", **kw)
        self.assertEqual(legacy["overfill_count"], 1)
        self.assertTrue(legacy["ok"])
        self.assertFalse(canon["ok"])


class EngineReconciliationTests(unittest.TestCase):
    def test_a_replay_through_a_maker_fill_and_a_hedge_reconciles_end_to_end(self):
        h = Harness()
        maker_filled(h)
        h.advance(10.0)
        doc = h.bt._exec_reconcile(h.bt.inventory.total_cash)
        self.assertTrue(doc["ok"], doc["problems"])
        self.assertEqual(doc["fill_kinds"].get("child"), 2)        # the maker fill and the hedge, each with its intent
        self.assertEqual(doc["problems"], {})

    def test_mutation_an_engine_that_forgets_a_fee_fails_the_run(self):
        from new_backtest.execution.reconcile import ExecutionReconciliationError
        h = Harness()
        maker_filled(h)
        h.advance(10.0)
        h.bt.fees_paid += 0.5                                      # the engine's fee total drifts from the schedule
        with self.assertRaises(ExecutionReconciliationError):
            h.bt._exec_reconcile(h.bt.inventory.total_cash)

    def test_mutation_an_engine_that_drops_a_fill_from_the_ledger_fails_the_run(self):
        from new_backtest.execution.reconcile import ExecutionReconciliationError
        h = Harness()
        maker_filled(h)
        h.advance(10.0)
        v = next(iter(h.bt.inventory.base))
        h.bt.inventory.base[v] += 0.1                              # a position the journal never saw
        with self.assertRaises(ExecutionReconciliationError):
            h.bt._exec_reconcile(h.bt.inventory.total_cash)


# ── the tactic set: PASSIVE (touch | improve | deep), IOC_CAPPED, MARKET_BANDED ──────────────────────────
class TacticSetTests(unittest.TestCase):
    def test_exactly_three_tactics_exist_and_the_old_names_are_gone(self):
        from new_backtest.execution.tactics import REGISTRY, get_tactic
        self.assertEqual(sorted(REGISTRY), ["IOC_CAPPED", "MARKET_BANDED", "PASSIVE"])
        for gone in ("PASSIVE_TOUCH", "PASSIVE_IMPROVE", "PASSIVE_DEEP", "IOC_SPECULATIVE", "SCHEDULED"):
            with self.assertRaises(KeyError, msg=gone):
                get_tactic(gone, ExecutionConfig())

    def test_the_config_refuses_old_names_wrong_tactics_and_bad_placements(self):
        from new_backtest.execution import ExecutionConfigError
        for bad in ({"primary": {"tactic": "PASSIVE_DEEP"}}, {"primary": {"tactic": "PASSIVE_TOUCH"}}, {"primary": {"tactic": "IOC_CAPPED"}},
                    {"hedge": {"tactic": "IOC_SPECULATIVE"}}, {"unwind": {"tactic": "SCHEDULED"}}, {"hedge": {"tactic": "PASSIVE"}},
                    {"primary": {"tactic": "PASSIVE", "placement": "middle"}},
                    {"primary": {"tactic": "PASSIVE", "placement": "touch", "deep_ticks": 2}},
                    {"primary": {"tactic": "PASSIVE", "placement": "deep", "deep_ticks": 0}}):
            with self.assertRaises(ExecutionConfigError, msg=str(bad)):
                ExecutionConfig(**bad)
        for good in ("touch", "improve", "deep"):
            self.assertEqual(ExecutionConfig(primary={"tactic": "PASSIVE", "placement": good}).primary["placement"], good)
        self.assertEqual(ExecutionConfig().primary["placement"], "deep")

    def test_placement_prices(self):
        from new_backtest.execution.tactics import placement_price as pp
        # book 98 / 102, tick 1, the conditional edge requires a buy at 100 or better, a sell at 100 or better
        self.assertEqual([pp(m, "buy", 98, 102, 1, 100) for m in ("touch", "improve", "deep")], [98, 99, 100])
        self.assertEqual([pp(m, "sell", 98, 102, 1, 100) for m in ("touch", "improve", "deep")], [102, 101, 100])
        self.assertEqual(pp("deep", "buy", 98, 102, 1, 100, deep_ticks=2), 96)             # n ticks behind the touch
        self.assertEqual(pp("deep", "sell", 98, 102, 1, 100, deep_ticks=2), 104)
        self.assertEqual(pp("improve", "buy", 98, 99, 1, 98.5), 98)                         # a one-tick spread cannot be improved
        self.assertEqual(pp("improve", "sell", 98, 99, 1, 98.5), 99)

    def test_a_placement_never_gives_up_the_edge_and_never_crosses(self):
        from new_backtest.execution.tactics import placement_price as pp
        self.assertEqual(pp("improve", "buy", 98, 102, 1, 98.4), 98.4)                      # capped at the edge price
        self.assertEqual(pp("improve", "sell", 98, 102, 1, 101.6), 101.6)
        self.assertLessEqual(pp("touch", "buy", 98, 102, 1, 105), 102)
        self.assertEqual(pp("deep", "buy", 98, 102, 1, 105), 98)                            # an edge price through the ask falls back to the touch
        self.assertEqual(pp("deep", "sell", 98, 102, 1, 95), 102)

    def test_deep_sends_the_strategys_price_unchanged(self):
        xl = layer()
        self.assertIsNone(xl.passive_price("buy", 98, 102, 1, 100.0))
        self.assertEqual(layer(primary={"tactic": "PASSIVE", "placement": "touch"}).passive_price("buy", 98, 102, 1, 100.0), 98)

    def test_the_engine_rests_the_order_where_the_placement_says(self):
        want = {"deep": 100.0, "touch": 98.0, "improve": 99.0}
        for placement, px in want.items():
            h = Harness(cfg_kw={"execution": ExecutionConfig(primary={"tactic": "PASSIVE", "placement": placement})})
            h.book(0.5, BYBIT, bids=((98.0, 5.0), (97.0, 5.0)), asks=((102.0, 5.0), (103.0, 5.0)))
            h.book(0.5, BINANCE)
            sig = h.send_maker(1.0, price=100.0)
            h.advance(2.0)
            order = h.order()
            self.assertEqual(order.price, px, placement)
            self.assertEqual(h.bt._primary_intent[sig.signal_id].worst_price, 100.0)        # the intent's cap stays the edge price

    def test_mutation_a_placement_that_ignores_the_edge_cap_is_refused_by_the_cap_check(self):
        from new_backtest.execution.tactics.passive import Passive
        with mock.patch.object(Passive, "price", lambda self, side, bb, ba, tick, edge: 101.0):     # a buy resting above its edge price of 100
            h = Harness(cfg_kw={"execution": ExecutionConfig(primary={"tactic": "PASSIVE", "placement": "improve"})})
            h.book(0.5, BYBIT, bids=((98.0, 5.0), (97.0, 5.0)), asks=((102.0, 5.0), (103.0, 5.0)))
            h.book(0.5, BINANCE)
            with self.assertRaises(IntentError):
                h.send_maker(1.0, price=100.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
