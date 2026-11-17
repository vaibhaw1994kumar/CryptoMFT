"""EMS acceptance scenario replays on 2-hour windows of the 2026-10-29 capture (owner 12 Nov, v1_9_TOLERANCES.md part 2, section K). A run wrapper: the cases differ only by settings the core already has (profile,
window, timing, injected faults, a lowered rate limit, an injected feed gap, an injected duplicate "Filled"); nothing here decides anything. Each case writes the standard replay outputs to runs/scn_<case>/ and its
timestamped stage trace to out/integration/ems_cases/scn_<case>.jsonl. READ ONLY on data/.
usage: scenarios.py CASE [--src DIR]        CASE is one of CASES (see below)"""
import dataclasses
from decimal import Decimal
import itertools
import json
import sys
import time
from pathlib import Path

argv = sys.argv[1:]
src = "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src"
if "--src" in argv:
    i = argv.index("--src"); src = argv[i + 1]; del argv[i:i + 2]
sys.path.insert(0, src)
from crypto_stat_arb.metrics import stage_trace
from crypto_stat_arb.models import MarketEvent
from crypto_stat_arb.orderbook import OrderBook
from new_backtest.profiles import apply_profile, load_profile
from new_backtest.session_replay import SessionReplay, dated_ticks, iter_recorded_events, safety_event_text, write_result, write_run_record, write_stage_trace

SESSION = Path("/Users/vaibhav/Documents/Newproject/data/live_sessions/solusdt/2026-10-29/SOLUSDT/e9b81728a3054812a48035a1b3e02d42")
DAY0 = 1793232000                      # 2026-10-29 00:00:00 UTC
BND = "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/regime_boundaries_day1_2026-10-29_insample.json"
A, B = ("11:00", "13:00"), ("19:00", "21:00")      # the windows that hold the capture natural maker fills (11:15 and 12:30 UTC; 19:59 UTC)
DENSE2_ARB = {"min_net_edge_bps": -2.0, "participation_max": 0.0, "quote_withdraw_hysteresis_bps": 2.5, "close_pairs": "reduce_only_quote"}      # item 10: the 2i dense entry, AND the pair-close switch (Order 3 B) ON: an open pair keeps a closing quote resting, so quotes rest all day and the injected move meets them (without it the 5 SOL inventory fills in minutes and nothing rests to amend). Labelled arb_overrides
DENSE2_SET = {}                                                                                            # placement improve was tried: the arb prices its quote by the edge rule, placement does not move it (probes 2i_v110_dense2_probe, _probe4, _probe6, _probe8: 1, 1, 0, 1 amends in 10 min)

# case -> settings. profile v1_9 (size_aware) unless the case needs another; every case uses 1 SOL unless it says size.
DENSE = {"min_net_edge_bps": -2.0, "participation_max": 0.0}          # labelled: the dense entry of the 2i cases, so that the capture has quotes for a limit to act on
CASES = {
    "H1a": dict(profile="perp_perp_maker_taker_v1_9", window=A),                                   # determinism: the same case twice
    "H1b": dict(profile="perp_perp_maker_taker_v1_9", window=A),
    "2a_quote_withdraw": dict(profile="perp_perp_maker_taker_v1_8", window=A),                     # quote_withdraw on
    "2b_cancel_latency": dict(profile="perp_perp_maker_taker_v1_8", window=A, cancel_ms={"bybit": 800.0}),   # a longer cancel latency: fills during cancel pending
    "2d_ghost3": dict(profile="perp_perp_maker_taker_v1_9", window=A, ghost_s=3.0),
    "2d_ghost30": dict(profile="perp_perp_maker_taker_v1_9", window=A, ghost_s=30.0),
    "2e_feed_gap": dict(profile="perp_perp_maker_taker_v1_9", window=B, gap=("20:20:00", "20:20:30")),   # an injected 30 s outage of both feeds inside 20:00-21:00
    "2f_rate_limit": dict(profile="perp_perp_maker_taker_v1_9", window=A, rate_limit=True),
    "2j_duplicate_filled": dict(profile="perp_perp_maker_taker_v1_9", window=A, duplicate_filled=True),
    "2k_breaker_pause": dict(profile="perp_perp_maker_taker_v1_9", window=A, breaker=True, gap=("11:40:00", "11:40:13")),              # flat and both gateways silent 13 s (heartbeat limit 10 s): PAUSE, resumes by itself
    "2k_breaker_trip": dict(profile="perp_perp_maker_taker_v1_9", window=A, breaker=True, ghost_s=30.0, reset_after_s=60.0),          # exposure + a hedge unknown for 30 s: TRIP, quotes cancelled, flatten, then a scripted manual reset 60 s after the trip
    "2l_maker_outage": dict(profile="perp_perp_maker_taker_v1_9", window=A, maker_outage=[("11:15:28.5", "11:15:48.5"), ("12:30:05", "12:30:30")]),   # Bybit order entry and private stream down: a quote resting at the first outage fills inside it; a quote due inside the second never reaches the venue
    "2c_zero_then_fill": dict(profile="perp_perp_maker_taker_v1_9", window=A, hedge_retries=2, thin=[(0, 0.0)]),                      # the first hedge IOC of every plan finds nothing; the retry sees the real book
    "2c_partial_then_fill": dict(profile="perp_perp_maker_taker_v1_9", window=A, hedge_retries=2, thin=[(2, 0.4)]),                  # the first hedge IOC finds 2 levels of 0.4 SOL: a partial of 0.8 of 1.0; only the true residual 0.2 is retried
    "2c_zero_exhausted": dict(profile="perp_perp_maker_taker_v1_9", window=A, hedge_retries=2, thin=[(0, 0.0)] * 3),                # every attempt finds nothing: retries run out, unwind, alarm
    "2c_thin_top": dict(profile="perp_perp_maker_taker_v1_9", window=A, hedge_retries=2, thin=[(-1, 0.05)] * 3, thin_view_top=0.05),     # owner 13 Nov: the Binance TOUCH shows 0.05 SOL (to the bot and at the venue), every deeper level is the capture's: the hedge is larger than the touch, so the size_aware cap spans levels
    "2m_processing": dict(profile="perp_perp_maker_taker_v1_9", window=A, processing_ms={"strategy": 5.0, "risk": 5.0}),
    "2m_p99": dict(profile="perp_perp_maker_taker_v1_9", window=A, latency_q=0.99),                 # every order delay fixed at the testnet p99 of its stage, both venues (feed unchanged)
    "2m_p999": dict(profile="perp_perp_maker_taker_v1_9", window=A, latency_q=0.999),               # the same at the p99.9 (of n about 390: effectively the sample maximum)
    # S0 repricing in the session runtime (owner 14 Nov, case 2i on the arb): the control is v1_9 (S0 off), the others v1_9s (S0 on, ASSUMED values). Injected faults on the AMEND request: every Nth is rejected by the
    # venue / never answered (timeout) / lost in a Bybit outage (disconnect); duplicate_filled doubles every Filled report. arb_overrides: settings the case changes on the profile, labelled in the result.
    "2i_control_v19": dict(profile="perp_perp_maker_taker_v1_9", window=A),
    "2i_s0": dict(profile="perp_perp_maker_taker_v1_9s", window=A),
    "2i_s0_reject": dict(profile="perp_perp_maker_taker_v1_9s", window=A, amend_reject_every=3),
    "2i_s0_timeout": dict(profile="perp_perp_maker_taker_v1_9s", window=A, amend_timeout_every=3),
    "2i_s0_disconnect": dict(profile="perp_perp_maker_taker_v1_9s", window=A, maker_outage=[("11:20:00", "11:21:00"), ("12:00:00", "12:01:00")]),
    "2i_control_v19_B": dict(profile="perp_perp_maker_taker_v1_9", window=B),
    "2i_s0_B": dict(profile="perp_perp_maker_taker_v1_9s", window=B),
    "2i_s0_debug": dict(profile="perp_perp_maker_taker_v1_9s", window=("12:29", "12:31"), debug_requote=True),
    "2i_s0_dup_filled": dict(profile="perp_perp_maker_taker_v1_9s", window=A, duplicate_filled=True),
    "2n_size1": dict(profile="perp_perp_maker_taker_v1_9", window=A, size=1.0, gross=5.0, cap=True),
    "2n_size5": dict(profile="perp_perp_maker_taker_v1_9", window=A, size=5.0, gross=25.0, cap=True),
    "2n_size25": dict(profile="perp_perp_maker_taker_v1_9", window=A, size=25.0, gross=125.0, cap=True),
    # v1_10 (owner 16 Nov): withdrawal + S0 repricing + dust carry + 1 s no-requote ON. Case 2i on v1_10: an INJECTED hedge-venue move on the real capture (labelled INJECTED): every 20 s the Binance book is shifted
    # by 2 bps (two ticks; alternating down and up, so a resting bid and a resting ask are each hit) for 8 s, shifted books and prints, then restored by a snapshot. Beyond the 1 bp S0 band and inside the withdrawal tolerance at the lowered entry threshold, so the controller amends: the quotes enter at about -1.9 bps and a 2-tick move is 2.0 bps, which the profile's 1.5 bp validity tolerance would withdraw, so the case widens the tolerance to 2.5 bps (labelled in arb_overrides). On this capture the dense variant makes ONE amend (11:01:23 UTC, a quote already 0.9 of 1.0 filled): the reject and timeout variants fail every amend request so that one meets the fault, and the disconnect variant takes Bybit order entry and the private stream down from 11:01:23.291 to 11:01:53, over the amend (sent at 11:01:23.289). Only one real amend is met per variant: reported as such. Plus reject / timeout / disconnect /
    # duplicate Filled acting on real amends. v1_10 on the capture runs the hedge venue minimum at 5 USDT (the real Binance SOLUSDT rule) so the dust rule is live.
    "2i_v110": dict(profile="perp_perp_maker_taker_v1_10", window=A, inject_move=dict(venue="BINANCE", bps=2.0, every_s=20.0, offset_s=5.0, dur_s=8.0), hedge_min_notional=5.0),
    "2i_v110_reject": dict(profile="perp_perp_maker_taker_v1_10", window=A, inject_move=dict(venue="BINANCE", bps=2.0, every_s=20.0, offset_s=5.0, dur_s=8.0), amend_reject_every=1, hedge_min_notional=5.0, arb_overrides={"min_net_edge_bps": -2.0, "participation_max": 0.0, "quote_withdraw_hysteresis_bps": 2.5}),
    "2i_v110_timeout": dict(profile="perp_perp_maker_taker_v1_10", window=A, inject_move=dict(venue="BINANCE", bps=2.0, every_s=20.0, offset_s=5.0, dur_s=8.0), amend_timeout_every=1, hedge_min_notional=5.0, arb_overrides={"min_net_edge_bps": -2.0, "participation_max": 0.0, "quote_withdraw_hysteresis_bps": 2.5}),
    "2i_v110_disconnect": dict(profile="perp_perp_maker_taker_v1_10", window=A, inject_move=dict(venue="BINANCE", bps=2.0, every_s=20.0, offset_s=5.0, dur_s=8.0), maker_outage=[("11:01:23.291", "11:01:53")], hedge_min_notional=5.0, arb_overrides={"min_net_edge_bps": -2.0, "participation_max": 0.0, "quote_withdraw_hysteresis_bps": 2.5}),
    "2i_v110_dup_filled": dict(profile="perp_perp_maker_taker_v1_10", window=A, inject_move=dict(venue="BINANCE", bps=2.0, every_s=20.0, offset_s=5.0, dur_s=8.0), duplicate_filled=True, hedge_min_notional=5.0, arb_overrides={"min_net_edge_bps": -2.0, "participation_max": 0.0, "quote_withdraw_hysteresis_bps": 2.5}),
    # DENSE variant: the capture window has 2 signals at the profile's entry edge, too few for an injected move to meet a resting quote. The entry threshold is lowered and the participation limit lifted FOR THE CASE (labelled in the result: arb_overrides), so more quotes rest.
    "2i_v110_dense": dict(profile="perp_perp_maker_taker_v1_10", window=A, inject_move=dict(venue="BINANCE", bps=2.0, every_s=20.0, offset_s=5.0, dur_s=8.0), hedge_min_notional=5.0, arb_overrides={"min_net_edge_bps": -2.0, "participation_max": 0.0, "quote_withdraw_hysteresis_bps": 2.5}),
    # Item 10 (owner 17 Nov, after the 2i core fixes): DENSER. The quote rests one tick INSIDE the touch (placement improve, a case setting), so the injected 2 bps hedge move meets resting quotes that
    # have no slack and the controller amends many times; reject / timeout / disconnect / duplicate Filled act on those real amends. The disconnect windows cover amends by construction (amends are frequent).
    "2i_v110_dense2_probe": dict(profile="perp_perp_maker_taker_v1_10", window=("11:00", "11:10"), inject_move=dict(venue="BINANCE", bps=2.0, every_s=20.0, offset_s=5.0, dur_s=8.0), hedge_min_notional=5.0, arb_overrides=DENSE2_ARB, profile_set=DENSE2_SET),
    "2i_v110_dense2_probe4": dict(profile="perp_perp_maker_taker_v1_10", window=("11:00", "11:10"), inject_move=dict(venue="BINANCE", bps=4.0, every_s=10.0, offset_s=5.0, dur_s=5.0), hedge_min_notional=5.0,
                                  arb_overrides={**DENSE2_ARB, "quote_withdraw_hysteresis_bps": 6.0}, profile_set={"execution.primary.reprice.band_down_bps": 0.5}),
    "2i_v110_dense2_probe6": dict(profile="perp_perp_maker_taker_v1_10", window=("11:00", "11:10"), inject_move=dict(venue="BINANCE", bps=4.0, every_s=10.0, offset_s=5.0, dur_s=5.0), hedge_min_notional=5.0,
                                  arb_overrides={"min_net_edge_bps": -8.0, "participation_max": 0.0, "quote_withdraw_hysteresis_bps": 8.0}, profile_set={"execution.primary.reprice.band_down_bps": 0.5}),
    "2i_v110_dense2_probe8": dict(profile="perp_perp_maker_taker_v1_10", window=("11:00", "11:10"), inject_move=dict(venue="BINANCE", bps=8.0, every_s=10.0, offset_s=5.0, dur_s=5.0), hedge_min_notional=5.0,
                                  arb_overrides={"min_net_edge_bps": -2.0, "participation_max": 0.0, "quote_withdraw_hysteresis_bps": 12.0}),
    "2i_v110_dense2_probe_cp": dict(profile="perp_perp_maker_taker_v1_10", window=("11:00", "11:10"), inject_move=dict(venue="BINANCE", bps=2.0, every_s=20.0, offset_s=5.0, dur_s=8.0), hedge_min_notional=5.0, arb_overrides=DENSE2_ARB, profile_set=DENSE2_SET),
    "2i_v110_dense2": dict(profile="perp_perp_maker_taker_v1_10", window=A, inject_move=dict(venue="BINANCE", bps=2.0, every_s=20.0, offset_s=5.0, dur_s=8.0), hedge_min_notional=5.0, arb_overrides=DENSE2_ARB, profile_set=DENSE2_SET),
    "2i_v110_dense2_reject": dict(profile="perp_perp_maker_taker_v1_10", window=A, inject_move=dict(venue="BINANCE", bps=2.0, every_s=20.0, offset_s=5.0, dur_s=8.0), amend_reject_every=1, hedge_min_notional=5.0, arb_overrides=DENSE2_ARB, profile_set=DENSE2_SET),
    "2i_v110_dense2_timeout": dict(profile="perp_perp_maker_taker_v1_10", window=A, inject_move=dict(venue="BINANCE", bps=2.0, every_s=20.0, offset_s=5.0, dur_s=8.0), amend_timeout_every=1, hedge_min_notional=5.0, arb_overrides=DENSE2_ARB, profile_set=DENSE2_SET),
    "2i_v110_dense2_disconnect": dict(profile="perp_perp_maker_taker_v1_10", window=A, inject_move=dict(venue="BINANCE", bps=2.0, every_s=20.0, offset_s=5.0, dur_s=8.0), maker_outage=[("09:56:30", "09:56:31")], disconnect_at_amend_s=30.0, hedge_min_notional=5.0, arb_overrides=DENSE2_ARB, profile_set=DENSE2_SET),      # the static outage (1 s in the warm-up, quoting off) only switches the maker-outage model on; each amend opens its own 30 s outage
    "2i_v110_dense2_dup_filled": dict(profile="perp_perp_maker_taker_v1_10", window=A, inject_move=dict(venue="BINANCE", bps=2.0, every_s=20.0, offset_s=5.0, dur_s=8.0), duplicate_filled=True, hedge_min_notional=5.0, arb_overrides=DENSE2_ARB, profile_set=DENSE2_SET),
    "2i_v110_timeout_short": dict(profile="perp_perp_maker_taker_v1_10", window=("11:00", "11:03"), inject_move=dict(venue="BINANCE", bps=2.0, every_s=20.0, offset_s=5.0, dur_s=8.0), amend_timeout_every=1, hedge_min_notional=5.0, arb_overrides={"min_net_edge_bps": -2.0, "participation_max": 0.0, "quote_withdraw_hysteresis_bps": 2.5}),
    "2i_v110_short": dict(profile="perp_perp_maker_taker_v1_10", window=("11:00", "11:04"), inject_move=dict(venue="BINANCE", bps=2.0, every_s=20.0, offset_s=5.0, dur_s=8.0), hedge_min_notional=5.0),
    "2a_v110": dict(profile="perp_perp_maker_taker_v1_10", window=A),
    "2b_v110": dict(profile="perp_perp_maker_taker_v1_10", window=A, cancel_ms={"bybit": 800.0}),
    "2f_v110": dict(profile="perp_perp_maker_taker_v1_10", window=A, rate_limit=True),
    "2m_processing_v110": dict(profile="perp_perp_maker_taker_v1_10", window=A, processing_ms={"strategy": 5.0, "risk": 5.0}),
    "2m_p99_v110": dict(profile="perp_perp_maker_taker_v1_10", window=A, latency_q=0.99),
    "2m_p999_v110": dict(profile="perp_perp_maker_taker_v1_10", window=A, latency_q=0.999),
    "2n_size1_v110": dict(profile="perp_perp_maker_taker_v1_10", window=A, size=1.0, gross=5.0, cap=True),
    "2n_size5_v110": dict(profile="perp_perp_maker_taker_v1_10", window=A, size=5.0, gross=25.0, cap=True),
    "2n_size25_v110": dict(profile="perp_perp_maker_taker_v1_10", window=A, size=25.0, gross=125.0, cap=True),
    # ORDER 3 limits on v1_10 (owner 16 Nov). Each case INJECTS the condition (labelled in the result): nothing binds on the 29 Oct day by itself. All use the dense entry (threshold -2.0 bps, participation limit off:
    # labelled arb_overrides) so that there are quotes to limit. profile_set changes profile keys for the case only.
    "3a_order_rate_v110": dict(profile="perp_perp_maker_taker_v1_10", window=A, hedge_min_notional=5.0, arb_overrides=DENSE, profile_set={"oms.session_limits.order_rate": {"per_s": 0.2, "burst": 1}}),
    "3b_loss_stop_v110": dict(profile="perp_perp_maker_taker_v1_10", window=A, hedge_min_notional=5.0, arb_overrides=DENSE, profile_set={"oms.session_limits.daily_loss_stop_usdt_per_sol_of_clip": -0.02}),
    "3c_clock_bad_v110": dict(profile="perp_perp_maker_taker_v1_10", window=A, hedge_min_notional=5.0, arb_overrides=DENSE, clock_inject={"offset_ms": 1.0, "uncertainty_ms": 6.0}),
    "3c_clock_ok_v110": dict(profile="perp_perp_maker_taker_v1_10", window=A, hedge_min_notional=5.0, arb_overrides=DENSE, clock_inject={"offset_ms": 1.0, "uncertainty_ms": 4.0}),
    "3c_clock_none_v110": dict(profile="perp_perp_maker_taker_v1_10", window=A, hedge_min_notional=5.0, arb_overrides=DENSE),
    "3d_margin_warning_v110": dict(profile="perp_perp_maker_taker_v1_10", window=A, hedge_min_notional=5.0, arb_overrides=DENSE, profile_set={"margin.venues.bybit.initial_capital": 30.0, "margin.venues.binance.initial_capital": 30.0}),
    "3e_missing_marks_v110": dict(profile="perp_perp_maker_taker_v1_10", window=A, hedge_min_notional=5.0, arb_overrides=DENSE, gap=("11:20:00", "11:22:30"), gap_venue="BINANCE"),
    # ORDER 3 B: the pair-close switch ON (arb.close_pairs reduce_only_quote, NOT in v1_10), on the dense window of the baseline (3x): the same run with the switch on. Labelled: the dense entry is a case override.
    "3g_close_pairs_on_v110": dict(profile="perp_perp_maker_taker_v1_10", window=A, hedge_min_notional=5.0, arb_overrides={**DENSE, "close_pairs": "reduce_only_quote"}),
    # W1 feed health on v1_11 (owner 17 Nov). 4x: v1_11 as it is (observation only) on the dense window. 4a: the hedge feed (Binance) LOST for 40 s from 11:01:33 (INJECTED; a maker quote placed at 11:01:32.24 is resting then) with the two actions ON for the case (labelled
    # profile_set: both are none in v1_11 and wait for the owner). 4b: the same loss, observation only: what happens today without an action. 4c (W13-B1): an INJECTED feed delay: Binance exchange stamps moved
    # 1.5 s into the past for 60 s with feed_delayed_ms 1000 set FOR THE CASE (PENDING the owner's 1000 vs 2000): the time gate blocks decisions and the state model logs it.
    "4x_feed_health_v111": dict(profile="perp_perp_maker_taker_v1_11", window=A, hedge_min_notional=5.0, arb_overrides=DENSE),
    "4a_hedge_feed_lost_actions_v111": dict(profile="perp_perp_maker_taker_v1_11", window=A, hedge_min_notional=5.0, arb_overrides=DENSE, gap=("11:01:33", "11:02:13"), gap_venue="BINANCE",
                                            profile_set={"feed_health.actions.withdraw_quotes_on": "DEGRADED", "feed_health.actions.block_new_quotes_on": "DEGRADED"}),
    "4b_hedge_feed_lost_observe_v111": dict(profile="perp_perp_maker_taker_v1_11", window=A, hedge_min_notional=5.0, arb_overrides=DENSE, gap=("11:01:33", "11:02:13"), gap_venue="BINANCE"),
    "4c_feed_delay_v111": dict(profile="perp_perp_maker_taker_v1_11", window=A, hedge_min_notional=5.0, arb_overrides=DENSE, inject_delay=dict(venue="BINANCE", start="11:01:00", end="11:02:00", delay_s=1.5),
                               profile_set={"feed_health.feed_delayed_ms": 1000.0}),
    # Queue item (d), owner decisions of 16 Nov applied to v1_11 (17 Nov): the same three cases with the PROFILE'S OWN values (no profile_set): FEED_DELAYED 1000 ms, block on DEGRADED,
    # withdraw on the hedge feed DEGRADED, UNSAFE cancels and pauses, OWN_LAG blocks. The 4a / 4b / 4c rows above are the W1 records (observation build, actions set per case).
    "4x_feed_health_v111_decided": dict(profile="perp_perp_maker_taker_v1_11", window=A, hedge_min_notional=5.0, arb_overrides=DENSE),
    "4a_hedge_feed_lost_v111_decided": dict(profile="perp_perp_maker_taker_v1_11", window=A, hedge_min_notional=5.0, arb_overrides=DENSE, gap=("11:01:33", "11:02:13"), gap_venue="BINANCE"),
    "4c_feed_delay_v111_decided": dict(profile="perp_perp_maker_taker_v1_11", window=A, hedge_min_notional=5.0, arb_overrides=DENSE, inject_delay=dict(venue="BINANCE", start="11:01:00", end="11:02:00", delay_s=1.5)),
    "3x_baseline_v110": dict(profile="perp_perp_maker_taker_v1_10", window=A, hedge_min_notional=5.0, arb_overrides=DENSE),
}


def hms(text):
    parts = [float(x) for x in text.split(":")] + [0, 0]
    return DAY0 + parts[0] * 3600 + parts[1] * 60 + parts[2]


layer_only = "--layer-only" in argv          # attribution run (owner 13 Nov): the layer gate in enforce but NO breaker; writes runs/scn_<case>_layeronly and no stage trace under ems_cases
if layer_only:
    argv.remove("--layer-only")
case = argv[0]
st = CASES[case]
out = Path("runs") / (f"scn_{case}_layeronly" if layer_only else f"scn_{case}")
import copy
profile = copy.deepcopy(dict(load_profile(st["profile"])))
for _path, _val in (st.get("profile_set") or {}).items():        # Order 3 cases: profile keys changed for the case only (labelled in the result settings)
    _node = profile
    _parts = _path.split(".")
    for _p in _parts[:-1]:
        _node = _node[_p]
    _node[_parts[-1]] = _val
cfg = apply_profile(profile, dataset="mar2024", data_root="", pair="SOLUSDT", start_date="2026-10-29", end_date="2026-10-29", output_dir=str(out))
cfg.arb.max_position_base = st.get("size", 1.0)
cfg.arb.max_venue_imbalance_base = st.get("gross", 5.0)
cfg.maker.participation_cap = bool(st.get("cap", False)) or bool(cfg.maker.participation_cap)      # the profile's value (v1_9: on, owner 13 Nov) or the case's
for k, v in (st.get("arb_overrides") or {}).items():
    setattr(cfg.arb, k, v)
if st.get("processing_ms"):
    cfg.timing = dataclasses.replace(cfg.timing, processing_ms=st["processing_ms"])
for venue, ms in (st.get("cancel_ms") or {}).items():
    vt = cfg.timing.venues[venue]
    venues = dict(cfg.timing.venues)
    venues[venue] = dataclasses.replace(vt, cancel=dataclasses.replace(vt.cancel, mean_ms=ms, min_ms=ms))
    cfg.timing = dataclasses.replace(cfg.timing, venues=venues)
if st.get("latency_q"):
    q = st["latency_q"]

    def at(grid):            # linear interpolation on the testnet empirical quantile grid
        qs, ms = grid["q"], grid["ms"]
        return next(ms[i - 1] + (ms[i] - ms[i - 1]) * (q - qs[i - 1]) / (qs[i] - qs[i - 1]) for i in range(1, len(qs)) if qs[i] >= q)
    venues = dict(cfg.timing.venues)
    for venue in venues:
        cal = json.loads(Path(f"/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/lifecycle_calibration_perp_{venue}.json").read_text())
        grids, fill = cal["empirical_quantile_grids_by_network"]["testnet"], cal["networks"]["testnet"]["fill_report_delay"]["report_delay_fill_corrected_ms"]["max"]
        label = f"MEASURED:TESTNET q={q} (indicative bound for mainnet, not a measurement of it); stage mapping ASSUMED"

        def spec(ms, old):
            return dataclasses.replace(old, mean_ms=float(ms), jitter_ms=0.0, max_ms=max(old.max_ms, float(ms) + 1.0), min_ms=0.0, source=label)
        vt = venues[venue]
        venues[venue] = dataclasses.replace(vt, entry=spec(at(grids["entry_offset_corrected_ms"]), vt.entry), ack_report=spec(at(grids["ack_rest_rtt_ms"]), vt.ack_report),
                                            fill_report=spec(fill, vt.fill_report), cancel=spec(at(grids["cancel_rest_rtt_ms"]) / 2, vt.cancel), cancel_report=spec(at(grids["cancel_rest_rtt_ms"]) / 2, vt.cancel_report))
    cfg.timing = dataclasses.replace(cfg.timing, venues=venues)
kw = {}
if st["profile"] in ("perp_perp_maker_taker_v1_9", "perp_perp_maker_taker_v1_9s", "perp_perp_maker_taker_v1_10", "perp_perp_maker_taker_v1_11"):
    if st.get("hedge_min_notional") is not None:
        kw["hedge_min_notional"] = st["hedge_min_notional"]       # owner 16 Nov: the hedge venue's real minimum for this run (the replay default is 1 USDT)
    if layer_only:
        kw["layer_risk_mode"] = "enforce"
    else:
        kw["profile_safety"] = True          # owner 13 Nov: the layer risk gate in the profile's own mandatory mode (enforce) and the profile's breaker block, never the replay's defaults (count, no breaker)
if st.get("ghost_s"):
    from crypto_stat_arb.simulation.hedge_faults import HedgeFaultConfig
    cfg.hedge_faults = HedgeFaultConfig(outage_on_fill_s=st["ghost_s"], outage_on_fill_venue="hedge", outage_policy="ghost", max_retries=2, retry_backoff_s=0.25)
    kw["hedge_fault_injection"] = True
if st.get("thin"):
    from crypto_stat_arb.simulation.hedge_faults import HedgeFaultConfig
    cfg.hedge_faults = HedgeFaultConfig(max_retries=st["hedge_retries"], retry_backoff_s=0.25)      # retries only; nothing is injected by this block (the thin book is the fault)
    kw["hedge_thin_book"] = tuple(st["thin"])
if st.get("maker_outage"):
    from crypto_stat_arb.simulation.hedge_faults import HedgeFaultConfig, Outage
    cfg.hedge_faults = HedgeFaultConfig(outages=tuple(Outage("BYBIT", hms(a), hms(b)) for a, b in st["maker_outage"]), outage_policy="blackhole", ack_timeout_s=2.0, max_retries=2, retry_backoff_s=0.25)
    kw["hedge_fault_injection"] = True
replay = SessionReplay(cfg, profile, ticks=dated_ticks("2026-10-29"), recorded=True, decompose_markouts=True, mark_from_mid=True, lifecycle_report=True, regime_boundaries=BND, **kw)
if st.get("clock_inject"):                         # Order 3 (A3): an INJECTED clock measurement (a replay has no measured clock); measured at the end of the window so it never ages out
    from crypto_stat_arb.time_integrity import ClockMeasurement
    replay.session.clock_gate.update(ClockMeasurement(offset_ns=int(st["clock_inject"]["offset_ms"] * 1e6), uncertainty_ns=int(st["clock_inject"]["uncertainty_ms"] * 1e6), measured_ts_ns=int(hms(st["window"][1] + ":00") * 1e9), source="INJECTED"))

if st.get("profile") in ("perp_perp_maker_taker_v1_9", "perp_perp_maker_taker_v1_9s", "perp_perp_maker_taker_v1_10", "perp_perp_maker_taker_v1_11"):
    assert replay.session.oms.maker_ack_deadline, "v1_9 carries oms.maker_ack_deadline (owner 13 Nov)"      # from the profile now, not set here
if st.get("thin_view_top"):                   # the bot sees the same thin touch (the market's touch IS thin): the size_aware cap is taken from this view, every deeper level is the real one
    import copy
    from crypto_stat_arb.oms import OMS
    _real_cap = OMS._size_aware_cap

    def _thin_view_cap(self, plan, chain, spec, book, touch, qty, ts_ns, attempt):
        view, buy = copy.copy(book), spec["side"].value == "BUY"
        levels = dict(view.asks if buy else view.bids)
        best = min(levels) if buy else max(levels)
        levels[best] = min(levels[best], Decimal(str(st["thin_view_top"])))
        if buy:
            view.asks = levels
        else:
            view.bids = levels
        return _real_cap(self, plan, chain, spec, view, touch, qty, ts_ns, attempt)
    OMS._size_aware_cap = _thin_view_cap
if st.get("rate_limit"):                      # a lowered limit: 6 requests per 10 s per bucket (ASSUMED test value), so new quotes are refused early while cancels (100% tier) and hedges (80% tier) keep room
    from crypto_stat_arb.rate_limits import ScopedRateLimiter

    class Row:
        def __init__(self, name, scope, limit):
            self.name, self.scope, self.limit, self.interval_seconds, self.status = name, scope, limit, 10.0, "ASSUMED"
    replay.session.oms.router.scoped_limits = ScopedRateLimiter({("BYBIT", "PERP"): [Row("HTTP", "IP", 6), Row("/v5/order/create", "UID_ENDPOINT", 6), Row("/v5/order/cancel", "UID_ENDPOINT", 6)],
                                                                 ("BINANCE", "PERP"): [Row("REQUEST_WEIGHT", "IP", 6), Row("ORDERS", "ACCOUNT", 6)]})

if st.get("amend_reject_every") or st.get("amend_timeout_every"):          # the venue rejects / never answers every Nth amend request (the request itself is lost: the old order stays as it was)
    from crypto_stat_arb.execution import ExecutionEventType
    from crypto_stat_arb.simulation.queue_adapter import QueueAwareVenueAdapter
    _real_replace = QueueAwareVenueAdapter._replace_own
    _n = [0]

    def _faulty_replace(self, request, book):
        _n[0] += 1
        if st.get("amend_reject_every") and _n[0] % st["amend_reject_every"] == 0:
            return (self._callback(request, ExecutionEventType.REJECTED, reason="amend_rejected_injected"),)
        if st.get("amend_timeout_every") and _n[0] % st["amend_timeout_every"] == 0:
            return ()
        return _real_replace(self, request, book)
    QueueAwareVenueAdapter._replace_own = _faulty_replace

if st.get("disconnect_at_amend_s"):           # item 10 (owner 17 Nov): the maker venue's gateway goes down AS each amend arrives (for this many seconds): the request is lost into the disconnect, every time
    from crypto_stat_arb.simulation.queue_adapter import QueueAwareVenueAdapter
    _real_replace2 = QueueAwareVenueAdapter._replace_own
    disconnects_at_amend = [0]

    def _disconnect_on_amend(self, request, book):
        now_s = self._at(request)
        self._windows[f"amend-disconnect-{request.order_id}"] = Outage(self.venue, now_s, now_s + float(st["disconnect_at_amend_s"]))
        disconnects_at_amend[0] += 1
        return ()
    QueueAwareVenueAdapter._replace_own = _disconnect_on_amend

if st.get("duplicate_filled"):                # every venue "Filled" / "Partial" report is delivered twice, the second under a new update id
    from crypto_stat_arb.execution import ExecutionEventType
    from crypto_stat_arb.oms import OMS
    real = OMS.ingest_order_update

    def doubled(self, update):
        result = real(self, update)
        if update.state in (ExecutionEventType.FILLED, ExecutionEventType.PARTIAL) and "INTERNAL" not in str(update.source):
            real(self, dataclasses.replace(update, update_id=update.update_id + "-dup"))
        return result
    OMS.ingest_order_update = doubled

if st.get("debug_requote"):                    # diagnosis only (owner 14 Nov): print what requote_target saw whenever it answered "no target" for a resting quote
    _strat = replay.strategy
    _real_rt = _strat.requote_target

    def _rt(maker_venue, side, qty, books, now):
        target, why = _real_rt(maker_venue, side, qty, books, now)
        if target is None:
            taker = next(v for v in books if v != maker_venue)
            mb, tb = books[maker_venue], books[taker]
            cfg = _strat.config
            bid = side == "buy"
            fee = _strat.venues[maker_venue].fees.maker_bps + _strat.venues[taker].fees.taker_bps
            view = _strat._hedge_vwap_view(tb, "bid" if bid else "ask", qty)
            px_dbg = None if view is None else _strat._quote_limit("bid" if bid else "ask", view.best_bid if bid else view.best_ask, fee, _strat._hedge_move_bps("bid" if bid else "ask", maker_venue, taker, books, None, cfg.adverse_requote_horizon_s), _strat._funding_holding_bps(maker_venue, taker, "bid" if bid else "ask", now))
            print("REQUOTE_NONE", json.dumps({"now": now, "side": side, "qty": qty, "why": why, "maker_bid": mb.best_bid, "maker_ask": mb.best_ask, "hedge_bid": tb.best_bid, "hedge_ask": tb.best_ask,
                                              "hedge_vwap": None if view is None else view.best_bid, "limit_px_unrounded": px_dbg, "fee_bps": fee, "move_bps": _strat._hedge_move_bps("bid" if bid else "ask", maker_venue, taker, books, None, cfg.adverse_requote_horizon_s),
                                              "min_net_edge": cfg.min_net_edge_bps, "buffer": cfg.safety_buffer_bps, "tick": cfg.venue_ticks.get(maker_venue)}), flush=True)
        return target, why
    _strat.requote_target = _rt

t0, t1 = hms(st["window"][0] + ":00"), hms(st["window"][1] + ":00")
gap = None if not st.get("gap") else (hms(st["gap"][0]), hms(st["gap"][1]))
market_rows = []
delay_injected = [0]
inj_events = []                                   # the injected shifts (aggregates are written to session_injection.json)


ARMED_EDGE = replay.strategy.config.min_net_edge_bps
replay.strategy.config.min_net_edge_bps = 1e9      # warm-up: the books are fed from the capture's start (09:56 UTC, the only snapshot is in its first part) with quoting off until the window opens


def events():
    armed, reset_done, n, reconnected, shadow = False, False, 0, False, {}
    gap_shadow = {}
    inj = st.get("inject_move")                                  # INJECTED hedge-venue move (owner 16 Nov, case 2i on v1_10): see CASES
    inj_on = {"shift": None}
    import copy as _copy
    inj_shadow = {"book": _copy.deepcopy(replay.session.books[(inj["venue"], replay.instrument_id)]) if inj is not None else None}
    for ev in iter_recorded_events(SESSION):
        ts = ev.receive_ts_ns / 1e9
        n += 1
        if inj is not None:
            from crypto_stat_arb.models import Level, TradeEvent
            true_ev = ev
            k = int((ts - t0 - inj["offset_s"]) // inj["every_s"]) if ts >= t0 + inj["offset_s"] else -1
            into = (ts - t0 - inj["offset_s"]) - k * inj["every_s"] if k >= 0 else None
            active = k >= 0 and into < inj["dur_s"] and ts < t1 - 1
            book = inj_shadow["book"]
            book_ok = book.state.value in ("VALID", "STALE") and bool(book.bids) and bool(book.asks)      # a shadow of the venue's book with the session book's own delta rules, fed the TRUE stream with no latency: its sequence is the one the next delta continues from
            at_delta = isinstance(ev, MarketEvent) and ev.venue == inj["venue"] and ev.event_type.value == "BOOK_DELTA"      # the snapshot is placed right before a delta of the venue and carries that delta's start id minus 1, so the delta bridges it (Binance chains on pu after the first bridging delta)
            if active and inj_on["shift"] is None and book_ok and at_delta:
                mid = (book.best_bid + book.best_ask) / 2
                tick = Decimal("0.01")
                shift = (Decimal(str(mid)) * Decimal(str(inj["bps"])) / Decimal(10000) / tick).to_integral_value() * tick * (-1 if k % 2 == 0 else 1)      # down at even periods (hits a resting bid), up at odd (a resting ask)
                inj_on["shift"] = shift
                yield MarketEvent.from_dict({"event_type": "BOOK_SNAPSHOT", "venue": inj["venue"], "instrument_id": book.instrument_id, "exchange_ts_ns": ev.exchange_ts_ns, "receive_ts_ns": ev.receive_ts_ns,
                                             "sequence": ev.sequence_start - 1, "bids": [[str(Decimal(str(p)) + shift), str(q)] for p, q in sorted(book.bids.items(), reverse=True)], "asks": [[str(Decimal(str(p)) + shift), str(q)] for p, q in sorted(book.asks.items())]})
                inj_events.append({"ts": ts, "shift": str(shift), "mid": float(mid)})
            elif not active and inj_on["shift"] is not None and book_ok and at_delta:
                sh0 = inj_on["shift"]
                inj_on["shift"] = None
                yield MarketEvent.from_dict({"event_type": "BOOK_SNAPSHOT", "venue": inj["venue"], "instrument_id": book.instrument_id, "exchange_ts_ns": ev.exchange_ts_ns, "receive_ts_ns": ev.receive_ts_ns,
                                             "sequence": ev.sequence_start - 1, "bids": [[str(Decimal(str(p))), str(q)] for p, q in sorted(book.bids.items(), reverse=True)], "asks": [[str(Decimal(str(p))), str(q)] for p, q in sorted(book.asks.items())]})
            if inj_on["shift"] is not None and ev.venue == inj["venue"]:
                sh = inj_on["shift"]
                if isinstance(ev, MarketEvent):
                    import dataclasses as _dc
                    ev = _dc.replace(ev, bids=tuple(Level(l.price + sh, l.quantity) for l in ev.bids), asks=tuple(Level(l.price + sh, l.quantity) for l in ev.asks))
                elif isinstance(ev, TradeEvent):
                    import dataclasses as _dc
                    ev = _dc.replace(ev, price=ev.price + sh)
            if isinstance(true_ev, MarketEvent) and true_ev.venue == inj["venue"]:
                inj_shadow["book"].apply(true_ev)                    # the true event, after any snapshot above was taken (a snapshot never contains the event that follows it)
        if st.get("reset_after_s") and not reset_done and n % 200 == 0:                      # the operator's manual reset, scripted: once the breaker has tripped and the wait is over
            trips = [e["ts_ns"] / 1e9 for e in replay.session.ledger.events if e["event_type"] == "BREAKER_TRIP"]
            if trips and ts >= trips[0] + st["reset_after_s"]:
                replay.session.reset_breaker(int(ts * 1e9), "operator-drill")
                reset_done = True
        if ts >= t1:
            break
        if not armed and ts >= t0:
            replay.strategy.config.min_net_edge_bps = ARMED_EDGE
            armed = True
        dl = st.get("inject_delay")                                   # W1 (W13-B1): INJECTED feed delay: the venue's exchange stamps are moved into the past inside the window, so receive minus exchange grows by delay_s
        if dl is not None and ev.venue == dl["venue"] and hms(dl["start"]) <= ts < hms(dl["end"]):
            import dataclasses as _dc2
            ev = _dc2.replace(ev, exchange_ts_ns=ev.exchange_ts_ns - int(dl["delay_s"] * 1e9))
            delay_injected[0] += 1
        if gap is not None and st.get("gap_venue") is not None:
            # ONE venue's feed lost (Order 3 case 3e, W1 cases 4a / 4b). The venue's true stream keeps feeding a copy of the session's own book (its delta rules, its sequence), so that the reconnect can
            # be a snapshot placed right before a delta of the venue and carrying that delta's start id minus 1 (the delta bridges it): the book is VALID again at once. (17 Nov: the first version rebuilt the
            # snapshot from a fresh book and the venue stayed invalid for over an hour after the gap, an artefact that case 3e reported.)
            gv = st["gap_venue"]
            if "book" not in gap_shadow:
                import copy as _copy2
                gap_shadow["book"] = _copy2.deepcopy(replay.session.books[(gv, replay.instrument_id)])
            if ev.venue == gv:
                gb = gap_shadow["book"]
                if isinstance(ev, MarketEvent):
                    ok = gb.state.value in ("VALID", "STALE") and bool(gb.bids) and bool(gb.asks)
                    if not reconnected and ts >= gap[1] and ev.event_type.value == "BOOK_DELTA" and ok:
                        reconnected = True
                        yield MarketEvent.from_dict({"event_type": "BOOK_SNAPSHOT", "venue": gv, "instrument_id": gb.instrument_id, "exchange_ts_ns": ev.exchange_ts_ns, "receive_ts_ns": ev.receive_ts_ns,
                                                     "sequence": ev.sequence_start - 1, "bids": [[str(p), str(q)] for p, q in sorted(gb.bids.items(), reverse=True)], "asks": [[str(p), str(q)] for p, q in sorted(gb.asks.items())]})
                    gb.apply(ev)
                if ts >= gap[0] and not reconnected:
                    continue                   # the venue's feed is lost from the gap's start until its reconnect snapshot
        elif gap is not None:
            if isinstance(ev, MarketEvent) and (st.get("gap_venue") is None or ev.venue == st["gap_venue"]):
                shadow.setdefault(ev.venue, OrderBook(ev.venue, ev.instrument_id)).apply(ev)         # the books as the exchange has them, outage or not
            if gap[0] <= ts < gap[1] and (st.get("gap_venue") is None or ev.venue == st["gap_venue"]):
                continue                       # the injected outage: neither feed delivers anything (Order 3: with gap_venue, only that venue's feed)
            if not reconnected and ts >= gap[1]:
                reconnected = True             # the reconnect: each feed restarts with a SNAPSHOT of the book as it stands (the recorded capture has none after its first part, so without this a gap could never end)
                for venue, book in shadow.items():
                    yield MarketEvent.from_dict({"event_type": "BOOK_SNAPSHOT", "venue": venue, "instrument_id": book.instrument_id, "exchange_ts_ns": ev.exchange_ts_ns, "receive_ts_ns": ev.receive_ts_ns,
                                                 "sequence": book.sequence, "bids": [[str(p), str(q)] for p, q in sorted(book.bids.items(), reverse=True)], "asks": [[str(p), str(q)] for p, q in sorted(book.asks.items())]})
        yield ev


t = time.time()
result = replay.run_recorded(events())
result["run"] = {"case": case, "settings": {k: v for k, v in st.items()}, "window_utc": st["window"], "wall_s": round(time.time() - t, 1), "labels": ["EXPLORATORY: mechanics only, no profit claim", "2-hour window of the 2026-10-29 capture; the books are warmed from the capture start (09:56 UTC) with quoting off until the window opens: counts of events and the regime samples include the warm-up",
                 "mark = mid, no funding", "regime boundaries IN-SAMPLE (the capture's own terciles)"]}
write_result(result, out)
safety = safety_event_text(replay.session.ledger.events, ("BREAKER_", "LIMIT_STATE", "FEED_HEALTH", "TIME_INTEGRITY", "KILL_SWITCH", "UNWIND_", "HEDGE_ALARM", "MARGIN_PRICE", "NEW_RISK_HELD", "RECONCILIATION", "QUOTE_SKIPPED"))      # W2a hygiene: the writer lives in the replay module
if safety:                                   # the safety events of the run in full (payloads included), next to the standard outputs
    (out / "session_safety_events.jsonl").write_text(safety)
else:
    (out / "session_safety_events.jsonl").unlink(missing_ok=True)      # a rerun with no safety events must not leave the previous run's file behind
if st.get("inject_move"):
    (out / "session_injection.json").write_text(json.dumps({"label": "INJECTED: a scripted shift of the hedge venue's book on the real capture (not a market event)", "setting": st["inject_move"], "shifts_applied": len(inj_events), "events": inj_events}, indent=1))
if replay.session.feed_health is not None:         # W1: the feed-health verdict of the run (aggregates; the transition rows carry venue, stream, state, cause and time only)
    (out / "session_feed_health.json").write_text(json.dumps({"label": "EXPLORATORY: feed health on the 29 Oct capture; 4a-4c INJECT their condition", "injected_delay_events": delay_injected[0],
                                                              **replay.session.feed_health_report(getattr(replay, "last_ns", None))}, indent=1, default=str))
if replay.session._limits is not None:             # Order 3: every limit, whether it bound, and why quoting stopped (aggregates only)
    (out / "session_limits.json").write_text(json.dumps({"label": "EXPLORATORY: session limits of Order 3 on the 29 Oct capture; the condition is INJECTED in the 3x cases", **replay.session.limits_report()}, indent=1, default=str))
from new_backtest.session_replay import s0_audit
(out / "session_s0_audit.json").write_text(json.dumps(s0_audit(replay.session.ledger.events, replay.session.oms.plans, replay.session.reprice_counters), indent=1, default=str))       # owner 14 Nov: the counts for case 2i (a-j)
sigs = result["signals"]
cases_dir = Path("/Users/vaibhav/Documents/Newproject/out/integration/ems_cases")
cases_dir.mkdir(parents=True, exist_ok=True)
trace_rows = write_stage_trace(replay, result, (out if layer_only else cases_dir).joinpath(f"scn_{case}.jsonl"))      # W2a hygiene: the writer lives in the replay module
write_run_record(out, replay)
print(json.dumps({"case": case, "wall_s": result["run"]["wall_s"], "events": result["events"], "signals": len(sigs), "orders": len(result["orders"]), "fills": len(result["fills"]), "trace_rows": trace_rows,
                  "positions": result["positions"]}, indent=1))
