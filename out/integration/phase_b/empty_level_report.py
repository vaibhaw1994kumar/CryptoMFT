"""Owner 7 Nov item 5, report only: why do 79 % of January orders and every March order rest on an empty level, and what does that mean for adverse selection?
Reads maker observations of existing runs (no replay): the 7-day TRAIN probe (probe_adverse, v1_2 settings incl. the March windows; it carries the 1 / 5 / 30 s markouts) and the strategy runs
(v1.1 January 01-05 and 01-13, v1_2 March 03-08..03-11). Output: empty_level_report.json. EXPLORATORY.
Per order: state = 'empty_level' (level_was_absent) or 'behind_queue' (a displayed level, queue ahead > 0); position = inside_spread (price above our best bid / below our best ask) | at_touch | behind_touch;
distance from the touch in bps and ticks; spread at placement; time from placement to the first fill; the print that filled it (at our price or strictly through, its size); markout = Binance mid move
after the fill (probe only; negative = adverse for a buy)."""
import glob
import json
import statistics as st
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path("/Users/vaibhav/Documents/Newproject")
R = ROOT / "out" / "integration"
TICK = {"jan2022": {"bybit": 0.005, "binance": 0.001}, "mar2024": {"bybit": 0.001, "binance": 0.001}}


def q(vs, p):
    vs = sorted(vs)
    return vs[min(len(vs) - 1, int(p * len(vs)))] if vs else None


def dist(vs):
    return {"n": len(vs), "p10": q(vs, 0.1), "median": q(vs, 0.5), "p90": q(vs, 0.9), "mean": (sum(vs) / len(vs)) if vs else None}


def load_obs(path):
    return [json.loads(l) for l in open(path)]


def orders_from(obs, ds, tag, rows=None):
    """one record per maker order: placement facts + first fill facts (+ probe markouts when rows are given)"""
    fills = defaultdict(list)
    for o in obs:
        if o["obs"] == "maker_fill":
            fills[o["maker_order_id"]].append(o)
    mk = {}
    if rows:
        for r in rows:
            mk[(r["signal_id"], round(r["ts"], 6))] = r
    out = []
    for o in obs:
        if o["obs"] != "maker_place":
            continue
        v = o["venue"]
        t = o["touch"].get(v) or {}
        if not t.get("bid") or not t.get("ask"):
            continue
        buy = o["side"] == "buy"
        own, opp = (t["bid"], t["ask"]) if buy else (t["ask"], t["bid"])
        tick = TICK[ds][v]
        steps = (own - o["price"]) / tick if buy else (o["price"] - own) / tick           # > 0: behind our own best, < 0: inside the spread (an improvement)
        pos = "inside_spread" if steps < -0.5 else ("at_touch" if abs(steps) <= 0.5 else "behind_touch")
        state = "empty_level" if o["level_was_absent"] else "behind_queue"
        rec = {"ds": ds, "tag": tag, "venue": v, "state": state, "pos": pos, "dist_bps": steps * tick / own * 1e4, "dist_ticks": steps, "spread_bps": (t["ask"] - t["bid"]) / ((t["ask"] + t["bid"]) / 2) * 1e4,
               "spread_ticks": (t["ask"] - t["bid"]) / tick, "queue_ahead": o["queue_ahead"], "size": o["size"], "level_size": o["level_size"]}
        fl = sorted(fills.get(o["maker_order_id"], []), key=lambda x: x["ts"])
        if fl:
            f = fl[0]
            tr = f.get("trigger") or {}
            tp = tr.get("price")
            rec.update(filled=True, rest_s=f["ts"] - o["ts"], cause=f["fill"]["cause"], print_rel=("at_price" if tp is not None and abs(tp - f["price"]) <= 1e-9 * max(1, tp) else "through") if tp is not None else "n/a",
                       print_size=tr.get("gross"), print_through_ticks=((f["price"] - tp) / tick if buy else (tp - f["price"]) / tick) if tp is not None else None, fills=len(fl))
            if rows:
                m = mk.get((f["signal_id"], round(f["ts"], 6)))
                if m:
                    rec.update(m1=m["move_bps"], m5=m.get("move_bps_5s"), m30=m.get("move_bps_30s"))
        else:
            rec["filled"] = False
        out.append(rec)
    return out


def summarise(rs):
    n = len(rs)
    f = [r for r in rs if r["filled"]]
    out = {"orders": n, "share_of_orders": None, "filled": len(f), "fill_rate": len(f) / n if n else None,
           "distance_from_own_touch_bps": dist([r["dist_bps"] for r in rs]), "distance_ticks": dist([r["dist_ticks"] for r in rs]), "spread_bps": dist([r["spread_bps"] for r in rs]),
           "spread_ticks": dist([r["spread_ticks"] for r in rs]), "position": dict(Counter(r["pos"] for r in rs)),
           "rest_s_to_first_fill": dist([r["rest_s"] for r in f]), "filled_within": {f"{s}s": sum(r["rest_s"] <= s for r in f) for s in (1, 5, 15)},
           "fill_cause": dict(Counter(r["cause"] for r in f)), "fill_print": dict(Counter(r["print_rel"] for r in f)),
           "print_size_vs_order": dist([r["print_size"] / r["size"] for r in f if r.get("print_size") is not None]),
           "through_depth_ticks": dist([r["print_through_ticks"] for r in f if r["print_rel"] == "through"]),
           "queue_ahead_at_placement": dist([r["queue_ahead"] for r in rs])}
    for h in ("m1", "m5", "m30"):
        v = [r[h] for r in f if r.get(h) is not None]
        out[f"markout_{h[1:]}s_bps"] = {"n": len(v), "mean": (sum(v) / len(v)) if v else None, "se": (st.pstdev(v) / len(v) ** 0.5) if len(v) > 1 else None, "median": st.median(v) if v else None}
    return out


def main():
    allr = defaultdict(list)
    for day in [f"2024-03-0{i}" for i in range(1, 8)]:
        rows = json.load(open(R / "b0/probe_adverse" / f"{day}.json"))["rows"]
        allr["probe_mar2024_TRAIN_bybit"] += orders_from(load_obs(R / "b0/probe_adverse" / f"run_{day}" / "maker_observations.jsonl"), "mar2024", day, rows)
    for f in glob.glob(str(R / "phase_b/ledger/runs/l0/jan/perp_perp_maker_taker_v1_1/*/2022-01-13/maker_observations.jsonl")):
        allr["strategy_jan2022_v1_1"] += orders_from(load_obs(f), "jan2022", "01-13")
    for f in glob.glob(str(R / "baselines/runs/b1/jan/perp_perp_maker_taker_v1_1/*/2022-01-05/maker_observations.jsonl")):
        allr["strategy_jan2022_v1_1"] += orders_from(load_obs(f), "jan2022", "01-05")
    for d in ("2024-03-08", "2024-03-09", "2024-03-10", "2024-03-11"):
        f = R / f"phase_b/ledger/runs/l0/base/perp_perp_maker_taker_v1_2/{d}/maker_observations.jsonl"
        allr["strategy_mar2024_v1_2"] += orders_from(load_obs(f), "mar2024", d)
    res = {"label": "item 5: empty-level orders; EXPLORATORY; probe = 7 TRAIN days 00:00-02:00 UTC, v1_2 settings, rule off", "sets": {}}
    for k, rs in allr.items():
        s = {"all": summarise(rs)}
        for v in sorted({r["venue"] for r in rs}):
            s[f"venue_{v}"] = summarise([r for r in rs if r["venue"] == v])
        for st_ in ("empty_level", "behind_queue"):
            sub = [r for r in rs if r["state"] == st_]
            s[st_] = summarise(sub)
            s[st_]["share_of_orders"] = len(sub) / len(rs) if rs else None
        for p in ("inside_spread", "at_touch", "behind_touch"):
            sub = [r for r in rs if r["pos"] == p]
            s[f"pos_{p}"] = summarise(sub)
            s[f"pos_{p}"]["share_of_orders"] = len(sub) / len(rs) if rs else None
            s[f"pos_{p}"]["empty_share"] = (sum(r["state"] == "empty_level" for r in sub) / len(sub)) if sub else None
        res["sets"][k] = s
    (R / "phase_b/empty_level_report.json").write_text(json.dumps(res, indent=1, default=str))
    for k, s in res["sets"].items():
        print("==", k, "orders", s["all"]["orders"])
        for part in ("empty_level", "behind_queue", "pos_inside_spread", "pos_at_touch", "pos_behind_touch"):
            x = s[part]
            print(f"  {part:20s} n={x['orders']:5d} share={x['share_of_orders']:.2f} fill_rate={x['fill_rate']} dist_bps med={x['distance_from_own_touch_bps']['median']} spread_bps med={x['spread_bps']['median']} "
                  f"rest med={x['rest_s_to_first_fill']['median']} prints={x['fill_print']} mk1/5/30={[None if x[f'markout_{h}s_bps']['mean'] is None else round(x[f'markout_{h}s_bps']['mean'],2) for h in (1,5,30)]}")


main()
