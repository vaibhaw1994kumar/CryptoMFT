# Config validation and the capability matrix

## Config keys are validated at load (SDK02)

`load_config` rejects any key the platform doesn't read. Each error names the full path and the closest known key:

```
ValueError: unknown config key(s): strategy.entry_bpz (did you mean 'entry_bps'?);
risk.margin_accounts.BINANCE.margn_enabled (did you mean 'margin_enabled'?); telemetry
```

- **The schema.** `crypto_stat_arb.config_schema.SCHEMA` lists exactly what the loader and the subsystems it configures read:
  - strategy, instruments, fees;
  - risk (with margin, borrow, transfers and inventory);
  - oms, time, quality, monitoring;
  - latency profiles, performance, strategies, funding.
- **Data maps take any key.** Maps keyed by venue, instrument or strategy id accept any key (e.g. `risk.initial_capital_by_venue.OKX`).
- **Comments.** Keys starting with `_` are comments and are allowed anywhere.
- **Adding a key.** A new config key needs an entry in `SCHEMA`; the error says so.
- **How the schema was built.** It came from tracking every key the code reads while loading each shipped config and running a backtest with it. Every key in the shipped configs is read, except `watch_notes`, a free-text note kept as a known key.
- **Out of scope.** Value ranges and types are still checked by the individual loaders (e.g. the margin, borrow and transfer sections), not by the schema. `trading_platform_poc` (the platform-kit stack) has its own pydantic config and isn't covered here.

## Venue capability matrix (REF05)

`crypto_stat_arb.capabilities` records, per venue and product:

- order types, time in force, how post-only is expressed;
- whether reduce-only is venue-enforced;
- amend, self-trade prevention modes;
- rate limits with their scope (IP, account, UID per endpoint);

and, per adapter, what our transport actually implements.

```
PYTHONPATH=src python -m crypto_stat_arb.cli capabilities [--venue BYBIT]
```

| | Binance spot | Binance USDⓈ-M | Bybit spot | Bybit linear |
|---|---|---|---|---|
| Post-only | `LIMIT_MAKER` | `GTX` | `PostOnly` | `PostOnly` |
| Reduce-only | none: **platform-only** | venue | none: **platform-only** | venue |
| Amend at the venue | yes (keep-priority, decrease only) | yes (`PUT /fapi/v1/order`) | yes | yes |
| Our testnet adapter: replace | **no** | **no** | **no** | **no** |
| Self-trade prevention | EXPIRE_TAKER / MAKER / BOTH | same | CancelMaker / Taker / Both (assumed) | same |
| Rate-limit scopes | IP weight, account orders (numbers assumed) | same | IP 600/5 s (documented), per UID per endpoint | same |

### What it changes

- **Routes are configured from the matrix.** Every route is configured from it: `TradingSession` sets order types, time in force, post-only and replace. The venue's capabilities are intersected with the adapter's: the internal adapter in simulation and paper mode, the transport on testnet. The router refuses what the route can't carry out, before sending.
- **Testnet routes refuse `REPLACE`.** The testnet transports treated a replace as a new submit, which would have placed a second order and left the first live. Cancel and resubmit instead.
- **Reduce-only on spot is platform-only.** It's accepted, because the OMS sizes unwinds to the residual, but the venue can't reject an order that would flip the position. The matrix states this rather than implying venue protection.
- **Unknown venue/product combinations fail closed.** A strategy leg on a venue/product with no matrix entry fails at config load.

### Where the facts come from

Every venue fact is labelled `DOCUMENTED` (checked against the venue's API documentation, URLs in `sources`) or `ASSUMED` (listed in `assumed`).

- **Assumed as of 2026-10-25:** the Binance rate-limit numbers, Binance spot amend semantics, Bybit's self-trade-prevention values, and which of Bybit's documented 10/s and 20/s per-UID limits apply to spot and which to linear.
- **Verify the assumed items before sizing a quoting strategy.** Rate limits by scope are the next requirement (EMS02).

For the shipped venues and products, simulation and paper behaviour is unchanged: the 2026-10-22 regression baseline is identical.
