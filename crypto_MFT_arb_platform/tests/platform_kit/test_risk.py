from decimal import Decimal
from uuid import uuid4

from trading_platform_poc.config import RiskConfig
from trading_platform_poc.models import OrderIntent, OrderType, Side
from trading_platform_poc.risk import RiskContext, RiskEngine


def intent(quantity: str = "0.01", price: str = "100") -> OrderIntent:
    return OrderIntent(
        intent_id=uuid4(),
        strategy_id="test",
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal(quantity),
        price=Decimal(price),
        created_timestamp_ns=1,
    )


def context(**overrides) -> RiskContext:
    values = {
        "position": Decimal(0),
        "open_order_count": 0,
        "daily_net_pnl": Decimal(0),
        "feed_stale": False,
        "mid_price": Decimal("100"),
    }
    values.update(overrides)
    return RiskContext(**values)


def test_approves_order_inside_limits() -> None:
    engine = RiskEngine(RiskConfig(max_net_position=Decimal("1")))

    decision = engine.evaluate(intent(), context(), 10)

    assert decision.approved is True
    assert decision.reason == "approved"


def test_rejects_stale_feed() -> None:
    engine = RiskEngine(RiskConfig())

    decision = engine.evaluate(intent(), context(feed_stale=True), 10)

    assert decision.approved is False
    assert decision.reason == "stale_market_data"


def test_rejects_kill_switch() -> None:
    engine = RiskEngine(RiskConfig())
    engine.set_kill_switch(True)

    decision = engine.evaluate(intent(), context(), 10)

    assert decision.reason == "kill_switch_active"


def test_rejects_projected_position() -> None:
    engine = RiskEngine(RiskConfig(max_net_position=Decimal("0.005")))

    decision = engine.evaluate(intent(), context(), 10)

    assert decision.reason == "max_net_position"
