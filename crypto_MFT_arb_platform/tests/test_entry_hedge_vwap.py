"""Owner 13 Nov, edge calibration item 1: with arb.entry_hedge_price "vwap" the entry edge and the quote price use the VWAP the hedge venue's book gives for the size the quote will have (the same walk
quote_validity uses). With a size above the touch depth the net edge at entry equals the net edge quote_validity gives for the same book, price and size (no place-then-withdraw flapping); on the touch path it does not.
SYNTHETIC books, tests only: nothing here is a profit figure."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb.simulation.order_book import float_mirror
from crypto_stat_arb.simulation.venue_state import VenueState
from crypto_stat_arb.strategies.arb_strategy import ArbConfig, CrossVenueArbStrategy, InventoryBook

NOW = 100.0


def venues():
    return {"bybit": SimpleNamespace(fees=SimpleNamespace(maker_bps=-0.5, taker_bps=5.5)), "binance": SimpleNamespace(fees=SimpleNamespace(maker_bps=2.0, taker_bps=5.0))}


def strategy(entry="touch", **kw):
    cfg = dict(mode="maker_taker", maker_venues=("bybit",), profile="perp_perp_maker_taker_v1", entry_hedge_price=entry, max_clip_base=2.0, max_position_base=2.0,
               depth_utilisation=1.0, max_walk_bps=5.0, safety_buffer_bps=0.5, min_net_edge_bps=1.0, max_touch_age_s=60.0)
    cfg.update(kw)
    return CrossVenueArbStrategy("SOLUSDT", venues(), ArbConfig(**cfg))


def books(hedge_bids):
    """Maker venue (bybit): bid 100.20 / ask 100.30. Hedge venue (binance): the given bid levels, asks far above."""
    maker = float_mirror("bybit", "SOLUSDT", {100.20: 10.0, 100.19: 10.0}, {100.30: 10.0, 100.31: 10.0}, NOW)
    hedge = float_mirror("binance", "SOLUSDT", hedge_bids, {101.00: 10.0, 101.01: 10.0}, NOW)
    return {"bybit": maker, "binance": hedge}


THIN_TOUCH = {100.30: 0.5, 100.29: 2.0, 100.28: 5.0}       # the touch holds 0.5; a 2.0 clip walks into the next level
DEEP_TOUCH = {100.30: 10.0, 100.29: 10.0}                    # the touch holds the whole clip: VWAP == touch


def signal_for(strat, bks):
    states = {v: VenueState(v) for v in bks}
    return strat._evaluate_maker_taker("bybit", "binance", bks, states, InventoryBook(), NOW)


class EntryHedgeVwap(unittest.TestCase):
    def test_size_above_the_touch_depth_gives_the_same_net_edge_at_entry_and_in_quote_validity(self):
        strat, bks = strategy("vwap"), books(THIN_TOUCH)
        sig = signal_for(strat, bks)
        self.assertIsNotNone(sig)
        self.assertGreater(sig.qty, 0.5)                                         # more than the touch shows: the VWAP is not the touch
        valid, net, why = strat.quote_validity("bybit", "buy", sig.buy_price, sig.qty, bks, NOW)
        self.assertEqual(why, "ok")
        self.assertAlmostEqual(sig.conditional_edge_bps, net, places=10)         # raw net values, no hysteresis in between
        self.assertTrue(valid)

    def test_the_touch_path_disagrees_with_quote_validity_so_the_test_above_can_fail(self):
        strat, bks = strategy("touch"), books(THIN_TOUCH)
        sig = signal_for(strat, bks)
        self.assertIsNotNone(sig)
        _, net, _ = strat.quote_validity("bybit", "buy", sig.buy_price, sig.qty, bks, NOW)
        self.assertGreater(sig.conditional_edge_bps - net, 0.5)                  # entry priced the hedge at the touch, validity at the VWAP: a mismatch that would flap

    def test_the_quote_price_is_built_from_the_vwap_not_the_touch(self):
        touch_sig = signal_for(strategy("touch"), books(THIN_TOUCH))
        vwap_sig = signal_for(strategy("vwap"), books(THIN_TOUCH))
        self.assertLess(vwap_sig.buy_price, touch_sig.buy_price)                 # a worse hedge price lowers what a bid may rest at
        self.assertEqual(vwap_sig.sell_price, 100.30)                            # the signal still reports the touch as its hedge reference

    def test_when_the_touch_holds_the_whole_size_vwap_and_touch_give_the_same_signal(self):
        a, b = signal_for(strategy("touch"), books(DEEP_TOUCH)), signal_for(strategy("vwap"), books(DEEP_TOUCH))
        self.assertIsNotNone(a)
        for f in ("qty", "buy_price", "sell_price", "net_edge_bps", "conditional_edge_bps", "naive_edge_bps", "gross_edge_bps"):
            self.assertEqual(getattr(a, f), getattr(b, f), f)

    def test_a_size_the_hedge_book_cannot_fill_is_refused_not_priced_at_the_touch(self):
        strat = strategy("vwap", depth_utilisation=100.0)                        # a (mis)configured utilisation: the size exceeds the visible book
        bks = books({100.30: 0.2, 100.29: 0.2})
        self.assertIsNone(signal_for(strat, bks))
        self.assertGreaterEqual(strat.rejects.get("no_depth", 0), 1)

    def test_ask_side_mirror(self):
        strat = strategy("vwap", maker_passive_side="ask")
        maker = float_mirror("bybit", "SOLUSDT", {100.20: 10.0}, {100.30: 10.0}, NOW)
        hedge = float_mirror("binance", "SOLUSDT", {99.0: 10.0}, {100.20: 0.5, 100.21: 2.0, 100.22: 5.0}, NOW)     # the hedge BUYS from the asks
        bks = {"bybit": maker, "binance": hedge}
        states = {v: VenueState(v) for v in bks}
        sig = strat._evaluate_maker_taker_ask("bybit", "binance", bks, states, InventoryBook(), NOW)
        self.assertIsNotNone(sig)
        self.assertGreater(sig.qty, 0.5)
        valid, net, why = strat.quote_validity("bybit", "sell", sig.legs[0].limit_price, sig.qty, bks, NOW)
        self.assertEqual(why, "ok")
        self.assertAlmostEqual(sig.conditional_edge_bps, net, places=10)

    def test_the_switch_is_validated_and_never_applies_to_legacy_v0(self):
        with self.assertRaises(ValueError):
            ArbConfig(entry_hedge_price="mid")
        with self.assertRaises(ValueError):
            ArbConfig(profile="legacy_v0", entry_hedge_price="vwap")
        self.assertEqual(ArbConfig().entry_hedge_price, "touch")

    def test_only_v1_9_sets_it(self):
        from new_backtest.profiles import load_profile
        for name in ("perp_perp_maker_taker_v1", "perp_perp_maker_taker_v1_1", "perp_perp_maker_taker_v1_2", "perp_perp_maker_taker_v1_3", "perp_perp_maker_taker_v1_4", "perp_perp_maker_taker_v1_7", "perp_perp_maker_taker_v1_8"):
            self.assertEqual(load_profile(name)["arb"].get("entry_hedge_price", "touch"), "touch", name)
        self.assertEqual(load_profile("perp_perp_maker_taker_v1_9")["arb"]["entry_hedge_price"], "vwap")


if __name__ == "__main__":
    unittest.main()
