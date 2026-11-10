"""S3 (owner 10 Nov): the do-not-quote filter. The feature definition is ONE function (`QuoteFeatures.vector`); the study assembles its `QuoteState` from a snapshot ring, the strategy from the books with `observe`: both must give the same state
on the same stream. The model is plain Python, deterministic, and its rule is the one in the docstring (skip when naive edge + predicted move < threshold)."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from crypto_stat_arb.simulation.order_book import OrderBook
from crypto_stat_arb.simulation.venue_state import VenueState
from crypto_stat_arb.strategies.edge import (QUOTE_FEATURE_NAMES, AdverseFeatures, DoNotQuoteFilter, QuoteFeatures, QuoteState, fit_do_not_quote_filter)
from new_backtest.event_loader import BOOK, TRADE
from new_backtest.market_hedge_move import QuoteFilterStudy, study_setup


def state(**kw):
    base = dict(maker_bid=100.0, maker_ask=100.02, maker_bid_size=40.0, maker_ask_size=10.0, maker_bid_top5=100.0, maker_ask_top5=50.0, hedge_bid=100.05, hedge_ask=100.06, hedge_bid_size=30.0, hedge_ask_size=10.0,
                vol_bps=2.0, since_touch_change_s=0.5, buy_vol_1s=7.0, sell_vol_1s=3.0)
    return QuoteState(**{**base, **kw})


class FeatureTests(unittest.TestCase):
    def test_every_feature_is_stated_for_our_side_and_the_naive_edge_has_no_expected_move_in_it(self):
        bid = dict(zip(QUOTE_FEATURE_NAMES, QuoteFeatures.vector("bid", state(), 100.0, 1.0, 1.7)))
        ask = dict(zip(QUOTE_FEATURE_NAMES, QuoteFeatures.vector("ask", state(), 100.02, 1.0, 1.7)))
        self.assertAlmostEqual(bid["own_share_touch_maker"], 40 / 50)                  # a bid: the bid side's share of the touch
        self.assertAlmostEqual(ask["own_share_touch_maker"], 10 / 50)
        self.assertAlmostEqual(bid["hedge_side_share_touch"], 30 / 40)                 # a bid is hedged by a sell into the hedge bid
        self.assertAlmostEqual(ask["hedge_side_share_touch"], 10 / 40)                 # an ask by a buy into the hedge ask
        self.assertGreater(bid["fill_dir_vol_1s"], ask["fill_dir_vol_1s"] - 10)        # a bid is filled by SELL prints (3.0), an ask by BUY prints (7.0)
        self.assertAlmostEqual(bid["fill_dir_vol_1s"], __import__("math").log1p(3.0))
        self.assertAlmostEqual(ask["fill_dir_vol_1s"], __import__("math").log1p(7.0))
        self.assertAlmostEqual(bid["naive_edge_bps"], (100.05 - 100.0) / 100.0 * 1e4 - 2.7, places=9)       # hedge bid less our bid, less fees: no move in it
        self.assertAlmostEqual(ask["naive_edge_bps"], (100.02 - 100.06) / 100.02 * 1e4 - 2.7, places=9)
        self.assertEqual((bid["improves_touch"], ask["improves_touch"]), (0.0, 0.0))
        self.assertEqual(dict(zip(QUOTE_FEATURE_NAMES, QuoteFeatures.vector("bid", state(), 100.01, 1.0, 1.7)))["improves_touch"], 1.0)
        self.assertEqual(dict(zip(QUOTE_FEATURE_NAMES, QuoteFeatures.vector("bid", state(), 100.01, 1.0, 1.7)))["queue_ahead"], 0.0)   # an improving quote is first in line
        self.assertEqual(dict(zip(QUOTE_FEATURE_NAMES, QuoteFeatures.vector("bid", state(vol_bps=None), 100.0, 1.0, 1.7)))["vol_known"], 0.0)


class ModelTests(unittest.TestCase):
    def test_the_ridge_fit_recovers_a_linear_move_and_the_rule_skips_when_edge_plus_move_is_below_the_threshold(self):
        import random

        rnd = random.Random(1)
        X = [[rnd.random() if i != QUOTE_FEATURE_NAMES.index("naive_edge_bps") else 3.0 for i in range(len(QUOTE_FEATURE_NAMES))] for _ in range(2000)]
        y = [-4.0 * x[0] + 0.01 * rnd.random() for x in X]
        f = fit_do_not_quote_filter({"bid": (X, y)}, horizon_s=0.05, ridge=1e-3, threshold_bps=1.0)
        lo, hi = [0.0] * len(QUOTE_FEATURE_NAMES), [0.0] * len(QUOTE_FEATURE_NAMES)
        lo[QUOTE_FEATURE_NAMES.index("naive_edge_bps")] = hi[QUOTE_FEATURE_NAMES.index("naive_edge_bps")] = 3.0
        hi[0] = 1.0
        self.assertAlmostEqual(f.predict_move_bps("bid", hi) - f.predict_move_bps("bid", lo), -4.0, places=1)
        self.assertFalse(f.skip("bid", lo))                       # edge 3.0 + move ~0 >= 1.0: keep
        self.assertTrue(f.skip("bid", hi))                        # edge 3.0 + move ~-4.0 < 1.0: skip
        self.assertFalse(f.with_threshold(-2.0).skip("bid", hi))   # the threshold is the only thing that changed

    def test_save_load_round_trip_and_a_pinned_form(self):
        f = DoNotQuoteFilter(QUOTE_FEATURE_NAMES, 0.05, 0.5, {"bid": {"intercept": -0.2, "weights": [0.0] * 12, "means": [0.0] * 12, "scales": [1.0] * 12}})
        with tempfile.TemporaryDirectory() as d:
            f.save(Path(d) / "f.json")
            g = DoNotQuoteFilter.load(Path(d) / "f.json")
        self.assertEqual((g.names, g.horizon_s, g.threshold_bps, g.sides), (f.names, f.horizon_s, f.threshold_bps, f.sides))


class StudyMatchesStrategyTests(unittest.TestCase):
    def test_the_study_snapshot_state_equals_observe_on_the_same_stream(self):
        setup = study_setup("perp_perp_maker_taker_v1_6")
        study = QuoteFilterStudy(setup)
        mb, hb = OrderBook("bybit", "SOLUSDT", tape_repair=False), OrderBook("binance", "SOLUSDT", tape_repair=False)
        ref_state, ref_qf = VenueState("BYBIT"), QuoteFeatures(AdverseFeatures())
        snap = lambda book, ts, bids, asks: book.apply_snapshot(bids, asks, ts)
        items = [(1.00, 1, "book", ([(100.00, 5.0), (99.99, 7.0), (99.98, 9.0)], [(100.02, 4.0), (100.03, 6.0)])),
                 (1.00, 0, "book", ([(100.05, 30.0)], [(100.06, 10.0)])),
                 (1.20, 1, "trade", (100.02, 2.0, 1)),
                 (1.30, 1, "book", ([(100.00, 6.0), (99.99, 7.0)], [(100.02, 3.0), (100.03, 6.0)])),
                 (1.40, 1, "trade", (100.00, 1.5, -1)),
                 (1.50, 0, "book", ([(100.04, 20.0)], [(100.05, 12.0)])),
                 (1.60, 1, "book", ([(100.01, 2.0), (100.00, 6.0)], [(100.03, 6.0)]))]
        for ts, kind, what, data in items:
            if what == "trade":
                price, vol, sign = data
                ev = SimpleNamespace(kind=TRADE, payload={"exchange_ts": ts, "price": str(price), "volume": str(vol), "side": "buy" if sign > 0 else "sell"})
            else:
                ev = SimpleNamespace(kind=BOOK, payload={"timestamp": ts})
            study.tick(ts, kind, ev, hb, mb)                                   # the loop calls the hook BEFORE it applies the item
            if what == "book":
                (snap(mb, ts, *data) if kind == 1 else snap(hb, ts, *data))
                if kind == 1:
                    ref_state.on_book(mb, ts)
                    ref_qf.on_touch(ts, mb.best_bid, mb.best_ask)
                else:
                    ref_qf.adverse.on_mid("hedge", ts, hb.mid)
            else:
                ref_state.on_trade(ts, data[0], data[1], data[2])
        study.tick(float("inf"), None, None, hb, mb)
        got, toxic = study._state_at(1.60)
        want = ref_qf.observe(1.60, mb, hb, ref_state)
        self.assertEqual(got, want)
        self.assertEqual(toxic, bool(ref_state.is_toxic(mb, setup["arb"].toxic_trade_sign, setup["arb"].toxic_depth_collapse)))
        self.assertEqual(study._state_at(1.25)[0].maker_bid_size, 5.0)         # an earlier moment reads the earlier book (the lag lookup)
        self.assertEqual(study._state_at(1.25)[0].buy_vol_1s, 2.0)


class StrategyFilterTests(unittest.TestCase):
    def strategy(self, threshold):
        from crypto_stat_arb.strategies.arb_strategy import CrossVenueArbStrategy
        from new_backtest.config import core_fee_specs, make_venues

        setup = study_setup("perp_perp_maker_taker_v1_6")
        cfg_fee, _ = core_fee_specs("SOLUSDT", "bybit_perp_vip5", "binance_perp_vip9")
        specs = make_venues("singapore", "bybit_perp_vip5", "binance_perp_vip9", fee_specs=cfg_fee)
        flt = DoNotQuoteFilter(QUOTE_FEATURE_NAMES, 0.05, threshold, {side: {"intercept": -1.0, "weights": [0.0] * 12, "means": [0.0] * 12, "scales": [1.0] * 12} for side in ("bid", "ask")})
        arb = setup["arb"]
        arb.do_not_quote = flt
        return CrossVenueArbStrategy("SOLUSDT", {"bybit": specs["bybit"], "binance": specs["binance"]}, arb), flt

    def books(self):
        mb, hb = OrderBook("bybit", "SOLUSDT", tape_repair=False), OrderBook("binance", "SOLUSDT", tape_repair=False)
        mb.apply_snapshot([(100.00, 5.0)], [(100.02, 4.0)], 1.0)
        hb.apply_snapshot([(100.05, 30.0)], [(100.06, 10.0)], 1.0)
        return {"bybit": mb, "binance": hb}, {"bybit": VenueState("BYBIT")}

    def test_the_strategy_skips_a_quote_when_naive_edge_plus_the_predicted_move_is_below_the_threshold_and_counts_it(self):
        books, states = self.books()
        st, flt = self.strategy(threshold=0.0)                       # naive edge of a bid at 100.00 against a hedge bid of 100.05 = 5.0 - 2.7 = 2.3 bps; predicted move -1.0: 1.3 >= 0 -> keep
        st._observe_quote_features(books, 1.0)
        self.assertFalse(st._dnq_skip("bid", 100.0, "bybit", books, states, 1.0, 1.7, 1.0))
        self.assertNotIn("dnq", st.rejects)
        st2, _ = self.strategy(threshold=2.0)                        # 1.3 < 2.0 -> skip
        st2._observe_quote_features(books, 1.0)
        self.assertTrue(st2._dnq_skip("bid", 100.0, "bybit", books, states, 1.0, 1.7, 1.0))
        self.assertEqual(st2.rejects["dnq"], 1)

    def test_without_a_filter_nothing_changes_and_no_dnq_key_appears(self):
        from crypto_stat_arb.strategies.arb_strategy import CrossVenueArbStrategy
        from new_backtest.config import core_fee_specs, make_venues

        setup = study_setup("perp_perp_maker_taker_v1_6")
        self.assertIsNone(setup["arb"].do_not_quote)
        cfg_fee, _ = core_fee_specs("SOLUSDT", "bybit_perp_vip5", "binance_perp_vip9")
        specs = make_venues("singapore", "bybit_perp_vip5", "binance_perp_vip9", fee_specs=cfg_fee)
        st = CrossVenueArbStrategy("SOLUSDT", {"bybit": specs["bybit"], "binance": specs["binance"]}, setup["arb"])
        books, states = self.books()
        self.assertIsNone(st._quote_features)
        self.assertFalse(st._dnq_skip("bid", 100.0, "bybit", books, states, 1.0, 1.7, 1.0))
        self.assertNotIn("dnq", st.rejects)


if __name__ == "__main__":
    unittest.main()
