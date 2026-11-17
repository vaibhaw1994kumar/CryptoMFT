# W2b incident capture: results (build session, 17 Nov 2026)

EXPLORATORY: mechanics only. The dry run reads the 8 Nov paper recording (d7d-run2); the gate replays use the 29 Oct 2026 capture (recorder quality REJECTED, host clock UNKNOWN), 1 SOL clip, singapore latency. A window's seed books are constructed state (a copy of the real book): never a profit figure.

## What was built

Profile `perp_perp_maker_taker_v1_12` = v1_11 + the `incident_capture` block. Live and paper only; a replay never reads it. No new source file: the capture is in `live_feed.py` next to the recorder, the diagnostics in `session_audit.py`, the profile reader in `config.py`.

| Item (audit id) | State |
|---|---|
| Ring of the last N seconds (2.1) | BUILT as a ring of MARKS (every 5 s: the size of each recorder file and a copy of each venue's book), not a second copy of the rows in memory: the rows are already on disk. Mark cost 0.12 ms median, 0.23 ms worst of 200, with the real 3,300-level Binance book |
| Trigger table (2.2) | BUILT: 10 rules that can fire (feed unsafe, book invalid, breaker trip, kill switch, hedge unfilled, ghost hedge, unknown order state, reconciliation mismatch, limit stop, manual) and 2 named but NOT BUILT (`markout_worse_than` needs W7, `venue_dislocation` needs W6) |
| Window of at least 90 s before and 5 s after (2.2) | BUILT: the start is aligned to a mark, so it is 90 to 95 s before; the copy runs in a worker thread |
| Manifest: rule, symbol, window, frame counts, hashes, completeness (2.2) | BUILT: `incident_manifest.json`, plus the part's own `live_part_manifest.json` |
| "Overload / restart / disk pressure flagged, never silent" (2.2) | BUILT: INCOMPLETE with the reason for a short pre or post window, a gap between marks (stalled loop), disk pressure, a write error, a recorder error, a missing source; suppressed triggers are counted. A recorder RESTART inside a window cannot happen in one process (a new process is a new session directory) |
| Output is a normal `part-*` directory (2.3) | BUILT: `iter_recorded_events` and `SessionReplay.run_recorded` open a window with no change |
| Seed snapshot per venue, `SEEDED_AT_WINDOW_START`, copied never built (2.3) | BUILT: a copy of the live book at the first mark; Binance's sequence bridged to its next delta, the true sequence kept; a venue with no usable book gets no seed and the manifest says so |
| Paper journals in the window | BUILT: the ledger stream, the intent journal and the timestamps rows of the same window are cut into `paper/` (uses W2a's per-record journals) |
| The same cut from an existing recording (Q3 dry run) | BUILT: `cut_recording_window`, command `incident-cut`; read-only on the recording; the seed is the book rebuilt from the recording's own events |
| Diagnostics callable on a window (2.3) | BUILT: `audit_incident`, command `incident-audit` (manifest, file hashes, wire records, trace references, books from the seed; with the source, book state and sequence event for event) |
| Capture in the session summary | BUILT: `incident_capture` block (policy, rule table, every count, the captures) |
| Profile key, default OFF | BUILT: `incident_capture` (v1_12); absent in every earlier profile; `LiveFeedSettings.incident_capture` None by default |
| Session limits reach the paper session through the profile setup | FIXED in passing: the CLI path did not hand the profile's `oms.session_limits` to the paper session's own start-up steps (arrival stamps on for v1_10); it does now |
| Stored LIVE capture of each trigger type (2.2 completion evidence) | NOT DONE: no live session was run (network). Each rule is raised in tests; the cut is exercised on the 8 Nov recording |
| Comparison report of decisions, orders, fills, ledger between a live incident and its replay (Q3) | NOT BUILT: a window replay starts flat, so only the books are comparable today |
| ollog / FAST_MM side | NOT TOUCHED (outside the core) |

## Q3 dry run: three all-channel stalls of the 8 Nov recording, 90 s + 90 s each

Read-only on the recording. Each window was cut, audited against the recording, and replayed through the session replay (v1_10, the profile's own safety values).

| Window | Completeness | Rows (wire / book / trade) | Audit | Book events equal to the recording | Replay |
|---|---|---|---|---|---|
| 1,594 ms stall | COMPLETE | 19,812 / 8,872 / 10,186 | PASS, all six checks | 8,872 of 8,872 | completed, 0 signals, 414 ledger rows |
| 1,941 ms stall | COMPLETE | 19,587 / 8,186 / 10,584 | PASS | 8,186 of 8,186 | completed, 0 signals, 43 ledger rows |
| 1,302 ms stall (36 s before the recording ends) | INCOMPLETE: POST_WINDOW_SHORT | 12,177 / 5,979 / 5,333 | PASS | 5,979 of 5,979 | completed, 0 signals, 9 ledger rows |

In every window the book state and sequence after every event equal the full recording's, and so does the final book on both venues. The second window starts, by chance, inside a real Binance resync: the first Binance event after the seed is a gap in the recording too, and the audit reports it as a property of the source. Files: `q3_dry_run/q3_dry_run_summary.json`, script `q3_dry_run.py`. The windows themselves (189 MB) are run outputs and are not committed.

## Gates

- Focused tests: `tests/test_incident_capture.py` (14) and the paper, live feed, replay, profile tests around it.
- G3 identity (v1_9, 1 SOL, full 29 Oct day) against the W2a gate run: 14 of 14 files equal. File: `identity_compare_W2a_vs_W2b.json`.
- G4 (v1_12 against v1_11, full day each, 1 SOL): 13 of 14 files byte-identical; `session_manifest.json` differs in ONE value, the profile name. File: `compare_v111_vs_v112.json`.
- G0 pinned hashes (`compare_baselines.py hashes`): PASS; v1_12 has no pin row yet (the gates session's file).
- G1 full suite against the 93 baseline: UNCHANGED (93 now, 0 new failures; 3,894 passed). File: `compare_baselines_W2b.txt`.

## Waiting on the owner

1. A live paper session with the block on, to store one capture per trigger type (network).
2. The thresholds are ASSUMED: mark every 5 s, cooldown 30 s, 50 captures per session, 1 GiB free-space floor.
3. Whether `feed_unsafe` should also fire on DEGRADED (on 29 Oct the Bybit book was DEGRADED 209 times; UNSAFE 45 times).
