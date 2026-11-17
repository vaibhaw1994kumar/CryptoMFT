# Build queue after the Phase 0 audit (owner decisions, 16 Nov 2026)

One session edits the core at a time. Status 16 Nov (updated): the build session has STARTED editing (dust carry in `oms.py` and `hedge_lifecycle.py`, a v1_10 profile draft; nothing committed yet, the Order 1 suite run was in progress). Everything else below is not built.

## Profile line (decision 1)
- v1_9: FROZEN reference (record its sha256 at HEAD in the contract; any change = new version). v1_9s unchanged. v1_8 stays (v1_7 + quote_withdraw).
- v1_10 = v1_9 + withdrawal + S0 repricing + dust carry + the new limits + 1 s no-requote; also the 5 s mark fix (4) and seeds as profile keys (8).
- v1_11 = v1_10 + feed health (W1). The old plan's "arb v1_8" for feed health is superseded.
- pair-close (`arb.close_pairs: reduce_only_quote`): a separate switch, measured on and off, not inside v1_10.

## Queue
1. BUILD SESSION `local_816c5791-824c-4246-a6e3-dcebe4338911`, one commit per step after the suite check: dust carry -> v1_10 (with decisions 1, 4, 8) -> limits (order rate, daily loss stop, clock-offset gate, margin WARNING, missing marks, tier table, 1 s no-requote, verify_account, stress proof) -> pair-close switch -> rehearsal rerun (new report beside the 13 Nov one). Defaults approved by the owner 16 Nov.
2. W1 feed health and clock -> profile v1_11. Starts after queue item 1 has committed. EXTEND `time_integrity.py` and `live_feed.py` (new `feed_health.py` only after asking). Still open: FEED_DELAYED threshold (1000 or 2000 ms), whether cancel or withdraw on DEGRADED is allowed.
3. Gate file (decision 2): `out/integration/GATES.md` plus a checking script. Adopt the MM session's c1 and the 14 pinned input hashes as they are (definitions in the MM session lock files on branch `mm-integration`), plus a SEPARATE group "profile hashes" for every profile YAML v1_3 to v1_11.
4. Manifest hashes (9): input hash (sha256 of each input file actually read; fix the empty capture list) and output hashes under `release_identity` / `run`; the identity comparison keeps excluding them. Effective seeds in every manifest, frozen profiles included (8).
5. One session builder for paper and replay (10), required before any v1_9 / v1_10 paper run: same intent journal, breaker, execution config, layer risk mode; append-and-flush ledger, order trace and intent journal; explain the 860 vs 837 ledger rows of d7d-run2; then rerun paper parity on v1_10.
6. Incident capture (2.2, W2) in the core recorder on the Mac, tested there first. Extending the VM recorder: separate deployment plan and its own approval (7).
7. Dislocation classes (5): NOTIFY-ONLY first (DATA or unclassified -> no new quotes; VENUE_EVENT -> no new risk on that venue plus alarm, flatten manual; TRADEABLE -> normal limits as a definition); switched on only in a later profile version after acceptance on 29 Oct and March with the false-alarm rate reported.
8. Test and housekeeping commits: arb never calls `choose_tactic` (11, test); `hypothesis` as a test-only pinned dependency from PyPI on the Mac (12); evidence builders (trace_rejects, stage traces, safety files) out of `out/` into `scripts/` or `src/` with tests, one commit, regenerated outputs byte-identical (13); `selftest.py` asserts mechanics only or moves to `discarded/`, report which (14).
9. MM side (3): b' stays as history; new reference b'' = the 1 July ADA clean run through `run_mm_session.py`, frozen with output hashes once the MM session commits it; differences explained by `case2i_compare`.

## Data rules (6, 7)
29 Oct capture: waiver granted for mechanics and validation only; every result carries the REJECTED/CRITICAL label and the catalog's reasons; no calibration or profit claim rests on it. VM: no changes now; the owner copies files himself (fast streams, `derivatives.jsonl`, days after 29 Oct).

## Mark rule (4)
A mark older than 5 s by its own timestamp pauses, no re-stamping; v1_10 onward; frozen profiles keep the old behaviour, labelled; boundary tests at 4.9 / 5.0 / 5.1 s.

## Still open (no owner decision yet)
Pooled vs decomposed markout and the MM adverse-fill sign; Q1 "four parts"; which holdout is unread; wiring `book_risk` into the hedge dispatch; moving the two unloadable config YAMLs (`config.yaml`, `config.testnet.example.yaml`) to `discarded/`. The USDe fact-check and FIX alignment wait for W6 with network access.

## Meanwhile (this session, read-only)
Deliverables B (architecture map), C (prioritised plan), F (risk register) under `out/integration/audit/`.

## A-to-W mapping (added 17 Nov 2026)
Two numbering systems. A1-A9, B, C are parts of the owner's Order 3 (16 Nov): the limits in `docs/strategy_contract.md` ("Session limits", line ~418). W1-W13 are the SRT order's build items (see DELIVERABLE_C). A is not a W. The mapping below is my reading of the two texts. It is UNVERIFIED against the original SRT order text, which is not in this folder (DELIVERABLE_C says the same about its own W mapping).

| A item (Order 3) | What it is | W row it touches | Effect on the W item |
|---|---|---|---|
| A1 order rate | token bucket 2/s, burst 5, per venue | none | none |
| A2 daily loss stop | latched kill at -5 USDT per SOL of clip | none | none |
| A3 clock-offset gate | no new quotes if offset unknown / older than 900 s / uncertainty above 5 ms; source = venue server time read the ollog way; `HostClockMonitor` is NOT a source | W1 rows 1.x (clock) | Covers the "server-time poll as clock source" part of W1 (a) and gives the gate. W1 still owns: state model HEALTHY / DEGRADED / UNSAFE, per-stream rollups, `receive_mono_ns`, the `elif` chain fix, the service-side STALE_FEED fix, `paper_summary` gate block, 0.3b, W13-B1, historical acceptance. Not verified: whether `HostClockMonitor` itself is now fed (the contract says it is not a source) |
| A4 margin WARNING stop | stop new quotes on real WARNING or free margin < 10x initial margin | none | none |
| A5 missing marks > 60 s | latched kill; amends "price pause never trips the kill switch" for v1_10 on | W7 / W6 item 6.4 (two defaults for `missing_price_action`) | Touches it but does not close it: the two defaults (`margin.py:186` breach vs `config.py:604` pause) are not unified by A5 |
| A6 tier table | read venue risk tiers at start-up (real-order modes) | none | none (endpoint shapes UNVERIFIED until the testnet drill) |
| A7 1 s no-requote after withdrawal | Order 2 | none | none |
| A8 account check at start-up | one-way mode, margin mode, leverage, reduce-only | W12 (testnet / drill) | The testnet drill that confirms A6 and A8 belongs with W12's testnet work; not built there |
| A9 stress proof, plus the fixes ordered 17 Nov | 10,000 events/s x 5 s; measured and FAILED; owner ordered (a) recorder per-frame cost cut, (b) arrival stamp | W1 1.5 / 0.2b (receive time) and W2a (durability) | (b) overlaps W1's `receive_mono_ns` on the wire frame: W1 must reuse whatever the build session stamps, not add a second stamp. (a) CONFLICTS with W2a "ledger, order trace, intent journal appended and flushed per record": A9 moves the recorder to a bounded flush interval. W2a must state the durability policy for the recorder and the journals separately before it is built |

B and C of Order 3 have no W row. B = pair-close switch (separate from v1_10). C = rehearsal rerun, new report beside the 13 Nov one.

Remaining W1 work after A3 and the A9 fixes: state model, rollups, baselines, health block in manifests, `elif` and STALE_FEED fixes, `paper_summary` gate block, 0.3b, W13-B1, historical acceptance, and the open decisions (FEED_DELAYED threshold, cancel / withdraw on DEGRADED). Re-scope W1 against what the build session actually commits for A3 and the arrival stamp before starting it.

Status 17 Nov: Order 1 (dust carry) and Order 2 (v1_10) are committed; Order 3 A1-A8 is committed (core bd8c8eb, outer a28210d); A9 ran and FAILED, fixes in progress in the build session; B and C wait behind them.

## SETTLED (owner, 16-17 Nov 2026; taken from the owner's paste of 17 Nov)
This section supersedes the "Still open" list above and the W1 "OPEN" questions where they conflict. Details of the DEGRADED/UNSAFE actions are "as specified in the owner's 16 Nov answers"; that specification is not reproduced in this file.
- New `feed_health.py` approved (answers the "new module or extend" question).
- FEED_DELAYED threshold: 1000 ms in v1_11.
- DEGRADED / UNSAFE actions: as specified by the owner (16 Nov).
- Markout: decomposed, measured after the fill, + = favourable. FAST_MM's pre-fill signal stays separate.
- "Four parts" = pre-trade delay, fees, impact, opportunity; mapped to the six-term split.
- March holdout is SPENT; the next recorder days are reserved as the unread holdout.
- `book_risk`: observation only, never in the hedge dispatch.
- `config.yaml` to `discarded/` (after the secrets check). `config.testnet.example.yaml` fixed to the schema.

## REVISED ORDER (owner, 17 Nov 2026) - replaces the "all 12 steps" order
(a) 2i core fixes 1-9 (amend confirmation, amend timeout query, lost amend then cancel, MM exits, fee_delta, incremental cost growth, MM interface, settings-hash fix, MARGIN_STATE_TRANSITION explanation), then 2i item 10 (dense arb case on v1_10);
(b) finish the rehearsal rerun;
(c) one session builder for paper and replay, a profile -> real-order session command, journals written as each record is made;
(d) W1 feed health and clock -> v1_11, with the clock-source decision (rework onto `feed_health.py`);
(e) Bybit tier endpoint, then the testnet drill on v1_10 (testnet only);
(f) gates, manifest hashes, settings-hash fix, core speed-up, housekeeping;
(g) incident capture (W2b).
Then, only after (g): W6 notify-only; W7/W10 limited to "persist the pre-trade estimate per order".

## PARKED until the owner reopens them
W8 routing selector; W9 Almgren-Chriss; the Cont-Kukanov split; W5 VPIN / conditions; W3 / W4 book-shape and resilience models (`book_risk` observation stays); further execution-cost studies; a faster recorder for 10,000 events/s (A9 stays "sustains about 3,000-3,500/s per venue, target not met"). The four design briefs in `out/integration/designs/` stay on disk; W3_W4, W8_W9_W12 and most of W7_W5_W10 are parked.

One core-editing session at a time; each step reports the suite and identity gates.
