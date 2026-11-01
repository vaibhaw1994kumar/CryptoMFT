"""Order-event and fill records with a required provenance label.

Every record the backtester emits about an order says where it came from:

    "historical"  came from real logs (real orders and executions replayed through the model, e.g. the PostOnly false-reject
                  check on bybit_30d.zip). Nothing the strategy backtest itself generates is historical: market data is an
                  input, not an order event.
    "modelled"    derived from market data plus assumptions: simulated fills, queue positions, PostOnly rejects, latencies
                  drawn from a calibrated or preset distribution, strategy decisions (sends, cancels on expiry).
    "injected"    produced by the fault-injection layer (hedge_faults.py): a forced reject, an added delay, a partial fill, an
                  outage, a lost acknowledgement.

Consequences of an injected fault are NOT labelled "injected". The fault itself is; whatever the strategy does about it
(a retry, an unwind, a late hedge) and whatever those orders fill at is "modelled" and carries `chain_id`, the id of the
fault that caused it, so a summary can split "P&L of fault chains" from the rest without calling a market-priced fill injected.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass

SOURCES = ("historical", "modelled", "injected")


def check_source(source) -> str:
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}, got {source!r}")
    return source


@dataclass(frozen=True)
class OrderEvent:
    """`source` has no default on purpose: an event cannot be built without saying where it came from."""
    ts: float
    kind: str
    venue: str
    source: str
    order_id: int = 0
    signal_id: int = 0
    chain_id: int = 0          # the injected fault this event is a consequence of (0 = none)
    side: str = ""
    qty: float = 0.0
    price: float = 0.0
    reason: str = ""

    def __post_init__(self) -> None:
        check_source(self.source)


class OrderEventLog:
    """Counts every event by (kind, source); keeps the events themselves only when asked (they number in the millions on a full day)."""

    def __init__(self, keep: bool = False, cap: int = 2_000_000) -> None:
        self.keep = keep
        self.cap = cap
        self.counts: Counter[tuple[str, str]] = Counter()
        self.events: list[OrderEvent] = []
        self.dropped = 0

    def emit(self, ts: float, kind: str, venue: str, *, source: str, **fields) -> None:
        ev = OrderEvent(ts=ts, kind=kind, venue=venue, source=source, **fields)
        self.counts[(kind, source)] += 1
        if self.keep:
            if len(self.events) < self.cap:
                self.events.append(ev)
            else:
                self.dropped += 1

    def by_source(self) -> dict[str, int]:
        out = {s: 0 for s in SOURCES}
        for (_, src), n in self.counts.items():
            out[src] += n
        return out

    def summary(self) -> dict:
        kinds: dict[str, dict[str, int]] = {}
        for (kind, src), n in sorted(self.counts.items()):
            kinds.setdefault(kind, {})[src] = n
        return {"by_source": self.by_source(), "by_kind": kinds, "events_kept": len(self.events), "events_dropped_over_cap": self.dropped}

    def as_rows(self) -> list[dict]:
        return [asdict(e) for e in self.events]


def require_source(rows, what: str = "row") -> None:
    """Raise if any row lacks a valid `source` (used by writers and tests)."""
    for i, r in enumerate(rows):
        if "source" not in r:
            raise ValueError(f"{what} {i} has no 'source' field")
        check_source(r["source"])
