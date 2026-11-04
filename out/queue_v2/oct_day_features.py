"""Oct-2024 BYBIT_ONLY_WINDOW, offline join (EXPLORATORY): for every maker_fill row of ONE model's engine run (default proportional) look up
  - the Bybit mid at fill_ts + h (h = 0, 1, 5, 30 s)   [OrderBook IMPORTED from new_backtest, fed with the same Bybit snapshot/delta/trade events the engine saw]
  - the Binance last trade price at fill_ts + h (h = 0, 50, 100, 250, 500, 1000 ms, 1, 5, 30 s)   [PROXY: Binance Oct-2024 is a trade tape, never a book]
The engine itself places the orders and produces the fills (main runner, maker-only mode); nothing here simulates a fill.
usage: oct_day_features.py DAY [MODEL]  -> out/queue_v2/oct_aux/<MODEL>/<DAY>.json"""
import array, bisect, json, os, sys, warnings
warnings.filterwarnings("ignore")
X = "/Users/vaibhav/Documents/Newproject/out/queue_v2"
sys.path.insert(0, f"{X}/snap_qv2e/src")
from new_backtest.event_loader import BOOK, DELTA, TRADE, delta_fields, iter_venue_events, snapshot_levels, trade_fields
from new_backtest.order_book import OrderBook

DATA = "/Users/vaibhav/Documents/Newproject/data"
BYB_H = (0.0, 1.0, 5.0, 30.0)
BIN_H = (0.0, 0.05, 0.1, 0.25, 0.5, 1.0, 5.0, 30.0)


def main(day, model="proportional"):
    man = json.load(open(f"{X}/manifest.json"))["days"][f"bybit/SOLUSDT/{day}"]["channels"]["book_events"]
    rows = [json.loads(l) for l in open(f"{X}/maker_only/oct/{model}/{day}/maker_observations.jsonl")]
    fills = [r for r in rows if r["obs"] == "maker_fill"]
    qs = sorted({round(r["ts"] + h, 6) for r in fills for h in BYB_H})
    mid_at = {}
    i = 0
    book = OrderBook("bybit", "SOLUSDT")
    t_end = man["last_ts"]
    for ev in iter_venue_events(DATA, "bybit", "SOLUSDT", day, day):
        ts = ev.exchange_ts
        if ts > t_end + 0.5:
            break
        while i < len(qs) and qs[i] < ts:
            mid_at[qs[i]] = book.mid if book.ready else None
            i += 1
        if ev.kind == BOOK:
            bids, asks = snapshot_levels(ev.payload)
            book.apply_snapshot(bids, asks, ts)
        elif ev.kind == DELTA:
            side, price, volume, et = delta_fields(ev.payload)
            if price > 0:
                book.apply_delta(side, price, volume, et, ts)
        elif ev.kind == TRADE:
            price, volume, sign = trade_fields(ev.payload)
            if price > 0 and volume > 0:
                book.on_trade(price, volume, sign, ts)
    while i < len(qs):
        mid_at[qs[i]] = None                      # beyond the real Bybit book window: no mid, the markout is dropped
        i += 1
    bt, bp = array.array("d"), array.array("d")
    for ev in iter_venue_events(DATA, "binance", "SOLUSDT", day, day, include_snapshots=False, include_deltas=False):
        bt.append(ev.exchange_ts); bp.append(float(ev.payload["price"]))

    def last(t):
        j = bisect.bisect_right(bt, t) - 1
        return bp[j] if j >= 0 else None
    out = []
    for r in fills:
        t0 = r["ts"]
        out.append({"maker_order_id": r["maker_order_id"], "ts": t0, "side": r["side"], "price": r["price"], "qty": r["qty"],
                    "bybit_mid": {f"{h:g}": mid_at.get(round(t0 + h, 6)) for h in BYB_H},
                    "binance_last_PROXY": {f"{h:g}": last(t0 + h) for h in BIN_H}})
    os.makedirs(f"{X}/oct_aux/{model}", exist_ok=True)
    json.dump({"day": day, "model": model, "label": "EXPLORATORY BYBIT_ONLY_WINDOW", "binance_series": "trade tape PROXY, not a book", "fills": out}, open(f"{X}/oct_aux/{model}/{day}.json", "w"))
    print(day, model, len(out), "fills joined", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], *(sys.argv[2:3]))
