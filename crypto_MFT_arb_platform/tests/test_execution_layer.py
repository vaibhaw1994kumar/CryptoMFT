"""Canonical execution layer, phase 0 acceptance: intents, journal-before-send, mandatory price cap, hedge only after a fill,
one live hedge child, profile switches. The mutation checks at the bottom are the ones gate 0 names: each mutation must be REFUSED
(a test here fails if the mutated engine were accepted)."""
from __future__ import annotations

import dataclasses
import unittest

import pytest

from new_backtest.arb_strategy import ArbLeg
from new_backtest.execution import ExecutionConfig, ExecutionConfigError, ExecutionLayer, IntentError, IntentState, Role
from new_backtest.hedge_faults import HedgeFaultConfig
from new_backtest.profiles import ProfileError, apply_profile, load_profile

from test_maker_order_lifetime import BINANCE, BYBIT, Harness


def layer(**kw) -> ExecutionLayer:
    cfg = ExecutionConfig(**kw.pop("cfg", {}))
    return ExecutionLayer(cfg, instrument="SOLUSDT", **kw)


def primary(xl: ExecutionLayer, qty=2.0):
    return xl.open_intent(role=Role.PRIMARY, side="buy", qty=qty, venues=(BYBIT,), worst_price=100.0, now=1.0, signal_id=7, reason="maker leg")


def hedge_leg(qty=2.0, px=99.0, venue=BINANCE, side="sell") -> ArbLeg:
    return ArbLeg(venue=venue, side=side, qty=qty, limit_price=px, is_taker=True, expected_price=px, fee_bps=5.0)


class IntentTests(unittest.TestCase):
    def test_intent_ids_are_deterministic_and_the_journal_holds_the_intent_before_any_child(self):
        xl = layer()
        p = primary(xl)
        self.assertEqual(p.intent_id, "sig7-P1")
        self.assertEqual([j["event"] for j in xl.journal], ["intent"])             # journaled at open, before the first child exists
        xl.child_sent(p, 11, 11, "PASSIVE", 1.0)
        self.assertEqual([j["event"] for j in xl.journal if j["event"] != "state"], ["intent", "child"])
        self.assertEqual(p.state, IntentState.WORKING)
        h = xl.open_intent(role=Role.HEDGE, side="sell", qty=2.0, venues=(BINANCE,), worst_price=98.0, now=2.0, signal_id=7,
                           parent=p, parent_filled_qty=2.0)
        self.assertEqual((h.intent_id, h.parent_id), ("sig7-H1", "sig7-P1"))

    def test_bad_intents_are_refused(self):
        xl = layer()
        with self.assertRaises(Exception):
            xl.open_intent(role="bogus", side="buy", qty=1.0, venues=(BYBIT,), worst_price=1.0, now=0.0, signal_id=1)
        with self.assertRaises(Exception):
            xl.open_intent(role=Role.PRIMARY, side="buy", qty=0.0, venues=(BYBIT,), worst_price=1.0, now=0.0, signal_id=1)

    def test_state_follows_fills(self):
        xl = layer()
        p = primary(xl, qty=2.0)
        xl.child_sent(p, 11, 11, "PASSIVE", 1.0)
        xl.note_filled(p, 0.5)
        self.assertEqual(p.state, IntentState.PARTIALLY_DONE)
        xl.note_filled(p, 1.5)
        self.assertEqual(p.state, IntentState.DONE)


class RiskRuleTests(unittest.TestCase):
    def test_a_hedge_exists_only_for_a_quantity_already_filled(self):
        xl = layer()
        p = primary(xl)
        with self.assertRaises(IntentError):
            xl.open_intent(role=Role.HEDGE, side="sell", qty=2.0, venues=(BINANCE,), worst_price=98.0, now=1.0, signal_id=7, parent=p,
                           parent_filled_qty=0.0)
        with self.assertRaises(IntentError):                                        # a hedge needs its parent
            xl.open_intent(role=Role.HEDGE, side="sell", qty=2.0, venues=(BINANCE,), worst_price=98.0, now=1.0, signal_id=7)
        sim = layer(dispatch_mode="simultaneous")                                    # Method B hedges before the fill on purpose
        sim.open_intent(role=Role.HEDGE, side="sell", qty=2.0, venues=(BINANCE,), worst_price=98.0, now=1.0, signal_id=7,
                        parent=primary(sim), parent_filled_qty=0.0)

    def test_the_price_cap_is_mandatory_and_every_child_stays_inside_it(self):
        xl = layer()
        p = primary(xl)
        with self.assertRaises(IntentError):
            xl.open_intent(role=Role.HEDGE, side="sell", qty=2.0, venues=(BINANCE,), worst_price=None, now=1.0, signal_id=7, parent=p,
                           parent_filled_qty=2.0)
        h = xl.open_intent(role=Role.HEDGE, side="sell", qty=2.0, venues=(BINANCE,), worst_price=98.0, now=1.0, signal_id=7, parent=p,
                           parent_filled_qty=2.0)
        xl.check(h, hedge_leg(px=98.0))                                              # at the cap: fine
        xl.check(h, hedge_leg(px=98.5))                                              # inside the cap (sell above worst): fine
        with self.assertRaises(IntentError):
            xl.check(h, hedge_leg(px=0.0))                                           # an uncapped sell is refused
        buy = xl.open_intent(role=Role.HEDGE, side="buy", qty=2.0, venues=(BINANCE,), worst_price=101.0, now=1.0, signal_id=7, parent=p,
                             parent_filled_qty=2.0)
        with self.assertRaises(IntentError):
            xl.check(buy, hedge_leg(px=float("inf"), side="buy"))                    # an uncapped buy is refused

    def test_wrong_side_venue_or_size_is_refused(self):
        xl = layer()
        p = primary(xl)
        h = xl.open_intent(role=Role.HEDGE, side="sell", qty=2.0, venues=(BINANCE,), worst_price=98.0, now=1.0, signal_id=7, parent=p,
                           parent_filled_qty=2.0)
        for bad in (hedge_leg(side="buy", px=99.0), hedge_leg(venue=BYBIT), hedge_leg(qty=3.0)):
            with self.assertRaises(IntentError):
                xl.check(h, bad)

    def test_one_live_hedge_child_is_counted_or_refused(self):
        for mode, refuses in (("count", False), ("enforce", True)):
            xl = layer(cfg={"one_hedge_child": mode})
            p = primary(xl)
            h = xl.open_intent(role=Role.HEDGE, side="sell", qty=2.0, venues=(BINANCE,), worst_price=98.0, now=1.0, signal_id=7, parent=p,
                               parent_filled_qty=2.0)
            xl.check(h, hedge_leg(px=98.5))
            xl.child_sent(h, 21, 21, "IOC_CAPPED", 1.0)
            if refuses:
                with self.assertRaises(IntentError):
                    xl.check(h, hedge_leg(px=98.5))
            else:
                xl.check(h, hedge_leg(px=98.5))
                self.assertEqual(xl.counts["one_hedge_child_violations"], 1)
            xl.child_closed(21)
            xl.check(h, hedge_leg(px=98.5))                                          # the first child is closed: a retry is fine
            self.assertEqual(xl.counts["one_hedge_child_violations"], 1)             # enforce counts the refused attempt too; no new one after the close

    def test_a_ghost_stays_live_after_the_strategy_wrote_it_off(self):
        xl = layer()
        p = primary(xl)
        h = xl.open_intent(role=Role.HEDGE, side="sell", qty=2.0, venues=(BINANCE,), worst_price=98.0, now=1.0, signal_id=7, parent=p,
                           parent_filled_qty=2.0)
        xl.child_sent(h, 21, 21, "IOC_CAPPED", 1.0)
        xl.child_closed(21)                                                          # _hedge_failed
        xl.child_ghosted(21)                                                         # ...but a copy is delivered at reconnect
        xl.check(h, hedge_leg(px=98.5))                                              # the retry goes out: two children live
        self.assertEqual(xl.counts["one_hedge_child_violations"], 1)
        self.assertEqual(xl.violations[0]["ghost"], [21])

    def test_retarget_moves_the_cap_only_under_the_touch_slack_rule(self):
        xl = layer()
        p = primary(xl)
        h = xl.open_intent(role=Role.HEDGE, side="sell", qty=2.0, venues=(BINANCE,), worst_price=98.0, now=1.0, signal_id=7, parent=p,
                           parent_filled_qty=2.0)
        xl.retarget(h, 97.0, qty=1.0, reason="retry")
        self.assertEqual(h.worst_price, 97.0)
        fixed = layer(cfg={"hedge": {"tactic": "IOC_CAPPED", "cap": "fixed"}})
        q = primary(fixed)
        f = fixed.open_intent(role=Role.HEDGE, side="sell", qty=2.0, venues=(BINANCE,), worst_price=98.0, now=1.0, signal_id=7, parent=q,
                              parent_filled_qty=2.0)
        with self.assertRaises(IntentError):
            fixed.retarget(f, 97.0, qty=1.0, reason="retry")

class ConfigTests(unittest.TestCase):
    def test_unknown_keys_and_bad_values_are_errors(self):
        with self.assertRaises(ExecutionConfigError):
            ExecutionConfig(pairing="bogus")
        with self.assertRaises(ExecutionConfigError):
            ExecutionConfig(hedge={"tactic": "IOC_CAPPED", "bogus": 1})
        with self.assertRaises(ExecutionConfigError):
            ExecutionConfig(one_hedge_child="sometimes")

    def test_pairing_must_match_the_arb_mode_and_maker_maker_waits_for_phase_3(self):
        with self.assertRaises(NotImplementedError):
            Harness(cfg_kw={"execution": ExecutionConfig(pairing="maker_maker")})
        with self.assertRaises(ValueError):
            Harness(cfg_kw={"execution": ExecutionConfig(pairing="taker_taker")})


class ProfileTests(unittest.TestCase):
    def _doc(self, name="perp_perp_maker_taker_v1", **execution):
        doc = load_profile(name)
        doc["execution"] = {**(doc.get("execution") or {}), **execution}
        return doc

    def _apply(self, doc):
        return apply_profile(doc, data_root="data", pair="SOLUSDT", start_date="2022-01-05", end_date="2022-01-05", latency_sensitivity="test")

    def test_both_profiles_carry_the_block_and_differ_only_in_the_two_enforcement_switches(self):
        a, b = self._apply(self._doc("legacy_v0")), self._apply(self._doc())
        da, db = a.execution.as_dict(), b.execution.as_dict()
        self.assertEqual((da["primary"].pop("price_rule"), db["primary"].pop("price_rule")), ("naive_edge", "conditional_edge"))
        self.assertEqual((da["primary"]["tactic"], da["primary"]["placement"]), ("PASSIVE", "deep"))     # both rest at the edge-required price
        self.assertEqual((da.pop("one_hedge_child"), da.pop("risk_reservation")), ("count", "count"))       # legacy: behaviour kept
        self.assertEqual((db.pop("one_hedge_child"), db.pop("risk_reservation")), ("enforce", "enforce"))   # canonical contract
        self.assertEqual(da, db)
        self.assertEqual(b.execution.pairing, b.arb.mode)
        self.assertIn("config_sha256", b.release_identity["execution"])              # recorded in the release identity

    def test_the_one_hedge_child_and_risk_reservation_rules_cannot_be_switched_off_in_perp(self):
        with self.assertRaises(ProfileError):
            self._apply(self._doc(one_hedge_child="off"))
        with self.assertRaises(ProfileError):
            self._apply(self._doc(risk_reservation="off"))
        self._apply(self._doc("legacy_v0", one_hedge_child="off"))                   # the comparison baseline is exempt by name

    def test_the_price_cap_switch_is_calibrated_until_the_cap_decision_and_is_on_in_both_profiles(self):
        from new_backtest.profiles import switch_table
        for name in ("legacy_v0", "perp_perp_maker_taker_v1"):
            doc = self._doc(name)
            row = next(r for r in switch_table(doc) if r["switch"] == "execution.price_cap_required")
            self.assertEqual((row["class"], row["value"]), ("CALIBRATED", True), name)
            self.assertTrue(self._apply(doc).execution.price_cap_required)
        off = self._apply(self._doc(price_cap_required=False))                       # allowed while CALIBRATED; recorded in the switch table
        self.assertFalse(off.execution.price_cap_required)

    def test_a_bad_block_is_a_profile_error(self):
        with self.assertRaises(ProfileError):
            self._apply(self._doc(bogus_key=1))


def maker_filled(h: Harness):
    h.book(0.5, BYBIT)
    h.book(0.5, BINANCE)
    h.send_maker(1.0)
    h.sell_trade(5.0, BYBIT, volume=2.5)                                             # fills the maker bid (2.0) -> the hedge is sent


class EngineIntegrationTests(unittest.TestCase):
    def test_maker_fill_creates_a_hedge_intent_for_the_filled_quantity_and_children_carry_intent_ids(self):
        h = Harness()
        maker_filled(h)
        xl = h.bt.xl
        p, hed = xl.intents["sig1-P1"], xl.intents["sig1-H1"]
        self.assertEqual((p.role, hed.role, hed.parent_id), ("primary", "hedge", "sig1-P1"))
        self.assertEqual(hed.qty, 2.0)
        self.assertEqual(p.state, IntentState.DONE)                                  # the maker order filled in full
        orders = {o.order_id: o for o in h.bt.pending.in_flight()}
        hedge_orders = [o for o in orders.values() if o.is_hedge]
        self.assertEqual([o.intent_id for o in hedge_orders], ["sig1-H1"])
        # the journal holds each intent before its first child
        seq = [(j["event"], j.get("intent_id")) for j in xl.journal]
        self.assertLess(seq.index(("intent", "sig1-P1")), seq.index(("child", "sig1-P1")))
        self.assertLess(seq.index(("intent", "sig1-H1")), seq.index(("child", "sig1-H1")))
        self.assertLess(seq.index(("child", "sig1-P1")), seq.index(("intent", "sig1-H1")))          # the hedge intent appears only after the maker child
        # the cap is the hedge-venue touch moved by max(max_walk_bps, 5) bps, computed independently of the strategy's limit
        touch = h.bt.books[BINANCE].best_bid
        self.assertAlmostEqual(hed.worst_price, touch - touch * max(h.bt.config.arb.max_walk_bps, 5.0) / 10_000.0, places=9)
        self.assertAlmostEqual(hedge_orders[0].limit_price, hed.worst_price, places=9)
        self.assertEqual(h.bt.xl.counts["one_hedge_child_violations"], 0)

    def test_the_result_carries_the_execution_report(self):
        h = Harness()
        maker_filled(h)
        rep = h.bt.xl.report()
        self.assertEqual(rep["intents"], 2)
        self.assertEqual(rep["counts"]["children_PASSIVE"], 1)
        self.assertEqual(rep["counts"]["children_IOC_CAPPED"], 1)
        self.assertEqual(rep["one_hedge_child_mode"], "count")

    # ── gate 0 mutation checks: each mutated engine must be refused ──
    def test_mutation_removing_the_price_cap_is_refused(self):
        h = Harness()
        h.book(0.5, BYBIT)
        h.book(0.5, BINANCE)
        real = h.bt.strategy.hedge_leg
        h.bt.strategy.hedge_leg = lambda sig, qty, book: dataclasses.replace(real(sig, qty, book), limit_price=0.0)   # an uncapped sell
        h.send_maker(1.0)
        with self.assertRaises(IntentError):
            h.sell_trade(5.0, BYBIT, volume=2.5)

    def test_mutation_creating_a_hedge_before_the_fill_is_refused(self):
        h = Harness()
        h.book(0.5, BYBIT)
        h.book(0.5, BINANCE)
        sig = h.send_maker(1.0)
        parent = h.bt._primary_intent[sig.signal_id]
        with self.assertRaises(IntentError):
            h.bt.xl.open_intent(role=Role.HEDGE, side="sell", qty=2.0, venues=(BINANCE,), worst_price=98.0, now=1.0, signal_id=sig.signal_id,
                                parent=parent, parent_filled_qty=parent.filled_qty)         # nothing filled yet

    def test_mutation_two_live_hedge_children_is_refused_in_enforce_mode(self):
        h = Harness(cfg_kw={"execution": ExecutionConfig(one_hedge_child="enforce")})
        maker_filled(h)
        hed = h.bt.xl.intents["sig1-H1"]
        leg = hedge_leg(px=hed.worst_price)
        with self.assertRaises(IntentError):
            h.bt._send_leg(leg, h.bt._trackers[1].signal, 5.0, is_hedge=True, intent=hed)    # a second child while the first is live

    def test_sending_without_an_intent_is_impossible(self):
        h = Harness()
        h.book(0.5, BYBIT)
        with self.assertRaises(TypeError):
            h.bt._send_leg(hedge_leg(), None, 1.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
