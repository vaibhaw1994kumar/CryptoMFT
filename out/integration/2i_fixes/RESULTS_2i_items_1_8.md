# 2i core fixes, items 1-8: results (build session, 17 Nov 2026)

What each item changed is in `docs/strategy_contract.md`, section "Amend at the venue, exits and quote callbacks: the 2i fixes". Every item has a test that fails on the code before the fix (checked on a copy of the pre-fix code; item 6 is a speed change and its test is an equality test against the full rescan).

Gates
- v1_9 1 SOL identity, full 29 Oct day, against the W2b gate run: 13 of 14 files byte-identical; `maker_observations.jsonl` differs ONLY in `model_settings_sha256`, now d8a79567... (item 8: the value before core 1509601) instead of e900d03a...; with that value swapped the file is equal. File: `identity_compare_W2b_vs_2i.json`. 4,356 input files, 0 recorder-hash mismatches.
- Full suite: 94 failing against 93 in the baseline; the one new failure was `test_queue_v2_ledger.py::DepletionLedgerSwitch::...settings_hash_of_every_earlier_profile_is_unchanged`, whose "earlier profile" hash still included `exclude_rpi_trades`; item 8 is the order to leave it out, the test now does, and its file passes (70 passed with test_queue_adapter). The suite was not rerun in full after that one-line test change (owner asked to accelerate).

Not done here
- Item 6 timing: the 1 July ADA day is not in this working copy (NOT RUN). On the 29 Oct arb day the two scans touch few plans, so no difference is expected; the before/after pair at equal concurrency was stopped when the owner asked to accelerate.
- Item 9 (MARGIN_STATE_TRANSITION 4 vs 3 in the MM g1s golden): not started; the MM golden is not in this working copy or in the MM worktree's outputs that I could find.
- Item 10 (dense arb 2i on v1_10): not started.
- Replays of v1_9s, v1_10 and later that amend change with items 1-3 and need a rerun (case 2i, the pair-close day, the rehearsal rows with an amend).
