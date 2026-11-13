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
    "2n_size1": dict(profile="perp_perp_maker_taker_v1_9", window=A, size=1.0, gross=5.0, cap=True),
    "2n_size5": dict(profile="perp_perp_maker_taker_v1_9", window=A, size=5.0, gross=25.0, cap=True),
    "2n_size25": dict(profile="perp_perp_maker_taker_v1_9", window=A, size=25.0, gross=125.0, cap=True),
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
if st["profile"] == "perp_perp_maker_taker_v1_9":
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

if st.get("profile") == "perp_perp_maker_taker_v1_9":
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
    armed, reset_done, n, reconnected, shadow = False, False, 0, False, {}
    for ev in iter_recorded_events(SESSION):
        ts = ev.receive_ts_ns / 1e9
        n += 1
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
        if gap is not None:
            if isinstance(ev, MarketEvent):
                shadow.setdefault(ev.venue, OrderBook(ev.venue, ev.instrument_id)).apply(ev)         # the books as the exchange has them, outage or not
            if gap[0] <= ts < gap[1]:
                continue                       # the injected outage: neither feed delivers anything
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
safety = [e for e in replay.session.ledger.events if e["event_type"].startswith(("BREAKER_", "KILL_SWITCH", "UNWIND_", "HEDGE_ALARM", "MARGIN_PRICE", "NEW_RISK_HELD", "RECONCILIATION", "QUOTE_SKIPPED"))]
if safety:                                   # the safety events of the run in full (payloads included), next to the standard outputs
    (out / "session_safety_events.jsonl").write_text("".join(json.dumps({k: (str(v) if not isinstance(v, (int, float, bool, type(None), list, dict)) else v) for k, v in e.items()}, sort_keys=True, default=str) + "\n" for e in safety))
else:
    (out / "session_safety_events.jsonl").unlink(missing_ok=True)      # a rerun with no safety events must not leave the previous run's file behind
sigs = result["signals"]
trace = stage_trace(replay.session.ledger.events, [(s["ts"], "signal-trigger", "book") for s in sigs])
cases_dir = Path("/Users/vaibhav/Documents/Newproject/out/integration/ems_cases")
cases_dir.mkdir(parents=True, exist_ok=True)
(out if layer_only else cases_dir).joinpath(f"scn_{case}.jsonl").write_text("".join(json.dumps(r, sort_keys=True, default=str) + "\n" for r in trace))
print(json.dumps({"case": case, "wall_s": result["run"]["wall_s"], "events": result["events"], "signals": len(sigs), "orders": len(result["orders"]), "fills": len(result["fills"]), "trace_rows": len(trace),
                  "positions": result["positions"]}, indent=1))
