from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BinanceConfig(BaseModel):
    rest_base_url: str = "https://api.binance.com"
    websocket_base_url: str = "wss://stream.binance.com:9443"
    depth_levels: int = Field(default=1000, ge=5, le=5000)
    stale_after_ms: int = Field(default=2000, gt=0)


class DataQualityConfig(BaseModel):
    max_market_data_delay_ms: int = Field(default=2000, gt=0)
    max_future_skew_ms: int = Field(default=1000, ge=0)
    max_timestamp_regression_ms: int = Field(default=0, ge=0)
    max_trade_timestamp_difference_ms: int = Field(
        default=1000, ge=0
    )
    min_price: Decimal = Field(default=Decimal("0.00000001"), gt=0)
    max_price: Decimal = Field(default=Decimal("100000000"), gt=0)
    min_trade_quantity: Decimal = Field(
        default=Decimal("0.00000001"), gt=0
    )
    max_trade_quantity: Decimal = Field(default=Decimal("1000"), gt=0)
    max_price_move_bps: Decimal = Field(default=Decimal("1000"), gt=0)
    max_spread_bps: Decimal = Field(default=Decimal("500"), gt=0)
    min_book_depth: int = Field(default=1, gt=0)
    max_processing_latency_ms: int = Field(default=50, gt=0)
    max_heartbeat_interval_ms: int = Field(default=5000, gt=0)
    max_retained_anomalies: int = Field(default=10_000, gt=0)
    max_retained_book_checksums: int = Field(default=10_000, gt=0)
    reject_stale_market_data: bool = True
    reject_abnormal_price_moves: bool = False
    reject_wide_spreads: bool = True
    reject_crossed_books: bool = True
    reject_processing_latency: bool = False
    # P0.4 cross-venue divergence
    max_cross_venue_divergence_bps: Decimal = Field(default=Decimal("200"), gt=0)
    min_venues_for_cross_venue_check: int = Field(default=2, gt=0)
    reject_cross_venue_divergence: bool = False
    # P0.5 OMS reconciliation
    position_reconciliation_tolerance: Decimal = Field(default=Decimal("0.001"), ge=0)
    reject_position_mismatch: bool = True

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.max_price <= self.min_price:
            raise ValueError("max_price must be greater than min_price")
        if self.max_trade_quantity <= self.min_trade_quantity:
            raise ValueError(
                "max_trade_quantity must be greater than min_trade_quantity"
            )
        return self

class RiskConfig(BaseModel):
    max_order_notional: Decimal = Field(default=Decimal("1000"), gt=0)
    max_net_position: Decimal = Field(default=Decimal("0.05"), gt=0)
    max_open_orders: int = Field(default=10, gt=0)
    max_daily_loss: Decimal = Field(default=Decimal("100"), gt=0)
    price_collar_bps: int = Field(default=100, gt=0)


class StrategyConfig(BaseModel):
    name: str = "spread_capture"
    spread_threshold_bps: Decimal = Field(default=Decimal("0.001"), gt=0)
    order_notional: Decimal = Field(default=Decimal("25"), gt=0)
    quote_cooldown_ms: int = Field(default=1000, ge=0)


class PaperConfig(BaseModel):
    maker_fee_bps: Decimal = Field(default=Decimal("1"), ge=0)
    taker_fee_bps: Decimal = Field(default=Decimal("2"), ge=0)
    acknowledgement_latency_ms: int = Field(default=5, ge=0)
    cancellation_latency_ms: int = Field(default=5, ge=0)


class StorageConfig(BaseModel):
    data_directory: Path = Path("data")
    report_directory: Path = Path("reports")


class OperationsConfig(BaseModel):
    status_host: str = "127.0.0.1"
    status_port: int = Field(default=8000, ge=1, le=65535)


class BinanceExecutionConfig(BaseModel):
    environment: Literal["testnet", "live"] = "testnet"
    rest_base_url: str = "https://testnet.binance.vision"
    api_key_environment_variable: str = "BINANCE_TESTNET_API_KEY"
    api_secret_environment_variable: str = "BINANCE_TESTNET_API_SECRET"
    recv_window_ms: int = Field(default=5000, ge=1000, le=60000)
    request_timeout_seconds: int = Field(default=10, gt=0)
    allow_live_trading: bool = False

    @model_validator(mode="after")
    def enforce_safe_environment(self) -> Self:
        parsed = urlparse(self.rest_base_url)
        canonical_origin = (
            parsed.scheme == "https"
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
        )
        if not canonical_origin:
            raise ValueError(
                "Binance execution URL must be a canonical HTTPS origin "
                "without port, credentials, path, query, or fragment"
            )
        if self.environment == "testnet":
            if parsed.hostname != "testnet.binance.vision":
                raise ValueError(
                    "Testnet execution requires the exact "
                    "testnet.binance.vision hostname"
                )
        elif (
            parsed.hostname != "api.binance.com"
            or not self.allow_live_trading
        ):
            raise ValueError(
                "live execution requires the exact api.binance.com hostname "
                "and allow_live_trading=true"
            )
        self.rest_base_url = self.rest_base_url.rstrip("/")
        return self


class OKXDemoExecutionConfig(BaseModel):
    rest_base_url: Literal[
        "https://us.okx.com",
    ] = "https://us.okx.com"
    api_key_environment_variable: str = "OKX_DEMO_API_KEY"
    api_secret_environment_variable: str = "OKX_DEMO_API_SECRET"
    passphrase_environment_variable: str = "OKX_DEMO_PASSPHRASE"
    request_timeout_seconds: float = Field(default=10, gt=0)
    transaction_expiry_ms: int = Field(default=5000, ge=1000, le=30000)
    instrument_type: Literal["SPOT"] = "SPOT"
    trade_mode: Literal["cash"] = "cash"


class DeribitTestExecutionConfig(BaseModel):
    rest_base_url: Literal[
        "https://test.deribit.com/api/v2"
    ] = "https://test.deribit.com/api/v2"
    client_id_environment_variable: str = "DERIBIT_TEST_CLIENT_ID"
    client_secret_environment_variable: str = "DERIBIT_TEST_CLIENT_SECRET"
    request_timeout_seconds: float = Field(default=10, gt=0)
    transaction_expiry_ms: int = Field(default=5000, ge=1000, le=30000)
    instrument_kind: Literal[
        "future",
        "option",
        "spot",
    ] = "future"


class EMSVenueInstrumentConfig(BaseModel):
    symbol: str = Field(min_length=1)
    compatibility_group: str = Field(min_length=1)
    quantity_unit: Literal["base", "quote", "contracts"] = "base"
    contract_size_quote: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_contract_unit(self) -> Self:
        if self.quantity_unit == "contracts" and self.contract_size_quote is None:
            raise ValueError(
                "contract_size_quote is required for contract quantities"
            )
        if self.quantity_unit != "contracts" and self.contract_size_quote is not None:
            raise ValueError(
                "contract_size_quote is only valid for contract quantities"
            )
        return self


class EMSInstrumentConfig(BaseModel):
    compatibility_group: str = Field(min_length=1)
    canonical_quantity_unit: Literal["base", "quote"] = "base"
    venues: dict[
        Literal["binance", "okx", "deribit"],
        EMSVenueInstrumentConfig,
    ]

    @model_validator(mode="after")
    def validate_compatibility(self) -> Self:
        if not self.venues:
            raise ValueError("EMS instrument requires at least one venue mapping")
        for mapping in self.venues.values():
            if mapping.compatibility_group != self.compatibility_group:
                raise ValueError(
                    "venue mapping compatibility group must match instrument"
                )
        return self


def _default_ems_instruments() -> dict[str, EMSInstrumentConfig]:
    return {
        "BTCUSDT": EMSInstrumentConfig(
            compatibility_group="btc-usdt-spot",
            venues={
                "binance": EMSVenueInstrumentConfig(
                    symbol="BTCUSDT",
                    compatibility_group="btc-usdt-spot",
                ),
                "okx": EMSVenueInstrumentConfig(
                    symbol="BTC-USDT",
                    compatibility_group="btc-usdt-spot",
                ),
            },
        ),
        "ETHUSDT": EMSInstrumentConfig(
            compatibility_group="eth-usdt-spot",
            venues={
                "binance": EMSVenueInstrumentConfig(
                    symbol="ETHUSDT",
                    compatibility_group="eth-usdt-spot",
                ),
                "okx": EMSVenueInstrumentConfig(
                    symbol="ETH-USDT",
                    compatibility_group="eth-usdt-spot",
                ),
            },
        ),
        "BTC-PERPETUAL": EMSInstrumentConfig(
            compatibility_group="btc-usd-inverse-perpetual",
            venues={
                "deribit": EMSVenueInstrumentConfig(
                    symbol="BTC-PERPETUAL",
                    compatibility_group="btc-usd-inverse-perpetual",
                    quantity_unit="quote",
                ),
            },
        ),
    }


class EMSConfig(BaseModel):
    enabled_venues: tuple[
        Literal["binance", "okx", "deribit"],
        ...,
    ] = ("binance", "okx", "deribit")
    dispatch_policy: Literal["sequential", "simultaneous"] = "sequential"
    max_reserved_notional: Decimal = Field(
        default=Decimal("1000000"),
        gt=0,
    )
    latency_penalty_bps_per_ms: Decimal = Field(default=Decimal(0), ge=0)
    instruments: dict[str, EMSInstrumentConfig] = Field(
        default_factory=_default_ems_instruments
    )

    @model_validator(mode="after")
    def validate_venues(self) -> Self:
        if not self.enabled_venues:
            raise ValueError("EMS requires at least one enabled venue")
        if len(set(self.enabled_venues)) != len(self.enabled_venues):
            raise ValueError("EMS enabled venues must be unique")
        normalized_symbols = [symbol.upper() for symbol in self.instruments]
        if len(set(normalized_symbols)) != len(normalized_symbols):
            raise ValueError("EMS canonical instrument symbols must be unique")
        for symbol in self.instruments:
            if symbol != symbol.upper():
                raise ValueError("EMS canonical instrument symbols must be uppercase")
        return self


class MinReqConfig(BaseModel):
    """Defaults for the minimum-requirements backtesting and paper stack."""

    dataset_root: Path = Path("datasets")
    report_root: Path = Path("reports/minreq")
    venues: tuple[str, ...] = ("binance", "okx")
    economic_group: str = "BTC/USDT.SPOT"
    seed: int = 20260817
    steps: int = 400
    entry_z: Decimal = Field(default=Decimal("1.2"), gt=0)
    exit_z: Decimal = Field(default=Decimal("0.3"), ge=0)
    target_quantity: Decimal = Field(default=Decimal("0.05"), gt=0)
    max_gross_notional: Decimal = Field(default=Decimal("200000"), gt=0)
    max_order_notional: Decimal = Field(default=Decimal("25000"), gt=0)
    execution_mode: Literal["internal_simulation", "internal_paper", "venue_testnet"] = (
        "internal_simulation"
    )
    latency_preset: str | None = None
    feed: Literal["synthetic", "live"] = "synthetic"
    live_symbol: str = "BTCUSDT"
    live_duration_seconds: float = Field(default=60.0, gt=0)

    @model_validator(mode="after")
    def validate_minreq(self) -> Self:
        if len(self.venues) < 2:
            raise ValueError("minreq requires at least two venues")
        if len(set(self.venues)) != len(self.venues):
            raise ValueError("minreq venues must be unique")
        if self.exit_z >= self.entry_z:
            raise ValueError("minreq exit_z must be below entry_z")
        if self.steps <= 0:
            raise ValueError("minreq steps must be positive")
        return self


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRADING_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    environment: str = "development"
    symbol: str = "BTCUSDT"
    binance: BinanceConfig = BinanceConfig()
    data_quality: DataQualityConfig = DataQualityConfig()
    risk: RiskConfig = RiskConfig()
    strategy: StrategyConfig = StrategyConfig()
    paper: PaperConfig = PaperConfig()
    storage: StorageConfig = StorageConfig()
    operations: OperationsConfig = OperationsConfig()
    binance_execution: BinanceExecutionConfig = BinanceExecutionConfig()
    okx_demo_execution: OKXDemoExecutionConfig = OKXDemoExecutionConfig()
    deribit_test_execution: DeribitTestExecutionConfig = (
        DeribitTestExecutionConfig()
    )
    ems: EMSConfig = EMSConfig()
    minreq: MinReqConfig = MinReqConfig()

    @classmethod
    def from_yaml(cls, path: Path) -> AppConfig:
        if not path.is_file():
            raise FileNotFoundError(f"configuration file does not exist: {path}")
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError("configuration root must be a mapping")
        return cls(**raw)
