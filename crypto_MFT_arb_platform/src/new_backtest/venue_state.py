"""Temporary re-export (Phase D, removed in Phase F): this module now lives in crypto_stat_arb.simulation.venue_state. `new_backtest.venue_state` is the same module object."""
import importlib as _importlib
import sys as _sys

_sys.modules[__name__] = _importlib.import_module("crypto_stat_arb.simulation.venue_state")
