"""Market-data message rate of the 2026-10-29 capture (READ ONLY on data/): events per second by kind, p50 / p99 / max over the seconds that had any, for the production-limits proposal. A short-lived run wrapper (no logic beyond counting)."""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from new_backtest.session_replay import iter_recorded_events

SESSION = Path("/Users/vaibhav/Documents/Newproject/data/live_sessions/solusdt/2026-10-29/SOLUSDT/e9b81728a3054812a48035a1b3e02d42")
per = Counter()
for ev in iter_recorded_events(SESSION):
    per[(int(ev.receive_ts_ns // 1_000_000_000), ev.kind if hasattr(ev, "kind") else type(ev).__name__)] += 1
secs = Counter()
for (s, k), n in per.items():
    secs[s] += n
vals = sorted(secs.values())
def q(p): return vals[min(len(vals) - 1, int(p * len(vals)))]
by_kind = {}
for (s, k), n in per.items():
    by_kind.setdefault(k, []).append(n)
out = {"seconds_with_events": len(vals), "all_kinds_per_second": {"p50": q(0.5), "p99": q(0.99), "p999": q(0.999), "max": vals[-1], "mean": sum(vals) / len(vals)},
       "by_kind_per_second": {k: {"p50": sorted(v)[len(v) // 2], "p99": sorted(v)[int(0.99 * len(v))], "max": max(v)} for k, v in by_kind.items()}}
Path("event_rate.json").write_text(json.dumps(out, indent=1))
print(json.dumps(out, indent=1))
