from pathlib import Path

from crypto_stat_arb.config import load_config
from crypto_stat_arb.runner import load_events
from crypto_stat_arb.sdk import (
    CrossVenueSDKStrategy,
    RuntimeMode,
    StrategyRuntime,
)


root = Path(__file__).resolve().parents[1]
config, _ = load_config(root / "config/research.json")
events = load_events(root / "data/samples/two_venue_books.jsonl")

# The strategy object and canonical event interface stay the same when the
# runtime mode is changed. TESTNET additionally requires explicit venue
# adapters backed by authenticated testnet transports.
runtime = StrategyRuntime(
    config,
    CrossVenueSDKStrategy(config),
    mode=RuntimeMode.PAPER,
)
result = runtime.run(events)

print(result.to_dict())
print(runtime.context.features.snapshot())
print(runtime.context.portfolio.snapshot())
