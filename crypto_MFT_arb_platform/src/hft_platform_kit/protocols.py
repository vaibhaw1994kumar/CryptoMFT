from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from trading_platform_poc.models import MarketEvent, Order, OrderIntent, TradeTick
from trading_platform_poc.order_book import BinanceOrderBook
from trading_platform_poc.paper_exchange import PaperResult


@runtime_checkable
class Strategy(Protocol):
    """Extension contract for custom strategy implementations."""

    strategy_id: str
    strategy_version: str

    def evaluate(
        self,
        book: BinanceOrderBook,
        timestamp_ns: int,
        open_orders: tuple[Order, ...],
    ) -> tuple[OrderIntent, ...]: ...

    def should_cancel(self, book: BinanceOrderBook) -> bool: ...


@runtime_checkable
class ExecutionVenue(Protocol):
    """Execution contract implemented by paper or external venue adapters."""

    def submit(
        self,
        order: Order,
        book: BinanceOrderBook,
        timestamp_ns: int,
    ) -> PaperResult: ...

    def process_trade(
        self,
        trade: TradeTick,
        timestamp_ns: int,
    ) -> PaperResult: ...

    def cancel_all(self, timestamp_ns: int) -> PaperResult: ...


@runtime_checkable
class MarketDataSource(Protocol):
    """Asynchronous source of raw and canonical market events."""

    def events(
        self,
    ) -> AsyncIterator[tuple[dict[str, Any], MarketEvent]]: ...
