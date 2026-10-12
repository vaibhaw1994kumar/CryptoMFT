from decimal import Decimal
from uuid import uuid4

from trading_platform_poc.config import PaperConfig
from trading_platform_poc.models import (
    BookSnapshot,
    Order,
    OrderState,
    OrderType,
    PriceLevel,
    Side,
    TradeTick,
)
from trading_platform_poc.order_book import BinanceOrderBook
from trading_platform_poc.paper_exchange import PaperExchange


def book() -> BinanceOrderBook:
    result = BinanceOrderBook("BTCUSDT")
    result.load_snapshot(
        BookSnapshot(
            "BTCUSDT",
            10,
            (PriceLevel(Decimal("100"), Decimal("1")),),
            (PriceLevel(Decimal("101"), Decimal("1")),),
        ),
        1,
    )
    return result


def order(
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.POST_ONLY,
    price: str = "100",
) -> Order:
    order_id = uuid4()
    return Order(
        order_id=order_id,
        intent_id=uuid4(),
        client_order_id=f"test-{order_id}",
        symbol="BTCUSDT",
        side=side,
        order_type=order_type,
        quantity=Decimal("0.01"),
        price=Decimal(price),
        state=OrderState.SUBMITTED,
        created_timestamp_ns=1,
    )


def test_acknowledges_resting_post_only_order_then_fills_on_trade() -> None:
    exchange = PaperExchange(PaperConfig())
    resting = order()

    submitted = exchange.submit(resting, book(), 2)
    traded = exchange.process_trade(
        TradeTick(
            "BTCUSDT",
            1,
            Decimal("100"),
            Decimal("0.5"),
            False,
            3,
        ),
        3,
    )

    assert submitted.updates[0].state is OrderState.ACKNOWLEDGED
    assert traded.updates[0].state is OrderState.FILLED
    assert traded.fills[0].price == Decimal("100")


def test_rejects_crossing_post_only_order() -> None:
    exchange = PaperExchange(PaperConfig())

    result = exchange.submit(order(price="101"), book(), 2)

    assert result.updates[0].state is OrderState.REJECTED
    assert result.updates[0].reason == "post_only_would_cross"


def test_fills_marketable_limit_at_best_ask() -> None:
    exchange = PaperExchange(PaperConfig())

    result = exchange.submit(
        order(order_type=OrderType.LIMIT, price="101"),
        book(),
        2,
    )

    assert result.fills[0].price == Decimal("101")
    assert result.updates[-1].state is OrderState.FILLED


def test_respects_aggressor_side_and_partial_trade_quantity() -> None:
    exchange = PaperExchange(PaperConfig())
    resting = order()
    exchange.submit(resting, book(), 2)

    wrong_side = exchange.process_trade(
        TradeTick(
            "BTCUSDT",
            1,
            Decimal("100"),
            Decimal("0.005"),
            True,
            3,
        ),
        3,
    )
    partial = exchange.process_trade(
        TradeTick(
            "BTCUSDT",
            2,
            Decimal("100"),
            Decimal("0.005"),
            False,
            4,
        ),
        4,
    )
    completed = exchange.process_trade(
        TradeTick(
            "BTCUSDT",
            3,
            Decimal("100"),
            Decimal("0.005"),
            False,
            5,
        ),
        5,
    )

    assert wrong_side.fills == ()
    assert partial.fills[0].quantity == Decimal("0.005")
    assert partial.updates[0].state is OrderState.PARTIALLY_FILLED
    assert completed.updates[0].state is OrderState.FILLED


def test_applies_configured_acknowledgement_and_cancel_latency() -> None:
    exchange = PaperExchange(
        PaperConfig(
            acknowledgement_latency_ms=7,
            cancellation_latency_ms=11,
        )
    )
    resting = order()

    submitted = exchange.submit(resting, book(), 1_000_000)
    canceled = exchange.cancel_all(2_000_000)

    assert submitted.updates[0].update_timestamp_ns == 8_000_000
    assert canceled.updates[0].update_timestamp_ns == 13_000_000
