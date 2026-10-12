# Guarded Multi-Exchange Execution Adapters

The OKX and Deribit adapters are intentionally restricted to simulated
environments. They do not expose a live-mode switch.

## Modules

- `trading_platform_poc.exchange_adapter`: shared contracts, normalized order,
  trade, account, telemetry, and reconciliation snapshots.
- `trading_platform_poc.okx_demo.OKXDemoAdapter`: OKX V5 SPOT cash demo trading.
- `trading_platform_poc.deribit_test.DeribitTestAdapter`: Deribit HTTP API test
  trading.

The adapters and shared execution contracts are exported from
`hft_platform_kit`; direct module imports remain available for compatibility.

## Credentials

```text
OKX_DEMO_API_KEY
OKX_DEMO_API_SECRET
OKX_DEMO_PASSPHRASE

DERIBIT_TEST_CLIENT_ID
DERIBIT_TEST_CLIENT_SECRET
```

This US-targeted release accepts only the exact official
`https://us.okx.com` OKX origin and always sends
`x-simulated-trading: 1`. There is no silent fallback to the global origin.
Global and EEA deployments must use the separately documented origin for the
region where the account was registered and require a separately configured
regional build. Deribit accepts only
`https://test.deribit.com/api/v2`. URL suffixes, prefixes, credentials,
non-default ports, and live Deribit endpoints are rejected.

Both adapters:

- use deterministic venue-safe client identifiers;
- normalize quantity and side-aware price to instrument filters;
- expose the last HTTP/rate/error timing telemetry;
- never blindly retry order submission after transport errors, HTTP 5xx, or
  OKX code `50004` at any HTTP status;
- reconcile an ambiguous submission using the deterministic client identifier;
- bounded-poll ambiguous cancellations and accept only documented terminal
  cancelled/filled states as conclusive;
- exhaust Deribit recent and historical order-history pagination for closed
  label lookups;
- provide open-order, trade, account, and local-vs-exchange snapshots.

## EMS canonical instruments

EMS routing uses explicit canonical-to-venue mappings and never treats products
from different compatibility groups as fungible:

- `BTCUSDT` / `btc-usdt-spot`: Binance `BTCUSDT` and OKX `BTC-USDT`,
  with canonical and venue quantities expressed in base BTC.
- `BTC-PERPETUAL` / `btc-usd-inverse-perpetual`: Deribit
  `BTC-PERPETUAL`, with canonical quantity expressed in base BTC and venue
  `amount` expressed directly in USD. For example, 0.001 BTC at USD 100,000
  is submitted as `amount=100`, not as 10 contracts.

Deribit inverse perpetual liquidity is therefore available as a distinct
multi-leg hedge, but it is never included in spot SOR comparisons. The EMS
converts quote sizes, route quantities, and lot sizes between canonical and
venue units using the route reference price. Fills are converted using the
exchange-reported `average_fill_price`, never the reference price, so that
slippage cannot hide realised exposure. Canonical quantities remain the basis
for risk reservation and public execution results.

`QuantityUnit.CONTRACTS` remains available for APIs that explicitly submit a
contract count. Deribit's `private/buy` and `private/sell` also expose a
separate `contracts` parameter, but this adapter deliberately submits `amount`,
so inverse perpetual mappings use `QuantityUnit.QUOTE`.

Deribit linear instruments (for example `ETH_USDC-PERPETUAL`) express `amount`
in the base currency, so they map to `QuantityUnit.BASE` and need no price
conversion. The denomination is derived solely from `instrument_type`
(`linear` vs `reversed`); `settlement_period` is never used, because linear
perpetuals would otherwise be misread as USD-denominated and over-ordered by a
factor of the price. Futures whose `instrument_type` is absent are rejected
rather than guessed. Linear instruments are also indexed by their settlement
currency (USDC/USDT) rather than their base currency, so instrument metadata is
fetched with `currency=any` and reconciliation lookups use the settlement
currency reported in the instrument metadata.

Deribit `future_combo` and `option_combo` configuration is rejected. Official
metadata exposes a single combo amount and contract size, while combo
definitions contain signed leg-size multipliers. This execution and risk model
does not retain those legs or calculate leg-level exposure, so accepting combo
configuration would create an instrument that could be submitted but could not
be safely reserved, reconciled, or unwound.

Market-order preflight passes the route reference price to adapter
normalization so venue minimum-notional checks run before dispatch.

## Scheduled execution reconciliation

TWAP/VWAP execution tracks every planned route at schedule level. Exchange
filled quantity is converted back to canonical units and retained
monotonically, so a later sparse acknowledgement cannot erase an earlier
partial fill. Accepted/open/partially-filled routes are queried before a
requested cancellation, and every cancel attempt is followed by another
query. A late fill therefore remains exposure rather than being reported as a
successful cancellation.

`ScheduleState.RECOVERY_REQUIRED` covers ambiguous queries, failed cancels, and
non-terminal post-cancel states. `max_reserved_notional` remains reserved while
any route is live, ambiguous, or unreconciled. Automatic release requires all
planned routes, including skipped future slices, to be confirmed terminal and
the schedule to be either fully unfilled or fully filled. A terminal partial
fill enters `ScheduleState.UNWIND_REQUIRED`; `residual_canonical_exposure`
reports the amount that must be hedged or unwound. Successful recovery is
recorded with
`complete_schedule_recovery(..., unwind_completed=True,
recovery_orders=...)`; each hedge/unwind order is independently queried and
must have a distinct coordinator-wide venue/order identity.
Bare operator resolution is rejected. `resolve_schedule_recovery` requires a
reason and external evidence, plus terminal per-order evidence for every
submitted route that query/cancel reconciliation leaves live, partially
filled, ambiguous, or unreconciled; partial exposure additionally requires
distinct verified recovery orders before release.

This is an internal conservative risk contract. Venue APIs provide independent
order states and cancellation acknowledgements; they do not provide an atomic
transaction spanning all TWAP/VWAP slices or venues.

Multi-leg plans apply the same conservative contract through
`complete_plan_recovery`. Confirmed, reconciled hedge/unwind order evidence is
recorded on the plan and must reduce signed canonical exposure to zero for
every symbol. `resolve_plan_recovery` is the separate operator path for
externally completed recovery and requires both a reason and external
evidence. Neither path can release risk while an original route remains live,
ambiguous, or unreconciled. Repeated completion calls do not release twice.
Recovery fill evidence is deduplicated by venue and exchange order ID, with
venue and client order ID used when the exchange ID is absent; duplicate or
conflicting evidence is rejected regardless of caller-provided `recovery_id`.

## Dependency and export notes

No new dependency is required; the adapters use the existing `aiohttp`
dependency. The public SDK exports the shared contracts, coordinator, and all
three guarded adapters from `hft_platform_kit/__init__.py`.

## Official documentation used (accessed 2026-07-31)

### OKX V5

- https://app.okx.com/docs-v5/en/#overview-demo-trading-services
- https://app.okx.com/docs-v5/en/#overview-rest-authentication
- https://app.okx.com/docs-v5/en/#public-data-rest-api-get-system-time
- https://app.okx.com/docs-v5/en/#public-data-rest-api-get-instruments
- https://app.okx.com/docs-v5/en/#order-book-trading-trade-post-place-order
- https://app.okx.com/docs-v5/en/#order-book-trading-trade-post-cancel-order
- https://app.okx.com/docs-v5/en/#order-book-trading-trade-get-order-details
- https://app.okx.com/docs-v5/en/#order-book-trading-trade-get-order-list
- https://app.okx.com/docs-v5/en/#order-book-trading-trade-get-transaction-details-last-3-days
- https://app.okx.com/docs-v5/en/#trading-account-rest-api-get-balance
- https://app.okx.com/docs-v5/en/#error-code-rest-api-public-general-class

### Deribit API 2.1.1

- https://docs.deribit.com/
- https://docs.deribit.com/articles/authentication
- https://docs.deribit.com/articles/rate-limits
- https://docs.deribit.com/articles/errors
- https://docs.deribit.com/api-reference/supporting/public-get_time
- https://docs.deribit.com/api-reference/market-data/public-get_instruments
- https://docs.deribit.com/api-reference/market-data/public-get_instrument
- https://docs.deribit.com/api-reference/combo-books/private-create_combo
- https://docs.deribit.com/api-reference/trading/private-buy
- https://docs.deribit.com/api-reference/trading/private-sell
- https://docs.deribit.com/api-reference/trading/private-cancel
- https://docs.deribit.com/api-reference/trading/private-get_order_state
- https://docs.deribit.com/api-reference/trading/private-get_open_orders
- https://docs.deribit.com/api-reference/trading/private-get_open_orders_by_label
- https://docs.deribit.com/api-reference/trading/private-get_order_history_by_currency
- https://docs.deribit.com/api-reference/trading/private-get_user_trades_by_instrument
- https://docs.deribit.com/api-reference/account-management/private-get_account_summary
- https://docs.deribit.com/api-reference/market-data/public-get_instruments
