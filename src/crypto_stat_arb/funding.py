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
  and re-running settlement is ignored by the idempotency key (events are
  stamped at the settlement time, so a re-run is byte-identical).
* **Restarts.** With a plan store, every FUNDING event and reversal is
  journaled (financing_journal.py) and replayed into the rebuilt ledger;
  the engine re-derives booked settlements and revisions from it.
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
from .periodic import PeriodicSettlement

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
    rate_cap: Decimal | None = None
    rate_floor: Decimal | None = None


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
    # A final settled rate from the venue's funding history: its timestamp is
    # the settlement itself, so observation-time checks do not apply.
    settlement_history: bool = False

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
            settlement_history=bool(row.get("settlement_history", False)),
        )


class FundingDataConflict(ValueError):
    """Two different rate records for the same settlement."""


@dataclass(slots=True)
class FundingRateBook:
    realized: dict[tuple[str, str, int], FundingRate] = field(default_factory=dict)
    predicted: dict[tuple[str, str], FundingRate] = field(default_factory=dict)
    duplicates_ignored: int = 0
    # Records that could not be used, kept with the reason for diagnostics
    # (audit P2); a rejected record is never booked.
    rejected: list[dict[str, Any]] = field(default_factory=list)

    def add_row(self, row: dict[str, Any], origin: str = "") -> None:
        """Parse a raw record; an unparseable one is rejected, not fatal."""
        try:
            record = FundingRate.from_dict(row)
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            self.rejected.append({"origin": origin, "reason": "UNPARSEABLE",
                                  "detail": f"{type(exc).__name__}: {exc}"[:200], "record": row})
            return
        self.add(record)

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
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    book.rejected.append({"origin": f"{path.name}:{number}", "reason": "UNPARSEABLE",
                                          "detail": str(exc)[:200], "record": line[:200]})
                    continue
                book.add_row(row, f"{path.name}:{number}")
        return book

    def validated(self, specs: dict[tuple[str, str], "FundingSpec"], max_abs_rate: Decimal) -> "FundingRateBook":
        """A copy holding only usable records; the rest are rejected with a reason.

        * the venue/instrument must be a configured perpetual;
        * the rate must lie within the venue's floor/cap when configured,
          else within +/- ``max_abs_rate``;
        * a mark price, when given, must be positive.
        """
        out = FundingRateBook(duplicates_ignored=self.duplicates_ignored, rejected=list(self.rejected))
        for record in [*self.realized.values(), *self.predicted.values()]:
            spec = specs.get((record.venue, record.instrument_id))
            reason, detail = "", ""
            if spec is None:
                reason, detail = "UNKNOWN_INSTRUMENT", f"{record.venue}:{record.instrument_id} is not a configured perpetual"
            elif spec.rate_cap is not None and spec.rate_floor is not None and not (
                spec.rate_floor <= record.rate <= spec.rate_cap
            ):
                reason, detail = "RATE_OUT_OF_BOUNDS", f"{record.rate} outside venue floor/cap [{spec.rate_floor}, {spec.rate_cap}]"
            elif (spec.rate_cap is None or spec.rate_floor is None) and abs(record.rate) > max_abs_rate:
                reason, detail = "RATE_OUT_OF_BOUNDS", f"|{record.rate}| > max_abs_rate {max_abs_rate}"
            elif record.mark_price is not None and record.mark_price <= 0:
                reason, detail = "INVALID_MARK", f"mark price {record.mark_price}"
            if reason:
                out.rejected.append({
                    "origin": "", "reason": reason, "detail": detail,
                    "record": {"venue": record.venue, "instrument_id": record.instrument_id,
                               "funding_ts_ns": record.funding_ts_ns, "rate": str(record.rate),
                               "predicted": record.predicted},
                })
            else:
                out.add(record)
        return out


class FundingEngine(PeriodicSettlement):
    """Books FUNDING events for configured perpetuals from rate records."""

    def __init__(
        self, ledger: "Ledger", specs: Iterable[FundingSpec], rates: FundingRateBook | None = None, *,
        settle_lag_ns: int = 5_000_000_000, max_rate_age_ns: int | None = None,
        mark_fallback: str = "none", max_abs_rate: Decimal = Decimal("0.05"),
        max_mark_age_ns: int = 60_000_000_000, schedule_tolerance_ns: int = 1_000_000_000,
    ) -> None:
        super().__init__(settle_lag_ns=settle_lag_ns)
        self.ledger = ledger
        self.specs = {(s.venue, s.instrument_id): s for s in specs}
        self.max_abs_rate = max_abs_rate
        self.rates = (rates or FundingRateBook()).validated(self.specs, max_abs_rate)
        # Venues stamp a settlement with when it was processed (Binance:
        # a few ms after the boundary). A record within schedule_tolerance of a
        # scheduled boundary *is* that settlement; the venue time is kept.
        self.schedule_tolerance_ns = schedule_tolerance_ns
        self.venue_settlement_ts: dict[tuple[str, str, int], int] = {}
        self._snap_to_schedule()
        self.max_rate_age_ns = max_rate_age_ns
        if mark_fallback not in ("none", "book_mid", "exchange_mark"):
            raise ValueError("funding mark_fallback must be 'none', 'book_mid' or 'exchange_mark'")
        self.mark_fallback = mark_fallback
        # exchange_mark: the venue's own mark price from the recorded
        # derivatives stream, observed no more than max_mark_age before the
        # settlement (not an approximation, unlike the book mid).
        self.max_mark_age_ns = max_mark_age_ns
        self._last_xmark: dict[tuple[str, str], tuple[int, Decimal]] = {}
        self._xmark_at: dict[tuple[str, str, int], tuple[int, Decimal]] = {}
        self.predictions_observed = 0
        self._last_mid: dict[tuple[str, str], tuple[int, Decimal]] = {}
        self._mid_at: dict[tuple[str, str, int], Decimal] = {}
        self.flags: list[dict[str, Any]] = []
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
        self.begin(ts_ns)
        last = self._last_mid.get(key)
        if last is not None:
            for when in self._times(key, self.start_ns, ts_ns):
                if last[0] <= when < ts_ns:
                    self._mid_at.setdefault((venue, instrument_id, when), last[1])
        if mid is not None:
            self._last_mid[key] = (ts_ns, mid)

    def _snap_to_schedule(self) -> None:
        snapped: dict[tuple[str, str, int], FundingRate] = {}
        for (venue, instrument_id, ts_ns), record in self.rates.realized.items():
            spec = self.specs.get((venue, instrument_id))
            target = ts_ns
            if spec is not None and spec.interval_ns > 0:
                k = round((ts_ns - spec.anchor_ns) / spec.interval_ns)
                boundary = spec.anchor_ns + k * spec.interval_ns
                if abs(ts_ns - boundary) <= self.schedule_tolerance_ns:
                    target = boundary
            key = (venue, instrument_id, target)
            if target != ts_ns:
                self.venue_settlement_ts[key] = ts_ns
                record = FundingRate(
                    record.venue, record.instrument_id, target, record.rate, record.mark_price, record.source,
                    record.asof_ns, record.predicted, record.settlement_history,
                )
            existing = snapped.get(key)
            if existing is not None and existing.rate != record.rate:
                raise FundingDataConflict(f"two different rates for the settlement at {key}")
            snapped[key] = record
        self.rates.realized = snapped

    def observe_exchange_mark(self, venue: str, instrument_id: str, ts_ns: int, mark: Decimal) -> None:
        """The venue's mark price (derivatives stream) at ``ts_ns``."""
        key = (venue, instrument_id)
        if key not in self.specs:
            return
        self.begin(ts_ns)
        last = self._last_xmark.get(key)
        if last is not None:
            for when in self._times(key, self.start_ns, ts_ns):
                if last[0] <= when < ts_ns:
                    self._xmark_at.setdefault((venue, instrument_id, when), last)
        self._last_xmark[key] = (ts_ns, mark)

    def observe_prediction(
        self, venue: str, instrument_id: str, rate: Decimal, next_funding_ts_ns: int, asof_ns: int,
        mark: Decimal | None = None,
    ) -> None:
        """The venue's current/predicted rate for its next settlement: for predict() only."""
        if (venue, instrument_id) not in self.specs or abs(rate) > self.max_abs_rate:
            return
        self.rates.add(FundingRate(
            venue, instrument_id, next_funding_ts_ns, rate, mark_price=mark,
            source=EventSource.OBSERVED, asof_ns=asof_ns, predicted=True,
        ))
        self.predictions_observed += 1

    # -- PeriodicSettlement hooks -------------------------------------------------
    def _lines(self):
        return self.specs.keys()

    def _times(self, line, start_ns: int, until_ns: int) -> list[int]:
        spec = self.specs[line]
        times = {
            ts for (v, i, ts) in self.rates.realized
            if v == spec.venue and i == spec.instrument_id and start_ns <= ts <= until_ns
        }
        times.update(self.schedule(spec.anchor_ns, spec.interval_ns, start_ns, until_ns))
        return sorted(times)

    def _quantity(self, line, ts_ns: int) -> Decimal:
        # Boundary: fills matched strictly before the settlement timestamp count.
        spec = self.specs[line]
        return self.ledger.economics.balance(spec.venue, spec.contract_asset, as_of_ns=ts_ns - 1)

    def _flag(self, spec: FundingSpec, ts_ns: int, code: str, position: Decimal, detail: str = "") -> None:
        self.flags.append({
            "venue": spec.venue, "instrument_id": spec.instrument_id, "funding_ts_ns": ts_ns,
            "code": code, "position": str(position), "detail": detail,
            # Missing data only matters economically when a position was held.
            "blocking": position != 0 and code != "LATE_FILL_REBOOKED",
        })

    def _note_rebook(self, line, ts_ns: int, quantity: Decimal, detail: str) -> None:
        spec = self.specs[line]
        self.flags = [
            f for f in self.flags
            if (f["venue"], f["instrument_id"], f["funding_ts_ns"]) != (spec.venue, spec.instrument_id, ts_ns)
            or f["code"] == "LATE_FILL_REBOOKED"
        ]
        self._flag(spec, ts_ns, "LATE_FILL_REBOOKED", quantity, detail)

    def _book(self, event: EconomicEvent) -> None:
        self.ledger.apply_funding(event)

    def _unbook(self, event_id: str, ts_ns: int, reason: str) -> None:
        self.ledger.reverse_funding(event_id, ts_ns, reason)

    def _build(self, line, ts_ns: int, position: Decimal, revision: int, booked_ns: int) -> EconomicEvent | None:
        spec = self.specs[line]
        record = self.rates.realized.get((spec.venue, spec.instrument_id, ts_ns))
        if record is None:
            self._flag(spec, ts_ns, "FUNDING_RATE_MISSING", position,
                       "no realized rate record for this settlement time")
            return None
        if not record.settlement_history:
            # A live observation of the rate: it must say when it was seen, not
            # be seen after we use it, and not be too old (audit P1).
            if record.asof_ns is not None and record.asof_ns > max(booked_ns, ts_ns):
                self._flag(spec, ts_ns, "FUNDING_RATE_ASOF_IN_FUTURE", position,
                           f"rate observed at {record.asof_ns}, after it was used at {max(booked_ns, ts_ns)}")
                return None
            if self.max_rate_age_ns is not None:
                if record.asof_ns is None:
                    self._flag(spec, ts_ns, "FUNDING_RATE_ASOF_MISSING", position,
                               "stale-rate protection is on and the record has no observation time "
                               "(mark venue history records settlement_history)")
                    return None
                if ts_ns - record.asof_ns > self.max_rate_age_ns:
                    self._flag(spec, ts_ns, "FUNDING_RATE_STALE", position,
                               f"rate observed {ts_ns - record.asof_ns} ns before settlement")
                    return None
        if position == 0:
            return None
        mark, mark_source = record.mark_price, "funding record mark price"
        if mark is None and self.mark_fallback == "book_mid":
            mark = self._mid_at.get((spec.venue, spec.instrument_id, ts_ns))
            last = self._last_mid.get((spec.venue, spec.instrument_id))
            if mark is None and last is not None and last[0] <= ts_ns:
                mark = last[1]
            mark_source = "book mid at settlement (approximation of the mark price)"
        if mark is None and self.mark_fallback == "exchange_mark":
            observed = self._xmark_at.get((spec.venue, spec.instrument_id, ts_ns))
            last = self._last_xmark.get((spec.venue, spec.instrument_id))
            if observed is None and last is not None and last[0] <= ts_ns:
                observed = last
            if observed is not None and ts_ns - observed[0] <= self.max_mark_age_ns:
                mark = observed[1]
                mark_source = "exchange mark price (recorded venue stream)"
        if mark is None or mark <= 0:
            self._flag(spec, ts_ns, "FUNDING_MARK_MISSING", position,
                       "no mark price in the record and no mid fallback")
            return None
        amount = funding_amount(
            position, record.rate, mark, multiplier=spec.multiplier, inverse=spec.inverse
        )
        return EconomicEvent(
            event_type=EconomicEventType.FUNDING,
            idempotency_key=f"funding|{spec.venue}|{spec.instrument_id}|{ts_ns}|r{revision}",
            # Stamped at the settlement time, not the booking time, so a
            # settlement re-run after a restart is an identical duplicate.
            ts_ns=ts_ns, effective_ts_ns=ts_ns,
            postings=(Posting(spec.venue, "main", spec.settlement_asset, amount),),
            venue=spec.venue, instrument_id=spec.instrument_id, source=record.source,
            reporting_value=amount if spec.settlement_asset == self.ledger.economics.reporting_currency else None,
            valuation_price=mark, valuation_source=mark_source,
            details=(
                ("rate", str(record.rate)), ("mark_price", str(mark)), ("position", str(position)),
                ("multiplier", str(spec.multiplier)), ("inverse", str(spec.inverse).lower()),
                ("interval_ns", str(spec.interval_ns)), ("revision", str(revision)),
                ("rate_asof_ns", "" if record.asof_ns is None else str(record.asof_ns)),
                ("venue_settlement_ts_ns", str(self.venue_settlement_ts.get(
                    (spec.venue, spec.instrument_id, ts_ns), ts_ns))),
            ),
        )

    # -- durability ----------------------------------------------------------------
    def _line_of(self, event: EconomicEvent):
        if event.event_type != EconomicEventType.FUNDING or (event.venue, event.instrument_id) not in self.specs:
            return None
        details = dict(event.details)
        return ((event.venue, event.instrument_id), event.effective,
                int(details.get("revision", 0)), Decimal(details.get("position", "0")))

    def _extra_state(self) -> dict[str, Any]:
        return {
            "flags": self.flags,
            "last_mid": [[v, i, ts, str(mid)] for (v, i), (ts, mid) in sorted(self._last_mid.items())],
            "last_xmark": [[v, i, ts, str(m)] for (v, i), (ts, m) in sorted(self._last_xmark.items())],
            "xmark_at": [[v, i, when, ts, str(m)] for (v, i, when), (ts, m) in sorted(self._xmark_at.items())],
            "mid_at": [[v, i, ts, str(mid)] for (v, i, ts), mid in sorted(self._mid_at.items())],
        }

    def _restore_extra(self, state: dict[str, Any], events: list[EconomicEvent]) -> None:
        self.flags = list(state.get("flags", []))
        self._last_mid = {(v, i): (int(ts), Decimal(mid)) for v, i, ts, mid in state.get("last_mid", [])}
        self._mid_at = {(v, i, int(ts)): Decimal(mid) for v, i, ts, mid in state.get("mid_at", [])}
        self._last_xmark = {(v, i): (int(ts), Decimal(m)) for v, i, ts, m in state.get("last_xmark", [])}
        self._xmark_at = {(v, i, int(w)): (int(ts), Decimal(m)) for v, i, w, ts, m in state.get("xmark_at", [])}

    def on_trade_fill(self, event: EconomicEvent) -> None:
        """A fill effective before a booked settlement: reverse and rebook it."""
        line = (event.venue, event.instrument_id)
        if line in self.specs:
            self.on_change(
                line, event.effective, event.ts_ns,
                f"fill {event.fill_id} effective {event.effective} reported at {event.ts_ns}",
            )

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
        for event in self.settled_events():
            by_venue[event.venue] = by_venue.get(event.venue, ZERO) + event.postings[0].quantity
        blocking = [f for f in self.flags if f["blocking"]]
        unscheduled = sorted(f"{v}:{i}" for (v, i), s in self.specs.items() if s.interval_ns <= 0)
        data_gaps = [f for f in self.flags if f["code"] != "LATE_FILL_REBOOKED"]
        estimated = [e for e in self.settled_events() if "approximation" in e.valuation_source]
        marks = closing_marks or {}
        rate_sources: dict[str, int] = {}
        mark_sources: dict[str, int] = {}
        revisions: dict[str, int] = {}
        for event in self.settled_events():
            rate_sources[event.source.value] = rate_sources.get(event.source.value, 0) + 1
            mark_sources[event.valuation_source] = mark_sources.get(event.valuation_source, 0) + 1
            label = f"r{dict(event.details).get('revision', '0')}"
            revisions[label] = revisions.get(label, 0) + 1
        off_schedule = sorted(
            f"{v}:{i}@{ts}" for (v, i, ts) in self.rates.realized
            if (v, i) in self.specs and self.specs[(v, i)].interval_ns > 0
            and (ts - self.specs[(v, i)].anchor_ns) % self.specs[(v, i)].interval_ns
        )
        return {
            # Three separate questions (audit P0): is the booked funding complete
            # for every position held; did every expected settlement have data;
            # is the configuration able to tell? Only the first two feed
            # ``status``, which the acceptance gate reads.
            "status": "INCOMPLETE" if blocking or unscheduled else "COMPLETE",
            "economic_status": "INCOMPLETE" if blocking else "COMPLETE",
            "data_coverage": "GAPS" if data_gaps or self.rates.rejected else "COMPLETE",
            "data_gaps": len(data_gaps),
            "data_gaps_while_flat": sum(1 for f in data_gaps if not f["blocking"]),
            "configuration": "INVALID" if unscheduled else "VALID",
            # Provenance of what was booked (audit P2).
            "rate_sources": dict(sorted(rate_sources.items())),
            "mark_sources": dict(sorted(mark_sources.items())),
            "settlement_revisions": dict(sorted(revisions.items())),
            # Usable but not on the configured schedule: a venue may have changed
            # the interval; check the instrument's funding_interval.
            "records_off_schedule": off_schedule[:50],
            "rejected_records": len(self.rates.rejected),
            "rejected_by_reason": {
                reason: sum(1 for r in self.rates.rejected if r["reason"] == reason)
                for reason in sorted({r["reason"] for r in self.rates.rejected})
            },
            "rejected_samples": self.rates.rejected[:20],
            # EXCHANGE_MARK when every booked settlement used the venue's mark;
            # ESTIMATED when any used the book mid instead (audit P1).
            "mark_basis": "ESTIMATED" if estimated else ("EXCHANGE_MARK" if self._settled else "NONE"),
            "settlements_on_book_mid": len(estimated),
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
                "max_mark_age_ns": self.max_mark_age_ns,
                "schedule_tolerance_ns": self.schedule_tolerance_ns,
                "records_snapped_to_schedule": len(self.venue_settlement_ts),
                "predictions_observed": self.predictions_observed,
                "settle_lag_ns": self.settle_lag_ns,
                "max_rate_age_ns": self.max_rate_age_ns,
                "stale_detection": "on" if self.max_rate_age_ns is not None else "off (max_rate_age_ms not set)",
            },
        }

    def estimated_by_venue(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for event in self.settled_events():
            if "approximation" in event.valuation_source:
                out.setdefault(event.venue, []).append(
                    f"FUNDING@{event.effective}: book mid used as the mark price"
                )
        return out

    def blocking_flags_by_venue(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for (venue, instrument_id), spec in self.specs.items():
            if spec.interval_ns <= 0:
                out.setdefault(venue, []).append(f"FUNDING_SCHEDULE_UNKNOWN:{instrument_id}")
        for flag in self.flags:
            if flag["blocking"]:
                out.setdefault(flag["venue"], []).append(f"{flag['code']}@{flag['funding_ts_ns']}")
        return out
