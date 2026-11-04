"""ArbConfig.round_quotes_to_tick and ArbConfig.maker_venues (v1.1 step 9b / 9d): quotes sit on the venue's grid, bids rounded down; the maker venue can be restricted.
Opt-in; the defaults keep every result unchanged."""
from __future__ import annotations

import heapq
import unittest

from new_backtest.arb_strategy import MAKER_TAKER, ArbConfig, CrossVenueArbStrategy, InventoryBook
from new_backtest.config import BINANCE, BYBIT, make_venues
from new_backtest.order_book import OrderBook
from new_backtest.venue_state import VenueState

NOW = 1000.0


def _books(bybit=(100.00, 100.08), binance=(100.10, 100.11)):
    books, states = {}, {}
    for venue, (bid, ask) in ((BYBIT, bybit), (BINANCE, binance)):
        ob = OrderBook(venue, "SOLUSDT", delta_semantics=False)
        ob._bids = {bid: 50.0, round(bid - 0.001, 6): 50.0}
        ob._asks = {ask: 50.0, round(ask + 0.001, 6): 50.0}
        ob._bid_heap = [-bid, -round(bid - 0.001, 6)]
        ob._ask_heap = [ask, round(ask + 0.001, 6)]
        heapq.heapify(ob._bid_heap); heapq.heapify(ob._ask_heap)
        ob._bid_conf = {bid: NOW}; ob._ask_conf = {ask: NOW}
        ob.last_update_ts = ob.last_snapshot_ts = NOW
        books[venue], states[venue] = ob, VenueState(venue)
    return books, states


def _strategy(**kw):
    specs = make_venues("neutral")
    cfg = ArbConfig(mode=MAKER_TAKER, max_spread_bps=1000.0, **kw)
    return CrossVenueArbStrategy("SOLUSDT", {BYBIT: specs[BYBIT], BINANCE: specs[BINANCE]}, cfg)


def _inv(books):
    inv = InventoryBook(); inv.ensure(tuple(books))
    return inv


class TickRoundingTests(unittest.TestCase):
    def test_default_quote_is_off_grid_and_unchanged(self):
        books, states = _books()
        sig = _strategy().evaluate(books, states, _inv(books), NOW)
        self.assertIsNotNone(sig)
        q = sig.legs[0].limit_price
        self.assertGreater(abs(q / 0.001 - round(q / 0.001)), 1e-6)            # an unrounded edge price, off the 0.001 grid

    def test_bid_rounds_down_onto_the_grid(self):
        books, states = _books()
        off = _strategy().evaluate(books, states, _inv(books), NOW)
        on = _strategy(round_quotes_to_tick=True, venue_ticks={BYBIT: 0.001, BINANCE: 0.001}).evaluate(books, states, _inv(books), NOW)
        self.assertIsNotNone(on)
        q = on.legs[0].limit_price
        self.assertLess(abs(q / 0.001 - round(q / 0.001)), 1e-6)
        self.assertLessEqual(q, off.legs[0].limit_price + 1e-12)               # never above the unrounded edge price
        self.assertGreater(off.legs[0].limit_price - q, -1e-12)
        self.assertLess(off.legs[0].limit_price - q, 0.001)                    # and less than one tick below it

    def test_a_quote_that_rounds_onto_the_touch_is_refused_not_joined(self):
        books, states = _books(bybit=(100.00, 100.50), binance=(100.20, 100.21))
        strat = _strategy(round_quotes_to_tick=True, venue_ticks={BYBIT: 0.5, BINANCE: 0.5})       # a coarse grid: the quote falls back onto the touch
        sig = strat._evaluate_maker_taker(BYBIT, BINANCE, books, states, _inv(books), NOW)
        self.assertIsNone(sig)
        self.assertEqual(strat.rejects.get("tick_rounding_no_improve", 0), 1)

    def test_missing_tick_is_an_error(self):
        with self.assertRaises(ValueError):
            _strategy(round_quotes_to_tick=True, venue_ticks={BYBIT: 0.001})

    def test_maker_venues_restricts_where_a_quote_rests(self):
        books, states = _books()
        both = _strategy(maker_passive_side="both")
        only = _strategy(maker_passive_side="both", maker_venues=(BYBIT,))
        for strat in (both, only):
            sig = strat.evaluate(books, states, _inv(books), NOW)
            if strat is only and sig is not None:
                self.assertEqual(sig.maker_venue, BYBIT)
        sig = _strategy(maker_venues=(BINANCE,)).evaluate(books, states, _inv(books), NOW)      # the only edge is a bid on Bybit: nothing rests on Binance
        self.assertIsNone(sig)

    def test_unknown_maker_venue_is_an_error(self):
        with self.assertRaises(ValueError):
            _strategy(maker_venues=("nowhere",))


if __name__ == "__main__":
    unittest.main()
