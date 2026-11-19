#!/bin/bash
# Phase 1 (owner 20 Nov): G3 v1_9 identity (1 SOL, --gross 5) after one item, against w1d/identity_1sol_v19. Usage: g3.sh ITEM
cd /Users/vaibhav/Documents/Newproject
W=/Users/vaibhav/Documents/Newproject/out/integration/phase1
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 $W/g3_$1 --size 1 --gross 5 > $W/g3_$1.log 2>&1
PYTHONPATH=crypto_MFT_arb_platform/src python3.12 -I -c "
import json,sys; sys.path.insert(0,'crypto_MFT_arb_platform/src')
from new_backtest.session_replay import compare_sessions
r=compare_sessions('out/integration/w1d/identity_1sol_v19','$W/g3_$1'); json.dump(r,open('$W/g3_$1.json','w'),indent=1)
print('G3', '$1', 'equal' if r['equal'] else 'DIFFERENT', len(r['files']), r['different'])" >> $W/g3_$1.log 2>&1
echo done > $W/g3_$1.done
