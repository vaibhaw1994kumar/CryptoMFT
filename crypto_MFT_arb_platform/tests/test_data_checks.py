"""Phase B data checks (observation only): new_backtest.quality_preflight.scan_venue_day_checks / data_checks_report, core ArrivalChecks, and the rule that a run stops when
quality.py cannot be loaded, override or not. Tiny hand-written partitions: wiring only."""
from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crypto_stat_arb.dataset_manifest import ArrivalChecks
from new_backtest import quality_preflight as qp

DAY = "2024-03-08"
T0 = 1709856000.0


def write(root: Path, venue: str, stream: str, rows: list[dict]) -> None:
    d = root / venue / stream / "pair=SOLUSDT" / f"date={DAY}"
    d.mkdir(parents=True, exist_ok=True)
    with gzip.open(d / "part.jsonl.gz", "wt") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def snap(t, bids=((99.0, 5.0), (98.0, 5.0)), asks=((101.0, 5.0), (102.0, 5.0))):
    return {"timestamp": t, "local_ts": t + 0.002, "best_bid": bids[0][0], "best_ask": asks[0][0], "bids": [list(b) for b in bids], "asks": [list(a) for a in asks]}


def delta(t, side, price, vol, et="update"):
    return {"exchange_ts": t, "local_ts": t + 0.002, "side": side, "price": price, "volume": vol, "event_type": et}


def trade(t, price, vol, side="buy"):
    return {"exchange_ts": t, "local_ts": t + 0.002, "price": price, "volume": vol, "side": side}


class Scan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, **kw):
        return qp.scan_venue_day_checks(self.root, "bybit", "SOLUSDT", DAY, **kw)

    def test_a_cross_inside_one_message_is_not_counted_but_a_cross_at_its_end_is(self):
        write(self.root, "bybit", "book_snapshots", [snap(T0)])
        write(self.root, "bybit", "book_events", [delta(T0 + 1, "bid", 102.0, 1.0), delta(T0 + 1, "bid", 102.0, 0.0, "delete"),      # one message: crosses and uncrosses
                                                  delta(T0 + 2, "bid", 101.5, 3.0)])                                                  # a second message: still crossed at its end
        r = self.scan()
        self.assertEqual((r["observation"]["messages"], r["observation"]["multi_delta_messages"], r["observation"]["crossed_books_at_message_end"]), (2, 1, 1))

    def test_timestamps_going_backwards_are_counted_per_clock(self):
        write(self.root, "bybit", "book_snapshots", [snap(T0)])
        write(self.root, "bybit", "book_events", [delta(T0 + 5, "bid", 99.0, 4.0), delta(T0 + 3, "bid", 98.0, 4.0), delta(T0 + 6, "bid", 99.0, 5.0)])
        r = self.scan()
        self.assertEqual((r["timestamps_backwards_exchange"], r["timestamps_backwards_receive"]), (1, 1))

    def test_invalid_trades_and_sides_are_scanned_and_reported_by_value(self):
        write(self.root, "bybit", "book_snapshots", [snap(T0)])
        write(self.root, "bybit", "trades", [trade(T0 + 1, 100.0, 1.0), trade(T0 + 2, 0.0, 1.0), trade(T0 + 3, 100.0, -1.0), trade(T0 + 4, 100.0, 1.0, side="x")])
        r = self.scan()
        self.assertEqual(r["invalid_trades"], 2)                                                  # price 0 and size -1; an odd side word is the arrival check's
        self.assertEqual(r["arrival_checks"]["trade_side"]["violations"], 1)
        self.assertEqual(r["arrival_checks"]["trade_side"]["status"], "MEASURED")

    def test_stale_gaps_are_counted_at_5_and_30_seconds_with_the_seconds_lost(self):
        write(self.root, "bybit", "book_snapshots", [snap(T0)])
        write(self.root, "bybit", "book_events", [delta(T0 + 6, "bid", 99.0, 4.0), delta(T0 + 40, "bid", 99.0, 5.0), delta(T0 + 40.2, "bid", 99.0, 6.0)])
        g = self.scan()["stale_gaps"]
        self.assertEqual((g["gt_5s"]["count"], g["gt_5s"]["seconds_in_gaps"]), (2, 40.0))        # gaps of 6 s and 34 s
        self.assertEqual((g["gt_30s"]["count"], g["gt_30s"]["seconds_in_gaps"]), (1, 34.0))

    def test_negative_sizes_and_off_tick_prices_are_counted_and_nothing_is_repaired(self):
        write(self.root, "bybit", "book_snapshots", [snap(T0)])
        write(self.root, "bybit", "book_events", [delta(T0 + 1, "bid", 99.0005, 4.0), delta(T0 + 2, "bid", 98.0, -1.0)])
        r = self.scan(tick=0.001)
        self.assertEqual(r["observation"]["negative_sizes"], 1)
        self.assertEqual(r["arrival_checks"]["tick"]["violations"], 1)
        self.assertEqual(r["arrival_checks"]["lot"]["status"], "NOT_MEASURED")                    # no lot step declared
        self.assertIsNone(r["arrival_checks"]["lot"]["violations"])

    def test_the_report_never_shows_a_defect_it_did_not_measure_as_zero(self):
        write(self.root, "bybit", "book_snapshots", [snap(T0)])
        write(self.root, "bybit", "book_events", [delta(T0 + 1, "bid", 99.0, 4.0)])
        rep = qp.data_checks_report(self.root, ("bybit",), "SOLUSDT", (DAY,), ticks={"bybit": 0.001})
        d = rep["rows"][0]["defects"]
        self.assertEqual(d["out_of_order"]["status"], "MEASURED")
        for name in ("gaps", "duplicates", "negative_latency", "impossible_latency", "schema_exceptions", "unpersisted_bytes"):
            self.assertEqual(d[name]["status"], "NOT_MEASURED", name)
            self.assertIsNone(d[name]["count"], name)
        self.assertEqual(d["invalid_trades"]["status"], "NOT_MEASURED")                           # no trade file in this venue-day
        self.assertIn("files carry no sequence ids", d["gaps"]["reason"])
        self.assertEqual(rep["stale_thresholds_s"], {"preflight": 30.0, "engine_dead_feed": 5.0})
        self.assertIn("max_out_of_order", rep["preflight_limits_per_day"])
        self.assertEqual(rep["inline_limits_per_moment"]["dead_feed_s"], 5.0)

    def test_sequence_ids_in_the_files_are_noticed(self):
        write(self.root, "bybit", "book_snapshots", [snap(T0)])
        write(self.root, "bybit", "book_events", [{**delta(T0 + 1, "bid", 99.0, 4.0), "u": 5}])
        d = qp.data_checks_report(self.root, ("bybit",), "SOLUSDT", (DAY,))["rows"][0]["defects"]
        self.assertIn("ids ARE present", d["gaps"]["reason"])


class ArrivalRules(unittest.TestCase):
    def test_side_tick_and_size_rules(self):
        a = ArrivalChecks(tick=0.005)
        a.book_side("Bid"); a.book_side("ask"); a.book_side("b")
        a.trade_side("buy"); a.trade_side("sell"); a.trade_side("b"); a.trade_side("")
        for p in (125.005, 125.01, 125.007):
            a.price(p)
        for sz, z in ((1.0, False), (0.0, True), (0.0, False), (-1.0, True), (float("nan"), True)):
            a.size(sz, may_be_zero=z)
        r = a.report()
        self.assertEqual((r["book_side"]["violations"], r["trade_side"]["violations"], r["tick"]["violations"], r["size_valid"]["violations"]), (1, 1, 1, 3))
        self.assertEqual(r["lot"]["status"], "NOT_MEASURED")


class RulebookMustLoad(unittest.TestCase):
    def test_run_preflight_stops_when_quality_py_cannot_load_even_with_an_override(self):
        with mock.patch.object(qp, "QualityGate", None):
            with self.assertRaises(RuntimeError):
                qp.run_preflight("unused", "bybit", "SOLUSDT", DAY, DAY, report_only=True, require_rules=True)      # what the feed passes for every backtest, override or not
            self.assertTrue(qp.run_preflight("unused", "bybit", "SOLUSDT", DAY, DAY, report_only=True)["skipped"])      # standalone use keeps the old, labelled skip

    def test_the_feed_asks_for_the_rulebook(self):
        from new_backtest.cross_venue_feed import CrossVenueFeed
        from new_backtest.latency_model import LatencyBook
        from new_backtest.config import make_venues
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(qp, "QualityGate", None):
            write(Path(tmp), "bybit", "book_snapshots", [snap(T0)])
            with self.assertRaises(RuntimeError):
                feed = CrossVenueFeed(data_root=tmp, pair="SOLUSDT", venues=("bybit",), start_day=DAY, end_day=DAY, latency=LatencyBook({v: s.latency for v, s in make_venues().items()}),
                                      quality_preflight=True, quality_report_only=True)      # report-only is what an allow_rejected_data override sets
                next(iter(feed))                                                           # the preflight runs when the replay starts


if __name__ == "__main__":
    unittest.main()
