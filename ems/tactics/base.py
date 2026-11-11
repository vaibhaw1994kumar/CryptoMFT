"""Tactic base: a small state machine that turns one intent into child orders through the engine's sender.

Phase 0: the price rule is the strategy's (the leg arrives priced); the tactic validates it against the intent's limits.
A tactic is the ONLY way an order is sent (ExecutionLayer.send)."""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..intent import ExecutionIntent, IntentError, Role


class Tactic:
    name = ""
    roles: tuple[str, ...] = ()
    passive = False                 # True: the child rests (PostOnly); False: it crosses (IOC-like)
    implemented = True
    escalates_to = ""

    def __init__(self, cfg) -> None:
        self.cfg = cfg              # ExecutionConfig

    def check_child(self, intent: ExecutionIntent, leg) -> None:
        """Risk checks that hold for every tactic; subclasses add their own and call super()."""
        if intent.role not in self.roles:
            raise IntentError(f"tactic {self.name} cannot work a {intent.role} intent")
        if leg.is_taker == self.passive:
            raise IntentError(f"tactic {self.name} {'rests' if self.passive else 'crosses'}; the child is "
                              f"{'a taker' if leg.is_taker else 'passive'}")
        if leg.side != intent.side:
            raise IntentError(f"child side {leg.side} differs from intent side {intent.side}")
        if leg.venue not in intent.venues:
            raise IntentError(f"child venue {leg.venue} not eligible for intent {intent.intent_id} ({intent.venues})")
        if leg.qty > intent.qty + 1e-9:
            raise IntentError(f"child qty {leg.qty} exceeds intent qty {intent.qty}")
        self.check_cap(intent, leg)

    def check_cap(self, intent: ExecutionIntent, leg) -> None:
        w = intent.worst_price
        if w is None:
            if self.cfg.price_cap_required and intent.role not in (Role.UNWIND, Role.FLATTEN):
                raise IntentError(f"intent {intent.intent_id} has no worst_price (price cap is mandatory)")
            return
        eps = 1e-9 * max(1.0, abs(w))
        if intent.side == "buy" and leg.limit_price > w + eps:
            raise IntentError(f"child limit {leg.limit_price} is above the intent's worst price {w}")
        if intent.side == "sell" and leg.limit_price < w - eps:
            raise IntentError(f"child limit {leg.limit_price} is below the intent's worst price {w}")


# ── tactic choice by market state (Gap F; the ONE tactic switch for every strategy: ExecutionConfig.tactic_choice, off by default, refused with the arb pairing maker_taker) ───────────────────────────────────────
VOLATILITY_BUCKETS = ("low", "normal", "high")
# every reason code choose_tactic can return, in the order its rules are tried
TACTIC_CHOICE_REASONS = (
    "BREAKER_FLATTEN", "BREAKER_FLATTEN_MARKET_NOT_ALLOWED", "BOOK_STALE_NO_AGGRESSION", "TIME_TO_LIMIT_FLATTEN", "TIME_TO_LIMIT_WIDE_SPREAD_CAPPED", "TIME_TO_LIMIT_MARKET_NOT_ALLOWED",
    "INVENTORY_AT_LIMIT_CAPPED", "SPREAD_WIDE_STAY_PASSIVE", "INVENTORY_PRESSURE_REDUCE", "INVENTORY_AGED", "HIGH_VOL_WITH_INVENTORY", "DEFAULT_PASSIVE",
    "LQ_WAIT_CHEAPER", "LQ_DEPTH_COVERS", "LQ_PARTIAL_RESIDUAL",              # CR-LQ: only with TacticChoiceConfig.liquidity_aware (default off)
)
# the only reasons that may ever produce MARKET_BANDED (owner 9 Nov): emergencies, never a normal exit and never a quote
MARKET_BANDED_REASONS = ("BREAKER_FLATTEN", "TIME_TO_LIMIT_FLATTEN")


@dataclass(frozen=True)
class MarketState:
    """What the choice reads. All of it is known at decision time; nothing here is a forecast.
    book_age_s          age of the book the order would be priced from, by the session's own age rule
    spread_bps          touch spread in basis points of the mid
    volatility_bucket   "low" | "normal" | "high" (the caller's bucketing of short-horizon volatility)
    inventory_age_s     how long the open inventory has been held (0 when flat)
    inventory_pressure  |position| divided by its limit (0 flat, 1 at the limit, above 1 over it)
    time_to_limit_s     seconds until a hard time limit by which the inventory must be flat (None: no such limit)
    breaker_flatten     the circuit breaker has ordered the position flat
    CR-LQ, all four OPTIONAL (None = unknown; None never changes an answer, and none is read unless TacticChoiceConfig.liquidity_aware):
    order_qty                      base quantity of the order the choice is for
    executable_depth_within_cap    base quantity on the opposite side inside the IOC price cap (core OrderBook.walk(side, qty, limit_price=cap).filled_qty, or the live book's equivalent)
    expected_fill_time_s           expected wait of a resting post-only order at the chosen price (queue ahead / traded rate at that level)
    expected_wait_cost_bps         expected adverse drift over expected_fill_time_s (a markout table by coin and book age), in basis points"""
    book_age_s: float
    spread_bps: float
    volatility_bucket: str
    inventory_age_s: float
    inventory_pressure: float
    time_to_limit_s: float | None = None
    breaker_flatten: bool = False
    order_qty: float | None = None
    executable_depth_within_cap: float | None = None
    expected_fill_time_s: float | None = None
    expected_wait_cost_bps: float | None = None


@dataclass(frozen=True)
class TacticChoiceConfig:
    """Thresholds of choose_tactic. EVERY value is ASSUMED (a starting point, not a fit): they are to be measured on full-lifecycle data before any profile switches the choice on."""
    max_book_age_s: float = 0.5               # an older book is never a basis for an aggressive order (the breaker's flatten excepted)
    flatten_lead_s: float = 20.0              # at or inside this time to the limit, inventory is worked down now
    pressure_soft: float = 0.6                # inventory worth reducing actively
    pressure_hard: float = 1.0                # at or over the inventory limit: a capped IOC, never a market order
    pressure_low: float = 0.25                # inventory that matters when volatility is high
    inventory_age_soft_s: float = 60.0        # inventory held this long is reduced
    wide_spread_bps: float = 10.0             # above this a soft reason does not cross the spread
    market_spread_max_bps: float = 30.0       # above this even an emergency uses a capped IOC, not a banded market order
    # CR-LQ (liquidity-aware choice; owner 11 Nov). Off by default: with False choose_tactic is exactly the choice above. No arb profile sets it.
    liquidity_aware: bool = False
    max_wait_s: float = 30.0                  # ASSUMED: a resting order expected to wait longer than this is not "cheaper than crossing"
    depth_margin: float = 1.0                 # ASSUMED: the book covers the order when executable depth >= order_qty * depth_margin (1.0 = exactly the order)
    taker_fee_bps: float = 5.5                # ASSUMED: crossing cost = spread_bps / 2 + taker_fee_bps - maker_fee_bps (the caller sets the venue's own fees)
    maker_fee_bps: float = 2.0                # ASSUMED: a rebate is a negative number


@dataclass(frozen=True)
class TacticChoice:
    tactic: str                               # PASSIVE | IOC_CAPPED | MARKET_BANDED
    reason: str                               # one of TACTIC_CHOICE_REASONS
    covered_qty: float | None = None          # CR-LQ_PARTIAL_RESIDUAL only: the part of the order the book covers inside the cap (IOC_CAPPED for this much)
    residual_qty: float | None = None         # CR-LQ_PARTIAL_RESIDUAL only: the rest, which the caller must decide on (rest passively, retry, or leave as risk)


def choose_tactic(state: MarketState, cfg: TacticChoiceConfig | None = None, *, allow_market_banded: bool = False) -> TacticChoice:
    """The ONE place the tactic is chosen from the market state. Pure: no clock, no book, no order is touched. The first matching rule wins:

      1. breaker ordered flat                                 -> MARKET_BANDED  BREAKER_FLATTEN               (IOC_CAPPED BREAKER_FLATTEN_MARKET_NOT_ALLOWED when market-banded is not allowed; the emergency path is not blocked by book age)
      2. book older than max_book_age_s                       -> PASSIVE        BOOK_STALE_NO_AGGRESSION      (an aggressive tactic needs a fresh book for its cap; the caller's own book gate still applies)
      3. inventory held and time to the limit <= flatten_lead -> MARKET_BANDED  TIME_TO_LIMIT_FLATTEN         (IOC_CAPPED TIME_TO_LIMIT_WIDE_SPREAD_CAPPED above market_spread_max_bps; IOC_CAPPED TIME_TO_LIMIT_MARKET_NOT_ALLOWED when not allowed)
      4. inventory pressure >= pressure_hard                  -> IOC_CAPPED     INVENTORY_AT_LIMIT_CAPPED     (a normal exit is capped, never a market order)
      5. spread wider than wide_spread_bps                    -> PASSIVE        SPREAD_WIDE_STAY_PASSIVE      (the rules below are soft: they never pay a wide spread)
      6. inventory pressure >= pressure_soft                  -> IOC_CAPPED     INVENTORY_PRESSURE_REDUCE
      7. inventory held >= inventory_age_soft_s               -> IOC_CAPPED     INVENTORY_AGED
      8. high volatility and pressure >= pressure_low         -> IOC_CAPPED     HIGH_VOL_WITH_INVENTORY
      9. otherwise                                            -> PASSIVE        DEFAULT_PASSIVE               (a quote)

    CR-LQ (cfg.liquidity_aware, default False; with False nothing below changes): the answer of rules 6 to 9 is then checked by _liquidity_adjusted against the book depth in the cap and the expected wait of a resting order, adding the reasons
    LQ_WAIT_CHEAPER, LQ_DEPTH_COVERS and LQ_PARTIAL_RESIDUAL (a covered part and a residual quantity on the TacticChoice). Rules 1 to 5 are untouched, so MARKET_BANDED stays emergency-only.

    MARKET_BANDED is possible only from rules 1 and 3 (MARKET_BANDED_REASONS) and only when `allow_market_banded` is True; ExecutionLayer.suggest_tactic passes True for the market-making pairing (maker_maker) and never for another.
    Raises ValueError on an input that is not a number, is negative where a duration or ratio cannot be, or names an unknown volatility bucket: a bad state is never answered with a tactic."""
    cfg = cfg or TacticChoiceConfig()
    if state.volatility_bucket not in VOLATILITY_BUCKETS:
        raise ValueError(f"volatility_bucket must be one of {VOLATILITY_BUCKETS}, not {state.volatility_bucket!r}")
    for name in ("book_age_s", "spread_bps", "inventory_age_s", "inventory_pressure"):
        v = getattr(state, name)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or math.isnan(v) or v < 0:
            raise ValueError(f"{name} must be a number >= 0, not {v!r}")
    ttl = state.time_to_limit_s
    if ttl is not None and (not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or math.isnan(ttl)):
        raise ValueError(f"time_to_limit_s must be a number or None, not {ttl!r}")
    if not isinstance(state.breaker_flatten, bool):
        raise ValueError(f"breaker_flatten must be a bool, not {state.breaker_flatten!r}")
    for name in ("order_qty", "executable_depth_within_cap", "expected_fill_time_s", "expected_wait_cost_bps"):
        v = getattr(state, name)
        if v is not None and (not isinstance(v, (int, float)) or isinstance(v, bool) or math.isnan(v) or v < 0):
            raise ValueError(f"{name} must be a number >= 0 or None, not {v!r}")
    if not isinstance(cfg.liquidity_aware, bool):
        raise ValueError(f"liquidity_aware must be a bool, not {cfg.liquidity_aware!r}")
    if cfg.liquidity_aware:
        for name in ("max_wait_s", "depth_margin", "taker_fee_bps", "maker_fee_bps"):
            v = getattr(cfg, name)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or math.isnan(v) or (name == "depth_margin" and v <= 0) or (name == "max_wait_s" and v < 0):
                raise ValueError(f"TacticChoiceConfig.{name} is not a usable number: {v!r}")
    held = state.inventory_pressure > 0
    if state.breaker_flatten:
        return TacticChoice("MARKET_BANDED", "BREAKER_FLATTEN") if allow_market_banded else TacticChoice("IOC_CAPPED", "BREAKER_FLATTEN_MARKET_NOT_ALLOWED")
    if state.book_age_s > cfg.max_book_age_s:
        return TacticChoice("PASSIVE", "BOOK_STALE_NO_AGGRESSION")
    if held and ttl is not None and ttl <= cfg.flatten_lead_s:
        if not allow_market_banded:
            return TacticChoice("IOC_CAPPED", "TIME_TO_LIMIT_MARKET_NOT_ALLOWED")
        if state.spread_bps > cfg.market_spread_max_bps:
            return TacticChoice("IOC_CAPPED", "TIME_TO_LIMIT_WIDE_SPREAD_CAPPED")
        return TacticChoice("MARKET_BANDED", "TIME_TO_LIMIT_FLATTEN")
    if state.inventory_pressure >= cfg.pressure_hard:
        return TacticChoice("IOC_CAPPED", "INVENTORY_AT_LIMIT_CAPPED")
    if state.spread_bps > cfg.wide_spread_bps:
        return TacticChoice("PASSIVE", "SPREAD_WIDE_STAY_PASSIVE")
    if state.inventory_pressure >= cfg.pressure_soft:
        soft = TacticChoice("IOC_CAPPED", "INVENTORY_PRESSURE_REDUCE")
    elif held and state.inventory_age_s >= cfg.inventory_age_soft_s:
        soft = TacticChoice("IOC_CAPPED", "INVENTORY_AGED")
    elif state.volatility_bucket == "high" and state.inventory_pressure >= cfg.pressure_low:
        soft = TacticChoice("IOC_CAPPED", "HIGH_VOL_WITH_INVENTORY")
    else:
        soft = TacticChoice("PASSIVE", "DEFAULT_PASSIVE")
    return _liquidity_adjusted(soft, state, cfg) if cfg.liquidity_aware else soft


def _liquidity_adjusted(soft: TacticChoice, state: MarketState, cfg: TacticChoiceConfig) -> TacticChoice:
    """CR-LQ: the soft rules' (6 to 9) answer, checked against what the book and the queue can do. Only called with cfg.liquidity_aware. A field that is None leaves `soft` as it is; PASSIVE and IOC_CAPPED are the only
    tactics that can come out (MARKET_BANDED is not reachable from a soft rule), and rules 1 to 5 never get here.
      expected wait is short enough and cheaper than crossing -> PASSIVE     LQ_WAIT_CHEAPER      (wait <= max_wait_s AND expected_wait_cost_bps < spread_bps / 2 + taker_fee_bps - maker_fee_bps)
      IOC_CAPPED and the book covers the order in the cap     -> IOC_CAPPED  LQ_DEPTH_COVERS      (executable depth / depth_margin >= order_qty)
      IOC_CAPPED and the book covers only part                -> IOC_CAPPED  LQ_PARTIAL_RESIDUAL  (covered_qty now, residual_qty left for the caller; with nothing covered: PASSIVE, covered_qty 0, residual_qty the whole order)
    A passive default is never turned into an aggressive order here: DEFAULT_PASSIVE is a quote."""
    wait, cost = state.expected_fill_time_s, state.expected_wait_cost_bps
    if wait is not None and cost is not None and wait <= cfg.max_wait_s and cost < state.spread_bps / 2.0 + cfg.taker_fee_bps - cfg.maker_fee_bps:
        return TacticChoice("PASSIVE", "LQ_WAIT_CHEAPER")
    depth, qty = state.executable_depth_within_cap, state.order_qty
    if soft.tactic != "IOC_CAPPED" or depth is None or qty is None:
        return soft
    usable = depth / cfg.depth_margin
    if usable >= qty:
        return TacticChoice("IOC_CAPPED", "LQ_DEPTH_COVERS")
    if usable <= 0:
        return TacticChoice("PASSIVE", "LQ_PARTIAL_RESIDUAL", covered_qty=0.0, residual_qty=qty)
    return TacticChoice("IOC_CAPPED", "LQ_PARTIAL_RESIDUAL", covered_qty=usable, residual_qty=qty - usable)
