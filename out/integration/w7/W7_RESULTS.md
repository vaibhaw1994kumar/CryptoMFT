# W7 7.D only: the pre-trade estimate per order (built 17 Nov 2026)

Design: `out/integration/designs/W7_W5_W10_design.md` section 7.D, only that item. Profile `perp_perp_maker_taker_v1_14` = v1_13 + `cost_records.estimate_at_decision: true` (sha256 a793b756ef014d45d7ba6b58f61c34aad7c2bbd805b61708932a825e32eb54d0). Every other `cost_records` key is refused at load.

## What is written
- Each maker-taker signal carries `estimate`, read from the variables the gate used: gross / naive / conditional / net edge and reference price, maker and taker fee, expected hedge move and slippage, funding/holding, failure cost (p_fail x loss_fail_bps, ASSUMED placeholders), safety buffer, hedge walk and its basis, maker half spread, total cost with its terms. Impact, delay, fill probability and opportunity cost are `{"missing": reason}`.
- The signal row (`session_signals.jsonl`) and the primary maker intent record (`session_intent_journal.jsonl`) gain the key. `opportunity_id` is inside the estimate (the layer's `signal_id` is its own serial). Hedge intents join through `parent_id`.
- NOT built: the daily-partitioned 30-day store and its query helper (a paper session already writes the key through the per-record intent journal).

## Tests
- `tests/test_execution_layer.py` PreTradeEstimateRecordShape: no key without an estimate.
- `tests/test_session_replay.py` PreTradeEstimatePerOrder (4): off has no key; on, every signal and primary maker intent has it, its net edge equals the signal's, conditional = gross - total cost, missing fields are labelled, every maker fill joins to its estimate; the switch changes no order, fill, position, signal or journal row apart from the key; other `cost_records` keys are refused.

## Gates (29 Oct 2026 day, 1 SOL)
- G3 v1_9 identity with the W7 code: 14 of 14 equal (`g3.json`).
- G4 v1_14 against v1_13 (`g4_v114_vs_v113.json`): different only `session_signals.jsonl`, `session_intent_journal.jsonl` (by the new key: equal once it is removed; 39 of 39 signals and 39 primary intents carry it) and `session_manifest.json` (profile name and hash).

## Full suite after W6 and W7 (once, against the 93 baseline)
- First run: 94, one new failure: `test_funding_term.py::Routing::test_the_trading_session_routes_derivative_ticks_to_a_strategy_with_the_hook_only`. The cause was a W6 slip: `TradingSession.on_derivative` read `self.dislocation` directly, and the test calls it on a SimpleNamespace. Fixed in core c0d6885 (getattr, no behaviour change in a session).
- Rerun: UNCHANGED at 93 (3,941 passed, 92 failed + 1 error, 4 skipped, 11 xfailed). `compare_baselines_w6w7_rerun.txt`.
