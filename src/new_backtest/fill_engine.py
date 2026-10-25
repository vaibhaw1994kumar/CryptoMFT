"""Execution simulation: aggressive (taker) and passive (maker) fills.

Taker fills walk the book. Taking more than the touch size eats successive
levels, so the realised price is a VWAP, not the touch. A limit price bounds the
walk, which is what turns the order into an IOC and produces the partial fills
that occur when the book moves during our flight time.

Maker fills use the queue model plus EmpiricalFillModel from altcoin_mm_v1.
When we join a price level, the resting size at that level is our queue ahead;
trades and cancels at that price drain it before any of our size trades. The
three drain sources, in descending confidence:

    trade at our price   fill_share 1.00   a real taker crossed us
    delta shrink         fill_share 0.15   level shrank — could be cancels ahead
                                           of us or behind; 0.15 is the
                                           conservative share attributable to us
    snapshot shrink      fill_share 0.05   coarsest signal, lowest weight

Those shares match backtest/bybit_backtester.py so results stay comparable with
the existing single-venue Bybit runs.

Every completed maker order is fed back to EmpiricalFillModel as a
LoggedOrderEvent, so the Bayesian posterior learns this venue's fill behaviour
during the run and can be warm-started into a second pass.
"""
from __future__ import annotations

import itertools
import math
from collections import deque
from dataclasses import dataclass, field

from .fill_model import EmpiricalFillModel
from .models import LambdaBucket, LoggedOrderEvent, Side
from .order_book import OrderBook, WalkResult


# ── Results ───────────────────────────────────────────────────────────────────

@dataclass
class Fill:
    ts: float
    venue: str
    side: str            # "buy" | "sell"
    qty: float
    price: float         # VWAP actually achieved
    fee: float           # quote currency; negative = rebate earned
    is_taker: bool
    levels_consumed: int = 0
    slippage_bps: float = 0.0     # vs the price the strategy expected
    order_id: int = 0
    signal_id: int = 0
    is_synthetic: bool = False    # True when triggered by BackgroundAgentModel

    @property
    def notional(self) -> float:
        return self.price * self.qty

    @property
    def signed_qty(self) -> float:
        return self.qty if self.side == "buy" else -self.qty

    @property
    def cash_delta(self) -> float:
        """Change in quote balance. Buying spends notional and pays the fee."""
        if self.side == "buy":
            return -(self.notional + self.fee)
        return self.notional - self.fee


@dataclass
class TakerOutcome:
    filled: bool
    fill: Fill | None
    requested_qty: float
    reason: str          # filled | partial | no_liquidity | price_moved


# ── Taker ─────────────────────────────────────────────────────────────────────

class TakerExecutor:
    """Aggressive execution against a replayed book."""

    def __init__(self, consume_liquidity: bool = True) -> None:
        # When True our own fill removes the liquidity it took, so two signals in
        # the same instant cannot both fill off the same resting size. The next
        # feed update overwrites the book, bounding own-impact to sub-tick time.
        self.consume_liquidity = consume_liquidity
        self._ids = itertools.count(1)

    def execute(
        self,
        book: OrderBook,
        side: str,                 # "buy" | "sell"
        qty: float,
        limit_price: float,
        fee_bps: float,
        now: float,
        expected_price: float = 0.0,
        signal_id: int = 0,
    ) -> TakerOutcome:
        if qty <= 0 or not book.ready:
            return TakerOutcome(False, None, qty, "no_liquidity")

        book_side = "ask" if side == "buy" else "bid"
        walk = book.consume(book_side, qty, limit_price) if self.consume_liquidity \
            else book.walk(book_side, qty, limit_price)

        if walk.is_empty:
            # The book moved through our limit during flight — the opportunity
            # was gone before the order landed. This is the dominant failure
            # mode of cross-exchange arb and must not be silently dropped.
            return TakerOutcome(False, None, qty, "price_moved")

        notional = walk.vwap * walk.filled_qty
        fee = notional * (fee_bps / 10_000.0)

        slippage_bps = 0.0
        if expected_price > 0:
            sign = 1.0 if side == "buy" else -1.0
            slippage_bps = sign * (walk.vwap - expected_price) / expected_price * 10_000.0

        fill = Fill(
            ts=now, venue=book.venue, side=side, qty=walk.filled_qty, price=walk.vwap,
            fee=fee, is_taker=True, levels_consumed=walk.levels_consumed,
            slippage_bps=slippage_bps, order_id=next(self._ids), signal_id=signal_id,
        )
        return TakerOutcome(True, fill, qty, "partial" if walk.exhausted else "filled")


# ── Maker ─────────────────────────────────────────────────────────────────────

@dataclass
class MakerOrder:
    order_id: int
    venue: str
    pair: str
    symbol: str
    side: Side
    price: float
    size: float
    remaining: float
    queue_ahead: float
    initial_queue_ahead: float       # snapshot at placement for unbiased label feature
    placed_ts: float
    active_ts: float                 # placed_ts + entry latency
    signal_id: int = 0

    # Features captured at placement, replayed into EmpiricalFillModel on close.
    relative_to_touch_ticks: int = 0
    local_depth: float = 0.0
    local_depth_baseline: float = 0.0
    spread_pct: float = 0.0
    short_vol: float = 0.0
    trade_count: float = 0.0
    trade_sign: float = 0.0
    regime_bucket: LambdaBucket = LambdaBucket.CALM

    observed_level_size: float = 0.0
    level_was_absent: bool = False     # True when _level_size() returned 0.0 at placement
    filled_qty: float = 0.0
    first_fill_ts: float | None = None
    first_fill_is_synthetic: bool = False  # True when the very first fill came from synthetic flow
    adverse_ts: float | None = None
    trade_fill_qty: float = 0.0
    delta_fill_qty: float = 0.0
    snapshot_fill_qty: float = 0.0
    synthetic_fill_qty: float = 0.0   # qty filled by BackgroundAgentModel synthetic trades

    # Cancel lifecycle (set by the backtester's lifetime handling; None until a cancel is requested).
    # Between cancel_requested_ts and cancel_effective_ts the order is still resting at the exchange and can fill.
    cancel_requested_ts: float | None = None
    cancel_effective_ts: float | None = None
    cancel_reason: str = ""            # "expired" | "end_of_data"
    filled_at_cancel_request: float = 0.0

    @property
    def is_live(self) -> bool:
        return self.remaining > 1e-12

    @property
    def state(self) -> str:
        """'active' (resting, no cancel asked for) or 'cancel_pending' (cancel sent, not yet applied at the exchange)."""
        return "active" if self.cancel_requested_ts is None else "cancel_pending"

    @property
    def side_str(self) -> str:
        return "buy" if self.side == Side.BID else "sell"


@dataclass
class MakerConfig:
    trade_fill_share: float = 1.00
    delta_fill_share: float = 0.15
    snapshot_fill_share: float = 0.05
    queue_stress_multiplier: float = 1.0    # >1 assumes more size ahead of us
    fill_horizons_s: tuple[int, ...] = (1, 5, 15)
    # Minimum queue depth assumed at placement when _level_size() returns zero
    # (absent price level in the book).  Zero means "no orders ahead" — fills
    # on the first touching trade, which is optimistic for liquid instruments.
    # Units: base-asset quantity, same scale as order size and level sizes.
    # Calibrate from observed time-to-first-fill distributions by venue/pair.
    # Interaction with queue_stress_multiplier: the effective queue at placement
    # is max(level_size * queue_stress_multiplier, min_queue_ahead).  It is a
    # floor for absent levels only, not a blanket override of the multiplier.
    min_queue_ahead: float = 0.0
    # One public trade can fill our resting orders only once in total: the volume is shared in
    # price-then-time priority instead of being handed in full to every order (False = legacy).
    share_trade_volume: bool = True
    # A real execution is published twice, as a trade and as a depth reduction at the same price. Volume already
    # attributed to an execution by one feed is not drained again when the other feed reports it (False = legacy).
    dedupe_trade_depth: bool = True
    dedupe_window_s: float = 0.05


class MakerExecutor:
    """Passive order lifecycle with queue simulation and online fill learning."""

    def __init__(
        self,
        fill_model: EmpiricalFillModel,
        config: MakerConfig | None = None,
    ) -> None:
        self.fill_model = fill_model
        self.config = config or MakerConfig()
        self._ids = itertools.count(1)
        self.orders: dict[int, MakerOrder] = {}
        self.orders_placed = 0
        self.orders_filled = 0
        self.orders_cancelled = 0
        self._explained: dict[str, dict[tuple, deque]] = {"trade": {}, "delta": {}}

    # ── Trade / depth de-duplication ────────────────────────────────

    def _take(self, kind: str, key: tuple, now: float, want: float) -> float:
        credits = self._explained[kind].get(key)
        if not credits:
            return 0.0
        horizon = now - self.config.dedupe_window_s
        while credits and credits[0][0] < horizon:
            credits.popleft()
        taken = 0.0
        while credits and taken < want - 1e-12:
            use = min(credits[0][1], want - taken)
            taken += use
            credits[0][1] -= use
            if credits[0][1] <= 1e-12:
                credits.popleft()
        if not credits:
            del self._explained[kind][key]
        return taken

    def _add(self, kind: str, key: tuple, now: float, amount: float) -> None:
        if amount <= 1e-12:
            return
        book = self._explained[kind]
        if len(book) > 4096:
            horizon = now - self.config.dedupe_window_s
            for k in [k for k, d in book.items() if not d or d[-1][0] < horizon]:
                del book[k]
        book.setdefault(key, deque(maxlen=64)).append([now, amount])

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def place(
        self,
        venue: str,
        pair: str,
        symbol: str,
        side: Side,
        price: float,
        size: float,
        book: OrderBook,
        state,                  # MarketState
        regime_bucket: LambdaBucket,
        now: float,
        active_ts: float,
        tick_size: float,
        signal_id: int = 0,
    ) -> MakerOrder:
        level_size = self._level_size(book, side, price, tick_size)
        touch = book.best_bid if side == Side.BID else book.best_ask
        rel_ticks = 0
        if tick_size > 0 and touch > 0:
            delta = (price - touch) if side == Side.BID else (touch - price)
            rel_ticks = int(round(delta / tick_size))
            rel_ticks = max(-3, min(3, rel_ticks))

        level_was_absent = level_size == 0.0
        initial_q = level_size * max(0.0, self.config.queue_stress_multiplier)
        # Apply min_queue_ahead as a floor only when the price level is absent
        # from the book (level_size == 0).  On a genuine empty level, queue = 0
        # is correct for FIFO.  When the level is absent due to data granularity
        # or a stale book, min_queue_ahead provides a conservative proxy depth.
        if level_was_absent and self.config.min_queue_ahead > 0.0:
            initial_q = self.config.min_queue_ahead
        order = MakerOrder(
            order_id=next(self._ids), venue=venue, pair=pair, symbol=symbol,
            side=side, price=price, size=size, remaining=size,
            queue_ahead=initial_q, initial_queue_ahead=initial_q,
            placed_ts=now, active_ts=active_ts, signal_id=signal_id,
            level_was_absent=level_was_absent,
            relative_to_touch_ticks=rel_ticks,
            local_depth=state.top_depth,
            local_depth_baseline=state.top_depth_baseline,
            spread_pct=state.spread_pct,
            short_vol=state.short_vol,
            trade_count=state.recent_trade_rate,
            trade_sign=state.trade_sign,
            regime_bucket=regime_bucket,
            observed_level_size=level_size,
        )
        self.orders[order.order_id] = order
        self.orders_placed += 1
        return order

    def cancel(self, order_id: int, now: float) -> MakerOrder | None:
        order = self.orders.pop(order_id, None)
        if order is None:
            return None
        self._record_outcome(order, end_ts=now)
        self.orders_cancelled += 1
        return order

    def cancel_all(self, now: float) -> list[MakerOrder]:
        out = []
        for oid in list(self.orders):
            o = self.cancel(oid, now)
            if o is not None:
                out.append(o)
        return out

    def live_orders(self, venue: str | None = None) -> list[MakerOrder]:
        if venue is None:
            return list(self.orders.values())
        return [o for o in self.orders.values() if o.venue == venue]

    # ── Fill triggers ────────────────────────────────────────────────────────

    def on_trade(
        self, venue: str, price: float, volume: float, sign: int, now: float, fee_bps: float,
        is_synthetic: bool = False,
    ) -> list[Fill]:
        """A public trade crossed the book. Fill any resting order it reached."""
        if self.config.dedupe_trade_depth:
            key = (venue, "bid" if sign < 0 else "ask", round(price, 8))
            volume -= self._take("delta", key, now, volume)
            self._add("trade", key, now, volume)
            if volume <= 1e-12:
                return []
        fills: list[Fill] = []
        eligible = []
        for order in list(self.orders.values()):
            if order.venue != venue or not order.is_live or now + 1e-12 < order.active_ts:
                continue
            # sign=-1: sell aggressor hits bids. sign=+1: buy aggressor lifts asks.
            if sign < 0 and order.side == Side.BID and price <= order.price + 1e-12:
                eligible.append(order)
            elif sign > 0 and order.side == Side.ASK and price >= order.price - 1e-12:
                eligible.append(order)
        share = self.config.share_trade_volume
        if share:
            eligible.sort(key=lambda o: (-o.price if o.side == Side.BID else o.price, o.placed_ts, o.order_id))
        remaining = volume
        for order in eligible:
            queue_before = order.queue_ahead
            f = self._consume(order, remaining if share else volume, now, self.config.trade_fill_share, "trade", fee_bps, is_synthetic)
            if f is not None:
                fills.append(f)
            if share:
                absorbed = queue_before - order.queue_ahead
                if f is not None and self.config.trade_fill_share > 0:
                    absorbed += f.qty / self.config.trade_fill_share
                remaining -= absorbed
                if remaining <= 1e-12:
                    break
        return fills

    def on_delta(
        self, venue: str, side: str, price: float, new_size: float, now: float,
        tick_size: float, fee_bps: float,
    ) -> list[Fill]:
        """A book delta shrank our level. Part of the shrink may be our queue."""
        fills: list[Fill] = []
        want = Side.BID if side.startswith("b") else Side.ASK
        at_level = []
        for order in list(self.orders.values()):
            if order.venue != venue or order.side != want or not order.is_live:
                continue
            if now + 1e-12 < order.active_ts:
                continue
            if abs(price - order.price) > max(1e-12, 0.5 * tick_size):
                continue
            at_level.append(order)
        shrinks = {o.order_id: o.observed_level_size - new_size for o in at_level if o.observed_level_size > new_size}
        explained = 0.0
        if self.config.dedupe_trade_depth and shrinks:
            key = (venue, "bid" if want == Side.BID else "ask", round(price, 8))
            level_shrink = max(shrinks.values())
            explained = self._take("trade", key, now, level_shrink)
            self._add("delta", key, now, level_shrink - explained)
        for order in at_level:
            shrink = shrinks.get(order.order_id, 0.0) - explained
            if shrink > 1e-12:
                f = self._consume(order, shrink, now, self.config.delta_fill_share, "delta", fee_bps)
                if f is not None:
                    fills.append(f)
            # Never increase observed_level_size: orders that join the level
            # after our placement arrive behind us in the FIFO queue.  Allowing
            # the tracked size to grow upward would incorrectly trigger fills
            # when those behind-us orders later cancel or trade.
            order.observed_level_size = min(order.observed_level_size, new_size)
        return fills

    def on_snapshot(
        self, venue: str, book: OrderBook, now: float, tick_size: float, fee_bps: float,
    ) -> list[Fill]:
        """Level shrank between snapshots — weakest signal, smallest share."""
        fills: list[Fill] = []
        for order in list(self.orders.values()):
            if order.venue != venue or not order.is_live or now + 1e-12 < order.active_ts:
                continue
            curr = self._level_size(book, order.side, order.price, tick_size)
            if order.observed_level_size > curr:
                f = self._consume(order, order.observed_level_size - curr, now,
                                  self.config.snapshot_fill_share, "snapshot", fee_bps)
                if f is not None:
                    fills.append(f)
            # Same invariant as on_delta: never increase observed_level_size.
            order.observed_level_size = min(order.observed_level_size, curr)
        return fills

    def mark_adverse(self, venue: str, book: OrderBook, now: float, tick_size: float) -> None:
        """Record when the market first traded through a resting order — the
        adverse-selection label EmpiricalFillModel learns from."""
        for order in self.orders.values():
            if order.venue != venue or order.adverse_ts is not None or not book.ready:
                continue
            if order.side == Side.BID and book.best_bid < order.price - 0.5 * tick_size:
                order.adverse_ts = now
            elif order.side == Side.ASK and book.best_ask > order.price + 0.5 * tick_size:
                order.adverse_ts = now

    # ── Internals ────────────────────────────────────────────────────────────

    def _consume(
        self, order: MakerOrder, volume: float, now: float,
        fill_share: float, source: str, fee_bps: float,
        is_synthetic: bool = False,
    ) -> Fill | None:
        """Drain queue first, then fill our own size from what is left."""
        if volume <= 0 or not order.is_live:
            return None

        available = volume
        if order.queue_ahead > 0:
            drained = min(order.queue_ahead, available)
            order.queue_ahead -= drained
            available -= drained
        if available <= 0 or fill_share <= 0:
            return None

        qty = min(order.remaining, available * fill_share)
        if qty <= 1e-12:
            return None

        order.remaining -= qty
        order.filled_qty += qty
        if order.first_fill_ts is None:
            order.first_fill_ts = now
            order.first_fill_is_synthetic = is_synthetic
        if is_synthetic:
            order.synthetic_fill_qty += qty
        elif source == "trade":
            order.trade_fill_qty += qty
        elif source == "delta":
            order.delta_fill_qty += qty
        else:
            order.snapshot_fill_qty += qty

        notional = order.price * qty
        fee = notional * (fee_bps / 10_000.0)
        fill = Fill(
            ts=now, venue=order.venue, side=order.side_str, qty=qty, price=order.price,
            fee=fee, is_taker=False, order_id=order.order_id, signal_id=order.signal_id,
            is_synthetic=is_synthetic,
        )

        if not order.is_live:
            self.orders.pop(order.order_id, None)
            # Skip training label only when the FIRST fill was synthetic — that
            # corrupts the time-to-fill timestamp (first_fill_ts = synthetic event time).
            # A real trade that later completes a partially-synthetic order is still a
            # valid observation; excluding it was overly conservative.
            if not order.first_fill_is_synthetic:
                self._record_outcome(order)
            self.orders_filled += 1
        return fill

    @staticmethod
    def _level_size(book: OrderBook, side: Side, price: float, tick_size: float) -> float:
        levels = book.levels("bid" if side == Side.BID else "ask", 50)
        tol = max(1e-12, 0.5 * tick_size)
        for lv_price, lv_size in levels:
            if abs(lv_price - price) <= tol:
                return lv_size
        return 0.0

    def _record_outcome(self, order: MakerOrder, end_ts: float | None = None) -> None:
        """Feed the completed order to EmpiricalFillModel, one event per horizon.

        `filled` is horizon-conditional: an order that filled after 12s counts as
        filled at the 15s horizon but not at 1s or 5s.

        Censoring: pass end_ts=now when cancelling. An observation at horizon h is
        emitted only if the order filled before h OR survived for at least h seconds.
        Without end_ts (full fills routed from _consume), fill_ts is finite and
        life_s is computed from it correctly.

        Feature: queue_ahead is snapshotted at placement (initial_queue_ahead) not at
        close, because _consume() decrements it as fills arrive.
        """
        fill_ts = order.first_fill_ts if order.first_fill_ts is not None else math.inf
        adverse_ts = order.adverse_ts if order.adverse_ts is not None else math.inf
        ratio = order.filled_qty / max(order.size, 1e-9)

        if order.filled_qty >= order.size - 1e-9:
            terminal_reason = "full_fill"
        elif order.filled_qty > 0:
            terminal_reason = "partial_then_cancel"
        else:
            terminal_reason = "cancel"

        # life_s: how long the order was observable, measured from active_ts (the
        # moment the order landed at the exchange after entry latency).  Using
        # placed_ts (strategy decision time) inflates TTF by the entry latency.
        # For fills: time-to-first-fill (fill_ts is finite).
        # For cancels: end_ts is the cancel timestamp; fall back to fill_ts if
        #   partially filled (partial_then_cancel), else None if neither is known.
        t0 = order.active_ts  # exchange-arrival time; equals placed_ts + entry_latency
        if fill_ts < math.inf:
            life_s: float | None = fill_ts - t0
        elif end_ts is not None:
            life_s = end_ts - t0
        else:
            life_s = None

        for horizon in self.config.fill_horizons_s:
            filled_by_h = fill_ts - t0 <= horizon
            # observation_time_s: how long this order was observable.
            # For fills: time-to-first-fill. For cancels: cancel_time_s.
            # When neither is known (life_s is None), use the horizon itself as a
            # conservative upper bound (order was alive at least until h).
            obs_time = life_s if life_s is not None else float(horizon)

            self.fill_model.record_order_event(
                LoggedOrderEvent(
                    timestamp=order.placed_ts,
                    symbol=order.symbol,
                    side=order.side,
                    relative_to_touch_ticks=order.relative_to_touch_ticks,
                    queue_ahead=max(0.0, order.initial_queue_ahead),  # placement snapshot
                    local_depth=order.local_depth,
                    local_depth_baseline=order.local_depth_baseline,
                    spread_pct=order.spread_pct,
                    short_vol=order.short_vol,
                    trade_count=order.trade_count,
                    trade_sign=order.trade_sign,
                    regime_bucket=order.regime_bucket,
                    horizon_s=horizon,
                    filled=filled_by_h,
                    price_moved_through=(adverse_ts - t0) <= horizon,
                    filled_qty=order.filled_qty,
                    fill_ratio=ratio,
                    trade_fill_qty=order.trade_fill_qty,
                    book_event_fill_qty=order.delta_fill_qty,
                    snapshot_fill_qty=order.snapshot_fill_qty,
                    terminal_reason=terminal_reason,
                    observation_time_s=obs_time,
                    level_was_absent=order.level_was_absent,
                )
            )

    def stats(self) -> dict[str, float]:
        return {
            "maker_orders_placed": float(self.orders_placed),
            "maker_orders_filled": float(self.orders_filled),
            "maker_orders_cancelled": float(self.orders_cancelled),
            "maker_fill_rate": self.orders_filled / max(1, self.orders_placed),
        }
