"""Temporary re-export (Phase C, removed in Phase F): this module now lives in crypto_stat_arb.venue_semantics_perp. `new_backtest.venue_semantics_perp` is the same module object."""
import importlib as _importlib
import sys as _sys

_sys.modules[__name__] = _importlib.import_module("crypto_stat_arb.venue_semantics_perp")
