"""Oct-2024, BYBIT_ONLY_WINDOW: simulated Bybit maker orders (the ORDERS are hypothetical, the book and the prints are real) pushed through every queue model in ONE pass.
usage: oct_bybit_maker_sim.py DAY   -> out/queue_v2/oct_sim/<DAY>.json     EXPLORATORY. Nothing here produces strategy P&L; no Binance book is used (the Binance trade tape is a PROXY price series).
Orders: every GRID seconds a 1-lot bid at the best bid and a 1-lot ask at the best ask join the back of the displayed queue (arrival = the first event 5 ms after the decision),
live for LIFE seconds, then are cancelled. At t+CHECK s a live order records its queue position, near/opposite queue size and the Bybit imbalance (Albers features)."""
import array, bisect, json, os, sys, time, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from new_backtest.backtester import _infer_tick
from new_backtest.event_loader import BOOK, DELTA, TRADE, delta_fields, iter_venue_events, snapshot_levels, trade_fields
from new_backtest.fill_engine import MakerConfig, MakerExecutor
from new_backtest.fill_model import EmpiricalFillModel
from new_backtest.models import LambdaBucket, Side
from new_backtest.order_book import OrderBook

DATA = "/Users/vaibhav/Documents/Newproject/data"
OUT = "/Users/vaibhav/Documents/Newproject/out/queue_v2/oct_sim"
GRID, LIFE, CHECK, SIZE, LAT, WARM = 90.0, 60.0, 5.0, 1.0, 0.005, 120.0
FEE_BPS = 0.0
COMMON = {"snapshot_trade_explain": True, "fill_evidence": "off"}
MODELS = {
    "min_baseline": {"cancel_model": "all_ahead"}, "behind_first": {"cancel_model": "behind_first"}, "proportional": {"cancel_model": "proportional"},
    "ahead_first": {"cancel_model": "ahead_first"}, "power_0.5": {"cancel_model": "power", "cancel_power_n": 0.5}, "power_2.0": {"cancel_model": "power", "cancel_power_n": 2.0},
    "cohort_youngest_first": {"queue_model": "cohort", "cohort_age": "youngest_first"}, "cohort_oldest_first": {"queue_model": "cohort", "cohort_age": "oldest_first"},
    "cohort_uniform": {"queue_model": "cohort", "cohort_age": "uniform"}, "cohort_hazard_0.5": {"queue_model": "cohort", "cohort_age": "hazard", "cohort_hazard_theta": 0.5},
}


class State:
    top_depth = 100.0; top_depth_baseline = 100.0; spread_pct = 0.001; short_vol = 0.0; recent_trade_rate = 0.0; trade_sign = 0.0


def binance_prints(day):
    ts, px = array.array("d"), array.array("d")
    for ev in iter_venue_events(DATA, "binance", "SOLUSDT", day, day, include_snapshots=False, include_deltas=False):
        ts.append(ev.exchange_ts); px.append(float(ev.payload["price"]))
    return ts, px


def last_at(ts, px, t):
    j = bisect.bisect_right(ts, t) - 1
    return px[j] if j >= 0 else None


def main(day):
    t_wall = time.time()
    ex = {m: MakerExecutor(EmpiricalFillModel(), MakerConfig(**{**COMMON, **kw})) for m, kw in MODELS.items()}
    book = OrderBook("bybit", "SOLUSDT")
    tick = 0.0
    orders = []            # one dict per hypothetical order (same set for every model)
    live = {}              # (model, maker order id) -> order dict
    mids_t, mids_m = array.array("d"), array.array("d")
    next_dec, t_first, t_last, n_ev = None, None, None, 0
    pending = []           # decisions waiting for arrival
    next_scan = 0.0
    tick_obs = []
    man = json.load(open('/Users/vaibhav/Documents/Newproject/out/queue_v2/manifest.json'))['days'][f'bybit/SOLUSDT/{day}']['channels']
    t_stop = man['book_events']['last_ts'] + 0.5          # the real Bybit book hours of this day (manifest); nothing after it is replayed
    bin_t, bin_p = binance_prints(day)
    for ev in iter_venue_events(DATA, "bybit", "SOLUSDT", day, day):
        ts = ev.exchange_ts
        if ts > t_stop: break
        t_first = t_first or ts
        t_last = ts
        n_ev += 1
        fills = []
        if ev.kind == BOOK:
            bids, asks = snapshot_levels(ev.payload)
            book.apply_snapshot(bids, asks, ts)
            tick_obs.append(_infer_tick(bids, asks))                 # the first snapshot of a day can be sparse (tick x2): use the smallest gap over the first 200 snapshots
            if len(tick_obs) <= 200:
                tick = round(min(tick_obs), 6)
            if book.ready:
                mids_t.append(ts); mids_m.append(book.mid)
            for m, e in ex.items():
                fills += [(m, f) for f in e.on_snapshot("bybit", book, ts, tick or 0.01, FEE_BPS, event_ts=ts)]
        elif ev.kind == DELTA:
            side, price, volume, et = delta_fields(ev.payload)
            if price <= 0: continue
            book.apply_delta(side, price, volume, et, ts)
            ns = book.level_size(side, price)
            for m, e in ex.items():
                fills += [(m, f) for f in e.on_delta("bybit", side, price, ns, ts, tick or 0.01, FEE_BPS, event_ts=ts)]
        elif ev.kind == TRADE:
            price, volume, sign = trade_fields(ev.payload)
            if price <= 0 or volume <= 0: continue
            book.on_trade(price, volume, sign, ts)
            for m, e in ex.items():
                fills += [(m, f) for f in e.on_trade("bybit", price, volume, sign, ts, FEE_BPS, event_ts=ts)]
        for m, f in fills:
            o = live.get((m, f.order_id))
            if o is not None:
                r = o["models"][m]
                r["fills"].append((ts, f.qty))
        if not book.ready or not tick:
            continue
        if next_dec is None:
            next_dec = t_first + WARM
        while ts >= next_dec:                                   # decisions on a fixed grid, identical for every model
            pending.append(next_dec)
            next_dec += GRID
        while pending and ts >= pending[0] + LAT:
            td = pending.pop(0)
            bb, ba = book.best_bid, book.best_ask
            bs, as_ = book.level_size("bid", bb), book.level_size("ask", ba)
            imb = (bs - as_) / (bs + as_) if (bs + as_) > 0 else 0.0
            bl, al = book.levels("bid", 3), book.levels("ask", 3)
            specs = []
            if ba - bb > tick * 1.5:                             # an improving quote exists only when the spread is wider than one tick
                specs += [(Side.BID, "improve", round(bb + tick, 8), 0.0, as_), (Side.ASK, "improve", round(ba - tick, 8), 0.0, bs)]
            specs += [(Side.BID, "touch", bb, bs, as_), (Side.ASK, "touch", ba, as_, bs)]
            if len(bl) > 1 and len(al) > 1:
                specs += [(Side.BID, "second", bl[1][0], bl[1][1], as_), (Side.ASK, "second", al[1][0], al[1][1], bs)]
            for side, kind, px, near, far in specs:
                o = {"id": len(orders), "kind": kind, "t_decision": td, "t_arrival": ts, "side": "buy" if side == Side.BID else "sell", "price": px, "near_size": near,
                     "opposite_size": far, "imbalance": imb, "mid": book.mid, "models": {}}
                for m, e in ex.items():
                    mo = e.place("bybit", "SOLUSDT", "SOLUSDT", side, px, SIZE, book, State(), LambdaBucket.CALM, ts, ts, tick, signal_id=o["id"], arrival_ts=ts)
                    o["models"][m] = {"maker_id": mo.order_id, "fills": [], "check": None, "cancelled": False, "queue_ahead0": mo.queue_ahead}
                    live[(m, mo.order_id)] = o
                o["rel0"] = (o["models"]["proportional"]["queue_ahead0"] / near) if near > 0 else 0.0
                orders.append(o)
        # checkpoint and expiry, scanned every 0.25 s of event time (check/cancel times are therefore good to 0.25 s)
        if ts >= next_scan:
            next_scan = ts + 0.25
            for (m, mid_), o in list(live.items()):
                r = o["models"][m]
                e = ex[m]
                mo = e.orders.get(mid_)
                if r["check"] is None and ts >= o["t_arrival"] + CHECK:
                    if mo is not None:
                        lvl = mo.level_now if mo.level_now > 0 else None
                        r["check"] = {"ahead": mo.queue_ahead, "level": mo.level_now, "rel": (mo.queue_ahead / lvl) if lvl else None, "filled": mo.filled_qty}
                    else:
                        r["check"] = {"gone": True}
                if mo is None:
                    del live[(m, mid_)]                          # fully filled (the executor dropped it)
                elif ts >= o["t_arrival"] + LIFE:
                    e.cancel(mid_, ts)
                    r["cancelled"] = True
                    del live[(m, mid_)]
    # markouts / drift for the proportional model's first fills (PROXY series for Binance), and per-model fill summaries
    def mid_at(t):
        j = bisect.bisect_right(mids_t, t) - 1
        return mids_m[j] if j >= 0 else None
    res_orders = []
    for o in orders:
        row = {k: o[k] for k in ("id", "kind", "rel0", "t_decision", "t_arrival", "side", "price", "near_size", "opposite_size", "imbalance", "mid")}
        row["models"] = {}
        for m, r in o["models"].items():
            fl = [(t_, x) for t_, x in r["fills"] if t_ <= o["t_arrival"] + LIFE]
            q = sum(x for _, x in fl)
            first = fl[0][0] if fl else None
            row["models"][m] = {"filled": q, "first_fill_dt": (first - o["t_arrival"]) if first else None, "check": r["check"]}
        r = o["models"]["proportional"]
        fl0 = [(t_, x) for t_, x in r["fills"] if t_ <= o["t_arrival"] + LIFE]
        if fl0:
            tf = fl0[0][0]
            sign = 1.0 if o["side"] == "buy" else -1.0
            mk = {}
            for h in (1.0, 5.0, 30.0):
                mb = mid_at(tf + h); pb = last_at(bin_t, bin_p, tf + h)
                mk[f"{int(h)}s"] = {"bybit_mid_bps": (sign * (mb - o["price"]) / o["price"] * 1e4) if mb else None,
                                    "binance_last_trade_bps_PROXY": (sign * (pb - o["price"]) / o["price"] * 1e4) if pb else None}
            p0 = last_at(bin_t, bin_p, tf)
            drift = {}
            for ms in (50, 100, 250, 500, 1000):
                p1 = last_at(bin_t, bin_p, tf + ms / 1000.0)
                # positive = adverse to the hedge: after a BUY fill the hedge sells on Binance, so a falling price is adverse; after a SELL fill a rising price is
                drift[f"{ms}ms"] = (-sign * (p1 - p0) / p0 * 1e4) if (p0 and p1) else None
            row["markout"], row["hedge_drift_PROXY_bps_positive_is_adverse"] = mk, drift
            row["fill_time_of_day_s"] = tf - t_first
        res_orders.append(row)
    os.makedirs(OUT, exist_ok=True)
    json.dump({"day": day, "label": "EXPLORATORY BYBIT_ONLY_WINDOW", "window_s": [t_first, t_last], "window_hours": (t_last - t_first) / 3600.0, "events": n_ev, "tick": tick,
               "grid_s": GRID, "life_s": LIFE, "check_s": CHECK, "size": SIZE, "models": list(MODELS), "orders": res_orders, "wall_s": time.time() - t_wall},
              open(f"{OUT}/{day}.json", "w"))
    print(day, "orders", len(orders), "events", n_ev, "wall %.0fs" % (time.time() - t_wall), flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
