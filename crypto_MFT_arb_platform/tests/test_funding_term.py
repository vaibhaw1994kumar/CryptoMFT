"""Owner 13 Nov, edge calibration item 3: the expected funding cost for the expected holding time is added to funding_holding_bps from the derivative stream's predicted rates. OFF by default (the constant, bit-identical);
a credit never raises the edge under floor_zero; a missing or stale rate adds 0 and is counted. SYNTHETIC rates, tests only."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from crypto_stat_arb.strategies.arb_strategy import ArbSessionStrategy
from crypto_stat_arb.simulation.venue_state import VenueState
from crypto_stat_arb.strategies.arb_strategy import InventoryBook
from test_entry_hedge_vwap import DEEP_TOUCH, NOW, books, signal_for, strategy, THIN_TOUCH

H8 = 8 * 3600.0


def with_rates(strat, bybit, binance, ts=NOW):
    strat.observe_funding_rate("bybit", bybit, H8, ts)
    strat.observe_funding_rate("binance", binance, H8, ts)
    return strat


class FundingTerm(unittest.TestCase):
    def test_default_is_exactly_the_constant_and_reads_no_rate(self):
        s = strategy("touch", funding_holding_bps=0.3)
        self.assertEqual(s._funding_holding_bps("bybit", "binance", "bid", NOW), 0.3)
        self.assertEqual(s.rejects.get("funding_term_unavailable", 0), 0)

    def test_a_long_pays_a_positive_rate_and_the_short_receives_it(self):
        s = with_rates(strategy("touch", funding_expected_holding_s=3600.0), 0.0001, 0.00005)
        # bid: long bybit (0.0001 / 8 h), short binance (0.00005 / 8 h): net cost (0.0001 - 0.00005) x (3600 / 28800) x 1e4 = 0.0625 bps
        self.assertAlmostEqual(s._funding_holding_bps("bybit", "binance", "bid", NOW), 0.0625, places=12)
        # ask: long binance, short bybit: the sign flips, a credit; floor_zero never lets it raise the edge
        self.assertEqual(s._funding_holding_bps("bybit", "binance", "ask", NOW), 0.0)

    def test_credit_allow_lowers_the_cost(self):
        s = with_rates(strategy("touch", funding_expected_holding_s=3600.0, funding_credit="allow"), 0.0001, 0.00005)
        self.assertAlmostEqual(s._funding_holding_bps("bybit", "binance", "ask", NOW), -0.0625, places=12)

    def test_the_term_is_added_to_the_constant(self):
        s = with_rates(strategy("touch", funding_expected_holding_s=3600.0, funding_holding_bps=0.2), 0.0001, 0.00005)
        self.assertAlmostEqual(s._funding_holding_bps("bybit", "binance", "bid", NOW), 0.2625, places=12)

    def test_a_missing_or_stale_rate_adds_nothing_and_is_counted(self):
        s = strategy("touch", funding_expected_holding_s=3600.0, funding_holding_bps=0.2)
        self.assertEqual(s._funding_holding_bps("bybit", "binance", "bid", NOW), 0.2)
        self.assertEqual(s.rejects["funding_term_unavailable"], 1)
        with_rates(s, 0.0001, 0.00005, ts=NOW - 1000.0)                                  # older than funding_rate_max_age_s (600)
        self.assertEqual(s._funding_holding_bps("bybit", "binance", "bid", NOW), 0.2)
        self.assertEqual(s.rejects["funding_term_unavailable"], 2)

    def test_a_positive_cost_lowers_the_quote_price_and_the_entry_edge_stays_consistent_with_quote_validity(self):
        plain = signal_for(strategy("vwap"), books(THIN_TOUCH))
        s = with_rates(strategy("vwap", funding_expected_holding_s=86_400.0), 0.0006, 0.0)    # 1 day at 6 bps per 8 h on the long venue: 18 bps cost
        sig = signal_for(s, books(THIN_TOUCH))
        if sig is not None:
            self.assertLess(sig.buy_price, plain.buy_price)
            _, net, why = s.quote_validity("bybit", "buy", sig.buy_price, sig.qty, books(THIN_TOUCH), NOW)
            self.assertEqual(why, "ok")
            self.assertAlmostEqual(sig.conditional_edge_bps, net, places=10)
        else:                                                                             # a cost this large may leave no quote that improves the maker book: also a correct answer
            self.assertGreaterEqual(s.rejects.get("no_edge", 0), 1)

    def test_validation(self):
        from crypto_stat_arb.strategies.arb_strategy import ArbConfig
        for bad in (dict(funding_credit="x"), dict(funding_expected_holding_s=-1.0), dict(funding_rate_max_age_s=0.0)):
            with self.assertRaises(ValueError):
                ArbConfig(**bad)


class Routing(unittest.TestCase):
    def tick(self, venue="BYBIT", rate="0.0001", hours=Decimal("8")):
        return SimpleNamespace(venue=venue, funding_rate=Decimal(rate), funding_interval_hours=hours, receive_ts_ns=int(NOW * 1e9), next_funding_ts_ns=None)

    def shim(self, holding):
        strat = strategy("touch", funding_expected_holding_s=holding)
        return strat, ArbSessionStrategy(strat, ledger=None, oms=None, states={}, instruments=None, instrument_id="X", venue_names={"bybit": "BYBIT", "binance": "BINANCE"})

    def test_the_session_hook_maps_the_core_venue_to_the_feed_venue_and_the_interval_to_seconds(self):
        strat, shim = self.shim(3600.0)
        shim.on_derivative(self.tick("BINANCE", "0.0002", Decimal("4")))
        self.assertEqual(strat._funding_rates["binance"], (0.0002, 4 * 3600.0, NOW))

    def test_off_means_nothing_is_read(self):
        strat, shim = self.shim(0.0)
        shim.on_derivative(self.tick())
        self.assertEqual(strat._funding_rates, {})

    def test_the_trading_session_routes_derivative_ticks_to_a_strategy_with_the_hook_only(self):
        from crypto_stat_arb.session import TradingSession
        got = []
        session = SimpleNamespace(strategies=[SimpleNamespace(on_derivative=got.append), SimpleNamespace()], exchange_marks={}, funding=SimpleNamespace(configured=False))
        TradingSession.on_derivative(session, SimpleNamespace(venue="BYBIT", instrument_id="X", mark_price=None, receive_ts_ns=1, funding_rate=None, next_funding_ts_ns=None))
        self.assertEqual(len(got), 1)


class EdgeLevel(unittest.TestCase):
    """Owner 14 Nov item 1: a net cost lowers the edge (and so the quote price), a net credit under floor_zero changes nothing, both signs."""

    def test_a_net_cost_lowers_the_quote_price_and_a_net_credit_is_zero(self):
        def limit(strat, side):
            f = strat._funding_holding_bps("bybit", "binance", side, NOW)
            return strat._quote_limit(side, 100.30 if side == "bid" else 100.20, 4.5, 0.0, f), f
        plain_bid, _ = limit(strategy("vwap"), "bid")
        cost_bid, f_cost = limit(with_rates(strategy("vwap", funding_expected_holding_s=28_800.0), 0.0003, 0.0), "bid")        # bid: long bybit pays 3 bps / 8 h, for 8 h: 3 bps
        credit_bid, f_credit = limit(with_rates(strategy("vwap", funding_expected_holding_s=28_800.0), 0.0, 0.0003), "bid")    # the long venue receives: a net credit
        self.assertAlmostEqual(f_cost, 3.0, places=9)
        self.assertLess(cost_bid, plain_bid)                                                      # the quote may rest only 3 bps lower
        self.assertAlmostEqual((plain_bid - cost_bid) / plain_bid * 1e4, 3.0, places=1)
        self.assertEqual((f_credit, credit_bid), (0.0, plain_bid))                                # floor_zero: funding never raises what a bid may pay
        plain_ask, _ = limit(strategy("vwap"), "ask")
        cost_ask, f_ask = limit(with_rates(strategy("vwap", funding_expected_holding_s=28_800.0), 0.0, 0.0003), "ask")        # ask: long is binance (pays 3 bps), short bybit
        self.assertAlmostEqual(f_ask, 3.0, places=9)
        self.assertGreater(cost_ask, plain_ask)                                                   # a sell quote may rest only higher

FUND_T = NOW + 100.0


def stream(strat, bybit_next=FUND_T, binance_next=FUND_T, at=NOW):
    strat.observe_funding_rate("bybit", None, H8, at, bybit_next)
    strat.observe_funding_rate("binance", None, H8, at, binance_next)
    return strat


class FundingWindow(unittest.TestCase):
    """Owner 14 Nov item 2: no new quote within 30 s before a funding timestamp on either venue; resting quotes withdrawn at the window start; quoting resumes when the stream shows the next settlement;
    no next-funding time = not evaluable (counted, nothing blocked)."""

    def evaluate(self, strat, now):
        bks = books(DEEP_TOUCH)
        for b in bks.values():
            b.apply_snapshot([[p, q] for p, q in b._bids.items()], [[p, q] for p, q in b._asks.items()], now)       # fresh touches at `now`
        return strat.evaluate(bks, {v: VenueState(v) for v in bks}, InventoryBook(), now)

    def test_states_across_the_settlement_and_either_venue(self):
        s = stream(strategy("touch", funding_window_s=30.0))
        self.assertEqual(s.funding_window_state(FUND_T - 31.0), ("open", ""))
        self.assertEqual(s.funding_window_state(FUND_T - 30.0)[0], "blocked")
        self.assertEqual(s.funding_window_state(FUND_T)[0], "blocked")
        self.assertEqual(s.funding_window_state(FUND_T + 29.0)[0], "blocked")                    # the settlement passed, the stream has not moved on yet
        s.observe_funding_rate("bybit", None, H8, FUND_T + 2.0, FUND_T + H8)                    # the stream shows the next settlement on one venue only
        self.assertEqual(s.funding_window_state(FUND_T + 3.0), ("blocked", "binance"))           # the other venue still blocks
        s.observe_funding_rate("binance", None, H8, FUND_T + 2.0, FUND_T + H8)
        self.assertEqual(s.funding_window_state(FUND_T + 3.0), ("open", ""))                     # resumed once funding is booked
        # the fallback: the stream never moves on, 30 s after the timestamp the held one is stale: not evaluable, not blocked forever
        t = stream(strategy("touch", funding_window_s=30.0))
        self.assertEqual(t.funding_window_state(FUND_T + 31.0)[0], "not_evaluable")

    def test_either_venue_alone_closes_the_window(self):
        s = stream(strategy("touch", funding_window_s=30.0), bybit_next=FUND_T + 10_000.0, binance_next=FUND_T)
        self.assertEqual(s.funding_window_state(FUND_T - 10.0), ("blocked", "binance"))

    def test_a_blocked_window_makes_no_signal_and_counts_it_and_open_makes_one(self):
        s = stream(strategy("touch", funding_window_s=30.0))
        self.assertIsNone(self.evaluate(s, FUND_T - 10.0))
        self.assertEqual(s.rejects["funding_window"], 1)
        self.evaluate(s, NOW)                                                                    # 100 s before: open, nothing more counted
        self.assertEqual(s.rejects["funding_window"], 1)

    def test_not_evaluable_is_labelled_and_blocks_nothing(self):
        s = strategy("touch", funding_window_s=30.0)                                             # no derivative data at all
        self.evaluate(s, NOW)
        self.assertEqual(s.rejects["funding_window_not_evaluable"], 1)
        self.assertEqual(s.rejects.get("funding_window", 0), 0)

    def test_off_is_identical_to_no_window(self):
        s = strategy("touch")
        self.evaluate(s, NOW)
        self.assertEqual({k for k in s.rejects if k.startswith("funding")}, set())                # off: no funding counter exists, nothing is read

    def test_resting_quotes_are_withdrawn_at_the_window_start_with_the_reason(self):
        from decimal import Decimal
        from crypto_stat_arb.models import ChildOrder, OrderState, Side
        strat = stream(strategy("touch", funding_window_s=30.0))
        child = ChildOrder(order_id="q1", parent_id="p", opportunity_id="o", venue="BYBIT", instrument_id="SOL-USDT-PERP", side=Side.BUY, quantity=Decimal("1"), limit_price=Decimal("100.2"),
                           state=OrderState.ACKNOWLEDGED, filled_quantity=Decimal("0"), post_only=True)
        shim = ArbSessionStrategy(strat, ledger=None, oms=SimpleNamespace(plans={"p": SimpleNamespace(children=[child])}), states={}, instruments=None, instrument_id="X", venue_names={"bybit": "BYBIT", "binance": "BINANCE"})
        self.assertEqual(shim.quotes_to_withdraw({}, int((FUND_T - 31.0) * 1e9)), [])            # before the window: nothing
        out = shim.quotes_to_withdraw({}, int((FUND_T - 30.0) * 1e9))
        self.assertEqual([(q["order_id"], q["reason"]) for q in out], [("q1", "funding_window")])

    def test_the_session_hook_feeds_the_next_funding_time_even_with_no_funding_term(self):
        strat = strategy("touch", funding_window_s=30.0)
        shim = ArbSessionStrategy(strat, ledger=None, oms=None, states={}, instruments=None, instrument_id="X", venue_names={"bybit": "BYBIT", "binance": "BINANCE"})
        shim.on_derivative(SimpleNamespace(venue="BYBIT", funding_rate=Decimal("0.0001"), funding_interval_hours=None, receive_ts_ns=int(NOW * 1e9), next_funding_ts_ns=int(FUND_T * 1e9)))
        self.assertEqual(strat._next_funding["bybit"], FUND_T)
        self.assertEqual(strat._funding_rates, {})                                               # no funding term configured: the rate is not kept


if __name__ == "__main__":
    unittest.main()
