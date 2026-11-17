"""W7 G4: v1_14 against v1_13 on the 29 Oct day. compare_sessions on the raw files, then the two files that gain `estimate` compared with the key stripped."""
import json, sys
from pathlib import Path
from new_backtest.session_replay import compare_sessions
a, b = Path(sys.argv[1]), Path(sys.argv[2])
raw = compare_sessions(a, b)
def rows(p):
    return [{k: v for k, v in json.loads(l).items() if k != "estimate"} for l in p.read_text().splitlines() if l.strip()]
def has_key(p):
    return sum(1 for l in p.read_text().splitlines() if '"estimate"' in l)
stripped = {}
for name in ("session_signals.jsonl", "session_intent_journal.jsonl"):
    stripped[name] = {"equal_without_estimate": rows(a / name) == rows(b / name), "rows_with_estimate_a": has_key(a / name), "rows_with_estimate_b": has_key(b / name), "rows": len(rows(b / name))}
out = {"raw_different": raw["different"], "raw_files_compared": raw["files_compared"], "stripped": stripped}
print(json.dumps(out, indent=1))
