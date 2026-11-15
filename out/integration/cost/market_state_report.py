"""Execution cost model, step B (a run wrapper; the logic is VenueState in the core): feed the 2026-10-29 capture through the venue views and the core VenueState (depth_quality on, the profile-less defaults g=0.25, m=3 ticks,
N=10 bps) and sample every field on a 1 s grid, both venues, both sides; also the update resolution of every stream. FEED ONLY, EXPLORATORY, APPROXIMATE. READ ONLY on data/.
usage: market_state_report.py OUT_DIR [--src SRC] [--minutes M]"""
import collections, json, sys, time
from pathlib import Path
argv = sys.argv[1:]
src = "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src"
if "--src" in argv:
    i = argv.index("--src"); src = argv[i + 1]; del argv[i:i + 2]
minutes = None
if "--minutes" in argv:
    i = argv.index("--minutes"); minutes = float(argv[i + 1]); del argv[i:i + 2]
sys.path.insert(0, src)
import numpy as np
from crypto_stat_arb.derivatives import DerivativeTicker
from crypto_stat_arb.models import EventType, TradeEvent
from crypto_stat_arb.orderbook import OrderBook as CoreBook
from new_backtest.profiles import apply_profile, load_profile
from new_backtest.session_replay import SessionReplay, dated_ticks, iter_recorded_events
SESSION = Path("/Users/vaibhav/Documents/Newproject/data/live_sessions/solusdt/2026-10-29/SOLUSDT/e9b81728a3054812a48035a1b3e02d42")
OUT = Path(argv[0]); OUT.mkdir(parents=True, exist_ok=True)
profile = load_profile("perp_perp_maker_taker_v1_9")
cfg = apply_profile(profile, dataset="mar2024", data_root="", pair="SOLUSDT", start_date="2026-10-29", end_date="2026-10-29", output_dir=str(OUT))
cfg.venue_state.depth_quality = True
replay = SessionReplay(cfg, profile, ticks=dated_ticks("2026-10-29"), recorded=True)
views, states = replay.views, replay.states
for v, st in states.items():
    st.set_tick(replay.ticks[v])
cores, counts = {}, collections.Counter()
samples = collections.defaultdict(lambda: collections.defaultdict(list))        # (venue, side) -> field -> values
asym = collections.defaultdict(list)
status = collections.Counter()
last_ts = {}
dts = collections.defaultdict(list)                                              # (venue, kind) -> inter-arrival seconds (subsampled)
first = nxt = None
t0 = time.time(); n = 0
for ev in iter_recorded_events(SESSION):
    if isinstance(ev, DerivativeTicker):
        continue
    ts = ev.receive_ts_ns / 1e9
    if first is None:
        first, nxt = ts, ts + 120.0                                              # two minutes of warm-up before sampling
    if minutes is not None and ts - first > minutes * 60:
        break
    kind = "trade" if isinstance(ev, TradeEvent) else ("book_snapshot" if ev.event_type == EventType.BOOK_SNAPSHOT else "book_delta")
    key = (ev.venue, kind)
    if key in last_ts:
        dts[key].append(ts - last_ts[key])
    last_ts[key] = ts
    while ts >= nxt:
        for fv in ("bybit", "binance"):
            v, st = views[fv], states[fv]
            if not v.ready or v.age_s(nxt) > 1.0:
                continue
            dq = st.depth_quality(v, nxt)
            if dq["gap_asymmetry"] is not None:
                asym[fv].append(dq["gap_asymmetry"])
            for side in ("bid", "ask"):
                g, d = dq["gap"][side], samples[(fv, side)]
                status[(fv, side, "gap", g["status"])] += 1
                if g["status"] == "GAP":
                    d["gap_depth"].append(g["depth"]); d["gap_dist_bps"].append(g["distance_bps"]); d["gap_persist_" + g["persistence"]].append(1)
                if g["count_within_bps"] is not None:
                    d["gap_count"].append(g["count_within_bps"])
                sv = dq["survival"][side]
                if sv is not None:
                    d["surv_median"].append(sv["median"]); d["surv_mean"].append(sv["mean"]); d["surv_p10"].append(sv["p10"])
                if dq["cancel_to_trade"][side] is not None:
                    d["c2t"].append(dq["cancel_to_trade"][side])
                status[(fv, side, "spike", dq["spike"][side]["status"])] += 1
        nxt += 1.0
    fv = replay._feed_venue(ev.venue)
    view, state = views[fv], states[fv]
    if isinstance(ev, TradeEvent):
        p, q = float(ev.price), float(ev.quantity)
        if p > 0 and q > 0:
            sign = 1 if ev.aggressor_side == "BUY" else -1
            view.on_trade(p, q, sign, ts); state.on_trade(ts, p, q, sign)
    elif ev.event_type in (EventType.BOOK_SNAPSHOT, EventType.BOOK_DELTA):
        core = cores.setdefault((ev.venue, ev.instrument_id), CoreBook(ev.venue, ev.instrument_id))
        before = core.stats.gaps
        core.apply(ev)
        if core.stats.gaps > before:
            state.note_feed_gap(ts); counts["feed_gaps_" + fv] += 1
        if ev.event_type == EventType.BOOK_SNAPSHOT:
            b, a = [[float(l.price), float(l.quantity)] for l in ev.bids], [[float(l.price), float(l.quantity)] for l in ev.asks]
            if b and a:
                view.apply_snapshot(b, a, ts); state.on_book(view, ts)
        else:
            for side, lv in (("bid", ev.bids), ("ask", ev.asks)):
                for l in lv:
                    view.apply_delta(side, float(l.price), float(l.quantity), "modify", ts)
            view.end_message(); state.on_book(view, ts)
    n += 1
    if n % 500_000 == 0:
        print(f"{n} events, t={ts - first:.0f}s, wall {time.time() - t0:.0f}s", flush=True)
pct = lambda a, ps=(5, 25, 50, 75, 95): {str(p): float(np.percentile(a, p)) for p in ps} if len(a) else None
rep = {"label": "EXPLORATORY, APPROXIMATE, FEED ONLY, ONE DAY (2026-10-29)", "settings": {k: getattr(cfg.venue_state, k) for k in dir(cfg.venue_state) if k.startswith("dq_")}, "events": n, "wall_s": round(time.time() - t0, 1),
       "feed_gaps": dict(counts), "venues": {}}
for fv in ("bybit", "binance"):
    r = {"closed_gap_lifetimes_by_class": {k: v for k, v in states[fv].dq_stats.items() if k.startswith("gap_closed_")}, "gap_asymmetry": {"n": len(asym[fv]), "mean": float(np.mean(asym[fv])) if asym[fv] else None, "share_abs_gt_0.5": float(np.mean(np.abs(asym[fv]) > 0.5)) if asym[fv] else None}}
    for side in ("bid", "ask"):
        d = samples[(fv, side)]
        tot = sum(v for k, v in status.items() if k[:3] == (fv, side, "gap"))
        r[side] = {"gap_status_share": {k[3]: v / tot for k, v in status.items() if k[:3] == (fv, side, "gap")},
                   "spike_status_share": {k[3]: v / max(1, sum(w for kk, w in status.items() if kk[:3] == (fv, side, "spike"))) for k, v in status.items() if k[:3] == (fv, side, "spike")},
                   "size_before_first_gap": pct(d["gap_depth"]), "first_gap_distance_bps": pct(d["gap_dist_bps"]), "gap_count_within_N_bps": pct(d["gap_count"]),
                   "standing_first_gap_persistence_share": {k.replace("gap_persist_", ""): len(v) / max(1, len(d["gap_depth"])) for k, v in d.items() if k.startswith("gap_persist_")},
                   "survival_median_of_median": pct(d["surv_median"]), "survival_mean": pct(d["surv_mean"]), "survival_p10": pct(d["surv_p10"]), "cancel_to_trade": pct(d["c2t"], (5, 25, 50, 75, 95, 99))}
    rep["venues"][fv] = r
rep["update_resolution_s"] = {f"{v}/{k}": {"n": len(a) + 1, "p10": float(np.percentile(a, 10)), "median": float(np.percentile(a, 50)), "p90": float(np.percentile(a, 90)), "p99": float(np.percentile(a, 99)), "max": float(max(a))} for (v, k), a in sorted(dts.items()) if a}
(OUT / "market_state.json").write_text(json.dumps(rep, indent=1))
print(json.dumps(rep, indent=1))
