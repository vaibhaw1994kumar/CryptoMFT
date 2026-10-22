from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .models import BookState
from .orderbook import OrderBook


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

    def venue_state(self, venue: str) -> str:
        return "ACTIVE" if self.venue_validity.get(venue, False) else "RISK_REDUCING_ONLY"
