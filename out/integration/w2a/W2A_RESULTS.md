# W2a session builder, journals, run record, hygiene: results (build session, 17 Nov 2026)

EXPLORATORY: mechanics only. Replays use the 29 Oct 2026 capture (recorder quality REJECTED, host clock UNKNOWN), 1 SOL clip, singapore latency; the paper check uses the 8 Nov paper recording (d7d-run2). No profit figure rests on any of this. No profile changed; no new source file.

## What was built

| Item (audit id) | State |
|---|---|
| One session builder for paper and replay (12.2) | BUILT: `profile_session_rules` in `session_replay.py`, called by `SessionReplay` and `live_paper_setup`. A paper session of v1_9 or later now runs the profile's breaker and layer risk mode (enforce); every paper session gets the execution config, the intent journal and the markout horizons. Test: the arguments are equal for v1_5, v1_9, v1_10, v1_11 |
| Journals on disk as they happen (2.1) | BUILT in `paper_live.py`: `ledger_stream.jsonl`, `session_intent_journal.jsonl` and the non-EVENT rows of `paper_timestamps.jsonl` are appended and flushed per record; `journal_manifest.json` unsealed at start, sealed at the normal end with counts and the largest persistence lag. Test: a session that is never finalized has every record on disk and an unsealed manifest |
| Durability policy, recorder and journals stated separately | WRITTEN in `docs/strategy_contract.md`: recorder files keep the A9 bounded flush; journals flush per record; fsync only with `durable`, at the bounded interval |
| Journal cap flag (0.2d) | BUILT: records dropped after 200,000 are counted; the layer report carries `journal_truncated` and `journal_records_dropped` when not zero (unchanged report otherwise) |
| Replay interlock (2.4) | BUILT: refuses a session that is not INTERNAL_SIMULATION, a non-simulated adapter, and an output directory that is or sits inside a live recording |
| Input and output hashes (2.5) | BUILT as `session_run_record.json` (written last, next to the manifest): every input file actually opened with size, sha256 and, for gzip, content sha256 checked against the recorder's part-manifest hash; every output file hashed; data span, wall time, speed; effective seeds. NOT inside `session_manifest.json` (a file cannot hold its own hash; wall time differs per run), so the manifest bytes are unchanged |
| Effective seeds | Already in every manifest since Order 2; also in the run record |
| Session compare | BUILT as `compare_sessions(dir_a, dir_b)` in `session_replay.py` (first differing row and field; manifest compared without `run` and `release_identity`). The `session` mode of `scripts/compare_baselines.py` is NOT added: that script belongs to the gates session |
| `PYTHONHASHSEED=0` in pool scripts | NOT DONE, on purpose: the pool scripts and day wrappers start Python with `-I`, which ignores the variable. The identity replays run with a random hash seed in separate processes and are byte-identical, which is the stronger evidence. Stated in the contract; the run record carries the setting |
| "Intentional non-determinism" paragraph and the exact-vs-statistical table | WRITTEN in `docs/strategy_contract.md` |
| 860 vs 837 ledger rows of d7d-run2 | EXPLAINED (below) |
| Paper parity on v1_10 | OFFLINE: DONE (below). LIVE: NOT RUN (needs network; and the A3 clock gate would stop it from quoting) |
| Test that the arb never calls `choose_tactic` (decision 11) | ADDED: a full v1_9 arb run with the function patched to fail; every arb profile leaves `tactic_choice` off; turning it on is refused |
| Logic out of `out/` (decision 13) | DONE in this commit: the safety-events, limits-and-stops, participation and impact writers (`write_evidence`), the stage-trace writer (`write_stage_trace`) and `trace_rejects` as a profile key (false everywhere). `run_day.py` and `scenarios.py` now only call them. Byte-identical proof: the v1_9 identity replay below, which includes the three moved files |
| `selftest.py` (decision 14) | MOVED to `discarded/v1.1/` with a MANIFEST line: it asserted a profit on a manufactured venue and nothing ran it |
| `hypothesis` pinned, test-only (decision 12) | NOT DONE: it is not installed and installing needs PyPI (network). Waiting for the owner's go-ahead |
| `live_paper_setup` moved into the core (A2 12.2) | NOT DONE: it needs the profile loader (`new_backtest.profiles`) in the core first; the core must not import `new_backtest` |
| `_impact_sync` defined twice, dead `_depth_evidence` | LEFT for W3 (the plan allows W2a or W3; W3 touches that code) |

## Gates

- G3 identity (v1_9, 1 SOL, full 29 Oct day), W2a code against the W1 gate run, with the new compare: 14 of 14 files equal (13 byte-identical; `session_manifest.json` equal outside `run` and `release_identity`). File: `identity_compare_W1_vs_W2a.json`.
- Run record of that replay: 4,356 input files (2.58 GB), every one checked against the recorder's own hash, 0 mismatches, 0 missing; data span 50,588 s in 1,026 s of wall time (49x).
- G1 full suite against the 93 baseline: UNCHANGED (93 now, 0 new failures, 0 no longer failing; 3,878 passed). File: `compare_baselines_W2a.txt`.

## The 860 against 837 ledger rows (d7d-run2, 8 Nov)

The recording was replayed again (read-only) and the feed-control rows matched to the live ledger by venue, state and time.

| Extra live rows | Count | Cause |
|---|---|---|
| `FEED_CONTROL_STATE_TRANSITION` | 17 | 1 start-up row (Bybit syncing before its first snapshot; a replay starts at the first frame). 16 = two rows each of 8 short stale episodes (22 ms to 1.9 s) the live staleness timer saw between two frames; a replay evaluates the gate only at recorded events and sees a fresh book at the next one |
| `FEED_CONTROL_VENUE_CANCEL` | 9 | one per extra transition into the restricted state (8 episodes + start-up) |
| `RMS_RECONCILIATION` | 1 | the finalize-time row, last in the ledger |
| Kill switch, margin alert, margin state | minus 4 (the replay had 1, 1 and 2 more) | They follow the stale intervals: all 8 live kill-switch rows, all 8 margin alerts and 18 of 19 margin transitions sit inside a stale interval or within 6 s after one. NOT matched row by row: the 9 Nov replay was not stored and today's code writes 817 rows for this recording (the mark rules changed on 11 and 16 Nov) |

The other 9 stale episodes exist on both sides at shifted times (the live timer fires up to 3.8 s before the replay notices). No fill, order, position or cash figure is part of the difference.

## Paper parity on v1_10 (offline, both wirings on the same 60 minutes)

| Run | Ledger rows replay / paper | Result |
|---|---|---|
| As built | 757 / 720 | The paper side logs one `LIMIT_STATE` at start and never evaluates the strategy: its clock gate (A3) has no measurement in an offline run and stops new quotes. The replay side is "not evaluable" and is not stopped |
| Paper clock gate given an INJECTED measurement | 757 / 757 | Identical: ledger rows, positions, cash, plans, strategy counters (154,132 evaluations, 1 signal, 1 quote withdrawn unfilled) |

This is wiring parity resting on one quote and no fill. Files: `offline_parity_run2_v1_10.json`, `offline_parity_run2_v1_10_clock.json`.

## Waiting on the owner

1. A live paper run of v1_10 (network). Note the A3 clock gate: from this Mac the probe's uncertainty is about 85 to 95 ms against the 5 ms limit, so a v1_10 paper session would not quote at all until that limit or the clock source is decided.
2. Installing `hypothesis` from PyPI (network).
3. Whether `live_paper_setup` and the profile loader should move into the core.
4. Whether the gates session should add the `session` mode to `compare_baselines.py` (it would call `compare_sessions`).
