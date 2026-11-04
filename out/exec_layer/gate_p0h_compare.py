"""Phase 0 hardening identity check: pre-refactor baseline (base) vs the final hardening build (p0h), 4 replays.
Byte-identical files; every summary.json field except the named exclusions; NEW fields present only in p0h are listed, not hidden."""
import hashlib, json
from pathlib import Path
X = Path("/Users/vaibhav/Documents/Newproject/out/exec_layer")
RUNS = [("legacy_v0", "2022-01-05"), ("legacy_v0", "2022-01-13"), ("perp_perp_maker_taker_v1", "2022-01-05"), ("perp_perp_maker_taker_v1", "2022-01-13")]
TAG = "singapore_bybit_perp_mm_clean_s1"
EXCLUDE = ("wall_s", "release_identity", "switch_table")
FILES = ("trades.jsonl", "fill_events.jsonl", "fill_bucket_stats.json", "model_settings.json")
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
def diff(a, b, path=""):
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a: out.append((f"{path}/{k}", "<absent>", "present"))
            elif k not in b: out.append((f"{path}/{k}", "present", "<absent>"))
            else: out += diff(a[k], b[k], f"{path}/{k}")
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b): out.append((path, f"len {len(a)}", f"len {len(b)}"))
        else:
            for i, (x, y) in enumerate(zip(a, b)): out += diff(x, y, f"{path}[{i}]")
    elif a != b: out.append((path, a, b))
    return out
nf = lambda d: sum(nf(v) for v in d.values()) if isinstance(d, dict) else (sum(nf(v) for v in d) if isinstance(d, list) else 1)
res = {"gate": "PHASE 0 HARDENING: pre-refactor (base) vs final build (p0h)", "base_fingerprint": (X/"snap_base/FINGERPRINT.txt").read_text().strip(),
       "p0h_fingerprint": (X/"snap_p0h/FINGERPRINT.txt").read_text().strip(), "runs": {}}
ok = True
for prof, day in RUNS:
    bd, pd = X/"base"/prof/TAG/day, X/"p0h"/prof/TAG/day
    b, p = json.loads((bd/"summary.json").read_text()), json.loads((pd/"summary.json").read_text())
    files = {f: sha(bd/f) == sha(pd/f) for f in FILES}
    d = diff({k: v for k, v in b.items() if k not in EXCLUDE}, {k: v for k, v in p.items() if k not in EXCLUDE})
    new_only = [x for x in d if x[1] == "<absent>"]
    changed = [x for x in d if x[1] != "<absent>"]
    rec = p.get("execution_reconciliation") or {}
    run_ok = all(files.values()) and not changed
    ok &= run_ok and (rec.get("ok") is True)
    res["runs"][f"{prof}/{day}"] = {"identical_files": files, "fields_compared": nf({k: v for k, v in b.items() if k not in EXCLUDE}),
        "changed_fields": changed[:20], "fields_new_in_p0h": [x[0] for x in new_only], "reconciliation_ok": rec.get("ok"),
        "reconciliation_fill_kinds": rec.get("fill_kinds"), "reconciliation_problems": rec.get("problems"), "overfills": rec.get("overfill_count"),
        "identical": run_ok}
res["PASS"] = ok
(X/"gate_p0h_result.json").write_text(json.dumps(res, indent=1, default=str))
print(json.dumps(res, indent=1, default=str))
