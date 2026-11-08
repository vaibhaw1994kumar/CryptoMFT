#!/bin/bash
# Identity check after a Phase D step (tag d1: arb_strategy, venue_state, lambda_regime moved to the core; tag d2: on_trade hook in router, OMS and session): March 2024-03-11 (2 h), legacy_v0, v1, v1.1 against b1; v1_2 (cell ft) and v1_4 (cell g1l) against the Phase C review cells (ledger_c2).
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
P=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
L=/Users/vaibhav/Documents/Newproject/out/integration/phase_b
TAG=${1:-d1}
BASELINE_ONLY='re:^mar/.*2024-03-11' python3.12 -I $B/run_baselines.py ${TAG}_m 3 > $B/logs/orchestrator_${TAG}_m.log 2>&1 &
LEDGER_OUT=$L/ledger_$TAG LEDGER_DAYS=2024-03-11 python3.12 -I $L/run_ledger_cells.py 2 mar ft g1l > $B/logs/orchestrator_${TAG}_ledger.log 2>&1 &
wait
python3.12 -I $P/scripts/compare_baselines.py compare b1 ${TAG}_m subset > $B/logs/compare_b1_${TAG}_m.log 2>&1
for c in ft g1l; do
  for f in trades.jsonl fill_events.jsonl fill_bucket_stats.json model_settings.json; do
    a=$(find $L/ledger_c2/runs/$c -name $f | head -1); b=$(find $L/ledger_$TAG/runs/$c -name $f | head -1)
    if [ -n "$a" ] && [ -n "$b" ] && cmp -s "$a" "$b"; then echo "$c $f IDENTICAL"; else echo "$c $f DIFFERENT_OR_MISSING ($a | $b)"; fi
  done
done > $B/logs/compare_ledger_c2_${TAG}.log 2>&1
date +%T > $B/logs/after_${TAG}.done
