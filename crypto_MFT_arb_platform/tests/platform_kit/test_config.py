from decimal import Decimal

import pytest

from trading_platform_poc.config import (
    AppConfig,
    BinanceExecutionConfig,
    DataQualityConfig,
)


def test_loads_project_configuration() -> None:
    config = AppConfig.from_yaml(__import__("pathlib").Path("config.yaml"))

    assert config.symbol == "BTCUSDT"
    assert config.risk.max_order_notional == Decimal("1000")
    assert config.strategy.order_notional == Decimal("25")


def test_rejects_inverted_data_quality_bounds() -> None:
    with pytest.raises(ValueError, match="max_price"):
        DataQualityConfig(
            min_price=Decimal("100"),
            max_price=Decimal("10"),
        )


def test_normalizes_canonical_binance_origin() -> None:
    config = BinanceExecutionConfig(
        rest_base_url="https://testnet.binance.vision/"
    )

    assert config.rest_base_url == "https://testnet.binance.vision"
