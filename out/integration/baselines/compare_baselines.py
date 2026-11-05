"""Phase 0 baseline checker (run wrapper). Compares two baseline labels run by run_baselines.py, or writes the manifest of one.
Byte-identical: trades.jsonl (trade log), fill_events.jsonl, fill_bucket_stats.json, model_settings.json. summary.json (the result): every field, recursively, except the
named exclusions, which are listed with what differs (timing, output paths, the code hash and switch table that must change when code moves, wall clock).
usage: compare_baselines.py manifest LABEL        -> baselines/manifest_LABEL.json (sha256 of every file, key result fields)
       compare_baselines.py compare LABEL_A LABEL_B [subset] -> exit 0 only if every run is identical; prints and writes compare_A_vs_B.json"""
import hashlib, json, sys
from pathlib import Path

B = Path("/Users/vaibhav/Documents/Newproject/out/integration/baselines")
FILES = ("trades.jsonl", "fill_events.jsonl", "fill_bucket_stats.json", "model_settings.json")
EXCLUDE = ("wall_s", "wall_seconds_engine", "release_identity", "switch_table", "tag", "trades_path", "fill_log_path", "qv2")
KEYS = ("signals", "fills", "orders_sent", "fees_paid", "gross_pnl", "cash_pnl", "ending_equity_common_timestamp", "final_inventory", "traded_notional")


def run_dirs(label):
    """name -> directory of one run (jan: runs/L/jan/<profile>/<tag>/<day>, mar: runs/L/mar/base/<profile>/<day>)."""
    root = B / "runs" / label
    out = {}
    for s in sorted(root.glob("jan/*/*/*/summary.json")):
        out[f"jan/{s.parents[2].name}/{s.parent.name}"] = s.parent
    for s in sorted(root.glob("mar/base/*/*/summary.json")):
        out[f"mar/{s.parents[1].name}/{s.parent.name}"] = s.parent
    return out


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def diff(a, b, path=""):
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if path == "" and k in EXCLUDE:
                continue
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


def manifest(label):
    res = {}
    for name, d in run_dirs(label).items():
        s = json.loads((d / "summary.json").read_text())
        res[name] = {"files": {f: sha(d / f) for f in FILES}, "summary_sha256": sha(d / "summary.json"),
                     "result": {k: s.get(k) for k in KEYS}, "wall_s": s.get("wall_s"), "events_processed": s.get("events_processed")}
    (B / f"manifest_{label}.json").write_text(json.dumps(res, indent=1, default=str))
    return res


def compare(a, b, subset=False):
    ra, rb = run_dirs(a), run_dirs(b)
    if subset:
        ra = {k: v for k, v in ra.items() if k in rb}                       # B is a partial re-run (determinism check): compare only what it ran
    res, ok = {}, True
    for name in sorted(set(ra) | set(rb)):
        if name not in ra or name not in rb:
            res[name] = "missing in " + (b if name not in rb else a); ok = False; continue
        files = {f: sha(ra[name] / f) == sha(rb[name] / f) for f in FILES}
        sa, sb = (json.loads((d[name] / "summary.json").read_text()) for d in (ra, rb))
        d = diff(sa, sb)
        res[name] = {"files_identical": files, "summary_differences": d[:12], "identical": all(files.values()) and not d}
        ok &= res[name]["identical"]
    out = {"A": a, "B": b, "ALL_IDENTICAL": ok, "runs": res, "excluded_from_summary_compare": list(EXCLUDE)}
    (B / f"compare_{a}_vs_{b}.json").write_text(json.dumps(out, indent=1, default=str))
    return out


if __name__ == "__main__":
    if sys.argv[1] == "manifest":
        print(json.dumps({k: v["result"] for k, v in manifest(sys.argv[2]).items()}, indent=1, default=str))
    else:
        r = compare(sys.argv[2], sys.argv[3], subset=len(sys.argv) > 4 and sys.argv[4] == "subset")
        print(json.dumps({"ALL_IDENTICAL": r["ALL_IDENTICAL"], "runs": {k: (v if isinstance(v, str) else v["identical"]) for k, v in r["runs"].items()}}, indent=1))
        sys.exit(0 if r["ALL_IDENTICAL"] else 1)
