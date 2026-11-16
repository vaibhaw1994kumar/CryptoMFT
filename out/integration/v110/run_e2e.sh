#!/bin/bash
# Order 2: the full 29 Oct end-to-end rehearsal on the current code, v1_9 (reference) next to v1_10, plus a labelled v1_10 run at 5.0 SOL gross per venue. EXPLORATORY. One heavy job at a time; waits for the case runs.
cd /Users/vaibhav/Documents/Newproject
while [ ! -f out/integration/day1/runs_v110_all.done ]; do sleep 20; done
D=out/integration/v110
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 $D/e2e_v19 --size 1 --hedge-min-notional 5 > $D/e2e_v19.log 2>&1; echo done > $D/e2e_v19.done
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_10 $D/e2e_v110 --size 1 --hedge-min-notional 5 > $D/e2e_v110.log 2>&1; echo done > $D/e2e_v110.done
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_10 $D/e2e_v110_gross5 --size 1 --gross 5 --hedge-min-notional 5 > $D/e2e_v110_gross5.log 2>&1; echo done > $D/e2e_v110_gross5.done
echo all > $D/e2e_all.done
