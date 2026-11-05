"""Temporary re-export (Phase A, removed in Phase F): this module now lives in crypto_stat_arb.ems.ioc_policy. `new_backtest.execution.ioc_policy` is the same module object."""
import importlib as _importlib
import sys as _sys

_sys.modules[__name__] = _importlib.import_module("crypto_stat_arb.ems.ioc_policy")
