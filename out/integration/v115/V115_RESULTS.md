# v1_15: a post-only amend to a crossing price ends the order (owner 18 Nov, go-live stage 1, item 2)

Profile `perp_perp_maker_taker_v1_15` = v1_14 + `maker.postonly_amend_cross: cancel_order` (sha256 6b7dd81cbfb028033f1f09f90f35076b4ba4512f90e57ccc970f1e4b28bed314). Strategy unchanged. v1_10 to v1_14 unchanged. EXPLORATORY: mechanics only, 29 Oct 2026 capture (REJECTED / CRITICAL waiver), 1 SOL clip, singapore latency, hedge minimum 5 USDT, mark = mid, no funding.

## Gates
- G3 v1_9 identity (1 SOL, `--gross 5`) against `w1d/identity_1sol_v19`: 14 of 14 equal (`g3.json`).
- G4 v1_15 against v1_14 (full day, `w7/v114`): 17 files; different only `session_manifest.json` (profile name and hash) and `maker_observations.jsonl`, where the only differing field is the fill model's `model_settings_sha256` (the rule is on); every row equal without it (`g4_v115_vs_v114.json`).
- G1 full suite after v1_15 and the GATES.md pins: UNCHANGED at 93 (3,946 passed, 92 failed + 1 error, 4 skipped, 11 xfailed), `compare_baselines_v115.txt`.
- G0 after pinning v1_11 .. v1_15: PASS (30 pass, 0 fail; v1_10 the one NOT_FROZEN INFO); checker selftest 15/15.
- Not done: item 3 (hypothesis download). The build session did not download it: the user rejected the question in this session's chat, and an approval relayed by another session is not taken as the user's yes for a download.

## Dense 2i case (2-hour window A, injected 2 bp hedge-book moves, pair-close on: labelled case settings) and full-day rehearsal (pair-close on), v1_14 vs v1_15

| Run | Amends requested | Confirmed | Rejected (reasons) | Timed out | Ended by crossing | Booked twice | Lineage overfill | Owed queries answered / owed | Two live orders | Fills |
|---|---|---|---|---|---|---|---|---|---|---|
| dense v1_14 | 8 | 3 | 5 (target not open 5) | 0 | 0 | 0 | 0 | 0 / 0 | 0 | 483 |
| dense v1_15 | 8 | 3 | 5 (target not open 5) | 0 | 0 | 0 | 0 | 0 / 0 | 0 | 483 |
| rehearsal v1_14 | 3 | 2 | 1 (target not open 1) | 0 | 0 | 0 | 0 | 0 / 0 | 0 | 37 |
| rehearsal v1_15 | 3 | 2 | 1 (target not open 1) | 0 | 0 | 0 | 0 | 0 / 0 | 0 | 37 |

Read with care:
- On both runs no amend reached the venue at a crossing price, so the new rule never fired: v1_15 equals v1_14 in every order, fill and position (both pairs: only the manifest and the settings hash differ). The rule is covered by tests (adapter and session level), not by these replays.
- The rehearsal on v1_14/v1_15 has 3 amends; the 17 Nov rehearsal on v1_10 (pair-close) had 20. v1_11 and later block new quotes on feed DEGRADED and withdraw on hedge-feed DEGRADED (owner decisions of 16 Nov), so fewer quotes rest and fewer are repriced. Not traced further.
- Maker filled vs hedge filled: dense 136.72 vs 136.24, rehearsal 6.0 vs 5.99; the audit counts HEDGE orders only (unwinds close the rest) and sub-lot dust carries.
