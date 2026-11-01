"""sim_fill_engine.py — Python bridge between bybit_order_manager.py and the
C++ MatchingEngine.

Responsibilities
────────────────
1. Wraps mm_matching_engine (C++ pybind11 module) with a clean Python API.
2. Feeds live L2 book updates and trade events into the matching engine.
3. Applies maker rebate on every fill (all orders are PostOnly — no taker fees).
4. Exposes simulate_fills() that replaces the naive price-crossing logic in
   bybit_order_manager.py.

If the C++ module is not compiled yet, falls back to the original naive model
with a warning so dry-runs still work.

Fee schedule (Bybit linear USDT perps, VIP 0):
  All orders PostOnly → maker rebate −0.010 bps (rate = −0.00001) on every fill.
  Negative fee = we earn → adds to realized PnL.

Fill realism gates (both engines)
──────────────────────────────────
Two gates are applied on top of the base fill logic to bring dry-run fill rates
closer to live observations:

  Gate 1 — Queue position (naive engine only; C++ already does FIFO natively):
    At placement, records book depth at our price level.  At fill time, computes
    how much of that depth has been consumed.  Fill probability scales from
    QUEUE_FILL_PROB_MIN (back of queue, depth unchanged) to 1.0 (full depth
    consumed ahead of us).

    fill_prob = QUEUE_FILL_PROB_MIN + (1 - QUEUE_FILL_PROB_MIN) × consumed/depth

  Gate 2 — Adverse selection (both engines):
    A sell aggressor clearing our bid while the top-of-book is heavily
    sell-dominated indicates informed flow picking us off.  In live, professional
    MMs cancel in <1ms; our cancel loop runs at 500ms+, so in live these fills
    would often not happen (the order would be cancelled before queue cleared).

    When: aggressor=sell, side=buy (our bid), book_imbalance < ADVERSE_IMBAL_THRESHOLD
    Action: fill accepted with probability ADVERSE_FILL_PROB (default 35%).
            Blocked fills are treated as implicit cancels — order is removed
            from tracking without inventory credit, modeling the live cancel.

    book_imbalance = (top_N_bid_vol − top_N_ask_vol) / (top_N_bid_vol + top_N_ask_vol)
    Range: −1.0 (all asks, max sell pressure) … +1.0 (all bids, max buy pressure)
    Threshold ADVERSE_IMBAL_THRESHOLD = −0.25 means ≥62.5% ask-dominated.

Calibration notes:
  These thresholds were calibrated so the combined gate produces ~35-40% fill
  rate reduction on adverse fills (matching observed live vs dry-run fill rate
  discrepancy for XRP/BNB-class coins).  Adjust ADVERSE_FILL_PROB to tune.
"""

from __future__ import annotations

import importlib
import math
import random as _random
import warnings
from dataclasses import dataclass, field
from typing import Optional

# ── Try to import C++ engine ──────────────────────────────────────────────────
import sys as _sys, os as _os
_here = _os.path.dirname(_os.path.abspath(__file__))
if _here not in _sys.path:
    _sys.path.insert(0, _here)

try:
    import mm_matching_engine as _cpp  # built by cmake into FAST_MM/
    _CPP_AVAILABLE = True
except ImportError:
    _cpp = None  # type: ignore
    _CPP_AVAILABLE = False
    warnings.warn(
        "[sim_fill_engine] C++ mm_matching_engine not found — "
        "falling back to naive price-crossing fill model. "
        "Build with: cd cpp_gateway && cmake -B build -DBUILD_PYTHON_BINDINGS=ON "
        "&& cmake --build build",
        stacklevel=2,
    )

# ── Fill realism gate constants ───────────────────────────────────────────────

# Gate 2 — Adverse selection (both engines)
_ADVERSE_IMBAL_THRESHOLD: float = -0.25   # imbalance < this → adversely sell-dominated
_ADVERSE_FILL_PROB:       float = 0.35    # prob fill goes through under adverse conditions
_ADVERSE_BOOK_LEVELS:     int   = 5       # top N bid/ask levels used for imbalance

# Gate 1 — Queue position (naive engine only)
_QUEUE_FILL_PROB_MIN:     float = 0.10    # min fill prob when queue hasn't moved at all

# Partial queue fill (C++ engine):
# In live trading, queue cancellations/amendments ahead of us mean we fill
# before 100% of the original queue is consumed.  The C++ engine is strict FIFO
# (requires full queue_ahead=0), which under-fills in choppy markets where the
# book churns fast but trades don't sweep the full level.
# When queue has been consumed >= PARTIAL_QUEUE_THRESHOLD fraction, award fill
# with PARTIAL_FILL_PROB probability per on_trade() call.
# This brings dry-run fill rate closer to live without disabling queue realism.
_PARTIAL_QUEUE_THRESHOLD: float = 0.10   # 10% of original queue consumed → eligible
_PARTIAL_FILL_PROB:       float = 0.05   # 5% chance per trade once threshold met


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class SimFill:
    order_id:   str
    side:       str       # "buy" or "sell"
    qty:        float
    price:      float
    fee:        float     # negative = rebate earned, positive = fee paid
    is_maker:   bool
    net_pnl:    float     # (qty * price) adjusted for fee sign

@dataclass
class FeeStats:
    total_maker_rebate: float = 0.0   # cumulative rebate earned (positive)
    total_taker_fee:    float = 0.0   # cumulative fee paid     (positive)

    @property
    def net(self) -> float:
        return self.total_maker_rebate - self.total_taker_fee

# ── Naive fallback (when C++ not compiled) ────────────────────────────────────

_MAKER_RATE = -0.00001   # −0.010 bps — only rate used (all orders PostOnly)

class _NaiveFillEngine:
    """Price-crossing fill model + fee accounting + queue/adverse-sel gates.

    Gate 1 (queue position): fill probability scales with how much of the
    book depth at our price has been consumed since placement.

    Gate 2 (adverse selection): bids touched by sell aggressors while the
    top-of-book is heavily sell-dominated are blocked with probability
    (1 − ADVERSE_FILL_PROB).
    """

    def __init__(self) -> None:
        self.fee_stats = FeeStats()
        self._orders: dict[str, dict] = {}
        # ── Book state for fill gating ────────────────────────────────────
        self._bid_levels: dict[float, float] = {}   # price → size
        self._ask_levels: dict[float, float] = {}
        self._order_depth_at_placement: dict[str, float] = {}
        # ── Diagnostics ───────────────────────────────────────────────────
        self._queue_gate_blocks:   int = 0
        self._adverse_sel_blocks:  int = 0
        self._adverse_sel_passes:  int = 0

    # ── Book helpers ──────────────────────────────────────────────────────

    def _book_imbalance(self) -> float:
        """Top-N imbalance: +1 = all bids, -1 = all asks."""
        top_bids = sorted(self._bid_levels.items(), reverse=True)[:_ADVERSE_BOOK_LEVELS]
        top_asks = sorted(self._ask_levels.items())[:_ADVERSE_BOOK_LEVELS]
        bid_vol = sum(v for _, v in top_bids)
        ask_vol = sum(v for _, v in top_asks)
        total = bid_vol + ask_vol
        return (bid_vol - ask_vol) / total if total > 0 else 0.0

    # ── Core API ──────────────────────────────────────────────────────────

    def place_order(self, order_id: str, side: str, price: float,
                    size: float, post_only: bool = True) -> None:
        self._orders[order_id] = {
            "side": side, "price": price,
            "size": size,  "remaining": size,
        }
        # Record depth at our price level for Gate 1 (queue position)
        p = float(price)
        if side == "buy":
            self._order_depth_at_placement[order_id] = self._bid_levels.get(p, 0.0)
        else:
            self._order_depth_at_placement[order_id] = self._ask_levels.get(p, 0.0)

    def cancel_order(self, order_id: str) -> None:
        self._orders.pop(order_id, None)
        self._order_depth_at_placement.pop(order_id, None)

    def amend_price(self, order_id: str, new_price: float) -> None:
        if order_id in self._orders:
            self._orders[order_id]["price"] = new_price
            # Re-record depth at new price (we're now at the back of that queue)
            side = self._orders[order_id]["side"]
            p = float(new_price)
            if side == "buy":
                self._order_depth_at_placement[order_id] = self._bid_levels.get(p, 0.0)
            else:
                self._order_depth_at_placement[order_id] = self._ask_levels.get(p, 0.0)

    def on_book_update(self, bids: list, asks: list) -> None:
        """Full L2 snapshot — update tracked book state."""
        for price, size in bids:
            p, s = float(price), float(size)
            if s > 0:
                self._bid_levels[p] = s
            else:
                self._bid_levels.pop(p, None)
        for price, size in asks:
            p, s = float(price), float(size)
            if s > 0:
                self._ask_levels[p] = s
            else:
                self._ask_levels.pop(p, None)

    def on_book_event(self, side: str, price: float, size: float) -> None:
        """Incremental L2 delta — update one level."""
        p, s = float(price), float(size)
        if side in ("bid", "buy"):
            if s > 0:
                self._bid_levels[p] = s
            else:
                self._bid_levels.pop(p, None)
        else:
            if s > 0:
                self._ask_levels[p] = s
            else:
                self._ask_levels.pop(p, None)

    def on_trade(self, trade_price: float, trade_vol: float,
                 aggressor: str) -> list[SimFill]:
        return []  # naive model drives fills through simulate_fills()

    def simulate_fills(self, best_bid: float, best_ask: float,
                       sym: str) -> list[SimFill]:
        fills = []
        imbal = self._book_imbalance()

        for oid, o in list(self._orders.items()):
            side, price, qty = o["side"], o["price"], o["remaining"]

            # Price-touch check (market has reached our level)
            hit = (side == "buy"  and best_bid <= price) or \
                  (side == "sell" and best_ask >= price)
            if not hit:
                continue

            # ── Gate 1: Queue position ─────────────────────────────────────
            # Estimate how much of the queue ahead of us has been consumed.
            # If depth_at_placement is 0 (no data), skip the gate.
            depth_at_pl = self._order_depth_at_placement.get(oid, 0.0)
            if depth_at_pl > 0:
                p = float(price)
                current_depth = (
                    self._bid_levels.get(p, 0.0)
                    if side == "buy" else
                    self._ask_levels.get(p, 0.0)
                )
                consumed = max(0.0, depth_at_pl - current_depth)
                queue_fill_ratio = min(1.0, consumed / depth_at_pl)
                fill_prob = (
                    _QUEUE_FILL_PROB_MIN
                    + (1.0 - _QUEUE_FILL_PROB_MIN) * queue_fill_ratio
                )
            else:
                fill_prob = 1.0   # no book data — don't gate

            # ── Gate 2: Adverse selection (bids only) ──────────────────────
            # Sell aggressor hitting our bid while book is sell-dominated →
            # informed flow; we'd have been cancelled in live.
            if side == "buy" and imbal < _ADVERSE_IMBAL_THRESHOLD:
                if _random.random() > _ADVERSE_FILL_PROB:
                    self._adverse_sel_blocks += 1
                    continue   # treat as implicit cancel
                self._adverse_sel_passes += 1

            # ── Queue gate roll ────────────────────────────────────────────
            if _random.random() > fill_prob:
                self._queue_gate_blocks += 1
                continue

            # ── Accept fill ───────────────────────────────────────────────
            fee  = qty * price * _MAKER_RATE
            sign = -1.0 if side == "buy" else +1.0
            self.fee_stats.total_maker_rebate -= fee
            fills.append(SimFill(
                order_id=oid, side=side, qty=qty,
                price=price, fee=fee, is_maker=True,
                net_pnl=sign * qty * price - fee,
            ))
            del self._orders[oid]
            self._order_depth_at_placement.pop(oid, None)

        return fills

    def fill_gate_stats(self) -> dict:
        """Return diagnostic counters for the fill probability gates."""
        return {
            "queue_blocks":        self._queue_gate_blocks,
            "adverse_sel_blocks":  self._adverse_sel_blocks,
            "adverse_sel_passes":  self._adverse_sel_passes,
        }

    def reset(self) -> None:
        self._orders.clear()
        self.fee_stats = FeeStats()
        self._bid_levels.clear()
        self._ask_levels.clear()
        self._order_depth_at_placement.clear()
        self._queue_gate_blocks  = 0
        self._adverse_sel_blocks = 0
        self._adverse_sel_passes = 0


# ── C++ engine wrapper ────────────────────────────────────────────────────────

class _CppFillEngine:
    """Wraps mm_matching_engine (C++) with the same API as _NaiveFillEngine.

    Queue position:
      - On place_order(): C++ records book volume ahead of us at that price
      - On on_trade():    C++ decrements queue_ahead_remaining in FIFO order
      - Fill only happens when queue is cleared AND trade sweeps our price

    Latency proxy:
      - latency_s: simulated EC2→Bybit one-way round-trip (default 10ms).
        Orders placed within `latency_s` of a market trade are not eligible
        to fill — Bybit wouldn't have received them yet.
        Measured EC2 ap-southeast-1 → Bybit Singapore: p50≈8ms, p99≈18ms.

    Adverse selection gate (Gate 2):
      Applied in on_trade() on top of C++ FIFO queue fills.
      When a sell aggressor clears our bid and the book is sell-dominated,
      fill is accepted with probability ADVERSE_FILL_PROB only.
      Blocked fills are treated as implicit cancels — order is removed from
      all tracking maps so inventory is unchanged, modeling the live scenario
      where our cancel loop would have removed the order before the adverse fill.
    """

    def __init__(self, maker_rate: float = _MAKER_RATE,
                 taker_rate: float = _MAKER_RATE,
                 latency_s:  float = 0.010) -> None:
        fee_cfg = _cpp.FeeConfig(maker_rate, taker_rate)
        self._engine = _cpp.MatchingEngine(fee_cfg)
        # Order tracking maps
        self._str_to_cpp:       dict[str, int]   = {}
        self._cpp_to_str:       dict[int, str]   = {}
        self._order_sides:      dict[str, str]   = {}  # oid → "buy"/"sell"
        self._placed_at:        dict[str, float] = {}  # oid → monotonic placement time
        self._placed_queue_ahead: dict[str, float] = {}  # oid → queue_ahead at placement
        self._latency_s:        float            = latency_s
        # ── Book imbalance tracking for Gate 2 ────────────────────────────
        # Maintained from on_book_update() snapshots.
        # on_book_event() incremental deltas keep the dict current.
        self._bid_levels: dict[float, float] = {}
        self._ask_levels: dict[float, float] = {}
        self._book_imbalance: float = 0.0      # last computed top-N imbalance
        # ── Diagnostics ───────────────────────────────────────────────────
        self._adverse_sel_blocks: int = 0
        self._adverse_sel_passes: int = 0
        # Partial fill diagnostic counters
        self._partial_skip_latency:  int = 0
        self._partial_skip_no_qa:    int = 0   # orig_qa == 0
        self._partial_skip_cpp_gone: int = 0
        self._partial_skip_pct:      int = 0   # consumed_frac < threshold
        self._partial_skip_adverse:  int = 0
        self._partial_skip_prob:     int = 0
        self._partial_fills:         int = 0
        self._partial_log_tick:      int = 0

    # ── Book imbalance helper ─────────────────────────────────────────────

    def _recompute_imbalance(self) -> None:
        """Recompute top-N imbalance from current bid/ask level dicts."""
        top_bids = sorted(self._bid_levels.items(), reverse=True)[:_ADVERSE_BOOK_LEVELS]
        top_asks = sorted(self._ask_levels.items())[:_ADVERSE_BOOK_LEVELS]
        bid_vol = sum(v for _, v in top_bids)
        ask_vol = sum(v for _, v in top_asks)
        total = bid_vol + ask_vol
        self._book_imbalance = (bid_vol - ask_vol) / total if total > 0 else 0.0

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def fee_stats(self) -> FeeStats:
        fe = self._engine.fee_engine()
        return FeeStats(
            total_maker_rebate=fe.total_maker_rebate,
            total_taker_fee=fe.total_taker_fee,
        )

    # ── Core API ──────────────────────────────────────────────────────────

    def place_order(self, order_id: str, side: str, price: float,
                    size: float, post_only: bool = True) -> None:
        import time as _time
        cpp_side = _cpp.Side.BID if side == "buy" else _cpp.Side.ASK
        otype    = _cpp.OrderType.POST_ONLY if post_only else _cpp.OrderType.IOC
        cpp_id   = self._engine.place_order(cpp_side, price, size, otype)
        self._str_to_cpp[order_id]  = cpp_id
        self._cpp_to_str[cpp_id]    = order_id
        self._order_sides[order_id] = side
        self._placed_at[order_id]   = _time.monotonic()
        # Record initial queue_ahead for partial fill eligibility check
        qa = self._engine.queue_ahead_remaining(cpp_id)
        self._placed_queue_ahead[order_id] = max(float(qa), 0.0)

    def cancel_order(self, order_id: str) -> None:
        cpp_id = self._str_to_cpp.pop(order_id, None)
        if cpp_id is not None:
            self._engine.cancel_order(cpp_id)
            self._cpp_to_str.pop(cpp_id, None)
        self._order_sides.pop(order_id, None)
        self._placed_at.pop(order_id, None)
        self._placed_queue_ahead.pop(order_id, None)

    def amend_price(self, order_id: str, new_price: float) -> None:
        cpp_id = self._str_to_cpp.get(order_id)
        if cpp_id is not None:
            self._engine.amend_price(cpp_id, new_price)

    def on_book_update(self, bids: list[tuple[float, float]],
                       asks: list[tuple[float, float]]) -> None:
        """Feed L2 snapshot into C++ engine AND update imbalance tracking."""
        for price, size in bids:
            self._engine.update_bid_level(price, size)
            p, s = float(price), float(size)
            if s > 0:
                self._bid_levels[p] = s
            else:
                self._bid_levels.pop(p, None)
        for price, size in asks:
            self._engine.update_ask_level(price, size)
            p, s = float(price), float(size)
            if s > 0:
                self._ask_levels[p] = s
            else:
                self._ask_levels.pop(p, None)
        self._recompute_imbalance()

    def on_book_event(self, side: str, price: float, size: float) -> None:
        """Incremental L2 delta — update C++ engine AND one imbalance level.

        Unlike on_book_update() which resets the whole book, this surgically
        updates one level — preserving queue_ahead accuracy between snapshots.
        Imbalance is recomputed only on snapshot (on_book_update) to avoid
        sorting on every sub-ms book event.
        """
        p, s = float(price), float(size)
        if side in ("bid", "buy"):
            self._engine.update_bid_level(price, max(size, 0.0))
            if s > 0:
                self._bid_levels[p] = s
            else:
                self._bid_levels.pop(p, None)
        else:
            self._engine.update_ask_level(price, max(size, 0.0))
            if s > 0:
                self._ask_levels[p] = s
            else:
                self._ask_levels.pop(p, None)
        # NOTE: imbalance is NOT recomputed on every tick — only on snapshots.
        # This is intentional: on_book_event fires ~100-1000×/s per pair;
        # sorting dicts that often would add meaningful CPU.  The imbalance
        # from the last snapshot is a sufficient approximation for Gate 2.

    def on_trade(self, trade_price: float, trade_vol: float,
                 aggressor: str) -> list[SimFill]:
        """Process a market trade; returns any of our orders that got filled.

        Two gates applied after C++ FIFO queue decision:

          Latency gate: orders placed < latency_s ago are skipped.

          Adverse selection gate (Gate 2):
            sell aggressor + our bid + book sell-dominated
            → fill accepted with prob ADVERSE_FILL_PROB only.
            Blocked fills: order removed from tracking (implicit cancel).
        """
        import time as _time
        now = _time.monotonic()
        agg = _cpp.AggressorSide.BUY if aggressor == "buy" else _cpp.AggressorSide.SELL
        cpp_fills = self._engine.on_trade(trade_price, trade_vol, agg)
        fills = []

        for f in cpp_fills:
            str_id = self._cpp_to_str.get(f.order_id, str(f.order_id))
            placed = self._placed_at.get(str_id, 0.0)

            # ── Latency gate ───────────────────────────────────────────────
            if now - placed < self._latency_s:
                continue   # order too new — Bybit hasn't received it yet

            side = self._order_sides.get(str_id, "buy")

            # ── Gate 2: Adverse selection ──────────────────────────────────
            # A sell aggressor clearing our bid while the top-of-book is
            # sell-dominated means informed flow is picking us off.  In live,
            # our order would often have been cancelled by the quote loop
            # (which runs ~500ms) before the queue cleared.  Model this by
            # randomly blocking a fraction of these adverse fills.
            if (aggressor == "sell"
                    and side == "buy"
                    and self._book_imbalance < _ADVERSE_IMBAL_THRESHOLD):
                if _random.random() > _ADVERSE_FILL_PROB:
                    # Blocked: treat as implicit cancel.
                    # C++ already removed order from its engine, so we just
                    # clean up the Python tracking maps — no inventory change.
                    self._cpp_to_str.pop(f.order_id, None)
                    self._str_to_cpp.pop(str_id, None)
                    self._order_sides.pop(str_id, None)
                    self._placed_at.pop(str_id, None)
                    self._adverse_sel_blocks += 1
                    continue
                self._adverse_sel_passes += 1

            # ── Accept fill ────────────────────────────────────────────────
            self._cpp_to_str.pop(f.order_id, None)
            self._str_to_cpp.pop(str_id, None)
            self._order_sides.pop(str_id, None)
            self._placed_at.pop(str_id, None)
            self._placed_queue_ahead.pop(str_id, None)
            sign = -1.0 if side == "buy" else +1.0
            fills.append(SimFill(
                order_id=str_id, side=side,
                qty=f.filled_qty, price=f.fill_price,
                fee=f.fee, is_maker=f.is_maker,
                net_pnl=sign * f.filled_qty * f.fill_price - f.fee,
            ))

        # ── Partial queue fill: award fill when ≥50% of original queue consumed ──
        # Models live behavior: cancellations/amendments ahead of us mean we fill
        # before the C++ FIFO engine's strict queue_ahead==0 threshold is reached.
        self._partial_log_tick += 1
        _log_partial = (self._partial_log_tick % 500 == 0)   # ~every 25s per pair
        for str_id, side in list(self._order_sides.items()):
            if now - self._placed_at.get(str_id, 0.0) < self._latency_s:
                self._partial_skip_latency += 1
                continue
            orig_qa = self._placed_queue_ahead.get(str_id, 0.0)
            if orig_qa <= 0.0:
                self._partial_skip_no_qa += 1
                continue   # no queue data recorded at placement — skip
            cpp_id = self._str_to_cpp.get(str_id)
            if cpp_id is None:
                self._partial_skip_cpp_gone += 1
                continue
            curr_qa = self._engine.queue_ahead_remaining(cpp_id)
            consumed_frac = 1.0 - (float(curr_qa) / orig_qa)
            if consumed_frac < _PARTIAL_QUEUE_THRESHOLD:
                self._partial_skip_pct += 1
                continue   # queue not sufficiently consumed yet

            # ── Adverse selection gate ─────────────────────────────────────
            if (aggressor == "sell"
                    and side == "buy"
                    and self._book_imbalance < _ADVERSE_IMBAL_THRESHOLD):
                if _random.random() > _ADVERSE_FILL_PROB:
                    self._adverse_sel_blocks += 1
                    self._partial_skip_adverse += 1
                    continue
                self._adverse_sel_passes += 1

            # ── Partial fill probability roll ──────────────────────────────
            if _random.random() >= _PARTIAL_FILL_PROB:
                self._partial_skip_prob += 1
                continue

            # ── Award fill at trade_price ──────────────────────────────────
            entry = self._engine.get_order(cpp_id)
            if entry is None or not entry.active:
                continue
            fill_qty   = float(entry.remaining_size)
            fill_price = trade_price
            fee        = fill_qty * fill_price * _MAKER_RATE
            sign       = -1.0 if side == "buy" else +1.0
            # Clean up all tracking before appending
            self._engine.cancel_order(cpp_id)
            self._cpp_to_str.pop(cpp_id, None)
            self._str_to_cpp.pop(str_id, None)
            self._order_sides.pop(str_id, None)
            self._placed_at.pop(str_id, None)
            self._placed_queue_ahead.pop(str_id, None)
            self._partial_fills += 1
            fills.append(SimFill(
                order_id=str_id, side=side,
                qty=fill_qty, price=fill_price,
                fee=fee, is_maker=True,
                net_pnl=sign * fill_qty * fill_price - fee,
            ))

        if _log_partial and (self._partial_skip_no_qa + self._partial_skip_pct +
                             self._partial_skip_prob + self._partial_fills > 0):
            print(
                f"  [partial-fill-dbg] skip_latency={self._partial_skip_latency}"
                f"  skip_no_qa={self._partial_skip_no_qa}"
                f"  skip_pct={self._partial_skip_pct}"
                f"  skip_adverse={self._partial_skip_adverse}"
                f"  skip_prob={self._partial_skip_prob}"
                f"  fills={self._partial_fills}"
            )

        return fills

    def simulate_fills(self, best_bid: float, best_ask: float,
                       sym: str) -> list[SimFill]:
        """
        Called every tick from bybit_order_manager.simulate_dry_fills().
        In the C++ model fills are driven by on_trade() events, so this
        method only handles edge cases (book crossed, immediate fill).
        Latency gate also applied here.  Adverse selection gate is NOT
        applied here — book-crossed orders are not adverse-selection scenarios.
        """
        import time as _time
        now   = _time.monotonic()
        fills: list[SimFill] = []
        if best_bid > 0 and best_ask > 0:
            for str_id, side in list(self._order_sides.items()):
                if now - self._placed_at.get(str_id, 0.0) < self._latency_s:
                    continue
                cpp_id = self._str_to_cpp.get(str_id)
                if cpp_id is None:
                    continue
                entry = self._engine.get_order(cpp_id)
                if entry is None or not entry.active:
                    continue
                crossed = (side == "buy"  and entry.price >= best_ask) or \
                          (side == "sell" and entry.price <= best_bid)
                if crossed:
                    cpp_side = _cpp.Side.BID if side == "buy" else _cpp.Side.ASK
                    lp = best_ask if side == "buy" else best_bid
                    agg_fills = self._engine.place_aggressive(
                        cpp_side, entry.remaining_size, lp)
                    self._engine.cancel_order(cpp_id)
                    self._str_to_cpp.pop(str_id, None)
                    self._cpp_to_str.pop(cpp_id, None)
                    self._order_sides.pop(str_id, None)
                    for f in agg_fills:
                        sign = -1.0 if side == "buy" else +1.0
                        fills.append(SimFill(
                            order_id=str_id, side=side,
                            qty=f.filled_qty, price=f.fill_price,
                            fee=f.fee, is_maker=False,
                            net_pnl=sign * f.filled_qty * f.fill_price - f.fee,
                        ))
        return fills

    def fill_gate_stats(self) -> dict:
        """Return diagnostic counters for the fill probability gates."""
        return {
            "adverse_sel_blocks":   self._adverse_sel_blocks,
            "adverse_sel_passes":   self._adverse_sel_passes,
            "partial_skip_no_qa":   self._partial_skip_no_qa,
            "partial_skip_pct":     self._partial_skip_pct,
            "partial_skip_adverse": self._partial_skip_adverse,
            "partial_skip_prob":    self._partial_skip_prob,
            "partial_fills":        self._partial_fills,
        }

    def reset(self) -> None:
        self._engine.reset_all()
        self._str_to_cpp.clear()
        self._cpp_to_str.clear()
        self._order_sides.clear()
        self._placed_at.clear()
        self._placed_queue_ahead.clear()
        self._bid_levels.clear()
        self._ask_levels.clear()
        self._book_imbalance  = 0.0
        self._adverse_sel_blocks = 0
        self._adverse_sel_passes = 0
        self._partial_skip_latency  = 0
        self._partial_skip_no_qa    = 0
        self._partial_skip_cpp_gone = 0
        self._partial_skip_pct      = 0
        self._partial_skip_adverse  = 0
        self._partial_skip_prob     = 0
        self._partial_fills         = 0
        self._partial_log_tick      = 0


# ── Public factory ─────────────────────────────────────────────────────────────

def make_fill_engine(latency_ms: float = 0.0) -> "_NaiveFillEngine | _CppFillEngine":
    """Return C++ engine if available, else naive fallback.

    Args:
        latency_ms: Simulated one-way order propagation latency in milliseconds.
                    Orders placed within this window before a market trade are
                    not eligible to fill (they haven't reached Bybit yet).
                    Recommended: 10ms for EC2 ap-southeast-1 → Bybit Singapore.
                    Default 0.0 = no latency (local / native testing).
    """
    if _CPP_AVAILABLE:
        return _CppFillEngine(maker_rate=_MAKER_RATE, taker_rate=_MAKER_RATE,
                              latency_s=latency_ms / 1000.0)
    return _NaiveFillEngine()


# ── Fee-only helper (usable without the full engine) ─────────────────────────

def compute_fee(qty: float, price: float) -> float:
    """Maker rebate: always negative (we earn it)."""
    return qty * price * _MAKER_RATE


def fee_summary(stats: FeeStats) -> str:
    return (f"maker_rebate=+${stats.total_maker_rebate:.4f}  "
            f"taker_fee=-${stats.total_taker_fee:.4f}  "
            f"net={'+' if stats.net >= 0 else ''}{stats.net:.4f}")
