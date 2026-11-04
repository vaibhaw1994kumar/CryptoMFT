"""MakerConfig.opposite_cross_fills: a resting order fills in full, at its own price, as a maker fill, once the opposite best quote
reaches it (best ask <= our bid, best bid >= our ask), as in hftbacktest's exchange models (probe cross-check 2026-11-03). Opt-in:
the default is off and leaves every result unchanged."""
from __future__ import annotations

import tempfile
import unittest
import warnings
from pathlib import Path

from new_backtest.arb_strategy import MAKER_TAKER, ArbConfig
from new_backtest.backtester import ArbBacktestConfig, CrossVenueArbBacktester
from new_backtest.fill_engine import MakerConfig
from new_backtest.models import Side

from synthetic_days import gen
from test_fill_engine_queue import _book, _executor, _place

ON = MakerConfig(cancel_model="proportional", opposite_cross_fills=True)


class OppositeCrossFillTests(unittest.TestCase):
    def test_off_by_default_and_off_means_no_fill(self):
        self.assertFalse(MakerConfig().opposite_cross_fills)
        ex = _executor(MakerConfig(cancel_model="proportional"))
        book = _book(bids=[(100.0, 50.0)], asks=[(101.0, 50.0)])
        o = _place(ex, book, price=100.0, size=2.0)
        book.apply_delta("ask", 100.0, 5.0, "modify", 2.0)             # an ask arrives at our bid
        self.assertEqual(ex.on_opposite_cross("bybit", book, 2.0, 0.0, event_ts=2.0), [])
        self.assertEqual(o.filled_qty, 0.0)

    def test_a_bid_fills_in_full_at_its_price_when_the_ask_reaches_it(self):
        ex = _executor(ON)
        book = _book(bids=[(100.0, 50.0)], asks=[(101.0, 50.0)])
        o = _place(ex, book, price=100.0, size=2.0)
        self.assertEqual(o.queue_ahead, 50.0)                            # back of a 50 queue: no print reached us
        book.apply_delta("ask", 101.0, 0.0, "delete", 2.0)
        self.assertEqual(ex.on_opposite_cross("bybit", book, 2.0, 0.0, event_ts=2.0), [])   # ask 102: not reached
        book.apply_delta("ask", 99.5, 3.0, "modify", 3.0)               # through our bid (the book's uncross drops our level)
        fills = ex.on_opposite_cross("bybit", book, 3.0, -0.5, event_ts=3.0)
        self.assertEqual(len(fills), 1)
        f = fills[0]
        self.assertEqual((f.qty, f.price, f.cause, f.is_taker, f.side), (2.0, 100.0, "opposite_cross", False, "buy"))
        self.assertAlmostEqual(f.fee, 100.0 * 2.0 * -0.5 / 10_000)
        self.assertEqual((o.cross_fill_qty, o.queue_ahead, o.is_live), (2.0, 0.0, False))
        self.assertEqual(ex.on_opposite_cross("bybit", book, 4.0, 0.0, event_ts=4.0), [])    # filled once only

    def test_an_ask_fills_when_the_bid_reaches_it(self):
        ex = _executor(ON)
        book = _book(bids=[(100.0, 50.0)], asks=[(101.0, 50.0)])
        o = _place(ex, book, side=Side.ASK, price=101.0, size=1.0)
        book.apply_delta("bid", 101.0, 4.0, "modify", 2.0)
        fills = ex.on_opposite_cross("bybit", book, 2.0, 0.0, event_ts=2.0)
        self.assertEqual([(f.side, f.qty, f.price) for f in fills], [("sell", 1.0, 101.0)])
        self.assertFalse(o.is_live)

    def test_an_event_executed_before_the_order_arrived_cannot_fill_it(self):
        ex = _executor(ON)
        book = _book(bids=[(100.0, 50.0)], asks=[(101.0, 50.0)])
        o = _place(ex, book, price=100.0, size=2.0)
        o.arrival_ts = 5.0
        book.apply_delta("ask", 100.0, 5.0, "modify", 3.0)
        self.assertEqual(ex.on_opposite_cross("bybit", book, 3.0, 0.0, event_ts=4.0), [])   # executed at 4, we arrive at 5
        fills = ex.on_opposite_cross("bybit", book, 6.0, 0.0, event_ts=6.0)                 # still crossed after arrival
        self.assertEqual([f.cause for f in fills], ["opposite_cross"])

    def test_other_venues_and_orders_not_yet_active_are_left_alone(self):
        ex = _executor(ON)
        book = _book(bids=[(100.0, 50.0)], asks=[(101.0, 50.0)])
        o = _place(ex, book, price=100.0, size=2.0, now=10.0)          # active from t = 10
        other = _book(venue="binance", bids=[(100.0, 50.0)], asks=[(100.0, 1.0)])
        self.assertEqual(ex.on_opposite_cross("binance", other, 11.0, 0.0, event_ts=11.0), [])
        book.apply_delta("ask", 100.0, 5.0, "modify", 5.0)
        self.assertEqual(ex.on_opposite_cross("bybit", book, 5.0, 0.0, event_ts=5.0), [])
        self.assertTrue(o.is_live)


class OppositeCrossBacktestTests(unittest.TestCase):
    @staticmethod
    def _cfg(root, out, on):
        return ArbBacktestConfig(
            data_root=str(root), venues=("bybit", "binance"), pair="BTCUSDT", start_date="2026-10-01", end_date="2026-10-01",
            latency_preset="colocated_both", bybit_fee_preset="bybit_perp_mm", binance_fee_preset="binance_perp_vip0",
            maker=MakerConfig(min_queue_ahead=0.5, cancel_model="proportional", opposite_cross_fills=on),
            arb=ArbConfig(mode=MAKER_TAKER, maker_max_age_s=10.0), output_dir=str(out))

    def test_the_switch_reaches_the_backtester_and_is_listed_only_when_on(self):
        with tempfile.TemporaryDirectory() as d, warnings.catch_warnings():
            warnings.simplefilter("ignore")
            root = Path(d) / "data"
            gen(root, ndays=1, seed=3, session_s=15.0)
            off = CrossVenueArbBacktester(self._cfg(root, Path(d) / "off", False)).run()
            on = CrossVenueArbBacktester(self._cfg(root, Path(d) / "on", True)).run()
        self.assertNotIn("opposite_cross_fills", off.model_settings)
        self.assertTrue(on.model_settings["opposite_cross_fills"])


if __name__ == "__main__":
    unittest.main()
