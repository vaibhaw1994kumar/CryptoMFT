"""Legacy bit-identity re-check on the final queue-v2 snapshot (snap_qv2g): legacy_v0 on 2022-01-05 and 2022-01-13 and v1 on 2022-01-05 against the Phase 0 gate runs
(out/exec_layer/p0i). Byte-identical files; every summary.json field except the named exclusions."""
import hashlib, json
from pathlib import Path
X = Path("/Users/vaibhav/Documents/Newproject/out/queue_v2"); P = Path("/Users/vaibhav/Documents/Newproject/out/exec_layer/p0i")
TAG = "singapore_bybit_perp_mm_clean_s1"
# data_integrity and queue_parameter_status are reporting fields added by audit decisions A and E (2026-11-04): they did not exist in the reference summaries
EX = ("wall_s", "release_identity", "switch_table", "qv2", "tag", "data_integrity", "queue_parameter_status")
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
def diff(a, b, p=""):
    o = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            o += [(p + "/" + k, "absent", "present")] if k not in a else ([(p + "/" + k, "present", "absent")] if k not in b else diff(a[k], b[k], p + "/" + k))
    elif isinstance(a, list) and isinstance(b, list):
        o += [(p, len(a), len(b))] if len(a) != len(b) else [x for i, (u, v) in enumerate(zip(a, b)) for x in diff(u, v, f"{p}[{i}]")]
    elif a != b: o.append((p, a, b))
    return o
runs = [("legacy_v0", "2022-01-05", "legacy_check"), ("legacy_v0", "2022-01-13", "legacy_check"), ("perp_perp_maker_taker_v1", "2022-01-05", "v1_check")]
res = {"snapshot": (X / "snap_qv2g/FINGERPRINT.txt").read_text().strip(), "reference": "out/exec_layer/p0i (Phase 0 gate, snapshot 51f9283ec840f473)", "runs": {}}
ok = True
for prof, day, tag in runs:
    q = X / "runs_final4" / prof / f"{TAG}_{tag}" / day; p = P / prof / TAG / day
    if not (q / "summary.json").exists():
        res["runs"][f"{prof}/{day}"] = "pending"; ok = False; continue
    files = {f: sha(q / f) == sha(p / f) for f in ("trades.jsonl", "fill_events.jsonl", "fill_bucket_stats.json", "model_settings.json")}
    sq, sp = json.loads((q / "summary.json").read_text()), json.loads((p / "summary.json").read_text())
    d = [x for x in diff({k: v for k, v in sp.items() if k not in EX}, {k: v for k, v in sq.items() if k not in EX}) if not x[0].startswith("/data_quality")]
    res["runs"][f"{prof}/{day}"] = {"files_identical": files, "summary_diffs": d[:10], "identical": all(files.values()) and not d}
    ok &= all(files.values()) and not d
res["ALL_IDENTICAL"] = ok
res["summary"] = ("all three replays byte-identical to the Phase 0 gate runs (trade log, fill events, fill stats, model settings) with no summary difference" if ok else "see runs")
json.dump(res, open(X / "identity_check.json", "w"), indent=1, default=str)
print(json.dumps(res, indent=1, default=str)[:1500])
