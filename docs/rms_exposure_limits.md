# RMS gross, net, instrument and venue limits

The RMS derives open exposure from immutable `FILL` ledger events and maps each fill to its parent strategy. Positions are valued using the current venue/instrument mark, falling back to the latest fill price when a mark is unavailable.

## Measures

| Scope | Measure | Admission rule |
| --- | --- | --- |
| Strategy | Gross notional: sum of absolute marked position notionals | Projected gross must not exceed the strategy override or global default |
| Strategy | Net notional: signed sum of marked position notionals | Absolute projected net must not exceed the strategy override or global default |
| Instrument | Gross notional across strategies and venues | Projected gross must not exceed the instrument override or global default |
| Venue | Gross notional across strategies and instruments | Projected gross must not exceed the venue override or global default |

`validate_pretrade()` adds the complete proposed package to ledger-derived positions without mutating state. Any binding limit rejects the parent before the first child dispatch.

After fills, `update_exposure_state()` publishes current values, limits and utilization ratios. A value above its limit creates a binding event and one durable `EXPOSURE_LIMIT_STATE_TRANSITION` from `NORMAL` to `BREACHED`; repeated observations while breached do not duplicate the transition. Breach activates the strategy kill switch. When exposure returns within bounds, the state transitions to `NORMAL`, but kill-switch reset remains an explicit operator decision.

## Configuration

Global defaults are `max_strategy_gross_notional`, `max_strategy_net_notional`, `max_instrument_gross_notional` and `max_venue_gross_notional`. Optional `strategy_gross_limits`, `strategy_net_limits`, `instrument_gross_limits` and `venue_gross_limits` maps override the corresponding default by exact key.

The run artifact is `exposure_utilization.json`, governed by `docs/schemas/exposure_utilization.schema.json`.
