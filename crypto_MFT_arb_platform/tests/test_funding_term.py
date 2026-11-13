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
from test_entry_hedge_vwap import NOW, books, signal_for, strategy, THIN_TOUCH

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
        return SimpleNamespace(venue=venue, funding_rate=Decimal(rate), funding_interval_hours=hours, receive_ts_ns=int(NOW * 1e9))

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


if __name__ == "__main__":
    unittest.main()
