"""Evidence files of the EMS acceptance cases (owner 12 Nov): each case writes its stage trace to out/integration/ems_cases/<case>.jsonl, one JSON line per stage with a timestamp. Support module for the tests."""
import json
from pathlib import Path

from crypto_stat_arb.metrics import stage_trace

CASES_DIR = Path(__file__).resolve().parents[2] / "out" / "integration" / "ems_cases"


def write_case(case: str, ledger_events, market=()) -> Path:
    rows = stage_trace(ledger_events, market)
    assert rows, f"case {case}: the run produced no stage at all"
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    path = CASES_DIR / f"{case}.jsonl"
    path.write_text("".join(json.dumps(r, sort_keys=True, default=str) + "\n" for r in rows))
    return path
