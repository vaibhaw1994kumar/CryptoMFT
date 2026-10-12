# Stale and invalid feed control

The market-data integrity gate and OMS/EMS share the same venue state. Every book is age-checked before strategy evaluation, and only `BookState.VALID` is tradable.

| Book condition | Opportunity behavior | Venue order behavior | Recovery |
| --- | --- | --- | --- |
| Fresh `VALID` | Allowed when all required cross-venue inputs are valid | `ACTIVE`; ordinary and risk-reducing requests may route | No action |
| Initial `SYNCING` | Blocked | `RISK_REDUCING_ONLY`; no new risk | Valid synchronized snapshot |
| `STALE` / over age | Blocked | Cancel ordinary orders once; preserve risk-reducing hedge/unwind work | Reconnect and fresh synchronized snapshot |
| `GAP_DETECTED` | Blocked | Same venue-scoped suspension/cancellation | Snapshot/delta resynchronization |
| `CHECKSUM_FAILED` | Blocked | Same venue-scoped suspension/cancellation | Verified replacement snapshot |
| Crossed/empty/invalid book | Blocked | Routing rejects `book_not_tradable`; risk reduction must use another valid eligible venue | Valid reconstructed snapshot |

The gate tracks transitions independently by venue, preventing one bad source from overwriting another venue's native state. On a valid-to-invalid transition it calls `OMS.apply_feed_control()`, changes the venue to `RISK_REDUCING_ONLY`, cancels ordinary created/resting orders, and retains orders proven to reduce the signed parent residual. Repeated observations of the same bad state do not create cancellation storms.

`FEED_CONTROL_STATE_TRANSITION` records the previous/current mode, feed reason and new-risk/risk-reduction permissions. `FEED_CONTROL_VENUE_CANCEL` records the venue, cancellation count and preservation policy. A fresh valid snapshot transitions the venue back to `ACTIVE` automatically.

The tests cover opportunity suppression during a controlled gap, a receive-time feed stall affecting only one venue, recovery from a fresh snapshot, and preservation of an acknowledged unwind while ordinary venue risk is disabled.

A representative transition/cancel/recovery sequence is published at `data/samples/orders/feed_control_ledger.jsonl`.
