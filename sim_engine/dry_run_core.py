"""
Dry-run fill simulation core — extracted from FAST_MM/bybit_order_manager.py.

Provides:
  QUEUE_POSITION_FRAC   — calibration constant
  MAKER_FEE_RATE        — Bybit PostOnly rebate (-0.5 bps)
  _QueueAwareFillModel  — per-symbol Python queue tracker (fallback to C++)
  DryRunOrderManager    — stateful wrapper: holds open orders, fill engines,
                          trade buffer; exposes simulate_dry_fills()

Integration contract (what your host project must supply):
  apply_fill(symbol, side, qty, price, fee) → None
      Called on every confirmed simulated fill.  Update your inventory here.

  pair_state: dict[str, YourPairState]
      Used for inventory checks on exit orders and vol_regime-based fill-prob
      multipliers.  YourPairState must have:
        .inventory  (float)       — base-asset position size
        .vol_regime (str)         — "calm" | "elevated" | "toxic"

  min_size(symbol) → float
      Returns the minimum lot size for a symbol (used as fill floor).
      Default implementation returns 0.0 if not overridden.

Build the C++ engine first (see CMakeLists.txt), then place the compiled
mm_matching_engine.so / .pyd next to this file.  If the C++ extension is not
found, DryRunOrderManager falls back to the pure-Python _QueueAwareFillModel.
"""

from __future__ import annotations

import math
import random
import time
from typing import Any, Callable


# ── Module-level constants ────────────────────────────────────────────────────

# Our estimated position in the queue as a fraction of visible book depth.
# 0.0 = front (unrealistic), 1.0 = back (pessimistic). 0.7 matches empirical
# passive maker data: slightly pessimistic to avoid over-filling in simulation.
QUEUE_POSITION_FRAC: float = 0.7

# Bybit linear USDT perpetual PostOnly rebate (VIP 0).
# Negative = we earn the rebate on every maker fill.
MAKER_FEE_RATE: float = -0.00005  # -0.5 bps

# ── Try to import the C++ fill engine ────────────────────────────────────────
try:
    from sim_fill_engine import make_fill_engine as _make_fill_engine, _CPP_AVAILABLE as _SIM_FILL_AVAILABLE
except ImportError:
    _make_fill_engine = None  # type: ignore
    _SIM_FILL_AVAILABLE = False


# ── Python queue-position fill model (fallback) ───────────────────────────────

class _QueueAwareFillModel:
    """Per-symbol queue-position-aware fill simulator (pure Python).

    Tracks each resting order's position in the queue and fills it only
    when enough taker volume has traded through its price level.

    State per open order:
      queue_ahead   — volume resting ahead of us at placement time
      vol_consumed  — cumulative taker volume at or through our price since placement
      filled_qty    — how much of this order has already been partially filled
    """

    def __init__(self) -> None:
        # oid → {"side", "price", "size", "queue_ahead", "vol_consumed", "filled_qty"}
        self._orders: dict[str, dict] = {}
        self._bid_book: dict[float, float] = {}
        self._ask_book: dict[float, float] = {}

    def place_order(self, oid: str, side: str, price: float, size: float) -> None:
        """Register a new resting order. Snapshots queue depth at placement."""
        book = self._bid_book if side == "buy" else self._ask_book
        queue_ahead = book.get(price, 0.0) * QUEUE_POSITION_FRAC
        self._orders[oid] = {
            "side": side, "price": price, "size": size,
            "queue_ahead": queue_ahead, "vol_consumed": 0.0, "filled_qty": 0.0,
        }

    def cancel_order(self, oid: str) -> None:
        self._orders.pop(oid, None)

    def on_book_update(self, bids: list, asks: list) -> None:
        """Full book snapshot — rebuild depth maps."""
        self._bid_book = {p: q for p, q in bids}
        self._ask_book = {p: q for p, q in asks}

    def on_book_event(self, side: str, price: float, volume: float) -> None:
        """Incremental book delta — update one level."""
        book = self._bid_book if side in ("bid", "buy") else self._ask_book
        if volume <= 0:
            book.pop(price, None)
        else:
            book[price] = volume

    def on_trade(self, price: float, volume: float, aggressor: str) -> list:
        """Process a taker trade. Returns list of fill-event dicts.

        A sell-aggressor trade at P consumes bids at P.
        A buy-aggressor  trade at P consumes asks at P.
        """
        fills = []
        for oid, o in list(self._orders.items()):
            side, oprice, osize = o["side"], o["price"], o["size"]
            remaining = osize - o["filled_qty"]
            if remaining <= 1e-9:
                continue

            if side == "buy" and aggressor == "sell" and price <= oprice:
                o["vol_consumed"] += volume
            elif side == "sell" and aggressor == "buy" and price >= oprice:
                o["vol_consumed"] += volume
            else:
                continue

            vol_past_queue = o["vol_consumed"] - o["queue_ahead"]
            if vol_past_queue <= 0:
                continue

            fill_qty = min(remaining, vol_past_queue - o["filled_qty"])
            if fill_qty <= 1e-9:
                continue

            o["filled_qty"] += fill_qty
            fills.append({
                "order_id": oid,
                "side": side,
                "price": oprice,
                "qty": fill_qty,
                "fee": fill_qty * oprice * MAKER_FEE_RATE,
                "fully_filled": abs(o["filled_qty"] - osize) < 1e-9,
            })
            if fills[-1]["fully_filled"]:
                self._orders.pop(oid, None)

        return fills


# ── Main dry-run manager ──────────────────────────────────────────────────────

class DryRunOrderManager:
    """Stateful dry-run fill simulator.

    Holds per-symbol open-order registries, fill engines, and the trade buffer.
    Call simulate_dry_fills() once per quote tick (before placing new quotes).

    Parameters
    ----------
    pairs : list[str]
        Symbols to track, e.g. ["BTC/USDT", "ETH/USDT"].
    apply_fill : callable(symbol, side, qty, price, fee) -> None
        Your inventory/P&L update callback.
    pair_state : dict[str, object]
        Live pair state dict. Each value must expose .inventory and .vol_regime.
    min_size_fn : callable(symbol) -> float, optional
        Returns minimum lot size for symbol. Defaults to returning 0.0.
    latency_ms : float
        Simulated order-to-fill latency. Use 2.0ms for local testing,
        10.0ms to model EC2→Bybit Singapore latency.
    maker_fee_rate : float
        Override fee rate if your account tier differs from -0.5 bps.
    """

    def __init__(
        self,
        pairs: list[str],
        apply_fill: Callable,
        pair_state: dict,
        min_size_fn: Callable | None = None,
        latency_ms: float = 2.0,
        maker_fee_rate: float = MAKER_FEE_RATE,
    ) -> None:
        self._apply_fill = apply_fill
        self._pair_state = pair_state
        self._min_size = min_size_fn or (lambda sym: 0.0)
        self._fill_latency_s = latency_ms / 1000.0
        self._maker_fee_rate = maker_fee_rate
        self._seq = 0  # monotonic order-ID counter

        canonical = [p.upper().replace("-", "/") for p in pairs]

        # Per-symbol open-order registry: {sym: {order_id: order_dict}}
        # Strict symbol isolation — a BTC order can never appear in ETH's fill loop.
        self._dry_open_orders: dict[str, dict[str, dict]] = {s: {} for s in canonical}
        self._dry_trade_buffer: dict[str, list] = {s: [] for s in canonical}

        # Fill engines: C++ (preferred) → Python _QueueAwareFillModel → naive
        self._fill_engines: dict[str, Any] = {}
        self._py_fill_engines: dict[str, _QueueAwareFillModel] = {}

        if _SIM_FILL_AVAILABLE and _make_fill_engine is not None:
            for s in canonical:
                self._fill_engines[s] = _make_fill_engine(latency_ms=latency_ms)
            print(f"  [sim-fill] C++ engine active  latency={latency_ms:.0f}ms")
        else:
            for s in canonical:
                self._py_fill_engines[s] = _QueueAwareFillModel()
            print(f"  [sim-fill] Python engine active  queue_frac={QUEUE_POSITION_FRAC:.1f}")

    # ── Order registration ────────────────────────────────────────────────────

    def register_order(self, symbol: str, order_id: str, side: str,
                       price: float, size: float) -> None:
        """Register a newly placed resting order into the dry-run registry."""
        sym = symbol.upper().replace("-", "/")
        now = time.monotonic()
        self._dry_open_orders.setdefault(sym, {})[order_id] = {
            "side": side, "price": price, "size": size,
            "remaining": size, "placed_at": now,
        }
        fe = self._fill_engines.get(sym)
        if fe is not None:
            import mm_matching_engine as _mme
            _side = _mme.Side.BID if side == "buy" else _mme.Side.ASK
            fe.engine.place_order(_side, price, size)
        pfe = self._py_fill_engines.get(sym)
        if pfe is not None:
            pfe.place_order(order_id, side, price, size)

    def cancel_order(self, symbol: str, order_id: str) -> None:
        sym = symbol.upper().replace("-", "/")
        self._dry_open_orders.get(sym, {}).pop(order_id, None)
        fe = self._fill_engines.get(sym)
        if fe is not None:
            fe.cancel_order(order_id)
        pfe = self._py_fill_engines.get(sym)
        if pfe is not None:
            pfe.cancel_order(order_id)

    def buffer_trade(self, symbol: str, price: float, volume: float,
                     aggressor: str) -> None:
        """Feed a public trade event into the fill engine for the given symbol."""
        sym = symbol.upper().replace("-", "/")
        fe = self._fill_engines.get(sym)
        if fe is not None:
            import mm_matching_engine as _mme
            agg = _mme.AggressorSide.SELL if aggressor == "sell" else _mme.AggressorSide.BUY
            for fr in fe.engine.on_trade(price, volume, agg):
                oid = str(fr.order_id)
                o = self._dry_open_orders.get(sym, {}).get(oid)
                if o is not None:
                    fee = fr.filled_qty * fr.fill_price * self._maker_fee_rate
                    self._apply_fill(sym, o["side"], fr.filled_qty, fr.fill_price, fee)
                    if fr.filled_qty >= o.get("remaining", o["size"]) - 1e-9:
                        self._dry_open_orders[sym].pop(oid, None)
            return
        pfe = self._py_fill_engines.get(sym)
        if pfe is not None:
            for fill in pfe.on_trade(price, volume, aggressor):
                oid = fill["order_id"]
                self._apply_fill(sym, fill["side"], fill["qty"], fill["price"], fill["fee"])
                if fill["fully_filled"]:
                    self._dry_open_orders.get(sym, {}).pop(oid, None)
            return
        # naive: buffer for simulate_dry_fills()
        self._dry_trade_buffer.setdefault(sym, []).append(
            {"price": price, "volume": volume, "side": aggressor}
        )

    def update_book(self, symbol: str, bids: list, asks: list) -> None:
        """Feed a full book snapshot to the Python fill model."""
        sym = symbol.upper().replace("-", "/")
        pfe = self._py_fill_engines.get(sym)
        if pfe is not None:
            pfe.on_book_update(bids, asks)

    # ── Core simulate method ──────────────────────────────────────────────────

    def simulate_dry_fills(
        self,
        symbol: str,
        best_bid: float,
        best_ask: float,
    ) -> None:
        """Simulate passive fills for resting dry-run orders.

        C++ engine path:
          Fills via buffer_trade() handle strict FIFO queue cases. This method
          additionally catches book-crossed orders and applies a calibrated
          price-touch decay model for orders that the trade stream missed.

        Python engine path:
          Same price-touch decay model. on_trade() fills already processed in
          buffer_trade(); this method handles the gap-based stochastic fills.

        Naive fallback:
          Uses buffered trade volume + best bid/ask crossing.

        Call this once per quote tick, BEFORE placing new quotes, so fills from
        the last tick are processed before the order refresh.
        """
        if best_bid <= 0 or best_ask <= 0:
            return
        sym = symbol.upper().replace("-", "/")
        now = time.monotonic()
        mid = (best_bid + best_ask) / 2.0
        sym_orders = self._dry_open_orders.get(sym, {})
        filled: list[str] = []

        # ── C++ engine path ───────────────────────────────────────────────────
        if self._fill_engines.get(sym) is not None:
            fe = self._fill_engines[sym]
            for oid, o in list(sym_orders.items()):
                side, price, size = o["side"], o["price"], o["size"]
                age_s = now - o.get("placed_at", now)
                if "remaining" not in o:
                    o["remaining"] = size

                # Sanity guard: discard orders whose price drifted >200% from mid
                if mid > 0 and abs(price - mid) / mid > 2.0:
                    fe.cancel_order(oid)
                    filled.append(oid)
                    continue

                # Book-crossed: order sits inside the spread → fill immediately
                crossed = (side == "buy" and price >= best_ask) or \
                          (side == "sell" and price <= best_bid)
                if crossed:
                    fill_price = best_ask if side == "buy" else best_bid
                    fill_qty = size
                    if side == "sell":
                        ps = self._pair_state.get(sym)
                        inv = getattr(ps, "inventory", 0.0) if ps is not None else 0.0
                        if inv < fill_qty * 0.5:
                            fe.cancel_order(oid)
                            filled.append(oid)
                            continue
                        fill_qty = min(fill_qty, inv)
                    fill_qty = max(fill_qty, self._min_size(sym))
                    fee = fill_qty * fill_price * self._maker_fee_rate
                    self._apply_fill(sym, side, fill_qty, fill_price, fee)
                    fe.cancel_order(oid)
                    filled.append(oid)
                    continue

                # Price-touch decay fill (calibrated against Bybit live Apr 2026):
                #   fill_prob = BASE × regime_mult × spread_mult × exp(-DECAY_K × gap_bps)
                #   BASE=0.015, DECAY_K=0.14, MAX_GAP=50bps
                if age_s < self._fill_latency_s:
                    continue
                _BASE    = 0.015
                _DECAY_K = 0.14
                _MAX_GAP = 50.0
                ps = self._pair_state.get(sym)
                regime = getattr(ps, "vol_regime", "calm") if ps is not None else "calm"
                regime_mult = 0.30 if regime == "toxic" else (0.60 if regime == "elevated" else 1.00)
                spread_bps = (best_ask - best_bid) / mid * 1e4 if mid > 0 else 0.0
                spread_mult = max(0.20, 1.0 - 0.8 * min(1.0, max(0.0, (spread_bps - 20.0) / 60.0)))
                gap_bps = (best_bid - price) / price * 1e4 if side == "buy" else \
                          (price - best_ask) / price * 1e4
                if 0.0 <= gap_bps <= _MAX_GAP:
                    fill_prob = _BASE * regime_mult * spread_mult * math.exp(-_DECAY_K * gap_bps)
                    if random.random() < fill_prob:
                        fill_qty = o.get("remaining", size)
                        if side == "sell":
                            inv = getattr(ps, "inventory", 0.0) if ps is not None else 0.0
                            if inv < fill_qty * 0.5:
                                fe.cancel_order(oid)
                                filled.append(oid)
                                continue
                            fill_qty = min(fill_qty, inv)
                        fill_qty = max(fill_qty, self._min_size(sym))
                        fee = fill_qty * price * self._maker_fee_rate
                        self._apply_fill(sym, side, fill_qty, price, fee)
                        fe.cancel_order(oid)
                        filled.append(oid)

            for oid in filled:
                sym_orders.pop(oid, None)
            self._dry_trade_buffer[sym] = []
            return

        # ── Python queue engine path ──────────────────────────────────────────
        pfe = self._py_fill_engines.get(sym)
        if pfe is not None:
            for oid, o in list(sym_orders.items()):
                side, price, size = o["side"], o["price"], o["size"]
                age_s = now - o.get("placed_at", now)

                if mid > 0 and abs(price - mid) / mid > 2.0:
                    pfe.cancel_order(oid)
                    filled.append(oid)
                    continue

                crossed = (side == "buy" and price >= best_ask) or \
                          (side == "sell" and price <= best_bid)
                if crossed:
                    fill_price = best_ask if side == "buy" else best_bid
                    pfe_state = pfe._orders.get(oid)
                    already = pfe_state["filled_qty"] if pfe_state is not None else 0.0
                    fill_qty = max(size - already, self._min_size(sym))
                    if side == "sell":
                        ps = self._pair_state.get(sym)
                        inv = getattr(ps, "inventory", 0.0) if ps is not None else 0.0
                        if inv < fill_qty * 0.5:
                            pfe.cancel_order(oid)
                            filled.append(oid)
                            continue
                        fill_qty = min(fill_qty, inv)
                    fee = fill_qty * fill_price * self._maker_fee_rate
                    self._apply_fill(sym, side, fill_qty, fill_price, fee)
                    pfe.cancel_order(oid)
                    filled.append(oid)
                    continue

                if age_s < self._fill_latency_s:
                    continue
                _BASE    = 0.015
                _DECAY_K = 0.14
                _MAX_GAP = 50.0
                ps = self._pair_state.get(sym)
                regime = getattr(ps, "vol_regime", "calm") if ps is not None else "calm"
                regime_mult = 0.30 if regime == "toxic" else (0.60 if regime == "elevated" else 1.00)
                spread_bps = (best_ask - best_bid) / mid * 1e4 if mid > 0 else 0.0
                spread_mult = max(0.20, 1.0 - 0.8 * min(1.0, max(0.0, (spread_bps - 20.0) / 60.0)))
                gap_bps = (best_bid - price) / price * 1e4 if side == "buy" else \
                          (price - best_ask) / price * 1e4
                if 0.0 <= gap_bps <= _MAX_GAP:
                    fill_prob = _BASE * regime_mult * spread_mult * math.exp(-_DECAY_K * gap_bps)
                    if random.random() < fill_prob:
                        fill_qty = o.get("remaining", size)
                        if side == "sell":
                            inv = getattr(ps, "inventory", 0.0) if ps is not None else 0.0
                            if inv < fill_qty * 0.5:
                                pfe.cancel_order(oid)
                                filled.append(oid)
                                continue
                            fill_qty = min(fill_qty, inv)
                        fill_qty = max(fill_qty, self._min_size(sym))
                        fee = fill_qty * price * self._maker_fee_rate
                        self._apply_fill(sym, side, fill_qty, price, fee)
                        pfe.cancel_order(oid)
                        filled.append(oid)

            for oid in filled:
                sym_orders.pop(oid, None)
            self._dry_trade_buffer[sym] = []
            return

        # ── Naive fallback path ───────────────────────────────────────────────
        trades = self._dry_trade_buffer.pop(sym, [])
        for oid, o in list(sym_orders.items()):
            side, price, size = o["side"], o["price"], o["size"]
            if side == "buy":
                sell_vol = sum(
                    float(t.get("volume", size))
                    for t in trades
                    if t.get("side") == "sell" and float(t.get("price", 0)) <= price
                )
                if sell_vol > 0 or best_bid < price:
                    fill_qty = max(min(size, sell_vol) if sell_vol < size else size,
                                   self._min_size(sym))
                    self._apply_fill(sym, "buy", fill_qty, price,
                                     fill_qty * price * self._maker_fee_rate)
                    filled.append(oid)
            else:
                ps = self._pair_state.get(sym)
                inv = getattr(ps, "inventory", 0.0) if ps is not None else 0.0
                if inv < size * 0.5:
                    filled.append(oid)
                    continue
                buy_vol = sum(
                    float(t.get("volume", size))
                    for t in trades
                    if t.get("side") == "buy" and float(t.get("price", 0)) >= price
                )
                if buy_vol > 0 or best_ask > price:
                    fill_qty = max(min(min(size, buy_vol) if buy_vol < size else size, inv),
                                   self._min_size(sym))
                    self._apply_fill(sym, "sell", fill_qty, price,
                                     fill_qty * price * self._maker_fee_rate)
                    filled.append(oid)

        for oid in filled:
            sym_orders.pop(oid, None)
