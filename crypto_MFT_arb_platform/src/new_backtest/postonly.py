"""Temporary re-export (Phase A, removed in Phase F): this module now lives in crypto_stat_arb.simulation.postonly. `new_backtest.postonly` is the same module object."""
import importlib as _importlib
import sys as _sys

_sys.modules[__name__] = _importlib.import_module("crypto_stat_arb.simulation.postonly")
