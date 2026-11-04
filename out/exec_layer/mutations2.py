"""Phase-0 hardening mutation evidence (gate 0 + 4h). For each named mutation: copy the engine sources to a temp tree, apply a textual change that removes the
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
    "allow_two_live_hedge_children": [("execution/layer.py", "                if self.cfg.one_hedge_child == \"enforce\":", "                if False:")],
    "create_hedge_before_the_fill": [("execution/layer.py", "            if parent_filled_qty < qty - 1e-9 and self.dispatch_mode != \"simultaneous\":", "            if False:")],
    "risk_check_disabled": [("execution/layer.py", "        if self.cfg.risk_reservation == \"off\" or self._limits_fn is None or self._base_fn is None:\n            return",
                             "        if True:\n            return")],
    "reservation_not_recorded (check and reserve split)": [("execution/layer.py", "            self.reservations.reserve(it.intent_id, sp[\"venues\"][0], sp[\"side\"], sp[\"qty\"])\n", "")],
    "reservation_never_released": [("execution/risk.py", "        r = self.held.pop(intent_id, None)\n        if r is None:", "        r = None\n        if r is None:")],
    "illegal_transitions_allowed": [("execution/intent.py", "        if to not in TRANSITIONS[self.state]:", "        if False:")],
    "ghost_does_not_make_the_intent_unknown": [("execution/layer.py", "                self._move(it, IntentState.UNKNOWN, release_ts, \"child given up but still live at the venue\")", "                pass")],
    "retry_not_deferred_for_a_live_ghost": [("backtester.py", "        if self.xl.cfg.one_hedge_child != \"enforce\" or not order.is_hedge or order.role == \"unwind\":\n            return None",
                                              "        return None")],
    "retarget_looser_than_touch_slack_allowed": [("execution/layer.py", "        if (intent.role == Role.HEDGE and touch is not None and max_walk_bps is not None and worst_price is not None):", "        if False:")],
    "retry_beyond_the_unfilled_remainder_allowed": [("execution/layer.py", "            if self.cfg.one_hedge_child == \"enforce\" and intent.role == Role.HEDGE:", "            if False:")],
    "placement_ignores_the_edge_cap": [("execution/tactics/passive.py", "    px = min(px, edge_price) if buy else max(px, edge_price)\n", "    px = px\n")],
    "placement_may_cross_the_book": [("execution/tactics/passive.py", "    if (buy and px >= best_ask) or (not buy and px <= best_bid):\n        px = touch\n", "")],
    "taker_consumes_the_public_book_with_the_flag_off": [("fill_engine.py", "        walk = book.consume(book_side, qty, limit_price) if self.consume_liquidity \\", "        walk = book.consume(book_side, qty, limit_price) if True \\")],
    "reconcile_ignores_a_wrong_fee": [("execution/reconcile.py", "        if abs(expected - r[\"fee\"]) > 1e-9 * max(1.0, q * px):", "        if False:")],
    "reconcile_ignores_a_fill_beyond_its_cap": [("execution/reconcile.py", "        if kind == \"child\" and r[\"taker\"] and cap is not None:", "        if False:")],
    "reconcile_ignores_the_ledger_cash": [("execution/reconcile.py", "        if not ok_c:\n            add(\"cash\"", "        if False:\n            add(\"cash\"")],
}
TESTS = ["test_execution_layer.py", "test_execution_contract.py", "test_maker_observations.py", "test_public_book_invariant.py"]
out = {}
for name, edits in MUTATIONS.items():
    tmp = Path(tempfile.mkdtemp(prefix="mut_", dir="/tmp"))
    shutil.copytree(P / "src" / "new_backtest", tmp / "src" / "new_backtest", ignore=shutil.ignore_patterns("__pycache__"))
    for d in ("profiles", "config"):
        shutil.copytree(P / d, tmp / d)
    for rel, old, new in edits:
        f = tmp / "src" / "new_backtest" / rel
        s = f.read_text()
        assert s.count(old) == 1, (name, rel, s.count(old))
        f.write_text(s.replace(old, new))
    env = {"PYTHONPATH": f"{tmp/'src'}:{P/'tests'}", "PATH": "/usr/bin:/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin"}
    r = subprocess.run(["python3.12", "-m", "pytest", *[str(P / "tests" / t) for t in TESTS], "-q", "-p", "no:cacheprovider"],
                       capture_output=True, text=True, env=env, cwd=str(tmp))
    failed = [l.split(" ")[1].split("::",1)[1] for l in r.stdout.splitlines() if l.startswith("FAILED")]
    out[name] = {"caught": bool(failed), "failed_tests": failed, "summary": r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-300:]}
    shutil.rmtree(tmp, ignore_errors=True)
# control: unmutated tree passes
tmp = Path(tempfile.mkdtemp(prefix="mut_", dir="/tmp"))
shutil.copytree(P / "src" / "new_backtest", tmp / "src" / "new_backtest", ignore=shutil.ignore_patterns("__pycache__"))
for d in ("profiles", "config"):
    shutil.copytree(P / d, tmp / d)
env = {"PYTHONPATH": f"{tmp/'src'}:{P/'tests'}", "PATH": "/usr/bin:/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin"}
r = subprocess.run(["python3.12", "-m", "pytest", *[str(P / "tests" / t) for t in TESTS], "-q", "-p", "no:cacheprovider"], capture_output=True, text=True, env=env, cwd=str(tmp))
out["_control_unmutated"] = {"summary": r.stdout.strip().splitlines()[-1]}
shutil.rmtree(tmp, ignore_errors=True)
Path(sys.argv[1]).write_text(json.dumps(out, indent=1))
print(json.dumps(out, indent=1))
