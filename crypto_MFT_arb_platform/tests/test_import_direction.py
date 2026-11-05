"""Import direction of the shared platform (owner design rule, 5 Nov 2026): new_backtest may import crypto_stat_arb; crypto_stat_arb never imports new_backtest.

The only exception is src/crypto_stat_arb/sdk.py (BacktestAPI keeps lazy imports of the runner until Phase F). Two checks: no source file of the core mentions an import of
new_backtest (any depth, lazy or not, TYPE_CHECKING included), and importing the core package pulls in no new_backtest module.
Not scanned: the stale nested copy, the older backtester, the core's own tests/docs/scripts/runs trees."""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
CORE = SRC / "crypto_stat_arb"
EXCLUDED = {"advanced_backtest", "crypto_MFT_arb_platform", "src", "tests", "docs", "scripts", "runs", "reports", "examples", "config", "data", "cpp", "altcoin_mm_v1", "__pycache__"}
ALLOWED = {"sdk.py"}


def core_files():
    for p in sorted(CORE.rglob("*.py")):
        rel = p.relative_to(CORE)
        if rel.parts[0] in EXCLUDED:
            continue
        yield p, rel


def imports_new_backtest(path: Path) -> list[int]:
    lines = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "new_backtest" for a in node.names):
            lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] == "new_backtest":
            lines.append(node.lineno)
    return lines


def test_the_core_does_not_import_new_backtest_except_through_sdk():
    scanned = 0
    offenders = {}
    for p, rel in core_files():
        scanned += 1
        hits = imports_new_backtest(p)
        if hits and rel.as_posix() not in ALLOWED:
            offenders[rel.as_posix()] = hits
    assert scanned > 60, "the scan found too few core files: the exclusion list is wrong"
    assert offenders == {}, f"crypto_stat_arb imports new_backtest in: {offenders}"


def test_the_allowed_exception_is_only_the_lazy_runner_import_in_sdk():
    hits = imports_new_backtest(CORE / "sdk.py")
    tree = ast.parse((CORE / "sdk.py").read_text())
    top_level = {n.lineno for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))}
    assert hits and not (set(hits) & top_level), "sdk.py may import new_backtest only lazily, inside functions"


def test_importing_the_core_package_loads_no_new_backtest_module():
    code = "import sys, crypto_stat_arb; print(sorted(m for m in sys.modules if m.split('.')[0] == 'new_backtest'))"
    out = subprocess.run([sys.executable, "-c", code], cwd=SRC, capture_output=True, text=True, env={"PYTHONPATH": str(SRC), "PATH": ""}, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", out.stdout
