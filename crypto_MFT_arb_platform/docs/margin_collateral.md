# Margin and collateral control

Margin control applies only when an `AccountRiskSnapshot` has `margin_enabled=true`, has a non-zero margin rate, or an open instrument is classified as a derivative/leveraged product. Cash spot portfolios publish `NOT_APPLICABLE` and are not forced through derivative liquidation rules.

## Paper margin model

For each venue, the RMS marks every applicable open position at the latest venue/instrument mark:

| Measure | Calculation |
| --- | --- |
| Gross notional | `sum(abs(position quantity * mark price))` |
| Initial margin required | `gross notional * initial_margin_rate` |
| Maintenance margin required | `gross notional * maintenance_margin_rate` |
| Local margin balance | `collateral_balance + realized PnL + unrealized PnL - fees` |
| Effective margin balance | Lower of local and venue-reported margin balance, when reported |
| Liquidation buffer | `effective margin balance - maintenance margin required` |
| Required buffer | `maintenance margin required * liquidation_buffer_rate` |

Initial margin is reserved atomically before the first child dispatch. An explicitly margin-enabled buy leg reserves initial margin rather than its full cash notional. This keeps cash spot and leveraged derivatives accounting separate while preserving the all-or-none package admission rule.

## Monitoring and controls

`OMS.write_margin_status()` writes `margin_status.json` and evaluates the following venue states:

| State | Condition | Action |
| --- | --- | --- |
| `NOT_APPLICABLE` | Cash spot portfolio | Publish status only |
| `HEALTHY` | Liquidation buffer is at or above the configured buffer | Continue monitoring |
| `WARNING` | Balance remains above maintenance, but the configured buffer is crossed | Emit `MARGIN_BUFFER_ALERT`; do not stop risk automatically |
| `BREACH` | Effective balance is below maintenance margin | Emit alert, activate the RMS kill switch, cancel ordinary open/queued orders, permit only policy-approved risk reduction |

Every state change is an immutable `MARGIN_STATE_TRANSITION`. Repeated observations in the same state do not create alert storms. Kill-switch recovery remains operator-controlled and is blocked while any venue is still in `BREACH`.

Venue/private-stream reports enter through `VenueRiskSnapshot.margin_balance`. The effective balance deliberately uses the lower of local and venue values, so an adverse venue state cannot be masked by stale local collateral.

## Reconciliation

The normal OMS/RMS reconciliation compares venue-supplied values with the independently rebuilt local model. Supported defect codes are:

- `MARGIN_BALANCE_MISMATCH`
- `INITIAL_MARGIN_MISMATCH`
- `MAINTENANCE_MARGIN_MISMATCH`
- `LIQUIDATION_BUFFER_MISMATCH`

Any unexplained mismatch rejects the reconciliation, suspends the affected venue, and activates the fail-closed reconciliation control. The machine-readable contract is `docs/schemas/margin_status.schema.json`; a representative status is `data/samples/orders/margin_status.json`.

## Configuration

Venue account settings live under `risk.margin_accounts`:

```json
{
  "BINANCE": {
    "margin_enabled": true,
    "collateral_balance": "25000",
    "available_margin": "20000",
    "initial_margin_rate": "0.10",
    "maintenance_margin_rate": "0.05",
    "liquidation_buffer_rate": "0.25"
  }
}
```

Instruments use `product_type` (for example `SPOT`, `PERPETUAL`, `FUTURE`, or `OPTION`) and `leverage`. Venue tier schedules and portfolio-margin offsets are intentionally adapter inputs; the RMS never invents them.
