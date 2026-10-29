from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from .instruments import InstrumentMaster
from .models import Instrument, d
from .risk import AccountRiskSnapshot, InventoryModel, InventoryPolicy, RiskLimits
from .time_integrity import TimePolicy
from .quality import QualityThresholds
from .execution import OrderTimeoutPolicy, ResidualPolicy, UnwindUrgency
from .fees import FeeSchedule, parse_effective_ns
from .orderbook import BookSanityPolicy
from .monitoring import MonitoringThresholds
from .config_schema import validate_config_keys
from .credentials import reject_plaintext_credentials


@dataclass(frozen=True, slots=True)
class AppConfig:
    strategy_id: str
    instrument_id: str
    venues: tuple[str, str]
    quantity: Decimal
    entry_bps: Decimal
    min_net_edge_bps: Decimal
    max_book_age_ns: int
    initial_capital_by_venue: dict[str, Decimal]
    risk_limits: RiskLimits
    time_policy: TimePolicy
    quality_thresholds: QualityThresholds
    residual_policy: ResidualPolicy
    account_risk_snapshots: dict[str, AccountRiskSnapshot]
    monitoring_thresholds: MonitoringThresholds
    instruments: InstrumentMaster
    latency: dict[str, Any] | None = None
    order_timeout_policy: OrderTimeoutPolicy = OrderTimeoutPolicy()
    book_sanity: BookSanityPolicy = BookSanityPolicy()
    drain_horizon_ns: int = 10_000_000_000
    performance: dict[str, Any] = field(default_factory=dict)
    execution_mode: str = "INTERNAL_SIMULATION"
    # oms.rate_limits: scoped venue rate limits (EMS02): enabled, cancel_reserve,
    # risk_reducing_reserve. Empty = defaults (enabled, 0.2, 0.1).
    rate_limits: dict[str, Any] = field(default_factory=dict)
    strategies: dict[str, Any] = field(default_factory=dict)
    inventory_policy: InventoryPolicy = field(default_factory=InventoryPolicy)
    # ``funding``: perpetual funding settlement (ECO02, see funding.py).
    funding: dict[str, Any] = field(default_factory=dict)
    # ``risk.borrow``: spot borrow lines and recalls (ECO03, see borrow.py).
    borrow: dict[str, Any] = field(default_factory=dict)
    # ``risk.margin``: risk tiers, collateral, cross/isolated, liquidation (ECO04, margin.py).
    margin: Any = None
    # ``risk.transfers``: inter-venue transfer routes and rebalancing (ECO05, transfers.py).
    transfers: dict[str, Any] = field(default_factory=dict)


def _optional_decimal(value: Any, default: str | None) -> Decimal | None:
    if value is None and default is None:
        return None
    return d(default if value is None else value)


def _inventory_policy(raw: dict) -> InventoryPolicy:
    """``risk.inventory``: ``{"model": "pre_funded", "initial_base_by_venue":
    {"BINANCE": "0.05", ...}}``. Quote balances default to
    ``risk.initial_capital_by_venue``. Missing section: UNCONSTRAINED."""
    risk = raw.get("risk", {})
    section = risk.get("inventory") or {}
    model = InventoryModel(str(section.get("model", "unconstrained")).upper())
    quote = section.get("initial_quote_by_venue", risk.get("initial_capital_by_venue", {}))
    policy = InventoryPolicy(
        model=model,
        instrument_id=str(raw["strategy"]["instrument_id"]),
        initial_base_by_venue={str(v): d(q) for v, q in (section.get("initial_base_by_venue") or {}).items()},
        initial_quote_by_venue={str(v): d(q) for v, q in quote.items()},
    )
    if model == InventoryModel.PRE_FUNDED:
        venues = set(raw["strategy"]["venues"])
        missing = venues - set(policy.initial_base_by_venue)
        if missing:
            raise ValueError(f"pre_funded inventory needs initial_base_by_venue for {sorted(missing)}")
        missing = venues - set(policy.initial_quote_by_venue)
        if missing:
            raise ValueError(f"pre_funded inventory needs quote balances for {sorted(missing)}")
    return policy


def _perpetual_fields(x: dict) -> dict[str, Any]:
    """Contract terms of a perpetual; unsupported terms fail closed at load."""
    from .funding import is_perpetual

    product_type = str(x.get("product_type", "SPOT")).upper()
    if not is_perpetual(product_type):
        return {}
    contract_type = str(x.get("contract_type", "LINEAR")).upper()
    if contract_type not in ("LINEAR", "INVERSE"):
        raise ValueError(f"{x['instrument_id']}: contract_type must be LINEAR or INVERSE")
    if contract_type == "INVERSE":
        raise ValueError(
            f"{x['instrument_id']}: inverse perpetuals are not supported yet "
            "(fill and position accounting is linear only)"
        )
    multiplier = d(x.get("contract_multiplier", "1"))
    if multiplier != 1:
        raise ValueError(
            f"{x['instrument_id']}: contract_multiplier {multiplier} is not supported yet "
            "(the trade ledger books quantity x price without a multiplier)"
        )
    interval_minutes = x.get("funding_interval_minutes")
    if interval_minutes is None and x.get("funding_interval_hours") is not None:
        interval_minutes = d(x["funding_interval_hours"]) * 60
    if interval_minutes is None or d(interval_minutes) <= 0:
        # Without a schedule a missing rate record leaves no trace, so missing
        # funding data could not be detected.
        raise ValueError(
            f"{x['instrument_id']}: a perpetual needs funding_interval_hours or "
            "funding_interval_minutes"
        )
    return {
        "contract_multiplier": multiplier,
        "inverse": False,
        "settlement_asset": str(x.get("settlement_asset") or x["quote"]),
        "funding_interval_ns": int(d(interval_minutes or 0) * 60 * 1_000_000_000),
        "funding_anchor_ns": parse_effective_ns(x.get("funding_anchor")),
        "funding_rate_cap": d(x["funding_rate_cap"]) if x.get("funding_rate_cap") not in (None, "") else None,
        "funding_rate_floor": d(x["funding_rate_floor"]) if x.get("funding_rate_floor") not in (None, "") else None,
    }


def load_config(path: Path) -> tuple[AppConfig, dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    reject_plaintext_credentials(raw)
    validate_config_keys(raw)
    _validate_strategy_venues(raw)
    instruments = [
        Instrument(
            instrument_id=x["instrument_id"],
            venue=x["venue"],
            symbol=x["symbol"],
            base=x["base"],
            quote=x["quote"],
            tick_size=d(x["tick_size"]),
            quantity_step=d(x["quantity_step"]),
            min_quantity=d(x["min_quantity"]),
            min_notional=d(x["min_notional"]),
            taker_fee_bps=d(x["taker_fee_bps"]),
            status=x.get("status", "TRADING"),
            product_type=str(x.get("product_type", "SPOT")).upper(),
            leverage=d(x.get("leverage", "1")),
            maker_fee_bps=(
                d(x["maker_fee_bps"]) if x.get("maker_fee_bps") is not None else None
            ),
            effective_from_ns=parse_effective_ns(x.get("effective_from")),
            borrow_applicable=bool(x.get("borrow_applicable", False)),
            **_perpetual_fields(x),
        )
        for x in raw["instruments"]
    ]
    from .cross_venue import check_instrument_compatibility

    check_instrument_compatibility(instruments, raw["strategy"]["instrument_id"], raw["strategy"]["venues"])
    from .capabilities import capabilities

    for x in raw["instruments"]:
        if x["instrument_id"] == raw["strategy"]["instrument_id"] and x["venue"] in raw["strategy"]["venues"]:
            capabilities(x["venue"], str(x.get("product_type", "SPOT")))   # fails closed if unknown
    inventory = _inventory_policy(raw)
    if inventory.model == InventoryModel.PRE_FUNDED and any(
        i.settlement_asset and i.instrument_id == raw["strategy"]["instrument_id"]
        for i in instruments
    ):
        raise ValueError(
            "pre_funded spot inventory does not apply to a perpetual instrument "
            "(margin accounting is not modelled yet)"
        )
    instrument_master = InstrumentMaster(
        instruments, FeeSchedule.from_config(raw.get("fees", []))
    )
    for row in raw.get("instrument_status_events", []):
        instrument_master.apply_status(
            row["venue"], row["instrument_id"],
            effective_from_ns=parse_effective_ns(row["effective_from"]),
            source=str(row.get("source", "config")),
            status=row.get("status"),
            **{
                key: d(row[key])
                for key in ("tick_size", "quantity_step", "min_quantity", "min_notional")
                if row.get(key) is not None
            },
        )
    timeout_config = raw.get("oms", {}).get("timeouts", {})
    sanity_config = raw.get("book_sanity", {})
    risk = raw["risk"]
    time_config = raw.get("time", {})
    quality_config = raw.get("quality", {})
    residual_config = raw.get("oms", {}).get("residual", {})
    monitoring_config = raw.get("monitoring", {})
    cfg = AppConfig(
        strategy_id=raw["strategy"]["strategy_id"],
        instrument_id=raw["strategy"]["instrument_id"],
        venues=tuple(raw["strategy"]["venues"]),  # type: ignore[arg-type]
        quantity=d(raw["strategy"]["quantity"]),
        entry_bps=d(raw["strategy"]["entry_bps"]),
        min_net_edge_bps=d(raw["strategy"]["min_net_edge_bps"]),
        max_book_age_ns=int(raw["strategy"]["max_book_age_ms"]) * 1_000_000,
        initial_capital_by_venue={
            str(venue): d(amount)
            for venue, amount in risk.get("initial_capital_by_venue", {}).items()
        },
        inventory_policy=inventory,
        risk_limits=RiskLimits(
            max_order_notional=d(risk["max_order_notional"]),
            max_gross_notional=d(risk["max_gross_notional"]),
            max_residual_base=d(risk["max_residual_base"]),
            max_book_age_ns=int(raw["strategy"]["max_book_age_ms"]) * 1_000_000,
            max_position_base=d(risk.get("max_position_base", "Infinity")),
            # Defaults to the per-venue position limit, the most a single
            # failed leg could ever leave on one venue.
            max_legging_exposure_base=d(risk.get(
                "max_legging_exposure_base", risk.get("max_position_base", "Infinity")
            )),
            max_cash_reserved=d(risk.get("max_cash_reserved", "Infinity")),
            max_margin_reserved=d(risk.get("max_margin_reserved", "Infinity")),
            max_message_capacity=int(
                risk.get("max_message_capacity", 1_000_000_000)
            ),
            max_price_deviation_bps=d(
                risk.get("max_price_deviation_bps", "Infinity")
            ),
            max_strategy_package_notional=d(
                risk.get("max_strategy_package_notional", "Infinity")
            ),
            max_strategy_gross_notional=d(
                risk.get("max_strategy_gross_notional", "Infinity")
            ),
            max_strategy_net_notional=d(
                risk.get("max_strategy_net_notional", "Infinity")
            ),
            max_instrument_gross_notional=d(
                risk.get("max_instrument_gross_notional", "Infinity")
            ),
            max_venue_gross_notional=d(
                risk.get("max_venue_gross_notional", "Infinity")
            ),
            strategy_gross_limits={
                str(key): d(value)
                for key, value in risk.get("strategy_gross_limits", {}).items()
            },
            strategy_net_limits={
                str(key): d(value)
                for key, value in risk.get("strategy_net_limits", {}).items()
            },
            instrument_gross_limits={
                str(key): d(value)
                for key, value in risk.get("instrument_gross_limits", {}).items()
            },
            venue_gross_limits={
                str(key): d(value)
                for key, value in risk.get("venue_gross_limits", {}).items()
            },
        ),
        time_policy=TimePolicy(
            max_event_latency_ns=int(time_config.get("max_event_latency_ms", 60_000)) * 1_000_000,
            max_feed_age_ns=int(time_config.get("max_feed_age_ms", raw["strategy"]["max_book_age_ms"])) * 1_000_000,
            max_cross_venue_skew_ns=int(time_config.get("max_cross_venue_skew_ms", raw["strategy"]["max_book_age_ms"])) * 1_000_000,
            max_host_offset_ns=int(time_config.get("max_host_offset_ms", 100)) * 1_000_000,
            max_host_drift_ppm=float(time_config.get("max_host_drift_ppm", 500.0)),
            negative_latency_tolerance_ns=int(
                float(time_config.get("negative_latency_tolerance_ms", 0)) * 1_000_000
            ),
            correct_host_offset=bool(time_config.get("correct_host_offset", False)),
            host_offset_quantile=float(time_config.get("host_offset_quantile", 0.01)),
            host_offset_window=int(time_config.get("host_offset_window", 2_000)),
            host_offset_min_samples=int(time_config.get("host_offset_min_samples", 20)),
            max_feed_delay_ns=(
                None if time_config.get("max_feed_delay_ms") is None
                else int(float(time_config["max_feed_delay_ms"]) * 1_000_000)
            ),
        ),
        quality_thresholds=QualityThresholds(
            min_events=int(quality_config.get("min_events", 1)),
            max_gaps=int(quality_config.get("max_gaps", 0)),
            max_stale_intervals=int(quality_config.get("max_stale_intervals", 0)),
            max_crossed_books=int(quality_config.get("max_crossed_books", 0)),
            max_duplicates=int(quality_config.get("max_duplicates", 0)),
            max_out_of_order=int(quality_config.get("max_out_of_order", 0)),
            max_schema_exceptions=int(quality_config.get("max_schema_exceptions", 0)),
            max_rejected_partitions=int(quality_config.get("max_rejected_partitions", 0)),
            max_negative_latency=int(quality_config.get("max_negative_latency", 0)),
            max_impossible_latency=int(quality_config.get("max_impossible_latency", 0)),
            max_unknown_instrument_rules=int(quality_config.get("max_unknown_instrument_rules", 0)),
            max_persistence_lag_ns=int(quality_config.get("max_persistence_lag_ms", 5000)) * 1_000_000,
            max_unpersisted_bytes=int(quality_config.get("max_unpersisted_bytes", 0)),
            max_price_jumps=int(quality_config.get("max_price_jumps", 0)),
            max_wide_spreads=int(quality_config.get("max_wide_spreads", 0)),
            max_invalid_levels=int(quality_config.get("max_invalid_levels", 0)),
            max_invalid_trades=int(quality_config.get("max_invalid_trades", 0)),
            max_silent_feeds=int(quality_config.get("max_silent_feeds", 0)),
        ),
        residual_policy=ResidualPolicy(
            max_residual_base=d(
                residual_config.get("max_residual_base", risk["max_residual_base"])
            ),
            max_residual_age_ns=int(
                residual_config.get("max_residual_age_ms", 5_000)
            ) * 1_000_000,
            hedge_venue_priority=tuple(
                residual_config.get("hedge_venue_priority", raw["strategy"]["venues"])
            ),
            unwind_urgency=UnwindUrgency(
                residual_config.get("unwind_urgency", "NORMAL")
            ),
            unwind_price_slippage_bps=d(
                residual_config.get("unwind_price_slippage_bps", "10")
            ),
            allow_risk_reducing_under_stop=bool(
                residual_config.get("allow_risk_reducing_under_stop", True)
            ),
            max_unwind_attempts=int(residual_config.get("max_unwind_attempts", 2)),
            max_one_sided_notional=d(
                residual_config.get("max_one_sided_notional", "Infinity")
            ),
            hold_new_risk_on_breach=bool(
                residual_config.get("hold_new_risk_on_breach", True)
            ),
            kill_after_ns=(
                int(residual_config["kill_after_ms"]) * 1_000_000
                if residual_config.get("kill_after_ms") is not None else None
            ),
        ),
        account_risk_snapshots={
            str(venue): AccountRiskSnapshot(
                venue=str(venue),
                available_cash=d(row.get("available_cash", "Infinity")),
                available_margin=d(row.get("available_margin", "Infinity")),
                margin_enabled=bool(row.get("margin_enabled", False)),
                collateral_balance=d(row.get("collateral_balance", "Infinity")),
                initial_margin_rate=d(row.get("initial_margin_rate", "0")),
                maintenance_margin_rate=d(
                    row.get("maintenance_margin_rate", "0")
                ),
                liquidation_buffer_rate=d(
                    row.get("liquidation_buffer_rate", "0.25")
                ),
            )
            for venue, row in risk.get("margin_accounts", {}).items()
        },
        monitoring_thresholds=MonitoringThresholds(
            max_data_age_ns=int(monitoring_config.get(
                "max_data_age_ms", raw["strategy"]["max_book_age_ms"]
            )) * 1_000_000,
            min_partition_completeness=float(monitoring_config.get(
                "min_partition_completeness", 1.0
            )),
            max_gaps=int(monitoring_config.get("max_gaps", 0)),
            max_invalid_books=int(monitoring_config.get("max_invalid_books", 0)),
            max_schema_mismatches=int(monitoring_config.get(
                "max_schema_mismatches", 0
            )),
            min_replay_events_per_second=float(monitoring_config.get(
                "min_replay_events_per_second", 1.0
            )),
            max_reconnects=int(monitoring_config.get("max_reconnects", 5)),
            max_stale_disconnects=int(monitoring_config.get(
                "max_stale_disconnects", 0
            )),
            max_persistence_lag_ns=int(monitoring_config.get(
                "max_persistence_lag_ms", 5000
            )) * 1_000_000,
            max_order_rejects=int(monitoring_config.get("max_order_rejects", 0)),
            max_open_residual_base=float(monitoring_config.get(
                "max_open_residual_base", risk["max_residual_base"]
            )),
            max_dispatch_latency_ns=int(monitoring_config.get(
                "max_dispatch_latency_ms", 10
            )) * 1_000_000,
            max_rate_limit_utilization=float(monitoring_config.get(
                "max_rate_limit_utilization", 0.90
            )),
            max_unhealthy_connectors=int(monitoring_config.get(
                "max_unhealthy_connectors", 0
            )),
            max_active_kill_switches=int(monitoring_config.get(
                "max_active_kill_switches", 0
            )),
        ),
        instruments=instrument_master,
        latency=raw.get("latency"),
        rate_limits=dict(raw.get("oms", {}).get("rate_limits", {})),
        order_timeout_policy=OrderTimeoutPolicy(
            report_timeout_ns=int(timeout_config.get("report_timeout_ms", 2_000)) * 1_000_000,
            query_interval_ns=int(timeout_config.get("query_interval_ms", 500)) * 1_000_000,
            max_queries=int(timeout_config.get("max_queries", 3)),
            kill_on_unresolved=bool(timeout_config.get("kill_on_unresolved", True)),
        ),
        book_sanity=BookSanityPolicy(
            max_mid_jump_bps=_optional_decimal(
                sanity_config.get("max_mid_jump_bps", "500"), None
            ),
            max_spread_bps=_optional_decimal(
                sanity_config.get("max_spread_bps", "500"), None
            ),
            jump_confirmation_updates=int(
                sanity_config.get("jump_confirmation_updates", 3)
            ),
        ),
        drain_horizon_ns=int(
            raw.get("backtest", {}).get("drain_horizon_ms", 10_000)
        ) * 1_000_000,
        performance=dict(raw.get("performance", {})),
        execution_mode=str(raw.get("execution_mode", "INTERNAL_SIMULATION")).upper(),
        strategies=dict(raw.get("strategies", {})),
        funding=dict(raw.get("funding", {})),
        borrow=_borrow_section(raw),
        margin=_margin_section(raw, instruments),
        transfers=_transfer_section(raw),
    )
    return cfg, raw


def _transfer_section(raw: dict) -> dict[str, Any]:
    """Validate ``risk.transfers`` at load: positive delays, known venues, one route per leg."""
    from .transfers import TransferRoute

    section = dict(raw.get("risk", {}).get("transfers") or {})
    routes = [TransferRoute.from_config(row) for row in section.get("routes", [])]
    venues = set(raw["strategy"]["venues"])
    for route in routes:
        if {route.source, route.destination} - venues:
            raise ValueError(f"transfer route {route.source}->{route.destination} names a venue outside the strategy")
    keys = [route.key for route in routes]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate transfer route")
    return section


def _validate_strategy_venues(raw: dict) -> None:
    """The cross-venue strategy reads ``venues[0]`` and ``venues[1]``: require two distinct venues."""
    venues = list(raw["strategy"]["venues"])
    if len(set(venues)) < 2:
        raise ValueError(
            f"strategy.venues must list at least two distinct venues for the "
            f"cross-venue strategy, got {venues}"
        )


def _margin_section(raw: dict, instruments: list[Instrument]):
    from .margin import MarginConfig

    config = MarginConfig.from_config(raw.get("risk", {}).get("margin"))
    known = {(i.venue, i.instrument_id) for i in instruments}
    for venue, row in config.venues.items():
        if venue not in raw["strategy"]["venues"]:
            raise ValueError(f"margin config for {venue}, which is not a strategy venue")
        for instrument_id in row.tiers:
            if (venue, instrument_id) not in known:
                raise ValueError(f"margin tiers for unknown instrument {venue}:{instrument_id}")
    return config


def _borrow_section(raw: dict) -> dict[str, Any]:
    """Validate ``risk.borrow`` at load so bad terms fail before a run starts."""
    from .borrow import BorrowLine, BorrowRecall

    section = dict(raw.get("risk", {}).get("borrow") or {})
    lines = [BorrowLine.from_config(row) for row in section.get("lines", [])]
    keys = [(line.venue, line.asset) for line in lines]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate borrow line for a venue/asset")
    for row in section.get("recalls", []):
        recall = BorrowRecall.from_config(row)
        if (recall.venue, recall.asset) not in keys:
            raise ValueError(f"recall {recall.recall_id} has no borrow line")
    return section
