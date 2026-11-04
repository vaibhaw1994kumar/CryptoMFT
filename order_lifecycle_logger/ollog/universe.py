"""Which coins to test: the Bybit export's coins, restricted to those tradable on every selected testnet market."""
from __future__ import annotations

import re
import zipfile

from .models import Instrument


def zip_coin_activity(zip_path: str) -> dict[str, int]:
    """symbol -> uncompressed size of its order_spot file (a stand-in for activity). Reads the zip's directory only."""
    out: dict[str, int] = {}
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            m = re.search(r"order_spot_([A-Z0-9]+)\.jsonl$", info.filename)
            if m:
                out[m.group(1)] = info.file_size
    return out


def choose(instruments: dict[str, dict[str, Instrument]], candidates: dict[str, int], n: int):
    """-> (chosen symbols, {dropped symbol: [reasons]}). Ranked by activity then name, so the pick is reproducible."""
    chosen: list[str] = []
    dropped: dict[str, list[str]] = {}
    for sym in sorted(candidates, key=lambda s: (-candidates[s], s)):
        missing = [m for m, table in instruments.items() if sym not in table]
        if missing:
            dropped[sym] = [f"not tradable (post-only, USDT, TRADING) on {m}" for m in missing]
        elif len(chosen) < n:
            chosen.append(sym)
        else:
            dropped[sym] = ["eligible but beyond the requested count"]
    return chosen, dropped
