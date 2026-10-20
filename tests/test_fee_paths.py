"""One fee rate path (audit P0): every public accessor returns the rate the ledger books."""

from decimal import Decimal
import unittest

from crypto_stat_arb.fees import FeeRule, FeeSchedule, FeeSource, Liquidity
from crypto_stat_arb.maker_sim import maker_simulator_from_config
from crypto_stat_arb.strategy import CrossVenueSpreadStrategy
from test_economic_ledger import child, fee_master, fill, oms_with
from test_execution_realism import books
from test_oms import instrument_master

INSTRUMENT = "BTC-USDT-SPOT"


def master_with(*rules):
    master = instrument_master()
    master.fee_schedule = FeeSchedule(rules)
    return master


def every_path(master, liquidity, at_ns=None):
    """(rule.effective_bps, schedule.rate_bps, master.fee_bps) for BINANCE."""
    return (
        master.fee_rule("BINANCE", INSTRUMENT, at_ns).effective_bps(liquidity)[0],
        master.fee_schedule.rate_bps("BINANCE", INSTRUMENT, liquidity, at_ns),
        master.fee_bps("BINANCE", INSTRUMENT, liquidity, at_ns),
    )


class FeePathTests(unittest.TestCase):
    def test_e01_unverified_rebate_is_zero_through_every_public_path(self):
        master = master_with(FeeRule("BINANCE", Decimal("-0.5"), Decimal("4"), instrument_id=INSTRUMENT,
                                     source=FeeSource.ASSUMED, conservative_unverified=True))
        self.assertEqual(every_path(master, Liquidity.MAKER), (Decimal("0"),) * 3)
        self.assertEqual(master.fee_schedule.raw_rate_bps("BINANCE", INSTRUMENT, Liquidity.MAKER),
                         Decimal("-0.5"))
        # ...and the ledger books zero for a maker fill.
        oms = oms_with(fee_master(maker_bps=Decimal("-0.5"), taker_bps=Decimal("4"),
                                  conservative_unverified=True))
        order = child(post_only=True)
        fill(oms, order, "0.01")
        self.assertEqual((order.fee, order.fee_source), (Decimal("0"), "CONSERVATIVE_ASSUMED"))

    def test_e02_verified_rebate_is_kept_everywhere(self):
        master = master_with(FeeRule("BINANCE", Decimal("-0.5"), Decimal("4"), instrument_id=INSTRUMENT,
                                     source=FeeSource.VERIFIED, conservative_unverified=True))
        self.assertEqual(every_path(master, Liquidity.MAKER), (Decimal("-0.5"),) * 3)

    def test_e03_instrument_rule_wins_over_venue_rule(self):
        master = master_with(FeeRule("BINANCE", Decimal("1"), Decimal("9")),
                             FeeRule("BINANCE", Decimal("1"), Decimal("3"), instrument_id=INSTRUMENT))
        self.assertEqual(every_path(master, Liquidity.TAKER), (Decimal("3"),) * 3)

    def test_e04_each_trade_gets_the_rate_effective_at_its_time(self):
        master = master_with(FeeRule("BINANCE", Decimal("1"), Decimal("4"), 0, instrument_id=INSTRUMENT),
                             FeeRule("BINANCE", Decimal("1"), Decimal("3"), 1_000, instrument_id=INSTRUMENT))
        self.assertEqual(every_path(master, Liquidity.TAKER, 999), (Decimal("4"),) * 3)
        self.assertEqual(every_path(master, Liquidity.TAKER, 1_000), (Decimal("3"),) * 3)

    def test_strategy_edge_filter_uses_the_dated_schedule(self):
        # The sample books show a 10 bps gross edge (buy 60000, sell 60060): worth
        # taking at 2 x 4 bps of scheduled fees, not at 2 x 40 bps, whatever the
        # instrument default says.
        current = books()
        for taker, expected in (("4", True), ("40", False)):
            with self.subTest(taker=taker):
                master = master_with(*(FeeRule(v, Decimal("2"), Decimal(taker), 0, instrument_id=INSTRUMENT)
                                       for v in ("BINANCE", "BYBIT")))
                strategy = CrossVenueSpreadStrategy(
                    "s", INSTRUMENT, ("BINANCE", "BYBIT"), Decimal("0.01"), Decimal("1"),
                    10**12, Decimal("1"), master,
                )
                self.assertEqual(strategy.evaluate(current, 10) is not None, expected)

    def test_maker_simulator_uses_the_effective_maker_rate(self):
        from pathlib import Path
        from crypto_stat_arb.config import load_config

        from dataclasses import replace

        cfg, _ = load_config(Path(__file__).parents[1] / "config/research.json")
        cfg = replace(cfg, strategies={"maker_simulation": {"enabled": True}})
        cfg.instruments.fee_schedule = FeeSchedule(
            FeeRule(v, Decimal("-1"), Decimal("4"), instrument_id=cfg.instrument_id,
                    conservative_unverified=True) for v in cfg.venues
        )
        simulator = maker_simulator_from_config(cfg)
        self.assertEqual(simulator.maker_fee_bps, {v: 0.0 for v in cfg.venues})


if __name__ == "__main__":
    unittest.main()
