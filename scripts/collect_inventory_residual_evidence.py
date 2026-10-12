"""Collect the evidence for the residual-exposure and spot-inventory changes.

Writes ``reports/inventory_residual_evidence/`` (zipped alongside):

1_tests/            junit XML + verbose log of tests/test_residual_exposure.py and
                    tests/test_spot_inventory.py, and the full-suite summary
2_paper_residual/   per-cycle evidence from the 44 recorded live paper cycles:
                    headline net_pnl vs marked equity PnL, end-of-cycle residual
3_replay_residual/  cycles 0006-0010 replayed before (aggregate-bound fix absent)
                    and after: residual, unwinds, net_pnl vs equity, kill switch
4_replay_inventory/ cycles 0006-0010 replayed with pre-funded 0.05 / 0.01 BTC
5_synthetic_drain/  one-directional synthetic stream with pre-funded inventory
6_acceptance/       scripts/run_all_local.py summary with the new configs
7_code/             the three commits as patches + the changed configs

Replay folders are read from the scratch directory given by --replay-dir
(``fix_*`` = before, ``res_*`` = after, ``inv0.05_*`` / ``inv0.01_*``).
"""

from __future__ import annotations

import argparse
import collections
import csv
from decimal import Decimal as D
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

CYCLES = ("0006-20260914T142731Z", "0007-20260914T145802Z", "0008-20260914T152833Z",
          "0009-20260914T155903Z", "0010-20260914T162934Z")


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def equity(account_path: Path) -> tuple[float, float]:
    account = json.loads(account_path.read_text())
    pnl = sum(D(v["equity"]) - D(v["initial_capital"]) for v in account["venues"].values())
    residual = sum(D(p["quantity"]) for p in account["positions"].values())
    return float(pnl), float(residual)


def ledger_stats(path: Path) -> dict:
    rows = [json.loads(line) for line in path.open()]
    roles = collections.Counter(
        (row.get("order") or {}).get("order_role") for row in rows if row["event_type"] == "FILL"
    )
    events = collections.Counter(row["event_type"] for row in rows)
    refusals = collections.Counter(
        row["reason"] for row in rows if row["event_type"] == "PACKAGE_RISK_REJECTED"
    )
    return {
        "primary_fills": roles.get("PRIMARY", 0), "unwind_fills": roles.get("UNWIND", 0),
        "residual_detected": events.get("RESIDUAL_DETECTED", 0),
        "residual_within_bound": events.get("RESIDUAL_WITHIN_APPROVED_BOUND", 0),
        "residual_limit_breach": events.get("RESIDUAL_LIMIT_BREACH", 0),
        "risk_refusals": sum(refusals.values()), "refusal_reasons": dict(refusals),
    }


def tests(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    env = {"PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'tests'}", "PATH": "/usr/bin:/bin"}
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_residual_exposure.py",
         "tests/test_spot_inventory.py", "-v", "-p", "no:cacheprovider", "-p", "no:logging",
         "--durations=0", f"--junitxml={out / 'junit_item1_item2.xml'}"],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )
    (out / "pytest_item1_item2.log").write_text(run.stdout + run.stderr)
    suite = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--ignore=tests/platform_kit", "-q",
         "-p", "no:cacheprovider", "-p", "no:logging"],
        cwd=ROOT, capture_output=True, text=True, env={**env, "PYTHONPATH": str(ROOT / "src")},
    )
    (out / "pytest_full_suite.log").write_text(suite.stdout[-4000:] + suite.stderr[-2000:])
    import xml.etree.ElementTree as ET
    root = ET.parse(out / "junit_item1_item2.xml").getroot()
    suite_node = root if root.tag == "testsuite" else root.find("testsuite")
    cases = []
    for case in suite_node.iter("testcase"):
        status = "FAILED" if case.find("failure") is not None or case.find("error") is not None else (
            "SKIPPED" if case.find("skipped") is not None else "PASSED")
        cases.append({"file": case.get("classname").split(".")[-2] + ".py",
                      "class": case.get("classname").split(".")[-1], "test": case.get("name"),
                      "seconds": float(case.get("time", 0)), "status": status})
    write_csv(out / "test_cases.csv", cases)
    return {"item_tests": len(cases), "passed": sum(c["status"] == "PASSED" for c in cases),
            "exit_code": run.returncode, "full_suite": suite.stdout.strip().splitlines()[-1],
            "cases": cases}


def paper_residual(out: Path) -> dict:
    rows = []
    for account_path in sorted((ROOT / "runs/paper_watch/live").glob("cycle-*/paper/account_state.json")):
        cycle = account_path.parts[-3]
        summary = json.loads((account_path.parent / "summary.json").read_text())
        pnl, residual = equity(account_path)
        rows.append({"cycle": cycle, "headline_net_pnl": float(D(str(summary.get("net_pnl", 0)))),
                     "equity_pnl": round(pnl, 6), "end_residual_base": residual,
                     "overstatement": round(float(D(str(summary.get("net_pnl", 0)))) - pnl, 6)})
    write_csv(out / "paper_cycles.csv", rows)
    return {"cycles": len(rows), "sum_headline_net_pnl": round(sum(r["headline_net_pnl"] for r in rows), 2),
            "sum_equity_pnl": round(sum(r["equity_pnl"] for r in rows), 2),
            "max_abs_end_residual": max(abs(r["end_residual_base"]) for r in rows),
            "cycles_with_end_residual": sum(r["end_residual_base"] != 0 for r in rows), "rows": rows}


def replay_residual(out: Path, replay: Path) -> dict:
    rows = []
    for name in CYCLES:
        key = name[:4]
        paper = ROOT / "runs/paper_watch/live" / f"cycle-{name}" / "paper"
        paper_summary = json.loads((paper / "summary.json").read_text())
        p_eq, p_res = equity(paper / "account_state.json")
        for label, folder in (("before", replay / f"fix_{key}"), ("after", replay / f"res_{key}")):
            summary = json.loads((folder / "summary.json").read_text())
            eq, res = equity(folder / "account_state.json")
            kill = json.loads((folder / "kill_switch_status.json").read_text())
            stats = ledger_stats(folder / "ledger.jsonl")
            rows.append({
                "cycle": key, "run": label, "opportunities": summary["opportunities"],
                "net_pnl": round(float(D(str(summary["net_pnl"]))), 4), "equity_pnl": round(eq, 4),
                "net_minus_equity": round(float(D(str(summary["net_pnl"]))) - eq, 4),
                "end_residual_base": res, "kill_switch": kill.get("state"),
                **{k: v for k, v in stats.items() if k != "refusal_reasons"},
                "paper_opportunities": paper_summary["opportunities"],
                "paper_net_pnl": round(float(D(str(paper_summary["net_pnl"]))), 4),
                "paper_equity_pnl": round(p_eq, 4), "paper_end_residual_base": p_res,
            })
    write_csv(out / "replay_before_after.csv", rows)
    return {"rows": rows}


def replay_inventory(out: Path, replay: Path) -> dict:
    rows = []
    for name in CYCLES:
        key = name[:4]
        base_eq, _ = equity(replay / f"res_{key}" / "account_state.json")
        base_opps = json.loads((replay / f"res_{key}" / "summary.json").read_text())["opportunities"]
        for level in ("0.05", "0.01"):
            folder = replay / f"inv{level}_{key}"
            summary = json.loads((folder / "summary.json").read_text())
            eq, _ = equity(folder / "account_state.json")
            venues = summary["inventory"]["venues"]
            stats = ledger_stats(folder / "ledger.jsonl")
            rows.append({
                "cycle": key, "initial_base_per_venue": level,
                "opportunities_unconstrained": base_opps, "opportunities_pre_funded": summary["opportunities"],
                "risk_refusals": stats["risk_refusals"],
                "equity_pnl_unconstrained": round(base_eq, 4), "equity_pnl_pre_funded": round(eq, 4),
                "end_base_binance": venues["BINANCE"]["held_base"], "end_base_bybit": venues["BYBIT"]["held_base"],
                "end_quote_binance": venues["BINANCE"]["held_quote"], "end_quote_bybit": venues["BYBIT"]["held_quote"],
                "label": summary["inventory"]["label"],
            })
    write_csv(out / "inventory_levels.csv", rows)
    return {"rows": rows}


def synthetic_drain(out: Path) -> dict:
    from crypto_stat_arb.benchmark import synthetic_book_events
    from crypto_stat_arb.config import load_config
    from crypto_stat_arb.models import MarketEvent
    from crypto_stat_arb.replay import deterministic_order
    from crypto_stat_arb.risk import InventoryPolicy
    from crypto_stat_arb.session import TradingSession
    from dataclasses import replace

    events = deterministic_order([MarketEvent.from_dict(r) for r in synthetic_book_events(6_000, seed=11)])
    result = {}
    for label in ("pre_funded_0.05", "unconstrained"):
        cfg, _ = load_config(ROOT / "config/paper_watch.json")
        if label == "unconstrained":
            cfg = replace(cfg, inventory_policy=InventoryPolicy())
        session = TradingSession(cfg)
        for event in events:
            session.on_event(event)
        session.drain()
        plans = collections.Counter(p.opportunity.strategy_id for p in session.oms.plans.values())
        refusals = collections.Counter(
            r["reason"] for r in session.ledger.events if r["event_type"] == "PACKAGE_RISK_REJECTED")
        result[label] = {
            "plans_by_strategy": dict(plans), "refusals": dict(refusals),
            "inventory": session.risk.inventory_snapshot(),
            "ledger_positions": {f"{v}:{i}": str(q) for (v, i), q in session.ledger.positions.items()},
        }
    (out / "synthetic_drain.json").parent.mkdir(parents=True, exist_ok=True)
    (out / "synthetic_drain.json").write_text(json.dumps(result, indent=2, default=str))
    return result


def acceptance(out: Path) -> dict:
    runs = sorted((ROOT / "runs").glob("local-all-*"))
    latest = runs[-1]
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(latest / "local_acceptance_summary.json", out / "local_acceptance_summary.json")
    labels = {}
    for summary_path in sorted(latest.rglob("summary.json")):
        inventory = json.loads(summary_path.read_text()).get("inventory")
        if inventory:
            labels[str(summary_path.relative_to(latest))] = {
                "label": inventory["label"],
                "held_base": {v: x["held_base"] for v, x in inventory["venues"].items()},
            }
    (out / "inventory_labels.json").write_text(json.dumps(labels, indent=2))
    summary = json.loads((latest / "local_acceptance_summary.json").read_text())
    return {"run": latest.name, "summary": summary, "labels": labels}


def code(out: Path, staging: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    commits = []
    for sha in ("40b52d4", "fa70b81", "8d66ce7"):
        patch = subprocess.run(["git", "format-patch", "-1", sha, "--stdout"], cwd=staging,
                               capture_output=True, text=True).stdout
        subject = subprocess.run(["git", "log", "-1", "--format=%s", sha], cwd=staging,
                                 capture_output=True, text=True).stdout.strip()
        stat = subprocess.run(["git", "show", "--stat", "--format=", sha], cwd=staging,
                              capture_output=True, text=True).stdout.strip()
        (out / f"{sha}.patch").write_text(patch)
        commits.append({"sha": sha, "subject": subject, "stat": stat})
    for name in ("research.json", "paper_watch.json"):
        shutil.copy(ROOT / "config" / name, out / name)
    return {"commits": commits}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay-dir", type=Path, required=True)
    parser.add_argument("--staging-repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "inventory_residual_evidence")
    options = parser.parse_args()
    out = options.output
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    summary = {
        "tests": tests(out / "1_tests"),
        "paper_residual": paper_residual(out / "2_paper_residual"),
        "replay_residual": replay_residual(out / "3_replay_residual", options.replay_dir),
        "replay_inventory": replay_inventory(out / "4_replay_inventory", options.replay_dir),
        "synthetic_drain": synthetic_drain(out / "5_synthetic_drain"),
        "acceptance": acceptance(out / "6_acceptance"),
        "code": code(out / "7_code", options.staging_repo),
    }
    files = {str(p.relative_to(out)): {"bytes": p.stat().st_size, "sha256": sha256(p.read_bytes()).hexdigest()}
             for p in sorted(out.rglob("*")) if p.is_file()}
    summary["files"] = files
    (out / "evidence_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    archive = shutil.make_archive(str(out), "zip", out.parent, out.name)
    print(out, archive, len(files), "files")


if __name__ == "__main__":
    main()
