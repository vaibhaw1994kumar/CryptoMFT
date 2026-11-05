#!/bin/bash
# after the b1/b2 pool: one replay alone (nothing else running) for a clean wall time, label b3; then the history check
B=/Users/vaibhav/Documents/Newproject/out/integration/baselines
until [ -f $B/logs/after_b1.done ]; do sleep 20; done
BASELINE_ONLY=mar/perp_perp_maker_taker_v1_1 python3.12 -I $B/run_baselines.py b3 1 > $B/logs/orchestrator_b3.log 2>&1
python3.12 -I - <<'PY' > $B/logs/solo_timing.log 2>&1
import json
B="/Users/vaibhav/Documents/Newproject/out/integration/baselines"
s=json.load(open(f"{B}/runs/b3/mar/base/perp_perp_maker_taker_v1_1/2024-03-08/summary.json"))
json.dump({"run":"v1.1, 2024-03-08 00:00-02:00 UTC slice, alone, 8-core 8 GB Mac","wall_s_total":round(s["wall_s"]),"wall_s_engine":round(s["wall_seconds_engine"]),"events":s.get("events_processed")}, open(f"{B}/solo_timing.json","w"))
PY
echo finished > $B/logs/after_b2.done
