"""Unit tests for FundingSettler — perpetual funding settlement in replay."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from new_backtest.arb_strategy import InventoryBook
from new_backtest.funding_settler import FundingSettler


# ---------------------------------------------------------------------------
# Minimal OrderBook stub — only .mid is needed for mark-price fallback
# ---------------------------------------------------------------------------

class _BookStub:
    def __init__(self, mid: float) -> None:
        self.mid = mid


def _make_dir(records: list[dict]) -> str:
    """Write records to a temp JSONL file and return the directory path."""
    d = tempfile.mkdtemp()
    path = Path(d) / "funding.jsonl"
    with open(path, "w") as fh:
        for row in records:
            fh.write(json.dumps(row) + "\n")
    return d


class TestFundingSettlerBasic(unittest.TestCase):

    def _inventory(self, position: float, venue: str = "bybit") -> InventoryBook:
        inv = InventoryBook()
        inv.ensure((venue,))
        inv.base[venue] = position
        inv.cash[venue] = 0.0
        return inv

    # ── Loading ──────────────────────────────────────────────────────────────

    def test_loads_records_from_jsonl(self):
        d = _make_dir([
            {"venue": "BYBIT", "funding_ts_ms": 1_000_000, "rate": "0.0001",
             "mark_price": "100.0", "settlement_history": True},
            {"venue": "BINANCE", "funding_ts_ms": 1_000_000, "rate": "0.0001",
             "mark_price": "100.0", "settlement_history": True},
        ])
        s = FundingSettler(d, ("bybit", "binance"))
        self.assertEqual(s.loaded_records, 2)

    def test_unknown_venue_skipped(self):
        d = _make_dir([
            {"venue": "OKX", "funding_ts_ms": 1_000_000, "rate": "0.0001",
             "mark_price": "100.0"},
        ])
        s = FundingSettler(d, ("bybit",))
        self.assertEqual(s.loaded_records, 0)

    def test_date_filter_excludes_out_of_window(self):
        # ts 0 = 1970-01-01, well before start_date 2022-01-13
        d = _make_dir([
            {"venue": "BYBIT", "funding_ts_ms": 0, "rate": "0.0001",
             "mark_price": "100.0"},
        ])
        s = FundingSettler(d, ("bybit",), start_date="2022-01-13")
        self.assertEqual(s.loaded_records, 0)

    # ── Settlement math ──────────────────────────────────────────────────────

    def test_long_pays_positive_rate(self):
        # position=10, mark=50, rate=0.001 → payment = 10*50*0.001 = 0.5
        d = _make_dir([
            {"venue": "BYBIT", "funding_ts_ms": 1_000, "rate": "0.001",
             "mark_price": "50.0"},
        ])
        s = FundingSettler(d, ("bybit",))
        inv = self._inventory(10.0)
        books = {"bybit": _BookStub(50.0)}

        s.settle(1.0, inv, books)

        self.assertAlmostEqual(s.funding_paid, 0.5, places=9)
        self.assertAlmostEqual(inv.cash["bybit"], -0.5, places=9)

    def test_short_receives_positive_rate(self):
        # position=-5, mark=100, rate=0.0001 → payment = -5*100*0.0001 = -0.05
        d = _make_dir([
            {"venue": "BYBIT", "funding_ts_ms": 1_000, "rate": "0.0001",
             "mark_price": "100.0"},
        ])
        s = FundingSettler(d, ("bybit",))
        inv = self._inventory(-5.0)
        books = {"bybit": _BookStub(100.0)}

        s.settle(1.0, inv, books)

        self.assertAlmostEqual(s.funding_paid, -0.05, places=9)
        self.assertAlmostEqual(inv.cash["bybit"], 0.05, places=9)

    def test_zero_position_no_payment(self):
        d = _make_dir([
            {"venue": "BYBIT", "funding_ts_ms": 1_000, "rate": "0.001",
             "mark_price": "50.0"},
        ])
        s = FundingSettler(d, ("bybit",))
        inv = self._inventory(0.0)
        books = {"bybit": _BookStub(50.0)}

        s.settle(1.0, inv, books)

        self.assertAlmostEqual(s.funding_paid, 0.0, places=9)

    def test_future_settlement_not_applied(self):
        # settlement at t=10s, settle called with now=5s
        d = _make_dir([
            {"venue": "BYBIT", "funding_ts_ms": 10_000, "rate": "0.001",
             "mark_price": "50.0"},
        ])
        s = FundingSettler(d, ("bybit",))
        inv = self._inventory(1.0)
        books = {"bybit": _BookStub(50.0)}

        s.settle(5.0, inv, books)

        self.assertAlmostEqual(s.funding_paid, 0.0, places=9)

    def test_settlement_applied_exactly_at_boundary(self):
        # settlement at exactly now_s=1.0 → must be applied
        d = _make_dir([
            {"venue": "BYBIT", "funding_ts_ms": 1_000, "rate": "0.001",
             "mark_price": "100.0"},
        ])
        s = FundingSettler(d, ("bybit",))
        inv = self._inventory(1.0)
        books = {"bybit": _BookStub(100.0)}

        s.settle(1.0, inv, books)

        self.assertAlmostEqual(s.funding_paid, 0.1, places=9)

    def test_no_double_settlement(self):
        # Call settle twice — second call must not re-apply the same tick
        d = _make_dir([
            {"venue": "BYBIT", "funding_ts_ms": 1_000, "rate": "0.001",
             "mark_price": "100.0"},
        ])
        s = FundingSettler(d, ("bybit",))
        inv = self._inventory(1.0)
        books = {"bybit": _BookStub(100.0)}

        s.settle(1.0, inv, books)
        s.settle(2.0, inv, books)   # no new ticks

        self.assertAlmostEqual(s.funding_paid, 0.1, places=9)

    def test_multiple_settlements_accumulate(self):
        # Three ticks: 1s, 5s, 9s; settle at 10s → all three applied
        d = _make_dir([
            {"venue": "BYBIT", "funding_ts_ms": 1_000, "rate": "0.001", "mark_price": "100.0"},
            {"venue": "BYBIT", "funding_ts_ms": 5_000, "rate": "0.001", "mark_price": "100.0"},
            {"venue": "BYBIT", "funding_ts_ms": 9_000, "rate": "0.001", "mark_price": "100.0"},
        ])
        s = FundingSettler(d, ("bybit",))
        inv = self._inventory(1.0)
        books = {"bybit": _BookStub(100.0)}

        s.settle(10.0, inv, books)

        self.assertAlmostEqual(s.funding_paid, 0.3, places=9)

    # ── Mark-price fallback ───────────────────────────────────────────────────

    def test_falls_back_to_book_mid_when_no_mark_in_record(self):
        d = _make_dir([
            {"venue": "BYBIT", "funding_ts_ms": 1_000, "rate": "0.001"},
        ])
        s = FundingSettler(d, ("bybit",))
        inv = self._inventory(2.0)
        books = {"bybit": _BookStub(200.0)}   # book mid = 200

        s.settle(1.0, inv, books)

        # 2 * 200 * 0.001 = 0.4
        self.assertAlmostEqual(s.funding_paid, 0.4, places=9)

    def test_no_payment_when_mark_zero_and_no_book(self):
        # No mark_price in record, no book → fallback = 0 → payment = 0
        d = _make_dir([
            {"venue": "BYBIT", "funding_ts_ms": 1_000, "rate": "0.001"},
        ])
        s = FundingSettler(d, ("bybit",))
        inv = self._inventory(1.0)
        books = {}

        s.settle(1.0, inv, books)

        self.assertAlmostEqual(s.funding_paid, 0.0, places=9)

    # ── funding_ts_ns support ─────────────────────────────────────────────────

    def test_funding_ts_ns_parsed_correctly(self):
        # 1_000_000_000 ns = 1.0 s
        d = _make_dir([
            {"venue": "BYBIT", "funding_ts_ns": 1_000_000_000, "rate": "0.001",
             "mark_price": "100.0"},
        ])
        s = FundingSettler(d, ("bybit",))
        inv = self._inventory(1.0)
        books = {"bybit": _BookStub(100.0)}

        s.settle(1.0, inv, books)

        self.assertAlmostEqual(s.funding_paid, 0.1, places=9)


if __name__ == "__main__":
    unittest.main()
