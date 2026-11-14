"""Depth-quality factor study, step 1 (a run wrapper, no logic of the platform): feed the 2026-10-29 capture through the venue views and the core VenueState (depth_quality on) and record, on a 1 s grid, the factors and
the outcomes defined in FACTOR_STUDY_RULES.md. FEED ONLY: no orders, no simulator fills, no session. EXPLORATORY, ONE DAY, PROXY (hypothetical hedge walks and public prints, never our fills). READ ONLY on data/.
usage: factor_study.py OUT_DIR [--src SRC] [--minutes M]"""
import collections
import gzip
import itertools
import json
import sys
import time
from pathlib import Path

argv = sys.argv[1:]
src = "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src"
if "--src" in argv:
    i = argv.index("--src"); src = argv[i + 1]; del argv[i:i + 2]
minutes = None
if "--minutes" in argv:
    i = argv.index("--minutes"); minutes = float(argv[i + 1]); del argv[i:i + 2]
sys.path.insert(0, src)
from crypto_stat_arb.derivatives import DerivativeTicker                      # noqa: E402
from crypto_stat_arb.models import EventType, TradeEvent                       # noqa: E402
from crypto_stat_arb.orderbook import OrderBook as CoreBook                    # noqa: E402
from new_backtest.profiles import apply_profile, load_profile                  # noqa: E402
from new_backtest.session_replay import SessionReplay, dated_ticks, iter_recorded_events   # noqa: E402

SESSION = Path("/Users/vaibhav/Documents/Newproject/data/live_sessions/solusdt/2026-10-29/SOLUSDT/e9b81728a3054812a48035a1b3e02d42")
OUT = Path(argv[0])
OUT.mkdir(parents=True, exist_ok=True)
GRID_S, DELTA_S, MARKOUT_S = 1.0, 0.2, 5.0
SIZES = (5.0, 25.0, 100.0)
HEDGE_FEED, MAKER_FEED = "binance", "bybit"
MAX_AGE_S = 1.0                                                                  # a view older than this is not sampled

profile = load_profile("perp_perp_maker_taker_v1_9")
cfg = apply_profile(profile, dataset="mar2024", data_root="", pair="SOLUSDT", start_date="2026-10-29", end_date="2026-10-29", output_dir=str(OUT))
cfg.venue_state.depth_quality = True
replay = SessionReplay(cfg, profile, ticks=dated_ticks("2026-10-29"), recorded=True)
views, states = replay.views, replay.states
cores: dict = {}
rows = open(OUT / "factor_rows.jsonl", "w")
counts = collections.Counter()
pending = collections.deque()                                                   # (target_ts, kind, payload), in target order (the targets of one kind are increasing; two kinds are kept apart below)
pend_s, pend_m = collections.deque(), collections.deque()


def slippage(view, side, q):
    w = view.walk(side, q)
    if w.filled_qty < q - 1e-9 or w.exhausted:
        return None
    touch = view.best_ask if side == "ask" else view.best_bid
    if touch <= 0:
        return None
    return ((w.vwap - touch) if side == "ask" else (touch - w.vwap)) / touch * 1e4


def factors(feed, side, ts):
    dq = states[feed].depth_quality(views[feed], ts, side)
    g = dq["gap"][side]
    return g["status"], g["depth"], dq["flicker"][side]


def sample_grid(ts):
    view, state = views[HEDGE_FEED], states[HEDGE_FEED]
    if not view.ready or view.age_s(ts) > MAX_AGE_S:
        counts["grid_view_not_ready"] += 1
        return
    for side in ("ask", "bid"):                                                   # a buy hedge consumes the asks, a sell hedge the bids
        status, depth, flick = factors(HEDGE_FEED, side, ts)
        if status == "FEED_FAULT":
            counts["slip_feed_fault"] += 1
            continue
        if flick is None:
            counts["slip_flicker_warmup"] += 1
            continue
        for q in SIZES:
            s0 = slippage(view, side, q)
            if s0 is None:
                counts[f"slip_unfillable_at_t_{q:g}"] += 1
                continue
            pend_s.append((ts + DELTA_S, {"ts": ts, "side": side, "q": q, "status": status, "gap_depth": depth, "flicker": flick, "s0": s0}))


def resolve(ts):
    view = views[HEDGE_FEED]
    while pend_s and pend_s[0][0] <= ts:
        _, p = pend_s.popleft()
        s1 = slippage(view, p["side"], p["q"]) if view.ready else None
        if s1 is None:
            counts[f"slip_unfillable_at_outcome_{p['q']:g}"] += 1
            continue
        rows.write(json.dumps({"kind": "slip", **p, "ds": s1 - p["s0"]}) + "\n")
        counts["slip_rows"] += 1
    mview = views[MAKER_FEED]
    while pend_m and pend_m[0][0] <= ts:
        _, p = pend_m.popleft()
        mid = mview.mid if mview.ready else 0.0
        if mid <= 0:
            counts["markout_no_mid"] += 1
            continue
        mk = ((mid - p["price"]) if p["passive_buyer"] else (p["price"] - mid)) / p["price"] * 1e4
        rows.write(json.dumps({"kind": "markout", **{k: v for k, v in p.items() if k != "passive_buyer"}, "markout_bps": mk}) + "\n")
        counts["markout_rows"] += 1


def on_print(ev, ts):
    price, vol = float(ev.price), float(ev.quantity)
    if price <= 0 or vol <= 0:
        return
    fv = replay._feed_venue(ev.venue)
    view, state = views[fv], states[fv]
    sign = 1 if ev.aggressor_side == "BUY" else -1
    if fv == MAKER_FEED and view.ready and view.age_s(ts) <= MAX_AGE_S:
        side = "ask" if sign > 0 else "bid"                                      # the side of the book this print depletes
        touch = view.best_ask if sign > 0 else view.best_bid
        if abs(price - touch) <= 1e-9 * max(1.0, abs(price)):
            status, depth, flick = factors(MAKER_FEED, side, ts)
            if status == "FEED_FAULT":
                counts["markout_feed_fault"] += 1
            elif flick is None:
                counts["markout_flicker_warmup"] += 1
            else:
                pend_m.append((ts + MARKOUT_S, {"ts": ts, "side": side, "price": price, "qty": vol, "status": status, "gap_depth": depth, "flicker": flick, "passive_buyer": sign < 0}))
    view.on_trade(price, vol, sign, ts)
    state.on_trade(ts, price, vol, sign)


def on_book(ev, ts):
    key = (ev.venue, ev.instrument_id)
    core = cores.get(key)
    if core is None:
        core = cores[key] = CoreBook(ev.venue, ev.instrument_id)
    fv = replay._feed_venue(ev.venue)
    view, state = views[fv], states[fv]
    before = core.stats.gaps
    core.apply(ev)
    if core.stats.gaps > before:
        state.note_feed_gap(ts)
        counts[f"feed_gaps_{fv}"] += 1
    if ev.event_type == EventType.BOOK_SNAPSHOT:
        bids, asks = [[float(l.price), float(l.quantity)] for l in ev.bids], [[float(l.price), float(l.quantity)] for l in ev.asks]
        if bids and asks:
            view.apply_snapshot(bids, asks, ts)
            state.on_book(view, ts)
    else:
        for side, levels in (("bid", ev.bids), ("ask", ev.asks)):
            for l in levels:
                view.apply_delta(side, float(l.price), float(l.quantity), "modify", ts)
        view.end_message()
        state.on_book(view, ts)


t0 = time.time()
first_ts = None
next_grid = None
n = 0
for ev in iter_recorded_events(SESSION):
    if isinstance(ev, DerivativeTicker):
        continue
    ts = ev.receive_ts_ns / 1e9
    if first_ts is None:
        first_ts, next_grid = ts, ts + 30.0                                      # the first 30 s are not sampled
    if minutes is not None and ts - first_ts > minutes * 60:
        break
    resolve(ts)                                                                  # outcomes due by now are read on the book as it stood BEFORE this event
    while next_grid is not None and next_grid <= ts:
        sample_grid(next_grid)
        next_grid += GRID_S
    if isinstance(ev, TradeEvent):
        on_print(ev, ts)
    elif ev.event_type in (EventType.BOOK_SNAPSHOT, EventType.BOOK_DELTA):
        on_book(ev, ts)
    n += 1
    if n % 500_000 == 0:
        print(f"{n} events, t={ts - first_ts:.0f}s, wall {time.time() - t0:.0f}s, rows {counts['slip_rows']}/{counts['markout_rows']}", flush=True)
rows.close()
meta = {"label": "EXPLORATORY: ONE DAY, PROXY (hypothetical hedge walks and public prints; no real fill, no simulator fill)", "capture": SESSION.name, "events": n, "wall_s": round(time.time() - t0, 1),
        "first_ts": first_ts, "last_ts": ts, "grid_s": GRID_S, "outcome_delta_s": DELTA_S, "markout_s": MARKOUT_S, "sizes": SIZES, "counts": dict(counts),
        "settings": {k: getattr(cfg.venue_state, k) for k in dir(cfg.venue_state) if k.startswith("dq_")}}
(OUT / "factor_meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True))
print(json.dumps(meta, indent=1))
