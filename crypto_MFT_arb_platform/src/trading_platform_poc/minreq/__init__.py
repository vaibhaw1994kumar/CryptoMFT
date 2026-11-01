"""Minimum-requirements platform layer for MFT cross-venue statistical arbitrage.

Implements the capability matrix MR-01 to MR-14 and the acceptance tests
AT-01 to AT-14 defined by the *Minimal Requirements: MFT Stat Arb Backtesting
and Paper Trading* specification (document version 2.1).

The layer is deliberately narrow: one strategy, one logical portfolio, at
least two venues, and one canonical event/instrument/order model shared by
research, backtest and paper mode.
"""

from __future__ import annotations

DOCUMENT_VERSION = "2.1"
LAYER_VERSION = "1.0.0"

__all__ = ["DOCUMENT_VERSION", "LAYER_VERSION"]
