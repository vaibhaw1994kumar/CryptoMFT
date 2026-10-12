"""One event-time clock for simulated execution.

Every simulated latency, order arrival, cancel, acknowledgement, fill report,
timeout and strategy timer is scheduled on this clock instead of reading
``time.monotonic``/``time.time``. The clock only moves when the driver
advances it to the next market event, and it fires every timer due at or
before that event first, so a replay and a live run that see the same events
in the same order produce the same timeline.

Ordering rules:

* timers fire in ``(due_ts_ns, scheduling order)`` order;
* a timer due at exactly an event's timestamp fires *before* that event is
  applied (an order that reaches the venue at ``t`` joins the queue before a
  print received at ``t``), matching the OMS, which runs in-flight timers
  before each book update;
* time never goes backwards: advancing to an earlier timestamp (a wall clock
  stepping back between two live frames) leaves ``now_ns`` unchanged and is
  counted in ``regressions``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import heapq
import itertools


@dataclass(order=True, slots=True)
class _Timer:
    due_ns: int
    ordinal: int
    callback: Callable[[int], None] = field(compare=False)
    label: str = field(default="", compare=False)
    cancelled: bool = field(default=False, compare=False)


class TimerHandle:
    __slots__ = ("_timer",)

    def __init__(self, timer: _Timer) -> None:
        self._timer = timer

    @property
    def due_ns(self) -> int:
        return self._timer.due_ns

    def cancel(self) -> None:
        self._timer.cancelled = True


class SimulationClock:
    def __init__(self, start_ns: int = 0) -> None:
        self._now_ns = start_ns
        self._started = start_ns != 0
        self._heap: list[_Timer] = []
        self._ordinal = itertools.count(1)
        self.fired = 0
        self.regressions = 0

    def now_ns(self) -> int:
        return self._now_ns

    @property
    def started(self) -> bool:
        return self._started

    def schedule(
        self, due_ns: int, callback: Callable[[int], None], *, label: str = ""
    ) -> TimerHandle:
        """Run ``callback(due_ns)`` once the clock reaches ``due_ns``.

        A timer in the past (or now) fires on the next ``advance_to``; its
        callback still receives the time it was due, never a later one.
        """
        timer = _Timer(max(int(due_ns), self._now_ns), next(self._ordinal), callback, label)
        heapq.heappush(self._heap, timer)
        return TimerHandle(timer)

    def schedule_after(
        self, delay_ns: int, callback: Callable[[int], None], *, label: str = ""
    ) -> TimerHandle:
        return self.schedule(self._now_ns + max(0, int(delay_ns)), callback, label=label)

    def next_due_ns(self) -> int | None:
        while self._heap and self._heap[0].cancelled:
            heapq.heappop(self._heap)
        return self._heap[0].due_ns if self._heap else None

    def advance_to(self, ts_ns: int) -> int:
        """Fire every timer due at or before ``ts_ns``, then set the clock there."""
        ts_ns = int(ts_ns)
        if not self._started:
            self._now_ns = ts_ns
            self._started = True
        if ts_ns < self._now_ns:
            self.regressions += 1
            ts_ns = self._now_ns
        while self._heap and self._heap[0].due_ns <= ts_ns:
            timer = heapq.heappop(self._heap)
            if timer.cancelled:
                continue
            self._now_ns = timer.due_ns
            self.fired += 1
            timer.callback(timer.due_ns)
        self._now_ns = ts_ns
        return self._now_ns

    def drain(self, until_ns: int | None = None) -> int:
        """Fire all remaining timers (or those due by ``until_ns``)."""
        while True:
            due = self.next_due_ns()
            if due is None or (until_ns is not None and due > until_ns):
                break
            self.advance_to(due)
        if until_ns is not None:
            self.advance_to(max(until_ns, self._now_ns))
        return self._now_ns

    def pending(self) -> int:
        return sum(1 for timer in self._heap if not timer.cancelled)
