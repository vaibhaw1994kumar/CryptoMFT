"""Track C item 2 (owner 11 Nov): ONE edge formula. Every arb edge (a price difference over a price, in bps) is computed in crypto_stat_arb/strategies/edge.py; every other module calls it.

The scan reads the live source trees (crypto_stat_arb, new_backtest, trading_platform_poc) and fails on any line outside edge.py that assigns a name containing edge/gross/net from an
expression of the form `(a - b) / c * 1e4` (any spelling of 1e4). Stale copies (crypto_stat_arb/src, the nested crypto_MFT_arb_platform, advanced_backtest) and test files are not live code
and are skipped. ALLOWED lists the few lines that compute a REALISED figure from fills, not an edge, each with its reason."""
from __future__ import annotations

import random
import re
from decimal import Decimal
from pathlib import Path

from crypto_stat_arb.strategies import edge

SRC = Path(__file__).resolve().parents[1] / "src"
ROOTS = (SRC / "crypto_stat_arb", SRC / "new_backtest", SRC / "trading_platform_poc")
SKIP_DIRS = {"src", "crypto_MFT_arb_platform", "advanced_backtest", "tests", "runs", "__pycache__"}   # runs/: frozen release artifacts (copied strategy code)
EDGE_FILE = SRC / "crypto_stat_arb" / "strategies" / "edge.py"
BPS = r"(?:1e4|10_?000(?:\.0)?|Decimal\(\s*[\"']10_?000[\"']\s*\))"
FORMULA = re.compile(r"^\s*(?:self\.)?(\w+)\s*=(?!=).*\)\s*/\s*[\w\.\[\]\"']+\s*\*\s*" + BPS)
EDGE_NAME = re.compile(r"(?<!h)edge|gross|net", re.IGNORECASE)          # "hedge" is not an edge
# path relative to src -> assigned name: realised figures from fills, not a quoted edge
ALLOWED = {
    ("new_backtest/backtester.py", "gross_bps"),     # realised gross of one completed signal, from its fill VWAPs (legging report)
}


def _scan() -> tuple[list[str], int]:
    hits, scanned = [], 0
    for root in ROOTS:
        for f in root.rglob("*.py"):
            if SKIP_DIRS & set(f.relative_to(root).parts[:-1]) or f == EDGE_FILE:
                continue
            scanned += 1
            for n, line in enumerate(f.read_text().splitlines(), 1):
                m = FORMULA.search(line.split("#", 1)[0])
                if m and EDGE_NAME.search(m.group(1)) and (str(f.relative_to(SRC)), m.group(1)) not in ALLOWED:
                    hits.append(f"{f.relative_to(SRC)}:{n}: {line.strip()}")
    return hits, scanned


def test_no_module_outside_edge_py_computes_an_edge():
    hits, scanned = _scan()
    assert scanned >= 100, f"the scan found too few files ({scanned})"
    assert not hits, "edge formula outside crypto_stat_arb/strategies/edge.py (call edge.cross_gross_bps or another edge.py function):\n" + "\n".join(hits)


def test_the_scan_sees_a_planted_formula():
    line = 'gross_bps = (sell_price - buy_price) / buy_price * Decimal("10000")'
    flagged = lambda code: bool((m := FORMULA.search(code)) and EDGE_NAME.search(m.group(1)))
    assert flagged(line) and flagged("        net = (a - b) / b * 10_000.0") and flagged("edge_bps = (x - y) / y * 1e4")
    assert not flagged("spread_bps = (ask - bid) / mid * 1e4") and not flagged("move = (p1 - p0) / p0 * 1e4")
    assert not flagged('move = (f - q) / q * 10_000.0 if sig_.hedge_side == "sell" else (q - f) / q * 10_000.0')
    assert not flagged("hedge_move = (a - b) / b * 1e4")


def test_cross_gross_bps_is_bit_identical_to_the_old_expressions():
    rnd = random.Random(11)
    for _ in range(20_000):
        b = rnd.uniform(0.01, 100_000.0)
        s = b * (1 + rnd.uniform(-0.01, 0.01))
        assert edge.cross_gross_bps(b, s) == (s - b) / b * 10_000.0                       # float: legacy_v0 / backtester
        bd, sd = Decimal(repr(b)), Decimal(repr(s))
        assert edge.cross_gross_bps(bd, sd) == (sd - bd) / bd * Decimal("10000")        # Decimal: live strategy and mean reversion
    g, net = edge.legacy_taker_taker_edge_bps(100.0, 100.05, 2.0, 3.0)
    assert g == (100.05 - 100.0) / 100.0 * 10_000.0 and net == g - 5.0
