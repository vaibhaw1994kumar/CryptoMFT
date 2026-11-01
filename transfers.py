"""Inter-venue transfers and rebalancing (ECO05).

Without ``risk.transfers`` the platform runs in no-rebalancing mode: each
venue trades only its own pre-funded balances (``PRE_FUNDED_INVENTORY_NO_
REBALANCING``). With it, transfers follow a lifecycle and are never
instantaneous:

* **Initiation** checks the source's free balance (fee included), then books
  ``TRANSFER`` (source main -q, source ``in_transit`` +q) and ``TRANSFER_FEE``
  (source main -fee, a cost). The funds leave the source's spendable balance
  at once and are not yet usable at the destination.
* **Confirmation** at initiation + the route's delay (``delay_ms`` > 0,
  enforced at load): ``TRANSFER`` (source in_transit -q, destination main +q).
* **Failure** (from the route's scripted ``outcomes``, labelled SIMULATED, or
  a venue status): ``TRANSFER`` back to the source's main, and a fee refund
  only when ``fee_refunded_on_failure``.
* **Horizon.** Nothing is confirmed past the last market event; a transfer
  still pending at the end is reported as in transit (unavailable).
* **Accounting.** The principal is an internal flow: the reconciliation keeps
  it out of each venue's strategy PnL (like deposits) and shows the
  cross-venue price difference as transfer valuation. The fee is a cost
  (``transfer_fees``), excluded from the plan-PnL comparison.
* **Rebalancer** (opt-in, ``rebalance.enabled``). When a venue's free balance
  of an asset falls below ``low_watermark`` it requests ``target - free`` from
  the venue with the most free balance. The source never goes below its own
  low watermark, one transfer per route is in flight at a time, and a
  cooldown applies between initiations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Iterable

from .economics import EconomicEvent, EconomicEventType, EventSource, Posting

if TYPE_CHECKING:
    from .ledger import Ledger

ZERO = Decimal("0")
IN_TRANSIT = "in_transit"


@dataclass(frozen=True, slots=True)
class TransferRoute:
    source: str
    destination: str
    asset: str
    delay_ns: int
    fee: Decimal = ZERO                       # in the transferred asset
    min_quantity: Decimal = ZERO
    fee_refunded_on_failure: bool = False
    outcomes: tuple[str, ...] = ("CONFIRMED",)  # scripted, cycled; SIMULATED
    source_label: EventSource = EventSource.ASSUMED

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.source, self.destination, self.asset)

    @classmethod
    def from_config(cls, row: dict[str, Any]) -> "TransferRoute":
        delay_ms = int(row.get("delay_ms", 0))
        if delay_ms <= 0:
            raise ValueError(f"transfer route {row.get('from')}->{row.get('to')}: delay_ms must be positive "
                             "(transfers are never instantaneous)")
        outcomes = tuple(str(o).upper() for o in row.get("outcomes", ["CONFIRMED"]))
        if not outcomes or any(o not in ("CONFIRMED", "FAILED") for o in outcomes):
            raise ValueError("transfer outcomes must be CONFIRMED or FAILED")
        if row.get("from") == row.get("to"):
            raise ValueError("a transfer route needs two different venues")
        return cls(
            source=str(row["from"]), destination=str(row["to"]), asset=str(row["asset"]),
            delay_ns=delay_ms * 1_000_000, fee=Decimal(str(row.get("fee", "0"))),
            min_quantity=Decimal(str(row.get("min_quantity", "0"))),
            fee_refunded_on_failure=bool(row.get("fee_refunded_on_failure", False)),
            outcomes=outcomes, source_label=EventSource(str(row.get("source", "ASSUMED")).upper()),
        )


@dataclass(slots=True)
class PendingTransfer:
    transfer_id: str
    route: TransferRoute
    quantity: Decimal
    initiated_ns: int
    confirm_at_ns: int
    outcome: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RebalanceRule:
    asset: str
    low_watermark: Decimal
    target: Decimal


class TransferEngine:
    def __init__(
        self, ledger: "Ledger", routes: Iterable[TransferRoute], *,
        free_balance: Callable[[str, str], Decimal],
        rebalance: Iterable[RebalanceRule] = (), rebalance_enabled: bool = False,
        cooldown_ns: int = 60_000_000_000, price: Callable[[str, str], Decimal | None] | None = None,
        reporting_currency: str = "USDT",
    ) -> None:
        self.ledger = ledger
        self.routes = {route.key: route for route in routes}
        self.free_balance = free_balance
        self.rules = {rule.asset: rule for rule in rebalance}
        self.rebalance_enabled = rebalance_enabled and bool(self.rules)
        self.cooldown_ns = cooldown_ns
        self.price = price or (lambda venue, asset: None)
        self.reporting_currency = reporting_currency
        self.pending: dict[str, PendingTransfer] = {}
        self.completed: list[dict[str, Any]] = []
        self.flags: list[dict[str, Any]] = []
        self.now_ns = 0
        self._sequence = 0
        self._outcome_index: dict[tuple[str, str, str], int] = {}
        self._last_initiated: dict[tuple[str, str, str], int] = {}

    @property
    def configured(self) -> bool:
        return bool(self.routes)

    # -- balances -----------------------------------------------------------------------
    def in_transit_out(self, venue: str, asset: str) -> Decimal:
        return sum((p.quantity for p in self.pending.values()
                    if p.route.source == venue and p.route.asset == asset), ZERO)

    def in_transit_in(self, venue: str, asset: str) -> Decimal:
        return sum((p.quantity for p in self.pending.values()
                    if p.route.destination == venue and p.route.asset == asset), ZERO)

    # -- lifecycle ------------------------------------------------------------------------
    def initiate(self, source: str, destination: str, asset: str, quantity: Decimal, now_ns: int,
                 reason: str = "") -> PendingTransfer:
        route = self.routes.get((source, destination, asset))
        if route is None:
            raise ValueError(f"no transfer route {source}->{destination} for {asset}")
        if quantity <= 0 or quantity < route.min_quantity:
            raise ValueError(f"transfer quantity {quantity} below the route minimum {route.min_quantity}")
        free = self.free_balance(source, asset)
        if quantity + route.fee > free:
            raise ValueError(f"insufficient free {asset} on {source} for transfer: need {quantity + route.fee}, free {free}")
        self._sequence += 1
        transfer_id = f"{source}->{destination}:{asset}:{now_ns}:{self._sequence}"
        index = self._outcome_index.get(route.key, 0)
        outcome = route.outcomes[index % len(route.outcomes)]
        self._outcome_index[route.key] = index + 1
        pending = PendingTransfer(transfer_id, route, quantity, now_ns, now_ns + route.delay_ns, outcome, reason)
        details = (("asset", asset), ("source", source), ("destination", destination),
                   ("quantity", str(quantity)), ("stage", "INITIATED"),
                   ("confirm_at_ns", str(pending.confirm_at_ns)), ("scripted_outcome", outcome),
                   ("reason", reason))
        self.ledger.apply_transfer(EconomicEvent(
            event_type=EconomicEventType.TRANSFER, idempotency_key=f"transfer-init|{transfer_id}",
            ts_ns=now_ns, effective_ts_ns=now_ns,
            postings=(Posting(source, "main", asset, -quantity), Posting(source, IN_TRANSIT, asset, quantity)),
            venue=source, source=route.source_label, reference_id=transfer_id, details=details,
        ))
        if route.fee:
            price = self.price(source, asset) if asset != self.reporting_currency else Decimal("1")
            self.ledger.apply_transfer(EconomicEvent(
                event_type=EconomicEventType.TRANSFER_FEE, idempotency_key=f"transfer-fee|{transfer_id}",
                ts_ns=now_ns, effective_ts_ns=now_ns,
                postings=(Posting(source, "main", asset, -route.fee),),
                venue=source, source=route.source_label, reference_id=transfer_id,
                reporting_value=None if price is None else -route.fee * price,
                valuation_price=price, valuation_source="book mid at initiation" if price is not None else "",
                details=(("asset", asset), ("fee", str(route.fee)), ("stage", "FEE")),
            ))
            if price is None:
                self._flag(transfer_id, "TRANSFER_FEE_UNPRICED", f"no {asset} price on {source} to value the fee")
        self.pending[transfer_id] = pending
        self._last_initiated[route.key] = now_ns
        return pending

    def process(self, now_ns: int) -> None:
        """Confirm or fail transfers due by ``now_ns`` (a market-data time), then rebalance."""
        self.now_ns = max(self.now_ns, now_ns)
        for transfer_id, pending in sorted(self.pending.items(), key=lambda kv: kv[1].confirm_at_ns):
            if pending.confirm_at_ns > now_ns:
                continue
            self._complete(pending, pending.outcome)
        if self.rebalance_enabled:
            self._rebalance(now_ns)

    def _complete(self, pending: PendingTransfer, outcome: str) -> None:
        route, q = pending.route, pending.quantity
        to_venue = route.destination if outcome == "CONFIRMED" else route.source
        self.ledger.apply_transfer(EconomicEvent(
            event_type=EconomicEventType.TRANSFER,
            idempotency_key=f"transfer-{outcome.lower()}|{pending.transfer_id}",
            ts_ns=pending.confirm_at_ns, effective_ts_ns=pending.confirm_at_ns,
            postings=(Posting(route.source, IN_TRANSIT, route.asset, -q), Posting(to_venue, "main", route.asset, q)),
            venue=to_venue, source=EventSource.SIMULATED, reference_id=pending.transfer_id,
            details=(("asset", route.asset), ("source", route.source), ("destination", route.destination),
                     ("quantity", str(q)), ("stage", outcome)),
        ))
        if outcome == "FAILED" and route.fee and route.fee_refunded_on_failure:
            price = self.price(route.source, route.asset) if route.asset != self.reporting_currency else Decimal("1")
            self.ledger.apply_transfer(EconomicEvent(
                event_type=EconomicEventType.TRANSFER_FEE,
                idempotency_key=f"transfer-fee-refund|{pending.transfer_id}",
                ts_ns=pending.confirm_at_ns, effective_ts_ns=pending.confirm_at_ns,
                postings=(Posting(route.source, "main", route.asset, route.fee),),
                venue=route.source, source=EventSource.SIMULATED, reference_id=pending.transfer_id,
                reporting_value=None if price is None else route.fee * price, valuation_price=price,
                details=(("asset", route.asset), ("fee", str(route.fee)), ("stage", "FEE_REFUND")),
            ))
        if outcome == "FAILED":
            self._flag(pending.transfer_id, "TRANSFER_FAILED", "principal returned to the source")
        del self.pending[pending.transfer_id]
        self.completed.append({"transfer_id": pending.transfer_id, "route": f"{route.source}->{route.destination}",
                               "asset": route.asset, "quantity": str(q), "outcome": outcome,
                               "initiated_ns": pending.initiated_ns, "completed_ns": pending.confirm_at_ns,
                               "reason": pending.reason})

    def _rebalance(self, now_ns: int) -> None:
        for asset, rule in sorted(self.rules.items()):
            venues = sorted({v for r in self.routes.values() if r.asset == asset for v in (r.source, r.destination)})
            for destination in venues:
                free = self.free_balance(destination, asset) + self.in_transit_in(destination, asset)
                if free >= rule.low_watermark:
                    continue
                options = []
                for route in self.routes.values():
                    if route.destination != destination or route.asset != asset:
                        continue
                    if any(p.route.key == route.key for p in self.pending.values()):
                        continue                                 # one in flight per route
                    last = self._last_initiated.get(route.key)
                    if last is not None and now_ns - last < self.cooldown_ns:
                        continue
                    spare = self.free_balance(route.source, asset) - route.fee - rule.low_watermark
                    amount = min(rule.target - free, spare)
                    if amount > 0 and amount >= route.min_quantity:
                        options.append((spare, route, amount))
                if options:
                    _, route, amount = max(options, key=lambda item: item[0])
                    self.initiate(route.source, destination, asset, amount, now_ns,
                                  reason=f"rebalance: {destination} free {free} < low watermark {rule.low_watermark}")

    def _flag(self, transfer_id: str, code: str, detail: str) -> None:
        self.flags.append({"transfer_id": transfer_id, "code": code, "detail": detail, "ts_ns": self.now_ns})

    # -- restart --------------------------------------------------------------------------
    def restore(self, events: list[EconomicEvent]) -> None:
        """Pending transfers and the outcome script position, from the (journaled) ledger."""
        started: dict[str, EconomicEvent] = {}
        finished: set[str] = set()
        for event in events:
            if event.event_type != EconomicEventType.TRANSFER:
                continue
            stage = dict(event.details).get("stage")
            if stage == "INITIATED":
                started[event.reference_id] = event
            elif stage in ("CONFIRMED", "FAILED"):
                finished.add(event.reference_id)
        for transfer_id, event in started.items():
            details = dict(event.details)
            key = (details["source"], details["destination"], details["asset"])
            route = self.routes.get(key)
            if route is None:
                continue
            self._outcome_index[key] = self._outcome_index.get(key, 0) + 1
            self._last_initiated[key] = max(self._last_initiated.get(key, 0), event.effective)
            self._sequence += 1
            if transfer_id in finished:
                continue
            self.pending[transfer_id] = PendingTransfer(
                transfer_id, route, Decimal(details["quantity"]), event.effective,
                int(details["confirm_at_ns"]), details["scripted_outcome"], details.get("reason", ""),
            )

    # -- reporting ------------------------------------------------------------------------
    def report(self) -> dict[str, Any]:
        if not self.routes:
            return {"status": "NO_REBALANCING",
                    "detail": "no transfer routes: each venue trades only its own pre-funded balances"}
        return {
            "status": "MODELLED", "rebalancing": "ENABLED" if self.rebalance_enabled else "DISABLED",
            "routes": [{"from": r.source, "to": r.destination, "asset": r.asset, "delay_ns": r.delay_ns,
                        "fee": str(r.fee), "outcomes": list(r.outcomes), "terms_source": r.source_label.value}
                       for r in self.routes.values()],
            "pending": [{"transfer_id": p.transfer_id, "quantity": str(p.quantity), "asset": p.route.asset,
                         "from": p.route.source, "to": p.route.destination, "confirm_at_ns": p.confirm_at_ns}
                        for p in self.pending.values()],
            "completed": self.completed, "flags": self.flags,
            "horizon_ns": self.now_ns,
        }
