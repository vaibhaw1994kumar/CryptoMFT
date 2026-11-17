# discarded/v1.1: files retired after v1.0

Nothing here is imported or run by the working copy. Files are moved, never deleted.

| From | To | Why |
|---|---|---|
| `crypto_MFT_arb_platform/src/new_backtest/selftest.py` | `discarded/v1.1/src/new_backtest/selftest.py` | W2a hygiene, owner decision 14 (17 Nov 2026): it asserts a PROFIT (`colocated_both` on a manufactured second venue with 25 bps windows), and a test never asserts a profit. Nothing in `src/new_backtest`, `tests/` or `scripts/` referenced it (grep, 17 Nov), so it was not being run. Its mechanics checks (no fill outside a window, P&L reconciliation, every signal accounted for) are covered by `tests/test_session_replay.py`, `tests/test_core_hedge_scenarios.py` and the reconciliation tests on real or clearly synthetic streams. Pre-move copy: `crypto_MFT_arb_platform/_backups/W2a_20261117_110416/selftest.py.bak` |
