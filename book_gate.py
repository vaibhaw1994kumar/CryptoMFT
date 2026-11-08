from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import time

from .models import BookState
from .orderbook import OrderBook


_DAY_NS = 86_400 * 1_000_000_000
CancelCallback = Callable[[str, int], int]
VenueControlCallback = Callable[[str, bool, str, int], int]


@dataclass(slots=True)
class BookIntegrityGate:
    """Suspend strategy evaluation and request cancels when any required book is unsafe."""

    max_book_age_ns: int
    cancel_callback: CancelCallback
    suspended: bool = True
    reason: str = "INITIAL_SYNC"
    suspend_count: int = 0
    resume_count: int = 0
    cancel_requests: int = 0
    cancelled_orders: int = 0
    venue_control_callback: VenueControlCallback | None = None
    venue_validity: dict[str, bool] = field(default_factory=dict)
    venue_transitions: int = 0
    # Suspended time (Phase D, owner 8 Nov): every interval the gate spent blocking, split at UTC midnight and keyed by cause ("BYBIT:CHECKSUM_FAILED", "BINANCE:SYNCING"...).
    # Measured between the receive times of the calls to ``allow``, so it is as fine as the event stream. Observation only: it changes no decision.
    suspended_ns_by_day: dict[str, dict[str, int]] = field(default_factory=dict)
    suspension_intervals: list[tuple[int, int, str]] = field(default_factory=list)      # (start ns, end ns, cause): the windows a parity comparison masks
    _since_ns: int | None = None
    _since_cause: str = ""

    def allow(
        self, books: tuple[OrderBook, ...], now_ns: int,
        ages_ns: dict[str, int | None] | None = None,
    ) -> bool:
        """``ages_ns`` (live): per-venue age of the last applied update on a monotonic
        clock. Without it (replay) the age is ``now_ns`` minus the recorded receive time."""
        for book in books:
            if ages_ns is None:
                book.mark_stale(now_ns, self.max_book_age_ns)
            elif ages_ns.get(book.venue) is not None:
                book.mark_stale_age(ages_ns[book.venue], self.max_book_age_ns)
            valid = book.state == BookState.VALID
            previous = self.venue_validity.get(book.venue)
            if previous is None or previous != valid:
                self.venue_validity[book.venue] = valid
                self.venue_transitions += 1
                if self.venue_control_callback is not None:
                    reason = "" if valid else (
                        f"{book.state.value}:{book.invalid_reason}"
                    )
                    cancelled = self.venue_control_callback(
                        book.venue, valid, reason, now_ns
                    )
                    if previous is True and not valid:
                        self.cancel_requests += 1
                        self.cancelled_orders += cancelled
        blocked = [book for book in books if book.state != BookState.VALID]
        self._account(now_ns, "+".join(sorted(f"{book.venue}:{book.state.value}" for book in blocked)) or None)
        if blocked:
            reason = ";".join(
                f"{book.venue}:{book.state.value}:{book.invalid_reason}" for book in blocked
            )
            if not self.suspended:
                self.suspended = True
                self.suspend_count += 1
                if self.venue_control_callback is None:
                    self.cancel_requests += 1
                    self.cancelled_orders += self.cancel_callback(reason, now_ns)
            self.reason = reason
            return False
        if self.suspended:
            self.suspended = False
            self.reason = ""
            self.resume_count += 1
        return True

    def _account(self, now_ns: int, cause: str | None) -> None:
        if self._since_ns is not None and cause != self._since_cause:
            self.suspension_intervals.append((self._since_ns, now_ns, self._since_cause))
            for day, ns in self._segments(self._since_ns, now_ns):
                bucket = self.suspended_ns_by_day.setdefault(day, {})
                bucket[self._since_cause] = bucket.get(self._since_cause, 0) + ns
            self._since_ns = None
        if cause is not None and self._since_ns is None:
            self._since_ns, self._since_cause = now_ns, cause

    @staticmethod
    def _segments(start_ns: int, end_ns: int):
        while start_ns < end_ns:
            stop = min(end_ns, (start_ns // _DAY_NS + 1) * _DAY_NS)
            yield time.strftime("%Y-%m-%d", time.gmtime(start_ns // 1_000_000_000)), stop - start_ns
            start_ns = stop

    def open_intervals(self, now_ns: int) -> list[tuple[int, int, str]]:
        """Every suspension window so far, closed ones and the one still open at ``now_ns``."""
        return self.suspension_intervals + ([(self._since_ns, now_ns, self._since_cause)] if self._since_ns is not None else [])

    def suspended_seconds_by_day(self, now_ns: int) -> dict[str, dict[str, float]]:
        """Suspended seconds per UTC day and cause, including a suspension that is still open at ``now_ns``."""
        out = {day: dict(causes) for day, causes in self.suspended_ns_by_day.items()}
        if self._since_ns is not None:
            for day, ns in self._segments(self._since_ns, now_ns):
                out.setdefault(day, {})[self._since_cause] = out.get(day, {}).get(self._since_cause, 0) + ns
        return {day: {cause: ns / 1e9 for cause, ns in sorted(causes.items())} for day, causes in sorted(out.items())}

    def venue_state(self, venue: str) -> str:
        return "ACTIVE" if self.venue_validity.get(venue, False) else "RISK_REDUCING_ONLY"
