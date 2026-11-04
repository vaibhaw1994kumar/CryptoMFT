"""Parity proof: maker fills of the maker-only path (main runner, analysis_mode='maker_only', Bybit book only) against the maker fills of the normal v1 replay of the same day
(same orders, same model). Compared per fill: ts, quantity, price, side, cause tag; an order is the schedule item (same landing time, price, side)."""
import json, sys
X = "/Users/vaibhav/Documents/Newproject/out/queue_v2"
res = {}
for day in ("2022-01-05", "2022-01-13"):
    R = [json.loads(l) for l in open(f"{X}/runs/perp_perp_maker_taker_v1/singapore_bybit_perp_mm_clean_s1_base/{day}/maker_observations.jsonl")]
    M = [json.loads(l) for l in open(f"{X}/maker_only/parity/{day}/maker_observations.jsonl")]
    rf = [r for r in R if r["obs"] == "maker_fill" and r["venue"] == "bybit"]
    mf = [r for r in M if r["obs"] == "maker_fill" and r["venue"] == "bybit"]
    rp = [r for r in R if r["obs"] == "maker_place" and r["venue"] == "bybit"]
    mp = [r for r in M if r["obs"] == "maker_place" and r["venue"] == "bybit"]
    key = lambda r: (round(r["ts"], 6), r["side"], round(r["price"], 8), round(r["qty"], 8), r["fill"]["cause"])
    kr, km = sorted(map(key, rf)), sorted(map(key, mf))
    match = sum(1 for a in kr if a in km)
    pk = lambda r: (round(r["price"], 8), r["side"], round(r["size"], 8))
    res[day] = {"orders_normal": len(rp), "orders_maker_only": len(mp), "orders_same_price_side_size": sorted(map(pk, rp)) == sorted(map(pk, mp)),
                "fills_normal": len(rf), "fills_maker_only": len(mf), "fills_identical_ts_qty_price_side_cause": match,
                "fills_only_in_normal": [list(a) for a in kr if a not in km][:8], "fills_only_in_maker_only": [list(a) for a in km if a not in kr][:8],
                "place_queue_ahead_identical": sum(1 for a, b in zip(sorted(rp, key=lambda r: (r["price"], r["side"], r["ts"])), sorted(mp, key=lambda r: (r["price"], r["side"], r["ts"]))) if abs(a["queue_ahead"] - b["queue_ahead"]) < 1e-9),
                "causes_normal": sorted({r["fill"]["cause"] for r in rf}), "causes_maker_only": sorted({r["fill"]["cause"] for r in mf})}
res["PASS"] = all(v["fills_normal"] == v["fills_identical_ts_qty_price_side_cause"] == v["fills_maker_only"] for v in res.values() if isinstance(v, dict))
res["total_fills_matched"] = f'{sum(v["fills_identical_ts_qty_price_side_cause"] for k, v in res.items() if isinstance(v, dict))} of {sum(v["fills_normal"] for k, v in res.items() if isinstance(v, dict))}'
json.dump(res, open(f"{X}/parity_result.json", "w"), indent=1)
print(json.dumps(res, indent=1)[:2600])
