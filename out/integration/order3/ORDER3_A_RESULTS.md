# Order 3, part A (limits A1-A9): results (build session, 17 Nov 2026)

EXPLORATORY: mechanics only. 29 Oct 2026 capture (recorder quality REJECTED, host clock UNKNOWN), window A, 1 SOL clip, the dense entry of the 2i cases (threshold -2.0 bps, participation limit off, both labelled) so that there are quotes for a limit to act on, hedge minimum 5 USDT. Every limit's condition is INJECTED: nothing binds on this day by itself.

## What was built (all in `oms.session_limits` of v1_10; absent in every earlier profile, which stay byte-identical)

| Limit | Profile key | Counter and event | Unit tests | Injected replay case |
|---|---|---|---|---|
| A1 order rate | `order_rate: {per_s 2.0, burst 5}` | `order_rate_skipped_quote/amend`, `LIMIT_STATE order_rate:<venue>` | `A1OrderRate` (3) | 3a: 0.2/s burst 1 |
| A2 daily loss stop | `daily_loss_stop_usdt_per_sol_of_clip: -5.0` | `daily_loss_stop_trips`, `LIMIT_STATE` + kill switch | `A2DailyLossStop` (3), `KillSwitchKeepsHedgesInFlight` (2) | 3b: limit set to -0.02 USDT |
| A3 clock-offset gate | `clock_offset: {max_uncertainty_ms 5.0, max_age_s 900}` | `clock_not_evaluable`, `limit_blocks clock_*`, `LIMIT_STATE clock_offset` | `A3ClockGate` (4), `ClockProbe` (3) | 3c: 6 ms, 4 ms, none |
| A4 margin WARNING stop | `margin_warning: {free_margin_multiple 10.0, resume_hysteresis_s 5.0}` | `limit_blocks margin_warning`, `LIMIT_STATE` | `A4MarginWarningStop` (3) | 3d: 30 USDT capital per venue |
| A5 missing marks | `missing_marks_kill_s: 60.0` | `missing_marks_trips`, `LIMIT_STATE` + kill switch | `A5MissingMarks` (2) | 3e: Binance feed lost 150 s |
| A6 tier table | `tier_table: read_at_start` | `account_checks.tier_table` | `TierReads` (8) | start-up refusal against recorded messages |
| A7 1 s no-requote | `arb.requote_delay_after_withdraw_s: 1.0` (Order 2) | `requote_blocked_after_withdraw` | Order 2 | Order 2 |
| A8 account check | `verify_account: {enabled, margin_mode cross, leverage 10}` | `account_checks.verify_account` | `VerifyAccount` (6), `StartUp` (4) | start-up refusal against recorded messages |
| A9 stress proof | none (a measurement) | `scripts/stress_feed.py` | the harness runs | see below |

Every one is a `REAL_ORDER_REQUIRED` row; v1_10 meets all of them, v1_9 and v1_9s meet none of the new ones (refused for real orders, as before for the earlier rows). Tests: 3815 pass in the suite (3776 before), the 93 baseline failures unchanged, `compare_baselines` UNCHANGED. v1_9 1 SOL identity replay: 12 of the 14 output files byte-identical to the Order 2 run; the manifest differs only in the `real_order_requirements` note (v1_9 now lists the new rows it lacks), the limits file only in added keys.

## Replay results (coverage matrix rows 3x, 3a-3f)

| Case | Result | What it shows |
|---|---|---|
| 3x baseline | no limit bound | 13 signals, 14 maker orders, 26 fills; daily P&L low point -0.546 USDT against -5; order rate 14 taken, 0 skipped; clock gate "not evaluable" 401,834 times (counted, not blocking) |
| 3a A1 | PASS | 162 of 170 asks skipped (157 quotes, 5 amends), 3 state changes once each, cleared when a token returned; maker 25 fills hedged in full; nothing queued |
| 3b A2 | PASS | tripped once at -0.027 USDT; one ACTIVE event; maker filled = hedge filled (the hedge in flight finished); flat; latched |
| 3c A3 | PASS | 6 ms: 401,874 evaluations blocked, 0 maker orders; 4 ms and "no measured clock": identical to the baseline (13 / 27 / 26), the latter counted "not evaluable" |
| 3d A4 | PASS | 401,874 evaluations blocked (free margin below 10x the hard-cap initial margin), 0 orders; 1000 USDT capital never triggers it |
| 3e A5 | PASS (trip only) | pause began 11:20:05, kill after 60.05 s, once. No hedge was in flight and the day's signals were over by 11:03, so the block on new quotes is shown by the unit test, not by this run. After the injected reconnect the Binance feed stayed paused over an hour: an artefact of my injection, not a market fact |
| 3f A6, A8 | PASS at test level | 21 recorded-message tests; a replay has no account, so there is no replay case |

## Findings for the owner (decisions, not done)

1. **A2 scaling.** The approved default said "-25 USDT at 5 SOL". The labelled "5.0 SOL gross per venue" run is `--size 1 --gross 5`: the CLIP stays 1 SOL, so the limit is -5 (the key is USDT per SOL of clip). The limit becomes -25 only for a 5 SOL CLIP (the 2n size-5 case). Confirm that is what you want; on 29 Oct nothing changes (daily P&L never below -0.6 USDT).
2. **A3 will hold quoting in every real-order run from this Mac.** The audit (A1, line 203) records the REST offset uncertainty (half the round trip) of ollog's 29-31 Oct testnet probes as about 84 to 94 ms; the limit is 5 ms. With a REST server-time probe the gate is "uncertain" all the time, so no real-order session would quote. I built it as specified and did not widen it. A source with a few ms of uncertainty (host NTP/PTP, or a probe from a host near the venue) is needed; host NTP is not built. Decide the source or the limit.
3. **A6 reads the wrong thing for Bybit.** `/v5/market/risk-limit` is the symbol's tier ladder, not the account's selected risk limit; the account's own tier is a separate read (a drill item). Both endpoint shapes are from the public documentation and UNVERIFIED against the live endpoints.
4. **A8 cannot verify reduce-only without an order.** It is DERIVED from one-way mode and labelled UNVERIFIED; also unverified, whether a reduce-only order is exempt from the minimum notional. Both are testnet-drill items.
5. **No caller builds a live session from the profile yet.** The core has no CLI path that builds a real-order `LivePaperSession` from a profile; the caller passes `session_limits=session_limits_from_profile(profile, clip)` and `clock_source="server_time"`. The start-up checks run only in a session with readers or real adapters.
6. **A book that is not VALID gives no equity**, so the daily loss check waits until both books are valid again (a price-anomaly book is not VALID).

## A9 stress proof: FAILED (a finding; nothing was tuned)

`scripts/stress_feed.py` (a new file, raised for your approval): synthetic top-of-book deltas on both venues through the live path (`LivePaperSession`: the real parse, capture, normaliser, book, book gate and `TradingSession.on_event`, wall clock), only the network faked. At **10,000 events/s per venue for 5 s**: no frame dropped, 0 stale-book events, 0 gaps / out-of-order / duplicates / normalisation errors, book age p99 0.24 ms (Binance) and 0.51 ms (Bybit), BUT the path took **33 s to process the 5 s burst** (about 3,000 frames/s in total): the harness's own schedule fell behind by up to **15.5 s** (p99 15.4 s), CPU at 95% of one core. Processing time per event of the session part (`_on_event`) is p50 15 us, p99 24 us, max 0.36 ms (Binance) and 7 ms (Bybit).

Where the time goes (cProfile, 2 s burst at the same rate, the burst only): about 60% is the recorder, not the session: `capture_wire` and `record_event` per frame (a `dataclasses.asdict` of the event, a `json.dumps`, a file write and flush per record) and `_should_rotate` (a `stat` call per frame); the session and strategy path is under 10%.

The rate the path keeps up with, on this Mac, one process (5 s each): 500, 1,000, 1,500 and 2,000 events/s per venue pass (harness lag p99 2.6, 5.7, 7.4 and 10.2 ms; CPU 65 to 82%); **3,000 fails** (lag p99 1.18 s) and 5,000 fails (5.4 s). So the knee is between 2,000 and 3,000 per venue, set by the recorder's per-frame cost. A real Binance or Bybit book stream is far below 2,000 messages/s per symbol in ordinary hours, but the 10,000/s burst the owner named is not met.
