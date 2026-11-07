from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Optional

from ..intent import IntentError, Role
from .base import Tactic

PLACEMENTS = ("touch", "improve", "deep")


# ── Quote repricing (S0, owner 7 Nov): one pure, venue-neutral rule for a RESTING passive quote ────────────────────────────────
# The rule decides; it never sends. The caller (the EMS in live, the Requoter of new_backtest in the simulator) computes the TARGET price
# (the strategy's edge recomputed at the CURRENT hedge-venue price, one place: ArbStrategy.requote_target), executes the answer, and tells the
# policy what happened. Every number is a setting the TRADER supplies (or the live bot's values, recorded as such): there are no defaults,
# nothing here is fitted, and a config that is not `enabled` never acts (HOLD "disabled").
#   adverse move   the target is below a resting bid (above a resting ask) by more than band_down_bps: act at once (subject to min_interval_ms);
#                  a quote whose target is gone (no edge at the current hedge price) is CANCELLED at once, whatever the interval
#   favourable     the target is above a resting bid (below an ask) by more than band_up_bps: act only when band_up_bps is set, the quote has no
#                  real queue ahead (queue_hold), no jump of the hedge price happened within debounce_ms (debounce), and min_interval_ms passed
#   age            a quote older than max_age_s is cancelled and re-quoted
#   amend_first    the first answer for a price change is AMEND (the venue keeps no queue priority: it is lost either way); a rejected amend is
#                  answered with CANCEL_REPLACE by `after_amend_rejected`, and only when the quote still has a target
HOLD, AMEND, CANCEL_REPLACE, CANCEL = "HOLD", "AMEND", "CANCEL_REPLACE", "CANCEL"
REPRICE_SOURCES = ("TRADER_SET", "LIVE_VALUES", "ILLUSTRATIVE_NOT_FITTED")


@dataclass(frozen=True)
class RepriceConfig:
    enabled: bool = False
    source: str = "TRADER_SET"                      # TRADER_SET | LIVE_VALUES | ILLUSTRATIVE_NOT_FITTED: said in every result that uses the rule
    band_down_bps: Optional[float] = None           # stickiness band for adverse moves (bps of the resting price)
    band_up_bps: Optional[float] = None             # None: favourable moves never move a quote
    debounce_ms: Optional[float] = None             # after a hedge-price jump, favourable reprices wait this long
    jump_bps: Optional[float] = None                # what counts as a jump (None: no jump detection, no debounce)
    min_interval_ms: Optional[float] = None         # least time between two reprice requests of one quote
    max_age_s: Optional[float] = None               # a quote older than this (since it was last placed or amended) is cancelled and re-quoted at the current target
    queue_hold: bool = True                         # an order with a real queue ahead keeps its place against favourable moves
    amend_first: bool = True                        # False: every price change is CANCEL_REPLACE

    def __post_init__(self) -> None:
        if self.source not in REPRICE_SOURCES:
            raise IntentError(f"reprice.source must be one of {REPRICE_SOURCES}, got {self.source!r}")
        for f in ("band_down_bps", "band_up_bps", "debounce_ms", "jump_bps", "min_interval_ms", "max_age_s"):
            v = getattr(self, f)
            if v is not None and not (v >= 0.0 and v == v and v != float("inf")):
                raise IntentError(f"reprice.{f} must be a finite number >= 0 or unset, got {v!r}")
        if self.enabled:
            missing = [f for f in ("band_down_bps", "min_interval_ms", "max_age_s") if getattr(self, f) is None]
            if missing:
                raise IntentError(f"reprice is enabled but {missing} are not set: the trader sets them, nothing is defaulted")
            if (self.debounce_ms is None) != (self.jump_bps is None):
                raise IntentError("reprice.debounce_ms and reprice.jump_bps are set together or not at all")

    @staticmethod
    def parse(value) -> "RepriceConfig":
        """The `execution.primary.reprice` setting: "none" (the default, no repricing) or a mapping of the fields above."""
        if value in (None, "none", False):
            return RepriceConfig()
        if not isinstance(value, dict):
            raise IntentError(f"execution.primary.reprice is 'none' or a mapping, got {value!r}")
        known = {f.name for f in fields(RepriceConfig)}
        bad = sorted(set(value) - known)
        if bad:
            raise IntentError(f"execution.primary.reprice: unknown keys {bad}; known: {sorted(known)}")
        return RepriceConfig(**value)

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass
class RepriceState:
    """What the policy remembers about ONE resting quote (times in seconds on one clock)."""
    quote_start_ts: float
    last_request_ts: Optional[float] = None
    pending: bool = False                           # an amend or cancel is in flight: the quote is still resting and fillable (catalogue H30)
    last_jump_ts: Optional[float] = None
    _last_hedge_px: Optional[float] = None
    reprices: int = 0
    holds: dict = None

    def __post_init__(self) -> None:
        self.holds = {}

    def observe_hedge(self, cfg: RepriceConfig, now: float, hedge_px: float) -> None:
        """Feed every hedge-venue touch change on the quote's side; a move of at least jump_bps since the previous one is a jump."""
        prev = self._last_hedge_px
        if prev and cfg.jump_bps is not None and abs(hedge_px / prev - 1.0) * 1e4 >= cfg.jump_bps:
            self.last_jump_ts = now
        self._last_hedge_px = hedge_px


@dataclass(frozen=True)
class RepriceDecision:
    action: str
    price: Optional[float]
    reason: str


def decide_reprice(cfg: RepriceConfig, side: str, resting_price: float, target_price: Optional[float], now: float, state: RepriceState,
                   queue_ahead: float = 0.0) -> RepriceDecision:
    """One decision for one resting quote. `target_price` is None when the edge no longer holds at the current hedge price (or a price on
    the grid would cross): the quote is then cancelled. Pure: the same inputs always give the same answer."""
    def hold(why: str) -> RepriceDecision:
        state.holds[why] = state.holds.get(why, 0) + 1
        return RepriceDecision(HOLD, None, why)
    if not cfg.enabled:
        return RepriceDecision(HOLD, None, "disabled")
    if state.pending:
        return hold("pending")
    buy = side == "buy"
    if target_price is None:
        return RepriceDecision(CANCEL, None, "no_edge_at_current_hedge_price")      # a quote with no edge is never held for its queue, and no interval delays a cancel
    interval_ok = state.last_request_ts is None or (now - state.last_request_ts) * 1e3 >= cfg.min_interval_ms
    first = AMEND if cfg.amend_first else CANCEL_REPLACE
    if now - state.quote_start_ts >= cfg.max_age_s:
        return hold("min_interval") if not interval_ok else RepriceDecision(CANCEL_REPLACE, target_price, "max_age")
    move_bps = (target_price - resting_price) / resting_price * 1e4
    adverse = (-move_bps if buy else move_bps)                  # > 0: the hedge venue moved against the resting quote
    if adverse > cfg.band_down_bps:
        return hold("min_interval") if not interval_ok else RepriceDecision(first, target_price, "adverse_move")
    favourable = -adverse
    if cfg.band_up_bps is not None and favourable > cfg.band_up_bps:
        if cfg.queue_hold and queue_ahead > 0.0:
            return hold("queue_hold")
        if cfg.debounce_ms is not None and state.last_jump_ts is not None and (now - state.last_jump_ts) * 1e3 < cfg.debounce_ms:
            return hold("debounce_after_jump")
        return hold("min_interval") if not interval_ok else RepriceDecision(first, target_price, "favourable_move")
    return hold("inside_band")


def after_amend_rejected(cfg: RepriceConfig, target_price: Optional[float], order_gone: bool) -> RepriceDecision:
    """The venue rejected an amend. If the order is gone (filled or cancelled) there is nothing to resend: the caller resyncs and treats a
    fill as a fill (OMS03: query, never assume). Otherwise cancel and place again, but only while the quote still has a target."""
    if order_gone:
        return RepriceDecision(HOLD, None, "order_gone_resync")
    if target_price is None:
        return RepriceDecision(CANCEL, None, "amend_rejected_no_target")
    return RepriceDecision(CANCEL_REPLACE, target_price, "amend_rejected")


def placement_price(placement: str, side: str, best_bid: float, best_ask: float, tick: float, edge_price: float,
                    deep_ticks: int | None = None) -> float:
    """Where a PostOnly order rests. `edge_price` is the price the conditional edge requires: a buy never rests above it, a sell
    never below it, so every placement keeps at least the edge the signal was gated on.
      touch   the best bid (buy) / best ask (sell)
      improve one tick inside the spread when the spread is wider than one tick, else the touch
      deep    the edge-required price; with `deep_ticks`, that many ticks behind the touch instead
    A price that would cross the book (>= best ask for a buy, <= best bid for a sell) falls back to the touch."""
    if placement not in PLACEMENTS:
        raise IntentError(f"unknown placement {placement!r}; known: {PLACEMENTS}")
    buy = side == "buy"
    touch = best_bid if buy else best_ask
    if placement == "touch":
        px = touch
    elif placement == "improve":
        wide = (best_ask - best_bid) > tick * (1.0 + 1e-9)
        px = (best_bid + tick if buy else best_ask - tick) if wide and tick > 0 else touch
    elif deep_ticks:
        px = best_bid - deep_ticks * tick if buy else best_ask + deep_ticks * tick
    else:
        px = edge_price
    px = min(px, edge_price) if buy else max(px, edge_price)
    if (buy and px >= best_ask) or (not buy and px <= best_bid):
        px = touch
    return px


class Passive(Tactic):
    """PostOnly limit at one of three placements (touch, improve, deep) — one tactic, one order type, one fill model, one lifetime and
    cancel/replace rule. Today both profiles use `deep`: the strategy rests at the price the edge requires. The intent's worst_price
    is that edge price, so a touch / improve child sits inside it."""
    name = "PASSIVE"
    roles = (Role.PRIMARY,)
    passive = True
    escalates_to = "IOC_CAPPED"

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self.placement = cfg.primary.get("placement", "deep")
        self.deep_ticks = cfg.primary.get("deep_ticks")
        if self.placement not in PLACEMENTS:
            raise IntentError(f"unknown placement {self.placement!r}; known: {PLACEMENTS}")
        self.reprice = RepriceConfig.parse(cfg.primary.get("reprice"))          # "none" (the default) or the trader's settings: the rule above

    @property
    def reprices(self) -> bool:
        """False for plain `deep`: the strategy's own price is sent unchanged (bit-identical to the pre-layer engine)."""
        return self.placement != "deep" or bool(self.deep_ticks)

    def price(self, side: str, best_bid: float, best_ask: float, tick: float, edge_price: float) -> float:
        return placement_price(self.placement, side, best_bid, best_ask, tick, edge_price, self.deep_ticks)
