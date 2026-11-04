"""Is Binance Oct-2024 book_events really depth, or a copy of the trade tape? Full-file comparison, every day (read-only on data/)."""
import glob, gzip, json, sys
D = "/Users/vaibhav/Documents/Newproject/data/binance"
out = {}
for day in [f"2024-10-{d:02d}" for d in range(1, 16)]:
    t = glob.glob(f"{D}/trades/pair=SOLUSDT/date={day}/*.gz")[0]; b = glob.glob(f"{D}/book_events/pair=SOLUSDT/date={day}/*.gz")[0]
    n = same = 0; sides_ok = 0; types = set()
    with gzip.open(t, "rt") as f1, gzip.open(b, "rt") as f2:
        for a, c in zip(f1, f2):
            A, C = json.loads(a), json.loads(c); n += 1
            same += (A["exchange_ts"] == C["exchange_ts"] and A["price"] == C["price"] and A["volume"] == C["volume"])
            sides_ok += ((A["side"] == "sell") == (C["side"] == "bid"))
            types.add(C["event_type"])
    out[day] = {"rows_compared": n, "identical_ts_price_volume": same, "share_identical": same / max(1, n), "side_sell_is_bid_share": sides_ok / max(1, n), "event_types": sorted(types)}
    print(day, out[day], flush=True)
json.dump(out, open("/Users/vaibhav/Documents/Newproject/out/queue_v2/oct_binance_book_events_vs_trades.json", "w"), indent=1)
