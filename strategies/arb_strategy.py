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
from decimal import Decimal
from typing import Any

from ..models import Opportunity, OrderState, stable_id
from ..simulation.models import Side
from ..simulation.order_book import OrderBook, float_mirror
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
    # Quote validity (owner 12 Nov, EMS note 09 Nov section 4; OFF by default, set by profile v1_8): on every book update a RESTING maker quote's net edge is recomputed against the current hedge-venue
    # executable price for the quote's size (VWAP over the depth needed); below the entry threshold minus the hysteresis it is withdrawn (cancelled). Withdrawal only: no repricing (S0 stays off).
    quote_withdraw: bool = False
    quote_withdraw_hysteresis_bps: float = 0.5   # ASSUMED
    # Entry hedge price (owner 13 Nov, edge calibration item 1; default "touch" = every profile up to v1_8 unchanged, "vwap" set by v1_9): "vwap" prices the hedge at the VWAP the hedge venue's book gives for the
    # size the quote will really have (the same walk quote_validity uses, one helper), in the quote price AND the entry edge, so entry and quote_validity give the same net edge for the same book, price and size.
    # expected_hedge_slippage_bps then stays 0 by design: the depth cost is already in the price.
    entry_hedge_price: str = "touch"

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
    # S1 (owner 9 Nov): an `edge.AdverseTable` (fitted on market-data labels only) replaces the two numbers above. None = off: the numbers above are used, as in every profile before v1_6. When set it needs a declared
    # tick for the maker venue (the table's depth buckets are in ticks). The object comes from the profile's pinned file (new_backtest.profiles); it is not a profile key and is not part of any dump.
    adverse_table: Any = None
    do_not_quote: Any = None            # S3 (owner 10 Nov): a `DoNotQuoteFilter` (market-data-fitted, pinned by the profile); None = off, same arithmetic as before. Applied at placement, on features read now (the study scored 8 ms before the sweep)
    adverse_horizon_s: float = 1.0               # the table's horizon read as the hedge move in the quote price and the entry rule: the one closest to the hedge arrival delay (v1_6: 0.05 s)
    adverse_requote_horizon_s: float | None = None   # the horizon the repricing rule (S0 `requote_target`) reads; None = the same as adverse_horizon_s (v1_6: 1 s, the markout horizon)
    expected_hedge_slippage_bps: float = 0.0     # hedge VWAP beyond the touch for the clip size
    funding_holding_bps: float = 0.0             # expected funding / holding cost of the hedged pair until it is closed (a constant; the live term below is added to it)
    # Expected funding cost for the expected holding time (owner 13 Nov, edge calibration item 3). OFF by default (0 = the term is exactly the constant above, bit-identical). With funding_expected_holding_s > 0 the term is
    # holding_s x (funding rate per second of the venue we are LONG on - that of the venue we are SHORT on) x 1e4 bps, from the latest predicted rates the derivative stream gave (`observe_funding_rate`); a long pays a positive rate.
    # funding_credit: "floor_zero" (ASSUMED, conservative: a credit never raises the edge) | "allow" (a credit lowers the cost). A missing or stale rate adds 0 and is COUNTED (funding_term_unavailable).
    # The holding time is an owner value (no basis in the data yet: the strategy has no exit for an open pair), so there is no default. The stop-quoting window before a settlement is a separate rule and is NOT BUILT.
    funding_expected_holding_s: float = 0.0
    funding_credit: str = "floor_zero"
    funding_rate_max_age_s: float = 600.0        # ASSUMED: a predicted rate older than this is not used
    # Funding stop-quoting window (owner 14 Nov): no NEW quote within funding_window_s before a funding timestamp on EITHER venue (the next funding time from the derivative stream); resting quotes are withdrawn when the
    # window starts; quoting resumes once the settlement has passed and the stream shows the next one (funding booked), or funding_window_after_s after the timestamp (ASSUMED 30 s) if it does not. 0 = off (bit-identical).
    # With no next-funding time for a venue the window is NOT EVALUABLE: counted (reject funding_window_not_evaluable), never skipped silently, and nothing is blocked.
    funding_window_s: float = 0.0
    funding_window_after_s: float = 30.0
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
        if self.entry_hedge_price not in ("touch", "vwap"):
            raise ValueError(f"arb.entry_hedge_price must be 'touch' or 'vwap', got {self.entry_hedge_price!r}")
        if self.funding_credit not in ("floor_zero", "allow"):
            raise ValueError(f"arb.funding_credit must be 'floor_zero' or 'allow', got {self.funding_credit!r}")
        if self.funding_window_s < 0 or self.funding_window_after_s < 0:
            raise ValueError("arb.funding_window_s and funding_window_after_s must be >= 0")
        if self.funding_expected_holding_s < 0 or self.funding_rate_max_age_s <= 0:
            raise ValueError("arb.funding_expected_holding_s must be >= 0 and funding_rate_max_age_s > 0")
        if self.entry_hedge_price == "vwap" and self.profile == "legacy_v0":
            raise ValueError("arb.entry_hedge_price 'vwap' needs a non-legacy profile (legacy_v0 stays byte-identical)")


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
        if self.config.adverse_table is not None:
            missing = [v for v in self.venue_names_cfg(venues) if not (self.config.venue_ticks.get(v, 0.0) > 0.0)]
            if missing:
                raise ValueError(f"ArbConfig.adverse_table needs a declared venue_ticks[venue] > 0 (its depth buckets are in ticks); missing {missing}")
            for name, h in (("adverse_horizon_s", self.config.adverse_horizon_s), ("adverse_requote_horizon_s", self.config.adverse_requote_horizon_s)):
                if h is not None and f"{h:g}" not in {f"{x:g}" for x in self.config.adverse_table.horizons_s}:
                    raise ValueError(f"ArbConfig.{name} {h} is not a horizon of the table {self.config.adverse_table.horizons_s}")
        self._adverse_features = edge_mod.AdverseFeatures() if self.config.adverse_table is not None else None
        self._quote_features = edge_mod.QuoteFeatures() if self.config.do_not_quote is not None else None
        self._adverse_states: dict | None = None
        self._adverse_now = 0.0
        self._ids = itertools.count(1)
        self._last_signal_ts = 0.0
        self.fill_model_v1 = fill_model_v1

        self.clip_binding: dict[str, int] = {"depth": 0, "inventory": 0, "clip": 0}
        self._next_funding: dict[str, float] = {}                                   # venue -> the next funding timestamp (seconds) the derivative stream last gave
        self._funding_rates: dict[str, tuple[float, float, float]] = {}       # venue -> (predicted rate per interval, interval seconds, as-of time); fed by observe_funding_rate
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

    def _observe_adverse_features(self, books: dict, states: dict, now: float) -> None:
        """S1: keep the quote-time features current (one hedge-mid sample per whole second per venue; the maker venue's print state for the flow). Called first in `evaluate`; a no-op without a table."""
        if self._adverse_features is None:
            return
        self._adverse_states, self._adverse_now = states, now
        for v, b in books.items():
            if b.ready:
                self._adverse_features.on_mid(v, now, b.mid)

    def _observe_quote_features(self, books: dict, now: float) -> None:
        """S3: keep the touch-change clock and the hedge-mid samples of the do-not-quote filter current. A no-op without a filter."""
        if self._quote_features is None:
            return
        for v, b in books.items():
            if b.ready:
                if v in self.config.maker_venues or not self.config.maker_venues:
                    self._quote_features.on_touch(now, b.best_bid, b.best_ask)

    def _dnq_skip(self, side: str, quote_px: float, maker_venue: str, books: dict, states: dict, maker_bps: float, taker_bps: float, now: float) -> bool:
        """S3: True when the do-not-quote filter says to skip this quote: naive edge after fees + the model's predicted hedge move < the filter's threshold. Counts a `dnq` reject (the key exists only once used)."""
        flt = self.config.do_not_quote
        if flt is None:
            return False
        taker_venue = next(v for v in books if v != maker_venue)
        st = self._quote_features.observe(now, books[maker_venue], books[taker_venue], states[maker_venue])
        if flt.skip(side, edge_mod.QuoteFeatures.vector(side, st, quote_px, maker_bps, taker_bps)):
            self.rejects["dnq"] = self.rejects.get("dnq", 0) + 1
            return True
        return False

    def _hedge_move_bps(self, side: str, maker_venue: str, taker_venue: str, books: dict, quote_px: float | None = None, horizon_s: float | None = None) -> float:
        """The expected hedge-venue move, in bps, for a passive `side` quote on `maker_venue` (the one place it is chosen: the quote price, the entry rule and the repricing rule all read it).
        No table: the profile's number for that side, unchanged. With a table: the move for a quote `quote_px` behind the maker touch (None = at the touch, all sweeps), at the volatility, flow and spread now.
        The quote price itself is built from the all-depth value (quote_px None), then the entry rule reads the value for the depth that price lands on: one pass, no iteration."""
        cfg = self.config
        table = cfg.adverse_table
        if table is None:
            return cfg.expected_hedge_move_bps_bid if side == "bid" else cfg.expected_hedge_move_bps_ask
        maker_book, taker_book = books[maker_venue], books[taker_venue]
        depth = 0
        if quote_px is not None:
            tick = cfg.venue_ticks[maker_venue]
            behind = (maker_book.best_bid - quote_px) if side == "bid" else (quote_px - maker_book.best_ask)
            depth = max(0, int(round(behind / tick)))
        st = (self._adverse_states or {}).get(maker_venue)
        flow = edge_mod.AdverseFeatures.side_flow(st.signed_volume_within(self._adverse_now, 1.0), side) if st is not None else 0.0
        return table.expected_move_reaching_bps(side, quote_depth_ticks=depth, vol_bps=self._adverse_features.vol_bps(taker_venue, taker_book.mid), flow=flow,
                                                spread_bps=maker_book.spread_bps if maker_book.ready else None, horizon_s=cfg.adverse_horizon_s if horizon_s is None else horizon_s)

    def _quote_limit(self, side: str, hedge_px: float, fee_bps: float, move_bps: float | None = None, funding_bps: float | None = None) -> float:
        """The most a passive bid may rest at / the least a passive ask may rest at, from the hedge-venue price `hedge_px` (the one place this is computed:
        first placement and repricing both call it).
        legacy_v0: the naive edge. Other profiles: the quote is built from the EXPECTED hedge price (touch moved by the conditional hedge-move estimate) less
        fees, edge, buffer, expected slippage, funding/holding and p_fail x loss_fail. The required edge is measured against the price we PAY (the hedge)."""
        cfg = self.config
        required_bps = fee_bps + cfg.min_net_edge_bps + cfg.safety_buffer_bps
        funding = cfg.funding_holding_bps if funding_bps is None else funding_bps
        if move_bps is None:
            move_bps = cfg.expected_hedge_move_bps_bid if side == "bid" else cfg.expected_hedge_move_bps_ask
        if side == "bid":
            px = hedge_px * (1.0 - required_bps / 10_000.0)
            if cfg.profile != "legacy_v0":
                extra_bps = cfg.expected_hedge_slippage_bps + funding + cfg.p_fail * cfg.loss_fail_bps
                expected_hedge = hedge_px * (1.0 + move_bps / 10_000.0)
                px = expected_hedge * (1.0 - (required_bps + extra_bps) / 10_000.0)
            return px
        px = hedge_px / (1.0 - required_bps / 10_000.0)
        if cfg.profile != "legacy_v0":
            extra_bps = cfg.expected_hedge_slippage_bps + funding + cfg.p_fail * cfg.loss_fail_bps
            expected_hedge = hedge_px * (1.0 - move_bps / 10_000.0)
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
        rq_h = cfg.adverse_requote_horizon_s                                 # the repricing rule reads its own horizon (None: the entry rule's)
        funding_bps = self._funding_holding_bps(maker_venue, taker_venue, "bid" if bid else "ask", now)
        px = self._quote_limit("bid" if bid else "ask", hedge_px, fee_bps, self._hedge_move_bps("bid" if bid else "ask", maker_venue, taker_venue, books, None, rq_h), funding_bps)
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
            expected_hedge_move_bps=self._hedge_move_bps("bid" if bid else "ask", maker_venue, taker_venue, books, px, rq_h),
            expected_hedge_slippage_bps=cfg.expected_hedge_slippage_bps, funding_holding_bps=funding_bps, p_fail=cfg.p_fail, loss_fail_bps=cfg.loss_fail_bps)
        if er.net_bps - cfg.safety_buffer_bps < cfg.min_net_edge_bps:
            return None, "no_edge"
        return px, "ok"

    def quote_validity(self, maker_venue: str, side: str, price: float, qty: float, books: dict, now: float) -> tuple[bool, float | None, str]:
        """Quote validity: is a RESTING maker quote (`side` "buy" or "sell", `price`, remaining `qty`) still worth resting? Its net edge is recomputed by the entry rule's own formula (edge.maker_taker_expected_edge_bps,
        the one place an edge is computed) with the hedge priced at the VWAP the hedge venue's book gives for `qty` NOW, and compared with the entry threshold minus `quote_withdraw_hysteresis_bps`.
        Returns (valid, net_bps, why). Valid with a reason when it cannot be measured (stale hedge touch, no price, not enough depth to price the size): an unmeasurable edge never withdraws a quote.
        Pure apart from reading the books; it counts no rejects."""
        cfg = self.config
        if cfg.profile == "legacy_v0":
            raise ValueError("quote_validity needs a non-legacy profile (conditional edge)")
        taker_venue = next(v for v in books if v != maker_venue)
        taker_book = books[taker_venue]
        maker_fees, taker_fees = self.venues[maker_venue].fees, self.venues[taker_venue].fees
        key = "bid" if side == "buy" else "ask"
        hedge_side = "bid" if key == "bid" else "ask"          # a buy quote is hedged by SELLING into the hedge venue's bids; a sell quote by BUYING from its asks
        if not taker_book.ready:
            return True, None, "no_hedge_price"
        if taker_book.touch_age_s(hedge_side, now) > cfg.max_touch_age_s:
            return True, None, "stale_hedge_touch"
        vwap_book = self._hedge_vwap_view(taker_book, hedge_side, qty)
        if vwap_book is None:
            return True, None, "hedge_depth_unknown"
        er = edge_mod.maker_taker_expected_edge_bps(
            price, key, vwap_book, qty, maker_fees.maker_bps, taker_fees.taker_bps,
            expected_hedge_move_bps=self._hedge_move_bps(key, maker_venue, taker_venue, books, price, None),
            expected_hedge_slippage_bps=cfg.expected_hedge_slippage_bps, funding_holding_bps=self._funding_holding_bps(maker_venue, taker_venue, key, now), p_fail=cfg.p_fail, loss_fail_bps=cfg.loss_fail_bps)
        if er.net_bps is None:
            return True, None, "no_hedge_price"
        net = er.net_bps - cfg.safety_buffer_bps
        if net < cfg.min_net_edge_bps - cfg.quote_withdraw_hysteresis_bps:
            return False, net, "edge_below_threshold"
        return True, net, "ok"

    def observe_funding_rate(self, venue: str, rate: float | None, interval_s: float, ts: float, next_funding_ts: float | None = None) -> None:
        """The latest PREDICTED funding rate of `venue` (a fraction per settlement interval of `interval_s` seconds) from the derivative stream, as of `ts`, and the next funding timestamp (seconds) when the tick has it.
        Never realised funding. A next-funding time later than the one held means the held one has settled: the window of that venue ends."""
        if rate is not None and interval_s > 0:
            self._funding_rates[venue] = (float(rate), float(interval_s), float(ts))
        if next_funding_ts:
            self._next_funding[venue] = float(next_funding_ts)

    def funding_window_state(self, now: float) -> tuple[str, str]:
        """The funding stop-quoting window at `now`: ("off", "") when funding_window_s is 0; ("blocked", venue) when a venue is inside its window (from funding_window_s before its next funding timestamp until the stream shows the
        settlement has passed, at most funding_window_after_s after the timestamp); ("not_evaluable", venue) when a venue has no usable next-funding time (never seen, or the held one is more than funding_window_after_s past
        with no newer one); else ("open", "")."""
        cfg = self.config
        if cfg.funding_window_s <= 0:
            return "off", ""
        unusable = ""
        for venue in self.venue_names:
            t = self._next_funding.get(venue)
            if t is None or now > t + cfg.funding_window_after_s:
                unusable = unusable or venue                                      # never seen, or the held timestamp passed with no newer one: nothing to evaluate against
            elif t - cfg.funding_window_s <= now:
                return "blocked", venue                                           # a stream tick after the settlement moves t to the next timestamp, which ends the window (funding booked)
        return ("not_evaluable", unusable) if unusable else ("open", "")

    def _funding_holding_bps(self, maker_venue: str, taker_venue: str, side: str, now: float) -> float:
        """The funding / holding deduction of the edge in bps: the configured constant, plus (only when funding_expected_holding_s > 0) the expected funding cost of holding the pair for that time. `side` "bid": we buy
        (LONG) on the maker venue and hedge short on the taker venue; "ask": the reverse. Cost = holding_s x (rate per second of the long venue - that of the short venue) x 1e4; a positive value is a cost. A missing or
        stale rate adds 0 and is counted (funding_term_unavailable); funding_credit floor_zero never lets a credit raise the edge. Default settings return the constant exactly."""
        cfg = self.config
        if cfg.funding_expected_holding_s <= 0:
            return cfg.funding_holding_bps
        long_v, short_v = (maker_venue, taker_venue) if side == "bid" else (taker_venue, maker_venue)
        rates = []
        for venue in (long_v, short_v):
            got = self._funding_rates.get(venue)
            if got is None or now - got[2] > cfg.funding_rate_max_age_s:
                self.rejects["funding_term_unavailable"] = self.rejects.get("funding_term_unavailable", 0) + 1
                return cfg.funding_holding_bps
            rates.append(got[0] / got[1])
        cost = cfg.funding_expected_holding_s * (rates[0] - rates[1]) * 10_000.0
        if cost < 0 and cfg.funding_credit == "floor_zero":
            cost = 0.0
        return cfg.funding_holding_bps + cost

    @staticmethod
    def _hedge_vwap_view(taker_book, side: str, qty: float):
        """THE walk that prices a hedge for `qty` on the hedge venue's book (`side` "bid": the hedge sells into the bids; "ask": it buys from the asks): a one-price stand-in carrying the VWAP, or None when the book cannot
        fill `qty` (or `qty` is not positive). Entry (entry_hedge_price "vwap") and quote_validity both call it, so they cannot disagree about the hedge price."""
        if qty <= 0:
            return None
        walk = taker_book.walk(side, qty)
        if walk.exhausted or walk.filled_qty + 1e-12 < qty or walk.vwap <= 0:
            return None
        return _VwapTouch(walk.vwap)

    def _entry_quote_qty(self, side: str, price: float, taker_book, hedge_px: float, maker_venue: str, taker_venue: str, inventory, pending) -> float:
        """The size a signal at `price` will have: min(tradeable hedge depth, inventory room, max clip), the formula the signal itself uses below. 0 when a limit leaves nothing (the signal then fails its own later
        checks, so the edge is measured at the clip size just as before)."""
        clip = self._max_clip(price)
        depth = self._tradeable_depth(taker_book, side, hedge_px)
        room = self._inventory_room(maker_venue, taker_venue, inventory, pending) if side == "bid" else self._inventory_room(taker_venue, maker_venue, inventory, pending)
        return clip if (depth <= 0 or room <= 0) else min(depth, room, clip)

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
        self.last_eval_ts = now                       # the time of the evaluation in progress (read by a reject trace: `session_replay.TracedRejects`; no decision reads it)
        self._observe_adverse_features(books, states, now)
        self._observe_quote_features(books, now)

        if cfg.min_resignal_gap_s > 0 and now - self._last_signal_ts < cfg.min_resignal_gap_s:
            self.rejects["throttled"] += 1
            return None

        window, _venue = self.funding_window_state(now)
        if window == "blocked":
            self.rejects["funding_window"] = self.rejects.get("funding_window", 0) + 1
            return None
        if window == "not_evaluable":
            self.rejects["funding_window_not_evaluable"] = self.rejects.get("funding_window_not_evaluable", 0) + 1      # labelled, nothing blocked

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
        which = "depth" if depth_cap < min(inv_cap, clip_cap) else "inventory" if inv_cap < min(depth_cap, clip_cap) else "clip"      # which cap sized this signal (owner 12 Nov, the EMS path coverage table); a counter read by nothing else
        self.clip_binding[which] = self.clip_binding.get(which, 0) + 1

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
        vwap_entry = cfg.profile != "legacy_v0" and cfg.entry_hedge_price == "vwap"      # owner 13 Nov: the hedge is priced at the VWAP for the quote's size (quote price and edge), not at the touch
        price_px = hedge_px
        if vwap_entry:
            est_view = self._hedge_vwap_view(taker_book, "bid", self._entry_quote_qty("bid", hedge_px, taker_book, hedge_px, maker_venue, taker_venue, inventory, pending))
            if est_view is None:
                self.rejects["no_depth"] += 1
                return None
            price_px = est_view.best_bid
        funding_bps = self._funding_holding_bps(maker_venue, taker_venue, "bid", now)
        max_bid = self._quote_limit("bid", price_px, fee_bps, self._hedge_move_bps("bid", maker_venue, taker_venue, books), funding_bps)

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
            edge_book, edge_qty = taker_book, self._max_clip(max_bid)
            if vwap_entry:
                edge_qty = self._entry_quote_qty("bid", max_bid, taker_book, hedge_px, maker_venue, taker_venue, inventory, pending)       # the size the signal will have, walked once: the same walk quote_validity uses
                edge_book = self._hedge_vwap_view(taker_book, "bid", edge_qty)
                if edge_book is None:
                    self.rejects["no_depth"] += 1
                    return None
            er = edge_mod.maker_taker_expected_edge_bps(
                max_bid, "bid", edge_book, edge_qty, maker_fees.maker_bps, taker_fees.taker_bps,
                expected_hedge_move_bps=self._hedge_move_bps("bid", maker_venue, taker_venue, books, max_bid), expected_hedge_slippage_bps=cfg.expected_hedge_slippage_bps,
                funding_holding_bps=funding_bps, p_fail=cfg.p_fail, loss_fail_bps=cfg.loss_fail_bps)
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
        if self._dnq_skip("bid", max_bid, maker_venue, books, states, maker_fees.maker_bps, taker_fees.taker_bps, now):
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
        vwap_entry = cfg.profile != "legacy_v0" and cfg.entry_hedge_price == "vwap"      # owner 13 Nov: see the bid path
        price_px = hedge_px
        if vwap_entry:
            est_view = self._hedge_vwap_view(taker_book, "ask", self._entry_quote_qty("ask", hedge_px, taker_book, hedge_px, maker_venue, taker_venue, inventory, pending))
            if est_view is None:
                self.rejects["no_depth"] += 1
                return None
            price_px = est_view.best_ask
        funding_bps = self._funding_holding_bps(maker_venue, taker_venue, "ask", now)
        min_ask = self._quote_limit("ask", price_px, fee_bps, self._hedge_move_bps("ask", maker_venue, taker_venue, books), funding_bps)

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
            edge_book, edge_qty = taker_book, self._max_clip(min_ask)
            if vwap_entry:
                edge_qty = self._entry_quote_qty("ask", min_ask, taker_book, hedge_px, maker_venue, taker_venue, inventory, pending)
                edge_book = self._hedge_vwap_view(taker_book, "ask", edge_qty)
                if edge_book is None:
                    self.rejects["no_depth"] += 1
                    return None
            er = edge_mod.maker_taker_expected_edge_bps(
                min_ask, "ask", edge_book, edge_qty, maker_fees.maker_bps, taker_fees.taker_bps,
                expected_hedge_move_bps=self._hedge_move_bps("ask", maker_venue, taker_venue, books, min_ask), expected_hedge_slippage_bps=cfg.expected_hedge_slippage_bps,
                funding_holding_bps=funding_bps, p_fail=cfg.p_fail, loss_fail_bps=cfg.loss_fail_bps)
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
        if self._dnq_skip("ask", min_ask, maker_venue, books, states, maker_fees.maker_bps, taker_fees.taker_bps, now):
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


_LIVE_CHILD_STATES = frozenset({OrderState.CREATED, OrderState.SENT, OrderState.CANCEL_PENDING, OrderState.AMEND_PENDING, OrderState.ACKNOWLEDGED, OrderState.PARTIAL})
_WITHDRAWABLE_STATES = frozenset({OrderState.ACKNOWLEDGED, OrderState.PARTIAL})       # a resting quote with no cancel in flight (CANCEL_PENDING is already on its way out)


class _VwapTouch:
    """A one-price stand-in for a hedge book: the VWAP the hedge size would get, in the shape `maker_taker_expected_edge_bps` reads (ready, best_bid, best_ask)."""
    ready = True

    def __init__(self, price: float) -> None:
        self.best_bid = self.best_ask = price


class ArbSessionStrategy:
    """The arb strategy as a `TradingSession` strategy (Phase D, D4): `evaluate(books, now_ns) -> Opportunity | None`.

    It wraps `CrossVenueArbStrategy` unchanged. What the backtester kept in float side books is supplied here from the core instead:
      * books      the float mirror of each core book (`float_mirror`, the one conversion, shared with the QueueAwareVenueAdapter), stamped with the core book's own last receive time so the strategy's
                   staleness check still sees the real age
      * inventory  per-venue base and cash from the CORE LEDGER (`ledger.positions`, `ledger.cash_deltas`), not a float inventory the strategy keeps
      * pending    base still to be bought or sold by orders that are not in the ledger yet, and the live maker order count, from the OMS plans
      * states     the per-venue market state, owned and updated by the runtime once per market event (read-only here)
    The signal becomes an `Opportunity`. Float to Decimal goes through the shortest round-trip text, so it is lossless (`float(Decimal(repr(x))) == x`); the only rounding is the instrument's tick (price) and
    step (quantity), applied where the signal is not already on the grid. `last_signal` and `signals` keep the strategy's own signal by opportunity id for the hedge and the parity manifest."""
    strategy_id = "cross_venue_arb"

    def __init__(
        self, strategy: CrossVenueArbStrategy, *, ledger, oms, states: dict[str, Any], instruments, instrument_id: str,
        venue_names: dict[str, str] | None = None, eligibility_policy=None, view_books=None, entry_gate=None,
    ) -> None:
        self.strategy = strategy
        self.eligibility_policy = eligibility_policy       # the session's shared validity/freshness rule: a signal is only made when it passes, and carries its fingerprint to the router
        self.eligibility_blocked = 0
        self.entry_gate = entry_gate          # callable(now_s) -> True blocks the decision (the profile's MANDATORY dead-feed gate, the backtester's own rule); None = no such gate
        self.entry_gate_blocked = 0
        self.view_books = view_books          # a runtime that owns persistent float view books (touch ages, message-atomic repair) passes them; else a mirror of the core books is built per decision
        self.ledger, self.oms, self.states = ledger, oms, states
        self.instruments, self.instrument_id = instruments, instrument_id
        self.venue_names = venue_names or {v: v.upper() for v in strategy.venue_names}        # feed name -> core venue name
        self._feed_name = {core: feed for feed, core in self.venue_names.items()}
        self.signals: dict[str, ArbSignal] = {}
        self.last_signal: ArbSignal | None = None
        self.grid_snaps = {"price": 0, "quantity": 0}                                         # how often the instrument grid changed a signal value (0 when the strategy already quotes on the grid)

    # -- inputs from the core ------------------------------------------------------------------------------------------------

    def inventory(self) -> InventoryBook:
        inv = InventoryBook()
        inv.ensure(tuple(self.venue_names))
        for feed, core in self.venue_names.items():
            inv.base[feed] = float(self.ledger.positions.get((core, self.instrument_id), Decimal(0)))
            inv.cash[feed] = float(self.ledger.cash_deltas.get(core, Decimal(0)))
        return inv

    def pending_and_live_makers(self) -> tuple[PendingExposure, int]:
        pending, makers = PendingExposure(), 0
        for plan in self.oms.plans.values():
            for child in plan.children:
                if child.state not in _LIVE_CHILD_STATES or child.venue not in self._feed_name:
                    continue
                remaining = float(child.quantity - child.filled_quantity)
                if remaining <= 0:
                    continue
                pending.add(self._feed_name[child.venue], child.side.value.lower(), remaining)
                makers += int(child.post_only)
        return pending, makers

    def float_books(self, books: dict, now_s: float) -> dict[str, OrderBook] | None:
        out = {}
        for feed, core in self.venue_names.items():
            book = books.get((core, self.instrument_id))
            if book is None or not book.bids or not book.asks:
                return None
            ts = book.last_receive_ts_ns / 1_000_000_000 if book.last_receive_ts_ns else now_s
            out[feed] = float_mirror(feed, self.strategy.pair, book.bids, book.asks, ts)
        return out

    def quotes_to_withdraw(self, books: dict, now_ns: int) -> list[dict]:
        """Quote validity, session side: the resting maker orders whose edge no longer clears the threshold minus the hysteresis. Empty unless `quote_withdraw` is on. Reads the OMS plans and the view books;
        it cancels nothing itself (the session asks the OMS to withdraw each one). An order that already has a cancel in flight is not offered again."""
        window, _venue = self.strategy.funding_window_state(now_ns / 1_000_000_000)
        if window == "blocked":                                      # owner 14 Nov: the funding window starts: every resting primary quote is withdrawn (not only the ones whose edge fell)
            return [{"order_id": child.order_id, "net_bps": None, "reason": "funding_window"}
                    for plan in self.oms.plans.values() for child in plan.children
                    if child.post_only and child.order_role == "PRIMARY" and child.state in _WITHDRAWABLE_STATES and child.venue in self._feed_name and child.quantity - child.filled_quantity > 0]
        if not self.strategy.config.quote_withdraw:
            return []
        floats = self.view_books() if self.view_books is not None else self.float_books(books, now_ns / 1_000_000_000)
        if floats is None:
            return []
        now_s = now_ns / 1_000_000_000
        out = []
        for plan in self.oms.plans.values():
            for child in plan.children:
                if not child.post_only or child.order_role != "PRIMARY" or child.state not in _WITHDRAWABLE_STATES or child.venue not in self._feed_name:
                    continue
                remaining = float(child.quantity - child.filled_quantity)
                if remaining <= 0:
                    continue
                valid, net, why = self.strategy.quote_validity(self._feed_name[child.venue], child.side.value.lower(), float(child.limit_price), remaining, floats, now_s)
                if not valid:
                    out.append({"order_id": child.order_id, "net_bps": net, "reason": why})
        return out

    # -- the session's call -------------------------------------------------------------------------------------------------

    def on_derivative(self, ticker) -> None:
        """The session routes every derivative tick here (TradingSession.on_derivative): the predicted funding rate of a venue feeds the strategy's expected funding term (arb.funding_expected_holding_s; off by default,
        then nothing is read). The interval is the tick's own (hours), else 8 h; the venue maps from the core name to the feed name."""
        cfg = self.strategy.config
        if cfg.funding_expected_holding_s <= 0 and cfg.funding_window_s <= 0:
            return
        feed = next((f for f, core in (self.venue_names or {}).items() if core == ticker.venue), None)
        if feed is None:
            return
        hours = float(ticker.funding_interval_hours) if ticker.funding_interval_hours else 8.0
        rate = float(ticker.funding_rate) if (ticker.funding_rate is not None and cfg.funding_expected_holding_s > 0) else None
        nxt = ticker.next_funding_ts_ns / 1_000_000_000 if ticker.next_funding_ts_ns else None
        self.strategy.observe_funding_rate(feed, rate, hours * 3600.0, ticker.receive_ts_ns / 1_000_000_000, nxt)

    def evaluate(self, books: dict, now_ns: int) -> Opportunity | None:
        now_s = now_ns / 1_000_000_000          # int / int is correctly rounded; / 1e9 is not at today's epoch
        fingerprint = ""
        if self.eligibility_policy is not None:
            decision = self.eligibility_policy.evaluate(books, now_ns)
            if not decision.eligible:
                self.eligibility_blocked += 1
                return None
            fingerprint = decision.fingerprint
        if self.entry_gate is not None and self.entry_gate(now_s):
            self.entry_gate_blocked += 1
            return None
        floats = self.view_books() if self.view_books is not None else self.float_books(books, now_s)
        if floats is None:
            return None
        pending, live_makers = self.pending_and_live_makers()
        signal = self.strategy.evaluate(
            floats, self.states, self.inventory(), now_s, live_maker_orders=live_makers,
            pending=pending if self.strategy.config.count_pending_exposure else None,
        )
        if signal is None:
            return None
        opportunity = self.to_opportunity(signal, now_ns, books, fingerprint)
        self.signals[opportunity.opportunity_id] = signal
        self.last_signal = signal
        return opportunity

    # -- signal to Opportunity ----------------------------------------------------------------------------------------------

    @staticmethod
    def _exact(x: float) -> Decimal:
        return Decimal(repr(float(x)))

    def _snap(self, value: Decimal, step: Decimal, kind: str, *, floor: bool = True) -> Decimal:
        if not step or value % step == 0:
            return value
        self.grid_snaps[kind] += 1
        units = value / step
        return (units.to_integral_value(rounding="ROUND_FLOOR" if floor else "ROUND_CEILING")) * step

    def to_opportunity(self, signal: ArbSignal, now_ns: int, books: dict | None = None, eligibility_fingerprint: str = "") -> Opportunity:
        buy_core, sell_core = self.venue_names[signal.buy_venue], self.venue_names[signal.sell_venue]
        buy_spec = self.instruments.get(buy_core, self.instrument_id)
        sell_spec = self.instruments.get(sell_core, self.instrument_id)
        quantity = self._snap(self._exact(signal.qty), max(buy_spec.quantity_step, sell_spec.quantity_step), "quantity")
        return Opportunity(
            opportunity_id=stable_id("arb-signal", self.strategy.pair, signal.signal_id, now_ns), strategy_id=self.strategy_id, instrument_id=self.instrument_id,
            observed_ts_ns=now_ns, buy_venue=buy_core, sell_venue=sell_core,
            buy_price=self._snap(self._exact(signal.buy_price), buy_spec.tick_size, "price", floor=True),         # a bid that is off the grid rests lower, never through its limit
            sell_price=self._snap(self._exact(signal.sell_price), sell_spec.tick_size, "price", floor=False),
            quantity=quantity, gross_edge_bps=self._exact(signal.gross_edge_bps), estimated_net_edge_bps=self._exact(signal.net_edge_bps), eligibility_fingerprint=eligibility_fingerprint,
            # the router refuses a plan whose books moved between the decision and the dispatch: it compares these with the books' last applied events
            source_event_ids=() if books is None else (books[(buy_core, self.instrument_id)].last_event_id or "", books[(sell_core, self.instrument_id)].last_event_id or ""),
        )
