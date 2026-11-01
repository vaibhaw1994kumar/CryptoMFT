"""Spot borrowing for short sales (ECO03): availability, accrual, repayment, recall.

Borrowing is an explicit add-on to pre-funded inventory, never inferred from a
``margin_enabled`` flag. Two layers decide whether an asset can be borrowed:

1. **Instrument:** the spot instrument must be margin-eligible
   (``borrow_applicable`` in config, from the contract master).
2. **Account:** a configured borrow line for ``(venue, asset)`` must be
   authorized, have limit left and not be under recall.

Each refusal has its own reason: ``NOT_ELIGIBLE``, ``NOT_AUTHORIZED``,
``RECALLED`` or ``LIMIT_EXHAUSTED``.

Ledger model (see economics.py): principal is a liability in account
``loan``, unpaid interest in ``loan_interest``, both negative balances in the
borrowed asset. ``main`` only holds what the account owns.

* ``BORROW_OPEN``: main +q, loan -q. Opened automatically when a spot sell
  fill would take ``main`` negative, effective at the fill's match time.
* ``BORROW_ACCRUAL``: loan_interest -interest, charged in the borrowed asset.
  The convention is per line and labelled ``ASSUMED`` unless verified:
  interval (default 1h), ``charge_on_open`` (one interval charged when the
  loan opens, Binance-style), basis ``PRINCIPAL`` (default) or
  ``PRINCIPAL_PLUS_INTEREST``, and the rate in effect at each charge time.
  Boundary charges use the as-of balance (changes effective strictly before
  the boundary count); a late borrow or repay rebooks them.
* ``BORROW_REPAY``: main -q, interest first, then principal. Automatic on a
  buy fill (``repay_policy: auto_on_buy``, the default) or from an explicit
  repay record. A duplicate repay id is ignored; an over-repayment is refused.
* ``BORROW_RECALL``: the venue recalls a loan (no postings). New shorts on the
  line are blocked at once and stay blocked unless a later limit change
  reopens it. At the deadline the loan is repaid from ``main``; any
  shortfall is bought in by the venue.
* ``BORROW_FORCED_CLOSE``: the buy-in fill (a fill outside every plan, priced
  from the recall record (OBSERVED) or the best ask (SIMULATED)), followed by
  the repayment. The kill switch is activated and the now-unhedged position
  is reported.

A spot balance going negative on ``main`` without an authorized line is an
``UNFINANCED_SHORT``: a failing reconciliation check under pre-funded
inventory, an informational note under the legacy unconstrained model.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Iterable

from .economics import EconomicEvent, EconomicEventType, EventSource, Posting
from .models import ChildOrder, OrderState, Side
from .periodic import PeriodicSettlement

if TYPE_CHECKING:
    from .ledger import Ledger

ZERO = Decimal("0")
HOUR_NS = 3_600_000_000_000
LOAN, INTEREST, MAIN = "loan", "loan_interest", "main"


class BorrowUnavailable(ValueError):
    def __init__(self, venue: str, asset: str, code: str, detail: str = "") -> None:
        super().__init__(f"borrow unavailable: {venue} {asset}: {code}" + (f" ({detail})" if detail else ""))
        self.code = code


@dataclass(frozen=True, slots=True)
class BorrowLine:
    venue: str
    asset: str
    authorized: bool
    limit: Decimal
    rate_per_interval: Decimal
    interval_ns: int = HOUR_NS
    anchor_ns: int = 0
    charge_on_open: bool = True
    basis: str = "PRINCIPAL"                      # or PRINCIPAL_PLUS_INTEREST
    rate_changes: tuple[tuple[int, Decimal], ...] = ()   # (effective_ns, rate per interval)
    limit_changes: tuple[tuple[int, Decimal], ...] = ()  # (effective_ns, limit)
    source: EventSource = EventSource.ASSUMED

    def rate_at(self, ts_ns: int) -> Decimal:
        rate = self.rate_per_interval
        for effective, value in sorted(self.rate_changes):
            if effective <= ts_ns:
                rate = value
        return rate

    @classmethod
    def from_config(cls, row: dict[str, Any]) -> "BorrowLine":
        """``risk.borrow.lines[]``; rates as ``rate_per_interval`` or ``daily_rate``."""
        from .fees import parse_effective_ns

        interval_ns = int(Decimal(str(row.get("interval_hours", 1))) * HOUR_NS)
        if interval_ns <= 0:
            raise ValueError(f"borrow line {row.get('venue')} {row.get('asset')}: interval must be positive")

        def per_interval(item: dict[str, Any]) -> Decimal:
            if item.get("rate_per_interval") is not None:
                return Decimal(str(item["rate_per_interval"]))
            if item.get("daily_rate") is not None:
                return Decimal(str(item["daily_rate"])) * interval_ns / (24 * HOUR_NS)
            raise ValueError(f"borrow line {row.get('venue')} {row.get('asset')}: needs rate_per_interval or daily_rate")

        basis = str(row.get("basis", "PRINCIPAL")).upper()
        if basis not in ("PRINCIPAL", "PRINCIPAL_PLUS_INTEREST"):
            raise ValueError("borrow basis must be PRINCIPAL or PRINCIPAL_PLUS_INTEREST")
        return cls(
            venue=str(row["venue"]), asset=str(row["asset"]),
            authorized=bool(row.get("authorized", False)), limit=Decimal(str(row.get("limit", "0"))),
            rate_per_interval=per_interval(row), interval_ns=interval_ns,
            anchor_ns=parse_effective_ns(row.get("anchor")),
            charge_on_open=bool(row.get("charge_on_open", True)), basis=basis,
            rate_changes=tuple(
                (parse_effective_ns(c["effective_from"]), per_interval(c)) for c in row.get("rate_changes", [])
            ),
            limit_changes=tuple(
                (parse_effective_ns(c["effective_from"]), Decimal(str(c["limit"])))
                for c in row.get("limit_changes", [])
            ),
            source=EventSource(str(row.get("source", "ASSUMED")).upper()),
        )

    def limit_at(self, ts_ns: int) -> Decimal:
        limit = self.limit
        for effective, value in sorted(self.limit_changes):
            if effective <= ts_ns:
                limit = value
        return limit


@dataclass(frozen=True, slots=True)
class BorrowRecall:
    recall_id: str
    venue: str
    asset: str
    effective_ns: int
    deadline_ns: int
    quantity: Decimal | None = None          # None = the whole loan
    forced_close_price: Decimal | None = None
    source: EventSource = EventSource.OBSERVED

    @classmethod
    def from_config(cls, row: dict[str, Any]) -> "BorrowRecall":
        from .fees import parse_effective_ns

        effective = parse_effective_ns(row["effective_from"])
        deadline = (
            parse_effective_ns(row["deadline"]) if row.get("deadline") is not None
            else effective + int(row.get("deadline_ms", 0)) * 1_000_000
        )
        price = row.get("forced_close_price")
        quantity = row.get("quantity")
        return cls(
            recall_id=str(row["recall_id"]), venue=str(row["venue"]), asset=str(row["asset"]),
            effective_ns=effective, deadline_ns=deadline,
            quantity=None if quantity in (None, "ALL") else Decimal(str(quantity)),
            forced_close_price=None if price is None else Decimal(str(price)),
            source=EventSource(str(row.get("source", "OBSERVED")).upper()),
        )


@dataclass(slots=True)
class _RecallState:
    recall: BorrowRecall
    announced: bool = False
    resolved: bool = False
    price_missing_flagged: bool = False


class BorrowEngine(PeriodicSettlement):
    def __init__(
        self, ledger: "Ledger", lines: Iterable[BorrowLine], *,
        spot_instruments: dict[tuple[str, str], tuple[str, str]],   # (venue, instrument) -> (base, quote)
        eligible: dict[tuple[str, str], bool] | None = None,        # (venue, asset) -> instrument flag
        recalls: Iterable[BorrowRecall] = (), repay_policy: str = "auto_on_buy",
        forced_close_fee_bps: Decimal = ZERO, settle_lag_ns: int = 5_000_000_000,
        on_forced_close: Callable[[str, int], None] | None = None,
        unfinanced_short_policy: str = "FAIL", reporting_currency: str = "USDT",
    ) -> None:
        super().__init__(settle_lag_ns=settle_lag_ns)
        if repay_policy not in ("auto_on_buy", "manual"):
            raise ValueError("borrow repay_policy must be auto_on_buy or manual")
        if unfinanced_short_policy not in ("FAIL", "NOTE"):
            raise ValueError("unfinanced_short_policy must be FAIL or NOTE")
        self.ledger = ledger
        self.lines = {(line.venue, line.asset): line for line in lines}
        self.spot_instruments = dict(spot_instruments)
        self.eligible = dict(eligible or {})
        self.repay_policy = repay_policy
        self.forced_close_fee_bps = forced_close_fee_bps
        self.on_forced_close = on_forced_close
        self.unfinanced_short_policy = unfinanced_short_policy
        self.reporting_currency = reporting_currency
        self.flags: list[dict[str, Any]] = []
        self.notes: list[dict[str, Any]] = []
        self.now_ns = 0
        self._recalls = [_RecallState(r) for r in sorted(recalls, key=lambda r: r.effective_ns)]
        self._prices: dict[tuple[str, str], tuple[Decimal | None, Decimal | None]] = {}
        self._repaid: dict[tuple[str, str], Decimal] = {}
        self._repay_ids: set[str] = set()
        self._forced = False
        self.forced_closes: list[dict[str, Any]] = []
        ledger.trade_fill_listeners.append(self.on_trade_fill)

    @property
    def configured(self) -> bool:
        return bool(self.lines)

    # -- availability ------------------------------------------------------------------
    def principal(self, venue: str, asset: str, as_of_ns: int | None = None) -> Decimal:
        return -self.ledger.economics.balance(venue, asset, LOAN, as_of_ns)

    def interest_due(self, venue: str, asset: str, as_of_ns: int | None = None) -> Decimal:
        return -self.ledger.economics.balance(venue, asset, INTEREST, as_of_ns)

    def _recalled(self, venue: str, asset: str) -> bool:
        # A recalled line stays closed to new borrowing for the rest of the
        # session; reopening it needs new terms (e.g. a later limit change).
        return any(
            s.announced and (s.recall.venue, s.recall.asset) == (venue, asset)
            and not any(eff >= s.recall.effective_ns for eff, _ in self.lines[(venue, asset)].limit_changes)
            for s in self._recalls if (venue, asset) in self.lines
        )

    def availability(self, venue: str, asset: str, reserved: Decimal = ZERO) -> tuple[Decimal, str]:
        """Borrowable quantity now and the reason when it is zero."""
        if not self.eligible.get((venue, asset), False):
            return ZERO, "NOT_ELIGIBLE"
        line = self.lines.get((venue, asset))
        if line is None or not line.authorized:
            return ZERO, "NOT_AUTHORIZED"
        if self._recalled(venue, asset):
            return ZERO, "RECALLED"
        left = line.limit_at(self.now_ns) - self.principal(venue, asset) - reserved
        if left <= 0:
            return ZERO, "LIMIT_EXHAUSTED"
        return left, "OK"

    def require(self, venue: str, asset: str, quantity: Decimal, reserved: Decimal = ZERO) -> None:
        available, code = self.availability(venue, asset, reserved)
        if code != "OK":
            raise BorrowUnavailable(venue, asset, code)
        if quantity > available:
            raise BorrowUnavailable(venue, asset, "LIMIT_EXHAUSTED", f"need {quantity}, {available} left")

    # -- market inputs -------------------------------------------------------------------
    def observe(self, venue: str, instrument_id: str, ts_ns: int, mid: Decimal | None,
                best_ask: Decimal | None = None) -> None:
        self.begin(ts_ns)
        self.now_ns = max(self.now_ns, ts_ns)
        assets = self.spot_instruments.get((venue, instrument_id))
        if assets is not None and assets[1] == self.reporting_currency:
            self._prices[(venue, assets[0])] = (mid, best_ask)

    def _price(self, venue: str, asset: str) -> Decimal | None:
        if asset == self.reporting_currency:
            return Decimal("1")
        return self._prices.get((venue, asset), (None, None))[0]

    # -- booking -------------------------------------------------------------------------
    def _accrual(self, line: BorrowLine, ts_ns: int, base: Decimal, key: str, booked_ns: int,
                 kind: str, revision: int | None = None) -> EconomicEvent | None:
        rate = line.rate_at(ts_ns)
        interest = base * rate
        if interest == 0:
            return None
        price = self._price(line.venue, line.asset)
        if price is None:
            self._flag(line, ts_ns, "BORROW_PRICE_MISSING", "interest booked in the asset; no price to value it")
        return EconomicEvent(
            event_type=EconomicEventType.BORROW_ACCRUAL, idempotency_key=key,
            # Stamped at the charge time so a re-run after a restart is an identical duplicate.
            ts_ns=ts_ns, effective_ts_ns=ts_ns,
            postings=(Posting(line.venue, INTEREST, line.asset, -interest),),
            venue=line.venue, source=line.source,
            reporting_value=None if price is None else -interest * price,
            valuation_price=price, valuation_source="book mid at accrual" if price is not None else "",
            details=(
                ("asset", line.asset), ("charge", kind), ("basis_quantity", str(base)),
                ("rate_per_interval", str(rate)), ("interval_ns", str(line.interval_ns)),
                ("accrual_start", str(ts_ns)), ("accrual_end", str(ts_ns + line.interval_ns)),
                ("interest_amount", str(interest)),
                *((("revision", str(revision)),) if revision is not None else ()),
            ),
        )

    def _open(self, line: BorrowLine, quantity: Decimal, effective_ns: int, reported_ns: int, reference: str) -> None:
        principal_after = self.principal(line.venue, line.asset) + quantity
        event = EconomicEvent(
            event_type=EconomicEventType.BORROW_OPEN,
            idempotency_key=f"borrow-open|{line.venue}|{line.asset}|{reference}",
            ts_ns=reported_ns, effective_ts_ns=effective_ns,
            postings=(Posting(line.venue, MAIN, line.asset, quantity),
                      Posting(line.venue, LOAN, line.asset, -quantity)),
            venue=line.venue, source=line.source, reference_id=reference,
            details=(("asset", line.asset), ("borrow_quantity", str(quantity)),
                     ("principal_after", str(principal_after)),
                     ("rate_per_interval", str(line.rate_at(effective_ns))),
                     ("limit", str(line.limit_at(effective_ns)))),
        )
        if not self.ledger.apply_borrow(event):
            return
        self._dirty = True
        if line.charge_on_open:
            charge = self._accrual(line, effective_ns, quantity, f"borrow-open-interest|{event.event_id}",
                                   reported_ns, "OPEN")
            if charge is not None:
                self.ledger.apply_borrow(charge)
        self.on_change((line.venue, line.asset), effective_ns, reported_ns, f"borrow {reference}")

    def repay(self, venue: str, asset: str, quantity: Decimal, ts_ns: int, *,
              repay_id: str | None = None, effective_ns: int | None = None, source: EventSource | None = None) -> bool:
        """Repay interest first, then principal. Returns False for a duplicate repay id."""
        line = self.lines.get((venue, asset))
        if line is None:
            raise ValueError(f"no borrow line for {venue} {asset}")
        if repay_id is not None and repay_id in self._repay_ids:
            self.ledger.economics.duplicates_ignored += 1
            return False
        interest, principal = self.interest_due(venue, asset), self.principal(venue, asset)
        if quantity <= 0 or quantity > interest + principal:
            raise ValueError(f"repayment {quantity} exceeds what is owed ({interest} interest + {principal} principal)")
        if quantity > self.ledger.economics.balance(venue, asset, MAIN):
            raise ValueError(f"repayment {quantity} exceeds the {asset} held on {venue}")
        to_interest = min(quantity, interest)
        to_principal = quantity - to_interest
        cumulative = self._repaid.get((venue, asset), ZERO) + quantity
        when = ts_ns if effective_ns is None else effective_ns
        event = EconomicEvent(
            event_type=EconomicEventType.BORROW_REPAY,
            idempotency_key=f"borrow-repay|{venue}|{asset}|{repay_id or f'cum:{cumulative}'}",
            ts_ns=ts_ns, effective_ts_ns=when,
            postings=tuple(p for p in (
                Posting(venue, MAIN, asset, -quantity),
                Posting(venue, INTEREST, asset, to_interest) if to_interest else None,
                Posting(venue, LOAN, asset, to_principal) if to_principal else None,
            ) if p is not None),
            venue=venue, source=source or line.source, reference_id=repay_id or "",
            details=(("asset", asset), ("repaid", str(quantity)), ("to_interest", str(to_interest)),
                     ("to_principal", str(to_principal)),
                     ("principal_after", str(principal - to_principal))),
        )
        if not self.ledger.apply_borrow(event):
            return False
        self._repaid[(venue, asset)] = cumulative
        if repay_id is not None:
            self._repay_ids.add(repay_id)
        self._dirty = True
        self.on_change((venue, asset), when, ts_ns, f"repay {repay_id or cumulative}")
        self.checkpoint()
        return True

    # -- periodic interest ---------------------------------------------------------------
    def _lines(self):
        return [key for key, line in self.lines.items() if line.authorized]

    def _times(self, key, start_ns: int, until_ns: int) -> list[int]:
        line = self.lines[key]
        return self.schedule(line.anchor_ns, line.interval_ns, start_ns, until_ns)

    def _quantity(self, key, ts_ns: int) -> Decimal:
        venue, asset = key
        owed = self.principal(venue, asset, ts_ns - 1)
        if self.lines[key].basis == "PRINCIPAL_PLUS_INTEREST":
            owed += self.interest_due(venue, asset, ts_ns - 1)
        return owed

    def _build(self, key, ts_ns: int, quantity: Decimal, revision: int, booked_ns: int):
        if quantity <= 0:
            return None
        line = self.lines[key]
        return self._accrual(line, ts_ns, quantity, f"borrow-interest|{line.venue}|{line.asset}|{ts_ns}|r{revision}",
                             booked_ns, "INTERVAL", revision)

    def _book(self, event: EconomicEvent) -> None:
        self.ledger.apply_borrow(event)

    def _unbook(self, event_id: str, ts_ns: int, reason: str) -> None:
        self.ledger.reverse_borrow(event_id, ts_ns, reason)

    def _note_rebook(self, key, ts_ns: int, quantity: Decimal, detail: str) -> None:
        self.notes.append({"venue": key[0], "asset": key[1], "ts_ns": ts_ns,
                           "code": "BORROW_INTEREST_REBOOKED", "detail": detail})

    def _flag(self, line: BorrowLine | tuple[str, str], ts_ns: int, code: str, detail: str) -> None:
        venue, asset = (line.venue, line.asset) if isinstance(line, BorrowLine) else line
        self.flags.append({"venue": venue, "asset": asset, "ts_ns": ts_ns, "code": code, "detail": detail})
        self._dirty = True

    # -- durability ----------------------------------------------------------------------
    def _line_of(self, event: EconomicEvent):
        details = dict(event.details)
        if event.event_type != EconomicEventType.BORROW_ACCRUAL or details.get("charge") != "INTERVAL":
            return None
        key = (event.venue, details.get("asset", ""))
        if key not in self.lines:
            return None
        return key, event.effective, int(details.get("revision", 0)), Decimal(details.get("basis_quantity", "0"))

    def _extra_state(self) -> dict[str, Any]:
        return {
            "flags": self.flags, "notes": self.notes, "now_ns": self.now_ns,
            "forced_closes": self.forced_closes,
            "resolved_recalls": sorted(s.recall.recall_id for s in self._recalls if s.resolved),
            "price_missing_flagged": sorted(s.recall.recall_id for s in self._recalls if s.price_missing_flagged),
            "prices": [[v, a, None if m is None else str(m), None if k is None else str(k)]
                       for (v, a), (m, k) in sorted(self._prices.items())],
        }

    def _restore_extra(self, state: dict[str, Any], events: list[EconomicEvent]) -> None:
        self.flags = list(state.get("flags", []))
        self.notes = list(state.get("notes", []))
        self.now_ns = int(state.get("now_ns") or 0)
        self.forced_closes = list(state.get("forced_closes", []))
        self._prices = {
            (v, a): (None if m is None else Decimal(m), None if k is None else Decimal(k))
            for v, a, m, k in state.get("prices", [])
        }
        # Repayments and recall notices are facts in the ledger.
        repays = [e for e in events if e.event_type == EconomicEventType.BORROW_REPAY]
        for event in repays:
            key = (event.venue, dict(event.details).get("asset", ""))
            self._repaid[key] = self._repaid.get(key, ZERO) + Decimal(dict(event.details).get("repaid", "0"))
            if event.reference_id:
                self._repay_ids.add(event.reference_id)
        announced = {e.reference_id for e in events if e.event_type == EconomicEventType.BORROW_RECALL}
        resolved = set(state.get("resolved_recalls", [])) | {
            e.reference_id.removeprefix("recall:") for e in repays if e.reference_id.startswith("recall:")
        }
        flagged = set(state.get("price_missing_flagged", []))
        for item in self._recalls:
            item.announced = item.recall.recall_id in announced
            item.resolved = item.recall.recall_id in resolved
            item.price_missing_flagged = item.recall.recall_id in flagged

    # -- fills ---------------------------------------------------------------------------
    def on_trade_fill(self, event: EconomicEvent) -> None:
        assets = self.spot_instruments.get((event.venue, event.instrument_id))
        if assets is None or self._forced:
            return
        base = assets[0]
        venue = event.venue
        held = self.ledger.economics.balance(venue, base, MAIN)
        line = self.lines.get((venue, base))
        side = dict(event.details).get("side", "")
        if held < 0:
            shortfall = -held
            try:
                if line is None or not line.authorized:
                    raise BorrowUnavailable(venue, base, "NOT_AUTHORIZED")
                if not self.eligible.get((venue, base), False):
                    raise BorrowUnavailable(venue, base, "NOT_ELIGIBLE")
                if self._recalled(venue, base):
                    raise BorrowUnavailable(venue, base, "RECALLED")
                left = line.limit_at(event.effective) - self.principal(venue, base)
                if shortfall > left:
                    raise BorrowUnavailable(venue, base, "LIMIT_EXHAUSTED", f"need {shortfall}, {left} left")
            except BorrowUnavailable as refused:
                self._unfinanced(venue, base, held, event, refused.code)
                return
            self._open(line, shortfall, event.effective, event.ts_ns, f"fill:{event.event_id}")
        elif side == "BUY" and line is not None and self.repay_policy == "auto_on_buy":
            owed = self.interest_due(venue, base) + self.principal(venue, base)
            amount = min(held, owed)
            if amount > 0:
                self.repay(venue, base, amount, event.ts_ns, repay_id=f"auto:{event.event_id}",
                           effective_ns=event.effective)
        self.checkpoint()

    def _unfinanced(self, venue: str, asset: str, held: Decimal, event: EconomicEvent, code: str) -> None:
        row = {"venue": venue, "asset": asset, "ts_ns": event.effective, "code": f"UNFINANCED_SHORT:{code}",
               "detail": f"{venue} {asset} main balance {held} after fill {event.fill_id}"}
        (self.flags if self.unfinanced_short_policy == "FAIL" else self.notes).append(row)
        self._dirty = True

    # -- recalls -------------------------------------------------------------------------
    def process(self, now_ns: int) -> None:
        """Announce recalls whose time has come and enforce those past their deadline."""
        self.now_ns = max(self.now_ns, now_ns)
        for state in self._recalls:
            recall = state.recall
            if not state.announced and recall.effective_ns <= now_ns:
                state.announced = True
                self.ledger.apply_borrow(EconomicEvent(
                    event_type=EconomicEventType.BORROW_RECALL,
                    idempotency_key=f"borrow-recall|{recall.recall_id}",
                    ts_ns=now_ns, effective_ts_ns=recall.effective_ns, postings=(),
                    venue=recall.venue, source=recall.source, reference_id=recall.recall_id,
                    details=(("asset", recall.asset), ("deadline_ns", str(recall.deadline_ns)),
                             ("quantity", "ALL" if recall.quantity is None else str(recall.quantity)),
                             ("principal_at_recall", str(self.principal(recall.venue, recall.asset)))),
                ))
            if state.announced and not state.resolved and recall.deadline_ns <= now_ns:
                self._enforce(state, now_ns)
                self._dirty = True
        self.checkpoint()

    def _enforce(self, state: _RecallState, now_ns: int) -> None:
        recall = state.recall
        venue, asset = recall.venue, recall.asset
        owed = self.interest_due(venue, asset) + self.principal(venue, asset)
        due = owed if recall.quantity is None else min(owed, recall.quantity + self.interest_due(venue, asset))
        if due <= 0:
            state.resolved = True
            return
        held = max(self.ledger.economics.balance(venue, asset, MAIN), ZERO)
        shortfall = due - min(held, due)
        if shortfall > 0:
            mid_ask = self._prices.get((venue, asset), (None, None))
            price, source = (recall.forced_close_price, EventSource.OBSERVED) if recall.forced_close_price else (
                mid_ask[1], EventSource.SIMULATED)
            if price is None:
                if not state.price_missing_flagged:
                    state.price_missing_flagged = True
                    self._flag((venue, asset), now_ns, "FORCED_CLOSE_PRICE_MISSING",
                               f"recall {recall.recall_id} overdue; no record price and no best ask")
                return
            self._buy_in(recall, shortfall, price, source, now_ns)
        self.repay(venue, asset, due, now_ns, repay_id=f"recall:{recall.recall_id}", source=recall.source)
        state.resolved = True

    def _buy_in(self, recall: BorrowRecall, quantity: Decimal, price: Decimal, source: EventSource, now_ns: int) -> None:
        instrument = next(
            i for (v, i), (b, q) in self.spot_instruments.items() if v == recall.venue and b == recall.asset
        )
        fee = quantity * price * self.forced_close_fee_bps / Decimal("10000")
        order = ChildOrder(
            order_id=f"forced-close|{recall.recall_id}", parent_id="borrow-forced-close",
            opportunity_id="", venue=recall.venue, instrument_id=instrument, side=Side.BUY,
            quantity=quantity, limit_price=price, state=OrderState.FILLED,
            filled_quantity=quantity, average_price=price, fee=fee, order_role="FORCED_CLOSE",
            liquidity="FORCED", fee_source=source.value,
            state_updates=[{"event_ts_ns": now_ns, "cumulative_filled_quantity": str(quantity)}],
        )
        self._forced = True
        try:
            self.ledger.apply_fill(order, now_ns, economic_type=EconomicEventType.BORROW_FORCED_CLOSE)
        finally:
            self._forced = False
        position = self.ledger.positions.get((recall.venue, instrument), ZERO)
        row = {"recall_id": recall.recall_id, "venue": recall.venue, "asset": recall.asset,
               "ts_ns": now_ns, "quantity": str(quantity), "price": str(price), "price_source": source.value,
               "fee": str(fee), "position_after": str(position),
               "unhedged_positions": {f"{v}:{i}": str(q) for (v, i), q in sorted(self.ledger.positions.items()) if q}}
        self.forced_closes.append(row)
        if self.on_forced_close is not None:
            self.on_forced_close(f"borrow recall {recall.recall_id}: venue bought in {quantity} {recall.asset}", now_ns)

    # -- reporting -----------------------------------------------------------------------
    def status_by_venue(self, venues: Iterable[str], perpetual_venues: Iterable[str] = ()) -> dict[str, str]:
        perps = set(perpetual_venues)
        out = {}
        for venue in venues:
            if venue in perps:
                out[venue] = "NOT_APPLICABLE"
            elif any(v == venue for v, _ in self.lines):
                out[venue] = "MODELLED"
            elif self.unfinanced_short_policy == "NOTE":
                out[venue] = "NOT_MODELLED"
            else:
                out[venue] = "NOT_APPLICABLE"
        return out

    def blocking_flags_by_venue(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for flag in self.flags:
            out.setdefault(flag["venue"], []).append(f"{flag['code']}@{flag['ts_ns']}")
        return out

    def notes_by_venue(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for note in self.notes:
            out.setdefault(note["venue"], []).append(f"{note['code']}@{note['ts_ns']}")
        return out

    def report(self) -> dict[str, Any]:
        lines = {}
        for (venue, asset), line in sorted(self.lines.items()):
            available, code = self.availability(venue, asset)
            lines[f"{venue}:{asset}"] = {
                "authorized": line.authorized, "instrument_eligible": self.eligible.get((venue, asset), False),
                "limit_now": str(line.limit_at(self.now_ns)), "principal": str(self.principal(venue, asset)),
                "interest_due": str(self.interest_due(venue, asset)), "available": str(available),
                "availability": code, "rate_per_interval_now": str(line.rate_at(self.now_ns)),
                "interval_ns": line.interval_ns, "charge_on_open": line.charge_on_open, "basis": line.basis,
                "terms_source": line.source.value,
                "repaid_total": str(self._repaid.get((venue, asset), ZERO)),
            }
        totals: dict[str, dict[str, str]] = {}
        for event in self.ledger.economics.events:
            if event.event_type == EconomicEventType.BORROW_ACCRUAL:
                row = totals.setdefault(event.venue, {})
                asset = dict(event.details)["asset"]
                row[asset] = str(Decimal(row.get(asset, "0")) - sum(p.quantity for p in event.postings))
        return {
            "status": "FAIL" if any(f["code"].startswith("UNFINANCED_SHORT") for f in self.flags)
            else "INCOMPLETE" if self.flags else ("COMPLETE" if self.lines else "NOT_CONFIGURED"),
            "repay_policy": self.repay_policy,
            "lines": lines,
            "interest_accrued_by_venue_asset": totals,
            "recalls": [
                {"recall_id": s.recall.recall_id, "venue": s.recall.venue, "asset": s.recall.asset,
                 "announced": s.announced, "resolved": s.resolved, "deadline_ns": s.recall.deadline_ns}
                for s in self._recalls
            ],
            "forced_closes": self.forced_closes,
            "flags": self.flags,
            "notes": self.notes,
            "rebooked_interest_settlements": self.rebooked,
            "rules": {
                **self.timing_rules(),
                "position_boundary": "changes effective strictly before an interest boundary count",
                "repayment_order": "interest first, then principal",
                "unfinanced_short": "failing check" if self.unfinanced_short_policy == "FAIL"
                else "note (unconstrained inventory model: shorts are not financed)",
            },
        }
