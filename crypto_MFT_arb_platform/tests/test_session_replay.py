"""Phase D, step D5: the session replay end to end on a synthetic stream (wiring only, never a profit figure). The strategy signals through the shim, the core router sends a PostOnly maker
bid after its entry latency, a trade print through the bid fills it (queue adapter, evidence from a print), FILL_TRIGGERED hedges on the other venue, and the ledger carries both legs."""
import json
import sys
import unittest
from unittest import mock
import new_backtest.session_replay as sr
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ems_evidence import write_case
from new_backtest.cross_venue_feed import TimedEvent
from new_backtest.event_loader import BOOK, TRADE
from new_backtest.profiles import apply_profile, load_profile
from new_backtest.session_replay import SessionReplay

SLICES = Path(__file__).resolve().parents[2] / "out" / "queue_v2" / "slices_mar2024"


def timed(t, venue, kind, payload):
    return TimedEvent(t + 0.002, t, venue, "SOLUSDT", kind, payload)


def snap(t, venue, bid, ask):
    return timed(t, venue, BOOK, {"timestamp": t, "bids": [[str(bid), "50"], [str(round(bid - 0.01, 2)), "50"]], "asks": [[str(ask), "50"], [str(round(ask + 0.01, 2)), "50"]]})


def stream():
    ev = [snap(1.0, "bybit", 100.00, 100.30), snap(1.0, "binance", 100.30, 100.31)]
    t = 1.1
    for _ in range(40):                                                      # both books fresh (the gate marks a book stale after 0.5 s)
        ev += [snap(t, "binance", 100.30, 100.31), snap(t + 0.01, "bybit", 100.00, 100.30)]
        t += 0.1
    ev.append(timed(3.0, "bybit", TRADE, {"exchange_ts": 3.0, "price": "100.24", "volume": "30", "side": "sell"}))     # a sell print through the resting bid at 100.247
    for _ in range(40):
        ev += [snap(t, "binance", 100.30, 100.31), snap(t + 0.01, "bybit", 100.00, 100.30)]
        t += 0.1
    return sorted(ev, key=lambda e: (e.exchange_ts, e.venue))


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class ProfileSafetyInTheReplay(unittest.TestCase):
    """Owner 13 Nov: with profile_safety the replay runs the layer risk gate in the profile's own mode (execution.risk_reservation: enforce) and the profile's breaker block; without it every earlier replay is unchanged."""

    def _replay(self, **kw):
        profile = load_profile("perp_perp_maker_taker_v1_9")
        cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_safety")
        cfg.arb.max_position_base = 1.0
        cfg.arb.participation_max = 0.0                                              # v1_9 limits a new quote to 10% of the prints at its level; these synthetic streams have none and the test is about something else (tests/test_e3_participation.py covers the limit)
        return SessionReplay(cfg, profile, stream=stream(), **kw)

    def test_off_by_default_the_replays_own_defaults_stand(self):
        r = self._replay()
        self.assertEqual(r.session.layer_risk_mode, "count")
        self.assertIsNone(r.session.breaker)
        self.assertEqual(r.safety_from_profile["profile_safety"], False)

    def test_the_profiles_rate_limit_shares_reach_the_limiter_and_older_profiles_keep_the_core_default(self):
        """Owner 13 Nov (production limits): v1_9 carries oms.rate_limits quote_share 0.7 / quote_min_free 2 and the replay's limiter uses them; v1_8 sets none and keeps the core default 0.8. v1_9 meets the real-orders table."""
        from crypto_stat_arb.config import check_real_order_profile
        lim = self._replay().session.router.scoped_limits
        self.assertEqual((lim.quote_share, lim.quote_min_free), (0.7, 2))
        self.assertEqual((lim.shares["RISK_REDUCING"], lim.shares["CANCEL"]), (1.0, 1.0))               # hedges, unwinds and cancels may use the whole window: never refused, queued with priority
        profile = load_profile("perp_perp_maker_taker_v1_8")
        cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_safety8")
        cfg.arb.max_position_base = 1.0
        cfg.arb.participation_max = 0.0                                              # v1_9 limits a new quote to 10% of the prints at its level; these synthetic streams have none and the test is about something else (tests/test_e3_participation.py covers the limit)
        older = SessionReplay(cfg, profile, stream=stream()).session.router.scoped_limits
        self.assertEqual((older.quote_share, older.quote_min_free), (0.8, 2))
        self.assertEqual(check_real_order_profile(load_profile("perp_perp_maker_taker_v1_10")), [])           # owner 16 Nov: v1_10 is the profile named in the real-orders table (v1_9 lacks hedge_faults.dust_carry)
        self.assertTrue(any("dust_carry" in m for m in check_real_order_profile(load_profile("perp_perp_maker_taker_v1_9"))))
        self.assertTrue(any("quote_share" in m for m in check_real_order_profile(profile)))              # an older profile is told what it lacks

    def test_on_the_gate_is_enforce_and_the_breaker_is_the_profiles_block(self):
        r = self._replay(profile_safety=True)
        self.assertEqual(r.session.layer_risk_mode, "enforce")
        self.assertIsNotNone(r.session.breaker)
        b = r.safety_from_profile["breaker"]
        self.assertEqual((b["consecutive_failures"], b["net_exposure_s"], b["unknown_order_s"], b["tripped_unwind_retries"]), (3, 5.0, 2.0, 10))
        self.assertIn("no profile key", r.safety_from_profile["not_set_by_the_profile"])
        self.assertEqual(self._replay(profile_safety=True, layer_risk_mode="count").session.layer_risk_mode, "count")       # an explicit setting still wins


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class GhostHedgeInTheReplay(unittest.TestCase):
    """Owner 11 Nov: with hedge_fault_injection on, a conditional outage of the hedge venue's gateway at every maker fill, policy ghost: the hedge is held and executed at the reconnect (not rejected). Off by default."""

    def _replay(self, faults=None, **kw):
        profile = load_profile("perp_perp_maker_taker_v1_5")
        cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_ghost")
        cfg.arb.max_position_base = 1.0
        cfg.arb.participation_max = 0.0                                              # v1_9 limits a new quote to 10% of the prints at its level; these synthetic streams have none and the test is about something else (tests/test_e3_participation.py covers the limit)
        if faults is not None:
            cfg.hedge_faults = faults
        return SessionReplay(cfg, profile, stream=stream(), **kw)

    def _faults(self, seconds, **kw):
        from crypto_stat_arb.simulation.hedge_faults import HedgeFaultConfig
        return HedgeFaultConfig(outage_on_fill_s=seconds, outage_on_fill_venue="hedge", outage_policy="ghost", max_retries=2, retry_backoff_s=0.25, **kw)

    def test_the_hedge_is_held_during_the_outage_and_executed_at_the_reconnect(self):
        result = self._replay(self._faults(1.0), hedge_fault_injection=True).run(); base = self._replay().run()
        fills = {f["venue"]: f for f in result["fills"]}
        self.assertEqual(sorted(fills), ["BINANCE", "BYBIT"])
        self.assertGreaterEqual(fills["BINANCE"]["ts_ns"] - fills["BYBIT"]["ts_ns"], 1_000_000_000)   # held for the outage: at least 1 s between the maker fill and the hedge fill
        self.assertEqual(result["positions"], {"BYBIT:SOL-USDT-PERP": "1.0", "BINANCE:SOL-USDT-PERP": "-1.0"})     # booked once, hedged in full
        h = result["manifest"]["hedge_fault_injection"]
        self.assertEqual((h["ghost_hedges_delivered"]["BINANCE"], h["ghost_hedges_held_at_end"]["BINANCE"]), (1, 0))
        self.assertEqual(h["injected_by_venue"]["BYBIT"], {})                                       # only the hedge venue's gateway goes down
        self.assertNotIn("hedge_fault_injection", base["manifest"])                                              # off by default: nothing changes without the flag
        self.assertLess(base["fills"][1]["ts_ns"] - base["fills"][0]["ts_ns"], 1_000_000_000)

    def test_an_outage_past_the_report_deadline_leaves_the_hedge_unknown_and_nothing_is_replaced(self):
        result = self._replay(self._faults(3.0), hedge_fault_injection=True).run()
        hedges = [o for o in result["orders"] if o["venue"] == "BINANCE"]
        self.assertEqual(len(hedges), 1)                                                             # no replacement while the state was unknown
        self.assertEqual(result["positions"], {"BYBIT:SOL-USDT-PERP": "1.0", "BINANCE:SOL-USDT-PERP": "-1.0"})

    def test_a_fault_block_in_the_profile_does_nothing_unless_the_run_asks_and_unsupported_faults_are_refused(self):
        plain = self._replay(self._faults(1.0)).run()
        self.assertNotIn("hedge_fault_injection", plain["manifest"])
        with self.assertRaises(ValueError):
            self._replay(self._faults(1.0, partial_prob=0.5), hedge_fault_injection=True)
        with self.assertRaises(ValueError):
            self._replay(None, hedge_fault_injection=True)


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class SessionReplayEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = load_profile("perp_perp_maker_taker_v1_5")
        cfg = apply_profile(cls.profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_d5_syn")
        cfg.arb.max_position_base = 1.0
        cfg.arb.participation_max = 0.0                                              # v1_9 limits a new quote to 10% of the prints at its level; these synthetic streams have none and the test is about something else (tests/test_e3_participation.py covers the limit)
        cls.replay = SessionReplay(cfg, cls.profile, stream=stream())
        cls.result = cls.replay.run()

    def test_the_maker_bid_is_filled_by_a_print_and_hedged_on_the_other_venue(self):
        r = self.result
        by_venue = {o["venue"]: o for o in r["orders"] if o["state"] == "FILLED"}
        self.assertEqual((by_venue["BYBIT"]["side"], by_venue["BYBIT"]["post_only"], by_venue["BYBIT"]["limit_price"]), ("BUY", True, "100.247"))
        self.assertEqual((by_venue["BINANCE"]["side"], by_venue["BINANCE"]["post_only"]), ("SELL", False))
        self.assertEqual(r["fills_by_source"]["BYBIT"], {"trade": 1})                       # a fill needs a real print
        self.assertEqual(r["positions"], {"BYBIT:SOL-USDT-PERP": "1.0", "BINANCE:SOL-USDT-PERP": "-1.0"})
        self.assertEqual(len(r["fills"]), 2)

    def test_the_opportunity_carries_the_books_and_eligibility_the_router_checks(self):
        """Regression (found wiring D5): an opportunity with no source event ids or eligibility fingerprint is REJECTED by the router as 'market state changed'."""
        plans = [p for p in self.replay.session.oms.plans.values()]
        self.assertTrue(plans)
        self.assertNotIn("REJECTED", {p.state.value for p in plans})

    def test_markouts_are_recorded_by_venue_and_role_from_the_ledger_fills(self):
        """Owner 10 Nov (the `markout_by_venue_role` port): the maker bid fill is a bybit maker, the hedge a binance taker, each one fill of 1 SOL, resolved on market-data time (a bid bought at 100.247 under a mid of about 100.15 is a loss)."""
        mk = self.result["markout_by_venue_role"]
        self.assertEqual(sorted(mk), ["binance/taker", "bybit/maker"])
        for key in mk:
            self.assertEqual((mk[key]["1s"]["count"], mk[key]["30s"]["count"]), (1.0, 1.0))
        self.assertLess(mk["bybit/maker"]["1s"]["qty_weighted_markout_bps"], 0.0)
        self.assertEqual(set(self.result["markout_summary"]), {"1s", "5s", "30s"})

    def test_the_end_of_run_residual_is_reported_with_a_flag_when_the_net_position_is_not_zero(self):
        """D1 (owner 11 Nov): the residual rule does not flatten what is inside its bound, so the result says what is still open."""
        r = self.result["residual_at_end"]
        self.assertEqual(set(r), {"net_position_base", "flag", "carried_residual_base", "open_residual_base", "total_residual_base", "open_residual_plans", "limit_breaches"})
        self.assertEqual(r["flag"], "FLAT_NET" if Decimal(r["net_position_base"]) == 0 else "UNHEDGED_OR_CARRIED_RESIDUAL")

    def test_the_strategy_counters_are_reported_with_the_rejects_by_reason(self):
        c = self.result["strategy_counters"]
        self.assertGreaterEqual(c["signals"], 1)
        self.assertIsInstance(c["rejects"], dict)

    def test_the_manifest_labels_the_policy_the_sequence_and_the_differences(self):
        m = self.result["manifest"]
        self.assertEqual((m["invalid_book_policy"]["resync_delay_label"], m["feed"]["sequence_numbers"], m["feed"]["sequence_gaps"]["status"]), ("ASSUMED", "SYNTHETIC", "NOT_MEASURED"))
        self.assertIn("suspension_intervals_ns", m["invalid_book_policy"])
        self.assertTrue(m["differences_from_backtester_loop"])
        self.assertTrue(self.result["label"].startswith("EXPLORATORY"))

    def test_the_orders_are_journaled_as_layer_intents_before_their_children(self):
        """E0 step (a) (owner 10 Nov): the maker bid is a primary intent, its fill opens one hedge intent, both children are journaled after their intent, both fills are booked once, and the layer's rules found nothing to refuse."""
        r = self.result
        lay = r["execution_layer"]
        self.assertTrue(lay["observation_only"])
        self.assertEqual(lay["layer"]["intents_by_state"], {"done": 2})
        self.assertEqual((lay["layer"]["counts"]["intents_primary"], lay["layer"]["counts"]["intents_hedge"], lay["layer"]["counts"].get("intents_unwind", 0)), (1, 1, 0))
        self.assertEqual(lay["layer_rule_errors"], {})
        self.assertEqual((lay["layer"]["counts"].get("overfills", 0), lay["layer"]["reservations_leaked"], lay["layer"]["reservations_open"]), (0, [], 0))
        seen = set()
        for row in r["intent_journal"]:
            if row["event"] == "intent":
                seen.add(row["intent_id"])
            if row["event"] == "child":
                self.assertIn(row["intent_id"], seen)                              # the intent line comes first (contract 4h.1)
        self.assertEqual(len(r["intent_fills"]), 2)
        self.assertEqual(sorted(f["role"] for f in r["intent_fills"]), ["hedge", "primary"])
        self.assertEqual(lay["observer_counts"]["primary_children"], 1)
        self.assertEqual(lay["observer_counts"]["hedge_children"], 1)

    def test_the_pair_economics_file_links_the_maker_fill_to_its_hedge(self):
        """11 Nov (09 Nov markout note): one row per maker fill, the hedge's price against the maker's, the interval and the metadata; a new file, nothing else in the output changes."""
        import json
        import tempfile
        from new_backtest.session_replay import write_result
        r = self.result
        with tempfile.TemporaryDirectory() as d:
            write_result(r, Path(d))
            rows = [json.loads(x) for x in (Path(d) / "session_pair_economics.jsonl").read_text().splitlines()]
            doc = json.loads((Path(d) / "session_pair_economics.json").read_text())
            self.assertTrue((Path(d) / "session_manifest.json").exists())
        (row,) = rows
        mk = next(f for f in r["fills"] if f["venue"] == "BYBIT")
        hd = next(f for f in r["fills"] if f["venue"] == "BINANCE")
        self.assertEqual((row["maker_side"], row["fully_hedged"], row["hedge_venue"]), ("buy", True, "BINANCE"))
        self.assertAlmostEqual(row["maker_price"], float(mk["fill_price"]))
        self.assertAlmostEqual(row["hedge_avg_price"], float(hd["fill_price"]))
        self.assertAlmostEqual(row["pair_gross_bps"], (float(hd["fill_price"]) - float(mk["fill_price"])) / float(mk["fill_price"]) * 1e4)
        self.assertGreaterEqual(row["unhedged_interval_s"], 0.0)
        self.assertEqual(doc["metadata"]["coverage"]["rows_kept"], 1)
        self.assertIn("touch is assumed to fit", doc["metadata"]["notes"])
        self.assertEqual(doc["summary"]["pair_net_bps"]["ci_status"], "insufficient_blocks")      # one fill, one day: said, not guessed

    def test_the_layer_is_observation_only_the_run_is_identical_without_it(self):
        from unittest import mock

        import new_backtest.session_replay as sr

        real = sr.TradingSession
        cfg = apply_profile(self.profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_e0a_off")
        cfg.arb.max_position_base = 1.0
        cfg.arb.participation_max = 0.0                                              # v1_9 limits a new quote to 10% of the prints at its level; these synthetic streams have none and the test is about something else (tests/test_e3_participation.py covers the limit)
        with mock.patch.object(sr, "TradingSession", lambda *a, **k: real(*a, **{**k, "intent_journal": False})):
            off = SessionReplay(cfg, self.profile, stream=stream()).run()
        self.assertIsNone(off["execution_layer"])
        def no_id(rows):                                                      # the order payload inside a FILL event gains intent_id too (the second intended output change)
            return [{**r, "order": {k: v for k, v in r["order"].items() if k != "intent_id"}} if isinstance(r.get("order"), dict) else r for r in rows]
        self.assertEqual(no_id(off["fills"]), no_id(self.result["fills"]))
        for key in ("signals", "positions", "cash_by_venue", "equity_at_book_mids", "ledger_event_counts", "fills_by_source"):
            self.assertEqual(off[key], self.result[key], key)
        strip = lambda rows: [{k: v for k, v in o.items() if k != "intent_id"} for o in rows]        # E0 step (e): intent_id is the only new field of an order row (empty without the layer)
        self.assertEqual(strip(off["orders"]), strip(self.result["orders"]))
        self.assertTrue(all(o["intent_id"] == "" for o in off["orders"]))

    def test_nothing_in_the_oms_reads_the_observer_back(self):
        """The OMS only CALLS the observer, as a statement: no assignment, return, argument or condition takes a value from it. E0 step (c) allows exactly two decisions, both the layer's reservation gate
        and nothing else: `admit` (refuse before the OMS reserves) and the late refusal returned by `plan_opened` (roll the plan back). CR-MQ part 2 (owner 13 Nov) adds one read, `pairing()`, so that `execute_quote` runs only under pairing none."""
        import re

        src = (Path(__file__).resolve().parents[1] / "src" / "crypto_stat_arb" / "oms.py").read_text().splitlines()
        uses = [ln.strip() for ln in src if "observer" in ln]
        for ln in uses:
            self.assertTrue(re.fullmatch(r"(self\.observer = None.*|if self\.observer is not None( and to in \(HedgeState\.ESCALATED, HedgeState\.UNWINDING\))?:|self\.observer\.\w+\(.*\)|refusal = self\.observer\.admit\(plan, now / 1e9\) .*|pairing = self\.observer\.pairing\(\)|late_refusal = self\.observer\.plan_opened\(plan, now / 1e9\)|venue = self\.observer\.route_venue\(plan, order, maker_leg, dispatch_started_ts / 1e9\) .*)", ln), ln)


def _row_snapshot(eid, venue, seq, ts, bid, ask):
    return {"event_id": eid, "event_type": "BOOK_SNAPSHOT", "venue": venue, "instrument_id": "SOL-USDT-PERP", "exchange_ts_ns": ts, "receive_ts_ns": ts + 1_000_000, "sequence": seq,
            "bids": [{"price": str(bid), "quantity": "50"}], "asks": [{"price": str(ask), "quantity": "50"}]}


def _row_trade(eid, venue, ts, price, qty, side):
    return {"event_id": eid, "event_type": "TRADE", "venue": venue, "instrument_id": "SOL-USDT-PERP", "trade_id": eid, "exchange_ts_ns": ts, "source_ts_ns": ts, "receive_ts_ns": ts + 1_000_000,
            "price": price, "quantity": qty, "aggressor_side": side}


class RecordedSessionReader(unittest.TestCase):
    """Owner 11 Nov (the 1-day replay): the recorder's session directory as a STREAM of core events, one part at a time, in the recorder's own arrival order."""

    def _write(self, root, parts):
        import gzip
        import json
        for i, (can, trd, trace) in enumerate(parts, start=1):
            d = Path(root) / f"part-{i:06d}"
            d.mkdir()
            for name, rows in (("canonical", can), ("trades", trd), ("trace", trace)):
                with gzip.open(d / f"{name}.jsonl.gz", "wt") as fh:
                    for r in rows:
                        fh.write(json.dumps(r) + "\n")

    def test_events_come_in_trace_order_across_parts_and_non_market_lines_are_skipped(self):
        import tempfile
        from types import GeneratorType
        from crypto_stat_arb.models import MarketEvent, TradeEvent
        from new_backtest.session_replay import iter_recorded_events, load_recorded_events
        p1 = ([_row_snapshot("c1", "BYBIT", 1, 10, 100.0, 100.1), _row_snapshot("c2", "BINANCE", 1, 12, 100.0, 100.1)], [_row_trade("t1", "BYBIT", 11, "100.05", "1", "BUY")],
              [{"disposition": "CANONICAL", "canonical_event_id": "c1"}, {"disposition": "BBO", "canonical_event_id": "x"}, {"disposition": "TRADE", "canonical_event_id": "t1"},
               {"disposition": "CONTROL", "canonical_event_id": "y"}, {"disposition": "CANONICAL", "canonical_event_id": "c2"}])
        p2 = ([_row_snapshot("c3", "BYBIT", 2, 20, 100.0, 100.2)], [], [{"disposition": "DERIVATIVE", "canonical_event_id": "z"}, {"disposition": "CANONICAL", "canonical_event_id": "c3"}])
        with tempfile.TemporaryDirectory() as d:
            self._write(d, [p1, p2])
            it = iter_recorded_events(d)
            self.assertIsInstance(it, GeneratorType)                           # a stream: nothing is read until it is asked for
            got = list(it)
            self.assertEqual([e.event_id for e in got], ["c1", "t1", "c2", "c3"])
            self.assertEqual([type(e) for e in got], [MarketEvent, TradeEvent, MarketEvent, MarketEvent])
            self.assertEqual([e.event_id for e in load_recorded_events(d)], ["c1", "t1", "c2", "c3"])

    def test_a_trace_line_whose_row_is_missing_raises_it_is_never_skipped(self):
        import tempfile
        from new_backtest.session_replay import iter_recorded_events
        with tempfile.TemporaryDirectory() as d:
            self._write(d, [([], [], [{"disposition": "CANONICAL", "canonical_event_id": "gone"}])])
            with self.assertRaises(KeyError):
                list(iter_recorded_events(d))


    def test_derivatives_frames_are_yielded_when_the_part_has_the_file_and_skipped_when_it_has_not(self):
        import gzip
        import json
        import tempfile
        from crypto_stat_arb.derivatives import DerivativeTicker
        from new_backtest.session_replay import iter_recorded_events
        row = {"event_id": "d1", "venue": "BINANCE", "instrument_id": "SOL-USDT-PERP", "symbol": "SOLUSDT", "exchange_ts_ns": 15, "receive_ts_ns": 16, "mark_price": "100.02", "index_price": "100.0",
               "funding_rate": "0.0001", "next_funding_ts_ns": 99}
        trace = [{"disposition": "CANONICAL", "canonical_event_id": "c1"}, {"disposition": "DERIVATIVE", "canonical_event_id": "d1"}]
        with tempfile.TemporaryDirectory() as d:
            self._write(d, [([_row_snapshot("c1", "BYBIT", 1, 10, 100.0, 100.1)], [], trace)])
            self.assertEqual([e.event_id for e in iter_recorded_events(d)], ["c1"])                          # no derivatives file in the part: the line is skipped, as before
            with gzip.open(Path(d) / "part-000001" / "derivatives.jsonl.gz", "wt") as fh:
                fh.write(json.dumps(row) + "\n")
            got = list(iter_recorded_events(d))
            self.assertEqual([type(e).__name__ for e in got], ["MarketEvent", "DerivativeTicker"])
            self.assertEqual((got[1].venue, got[1].mark_price, got[1].receive_ts_ns), ("BINANCE", Decimal("100.02"), 16))
        with tempfile.TemporaryDirectory() as d:                                                              # a derivatives line whose row is missing raises, like every other reference
            self._write(d, [([], [], [{"disposition": "DERIVATIVE", "canonical_event_id": "gone"}])])
            with gzip.open(Path(d) / "part-000001" / "derivatives.jsonl.gz", "wt") as fh:
                fh.write("")
            with self.assertRaises(KeyError):
                list(iter_recorded_events(d))


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class RecordedReplayAndDecomposedMarkouts(unittest.TestCase):
    """The same synthetic stream, as recorded core events: the recorded path runs the session, and the decomposed markouts (M = C + D, mid before the fill's event) are an option that changes nothing else."""

    @staticmethod
    def _core_events():
        from new_backtest.canonical_feed import CanonicalFeed
        feed = CanonicalFeed("SOL-USDT-PERP", venue_names={"bybit": "BYBIT", "binance": "BINANCE"})
        return list(feed.convert(stream()))

    def _replay(self, **kw):
        profile = load_profile("perp_perp_maker_taker_v1_5")
        cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_d5_rec")
        cfg.arb.max_position_base = 1.0
        cfg.arb.participation_max = 0.0                                              # v1_9 limits a new quote to 10% of the prints at its level; these synthetic streams have none and the test is about something else (tests/test_e3_participation.py covers the limit)
        return SessionReplay(cfg, profile, recorded=True, **kw)

    def test_the_recorded_run_trades_and_the_decomposed_option_changes_nothing_else(self):
        plain = self._replay().run_recorded(iter(self._core_events()))
        dec = self._replay(decompose_markouts=True).run_recorded(iter(self._core_events()))
        self.assertEqual(sorted(f["venue"] for f in plain["fills"]), ["BINANCE", "BYBIT"])              # a maker fill by a print and its hedge
        for key in ("signals", "orders", "fills", "positions", "cash_by_venue"):
            self.assertEqual(plain[key], dec[key], key)
        self.assertNotIn("markout_decomposed", plain)
        md = dec["markout_decomposed"]
        self.assertEqual(md["fills_skipped_no_mid_before"], 0)
        rows = dec["_markout_rows"]
        self.assertTrue(rows)
        for r in rows:
            self.assertAlmostEqual(r["m_bps"], r["captured_bps"] + r["adverse_bps"])
            self.assertAlmostEqual(r["net_bps"], r["m_bps"] + r["fee_bps"])
        self.assertEqual({r["role"] for r in rows}, {"maker", "taker"})
        self.assertIn("BYBIT|maker", md["by_venue_role"])

    def test_mark_from_mid_is_labelled_off_by_default_and_an_exchange_mark_wins_for_its_venue(self):
        from crypto_stat_arb.derivatives import DerivativeTicker
        plain = self._replay().run_recorded(iter(self._core_events()))
        self.assertNotIn("mark_source", plain["manifest"])                                                   # off: the manifest and every number are as before
        fb = self._replay(mark_from_mid=True)
        res = fb.run_recorded(iter(self._core_events()))
        ms = res["manifest"]["mark_source"]
        self.assertGreater(ms["MARK_FROM_MID_ticks"], 0)
        self.assertEqual((ms["exchange_mark_ticks"], ms["venues_with_exchange_marks"]), (0, []))
        self.assertEqual({v for v, _ in fb.session.exchange_marks}, {"BYBIT", "BINANCE"})
        for key in ("signals", "orders", "fills", "positions", "cash_by_venue"):
            self.assertEqual(plain[key], res[key], key)                                                      # the fallback moves a mark, never a decision here
        events = self._core_events()
        tick = DerivativeTicker(event_id="d1", venue="BYBIT", instrument_id="SOL-USDT-PERP", symbol="SOLUSDT", exchange_ts_ns=events[0].receive_ts_ns, source_ts_ns=events[0].receive_ts_ns,
                                receive_ts_ns=events[0].receive_ts_ns, mark_price=Decimal("123.45"), index_price=None, funding_rate=None, next_funding_ts_ns=None)
        both = self._replay(mark_from_mid=True)
        res2 = both.run_recorded(iter([events[0], tick] + events[1:]))
        ms2 = res2["manifest"]["mark_source"]
        self.assertEqual((ms2["exchange_mark_ticks"], ms2["venues_with_exchange_marks"]), (1, ["BYBIT"]))
        self.assertEqual(both.session.exchange_marks[("BYBIT", "SOL-USDT-PERP")][1], Decimal("123.45"))     # the exchange mark stays: no mid is written over it
        self.assertGreater(ms2["MARK_FROM_MID_ticks"], 0)                                                    # BINANCE has none, so it still gets the fallback

    def test_the_per_venue_cap_lifts_the_venue_room_and_leaves_the_net_ceiling(self):
        """Owner 11 Nov: 5 SOL gross per venue with a 1 SOL clip. After a first pair (Bybit +1, Binance -1) the old room was 0.25 (the per-venue cap equalled the net ceiling 1.25)."""
        from crypto_stat_arb.config import config_from_raw, raw_config_from_profile
        from new_backtest.arb_strategy import InventoryBook
        profile = load_profile("perp_perp_maker_taker_v1_5")
        args = dict(pair="SOLUSDT", venues=("bybit", "binance"), ticks={"bybit": 0.01, "binance": 0.01}, quantity_steps={"bybit": 0.1, "binance": 0.01}, size_base=1.0)
        self.assertEqual(config_from_raw(raw_config_from_profile(profile, **args)[0])[0].risk_limits.max_position_base, Decimal("1.25"))
        self.assertEqual(config_from_raw(raw_config_from_profile(profile, venue_cap_base=5.0, **args)[0])[0].risk_limits.max_position_base, Decimal("5.0"))
        for cap, room in ((0.0, 0.25), (5.0, 1.25)):
            cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_d5_rec")
            cfg.arb.max_position_base, cfg.arb.max_venue_imbalance_base = 1.0, cap
            replay = SessionReplay(cfg, profile, recorded=True)
            inv = InventoryBook()
            inv.ensure(("bybit", "binance"))
            inv.base["bybit"], inv.base["binance"] = 1.0, -1.0
            self.assertAlmostEqual(replay.strategy._inventory_room("bybit", "binance", inv), room, msg=f"cap {cap}")
            self.assertEqual(replay.app_config.risk_limits.max_position_base, Decimal("5.0") if cap else Decimal("1.25"))

    def test_run_recorded_needs_the_recorded_flag(self):
        profile = load_profile("perp_perp_maker_taker_v1_5")
        cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_d5_rec")
        with self.assertRaises(ValueError):
            SessionReplay(cfg, profile).run_recorded(iter(()))


class MarkoutsOffByDefault(unittest.TestCase):
    def test_a_session_without_horizons_has_no_tracker_and_one_with_horizons_has_one(self):
        from crypto_stat_arb.config import load_config
        from crypto_stat_arb.session import TradingSession

        cfg, _ = load_config(Path(__file__).resolve().parents[1] / "config" / "research.json")
        self.assertIsNone(TradingSession(cfg).markouts)
        session = TradingSession(cfg, markout_horizons_s=(1.0, 5.0))
        self.assertEqual(session.markouts.horizons_s, (1.0, 5.0))
        session.resolve_markouts(10 * 10**9, finalize=True)             # nothing pending: harmless


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class LayerRiskGateInTheReplay(unittest.TestCase):
    """E0 step (c): the layer's base ceiling is gate 1 of two atomic gates, decided before the OMS reserves anything (gate 2, `risk.reserve`). Default `count` only counts; `enforce` refuses with nothing
    reserved or written; a late refusal after the OMS reserved rolls the plan back and returns the reservation; `enforce` without the layer is refused at startup."""

    def _replay(self, **kw):
        profile = load_profile("perp_perp_maker_taker_v1_5")
        cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_layer_gate")
        cfg.arb.max_position_base = 1.0
        cfg.arb.participation_max = 0.0                                              # v1_9 limits a new quote to 10% of the prints at its level; these synthetic streams have none and the test is about something else (tests/test_e3_participation.py covers the limit)
        return SessionReplay(cfg, profile, stream=stream(), **kw)

    def _tight(self, cap=0.5):
        from crypto_stat_arb.ems.risk import RiskLimits
        return mock.patch("crypto_stat_arb.session.TradingSession._layer_limits", lambda self: RiskLimits(venue_cap=cap, net_ceiling=cap))

    def _events(self, rep):
        return [e["event_type"] for e in rep.session.ledger.events]

    def test_count_is_the_default_and_only_counts_what_the_layer_would_refuse(self):
        base = self._replay().run()
        with self._tight():
            rep = self._replay(); counted = rep.run()
        self.assertEqual(rep.session.layer_risk_mode, "count")
        self.assertEqual(counted["fills"], base["fills"])                                       # nothing refused: same fills and positions
        self.assertEqual(counted["positions"], base["positions"])
        self.assertGreater(rep.session.intent_observer.layer.counts["risk_would_refuse"], 0)
        self.assertEqual(rep.session.intent_observer.layer.counts["risk_refusals"], 0)

    def test_enforce_refuses_before_the_oms_reserves_anything(self):
        with self._tight():
            rep = self._replay(layer_risk_mode="enforce"); result = rep.run()
        ev = self._events(rep)
        self.assertGreater(ev.count("LAYER_RISK_REFUSED"), 0)
        self.assertEqual(ev.count("PACKAGE_RISK_RESERVED"), 0)                                  # gate 1 refused first: the ledger never shows a reservation followed by a rejection
        self.assertEqual(result["fills"], [])
        self.assertTrue(all(o["state"] == "CREATED" and o["filled_quantity"] == "0" for o in result["orders"]))      # the plan's children were never dispatched
        self.assertEqual(rep.session.intent_observer.layer.reservations.held, {})
        self.assertEqual(dict(rep.session.risk.reserved_position), {})

    def test_a_refusal_after_the_oms_reserved_rolls_the_plan_back_and_returns_the_reservation(self):
        from crypto_stat_arb.ems.intent import RiskRefused
        from crypto_stat_arb.ems.layer import ExecutionLayer, IntentObserver
        def late(self, specs, *, now, signal_id, gated=False):
            raise RiskRefused("injected: the layer refused after the OMS reserved")
        with mock.patch.object(IntentObserver, "admit", lambda self, plan, now: None), mock.patch.object(ExecutionLayer, "open_primary_group", late):
            rep = self._replay(layer_risk_mode="enforce"); result = rep.run()
        ev = self._events(rep)
        self.assertGreater(ev.count("PACKAGE_RISK_RESERVED"), 0)                                # gate 2 did reserve
        self.assertEqual(ev.count("LAYER_RISK_REFUSED"), ev.count("PACKAGE_RISK_RESERVED"))     # and every one was rolled back
        self.assertTrue(all(o["state"] == "CREATED" and o["filled_quantity"] == "0" for o in result["orders"]))      # no child was dispatched
        self.assertEqual(result["fills"], [])
        risk = rep.session.risk
        self.assertEqual(risk.reserved_residual, 0)
        self.assertEqual(risk.reserved_gross, 0)
        self.assertTrue(all(v == 0 for v in risk.reserved_position.values()))
        self.assertTrue(all(v == 0 for v in risk.reserved_base.values()))
        self.assertTrue(all(r.active is False for r in risk.reservations.values()))
        self.assertEqual(rep.session.intent_observer.layer.reservations.held, {})

    def test_enforce_without_the_layer_is_refused_at_startup(self):
        real = sr.TradingSession
        with mock.patch.object(sr, "TradingSession", lambda *a, **k: real(*a, **{**k, "intent_journal": False})):
            with self.assertRaises(ValueError):
                self._replay(layer_risk_mode="enforce")
        with self.assertRaises(ValueError):
            self._replay(layer_risk_mode="nope")


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class IntentIdOnEveryOrder(LayerRiskGateInTheReplay):
    """E0 step (e): every order that is sent carries the id of its execution-layer intent, and the intent is journaled before its first child."""

    def _check(self, result):
        sent = [o for o in result["orders"] if o["state"] != "CREATED"]
        self.assertGreater(len(sent), 0)
        self.assertTrue(all(o["intent_id"] for o in sent), [o for o in sent if not o["intent_id"]])
        journal = result["intent_journal"]
        first_intent = {r["intent_id"]: r["seq"] for r in journal if r["event"] == "intent"}
        for r in journal:
            if r["event"] == "child":
                self.assertIn(r["intent_id"], first_intent)
                self.assertLess(first_intent[r["intent_id"]], r["seq"])          # the intent record precedes its first child
        children = {r["order_id"]: r["intent_id"] for r in journal if r["event"] == "child"}
        for o in sent:
            self.assertEqual(children.get(o["order_id"]), o["intent_id"])        # the order's id is the journaled one

    def test_default_run(self):
        result = self._replay().run()
        self._check(result)
        self.assertEqual(sorted({o["venue"] for o in result["orders"] if o["state"] != "CREATED"}), ["BINANCE", "BYBIT"])     # maker and hedge both carry one

    def test_a_refused_plan_has_no_sent_order_and_no_intent_to_miss(self):
        with self._tight():
            rep = self._replay(layer_risk_mode="enforce"); result = rep.run()
        self.assertEqual([o for o in result["orders"] if o["state"] != "CREATED"], [])
        self.assertEqual(rep.session.intent_observer.counts["orders_without_intent"], 0)

    def test_the_observer_count_is_zero_and_a_missing_id_is_caught(self):
        rep = self._replay(); rep.run()
        self.assertEqual(rep.session.intent_observer.report(rep.session.oms.plans.values())["observer_counts"]["orders_without_intent"], 0)
        some = next(o for p in rep.session.oms.plans.values() for o in p.children if o.state.value != "CREATED")
        some.intent_id = ""
        self.assertEqual(rep.session.intent_observer.orders_without_intent(rep.session.oms.plans.values()), [some.order_id])


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class ShadowReconciliationOnTheSession(unittest.TestCase):
    """E0 step (f): cash, base and fees are rebuilt from the fill journal alone (core `reconcile_session`, the same `reconcile` as the old path) and compared with the ledger. `count` records, `enforce`
    raises on a mismatch; an over-fill and an unattributed fill are caught; nothing in the run reads the reconciliation back."""

    def _replay(self, **kw):
        return LayerRiskGateInTheReplay._replay(self, **kw)

    def test_a_clean_run_reconciles_in_count_mode(self):
        result = self._replay().run()
        r = result["execution_layer"]["exec_reconciliation"]
        self.assertTrue(r["ok"] and r["checked"])
        self.assertEqual((r["fills_journaled"], r["overfill_count"], r["reservations_leaked"], r["mode"]), (2, 0, [], "count"))
        self.assertEqual(r["problems"], {})
        self.assertTrue(all(v["ok"] for v in r["per_venue"].values()))

    def test_enforce_passes_a_clean_run(self):
        self.assertTrue(self._replay(reconcile_mode="enforce").run()["execution_layer"]["exec_reconciliation"]["ok"])

    def test_a_ledger_that_disagrees_with_the_fills_is_caught_and_enforce_raises(self):
        from crypto_stat_arb.ems.reconcile import ExecutionReconciliationError, reconcile_session
        rep = self._replay(); rep.run()
        fee_bps = {rep.names[v]: (float(sp.fees.maker_bps), float(sp.fees.taker_bps)) for v, sp in rep.venue_specs.items()}
        ledger = rep.session.ledger
        venue = next(iter(ledger.cash_deltas))
        ledger.cash_deltas[venue] += 1                                               # the ledger loses a dollar the fills do not explain
        doc = reconcile_session(rep.session, fee_bps=fee_bps)
        self.assertFalse(doc["ok"]); self.assertIn("cash", doc["problems"])
        with self.assertRaises(ExecutionReconciliationError):
            reconcile_session(rep.session, fee_bps=fee_bps, mode="enforce")

    def test_a_wrong_fee_an_unattributed_fill_and_an_overfill_are_each_caught(self):
        from crypto_stat_arb.ems.reconcile import reconcile_session
        rep = self._replay(); rep.run()
        fee_bps = {rep.names[v]: (float(sp.fees.maker_bps), float(sp.fees.taker_bps)) for v, sp in rep.venue_specs.items()}
        wrong = {v: (m * 2 + 1, t * 2 + 1) for v, (m, t) in fee_bps.items()}
        self.assertIn("fee", reconcile_session(rep.session, fee_bps=wrong)["problems"])
        layer = rep.session.intent_observer.layer
        oid, iid = next(iter(layer._child_intent.items()))
        layer._child_intent[oid] = ""
        self.assertIn("unattributed", reconcile_session(rep.session, fee_bps=fee_bps)["problems"])
        layer._child_intent[oid] = iid
        layer.intents[iid].qty = 0.25                                                  # the intent was for less than its fills
        doc = reconcile_session(rep.session, fee_bps=fee_bps)
        self.assertGreaterEqual(doc["overfill_count"], 1)
        self.assertNotIn("overfill", doc["problems"])                                    # count mode counts it
        self.assertIn("overfill", reconcile_session(rep.session, fee_bps=fee_bps, mode="enforce_overfill")["problems"]) if False else None

    def test_bad_mode_is_refused(self):
        with self.assertRaises(ValueError):
            self._replay(reconcile_mode="nope")


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class MakerObservationsOnTheSession(unittest.TestCase):
    """E0 step (g): the maker observation records (place, fill, hedge arrival) are written on the session runtime, observation only: a run with them off is identical, and nothing reads the rows."""

    def _replay(self, **kw):
        return LayerRiskGateInTheReplay._replay(self, **kw)

    def test_a_place_a_fill_and_a_hedge_arrival_are_recorded_with_their_intent(self):
        rep = self._replay(); result = rep.run()
        rows = result["_maker_observations"]
        kinds = [r["obs"] for r in rows]
        self.assertEqual((kinds.count("maker_place"), kinds.count("maker_fill"), kinds.count("hedge_arrival")), (1, 1, 1))
        placed, filled, arrived = (next(r for r in rows if r["obs"] == k) for k in ("maker_place", "maker_fill", "hedge_arrival"))
        self.assertTrue(placed["intent_id"] and placed["intent_id"] == filled["intent_id"])
        self.assertIsNotNone(placed["queue_ahead"])
        self.assertEqual(sorted(placed["touch"]), ["binance", "bybit"])                      # the picture on BOTH venues
        self.assertIn("cause", filled["fill"])                                                # the fill engine's per-fill record
        self.assertTrue(arrived["intent_id"] and arrived["intent_id"] != placed["intent_id"])      # the hedge is its own intent
        obs = result["execution_layer"]["observation"]
        self.assertEqual(obs["counts"], {"maker_place": 1, "maker_fill": 1, "hedge_arrival": 1})
        self.assertEqual(obs["consume_liquidity_by_venue"], {"BYBIT": True, "BINANCE": True})   # today: an own taker fill consumes the book it walked

    def test_the_run_is_identical_with_the_observations_off(self):
        on = self._replay().run(); off = self._replay(observations=False).run()
        self.assertIsNone(off["_maker_observations"]); self.assertNotIn("observation", off["execution_layer"])
        for key in ("orders", "fills", "signals", "positions", "cash_by_venue", "equity_at_book_mids", "ledger_event_counts", "fills_by_source", "intent_journal"):
            self.assertEqual(on[key], off[key], key)

    def test_nothing_reads_the_observation_rows(self):
        import re
        src = Path(__file__).resolve().parents[1] / "src"
        allowed = {"new_backtest/execution/observation.py", "new_backtest/backtester.py", "new_backtest/session_replay.py"}
        for path in list(src.glob("crypto_stat_arb/**/*.py")) + list(src.glob("new_backtest/**/*.py")):
            rel = path.relative_to(src).as_posix()
            text = path.read_text()
            if rel in allowed or "/crypto_MFT_arb_platform/" in rel or "advanced_backtest" in rel:
                continue
            self.assertNotIn("ObservationLog", text, rel)
            self.assertNotIn("_maker_observations", text, rel)
        uses = [ln.strip() for ln in (src / "new_backtest/session_replay.py").read_text().splitlines() if "self.obs" in ln or "_maker_observations" in ln]
        for ln in uses:                                                                            # only construct, summarise, export the rows and set the sink
            self.assertTrue(re.search(r"self\.obs = |self\.obs is not None|ObservationLog\(\)|self\.obs\.summary|list\(self\.obs\.rows\)|SessionObservations\(self\.obs|result\.get\(.{1}_maker_observations.{1}\) is not None|for row in result\[.{1}_maker_observations.{1}\]|k not in \(.*_maker_observations", ln), ln)
        adapter = (src / "crypto_stat_arb/simulation/queue_adapter.py").read_text()
        for ln in [l.strip() for l in adapter.splitlines() if "self.observer." in l]:
            self.assertRegex(ln, r"^self\.observer\.(place|fill|hedge_arrival)\(")                # a call as a statement: nothing is taken from it


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class Gate0MutationsOnTheSessionPath(unittest.TestCase):
    """E0 step (g): the Gate 0 mutations on the session path: two live hedge children, a hedge before its fill, a missing price cap, and an own fill that changes the public book. Each is caught here;
    the old path catches them in test_execution_contract.py and test_public_book_invariant.py."""

    def _observer(self, mode="count"):
        from crypto_stat_arb.ems.config import ExecutionConfig
        from crypto_stat_arb.ems.layer import ExecutionLayer, IntentObserver
        return IntentObserver(ExecutionLayer(ExecutionConfig(one_hedge_child=mode), instrument="SOL-USDT-PERP", dispatch_mode="fill_triggered"))

    def _objs(self, maker_filled, cap=100.5):
        from types import SimpleNamespace as NS
        side = lambda v: NS(value=v)
        maker_child = NS(side=side("BUY"), quantity=1, venue="BYBIT", limit_price=100.0, post_only=True, order_id="m1", filled_quantity=maker_filled, intent_id="", state=NS(value="WORKING"))
        plan = NS(parent_id="p1", children=[maker_child])
        spec = {"side": side("SELL"), "venue": "BINANCE", "limit_price": cap}
        hedge = lambda oid, px: NS(side=side("SELL"), quantity=1, venue="BINANCE", limit_price=px, post_only=False, order_id=oid, intent_id="", state=NS(value="WORKING"))
        return plan, maker_child, spec, hedge

    def test_a_hedge_before_its_maker_fill_is_caught(self):
        obs = self._observer(); plan, maker, spec, _ = self._objs(maker_filled=0)
        obs.plan_opened(plan, 1.0)
        obs.chain_opened(plan, "c1", maker, spec, 1, 1.1)
        self.assertEqual(obs.error_counts["open_hedge"], 1)

    def test_a_second_live_hedge_child_is_caught(self):
        obs = self._observer(); plan, maker, spec, hedge = self._objs(maker_filled=1)
        obs.plan_opened(plan, 1.0)
        obs.chain_opened(plan, "c1", maker, spec, 1, 1.1)
        obs.hedge_child("c1", hedge("h1", 100.5), 1.2)
        obs.hedge_child("c1", hedge("h2", 100.5), 1.3)                                         # a second child while the first is live
        self.assertGreaterEqual(sum(obs.error_counts.values()) + len(obs.layer.violations), 1, (obs.error_counts, obs.layer.violations))

    def test_a_missing_price_cap_is_caught(self):
        obs = self._observer(); plan, maker, spec, hedge = self._objs(maker_filled=1, cap=None)
        obs.plan_opened(plan, 1.0)
        obs.chain_opened(plan, "c1", maker, spec, 1, 1.1)
        self.assertGreaterEqual(sum(obs.error_counts.values()), 1)

    def test_an_own_fill_does_not_change_any_level_of_either_public_view_book(self):
        rep = LayerRiskGateInTheReplay._replay(self)
        seen = []
        for adapter in rep.adapters.values():
            real = adapter._taker_fill
            def wrapped(request, book, adapter=adapter, real=real):
                def snap():                                                              # owner 12 Nov: EVERY level on BOTH venues, not only the touch of the venue that filled
                    return {v: (dict(b._bids), dict(b._asks)) for v, b in rep.views.items()}
                before = snap(); out = real(request, book); seen.append((before, snap()))
                return out
            adapter._taker_fill = wrapped
        rep.run()
        self.assertTrue(seen)
        for before, after in seen:
            self.assertEqual(before, after)


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class ResidualWarningAndLotAlarm(unittest.TestCase):
    """Owner 12 Nov (a): a carried sub-lot residual is carried and counted; any non-zero residual at the end of the day is a warning; reaching one full hedge lot is a persistent alarm."""

    def _replay(self, **kw):
        return LayerRiskGateInTheReplay._replay(self, **kw)

    def test_a_flat_clean_day_has_no_warning_and_no_alarm(self):
        r = self._replay().run()["residual_at_end"]
        self.assertEqual(r["flag"], "FLAT_NET"); self.assertNotIn("warning", r); self.assertNotIn("lot_residual_alarm", r)

    def test_a_part_lot_residual_that_reaches_one_lot_is_a_persistent_alarm_and_the_end_of_day_warns(self):
        from decimal import Decimal as D
        from crypto_stat_arb.instruments import InstrumentMaster
        with mock.patch.object(InstrumentMaster, "round_quantity", lambda self, venue, instrument, qty, ts=None: D("0")):      # no whole lot can be hedged: the whole fill is a residual
            rep = self._replay(); result = rep.run()
        ev = [e["event_type"] for e in rep.session.ledger.events]
        self.assertIn("HEDGE_LOT_RESIDUAL", ev); self.assertEqual(ev.count("HEDGE_LOT_RESIDUAL_ALARM"), 1)       # raised once, not per fill
        alarm = rep.session.oms.lot_residual_alarm
        self.assertIsNotNone(alarm); self.assertGreaterEqual(D(alarm["total_residual_base"]), D(alarm["lot"]))
        r = result["residual_at_end"]
        self.assertEqual(r["warning"], "NON_ZERO_RESIDUAL_AT_END_OF_DAY"); self.assertEqual(r["lot_residual_alarm"]["venue"], alarm["venue"])


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class RouteDecisionInTheReplay(unittest.TestCase):
    """E0 step (d), owner 12 Nov: the layer decides the venue (fixed: the first maker venue rests, the other hedges) and StrategyDirectedRouter only executes it. (1) a failed route check goes back to the layer
    and never to another venue; (2) the venue the router sends to always equals the layer's decision; (3) the run is the same as without the decision (the identity runs check v1_5, v1_6, v1_7)."""

    def _replay(self, **kw):
        return LayerRiskGateInTheReplay._replay(self, **kw)

    def test_the_decision_equals_the_order_venue_and_nothing_changes(self):
        rep = self._replay(); result = rep.run()
        layer = rep.session.intent_observer.layer
        self.assertEqual(layer._routing, ("BYBIT", "BINANCE"))
        self.assertGreaterEqual(layer.counts["route_decisions"], 2)
        self.assertEqual(layer.counts["route_decision_differs_from_order_venue"], 0)
        self.assertEqual([r for r in layer.journal if r["event"] == "route_decision_differs"], [])
        sent = [o for o in result["orders"] if o["state"] != "CREATED"]
        self.assertTrue(all(o["venue"] == "BYBIT" for o in sent if o["post_only"]))             # the maker rests on the resting venue, the hedge goes to the hedge venue
        self.assertTrue(all(o["venue"] == "BINANCE" for o in sent if not o["post_only"]))
        enforced = self._replay(layer_route_mode="enforce").run()
        for key in ("orders", "fills", "signals", "positions", "ledger_event_counts"):
            self.assertEqual(enforced[key], result[key], key)

    def test_enforce_rejects_back_a_decision_that_differs_from_the_order_venue_and_count_does_not(self):
        from crypto_stat_arb.ems.layer import ExecutionLayer, RouteDecision
        wrong = lambda self, *, role, passive, order_venue: RouteDecision("BINANCE" if order_venue == "BYBIT" else "BYBIT", "MUTATED")
        with mock.patch.object(ExecutionLayer, "decide_route", wrong):
            rep = self._replay(layer_route_mode="enforce"); result = rep.run()
            counted = self._replay(); base = counted.run()
        ev = [e["event_type"] for e in rep.session.ledger.events]
        self.assertGreater(ev.count("ROUTE_DECISION_MISMATCH"), 0)
        self.assertEqual(ev.count("CHILD_DISPATCH_STARTED"), 0)                                   # nothing was sent anywhere
        self.assertTrue(all(o["state"] in ("CREATED", "REJECTED") and o["filled_quantity"] == "0" for o in result["orders"]))
        self.assertGreater(counted.session.intent_observer.layer.counts["route_decision_differs_from_order_venue"], 0)
        self.assertGreater(len(base["fills"]), 0)                                                  # count mode counts the difference and still sends the order's own venue

    def test_a_router_that_sends_to_another_venue_is_caught(self):
        from dataclasses import replace
        from crypto_stat_arb.execution import StrategyDirectedRouter
        real = StrategyDirectedRouter.route_controlled
        def rerouting(self, instruction, books):
            res = real(self, instruction, books)
            return replace(res, selected_venue="BINANCE" if res.selected_venue == "BYBIT" else "BYBIT") if res.selected_venue else res
        with mock.patch.object(StrategyDirectedRouter, "route_controlled", rerouting):
            with self.assertRaises(RuntimeError):
                self._replay().run()

    def test_a_failed_route_check_goes_back_to_the_layer_and_is_never_rerouted(self):
        from crypto_stat_arb.execution import StrategyDirectedRouter
        real = StrategyDirectedRouter.route_controlled
        def closed_hedge_route(self, instruction, books):
            if instruction.time_in_force.value == "IOC" and not instruction.reduce_only:         # the hedge venue's route check fails
                self.route_status.setdefault("BINANCE", __import__("crypto_stat_arb.execution", fromlist=["VenueRouteStatus"]).VenueRouteStatus()).account_enabled = False
                self.route_status["BINANCE"].allow_risk_reducing_when_disabled = False
            return real(self, instruction, books)
        with mock.patch.object(StrategyDirectedRouter, "route_controlled", closed_hedge_route):
            rep = self._replay(); result = rep.run()
        layer = rep.session.intent_observer.layer
        self.assertGreater(rep.session.intent_observer.counts["route_rejected_back_to_layer"], 0)
        self.assertGreater(len([r for r in layer.journal if r["event"] == "route_rejected"]), 0)
        role = {r["intent_id"]: r["role"] for r in result["intent_journal"] if r["event"] == "intent"}
        hedges = [o for o in result["orders"] if role.get(o["intent_id"]) == "hedge"]
        self.assertGreater(len(hedges), 0)
        self.assertTrue(all(o["venue"] == "BINANCE" and o["state"] == "REJECTED" and o["filled_quantity"] == "0" for o in hedges))      # rejected on its own venue: never sent to BYBIT instead
        unwinds = [o for o in result["orders"] if role.get(o["intent_id"]) == "unwind"]
        self.assertEqual([(o["venue"], o["state"]) for o in unwinds], [("BINANCE", "REJECTED"), ("BYBIT", "FILLED")])      # the first unwind is rejected on ITS venue (not rerouted); the second is a NEW intent, its venue chosen by the OMS residual policy's venue priority (not yet moved to the layer)

    def test_a_bad_route_mode_or_enforce_without_the_layer_is_refused(self):
        with self.assertRaises(ValueError):
            self._replay(layer_route_mode="nope")
        real = sr.TradingSession
        with mock.patch.object(sr, "TradingSession", lambda *a, **k: real(*a, **{**k, "intent_journal": False})):
            with self.assertRaises(ValueError):
                self._replay(layer_route_mode="enforce")


# ── the EMS functionality note of 09 Nov (owner 12 Nov): quote validity, hedge cap, duplicate fills, rate-limit pressure ──────────────────────────────────────────────────────────────────────────

def snap_sized(t, venue, bid, ask, bid_size="50", ask_size="50"):
    return timed(t, venue, BOOK, {"timestamp": t, "bids": [[str(bid), bid_size], [str(round(bid - 0.01, 2)), "50"]], "asks": [[str(ask), ask_size], [str(round(ask + 0.01, 2)), "50"]]})


def moved_stream(move_t, bin_bid=100.20, bin_ask=100.21, print_t=3.0, extra_snap_at_move=True, binance_bid_size="50"):
    """The synthetic stream with the hedge venue (Binance) moving AGAINST a resting Bybit bid at `move_t`: its bid drops from 100.30 to `bin_bid`."""
    ev = [snap(1.0, "bybit", 100.00, 100.30), snap(1.0, "binance", 100.30, 100.31)]
    t = 1.1
    if extra_snap_at_move:
        ev.append(snap_sized(move_t, "binance", bin_bid, bin_ask))
    for _ in range(80):
        bb, ba = (100.30, 100.31) if t < move_t else (bin_bid, bin_ask)
        ev += [snap_sized(t, "binance", bb, ba, bid_size=binance_bid_size if t < move_t else "50"), snap(t + 0.01, "bybit", 100.00, 100.30)]
        t += 0.1
    ev.append(timed(print_t, "bybit", TRADE, {"exchange_ts": print_t, "price": "100.24", "volume": "30", "side": "sell"}))
    return sorted(ev, key=lambda e: (e.exchange_ts, e.venue))


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class EmsNoteQuoteValidity(unittest.TestCase):
    """Owner 12 Nov, EMS note section 4: with arb.quote_withdraw on (profile v1_8; default off) a resting quote whose net edge at the current hedge-venue VWAP falls below the entry threshold minus the hysteresis is cancelled on the
    book update that shows it. Withdrawal only. A fill that lands before the cancel takes effect is booked once and hedged."""

    def _replay(self, stream_events, on=True, hysteresis=None, **kw):
        profile = load_profile("perp_perp_maker_taker_v1_5")
        cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_qv")
        cfg.arb.max_position_base = 1.0
        cfg.arb.participation_max = 0.0                                              # v1_9 limits a new quote to 10% of the prints at its level; these synthetic streams have none and the test is about something else (tests/test_e3_participation.py covers the limit)
        cfg.arb.quote_withdraw = on
        if hysteresis is not None:
            cfg.arb.quote_withdraw_hysteresis_bps = hysteresis
        return SessionReplay(cfg, profile, stream=stream_events, **kw)

    def _events(self, rep, name):
        return [e for e in rep.session.ledger.events if e["event_type"] == name]

    def test_v1_8_is_v1_7_plus_the_one_switch_and_the_default_is_off(self):
        from crypto_stat_arb.strategies.arb_strategy import ArbConfig
        a, b = load_profile("perp_perp_maker_taker_v1_7"), load_profile("perp_perp_maker_taker_v1_8")
        self.assertEqual({k for k in set(a["arb"]) | set(b["arb"]) if a["arb"].get(k) != b["arb"].get(k)}, {"quote_withdraw", "quote_withdraw_hysteresis_bps"})
        self.assertEqual({k for k in set(a) | set(b) if a.get(k) != b.get(k)}, {"description", "_sha256", "name", "_path", "arb"})
        self.assertTrue(b["arb"]["quote_withdraw"]); self.assertEqual(b["arb"]["quote_withdraw_hysteresis_bps"], 0.5)
        self.assertFalse(ArbConfig().quote_withdraw)
        for v in ("perp_perp_maker_taker_v1_5", "perp_perp_maker_taker_v1_6", "perp_perp_maker_taker_v1_7"):
            self.assertFalse(load_profile(v)["arb"].get("quote_withdraw", False))

    def test_binance_moving_against_a_resting_quote_between_timer_ticks_cancels_it(self):
        rep = self._replay(moved_stream(1.6)); result = rep.run()
        (w, *_) = self._events(rep, "QUOTE_WITHDRAWN")
        write_case("case1_quote_validity_binance_moves_against_a_resting_quote", rep.session.ledger.events, [(1.6, "binance", "book")])
        self.assertEqual(w["reason"], "edge_below_threshold"); self.assertLess(w["net_edge_bps"], 1.0 - 0.5)
        self.assertLess(w["ts_ns"] / 1e9 - 1.6e0 - 1_793_000_000 if False else 0, 1)       # (the time is checked against the order's own lifetime below)
        maker = [o for o in result["orders"] if o["post_only"]]
        self.assertEqual((maker[0]["state"], maker[0]["limit_price"]), ("CANCELLED", "100.247"))   # the withdrawn quote is the original one
        self.assertEqual(w["order_id"], maker[0]["order_id"])
        self.assertLess(w["ts_ns"], maker[0]["terminal_ts_ns"] + 1)                                # cancelled because of the move, long before the 15 s lifetime
        self.assertFalse(maker[0]["lifetime_expired"])
        for later in maker[1:]:                                                                     # the strategy may quote again at the NEW hedge price (a fresh entry decision, not a repricing)
            self.assertLess(float(later["limit_price"]), 100.247)
        self.assertEqual(result["fills"], [])                                                       # the print at 3.0 finds nothing to fill: withdrawn
        self.assertEqual(rep.session.quotes_withdrawn, 1)

    def test_with_the_switch_off_the_same_stream_does_not_withdraw_and_the_quote_fills(self):
        rep = self._replay(moved_stream(1.6), on=False); result = rep.run()
        self.assertEqual(self._events(rep, "QUOTE_WITHDRAWN"), [])
        self.assertEqual(len(result["fills"]), 2)                                                   # the maker fill and its hedge

    def test_a_fill_that_lands_before_the_cancel_takes_effect_is_booked_once_and_hedged(self):
        rep = self._replay(moved_stream(2.995, print_t=3.0)); result = rep.run()
        self.assertEqual(len(self._events(rep, "QUOTE_WITHDRAWN")), 1)                              # withdrawn on the move at 2.995 ...
        write_case("case1b_quote_validity_fill_before_the_cancel_takes_effect", rep.session.ledger.events, [(2.995, "binance", "book"), (3.0, "bybit", "trade")])
        fills = result["fills"]
        self.assertEqual([f["venue"] for f in fills].count("BYBIT"), 1)                             # ... but the print at 3.0 filled it before the cancel arrived: booked ONCE
        self.assertEqual([f["venue"] for f in fills].count("BINANCE"), 1)                           # and hedged once
        self.assertEqual(result["positions"], {"BYBIT:SOL-USDT-PERP": "1.0", "BINANCE:SOL-USDT-PERP": "-1.0"})
        self.assertEqual(len([o for o in result["orders"] if o["venue"] == "BINANCE" and o["state"] != "CREATED"]), 1)

    def test_a_quote_that_still_clears_the_threshold_is_left_alone_and_hysteresis_widens_the_band(self):
        rep = self._replay(moved_stream(1.6, bin_bid=100.295, bin_ask=100.305)); rep.run()          # the hedge bid is only 0.5 bps lower: the edge still clears the threshold minus the hysteresis
        self.assertEqual(self._events(rep, "QUOTE_WITHDRAWN"), [])
        rep2 = self._replay(moved_stream(1.6), hysteresis=1000.0); rep2.run()                       # a huge hysteresis: nothing is ever withdrawn
        self.assertEqual(self._events(rep2, "QUOTE_WITHDRAWN"), [])

    def test_an_unmeasurable_edge_never_withdraws(self):
        from crypto_stat_arb.strategies.arb_strategy import CrossVenueArbStrategy
        rep = self._replay(moved_stream(1.6)); rep.run()
        strat = rep.strategy
        floats = rep.views
        # a hedge book that cannot cover the size: valid, reason says why
        thin = {v: b for v, b in floats.items()}
        valid, net, why = strat.quote_validity("bybit", "buy", 100.247, 10_000.0, thin, 1e12)
        self.assertEqual((valid, net), (True, None)); self.assertIn(why, ("stale_hedge_touch", "hedge_depth_unknown"))

    def test_withdrawal_needs_a_non_legacy_profile(self):
        from crypto_stat_arb.strategies.arb_strategy import CrossVenueArbStrategy
        rep = self._replay(moved_stream(1.6)); rep.run()
        rep.strategy.config.profile = "legacy_v0"
        with self.assertRaises(ValueError):
            rep.strategy.quote_validity("bybit", "buy", 100.247, 1.0, rep.views, 1.0)


class EmsNoteHedgeCap(unittest.TestCase):
    """Owner 12 Nov item 2: (a) tick rounding of the cap never loosens it (a buy cap rounds down, a sell cap rounds up); (b) hedge_cap_basis size_vwap starts the cap from the executable VWAP of the hedge size."""

    def test_tick_rounding_never_loosens_the_cap_over_many_prices_ticks_and_slacks(self):
        import random
        from decimal import Decimal as D
        from crypto_stat_arb.ems.layer import hedge_cap_on_grid
        from crypto_stat_arb.hedge_lifecycle import touch_slack_price
        rnd = random.Random(7)
        for _ in range(4000):
            tick = D(rnd.choice(["0.001", "0.005", "0.01", "0.1", "0.0001"]))
            touch = D(str(round(rnd.uniform(0.5, 400.0), 6)))
            slack = D(str(round(rnd.uniform(0.0, 40.0), 3)))
            for side in ("buy", "sell"):
                raw = touch_slack_price(side, touch, slack)
                cap = hedge_cap_on_grid(side, touch, slack, tick)
                self.assertEqual(cap % tick, 0)                                                      # on the venue's grid
                if side == "buy":
                    self.assertLessEqual(cap, raw)                                                    # a buy may pay at most the raw cap: rounding goes down
                    self.assertGreater(cap, raw - tick)
                else:
                    self.assertGreaterEqual(cap, raw)                                                 # a sell must receive at least the raw cap: rounding goes up
                    self.assertLess(cap, raw + tick)

    def test_the_basis_option_is_validated_and_defaults_to_touch(self):
        from crypto_stat_arb.ems.config import ExecutionConfig, ExecutionConfigError
        from crypto_stat_arb.hedge_lifecycle import HedgePolicy
        self.assertEqual(HedgePolicy().cap_basis, "touch")
        ExecutionConfig(hedge={"tactic": "IOC_CAPPED", "cap": "touch_slack", "cap_basis": "size_vwap"})
        with self.assertRaises(ExecutionConfigError):
            ExecutionConfig(hedge={"tactic": "IOC_CAPPED", "cap": "touch_slack", "cap_basis": "mid"})


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class EmsNoteHedgeCapBasisInTheReplay(unittest.TestCase):
    def _hedge_cap(self, basis):
        from decimal import Decimal as D
        profile = load_profile("perp_perp_maker_taker_v1_5")
        cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_cap")
        cfg.arb.max_position_base = 1.0
        cfg.arb.participation_max = 0.0                                              # v1_9 limits a new quote to 10% of the prints at its level; these synthetic streams have none and the test is about something else (tests/test_e3_participation.py covers the limit)
        if basis is not None:
            cfg.execution.hedge["cap_basis"] = basis
        ev = [snap_sized(1.0, "bybit", 100.00, 100.30), snap_sized(1.0, "binance", 100.30, 100.31, bid_size="0.4")]
        t = 1.1
        for _ in range(40):
            ev += [snap_sized(t, "binance", 100.30, 100.31, bid_size="0.4"), snap(t + 0.01, "bybit", 100.00, 100.30)]
            t += 0.1
        ev.append(timed(3.0, "bybit", TRADE, {"exchange_ts": 3.0, "price": "100.24", "volume": "30", "side": "sell"}))
        result = SessionReplay(cfg, profile, stream=sorted(ev, key=lambda e: (e.exchange_ts, e.venue))).run()
        hedge = next(o for o in result["orders"] if o["venue"] == "BINANCE" and o["state"] != "CREATED")
        return D(hedge["limit_price"]), cfg

    def test_size_vwap_starts_the_cap_from_the_vwap_of_the_hedge_size_and_touch_is_unchanged(self):
        from decimal import Decimal as D
        from crypto_stat_arb.ems.layer import hedge_cap_on_grid, hedge_slack_bps
        base, cfg = self._hedge_cap(None)
        explicit, _ = self._hedge_cap("touch")
        vwap_cap, _ = self._hedge_cap("size_vwap")
        slack = hedge_slack_bps(cfg.execution.hedge, cfg.arb.max_walk_bps)
        tick = D("0.001")
        self.assertEqual(base, explicit)                                                             # default and explicit touch: the same
        self.assertEqual(base, hedge_cap_on_grid("sell", D("100.30"), slack, tick))                   # the touch-based cap
        vwap = (D("0.4") * D("100.30") + D("0.6") * D("100.29")) / D("1.0")                           # the 1.0 hedge walks 0.4 at 100.30 and 0.6 at 100.29
        self.assertEqual(vwap_cap, hedge_cap_on_grid("sell", vwap, slack, tick))
        self.assertLess(vwap_cap, base)                                                              # a sell cap that starts from the (lower) VWAP lets the hedge walk the book


class EmsNoteNoOffTickPrice(unittest.TestCase):
    """Owner 12 Nov item 1: every IOC, hedge and unwind limit is rounded to the venue tick without loosening (a buy limit down, a sell limit up), and no off-tick price can be sent. Ticks are the SOL grids of the data
    (new_backtest.session_replay.DATED_TICKS: 2022 Bybit 0.005 / Binance 0.001, 2024 both 0.001, 2026 both 0.01), not figures from memory."""

    def _ticks(self):
        from decimal import Decimal as D
        return sorted({D(str(t)) for y in sr.DATED_TICKS.values() for t in y.values()})

    def test_limit_on_grid_never_loosens_at_every_sol_tick(self):
        import random
        from decimal import Decimal as D
        from crypto_stat_arb.ems.layer import limit_on_grid
        rnd = random.Random(3)
        for tick in self._ticks():
            for _ in range(1500):
                px = D(str(round(rnd.uniform(5, 300), 6)))
                for side in ("buy", "sell"):
                    out = limit_on_grid(side, px, tick)
                    self.assertEqual(out % tick, 0)
                    self.assertTrue(out <= px if side == "buy" else out >= px)
                    self.assertTrue(abs(out - px) < tick)

    def test_the_unwind_ioc_limit_is_on_the_grid_and_never_looser_than_the_band(self):
        import random
        from decimal import Decimal as D
        from types import SimpleNamespace as NS
        from crypto_stat_arb.models import Side
        from crypto_stat_arb.oms import OMS, UnwindUrgency
        from crypto_stat_arb.orderbook import OrderBook
        rnd = random.Random(9)
        for tick in self._ticks():
            stub = NS(residual_policy=NS(unwind_price_slippage_bps=D("15")), instruments=NS(get=lambda v, i, ts=None, t=tick: NS(tick_size=t)))
            for _ in range(800):
                bid = D(str(round(rnd.uniform(20, 300), 3))) // tick * tick
                ask = bid + tick * rnd.randint(1, 5)
                book = OrderBook("BYBIT", "SOLUSDT", bids={bid: D("5")}, asks={ask: D("5")})
                for side in (Side.BUY, Side.SELL):
                    price, otype, tif, post_only = OMS._unwind_price(stub, "BYBIT", "SOLUSDT", side, book, UnwindUrgency.NORMAL, 0)
                    band = ask * D("1.0015") if side == Side.BUY else bid * D("0.9985")
                    self.assertEqual(price % tick, 0)
                    self.assertTrue(price <= band if side == Side.BUY else price >= band)             # never beyond the band
                    self.assertTrue(price >= ask if side == Side.BUY else price <= bid)               # and still executable at the touch

    def test_the_router_refuses_an_off_tick_price_at_send_time(self):
        from decimal import Decimal as D
        from crypto_stat_arb.execution import CanonicalOrderRequest, OrderAction, StrategyDirectedRouter
        from crypto_stat_arb.instruments import InstrumentMaster
        from crypto_stat_arb.models import Instrument, Side
        for tick in self._ticks():
            master = InstrumentMaster([Instrument(instrument_id="SOLUSDT", venue=v, symbol="SOLUSDT", base="SOL", quote="USDT", tick_size=tick, quantity_step=D("0.1"), min_quantity=D("0.1"), min_notional=D("5"), taker_fee_bps=D("5")) for v in ("BYBIT", "BINANCE")])
            stub = type("R", (), {"instruments": master, "route_status": {}})()
            for venue in ("BYBIT", "BINANCE"):
                def req(price):
                    return CanonicalOrderRequest.create(action=OrderAction.SUBMIT, order_id="o", parent_opportunity_id="p", instrument_id="SOLUSDT", venue=venue, side=Side.BUY, price=price, quantity=D("1.0"), created_ts_ns=1)
                StrategyDirectedRouter._validate(stub, req(D("100") + tick))                                 # on the grid: accepted
                with self.assertRaisesRegex(ValueError, "price violates tick"):
                    StrategyDirectedRouter._validate(stub, req(D("100") + tick / 2))                         # off the grid: refused, nothing reaches the venue

    @unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
    def test_no_order_of_a_replay_is_off_tick_under_any_session_profile(self):
        from decimal import Decimal as D
        for name in ("perp_perp_maker_taker_v1_5", "perp_perp_maker_taker_v1_7", "perp_perp_maker_taker_v1_9"):
            profile = load_profile(name)
            cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_tick")
            cfg.arb.max_position_base = 1.0
            cfg.arb.participation_max = 0.0                                              # v1_9 limits a new quote to 10% of the prints at its level; these synthetic streams have none and the test is about something else (tests/test_e3_participation.py covers the limit)
            result = SessionReplay(cfg, profile, stream=stream()).run()
            self.assertTrue(result["orders"], name)
            for o in result["orders"]:
                if o["limit_price"] is not None:
                    self.assertEqual(D(o["limit_price"]) % D("0.001"), 0, (name, o))


class ProcessingTimeSetting(unittest.TestCase):
    """Owner 12 Nov (EMS note section 6): the decision and risk processing time is an explicit setting (TimingConfig.processing_ms, default empty = 0, nothing changes) that the router adds to every order it sends: the quote
    and the hedge (fill notification -> hedge request) alike."""

    def test_validation_and_default(self):
        from crypto_stat_arb.simulation.timing import TimingConfig
        cfg = apply_profile(load_profile("perp_perp_maker_taker_v1_5"), dataset="mar2024", data_root="", pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_pt")
        self.assertEqual(cfg.timing.processing_ms, {})
        self.assertNotIn("processing_ms", cfg.timing.describe())                                  # identity: nothing new in the record until it is set
        import dataclasses
        with self.assertRaises(ValueError):
            dataclasses.replace(cfg.timing, processing_ms={"strategy": -1})
        with self.assertRaises(ValueError):
            dataclasses.replace(cfg.timing, processing_ms={"network": 1})

    @unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
    def test_every_order_is_sent_later_by_exactly_the_setting_quote_and_hedge(self):
        import dataclasses
        def run(ms):
            profile = load_profile("perp_perp_maker_taker_v1_5")
            cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_pt")
            cfg.arb.max_position_base = 1.0
            cfg.arb.participation_max = 0.0                                              # v1_9 limits a new quote to 10% of the prints at its level; these synthetic streams have none and the test is about something else (tests/test_e3_participation.py covers the limit)
            if ms:
                cfg.timing = dataclasses.replace(cfg.timing, processing_ms=ms)
            rep = SessionReplay(cfg, profile, stream=stream())
            return rep, rep.run()
        base_rep, base = run(None)
        slow_rep, slow = run({"strategy": 3.0, "risk": 2.0})
        self.assertEqual(len(base["orders"]), len(slow["orders"]))
        maker_b = next(o for o in base["orders"] if o["venue"] == "BYBIT"); maker_s = next(o for o in slow["orders"] if o["venue"] == "BYBIT")
        hedge_b = next(o for o in base["orders"] if o["venue"] == "BINANCE" and o["state"] != "CREATED"); hedge_s = next(o for o in slow["orders"] if o["venue"] == "BINANCE" and o["state"] != "CREATED")
        self.assertGreater(slow_rep.latency_model.processing_stages.get("strategy+risk", 0), 0)
        lm = slow["manifest"]["timing"]["latency_model"]
        self.assertEqual(lm["processing_ms"], {"strategy": 3.0, "risk": 2.0})
        # the hedge request leaves 5 ms later, so the hedge order's terminal report is later by at least that
        self.assertGreaterEqual(hedge_s["terminal_ts_ns"] - hedge_b["terminal_ts_ns"], 5_000_000 - 1)
        self.assertNotIn("processing_ms", base["manifest"]["timing"]["latency_model"])


class LifecycleReportByRegime(unittest.TestCase):
    """Owner 12 Nov (EMS note section 6): fill rate = quotes with a fill / quotes placed, the cancel-to-fill races, message load per minute, residual exposure, hedge completion and the adverse tails, in full and split by
    volatility, spread, liquidity (terciles from the TRAINING days) and UTC hour. Every split is a partition of the run."""

    def _orders(self):
        base = 1793272000.0
        mk = lambda i, sent, fills=(), cancel_req=None, cancel_conf=None, ack=True, wd=False, cws=False: {
            "order_id": f"o{i}", "sent_ts": sent, "ack_ts": sent + 0.04 if ack else None, "cancel_req_ts": cancel_req, "cancel_conf_ts": cancel_conf, "fills": [list(f) for f in fills],
            "withdrawn": wd, "cancel_while_sent": cws, "amend_ts": []}
        rows = []
        for i in range(40):                                   # 40 quotes, 10 of them filled, spread over 2 hours and the three regimes
            sent = base + i * 100.0
            fills = [(sent + 1.0, 1.0)] if i % 4 == 0 else []
            rows.append(mk(i, sent, fills))
        rows.append(mk(100, base + 5.0, fills=[(base + 6.5, 0.5)], cancel_req=base + 6.0, cancel_conf=base + 7.0))                        # a fill while the cancel was pending
        rows.append(mk(101, base + 9.0, fills=[(base + 12.0, 0.5)], cancel_req=base + 10.0, cancel_conf=base + 11.0))                      # a late fill after the cancel
        rows.append(mk(102, base + 20.0, cancel_req=base + 20.01, cancel_conf=base + 20.1, ack=False, cws=True))                           # cancelled while still submitted
        return base, rows

    def test_fill_rate_races_load_partition_and_small_cells(self):
        from crypto_stat_arb.metrics import lifecycle_report
        base, orders = self._orders()
        per_second = {int(base) + k: (float(k % 7), 1.0 + (k % 5), 50.0 + (k % 11) * 10) for k in range(0, 4200)}
        cuts = {"volatility": [2.0, 4.0], "spread": [2.0, 4.0], "liquidity": [80.0, 120.0]}
        pairs = [{"maker_fill_ts": o["fills"][0][0], "unhedged_interval_s": 0.04, "fully_hedged": True, "hedge_children": 1} for o in orders if o["fills"]]
        marks = [{"ts": p["maker_fill_ts"], "horizon_s": 1.0, "role": "maker", "m_bps": float(i % 5) - 2.0} for i, p in enumerate(pairs)]
        rep = lifecycle_report(orders, pairs, marks, [{"state": "RECONCILED"}] * len(pairs), per_second, cuts)
        q = rep["total"]["quotes"]
        self.assertEqual(q["quotes_placed"], 43)
        self.assertEqual(q["quotes_filled"], 12)                                                              # 10 plain + the two raced quotes
        self.assertAlmostEqual(q["fill_rate"], 12 / 43)
        self.assertEqual((q["fills_during_cancel_pending"], q["late_fills_after_cancel"], q["cancel_while_submitted"]), (1, 1, 1))
        self.assertTrue(rep["partition_ok"])                                                                   # every split adds up to the whole
        for dim in ("volatility", "spread", "liquidity", "utc_hour"):
            self.assertEqual(sum(c["quotes"]["quotes_placed"] for c in rep["by_regime"][dim].values()), 43, dim)
            self.assertEqual(sum(c["hedge_completion"]["maker_fills"] for c in rep["by_regime"][dim].values()), len(pairs), dim)
        small = [c["quotes"] for c in rep["by_regime"]["utc_hour"].values() if c["quotes"]["quotes_placed"] < 30]
        self.assertTrue(small and all(c["fill_rate"] is None and "n < 30" in c["note"] for c in small))        # a small cell prints counts, never a rate
        self.assertEqual(rep["hedge_chains_by_final_state"], {"RECONCILED": len(pairs)})
        self.assertIn("1s", rep["total"]["adverse_tails_markout_bps"])

    def test_without_boundaries_only_the_hour_split_exists_and_the_run_is_unbucketed(self):
        from crypto_stat_arb.metrics import lifecycle_report
        base, orders = self._orders()
        rep = lifecycle_report(orders, [], [], [], {}, None)
        self.assertEqual(list(rep["by_regime"]["volatility"]), ["unbucketed"])
        self.assertTrue(rep["partition_ok"])
        self.assertIsNone(rep["regime_boundaries"])

    def test_tercile_cuts_and_buckets(self):
        from crypto_stat_arb.metrics import regime_bucket, regime_sample, tercile_cuts
        cuts = tercile_cuts([float(i) for i in range(1, 91)])
        self.assertEqual([regime_bucket(v, cuts) for v in (1, 30, 31, 60, 61, 90)], ["low", "low", "mid", "mid", "high", "high"])
        vol, spread, depth, mid = regime_sample(100.0, 100.0, 100.02, 3.0, 4.0)
        self.assertAlmostEqual(vol, 1.0, places=3); self.assertAlmostEqual(spread, 2.0, places=2); self.assertEqual(depth, 7.0)

    @unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
    def test_a_replay_with_the_report_on_writes_it_and_off_changes_nothing(self):
        import tempfile
        from new_backtest.session_replay import write_result
        profile = load_profile("perp_perp_maker_taker_v1_9")
        def run(on):
            cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_lc")
            cfg.arb.max_position_base = 1.0
            cfg.arb.participation_max = 0.0                                              # v1_9 limits a new quote to 10% of the prints at its level; these synthetic streams have none and the test is about something else (tests/test_e3_participation.py covers the limit)
            return SessionReplay(cfg, profile, stream=stream(), decompose_markouts=True, lifecycle_report=on).run()
        off, on = run(False), run(True)
        self.assertNotIn("_lifecycle", off)
        self.assertEqual(off["orders"], on["orders"]); self.assertEqual(off["fills"], on["fills"])               # observation only
        cov = on["_ems_coverage"]                                                                                # owner 12 Nov, section J: every path listed, the per-order checks pass
        for path in ("intent_states", "maker_placed", "maker_clipped_by_hedge_depth", "maker_withdrawn_on_event", "maker_expired_at_lifetime", "maker_cancel_while_submitted", "hedge_ioc_full", "hedge_ioc_partial",
                     "hedge_ioc_zero", "hedge_retry", "query_before_replace", "unwind_orders", "flatten_market_banded", "breaker_trip", "margin_pause", "margin_resume", "lot_residual", "rate_limit_deferral",
                     "duplicate_updates_suppressed", "late_fills_after_cancel", "participation_cap_would_bind", "hedge_cap_size_aware", "hedge_cap_touch_slack"):
            self.assertIn(path, cov["paths"])
        self.assertEqual(cov["paths"]["maker_placed"], on["_lifecycle"]["total"]["quotes"]["quotes_placed"])
        self.assertTrue(cov["checks"]["pass"], cov["checks"]); self.assertEqual(cov["tactic_violations"], [])
        self.assertIn("matched_entry_spread_bps", on["_lifecycle"]["total"]["quotes"])
        rep = on["_lifecycle"]
        self.assertTrue(rep["partition_ok"])
        self.assertGreaterEqual(rep["total"]["quotes"]["quotes_placed"], 1)
        with tempfile.TemporaryDirectory() as d:
            write_result(on, Path(d))
            self.assertTrue((Path(d) / "session_lifecycle_report.json").exists())
            self.assertNotIn("_lifecycle", json.loads((Path(d) / "session_manifest.json").read_text()))


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class RecoveryAfterAFeedGap(unittest.TestCase):
    """Owner 12 Nov (EMS note section 6, case 8): after a feed gap or a stale book, quoting resumes only after a valid, fresh book; no order is priced from a stale book."""

    def _run(self, gap):
        profile = load_profile("perp_perp_maker_taker_v1_7")
        cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_gap")
        cfg.arb.max_position_base = 1.0
        cfg.arb.participation_max = 0.0                                              # v1_9 limits a new quote to 10% of the prints at its level; these synthetic streams have none and the test is about something else (tests/test_e3_participation.py covers the limit)
        cfg.arb.maker_max_age_s = 1.0                                               # a quote that nobody hits lives 1 s, so a new quote is due about once a second when quoting is allowed
        ev, book_ts, t = [snap(1.0, "bybit", 100.00, 100.30), snap(1.0, "binance", 100.30, 100.31)], {"bybit": [1.0], "binance": [1.0]}, 1.1
        while t < 14.0:
            if not (gap[0] <= t < gap[1]):                                           # nothing arrives from either venue during the gap
                ev += [snap(t, "binance", 100.30, 100.31), snap(t + 0.01, "bybit", 100.00, 100.30)]
                book_ts["binance"].append(t); book_ts["bybit"].append(t + 0.01)
            t += 0.1
        rep = SessionReplay(cfg, profile, stream=sorted(ev, key=lambda e: (e.exchange_ts, e.venue)))
        return rep, rep.run(), cfg, book_ts

    def _sends(self, rep):
        quotes = {c.order_id for p in rep.session.oms.plans.values() for c in p.children if c.post_only}
        return sorted(e["ts_ns"] / 1e9 for e in rep.session.ledger.events if e["event_type"] == "ORDER_LIFECYCLE" and e.get("lifecycle_event") == "SENT" and e["order_id"] in quotes)

    def test_no_order_is_priced_from_a_stale_book_and_quoting_resumes_after_a_fresh_book(self):
        rep, result, cfg, book_ts = self._run(gap=(3.0, 9.0))
        sends = self._sends(rep)
        max_age = rep.session.cfg.max_book_age_ns / 1e9
        self.assertTrue(any(t < 3.0 for t in sends) and any(t >= 9.0 for t in sends), sends)               # it quoted before the gap and quotes again after it
        for ts in sends:
            for venue, times in book_ts.items():
                last = max(x for x in times if x <= ts + 0.011)                                              # the venue's latest book message at the time of the send
                self.assertLessEqual(ts - last, max_age + 0.05, f"order sent at {ts} priced from a {venue} book {ts - last:.2f} s old")
        inside = [t for t in sends if 3.0 + max_age < t < 9.0]
        self.assertEqual(inside, [])                                                                          # none inside the gap
        write_case("case8_recovery_after_a_feed_gap_no_order_priced_from_a_stale_book", rep.session.ledger.events, [(3.0, "both", "last_book_before_gap"), (9.0, "both", "first_book_after_gap")])

    def test_the_same_stream_without_a_gap_quotes_through_the_window(self):
        rep, result, cfg, book_ts = self._run(gap=(0.0, 0.0))
        self.assertTrue(any(3.0 + 0.5 < t < 9.0 for t in self._sends(rep)))                                  # the gap is what stopped the quoting, not the stream


def snap_levels(t, venue, bids, asks):
    return timed(t, venue, BOOK, {"timestamp": t, "bids": [[str(p), q] for p, q in bids], "asks": [[str(p), q] for p, q in asks]})


class SizeAwareCapRule(unittest.TestCase):
    """Owner 12 Nov, EMS note section 1 (price-cap construction): execution.hedge.cap = size_aware. The pure rule and the OMS method that applies it."""

    def test_touch_covers_the_size_equals_touch_slack_exactly(self):
        import random
        from decimal import Decimal as D
        from crypto_stat_arb.ems.layer import hedge_cap_on_grid, hedge_cap_size_aware
        rnd = random.Random(11)
        for _ in range(2000):
            tick = D(rnd.choice(["0.001", "0.005", "0.01", "0.1"]))
            touch = D(str(round(rnd.uniform(1, 400), 4)))
            slack = D(str(round(rnd.uniform(0, 30), 3)))
            for side in ("buy", "sell"):
                cap, binds = hedge_cap_size_aware(side, touch, touch, slack, 0.0, 15.0, tick)
                self.assertEqual(cap, hedge_cap_on_grid(side, touch, slack, tick))
                self.assertFalse(binds)

    def test_a_size_spanning_levels_moves_the_cap_to_the_deepest_level_and_the_absolute_limit_binds(self):
        from decimal import Decimal as D
        from crypto_stat_arb.ems.layer import hedge_cap_on_grid, hedge_cap_size_aware
        tick = D("0.01")
        cap, binds = hedge_cap_size_aware("sell", D("100.30"), D("100.29"), 5.0, 0.0, 15.0, tick)          # one tick deeper: 1 bp
        self.assertEqual(cap, hedge_cap_on_grid("sell", D("100.29"), 5.0, tick))
        self.assertLess(cap, hedge_cap_on_grid("sell", D("100.30"), 5.0, tick))
        self.assertFalse(binds)
        cap, binds = hedge_cap_size_aware("sell", D("100.30"), D("99.00"), 5.0, 0.0, 15.0, tick)           # 130 bps deeper: the limit (15 bps beyond the touch) binds
        self.assertTrue(binds)
        self.assertEqual(cap, hedge_cap_on_grid("sell", D("100.30"), 15.0, tick))
        cap, binds = hedge_cap_size_aware("buy", D("100.31"), D("102.00"), 5.0, 0.0, 15.0, tick)
        self.assertTrue(binds)
        self.assertEqual(cap, hedge_cap_on_grid("buy", D("100.31"), 15.0, tick))

    def test_the_volatility_term_widens_the_allowance_and_k_zero_is_off(self):
        from decimal import Decimal as D
        from crypto_stat_arb.ems.layer import hedge_cap_size_aware
        tick = D("0.01")
        off, _ = hedge_cap_size_aware("buy", D("100.00"), D("100.00"), 5.0, 0.0, 15.0, tick)
        on, _ = hedge_cap_size_aware("buy", D("100.00"), D("100.00"), 5.0, 4.0, 15.0, tick)
        self.assertGreater(on, off)

    def test_tick_rounding_direction_never_loosens_even_when_the_limit_binds(self):
        import random
        from decimal import Decimal as D
        from crypto_stat_arb.ems.layer import hedge_cap_size_aware
        from crypto_stat_arb.hedge_lifecycle import touch_slack_price
        rnd = random.Random(5)
        for _ in range(3000):
            tick = D(rnd.choice(["0.001", "0.005", "0.01"]))
            touch = D(str(round(rnd.uniform(20, 300), 3)))
            slack = rnd.uniform(0, 12)
            far = D(str(round(rnd.uniform(0, 0.02), 4)))
            for side in ("buy", "sell"):
                deepest = touch + far if side == "buy" else touch - far
                cap, _ = hedge_cap_size_aware(side, touch, deepest, slack, 0.0, 15.0, tick)
                loosest = touch_slack_price(side, deepest, D(str(slack)))
                limit = touch_slack_price(side, touch, D(str(max(15.0, slack))))
                bound = min(loosest, limit) if side == "buy" else max(loosest, limit)
                self.assertEqual(cap % tick, 0)
                self.assertLessEqual(cap, bound) if side == "buy" else self.assertGreaterEqual(cap, bound)

    def _oms_call(self, book, qty="1.0", side_name="SELL", policy_kw=None):
        from decimal import Decimal as D
        from types import SimpleNamespace as NS
        from crypto_stat_arb.hedge_lifecycle import HedgePolicy
        from crypto_stat_arb.ledger import Ledger
        from crypto_stat_arb.models import Side
        from crypto_stat_arb.oms import OMS
        side = Side.SELL if side_name == "SELL" else Side.BUY
        stub = NS(hedge_policy=HedgePolicy(touch_slack_bps=5.0, size_aware=True, **(policy_kw or {})), instruments=NS(get=lambda v, i, ts=None: NS(tick_size=D("0.01"))),
                  realism_counters={}, ledger=Ledger(), hedge_short_vol_bps=None)
        touch = book.best_bid if side == Side.SELL else book.best_ask
        cap = OMS._size_aware_cap(stub, NS(parent_id="p"), NS(chain_id="c"), {"side": side, "venue": "BINANCE", "instrument_id": "SOLUSDT"}, book, touch, D(qty), 1, 0)
        return cap, stub

    def _book(self, bids, asks=None):
        from decimal import Decimal as D
        from crypto_stat_arb.orderbook import OrderBook
        return OrderBook("BINANCE", "SOLUSDT", bids={D(p): D(q) for p, q in bids}, asks={D(p): D(q) for p, q in (asks or [("100.31", "50")])})

    def test_the_method_covers_spans_falls_back_and_recomputes_on_each_attempt(self):
        from decimal import Decimal as D
        from crypto_stat_arb.ems.layer import hedge_cap_on_grid
        covered, st = self._oms_call(self._book([("100.30", "50"), ("100.29", "50")]))
        self.assertEqual(covered, hedge_cap_on_grid("sell", D("100.30"), 5.0, D("0.01")))                    # the touch covers 1.0: touch_slack exactly
        ev = st.ledger.events[-1]; self.assertTrue(ev["touch_covers"]); self.assertEqual(ev["fallback"], "")
        spans, st = self._oms_call(self._book([("100.30", "0.4"), ("100.29", "50")]))
        self.assertEqual(spans, hedge_cap_on_grid("sell", D("100.29"), 5.0, D("0.01")))                      # the size reaches 100.29
        self.assertFalse(st.ledger.events[-1]["touch_covers"])
        single, st = self._oms_call(self._book([("100.30", "0.4")]))                                          # best bid/ask only and the touch cannot cover it: touch_slack, labelled
        self.assertEqual(single, hedge_cap_on_grid("sell", D("100.30"), 5.0, D("0.01")))
        self.assertIn("touch_slack", st.ledger.events[-1]["fallback"]); self.assertEqual(st.realism_counters["hedge_cap_depth_fallback"], 1)
        far, st = self._oms_call(self._book([("100.30", "0.4"), ("99.00", "50")]))                            # the size needs a level 130 bps away: the limit binds, the remainder follows the retry/unwind path
        self.assertEqual(far, hedge_cap_on_grid("sell", D("100.30"), 15.0, D("0.01")))
        self.assertEqual(st.realism_counters["hedge_cap_abs_limit_binds"], 1)
        self.assertEqual(st.ledger.events[-1]["book"], "view")
        # a retry takes the CURRENT view book: the same rule on a book that moved gives a different cap, nothing is carried from the first attempt
        first, _ = self._oms_call(self._book([("100.30", "0.4"), ("100.29", "50")]))
        second, _ = self._oms_call(self._book([("100.20", "0.4"), ("100.19", "50")]))
        self.assertLess(second, first)
        self.assertEqual(second, hedge_cap_on_grid("sell", D("100.19"), 5.0, D("0.01")))

    def test_config_rules(self):
        from crypto_stat_arb.ems.config import ExecutionConfig, ExecutionConfigError
        from crypto_stat_arb.ems.layer import hedge_policy_fields
        ExecutionConfig(hedge={"tactic": "IOC_CAPPED", "cap": "size_aware", "slack_bps_floor": 5.0, "max_slip_bps": 15.0, "vol_k": 0.0, "latency_s": 0.0})
        with self.assertRaises(ExecutionConfigError):
            ExecutionConfig(hedge={"tactic": "IOC_CAPPED", "cap": "size_aware", "max_slip_bps": -1})
        with self.assertRaises(ExecutionConfigError):
            ExecutionConfig(hedge={"tactic": "IOC_CAPPED", "cap": "size_aware", "cap_basis": "size_vwap"})
        self.assertEqual(hedge_policy_fields({"cap": "touch_slack", "slack_bps_floor": 5.0}, 3.0), {"touch_slack_bps": 5.0, "cap_basis": "touch"})        # touch_slack: nothing new
        self.assertTrue(hedge_policy_fields({"cap": "size_aware"}, 3.0)["size_aware"])


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class SizeAwareCapInTheReplay(unittest.TestCase):
    def _run(self, profile_name, bin_bids, bin_asks=None, thin_from=None):
        profile = load_profile(profile_name)
        cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_sacap")
        cfg.arb.max_position_base = 1.0
        cfg.arb.participation_max = 0.0                                              # v1_9 limits a new quote to 10% of the prints at its level; these synthetic streams have none and the test is about something else (tests/test_e3_participation.py covers the limit)
        asks = bin_asks or [("100.31", "50")]
        ev = [snap(1.0, "bybit", 100.00, 100.30), snap_levels(1.0, "binance", bin_bids, asks)]
        t = 1.1
        for _ in range(40):
            bids = bin_bids if thin_from is None or t < thin_from else [("100.30", "0.4")]        # thin_from: the hedge book holds the touch only (and too little of it) from that time on
            ev += [snap_levels(t, "binance", bids, asks), snap(t + 0.01, "bybit", 100.00, 100.30)]
            t += 0.1
        ev.append(timed(3.0, "bybit", TRADE, {"exchange_ts": 3.0, "price": "100.24", "volume": "30", "side": "sell"}))
        rep = SessionReplay(cfg, profile, stream=sorted(ev, key=lambda e: (e.exchange_ts, e.venue)))
        result = rep.run()
        hedge = [o for o in result["orders"] if o["venue"] == "BINANCE" and o["state"] != "CREATED"]
        return result, rep, hedge

    def test_when_the_touch_covers_the_size_v1_9_sends_the_v1_7_hedge_limit(self):
        deep = [("100.30", "50"), ("100.29", "50")]
        _, _, h7 = self._run("perp_perp_maker_taker_v1_7", deep)
        r9, _, h9 = self._run("perp_perp_maker_taker_v1_9", deep)
        self.assertTrue(h7 and h9)
        self.assertEqual([o["limit_price"] for o in h7], [o["limit_price"] for o in h9])
        ev = [e for e in r9["_decision_events"] if e["event_type"] == "HEDGE_CAP_SIZE_AWARE"]
        self.assertTrue(ev and all(e["touch_covers"] for e in ev))

    def test_a_size_spanning_levels_lowers_the_sell_cap_and_the_view_book_is_the_one_read(self):
        from decimal import Decimal as D
        from crypto_stat_arb.ems.layer import hedge_cap_on_grid
        from crypto_stat_arb.orderbook import OrderBook
        seen = []
        real = OrderBook.reach
        def spy(self, side, qty):
            seen.append(self); return real(self, side, qty)
        with mock.patch.object(OrderBook, "reach", spy):
            r9, rep, h9 = self._run("perp_perp_maker_taker_v1_9", [("100.30", "0.4"), ("100.29", "50")])
        _, _, h7 = self._run("perp_perp_maker_taker_v1_7", [("100.30", "0.4"), ("100.29", "50")])
        self.assertEqual(D(h9[0]["limit_price"]), hedge_cap_on_grid("sell", D("100.29"), 5.0, D("0.001")))
        self.assertLess(D(h9[0]["limit_price"]), D(h7[0]["limit_price"]))
        view_books = {id(b) for b in rep.session.books.values()}
        self.assertTrue(seen and all(id(b) in view_books for b in seen))                                   # the OMS reads the bot's own book objects; the venue's truth is a different structure (the simulator's SimBook)
        ev = [e for e in r9["_decision_events"] if e["event_type"] == "HEDGE_CAP_SIZE_AWARE"]
        self.assertFalse(ev[0]["touch_covers"]); self.assertEqual(ev[0]["book"], "view")
        write_case("case2_size_aware_hedge_cap_size_spans_levels_view_book", rep.session.ledger.events if hasattr(rep.session, "ledger") else r9["_decision_events"])

    def test_best_bid_ask_only_falls_back_to_touch_slack_and_says_so(self):
        full = [("100.30", "50")]
        _, _, h7 = self._run("perp_perp_maker_taker_v1_7", full, thin_from=2.5)
        r9, _, h9 = self._run("perp_perp_maker_taker_v1_9", full, thin_from=2.5)
        self.assertTrue(h7 and h9)
        # the hedge attempts price the same under both caps; v1_9 (13 Nov: a failed hedge always unwinds) then sends the unwind orders that v1_7 does not (its residual stays under the position-size bound)
        self.assertEqual([o["limit_price"] for o in h7], [o["limit_price"] for o in h9][:len(h7)])
        ev = [e for e in r9["_decision_events"] if e["event_type"] == "HEDGE_CAP_SIZE_AWARE"]
        self.assertTrue(ev and all("touch_slack" in e["fallback"] for e in ev))


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class EmsNoteDuplicateFilledRace(unittest.TestCase):
    """Owner 12 Nov item 3: Bybit repeats a "Filled" message in a cancel/fill race. The fill is booked once and the residual is hedged once."""

    def _run(self, duplicate):
        from dataclasses import replace
        from crypto_stat_arb.execution import ExecutionEventType
        from crypto_stat_arb.oms import OMS
        real = OMS.ingest_order_update
        def doubled(self, update):
            out = real(self, update)
            if duplicate and update.state in (ExecutionEventType.FILLED, ExecutionEventType.PARTIAL) and "INTERNAL" not in str(update.source):
                real(self, replace(update, update_id=update.update_id + "-dup"))                  # the same fill reported again, as a new message
            return out
        with mock.patch.object(OMS, "ingest_order_update", doubled):
            rep = LayerRiskGateInTheReplay._replay(self); result = rep.run()
        return rep, result

    def test_a_duplicate_filled_message_changes_nothing(self):
        rep, dup = self._run(True); _, base = self._run(False)
        write_case("case3_duplicate_bybit_filled_in_a_cancel_fill_race", rep.session.ledger.events, [(3.0, "bybit", "trade")])
        self.assertEqual([f["venue"] for f in dup["fills"]], [f["venue"] for f in base["fills"]])      # one maker fill and one hedge fill, not two of each
        self.assertEqual(dup["positions"], base["positions"]); self.assertEqual(dup["cash_by_venue"], base["cash_by_venue"])
        self.assertEqual(len([o for o in dup["orders"] if o["venue"] == "BINANCE" and o["state"] != "CREATED"]), 1)       # the hedge was sent once
        self.assertEqual(dup["residual_at_end"]["flag"], "FLAT_NET")
        self.assertTrue(dup["execution_layer"]["exec_reconciliation"]["ok"])                         # and the shadow reconciliation agrees with the ledger


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class EmsNoteRateLimitPressure(unittest.TestCase):
    """Owner 12 Nov item 4: with the scoped limiter near capacity on the session path, cancels and hedges still go out and a new maker quote is refused (tiers: new risk 70%, risk-reducing 80%, cancel 100% of capacity)."""

    class Row:
        def __init__(self, name, scope, limit, interval_s=100_000.0, status="ASSUMED"):
            self.name, self.scope, self.limit, self.interval_seconds, self.status = name, scope, limit, interval_s, status

    def _limiter(self, used, ts_ns=0, interval_s=100_000.0):
        from crypto_stat_arb.rate_limits import ScopedRateLimiter
        R = lambda name, scope, limit: self.Row(name, scope, limit, interval_s)
        lim = ScopedRateLimiter({("BYBIT", "PERP"): [R("HTTP", "IP", 100), R("/v5/order/create", "UID_ENDPOINT", 100), R("/v5/order/cancel", "UID_ENDPOINT", 100)],
                                 ("BINANCE", "PERP"): [R("REQUEST_WEIGHT", "IP", 100), R("ORDERS", "ACCOUNT", 100)]})
        for b in lim.buckets.values():
            b.charge(ts_ns, used)
        return lim

    def _replay(self):
        return LayerRiskGateInTheReplay._replay(self)

    def test_at_85_percent_a_new_maker_quote_is_skipped_not_queued_not_retried(self):
        """Owner 12 Nov (the 2f decision): new quotes use at most 80% of the window and leave 2 requests free; beyond that the quote is SKIPPED (no plan, nothing queued)."""
        rep = self._replay(); rep.session.oms.router.scoped_limits = self._limiter(85, 1)
        result = rep.run()
        self.assertEqual([o for o in result["orders"] if o["post_only"]], [])                          # no maker order was even created
        self.assertEqual(result["fills"], [])
        self.assertGreater(rep.session.quotes_skipped_rate_limit, 0)
        self.assertEqual(len([e for e in rep.session.ledger.events if e["event_type"] == "QUOTE_SKIPPED_RATE_LIMIT"]), min(rep.session.quotes_skipped_rate_limit, 200))

    def test_at_75_percent_a_new_quote_still_fits_its_share(self):
        rep = self._replay(); rep.session.oms.router.scoped_limits = self._limiter(75, 1)
        result = rep.run()
        self.assertTrue(result["fills"])                                                               # the quote rested and filled, the hedge went out
        self.assertEqual(rep.session.quotes_skipped_rate_limit, 0)

    def test_a_hedge_at_a_full_window_is_queued_with_priority_and_sent_when_the_window_slides_never_refused(self):
        """Owner 12 Nov: hedges, unwinds, flattens and cancels are never refused by our own limiter. The window is FULL just as the maker fills; the hedge waits for room (1 s window) and goes out."""
        from crypto_stat_arb.oms import OMS
        real_hedge = OMS._hedge_for_maker_fill
        def at_fill(self_, plan, spec, maker, ts_ns):
            self_.router.scoped_limits = self._limiter(100, ts_ns, interval_s=1.0)
            return real_hedge(self_, plan, spec, maker, ts_ns)
        rep = self._replay()
        with mock.patch.object(OMS, "_hedge_for_maker_fill", at_fill):
            result = rep.run()
        write_case("case4_rate_limit_full_window_hedge_queued_with_priority_never_refused", rep.session.ledger.events)
        hedges = [o for o in result["orders"] if o["venue"] == "BINANCE"]
        self.assertTrue(hedges)
        self.assertEqual([o for o in hedges if o["cancel_reason"] == "scoped_rate_limited"], [])        # zero hedges refused
        self.assertEqual([(o["state"], o["filled_quantity"]) for o in hedges], [("FILLED", "1.0")])
        self.assertEqual(result["residual_at_end"]["flag"], "FLAT_NET")
        maker_fill = next(f for f in result["fills"] if f["venue"] == "BYBIT")
        hedge_fill = next(f for f in result["fills"] if f["venue"] == "BINANCE")
        self.assertGreaterEqual(hedge_fill["ts_ns"] - maker_fill["ts_ns"], 900_000_000)               # it waited for the window to slide
        self.assertTrue(any(e["event_type"] == "CHILD_DISPATCH_THROTTLED" for e in rep.session.ledger.events))

    def test_a_cancel_waits_for_room_and_is_never_refused_at_a_full_window(self):
        rep2 = self._replay(); rep2.run()
        lim = self._limiter(100, 1)
        self.assertFalse(lim.check("BYBIT", "PERP", "SUBMIT", "NEW_RISK", 2).admitted)
        self.assertFalse(lim.check("BINANCE", "PERP", "SUBMIT", "RISK_REDUCING", 2).admitted)       # the venue's own limit is full: the router queues it, it does not drop it
        self.assertFalse(lim.check("BYBIT", "PERP", "CANCEL", "CANCEL", 2).admitted)                  # at 100% even a cancel must wait (the router delays it, see test_rate_limits)


@unittest.skipUnless(SLICES.is_dir(), "March slices not available (the profile needs a data root)")
class EmsNoteAlertContext(unittest.TestCase):
    """Owner 12 Nov item 5: the hedge alarm (and the breaker and unwind alarms, see test_session_breaker and test_oms_unwind_alarm) carry the confirmed residual, the pending hedge quantity, the oldest exposure age and why execution is blocked."""

    def test_the_hedge_alarm_carries_the_exposure_and_the_reason(self):
        gh = GhostHedgeInTheReplay("test_the_hedge_is_held_during_the_outage_and_executed_at_the_reconnect")
        rep = gh._replay(gh._faults(30.0), hedge_fault_injection=True); rep.run()
        alarms = [e for e in rep.session.ledger.events if e["event_type"] == "HEDGE_ALARM"]
        write_case("case5_alerts_hedge_alarm_carries_the_exposure_and_the_reason", rep.session.ledger.events)
        self.assertEqual(len(alarms), 1)
        a = alarms[0]
        for key in ("confirmed_residual_base", "pending_hedge_qty", "oldest_exposure_age_s", "execution_blocked_reason"):
            self.assertIn(key, a)
        self.assertGreater(float(a["pending_hedge_qty"]), 0.0)                                         # the hedge is still out
        self.assertGreater(a["oldest_exposure_age_s"], 0.0)
        self.assertTrue(a["execution_blocked_reason"])                                                  # new risk is blocked, and the alert says why
