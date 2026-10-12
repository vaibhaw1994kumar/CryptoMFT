"""Research-grade multi-venue stat-arb platform core."""

__version__ = "0.1.0"

from .sdk import (  # noqa: E402,F401
    STRATEGY_SDK_VERSION,
    CrossVenueSDKStrategy,
    RuntimeMode,
    StrategyContext,
    StrategyProtocol,
    StrategyRunResult,
    StrategyRuntime,
    TestnetVenueAdapter,
)
