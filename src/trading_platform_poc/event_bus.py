from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable

from trading_platform_poc.models import EventType, MarketEvent


EventHandler = Callable[[MarketEvent], Awaitable[None]]


class EventBus:
    """Dispatch events sequentially so handler ordering remains deterministic."""

    def __init__(self) -> None:
        self._handlers: dict[EventType, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        handlers = self._handlers[event_type]
        if handler in handlers:
            raise ValueError(f"handler already subscribed for {event_type.value}")
        handlers.append(handler)

    async def publish(self, event: MarketEvent) -> None:
        for handler in tuple(self._handlers[event.event_type]):
            await handler(event)
