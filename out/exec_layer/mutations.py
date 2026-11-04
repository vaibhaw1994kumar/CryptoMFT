"""Gate-0 mutation evidence. For each named mutation: copy the engine sources to a temp tree, apply a textual change that removes the
rule, run tests/test_execution_layer.py against that tree, and record which tests fail. A mutation 'is caught' when at least one fails.
usage: mutations.py OUT.json"""
import json, shutil, subprocess, sys, tempfile
from pathlib import Path

P = Path("/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform")
MUTATIONS = {
    "remove_price_cap": [
        ("execution/tactics/base.py", "        w = intent.worst_price\n        if w is None:", "        return\n        w = intent.worst_price\n        if w is None:"),
        ("execution/layer.py", "        if worst_price is None and self.cfg.price_cap_required and role not in (Role.UNWIND, Role.FLATTEN):\n            raise IntentError(f\"{role} intent",
                               "        if False:\n            raise IntentError(f\"{role} intent"),
    ],
    "allow_two_live_hedge_children": [
        ("execution/layer.py", "                if self.cfg.one_hedge_child == \"enforce\":", "                if False:"),
    ],
    "create_hedge_before_the_fill": [
        ("execution/layer.py", "            if parent_filled_qty < qty - 1e-9 and self.dispatch_mode != \"simultaneous\":", "            if False:"),
    ],
}
out = {}
for name, edits in MUTATIONS.items():
    tmp = Path(tempfile.mkdtemp(prefix="mut_"))
    shutil.copytree(P / "src" / "new_backtest", tmp / "src" / "new_backtest", ignore=shutil.ignore_patterns("__pycache__"))
    for d in ("profiles", "config"):
        shutil.copytree(P / d, tmp / d)
    for rel, old, new in edits:
        f = tmp / "src" / "new_backtest" / rel
        s = f.read_text()
        assert s.count(old) == 1, (name, rel, s.count(old))
        f.write_text(s.replace(old, new))
    env = {"PYTHONPATH": f"{tmp/'src'}:{P/'tests'}", "PATH": "/usr/bin:/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin"}
    r = subprocess.run(["python3.12", "-m", "pytest", str(P / "tests" / "test_execution_layer.py"), "-q", "-p", "no:cacheprovider", "-x" if False else "-q"],
                       capture_output=True, text=True, env=env, cwd=str(tmp))
    failed = [l.split(" ")[1].split("::",1)[1] for l in r.stdout.splitlines() if l.startswith("FAILED")]
    out[name] = {"caught": bool(failed), "failed_tests": failed, "summary": r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-300:]}
    shutil.rmtree(tmp, ignore_errors=True)
# control: unmutated tree passes
tmp = Path(tempfile.mkdtemp(prefix="mut_"))
shutil.copytree(P / "src" / "new_backtest", tmp / "src" / "new_backtest", ignore=shutil.ignore_patterns("__pycache__"))
for d in ("profiles", "config"):
    shutil.copytree(P / d, tmp / d)
env = {"PYTHONPATH": f"{tmp/'src'}:{P/'tests'}", "PATH": "/usr/bin:/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin"}
r = subprocess.run(["python3.12", "-m", "pytest", str(P / "tests" / "test_execution_layer.py"), "-q", "-p", "no:cacheprovider"], capture_output=True, text=True, env=env, cwd=str(tmp))
out["_control_unmutated"] = {"summary": r.stdout.strip().splitlines()[-1]}
shutil.rmtree(tmp, ignore_errors=True)
Path(sys.argv[1]).write_text(json.dumps(out, indent=1))
print(json.dumps(out, indent=1))
