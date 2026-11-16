# Order 2 results: profile v1_10 (build session, 16-17 Nov 2026)

ALL P&L BELOW IS EXPLORATORY: 29 Oct 2026 capture (recorder quality REJECTED, host clock UNKNOWN, no funding file, mark = mid), 1 SOL clip, Binance SOLUSDT hedge minimum set to 5 USDT for the run (`--hedge-min-notional 5`), singapore latency, simulated fills only. Two to thirteen fills per run: nothing here is a profit estimate or a statistical result.

## 1. Two defects found by the 2i / 2n replays, fixed, each with a test that fails without the fix

| Defect | Where | Found by | Test |
|---|---|---|---|
| A held hedge's age timer could land 1 ns before its own breach test (float seconds at today's epoch step about 240 ns), so the EMS timers never converged and the replay died | `oms.py` `_hedge_timer_candidates` | 2i dense | `HeldHedgeTimerConverges` (fails without the fix, verified) |
| An amend price from the controller was a float off the venue's tick grid, so the first real amend of the arb was refused "price violates tick" | `session.py` `_amend_for_reprice` (bid rounds down, ask up, counted as `amend_price_snapped_to_tick`) | 2i dense | `AmendPriceOnGrid` (3 tests; 2 fail without the fix, verified) |
| A quote that had filled part of its size was judged ACKNOWLEDGED (illegal PARTIAL -> ACKNOWLEDGED, replay crash) when its head was the replacement after an amend, or the old order during AMEND_PENDING, and only an acknowledged hedge or nothing else counted. Crashed 2n size 25 and all four 2i fault variants | `oms.py` `_derive_execution_state` | 2n size 25, 2i variants | two tests in `tests/test_oms_amend.py` (both fail without the fix, verified) |

## 2. Case 2i on v1_10: INJECTED hedge-venue move on the real 29 Oct feed

Setting (INJECTED, not a market event): the Binance book is shifted 2 ticks (2.0 bps) every 20 s for 8 s, alternating down and up, 360 shifts over the window; shifted books and prints, restored by a snapshot that bridges the delta chain (state VALID throughout). For the case only (labelled `arb_overrides`): entry threshold -2.0 bps, participation limit off, validity tolerance 2.5 bps (the profile's 1.5 bps would withdraw a 2 bps move before the controller sees it; quotes enter at about -1.9 bps).

What the real capture gives: ONE real amend per run (11:01:23 UTC, a quote already 0.9 of 1.0 filled). Most quotes sit on a deep price with slack, so the move is held (`hold_inside_band` 340, `hold_no_target_but_valid` about 1100). The amend path is therefore exercised once per variant, not densely. The synthetic stream tests (`tests/test_s0_session.py`, `tests/test_oms_amend.py`) remain the main cover for the amend paths.

| Variant | Amends requested / confirmed / rejected / timed out | Fills during AMEND_PENDING | Late fills on the old order | Cancels during a pending amend | Venue queries | Two live orders | Hedged = filled |
|---|---|---|---|---|---|---|---|
| dense (move only) | 1 / 1 / 0 / 0 | 0 | 0 | 0 | 0 | 0 | 4.9 = 4.9 |
| reject (every amend rejected) | 1 / 0 / 1 / 0 | 0 | 0 | 0 | 1 | 0 | 4.9 = 4.9 |
| timeout (every amend unanswered) | 1 / 0 / 1 / 0 | 1 | 1 | 3 | 1 | 0 | 5.0 = 5.0 |
| disconnect (Bybit order entry and private stream down 11:01:23.291 to 11:01:53, over the amend) | 1 / 0 / 0 / 1 | 0 | 1 | 0 | 0 | 0 | 5.0 = 5.0 |
| duplicate Filled (every Filled report twice) | 1 / 1 / 0 / 0 | 0 | 0 | 0 | 0 | 0 | 4.9 = 4.9 |

Duplicate fills booked twice 0, lineage overfill 0 in every variant.

Not covered, said plainly (coverage matrix result NOT COVERED, not PASS):
- Disconnect: the amend timed out (AMEND_TIMEOUT at +2 s) and NO `VENUE_ORDER_QUERY` followed during the outage. Reading `check_order_deadlines`: a query is deferred while the router still holds a request for the order, and the blackholed request stays pending until the reconnect, when the held reports arrived first. The path "timeout ends in a query" is shown by the timeout variant only (1 query). I did not prove the deferral explanation by a test.
- The timeout variant's unanswered amend ended with the venue's REJECT of the replacement after a fill and three cancels during the pending amend, not in an `AMEND_TIMEOUT` event. So the pure timer path is seen only in the disconnect variant.
- The duplicate Filled variant did not have a fill during the pending amend; the duplicate-delivery logic acted on the maker fills around it.
- Amends were requested once per run, so the amend-per-minute figures are close to zero by construction of the capture, not a measurement.

Real-order guard: `case2i_passed` and `testnet_amend_drill_passed` are both false in v1_10, and the matrix row is NOT COVERED, so `case2i_passed` stays false. Until both are recorded every reprice is a cancel plus a fresh quote (`amend_guard_cancel_instead`, labelled). Test: `V1_10Rules` in `tests/test_session_breaker.py`.

## 3. Cases 2a, 2b, 2f, 2m, 2n on v1_10 (coverage matrix rows "v1_10")

| Case | Result | What it shows |
|---|---|---|
| 2a Binance move before the timer tick | PASS | 5 quotes withdrawn on the event, 5 order rows with the reason; maker filled 2 = hedge filled 2 |
| 2b fill during cancel pending (800 ms) | PASS | 2 fills during cancel pending, fill ids unique, 4 = 4 |
| 2f rate-limit pressure | PASS | 0 hedges refused, 5 quotes skipped before a plan existed, 6 dispatches queued |
| 2m processing 5+5 ms | PASS | unhedged interval p50 47 ms, 2 = 2 |
| 2m p99 delays | NOT COVERED | the run had 0 fills (both quotes withdrawn before a fill), so no race happened |
| 2m p99.9 delays | PASS | 1 fill during cancel pending, unhedged 0.445 s, 1 = 1 |
| 2n size 1 / 5 / 25 SOL | PASS | hedge IOC all full, no unwind; 2 = 2, 10 = 10, 36.1 = 36.1; the size-25 run now completes (it crashed before the PARTIAL fix) |

## 4. Full 29 Oct end-to-end, v1_9 next to v1_10 (1 SOL clip, about 10.85 h of capture)

| | v1_9 | v1_10 | v1_10 at 5.0 SOL gross per venue (labelled) |
|---|---|---|---|
| strategy signals | 2 | 5 | 20 |
| maker orders | 2 | 5 | 20 |
| maker fills / hedge fills | 3 / 3 (one quote filled in two parts) | 2 / 2 | 13 / 13 |
| quotes withdrawn (per hour) | 0 | 3 (0.28) | 14 (1.29) |
| amends requested | 0 | 0 | 0 |
| cancels per minute | 0 | 0.0046 | 0.0215 |
| mean / max quote age | 1.57 s / 2.17 s | 0.14 s / 0.26 s | 0.29 s / 2.0 s |
| orders per maker fill (churn) | 0.67 | 2.5 | 1.5 |
| equity at book mids, EXPLORATORY | -0.104 USDT | +0.0057 USDT | -0.0176 USDT (4.7 SOL) |
| pair net bps, mean (n) | -2.21 (2 fully hedged) | +3.27 (2) | +2.17 (13) |
| margin price pauses | 1 | 7 | 7 |
| signals blocked by the margin price pause | n/a here | 249 | n/a here |
| lot residual (HEDGE_LOT_RESIDUAL) | 1 (0.0076 SOL) | 0 | 0 |
| dust carried | not on | 0 events | 0 events |
| end position | flat net | flat net | flat net |

Every difference, explained from the order and fill rows (EXPLORATORY, n = 2 to 3 fills, no significance claimed):
1. First quote (signal 11:15:27.96, buy 100.57): v1_9 left it resting and it filled in two parts at 11:15:29.29 and 11:15:30.14, 1.3 to 2.2 s after the signal, hedged by Binance IOC orders with limit 100.52 (v1_9's mean pair net over its two fully hedged pairs was -2.2 bps; I did not split it by quote). v1_10 withdrew it at 11:15:28.20, 0.24 s after the signal, because the edge fell below the threshold minus 1.5 bps; no fill. The v1_9 fill is the stale-quote fill that withdrawal exists to avoid.
2. Second quote (signal 12:29:58.14, buy 100.42): v1_9 filled at 12:29:59.105. v1_10 withdrew it at 12:29:58.196, before that fill.
3. v1_10 then placed three more quotes (12:30:11.02, 12:30:12.34, 12:30:12.96). The first was withdrawn at 12:30:11.28; the other two filled (12:30:12.41 at 100.46, 12:30:13.03 at 100.40) and were hedged at once. v1_9 made no signal there (its only two signals are the two above); why its strategy did not signal then, I did not trace, so I do not claim a cause. Those two fills exist only in v1_10. The spacing of the third signal (12:30:12.34) from the withdrawal at 12:30:11.28 is 1.06 s: consistent with the 1 s no-requote.
4. The 7 margin pauses versus 1 are the 5 s mark rule: a mark older than 5 s by its own timestamp pauses; v1_9 re-stamps it and does not. The extra six pauses each last 1 to 4 s and coincide with Binance book resyncs (GAP_DETECTED) and gaps; 249 signals were blocked during them. This is the intended behaviour, and it costs signals.
5. The lot residual of v1_9 (0.0076 SOL left unhedged at 5 USDT) does not occur in v1_10: the dust rule has nothing to carry at 1 SOL because every chunk here is above the minimum.
6. At 5.0 SOL gross: 20 signals, 13 maker fills, 4.7 SOL end position (one 0.7 SOL fill), withdrawals rise from 0.28 to 1.29 per hour; every maker fill was hedged in full (13 = 13), hedge IOC full, no unwind, no kill switch.

The run wrapper did not write the reprice counters, the no-requote counter or the effective seeds for these three runs; `run_day.py` now does (`no_requote`, `amend_guard`, `reprice_counters`, `amend_refusals` in `session_limits_and_stops.json`). The runs for Order 3 part C will carry them. The 1 s no-requote is shown here by the spacing in point 3 and by the unit tests only, not by its counter.

## 5. What is tested (all new tests fail without their change)
Dust carry (Order 1, committed); `V1_10Rules` (6: 1 s no-requote, amend guard, 5 s mark boundaries 4.9 / 5.0 / 5.1 s, seeds, v1_9 frozen), `FrozenV1_9` (sha256 pin, v1_9s pinned, v1_9 lacks every v1_10 key), `HeldHedgeTimerConverges`, `AmendPriceOnGrid`, and the two PARTIAL tests. The diff of v1_10 against v1_9 is pinned to the owner's key list plus `arb.requote_delay_after_withdraw_s`, `margin.mark_stamp`, the two seeds and the two decision_status flags.
