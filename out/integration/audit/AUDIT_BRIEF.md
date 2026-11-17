# SRT infrastructure audit: common brief for the Phase 0 agents (read-only)

Owner order: "SRT TRADING INFRASTRUCTURE - AUDIT, BUILD AND ACCEPTANCE" (16 Nov 2026). This is PHASE 0, read-only. Result: deliverable A, the audit matrix, one file per agent.

## Hard rules (every agent)
- READ ONLY. No edits to any source, config, profile, test or data file. No writes to `data/`. No downloads, no network, no VM access, no replays, no backtests, no pytest of the full suite, no other heavy job. A short read-only Python call (import and print, e.g. resolve a profile through `new_backtest/profiles.py`) is fine; a single targeted test file may be run if it takes under a minute and writes nothing outside /tmp. Never print environment variables or key material.
- The ONLY file you write is your own findings file named in your task, under `/Users/vaibhav/Documents/Newproject/out/integration/audit/`.
- Never open or print `FAST_MM/hft_scaffold/check_link_orders_history.py`, `src/crypto_stat_arb/Documents`, `Documents.pub`.
- AUDIT THE WORKING COPY ONLY. Cite code only from `/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src/crypto_stat_arb/` (core platform), `/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src/new_backtest/` (backtester), `crypto_MFT_arb_platform/tests/`, `crypto_MFT_arb_platform/profiles/`, `crypto_MFT_arb_platform/docs/`, `order_lifecycle_logger/` (ollog, testnet/mainnet probes and the recorder-side tools) and `out/integration/`. FAST_MM (`Newproject/FAST_MM/`) is the market-making package: read it for concepts, report it as MM-side evidence, ignore every `*.bak*` file. IGNORE and never cite: the root-level duplicates `Newproject/*.py` (monitoring.py, live_feed.py, ledger.py, oms.py and so on), `Newproject/src|tests|scripts`, the nested `crypto_stat_arb/crypto_MFT_arb_platform/`, `crypto_stat_arb/advanced_backtest/`, `out/**/snap*/`, `discarded/`, `_backups/`.
- Never invent a result. If something is missing, say MISSING and name the blocker. A claim needs a `file:line` (or a command you ran and its output). If you could not look at something, say UNVERIFIED and why.

## Status classes (use exactly these words)
VERIFIED_COMPLETE: the requirement is implemented, wired into the path it is meant for, AND there is recorded end-to-end evidence (a run, a report, an output file you can name). A unit test alone is NOT enough.
PARTIAL: part of it exists. UNVERIFIED: code and/or tests exist but no recorded end-to-end evidence, or you could not check. MISSING: nothing exists. FAILING: it exists and a test or run shows it wrong (give the test or output).
Tests that exist and pass with no recorded end-to-end evidence = UNVERIFIED, never VERIFIED_COMPLETE.

## Already covered by the "Large-size gaps + probe" order (15 Nov): do NOT redo, mark "covered by LARGE_GAPS step N" and point to the output
fill-range rule (step 1, `simulation/impact.py`, `ems/tactics/base.py`), own consumption and refill (step 2), CVaR waiting cost (step 3), large-size and time-sliced tests, reaction overlay (4), resting-hedge realism (5), ADA/MNT flicker and cliff (6), 2,035-fill probe shortfall (7), T1 framing caveat (8), fast-stream backups (9), mainnet probe (10). Results: `out/integration/cost/LARGE_GAPS_RESULTS.md`, `LARGE_GAPS_RULES.md`, `G_RESULTS.md`, `PERP_QUEUE_VALIDATION_PLAN.md`. Committed: outer 3e0f8bb, core 0436b3a.

## Output format: one block per requirement ID, in this exact shape
```
## <ID> - <short title>
- Status: <one status class>
- Exists: what exists (files, classes, functions, config keys, call paths) - each with file:line
- Wired into: live / paper / testnet / replay (say which, with the call path), or "not wired"
- Effective config per profile: key = value for the profiles that matter (arb v1_2, v1_9, v1_9s; MM profile where relevant); say where the value comes from
- Tests and evidence: test files and what they cover; recorded runs or reports (paths); results you saw
- Exact gap: what is missing, wrong or untested
- Implementation: how to build it, naming the EXISTING module to extend (CLAUDE.md: extend, do not create; if a new file seems needed, say so and give the overlap)
- Acceptance test: how it would be verified end to end
- Dependencies and risk: other IDs, data needs, what is unsafe to enable
- Completion evidence: what would count as done
```
Finish the file with a section "## Findings that need the owner" (decisions, conflicts, anything surprising) and "## Returned IDs" listing every ID of your task, in order. Keep each block compact (a few lines per field); precision over length. Your final chat reply is a SHORT summary (at most 15 lines): counts per status, the five most important gaps, owner questions. The file is the deliverable.

## Requirement ID list (each ID belongs to exactly one agent; the merge diffs the returned IDs against this list)
- A1 feed health and clock: 0.2a (feed-health pieces: time_integrity correct_host_offset, max_feed_delay_ns / FEED_DELAYED, HostClockMonitor, book_gate, live_feed silence limits, quality.py, monitoring.py, raw_capture), 0.2b (recorder timestamp fields: ollog recorder, the market-data recorder as far as it is in this repo), 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8
- A2 capture, determinism, parity: 0.2c (replay engine determinism scope), 0.2d (intent journal, reject trace, choose_tactic reason codes), 2.1, 2.2, 2.3, 2.4, 2.5, 12.1, 12.2, 12.3, 12.4, Q3, Q5
- A3 book shape, resilience, conditions: 0.2e (depth-quality fields), 0.2f (FAST_MM regime_gate book_hole_detected / depth_collapse / sweep_risk: implementation, data used, calibration, whether they act), 3.1, 3.2, 3.3, 3.4, 3.5, 4.1, 4.2, 5.1, M5
- A4 dislocation and cost: 0.2g (CrossVenueMonitor, cross_venue.py), 0.2h (existing markouts 1/5/30 s, cost breakdowns, ledger P&L, pair-economics file: formulas, timestamp basis, completeness), 6.1 (record only what the repo already says on persistent gaps, executable arb, artefacts, stress dislocations; the WEB fact-check of the October 2025 USDe/Binance facts is DEFERRED to the W6 build phase, say so), 6.2, 6.3, 6.4 (incl. the existing 5 s last-good-mark rule and where it is deployed), 6.5, 7.1, 7.2, 7.3, 7.4, 7.5, Q4
- A5 routing, speed, diagnostics: 0.2i (venue-selection code, Phase E status), 8.1, 8.2, 8.3, 8.4, 9.1, 9.2, 10.1, 10.2, 10.3 (find and verify the MM "+2 captured / -4 adverse" definition in FAST_MM), 10.4, 10.5, 10.6, 10.7, 10.8, Q1 (find the "+8.15 believed / +3.83 realised" records: where, units, sample), Q2, W13-A, W13-B, W13-C (map each question of the three-stage list to a component, gap and acceptance test)
- A6 datasets, gates, MM package: 0.3a (April ADA/MNT recorder data), 0.3b (04-23 06:09 UTC restart), 0.3c (29 Oct SOL capture, D7d silences, both venues), 0.3d (Bybit March), 0.3e (1 July ADA b' run: dataset, strategy, config), 0.4 (gate definitions: where the "c1 goldens", the "14 profile hashes", the "Phase 0 list" (baselines/failing_tests_phase0.txt), "b' 1 July ADA identical" are defined and how they run; whether profile arb v1_8 exists and what v1_8, v1_9, v1_9s are; if "c1 goldens" or "14 profile hashes" cannot be found say so, do not substitute), M1, M2, M3, M4, M6 (status and dependencies only: the MM session builds them; M5 belongs to A3)
- Cross-check items all agents report under "Findings that need the owner": overlap with existing modules before any new file (CLAUDE.md: extend, do not create); anything that would change trading policy; anything unsafe to enable.
