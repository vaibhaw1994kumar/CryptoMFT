# Strategy kill switches

The kill switch is a durable control state, not a mutable trading flag. It has two states: `INACTIVE` and `ACTIVE`.

## Activation

`OMS.activate_kill_switch(reason, now_ns, operator_id, source="PM")` performs one coordinated action:

1. Set strategy new-risk admission to stopped.
2. Record `KILL_SWITCH_STATE_TRANSITION` with the previous state, source, operator and reason.
3. Cancel adapter-resting orders and remove queued/delayed EMS requests before they can reach an adapter.
4. Record `KILL_SWITCH_CANCEL_ALL_COMPLETE` with the cancellation count.
5. Reconcile cancelled parents into hedge/unwind or terminal state.

The configured `allow_risk_reducing_under_stop` policy is published in the PM status. When enabled, only orders mathematically proven to reduce the parent residual may continue; new strategy packages remain blocked. When disabled, even unwind routing is held and the residual breach stays visible for operator action.

## Recovery lock

The switch cannot be reset directly. `OMS.recover_kill_switch()` first runs the current RMS reconciliation. Reset requires all of the following:

- the latest reconciliation decision is `APPROVED`;
- there are no unresolved venue suspensions;
- exposure-limit state is `NORMAL`;
- no parent opportunity remains incomplete.

Only then is an `ACTIVE → INACTIVE` transition recorded with the reconciliation ID and PM operator ID. The switch does not automatically clear merely because the original trigger disappears.

## PM visibility

Every backtest or paper run writes `kill_switch_status.json`, including current state, trigger reason/source/time, new-risk permission, cancel-all policy, risk-reducing permission, reconciliation requirement, last reset reconciliation ID and complete audit events.

The controlled test queues two venue children, activates the PM stop, proves both queued requests were removed, verifies that another opportunity is rejected, performs approved reconciliation and PM reset, then executes a fresh package successfully.
