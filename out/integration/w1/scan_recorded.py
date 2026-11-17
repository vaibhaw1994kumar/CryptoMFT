"""W1 historical acceptance (audit A1 1.7), a run wrapper with no logic of its own: feeds a recorded canonical event file through crypto_stat_arb.time_integrity.feed_health_scan with the
v1_11 thresholds and writes the verdict (aggregates only). READ ONLY on the recording. usage: scan_recorded.py CANONICAL.jsonl[.gz] OUT.json LABEL"""
import gzip, json, sys, time
from pathlib import Path
sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from crypto_stat_arb.config import feed_health_from_profile
from crypto_stat_arb.models import MarketEvent
from crypto_stat_arb.time_integrity import FeedHealthPolicy, feed_health_scan
from new_backtest.profiles import load_profile

src, out, label = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
cfg = feed_health_from_profile(load_profile("perp_perp_maker_taker_v1_11"))
ms = lambda k: int(cfg[k] * 1e6)
policy = FeedHealthPolicy(ms("delay_degraded_ms"), ms("delay_unsafe_ms"), ms("silence_degraded_ms"), ms("silence_unsafe_ms"), ms("own_lag_ms"), int(cfg["recover_s"] * 1e9))


def events():
    opener = gzip.open if src.suffix == ".gz" else open
    with opener(src, "rt") as fh:
        for line in fh:
            if line.strip():
                row = json.loads(line)
                row.pop("processing_ordinal", None)
                yield MarketEvent.from_dict(row)


t = time.time()
result = feed_health_scan(events(), policy)
result["label"] = label
result["source_file"] = src.name
result["thresholds"] = "perp_perp_maker_taker_v1_11 feed_health block (ASSUMED values)"
result["wall_s"] = round(time.time() - t, 1)
rows = result["feed_health"].pop("transition_rows")
result["transitions_by_feed_and_state"] = {}
for r in rows:
    key = f"{r['venue']}|{r['stream']}|{r['previous']}->{r['state']}|{r['cause']}"
    result["transitions_by_feed_and_state"][key] = result["transitions_by_feed_and_state"].get(key, 0) + 1
result["first_transitions"] = rows[:40]
out.write_text(json.dumps(result, indent=1, default=str))
print(json.dumps({k: result[k] for k in ("events", "time_integrity_blocked_events", "transitions_by_feed_and_state", "wall_s")}, indent=1))
print(json.dumps({k: {x: v[x] for x in ("events", "rate_per_s", "delay_ms", "max_gap_ms", "bursts", "episodes", "seconds_in_state")} for k, v in result["feed_health"]["feeds"].items()}, indent=1))
