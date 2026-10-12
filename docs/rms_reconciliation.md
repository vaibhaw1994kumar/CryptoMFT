# Reconciliation and minimum RMS controls

## Release invariant

A run is releasable only when its order, fill, position, balance, PnL, reserved-capacity and control states are internally consistent. The RMS decision is fail-closed: any unexplained difference returns `REJECTED` and activates the strategy kill switch.

## Canonical order/fill ledger

Every OMS action and observation is appended as an immutable event with a deterministic `ledger_event_id` and parent, opportunity, order, venue and instrument correlation IDs.

| Input | Ledger representation |
| --- | --- |
| Submit, cancel or replace | `ORDER_LIFECYCLE` with request and idempotency IDs |
| Acknowledgement | `ORDER_LIFECYCLE: ACKNOWLEDGED` |
| Partial or full fill | `ORDER_LIFECYCLE: PARTIAL/FILLED` plus incremental `FILL` |
| Reject, cancel or timeout | `ORDER_LIFECYCLE: REJECTED/CANCELLED/TIMED_OUT` |
| Duplicate/conflicting update | Explicit synthetic control event |
| Fee or average-price correction | `FEE_ADJUSTMENT` or `FILL_NOTIONAL_ADJUSTMENT` |

Fills are accounted from cumulative venue quantity. Only the positive difference from the already-accounted quantity changes the position. Replaying the same private-stream or REST event therefore cannot create a second fill. Cash is maintained as a session delta from executed notional and cumulative fees, avoiding any assumption about the account's opening balance.

The event schema is `docs/schemas/order_fill_ledger.schema.json`; the mixed private-stream/REST example is `data/samples/orders/order_fill_reconciliation.jsonl`.

## Ledger-derived position and balance state

`Ledger.recalculate_account_state()` replays incremental fills using average-cost accounting. For every venue/instrument it derives signed quantity, average entry price, realized PnL, mark-to-market unrealized PnL, fees and gross traded notional. For every venue it derives opening-capital-adjusted cash balance, equity, available cash and available capital after current OMS cash, margin, position and message reservations. `OMS.write_account_state()` publishes this state as `account_state.json` at the end of each backtest.

On paper-process restart, the OMS loads every durable nonterminal parent snapshot, reconstructs its submit/order/fill ledger, and rehydrates the all-or-none package reservation before accepting new work. A reservation that cannot be restored activates the kill switch. This makes the first post-reconnect venue comparison use the same accountant as normal callbacks and end-of-run reporting.

## Testnet reconciliation

`OMS.reconcile_risk_state()` accepts the complete testnet order result set plus `VenueRiskSnapshot` records. Venue order updates should first enter `reconcile_plan()` or `ingest_private_stream_update()` so the same deterministic reducer establishes local state. The RMS then compares:

- local and venue order identities, states and cumulative fills;
- fill-ledger totals against every child order;
- local position totals against venue positions;
- fill-derived session cash deltas against venue balance deltas;
- average price and realized/unrealized PnL against venue account state when supplied;
- available cash/capital and reserved cash/margin/position/message capacity when supplied;
- margin balance, initial margin, maintenance margin and liquidation buffer when supplied;
- local kill-switch state against account, venue and strategy controls.

The complete-order endpoint must be used when `require_venue_orders=true`; an open-orders-only response would incorrectly report completed orders as missing.

Defect codes include orphan or missing venue orders, orphan ledger events, duplicate ledger/fill IDs, unexplained or mismatched fills, position/average-price/PnL/balance/capacity differences and control-state differences. Position tolerance applies to quantity, average-price and reserved-position comparisons; balance tolerance applies to monetary fields. Identity, message capacity and control checks are exact.

## Trade-break handling

Reconciliation classifies venue execution discrepancies explicitly:

- `MISSING_LOCAL_FILL`: venue cumulative fill is greater than the local ledger;
- `EXTRA_LOCAL_FILL`: local cumulative fill is greater than the venue result;
- `CONFLICTING_ORDER_STATE`: local and venue lifecycle states disagree;
- `INCOMPLETE_OPPORTUNITY`: an end-of-run or reconnect gate finds a parent that has not reached `CLOSED`;
- `MISSING_VENUE_ORDER` and `ORPHAN_VENUE_ORDER`: the complete venue order set and local correlated set do not match.

Each discrepancy carries its venue and correlation IDs. The RMS adds the affected venue to `RiskEngine.suspended_venues`; subsequent packages touching that venue are rejected before either child is dispatched. Other venues remain eligible, and reconciliation updates are still accepted so private-stream or REST late fills can converge through the normal order reducer. A later reconciliation clears only a venue that was observed again and has no remaining defect. The report publishes both current suspensions and venues resolved by that pass in `venue_controls`.

Set `require_complete_opportunities=true` for reconnect and end-of-run release gates. Internal interim audits leave it false so an actively executing parent is not misclassified as a break. Missing-fill, extra-fill, conflicting-state, incomplete-package and late-fill resolution paths are covered by injected tests in `tests/test_reconciliation.py`.

## Persistence and release gating

SQLite-backed paper runs persist `ORDER_LIFECYCLE` and `RMS_RECONCILIATION` entries with the parent snapshot. Backtests write the same information to `ledger.jsonl` and always publish `account_state.json` plus `rms_reconciliation_report.json`.

Before any plan reaches `RECONCILED → CLOSED`, an internal order/fill audit must return `APPROVED`. An external testnet mismatch records the full report, changes the RMS decision to `REJECTED`, activates the kill switch and blocks new strategy packages until an operator resolves the defect and explicitly resets controls.

The account-state and decision schemas are `docs/schemas/account_state.schema.json` and `docs/schemas/rms_reconciliation_report.schema.json`; derivative margin monitoring is specified in `docs/margin_collateral.md` and `docs/schemas/margin_status.schema.json`. Approved samples are under `data/samples/orders/`.
