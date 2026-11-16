"""Venue rate limits by scope, with capacity kept for cancels (requirement EMS02).

Venues limit requests per IP (request weight), per account (order count) and per
user per endpoint. ``capabilities.VENUES[...].rate_limits`` records each limit;
this module turns them into sliding-window buckets and decides, for one request,
whether every bucket it touches has room.

Priority tiers, per bucket (a request may use up to its tier's share of capacity):

    NEW_RISK        capacity x quote_share (0.8), and at least quote_min_free (2) requests left free
    RISK_REDUCING   capacity            (hedges, unwinds, flattens)
    CANCEL          capacity            (cancels and order queries: recovery traffic)

(Owner 12 Nov, the 2f finding: urgent actions have the whole window, new quotes have 80% of it and always leave room for two requests; the router queues a risk-reducing request with priority
instead of refusing it, and skips a quote that does not fit its share.)

Nothing may exceed full capacity, not even a risk-reducing order: the venue would
answer with 429s or a ban. A request is admitted only if every bucket it touches
has room, and then every bucket is charged; a refusal charges none.

Windows are sliding, on the event clock (the request's own timestamp), so replay is
deterministic. A sliding window is never more permissive than a fixed one, which is
the safe side while the venues' window semantics are unconfirmed.

Assumptions (each throttle record names its bucket's DOCUMENTED / ASSUMED status):
- The per-action costs in ``ACTION_COSTS`` are ASSUMED unless marked.
- Bybit's IP limit is per host and shared by spot and linear, so IP buckets are keyed
  by (venue, host). Account buckets are keyed by (venue, product), endpoint buckets
  by (venue, product, endpoint).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

NEW_RISK = "NEW_RISK"
RISK_REDUCING = "RISK_REDUCING"
CANCEL = "CANCEL"
TIERS = (NEW_RISK, RISK_REDUCING, CANCEL)

SUBMIT, CANCEL_ACTION, REPLACE, QUERY = "SUBMIT", "CANCEL", "REPLACE", "QUERY"

# (venue, limit name) -> {action: cost}. A limit an action does not touch is absent.
# Binance counts new orders (and amends, assumed) in the account order count; cancels
# and queries spend IP weight only (query weight assumed 1 for futures, 4 for spot).
# Bybit charges every request to the IP limit and each endpoint to its own UID limit.
ACTION_COSTS: dict[tuple[str, str], dict[str, int]] = {
    ("BINANCE", "REQUEST_WEIGHT"): {SUBMIT: 1, CANCEL_ACTION: 1, REPLACE: 1, QUERY: 4},
    ("BINANCE", "ORDERS"): {SUBMIT: 1, REPLACE: 1},
    ("BYBIT", "HTTP"): {SUBMIT: 1, CANCEL_ACTION: 1, REPLACE: 1, QUERY: 1},
    ("BYBIT", "/v5/order/create"): {SUBMIT: 1},
    ("BYBIT", "/v5/order/cancel"): {CANCEL_ACTION: 1},
    ("BYBIT", "/v5/order/amend"): {REPLACE: 1},
}
QUERY_WEIGHT_FUTURES = {("BINANCE", "PERP"): 1}       # GET /fapi/v1/order (assumed)
HOSTS = {("BINANCE", "SPOT"): "api.binance.com", ("BINANCE", "PERP"): "fapi.binance.com",
         ("BYBIT", "SPOT"): "api.bybit.com", ("BYBIT", "PERP"): "api.bybit.com"}


@dataclass
class Bucket:
    key: tuple[str, ...]
    name: str
    scope: str
    limit: int
    interval_ns: int
    status: str                                  # DOCUMENTED | ASSUMED
    _spent: deque = field(default_factory=deque)  # (ts_ns, cost)
    used: int = 0
    peak_used: int = 0                            # the most the window ever held (owner 13 Nov: the report names the limits that bound; observation only, nothing reads it)

    def _expire(self, now_ns: int) -> None:
        while self._spent and self._spent[0][0] <= now_ns - self.interval_ns:
            self.used -= self._spent.popleft()[1]

    def room(self, now_ns: int, cost: int, share: float, min_free: int = 0) -> bool:
        self._expire(now_ns)
        return self.used + cost <= int(self.limit * share) and self.used + cost + min_free <= self.limit

    def next_free_ns(self, now_ns: int, cost: int, share: float, min_free: int = 0) -> int:
        """Earliest time ``cost`` fits within ``share`` of the limit (and leaves ``min_free`` requests free)."""
        self._expire(now_ns)
        allowed, used = min(int(self.limit * share), self.limit - min_free), self.used
        if used + cost <= allowed:
            return now_ns
        for ts, spent in self._spent:
            used -= spent
            if used + cost <= allowed:
                return ts + self.interval_ns
        return now_ns + self.interval_ns          # cost alone exceeds the share: next full window

    def charge(self, now_ns: int, cost: int) -> None:
        self._spent.append((now_ns, cost))
        self.used += cost
        if self.used > self.peak_used:
            self.peak_used = self.used


@dataclass(frozen=True)
class Decision:
    admitted: bool
    tier: str
    binding: Bucket | None = None                 # first bucket without room
    next_free_ns: int | None = None               # when every bucket has room


class ScopedRateLimiter:
    def __init__(self, limits: dict[tuple[str, str], Iterable], *, quote_share: float = 0.8, quote_min_free: int = 2) -> None:
        """Owner 12 Nov (the 2f finding): new quotes may use at most `quote_share` of each window and must leave at least `quote_min_free` requests free, so urgent actions always have room. Hedges, unwinds, flattens and cancels
        have the whole window (tiers RISK_REDUCING and CANCEL: share 1.0, nothing held back): the router never refuses them, it queues them with priority until the window allows (see StrategyDirectedRouter)."""
        if not 0 < quote_share <= 1 or quote_min_free < 0:
            raise ValueError("quote_share is a fraction in (0, 1] and quote_min_free is a count >= 0")
        self.shares = {NEW_RISK: quote_share, RISK_REDUCING: 1.0, CANCEL: 1.0}
        self.min_free = {NEW_RISK: quote_min_free, RISK_REDUCING: 0, CANCEL: 0}
        self.quote_share, self.quote_min_free = quote_share, quote_min_free
        self.buckets: dict[tuple[str, ...], Bucket] = {}
        self._by_route: dict[tuple[str, str], list[tuple[Bucket, dict[str, int]]]] = {}
        self.throttled: dict[str, int] = {"DOCUMENTED": 0, "ASSUMED": 0}
        self.cancels_delayed = 0
        self.over_limit_sent = 0
        for (venue, product), rows in limits.items():
            route = self._by_route.setdefault((venue, product), [])
            for row in rows:
                costs = ACTION_COSTS.get((venue, row.name))
                if not costs:
                    continue
                if row.scope == "IP":
                    key = ("IP", venue, HOSTS.get((venue, product), venue), row.name, str(row.interval_seconds))
                elif row.scope == "ACCOUNT":
                    key = ("ACCOUNT", venue, product, row.name, str(row.interval_seconds))
                else:
                    key = ("UID_ENDPOINT", venue, product, row.name, str(row.interval_seconds))
                bucket = self.buckets.setdefault(key, Bucket(
                    key, row.name, row.scope, int(row.limit), int(row.interval_seconds * 1_000_000_000), row.status))
                if (venue, product) in QUERY_WEIGHT_FUTURES and QUERY in costs:
                    costs = {**costs, QUERY: QUERY_WEIGHT_FUTURES[(venue, product)]}
                route.append((bucket, costs))

    def _touched(self, venue: str, product: str, action: str) -> list[tuple[Bucket, int]]:
        return [(bucket, costs[action]) for bucket, costs in self._by_route.get((venue, product), [])
                if costs.get(action)]

    def check(self, venue: str, product: str, action: str, tier: str, now_ns: int) -> Decision:
        share, min_free = self.shares[tier], self.min_free[tier]
        touched = self._touched(venue, product, action)
        binding = next((bucket for bucket, cost in touched if not bucket.room(now_ns, cost, share, min_free)), None)
        if binding is None:
            return Decision(True, tier)
        free = max(bucket.next_free_ns(now_ns, cost, share, min_free) for bucket, cost in touched)
        return Decision(False, tier, binding, free)

    def charge(self, venue: str, product: str, action: str, now_ns: int) -> None:
        for bucket, cost in self._touched(venue, product, action):
            bucket.charge(now_ns, cost)

    def note_throttle(self, decision: Decision) -> None:
        if decision.binding is not None:
            self.throttled[decision.binding.status] = self.throttled.get(decision.binding.status, 0) + 1

    def snapshot(self, now_ns: int | None = None) -> dict:
        rows = []
        for key, bucket in sorted(self.buckets.items()):
            if now_ns is not None:
                bucket._expire(now_ns)
            rows.append({"key": "|".join(key), "scope": bucket.scope, "name": bucket.name, "limit": bucket.limit,
                         "interval_ns": bucket.interval_ns, "used": bucket.used, "status": bucket.status,
                         "utilization": bucket.used / bucket.limit if bucket.limit else 1.0,
                         "peak_used": bucket.peak_used, "peak_utilization": bucket.peak_used / bucket.limit if bucket.limit else 1.0})
        return {"quote_share": self.quote_share, "quote_min_free": self.quote_min_free,
                "buckets": rows, "throttled_by_status": dict(self.throttled),
                "cancels_delayed": self.cancels_delayed, "over_limit_sent": self.over_limit_sent,
                "max_utilization": max((row["utilization"] for row in rows), default=0.0)}


class OrderRateBucket:
    """Owner 16 Nov (limit A1): new maker orders and amends of one venue, at most `per_s` per second sustained, bursts up to `burst` (a token bucket). A request that finds no whole token is SKIPPED: nothing is queued
    and nothing is retried here. Hedges, unwinds, flattens and cancels never pass through it. It is the strict per-venue cap on top of the window limiter above (which can queue); both apply.
    Refills on the request's own timestamp (the event clock), so a replay is deterministic. Tokens are floats of seconds' worth; the arithmetic is exact enough that a run of requests spaced 1/per_s apart
    is always admitted."""

    __slots__ = ("per_s", "burst", "tokens", "last_ns", "taken", "skipped")

    def __init__(self, per_s: float, burst: int) -> None:
        if per_s <= 0 or burst < 1:
            raise ValueError("order rate needs per_s > 0 and burst >= 1")
        self.per_s, self.burst = float(per_s), int(burst)
        self.tokens, self.last_ns = float(burst), None
        self.taken = self.skipped = 0

    def _refill(self, now_ns: int) -> None:
        if self.last_ns is not None and now_ns > self.last_ns:
            self.tokens = min(float(self.burst), self.tokens + (now_ns - self.last_ns) / 1e9 * self.per_s)
        if self.last_ns is None or now_ns > self.last_ns:
            self.last_ns = now_ns

    def has_room(self, now_ns: int) -> bool:
        self._refill(now_ns)
        return self.tokens >= 1.0 - 1e-9

    def take(self, now_ns: int) -> bool:
        """One token for one new order or amend; False (and a counted skip) when there is none."""
        if not self.has_room(now_ns):
            self.skipped += 1
            return False
        self.tokens = max(0.0, self.tokens - 1.0)
        self.taken += 1
        return True

    def snapshot(self) -> dict:
        return {"per_s": self.per_s, "burst": self.burst, "taken": self.taken, "skipped": self.skipped, "tokens_now": round(self.tokens, 6)}
