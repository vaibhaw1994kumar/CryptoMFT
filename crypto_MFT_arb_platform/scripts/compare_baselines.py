"""Baseline checker for the shared-platform phases (Phase 0 onward). Compares replay outputs run by out/integration/baselines/run_baselines.py.

Byte-identical: trades.jsonl (trade log), fill_events.jsonl, fill_bucket_stats.json, model_settings.json.
summary.json (the result): every field, recursively, except the named exclusions, which are listed with what differs (timing, output paths, the code hash and switch table
that must change when code moves, wall clock).

usage (from anywhere):
  compare_baselines.py manifest LABEL                  write baselines/manifest_LABEL.json (sha256 of every file, key result fields)
  compare_baselines.py compare A B [subset]            two labels, run by run; subset = B is a partial re-run, compare only what it ran
  compare_baselines.py check LABEL [subset]            LABEL against the committed manifest_b1.json (the reference), by sha256, when runs/b1 is not on disk
  compare_baselines.py tests LOGFILE                   the failing and erroring tests of a pytest log (run with -rfE) against baselines/failing_tests_phase0.txt:
                                                       a gate passes only if the set is unchanged (no new failure, none fixed without updating the list)
exit 0 only if everything compared is identical.

Run names: jan/<profile>/<day>, mar/<profile>/<day>; a run with a non-default tag adds @<tag>, e.g. jan/perp_perp_maker_taker_v1@frozen/2022-01-13 or
jan/perp_perp_maker_taker_v1_1@stressed_s1/2022-01-13 (the default tag is clean_s1_base)."""
import hashlib, json, sys
from pathlib import Path

B = Path(__file__).resolve().parents[2] / "out" / "integration" / "baselines"
FILES = ("trades.jsonl", "fill_events.jsonl", "fill_bucket_stats.json", "model_settings.json")
EXCLUDE = ("wall_s", "wall_seconds_engine", "release_identity", "switch_table", "tag", "trades_path", "fill_log_path", "qv2")
KEYS = ("signals", "fills", "orders_sent", "fees_paid", "gross_pnl", "cash_pnl", "ending_equity_common_timestamp", "final_inventory", "traded_notional")
JAN_PREFIX = "singapore_bybit_perp_mm_"


def _suffix(tag: str) -> str:
    rest = tag[len(JAN_PREFIX):] if tag.startswith(JAN_PREFIX) else tag
    return "" if rest == "clean_s1_base" else "@" + rest


def run_dirs(label):
    root = B / "runs" / label
    out = {}
    for s in sorted(root.glob("jan/*/*/*/summary.json")):
        out[f"jan/{s.parents[2].name}{_suffix(s.parents[1].name)}/{s.parent.name}"] = s.parent
    for s in sorted(root.glob("short/*/*/*/summary.json")):
        out[f"short/{s.parents[2].name}{_suffix(s.parents[1].name)}/{s.parent.name}"] = s.parent
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


def manifest(label, write=True):
    res = {}
    for name, d in run_dirs(label).items():
        s = json.loads((d / "summary.json").read_text())
        res[name] = {"files": {f: sha(d / f) for f in FILES}, "summary_sha256": sha(d / "summary.json"),
                     "result": {k: s.get(k) for k in KEYS}, "wall_s": s.get("wall_s"), "events_processed": s.get("events_processed")}
    if write:
        (B / f"manifest_{label}.json").write_text(json.dumps(res, indent=1, default=str))
    return res


def compare(a, b, subset=False):
    ra, rb = run_dirs(a), run_dirs(b)
    if subset:
        ra = {k: v for k, v in ra.items() if k in rb}
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


def check(label, subset=False):
    """Files and result fields of LABEL against the committed manifest_b1.json (sha256 only: summary.json hashes differ by design, so the key result fields are compared)."""
    ref = json.loads((B / "manifest_b1.json").read_text())
    got = manifest(label, write=False)                      # never overwrite a manifest while checking
    res, ok = {}, True
    for name in sorted(set(ref) | set(got)):
        if name not in got:
            if subset: continue
            res[name] = "missing in " + label; ok = False; continue
        if name not in ref:
            res[name] = "not in the reference (new run)"; continue
        files = {f: ref[name]["files"][f] == got[name]["files"][f] for f in FILES}
        fields = {k: ref[name]["result"][k] == got[name]["result"][k] for k in KEYS}
        res[name] = {"files_identical": files, "result_fields_identical": fields, "identical": all(files.values()) and all(fields.values())}
        ok &= res[name]["identical"]
    out = {"label": label, "reference": "manifest_b1.json", "ALL_IDENTICAL": ok, "runs": res}
    (B / f"check_{label}.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def tests(log):
    base = set((B / "failing_tests_phase0.txt").read_text().split("\n")) - {""}
    now = set()
    for line in Path(log).read_text().splitlines():
        if line.startswith(("FAILED ", "ERROR ")):
            now.add(line.split(" - ")[0].strip())
    return {"baseline": len(base), "now": len(now), "new_failures": sorted(now - base), "no_longer_failing": sorted(base - now), "UNCHANGED": now == base}


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "tests":
        r = tests(sys.argv[2]); print(json.dumps(r, indent=1)); sys.exit(0 if r["UNCHANGED"] else 1)
    if cmd == "manifest":
        print(json.dumps({k: v["result"] for k, v in manifest(sys.argv[2]).items()}, indent=1, default=str))
    elif cmd == "compare":
        r = compare(sys.argv[2], sys.argv[3], subset=len(sys.argv) > 4 and sys.argv[4] == "subset")
        print(json.dumps({"ALL_IDENTICAL": r["ALL_IDENTICAL"], "runs": {k: (v if isinstance(v, str) else v["identical"]) for k, v in r["runs"].items()}}, indent=1))
        sys.exit(0 if r["ALL_IDENTICAL"] else 1)
    else:
        r = check(sys.argv[2], subset=len(sys.argv) > 3 and sys.argv[3] == "subset")
        print(json.dumps({"ALL_IDENTICAL": r["ALL_IDENTICAL"], "runs": {k: (v if isinstance(v, str) else v["identical"]) for k, v in r["runs"].items()}}, indent=1))
        sys.exit(0 if r["ALL_IDENTICAL"] else 1)
