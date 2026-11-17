# GATES: what a change must pass, and the pinned hashes

Status: DRAFT written 17 Nov 2026 by the G0 side session (docs and scripts only; nothing committed). Owner decision 2 of 16 Nov asked for this file and a checking script (`out/integration/audit/BUILD_QUEUE.md` item 3, `DELIVERABLE_C_prioritised_plan.md` section 1 and G0). All P&L anywhere near these gates is EXPLORATORY.

Every statement below is marked by its source. "Observed" means I ran a read-only command on 17 Nov 2026 on this Mac. "Copied" means the value was read from a file and not recomputed. UNVERIFIED means I could not check it.

## 1. The gates

| Gate | What it checks | Evidence that counts | How it runs | Source |
|---|---|---|---|---|
| G0 pins | the pinned hashes in section 3 (14 input files, profile hashes v1_3 .. v1_11, freeze files) against disk and git HEAD; the golden hashes are recorded here and checked only with `--golden-root` (none are checked by default; section 4) | the printed PASS / FAIL list of the command below, exit code 0, and the SUMMARY line, including the count "goldens X/Y checked" | `python3.12 crypto_MFT_arb_platform/scripts/compare_baselines.py hashes` (from `Newproject/`; light, reads files, writes nothing). Its own test: `... compare_baselines.py hashes-selftest` | defined by this file under decision 2; the script is the `hashes` subcommand proposed in DELIVERABLE_C section 1 |
| G1 suite | the full test suite fails only on the tests listed in `out/integration/baselines/failing_tests_phase0.txt` | real counts (passed / failed / errors) and the output of `compare_baselines.py tests LOG` with `UNCHANGED: true`; new tests must pass; new tests never go on the list | from `crypto_MFT_arb_platform/`: `PYTHONPATH=src python3.12 -m pytest -rfE tests > LOG`, then `python3.12 scripts/compare_baselines.py tests LOG`. The list has 93 names: 92 FAILED + 1 collection ERROR, all under `tests/platform_kit` | DELIVERABLE_C section 1; A6 0.4 (`compare_baselines.py` `tests`). Last recorded result: 3,749 passed, 92 failed, 1 collection error (`out/integration/cost/LARGE_GAPS_RESULTS.md`, per A6). Not re-run by me |
| G2 pins | the G0 pin block still holds on the tree being accepted: the 14 pinned input files unchanged, every pinned or frozen profile YAML unchanged, no frozen profile edited in place | G0's output at the commit being accepted; a version that is frozen later is added to the pin block in the same commit as its freeze file | same command as G0 (G0 is the mechanism, G2 is the requirement at each phase) | DELIVERABLE_C section 1 (G2) |
| G3 identity | frozen v1_9, 1 SOL, 29 Oct 2026 capture, `--gross 5.0` (the identityB/C/F/L/M recipe in `out/integration/cost/LARGE_GAPS_RULES.md`): the 13 non-manifest output files byte-identical; the manifest compared excluding `release_identity.*` and `run` | the replay's output files and a per-file equality list. The reference aggregate sha256 prefix is 9d5fa86f71393161 (A2 0.2c, copied here as a prefix only; the full value and the file list are NOT in this file: UNVERIFIED by me) | heavy replay, one heavy job at a time, run by the build session; no script of this file runs it | DELIVERABLE_C section 1 (G3); the 29 Oct capture carries the waiver label REJECTED / CRITICAL (BUILD_QUEUE data rules) |
| G4 off-identity | the NEW version with every new switch OFF is byte-identical to its parent on the same 13 files | the same per-file equality list, new version against parent; each phase states it | heavy replay, as G3 | DELIVERABLE_C section 1 (G4) |
| G5 reference loop | `compare_baselines.py check` on the backtester-loop baselines (legacy_v0, v1, v1_1, v1_2), whenever a changed file has a shim in `new_backtest` (simulation/, strategies/, ems/, circuit_breaker, valuation, order_state_machine) | `ALL_IDENTICAL: true` from the check, runs listed | `python3.12 scripts/compare_baselines.py check LABEL [subset]` against the committed `out/integration/baselines/manifest_b1.json`; heavy: one job at a time (the Mac has about 8.6 GB RAM, per C) | DELIVERABLE_C section 1 (G5); `compare_baselines.py` docstring |

Run times: not recorded in any file I read; I do not state any.

What the gates do not cover (recorded, not fixed here): the legacy_v0 pinned-hash TEST that `out/integration/cost/LARGE_GAPS_RULES.md` line 11 names does not exist in `tests/` (A6 0.4 found none; UNVERIFIED by me, I did not search again). legacy_v0 is covered in G0 only by its YAML hash and by the baselines in G5.

## 2. Verdict words printed by the checker

PASS (file on disk equals the pin; for profiles and freeze files also equal at git HEAD) | FAIL (differs, missing, not tracked at HEAD, a NOT_CREATED file appeared, or the pin block is damaged) | NOT CHECKED (a golden whose run directory is not on this machine, or whose byte check is excluded by design: it is a record, never a PASS) | INFO (a NOT_FROZEN profile: the observed state is shown, never a failure; a NOT_CREATED file still absent). Exit code 1 if there is any FAIL, otherwise 0. Statuses in the pin block: PINNED, FROZEN, NOT_FROZEN, NOT_CREATED, GOLDEN, GOLDEN_NOCHECK. The block is read by the script; do not edit it by hand except by the procedure in section 6.

## 3. Profile hashes (summary of the pin block)

Computed by me on 17 Nov 2026 from the working copy `crypto_MFT_arb_platform/profiles/` and confirmed equal to git HEAD (outer f45fedf) by the checker. The 16 Nov prefixes in A6 0.4 (v1_3 f6a4137c, v1_4 0142fbaf, v1_5 9235acad, v1_6 3eeea315, v1_7 b4ba591c, v1_8 e52b22aa, v1_9 f798ac62, v1_9s 3d037d2b) are the same as the values below.

| Profile | Status in this file | sha256 | Notes |
|---|---|---|---|
| v1_3 .. v1_8 | PINNED (17 Nov) | in the block | working profiles; "pinned" = guarded against change; there is no owner freeze file for them (A6 0.4) |
| v1_9 | FROZEN (owner, 16 Nov) | f798ac62497da2f26b93c845b383ea5f47dd6d7409e6122c3f733d7b857be8cb | equals `profile_sha256` in `perp_perp_maker_taker_v1_9.freeze.json` (committed in 27a838e); checked by the script |
| v1_9s | PINNED | 3d037d2be19a2ffbf8cc3586bbcbbc7ee6ed20e5f17553b936b3aba2d1cf1d0a | listed as a reference profile in `v1_9.freeze.json`; checked by the script |
| v1_10 | NOT FROZEN | e0fb1910fd3c890469f8d1a7da45ca4f07d415dcd36b6795fbe00774ca436a18 observed 17 Nov, NEVER pinned | still changing (BUILD_QUEUE; last commit f45fedf turned arrival stamps on). Shown as INFO. Joins the PINNED group only at its freeze |
| v1_11 | NOT CREATED | none | planned as v1_10 + feed health (W1); the checker FAILs if the file appears while this file says NOT_CREATED |

Not in the "v1_3 .. v1_11" group, covered elsewhere: legacy_v0, v1, v1_1, v1_2 and `v1_2.freeze.json` are in the 14 inputs (section 4); the two spot profiles (spot_perp_basis_v1, spot_spot_maker_taker_v1) are in no pin group (A6 listed their prefixes 1de39a19 and 45a4d11b; not pinned here, owner to say if they should be).

## 4. Adopted from the MM session (copied, not recomputed)

"14" means 14 pinned INPUT FILES (profiles, freeze file, calibration and fit files, manifests), not 14 profile YAMLs. The `profiles/` directory holds 17 YAML or JSON files today; the MM list names 5 of them.

| MM source | Where it is (read only on 17 Nov) | What was taken |
|---|---|---|
| `baseline_hashes.json` (recorded 8 Nov 2026; outer 66dd9d0, core b16bc49, "clean checkout of both branch heads") | `/Users/vaibhav/Documents/mm_wt/out/mm_integration/baseline_hashes.json`. This is OUTSIDE the `mm_wt/Newproject` checkout and untracked: it is not a file of branch mm-integration. File sha256 f6a6d205fe1a0fb94f4f57411f2c3fb953b6a06669e16449e6e58d453411f1a6 | the 14 sha256 values: group "inputs" in the pin block |
| `LOCK_c2.json` (locked 9 Nov 2026 on a cloud container; scope: March 2024-03-11 slice plus January goldens) | tracked on branch mm-integration: `git -C /Users/vaibhav/Documents/mm_wt/Newproject show mm-integration:out/integration/baselines/runs/mmb/LOCK_c2.json` equals the working file (sha256 1496e244adfe925a2f5ee844eb8b514d3751fa9883dd4ce8585f907f5cd1bd36; last commit 394976f; mm-integration head 150af1a) | 101 file hashes of 11 golden runs: group "golden" in the pin block. These are the c2 hashes. LOCK_c2 says (`vs_c1`) every trade, fill and P&L field is identical to c1, that `summary.json` differs by design (results identical) and that the g1s `session_manifest.json` differs only in the source-code hash |
| `LOCK_c3.json` (10 Nov 2026) | `/Users/vaibhav/Documents/mm_wt/Newproject/out/mm_integration/LOCK_c3.json`, untracked in the worktree; sha256 4b991858ff6f5940017e316b5909ba4da38c1f5b134b8f61e3c96ed7ebe066c9 | only the statements: reference is "c1 (mmb)"; 9/9 baselines identical; 4/4 ledger cells identical (g1s manifest differs in `sha256_sources` only); "14/14 match out/mm_integration/baseline_hashes.json"; suite 92 failed, 3134 passed, 7 skipped, 11 xfailed, 1 error, compare_baselines UNCHANGED true. No hashes were taken from it |

What is NOT there, said plainly:
- `LOCK_c1.json` is not on this Mac. I searched `Documents` and `Downloads` for `LOCK_c1*` and found none. So the c1 hashes themselves are not available; the golden group holds the c2 hashes that LOCK_c2 states equal c1 on trade, fill and P&L fields. They are labelled as c2 here, not relabelled as c1.
- The count conflict stays OPEN: LOCK_c2 says "11 of the 13 c1 goldens" (the two not re-run on 10 Nov by owner cut: `perp_perp_maker_taker_v1` and `perp_perp_maker_taker_v1_1` on 2022-01-05, so those two have no hash here), while the MM report text `mm_spot_apr2026_report.py` says "14 golden runs" (per A6 0.4; I did not re-open that script, it is not at the path A6 gave inside the worktree: UNVERIFIED). I cannot resolve 13 vs 14 from the files I could read.
- The golden run directories (the fresh outputs of a golden replay) are NOT on this Mac: 0 `trades.jsonl` under `mm_wt/Newproject/out/integration/baselines/runs`, and no `runs/c2` in this repo. The checker therefore checks a golden line ONLY when `--golden-root DIR` is given (DIR holding the same relative paths, e.g. a fresh replay's output); otherwise every golden line prints NOT CHECKED and the SUMMARY shows "goldens 0/101 checked". It never searches `out/` for old artefacts, so a stale file cannot pass or fail a gate by accident. `summary.json` and `session_manifest.json` lines are recorded but never byte-checked (GOLDEN_NOCHECK), as `compare_baselines.py` already excludes them.
- Side observation, NOT a gate result (one-off read-only comparison on 17 Nov, run before I restricted the checker): this repo holds two of the cells under `out/integration/phase_b/ledger_c2/runs/` (`ft` and `g1l`; files dated 8 Nov 06:42, one day before LOCK_c2's 9 Nov lock; the cells `g1o` and `g1s` and the March / January baseline runs of the golden list are not there). 16 of their 17 non-excluded files equal the LOCK_c2 hash; `g1l/.../economic_reconciliation.json` does NOT (LOCK_c2 5ffc0e09cae1, on disk b2e35a207ba1). I did not look into why (the c2 copy of that file is not on this Mac; the cause could be the 8 Nov vs 9 Nov run, or a real difference). UNVERIFIED; the MM session or the owner should check it when the c2 run files are available.
- The path from MM `dir` fields (`/Users/vaibhav/Documents/mm_wt/Newproject/out/integration/...`) was rewritten to a path relative to `out/integration/`. No other change.
- b' (MM 1 July ADA run) is history; the new MM reference b'' is recorded here only when the MM session commits it (decision 3, BUILD_QUEUE item 9). Not recorded yet.

## 5. Open items (not decided here)

1. Owner: confirm that "c1 goldens" means the LOCK_c2 11 runs (+2 not re-run) and that the c2 hashes may stand in until LOCK_c1 is found (13 vs 14).
2. DECIDED by the owner on 17 Nov 2026 (chat): the two spot profiles (spot_perp_basis_v1, spot_spot_maker_taker_v1) stay OUT of the pin block. v1_9's sha256 is already in `docs/strategy_contract.md` (line 407, committed in 27a838e, value equal to the pin); owner asked on 17 Nov to add it, nothing further was needed.
3. The `legacy_v0` pinned-hash test named in LARGE_GAPS_RULES.md:11 is not found; write it or correct that line (build session, tests).
4. This checker is the `hashes` subcommand of the existing script (the reading proposed in DELIVERABLE_C section 1). CONFIRMED by the owner on 17 Nov 2026 (relayed in chat to this side session). The pin list is inside this file, not a second file under `baselines/`, so there is one source of truth.
5. The checker's own test is a subcommand (`hashes-selftest`), not a file in `tests/`, so the running suite and its Phase 0 list are not disturbed; move it into `tests/` as a pytest wrapper at commit time if wanted.

## 6. How to change a pin (the only way)

1. A version is frozen: the owner approves; add `profiles/<name>.freeze.json` (patterned on the v1_2 and v1_9 ones); change its line in the block from NOT_FROZEN to FROZEN with the sha256; add the freeze file under group "freeze".
2. A new version appears (v1_11 etc.): replace NOT_CREATED by NOT_FROZEN with the observed hash; the checker FAILs until this is done.
3. A pinned file must change: it should not. Make a new version instead. If the owner approves an exception, say so in the commit and change the line.
4. After any change: run `compare_baselines.py hashes` and `hashes-selftest`; both must pass; commit with the change.

## 7. The pin block (read by the script)

Line format `group | status | sha256 | path`; paths are relative to `Newproject/` except group golden, which is relative to `Newproject/out/integration/`. Lines starting with # are comments.

<!-- PINS:BEGIN -->
# group 1: the 14 pinned input files, copied from the MM file baseline_hashes.json (value for value, not recomputed)
inputs | PINNED | dc89d62b7c2003baee9d7932aa07130e8ea2b89082cf13f26ccef434e578bb17 | crypto_MFT_arb_platform/profiles/legacy_v0.yaml
inputs | PINNED | 2c70171b6630e04656a8545dc9cf6a333a324648a3153c4687160cf4007cbdb4 | crypto_MFT_arb_platform/profiles/perp_perp_maker_taker_v1.yaml
inputs | PINNED | bae35cff2e8ffef9f4da6ffdde08a9039179754ed7b062eca92d74d20e7412a1 | crypto_MFT_arb_platform/profiles/perp_perp_maker_taker_v1_1.yaml
inputs | PINNED | 734d7d2cf54db3bdb9b16a0d930be7cd1890649d9ed770bae9f8676797368a46 | crypto_MFT_arb_platform/profiles/perp_perp_maker_taker_v1_2.yaml
inputs | PINNED | 4e775e407472797e4f236d177ca90e98cbdc5ea31b7d41390b7322a86464c298 | crypto_MFT_arb_platform/profiles/perp_perp_maker_taker_v1_2.freeze.json
inputs | PINNED | 4984335f71e91afff6b9a42432c2694c08b1b782e6b6ed2bcaebbc31ef83d5c0 | crypto_MFT_arb_platform/config/lifecycle_calibration_perp_bybit.json
inputs | PINNED | c369b1b97af87dd7cecf146cce0583361be0d026a1d6dc6e3fed0d32506e8de7 | crypto_MFT_arb_platform/config/hedge_move_arrival45ms_mar2024_train_0301_0307_v1.json
inputs | PINNED | c7caa3982aeef0df8630479dacb8cd9382391d2673afc97560c7bb99c56d77b6 | out/queue_v2/hedge_move/jan_train_for_2022-01-13.json
inputs | PINNED | ba8cc52f0844c920ebee934daf5c0c4585cd0e7efceb238efdb512d7f65f26fb | out/queue_v2/hedge_move/mar_train_2024-03-01_07.json
inputs | PINNED | c5f00c379d8eccf10a53e959ea85c29fd7d6440fa6c3261e79dced518f65f7ba | out/queue_v2/unknown_queue_fit_for_2022-01-05.json
inputs | PINNED | 30c5a65f245e4a38a437ac5e19404a82740e06d575f3c6fcd2c7ec0d612c92f2 | out/queue_v2/unknown_queue_fit_for_2022-01-13.json
inputs | PINNED | 5fb59b9081c04f26c2cce978f81413d15882675099aadd20eca0770938e6f972 | out/queue_v2/unknown_queue_fit_mar2024_NONE.json
inputs | PINNED | 698431278b62b67c89b43fa49c888721624aba00b3cad6565bda9426b892c210 | out/queue_v2/manifest.json
inputs | PINNED | f1b6eee8f872939ef1471da046ed1271baab2fd3b5e2cfbae586a345b31c1172 | out/queue_v2/manifest_mar2024.json

# group 2: profile hashes v1_3 .. v1_11 (computed from the working copy on 17 Nov 2026 and checked against git HEAD; no MM source)
profiles | PINNED | f6a4137c8e252411e079fd23f265cdca6d2900f65f3eb7870c8ba42b673d876f | crypto_MFT_arb_platform/profiles/perp_perp_maker_taker_v1_3.yaml
profiles | PINNED | 0142fbaf61db51839c400a22a15441914e7393301acb14650604ca310504a0fd | crypto_MFT_arb_platform/profiles/perp_perp_maker_taker_v1_4.yaml
profiles | PINNED | 9235acad441e2e45ca6c94709038db4d80d056bd53daa972c3bd221c5729fbd1 | crypto_MFT_arb_platform/profiles/perp_perp_maker_taker_v1_5.yaml
profiles | PINNED | 3eeea31564d7f6d29891b2c0d3c0ed11d1cdce7c110081bc56f50f3afbf6863a | crypto_MFT_arb_platform/profiles/perp_perp_maker_taker_v1_6.yaml
profiles | PINNED | b4ba591c3461536a24939ea779973e8210bd887ba0260840353ca39cf4dcc07b | crypto_MFT_arb_platform/profiles/perp_perp_maker_taker_v1_7.yaml
profiles | PINNED | e52b22aa0bb088484838d2ea4e31c61e9c71330d5bec6383d78e035dcd1e84ac | crypto_MFT_arb_platform/profiles/perp_perp_maker_taker_v1_8.yaml
profiles | FROZEN | f798ac62497da2f26b93c845b383ea5f47dd6d7409e6122c3f733d7b857be8cb | crypto_MFT_arb_platform/profiles/perp_perp_maker_taker_v1_9.yaml
profiles | PINNED | 3d037d2be19a2ffbf8cc3586bbcbbc7ee6ed20e5f17553b936b3aba2d1cf1d0a | crypto_MFT_arb_platform/profiles/perp_perp_maker_taker_v1_9s.yaml
profiles | NOT_FROZEN | e0fb1910fd3c890469f8d1a7da45ca4f07d415dcd36b6795fbe00774ca436a18 | crypto_MFT_arb_platform/profiles/perp_perp_maker_taker_v1_10.yaml
profiles | NOT_CREATED | - | crypto_MFT_arb_platform/profiles/perp_perp_maker_taker_v1_11.yaml

# group 3: freeze files (v1_2 freeze file is in group 1)
freeze | PINNED | c452f37087a72f1a8e04e9c00a87cdd9f0e3b33473e973a2fc1ff6301e9b4624 | crypto_MFT_arb_platform/profiles/perp_perp_maker_taker_v1_9.freeze.json

# group 4: c1 golden run files; values are the c2 hashes of mm-integration LOCK_c2.json (see section 4); path is relative to out/integration/
# legacy_v0 2024-03-11
golden | GOLDEN | 31e6b9b02512d507fd0263e3c23a891e8c08279fa17dc3ef792312c6dc13f305 | baselines/runs/c2/mar/base/legacy_v0/2024-03-11/fill_bucket_stats.json
golden | GOLDEN | b674c6179734bfab8130bf838516820020207d08e429c2508d48c275f151418c | baselines/runs/c2/mar/base/legacy_v0/2024-03-11/fill_events.jsonl
golden | GOLDEN | 1978247202060065667965a8378cd8770c9956172420d44c46c1184b416958e8 | baselines/runs/c2/mar/base/legacy_v0/2024-03-11/hedge_attempts.json
golden | GOLDEN | 92669bac65533d486861d032cf44ffac3ca5108821f0958016ae609cb10413b7 | baselines/runs/c2/mar/base/legacy_v0/2024-03-11/legging_rows.json
golden | GOLDEN | 363111a0cad78be8e5d87eb23bf04747e76f5ebfd024757a90504acfeecb6061 | baselines/runs/c2/mar/base/legacy_v0/2024-03-11/maker_observations.jsonl
golden | GOLDEN | e5e28d95fb69e53910ff92a624a773f49d50269a6f6fb7337187ee97d7d8243d | baselines/runs/c2/mar/base/legacy_v0/2024-03-11/model_settings.json
golden | GOLDEN_NOCHECK | 907c117fce582da1df17e20de0a65c055fc844d21bb6dc48dcffaf387506ab77 | baselines/runs/c2/mar/base/legacy_v0/2024-03-11/summary.json
golden | GOLDEN | 2305afd9cafaf7669ecc94dd8574324d5bdc3327e5ad3fd07565bc2ec1dcc580 | baselines/runs/c2/mar/base/legacy_v0/2024-03-11/trades.jsonl
# perp_perp_maker_taker_v1 2024-03-11
golden | GOLDEN | b27e2f342c3209d7ad918c127985756e5a7a8b02df96af71b359760a6f25a95a | baselines/runs/c2/mar/base/perp_perp_maker_taker_v1/2024-03-11/fill_bucket_stats.json
golden | GOLDEN | 03b947aedfe928f0d896a6cad478ec32155291202ade28e9277b24fcd6f21a98 | baselines/runs/c2/mar/base/perp_perp_maker_taker_v1/2024-03-11/fill_events.jsonl
golden | GOLDEN | 916f70c8d68c47f6f46fef8e841ff8b8f349e40858b66409c52b87d6a098abe5 | baselines/runs/c2/mar/base/perp_perp_maker_taker_v1/2024-03-11/hedge_attempts.json
golden | GOLDEN | cf809e78e7398a9beb1e537b41dcce901b7a07d18f6eb3edb12cc34e2af533cb | baselines/runs/c2/mar/base/perp_perp_maker_taker_v1/2024-03-11/legging_rows.json
golden | GOLDEN | cce4d4ac1e06e3b3dac43029c74054853b7aad402aac3b995a7066cca9a83858 | baselines/runs/c2/mar/base/perp_perp_maker_taker_v1/2024-03-11/maker_observations.jsonl
golden | GOLDEN | 6f1a3b8a34f2de16fd7efbce258f0a68fcfaf9238a875383a831cc8bad4a2dfd | baselines/runs/c2/mar/base/perp_perp_maker_taker_v1/2024-03-11/model_settings.json
golden | GOLDEN_NOCHECK | 46dd714cec2af9483f5e5e254ab2d700bcb67ffce31f6a41adf446789aebe6e9 | baselines/runs/c2/mar/base/perp_perp_maker_taker_v1/2024-03-11/summary.json
golden | GOLDEN | 005c3d26755dc3a8dc14e8d753521ebe232e5ddac314ee39650e8e00dba3a242 | baselines/runs/c2/mar/base/perp_perp_maker_taker_v1/2024-03-11/trades.jsonl
# perp_perp_maker_taker_v1_1 2024-03-11
golden | GOLDEN | c8d07ed0b5a34cf5a17b80fb93a72a4cd0bdca9f726e1749aa2c28256993b841 | baselines/runs/c2/mar/base/perp_perp_maker_taker_v1_1/2024-03-11/fill_bucket_stats.json
golden | GOLDEN | 4f50eea5b4847cef1ba2fddf5e149c09dce1ed708d6163f778840e65cc76910f | baselines/runs/c2/mar/base/perp_perp_maker_taker_v1_1/2024-03-11/fill_events.jsonl
golden | GOLDEN | 1716bf3e9c8b66a22751dab6943ca8e7bb688c14023499226c7c28300154b877 | baselines/runs/c2/mar/base/perp_perp_maker_taker_v1_1/2024-03-11/hedge_attempts.json
golden | GOLDEN | db42d732feb8b8a1629d02d03065d79bb4985a357d8c3f37c8aa9f3589db3a8d | baselines/runs/c2/mar/base/perp_perp_maker_taker_v1_1/2024-03-11/legging_rows.json
golden | GOLDEN | 6c41c82e3b09b7aba6023aba987d42f001d7d5166d13986aa436ff67b957d8ec | baselines/runs/c2/mar/base/perp_perp_maker_taker_v1_1/2024-03-11/maker_observations.jsonl
golden | GOLDEN | 1188e30c57080cbda60e7988f455eaf3d493db1ef8b28034359aa1f1f8c51787 | baselines/runs/c2/mar/base/perp_perp_maker_taker_v1_1/2024-03-11/model_settings.json
golden | GOLDEN_NOCHECK | fd53e9fe3c0de892c2150fc642bf903c8f3fc2dc86489ca7e96745d14fd68c26 | baselines/runs/c2/mar/base/perp_perp_maker_taker_v1_1/2024-03-11/summary.json
golden | GOLDEN | e8e671fb13be415345400a14ca34cac8ef54d23ed750a278ae8c27f8be569ded | baselines/runs/c2/mar/base/perp_perp_maker_taker_v1_1/2024-03-11/trades.jsonl
# perp_perp_maker_taker_v1_2 2024-03-11 (cell ft)
golden | GOLDEN | 707f504c8a933109d11c90d2b8f044cff493a4f5499b6746b5f6d0ed0fd4f8a3 | phase_b/ledger_c2/runs/ft/base/perp_perp_maker_taker_v1_2/2024-03-11/fill_bucket_stats.json
golden | GOLDEN | bc2eae05e2807cf2ae3dc3d7365b5c533ad1edfeb7f790e5080e6b3746482cb7 | phase_b/ledger_c2/runs/ft/base/perp_perp_maker_taker_v1_2/2024-03-11/fill_events.jsonl
golden | GOLDEN | 219cf788086bfccfd396d67c2b10281a8469668f15e797e26ddd57ebb80fe2f3 | phase_b/ledger_c2/runs/ft/base/perp_perp_maker_taker_v1_2/2024-03-11/hedge_attempts.json
golden | GOLDEN | 08b4c6a20d604c0a48267552481eefaf1aea2351e000435dc324ac61d92b091f | phase_b/ledger_c2/runs/ft/base/perp_perp_maker_taker_v1_2/2024-03-11/legging_rows.json
golden | GOLDEN | cdf00d34db1df87dd9b65141126838b04bea3382c67ca6f79850c7f2c994abf6 | phase_b/ledger_c2/runs/ft/base/perp_perp_maker_taker_v1_2/2024-03-11/maker_observations.jsonl
golden | GOLDEN | 200fc17df3b3d5d490a46c6530085d0325aa0881220cc507e04b546d873d9cdc | phase_b/ledger_c2/runs/ft/base/perp_perp_maker_taker_v1_2/2024-03-11/model_settings.json
golden | GOLDEN_NOCHECK | c2991909adbfe4f249df0badacad65b700e133a9efdb91355dd8da815bbc4f32 | phase_b/ledger_c2/runs/ft/base/perp_perp_maker_taker_v1_2/2024-03-11/summary.json
golden | GOLDEN | 87d4210e1ec954e6e28e111d5b21b4c9b430044754e00a788ade51c4805e6dda | phase_b/ledger_c2/runs/ft/base/perp_perp_maker_taker_v1_2/2024-03-11/trades.jsonl
# perp_perp_maker_taker_v1_4 2024-03-11 (cell g1o)
golden | GOLDEN | e504cfe8d33c900e24368a47b2b880f1f54ceebe5135b7c8a1d2f0128e7a7f81 | phase_b/ledger_c2/runs/g1o/base/perp_perp_maker_taker_v1_4/2024-03-11/core_shadow.json
golden | GOLDEN | 55178fb38dff369396165a1c6cccb3a84d87f830d835db32456b7ec3e12f5475 | phase_b/ledger_c2/runs/g1o/base/perp_perp_maker_taker_v1_4/2024-03-11/data_quality_report.json
golden | GOLDEN | 5ffc0e09cae19c2f469ee608b341fe13de21f8c16b98801875618022011ec791 | phase_b/ledger_c2/runs/g1o/base/perp_perp_maker_taker_v1_4/2024-03-11/economic_reconciliation.json
golden | GOLDEN | ec711a4c0e0b928e72c7257e7d6a7baa97015a8fe998664553934c741621b337 | phase_b/ledger_c2/runs/g1o/base/perp_perp_maker_taker_v1_4/2024-03-11/fill_bucket_stats.json
golden | GOLDEN | c56857b72495224641adfa47869b1414cb0b49daaed47229b1da3ce55fbf866f | phase_b/ledger_c2/runs/g1o/base/perp_perp_maker_taker_v1_4/2024-03-11/fill_events.jsonl
golden | GOLDEN | 041df65501348db8e2c8f7a004180ea625092175ff7bfedddf7b302d2265ddcd | phase_b/ledger_c2/runs/g1o/base/perp_perp_maker_taker_v1_4/2024-03-11/hedge_attempts.json
golden | GOLDEN | 4bb435faa1890a819e95e9d1846ac90a25649b5e5a5751c943c12de052588d41 | phase_b/ledger_c2/runs/g1o/base/perp_perp_maker_taker_v1_4/2024-03-11/legging_rows.json
golden | GOLDEN | aa2d214364c4b92cf659eb32b682adc865230815feef5264eaf3c9da73e0d6dd | phase_b/ledger_c2/runs/g1o/base/perp_perp_maker_taker_v1_4/2024-03-11/maker_observations.jsonl
golden | GOLDEN | 200fc17df3b3d5d490a46c6530085d0325aa0881220cc507e04b546d873d9cdc | phase_b/ledger_c2/runs/g1o/base/perp_perp_maker_taker_v1_4/2024-03-11/model_settings.json
golden | GOLDEN | 7691a83d1ee5e081644f7ce453ac003adbc25693cd76c0e450482c0881d5cf10 | phase_b/ledger_c2/runs/g1o/base/perp_perp_maker_taker_v1_4/2024-03-11/order_events.jsonl
golden | GOLDEN_NOCHECK | 7906e0a38d6d9290374fc66b83ffe556bf0c03f3985369f1fef13ac768d4e198 | phase_b/ledger_c2/runs/g1o/base/perp_perp_maker_taker_v1_4/2024-03-11/summary.json
golden | GOLDEN | f0b48856b9e294fc02ade62d73ecb5f9701524bcf24e0584b8d706317af70669 | phase_b/ledger_c2/runs/g1o/base/perp_perp_maker_taker_v1_4/2024-03-11/trades.jsonl
# perp_perp_maker_taker_v1_4 2024-03-11 (cell g1l)
golden | GOLDEN | e504cfe8d33c900e24368a47b2b880f1f54ceebe5135b7c8a1d2f0128e7a7f81 | phase_b/ledger_c2/runs/g1l/base/perp_perp_maker_taker_v1_4/2024-03-11/core_shadow.json
golden | GOLDEN | 55178fb38dff369396165a1c6cccb3a84d87f830d835db32456b7ec3e12f5475 | phase_b/ledger_c2/runs/g1l/base/perp_perp_maker_taker_v1_4/2024-03-11/data_quality_report.json
golden | GOLDEN | 5ffc0e09cae19c2f469ee608b341fe13de21f8c16b98801875618022011ec791 | phase_b/ledger_c2/runs/g1l/base/perp_perp_maker_taker_v1_4/2024-03-11/economic_reconciliation.json
golden | GOLDEN | ec711a4c0e0b928e72c7257e7d6a7baa97015a8fe998664553934c741621b337 | phase_b/ledger_c2/runs/g1l/base/perp_perp_maker_taker_v1_4/2024-03-11/fill_bucket_stats.json
golden | GOLDEN | c56857b72495224641adfa47869b1414cb0b49daaed47229b1da3ce55fbf866f | phase_b/ledger_c2/runs/g1l/base/perp_perp_maker_taker_v1_4/2024-03-11/fill_events.jsonl
golden | GOLDEN | 041df65501348db8e2c8f7a004180ea625092175ff7bfedddf7b302d2265ddcd | phase_b/ledger_c2/runs/g1l/base/perp_perp_maker_taker_v1_4/2024-03-11/hedge_attempts.json
golden | GOLDEN | 4bb435faa1890a819e95e9d1846ac90a25649b5e5a5751c943c12de052588d41 | phase_b/ledger_c2/runs/g1l/base/perp_perp_maker_taker_v1_4/2024-03-11/legging_rows.json
golden | GOLDEN | aa2d214364c4b92cf659eb32b682adc865230815feef5264eaf3c9da73e0d6dd | phase_b/ledger_c2/runs/g1l/base/perp_perp_maker_taker_v1_4/2024-03-11/maker_observations.jsonl
golden | GOLDEN | 200fc17df3b3d5d490a46c6530085d0325aa0881220cc507e04b546d873d9cdc | phase_b/ledger_c2/runs/g1l/base/perp_perp_maker_taker_v1_4/2024-03-11/model_settings.json
golden | GOLDEN_NOCHECK | f8a24d3e340ace9066d7624eea1556aabffd5c94ad227ffdd821b296d2b9f2d4 | phase_b/ledger_c2/runs/g1l/base/perp_perp_maker_taker_v1_4/2024-03-11/summary.json
golden | GOLDEN | f0b48856b9e294fc02ade62d73ecb5f9701524bcf24e0584b8d706317af70669 | phase_b/ledger_c2/runs/g1l/base/perp_perp_maker_taker_v1_4/2024-03-11/trades.jsonl
# perp_perp_maker_taker_v1_5 2024-03-11 (cell g1s)
golden | GOLDEN | 39003fbe4be4fafc8616cee53595165323ac699e670d41e982fe15ed15f4bd64 | phase_b/ledger_c2/runs/g1s/base/perp_perp_maker_taker_v1_5/2024-03-11/order_times.json
golden | GOLDEN | 750786e3a8ef0ad041d88224c9dea695a39111c11652453575648a0776acafeb | phase_b/ledger_c2/runs/g1s/base/perp_perp_maker_taker_v1_5/2024-03-11/session_fills.jsonl
golden | GOLDEN_NOCHECK | 23360fd2d17805461719c803d3f0517b1d74f0a1d3fd2c6c739a0a3ef4830304 | phase_b/ledger_c2/runs/g1s/base/perp_perp_maker_taker_v1_5/2024-03-11/session_manifest.json
golden | GOLDEN | c0ac7783de8247b0857d00e5754af0288908eae1e2402b5f144f938b95acfbbb | phase_b/ledger_c2/runs/g1s/base/perp_perp_maker_taker_v1_5/2024-03-11/session_orders.jsonl
golden | GOLDEN | 4a6131452891ed6942ecd0bdd810129c181a953b78af5b1bf650935be9c56a86 | phase_b/ledger_c2/runs/g1s/base/perp_perp_maker_taker_v1_5/2024-03-11/session_signals.jsonl
golden | GOLDEN_NOCHECK | c054ceedaa69908642d394a1943326c4c47e9f33bb1f49874fe283e1671ee1e8 | phase_b/ledger_c2/runs/g1s/base/perp_perp_maker_taker_v1_5/2024-03-11/summary.json
# legacy_v0 2022-01-05
golden | GOLDEN | 4e59c7e7651306d44975009c86cdd0ebfbf5820cee1c9a1fab35fc90d9cb1331 | baselines/runs/c2/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-05/execution_reconciliation.json
golden | GOLDEN | 7bcc05e2eaaed5c5b5cd6e0850de2f5063f4563cdd966ecc2607b571c1b0d651 | baselines/runs/c2/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-05/execution_report.json
golden | GOLDEN | cdecd1eee571d655db72144cf777b3efb18c0e7cccd9ada275ffb399af15317c | baselines/runs/c2/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-05/fill_bucket_stats.json
golden | GOLDEN | e01caeeebd714d8ae2861595d719adf914b6f140d56fc7f5ffb13d4101365866 | baselines/runs/c2/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-05/fill_events.jsonl
golden | GOLDEN | 943d1ad53ba73b67df57e2a67d6ae6a6336bccdee867393d17389d4c96ca8a00 | baselines/runs/c2/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-05/ledger_stats.json
golden | GOLDEN | 8dcaa3eff7ca8497cdc5d1f17b2e125998f73fe9bed47268ae234fbd11054b16 | baselines/runs/c2/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-05/maker_observations.jsonl
golden | GOLDEN | e57ec65fee0e1b58392c4ce84ade29aeeda3ae3c5e2b734047d050d7372aff8d | baselines/runs/c2/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-05/maker_observations_summary.json
golden | GOLDEN | ec920fc828298357b87cb49f2919303bff1adc3c91c5c742a8e719d244a15010 | baselines/runs/c2/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-05/model_settings.json
golden | GOLDEN_NOCHECK | 910856e27e74d443c1a60a6db198190b628a677d2f8320b73488e53b3a50050f | baselines/runs/c2/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-05/summary.json
golden | GOLDEN | c78fbbc6e58e1196a225fe04ff46576b1264489694e743a9723d236b034a0c21 | baselines/runs/c2/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-05/trades.jsonl
# legacy_v0 2022-01-13
golden | GOLDEN | 6edb53915c62796f474d935c2213cc59747a95ab5670e5bdf0d28bdf14879766 | baselines/runs/c2/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/execution_reconciliation.json
golden | GOLDEN | 89c0e208121ea34b5d0f9d236242c9915f891543a2519f7dc9d4c4a378fc9255 | baselines/runs/c2/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/execution_report.json
golden | GOLDEN | 53ad66db1c599f43d0f0803bb066ee82a677a5550b2bc175ea0555e09f1d7859 | baselines/runs/c2/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/fill_bucket_stats.json
golden | GOLDEN | 57e687e57caee4546a6c96b5f033bf1a5e3f72b46893633ce9a9d1db7748b380 | baselines/runs/c2/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/fill_events.jsonl
golden | GOLDEN | c30ce1f40ceec083b0b1370a37e0201d71b08c53aef077da71f761d914aedf3f | baselines/runs/c2/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/ledger_stats.json
golden | GOLDEN | 6149aa54a49aae908347f74ef06086da5b43caa6bb04b39ee23409b3b99e8d7a | baselines/runs/c2/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/maker_observations.jsonl
golden | GOLDEN | 3c0e2a5e83fe05cd1e8b0944e4454394bb52e5553b0572b63085dc7f02b10017 | baselines/runs/c2/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/maker_observations_summary.json
golden | GOLDEN | ec920fc828298357b87cb49f2919303bff1adc3c91c5c742a8e719d244a15010 | baselines/runs/c2/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/model_settings.json
golden | GOLDEN_NOCHECK | 3de544802a6517a23d779ac54625922a0317cf1d370c582e9c046154dbd32ff4 | baselines/runs/c2/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/summary.json
golden | GOLDEN | 2c5dd48caba8b807484e19734b1d0296f3a307b4c91985771de60c4ff10345c2 | baselines/runs/c2/jan/legacy_v0/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/trades.jsonl
# perp_perp_maker_taker_v1 2022-01-13
golden | GOLDEN | 0e4f2b444a0898fcc533132cd3e68484556a3b04b15f224778ec4b3c2964a4ba | baselines/runs/c2/jan/perp_perp_maker_taker_v1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/execution_reconciliation.json
golden | GOLDEN | bd5d2ff70cd4c4304056f34c5eafe3b9f8d2a28eabf8effac76d74f01e3fa51a | baselines/runs/c2/jan/perp_perp_maker_taker_v1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/execution_report.json
golden | GOLDEN | 510a96ba718c7ee829ed7d2500dbb18007f57be9642b701699952e58d97a1d79 | baselines/runs/c2/jan/perp_perp_maker_taker_v1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/fill_bucket_stats.json
golden | GOLDEN | f2e9383218078c5ef5658e06f070440c90649d21cdf5d05e690bee4bfda15a63 | baselines/runs/c2/jan/perp_perp_maker_taker_v1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/fill_events.jsonl
golden | GOLDEN | 4e1201ff0deb269ab451390de86c6c4514414920259e7089c82d56bab0f44206 | baselines/runs/c2/jan/perp_perp_maker_taker_v1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/ledger_stats.json
golden | GOLDEN | 86cbddfabf0722afdbc382dc3ae5193f77476f3782e1f3e26177c7d7aea0bc7b | baselines/runs/c2/jan/perp_perp_maker_taker_v1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/maker_observations.jsonl
golden | GOLDEN | 30b3fb05646fe47f20f81e40856fd2711305aea766a80337fed12eb2d32856c7 | baselines/runs/c2/jan/perp_perp_maker_taker_v1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/maker_observations_summary.json
golden | GOLDEN | 73719d67e0765f5d69a55829ab27fb6043b26ebb48a498fadbf00ef278a9910c | baselines/runs/c2/jan/perp_perp_maker_taker_v1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/model_settings.json
golden | GOLDEN_NOCHECK | d5c6f45ea4423bc9978b4af204a3d3b27f6ae1edf773ee6994bfac24ef405b9a | baselines/runs/c2/jan/perp_perp_maker_taker_v1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/summary.json
golden | GOLDEN | 076baab8d2aed986e655748bad9015111356fc49a422bb3de705619967c5b70c | baselines/runs/c2/jan/perp_perp_maker_taker_v1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/trades.jsonl
# perp_perp_maker_taker_v1_1 2022-01-13
golden | GOLDEN | d264fcc7dddc07b5b7846931624d6868071688bf97290e19389612c4d525e159 | baselines/runs/c2/jan/perp_perp_maker_taker_v1_1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/cross_log.json
golden | GOLDEN | 7291956e62f51b9eb2375bb0ef27bdc96f9b858ed87e3ed121704c63eef8f99c | baselines/runs/c2/jan/perp_perp_maker_taker_v1_1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/execution_reconciliation.json
golden | GOLDEN | 28c097896c95d9aa0310ceb4a763ace455af57f64c889639b92d2c95f3498a7c | baselines/runs/c2/jan/perp_perp_maker_taker_v1_1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/execution_report.json
golden | GOLDEN | 9f9edc90a7d2cb64c02b899ecc49a8b1e5af510b10350a9244d3822c53cf085c | baselines/runs/c2/jan/perp_perp_maker_taker_v1_1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/fill_bucket_stats.json
golden | GOLDEN | 649b911dece69b5e4935df7da72830f24b6fdb8913f10e822f2e413552579021 | baselines/runs/c2/jan/perp_perp_maker_taker_v1_1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/fill_events.jsonl
golden | GOLDEN | 3c5aef21be04c6a8824a00e3560da2064bc07bfce8f53346db26d283c864a9b1 | baselines/runs/c2/jan/perp_perp_maker_taker_v1_1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/maker_observations.jsonl
golden | GOLDEN | e33a0402a508c03cbd1f07eb589e463a3bd2e838ddada8f28ad0fea5bce4a131 | baselines/runs/c2/jan/perp_perp_maker_taker_v1_1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/maker_observations_summary.json
golden | GOLDEN | 0f8ea1b7815e4f0176601b442c3257947ca65abcc48ecaf92cf8969751a47e4b | baselines/runs/c2/jan/perp_perp_maker_taker_v1_1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/model_settings.json
golden | GOLDEN_NOCHECK | e74e0c25ee28291778000a5afa83f76960f70b57a0f3f1846591c23e6857f1dc | baselines/runs/c2/jan/perp_perp_maker_taker_v1_1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/summary.json
golden | GOLDEN | 6a1643739aada931cc1a1f5b1335ddbc58ea06ef3a704577da1a39ae79b53553 | baselines/runs/c2/jan/perp_perp_maker_taker_v1_1/singapore_bybit_perp_mm_clean_s1_base/2022-01-13/trades.jsonl
<!-- PINS:END -->
