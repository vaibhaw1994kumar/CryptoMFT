"""Impact fits step 1 (a run wrapper, feed only, READ ONLY on data/ and out/queue_v2/slices_mar2024): per window, the mid and the depth within k bps of the touch of each venue on a 100 ms grid, and every print.
usage: impact_prep.py march 2024-03-01|2024-03-08|2024-03-11      (Bybit: book from its events, hybrid deltas; Binance: best bid/ask only, no depth)
       impact_prep.py capture                                      (2026-10-29, both venues L2)
Output: out/integration/cost/impact/data_<window>.npz"""
import itertools, sys, time
from pathlib import Path
SRC = "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src"
sys.path.insert(0, SRC)
import numpy as np
OUT = Path("/Users/vaibhav/Documents/Newproject/out/integration/cost/impact"); OUT.mkdir(parents=True, exist_ok=True)
GRID, KS = 0.1, (2.0, 5.0, 10.0)
mode = sys.argv[1]


def depth_all(book, side):
    """The size within each k of KS bps of the touch, from the best 80 levels (10 bps is about 10 levels on SOL)."""
    lv = book.levels(side, 80)
    if not lv:
        return [float("nan")] * len(KS)
    touch = lv[0][0]
    out = []
    for k in KS:
        lim = touch * (1 + k / 1e4) if side == "ask" else touch * (1 - k / 1e4)
        out.append(sum(sz for p, sz in lv if (p <= lim if side == "ask" else p >= lim)))
    return out


class Sampler:
    def __init__(self):
        self.t, self.mid, self.dbid, self.dask = [], [], [], []
        self.tr_t, self.tr_p, self.tr_v, self.tr_s, self.tr_xt = [], [], [], [], []
        self.nxt = None

    def tick(self, ts, book_ready, mid, bid_d, ask_d):
        if self.nxt is None:
            self.nxt = ts
        while ts >= self.nxt:
            if book_ready:
                self.t.append(self.nxt); self.mid.append(mid()); self.dbid.append(bid_d()); self.dask.append(ask_d())
            self.nxt += GRID

    def arrays(self):
        return dict(t=np.array(self.t), mid=np.array(self.mid), dbid=np.array(self.dbid).reshape(-1, len(KS)), dask=np.array(self.dask).reshape(-1, len(KS)),
                    tr_t=np.array(self.tr_t), tr_xt=np.array(self.tr_xt if self.tr_xt else self.tr_t), tr_p=np.array(self.tr_p), tr_v=np.array(self.tr_v), tr_s=np.array(self.tr_s))


t0 = time.time()
res = {}
if mode == "march":
    from new_backtest.event_loader import BOOK, DELTA, TRADE, delta_fields, iter_venue_events, snapshot_levels, trade_fields
    from new_backtest.order_book import OrderBook
    day = sys.argv[2]
    root = "/Users/vaibhav/Documents/Newproject/out/queue_v2/slices_mar2024"
    for venue in ("bybit", "binance"):
        S = Sampler()
        book = OrderBook(venue, "SOLUSDT", "hybrid" if venue == "bybit" else "absolute")
        bbo = {"bid": 0.0, "ask": 0.0}
        for ev in iter_venue_events(root, venue, "SOLUSDT", day, day):
            ts = ev.exchange_ts
            if venue == "bybit":
                S.tick(ts, book.ready, lambda: book.mid, lambda: depth_all(book, "bid"), lambda: depth_all(book, "ask"))
            else:
                S.tick(ts, bbo["bid"] > 0 and bbo["ask"] > 0, lambda: 0.5 * (bbo["bid"] + bbo["ask"]), lambda: [float("nan")] * len(KS), lambda: [float("nan")] * len(KS))
            if ev.kind == BOOK:
                if venue == "bybit":
                    b, a = snapshot_levels(ev.payload)
                    if b and a:
                        book.apply_snapshot(b, a, ts)
                else:
                    p = ev.payload
                    bbo["bid"], bbo["ask"] = float(p.get("best_bid") or 0.0), float(p.get("best_ask") or 0.0)
            elif ev.kind == DELTA and venue == "bybit":
                side, price, vol, et = delta_fields(ev.payload)
                book.apply_delta(side, price, vol, et, ts)
            elif ev.kind == TRADE:
                p, v, s = trade_fields(ev.payload)
                S.tr_t.append(ts); S.tr_xt.append(ts); S.tr_p.append(p); S.tr_v.append(v); S.tr_s.append(s)
        res[venue] = S.arrays()
        print(venue, len(S.t), "grid points", len(S.tr_t), "prints", round(time.time() - t0), "s", flush=True)
    name = day
else:
    from crypto_stat_arb.derivatives import DerivativeTicker
    from crypto_stat_arb.models import EventType, TradeEvent
    from new_backtest.profiles import apply_profile, load_profile
    from new_backtest.session_replay import SessionReplay, dated_ticks, iter_recorded_events
    SESSION = Path("/Users/vaibhav/Documents/Newproject/data/live_sessions/solusdt/2026-10-29/SOLUSDT/e9b81728a3054812a48035a1b3e02d42")
    profile = load_profile("perp_perp_maker_taker_v1_9")
    cfg = apply_profile(profile, dataset="mar2024", data_root="", pair="SOLUSDT", start_date="2026-10-29", end_date="2026-10-29", output_dir="/tmp/x_ip")
    replay = SessionReplay(cfg, profile, ticks=dated_ticks("2026-10-29"), recorded=True)
    views = replay.views
    S = {v: Sampler() for v in ("bybit", "binance")}
    n = 0
    for ev in iter_recorded_events(SESSION):
        if isinstance(ev, DerivativeTicker):
            continue
        ts = ev.receive_ts_ns / 1e9
        for fv, smp in S.items():
            v = views[fv]
            smp.tick(ts, v.ready, lambda v=v: v.mid, lambda v=v: depth_all(v, "bid"), lambda v=v: depth_all(v, "ask"))
        fv = replay._feed_venue(ev.venue); view = views[fv]
        if isinstance(ev, TradeEvent):
            p, q = float(ev.price), float(ev.quantity)
            if p > 0 and q > 0:
                sign = 1 if ev.aggressor_side == "BUY" else -1
                view.on_trade(p, q, sign, ts)
                S[fv].tr_t.append(ts); S[fv].tr_xt.append(ev.exchange_ts_ns / 1e9); S[fv].tr_p.append(p); S[fv].tr_v.append(q); S[fv].tr_s.append(sign)
        elif ev.event_type == EventType.BOOK_SNAPSHOT:
            b, a = [[float(l.price), float(l.quantity)] for l in ev.bids], [[float(l.price), float(l.quantity)] for l in ev.asks]
            if b and a:
                view.apply_snapshot(b, a, ts)
        elif ev.event_type == EventType.BOOK_DELTA:
            for side, lv in (("bid", ev.bids), ("ask", ev.asks)):
                for l in lv:
                    view.apply_delta(side, float(l.price), float(l.quantity), "modify", ts)
            view.end_message()
        n += 1
        if n % 1_000_000 == 0:
            print(n, "events", round(time.time() - t0), "s", flush=True)
    res = {v: S[v].arrays() for v in S}
    name = "capture_2026-10-29"
flat = {f"{v}_{k}": a for v, d in res.items() for k, a in d.items()}
np.savez_compressed(OUT / f"data_{name}.npz", **flat)
print("saved", name, {k: a.shape for k, a in flat.items() if k.endswith(("_t", "_mid"))}, round(time.time() - t0), "s")
