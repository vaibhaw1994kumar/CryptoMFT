"""EMS acceptance scenario replays on 2-hour windows of the 2026-10-29 capture (owner 12 Nov, v1_9_TOLERANCES.md part 2, section K). A run wrapper: the cases differ only by settings the core already has (profile,
window, timing, injected faults, a lowered rate limit, an injected feed gap, an injected duplicate "Filled"); nothing here decides anything. Each case writes the standard replay outputs to runs/scn_<case>/ and its
timestamped stage trace to out/integration/ems_cases/scn_<case>.jsonl. READ ONLY on data/.
usage: scenarios.py CASE [--src DIR]        CASE is one of CASES (see below)"""
import dataclasses
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
from new_backtest.profiles import apply_profile, load_profile
from new_backtest.session_replay import SessionReplay, dated_ticks, iter_recorded_events, write_result

SESSION = Path("/Users/vaibhav/Documents/Newproject/data/live_sessions/solusdt/2026-10-29/SOLUSDT/e9b81728a3054812a48035a1b3e02d42")
DAY0 = 1793232000                      # 2026-10-29 00:00:00 UTC
BND = "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/regime_boundaries_day1_2026-10-29_insample.json"
A, B = ("11:00", "13:00"), ("19:00", "21:00")      # the windows that hold the capture natural maker fills (11:15 and 12:30 UTC; 19:59 UTC)

# case -> settings. profile v1_9 (size_aware) unless the case needs another; every case uses 1 SOL unless it says size.
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
    "2m_processing": dict(profile="perp_perp_maker_taker_v1_9", window=A, processing_ms={"strategy": 5.0, "risk": 5.0}),
    "2n_size1": dict(profile="perp_perp_maker_taker_v1_9", window=A, size=1.0, gross=5.0, cap=True),
    "2n_size5": dict(profile="perp_perp_maker_taker_v1_9", window=A, size=5.0, gross=25.0, cap=True),
    "2n_size25": dict(profile="perp_perp_maker_taker_v1_9", window=A, size=25.0, gross=125.0, cap=True),
}


def hms(text):
    parts = [int(x) for x in text.split(":")] + [0, 0]
    return DAY0 + parts[0] * 3600 + parts[1] * 60 + parts[2]


case = argv[0]
st = CASES[case]
out = Path("runs") / f"scn_{case}"
profile = dict(load_profile(st["profile"]))
cfg = apply_profile(profile, dataset="mar2024", data_root="", pair="SOLUSDT", start_date="2026-10-29", end_date="2026-10-29", output_dir=str(out))
cfg.arb.max_position_base = st.get("size", 1.0)
cfg.arb.max_venue_imbalance_base = st.get("gross", 5.0)
cfg.maker.participation_cap = bool(st.get("cap", False))
if st.get("processing_ms"):
    cfg.timing = dataclasses.replace(cfg.timing, processing_ms=st["processing_ms"])
for venue, ms in (st.get("cancel_ms") or {}).items():
    vt = cfg.timing.venues[venue]
    venues = dict(cfg.timing.venues)
    venues[venue] = dataclasses.replace(vt, cancel=dataclasses.replace(vt.cancel, mean_ms=ms, min_ms=ms))
    cfg.timing = dataclasses.replace(cfg.timing, venues=venues)
kw = {}
if st.get("ghost_s"):
    from crypto_stat_arb.simulation.hedge_faults import HedgeFaultConfig
    cfg.hedge_faults = HedgeFaultConfig(outage_on_fill_s=st["ghost_s"], outage_on_fill_venue="hedge", outage_policy="ghost", max_retries=2, retry_backoff_s=0.25)
    kw["hedge_fault_injection"] = True
replay = SessionReplay(cfg, profile, ticks=dated_ticks("2026-10-29"), recorded=True, decompose_markouts=True, mark_from_mid=True, lifecycle_report=True, regime_boundaries=BND, **kw)

if st.get("rate_limit"):                      # a lowered limit: 6 requests per 10 s per bucket (ASSUMED test value), so new quotes are refused early while cancels (100% tier) and hedges (80% tier) keep room
    from crypto_stat_arb.rate_limits import ScopedRateLimiter

    class Row:
        def __init__(self, name, scope, limit):
            self.name, self.scope, self.limit, self.interval_seconds, self.status = name, scope, limit, 10.0, "ASSUMED"
    replay.session.oms.router.scoped_limits = ScopedRateLimiter({("BYBIT", "PERP"): [Row("HTTP", "IP", 6), Row("/v5/order/create", "UID_ENDPOINT", 6), Row("/v5/order/cancel", "UID_ENDPOINT", 6)],
                                                                 ("BINANCE", "PERP"): [Row("REQUEST_WEIGHT", "IP", 6), Row("ORDERS", "ACCOUNT", 6)]})

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

t0, t1 = hms(st["window"][0] + ":00"), hms(st["window"][1] + ":00")
gap = None if not st.get("gap") else (hms(st["gap"][0]), hms(st["gap"][1]))
market_rows = []


ARMED_EDGE = replay.strategy.config.min_net_edge_bps
replay.strategy.config.min_net_edge_bps = 1e9      # warm-up: the books are fed from the capture's start (09:56 UTC, the only snapshot is in its first part) with quoting off until the window opens


def events():
    armed = False
    for ev in iter_recorded_events(SESSION):
        ts = ev.receive_ts_ns / 1e9
        if ts >= t1:
            break
        if not armed and ts >= t0:
            replay.strategy.config.min_net_edge_bps = ARMED_EDGE
            armed = True
        if gap is not None and gap[0] <= ts < gap[1]:
            continue                           # the injected outage: neither feed delivers anything
        yield ev


t = time.time()
result = replay.run_recorded(events())
result["run"] = {"case": case, "settings": {k: v for k, v in st.items()}, "window_utc": st["window"], "wall_s": round(time.time() - t, 1), "labels": ["EXPLORATORY: mechanics only, no profit claim", "2-hour window of the 2026-10-29 capture; the books are warmed from the capture start (09:56 UTC) with quoting off until the window opens: counts of events and the regime samples include the warm-up",
                 "mark = mid, no funding", "regime boundaries IN-SAMPLE (the capture's own terciles)"]}
write_result(result, out)
sigs = result["signals"]
trace = stage_trace(replay.session.ledger.events, [(s["ts"], "signal-trigger", "book") for s in sigs])
cases_dir = Path("/Users/vaibhav/Documents/Newproject/out/integration/ems_cases")
cases_dir.mkdir(parents=True, exist_ok=True)
(cases_dir / f"scn_{case}.jsonl").write_text("".join(json.dumps(r, sort_keys=True, default=str) + "\n" for r in trace))
print(json.dumps({"case": case, "wall_s": result["run"]["wall_s"], "events": result["events"], "signals": len(sigs), "orders": len(result["orders"]), "fills": len(result["fills"]), "trace_rows": len(trace),
                  "positions": result["positions"]}, indent=1))
