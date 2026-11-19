#!/bin/bash
# Phase 1 F05 (owner 20 Nov): one LABELLED 29 Oct replay of v1_16 with pair close and the per-pair reservation ON through --set (the profile keeps both off), CLOSE_UNFILLED at 60 s (ASSUMED).
# EXPLORATORY: the 29 Oct capture is REJECTED / CRITICAL (waiver: mechanics only); no P&L is claimed. Reported: CLOSE_PAIR_BEYOND_FLAT, the reservation counts, reduce-only by position.
cd /Users/vaibhav/Documents/Newproject
W=/Users/vaibhav/Documents/Newproject/out/integration/phase1
RB=/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/regime_boundaries_day1_2026-10-29_insample.json
python3.12 -I out/integration/day1/run_day.py perp_perp_maker_taker_v1_16 $W/f05_close_on --size 1 --hedge-min-notional 5 --set arb.close_pairs=reduce_only_quote --set arb.close_reservation=true --set arb.close_unfilled_after_s=60 --regime-boundaries $RB --s0-audit > $W/f05_close_on.log 2>&1
echo done > $W/f05_close_on.done
