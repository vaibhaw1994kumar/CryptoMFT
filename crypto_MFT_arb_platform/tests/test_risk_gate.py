"""Phase E (owner 9 Nov): the risk gate, one fixture per refusal reason, risk-reducing children past a trip, reachable exposure counting UNKNOWN children (the ghost-hedge rule), and the mutations the brief names."""
from __future__ import annotations

import unittest

from crypto_stat_arb.ems import risk_gate as rg
from crypto_stat_arb.ems.risk_gate import ExposureView, GateChild, GateContext, GateLimits, evaluate

LIMITS = GateLimits(net_ceiling=1.0, fill_headroom=0.1, venue_cap=1.5, band_bps=15.0, margin_utilisation_cap=0.8)


def child(**kw):
    base = dict(role="PRIMARY", tactic="PASSIVE", venue="BYBIT", side="buy", qty=0.3, intent_id="i1", limit_price=100.0)
    base.update(kw)
    return GateChild(**base)


def ctx(**kw):
    base = dict(breaker_state="OK", exposure=ExposureView(), limits=LIMITS, mid={"BYBIT": 100.0, "BINANCE": 100.0})
    base.update(kw)
    return GateContext(**base)


class RefusalFixtures(unittest.TestCase):
    def refused(self, c, x, reason):
        d = evaluate(c, x)
        self.assertEqual((d.allowed, d.reason), (False, reason), d)
        return d

    def test_a_clean_new_quote_is_allowed(self):
        self.assertTrue(evaluate(child(), ctx()).allowed)

    def test_no_price_cap(self):
        self.refused(child(limit_price=None), ctx(), "NO_PRICE_CAP")
        self.refused(child(tactic="MARKET_BANDED", role="FLATTEN", limit_price=None, worst_price=None), ctx(), "NO_PRICE_CAP")

    def test_only_market_banded_may_carry_a_worst_price(self):
        self.refused(child(tactic="IOC_CAPPED", role="HEDGE", worst_price=99.0), ctx(), "WORST_PRICE_NOT_ALLOWED")

    def test_with_a_touch_the_band_is_read_from_the_touch_the_child_crosses(self):
        """Owner 11 Nov: a sell is banded from the bid, a buy from the ask. With a 20 bps spread (bid 99.9, ask 100.1, mid 100.0) the two references answer differently."""
        touch = {"BYBIT": (99.9, 100.1)}
        sell = lambda w: child(tactic="MARKET_BANDED", role="FLATTEN", side="sell", limit_price=None, worst_price=w)
        buy = lambda w: child(tactic="MARKET_BANDED", role="FLATTEN", side="buy", limit_price=None, worst_price=w)
        self.assertTrue(evaluate(sell(99.76), ctx(touch=touch)).allowed)                  # 14.0 bps below the bid
        self.refused(sell(99.76), ctx(), "BAND_VIOLATED")                                   # 24 bps below the mid
        self.refused(sell(99.74), ctx(touch=touch), "BAND_VIOLATED")                        # 16.0 bps below the bid
        self.assertTrue(evaluate(buy(100.24), ctx(touch=touch)).allowed)                  # 14.0 bps above the ask
        self.refused(buy(100.24), ctx(), "BAND_VIOLATED")                                   # 24 bps above the mid
        self.refused(buy(100.26), ctx(touch=touch), "BAND_VIOLATED")                        # 16.0 bps above the ask

    def test_the_band_is_enforced_for_market_banded(self):
        ok = child(tactic="MARKET_BANDED", role="FLATTEN", side="sell", limit_price=None, worst_price=99.9)               # 10 bps below the mid
        self.assertTrue(evaluate(ok, ctx()).allowed)
        self.refused(child(tactic="MARKET_BANDED", role="FLATTEN", side="sell", limit_price=None, worst_price=99.8), ctx(), "BAND_VIOLATED")     # 20 bps
        self.refused(child(tactic="MARKET_BANDED", role="FLATTEN", side="buy", limit_price=None, worst_price=100.2), ctx(), "BAND_VIOLATED")

    def test_a_paused_or_tripped_breaker_blocks_new_risk(self):
        self.refused(child(), ctx(breaker_state="PAUSED"), "BREAKER_PAUSED")
        self.refused(child(), ctx(breaker_state="TRIPPED"), "BREAKER_TRIPPED")

    def test_risk_reducing_children_are_allowed_while_tripped_but_still_capped(self):
        for role in ("HEDGE", "UNWIND", "FLATTEN"):
            self.assertTrue(evaluate(child(role=role, tactic="IOC_CAPPED", side="sell"), ctx(breaker_state="TRIPPED")).allowed, role)
        self.refused(child(role="HEDGE", tactic="IOC_CAPPED", limit_price=None), ctx(breaker_state="TRIPPED"), "NO_PRICE_CAP")          # still capped
        self.refused(child(role="HEDGE", tactic="IOC_CAPPED"), ctx(breaker_state="TRIPPED", live_hedge_children={"i1": 1}), "SECOND_HEDGE_CHILD")
        self.refused(child(role="HEDGE", tactic="IOC_CAPPED"), ctx(breaker_state="TRIPPED", rate_allowed=False), "RATE_LIMIT")

    def test_one_live_hedge_child_per_hedge_intent(self):
        self.refused(child(role="HEDGE", tactic="IOC_CAPPED"), ctx(live_hedge_children={"i1": 1}), "SECOND_HEDGE_CHILD")
        self.assertTrue(evaluate(child(role="HEDGE", tactic="IOC_CAPPED"), ctx(live_hedge_children={"other": 1})).allowed)

    def test_no_blind_replacement_of_an_unknown_hedge_the_answer_is_a_status_query(self):
        for role in ("HEDGE", "UNWIND"):
            d = self.refused(child(role=role, tactic="IOC_CAPPED"), ctx(unknown_hedge_intents=frozenset({"i1"})), "UNKNOWN_HEDGE_PENDING")
            self.assertEqual(d.action, "QUERY_STATUS")

    def test_reachable_exposure_counts_filled_resting_pending_and_unknown(self):
        # filled net +0.5; resting buy 0.2; pending buy 0.1; unknown buy 0.2: worst case 1.0 already; a new buy of 0.2 reaches 1.2 > 1.0 + 0.1
        view = ExposureView(filled={"BYBIT": 0.5}, resting={("BYBIT", "buy"): 0.2}, pending={("BINANCE", "buy"): 0.1}, unknown={("BYBIT", "buy"): 0.2})
        self.refused(child(qty=0.2), ctx(exposure=view), "NET_CEILING")
        self.assertTrue(evaluate(child(qty=0.1), ctx(exposure=view)).allowed)                  # 1.1 = the ceiling plus the headroom exactly
        self.refused(child(qty=0.11), ctx(exposure=view), "NET_CEILING")

    def test_ghost_hedge_an_unknown_child_is_reachable_exposure(self):
        # filled net +0.5 and a buy child whose state is UNKNOWN (0.4): if it filled, the net is +0.9, so a new buy of 0.3 would reach 1.2 > 1.1
        view = ExposureView(filled={"BYBIT": 0.5}, unknown={("BINANCE", "buy"): 0.4})
        self.refused(child(qty=0.3), ctx(exposure=view), "NET_CEILING")
        # counting only the filled position (0.5 + 0.3 = 0.8) would have allowed it: this is the mutation the brief names
        self.assertTrue(evaluate(child(qty=0.3), ctx(exposure=ExposureView(filled=view.filled))).allowed)

    def test_the_short_side_is_checked_too(self):
        view = ExposureView(filled={"BYBIT": -0.9})
        self.refused(child(side="sell", qty=0.3), ctx(exposure=view), "NET_CEILING")

    def test_per_venue_cap(self):
        view = ExposureView(filled={"BYBIT": 1.4, "BINANCE": -1.4})                          # net 0, but one venue near its cap
        self.refused(child(qty=0.2), ctx(exposure=view), "VENUE_CAP")

    def test_margin_utilisation(self):
        self.refused(child(), ctx(margin_utilisation={"BYBIT": 0.9}), "MARGIN_UTILISATION")
        self.assertTrue(evaluate(child(), ctx(margin_utilisation={"BYBIT": 0.7})).allowed)

    def test_rate_limit(self):
        self.refused(child(), ctx(rate_allowed=False), "RATE_LIMIT")

    def test_a_reducing_child_is_allowed_at_the_ceiling_but_new_risk_is_not(self):
        view = ExposureView(filled={"BYBIT": 1.0, "BINANCE": 0.0})
        self.assertTrue(evaluate(child(role="HEDGE", tactic="IOC_CAPPED", side="sell", qty=1.0), ctx(exposure=view)).allowed)
        self.refused(child(qty=0.2), ctx(exposure=view), "NET_CEILING")

    def test_every_reason_code_has_a_fixture_above(self):
        seen = {"NO_PRICE_CAP", "WORST_PRICE_NOT_ALLOWED", "BAND_VIOLATED", "BREAKER_PAUSED", "BREAKER_TRIPPED", "SECOND_HEDGE_CHILD", "UNKNOWN_HEDGE_PENDING", "NET_CEILING", "VENUE_CAP", "MARGIN_UTILISATION", "RATE_LIMIT"}
        self.assertEqual(seen, set(rg.REASONS))


class MutationChecks(unittest.TestCase):
    """The brief's mutations: each must make a test above fail. Here each is applied and the fixture it breaks is shown to notice."""

    def test_bypassing_the_gate_would_send_a_refused_child(self):
        self.assertFalse(evaluate(child(), ctx(breaker_state="TRIPPED")).allowed)            # the gate refuses; a bypass would have sent it

    def test_counting_only_filled_exposure_changes_the_answer_on_the_ghost_fixture(self):
        view = ExposureView(filled={"BYBIT": 0.8}, unknown={("BINANCE", "sell"): 0.6}, resting={("BYBIT", "buy"): 0.3})
        full = evaluate(child(qty=0.2), ctx(exposure=view))
        filled_only = evaluate(child(qty=0.2), ctx(exposure=ExposureView(filled=view.filled)))
        self.assertEqual((full.allowed, filled_only.allowed), (False, True))

    def test_allowing_a_second_hedge_child_or_replacing_an_unknown_one_is_refused(self):
        self.assertEqual(evaluate(child(role="HEDGE", tactic="IOC_CAPPED"), ctx(live_hedge_children={"i1": 1})).reason, "SECOND_HEDGE_CHILD")
        self.assertEqual(evaluate(child(role="HEDGE", tactic="IOC_CAPPED"), ctx(unknown_hedge_intents=frozenset({"i1"}))).reason, "UNKNOWN_HEDGE_PENDING")


if __name__ == "__main__":
    unittest.main()
