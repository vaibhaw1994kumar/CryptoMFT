"""Venue presets: fees, latency profiles, tick/lot metadata.

Everything here is a *default*. Override per-run from the CLI or by passing a
modified VenueSpec into the backtester.

Fee sign convention (same as backtest/bybit_backtester.py):
    positive bps = cost paid,  negative bps = rebate earned.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace


BYBIT = "bybit"
BINANCE = "binance"


# ── Fees ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FeeSpec:
    maker_bps: float
    taker_bps: float


# Bybit spot VIP-0 is 10bps/10bps; the MM desk runs PostOnly with a rebate tier.
# Binance USD-M futures VIP-0 is 2.0/5.0, with BNB discount 1.8/4.5.
FEE_PRESETS: dict[str, FeeSpec] = {
    "bybit_perp_vip0":   FeeSpec(maker_bps=2.0,  taker_bps=5.5),
    "bybit_perp_mm":     FeeSpec(maker_bps=-0.5, taker_bps=5.5),   # MM rebate tier
    "bybit_spot_vip0":   FeeSpec(maker_bps=10.0, taker_bps=10.0),
    "bybit_spot_mm":     FeeSpec(maker_bps=-0.5, taker_bps=6.0),   # matches bybit_backtester.py
    "binance_perp_vip0": FeeSpec(maker_bps=2.0,  taker_bps=5.0),
    "binance_perp_bnb":  FeeSpec(maker_bps=1.8,  taker_bps=4.5),
    "binance_perp_mm":   FeeSpec(maker_bps=0.0,  taker_bps=3.0),   # MM program tier
    "binance_spot_vip0": FeeSpec(maker_bps=10.0, taker_bps=10.0),
}


# ── Latency ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VenueLatencySpec:
    """One-way latencies in milliseconds.

    feed_ms      market data: exchange match -> our process sees it
    entry_ms     our order send -> exchange matching engine accepts it
    cancel_ms    our cancel send -> exchange applies it
    jitter_ms    std-dev of the lognormal around the mean
    """
    feed_ms: float
    entry_ms: float
    cancel_ms: float
    jitter_ms: float


# You cannot be co-located with both venues at once. Bybit matching runs out of
# AWS ap-southeast-1 (Singapore); Binance out of ap-northeast-1 (Tokyo).
# Tokyo <-> Singapore one-way is ~35ms. That asymmetry is the single biggest
# driver of cross-exchange arb viability, so it is modelled explicitly.
LATENCY_PRESETS: dict[str, dict[str, VenueLatencySpec]] = {
    # Co-located Singapore: fast to Bybit, slow to Binance.
    "singapore": {
        BYBIT:   VenueLatencySpec(feed_ms=2.0,  entry_ms=6.0,  cancel_ms=6.0,  jitter_ms=2.5),
        BINANCE: VenueLatencySpec(feed_ms=36.0, entry_ms=40.0, cancel_ms=40.0, jitter_ms=8.0),
    },
    # Co-located Tokyo: fast to Binance, slow to Bybit.
    "tokyo": {
        BYBIT:   VenueLatencySpec(feed_ms=36.0, entry_ms=40.0, cancel_ms=40.0, jitter_ms=8.0),
        BINANCE: VenueLatencySpec(feed_ms=2.0,  entry_ms=6.0,  cancel_ms=6.0,  jitter_ms=2.5),
    },
    # Midpoint host (e.g. Hong Kong) — symmetric but slower to both.
    "neutral": {
        BYBIT:   VenueLatencySpec(feed_ms=18.0, entry_ms=22.0, cancel_ms=22.0, jitter_ms=6.0),
        BINANCE: VenueLatencySpec(feed_ms=20.0, entry_ms=24.0, cancel_ms=24.0, jitter_ms=6.0),
    },
    # Physically impossible; use only as an upper bound in sensitivity sweeps.
    "colocated_both": {
        BYBIT:   VenueLatencySpec(feed_ms=1.0, entry_ms=3.0, cancel_ms=3.0, jitter_ms=1.0),
        BINANCE: VenueLatencySpec(feed_ms=1.0, entry_ms=3.0, cancel_ms=3.0, jitter_ms=1.0),
    },
}

DEFAULT_LATENCY_PRESET = "singapore"


# ── Venue ─────────────────────────────────────────────────────────────────────

@dataclass
class VenueSpec:
    name: str
    fees: FeeSpec
    latency: VenueLatencySpec
    # Tardis exchange id, used by fetch_tardis.py
    tardis_exchange: str = ""
    # If the feed carries local_ts we trust it as receive time; otherwise we
    # synthesise receive_ts = exchange_ts + feed latency sample.
    trust_local_ts: bool = True
    # Book deltas: how to interpret a "cancel" event's volume field.
    #   absolute    -> level is set to `volume`
    #   decremental -> level is reduced by `volume`
    #   hybrid      -> add/modify absolute, cancel decremental (Bybit collector)
    delta_semantics: str = "hybrid"
    # Full-snapshot resync cadence, seconds. Used to warn on book drift.
    snapshot_period_s: float = 1.0


def make_venues(
    latency_preset: str = DEFAULT_LATENCY_PRESET,
    bybit_fee_preset: str = "bybit_spot_mm",
    binance_fee_preset: str = "binance_perp_vip0",
) -> dict[str, VenueSpec]:
    lat = LATENCY_PRESETS[latency_preset]
    return {
        BYBIT: VenueSpec(
            name=BYBIT,
            fees=FEE_PRESETS[bybit_fee_preset],
            latency=lat[BYBIT],
            tardis_exchange="bybit",
            trust_local_ts=True,
            delta_semantics="hybrid",
            snapshot_period_s=1.0,
        ),
        BINANCE: VenueSpec(
            name=BINANCE,
            fees=FEE_PRESETS[binance_fee_preset],
            latency=lat[BINANCE],
            tardis_exchange="binance-futures",
            trust_local_ts=True,
            delta_semantics="absolute",
            snapshot_period_s=0.0,   # Tardis L2 is pure incremental after bootstrap
        ),
    }


def override_latency(venues: dict[str, VenueSpec], **per_venue_ms: float) -> dict[str, VenueSpec]:
    """e.g. override_latency(v, bybit_entry_ms=12.0, binance_feed_ms=50.0)."""
    out = {k: replace(v) for k, v in venues.items()}
    for key, value in per_venue_ms.items():
        venue, _, field_name = key.partition("_")
        if venue in out and hasattr(out[venue].latency, field_name):
            out[venue].latency = replace(out[venue].latency, **{field_name: float(value)})
    return out


# ── Instrument sizing ─────────────────────────────────────────────────────────

# Per-pair max position in BASE units. Keeps notional roughly comparable
# (~USD 20-50k) across instruments so cross-pair results are on one scale.
MAX_POSITION_BASE: dict[str, float] = {
    "BTCUSDT": 0.40,     "ETHUSDT": 12.0,    "BNBUSDT": 45.0,
    "SOLUSDT": 200.0,    "XRPUSDT": 25000.0, "DOGEUSDT": 150000.0,
    "ADAUSDT": 40000.0,  "AVAXUSDT": 900.0,  "LINKUSDT": 1600.0,
    "LTCUSDT": 300.0,    "DOTUSDT": 6000.0,  "TRXUSDT": 150000.0,
    "NEARUSDT": 8000.0,  "APTUSDT": 5000.0,  "ARBUSDT": 50000.0,
    "ATOMUSDT": 6000.0,  "AAVEUSDT": 180.0,  "ENAUSDT": 60000.0,
    "FILUSDT": 8000.0,   "INJUSDT": 1500.0,  "LDOUSDT": 20000.0,
    "MNTUSDT": 30000.0,  "ONDOUSDT": 30000.0,"OPUSDT": 25000.0,
    "SUIUSDT": 15000.0,  "TONUSDT": 8000.0,  "UNIUSDT": 3000.0,
    "VVVUSDT": 5000.0,   "WLDUSDT": 20000.0, "LITUSDT": 20000.0,
}

DEFAULT_MAX_POSITION_BASE = 1000.0


def max_position_for(pair: str) -> float:
    return MAX_POSITION_BASE.get(pair.upper(), DEFAULT_MAX_POSITION_BASE)


# The 30 pairs present in data/bybit/. Binance coverage must be fetched.
ALL_PAIRS: tuple[str, ...] = (
    "AAVEUSDT", "ADAUSDT", "APTUSDT", "ARBUSDT", "ATOMUSDT", "AVAXUSDT",
    "BNBUSDT", "BTCUSDT", "DOGEUSDT", "DOTUSDT", "ENAUSDT", "ETHUSDT",
    "FILUSDT", "INJUSDT", "LDOUSDT", "LINKUSDT", "LITUSDT", "LTCUSDT",
    "MNTUSDT", "NEARUSDT", "ONDOUSDT", "OPUSDT", "SOLUSDT", "SUIUSDT",
    "TONUSDT", "TRXUSDT", "UNIUSDT", "VVVUSDT", "WLDUSDT", "XRPUSDT",
)

# Widest window with near-complete coverage across data/bybit/.
DEFAULT_START_DATE = "2026-04-16"
DEFAULT_END_DATE = "2026-04-25"
