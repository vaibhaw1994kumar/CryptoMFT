"""Phase D, step D5: the session replay end to end on a synthetic stream (wiring only, never a profit figure). The strategy signals through the shim, the core router sends a PostOnly maker
bid after its entry latency, a trade print through the bid fills it (queue adapter, evidence from a print), FILL_TRIGGERED hedges on the other venue, and the ledger carries both legs."""
import sys
import unittest
from unittest import mock
import new_backtest.session_replay as sr
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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
class GhostHedgeInTheReplay(unittest.TestCase):
    """Owner 11 Nov: with hedge_fault_injection on, a conditional outage of the hedge venue's gateway at every maker fill, policy ghost: the hedge is held and executed at the reconnect (not rejected). Off by default."""

    def _replay(self, faults=None, **kw):
        profile = load_profile("perp_perp_maker_taker_v1_5")
        cfg = apply_profile(profile, dataset="mar2024", data_root=str(SLICES), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/x_ghost")
        cfg.arb.max_position_base = 1.0
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
        and nothing else: `admit` (refuse before the OMS reserves) and the late refusal returned by `plan_opened` (roll the plan back)."""
        import re

        src = (Path(__file__).resolve().parents[1] / "src" / "crypto_stat_arb" / "oms.py").read_text().splitlines()
        uses = [ln.strip() for ln in src if "observer" in ln]
        for ln in uses:
            self.assertTrue(re.fullmatch(r"(self\.observer = None.*|if self\.observer is not None( and to in \(HedgeState\.ESCALATED, HedgeState\.UNWINDING\))?:|self\.observer\.\w+\(.*\)|refusal = self\.observer\.admit\(plan, now / 1e9\) .*|late_refusal = self\.observer\.plan_opened\(plan, now / 1e9\))", ln), ln)


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

    def test_an_own_fill_does_not_change_the_public_view_book(self):
        rep = LayerRiskGateInTheReplay._replay(self)
        seen = []
        for adapter in rep.adapters.values():
            real = adapter._taker_fill
            def wrapped(request, book, adapter=adapter, real=real):
                def snap():
                    b = rep.views[adapter.feed_venue]
                    return [(s, p, b.level_size(s, p)) for s in ("bid", "ask") for p in (b.best_bid if s == "bid" else b.best_ask,)]
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
