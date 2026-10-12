import os
from decimal import Decimal, ROUND_UP
from uuid import uuid4

import pytest

from trading_platform_poc.binance_execution import (
    BinanceCredentials,
    BinanceSpotTestnetGateway,
)
from trading_platform_poc.models import (
    Order,
    OrderState,
    OrderType,
    Side,
)


pytestmark = pytest.mark.binance_testnet


@pytest.mark.asyncio
async def test_signed_account_and_order_test_endpoint() -> None:
    api_key = os.environ.get("BINANCE_TESTNET_API_KEY")
    api_secret = os.environ.get("BINANCE_TESTNET_API_SECRET")
    if not api_key or not api_secret:
        pytest.skip("Binance Spot Testnet credentials are not configured")

    async with BinanceSpotTestnetGateway(
        credentials=BinanceCredentials(api_key, api_secret)
    ) as gateway:
        account = await gateway.account()
        price = await gateway.ticker_price("BTCUSDT")
        rules = await gateway.symbol_rules("BTCUSDT")
        target_notional = max(
            rules.minimum_notional * Decimal("1.1"),
            Decimal("10"),
        )
        market_step = rules.market_step_size or rules.step_size
        units = (
            target_notional / price / market_step
        ).to_integral_value(rounding=ROUND_UP)
        quantity = units * market_step
        order_id = uuid4()
        order = Order(
            order_id=order_id,
            intent_id=uuid4(),
            client_order_id=f"smoke-{order_id.hex[:20]}",
            symbol="BTCUSDT",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=quantity,
            price=None,
            state=OrderState.SUBMITTED,
            created_timestamp_ns=1,
        )

        result = await gateway.submit_order(
            order,
            reference_price=price,
            test_only=True,
        )

    assert account["canTrade"] is True
    assert result.status == "TEST_ACCEPTED"
