"""The hedge-failure catalogue (tests/integration_hedge_failure_catalogue.json) is the Phase C acceptance list. This test keeps it honest in the small ways a machine can:
40 unique cases, every case names its reports, and every test it cites exists in the tests directory (existence only, not coverage)."""
from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOC = json.loads((HERE / "integration_hedge_failure_catalogue.json").read_text())


def test_forty_unique_cases_each_with_reports_and_a_decision_or_note():
    ids = [c["id"] for c in DOC["cases"]]
    assert len(ids) == 40 and len(set(ids)) == 40
    for c in DOC["cases"]:
        assert c["case"] and c["note"] and c["scenario"], c["id"]
        assert c["reports"] or c["id"] in {"H25"}, c["id"]           # H25 (flatten on trip) comes from the plan, not a report


def test_every_cited_test_exists():
    missing = []
    for c in DOC["cases"]:
        for ref in c["backtester"]["tests"] + c["core"]["tests"]:
            f, name = ref.lstrip("~").split("::", 1)
            path = HERE / Path(f).name if (HERE / Path(f).name).exists() else HERE / f
            if not path.exists():
                missing.append((c["id"], ref)); continue
            text = path.read_text()
            base = name.split("[")[0]
            if not re.search(rf"def {re.escape(base)}\b", text):
                missing.append((c["id"], ref))
    assert missing == []
