"""Phase D, step D5: the session replay end to end on a synthetic stream (wiring only, never a profit figure). The strategy signals through the shim, the core router sends a PostOnly maker
bid after its entry latency, a trade print through the bid fills it (queue adapter, evidence from a print), FILL_TRIGGERED hedges on the other venue, and the ledger carries both legs."""
import sys
import unittest
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
        for key in ("orders", "fills", "signals", "positions", "cash_by_venue", "equity_at_book_mids", "ledger_event_counts", "fills_by_source"):
            self.assertEqual(off[key], self.result[key], key)

    def test_nothing_in_the_oms_reads_the_observer_back(self):
        """The OMS only CALLS the observer, as a statement: no assignment, return, argument or condition takes a value from it."""
        import re

        src = (Path(__file__).resolve().parents[1] / "src" / "crypto_stat_arb" / "oms.py").read_text().splitlines()
        uses = [ln.strip() for ln in src if "observer" in ln]
        for ln in uses:
            self.assertTrue(re.fullmatch(r"(self\.observer = None.*|if self\.observer is not None( and to in \(HedgeState\.ESCALATED, HedgeState\.UNWINDING\))?:|self\.observer\.\w+\(.*\))", ln), ln)


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
