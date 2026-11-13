"""E3 participation cap (owner 13 Nov). Item 1 (print spent once across own orders) is tested in tests/test_fill_engine_queue.py::TestParticipationCap; this file adds the order's remaining tests.
SYNTHETIC books and prints, tests only: nothing here is a profit figure."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from crypto_stat_arb.execution import DeterministicVenueAdapter, ExecutionEventType, OrderType, TimeInForce
from crypto_stat_arb.models import Side
from test_queue_adapter import INSTR, VENUE, S, core_book, request

BIDS = [["100.00", "50"], ["99.99", "100"]]
ASKS = [["100.02", "50"], ["100.03", "100"]]


def ioc_sell(order_id, qty, limit="99.99", ts=2 * S):
    return request(order_id, Side.SELL, limit, qty, tif=TimeInForce.IOC, post_only=False, ts=ts)


class OwnTakerDepthConsumption(unittest.TestCase):
    """Item 2: an own IOC removes the depth it took from the hedge venue's book until a real book update reflects it; a following own order cannot reuse it."""

    def fills(self, callbacks):
        return [c for c in callbacks if c.event_type in (ExecutionEventType.FILLED, ExecutionEventType.PARTIAL)]

    def test_hedge_then_immediate_retry_at_the_same_level_sees_the_reduced_depth(self):
        book = core_book(BIDS, ASKS)
        a = DeterministicVenueAdapter(VENUE, consume_liquidity=True)
        first = self.fills(a.handle(ioc_sell("h1", "40"), book))[-1]
        retry = self.fills(a.handle(ioc_sell("h2", "40", ts=2 * S + 1), book))[-1]          # the same public book object: no update in between
        self.assertEqual((first.cumulative_filled_quantity, first.average_price), (Decimal("40"), Decimal("100.00")))
        # 10 of the 50 at 100.00 are left; the other 30 come from 99.99: (10 x 100.00 + 30 x 99.99) / 40
        self.assertEqual(retry.cumulative_filled_quantity, Decimal("40"))
        self.assertEqual(retry.average_price, (Decimal("10") * Decimal("100.00") + Decimal("30") * Decimal("99.99")) / Decimal("40"))
        self.assertLess(retry.average_price, first.average_price)

    def test_without_consumption_the_retry_reuses_the_same_depth_so_the_test_can_fail(self):
        book = core_book(BIDS, ASKS)
        a = DeterministicVenueAdapter(VENUE, consume_liquidity=False)
        self.fills(a.handle(ioc_sell("h1", "40"), book))
        retry = self.fills(a.handle(ioc_sell("h2", "40", ts=2 * S + 1), book))[-1]
        self.assertEqual(retry.average_price, Decimal("100.00"))                           # the 40 taken first are still there: depth used twice

    def test_a_real_book_update_that_reflects_the_take_releases_it_and_a_new_snapshot_clears_it(self):
        a = DeterministicVenueAdapter(VENUE, consume_liquidity=True)
        book = core_book(BIDS, ASKS)
        self.fills(a.handle(ioc_sell("h1", "40"), book))
        # the venue's book now shows the level reduced by the same 40: our take is reflected, the retry sees 10 at 100.00 (not 10 minus 40 again)
        reflected = core_book([["100.00", "10"], ["99.99", "100"]], ASKS, ts=3 * S, seq=2)
        reflected_retry = self.fills(a.handle(ioc_sell("h2", "10", ts=3 * S + 1), reflected))[-1]
        self.assertEqual((reflected_retry.cumulative_filled_quantity, reflected_retry.average_price), (Decimal("10"), Decimal("100.00")))
        # a fresh snapshot (resync marker) releases every withheld take
        b = DeterministicVenueAdapter(VENUE, consume_liquidity=True)
        book2 = core_book(BIDS, ASKS)
        self.fills(b.handle(ioc_sell("h1", "40"), book2))
        book2.apply(__import__("crypto_stat_arb.models", fromlist=["MarketEvent"]).MarketEvent.from_dict({
            "event_type": "BOOK_SNAPSHOT", "venue": VENUE, "instrument_id": INSTR, "exchange_ts_ns": 4 * S, "receive_ts_ns": 4 * S, "sequence": 3, "bids": BIDS, "asks": ASKS}))
        after = self.fills(b.handle(ioc_sell("h3", "40", ts=4 * S + 1), book2))[-1]
        self.assertEqual(after.average_price, Decimal("100.00"))


class ParticipationLimit(unittest.TestCase):
    """Item 3: a new quote's size is capped at participation_max x the public volume printed at its price level in the last participation_window_s; refusals count as reject "participation"; hedges are untouched."""

    def setUp(self):
        from test_entry_hedge_vwap import DEEP_TOUCH, NOW, books, strategy
        from crypto_stat_arb.simulation.venue_state import VenueState
        self.NOW, self.books, self.DEEP, self.strategy, self.VS = NOW, books, DEEP_TOUCH, strategy, VenueState

    def sig(self, strat, state):
        from crypto_stat_arb.strategies.arb_strategy import InventoryBook
        bks = self.books(self.DEEP)
        return strat._evaluate_maker_taker("bybit", "binance", bks, {"bybit": state, "binance": self.VS("binance")}, InventoryBook(), self.NOW)

    def price_of(self, strat):
        return self.sig(self.strategy("touch"), self.VS("bybit")).buy_price

    def test_off_is_identical_and_tracks_nothing(self):
        st = self.VS("bybit")
        a, b = self.sig(self.strategy("touch"), st), self.sig(self.strategy("touch", participation_max=0.0), self.VS("bybit"))
        self.assertEqual((a.qty, a.buy_price), (b.qty, b.buy_price))
        self.assertEqual(st._level_trades, {})

    def test_the_size_is_capped_at_the_share_of_the_level_volume(self):
        p = self.price_of(None)
        strat = self.strategy("touch", participation_max=0.10, participation_window_s=60.0, min_clip_notional=1.0)
        st = self.VS("bybit"); st.track_level_volume(60.0)
        st.on_trade(self.NOW - 30.0, p, 4.0, -1); st.on_trade(self.NOW - 10.0, p, 6.0, 1)           # 10.0 printed at the level in the window, both aggressor sides
        sig = self.sig(strat, st)
        self.assertAlmostEqual(sig.qty, 1.0)                                                         # 10% of 10.0, below the 2.0 clip
        self.assertEqual(strat.rejects["participation_size_capped"], 1)

    def test_prints_older_than_the_window_or_at_another_price_do_not_count_and_the_quote_is_refused(self):
        p = self.price_of(None)
        strat = self.strategy("touch", participation_max=0.10, participation_window_s=60.0)
        st = self.VS("bybit"); st.track_level_volume(60.0)
        st.on_trade(self.NOW - 61.0, p, 500.0, -1)                                                   # too old
        st.on_trade(self.NOW - 5.0, p + 0.01, 500.0, -1)                                             # another level
        self.assertIsNone(self.sig(strat, st))
        self.assertEqual(strat.rejects["participation"], 1)

    def test_a_cap_that_still_clears_the_minimum_clip_is_not_a_refusal_and_enough_volume_leaves_the_size_alone(self):
        p = self.price_of(None)
        strat = self.strategy("touch", participation_max=0.10, participation_window_s=60.0)
        st = self.VS("bybit"); st.track_level_volume(60.0)
        st.on_trade(self.NOW - 1.0, p, 1000.0, -1)
        sig = self.sig(strat, st)
        self.assertEqual(sig.qty, 2.0)                                                               # the full clip: 10% of 1000 is 100
        self.assertEqual(strat.rejects.get("participation", 0) + strat.rejects.get("participation_size_capped", 0), 0)

    def test_the_hedge_depth_cap_is_not_this_rules_business(self):
        import inspect
        from crypto_stat_arb import oms
        self.assertNotIn("participation", inspect.getsource(oms))                                    # hedges (the OMS) never read the limit

    def test_validation_and_the_state_map_stays_small(self):
        from crypto_stat_arb.strategies.arb_strategy import ArbConfig
        for bad in (dict(participation_max=1.5), dict(participation_max=-0.1), dict(participation_window_s=0.0)):
            with self.assertRaises(ValueError):
                ArbConfig(**bad)
        st = self.VS("x"); st.track_level_volume(1.0)
        for i in range(10_000):
            st.on_trade(float(i), 100.0 + i * 0.001, 1.0, 1)
        self.assertLess(len(st._level_trades), 4100)


class WholeReplayAudit(unittest.TestCase):
    """Item 1's whole-replay check: own passive fills per level against the public volume printed there; any excess fails."""

    def fill(self, venue, price, qty, post_only=True):
        return {"venue": venue, "fill_price": price, "fill_quantity": qty, "order": {"post_only": post_only}}

    def test_a_replay_within_the_public_volume_passes_and_an_excess_is_reported(self):
        from new_backtest.session_replay import participation_audit
        traded = {("BYBIT", "100.570"): 5.0, ("BYBIT", "100.4"): 1.0}
        ok = participation_audit([self.fill("BYBIT", "100.57", "2.0"), self.fill("BYBIT", "100.57", "3.0"), self.fill("BYBIT", "100.4", "0.5"), self.fill("BINANCE", "99", "9", post_only=False)], traded)
        self.assertTrue(ok["ok"]); self.assertEqual(ok["levels_checked"], 2)                          # the taker fill is not a passive fill
        bad = participation_audit([self.fill("BYBIT", "100.57", "2.0"), self.fill("BYBIT", "100.57", "3.1")], traded)
        self.assertFalse(bad["ok"]); self.assertEqual([(x["price"], x["own_filled"]) for x in bad["excess"]], [("100.57", 5.1)])
        none = participation_audit([self.fill("BYBIT", "100.9", "0.1")], traded)                      # a level with no public print at all
        self.assertFalse(none["ok"])


if __name__ == "__main__":
    unittest.main()
