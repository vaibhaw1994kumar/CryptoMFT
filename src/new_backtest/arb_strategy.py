"""Cross-venue arbitrage signal generation.

The edge, stated plainly: when venue A's best ask is below venue B's best bid by
more than the round-trip fee, buying on A and selling on B locks in the
difference. Everything hard about this is in the qualifiers.

Gates, and why each one exists — each blocks a distinct way a backtest invents
profit that does not exist live:

  staleness   A book that has not updated in 500ms is not a price, it is a
              memory. If one venue's feed gaps, the other venue moves, and the
              stale side shows an enormous spread against it. Unfiltered, feed
              gaps become the single largest source of "profit" in the run.

  crossed     A book whose bid exceeds its own ask has lost a delete. Its touch
              is not tradeable. Skip until the next snapshot resyncs it.

  toxicity    A spread that opens while depth collapses and trades run one way
              is not an opportunity — it is one venue repricing and the other
              about to follow. Taking it buys the top of a move.

  depth       Edge measured at the touch is only available for the touch size.
              Sizing beyond it walks the book and the realised VWAP can erase
              the entire spread. Size is capped by depth reachable within
              max_walk_bps.

  inventory   Selling base on venue B requires base on venue B. Cross-exchange
              arb is inventory-bounded rebalancing, not free money: withdrawals
              take minutes, so intraday you trade only what you already hold.
              This is the real capacity constraint and it is modelled, not
              assumed away.

Two execution modes:

  TAKER_TAKER  Both legs aggressive. Both fills near-certain, both pay taker
               fees (~10bps round trip). On liquid majors this is usually a
               losing structure and the backtest should say so.

  MAKER_TAKER  Rest passive on the wide venue; when it fills, immediately take
               on the tight venue. Round-trip fee falls to roughly maker+taker,
               which is where the economics actually work — at the cost of leg
               risk, since the passive fill is probabilistic and the hedge can
               slip. EmpiricalFillModel prices that fill probability.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .config import VenueSpec
from .models import Side
from .order_book import OrderBook
from .venue_state import VenueState

if TYPE_CHECKING:
    from .fill_model_v1 import FillModelV1


TAKER_TAKER = "taker_taker"
MAKER_TAKER = "maker_taker"


def validate_maker_lifetime(value: float) -> None:
    """maker_max_age_s is seconds from exchange arrival; 0 disables expiry. Negative or non-finite values are errors, not 'off'."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ValueError(f"ArbConfig.maker_max_age_s must be a finite number >= 0 (0 disables expiry), got {value!r}")


@dataclass
class ArbConfig:
    mode: str = TAKER_TAKER

    # Edge thresholds
    min_net_edge_bps: float = 1.0        # after fees, before slippage
    safety_buffer_bps: float = 0.5       # extra cushion for in-flight drift

    # Gates
    max_book_age_s: float = 0.50
    # The best level we intend to hit must have been confirmed (snapshot, delta
    # at that price, or trade at that price) within this window. The Bybit
    # collector drops most level removals, so an unconfirmed touch is often a
    # level that no longer exists.
    max_touch_age_s: float = 0.30
    max_spread_bps: float = 50.0         # absurd spread => bad data, not an edge
    block_on_toxic: bool = True
    toxic_trade_sign: float = 0.75
    toxic_depth_collapse: float = 0.60

    # Sizing
    max_clip_base: float = 0.0           # 0 => derive from max_position_base
    max_clip_notional: float = 25_000.0
    max_walk_bps: float = 2.0            # how far past touch we will consume
    min_clip_notional: float = 50.0
    depth_utilisation: float = 0.50      # take at most this share of visible depth

    # Inventory
    max_position_base: float = 1.0       # |net base across venues|
    max_venue_imbalance_base: float = 0.0  # 0 => 2x max_position_base
    # False (default, legacy): position limits look at filled inventory only. True: they also assume the worst case for
    # orders that are not in the inventory yet (resting maker remainders, maker legs in flight, hedges in flight),
    # with no netting between them. With maker_max_live_orders > 1 the legacy check can be breached by orders that
    # all fill; see PendingExposure.
    count_pending_exposure: bool = False

    # Maker-taker
    maker_improve_ticks: int = 1         # how far inside the touch we rest
    maker_requote_bps: float = 0.5       # requote if fair moves by this much
    maker_max_live_orders: int = 1
    maker_max_age_s: float = 0.0         # seconds from exchange arrival until a cancel is requested; 0 = off (rest until filled); negative/NaN raise
    maker_count_inflight: bool = True    # orders still in flight count toward maker_max_live_orders (False = legacy stacking)

    # Pacing
    min_resignal_gap_s: float = 0.0      # throttle repeat fires on one edge

    # Phase 1 fill-probability gate (MAKER_TAKER only)
    # Set min_fill_prob > 0 to skip quotes where the fitted model predicts a low
    # fill probability.  fill_gate_horizon_s selects which trained horizon to use.
    min_fill_prob: float = 0.0           # 0 = gate disabled
    fill_gate_horizon_s: int = 15
    # Stage 4: adverse-selection gate (FillModelV2 only; 1.0 = disabled)
    max_adverse_prob: float = 1.0
    # TTF gate: skip if E[TTF] > max_ttf_s.  Reduces inventory lock-up in slow
    # markets.  0.0 = disabled.  Typical value: 8.0 (seconds).
    max_ttf_s: float = 0.0
    # Cross-venue temporal coherence: reject if the two books' last_update_ts
    # differ by more than this threshold.  Prevents comparing prices that were
    # never simultaneously actionable (e.g. Bybit at T vs Binance at T-490ms).
    # 0.0 = disabled.  Default 0.2 s is half the per-venue max_book_age_s.
    max_cross_venue_skew_s: float = 0.2

    def __post_init__(self) -> None:
        validate_maker_lifetime(self.maker_max_age_s)


@dataclass
class ArbLeg:
    venue: str
    side: str                # "buy" | "sell"
    qty: float
    limit_price: float
    is_taker: bool
    expected_price: float
    fee_bps: float


@dataclass
class ArbSignal:
    signal_id: int
    ts: float
    buy_venue: str
    sell_venue: str
    qty: float
    gross_edge_bps: float
    fee_bps: float
    net_edge_bps: float
    legs: list[ArbLeg]
    mode: str
    buy_price: float
    sell_price: float
    depth_limited: bool = False
    inventory_limited: bool = False


@dataclass
class PendingExposure:
    """Base quantity (positive magnitudes) per venue that would still be bought / sold if every order that is not in the
    InventoryBook yet executed in full: resting maker remainders, maker legs in flight and hedges in flight."""
    buys: dict[str, float] = field(default_factory=dict)
    sells: dict[str, float] = field(default_factory=dict)

    def add(self, venue: str, side: str, qty: float) -> None:
        book = self.buys if side == "buy" else self.sells
        book[venue] = book.get(venue, 0.0) + qty


@dataclass
class InventoryBook:
    """Per-venue base balances, plus the aggregate net position.

    Quote balances are tracked as running cash rather than a hard constraint —
    the binding constraint in practice is base inventory on the sell side.
    """
    base: dict[str, float] = field(default_factory=dict)
    cash: dict[str, float] = field(default_factory=dict)

    def ensure(self, venues: tuple[str, ...]) -> None:
        for v in venues:
            self.base.setdefault(v, 0.0)
            self.cash.setdefault(v, 0.0)

    @property
    def net_base(self) -> float:
        return sum(self.base.values())

    @property
    def total_cash(self) -> float:
        return sum(self.cash.values())

    def apply(self, venue: str, signed_qty: float, cash_delta: float) -> None:
        self.base[venue] = self.base.get(venue, 0.0) + signed_qty
        self.cash[venue] = self.cash.get(venue, 0.0) + cash_delta

    def imbalance(self) -> float:
        """Largest single-venue absolute base position."""
        return max((abs(v) for v in self.base.values()), default=0.0)


class CrossVenueArbStrategy:
    def __init__(
        self,
        pair: str,
        venues: dict[str, VenueSpec],
        config: ArbConfig | None = None,
        fill_model_v1: "FillModelV1 | None" = None,
    ) -> None:
        self.pair = pair
        self.venues = venues
        self.config = config or ArbConfig()
        self.venue_names = tuple(sorted(venues))
        self._ids = itertools.count(1)
        self._last_signal_ts = 0.0
        self.fill_model_v1 = fill_model_v1

        self.rejects: dict[str, int] = {
            "not_ready": 0, "stale": 0, "crossed": 0, "wide_spread": 0,
            "toxic": 0, "no_edge": 0, "stale_touch": 0, "no_depth": 0,
            "inventory": 0, "clip_too_small": 0, "throttled": 0,
            "maker_limit": 0,
            "low_fill_prob": 0,   # Phase 1 gate: P(fill) < min_fill_prob
            "high_adverse_prob": 0,  # Stage 4 gate: P(adverse) > max_adverse_prob
            "slow_ttf": 0,           # TTF gate: E[TTF] > max_ttf_s
            "cross_venue_skew": 0,   # cross-venue coherence: |ts_buy - ts_sell| > max_cross_venue_skew_s
        }
        self.evaluations = 0
        self.signals = 0
        self.best_edge_seen_bps = float("-inf")

    # ── Gates ────────────────────────────────────────────────────────────────

    def _venue_tradeable(self, venue: str, book: OrderBook, state: VenueState, now: float) -> bool:
        cfg = self.config
        if not book.ready:
            self.rejects["not_ready"] += 1
            return False
        if book.is_stale(now, cfg.max_book_age_s):
            self.rejects["stale"] += 1
            return False
        if book.is_crossed():
            self.rejects["crossed"] += 1
            return False
        if book.spread_bps > cfg.max_spread_bps:
            self.rejects["wide_spread"] += 1
            return False
        if cfg.block_on_toxic and state.is_toxic(book, cfg.toxic_trade_sign, cfg.toxic_depth_collapse):
            self.rejects["toxic"] += 1
            return False
        return True

    # ── Sizing ───────────────────────────────────────────────────────────────

    def _max_clip(self, price: float) -> float:
        cfg = self.config
        clip = cfg.max_clip_base if cfg.max_clip_base > 0 else cfg.max_position_base
        if cfg.max_clip_notional > 0 and price > 0:
            clip = min(clip, cfg.max_clip_notional / price)
        return max(0.0, clip)

    def _tradeable_depth(self, book: OrderBook, side: str, touch: float) -> float:
        """Depth reachable without walking more than max_walk_bps past the touch."""
        cfg = self.config
        if touch <= 0:
            return 0.0
        slack = touch * cfg.max_walk_bps / 10_000.0
        limit = touch + slack if side == "ask" else touch - slack
        return book.depth_within(side, limit) * cfg.depth_utilisation

    def _inventory_room(
        self, buy_venue: str, sell_venue: str, inv: InventoryBook, pending: "PendingExposure | None" = None,
    ) -> float:
        """How much we can trade before breaching a position limit.

        Buying on A and selling on B leaves the net flat but moves A's balance up
        and B's down. The binding limits are the per-venue imbalance cap (we
        cannot sell base we do not hold on that venue) and the aggregate net.
        """
        cfg = self.config
        cap = cfg.max_venue_imbalance_base or (2.0 * cfg.max_position_base)
        buy_base, sell_base, net_hi, net_lo = inv.base.get(buy_venue, 0.0), inv.base.get(sell_venue, 0.0), inv.net_base, inv.net_base
        if pending is not None and cfg.count_pending_exposure:
            # Worst case per limit: every pending buy lands on the buy venue, every pending sell on the sell venue,
            # and nothing nets off against anything else.
            buy_base += pending.buys.get(buy_venue, 0.0)
            sell_base -= pending.sells.get(sell_venue, 0.0)
            net_hi += sum(pending.buys.values())
            net_lo -= sum(pending.sells.values())
        buy_room = max(0.0, cap - buy_base)
        sell_room = max(0.0, cap + sell_base)
        net_room = max(0.0, cfg.max_position_base * 2.0 - max(abs(net_hi), abs(net_lo)))
        return min(buy_room, sell_room, net_room)

    # ── Evaluation ───────────────────────────────────────────────────────────

    def evaluate(
        self,
        books: dict[str, OrderBook],
        states: dict[str, VenueState],
        inventory: InventoryBook,
        now: float,
        live_maker_orders: int = 0,
        pending: "PendingExposure | None" = None,
    ) -> ArbSignal | None:
        cfg = self.config
        self.evaluations += 1

        if cfg.min_resignal_gap_s > 0 and now - self._last_signal_ts < cfg.min_resignal_gap_s:
            self.rejects["throttled"] += 1
            return None

        # A7: enforce maker_max_live_orders before even computing the signal.
        if cfg.mode == MAKER_TAKER and live_maker_orders >= cfg.maker_max_live_orders:
            self.rejects["maker_limit"] += 1
            return None

        tradeable = [
            v for v in self.venue_names
            if self._venue_tradeable(v, books[v], states[v], now)
        ]
        if len(tradeable) < 2:
            return None

        best: ArbSignal | None = None
        for buy_venue in tradeable:
            for sell_venue in tradeable:
                if buy_venue == sell_venue:
                    continue
                sig = self._evaluate_direction(
                    buy_venue, sell_venue, books, states, inventory, now, pending,
                )
                if sig is not None and (best is None or sig.net_edge_bps > best.net_edge_bps):
                    best = sig

        if best is not None:
            self.signals += 1
            self._last_signal_ts = now
        return best

    def _evaluate_direction(
        self,
        buy_venue: str,
        sell_venue: str,
        books: dict[str, OrderBook],
        states: dict[str, VenueState],
        inventory: InventoryBook,
        now: float,
        pending: "PendingExposure | None" = None,
    ) -> ArbSignal | None:
        cfg = self.config
        buy_book, sell_book = books[buy_venue], books[sell_venue]
        buy_fees, sell_fees = self.venues[buy_venue].fees, self.venues[sell_venue].fees

        # Cross-venue temporal coherence: both books must have been updated
        # within max_cross_venue_skew_s of each other.  Without this gate a
        # stale Binance book (T-490ms) is compared against a fresh Bybit book
        # (T), giving a spread that was never simultaneously actionable.
        if cfg.max_cross_venue_skew_s > 0.0:
            skew = abs(buy_book.last_update_ts - sell_book.last_update_ts)
            if skew > cfg.max_cross_venue_skew_s:
                self.rejects["cross_venue_skew"] += 1
                return None

        if cfg.mode == MAKER_TAKER:
            return self._evaluate_maker_taker(
                buy_venue, sell_venue, books, states, inventory, now, pending,
            )

        buy_px = buy_book.best_ask      # we lift the offer
        sell_px = sell_book.best_bid    # we hit the bid
        if buy_px <= 0 or sell_px <= 0:
            return None

        gross_bps = (sell_px - buy_px) / buy_px * 10_000.0
        fee_bps = buy_fees.taker_bps + sell_fees.taker_bps
        net_bps = gross_bps - fee_bps - cfg.safety_buffer_bps
        self.best_edge_seen_bps = max(self.best_edge_seen_bps, net_bps)

        if net_bps < cfg.min_net_edge_bps:
            self.rejects["no_edge"] += 1
            return None

        # Only after the edge check, so this counts phantom-touch edges
        # specifically rather than every quiet moment.
        if (buy_book.touch_age_s("ask", now) > cfg.max_touch_age_s
                or sell_book.touch_age_s("bid", now) > cfg.max_touch_age_s):
            self.rejects["stale_touch"] += 1
            return None

        buy_depth = self._tradeable_depth(buy_book, "ask", buy_px)
        sell_depth = self._tradeable_depth(sell_book, "bid", sell_px)
        depth_cap = min(buy_depth, sell_depth)
        if depth_cap <= 0:
            self.rejects["no_depth"] += 1
            return None

        inv_cap = self._inventory_room(buy_venue, sell_venue, inventory, pending)
        if inv_cap <= 0:
            self.rejects["inventory"] += 1
            return None

        clip_cap = self._max_clip(buy_px)
        qty = min(depth_cap, inv_cap, clip_cap)
        if qty * buy_px < cfg.min_clip_notional:
            self.rejects["clip_too_small"] += 1
            return None

        # Limit prices bound how far the book may move during flight before we
        # decline the fill. Without them the sim would fill at any price and
        # report slippage as profit.
        slack_buy = buy_px * cfg.max_walk_bps / 10_000.0
        slack_sell = sell_px * cfg.max_walk_bps / 10_000.0

        return ArbSignal(
            signal_id=next(self._ids), ts=now,
            buy_venue=buy_venue, sell_venue=sell_venue, qty=qty,
            gross_edge_bps=gross_bps, fee_bps=fee_bps, net_edge_bps=net_bps,
            mode=TAKER_TAKER, buy_price=buy_px, sell_price=sell_px,
            depth_limited=qty >= depth_cap - 1e-12,
            inventory_limited=qty >= inv_cap - 1e-12,
            legs=[
                ArbLeg(buy_venue, "buy", qty, buy_px + slack_buy, True, buy_px, buy_fees.taker_bps),
                ArbLeg(sell_venue, "sell", qty, sell_px - slack_sell, True, sell_px, sell_fees.taker_bps),
            ],
        )

    def _evaluate_maker_taker(
        self,
        maker_venue: str,
        taker_venue: str,
        books: dict[str, OrderBook],
        states: dict[str, VenueState],
        inventory: InventoryBook,
        now: float,
        pending: "PendingExposure | None" = None,
    ) -> ArbSignal | None:
        """Rest a passive bid on maker_venue, hedge by selling on taker_venue."""
        cfg = self.config
        maker_book, taker_book = books[maker_venue], books[taker_venue]
        maker_fees, taker_fees = self.venues[maker_venue].fees, self.venues[taker_venue].fees

        hedge_px = taker_book.best_bid
        if hedge_px <= 0 or not maker_book.ready:
            return None

        fee_bps = maker_fees.maker_bps + taker_fees.taker_bps
        required_bps = fee_bps + cfg.min_net_edge_bps + cfg.safety_buffer_bps
        max_bid = hedge_px * (1.0 - required_bps / 10_000.0)

        # Must be a real improvement on the maker venue's book, and never cross it.
        touch_bid, touch_ask = maker_book.best_bid, maker_book.best_ask
        if max_bid <= touch_bid or max_bid >= touch_ask:
            self.rejects["no_edge"] += 1
            return None

        gross_bps = (hedge_px - max_bid) / max_bid * 10_000.0
        net_bps = gross_bps - fee_bps - cfg.safety_buffer_bps
        self.best_edge_seen_bps = max(self.best_edge_seen_bps, net_bps)
        if net_bps < cfg.min_net_edge_bps:
            self.rejects["no_edge"] += 1
            return None

        if taker_book.touch_age_s("bid", now) > cfg.max_touch_age_s:
            self.rejects["stale_touch"] += 1
            return None

        hedge_depth = self._tradeable_depth(taker_book, "bid", hedge_px)
        if hedge_depth <= 0:
            self.rejects["no_depth"] += 1
            return None

        inv_cap = self._inventory_room(maker_venue, taker_venue, inventory, pending)
        if inv_cap <= 0:
            self.rejects["inventory"] += 1
            return None

        qty = min(hedge_depth, inv_cap, self._max_clip(max_bid))
        if qty * max_bid < cfg.min_clip_notional:
            self.rejects["clip_too_small"] += 1
            return None

        # Fill-probability gate (Phase 1) + adverse-selection gate (Stage 4) ──
        # queue_ahead=0: order not yet placed; biases toward over-estimating
        # fill probability (conservative — gates fewer signals than reality).
        # relative_to_touch_ticks=0: tick_size unavailable at gate time; actual
        # value at placement is -maker_improve_ticks (typically -1), so the gate
        # slightly over-estimates fill probability for improve-by-1 quotes.
        if (cfg.min_fill_prob > 0.0
                and self.fill_model_v1 is not None
                and self.fill_model_v1.horizon_s == cfg.fill_gate_horizon_s):
            ms = states[maker_venue]
            mb = books[maker_venue]
            top_depth = mb.best_bid_size + mb.best_ask_size
            feat = dict(
                queue_ahead=0.0,
                local_depth=top_depth,
                local_depth_baseline=ms.top_depth_baseline,
                spread_pct=mb.spread_bps / 100.0,
                short_vol=ms.short_vol,
                trade_count=ms.trade_rate,
                trade_sign=ms.trade_sign,
                regime_bucket=ms.last_regime.bucket.value,
            )
            model = self.fill_model_v1
            if hasattr(model, "predict_ttf"):
                pred = model.predict_ttf(**feat)
                p_fill = pred.p_fill
                if cfg.max_adverse_prob < 1.0 and pred.p_adverse > cfg.max_adverse_prob:
                    self.rejects["high_adverse_prob"] += 1
                    return None
                if cfg.max_ttf_s > 0.0 and pred.e_ttf > cfg.max_ttf_s:
                    self.rejects["slow_ttf"] += 1
                    return None
            else:
                p_fill = model.predict(**feat)
            if p_fill < cfg.min_fill_prob:
                self.rejects["low_fill_prob"] += 1
                return None

        slack = hedge_px * cfg.max_walk_bps / 10_000.0
        return ArbSignal(
            signal_id=next(self._ids), ts=now,
            buy_venue=maker_venue, sell_venue=taker_venue, qty=qty,
            gross_edge_bps=gross_bps, fee_bps=fee_bps, net_edge_bps=net_bps,
            mode=MAKER_TAKER, buy_price=max_bid, sell_price=hedge_px,
            depth_limited=qty >= hedge_depth - 1e-12,
            inventory_limited=qty >= inv_cap - 1e-12,
            legs=[
                # Passive leg only. The hedge is fired on fill, not now — firing
                # it up front would be a naked short against a maybe-fill.
                ArbLeg(maker_venue, "buy", qty, max_bid, False, max_bid, maker_fees.maker_bps),
            ],
        )

    def hedge_leg(self, signal: ArbSignal, filled_qty: float, book: OrderBook) -> ArbLeg | None:
        """Build the aggressive hedge after a passive leg fills."""
        if filled_qty <= 0 or not book.ready:
            return None
        px = book.best_bid
        if px <= 0:
            return None
        slack = px * max(self.config.max_walk_bps, 5.0) / 10_000.0
        return ArbLeg(
            venue=signal.sell_venue, side="sell", qty=filled_qty,
            limit_price=px - slack, is_taker=True, expected_price=signal.sell_price,
            fee_bps=self.venues[signal.sell_venue].fees.taker_bps,
        )

    def report(self) -> dict[str, float]:
        total_rejects = sum(self.rejects.values())
        return {
            "evaluations": float(self.evaluations),
            "signals": float(self.signals),
            "signal_rate": self.signals / max(1, self.evaluations),
            "best_net_edge_bps": self.best_edge_seen_bps if self.best_edge_seen_bps > float("-inf") else 0.0,
            "rejects_total": float(total_rejects),
            **{f"reject_{k}": float(v) for k, v in self.rejects.items()},
        }
