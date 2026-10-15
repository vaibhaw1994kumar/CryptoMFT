# Maker execution-realism evidence (20261015T172614Z)

Each decision covers only the scope stated; the full session audit status
is reported separately and is not implied by a PASS here.

| Check | Scope | Decision | Full audit overall | Folder |
|---|---|---|---|---|
| Recorded inputs present and checksum-exact | recorded input files (presence and checksums) | INPUT_MISSING | n/a | `0_data_integrity/` |
| C++ vs Python engine parity | C++ vs Python engine parity | PASS | n/a | `1_engine_parity/` |
| C++ queue semantics smoke test | C++ queue semantics smoke test | PASS | n/a | `2_cpp_semantics/` |
| Live paper vs replay parity | Live paper vs replay parity | PASS | n/a | `3_live_replay_parity/` |
| Replay ordering + duplicate event ids (all cycles) | Replay ordering + duplicate event ids (all cycles) | PASS | n/a | `4_5_replay_order_and_ids/` |
| Session audit analyze-session (cycle-0006) | maker simulation component of analyze-session | PASS | INPUT_MISSING | `6_session_audit/` |
| CLI simulate-maker (cycle-0001) | CLI simulate-maker (cycle-0001) | PASS | n/a | `7_cli_simulate_maker/` |
| Sensitivity sweep sanity | Sensitivity sweep sanity | PASS | n/a | `8_sensitivity_sweep/` |

92 files; SHA-256 of each in `evidence_summary.json`; large intermediates listed in `6_session_audit/pruned_large_files.json`.
