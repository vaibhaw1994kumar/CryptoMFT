from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from trading_platform_poc.config import AppConfig
from trading_platform_poc.engine import TradingEngine
from trading_platform_poc.replay import ReplaySource

from hft_platform_kit.protocols import Strategy


def create_engine(
    config: AppConfig | None = None,
    *,
    strategy: Strategy | None = None,
    **components,
) -> TradingEngine:
    """Create an injectable trading engine for embedding in another codebase."""

    effective_config = config or AppConfig()
    return TradingEngine(
        effective_config,
        strategy=strategy,
        **components,
    )


async def replay_dataset(
    data_path: str | Path,
    config: AppConfig | None = None,
    *,
    speed: str | float = "max",
    strategy: Strategy | None = None,
) -> TradingEngine:
    """Replay a dataset and return the populated engine for programmatic use."""

    engine = create_engine(config, strategy=strategy)
    source = ReplaySource(Path(data_path), uuid4())
    final_timestamp_ns = 0
    async for event in source.paced_events(speed):
        final_timestamp_ns = event.receive_timestamp_ns
        engine.process(event)
    if not engine.book.initialized:
        raise ValueError("dataset did not contain a usable book snapshot")
    engine.close(final_timestamp_ns)
    return engine
