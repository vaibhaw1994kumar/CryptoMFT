#!/bin/bash
# Order 1 replays (owner 16 Nov), one heavy job at a time. EXPLORATORY.
cd /Users/vaibhav/Documents/Newproject
D=out/integration/dust
# (1) the 1 SOL identity replay on v1_9 (switch off): must equal out/integration/cost/identityM_1sol in 13 of 14 files
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 $D/identity_1sol --size 1 --gross 5 > $D/identity_1sol.log 2>&1; echo done > $D/identity_1sol.done
# (2) the size-25 sweep day on v1_9 with dust carry ON as a labelled run override and the hedge venue's real 5 USDT minimum
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 $D/size25_dust_on --size 25 --gross 125 --set hedge_faults.dust_carry=next_hedge --hedge-min-notional 5 > $D/size25_dust_on.log 2>&1; echo done > $D/size25_dust_on.done
# (3) the same day, same minimum, dust carry OFF (the refusal path as it is today): the comparison
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_9 $D/size25_dust_off --size 25 --gross 125 --hedge-min-notional 5 > $D/size25_dust_off.log 2>&1; echo done > $D/size25_dust_off.done
