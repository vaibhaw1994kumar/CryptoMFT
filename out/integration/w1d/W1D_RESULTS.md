# Queue item (d): v1_11 with the owner's feed-health decisions (build session, 17 Nov 2026)

EXPLORATORY: mechanics only, the 29 Oct 2026 capture (recorder quality REJECTED, host clock UNKNOWN), 1 SOL clip, singapore latency, hedge minimum 5 USDT. Every threshold is ASSUMED and was not tuned.

## What changed
- `feed_health.py` (approved new file): the W1 state model, rollups and scan moved from `time_integrity.py` unchanged; `time_integrity` re-exports every name.
- v1_11 (it was NOT FROZEN and never pinned, so it was changed in place; v1_10 untouched): `feed_delayed_ms: 1000`; `block_new_quotes_on: DEGRADED` (either feed); `withdraw_quotes_on: DEGRADED` with the new `withdraw_quotes_scope: hedge_venue` (Binance only); new `unsafe: cancel_quotes_and_pause` (UNSAFE on any feed cancels every resting quote and pauses new ones); new `own_lag: no_new_quotes`. Hedges, unwinds and cancels are never held. Each new key defaults to the W1 behaviour, so a block without them acts as before.
- v1_12 follows v1_11 (v1_11 + the incident block).
- New profile hashes: v1_11 5413d192313c0c7791dc867003609056fae9380f7d2152225d6b2b7d09e53889 (was 4eb991d1...), v1_12 067bcd7878d6e2cad380618b09ed018f15d1d926cbecdd5d3c6f0757f04fe220.

## Gates
- G3 v1_9 1 SOL identity, full day, against the 2i gate run: 14 of 14 files equal.
- G4 v1_11 with the feed_health block removed against v1_10, full day: every output file equal; the manifest differs only in the profile name.
- v1_12 against v1_11 (both as decided), full day: equal apart from the profile name in the manifest (the incident block is never read by a replay).
- Tests: tests/test_feed_health.py (the four W1 tests that encoded "observation only" now say what v1_11 decides; new OwnerActions tests: hedge-feed DEGRADED withdraws once, a maker-feed DEGRADED withdraws nothing and blocks, UNSAFE cancels and pauses, OWN_LAG blocks, key validation).

## The 29 Oct day at 500 ms silence (v1_11 as decided, against v1_10)

| | v1_10 | v1_11 decided |
|---|---|---|
| Feed transitions to DEGRADED / UNSAFE | (not built) | 881 / 197 |
| Withdrawal triggers (hedge feed DEGRADED, or UNSAFE on any feed) | - | 637 |
| Quotes withdrawn by feed health | - | 2 (both at a hedge-feed DEGRADED) |
| New-quote evaluations blocked by feed health | - | 45,007 |
| Events blocked by FEED_DELAYED at 1000 ms | 0 | 46,614 |
| Maker quotes placed | 5 | 39 |
| Withdrawn by quote validity (edge below threshold) | 3 | 31 |
| Quotes that filled ("survived" to a fill) | 2 (12:30:12 and 12:30:13 UTC) | 2 (20:37:09 and 20:41:19 UTC) |
| End position | +2.0 / -2.0 | +2.0 / -2.0 |

Read: 637 triggers but only 2 withdrawals, because at almost every trigger no quote was resting (the block had already stopped new ones). The two 12:30 fills of v1_10 did NOT happen under v1_11: quoting was blocked or the quotes were withdrawn around them, and two later quotes filled instead (8 hours later, at lower prices). More quotes were placed (39 against 5): each block ends with a fresh signal, and most of those quotes were then withdrawn by validity. This is a large behaviour change on a capture whose recorder quality is REJECTED; the thresholds are ASSUMED.

## Window cases with the profile's own values (2-hour window A; W1 rows for comparison)
4x dense: 12 signals / 27 orders / 29 fills (W1 observation build 13 / 27 / 26), 25 withdrawal triggers, 0 quotes withdrawn, 4,357 blocked. 4a hedge feed lost 40 s: 1 quote withdrawn at the DEGRADED point (as the W1 case with actions set), 27 triggers, 4,433 blocked. 4c 1.5 s injected delay: FEED_DELAYED at 1000 ms (now the profile value) blocks the delayed minute as the W1 case did, 28 triggers, 4,267 blocked.

Clock source (A3) unchanged: still the owner's call; nothing turned it off.
