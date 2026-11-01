# Python Strategy SDK

The versioned `crypto_stat_arb.sdk` package is the supported strategy boundary for Phase 1. A strategy receives one `StrategyContext` regardless of execution mode. Version `1.2.0` exposes canonical market data, deterministic replay, feature publication, ledger-derived portfolio state, RMS controls, OMS/EMS submission and offline advanced backtesting.

## Stable interfaces

| Context member | Contract |
| --- | --- |
| `data` | Current canonical event, configured reconstructed books and safe top-of-book access |
| `features` | Publish/read timestamped feature values with source event IDs |
| `portfolio` | Recalculate positions, average prices, realized/unrealized PnL, balances and available capacity from the ledger |
| `risk` | New-risk eligibility, reservation capacity and kill-switch state |
| `oms` | Submit an `Opportunity`, retrieve persistent plans and use the common OMS/EMS path |
| `backtest` | Run queue-, latency-, depth- and empirical-fill replay; fails closed outside `BACKTEST` mode |
| `mode` | Explicit `BACKTEST`, `PAPER` or `TESTNET`; strategy logic must not branch on connector implementation |

`StrategyRuntime.run()` orders canonical inputs with the production replay rule, updates sequence-aware books, enforces the book-integrity gate, publishes standard mid/spread features and invokes `strategy.on_event(event, context)`. Returning an opportunity sends it through the existing pre-trade RMS, reservation, parent plan, routing, callback, fill, residual and reconciliation logic.

## Minimal strategy port

`CrossVenueSDKStrategy` adapts the existing `CrossVenueSpreadStrategy` to the protocol without changing its signal logic. The required method is:

```python
def on_event(self, event: MarketEvent, context: StrategyContext) -> Opportunity | None:
    ...
```

Optional `on_start(context)` and `on_stop(context)` hooks are supported. See `examples/strategy_sdk.py` for an executable paper example.

## Mode-specific adapters

Paper and backtest modes use the deterministic venue adapter. Testnet mode requires one explicit `TestnetVenueAdapter` per configured venue and fails closed if any is missing. Each adapter passes the versioned canonical order request to an injected authenticated transport and maps its native acknowledgements, fills, cancellations or rejects back into canonical callbacks. Network and credentials never enter strategy code.

Authenticated testnet transports should be wrapped by `CredentialedTestnetTransport`, which requests only the required `TRADE` or `CANCEL` scope from the credential broker for the duration of one adapter call. See `docs/credential_security.md`.

The acceptance test runs the same strategy and canonical events in paper and testnet modes—even supplying events in reverse order to the testnet run—and asserts identical opportunity/plan IDs, canonical order requests and final portfolio state. The testnet transport is deterministic and performs no network or live-capital action.

Run the focused acceptance test:

```bash
PYTHONPATH=src python3 -m unittest tests.test_strategy_sdk -v
```

The result contract and representative evidence are `docs/schemas/strategy_sdk_acceptance.schema.json` and `data/samples/strategy_sdk_acceptance.json`.

The advanced engine imported from `new_backtest.zip` is exposed through
`AdvancedBacktestSpec`, `BacktestAPI`, and `context.backtest`. It adds
Tardis-layout replay, latency presets, queue-aware maker fills, taker depth
walking, maker/taker and taker/taker modes, inventory constraints, two-pass fill
model calibration, markouts, leg-risk attribution and batch aggregation. See
`docs/advanced_sdk_backtest.md`.
