"""S1 (owner 9 Nov): the adverse-selection table, fitted from market-data label rows with shrinkage toward the parent cell; lookups fall back to the longest existing prefix; the table round-trips through JSON."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import copy
import hashlib

from crypto_stat_arb.strategies.edge import AdverseFeatures, AdverseTable, fit_adverse_table
from new_backtest.arb_strategy import MAKER_TAKER, ArbConfig, CrossVenueArbStrategy, InventoryBook
from new_backtest.config import BINANCE, BYBIT, make_venues
from new_backtest.order_book import OrderBook
from new_backtest.profiles import ProfileError, apply_profile, load_profile
from new_backtest.venue_state import VenueState


def row(side="bid", depth=0, move1=-1.0, vol=2.0, flow=0.0, spread=1.0, move5=None, move30=None):
    return {"side": side, "depth_ticks": depth, "vol_bps": vol, "flow": flow, "spread_bps": spread, "move_1s": move1, "move_5s": move5, "move_30s": move30}


class FitTests(unittest.TestCase):
    def test_k_zero_gives_the_raw_cell_means_and_the_side_mean_is_the_root(self):
        rows = [row(depth=0, move1=1.0), row(depth=0, move1=3.0), row(depth=5, move1=-4.0), row(depth=5, move1=-6.0), row("ask", depth=0, move1=-2.0)]
        t = fit_adverse_table(rows, dims=("depth",), depth_edges=(1, 4), shrink_k=0.0)
        self.assertAlmostEqual(t.expected_move_bps("bid", depth_ticks=0), 2.0)             # bucket 0: mean of 1 and 3
        self.assertAlmostEqual(t.expected_move_bps("bid", depth_ticks=9), -5.0)            # bucket 2: mean of -4 and -6
        self.assertAlmostEqual(t.side_mean["bid"]["1"], (1 + 3 - 4 - 6) / 4)
        self.assertAlmostEqual(t.expected_move_bps("ask"), -2.0)

    def test_shrinkage_pulls_a_thin_cell_toward_its_parent_by_n_over_n_plus_k(self):
        rows = [row(depth=0, move1=0.0)] * 9 + [row(depth=5, move1=-10.0)]                  # parent (side) mean = -1.0; deep cell n = 1, raw -10
        t = fit_adverse_table(rows, dims=("depth",), depth_edges=(1, 4), shrink_k=3.0)
        self.assertAlmostEqual(t.expected_move_bps("bid", depth_ticks=5), (1 * -10.0 + 3.0 * -1.0) / (1 + 3.0))      # -3.25
        self.assertAlmostEqual(t.expected_move_bps("bid", depth_ticks=0), (9 * 0.0 + 3.0 * -1.0) / (9 + 3.0))      # -0.25

    def test_two_dimensions_shrink_toward_the_first_and_an_unseen_cell_falls_back_to_its_prefix(self):
        rows = [row(depth=0, vol=1.0, move1=-2.0)] * 4 + [row(depth=0, vol=9.0, move1=-6.0)] * 4
        t = fit_adverse_table(rows, dims=("depth", "vol"), depth_edges=(1,), vol_edges=(5.0,), shrink_k=4.0)
        depth0 = (8 * -4.0 + 4.0 * -4.0) / 12.0                                              # parent: side mean -4, so the depth-0 cell is -4
        self.assertAlmostEqual(t.expected_move_bps("bid", depth_ticks=0, vol_bps=1.0), (4 * -2.0 + 4.0 * depth0) / 8.0)
        self.assertAlmostEqual(t.expected_move_bps("bid", depth_ticks=0, vol_bps=9.0), (4 * -6.0 + 4.0 * depth0) / 8.0)
        self.assertAlmostEqual(t.expected_move_bps("bid", depth_ticks=3, vol_bps=1.0), t.side_mean["bid"]["1"])         # depth bucket 1 was never seen: the side mean
        self.assertAlmostEqual(t.expected_move_bps("bid", depth_ticks=0, vol_bps=None), depth0)                           # an unknown volatility uses the depth cell

    def test_unresolved_labels_are_skipped_per_horizon(self):
        rows = [row(move1=-1.0, move5=-2.0), row(move1=-3.0, move5=None)]
        t = fit_adverse_table(rows, dims=(), shrink_k=0.0, horizons_s=(1.0, 5.0, 30.0))
        self.assertAlmostEqual(t.side_mean["bid"]["1"], -2.0)
        self.assertAlmostEqual(t.side_mean["bid"]["5"], -2.0)
        self.assertEqual(t.side_mean["bid"]["30"], 0.0)                                      # nothing resolved: zero, not an error

    def test_json_round_trip_keeps_every_answer(self):
        rows = [row(depth=d % 6, vol=float(d % 7), move1=-(d % 5) * 0.3) for d in range(200)]
        t = fit_adverse_table(rows, dims=("depth", "vol"), depth_edges=(1, 3), vol_edges=(2.0, 4.0), shrink_k=20.0, fitted_on={"days": ["2024-03-01"], "source": "MARKET_DATA_LABELS"})
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.json"
            t.save(path)
            u = AdverseTable.load(path)
            self.assertEqual(json.loads(path.read_text())["fitted_on"]["source"], "MARKET_DATA_LABELS")
        for depth in (0, 1, 2, 4, 9):
            for vol in (None, 1.0, 3.0, 8.0):
                self.assertEqual(t.expected_move_bps("ask", depth_ticks=depth, vol_bps=vol), u.expected_move_bps("ask", depth_ticks=depth, vol_bps=vol))

    def test_bad_arguments_are_refused(self):
        t = fit_adverse_table([row()], dims=("depth",))
        with self.assertRaises(ValueError):
            t.expected_move_bps("both")
        with self.assertRaises(ValueError):
            t.expected_move_bps("bid", horizon_s=2.0)
        with self.assertRaises(ValueError):
            fit_adverse_table([row()], dims=("depth", "depth"))
        with self.assertRaises(ValueError):
            fit_adverse_table([row()], dims=("colour",))


class ReachingLookupTests(unittest.TestCase):
    """A resting quote is filled by every sweep that reached it, so its estimate averages the depth buckets at or beyond its own."""

    def table(self):
        rows = [row(depth=0, move1=0.0)] * 100 + [row(depth=5, move1=-10.0)] * 100
        return fit_adverse_table(rows, dims=("depth",), depth_edges=(1, 4), shrink_k=0.0)

    def test_a_touch_quote_is_filled_by_the_deep_sweeps_too(self):
        t = self.table()
        self.assertAlmostEqual(t.expected_move_bps("bid", depth_ticks=0), 0.0)                       # the exact-bucket read: shallow prints only
        self.assertAlmostEqual(t.expected_move_reaching_bps("bid", quote_depth_ticks=0), -5.0)       # all prints at this side: half of them reached depth 5
        self.assertAlmostEqual(t.expected_move_reaching_bps("bid", quote_depth_ticks=5), -10.0)
        self.assertAlmostEqual(t.expected_move_reaching_bps("bid", quote_depth_ticks=2), -10.0)      # bucket 1 holds no prints: only the deep bucket reaches it

    def test_without_a_depth_dimension_it_is_the_plain_lookup(self):
        t = fit_adverse_table([row(vol=1.0, move1=-2.0), row(vol=9.0, move1=-6.0)], dims=("vol",), vol_edges=(5.0,), shrink_k=0.0)
        self.assertEqual(t.expected_move_reaching_bps("bid", quote_depth_ticks=3, vol_bps=9.0), t.expected_move_bps("bid", vol_bps=9.0))

    def test_the_other_dimensions_are_read_inside_each_bucket(self):
        rows = [row(depth=0, flow=0.0, move1=0.0)] * 50 + [row(depth=0, flow=9.0, move1=-4.0)] * 50 + [row(depth=5, flow=0.0, move1=-2.0)] * 50 + [row(depth=5, flow=9.0, move1=-8.0)] * 50
        t = fit_adverse_table(rows, dims=("depth", "flow"), depth_edges=(1, 4), flow_edges=(1.0,), shrink_k=0.0)
        self.assertAlmostEqual(t.expected_move_reaching_bps("bid", quote_depth_ticks=0, flow=9.0), (100 * -4.0 + 100 * -8.0) / 200)
        self.assertAlmostEqual(t.expected_move_reaching_bps("bid", quote_depth_ticks=0, flow=0.0), (100 * 0.0 + 100 * -2.0) / 200)


class FeatureTests(unittest.TestCase):
    def test_volatility_needs_five_changes_and_samples_once_per_second(self):
        f = AdverseFeatures()
        for i in range(5):                                      # five samples: four changes
            f.on_mid("B", 1000.0 + i + 0.2, 100.0 + i * i)
            f.on_mid("B", 1000.0 + i + 0.9, 999.0)              # same second: ignored
        self.assertIsNone(f.vol_bps("B", 100.0))
        f.on_mid("B", 1005.5, 100.0 + 25)
        ch = [1, 3, 5, 7, 9]
        m = sum(ch) / 5
        want = (sum((x - m) ** 2 for x in ch) / 4) ** 0.5 / 125.0 * 1e4
        self.assertAlmostEqual(f.vol_bps("B", 125.0), want)
        self.assertIsNone(f.vol_bps("OTHER", 125.0))

    def test_old_samples_leave_the_window(self):
        f = AdverseFeatures(vol_window_s=10.0)
        for i in range(30):
            f.on_mid("B", 2000.0 + i, 100.0 + (i % 2))
        self.assertLessEqual(len(f._mids["B"]), 11)

    def test_a_bid_is_hit_by_sell_prints_so_sell_volume_is_its_positive_flow(self):
        self.assertEqual(AdverseFeatures.side_flow(-5.0, "bid"), 5.0)       # signed volume - = sell aggressors
        self.assertEqual(AdverseFeatures.side_flow(-5.0, "ask"), -5.0)
        self.assertEqual(AdverseFeatures.side_flow(5.0, "ask"), 5.0)


NOW = 1000.0


def _books(bybit=(100.00, 100.08), binance=(100.10, 100.11)):
    import heapq
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


def _strategy(table=None, **kw):
    specs = make_venues("neutral")
    cfg = ArbConfig(mode=MAKER_TAKER, max_spread_bps=1000.0, adverse_table=table, venue_ticks={BYBIT: 0.001, BINANCE: 0.001}, **kw)
    return CrossVenueArbStrategy("SOLUSDT", {BYBIT: specs[BYBIT], BINANCE: specs[BINANCE]}, cfg)


def _inv(books):
    inv = InventoryBook(); inv.ensure(tuple(books))
    return inv


def _flat(move):
    rows = [row(side=s, depth=0, move1=move) for s in ("bid", "ask")] * 5
    return fit_adverse_table(rows, dims=("depth",), depth_edges=(1,), shrink_k=0.0)


class StrategyUsesTheTableTests(unittest.TestCase):
    def test_no_table_reads_the_profile_numbers_and_the_quote_is_unchanged(self):
        books, states = _books()
        s = _strategy(expected_hedge_move_bps_bid=-0.5, expected_hedge_move_bps_ask=-0.7)
        self.assertEqual(s._hedge_move_bps("bid", BYBIT, BINANCE, books), -0.5)
        self.assertEqual(s._hedge_move_bps("ask", BYBIT, BINANCE, books, 101.0), -0.7)
        self.assertEqual(s._quote_limit("bid", 100.1, 1.0), s._quote_limit("bid", 100.1, 1.0, -0.5))
        self.assertIsNone(s._adverse_features)

    def test_a_flat_table_moves_the_quote_exactly_as_the_same_number_would(self):
        books, states = _books()
        with_table = _strategy(_flat(-2.0)).evaluate(books, states, _inv(books), NOW)
        with_number = _strategy(expected_hedge_move_bps_bid=-2.0, expected_hedge_move_bps_ask=-2.0).evaluate(books, states, _inv(books), NOW)
        self.assertIsNotNone(with_table)
        self.assertEqual(with_table.legs[0].limit_price, with_number.legs[0].limit_price)
        self.assertEqual(with_table.net_edge_bps, with_number.net_edge_bps)             # the entry rule reads the same value as the quote price

    def test_the_entry_rule_reads_the_depth_the_quote_lands_on(self):
        rows = [row(depth=0, move1=0.0)] * 50 + [row(depth=3, move1=-30.0)] * 50
        t = fit_adverse_table(rows, dims=("depth",), depth_edges=(1, 3), shrink_k=0.0)
        books, states = _books()
        s = _strategy(t)
        s._observe_adverse_features(books, states, NOW)
        at_touch = s._hedge_move_bps("bid", BYBIT, BINANCE, books)                      # the price is built from the all-depth mean
        self.assertAlmostEqual(at_touch, -15.0)
        deep = s._hedge_move_bps("bid", BYBIT, BINANCE, books, 100.00 - 0.004)          # 4 ticks behind the touch
        self.assertAlmostEqual(deep, -30.0)
        inside = s._hedge_move_bps("bid", BYBIT, BINANCE, books, 100.00 + 0.002)        # better than the touch: depth 0, never negative
        self.assertAlmostEqual(inside, at_touch)

    def test_sell_prints_feed_a_bids_flow_and_buy_prints_an_asks(self):
        rows = ([row("bid", depth=0, flow=5.0, move1=-8.0)] * 40 + [row("bid", depth=0, flow=0.0, move1=0.0)] * 40
                + [row("ask", depth=0, flow=5.0, move1=-9.0)] * 40 + [row("ask", depth=0, flow=0.0, move1=-1.0)] * 40)
        t = fit_adverse_table(rows, dims=("depth", "flow"), depth_edges=(1,), flow_edges=(1.0,), shrink_k=0.0)
        books, states = _books()
        s = _strategy(t)
        states[BYBIT].on_trade(NOW - 0.5, 100.0, 5.0, -1)                                # a sell aggressor 0.5 s ago
        s._observe_adverse_features(books, states, NOW)
        self.assertAlmostEqual(s._hedge_move_bps("bid", BYBIT, BINANCE, books), -8.0)     # the sell flow is a bid's flow
        self.assertAlmostEqual(s._hedge_move_bps("ask", BYBIT, BINANCE, books), -1.0)     # for an ask it is the opposite direction: the calm cell
        states[BYBIT].on_trade(NOW - 0.2, 100.0, 12.0, 1)                                # then a larger buy
        s._observe_adverse_features(books, states, NOW)
        self.assertAlmostEqual(s._hedge_move_bps("ask", BYBIT, BINANCE, books), -9.0)

    def test_a_table_needs_declared_ticks_and_a_known_horizon(self):
        specs = make_venues("neutral")
        v = {BYBIT: specs[BYBIT], BINANCE: specs[BINANCE]}
        with self.assertRaises(ValueError):
            CrossVenueArbStrategy("SOLUSDT", v, ArbConfig(mode=MAKER_TAKER, adverse_table=_flat(-1.0)))
        with self.assertRaises(ValueError):
            CrossVenueArbStrategy("SOLUSDT", v, ArbConfig(mode=MAKER_TAKER, adverse_table=_flat(-1.0), adverse_horizon_s=7.0, venue_ticks={BYBIT: 0.001, BINANCE: 0.001}))


class ProfileTests(unittest.TestCase):
    def apply(self, doc, start="2024-03-11"):
        return apply_profile(doc, pair="SOLUSDT", start_date=start, end_date=start, data_root="", output_dir="")

    def test_v1_6_pins_the_table_and_v1_5_has_none(self):
        self.assertIsNone(self.apply(load_profile("perp_perp_maker_taker_v1_5")).arb.adverse_table)
        c = self.apply(load_profile("perp_perp_maker_taker_v1_6"))
        self.assertIsInstance(c.arb.adverse_table, AdverseTable)
        self.assertEqual(c.release_identity["adverse_table"]["days"][-1], "2024-03-07")

    def test_v1_6_differs_from_v1_5_only_by_the_table(self):
        a, b = load_profile("perp_perp_maker_taker_v1_5"), load_profile("perp_perp_maker_taker_v1_6")
        skip = {"name", "description", "adverse_table", "_sha256", "_path", "decision_status"}
        self.assertEqual({k: v for k, v in a.items() if k not in skip}, {k: v for k, v in b.items() if k not in skip})

    def test_an_edited_table_a_too_late_fit_and_a_fill_fitted_table_are_refused(self):
        doc = load_profile("perp_perp_maker_taker_v1_6")
        bad = copy.deepcopy(doc)
        bad["adverse_table"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ProfileError, "adverse_table"):
            self.apply(bad)
        early = copy.deepcopy(doc)
        early.pop("hedge_move_calibration", None)
        with self.assertRaisesRegex(ProfileError, "adverse_table.*strictly before"):
            self.apply(early, start="2024-03-05")
        with tempfile.TemporaryDirectory() as d:
            t = AdverseTable.load(Path(__file__).resolve().parents[1] / doc["adverse_table"]["file"])
            t.fitted_on = {**t.fitted_on, "source": "STRATEGY_FILLS"}
            path = Path(d) / "t.json"
            t.save(path)
            fills = copy.deepcopy(doc)
            fills["adverse_table"].update(file=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            with self.assertRaisesRegex(ProfileError, "market-data labels"):
                self.apply(fills)


def _two_horizon(m_short, m_long):
    rows = [{"side": s, "depth_ticks": 0, "vol_bps": 2.0, "flow": 0.0, "spread_bps": 1.0, "move_0.05s": m_short, "move_1s": m_long} for s in ("bid", "ask")] * 5
    return fit_adverse_table(rows, dims=("depth",), depth_edges=(1,), shrink_k=0.0, horizons_s=(0.05, 1.0))


class HorizonTests(unittest.TestCase):
    def test_the_entry_rule_and_the_repricing_rule_read_their_own_horizons(self):
        books, states = _books()
        kw = dict(profile="perp_perp_maker_taker_v1")
        s = _strategy(_two_horizon(-1.0, -9.0), adverse_horizon_s=0.05, adverse_requote_horizon_s=1.0, **kw)
        s._observe_adverse_features(books, states, NOW)
        self.assertAlmostEqual(s._hedge_move_bps("bid", BYBIT, BINANCE, books), -1.0)                 # the entry rule: the hedge-delay horizon
        self.assertAlmostEqual(s._hedge_move_bps("bid", BYBIT, BINANCE, books, None, 1.0), -9.0)
        px, why = s.requote_target(BYBIT, "buy", 1.0, books, NOW)
        ref, why2 = _strategy(expected_hedge_move_bps_bid=-9.0, **kw).requote_target(BYBIT, "buy", 1.0, books, NOW)
        self.assertEqual((why, why2), ("ok", "ok"))
        self.assertEqual(px, ref)                                                                    # repricing used the 1 s value, as the plain number -9.0 would
        s2 = _strategy(_two_horizon(-1.0, -9.0), adverse_horizon_s=0.05, **kw)                       # no separate repricing horizon: the entry rule's
        s2._observe_adverse_features(books, states, NOW)
        px2, _ = s2.requote_target(BYBIT, "buy", 1.0, books, NOW)
        self.assertEqual(px2, _strategy(expected_hedge_move_bps_bid=-1.0, **kw).requote_target(BYBIT, "buy", 1.0, books, NOW)[0])
        self.assertGreater(px2, px)                                                                  # a less adverse move allows a higher bid

    def test_v1_6_reads_50_ms_for_the_hedge_move_and_1_s_for_repricing(self):
        c = apply_profile(load_profile("perp_perp_maker_taker_v1_6"), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", data_root="", output_dir="")
        self.assertEqual((c.arb.adverse_horizon_s, c.arb.adverse_requote_horizon_s), (0.05, 1.0))
        self.assertEqual(c.release_identity["adverse_table"]["requote_horizon_s"], 1.0)
        self.assertIsNone(apply_profile(load_profile("perp_perp_maker_taker_v1_5"), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", data_root="", output_dir="").arb.adverse_requote_horizon_s)


if __name__ == "__main__":
    unittest.main()
