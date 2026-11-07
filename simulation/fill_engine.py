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

import functools
import itertools
import math
import warnings
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

from .models import LambdaBucket, LoggedOrderEvent, Side
from .order_book import OrderBook, WalkResult
from .postonly import would_cross
from .queue_v2.cancel_models import cancel_ahead, clamp_feasible

HAZARD_THETA_PROVENANCE = "SPOT_CALIBRATED (spot data 04-19..04-22); no perp fit exists yet"


class OrderEventSink(Protocol):
    """What the executor needs from a fill model: one call per completed order. new_backtest.fill_model.EmpiricalFillModel implements it (research code, it stays in
    new_backtest); the core must not import it. Phase A: a typing change only, no behaviour."""

    def record_order_event(self, event: LoggedOrderEvent) -> None: ...


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
    cause: str = ""               # maker fills only: trade | delta_share | delta_vanish | snapshot (what produced the fill)
    obs: dict | None = None       # observation only (MakerExecutor.obs_enabled): queue estimate, volume available / consumed, flags; no decision reads it

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

    def __init__(self, consume_liquidity: bool = True, own_impact: str = "legacy") -> None:
        if own_impact not in ("legacy", "overlay"):
            raise ValueError(f"own_impact must be 'legacy' or 'overlay', got {own_impact!r}")
        self.own_impact = own_impact        # overlay: our consumption goes to OrderBook.consume_overlay, the public levels are never changed
        # When True our own fill removes the liquidity it took, so two signals in
        # the same instant cannot both fill off the same resting size. The next
        # feed update overwrites the book, bounding own-impact to sub-tick time.
        self.consume_liquidity = consume_liquidity
        self._ids = itertools.count(1)
        self.consume_calls = 0           # observation only: own taker fills that removed liquidity from the book they walked
        self.consumed_qty = 0.0

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
        if self.consume_liquidity:
            walk = (book.consume_overlay if self.own_impact == "overlay" else book.consume)(book_side, qty, limit_price)
        else:
            walk = book.walk(book_side, qty, limit_price)

        if walk.is_empty:
            # The book moved through our limit during flight — the opportunity
            # was gone before the order landed. This is the dominant failure
            # mode of cross-exchange arb and must not be silently dropped.
            return TakerOutcome(False, None, qty, "price_moved")

        if self.consume_liquidity:
            self.consume_calls += 1
            self.consumed_qty += walk.filled_qty
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
    # WP4: the level was absent AND outside the captured depth, so the queue ahead is UNKNOWN and was drawn from
    # MakerConfig.unknown_queue_* (a flag on the order and in the diagnostics), not observed.
    queue_unknown: bool = False
    filled_qty: float = 0.0
    first_fill_ts: float | None = None
    first_fill_is_synthetic: bool = False  # True when the very first fill came from synthetic flow
    adverse_ts: float | None = None
    trade_fill_qty: float = 0.0
    delta_fill_qty: float = 0.0
    snapshot_fill_qty: float = 0.0
    cross_fill_qty: float = 0.0       # qty filled because the opposite best quote reached our price (opposite_cross_fills)
    synthetic_fill_qty: float = 0.0   # qty filled by BackgroundAgentModel synthetic trades

    # Cancel lifecycle (set by the backtester's lifetime handling; None until a cancel is requested).
    # Between cancel_requested_ts and cancel_effective_ts the order is still resting at the exchange and can fill.
    cancel_requested_ts: float | None = None
    cancel_effective_ts: float | None = None
    cancel_reason: str = ""            # "expired" | "end_of_data"
    filled_at_cancel_request: float = 0.0
    # Host time at which the order reached the exchange (InFlightOrder.release_ts). Events that executed at the exchange
    # before this may correct the queue estimate but cannot fill the order. Defaults to active_ts.
    arrival_ts: float = 0.0
    # Public trade volume printed at this order's price since the level was last observed (by a depth event or a snapshot);
    # used to avoid draining the queue a second time when a snapshot shows the same execution as a smaller level.
    trade_vol_since_obs: float = 0.0
    # Latest displayed size of this order's price level in the feed (others' orders only; unlike observed_level_size it
    # does follow the level upward, because volume that joined behind us dilutes the share of a cancel that was ahead).
    level_now: float = 0.0
    # fill_evidence != "off": time (credit clock) and size of the latest unexplained decrease that moved this order to the
    # front of the queue without filling it. A real trade at the order's price within evidence_window_ms of front_ts fills it.
    front_ts: float | None = None
    front_decrease: float = 0.0
    # opposite_cross_confirm_ms > 0: None (no cross) | "pending" (crossed, waiting for a reaching print until cross_deadline) | "dropped" (window passed
    # with no print; stays dropped until the book uncrosses). Credit-clock times.
    cross_state: str | None = None
    cross_ts: float = 0.0
    cross_deadline: float = 0.0
    cross_gap: bool = False            # the book uncrossed while a wait was still inside its window

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


# fill_evidence mode groups (see MakerConfig.fill_evidence)
_TRADE_CONFIRM_MODES = ("trade_confirm", "trade_confirm+fractional", "confirm_delta_snapshot", "confirm_delta")
_GATED_MODES = ("confirm_delta_snapshot", "confirm_delta")
_WHOLE_RULE_MODES = ("trade_confirm", "fractional", "trade_confirm+fractional")


@dataclass
class LifecycleRecord:
    """Explicit lifecycle of one passive order in the shared vocabulary (order_state_machine.State), kept by the executor.

    Keyed by the in-flight order id (the id the strategy and the backtester know the order by; a rejected order never becomes a MakerOrder).
    Bookkeeping only: nothing here moves a fill, a queue position or a timer, so recording it cannot change a result.
    `source` of every transition is historical | modelled | injected (order_events.SOURCES)."""
    inflight_id: int
    venue: str
    maker_id: int | None = None
    state: str = "submitted"
    unknown_since: float | None = None
    unknown_until: float | None = None        # when a status query / late ack settles it; None = never within the data
    reject_ts: float | None = None
    reject_reason: str = ""
    reject_after_fill: bool = False
    cancelled_ts: float | None = None
    order: object = None                       # the MakerOrder once it rests (kept after it leaves the book, for its final fill total)
    history: list = field(default_factory=list)


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
    # WP4 — what an ABSENT level means. "legacy" (default): min_queue_ahead is the floor on every absent level (above).
    # "depth_aware": an absent level INSIDE the captured depth (between the touch and the deepest captured level, or inside the
    # spread) is a genuinely empty level -> queue 0; an absent level OUTSIDE the captured depth is UNKNOWN -> the queue is set
    # from unknown_queue_dist ("floor": unknown_queue_floor; "lognormal": a draw with median unknown_queue_median and
    # log-std unknown_queue_sigma, seeded) and the order is flagged queue_unknown.
    unknown_queue_model: str = "legacy"
    unknown_queue_dist: str = "floor"
    # "fitted" (queue model v2, step 7): draw from the distribution fitted on in-depth observations at comparable distance from the touch (training days only),
    # file = unknown_queue_fit (queue_v2/unknown_queue.py), one seeded draw per order. unknown_queue_floor remains the lower bound only when > 0.
    unknown_queue_fit: str = ""
    # v1.1 (audit decision C step 6): the queue used where the fitted distribution has NO data for a venue (counted in diag unknown_queue_fit_missing_venue_used_floor).
    # -1 (default, bit-identical): unknown_queue_floor plays both roles (lower bound of a draw AND the no-fit fallback). >= 0: unknown_queue_floor stays the lower bound
    # of a draw only (0 = no universal floor on a fitted draw) and this value is the flagged fallback.
    unknown_queue_fallback: float = -1.0
    unknown_queue_floor: float = 0.0
    unknown_queue_median: float = 0.0
    unknown_queue_sigma: float = 0.0
    unknown_queue_seed: int = 7
    # WP4 — level-creator rule (opt-in). An order that CREATED its level (level_was_absent) has nobody ahead of it, so a depth
    # decrease at that price cannot be "the queue ahead leaving": it may only fill by public trades. False (default): legacy.
    level_creator_trade_only: bool = False
    # One public trade can fill our resting orders only once in total: the volume is shared in
    # price-then-time priority instead of being handed in full to every order (False = legacy).
    share_trade_volume: bool = True
    # A real execution is published twice, as a trade and as a depth reduction at the same price. Volume already
    # attributed to an execution by one feed is not drained again when the other feed reports it (False = legacy).
    dedupe_trade_depth: bool = True
    dedupe_window_s: float = 0.05
    # Window clock for the trade/depth overlap: "exchange" (the events' exchange times, host-aligned; reliable because both
    # feeds stamp the match time) or "receive" (delivery times; legacy, jittery on collector data).
    dedupe_time_basis: str = "exchange"
    # A snapshot that shows a trade's volume as a smaller level does not drain the queue a second time (needs
    # dedupe_trade_depth). False: legacy, the snapshot shrink drains in full.
    snapshot_trade_explain: bool = True
    # True: an event that executed at the exchange before the order arrived there cannot fill it (it may still drain the
    # stale queue estimate). False: legacy, any event received after placement can fill.
    exchange_time_eligibility: bool = True
    # How a depth decrease that no trade explains moves the queue ahead of us. The feed has no order ids, so who cancelled
    # is unknown. Default "proportional" (chosen 2026-10-30 after the 22-coin fill-evidence study: higher hit rate and lower
    # loss than "all_ahead" in every liquidity tier). None = not chosen: behaves as the default, and a backtest warns about it.
    #   "all_ahead":     every decrease BELOW the level size we last saw (observed_level_size, a running minimum that never
    #                    rises) is charged in full to the queue ahead of us. Growth after we joined is ignored, so a
    #                    decrease of volume that arrived behind us, or any cancel hidden by such growth, is not seen at
    #                    all. The queue therefore advances in full steps on some decreases and not at all on others;
    #                    on real fills it is the SLOWER model, because the ignored decreases include cancels ahead of us.
    #   "proportional":  every decrease of the level as displayed (level_now, which does follow growth) is split between
    #                    ahead and behind by size, so queue_ahead / level_before of it advances us. A delta can fill us only
    #                    once the queue is exhausted.
    #   "power":         as proportional but the share ahead is ahead**n / (ahead**n + behind**n), n = cancel_power_n.
    #                    n = 1 is exactly "proportional"; n > 1 assumes cancels favour the larger side.
    cancel_model: str | None = "proportional"
    cancel_power_n: float = 1.0
    # Evidence needed before an unexplained decrease may fill us. Without it, once the simulated queue ahead is 0 any later
    # unexplained depth decrease (delta path) or snapshot shrink fills the order (delta_fill_share / snapshot_fill_share).
    # Opt-in; "off" is the behaviour described above, unchanged.
    #   "off":                       as above.
    #   "trade_confirm":             a decrease that finds no queue ahead (or a level that vanished) does NOT fill us: it moves us
    #                                to the front and records when. A public trade at our price (or through it, when
    #                                fill_through_prints is on) arriving within evidence_window_ms of that decrease then fills us,
    #                                up to the trade's size, even if the trade/depth de-duplication would net it against the
    #                                decrease (cause "trade_after_front"). Without a trade we stay at the front; a later trade
    #                                fills us by the ordinary trade rule.
    #   "fractional":                the fill from such a decrease is capped at evidence_fill_fraction x the decrease.
    #   "trade_confirm+fractional":  trade_confirm, and the confirming fill is capped at evidence_fill_fraction x the decrease.
    # A level that vanished moves us to the front with no fill under every mode except "off".
    #   "confirm_delta_snapshot":    targeted variant: only the fills that rest on a depth decrease that finds no queue ahead
    #                                (cause delta_share) or on a snapshot shrink (cause snapshot) need a confirming trade, exactly as
    #                                in trade_confirm (mark the front, fill up to the trade's size within evidence_window_ms, cause
    #                                trade_after_front). A level that vanished (delta_vanish) is NOT gated and behaves as "off";
    #                                trades are unchanged.
    #   "confirm_delta":             as confirm_delta_snapshot, but a snapshot shrink behaves as "off" (only delta_share is gated).
    fill_evidence: str = "off"
    evidence_window_ms: float = 100.0
    evidence_fill_fraction: float = 0.15
    # When the queue ahead of an order counts as empty. "absolute": queue_ahead <= 1e-12 (the behaviour so far, bit-identical).
    # "lot": queue_ahead <= queue_empty_lot (one lot of the instrument, set by the caller). "frac_of_order": queue_ahead <=
    # queue_empty_frac x the order's size. A queue at or below the threshold after a drain (or at placement) is set to 0.
    # Proportional cancels shrink the queue by a constant share, so it only reaches 1e-12 after hundreds of cancels.
    # True: when a depth event removes the whole level we rest on (cause delta_vanish), the queue ahead of us is gone with it
    # (queue_ahead = 0) before the fill is computed. False (default): the proportional share rule leaves a residual queue
    # (the part of the vanished level that a trade had already explained is not charged to it), which can block the fill.
    vanish_clears_queue: bool = False
    # Queue model v2 (step 4), opt-in. False (default): netting credits expire only with dedupe_window_s (legacy, bit-identical). True: a credit also
    # belongs to its connection epoch, so a snapshot (and reset_credits(), called on a feed reset / reconnect) invalidates every credit of that venue, and a print
    # that has to choose between several pending credits at one level inside the window is counted as an AMBIGUOUS association (diag ledger_ambiguous,
    # obs flag ambiguous_trade_depth) instead of being silently resolved.
    ledger_epoch: bool = False
    # Queue model v2 (step 4, depletion ledger), opt-in, v1_2. False (default): a print netted against an earlier depth decrease drains nothing more, so a decrease that moved the
    # queue ahead by only its share (proportional) leaves the order further back than the same execution arriving print-first, and a trade_confirm fill after a head move
    # may use the whole print (legacy, bit-identical). True: each depth decrease records the ahead movement it applied; when the matching print arrives only the print
    # volume NOT already applied as head movement reaches the queue ahead and then the order (trade_after_front included), so both arrival orders give the same queue
    # ahead and the same fills. Also counts netted prints / decreases that rest on the price+window association alone (diag ledger_netted_*_no_proof, kept apart from
    # ledger_ambiguous). Scalar queue only: the cohort queue has its own provisional ledger and ignores the movement rule (the counting still runs).
    depletion_ledger: bool = False
    # Queue model v2 (step 6), opt-in. "scalar" (default): one number, queue_ahead, per order (legacy). "cohort": the external size at our price is kept as cohorts by
    # arrival with our own orders interleaved (queue_v2/cohort.py); residual cancellations are removed by `cohort_age`; the cancel_model arithmetic is NOT used. The
    # initial displayed size is one cohort of unknown age (cohort_unknown_age_s, an assumption). Works with opposite_cross_fills (a cross clears the cohorts ahead of the order, _clear_ahead); not supported with amend.
    # cohort_hazard_theta (age rule 'hazard') is SPOT_CALIBRATED (spot data 04-19..04-22), NOT a perp fit: do not use the hazard rule in a perp sensitivity range until a perp fit from probe data exists;
    # every result that runs the hazard rule carries diag cohort_hazard_theta_provenance.
    queue_model: str = "scalar"
    cohort_age: str = "uniform"
    cohort_hazard_theta: float = 1.0
    cohort_unknown_age_s: float = 30.0
    queue_empty_threshold: str = "absolute"
    queue_empty_lot: float = 0.0
    queue_empty_frac: float = 0.05

    CANCEL_MODELS = ("all_ahead", "min_baseline", "proportional", "power", "behind_first", "ahead_first")
    UNKNOWN_QUEUE_MODELS = ("legacy", "depth_aware")
    UNKNOWN_QUEUE_DISTS = ("floor", "lognormal", "fitted")
    FILL_EVIDENCE_MODES = ("off", "trade_confirm", "fractional", "trade_confirm+fractional", "confirm_delta_snapshot", "confirm_delta")
    QUEUE_EMPTY_MODES = ("absolute", "lot", "frac_of_order")

    def __post_init__(self) -> None:
        if self.cancel_model is not None and self.cancel_model not in self.CANCEL_MODELS:
            raise ValueError(f"cancel_model must be one of {self.CANCEL_MODELS} or None, got {self.cancel_model!r}")
        if self.queue_model not in ("scalar", "cohort"):
            raise ValueError(f"queue_model must be 'scalar' or 'cohort', got {self.queue_model!r}")
        if self.queue_model == "cohort":
            if self.cohort_age not in ("youngest_first", "oldest_first", "uniform", "hazard"):
                raise ValueError(f"cohort_age must be youngest_first | oldest_first | uniform | hazard, got {self.cohort_age!r}")
            if self.cohort_hazard_theta < 0.0 or self.cohort_unknown_age_s < 0.0:
                raise ValueError("cohort_hazard_theta and cohort_unknown_age_s must be >= 0")
            if self.cohort_age == "hazard":
                warnings.warn(f"cohort_age='hazard' uses cohort_hazard_theta={self.cohort_hazard_theta}, {HAZARD_THETA_PROVENANCE}: not a perp fit, keep it out of the perp sensitivity range", RuntimeWarning, stacklevel=2)
        if not (self.opposite_cross_confirm_ms >= 0.0):
            raise ValueError(f"opposite_cross_confirm_ms must be >= 0, got {self.opposite_cross_confirm_ms!r}")
        if self.opposite_cross_confirm_ms > 0.0 and not self.opposite_cross_fills:
            raise ValueError("opposite_cross_confirm_ms > 0 needs opposite_cross_fills")
        if self.opposite_cross_cap_to_print and not self.opposite_cross_confirm_ms > 0.0:
            raise ValueError("opposite_cross_cap_to_print needs opposite_cross_confirm_ms > 0")
        if not (self.cancel_power_n > 0.0):
            raise ValueError(f"cancel_power_n must be > 0, got {self.cancel_power_n!r}")
        if self.fill_evidence not in self.FILL_EVIDENCE_MODES:
            raise ValueError(f"fill_evidence must be one of {self.FILL_EVIDENCE_MODES}, got {self.fill_evidence!r}")
        if not (self.evidence_window_ms >= 0.0):
            raise ValueError(f"evidence_window_ms must be >= 0, got {self.evidence_window_ms!r}")
        if not (0.0 < self.evidence_fill_fraction <= 1.0):
            raise ValueError(f"evidence_fill_fraction must be in (0, 1], got {self.evidence_fill_fraction!r}")
        if self.unknown_queue_model not in self.UNKNOWN_QUEUE_MODELS:
            raise ValueError(f"unknown_queue_model must be one of {self.UNKNOWN_QUEUE_MODELS}, got {self.unknown_queue_model!r}")
        if self.unknown_queue_dist not in self.UNKNOWN_QUEUE_DISTS:
            raise ValueError(f"unknown_queue_dist must be one of {self.UNKNOWN_QUEUE_DISTS}, got {self.unknown_queue_dist!r}")
        if self.unknown_queue_floor < 0.0 or self.unknown_queue_median < 0.0 or self.unknown_queue_sigma < 0.0:
            raise ValueError("unknown_queue_floor / median / sigma must be >= 0")
        if self.queue_empty_threshold not in self.QUEUE_EMPTY_MODES:
            raise ValueError(f"queue_empty_threshold must be one of {self.QUEUE_EMPTY_MODES}, got {self.queue_empty_threshold!r}")
        if self.queue_empty_threshold == "lot" and not (self.queue_empty_lot > 0.0):
            raise ValueError(f"queue_empty_threshold='lot' needs queue_empty_lot > 0, got {self.queue_empty_lot!r}")
        if self.queue_empty_threshold == "frac_of_order" and not (0.0 < self.queue_empty_frac <= 1.0):
            raise ValueError(f"queue_empty_frac must be in (0, 1], got {self.queue_empty_frac!r}")

    @property
    def effective_cancel_model(self) -> str:
        """The engine's name: min_baseline is the guide's name for all_ahead (the old name stays the canonical one in outputs)."""
        m = self.cancel_model or "proportional"
        return "all_ahead" if m == "min_baseline" else m
    # Price priority. True: a public print strictly THROUGH our price (a sell below our bid, a buy above our ask) means the
    # level we rest on has been consumed, so the order fills in full, as in the core queue engines. The print's volume is
    # not charged to the shared budget. False (default, legacy): the print only burns queue and fills what is left.
    fill_through_prints: bool = False
    # Queue model v2 (step 8), opt-in; only with fill_through_prints. False (default): a print strictly through our price fills the order in full. True: the fill is
    # limited by the print's volume (one budget shared by every order the print went through): what a print can really have executed at a level it passed through.
    through_print_volume_limited: bool = False
    # Price priority on the book. True: once the opposite best quote is at or through our price (best ask <= our bid, best bid
    # >= our ask) on an event that reached the exchange after our order did, the order fills in full at its own price as a
    # maker fill (cause "opposite_cross"), as hftbacktest's exchange models do. A real resting order would have traded with that
    # quote. False (default, legacy): no fill from the book alone; a crossed level is only removed by the book's uncross.
    opposite_cross_fills: bool = False
    # v1.1 step 8, opt-in (0 = off, the behaviour above, unchanged). > 0 (needs opposite_cross_fills): a cross is only a fill when a public print
    # that reaches the order (sell aggressor at or below a bid, buy aggressor at or above an ask; the same test as on_trade) executed on the same
    # venue within +-this many ms of the crossing event, after the order reached the exchange. A print in the window BEFORE the cross fills the
    # order at the cross; otherwise the order waits, and the first reaching print in the window AFTER fills it at that print's time (causal: nothing
    # starts before its evidence). No print: the cross is dropped and has no effect at all (no queue jump, the order stays as it was); the window is
    # anchored at the FIRST cross of a crossing episode and re-armed only after the book uncrosses. The fill size is the cross's (the order's
    # remaining size), not capped by the print: the print is evidence, not a volume budget; cause trade_confirmed_cross_before / _after.
    opposite_cross_confirm_ms: float = 0.0
    # v1.1 step 9, opt-in (False = the uncapped fill above, unchanged; needs opposite_cross_confirm_ms > 0). True: the print IS a volume budget. A confirmed cross
    # fills min(order remaining, volume of the reaching prints in the window that no order has used yet). Each buffered print carries the volume it has
    # left; a fill (cross or ordinary trade rule) consumes it, so one print never fills more than its size in total, across orders and across repeated
    # looks at a still-crossed book. A partly filled order stays pending until its window ends and is topped up by later reaching prints.
    opposite_cross_cap_to_print: bool = False


@dataclass
class AmendResult:
    order: "MakerOrder | None"     # the replacement, or None when it was rejected / the order was unknown
    rejected: bool                 # the replacement was refused (PostOnly would take liquidity)
    old_cancelled: bool            # the original order (and its queue position) is gone
    reason: str


def _count_queue_updates(kind: str):
    """Observation only (B0): count the events after which a resting order's queue_ahead went DOWN (a queue-position update), and how many of them carried the ambiguity flag
    (the event's trigger is marked ambiguous: a depth decrease with no matched print, or a print several pending credits could explain). Reads queue_ahead before and after
    the call; changes nothing. It is installed on the instance only while observations are on (the obs_enabled setter), so a run with observations off calls the
    plain methods. Read from MakerExecutor.queue_updates."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(self, *a, **kw):
            if not self.orders:
                return fn(self, *a, **kw)
            before = [(o, o.queue_ahead) for o in self.orders.values()]
            trig0 = self._obs_trig
            out = fn(self, *a, **kw)
            moved = sum(1 for o, q in before if o.queue_ahead < q - 1e-12)
            if moved:
                amb = self._obs_trig is not trig0 and bool(self._obs_trig.get("ambiguous"))
                row = self.queue_updates.setdefault(kind, {"events": 0, "order_updates": 0, "ambiguous_events": 0, "ambiguous_order_updates": 0})
                row["events"] += 1
                row["order_updates"] += moved
                if amb:
                    row["ambiguous_events"] += 1
                    row["ambiguous_order_updates"] += moved
            return out
        return inner
    return wrap


class MakerExecutor:
    """Passive order lifecycle with queue simulation and online fill learning."""

    _QUEUE_UPDATE_HOOKS = (("on_trade", "trade"), ("on_delta", "delta"), ("on_snapshot", "snapshot"))

    @property
    def obs_enabled(self) -> bool:
        return self._obs_enabled

    @obs_enabled.setter
    def obs_enabled(self, on: bool) -> None:
        self._obs_enabled = bool(on)
        for name, kind in self._QUEUE_UPDATE_HOOKS:
            if self._obs_enabled:
                self.__dict__[name] = functools.partial(_count_queue_updates(kind)(getattr(type(self), name)), self)
            else:
                self.__dict__.pop(name, None)

    def __init__(
        self,
        fill_model: OrderEventSink,
        config: MakerConfig | None = None,
    ) -> None:
        self.fill_model = fill_model
        self.config = config or MakerConfig()
        self._unknown_rng = None
        self._ids = itertools.count(1)
        self.orders: dict[int, MakerOrder] = {}
        self.lifecycle: dict[int, LifecycleRecord] = {}        # explicit REJECTED / UNKNOWN / ... states by in-flight order id
        self._lc_by_maker: dict[int, LifecycleRecord] = {}
        self.lifecycle_counts: dict[str, int] = {}
        self.orders_placed = 0
        self.orders_filled = 0
        self.orders_cancelled = 0
        self.amends_requested = 0
        self.amends_rejected_postonly = 0
        # Observation only (steps 6-7 of the execution-layer brief): when on, every maker Fill carries `obs`. Reads state, draws nothing, writes nothing the model uses.
        # Queue model v2: with own_overlay the print volume our own fills consumed is recorded per (venue, side, price) and dropped at the next
        # public update of that level (a depth event or a snapshot). It is a ledger of OUR consumption; the public book is never touched.
        self.own_overlay = False
        self.print_overlay: dict[tuple, float] = {}
        self._uq_fit = None
        self._uq_draws = 0
        self._settings_hash = None
        self._lq: dict[tuple, "LevelQueue"] = {}           # cohort mode: one level queue per (venue, side, price)
        self._cohort_ahead: dict[int, float] = {}          # cohort mode: queue ahead per own order after the current public event
        self.cohort_log: list[dict] = []                   # cohort mode: counterfactual projections (bounded)
        self.obs_enabled = False
        self._obs_trig: dict = {}
        self.queue_updates: dict[str, dict[str, int]] = {}         # observation only: see _count_queue_updates
        self._obs_qb: dict[int, tuple[float, float]] = {}      # maker order id -> (queue ahead, event volume offered) just before its fill
        self._explained: dict[str, dict[tuple, deque]] = {"trade": {}, "delta": {}}
        self.ledger_stats: dict[str, float] = {}           # observation / depletion_ledger only: how much matched volume arrived book-first vs trade-first, and on what evidence (see _ledger_note)
        self.cross_log: list[dict] = []                    # opposite_cross_confirm_ms: one row per resolved cross (confirmed before / after, dropped); bounded
        self._cx_prints: dict[str, deque] = {}             # opposite_cross_confirm_ms: recent public prints per venue [ct, event_ts, price, sign, gross, left] (left = volume no fill has used yet)
        # Aggregate counters, all in base-asset units unless named *_events.
        self.diag: dict[str, float] = {k: 0.0 for k in (
            "trade_events", "trade_volume_observed", "trade_volume_credited_to_depth", "trade_volume_net",
            "trade_volume_to_queue_ahead", "trade_volume_to_fills", "pre_arrival_event_drains", "pre_arrival_queue_drained",
            "delta_shrink_events", "delta_shrink_total", "delta_shrink_explained_by_trades", "delta_queue_drained",
            "snapshot_shrink_total", "snapshot_shrink_explained_by_trades", "snapshot_queue_drained",
            "fills_from_trade", "fills_from_delta", "fills_from_snapshot", "fills_from_through_print",
            "evidence_front_marks", "evidence_suppressed_fill_qty", "evidence_confirmed_fill_qty")}

    def _settings_sha(self) -> str:
        if self._settings_hash is None:
            import dataclasses
            import hashlib
            import json
            settings = dataclasses.asdict(self.config)
            if not settings.get("depletion_ledger"):
                settings.pop("depletion_ledger", None)         # listed only when on: the settings hash of every earlier profile is unchanged
            self._settings_hash = hashlib.sha256(json.dumps(settings, sort_keys=True, default=str).encode()).hexdigest()
        return self._settings_hash

    def _obs_begin(self, kind: str, **kw) -> None:
        self._obs_qb.clear()
        self._obs_trig = {"kind": kind, **kw}

    # ── Trade / depth de-duplication ────────────────────────────────

    # ── cohort mode helpers (queue_model == "cohort") ──
    def _cohort_on(self) -> bool:
        return self.config.queue_model == "cohort"

    @staticmethod
    def _lqk(order: "MakerOrder") -> tuple:
        return (order.venue, "bid" if order.side == Side.BID else "ask", round(order.price, 8))

    def _lq_drop(self, order: "MakerOrder") -> None:
        key = self._lqk(order)
        lq = self._lq.get(key)
        if lq is None:
            return
        lq.remove_own(order.order_id)
        if not lq.has_own:
            if len(self.cohort_log) < 20000:
                self.cohort_log.extend(lq.log)
            del self._lq[key]
        else:
            for o in self.orders.values():
                if o.is_live and self._lqk(o) == key:
                    self._cohort_refresh(o)          # an own order ahead left the queue (filled or cancelled): the ones behind it move up

    def _clear_ahead(self, order: "MakerOrder") -> None:
        """Everything ahead of this order is gone (a confirmed cross, a vanished level, a queue at or below the empty threshold, a print through our price):
        the cohort queue is told too, so queue_ahead and the cohort queue never disagree. Scalar queue: queue_ahead = 0 and nothing else."""
        if self._lq:
            lq = self._lq.get(self._lqk(order))
            if lq is not None:
                lq.clear_ahead(order.order_id)
                self._cohort_refresh(order)          # an own order resting ahead of this one is not cleared: its remaining size stays ahead (0 when there is none)
                return
        order.queue_ahead = 0.0

    def _cohort_refresh(self, order: "MakerOrder") -> None:
        """Cohort mode: copy the cohort queue's value onto queue_ahead after a change that did not pass through _cohort_trade / _consume."""
        lq = self._lq.get(self._lqk(order))
        if lq is None or not order.is_live:
            return
        try:
            order.queue_ahead = lq.ahead(order.order_id)
        except KeyError:
            order.queue_ahead = 0.0

    def _cohort_check(self) -> None:
        """Observation only (cohort mode): after an event, every live order's queue_ahead equals the cohort queue's ahead. Counts checks and violations, changes nothing."""
        if not self._lq:
            return
        bad = 0
        for o in self.orders.values():
            lq = self._lq.get(self._lqk(o))
            if lq is None or not o.is_live:
                continue
            try:
                if abs(o.queue_ahead - lq.ahead(o.order_id)) > 1e-9:
                    bad += 1
            except KeyError:
                bad += 1
        self.diag["cohort_ahead_checks"] = self.diag.get("cohort_ahead_checks", 0) + 1
        if bad:
            self.diag["cohort_ahead_mismatch_orders"] = self.diag.get("cohort_ahead_mismatch_orders", 0) + bad
            self.diag["cohort_ahead_mismatch_events"] = self.diag.get("cohort_ahead_mismatch_events", 0) + 1

    def _cohort_rebase(self, orders: list, displayed: float, now: float, note: str) -> None:
        """Counterfactual projection at a settled update boundary, once per level: the displayed external size is the truth about the public book."""
        lq = self._lq.get(self._lqk(orders[0]))
        if lq is None:
            return
        cfg = self.config
        lq.expire_provisional(now - cfg.dedupe_window_s)
        lq.rebase_external(displayed, now, cfg.cohort_age, cfg.cohort_hazard_theta, note)
        for o in orders:
            self._cohort_ahead[o.order_id] = lq.ahead(o.order_id)

    def _cohort_trade(self, eligible: list, volume: float, now: float, fee_bps: float, is_synthetic: bool) -> list:
        """One print, one budget, one traversal per level: the print goes through the cohorts and our orders in queue order."""
        cfg = self.config
        fills: list = []
        by_key: dict[tuple, list] = {}
        for o in eligible:
            by_key.setdefault(self._lqk(o), []).append(o)
        for key, orders in by_key.items():
            lq = self._lq.get(key)
            if lq is None:
                continue
            qb = {o.order_id: o.queue_ahead for o in orders}
            got = lq.consume_print(volume)
            for o in sorted(orders, key=lambda x: (x.placed_ts, x.order_id)):
                if self.obs_enabled:
                    self._obs_qb[o.order_id] = (qb[o.order_id], volume)
                try:
                    o.queue_ahead = lq.ahead(o.order_id)
                except KeyError:
                    o.queue_ahead = 0.0
                self.diag["trade_volume_to_queue_ahead"] += max(0.0, qb[o.order_id] - o.queue_ahead)
                q = got.get(o.order_id, 0.0)
                if q > 1e-12 and cfg.trade_fill_share > 0:
                    f = self._fill_from(o, q / cfg.trade_fill_share, now, cfg.trade_fill_share, "trade", fee_bps, is_synthetic, cause="trade")
                    if f is not None:
                        fills.append(f)
                        self.diag["trade_volume_to_fills"] += f.qty
                lq.sync_own(o.order_id, o.remaining)
        return fills

    def reset_credits(self, venue: str) -> None:
        """Connection epoch change (reconnect, feed reset, snapshot with ledger_epoch): credit from the old epoch can never explain a new print."""
        for kind in self._explained:
            for k in [k for k in self._explained[kind] if k[0] == venue]:
                del self._explained[kind][k]

    _PROOF_GAP_S = 1e-3          # a print and a decrease stamped within 1 ms by the exchange AND equal in size are counted as matched on more than the price and the window

    def _take(self, kind: str, key: tuple, now: float, want: float, moved_out: dict | None = None, trace: list | None = None) -> float:
        """Take up to `want` of pending credit. moved_out (depletion_ledger): order id -> [ahead movement the credit's decrease applied, credit volume taken while the order rested]. trace: one
        (credit time, credit size before, volume taken) per credit used."""
        credits = self._explained[kind].get(key)
        if not credits:
            return 0.0
        horizon = now - self.config.dedupe_window_s
        while credits and credits[0][0] < horizon:
            credits.popleft()
        if self.config.ledger_epoch and len(credits) > 1:
            self.diag["ledger_ambiguous"] = self.diag.get("ledger_ambiguous", 0.0) + 1.0
            self._obs_trig["ambiguous"] = True
            self._obs_trig["competing_credits"] = [[round(now - c[0], 6), c[1]] for c in credits]       # age (s) and quantity of every pending credit that could explain this decrease
        taken = 0.0
        while credits and taken < want - 1e-12:
            use = min(credits[0][1], want - taken)
            if trace is not None:
                trace.append((credits[0][0], credits[0][1], use))
            if moved_out is not None and len(credits[0]) > 2:
                for oid, m in credits[0][2].items():
                    got = min(m, use)                        # the head movement the decrease applied, first come first matched: a print never explains more movement than its own volume
                    credits[0][2][oid] = m - got
                    cell = moved_out.setdefault(oid, [0.0, 0.0])
                    cell[0] += got
                    cell[1] += use
            taken += use
            credits[0][1] -= use
            if credits[0][1] <= 1e-12:
                credits.popleft()
        if not credits:
            del self._explained[kind][key]
        return taken

    def _add(self, kind: str, key: tuple, now: float, amount: float, ledger: bool = False) -> list | None:
        if amount <= 1e-12:
            return None
        book = self._explained[kind]
        if len(book) > 4096:
            horizon = now - self.config.dedupe_window_s
            for k in [k for k, d in book.items() if not d or d[-1][0] < horizon]:
                del book[k]
        entry = [now, amount, {}] if ledger else [now, amount]
        book.setdefault(key, deque(maxlen=64)).append(entry)
        return entry

    def _ledger_note(self, kind: str, matched: float, whole: float, trace: list, now: float) -> bool:
        """Observation: one netting. kind 'print' = a print netted against an EARLIER decrease (book-first), 'decrease' = a decrease netted against an EARLIER print (trade-first).
        Returns True when the pair rests on the price + window association alone (no other proof): the exchange stamps are more than 1 ms apart AND the sizes differ."""
        tol = 1e-9 * max(1.0, whole)
        same_size = len(trace) == 1 and abs(trace[0][1] - whole) <= tol and abs(trace[0][2] - whole) <= tol
        gap = max(now - t[0] for t in trace)
        tight = gap <= self._PROOF_GAP_S + 1e-12
        tier = "exact" if (same_size and tight) else ("size_or_time" if (same_size or tight) else "no_proof")
        st = self.ledger_stats
        for k, v in ((f"{kind}s_netted", 1.0), (f"{kind}_volume_netted", matched), (f"{kind}s_netted_{tier}", 1.0), (f"{kind}_volume_netted_{tier}", matched)):
            st[k] = st.get(k, 0.0) + v
        if tier == "no_proof" and self.config.depletion_ledger:
            self.diag[f"ledger_netted_{kind}s_no_proof"] = self.diag.get(f"ledger_netted_{kind}s_no_proof", 0.0) + 1.0
            self.diag[f"ledger_netted_{kind}_volume_no_proof"] = self.diag.get(f"ledger_netted_{kind}_volume_no_proof", 0.0) + matched
        return tier == "no_proof"

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
        arrival_ts: float | None = None,
    ) -> MakerOrder:
        level_size = self._level_size(book, side, price, tick_size)
        if self.config.queue_model == "cohort":                   # exact price only: a coarse tick estimate must not pick a neighbouring level's size
            level_size = book.level_size("bid" if side == Side.BID else "ask", price)
        touch = book.best_bid if side == Side.BID else book.best_ask
        rel_ticks = 0
        if tick_size > 0 and touch > 0:
            delta = (price - touch) if side == Side.BID else (touch - price)
            rel_ticks = int(round(delta / tick_size))
            rel_ticks = max(-3, min(3, rel_ticks))

        level_was_absent = level_size == 0.0
        initial_q = level_size * max(0.0, self.config.queue_stress_multiplier)
        queue_unknown = False
        if level_was_absent:
            if self.config.unknown_queue_model == "legacy":
                # Apply min_queue_ahead as a floor only when the price level is absent
                # from the book (level_size == 0).  On a genuine empty level, queue = 0
                # is correct for FIFO.  When the level is absent due to data granularity
                # or a stale book, min_queue_ahead provides a conservative proxy depth.
                if self.config.min_queue_ahead > 0.0:
                    initial_q = self.config.min_queue_ahead
            elif self._inside_captured_depth(book, side, price):
                self.diag["absent_level_inside_depth"] = self.diag.get("absent_level_inside_depth", 0) + 1          # legitimately empty: queue 0
            else:
                queue_unknown = True
                initial_q = self._unknown_queue_draw(venue, price, touch, tick_size)
                self.diag["absent_level_outside_depth_unknown"] = self.diag.get("absent_level_outside_depth_unknown", 0) + 1
                self.diag["unknown_queue_assigned_total"] = self.diag.get("unknown_queue_assigned_total", 0) + initial_q
        order = MakerOrder(
            order_id=next(self._ids), venue=venue, pair=pair, symbol=symbol,
            side=side, price=price, size=size, remaining=size,
            queue_ahead=initial_q, initial_queue_ahead=initial_q,
            placed_ts=now, active_ts=active_ts, signal_id=signal_id,
            arrival_ts=active_ts if arrival_ts is None else arrival_ts,
            level_was_absent=level_was_absent, queue_unknown=queue_unknown,
            relative_to_touch_ticks=rel_ticks,
            local_depth=state.top_depth,
            local_depth_baseline=state.top_depth_baseline,
            spread_pct=state.spread_pct,
            short_vol=state.short_vol,
            trade_count=state.recent_trade_rate,
            trade_sign=state.trade_sign,
            regime_bucket=regime_bucket,
            observed_level_size=level_size,
            level_now=level_size,
        )
        self.orders[order.order_id] = order
        self.orders_placed += 1
        if self.config.queue_model == "cohort":
            from .queue_v2.cohort import LevelQueue
            key = self._lqk(order)
            lq = self._lq.get(key)
            if lq is None or not lq.has_own:
                lq = self._lq[key] = LevelQueue(initial_q, now, self.config.cohort_unknown_age_s)
            elif not level_was_absent:
                lq.rebase_external(level_size, now, self.config.cohort_age, self.config.cohort_hazard_theta, "placement")
            order.queue_ahead = order.initial_queue_ahead = lq.join(order.order_id, size)
        self._settle_queue(order)
        return order

    @staticmethod
    def _inside_captured_depth(book: OrderBook, side: Side, price: float) -> bool:
        """An absent price is covered by the capture when it lies at or above the deepest captured bid (for a bid) / at or
        below the deepest captured ask (for an ask): anything the capture would have shown there is genuinely not there."""
        lv = book.levels("bid" if side == Side.BID else "ask", 10_000)
        if not lv:
            return False
        deepest = lv[-1][0]
        return price >= deepest - 1e-12 if side == Side.BID else price <= deepest + 1e-12

    def _unknown_queue_draw(self, venue: str = "", price: float = 0.0, touch: float = 0.0, tick: float = 0.0) -> float:
        cfg = self.config
        if cfg.unknown_queue_dist == "fitted":
            if self._uq_fit is None:
                from .queue_v2.unknown_queue import UnknownQueueFit
                self._uq_fit = UnknownQueueFit.load(cfg.unknown_queue_fit)
            d = int(round(abs(price - touch) / tick)) if tick > 0 and touch > 0 else 1
            if not any(b["median"] is not None for b in self._uq_fit.venues.get(venue, {"buckets": []})["buckets"]):
                self.diag["unknown_queue_fit_missing_venue_used_floor"] = self.diag.get("unknown_queue_fit_missing_venue_used_floor", 0.0) + 1.0
                return cfg.unknown_queue_floor if cfg.unknown_queue_fallback < 0 else cfg.unknown_queue_fallback
            self._uq_draws += 1
            return max(cfg.unknown_queue_floor, self._uq_fit.draw(venue, d, cfg.unknown_queue_seed * 1_000_003 + self._uq_draws))
        if cfg.unknown_queue_dist == "floor" or cfg.unknown_queue_median <= 0.0:
            return cfg.unknown_queue_floor
        if self._unknown_rng is None:
            import random
            self._unknown_rng = random.Random(cfg.unknown_queue_seed)
        return max(cfg.unknown_queue_floor, cfg.unknown_queue_median * math.exp(cfg.unknown_queue_sigma * self._unknown_rng.gauss(0.0, 1.0)))

    def cancel(self, order_id: int, now: float) -> MakerOrder | None:
        order = self.orders.pop(order_id, None)
        if order is None:
            return None
        if self._lq:
            self._lq_drop(order)
        rec = self._lc_by_maker.get(order_id)
        if rec is not None:
            rec.cancelled_ts = now
        self._record_outcome(order, end_ts=now)
        self.orders_cancelled += 1
        return order

    def amend(
        self, order_id: int, new_price: float, new_size: float, *, book: OrderBook, state, regime_bucket: LambdaBucket, now: float,
        active_ts: float, tick_size: float, postonly: bool = True, arrival_ts: float | None = None,
    ) -> AmendResult:
        """Cancel/replace. The old order and its queue position are gone whatever happens next (the venue treats an amend as a
        cancel followed by a new order). The replacement is checked like any new passive order: with `postonly`, a price that
        would take liquidity against `book` (the exchange-time book at the amend's arrival, supplied by the caller) is rejected,
        and then nothing rests. The strategy in this backtester never amends, so this path has no effect on any result."""
        if self.config.queue_model == "cohort":
            raise NotImplementedError("amend is not supported with queue_model='cohort'")
        old = self.orders.get(order_id)
        if old is None:
            return AmendResult(None, False, False, "unknown_order")
        venue, pair, symbol, side, signal_id = old.venue, old.pair, old.symbol, old.side, old.signal_id
        self.cancel(order_id, now)
        self.amends_requested += 1
        if postonly and would_cross("buy" if side == Side.BID else "sell", new_price, book.best_bid, book.best_ask):
            self.amends_rejected_postonly += 1
            return AmendResult(None, True, True, "postonly_would_take_liquidity")
        new = self.place(venue, pair, symbol, side, new_price, new_size, book, state, regime_bucket, now, active_ts, tick_size,
                         signal_id=signal_id, arrival_ts=arrival_ts)
        return AmendResult(new, False, True, "")

    def cancel_all(self, now: float) -> list[MakerOrder]:
        out = []
        for oid in list(self.orders):
            o = self.cancel(oid, now)
            if o is not None:
                out.append(o)
        return out

    # ── explicit lifecycle: REJECTED (D-01) and UNKNOWN (D-02) ───────────────────────────────────────
    # Bookkeeping in the shared vocabulary of order_state_machine; it moves no fill, queue position or timer.

    def _lc_note(self, rec: LifecycleRecord, ts: float, state: str, source: str, reason: str = "") -> None:
        rec.history.append((ts, state, source, reason))
        self.lifecycle_counts[state] = self.lifecycle_counts.get(state, 0) + 1

    def lc_submit(self, inflight_id: int, venue: str, ts: float, source: str = "modelled") -> LifecycleRecord:
        rec = self.lifecycle.get(inflight_id)
        if rec is None:
            rec = self.lifecycle[inflight_id] = LifecycleRecord(inflight_id=inflight_id, venue=venue)
            self._lc_note(rec, ts, "submitted", source)
        return rec

    def lc_accept(self, inflight_id: int, order: MakerOrder, ts: float, source: str = "modelled") -> None:
        rec = self.lc_submit(inflight_id, order.venue, ts, source)
        rec.maker_id, rec.order, rec.state = order.order_id, order, "resting"
        self._lc_by_maker[order.order_id] = rec
        self._lc_note(rec, ts, "resting", source)

    def lc_amended(self, old_maker_id: int, new_order: MakerOrder, ts: float) -> None:
        """S0: a repriced quote is the same submission under a new maker id (it lost its queue place, not its identity)."""
        rec = self._lc_by_maker.pop(old_maker_id, None)
        if rec is None:
            return
        rec.maker_id, rec.order = new_order.order_id, new_order
        self._lc_by_maker[new_order.order_id] = rec
        self._lc_note(rec, ts, "resting", "modelled", "amended")

    def lc_unknown(self, inflight_id: int, since: float, until: float | None, source: str = "injected", reason: str = "ack_lost") -> None:
        """No answer to the submit by `since` (the ack timeout). Settled at `until` by a status query or a late ack; None = never within the data.
        Entered from SUBMITTED only (R4): an order that already carries evidence is not made unknown."""
        rec = self.lifecycle.get(inflight_id)
        if rec is None or rec.state == "rejected":
            return
        rec.unknown_since, rec.unknown_until = since, until
        self._lc_note(rec, since, "unknown", source, reason)

    def lc_reject(self, inflight_id: int, ts: float, reason: str, source: str = "modelled", venue: str = "") -> bool:
        """The venue refused the order. Returns True when it had already filled (D-06): the fill is a fact and is KEPT, the order stays
        PARTIALLY_FILLED and an alarm is counted once; False for an ordinary terminal REJECTED."""
        rec = self.lc_submit(inflight_id, venue, ts, source)
        filled = rec.order.filled_qty if rec.order is not None else 0.0
        if filled > 1e-12:
            if not rec.reject_after_fill:
                rec.reject_after_fill = True
                self._lc_note(rec, ts, "reject_after_fill_alarm", source, reason)
            return True
        rec.state, rec.reject_ts, rec.reject_reason = "rejected", ts, reason
        self._lc_note(rec, ts, "rejected", source, reason)
        return False

    def lifecycle_state(self, inflight_id: int, now: float | None = None) -> str | None:
        """State in the shared vocabulary at `now` (None = after everything recorded). UNKNOWN holds from unknown_since until unknown_until."""
        rec = self.lifecycle.get(inflight_id)
        if rec is None:
            return None
        t = float("inf") if now is None else now
        if rec.state == "rejected":
            return "rejected"
        if rec.unknown_since is not None and rec.unknown_since <= t and (rec.unknown_until is None or t < rec.unknown_until):
            return "unknown"
        o = rec.order
        if o is None:
            return "submitted"
        eps = 1e-9
        if o.filled_qty >= o.size - eps:
            return "filled"
        if rec.cancelled_ts is not None and rec.cancelled_ts <= t:
            return "cancelled"
        if o.cancel_requested_ts is not None and o.cancel_requested_ts <= t:
            return "cancel_requested"
        if o.filled_qty > eps:
            return "partially_filled"
        return "resting"

    def live_orders(self, venue: str | None = None) -> list[MakerOrder]:
        if venue is None:
            return list(self.orders.values())
        return [o for o in self.orders.values() if o.venue == venue]

    # ── Fill triggers ────────────────────────────────────────────────────────

    def _pre_arrival(self, order: MakerOrder, event_ts: float | None) -> bool:
        """True when the event executed at the exchange before the order got there (it can drain the queue, not fill)."""
        return (self.config.exchange_time_eligibility and event_ts is not None
                and event_ts + 1e-12 < order.arrival_ts)

    def _credit_time(self, now: float, event_ts: float | None) -> float:
        return event_ts if (event_ts is not None and self.config.dedupe_time_basis == "exchange") else now

    def on_trade(
        self, venue: str, price: float, volume: float, sign: int, now: float, fee_bps: float,
        is_synthetic: bool = False, event_ts: float | None = None,
    ) -> list[Fill]:
        """A public trade crossed the book. Fill any resting order it reached.

        `now` is when the trade reached us; `event_ts` (optional) is when it executed at the exchange, on the host clock.
        """
        cfg = self.config
        ct = self._credit_time(now, event_ts)
        self.diag["trade_events"] += 1
        self.diag["trade_volume_observed"] += volume
        gross_volume = volume
        confirm_ids: set[int] = set()
        if cfg.fill_evidence in _TRADE_CONFIRM_MODES:
            confirm_ids = self._confirming_orders(venue, price, sign, now, ct, event_ts)
        cx_ids: set[int] = set()
        cx_rec = None
        if cfg.opposite_cross_confirm_ms > 0.0:
            # BEFORE the netting early return below: a print netted against a depth credit is still the evidence a cross needs.
            cx_rec = [ct, event_ts, price, sign, gross_volume, gross_volume]
            self._cx_prints.setdefault(venue, deque()).append(cx_rec)
            cx_ids = self._cx_pending_hits(venue, price, sign, now, ct, event_ts)
        moved: dict | None = None                     # depletion_ledger: order id -> [head movement already applied by the matched decrease, credit taken while it rested]
        extras: dict[int, float] = {}                 # depletion_ledger: order id -> print volume still owed to its queue (matched volume minus the movement already applied)
        no_proof = False
        if cfg.dedupe_trade_depth:
            key = (venue, "bid" if sign < 0 else "ask", round(price, 8))
            ledger = cfg.depletion_ledger and not self._lq
            moved = {} if ledger else None
            trace = [] if (ledger or cfg.depletion_ledger or self.obs_enabled) else None
            credited = self._take("delta", key, ct, volume, moved_out=moved, trace=trace)
            volume -= credited
            if self._lq and credited > 1e-12:
                lq = self._lq.get(key)
                if lq is not None:
                    lq.reclassify(credited)                      # book-first: the decrease already applied was an execution at the head
                    for o in self.orders.values():
                        if o.venue == venue and o.is_live and self._lqk(o) == key:
                            self._cohort_refresh(o)              # the head moved: queue_ahead follows, also when the print is fully netted and returns below
            self.diag["trade_volume_credited_to_depth"] += credited
            self._add("trade", key, ct, volume)
            if credited > 1e-12 and trace:
                no_proof = self._ledger_note("print", credited, gross_volume, trace, ct)
            if moved:
                for oid, (applied, covered) in moved.items():
                    o = self.orders.get(oid)
                    if o is not None and o.is_live and covered - applied > 1e-12:
                        extras[oid] = covered - applied
            if volume <= 1e-12 and not confirm_ids and not cx_ids and not extras and not (cfg.fill_through_prints and self._has_through_orders(venue, price, sign, now)):
                self._cohort_check()
                return []
        if self.obs_enabled:
            self._obs_begin("trade", price=price, gross=gross_volume, credited=gross_volume - volume, net=volume, sign=sign, event_ts=event_ts,
                            ambiguous=bool(gross_volume - volume > 1e-12 or confirm_ids), synthetic=is_synthetic)
            if no_proof and cfg.depletion_ledger:
                self._obs_trig["netting_no_proof"] = True
        self.diag["trade_volume_net"] += volume
        fills: list[Fill] = []
        eligible, pre, through, confirmed, cx_orders = [], [], [], [], []
        for order in list(self.orders.values()):
            if order.venue != venue or not order.is_live or now + 1e-12 < order.active_ts:
                continue
            # sign=-1: sell aggressor hits bids. sign=+1: buy aggressor lifts asks.
            reaches = ((sign < 0 and order.side == Side.BID and price <= order.price + 1e-12)
                       or (sign > 0 and order.side == Side.ASK and price >= order.price - 1e-12))
            if not reaches:
                continue
            if order.order_id in cx_ids:
                cx_orders.append(order)             # waiting on this print since its cross: filled below, once, and not again by the ordinary rule
                continue
            if cfg.dedupe_trade_depth and abs(price - order.price) <= 1e-9 * max(1.0, abs(price)):
                order.trade_vol_since_obs += volume
            if self._pre_arrival(order, event_ts):
                pre.append(order)
            elif cfg.fill_through_prints and abs(price - order.price) > 1e-9 * max(1.0, abs(price)):
                through.append(order)
            elif order.order_id in confirm_ids:
                confirmed.append(order)
            else:
                eligible.append(order)
        spend = cfg.fill_through_prints and cfg.through_print_volume_limited      # owner 7 Nov: ONE print budget across the cross, through and ordinary rules (legacy budgets when off)
        for order in cx_orders:
            cap = cfg.opposite_cross_cap_to_print
            qty_cap = None
            if cap:
                qty_cap = min(order.remaining, cx_rec[5])
                if qty_cap <= 1e-12:
                    continue                                   # this print has been used up by an earlier fill: the order keeps waiting
            else:
                order.cross_state, order.cross_gap = None, False
                self._clear_ahead(order)
            self.diag.setdefault("fills_from_cross", 0.0)
            self.diag["cross_confirmed_after"] = self.diag.get("cross_confirmed_after", 0.0) + 1.0
            self._cx_note(order, "confirmed_after", print_ts=ct, print_price=price, print_volume=gross_volume)
            if self.obs_enabled:
                self._obs_trig["cross_confirm"] = {"direction": "print_after_cross", "cross_ts": order.cross_ts, "print_ts": ct, "print_price": price, "print_volume": gross_volume}
            if gross_volume + 1e-12 < order.remaining:
                self.diag["cross_print_smaller_than_fill"] = self.diag.get("cross_print_smaller_than_fill", 0.0) + 1.0
            f = self._fill_from(order, order.remaining, now, 1.0, "cross", fee_bps, is_synthetic, cause="trade_confirmed_cross_after", qty_cap=qty_cap)
            if f is not None:
                fills.append(f)
                if cap:
                    cx_rec[5] -= f.qty
                    if order.remaining <= 1e-12:
                        order.cross_state, order.cross_gap = None, False   # fully filled; a partial fill stays pending until its window ends
        print_left = max(0.0, gross_volume - sum(f.qty for f in fills)) if spend else gross_volume        # cross fills have spent part of the print
        through_budget = print_left
        for order in through:
            if self.obs_enabled:
                self._obs_qb[order.order_id] = (order.queue_ahead, gross_volume)
            self._clear_ahead(order)
            avail = order.remaining / max(cfg.trade_fill_share, 1e-12)
            if cfg.through_print_volume_limited:
                avail = min(avail, max(through_budget, 0.0))
            f = self._fill_from(order, avail, now, cfg.trade_fill_share, "trade", fee_bps, is_synthetic, cause="trade_through")
            if f is not None:
                if cfg.through_print_volume_limited and cfg.trade_fill_share > 0:
                    through_budget -= f.qty / cfg.trade_fill_share
                fills.append(f)
                self.diag["fills_from_through_print"] += f.qty
        if spend:
            print_left = max(0.0, through_budget)                                  # what the through fills left of the print
            volume = min(volume, print_left)                                       # the ordinary rule gets no more than that
        if self._lq and pre:
            seen = set()
            for order in pre:
                k = self._lqk(order)
                if k not in seen and k in self._lq:
                    seen.add(k)
                    self._lq[k].cancel_head_external(volume)
            for o in self.orders.values():
                if o.venue == venue and o.is_live and self._lqk(o) in seen:
                    self._cohort_refresh(o)                          # every order at a level whose head moved, not only the pre-arrival ones
        for order in pre:
            if self._lq and self._lqk(order) in self._lq:
                new = self._lq[self._lqk(order)].ahead(order.order_id)
                drained = max(0.0, order.queue_ahead - new)
                order.queue_ahead = new
            else:
                drained = min(order.queue_ahead, volume)
                order.queue_ahead -= drained
            self.diag["pre_arrival_event_drains"] += 1
            self.diag["pre_arrival_queue_drained"] += drained
        if confirmed:
            # The trade is the second publication of the execution that produced the decrease: it fills the order up to its
            # own (gross) size, not reduced by the credit the de-duplication gave the decrease.
            confirmed.sort(key=lambda o: (o.placed_ts, o.order_id))
            budget = min(gross_volume, print_left) if spend else gross_volume
            budget0 = budget
            for order in confirmed:
                if moved:
                    applied = moved.get(order.order_id, (0.0, 0.0))[0]
                    if applied > 1e-12:
                        budget = max(0.0, budget - applied)       # depletion_ledger: the head movement the decrease already applied is the first part of this print
                if self.obs_enabled:
                    self._obs_qb[order.order_id] = (order.queue_ahead, budget)
                cap = cfg.evidence_fill_fraction * order.front_decrease if cfg.fill_evidence == "trade_confirm+fractional" else None
                f = self._fill_from(order, budget, now, cfg.trade_fill_share, "trade", fee_bps, is_synthetic,
                                    cause="trade_after_front", qty_cap=cap)
                order.front_ts = None
                if f is not None:
                    fills.append(f)
                    self.diag["evidence_confirmed_fill_qty"] += f.qty
                    if cfg.trade_fill_share > 0:
                        budget -= f.qty / cfg.trade_fill_share
                    if budget <= 1e-12:
                        break
            if spend:
                volume = min(volume, max(0.0, print_left - (budget0 - budget)))    # the confirmed fills spent part of it too
        if self._lq and eligible:
            fills += self._cohort_trade(eligible, volume, now, fee_bps, is_synthetic)
            eligible = []
        share = cfg.share_trade_volume
        if share:
            eligible.sort(key=lambda o: (-o.price if o.side == Side.BID else o.price, o.placed_ts, o.order_id))
        remaining = volume
        for order in eligible:
            queue_before = order.queue_ahead
            extra = extras.get(order.order_id, 0.0)       # depletion_ledger: matched volume the earlier decrease did not move through this queue yet (0 on every legacy path)
            f = self._consume(order, (remaining if share else volume) + extra, now, cfg.trade_fill_share, "trade", fee_bps, is_synthetic, cause="trade")
            self.diag["trade_volume_to_queue_ahead"] += queue_before - order.queue_ahead
            if f is not None:
                fills.append(f)
                self.diag["trade_volume_to_fills"] += f.qty
            if share:
                absorbed = queue_before - order.queue_ahead
                if f is not None and cfg.trade_fill_share > 0:
                    absorbed += f.qty / cfg.trade_fill_share
                remaining -= max(0.0, absorbed - extra) if extra else absorbed       # the owed volume was already matched, it is not taken from the unmatched budget
                if remaining <= 1e-12 and not extras:
                    break
        if cx_rec is not None and cfg.opposite_cross_cap_to_print:
            used = sum(f.qty for f in fills if not (f.cause or "").startswith("trade_confirmed_cross"))
            cx_rec[5] = max(0.0, cx_rec[5] - used)           # the ordinary rules spent part of this print: a later cross cannot spend it again
        self._cohort_check()
        return fills

    def _has_through_orders(self, venue: str, price: float, sign: int, now: float) -> bool:
        """A live, active order of ours that this print went strictly through (a sell below our bid / a buy above our ask)."""
        for o in self.orders.values():
            if o.venue != venue or not o.is_live or now + 1e-12 < o.active_ts:
                continue
            if abs(price - o.price) <= 1e-9 * max(1.0, abs(price)):
                continue
            if (sign < 0 and o.side == Side.BID and price < o.price) or (sign > 0 and o.side == Side.ASK and price > o.price):
                return True
        return False

    def on_delta(
        self, venue: str, side: str, price: float, new_size: float, now: float,
        tick_size: float, fee_bps: float, event_ts: float | None = None,
    ) -> list[Fill]:
        """A depth event changed a level we rest on. A decrease that no trade explains advances our queue according to
        MakerConfig.cancel_model (see there): "all_ahead" counts only decreases below the lowest size seen since we joined
        and charges them in full to the queue ahead; the "proportional" family counts every decrease of the displayed level
        and charges the share that was ahead of us."""
        cfg = self.config
        ct = self._credit_time(now, event_ts)
        if self.own_overlay:
            self.print_overlay.pop((venue, "buy" if side.startswith("b") else "sell", price), None)
        if self.obs_enabled:
            self._obs_begin("delta", price=price, side=side, new_size=new_size, event_ts=event_ts, ambiguous=True)
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
        if cfg.queue_model == "cohort":              # a level is a price: a coarse tick estimate must never merge two adjacent levels into one queue
            at_level = [o for o in at_level if abs(o.price - price) <= 1e-9 * max(1.0, abs(price))]
        proportional = cfg.effective_cancel_model in ("proportional", "power", "behind_first", "ahead_first") or cfg.queue_model == "cohort"
        power_n = cfg.cancel_power_n if cfg.effective_cancel_model == "power" else 1.0
        if self._lq and at_level:
            self._cohort_ahead = {}
            self._cohort_rebase(at_level, new_size, now, "delta")
        if proportional:
            shrinks = {o.order_id: o.level_now - new_size for o in at_level if o.level_now > new_size}
        else:
            shrinks = {o.order_id: o.observed_level_size - new_size for o in at_level if o.observed_level_size > new_size}
        explained = 0.0
        credit = None                                  # depletion_ledger: the credit entry this decrease leaves for its print, filled with each order's applied movement below
        if shrinks:
            level_shrink = max(shrinks.values())
            self.diag["delta_shrink_events"] += 1
            self.diag["delta_shrink_total"] += level_shrink
            if cfg.dedupe_trade_depth:
                key = (venue, "bid" if want == Side.BID else "ask", round(price, 8))
                trace = [] if (cfg.depletion_ledger or self.obs_enabled) else None
                explained = self._take("trade", key, ct, level_shrink, trace=trace)
                credit = self._add("delta", key, ct, level_shrink - explained, ledger=cfg.depletion_ledger and not self._lq)
                if credit is not None and len(credit) < 3:
                    credit = None
                self.diag["delta_shrink_explained_by_trades"] += explained
                if explained > 1e-12 and trace:
                    if self._ledger_note("decrease", explained, level_shrink, trace, ct) and self.obs_enabled and cfg.depletion_ledger:
                        self._obs_trig["netting_no_proof"] = True
        for order in at_level:
            full = shrinks.get(order.order_id, 0.0)
            shrink = full - explained
            if shrink > 1e-12:
                queue_before = order.queue_ahead
                can_fill = not self._pre_arrival(order, event_ts)
                if can_fill and cfg.level_creator_trade_only and order.level_was_absent:
                    can_fill = False                                  # WP4: the level's creator fills by trades only
                    self.diag["level_creator_depth_fills_blocked"] = self.diag.get("level_creator_depth_fills_blocked", 0) + 1
                if proportional:
                    f = self._consume_proportional(order, shrink, order.level_now, now, cfg.delta_fill_share, "delta", fee_bps, can_fill,
                                                   power_n=power_n, cause="delta_vanish" if new_size <= 1e-12 else "delta_share",
                                                   credit_ts=ct, model=cfg.effective_cancel_model)
                else:
                    f = self._consume(order, shrink, now, cfg.delta_fill_share, "delta", fee_bps, can_fill=can_fill,
                                      cause="delta_vanish" if new_size <= 1e-12 else "delta_share", credit_ts=ct)
                self.diag["delta_queue_drained"] += queue_before - order.queue_ahead
                if not can_fill:
                    self.diag["pre_arrival_event_drains"] += 1
                    self.diag["pre_arrival_queue_drained"] += queue_before - order.queue_ahead
                if f is not None:
                    fills.append(f)
                if credit is not None:
                    credit[2][order.order_id] = queue_before - order.queue_ahead + (f.qty / max(cfg.delta_fill_share, 1e-12) if f is not None and f.cause != "trade_after_front" else 0.0)
            # Any depth event is a fresh observation of this level: trade volume printed before it is already reflected
            # in the size it reports, so it must not be credited against a later snapshot shrink.
            if credit is not None and order.order_id not in credit[2]:
                credit[2][order.order_id] = 0.0                  # it rested through this decrease and nothing of it moved this order
            order.trade_vol_since_obs = 0.0
            # observed_level_size is a running MINIMUM (the 'all_ahead' baseline): it never rises, so volume that joins
            # behind us cannot later be mistaken for queue ahead, but a cancel that is hidden by such growth is never
            # counted either. level_now below does follow growth and is what the proportional family uses.
            order.observed_level_size = min(order.observed_level_size, new_size)
            order.level_now = new_size
        if self._lq:
            keys = {self._lqk(o) for o in at_level}
            for order in self.orders.values():
                if order.venue == venue and order.is_live and self._lqk(order) in keys:
                    self._cohort_refresh(order)          # a rebase can move the queue ahead of an order whose own shrink was zero (explained, or seen before it was active)
            self._cohort_check()
        return fills

    def on_snapshot(
        self, venue: str, book: OrderBook, now: float, tick_size: float, fee_bps: float,
        event_ts: float | None = None,
    ) -> list[Fill]:
        """Level shrank between snapshots — weakest signal, smallest share.

        With dedupe_trade_depth, the part of the shrink that public trades at this price already accounted for (and that
        has not been matched to a delta) is not drained a second time."""
        cfg = self.config
        if cfg.ledger_epoch:
            self.reset_credits(venue)
        if self.own_overlay:
            for k in [k for k in self.print_overlay if k[0] == venue]:
                del self.print_overlay[k]
        if self.obs_enabled:
            self._obs_begin("snapshot", event_ts=event_ts, ambiguous=True)
        fills: list[Fill] = []
        if self._lq:
            self._cohort_ahead = {}
            groups: dict[tuple, list] = {}
            for o in self.orders.values():
                if o.venue == venue and o.is_live and now + 1e-12 >= o.active_ts:
                    groups.setdefault(self._lqk(o), []).append(o)
            for grp in groups.values():
                self._cohort_rebase(grp, book.level_size("bid" if grp[0].side == Side.BID else "ask", grp[0].price), now, "snapshot")
        cohort = bool(self._lq)
        for order in list(self.orders.values()):
            if order.venue != venue or not order.is_live or now + 1e-12 < order.active_ts:
                continue
            curr = self._level_size(book, order.side, order.price, tick_size)
            if (order.level_now if cohort else order.observed_level_size) > curr:
                shrink = (order.level_now if cohort else order.observed_level_size) - curr
                self.diag["snapshot_shrink_total"] += shrink
                if cfg.dedupe_trade_depth and cfg.snapshot_trade_explain:
                    explained = min(shrink, order.trade_vol_since_obs)
                    order.trade_vol_since_obs -= explained
                    shrink -= explained
                    self.diag["snapshot_shrink_explained_by_trades"] += explained
                    if explained > 1e-12 and (cfg.depletion_ledger or self.obs_enabled):
                        self.ledger_stats["snapshot_volume_netted"] = self.ledger_stats.get("snapshot_volume_netted", 0.0) + explained
                if shrink > 1e-12:
                    queue_before = order.queue_ahead
                    can_fill = not self._pre_arrival(order, event_ts)
                    if can_fill and cfg.level_creator_trade_only and order.level_was_absent:
                        can_fill = False
                        self.diag["level_creator_depth_fills_blocked"] = self.diag.get("level_creator_depth_fills_blocked", 0) + 1
                    f = self._consume(order, shrink, now, cfg.snapshot_fill_share, "snapshot", fee_bps, can_fill=can_fill, cause="snapshot",
                                      credit_ts=self._credit_time(now, event_ts))
                    self.diag["snapshot_queue_drained"] += queue_before - order.queue_ahead
                    if f is not None:
                        fills.append(f)
            # Same invariant as on_delta: never increase observed_level_size.
            order.observed_level_size = min(order.observed_level_size, curr)
            order.level_now = curr
        if cohort:
            for order in self.orders.values():
                if order.venue == venue and order.is_live:
                    self._cohort_refresh(order)
            self._cohort_check()
        return fills

    def on_opposite_cross(
        self, venue: str, book: OrderBook, now: float, fee_bps: float, event_ts: float | None = None,
    ) -> list[Fill]:
        """MakerConfig.opposite_cross_fills: fill every live order on `venue` whose price the opposite best quote has reached.

        Called after each book update. An event that executed at the exchange before the order arrived cannot fill it (the
        PostOnly check at arrival covers a book that was already crossed then); the book stays crossed until a later event,
        which is checked again."""
        if not self.config.opposite_cross_fills:
            return []
        if self.obs_enabled:
            self._obs_begin("opposite_cross", event_ts=event_ts, ambiguous=False)
        bb, ba = book.best_bid, book.best_ask          # 0.0 for an empty side. Only the opposite side is needed: the book's
        fills: list[Fill] = []                          # uncross may just have removed our own side's levels.
        for order in list(self.orders.values()):
            if order.venue != venue or not order.is_live or now + 1e-12 < order.active_ts:
                continue
            tol = 1e-9 * max(1.0, abs(order.price))
            crossed = (0.0 < ba <= order.price + tol) if order.side == Side.BID else (bb > 0.0 and bb >= order.price - tol)
            confirm_ms = self.config.opposite_cross_confirm_ms
            if confirm_ms > 0.0 and not crossed and order.cross_state is not None:
                # The book uncrossed: the next cross is a new episode with its own window. A wait that is still inside its window survives the
                # uncross (a print can come after the quote moved away); an expired wait is dropped now.
                if order.cross_state == "pending" and self._credit_time(now, event_ts) <= order.cross_deadline + 1e-12:
                    order.cross_gap = True
                else:
                    if order.cross_state == "pending":
                        self.diag["cross_dropped"] = self.diag.get("cross_dropped", 0.0) + 1.0
                        self._cx_note(order, "dropped")
                    order.cross_state, order.cross_gap = None, False
            if not crossed or self._pre_arrival(order, event_ts):
                continue
            cause = "opposite_cross"
            if confirm_ms > 0.0:
                ct = self._credit_time(now, event_ts)
                if order.cross_state == "pending":
                    if ct <= order.cross_deadline + 1e-12:
                        continue
                    self.diag["cross_dropped"] = self.diag.get("cross_dropped", 0.0) + 1.0
                    self._cx_note(order, "dropped")
                    if not order.cross_gap:
                        order.cross_state = "dropped"       # one unbroken crossing episode: its single window has passed
                        continue
                    order.cross_state, order.cross_gap = None, False   # the book uncrossed in between: this is a new episode
                elif order.cross_state == "dropped":
                    continue
                if self.config.opposite_cross_cap_to_print:
                    items = self._cx_lookback_all(order, ct, confirm_ms / 1000.0)
                    avail = sum(it[5] for it in items)
                    if avail <= 1e-12:
                        order.cross_state, order.cross_ts, order.cross_deadline, order.cross_gap = "pending", ct, ct + confirm_ms / 1000.0, False
                        self.diag["cross_armed"] = self.diag.get("cross_armed", 0.0) + 1.0
                        self._cx_note(order, "armed")
                        continue
                    qty = min(order.remaining, avail)
                    need = qty
                    for it in items:                           # oldest first; each print is spent once, whoever spends it
                        take = min(it[5], need)
                        it[5] -= take
                        need -= take
                        if need <= 1e-12:
                            break
                    order.cross_ts = ct
                    self._cx_note(order, "confirmed_before", print_ts=items[0][0], print_price=items[0][2], print_volume=avail, filled=qty)
                    self.diag["cross_confirmed_before"] = self.diag.get("cross_confirmed_before", 0.0) + 1.0
                    if avail + 1e-12 < order.remaining:
                        self.diag["cross_print_smaller_than_fill"] = self.diag.get("cross_print_smaller_than_fill", 0.0) + 1.0
                    if self.obs_enabled:
                        self._obs_trig["cross_confirm"] = {"direction": "print_before_cross", "cross_ts": ct, "print_ts": items[0][0], "print_price": items[0][2], "print_volume": avail}
                    self.diag.setdefault("fills_from_cross", 0.0)      # queue_ahead is left alone: a capped fill does not give the rest of the order the front of the queue
                    f = self._fill_from(order, order.remaining, now, 1.0, "cross", fee_bps, False, cause="trade_confirmed_cross_before", qty_cap=qty)
                    if f is not None:
                        fills.append(f)
                    if order.remaining > 1e-12:                # partly filled: wait out the window for the rest
                        order.cross_state, order.cross_deadline, order.cross_gap = "pending", ct + confirm_ms / 1000.0, False
                    else:
                        order.cross_state, order.cross_gap = None, False
                    continue
                prior = self._cx_lookback(order, ct, confirm_ms / 1000.0)
                if prior is None:
                    order.cross_state, order.cross_ts, order.cross_deadline = "pending", ct, ct + confirm_ms / 1000.0
                    self.diag["cross_armed"] = self.diag.get("cross_armed", 0.0) + 1.0
                    self._cx_note(order, "armed")
                    continue
                cause = "trade_confirmed_cross_before"
                order.cross_ts = ct
                self._cx_note(order, "confirmed_before", print_ts=prior[0], print_price=prior[2], print_volume=prior[4])
                self.diag["cross_confirmed_before"] = self.diag.get("cross_confirmed_before", 0.0) + 1.0
                if prior[4] + 1e-12 < order.remaining:
                    self.diag["cross_print_smaller_than_fill"] = self.diag.get("cross_print_smaller_than_fill", 0.0) + 1.0
                if self.obs_enabled:
                    self._obs_trig["cross_confirm"] = {"direction": "print_before_cross", "cross_ts": ct, "print_ts": prior[0], "print_price": prior[2], "print_volume": prior[4]}
                order.cross_state, order.cross_gap = None, False
            self._clear_ahead(order)
            self.diag.setdefault("fills_from_cross", 0.0)
            f = self._fill_from(order, order.remaining, now, 1.0, "cross", fee_bps, False, cause=cause)
            if f is not None:
                fills.append(f)
        self._cohort_check()
        return fills

    def _cx_note(self, order: MakerOrder, outcome: str, **kw) -> None:
        if len(self.cross_log) < 20000:
            self.cross_log.append({"venue": order.venue, "order_id": order.order_id, "price": order.price, "cross_ts": order.cross_ts, "outcome": outcome, **kw})

    @staticmethod
    def _cx_reaches(order: MakerOrder, price: float, sign: int) -> bool:
        return ((sign < 0 and order.side == Side.BID and price <= order.price + 1e-12)
                or (sign > 0 and order.side == Side.ASK and price >= order.price - 1e-12))

    def _cx_lookback(self, order: MakerOrder, ct: float, window_s: float):
        """Newest print on the order's venue within window_s BEFORE (or at) ct that reaches the order and executed after the order reached the exchange."""
        buf = self._cx_prints.get(order.venue)
        if not buf:
            return None
        for item in reversed(buf):
            if item[0] > ct + 1e-12:
                continue
            if ct - item[0] > window_s + 1e-12:
                break
            if self._cx_reaches(order, item[2], item[3]) and not self._pre_arrival(order, item[1]):
                return item
        return None

    def _cx_lookback_all(self, order: MakerOrder, ct: float, window_s: float) -> list:
        """opposite_cross_cap_to_print: every print within window_s before ct that reaches the order, executed after it arrived, and has volume left (oldest first)."""
        buf = self._cx_prints.get(order.venue)
        out = []
        if not buf:
            return out
        for item in reversed(buf):
            if item[0] > ct + 1e-12:
                continue
            if ct - item[0] > window_s + 1e-12:
                break
            if item[5] > 1e-12 and self._cx_reaches(order, item[2], item[3]) and not self._pre_arrival(order, item[1]):
                out.append(item)
        out.reverse()
        return out

    def _cx_pending_hits(self, venue: str, price: float, sign: int, now: float, ct: float, event_ts: float | None) -> set[int]:
        """Orders waiting on a print since their cross that this print reaches inside their window; expired waits are marked dropped."""
        out: set[int] = set()
        buf = self._cx_prints.get(venue)
        if buf:
            horizon = ct - 2.0 * self.config.opposite_cross_confirm_ms / 1000.0 - 1.0
            while buf and buf[0][0] < horizon:
                buf.popleft()
        for order in self.orders.values():
            if order.cross_state != "pending" or order.venue != venue or not order.is_live or now + 1e-12 < order.active_ts:
                continue
            if ct > order.cross_deadline + 1e-12:
                self.diag["cross_dropped"] = self.diag.get("cross_dropped", 0.0) + 1.0
                self._cx_note(order, "dropped")
                order.cross_state, order.cross_gap = (None, False) if order.cross_gap else ("dropped", False)
                continue
            if ct + 1e-12 >= order.cross_ts and self._cx_reaches(order, price, sign) and not self._pre_arrival(order, event_ts):
                out.add(order.order_id)
        return out

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
        is_synthetic: bool = False, can_fill: bool = True, cause: str | None = None, credit_ts: float | None = None,
    ) -> Fill | None:
        """Drain queue first, then fill our own size from what is left (if the event may fill this order). The whole
        `volume` is charged to the queue ahead of this order (the 'all_ahead' rule when called for a depth decrease)."""
        if volume <= 0 or not order.is_live:
            return None
        if self.obs_enabled:
            self._obs_qb[order.order_id] = (order.queue_ahead, volume)

        available = volume
        if self._lq and source != "trade" and order.order_id in self._cohort_ahead:
            drained = max(0.0, order.queue_ahead - self._cohort_ahead[order.order_id])
            order.queue_ahead -= drained
            available -= drained
            self._settle_queue(order)
        elif order.queue_ahead > 0:
            drained = min(order.queue_ahead, available)
            order.queue_ahead -= drained
            available -= drained
            self._settle_queue(order)
        if not can_fill:
            return None
        if source != "trade" and self.config.fill_evidence != "off":
            return self._decrease_with_evidence(order, volume, available, now, fill_share, source, fee_bps, cause, credit_ts)
        return self._fill_from(order, available, now, fill_share, source, fee_bps, is_synthetic, cause=cause)

    def _consume_proportional(
        self, order: MakerOrder, shrink: float, prev_level: float, now: float, fill_share: float, source: str, fee_bps: float,
        can_fill: bool, power_n: float = 1.0, cause: str | None = None, credit_ts: float | None = None, model: str = "power",
    ) -> Fill | None:
        """Unattributed depth decrease, split between the queue ahead of us and the volume behind us: only the share that
        was ahead advances us. With power_n == 1 the share is ahead / level_before ('proportional'); otherwise
        ahead**n / (ahead**n + behind**n) with behind = level_before - ahead.

        The feed carries no order ids, so we do not know who cancelled. A fill is possible only once no queue is left ahead."""
        if shrink <= 0 or not order.is_live:
            return None
        if self.obs_enabled:
            self._obs_qb[order.order_id] = (order.queue_ahead, shrink)
        ahead = order.queue_ahead
        if self._lq and order.order_id in self._cohort_ahead:
            drained = max(0.0, ahead - self._cohort_ahead[order.order_id])      # the cohort queue already moved; the common tail subtracts it
        elif model in ("behind_first", "ahead_first"):
            drained = cancel_ahead(model, shrink, ahead, max(prev_level - ahead, 0.0))
        else:
            if ahead <= 0:
                frac = 0.0
            elif power_n == 1.0:
                frac = min(1.0, ahead / max(prev_level, 1e-12))
            else:
                behind = max(prev_level - ahead, 0.0)
                frac = (ahead ** power_n) / (ahead ** power_n + behind ** power_n)
            drained = min(order.queue_ahead, shrink * frac)
            if ahead > 0:
                # every model stays inside max(0, C-B) <= C_ahead <= min(C, A); a proportional share is inside already and keeps its bits
                drained = clamp_feasible(shrink, ahead, max(prev_level - ahead, 0.0), drained)
        order.queue_ahead -= drained
        self._settle_queue(order)
        if cause == "delta_vanish" and can_fill and (self.config.vanish_clears_queue or self.config.fill_evidence in _WHOLE_RULE_MODES):
            self._clear_ahead(order)                             # the level is gone: everything that was ahead of us left with it
        if not can_fill or order.queue_ahead > 1e-12:
            return None
        if self.config.fill_evidence != "off":
            return self._decrease_with_evidence(order, shrink, shrink - drained, now, fill_share, source, fee_bps, cause, credit_ts)
        return self._fill_from(order, shrink - drained, now, fill_share, source, fee_bps, False, cause=cause)

    def _decrease_with_evidence(
        self, order: MakerOrder, decrease: float, available: float, now: float, fill_share: float, source: str, fee_bps: float,
        cause: str | None, credit_ts: float | None,
    ) -> Fill | None:
        """fill_evidence != "off": what an unexplained decrease (depth delta or snapshot shrink) does once it has been charged to
        the queue ahead. `decrease` is its size, `available` what is left of it after the queue ahead (0 when the queue is not
        yet exhausted). See MakerConfig.fill_evidence."""
        cfg = self.config
        if cfg.fill_evidence in _GATED_MODES:
            gated = cause == "delta_share" or (cause == "snapshot" and cfg.fill_evidence == "confirm_delta_snapshot")
            if not gated:                                           # vanished level, or a snapshot in confirm_delta: as "off"
                return self._fill_from(order, available, now, fill_share, source, fee_bps, False, cause=cause)
            if order.queue_ahead > 1e-12:
                return None
            order.front_ts = now if credit_ts is None else credit_ts
            order.front_decrease = decrease
            self.diag["evidence_front_marks"] += 1
            self.diag["evidence_suppressed_fill_qty"] += min(order.remaining, max(available, 0.0) * fill_share)
            return None
        vanish = cause == "delta_vanish"
        if vanish:
            self._clear_ahead(order)
        if order.queue_ahead > 1e-12:
            return None
        if vanish or cfg.fill_evidence in ("trade_confirm", "trade_confirm+fractional"):
            order.front_ts = now if credit_ts is None else credit_ts
            order.front_decrease = decrease
            self.diag["evidence_front_marks"] += 1
            self.diag["evidence_suppressed_fill_qty"] += min(order.remaining, max(available, 0.0) * fill_share)
            return None
        return self._fill_from(order, available, now, fill_share, source, fee_bps, False, cause=cause,
                               qty_cap=cfg.evidence_fill_fraction * decrease)

    def _settle_queue(self, order: MakerOrder) -> None:
        """queue_empty_threshold != "absolute": a queue at or below the threshold counts as empty and is set to 0."""
        mode = self.config.queue_empty_threshold
        if mode == "absolute" or order.queue_ahead <= 0.0:
            return
        thr = self.config.queue_empty_lot if mode == "lot" else self.config.queue_empty_frac * order.size
        if order.queue_ahead <= max(thr, 1e-12):
            self.diag["queue_empty_forgiven"] = self.diag.get("queue_empty_forgiven", 0.0) + order.queue_ahead
            self.diag["queue_empty_events"] = self.diag.get("queue_empty_events", 0.0) + 1.0
            self._clear_ahead(order)

    def _confirming_orders(self, venue: str, price: float, sign: int, now: float, ct: float, event_ts: float | None) -> set[int]:
        """Orders at the front of the queue whose latest unexplained decrease is within evidence_window_ms before this trade,
        resting at exactly the trade's price on the side it hits."""
        window = self.config.evidence_window_ms / 1000.0
        out: set[int] = set()
        for order in self.orders.values():
            if order.front_ts is None or order.venue != venue or not order.is_live or now + 1e-12 < order.active_ts:
                continue
            if not ((sign < 0 and order.side == Side.BID) or (sign > 0 and order.side == Side.ASK)):
                continue
            if abs(price - order.price) > 1e-9 * max(1.0, abs(price)) or self._pre_arrival(order, event_ts):
                continue
            if -1e-12 <= ct - order.front_ts <= window + 1e-12:
                out.add(order.order_id)
        return out

    def _fill_from(
        self, order: MakerOrder, available: float, now: float, fill_share: float, source: str, fee_bps: float,
        is_synthetic: bool, cause: str | None = None, qty_cap: float | None = None,
    ) -> Fill | None:
        if available <= 0 or fill_share <= 0:
            return None

        qty = min(order.remaining, available * fill_share)
        if qty_cap is not None:
            qty = min(qty, qty_cap)
        if qty <= 1e-12:
            return None

        order.remaining -= qty
        order.filled_qty += qty
        if self.own_overlay and source == "trade" and fill_share > 0:
            k = (order.venue, order.side_str, order.price)
            self.print_overlay[k] = self.print_overlay.get(k, 0.0) + qty / fill_share
        self.diag[f"fills_from_{source}"] += qty
        if order.first_fill_ts is None:
            order.first_fill_ts = now
            order.first_fill_is_synthetic = is_synthetic
        if is_synthetic:
            order.synthetic_fill_qty += qty
        elif source == "trade":
            order.trade_fill_qty += qty
        elif source == "delta":
            order.delta_fill_qty += qty
        elif source == "cross":
            order.cross_fill_qty += qty
        else:
            order.snapshot_fill_qty += qty

        notional = order.price * qty
        fee = notional * (fee_bps / 10_000.0)
        fill = Fill(
            ts=now, venue=order.venue, side=order.side_str, qty=qty, price=order.price,
            fee=fee, is_taker=False, order_id=order.order_id, signal_id=order.signal_id,
            is_synthetic=is_synthetic, cause=cause or source,
        )
        if self.obs_enabled:
            qb, offered = self._obs_qb.pop(order.order_id, (order.queue_ahead, None))
            trig = self._obs_trig
            flags = []
            if order.queue_unknown:
                flags += ["queue_unknown", "outside_captured_depth"]          # set exactly when the level was absent AND outside the captured depth
            elif order.level_was_absent and self.config.unknown_queue_model == "legacy":
                flags.append("queue_proxy")                                    # min_queue_ahead stands in for an absent level
            alternates = []                                                    # guide s4: where the association is ambiguous the alternate reading is KEPT with the fill, not only flagged
            if trig.get("ambiguous") or source in ("delta", "snapshot"):
                flags.append("ambiguous_trade_depth")                          # volume attributed to us without one causal print
            if trig.get("netting_no_proof"):
                flags.append("netting_without_proof")                          # depletion_ledger: the print / decrease behind this fill was matched to its twin by price and window alone
            if source in ("delta", "snapshot"):
                alternates.append({"scenario": "depletion_was_cancellation_not_execution", "own_fill_qty": 0.0,
                                   "basis": "a level decrease with no matched print is not proof of an execution; under this reading the order is not filled at this event"})
            if trig.get("competing_credits"):
                alternates.append({"scenario": "decrease_explained_by_a_different_pending_print", "competing_credits_age_s_qty": trig["competing_credits"],
                                   "basis": "several pending prints could explain this decrease; the engine consumed the oldest first (FIFO); another association changes which print is left over for later depletions"})
            fill.obs = {"cause": cause or source, "source": source, "trigger": dict(trig), "queue_before": qb, "queue_after": order.queue_ahead,
                        "queue_at_placement": order.initial_queue_ahead, "level_size_at_placement": order.observed_level_size,
                        "level_now": order.level_now, "volume_offered": offered if offered is not None else trig.get("gross"),
                        "volume_consumed_by_queue": (qb - order.queue_ahead) if qb is not None else None,
                        "volume_consumed_by_fill": qty / fill_share if fill_share > 0 else None,
                        "netting_credit": trig.get("credited", 0.0), "cancel_model": self.config.effective_cancel_model,
                        "fill_evidence": self.config.fill_evidence, "dedupe_trade_depth": bool(self.config.dedupe_trade_depth),
                        "flags": flags, "alternates": alternates, "association_robust": not alternates, "synthetic": is_synthetic, "remaining_after": order.remaining,
                        # step 9 provenance: model ids are labels of THIS model (never native exchange ids), the three clocks, the source type, the settings hash
                        "order_model_id": f"qv2:{order.venue}:{order.order_id}", "id_kind": "model_label_not_native",
                        "arrival_ts": order.arrival_ts, "match_ts": trig.get("event_ts"), "report_ts": now,
                        "source_type": "synthetic_flow" if is_synthetic else {"trade": "print", "delta": "depth_decrease", "snapshot": "snapshot_shrink",
                                                                              "cross": "opposite_cross"}.get(source, source),
                        "queue_model": self.config.queue_model, "cohort_age": self.config.cohort_age if self.config.queue_model == "cohort" else None,
                        "model_settings_sha256": self._settings_sha(),
                        # only a run that uses the hazard rule carries the theta and where it came from
                        **({"cohort_hazard_theta": {"value": self.config.cohort_hazard_theta, "provenance": HAZARD_THETA_PROVENANCE}}
                           if self.config.queue_model == "cohort" and self.config.cohort_age == "hazard" else {})}

        if not order.is_live:
            self.orders.pop(order.order_id, None)
            if self._lq:
                self._lq_drop(order)
            # Skip training label only when the FIRST fill was synthetic — that
            # corrupts the time-to-fill timestamp (first_fill_ts = synthetic event time).
            # A real trade that later completes a partially-synthetic order is still a
            # valid observation; excluding it was overly conservative.
            if not order.first_fill_is_synthetic:
                self._record_outcome(order)
            self.orders_filled += 1
        elif self._lq:
            lq = self._lq.get(self._lqk(order))
            if lq is not None:
                lq.sync_own(order.order_id, order.remaining)     # a part-filled order keeps its place with what is left of it (the orders behind it see that size ahead)
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

    def diagnostics(self) -> dict[str, float]:
        """Aggregate fill-accounting counters (observed vs modelled quantities, base-asset units)."""
        return {k: round(v, 9) for k, v in self.diag.items()}

    def stats(self) -> dict[str, float]:
        return {
            "maker_orders_placed": float(self.orders_placed),
            "maker_orders_filled": float(self.orders_filled),
            "maker_orders_cancelled": float(self.orders_cancelled),
            "maker_fill_rate": self.orders_filled / max(1, self.orders_placed),
        }
