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
- **Verify the assumed items before sizing a quoting strategy.** They now constrain sessions through the scoped rate limits below.

## Rate limits by scope, with room kept for cancels (EMS02)

Every session enforces the matrix's rate limits per scope, in `crypto_stat_arb.rate_limits`, on top of the router's older per-venue budget.

- **Buckets:**
  - IP request weight, keyed by venue and host. Bybit's IP limit is shared by spot and linear.
  - Account order count, keyed by venue and product.
  - Per-user, per-endpoint limits, keyed by venue, product and endpoint.
- **Windows:** sliding, on the event clock, so replays are deterministic.
- **What each request costs** (`ACTION_COSTS`; assumed unless the matrix says otherwise):
  - Binance: new orders and amends count toward the account order limit, while cancels and queries spend only IP weight.
  - Bybit: every request spends the IP limit, and each endpoint also spends its own limit.
- **All buckets at once:** a request is admitted only if every bucket it touches has room. Then all of them are charged; a refusal charges none.

| Tier | May use | Used by |
|---|---|---|
| New risk | capacity x (1 - cancel reserve - risk-reducing reserve) | new orders |
| Risk-reducing | capacity x (1 - cancel reserve) | reduce-only orders, unwinds |
| Cancel | full capacity | cancels, order queries |

- **Nothing exceeds full capacity**, not even a risk-reducing order: the venue would answer with 429s or a ban.
- **New orders** over their share are rejected or queued according to the route's policy. A queued order is released when every bucket has room.
- **Cancels are never refused locally:**
  - With a latency model, a cancel waits until there is room; `cancels_delayed` counts these. The order stays CANCEL_PENDING, so it is still live for risk.
  - Without one, it is sent anyway and flagged; `over_limit_sent` counts these.
- **Queries** spend IP weight in the cancel tier.

Each throttle trace record names the binding bucket and whether its limit is DOCUMENTED or ASSUMED. The summary's `execution_api.rate_limits.scoped` counts throttles by status, and its utilisation feeds the monitoring `max_utilization` metric.

Configuration lives in `oms.rate_limits`: `enabled` (default true), `cancel_reserve` (0.2) and `risk_reducing_reserve` (0.1). On the 2026-10-22 regression no request was throttled or delayed; the busiest bucket reached 5%.

For the shipped venues and products, simulation and paper behaviour is unchanged: the 2026-10-22 regression baseline is identical.
