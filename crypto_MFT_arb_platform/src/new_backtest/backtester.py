"""Cross-venue arbitrage replay engine.

Event loop, and the one ordering rule that makes it honest:

    for ev in receive_time_ordered_feed:
        clock.set_now(ev.receive_ts)
        release_in_flight_orders(now)      # fill against the book AS IT IS NOW
        apply_event_to_book(ev)
        if touch_changed: evaluate_strategy()

Orders are never filled at the price that triggered them. A signal computed at
t fires legs that arrive at t + entry_latency, and they execute against whatever
the book holds at arrival. When the edge has closed in that window the order
misses, or fills worse. That gap — measured per signal as expected vs realised
edge — is the difference between a cross-exchange arb backtest and a fantasy.

PnL is tracked as cash per venue plus base inventory per venue, marked at the
final mid. Attribution splits the result into:

    gross edge   what the matched legs actually earned
    fees         maker/taker, sign-correct (negative = rebate earned)
    slippage     realised fill VWAP vs the expected price
    leg risk     cost of flattening a signal where one leg filled and one missed
"""
from __future__ import annotations

import argparse
import json
import random
from collections import Counter, deque
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Iterable
import warnings

from . import virtual_clock
from .arb_strategy import (
    MAKER_TAKER, TAKER_TAKER, ArbConfig, ArbLeg, ArbSignal,
    CrossVenueArbStrategy, InventoryBook, PendingExposure, validate_maker_lifetime,
)
from .background_agent_model import AgentFlowConfig, BackgroundAgentModel
from .config import (
    BINANCE, BYBIT, DEFAULT_END_DATE, DEFAULT_LATENCY_PRESET, DEFAULT_START_DATE,
    LATENCY_PRESETS, VenueSpec, make_venues, max_position_for,
)
from .cross_venue_feed import CrossVenueFeed, check_coverage
from .event_loader import BOOK, DELTA, TRADE, delta_fields, snapshot_levels, trade_fields
from .fill_engine import Fill, MakerConfig, MakerExecutor, MakerOrder, TakerExecutor
from .fill_model import EmpiricalFillModel
from .circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from .hedge_faults import DisconnectGen, ExposureTracker, HedgeFaultConfig, Outage
from .margin_model import MarginModel, MarginModelConfig
from .timing import FixedOffsetAligner, Timelines, TimingConfig, feed_state
from .valuation import MidHistory, common_timestamp_valuation
from .latency_model import InFlightOrder, LatencyBook, MarkoutTracker, PendingOrderQueue
from .models import Side
from .order_book import OrderBook
from .order_events import OrderEventLog
from .postonly import AckLatencyConfig, AckLatencySource, would_cross
from .venue_state import VenueState, VenueStateConfig


CLOCK_OFFSET_WARN_MS = 5.0   # a venue whose receive times average this far before its exchange stamps is flagged


@dataclass
class ArbBacktestConfig:
    pair: str = "BTCUSDT"
    data_root: str = "data"
    venues: tuple[str, ...] = (BYBIT, BINANCE)
    start_date: str = DEFAULT_START_DATE
    end_date: str = DEFAULT_END_DATE
    max_events: int | None = None

    latency_preset: str = DEFAULT_LATENCY_PRESET
    bybit_fee_preset: str = "bybit_perp_mm"
    binance_fee_preset: str = "binance_perp_vip0"

    arb: ArbConfig = field(default_factory=ArbConfig)
    maker: MakerConfig = field(default_factory=MakerConfig)
    venue_state: VenueStateConfig = field(default_factory=VenueStateConfig)

    consume_liquidity: bool = True
    include_deltas: bool = True
    # How duplicate trade records are removed when several collector files overlap: "set" (legacy: any repeat of a
    # record within 3 s is dropped, which also drops genuine identical prints inside one file) or "multiset" (only
    # cross-file duplicates are dropped). Trade records have no id, so neither is exact; see event_loader._merge_sorted.
    trade_dedupe: str = "set"
    # True: the maker engine is given the level size left in the book after each depth event. False is a KNOWN DEFECT kept
    # only to reproduce results published before the fix: it hands the engine the raw `volume` field, which for the hybrid
    # collector format's 'cancel' events is the removed size, so level removals never drained any queue (top-5 book match
    # 68.8% vs 91.3% with the fix; see followup_report.html, decision 1, accepted 2026-11-01).
    delta_uses_book_size: bool = True
    seed: int = 7

    agent_flow: AgentFlowConfig = field(default_factory=AgentFlowConfig)

    output_dir: str | None = None
    warm_start_fill_events: str | None = None
    # If set, only load fill-log events whose label has matured by this epoch second.
    # Prevents temporal leakage when the calibration window overlaps the eval window.
    warm_start_until_ts: float | None = None
    # Path to a FillModelV1 JSON checkpoint.  When set, the strategy gates maker
    # quotes on P(fill) >= arb.min_fill_prob.  The model's trained_until_ts must
    # be <= epoch(start_date); the backtester raises ValueError otherwise to
    # prevent forward-looking model use.
    fill_model_v1_path: str | None = None
    markout_horizons_s: tuple[float, ...] = (1.0, 5.0, 30.0)
    progress_every: int = 0          # print every N events; 0 = silent
    max_trade_log: int = 200_000     # cap in-memory fill log on long runs
    # The data-quality preflight blocks the run when a venue's data is CRITICAL
    # (QualityGate REJECTED). A written justification here runs it anyway; the
    # justification and the gate decisions are recorded in the result.
    allow_rejected_data: str | None = None
    # Directory of funding-rate history JSONL files (e.g. data/funding/).
    # When set, perpetual funding payments are settled during replay and
    # reported as funding_paid in ArbBacktestResult.  None = not modelled.
    funding_rate_dir: str | None = None
    # Hedge-leg failure injection (hedge_faults.py). None = off and the replay is exactly as before.
    hedge_faults: HedgeFaultConfig | None = None
    # Opt-in margin / liquidation / cross-venue equity-transfer model (margin_model.py). None (default): infinite margin and no
    # transfers, exactly as before.
    margin: MarginModelConfig | None = None
    # End-of-run ledger identity check (docs/strategy_contract.md section 4): "raise" (default) or "warn".
    equity_bridge: str = "raise"
    # R11 — how the open position in ending_equity is marked. "common_timestamp" (default): every venue at the last time ALL
    # venues had data (valuation.py). "last_mark" (LEGACY_COMPAT): each venue at its own last mid — the pre-2026-11-01 figure,
    # which overstates a hedged pair when one feed stops early; the bridge then fails on such days (SOL 2022-01-05) unless
    # equity_bridge == "warn".
    valuation: str = "common_timestamp"
    # WP4 — book states. True: no signal while any venue's book status (valid | stale | invalid | recovering, order_book.status) is
    # not "valid", and a taker arrival on a non-valid book misses with reason "book_not_valid"; sequence ids in the feed (Binance
    # U/u/pu, Bybit seq) drive invalid/recovering. False (default, legacy): only the age-based staleness gate applies.
    enforce_book_state: bool = False
    # WP7 — product rules and release identity. product: "perp" | "spot" | "spot_perp" (venues[0] spot, venues[1] perp) | None
    # (legacy: inferred per venue from the fee preset family, mismatches only warn). product_fee_consistency: with a product set,
    # a fee preset of the wrong family is a startup ERROR. spot_requires_inventory: a spot sell needs base held on that venue
    # (REJECTED otherwise) unless borrow_enabled. release_identity: stamped by profiles.apply_profile, copied into every result.
    product: str | None = None
    product_fee_consistency: bool = False
    spot_requires_inventory: bool = False
    borrow_enabled: bool = False
    release_identity: dict | None = None
    # WP3 timing model (timing.py): separate sampled timelines per venue, measured host offset, timer wheel, equal-time policy,
    # dead-feed gate. None (default, legacy_v0): the single latency preset and the median clock aligner, exactly as before.
    timing: TimingConfig | None = None
    # Track the inventory path (peak / longest net exposure, equity drawdown) after every event; implied by hedge_faults.
    record_exposure: bool = False
    # PostOnly acceptance (postonly.py). "off" (default, legacy): every passive order that reaches the exchange rests. "on": at its
    # arrival the order is checked against the EXCHANGE-time book (the book just before the first event on the venue whose
    # exchange time is >= the arrival time); a buy at/above the best ask or a sell at/below the best bid is rejected: no fill, no
    # queue position, no taker conversion, and the strategy hears of it one more latency later.
    postonly_reject_model: str = "off"
    # How long after the exchange's decision the strategy learns of a reject. None: one more draw from the ack-latency source
    # (an assumption: the return leg is not in the real logs); a number: that many seconds, fixed.
    postonly_notice_s: float | None = None
    # Send-to-arrival time of passive orders: "preset" (the venue's entry latency, legacy), "empirical" (calibrated from real
    # orders) or "fixed". Independent of postonly_reject_model, with its own RNG stream.
    ack_latency: AckLatencyConfig = field(default_factory=AckLatencyConfig)
    # When a cancel takes effect: "receive" (legacy: at the first replay event whose RECEIVE time is >= the effective time, so a
    # fill that executed before the cancel landed but was delivered after it is dropped) or "exchange" (at the first event on
    # the venue whose EXCHANGE time is >= the effective time, so that fill is kept).
    cancel_time_basis: str = "receive"
    # Keep every order event (source-labelled) in memory / write order_events.jsonl. The per-(kind, source) counts are always kept.
    record_order_events: bool = False
    max_order_events: int = 2_000_000
    # Defect #6: with several live maker orders allowed, the legacy position check sees filled inventory only, so orders that all
    # fill can breach the limits. True (default): a backtest that allows more than one live order switches
    # ArbConfig.count_pending_exposure on. False: keep whatever the ArbConfig says (legacy). ArbConfig's own default, which
    # other callers of the strategy see, is NOT changed.
    auto_pending_exposure: bool = True
    # Task 2d (opt-in): raise ValueError if arb.max_position_base is the ArbConfig default (1.0) rather than explicitly chosen.
    # Message: "max_position_base not set: choose a size or use per_pair=True (batch_backtest.run_pair)".
    # Default False preserves old behaviour. Set True in new run scripts to prevent silent default usage.
    require_explicit_size: bool = False
    # Task 3 (opt-in): circuit breaker. None = disabled and replay is identical to before.
    circuit_breaker: CircuitBreakerConfig | None = None


@dataclass
class ArbBacktestResult:
    pair: str
    venues: list[str]
    start_date: str
    end_date: str
    mode: str
    latency_preset: str

    events_processed: int
    events_by_venue: dict[str, int]
    events_by_kind: dict[str, int]
    wall_seconds: float

    signals_generated: int
    signals_completed: int
    signals_fully_hedged: int
    signals_unhedged: int
    signals_both_missed: int
    orders_sent: int
    orders_filled: int
    orders_missed: int

    gross_pnl: float
    fees_paid: float
    unhedged_loss: float
    funding_paid: float
    # DEPRECATED: realized_pnl = gross_pnl - fees - unhedged_loss - funding is an attribution
    # decomposition that can diverge from the cash ledger (e.g. when unhedged_loss slippage is
    # measured vs the current mid, not the maker fill price). Use cash_pnl or ending_equity instead.
    realized_pnl: float          # DEPRECATED alias of attribution_pnl (kept for callers); see attribution_pnl below
    open_position_value: float   # A10: unrealised – base inventory marked at end-of-run mids
    ending_equity: float         # A10: total_cash + open_position_value (does not assume zero start); authoritative
    mark_to_market_pnl: float    # legacy alias == ending_equity; kept for back-compat

    # Gross-to-gross: what share of the spread visible at fire time did the
    # matched legs actually collect. Latency and slippage push it below 1.
    mean_expected_gross_edge_bps: float
    mean_realized_gross_edge_bps: float
    edge_capture_ratio: float
    # After the fees actually charged on the matched legs.
    mean_realized_net_edge_bps: float
    mean_slippage_bps: float

    final_inventory: dict[str, float]
    final_cash: dict[str, float]
    final_mids: dict[str, float]
    net_base_position: float
    traded_notional: float

    strategy_report: dict[str, float]
    feed_report: dict[str, dict[str, float]]
    latency_report: dict[str, dict[str, float]]
    book_stats: dict[str, dict[str, float]]
    maker_stats: dict[str, float]
    markout_summary: dict[str, dict[str, float]]
    miss_reasons: dict[str, int]
    coverage: dict[str, list[str]]

    fill_log_path: str | None = None
    bucket_stats_path: str | None = None
    trades_path: str | None = None
    # Preflight decision per venue, whether it blocked, and any recorded override.
    data_quality: dict | None = None
    # Task 5 — cash-and-inventory ledger fields (default 0.0 for back-compat with callers that construct this directly).
    # cash_pnl: sum of all fill cash_deltas (fees already deducted in cash_delta); authoritative when net_base == 0.
    # When net_base == 0: ending_equity == cash_pnl; any difference is an accounting divergence.
    # When net_base != 0: ending_equity == cash_pnl + open_position_value (the marked residual).
    cash_pnl: float = 0.0
    # common_timestamp_equity: inventory marked at the prices each venue published AT OR BEFORE
    # common_ts = min(last-update-ts across venues).  This is a TRUE common-timestamp mark that
    # never uses future observations; it differs from ending_equity when venues update at different
    # times.  Set to ending_equity and common_mid_staleness_s = inf when unavailable (no history
    # within range, or one venue had no price at all during the run).
    common_timestamp_equity: float = 0.0
    common_mid_staleness_s: float = 0.0   # max lag between venues' last-update ts; inf = unavailable
    # R11: the pre-2026-11-01 figure (each venue at its own last mid) kept as a diagnostic; valuation = the common-timestamp
    # marks actually used (common_ts, per-venue mark and its age, lagging-venue flag, stale_mark_inflation = last_mark - common).
    last_mark_equity: float = 0.0
    valuation: dict = field(default_factory=dict)
    # WP5: the attribution decomposition gross - fees - unhedged - funding, under its proper name. Headline P&L is the ledger
    # (cash_pnl / ending_equity at the common timestamp); this is a diagnostic. realized_pnl is the deprecated alias.
    attribution_pnl: float = 0.0
    # Funding settlements as applied: venue, time, position, mark (and its source), rate, payment (funding_settler.report()).
    funding_report: dict = field(default_factory=dict)
    # WP6: persistent alarms raised during the run (unwind exhausted / UNKNOWN unresolved); the last one still set at the end.
    alarms: list = field(default_factory=list)
    alarm_active: dict | None = None
    # WP7: what produced this result (profiles.release_identity) and the product per venue; spot results are mechanics only.
    release_identity: dict | None = None
    product_by_venue: dict = field(default_factory=dict)
    strategy_validation_label: str = ""
    # WP7b spot_perp: the hedged pair's value at the common-timestamp marks (perp mark - spot mark) x spot base.
    basis_component: float | None = None
    # single_venue_diagnostic: per-venue count of events where that venue's book exceeded max_book_age_s.
    # Non-zero means there were windows where the strategy was running with stale data on that side.
    # This is purely diagnostic; fills and cash movements that occurred during those windows are intact.
    single_venue_diagnostic: dict[str, int] = field(default_factory=dict)
    # single_venue_intervals: count of events where EXACTLY one venue had a usable (non-stale) book.
    # Distinguishes true single-venue windows from periods where both venues were simultaneously stale.
    # Non-zero signals that some events arrived with only one side's quote available.
    single_venue_intervals: int = 0
    # Maker-order lifetime / cancel accounting (all zero when maker_max_age_s == 0). See _expire_maker_orders.
    maker_lifetime: dict[str, float] = field(default_factory=dict)
    # Fill-accounting counters from MakerExecutor.diagnostics(): observed trade/depth quantities vs what was allocated.
    maker_diagnostics: dict[str, float] = field(default_factory=dict)
    # The fill-model settings this run used (cancel model and its parameters, snapshot netting, depth-size fix, ...).
    model_settings: dict = field(default_factory=dict)
    # Hedge-leg report: failure counters, the inventory path, per-signal cash flows. Empty unless hedge_faults or record_exposure is set.
    hedge_report: dict = field(default_factory=dict)
    # Order events and fills counted by kind and source ("historical" | "modelled" | "injected"); see order_events.py.
    order_event_summary: dict = field(default_factory=dict)
    # PostOnly model counters (checks, rejects by side, notice delays, fallbacks) and the ack-latency source in use.
    postonly_report: dict = field(default_factory=dict)
    # Margin model report (equity per venue, margin ratios, liquidations, transfers). Empty unless ArbBacktestConfig.margin is set.
    margin_report: dict = field(default_factory=dict)
    # Legging exposure (maker fill -> hedge fill) per resolved maker signal; see _legging_report.
    legging_report: dict = field(default_factory=dict)
    # WP3 timing report (timelines and their draw counts, clock, timer and feed-state counters). Empty unless timing is set.
    timing_report: dict = field(default_factory=dict)
    # Ledger identity check (cash from the trade log + funding + transfers vs ledger cash; equity vs cash + marked position).
    equity_bridge: dict = field(default_factory=dict)
    # Every fired signal with its naive and conditional edge (perp_perp_maker_taker_v1 logs both; legacy_v0 logs None).
    signal_log: list = field(default_factory=list)
    order_events_path: str | None = None


@dataclass
class _SignalTracker:
    """Resolves a signal once every leg has either filled or missed."""
    signal: ArbSignal
    legs_outstanding: int
    buy_qty: float = 0.0
    sell_qty: float = 0.0
    buy_notional: float = 0.0
    sell_notional: float = 0.0
    fees: float = 0.0
    missed: int = 0
    hedged_qty: float = 0.0   # cumulative qty for which a hedge leg was dispatched
    maker_resting: bool = False
    # Legging timestamps (MAKER_TAKER): when the passive leg first/last filled, when the first hedge was sent, when the last hedge
    # filled. hedge_fill_ts - maker_fill_ts is the legging exposure the strategy contract asks to be measurable.
    maker_first_fill_ts: float | None = None
    maker_last_fill_ts: float | None = None
    hedge_first_sent_ts: float | None = None
    hedge_last_fill_ts: float | None = None
    hedge_fill_qty: float = 0.0
    hedge_px_at_fill: float | None = None    # the hedge venue's executable touch at the first maker fill (vs the quote-time touch in the signal)
    managed: bool = False     # a hedge failure is being handled by HedgeFaultConfig.response: no instant, latency-free flatten


class EquityBridgeError(RuntimeError):
    """The cash/inventory ledger did not reconcile with the trade log at the end of a run (see _equity_bridge)."""


class DefaultModelWarning(UserWarning):
    """A backtest ran with MakerConfig.cancel_model unset, i.e. on the default 'proportional' without anyone choosing it."""


def cfg_delta_book_size(config: "ArbBacktestConfig") -> bool:
    return config.delta_uses_book_size


def _fee_preset_family(preset: str) -> str:
    """Return 'spot', 'perp', or 'unknown' from the preset name."""
    if "_spot_" in preset:
        return "spot"
    if "_perp_" in preset:
        return "perp"
    return "unknown"


_ARBCONFIG_DEFAULT_SIZE = ArbConfig().max_position_base   # 1.0; the sentinel for "not explicitly chosen"


class CrossVenueArbBacktester:
    def __init__(self, config: ArbBacktestConfig) -> None:
        self.config = config
        if config.require_explicit_size and config.arb.max_position_base == _ARBCONFIG_DEFAULT_SIZE:
            raise ValueError(
                "max_position_base not set: choose a size or use per_pair=True (batch_backtest.run_pair)"
            )
        validate_maker_lifetime(config.arb.maker_max_age_s)   # the arb config may have been mutated after construction
        self.pending_exposure_auto_enabled = False
        if config.auto_pending_exposure and config.arb.maker_max_live_orders > 1 and not config.arb.count_pending_exposure:
            config.arb.count_pending_exposure = True
            self.pending_exposure_auto_enabled = True
        if config.postonly_reject_model not in ("off", "on"):
            raise ValueError(f"postonly_reject_model must be 'off' or 'on', got {config.postonly_reject_model!r}")
        if config.cancel_time_basis not in ("receive", "exchange"):
            raise ValueError(f"cancel_time_basis must be 'receive' or 'exchange', got {config.cancel_time_basis!r}")
        if config.postonly_notice_s is not None and not config.postonly_notice_s >= 0.0:
            raise ValueError("postonly_notice_s must be >= 0 or None")
        bybit_family = _fee_preset_family(config.bybit_fee_preset)
        binance_family = _fee_preset_family(config.binance_fee_preset)
        # WP7: product per venue (from the fee preset family, or the declared product), checked at startup when asked to be.
        self.product_by_venue: dict[str, str] = {}
        for i, v in enumerate(config.venues):
            fam = _fee_preset_family(config.bybit_fee_preset if v == BYBIT else config.binance_fee_preset if v == BINANCE else "")
            self.product_by_venue[v] = fam
        if config.product is not None:
            if config.product not in ("perp", "spot", "spot_perp"):
                raise ValueError("product must be 'perp', 'spot' or 'spot_perp'")
            want = {v: ("spot" if config.product == "spot" or (config.product == "spot_perp" and i == 0) else "perp") for i, v in enumerate(config.venues)}
            for v, fam in self.product_by_venue.items():
                if fam != "unknown" and fam != want[v]:
                    msg = f"product {config.product}: venue {v} fee preset is {fam} but the product says {want[v]}"
                    if config.product_fee_consistency:
                        raise ValueError(msg)
                    warnings.warn(msg, stacklevel=2)
                self.product_by_venue[v] = want[v]
        elif config.product_fee_consistency and bybit_family != "unknown" and binance_family != "unknown" and bybit_family != binance_family:
            raise ValueError(f"fee preset family mismatch: bybit={config.bybit_fee_preset!r} ({bybit_family}) vs binance={config.binance_fee_preset!r} ({binance_family})")
        if bybit_family != "unknown" and binance_family != "unknown" and bybit_family != binance_family and config.product is None:
            warnings.warn(
                f"Fee preset family mismatch: bybit={config.bybit_fee_preset!r} ({bybit_family}) "
                f"vs binance={config.binance_fee_preset!r} ({binance_family}). "
                "Use matching product types (both spot or both perp) unless intentional.",
                stacklevel=2,
            )
        if config.arb.mode == "maker_taker" and config.maker.min_queue_ahead <= 0.0:
            warnings.warn(
                "MakerConfig.min_queue_ahead is 0 in maker_taker mode: a quote that joins a price level absent from "
                "the book starts at the front of the queue, so fill rates and edge are optimistic. Set it to a "
                "typical level depth for the instrument (--min-queue-ahead).",
                RuntimeWarning, stacklevel=2,
            )
        if config.arb.mode == "maker_taker" and config.arb.maker_max_age_s <= 0.0:
            warnings.warn(
                "ArbConfig.maker_max_age_s is 0 in maker_taker mode: a resting maker order is only ever removed by a "
                "fill, so one that stops being marketable keeps its place in the queue, blocks the live-order limit "
                "and can fill much later at a stale price. The engine has no cancel/replace on price moves "
                "(maker_requote_bps is not used); set maker_max_age_s as a time-in-force.",
                RuntimeWarning, stacklevel=2,
            )
        if (config.arb.mode == "maker_taker" and config.arb.maker_max_live_orders > 1
                and not config.arb.count_pending_exposure):
            warnings.warn(
                f"maker_max_live_orders={config.arb.maker_max_live_orders} with count_pending_exposure=False: position "
                "limits look at filled inventory only, so several resting or in-flight orders that all fill can exceed "
                "max_position_base / the venue imbalance cap. Set ArbConfig.count_pending_exposure=True.",
                RuntimeWarning, stacklevel=2,
            )
        if config.maker.cancel_model is None:
            warnings.warn(
                "MakerConfig.cancel_model is None: this run uses the default 'proportional'. Pass a model name explicitly "
                "('all_ahead', 'proportional' or 'power') to silence this warning.",
                DefaultModelWarning, stacklevel=2,
            )
        if config.arb.mode == "maker_taker" and not config.arb.maker_count_inflight:
            warnings.warn(
                "ArbConfig.maker_count_inflight is False in maker_taker mode: orders still in flight are not counted "
                "toward maker_max_live_orders, so one signal can be repeated until the first order lands (21 identical "
                "orders in the original audit). Legacy behaviour, kept for comparison only.",
                RuntimeWarning, stacklevel=2,
            )
        all_specs = make_venues(
            config.latency_preset, config.bybit_fee_preset, config.binance_fee_preset,
        )
        self.venue_specs: dict[str, VenueSpec] = {v: all_specs[v] for v in config.venues}

        self.latency = LatencyBook(
            {v: s.latency for v, s in self.venue_specs.items()}, seed=config.seed,
        )
        self.timing_cfg: TimingConfig | None = config.timing
        self.timelines: Timelines | None = None
        self.timing_stats: Counter[str] = Counter()
        self._feed_state: dict[str, str] = {}
        self._current_ets: float | None = None
        self._fill_kind: str | None = None          # WP5: "liquidation" | "flatten" while the margin model's fills are booked
        self._maker_fill_count = 0                  # r12: maker fills seen by the fault layer (restart_at_fills counts these)
        self._breaker_poll_ts = -1e18               # r12: throttle for the connectivity poll
        self._trip_cancelled = False                # r12: resting orders already cancelled for the current trip
        # WP6: a persistent ALARM (unwind attempts exhausted with exposure retained, or an UNKNOWN order never resolved) blocks new
        # risk until reset_alarm() — a manual action, never automatic.
        self.alarm: dict | None = None
        self.alarms: list[dict] = []
        self._funding_predicted = 0.0               # WP5: Σ signal notional x funding_holding_bps at fire time (predicted financing)
        self._market_first = config.timing is not None and config.timing.equal_time_policy == "market_first"
        # WP3 venue-truth books: per venue, the book as the EXCHANGE saw it at an arrival's exchange time, built from events the
        # feed has delivered or will deliver within the look-ahead window (receive order == exchange order within a venue, counted
        # when not). Taker arrivals execute against it; the strategy only ever reads self.books (receive time).
        self._truth_on = config.timing is not None and config.timing.truth_books
        self.truth_books: dict[str, OrderBook] = {}
        self._ahead: dict[str, deque] = {v: deque() for v in config.venues}
        self._ahead_mode = False
        if config.timing is not None:
            missing = [v for v in config.venues if v not in config.timing.venues]
            if missing:
                raise ValueError(f"timing: no timelines for venue(s) {missing}")
            self.timelines = Timelines(config.timing)
            self._install_timing()
            if self._truth_on:
                self.truth_books = {v: OrderBook(v, config.pair, self.venue_specs[v].delta_semantics) for v in config.venues}
        self.books: dict[str, OrderBook] = {
            v: OrderBook(v, config.pair, self.venue_specs[v].delta_semantics)
            for v in config.venues
        }
        self.states: dict[str, VenueState] = {
            v: VenueState(v, config.venue_state) for v in config.venues
        }
        self.tick_size: dict[str, float] = {v: 0.0 for v in config.venues}

        self.fill_model = EmpiricalFillModel()
        if config.warm_start_fill_events:
            self.fill_model.load_event_log(
                config.warm_start_fill_events,
                until_ts=config.warm_start_until_ts,
            )

        if config.arb.max_position_base <= 0:
            config.arb.max_position_base = max_position_for(config.pair)

        fill_model_v1 = None
        if config.fill_model_v1_path:
            from .fill_model_v1 import load_fill_model  # noqa: PLC0415
            import datetime as _dt                       # noqa: PLC0415
            fill_model_v1 = load_fill_model(config.fill_model_v1_path)
            # Leakage guard: model must not have been trained on data from the
            # eval window.  Convert start_date "YYYY-MM-DD" to midnight UTC.
            start_epoch = _dt.datetime.fromisoformat(config.start_date).replace(
                tzinfo=_dt.timezone.utc
            ).timestamp()
            if fill_model_v1.trained_until_ts > start_epoch:
                raise ValueError(
                    f"Fill model trained_until_ts={fill_model_v1.trained_until_ts:.0f} "
                    f"is after start_date epoch {start_epoch:.0f} ({config.start_date!r}). "
                    "Using a model trained on future data leaks labels into the backtest."
                )

        self.strategy = CrossVenueArbStrategy(
            config.pair, self.venue_specs, config.arb,
            fill_model_v1=fill_model_v1,
        )
        self.taker = TakerExecutor(consume_liquidity=config.consume_liquidity)
        self.maker = MakerExecutor(self.fill_model, config.maker)
        self.pending = PendingOrderQueue()
        self.markouts = MarkoutTracker(config.markout_horizons_s)
        self.inventory = InventoryBook()
        self.inventory.ensure(config.venues)

        self.settler: "FundingSettler | None" = None
        # WP7b: spot never pays funding — the settler only knows the perp venues (a spot-only run has no settler at all).
        funding_venues = tuple(v for v in config.venues if self.product_by_venue.get(v) != "spot")
        if config.funding_rate_dir and funding_venues:
            from .funding_settler import FundingSettler  # noqa: PLC0415
            self.settler = FundingSettler(
                config.funding_rate_dir,
                funding_venues,
                start_date=config.start_date,
                end_date=config.end_date,
            )

        self.agent = BackgroundAgentModel(config.agent_flow)
        # WP7b: only perp venues carry a margin account (a spot wallet holds inventory, not margin).
        self.margin_model: MarginModel | None = (MarginModel(config.margin, tuple(v for v in config.venues if self.product_by_venue.get(v) != "spot"))
                                                 if config.margin is not None else None)
        self._last_trade_ts: dict[str, float] = {}

        self._order_ids = 0
        self._trackers: dict[int, _SignalTracker] = {}
        self._maker_to_signal: dict[int, int] = {}
        # Exchange-side cancel has landed but the strategy cannot know yet: slot is released at effective_ts + feed delay.
        self._cancel_acks: list[float] = []
        self._cancel_watch: dict[int, "MakerOrder"] = {}   # orders with a cancel requested and not yet resolved
        self.lifetime: Counter[str] = Counter()
        self._last_touch: dict[str, tuple[float, float]] = {v: (0.0, 0.0) for v in config.venues}
        # Task 5: track (ts, mid) history per venue for true common-timestamp equity.
        # maxlen=32 covers staleness gaps of up to ~32 updates on the faster venue.
        self._last_mid_ts: dict[str, tuple[float, float]] = {v: (0.0, 0.0) for v in config.venues}
        if config.valuation not in ("common_timestamp", "last_mark"):
            raise ValueError('ArbBacktestConfig.valuation must be "common_timestamp" or "last_mark"')
        self._mid_history: dict[str, MidHistory] = {v: MidHistory() for v in config.venues}
        # Single-venue diagnostic: count events where one or more required venues had a stale book.
        self._stale_event_counts: Counter[str] = Counter()
        # Exactly-one-venue-usable count: incremented when some venues are stale and some are not.
        self._single_venue_event_count: int = 0

        self.events_processed = 0
        self.events_by_venue: Counter[str] = Counter()
        self.events_by_kind: Counter[str] = Counter()
        self.orders_sent = 0
        self.orders_filled = 0
        self.orders_missed = 0
        self.miss_reasons: Counter[str] = Counter()
        self.signals_completed = 0
        self.signals_fully_hedged = 0
        self.signals_unhedged = 0
        self.signals_both_missed = 0
        self.fees_paid = 0.0
        self.gross_pnl = 0.0
        self.unhedged_loss = 0.0
        self.traded_notional = 0.0
        self.expected_gross_sum = 0.0
        self.realized_gross_sum = 0.0
        self.realized_net_sum = 0.0
        self.edge_samples = 0
        self.slippage_sum = 0.0
        self.slippage_count = 0
        self.trade_log: list[dict] = []

        # Task 3 (opt-in): circuit breaker. None = disabled; no extra state when off.
        cb_cfg = config.circuit_breaker
        self._breaker: CircuitBreaker | None = (
            CircuitBreaker(cb_cfg, config.venues[0], config.venues[1]) if cb_cfg is not None else None
        )

        # Hedge-failure injection and hedge accounting. The counters are always kept; the injector only when configured.
        self.faults: HedgeFaultConfig | None = config.hedge_faults
        if self.faults is not None:
            for o in self.faults.outages:
                if o.venue not in config.venues:
                    raise ValueError(f"outage on {o.venue!r}, which is not one of the venues {config.venues}")
        self._fault_rng = random.Random(self.faults.seed) if self.faults is not None else None
        self._dyn_outages: list[Outage] = []
        self._now = 0.0
        self.hedge: Counter[str] = Counter()
        self.legging_rows: list[dict] = []          # one row per resolved MAKER_TAKER signal with a maker fill (see _legging_report)
        self.signal_log: list[dict] = []            # every fired signal with its naive and conditional edge (capped like the trade log)
        self.hedge_events: list[dict] = []
        self._hedge_open: dict[int, dict] = {}
        self.exposure: ExposureTracker | None = ExposureTracker() if (config.record_exposure or self.faults is not None) else None

        # Order events (always counted by kind and source; kept only on request), the ack-latency source and the PostOnly model.
        self.events = OrderEventLog(keep=config.record_order_events, cap=config.max_order_events)
        self.ack = AckLatencySource(config.ack_latency)
        self.po: Counter[str] = Counter()
        # Passive orders that have reached the exchange (by arrival time) and wait for the first venue event whose exchange time
        # is >= that arrival, to be checked against the book just before it.
        self._po_wait: dict[str, list[InFlightOrder]] = {v: [] for v in config.venues}
        # Random phenomena added by the strategy-level fault layer draw from their own streams (see hedge_faults.py).
        fseed = self.faults.seed if self.faults is not None else 0
        self._rng_maker = random.Random(fseed + 104_729)
        self._rng_extra = random.Random(fseed + 1_299_709)
        self._disc_gens: list[DisconnectGen] = []
        if self.faults is not None:
            for i, spec in enumerate(self.faults.disconnects):
                if spec.venue not in config.venues:
                    raise ValueError(f"disconnect spec on {spec.venue!r}, which is not one of the venues {config.venues}")
                self._disc_gens.append(DisconnectGen(spec, random.Random(fseed + 15_485_863 + 7919 * i)))
        self._maker_unknown_until: dict[int, float] = {}       # maker order id -> when its unknown state is resolved
        self._mid_hist: dict[str, deque] = {v: deque() for v in config.venues}
        self._track_mids = bool(self.faults is not None and self.faults.fast_move_reject_prob and self.faults.fast_move_bps)

    # ── Event application ────────────────────────────────────────────────────

    def _event_host_ts(self, ev) -> float | None:
        """When the event executed at the exchange, on the host clock; None when unknown (no exchange time, or the clock
        aligner has no sample yet). Callers then fall back to receive time."""
        ex = getattr(ev, "exchange_ts", None)
        if ex is None or not self.latency.aligner.ready:
            return None
        return self.latency.aligner.to_host(ex)

    def _apply_event(self, ev) -> None:
        book = self.books[ev.venue]
        state = self.states[ev.venue]
        spec = self.venue_specs[ev.venue]
        now = ev.receive_ts
        ets = self._event_host_ts(ev)
        self._current_ets = ets
        if self._truth_on:
            self._truth_sync_event(ev)

        # Exchange-time rules run on the book as it stands BEFORE this event, which the exchange handled at time `ets`.
        if self._po_wait[ev.venue]:
            self._resolve_postonly(ev.venue, ets, now)
        if self.config.cancel_time_basis == "exchange" and self._cancel_watch:
            self._apply_due_cancels(ev.venue, ets, now)

        if ev.kind == BOOK:
            bids, asks = snapshot_levels(ev.payload)
            if not bids or not asks:
                return
            if self.tick_size[ev.venue] <= 0:
                self.tick_size[ev.venue] = _infer_tick(bids, asks)
            book.apply_snapshot(bids, asks, now)
            self._record_mark(ev.venue, now)
            state.on_book(book, now)
            self.maker.mark_adverse(ev.venue, book, now, self.tick_size[ev.venue])
            for f in self.maker.on_snapshot(
                ev.venue, book, now, self.tick_size[ev.venue], spec.fees.maker_bps, event_ts=ets
            ):
                self._on_fill(f)

        elif ev.kind == DELTA:
            side, price, volume, event_type = delta_fields(ev.payload)
            if price <= 0:
                return
            self._feed_sequence(book, ev.payload, now)
            book.apply_delta(side, price, volume, event_type, now)
            self._record_mark(ev.venue, now)
            state.on_book(book, now)
            # The maker engine needs the level's NEW size. `volume` is that only for absolute feeds; in the hybrid
            # collector format a 'cancel' carries the size that was removed, so ask the book what is left.
            new_size = book.level_size(side, price) if cfg_delta_book_size(self.config) else volume
            for f in self.maker.on_delta(
                ev.venue, side, price, new_size, now,
                self.tick_size[ev.venue], spec.fees.maker_bps, event_ts=ets,
            ):
                self._on_fill(f)

        elif ev.kind == TRADE:
            price, volume, sign = trade_fields(ev.payload)
            if price <= 0 or volume <= 0:
                return
            # Inject synthetic background taker flow in the gap since the last
            # real trade on this venue.  Gate on live orders: with nothing
            # resting the synthetic trades have no effect on queue state.
            if (self.agent.config.enabled
                    and book.ready
                    and self.maker.live_orders(ev.venue)):
                last = self._last_trade_ts.get(ev.venue, now)
                if last < now:
                    for syn in self.agent.generate(
                        last, now, book.best_bid, book.best_ask,
                    ):
                        for f in self.maker.on_trade(
                            ev.venue, syn.price, syn.volume, syn.sign,
                            syn.timestamp, spec.fees.maker_bps, is_synthetic=True, event_ts=syn.timestamp,
                        ):
                            self._on_fill(f)
            self._last_trade_ts[ev.venue] = now
            book.on_trade(price, volume, sign, now)
            state.on_trade(now, price, volume, sign)
            for f in self.maker.on_trade(
                ev.venue, price, volume, sign, now, spec.fees.maker_bps, event_ts=ets
            ):
                self._on_fill(f)

        if self._track_mids and book.ready:
            hist = self._mid_hist[ev.venue]
            hist.append((now, book.mid))
            horizon = now - 2.0 * self.faults.fast_move_window_s
            while len(hist) > 1 and hist[1][0] < horizon:
                hist.popleft()

    def _touch_changed(self, venue: str) -> bool:
        book = self.books[venue]
        if not book.ready:
            return False
        touch = (book.best_bid, book.best_ask)
        if touch != self._last_touch[venue]:
            self._last_touch[venue] = touch
            return True
        return False

    def _record_mid_ts(self, venue: str, now: float) -> None:
        """Track per-venue (ts, mid) for common_timestamp_equity and staleness diagnostics."""
        book = self.books.get(venue)
        if book is not None and book.ready:
            self._last_mid_ts[venue] = (now, book.mid)
        # Single-venue diagnostic: accumulate stale-book event counts across all venues.
        max_age = self.config.arb.max_book_age_s
        n_stale = 0
        for v, b in self.books.items():
            if b.is_stale(now, max_age):
                self._stale_event_counts[v] += 1
                n_stale += 1
        # Count events where EXACTLY some (but not all) venues are stale — true single-venue windows.
        if 0 < n_stale < len(self.books):
            self._single_venue_event_count += 1

    def _record_mark(self, venue: str, now: float) -> None:
        """R11: (ts, mid) marks for the common-timestamp valuation, recorded on every book-changing event (also when the
        backtester is driven by hand), pruned to the other venues' last update."""
        book = self.books[venue]
        if not book.ready:
            return
        self._mid_history[venue].record(now, book.mid)
        floor = min((h.latest[0] for h in self._mid_history.values() if h.latest is not None), default=None)
        if floor is not None:
            for h in self._mid_history.values():
                h.prune(floor)

    @staticmethod
    def _feed_sequence(book: OrderBook, p: dict, now: float) -> None:
        """WP4: hand update ids to the book when the record carries them (Binance U/u/pu or first/last_update_id, Bybit seq)."""
        last = p.get("u", p.get("last_update_id", p.get("seq")))
        if last is None:
            return
        first = p.get("U", p.get("first_update_id"))
        prev = p.get("pu", p.get("prev_update_id"))
        try:
            book.apply_sequence(int(first) if first is not None else None, int(last), int(prev) if prev is not None else None, now)
        except (TypeError, ValueError):
            return

    def _book_state_blocks(self, now: float) -> bool:
        """WP4 (enforce_book_state): entry is blocked while any book is not valid; counted by status."""
        blocked = False
        for v, b in self.books.items():
            st = b.status(now, self.config.arb.max_book_age_s)
            if st != "valid":
                self.hedge[f"signals_blocked_by_book_{st}_{v}"] = self.hedge.get(f"signals_blocked_by_book_{st}_{v}", 0) + 1
                blocked = True
        if blocked:
            self.hedge["signals_blocked_by_book_state"] = self.hedge.get("signals_blocked_by_book_state", 0) + 1
        return blocked

    def _common_valuation(self) -> dict:
        return common_timestamp_valuation(self._mid_history, self.inventory.base, self.inventory.total_cash, self.config.venues,
                                          max_staleness_s=self.config.arb.max_common_staleness_s,
                                          mark_venue=self.config.arb.mark_inventory_venue)

    # ── Order dispatch ───────────────────────────────────────────────────────

    def _send_leg(self, leg: ArbLeg, signal: ArbSignal, now: float, *, is_hedge: bool = False, role: str = "",
                  attempt: int = 0, origin_id: int = 0) -> None:
        self._order_ids += 1
        flight = self.latency.entry_delay(leg.venue)           # always drawn, so the latency stream is the same with every switch
        if self.timelines is not None:
            flight = self.timelines.sample_s(leg.venue, "entry")   # WP3: one entry timeline for taker and passive legs; no ack-source draw
        from_ack_source = not leg.is_taker and not role and self.ack.active and self.timelines is None
        if from_ack_source:
            flight = self.ack.sample_s()                       # passive order: send-to-arrival from the calibrated / fixed source (it has its own scale)
        elif self.faults is not None and self.faults.latency_scale != 1.0:
            flight *= self.faults.latency_scale
        order = InFlightOrder(
            release_ts=now + flight,
            order_id=self._order_ids,
            venue=leg.venue,
            side=leg.side,
            qty=leg.qty,
            limit_price=leg.limit_price,
            is_taker=leg.is_taker,
            sent_ts=now,
            signal_id=signal.signal_id,
            expected_price=leg.expected_price,
            expected_edge_bps=signal.net_edge_bps,
            is_hedge=is_hedge, role=role, attempt=attempt, origin_id=origin_id or self._order_ids,
        )
        if is_hedge:
            self.hedge["hedges_sent"] += 1
        self.events.emit(now, "order_sent", leg.venue, source="modelled", order_id=order.order_id, signal_id=signal.signal_id,
                         chain_id=self._chain_of(order), side=leg.side, qty=leg.qty, price=leg.limit_price, reason=role or ("hedge" if is_hedge else ""))
        if self.faults is not None and (is_hedge or role):
            if self._inject_send_faults(order, now):
                return
        elif self.faults is not None and not leg.is_taker:
            if self._inject_maker_send_faults(order, now):
                return
        self.pending.enqueue(order)
        self.orders_sent += 1

    @staticmethod
    def _chain_of(order: InFlightOrder) -> int:
        """The injected fault an order is a consequence of: retries and unwinds belong to the chain of the hedge that failed."""
        return order.origin_id if (order.attempt > 0 or order.role == "unwind") and order.origin_id != order.order_id else 0

    def _spot_inventory_blocks(self, signal: ArbSignal) -> bool:
        """WP7b: a spot sell needs the base on that venue (the venue would reject an implicit short) unless borrowing is enabled."""
        if not self.config.spot_requires_inventory or self.config.borrow_enabled:
            return False
        sells = [(leg.venue, leg.qty) for leg in signal.legs if leg.side == "sell"]
        if signal.mode == MAKER_TAKER and signal.hedge_side == "sell":
            sells.append((signal.hedge_venue, signal.qty))
        for venue, qty in sells:
            if self.product_by_venue.get(venue) == "spot" and self.inventory.base.get(venue, 0.0) + 1e-12 < qty:
                self.hedge["signals_blocked_spot_inventory"] += 1
                return True
        return False

    def _fire(self, signal: ArbSignal, now: float) -> None:
        if self._spot_inventory_blocks(signal):
            return
        tracker = self._trackers[signal.signal_id] = _SignalTracker(signal, len(signal.legs))
        for leg in signal.legs:
            self._send_leg(leg, signal, now)
        self._funding_predicted += signal.qty * max(signal.buy_price, 0.0) * self.config.arb.funding_holding_bps / 10_000.0
        self.signal_log.append({"signal_id": signal.signal_id, "ts": now, "mode": signal.mode, "buy_venue": signal.buy_venue, "sell_venue": signal.sell_venue,
                                "qty": signal.qty, "gross_edge_bps": signal.gross_edge_bps, "net_edge_bps": signal.net_edge_bps,
                                "naive_edge_bps": signal.naive_edge_bps, "conditional_edge_bps": signal.conditional_edge_bps, "edge_ref": signal.edge_ref,
                                "maker_side": (("bid" if signal.legs[0].side == "buy" else "ask") if signal.mode == MAKER_TAKER and signal.legs else None)})
        if signal.mode == MAKER_TAKER and self.config.arb.dispatch_mode == "simultaneous":
            # Method B, for comparison only: the full-size hedge goes out with the maker order. If the maker never fills, the
            # hedge is an orphan position (counted in _maybe_resolve and unwound by the residual policy).
            hedge = self.strategy.hedge_leg(signal, signal.qty, self.books[signal.hedge_venue])
            if hedge is not None:
                tracker.hedged_qty += signal.qty
                tracker.legs_outstanding += 1
                tracker.hedge_first_sent_ts = now
                self.hedge["hedges_sent_before_fill"] += 1
                self._send_leg(hedge, signal, now, is_hedge=True)
            else:
                self.hedge["hedge_leg_unavailable"] += 1

    def _execute_arrival(self, order: InFlightOrder, now: float) -> None:
        """An in-flight order reached the exchange. Fill against the live book."""
        if order.kind == "recover":
            self._handle_recover(order, now)
            return
        if order.ghost:
            self._execute_ghost(order, now)
            return
        tracker = self._trackers.get(order.signal_id)
        book = self.books[order.venue]
        spec = self.venue_specs[order.venue]

        if order.role == "unwind":
            self._execute_unwind(order, tracker, now)
            return

        if order.is_taker and order.is_hedge and self.faults is not None:
            self._execute_hedge_with_faults(order, tracker, now)
            return

        if order.is_taker:
            if self.config.enforce_book_state and book.status(now, self.config.arb.max_book_age_s) != "valid":
                self.orders_missed += 1
                self.miss_reasons["book_not_valid"] += 1
                if tracker is not None:
                    tracker.missed += 1
                    tracker.legs_outstanding -= 1
                    self._maybe_resolve(order.signal_id, now)
                return
            outcome = self.taker.execute(
                book=self._arrival_book(order), side=order.side, qty=order.qty,
                limit_price=order.limit_price, fee_bps=spec.fees.taker_bps,
                now=now, expected_price=order.expected_price,
                signal_id=order.signal_id,
            )
            if outcome.filled and outcome.fill is not None:
                self.orders_filled += 1
                if order.is_hedge:
                    self.hedge["hedges_filled"] += 1
                    self.hedge["hedge_flight_s_sum"] += now - order.sent_ts
                    if tracker is not None:
                        tracker.hedge_last_fill_ts = now
                        tracker.hedge_fill_qty += outcome.fill.qty
                self._on_fill(outcome.fill, tracker)
            else:
                self.orders_missed += 1
                self.miss_reasons[outcome.reason] += 1
                if tracker is not None:
                    tracker.missed += 1
            if tracker is not None:
                tracker.legs_outstanding -= 1
                self._maybe_resolve(order.signal_id, now)
            return

        # Passive leg joins the book and waits for the market to come to it.
        if order.fault == "reject":                   # injected exchange reject that has nothing to do with the book (rate limit, error)
            self._maker_rejected(order, "fault_rejected", order.release_ts, injected=True)
            return
        if self.config.postonly_reject_model == "on":
            if self._truth_on and self._truth_complete_at(order):
                # WP3: the venue-truth book already holds every event up to the arrival's exchange time (the look-ahead window
                # reaches past it), so the post-only check runs AT arrival against the book the matching engine held.
                tb = self.truth_books[order.venue]
                self.po["arrivals"] += 1
                self.po["checks"] += 1
                self.po["checks_truth_at_arrival"] += 1
                if would_cross(order.side, order.limit_price, tb.best_bid, tb.best_ask):
                    self.po["rejects"] += 1
                    self.po["rejects_" + order.side] += 1
                    self._maker_rejected(order, "postonly_reject", order.release_ts, injected=False)
                else:
                    self.po["accepted"] += 1
                    self._place_passive(order, now)
                return
            # Arrived at the exchange at release_ts; accepted or rejected against the exchange-time book, which is known only
            # once the feed has delivered the first event stamped at or after that time (see _resolve_postonly).
            self._po_wait[order.venue].append(order)
            self.po["arrivals"] += 1
            return
        self._place_passive(order, now)

    def _truth_complete_at(self, order: InFlightOrder) -> bool:
        """True when the truth book of the order's venue is known to be complete at the arrival's exchange time: the catch-up
        has run and an event stamped after that time is already held (so nothing stamped before it is still to come)."""
        aligner = self.latency.aligner
        x = aligner.to_exchange(order.release_ts) if aligner.ready else order.release_ts
        self._truth_catch_up(order.venue, x, order.release_ts)
        q = self._ahead[order.venue]
        tb = self.truth_books[order.venue]
        return tb.ready and bool(q) and (getattr(q[0], "exchange_ts", None) or 0.0) > x

    def _place_passive(self, order: InFlightOrder, now: float) -> None:
        tracker = self._trackers.get(order.signal_id)
        book = self.books[order.venue]
        if not book.ready:
            self.orders_missed += 1
            self.miss_reasons["not_ready"] += 1
            if tracker is not None:
                tracker.legs_outstanding -= 1
                self._maybe_resolve(order.signal_id, now)
            return

        state = self.states[order.venue]
        maker_order = self.maker.place(
            venue=order.venue, pair=self.config.pair, symbol=self.config.pair,
            side=Side.BID if order.side == "buy" else Side.ASK,
            price=order.limit_price, size=order.qty, book=book,
            state=state.to_market_state(book, now),
            regime_bucket=state.regime().bucket, now=now, active_ts=now,
            tick_size=self.tick_size[order.venue], signal_id=order.signal_id,
            arrival_ts=order.release_ts,
        )
        self._maker_to_signal[maker_order.order_id] = order.signal_id
        if self._breaker is not None and self._breaker.is_tripped():
            self._request_cancel(maker_order, now, "breaker_trip")      # r12: it landed after the trip: cancel it straight away
        self.events.emit(now, "order_resting", order.venue, source="modelled", order_id=order.order_id, signal_id=order.signal_id,
                         side=order.side, qty=order.qty, price=order.limit_price)
        if order.fault == "unknown":
            # The acknowledgement was lost: the order is live at the exchange but the strategy treats it as unknown until a
            # venue query answers. Fills in that window are reported only then.
            until = order.release_ts + self.faults.ack_timeout_s + self.faults.unknown_resolve_s
            self._maker_unknown_until[maker_order.order_id] = until
            self.hedge["unknown_orders"] += 1
            self.events.emit(order.release_ts, "order_unknown", order.venue, source="injected", order_id=order.order_id,
                             signal_id=order.signal_id, reason="ack_lost")
        if tracker is not None:
            tracker.legs_outstanding -= 1
            tracker.maker_resting = True

    # ── PostOnly acceptance (postonly.py) ────────────────────────────────────

    def _notice_s(self, venue: str) -> float:
        """How long after an exchange-side reject of a passive order the strategy learns of it."""
        if self.config.postonly_notice_s is not None:
            return self.config.postonly_notice_s
        if self.timelines is not None:
            return self.timelines.sample_s(venue, "ack_report")   # WP3: the report timeline, never a second arrival draw
        if self.ack.active:
            return self.ack.sample_s()
        return self.venue_specs[venue].latency.feed_ms / 1000.0

    def _maker_rejected(self, order: InFlightOrder, reason: str, decision_ts: float, *, injected: bool) -> None:
        """A passive order was refused: no fill, no queue position, no conversion. The slot stays occupied until the notice."""
        notice_at = decision_ts + self._notice_s(order.venue)
        self._order_ids += 1
        self.pending.enqueue(InFlightOrder(
            release_ts=notice_at, order_id=self._order_ids, venue=order.venue, side=order.side, qty=order.qty,
            limit_price=order.limit_price, is_taker=False, sent_ts=order.sent_ts, signal_id=order.signal_id, kind="recover",
            tag="maker_fail:" + reason, origin_id=order.order_id))
        self.events.emit(decision_ts, "order_rejected", order.venue, source="injected" if injected else "modelled", order_id=order.order_id,
                         signal_id=order.signal_id, side=order.side, qty=order.qty, price=order.limit_price, reason=reason)
        if injected:
            self.hedge["maker_orders_rejected_injected"] += 1

    def _resolve_postonly(self, venue: str, ets: float | None, now: float) -> None:
        """Check the waiting passive orders of `venue` against the book as it is BEFORE the event now being applied.

        The event's host-aligned exchange time `ets` says how far the exchange has got. Within one venue the feed is in exchange
        order, so every update stamped before an order's arrival has been applied and none stamped after: the book is exactly
        what the matching engine held when the order arrived. Without an exchange time the receive time is used instead
        (counted), which is the book the strategy sees.
        """
        t_cmp = ets if ets is not None else now
        book = self.books[venue]
        keep: list[InFlightOrder] = []
        for o in self._po_wait[venue]:
            if t_cmp < o.release_ts:
                keep.append(o)
                continue
            tracker = self._trackers.get(o.signal_id)
            self.po["checks"] += 1
            if ets is None:
                self.po["checks_receive_time_fallback"] += 1
            self.po["wait_s_sum"] += max(0.0, t_cmp - o.release_ts)
            if not book.ready:
                self.orders_missed += 1
                self.miss_reasons["not_ready"] += 1
                if tracker is not None:
                    tracker.legs_outstanding -= 1
                    self._maybe_resolve(o.signal_id, now)
                continue
            if would_cross(o.side, o.limit_price, book.best_bid, book.best_ask):
                self.po["rejects"] += 1
                self.po["rejects_" + o.side] += 1
                self._maker_rejected(o, "postonly_reject", o.release_ts, injected=False)
            else:
                self.po["accepted"] += 1
                self._place_passive(o, now)
        self._po_wait[venue] = keep

    # ── Fill handling ────────────────────────────────────────────────────────

    def _on_fill(self, fill: Fill, tracker: _SignalTracker | None = None, chain: int = 0) -> None:
        if tracker is None:
            tracker = self._trackers.get(fill.signal_id)
        # Every fill the backtester produces is derived from market data and assumptions: "modelled". `chain` names the injected
        # fault that made the strategy send this order (a retry, an unwind, a ghost), so such fills can be summed separately.
        self.events.emit(fill.ts, "fill", fill.venue, source="modelled", order_id=fill.order_id, signal_id=fill.signal_id, chain_id=chain,
                         side=fill.side, qty=fill.qty, price=fill.price, reason="synthetic_flow" if fill.is_synthetic else ("taker" if fill.is_taker else "maker"))

        self.inventory.apply(fill.venue, fill.signed_qty, fill.cash_delta)

        if self.margin_model is not None:
            self.margin_model.note_fill(fill.venue, fill.qty if fill.side == "buy" else -fill.qty, fill.price, self.inventory)
        self.fees_paid += fill.fee
        self.traded_notional += fill.notional
        self.markouts.record_fill(fill.ts, fill.venue, fill.side, fill.price, fill.qty)

        if fill.slippage_bps:
            self.slippage_sum += fill.slippage_bps
            self.slippage_count += 1

        if tracker is not None:
            if fill.side == "buy":
                tracker.buy_qty += fill.qty
                tracker.buy_notional += fill.notional
            else:
                tracker.sell_qty += fill.qty
                tracker.sell_notional += fill.notional
            tracker.fees += fill.fee

            # Passive leg filled: fire the hedge now, not before. Sending it
            # earlier would leave a naked position against an uncertain fill.
            sig = tracker.signal
            if sig.mode == MAKER_TAKER and not fill.is_taker:
                if tracker.maker_first_fill_ts is None:
                    tracker.maker_first_fill_ts = fill.ts
                    hb = self.books[sig.hedge_venue]
                    tracker.hedge_px_at_fill = (hb.best_bid if sig.hedge_side == "sell" else hb.best_ask) if hb.ready else None
                tracker.maker_last_fill_ts = fill.ts
                simultaneous = self.config.arb.dispatch_mode == "simultaneous"
                if simultaneous:
                    hedge, send_ts = None, fill.ts                 # Method B: the full-size hedge already went out with the maker order
                else:
                    hedge = self.strategy.hedge_leg(sig, fill.qty, self.books[sig.hedge_venue])
                    send_ts = self._maker_fill_faults(sig, fill) if self.faults is not None else fill.ts
                    if self.timelines is not None:
                        # WP3: the strategy learns of the fill one private fill-report latency after the venue matched it
                        # (the event's exchange time on the host clock), never before the public event that revealed it.
                        base = self._current_ets if self._current_ets is not None else fill.ts
                        send_ts = max(send_ts, base + self.timelines.sample_s(fill.venue, "fill_report"))
                if simultaneous:
                    pass
                elif hedge is not None:
                    tracker.hedged_qty += fill.qty
                    tracker.legs_outstanding += 1
                    if tracker.hedge_first_sent_ts is None:
                        tracker.hedge_first_sent_ts = send_ts
                    self._send_leg(hedge, sig, send_ts, is_hedge=True)
                else:
                    # No hedge could be built (hedge venue book not ready / no bid). The original code then leaves the
                    # signal unresolved until the end of data, with the maker fill sitting naked in the inventory.
                    self.hedge["hedge_leg_unavailable"] += 1
                    self.hedge["hedge_leg_unavailable_qty"] += fill.qty
                    if self.faults is not None and self.faults.response != "legacy":
                        tracker.hedged_qty += fill.qty
                        tracker.legs_outstanding += 1
                        probe = self._probe_order(sig, fill.qty, send_ts, is_hedge=True)
                        self._hedge_failed(probe, send_ts + self.faults.local_reject_s, "hedge_unavailable")
                # Maker order fully consumed: order was removed from self.maker.orders
                # in _consume(). Resolve immediately instead of waiting for cancel_all.
                if fill.order_id not in self.maker.orders:
                    tracker.maker_resting = False

        if len(self.trade_log) < self.config.max_trade_log:
            notional = fill.qty * fill.price
            self.trade_log.append({
                "ts": fill.ts, "venue": fill.venue, "side": fill.side,
                "qty": fill.qty, "price": fill.price, "fee": fill.fee,
                "fee_bps": (fill.fee / notional * 10_000.0) if notional > 0 else 0.0,
                "kind": self._fill_kind or ("taker" if fill.is_taker else "maker"),
                "taker": fill.is_taker, "signal_id": fill.signal_id,
                "slippage_bps": fill.slippage_bps,
                "synthetic": fill.is_synthetic,
                "source": "modelled", "chain_id": chain,
            })

    def _maybe_resolve(self, signal_id: int, now: float) -> None:
        tracker = self._trackers.get(signal_id)
        if tracker is None or tracker.legs_outstanding > 0:
            return
        # A maker signal whose passive leg is still resting is not finished.
        if tracker.maker_resting:
            return

        self._trackers.pop(signal_id, None)
        self.signals_completed += 1
        sig_ = tracker.signal
        if sig_.mode == MAKER_TAKER and self.config.arb.dispatch_mode == "simultaneous":
            maker_filled = tracker.buy_qty if sig_.hedge_side == "sell" else tracker.sell_qty
            orphan = max(0.0, tracker.hedge_fill_qty - maker_filled)
            if orphan > 1e-12:
                self.hedge["orphan_hedges"] += 1
                self.hedge["orphan_qty"] += orphan
        if tracker.maker_first_fill_ts is not None:
            px_quote = sig_.sell_price if sig_.hedge_side == "sell" else sig_.buy_price
            px_fill = tracker.hedge_px_at_fill
            move = None
            if px_fill is not None and px_quote > 0:
                move = (px_fill - px_quote) / px_quote * 10_000.0 if sig_.hedge_side == "sell" else (px_quote - px_fill) / px_quote * 10_000.0
            if sig_.hedge_side == "sell":
                hedge_vwap = tracker.sell_notional / tracker.sell_qty if tracker.sell_qty > 0 else None
            else:
                hedge_vwap = tracker.buy_notional / tracker.buy_qty if tracker.buy_qty > 0 else None
            exec_move = None
            if hedge_vwap is not None and px_quote > 0:
                exec_move = (hedge_vwap - px_quote) / px_quote * 10_000.0 if sig_.hedge_side == "sell" else (px_quote - hedge_vwap) / px_quote * 10_000.0
            self.legging_rows.append({
                "side": "bid" if sig_.hedge_side == "sell" else "ask", "hedge_px_at_quote": px_quote, "hedge_px_at_fill": px_fill, "hedge_move_bps": move,
                # quote-time touch -> executed hedge VWAP: the move the conditional edge must expect (touch move + flight + slippage). NEGATIVE = adverse.
                "hedge_fill_vwap": hedge_vwap, "hedge_exec_move_bps": exec_move,
                "signal_id": signal_id, "maker_first_fill_ts": tracker.maker_first_fill_ts, "maker_last_fill_ts": tracker.maker_last_fill_ts,
                "hedge_first_sent_ts": tracker.hedge_first_sent_ts, "hedge_last_fill_ts": tracker.hedge_last_fill_ts,
                "maker_qty": tracker.buy_qty if tracker.signal.hedge_side == "sell" else tracker.sell_qty, "hedge_fill_qty": tracker.hedge_fill_qty,
                "legging_s": (tracker.hedge_last_fill_ts - tracker.maker_first_fill_ts) if tracker.hedge_last_fill_ts is not None else None,
            })

        matched = min(tracker.buy_qty, tracker.sell_qty)
        imbalance = tracker.buy_qty - tracker.sell_qty

        if matched > 0:
            buy_vwap = tracker.buy_notional / max(tracker.buy_qty, 1e-12)
            sell_vwap = tracker.sell_notional / max(tracker.sell_qty, 1e-12)
            self.gross_pnl += (sell_vwap - buy_vwap) * matched
            if buy_vwap > 0:
                gross_bps = (sell_vwap - buy_vwap) / buy_vwap * 10_000.0
                fee_bps = tracker.fees / max(matched * buy_vwap, 1e-12) * 10_000.0
                self.realized_gross_sum += gross_bps
                self.realized_net_sum += gross_bps - fee_bps
                self.expected_gross_sum += tracker.signal.gross_edge_bps
                self.edge_samples += 1

        if abs(imbalance) > 1e-9:
            self.signals_unhedged += 1
            if tracker.managed:
                # The fault response policy decided to carry (or could not finish unwinding) this inventory: no instant flatten.
                self.hedge["residual_signals"] += 1
                self.hedge["residual_qty"] += abs(imbalance)
            else:
                self._unwind(tracker, imbalance, now)
        elif matched > 0:
            self.signals_fully_hedged += 1
        else:
            # Both legs missed: the edge closed during flight on both venues.
            self.signals_both_missed += 1

    def _unwind(self, tracker: _SignalTracker, imbalance: float, now: float) -> None:
        """Flatten the leftover from a half-filled signal at market.

        Charged to unhedged_loss rather than buried in PnL: leg risk is the
        characteristic failure mode of cross-exchange arb and belongs on its own
        line. The unwind uses an unbounded limit because it must complete —
        carrying the exposure forward would silently turn an arb backtest into
        a directional one.
        """
        sig = tracker.signal
        # imbalance = buy_qty - sell_qty; positive → long on buy_venue → sell there
        venue = sig.buy_venue if imbalance > 0 else sig.sell_venue
        book = self.books[venue]
        qty = abs(imbalance)
        # This flatten is instant and latency-free: it trades at the touch of the moment the strategy learns of the miss.
        self.hedge["instant_unwinds"] += 1
        self.hedge["instant_unwind_qty"] += qty
        if not book.ready:
            self.hedge["instant_unwind_skipped_book_not_ready"] += 1
            self.hedge["instant_unwind_not_done_qty"] += qty
            return
        side = "sell" if imbalance > 0 else "buy"
        ref = book.mid

        outcome = self.taker.execute(
            book=book, side=side, qty=qty,
            limit_price=float("inf") if side == "buy" else 0.0,
            fee_bps=self.venue_specs[venue].fees.taker_bps,
            now=now, expected_price=ref, signal_id=sig.signal_id,
        )
        if outcome.filled and outcome.fill is not None:
            self.orders_filled += 1
            f = outcome.fill
            self.unhedged_loss += abs(f.price - ref) * f.qty  # fee goes to fees_paid via _on_fill
            self._on_fill(f)
            if f.qty < qty - 1e-9:
                self.hedge["instant_unwind_partial"] += 1
                self.hedge["instant_unwind_not_done_qty"] += qty - f.qty
        else:
            self.hedge["instant_unwind_missed"] += 1
            self.hedge["instant_unwind_not_done_qty"] += qty


    # ── Hedge-failure injection (hedge_faults.py) ────────────────────────────

    def _outages_at(self, venue: str, ts: float):
        for o in self.faults.outages:
            if o.venue == venue and o.covers(ts):
                yield o
        for o in self._dyn_outages:
            if o.venue == venue and o.covers(ts):
                yield o
        for g in self._disc_gens:
            if g.spec.venue == venue:
                w = g.active(ts)
                if w is not None:
                    yield w

    def _active_outage(self, venue: str, ts: float) -> Outage | None:
        """The outage on `venue` covering `ts` that lasts longest (None if the venue is reachable)."""
        best = None
        for o in self._outages_at(venue, ts):
            if best is None or o.end_ts > best.end_ts:
                best = o
        return best

    def _install_timing(self) -> None:
        """WP3: the measured-offset clock replaces the median aligner and the feed timeline replaces the preset feed sampler."""
        if self.timelines is None:
            return
        aligner = FixedOffsetAligner(self.timing_cfg.clock)
        self.latency.aligner = aligner
        for v, vl in self.latency.venues.items():
            vl.aligner = aligner
            vl.feed = self.timelines.sampler(v, "feed")

    def _breaker_poll(self, now: float) -> None:
        """r12: drive the breaker's gateway-down trigger. A venue whose order gateway is reachable produces a heartbeat; one inside
        an outage (gateway or full) does not, so after heartbeat_timeout_s without one the monitor reports it down. Polled at most
        every 0.25 s of event time; the trip is stamped at the poll, i.e. up to 0.25 s + one event after the timeout."""
        b = self._breaker
        if b is None or now - self._breaker_poll_ts < 0.25:
            return
        first = self._breaker_poll_ts < -1e17
        self._breaker_poll_ts = now
        for v, mon in b.connectivity.items():
            if first or self.faults is None or self._active_outage(v, now) is None:     # the first poll seeds every venue as alive
                mon.record_heartbeat(now)
        b.on_gateway_status(now, abs(self.inventory.net_base), self.pending.in_flight_count())

    def reset_breaker(self) -> None:
        """Manual reset (an operator action). Nothing else in the simulator clears a tripped breaker."""
        if self._breaker is not None:
            self._breaker.reset()
            self._trip_cancelled = False

    def _next_deadline(self) -> float | None:
        """Earliest timer strictly after the engine's current time: in-flight arrivals and recovery/UNKNOWN timers, maker
        lifetimes (request, then the effective cancel on the receive basis), funding settlements."""
        cands = [self.pending.peek_release_ts()]
        if self.settler is not None:
            cands.append(self.settler.next_ts())
        ttl = self.config.arb.maker_max_age_s
        if ttl > 0:
            for o in self.maker.orders.values():
                if o.cancel_requested_ts is None:
                    cands.append(o.placed_ts + ttl)
                elif self.config.cancel_time_basis != "exchange" and o.cancel_effective_ts is not None:
                    cands.append(o.cancel_effective_ts)
        cands = [c for c in cands if c is not None and c > self._now]
        return min(cands) if cands else None

    def _fire_timers_before(self, now: float) -> None:
        """WP3 timer wheel: fire every deadline strictly before the next market event at its own time (not at the event's).
        Deadlines equal to the event time are handled by _advance_to(now) under the equal-time policy."""
        guard = 0
        while True:
            d = self._next_deadline()
            if d is None or d >= now:
                return
            guard += 1
            if guard > 1_000_000:
                raise RuntimeError("timer wheel did not converge")
            self._advance_to(d, from_timer=True)
            if self.settler is not None:
                self.settler.settle(d, self.inventory, self.books)
            self.timing_stats["timers_fired_between_events"] += 1

    # ── WP3 venue-truth books ────────────────────────────────────────────────
    def _lookahead_s(self) -> float:
        return max(t.feed.max_ms for t in self.timing_cfg.venues.values()) / 1000.0

    def _with_lookahead(self, feed):
        """Yield the feed in receive order while holding a window of later events per venue (self._ahead), so an arrival at
        exchange time x can see venue events that the exchange had already processed but the collector had not yet delivered."""
        self._ahead_mode = True
        window = self._lookahead_s()
        buf: deque = deque()
        it = iter(feed)
        for ev in it:
            buf.append(ev)
            self._ahead[ev.venue].append(ev)
            while buf and buf[-1].receive_ts - buf[0].receive_ts >= window:
                yield buf.popleft()
        while buf:
            yield buf.popleft()

    def _truth_ingest(self, ev) -> None:
        """Test hook: hand the truth layer an event the feed will deliver later (what _with_lookahead does from the feed)."""
        self._ahead_mode = True
        self._ahead[ev.venue].append(ev)

    def _truth_apply(self, ev, tb: OrderBook) -> None:
        if ev.kind == BOOK:
            bids, asks = snapshot_levels(ev.payload)
            if bids and asks:
                tb.apply_snapshot(bids, asks, getattr(ev, "exchange_ts", None) or ev.receive_ts)
        elif ev.kind == DELTA:
            side, price, volume, event_type = delta_fields(ev.payload)
            if price > 0:
                tb.apply_delta(side, price, volume, event_type, getattr(ev, "exchange_ts", None) or ev.receive_ts)

    def _truth_catch_up(self, venue: str, x_exchange: float | None, ref_host_ts: float | None = None) -> None:
        """Apply to the venue-truth book every held event of `venue` stamped at or before exchange time x. ref_host_ts: the host
        time of whatever is being evaluated (an arrival's release time); events received after it are counted as applied
        before receipt — the look-ahead the truth layer exists for."""
        if not self._truth_on:
            return
        q = self._ahead[venue]
        tb = self.truth_books[venue]
        while q:
            ev = q[0]
            ets = getattr(ev, "exchange_ts", None)
            if x_exchange is not None and ets is not None and ets > x_exchange:
                break
            q.popleft()
            if ets is not None and ets < tb.last_update_ts:
                self.timing_stats["truth_events_out_of_exchange_order"] += 1
            self._truth_apply(ev, tb)
            if ref_host_ts is not None and ev.receive_ts > ref_host_ts:
                self.timing_stats["truth_events_applied_before_receipt"] += 1

    def _truth_sync_event(self, ev) -> None:
        """Called for every event the strategy view applies: the truth book must be at least as far along."""
        if not self._truth_on:
            return
        if self._ahead_mode:
            self._truth_catch_up(ev.venue, getattr(ev, "exchange_ts", None), ev.receive_ts)
            q = self._ahead[ev.venue]
            if q and q[0] is ev:                       # exchange_ts missing or out of order: apply it now anyway
                q.popleft()
                self._truth_apply(ev, self.truth_books[ev.venue])
        else:
            self._truth_apply(ev, self.truth_books[ev.venue])

    def _arrival_book(self, order: InFlightOrder) -> OrderBook:
        """The book a taker arrival executes against: the venue-truth book at the arrival's exchange time under WP3, else the
        strategy view."""
        if not self._truth_on:
            return self.books[order.venue]
        aligner = self.latency.aligner
        x = aligner.to_exchange(order.release_ts) if aligner.ready else order.release_ts
        self._truth_catch_up(order.venue, x, order.release_ts)
        tb = self.truth_books[order.venue]
        view = self.books[order.venue]
        if not tb.ready:
            self.timing_stats["truth_book_not_ready_at_arrival"] += 1
            return view
        self.timing_stats["truth_arrivals"] += 1
        if view.ready and view.mid > 0:
            d = abs(tb.mid - view.mid) / view.mid * 1e4
            self.timing_stats["truth_vs_view_mid_bps_sum"] += d
            if d > self.timing_stats.get("truth_vs_view_mid_bps_max", 0.0):
                self.timing_stats["truth_vs_view_mid_bps_max"] = d
        return tb

    def _dead_feed_blocks(self, now: float) -> bool:
        """WP3 dead-feed gate on ABSOLUTE age per venue: a dead venue, or every venue stale at once, blocks entry."""
        cfg = self.timing_cfg
        max_age = cfg.max_book_age_s if cfg.max_book_age_s is not None else self.config.arb.max_book_age_s
        states = {v: feed_state(self.books[v].age_s(now), max_age, cfg.dead_feed_s) for v in self.config.venues}
        for v, st in states.items():
            if st != self._feed_state.get(v):
                self.timing_stats[f"feed_{st}_transitions_{v}"] += 1
                self._feed_state[v] = st
        if any(st == "dead" for st in states.values()):
            self.timing_stats["signals_blocked_by_dead_feed"] += 1
            return True
        if all(st != "valid" for st in states.values()):
            self.timing_stats["entry_blocked_both_stale"] += 1
            return True
        return False

    def _timing_report(self) -> dict:
        if self.timelines is None:
            return {}
        stats = dict(self.timing_stats)
        if stats.get("truth_arrivals"):
            stats["truth_vs_view_mid_bps_mean"] = stats.pop("truth_vs_view_mid_bps_sum", 0.0) / stats["truth_arrivals"]
        return {"config": self.timing_cfg.describe(), "timelines": self.timelines.report(), "clock": self.latency.aligner.report(),
                "stats": stats, "feed_state_final": dict(self._feed_state), "truth_books": self._truth_on}

    def _feed_dark(self, ts: float) -> bool:
        """True while any venue is in a 'full' outage: the strategy has no market data it can trust and does not evaluate."""
        return any(o.kind == "full" for v in self.config.venues for o in self._outages_at(v, ts))

    def _ack_s(self, venue: str) -> float:
        """How long after an exchange-side rejection or miss the strategy learns of it (the legacy response knows at once)."""
        if self.faults.response == "legacy":
            return 0.0
        if self.timelines is not None:
            return self.timelines.sample_s(venue, "ack_report")
        return self.venue_specs[venue].latency.feed_ms / 1000.0

    def _probe_order(self, sig: ArbSignal, qty: float, ts: float, *, is_hedge: bool, attempt: int = 0, origin_id: int = 0) -> InFlightOrder:
        """A hedge that could not even be built, represented as an order that is never queued (so a failure can be recorded for it)."""
        self._order_ids += 1
        return InFlightOrder(release_ts=ts, order_id=self._order_ids, venue=sig.hedge_venue, side=sig.hedge_side, qty=qty, limit_price=0.0,
                             is_taker=True, sent_ts=ts, signal_id=sig.signal_id,
                             expected_price=sig.sell_price if sig.hedge_side == "sell" else sig.buy_price, is_hedge=is_hedge,
                             attempt=attempt, origin_id=origin_id or self._order_ids)

    def _maker_fill_faults(self, sig: ArbSignal, fill: Fill) -> float:
        """Faults tied to the maker fill. Returns when the strategy can act on it (its execution report may be held up)."""
        f = self.faults
        ts = fill.ts
        until = self._maker_unknown_until.get(fill.order_id)
        if until is not None and ts < until:
            self.hedge["fill_reports_deferred_unknown"] += 1
            self.hedge["fill_report_delay_s_sum"] += until - ts
            ts = until                                    # the acknowledgement was lost: the first the strategy hears is the reconciliation
        out = self._active_outage(fill.venue, ts)
        if out is not None:
            ts = out.end_ts                               # execution reports ride on the order-entry session: they arrive at reconnect
            self.hedge["fill_reports_deferred"] += 1
            self.hedge["fill_report_delay_s_sum"] += ts - fill.ts
        self._maker_fill_count += 1
        if f.restart_s > 0 and self._maker_fill_count in f.restart_at_fills:
            # MODELLED restart: the process dies right after this fill; both venues are dark for the strategy until it is back,
            # then it reconciles (learns of the fill) and hedges. Breaker state persists (modelled assumption).
            end = ts + f.restart_s
            for v in self.config.venues:
                self._dyn_outages.append(Outage(v, ts, end, "full"))
            self.hedge["restarts"] += 1
            self.hedge["restart_downtime_s"] += f.restart_s
            self.events.emit(ts, "restart", fill.venue, source="modelled", signal_id=fill.signal_id, reason=f"down {f.restart_s:g}s")
            ts = end
        if f.outage_on_fill_s > 0 and self._active_outage(sig.hedge_venue, ts) is None:
            self._dyn_outages = [o for o in self._dyn_outages if o.end_ts > self._now - 3600.0]
            self._dyn_outages.append(Outage(sig.hedge_venue, ts, ts + f.outage_on_fill_s, f.outage_on_fill_kind))
            self.hedge["outages_triggered_by_fills"] += 1
        return ts

    def _inject_send_faults(self, order: InFlightOrder, now: float) -> bool:
        """Apply the configured faults to a hedge / unwind order at send time. True = diverted, do not enqueue it normally.

        The three random draws are always taken for a hedge, so changing one probability never shifts the others' draws.
        """
        f = self.faults
        out = self._active_outage(order.venue, now)
        if out is not None:
            self.hedge["sends_into_outage"] += 1
            self.events.emit(now, "send_into_outage", order.venue, source="injected", order_id=order.order_id, signal_id=order.signal_id,
                             chain_id=self._chain_of(order), side=order.side, qty=order.qty, reason=f.outage_policy)
            flight = order.release_ts - now
            if f.outage_policy == "reject":
                self._hedge_failed(order, now + f.local_reject_s, "fault_outage_reject")
                return True
            if f.outage_policy == "queue":
                order.release_ts = max(order.release_ts, out.end_ts + flight)
                self.hedge["queued_through_outage"] += 1
                self.hedge["queue_wait_s_sum"] += order.release_ts - now - flight
                return False
            self.orders_sent += 1
            self._hedge_failed(order, now + f.ack_timeout_s, "fault_outage_" + f.outage_policy)
            if f.outage_policy == "ghost":
                self.pending.enqueue(replace(order, release_ts=out.end_ts + flight, ghost=True))
            else:
                self.hedge["blackholed_orders"] += 1
            return True
        if order.is_hedge:
            r = self._fault_rng
            u_delay, u_reject, u_partial = r.random(), r.random(), r.random()
            if f.delay_prob and f.extra_delay_s and u_delay < f.delay_prob:
                order.release_ts += f.extra_delay_s
                self.hedge["hedges_delayed"] += 1
                self.events.emit(now, "hedge_delayed", order.venue, source="injected", order_id=order.order_id, signal_id=order.signal_id,
                                 chain_id=self._chain_of(order), qty=order.qty, reason=f"+{f.extra_delay_s:g}s")
            if u_reject < f.reject_prob:
                order.fault = "reject"
            elif u_partial < f.partial_prob:
                order.fault = "partial"
                order.partial_fraction = f.partial_fraction
            # Phenomena of the strategy-level fault layer: own stream, three draws per hedge, always taken.
            x = self._rng_extra
            u_unknown, u_fast, u_venue = x.random(), x.random(), x.random()
            if not order.fault:
                p_venue = dict(f.reject_prob_by_venue).get(order.venue, 0.0) if f.venue_reject_applies_to_hedges else 0.0
                fast = False
                if f.fast_move_reject_prob and f.fast_move_bps:
                    hist = self._mid_hist[order.venue]
                    if len(hist) >= 2:
                        edge = now - f.fast_move_window_s
                        ref = next((m for t, m in hist if t >= edge), hist[0][1])
                        fast = ref > 0 and abs(hist[-1][1] - ref) / ref * 1e4 >= f.fast_move_bps
                if u_venue < p_venue:
                    order.fault = "reject"
                    self.hedge["hedges_rejected_by_venue_prob"] += 1
                elif fast and u_fast < f.fast_move_reject_prob:
                    order.fault = "reject"
                    self.hedge["fast_move_hedge_rejects"] += 1
                elif u_unknown < f.ack_unknown_prob:
                    order.fault = "unknown"
                if fast:
                    self.hedge["hedges_sent_in_fast_move"] += 1
        return False

    def _inject_maker_send_faults(self, order: InFlightOrder, now: float) -> bool:
        """Faults on a passive order at send time. True = diverted (it never reaches the exchange).

        A passive order sent while its venue's gateway is down fails like any order sent into an outage: "reject" is known at once,
        "blackhole" and "ghost" are lost and noticed after ack_timeout_s (a passive ghost is not modelled: it is treated as a
        blackhole), "queue" is delivered at reconnect. Two draws per passive order are always taken, from their own stream.
        """
        f = self.faults
        u_reject, u_unknown = self._rng_maker.random(), self._rng_maker.random()
        out = self._active_outage(order.venue, now)
        if out is not None:
            self.hedge["maker_sends_into_outage"] += 1
            self.events.emit(now, "send_into_outage", order.venue, source="injected", order_id=order.order_id, signal_id=order.signal_id,
                             side=order.side, qty=order.qty, price=order.limit_price, reason=f.outage_policy)
            if f.outage_policy == "queue":
                order.release_ts = max(order.release_ts, out.end_ts + (order.release_ts - now))
                self.hedge["maker_queued_through_outage"] += 1
                return False
            self.orders_sent += 1
            delay = f.local_reject_s if f.outage_policy == "reject" else f.ack_timeout_s
            self._order_ids += 1
            self.pending.enqueue(InFlightOrder(
                release_ts=now + delay, order_id=self._order_ids, venue=order.venue, side=order.side, qty=order.qty,
                limit_price=order.limit_price, is_taker=False, sent_ts=now, signal_id=order.signal_id, kind="recover",
                tag="maker_fail:fault_outage_" + f.outage_policy, origin_id=order.order_id))
            return True
        if u_reject < dict(f.reject_prob_by_venue).get(order.venue, 0.0):
            order.fault = "reject"
        elif u_unknown < f.ack_unknown_prob:
            order.fault = "unknown"
        return False

    def _enqueue_timer(self, order: InFlightOrder, release_ts: float, tag: str, attempt: int) -> None:
        self._order_ids += 1
        self.pending.enqueue(InFlightOrder(
            release_ts=release_ts, order_id=self._order_ids, venue=order.venue, side=order.side, qty=order.qty,
            limit_price=order.limit_price, is_taker=True, sent_ts=order.sent_ts, signal_id=order.signal_id, kind="recover", tag=tag,
            is_hedge=order.is_hedge, role=order.role, attempt=attempt, origin_id=order.origin_id or order.order_id))

    def _hedge_failed(self, order: InFlightOrder, detect_ts: float, reason: str) -> None:
        """The hedge (or an unwind / retry) did not do its job. The strategy reacts at `detect_ts`, not before."""
        origin = order.origin_id or order.order_id
        ev = self._hedge_open.get(origin)
        first_failure_of_chain = ev is None
        if ev is None:
            ev = {"origin_id": origin, "signal_id": order.signal_id, "hedge_venue": order.venue if order.is_hedge else "", "qty": order.qty,
                  "t_sent": order.sent_ts, "first_reason": reason, "t_first_detect": detect_ts, "reasons": [], "outcome": None,
                  "t_resolved": None, "unwound_qty": 0.0, "ghost_qty": 0.0}
            self._hedge_open[origin] = ev                       # kept after the chain ends: a late ghost fill still finds its record
            if len(self.hedge_events) < 100_000:
                self.hedge_events.append(ev)
        ev["reasons"].append(reason)
        self.events.emit(detect_ts, "hedge_failed", order.venue, source="injected" if reason.startswith("fault_") else "modelled",
                         order_id=order.order_id, signal_id=order.signal_id, chain_id=origin, side=order.side, qty=order.qty, reason=reason)
        self.hedge["failed_" + reason] += 1
        self.hedge["failed_qty"] += order.qty
        if self._breaker is not None and first_failure_of_chain and order.role != "unwind":
            # r12 counting rule: one hedge FAILURE per hedge chain (its first failed attempt), not one per retry or per unwind
            # attempt; a failed unwind is the separate trip_on_failed_unwind trigger.
            net_abs = abs(self.inventory.net_base) if hasattr(self, "inventory") else 0.0
            self._breaker.on_hedge_result(detect_ts, failed=True, net_abs=net_abs,
                                          pending_hedges=self.pending.in_flight_count())
        tracker = self._trackers.get(order.signal_id)
        if tracker is not None and self.faults.response != "legacy":
            tracker.managed = True
        self._enqueue_timer(order, detect_ts, "failed:" + reason, order.attempt)

    def _finish_leg(self, order: InFlightOrder, tracker: _SignalTracker | None, now: float, outcome: str) -> None:
        ev = self._hedge_open.get(order.origin_id or order.order_id)
        if ev is not None and ev["outcome"] is None:
            ev["outcome"] = outcome
            ev["t_resolved"] = max(now, order.release_ts)
        if tracker is not None:
            tracker.legs_outstanding -= 1
            self._maybe_resolve(order.signal_id, now)

    def _handle_recover(self, order: InFlightOrder, now: float) -> None:
        """A strategy-side timer fired: a failure became known, or a retry is due."""
        f = self.faults
        tracker = self._trackers.get(order.signal_id)
        t0 = order.release_ts                                   # logical time of the timer; `now` is the next market event
        if order.tag.startswith("maker_fail:"):
            reason = order.tag.split(":", 1)[1]
            self.orders_missed += 1
            self.miss_reasons[reason] += 1
            self.po["notices"] += 1
            self.po["notice_delay_s_sum"] += now - order.release_ts
            if tracker is not None:
                tracker.legs_outstanding -= 1
                self._maybe_resolve(order.signal_id, now)
            return
        if order.tag == "unknown_resolved":
            if self._breaker is not None:
                self._breaker.on_order_resolved(order.origin_id or order.order_id)     # r12: a resolved UNKNOWN no longer ages toward a trip
            # WP6: the status query itself can fail; bounded attempts, then ALARM (the reservation stays, nothing is replaced blind).
            if f.unknown_query_fail_prob and self._rng_extra.random() < f.unknown_query_fail_prob:
                self.hedge["unknown_query_failed"] += 1
                if order.attempt + 1 < f.unknown_query_max_attempts:
                    self._enqueue_timer(order, t0 + f.unknown_resolve_s, "unknown_resolved", order.attempt + 1)
                    return
                self._raise_alarm(now, "unknown_unresolved", order)
                self._finish_leg(order, tracker, now, "unknown_unresolved_alarm")
                return
            self.hedge["unknown_resolved"] += 1
            self._finish_leg(order, tracker, now, "hedged_after_unknown")
            return
        if order.tag == "retry_send":
            self._resend(order, tracker, t0)
            return
        reason = order.tag.split(":", 1)[1]
        self.orders_missed += 1
        self.miss_reasons[reason] += 1
        if tracker is None:                                     # the signal was already closed out
            self.hedge["failures_after_signal_closed"] += 1
            return
        if f.response == "legacy":
            tracker.missed += 1
            self._finish_leg(order, tracker, now, "legacy_instant_flatten")
            return
        retryable = order.role == "unwind" or (order.is_hedge and f.response in ("retry", "retry_unwind"))
        limit = (f.unwind_max_attempts - 1) if (order.role == "unwind" and f.unwind_max_attempts is not None) else f.max_retries
        if retryable and order.attempt < limit:
            self._enqueue_timer(order, t0 + f.retry_backoff_s, "retry_send", order.attempt + 1)
            return
        if order.is_hedge and f.response in ("unwind", "retry_unwind"):
            self._send_unwind(order, tracker, t0)
            return
        self.hedge["carried_events"] += 1
        self.hedge["carried_qty"] += order.qty
        if self._breaker is not None and order.role == "unwind":
            self._breaker.on_failed_unwind(now, abs(self.inventory.net_base), self.pending.in_flight_count())
        if order.role == "unwind":
            self._raise_alarm(now, "unwind_exhausted", order)
        self._finish_leg(order, tracker, now, "unwind_failed_carried" if order.role == "unwind" else "carried")

    def _raise_alarm(self, now: float, reason: str, order: InFlightOrder) -> None:
        doc = {"ts": now, "reason": reason, "venue": order.venue, "qty": order.qty, "signal_id": order.signal_id,
               "net_base": self.inventory.net_base, "base": dict(self.inventory.base)}
        self.alarms.append(doc)
        if self.alarm is None:
            self.alarm = doc
        self.hedge["alarms_raised"] += 1
        self.events.emit(now, "alarm", order.venue, source="modelled", order_id=order.order_id, signal_id=order.signal_id, qty=order.qty, reason=reason)

    def reset_alarm(self, now: float | None = None) -> None:
        """Manual reset (an operator action): clears the block; the alarm history stays in `alarms`."""
        if self.alarm is not None:
            self.hedge["alarms_reset"] += 1
        self.alarm = None

    def _resend(self, order: InFlightOrder, tracker: _SignalTracker | None, t0: float) -> None:
        if tracker is None:
            return
        sig = tracker.signal
        if order.role == "unwind":
            self._send_unwind(order, tracker, t0, attempt=order.attempt)
            return
        hedge = self.strategy.hedge_leg(sig, order.qty, self.books[sig.hedge_venue])
        if hedge is None:
            self.hedge["hedge_leg_unavailable"] += 1
            self._hedge_failed(self._probe_order(sig, order.qty, t0, is_hedge=True, attempt=order.attempt, origin_id=order.origin_id),
                               t0 + self.faults.local_reject_s, "hedge_unavailable")
            return
        self.hedge["retries_sent"] += 1
        self._send_leg(hedge, sig, t0, is_hedge=True, attempt=order.attempt, origin_id=order.origin_id)

    def _send_unwind(self, failed: InFlightOrder, tracker: _SignalTracker, t0: float, attempt: int = 0) -> None:
        """Trade the unhedged maker fill back on the maker venue as a real order (sell back a filled bid, buy back a filled ask):
        entry latency, executes against the book of its arrival."""
        sig = tracker.signal
        if self._breaker is not None and not self._breaker.allow_unwind():
            # r12: a tripped breaker lets only tripped_unwind_retries unwind sends through; the rest is an ALARM, not a silent skip
            self.hedge["unwinds_denied_by_breaker"] += 1
            self._raise_alarm(t0, "unwind_budget_exhausted", failed)
            self._finish_leg(failed, tracker, t0, "unwind_denied_by_breaker")
            return
        venue = sig.maker_venue
        side = "sell" if sig.hedge_side == "sell" else "buy"
        book = self.books[venue]
        band = (self.faults.unwind_band_bps if self.faults is not None else 0.0) / 10_000.0
        if band > 0.0 and book.ready:
            limit = book.mid * (1.0 - band) if side == "sell" else book.mid * (1.0 + band)     # WP6: price-banded unwind
        else:
            limit = 0.0 if side == "sell" else float("inf")
        leg = ArbLeg(venue=venue, side=side, qty=failed.qty, limit_price=limit, is_taker=True,
                     expected_price=book.mid if book.ready else (sig.buy_price if side == "sell" else sig.sell_price),
                     fee_bps=self.venue_specs[venue].fees.taker_bps)
        self.hedge["unwinds_sent"] += 1
        self._send_leg(leg, sig, t0, role="unwind", attempt=attempt, origin_id=failed.origin_id)

    def _execute_unwind(self, order: InFlightOrder, tracker: _SignalTracker | None, now: float) -> None:
        book = self.books[order.venue]
        ref = book.mid if book.ready else 0.0
        out = self.taker.execute(
            book=book, side=order.side, qty=order.qty, limit_price=order.limit_price,
            fee_bps=self.venue_specs[order.venue].fees.taker_bps, now=now, expected_price=ref, signal_id=order.signal_id)
        done = 0.0
        if out.filled and out.fill is not None:
            self.orders_filled += 1
            done = out.fill.qty
            if ref > 0:
                self.unhedged_loss += abs(out.fill.price - ref) * done
            self._on_fill(out.fill, tracker, chain=order.origin_id or order.order_id)
            self.hedge["unwind_filled_qty"] += done
        ev = self._hedge_open.get(order.origin_id or order.order_id)
        if ev is not None:
            ev["unwound_qty"] += done
        remainder = order.qty - done
        if remainder > 1e-9:
            self._hedge_failed(replace(order, qty=remainder), order.release_ts + self._ack_s(order.venue),
                               "unwind_partial" if done > 0 else "unwind_" + out.reason)
            return
        self._finish_leg(order, tracker, now, "unwound")

    def _execute_ghost(self, order: InFlightOrder, now: float) -> None:
        """A hedge the strategy had written off is delivered at reconnect and fills if the book allows: an over-hedge."""
        book = self.books[order.venue]
        out = self.taker.execute(
            book=book, side=order.side, qty=order.qty, limit_price=order.limit_price,
            fee_bps=self.venue_specs[order.venue].fees.taker_bps, now=now, expected_price=order.expected_price,
            signal_id=order.signal_id)
        if out.filled and out.fill is not None:
            self.orders_filled += 1
            self.hedge["ghost_fills"] += 1
            self.hedge["ghost_fill_qty"] += out.fill.qty
            ev = self._hedge_open.get(order.origin_id or order.order_id)
            if ev is not None:
                ev["ghost_qty"] += out.fill.qty
            self._on_fill(out.fill, chain=order.origin_id or order.order_id)
        else:
            self.hedge["ghost_misses"] += 1

    def _execute_hedge_with_faults(self, order: InFlightOrder, tracker: _SignalTracker | None, now: float) -> None:
        """A hedge (or retry) reaching the exchange while injection is configured."""
        origin = order.origin_id or order.order_id
        if order.fault == "reject":
            self._hedge_failed(order, order.release_ts + self._ack_s(order.venue), "fault_rejected")
            return
        qty = order.qty * order.partial_fraction if order.fault == "partial" else order.qty
        outcome = self.taker.execute(
            book=self.books[order.venue], side=order.side, qty=qty, limit_price=order.limit_price,
            fee_bps=self.venue_specs[order.venue].fees.taker_bps, now=now, expected_price=order.expected_price,
            signal_id=order.signal_id)
        done = 0.0
        if outcome.filled and outcome.fill is not None:
            self.orders_filled += 1
            done = outcome.fill.qty
            self.hedge["hedges_filled"] += 1
            self.hedge["hedge_flight_s_sum"] += now - order.sent_ts
            self._on_fill(outcome.fill, tracker, chain=self._chain_of(order))
            if (self._breaker is not None and done >= order.qty - 1e-9 and order.attempt == 0
                    and self._hedge_open.get(order.origin_id or order.order_id) is None):
                # r12: only a clean first-attempt hedge counts as a success (resets the consecutive-failure run)
                self._breaker.on_hedge_result(now, failed=False, net_abs=abs(self.inventory.net_base),
                                              pending_hedges=self.pending.in_flight_count())
        remainder = order.qty - done
        learn = order.release_ts + self._ack_s(order.venue)
        if order.fault == "unknown":
            # The acknowledgement was lost: the strategy does not know what happened until it queries the venue. It does not
            # retry in the meantime (that is what would double-hedge); the order has executed or not at the exchange regardless.
            learn = order.release_ts + self.faults.ack_timeout_s + self.faults.unknown_resolve_s
            self.hedge["unknown_orders"] += 1
            if self._breaker is not None:
                # r12: the order is UNKNOWN for the strategy from the moment its ack timeout expires, not from arrival
                self._breaker.on_unknown_order(order.origin_id or order.order_id, order.release_ts + self.faults.ack_timeout_s, order.venue)
            self.events.emit(order.release_ts, "order_unknown", order.venue, source="injected", order_id=order.order_id,
                             signal_id=order.signal_id, chain_id=self._chain_of(order), qty=order.qty, reason="ack_lost")
            if remainder <= 1e-9:
                self._enqueue_timer(order, learn, "unknown_resolved", order.attempt)
                return
        if remainder > 1e-9:
            reason = "fault_partial" if order.fault == "partial" else (outcome.reason if not outcome.filled else "partial_depth")
            self._hedge_failed(replace(order, qty=remainder), learn, reason)
            return
        ev = self._hedge_open.get(origin)
        if ev is not None and ev["outcome"] is None:
            ev["outcome"] = "hedged_on_retry"
            ev["t_resolved"] = max(now, order.release_ts)
        if tracker is not None:
            tracker.legs_outstanding -= 1
            self._maybe_resolve(order.signal_id, now)

    def _ref_mid(self) -> float:
        """Mid of the book that was updated most recently: the one price both venues' inventory can be valued at without
        counting a stale book's age as profit or loss."""
        best = max(self.books.values(), key=lambda b: b.last_update_ts)
        return best.mid

    def _hedge_report(self, end_ts: float) -> dict:
        """Counters, the inventory path and one row per signal (cash flows from the trade log, residual marked at the final mids)."""
        if self.faults is None and self.exposure is None:
            return {}
        truncated = len(self.trade_log) >= self.config.max_trade_log
        ref = self._ref_mid()
        per: dict[int, dict] = {}
        for r in self.trade_log:
            row = per.setdefault(r["signal_id"], {"signal_id": r["signal_id"], "first_ts": r["ts"], "last_ts": r["ts"], "cash": 0.0,
                                                  "fees": 0.0, "pos": {}, "n_fills": 0, "buy_qty": 0.0, "sell_qty": 0.0, "buy_notional": 0.0})
            sgn = 1.0 if r["side"] == "buy" else -1.0
            row["cash"] -= sgn * r["qty"] * r["price"]
            row["fees"] += r["fee"]
            row["pos"][r["venue"]] = row["pos"].get(r["venue"], 0.0) + sgn * r["qty"]
            if sgn > 0:
                row["buy_qty"] += r["qty"]
                row["buy_notional"] += r["qty"] * r["price"]
            else:
                row["sell_qty"] += r["qty"]
            row["first_ts"] = min(row["first_ts"], r["ts"])
            row["last_ts"] = max(row["last_ts"], r["ts"])
            row["n_fills"] += 1
        signals = []
        for row in per.values():
            resid = sum(row["pos"].values())
            signals.append({"signal_id": row["signal_id"], "first_ts": row["first_ts"], "last_ts": row["last_ts"], "n_fills": row["n_fills"],
                            "buy_qty": row["buy_qty"], "sell_qty": row["sell_qty"], "buy_notional": row["buy_notional"],
                            "cash_after_fees": row["cash"] - row["fees"], "residual_net_base": resid,
                            "pnl_marked": row["cash"] - row["fees"] + resid * ref, "positions": row["pos"]})
        return {
            "counters": {k: float(v) for k, v in sorted(self.hedge.items())},
            "faults": self.faults.as_dict() if self.faults is not None else None,
            "exposure": self.exposure.report(end_ts) if self.exposure is not None else {},
            "events": self.hedge_events, "signals": signals, "trade_log_truncated": truncated,
            "unresolved_chains_at_data_end": sum(1 for e in self.hedge_events if e["outcome"] is None),
            "max_position_base": self.config.arb.max_position_base, "reference_mid_at_end": ref,
            "circuit_breaker": self._breaker.summary() if self._breaker is not None else None,
        }

    # ── Main loop ────────────────────────────────────────────────────────────

    def _expire_maker_orders(self, now: float, *, from_timer: bool = False) -> None:
        """Time-in-force for resting maker orders (ArbConfig.maker_max_age_s, seconds; 0 disables).

        Semantics (all times are replay receive time):
          * The lifetime clock starts when the order is placed at the exchange (`placed_ts`, the first replay event at or
            after its release_ts). An order still in flight therefore cannot expire; it is counted toward the live-order
            limit until it lands.
          * At `placed_ts + ttl` the strategy requests a cancel. The cancel takes effect after one sampled cancel latency,
            drawn once and stored on the order, so scanning again can neither move the deadline nor cancel twice.
          * Between the request and the effective time the order is 'cancel_pending': still resting, still fillable, and
            still counted as live. Fills in that window are kept.
          * Tie rule: expiry is processed before the market event it coincides with, so an event stamped exactly at the
            effective time does not fill the order.
          * The slot is released to the strategy one feed delay after the cancel lands, not at the instant it lands.
        """
        ttl = self.config.arb.maker_max_age_s
        # A cancel that lost the race to a full fill: the order left the book on its own, the cancel finds nothing.
        for oid, gone in list(self._cancel_watch.items()):
            if oid not in self.maker.orders:
                del self._cancel_watch[oid]
                self.lifetime["cancels_lost_to_fill"] += 1
                during = gone.filled_qty - gone.filled_at_cancel_request
                if during > 1e-12:
                    self.lifetime["fills_while_cancel_pending"] += 1
                    self.lifetime["qty_filled_while_cancel_pending"] += during
        for order in list(self.maker.orders.values()):
            if order.cancel_requested_ts is None:
                if ttl <= 0:
                    continue                                  # no lifetime: only an explicit cancel request (breaker trip) is ever pending
                requested = order.placed_ts + ttl
                if now < requested:
                    continue
                self._request_cancel(order, requested, "expired")
            if now < order.cancel_effective_ts or (self._market_first and not from_timer and now == order.cancel_effective_ts):
                continue                                  # market_first: an event stamped exactly at the effective time is applied first
            if self.config.cancel_time_basis == "exchange":
                continue                                  # applied by _apply_due_cancels when the venue's feed reaches that exchange time
            self._apply_cancel(order, now)

    def _request_cancel(self, order: MakerOrder, requested: float, reason: str) -> None:
        """The strategy asks the venue to cancel `order` at `requested`: one sampled cancel latency (later if the gateway is down),
        the order stays resting and fillable until then. reason: "expired" (time in force) or "breaker_trip"."""
        order.cancel_requested_ts = requested
        delay = self.latency.cancel_delay(order.venue)                 # always drawn
        if self.timelines is not None:
            delay = self.timelines.sample_s(order.venue, "cancel")    # WP3: the cancel timeline
        base = requested
        if self.faults is not None:
            delay *= self.faults.latency_scale
            out = self._active_outage(order.venue, requested)
            if out is not None:
                # The cancel cannot reach the venue until the gateway is back; the order keeps resting and can fill.
                base = out.end_ts
                self.hedge["cancels_delayed_by_outage"] += 1
                self.hedge["cancel_outage_delay_s_sum"] += out.end_ts - requested
                self.events.emit(requested, "cancel_delayed_by_outage", order.venue, source="injected", order_id=order.order_id,
                                 signal_id=self._maker_to_signal.get(order.order_id, 0))
        order.cancel_effective_ts = base + delay
        order.cancel_reason = reason
        order.filled_at_cancel_request = order.filled_qty
        self._cancel_watch[order.order_id] = order
        self.lifetime["expiries_triggered" if reason == "expired" else "breaker_cancels_requested"] += 1
        self.events.emit(requested, "cancel_requested", order.venue, source="modelled", order_id=order.order_id,
                         signal_id=self._maker_to_signal.get(order.order_id, 0), side=order.side_str, qty=order.remaining, price=order.price,
                         **({} if reason == "expired" else {"reason": reason}))

    def _breaker_trip_check(self, now: float) -> None:
        """r12: a tripped breaker cancels every resting maker order (each with its own cancel latency; fills in that window are kept and
        hedged as usual). Runs once per trip; a manual reset re-arms it. Orders that land AFTER the trip are cancelled at placement."""
        b = self._breaker
        if b is None:
            return
        if b.is_tripped():
            if not self._trip_cancelled:
                self._trip_cancelled = True
                n = 0
                for order in list(self.maker.orders.values()):
                    if order.cancel_requested_ts is None:
                        self._request_cancel(order, now, "breaker_trip")
                        n += 1
                self.hedge["breaker_trips_handled"] += 1
                self.hedge["resting_orders_cancel_requested_on_trip"] += n
        elif self._trip_cancelled:
            self._trip_cancelled = False

    def _apply_due_cancels(self, venue: str, ets: float | None, now: float) -> None:
        """cancel_time_basis == "exchange": cancels take effect before the first venue event stamped at or after their effective time."""
        t_cmp = ets if ets is not None else now
        for order in list(self.maker.orders.values()):
            if (order.venue == venue and order.cancel_requested_ts is not None and order.cancel_effective_ts is not None
                    and (t_cmp > order.cancel_effective_ts or (t_cmp == order.cancel_effective_ts and not self._market_first))):
                self._apply_cancel(order, now)

    def _apply_cancel(self, order: MakerOrder, now: float) -> None:
        effective = max(order.cancel_effective_ts, order.placed_ts)
        cancelled = self.maker.cancel(order.order_id, effective)
        if cancelled is None:
            return
        self._cancel_watch.pop(cancelled.order_id, None)
        self.lifetime["cancels_applied"] += 1
        self.lifetime["age_at_cancel_sum_s"] += effective - cancelled.placed_ts
        self.lifetime["cancel_pending_time_sum_s"] += effective - cancelled.cancel_requested_ts
        if cancelled.filled_qty > 1e-12:
            self.lifetime["expired_after_partial_fill"] += 1
        during = cancelled.filled_qty - cancelled.filled_at_cancel_request
        if during > 1e-12:
            self.lifetime["fills_while_cancel_pending"] += 1
            self.lifetime["qty_filled_while_cancel_pending"] += during
        self._cancel_acks.append(effective + (self.timelines.sample_s(cancelled.venue, "cancel_report") if self.timelines is not None
                                              else self.venue_specs[cancelled.venue].latency.feed_ms / 1000.0))
        sid = self._maker_to_signal.get(cancelled.order_id, -1)
        self.events.emit(effective, "cancel_applied", cancelled.venue, source="modelled", order_id=cancelled.order_id, signal_id=sid if sid > 0 else 0,
                         side=cancelled.side_str, qty=cancelled.remaining, price=cancelled.price)
        tracker = self._trackers.get(sid)
        if tracker is not None:
            tracker.maker_resting = False
        self._maybe_resolve(sid, now)

    def _pending_exposure(self) -> PendingExposure:
        """Worst-case base not in the inventory yet: resting maker remainders plus every order still in flight.

        When count_unknown_exposure=True (Task 2c opt-in), ghost/UNKNOWN orders are also counted: they are
        orders the strategy has given up on but which the exchange may still execute.
        """
        pend = PendingExposure()
        count_unknown = self.config.arb.count_unknown_exposure
        for o in self.maker.orders.values():
            pend.add(o.venue, o.side_str, o.remaining)
        for o in self.pending.in_flight():
            if o.kind != "order":
                continue
            if o.ghost and not count_unknown:
                continue   # strategy has written this off; exclude unless count_unknown_exposure
            pend.add(o.venue, o.side, o.qty)
        for waiting in self._po_wait.values():    # arrived at the exchange, PostOnly check not yet resolved: still the strategy's exposure
            for o in waiting:
                pend.add(o.venue, o.side, o.qty)
        return pend

    def _live_maker_orders(self, now: float) -> int:
        """Maker orders the strategy must treat as possibly executable: resting (incl. cancel-pending), in flight,
        and cancelled at the exchange but not yet acknowledged to the strategy."""
        n = len(self.maker.orders)
        if self.config.arb.maker_count_inflight:
            n += self.pending.maker_legs_in_flight()
            n += sum(len(w) for w in self._po_wait.values())
        if self._cancel_acks:
            self._cancel_acks = [t for t in self._cancel_acks if t > now]
            n += len(self._cancel_acks)
        return n

    def _advance_to(self, now: float, *, from_timer: bool = False) -> None:
        """Bring order state up to `now`, before the market event stamped `now` is applied (from_timer: the WP3 timer wheel is
        firing a deadline at its own time — there is no market event at `now`, so the equal-time policy does not apply).

        Order: in-flight orders land, then expiries/cancels take effect, then the caller applies the event. An order
        sent at t-latency therefore cannot see the book update arriving right now, and a cancel effective at or before
        `now` wins a tie against that event.
        """
        self._now = now
        for order in self.pending.release(now):
            self._execute_arrival(order, now)
        if self.config.arb.maker_max_age_s > 0 or self._cancel_watch:
            self._expire_maker_orders(now, from_timer=from_timer)

    def _close_out(self, now: float) -> None:
        """End of data. Orders still in flight land on the last book, resting orders are closed.

        Neither is a real outcome: the data simply stopped. They are counted separately (`maker_landed_after_data_end`,
        `taker_landed_after_data_end`, `resting_at_data_end`, `cancel_pending_at_data_end`) and never as an applied
        cancel. The closes only censor the fill-model observations; they add no fills.
        """
        for venue, waiting in self._po_wait.items():
            for o in waiting:                        # the feed ended before the exchange-time check could run: neither accepted nor rejected
                self.po["unresolved_at_data_end"] += 1
                t = self._trackers.get(o.signal_id)
                if t is not None:
                    t.legs_outstanding -= 1
            waiting.clear()
        self.lifetime["resting_at_data_end"] = len(self.maker.orders)
        self.lifetime["cancel_pending_at_data_end"] = sum(
            1 for o in self.maker.orders.values() if o.cancel_requested_ts is not None)
        for order in self.pending.drain():
            if order.kind == "recover":
                self.hedge["recovery_timers_pending_at_data_end"] += 1       # the strategy's reaction was still to come
                continue
            if order.ghost:
                self.hedge["ghost_orders_pending_at_data_end"] += 1
                continue
            self.lifetime["taker_landed_after_data_end" if order.is_taker else "maker_landed_after_data_end"] += 1
            self._execute_arrival(order, now)
        for mo in self.maker.orders.values():
            if mo.cancel_requested_ts is None:
                mo.cancel_reason = "end_of_data"
        for mo in self.maker.cancel_all(now):
            sid = self._maker_to_signal.get(mo.order_id, -1)
            t = self._trackers.get(sid)
            if t is not None:
                t.maker_resting = False
            self._maybe_resolve(sid, now)
        for sid in list(self._trackers):
            self._trackers[sid].legs_outstanding = 0
            self._trackers[sid].maker_resting = False
            self._maybe_resolve(sid, now)
        self._cancel_watch.clear()
        self._cancel_acks.clear()

    def run(self) -> ArbBacktestResult:
        cfg = self.config
        virtual_clock.install_virtual_clock(0.0)
        virtual_clock.reset(0.0)
        wall_start = virtual_clock.real_time()

        coverage = check_coverage(
            cfg.data_root, cfg.pair, cfg.venues, cfg.start_date, cfg.end_date,
        )
        feed = CrossVenueFeed(
            data_root=cfg.data_root, pair=cfg.pair, venues=cfg.venues,
            start_day=cfg.start_date, end_day=cfg.end_date, latency=self.latency,
            trust_local_ts={v: s.trust_local_ts for v, s in self.venue_specs.items()},
            include_deltas=cfg.include_deltas,
            quality_preflight=True,
            quality_report_only=_preflight_override(cfg) is not None,
            trade_dedupe=cfg.trade_dedupe,
        )

        now = 0.0
        try:
            for ev in (self._with_lookahead(feed) if self._truth_on else feed):
                if cfg.max_events is not None and self.events_processed >= cfg.max_events:
                    break
                now = ev.receive_ts
                virtual_clock.set_now(now)
                self.events_processed += 1
                self.events_by_venue[ev.venue] += 1
                self.events_by_kind[ev.kind] += 1

                if self.timelines is not None and self.timing_cfg.timer_wheel:
                    self._fire_timers_before(now)
                self._advance_to(now)
                self._apply_event(ev)
                self._record_mid_ts(ev.venue, now)
                if self._breaker is not None:
                    self._breaker_poll(now)
                    self._breaker_trip_check(now)

                if self.settler is not None:
                    self.settler.settle(now, self.inventory, self.books)
                if self.margin_model is not None:
                    self._margin_step(now)
                if self.exposure is not None:
                    self.exposure.update(now, self.inventory.base, self.inventory.total_cash,
                                         {v: b.mid for v, b in self.books.items()}, self._ref_mid())

                # Trades can move the touch too, via trade-tape repair.
                if self._touch_changed(ev.venue):
                    self.markouts.resolve(now, {v: b.mid for v, b in self.books.items()})
                    blocked_before = self.strategy.rejects["maker_limit"]
                    if self.faults is not None and self._feed_dark(now):
                        self.hedge["evaluations_skipped_feed_dark"] += 1
                        signal = None
                    elif self.timing_cfg is not None and self._dead_feed_blocks(now):
                        signal = None
                    elif cfg.enforce_book_state and self._book_state_blocks(now):
                        signal = None
                    elif self.alarm is not None:
                        self.hedge["signals_blocked_by_alarm"] += 1
                        signal = None
                    elif self._breaker is not None and not self._breaker.allow_signal(now, abs(self.inventory.net_base)):
                        self.hedge["signals_blocked_by_circuit_breaker"] = \
                            self.hedge.get("signals_blocked_by_circuit_breaker", 0) + 1
                        signal = None
                    else:
                        signal = self.strategy.evaluate(
                            self.books, self.states, self.inventory, now,
                            live_maker_orders=self._live_maker_orders(now),
                            pending=self._pending_exposure() if cfg.arb.count_pending_exposure else None,
                        )
                    if self._breaker is not None:
                        self._breaker.on_net_update(now, abs(self.inventory.net_base), self.pending.in_flight_count())
                        self._breaker.check_unknown_orders(now, abs(self.inventory.net_base), self.pending.in_flight_count())
                    if (self.strategy.rejects["maker_limit"] > blocked_before
                            and (self._cancel_acks or any(o.cancel_requested_ts is not None for o in self.maker.orders.values()))):
                        self.lifetime["quotes_blocked_by_unresolved_cancel"] += 1
                    if signal is not None:
                        if (self.faults is not None and self.faults.entry_guard
                                and any(self._active_outage(v, now) is not None for v in (signal.buy_venue, signal.sell_venue))):
                            self.hedge["signals_blocked_by_entry_guard"] += 1
                        elif self.margin_model is not None and not self.margin_model.allows(signal, self.inventory, self.books, self._reserved_notional()):
                            self.hedge["signals_blocked_by_margin"] += 1          # the venue could not post initial margin for the clip
                        else:
                            self._fire(signal, now)

                if cfg.progress_every and self.events_processed % cfg.progress_every == 0:
                    print(
                        f"  {self.events_processed:>12,} ev  "
                        f"sig={self.strategy.signals:<7,} fill={self.orders_filled:<7,} "
                        f"miss={self.orders_missed:<7,} "
                        f"pnl={self.gross_pnl - self.fees_paid - self.unhedged_loss:,.2f}",
                        flush=True,
                    )

            self._close_out(now)
            if self.exposure is not None:
                self.exposure.update(now, self.inventory.base, self.inventory.total_cash,
                                     {v: b.mid for v, b in self.books.items()}, self._ref_mid())

            self.markouts.resolve(now, {v: b.mid for v, b in self.books.items()}, finalize=True)
            return self._build_result(feed, coverage, virtual_clock.real_time() - wall_start)
        finally:
            virtual_clock.uninstall()

    # ── Result ───────────────────────────────────────────────────────────────

    def _equity_bridge(self, open_pos_value: float, ending_equity: float, valuation: dict | None = None) -> dict:
        """Ledger identity, checked at the end of every run (docs/strategy_contract.md section 4):
             cash rebuilt from the trade log (fills, fees) + funding + transfers == ledger cash
             ending_equity == ledger cash + open position marked at the COMMON timestamp (valuation.py: every venue at the last
             time all venues had data). The common-timestamp value is computed here, independently of the figure under test, so
             an ending_equity built from each venue's own last mid (the pre-R11 figure) fails on any day where one feed stopped
             early (SOL 2022-01-05: +$17.16 on a flat pair).
        Tolerance 1e-6 USDT, scaled by 1e-9 x traded notional for float accumulation. Skipped (and flagged) when the trade log was
        truncated by max_trade_log. Raises EquityBridgeError unless ArbBacktestConfig.equity_bridge == "warn"."""
        val = valuation if valuation is not None else self._common_valuation()
        truncated = len(self.trade_log) >= self.config.max_trade_log
        funding = -(self.settler.funding_paid if self.settler else 0.0)
        # Transfers move equity between the margin model's venue accounts; the ledger cash (InventoryBook) never sees them, so
        # they are reported here but are not part of the cash identity.
        transfers = sum(self.margin_model.transfer_cash.values()) if self.margin_model is not None else 0.0
        rebuilt = sum((-r["qty"] * r["price"] if r["side"] == "buy" else r["qty"] * r["price"]) - r["fee"] for r in self.trade_log) + funding
        tol = max(1e-6, 1e-9 * self.traded_notional)
        gap_cash = rebuilt - self.inventory.total_cash
        # WP5 — fee reconciliation: every fill's fee must equal the venue's schedule (maker / taker bps, plus the declared liquidation
        # fee on a liquidation) x its notional. Synthetic (BackgroundAgentModel) fills follow the maker schedule like any other.
        fee_mismatches: list[dict] = []
        for r in self.trade_log:
            fees = self.venue_specs[r["venue"]].fees
            bps = fees.taker_bps if r["taker"] else fees.maker_bps
            if r.get("kind") == "liquidation" and self.margin_model is not None:
                bps += self.margin_model.cfg.venues[r["venue"]].liquidation_fee_bps
            expected = r["qty"] * r["price"] * bps / 10_000.0
            if abs(expected - r["fee"]) > 1e-9 * max(1.0, r["qty"] * r["price"]):
                if len(fee_mismatches) < 20:
                    fee_mismatches.append({"ts": r["ts"], "venue": r["venue"], "kind": r.get("kind"), "fee": r["fee"], "expected": expected})
                else:
                    fee_mismatches[-1]["more"] = fee_mismatches[-1].get("more", 0) + 1
        # WP5 — per-venue identities (cash and base), tolerance 1e-9 scaled by the venue's traded notional
        per_venue: dict[str, dict] = {}
        funding_by_venue = self.settler.report()["by_venue"] if self.settler else {}
        for v in self.config.venues:
            rows = [r for r in self.trade_log if r["venue"] == v]
            cash_v = sum((-r["qty"] * r["price"] if r["side"] == "buy" else r["qty"] * r["price"]) - r["fee"] for r in rows) - funding_by_venue.get(v, 0.0)
            base_v = sum(r["qty"] if r["side"] == "buy" else -r["qty"] for r in rows)
            notional_v = sum(r["qty"] * r["price"] for r in rows)
            tol_v = 1e-9 * max(1.0, notional_v)
            per_venue[v] = {"ledger_cash": self.inventory.cash.get(v, 0.0), "cash_from_trade_log": cash_v, "funding": -funding_by_venue.get(v, 0.0),
                            "cash_gap": cash_v - self.inventory.cash.get(v, 0.0), "ledger_base": self.inventory.base.get(v, 0.0),
                            "base_from_trade_log": base_v, "base_gap": base_v - self.inventory.base.get(v, 0.0), "tolerance": tol_v,
                            "ok": truncated or (abs(cash_v - self.inventory.cash.get(v, 0.0)) <= tol_v and abs(base_v - self.inventory.base.get(v, 0.0)) <= 1e-9)}
        if val.get("available"):
            common_open = val["open_position_value"]
            valuation_basis = "common_timestamp"
        else:
            # No common timestamp exists (a venue never priced, or the history cannot reach common_ts): the identity can only be
            # checked against the marks handed in, and the result is flagged.
            common_open = open_pos_value
            valuation_basis = f"unavailable:{val.get('reason')}"
        gap_equity = ending_equity - (self.inventory.total_cash + common_open)
        gap_marks = open_pos_value - common_open                      # the figure under test vs the common-timestamp marks
        doc = {"checked": not truncated, "tolerance_usdt": tol, "ledger_cash": self.inventory.total_cash, "cash_from_trade_log": rebuilt,
               "funding": funding, "transfers": transfers, "open_position_value": open_pos_value, "ending_equity": ending_equity,
               "common_timestamp_open_position_value": common_open, "valuation_basis": valuation_basis,
               "common_ts": val.get("common_ts"), "marks": val.get("marks"), "mark_gap_s": val.get("mark_gap_s"),
               "venue_last_update_spread_s": val.get("venue_last_update_spread_s"), "lagging_venue_flag": val.get("lagging_venue_flag"),
               "cash_gap": gap_cash, "equity_gap": gap_equity, "mark_gap": gap_marks,
               "fee_mismatches": fee_mismatches, "per_venue": per_venue,
               "funding_predicted_at_signal": self._funding_predicted, "funding_realised": -funding,
               "ok": truncated or (abs(gap_cash) <= tol and abs(gap_equity) <= tol and not fee_mismatches and all(x["ok"] for x in per_venue.values()))}
        if not doc["ok"]:
            msg = (f"equity bridge failed: cash gap {gap_cash:.9f}, equity gap {gap_equity:.9f} (open position under test {open_pos_value:.6f} "
                   f"vs {common_open:.6f} at the common timestamp {val.get('common_ts')}), tolerance {tol:.3e}; "
                   f"fee mismatches {len(fee_mismatches)}; per-venue failures {[v for v, x in per_venue.items() if not x['ok']]}")
            if self.config.equity_bridge == "warn":
                warnings.warn(msg, RuntimeWarning, stacklevel=2)
            else:
                raise EquityBridgeError(msg)
        return doc

    def _legging_report(self) -> dict:
        """Legging exposure per resolved maker signal: maker fill -> hedge fill, from the recorded timestamps."""
        rows = self.legging_rows
        vals = sorted(r["legging_s"] for r in rows if r["legging_s"] is not None)
        q = lambda p: vals[min(len(vals) - 1, int(p * len(vals)))] if vals else None
        def dist(xs):
            xs = sorted(xs)
            qq = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None
            return {"n": len(xs), "mean": (sum(xs) / len(xs)) if xs else None, "p10": qq(0.1), "p50": qq(0.5), "p90": qq(0.9), "max": xs[-1] if xs else None}
        moves = {side: dist([r["hedge_move_bps"] for r in rows if r["side"] == side and r["hedge_move_bps"] is not None]) for side in ("bid", "ask")}
        exec_moves = {side: dist([r["hedge_exec_move_bps"] for r in rows if r["side"] == side and r["hedge_exec_move_bps"] is not None]) for side in ("bid", "ask")}
        return {"signals_with_maker_fill": len(rows), "hedged_signals": len(vals),
                "unhedged_after_maker_fill": sum(1 for r in rows if r["legging_s"] is None),
                "legging_s": {"n": len(vals), "mean": (sum(vals) / len(vals)) if vals else None, "p50": q(0.5), "p90": q(0.9), "max": vals[-1] if vals else None},
                # Hedge venue move between quote time and first maker fill, conditional on a fill, by passive side. NEGATIVE = adverse.
                # This is the empirical input to ArbConfig.expected_hedge_move_bps_{bid,ask}.
                "hedge_move_bps": moves,
                # Quote-time touch -> executed hedge VWAP (touch move + flight + slippage), by passive side. NEGATIVE = adverse.
                # This is the empirical estimate of E[hedge price | fill] - quote: the input to ArbConfig.expected_hedge_move_bps_{bid,ask}.
                "hedge_exec_move_bps": exec_moves,
                "rows": rows[:5000]}

    # ── Margin / liquidation / transfers (margin_model.py) ───────────────────

    def _reserved_notional(self) -> dict[str, float]:
        """WP5: notional of resting maker orders per venue (their initial margin is reserved at the venue)."""
        out: dict[str, float] = {}
        for o in self.maker.orders.values():
            out[o.venue] = out.get(o.venue, 0.0) + o.remaining * o.price
        return out

    def _margin_step(self, now: float) -> None:
        """Run the margin model; execute any liquidation (and the matching flatten) it orders against the live books. Liquidations
        trade at market through the same taker path as an unwind; their fee is charged to cash and reported on its own line."""
        for venue, side, qty, fee_bps in self.margin_model.step(now, self.inventory, self.books):
            book = self.books[venue]
            if qty <= 0 or not book.ready:
                continue
            flatten = fee_bps < 0
            # The liquidation fee rides on the fill itself (taker fee + liquidation fee), so cash, fees_paid and the trade log
            # agree; the model reports the liquidation part separately.
            out = self.taker.execute(book=book, side=side, qty=qty, limit_price=float("inf") if side == "buy" else 0.0,
                                     fee_bps=self.venue_specs[venue].fees.taker_bps + (0.0 if flatten else fee_bps), now=now,
                                     expected_price=book.mid, signal_id=0)
            if out.filled and out.fill is not None:
                f = out.fill
                self.orders_filled += 1
                self._fill_kind = "flatten" if flatten else "liquidation"
                try:
                    self._on_fill(f)
                finally:
                    self._fill_kind = None
                self.margin_model.record_liquidation(venue, f.qty, 0.0 if flatten else f.notional * fee_bps / 10_000.0, flatten)

    def _build_result(self, feed: CrossVenueFeed, coverage, wall_s: float) -> ArbBacktestResult:
        cfg = self.config
        if self.margin_model is not None:
            self.margin_model.finish(getattr(self, "_now", 0.0) or 0.0)
        mids = {v: b.mid for v, b in self.books.items()}

        # Opt-in single-venue marking (Task 5): override per-venue mids with one venue's price.
        mark_venue = cfg.arb.mark_inventory_venue
        if mark_venue is not None and mark_venue in mids:
            ref_mid = mids[mark_venue]
            mids = {v: ref_mid for v in cfg.venues}

        # R11 — two valuations of the same open position. last_mark: each venue at its own last mid (the pre-2026-11-01 figure;
        # overstated whenever one feed stops before the other). common_timestamp: every venue at the last time ALL venues had
        # data (valuation.py). The headline ending_equity uses cfg.valuation; the bridge always checks against the common one.
        last_mark_open = sum(self.inventory.base.get(v, 0.0) * mids.get(v, 0.0) for v in cfg.venues)
        last_mark_equity = self.inventory.total_cash + last_mark_open
        val = self._common_valuation()
        if val.get("available"):
            common_timestamp_equity = val["equity"]
            common_staleness = val["venue_last_update_spread_s"]
        else:
            common_timestamp_equity = last_mark_equity
            common_staleness = float("inf")
            warnings.warn(f"common-timestamp valuation unavailable ({val.get('reason')}); ending_equity falls back to each venue's "
                          "last mark and is flagged", RuntimeWarning, stacklevel=2)
        if cfg.valuation == "common_timestamp" and val.get("available"):
            open_pos_value = val["open_position_value"]
            ending_equity = common_timestamp_equity
        else:
            open_pos_value = last_mark_open
            ending_equity = last_mark_equity
        val = dict(val, headline=cfg.valuation, last_mark_equity=last_mark_equity, last_mark_open_position_value=last_mark_open,
                   last_marks=dict(mids), stale_mark_inflation=last_mark_equity - common_timestamp_equity)
        mtm = ending_equity  # legacy alias
        self.equity_bridge = self._equity_bridge(open_pos_value, ending_equity, val)
        self.valuation_doc = val

        # Task 5 — cash_pnl: direct ledger reading (fees already deducted in cash_delta).
        cash_pnl = self.inventory.total_cash

        n = max(1, self.edge_samples)
        mean_exp = self.expected_gross_sum / n
        mean_real = self.realized_gross_sum / n
        mean_net = self.realized_net_sum / n
        capture = (mean_real / mean_exp) if abs(mean_exp) > 1e-12 else 0.0
        mean_slip = self.slippage_sum / self.slippage_count if self.slippage_count else 0.0

        feed_report = feed.report()
        for venue, rep in feed_report.items():
            lat = None if venue.startswith("_") else rep.get("mean_feed_latency_vs_host_ms")
            if lat is not None and lat < -CLOCK_OFFSET_WARN_MS:
                warnings.warn(
                    f"{venue}: recorded receive times average {-lat:.1f} ms BEFORE the exchange timestamps after "
                    "host-clock correction. The collector clock for this venue is probably offset; the replay then "
                    "shows this venue's updates early and results can be look-ahead biased.",
                    RuntimeWarning, stacklevel=2)

        fill_log_path = bucket_stats_path = trades_path = order_events_path = None
        if cfg.output_dir:
            out = Path(cfg.output_dir)
            out.mkdir(parents=True, exist_ok=True)
            (out / "model_settings.json").write_text(json.dumps(self.model_settings(), indent=1, sort_keys=True), encoding="utf-8")
            fill_log_path = str(out / "fill_events.jsonl")
            bucket_stats_path = str(out / "fill_bucket_stats.json")
            trades_path = str(out / "trades.jsonl")
            self.fill_model.save_event_log(fill_log_path)
            self.fill_model.save_bucket_stats(bucket_stats_path)
            with open(trades_path, "w", encoding="utf-8") as fh:
                for row in self.trade_log:
                    fh.write(json.dumps(row, sort_keys=True) + "\n")
            if cfg.record_order_events:
                order_events_path = str(out / "order_events.jsonl")
                with open(order_events_path, "w", encoding="utf-8") as fh:
                    for row in self.events.as_rows():
                        fh.write(json.dumps(row, sort_keys=True) + "\n")

        return ArbBacktestResult(
            pair=cfg.pair, venues=list(cfg.venues),
            start_date=cfg.start_date, end_date=cfg.end_date,
            mode=cfg.arb.mode, latency_preset=cfg.latency_preset,
            events_processed=self.events_processed,
            events_by_venue=dict(self.events_by_venue),
            events_by_kind=dict(self.events_by_kind),
            wall_seconds=wall_s,
            signals_generated=self.strategy.signals,
            signals_completed=self.signals_completed,
            signals_fully_hedged=self.signals_fully_hedged,
            signals_unhedged=self.signals_unhedged,
            signals_both_missed=self.signals_both_missed,
            orders_sent=self.orders_sent,
            orders_filled=self.orders_filled,
            orders_missed=self.orders_missed,
            gross_pnl=self.gross_pnl,
            fees_paid=self.fees_paid,
            unhedged_loss=self.unhedged_loss,
            funding_paid=self.settler.funding_paid if self.settler else 0.0,
            realized_pnl=(self.gross_pnl - self.fees_paid - self.unhedged_loss
                          - (self.settler.funding_paid if self.settler else 0.0)),
            open_position_value=open_pos_value,
            ending_equity=ending_equity,
            mark_to_market_pnl=mtm,
            cash_pnl=cash_pnl,
            common_timestamp_equity=common_timestamp_equity,
            common_mid_staleness_s=common_staleness,
            last_mark_equity=last_mark_equity,
            valuation=self.valuation_doc,
            attribution_pnl=(self.gross_pnl - self.fees_paid - self.unhedged_loss - (self.settler.funding_paid if self.settler else 0.0)),
            funding_report=self.settler.report() if self.settler else {},
            alarms=list(self.alarms),
            alarm_active=self.alarm,
            release_identity=cfg.release_identity,
            product_by_venue=dict(self.product_by_venue),
            strategy_validation_label=("UNVALIDATED STRATEGY - mechanics only" if any(p == "spot" for p in self.product_by_venue.values()) else ""),
            basis_component=((val.get("cross_venue_basis_at_common_ts") or 0.0) * -self.inventory.base.get(cfg.venues[0], 0.0)
                             if cfg.product == "spot_perp" and val.get("available") else None),
            single_venue_diagnostic=dict(self._stale_event_counts),
            single_venue_intervals=self._single_venue_event_count,
            mean_expected_gross_edge_bps=mean_exp,
            mean_realized_gross_edge_bps=mean_real,
            edge_capture_ratio=capture,
            mean_realized_net_edge_bps=mean_net,
            mean_slippage_bps=mean_slip,
            final_inventory=dict(self.inventory.base),
            final_cash=dict(self.inventory.cash),
            final_mids=mids,
            net_base_position=self.inventory.net_base,
            traded_notional=self.traded_notional,
            strategy_report=self.strategy.report(),
            feed_report=feed_report,
            latency_report=self.latency.report(),
            book_stats={v: b.stats() for v, b in self.books.items()},
            maker_stats=self.maker.stats(),
            markout_summary=self.markouts.summary(),
            miss_reasons=dict(self.miss_reasons),
            coverage={k: list(v) for k, v in coverage.items()},
            fill_log_path=fill_log_path,
            bucket_stats_path=bucket_stats_path,
            trades_path=trades_path,
            data_quality=_data_quality(feed, cfg),
            maker_lifetime=self._lifetime_report(),
            maker_diagnostics=self.maker.diagnostics(),
            model_settings=self.model_settings(),
            hedge_report=self._hedge_report(self.exposure.last_ts if self.exposure is not None and self.exposure.last_ts else 0.0),
            order_event_summary=self.events.summary(),
            postonly_report=self._postonly_report(),
            margin_report=self.margin_model.report(self.inventory, self.books) if self.margin_model is not None else {},
            legging_report=self._legging_report(),
            timing_report=self._timing_report(),
            equity_bridge=self.equity_bridge,
            signal_log=self.signal_log[:self.config.max_trade_log],
            order_events_path=order_events_path,
        )

    def _postonly_report(self) -> dict:
        c = self.po
        n = max(1, c["checks"])
        return {
            "model": self.config.postonly_reject_model, "ack_latency": self.ack.describe(),
            "notice": {"fixed_s": self.config.postonly_notice_s,
                       "source": "fixed" if self.config.postonly_notice_s is not None else ("ack_latency_source" if self.ack.active else "venue_feed_latency_preset")},
            "counters": {k: float(v) for k, v in sorted(c.items())},
            "reject_rate_of_checked": c["rejects"] / n if c["checks"] else None,
            "mean_wait_for_exchange_time_s": c["wait_s_sum"] / n if c["checks"] else None,
            "mean_notice_delay_s": c["notice_delay_s_sum"] / max(1, c["notices"]) if c["notices"] else None,
        }

    def model_settings(self) -> dict:
        m = self.config.maker
        return {
            "postonly_reject_model": self.config.postonly_reject_model, "cancel_time_basis": self.config.cancel_time_basis,
            "funding_rate_dir": self.config.funding_rate_dir,
            "funding_records_loaded": self.settler.loaded_records if self.settler is not None else 0,
            "count_pending_exposure": self.config.arb.count_pending_exposure, "pending_exposure_auto_enabled": self.pending_exposure_auto_enabled,
            "count_unknown_exposure": self.config.arb.count_unknown_exposure,
            "max_position_base": self.config.arb.max_position_base,
            "max_net_base_hard": self.config.arb.max_net_base_hard,
            "fill_headroom_base": self.config.arb.fill_headroom_base,
            "ack_latency_source": self.config.ack_latency.source, "fault_preset": getattr(self.faults, "label", "") if self.faults else "",
            **self._model_settings_core(m),
        }

    def _model_settings_core(self, m) -> dict:
        return {
            "cancel_model": m.effective_cancel_model, "cancel_model_explicit": m.cancel_model is not None,
            "cancel_power_n": m.cancel_power_n, "delta_fill_share": m.delta_fill_share, "trade_fill_share": m.trade_fill_share,
            "snapshot_fill_share": m.snapshot_fill_share, "snapshot_netting": m.snapshot_trade_explain,
            "depth_size_fix": self.config.delta_uses_book_size, "dedupe_trade_depth": m.dedupe_trade_depth,
            "dedupe_time_basis": m.dedupe_time_basis, "dedupe_window_s": m.dedupe_window_s,
            "exchange_time_eligibility": m.exchange_time_eligibility, "fill_through_prints": m.fill_through_prints,
            "queue_stress_multiplier": m.queue_stress_multiplier, "min_queue_ahead": m.min_queue_ahead,
            "trade_dedupe": self.config.trade_dedupe,
        }

    def _lifetime_report(self) -> dict[str, float]:
        lt = self.lifetime
        cancels = max(1, lt["cancels_applied"])
        keys = ("expiries_triggered", "cancels_applied", "cancels_lost_to_fill", "expired_after_partial_fill",
                "fills_while_cancel_pending", "qty_filled_while_cancel_pending", "quotes_blocked_by_unresolved_cancel",
                "resting_at_data_end", "cancel_pending_at_data_end", "maker_landed_after_data_end",
                "taker_landed_after_data_end")
        out = {k: float(lt[k]) for k in keys}
        out["mean_age_at_cancel_s"] = lt["age_at_cancel_sum_s"] / cancels
        out["mean_cancel_pending_s"] = lt["cancel_pending_time_sum_s"] / cancels
        return out


def _preflight_override(cfg: ArbBacktestConfig) -> str | None:
    if cfg.allow_rejected_data is None:
        return None
    if not cfg.allow_rejected_data.strip():
        raise ValueError("allow_rejected_data needs a written justification")
    return cfg.allow_rejected_data.strip()


def _data_quality(feed, cfg: ArbBacktestConfig) -> dict:
    preflight = {
        venue: {"decision": report.get("decision"), "health": report.get("health"),
                "skipped": bool(report.get("skipped")),
                "active_flag_codes": [flag.get("code") for flag in report.get("active_flags", [])]}
        for venue, report in sorted(getattr(feed, "preflight_reports", {}).items())   # a stub feed has none
    }
    rejected = [venue for venue, row in preflight.items() if row["health"] == "CRITICAL"]
    override = _preflight_override(cfg)
    return {
        "preflight": preflight,
        "blocking": override is None,
        "rejected_venues": rejected,
        "override": ({"justification": override, "rejected_venues": rejected}
                     if override is not None else None),
    }


def _infer_tick(bids, asks) -> float:
    prices = sorted({float(l[0]) for l in list(bids) + list(asks) if len(l) >= 2})
    diffs = [b - a for a, b in zip(prices, prices[1:]) if b > a]
    if diffs:
        return min(diffs)
    return (prices[0] if prices else 1.0) * 1e-6


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Cross-venue (Bybit x Binance) arbitrage backtester",
    )
    p.add_argument("--pair", default="BTCUSDT")
    p.add_argument("--data-root", default="data")
    p.add_argument("--start", dest="start_date", default=DEFAULT_START_DATE)
    p.add_argument("--end", dest="end_date", default=DEFAULT_END_DATE)
    p.add_argument("--venues", nargs="+", default=[BYBIT, BINANCE])
    p.add_argument("--max-events", type=int)
    p.add_argument("--mode", choices=[TAKER_TAKER, MAKER_TAKER], default=TAKER_TAKER)
    p.add_argument("--latency-preset", choices=sorted(LATENCY_PRESETS),
                   default=DEFAULT_LATENCY_PRESET)
    p.add_argument("--bybit-fees", default="bybit_perp_mm")
    p.add_argument("--binance-fees", default="binance_perp_vip0")
    p.add_argument("--min-edge-bps", type=float, default=1.0)
    p.add_argument("--safety-buffer-bps", type=float, default=0.5)
    p.add_argument("--max-book-age-ms", type=float, default=500.0)
    p.add_argument("--max-touch-age-ms", type=float, default=300.0,
                   help="refuse to hit a best level not confirmed within this window")
    p.add_argument("--max-walk-bps", type=float, default=2.0)
    p.add_argument("--max-clip-notional", type=float, default=25_000.0)
    p.add_argument("--max-position-base", type=float, default=0.0,
                   help="0 => per-pair default from config.MAX_POSITION_BASE")
    p.add_argument("--min-queue-ahead", type=float, default=0.0,
                   dest="min_queue_ahead",
                   help="MakerConfig.min_queue_ahead: queue-depth floor for absent "
                        "price levels (base-asset units). 0 = no floor (default). "
                        "Calibrate from the p25 of observed present-level depths.")
    p.add_argument("--no-toxic-gate", action="store_true")
    p.add_argument("--no-consume", action="store_true",
                   help="do not remove liquidity our own taker orders consumed")
    p.add_argument("--no-deltas", action="store_true",
                   help="snapshots only — much faster, much coarser")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--output-dir")
    p.add_argument("--warm-start", dest="warm_start_fill_events")
    p.add_argument("--progress-every", type=int, default=0)
    p.add_argument("--funding-rate-dir", default=None,
                   help="directory of *.jsonl funding-rate files (opt-in, see FundingSettler)")
    p.add_argument("--allow-rejected-data", metavar="JUSTIFICATION",
                   help="run even if the data-quality preflight rejects the data; "
                        "the justification is recorded in the result")
    return p


def config_from_args(args) -> ArbBacktestConfig:
    arb = ArbConfig(
        mode=args.mode,
        min_net_edge_bps=args.min_edge_bps,
        safety_buffer_bps=args.safety_buffer_bps,
        max_book_age_s=args.max_book_age_ms / 1000.0,
        max_touch_age_s=args.max_touch_age_ms / 1000.0,
        max_walk_bps=args.max_walk_bps,
        max_clip_notional=args.max_clip_notional,
        max_position_base=args.max_position_base,
        block_on_toxic=not args.no_toxic_gate,
    )
    return ArbBacktestConfig(
        pair=args.pair, data_root=args.data_root, venues=tuple(args.venues),
        start_date=args.start_date, end_date=args.end_date,
        max_events=args.max_events, latency_preset=args.latency_preset,
        bybit_fee_preset=args.bybit_fees, binance_fee_preset=args.binance_fees,
        arb=arb, maker=MakerConfig(min_queue_ahead=args.min_queue_ahead),
        consume_liquidity=not args.no_consume,
        include_deltas=not args.no_deltas, seed=args.seed,
        output_dir=args.output_dir,
        warm_start_fill_events=args.warm_start_fill_events,
        progress_every=args.progress_every,
        funding_rate_dir=args.funding_rate_dir,
        allow_rejected_data=args.allow_rejected_data,
    )


def main(argv: Iterable[str] | None = None) -> int:
    args = build_arg_parser().parse_args(list(argv) if argv is not None else None)
    cfg = config_from_args(args)
    result = CrossVenueArbBacktester(cfg).run()
    payload = json.dumps(asdict(result), indent=2, sort_keys=True, default=str)
    print(payload)
    if cfg.output_dir:
        Path(cfg.output_dir, "result.json").write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
