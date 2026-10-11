"""
Phase-Based Queue-Aware Execution
==================================
Tracks each active order from placement through four execution phases.
The goal: preserve queue priority while exiting intelligently when the
queue has turned against us.

Phases
------
  Phase 1 (0 – phase1_end_s, default 60s):
      HOLD.  Do nothing.  Let the order work.  Queue priority is highest
      right after placement — churning resets it to the back.
      Exception: extreme TFI (< 0.20 or > 0.80) → CANCEL immediately.

  Phase 2 (phase1_end_s – phase2_end_s, default 60–120s):
      AMEND_SIZE only.  Linearly decay order qty from 100% → min_size_fraction
      (default 50%) to reduce inventory risk without changing price.
      Note: on Bybit, qty amend also resets queue position — but it is
      still atomic (no gap in book) and halves the API call count vs cancel+place.

  Phase 3 (phase2_end_s+, default 120s+):
      Conditional REPLACE / CANCEL.
        - Queue depth grew > growth_threshold (20%) since placement → REPLACE
        - Flow turned adversely (TFI < 0.28 bid, > 0.72 ask) → CANCEL
        - Otherwise → HOLD

  Phase 4 (hard exit):
      Handled externally by age-force / age-loss / hard-cap logic in
      main_bybit.py — not the queue tracker's responsibility.

Key design invariants
---------------------
- Never REPLACE at unchanged price in Phase 1 or 2.
- CANCEL without replace only when flow is genuinely adverse (not just noisy).
- Minimum amend interval (default 15s) prevents size-amend thrashing in Phase 2.
- Extreme TFI override fires in ALL phases — it is a hard risk gate.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import time


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

class OrderAction(Enum):
    HOLD       = "HOLD"
    AMEND_SIZE = "AMEND_SIZE"   # reduce qty, keep price — preserves queue atomically
    REPLACE    = "REPLACE"      # cancel + re-place at fresh best price
    CANCEL     = "CANCEL"       # cancel without re-placing (flow turned adverse)


# ---------------------------------------------------------------------------
# Decision returned by evaluate()
# ---------------------------------------------------------------------------

@dataclass
class QueueDecision:
    """Result of QueueTracker.evaluate()."""
    action:        OrderAction
    size_fraction: float = 1.0   # AMEND_SIZE only: new_size = original_size × fraction


# ---------------------------------------------------------------------------
# Per-order state
# ---------------------------------------------------------------------------

@dataclass
class TrackedOrder:
    order_id:           str
    side:               str    # 'bid' or 'ask'
    price:              float
    size:               float  # current tracked size (updated after each amend)
    original_size:      float  # size at first placement
    placed_at:          float  # time.monotonic() at placement
    depth_at_placement: float  # queue depth heuristic at placement
    last_amend_at:      float = field(default_factory=time.monotonic)
    # ── Smoothing state ─────────────────────────────────────────────────────
    consec_breach:      int   = 0    # consecutive ticks where depth_growth > threshold
    improving:          bool  = False  # True when queue is improving (growth < improve_threshold)
    # ── Exit flag ────────────────────────────────────────────────────────────
    is_exit:            bool  = False  # True when this ask is unwinding existing inventory
    #   Exit asks skip extreme-TFI cancel — we must exit regardless of buy pressure


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class QueueTrackerConfig:
    # ── Phase timing ──────────────────────────────────────────────────────────
    phase1_end_s:          float = 60.0    # pure hold
    phase2_end_s:          float = 120.0   # size decay only
    # (Phase 3 starts at phase2_end_s; no upper bound — age-force handles hard exit)

    # ── Queue depth monitoring ────────────────────────────────────────────────
    growth_threshold:      float = 0.20    # Phase 3: breach threshold (>20% depth growth)
    consec_breach_needed:  int   = 3       # REPLACE only after this many consecutive breaches
    improve_threshold:     float = 0.10    # queue is "improving" if growth < −10% (shrinking)
    # When queue is improving: hold aggressively — reset breach counter, skip all actions.

    # ── Size decay (Phase 2) ─────────────────────────────────────────────────
    min_size_fraction:     float = 0.50    # floor: never amend below 50% of original
    size_amend_interval_s: float = 15.0    # min seconds between size amends

    # ── Flow-based cancel thresholds ──────────────────────────────────────────
    # evaluate() receives final_flow = pressure_fast × arrival_imbalance
    #   arrival_imbalance = (buy_int - sell_int) / (buy_int + sell_int) ∈ [-1, +1]
    #
    # Signal properties:
    #   ≈  0.0  balanced or quiet market (no cancel)
    #   ≈ ±0.5  moderate confirmed directional flow
    #   ≈ ±1.0  pure confirmed directional flow (extreme)
    #
    # Thresholds are tighter than the old pfast-only values (±1.5 / ±0.8)
    # because arrival_imbalance confirmation shrinks the false-positive rate —
    # we can afford to act at a lower threshold without over-cancelling.
    # Keep extreme loose (±0.8) so we only cancel on genuinely confirmed flow.
    # When pressure is None (v1 fallback), raw TFI thresholds are used.
    #
    # Extreme (all phases) — only for NEW orders (is_exit=False):
    pressure_extreme_bid:  float = -0.8   # cancel new bid if final_flow < this
    pressure_extreme_ask:  float = +0.8   # cancel new ask if final_flow > this
    # Fallback raw TFI (used when pressure is None):
    tfi_extreme_bid:       float = 0.15   # very tight — only extreme sell
    tfi_extreme_ask:       float = 0.85   # very tight — only extreme buy
    # Moderate (Phase 3 only):
    pressure_cancel_bid:   float = -0.5   # cancel bid Phase3 if final_flow < this
    pressure_cancel_ask:   float = +0.5   # cancel ask Phase3 if final_flow > this
    # Fallback TFI moderate:
    tfi_cancel_bid_lo:     float = 0.25
    tfi_cancel_ask_hi:     float = 0.75


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

class QueueTracker:
    """
    One instance per trading pair.

    Typical usage in PairEngine (main_bybit.py)
    -------------------------------------------
    # After placing an order:
    depth = state.bid_size if side == 'bid' else state.ask_size
    self._queue_tracker.register(order_id, side, price, size, depth)

    # Each on_quote_tick(), before computing new quotes:
    flow_sig = self._flow.signal()
    decision = self._queue_tracker.evaluate(
        order_id, current_depth, flow_sig.tfi, side)

    if decision.action == OrderAction.AMEND_SIZE:
        new_size = tracked.size * decision.size_fraction
        await order_manager.amend_qty(order_id, symbol, new_size)
        self._queue_tracker.update_size(order_id, new_size)

    elif decision.action in (OrderAction.REPLACE, OrderAction.CANCEL):
        await order_manager.cancel_order(order_id, symbol)
        self._queue_tracker.unregister(order_id)
        ps.bid_order = None  # trigger retick on next cycle

    # On fill or cancel confirmation:
    self._queue_tracker.unregister(order_id)
    """

    def __init__(self, config: QueueTrackerConfig | None = None) -> None:
        self._cfg    = config or QueueTrackerConfig()
        self._orders: dict[str, TrackedOrder] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        order_id:  str,
        side:      str,
        price:     float,
        size:      float,
        depth_now: float,
        is_exit:   bool = False,
    ) -> None:
        """Call immediately after a new order is placed.

        Args:
            is_exit: True when this ask is liquidating existing inventory.
                     Exit asks skip extreme-TFI cancel — must exit regardless
                     of buy pressure. New asks (is_exit=False) are protected.
        """
        t0 = time.monotonic()
        self._orders[order_id] = TrackedOrder(
            order_id=order_id,
            side=side,
            price=price,
            size=size,
            original_size=size,
            placed_at=t0,
            depth_at_placement=max(depth_now, 1e-9),
            last_amend_at=t0,
            consec_breach=0,
            improving=False,
            is_exit=is_exit,
        )

    def unregister(self, order_id: str) -> None:
        """Call on fill, cancel, or explicit removal."""
        self._orders.pop(order_id, None)

    def update_size(self, order_id: str, new_size: float) -> None:
        """Call after a successful AMEND_SIZE to keep tracked size current."""
        order = self._orders.get(order_id)
        if order:
            order.size = new_size
            order.last_amend_at = time.monotonic()

    def clear(self) -> None:
        self._orders.clear()

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        order_id:      str,
        current_depth: float,
        tfi:           float,
        side:          str,        # 'bid' or 'ask'
        pressure:      float | None = None,  # FlowSignal.pressure_fast if available
    ) -> QueueDecision:
        """
        Decide what to do with an active order.

        Args:
            pressure: Normalized flow pressure from FlowFeatureEngine v2.
                      If provided, used instead of raw TFI for flow gating.
                      Negative = sell pressure, Positive = buy pressure.
                      None = fall back to raw TFI thresholds.

        Returns a QueueDecision(action, size_fraction).
        """
        order = self._orders.get(order_id)
        if order is None:
            return QueueDecision(OrderAction.HOLD)

        cfg   = self._cfg
        now   = time.monotonic()
        age_s = now - order.placed_at

        # ── Extreme flow override (all phases, new orders only) ───────────────
        # Exit asks (is_exit=True) skip — must unwind regardless of buy pressure.
        if not order.is_exit:
            if pressure is not None:
                # v2: normalized pressure — much less noisy than raw TFI
                if side == "bid" and pressure < cfg.pressure_extreme_bid:
                    return QueueDecision(OrderAction.CANCEL)
                if side == "ask" and pressure > cfg.pressure_extreme_ask:
                    return QueueDecision(OrderAction.CANCEL)
            else:
                # v1 fallback: raw TFI (tight thresholds to avoid over-cancelling)
                if side == "bid" and tfi < cfg.tfi_extreme_bid:
                    return QueueDecision(OrderAction.CANCEL)
                if side == "ask" and tfi > cfg.tfi_extreme_ask:
                    return QueueDecision(OrderAction.CANCEL)

        # ── Phase 1: Pure hold (0 – phase1_end_s) ────────────────────────────
        # Queue priority is highest right after placement. Do nothing.
        if age_s < cfg.phase1_end_s:
            return QueueDecision(OrderAction.HOLD)

        # ── Phase 2: Size decay only (phase1_end_s – phase2_end_s) ───────────
        # Reduce qty linearly from 100% → min_size_fraction while keeping price.
        # Goal: shrink inventory risk without abandoning queue position.
        if age_s < cfg.phase2_end_s:
            elapsed_in_phase = age_s - cfg.phase1_end_s
            phase_len        = cfg.phase2_end_s - cfg.phase1_end_s   # 60s
            # Linear decay: 1.0 at phase start → min_size_fraction at phase end
            target_fraction = 1.0 - (1.0 - cfg.min_size_fraction) * (elapsed_in_phase / phase_len)
            target_fraction = max(cfg.min_size_fraction, min(1.0, target_fraction))

            current_fraction   = order.size / order.original_size if order.original_size > 0 else 1.0
            time_since_amend   = now - order.last_amend_at
            meaningful_decay   = target_fraction < current_fraction - 0.05   # >5% reduction

            if meaningful_decay and time_since_amend >= cfg.size_amend_interval_s:
                return QueueDecision(OrderAction.AMEND_SIZE, target_fraction)
            return QueueDecision(OrderAction.HOLD)

        # ── Phase 3: Conditional cancel/replace (phase2_end_s+) ──────────────
        # Moderate flow-based cancel
        if pressure is not None:
            cancel_bid_p3 = side == "bid" and pressure < cfg.pressure_cancel_bid
            cancel_ask_p3 = side == "ask" and pressure > cfg.pressure_cancel_ask
        else:
            cancel_bid_p3 = side == "bid" and tfi < cfg.tfi_cancel_bid_lo
            cancel_ask_p3 = side == "ask" and tfi > cfg.tfi_cancel_ask_hi

        if cancel_bid_p3 or cancel_ask_p3:
            order.consec_breach = 0
            order.improving = False
            return QueueDecision(OrderAction.CANCEL)

        # ── Queue depth: smoothed deterioration + improvement detection ────────
        depth_growth = (current_depth - order.depth_at_placement) / order.depth_at_placement

        # Queue IMPROVING: depth shrinking meaningfully vs placement baseline.
        # This means cancels are being pulled or fills happening ahead of us —
        # our relative queue position is getting better.  Hold aggressively.
        if depth_growth < -cfg.improve_threshold:
            order.consec_breach = 0     # reset any pending breach counter
            order.improving = True
            return QueueDecision(OrderAction.HOLD)

        # Queue neutral or slightly worsening: reset improvement flag, keep watching.
        order.improving = False

        # Queue DETERIORATING: depth grew above threshold.
        # Require consec_breach_needed consecutive ticks to filter spoof/flicker.
        if depth_growth > cfg.growth_threshold:
            order.consec_breach += 1
            if order.consec_breach >= cfg.consec_breach_needed:
                order.consec_breach = 0  # reset so next order starts clean
                return QueueDecision(OrderAction.REPLACE)
            # Not enough consecutive ticks yet — HOLD and keep counting
            return QueueDecision(OrderAction.HOLD)

        # Growth between -improve_threshold and growth_threshold → neutral, reset counter
        order.consec_breach = 0
        return QueueDecision(OrderAction.HOLD)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def queue_score(self, order_id: str, current_depth: float) -> float:
        """
        Heuristic 0.0 (front) … 1.0 (back) queue position score.
        Based on depth growth since placement.
        """
        order = self._orders.get(order_id)
        if order is None:
            return 0.0
        growth = (current_depth - order.depth_at_placement) / order.depth_at_placement
        return float(min(1.0, max(0.0, growth)))

    def active_count(self) -> int:
        return len(self._orders)

    def get(self, order_id: str) -> TrackedOrder | None:
        return self._orders.get(order_id)

    def phase(self, order_id: str) -> int:
        """Return current phase (1-3) for an order, or 0 if not tracked."""
        order = self._orders.get(order_id)
        if order is None:
            return 0
        age_s = time.monotonic() - order.placed_at
        if age_s < self._cfg.phase1_end_s:
            return 1
        if age_s < self._cfg.phase2_end_s:
            return 2
        return 3
