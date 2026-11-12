# Production exposure limits: PROPOSAL for the owner's approval (12 Nov 2026)

Status: PROPOSAL. Nothing here is a setting yet. Every number is marked EXISTING (already in a profile or the core), PROPOSED
(new, mine, for approval) or MEASURED (from a stated capture). Nothing is evaluated until the owner approves. Controlled live starts at
these fixed small limits; **no automatic scale-up**; each size increase needs the owner's explicit approval and the evidence of the
previous step (rollout rule, below).

Reference prices: SOL near 100 USDT (day-1 capture 2026-10-29), so 1 SOL is about 100 USDT notional. Instrument rules (published
8 Nov 2026): Bybit SOLUSDT tick 0.01, lot 0.1, min 0.1 SOL, min notional 5; Binance tick 0.01, lot 0.01, min 0.01, min notional 5.

## 1. Exposure limits (stage 0 of the rollout: 1 SOL clip)

| Limit | Value | Basis |
|---|---|---|
| Clip (order size, both legs) | 1.0 SOL (about 100 USDT) | EXISTING (profile arb.max_position_base, the size of every replay so far) |
| Net ceiling | 1.0 SOL x net_ceiling_multiple 1.0 | EXISTING (`net_ceiling_multiple`, DECISION_PENDING_CALIBRATION) |
| Fill headroom on top of the ceiling | 0.25 SOL | EXISTING (starting value chosen by me on 7 Nov, never approved by the owner): owner to confirm or replace |
| Hard net cap at any fill | 1.25 SOL | derived |
| Gross per venue | 2.0 SOL | PROPOSED (replays used 5; stage 0 does not need it, and a smaller number makes a bug cheaper) |
| Open maker quotes | 1 per venue at a time (the strategy places one) | EXISTING |
| Residual (unhedged) limit | hold new risk after 5 s unhedged; kill switch after 30 s | EXISTING (owner 8 Nov) |
| Residual size | `max_residual_base` 0.02 SOL in the core default; at stage 0 PROPOSE 0.10 SOL (one Bybit lot) with the persistent alarm at one full hedge lot (Binance 0.01, Bybit 0.1) | the alarm rule is EXISTING (owner 12 Nov (a)); the 0.10 is PROPOSED |
| Unwind | 15 bps band, 3 attempts, then the persistent alarm | EXISTING (owner 8 Nov) |
| Hedge cap | cap `size_aware`, 5 bps floor, 15 bps absolute limit from the touch | EXISTING in v1_9 (ASSUMED values) |
| Quote lifetime watchdog | 15 s | EXISTING |
| Gate limits (layer risk gate) | stay in `count` mode until day-1 and spot validation are in (owner 12 Nov (b)); then `enforce` at the net ceiling plus headroom and the per-venue cap above | EXISTING decision, values PROPOSED |
| Daily loss stop (realised + marked, USDT) | kill switch at -5 USDT on 1 SOL clips; owner to set | PROPOSED (about 5% of one position; no basis in data, a bound to be replaced by the owner's number) |
| Funding | quote nothing in the 30 s before a funding timestamp until funding is in the shadow reconciliation | PROPOSED |

## 2. Margin headroom

| Item | Proposal |
|---|---|
| Capital per venue at stage 0 | the account's actual prefunded amount; replays use 1,000 USDT (ASSUMED) |
| Margin use at the net cap | 1.25 SOL x 100 = 125 USDT notional: about 2.5% at the first tier's 2% initial rate on 1,000 USDT per venue |
| Stop new quotes | margin state WARNING on either venue (EXISTING states), or free margin below 10x the initial margin of the hard net cap |
| Kill switch | margin BREACH (EXISTING: `above_last_tier: breach`), or a missing mark price for longer than 5 s (EXISTING pause, then PROPOSE kill after 60 s paused) |
| Tier table | the account's own tiers, read once at startup; until read, refuse to go live (PROPOSED check; the replays use ASSUMED tiers) |
| Position mode and reduce-only | checked against the real account settings on testnet before any live order (owner input, open) |

## 3. Order and message rates

Market data observed (MEASURED, the 2026-10-29 capture, 50,589 seconds with events, both venues, book plus trades): mean 97 events/s;
p50 60, p99 562, p99.9 1,238, max 3,089 events/s (trade bursts: max 3,089/s; book updates max 383/s).

| Limit | Value | Basis |
|---|---|---|
| New maker orders | at most 2/s per venue sustained, 5/s peak, and at most 70% of the limiter's capacity (the NEW_RISK tier) | tier shares EXISTING (NEW_RISK 0.7, RISK_REDUCING 0.8, CANCEL 1.0, ASSUMED); the rates are PROPOSED |
| Cancels | at most 5/s per venue; the tier keeps all capacity for cancels | PROPOSED |
| Amends | none at stage 0 (amend is off; no strategy calls it) | EXISTING |
| Hedges | never refused by the strategy; refused by the limiter only above 80% (FINDING pinned in a test: above 80% a hedge fails and ends as an alarm; the owner should decide whether the hedge tier goes to 90% or 100%) | EXISTING, DECISION PENDING |
| Market data the system must withstand | 10,000 events/s for 5 s without dropping or a stale-book event (3x the measured max) and 1,000/s sustained | PROPOSED, to be proven by a replay at 3x speed of the capture |

## 4. Latency percentiles the system must withstand

The model uses a lognormal with light tails today (finding, v1_9_TOLERANCES.md section F). The testnet calibration files are the only
measured tails and are TESTNET-labelled indicative bounds, not mainnet measurements.

| Stage | Design target (p99) | Stress case (p99.9) | Basis |
|---|---|---|---|
| Decision plus risk processing (our process) | 5 ms | 20 ms | PROPOSED; the sensitivity setting `processing_ms` runs it (1 ms, 5+5 ms), D7d live values replace these when extracted |
| Order entry to the venue | 250 ms | 500 ms | the default profile's tail (250 ms); testnet grids to confirm |
| Cancel effective | 500 ms | 1,000 ms | PROPOSED |
| Fill notification to hedge request | 20 ms | 100 ms | PROPOSED |
| Public feed delay | 100 ms | 500 ms (feed treated as dead at 5 s, EXISTING) | PROPOSED |

The system must remain safe (B1 safety rules in v1_9_TOLERANCES.md) at the stress case: no exposure above the hard cap, no
unhedged interval beyond the residual limits without the alarm, no order priced from a stale book.

## 5. Explicit stop-quoting conditions (a new quote is not sent while ANY holds; existing quotes are withdrawn where stated)

1. Either book not VALID, in resync, one-sided, crossed, or older than `max_book_age_s` (EXISTING); dead feed over 5 s (EXISTING). Existing quotes withdrawn.
2. Cross-venue skew above 0.2 s (EXISTING).
3. Clock offset unknown or its uncertainty above 5 ms (PROPOSED).
4. Breaker not OK, kill switch on, unwind-exhausted alarm, lot-residual alarm, residual hold (EXISTING).
5. Margin WARNING, margin price missing, margin BREACH (EXISTING states; WARNING as a stop is PROPOSED).
6. Rate limiter above 70% on any bucket (EXISTING tier).
7. A recovered plan not yet reconciled after a restart (`restart_reconcile_gate`, built 12 Nov, to be turned on for live), or a feed gap not yet followed by a valid book, resync and reconciliation.
8. Gateway silent for 10 s (EXISTING breaker) or a venue disconnect.
9. Another instance owns the orders (`order_owner_guard`, EXISTING).
10. Within 30 s before a funding timestamp (PROPOSED, until funding is in the shadow reconciliation).
11. Daily loss stop reached (PROPOSED), or any quote withdrawn by quote validity is not re-quoted for 1 s (PROPOSED, avoids flapping).
12. The venue's account settings (position mode, reduce-only, leverage, margin mode) not confirmed against the expected values at startup (PROPOSED).

## 6. Live rollout rule (recorded in the plan)

Controlled live starts at fixed small limits approved by the owner (stage 0 above). There is no automatic scale-up. Each size
increase needs the owner's explicit approval and the evidence of the previous step: the lifecycle report by regime, the EMS case
evidence files, the reconciliation of orders, fills and positions against the venue, and the list of every alert raised.

## 7. What the owner is asked to approve

1. The stage-0 numbers in section 1 (especially the 0.25 headroom, 2.0 gross, 0.10 residual size, daily loss stop).
2. The margin rules in section 2.
3. The rates and the hedge-tier decision in section 3.
4. The latency targets in section 4.
5. The stop-quoting list in section 5, in particular items 3, 10, 11 and 12.
