"""Effective-dated maker/taker/rebate fee schedule (MR-09).

A rule applies from ``effective_from_ns`` until the next rule for the same
venue and instrument scope. Positive bps are costs; negative bps are rebates.
``instrument_id="*"`` is a venue-wide rule; an instrument-specific rule always
wins over the venue-wide rule effective at the same time.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Iterable


class Liquidity(StrEnum):
    MAKER = "MAKER"
    TAKER = "TAKER"


def parse_effective_ns(value: Any) -> int:
    """Accept integer nanoseconds or an ISO-8601 timestamp (UTC if naive)."""
    if value is None or value == "":
        return 0
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp()) * 1_000_000_000 + parsed.microsecond * 1_000


@dataclass(frozen=True, slots=True)
class FeeRule:
    venue: str
    maker_bps: Decimal
    taker_bps: Decimal
    effective_from_ns: int = 0
    instrument_id: str = "*"
    tier: str = ""

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["maker_bps"] = str(self.maker_bps)
        row["taker_bps"] = str(self.taker_bps)
        return row


class FeeSchedule:
    schema_version = "1.0.0"

    def __init__(self, rules: Iterable[FeeRule] = ()) -> None:
        self._rules: dict[tuple[str, str], list[FeeRule]] = {}
        for rule in rules:
            self.add(rule)

    def add(self, rule: FeeRule) -> None:
        versions = self._rules.setdefault((rule.venue, rule.instrument_id), [])
        if any(item.effective_from_ns == rule.effective_from_ns for item in versions):
            raise ValueError(
                f"duplicate fee rule for {rule.venue}/{rule.instrument_id} "
                f"at {rule.effective_from_ns}"
            )
        versions.append(rule)
        versions.sort(key=lambda item: item.effective_from_ns)

    def __bool__(self) -> bool:
        return bool(self._rules)

    @staticmethod
    def _effective(versions: list[FeeRule], at_ns: int | None) -> FeeRule | None:
        if not versions:
            return None
        if at_ns is None:
            return versions[-1]
        found = None
        for rule in versions:
            if rule.effective_from_ns <= at_ns:
                found = rule
            else:
                break
        return found

    def rule(
        self, venue: str, instrument_id: str, at_ns: int | None = None
    ) -> FeeRule | None:
        specific = self._effective(self._rules.get((venue, instrument_id), []), at_ns)
        if specific is not None:
            return specific
        return self._effective(self._rules.get((venue, "*"), []), at_ns)

    def rate_bps(
        self, venue: str, instrument_id: str, liquidity: Liquidity,
        at_ns: int | None = None,
    ) -> Decimal | None:
        rule = self.rule(venue, instrument_id, at_ns)
        if rule is None:
            return None
        return rule.maker_bps if liquidity == Liquidity.MAKER else rule.taker_bps

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "rules": [
                rule.to_dict()
                for _, versions in sorted(self._rules.items()) for rule in versions
            ],
        }

    @classmethod
    def from_config(cls, rows: Iterable[dict[str, Any]]) -> "FeeSchedule":
        return cls(
            FeeRule(
                venue=str(row["venue"]),
                instrument_id=str(row.get("instrument_id", "*")),
                maker_bps=Decimal(str(row["maker_fee_bps"])),
                taker_bps=Decimal(str(row["taker_fee_bps"])),
                effective_from_ns=parse_effective_ns(row.get("effective_from")),
                tier=str(row.get("tier", "")),
            )
            for row in rows
        )
