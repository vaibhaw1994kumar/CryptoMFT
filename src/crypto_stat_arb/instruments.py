from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from dataclasses import asdict, replace
from hashlib import sha256
import json
from typing import Any

from .fees import FeeSchedule, Liquidity
from .models import ChildOrder, Instrument


class InstrumentMaster:
    """Effective-dated security master (MR-03).

    Each ``(venue, instrument_id)`` keeps an ordered list of rule versions.
    ``get(..., at_ns=ts)`` returns the version effective at ``ts``; ``at_ns``
    omitted returns the latest version, which preserves the single-version
    behaviour of earlier callers. Status changes (halts, delistings, tick/lot
    changes) are appended as new versions and never rewrite history.
    """

    schema_version = "1.1.0"

    def __init__(
        self, instruments: list[Instrument], fee_schedule: FeeSchedule | None = None
    ):
        self._versions: dict[tuple[str, str], list[Instrument]] = {}
        self.status_events: list[dict[str, Any]] = []
        self.fee_schedule = fee_schedule or FeeSchedule()
        for item in instruments:
            self.add_version(item)

    @property
    def _items(self) -> dict[tuple[str, str], Instrument]:
        return {key: versions[-1] for key, versions in self._versions.items()}

    def add_version(self, instrument: Instrument) -> None:
        key = (instrument.venue, instrument.instrument_id)
        versions = self._versions.setdefault(key, [])
        if any(x.effective_from_ns == instrument.effective_from_ns for x in versions):
            raise ValueError(
                f"duplicate instrument version for {key} at "
                f"{instrument.effective_from_ns}"
            )
        versions.append(instrument)
        versions.sort(key=lambda item: item.effective_from_ns)

    def get(
        self, venue: str, instrument_id: str, at_ns: int | None = None
    ) -> Instrument:
        try:
            versions = self._versions[(venue, instrument_id)]
        except KeyError as exc:
            raise ValueError(f"unknown instrument {venue}/{instrument_id}") from exc
        if at_ns is None:
            return versions[-1]
        effective = None
        for item in versions:
            if item.effective_from_ns <= at_ns:
                effective = item
            else:
                break
        if effective is None:
            raise ValueError(
                f"no instrument rules for {venue}/{instrument_id} effective at {at_ns}"
            )
        return effective

    def versions(self, venue: str, instrument_id: str) -> tuple[Instrument, ...]:
        return tuple(self._versions.get((venue, instrument_id), ()))

    def apply_status(
        self, venue: str, instrument_id: str, *, effective_from_ns: int,
        source: str, status: str | None = None, **rule_changes: Any,
    ) -> Instrument | None:
        """Append a new version when status or trading rules change."""
        current = self.get(venue, instrument_id, effective_from_ns)
        changes = {
            key: value for key, value in rule_changes.items()
            if value is not None and getattr(current, key) != value
        }
        if status is not None and status != current.status:
            changes["status"] = status
        if not changes:
            return None
        if current.effective_from_ns >= effective_from_ns:
            raise ValueError("status change must be later than the current version")
        updated = replace(current, effective_from_ns=effective_from_ns, **changes)
        self.add_version(updated)
        self.status_events.append({
            "venue": venue, "instrument_id": instrument_id,
            "effective_from_ns": effective_from_ns, "source": source,
            "changes": {key: str(value) for key, value in sorted(changes.items())},
        })
        return updated

    def fee_bps(
        self, venue: str, instrument_id: str,
        liquidity: Liquidity = Liquidity.TAKER, at_ns: int | None = None,
    ) -> Decimal:
        """The rate booked for a fill: the dated schedule rule (or the instrument
        default) with the conservative-rebate policy applied, as in the OMS."""
        return self.fee_rule(venue, instrument_id, at_ns).effective_bps(liquidity)[0]

    def fee_rule(self, venue: str, instrument_id: str, at_ns: int | None = None):
        """The effective schedule rule, or a synthetic one from instrument fields."""
        from .fees import FeeRule

        rule = self.fee_schedule.rule(venue, instrument_id, at_ns)
        if rule is not None:
            return rule
        spec = self.get(venue, instrument_id, at_ns)
        return FeeRule(
            venue=venue, instrument_id=instrument_id,
            maker_bps=spec.maker_fee_bps if spec.maker_fee_bps is not None else spec.taker_fee_bps,
            taker_bps=spec.taker_fee_bps, tier="instrument-default",
        )

    def fee(
        self, venue: str, instrument_id: str, notional: Decimal,
        liquidity: Liquidity = Liquidity.TAKER, at_ns: int | None = None,
    ) -> Decimal:
        return (
            notional * self.fee_bps(venue, instrument_id, liquidity, at_ns)
            / Decimal("10000")
        )

    def version_snapshot(self) -> dict[str, object]:
        rows = [
            asdict(item)
            for _, versions in sorted(self._versions.items()) for item in versions
        ]
        fees = self.fee_schedule.to_dict()
        encoded = json.dumps(
            {"instruments": rows, "fees": fees, "status_events": self.status_events},
            default=str, sort_keys=True, separators=(",", ":"),
        ).encode()
        return {
            "schema_version": self.schema_version,
            "instrument_count": len(self._versions),
            "version_count": len(rows),
            "status_event_count": len(self.status_events),
            "fee_rule_count": len(fees["rules"]),
            "content_sha256": sha256(encoded).hexdigest(),
        }

    def validate_order(self, order: ChildOrder, at_ns: int | None = None) -> None:
        spec = self.get(order.venue, order.instrument_id, at_ns)
        if spec.status != "TRADING":
            raise ValueError("instrument is not trading")
        if order.quantity < spec.min_quantity:
            raise ValueError("quantity below minimum")
        if order.quantity % spec.quantity_step:
            raise ValueError("quantity violates step")
        if order.limit_price % spec.tick_size:
            raise ValueError("price violates tick")
        if order.quantity * order.limit_price < spec.min_notional:
            raise ValueError("notional below minimum")

    def round_quantity(
        self, venue: str, instrument_id: str, quantity: Decimal,
        at_ns: int | None = None,
    ) -> Decimal:
        step = self.get(venue, instrument_id, at_ns).quantity_step
        return (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step
