from decimal import Decimal
from uuid import uuid4

from trading_platform_poc.models import Fill, Side
from trading_platform_poc.portfolio import Portfolio


def fill(side: Side, price: str, quantity: str = "1") -> Fill:
    return Fill(
        fill_id=uuid4(),
        order_id=uuid4(),
        symbol="BTCUSDT",
        side=side,
        price=Decimal(price),
        quantity=Decimal(quantity),
        fee=Decimal("0.01"),
        fee_asset="USDT",
        fill_timestamp_ns=1,
    )


def test_round_trip_position_and_pnl() -> None:
    portfolio = Portfolio()

    portfolio.apply_fill(fill(Side.BUY, "100"))
    portfolio.apply_fill(fill(Side.SELL, "105"))

    assert portfolio.position == 0
    assert portfolio.realized_pnl == Decimal("5")
    assert portfolio.fees == Decimal("0.02")
    assert portfolio.net_realized_pnl == Decimal("4.98")
