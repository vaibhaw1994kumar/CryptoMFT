"""B1 helper (read only): do the trace lines of each part resolve inside the same part? counts per disposition, unresolved refs, events per part."""
import gzip, json, sys
from collections import Counter
from pathlib import Path
D = Path(sys.argv[1]); tot = Counter(); unresolved = 0; cross = 0; sizes = []
for part in sorted(D.glob("part-*")):
    can = {json.loads(l)["event_id"] for l in gzip.open(part / "canonical.jsonl.gz", "rt")}
    tr = {json.loads(l)["event_id"] for l in gzip.open(part / "trades.jsonl.gz", "rt")}
    n = 0
    for l in gzip.open(part / "trace.jsonl.gz", "rt"):
        r = json.loads(l); tot[r["disposition"]] += 1; n += 1
        if r["disposition"] == "CANONICAL" and r["canonical_event_id"] not in can: unresolved += 1
        if r["disposition"] == "TRADE" and r["canonical_event_id"] not in tr: unresolved += 1
    sizes.append(n)
print(dict(tot), "unresolved in-part refs", unresolved, "parts", len(sizes), "max trace lines per part", max(sizes))
