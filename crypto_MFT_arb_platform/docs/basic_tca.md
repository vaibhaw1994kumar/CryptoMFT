# Basic transaction-cost analysis

Every backtest or paper-session close automatically writes `tca_report.json` and one UTC report per trading day under `tca_daily/YYYY-MM-DD.json`. The complete run report is also embedded under `tca` in `pipeline_monitoring.json`; its core numbers are exposed as `crypto_tca_*` Prometheus gauges.

## Statistics

Reports provide run totals and groups keyed by `(venue, instrument_id, order_type)`.

| Statistic | Definition |
| --- | --- |
| Fills | Child orders with positive cumulative filled quantity |
| Slippage | Signed adverse difference between average fill and dispatch decision/limit price; positive is worse, negative is improvement |
| Fees | Actual fee accumulated on each filled child |
| Reject/cancel/partial/timeout | Current canonical child outcome |
| Residual/unwind | Open residual base, unwind order/fill counts and unwind cost |
| Missed opportunity | A detected parent whose primary legs did not all fill or whose residual remained beyond the approved bound |

For buys, quote slippage is `(average_fill_price - benchmark_price) × filled_quantity`. For sells, it is `(benchmark_price - average_fill_price) × filled_quantity`. Portfolio slippage in basis points is:

$$
\text{slippage bps} =
\frac{\sum \text{signed quote slippage}}
{\sum (\text{filled quantity}\times\text{benchmark price})}\times 10{,}000.
$$

## Reproducibility and lineage

Each report declares `basic-tca-v1`, a deterministic report ID, canonical input path and SHA-256, the exact source event IDs behind every opportunity, resolved raw references and their coverage ratio. The same OMS state and canonical input therefore produce the same report bytes independent of output directory.

The published contract is `docs/schemas/tca_report.schema.json`; the worked daily example is `data/samples/orders/basic_tca_report.json`. `tests/test_tca.py` proves the signed slippage calculation, complete raw-reference lineage, byte-equivalent repeated calculation, automatic daily generation and pipeline/Prometheus integration.
