# Metric dictionary (GAP02)

`crypto_stat_arb.metrics` defines every reported metric once. Each `DICTIONARY` entry gives:

- the formula;
- the unit, numerator and denominator;
- the sign convention;
- which reports carry the metric.

The reports compute their metrics through the module's functions: TCA slippage, maker markouts, and the plan P&L sums in the summary, monitoring and economics. A metric name therefore means the same thing in every report. Each run also writes the dictionary into `metrics_report.json`.

## Sign conventions

| Kind | Positive means | Metrics |
|---|---|---|
| Cost | cost to us | `slippage_quote`, `weighted_slippage_bps`, `implementation_shortfall`, `plan_fees` |
| Markout | favourable: the price moved our way after the fill | `markout_bps` |
| P&L | profit | `plan_net_pnl`, `strategy_equity_pnl`, `maker_mark_to_market_pnl`, `time_weighted_return` |

## Names that used to collide

- **Net P&L is three different quantities:**
  - `plan_net_pnl`: trade P&L after fees and unwind cost, with any open position valued at the mid. Shown as `net_pnl` in `summary.json`, the TCA report and monitoring.
  - `strategy_equity_pnl`: the change in account equity. It also includes funding, borrow and transfer costs.
  - `maker_mark_to_market_pnl`: from the maker simulation, which isn't booked in the ledger.
- **Fills are counted three ways:**
  - `tca_fills`: orders with any fill.
  - `monitoring_order_fills`: fully filled orders only.
  - `maker_fill_ratio`: fill events per quote. Partial fills can push it above 1.
- **TCA slippage isn't implementation shortfall.** `weighted_slippage_bps` is measured against the child order's own limit price, so for a limit order it can only be zero or negative (price improvement).

## `metrics_report.json` (new, one per run)

| Field | Definition |
|---|---|
| `fill_rates.quantity_fill_rate` | filled / ordered quantity, primary orders only. Unwinds are excluded because they are sized to the residual. |
| `fill_rates.order_fill_rate` | share of primary orders with any fill |
| `implementation_shortfall` | paper P&L at the decision prices (quantity × (sell − buy)) minus realised `plan_net_pnl`. It is split into execution cost (filled primary quantity against the decision price), fees, and the rest (unfilled quantity, unwinds, the open position). Plans that flatten exposure taken over from a liquidation are excluded. |
| `implementation_shortfall_bps` | shortfall / decision notional (quantity × decision buy price) × 10 000 |
| `time_weighted_gross_exposure_base` | time-average of Σ \|position\| over venue and instrument, rebuilt from the ledger's fill events, in base units |
| `time_weighted_net_exposure_base` | time-average of \|position summed across venues\| per instrument: the exposure left after the cross-venue hedge |
| `time_weighted_return` | daily returns compounded: product of (1 + daily `plan_net_pnl` / equity at the start of the day), minus 1. Assumes no deposits or withdrawals. |

`metrics_report.json` is a separate file. `summary.json` and its `deterministic_result_sha256` are unchanged.

## Verification

- **Unchanged reports.** The 2026-10-22 regression produced identical output from the old and the new code for `summary.json`, `tca_report.json`, `economic_reconciliation.json`, `pipeline_monitoring.json` and `account_state.json`, in all three regression cases.
- **Two-venue sample.** Paper P&L (0.60) equals gross P&L, and the shortfall (0.48024, 8.0 bps) equals the fees exactly.
- **Mutation checks.** Each of these breaks a test:
  - flipping the slippage sign or the markout sign;
  - dropping fees from the shortfall split;
  - counting unwinds in the fill rate;
  - computing net exposure as gross;
  - not compounding the return;
  - the wrong bps scale;
  - swapping the decision prices.
