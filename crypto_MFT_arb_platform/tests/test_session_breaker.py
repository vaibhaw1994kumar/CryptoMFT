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


class DustAndFlattenRules(unittest.TestCase):
    """Owner 13 Nov. (1) The net-exposure trigger ignores a residual below one lot of the hedge venue (untradeable dust: still counted, shown in the summary); one lot or more held past the time limit trips as before.
    (2) After a trip, a flatten remainder below the venue minimum is DUST: recorded once, not retried, the trip stays latched until a manual reset; a venue that is unavailable is retried with a backoff (1 s doubling to 30 s)
    and logged once per state change plus a counter, never once per event."""
    net_cfg = CircuitBreakerConfig(net_exposure_s=5.0)

    def test_the_trigger_ignores_dust_and_trips_at_one_lot(self):
        from crypto_stat_arb.circuit_breaker import CircuitBreaker
        b = CircuitBreaker(self.net_cfg, A, B)
        for t in range(0, 40):
            b.on_net_update(float(t), 0.0076, 0, dust_below=0.01)                       # 0.0076 SOL carried for 40 s: no trip
        self.assertFalse(b.is_tripped())
        self.assertEqual((b.dust_base, b.max_dust_base), (0.0076, 0.0076))
        self.assertEqual(b.summary()["dust_residual_base"], 0.0076)                       # counted in the exposure report
        b2 = CircuitBreaker(self.net_cfg, A, B)
        for t in (0.0, 3.0, 5.0):
            b2.on_net_update(t, 0.01, 0, dust_below=0.01)
        self.assertFalse(b2.is_tripped())                                                 # not yet past the 5 s limit
        b2.on_net_update(6.0, 0.01, 0, dust_below=0.01)
        self.assertTrue(b2.is_tripped())                                                  # 0.01 SOL (one lot) held past the limit trips
        self.assertIn("net_exposure", b2.trips[-1].cause)
        b3 = CircuitBreaker(self.net_cfg, A, B)
        b3.on_net_update(0.0, 0.0076, 0)
        b3.on_net_update(6.0, 0.0076, 0)
        self.assertTrue(b3.is_tripped())                                                  # without a dust threshold the old rule stands
        self.assertNotIn("dust_residual_base", CircuitBreaker(self.net_cfg, A, B).summary())      # every older result keeps its exact keys

    def test_in_the_session_a_part_lot_net_does_not_trip_and_one_lot_does(self):
        s = session(self.net_cfg)
        with_books(s)
        s._breaker_step(1 * S)
        s.ledger.positions[(A, INSTR)] = Decimal("0.0004")                                # below the hedge venue's lot (0.001)
        for t in range(2, 30):
            s._breaker_step(t * S)
        self.assertEqual(s.breaker.state(), "OK")
        s.ledger.positions[(A, INSTR)] = Decimal("0.001")                                 # one lot
        s._breaker_step(30 * S)
        s._breaker_step(36 * S)
        self.assertEqual(s.breaker.state(), "TRIPPED")
        self.assertIn("net_exposure", events(s, "BREAKER_TRIP")[0]["cause"])

    def test_carried_hedge_dust_does_not_trip_the_net_trigger_but_an_uncarried_lot_does(self):
        """Owner 16 Nov (hedge dust carry): exposure recorded as carried dust is not what the net-exposure trigger reads, however large it is against the lot; anything not carried still trips."""
        from crypto_stat_arb.hedge_lifecycle import HedgePolicy
        s = session(self.net_cfg, hedge_policy=HedgePolicy(dust_carry="next_hedge"))
        with_books(s)
        s._breaker_step(1 * S)
        s.oms._dust[(A, INSTR)] = {"exposure": Decimal("0.04"), "since_ns": 1 * S, "alarmed": False, "host": None}      # 0.04 carried: four lots of 0.01, 3.96 USDT, below the 5 USDT minimum
        s.ledger.positions[(B, INSTR)] = Decimal("0.04")
        for t in range(2, 40):
            s._breaker_step(t * S)
        self.assertEqual(s.breaker.state(), "OK")                                          # carried dust: no trip, however long it is held
        s.ledger.positions[(B, INSTR)] = Decimal("0.041")                                   # one lot (0.001) that is NOT carried
        s._breaker_step(40 * S)
        s._breaker_step(46 * S)
        self.assertEqual(s.breaker.state(), "TRIPPED")
        self.assertIn("net_exposure", events(s, "BREAKER_TRIP")[0]["cause"])

    def test_the_same_dust_without_the_switch_trips_as_before(self):
        s = session(self.net_cfg)
        with_books(s)
        s._breaker_step(1 * S)
        s.ledger.positions[(B, INSTR)] = Decimal("0.04")
        s._breaker_step(2 * S)
        s._breaker_step(8 * S)
        self.assertEqual(s.breaker.state(), "TRIPPED")

    def test_a_flatten_remainder_below_the_minimum_is_dust_recorded_once_never_retried_and_latched_until_reset(self):
        cfg = CircuitBreakerConfig(trip_on_gateway_down=True, pause_on_gateway_down_when_flat=True, heartbeat_timeout_s=2.0)
        s = session(cfg)
        with_books(s)
        s._breaker_step(1 * S)
        s.ledger.positions[(A, INSTR)] = Decimal("0.0004")
        for k in range(0, 400):                                                           # every 100 ms for 40 s: an event-rate retry would show here
            s._breaker_step(20 * S + k * S // 10)
        self.assertEqual(s.breaker.state(), "TRIPPED")
        self.assertEqual(len(events(s, "BREAKER_FLATTEN_DUST")), 1)
        dust = events(s, "BREAKER_FLATTEN_DUST")[0]
        self.assertEqual((dust["venue"], dust["residual_base"]), (A, "0.0004"))
        for key in ("min_quantity", "min_notional", "lot"):
            self.assertIn(key, dust)
        self.assertEqual(events(s, "BREAKER_FLATTEN", "RISK_FLATTEN_OPENED", "UNWIND_VENUE_SKIPPED", "BREAKER_ALARM"), [])      # no flatten plan, no per-event skip, no alarm for dust
        self.assertEqual(s.breaker_flatten_counters["dust_recorded"], 1)
        self.assertEqual(events(s, "BREAKER_TRIP")[0]["dust_residual_base"], 0.0004)
        self.assertEqual(s.breaker_report()["breaker_flatten"]["dust_now"]["residual_base"], "0.0004")      # shown in the report
        s.reset_breaker(70 * S, "operator-1")
        self.assertIsNone(s._breaker_dust)                                                # a manual reset clears it

    def test_an_unavailable_venue_is_retried_with_a_doubling_backoff_and_the_alarm_is_logged_once(self):
        cfg = CircuitBreakerConfig(trip_on_gateway_down=True, pause_on_gateway_down_when_flat=True, heartbeat_timeout_s=2.0, tripped_unwind_retries=50)
        s = session(cfg)                                                                  # no books: no venue can be flattened on
        s._breaker_step(1 * S)
        s.ledger.positions[(A, INSTR)] = Decimal("5")
        times = []
        for k in range(0, 1200):                                                          # every 100 ms from 20 s to 140 s
            t = 20 * S + k * S // 10
            before = s.breaker_flatten_counters["unavailable_retries"]
            s._breaker_step(t)
            if s.breaker_flatten_counters["unavailable_retries"] != before:
                times.append(round((t - 20 * S) / S, 1))
        gaps = [round(b - a, 1) for a, b in zip(times, times[1:])]
        self.assertEqual(gaps[:5], [1.0, 2.0, 4.0, 8.0, 16.0])                            # 1 s doubling
        self.assertTrue(all(g <= 30.1 for g in gaps) and 30.0 in gaps)                    # capped at 30 s
        self.assertLess(len(times), 15)                                                   # not on every event (1200 steps)
        self.assertEqual(len(events(s, "BREAKER_ALARM")), 1)

    def test_an_unavailable_flatten_venue_logs_one_event_per_state_change_not_one_per_event(self):
        s = session()
        s.oms.open_flatten_plan(A, INSTR, Decimal("5"), 10 * S, "test")                    # no book on A
        self.assertEqual(len(events(s, "UNWIND_VENUE_SKIPPED")), 1)
        for k in range(1, 3000):                                                          # a retry on every book event for 3 s
            s.oms.retry_flatten(A, 10 * S + k * S // 1000)
        self.assertEqual(len(events(s, "UNWIND_VENUE_SKIPPED")), 1)
        self.assertEqual(s.oms.realism_counters["unwind_venue_skips"], 1)
        self.assertGreater(s.oms.realism_counters["unwind_venue_skip_retries_suppressed"], 2000)
        (key,) = [k for k in s.oms._unwind_skip]
        delays = []
        for _ in range(8):                                                                # retried when due: the delay doubles, capped at 30 s
            due = s.oms._unwind_skip[key]["next_ns"]
            s.oms.retry_flatten(A, due)
            delays.append(s.oms._unwind_skip[key]["delay_ns"] // S)
        self.assertEqual(delays, [4, 8, 16, 30, 30, 30, 30, 30])           # (the retries due inside the 3 s loop already took it 1 s -> 2 s -> 4 s)
        self.assertEqual(len(events(s, "UNWIND_VENUE_SKIPPED")), 1)                       # still one: the state did not change
        with_books(s)                                                                     # the venue is back
        s.oms.retry_flatten(A, s.oms._unwind_skip[key]["next_ns"])
        self.assertEqual(len(events(s, "UNWIND_VENUE_AVAILABLE")), 1)
        self.assertTrue(any(o.order_role == "UNWIND" for p in s.oms.plans.values() for o in p.children))

    def test_a_flatten_plan_below_every_minimum_is_dust_once_and_is_not_retried(self):
        s = session()
        with_books(s)
        plan = s.oms.open_flatten_plan(A, INSTR, Decimal("0.0004"), 10 * S, "test")
        self.assertEqual(len(events(s, "UNWIND_DUST")), 1)
        self.assertEqual(events(s, "UNWIND_VENUE_SKIPPED"), [])
        self.assertEqual(s.oms.realism_counters["unwind_dust_plans"], 1)
        for k in range(1, 500):
            s.oms.retry_flatten(A, 10 * S + k * S // 100)
        self.assertEqual((len(events(s, "UNWIND_DUST")), len(events(s, "UNWIND_VENUE_SKIPPED"))), (1, 0))
        self.assertEqual([o for o in plan.children if o.order_role == "UNWIND"], [])


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


class V1_10Rules(unittest.TestCase):
    """Owner 16 Nov (v1_10): the 5 s mark rule by the mark's own timestamp (boundary 4.9 / 5.0 / 5.1 s), the real-order amend guard, and the 1 s no-requote after a validity withdrawal."""

    def sess(self, stamp):
        from dataclasses import replace
        s = session()
        s.margin_config = replace(s.margin_config, mark_stamp=stamp)
        return s

    def test_a_mark_older_than_5_s_by_its_own_timestamp_is_missing_4_9_5_0_5_1(self):
        for age_s, usable in ((4.9, True), (5.0, True), (5.1, False)):
            s = self.sess("own_timestamp")
            s.exchange_marks[(A, INSTR)] = (10 * S, Decimal("100"))
            now = 10 * S + int(age_s * S)
            marks = s._margin_marks(now)
            self.assertEqual((A, INSTR) in marks, usable, age_s)

    def test_nothing_is_restamped_so_a_mark_never_outlives_5_s_by_its_own_time(self):
        s = self.sess("own_timestamp")
        s.exchange_marks[(A, INSTR)] = (10 * S, Decimal("100"))
        self.assertIn((A, INSTR), s._margin_marks(10 * S + int(4.9 * S)))       # seen at 4.9 s ...
        self.assertNotIn((A, INSTR), s._margin_marks(10 * S + int(9.8 * S)))     # ... and gone at 9.8 s: the legacy rule re-stamped it at 4.9 s and kept it to 9.9 s

    def test_the_legacy_rule_still_restamps_and_is_labelled_legacy(self):
        s = self.sess("restamp")
        s.exchange_marks[(A, INSTR)] = (10 * S, Decimal("100"))
        s._margin_marks(10 * S + int(4.9 * S))
        self.assertIn((A, INSTR), s._margin_marks(10 * S + int(9.8 * S)))        # lives 9.8 s: what every profile up to v1_9 does
        from crypto_stat_arb.margin import MarginConfig
        self.assertEqual(MarginConfig().mark_stamp, "restamp")                    # the default: frozen profiles keep it

    def test_a_newer_mark_replaces_the_older_one(self):
        s = self.sess("own_timestamp")
        s.exchange_marks[(A, INSTR)] = (10 * S, Decimal("100"))
        s._margin_marks(11 * S)
        s.exchange_marks[(A, INSTR)] = (14 * S, Decimal("101"))
        self.assertEqual(s._margin_marks(18 * S)[(A, INSTR)], Decimal("101"))

    def test_the_amend_guard_blocks_real_orders_until_both_proofs_are_recorded(self):
        from crypto_stat_arb.config import amend_guard_from_profile
        from crypto_stat_arb.session import ExecutionMode
        from crypto_stat_arb.ems.tactics.passive import RepriceConfig
        prof = {"execution": {"primary": {"reprice": {"enabled": True, "amend_first": True}}}, "decision_status": {"case2i_passed": True}}
        guard = amend_guard_from_profile(prof)
        self.assertEqual(guard["recorded"], {"case2i_passed": True, "testnet_amend_drill_passed": False})
        s = session()
        s._reprice_cfg = RepriceConfig(enabled=True, band_down_bps=1.0, min_interval_ms=1000.0, max_age_s=15.0, amend_first=True)
        s.oms.amend_enabled = True
        s._apply_amend_guard(guard, ExecutionMode.INTERNAL_SIMULATION)             # a replay is not restricted
        self.assertIsNone(s.amend_guard_state)
        s._apply_amend_guard(guard, ExecutionMode.VENUE_TESTNET)
        self.assertEqual(s.amend_guard_state["missing"], ["testnet_amend_drill_passed"])
        self.assertFalse(s._reprice_cfg.amend_first)                                # every reprice is a cancel and a fresh quote
        self.assertEqual(s.oms.request_amend("x", Decimal("1"), 1), "REFUSED:amend_guard:testnet_amend_drill_passed")
        self.assertEqual(s.oms.realism_counters["amend_refused_real_order_guard"], 1)
        both = amend_guard_from_profile({**prof, "decision_status": {"case2i_passed": True, "testnet_amend_drill_passed": True}})
        s2 = session()
        s2._reprice_cfg = RepriceConfig(enabled=True, band_down_bps=1.0, min_interval_ms=1000.0, max_age_s=15.0, amend_first=True)
        s2._apply_amend_guard(both, ExecutionMode.VENUE_TESTNET)
        self.assertIsNone(s2.amend_guard_state)
        self.assertTrue(s2._reprice_cfg.amend_first)
        self.assertIsNone(amend_guard_from_profile({"execution": {"primary": {"reprice": {"enabled": False}}}}))

    def test_a_validity_withdrawal_blocks_the_same_venue_and_side_for_the_delay_and_is_counted(self):
        from types import SimpleNamespace
        from crypto_stat_arb.strategies.arb_strategy import ArbSessionStrategy
        st = ArbSessionStrategy.__new__(ArbSessionStrategy)
        sig = SimpleNamespace(maker_venue="bybit", buy_venue="bybit")               # a passive bid on bybit
        st.strategy = SimpleNamespace(evaluate=lambda *a, **k: sig, config=SimpleNamespace(count_pending_exposure=False, requote_delay_after_withdraw_s=1.0))
        st.eligibility_policy, st.entry_gate, st.view_books = None, None, (lambda: {})
        st.pending_and_live_makers = lambda: (None, 0)
        st.inventory = lambda: None
        st.to_opportunity = lambda *a, **k: SimpleNamespace(opportunity_id="o1")
        st.signals, st.last_signal = {}, None
        st._no_requote_until_ns, st.requote_blocked_after_withdraw, st.withdrawn_by_validity = {}, 0, 0
        st.states = {}
        self.assertIsNotNone(st.evaluate({}, 5 * S))                                  # nothing withdrawn yet
        st.note_quote_withdrawn({"venue_feed": "bybit", "side": "buy"}, 10 * S)
        self.assertIsNone(st.evaluate({}, 10 * S + 999_000_000))                      # 0.999 s later: blocked
        self.assertIsNotNone(st.evaluate({}, 11 * S))                                 # 1.0 s later: quoting again
        self.assertEqual((st.requote_blocked_after_withdraw, st.withdrawn_by_validity), (1, 1))
        st.strategy.config.requote_delay_after_withdraw_s = 0.0                       # off: a later withdrawal starts no delay
        st.note_quote_withdrawn({"venue_feed": "bybit", "side": "buy"}, 20 * S)
        self.assertIsNotNone(st.evaluate({}, 20 * S + 1))
