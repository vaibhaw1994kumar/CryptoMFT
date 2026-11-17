"""W1 feed health and clock (owner 17 Nov). The state model (HEALTHY / DEGRADED / UNSAFE per venue, instrument and stream, with hysteresis), the rollups, the time-integrity fixes and boundaries, the
host clock's independent reference, and the session wiring: observation by default, two actions behind switches that are off. SYNTHETIC streams: nothing here is a profit figure."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from crypto_stat_arb.config import feed_health_from_profile
from crypto_stat_arb.models import MarketEvent
from crypto_stat_arb.time_integrity import ClockMeasurement, FeedHealthMonitor, FeedHealthPolicy, HostClockMonitor, TimeIntegrityMonitor, TimePolicy

S, MS = 10 ** 9, 10 ** 6


def policy(**kw):
    base = dict(delay_degraded_ns=500 * MS, delay_unsafe_ns=2000 * MS, silence_degraded_ns=500 * MS, silence_unsafe_ns=5000 * MS, own_lag_ns=100 * MS, recover_ns=2 * S)
    base.update(kw)
    return FeedHealthPolicy(**base)


def feed(m, venue, t_ns, delay=10 * MS, stream="book", lag=0, **kw):
    return m.observe(venue, "SOL", stream, receive_ts_ns=t_ns, now_ns=t_ns + lag, corrected_delay_ns=delay, **kw)


def moves(rows):
    return [(r["venue"], r["stream"], r["previous"], r["state"], r["cause"]) for r in rows]


class StateModel(unittest.TestCase):
    def test_a_quiet_stream_degrades_then_is_unsafe_and_recovers_only_after_the_hold_one_row_per_change(self):
        m = FeedHealthMonitor(policy())
        feed(m, "A", 1 * S); feed(m, "B", 1 * S)
        seen = []
        t = 1 * S
        while t < 8 * S:                                             # B keeps sending every 100 ms, A is silent from 1 s
            t += 100 * MS
            seen += feed(m, "B", t)
        self.assertEqual(moves(seen), [("A", "book", "HEALTHY", "DEGRADED", "silence"), ("A", "book", "DEGRADED", "UNSAFE", "silence")])
        self.assertEqual((seen[0]["ts_ns"], seen[1]["ts_ns"]), (1 * S + 600 * MS, 6 * S + 100 * MS))        # the first observation strictly beyond 500 ms and beyond 5 s
        back = []
        for k in range(1, 40):                                       # A is back, every 100 ms
            back += feed(m, "A", 8 * S + k * 100 * MS)
            back += feed(m, "B", 8 * S + k * 100 * MS + 1)
        self.assertEqual(moves(back), [("A", "book", "UNSAFE", "HEALTHY", "recovered")])                   # one recovery event, after 2 s of clean observations
        self.assertEqual(back[0]["ts_ns"], 8 * S + 100 * MS + 2 * S)
        rep = m.report(12 * S)["feeds"]["A|SOL|book"]
        self.assertEqual((rep["episodes"], rep["state"]), ({"DEGRADED": 1, "UNSAFE": 1}, "HEALTHY"))
        self.assertEqual(m.transition_count, 3)

    def test_the_delay_limits_are_strict_and_a_noisy_threshold_does_not_flap(self):
        m = FeedHealthMonitor(policy())
        self.assertEqual(feed(m, "A", 1 * S, delay=500 * MS), [])                                            # exactly at the limit: still HEALTHY
        self.assertEqual(moves(feed(m, "A", 1 * S + 10 * MS, delay=500 * MS + 1)), [("A", "book", "HEALTHY", "DEGRADED", "delay")])
        rows = []
        for k in range(1, 60):                                       # the delay flips around the limit every 50 ms for 3 s
            rows += feed(m, "A", 1 * S + 10 * MS + k * 50 * MS, delay=(500 * MS + 1) if k % 2 else 400 * MS)
        self.assertEqual(rows, [])                                   # no flapping: it stays DEGRADED
        self.assertEqual(m.state("A"), "DEGRADED")
        self.assertEqual(moves(feed(m, "A", 5 * S, delay=2000 * MS + 1)), [("A", "book", "DEGRADED", "UNSAFE", "delay")])
        self.assertEqual(feed(m, "A", 5 * S + 1, delay=2000 * MS), [])                                       # exactly the unsafe limit is not unsafe, but an improvement waits for the hold

    def test_a_trade_stream_is_judged_on_delay_only_and_does_not_make_the_book_stream_healthy(self):
        m = FeedHealthMonitor(policy())
        feed(m, "A", 1 * S); feed(m, "A", 1 * S, stream="trade", check_silence=False)
        rows = feed(m, "A", 9 * S, stream="trade", check_silence=False)                                      # 8 s with no trade and no book update
        self.assertEqual(moves(rows), [("A", "book", "HEALTHY", "UNSAFE", "silence")])                       # the BOOK stream is unsafe; the trade stream's own quiet is not a fault
        self.assertEqual(m.report()["feeds"]["A|SOL|trade"]["state"], "HEALTHY")
        self.assertEqual(m.state("A"), "UNSAFE")                                                             # the venue reads its worst stream

    def test_own_lag_is_a_flag_with_its_own_cause_not_the_venues_silence(self):
        m = FeedHealthMonitor(policy())
        feed(m, "A", 1 * S)
        self.assertEqual(feed(m, "A", 2 * S, lag=100 * MS), [])                                              # exactly the own-lag limit: no flag
        self.assertFalse(m.own_lag_active)
        rows = feed(m, "A", 2 * S + 10 * MS, lag=600 * MS)                                                   # processed 600 ms after it arrived
        self.assertTrue(m.own_lag_active)
        self.assertEqual(moves(rows), [("A", "book", "HEALTHY", "DEGRADED", "own_lag")])
        self.assertTrue(rows[0]["own_lag"])
        self.assertEqual(m.own_lag_episodes, 1)
        self.assertEqual(m.report()["feeds"]["A|SOL|book"]["own_lag_events"], 1)

    def test_an_unsynced_host_clock_degrades_every_feed_and_unknown_changes_nothing(self):
        clock = HostClockMonitor(100 * MS, 500.0)
        m = FeedHealthMonitor(policy(), clock)
        self.assertEqual(feed(m, "A", 1 * S), [])                                                            # UNKNOWN: not evaluable, nothing changes
        clock.record_sample(local_ts_ns=2 * S, reference_ts_ns=2 * S - 100 * MS, observed_ts_ns=2 * S, source="t")
        self.assertEqual(clock.state, "SYNCED")                                                              # exactly 100 ms: within the limit
        self.assertEqual(feed(m, "A", 2 * S), [])
        clock.record_sample(local_ts_ns=3 * S, reference_ts_ns=3 * S - 100 * MS - 1, observed_ts_ns=3 * S, source="t")
        self.assertEqual(clock.state, "UNSYNCED")
        self.assertEqual(moves(feed(m, "A", 3 * S)), [("A", "book", "HEALTHY", "DEGRADED", "host_clock_unsynced")])

    def test_rollup_counts_rate_gap_and_one_burst_per_episode(self):
        m = FeedHealthMonitor(policy(burst_window_ns=100 * MS, burst_count=5))
        t = 1 * S
        for k in range(10):                                          # one message per second
            feed(m, "A", t + k * S, delay=(k + 1) * MS)
        for k in range(8):                                           # then 8 messages inside 8 ms: one burst
            feed(m, "A", t + 9 * S + (k + 1) * MS)
        r = m.report()["feeds"]["A|SOL|book"]
        self.assertEqual((r["events"], r["bursts"], r["max_gap_ms"]), (18, 1, 1000.0))
        self.assertEqual(r["delay_ms"]["max"], 10.0)
        self.assertAlmostEqual(r["rate_per_s"], 18 / 9.008, places=2)

    def test_the_policy_refuses_inverted_or_non_positive_limits(self):
        with self.assertRaises(ValueError):
            policy(delay_degraded_ns=3000 * MS)
        with self.assertRaises(ValueError):
            policy(own_lag_ns=0)


def event(venue, exchange_ns, receive_ns, seq=1):
    return MarketEvent.from_dict({"event_type": "BOOK_SNAPSHOT", "venue": venue, "instrument_id": "SOL", "exchange_ts_ns": exchange_ns, "receive_ts_ns": receive_ns, "sequence": seq,
                                  "bids": [["99", "1"]], "asks": [["101", "1"]]})


class TimeIntegrityFixes(unittest.TestCase):
    def test_a_corrected_negative_event_beyond_the_delay_limit_is_feed_delayed(self):
        """Audit A1 1.6 f: the delay check sat in the elif after the negative-latency branch, so an event whose raw latency was negative was never tested for FEED_DELAYED."""
        m = TimeIntegrityMonitor(TimePolicy(max_feed_delay_ns=2000 * MS), ("A",))
        m.clock_offset_estimate_ns = -3000 * MS                      # the host clock is 3 s behind the venue
        a = m.observe(event("A", 10 * S, 10 * S - 500 * MS), now_ns=10 * S - 500 * MS)                       # raw latency -0.5 s, corrected +2.5 s: beyond the 2 s limit
        self.assertIn("NEGATIVE_RECEIVE_LATENCY_CORRECTED", a.flags)
        self.assertIn("FEED_DELAYED", a.flags)
        self.assertTrue(a.blocking)
        b = m.observe(event("A", 11 * S, 11 * S - 1500 * MS, 2), now_ns=11 * S - 1500 * MS)                  # corrected +1.5 s: inside the limit
        self.assertNotIn("FEED_DELAYED", b.flags)

    def test_delay_exactly_at_the_limit_is_not_delayed_and_one_nanosecond_more_is(self):
        m = TimeIntegrityMonitor(TimePolicy(max_feed_delay_ns=2000 * MS), ("A",))
        self.assertNotIn("FEED_DELAYED", m.observe(event("A", 10 * S, 12 * S), now_ns=12 * S).flags)
        self.assertIn("FEED_DELAYED", m.observe(event("A", 20 * S, 22 * S + 1, 2), now_ns=22 * S + 1).flags)

    def test_tolerance_zero_blocks_the_smallest_negative_latency_and_a_tolerance_admits_it(self):
        strict = TimeIntegrityMonitor(TimePolicy(), ("A",))
        a = strict.observe(event("A", 10 * S, 10 * S - 1), now_ns=10 * S)
        self.assertEqual(a.flags, ("NEGATIVE_RECEIVE_LATENCY",))
        self.assertTrue(a.blocking)
        tolerant = TimeIntegrityMonitor(TimePolicy(negative_latency_tolerance_ns=5 * MS), ("A",))
        self.assertEqual(tolerant.observe(event("A", 10 * S, 10 * S - 5 * MS), now_ns=10 * S).flags, ("NEGATIVE_RECEIVE_LATENCY_CORRECTED",))       # exactly the tolerance
        self.assertIn("NEGATIVE_RECEIVE_LATENCY", tolerant.observe(event("A", 20 * S, 20 * S - 5 * MS - 1, 2), now_ns=20 * S).flags)

    def test_the_host_clock_offset_limit_is_strict(self):
        clock = HostClockMonitor(100 * MS, 500.0)
        self.assertEqual(clock.record_sample(local_ts_ns=S, reference_ts_ns=S - 100 * MS, observed_ts_ns=S, source="t"), "SYNCED")
        self.assertEqual(clock.record_sample(local_ts_ns=2 * S, reference_ts_ns=2 * S + 100 * MS + 1, observed_ts_ns=2 * S, source="t"), "UNSYNCED")       # ahead or behind: the absolute offset

    def test_an_independent_reference_feeds_the_host_clock_and_ends_the_circular_self_feed(self):
        """Audit A1 1.6: the monitor's only sample source was its own low-quantile estimate. A server-time measurement is the reference; the estimate stays as a cross-check."""
        m = TimeIntegrityMonitor(TimePolicy(correct_host_offset=True, host_offset_min_samples=5), ("A",))
        for k in range(300):                                         # raw latency -20 ms on every event: the estimate settles at -20 ms and (old rule) feeds the host clock
            m.observe(event("A", (10 + k) * S, (10 + k) * S - 20 * MS, k + 1), now_ns=(10 + k) * S)
        self.assertEqual(m.host_clock.source, "EXCHANGE_TIMESTAMP_LOW_QUANTILE")
        before = m.host_clock.samples
        state = m.record_clock_reference(ClockMeasurement(offset_ns=150 * MS, uncertainty_ns=3 * MS, measured_ts_ns=400 * S, source="server_time:A"))
        self.assertEqual(state, "UNSYNCED")                                                                  # 150 ms ahead: something the one-sided estimate could never see
        self.assertEqual((m.host_clock.source, m.host_clock.samples), ("server_time:A", before + 1))
        for k in range(300, 600):
            m.observe(event("A", (10 + k) * S, (10 + k) * S - 20 * MS, k + 1), now_ns=(10 + k) * S)
        self.assertEqual((m.host_clock.source, m.host_clock.samples), ("server_time:A", before + 1))         # the estimate no longer feeds it
        check = m.report()["host_offset_correction"]["reference_cross_check"]
        self.assertEqual((check["reference_offset_ns"], check["low_quantile_estimate_ns"], check["gap_ns"]), (150 * MS, -20 * MS, 170 * MS))

    def test_a_run_without_a_reference_has_no_cross_check_key(self):
        m = TimeIntegrityMonitor(TimePolicy(), ("A",))
        m.observe(event("A", 10 * S, 10 * S + MS), now_ns=10 * S + MS)
        self.assertNotIn("reference_cross_check", m.report()["host_offset_correction"])


class ProfileBlock(unittest.TestCase):
    def test_v1_11_is_v1_10_plus_the_feed_health_block_with_every_action_off(self):
        from new_backtest.profiles import load_profile

        def flat(d, p=""):
            out = {}
            for k, v in d.items():
                if k.startswith("_"):
                    continue
                out.update(flat(v, p + k + ".") if isinstance(v, dict) else {p + k: v})
            return out
        a, b = flat(load_profile("perp_perp_maker_taker_v1_10")), flat(load_profile("perp_perp_maker_taker_v1_11"))
        changed = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
        self.assertEqual(changed, {"name", "description", "feed_health.delay_degraded_ms", "feed_health.delay_unsafe_ms", "feed_health.silence_degraded_ms", "feed_health.silence_unsafe_ms",
                                   "feed_health.own_lag_ms", "feed_health.recover_s", "feed_health.recorder_health_block", "feed_health.actions.block_new_quotes_on", "feed_health.actions.withdraw_quotes_on"})
        cfg = feed_health_from_profile(load_profile("perp_perp_maker_taker_v1_11"))
        self.assertEqual(cfg["actions"], {"block_new_quotes_on": "none", "withdraw_quotes_on": "none"})      # PENDING the owner: nothing acts
        self.assertIsNone(cfg["feed_delayed_ms"])                                                            # FEED_DELAYED stays off (1000 vs 2000 ms is not decided)
        self.assertIsNone(feed_health_from_profile(load_profile("perp_perp_maker_taker_v1_10")))
        self.assertIsNone(feed_health_from_profile(load_profile("perp_perp_maker_taker_v1_9")))

    def test_the_block_is_validated(self):
        good = {"delay_degraded_ms": 500, "delay_unsafe_ms": 2000, "silence_degraded_ms": 500, "silence_unsafe_ms": 5000, "own_lag_ms": 100, "recover_s": 2}
        self.assertEqual(feed_health_from_profile({"feed_health": dict(good)})["actions"]["withdraw_quotes_on"], "none")
        for bad in ({**good, "surprise": 1}, {k: v for k, v in good.items() if k != "recover_s"}, {**good, "actions": {"withdraw_quotes_on": "ALWAYS"}}, {**good, "actions": {"cancel_all": "UNSAFE"}}):
            with self.assertRaises(ValueError):
                feed_health_from_profile({"feed_health": bad})

    def test_feed_delayed_ms_reaches_the_time_policy_only_when_the_profile_sets_it(self):
        from new_backtest.session_replay import feed_health_config_table
        table = feed_health_config_table(["perp_perp_maker_taker_v1_2", "perp_perp_maker_taker_v1_9", "perp_perp_maker_taker_v1_10", "perp_perp_maker_taker_v1_11"])
        for name in ("perp_perp_maker_taker_v1_9", "perp_perp_maker_taker_v1_10", "perp_perp_maker_taker_v1_11"):
            self.assertIsNone(table[name]["time_policy"]["max_feed_delay_ms (FEED_DELAYED, blocking)"], name)
            self.assertEqual(table[name]["book_gate_max_book_age_ms"], 500.0)
        self.assertEqual(table["perp_perp_maker_taker_v1_9"]["feed_health_block"], "absent (no feed-health state model; nothing is built)")
        self.assertEqual(table["perp_perp_maker_taker_v1_11"]["feed_health_block"]["silence_unsafe_ms"], 5000.0)
        self.assertIn("runtime backtester", table["perp_perp_maker_taker_v1_2"]["note"])
        self.assertTrue(table["perp_perp_maker_taker_v1_11"]["session_limits_feed_arrival_stamp"])


try:
    from test_session_replay import SLICES, TRADE, snap, timed
    from new_backtest.profiles import apply_profile, load_profile
    from new_backtest.session_replay import SessionReplay
    HAVE_SLICES = SLICES.is_dir()
except Exception:                                    # pragma: no cover
    HAVE_SLICES = False


def hedge_feed_stops(stop_t=3.0, end_t=12.0, back_t=None):
    """Both books every 100 ms; a Bybit bid rests (the edge is there from the start); the hedge venue (Binance) sends nothing from `stop_t` (until `back_t` when given) while Bybit keeps updating."""
    ev = [snap(1.0, "bybit", 100.00, 100.30), snap(1.0, "binance", 100.30, 100.31)]
    t = 1.1
    while t < end_t:
        if t < stop_t or (back_t is not None and t >= back_t):
            ev.append(snap(t, "binance", 100.30, 100.31))
        ev.append(snap(t + 0.01, "bybit", 100.00, 100.30))
        t = round(t + 0.1, 6)
    return sorted(ev, key=lambda e: (e.exchange_ts, e.venue))


@unittest.skipUnless(HAVE_SLICES, "March slices not available (the profile needs a data root)")
class SessionWiring(unittest.TestCase):
    def replay(self, stream_events, profile_name="perp_perp_maker_taker_v1_11", actions=None, **over):
        profile = copy.deepcopy(load_profile(profile_name))
        if actions:
            profile["feed_health"]["actions"].update(actions)
        cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_fh")
        cfg.arb.max_position_base = 1.0
        cfg.arb.participation_max = 0.0
        cfg.arb.quote_withdraw = False                               # validity is not the subject here (and never withdraws on an unmeasurable edge)
        for k, v in over.items():
            setattr(cfg.arb, k, v)
        return SessionReplay(cfg, profile, stream=stream_events)

    def health(self, rep):
        return [(e["venue"], e["stream"], e["previous"], e["state"], e["cause"]) for e in rep.session.ledger.events if e["event_type"] == "FEED_HEALTH_STATE"]

    def resting(self, rep):
        return [c for p in rep.session.oms.plans.values() for c in p.children if c.post_only and c.state.value in ("ACKNOWLEDGED", "PARTIAL")]

    def withdrawn(self, rep):
        return [e for e in rep.session.ledger.events if e["event_type"] == "QUOTE_WITHDRAWN" and str(e.get("reason", "")).startswith("feed_health")]

    def test_v1_10_builds_nothing(self):
        rep = self.replay(hedge_feed_stops(end_t=5.0), "perp_perp_maker_taker_v1_10", actions=None)
        rep.run()
        self.assertIsNone(rep.session.feed_health)
        self.assertNotIn("feed_health", rep.session.breaker_report())
        self.assertEqual(self.health(rep), [])
        self.assertNotIn("feed_health", rep.config_notes)

    def test_the_hedge_feed_goes_stale_with_a_quote_resting_observation_only_one_event_per_change_and_the_quote_stays(self):
        """Audit A1 1.4 gap 5, now on record: with the actions off (v1_11 as it is) a resting maker quote is NOT taken down when the hedge venue's feed goes silent. The state model records
        DEGRADED and then UNSAFE once each, with the alert context; nothing else changes."""
        rep = self.replay(hedge_feed_stops(stop_t=3.0, end_t=6.0))                 # ends before the quote's 15 s lifetime
        rep.run()
        self.assertEqual([m for m in self.health(rep) if m[0] == "BINANCE"], [("BINANCE", "book", "HEALTHY", "DEGRADED", "silence")])
        self.assertEqual(self.withdrawn(rep), [])
        self.assertTrue(self.resting(rep))                                           # the quote is still live with the hedge feed dead: the documented behaviour without an action
        (ev,) = [e for e in rep.session.ledger.events if e["event_type"] == "FEED_HEALTH_STATE" and e["venue"] == "BINANCE"]
        for key in ("confirmed_residual_base", "pending_hedge_qty", "execution_blocked_reason"):
            self.assertIn(key, ev)                                                   # a worsening is an alert and carries its context
        self.assertEqual(rep.session.feed_health_report()["actions"], {"block_new_quotes_on": "none", "withdraw_quotes_on": "none"})
        self.assertIn("feed_health", rep.config_notes)

    def test_unsafe_after_five_seconds_and_one_recovery_after_the_feed_returns(self):
        rep = self.replay(hedge_feed_stops(stop_t=3.0, end_t=14.0, back_t=9.0))
        rep.run()
        self.assertEqual([m for m in self.health(rep) if m[0] == "BINANCE"],
                         [("BINANCE", "book", "HEALTHY", "DEGRADED", "silence"), ("BINANCE", "book", "DEGRADED", "UNSAFE", "silence"), ("BINANCE", "book", "UNSAFE", "HEALTHY", "recovered")])
        r = rep.session.feed_health_report()
        self.assertEqual(r["feeds"]["BINANCE|SOL-USDT-PERP|book"]["episodes"], {"DEGRADED": 1, "UNSAFE": 1})
        self.assertEqual(r["counters"], {"to_DEGRADED": 1, "to_HEALTHY": 1, "to_UNSAFE": 1})

    def test_withdraw_on_degraded_takes_the_resting_quote_down_once_when_the_switch_is_on(self):
        rep = self.replay(hedge_feed_stops(stop_t=3.0, end_t=6.0), actions={"withdraw_quotes_on": "DEGRADED"})
        rep.run()
        self.assertEqual(len(self.withdrawn(rep)), 1)                                # one withdrawal, at the DEGRADED transition
        self.assertEqual(self.resting(rep), [])                                      # no quote is live after the transition
        self.assertEqual(rep.session.feed_health_counters["withdraw_actions"], 1)
        self.assertEqual(rep.session.feed_health_counters["quotes_withdrawn_by_feed_health"], 1)

    def test_withdraw_on_unsafe_waits_for_unsafe(self):
        rep = self.replay(hedge_feed_stops(stop_t=3.0, end_t=7.0), actions={"withdraw_quotes_on": "UNSAFE"})
        rep.run()
        self.assertEqual(self.withdrawn(rep), [])                                    # 4 s of silence: DEGRADED only
        rep = self.replay(hedge_feed_stops(stop_t=3.0, end_t=10.0), actions={"withdraw_quotes_on": "UNSAFE"})
        rep.run()
        self.assertEqual(len(self.withdrawn(rep)), 1)

    def test_block_new_quotes_holds_quoting_through_the_recovery_hold_when_the_switch_is_on(self):
        """While a book is stale the existing book gate already stops the strategy (so the switch adds nothing there). What it adds is everything the gate does not see: here the 2 s recovery hold
        after the feed returns (the gate resumes on the first fresh frame; the state model does not), and in live a delay, an own lag or an unsynced clock on a fresh book."""
        on = self.replay(hedge_feed_stops(stop_t=3.0, end_t=9.0, back_t=5.0), actions={"block_new_quotes_on": "DEGRADED"})
        on.run()
        blocked = on.session.feed_health_counters.get("signals_blocked_by_feed_health", 0)
        self.assertGreater(blocked, 0)
        self.assertLessEqual(blocked, 2 * 21)                                        # only during the hold: about 2 s of book events on two venues at 10 per second each
        off = self.replay(hedge_feed_stops(stop_t=3.0, end_t=9.0, back_t=5.0))
        off.run()
        self.assertNotIn("signals_blocked_by_feed_health", off.session.feed_health_counters)
        self.assertEqual(self.health(on), self.health(off))                          # the state model itself is the same with the action on or off

if __name__ == "__main__":
    unittest.main()
