# Track B — hedge-fault injection and margin distance (session runtime)

**Worktree:** `efaults_wt` branch `e-faults`  
**Date:** 2026-11-11  
**Status:** All 4 items complete; local commits only. Ready for owner review and merge.

---

## Base SHAs

| Repo | Base commit |
|---|---|
| Outer (`Newproject/`) | `2c9f196` (feat day-1 replay, S1 label) |
| Core (`crypto_stat_arb/`) | `80fcfb8` (feat oms D2 cancel while submitted) |

## Per-item SHAs

| Item | Core commit | Outer commit |
|---|---|---|
| 1 — fault injection | `9ebd580` feat | `a08614f` test |
| 1 fix — handle() path | `571cdfb` fix | _(no outer change)_ |
| 2 — restart recovery | _(no core change)_ | `9db192d` test |
| 3 — ghost/1x ceiling | `571cdfb` fix (shared) | `07bb7b0` test |
| 4 — liq distance gate | `cc79e20` feat | `2bacbd5` test |

---

## Suite counts

| Test file | Tests |
|---|---|
| `test_hedge_fault_injection.py` | 14 (+ 4 subtests) |
| `test_restart_resilience.py` | 3 (+ 3 previous subtests) |
| `test_ghost_hedge_ceiling.py` | 3 |
| `test_liq_distance.py` | 15 |
| **Total added / changed** | **35 new tests** |

Focused run before merge: `tests/test_liq_distance.py tests/test_margin_tiers.py tests/test_ghost_hedge_ceiling.py tests/test_hedge_fault_injection.py tests/test_restart_resilience.py tests/test_oms.py` → **83 passed, 7 subtests passed.**

Environmental failures (pre-existing, unrelated to Track B):
- `tests/platform_kit/test_config.py::test_loads_project_configuration` — test runner
- `tests/test_adapters_pipeline.py` — missing `data/samples/raw_depth.jsonl`

---

## Item 1 — Hedge-fault injection on `QueueAwareVenueAdapter`

**File:** `src/crypto_stat_arb/simulation/queue_adapter.py`

`QueueAwareVenueAdapter.__init__` gains `fault_config: HedgeFaultConfig | None = None`.

Fault injection fires in `handle()` for IOC LIMIT orders only (taker/hedge path). The check
uses `self._arrival_ns` when set by `handle_at()` (backtester path), or falls back to
`request.created_ts_ns` when the router calls `handle()` directly (session runtime path).
This was a bug in the first commit (`9ebd580`) — fixed in `571cdfb`.

Supported faults:
- `reject_prob`: hash-based, seed-keyed, monotone subset property across different prob values
- Static `Outage` windows: all outage policies (`reject`/`blackhole`/`queue`/`ghost`) map to
  `REJECTED` immediately (parity difference — see below)

`hedge_faults_injected: dict[str, int]` tracks `reason → count` for tests and reports.

---

## Item 2 — Restart-recovery E2E tests

**File:** `tests/test_restart_resilience.py`

Three new tests:

| Test | Scenario |
|---|---|
| `test_open_hedge_recovered_across_restart_no_alarm` | OMS1 dispatches maker+hedge; OMS2 rebuilds from ParentPlanStore; hedge recovered, no alarm |
| `test_alarm_loads_on_second_oms_and_blocks_new_risk` | OMS1 raises unwind-exhausted alarm; OMS2 rebuilds and block new risk; `terminal_outcome == "REJECTED"` |
| `test_alarm_reset_clears_block_and_does_not_persist` | alarm reset on OMS1; OMS3 does NOT see the alarm; new risk passes |

Key implementation detail: `_raise_unwind_exhausted_alarm` has a guard requiring
`plan.unwind_attempts >= max_unwind_attempts`. Tests use
`ResidualPolicy(max_unwind_attempts=0)` to satisfy the guard with a fresh plan.

---

## Item 3 — Ghost hedge / 1x ceiling test

**File:** `tests/test_ghost_hedge_ceiling.py`

Three tests proving:
1. `test_ghost_policy_never_exceeds_1x_maker_qty`: ghost outage → all hedges REJECTED;
   `|plan.residual_base| ≤ maker_qty` at every observable point.
2. `test_ghost_becomes_rejected_not_queued`: ghost maps to immediate REJECTED (not queued
   for silent later delivery); executor has 0 live maker orders on BINANCE.
3. `test_zero_fault_config_no_rejections`: baseline with `fault_config=None` — 0 injections,
   ceiling still holds.

The 1x ceiling property holds by construction in the session runtime: a ghost hedge cannot
fill silently after a retry/unwind has already covered the position, because ghost → REJECTED
immediately. The 2× spike that can occur in the backtester is not possible here.

---

## Item 4 — Liquidation price and distance in core margin engine

**Files:** `src/crypto_stat_arb/margin.py`, `src/crypto_stat_arb/risk.py`

### New functions in `margin.py`

```python
liq_price_at(schedule, is_long, qty, entry_price, available_margin) -> Decimal | None
```
Closed-form inverse of `equity_at_liq = maintenance_margin_at_liq`:
- Long:  `liq = (margin - qty*entry + deduction) / (qty*(mmr - 1))`
- Short: `liq = (margin + qty*entry + deduction) / (qty*(mmr + 1))`

Returns `None` when `qty * entry_price` is above the last risk tier.

```python
liq_distance_bps(liq_price, entry_price, is_long) -> Decimal
```
Signed-positive distance from entry to liq in basis points.

### New field in `RiskLimits`

```python
liq_distance_bps_min: Decimal | None = None
```
When set, `_reserve_package` checks each tiered-margin leg. If the estimated liq distance
< `liq_distance_bps_min`, it raises `ValueError("liq_distance_too_close: ...")` which is
counted in `engine.refusals["liq_distance_too_close"]`. Default `None` disables the check.

---

## Parity differences with the backtester (documented)

| Backtester behaviour | Session runtime behaviour | Reason |
|---|---|---|
| `outage_policy="ghost"`: order silently unacknowledged; re-fills at reconnect | Mapped to `REJECTED` immediately | Cannot model delayed unknown arrival in session runtime |
| `outage_policy="blackhole"`: order lost silently | Mapped to `REJECTED` immediately | Same reason |
| `outage_policy="queue"`: order queued and replayed at reconnect | Mapped to `REJECTED` immediately | Same reason |
| `partial_prob`: partial fill injected via fill walk interception | Not injected | `DeterministicVenueAdapter` fill walk is not interceptable at this level |

Policy fields not injected (backtester-only): `response`, `max_retries`, `entry_guard`, `query_before_replace`.

---

## What was not done

- 2022-01-05 replay with fault injection: data not available in this worktree.
- `fault_config` YAML profile integration: `perp_perp_maker_taker_v1_4.yaml` does not yet
  have a `fault_config` section; wiring that belongs in the profile work, not Track B.
