"""Track A item 1 (owner 11 Nov): the circuit breaker and the pre-send risk gate on TradingSession. Off by default. Scripted time, no replay: PAUSE versus TRIP, no new risk while paused or tripped, the cancel and the
net flatten on a trip priced at the touch plus the unwind band, the persistent alarm, the manual reset, the breaker fed from the hedge-chain events of the ledger, and the gate's count and enforce modes."""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb.circuit_breaker import CircuitBreakerConfig
from crypto_stat_arb.config import load_config
from crypto_stat_arb.ems.risk_gate import GateLimits
from crypto_stat_arb.models import MarketEvent, Opportunity
from crypto_stat_arb.session import ExecutionMode, TradingSession

S = 10 ** 9
CFG, _ = load_config(Path(__file__).resolve().parents[1] / "config/perp_research.json")
A, B = CFG.venues                                    # BINANCE, BYBIT
INSTR = CFG.instrument_id


def session(breaker=None, **kw):
    return TradingSession(CFG, execution_mode=ExecutionMode.INTERNAL_SIMULATION, breaker_config=breaker, **kw)


def snapshot(venue, ts_ns, bid="99.0", ask="101.0", seq=1):
    return MarketEvent.from_dict({"event_type": "BOOK_SNAPSHOT", "venue": venue, "instrument_id": INSTR, "exchange_ts_ns": ts_ns, "receive_ts_ns": ts_ns, "sequence": seq, "bids": [[bid, "5"]], "asks": [[ask, "5"]]})


def with_books(s, bid="99.0", ask="101.0"):
    for v in CFG.venues:
        s.books[(v, INSTR)].apply(snapshot(v, 1, bid, ask))
    s.oms._books.update(s.books)


def opp(qty="0.3", buy=A, sell=B):
    return Opportunity(opportunity_id="op-1", strategy_id="s-1", instrument_id=INSTR, observed_ts_ns=2, buy_venue=buy, sell_venue=sell, buy_price=Decimal("100.0"), sell_price=Decimal("100.1"),
                       quantity=Decimal(qty), gross_edge_bps=Decimal("10"), estimated_net_edge_bps=Decimal("2"), source_event_ids=("a", "b"))


class Fake:
    strategy_id = "fake"

    def __init__(self):
        self.calls = 0

    def evaluate(self, books, now):
        self.calls += 1
        return None


def events(s, *names):
    return [e for e in s.ledger.events if e["event_type"] in names]


class OffByDefault(unittest.TestCase):
    def test_nothing_runs_and_the_report_has_no_breaker_key(self):
        s = session()
        self.assertIsNone(s.breaker)
        s._breaker_step(5 * S)
        self.assertEqual(s.ledger.events and [e["event_type"] for e in s.ledger.events if e["event_type"].startswith("BREAKER")], [])
        self.assertNotIn("breaker_and_gate", s.strategy_report())
        self.assertEqual(s.risk_gate_mode, "off")

    def test_a_bad_gate_mode_and_a_reset_without_a_breaker_are_refused(self):
        with self.assertRaises(ValueError):
            session(risk_gate_mode="maybe")
        with self.assertRaises(ValueError):
            session().reset_breaker(S)


class PauseVersusTrip(unittest.TestCase):
    cfg = CircuitBreakerConfig(trip_on_gateway_down=True, pause_on_gateway_down_when_flat=True, heartbeat_timeout_s=2.0)

    def test_a_silent_gateway_while_flat_pauses_blocks_new_signals_and_resumes_by_itself(self):
        fake = Fake()
        s = session(self.cfg, strategies=[fake])
        with_books(s)
        s.on_event(snapshot(A, 1 * S), gate_allowed=True)                     # both venues are graced at the first step
        self.assertEqual(s.breaker.state(), "OK")
        calls = fake.calls
        s.on_event(snapshot(A, 5 * S), gate_allowed=True)                     # BYBIT has said nothing for 4 s: the gateway looks down, the book is flat
        self.assertEqual(s.breaker.state(), "PAUSED")
        self.assertEqual((fake.calls, s.signals_blocked_by_breaker), (calls, 1))
        s.on_event(snapshot(B, 6 * S), gate_allowed=True)                     # it speaks again, nothing is open to reconcile
        self.assertEqual(s.breaker.state(), "OK")
        self.assertEqual(fake.calls, calls + 1)
        self.assertEqual([(e["previous_state"], e["state"]) for e in events(s, "BREAKER_STATE_TRANSITION")], [("OK", "PAUSED"), ("PAUSED", "OK")])

    def test_a_silent_gateway_with_exposure_trips_cancels_flattens_at_the_touch_band_and_stays_until_a_manual_reset(self):
        s = session(self.cfg)
        with_books(s, "99.0", "101.0")
        s._breaker_step(1 * S)
        s.ledger.positions[(A, INSTR)] = Decimal("5")
        s._breaker_step(20 * S)
        self.assertEqual(s.breaker.state(), "TRIPPED")
        (trip,) = events(s, "BREAKER_TRIP")
        self.assertIn("gateway_down", trip["cause"])
        for key in ("confirmed_residual_base", "pending_hedge_qty", "oldest_exposure_age_s", "execution_blocked_reason"):                                                    # owner 12 Nov: an alert carries the exposure and why execution is blocked
            self.assertIn(key, trip)
        self.assertEqual(trip["execution_blocked_reason"], "breaker:TRIPPED")
        (flat,) = events(s, "BREAKER_FLATTEN")
        self.assertEqual((flat["venue"], flat["side"], flat["quantity"], flat["tactic"]), (A, "sell", Decimal("5"), "MARKET_BANDED"))
        self.assertAlmostEqual(flat["worst_price"], 99.0 * (1 - 15 / 1e4))            # the touch the sell crosses (the bid), less the unwind band: 15 bps
        (plan,) = [p for p in s.oms.plans.values() if p.inherited_base]
        (unwind,) = plan.children
        self.assertEqual((unwind.order_role, unwind.side.value, unwind.quantity, unwind.limit_price), ("UNWIND", "SELL", Decimal("5"), Decimal("98.90")))      # on the 0.1 tick, never looser than the band (owner 12 Nov: a sell limit rounds UP; 98.8515 -> 98.90, it was 98.80 before)
        for v in CFG.venues:                                                          # everything is back up: a TRIP does not resume by itself
            s.note_venue_message(v, 25 * S)
        s._breaker_step(25 * S)
        self.assertEqual(s.breaker.state(), "TRIPPED")
        self.assertFalse(s.breaker.allow_signal(25.0, 5.0))
        s.reset_breaker(30 * S, "operator-1")
        self.assertEqual((s.breaker.state(), s.breaker.summary()["manual_resets"]), ("OK", 1))
        self.assertEqual(events(s, "BREAKER_RESET")[0]["operator_id"], "operator-1")

    def test_a_trip_with_no_valid_book_to_flatten_on_is_a_persistent_alarm_once(self):
        s = session(self.cfg)                                                         # no books at all: nothing can be flattened on
        s._breaker_step(1 * S)
        s.ledger.positions[(A, INSTR)] = Decimal("5")
        s._breaker_step(20 * S)
        s._breaker_step(25 * S)
        self.assertEqual(events(s, "BREAKER_FLATTEN"), [])
        self.assertEqual([a["reason"] for a in s.breaker_alarms], ["NO_AVAILABLE_VENUE_TO_FLATTEN_ON"])
        self.assertEqual(len(events(s, "BREAKER_ALARM")), 1)
        for key in ("confirmed_residual_base", "pending_hedge_qty", "oldest_exposure_age_s", "execution_blocked_reason"):
            self.assertIn(key, events(s, "BREAKER_ALARM")[0])
        self.assertEqual(s.breaker_report()["breaker_alarms"][0]["quantity"], 5.0)


class FedFromTheHedgeEvents(unittest.TestCase):
    def hedge(self, s, chain, to, frm="SUBMITTED", ts=2 * S):
        s.ledger.record("HEDGE_STATE", ts, parent_id="p", chain_id=chain, from_state=frm, to_state=to, reason="test")

    def test_two_failed_chains_in_a_row_trip_and_a_success_in_between_does_not(self):
        s = session(CircuitBreakerConfig(consecutive_failures=2))
        s._breaker_step(1 * S)
        self.hedge(s, "c1", "REJECTED")
        self.hedge(s, "c1", "HEDGED", "RETRYING")                                    # a retry that succeeds is still the failure it began as: counted once
        s._breaker_step(3 * S)
        self.assertEqual(s.breaker.state(), "OK")
        self.hedge(s, "c2", "HEDGED", ts=4 * S)                                      # a clean chain resets the run
        self.hedge(s, "c3", "REJECTED", ts=5 * S)
        s._breaker_step(6 * S)
        self.assertEqual(s.breaker.state(), "OK")
        self.hedge(s, "c4", "ESCALATED", ts=7 * S)
        s._breaker_step(8 * S)
        self.assertEqual(s.breaker.state(), "TRIPPED")

    def test_an_exhausted_unwind_trips_when_configured(self):
        s = session(CircuitBreakerConfig(trip_on_failed_unwind=True))
        s._breaker_step(1 * S)
        s.ledger.record("UNWIND_EXHAUSTED_ALARM", 2 * S, parent_id="p")
        s._breaker_step(3 * S)
        self.assertEqual(s.breaker.state(), "TRIPPED")

    def test_a_hedge_unknown_for_too_long_trips_and_is_cleared_when_resolved(self):
        s = session(CircuitBreakerConfig(unknown_order_s=5.0))
        s._breaker_step(1 * S)
        self.hedge(s, "c1", "UNKNOWN", ts=2 * S)
        s._breaker_step(4 * S)
        self.assertEqual(s.breaker.state(), "OK")
        self.hedge(s, "c1", "HEDGED", "UNKNOWN", ts=5 * S)
        s._breaker_step(20 * S)
        self.assertEqual(s.breaker.state(), "OK")                                     # resolved before the limit: no trip
        self.hedge(s, "c2", "UNKNOWN", ts=21 * S)
        s._breaker_step(30 * S)
        self.assertEqual(s.breaker.state(), "TRIPPED")


class RiskGateModes(unittest.TestCase):
    limits = GateLimits(net_ceiling=1.0, fill_headroom=0.1, venue_cap=1.5, band_bps=15.0)

    def test_count_logs_what_it_would_refuse_and_sends_enforce_refuses(self):
        for mode, sends in (("count", True), ("enforce", False)):
            s = session(risk_gate_mode=mode, gate_limits=self.limits)
            with_books(s)
            s.ledger.positions[(A, INSTR)] = Decimal("0.9")                           # 0.9 filled + 0.3 more is 1.2 > ceiling 1.0 + headroom 0.1
            self.assertEqual(s._risk_gate_allows(opp("0.3"), A, 5 * S), sends, mode)
            self.assertEqual(s.gate_counts, {"NET_CEILING": 1})
            self.assertEqual(len(events(s, "RISK_GATE_REFUSED" if mode == "enforce" else "RISK_GATE_WOULD_REFUSE")), 1)
            self.assertTrue(s._risk_gate_allows(opp("0.1"), A, 6 * S))                # 1.0 is inside the ceiling
            self.assertEqual(s.breaker_report()["risk_gate_refusals_by_reason"], {"NET_CEILING": 1})

    def test_a_refused_dispatch_never_reaches_the_oms_and_off_never_asks_the_gate(self):
        s = session(risk_gate_mode="enforce", gate_limits=self.limits)
        with_books(s)
        s.ledger.positions[(A, INSTR)] = Decimal("0.9")
        with mock.patch.object(s.oms, "execute") as execute:
            self.assertIsNone(s._dispatch(Fake(), opp("0.3"), 5 * S, s.wall_ns()))
            execute.assert_not_called()
        off = session()
        with mock.patch.object(off, "_risk_gate_allows") as gate, mock.patch.object(off.oms, "execute", return_value=mock.Mock(parent_id="p", state=mock.Mock(value="CREATED"))):
            off._dispatch(Fake(), opp("0.3"), 5 * S, off.wall_ns())
            gate.assert_not_called()

    def test_a_tripped_breaker_blocks_new_risk_at_the_gate_and_resting_primary_orders_count_as_reachable_exposure(self):
        s = session(CircuitBreakerConfig(trip_on_failed_unwind=True), risk_gate_mode="enforce", gate_limits=self.limits)
        with_books(s)
        s._breaker_step(1 * S)
        s.ledger.record("UNWIND_EXHAUSTED_ALARM", 2 * S, parent_id="p")
        s._breaker_step(3 * S)
        self.assertEqual(s.breaker.state(), "TRIPPED")
        self.assertFalse(s._risk_gate_allows(opp("0.1"), A, 4 * S))
        self.assertEqual(s.gate_counts, {"BREAKER_TRIPPED": 1})


class MarginMarks(unittest.TestCase):
    """Owner 11 Nov: the margin check marks a position with the venue's own mark when a fresh one arrived, else the mid of a VALID book, and keeps the last good mark for mark_max_age (5 s, ASSUMED)."""

    def test_a_valid_book_gives_its_mid_a_stale_one_keeps_the_last_good_mid_for_five_seconds(self):
        s = session()
        with_books(s)
        marks = s._margin_marks(5 * S)
        self.assertEqual(marks, {(A, INSTR): Decimal("100"), (B, INSTR): Decimal("100")})
        s.books[(A, INSTR)].start_resync("test")                                                      # the book is no longer VALID
        self.assertEqual(s._margin_marks(7 * S)[(A, INSTR)], Decimal("100"))                         # 2 s old: still the last good mark
        self.assertEqual(set(s._margin_marks(11 * S)), {(B, INSTR)})                                 # 6 s old: missing

    def test_a_fresh_exchange_mark_survives_a_stale_book_and_wins_over_the_mid_an_old_one_is_ignored(self):
        from crypto_stat_arb.derivatives import DerivativeTicker
        s = session()
        with_books(s)
        tick = lambda v, ts, mark: DerivativeTicker(event_id=f"d-{v}-{ts}", venue=v, instrument_id=INSTR, symbol="X", exchange_ts_ns=ts, source_ts_ns=ts, receive_ts_ns=ts, mark_price=Decimal(mark),
                                                    index_price=None, funding_rate=None, next_funding_ts_ns=None)
        s.on_derivative(tick(A, 4 * S, "100.3"))                                                      # funding is not configured: the mark is still kept
        self.assertEqual(s._margin_marks(5 * S)[(A, INSTR)], Decimal("100.3"))                       # wins over the book mid 100
        s.books[(A, INSTR)].start_resync("test")
        self.assertEqual(s._margin_marks(5 * S)[(A, INSTR)], Decimal("100.3"))                       # the book is stale, the mark is still there
        self.assertNotIn((A, INSTR), s._margin_marks(15 * S))                                         # 11 s old: not used


def margin_session(action="pause"):
    """A session with one short BINANCE position so that a missing BINANCE price is an unpriced liability (the day-1 case)."""
    from dataclasses import replace
    from crypto_stat_arb.models import ChildOrder, OrderState, Side
    s = TradingSession(replace(CFG, margin=replace(CFG.margin, missing_price_action=action)), execution_mode=ExecutionMode.INTERNAL_SIMULATION)
    q = Decimal("0.1")
    s.ledger.apply_fill(ChildOrder(order_id="f1", parent_id="p", opportunity_id="o", venue=A, instrument_id=INSTR, side=Side.SELL, quantity=q, limit_price=Decimal("100"), state=OrderState.FILLED,
                                   filled_quantity=q, average_price=Decimal("100")), 1)
    with_books(s)
    return s


class MissingMarginPrice(unittest.TestCase):
    """Owner 11 Nov: a missing price pauses new risk and resumes by itself; it never latches the kill switch. Only a real shortfall with a fresh price trips it."""

    def test_a_500_ms_stale_binance_book_causes_no_breach(self):
        s = margin_session()
        s._evaluate_margin(5 * S)
        s.books[(A, INSTR)].start_resync("stale")                                                     # the book is STALE from here
        s._evaluate_margin(5 * S + S // 2)
        self.assertFalse(s.risk.kill_switch)
        self.assertFalse(s.margin_price_paused)
        self.assertEqual(s.risk.margin_states[A].value, "HEALTHY")

    def test_a_ten_second_missing_price_pauses_then_resumes_and_never_trips(self):
        s = margin_session()
        s._evaluate_margin(5 * S)
        s.books[(A, INSTR)].start_resync("stale")
        s._evaluate_margin(11 * S)                                                                    # last good mark is 6 s old: the price is missing
        self.assertTrue(s.margin_price_paused)
        self.assertFalse(s.risk.kill_switch)
        fake = Fake()
        s.strategies = [fake]
        s._margin_due_ns = 10 ** 18                                                                   # no margin evaluation inside this event: the pause stands
        s.on_event(snapshot(B, 12 * S, seq=2), now_ns=12 * S, gate_allowed=True)
        self.assertEqual(fake.calls, 0)                                                               # no new risk while paused
        self.assertEqual(s.signals_blocked_by_margin_price, 1)
        s._margin_due_ns = None
        s.books[(A, INSTR)].apply(snapshot(A, 15 * S, seq=3))                                         # a fresh price is back
        s.oms._books.update(s.books)
        s._evaluate_margin(16 * S)
        self.assertFalse(s.margin_price_paused)
        self.assertFalse(s.risk.kill_switch)
        self.assertEqual([e["event_type"] for e in events(s, "MARGIN_PRICE_PAUSE", "MARGIN_PRICE_RESUME")], ["MARGIN_PRICE_PAUSE", "MARGIN_PRICE_RESUME"])
        pause, resume = events(s, "MARGIN_PRICE_PAUSE", "MARGIN_PRICE_RESUME")
        for key in ("confirmed_residual_base", "pending_hedge_qty", "oldest_exposure_age_s", "execution_blocked_reason"):                  # owner 12 Nov: the pause is an alert and says why new risk stops
            self.assertIn(key, pause)
            self.assertNotIn(key, resume)                                                                                              # the resume is not
        self.assertEqual(pause["execution_blocked_reason"], "margin_price_missing")

    def test_a_real_shortfall_with_a_fresh_price_still_trips_the_kill_switch(self):
        from crypto_stat_arb.derivatives import DerivativeTicker
        s = margin_session()
        s.on_derivative(DerivativeTicker(event_id="d1", venue=A, instrument_id=INSTR, symbol="X", exchange_ts_ns=5 * S, source_ts_ns=5 * S, receive_ts_ns=5 * S, mark_price=Decimal("5000000"),
                                         index_price=None, funding_rate=None, next_funding_ts_ns=None))
        s._evaluate_margin(5 * S)
        self.assertFalse(s.margin_price_paused)
        self.assertEqual(s.risk.margin_states[A].value, "BREACH")
        self.assertTrue(s.risk.kill_switch)
        trip = events(s, "KILL_SWITCH_STATE_TRANSITION")[0]                                                                              # owner 12 Nov: the kill switch event carries the alert fields
        for key in ("confirmed_residual_base", "pending_hedge_qty", "oldest_exposure_age_s", "execution_blocked_reason"):
            self.assertIn(key, trip)
        self.assertTrue(trip["execution_blocked_reason"].startswith("kill_switch:"))

    def test_the_old_rule_is_the_default_without_the_pause_setting(self):
        s = margin_session(action="breach")
        s._evaluate_margin(5 * S)
        s.books[(A, INSTR)].start_resync("stale")
        s._evaluate_margin(11 * S)
        self.assertEqual(s.risk.margin_states[A].value, "BREACH")                                     # missing price: fail closed, as before
        self.assertTrue(s.risk.kill_switch)


if __name__ == "__main__":
    unittest.main()
