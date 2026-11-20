#!/bin/bash
# Phase 2 F06 (owner 20 Nov) item 4: the pair-close day of Phase 1 (out/integration/phase1/f05_close_on.sh, v1_16) rerun on v1_17 (dust_bound_rule aggregate), same overrides.
# EXPLORATORY: the 29 Oct capture is REJECTED / CRITICAL (waiver: mechanics only); no P&L is claimed. Reported: signals blocked by the dust rule, dust alarms, the largest aggregate dust (USDT), the end balance.
cd /Users/vaibhav/Documents/Newproject
W=/Users/vaibhav/Documents/Newproject/out/integration/phase2
RB=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/regime_boundaries_day1_2026-10-29_insample.json
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_17 $W/f06_close_on_v117b --size 1 --hedge-min-notional 5 --set arb.close_pairs=reduce_only_quote --set arb.close_reservation=true --set arb.close_unfilled_after_s=60 --regime-boundaries $RB --s0-audit > $W/f06_close_on_v117b.log 2>&1
echo done > $W/f06_close_on_v117b.done
