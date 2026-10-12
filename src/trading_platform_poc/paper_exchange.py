from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, NAMESPACE_URL, uuid5

from trading_platform_poc.config import PaperConfig
from trading_platform_poc.models import (
    Fill,
    Order,
    OrderState,
    OrderType,
    OrderUpdate,
    Side,
    TradeTick,
)
from trading_platform_poc.order_book import BinanceOrderBook


@dataclass(frozen=True, slots=True)
class PaperResult:
    updates: tuple[OrderUpdate, ...] = ()
    fills: tuple[Fill, ...] = ()


class PaperExchange:
    def __init__(self, config: PaperConfig) -> None:
        self._config = config
        self._resting_orders: dict[UUID, Order] = {}
        self._remaining_quantities: dict[UUID, Decimal] = {}
        self._filled_quantities: dict[UUID, Decimal] = {}

    def submit(
        self,
        order: Order,
        book: BinanceOrderBook,
        timestamp_ns: int,
    ) -> PaperResult:
        if book.best_bid is None or book.best_ask is None:
            return PaperResult(
                updates=(
                    self._update(
                        order,
                        OrderState.REJECTED,
                        timestamp_ns,
                        "invalid_book",
                    ),
                )
            )
        if self._post_only_would_cross(order, book):
            return PaperResult(
                updates=(
                    self._update(
                        order,
                        OrderState.REJECTED,
                        timestamp_ns,
                        "post_only_would_cross",
                    ),
                )
            )

        acknowledgement = self._update(
            order,
            OrderState.ACKNOWLEDGED,
            timestamp_ns
            + self._config.acknowledgement_latency_ms * 1_000_000,
            None,
        )
        fill_price = self._marketable_fill_price(order, book)
        if fill_price is not None:
            fill_timestamp_ns = acknowledgement.update_timestamp_ns
            fill = self._fill(
                order,
                fill_price,
                order.quantity,
                fill_timestamp_ns,
                self._config.taker_fee_bps,
            )
            return PaperResult(
                updates=(
                    acknowledgement,
                    self._filled_update(
                        order,
                        fill,
                        order.quantity,
                        fill_timestamp_ns,
                    ),
                ),
                fills=(fill,),
            )
        self._resting_orders[order.order_id] = order
        self._remaining_quantities[order.order_id] = order.quantity
        self._filled_quantities[order.order_id] = Decimal(0)
        return PaperResult(updates=(acknowledgement,))

    def process_trade(
        self,
        trade: TradeTick,
        timestamp_ns: int,
    ) -> PaperResult:
        updates: list[OrderUpdate] = []
        fills: list[Fill] = []
        for order_id, order in tuple(self._resting_orders.items()):
            if order.symbol != trade.symbol or order.price is None:
                continue
            seller_aggressed = not trade.buyer_is_maker
            buyer_aggressed = trade.buyer_is_maker
            should_fill = (
                order.side is Side.BUY
                and seller_aggressed
                and trade.price <= order.price
            ) or (
                order.side is Side.SELL
                and buyer_aggressed
                and trade.price >= order.price
            )
            if not should_fill:
                continue
            remaining = self._remaining_quantities[order_id]
            fill_quantity = min(remaining, trade.quantity)
            fill = self._fill(
                order,
                order.price,
                fill_quantity,
                timestamp_ns,
                self._config.maker_fee_bps,
            )
            fills.append(fill)
            cumulative = self._filled_quantities[order_id] + fill_quantity
            self._filled_quantities[order_id] = cumulative
            remaining -= fill_quantity
            self._remaining_quantities[order_id] = remaining
            if remaining == 0:
                updates.append(
                    self._filled_update(order, fill, cumulative, timestamp_ns)
                )
                del self._resting_orders[order_id]
                del self._remaining_quantities[order_id]
                del self._filled_quantities[order_id]
            else:
                updates.append(
                    OrderUpdate(
                        order_id=order.order_id,
                        state=OrderState.PARTIALLY_FILLED,
                        cumulative_filled_quantity=cumulative,
                        average_fill_price=fill.price,
                        reason=None,
                        update_timestamp_ns=timestamp_ns,
                    )
                )
        return PaperResult(tuple(updates), tuple(fills))

    def cancel_all(self, timestamp_ns: int) -> PaperResult:
        cancellation_timestamp_ns = (
            timestamp_ns + self._config.cancellation_latency_ms * 1_000_000
        )
        updates = tuple(
            self._update(
                order,
                OrderState.CANCELED,
                cancellation_timestamp_ns,
                "cancel_all",
            )
            for order in self._resting_orders.values()
        )
        self._resting_orders.clear()
        self._remaining_quantities.clear()
        self._filled_quantities.clear()
        return PaperResult(updates=updates)

    @staticmethod
    def _post_only_would_cross(
        order: Order,
        book: BinanceOrderBook,
    ) -> bool:
        if order.order_type is not OrderType.POST_ONLY or order.price is None:
            return False
        if order.side is Side.BUY:
            return book.best_ask is not None and order.price >= book.best_ask
        return book.best_bid is not None and order.price <= book.best_bid

    @staticmethod
    def _marketable_fill_price(
        order: Order,
        book: BinanceOrderBook,
    ) -> Decimal | None:
        if order.order_type is OrderType.POST_ONLY:
            return None
        if order.side is Side.BUY:
            if order.order_type is OrderType.MARKET:
                return book.best_ask
            if (
                order.price is not None
                and book.best_ask is not None
                and order.price >= book.best_ask
            ):
                return book.best_ask
        else:
            if order.order_type is OrderType.MARKET:
                return book.best_bid
            if (
                order.price is not None
                and book.best_bid is not None
                and order.price <= book.best_bid
            ):
                return book.best_bid
        return None

    @staticmethod
    def _update(
        order: Order,
        state: OrderState,
        timestamp_ns: int,
        reason: str | None,
    ) -> OrderUpdate:
        return OrderUpdate(
            order_id=order.order_id,
            state=state,
            cumulative_filled_quantity=Decimal(0),
            average_fill_price=None,
            reason=reason,
            update_timestamp_ns=timestamp_ns,
        )

    @staticmethod
    def _filled_update(
        order: Order,
        fill: Fill,
        cumulative_quantity: Decimal,
        timestamp_ns: int,
    ) -> OrderUpdate:
        return OrderUpdate(
            order_id=order.order_id,
            state=OrderState.FILLED,
            cumulative_filled_quantity=cumulative_quantity,
            average_fill_price=fill.price,
            reason=None,
            update_timestamp_ns=timestamp_ns,
        )

    @staticmethod
    def _fill(
        order: Order,
        price: Decimal,
        quantity: Decimal,
        timestamp_ns: int,
        fee_bps: Decimal,
    ) -> Fill:
        fee = price * quantity * fee_bps / Decimal(10_000)
        fill_id = uuid5(
            NAMESPACE_URL,
            f"fill:{order.order_id}:{price}:{quantity}:{timestamp_ns}",
        )
        return Fill(
            fill_id=fill_id,
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            price=price,
            quantity=quantity,
            fee=fee,
            fee_asset="USDT",
            fill_timestamp_ns=timestamp_ns,
        )
