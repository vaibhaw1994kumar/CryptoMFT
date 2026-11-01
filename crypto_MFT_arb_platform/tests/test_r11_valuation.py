"""R11: ending_equity is the common-timestamp valuation and the equity bridge catches the last-mark figure.

Scenario (Harness books: bids 99/98, asks 101/102 on both venues at t=0.5): a 2.0 maker buy on bybit at 100 fills at t=5, the
hedge sells 2.0 on binance at its bid 99. The pair is flat (+2 bybit / -2 binance). At t=20 bybit alone publishes a new book with
mid 110; binance's last data is t=0.5. Marking each venue at its own last mid books 2 x (110 - 100) = +20 USDT of equity on a
flat pair; marking both at the common timestamp (0.5, bybit mid 100 then) books 0.
"""
from __future__ import annotations

import unittest
import warnings

from new_backtest.backtester import EquityBridgeError
from new_backtest.funding_settler import FundingSettler
from new_backtest.valuation import MidHistory, common_timestamp_valuation

from test_maker_order_lifetime import BINANCE, BYBIT, Harness


def hedged_pair(**cfg_kw) -> Harness:
    h = Harness(cfg_kw=cfg_kw)
    h.book(0.5, BYBIT)
    h.book(0.5, BINANCE)
    h.send_maker(1.0)
    h.sell_trade(5.0, BYBIT, volume=2.5)
    h.advance(5.003)
    return h


class MidHistoryTests(unittest.TestCase):
    def test_samples_keep_the_latest_exactly_and_look_up_at_or_before(self):
        m = MidHistory(sample_s=1.0)
        for t, mid in ((0.0, 100.0), (0.3, 100.5), (1.2, 101.0), (1.9, 101.5), (2.5, 102.0)):
            m.record(t, mid)
        self.assertEqual(m.latest, (2.5, 102.0))
        self.assertEqual(len(m), 3)                                           # 0.0, 1.2, 2.5 sampled; 0.3 and 1.9 dropped
        self.assertEqual(m.at_or_before(1.95), (1.2, 101.0))                 # the last SAMPLE at or before t
        self.assertEqual(m.at_or_before(3.0), (2.5, 102.0))                  # the latest when it is at or before t
        self.assertIsNone(m.at_or_before(-1.0))
        m.record(2.0, 999.0)                                                 # out of order: ignored
        self.assertEqual(m.latest, (2.5, 102.0))

    def test_prune_is_batched_and_keeps_the_mark_at_or_before_the_floor(self):
        m = MidHistory(sample_s=1.0)
        for i in range(10_000):
            m.record(float(i), 100.0 + i)
        m.prune(5_000.5)
        self.assertEqual(m.at_or_before(5_000.5), (5_000.0, 5_100.0))
        self.assertLess(len(m), 5_100)
        m2 = MidHistory()
        for i in range(10):
            m2.record(float(i), 1.0)
        m2.prune(9.0)                                                        # fewer than PRUNE_BATCH entries: nothing shifted
        self.assertEqual(len(m2), 10)

    def test_valuation_marks_both_venues_at_the_common_timestamp_and_flags_a_lagging_venue(self):
        a, b = MidHistory(), MidHistory()
        a.record(0.5, 100.0); a.record(20.0, 110.0)
        b.record(0.5, 100.0)
        v = common_timestamp_valuation({"a": a, "b": b}, {"a": 2.0, "b": -2.0}, 1.0, ("a", "b"), max_staleness_s=1.0)
        self.assertTrue(v["available"])
        self.assertEqual((v["common_ts"], v["marks"]), (0.5, {"a": 100.0, "b": 100.0}))
        self.assertEqual(v["open_position_value"], 0.0)
        self.assertEqual(v["equity"], 1.0)
        self.assertEqual(v["venue_last_update_spread_s"], 19.5)
        self.assertTrue(v["lagging_venue_flag"])                             # flagged, never rejected
        self.assertEqual(v["cross_venue_basis_at_common_ts"], 0.0)
        nothing = common_timestamp_valuation({"a": a, "b": MidHistory()}, {}, 0.0, ("a", "b"), max_staleness_s=1.0)
        self.assertFalse(nothing["available"])
        self.assertEqual(nothing["reason"], "no_price_on_venue")


class BridgeTests(unittest.TestCase):
    def test_last_mark_figure_fails_the_bridge_and_the_common_timestamp_figure_passes(self):
        h = hedged_pair()
        bt = h.bt
        h.book(20.0, BYBIT, bids=((109.0, 5.0), (108.0, 5.0)), asks=((111.0, 5.0), (112.0, 5.0)))
        self.assertEqual((bt.inventory.base[BYBIT], bt.inventory.base[BINANCE]), (2.0, -2.0))
        last_mark_open = sum(bt.inventory.base[v] * bt.books[v].mid for v in (BYBIT, BINANCE))
        self.assertAlmostEqual(last_mark_open, 20.0, places=9)                # 2 x (110 - 100): the inflation
        with self.assertRaises(EquityBridgeError) as cm:
            bt._equity_bridge(last_mark_open, bt.inventory.total_cash + last_mark_open)
        self.assertIn("common timestamp 0.5", str(cm.exception))
        val = bt._common_valuation()
        self.assertEqual(val["marks"], {BYBIT: 100.0, BINANCE: 100.0})
        doc = bt._equity_bridge(val["open_position_value"], bt.inventory.total_cash + val["open_position_value"])
        self.assertTrue(doc["ok"])
        self.assertAlmostEqual(doc["common_timestamp_open_position_value"], 0.0, places=9)
        self.assertEqual(doc["valuation_basis"], "common_timestamp")

    def test_build_result_headline_is_common_timestamp_and_last_mark_is_reported_as_inflation(self):
        h = hedged_pair()
        h.book(20.0, BYBIT, bids=((109.0, 5.0), (108.0, 5.0)), asks=((111.0, 5.0), (112.0, 5.0)))
        bt = h.bt
        from types import SimpleNamespace
        r = bt._build_result(SimpleNamespace(report=lambda: {}), {}, 0.0)
        self.assertAlmostEqual(r.ending_equity, bt.inventory.total_cash, places=9)       # flat pair: equity == cash
        self.assertAlmostEqual(r.common_timestamp_equity, r.ending_equity, places=12)
        self.assertAlmostEqual(r.last_mark_equity, r.ending_equity + 20.0, places=9)
        self.assertAlmostEqual(r.valuation["stale_mark_inflation"], 20.0, places=9)
        self.assertEqual(r.valuation["headline"], "common_timestamp")
        self.assertTrue(r.valuation["lagging_venue_flag"])
        self.assertTrue(r.equity_bridge["ok"])

    def test_legacy_last_mark_valuation_is_reachable_but_the_bridge_fails_on_it(self):
        h = hedged_pair(valuation="last_mark", equity_bridge="warn")
        h.book(20.0, BYBIT, bids=((109.0, 5.0), (108.0, 5.0)), asks=((111.0, 5.0), (112.0, 5.0)))
        from types import SimpleNamespace
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            r = h.bt._build_result(SimpleNamespace(report=lambda: {}), {}, 0.0)
        self.assertAlmostEqual(r.ending_equity, h.bt.inventory.total_cash + 20.0, places=9)   # the old figure, on request
        self.assertFalse(r.equity_bridge["ok"])
        self.assertAlmostEqual(r.equity_bridge["equity_gap"], 20.0, places=9)
        self.assertTrue(any("equity bridge failed" in str(x.message) for x in w))
        h2 = hedged_pair(valuation="last_mark")
        h2.book(20.0, BYBIT, bids=((109.0, 5.0), (108.0, 5.0)), asks=((111.0, 5.0), (112.0, 5.0)))
        with self.assertRaises(EquityBridgeError):
            h2.bt._build_result(SimpleNamespace(report=lambda: {}), {}, 0.0)

    def test_no_inflation_when_both_venues_update_together(self):
        h = hedged_pair()
        h.book(20.0, BYBIT, bids=((109.0, 5.0), (108.0, 5.0)), asks=((111.0, 5.0), (112.0, 5.0)))
        h.book(20.0, BINANCE, bids=((109.0, 5.0), (108.0, 5.0)), asks=((111.0, 5.0), (112.0, 5.0)))
        val = h.bt._common_valuation()
        self.assertEqual(val["common_ts"], 20.0)
        self.assertAlmostEqual(val["open_position_value"], 0.0, places=9)
        self.assertFalse(val["lagging_venue_flag"])


class FundingReportTests(unittest.TestCase):
    def test_every_settlement_is_logged_with_position_mark_and_payment(self):
        import json, tempfile
        from pathlib import Path
        from new_backtest.backtester import InventoryBook
        with tempfile.TemporaryDirectory() as d:
            rows = [{"venue": "bybit", "funding_ts_ns": 1_000 * 10 ** 9, "rate": 0.0001, "mark_price": 100.0},
                    {"venue": "binance", "funding_ts_ns": 1_000 * 10 ** 9, "rate": 0.0002},
                    {"venue": "bybit", "funding_ts_ns": 2_000 * 10 ** 9, "rate": 0.0001, "mark_price": 100.0}]
            Path(d, "f.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
            s = FundingSettler(d, ("bybit", "binance"))
            inv = InventoryBook()
            inv.apply("bybit", 2.0, -200.0)
            inv.apply("binance", -2.0, 198.0)
            s.settle(1_500.0, inv, {})                                       # binance has no mark in the record and no book
            rep = s.report()
            self.assertEqual([r["venue"] for r in rep["settlements"]], ["bybit", "binance"])
            b, n = rep["settlements"]
            self.assertEqual((b["position_base"], b["mark"], b["mark_source"], b["rate"]), (2.0, 100.0, "record", 0.0001))
            self.assertAlmostEqual(b["payment"], 2.0 * 100.0 * 0.0001, places=12)
            self.assertEqual((n["position_base"], n["mark_source"], n["payment"]), (-2.0, "none", 0.0))
            s.settle(2_500.0, inv, {})
            self.assertEqual(len(s.report()["settlements"]), 3)
            self.assertAlmostEqual(s.report()["by_venue"]["bybit"], 0.04, places=12)


if __name__ == "__main__":
    unittest.main()
