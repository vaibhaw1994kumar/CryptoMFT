"""Economics acceptance gate: a run's P&L is not accepted unless its economics reconcile.

Reads a run directory's ``economic_reconciliation.json`` (plus
``funding_report.json`` and ``borrow_report.json`` when present) and returns
PASS or FAIL:

* reconciliation ``FAIL`` (an identity or cross-check broke), a missing
  reconciliation, borrow ``FAIL`` (an unfinanced short) -> FAIL, always;
* ``INCOMPLETE`` -> FAIL, unless *every* reason is in ``allow_incomplete``
  with a written justification, which the result lists. Reasons:

  ``MISSING_PRICE``      a balance could not be valued at the opening/closing mark
  ``FEE_ASSET_UNPRICED`` a fee in a third token had no price
  ``FUNDING_DATA``       a settlement with a position had no rate / stale rate / no mark,
                         or a perpetual has no funding schedule
  ``BORROW_DATA``        borrow interest could not be valued, or a buy-in had no price

Funding's ``economic_status`` is what counts. Data gaps while flat are
reported, not gated (see funding.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

REASONS = ("MISSING_PRICE", "FEE_ASSET_UNPRICED", "FUNDING_DATA", "BORROW_DATA")


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def incomplete_reasons(report: dict[str, Any], funding: dict[str, Any] | None) -> dict[str, list[str]]:
    reasons: dict[str, list[str]] = {}
    for venue, row in report.get("venues", {}).items():
        for item in row.get("missing_prices", []):
            reasons.setdefault("MISSING_PRICE", []).append(item)
        for item in row.get("unvalued_fee_events", []):
            reasons.setdefault("FEE_ASSET_UNPRICED", []).append(f"{venue}:{item}")
        for item in row.get("funding_flags", []):
            reasons.setdefault("FUNDING_DATA", []).append(f"{venue}:{item}")
        for item in row.get("unvalued_borrow_accruals", []):
            reasons.setdefault("BORROW_DATA", []).append(f"{venue}:unvalued accrual {item}")
        for item in row.get("borrow_flags", []):
            if not item.startswith("UNFINANCED_SHORT"):
                reasons.setdefault("BORROW_DATA", []).append(f"{venue}:{item}")
    if funding and funding.get("economic_status", funding.get("status")) == "INCOMPLETE":
        reasons.setdefault("FUNDING_DATA", [])
    if funding and funding.get("configuration") == "INVALID":
        reasons.setdefault("FUNDING_DATA", []).extend(
            f"no funding schedule: {item}" for item in funding.get("unscheduled_instruments", [])
        )
    return reasons


def economics_gate(run_dir: Path, *, allow_incomplete: Mapping[str, str] | None = None) -> dict[str, Any]:
    allow = dict(allow_incomplete or {})
    unknown = sorted(set(allow) - set(REASONS))
    if unknown:
        raise ValueError(f"unknown allow_incomplete reason(s) {unknown}; use {list(REASONS)}")
    if any(not str(why).strip() for why in allow.values()):
        raise ValueError("every allowed INCOMPLETE reason needs a written justification")
    report = _read(run_dir / "economic_reconciliation.json")
    funding = _read(run_dir / "funding_report.json")
    borrow = _read(run_dir / "borrow_report.json")
    result: dict[str, Any] = {
        "run_dir": str(run_dir),
        "reconciliation": None if report is None else report["status"],
        "funding": None if funding is None else funding.get("economic_status", funding.get("status")),
        "funding_data_coverage": None if funding is None else funding.get("data_coverage"),
        "borrow": None if borrow is None else borrow.get("status"),
        "failures": [],
        "incomplete_reasons": {},
        "allowed_incomplete": {},
    }
    if report is None:
        result["failures"].append("economic_reconciliation.json missing: economics were not reconciled")
    else:
        result["failures"] += [
            f"{check['check']}: {check['detail']}" for check in report.get("checks", []) if not check["passed"]
        ]
        if report["status"] == "FAIL" and not result["failures"]:
            result["failures"].append("reconciliation status FAIL")
        reasons = incomplete_reasons(report, funding)
        if report["status"] == "INCOMPLETE" and not reasons:
            reasons = {"UNCLASSIFIED": ["reconciliation INCOMPLETE without a classified reason"]}
        result["incomplete_reasons"] = reasons
        for reason, items in reasons.items():
            if reason in allow:
                result["allowed_incomplete"][reason] = {"justification": allow[reason], "items": items}
            else:
                result["failures"].append(f"INCOMPLETE {reason}: {'; '.join(items)[:500] or 'see report'}")
    if borrow is not None and borrow.get("status") == "FAIL":
        result["failures"].append("borrow FAIL: " + "; ".join(
            f"{f['code']}@{f['ts_ns']}" for f in borrow.get("flags", []))[:500])
    result["decision"] = "FAIL" if result["failures"] else "PASS"
    return result
