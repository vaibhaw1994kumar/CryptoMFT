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
from typing import Any

from ..simulation.models import Side
from ..simulation.order_book import OrderBook
from ..simulation.venue_state import VenueState
from . import edge as edge_mod


TAKER_TAKER = "taker_taker"
MAKER_TAKER = "maker_taker"
PROFILES = ("legacy_v0", "perp_perp_maker_taker_v1")
DISPATCH_MODES = ("fill_triggered", "simultaneous")


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
    max_position_base: float = 1.0       # |net base across venues|; used as clip-size and as the denominator for the 2x net ceiling
    max_venue_imbalance_base: float = 0.0  # 0 => 2x max_position_base
    # Opt-in hard ceiling on |net base| across all venues (Task 2b).
    # None (default) keeps the legacy 2× behaviour (net_room = 2× max_position_base).
    # Set to max_position_base for a 1× hard ceiling; set to 2× for the legacy equivalent but explicit.
    max_net_base_hard: float | None = None
    # Opt-in allowance added to max_net_base_hard (or the net_ceiling_multiple ceiling) to give room for a pending fill + pending
    # hedge that have not yet settled. It is a SEPARATE parameter from the ceiling, but what it buys is room: the cap used by
    # _inventory_room (net, and per venue unless max_venue_imbalance_base is set) is ceiling + headroom, so the effective sizing
    # limit is e.g. 1.25 x max_position_base for ceiling 1.0 and headroom 0.25 x max_position_base. Headroom 0 = the bare ceiling.
    fill_headroom_base: float = 0.0
    # False (default, legacy): position limits look at filled inventory only. True: they also assume the worst case for
    # orders that are not in the inventory yet (resting maker remainders, maker legs in flight, hedges in flight),
    # with no netting between them. With maker_max_live_orders > 1 the legacy check can be breached by orders that
    # all fill; see PendingExposure.
    count_pending_exposure: bool = False
    # WP6 — reachable-exposure limits. net_ceiling_multiple: the hard net ceiling is this x max_position_base when max_net_base_hard
    # is None (legacy 2.0; the perp profile asks for 1.0 — DECISION_PENDING: 1x vs fill headroom). Gross limits (None = off): the
    # worst-case long / short base summed over venues, pending orders included when count_pending_exposure is on.
    net_ceiling_multiple: float = 2.0
    max_gross_long_base: float | None = None
    max_gross_short_base: float | None = None
    # Opt-in (Task 2c): when True, orders the strategy has written off (ghost/UNKNOWN) are still counted as potential
    # exposure until the exchange confirms they are cancelled or filled. Requires count_pending_exposure=True.
    # Prevents new positions when an UNKNOWN order is unresolved; needed to stop the ghost scenario exceeding max_net_base_hard.
    count_unknown_exposure: bool = False

    # Opt-in (Task 5): when set to a venue name, all open inventory is marked at that venue's mid rather than each
    # venue's own mid. Eliminates stale-mid distortion when the two venues' books last updated at different times.
    # None (default) preserves the legacy per-venue marking. Typical value: the venue with the more reliable feed.
    mark_inventory_venue: str | None = None
    # Opt-in (Task 5): when common_mid_staleness_s exceeds this threshold the
    # common_timestamp_equity is unreliable (one venue's book is too far behind) and
    # the result falls back to ending_equity with common_mid_staleness_s=inf.
    # Default 1.0 s is tight enough to flag typical feed-lag spikes; set to inf to
    # disable the threshold (report staleness but never reject the mark).
    max_common_staleness_s: float = 1.0

    # Maker-taker
    maker_max_live_orders: int = 1
    maker_max_age_s: float = 0.0         # seconds from exchange arrival until a cancel is requested; 0 = off (rest until filled); negative/NaN raise
    maker_count_inflight: bool = True    # orders still in flight count toward maker_max_live_orders (False = legacy stacking)
    # Which passive direction(s) a maker venue may quote. "bid" (default, the behaviour so far): rest a buy, hedge by selling on
    # the taker venue. "both": also rest a sell (an improved ask) hedged by BUYING on the taker venue; per evaluation the
    # direction with the higher net edge wins. On a perpetual both directions are equally available.
    maker_passive_side: str = "bid"      # "bid" (legacy: passive buy only) | "ask" (passive sell only) | "both" (better net edge wins)

    # ── Strategy profile (docs/strategy_contract.md) ─────────────────────────
    # "legacy_v0" (default): the edge expressions and gate used by every result before 2026-11-01, kept bit-identical.
    # "perp_perp_maker_taker_v1": one edge function (edge.py) everywhere — taker/taker edge is depth-weighted (VWAP over the
    # levels the size consumes, reference = buy venue mid), and the maker/taker gate uses the CONDITIONAL expected edge
    # (expected hedge price given a fill, hedge slippage, funding/holding, p_fail x loss_fail) instead of the current quote.
    profile: str = "legacy_v0"
    # How the hedge is dispatched in MAKER_TAKER: "fill_triggered" (canonical: the hedge is sent when the maker fill is
    # confirmed, for the filled quantity) or "simultaneous" (the hedge is sent with the maker order; if the maker never fills
    # the hedge is an orphan position, reported as such). Both profiles default to fill_triggered.
    dispatch_mode: str = "fill_triggered"
    # Conditional-edge parameters (perp_perp_maker_taker_v1). Hedge-move: expected move of the hedge venue's executable price
    # between quote time and maker-fill time, conditional on a fill, in bps of the hedge price, signed so that NEGATIVE is
    # adverse for the hedge (a lower bid for a sell hedge, a higher ask for a buy hedge). First estimates come from the replay's
    # own legging_report (hedge_move_bps by side); default 0 = no information yet.
    expected_hedge_move_bps_bid: float = 0.0     # passive bid -> sell hedge
    expected_hedge_move_bps_ask: float = 0.0     # passive ask -> buy hedge
    expected_hedge_slippage_bps: float = 0.0     # hedge VWAP beyond the touch for the clip size
    funding_holding_bps: float = 0.0             # expected funding / holding cost of the hedged pair until it is closed
    # PLACEHOLDER — NOT MEASURED. p_fail = 0.07 is the hedge-failure study's break-even figure, an assumed value. A replay with no
    # injected failures cannot measure a production hedge-failure rate: the zero failures seen in the Jan-2022 replays are only the
    # simulator's own depth misses and say nothing about rejects, outages or disconnects. Sensitivity (0.02 / 0.05 / 0.07 / 0.10)
    # is in perp_mechanics_report.html; calibrate from live or testnet order data before relying on any value.
    p_fail: float = 0.07                         # hedge-failure probability — PLACEHOLDER
    loss_fail_bps: float = 7.8                   # loss per hedge failure in bps of notional (hedge-failure study ~-7.8 bps)

    # Pacing
    min_resignal_gap_s: float = 0.0      # throttle repeat fires on one edge

    # Phase 1 fill-probability gate (MAKER_TAKER only)
    # Set min_fill_prob > 0 to skip quotes where the fitted model predicts a low
    # fill probability.  fill_gate_horizon_s selects which trained horizon to use.
    min_fill_prob: float = 0.0           # 0 = gate disabled
    fill_gate_horizon_s: int = 15
    # WP8: the fill gate's queue feature. Legacy 0.0 = "queue_ahead=0" (the order is scored as if it were at the front, which
    # over-estimates the fill probability and gates too little). > 0: the PRE-PLACEMENT expected queue = the displayed size at the
    # quote price when the level exists, else this floor (the same proxy the fill engine uses for an absent level).
    fill_gate_queue_floor: float = 0.0
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
    # v1.1 step 9, opt-in (defaults keep every result unchanged). round_quotes_to_tick: a maker quote is rounded onto the venue's price grid BEFORE the
    # improve-the-touch check and the edge calculation (bids down, asks up), so the edge, the gate and the order that is sent are the same price.
    # venue_ticks = the declared tick per venue ({"bybit": 0.001, ...}), supplied by the runner from the dataset; there is no fallback (a missing tick is an
    # error). A quote that no longer improves the touch once rounded is refused (reject "tick_rounding_no_improve"), it does not join the queue.
    round_quotes_to_tick: bool = False
    venue_ticks: dict = field(default_factory=dict)
    # v1.2 (owner decision B0, 5 Nov), opt-in, needs round_quotes_to_tick. A quote that was inside the spread before rounding and lands ON the touch once rounded joins the back of that
    # level's queue instead of being refused ("tick_rounding_no_improve"), provided the edge recomputed at the rounded price still passes (else reject "tick_rounding_join_no_edge").
    # Counted as "tick_rounding_joined_touch" in the rejects dict (a count of signals, not a reject). A quote rounded BELOW the touch is still refused. Both sides.
    join_touch_on_tick_rounding: bool = False
    # maker_venues: the venues a passive quote may rest on (empty = either, as before). The hedge goes to the other venue.
    maker_venues: tuple = ()

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
    # Logged on every signal (perp_perp_maker_taker_v1): the edge if the current opposite quote were locked, and the conditional
    # expected edge the gate used; edge_ref names the reference price of the bps figures.
    naive_edge_bps: float | None = None
    conditional_edge_bps: float | None = None
    edge_ref: str = "buy_ask"

    # MAKER_TAKER: the passive leg is legs[0]; the hedge is a taker on the other venue, on the opposite side. For a passive
    # bid (the only direction before maker_passive_side) these equal buy_venue / sell_venue / "sell".
    @property
    def maker_venue(self) -> str:
        return self.legs[0].venue if self.legs else self.buy_venue

    @property
    def hedge_venue(self) -> str:
        return self.sell_venue if self.maker_venue == self.buy_venue else self.buy_venue

    @property
    def hedge_side(self) -> str:
        return "sell" if self.maker_venue == self.buy_venue else "buy"


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
        venues: dict[str, Any],      # name -> object with .fees (maker_bps, taker_bps): new_backtest.config.VenueSpec (Phase D move: a typing change, no behaviour)
        config: ArbConfig | None = None,
        fill_model_v1: Any = None,      # new_backtest.fill_model_v1.FillModelV1 (research code stays in new_backtest; the core must not import it)
    ) -> None:
        self.pair = pair
        self.venues = venues
        self.config = config or ArbConfig()
        if self.config.maker_passive_side not in ("bid", "ask", "both"):
            raise ValueError(f"ArbConfig.maker_passive_side must be 'bid', 'ask' or 'both', got {self.config.maker_passive_side!r}")
        if self.config.join_touch_on_tick_rounding and not self.config.round_quotes_to_tick:
            raise ValueError("ArbConfig.join_touch_on_tick_rounding needs round_quotes_to_tick")
        if self.config.round_quotes_to_tick:
            missing = [v for v in self.venue_names_cfg(venues) if not (self.config.venue_ticks.get(v, 0.0) > 0.0)]
            if missing:
                raise ValueError(f"ArbConfig.round_quotes_to_tick needs a declared venue_ticks[venue] > 0 for every venue; missing {missing}")
        unknown = [v for v in self.config.maker_venues if v not in venues]
        if unknown:
            raise ValueError(f"ArbConfig.maker_venues {unknown} are not among the venues {sorted(venues)}")
        if self.config.profile not in PROFILES:
            raise ValueError(f"ArbConfig.profile must be one of {PROFILES}, got {self.config.profile!r}")
        if self.config.dispatch_mode not in DISPATCH_MODES:
            raise ValueError(f"ArbConfig.dispatch_mode must be one of {DISPATCH_MODES}, got {self.config.dispatch_mode!r}")
        if not (0.0 <= self.config.p_fail <= 1.0) or self.config.loss_fail_bps < 0.0:
            raise ValueError("ArbConfig.p_fail must be in [0, 1] and loss_fail_bps >= 0")
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

    @staticmethod
    def venue_names_cfg(venues) -> list[str]:
        return sorted(venues)

    def _quote_limit(self, side: str, hedge_px: float, fee_bps: float) -> float:
        """The most a passive bid may rest at / the least a passive ask may rest at, from the hedge-venue price `hedge_px` (the one place this is computed:
        first placement and repricing both call it).
        legacy_v0: the naive edge. Other profiles: the quote is built from the EXPECTED hedge price (touch moved by the conditional hedge-move estimate) less
        fees, edge, buffer, expected slippage, funding/holding and p_fail x loss_fail. The required edge is measured against the price we PAY (the hedge)."""
        cfg = self.config
        required_bps = fee_bps + cfg.min_net_edge_bps + cfg.safety_buffer_bps
        if side == "bid":
            px = hedge_px * (1.0 - required_bps / 10_000.0)
            if cfg.profile != "legacy_v0":
                extra_bps = cfg.expected_hedge_slippage_bps + cfg.funding_holding_bps + cfg.p_fail * cfg.loss_fail_bps
                expected_hedge = hedge_px * (1.0 + cfg.expected_hedge_move_bps_bid / 10_000.0)
                px = expected_hedge * (1.0 - (required_bps + extra_bps) / 10_000.0)
            return px
        px = hedge_px / (1.0 - required_bps / 10_000.0)
        if cfg.profile != "legacy_v0":
            extra_bps = cfg.expected_hedge_slippage_bps + cfg.funding_holding_bps + cfg.p_fail * cfg.loss_fail_bps
            expected_hedge = hedge_px * (1.0 - cfg.expected_hedge_move_bps_ask / 10_000.0)
            px = expected_hedge / (1.0 - (required_bps + extra_bps) / 10_000.0)
        return px

    def requote_target(self, maker_venue: str, side: str, qty: float, books: dict, now: float) -> tuple[float | None, str]:
        """S0: where a RESTING passive quote should sit now, by the same edge rule as its first placement, evaluated at the CURRENT hedge-venue price (not the
        maker venue's mid). Returns (price, "ok"), or (None, why): "stale_hedge_touch" (do not act on an old touch), "no_hedge_price", "no_edge" (the edge at the
        current hedge price no longer clears the minimum, or the grid price would not rest). Pure apart from reading the books; it counts no rejects."""
        cfg = self.config
        if cfg.profile == "legacy_v0":
            raise ValueError("requote_target needs a non-legacy profile (conditional edge)")
        taker_venue = next(v for v in books if v != maker_venue)
        maker_book, taker_book = books[maker_venue], books[taker_venue]
        maker_fees, taker_fees = self.venues[maker_venue].fees, self.venues[taker_venue].fees
        bid = side == "buy"
        hedge_px = taker_book.best_bid if bid else taker_book.best_ask
        if hedge_px <= 0 or not maker_book.ready:
            return None, "no_hedge_price"
        if taker_book.touch_age_s("bid" if bid else "ask", now) > cfg.max_touch_age_s:
            return None, "stale_hedge_touch"
        fee_bps = maker_fees.maker_bps + taker_fees.taker_bps
        px = self._quote_limit("bid" if bid else "ask", hedge_px, fee_bps)
        if cfg.round_quotes_to_tick:
            px = self._quote_to_tick(maker_venue, px, "bid" if bid else "ask")
            tick = cfg.venue_ticks[maker_venue]
            touch_bid, touch_ask = maker_book.best_bid, maker_book.best_ask
            if bid and px >= touch_ask:
                px = round(touch_ask - tick, 10)               # never cross: the post-only rule would reject it
            elif not bid and px <= touch_bid:
                px = round(touch_bid + tick, 10)
        if px <= 0:
            return None, "no_edge"
        er = edge_mod.maker_taker_expected_edge_bps(
            px, "bid" if bid else "ask", taker_book, qty, maker_fees.maker_bps, taker_fees.taker_bps,
            expected_hedge_move_bps=cfg.expected_hedge_move_bps_bid if bid else cfg.expected_hedge_move_bps_ask,
            expected_hedge_slippage_bps=cfg.expected_hedge_slippage_bps, funding_holding_bps=cfg.funding_holding_bps, p_fail=cfg.p_fail, loss_fail_bps=cfg.loss_fail_bps)
        if er.net_bps - cfg.safety_buffer_bps < cfg.min_net_edge_bps:
            return None, "no_edge"
        return px, "ok"

    def _quote_to_tick(self, venue: str, price: float, side: str) -> float:
        """The quote on the venue's price grid: bids round down, asks up (1e-9 of a tick of float slack)."""
        tick = self.config.venue_ticks[venue]
        n = math.floor(price / tick + 1e-9) if side == "bid" else math.ceil(price / tick - 1e-9)
        return round(n * tick, 10)

    def _maker_allowed(self, venue: str) -> bool:
        return not self.config.maker_venues or venue in self.config.maker_venues

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
        cap = cfg.max_venue_imbalance_base or (cfg.net_ceiling_multiple * cfg.max_position_base + cfg.fill_headroom_base)
        # Hard net ceiling: opt-in (max_net_base_hard), default legacy net_ceiling_multiple (2) × max_position_base.
        # fill_headroom_base adds allowance on top of the effective net ceiling for pending settle.
        if cfg.max_net_base_hard is not None:
            net_ceil = cfg.max_net_base_hard + cfg.fill_headroom_base
        else:
            net_ceil = cfg.max_position_base * cfg.net_ceiling_multiple + cfg.fill_headroom_base
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
        net_room = max(0.0, net_ceil - max(abs(net_hi), abs(net_lo)))
        room = min(buy_room, sell_room, net_room)
        # WP6 gross limits: worst case over venues, longs and shorts separately (a clip adds to both: +qty long, +qty short).
        if cfg.max_gross_long_base is not None or cfg.max_gross_short_base is not None:
            longs = sum(max(0.0, b) for b in inv.base.values())
            shorts = sum(max(0.0, -b) for b in inv.base.values())
            if pending is not None and cfg.count_pending_exposure:
                longs += sum(pending.buys.values())
                shorts += sum(pending.sells.values())
            if cfg.max_gross_long_base is not None:
                room = min(room, max(0.0, cfg.max_gross_long_base - longs))
            if cfg.max_gross_short_base is not None:
                room = min(room, max(0.0, cfg.max_gross_short_base - shorts))
        return room

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
            if cfg.maker_passive_side == "ask":                       # passive sell on the maker venue only, hedge buys
                return self._evaluate_maker_taker_ask(buy_venue, sell_venue, books, states, inventory, now, pending)
            bid = self._evaluate_maker_taker(
                buy_venue, sell_venue, books, states, inventory, now, pending,
            )
            if cfg.maker_passive_side != "both":
                return bid
            ask = self._evaluate_maker_taker_ask(buy_venue, sell_venue, books, states, inventory, now, pending)
            if bid is None or (ask is not None and ask.net_edge_bps > bid.net_edge_bps):
                return ask
            return bid

        buy_px = buy_book.best_ask      # we lift the offer
        sell_px = sell_book.best_bid    # we hit the bid
        if buy_px <= 0 or sell_px <= 0:
            return None

        fee_bps = buy_fees.taker_bps + sell_fees.taker_bps
        naive_bps = cond_bps = None
        edge_ref = "buy_ask"
        if cfg.profile == "legacy_v0":
            gross_bps, _ = edge_mod.legacy_taker_taker_edge_bps(buy_px, sell_px, buy_fees.taker_bps, sell_fees.taker_bps)
        else:
            # perp_perp_maker_taker_v1: depth-weighted over the levels the clip consumes, reference = buy venue mid.
            probe = min(self._max_clip(buy_px), self._tradeable_depth(buy_book, "ask", buy_px), self._tradeable_depth(sell_book, "bid", sell_px))
            er = edge_mod.taker_taker_edge_bps(buy_book, sell_book, max(probe, 1e-12), buy_fees.taker_bps, sell_fees.taker_bps)
            if er.status != edge_mod.OK:
                self.rejects["no_depth" if er.status == edge_mod.INSUFFICIENT_DEPTH else "not_ready"] += 1
                return None
            gross_bps, edge_ref = er.gross_bps, er.ref_name
            naive_bps, _ = edge_mod.legacy_taker_taker_edge_bps(buy_px, sell_px, buy_fees.taker_bps, sell_fees.taker_bps)
            cond_bps = er.net_bps - cfg.safety_buffer_bps
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
            naive_edge_bps=naive_bps, conditional_edge_bps=cond_bps, edge_ref=edge_ref,
            legs=[
                ArbLeg(buy_venue, "buy", qty, buy_px + slack_buy, True, buy_px, buy_fees.taker_bps),
                ArbLeg(sell_venue, "sell", qty, sell_px - slack_sell, True, sell_px, sell_fees.taker_bps),
            ],
        )

    def _gate_queue(self, book: OrderBook, side: str, price: float) -> float:
        """WP8: pre-placement expected queue for the fill gate (0.0 = legacy front-of-queue assumption)."""
        if self.config.fill_gate_queue_floor <= 0.0:
            return 0.0
        shown = book.level_size(side, price)
        return shown if shown > 0.0 else self.config.fill_gate_queue_floor

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
        if not self._maker_allowed(maker_venue):
            return None
        maker_book, taker_book = books[maker_venue], books[taker_venue]
        maker_fees, taker_fees = self.venues[maker_venue].fees, self.venues[taker_venue].fees

        hedge_px = taker_book.best_bid
        if hedge_px <= 0 or not maker_book.ready:
            return None

        fee_bps = maker_fees.maker_bps + taker_fees.taker_bps
        max_bid = self._quote_limit("bid", hedge_px, fee_bps)

        # Must be a real improvement on the maker venue's book, and never cross it.
        touch_bid, touch_ask = maker_book.best_bid, maker_book.best_ask
        joins_touch = False
        if cfg.round_quotes_to_tick:
            was_ok = touch_bid < max_bid < touch_ask
            max_bid = self._quote_to_tick(maker_venue, max_bid, "bid")
            on_touch = abs(max_bid - touch_bid) < 1e-6 * cfg.venue_ticks[maker_venue]
            if on_touch:
                max_bid = touch_bid
            if was_ok and not touch_bid < max_bid < touch_ask:
                if cfg.join_touch_on_tick_rounding and on_touch:
                    joins_touch = True                              # rests at the back of the touch level; the edge below is computed at this price
                else:
                    self.rejects["tick_rounding_no_improve"] = self.rejects.get("tick_rounding_no_improve", 0) + 1
                    return None
        if not joins_touch and (max_bid <= touch_bid or max_bid >= touch_ask):
            self.rejects["no_edge"] += 1
            return None

        gross_bps, _ = edge_mod.legacy_maker_bid_edge_bps(hedge_px, max_bid, fee_bps)
        net_bps = gross_bps - fee_bps - cfg.safety_buffer_bps
        naive_bps = cond_bps = None
        edge_ref = "maker_price"
        if cfg.profile != "legacy_v0":
            # perp_perp_maker_taker_v1: the gate uses the conditional expected edge; the naive one is logged beside it.
            er = edge_mod.maker_taker_expected_edge_bps(
                max_bid, "bid", taker_book, self._max_clip(max_bid), maker_fees.maker_bps, taker_fees.taker_bps,
                expected_hedge_move_bps=cfg.expected_hedge_move_bps_bid, expected_hedge_slippage_bps=cfg.expected_hedge_slippage_bps,
                funding_holding_bps=cfg.funding_holding_bps, p_fail=cfg.p_fail, loss_fail_bps=cfg.loss_fail_bps)
            naive_bps, cond_bps, edge_ref = er.naive_bps, er.net_bps - cfg.safety_buffer_bps, er.ref_name
            net_bps = cond_bps
        self.best_edge_seen_bps = max(self.best_edge_seen_bps, net_bps)
        if net_bps < cfg.min_net_edge_bps:
            if joins_touch:
                self.rejects["tick_rounding_join_no_edge"] = self.rejects.get("tick_rounding_join_no_edge", 0) + 1
            else:
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
        # value at placement is -1 (one tick inside the touch), so the gate
        # slightly over-estimates fill probability for improve-by-1 quotes.
        if (cfg.min_fill_prob > 0.0
                and self.fill_model_v1 is not None
                and self.fill_model_v1.horizon_s == cfg.fill_gate_horizon_s):
            ms = states[maker_venue]
            mb = books[maker_venue]
            top_depth = mb.best_bid_size + mb.best_ask_size
            feat = dict(
                queue_ahead=self._gate_queue(mb, "bid", max_bid),
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

        if joins_touch:
            self.rejects["tick_rounding_joined_touch"] = self.rejects.get("tick_rounding_joined_touch", 0) + 1
        slack = hedge_px * cfg.max_walk_bps / 10_000.0
        return ArbSignal(
            signal_id=next(self._ids), ts=now,
            buy_venue=maker_venue, sell_venue=taker_venue, qty=qty,
            gross_edge_bps=gross_bps, fee_bps=fee_bps, net_edge_bps=net_bps,
            mode=MAKER_TAKER, buy_price=max_bid, sell_price=hedge_px,
            depth_limited=qty >= hedge_depth - 1e-12,
            inventory_limited=qty >= inv_cap - 1e-12,
            naive_edge_bps=naive_bps, conditional_edge_bps=cond_bps, edge_ref=edge_ref,
            legs=[
                # Passive leg only. The hedge is fired on fill, not now — firing
                # it up front would be a naked short against a maybe-fill.
                ArbLeg(maker_venue, "buy", qty, max_bid, False, max_bid, maker_fees.maker_bps),
            ],
        )

    def _evaluate_maker_taker_ask(
        self,
        maker_venue: str,
        taker_venue: str,
        books: dict[str, OrderBook],
        states: dict[str, VenueState],
        inventory: InventoryBook,
        now: float,
        pending: "PendingExposure | None" = None,
    ) -> ArbSignal | None:
        """Rest a passive ask on maker_venue, hedge by buying on taker_venue (mirror of _evaluate_maker_taker)."""
        cfg = self.config
        if not self._maker_allowed(maker_venue):
            return None
        maker_book, taker_book = books[maker_venue], books[taker_venue]
        maker_fees, taker_fees = self.venues[maker_venue].fees, self.venues[taker_venue].fees

        hedge_px = taker_book.best_ask
        if hedge_px <= 0 or not maker_book.ready:
            return None

        fee_bps = maker_fees.maker_bps + taker_fees.taker_bps
        min_ask = self._quote_limit("ask", hedge_px, fee_bps)

        touch_bid, touch_ask = maker_book.best_bid, maker_book.best_ask
        joins_touch = False
        if cfg.round_quotes_to_tick:
            was_ok = touch_bid < min_ask < touch_ask
            min_ask = self._quote_to_tick(maker_venue, min_ask, "ask")
            on_touch = abs(min_ask - touch_ask) < 1e-6 * cfg.venue_ticks[maker_venue]
            if on_touch:
                min_ask = touch_ask
            if was_ok and not touch_bid < min_ask < touch_ask:
                if cfg.join_touch_on_tick_rounding and on_touch:
                    joins_touch = True
                else:
                    self.rejects["tick_rounding_no_improve"] = self.rejects.get("tick_rounding_no_improve", 0) + 1
                    return None
        if not joins_touch and (min_ask >= touch_ask or min_ask <= touch_bid):
            self.rejects["no_edge"] += 1
            return None

        gross_bps, _ = edge_mod.legacy_maker_ask_edge_bps(min_ask, hedge_px, fee_bps)
        net_bps = gross_bps - fee_bps - cfg.safety_buffer_bps
        naive_bps = cond_bps = None
        edge_ref = "hedge_price"
        if cfg.profile != "legacy_v0":
            er = edge_mod.maker_taker_expected_edge_bps(
                min_ask, "ask", taker_book, self._max_clip(min_ask), maker_fees.maker_bps, taker_fees.taker_bps,
                expected_hedge_move_bps=cfg.expected_hedge_move_bps_ask, expected_hedge_slippage_bps=cfg.expected_hedge_slippage_bps,
                funding_holding_bps=cfg.funding_holding_bps, p_fail=cfg.p_fail, loss_fail_bps=cfg.loss_fail_bps)
            naive_bps, cond_bps, edge_ref = er.naive_bps, er.net_bps - cfg.safety_buffer_bps, er.ref_name
            net_bps = cond_bps
        self.best_edge_seen_bps = max(self.best_edge_seen_bps, net_bps)
        if net_bps < cfg.min_net_edge_bps:
            if joins_touch:
                self.rejects["tick_rounding_join_no_edge"] = self.rejects.get("tick_rounding_join_no_edge", 0) + 1
            else:
                self.rejects["no_edge"] += 1
            return None

        if taker_book.touch_age_s("ask", now) > cfg.max_touch_age_s:
            self.rejects["stale_touch"] += 1
            return None

        hedge_depth = self._tradeable_depth(taker_book, "ask", hedge_px)
        if hedge_depth <= 0:
            self.rejects["no_depth"] += 1
            return None

        inv_cap = self._inventory_room(taker_venue, maker_venue, inventory, pending)   # we buy on the taker venue, sell on the maker venue
        if inv_cap <= 0:
            self.rejects["inventory"] += 1
            return None

        qty = min(hedge_depth, inv_cap, self._max_clip(min_ask))
        if qty * min_ask < cfg.min_clip_notional:
            self.rejects["clip_too_small"] += 1
            return None

        if (cfg.min_fill_prob > 0.0
                and self.fill_model_v1 is not None
                and self.fill_model_v1.horizon_s == cfg.fill_gate_horizon_s):
            ms = states[maker_venue]
            mb = books[maker_venue]
            feat = dict(
                queue_ahead=self._gate_queue(mb, "ask", min_ask), local_depth=mb.best_bid_size + mb.best_ask_size, local_depth_baseline=ms.top_depth_baseline,
                spread_pct=mb.spread_bps / 100.0, short_vol=ms.short_vol, trade_count=ms.trade_rate, trade_sign=ms.trade_sign,
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

        if joins_touch:
            self.rejects["tick_rounding_joined_touch"] = self.rejects.get("tick_rounding_joined_touch", 0) + 1
        return ArbSignal(
            signal_id=next(self._ids), ts=now,
            buy_venue=taker_venue, sell_venue=maker_venue, qty=qty,
            gross_edge_bps=gross_bps, fee_bps=fee_bps, net_edge_bps=net_bps,
            mode=MAKER_TAKER, buy_price=hedge_px, sell_price=min_ask,
            depth_limited=qty >= hedge_depth - 1e-12,
            inventory_limited=qty >= inv_cap - 1e-12,
            naive_edge_bps=naive_bps, conditional_edge_bps=cond_bps, edge_ref=edge_ref,
            legs=[
                # Passive leg only; the buy hedge is fired on fill (see _evaluate_maker_taker).
                ArbLeg(maker_venue, "sell", qty, min_ask, False, min_ask, maker_fees.maker_bps),
            ],
        )

    def hedge_leg(self, signal: ArbSignal, filled_qty: float, book: OrderBook) -> ArbLeg | None:
        """Build the aggressive hedge after a passive leg fills: a sell after a passive bid, a buy after a passive ask."""
        if filled_qty <= 0 or not book.ready:
            return None
        if signal.hedge_side == "buy":
            px = book.best_ask
            if px <= 0:
                return None
            slack = px * max(self.config.max_walk_bps, 5.0) / 10_000.0
            return ArbLeg(
                venue=signal.hedge_venue, side="buy", qty=filled_qty,
                limit_price=px + slack, is_taker=True, expected_price=signal.buy_price,
                fee_bps=self.venues[signal.hedge_venue].fees.taker_bps,
            )
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
