#!/bin/bash
# Owner order 20 Nov (N1, N2, item 9, RPI, depth counter): gates with fresh numbers, launched from COMMITTED code only.
#   G3: v1_9 identity (1 SOL, --gross 5) against w1d/identity_1sol_v19.
#   dense arb 2i case (scenarios.py 2i_v110_dense2) BEFORE this order (snapshot of core 776c2a9 + outer a415bbd) and AFTER (current committed tree): amend counts compared.
#   pair-close day (v1_16 with pair close and the reservation on, Phase 1's f05_close_on arguments): amend counts against Phase 1's run.
cd /Users/vaibhav/Documents/Newproject
N=/Users/vaibhav/Documents/Newproject/out/integration/n_order
RB=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/regime_boundaries_day1_2026-10-29_insample.json
until [ -f out/integration/phase2/f02/ladder/i_20k.done ] && [ -f out/integration/phase2/f02/ladder/i_30k.done ] && [ -f out/integration/phase2/f02/ladder/i_50k.done ]; do sleep 30; done
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 $N/g3 --size 1 --gross 5 > $N/g3.log 2>&1 &
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_16 $N/pair_close_v116 --size 1 --hedge-min-notional 5 --set arb.close_pairs=reduce_only_quote --set arb.close_reservation=true --set arb.close_unfilled_after_s=60 --regime-boundaries $RB --s0-audit > $N/pair_close_v116.log 2>&1 &
# keep the 17 Nov outputs: moved aside, never overwritten
[ -d out/integration/day1/runs/scn_2i_v110_dense2 ] && mv out/integration/day1/runs/scn_2i_v110_dense2 out/integration/day1/runs/scn_2i_v110_dense2_ref17nov
[ -f out/integration/ems_cases/scn_2i_v110_dense2.jsonl ] && mv out/integration/ems_cases/scn_2i_v110_dense2.jsonl out/integration/ems_cases/scn_2i_v110_dense2_ref17nov.jsonl
( cd out/integration/day1 && python3.12 -I scenarios.py 2i_v110_dense2 --src $N/snap_before/src > $N/dense2_before.log 2>&1; mv runs/scn_2i_v110_dense2 $N/dense2_before;
  python3.12 -I scenarios.py 2i_v110_dense2 > $N/dense2_after.log 2>&1; mv runs/scn_2i_v110_dense2 $N/dense2_after ) &
wait
PYTHONPATH=crypto_MFT_arb_platform/src python3.12 -I -c "
import json,sys; sys.path.insert(0,'crypto_MFT_arb_platform/src')
from new_backtest.session_replay import compare_sessions
r=compare_sessions('out/integration/w1d/identity_1sol_v19','$N/g3'); json.dump(r,open('$N/g3.json','w'),indent=1)
print('G3', 'equal' if r['equal'] else 'DIFFERENT', len(r['files']), r['different'])
r=compare_sessions('$N/dense2_before','$N/dense2_after'); json.dump(r,open('$N/dense2_compare.json','w'),indent=1)
print('dense2', 'equal' if r['equal'] else 'DIFFERENT', len(r['files']), r['different'])
r=compare_sessions('out/integration/phase1/f05_close_on','$N/pair_close_v116'); json.dump(r,open('$N/pair_close_compare.json','w'),indent=1)
print('pair_close', 'equal' if r['equal'] else 'DIFFERENT', len(r['files']), r['different'])" > $N/gates.txt 2>&1
echo done > $N/gates.done
