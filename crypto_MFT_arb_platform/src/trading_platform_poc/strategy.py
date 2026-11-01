from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from uuid import NAMESPACE_URL, uuid5

from trading_platform_poc.config import StrategyConfig
from trading_platform_poc.models import Order, OrderIntent, OrderState, OrderType, Side
from trading_platform_poc.order_book import BinanceOrderBook


class SpreadCaptureStrategy:
    strategy_id = "spread_capture"
    strategy_version = "1.0.0"

    def __init__(self, config: StrategyConfig) -> None:
        self._config = config
        self._last_quote_timestamp_ns: int | None = None

    def evaluate(
        self,
        book: BinanceOrderBook,
        timestamp_ns: int,
        open_orders: tuple[Order, ...],
    ) -> tuple[OrderIntent, ...]:
        spread_bps = book.spread_bps()
        mid_price = book.mid_price
        if (
            spread_bps is None
            or mid_price is None
            or spread_bps < self._config.spread_threshold_bps
        ):
            return ()
        if (
            self._last_quote_timestamp_ns is not None
            and timestamp_ns - self._last_quote_timestamp_ns
            < self._config.quote_cooldown_ms * 1_000_000
        ):
            return ()

        open_sides = {
            order.side
            for order in open_orders
            if order.state
            in {
                OrderState.CREATED,
                OrderState.RISK_APPROVED,
                OrderState.SUBMITTED,
                OrderState.ACKNOWLEDGED,
                OrderState.PARTIALLY_FILLED,
                OrderState.RECONCILIATION_REQUIRED,
            }
        }
        quantity = (
            self._config.order_notional / mid_price
        ).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        if quantity <= 0:
            return ()

        intents: list[OrderIntent] = []
        for side, price in (
            (Side.BUY, book.best_bid),
            (Side.SELL, book.best_ask),
        ):
            if side in open_sides or price is None:
                continue
            key = (
                f"{self.strategy_id}:{book.symbol}:{side.value}:"
                f"{price}:{timestamp_ns}"
            )
            intents.append(
                OrderIntent(
                    intent_id=uuid5(NAMESPACE_URL, key),
                    strategy_id=self.strategy_id,
                    symbol=book.symbol,
                    side=side,
                    order_type=OrderType.POST_ONLY,
                    quantity=quantity,
                    price=price,
                    created_timestamp_ns=timestamp_ns,
                )
            )
        if intents:
            self._last_quote_timestamp_ns = timestamp_ns
        return tuple(intents)

    def should_cancel(self, book: BinanceOrderBook) -> bool:
        spread_bps = book.spread_bps()
        return (
            spread_bps is None
            or spread_bps < self._config.spread_threshold_bps
        )
