from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    def now_ns(self) -> int: ...

    async def sleep_until(self, timestamp_ns: int) -> None: ...


class LiveClock:
    def now_ns(self) -> int:
        return time.time_ns()

    async def sleep_until(self, timestamp_ns: int) -> None:
        delay_seconds = max(0, timestamp_ns - self.now_ns()) / 1_000_000_000
        await asyncio.sleep(delay_seconds)


class SimulatedClock:
    def __init__(self, start_timestamp_ns: int = 0) -> None:
        if start_timestamp_ns < 0:
            raise ValueError("start_timestamp_ns must not be negative")
        self._timestamp_ns = start_timestamp_ns

    def now_ns(self) -> int:
        return self._timestamp_ns

    async def sleep_until(self, timestamp_ns: int) -> None:
        if timestamp_ns < self._timestamp_ns:
            raise ValueError("simulated time cannot move backwards")
        self._timestamp_ns = timestamp_ns
        await asyncio.sleep(0)

    def advance_to(self, timestamp_ns: int) -> None:
        if timestamp_ns < self._timestamp_ns:
            raise ValueError("simulated time cannot move backwards")
        self._timestamp_ns = timestamp_ns
