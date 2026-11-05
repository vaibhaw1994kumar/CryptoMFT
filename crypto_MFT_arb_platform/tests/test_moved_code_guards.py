"""Guards that read engine SOURCE FILES by path and would silently lose coverage when code moves from src/new_backtest to src/crypto_stat_arb (Phase A of the shared-platform work).

test_maker_observations.py::test_nothing_in_the_engine_reads_the_observation scans only src/new_backtest. fill_engine.py (the producer of fill.obs) now lives in
crypto_stat_arb/simulation, where that test no longer looks. This file repeats the same scan over the moved code, without editing the original test."""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
ALLOWED = (r"self\.obs = ObservationLog\(\)", r"self\.obs\.add\(", r"self\.obs\.summary\(", r"self\.obs\.write_jsonl\(",
           r"dict\(fill\.obs or \{\}\)", r"fill\.obs = \{", r"fill\.obs = ")


def test_nothing_in_the_moved_engine_reads_the_observation():
    scanned = 0
    for root in (SRC / "crypto_stat_arb" / "simulation", SRC / "crypto_stat_arb" / "ems", SRC / "crypto_stat_arb" / "strategies"):
        for f in root.rglob("*.py"):
            scanned += 1
            for n, line in enumerate(f.read_text().splitlines(), 1):
                code = line.split("#", 1)[0]
                if re.search(r"\.obs\b(?!_)", code) and not any(re.search(a, code) for a in ALLOWED):
                    raise AssertionError(f"{f.name}:{n} touches an observation outside the allowed write paths: {line.strip()}")
    assert scanned >= 15, "the scan found too few moved files"
