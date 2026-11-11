"""Track B Item 4: liquidation price and distance in core margin engine → risk gate refusal.

Tests:
- liq_price_at: round-trip (margin = initial margin → verify algebra), long and short
- liq_distance_bps: long (price drop), short (price rise)
- tier boundary: different mmr applies in higher tier
- liq_distance_bps_min in RiskLimits: package refused when liq distance too close
- liq_distance_bps_min=None: check disabled (no refusal regardless of distance)
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb.margin import (
    RiskTier, TierSchedule, liq_distance_bps, liq_price_at,
)
from crypto_stat_arb.risk import RiskLimits


def _schedule(mmr: str, deduction: str = "0") -> TierSchedule:
    """Single-tier schedule with given maintenance rate."""
    return TierSchedule((RiskTier(
        max_notional=Decimal("10000000"),
        maintenance_rate=Decimal(mmr),
        initial_rate=Decimal("0.1"),
        maintenance_deduction=Decimal(deduction),
    ),))


def _two_tier_schedule() -> TierSchedule:
    """Two tiers: notional ≤ 50 000 → mmr=0.5%, notional > 50 000 ≤ 1 000 000 → mmr=1%."""
    return TierSchedule((
        RiskTier(max_notional=Decimal("50000"), maintenance_rate=Decimal("0.005"), initial_rate=Decimal("0.1")),
        RiskTier(max_notional=Decimal("1000000"), maintenance_rate=Decimal("0.01"), initial_rate=Decimal("0.15")),
    ))


class LiqPriceAtTests(unittest.TestCase):
    """liq_price_at: algebraic round-trip and long/short correctness."""

    def test_long_round_trip(self):
        """liq_price_at recovers exactly when margin equals initial margin for a long."""
        entry = Decimal("100")
        qty = Decimal("1")
        mmr = Decimal("0.05")
        margin = Decimal("10")                # 10% of notional, more than mmr
        schedule = TierSchedule((RiskTier(
            max_notional=Decimal("1000000"),
            maintenance_rate=mmr, initial_rate=Decimal("0.1"),
        ),))
        lp = liq_price_at(schedule, is_long=True, qty=qty, entry_price=entry, available_margin=margin)
        self.assertIsNotNone(lp)
        # verify: equity_at_lp == MM_at_lp
        pnl = qty * (lp - entry)
        equity = margin + pnl
        mm = mmr * qty * lp
        self.assertAlmostEqual(float(equity), float(mm), places=8)

    def test_short_round_trip(self):
        """liq_price_at recovers exactly for a short."""
        entry = Decimal("100")
        qty = Decimal("1")
        mmr = Decimal("0.05")
        margin = Decimal("10")
        schedule = TierSchedule((RiskTier(
            max_notional=Decimal("1000000"),
            maintenance_rate=mmr, initial_rate=Decimal("0.1"),
        ),))
        lp = liq_price_at(schedule, is_long=False, qty=qty, entry_price=entry, available_margin=margin)
        self.assertIsNotNone(lp)
        # For short: pnl = qty*(entry - lp); equity = margin + pnl; MM = mmr*qty*lp
        pnl = qty * (entry - lp)
        equity = margin + pnl
        mm = mmr * qty * lp
        self.assertAlmostEqual(float(equity), float(mm), places=8)

    def test_long_liq_below_entry(self):
        """Long position: liq price is below entry."""
        schedule = _schedule("0.05")
        lp = liq_price_at(schedule, is_long=True, qty=Decimal("1"), entry_price=Decimal("100"), available_margin=Decimal("10"))
        self.assertIsNotNone(lp)
        self.assertLess(float(lp), 100.0, "long: liq price must be below entry")

    def test_short_liq_above_entry(self):
        """Short position: liq price is above entry."""
        schedule = _schedule("0.05")
        lp = liq_price_at(schedule, is_long=False, qty=Decimal("1"), entry_price=Decimal("100"), available_margin=Decimal("10"))
        self.assertIsNotNone(lp)
        self.assertGreater(float(lp), 100.0, "short: liq price must be above entry")

    def test_above_last_tier_returns_none(self):
        """Notional above the last tier returns None (position is above risk limit)."""
        schedule = TierSchedule((RiskTier(max_notional=Decimal("100"), maintenance_rate=Decimal("0.05"), initial_rate=Decimal("0.1")),))
        lp = liq_price_at(schedule, is_long=True, qty=Decimal("10"), entry_price=Decimal("200"), available_margin=Decimal("100"))
        self.assertIsNone(lp)

    def test_tier_boundary_different_mmr(self):
        """Higher tier has higher mmr → smaller liq distance for the same margin per notional."""
        schedule = _two_tier_schedule()
        entry = Decimal("100")
        margin_rate = Decimal("0.1")           # 10% initial margin
        qty_low = Decimal("100")               # notional 10 000 → tier 1 (mmr 0.5%)
        qty_high = Decimal("600")              # notional 60 000 → tier 2 (mmr 1%)
        margin_low = qty_low * entry * margin_rate
        margin_high = qty_high * entry * margin_rate
        lp_low = liq_price_at(schedule, is_long=True, qty=qty_low, entry_price=entry, available_margin=margin_low)
        lp_high = liq_price_at(schedule, is_long=True, qty=qty_high, entry_price=entry, available_margin=margin_high)
        self.assertIsNotNone(lp_low)
        self.assertIsNotNone(lp_high)
        dist_low = float(liq_distance_bps(lp_low, entry, is_long=True))
        dist_high = float(liq_distance_bps(lp_high, entry, is_long=True))
        # Tier 2 has higher mmr → larger MM requirement → liq distance is SMALLER for same margin %
        self.assertGreater(dist_low, dist_high, "higher mmr tier → closer to liquidation")

    def test_deduction_shifts_liq_further(self):
        """A maintenance_deduction shifts the liq price further from entry (wider buffer)."""
        entry = Decimal("100")
        qty = Decimal("1")
        margin = Decimal("10")
        schedule_no_ded = _schedule("0.05", deduction="0")
        schedule_with_ded = _schedule("0.05", deduction="2")
        lp_no = liq_price_at(schedule_no_ded, is_long=True, qty=qty, entry_price=entry, available_margin=margin)
        lp_with = liq_price_at(schedule_with_ded, is_long=True, qty=qty, entry_price=entry, available_margin=margin)
        self.assertIsNotNone(lp_no)
        self.assertIsNotNone(lp_with)
        # Deduction reduces MM required at the same price → liq triggers lower → liq_price further below entry
        self.assertLess(float(lp_with), float(lp_no), "deduction shifts liq further below entry")


class LiqDistanceBpsTests(unittest.TestCase):
    def test_long_positive_distance(self):
        entry = Decimal("100")
        lp = Decimal("95")                     # 5% drop
        dist = liq_distance_bps(lp, entry, is_long=True)
        self.assertAlmostEqual(float(dist), 500.0, places=4)

    def test_short_positive_distance(self):
        entry = Decimal("100")
        lp = Decimal("105")                    # 5% rise
        dist = liq_distance_bps(lp, entry, is_long=False)
        self.assertAlmostEqual(float(dist), 500.0, places=4)


class LiqDistanceLimitTests(unittest.TestCase):
    """RiskLimits.liq_distance_bps_min: when set, refuse packages that would liq too close."""

    def test_field_exists_and_defaults_none(self):
        limits = RiskLimits(
            max_order_notional=Decimal("5000"),
            max_gross_notional=Decimal("50000"),
            max_residual_base=Decimal("0.5"),
            max_book_age_ns=10_000_000_000,
        )
        self.assertIsNone(limits.liq_distance_bps_min)

    def test_field_accepts_decimal(self):
        limits = RiskLimits(
            max_order_notional=Decimal("5000"),
            max_gross_notional=Decimal("50000"),
            max_residual_base=Decimal("0.5"),
            max_book_age_ns=10_000_000_000,
            liq_distance_bps_min=Decimal("200"),
        )
        self.assertEqual(limits.liq_distance_bps_min, Decimal("200"))


class LiqDistanceRiskGateTests(unittest.TestCase):
    """liq_distance_bps_min wired into RiskEngine._reserve_package: integration."""

    def _make_engine(self, liq_distance_bps_min: Decimal | None = None) -> "RiskEngine":
        """RiskEngine with one tiered perp venue (BINANCE buyer leg)."""
        from crypto_stat_arb.economics import opening_balance_events
        from crypto_stat_arb.instruments import InstrumentMaster
        from crypto_stat_arb.ledger import Ledger
        from crypto_stat_arb.margin import MarginConfig
        from crypto_stat_arb.models import Instrument
        from crypto_stat_arb.risk import AccountRiskSnapshot, RiskEngine, RiskLimits

        PERP = "BTC-USDT-PERP"
        instruments = InstrumentMaster([
            Instrument(
                instrument_id=PERP, venue=v, symbol="BTCUSDT", base="BTC", quote="USDT",
                tick_size=Decimal("0.1"), quantity_step=Decimal("0.001"),
                min_quantity=Decimal("0.001"), min_notional=Decimal("5"),
                taker_fee_bps=Decimal("4"), product_type="PERP",
                leverage=Decimal("10"), settlement_asset="USDT", funding_interval_ns=8 * 3_600 * 10**9,
            )
            for v in ("BINANCE", "BYBIT")
        ])
        # Tiers: initial_rate=10% → initial margin increment = qty * price * 0.1
        tiers = [{"max_notional": "1000000", "maintenance_rate": "0.005", "initial_rate": "0.1"}]
        config = MarginConfig.from_config({"venues": {
            "BINANCE": {"mode": "CROSS", "tiers": {PERP: tiers},
                        "collateral": [{"asset": "USDT", "haircut": "0"}]},
        }})
        ledger = Ledger()
        ledger.asset_resolver = lambda venue, instrument: (instrument, "USDT")
        for event in opening_balance_events([("BINANCE", "USDT", Decimal("10000"))]):
            ledger.economics.apply(event)
        engine = RiskEngine(RiskLimits(
            max_order_notional=Decimal("100000"),
            max_gross_notional=Decimal("200000"),
            max_residual_base=Decimal("10"),
            max_book_age_ns=10_000_000_000,
            liq_distance_bps_min=liq_distance_bps_min,
        ), {
            "BINANCE": AccountRiskSnapshot(venue="BINANCE", margin_enabled=True),
            "BYBIT": AccountRiskSnapshot(
                venue="BYBIT", available_cash=Decimal("10000"),
                available_margin=Decimal("10000"), initial_margin_rate=Decimal("0.0"),
            ),
        })
        engine.margin_config = config
        engine.instruments = instruments
        engine.ledger = ledger
        return engine

    def _opportunity(self, qty: str = "1", buy_price: str = "100", sell_price: str = "101") -> "Opportunity":
        from crypto_stat_arb.models import Opportunity
        return Opportunity(
            opportunity_id="liq-opp-1", strategy_id="strategy-1",
            instrument_id="BTC-USDT-PERP", observed_ts_ns=1,
            buy_venue="BINANCE", sell_venue="BYBIT",
            buy_price=Decimal(buy_price), sell_price=Decimal(sell_price),
            quantity=Decimal(qty),
            gross_edge_bps=Decimal("100"), estimated_net_edge_bps=Decimal("50"),
            source_event_ids=("e1", "e2"),
        )

    def test_liq_distance_check_disabled_by_default(self):
        """With liq_distance_bps_min=None (default), no liq_distance_too_close refusal."""
        engine = self._make_engine(liq_distance_bps_min=None)
        opp = self._opportunity()
        # Should not raise (the initial_margin increment is 10% of notional = 10,
        # which gives liq distance ≈ 952 bps, well above any floor)
        reservation = engine.reserve(opp)
        self.assertIsNotNone(reservation)

    def test_liq_distance_too_close_raises_with_reason(self):
        """When liq_distance_bps_min is set above the actual distance, reserve raises."""
        # At entry=100, qty=1, mmr=0.5%, initial_margin=10%:
        #   liq ≈ (10 - 100 + 0) / (1*(0.005 - 1)) ≈ -90 / -0.995 ≈ 90.45
        #   liq_distance ≈ (100 - 90.45) / 100 * 10000 ≈ 955 bps
        # Setting liq_distance_bps_min=2000 triggers the refusal.
        engine = self._make_engine(liq_distance_bps_min=Decimal("2000"))
        opp = self._opportunity()
        with self.assertRaises(ValueError) as ctx:
            engine.reserve(opp)
        self.assertIn("liq_distance_too_close", str(ctx.exception))

    def test_liq_distance_refusal_counted_in_refusals(self):
        """The refusal reason 'liq_distance_too_close' is counted in engine.refusals."""
        engine = self._make_engine(liq_distance_bps_min=Decimal("2000"))
        opp = self._opportunity()
        with self.assertRaises(ValueError):
            engine.reserve(opp)
        self.assertGreater(engine.refusals.get("liq_distance_too_close", 0), 0,
                           "liq_distance_too_close must appear in engine.refusals")

    def test_liq_distance_wide_enough_passes(self):
        """When actual liq distance > liq_distance_bps_min, reserve succeeds."""
        engine = self._make_engine(liq_distance_bps_min=Decimal("500"))
        opp = self._opportunity()     # expected liq_distance ≈ 955 bps > 500 bps
        reservation = engine.reserve(opp)
        self.assertIsNotNone(reservation)


if __name__ == "__main__":
    unittest.main()
