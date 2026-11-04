"""Totals of the exact-rebuilder cause tables (oct_causes/<day>.json, produced by queue_v2_rebuild_acceptance logic, not by any simulator) + Bybit window hours from the manifest."""
import collections, glob, json
X = "/Users/vaibhav/Documents/Newproject/out/queue_v2"
man = json.load(open(f"{X}/manifest.json"))["days"]
causes = {}
for f in sorted(glob.glob(f"{X}/oct_causes/*.json")):
    d = json.load(open(f)); causes[d["day"]] = d
tot = collections.defaultdict(lambda: {"snapshots": 0, "price_miss": 0.0, "size_miss": 0.0}); lv = collections.defaultdict(lambda: [0.0, 0.0])
for d in causes.values():
    for c, v in d["by_cause"].items():
        t = tot[c]; t["snapshots"] += v["snapshots"]; t["price_miss"] += v["snapshots"] * v["touch_price_miss_pct"] / 100; t["size_miss"] += v["snapshots"] * v["touch_size_miss_pct"] / 100
        for k, m in v["level_miss_pct"].items():
            lv[(c, k)][0] += m * v["snapshots"] / 100; lv[(c, k)][1] += v["snapshots"]
win = {}
for day in causes:
    e = man[f"bybit/SOLUSDT/{day}"]["channels"]["book_events"]; win[day] = (e["last_ts"] - e["first_ts"]) / 3600
res = {"days": sorted(causes), "window_hours": win, "per_day": {k: v["by_cause"] for k, v in causes.items()},
       "total": {c: {"snapshots": t["snapshots"], "touch_price_miss_pct": 100 * t["price_miss"] / max(1, t["snapshots"]), "touch_size_miss_pct": 100 * t["size_miss"] / max(1, t["snapshots"]),
                     "level_miss_pct": {k[1]: 100 * a / b for k, (a, b) in lv.items() if k[0] == c and b}} for c, t in tot.items()}}
json.dump(res, open(f"{X}/oct_rebuild_causes.json", "w"), indent=1)
print(len(causes), "days", round(sum(win.values()), 1), "h", {c: round(v["touch_price_miss_pct"], 2) for c, v in res["total"].items()})
