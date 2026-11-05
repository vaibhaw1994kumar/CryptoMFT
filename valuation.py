"""Common-timestamp valuation (review item R11, docs/strategy_contract.md section 4).

The ledger's open position is worth Σ base_v × mark_v. Marking each venue at ITS OWN last published mid overstates a hedged pair
whenever one venue's feed stops before the other's: the long leg carries the live price, the short leg a price from hours
earlier, and the difference is booked as equity although nothing can be realised at it (SOL 2022-01-05: +$17.16 of "open
position value" on a flat pair). The common-timestamp valuation marks every venue at the last time ALL venues had data:
common_ts = min over venues of the last update time, and each venue's mark is its mid at or before common_ts.

MidHistory keeps the marks needed for that lookup with bounded memory: the latest mark exactly, earlier marks sampled at
>= sample_s spacing, and nothing older than the last sample at or before the other venues' last update (prune). A venue that
keeps publishing for N seconds after the other has gone quiet costs O(N / sample_s) entries, never O(events)."""
from __future__ import annotations

from bisect import bisect_right

PRUNE_BATCH = 4096


class MidHistory:
    def __init__(self, sample_s: float = 1.0):
        if not (sample_s > 0.0):
            raise ValueError("sample_s must be > 0")
        self.sample_s = sample_s
        self._ts: list[float] = []
        self._mid: list[float] = []
        self.latest: tuple[float, float] | None = None
        self.recorded = 0

    def record(self, ts: float, mid: float) -> None:
        if not (mid > 0.0):
            return
        if self.latest is not None and ts < self.latest[0]:
            return                                       # out-of-order stamp: never let it become "the latest"
        self.recorded += 1
        self.latest = (ts, mid)
        if not self._ts or ts - self._ts[-1] >= self.sample_s:
            self._ts.append(ts)
            self._mid.append(mid)

    def prune(self, floor_ts: float) -> None:
        """Drop samples older than the last one at or before floor_ts (batched so the list is not shifted per event)."""
        i = bisect_right(self._ts, floor_ts) - 1
        if i >= PRUNE_BATCH:
            del self._ts[:i]
            del self._mid[:i]

    def at_or_before(self, t: float) -> tuple[float, float] | None:
        if self.latest is not None and self.latest[0] <= t:
            return self.latest
        i = bisect_right(self._ts, t) - 1
        return (self._ts[i], self._mid[i]) if i >= 0 else None

    def __len__(self) -> int:
        return len(self._ts)


def common_timestamp_valuation(hist: dict[str, MidHistory], base: dict[str, float], total_cash: float, venues,
                               *, max_staleness_s: float, mark_venue: str | None = None) -> dict:
    """Marks every venue at common_ts = min(last update time over venues). Never falls back silently: when a venue has no price
    or no mark at or before common_ts the result says available=False and the caller decides (and flags) what to do. A lagging
    venue (last updates further apart than max_staleness_s) is FLAGGED, not rejected: the common-timestamp mark is still the only
    valuation at which both legs have a price."""
    latest = {v: hist[v].latest for v in venues}
    if any(x is None for x in latest.values()):
        return {"available": False, "reason": "no_price_on_venue", "venues_without_price": [v for v, x in latest.items() if x is None]}
    common_ts = min(x[0] for x in latest.values())
    spread_s = max(x[0] for x in latest.values()) - common_ts
    marks: dict[str, float] = {}
    mark_ts: dict[str, float] = {}
    for v in venues:
        e = hist[v].at_or_before(common_ts)
        if e is None:
            return {"available": False, "reason": "history_too_shallow", "venue": v, "common_ts": common_ts}
        mark_ts[v], marks[v] = e
    if mark_venue is not None and mark_venue in marks:
        marks = {v: marks[mark_venue] for v in venues}
    open_pos = sum(base.get(v, 0.0) * marks[v] for v in venues)
    vs = list(venues)
    basis = (marks[vs[0]] - marks[vs[1]]) if len(vs) == 2 else None
    return {"available": True, "common_ts": common_ts, "marks": marks, "mark_ts": mark_ts,
            "mark_gap_s": {v: common_ts - mark_ts[v] for v in venues},            # how far before common_ts each mark was published
            "venue_last_update_spread_s": spread_s, "lagging_venue_flag": spread_s > max_staleness_s,
            "last_update_ts": {v: x[0] for v, x in latest.items()},
            "open_position_value": open_pos, "equity": total_cash + open_pos,
            "cross_venue_basis_at_common_ts": basis, "base": {v: base.get(v, 0.0) for v in venues}}
