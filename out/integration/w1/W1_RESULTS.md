# W1 feed health and clock: results (build session, 17 Nov 2026)

EXPLORATORY: mechanics only. Replays use the 29 Oct 2026 capture (recorder quality REJECTED, host clock UNKNOWN), 1 SOL clip, singapore latency, hedge minimum 5 USDT. Every feed-health threshold is ASSUMED. No profit figure rests on any of this.

## What was built

Profile `perp_perp_maker_taker_v1_11` = v1_10 + the `feed_health` block (sha256 4eb991d1...; v1_10 unchanged, e0fb1910...). Everything is OBSERVATION: both actions are `none` and FEED_DELAYED stays off. No new source file: the state model lives in `time_integrity.py`, the wiring in `session.py`, `live_feed.py`, `paper_live.py`, `config.py`, `quality_preflight.py`, `data_manifest.py`.

| Item (audit id) | State |
|---|---|
| State model HEALTHY / DEGRADED / UNSAFE per venue, instrument, stream, hysteresis, OWN_LAG flag (1.3) | BUILT, observation |
| Per-stream rollup: events, rate, delay mean / p50 / p99 / max, largest gap, bursts, own lag, episodes, seconds in state (1.2) | BUILT. No time-of-day baseline and no versioned baseline file: NOT BUILT |
| One `FEED_HEALTH_STATE` event per transition, with the alert context on a worsening (1.4) | BUILT. No delivery channel (none exists; a network sender needs its own decision) |
| Actions `block_new_quotes_on`, `withdraw_quotes_on` (1.4 c) | BUILT behind switches, OFF. Waiting for the owner |
| FEED_DELAYED as a profile key `feed_health.feed_delayed_ms` (1.6) | BUILT, null (off). Waiting for the owner (1000 vs 2000 ms) |
| Host clock fed by the server-time probe, the estimate kept as a cross-check (1.6 a) | BUILT (live only). The probe's uncertainty from this Mac is about 85 to 95 ms (audit), so it is a coarse reference |
| UNSYNCED host clock makes every feed DEGRADED (1.6 c) | BUILT |
| `elif` chain fix: a corrected-negative event beyond the delay limit is FEED_DELAYED (1.6 f) | FIXED, test fails without it |
| Service-side feed age always 0 (1.4 f4) | FIXED with arrival stamping on; unchanged (LEGACY) without |
| Boundary tests: delay exactly at the limit, offset exactly 100 ms, tolerance 0 (1.6 e) | ADDED |
| `paper_summary` gate block (1.8) | FIXED additively: `book_integrity_gate_acting` plus a label on the session's block |
| `receive_mono_ns` on the wire record and the trace row (0.2b, 1.5) | BUILT, only with arrival stamping on (the A9 stamp, no second stamp). The canonical `MarketEvent` is NOT changed: a new field there would change every recorded and replayed event file. Owner to say if that is wanted |
| Silent receive-time substitution (0.2b) | COUNTED per venue (`exchange_ts_substituted`); the event is not flagged, for the same reason |
| Recorder health block in the part manifest (1.8) | BUILT, off by default, on through the profile |
| Feed-health config report per profile (1.1) | BUILT: `feed_health_config_table`; `out/integration/w1/feed_health_config_by_profile.json`; a v1_11 run carries it in its manifest |
| `docs/time/timestamp_dictionary.json` (1.5) | EXTENDED (1.1.0). Send, ack and execution times are still one `ts_ns` in the ledger: a gap, stated there |
| Sequence reset as a manifest event (0.3b) | BUILT in the scan and the gate. The manifest writer and the spot validation's hard-coded 04-23 window (another worktree) are NOT changed |
| Historical acceptance (1.7) | D7d run2 and 29 Oct: DONE (below). April ADA / MNT and 04-23 06:09: NOT RUN (that data is not in this working copy; the perp ob200 file needs the owner's read permission) |
| The two unloadable YAML configs (1.1 b) | NOT TOUCHED (moving them is an open owner decision) |
| ollog monotonic stamp in raw lines (0.2b) | NOT BUILT (ollog is outside the core) |

## Gates

- G4 off-identity: v1_11 with the `feed_health` block removed against v1_10, full 29 Oct day: every output file byte-identical; the manifest differs only in the profile name.
- v1_11 as it is (observation on) against v1_10, same day: signals, orders and fills are the same rows. Five files differ: the limits file and the manifest (the new `feed_health` block and the `FEED_HEALTH_STATE` count), the safety events file (the new events), and the fills and decision files ONLY in `ledger_event_id` (an id derived from the ledger's event sequence, which the extra events shift).
- G3 identity (v1_9, 1 SOL) and G1 (full suite against the 93 baseline): in the commit message and the report to the integration session.

## Replays (coverage matrix rows 4x, 4a, 4b, 4c)

| Case | Result | What it shows |
|---|---|---|
| 4x v1_11 observation, dense window | PASS | signals 13, orders 27, fills 26: the same as v1_10; 165 transitions, one event each |
| 4a hedge feed lost 40 s with a quote resting, actions ON for the case | PASS | Binance book HEALTHY -> DEGRADED -> UNSAFE -> HEALTHY, one event each; the resting quote withdrawn once, 1.22 s after it was placed (at the DEGRADED point); 4,687 evaluations blocked |
| 4b the same loss, observation only | PASS as a record | WITHOUT the action the quote stayed live 9.5 s after the hedge feed was DEGRADED (it ended by another rule). This is today's behaviour, now on record |
| 4c injected feed delay 1.5 s for 60 s, FEED_DELAYED on at 1000 ms for the case | PASS | 3,046 events delayed, 2,817 decisions blocked by the time gate, 0 maker quotes sent in the delayed minute, the delay shows as DEGRADED / UNSAFE |

The first version of the feed-loss injection left the lost venue's book invalid for over an hour after the gap (an artefact case 3e reported). The reconnect snapshot is now bridged to the venue's delta chain; 3e was rerun and its price pause ends at the reconnect.

## Historical verdicts (read-only, v1_11 thresholds, ASSUMED)

| Recording | Events | Verdict |
|---|---|---|
| D7d run2 live paper session, 8 Nov (55 min) | 156,219 book events | Binance book: 22 DEGRADED and 3 UNSAFE episodes, 71 s and 10 s; Bybit book: 20 and 2, 54 s and 4.5 s; largest gaps 6.5 s and 3.9 s. The time monitor blocks 659 events, the number the audit read from that run's own report. Own lag is not visible (the recording has no arrival stamp) |
| 29 Oct capture, full day (about 14 h of events) | 1.7 M Bybit and 0.48 M Binance book events, plus trades | 1,955 transitions. Binance book: 248 DEGRADED and 59 UNSAFE episodes, 765 s and 674 s, delay p99 262 ms, max 34.9 s. Bybit book: 209 and 45, 607 s and 161 s, delay max 12.1 s. The trade streams: Bybit delay p99 1.8 s. Consistent with the capture's REJECTED recorder quality: this is a verdict on the recording, not on the venues |

Files: `verdict_d7d_run2.json`, `v111_observe/session_limits_and_stops.json`.

## Owner decisions this step is waiting on (nothing was turned on)

1. FEED_DELAYED threshold: 1000 or 2000 ms (`feed_health.feed_delayed_ms`, null).
2. Withdraw and / or block on DEGRADED or only on UNSAFE (`feed_health.actions.*`, none). Case 4b is what happens without it.
3. Whether the canonical `MarketEvent` should carry `receive_mono_ns` and a substitution flag (it changes every event file).
4. The state thresholds themselves: all ASSUMED. On 29 Oct the Bybit book was DEGRADED 209 times at a 500 ms silence limit; with an action on DEGRADED that is 209 withdrawals a day.
5. A delivery channel for alerts.
6. `GATES.md` lists v1_11 as NOT CREATED: the pin block needs the new profile's hash (that file belongs to the gates session).
