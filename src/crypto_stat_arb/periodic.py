"""Periodic economic settlements driven by an as-of quantity (funding, borrow interest).

A settlement at time ``ts`` on a line (e.g. a perpetual position or a loan)
is computed from the line's quantity as of ``ts`` by venue/effective time,
booked once per revision, and reversed and rebooked when a change effective
before ``ts`` is reported after it was booked. Subclasses supply the line
schedule, the as-of quantity and the event; this class owns the timing:

* settle at ``now - settle_lag`` while data is flowing, up to ``now`` when
  final; ``now`` is always a market-data time, so nothing past the data is
  settled;
* each ``(line, ts)`` is processed once; a rebook gets the next revision in
  its idempotency key, so history is never rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Hashable, Iterable

from .economics import EconomicEvent

ZERO = Decimal("0")


@dataclass(slots=True)
class Settled:
    event: EconomicEvent
    quantity: Decimal
    revision: int


class PeriodicSettlement:
    def __init__(self, *, settle_lag_ns: int) -> None:
        self.settle_lag_ns = settle_lag_ns
        self.start_ns: int | None = None
        self.horizon_ns = 0
        self.rebooked = 0
        self._processed: set[tuple[Hashable, int]] = set()
        self._settled: dict[tuple[Hashable, int], Settled] = {}
        self._revisions: dict[tuple[Hashable, int], int] = {}

    # -- subclass hooks --------------------------------------------------------------
    def _lines(self) -> Iterable[Hashable]:
        raise NotImplementedError

    def _times(self, line: Hashable, start_ns: int, until_ns: int) -> list[int]:
        raise NotImplementedError

    def _quantity(self, line: Hashable, ts_ns: int) -> Decimal:
        raise NotImplementedError

    def _build(
        self, line: Hashable, ts_ns: int, quantity: Decimal, revision: int, booked_ns: int,
    ) -> EconomicEvent | None:
        """The settlement event, or None (with the reason flagged) when nothing is booked."""
        raise NotImplementedError

    def _book(self, event: EconomicEvent) -> None:
        raise NotImplementedError

    def _unbook(self, event_id: str, ts_ns: int, reason: str) -> None:
        raise NotImplementedError

    def _note_rebook(self, line: Hashable, ts_ns: int, quantity: Decimal, detail: str) -> None:
        """Record that a settlement was revisited; also clear its earlier data flags."""

    # -- engine ------------------------------------------------------------------------
    @staticmethod
    def schedule(anchor_ns: int, interval_ns: int, start_ns: int, until_ns: int) -> list[int]:
        if interval_ns <= 0:
            return []
        k = -(-(start_ns - anchor_ns) // interval_ns)  # first boundary at or after start
        ts = anchor_ns + k * interval_ns
        out = []
        while ts <= until_ns:
            out.append(ts)
            ts += interval_ns
        return out

    def begin(self, ts_ns: int) -> None:
        if self.start_ns is None:
            self.start_ns = ts_ns

    def settle_until(self, now_ns: int, *, final: bool = False) -> None:
        if self.start_ns is None:
            return
        self.horizon_ns = max(self.horizon_ns, now_ns)
        cutoff = now_ns if final else now_ns - self.settle_lag_ns
        for line in list(self._lines()):
            for ts in self._times(line, self.start_ns, cutoff):
                if (line, ts) not in self._processed:
                    self._processed.add((line, ts))
                    self._settle(line, ts, now_ns)

    def _settle(self, line: Hashable, ts_ns: int, booked_ns: int) -> None:
        quantity = self._quantity(line, ts_ns)
        revision = self._revisions.get((line, ts_ns), 0)
        event = self._build(line, ts_ns, quantity, revision, booked_ns)
        if event is None:
            return
        self._revisions[(line, ts_ns)] = revision + 1
        self._book(event)
        self._settled[(line, ts_ns)] = Settled(event, quantity, revision)

    def on_change(self, line: Hashable, effective_ns: int, reported_ns: int, detail: str) -> None:
        """A change to ``line`` effective at ``effective_ns`` was just booked."""
        for (item, ts_ns), settled in sorted(self._settled.items(), key=lambda kv: kv[0][1]):
            if item != line or effective_ns >= ts_ns:
                continue
            if self._quantity(line, ts_ns) == settled.quantity:
                continue
            self._unbook(settled.event.event_id, reported_ns, f"late change: {detail}")
            del self._settled[(line, ts_ns)]
            self.rebooked += 1
            self._note_rebook(line, ts_ns, settled.quantity, detail)
            self._settle(line, ts_ns, reported_ns)
        for item, ts_ns in sorted(self._processed, key=lambda key: key[1]):
            if (
                item != line or (line, ts_ns) in self._settled or effective_ns >= ts_ns
                or self._quantity(line, ts_ns) == 0
            ):
                continue
            self.rebooked += 1
            self._note_rebook(line, ts_ns, ZERO, detail)
            self._settle(line, ts_ns, reported_ns)

    def settled_events(self) -> list[EconomicEvent]:
        return [s.event for s in self._settled.values()]

    def timing_rules(self) -> dict[str, Any]:
        return {"settle_lag_ns": self.settle_lag_ns, "horizon_ns": self.horizon_ns}
