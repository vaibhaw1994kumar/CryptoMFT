"""Aggregate the Oct-2024 BYBIT_ONLY_WINDOW maker-only runs (engine outputs only; the quarantined oct_bybit_maker_sim.py is NOT used). EXPLORATORY.
Inputs : maker_only/oct/<model>/<day>/{maker_observations.jsonl,summary.json}  (main runner, analysis_mode=maker_only)
         oct_aux/proportional/<day>.json  (oct_day_features.py: Bybit mid at fill+h via the imported OrderBook; Binance last trade at fill+h = PROXY)
Output : oct_maker_only_summary.json
(a) rebuild acceptance by cause is read from oct_causes/ by the report builder (exact rebuilder), not recomputed here."""
import glob, json, os, sys
import numpy as np
sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from new_backtest.queue_v2 import survival as sv
X = "/Users/vaibhav/Documents/Newproject/out/queue_v2"
MODELS = ["min_baseline", "behind_first", "proportional", "ahead_first", "power_0.5", "power_2.0", "cohort_youngest_first", "cohort_oldest_first", "cohort_uniform", "cohort_hazard_0.5"]
PRIMARY = "proportional"
KIND = {1: "improve", 0: "touch", -1: "second"}
rng = np.random.default_rng(20241104)
man = json.load(open(f"{X}/manifest.json"))["days"]
days = sorted(d.split("/")[-1] for d in man if d.startswith("bybit/SOLUSDT/2024-10") and all(os.path.exists(f"{X}/maker_only/oct/{m}/{d.split('/')[-1]}/summary.json") for m in MODELS))
if len(sys.argv) > 1:
    days = [d for d in days if d in sys.argv[1:]]


def load(model, day):
    places, fills = {}, {}
    for l in open(f"{X}/maker_only/oct/{model}/{day}/maker_observations.jsonl"):
        r = json.loads(l)
        if r["obs"] == "maker_place":
            places[r["maker_order_id"]] = r
        elif r["obs"] == "maker_fill":
            fills.setdefault(r["maker_order_id"], []).append(r)
    out = {}
    t0 = man[f"bybit/SOLUSDT/{day}"]["channels"]["book_events"]["first_ts"]
    for oid, p in places.items():
        t = p["touch"]["bybit"] if False else p["hedge_touch_at_decision"]
        buy = p["side"] == "buy"
        near, opp = (t["bid_size"], t["ask_size"]) if buy else (t["ask_size"], t["bid_size"])
        imb = t["imbalance"] if buy else -t["imbalance"]
        fl = sorted(fills.get(oid, []), key=lambda r: r["ts"])
        key = (round(p["ts"], 6), p["side"], round(p["price"], 8))
        out[key] = {"day": day, "hour_block": f"{day}#{int((p['ts'] - t0) // 3600)}", "kind": KIND.get(p["relative_to_touch_ticks"], "other"), "side": p["side"], "rel": (0.0 if p["relative_queue_position"] is None else p["relative_queue_position"]),
                    "near": near, "opp": opp, "ahead": p["queue_ahead"], "imb": imb, "qty_filled": min(1.0, sum(r["qty"] for r in fl)), "filled": bool(fl),
                    "ttf": (fl[0]["fill"]["match_ts"] - fl[0]["fill"]["arrival_ts"]) if fl else None, "cause": fl[0]["fill"]["cause"] if fl else None,
                    "first_fill": {"ts": fl[0]["ts"], "oid": oid} if fl else None,
                    "first_fill_ambiguous": bool(fl) and "ambiguous_trade_depth" in fl[0]["fill"]["flags"],
                    "any_fill_unflagged": any("ambiguous_trade_depth" not in r["fill"]["flags"] for r in fl)}
    return out


data = {m: {} for m in MODELS}
for m in MODELS:
    for d in days:
        data[m].update({(d,) + k: v for k, v in load(m, d).items()})
keys = sorted(data[PRIMARY])
same = all(set(data[m]) == set(data[PRIMARY]) for m in MODELS)
res = {"label": "EXPLORATORY BYBIT_ONLY_WINDOW", "days": days, "orders": len(keys), "order_set_identical_across_models": same,
       "note": "orders are hypothetical v1-style PASSIVE orders, 1 lot, grid every 90 s, time-in-force from the v1 profile; Binance Oct-2024 = trade tape only (PROXY)"}


def pct(a, q):
    return float(np.percentile(a, q)) if len(a) else None


def boot(blocks, vals, n=2000):
    """hour-block bootstrap of the mean of vals; blocks: list of block ids per observation."""
    if len(vals) == 0:
        return None
    ub = sorted(set(blocks))
    idx = {b: i for i, b in enumerate(ub)}
    s, c = np.zeros(len(ub)), np.zeros(len(ub))
    for b, v in zip(blocks, vals):
        s[idx[b]] += v; c[idx[b]] += 1
    draws = rng.integers(0, len(ub), (n, len(ub)))
    num, den = s[draws].sum(1), c[draws].sum(1)
    est = num / np.where(den > 0, den, np.nan)
    return {"mean": float(s.sum() / c.sum()), "ci95": [float(np.nanpercentile(est, 2.5)), float(np.nanpercentile(est, 97.5))], "n": int(c.sum()), "blocks": len(ub)}


# (b) cancel-model comparison
tab = {}
for m in MODELS:
    rows = [data[m][k] for k in keys]
    f = [r for r in rows if r["filled"]]
    t = np.array([r["ttf"] for r in f]) if f else np.array([])
    tab[m] = {"orders": len(rows), "fill_rate_any": len(f) / len(rows), "fill_rate_full": sum(r["qty_filled"] >= 0.999 for r in rows) / len(rows),
              "time_to_fill_s": {"p10": pct(t, 10), "p50": pct(t, 50), "p90": pct(t, 90)},
              "fill_rate_ci95_hour_block": boot([r["hour_block"] for r in rows], [float(r["filled"]) for r in rows])["ci95"],
              "by_kind": {k: {"orders": sum(r["kind"] == k for r in rows), "fill_rate": (sum(r["filled"] for r in rows if r["kind"] == k) / max(1, sum(r["kind"] == k for r in rows)))} for k in ("improve", "touch", "second")},
              "first_fill_cause": {c: sum(1 for r in f if r["cause"] == c) for c in sorted({r["cause"] for r in f})}}
res["models"] = tab
fr = np.array([tab[m]["fill_rate_any"] for m in MODELS])
dis = sum(len({data[m][k]["filled"] for m in MODELS}) > 1 for k in keys)
ttf_med = [tab[m]["time_to_fill_s"]["p50"] for m in MODELS if tab[m]["time_to_fill_s"]["p50"] is not None]
res["spread_across_models"] = {"fill_rate_min": float(fr.min()), "fill_rate_max": float(fr.max()), "fill_rate_range_pp": float((fr.max() - fr.min()) * 100),
                               "orders_where_models_disagree_on_filled": dis, "share_orders_models_disagree": dis / len(keys),
                               "time_to_fill_p50_range_s": [min(ttf_med), max(ttf_med)] if ttf_med else None}

# guide s4 / s8: association bounds and survival under censoring (the time-in-force is an ADMINISTRATIVE censoring at TTL seconds)
TTL = 15.0
bounds, surv = {}, {}
for m in MODELS:
    rows = [data[m][k] for k in keys]
    n = len(rows)
    bounds[m] = {"fill_rate_as_simulated": sum(r["filled"] for r in rows) / n,
                 "fill_rate_first_fill_association_robust": sum(r["filled"] and not r["first_fill_ambiguous"] for r in rows) / n,
                 "fill_rate_if_every_ambiguous_fill_were_a_cancellation": sum(r["any_fill_unflagged"] for r in rows) / n,
                 "share_of_filled_orders_whose_first_fill_is_ambiguous": sum(r["first_fill_ambiguous"] for r in rows) / max(1, sum(r["filled"] for r in rows))}
    x = np.array([r["ttf"] if r["filled"] else TTL for r in rows]); e = np.array([1 if r["filled"] else 0 for r in rows])
    d = sv.diagnostics(x, e, (1.0, 5.0, 10.0, 15.0, 30.0, 60.0), min_at_risk=20)
    blocks = [r["hour_block"] for r in rows]
    def rm(h):
        v = np.minimum(x, h)
        return boot(blocks, list(v))
    d["rmst_ci95_hour_block"] = {f"{int(h)}s": rm(h) for h in (5.0, 15.0)}
    surv[m] = d
res["association_bounds"] = {"note": "ambiguous = flag ambiguous_trade_depth (a depth decrease or snapshot shrink attributed to us without one causal print, or an ambiguous ledger association). The lower bound treats every such fill as a cancellation.", "models": bounds}
res["survival"] = {"time_in_force_s": TTL, "censoring": "administrative: every unfilled order is cancelled at the time-in-force, independent of the market, so Kaplan-Meier / IPCW equal the naive share up to the TTL; beyond it (30 s, 60 s) the fill probability of a quote left alive is NOT identified (nobody is at risk)",
                   "models": surv}

# (c) Albers check on the primary model: first fill per order; markouts vs Bybit mid and Binance last trade (PROXY)
aux = {}
for d in days:
    p = f"{X}/oct_aux/{PRIMARY}/{d}.json"
    if os.path.exists(p):
        for f in json.load(open(p))["fills"]:
            aux.setdefault((d, f["maker_order_id"]), []).append(f)
HB, HX = ("1", "5", "30"), ("0.05", "0.1", "0.25", "0.5", "1")
recs = []
for k in keys:
    r = data[PRIMARY][k]
    if not r["filled"]:
        continue
    al = aux.get((r["day"], r["first_fill"]["oid"]))
    if not al:
        continue
    a = min(al, key=lambda f: f["ts"])                       # the order's FIRST fill row
    sg = 1.0 if a["side"] == "buy" else -1.0
    m = {}
    for h in HB:
        mb, pb = a["bybit_mid"].get(h), a["binance_last_PROXY"].get(h)
        m["mid" + h] = sg * (mb - a["price"]) / a["price"] * 1e4 if mb else None
        m["bin" + h] = sg * (pb - a["price"]) / a["price"] * 1e4 if pb else None
    p0 = a["binance_last_PROXY"].get("0")
    for h in HX:
        p1 = a["binance_last_PROXY"].get(h)
        m["drift" + h] = -sg * (p1 - p0) / p0 * 1e4 if (p0 and p1) else None    # positive = adverse to the hedge on Binance
    recs.append({**r, **m})
res["albers"] = {"primary_model": PRIMARY, "fills_with_markout_inputs": len(recs), "markout_sign": "positive = price moved in our favour; bps of the fill price"}
med_near, med_opp = (float(np.median([data[PRIMARY][k]["near"] for k in keys])), float(np.median([data[PRIMARY][k]["opp"] for k in keys])))
a33, a66 = np.percentile([data[PRIMARY][k]["ahead"] for k in keys], [33.3, 66.7])
q33, q66 = np.percentile([data[PRIMARY][k]["imb"] for k in keys], [33.3, 66.7])
groups = {"all": lambda r: True, "queue_front_lt_0.25": lambda r: r["rel"] < 0.25, "queue_back_gt_0.75": lambda r: r["rel"] > 0.75,
          "near_small_opp_small": lambda r: r["near"] <= med_near and r["opp"] <= med_opp, "near_small_opp_large": lambda r: r["near"] <= med_near and r["opp"] > med_opp,
          "near_large_opp_small": lambda r: r["near"] > med_near and r["opp"] <= med_opp, "near_large_opp_large": lambda r: r["near"] > med_near and r["opp"] > med_opp,
          "ahead_size_low_tercile": lambda r: r["ahead"] <= a33, "ahead_size_mid_tercile": lambda r: a33 < r["ahead"] <= a66, "ahead_size_high_tercile": lambda r: r["ahead"] > a66,
          "imbalance_against_us_low_tercile": lambda r: r["imb"] <= q33, "imbalance_mid_tercile": lambda r: q33 < r["imb"] <= q66, "imbalance_with_us_high_tercile": lambda r: r["imb"] > q66}
res["albers"]["splits"] = {"queue_ahead_size_terciles": [float(a33), float(a66)], "note_relative_position": "orders join the BACK of the queue by construction, so the relative position at placement is ~1 for almost every order and the front (<0.25) group is (nearly) empty; the absolute size ahead terciles are the substitute", "median_near_size": med_near, "median_opposite_size": med_opp, "imbalance_terciles": [float(q33), float(q66)]}
res["albers"]["groups"] = {}
for g, fn in groups.items():
    gr_all = [data[PRIMARY][k] for k in keys if fn(data[PRIMARY][k])]
    gr = [r for r in recs if fn(r)]
    row = {"orders": len(gr_all), "fills": len(gr), "fill_rate": boot([r["hour_block"] for r in gr_all], [float(r["filled"]) for r in gr_all]) if gr_all else None}
    for h in HB:
        for tag, lab in (("mid", "bybit_mid"), ("bin", "binance_last_trade_PROXY")):
            v = [(r["hour_block"], r[tag + h]) for r in gr if r[tag + h] is not None]
            row[f"markout_{h}s_{lab}_bps"] = boot([b for b, _ in v], [x for _, x in v]) if v else None
    res["albers"]["groups"][g] = row

# (d) Binance drift after a Bybit fill (PROXY): distribution in bps, positive = adverse to the hedge
res["binance_drift_PROXY_bps_positive_adverse"] = {}
for h in HX:
    v = np.array([r["drift" + h] for r in recs if r["drift" + h] is not None])
    res["binance_drift_PROXY_bps_positive_adverse"][f"+{int(float(h) * 1000)}ms"] = {"n": int(len(v)), "p10": pct(v, 10), "p50": pct(v, 50), "p90": pct(v, 90), "mean": float(v.mean()) if len(v) else None}

# cross-check: offline markout vs the engine's own aggregate (qty-weighted, Bybit mid)
xc = {}
for d in days:
    s = json.load(open(f"{X}/maker_only/oct/{PRIMARY}/{d}/summary.json"))["markout_summary"]
    fl = [a for (dd, _), al in aux.items() if dd == d for a in al]              # every fill row, as the engine's tracker counts them
    for h in HB:
        num = den = 0.0
        for a in fl:
            mb = a["bybit_mid"].get(h)
            if mb:
                sg = 1.0 if a["side"] == "buy" else -1.0
                num += sg * (mb - a["price"]) / a["price"] * 1e4 * a["qty"]; den += a["qty"]
        xc.setdefault(d, {})[h + "s"] = {"offline_bps": num / den if den else None, "engine_bps": s[h + "s"]["qty_weighted_markout_bps"]}
res["markout_cross_check_vs_engine_aggregate"] = xc
json.dump(res, open(f"{X}/oct_maker_only_summary.json", "w"), indent=1, default=str)
print("days", len(days), "orders", len(keys), "identical order sets", same, "models", len(tab))
print(json.dumps({m: (round(t["fill_rate_any"], 3), t["time_to_fill_s"]["p50"]) for m, t in tab.items()}))
print(json.dumps(res["spread_across_models"]))
