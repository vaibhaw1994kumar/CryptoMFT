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
  compare_baselines.py hashes [--root DIR] [--gates FILE] [--golden-root DIR] [--no-head]
                                                       gate G0: the pinned sha256 lines of out/integration/GATES.md (block between PINS:BEGIN / PINS:END) against the
                                                       files on disk and in git HEAD; one PASS / FAIL / NOT CHECKED / INFO line each; writes nothing; exit 1 on any FAIL
  compare_baselines.py hashes-selftest                 the checker's own test on a temporary copy (same verdict twice, one-byte change, missing file, ...); never touches the real files
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


# ---- gate G0: pinned hashes (out/integration/GATES.md is the one source of truth; this code only reads it) ----
PIN_BEGIN, PIN_END = "<!-- PINS:BEGIN -->", "<!-- PINS:END -->"
OUTER_ROOT = Path(__file__).resolve().parents[2]          # Newproject/ (the outer repo root); the pin paths are relative to it
STATUSES = ("PINNED", "FROZEN", "NOT_FROZEN", "NOT_CREATED", "GOLDEN", "GOLDEN_NOCHECK")
GROUPS = ("inputs", "profiles", "freeze", "golden")
# owner 18 Nov 2026 (go-live stage 1): these profiles are PINNED; the block must list each of them as PINNED or FROZEN, never NOT_FROZEN or absent (a line edited back is a FAIL, not an INFO)
MUST_BE_PINNED = tuple(f"crypto_MFT_arb_platform/profiles/perp_perp_maker_taker_v1_{n}.yaml" for n in (11, 12, 13, 14, 15))


def _file_sha(p: Path):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def _head_sha(root: Path, rel: str):
    """sha256 of the file as committed at HEAD of the repo at root; None = not tracked or no git. Read only."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(root), "show", f"HEAD:{rel}"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return hashlib.sha256(r.stdout).hexdigest() if r.returncode == 0 else None


def parse_pins(text: str):
    """Pin lines `group | status | sha256 | path` between the PINS markers. Returns (pins, errors)."""
    if text.count(PIN_BEGIN) != 1 or text.count(PIN_END) != 1:
        return [], [f"the block {PIN_BEGIN} ... {PIN_END} must appear exactly once"]
    block = text.split(PIN_BEGIN, 1)[1].split(PIN_END, 1)[0]
    pins, errors, seen = [], [], set()
    for n, line in enumerate(block.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = [x.strip() for x in s.split("|")]
        if len(parts) != 4:
            errors.append(f"pin block line {n}: expected 4 fields `group | status | sha256 | path`"); continue
        group, status, sha, path = parts
        if group not in GROUPS or status not in STATUSES:
            errors.append(f"pin block line {n}: unknown group or status ({group}, {status})"); continue
        if status != "NOT_CREATED" and (len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha)):
            errors.append(f"pin block line {n}: sha256 must be 64 lowercase hex characters"); continue
        key = (group, path)
        if key in seen:
            errors.append(f"pin block line {n}: duplicate pin for {path}"); continue
        seen.add(key)
        pins.append({"group": group, "status": status, "sha": sha, "path": path})
    if not pins and not errors:
        errors.append("the pin block holds no pins")
    return pins, errors


def check_pins(root: Path, gates: Path, golden_root=None, head=True):
    """Returns (lines, counts). A line is (verdict, group, path, detail); verdict PASS | FAIL | NOT CHECKED | INFO. Writes nothing."""
    lines = []
    if not gates.is_file():
        return [("FAIL", "gates", str(gates), "GATES.md not found")], {"PASS": 0, "FAIL": 1, "NOT CHECKED": 0, "INFO": 0, "goldens_checked": 0, "goldens_total": 0}
    pins, errors = parse_pins(gates.read_text(encoding="utf-8"))
    lines += [("FAIL", "gates", str(gates), e) for e in errors]
    use_head = head and (root / ".git").exists()
    goldens_checked = goldens_total = 0
    by_path = {p["path"]: p for p in pins}
    for rel in MUST_BE_PINNED:
        if rel not in by_path or by_path[rel]["status"] not in ("PINNED", "FROZEN"):
            lines.append(("FAIL", "profiles", rel, "must be PINNED (owner 18 Nov) but the block lists it as " + (by_path[rel]["status"] if rel in by_path else "absent")))
    for p in pins:
        g, st, sha, rel = p["group"], p["status"], p["sha"], p["path"]
        if g == "golden":
            goldens_total += 1
            if st == "GOLDEN_NOCHECK":
                lines.append(("NOT CHECKED", g, rel, "recorded only: byte check excluded by design (summary.json / session_manifest.json differ in wall clock, paths, code hash)")); continue
            f = Path(golden_root) / rel if golden_root else None      # only a fresh run's output dir given with --golden-root counts; old artefacts under out/ are not the golden run
            if f is None or not f.is_file():
                lines.append(("NOT CHECKED", g, rel, ("no --golden-root given" if f is None else "file not found under --golden-root") + "; this line is a record, not a check")); continue
            goldens_checked += 1
            got = _file_sha(f)
            lines.append(("PASS", g, rel, "matches") if got == sha else ("FAIL", g, rel, f"golden changed: pinned {sha[:12]}, on disk {got[:12]}"))
            continue
        f = root / rel
        got = _file_sha(f)
        if st == "NOT_CREATED":
            lines.append(("INFO", g, rel, "not created yet (as recorded)") if got is None else ("FAIL", g, rel, "file exists now but GATES.md says NOT_CREATED: record its status and hash in GATES.md"))
            continue
        if got is None:
            lines.append(("FAIL", g, rel, "pinned file is missing")); continue
        if st == "NOT_FROZEN":
            lines.append(("INFO", g, rel, "NOT FROZEN; observed hash " + ("unchanged since recorded" if got == sha else f"CHANGED since recorded (recorded {sha[:12]}, now {got[:12]}); not a failure, never pinned")))
            continue
        if got != sha:
            lines.append(("FAIL", g, rel, f"pinned {sha[:12]}, on disk {got[:12]}")); continue
        detail = "disk matches"
        if use_head and g in ("profiles", "freeze"):
            h = _head_sha(root, rel)
            if h is None:
                lines.append(("FAIL", g, rel, "disk matches the pin but the file is not tracked at HEAD")); continue
            if h != sha:
                lines.append(("FAIL", g, rel, f"disk matches the pin but HEAD holds {h[:12]} (uncommitted edit or a different commit)")); continue
            detail = "disk and HEAD match"
        lines.append(("PASS", g, rel, detail))
    # a freeze file must agree with the profile it names (and with the reference profiles it lists)
    for p in pins:
        if p["group"] == "freeze" and p["path"].endswith(".freeze.json") and (root / p["path"]).is_file():
            try:
                fj = json.loads((root / p["path"]).read_text(encoding="utf-8"))
                want = {fj["profile_file"]: fj["profile_sha256"]}
                want.update({f"crypto_MFT_arb_platform/profiles/{k}.yaml": v for k, v in (fj.get("reference_profiles_sha256") or {}).items()})
            except (ValueError, KeyError):
                lines.append(("FAIL", "freeze", p["path"], "unreadable freeze file (need profile_file, profile_sha256)")); continue
            for prof, expect in want.items():
                got = _file_sha(root / prof)
                lines.append(("PASS", "freeze", p["path"] + " -> " + prof.rsplit("/", 1)[-1], "freeze file agrees with the profile on disk") if got == expect
                             else ("FAIL", "freeze", p["path"] + " -> " + prof.rsplit("/", 1)[-1], f"freeze file says {expect[:12]}, profile on disk {str(got)[:12]}"))
                if prof in by_path and by_path[prof]["status"] in ("FROZEN", "PINNED") and by_path[prof]["sha"] != expect:
                    lines.append(("FAIL", "freeze", p["path"] + " -> " + prof.rsplit("/", 1)[-1], "freeze file and the GATES.md pin disagree"))
    counts = {k: sum(1 for v in lines if v[0] == k) for k in ("PASS", "FAIL", "NOT CHECKED", "INFO")}
    counts.update(goldens_checked=goldens_checked, goldens_total=goldens_total)
    return lines, counts


def hashes_main(argv):
    root, gates, golden_root, head = OUTER_ROOT, None, None, True
    it = iter(argv)
    for a in it:
        if a == "--root": root = Path(next(it))
        elif a == "--gates": gates = Path(next(it))
        elif a == "--golden-root": golden_root = next(it)
        elif a == "--no-head": head = False
        else:
            print(f"unknown argument {a}", file=sys.stderr); return 2
    gates = gates or root / "out" / "integration" / "GATES.md"
    lines, c = check_pins(root, gates, golden_root, head)
    for v, g, path, detail in lines:
        print(f"{v:<11} {g:<8} {path}  [{detail}]")
    print(f"SUMMARY pass={c['PASS']} fail={c['FAIL']} not_checked={c['NOT CHECKED']} info={c['INFO']} | goldens {c['goldens_checked']}/{c['goldens_total']} checked | G0 {'FAIL' if c['FAIL'] else 'PASS'}")
    return 1 if c["FAIL"] else 0


def hashes_selftest():
    """The checker's own test, on a temporary copy of the pinned files (the real files are only read)."""
    import shutil, tempfile
    real = OUTER_ROOT
    pins, errs = parse_pins((real / "out" / "integration" / "GATES.md").read_text(encoding="utf-8"))
    results = []

    def case(name, ok, detail=""):
        results.append(ok); print(f"{'PASS' if ok else 'FAIL'}  selftest: {name}" + (f"  [{detail}]" if detail else ""))

    case("GATES.md pin block parses", not errs and len(pins) > 0, f"{len(pins)} pins" if not errs else "; ".join(errs))
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "Newproject"
        for rel in {p["path"] for p in pins if p["group"] in ("inputs", "profiles", "freeze")}:
            if (real / rel).is_file():
                (root / rel).parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(real / rel, root / rel)
        gates = root / "out" / "integration" / "GATES.md"
        gates.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(real / "out" / "integration" / "GATES.md", gates)
        l1, c1 = check_pins(root, gates, head=False); l2, c2 = check_pins(root, gates, head=False)
        case("copy of the real tree has no FAIL", c1["FAIL"] == 0, f"fail={c1['FAIL']} " + "; ".join(f"{p}" for v, g, p, d in l1 if v == "FAIL")[:200])
        case("same verdict twice (identical lines)", l1 == l2)
        pinned = [p for p in pins if p["status"] in ("PINNED", "FROZEN") and p["group"] == "profiles"]
        target = root / pinned[0]["path"]
        orig = target.read_bytes()
        target.write_bytes(orig[:-1] + bytes([orig[-1] ^ 1]))                       # one byte flipped, in the copy only
        l3, c3 = check_pins(root, gates, head=False)
        failed = [p for v, g, p, d in l3 if v == "FAIL" and p == pinned[0]["path"]]
        case("one-byte change in a pinned profile gives FAIL on that line", bool(failed) and c3["FAIL"] >= 1, pinned[0]["path"].rsplit("/", 1)[-1])
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            rc = hashes_main(["--root", str(root), "--no-head"])
        case("exit code of the command is 1 on that FAIL", rc == 1)
        target.write_bytes(orig)
        ip = next(p for p in pins if p["group"] == "inputs"); (root / ip["path"]).unlink()
        l4, c4 = check_pins(root, gates, head=False)
        case("a missing pinned input gives FAIL", any(v == "FAIL" and p == ip["path"] and "missing" in d for v, g, p, d in l4))
        shutil.copyfile(real / ip["path"], root / ip["path"])
        nf = [p for p in pins if p["status"] == "NOT_FROZEN"]
        if nf:
            f = root / nf[0]["path"]; f.write_bytes(f.read_bytes() + b"\n# edit")
            l5, c5 = check_pins(root, gates, head=False)
            case("an edit to a NOT_FROZEN profile is INFO, not FAIL", c5["FAIL"] == 0 and any(v == "INFO" and p == nf[0]["path"] and "CHANGED" in d for v, g, p, d in l5))
        gtext = gates.read_text(encoding="utf-8")
        must = MUST_BE_PINNED[0]
        gates.write_text(gtext.replace(f"profiles | PINNED | {_file_sha(real / must)} | {must}", f"profiles | NOT_FROZEN | {_file_sha(real / must)} | {must}"), encoding="utf-8")
        l5b, c5b = check_pins(root, gates, head=False)
        case("a profile pinned by the owner on 18 Nov listed back as NOT_FROZEN gives FAIL", any(v == "FAIL" and p == must and "must be PINNED" in d for v, g, p, d in l5b))
        gates.write_text(gtext, encoding="utf-8")
        nc_rel = "crypto_MFT_arb_platform/profiles/selftest_not_created.yaml"       # synthetic: the real block may hold no NOT_CREATED row
        gates.write_text(gtext.replace(PIN_END, f"profiles | NOT_CREATED | - | {nc_rel}\n{PIN_END}"), encoding="utf-8")
        l6a, c6a = check_pins(root, gates, head=False)
        case("a NOT_CREATED profile that is still absent is INFO", c6a["FAIL"] == 0 and any(v == "INFO" and p == nc_rel for v, g, p, d in l6a))
        f = root / nc_rel; f.parent.mkdir(parents=True, exist_ok=True); f.write_text("x")
        l6, c6 = check_pins(root, gates, head=False)
        case("a NOT_CREATED profile that appears gives FAIL", any(v == "FAIL" and p == nc_rel for v, g, p, d in l6))
        f.unlink()
        gates.write_text(gtext, encoding="utf-8")
        gold_rel = "baselines/runs/selftest/trades.jsonl"
        gates.write_text(gtext.replace(PIN_END, f"golden | GOLDEN | {hashlib.sha256(b'x').hexdigest()} | {gold_rel}\n{PIN_END}"), encoding="utf-8")
        groot = Path(td) / "fresh_run"
        l7, c7 = check_pins(root, gates, golden_root=groot, head=False)
        case("a golden whose file is absent under --golden-root is NOT CHECKED, not PASS", any(v == "NOT CHECKED" and p == gold_rel for v, g, p, d in l7) and c7["goldens_checked"] == 0)
        l7b, c7b = check_pins(root, gates, head=False)
        case("without --golden-root no golden is checked (0 checked)", c7b["goldens_checked"] == 0 and c7b["FAIL"] == 0)
        gf = groot / gold_rel; gf.parent.mkdir(parents=True, exist_ok=True); gf.write_bytes(b"x")
        l8, c8 = check_pins(root, gates, golden_root=groot, head=False)
        case("a golden file that is present and equal is PASS", any(v == "PASS" and p == gold_rel for v, g, p, d in l8) and c8["goldens_checked"] == 1)
        gf.write_bytes(b"y")
        l9, c9 = check_pins(root, gates, golden_root=groot, head=False)
        case("a golden file that differs is FAIL", any(v == "FAIL" and p == gold_rel for v, g, p, d in l9))
        gates.write_text(gtext.replace(PIN_BEGIN, ""), encoding="utf-8")
        l10, c10 = check_pins(root, gates, head=False)
        case("a damaged pin block is FAIL", c10["FAIL"] >= 1)
    print(f"SELFTEST {'PASS' if all(results) else 'FAIL'}: {sum(results)}/{len(results)} cases")
    return 0 if all(results) else 1


if __name__ == "__main__":
    if sys.argv[1] == "hashes":
        sys.exit(hashes_main(sys.argv[2:]))
    if sys.argv[1] == "hashes-selftest":
        sys.exit(hashes_selftest())
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
