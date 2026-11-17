# Deliverable C: prioritised plan (16 Nov 2026)

1. The plan follows the owner's order: Phase 0 audit (done) -> W1 clock/feed health -> W2 incident capture/determinism -> W3, W4, W6 -> W7 -> W5+W10 -> W8 -> W9 -> W11 -> W12; MM steps follow each core step through merges.
2. Work is split in three groups that are never mixed in one change: (a) safety/correctness fixes, (b) new capabilities (observation and records, off by default), (c) strategy optimisation (nothing here changes trading policy without the owner; split into APPROVED and PENDING).
3. The owner's 14 decisions of 16 Nov are recorded as settled (section 2). Ten items are still OPEN (section 8: the owner's eight, the build session's pending defaults, and two confirmations found here) and each is tied to the phase it blocks.
4. Every phase passes the same four-part gate (section 1): suite vs the Phase 0 list, pinned hashes (14 inputs + a new profile-hash group), frozen v1_9 identity replay, and "new version with all switches off equals its parent". Because 28 `new_backtest` modules are shims onto the core, a core edit can move frozen v1_2 / legacy_v0 bytes: the reference-loop baselines are part of the gate whenever a shimmed file is touched.
5. Only one session edits the core. The build session (dust carry -> v1_10 -> limits -> pair-close switch -> rehearsal rerun) is ALREADY editing: 6 core files and a v1_10 YAML draft are in the working tree (16 Nov 06:54-07:00). BUILD_QUEUE.md's "nothing edited yet" is stale, and two of its lines are superseded (section 4).

W-number mapping: W1 = audit rows 1.x, W2 = 2.x (+0.2c, 0.2d, 12.2 builder, Q3), W3 = 3.x (+0.2e, 0.2f), W4 = 4.x, W5 = 5.1, W6 = 6.x (+0.2g, Q4), W7 = 7.x (+0.2h), W8 = 8.x (+0.2i, Q2), W9 = 9.x, W10 = 10.x (+Q1), W12 = 12.x (+Q5). Taken from the audit's row numbering and from BUILD_QUEUE.md / AUDIT_BRIEF.md (W1, W6 named there); the original order text is not in this folder, so the mapping for W2-W5, W7-W10, W12 is UNVERIFIED against it. W11: no audit rows exist; scope UNVERIFIED, nothing planned beyond a placeholder. "Closes" below means "targets"; a row closes only with recorded end-to-end evidence (the audit's rule: unit tests alone leave it UNVERIFIED).

## 1. Gate pattern (applies to every phase)

| Gate | Check | Notes |
|---|---|---|
| G1 suite | `PYTHONPATH=src python3.12 -m pytest` from `crypto_MFT_arb_platform/`; failures must be a subset of `out/integration/baselines/failing_tests_phase0.txt` (93 lines: 92 FAILED + 1 collection ERROR, per A2) | report the real counts; new tests must pass; a failing test already on the list can hide a regression: new tests never go on the list |
| G2 pins | the 14 pinned input hashes (MM definition, adopted) unchanged, plus the NEW group "profile hashes": every profile YAML from v1_3 to v1_11 unchanged once frozen (8 files today: v1_3, v1_4, v1_5, v1_6, v1_7, v1_8, v1_9, v1_9s; v1_10 and v1_11 join at their freeze). v1_2 stays pinned by its freeze.json test | GATES.md must resolve the audit's count conflict (13 vs 14 goldens, A6 0.4) from the MM lock files; LOCK_c1 is absent on this Mac: UNVERIFIED which file set "c1 goldens" means until read from the MM worktree |
| G3 identity | frozen v1_9, 1 SOL, 29 Oct capture, `--gross 5.0` (the identityB/C/F/L/M recipe, LARGE_GAPS_RULES.md): the 13 non-manifest output files byte-identical (reference aggregate sha256 prefix 9d5fa86f71393161, A2 0.2c); manifest compared excluding `release_identity.*` and `run` | a code change that touches replay output bytes fails here |
| G4 off-identity | the NEW version with every new switch OFF is byte-identical to its parent on the same 13 files | this is what proves "off by default"; state it in every phase |
| G5 reference loop | `scripts/compare_baselines.py check` on the backtester-loop baselines (legacy_v0, v1, v1_1, v1_2) whenever a changed file has a shim in `new_backtest` (simulation/, strategies/, ems/, circuit_breaker, valuation, order_state_machine) | heavy: one job at a time, the Mac has about 8.6 GB RAM |

Script location (tension to flag): decision 2 asks for "a checking script"; CLAUDE.md and decision 13 say no logic in `out/`. Proposal: the checker is a `hashes` subcommand of the existing `scripts/compare_baselines.py` (A6 0.4: it already owns manifest/compare/check/tests), the pin list is data under `out/integration/baselines/`, and `GATES.md` (the definitions) is a committed document in `out/integration/`. Owner to confirm this reading of decisions 2 and 13.

Also required by every phase: label results EXPLORATORY with profile, size, days, fees (CLAUDE.md); no calibration or profit claim on the 29 Oct capture (decision 6); latency preset `singapore`; one change at a time and measured.

## 2. Owner decisions of 16 Nov (settled)

| # | Decision | Where it lands |
|---|---|---|
| 1 | v1_9 FROZEN now (5 in-place commits; record sha256 at HEAD in the contract; any change = new version). Line: v1_9 reference; v1_10 = v1_9 + withdrawal + S0 repricing + dust carry + new limits + 1 s no-requote; v1_11 = v1_10 + feed health (W1); pair-close = a separate switch, measured on and off, not inside v1_10. v1_8 stays (= v1_7 + quote_withdraw); the old plan's "arb v1_8 for feed health" is superseded by v1_11 | G0, B1, W1. v1_9 sha256 observed 16 Nov: f798ac62497da2f26b93c845b383ea5f47dd6d7409e6122c3f733d7b857be8cb, identical in the working tree and at HEAD (outer 3e0f8bb); its header says the 13 Nov E0 rules (maker ack deadline, silent carry bound) were applied to v1_9 itself, so v1_9 results before 13 Nov carried neither rule |
| 2 | ADOPT the MM session's definitions of c1 and the 14 pinned input hashes; write them in a committed `out/integration/GATES.md` plus a checking script; keep the 14 and add a SEPARATE group "profile hashes" for every YAML v1_3..v1_11 | G0, section 1 |
| 3 | b' (MM 1 July ADA) cannot run any more (MM runner refuses Amend): keep as history; new MM reference b'' = 1 July ADA clean run through `run_mm_session.py` (session quote path, no faults), frozen with output hashes once the MM session commits it; b' vs b'' differences explained by `case2i_compare` | G0 (record), M3 (uses b'') |
| 4 | 5 s mark rule FIX: a mark older than 5 s by its own timestamp pauses; no re-stamping; applies in v1_10 onward; frozen profiles keep the old behaviour, labelled; boundary tests at 4.9 / 5.0 / 5.1 s | B1 (c-APPROVED), closes the re-stamp finding of 6.4 |
| 5 | Dislocation classes: NOTIFY-ONLY first. Fail-closed rule approved as a DEFINITION (DATA or unclassified -> no new quotes; VENUE_EVENT -> no new risk on that venue plus an alarm, any flatten stays manual; TRADEABLE -> normal limits), switched on only in a later profile version after acceptance on 29 Oct and March with the false-alarm rate reported | W6 (build notify-only); switch-on version after acceptance |
| 6 | 29 Oct capture waiver GRANTED for mechanics and validation only; every result using it carries the REJECTED/CRITICAL label and the catalog's reasons; no calibration or profit claim may rest on it | all phases |
| 7 | VM: no changes now; the owner copies files himself (fast streams, derivatives.jsonl, days after 29 Oct); incident capture (2.2) is built and tested in the core recorder on the Mac first; extending the VM recorder needs a separate deployment plan and its own approval | W2b, M1 |
| 8 | Seeds as profile keys from v1_10 with current defaults (outputs unchanged); every manifest records the effective seeds, frozen profiles included | B1 / W2a |
| 9 | Manifest hashes: input hash (sha256 of each input file actually read; fix the empty file list for the capture) and output hashes under `release_identity` / `run`; identity comparison keeps excluding those sections | W2a (2.5) |
| 10 | One session builder (REQUIRED BEFORE any v1_9 or v1_10 paper run): paper and replay build the same session (intent journal, breaker, execution config, layer risk mode); ledger, order trace and intent journal written as each record is made (append and flush); explain the 860 vs 837 ledger rows of d7d-run2, then rerun paper parity on v1_10 | W2a. This is a GATE on paper runs, not a slot in the build session's sequence: no paper run of v1_9 or v1_10 is allowed before it exists |
| 11 | `choose_tactic` in outputs: NO for the arb (absent as expected, refused with maker_taker); add a test that the arb never calls it; MM reason codes only when an MM profile enables it | W2a (test), M2 |
| 12 | Property testing: YES, hypothesis as a TEST-ONLY dependency, pinned version, installed from PyPI on the Mac; nothing at runtime imports it | W2a (infra), W12.4 (ladder) |
| 13 | Logic under `out/` (trace_rejects, stage traces, safety files, evidence builders) moves into `scripts/` or `src/` with tests; `out/` holds outputs only; one commit; prove regenerated outputs byte-identical | W2a |
| 14 | `selftest.py`: a test never asserts a profit; change it to mechanics only, or move to `discarded/` if nothing runs it; report which | W2a. Check 16 Nov: `new_backtest/selftest.py:254` asserts `colocated_both` profit on 25 bps synthetic windows; no file in `src/new_backtest`, `tests/`, `scripts/` references it (grep), so it looks unrun: report to the owner, default proposal = move to `discarded/v<next>/` with a MANIFEST line |
| seq | One session edits the core at a time. USDe fact-check and FIX alignment wait until W6 has network access | section 4, W6, W7 |

## 3. Profile line

| Version | Content | Status |
|---|---|---|
| legacy_v0, v1, v1_1 (control), v1_2 | reference; v1_2 frozen (freeze.json + sha test) | frozen, must stay byte-identical |
| v1_3 .. v1_8, v1_9s | v1_8 = v1_7 + quote_withdraw; v1_9s = v1_9 + S0 repricing ON + quote_withdraw ON | exist; hash group G2 |
| v1_9 | reference, FROZEN 16 Nov (decision 1) | sha256 recorded in section 2 |
| v1_10 | v1_9 + withdrawal + S0 repricing + dust carry + new limits + 1 s no-requote; also the 5 s mark fix (4) and seeds as keys (8) | draft YAML exists in the working tree (16 Nov 06:59, ignored by .gitignore, needs `git add -f`); content not read by this task |
| pair-close | a separate switch measured on and off; NOT inside v1_10 | build session, after the limits |
| v1_11 | v1_10 + feed health (W1); supersedes "arb v1_8 for feed health" | W1 |
| next versions | W2 onward: "next version after v1_11, number assigned at build"; no numbers are coined here | each phase's new switches OFF by default |
| MM | new MM profile version for M2 actions, off by default; b'' reference | MM session |

Mechanism for every new switch: default = old behaviour; the new version sets it; frozen profiles run unchanged and their manifests say which rule applied.

## 4. Sequencing

1. Build session (queue item 1, `local_816c5791-824c-4246-a6e3-dcebe4338911`): hedge dust carry -> v1_10 -> limits -> pair-close switch -> rehearsal rerun. Running. Working tree 16 Nov 07:00: modified core files config.py, ems/intent.py, ems/layer.py, hedge_lifecycle.py, oms.py, session.py (341 insertions), outer session_replay.py, profiles.py and four tests; `HedgePolicy.dust_carry` already present. It was still waiting for the owner's dust-carry and limits defaults when BUILD_QUEUE.md was written; whether they are answered: UNVERIFIED.
2. W1 starts only after item 1 has committed (or the owner says otherwise). W1 steps that need no open decision can start first: 1.1 report-only config audit, baselines, the state model with no action attached.
3. Superseded lines in BUILD_QUEUE.md: "v1_9 is not frozen" (now frozen, decision 1) and "enabled only in v1_10 or later" for W1 (now v1_11).
4. Items not placed in the build session's sequence and needing an owner slot: G0 (GATES.md + `hashes` subcommand; docs and scripts, no core edit, but it shares the outer repo with the build session), decision 13's one-commit move of `out/` logic, decision 10's session builder. Proposal: G0 right after item 1 commits and before v1_10 is accepted; the session builder is the first step of W2.
5. W2 onward follow the owner order, each waiting for the single-editor rule. MM steps are merged after the core step they depend on (section 7).
6. USDe fact-check (6.1) and FIX alignment (7.5) wait until W6 has network access; Phase 0 had none.
7. VM: nothing is changed. The owner copies fast streams, `derivatives.jsonl` and days after 29 Oct himself. No recorder or deployment change on the VM without a separate plan and approval.
8. v1_10 precondition (do not skip): A6 finding 4, MM-side defect C1 (`request_amend` routes the REPLACE without a book, so 73 of 73 confirmed amends end the quote in the MM run; evidence mm_wt `out/mm_case2i/case2i_mm_route_evidence.json`). v1_10 S0 repricing goes through `request_amend` (WT oms.py:2896). C1's status on the integration branch is UNVERIFIED: check it before any v1_10 S0 result counts. C2, C3, C4 (UNWIND child locks base, cumulative fee instead of `fee_delta`) are the same class.

## 5. Phases

Format per phase: IDs targeted; built (extend which module); profile version; acceptance test; dependencies and gate. CLAUDE.md rule 4 applies throughout: extend, do not create; where an audit proposal needs a new file I say so and ask.

### G0 Gates and profile bookkeeping (before v1_10 acceptance)
- IDs: 0.4; 0.3c, 0.3d (manifest entries); 0.3a (owner-gated); supports 0.3e. Group: (a) process.
- Built: `GATES.md` in `out/integration/` (definitions of c1 goldens, the 14 input hashes, the profile-hash group, G1-G5, how each runs, run times); `hashes` subcommand in `scripts/compare_baselines.py` over a pin list under `out/integration/baselines/`; `perp_perp_maker_taker_v1_9.freeze.json` + sha test patterned on the v1_2 one (`tests/test_audit_decision_a.py:274`); the v1_9 sha256 in `docs/strategy_contract.md`; b' recorded as history, b'' recorded when the MM session commits it. Data manifests, all through the existing modules and no writes to `data/`: 0.3c the 29 Oct waiver as a `dataset_manifest.py` entry carrying the REJECTED / CRITICAL label and the catalog's reasons (decision 6); 0.3d `data_manifest.py` / `scripts/queue_v2_manifest.py` extended to March 03-01..07, the TRAIN / SELECT / HOLDOUT split written as data; 0.3a a manifest for the April ADA/MNT public archive pointing at `~/Downloads/bybit_data` (OWNER decision: location, naming such as `bybit-spot-from-archive`, and whether anything may be copied; no third rebuild script).
- Profile: none. Acceptance: the script run twice gives the same verdict; a one-byte change to a pinned YAML makes it fail; GATES.md names every file set. Gate: G1.
- Dependencies: reading the MM lock files in the mm_wt worktree (read-only) to adopt their c1 and 14-hash definitions; the missing `legacy_v0` pinned-hash test that LARGE_GAPS_RULES.md:11 names (A6): not found, to be written or the line corrected.

### B1 Build session (dust carry, v1_10, limits, pair-close, rehearsal rerun)
- IDs: none of the audit rows directly. Side effects: the 5 s mark fix removes the re-stamp finding of 6.4; seeds as keys settle the seed part of 0.2c; the rehearsal rerun can settle A4 finding 6 (`exec_reconciliation ok: false`, UNVERIFIED without a re-run). Groups: dust carry, limits = (c-APPROVED, v1_10 contents); 5 s fix (c-APPROVED).
- Built: `hedge_lifecycle.py` / `oms.py` dust carry (in progress); `ems/` limits; S0 via `ems/tactics/passive.py` `decide_reprice` + `request_amend`; 1 s no-requote; mark-age rule in `session.py` `_margin_marks` behind a profile key (old behaviour is the default; boundary tests 4.9 / 5.0 / 5.1 s); seed keys read in `config.py` `raw_config_from_profile` and recorded in `release_identity`; pair-close as its own switch.
- Profile: v1_10 (pair-close: separate switch, on/off measured).
- Acceptance: G4 (v1_10 with every new switch off equals v1_9 on the 13 files); each new switch measured one at a time on vs off (EXPLORATORY); C1 check before S0 results count; dust-carry measurement labelled: replay instrument rules use `min_notional` "1" (C:config.py:612), not the real 5 USDT, so dust events and hedge refusals are under-counted in replay (the flatten / dust test and the simulated adapter both read `min_notional`: C:oms.py:1550 WT, C:execution.py:1798; magnitude UNVERIFIED; Deliverable F R10).
- Dependencies and gate: G1, G2 (v1_10 joins the hash group at freeze), G3, G4, G5 (hedge_lifecycle, oms, ems touch shimmed code).

### W1 Clock and feed health (v1_11)
- IDs: 0.2a, 0.2b, 1.1-1.8, 0.3b, W13-B1; feeds Q5 (clock uncertainty) and the M1 / M2 consumers.
- Built (EXTEND `time_integrity.py` and `live_feed.py`, plus `book_gate.py`, `quality.py`, `monitoring.py` where they already do the job; a new `feed_health.py` only with the owner's answer, see OPEN):
  - (b) first, no decision needed: 1.1 report-only per-profile feed-health config report (`feed_health` section in `release_identity` and `SessionReplay.config_notes`; keys with core defaults in the profile; v1_2 / v1_9 untouched); per (venue, symbol, stream) rollup in `_VenueTimeStats` (1.2); state model HEALTHY / DEGRADED / UNSAFE (+OWN_LAG as a flag on `TimeAssessment`) with hysteresis and NO action attached (1.3); `receive_mono_ns` on the wire frame and canonical event, additive with a `schema_version` bump (0.2b, 1.5); corrected value and uncertainty stored next to the raw stamp; extend `docs/time/timestamp_dictionary.json`; recorder health block in `live_part_manifest.json` (`_close_part`); flag the silent receive-time substitution (`adapters.py:24, :64`, `sdk.py:248`).
  - (a) correctness, no policy: HostClockMonitor fed by a server-time poll (reuse the ollog method) with the low-quantile estimate as cross-check; fix the `elif` chain that skips the delay check for corrected-negative events (`time_integrity.py:196-215`); fix the service-side monitor that is called with `now_ns = receive_ts` so STALE_FEED never fires (`live_feed.py:1617-1619`); fix `paper_summary.json` `book_integrity_gate`; boundary tests (delay exactly at the limit, strict `>`; offset 100 ms; tolerance 0); a test with a resting quote when the hedge feed goes stale (1.4 gap 5, UNVERIFIED today).
  - (c-PENDING) actions: FEED_DELAYED on and its threshold (1000 vs 2000 ms), cancel / withdraw on DEGRADED, quote withdrawal during a time or gate block, strategy notification that changes behaviour. Built behind switches default OFF; turned on only after the owner decides.
  - 0.3b: a `u = 1` sequence reset (the 2026-04-23 about 06:09 UTC feed restart) becomes a recorded manifest event per file with its resync time, in `quality_preflight.py` / `data_manifest.py`, used as the exclusion source instead of a hard-coded window; the perp ob200 file for 04-23 is checked only with the owner's read permission (not scanned in Phase 0). W13-B1 is the feed-delay injection replay that blocks quoting and logs the state.
  - Historical acceptance (1.7): run the monitor read-only over April ADA/MNT windows (feed-level claims only, the data is spot), 04-23 06:09, 29 Oct and the D7d run2 stalls; store verdicts under `out/integration/`. A notification hook exists, no delivery channel is built (none exists; any network sender needs a separate decision).
- Profile: v1_11 carries the keys; which actions are ON in v1_11 follows the OPEN decisions; state, report and baselines are always observation.
- Acceptance: A1's end-to-end test: a recorded session with a resting quote on each venue, inject hedge-feed stale / loop stall / FEED_DELAYED, assert the documented action and one alert; the monitor verdicts on the four historical windows; G4 for v1_11 with W1 switches off.
- Dependencies and gate: queue item 1 committed; OPEN decisions; replay acceptance of `correct_host_offset` or FEED_DELAYED on March measures the latency model, not the feed (`trust_local_ts` False): acceptance needs the recorded live sessions. G1-G5.

### W2 Incident capture, determinism, parity foundations (next version after v1_11)
- IDs: 0.2c, 0.2d, 2.1-2.5, Q3, 12.2 (builder part).
- W2a, first (this is also the gate on any paper run, decision 10):
  - (a) one session builder: extract `SessionReplay.__init__` lines 243-253 (`B:session_replay.py`) into a function both `SessionReplay` and `live_paper_setup` call (one rule, one place); paper then runs the profile's breaker, execution config, layer risk mode and writes the intent journal.
  - (a) durability: ledger, order trace, intent journal, `paper_timestamps` appended and flushed per record (fsync policy as `durable`; measure persistence lag, run 2 reached 1.34 s); `truncated` counter and flag in `ExecutionLayer.report()` (journal silently stops at 200,000, `ems/layer.py:31`).
  - (a) replay interlock (2.4): refuse non-`INTERNAL_SIMULATION` mode, real adapters, an out_dir containing `live_manifest.json` or `part-*`; record `data_start_ns`, `data_end_ns`, `wall_s`, `speed_x`.
  - (b) manifest: input hash per file actually read and output hashes under `release_identity` / `run` (decision 9); `session` mode in `scripts/compare_baselines.py` (structured diff, first differing row); `PYTHONHASHSEED=0` in pool scripts; "intentional non-determinism" paragraph in `docs/strategy_contract.md`.
  - (a) hygiene (decisions 11, 13, 14): test that the arb never calls `choose_tactic`; move the `out/` logic (`trace_rejects` to a profile key read in `profiles.py`, stage-trace writer from `OI:day1/scenarios.py`, safety / limits / participation files from `OI:day1/run_day.py` into `write_result`) in ONE commit with a proof that regenerated outputs are byte-identical (these files are inside the A1-A7 identity sets); `selftest.py` to mechanics or `discarded/`; hypothesis pinned, test-only.
  - Explain 860 vs 837 d7d-run2 ledger rows (`FEED_CONTROL_STATE_TRANSITION` 56 vs 39, VENUE_CANCEL 28 vs 19, others), then rerun paper parity on v1_10 and write the exact-vs-statistical table into `v1_9_TOLERANCES.md` or `docs/strategy_contract.md` (exact: inputs, books, decisions, intents, simulated acks / fills / hedges, ledger, positions, P&L; statistical: processing time, health transitions from loop stalls, silence-timer cancels, stage timers).
- W2b: (b) `IncidentCapture` inside `live_feed.py` next to `RotatingLiveRecorder` (ring of the last N seconds, trigger table, window cut of at least 90 s before and 5 s after, manifest with rule / symbol / window / frame counts / hashes / completeness, "overload / restart / disk pressure flagged, never silent"); output is a normal `part-*` directory plus a seed snapshot per venue labelled `SEEDED_AT_WINDOW_START` (copied from the real book, never built); built and tested on the Mac in the core recorder (decision 7); a separate module only if the owner agrees, it would overlap `live_feed.py` and `raw_capture.py`. Diagnostics callable on a window (`session_audit.py`). Q3 dry run: cut 90 s + 90 s around the three silence stalls of d7d-run2 (read-only on the existing recording).
- Profile: seeds in v1_10 (done in B1); incident and recorder keys in the next version after v1_11, default OFF; the session builder and manifest hashes change no profile.
- Acceptance: kill -9 mid paper run still leaves every decision / order row up to the kill with an "unsealed" manifest (2.1); replay twice in separate processes: per-file equality, first differing row on a forced one-bit change, manifest input hash equals the recorder's hash (2.5); window replay of a cut capture opens in the session replay (2.3); G4.
- Dependencies and gate: queue item 1 and W1 first (health triggers); capture must never block the event loop; G1-G5.

### W3 Book shape and book risk
- IDs: 0.2e, 0.2f (MM-side, UNVERIFIED), 3.1-3.5, W13-A4, W13-B2, W13-B3; M5 follows.
- Built: (a) tick-aware stretch limit in `simulation/venue_state.py` `_dq_scan` (ADA tick 4.1 bps vs 3 bps stretch: 99.9% flagged); remove or use the dead `SessionReplay._depth_evidence`; one `depth_collapse` parametrised, not a copy. (b) `sweep_in_progress` and advisory `VenueState.book_risk(book, side, size, now, own_takes)` using `impact.fill_range_totals`, flag constants beside `GAP_STATUSES` in `ems/tactics/base.py`, evidence through `_routing_evidence` and `ExecutionLayer._evidence`; empirical surprise table as a pinned JSON like `adverse_table`; per instrument x venue "safe size" table; tests added to `tests/test_depth_quality.py` (a real-book fixture only if the owner allows data access). (c-PENDING) `block_on_toxic` back on, `gap_aware` or `flicker_weight > 0` on, `book_risk` into hedge dispatch.
- Profile: next version after v1_11, evidence on (observation only), `block_on_toxic` unchanged.
- Acceptance: 123 existing tests stay green; ADA fixture no longer flags 99.9%; evaluation on a holdout the owner declares unread (OPEN); FAST_MM vs core flags side by side (M5).
- Dependencies and gate: W1 (feed-fault input); OPEN holdout and `book_risk` wiring; G1-G5.

### W4 Resilience
- IDs: 4.1, 4.2.
- Built: (b) offline resilience measurement over public aggressive runs (spread, top-N depth back to pre-run median, number of best-price updates, event-clock and wall-clock) per venue and instrument on 29 Oct (mechanics only), March Bybit, ADA/MNT; the tool goes in `scripts/` or `src/new_backtest`, NOT `out/` (A3 proposes extending `out/integration/cost/impact_fit.py`; decision 13 overrides). Per-venue tau into the profile `impact:` block; `refill_state` exposed in `book_risk`; `ImpactConfig` per-venue tau, no new class. (c-PENDING) turning `own_impact: refill` on and using resilience as an execution input.
- Profile: `impact:` block in the next version, refill OFF.
- Acceptance: a tau per venue / instrument with an interval, or an explicit "not identified"; the "about 20 best-limit updates" hypothesis tested; no tau used as an execution input before 4.1 (A3 finding 5).
- Dependencies and gate: W3; data labels; G1-G5.

### W6 Dislocation and reference price
- IDs: 0.2g, 6.1-6.5, Q4, W13-B4.
- Built: (b) extend `cross_venue.py` `CrossVenueMonitor` (not a third module): `Episode` open / close (start, end, duration, extremes), executable price at size on both venues (reuse `edge.vwap_for_size`, `fees.py`, `instruments.py`, `derivatives.py`), per-coin baseline of gap size and duration, an assessment record per episode (6.3 fields), fed from `TradingSession.on_event` so replay, paper and live share it; classes DATA / VENUE_EVENT / TRADEABLE with the approved fail-closed DEFINITION, NOTIFY-ONLY (it never trades, never gates; decision 5); `ReferencePrice` block in `derivatives.py` or `valuation.py` (`MidHistory`) with per-coin sources, stamps, age, method, observation count; `_margin_marks` logs book mid, reference and risk mark side by side; (a) fix `STALLED_VENUE` age (from last mid change; the gate uses last message) and `breaches` counting samples. Controlled dislocation replay (Q4) as a labelled FIXTURE (an offset on a COPY of one venue's events, Bybit-only in March, no profit figure). 6.1 doc section (after the web fact-check with network).
- (c) switching the fail-closed rule ON: a later version, only after acceptance on 29 Oct and March with the false-alarm rate reported; until then the records are notifications.
- Profile: monitor keys in the next version, notify-only.
- Acceptance: episode counts by class and duration on 29 Oct (14 h, label REJECTED) and March TRAIN / SELECT; no DATA episode classed TRADEABLE; hour 20 UTC of 29 Oct (gaps up to 90.9 bps, unexplained) classified with evidence; controlled replay: reference stays near the index, divergence alarm fires, no trade or kill-switch decision uses the dislocated mid.
- Dependencies and gate: W1 flags (DATA class needs feed health); network for the USDe fact-check and FIX; March has no Binance depth and no funding file; G1-G5.

### W7 Cost and implementation shortfall
- IDs: 0.2h, 7.1-7.5, W13-A1, W13-C2, W13-C6; Q1 inputs.
- Built: (a) `metrics.pair_economics` join by `intent_id` / `opportunity_id` (now `created_ts` within 1e-4 s, `metrics.py:503`); `_fill_fee` fallback to cumulative fee; guard in `ArbConfig.__post_init__` refusing vwap with nonzero expected slippage (latent double count); call `economics.reconcile` and `economics_gate` from `session_replay` (replay reconciles only to the shadow ledger today); label `equity_at_book_mids` as not P&L. (b) `estimate` dict on `ExecutionIntent.as_record` persisted at decision time with a drop counter (daily-partitioned JSONL, 30 days queryable; the journal cap cannot serve this); `shortfall_split` from `write_result` opt-in using the replay's own touches; arrival and decision mid on the intent, interval VWAP / average from the trade tape (7.4); missing `Metric(...)` entries in `metrics.DICTIONARY` with sign conventions, regenerate `docs/metric_dictionary.md`. (c-PENDING) the single markout definition and the MM adverse sign (OPEN).
- Profile: record fields change journal bytes: identity gates need the E0-style exclusion; next version after v1_11.
- Acceptance: signal row holds fee, spread, impact, adverse, total and replay equals recomputation; one replay with `economic_reconciliation.json` RECONCILED; the four `exec_reconciliation ok: false` manifests re-run (settled by the rehearsal rerun if it covers them).
- Dependencies and gate: network for FIX naming; OPEN markout definition; G1-G5.

### W5 + W10 MarketState fields and diagnostics
- IDs: 5.1, 10.1-10.8, Q1, W13-B6 (exposure steps in the chart), W13-C1, W13-C3, W13-C4, W13-C5.
- Built: (b) optional `MarketState` fields (None = unknown) in `ems/tactics/base.py` and `VenueState.to_execution_state`; VPIN assessed first, no proxy called VPIN (`vpin` in `simulation/models.py:69` never filled; microprice, burst, `book_hole`, `sweep_risk` fields hard-set to 0/False at `venue_state.py:575-600`: compute or leave None); one context row per fill from `_obs_fill`; `pair_economics` for pairing none (MM quotes get no pair rows); exclusion counter in the pooled markout path; `decomposed_summary` by regime with CIs; one report module in `new_backtest` (or `cycle_reports.py`) fed by the output dir and `session_manifest.json`, asset-agnostic (pair, venues, tick, lot from the manifest), called by `write_result` and paper / testnet teardown; retire the `out/` builders to `discarded/` with MANIFEST lines (five builders overlap: do not add a sixth); synchronized chart (10.4), timing distributions with the timestamp pair (10.5), cost section pre-trade vs realised (10.6), self-check page Q1-Q5 with a seeded fill sampler (10.7; check overlap with `out/integration/reports/claim_rows.json` first).
- Profile: observation only, next version after v1_11.
- Acceptance: per-fill context row present for every fill; report generated unmodified for SOL and for an ADA/MNT window; Q1 on real orders is MISSING until real arb orders exist and the owner names the four parts (OPEN).
- Dependencies and gate: W7, W3, W1 (health at fill), W2 (timestamps); G1-G5.

### W8 Routing evidence and selection
- IDs: 0.2i, 8.1-8.4, Q2, W13-A2, W13-A5, W13-A6, W13-B7, W13-C7 (the rest of W13-A / B / C sit with the phases that build their components: A1, C2, C6 in W7; A3 in W9; A4, B2, B3 in W3; B1 in W1; B4 in W6; B5 in W12; B6, C1, C3, C4, C5 in W5+W10).
- Built: (b) `ExecutionLayer.set_routing` / `decide_route` call `assign_rest_hedge` with real inputs (`fees.py` FeeSchedule, spread and impact from `simulation/impact.py`, adverse from `edge.py`) in `fixed` mode, REPORT ONLY; one journal record per decision (chosen, alternatives incl. one-venue / split / sequential / fallback / wait, costs, reason, inputs, profile sha and `release.py` version, snapshot reference = feed offset / sequence); immutable record: hash chain plus sequence, dropped counter beside `JOURNAL_MAX`; input assembler for latency, reject rate, throttle headroom, health, inventory; check `ems/ioc_policy.py` and `ioc_runtime.py` for overlap before adding any chooser. (c-PENDING) `dynamic` routing, any reroute or fallback venue (adds duplicate-exposure risk; `hedge_blocked` and reservation rules must cover it first).
- Profile: next version after v1_11; routing evidence on, `fixed` mode.
- Acceptance: reproduce a historical decision from its record without future data (8.4); policy table test: each market state gives one action with a reason code (W13-B7).
- Dependencies and gate: W1, W3, W6; journal bytes change: exclusion as E0; G1-G5.

### W9 Almgren-Chriss hedge schedule
- IDs: 9.1, 9.2, W13-A3.
- Built: (b) trajectory as a pure function in `ems/tactics/base.py` or `ems/venue_selector.py` beside `split_single_venue` with a `hedge_mode` (fast / N slices over T / wait), default fast; a schedule arm switch in `batch_backtest.py` (planned runner home). (c-PENDING) enabling any schedule: LARGE_GAPS_RESULTS.md found no mode beating all-at-once at 100-1000 SOL on the risk-adjusted net; unhedged drift p99 6-17 bps vs 1.0.
- Profile: next version, mode fast.
- Acceptance: same-conditions comparison harness in `src/`; a holdout the owner declares unread for large sizes.
- Dependencies and gate: W7, W8; compute heavy; G1-G5.

### W11
- No audit rows (no 11.x ID in the brief or the matrix). Scope UNVERIFIED: ask the owner for the order text. Nothing planned.

### W12 Timing categories, parity, testnet, test ladder
- IDs: 12.1-12.4, Q5, W13-B5 (acked / partial / rejected / unknown reaches a terminal state with journal rows, in a fault replay and on testnet).
- Built: (b) per-chain `hedge_start_ts_ns` / `hedge_complete_ts_ns`; causal-interval summary in `latency_report()` (decision-send, send-ack, ack-fill, fill-hedge-send, hedge-send-hedge-fill, p50 / p99, labelled event-time vs wall), compared across paper / replay / testnet with `calibration.compare_runs`; a runnable `VENUE_TESTNET` session wrapper around `LivePaperSession` with `build_testnet_adapters`, small size and the probe's 20 / 200 USDT caps, a "testnet limits" section in `docs/execution_realism.md`; the test ladder (unit / property -> integration -> historical -> incident replay -> paper / testnet -> shadow) written into `docs/strategy_contract.md` or `docs/requirements_traceability.md` with per-stage gates reusing G1-G5, hypothesis property tests, a shadow stage (paper run with orders to a would-send log); Q5 cross-clock timeline with +- uncertainty per clock and ambiguous-order marking (`replay.py`, `session_audit.time_integrity`).
- Profile: none (tooling and records).
- Acceptance: 60 min paper run on v1_10 in one command, `run_recorded` replay, parity report with every difference classified (12.2); a testnet bot run covering maker, taker hedge, cancel, amend, unwind, breaker, reconcile, restart (12.3, owner approval for any key use; keys only from the environment); Q5 report.
- Dependencies and gate: W1, W2, W7, W10; keys and real testnet orders need the owner's approval each time; G1-G5.

## 6. The three groups

### (a) Safety and correctness fixes (no strategy change; default behaviour of frozen profiles unchanged)
| Item | Audit ID | Phase |
|---|---|---|
| Quotes not withdrawn during a time block or gate block (action is (c); the test and the fix of the hedge-feed-stale case are (a) once decided) | 1.4 | W1 |
| Service-side `TimeIntegrityMonitor` called with `now = receive_ts` so STALE_FEED never fires; two monitors, one policy | 1.4, A1 f4 | W1 |
| Silent substitution of receive time for exchange time | 0.2b, 1.5 | W1 |
| Misleading `paper_summary` gate block | 1.8 | W1 |
| `elif` chain skips the delay check; no boundary tests; HostClockMonitor circular | 1.6 | W1 |
| Journal silently capped at 200,000, no `truncated` flag | 0.2d, 8.4 | W2a |
| Paper and replay build different sessions; ledger and journal not durable until finalize | 0.2d, 2.1, 12.2 | W2a |
| No replay interlock | 2.4 | W2a |
| No input or output hashes in the manifest; empty file list for the capture | 2.5 | W2a |
| `choose_tactic` never-called test for the arb | 0.2d | W2a |
| `selftest.py` asserts profit; logic under `out/`; `trace_rejects` not a profile key | 0.2d, 10.8 | W2a |
| `_impact_sync` defined twice (AST-identical today) and dead `_depth_evidence` in `session_replay.py` | A2 f8, A3 f11 | W2a or W3 |
| ADA tick defect in the depth-quality stretch limit | 3.1 | W3 |
| `STALLED_VENUE` age basis; `breaches` counts samples | 0.2g | W6 |
| Pair join by `created_ts`; `_fill_fee` cumulative fallback | 0.2h | W7 |
| `economics.reconcile` not run on replay; `exec_reconciliation ok: false` | 0.2h, 7.1 | W7 |
| vwap plus nonzero slippage double count has no guard | 7.1 | W7 |
| Two defaults for `missing_price_action` (breach in `margin.py:186`, pause in `config.py:604`) | 6.4 | W7 or W6 |
| Per-fill rows missing context; `pair_economics` ignores pairing none; pooled markout without exclusion counter | 10.1, 10.2 | W10 |
| Gate definitions absent; `legacy_v0` pin test missing | 0.4 | G0 |
| Core-to-new_backtest string coupling (`cli.py:131-132, :349`): move `live_paper_setup` into the core with the session builder | A2 12.2 | W2a |

### (b) New capabilities (observation, records, tooling; all OFF or report-only by default)
Feed-health state model, rollups, baselines, health block in part manifests (W1); incident capture, window replay, replay-twice tool (W2); `book_risk`, `sweep_in_progress`, safe-size table (W3); resilience measurement (W4); episode records, executable-price gap, reference price, controlled replay (W6, notify-only); estimate-at-decision record, benchmarks, shortfall split in `write_result`, cost dictionary (W7); MarketState fields, context rows, one report module, synchronized chart, self-check page (W5+W10); per-decision routing record with hash chain, fixed-mode costs (W8); `hedge_mode` pure function with default fast (W9); causal-interval report, testnet wrapper, ladder, shadow stage, timeline (W12); MM recorder on `raw_capture` (M1).

### (c) Strategy optimisation (may change trading policy; owner approval first)
APPROVED (owner, 16 Nov, or earlier settled):
- v1_10 contents: quote withdrawal, S0 repricing, dust carry, new limits, 1 s no-requote.
- 5 s mark rule fix (a mark older than 5 s by its own timestamp pauses; v1_10 onward).
- Dislocation classes as a notify-only DEFINITION; switching the fail-closed rule on is a later version after acceptance.
- Pair-close as a separate switch, measured on and off.
PENDING (no owner decision; nothing is to be built to run ON):
- FEED_DELAYED on and its threshold (1000 vs 2000 ms); cancel / withdraw on DEGRADED; quote withdrawal during blocks.
- `book_risk` wired into hedge dispatch; `choose_tactic` for arb hedges; `block_on_toxic` back on; `gap_aware`, `flicker_weight > 0`.
- Dynamic routing, any reroute or fallback venue; split or sequential hedges.
- `own_impact: refill` and resilience as an execution input (tau unidentified, 0.5-200 s across fits).
- Almgren-Chriss or any time-sliced hedge mode.
- One markout definition (pooled vs decomposed) and the MM adverse-fill sign; the benchmark for implementation shortfall.
- Any use of a reference price in margin or risk.

## 7. MM steps (merged after the core step; MM session builds, mm-integration is 18 ahead / 4 behind, locked worktree)
| Step | After | Note |
|---|---|---|
| M1 MM recorder on core `raw_capture` + feed health | W1 | Mac / public feed only; VM deployment needs its own plan (decision 7); no new file unless an MM adapter is needed (ask) |
| M2 MM profile actions on health events, default OFF | W1 and M1 | new MM profile version; UNSAFE cancel-all without reconciliation can leave unknown orders |
| M3 rerun 1 July ADA with preflight on, attribute differences | G0 and b'' | reference is b'' (decision 3); core defects C1-C4 must be fixed or labelled first or the attribution is confounded; attribute with `case2i_compare` |
| M5 FAST_MM detectors vs core detectors on ADA/MNT | W3 | core ADA flag repaired first; Bybit only (no Binance ADA depth, no MNTUSDT perp) |
| M4 MM implementation shortfall and diagnostics on the 6 July days | W7 and W10, and the OPEN markout decision | 100 ms archive caveat: 97% of 0.1 s outcomes are the next snapshot; EXPLORATORY, 2 coins |
| M6 MM paper-vs-replay parity | W2, W12 and M1 | paper MM session on the public feed recorded with M1's recorder, replay compared order by order |

## 8. Still OPEN (no owner decision yet; nothing assumed)
| Item | Blocks |
|---|---|
| FEED_DELAYED threshold 1000 vs 2000 ms (max_book_age is 500 ms) | W1 action switches |
| Cancel / withdraw on DEGRADED allowed? (trading policy) | W1 actions, M2 |
| Extend `time_integrity` + `live_feed` vs a new `feed_health.py` (queued as "extending time_integrity") | W1 file layout, state-machine home |
| Which markout definition (pooled vs decomposed) and adverse-fill sign for MM | W7, W10, M4 |
| Q1 "four parts" (the split has six: delay, hedge-latency, hedge-impact, fee, quote-price term, allowances) | W10, Q1 |
| Which holdout is still unread (March 13-15 read at least twice) | W3 3.5, W9 9.2 |
| Wiring `book_risk` into hedge dispatch | W3 to W8 |
| Moving the two unloadable config YAMLs (`config.yaml`, `config.testnet.example.yaml`) to `discarded/` | W1 1.1 cleanup |
| Answers to the build session's dust-carry (4 defaults) and Order 3 defaults | B1 |
| Confirm the script-location reading of decisions 2 and 13; the `selftest.py` outcome | G0, W2a |
