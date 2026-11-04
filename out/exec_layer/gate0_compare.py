"""Gate 0: pre-refactor (base) vs post-refactor (post) replays, field by field.
Byte-identical: trades.jsonl, fill_events.jsonl, fill_bucket_stats.json, model_settings.json.
summary.json: every field, recursively, EXCEPT the named exclusions, which are listed with what differs:
  wall_s (timing), release_identity (code hash, profile hash, execution block: differ by design), switch_table (two new rows by design).
Also compares base against the 2 Nov e2e results (out/e2e_perp, an older tree) and reports 'changed since 2 Nov' separately."""
import hashlib, json, sys
from pathlib import Path

X = Path("/Users/vaibhav/Documents/Newproject/out/exec_layer")
E2E = Path("/Users/vaibhav/Documents/Newproject/out/e2e_perp")
RUNS = [("legacy_v0", "2022-01-05"), ("legacy_v0", "2022-01-13"), ("perp_perp_maker_taker_v1", "2022-01-05"), ("perp_perp_maker_taker_v1", "2022-01-13")]
TAG = "singapore_bybit_perp_mm_clean_s1"
EXCLUDE = ("wall_s", "release_identity", "switch_table")
FILES = ("trades.jsonl", "fill_events.jsonl", "fill_bucket_stats.json", "model_settings.json")


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


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
    elif a != b:
        out.append((path, a, b))
    return out


def load(root, prof, day):
    return json.loads((root / prof / TAG / day / "summary.json").read_text()), root / prof / TAG / day


def n_fields(d):
    return sum(n_fields(v) for v in d.values()) if isinstance(d, dict) else (sum(n_fields(v) for v in d) if isinstance(d, list) else 1)


res = {"gate": "GATE 0 pre vs post refactor", "runs": {}, "base_fingerprint": (X / "snap_base/FINGERPRINT.txt").read_text().strip(),
       "post_fingerprint": (X / "snap_post/FINGERPRINT.txt").read_text().strip()}
ok = True
for prof, day in RUNS:
    b, bd = load(X / "base", prof, day)
    p, pd = load(X / "post", prof, day)
    files = {f: {"identical": sha(bd / f) == sha(pd / f), "sha256": sha(bd / f)} for f in FILES}
    d = diff({k: v for k, v in b.items() if k not in EXCLUDE}, {k: v for k, v in p.items() if k not in EXCLUDE})
    excl = {}
    for k in EXCLUDE:
        if k == "wall_s": continue
        dd = diff(b.get(k), p.get(k), "")
        excl[k] = {"n_differences": len(dd), "paths": [x[0] for x in dd][:12]}
    run_ok = all(v["identical"] for v in files.values()) and not d
    ok &= run_ok
    # base vs the 2 Nov e2e results
    try:
        e, _ = load(E2E, prof, day)
        dd = diff({k: v for k, v in e.items() if k not in EXCLUDE}, {k: v for k, v in b.items() if k not in EXCLUDE})
        since = {"n_differences": len(dd), "first": [(x[0], x[1], x[2]) for x in dd[:8]]}
    except Exception as ex:
        since = {"error": str(ex)}
    res["runs"][f"{prof}/{day}"] = {"identical": run_ok, "files": files, "summary_fields_compared": n_fields({k: v for k, v in b.items() if k not in EXCLUDE}),
                                    "summary_differences": [(x[0], x[1], x[2]) for x in d[:20]], "excluded_fields": excl, "base_vs_2nov_e2e": since}
res["PASS"] = ok
(X / "gate0_result.json").write_text(json.dumps(res, indent=1, default=str))
print(json.dumps({k: (v if k != "runs" else {r: {"identical": x["identical"], "fields": x["summary_fields_compared"], "n_diff": len(x["summary_differences"]), "excl": {a: b["n_differences"] for a, b in x["excluded_fields"].items()}, "vs_2nov": x["base_vs_2nov_e2e"].get("n_differences")} for r, x in v.items()}) for k, v in res.items()}, indent=1, default=str))
