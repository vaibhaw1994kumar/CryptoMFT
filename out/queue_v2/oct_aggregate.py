"""Aggregate the Oct-2024 BYBIT_ONLY_WINDOW work: rebuild-acceptance causes (a), queue-model comparison (b), Albers check (c), Binance drift PROXY (d).
Hour-block bootstrap: a block is one clock hour of one day's real Bybit window. EXPLORATORY. usage: oct_aggregate.py"""
import collections, glob, json
import numpy as np
X = "/Users/vaibhav/Documents/Newproject/out/queue_v2"
rng = np.random.default_rng(20261104)
B = 2000

def block_ci(rows, val, blk, stat=np.mean):
    """rows: list; val(row)->float or None; blk(row)->hashable. Returns (point, lo, hi, n, n_blocks)."""
    by = collections.defaultdict(list)
    for r in rows:
        v = val(r)
        if v is not None and not (isinstance(v, float) and np.isnan(v)): by[blk(r)].append(v)
    keys = list(by)
    if not keys: return (None, None, None, 0, 0)
    allv = [v for k in keys for v in by[k]]
    pt = float(stat(allv))
    vals = []
    for _ in range(B):
        pick = rng.integers(0, len(keys), len(keys))
        xs = [v for i in pick for v in by[keys[i]]]
        vals.append(float(stat(xs)))
    return (pt, float(np.quantile(vals, 0.025)), float(np.quantile(vals, 0.975)), len(allv), len(keys))

def load_days():
    out = {}
    for f in sorted(glob.glob(f"{X}/oct_sim/*.json")):
        d = json.load(open(f)); out[d["day"]] = d
    return out

def main():
    days = load_days()
    res = {"label": "EXPLORATORY BYBIT_ONLY_WINDOW", "days": sorted(days), "window_hours": {k: v["window_hours"] for k, v in days.items()}}
    # (a) causes
    causes = {}
    for f in sorted(glob.glob(f"{X}/oct_causes/*.json")):
        d = json.load(open(f)); causes[d["day"]] = d
    tot = collections.defaultdict(lambda: {"snapshots": 0, "price_miss": 0.0, "size_miss": 0.0})
    lv = collections.defaultdict(lambda: [0.0, 0.0])
    for d in causes.values():
        for c, v in d["by_cause"].items():
            t = tot[c]; t["snapshots"] += v["snapshots"]; t["price_miss"] += v["snapshots"] * v["touch_price_miss_pct"] / 100; t["size_miss"] += v["snapshots"] * v["touch_size_miss_pct"] / 100
            for k, m in v["level_miss_pct"].items():
                lv[(c, k)][0] += m * v["snapshots"] / 100; lv[(c, k)][1] += v["snapshots"]
    res["rebuild_causes"] = {"per_day": {k: v["by_cause"] for k, v in causes.items()},
                             "total": {c: {"snapshots": t["snapshots"], "touch_price_miss_pct": 100 * t["price_miss"] / max(1, t["snapshots"]), "touch_size_miss_pct": 100 * t["size_miss"] / max(1, t["snapshots"]),
                                           "level_miss_pct": {k[1]: 100 * a / b for k, (a, b) in lv.items() if k[0] == c and b}} for c, t in tot.items()}}
    # flatten orders
    O = []
    for day, d in days.items():
        for o in d["orders"]:
            o["day"] = day; o["blk"] = (day, int((o["t_arrival"] - d["window_s"][0]) // 3600)); O.append(o)
    models = days[next(iter(days))]["models"] if days else []
    # (b) queue models
    fr = lambda m, H: (lambda o: float(o["models"][m]["first_fill_dt"] is not None and o["models"][m]["first_fill_dt"] <= H))
    bres = {}
    for kind in ("touch", "improve", "second", "all"):
        rows = [o for o in O if kind == "all" or o["kind"] == kind]
        bres[kind] = {"orders": len(rows), "models": {}}
        for m in models:
            ent = {}
            for H in (1.0, 5.0, 30.0, 60.0):
                ent[f"fill_{int(H)}s"] = block_ci(rows, fr(m, H), lambda o: o["blk"])
            ft = [o["models"][m]["first_fill_dt"] for o in rows if o["models"][m]["first_fill_dt"] is not None]
            ent["median_time_to_fill_s"] = float(np.median(ft)) if ft else None
            bres[kind]["models"][m] = ent
        for H in ("fill_1s", "fill_5s", "fill_30s", "fill_60s"):
            pts = [bres[kind]["models"][m][H][0] for m in models if bres[kind]["models"][m][H][0] is not None]
            bres[kind][f"spread_{H}"] = (max(pts) - min(pts)) if pts else None
    res["queue_model_comparison"] = bres
    # (c) Albers check (proportional model)
    P = [o for o in O if o["models"]["proportional"]["first_fill_dt"] is not None or True]
    med_near = float(np.median([o["near_size"] for o in O if o["near_size"] > 0])) if O else 0
    med_far = float(np.median([o["opposite_size"] for o in O])) if O else 0
    def grp(o):
        pos = "front(<0.25)" if o["rel0"] < 0.25 else ("back(>0.75)" if o["rel0"] > 0.75 else "middle")
        sz = ("near_big" if o["near_size"] > med_near else "near_small") + "/" + ("opp_big" if o["opposite_size"] > med_far else "opp_small")
        # imbalance in the direction of the order: positive = more size on OUR side of the touch
        imb = o["imbalance"] if o["side"] == "buy" else -o["imbalance"]
        return pos, sz, ("own_side_heavy" if imb > 0 else "opp_side_heavy")
    ares = {"split": {"near_size_median": med_near, "opposite_size_median": med_far}, "groups": {}}
    keys = sorted({(g[0], g[1], g[2]) for g in map(grp, O)})
    for gname, gi in (("position", 0), ("near_x_opposite_size", 1), ("imbalance_direction", 2)):
        ares["groups"][gname] = {}
        for k in sorted({grp(o)[gi] for o in O}):
            rows = [o for o in O if grp(o)[gi] == k]
            ent = {"orders": len(rows)}
            for H in (1.0, 5.0, 30.0):
                ent[f"fill_{int(H)}s"] = block_ci(rows, fr("proportional", H), lambda o: o["blk"])
            for h in ("1s", "5s", "30s"):
                ent[f"markout_{h}_bybit_mid_bps"] = block_ci(rows, lambda o, h=h: (o.get("markout") or {}).get(h, {}).get("bybit_mid_bps"), lambda o: o["blk"])
                ent[f"markout_{h}_binance_last_trade_bps_PROXY"] = block_ci(rows, lambda o, h=h: (o.get("markout") or {}).get(h, {}).get("binance_last_trade_bps_PROXY"), lambda o: o["blk"])
            ares["groups"][gname][k] = ent
    res["albers_check"] = ares
    # (d) Binance drift after a Bybit fill (PROXY)
    dres = {}
    for side in ("all", "buy", "sell"):
        rows = [o for o in O if o.get("hedge_drift_PROXY_bps_positive_is_adverse") and (side == "all" or o["side"] == side)]
        dres[side] = {"fills": len(rows)}
        for ms in ("50ms", "100ms", "250ms", "500ms", "1000ms"):
            xs = [o["hedge_drift_PROXY_bps_positive_is_adverse"][ms] for o in rows if o["hedge_drift_PROXY_bps_positive_is_adverse"][ms] is not None]
            dres[side][ms] = {"n": len(xs), "p10": float(np.quantile(xs, .1)) if xs else None, "p50": float(np.quantile(xs, .5)) if xs else None, "p90": float(np.quantile(xs, .9)) if xs else None,
                              "share_exactly_zero": float(np.mean([x == 0 for x in xs])) if xs else None}
    res["binance_drift_after_bybit_fill_PROXY"] = dres
    json.dump(res, open(f"{X}/oct_bybit_only_summary.json", "w"), indent=1, default=str)
    print("written", len(days), "days,", len(O), "orders")

main()
