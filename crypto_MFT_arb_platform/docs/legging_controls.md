# OMS/EMS legging controls

Legging risk is evaluated after each child fill and whenever an open residual is rechecked. The control separates three limits:

| Limit | Measure | Binding action |
| --- | --- | --- |
| One-sided exposure | Absolute residual base quantity multiplied by the highest available venue mid-price | Dispatch the remaining exposure-reducing package leg; otherwise enter deterministic hedge/unwind |
| Residual size | Absolute signed base quantity across all filled children | Dispatch the remaining hedge or create a persistent unwind child |
| Residual duration | Local logical time since the imbalance first appeared | Cancel passive work and escalate to aggressive market IOC unwind |

`LEGGING_LIMIT_BOUND` contains the measured residual, marked one-sided notional, elapsed time, binding codes, chosen action and intended hedge order IDs. `LEGGING_LIMIT_CLEARED` proves that the package returned within all bounds.

## Control interaction

The router distinguishes local package risk reduction from the exchange's `reduce_only` order semantic. A second cross-venue primary leg can reduce package exposure without reducing an existing position at that destination venue, so it carries `risk_reducing=true` and `reduce_only=false`. An actual unwind child carries both.

Once a leg is open, emergency risk-reducing work may override the global kill switch, a locally disabled venue/account control, or ordinary rate-limit capacity when the corresponding route policy permits it. These exceptions cannot admit new risk: the OMS first proves that the order side reduces the signed parent residual. Each exception produces `LEGGING_CONTROL_OVERRIDE` with the exact controls and action. If no eligible route or tradable book exists, the plan remains in `HEDGE_UNWIND`, retains its reservation and emits a residual breach rather than closing silently.

The controlled test fills the first Binance leg, binds both residual-size and one-sided-notional limits, activates the global stop, disables Bybit and exhausts its ordinary message capacity. The Bybit hedge is admitted only as risk-reducing, the exception is ledgered, and the plan ends flat.
