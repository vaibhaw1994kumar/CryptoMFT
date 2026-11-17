"""Session replay (Phase D, step D5): the replay driver and result adapter for `runtime: session`.

The arb strategy runs on the core `TradingSession` behind the `QueueAwareVenueAdapter`, fed by the canonical feed converter. This file only DRIVES (feed, per-venue market state,
adapters, result files); every rule it uses lives in `crypto_stat_arb` (config from the profile, the session, the gate, the invalid-book policy, the queue adapter, the strategy
shim). Its replay loop retires with the backtester's in Phase F.

Order of work for one market event (the same for every venue):
  1. a book whose resync wait is over is resynced FIRST (session.resync_if_due), from the feed's own state at that time;
  2. the core book applies the event;
  3. the per-venue float `VenueState` (owned here, shared with the strategy and the adapters) reads the float mirror of the core book;
  4. `TradingSession.on_event(apply_book=False)`: gate, OMS, strategy, dispatch.
A trade print reaches the venue state first and then the session's trade hook (queue fills come from prints only).

Differences from the backtester's loop, recorded in the manifest (nothing here pretends to be the same loop). Timing IS shared since 9 Nov: the profile's timing block (latency preset, floors, jitter) drives
the feed delay, the clock and every order delay through `Timelines` / `TimelinesLatencyModel`, and the venue-truth books are built here (`TruthBooks`) for taker arrivals and PostOnly checks. What the session runtime still LACKS
(owner audit 10 Nov; each item has a Phase E step in the plan): E0 the ems/ layer (S0 repricing `decide_reprice`, the tactic switch, PASSIVE placements, risk gate, pairing, venue selector never reach this path); E1 the maker order
lifetime as a TIMER (`oms.order_lifetime_ns` exists and fires only when a market event or `tick` advances the OMS clock); E2 hedge-fault injection is built and opt-in (hedge_fault_injection=True: outage_on_fill on the hedge venue with ghost hedge, partial hedge; the other fault kinds are refused; the fault presets and the other conditional scenarios do not run here); E3 the participation cap and the size sweep; the circuit breaker (`circuit_breaker.py` is not wired into the session); the
backtester's own margin step (the core margin engine replaces it). A book that ends a message crossed is invalidated by the core (the backtester repairs it), and the sequence numbers are SYNTHETIC. Parity is explained, not forced (prompt, PHASE D).
"""
from __future__ import annotations

import gzip
import json
import dataclasses
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from crypto_stat_arb.config import check_real_order_profile, config_from_raw, feed_health_config_report, feed_health_from_profile, hedge_dust_fields, raw_config_from_profile, session_limits_from_profile
from crypto_stat_arb.derivatives import DerivativeTicker
from crypto_stat_arb.metrics import regime_sample
from crypto_stat_arb.ems.layer import hedge_policy_fields
from crypto_stat_arb.execution import DispatchPolicy
from crypto_stat_arb.hedge_lifecycle import HedgePolicy
from crypto_stat_arb.models import EventType, MarketEvent, TradeEvent
from crypto_stat_arb.session import ExecutionMode, TradingSession
from crypto_stat_arb.ems.venue_selector import SplitVenue, split_single_venue
from crypto_stat_arb.simulation.impact import CurveImpact, DisplayedDepthImpact, ImpactOverlay, OwnTakeLedger
from crypto_stat_arb.simulation.order_book import float_mirror
from crypto_stat_arb.simulation.queue_adapter import QueueAwareVenueAdapter, SimulatedVenueFeeder
from crypto_stat_arb.simulation.venue_state import VenueState
from crypto_stat_arb.strategies.arb_strategy import ArbSessionStrategy, CrossVenueArbStrategy

from .canonical_feed import CanonicalFeed
from .config import core_fee_specs, make_venues
from .cross_venue_feed import CrossVenueFeed
from .backtester import _preflight_override
from .fill_engine import MakerExecutor
from .fill_model import EmpiricalFillModel
from .latency_model import LatencyBook
from .timing import FixedOffsetAligner, Timelines, TimelinesLatencyModel, TruthBooks, dead_feed_blocks
from collections import Counter
from .event_loader import BOOK, DELTA, delta_fields, files_read, note_file_read, reset_files_read, snapshot_levels
from .order_book import OrderBook

# The dated price grid per dataset (the Phase B data checks' ticks) and the quantity step. The step is ASSUMED: no instrument master is stored for these datasets (lot NOT_MEASURED).
DATED_TICKS = {"2022": {"bybit": 0.005, "binance": 0.001}, "2024": {"bybit": 0.001, "binance": 0.001},
               "2026": {"bybit": 0.01, "binance": 0.01}}          # 2026: the published rules of 8 Nov 2026 (config/instrument_rules_solusdt_perp.json), checked against the recorded 2026-10-29 books
_RULES_FILE = Path(__file__).resolve().parents[2] / "config" / "instrument_rules_solusdt_perp.json"


def published_rules() -> dict:
    """The venues' PUBLISHED CURRENT SOLUSDT perpetual rules (lot step, minimum quantity, tick), fetched from their public exchange-info endpoints on 8 Nov 2026 with the source and date in the file
    (REF03/REF04). Current, not historical: the data's own grid is the dated tick above; the lot steps of the data dates are not published, so the current steps are applied and labelled so."""
    return json.loads(_RULES_FILE.read_text())


def quantity_steps() -> dict[str, float]:
    return {v: float(row["quantity_step"]) for v, row in published_rules()["venues"].items()}
_NS = 1_000_000_000


def dated_ticks(start_date: str) -> dict[str, float]:
    return dict(DATED_TICKS[start_date[:4]])


class TracedRejects(dict):
    """A reject counter that also records WHEN each reject happened: (evaluation time, reason). Observation only (owner 10 Nov, the event reconciliation): the strategy increments the same keys as ever; the trace is read by the
    reconciliation wrapper and by nothing else. Enabled by `trace_rejects` on the run config; off by default."""

    def __init__(self, base: dict, strategy, sink: list) -> None:
        super().__init__(base)
        self._strategy, self._sink = strategy, sink

    def __setitem__(self, key, value) -> None:
        if value > self.get(key, 0):
            self._sink.append((getattr(self._strategy, "last_eval_ts", None), key))
        super().__setitem__(key, value)


def profile_safety_values(profile: dict, profile_safety: bool, layer_risk_mode: str | None = None, breaker_config=None):
    """The layer risk mode and the circuit breaker a session runs with (owner 13 Nov). With `profile_safety` they are the PROFILE's own: `execution.risk_reservation` (mandatory: enforce) and the `breaker`
    block. Without it: count and no breaker (every replay before 13 Nov). An explicit value always wins. Returns (layer_risk_mode, breaker_config)."""
    if layer_risk_mode is None:
        layer_risk_mode = str(((profile.get("execution") or {}).get("risk_reservation")) or "count") if profile_safety else "count"
        if layer_risk_mode == "off":
            layer_risk_mode = "count"
    if profile_safety and breaker_config is None and profile.get("breaker"):
        from crypto_stat_arb.circuit_breaker import CircuitBreakerConfig
        b = profile["breaker"]
        breaker_config = CircuitBreakerConfig(consecutive_failures=b["consecutive_failures"], failure_rate_window_n=b["failure_rate_window_n"], failure_rate_thresh=b["failure_rate_thresh"],
                                              trip_on_failed_unwind=b["trip_on_failed_unwind"], net_exposure_s=b["net_exposure_s"], unknown_order_s=b["unknown_order_s"],
                                              trip_on_gateway_down=b["trip_on_gateway_down"], heartbeat_timeout_s=b["heartbeat_timeout_s"], tripped_unwind_retries=b["tripped_unwind_retries"],
                                              pause_on_gateway_down_when_flat=True)      # a gateway silent while flat PAUSES (resumes by itself); with exposure it TRIPS (manual reset): the replay's setting, not a profile key
    return layer_risk_mode, breaker_config


def profile_safety_default(profile: dict) -> bool:
    """Whether a profile is run with its OWN safety values (its breaker block, its layer risk mode) when the caller does not say: v1_9 and later, the profiles that carry the 13 Nov production
    limits, recognised by `hedge_faults.silent_carry_bound: below_one_lot` (a real-orders requirement no earlier profile meets). Earlier profiles keep the replay's defaults (count, no breaker), so
    their paper sessions match their replays. The day-1 run wrappers pass the same choice by profile name; a paper session reads it here."""
    return str((profile.get("hedge_faults") or {}).get("silent_carry_bound", "")) == "below_one_lot"


def profile_session_rules(config, profile: dict, names: dict, *, hedge_policy, latency_model, layer_risk_mode: str, breaker_config, layer_route_mode: str = "count") -> dict:
    """W2a (owner 17 Nov): the TradingSession arguments that are RULES OF THE PROFILE, in one place, for the replay (`SessionReplay`) and the paper session (`live_paper_setup`) alike. Nothing here
    depends on where the events come from. A new profile rule is added HERE, once."""
    return {
        "dispatch_policy": DispatchPolicy.FILL_TRIGGERED, "maker_venues": tuple(names[v] for v in config.arb.maker_venues), "latency_model": latency_model, "hedge_policy": hedge_policy,
        "gate_on_every_event": True,          # owner 9 Nov (D7d): the session runtime is v1_5 and later; the gate is evaluated on every event, as the live service does
        "layer_route_mode": layer_route_mode, "breaker_config": breaker_config,      # owner 12 Nov (2k): the circuit breaker, off unless a config is passed
        "execution_config": getattr(config, "execution", None),      # owner 13 Nov: the layer (and so the manifest) carries the profile's own execution block, not the defaults
        "layer_risk_mode": layer_risk_mode,   # E0 step (c): count = the layer's base-ceiling gate only counts; enforce = it refuses before the OMS reserves anything
        "intent_journal": True,               # E0 step (a) (owner 10 Nov): the orders are journaled as layer intents next to the OMS plan; observation only
        "markout_horizons_s": tuple(config.markout_horizons_s),          # owner 9 Nov: forward markouts per fill, by venue and role (the core's MarkoutTracker)
        "feed_health": feed_health_from_profile(profile),             # W1 (owner 17 Nov): None (nothing built) for every profile without the block
        "session_limits": session_limits_from_profile(profile, float(config.arb.max_position_base)),      # owner 16 Nov (Order 3): None for every profile without the block
    }


class SessionReplay:
    """One replay on the session. `stream` is any iterable of `TimedEvent` built with `decimal_numbers=True` (the real feed or a synthetic list); consumed lazily."""

    def __init__(self, config, profile: dict, *, stream: Iterable | None = None, ticks: dict[str, float] | None = None, recorded: bool = False,
                 decompose_markouts: bool = False, mark_from_mid: bool = False, hedge_fault_injection: bool = False, hedge_thin_book: tuple = (), layer_risk_mode: str | None = None, reconcile_mode: str = "count", observations: bool = True, layer_route_mode: str = "count",
                 lifecycle_report: bool = False, regime_boundaries=None, breaker_config=None, profile_safety: bool = False, hedge_min_notional: float | None = None) -> None:
        if config.runtime != "session":
            raise ValueError("SessionReplay runs the session runtime only")
        self.config, self.profile = config, profile
        # Safety switches from the PROFILE (owner 13 Nov): with profile_safety the layer risk gate runs in the profile's own mode (execution.risk_reservation, mandatory: enforce) and the circuit breaker is the profile's
        # `breaker` block, instead of the replay's own defaults (count, no breaker). An explicit layer_risk_mode / breaker_config still wins. Off (the default) = every earlier replay is unchanged.
        self.safety_from_profile: dict = {"profile_safety": profile_safety}
        layer_risk_mode, breaker_config = profile_safety_values(profile, profile_safety, layer_risk_mode, breaker_config)
        self.safety_from_profile.update(layer_risk_mode=layer_risk_mode, breaker=None if breaker_config is None else dataclasses.asdict(breaker_config),
                                        layer_route_mode=layer_route_mode, reconcile_mode=reconcile_mode,
                                        not_set_by_the_profile="layer_route_mode and reconcile_mode have no profile key (count); risk_gate_mode is off in the session")
        # Decomposed markouts (owner 11 Nov, the 09 Nov note): captured / adverse / M / fee / net per fill and horizon, from the replay's own fills, with the mid BEFORE the event that caused the fill
        # (m(t-)) and the mid in force at each horizon. Observation only; off by default, so no other run changes.
        # Lifecycle report by regime (owner 12 Nov, v1_9_TOLERANCES.md section D): off unless asked; one sample per second of the maker venue's view book (volatility, spread, touch liquidity). regime_boundaries = a dict or the path of the
        # training-day boundaries file; without it only the UTC-hour split exists.
        self.lifecycle_report = lifecycle_report
        self._regime_samples: dict[int, tuple[float, float, float]] = {}
        self._regime_prev_mid: float | None = None
        self._regime_last_sec: int | None = None
        if isinstance(regime_boundaries, (str, Path)):
            regime_boundaries = json.loads(Path(regime_boundaries).read_text())["boundaries"]
        self.regime_boundaries = regime_boundaries
        self.decomp = None
        if decompose_markouts:
            from crypto_stat_arb.simulation.latency_model import MarkoutTracker
            self.decomp = MarkoutTracker(tuple(config.markout_horizons_s), decompose=True)
        self._mid_pre: dict[str, float] = {}
        self._decomp_pos = 0
        self.decomp_skipped_no_mid = 0
        # Marks for the margin check (owner 11 Nov): the venue's own mark price from the recorded derivatives stream wins when it is present (`_derivative`). With `mark_from_mid` and no exchange mark for a venue, the
        # replay supplies the last VALID book mid as that venue's mark, at most every 100 ms, labelled MARK_FROM_MID in the manifest: a book that goes quiet for 500 ms then no longer drops its mark and trips the margin
        # check and the kill switch (every margin breach of the 2026-10-29 run sat inside a BINANCE:STALE interval). Replay only: the core's rule (fail closed on a missing mark) is unchanged.
        self.mark_from_mid = mark_from_mid
        self._exchange_mark_venues: set[str] = set()
        self._last_synth_mark_ns: dict[str, int] = {}
        self.mark_ticks = {"exchange_mark": 0, "MARK_FROM_MID": 0}
        reset_files_read()                # W2a: the input files this replay opens are noted from here on (the run record hashes them)
        self.first_ns = None              # the receive time of the first event processed; with last_ns the data span of the run record
        self.wall_s = None
        self.recorded = recorded          # the input is a recorded live session (core events already): `run_recorded`; no venue-truth books (they are built from raw research events)
        venues = tuple(config.venues)
        self.venues = venues
        self.names = {v: v.upper() for v in venues}
        fee_specs, self.fee_rows = core_fee_specs(config.pair, config.bybit_fee_preset, config.binance_fee_preset)
        specs = make_venues(config.latency_preset, config.bybit_fee_preset, config.binance_fee_preset, fee_specs=fee_specs)
        self.venue_specs = {v: specs[v] for v in venues}
        for v, trust in (config.trust_local_ts or {}).items():          # the profile's run-level setting, applied exactly as the backtester does
            if v in self.venue_specs:
                self.venue_specs[v] = replace(self.venue_specs[v], trust_local_ts=bool(trust))
        self.ticks = ticks or dated_ticks(config.start_date)
        size = float(config.arb.max_position_base)
        raw, self.config_notes = raw_config_from_profile(profile, pair=config.pair, venues=venues, ticks=self.ticks, quantity_steps={v: quantity_steps()[v] for v in venues}, size_base=size,
                                                         seed=config.seed, venue_cap_base=float(config.arb.max_venue_imbalance_base) or None,
                                                         min_notional_by_venue=None if hedge_min_notional is None else {v: repr(hedge_min_notional) for v in venues if v not in config.arb.maker_venues})        # owner 16 Nov: a run option, the hedge venue's minimum notional (dust cases)
        self.app_config, self.raw_config = config_from_raw(raw)
        self.config_notes["fees"] = "PROFILE_FEES: a replay books the profile's fee_presets; no account rates are read (a live or paper session that sends real orders refuses to start unless the account's rates equal them)"        # owner 14 Nov
        self.config_notes["funding_window"] = "evaluated only where the run has derivative data (next funding times); without it every evaluation counts funding_window_not_evaluable and nothing is blocked"
        # Owner 16 Nov (seeds, the 5 s mark rule): every manifest records the seeds the run used (a profile key from v1_10, the current defaults before) and the mark-stamp rule, frozen profiles included.
        self.config_notes["effective_seeds"] = {"run_seed": config.seed, "hedge_fault_seed": getattr(config.hedge_faults, "seed", None), "latency_seed": config.seed,
                                                "source": "profile key (seed, hedge_faults.seed)" if ("seed" in profile or "seed" in (profile.get("hedge_faults") or {})) else "code defaults (the profile sets none)"}
        self.config_notes["mark_stamp"] = getattr(self.app_config.margin, "mark_stamp", "restamp") + (" (LEGACY: a mark is re-stamped with the evaluation time and lives up to about 2 x the 5 s limit; the fix is margin.mark_stamp own_timestamp, v1_10 onward)" if getattr(self.app_config.margin, "mark_stamp", "restamp") == "restamp" else " (a mark older than the limit by its own timestamp pauses; nothing is re-stamped)")
        self.config_notes["real_order_requirements"] = check_real_order_profile(profile) or "met"     # owner 13 Nov: a profile used with real orders must meet crypto_stat_arb.config.REAL_ORDER_REQUIRED; a backtest of an older profile may not, and says so here
        self.instrument_id = self.app_config.instrument_id
        self.feed = CanonicalFeed(self.instrument_id, venue_names=self.names, delta_semantics={v: s.delta_semantics for v, s in self.venue_specs.items()},
                                  max_silence_s=None if config.timing is None else config.timing.dead_feed_s)
        self.states = {v: VenueState(v, config.venue_state) for v in venues}
        self.timing_stats: Counter = Counter()
        self._feed_state: dict[str, str] = {}
        self._last_book_ns: dict[str, int] = {}
        self._bo_since: int | None = None
        self._bo_intervals: list[tuple[int, int]] = []
        self.venue_span: dict[str, list[int]] = {}
        self.book_only_ns_by_day: dict[str, int] = {}
        self.truth = None
        if config.timing is not None and config.timing.truth_books and not recorded:       # WP3 truth books, the shared rule (timing.TruthBooks): taker arrivals and PostOnly checks use the venue-truth book at the arrival's exchange time
            truth_books = {v: OrderBook(v, config.pair, self.venue_specs[v].delta_semantics, tape_repair=config.book_tape_repair, own_overlay=config.own_impact in ("overlay", "refill"),
                                        message_atomic=config.message_atomic_book) for v in venues}
            self.truth = TruthBooks(truth_books, self._truth_apply, message_atomic=config.message_atomic_book, aligner_of=lambda: self.latency.aligner, stats=self.timing_stats)
        # The float VIEW books the strategy reads and the maker executor is judged against: persistent, message-atomic, touch ages kept, a cross repaired at message end: the backtester's own books,
        # fed here from the converted events. The core books (below, in the session) serve the core's gates, OMS and risk.
        self.views = {v: OrderBook(v, config.pair, self.venue_specs[v].delta_semantics, tape_repair=config.book_tape_repair, own_overlay=config.own_impact in ("overlay", "refill"),
                                   message_atomic=config.message_atomic_book) for v in venues}
        if config.own_impact == "refill" and getattr(config, "impact", None) is None:
            raise ValueError("own_impact 'refill' needs an impact: block (its own_refill_tau_s is the refill constant of our own takes)")
        executor = MakerExecutor(EmpiricalFillModel(), config.maker)
        self.executor = executor
        executor.own_overlay = config.own_impact in ("overlay", "refill")
        self.adapters = {self.names[v]: QueueAwareVenueAdapter(self.names[v], executor, tick_size=self.ticks[v], maker_fee_bps=self.venue_specs[v].fees.maker_bps, pair=config.pair,
                                                               state=self.states[v], truth=self.truth, feed_venue=v, view_book=(lambda v=v: self.views[v]), driver_fed=True) for v in venues}
        # Hedge-fault injection (owner 11 Nov, the ghost hedge): OFF unless the run asks (an existing run never changes because its profile names a fault block). On, the config's hedge_faults reach the venue
        # simulators: outages (static or one per plan at the first hedge order), reject_prob, and the ghost policy (the hedge is held at the gateway and executed at the reconnect). Faults the simulator does not
        # model are refused here, not ignored.
        if reconcile_mode not in ("count", "enforce"):
            raise ValueError("reconcile_mode must be count or enforce")
        self.reconcile_mode = reconcile_mode
        self.hedge_fault_injection = hedge_fault_injection
        if hedge_fault_injection:
            hf_cfg = config.hedge_faults
            if hf_cfg is None or not hf_cfg.injects:
                raise ValueError("hedge_fault_injection needs a hedge_faults block that injects something")
            if hf_cfg.outage_on_fill_s and (hf_cfg.outage_on_fill_venue != "hedge" or hf_cfg.outage_on_fill_kind != "gateway"):
                raise ValueError("hedge_fault_injection simulates a conditional outage of the hedge venue's gateway only (outage_on_fill_venue 'hedge', kind 'gateway')")
            unsupported = [n for n in ("delay_prob", "partial_prob", "ack_unknown_prob", "fast_move_reject_prob", "restart_s") if getattr(hf_cfg, n)] + (["disconnects"] if hf_cfg.disconnects else []) + (["reject_prob_by_venue"] if any(p for _, p in hf_cfg.reject_prob_by_venue) else [])
            if unsupported:
                raise ValueError(f"hedge_fault_injection does not simulate: {', '.join(unsupported)}")
            hedge_names = {self.names[v] for v in venues if v not in config.arb.maker_venues}
            maker_outage = [o for o in hf_cfg.outages if o.venue.lower() in {n.lower() for n in self.adapters if n not in hedge_names}]
            if maker_outage and hf_cfg.outage_policy != "blackhole":
                raise ValueError("a static outage of a maker venue is simulated with outage_policy 'blackhole' only (order entry lost, fill reports held until the reconnect)")
            for name, adapter in self.adapters.items():
                adapter._fault_config = hf_cfg
                adapter.outage_on_fill_here = name in hedge_names
                adapter.maker_outage_here = name not in hedge_names and any(o.venue.lower() == name.lower() for o in maker_outage)
        # Case 2c (owner 12 Nov): attempt i of each hedge plan executes against a hedge-venue book thinned to (levels, quantity per level); (0, 0) is a zero fill. Opt-in, and only ever shows LESS depth than the capture.
        self.hedge_thin_book = tuple((int(n), float(q)) for n, q in hedge_thin_book)
        if self.hedge_thin_book:
            for name, adapter in self.adapters.items():
                if name in {self.names[v] for v in venues if v not in config.arb.maker_venues}:
                    adapter.hedge_thin_attempts = self.hedge_thin_book
        self.resync_delay_s = float(getattr(config, "resync_delay_s", 5.0))
        # Timing is a RULE, the same on both runtimes (owner 9 Nov): the profile's timing block (latency preset, floors, jitter) drives the feed delay, the clock and every order delay.
        self.latency = LatencyBook({v: s.latency for v, s in self.venue_specs.items()}, seed=config.seed)
        self.timelines = Timelines(config.timing) if config.timing is not None else None
        if self.timelines is not None:
            aligner = FixedOffsetAligner(config.timing.clock)          # what the backtester's _install_timing does
            self.latency.aligner = aligner
            for v, vl in self.latency.venues.items():
                vl.aligner = aligner
                vl.feed = self.timelines.sampler(v, "feed")
        self.latency_model = TimelinesLatencyModel(self.timelines) if self.timelines is not None else None
        hf, hedge_cfg = config.hedge_faults, (config.execution.hedge if getattr(config, "execution", None) is not None else {})
        hedge_policy = HedgePolicy(retry_limit=int(getattr(hf, "max_retries", 0) or 0), retry_backoff_ns=int(float(getattr(hf, "retry_backoff_s", 0.0) or 0.0) * _NS),
                                   query_attempt_limit=int(getattr(hf, "unknown_query_max_attempts", 3) or 3),
                                   **hedge_policy_fields(hedge_cfg, config.arb.max_walk_bps), **hedge_dust_fields(profile))
        # E0 step (g): the maker observation records (place / fill / hedge arrival), observation only, ported from the backtester; maker_observations.jsonl is written with the run's output
        self.obs = None
        if observations:
            from .execution.observation import ObservationLog, SessionObservations
            self.obs = ObservationLog()
            sink = SessionObservations(self.obs, self.views, lambda oid: self.session.intent_observer.layer._child_intent.get(oid, "") if self.session.intent_observer is not None else "")
            for adapter in self.adapters.values():
                adapter.observer = sink
                adapter.executor.obs_enabled = True      # the fill engine attaches its per-fill record (cause, volume offered and consumed, queue before the fill) to each fill
        # W2a (owner 17 Nov; audit A2 12.2 finding 4): the session's PROFILE RULES come from one function, `profile_session_rules`, that the paper session (`live_paper_setup`) calls too, so a paper run of a
        # profile runs the same dispatch policy, hedge policy, breaker, execution config, layer modes, intent journal, markout horizons, feed health and limits as its replay. Only what belongs to the
        # replay itself is passed here: the simulated mode and adapters, and the resync rule of a recorded stream.
        self.session = TradingSession(
            self.app_config, execution_mode=ExecutionMode.INTERNAL_SIMULATION, adapters=self.adapters, strategies=[],
            resync_delay_ns=int(self.resync_delay_s * _NS), resync_source=self.feed.resync_snapshot,
            resync_silence_ns=None if config.timing is None else int(config.timing.dead_feed_s * _NS),
            **profile_session_rules(config, profile, self.names, hedge_policy=hedge_policy, latency_model=self.latency_model, layer_risk_mode=layer_risk_mode, breaker_config=breaker_config,
                                    layer_route_mode=layer_route_mode),
        )
        if self.session.feed_health is not None:                            # W1: the effective feed-health settings of the run, in the manifest only when the profile has the block
            self.config_notes["feed_health"] = feed_health_config_report(self.app_config, profile)
        if self.session._limits is not None:                                # owner 16 Nov (Order 3): recorded in the manifest only when the profile has the block
            self.config_notes["session_limits"] = {"config": self.session._limits, "tier_table": "replays keep the ASSUMED margin tiers (the account-read tiers are a real-order start-up step), labelled",
                                                   "clock_offset": "not evaluable in a replay without a measured clock: counted, never blocking", "verify_account": "a start-up step for paper and live; not run in a replay"}
        if config.arb.maker_max_age_s > 0:
            self.session.oms.order_lifetime_ns = int(config.arb.maker_max_age_s * _NS)     # the profile's MANDATORY maker order lifetime (a rule, the same on both runtimes)
        self.strategy = CrossVenueArbStrategy(config.pair, self.venue_specs, config.arb)
        self.shim = ArbSessionStrategy(self.strategy, ledger=self.session.ledger, oms=self.session.oms, states=self.states, instruments=self.app_config.instruments,
                                       instrument_id=self.instrument_id, venue_names=self.names, eligibility_policy=self.session.alignment_policy, view_books=lambda: self.views)
        self.feeder = SimulatedVenueFeeder(self.session, self.names, self.views, self.states, self.adapters, ets_of=self._ets, delta_uses_book_size=config.delta_uses_book_size)
        self.reject_trace: list | None = [] if getattr(config, "trace_rejects", False) else None
        if self.reject_trace is not None:
            self.strategy.rejects = TracedRejects(self.strategy.rejects, self.strategy, self.reject_trace)
        self.session.strategies.append(self.shim)
        self.session.strategy = self.shim
        if config.own_impact == "refill":                                  # step 2 (owner 15 Nov): our own takes live in a ledger per venue adapter; the taker walk reads a copy of the book with them removed (they refill as 1 - exp(-t / tau))
            for a in self.adapters.values():
                a.own_ledger = OwnTakeLedger(config.impact.own_refill_tau_s)
                a.consume_liquidity = False                              # the base adapter's own mechanism (a take is withheld until the published level shrinks or a snapshot arrives; it never refills) is REPLACED by the ledger, not added to it
            self.config_notes["own_impact"] = ("REFILL ON (ASSUMED tau " + str(config.impact.own_refill_tau_s) + " s): after an aggressive fill of ours the size taken is missing from the book the next taker walk reads, "
                                               "coming back as 1 - exp(-t / tau); a public update does not reset it; the core book is never touched")
        self.impact = None
        if getattr(config, "impact", None) is not None and config.impact.level == 3:         # step C (owner 14 Nov): the reactive overlay. Absent (None / level below 3): nothing below exists
            self.impact = ImpactOverlay(config.impact, {v: self.ticks[v] for v in venues})
            for a in self.adapters.values():
                a.impact = self.impact
            self._impact_dp = {v: 0.0 for v in venues}                    # the price shift (price units) each venue's feed carries now, a whole number of its own ticks
            self.config_notes["impact"] = ("LEVEL 3 OVERLAY ON (" + config.impact.label + "): after each aggressive order of ours the later prices of its venue move by the permanent + decaying shift of the fitted curve, the other venue by the "
                                           "cross coefficient; the feed the views, the strategy and the maker fill engine read is shifted, the taker walk reads a shifted copy of the core book; the core books (gate, markouts, valuation) are NOT shifted")
        self._impact_model = CurveImpact(config.impact) if getattr(config, "impact", None) is not None and config.impact.level >= 2 else None       # step C level 2: the peak estimate journaled with each decision
        self._split_cfg = getattr(config, "split", None)                                                                                      # step E/F: the order-split evidence (observation only)
        self._outflow_ratios: tuple = ()
        if self._split_cfg is not None:
            if not getattr(config.venue_state, "depth_quality", False):
                raise ValueError("split evidence needs venue_state.depth_quality (it reads the recent depleting volume of the depth-quality tracker)")
            if self._split_cfg.volume_window_s > config.venue_state.dq_flicker_window_s + 1e-9:
                raise ValueError("split.volume_window_s must not exceed venue_state.dq_flicker_window_s")
            self._outflow_ratios = tuple(sorted(float(x) for x in json.loads(Path(self._split_cfg.outflow_ratio_file).read_text())))
            if not self._outflow_ratios:
                raise ValueError("split.outflow_ratio_file holds no samples")
        if getattr(config.venue_state, "depth_quality", False):         # owner 14 Nov: gap depth and flicker per venue (off by default; nothing below exists with it off)
            self.session.feed_gap_hook = lambda venue, now_s: self.states[self._feed_venue(venue)].note_feed_gap(now_s)
            for v, st in self.states.items():
                st.set_tick(self.ticks[v])                              # the venue's own price grid, for the thin-tick run rule (step B)
        if (getattr(config.venue_state, "depth_quality", False) or self._impact_model is not None) and self.session.intent_observer is not None:
            self.session.intent_observer.layer.set_evidence_provider(self._routing_evidence)
        self._stream = stream
        self.events = {"book": 0, "trade": 0}
        self.last_ns = 0

    # -- feed --------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _truth_apply(ev, tb) -> None:
        """One raw research event into a venue-truth book (the same reader as the backtester's)."""
        if ev.kind == BOOK:
            bids, asks = snapshot_levels(ev.payload)
            if bids and asks:
                tb.apply_snapshot(bids, asks, getattr(ev, "exchange_ts", None) or ev.receive_ts)
        elif ev.kind == DELTA:
            side, price, volume, event_type = delta_fields(ev.payload)
            if price > 0:
                tb.apply_delta(side, price, volume, event_type, getattr(ev, "exchange_ts", None) or ev.receive_ts)

    def _with_truth(self, raw):
        """Hold a look-ahead window of events per venue for the truth books, and bring the truth book along with every event the view receives (the backtester's `_with_lookahead` and `_truth_sync_event`)."""
        if self.truth is None:
            return raw
        window = max(t.feed.max_ms for t in self.config.timing.venues.values()) / 1000.0

        def synced():
            for ev in self.truth.with_lookahead(raw, window):
                self.truth.sync_event(ev)
                yield ev
        return synced()

    def _timed_stream(self) -> Iterable:
        if self._stream is not None:
            return self._stream
        c = self.config
        return CrossVenueFeed(
            data_root=c.data_root, pair=c.pair, venues=c.venues, start_day=c.start_date, end_day=c.end_date, latency=self.latency,
            trust_local_ts={v: s.trust_local_ts for v, s in self.venue_specs.items()}, include_deltas=c.include_deltas, quality_preflight=True, quality_report_only=_preflight_override(c) is not None,
            trade_dedupe=c.trade_dedupe, event_order=c.event_order, skip_snapshots=tuple(c.skip_snapshot_venues), decimal_numbers=True,
        )

    # -- loop --------------------------------------------------------------------------------------------------------------

    def _layer_report(self) -> dict:
        """The layer's report plus the E0 step (f) shadow reconciliation: cash, base and fees rebuilt from the fill journal alone against the ledger (None without the layer). `reconcile_mode`
        enforce raises on a mismatch; count only records it."""
        report = self.session.intent_report()
        if report is None or self.session.intent_observer is None:
            return report
        from crypto_stat_arb.ems.reconcile import reconcile_session
        fee_bps = {self.names[v]: (float(sp.fees.maker_bps), float(sp.fees.taker_bps)) for v, sp in self.venue_specs.items()}
        if self.obs is not None:
            report["observation"] = {**self.obs.summary(), "consume_liquidity_by_venue": {n: a.consume_liquidity for n, a in self.adapters.items()},
                                     "note": "observation only: nothing in the run reads these rows; consume_liquidity True = an own hedge fill removes the walked quantity from the book it walked"}
        report["exec_reconciliation"] = reconcile_session(self.session, fee_bps=fee_bps, mode=self.reconcile_mode)
        return report

    def run(self) -> dict[str, Any]:
        return self._run_events(self.feed.convert(self._with_truth(self._timed_stream())))

    def run_recorded(self, events: Iterable) -> dict[str, Any]:
        """The same loop on a recorded LIVE session's core events (`iter_recorded_events`): nothing is converted, the receive time of each event is the clock."""
        if not self.recorded:
            raise ValueError("run_recorded needs SessionReplay(..., recorded=True)")
        return self._run_events(events)

    def _run_events(self, events: Iterable) -> dict[str, Any]:
        replay_interlock(session=self.session)
        import time as _time
        wall0 = _time.perf_counter()
        try:
            return self._run_loop(events)
        finally:
            self.wall_s = _time.perf_counter() - wall0        # W2a: for the run record only (session_run_record.json); never in the result or the manifest

    def _run_loop(self, events: Iterable) -> dict[str, Any]:
        limit_ns = None if self.config.max_event_ts is None else int(self.config.max_event_ts * _NS)
        for event in events:
            if isinstance(event, DerivativeTicker):
                self._derivative(event)
                continue
            if self.config.max_events is not None and sum(self.events.values()) >= self.config.max_events:
                break
            if limit_ns is not None and event.receive_ts_ns > limit_ns:
                break
            self.last_ns = event.receive_ts_ns
            if self.first_ns is None:
                self.first_ns = self.last_ns
            if self.decomp is not None and self.decomp.due(self.last_ns / _NS):
                self.decomp.resolve_asof(self.last_ns / _NS, self._mid_pre)        # the mids as they stood BEFORE this event: the mid at each target
            if isinstance(event, TradeEvent):
                self._trade(event)
            else:
                self._book(event)
            if self.decomp is not None:
                self._decomp_after(event)
            if self.lifecycle_report:
                self._regime_tick()
        self.session.drain()
        self.session.resolve_markouts(self.last_ns, finalize=True)
        if self.decomp is not None:
            self._decomp_after(None)
            end = self.last_ns / _NS + 1e-9
            self.decomp.resolve_asof(end, self._mid_pre)
            self.decomp.resolve_asof(end, self._mid_pre, finalize=True)
        return self.result()

    def _regime_tick(self) -> None:
        """Once a second: the maker venue's view book as (volatility, spread, liquidity). Observation only."""
        sec = self.last_ns // _NS
        if sec == self._regime_last_sec:
            return
        book = self.views[self.config.arb.maker_venues[0]]
        if not (book.best_bid > 0 and book.best_ask > 0):
            return
        self._regime_last_sec = sec
        vol, spread, depth, mid = regime_sample(self._regime_prev_mid, book.best_bid, book.best_ask, book.best_bid_size, book.best_ask_size)
        self._regime_prev_mid = mid
        self._regime_samples[sec] = (vol, spread, depth)

    def _lifecycle_orders(self) -> list[dict[str, Any]]:
        """The maker quotes (post-only primary orders) as lifecycle rows, from the ledger's order events: when each was sent, acknowledged, asked to cancel, cancelled, and every fill (time, quantity)."""
        events = [e for e in self.session.ledger.events if e["event_type"] in ("ORDER_LIFECYCLE", "QUOTE_WITHDRAWN", "AMEND_REQUESTED")]
        quotes = {c.order_id: c for plan in self.session.oms.plans.values() for c in plan.children if c.post_only and c.order_role == "PRIMARY"}
        def entry_spread(c):
            sig = self.shim.signals.get(self.session.oms.plans[c.parent_id].opportunity.opportunity_id)
            if sig is None or not (sig.buy_price and sig.sell_price):
                return None
            return (sig.sell_price - sig.buy_price) / ((sig.sell_price + sig.buy_price) / 2.0) * 1e4         # hedge-venue price against the quote's price, bps of the mid: what the quote was entered for
        rows = {oid: {"order_id": oid, "intent_id": quotes[oid].intent_id, "entry_spread_bps": entry_spread(quotes[oid]), "sent_ts": None, "ack_ts": None, "cancel_req_ts": None, "cancel_conf_ts": None, "fills": [], "withdrawn": False, "cancel_while_sent": False, "amend_ts": []} for oid in quotes}
        cum: dict[str, Decimal] = {}
        for e in events:
            oid, ts = e.get("order_id"), e["ts_ns"] / _NS
            if oid not in rows:
                continue
            r, kind = rows[oid], e["event_type"]
            if kind == "QUOTE_WITHDRAWN":
                r["withdrawn"] = True
            elif kind == "AMEND_REQUESTED":
                r["amend_ts"].append(ts)
            else:
                ev = e.get("lifecycle_event")
                if ev == "SENT" and r["sent_ts"] is None:
                    r["sent_ts"] = ts
                elif ev == "ACKNOWLEDGED" and r["ack_ts"] is None:
                    r["ack_ts"] = ts
                elif ev == "CANCEL_REQUESTED" and r["cancel_req_ts"] is None:
                    r["cancel_req_ts"] = ts
                    r["cancel_while_sent"] = r["ack_ts"] is None and not r["fills"]
                elif ev == "CANCELLED" and r["cancel_conf_ts"] is None:
                    r["cancel_conf_ts"] = ts
                filled = Decimal(str(e.get("cumulative_filled_quantity", 0)))
                if filled > cum.get(oid, Decimal("0")):
                    r["fills"].append([ts, float(filled - cum.get(oid, Decimal("0")))])
                    cum[oid] = filled
        return [r for r in rows.values() if r["sent_ts"] is not None]

    def _ems_coverage(self) -> dict[str, Any]:
        from crypto_stat_arb.metrics import ems_path_coverage
        s = self.session
        children = [c for plan in s.oms.plans.values() for c in plan.children]
        hedge_ids = {oid for ids in s.oms._chain_children.values() for oid in ids}
        journal = list(s.intent_observer.layer.journal) if s.intent_observer is not None else []
        layer_cfg = s.intent_observer.layer.config.as_dict() if s.intent_observer is not None and hasattr(s.intent_observer.layer, "config") else None
        return ems_path_coverage(s.ledger.events, journal, children, hedge_ids, self.executor.diag, self.strategy.clip_binding, self.ticks, getattr(self, "_lifecycle_rows", []), layer_cfg)

    def _lifecycle(self, fills_rows, markout_rows) -> dict[str, Any]:
        from crypto_stat_arb.metrics import lifecycle_report, pair_economics
        journal = list(self.session.intent_observer.layer.journal) if self.session.intent_observer is not None else []
        pairs = pair_economics(fills_rows, journal)["rows"] if journal else []
        chains = [dict(c.as_record(), parent_id=pid) for pid, group in self.session.oms.hedge_chains.items() for c in group.values()]
        orders = self._lifecycle_orders()
        pnl: dict[str, float] = {}
        for p in pairs:
            if p.get("pair_net_bps") is not None:
                pnl[p["maker_intent_id"]] = pnl.get(p["maker_intent_id"], 0.0) + float(p["pair_net_bps"]) / 1e4 * float(p["qty"]) * float(p["maker_price"])
        for r in orders:
            r["net_pnl_usdt"] = pnl.get(r["intent_id"])
        self._lifecycle_rows = orders
        return lifecycle_report(orders, pairs, markout_rows, chains, self._regime_samples, self.regime_boundaries)

    def _derivative(self, ticker: DerivativeTicker) -> None:
        """A recorded derivatives frame (mark, index, funding): the session keeps the venue's mark for the margin check (and settles funding when it is configured). Nothing else moves."""
        self.session.on_derivative(ticker)
        if ticker.mark_price is not None:
            self._exchange_mark_venues.add(ticker.venue)
            self.mark_ticks["exchange_mark"] += 1

    def _synth_mark(self, event) -> None:
        """MARK_FROM_MID (replay only, off unless asked): the last valid mid of the venue's book as its mark, for a venue with no exchange mark, at most every 100 ms."""
        if event.venue in self._exchange_mark_venues:
            return
        key = (event.venue, event.instrument_id)
        book = self.session.books[key]
        now = event.receive_ts_ns
        if book.state.value != "VALID" or not book.bids or not book.asks or now - self._last_synth_mark_ns.get(event.venue, -10**18) < 100_000_000:
            return
        self._last_synth_mark_ns[event.venue] = now
        self.session.exchange_marks[key] = (now, (book.best_bid + book.best_ask) / 2)
        self.mark_ticks["MARK_FROM_MID"] += 1

    def _decomp_after(self, event) -> None:
        """Record the fills this event caused with the mid before it, then move that mid on to the state after it."""
        events = self.session.ledger.events
        while self._decomp_pos < len(events):
            e = events[self._decomp_pos]
            self._decomp_pos += 1
            if e.get("event_type") != "FILL":
                continue
            venue = e["venue"]
            m0 = self._mid_pre.get(venue)
            if m0 is None or m0 <= 0:
                self.decomp_skipped_no_mid += 1
                continue
            role = "maker" if (e.get("order") or {}).get("post_only") else "taker"
            self.decomp.record_fill(e["ts_ns"] / _NS, venue, str(e["side"]).lower(), float(e["fill_price"]), float(e["fill_quantity"]), role, mid_at_fill=m0, fee=float(e.get("fee_delta") or 0.0))
        if event is not None and not isinstance(event, TradeEvent):
            book = self.session.books[(event.venue, event.instrument_id)]
            if book.bids and book.asks:
                self._mid_pre[event.venue] = float((book.best_bid + book.best_ask) / 2)

    def _feed_venue(self, core_name: str) -> str:
        return next(v for v, n in self.names.items() if n == core_name)

    def _impact_sync(self, fv: str, now_s: float, ets) -> float:
        """Level 3: the price shift (price units) venue `fv`'s feed carries at `now_s`. When the whole-tick shift changed, the view is re-seeded from its OWN current levels moved by the difference, and the maker fill engine
        is told as it is for any snapshot (a depth change moves queues, never fills an order)."""
        ov = self.impact
        if not ov.orders:
            return 0.0
        view = self.views[fv]
        mid = view.mid if view.ready else 0.0
        if mid <= 0:
            return self._impact_dp[fv]
        k = ov.shift_ticks(fv, now_s, mid)
        if k != ov.applied_ticks[fv]:
            delta = round((k - ov.applied_ticks[fv]) * self.ticks[fv], 12)
            bids = [[round(p + delta, 8), q] for p, q in view.levels("bid", 100_000)]
            asks = [[round(p + delta, 8), q] for p, q in view.levels("ask", 100_000)]
            ov.applied_ticks[fv] = k
            self._impact_dp[fv] = round(k * self.ticks[fv], 12)
            if bids and asks:
                view.apply_snapshot(bids, asks, now_s)
                self.states[fv].on_book(view, now_s)
                adapter = self.adapters[self.names[fv]]
                self._deliver(adapter, adapter.feed_snapshot(view, now_s, ets), int(now_s * _NS), int(now_s * _NS))
        return self._impact_dp[fv]

    def _routing_evidence(self, venue: str, side: str, now_s: float, qty: float | None = None) -> dict | None:
        """The routing evidence the layer journals with a decision, for the side the order consumes of one venue's book (steps B, C, E, F; OBSERVATION ONLY, nothing is sent from it):
          depth_quality  gap depth, survival, cancel-to-trade, spike (VenueState), when depth_quality is on
          impact         the level-2 peak estimate in bps for `qty` at the decision, when config.impact has a curve
          order_split    what the order split (Cont and Kukanov) would have chosen for `qty`: its inputs and the market and limit parts, when config.split is set"""
        fv = self._feed_venue(venue)
        view, state = self.views[fv], self.states[fv]
        out: dict = {}
        dq = state.depth_quality(view, now_s, side)
        if dq is not None:
            out["depth_quality"] = dq
        peak = None
        if self._impact_model is not None and qty:
            cfg = self.config.impact
            touch = (view.best_ask if side == "ask" else view.best_bid) if view.ready else 0.0
            dk = None
            if touch > 0:
                limit = touch * (1.0 + cfg.depth_k_bps / 1e4) if side == "ask" else touch * (1.0 - cfg.depth_k_bps / 1e4)
                dk = view.depth_within(side, limit)
            peak = self._impact_model.cost_bps(float(qty), depth_k=dk)
            out["impact"] = {"peak_bps": peak, "form": cfg.form, "depth_k": dk, "label": cfg.label + " (a fitted curve, a PROXY: not checked against real fills)"}
        if self._split_cfg is not None and qty and dq is not None and view.ready:
            out["order_split"] = self._split_evidence(fv, view, state, side, float(qty), now_s, dq, peak)
        return out or None

    def _split_evidence(self, fv: str, view, state, consumed: str, qty: float, now_s: float, dq: dict, peak: float | None) -> dict:
        cfg, specs = self._split_cfg, self.venue_specs[fv]
        rest_side = "ask" if consumed == "bid" else "bid"                       # a limit order of ours would rest on the OTHER side of the book, at its touch
        levels = view.levels(rest_side, 1)
        queue = levels[0][1] if levels else 0.0
        recent = state.recent_volume(rest_side, now_s, cfg.volume_window_s) or 0.0
        scale = max(recent, cfg.min_recent_volume)
        samples = tuple(r * scale for r in self._outflow_ratios)
        survival = (dq["survival"][consumed] or {}).get("mean", 1.0)
        gap = dq["gap"][consumed]
        depth_k = DisplayedDepthImpact().effective_depth_within_cap(view.levels(consumed, 80), (view.best_ask if consumed == "ask" else view.best_bid) * (1.0 + (1 if consumed == "ask" else -1) * 10.0 / 1e4), consumed == "ask", survival)
        cap = min(depth_k, gap["depth"]) if gap["status"] == "GAP" and gap["depth"] is not None else depth_k
        v = SplitVenue(fv, True, queue, samples, view.spread_bps / 2.0, specs.fees.taker_bps, -specs.fees.maker_bps, cfg.adverse_bps, peak or 0.0, cap)
        d = split_single_venue(qty, v, cfg.lambda_u_bps, lambda_o_bps=cfg.lambda_o_bps)
        return {"market": d.market.get(fv, 0.0), "limit": d.limit.get(fv, 0.0), "p": d.p, "status": d.status, "method": d.method,
                "inputs": {"queue_ahead": queue, "recent_volume": recent, "outflow_samples": len(samples), "half_spread_bps": v.half_spread_bps, "taker_fee_bps": v.fee_bps, "rebate_bps": v.rebate_bps,
                           "adverse_bps": v.adverse_bps, "impact_bps": v.impact_bps, "lambda_u_bps": cfg.lambda_u_bps, "max_market_qty": cap, "survival_mean": survival, "horizon_s": cfg.horizon_s},
                "label": cfg.label + ", OBSERVATION ONLY: nothing was sent from this split"}

    def _impact_sync(self, fv: str, now_s: float, ets) -> float:
        """Level 3: the price shift (price units) venue `fv`'s feed carries at `now_s`. When the whole-tick shift changed, the view is re-seeded from its OWN current levels moved by the difference, and the maker fill engine
        is told as it is for any snapshot (a depth change moves queues, never fills an order)."""
        ov = self.impact
        if not ov.orders:
            return 0.0
        view = self.views[fv]
        mid = view.mid if view.ready else 0.0
        if mid <= 0:
            return self._impact_dp[fv]
        k = ov.shift_ticks(fv, now_s, mid)
        if k != ov.applied_ticks[fv]:
            delta = round((k - ov.applied_ticks[fv]) * self.ticks[fv], 12)
            bids = [[round(p + delta, 8), q] for p, q in view.levels("bid", 100_000)]
            asks = [[round(p + delta, 8), q] for p, q in view.levels("ask", 100_000)]
            ov.applied_ticks[fv] = k
            self._impact_dp[fv] = round(k * self.ticks[fv], 12)
            if bids and asks:
                view.apply_snapshot(bids, asks, now_s)
                self.states[fv].on_book(view, now_s)
                adapter = self.adapters[self.names[fv]]
                self._deliver(adapter, adapter.feed_snapshot(view, now_s, ets), int(now_s * _NS), int(now_s * _NS))
        return self._impact_dp[fv]

    def _depth_evidence(self, venue: str, side: str, now_s: float) -> dict | None:
        """The routing evidence the layer journals with a decision: the gap depth and flicker of `side` of one venue's book (the side the order consumes), from the venue's own VenueState."""
        fv = self._feed_venue(venue)
        return self.states[fv].depth_quality(self.views[fv], now_s, side)

    def _dead_feed_gate(self, now_s: float) -> bool:
        """The profile's MANDATORY timing.dead_feed_s gate on the view books' absolute age, the same rule as the backtester's `_dead_feed_blocks` (`timing.dead_feed_blocks`)."""
        cfg = self.config.timing
        if cfg is None:
            return False
        max_age = cfg.max_book_age_s if cfg.max_book_age_s is not None else self.config.arb.max_book_age_s
        return dead_feed_blocks({v: self.views[v].age_s(now_s) for v in self.venues}, max_age, cfg.dead_feed_s, self._feed_state, self.timing_stats)

    def _book_only_suspension(self, venue: str, now_ns: int) -> None:
        """Owner 8 Nov (Q-G1): suspended seconds reported BOTH ways. The session's gate measures age from the venue's last message of any kind; this counts the same blocks with age from the last BOOK
        change only (the older reading), so the part of the suspension that is only a quiet best-bid/ask feed shows."""
        self._last_book_ns[venue] = now_ns
        limit = self.session.cfg.max_book_age_ns
        blocked = any(
            (now_ns - self._last_book_ns.get(v, -10 ** 18)) > limit or self.session.books[(v, self.instrument_id)].state.value in ("SYNCING", "GAP_DETECTED", "CHECKSUM_FAILED", "INVALID_DATA", "PRICE_ANOMALY")
            for v in (self.names[x] for x in self.venues))
        if blocked and self._bo_since is None:
            self._bo_since = now_ns
        elif not blocked and self._bo_since is not None:
            self._bo_intervals.append((self._bo_since, now_ns))
            self._bo_since = None

    def _common_window_ns(self) -> tuple[int, int] | None:
        """The span in which BOTH venues have data (owner 8 Nov): from the later first message to the earlier last message. The Binance tape ends at about 17 h on 2022-01-05 and 2022-01-13."""
        if len(self.venue_span) < len(self.venues):
            return None
        return max(v[0] for v in self.venue_span.values()), min(v[1] for v in self.venue_span.values())

    def _seconds_by_day(self, intervals, clip: tuple[int, int] | None) -> dict[str, float]:
        out: dict[str, int] = {}
        for a, b in intervals:
            if clip is not None:
                a, b = max(a, clip[0]), min(b, clip[1])
            for day, ns in self.session.book_gate._segments(a, b):
                out[day] = out.get(day, 0) + ns
        return {d: ns / 1e9 for d, ns in sorted(out.items())}

    def _book_only_seconds(self, clip) -> dict[str, float]:
        """Book-change-age reading, per UTC day, including a suspension still open at the last event, clipped to the common window."""
        intervals = list(self._bo_intervals) + ([(self._bo_since, self.last_ns)] if self._bo_since is not None else [])
        return self._seconds_by_day(intervals, clip)

    def _coverage(self) -> dict:
        w = self._common_window_ns()
        spans = {v: {"first_ns": a, "last_ns": b} for v, (a, b) in self.venue_span.items()}
        if w is None:
            return {"venues": spans, "common_window_ns": None}
        excluded_after = [{"venue_with_data_until": v, "excluded_from_ns": w[1], "excluded_to_ns": span["last_ns"], "excluded_seconds": (span["last_ns"] - w[1]) / 1e9} for v, span in spans.items() if span["last_ns"] > w[1]]
        return {"venues": spans, "common_window_ns": list(w), "common_window_hours": (w[1] - w[0]) / 3.6e12, "excluded_span": excluded_after,
                "label": "DATA_LIMITATION: the span after one venue's data ends is excluded from the comparison and from the suspended seconds; it is not a suspension"}

    def _ets(self, exchange_ts_ns: int) -> float | None:
        """When the event executed at the exchange, on the host clock (the backtester's `_event_host_ts`); None until the aligner has a sample."""
        aligner = self.latency.aligner
        return aligner.to_host(exchange_ts_ns / _NS) if aligner.ready else None

    def _book(self, event: MarketEvent) -> None:
        """Replay of one book event: the shared feeder (`SimulatedVenueFeeder`, core) does the backtester loop's work, this driver keeps only its own accounting."""
        now = event.receive_ts_ns
        self.events["book"] += 1
        span = self.venue_span.setdefault(self._feed_venue(event.venue), [now, now])
        span[1] = max(span[1], now)
        self.feeder.on_book(event)
        self._book_only_suspension(event.venue, now)

    def _book_only_suspension(self, venue: str, now_ns: int) -> None:
        """Owner 8 Nov (Q-G1): suspended seconds reported BOTH ways. The session's gate measures age from the venue's last message of any kind; this counts the same blocks with age from the last BOOK
        change only (the older reading), so the part of the suspension that is only a quiet best-bid/ask feed shows."""
        self._last_book_ns[venue] = now_ns
        limit = self.session.cfg.max_book_age_ns
        blocked = any(
            (now_ns - self._last_book_ns.get(v, -10 ** 18)) > limit or self.session.books[(v, self.instrument_id)].state.value in ("SYNCING", "GAP_DETECTED", "CHECKSUM_FAILED", "INVALID_DATA", "PRICE_ANOMALY")
            for v in (self.names[x] for x in self.venues))
        if blocked and self._bo_since is None:
            self._bo_since = now_ns
        elif not blocked and self._bo_since is not None:
            self._bo_intervals.append((self._bo_since, now_ns))
            self._bo_since = None

    def _common_window_ns(self) -> tuple[int, int] | None:
        """The span in which BOTH venues have data (owner 8 Nov): from the later first message to the earlier last message. The Binance tape ends at about 17 h on 2022-01-05 and 2022-01-13."""
        if len(self.venue_span) < len(self.venues):
            return None
        return max(v[0] for v in self.venue_span.values()), min(v[1] for v in self.venue_span.values())

    def _seconds_by_day(self, intervals, clip: tuple[int, int] | None) -> dict[str, float]:
        out: dict[str, int] = {}
        for a, b in intervals:
            if clip is not None:
                a, b = max(a, clip[0]), min(b, clip[1])
            for day, ns in self.session.book_gate._segments(a, b):
                out[day] = out.get(day, 0) + ns
        return {d: ns / 1e9 for d, ns in sorted(out.items())}

    def _book_only_seconds(self, clip) -> dict[str, float]:
        """Book-change-age reading, per UTC day, including a suspension still open at the last event, clipped to the common window."""
        intervals = list(self._bo_intervals) + ([(self._bo_since, self.last_ns)] if self._bo_since is not None else [])
        return self._seconds_by_day(intervals, clip)

    def _coverage(self) -> dict:
        w = self._common_window_ns()
        spans = {v: {"first_ns": a, "last_ns": b} for v, (a, b) in self.venue_span.items()}
        if w is None:
            return {"venues": spans, "common_window_ns": None}
        excluded_after = [{"venue_with_data_until": v, "excluded_from_ns": w[1], "excluded_to_ns": span["last_ns"], "excluded_seconds": (span["last_ns"] - w[1]) / 1e9} for v, span in spans.items() if span["last_ns"] > w[1]]
        return {"venues": spans, "common_window_ns": list(w), "common_window_hours": (w[1] - w[0]) / 3.6e12, "excluded_span": excluded_after,
                "label": "DATA_LIMITATION: the span after one venue's data ends is excluded from the comparison and from the suspended seconds; it is not a suspension"}

    def _ets(self, exchange_ts_ns: int) -> float | None:
        """When the event executed at the exchange, on the host clock (the backtester's `_event_host_ts`); None until the aligner has a sample."""
        aligner = self.latency.aligner
        return aligner.to_host(exchange_ts_ns / _NS) if aligner.ready else None

    def _deliver(self, adapter, produced, now_ns: int, exchange_ts_ns: int) -> None:
        if produced:
            self.session.router._deliver_resting_fills(adapter.venue, produced, now_ns, exchange_ts_ns=exchange_ts_ns)

    def _book(self, event: MarketEvent) -> None:
        """The backtester's `_apply_event` for a book event: the view book and venue state, then the maker executor (snapshot, or each level of the message, then the cross check), then the core session."""
        now = event.receive_ts_ns
        now_s = now / _NS
        self.events["book"] += 1
        span = self.venue_span.setdefault(self._feed_venue(event.venue), [now, now])
        span[1] = max(span[1], now)
        self.session.resync_if_due(now)
        self.session.oms.advance_time(now, self.session.books)         # in-flight orders land and timers fire BEFORE this event is applied (the backtester's _advance_to): an order sent at t - latency never sees an event received at t
        self.session.books[(event.venue, event.instrument_id)].apply(event)
        fv = self._feed_venue(event.venue)
        view, state, adapter, ets = self.views[fv], self.states[fv], self.adapters[event.venue], self._ets(event.exchange_ts_ns)
        dp = self._impact_sync(fv, now_s, ets) if self.impact is not None else 0.0          # level 3: the shift this venue's feed carries (0.0 = no overlay: every price below is exactly the event's)
        px = (lambda l: float(l.price)) if not dp else (lambda l: round(float(l.price) + dp, 8))
        produced: list = []
        if event.event_type == EventType.BOOK_SNAPSHOT:
            bids, asks = [[px(l), float(l.quantity)] for l in event.bids], [[px(l), float(l.quantity)] for l in event.asks]
            if bids and asks:
                view.apply_snapshot(bids, asks, now_s)
                state.on_book(view, now_s)
                produced += adapter.feed_snapshot(view, now_s, ets)
        else:
            levels = [("bid", px(l), float(l.quantity)) for l in event.bids] + [("ask", px(l), float(l.quantity)) for l in event.asks]
            for side, price, size in levels:
                view.apply_delta(side, price, size, "modify", now_s)
            view.end_message()
            state.on_book(view, now_s)
            for side, price, size in levels:
                produced += adapter.feed_delta(side, price, view.level_size(side, price) if self.config.delta_uses_book_size else size, now_s, ets)
        produced += adapter.feed_cross(view, now_s, ets)
        self._deliver(adapter, produced, now, event.exchange_ts_ns)
        self.session.on_event(event, apply_book=False)
        self._book_only_suspension(event.venue, now)
        if self.mark_from_mid:
            self._synth_mark(event)

    def _trade(self, trade: TradeEvent) -> None:
        if self.impact is not None:                                           # level 3: a print on a venue the overlay has moved is printed at the moved price
            fv = self._feed_venue(trade.venue)
            dp = self._impact_sync(fv, trade.receive_ts_ns / _NS, self._ets(trade.exchange_ts_ns))
            if dp:
                trade = dataclasses.replace(trade, price=trade.price + Decimal(repr(round(dp, 12))))
        if not self.feeder.on_trade(trade):
            return
        now = trade.receive_ts_ns
        self.events["trade"] += 1
        span = self.venue_span.setdefault(self._feed_venue(trade.venue), [now, now])
        span[1] = max(span[1], now)

    # -- result ------------------------------------------------------------------------------------------------------------

    def _observation_rows(self) -> list[dict]:
        """The observation rows for the file. Case 2h (owner 12 Nov): every maker_fill row also says how its order stood when it was placed (from the maker_place row of the same order): "new_level" (no displayed
        size at its price: we created the level and were first in the queue), "queued" (behind displayed size) or "unknown" (the queue was not known). Observation only: the fills, orders and ledger are untouched."""
        rows = list(self.obs.rows)
        placed = {o["order_id"]: o for o in rows if o.get("obs") == "maker_place"}
        out = []
        for row in rows:
            o = placed.get(row.get("order_id")) if row.get("obs") == "maker_fill" else None
            if o is not None:
                row = {**row, "queue_class": "unknown" if o.get("queue_unknown") else "new_level" if o.get("level_was_absent") else "queued", "queue_ahead_at_placement": o.get("queue_ahead")}
            out.append(row)
        return out

    def _residual_at_end(self) -> dict[str, Any]:
        """Owner 11 Nov (D1): what is still open when the run ends. The residual rule does not flatten a residual inside its approved bound (and a part-lot residual cannot be sent: it is below every venue's minimum), so it is
        reported here, not left to be found in the positions: the OMS's own residual snapshot, the net position over both venues, and a flag when that net is not zero (an unhedged leg or a carried residual)."""
        snap = self.session.oms.residual_risk_snapshot()
        net = sum(self.session.ledger.positions.values(), Decimal(0))
        doc = {"net_position_base": str(net), "flag": "UNHEDGED_OR_CARRIED_RESIDUAL" if net != 0 else "FLAT_NET",
               **{k: snap[k] for k in ("carried_residual_base", "open_residual_base", "total_residual_base", "open_residual_plans", "limit_breaches")}}
        # owner 12 Nov (a): a non-zero residual at the end of the day is a WARNING, and a carried part-lot residual that reached one full hedge lot is a PERSISTENT ALARM (both keys appear only when raised)
        if net != 0 or Decimal(doc["total_residual_base"]) != 0:
            doc["warning"] = "NON_ZERO_RESIDUAL_AT_END_OF_DAY"
        if snap.get("lot_residual_alarm") is not None:
            doc["lot_residual_alarm"] = snap["lot_residual_alarm"]
        bf = self.session.breaker_report()
        dust_seen = (bf.get("breaker") or {}).get("max_dust_residual_base")
        if bf.get("breaker_flatten", {}).get("dust_now") or dust_seen:       # owner 13 Nov: dust (below one lot / below the venue minimum) is counted and shown here, key only when there was any
            doc["breaker_dust"] = {"max_dust_residual_base": dust_seen, "flatten_dust_now": (bf.get("breaker_flatten") or {}).get("dust_now"), "counters": (bf.get("breaker_flatten") or {}).get("counters")}
        return doc

    def result(self) -> dict[str, Any]:
        s = self.session
        orders, fills = [], []
        lifetime_ids = {e["order_id"] for e in s.ledger.events if e["event_type"] == "ORDER_LIFETIME_EXPIRED"}
        withdrawn = {e["order_id"]: e.get("reason") for e in s.ledger.events if e["event_type"] == "QUOTE_WITHDRAWN"}        # owner 12 Nov: the order file shows why a quote was cancelled (only runs with quote_withdraw on have any)
        for plan in s.oms.plans.values():
            for child in plan.children:
                terminal = [u for u in child.state_updates if str(u.get("resolved_order_state")) in ("CANCELLED", "FILLED", "REJECTED", "EXPIRED", "TIMED_OUT")]
                orders.append({"cancel_reason": child.reject_reason or (f"quote_withdrawn:{withdrawn[child.order_id]}" if child.order_id in withdrawn else None), "terminal_ts_ns": int(terminal[-1]["event_ts_ns"]) if terminal else None,
                               "lifetime_expired": child.order_id in lifetime_ids, "order_id": child.order_id, "venue": child.venue, "side": child.side.value, "quantity": str(child.quantity), "limit_price": None if child.limit_price is None else str(child.limit_price),
                               "post_only": bool(child.post_only), "state": child.state.value, "filled_quantity": str(child.filled_quantity), "average_price": str(child.average_price), "fee": str(child.fee),
                               "intent_id": child.intent_id})      # E0 step (e): the only new field in session_orders.jsonl
        for e in s.ledger.events:
            if e.get("event_type") == "FILL":
                fills.append({k: (str(v) if isinstance(v, Decimal) else v) for k, v in e.items()})
        mids = {}
        for (venue, instrument_id), book in s.books.items():
            if book.bids and book.asks:
                mids[venue] = (book.best_bid + book.best_ask) / 2
        positions = {f"{v}:{i}": str(q) for (v, i), q in s.ledger.positions.items()}
        cash = {v: str(c) for v, c in s.ledger.cash_deltas.items()}
        equity = sum((s.ledger.cash_deltas.get(v, Decimal(0)) for v in mids), Decimal(0)) + sum((q * mids[v] for (v, i), q in s.ledger.positions.items() if v in mids), Decimal(0))
        signals = [{"opportunity_id": k, "ts": sig.ts, "buy_venue": sig.buy_venue, "sell_venue": sig.sell_venue, "qty": sig.qty, "buy_price": sig.buy_price, "sell_price": sig.sell_price,
                    "net_edge_bps": sig.net_edge_bps} for k, sig in self.shim.signals.items()]
        return {
            "label": "EXPLORATORY: session runtime, not a result to quote", "runtime": "session", "profile": self.profile.get("name"), "pair": self.config.pair,
            "days": [self.config.start_date, self.config.end_date], "size_base": float(self.config.arb.max_position_base), "fee_presets": self.profile.get("fee_presets"),
            "events": dict(self.events), "signals": signals, "orders": orders, "fills": fills, "positions": positions, "cash_by_venue": cash, "equity_at_book_mids": str(equity),
            "residual_at_end": self._residual_at_end(),
            **({"_lifecycle": self._lifecycle(fills, list(self.decomp.rows) if self.decomp is not None else [])} if self.lifecycle_report else {}),
            **({"_ems_coverage": self._ems_coverage()} if self.lifecycle_report else {}),
            **({"participation": {"cap_on": bool(self.config.maker.participation_cap), "prints_it_binds_on": self.executor.diag.get("participation_cap_would_bind", 0.0), "excess_fill_qty": self.executor.diag.get("participation_cap_excess_qty", 0.0)}}
               if self.executor.diag.get("participation_cap_would_bind") or self.config.maker.participation_cap else {}),        # E3: only when the cap is on or something would have bound, so every earlier run's manifest is unchanged
            "ledger_event_counts": dict(Counter(e["event_type"] for e in s.ledger.events)),
            "strategy_counters": {"evaluations": self.strategy.evaluations, "signals": self.strategy.signals, "best_edge_seen_bps": None if self.strategy.best_edge_seen_bps == float("-inf") else self.strategy.best_edge_seen_bps,
                                  "rejects": {k: v for k, v in self.strategy.rejects.items() if v}},
            "reject_trace": self.reject_trace,
            "execution_layer": self._layer_report(), "intent_journal": list(s.intent_observer.layer.journal) if s.intent_observer is not None else None, "_maker_observations": self._observation_rows() if self.obs is not None else None, "_decision_events": [e for e in s.ledger.events if e["event_type"] in DECISION_EVENTS] or None, "intent_fills": list(s.intent_observer.layer.fills) if s.intent_observer is not None else None,
            **({} if self.decomp is None else {"markout_decomposed": {
                "label": "EXPLORATORY: decomposed markouts (captured C, adverse D, M = C + D, fee, net) per fill and horizon, bps of the mid at the fill, positive = good for us; mid at the fill = the mid before the event that caused it",
                "by_venue_role": self.decomp.decomposed_summary(by=("venue", "role")), "excluded_counts": self.decomp.excluded_counts(), "fills_skipped_no_mid_before": self.decomp_skipped_no_mid,
                "rows": len(self.decomp.rows), "age_rule": "off (no mid_max_age_s)"}, "_markout_rows": list(self.decomp.rows)}),
            "markout_summary": s.markouts.summary() if s.markouts is not None else None, "markout_by_venue_role": s.markouts.summary_by_venue_role() if s.markouts is not None else None,
            "gate_events": [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in e.items() if k in ("event_type", "ts_ns", "reason", "chain_id", "parent_id")}
                            for e in s.ledger.events if e["event_type"] in ("HEDGE_DISPATCH_HELD", "TIME_INTEGRITY_BLOCK", "ORDER_LIFETIME_EXPIRED", "ORDER_REJECT_CLASSIFIED", "HEDGE_TOUCH_PAST_PLANNED_LIMIT", "HEDGE_ALARM")][:500],
            "grid_snaps": dict(self.shim.grid_snaps), "fills_by_source": {n: dict(a.fills_by_source) for n, a in self.adapters.items()},
            "manifest": {
                "invalid_book_policy": s.book_policy_report(self.last_ns),
                "coverage": self._coverage(),
                "suspended_seconds_two_ways": {"any_message_age (the rule), common hours only": self._seconds_by_day([(a, b) for a, b, c in s.book_gate.open_intervals(self.last_ns)], self._common_window_ns()),
                                               "book_change_age_only (older reading), common hours only": self._book_only_seconds(self._common_window_ns()),
                                               "any_message_age, whole run (incl. the span after a venue's data ends: a data limitation, not a suspension)": s.book_policy_report(self.last_ns)["suspended_seconds_total_by_day"]},
                "price_sanity_core_stats": {f"{v}": {k: getattr(b.stats, k) for k in ("price_jumps", "confirmed_price_moves", "wide_spreads", "crossed_or_locked", "stale_transitions", "invalid_levels", "resyncs", "snapshots", "deltas_applied")}
                                            for (v, _), b in s.books.items()},
                "time_integrity_blocks": sum(1 for e in s.ledger.events if e["event_type"] == "TIME_INTEGRITY_BLOCK"),
                "model_choices": {"consume_liquidity_on_own_taker_fills": {**{n: a.consume_liquidity for n, a in self.adapters.items()}, "status": "DELIBERATE_MODEL_CHOICE (owner 12 Nov): an own hedge or unwind fill removes the walked quantity from the book it walked; the public view book is not changed by it (every level on both venues, tested)"}},
                **({"hedge_fault_injection": {"faults": self.config.hedge_faults.as_dict(), "injected_by_venue": {n: dict(a.hedge_faults_injected) for n, a in self.adapters.items()},
                                              "ghost_hedges_held_at_end": {n: len(a._held) for n, a in self.adapters.items()}, "ghost_hedges_delivered": {n: len(a.ghost_delivered) for n, a in self.adapters.items()},
                                              "not_simulated": "feed darkness (outage kind 'full'), delay, partial_prob, a lost ack on an order that reached the venue, fast-move reject, restart, disconnects: refused, not ignored. A static outage of the MAKER venue is simulated (blackhole policy: order entry lost, fill reports held until the reconnect)"}}
                   if self.hedge_fault_injection else {}),
                **({"hedge_thin_book": {"attempts_levels_x_quantity": [list(a) for a in self.hedge_thin_book], "injected_by_venue": {n: dict(a.hedge_faults_injected) for n, a in self.adapters.items()},
                                        "label": "the hedge venue's truth book shows LESS than the capture for those attempts (no depth is added)"}} if self.hedge_thin_book else {}),
                **({} if not (self.mark_from_mid or self.mark_ticks["exchange_mark"]) else {"mark_source": {
                    "exchange_mark_ticks": self.mark_ticks["exchange_mark"], "MARK_FROM_MID_ticks": self.mark_ticks["MARK_FROM_MID"], "venues_with_exchange_marks": sorted(self._exchange_mark_venues),
                    "label": "the margin check marks a position with the venue's exchange mark when the recorded derivatives stream has one (age at most mark_max_age, 5 s ASSUMED), else with the last good mark or valid book mid (kept for up to 5 s; after that the price is missing and new risk is paused, not the kill switch); MARK_FROM_MID is the replay's "
                             "labelled fallback for a venue with no exchange mark (replay only; funding is not settled from it)"}}),
                "sequence_gap_counter_of_the_core_book": "NOT REPORTED: synthetic sequence, gaps NOT_MEASURED",
                "entry_gates": {"book_age_rule": "core, 500 ms, age from the last message of any kind (owner 8 Nov Q-G1)", "dead_feed_5s": "not a blocking gate; the resubscribe trigger only (Q-G2)", "price_sanity_500bps": "core, kept (Q-G3)",
                                "time_integrity": "core, host-offset correction on (Q-G4)", "circuit_breaker": ("in the core (CircuitBreaker + pre-send risk gate in TradingSession, Track A item 1, off by default); " + ("ON: the profile's breaker block (profile_safety)" if self.safety_from_profile.get("breaker") else "NOT SWITCHED ON in this replay (no breaker_config is passed)") + "; old-path trips are in the parity report")}, "feed": self.feed.manifest(), "ticks": self.ticks, "quantity_step": {"values": quantity_steps(), "label": published_rules()["label"], "fetched": published_rules()["fetched"], "sources": {v: r["source"] for v, r in published_rules()["venues"].items()}},
                "config_notes": self.config_notes, "safety_from_profile": self.safety_from_profile, "timing": {"latency_model": None if self.latency_model is None else self.latency_model.report(), "truth_books": dict(self.timing_stats)}, "release_identity": self.config.release_identity,
                "differences_from_backtester_loop": ["timing is shared (profile timing block through Timelines; venue-truth books built by this driver): the old text 'no timing timelines or venue-truth books' is no longer true (corrected 10 Nov)",
                                                     "STILL MISSING in the session runtime (Phase E): E0 ems/ layer (repricing, tactics, PASSIVE placements, risk gate, pairing, venue selector); E1 maker order lifetime as a timer (the OMS deadline fires only on an event or tick); "
                                                     "E2 the fault presets and the conditional scenarios other than outage_on_fill (hedge-fault injection itself is built and opt-in: hedge_fault_injection=True, ghost and partial hedge included; the manifest key hedge_fault_injection lists what it refuses); E3 participation cap and size sweep; circuit breaker: in the core but not switched on in this replay; the backtester's margin step (the core margin engine replaces it)",
                                                     "message-end cross invalidates the core book", "sequence SYNTHETIC, gaps NOT_MEASURED"],
            },
        }


DECISION_EVENTS = ("HEDGE_CAP_SIZE_AWARE", "QUOTE_WITHDRAWN")


def s0_audit(ledger_events: list[dict], plans: dict, reprice_counters: dict | None = None) -> dict:
    """Case 2i on the arb (owner 14 Nov, S0 in the session runtime): the counts of the EMS-note section 5 invariants a-j from one replay's ledger. Read-only; decides nothing.
      amends: requested / confirmed / rejected / timed out / fallback-to-cancel / accepted-but-not-confirmed;  fills_during_amend_pending: fills of the OLD order between its amend request and the venue's answer;
      cancels_during_amend: cancel or withdraw requests on a lineage member while its amend was pending;  duplicate_fills_booked_twice: (order, cumulative) pairs booked more than once (must be 0);
      lineage_overfill: a lineage that filled more than the quote's quantity (must be 0);  maker_filled / hedge_filled: totals, hedged = filled check;  two_live_orders: the most venue-live primary orders of ONE quote at once, minus one
      (must be 0: live = from the venue's acknowledgement to the terminal report; at the same instant an end is processed before a start)."""
    from collections import Counter, defaultdict
    from decimal import Decimal
    ev = sorted(ledger_events, key=lambda e: e["ts_ns"])
    kinds = Counter(e["event_type"] for e in ev)
    requested = [e for e in ev if e["event_type"] == "AMEND_REQUESTED"]
    answers = [e for e in ev if e["event_type"] in ("AMEND_CONFIRMED", "AMEND_REJECTED", "AMEND_TIMEOUT")]
    fills = [e for e in ev if e["event_type"] == "FILL"]
    life = [e for e in ev if e["event_type"] == "ORDER_LIFECYCLE"]
    during = cancels_during = 0
    for r in requested:
        answer = next((a for a in answers if a["order_id"] == r["order_id"] and a["ts_ns"] >= r["ts_ns"]), None)
        end = answer["ts_ns"] if answer else float("inf")
        members = {r["order_id"], r.get("replacement_order_id")}
        during += sum(1 for f in fills if f["order_id"] == r["order_id"] and r["ts_ns"] < f["ts_ns"] < end)
        cancels_during += sum(1 for e in life if e.get("lifecycle_event") in ("CANCEL_REQUESTED", "CANCEL") and e.get("order_id") in members and r["ts_ns"] < e["ts_ns"] < end) \
            + sum(1 for e in ev if e["event_type"] == "QUOTE_WITHDRAWN" and e.get("order_id") in members and r["ts_ns"] < e["ts_ns"] < end)
    seen, dup = Counter(), 0
    for f in fills:
        key = (f["order_id"], f.get("cumulative_filled_quantity"))
        seen[key] += 1
    dup = sum(1 for v in seen.values() if v > 1)
    primary_qty = {}
    for plan in plans.values():
        for c in plan.children:
            if c.order_role == "PRIMARY" and c.post_only and not getattr(c, "replaces_order_id", None):
                primary_qty[plan.parent_id] = primary_qty.get(plan.parent_id, Decimal("0")) + c.quantity
    filled_by_plan = defaultdict(lambda: Decimal("0"))
    for plan in plans.values():
        for c in plan.children:
            if c.order_role == "PRIMARY" and c.post_only:
                filled_by_plan[plan.parent_id] += c.filled_quantity
    overfill = sum(1 for pid, q in primary_qty.items() if filled_by_plan[pid] > q)
    maker_filled = sum((c.filled_quantity for plan in plans.values() for c in plan.children if c.order_role == "PRIMARY" and c.post_only), Decimal("0"))
    hedge_filled = sum((c.filled_quantity for plan in plans.values() for c in plan.children if c.order_role == "HEDGE"), Decimal("0"))
    primary_ids = {c.order_id: plan.parent_id for plan in plans.values() for c in plan.children if c.order_role == "PRIMARY" and c.post_only}
    points = []
    for e in life:
        oid = e.get("order_id")
        if oid not in primary_ids:
            continue
        le = e.get("lifecycle_event")
        if le in ("ACKNOWLEDGED", "REPLACED"):
            points.append((e["ts_ns"], 1, oid))
        elif le in ("CANCELLED", "FILLED", "REJECTED", "TIMED_OUT", "EXPIRED"):
            points.append((e["ts_ns"], 0, oid))
    live, worst = defaultdict(set), 0
    for ts, is_start, oid in sorted(points):                         # at the same instant an end (0) sorts before a start (1)
        pid = primary_ids[oid]
        (live[pid].add if is_start else live[pid].discard)(oid)
        worst = max(worst, len(live[pid]))
    return {"label": "EXPLORATORY: mechanics of S0 repricing on the 2026-10-29 capture, no profit claim",
            "amend_requests": kinds["AMEND_REQUESTED"], "amends_confirmed": kinds["AMEND_CONFIRMED"], "amends_rejected": kinds["AMEND_REJECTED"], "amend_timeouts": kinds["AMEND_TIMEOUT"],
            "amend_fallback_cancel": kinds["AMEND_FALLBACK_CANCEL"], "amend_accepted_not_confirmed": kinds["AMEND_ACCEPTED_NOT_CONFIRMED"],
            "fills_during_amend_pending": during, "cancels_during_amend": cancels_during, "quotes_withdrawn": kinds["QUOTE_WITHDRAWN"],
            "late_fills_reducing_replacement": sum(1 for r in requested if any(f["order_id"] == r["order_id"] and f["ts_ns"] > r["ts_ns"] for f in fills)),
            "duplicate_fills_booked_twice": dup, "lineage_overfill": overfill, "lineage_overfill_alarms": kinds["AMEND_LINEAGE_OVERFILL"],
            "maker_filled": str(maker_filled), "hedge_filled": str(hedge_filled), "hedged_equals_filled": maker_filled == hedge_filled,
            "two_live_orders": max(0, worst - 1), "controller_counters": dict(reprice_counters or {})}


def participation_audit(fills: list[dict], traded: dict[tuple[str, str], float], *, tolerance: float = 1e-9) -> dict:
    """E3 check (owner 13 Nov): over a whole replay, the quantity our own PASSIVE fills took at each (venue, price level) never exceeds the public volume printed at that level (`traded`: (venue, price) -> volume, summed
    from the capture's prints). Returns the per-level table and `excess` (the levels that break the rule: it must be empty). A necessary condition only: the per-print rule is tested in the fill engine; this
    catches any print spent twice anywhere in a day."""
    from decimal import Decimal

    def key(venue, price):
        return (str(venue).upper(), format(Decimal(str(price)).normalize(), "f"))
    own: dict[tuple[str, str], float] = {}
    for row in fills:
        order = row.get("order") or {}
        if not order.get("post_only", False):
            continue
        k = key(row["venue"], row["fill_price"])
        own[k] = own.get(k, 0.0) + float(row["fill_quantity"])
    public = {key(v, p): q for (v, p), q in traded.items()}
    levels = [{"venue": k[0], "price": k[1], "own_filled": q, "public_traded": public.get(k, 0.0), "own_over_public": (q / public[k]) if public.get(k, 0.0) > 0 else None}
              for k, q in sorted(own.items())]
    excess = [lv for lv in levels if lv["own_filled"] > lv["public_traded"] + tolerance]
    return {"levels": levels, "excess": excess, "levels_checked": len(levels), "ok": not excess}


# ---- W2a (owner 17 Nov; audit A2 2.4, 2.5): the replay interlock, the run record (input and output hashes, data span, speed) and the session compare -----------------------------------
RUN_RECORD = "session_run_record.json"


def replay_interlock(session=None, out_dir=None) -> None:
    """A replay can never be mistaken for, or written over, a live session. Refused (ValueError):
      a session that is not INTERNAL_SIMULATION, or that has an adapter other than the simulated venue (`QueueAwareVenueAdapter`): a replay never reaches a venue;
      an output directory that is a live recording (it holds `live_manifest.json`, `live_part_manifest.json` or a `part-*` directory, or sits inside one): a replay never writes into a capture."""
    if session is not None:
        if session.execution_mode is not ExecutionMode.INTERNAL_SIMULATION:
            raise ValueError(f"REPLAY_INTERLOCK: a replay runs only in INTERNAL_SIMULATION, not {session.execution_mode}")
        real = sorted(v for v, a in session.router.adapters.items() if not isinstance(a, QueueAwareVenueAdapter))
        if real:
            raise ValueError(f"REPLAY_INTERLOCK: a replay runs only on the simulated venue; other adapters for {real}")
    if out_dir is not None:
        out_dir = Path(out_dir)
        if out_dir.is_dir() and any(out_dir.glob("part-*")):
            raise ValueError(f"REPLAY_INTERLOCK: {out_dir} holds part-* (a live recording): a replay never writes there")
        for d in (out_dir, *out_dir.parents[:2]):               # the directory itself, or a recording it sits inside (a recorder session holds part-N/live_part_manifest.json)
            if d.is_dir() and ((d / "live_manifest.json").exists() or (d / "live_part_manifest.json").exists()
                               or any((p / "live_part_manifest.json").exists() for p in d.glob("part-*"))):
                raise ValueError(f"REPLAY_INTERLOCK: {out_dir} is, or is inside, a live recording ({d}): a replay never writes there")


def file_sha256(path, uncompressed: bool = False) -> str:
    """sha256 of the file's bytes; with `uncompressed`, of the gzip stream's CONTENT (the recorder's part manifest hashes the file before it was compressed)."""
    import hashlib
    h = hashlib.sha256()
    with (gzip.open(path, "rb") if uncompressed else Path(path).open("rb")) as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _recorder_hash(path: Path) -> str | None:
    """The sha256 the live recorder wrote for this file in its part manifest (of the uncompressed file), or None."""
    manifest = path.parent / "live_part_manifest.json"
    if not manifest.exists():
        return None
    try:
        files = json.loads(manifest.read_text()).get("files", {})
    except (OSError, ValueError):
        return None
    return (files.get(path.name.split(".")[0]) or {}).get("sha256")


def input_file_rows(paths: Iterable, content_hash: bool = True) -> list[dict]:
    """One row per input file a run opened: bytes, sha256 of the file as it is on disk and, for a gzip file, of its content; where the recorder's part manifest has a hash for the file, that hash and
    whether the content equals it (a recording changed after it was made shows as recorder_match false)."""
    rows = []
    for p in paths:
        p = Path(p)
        if not p.exists():
            rows.append({"path": str(p), "missing": True})
            continue
        row = {"path": str(p), "bytes": p.stat().st_size, "sha256": file_sha256(p)}
        if p.suffix == ".gz" and content_hash:
            try:
                row["content_sha256"] = file_sha256(p, uncompressed=True)
            except (OSError, EOFError) as e:
                row["content_sha256"] = None
                row["content_error"] = type(e).__name__          # a truncated gzip stream: the run used what could be read (event_loader.truncation_report)
        recorded = _recorder_hash(p)
        if recorded is not None:
            row["recorder_sha256"] = recorded
            row["recorder_match"] = (row.get("content_sha256", row["sha256"]) == recorded) if content_hash or p.suffix != ".gz" else None
        rows.append(row)
    return rows


def write_run_record(out_dir: Path, replay: "SessionReplay | None" = None, *, input_paths: Iterable | None = None, content_hash: bool = True, notes: dict | None = None) -> dict:
    """`session_run_record.json`, written LAST, next to the manifest and never inside it (a file cannot hold its own hash, and the wall time differs run to run): what went in and what came out.
      inputs: every data file the replay opened (`event_loader.files_read`), hashed; the profile's own hash is already in the manifest's release_identity.
      outputs: bytes and sha256 of every other file in the directory.
      data_start_ns / data_end_ns: the receive time of the first and last event processed; wall_s; speed_x = data seconds per wall second.
      effective_seeds, PYTHONHASHSEED and the Python version: what a second run needs to be the same run.
    The session compare leaves this file out (`compare_sessions`)."""
    import hashlib, os, platform
    out_dir = Path(out_dir)
    replay_interlock(out_dir=out_dir)
    inputs = input_file_rows(files_read() if input_paths is None else input_paths, content_hash=content_hash)
    outputs = {p.name: {"bytes": p.stat().st_size, "sha256": file_sha256(p)} for p in sorted(out_dir.iterdir()) if p.is_file() and p.name != RUN_RECORD}
    span = None
    if replay is not None and replay.first_ns is not None:
        span = (replay.last_ns - replay.first_ns) / _NS
    doc = {"label": "run record (W2a): input and output hashes, data span and speed. Not part of the identity comparison (wall time differs run to run)",
           "inputs": inputs, "inputs_sha256": hashlib.sha256(json.dumps([(r["path"].rsplit("/", 3)[-3:] if "/" in r["path"] else r["path"], r.get("sha256")) for r in inputs]).encode()).hexdigest(),
           "input_files": len(inputs), "input_bytes": sum(r.get("bytes", 0) for r in inputs), "inputs_recorder_checked": sum(1 for r in inputs if "recorder_match" in r),
           "inputs_recorder_mismatch": [r["path"] for r in inputs if r.get("recorder_match") is False], "inputs_missing": [r["path"] for r in inputs if r.get("missing")],
           "outputs": outputs, "outputs_sha256": hashlib.sha256(json.dumps(sorted((n, o["sha256"]) for n, o in outputs.items())).encode()).hexdigest(),
           "data_start_ns": None if replay is None else replay.first_ns, "data_end_ns": None if replay is None or replay.first_ns is None else replay.last_ns,
           "data_span_s": span, "wall_s": None if replay is None else replay.wall_s,
           "speed_x": (span / replay.wall_s) if span is not None and replay.wall_s else None,
           "effective_seeds": None if replay is None else replay.config_notes.get("effective_seeds"),
           "python_hash_seed": os.environ.get("PYTHONHASHSEED", "not set (str hashes differ per process; no output may depend on set or dict-of-str order, see the contract)"),
           "python": platform.python_version(), **({"notes": notes} if notes else {})}
    (out_dir / RUN_RECORD).write_text(json.dumps(doc, indent=1, default=str))
    return doc


# What the identity comparison leaves out of session_manifest.json: `run` (wall time, labels of the run wrapper) and `manifest.release_identity` (git state, code hash, absolute paths). Everything else is compared.
COMPARE_MANIFEST_EXCLUDED = (("run",), ("manifest", "release_identity"))


def _first_json_difference(a, b, path: str = "") -> dict | None:
    if type(a) is not type(b):
        return {"at": path or "(root)", "a": repr(a)[:200], "b": repr(b)[:200]}
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                return {"at": f"{path}.{k}" if path else str(k), "a": "(absent)" if k not in a else repr(a[k])[:200], "b": "(absent)" if k not in b else repr(b[k])[:200]}
            d = _first_json_difference(a[k], b[k], f"{path}.{k}" if path else str(k))
            if d:
                return d
        return None
    if isinstance(a, list):
        for i, (x, y) in enumerate(zip(a, b)):
            d = _first_json_difference(x, y, f"{path}[{i}]")
            if d:
                return d
        return None if len(a) == len(b) else {"at": f"{path}[{min(len(a), len(b))}]", "a": f"{len(a)} items", "b": f"{len(b)} items"}
    return None if a == b else {"at": path or "(root)", "a": repr(a)[:200], "b": repr(b)[:200]}


def compare_sessions(dir_a, dir_b, *, skip: Iterable[str] = (RUN_RECORD,), manifest_excluded=COMPARE_MANIFEST_EXCLUDED) -> dict:
    """Two replay output directories, file by file. A .jsonl file: rows equal as text, else the FIRST differing row (number, the first differing field, both values). A .json file: equal bytes, else the first
    differing key path; `session_manifest.json` is compared without the sections in `manifest_excluded`. Any other file: bytes, else the first differing offset. A file present on one side only is a difference.
    Returns {"equal", "files": {name: {"status": EQUAL | DIFFERENT | ONLY_A | ONLY_B | EQUAL_EXCEPT_EXCLUDED, ...}}, "different": [names]}. Read-only."""
    a, b = Path(dir_a), Path(dir_b)
    names = sorted(({p.name for p in a.iterdir() if p.is_file()} | {p.name for p in b.iterdir() if p.is_file()}) - set(skip))
    files: dict[str, dict] = {}
    for name in names:
        pa, pb = a / name, b / name
        if not pa.exists() or not pb.exists():
            files[name] = {"status": "ONLY_A" if pa.exists() else "ONLY_B"}
            continue
        ba, bb = pa.read_bytes(), pb.read_bytes()
        if ba == bb:
            files[name] = {"status": "EQUAL", "bytes": len(ba)}
            continue
        if name.endswith(".jsonl"):
            la, lb = ba.splitlines(), bb.splitlines()
            row = next((i for i, (x, y) in enumerate(zip(la, lb)) if x != y), min(len(la), len(lb)))
            diff = {"row": row + 1, "rows_a": len(la), "rows_b": len(lb)}
            if row < len(la) and row < len(lb):
                try:
                    diff["first_difference"] = _first_json_difference(json.loads(la[row]), json.loads(lb[row]))
                except ValueError:
                    diff["first_difference"] = {"a": la[row][:200].decode("utf-8", "replace"), "b": lb[row][:200].decode("utf-8", "replace")}
            files[name] = {"status": "DIFFERENT", **diff}
        elif name.endswith(".json"):
            try:
                ja, jb = json.loads(ba), json.loads(bb)
            except ValueError:
                files[name] = {"status": "DIFFERENT", "offset": next((i for i, (x, y) in enumerate(zip(ba, bb)) if x != y), min(len(ba), len(bb)))}
                continue
            excluded = []
            if name == "session_manifest.json":
                for keys in manifest_excluded:
                    for doc in (ja, jb):
                        node = doc
                        for k in keys[:-1]:
                            node = node.get(k, {}) if isinstance(node, dict) else {}
                        if isinstance(node, dict):
                            node.pop(keys[-1], None)
                    excluded.append(".".join(keys))
            d = _first_json_difference(ja, jb)
            files[name] = {"status": "EQUAL_EXCEPT_EXCLUDED", "excluded": excluded} if d is None else {"status": "DIFFERENT", "first_difference": d, **({"excluded": excluded} if excluded else {})}
        else:
            files[name] = {"status": "DIFFERENT", "offset": next((i for i, (x, y) in enumerate(zip(ba, bb)) if x != y), min(len(ba), len(bb)))}
    different = [n for n, f in files.items() if f["status"] not in ("EQUAL", "EQUAL_EXCEPT_EXCLUDED")]
    return {"a": str(a), "b": str(b), "equal": not different, "files_compared": len(files), "different": different, "skipped": sorted(skip), "files": files}


# ---- W2a hygiene (owner 17 Nov, decision 13: no logic under out/): the evidence files the day and scenario run wrappers used to build themselves. Moved here as they were: the bytes written are the same.
SAFETY_EVENT_PREFIXES = ("BREAKER_", "LIMIT_STATE", "FEED_HEALTH", "KILL_SWITCH", "UNWIND_", "HEDGE_ALARM", "HEDGE_DUST", "HEDGE_CHAIN", "HEDGE_STATE", "MARGIN_", "NEW_RISK_HELD", "RECONCILIATION", "QUOTE_SKIPPED", "QUOTE_WITHDRAWN", "RESIDUAL_", "LAYER_RISK", "RISK_FLATTEN", "ORDER_LIFETIME", "TIME_INTEGRITY", "ORDER_TIMED_OUT", "ORDER_REJECT_CLASSIFIED")


def safety_event_text(ledger_events: list[dict], prefixes: tuple = SAFETY_EVENT_PREFIXES) -> str:
    """The ledger's safety events in full (payloads included), one JSON line each, in ledger order. Empty text when there are none."""
    safety = [e for e in ledger_events if e["event_type"].startswith(prefixes)]
    return "".join(json.dumps({k: (str(v) if not isinstance(v, (int, float, bool, type(None), list, dict)) else v) for k, v in e.items()}, sort_keys=True, default=str) + "\n" for e in safety)


def write_evidence(replay: "SessionReplay", result: dict, out_dir: Path, traded: dict | None = None) -> None:
    """After `write_result`: the files the rehearsal report reads that the result itself does not hold. The safety events in full; one file with every stop-quoting condition and every limit counter of the run,
    read from the session (counts are complete, never a truncated list); the participation samples when the run recorded any; the participation audit (own passive fills against the public volume printed
    at the same level, `traded` = {(venue, price text): quantity} summed from the capture by the caller); the impact overlay's summary when the run had one. Aggregates only."""
    import collections
    out = Path(out_dir)
    replay_interlock(out_dir=out)
    if getattr(replay, "impact", None) is not None:                     # step C: what the overlay did (aggregates only)
        ov = replay.impact
        (out / "session_impact.json").write_text(json.dumps({"label": "EXPLORATORY: level-3 impact overlay; the settings are in impact_settings, the label says where they come from", "impact_settings": dataclasses.asdict(ov.cfg) if hasattr(ov.cfg, "__dataclass_fields__") else {},
            "counts": ov.counts, "orders": len(ov.orders), "orders_by_venue": {v: sum(1 for o in ov.orders if o.venue == v) for v in ov.ticks}, "mean_peak_bps": (sum(o.peak_bps for o in ov.orders) / len(ov.orders)) if ov.orders else None,
            "max_peak_bps": max((o.peak_bps for o in ov.orders), default=None), "final_applied_ticks": ov.applied_ticks, "tick": ov.ticks}, indent=1))
    sess = replay.session
    led = sess.ledger.events
    (out / "session_safety_events.jsonl").write_text(safety_event_text(led))
    lim = getattr(sess.router, "scoped_limits", None)
    rejects = getattr(replay.strategy, "rejects", None)
    doc = {"label": "EXPLORATORY: every counter of the run; counts are complete (nothing truncated)",
           "ledger_event_type_counts": dict(sorted(collections.Counter(e["event_type"] for e in led).items())),
           "stop_quoting": {"signals_blocked_by_breaker": sess.signals_blocked_by_breaker, "breaker": sess.breaker_report().get("breaker"), "breaker_alarms": list(sess.breaker_alarms), "hedge_dust_carry": sess.breaker_report().get("hedge_dust_carry"),
                            **({"feed_health": {k: v for k, v in sess.breaker_report()["feed_health"].items() if k != "transition_rows"}} if sess.breaker_report().get("feed_health") is not None else {}), "session_limits": sess.breaker_report().get("session_limits"), "no_requote": sess.breaker_report().get("no_requote"), "amend_guard": sess.breaker_report().get("amend_guard"), "reprice_counters": dict(sess.reprice_counters), "amend_refusals": {k: v for k, v in sess.oms.realism_counters.items() if "amend" in k},
                            "margin_price_paused_at_end": sess.margin_price_paused, "signals_blocked_by_margin_price": sess.signals_blocked_by_margin_price,
                            "quotes_skipped_rate_limit": sess.quotes_skipped_rate_limit, "quotes_withdrawn": sess.quotes_withdrawn, "risk_gate_refusals_by_reason": dict(sess.gate_counts),
                            "kill_switch": bool(sess.oms.risk.kill_switch), "kill_switch_reason": getattr(sess.oms.risk, "kill_switch_reason", ""),
                            "strategy_reject_counts": (dict(rejects.counts) if hasattr(rejects, "counts") else (dict(rejects) if isinstance(rejects, dict) else None)),
                            "oms_new_risk_held": {k: v for k, v in sess.oms.realism_counters.items() if k.startswith("new_risk_held")}},
           "limits_that_bound": {"rate_limiter": None if lim is None else lim.snapshot(getattr(replay, "last_ns", None)), "risk_engine_refusals": dict(getattr(sess.oms.risk, "refusals", {}) or {}),
                                 "layer_gate": {k: v for k, v in (result.get("execution_layer", {}).get("layer", {}).get("counts", {}) or {}).items() if "risk" in k or "refus" in k},
                                 "residual_breaches": result["manifest"].get("residual_risk") if isinstance(result.get("manifest"), dict) else None,
                                 "oms_realism_counters": dict(sess.oms.realism_counters)},
           "safety_from_profile": getattr(replay, "safety_from_profile", None)}
    (out / "session_limits_and_stops.json").write_text(json.dumps(doc, indent=1, default=str))
    if replay.strategy.participation_samples:
        (out / "session_participation_samples.jsonl").write_text("".join(json.dumps({"ts": r[0], "price": r[1], "qty": r[2], "volume_in_window": r[3]}) + "\n" for r in replay.strategy.participation_samples))
    audit = participation_audit(sess.ledger_fill_rows() if hasattr(sess, "ledger_fill_rows") else [e for e in led if e["event_type"] == "FILL"], dict(traded or {}))
    (out / "session_participation_audit.json").write_text(json.dumps({"label": "EXPLORATORY: own passive fills vs the public volume printed at the same level over the day", **audit}, indent=1))


def write_stage_trace(replay: "SessionReplay", result: dict, path: Path) -> int:
    """The stage trace of a run (crypto_stat_arb.metrics.stage_trace over the ledger, each signal as a `signal-trigger` row), one JSON line per row. Returns the row count."""
    from crypto_stat_arb.metrics import stage_trace
    trace = stage_trace(replay.session.ledger.events, [(s["ts"], "signal-trigger", "book") for s in result["signals"]])
    Path(path).write_text("".join(json.dumps(r, sort_keys=True, default=str) + "\n" for r in trace))
    return len(trace)


def write_result(result: dict, out_dir: Path) -> None:
    replay_interlock(out_dir=out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("signals", "orders", "fills"):
        with (out_dir / f"session_{name}.jsonl").open("w") as fh:
            for row in result[name]:
                fh.write(json.dumps(row, default=str) + "\n")
    if result.get("reject_trace") is not None:
        with (out_dir / "session_reject_trace.jsonl").open("w") as fh:
            for ts, reason in result["reject_trace"]:
                fh.write(json.dumps([ts, reason]) + "\n")
    for key, name in (("intent_journal", "session_intent_journal"), ("intent_fills", "session_intent_fills")):      # E0 step (a): new log files, observation only
        if result.get(key) is not None:
            with (out_dir / f"{name}.jsonl").open("w") as fh:
                for row in result[key]:
                    fh.write(json.dumps(row, default=str) + "\n")
    if result.get("_maker_observations") is not None:       # E0 step (g): a new file, observation only
        with (out_dir / "maker_observations.jsonl").open("w") as fh:
            for row in result["_maker_observations"]:
                fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    if result.get("_lifecycle") is not None:             # owner 12 Nov: the lifecycle report by regime, a new file next to the statistics block; only when the replay was asked for it
        (out_dir / "session_lifecycle_report.json").write_text(json.dumps(result["_lifecycle"], indent=1, default=str))
    if result.get("_ems_coverage") is not None:           # owner 12 Nov (section J): the EMS path coverage table, tactic and placement of every order, the per-order checks
        (out_dir / "session_ems_path_coverage.json").write_text(json.dumps(result["_ems_coverage"], indent=1, default=str))
    if result.get("_decision_events"):                    # owner 12 Nov: every size-aware hedge cap decision and every quote withdrawal, one line each; written only when the run produced any (every earlier profile writes nothing new)
        with (out_dir / "session_decision_events.jsonl").open("w") as fh:
            for row in result["_decision_events"]:
                fh.write(json.dumps({k: (str(v) if isinstance(v, Decimal) else v) for k, v in row.items()}, sort_keys=True, default=str) + "\n")
    if result.get("_markout_rows"):
        with (out_dir / "session_markouts_decomposed.jsonl").open("w") as fh:
            for row in result["_markout_rows"]:
                fh.write(json.dumps(row, default=str) + "\n")
    if result.get("intent_journal") is not None:       # 11 Nov (09 Nov markout note): linked pair economics per maker fill, a new file; reads what the run wrote, changes nothing in it
        from crypto_stat_arb.metrics import pair_economics
        pe = pair_economics(result["fills"], result["intent_journal"],
                            hedge_price_basis="simulated fill price at hedge arrival (size-aware where the venue book has depth; with best bid/ask only, as Binance in March 2024, the touch is assumed to fit the size)")
        with (out_dir / "session_pair_economics.jsonl").open("w") as fh:
            for row in pe["rows"]:
                fh.write(json.dumps(row, default=str) + "\n")
        (out_dir / "session_pair_economics.json").write_text(json.dumps({"label": "EXPLORATORY: linked pair economics per maker fill, session runtime", "summary": pe["summary"], "metadata": pe["metadata"]}, indent=1, default=str))
    (out_dir / "session_manifest.json").write_text(json.dumps({k: v for k, v in result.items() if k not in ("signals", "orders", "fills", "reject_trace", "intent_journal", "intent_fills", "_markout_rows", "_maker_observations", "_decision_events", "_lifecycle", "_ems_coverage")}, indent=1, default=str))


# ---- parity (D5): v1_5 on the session against v1_4 on the backtester's loop ----------------------------------------------------------------------------------------------------
# Tolerances, written in the prompt before any run: signals equal in count and within 1 ms; orders equal in count, side and quantity with the limit price within 1 tick of the venue;
# fills equal in count, within 1 ms and 1 tick; P&L within 0.05 USDT per slice at size 1 SOL. A difference above a tolerance is a finding to classify (rounding, BUG_BACKTEST,
# BUG_CORE, BY_DESIGN), never something to tune away. Fills are compared per (venue, side) because the two paths split one fill differently (the backtester reports partial chunks).
TOLERANCES = {"signal_time_s": 1e-3, "fill_time_s": 1e-3, "price_ticks": 1.0, "pnl_usdt": 0.05}


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _by_leg(fills: list[dict]) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for f in fills:
        leg = out.setdefault((f["venue"], f["side"]), {"qty": 0.0, "notional": 0.0, "first_ts": f["ts"], "last_ts": f["ts"], "rows": 0, "fee": 0.0})
        leg["qty"] += f["qty"]
        leg["notional"] += f["qty"] * f["price"]
        leg["first_ts"], leg["last_ts"] = min(leg["first_ts"], f["ts"]), max(leg["last_ts"], f["ts"])
        leg["rows"] += 1
        leg["fee"] += f.get("fee", 0.0)
    for leg in out.values():
        leg["vwap"] = leg["notional"] / leg["qty"] if leg["qty"] else 0.0
    return out


def old_fills(run_dir: Path) -> list[dict]:
    """The backtester's trades.jsonl as fills: venue, side, qty, price, time (seconds), fee."""
    return [{"venue": r["venue"], "side": r["side"], "qty": float(r["qty"]), "price": float(r["price"]), "ts": float(r["ts"]), "fee": float(r.get("fee", 0.0))} for r in _rows(run_dir / "trades.jsonl")]


def new_fills(run_dir: Path) -> list[dict]:
    """The session's ledger FILL events as fills (venue names lower-cased to the feed's)."""
    out = []
    for e in _rows(run_dir / "session_fills.jsonl"):
        out.append({"venue": str(e["venue"]).lower(), "side": str(e["side"]).lower(), "qty": float(e["fill_quantity"]), "price": float(e["fill_price"]), "ts": int(e["ts_ns"]) / _NS,
                    "fee": float(e.get("fee_delta", e.get("fee", 0.0)) or 0.0)})
    return out


def compare_parity(old_dir: Path, new_dir: Path, ticks: dict[str, float], windows_ns: list[list] | None = None, until: float | None = None) -> dict:
    """Per (venue, side) leg: quantity, vwap price in ticks, first and last fill time, row counts; the signal count; every difference above a tolerance, each with whether it lies inside a
    suspension window of the session run. Classification is done by a person from this output and recorded in the run manifest (a machine cannot tell BUG_CORE from BY_DESIGN)."""
    keep = (lambda rows: rows) if until is None else (lambda rows: [r for r in rows if r['ts'] <= until])        # owner 8 Nov: only the hours where both venues have data
    o, n = _by_leg(keep(old_fills(old_dir))), _by_leg(keep(new_fills(new_dir)))
    windows = [(w[0] / _NS, w[1] / _NS) for w in (windows_ns or [])]
    inside = lambda t: any(a <= t <= b for a, b in windows)
    diffs = []
    for key in sorted(set(o) | set(n)):
        a, b = o.get(key), n.get(key)
        if a is None or b is None:
            diffs.append({"leg": key, "what": "leg present in only one run", "old": a, "new": b})
            continue
        tick = ticks[key[0]]
        if abs(a["qty"] - b["qty"]) > 1e-9:
            diffs.append({"leg": key, "what": "quantity", "old": a["qty"], "new": b["qty"]})
        if abs(a["vwap"] - b["vwap"]) > TOLERANCES["price_ticks"] * tick:
            diffs.append({"leg": key, "what": "price (ticks)", "old": a["vwap"], "new": b["vwap"], "ticks": (b["vwap"] - a["vwap"]) / tick})
        for edge in ("first_ts", "last_ts"):
            if abs(a[edge] - b[edge]) > TOLERANCES["fill_time_s"]:
                diffs.append({"leg": key, "what": f"time {edge}", "old": a[edge], "new": b[edge], "seconds": b[edge] - a[edge], "in_session_window": inside(a[edge]) or inside(b[edge])})
    old_summary = json.loads((old_dir / "summary.json").read_text()) if (old_dir / "summary.json").exists() else {}
    new_summary = json.loads((new_dir / "summary.json").read_text()) if (new_dir / "summary.json").exists() else {}
    return {"tolerances": TOLERANCES, "old_legs": {f"{k[0]}/{k[1]}": v for k, v in o.items()}, "new_legs": {f"{k[0]}/{k[1]}": v for k, v in n.items()},
            "signals": {"old": old_summary.get("signals"), "new": new_summary.get("signals")}, "differences": diffs, "windows": len(windows),
            "classification": "UNCLASSIFIED: to be classified by hand (rounding, BUG_BACKTEST, BUG_CORE, BY_DESIGN) and recorded in the run manifest"}


def order_times_old(events) -> list[dict]:
    """Per order on the backtester's loop, in send order: decision (= send; the decision is the send there), send, arrival (a passive order's `order_resting`, a taker's fill), first and last fill."""
    from types import SimpleNamespace
    rows, by_id = [], {}
    for e in events:
        if isinstance(e, dict):                                     # a row of order_events.jsonl
            e = SimpleNamespace(**e)
        if e.kind == "order_sent":
            row = {"venue": e.venue, "side": e.side, "decision": e.ts, "send": e.ts, "arrival": None, "fills": []}
            by_id[e.order_id] = row
            rows.append(row)
        elif e.kind == "order_resting" and e.order_id in by_id:
            by_id[e.order_id]["arrival"] = e.ts
        elif e.kind == "fill":
            owner = next((r for r in reversed(rows) if r["venue"] == e.venue and r["side"] == e.side and r["send"] <= e.ts), None)     # a fill belongs to the latest earlier order on its venue and side
            if owner is not None:
                owner["fills"].append(e.ts)
    return rows


def order_times_new(replay: "SessionReplay") -> list[dict]:
    """The same table from the session: decision (the opportunity's time), send (ORDER_IN_FLIGHT), arrival (ORDER_ARRIVED), fills (ledger FILL events), in send order."""
    s = replay.session
    decision = {}
    for e in s.ledger.events:
        if e["event_type"] == "OPPORTUNITY":
            decision[e.get("opportunity", {}).get("opportunity_id")] = e["ts_ns"] / _NS
    side = {c.order_id: (c.side.value.lower(), c.opportunity_id) for p in s.oms.plans.values() for c in p.children}
    rows, by_id = [], {}
    for t in s.router.trace:
        if t["record_type"] == "ORDER_IN_FLIGHT" and t["action"] == "SUBMIT":
            sd, opp = side.get(t["order_id"], ("", None))
            row = {"venue": t["venue"].lower(), "side": sd, "decision": decision.get(opp), "send": t["ts_ns"] / _NS, "arrival": t["arrival_ts_ns"] / _NS, "fills": []}
            by_id[t["order_id"]] = row
            rows.append(row)
    for e in s.ledger.events:
        if e["event_type"] == "FILL" and e.get("order_id") in by_id:
            by_id[e["order_id"]]["fills"].append(e["ts_ns"] / _NS)
    first_of: dict = {}
    for t in s.router.trace:                                  # a hedge's decision is the maker fill that triggered it (FILL_TRIGGERED), as on the backtester's loop
        if t["record_type"] == "ORDER_IN_FLIGHT" and t["action"] == "SUBMIT" and t["order_id"] in by_id:
            opp = side.get(t["order_id"], ("", None))[1]
            if opp in first_of and first_of[opp]["fills"]:
                by_id[t["order_id"]]["decision"] = first_of[opp]["fills"][0]
            first_of.setdefault(opp, by_id[t["order_id"]])
    return rows


def compare_order_times(old_rows: list[dict], new_rows: list[dict], tolerance_s: float = 1e-3) -> list[dict]:
    """Order by order (send order): each of decision, send, arrival, first and last fill with its difference and whether it is within the stated tolerance (1 ms)."""
    out = []
    for i in range(max(len(old_rows), len(new_rows))):
        a = old_rows[i] if i < len(old_rows) else None
        b = new_rows[i] if i < len(new_rows) else None
        if a is None or b is None:
            out.append({"order": i, "what": "order present in only one run", "old": a, "new": b})
            continue
        row = {"order": i, "venue": (a["venue"], b["venue"]), "side": (a["side"], b["side"])}
        for name, x, y in (("decision", a["decision"], b["decision"]), ("send", a["send"], b["send"]), ("arrival", a["arrival"] if a["arrival"] is not None else (a["fills"] or [None])[0], b["arrival"]),
                           ("first_fill", (a["fills"] or [None])[0], (b["fills"] or [None])[0]), ("last_fill", (a["fills"] or [None])[-1], (b["fills"] or [None])[-1])):
            row[name] = {"old": x, "new": y, "diff_s": None if x is None or y is None else y - x, "within_tolerance": None if x is None or y is None else abs(y - x) <= tolerance_s}
        out.append(row)
    return out


def live_paper_setup(profile_name: str) -> dict:
    """Phase D, D7d (owner 9 Nov): what `live-paper --strategy-setup new_backtest.session_replay:live_paper_setup --strategy-profile NAME` needs to run the PROFILE's strategy in a paper session: the same
    `ArbSessionStrategy` over `CrossVenueArbStrategy` the session replay builds, the profile's maker settings for the queue simulator, and the session rules the replay passes to `TradingSession`
    (fill-triggered dispatch, the maker venue, the hedge policy, the timing latency model, the maker order lifetime). Returns {"session_kwargs", "queue_simulation", "strategy_builder"}.
    The live feed supplies books and time; nothing here reads a dataset (the run-specific fields given to apply_profile are placeholders; the tick sizes come from the live config file, which `out/integration/paper_live/make_config.py` builds with the dated March ticks)."""
    from dataclasses import asdict

    from .profiles import apply_profile, load_profile

    profile = load_profile(profile_name)
    cfg = apply_profile(profile, pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", data_root="", output_dir="")
    if cfg.runtime != "session":
        raise ValueError(f"profile {profile_name} does not select the session runtime")
    venues = tuple(cfg.venues)
    names = {v: v.upper() for v in venues}
    fee_specs, _ = core_fee_specs(cfg.pair, cfg.bybit_fee_preset, cfg.binance_fee_preset)
    specs = make_venues(cfg.latency_preset, cfg.bybit_fee_preset, cfg.binance_fee_preset, fee_specs=fee_specs)
    venue_specs = {v: specs[v] for v in venues}
    for v, trust in (cfg.trust_local_ts or {}).items():
        if v in venue_specs:
            venue_specs[v] = replace(venue_specs[v], trust_local_ts=bool(trust))
    hf, hedge_cfg = cfg.hedge_faults, (cfg.execution.hedge if getattr(cfg, "execution", None) is not None else {})
    hedge_policy = HedgePolicy(retry_limit=int(getattr(hf, "max_retries", 0) or 0), retry_backoff_ns=int(float(getattr(hf, "retry_backoff_s", 0.0) or 0.0) * _NS),
                               query_attempt_limit=int(getattr(hf, "unknown_query_max_attempts", 3) or 3),
                               **hedge_policy_fields(hedge_cfg, cfg.arb.max_walk_bps), **hedge_dust_fields(profile))
    timelines = Timelines(cfg.timing) if cfg.timing is not None else None
    # W2a (owner 17 Nov): the same rules as the replay, from the one builder. A paper session of v1_9 or later runs the PROFILE's own safety values (its breaker block and its layer risk mode, enforce;
    # `profile_safety_default`), and every paper session its execution config, the intent journal and the markout horizons: before this it ran none of them (audit A2 12.2 finding 4).
    layer_risk_mode, breaker_config = profile_safety_values(profile, profile_safety_default(profile))
    session_kwargs = profile_session_rules(cfg, profile, names, hedge_policy=hedge_policy, latency_model=TimelinesLatencyModel(timelines) if timelines is not None else None,
                                           layer_risk_mode=layer_risk_mode, breaker_config=breaker_config)

    def build(paper) -> list:
        session = paper.session
        if cfg.arb.maker_max_age_s > 0:
            session.oms.order_lifetime_ns = int(cfg.arb.maker_max_age_s * _NS)       # the profile's MANDATORY maker order lifetime, the same rule as the replay
        strategy = CrossVenueArbStrategy(cfg.pair, venue_specs, cfg.arb)
        return [ArbSessionStrategy(strategy, ledger=session.ledger, oms=session.oms, states={v: paper._states[names[v]] for v in venues}, instruments=paper.cfg.instruments,
                                   instrument_id=paper.cfg.instrument_id, venue_names=names, eligibility_policy=session.alignment_policy,
                                   view_books=lambda: {v: paper._views[names[v]] for v in venues})]

    from crypto_stat_arb.config import amend_guard_from_profile
    guard = amend_guard_from_profile(profile)
    if guard is not None:
        session_kwargs["amend_guard"] = guard             # owner 16 Nov: read by TradingSession only when it sends real orders
    return {"session_kwargs": session_kwargs, "queue_simulation": asdict(cfg.maker), "strategy_builder": build}


def _open_jsonl(path: Path):
    """Open a .jsonl or .jsonl.gz file for line-by-line reading."""
    gz = path.with_suffix(".jsonl.gz") if path.suffix == ".jsonl" else None
    if gz and gz.exists():
        note_file_read(gz)
        return gzip.open(gz, "rt")
    note_file_read(path)
    return path.open()


def iter_recorded_events(session_dir):
    """A recorded LIVE session (the recorder's directory) as a stream of core events in the recorder's own arrival order (trace.jsonl): books from canonical.jsonl, public prints from trades.jsonl. Ticker, control,
    best-bid/ask and derivative frames are not market events; the book connection's control frames are `load_keepalive_notes`. Accepts plain .jsonl and gzip-compressed .jsonl.gz files.
    One part at a time (owner 11 Nov, the 1-day replay): a day of SOLUSDT is about 4.9 million events, more than a list of them holds in memory. Every trace line of a part refers to a row of the SAME part
    (checked on the 2026-10-29 capture: 0 unresolved of 4.9 million); a reference that is not found raises, never skips."""
    for part in sorted(Path(session_dir).glob("part-*")):
        can, trades, deriv = {}, {}, None
        if (part / "derivatives.jsonl").exists() or (part / "derivatives.jsonl.gz").exists():      # the recorder's derivatives frames (mark, index, funding): yielded as DerivativeTicker; a part without the file has none
            deriv = {}
            with _open_jsonl(part / "derivatives.jsonl") as fh:
                for line in fh:
                    row = json.loads(line)
                    deriv[row["event_id"]] = row
        with _open_jsonl(part / "canonical.jsonl") as fh:
            for line in fh:
                row = json.loads(line)
                can[row["event_id"]] = row
        with _open_jsonl(part / "trades.jsonl") as fh:
            for line in fh:
                row = json.loads(line)
                trades[row["event_id"]] = row
        with _open_jsonl(part / "trace.jsonl") as fh:
            for line in fh:
                row = json.loads(line)
                if row["disposition"] == "CANONICAL":
                    yield MarketEvent.from_dict(can[row["canonical_event_id"]])
                elif row["disposition"] == "TRADE":
                    yield TradeEvent.from_dict(trades[row["canonical_event_id"]])
                elif row["disposition"] == "DERIVATIVE" and deriv is not None:
                    yield DerivativeTicker.from_dict(deriv[row["canonical_event_id"]])


def load_recorded_events(session_dir) -> list:
    """The whole recorded session as one list (a short capture; for a day use `iter_recorded_events`)."""
    return list(iter_recorded_events(session_dir))


def load_keepalive_notes(session_dir) -> list[tuple[int, str]]:
    """(receive_ts_ns, venue) of every frame on a BOOK connection that is not a book update: Bybit pongs and acknowledgements, any non-book Binance depth-channel frame, read from the recorder's wire file.
    Applied as `TradingSession.note_venue_message`, the rule the live service uses (a frame on the book stream proves it alive; owner 9 Nov)."""
    notes = []
    for part in sorted(Path(session_dir).glob("part-*")):
        wire = part / "wire.jsonl"
        if not wire.exists() and not wire.with_suffix(".jsonl.gz").exists():
            continue
        for line in _open_jsonl(wire):
            w = json.loads(line)
            ch, fd = w["channel"], w["frame_data"]
            if w["venue"] == "BYBIT" and ch.startswith("orderbook") and '"topic":"orderbook' not in fd:
                notes.append((w["receive_ts_ns"], "BYBIT"))
            elif w["venue"] == "BINANCE" and "depth" in ch and '"e":"depthUpdate"' not in fd and '"lastUpdateId"' not in fd:
                notes.append((w["receive_ts_ns"], "BINANCE"))
    return sorted(notes)


def feed_health_config_table(profile_names: Iterable[str]) -> dict:
    """W1 (audit A1 1.1): the effective feed-health settings of each named profile, resolved through the same loaders a run uses (no data is read; the run-specific fields are placeholders).
    Report only. A profile whose runtime is `backtester` runs on the backtester's own loop, where the core time policy is NOT used: its row says so."""
    from .profiles import apply_profile, load_profile
    out = {}
    for name in profile_names:
        profile = load_profile(name)
        cfg = apply_profile(profile, dataset="mar2024", data_root="", pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir="/tmp/_feed_health_table")
        venues = tuple(cfg.venues)
        try:
            raw, _notes = raw_config_from_profile(profile, pair=cfg.pair, venues=venues, ticks=dated_ticks(cfg.start_date), quantity_steps={v: quantity_steps()[v] for v in venues},
                                                  size_base=float(cfg.arb.max_position_base), seed=cfg.seed)
            app, _ = config_from_raw(raw)
            row = feed_health_config_report(app, profile)
            if row["runtime"] != "session":
                row["note"] = "runtime backtester: the core time policy is not used by this profile's own runs; the values are what a session-runtime run would get"
        except Exception as exc:                 # a profile the core loader refuses (a legacy or spot profile): reported, not guessed
            row = {"runtime": str(profile.get("runtime", "backtester")), "not_resolvable_through_the_core_loader": f"{type(exc).__name__}: {exc}"[:200]}
        out[name] = row
    return out
