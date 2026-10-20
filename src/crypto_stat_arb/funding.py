"""Perpetual funding settlement (ECO02).

At every funding settlement timestamp of an eligible perpetual instrument the
position held at that instant pays or receives

    linear:  amount = -contracts x multiplier x mark x rate   (settlement asset)
    inverse: amount = -contracts x multiplier / mark x rate   (base coin)

so a long pays a positive rate and a short receives it. Each settlement is a
first-class ``FUNDING`` economic event (``funding|venue|instrument|ts|r<rev>``)
carrying the rate, mark price, position, interval and data source; the legacy
trade ledger gets the same cash so ``account_state.json`` equity agrees.

Rules (all regression-tested in ``tests/test_funding.py``):

* **Realized funding comes from rate records only.** An OBSERVED record (a
  venue's funding history) or a SIMULATED one (a scenario file) is booked
  with that source. A settlement time with no record, a stale record or no
  mark price is flagged and nothing is booked; nothing is assumed.
* **Settlement times** are the union of the record timestamps and the
  instrument's configured schedule (interval + anchor). Schedule-only times
  are where data is expected; they are never booked without a record.
* **Position at settlement** is the as-of contract balance by venue match
  time (``TRADE_FILL.effective_ts_ns``), not by when the fill report reached
  the OMS. Boundary: a fill matched strictly *before* the settlement
  timestamp counts; one matched *at* the timestamp does not.
* **Late fills.** If a fill effective before an already-booked settlement is
  reported afterwards, that settlement is reversed (``REVERSAL``) and rebooked
  as the next revision; history is never rewritten.
* **No double settlement.** Each settlement is processed once per revision,
  and re-running settlement on the same ledger is ignored by the idempotency
  key. Funding is not yet persisted or rebuilt on an OMS restart.
* **Predicted funding** (the venue's current/next rate) is reported by
  ``predict`` and never booked.
* **Horizon.** Settlements are booked only up to the last market event; none
  are invented beyond the data, even if the drain horizon runs past one.
* **Unsupported, fail closed:** inverse contracts and contract multipliers
  other than 1 are rejected at config load (the trade ledger does not carry
  them yet); the amount formula supports both and is tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from .economics import EconomicEvent, EconomicEventType, EventSource, Posting

if TYPE_CHECKING:
    from .ledger import Ledger

PERPETUAL_PRODUCT_TYPES = frozenset({"PERP", "PERPETUAL", "LINEAR_PERP", "SWAP"})
ZERO = Decimal("0")


def is_perpetual(product_type: str) -> bool:
    return str(product_type).upper() in PERPETUAL_PRODUCT_TYPES


def funding_amount(
    contracts: Decimal, rate: Decimal, mark_price: Decimal, *,
    multiplier: Decimal = Decimal("1"), inverse: bool = False,
) -> Decimal:
    """Signed funding cash flow for the holder (negative = paid)."""
    if inverse:
        return -contracts * multiplier / mark_price * rate
    return -contracts * multiplier * mark_price * rate


def _record_ts_ns(row: dict[str, Any], ns_key: str, ms_key: str, text_key: str) -> int | None:
    """Explicit units: ``*_ns`` nanoseconds, ``*_ms`` milliseconds, or an ISO-8601 time."""
    if row.get(ns_key) is not None:
        return int(row[ns_key])
    if row.get(ms_key) is not None:
        return int(row[ms_key]) * 1_000_000
    if row.get(text_key) is not None:
        text = str(row[text_key]).replace("Z", "+00:00")
        moment = datetime.fromisoformat(text)
        return (int(moment.timestamp()) * 1_000_000_000) + moment.microsecond * 1_000
    return None


@dataclass(frozen=True, slots=True)
class FundingSpec:
    venue: str
    instrument_id: str
    contract_asset: str       # ledger asset holding the position (the instrument id)
    settlement_asset: str     # asset funding is paid in (quote for linear)
    multiplier: Decimal = Decimal("1")
    inverse: bool = False
    interval_ns: int = 0      # 0 = no schedule; settlement times come from records only
    anchor_ns: int = 0


@dataclass(frozen=True, slots=True)
class FundingRate:
    venue: str
    instrument_id: str
    funding_ts_ns: int
    rate: Decimal
    mark_price: Decimal | None = None
    source: EventSource = EventSource.OBSERVED
    asof_ns: int | None = None    # when the rate was published/observed
    predicted: bool = False       # a forecast (current/next rate), never booked

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "FundingRate":
        when = _record_ts_ns(row, "funding_ts_ns", "funding_ts_ms", "funding_time")
        if when is None:
            raise ValueError(f"funding record without a timestamp: {row}")
        asof = _record_ts_ns(row, "asof_ns", "asof_ts_ms", "asof")
        mark = row.get("mark_price")
        return cls(
            venue=str(row["venue"]), instrument_id=str(row["instrument_id"]),
            funding_ts_ns=when, rate=Decimal(str(row["rate"])),
            mark_price=None if mark in (None, "") else Decimal(str(mark)),
            source=EventSource(str(row.get("source", "OBSERVED")).upper()),
            asof_ns=asof,
            predicted=bool(row.get("predicted", False)),
        )


class FundingDataConflict(ValueError):
    """Two different rate records for the same settlement."""


@dataclass(slots=True)
class FundingRateBook:
    realized: dict[tuple[str, str, int], FundingRate] = field(default_factory=dict)
    predicted: dict[tuple[str, str], FundingRate] = field(default_factory=dict)
    duplicates_ignored: int = 0

    def add(self, record: FundingRate) -> None:
        if record.predicted:
            key2 = (record.venue, record.instrument_id)
            latest = self.predicted.get(key2)
            if latest is None or (record.asof_ns or 0) >= (latest.asof_ns or 0):
                self.predicted[key2] = record
            return
        key = (record.venue, record.instrument_id, record.funding_ts_ns)
        existing = self.realized.get(key)
        if existing is None:
            self.realized[key] = record
        elif existing == record:
            self.duplicates_ignored += 1
        else:
            raise FundingDataConflict(
                f"conflicting funding records for {key}: {existing.rate} vs {record.rate}"
            )

    def extend(self, records: Iterable[FundingRate]) -> "FundingRateBook":
        for record in records:
            self.add(record)
        return self

    @classmethod
    def from_jsonl(cls, path: Path) -> "FundingRateBook":
        book = cls()
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    book.add(FundingRate.from_dict(json.loads(line)))
        return book


@dataclass(slots=True)
class _Settled:
    event_id: str
    position: Decimal
    revision: int
    amount: Decimal


class FundingEngine:
    """Books FUNDING events for configured perpetuals from rate records."""

    def __init__(
        self, ledger: "Ledger", specs: Iterable[FundingSpec], rates: FundingRateBook | None = None, *,
        settle_lag_ns: int = 5_000_000_000, max_rate_age_ns: int | None = None,
        mark_fallback: str = "none",
    ) -> None:
        self.ledger = ledger
        self.specs = {(s.venue, s.instrument_id): s for s in specs}
        self.rates = rates or FundingRateBook()
        self.settle_lag_ns = settle_lag_ns
        self.max_rate_age_ns = max_rate_age_ns
        if mark_fallback not in ("none", "book_mid"):
            raise ValueError("funding mark_fallback must be 'none' or 'book_mid'")
        self.mark_fallback = mark_fallback
        self.start_ns: int | None = None
        self._last_mid: dict[tuple[str, str], tuple[int, Decimal]] = {}
        self._mid_at: dict[tuple[str, str, int], Decimal] = {}
        self._processed: set[tuple[str, str, int]] = set()
        self._settled: dict[tuple[str, str, int], _Settled] = {}
        self._revisions: dict[tuple[str, str, int], int] = {}
        self.flags: list[dict[str, Any]] = []
        self.rebooked = 0
        self.horizon_ns = 0
        ledger.trade_fill_listeners.append(self.on_trade_fill)

    @property
    def configured(self) -> bool:
        return bool(self.specs)

    # -- inputs ------------------------------------------------------------------
    def observe(self, venue: str, instrument_id: str, ts_ns: int, mid: Decimal | None) -> None:
        """Track the book mid so a settlement can fall back to the mid prevailing at its time."""
        key = (venue, instrument_id)
        if key not in self.specs:
            return
        if self.start_ns is None:
            self.start_ns = ts_ns
        last = self._last_mid.get(key)
        if last is not None:
            for when in self._settlement_times(self.specs[key], ts_ns):
                if last[0] <= when < ts_ns:
                    self._mid_at.setdefault((venue, instrument_id, when), last[1])
        if mid is not None:
            self._last_mid[key] = (ts_ns, mid)

    def _settlement_times(self, spec: FundingSpec, until_ns: int) -> list[int]:
        start = self.start_ns if self.start_ns is not None else until_ns
        times = {
            ts for (v, i, ts) in self.rates.realized
            if v == spec.venue and i == spec.instrument_id and start <= ts <= until_ns
        }
        if spec.interval_ns > 0:
            k = -(-(start - spec.anchor_ns) // spec.interval_ns)  # ceil
            ts = spec.anchor_ns + k * spec.interval_ns
            while ts <= until_ns:
                times.add(ts)
                ts += spec.interval_ns
        return sorted(times)

    # -- settlement --------------------------------------------------------------
    def settle_until(self, now_ns: int, *, final: bool = False) -> None:
        """Book every settlement at or before ``now - settle_lag`` (``now`` when final).

        ``now_ns`` must be a market-data time: no settlement past the data is booked.
        """
        if not self.specs or self.start_ns is None:
            return
        self.horizon_ns = max(self.horizon_ns, now_ns)
        cutoff = now_ns if final else now_ns - self.settle_lag_ns
        for spec in self.specs.values():
            for ts in self._settlement_times(spec, cutoff):
                key = (spec.venue, spec.instrument_id, ts)
                if key not in self._processed:
                    self._processed.add(key)
                    self._settle(spec, ts, now_ns)

    def _position(self, spec: FundingSpec, ts_ns: int) -> Decimal:
        # Boundary: fills matched strictly before the settlement timestamp count.
        return self.ledger.economics.balance(spec.venue, spec.contract_asset, as_of_ns=ts_ns - 1)

    def _flag(self, spec: FundingSpec, ts_ns: int, code: str, position: Decimal, detail: str = "") -> None:
        self.flags.append({
            "venue": spec.venue, "instrument_id": spec.instrument_id, "funding_ts_ns": ts_ns,
            "code": code, "position": str(position), "detail": detail,
            # Missing data only matters economically when a position was held.
            "blocking": position != 0 and code != "LATE_FILL_REBOOKED",
        })

    def _settle(self, spec: FundingSpec, ts_ns: int, booked_ns: int) -> None:
        position = self._position(spec, ts_ns)
        record = self.rates.realized.get((spec.venue, spec.instrument_id, ts_ns))
        if record is None:
            self._flag(spec, ts_ns, "FUNDING_RATE_MISSING", position,
                       "no realized rate record for this settlement time")
            return
        if (
            self.max_rate_age_ns is not None and record.asof_ns is not None
            and ts_ns - record.asof_ns > self.max_rate_age_ns
        ):
            self._flag(spec, ts_ns, "FUNDING_RATE_STALE", position,
                       f"rate observed {ts_ns - record.asof_ns} ns before settlement")
            return
        if position == 0:
            return
        mark, mark_source = record.mark_price, "funding record mark price"
        if mark is None and self.mark_fallback == "book_mid":
            mark = self._mid_at.get((spec.venue, spec.instrument_id, ts_ns))
            last = self._last_mid.get((spec.venue, spec.instrument_id))
            if mark is None and last is not None and last[0] <= ts_ns:
                mark = last[1]
            mark_source = "book mid at settlement (approximation of the mark price)"
        if mark is None or mark <= 0:
            self._flag(spec, ts_ns, "FUNDING_MARK_MISSING", position,
                       "no mark price in the record and no mid fallback")
            return
        amount = funding_amount(
            position, record.rate, mark, multiplier=spec.multiplier, inverse=spec.inverse
        )
        settlement_key = (spec.venue, spec.instrument_id, ts_ns)
        revision = self._revisions.get(settlement_key, 0)
        self._revisions[settlement_key] = revision + 1
        event = EconomicEvent(
            event_type=EconomicEventType.FUNDING,
            idempotency_key=f"funding|{spec.venue}|{spec.instrument_id}|{ts_ns}|r{revision}",
            ts_ns=max(booked_ns, ts_ns), effective_ts_ns=ts_ns,
            postings=(Posting(spec.venue, "main", spec.settlement_asset, amount),),
            venue=spec.venue, instrument_id=spec.instrument_id, source=record.source,
            reporting_value=amount if spec.settlement_asset == self.ledger.economics.reporting_currency else None,
            valuation_price=mark, valuation_source=mark_source,
            details=(
                ("rate", str(record.rate)), ("mark_price", str(mark)), ("position", str(position)),
                ("multiplier", str(spec.multiplier)), ("inverse", str(spec.inverse).lower()),
                ("interval_ns", str(spec.interval_ns)), ("revision", str(revision)),
                ("rate_asof_ns", "" if record.asof_ns is None else str(record.asof_ns)),
            ),
        )
        self.ledger.apply_funding(event)
        self._settled[(spec.venue, spec.instrument_id, ts_ns)] = _Settled(
            event.event_id, position, revision, amount
        )

    def on_trade_fill(self, event: EconomicEvent) -> None:
        """A fill effective before a booked settlement: reverse and rebook it."""
        spec = self.specs.get((event.venue, event.instrument_id))
        if spec is None:
            return
        for (venue, instrument_id, ts_ns), settled in sorted(self._settled.items()):
            if (venue, instrument_id) != (spec.venue, spec.instrument_id) or event.effective >= ts_ns:
                continue
            if self._position(spec, ts_ns) == settled.position:
                continue
            self.ledger.reverse_funding(settled.event_id, event.ts_ns, "late fill changed the settlement position")
            del self._settled[(venue, instrument_id, ts_ns)]
            self.rebooked += 1
            self._flag(spec, ts_ns, "LATE_FILL_REBOOKED", settled.position,
                       f"fill {event.fill_id} effective {event.effective} reported at {event.ts_ns}")
            self._settle(spec, ts_ns, event.ts_ns)
        # A settlement processed while flat (nothing booked) now has a position:
        # settle it for the first time (its earlier flags are re-evaluated).
        for key in sorted(self._processed):
            venue, instrument_id, ts_ns = key
            if (
                (venue, instrument_id) != (spec.venue, spec.instrument_id) or key in self._settled
                or event.effective >= ts_ns or self._position(spec, ts_ns) == 0
            ):
                continue
            self.flags = [
                f for f in self.flags
                if (f["venue"], f["instrument_id"], f["funding_ts_ns"]) != key
                or f["code"] == "LATE_FILL_REBOOKED"
            ]
            self.rebooked += 1
            self._flag(spec, ts_ns, "LATE_FILL_REBOOKED", ZERO,
                       f"fill {event.fill_id} effective {event.effective} reported at {event.ts_ns}")
            self._settle(spec, ts_ns, event.ts_ns)

    # -- forecasts and report ----------------------------------------------------
    def predict(self, venue: str, instrument_id: str, *, mark_price: Decimal | None = None) -> dict[str, Any]:
        """Predicted funding for the current position at the venue's next rate. Never booked."""
        spec = self.specs.get((venue, instrument_id))
        record = self.rates.predicted.get((venue, instrument_id))
        if spec is None or record is None:
            return {"venue": venue, "instrument_id": instrument_id, "status": "NO_PREDICTION"}
        position = self.ledger.economics.balance(venue, spec.contract_asset)
        mark = record.mark_price or mark_price
        if mark is None:
            return {"venue": venue, "instrument_id": instrument_id, "status": "NO_MARK"}
        return {
            "venue": venue, "instrument_id": instrument_id, "status": "PREDICTED (not booked)",
            "next_funding_ts_ns": record.funding_ts_ns, "predicted_rate": str(record.rate),
            "position": str(position), "mark_price": str(mark),
            "predicted_amount": str(funding_amount(
                position, record.rate, mark, multiplier=spec.multiplier, inverse=spec.inverse,
            )),
            "settlement_asset": spec.settlement_asset,
        }

    def report(self, closing_marks: dict[tuple[str, str], Decimal] | None = None) -> dict[str, Any]:
        if not self.specs:
            return {"status": "NOT_CONFIGURED", "detail": "no perpetual instruments configured"}
        by_venue: dict[str, Decimal] = {}
        for (venue, _i, _ts), settled in self._settled.items():
            by_venue[venue] = by_venue.get(venue, ZERO) + settled.amount
        blocking = [f for f in self.flags if f["blocking"]]
        unscheduled = sorted(f"{v}:{i}" for (v, i), s in self.specs.items() if s.interval_ns <= 0)
        marks = closing_marks or {}
        return {
            "status": "INCOMPLETE" if blocking or unscheduled else "COMPLETE",
            "unscheduled_instruments": unscheduled,
            "horizon_ns": self.horizon_ns,
            "settlements_booked": len(self._settled),
            "settlement_times_processed": len(self._processed),
            "rebooked_after_late_fills": self.rebooked,
            "duplicate_rate_records_ignored": self.rates.duplicates_ignored,
            "realized_by_venue": {v: str(a) for v, a in sorted(by_venue.items())},
            "flags": self.flags,
            "predicted": [
                self.predict(v, i, mark_price=marks.get((v, i))) for (v, i) in sorted(self.specs)
            ],
            "rules": {
                "position_boundary": "fills matched strictly before the settlement timestamp count",
                "mark_fallback": self.mark_fallback,
                "settle_lag_ns": self.settle_lag_ns,
                "max_rate_age_ns": self.max_rate_age_ns,
                "stale_detection": "on" if self.max_rate_age_ns is not None else "off (max_rate_age_ms not set)",
            },
        }

    def blocking_flags_by_venue(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for (venue, instrument_id), spec in self.specs.items():
            if spec.interval_ns <= 0:
                out.setdefault(venue, []).append(f"FUNDING_SCHEDULE_UNKNOWN:{instrument_id}")
        for flag in self.flags:
            if flag["blocking"]:
                out.setdefault(flag["venue"], []).append(f"{flag['code']}@{flag['funding_ts_ns']}")
        return out
