# Restart resilience

## Safety contract

`RestartCoordinator` stores process control in SQLite WAL with full synchronous
commits. A paper session has exactly one active order owner. Each successful
acquisition increments a durable fencing token; every heartbeat, checkpoint and
connector transition must present the current owner and token. A replacement may
take ownership only after the prior lease expires. The higher token immediately
fences the old process, even if its socket or event loop later recovers.

The OMS accepts two optional guards. `order_owner_guard` runs before any outbound
order route, while `new_risk_guard` runs before a new parent plan is created.
Recovered opportunities remain idempotent: calling `execute` for an already
durable parent returns that parent without dispatching another child request.

## Restart sequence

```mermaid
sequenceDiagram
    participant A as Failed process
    participant DB as Durable state
    participant B as Replacement
    participant V as Venues
    A->>DB: Parent dispatch intent + checkpoint
    Note over A: Process stops; lease expires
    B->>DB: Acquire higher fencing token
    B->>V: Reconnect required venues
    V-->>B: Private/REST order and account state
    B->>DB: Restore parent and checkpoint
    B->>B: OMS/RMS reconciliation
    B->>DB: Approved reconciliation
    Note over B: New risk may resume
```

Acquisition always puts the session in `RECONNECTING` and marks reconciliation
required. Once every configured venue is connected the state becomes
`RECONCILING`. Only an `APPROVED` `ReconciliationReport` can move it to `READY`.
A rejected report leaves the session `SUSPENDED`. This gate applies after every
owner acquisition and after any connector disconnect, so a heartbeat alone can
never authorize strategy risk.

## Reconnect and checkpoints

Disconnects schedule deterministic bounded exponential retry:

`next_retry = now + min(max_delay, base_delay * 2 ** (attempt - 1))`

The coordinator exposes the exact next-reconnect timestamp and attempt count.
`run_due_reconnects()` invokes the supplied connector callback once per due
venue, marks successes connected, and automatically persists the next bounded
attempt after a failure.
Replay checkpoints are keyed by stream, contain sequence plus raw reference, and
cannot move backwards. Repeating the same `(sequence, raw_ref)` is idempotent;
reusing a sequence for different raw data is rejected. This complements the
pipeline's transactional raw-offset checkpoint and the OMS parent-plan store.

## Acceptance evidence

`tests/test_restart_resilience.py` covers lease renewal/takeover, stale-owner
fencing, reconnect backoff, reconcile-before-resume, exact checkpoint recovery,
and a process restart with open paper orders. The restart scenario proves the
recovered parent reaches `CLOSED`, the canonical checkpoint is unchanged, and
the replacement router emits no duplicate submission.

Machine-readable state follows
`docs/schemas/restart_resilience.schema.json`; a representative control snapshot
is in `data/samples/pipeline/restart_resilience.json`. Passing that snapshot to
`PipelineMonitor.build(restart_resilience=...)` publishes active-owner,
resume-readiness and disconnected-connector health metrics in the shared JSON
and Prometheus outputs.
