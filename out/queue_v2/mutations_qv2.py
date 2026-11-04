"""Queue model v2 step 10 mutation evidence: copy the engine sources to a temp tree, break one rule textually, run the queue-v2 tests, and require that at least one fails.
usage: python3.12 mutations_qv2.py OUT.json"""
import json, shutil, subprocess, sys, tempfile
from pathlib import Path

P = Path("/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform")
TESTS = ["test_queue_v2_ledger.py", "test_queue_v2_cohort.py", "test_queue_v2_cohort_executor.py", "test_queue_v2_invariants.py", "test_queue_v2_cancel_models.py",
         "test_queue_v2_hypothetical_order.py", "test_public_book_invariant.py"]
MUT = {
    "REQUIRED_break_the_credit_expiry (a snapshot no longer invalidates credit)": [("fill_engine.py", "        if cfg.ledger_epoch:\n            self.reset_credits(venue)\n", "        if False:\n            self.reset_credits(venue)\n")],
    "REQUIRED_let_a_vanished_level_fill (no evidence gate)": [("fill_engine.py", "        if vanish or cfg.fill_evidence in (\"trade_confirm\", \"trade_confirm+fractional\"):\n            order.front_ts = now if credit_ts is None else credit_ts",
                                                              "        if False:\n            order.front_ts = now if credit_ts is None else credit_ts")],
    "REQUIRED_double_spend_a_print_across_two_own_orders (cohort traversal)": [("queue_v2/cohort.py", "            take = min(i[\"size\"], rem)\n            i[\"size\"] -= take\n            rem -= take\n            if i[\"t\"] == \"own\" and take > EPS:",
                                                                                "            take = min(i[\"size\"], rem)\n            i[\"size\"] -= take\n            rem -= take if i[\"t\"] == \"ext\" else 0.0\n            if i[\"t\"] == \"own\" and take > EPS:")],
    "REQUIRED_double_spend_a_print_across_two_own_orders (scalar shared budget)": [("fill_engine.py", "                remaining -= absorbed\n", "                pass\n")],
    "REQUIRED_mutate_the_public_book_with_an_own_fill (overlay off, real consume)": [("fill_engine.py", "walk = (book.consume_overlay if self.own_impact == \"overlay\" else book.consume)(book_side, qty, limit_price)",
                                                                                  "walk = book.consume(book_side, qty, limit_price)")],
    "REQUIRED_mutate_the_public_book_with_an_own_fill (flag off still consumes)": [("fill_engine.py", "        else:\n            walk = book.walk(book_side, qty, limit_price)", "        else:\n            walk = book.consume(book_side, qty, limit_price)")],
    "cohort_new_size_pushes_us_back (additions join in FRONT)": [("queue_v2/cohort.py", "            self.items.append({\"t\": \"ext\", \"size\": float(size), \"birth\": now})", "            self.items.insert(0, {\"t\": \"ext\", \"size\": float(size), \"birth\": now})")],
    "cohort_reset_cancels_the_own_order": [("queue_v2/cohort.py", "        elif displayed < ext - EPS:\n            removed = self.cancel_external(ext - displayed, now, hypothesis, theta)", "        elif displayed < ext - EPS:\n            removed = self.cancel_external(ext - displayed, now, hypothesis, theta)\n            self.items = [i for i in self.items if i['t'] == 'ext']")],
    "behind_first_formula_wrong": [("queue_v2/cancel_models.py", "        x = min(A, max(0.0, C - B))", "        x = min(C, A)")],
    "pre_arrival_print_can_fill": [("fill_engine.py", "        return (self.config.exchange_time_eligibility and event_ts is not None\n                and event_ts + 1e-12 < order.arrival_ts)", "        return False")],
    "event_order_capture_sorts_by_kind": [("event_loader.py", "yield (float(rec.get(tkey, 0.0) or 0.0), float(rec.get(\"local_ts\", 0.0) or 0.0), n, sidx), kind, rec", "yield (float(rec.get(tkey, 0.0) or 0.0), sidx, n, float(rec.get(\"local_ts\", 0.0) or 0.0)), kind, rec")],
}
out = {}
def run(tmp):
    env = {"PYTHONPATH": f"{tmp/'src'}:{P/'tests'}", "PATH": "/usr/bin:/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin"}
    r = subprocess.run(["python3.12", "-m", "pytest", *[str(P / "tests" / t) for t in TESTS], "-q", "-p", "no:cacheprovider"], capture_output=True, text=True, env=env, cwd=str(tmp))
    return r
def tree():
    tmp = Path(tempfile.mkdtemp(prefix="qmut_", dir="/tmp"))
    shutil.copytree(P / "src" / "new_backtest", tmp / "src" / "new_backtest", ignore=shutil.ignore_patterns("__pycache__"))
    for d in ("profiles", "config"):
        shutil.copytree(P / d, tmp / d)
    return tmp
for name, edits in MUT.items():
    tmp = tree()
    for rel, old, new in edits:
        f = tmp / "src" / "new_backtest" / rel
        s = f.read_text()
        assert s.count(old) == 1, (name, rel, s.count(old))
        f.write_text(s.replace(old, new))
    r = run(tmp)
    failed = [l.split(" ")[1].split("::", 1)[1] for l in r.stdout.splitlines() if l.startswith("FAILED")]
    out[name] = {"caught": bool(failed), "failed_tests": failed, "summary": (r.stdout.strip().splitlines() or [r.stderr[-200:]])[-1]}
    shutil.rmtree(tmp, ignore_errors=True)
tmp = tree()
r = run(tmp)
out["_control_unmutated"] = {"summary": r.stdout.strip().splitlines()[-1]}
shutil.rmtree(tmp, ignore_errors=True)
Path(sys.argv[1]).write_text(json.dumps(out, indent=1))
for k, v in out.items():
    print(("CAUGHT " if v.get("caught") else ("CONTROL" if k.startswith("_") else "MISSED ")), k, "|", len(v.get("failed_tests", [])), "|", v["summary"])
