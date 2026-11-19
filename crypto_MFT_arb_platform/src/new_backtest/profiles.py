"""WP7 — execution profiles and release identity.

A profile is a YAML file under profiles/ that sets every switch the engine exposes, each one classified:

    MANDATORY      a control that must stay on in a non-legacy profile (causal fills, shared trade volume, in-flight / pending /
                   UNKNOWN exposure, depth-size fix, trade de-duplication, staleness and dead-feed gates, breaker, recovery,
                   ledger bridge, product / fee consistency). Startup REJECTS a profile that disables one, unless the profile
                   is legacy_v0 (the comparison baseline, kept bit-identical).
    CALIBRATED     a number that comes from a measurement (fill model, latencies, failure costs); its source is named.
    LEGACY_COMPAT  a switch that only exists so the old behaviour stays reachable.

Unknown keys are errors. `apply_profile` builds the ArbBacktestConfig; `release_identity` stamps every result with the profile
hash, the code hash (sha256 over the engine sources + git revision + dirty flag), the data manifest hash, the instrument / fee
snapshot versions, the calibration cutoff and the seed, so two results can be told apart by what produced them.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, fields, is_dataclass, replace
from pathlib import Path
from typing import Any

from crypto_stat_arb.config import MANDATORY_BY_KEY, SENSITIVITY_ONLY_LATENCY, check_mandatory as core_check_mandatory, latency_preset_problem

from .config import FEE_PRESETS, LATENCY_PRESETS

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"
ENGINE_DIR = Path(__file__).resolve().parent
PLATFORM_DIR = ENGINE_DIR.parents[1]          # the platform checkout / release root (config/, profiles/ live here)


class ProfileError(ValueError):
    pass


# B2 step 8 (owner, 7 Nov 2026): these profiles keep the old accounting modules (funding_settler, margin_model, the fee presets, the backtester's own equity books)
# so their results reproduce byte for byte. Every other profile, v1_4 and every later version, uses only the core accounting.
REFERENCE_PROFILES = frozenset({"legacy_v0", "perp_perp_maker_taker_v1", "perp_perp_maker_taker_v1_1", "perp_perp_maker_taker_v1_2", "perp_perp_maker_taker_v1_3",
                                "spot_perp_basis_v1", "spot_spot_maker_taker_v1"})
CONTROL_PROFILES = frozenset({"perp_perp_maker_taker_v1_1"})          # a reference profile that is the CONTROL of the working comparisons
CORE_ACCOUNTING = {"fee_source": "core", "funding_source": "core", "margin_source": "core", "ledger_source": "core"}


def profile_status(name: str | None) -> str:
    """CONTROL (v1.1), REFERENCE_ONLY (the other reference profiles) or WORKING (everything else: core accounting only)."""
    if name in CONTROL_PROFILES:
        return "CONTROL"
    return "REFERENCE_ONLY" if name in REFERENCE_PROFILES else "WORKING"


def profile_label(name: str | None) -> str:
    """The profile name as every runner prints it: reference profiles carry their status, a working profile is printed as it is."""
    st = profile_status(name)
    return f"{name} [{st}]" if st != "WORKING" else str(name)


def require_core_accounting(cfg, name: str | None = None) -> None:
    """Refuse a non-reference profile (WORKING) that would call an old accounting module: fee presets, funding_settler, margin_model, the backtester's equity books.
    Called at profile load (apply_profile) and again when the backtester starts, so an override or a later edit of the config is caught too."""
    name = name or (getattr(cfg, "release_identity", None) or {}).get("profile")
    if name is None or profile_status(name) != "WORKING":
        return
    bad = {k: getattr(cfg, k, None) for k, want in CORE_ACCOUNTING.items() if getattr(cfg, k, None) != want}
    if bad:
        raise ProfileError(f"profile {name} is a working profile and uses only the core accounting; {bad} would call an old accounting module "
                           f"(the old modules are for the reference profiles {sorted(REFERENCE_PROFILES)} only, they move to discarded/ in Phase F)")


RUNTIMES = ("backtester", "session")


def require_runtime(cfg, name: str | None = None) -> None:
    """Phase D (owner 8 Nov): the session runtime (TradingSession behind the QueueAwareVenueAdapter) is for v1_5 and later. A reference profile refuses it, at profile load and again when the
    backtester starts, so an override or a later edit is caught too. Parity is v1_5 against v1_4, so v1_4 itself stays on the backtester's loop by default."""
    runtime = getattr(cfg, "runtime", "backtester")
    if runtime not in RUNTIMES:
        raise ProfileError(f"runtime must be one of {RUNTIMES}, got {runtime!r}")
    name = name or (getattr(cfg, "release_identity", None) or {}).get("profile")
    if runtime == "session" and name is not None and profile_status(name) != "WORKING":
        raise ProfileError(f"profile {name} is a reference profile ({profile_status(name)}) and refuses the session runtime: reference results reproduce on the backtester's own loop only")

@dataclass(frozen=True)
class Switch:
    key: str                  # dotted path into the profile document (and into ArbBacktestConfig unless `target` says otherwise)
    cls: str                  # MANDATORY | CALIBRATED | LEGACY_COMPAT
    forbidden: tuple = ()     # MANDATORY only: values that count as "disabled" (rejected outside legacy_v0)
    note: str = ""


def _mandatory(key: str) -> Switch:
    """A MANDATORY control: the rule (key, forbidden values, note) lives in the core (`crypto_stat_arb.config.MANDATORY_CONTROLS`), once; this is its row in the switch table."""
    c = MANDATORY_BY_KEY[key]
    return Switch(c.key, "MANDATORY", c.forbidden, c.note)


SWITCHES: tuple[Switch, ...] = (
    Switch("arb.profile", "LEGACY_COMPAT", note="legacy_v0 | perp_perp_maker_taker_v1 | spot_* (edge expressions and gate)"),
    _mandatory("arb.dispatch_mode"),
    _mandatory("valuation"),
    _mandatory("equity_bridge"),
    _mandatory("enforce_book_state"),
    _mandatory("trade_dedupe"),
    _mandatory("delta_uses_book_size"),
    _mandatory("maker.share_trade_volume"),
    _mandatory("maker.dedupe_trade_depth"),
    _mandatory("maker.exchange_time_eligibility"),
    _mandatory("arb.count_pending_exposure"),
    _mandatory("arb.count_unknown_exposure"),
    _mandatory("arb.maker_max_age_s"),
    _mandatory("timing.dead_feed_s"),
    _mandatory("breaker"),
    _mandatory("hedge_faults.response"),
    _mandatory("cancel_time_basis"),
    Switch("timing.equal_time_policy", "LEGACY_COMPAT", note="market_first (v1) | cancel_first (legacy tie rule)"),
    _mandatory("timing.truth_books"),
    Switch("maker.cancel_model", "CALIBRATED", note="proportional (fill-evidence study 2026-10-30)"),
    Switch("maker.unknown_queue_model", "CALIBRATED", note="depth_aware; floor / distribution pending calibration (WP4)"),
    Switch("maker.vanish_clears_queue", "LEGACY_COMPAT", note="True in v1 (PROVISIONAL)"),
    Switch("maker.fill_evidence", "CALIBRATED", note="confirm_delta 20 ms in v1 (PROVISIONAL)"),
    Switch("maker.level_creator_trade_only", "LEGACY_COMPAT", note="opt-in"),
    Switch("maker.opposite_cross_fills", "CALIBRATED", note="True in v1 (PROVISIONAL): fill in full once the opposite best quote reaches our price (probe cross-check vs hftbacktest 2026-11-03)"),
    Switch("maker.opposite_cross_confirm_ms", "CALIBRATED", note="0 (off) in v1; v1.1 step 8 sets 500: a cross fills only with a reaching print on the same venue within +-500 ms, else it is dropped (EXPLORATORY window)"),
    Switch("maker.opposite_cross_cap_to_print", "CALIBRATED", note="False in v1; v1.1 step 9c sets True: a confirmed cross fills at most the reaching prints' unused volume (EXPLORATORY)"),
    Switch("arb.round_quotes_to_tick", "LEGACY_COMPAT", note="False in v1 (off-grid quotes); v1.1 step 9b sets True: bids round down, asks up, onto the declared venue tick"),
    Switch("message_atomic_book", "LEGACY_COMPAT", note="False in v1 / v1.1 (each delta is applied, repaired and evaluated on its own); v1.2 sets True: the deltas of one exchange message are one unit (cross judged at the message end; strategy, touch and maker queue updates run after the whole message)"),
    Switch("arb.join_touch_on_tick_rounding", "LEGACY_COMPAT", note="False in v1 / v1.1 (a quote rounded onto the touch is refused); v1.2 sets True: it joins the back of the touch queue if the edge at the rounded price passes"),
    Switch("hedge_arrival_book", "LEGACY_COMPAT", note="view in v1 (default); v1.1 step 9e sets truth: hedge IOCs execute on the venue-truth book at arrival"),
    _mandatory("arb.net_ceiling_multiple"),
    Switch("arb.p_fail", "CALIBRATED", note="SCENARIO, not calibrated (PLACEHOLDER 0.07; no execution logs exist)"),
    Switch("arb.loss_fail_bps", "CALIBRATED", note="SCENARIO, not calibrated (7.8 from a simulated hedge-failure study, not execution logs)"),
    Switch("arb.expected_hedge_move_bps_bid", "CALIBRATED", note="training-day executed hedge move"),
    Switch("adverse_table", "CALIBRATED", note="absent in every profile up to v1_5 (the single pinned hedge move is used); v1_6 (S1) pins a table fitted on market-data labels of TRAIN days only: expected hedge move by depth reached, volatility and flow, in place of that number in the quote price, the entry rule and the repricing rule"),
    Switch("maker.participation_cap", "CALIBRATED", note="absent in every profile up to v1_9 (default False, bit-identical); True (owner 12 Nov, E3; v1_9 sets it, owner 13 Nov): one budget per public print across every own order and every rule, so the own fills a print causes never exceed its volume; the fills it would have clipped are counted either way (diag participation_cap_would_bind)"),
    Switch("arb.entry_hedge_price", "CALIBRATED", note="touch in every profile up to v1_8 (default, bit-identical: the hedge is priced at the touch in the quote price and the entry edge); v1_9 (owner 13 Nov) sets vwap: both use the VWAP the hedge-venue book gives for the size the quote will have, the same walk quote_validity uses, so entry and quote_validity give the same net edge; expected_hedge_slippage_bps stays 0 by design"),
    Switch("arb.funding_expected_holding_s", "CALIBRATED", note="absent in every profile (0 = off: funding_holding_bps stays the constant, bit-identical); owner 13 Nov (edge calibration item 3): > 0 adds the expected funding cost of holding the pair that long, from the predicted rates of the derivative stream, with arb.funding_credit (ASSUMED floor_zero) and arb.funding_rate_max_age_s (ASSUMED 600). v1_9 (owner 14 Nov) sets 28800 (8 h, ASSUMED, to be replaced by the measured median holding time from the stage-0 logs) with floor_zero"),
    Switch("arb.funding_window_s", "CALIBRATED", note="absent in every profile up to v1_8 (0 = off, bit-identical); v1_9 (owner 14 Nov) 30: no new quote within 30 s before a funding timestamp on either venue, resting quotes withdrawn at the window start, quoting resumes when the derivative stream shows the next settlement (at most arb.funding_window_after_s, ASSUMED 30 s, after the timestamp); a run with no next-funding time labels the window not evaluable (reject funding_window_not_evaluable) and blocks nothing"),
    Switch("arb.participation_max", "CALIBRATED", note="absent in every profile up to v1_8 (0 = off, bit-identical); v1_9 (owner 13 Nov, E3) 0.10 over arb.participation_window_s 60: a new quote's size is capped at that share of the public volume printed at its price level on the maker venue in the window; below the minimum clip the quote is refused (reject participation); hedges are never blocked. ASSUMED, owner to approve"),
    Switch("arb.quote_withdraw", "CALIBRATED", note="absent in every profile up to v1_7; v1_8 (owner 12 Nov) sets true: a resting quote is cancelled when its edge at the current hedge-venue VWAP for its size falls below the entry threshold minus arb.quote_withdraw_hysteresis_bps (ASSUMED 0.5); withdrawal only, no repricing"),
    Switch("do_not_quote", "CALIBRATED", note="absent in every profile up to v1_6; v1_7 (S3, owner 10 Nov) pins a do-not-quote filter fitted on market-data labels of TRAIN days only (ridge of the 50 ms hedge-venue move on quote-time features; skip when the naive edge after fees plus the predicted move is below its threshold), in place of block_on_toxic. EXPLORATORY until simulator validation"),
    Switch("latency_preset", "CALIBRATED", note="ASSUMED until ollog measurements replace it"),
    _mandatory("margin"),
    _mandatory("postonly_reject_model"),
    _mandatory("product_fee_consistency"),
    Switch("book_tape_repair", "LEGACY_COMPAT", note="engine default True (and in v1): the trade tape lowers book levels, which double-depletes when the depth decrease arrives first; v1.1 sets False"),
    Switch("own_impact", "LEGACY_COMPAT", note="legacy (v1): own taker consumption mutates the public book; overlay (v1.1): a shadow overlay cleared at the next public update of that level; refill (owner 15 Nov, off in every profile): the shadow persists through public updates and refills as 1 - exp(-t / impact.own_refill_tau_s)"),
    Switch("maker.ledger_epoch", "LEGACY_COMPAT", note="off in v1; v1.1 sets True: a snapshot / reset invalidates old netting credit, ambiguous associations are flagged"),
    Switch("execution.primary.reprice", "CALIBRATED", note="none in v1 / v1.1 / v1_2 (a resting quote is never repriced); v1_3 carries the S0 rule with every number null and enabled false: the trader (or the live values) supplies them, nothing is fitted"),
    Switch("placement_book", "LEGACY_COMPAT", note="view in v1 / v1.1 (a passive order joins the strategy's feed-delayed book); v1_2 carries the switch: truth = the venue-truth book at the arrival's exchange time, the book PostOnly is judged on, when it is complete there, otherwise the view book (counted)"),
    Switch("fee_source", "LEGACY_COMPAT", note="presets in v1 / v1.1 / v1_2 / v1_3 / legacy_v0 (the backtester's own FEE_PRESETS); core in v1_4 (B2 step 4): the preset names are tier names of the core FeeSchedule (crypto_stat_arb.fees.FEE_TIERS) and every rate a run uses is that schedule's booked rate"),
    Switch("funding_source", "LEGACY_COMPAT", note="settler in v1 / v1.1 / v1_2 / v1_3 / legacy_v0 (funding_settler, mark = the record's mark price else the book mid, a settlement with no mark books 0 with source none); core in v1_4 (B2 step 5): the core FundingEngine books it, same mark rule, a book-mid mark is flagged FUNDING_MARK_FALLBACK and counted in every result, a settlement with no mark is flagged FUNDING_MARK_MISSING (blocking)"),
    Switch("margin_source", "LEGACY_COMPAT", note="model in v1 / v1.1 / v1_2 / v1_3 / legacy_v0 (margin_model.py decides breach and admission); core in v1_4 (B2 step 6): the core RiskEngine margin path decides (tiers, ledger collateral, cross, A9 fail-closed above the last tier, worst-case admission); above_last_tier is then redundant; transfers, isolated mode and a utilisation limit are refused"),
    Switch("ledger_source", "LEGACY_COMPAT", note="backtester in v1 / v1.1 / v1_2 / v1_3 / legacy_v0 (equity, cash P&L and the equity bridge from the float books); core in v1_4 (B2 step 7): from the core ledger at the common-timestamp marks, with the strict bridge, reconciliation and economics gate of the core (a run stops on a failed bridge or a failed gate unless equity_bridge is warn)"),
    Switch("resync_delay_s", "LEGACY_COMPAT", note="session runtime only (Phase D, owner 8 Nov): seconds trading stays suspended after a book goes invalid with no snapshot in the data, before the venue is resubscribed and resynced from the data's own state. ASSUMED. Ignored by the backtester runtime"),
    Switch("runtime", "LEGACY_COMPAT", note="backtester in every profile up to v1_4 (the backtester's own event loop); session in v1_5 (Phase D): the core TradingSession behind the QueueAwareVenueAdapter, fed by the canonical feed converter. Reference profiles refuse it; parity is v1_5 against v1_4"),
    Switch("trust_local_ts", "CALIBRATED", note="ASSUMED. True on both venues in v1 / v1.1 (a recorded local_ts is the receive time); v1.2 sets False per venue for the March data, whose local stamps are constructed (exchange + exactly 2.000 ms): the latency preset feed delay applies instead, one sample per exchange message"),
    Switch("maker.fill_through_prints", "LEGACY_COMPAT", note="False in v1 / v1.1 (a print through our price only burns queue); v1.2 sets True together with through_print_volume_limited: the order fills when a print goes through its price, limited by the print's volume. True alone (the full version) is refused at startup"),
    Switch("maker.through_print_volume_limited", "LEGACY_COMPAT", note="False in v1 / v1.1; v1.2 True: the through-print fill is limited by the print volume, one budget shared by every order the print went through"),
    Switch("maker.depletion_ledger", "LEGACY_COMPAT", note="False in v1 / v1.1 (a print netted against an earlier depth decrease drains nothing more: book-first leaves the queue ahead further back with size behind us, and trade_after_front uses the whole print); v1.2 sets True: the decrease records the head movement it applied, the print moves the queue only by the rest, and netted pairs with no other proof are counted"),
    Switch("maker.unknown_queue_dist", "CALIBRATED", note="floor in v1 (hand-set 25); v1.1: fitted on strictly earlier days, floor 0, flagged fallback 25 where the fit has no data (unknown_queue_fallback)"),
    Switch("dislocation", "LEGACY_COMPAT", note="absent in every profile up to v1_12; v1_13 (W6, owner decision 5 of 16 Nov) carries the block: dislocation episodes between the two venues' mids (rolling baseline, hysteresis), classed DATA / VENUE_EVENT / TRADEABLE / UNCLASSIFIED and recorded; NOTIFY-ONLY: nothing reads an episode, no action is wired; fail_closed is a definition and cannot be enabled; every threshold ASSUMED"),
    Switch("cost_records", "LEGACY_COMPAT", note="absent in every profile up to v1_13; v1_14 (W7 7.D only, owner 17 Nov) carries `estimate_at_decision: true`: each maker-taker signal row and its primary (maker) intent record gain an `estimate` dict, the pre-trade cost and edge terms the gate used, read from the same variables. It changes no decision; the other W7 keys are not built"),
    Switch("incident_capture", "LEGACY_COMPAT", note="absent in every profile up to v1_11; v1_12 (W2b, owner 17 Nov) carries the block: on a trigger the LIVE recorder cuts the last pre_seconds and the next post_seconds of the recording into its own small recording with a manifest (rule, window, frame counts, hashes, completeness). Read by the live feed service only; a replay never reads it; it changes no decision"),
    Switch("feed_health", "LEGACY_COMPAT", note="absent in every profile up to v1_10; v1_11 (W1, owner 17 Nov) carries the feed-health block: thresholds of the HEALTHY / DEGRADED / UNSAFE state model per (venue, instrument, stream), all ASSUMED; OBSERVATION only: the actions (block_new_quotes_on, withdraw_quotes_on) are none and feed_delayed_ms is null until the owner decides. Read by the session runtime only"),
    Switch("oms.session_limits", "LEGACY_COMPAT", note="absent in every profile up to v1_9; v1_10 (owner 16 Nov, Order 3) carries the approved limits: order_rate (A1), daily_loss_stop_usdt_per_sol_of_clip (A2), clock_offset (A3), margin_warning (A4), missing_marks_kill_s (A5), tier_table (A6), verify_account (A8). Read by the session runtime only; each has a counter and a LIMIT_STATE event. Each is a REAL_ORDER_REQUIRED row"),
    Switch("oms.account_fee_check", "LEGACY_COMPAT", note="absent in every profile up to v1_8; v1_9 (owner 13 Nov) true: a declaration only (a backtest has no account to read): a live or paper session started with this profile reads the account's SOLUSDT maker/taker rates on both venues and refuses to start if they differ from fee_presets. REQUIRED for any profile used with real orders (crypto_stat_arb.config.REAL_ORDER_REQUIRED)"),
    Switch("oms.maker_ack_deadline", "LEGACY_COMPAT", note="off in every profile up to v1_8 (a sent PostOnly quote with no answer stays SENT, unwatched); v1_9 (owner 13 Nov) ON: it is watched from the send and, unanswered, queried, TIMED_OUT, kill switch. REQUIRED for any profile used with real orders (crypto_stat_arb.config.REAL_ORDER_REQUIRED)"),
    Switch("arb.close_reservation", "LEGACY_COMPAT", note="F05 (owner 20 Nov): false (default, every profile; v1_16 writes it false) | true = closing quotes sized from the OMS's per-pair closable quantity, reserved until they end, reduce-only by the venue position; needs close_pairs"),
    Switch("arb.close_triggers", "LEGACY_COMPAT", note="F05 (owner 20 Nov): [edge] (default) | any of edge, time_stop, funding, inventory_limit, margin_limit, end_of_session; a non-edge trigger rests the closing quote at the maker touch (ASSUMED); needs close_pairs"),
    Switch("arb.max_pair_hold_s", "LEGACY_COMPAT", note="F05: the time_stop trigger's pair age, ASSUMED; 0 = off"),
    Switch("arb.funding_close_lead_s", "LEGACY_COMPAT", note="F05: the funding trigger fires within funding_window_s + this before a funding time, ASSUMED; 0 = the window alone"),
    Switch("arb.end_of_session_utc_s", "LEGACY_COMPAT", note="F05: the end_of_session trigger's UTC second of day, ASSUMED; -1 = off"),
    Switch("arb.close_unfilled_after_s", "LEGACY_COMPAT", note="F05: a closing quote resting longer is reported once as CLOSE_UNFILLED (no aggressive exit), ASSUMED; 0 = off"),
    Switch("arb.close_pairs", "LEGACY_COMPAT", note="absent (off) in every profile, v1_10 included (owner 16 Nov, Order 3 B): reduce_only_quote = while a pair is open, also quote the opposite maker side for at most the open size by the same edge formula in the reducing direction, hedge reduce-only; a SEPARATE switch for the owner's approval, measured on and off"),
    Switch("arb.requote_delay_after_withdraw_s", "CALIBRATED", note="absent (0 = off, bit-identical) in every profile up to v1_9; v1_10 (owner 16 Nov) 1.0 ASSUMED: a quote withdrawn by validity is not placed again on the same venue and side for this long; counted (requote_blocked_after_withdraw)"),
    Switch("margin.mark_stamp", "LEGACY_COMPAT", note="restamp in every profile up to v1_9 (LEGACY: a mark is stamped with the time the evaluation saw it, so it lives 9-10 s against the 5 s limit); v1_10 (owner 16 Nov) own_timestamp: a mark older than 5 s by its OWN timestamp pauses, nothing is re-stamped"),
    Switch("trace_rejects", "LEGACY_COMPAT", note="absent (false) in every profile: W2a (owner 17 Nov) made the session replay's reject trace a profile key (it was an attribute a run wrapper set on the config). true writes session_reject_trace.jsonl, one row per evaluation that ended in a reject; observation only, nothing reads it back"),
    Switch("seed", "LEGACY_COMPAT", note="owner 16 Nov: the run seed (latency draws, queue) is a profile key from v1_10 on, with the current default 7 (outputs unchanged); every session manifest records the effective seeds, frozen profiles included"),
    Switch("hedge_faults.seed", "LEGACY_COMPAT", note="owner 16 Nov: the hedge-fault seed is a profile key from v1_10 on, default 11 (outputs unchanged); recorded in the manifest"),
    Switch("hedge_faults.dust_carry", "LEGACY_COMPAT", note="absent (off) in every profile up to v1_9; v1_10 (owner 16 Nov) next_hedge: a hedge chunk below the hedge venue's minimum is carried in one signed balance per venue and added to the next hedge, instead of being refused and left open. REQUIRED for any profile used with real orders"),
    Switch("hedge_faults.dust_alarm_after_s", "LEGACY_COMPAT", note="ASSUMED 60 s (owner 16 Nov): carried dust older than this raises HEDGE_DUST_ALARM once; read only with dust_carry on"),
    Switch("hedge_faults.dust_max_notional_usdt", "LEGACY_COMPAT", note="F06 (owner 20 Nov) aggregate dust bound, USDT per direction over every venue and instrument (same instrument netted across venues); absent = no bound (every profile before v1_16); v1_16: 50 ASSUMED placeholder; needs dust_carry next_hedge"),
    Switch("hedge_faults.late_fill_after_close", "LEGACY_COMPAT", note="F06 c (owner 20 Nov): refuse (default, every profile before v1_16: a fill reported for an order of a closed plan is refused and raised) | reopen (v1_16: booked, the plan reopens and the ordinary hedge runs)"),
    Switch("hedge_faults.dust_max_age_s", "LEGACY_COMPAT", note="F06 (owner 20 Nov) carried dust older than this reaches the dust bound too; absent = no age bound; v1_16: 300 ASSUMED placeholder; needs dust_carry next_hedge"),
    Switch("hedge_faults.silent_carry_bound", "LEGACY_COMPAT", note="position_size in every profile up to v1_8 (a residual under the position size is carried with no action); v1_9 (owner 13 Nov) below_one_lot: a failed hedge unwinds whatever its size, only a sub-lot remainder is carried. REQUIRED for any profile used with real orders"),
    Switch("execution.price_cap_required", "CALIBRATED", note="execution layer: every non-unwind intent carries a worst price and every child stays inside it; true in both profiles (today's behaviour), CALIBRATED until the cap decision (brief step 11) is made"),
    _mandatory("execution.one_hedge_child"),
    _mandatory("execution.risk_reservation"),
)
MANDATORY = {s.key: s for s in SWITCHES if s.cls == "MANDATORY"}


def _get(doc: dict, dotted: str, default=None):
    cur: Any = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def load_profile(path: str | Path) -> dict:
    import yaml
    p = Path(path)
    if not p.exists() and (PROFILES_DIR / f"{path}.yaml").exists():
        p = PROFILES_DIR / f"{path}.yaml"
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or "name" not in doc:
        raise ProfileError(f"{p}: a profile is a mapping with a 'name'")
    doc["_path"] = str(p)
    doc["_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    return doc


def check_mandatory(doc: dict) -> list[str]:
    """Which MANDATORY controls the profile disables (the rule is the core's: `crypto_stat_arb.config.check_mandatory`). Empty for a valid non-legacy profile; legacy_v0 is exempt (and says so)."""
    return core_check_mandatory(doc)


NESTED = {"arb": "ArbConfig", "maker": "MakerConfig", "venue_state": "VenueStateConfig", "agent_flow": "AgentFlowConfig",
          "ack_latency": "AckLatencyConfig"}


def _build(cls, data: dict, *, where: str, nested: dict[str, type] | None = None):
    """Instantiate a dataclass from a mapping; nested mappings become the dataclasses named in `nested`; unknown keys are errors."""
    names = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - names)
    if unknown:
        raise ProfileError(f"{where}: unknown field(s) {unknown}")
    kw = {}
    for k, v in data.items():
        if isinstance(v, dict) and nested and k in nested:
            kw[k] = _build(nested[k], v, where=f"{where}.{k}")
        else:
            kw[k] = v
    return cls(**kw)


def apply_profile(doc: dict, dataset: str | None = None, **overrides):
    """ArbBacktestConfig from a loaded profile. `overrides` are run-specific fields (pair, dates, data_root, output_dir, …).
    `dataset` names the data the run reads ("mar2024", "jan2022"): the profile's `dataset_settings[dataset]` (owner 7 Nov: e.g. March Bybit's trade/depth windows) is merged over the profile
    for that dataset only; a profile without an entry for it, and every run that passes no dataset, is unchanged. What was applied is recorded in release_identity."""
    from .arb_strategy import ArbConfig
    from .backtester import ArbBacktestConfig
    from .background_agent_model import AgentFlowConfig
    from .circuit_breaker import CircuitBreakerConfig
    from .fill_engine import MakerConfig
    from .postonly import AckLatencyConfig
    from .venue_state import VenueStateConfig
    from crypto_stat_arb.simulation.impact import ImpactConfig
    from crypto_stat_arb.ems.venue_selector import SplitConfig
    from .hedge_faults import HedgeFaultConfig
    from .margin_model import MarginModelConfig, MarginTier, VenueMargin
    from .timing import ClockConfig, TimingConfig

    bad = check_mandatory(doc)
    if bad:
        raise ProfileError(f"profile {doc.get('name')}: " + "; ".join(bad))
    body = {k: v for k, v in doc.items() if not k.startswith("_") and k not in ("name", "description", "classification", "decision_status",
                                                                               "product_fee_consistency", "timing", "breaker", "margin",
                                                                               "fee_presets", "calibration", "execution", "hedge_move_calibration", "adverse_table", "do_not_quote", "dataset_settings", "oms", "feed_health", "incident_capture", "dislocation", "cost_records")}
    ds_applied = None
    ds_cfg = ((doc.get("dataset_settings") or {}).get(dataset) if dataset else None)
    if ds_cfg:
        body = {**body, **{k: ({**(body.get(k) or {}), **v} if isinstance(v, dict) and isinstance(body.get(k) or {}, dict) else v) for k, v in ds_cfg.items()}}
        ds_applied = {"dataset": dataset, "applied": ds_cfg}
    cfg_kw: dict[str, Any] = dict(body)
    cfg_kw["product_fee_consistency"] = bool(doc.get("product_fee_consistency", True))
    for k, v in (doc.get("fee_presets") or {}).items():
        cfg_kw[f"{k}_fee_preset"] = v
    timing_preset = None
    t = doc.get("timing")
    if t:
        t = dict(t)
        preset = timing_preset = t.pop("from_latency_preset", None) or doc.get("latency_preset")
        clock = t.pop("clock", None)
        if clock:
            t["clock"] = ClockConfig(**clock)
        floors = (t.pop("floors_ms", None) or {}).get(preset)        # ASSUMED floors per preset: they name the cross-region venue of THAT preset, so another preset gets none
        cfg_kw["timing"] = TimingConfig.from_latency_preset(LATENCY_PRESETS[preset], floors_ms=floors, **t)
    b = doc.get("breaker")
    if b:
        cfg_kw["circuit_breaker"] = CircuitBreakerConfig(**b)
    m = doc.get("margin")
    if m:
        venues = {}
        for v, spec in (m.get("venues") or {}).items():
            spec = dict(spec)
            tiers = tuple(MarginTier(**x) for x in spec.pop("tiers"))
            venues[v] = VenueMargin(tiers=tiers, **spec)
        rest = {k: v for k, v in m.items() if k not in ("venues", "mark_stamp")}      # mark_stamp is the session runtime's rule (core MarginConfig), not the backtester's margin model
        cfg_kw["margin"] = MarginModelConfig(venues=venues, **rest)
    ex = doc.get("execution")
    if ex is not None:
        from .execution import ExecutionConfig, ExecutionConfigError
        try:
            cfg_kw["execution"] = ExecutionConfig(**ex)
        except (ExecutionConfigError, TypeError) as e:
            raise ProfileError(f"profile {doc.get('name')}: execution block: {e}") from e
        if cfg_kw["execution"].pairing == "none":
            raise ProfileError(f"profile {doc.get('name')}: execution.pairing none is a market-making quote (no hedge leg), never an arb profile's pairing: this is an arb profile")
    hf = cfg_kw.pop("hedge_faults", None)
    if isinstance(hf, dict):
        cfg_kw["hedge_faults"] = HedgeFaultConfig(**{k: v for k, v in hf.items() if k not in ("silent_carry_bound", "dust_carry", "dust_alarm_after_s", "dust_max_notional_usdt", "dust_max_age_s", "late_fill_after_close")})      # the session runtime's residual bound (core config); the backtester's own loop has no such rule
    cal = doc.get("calibration")
    if cal:
        path = resolve_calibration(doc, cal)
        al = dict(cfg_kw.get("ack_latency") or {})
        cfg_kw["ack_latency"] = AckLatencyConfig(source="empirical", calibration_path=str(path), period=cal.get("period", "all"),
                                                 **{k: v for k, v in al.items() if k not in ("source", "calibration_path", "period")})
    cr = doc.get("cost_records") or {}
    unknown_cr = set(cr) - {"estimate_at_decision"}
    if unknown_cr:
        raise ProfileError(f"profile {doc.get('name')}: cost_records keys not built (W7: only estimate_at_decision): {sorted(unknown_cr)}")
    if cr.get("estimate_at_decision"):
        cfg_kw["arb"] = {**(cfg_kw.get("arb") or {}), "estimate_at_decision": True}     # W7 7.D: carried to the strategy config; the strategy writes the estimate, nothing reads it
    cfg_kw.update(overrides)
    sensitivity = cfg_kw.pop("latency_sensitivity", None)
    check_latency_preset(doc, cfg_kw.get("latency_preset"), timing_preset, sensitivity)
    cfg = _build(ArbBacktestConfig, cfg_kw, where=f"profile {doc.get('name')}",
                 nested={"arb": ArbConfig, "maker": MakerConfig, "venue_state": VenueStateConfig, "agent_flow": AgentFlowConfig,
                         "ack_latency": AckLatencyConfig, "impact": ImpactConfig, "split": SplitConfig})
    hm = doc.get("hedge_move_calibration")
    if hm:
        cfg.arb.expected_hedge_move_bps_bid, cfg.arb.expected_hedge_move_bps_ask, hm_info = load_hedge_move_calibration(doc, hm, cfg.start_date)
    at = doc.get("adverse_table")
    if at:
        cfg.arb.adverse_table, at_info = load_adverse_table(doc, at, cfg.start_date)
        cfg.arb.adverse_horizon_s = float(at.get("horizon_s", 1.0))
        cfg.arb.adverse_requote_horizon_s = float(at["requote_horizon_s"]) if at.get("requote_horizon_s") is not None else None
    dq = doc.get("do_not_quote")
    if dq:
        cfg.arb.do_not_quote, dq_info = load_do_not_quote(doc, dq, cfg.start_date)
    require_core_accounting(cfg, doc.get("name"))
    require_runtime(cfg, doc.get("name"))
    cfg.release_identity = release_identity(cfg, doc)
    if ds_applied:
        cfg.release_identity["dataset_settings"] = ds_applied
    cfg.release_identity["latency_sensitivity"] = sensitivity
    if hm:
        cfg.release_identity["hedge_move_calibration"] = hm_info
    if at:
        cfg.release_identity["adverse_table"] = {**at_info, "horizon_s": cfg.arb.adverse_horizon_s, "requote_horizon_s": cfg.arb.adverse_requote_horizon_s}
    if dq:
        cfg.release_identity["do_not_quote"] = dq_info
    return cfg


def load_adverse_table(doc: dict, at: dict, run_start: str):
    """S1: the AdverseTable a profile pins: a data file whose sha256 the profile records, fitted on TRAIN days that all end before the run starts, from market-data labels only.
    Refuses a missing file, bytes that no longer match the recorded hash, a file with no days or with a day on or after the run's start date, one not marked as fitted on market-data labels, and a horizon it does not hold."""
    from crypto_stat_arb.strategies.edge import AdverseTable
    p = Path(at["file"])
    path = p if p.is_absolute() else PLATFORM_DIR / p
    if not path.exists():
        raise ProfileError(f"profile {doc.get('name')}: adverse_table file {path} does not exist")
    got = hashlib.sha256(path.read_bytes()).hexdigest()
    if got != at.get("sha256"):
        raise ProfileError(f"profile {doc.get('name')}: adverse_table {path.name} has sha256 {got}, the profile pins {at.get('sha256')} (file edited after it was pinned)")
    table = AdverseTable.load(path)
    days = (table.fitted_on or {}).get("days") or []
    if not days or any(str(x) >= str(run_start) for x in days):
        raise ProfileError(f"profile {doc.get('name')}: adverse_table {path.name} must be fitted on TRAIN days strictly before the run start {run_start}; it lists {days}")
    if (table.fitted_on or {}).get("source") != "MARKET_DATA_LABELS":
        raise ProfileError(f"profile {doc.get('name')}: adverse_table {path.name} is not marked as fitted on market-data labels (fitted_on.source); a table fitted on strategy fills is refused")
    for key in ("horizon_s", "requote_horizon_s"):
        h = at.get(key, 1.0 if key == "horizon_s" else None)
        if h is not None and f"{float(h):g}" not in {f"{x:g}" for x in table.horizons_s}:
            raise ProfileError(f"profile {doc.get('name')}: adverse_table {key} {h} is not in the table's horizons {table.horizons_s}")
    return table, {"file": str(path), "sha256": got, "days": days, "dims": list(table.dims), "shrink_k": table.shrink_k, "events": table.fitted_on.get("events"), "version": table.version}


def load_do_not_quote(doc: dict, dq: dict, run_start: str):
    """S3: the DoNotQuoteFilter a profile pins: a data file whose sha256 the profile records, fitted on TRAIN days that all end before the run starts, from market-data labels only, with the feature names the code computes.
    Refuses a missing file, bytes that no longer match the recorded hash, a file with no days or with a day on or after the run's start date, one not marked as fitted on market-data labels, and a feature list that is not the code's."""
    from crypto_stat_arb.strategies.edge import QUOTE_FEATURE_NAMES, DoNotQuoteFilter
    p = Path(dq["file"])
    path = p if p.is_absolute() else PLATFORM_DIR / p
    if not path.exists():
        raise ProfileError(f"profile {doc.get('name')}: do_not_quote file {path} does not exist")
    got = hashlib.sha256(path.read_bytes()).hexdigest()
    if got != dq.get("sha256"):
        raise ProfileError(f"profile {doc.get('name')}: do_not_quote {path.name} has sha256 {got}, the profile pins {dq.get('sha256')} (file edited after it was pinned)")
    flt = DoNotQuoteFilter.load(path)
    days = (flt.fitted_on or {}).get("days") or []
    if not days or any(str(x) >= str(run_start) for x in days):
        raise ProfileError(f"profile {doc.get('name')}: do_not_quote {path.name} must be fitted on TRAIN days strictly before the run start {run_start}; it lists {days}")
    if (flt.fitted_on or {}).get("source") != "MARKET_DATA_LABELS":
        raise ProfileError(f"profile {doc.get('name')}: do_not_quote {path.name} is not marked as fitted on market-data labels (fitted_on.source); a filter fitted on strategy fills is refused")
    if tuple(flt.names) != tuple(QUOTE_FEATURE_NAMES):
        raise ProfileError(f"profile {doc.get('name')}: do_not_quote {path.name} lists features {flt.names}, the code computes {QUOTE_FEATURE_NAMES}")
    return flt, {"file": str(path), "sha256": got, "days": days, "horizon_s": flt.horizon_s, "threshold_bps": flt.threshold_bps, "events": (flt.fitted_on or {}).get("events"), "version": flt.version}


def load_hedge_move_calibration(doc: dict, hm: dict, run_start: str) -> tuple[float, float, dict]:
    """The expected hedge move (bps, bid and ask) a profile pins: a data file whose sha256 the profile records, estimated on TRAIN days that all end before the run starts.
    Refuses a missing file, a file whose bytes no longer match the recorded hash, and a file with no days or with a day on or after the run's start date."""
    p = Path(hm["file"])
    path = p if p.is_absolute() else PLATFORM_DIR / p
    if not path.exists():
        raise ProfileError(f"profile {doc.get('name')}: hedge_move_calibration file {path} does not exist")
    got = hashlib.sha256(path.read_bytes()).hexdigest()
    if got != hm.get("sha256"):
        raise ProfileError(f"profile {doc.get('name')}: hedge_move_calibration {path.name} has sha256 {got}, the profile pins {hm.get('sha256')} (file edited after it was pinned)")
    d = json.loads(path.read_text())
    days = d.get("days") or []
    if not days or any(str(x) >= str(run_start) for x in days):
        raise ProfileError(f"profile {doc.get('name')}: hedge_move_calibration {path.name} must come from TRAIN days strictly before the run start {run_start}; it lists {days}")
    mv = d["expected_hedge_move_bps"]
    return float(mv["bid"]), float(mv["ask"]), {"file": str(path), "sha256": got, "days": days, "horizon_s": d.get("horizon_s"),
                                                "n": {k: (d.get("stats", {}).get(k) or {}).get("n") for k in ("bid", "ask")},
                                                "method": d.get("method")}


# Latency presets that cannot exist in production (config.py: nobody is co-located with both venues). A non-legacy profile refuses
# them unless the run is declared a sensitivity run: apply_profile(..., latency_sensitivity="<why>"), recorded in release_identity.


def check_latency_preset(doc: dict, latency_preset, timing_preset, sensitivity) -> None:
    problem = latency_preset_problem(doc, latency_preset, timing_preset, sensitivity)       # the rule is the core's
    if problem:
        raise ProfileError(f"profile {doc.get('name')}: {problem}")


def _sha_files(paths) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(str(Path(p).name).encode())
        h.update(Path(p).read_bytes())
    return h.hexdigest()


# Phase A: engine code that moved into the core still belongs to the release identity. The directories are new core packages; the files are single moved modules.
CORE_DIR = ENGINE_DIR.parent / "crypto_stat_arb"
CORE_MOVED_DIRS = ("strategies", "simulation", "ems")
CORE_MOVED_FILES: tuple[str, ...] = ("order_state_machine.py", "circuit_breaker.py", "valuation.py")


def code_hash() -> dict:
    files = [p for p in ENGINE_DIR.glob("*.py")] + [p for p in (ENGINE_DIR / "execution").rglob("*.py")]
    files += [p for d in CORE_MOVED_DIRS if (CORE_DIR / d).is_dir() for p in sorted((CORE_DIR / d).rglob("*.py"))]
    files += [CORE_DIR / f for f in CORE_MOVED_FILES if (CORE_DIR / f).is_file()]
    root = ENGINE_DIR.parents[2]
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", str(ENGINE_DIR)], cwd=root, capture_output=True, text=True, timeout=10).stdout.strip())
    except Exception:  # pragma: no cover
        rev, dirty = "unknown", True
    return {"sha256_sources": _sha_files(files), "n_files": len(files), "git_rev": rev or "unknown", "git_dirty": dirty}


def data_manifest_hash(data_root: str | Path, pair: str, venues, start_date: str, end_date: str) -> dict:
    """Hash of the manifests under data/manifests when present, plus a listing hash (name + size) of the day directories used."""
    root = Path(data_root)
    manifests = sorted((root / "manifests").glob("*.json")) if (root / "manifests").is_dir() else []
    listing = hashlib.sha256()
    n = 0
    from .event_loader import _day_range, venue_root
    for v in venues:
        vr = venue_root(root, v)
        for day in _day_range(start_date, end_date):
            for stream in ("book_snapshots", "book_events", "trades"):
                d = vr / stream / f"pair={pair}" / f"date={day}"
                if d.is_dir():
                    for f in sorted(d.glob("*.jsonl.gz")):
                        listing.update(f"{v}/{stream}/{day}/{f.name}:{f.stat().st_size}".encode())
                        n += 1
    return {"manifests_sha256": _sha_files(manifests) if manifests else None, "manifest_files": [m.name for m in manifests],
            "day_listing_sha256": listing.hexdigest(), "files_listed": n}


def read_calibration(path) -> dict:
    return json.loads(Path(path).read_text())


def resolve_calibration(doc: dict, cal: dict) -> Path:
    """Path of the lifecycle calibration file a profile names, after checking it measured the profile's product.

    A perp profile refuses a file whose `product` is not 'perp' (the spot calibration config/lifecycle_calibration_bybit.json has no
    `product` key and is read as spot), and a spot profile refuses a perp file. The file's content hash must also match its own recorded hash."""
    p = Path(cal["file"])
    path = p if p.is_absolute() else PLATFORM_DIR / p
    if not path.exists():
        raise ProfileError(f"profile {doc.get('name')}: calibration file {path} does not exist")
    c = read_calibration(path)
    got = c.get("product", "spot")
    want = doc.get("product", "perp")
    if got != want:
        raise ProfileError(f"profile {doc.get('name')} (product {want}) refuses calibration {path.name}: it measured product {got!r}")
    if cal.get("venue") and c.get("venue") != cal["venue"]:
        raise ProfileError(f"profile {doc.get('name')}: calibration {path.name} is for venue {c.get('venue')!r}, profile asks for {cal['venue']!r}")
    body = {k: v for k, v in c.items() if k != "meta"}
    if (c.get("meta") or {}).get("content_sha256") != hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest():
        raise ProfileError(f"calibration {path.name}: content hash does not match its recorded content_sha256 (file edited after it was written)")
    return path


def _git_state(repo: Path) -> dict:
    """Phase B freeze: the commit a repository is at and whether tracked files differ from it (untracked files are not counted). Never raises: a repository that cannot be read says so."""
    import subprocess

    def run(*a: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, timeout=20, check=True).stdout.strip()
    try:
        dirty = [ln for ln in run("status", "--porcelain", "--untracked-files=no").splitlines() if ln.strip()]
        return {"repo": str(repo), "branch": run("rev-parse", "--abbrev-ref", "HEAD"), "commit": run("rev-parse", "HEAD"), "dirty": bool(dirty), "tracked_files_changed": len(dirty)}
    except Exception as e:  # pragma: no cover - depends on the checkout
        return {"repo": str(repo), "error": type(e).__name__}


def release_identity(cfg, doc: dict | None = None) -> dict:
    root = ENGINE_DIR.parents[2]
    fee_snapshot = hashlib.sha256(json.dumps({k: (v.maker_bps, v.taker_bps) for k, v in sorted(FEE_PRESETS.items())}).encode()).hexdigest()
    fee_schedule = None
    if getattr(cfg, "fee_source", "presets") == "core":                 # B2 step 4: the rows the run booked, labels included; the presets dict is not what this run used
        from .config import core_fee_specs
        _, rows = core_fee_specs(cfg.pair, cfg.bybit_fee_preset, cfg.binance_fee_preset)
        fee_schedule = {"source": "core crypto_stat_arb.fees.FEE_TIERS", "rows": rows, "rows_sha256": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()}
    cm = root / "data" / "contract_master"
    cm_files = sorted(cm.glob("*")) if cm.is_dir() else []
    calib = PLATFORM_DIR / "config" / "lifecycle_calibration_bybit.json"
    pcal = (doc or {}).get("calibration")
    calib_info = None
    if pcal:
        calib = resolve_calibration(doc, pcal)
        cj = read_calibration(calib)
        calib_info = {"file": str(calib), "file_sha256": hashlib.sha256(calib.read_bytes()).hexdigest(),
                      "content_sha256": (cj.get("meta") or {}).get("content_sha256"), "product": cj.get("product", "spot"),
                      "venue": cj.get("venue"), "schema": cj.get("schema"), "networks_with_data": (cj.get("provenance") or {}).get("networks_with_data")}
    cutoff = None
    if calib.exists():
        try:
            c = json.loads(calib.read_text())
            cutoff = (c.get("meta") or {}).get("generated_utc")      # the calibration post-dates the Jan-2022 replay data
        except Exception:  # pragma: no cover
            cutoff = "unreadable"
    return {"profile": (doc or {}).get("name"), "profile_status": profile_status((doc or {}).get("name")) if doc else None, "profile_sha256": (doc or {}).get("_sha256"), "profile_path": (doc or {}).get("_path"),
            "decision_status": (doc or {}).get("decision_status"),
            "code": code_hash(),
            "data": data_manifest_hash(cfg.data_root, cfg.pair, cfg.venues, cfg.start_date, cfg.end_date),
            "fee_snapshot_sha256": fee_snapshot, "fee_schedule": fee_schedule, "funding_source": getattr(cfg, "funding_source", "settler"), "margin_source": getattr(cfg, "margin_source", "model"), "ledger_source": getattr(cfg, "ledger_source", "backtester"), "runtime": getattr(cfg, "runtime", "backtester"), **({"resync_delay_s": cfg.resync_delay_s, "resync_delay_label": "ASSUMED"} if getattr(cfg, "runtime", "backtester") == "session" else {}), "funding_modelled": bool(getattr(cfg, "funding_rate_dir", None)), "fee_presets": {"bybit": cfg.bybit_fee_preset, "binance": cfg.binance_fee_preset},
            "instrument_master_sha256": _sha_files(cm_files) if cm_files else None,
            "calibration_file": str(calib) if calib.exists() else None, "calibration_cutoff": cutoff, "calibration": calib_info,
            "git": {"outer": _git_state(root), "core": _git_state(CORE_DIR)},
            "seed": cfg.seed, "latency_preset": cfg.latency_preset,
            "execution": ({"config": cfg.execution.as_dict(), "config_sha256": cfg.execution.sha256()} if getattr(cfg, "execution", None) is not None else None)}


def switch_table(doc: dict) -> list[dict]:
    """Every switch with its classification and the value this profile sets (None = engine default)."""
    rows = []
    for sw in SWITCHES:
        rows.append({"switch": sw.key, "class": sw.cls, "value": _get(doc, sw.key, doc.get(sw.key.split(".")[0]) if "." not in sw.key else None),
                     "note": sw.note})
    return rows
