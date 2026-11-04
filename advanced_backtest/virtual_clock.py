"""Deterministic replay clock.

Replaces time.monotonic / time.time / time.perf_counter with a single value the
driver advances explicitly. Must be installed *before* importing any module that
captures a time function at import time (altcoin_mm_v1 does this in places).

Unlike backtest/virtual_clock.py this exposes reset() so batch runs over many
pairs in one process start each replay from a clean clock.
"""
from __future__ import annotations

import time as _time

_NOW: list[float] = [0.0]
_INSTALLED: list[bool] = [False]
_REAL = {
    "monotonic": _time.monotonic,
    "time": _time.time,
    "perf_counter": _time.perf_counter,
}


def now() -> float:
    """Current virtual epoch-seconds."""
    return _NOW[0]


def set_now(ts: float) -> None:
    """Advance the clock. Backward jumps are ignored — replay streams are
    receive-time ordered, so a backward jump means a merge bug upstream and
    silently rewinding would corrupt every elapsed-time computation."""
    if ts > _NOW[0]:
        _NOW[0] = ts


def advance(delta_s: float) -> None:
    if delta_s > 0:
        _NOW[0] += delta_s


def install_virtual_clock(start_epoch: float = 0.0) -> None:
    """Idempotent. Only the first call's start_epoch is honoured; use reset()
    to rewind between replays in the same process."""
    if _INSTALLED[0]:
        return
    _NOW[0] = float(start_epoch)
    _time.monotonic = now       # type: ignore[assignment]
    _time.time = now            # type: ignore[assignment]
    _time.perf_counter = now    # type: ignore[assignment]
    _INSTALLED[0] = True


def reset(start_epoch: float = 0.0) -> None:
    """Rewind the clock without uninstalling. For batch runs."""
    _NOW[0] = float(start_epoch)


def uninstall() -> None:
    _time.monotonic = _REAL["monotonic"]      # type: ignore[assignment]
    _time.time = _REAL["time"]                # type: ignore[assignment]
    _time.perf_counter = _REAL["perf_counter"]  # type: ignore[assignment]
    _INSTALLED[0] = False


def is_installed() -> bool:
    return _INSTALLED[0]


def real_time() -> float:
    """Wall-clock time, for measuring how long the replay itself takes."""
    return _REAL["time"]()
