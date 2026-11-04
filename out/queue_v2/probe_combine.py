import json, math, statistics as st
from pathlib import Path
P = Path("/Users/vaibhav/Documents/Newproject/out/queue_v2/hedge_move")
days = ["2024-03-01", "2024-03-02", "2024-03-03"]
per = {d: json.loads((P / "probe" / f"{d}.json").read_text()) for d in days}
mv = [r["move_bps"] for d in days for r in per[d]["rows"]]
n = len(mv)
mean = sum(mv) / n if n else None
sd = st.pstdev(mv) if n > 1 else None
res = {"label": "hedge-move PROBE (resting 1 SOL Bybit bid at the strategy's quote depth, v1.1 fill rules); EXPLORATORY", "train_days": days, "slice": "00:00-02:00 UTC",
       "probe_bid_move_bps": mean, "median_bps": st.median(mv) if n else None, "n": n, "stdev_bps": sd, "se_bps": (sd / math.sqrt(n)) if sd else None,
       "min_bps": min(mv) if n else None, "max_bps": max(mv) if n else None,
       "per_day": {d: {"orders": per[d]["orders_scheduled"], "fills": per[d]["fills"], "measured": per[d]["fills_measured"], "causes": per[d]["maker_fill_causes"],
                       "mean_bps": (sum(r["move_bps"] for r in per[d]["rows"]) / len(per[d]["rows"])) if per[d]["rows"] else None} for d in days},
       "compare": {"market_data_unconditional_bid_bps": -0.6538, "old_legacy_trained_bid_bps": -4.2853}}
(P / "probe_result.json").write_text(json.dumps(res, indent=1)); print(json.dumps({k: v for k, v in res.items() if k != "per_day"}, indent=1)); print(json.dumps(res["per_day"], indent=1))
