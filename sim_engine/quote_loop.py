"""
Quote loop bodies extracted from FAST_MM/main_bybit.py (BybitMMSession).

These belong inside a session class that holds:
  self.engines          — dict[symbol, PairEngine]
  self.quote_interval_s — float (e.g. 0.2 for 200ms)

Usage in your session class:
    import asyncio
    from quote_loop import quote_loop, quote_loop_fast

    async def run(self):
        await asyncio.gather(
            self._quote_loop(),
            self._quote_loop_fast(),
        )

on_quote_tick() is the full quoting method on PairEngine (~3700 lines in the
original). Copy the PairEngine class from FAST_MM/main_bybit.py (lines 1–5198)
if you need the complete strategy logic, or implement your own version that
calls order_manager.simulate_dry_fills() on each tick.
"""

import asyncio


# ── Pairs that get a dedicated 50ms fast loop ─────────────────────────────────
# Calibrated against session data: BTC/ETH benefit most from sub-200ms quoting.
# XRP / SOL moved back to the 200ms pool — faster polling made PnL worse.
_FAST_POLL_PAIRS: tuple = ("BTC/USDT", "ETH/USDT")
_FAST_POLL_INTERVAL_S: float = 0.05  # 50ms


async def _quote_loop(session) -> None:
    """Main quote loop: calls on_quote_tick() for every pair at quote_interval_s."""
    while True:
        for engine in session.engines.values():
            await engine.on_quote_tick()
        await asyncio.sleep(session.quote_interval_s)


async def _quote_loop_fast(session) -> None:
    """50ms loop for BTC/ETH only. Run alongside _quote_loop."""
    fast_engines = [session.engines[p] for p in _FAST_POLL_PAIRS if p in session.engines]
    if not fast_engines:
        return
    while True:
        for engine in fast_engines:
            await engine.on_quote_tick()
        await asyncio.sleep(_FAST_POLL_INTERVAL_S)
