"""Lazy loading for heavy native optional dependencies.

``pyarrow`` and ``hftbacktest`` ship compiled extension modules. Importing
them at module scope means any environment that cannot load those binaries
breaks ``import hft_platform_kit`` outright, even for callers that only need
the pure-Python config, order book, OMS, risk, and EMS surfaces.

That is not hypothetical: on Windows an Application Control policy (WDAC,
AppLocker, or Smart App Control) can block ``pyarrow\\lib.pyd``, producing
``ImportError: DLL load failed while importing lib``. Loading these modules
on first use keeps the rest of the SDK usable and turns the failure into an
actionable message at the point the Parquet or backtest feature is used.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Final

_PYARROW_HINT: Final = (
    "Install it with `pip install pyarrow`. If pyarrow is already installed "
    "and the import fails with 'DLL load failed', a Windows Application "
    "Control policy (WDAC/AppLocker/Smart App Control) is most likely "
    "blocking pyarrow's native library; ask your administrator to allow it, "
    "or use the JSONL dataset paths, which need no native dependency."
)
_HFTBACKTEST_HINT: Final = "Install it with `pip install hftbacktest`."
_NUMPY_HINT: Final = (
    "Install it with `pip install numpy`. If numpy is already installed and "
    "the import fails with 'DLL load failed', a Windows Application Control "
    "policy is most likely blocking its native library."
)
_NUMBA_HINT: Final = "Install it with `pip install numba`."


class OptionalDependencyError(ImportError):
    """Raised when a feature needs an optional dependency that cannot load."""


def _require(module_name: str, feature: str, hint: str) -> ModuleType:
    try:
        return importlib.import_module(module_name)
    except ImportError as error:
        raise OptionalDependencyError(
            f"{feature} requires the optional dependency "
            f"'{module_name}', which could not be imported "
            f"({error}). {hint}"
        ) from error


def require_pyarrow() -> ModuleType:
    """Return the ``pyarrow`` module or raise an actionable error."""

    return _require("pyarrow", "Parquet support", _PYARROW_HINT)


def require_pyarrow_parquet() -> ModuleType:
    """Return the ``pyarrow.parquet`` module or raise an actionable error."""

    require_pyarrow()
    return _require("pyarrow.parquet", "Parquet support", _PYARROW_HINT)


def require_hftbacktest() -> ModuleType:
    """Return the ``hftbacktest`` module or raise an actionable error."""

    return _require(
        "hftbacktest",
        "hftbacktest export and health reporting",
        _HFTBACKTEST_HINT,
    )


def require_hftbacktest_snapshot() -> ModuleType:
    """Return HftBacktest snapshot utilities or raise an actionable error."""

    require_hftbacktest()
    return _require(
        "hftbacktest.data.utils.snapshot",
        "HftBacktest snapshot generation",
        _HFTBACKTEST_HINT,
    )


def require_hftbacktest_binance_futures() -> ModuleType:
    """Return the HftBacktest Binance Futures converter."""

    require_hftbacktest()
    return _require(
        "hftbacktest.data.utils.binancefutures",
        "Binance Futures raw-feed conversion",
        _HFTBACKTEST_HINT,
    )


def require_hftbacktest_tardis() -> ModuleType:
    """Return the HftBacktest Tardis converter."""

    require_hftbacktest()
    return _require(
        "hftbacktest.data.utils.tardis",
        "Tardis CSV conversion",
        _HFTBACKTEST_HINT,
    )


def require_hftbacktest_stats() -> ModuleType:
    """Return HftBacktest statistics or raise an actionable error."""

    require_hftbacktest()
    return _require(
        "hftbacktest.stats",
        "HftBacktest performance statistics",
        _HFTBACKTEST_HINT,
    )


def require_numpy() -> ModuleType:
    """Return the ``numpy`` module or raise an actionable error."""

    return _require("numpy", "Array export", _NUMPY_HINT)


def require_numba() -> ModuleType:
    """Return ``numba`` or raise an actionable error."""

    return _require(
        "numba",
        "compiled HftBacktest strategies and analytics",
        _NUMBA_HINT,
    )
