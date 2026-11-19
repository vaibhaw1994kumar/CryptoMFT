#!/bin/bash
# Phase 1 (owner 20 Nov): the end-of-phase gates, run side by side. G1: the full suite split into 4 shards by file size (the same tests and the same check against the 93 baseline, concatenated logs);
# G3: v1_9 identity after F04; G4: v1_16 with every Phase 1 switch off against v1_15, both fresh on the full 29 Oct day.
cd /Users/vaibhav/Documents/Newproject
W=/Users/vaibhav/Documents/Newproject/out/integration/phase1
RB=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/regime_boundaries_day1_2026-10-29_insample.json
(./out/integration/phase1/g3.sh f04) &
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_15 $W/g4_v115 --size 1 --hedge-min-notional 5 --regime-boundaries $RB > $W/g4_v115.log 2>&1 &
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_16 $W/g4_v116_off --size 1 --hedge-min-notional 5 --regime-boundaries $RB \
  --set hedge_faults.dust_max_notional_usdt=null --set hedge_faults.dust_max_age_s=null --set hedge_faults.late_fill_after_close=refuse --set oms.session_limits.account_tier_check=null > $W/g4_v116_off.log 2>&1 &
cd crypto_MFT_arb_platform
SHARDS=$(python3.12 - <<'PY'
import os
files = sorted([f"tests/{f}" for f in os.listdir("tests") if f.startswith("test_") and f.endswith(".py")] + ["tests/platform_kit"], key=lambda f: -(os.path.getsize(f) if os.path.isfile(f) else 400000))
shards, sizes = [[] for _ in range(4)], [0] * 4
for f in files:
    i = sizes.index(min(sizes)); shards[i].append(f); sizes[i] += os.path.getsize(f) if os.path.isfile(f) else 400000
print("|".join(" ".join(s) for s in shards))
PY
)
IFS='|' read -r S1 S2 S3 S4 <<< "$SHARDS"
i=0
for S in "$S1" "$S2" "$S3" "$S4"; do
  i=$((i+1))
  PYTHONPATH=src python3.12 -m pytest $S -q -p no:cacheprovider -rfE --continue-on-collection-errors > $W/suite_shard$i.log 2>&1 &
done
wait
cat $W/suite_shard*.log > $W/suite_all.log
python3.12 scripts/compare_baselines.py tests $W/suite_all.log > $W/compare_baselines_suite.txt 2>&1
cd /Users/vaibhav/Documents/Newproject
PYTHONPATH=crypto_MFT_arb_platform/src python3.12 -I -c "
import json,sys; sys.path.insert(0,'crypto_MFT_arb_platform/src')
from new_backtest.session_replay import compare_sessions
r=compare_sessions('$W/g4_v115','$W/g4_v116_off'); json.dump(r,open('$W/g4_v116_vs_v115.json','w'),indent=1)
print('G4', 'equal' if r['equal'] else 'DIFFERENT', len(r['files']), r['different'])" > $W/g4.txt 2>&1
echo done > $W/end_of_phase.done
