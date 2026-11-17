"""Owner 16 Nov, Order 3 B: arb.close_pairs = reduce_only_quote (off by default, NOT in v1_10). While a pair is open, ALSO quote the opposite maker side for at most the open size, by the same edge formula in the reducing
direction; its hedge closes the other leg, reduce-only. It never opens risk and never goes beyond flat. SYNTHETIC books, tests only: nothing here is a profit figure."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb.simulation.order_book import float_mirror
from crypto_stat_arb.simulation.venue_state import VenueState
from crypto_stat_arb.strategies.arb_strategy import ArbConfig, CrossVenueArbStrategy, InventoryBook, PendingExposure

NOW = 100.0


def venues():
    return {"bybit": SimpleNamespace(fees=SimpleNamespace(maker_bps=-0.5, taker_bps=5.5)), "binance": SimpleNamespace(fees=SimpleNamespace(maker_bps=2.0, taker_bps=5.0))}


def strategy(**kw):
    cfg = dict(mode="maker_taker", maker_venues=("bybit",), profile="perp_perp_maker_taker_v1", entry_hedge_price="vwap", max_clip_base=1.5, max_position_base=2.0, max_venue_imbalance_base=5.0,
               net_ceiling_multiple=2.0, count_pending_exposure=True, depth_utilisation=1.0, max_walk_bps=5.0, safety_buffer_bps=0.5, min_net_edge_bps=1.0, max_touch_age_s=60.0)
    cfg.update(kw)
    return CrossVenueArbStrategy("SOLUSDT", venues(), ArbConfig(**cfg))


def books(open_edge=False, close_edge=False):
    """bybit (maker): bid 100.20 / ask 100.30. binance (hedge), a tight book: with `open_edge` its bid is 100.28 (a bybit BID can rest at an edge, the hedge SELLS there); with `close_edge` its ask is 100.20 (a bybit ASK can
    rest at an edge, the hedge BUYS there); with neither it sits at 99.0 / 99.05 (no edge either way). The two edges cannot coexist (they would cross the hedge book)."""
    maker = float_mirror("bybit", "SOLUSDT", {100.20: 10.0, 100.19: 10.0}, {100.30: 10.0, 100.31: 10.0}, NOW)
    if open_edge:
        hedge = float_mirror("binance", "SOLUSDT", {100.28: 10.0, 100.27: 10.0}, {100.33: 10.0, 100.34: 10.0}, NOW)
    elif close_edge:
        hedge = float_mirror("binance", "SOLUSDT", {100.15: 10.0, 100.14: 10.0}, {100.20: 10.0, 100.21: 10.0}, NOW)
    else:
        hedge = float_mirror("binance", "SOLUSDT", {99.0: 10.0, 98.99: 10.0}, {99.05: 10.0, 99.06: 10.0}, NOW)
    return {"bybit": maker, "binance": hedge}


def inv(bybit=0.0, binance=0.0):
    i = InventoryBook()
    i.ensure(("bybit", "binance"))
    i.base["bybit"], i.base["binance"] = bybit, binance
    return i


def evaluate(strat, bks, inventory, pending=None, live=0):
    states = {v: VenueState(v) for v in bks}
    return strat.evaluate(bks, states, inventory, NOW, live_maker_orders=live, pending=pending)


class Strategy(unittest.TestCase):
    def test_off_by_default_and_an_open_pair_is_not_closed(self):
        self.assertEqual(ArbConfig().close_pairs, "off")
        self.assertIsNone(evaluate(strategy(), books(close_edge=True), inv(1.0, -1.0), PendingExposure()))             # only the closing direction clears: nothing is quoted while the switch is off

    def test_a_long_on_the_maker_venue_is_closed_by_a_sell_for_at_most_the_open_size(self):
        sig = evaluate(strategy(close_pairs="reduce_only_quote"), books(close_edge=True), inv(1.0, -1.0), PendingExposure())
        self.assertIsNotNone(sig)
        self.assertTrue(sig.close_pair)
        self.assertEqual((sig.legs[0].venue, sig.legs[0].side, sig.hedge_side, sig.hedge_venue), ("bybit", "sell", "buy", "binance"))
        self.assertLessEqual(sig.qty, 1.0 + 1e-12)                                    # the clip is 1.5: the open size caps it
        self.assertAlmostEqual(sig.qty, 1.0)

    def test_the_closing_quote_has_the_edge_of_the_ordinary_ask_quote_of_the_same_size(self):
        close = evaluate(strategy(close_pairs="reduce_only_quote"), books(close_edge=True), inv(1.5, -1.5), PendingExposure())      # open size 1.5 = the clip
        ordinary_strat = strategy(maker_passive_side="ask")
        bks = books(close_edge=True)
        ordinary = ordinary_strat._evaluate_maker_taker_ask("bybit", "binance", bks, {v: VenueState(v) for v in bks}, inv(), NOW)
        self.assertIsNotNone(close)
        self.assertIsNotNone(ordinary)
        for f in ("qty", "sell_price", "buy_price", "net_edge_bps", "conditional_edge_bps", "naive_edge_bps", "gross_edge_bps"):
            self.assertEqual(getattr(close, f), getattr(ordinary, f), f)                  # the same formula: fees, S1 hedge move, VWAP hedge, funding
        self.assertFalse(ordinary.close_pair)

    def test_a_short_on_the_maker_venue_is_closed_by_a_buy(self):
        sig = evaluate(strategy(close_pairs="reduce_only_quote"), books(open_edge=True), inv(-0.6, 0.6), PendingExposure())
        self.assertIsNotNone(sig)
        self.assertTrue(sig.close_pair)
        self.assertEqual((sig.legs[0].side, sig.hedge_side), ("buy", "sell"))
        self.assertAlmostEqual(sig.qty, 0.6)

    def test_it_never_opens_risk_whatever_the_position(self):
        strat = strategy(close_pairs="reduce_only_quote")
        for position in (-2.0, -0.5, 0.5, 2.0):
            sig = evaluate(strat, books(open_edge=position < 0, close_edge=position > 0), inv(position, -position), PendingExposure())
            self.assertIsNotNone(sig, position)
            if sig.close_pair:
                self.assertEqual(sig.legs[0].side, "sell" if position > 0 else "buy", position)          # opposite to the position: it reduces
                self.assertLessEqual(sig.qty, abs(position) + 1e-12, position)                           # and never by more than is open
        self.assertIsNone(evaluate(strat, books(close_edge=True), inv(0.0, 0.0), PendingExposure()))     # nothing open: nothing to close, and the ordinary rules (bid only) find no edge here

    def test_a_resting_closing_quote_is_not_quoted_again_and_the_opening_side_is_still_evaluated(self):
        strat = strategy(close_pairs="reduce_only_quote")
        pending = PendingExposure()
        pending.closing["bybit"] = 1.0                                               # a closing quote already rests for the whole open size
        self.assertIsNone(evaluate(strat, books(close_edge=True), inv(1.0, -1.0), pending))              # nothing left to close, no opening edge
        opening = evaluate(strat, books(open_edge=True), inv(1.0, -1.0), pending)       # an opening bid edge exists: it is quoted as always (the closing side is already resting)
        self.assertIsNotNone(opening)
        self.assertFalse(opening.close_pair)
        self.assertEqual(opening.legs[0].side, "buy")
        part = PendingExposure()
        part.closing["bybit"] = 0.4
        sig = evaluate(strat, books(close_edge=True), inv(1.0, -1.0), part)
        self.assertAlmostEqual(sig.qty, 0.6)                                         # what a closing quote already rests for is taken off

    def test_the_opening_rules_are_unchanged_when_nothing_is_open(self):
        a = evaluate(strategy(), books(open_edge=True), inv(), PendingExposure())
        b = evaluate(strategy(close_pairs="reduce_only_quote"), books(open_edge=True), inv(), PendingExposure())
        self.assertIsNotNone(a)
        for f in ("qty", "buy_price", "sell_price", "net_edge_bps", "conditional_edge_bps"):
            self.assertEqual(getattr(a, f), getattr(b, f), f)
        self.assertFalse(b.close_pair)

    def test_the_switch_is_validated(self):
        with self.assertRaises(ValueError):
            strategy(close_pairs="maybe")
        with self.assertRaises(ValueError):
            strategy(close_pairs="reduce_only_quote", mode="taker_taker")
        with self.assertRaises(ValueError):
            strategy(close_pairs="reduce_only_quote", count_pending_exposure=False)


class Shim(unittest.TestCase):
    def test_a_resting_reduce_only_quote_is_counted_as_closing_not_as_pending_risk_or_a_live_opening_quote(self):
        from decimal import Decimal
        from crypto_stat_arb.models import ChildOrder, OrderState, Side
        from crypto_stat_arb.strategies.arb_strategy import ArbSessionStrategy
        shim = ArbSessionStrategy.__new__(ArbSessionStrategy)
        def child(oid, reduce_only):
            return ChildOrder(order_id=oid, parent_id="p", opportunity_id="o", venue="BYBIT", instrument_id="SOL", side=Side.SELL, quantity=Decimal("1.0"), limit_price=Decimal("100"), state=OrderState.ACKNOWLEDGED,
                              post_only=True, reduce_only=reduce_only, filled_quantity=Decimal("0.25"))
        plan = SimpleNamespace(children=[child("close", True), child("open", False)])
        shim.oms = SimpleNamespace(plans={"p": plan})
        shim._feed_name = {"BYBIT": "bybit"}
        pending, makers = shim.pending_and_live_makers()
        self.assertEqual(pending.closing, {"bybit": 0.75})
        self.assertEqual(makers, 1)                                                  # only the opening quote counts against maker_max_live_orders
        self.assertEqual(pending.sells.get("bybit"), 0.75)                           # and only the opening quote is pending sell exposure


if __name__ == "__main__":
    unittest.main()
