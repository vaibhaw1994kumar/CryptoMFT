"""Step G, T5 (a run wrapper; rules G_RULES.md): E's market/limit split of a HEDGE against the current hedge (all at market at the touch), on the real prints of Binance March 2024 (best bid/ask with sizes, prints). FEED ONLY, READ ONLY on data/.
usage: t5_split.py ratios DAY [DAY ...]      outflow-ratio samples from the prints of those days (TRAIN), per horizon T      -> t5/ratios.json
       t5_split.py run DAY [DAY ...]         the hedge simulation per window for every (T, lambda_u)                        -> t5/t5_DAY.npz
Fees are the v1_9 schedule (Binance taker 1.7 bps, maker 0), the hedge arrives 0.045 s after the decision, the limit part rests at the touch at the decision and fills by the depleting aggressor volume reaching the queue ahead plus its size,
the unfilled part is sent at market at T + 0.045 s. The touch is assumed to fit the market size (Binance March has no depth). EXPLORATORY, PROXY."""
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
import numpy as np
from crypto_stat_arb.ems.venue_selector import SplitVenue, split_single_venue
from new_backtest.event_loader import BOOK, TRADE, iter_venue_events, trade_fields
def _flag(name, default=None):
    """--name VALUE anywhere on the command line (removed from sys.argv); with none, the default (the run is then the G run, byte for byte)."""
    if name in sys.argv:
        i = sys.argv.index(name)
        v = sys.argv[i + 1]
        del sys.argv[i:i + 2]
        return v
    return default


OUT = Path("/Users/vaibhav/Documents/Newproject/out/integration/cost") / _flag("--out", "t5b"); OUT.mkdir(exist_ok=True)
PA = "--price-aware" in sys.argv                                                 # a resting limit order is filled only by prints AT its price (queue ahead first) or beyond it (its level is gone); the G run counted every print of the depleting side (default off: G byte for byte)
if PA:
    sys.argv.remove("--price-aware")
BIGV = 1e5                                                                       # a print beyond the rest price clears the level: counts as a huge depleting volume
RATIOS_PA = Path("/Users/vaibhav/Documents/Newproject/out/integration/cost/t5c/ratios_pa.json")
LAM_FILE = _flag("--lam-file")                                                  # LARGE_GAPS_RULES.md step 3: lambda_u per T from a lambdas.json group (sigma_1s sqrt(T), CVaR(5%) from random, CVaR(5%) from random + sharp moments) instead of the fixed grid; the cells are T x those three
LAM_GROUP = _flag("--lam-group", "TRAIN")
PESS = "--pessimistic" in sys.argv                                              # LARGE_GAPS_RULES.md step 5: the pessimistic floor for the rested part: only a print STRICTLY BEYOND the rest price fills (no credit for the queue ahead, none for prints at the price)
if PESS:
    sys.argv.remove("--pessimistic")
TIMES_FILE = _flag("--times")                                                    # FOLLOWUP_RULES.md item 2: hedge moments from a file {day: [[t, hedge_sell 1/0], ...]} instead of every 1 s
RATIOS = Path("/Users/vaibhav/Documents/Newproject/out/integration/cost/t5/ratios.json")              # built once from the TRAIN prints (the first T5 pass); the markout columns are the only change of this pass
DATA = "/Users/vaibhav/Documents/Newproject/data"
TS_, LAMS = (1.0, 2.0, 5.0), (1.0, 2.6, 5.0, 10.0)
SIZES, TAKER_BPS, MAKER_REBATE_BPS, ARR_S, WARM_S, WIN_S, MIN_V = (1.0, 10.0, 100.0), 1.7, 0.0, 0.045, 120.0, 10.0, 1.0
mode = sys.argv[1]


def load(day):
    t0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    bt, bb, ba, bbs, bas, tt, tp, tv, ts_ = [], [], [], [], [], [], [], [], []
    for ev in iter_venue_events(DATA, "binance", "SOLUSDT", day, day):
        if ev.exchange_ts > t0 + 7200 + 60:
            break
        if ev.kind == BOOK:
            p = ev.payload
            b, a = p.get("bids") or [], p.get("asks") or []
            if b and a:
                bt.append(ev.exchange_ts); bb.append(float(b[0][0])); ba.append(float(a[0][0])); bbs.append(float(b[0][1])); bas.append(float(a[0][1]))
        elif ev.kind == TRADE:
            pr, vol, sg = trade_fields(ev.payload)
            tt.append(ev.exchange_ts); tp.append(pr); tv.append(vol); ts_.append(sg)
    return t0, dict(bt=np.array(bt), bb=np.array(bb), ba=np.array(ba), bbs=np.array(bbs), bas=np.array(bas)), dict(t=np.array(tt), p=np.array(tp), v=np.array(tv), s=np.array(ts_))


def pa_contrib(v, p, rest, sell_rests):
    """The depleting volume of prints against a limit order resting at `rest`: a print AT the price counts its size, a print beyond it (a buy above a resting sell, a sell below a resting buy) means the level was cleared (BIGV), a print inside it counts 0.
    With --pessimistic only the prints beyond count."""
    eps = 1e-9 * abs(rest)
    beyond = (p > rest + eps) if sell_rests else (p < rest - eps)
    if PESS:
        return np.where(beyond, BIGV, 0.0)
    return np.where(beyond, BIGV, np.where(np.abs(p - rest) <= eps, v, 0.0))


def side_arrays(trd, sign):
    m = trd["s"] == sign
    return trd["p"][m], trd["v"][m]


def depleting(trd, sign):
    m = trd["s"] == sign
    t, v = trd["t"][m], trd["v"][m]
    return t, np.r_[0.0, np.cumsum(v)]


if mode == "ratios":
    out = {str(T): [] for T in TS_}
    for day in sys.argv[2:]:
        t0, bbo, trd = load(day)
        grid = np.arange(t0 + WARM_S, t0 + 7200 - max(TS_), 1.0)
        for sign in (1, -1):
            tt, cs = depleting(trd, sign)
            f = lambda a, b: cs[np.searchsorted(tt, b, "right")] - cs[np.searchsorted(tt, a, "right")]
            prev = np.maximum(f(grid - WIN_S, grid), MIN_V)
            if PA:
                ps, vs = side_arrays(trd, sign)
                bt_ = bbo["bt"]
                rest = (bbo["ba"] if sign > 0 else bbo["bb"])[np.maximum(np.searchsorted(bt_, grid, side="right") - 1, 0)]
                for T in TS_:
                    num = np.empty(len(grid))
                    for i, g in enumerate(grid):
                        a0, a1 = np.searchsorted(tt, g, "right"), np.searchsorted(tt, g + T, "right")
                        num[i] = pa_contrib(vs[a0:a1], ps[a0:a1], rest[i], sign > 0).sum()
                    out[str(T)] += list(np.sort(num / prev)[:: 3])
                continue
            for T in TS_:
                out[str(T)] += list(np.sort(f(grid, grid + T) / prev)[:: 3])
    out = {k: sorted(v) for k, v in out.items()}
    json.dump(out, open(RATIOS_PA if PA else RATIOS, "w"))
    print({k: len(v) for k, v in out.items()}, {k: [round(float(np.percentile(v, p)), 3) for p in (10, 50, 90)] for k, v in out.items()})
elif mode == "run":
    LAMT = {}
    if LAM_FILE:
        _lf = json.load(open(LAM_FILE))["lambdas"][LAM_GROUP]
        LAMT = {T: [_lf["sigma_sqrtT"][str(float(T))], _lf["cvar_b"][str(float(T))], _lf["cvar_bc"][str(float(T))]] for T in TS_}
    ratios = {float(k): tuple(v[:: max(1, len(v) // 2000)]) for k, v in json.load(open(RATIOS_PA if PA else RATIOS)).items()}      # about 2,000 samples per horizon (sorted)
    for day in sys.argv[2:]:
        w0 = time.time()
        t0, bbo, trd = load(day)
        bt = bbo["bt"]
        at = lambda arr, t: arr[np.maximum(np.searchsorted(bt, t, side="right") - 1, 0)]
        mid_at = lambda t: 0.5 * (at(bbo["bb"], t) + at(bbo["ba"], t))
        grid = np.arange(t0 + WARM_S, t0 + 7200 - 10.0, 1.0)
        sides_at = None
        if TIMES_FILE:
            mom = json.load(open(TIMES_FILE)).get(day, [])
            sides_at = {}
            for tm_, hs_ in mom:
                if t0 + WARM_S <= tm_ <= t0 + 7200 - 10.0:
                    sides_at.setdefault(tm_, set()).add(bool(hs_))
            grid = np.array(sorted(sides_at))
        cum = {s: depleting(trd, s) for s in (1, -1)}
        sidearr = {s: side_arrays(trd, s) for s in (1, -1)} if PA else None                  # +1: buy aggressors (deplete the asks), -1: sell aggressors (deplete the bids)
        rows = []
        for t in grid:
            bid, ask, bs, as_ = at(bbo["bb"], t), at(bbo["ba"], t), at(bbo["bbs"], t), at(bbo["bas"], t)
            if bid <= 0 or ask <= bid:
                continue
            mid = 0.5 * (bid + ask)
            half = (ask - bid) / 2 / mid * 1e4
            for hedge_sell in ((True, False) if sides_at is None else sorted(sides_at[t], reverse=True)):
                rest_price = ask if hedge_sell else bid
                q_ahead = as_ if hedge_sell else bs
                dsign = 1 if hedge_sell else -1                          # a sell hedge rests on the ask and is filled by BUY aggressors
                tt, cs = cum[dsign]
                vol = lambda a, b: cs[np.searchsorted(tt, b, "right")] - cs[np.searchsorted(tt, a, "right")]
                v_prev = max(vol(t - WIN_S, t), MIN_V)
                arr_bid, arr_ask = at(bbo["bb"], t + ARR_S), at(bbo["ba"], t + ARR_S)
                cost_m_unit = ((mid - arr_bid) if hedge_sell else (arr_ask - mid)) / mid * 1e4 + TAKER_BPS
                scaled = {T: tuple(r * v_prev for r in ratios[T]) for T in TS_}
                for S in SIZES:
                    rec = [t, 1.0 if hedge_sell else -1.0, S, cost_m_unit]
                    ff = []                                                      # (moments mode) the fill rate of the rested part per cell: filled / rested, NaN when nothing rests
                    for T in TS_:
                        samples = scaled[T]
                        late_bid, late_ask = at(bbo["bb"], t + T + ARR_S), at(bbo["ba"], t + T + ARR_S)
                        cost_late = ((mid - late_bid) if hedge_sell else (late_ask - mid)) / mid * 1e4 + TAKER_BPS
                        gain = ((mid - rest_price) if hedge_sell else (rest_price - mid)) / mid * 1e4 - MAKER_REBATE_BPS      # negative = gain
                        # fills over time: cumulative depleting volume since t, in the window (t, t + T]
                        a0, a1 = np.searchsorted(tt, t, "right"), np.searchsorted(tt, t + T, "right")
                        times = tt[a0:a1]
                        cumv = np.cumsum(pa_contrib(sidearr[dsign][1][a0:a1], sidearr[dsign][0][a0:a1], rest_price, hedge_sell)) if PA else cs[a0 + 1:a1 + 1] - cs[a0]
                        for lam in (LAMT[T] if LAM_FILE else LAMS):
                            d = split_single_venue(S, SplitVenue("BINANCE", True, q_ahead, samples, half, TAKER_BPS, MAKER_REBATE_BPS), lam)
                            L, M = d.limit.get("BINANCE", 0.0), d.market.get("BINANCE", 0.0)
                            if L > 1e-12 and len(cumv):
                                filled = min(max(cumv[-1] - q_ahead, 0.0), L)
                                # volume-weighted time of the filled units: increments of min(max(cumv - q, 0), L) over the prints
                                inc = np.diff(np.r_[0.0, np.minimum(np.maximum(cumv - q_ahead, 0.0), L)])
                                ftime = float((inc * (times - t)).sum() / inc.sum()) if inc.sum() > 0 else 0.0
                            else:
                                filled, ftime = 0.0, 0.0
                            unfilled = L - filled
                            cost = (M * cost_m_unit + filled * gain + unfilled * cost_late) / S
                            tm = (M * ARR_S + filled * ftime + unfilled * (T + ARR_S)) / S
                            # markout of the rested fill (reported apart; the hedge is flat after the fill so it is NOT part of the hedge cost): the side-signed move of the mid after the fill against the fill price,
                            # positive = the market moved the way that favours the resting order (a sell filled at the ask, the mid then fell). NaN without a fill.
                            if filled > 1e-12:
                                tf = t + ftime
                                mk = [((rest_price - mid_at(tf + h)) if hedge_sell else (mid_at(tf + h) - rest_price)) / mid * 1e4 for h in (1.0, 5.0)]
                            else:
                                mk = [float("nan"), float("nan")]
                            rec += [cost, tm, L / S, mk[0], mk[1]]
                            ff.append(filled / L if L > 1e-12 else float("nan"))
                    if sides_at is not None:                                     # appended AFTER the cells, so the cell columns keep their places: the fill rates, then the Binance mid move before the decision, side-signed, positive = against the hedge (bps)
                        pre = [((mid_at(t - h) - mid) if hedge_sell else (mid - mid_at(t - h))) / mid_at(t - h) * 1e4 for h in (1.0, 5.0)]
                        rec += ff + pre
                    rows.append(rec)
        a = np.array(rows, dtype=np.float64)
        np.savez_compressed(OUT / f"t5_{day}.npz", a=a, T=np.array(TS_), lam=np.array([LAMT[T] for T in TS_] if LAM_FILE else LAMS), sizes=np.array(SIZES))
        print(day, a.shape, "wall", round(time.time() - w0), "s", flush=True)


if mode == "times":
    # FOLLOWUP_RULES.md item 2: the hedge moments = the PROBE's simulated Bybit maker fill times (a 1 SOL bid, so the hedge is the SELL on Binance) of TRAIN 03-01..07 and SELECT 03-08..11 -> t5c/times.json. HOLDOUT has no probe fills (not run).
    B0 = Path("/Users/vaibhav/Documents/Newproject/out/integration/b0")
    out = {}
    for day in sys.argv[2:]:
        grp = "probe_adverse" if day <= "2024-03-07" else "probe/select"
        out[day] = [[float(r["ts"]), 1] for r in json.load(open(B0 / grp / f"{day}.json"))["rows"]]
    json.dump(out, open(OUT / "times.json", "w"))
    print({d: len(v) for d, v in out.items()}, "moments")


if mode == "worst":
    # LARGE_GAPS_RULES.md step 3: the worst-case cost of waiting. The side-signed move of the mid against an UNHEDGED position over h = 0.1 / 0.5 / 1 / 2 / 5 s after three kinds of moments: (a) the probe's SIMULATED maker-fill times (hedge = SELL on Binance),
    # (b) random prints (a 1 s grid, both hedge sides), (c) grid moments right after a sharp move (the Binance mid moved at least SHARP_BPS against the hedge in the previous 1 s). Per venue: Binance on every day (the exact tops), Bybit on the days with a 100 ms mid grid
    # (impact/data_*.npz: 03-01, 03-08, 03-11). Mean, p95, p99 and CVaR(5%) (the mean of the worst 5%), block-bootstrap intervals (10-minute blocks). usage: t5_split.py worst --out t5w
    HS_ = (0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0)
    SHARP_BPS = 3.0                                                                   # ASSUMED
    GRID = {"2024-03-01": "TRAIN", "2024-03-08": "SELECT", "2024-03-11": "SELECT"}
    DAYS = {"TRAIN": ["2024-03-0%d" % d for d in range(1, 8)], "SELECT": ["2024-03-08", "2024-03-09", "2024-03-10", "2024-03-11", "2024-03-12"]}
    times_all = json.load(open("/Users/vaibhav/Documents/Newproject/out/integration/cost/t5c/times.json"))
    rng_w = np.random.default_rng(1)
    store = {}                                                                        # (group, moment, venue, h) -> (values, blocks)

    def adv(mid_fn, t, hs, h):
        m0, m1 = mid_fn(t), mid_fn(t + h)
        return ((m0 - m1) if hs else (m1 - m0)) / m0 * 1e4

    def add(key, vals, blocks):
        v, b = store.setdefault(key, ([], []))
        v.append(np.asarray(vals)); b.append(np.asarray(blocks))

    for di, (group, days) in enumerate(DAYS.items()):
        for day in days:
            t0, bbo, trd = load(day)
            bt = bbo["bt"]
            mid_b = lambda t: 0.5 * (bbo["bb"][np.maximum(np.searchsorted(bt, t, side="right") - 1, 0)] + bbo["ba"][np.maximum(np.searchsorted(bt, t, side="right") - 1, 0)])
            z = np.load(f"/Users/vaibhav/Documents/Newproject/out/integration/cost/impact/data_{day}.npz") if day in GRID else None
            mid_y = (lambda t, g=z["bybit_t"], m=z["bybit_mid"]: m[np.clip(np.searchsorted(g, t, side="right") - 1, 0, len(g) - 1)]) if z is not None else None
            grid = np.arange(t0 + WARM_S, t0 + 7200 - max(HS_) - 1.0, 1.0)
            dayid = int(day[-2:]) * 1000
            moments = {}
            gb = mid_b(grid)
            pre = (mid_b(grid - 1.0) - gb) / gb * 1e4                                 # > 0: the mid FELL in the last second = against a long position (a SELL hedge)
            both = np.r_[grid, grid]; hs_both = np.r_[np.ones(len(grid), bool), np.zeros(len(grid), bool)]
            moments["random"] = (both, hs_both)
            sel_s, sel_b = pre >= SHARP_BPS, -pre >= SHARP_BPS                        # a fall is adverse to a sell hedge, a rise to a buy hedge
            moments["sharp"] = (np.r_[grid[sel_s], grid[sel_b]], np.r_[np.ones(sel_s.sum(), bool), np.zeros(sel_b.sum(), bool)])
            ft = np.array([x[0] for x in times_all.get(day, [])])
            ft = ft[(ft >= t0 + WARM_S) & (ft <= t0 + 7200 - max(HS_) - 1.0)]
            moments["fill_sim"] = (ft, np.ones(len(ft), bool))
            for mname, (tm, hs) in moments.items():
                if len(tm) == 0:
                    continue
                blocks = dayid + np.floor((tm - t0) / 600.0).astype(int)
                for h in HS_:
                    g0 = mid_b(tm); g1 = mid_b(tm + h)
                    add((group, mname, "binance", h), np.where(hs, g0 - g1, g1 - g0) / g0 * 1e4, blocks)
                    if mid_y is not None:
                        y0 = mid_y(tm); y1 = mid_y(tm + h)
                        add((group, mname, "bybit", h), np.where(hs, y0 - y1, y1 - y0) / y0 * 1e4, blocks)
            print(group, day, {k: len(v[0]) for k, v in moments.items()}, flush=True)

    # the 2026-10-29 capture, FIRST half (builds lambda_u for the large-size runs) and SECOND half (the answer): both venues' mids from the 100 ms grid (impact/data_capture_2026-10-29.npz)
    zc = np.load("/Users/vaibhav/Documents/Newproject/out/integration/cost/impact/data_capture_2026-10-29.npz")
    cz_ = np.load("/Users/vaibhav/Documents/Newproject/out/integration/cost/t1/capture_binance.npz")
    SPLIT_C = float((cz_["ts"].min() + cz_["ts"].max()) / 2.0)
    mc_b = lambda t, g=zc["binance_t"], m=zc["binance_mid"]: m[np.clip(np.searchsorted(g, t, side="right") - 1, 0, len(g) - 1)]
    mc_y = lambda t, g=zc["bybit_t"], m=zc["bybit_mid"]: m[np.clip(np.searchsorted(g, t, side="right") - 1, 0, len(g) - 1)]
    tc0, tc1 = max(zc["binance_t"][0], zc["bybit_t"][0]) + WARM_S, min(zc["binance_t"][-1], zc["bybit_t"][-1]) - max(HS_) - 1.0
    for gname, (a_, b_) in (("CAPFIRST", (tc0, SPLIT_C)), ("CAPSECOND", (SPLIT_C, tc1))):
        grid = np.arange(a_, b_, 1.0)
        gb = mc_b(grid)
        pre = (mc_b(grid - 1.0) - gb) / gb * 1e4
        mom_c = {"random": (np.r_[grid, grid], np.r_[np.ones(len(grid), bool), np.zeros(len(grid), bool)])}
        ss_, sb_ = pre >= SHARP_BPS, -pre >= SHARP_BPS
        mom_c["sharp"] = (np.r_[grid[ss_], grid[sb_]], np.r_[np.ones(ss_.sum(), bool), np.zeros(sb_.sum(), bool)])
        for mname, (tm, hs) in mom_c.items():
            blocks = (9000 if gname == "CAPFIRST" else 9500) + np.floor((tm - a_) / 600.0).astype(int)
            for h in HS_:
                for vname, fn in (("binance", mc_b), ("bybit", mc_y)):
                    m0, m1 = fn(tm), fn(tm + h)
                    add((gname, mname, vname, h), np.where(hs, m0 - m1, m1 - m0) / m0 * 1e4, blocks)
        print(gname, {k: len(v[0]) for k, v in mom_c.items()}, flush=True)

    def stats(x):
        n_ = len(x)
        k = max(1, int(round(0.05 * n_)))
        p95, p99 = np.percentile(x, [95, 99])
        return [float(x.mean()), float(p95), float(p99), float(np.partition(x, n_ - k)[n_ - k:].mean())]       # CVaR(5%) = the mean of the worst 5% (the largest adverse moves)

    def boot(x, b, reps=2000):
        ub, inv = np.unique(b, return_inverse=True)
        order = np.argsort(inv, kind="stable"); xs = x[order]; cnt = np.bincount(inv); off = np.r_[0, np.cumsum(cnt)]
        parts = [xs[off[i]:off[i + 1]] for i in range(len(ub))]
        res = []
        for _ in range(reps):
            pick = rng_w.integers(0, len(ub), len(ub))
            res.append(stats(np.concatenate([parts[i] for i in pick])))
        res = np.array(res)
        return [[float(np.percentile(res[:, j], 2.5)), float(np.percentile(res[:, j], 97.5))] for j in range(4)]

    out = {}
    for (group, mname, venue, h), (v, b) in sorted(store.items()):
        x, bl = np.concatenate(v), np.concatenate(b)
        if len(x) < 30:
            continue
        st = stats(x)
        ci = boot(x, bl, 500 if len(x) > 200000 else 2000)
        out[f"{group}|{mname}|{venue}|{h}"] = {"n": int(len(x)), "mean": st[0], "p95": st[1], "p99": st[2], "cvar5": st[3], "ci": {"mean": ci[0], "p95": ci[1], "p99": ci[2], "cvar5": ci[3]}}
        print(group, mname, venue, h, len(x), [round(a, 3) for a in st], flush=True)
    # lambda_u inputs from the TRAIN days' Binance moments: sigma_1s x sqrt(T) (sigma_1s of the 1 s mid changes, TRAIN) next to the CVaR(5%) of the adverse move over T from (b) random moments and from (b)+(c)
    # lambda_u candidates per group (Binance, the hedge venue): sigma_1s x sqrt(T) (sigma_1s = the std of the 1 s side-signed mid move over random moments), CVaR(5%) from the random moments, CVaR(5%) from random + sharp moments pooled
    lam = {}
    for group in ("TRAIN", "CAPFIRST"):
        k1 = (group, "random", "binance", 1.0)
        if k1 not in store:
            continue
        sig = float(np.concatenate(store[k1][0]).std())
        lam[group] = {"sigma_1s_bps": sig, "sigma_sqrtT": {}, "cvar_b": {}, "cvar_bc": {}}
        for h in HS_:
            xr = np.concatenate(store[(group, "random", "binance", h)][0])
            xs = np.concatenate(store[(group, "sharp", "binance", h)][0]) if (group, "sharp", "binance", h) in store else np.array([])
            lam[group]["sigma_sqrtT"][str(h)] = sig * h ** 0.5
            lam[group]["cvar_b"][str(h)] = stats(xr)[3]
            lam[group]["cvar_bc"][str(h)] = stats(np.concatenate([xr, xs]))[3]
    json.dump({"lambdas": lam}, open(OUT / "lambdas.json", "w"))
    json.dump({"results": out, "sharp_bps": SHARP_BPS, "horizons": HS_, "label": "SIMULATED fill times for fill_sim (the probe's maker fills); random and sharp are market data alone and are what sets lambda_u"}, open(OUT / "worst.json", "w"))
    sys.exit(0)


if mode in ("largeprep", "large", "large2"):
    # FOLLOWUP_RULES.md 1c: T5 at 100 and 500 SOL on the 2026-10-29 CAPTURE, Binance as the hedge venue, with the real Binance depth (the touch is NOT assumed to fit the size). Decisions every 5 s; chronological halves: the FIRST half builds the outflow samples and sigma_1s
    # (largeprep) and chooses, the SECOND half is the answer. Arms: M all at market; E (split_single_venue, theta 0; limit rests at the touch, filled by the depleting prints after the queue ahead, the rest at market at T + ARR); TS-AC (N market slices over H seconds,
    # Almgren-Chriss trajectory with kappa H in {0, 2}); own consumption = a VIRTUAL offset in the walk (the live book is never changed), REFILL tau 3.5 s (E: market part then the late remainder; slices: REFILL and OFF). EXPLORATORY PROXY.
    import heapq, math
    from bisect import bisect_right
    from crypto_stat_arb.derivatives import DerivativeTicker
    from crypto_stat_arb.models import EventType, TradeEvent
    from new_backtest.profiles import apply_profile, load_profile
    from new_backtest.session_replay import SessionReplay, dated_ticks, iter_recorded_events
    SESSION = Path("/Users/vaibhav/Documents/Newproject/data/live_sessions/solusdt/2026-10-29/SOLUSDT/e9b81728a3054812a48035a1b3e02d42")
    cz = np.load("/Users/vaibhav/Documents/Newproject/out/integration/cost/t1/capture_binance.npz")
    SPLIT_TS = float((cz["ts"].min() + cz["ts"].max()) / 2.0)
    PREP = OUT / "large_prep.json"
    SIZES_L, DEC_EVERY, TAU = (100.0, 500.0), 5.0, 3.5
    NS, HS, KAPS, OWN = (2, 5, 10), (5.0, 30.0), (0.0, 2.0), ("refill", "off")
    SL_CFGS = [(n, h, k) for n in NS for h in HS for k in KAPS]
    profile = load_profile("perp_perp_maker_taker_v1_9")
    cfg = apply_profile(profile, dataset="mar2024", data_root="", pair="SOLUSDT", start_date="2026-10-29", end_date="2026-10-29", output_dir="/tmp/x_t5l")
    replay = SessionReplay(cfg, profile, ticks=dated_ticks("2026-10-29"), recorded=True)
    view = replay.views["binance"]
    pt = {1: [], -1: []}; pc = {1: [0.0], -1: [0.0]}                             # print times and cumulative volume by aggressor sign (index 0 of pc is 0)
    pp = {1: [], -1: []}; pv = {1: [], -1: []}                                   # print prices and sizes (the fills of a resting order are PRICE-AWARE: prints at its price, or beyond it)
    last_px = []

    def vol(sign, a, b):
        ts_ = pt[sign]
        return pc[sign][bisect_right(ts_, b)] - pc[sign][bisect_right(ts_, a)]

    def trade_in(ev, ts):
        p, q = float(ev.price), float(ev.quantity)
        if p <= 0 or q <= 0:
            return None
        sg = 1 if ev.aggressor_side == "BUY" else -1
        pt[sg].append(ts); pc[sg].append(pc[sg][-1] + q); pp[sg].append(p); pv[sg].append(q); last_px.append((ts, p))
        return p, q, sg

if mode == "largeprep":
    # the first half of the capture: prints AND the book (the rest price at every 1 s grid time is the touch), so the outflow samples are PRICE-AWARE like the fills (a print at the price counts its size, a print beyond it clears the level)
    n_ev, grid_ts, grid_bid, grid_ask, nxt_g = 0, [], [], [], [None]
    for ev in iter_recorded_events(SESSION):
        if isinstance(ev, DerivativeTicker) or replay._feed_venue(ev.venue) != "binance":
            continue
        ts = ev.receive_ts_ns / 1e9
        if ts > SPLIT_TS:
            break
        if nxt_g[0] is None:
            nxt_g[0] = ts + WARM_S
        while ts >= nxt_g[0]:
            if view.ready and view.age_s(nxt_g[0]) <= 1.0 and view.best_bid > 0:
                grid_ts.append(nxt_g[0]); grid_bid.append(view.best_bid); grid_ask.append(view.best_ask)
            nxt_g[0] += 1.0
        if isinstance(ev, TradeEvent):
            r = trade_in(ev, ts)
            if r:
                view.on_trade(r[0], r[1], r[2], ts); n_ev += 1
        elif ev.event_type == EventType.BOOK_SNAPSHOT:
            b_, a_ = [[float(l.price), float(l.quantity)] for l in ev.bids], [[float(l.price), float(l.quantity)] for l in ev.asks]
            if b_ and a_:
                view.apply_snapshot(b_, a_, ts)
        elif ev.event_type == EventType.BOOK_DELTA:
            for sd, lv_ in (("bid", ev.bids), ("ask", ev.asks)):
                for l in lv_:
                    view.apply_delta(sd, float(l.price), float(l.quantity), "modify", ts)
            view.end_message()
    gts, gbid, gask = np.array(grid_ts), np.array(grid_bid), np.array(grid_ask)
    gts, gbid, gask = gts[gts < SPLIT_TS - max(TS_)], gbid[:len(gts[gts < SPLIT_TS - max(TS_)])], gask[:len(gts[gts < SPLIT_TS - max(TS_)])]
    out = {str(T): [] for T in TS_}
    for sign in (1, -1):
        tt_, pp_, pv_, cs_ = np.array(pt[sign]), np.array(pp[sign]), np.array(pv[sign]), np.array(pc[sign])
        rest = gask if sign > 0 else gbid
        prev = np.maximum(cs_[np.searchsorted(tt_, gts, "right")] - cs_[np.searchsorted(tt_, gts - WIN_S, "right")], MIN_V)
        for T in TS_:
            num = np.empty(len(gts))
            for i, g in enumerate(gts):
                a0, a1 = np.searchsorted(tt_, g, "right"), np.searchsorted(tt_, g + T, "right")
                num[i] = pa_contrib(pv_[a0:a1], pp_[a0:a1], rest[i], sign > 0).sum()
            out[str(T)] += list(np.sort(num / prev)[:: 3])
    out = {k: sorted(v) for k, v in out.items()}
    px_t = np.array([x[0] for x in last_px]); px_v = np.array([x[1] for x in last_px])
    t_first = float(min(pt[1][0], pt[-1][0]))
    sec = np.arange(t_first, SPLIT_TS, 1.0)
    px1 = px_v[np.maximum(np.searchsorted(px_t, sec, side="right") - 1, 0)]
    sigma = float(np.std(np.diff(np.log(px1))) * 1e4)
    json.dump({"ratios": out, "sigma_1s_bps": sigma, "split_ts": SPLIT_TS, "first_half_prints": n_ev, "t_first": t_first, "price_aware": True,
               "label": "first half of the capture (TRAIN of this test only): PRICE-AWARE outflow ratios (depleting volume at the touch price, or beyond it, over T / volume of that side over the previous 10 s) and sigma_1s of the last-trade price"}, open(PREP, "w"))
    print({k: len(v) for k, v in out.items()}, "sigma_1s", sigma, "prints", n_ev, "grid", len(gts))

if mode == "large":
    prep = json.load(open(PREP))
    ratios = {float(k): tuple(v[:: max(1, len(v) // 500)]) for k, v in prep["ratios"].items()}
    ASYM = lambda: None
    pend, cnt, rows, nxt, first = [], [0], [], [None], [None]
    counts = {"unfillable": 0, "decisions": 0}

    def walk_v(lv, q, removed):
        need, notional, used = q, 0.0, []
        for p, sz in lv:
            avail = sz - removed.get(p, 0.0) if removed else sz
            if avail <= 0:
                continue
            take = min(avail, need)
            notional += take * p; used.append((p, take)); need -= take
            if need <= 1e-12:
                return notional / q, used
        return None

    def removed_at(ledger, now):
        r = {}
        for ta, used in ledger:
            w = math.exp(-(now - ta) / TAU)
            for p, x in used:
                r[p] = r.get(p, 0.0) + x * w
        return r

    def unit_cost(vwap, mid, hedge_sell):
        return ((mid - vwap) if hedge_sell else (vwap - mid)) / mid * 1e4 + TAKER_BPS

    def risk_t(events, S):
        r, last, acc = S, 0.0, 0.0
        for tau, q in sorted(events):
            acc += (r / S) ** 2 * (tau - last); r -= q; last = tau
        return math.sqrt(acc)

    def push(t, kind, rec, arg=None):
        cnt[0] += 1
        heapq.heappush(pend, (t, cnt[0], kind, rec, arg))

    def decide(t):
        v = view
        if not v.ready or v.age_s(t) > 1.0:
            return
        bid, ask, bs, as_ = v.best_bid, v.best_ask, v.best_bid_size, v.best_ask_size
        if bid <= 0 or ask <= bid:
            return
        mid = 0.5 * (bid + ask)
        half = (ask - bid) / 2 / mid * 1e4
        for hedge_sell in (True, False):
            dsign = 1 if hedge_sell else -1                                       # a sell hedge rests on the ask and is filled by BUY aggressors
            rest_price, q_ahead = (ask, as_) if hedge_sell else (bid, bs)
            v_prev = max(vol(dsign, t - WIN_S, t), MIN_V)
            for S in SIZES_L:
                counts["decisions"] += 1
                rec = {"t": t, "hs": hedge_sell, "S": S, "mid": mid, "rest": rest_price, "qa": q_ahead, "dsign": dsign, "E": {}, "TS": {}, "M": None, "left": 0}
                # E cells
                tasks_late = {T: [] for T in TS_}
                for T in TS_:
                    samples = tuple(r * v_prev for r in ratios[T])
                    for lam in LAMS:
                        d = split_single_venue(S, SplitVenue("BINANCE", True, q_ahead, samples, half, TAKER_BPS, MAKER_REBATE_BPS), lam)
                        L = d.limit.get("BINANCE", 0.0)
                        rec["E"][(T, lam)] = {"L": L, "Me": S - L, "cost": None, "ledger": [], "done": False}
                        tasks_late[T].append((T, lam))
                rec["left"] += 1; push(t + ARR_S, "arr", rec)
                for T in TS_:
                    rec["left"] += 1; push(t + T + ARR_S, "late", rec, T)
                for (n, h, k) in SL_CFGS:
                    kh = k
                    xs = [S * (math.sinh(kh * (1 - i / n)) / math.sinh(kh) if kh > 0 else (1 - i / n)) for i in range(n + 1)]
                    qs = [xs[i] - xs[i + 1] for i in range(n)]
                    st = {"qs": qs, "own": {o: {"cost": 0.0, "ledger": [], "ok": True} for o in OWN}}
                    rec["TS"][(n, h, k)] = st
                    for i in range(n):
                        rec["left"] += 1; push(t + i * h / n + ARR_S, "slice", rec, ((n, h, k), i))

    def finish(rec):
        S = rec["S"]
        row = [rec["t"], 1.0 if rec["hs"] else -1.0, S, rec["M"]["cost"] if rec["M"] is not None else float("nan"), ARR_S, math.sqrt(ARR_S)]
        for T in TS_:
            for lam in LAMS:
                e = rec["E"][(T, lam)]
                row += e["res"] if e.get("res") else [float("nan")] * 5
        for key in SL_CFGS:
            n, h, k = key
            st = rec["TS"][key]
            vw = sum(q * (i * h / n + ARR_S) for i, q in enumerate(st["qs"])) / S
            rk = risk_t([(i * h / n + ARR_S, q) for i, q in enumerate(st["qs"])], S)
            for o in OWN:
                row += [st["own"][o]["cost"] / S if st["own"][o]["ok"] else float("nan"), vw, rk]
        rows.append(row)

    def process(t_task, kind, rec, arg):
        v = view
        hs, S, mid = rec["hs"], rec["S"], rec["mid"]
        side = "bid" if hs else "ask"
        lv = v.levels(side, 400) if v.ready else None
        if kind == "arr":
            res = walk_v(lv, S, None) if lv else None
            rec["M"] = {"cost": unit_cost(res[0], mid, hs)} if res else {"cost": float("nan")}
            if res is None:
                counts["unfillable"] += 1
            for key, e in rec["E"].items():
                if e["Me"] > 1e-12:
                    r2 = walk_v(lv, e["Me"], None) if lv else None
                    e["m"] = (unit_cost(r2[0], mid, hs), r2[1], t_task) if r2 else None
                else:
                    e["m"] = (0.0, [], t_task)
        elif kind == "late":
            T = arg
            tt, cs = pt[rec["dsign"]], pc[rec["dsign"]]
            a0, a1 = bisect_right(tt, rec["t"]), bisect_right(tt, rec["t"] + T)
            times = [x - rec["t"] for x in tt[a0:a1]]
            rest_, eps_, hs_ = rec["rest"], 1e-9 * abs(rec["rest"]), rec["hs"]
            cumv, run_ = [], 0.0
            for i in range(a0, a1):                                              # PRICE-AWARE: a print at the rest price counts its size, a print beyond it clears the level, a print inside it counts 0
                px_ = pp[rec["dsign"]][i]
                if (hs_ and px_ > rest_ + eps_) or ((not hs_) and px_ < rest_ - eps_):
                    run_ += BIGV
                elif abs(px_ - rest_) <= eps_:
                    run_ += pv[rec["dsign"]][i]
                cumv.append(run_)
            for (T2, lam), e in rec["E"].items():
                if T2 != T:
                    continue
                L, Me, qa = e["L"], e["Me"], rec["qa"]
                m = e.get("m")
                if m is None:
                    e["res"] = None
                    continue
                filled, events, ftime = 0.0, [], 0.0
                if L > 1e-12 and cumv:
                    prev = 0.0
                    inc_t = 0.0
                    for tm_, cv in zip(times, cumv):
                        cur = min(max(cv - qa, 0.0), L)
                        if cur > prev:
                            events.append((tm_, cur - prev)); inc_t += (cur - prev) * tm_
                            prev = cur
                    filled = prev
                    ftime = inc_t / filled if filled > 0 else 0.0
                unfilled = L - filled
                c_late, ok = 0.0, True
                if unfilled > 1e-9:
                    rm = removed_at([(m[2], m[1])], t_task) if Me > 1e-12 else {}
                    r3 = walk_v(lv, unfilled, rm) if lv else None
                    if r3 is None:
                        ok = False
                    else:
                        c_late = unit_cost(r3[0], mid, hs)
                if not ok:
                    e["res"] = None
                    continue
                gain = ((mid - rec["rest"]) if hs else (rec["rest"] - mid)) / mid * 1e4 - MAKER_REBATE_BPS
                cost = (Me * m[0] + filled * gain + unfilled * c_late) / S
                vw = (Me * ARR_S + filled * ftime + unfilled * (T + ARR_S)) / S
                ev_all = list(events) + ([(ARR_S, Me)] if Me > 1e-12 else []) + ([(T + ARR_S, unfilled)] if unfilled > 1e-9 else [])
                e["res"] = [cost, vw, risk_t(ev_all, S), L / S, filled / L if L > 1e-12 else float("nan")]
        else:
            key, i = arg
            st = rec["TS"][key]
            q = st["qs"][i]
            for o in OWN:
                s_ = st["own"][o]
                if not s_["ok"] or q <= 1e-12:
                    continue
                rm = removed_at(s_["ledger"], t_task) if o == "refill" else {}
                r2 = walk_v(lv, q, rm) if lv else None
                if r2 is None:
                    s_["ok"] = False
                    counts["unfillable"] += 1
                    continue
                s_["cost"] += q * unit_cost(r2[0], mid, hs)
                s_["ledger"].append((t_task, r2[1]))
        rec["left"] -= 1
        if rec["left"] == 0:
            finish(rec)

    def tick(ts):
        if first[0] is None:
            first[0], nxt[0] = ts, ts + WARM_S
        while pend and pend[0][0] <= ts:
            t_task, _, kind, rec, arg = heapq.heappop(pend)
            process(t_task, kind, rec, arg)
        while ts >= nxt[0]:
            decide(nxt[0]); nxt[0] += DEC_EVERY

    w0, n = time.time(), 0
    for ev in iter_recorded_events(SESSION):
        if isinstance(ev, DerivativeTicker):
            continue
        if replay._feed_venue(ev.venue) != "binance":
            continue
        ts = ev.receive_ts_ns / 1e9
        tick(ts)
        if isinstance(ev, TradeEvent):
            r = trade_in(ev, ts)
            if r:
                view.on_trade(r[0], r[1], r[2], ts)
        elif ev.event_type == EventType.BOOK_SNAPSHOT:
            b, a = [[float(l.price), float(l.quantity)] for l in ev.bids], [[float(l.price), float(l.quantity)] for l in ev.asks]
            if b and a:
                view.apply_snapshot(b, a, ts)
        elif ev.event_type == EventType.BOOK_DELTA:
            for sd, lv_ in (("bid", ev.bids), ("ask", ev.asks)):
                for l in lv_:
                    view.apply_delta(sd, float(l.price), float(l.quantity), "modify", ts)
            view.end_message()
        n += 1
        if n % 1_000_000 == 0:
            print(n, "events, wall", round(time.time() - w0), "s, rows", len(rows), flush=True)
    a = np.array(rows, dtype=np.float64)
    np.savez_compressed(OUT / "large_rows.npz", a=a, split_ts=SPLIT_TS, Ts=np.array(TS_), lams=np.array(LAMS), sl_cfgs=np.array(SL_CFGS), sizes=np.array(SIZES_L))
    print("rows", a.shape, "counts", counts, "wall", round(time.time() - w0), "s")


if mode == "large2":
    # LARGE_GAPS_RULES.md steps 4c and 5 on the 2026-10-29 CAPTURE (Binance hedge venue, real depth). Decisions every 5 s. FIRST half: S = 100 only, arms M and E (chooses the cell); SECOND half (the answer): S = 100 / 500 / 1000, arms
    #   M          all at market at the touch (single order: the overlay and own consumption cannot act)
    #   E          Cont-Kukanov split, T in {1, 2, 5} x lambda_u source {sigma_1s sqrt(T), CVaR(5%) from random moments, CVaR(5%) from random + sharp moments} (lambdas.json, group CAPFIRST), the rested part filled by the PRICE-AWARE rule ("pa") or the
    #              PESSIMISTIC floor ("pess": only a print STRICTLY beyond the rest price fills, no credit for the queue ahead or for prints at the price), overlay off / default / upper (pa), off (pess)
    #   P_<own>_<ov>  time-sliced (N slices over H s, Almgren-Chriss sinh trajectory, kappa H) plain slices, own consumption refill|off, overlay off|default|upper
    #   L_refill_<ov> the same slices each sized by rule 1 (fill range at survival x 0.8 / 1 / 1.2, less our unrefilled take, IOC capped at 10 bps; the carry goes to the next slice; the last slice sweeps)
    # Own consumption = core OwnTakeLedger (tau 3.5 s ASSUMED), overlay = core ImpactOverlay (MODELLED, whole ticks), rule 1 = core fill_range_totals. Per arm: cost (bps per unit), volume-weighted unhedged seconds, risk time, realised adverse drift
    # (the side-signed mid move at the execution times, volume-weighted, bps), extra (E: rested share and fill rate). Decisions are tagged with the Binance mid move of the previous 1 s and 5 s (side-signed, positive = against the hedge). EXPLORATORY PROXY.
    import heapq, math
    from bisect import bisect_right
    from crypto_stat_arb.derivatives import DerivativeTicker
    from crypto_stat_arb.models import EventType, TradeEvent
    from crypto_stat_arb.simulation.impact import ImpactConfig, ImpactOverlay, OwnTakeLedger, fill_range_totals
    CST = Path("/Users/vaibhav/Documents/Newproject/out/integration/cost")
    SMOKE = "--smoke" in sys.argv                                                   # a test run: every decision gets every arm (even in the first half) and the run stops after --max-events events
    if SMOKE:
        sys.argv.remove("--smoke")
    MAXEV = int(_flag("--max-events", "0"))
    lamf = json.load(open(_flag("--lambdas", str(CST / "t5w/lambdas.json"))))["lambdas"]["CAPFIRST"]
    LSRC = {"sigma": lamf["sigma_sqrtT"], "cvar_b": lamf["cvar_b"], "cvar_bc": lamf["cvar_bc"]}
    prep = json.load(open(PREP))
    ratios = {float(k): tuple(v[:: max(1, len(v) // 500)]) for k, v in prep["ratios"].items()}
    SZ2, DEC2, TAU2, CAPB, SK = (100.0, 500.0, 1000.0), 5.0, 3.5, 10.0, 0.0
    TICK = replay.ticks["binance"]
    SURV = json.load(open(CST / "t1b/extra_CAPTURE_BINANCE.json"))["survival_mean_per_sample"]["mean"]
    OVC = {"off": None, "default": ImpactConfig(**json.load(open(CST / "impact_overlay_settings.json"))), "upper": ImpactConfig(**json.load(open(CST / "impact_overlay_settings_upper.json")))}
    SLC2 = [(5, 5.0, 0.0), (10, 5.0, 0.0), (5, 30.0, 0.0), (10, 30.0, 0.0), (10, 30.0, 2.0)]
    PVAR = [("refill", "off"), ("refill", "default"), ("refill", "upper"), ("off", "off")]
    LVAR = [("refill", "off"), ("refill", "default"), ("refill", "upper")]
    SRCS = ("sigma", "cvar_b", "cvar_bc")
    mh_t, mh_m, nxt_m = [], [], [0.0]
    pend, cnt, recs, nxt, first = [], [0], [], [None], [None]
    counts = {"unfillable": 0, "decisions": 0}
    cache = {}

    def mid_hist(t):
        i = bisect_right(mh_t, t) - 1
        return mh_m[i] if i >= 0 else float("nan")

    def adv(rec, t):
        m = mid_hist(t)
        return ((rec["mid"] - m) if rec["hs"] else (m - rec["mid"])) / rec["mid"] * 1e4

    def walk2(lv, q, rm, dp, limit, hs):
        need, notional, used = q, 0.0, []
        for p, sz in lv:
            pe = p + dp
            if limit is not None and ((pe < limit) if hs else (pe > limit)):
                break
            avail = sz - (rm.get(p, 0.0) if rm else 0.0)
            if avail <= 0:
                continue
            take = min(avail, need)
            notional += take * pe; used.append((p, take)); need -= take
            if need <= 1e-12:
                break
        f = q - need
        return (f, notional / f, used) if f > 1e-12 else None

    def unit_cost(vwap, mid, hs):
        return ((mid - vwap) if hs else (vwap - mid)) / mid * 1e4 + TAKER_BPS

    def risk_t(events, S):
        r, last, acc = S, 0.0, 0.0
        for tau, q in sorted(events):
            acc += (r / S) ** 2 * (tau - last); r -= q; last = tau
        return math.sqrt(acc)

    def new_state(own, ov):
        return {"led": OwnTakeLedger(TAU2) if own == "refill" else None, "ov": ImpactOverlay(OVC[ov], {"binance": TICK}) if OVC[ov] is not None else None, "cost": 0.0, "ev": [], "drift": 0.0, "ok": True, "carry": 0.0}

    def chunk(st, rec, q, t_task, lv, limit_bps=None):
        hs, mid = rec["hs"], rec["mid"]
        rm = st["led"].missing_by_price("bid" if hs else "ask", t_task) if st["led"] is not None else None
        dp = st["ov"].shift_ticks("binance", t_task, mid) * TICK if st["ov"] is not None else 0.0
        limit = None
        if limit_bps is not None and lv:
            te = lv[0][0] + dp
            limit = te * (1 - limit_bps / 1e4) if hs else te * (1 + limit_bps / 1e4)
        r = walk2(lv, q, rm, dp, limit, hs)
        if r is None:
            return None
        f, vw, used = r
        if st["led"] is not None:
            for p, x in used:
                st["led"].record("bid" if hs else "ask", p, x, t_task)
        if st["ov"] is not None:
            st["ov"].add_order("binance", t_task, "sell" if hs else "buy", f)
        st["ev"].append((t_task - rec["t"], f))
        st["drift"] += f * adv(rec, t_task)
        return f, unit_cost(vw, mid, hs)

    def push(t, kind, rec, arg=None):
        cnt[0] += 1
        heapq.heappush(pend, (t, cnt[0], kind, rec, arg))

    def decide(t):
        v = view
        if not v.ready or v.age_s(t) > 1.0:
            return
        bid, ask, bs, as_ = v.best_bid, v.best_ask, v.best_bid_size, v.best_ask_size
        if bid <= 0 or ask <= bid:
            return
        mid = 0.5 * (bid + ask)
        half = (ask - bid) / 2 / mid * 1e4
        is_first = t < SPLIT_TS and not SMOKE
        for hedge_sell in (True, False):
            dsign = 1 if hedge_sell else -1
            rest_price, q_ahead = (ask, as_) if hedge_sell else (bid, bs)
            v_prev = max(vol(dsign, t - WIN_S, t), MIN_V)
            pre = [((mid_hist(t - h) - mid) if hedge_sell else (mid - mid_hist(t - h))) / mid * 1e4 for h in (1.0, 5.0)]
            for S in ((100.0,) if is_first else SZ2):
                counts["decisions"] += 1
                rec = {"t": t, "hs": hedge_sell, "S": S, "mid": mid, "rest": rest_price, "qa": q_ahead, "dsign": dsign, "pre": pre, "left": 0, "arms": {}, "E": {}, "M": None, "TS": {}}
                for T in TS_:
                    samples = tuple(r * v_prev for r in ratios[T])
                    for src in SRCS:
                        d = split_single_venue(S, SplitVenue("BINANCE", True, q_ahead, samples, half, TAKER_BPS, MAKER_REBATE_BPS), LSRC[src][str(float(T))])
                        L = d.limit.get("BINANCE", 0.0)
                        for rule, ov in [("pa", o) for o in OVC] + [("pess", "off")]:
                            rec["E"][(T, src, rule, ov)] = {"st": new_state("refill", ov), "L": L, "Me": S - L, "m": None, "ok": True}
                rec["left"] += 1; push(t + ARR_S, "arr", rec)
                for T in TS_:
                    rec["left"] += 1; push(t + T + ARR_S, "late", rec, T)
                if not is_first:
                    for (n, h, k) in SLC2:
                        xs = [S * (math.sinh(k * (1 - i / n)) / math.sinh(k) if k > 0 else (1 - i / n)) for i in range(n + 1)]
                        qs = [xs[i] - xs[i + 1] for i in range(n)]
                        sts = {("P", o, ov): new_state(o, ov) for o, ov in PVAR}
                        sts.update({("L", o, ov): new_state(o, ov) for o, ov in LVAR})
                        rec["TS"][(n, h, k)] = {"qs": qs, "sts": sts}
                        for i in range(n):
                            rec["left"] += 1; push(t + i * h / n + ARR_S, "slice", rec, ((n, h, k), i))

    def levels_of(side, key):
        ck = (key, side)
        if ck not in cache:
            cache[ck] = view.levels(side, 400) if view.ready else None
        return cache[ck]

    def finish(rec):
        S, t = rec["S"], rec["t"]
        a = rec["arms"]
        if rec["M"] is not None:
            a["M"] = (rec["M"], ARR_S, math.sqrt(ARR_S), rec["Mdrift"], float("nan"), float("nan"))
        for (T, src, rule, ov), e in rec["E"].items():
            if e.get("res") is not None:
                a[f"E_T{T:g}_{src}_{rule}_{ov}"] = e["res"]
        for key, tsd in rec["TS"].items():
            n, h, k = key
            for (kind, o, ov), st in tsd["sts"].items():
                if not st["ok"] or abs(sum(q for _, q in st["ev"]) - S) > 1e-6 * S:
                    continue
                vw = sum(q * tt for tt, q in st["ev"]) / S
                a[f"{kind}_n{n}_H{h:g}_k{k:g}_{o}_{ov}"] = (st["cost"] / S, vw, risk_t(st["ev"], S), st["drift"] / S, st["carry"], float("nan"))
        recs.append((t, 1.0 if rec["hs"] else -1.0, S, rec["pre"][0], rec["pre"][1], a))

    def process(t_task, kind, rec, arg, key):
        hs, S, mid = rec["hs"], rec["S"], rec["mid"]
        side = "bid" if hs else "ask"
        lv = levels_of(side, key)
        if kind == "arr":
            r = walk2(lv, S, None, 0.0, None, hs) if lv else None
            if r is None or r[0] < S - 1e-9:
                counts["unfillable"] += 1
                rec["M"] = float("nan"); rec["Mdrift"] = float("nan")
            else:
                rec["M"] = unit_cost(r[1], mid, hs); rec["Mdrift"] = adv(rec, t_task)
            for k_, e in rec["E"].items():
                if e["Me"] > 1e-12:
                    e["m"] = chunk(e["st"], rec, e["Me"], t_task, lv) if lv else None
                    e["ok"] = e["m"] is not None and e["m"][0] >= e["Me"] - 1e-9
                else:
                    e["m"] = (0.0, 0.0)
        elif kind == "late":
            T = arg
            tt, dsg = pt[rec["dsign"]], rec["dsign"]
            a0, a1 = bisect_right(tt, rec["t"]), bisect_right(tt, rec["t"] + T)
            times = [x - rec["t"] for x in tt[a0:a1]]
            rest_, eps_ = rec["rest"], 1e-9 * abs(rec["rest"])
            cums = {"pa": [], "pess": []}
            run = {"pa": 0.0, "pess": 0.0}
            for i in range(a0, a1):
                px_ = pp[dsg][i]
                beyond = (hs and px_ > rest_ + eps_) or ((not hs) and px_ < rest_ - eps_)
                if beyond:
                    run["pa"] += BIGV; run["pess"] += BIGV
                elif abs(px_ - rest_) <= eps_:
                    run["pa"] += pv[dsg][i]
                cums["pa"].append(run["pa"]); cums["pess"].append(run["pess"])
            for (T2, src, rule, ov), e in rec["E"].items():
                if T2 != T:
                    continue
                if not e["ok"]:
                    e["res"] = None
                    continue
                L, Me, qa, st = e["L"], e["Me"], rec["qa"], e["st"]
                filled, events, prev = 0.0, [], 0.0
                if L > 1e-12 and cums[rule]:
                    for tm_, cv in zip(times, cums[rule]):
                        cur = min(max(cv - qa, 0.0), L)
                        if cur > prev:
                            events.append((tm_, cur - prev)); prev = cur
                    filled = prev
                unfilled = L - filled
                c_late = 0.0
                if unfilled > 1e-9:
                    r2 = chunk(st, rec, unfilled, t_task, lv) if lv else None
                    if r2 is None or r2[0] < unfilled - 1e-9:
                        e["res"] = None
                        continue
                    c_late = r2[1]
                gain = ((mid - rec["rest"]) if hs else (rec["rest"] - mid)) / mid * 1e4 - MAKER_REBATE_BPS
                cost = (Me * (e["m"][1] if Me > 1e-12 else 0.0) + filled * gain + unfilled * c_late) / S
                ev_all = [(ARR_S, Me)] * (Me > 1e-12) + events + [(T + ARR_S, unfilled)] * (unfilled > 1e-9)
                vw = sum(q * tm for tm, q in ev_all) / S
                drift = (st["drift"] + sum(q * adv(rec, rec["t"] + tm) for tm, q in events)) / S         # market chunks were added by chunk(); the rested fills are added here
                e["res"] = (cost, vw, risk_t(ev_all, S), drift, L / S, filled / L if L > 1e-12 else float("nan"))
        else:
            key2, i = arg
            tsd = rec["TS"][key2]
            q = tsd["qs"][i]
            n_ = key2[0]
            for (kd, o, ov), st in tsd["sts"].items():
                if not st["ok"] or q <= 1e-12 and st["carry"] <= 1e-12:
                    continue
                if kd == "P":
                    r2 = chunk(st, rec, q, t_task, lv) if lv else None
                    if r2 is None or r2[0] < q - 1e-9:
                        st["ok"] = False; counts["unfillable"] += 1
                        continue
                    st["cost"] += r2[0] * r2[1]
                else:
                    want = q + st["carry"]
                    if i == n_ - 1:
                        r2 = chunk(st, rec, want, t_task, lv) if lv else None
                        if r2 is None or r2[0] < want - 1e-9:
                            st["ok"] = False; counts["unfillable"] += 1
                            continue
                        st["cost"] += r2[0] * r2[1]; st["carry"] = 0.0
                    else:
                        te = lv[0][0] if lv else 0.0
                        lim = te * (1 - CAPB / 1e4) if hs else te * (1 + CAPB / 1e4)
                        D = sum(sz for p, sz in lv if (p >= lim if hs else p <= lim)) if lv else 0.0
                        miss = sum(x for p, x in st["led"].missing_by_price(side, t_task).items() if (p >= lim if hs else p <= lim))
                        fr = fill_range_totals(D, min(1.0, SURV), min(miss, D), 0.8, 1.2)
                        send = min(want, fr.low)
                        r2 = chunk(st, rec, send, t_task, lv, CAPB) if send > 1e-12 and lv else None
                        if r2 is None:
                            st["carry"] = want
                        else:
                            st["cost"] += r2[0] * r2[1]; st["carry"] = want - r2[0]
        rec["left"] -= 1
        if rec["left"] == 0:
            finish(rec)

    ev_n = [0]
    def tick(ts):
        if first[0] is None:
            first[0], nxt[0] = ts, ts + WARM_S
        key = ev_n[0]
        while pend and pend[0][0] <= ts:
            t_task, _, kind, rec, arg = heapq.heappop(pend)
            process(t_task, kind, rec, arg, key)
        while ts >= nxt[0]:
            decide(nxt[0]); nxt[0] += DEC2

    w0, n = time.time(), 0
    for ev in iter_recorded_events(SESSION):
        if isinstance(ev, DerivativeTicker) or replay._feed_venue(ev.venue) != "binance":
            continue
        ts = ev.receive_ts_ns / 1e9
        ev_n[0] += 1
        cache.clear()
        tick(ts)
        if view.ready and ts >= nxt_m[0]:
            mh_t.append(ts); mh_m.append(0.5 * (view.best_bid + view.best_ask)); nxt_m[0] = ts + 0.1
        if isinstance(ev, TradeEvent):
            r = trade_in(ev, ts)
            if r:
                view.on_trade(r[0], r[1], r[2], ts)
        elif ev.event_type == EventType.BOOK_SNAPSHOT:
            b, a = [[float(l.price), float(l.quantity)] for l in ev.bids], [[float(l.price), float(l.quantity)] for l in ev.asks]
            if b and a:
                view.apply_snapshot(b, a, ts)
        elif ev.event_type == EventType.BOOK_DELTA:
            for sd, lv_ in (("bid", ev.bids), ("ask", ev.asks)):
                for l in lv_:
                    view.apply_delta(sd, float(l.price), float(l.quantity), "modify", ts)
            view.end_message()
        n += 1
        if MAXEV and n >= MAXEV:
            break
        if n % 1_000_000 == 0:
            print(n, "events, wall", round(time.time() - w0), "s, records", len(recs), flush=True)
    names = sorted({k for r in recs for k in r[5]})
    A = np.full((len(recs), len(names), 6), np.nan, dtype=np.float32)
    ix = {k: i for i, k in enumerate(names)}
    for j, r in enumerate(recs):
        for k, v in r[5].items():
            A[j, ix[k]] = v
    base = np.array([[r[0], r[1], r[2], r[3], r[4]] for r in recs])
    np.savez_compressed(OUT / "large2_rows.npz", base=base, arms=A, names=np.array(names), split_ts=SPLIT_TS, label="t,hedge_sell,S,pre1,pre5 | arms: cost,vw_time,risk_time,drift,extra1,extra2")
    print("records", len(recs), "arms", len(names), "counts", counts, "wall", round(time.time() - w0), "s")
