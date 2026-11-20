#!/bin/bash
# Phase 2 G4 (owner 20 Nov): v1_17 with every Phase 1 and Phase 2 switch off, fresh on the full 29 Oct day, against the v1_15 run of Phase 1 (out/integration/phase1/g4_v115, made with the Phase 1 code):
# equal = the Phase 2 code with its switches off reproduces v1_15.
cd /Users/vaibhav/Documents/Newproject
W=/Users/vaibhav/Documents/Newproject/out/integration/phase2
RB=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/regime_boundaries_day1_2026-10-29_insample.json
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_17 $W/g4_v117_off --size 1 --hedge-min-notional 5 --regime-boundaries $RB \
  --set hedge_faults.dust_max_notional_usdt=null --set hedge_faults.dust_max_age_s=null --set hedge_faults.late_fill_after_close=refuse --set hedge_faults.dust_bound_rule=latch \
  --set oms.session_limits.account_tier_check=null --set oms.session_limits.clock_offset.max_offset_ms=null --set oms.session_limits.clock_offset.source=null \
  --set oms.session_limits.clock_offset.monotonic_clock=null --set oms.session_limits.margin_warning.stressed_move_bps=null > $W/g4_v117_off.log 2>&1
PYTHONPATH=crypto_MFT_arb_platform/src python3.12 -I -c "
import json,sys; sys.path.insert(0,'crypto_MFT_arb_platform/src')
from new_backtest.session_replay import compare_sessions
r=compare_sessions('out/integration/phase1/g4_v115','$W/g4_v117_off'); json.dump(r,open('$W/g4_v117_vs_v115.json','w'),indent=1)
print('G4', 'equal' if r['equal'] else 'DIFFERENT', len(r['files']), r['different'])" > $W/g4.txt 2>&1
echo done > $W/g4.done
