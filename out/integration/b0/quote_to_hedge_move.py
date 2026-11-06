"""Owner 7 Nov, report only: what the single expected hedge move (-0.488 bps, Binance mid, fill -> +45 ms) leaves out. For every probe fill: the Binance bid at placement (the reference the quote was priced from),
at the fill's report time, and 45 ms after it; move quote -> fill, fill -> hedge, and quote -> hedge, in bps of the placement bid (negative = adverse for a sell hedge). Output probe_adverse/quote_to_hedge_move.json. EXPLORATORY."""
import bisect, glob, gzip, json, statistics as st, math
from pathlib import Path
R = Path("/Users/vaibhav/Documents/Newproject/out/integration/b0/probe_adverse")
SL = Path("/Users/vaibhav/Documents/Newproject/out/queue_v2/slices_mar2024")
rows = []
for day in [f"2024-03-0{i}" for i in range(1, 8)]:
    ts, bid, ask = [], [], []
    for fp in sorted(glob.glob(f"{SL}/binance/book_snapshots/pair=SOLUSDT/date={day}/*.gz")):
        with gzip.open(fp, "rt") as fh:
            for l in fh:
                r = json.loads(l); ts.append(float(r.get("local_ts") or r["timestamp"])); bid.append(float(r["best_bid"])); ask.append(float(r["best_ask"]))
    at = lambda arr, t: arr[max(0, bisect.bisect_right(ts, t) - 1)]
    d = json.load(open(R / f"{day}.json"))
    obs = [json.loads(l) for l in open(R / f"run_{day}" / "maker_observations.jsonl")]
    place = {o["signal_id"]: o for o in obs if o["obs"] == "maker_place"}
    for r in d["rows"]:
        p = place.get(r["signal_id"])
        if not p:
            continue
        b0, bf, bh = at(bid, p["ts"]), at(bid, r["ts"]), at(bid, r["ts"] + 0.045)
        rows.append({"day": day, "q2f": (bf - b0) / b0 * 1e4, "f2h": (bh - bf) / b0 * 1e4, "q2h": (bh - b0) / b0 * 1e4, "wait_s": r["ts"] - p["ts"]})
def agg(k):
    v = [x[k] for x in rows]; by = {}
    for x in rows: by.setdefault(x["day"], []).append(x[k])
    dm = [sum(a) / len(a) for a in by.values()]
    return {"n": len(v), "mean": sum(v) / len(v), "median": st.median(v), "se_day_block": st.pstdev(dm) / math.sqrt(len(dm))}
res = {"label": "Binance BID move, quote time -> fill -> +45 ms, probe fills, 7 TRAIN days; EXPLORATORY", "quote_to_fill": agg("q2f"), "fill_to_hedge_45ms": agg("f2h"), "quote_to_hedge_45ms": agg("q2h"),
       "mean_wait_quote_to_fill_s": sum(x["wait_s"] for x in rows) / len(rows), "pinned_scalar_bps": -0.488}
(R / "quote_to_hedge_move.json").write_text(json.dumps(res, indent=1)); print(json.dumps(res, indent=1))
