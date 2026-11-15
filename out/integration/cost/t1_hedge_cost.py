"""Step G, T1 (a run wrapper; rules: G_RULES.md and, for the flags below, FOLLOWUP_RULES.md): hypothetical market-style hedge orders on Bybit, every 1 s, sizes as fractions of the displayed depth within 10 bps; the cost predicted at the decision
(displayed depth, effective depth with the measured survival and x0.8 / x1.2, the cliff flag) and the cost of a walk of the book 0.1 s later. FEED ONLY, READ ONLY on data/. EXPLORATORY.
usage: t1_hedge_cost.py [flags] march DAY [DAY ...]  |  t1_hedge_cost.py [flags] capture  |  t1_hedge_cost.py [flags] archive COIN DAY HOUR    (a 2 h window of the owner's Bybit linear-perp ob200 archive in ~/Downloads/bybit_data, read in place)
flags (all optional; with none the run is the G run, byte for byte):
  --fracs a,b,c     the fractions of D10 (default 0.05,0.1,0.25,0.5,1.0)
  --out DIR         output directory name under out/integration/cost (default t1)
  --max-levels N    levels kept by the book view (default 200, the view's own default)
  --extra           also save, per row: the book mid, the number of book updates between the decision and the arrival (nupd), the cancel-to-trade of the consumed side, and the gap flag as 3 TICKS; the 2 / 3 / 5 bps flags are then
                    run with dq_gap_min_ticks = 1 (a price distance alone; identical to the default on SOL, where 3 bps is 40+ ticks); a {name}_aux.npz holds the unfillable counts per fraction
  --seq             also run the repeated-hedge sequences of FOLLOWUP_RULES.md 1b ({name}_seq.npz): the own-consumption reductions are a virtual offset in the walk, the live book and VenueState are never changed"""
import bisect, csv, gzip, heapq, json, math, sys, time, zipfile
from datetime import datetime, timezone
from pathlib import Path
SRC = "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src"
sys.path.insert(0, SRC)
import numpy as np
from crypto_stat_arb.simulation.impact import DisplayedDepthImpact
from crypto_stat_arb.simulation.venue_state import VenueState, VenueStateConfig

argv = sys.argv[1:]


def popflag(name, default=None, has_value=True):
    if name in argv:
        i = argv.index(name)
        if not has_value:
            del argv[i]
            return True
        v = argv[i + 1]
        del argv[i:i + 2]
        return v
    return default


FRACS = tuple(float(x) for x in popflag("--fracs", "0.05,0.1,0.25,0.5,1.0").split(","))
OUT = Path("/Users/vaibhav/Documents/Newproject/out/integration/cost") / popflag("--out", "t1"); OUT.mkdir(exist_ok=True)
MAXLV = int(popflag("--max-levels", "200"))
EXTRA = popflag("--extra", False, False)
SEQ = popflag("--seq", False, False)
NODEC = popflag("--no-decisions", False, False)                                                # sequences only: the 1 s T1 decisions are skipped (the rows file is empty)
SEQ_OV = popflag("--seq-ov", False, False)                                                      # LARGE_GAPS_RULES.md step 4b: also the core ImpactOverlay variants of the sequences (MODELLED; default isolated-run settings and the all-runs upper bound; a continuous shift in bps, not whole ticks)
DELTA_S, WARM_S, MULTS, GAPS = 0.1, 120.0, (0.8, 1.0, 1.2), (2.0, 3.0, 5.0)
SEQ_F, SEQ_S, SEQ_K, SEQ_EVERY, SEQ_TAU, SEQ_VARIANTS = (0.1, 0.5, 1.0), (1.0, 2.0, 5.0, 10.0), 10, 60.0, 3.5, ("off", "refill", "never")
if SEQ_OV:
    SEQ_VARIANTS = SEQ_VARIANTS + ("refill_ovd", "refill_ovu", "off_ovd", "off_ovu")
    import json as _json
    from crypto_stat_arb.simulation.impact import ImpactConfig, ImpactOverlay
    _OVC = {"ovd": ImpactConfig(**_json.load(open("/Users/vaibhav/Documents/Newproject/out/integration/cost/impact_overlay_settings.json"))), "ovu": ImpactConfig(**_json.load(open("/Users/vaibhav/Documents/Newproject/out/integration/cost/impact_overlay_settings_upper.json")))}
L1 = DisplayedDepthImpact()
mode = argv[0]


def walk_virtual(lv, q, removed):
    """Walk q over the levels (best first) with `removed` (price -> size taken earlier by OUR hedges, still out of the book) subtracted from each level; (vwap, [(price, taken)]) or None when the book cannot fill it."""
    need, notional, used = q, 0.0, []
    for p, sz in lv:
        avail = sz - removed.get(p, 0.0) if removed else sz
        if avail <= 0:
            continue
        take = min(avail, need)
        notional += take * p
        used.append((p, take))
        need -= take
        if need <= 1e-12:
            return notional / q, used
    return None


class Sampler:
    def __init__(self, view, state, tag):
        self.view, self.state, self.tag = view, state, tag
        self.rows, self.pending, self.nxt, self.first = [], [], None, None
        self.counts = {"unfillable_pred_disp": 0, "unfillable_at_arrival": 0, "no_survival": 0, "seq_off_mismatch": 0}
        self.nup = 0
        self.dec_f, self.unf_pred_f, self.unf_arr_f = {f: 0 for f in FRACS}, {f: 0 for f in FRACS}, {f: 0 for f in FRACS}
        self.tasks, self.tcount, self.next_seq, self.seq_n, self.seqrows = [], 0, None, 0, []

    def decide(self, t):
        v = self.view
        if not v.ready or v.age_s(t) > 1.0:
            return
        for side in ("ask", "bid"):
            dq = self.state.depth_quality(v, t, side)
            sv = dq["survival"][side]
            if sv is None:
                self.counts["no_survival"] += 1
                continue
            lv = v.levels(side, 400)
            if not lv:
                continue
            touch = lv[0][0]
            lim = touch * (1 + 10 / 1e4) if side == "ask" else touch * (1 - 10 / 1e4)
            d10 = sum(s for p, s in lv if (p <= lim if side == "ask" else p >= lim))
            gap = dq["gap"][side]
            gaps = {}
            cfg = self.state.config
            default_ticks = cfg.dq_gap_min_ticks
            if EXTRA:
                cfg.dq_gap_min_ticks = 1                                                    # the 2 / 3 / 5 bps flags as a price distance alone
            for gm in GAPS:                                                          # the gap run length as a price distance: 2, 3 (ASSUMED default) and 5 bps
                cfg.dq_gap_min_bps = gm
                gaps[gm] = self.state.gap_depth(v, side, t)
            if EXTRA:
                cfg.dq_gap_min_ticks = default_ticks
                cfg.dq_gap_min_bps = 0.0
                g3t = self.state.gap_depth(v, side, t)                               # the tick rule alone: m = dq_gap_min_ticks (3) ticks
                fl3t = g3t["status"] == "GAP" and g3t["depth"] is not None
            cfg.dq_gap_min_bps = 3.0
            mid = 0.5 * (v.best_bid + v.best_ask) if EXTRA else None
            c2t = dq["cancel_to_trade"][side] if EXTRA else None
            for f in FRACS:
                q = f * d10
                if q <= 0:
                    continue
                self.dec_f[f] += 1
                p0 = L1.vwap_cost_bps(lv, q, 1.0)
                if p0 is None:
                    self.counts["unfillable_pred_disp"] += 1
                    self.unf_pred_f[f] += 1
                    continue
                peff, cover = [], []
                for m in MULTS:
                    se = min(1.0, sv["mean"] * m)
                    peff.append(L1.vwap_cost_bps(lv, q, se) if se > 0 else None)
                    cover.append(d10 * se >= q)
                fl = [gaps[gm]["status"] == "GAP" and gaps[gm]["depth"] is not None and gaps[gm]["depth"] < q for gm in GAPS]
                if EXTRA:
                    fl3 = fl3t and g3t["depth"] < q
                self.pending.append((t + DELTA_S, dict(ts=t, side=side, f=f, q=q, touch=touch, s=sv["mean"], flag=fl[1], fl2=fl[0], fl5=fl[2], gapd=gap["depth"] if gap["depth"] is not None else np.nan, d10=d10, p0=p0, peff=peff, cover=cover,
                                                        mid=mid, nup0=self.nup, c2t=c2t, fl3t=fl3 if EXTRA else None)))

    def resolve(self, t):
        v = self.view
        while self.pending and self.pending[0][0] <= t:
            r = self.pending.pop(0)[1]
            w = v.walk(r["side"], r["q"]) if v.ready else None
            if w is None or w.filled_qty < r["q"] - 1e-9 or w.exhausted:
                self.counts["unfillable_at_arrival"] += 1
                self.unf_arr_f[r["f"]] += 1
                continue
            real = ((w.vwap - r["touch"]) if r["side"] == "ask" else (r["touch"] - w.vwap)) / r["touch"] * 1e4
            row = (r["ts"], 1 if r["side"] == "ask" else -1, r["f"], r["q"], r["s"], r["flag"], r["gapd"], r["d10"], r["p0"],
                   *(np.nan if x is None else x for x in r["peff"]), *r["cover"], real, r["fl2"], r["fl5"])
            if EXTRA:
                row += (r["mid"], self.nup - r["nup0"], np.nan if r["c2t"] is None else r["c2t"], r["fl3t"])
            self.rows.append(row)

    # ---- repeated-hedge sequences (FOLLOWUP_RULES.md 1b) ----
    def seq_start(self, t):
        v = self.view
        if not v.ready or v.age_s(t) > 1.0:
            return
        side = "ask" if self.seq_n % 2 == 0 else "bid"
        self.seq_n += 1
        lv = v.levels(side, 400)
        if not lv:
            return
        touch = lv[0][0]
        lim = touch * (1 + 10 / 1e4) if side == "ask" else touch * (1 - 10 / 1e4)
        d10 = sum(s for p, s in lv if (p <= lim if side == "ask" else p >= lim))
        if d10 <= 0:
            return
        for f in SEQ_F:
            for s in SEQ_S:
                seq = dict(t0=t, side=side, f=f, s=s, q=f * d10, d10=d10, ledger={vr: [] for vr in SEQ_VARIANTS}, touch={}, ov={vr: ImpactOverlay(_OVC[vr.split("_")[1]], {"x": 0.01}) for vr in SEQ_VARIANTS if "_" in vr} if SEQ_OV else {})
                self.tcount += 1
                heapq.heappush(self.tasks, (t, self.tcount, "dec", seq, 0))

    def seq_tasks(self, ts):
        v = self.view
        while self.tasks and self.tasks[0][0] <= ts:
            tt, _, kind, seq, k = heapq.heappop(self.tasks)
            side = seq["side"]
            if kind == "dec":
                lv1 = v.levels(side, 1) if v.ready else None
                if lv1:
                    seq["touch"][k] = lv1[0][0]
                    self.tcount += 1
                    heapq.heappush(self.tasks, (tt + DELTA_S, self.tcount, "arr", seq, k))
                if k + 1 < SEQ_K:
                    self.tcount += 1
                    heapq.heappush(self.tasks, (seq["t0"] + (k + 1) * seq["s"], self.tcount, "dec", seq, k + 1))
            else:
                lv = v.levels(side, 400) if v.ready else None
                touch = seq["touch"].get(k)
                if not lv or touch is None:
                    continue
                costs = []
                for vr in SEQ_VARIANTS:
                    removed = {}
                    bvr = vr.split("_")[0]                                         # off | refill | never (the overlay variants add a price shift on top)
                    for ta, taken in seq["ledger"][vr]:
                        wgt = 1.0 if bvr == "never" else (math.exp(-(tt - ta) / SEQ_TAU) if bvr == "refill" else 0.0)
                        if wgt > 0:
                            for p, x in taken:
                                removed[p] = removed.get(p, 0.0) + x * wgt
                    if bvr == "off":
                        removed = {}
                    res = walk_virtual(lv, seq["q"], removed)
                    if res is None:
                        costs.append(np.nan)
                        continue
                    vwap, used = res
                    if vr in seq["ov"]:                                            # MODELLED: the later prices of the venue are shifted by our earlier hedges of this sequence (core ImpactOverlay, continuous bps)
                        vwap *= 1.0 + seq["ov"][vr].shift_bps("x", tt) / 1e4
                        seq["ov"][vr].add_order("x", tt, "buy" if side == "ask" else "sell", seq["q"])
                    costs.append(((vwap - touch) if side == "ask" else (touch - vwap)) / touch * 1e4)
                    if vr != "off":
                        seq["ledger"][vr].append((tt, used))
                    else:
                        seq["ledger"][vr].append((tt, used))                    # kept for symmetry; "off" never reads it
                p0 = L1.vwap_cost_bps(lv, seq["q"], 1.0)
                if (p0 is None) != (not np.isfinite(costs[0])) or (p0 is not None and abs(abs(p0) - abs(costs[0])) > 1e-6 * max(1.0, abs(p0)) and abs(lv[0][0] - touch) < 1e-12):
                    self.counts["seq_off_mismatch"] += 1                       # the OFF walk must equal the single-order walk whenever the touch has not moved
                self.seqrows.append((seq["t0"], 1 if side == "ask" else -1, seq["f"], seq["s"], k, seq["q"], seq["d10"], *costs))

    def tick(self, ts):
        if self.first is None:
            self.first, self.nxt = ts, ts + WARM_S
            self.next_seq = ts + WARM_S
        self.resolve(ts)
        while ts >= self.nxt:
            if not NODEC:
                self.decide(self.nxt)
            self.nxt += 1.0
        if SEQ:
            while ts >= self.next_seq:
                self.seq_start(self.next_seq)
                self.next_seq += SEQ_EVERY
            self.seq_tasks(ts)

    def save(self, name):
        cols = ["ts", "side", "f", "q", "s", "flag", "gap_depth", "d10", "p_disp", "p_eff08", "p_eff10", "p_eff12", "cover08", "cover10", "cover12", "real", "flag2", "flag5"]
        if EXTRA:
            cols += ["mid", "nupd", "c2t", "flag3t"]
        a = np.array(self.rows, dtype=float).reshape(-1, len(cols))
        np.savez_compressed(OUT / f"{name}.npz", **{c: a[:, i] for i, c in enumerate(cols)}, counts=np.array([self.counts["unfillable_pred_disp"], self.counts["unfillable_at_arrival"], self.counts["no_survival"]]))
        aux = {}
        if EXTRA:
            aux.update(fracs=np.array(FRACS), dec=np.array([self.dec_f[f] for f in FRACS]), unf_pred=np.array([self.unf_pred_f[f] for f in FRACS]), unf_arr=np.array([self.unf_arr_f[f] for f in FRACS]))
        if aux:
            np.savez_compressed(OUT / f"{name}_aux.npz", **aux)
        if SEQ:
            sc = ["t0", "side", "f", "s", "k", "q", "d10"] + ["cost_" + v for v in SEQ_VARIANTS]
            sa = np.array(self.seqrows, dtype=float).reshape(-1, len(sc))
            np.savez_compressed(OUT / f"{name}_seq.npz", **{c: sa[:, i] for i, c in enumerate(sc)})
        print(name, len(a), "rows", self.counts, "seq rows", len(self.seqrows), flush=True)


def run_march(days):
    from new_backtest.event_loader import BOOK, DELTA, TRADE, delta_fields, iter_venue_events, snapshot_levels, trade_fields
    from new_backtest.order_book import OrderBook
    from new_backtest.session_replay import dated_ticks
    for day in days:
        t0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        view = OrderBook("bybit", "SOLUSDT", "hybrid", max_levels=MAXLV)
        state = VenueState("bybit", VenueStateConfig(depth_quality=True, dq_gap_min_bps=3.0))
        state.set_tick(dated_ticks(day)["bybit"])
        S = Sampler(view, state, day)
        w0 = time.time()
        for ev in iter_venue_events("/Users/vaibhav/Documents/Newproject/data", "bybit", "SOLUSDT", day, day):
            ts = ev.exchange_ts
            if ts > t0 + 7200:
                break
            S.tick(ts)
            if ev.kind == BOOK:
                b, a = snapshot_levels(ev.payload)
                if b and a:
                    view.apply_snapshot(b, a, ts); state.on_book(view, ts); S.nup += 1
            elif ev.kind == DELTA:
                side, price, vol, et = delta_fields(ev.payload)
                view.apply_delta(side, price, vol, et, ts); state.on_book(view, ts); S.nup += 1
            elif ev.kind == TRADE:
                p, vq, sg = trade_fields(ev.payload)
                state.on_trade(ts, p, vq, sg)
        S.save(f"march_{day}")
        print(day, "wall", round(time.time() - w0), "s", flush=True)


def run_capture():
    from crypto_stat_arb.derivatives import DerivativeTicker
    from crypto_stat_arb.models import EventType, TradeEvent
    from new_backtest.profiles import apply_profile, load_profile
    from new_backtest.session_replay import SessionReplay, dated_ticks, iter_recorded_events
    SESSION = Path("/Users/vaibhav/Documents/Newproject/data/live_sessions/solusdt/2026-10-29/SOLUSDT/e9b81728a3054812a48035a1b3e02d42")
    profile = load_profile("perp_perp_maker_taker_v1_9")
    cfg = apply_profile(profile, dataset="mar2024", data_root="", pair="SOLUSDT", start_date="2026-10-29", end_date="2026-10-29", output_dir="/tmp/x_t1")
    cfg.venue_state.depth_quality = True
    cfg.venue_state.dq_gap_min_bps = 3.0                                      # the run length m as a price distance (3 bps), not 3 ticks: see tests/test_depth_quality.py GapMinBps
    replay = SessionReplay(cfg, profile, ticks=dated_ticks("2026-10-29"), recorded=True)
    for v, st in replay.states.items():
        st.set_tick(replay.ticks[v])
    S = {v: Sampler(replay.views[v], replay.states[v], v) for v in ("bybit", "binance")}
    for ev in iter_recorded_events(SESSION):
        if isinstance(ev, DerivativeTicker):
            continue
        ts = ev.receive_ts_ns / 1e9
        for smp in S.values():
            smp.tick(ts)
        fv = replay._feed_venue(ev.venue); view, state = replay.views[fv], replay.states[fv]
        if isinstance(ev, TradeEvent):
            p, q = float(ev.price), float(ev.quantity)
            if p > 0 and q > 0:
                sg = 1 if ev.aggressor_side == "BUY" else -1
                view.on_trade(p, q, sg, ts); state.on_trade(ts, p, q, sg)
        elif ev.event_type == EventType.BOOK_SNAPSHOT:
            b, a = [[float(l.price), float(l.quantity)] for l in ev.bids], [[float(l.price), float(l.quantity)] for l in ev.asks]
            if b and a:
                view.apply_snapshot(b, a, ts); state.on_book(view, ts); S[fv].nup += 1
        elif ev.event_type == EventType.BOOK_DELTA:
            for sd, lv in (("bid", ev.bids), ("ask", ev.asks)):
                for l in lv:
                    view.apply_delta(sd, float(l.price), float(l.quantity), "modify", ts)
            view.end_message(); state.on_book(view, ts); S[fv].nup += 1
    for v, smp in S.items():
        smp.save(f"capture_{v}")


def run_archive(coin, day, hour):
    """A 2 h window of the Bybit linear-perp public archive (ob200 snapshot+delta zip, trade csv.gz), read in place from ~/Downloads/bybit_data. Time = the matching-engine time (cts for the book, the csv timestamp for prints). The book is rebuilt from the
    start of the day (deltas are absolute sizes) and the sampler starts at the window start."""
    from new_backtest.order_book import OrderBook
    root = Path.home() / "Downloads" / "bybit_data"
    t_start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() + hour * 3600
    t_end = t_start + 7200
    trades = []
    with gzip.open(root / "trades" / coin / f"{coin}{day}.csv.gz", "rt", newline="") as fh:
        for r in csv.DictReader(fh):
            tt = float(r["timestamp"])
            if t_start <= tt <= t_end:
                trades.append((tt, float(r["price"]), float(r["size"]), 1 if r["side"] == "Buy" else -1))
    trades.sort(key=lambda x: x[0])
    view = OrderBook("bybit", coin, "hybrid", max_levels=MAXLV)
    state = VenueState("bybit", VenueStateConfig(depth_quality=True, dq_gap_min_bps=3.0))
    S = Sampler(view, state, f"{coin}_{day}_h{hour:02d}")
    tp, in_window, tick_set, w0 = 0, False, False, time.time()
    with zipfile.ZipFile(root / "orderbook" / coin / f"{day}_{coin}_ob200.data.zip") as z, z.open(z.namelist()[0]) as fh:
        for line in fh:
            m = json.loads(line)
            d = m["data"]
            bt = m.get("cts", m["ts"]) / 1e3
            if bt > t_end:
                break
            if not in_window and bt >= t_start:
                in_window = True
            if in_window:
                while tp < len(trades) and trades[tp][0] <= bt:
                    tt, p, vq, sg = trades[tp]; tp += 1
                    S.tick(tt); state.on_trade(tt, p, vq, sg)
                S.tick(bt)
            if m["type"] == "snapshot":
                b = [[float(p), float(s)] for p, s in d["b"]]
                a = [[float(p), float(s)] for p, s in d["a"]]
                if b and a:
                    if not tick_set:
                        px = sorted({x[0] for x in b})
                        tick = min(round(y - x, 10) for x, y in zip(px, px[1:]) if y - x > 0)
                        state.set_tick(tick); tick_set = True
                    view.apply_snapshot(b, a, bt)
                    if in_window:
                        state.on_book(view, bt); S.nup += 1
            else:
                for side, key in (("bid", "b"), ("ask", "a")):
                    for p, s in d.get(key, []):
                        sz = float(s)
                        view.apply_delta(side, float(p), sz, "delete" if sz == 0 else "modify", bt)
                view.end_message()
                if in_window:
                    state.on_book(view, bt); S.nup += 1
    S.save(f"archive_{coin}_{day}_h{hour:02d}")
    print(coin, day, hour, "tick", state.tick if hasattr(state, "tick") else "?", "trades", len(trades), "wall", round(time.time() - w0), "s", flush=True)


if mode == "march":
    run_march(argv[1:])
elif mode == "capture":
    run_capture()
elif mode == "archive":
    run_archive(argv[1], argv[2], int(argv[3]))
else:
    raise SystemExit(__doc__)
