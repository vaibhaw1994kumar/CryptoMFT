"""Config key validation (requirement SDK02): an unknown key is an error, not a silent default.

``load_config`` checks the raw document against ``SCHEMA`` before building anything,
so a misspelt key (``entry_bpz``) or a section the platform does not read fails at load
with its full path and the nearest known key, instead of quietly running on a default.

Schema forms
    ``ANY``            the value is not checked here (a leaf, or data with its own validator)
    ``{key: schema}``  an object whose keys must all be listed
    ``Map(schema)``    an object keyed by data (venue, instrument or strategy ids)
    ``ListOf(schema)`` a list whose items match ``schema``
    ``Either(a, b)``   an object (``a``) or a list (``b``), as some sections accept both

Keys whose name starts with ``_`` are comments and are allowed anywhere. The schema lists
exactly what the loader and the subsystems it configures read; a new config key needs an
entry here (the error says so).
"""

from __future__ import annotations

from dataclasses import dataclass
import difflib
from typing import Any


ANY = object()


@dataclass(frozen=True)
class Map:
    values: Any


@dataclass(frozen=True)
class ListOf:
    item: Any


@dataclass(frozen=True)
class Either:
    as_object: Any
    as_list: Any


_DISTRIBUTION = {f"{name}_{unit}": ANY for name in ("base", "jitter", "tail", "std", "min", "max")
                 for unit in ("ms", "ns")} | {"tail_probability": ANY, "model": ANY}
_LATENCY_STAGES = ("market_data", "strategy", "risk", "entry", "submit_ack", "fill", "cancel")
_MARGIN_TIER = {"max_notional": ANY, "maintenance_rate": ANY, "initial_rate": ANY,
                "maintenance_deduction": ANY, "initial_deduction": ANY}

SCHEMA: dict[str, Any] = {
    "strategy": {"strategy_id": ANY, "instrument_id": ANY, "venues": ANY, "quantity": ANY,
                 "entry_bps": ANY, "min_net_edge_bps": ANY, "max_book_age_ms": ANY},
    "execution_mode": ANY,
    "instruments": ListOf({
        "instrument_id": ANY, "venue": ANY, "symbol": ANY, "base": ANY, "quote": ANY, "tick_size": ANY,
        "quantity_step": ANY, "min_quantity": ANY, "min_notional": ANY, "taker_fee_bps": ANY,
        "maker_fee_bps": ANY, "status": ANY, "product_type": ANY, "leverage": ANY, "effective_from": ANY,
        "borrow_applicable": ANY, "contract_type": ANY, "contract_multiplier": ANY,
        "settlement_asset": ANY, "funding_interval_minutes": ANY, "funding_interval_hours": ANY,
        "funding_anchor": ANY, "funding_rate_cap": ANY, "funding_rate_floor": ANY,
    }),
    "fees": ListOf({"venue": ANY, "instrument_id": ANY, "effective_from": ANY, "tier": ANY,
                    "maker_fee_bps": ANY, "taker_fee_bps": ANY, "fee_asset": ANY, "fee_source": ANY,
                    "conservative_unverified": ANY}),
    "instrument_status_events": ListOf({"venue": ANY, "instrument_id": ANY, "effective_from": ANY,
                                        "source": ANY, "status": ANY, "tick_size": ANY,
                                        "quantity_step": ANY, "min_quantity": ANY, "min_notional": ANY}),
    "risk": {
        "max_order_notional": ANY, "max_gross_notional": ANY, "max_position_base": ANY,
        "max_residual_base": ANY, "max_price_deviation_bps": ANY, "max_cash_reserved": ANY,
        "max_margin_reserved": ANY, "max_message_capacity": ANY, "max_instrument_gross_notional": ANY,
        "max_strategy_gross_notional": ANY, "max_strategy_net_notional": ANY,
        "max_strategy_package_notional": ANY, "max_venue_gross_notional": ANY,
        "initial_capital_by_venue": Map(ANY), "instrument_gross_limits": Map(ANY),
        "strategy_gross_limits": Map(ANY), "strategy_net_limits": Map(ANY), "venue_gross_limits": Map(ANY),
        "margin_accounts": Map({"available_cash": ANY, "available_margin": ANY, "margin_enabled": ANY,
                                "collateral_balance": ANY, "initial_margin_rate": ANY,
                                "maintenance_margin_rate": ANY, "liquidation_buffer_rate": ANY}),
        "inventory": {"model": ANY, "initial_base_by_venue": Map(ANY), "initial_quote_by_venue": Map(ANY)},
        "margin": {
            "evaluate_interval_ms": ANY, "flatten_after_liquidation": ANY,
            "venues": Map({
                "mode": ANY, "reported_tolerance": ANY, "isolated_margin": Map(ANY),
                "collateral": ListOf({"asset": ANY, "haircut": ANY, "eligible": ANY}),
                "liquidation": {"enabled": ANY, "fee_rate": ANY, "source": ANY},
                "tiers": Map(Either({"source": ANY, "tiers": ListOf(_MARGIN_TIER)}, ListOf(_MARGIN_TIER))),
            }),
        },
        "borrow": {
            "settle_lag_ms": ANY, "forced_close_fee_bps": ANY, "repay_policy": ANY,
            "lines": ListOf({"venue": ANY, "asset": ANY, "authorized": ANY, "limit": ANY, "interval_hours": ANY,
                             "rate_per_interval": ANY, "daily_rate": ANY, "basis": ANY, "anchor": ANY,
                             "charge_on_open": ANY, "source": ANY,
                             "rate_changes": ListOf({"effective_from": ANY, "rate_per_interval": ANY,
                                                     "daily_rate": ANY}),
                             "limit_changes": ListOf({"effective_from": ANY, "limit": ANY})}),
            "recalls": ListOf({"recall_id": ANY, "venue": ANY, "asset": ANY, "effective_from": ANY,
                               "deadline": ANY, "deadline_ms": ANY, "quantity": ANY,
                               "forced_close_price": ANY, "source": ANY}),
        },
        "transfers": {
            "routes": ListOf({"from": ANY, "to": ANY, "asset": ANY, "delay_ms": ANY, "fee": ANY,
                              "min_quantity": ANY, "fee_refunded_on_failure": ANY, "outcomes": ANY,
                              "source": ANY}),
            "rebalance": {"enabled": ANY, "cooldown_ms": ANY,
                          "assets": Map({"low_watermark": ANY, "target": ANY})},
        },
    },
    "oms": {
        "timeouts": {"report_timeout_ms": ANY, "query_interval_ms": ANY, "max_queries": ANY,
                     "kill_on_unresolved": ANY},
        "rate_limits": {"enabled": ANY, "cancel_reserve": ANY, "risk_reducing_reserve": ANY},
        "residual": {"max_residual_base": ANY, "max_residual_age_ms": ANY, "max_one_sided_notional": ANY,
                     "hedge_venue_priority": ANY, "hold_new_risk_on_breach": ANY,
                     "allow_risk_reducing_under_stop": ANY, "max_unwind_attempts": ANY,
                     "unwind_price_slippage_bps": ANY, "unwind_urgency": ANY, "kill_after_ms": ANY},
    },
    "book_sanity": {"max_mid_jump_bps": ANY, "max_spread_bps": ANY, "jump_confirmation_updates": ANY},
    "time": {"max_event_latency_ms": ANY, "max_feed_age_ms": ANY, "max_cross_venue_skew_ms": ANY,
             "max_host_offset_ms": ANY, "max_host_drift_ppm": ANY, "negative_latency_tolerance_ms": ANY,
             "correct_host_offset": ANY, "host_offset_quantile": ANY, "host_offset_window": ANY,
             "host_offset_min_samples": ANY, "max_feed_delay_ms": ANY},
    "quality": {key: ANY for key in (
        "min_events", "max_gaps", "max_stale_intervals", "max_crossed_books", "max_duplicates",
        "max_out_of_order", "max_schema_exceptions", "max_rejected_partitions", "max_negative_latency",
        "max_impossible_latency", "max_unknown_instrument_rules", "max_persistence_lag_ms",
        "max_unpersisted_bytes", "max_price_jumps", "max_wide_spreads", "max_invalid_levels",
        "max_invalid_trades", "max_silent_feeds")},
    "monitoring": {key: ANY for key in (
        "max_data_age_ms", "min_partition_completeness", "max_gaps", "max_invalid_books",
        "max_schema_mismatches", "min_replay_events_per_second", "max_reconnects", "max_stale_disconnects",
        "max_persistence_lag_ms", "max_order_rejects", "max_open_residual_base", "max_dispatch_latency_ms",
        "max_rate_limit_utilization", "max_unhealthy_connectors", "max_active_kill_switches")},
    "latency": {"enabled": ANY, "seed": ANY, "preset": ANY,
                "profiles": Map({stage: _DISTRIBUTION for stage in _LATENCY_STAGES} | {"venue": ANY})},
    "backtest": {"drain_horizon_ms": ANY},
    "performance": {"max_feed_to_strategy_p99_ms": ANY, "max_paper_frame_overhead_p99_ms": ANY,
                    "max_route_decision_p99_ms": ANY, "min_replay_events_per_second": ANY},
    "strategies": {
        "cross_venue_arb": {"enabled": ANY},
        "maker_simulation": {"enabled": ANY, "venues": ANY, "quote_size": ANY, "quote_interval_ms": ANY,
                             "max_inventory": ANY, "seed": ANY, "depth": ANY, "engine": ANY,
                             "feed_lag_ms": ANY, "queue_position_frac": ANY, "replay_tie_priority": ANY},
        "mean_reversion": {"enabled": ANY, "strategy_id": ANY, "quantity": ANY, "window": ANY,
                           "min_samples": ANY, "sample_interval_ms": ANY, "entry_z": ANY, "exit_z": ANY,
                           "stop_z": ANY, "min_deviation_bps": ANY, "max_holding_ms": ANY, "cooldown_ms": ANY},
    },
    "funding": {"rates": ANY, "rates_path": ANY, "mark_fallback": ANY, "max_abs_rate": ANY,
                "max_mark_age_ms": ANY, "max_rate_age_ms": ANY, "settle_lag_ms": ANY,
                "schedule_tolerance_ms": ANY},
    # Free text for the people running the watcher; read by nothing.
    "watch_notes": ANY,
}


def unknown_keys(document: Any, schema: Any = None, path: str = "") -> list[str]:
    """Every key in ``document`` the schema does not know, with a suggestion where one is close."""
    schema = SCHEMA if schema is None else schema
    if schema is ANY:
        return []
    if isinstance(schema, Either):
        return unknown_keys(document, schema.as_list if isinstance(document, list) else schema.as_object, path)
    if isinstance(schema, ListOf):
        if not isinstance(document, list):
            return []                                   # type errors are the loader's job
        return [problem for index, item in enumerate(document)
                for problem in unknown_keys(item, schema.item, f"{path}[{index}]")]
    if not isinstance(document, dict):
        return []
    problems = []
    for key, value in document.items():
        child = f"{path}.{key}" if path else str(key)
        if isinstance(schema, Map):
            problems += unknown_keys(value, schema.values, child)
        elif str(key).startswith("_"):
            continue                                    # a comment
        elif key not in schema:
            near = difflib.get_close_matches(str(key), list(schema), n=1)
            problems.append(child + (f" (did you mean '{near[0]}'?)" if near else ""))
        else:
            problems += unknown_keys(value, schema[key], child)
    return problems


def validate_config_keys(document: dict) -> None:
    problems = unknown_keys(document)
    if problems:
        raise ValueError(
            "unknown config key(s): " + "; ".join(problems)
            + " (a key the platform does not read would silently fall back to a default; "
              "fix the name, or add it to crypto_stat_arb.config_schema.SCHEMA if it is new)"
        )
