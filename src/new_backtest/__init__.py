"""Cross-venue (Bybit x Binance) arbitrage backtester.

Entry points:

    python -m new_backtest.fetch_tardis   download|normalize   # get Binance data
    python -m new_backtest.backtester     --pair BTCUSDT ...   # one pair
    python -m new_backtest.batch_backtest --pairs ... ...      # all pairs

Imports are lazy: pulling in `new_backtest` alone does not drag in
altcoin_mm_v1, so the fetcher runs in environments where the strategy stack is
not installed.
"""
from __future__ import annotations

__all__ = [
    "ArbBacktestConfig", "ArbBacktestResult", "CrossVenueArbBacktester",
    "ArbConfig", "CrossVenueArbStrategy", "InventoryBook",
    "BatchSummary", "run_batch",
    "OrderBook", "CrossVenueFeed", "LatencyBook", "PendingOrderQueue",
    "VenueState", "TakerExecutor", "MakerExecutor",
    "make_venues", "ALL_PAIRS",
    "run_preflight",
]


def __getattr__(name: str):
    if name in ("ArbBacktestConfig", "ArbBacktestResult", "CrossVenueArbBacktester"):
        from . import backtester
        return getattr(backtester, name)
    if name in ("ArbConfig", "CrossVenueArbStrategy", "InventoryBook"):
        from . import arb_strategy
        return getattr(arb_strategy, name)
    if name in ("BatchSummary", "run_batch"):
        from . import batch_backtest
        return getattr(batch_backtest, name)
    if name == "OrderBook":
        from .order_book import OrderBook
        return OrderBook
    if name == "CrossVenueFeed":
        from .cross_venue_feed import CrossVenueFeed
        return CrossVenueFeed
    if name in ("LatencyBook", "PendingOrderQueue"):
        from . import latency_model
        return getattr(latency_model, name)
    if name == "VenueState":
        from .venue_state import VenueState
        return VenueState
    if name in ("TakerExecutor", "MakerExecutor"):
        from . import fill_engine
        return getattr(fill_engine, name)
    if name in ("make_venues", "ALL_PAIRS"):
        from . import config
        return getattr(config, name)
    if name == "run_preflight":
        from .quality_preflight import run_preflight
        return run_preflight
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
