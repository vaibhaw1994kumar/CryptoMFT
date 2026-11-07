"""Effective-dated maker/taker/rebate fee schedule (MR-09, ECO01).

A rule applies from ``effective_from_ns`` until the next rule for the same
venue and instrument scope. Positive bps are costs; negative bps are rebates.
``instrument_id="*"`` is a venue-wide rule; an instrument-specific rule always
wins over the venue-wide rule effective at the same time.

``fee_asset`` is the asset the venue charges in: ``QUOTE`` (default), ``BASE``
or an explicit token symbol (e.g. ``BNB``). ``source`` says whether the rate is
``VERIFIED`` for this account or ``ASSUMED`` (e.g. a default tier). With
``conservative_unverified`` an assumed *negative* rate is not credited (booked
at 0 bps) until the tier is verified. That covers maker rebates and any
negative taker rate alike; assumed costs are kept as configured.

There is one rate path: ``FeeRule.effective_bps`` / ``FeeSchedule.rate_bps``
/ ``InstrumentMaster.fee_bps`` all return the rate that is booked, so edge
filters, simulators and the ledger cannot disagree. ``raw_rate_bps`` exposes
the configured number for reporting only.
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


class FeeSource(StrEnum):
    VERIFIED = "VERIFIED"
    ASSUMED = "ASSUMED"
    # B2 (owner 7 Nov): a public schedule for the account's tier, not yet read from the account. Treated as unverified everywhere (conservative_unverified applies); the label survives on every booked fee.
    PUBLIC_SCHEDULE_UNVERIFIED = "PUBLIC_SCHEDULE_UNVERIFIED"


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
    fee_asset: str = "QUOTE"
    source: FeeSource = FeeSource.ASSUMED
    conservative_unverified: bool = False

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["maker_bps"] = str(self.maker_bps)
        row["taker_bps"] = str(self.taker_bps)
        row["source"] = self.source.value
        return row

    def effective_bps(self, liquidity: "Liquidity") -> tuple[Decimal, str]:
        """Rate actually booked for ``liquidity`` and the label for its source."""
        bps = self.maker_bps if liquidity == Liquidity.MAKER else self.taker_bps
        if self.source == FeeSource.VERIFIED:
            return bps, FeeSource.VERIFIED.value
        if self.conservative_unverified and bps < 0:
            return Decimal("0"), "CONSERVATIVE_ASSUMED"
        if self.source == FeeSource.PUBLIC_SCHEDULE_UNVERIFIED:
            return bps, "ASSUMED_" + FeeSource.PUBLIC_SCHEDULE_UNVERIFIED.value      # still an ASSUMED fee for the ledger, with its own label
        return bps, FeeSource.ASSUMED.value


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
        """The rate actually booked (conservative policy applied); None without a rule."""
        rule = self.rule(venue, instrument_id, at_ns)
        if rule is None:
            return None
        return rule.effective_bps(liquidity)[0]

    def raw_rate_bps(
        self, venue: str, instrument_id: str, liquidity: Liquidity,
        at_ns: int | None = None,
    ) -> Decimal | None:
        """The configured rate before the conservative policy. For reporting only."""
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
                fee_asset=str(row.get("fee_asset", "QUOTE")).upper(),
                source=FeeSource(str(row.get("fee_source", "ASSUMED")).upper()),
                conservative_unverified=bool(row.get("conservative_unverified", False)),
            )
            for row in rows
        )


# Named fee tiers for the backtester's profiles (B2 step 4). One place for every number a perp-perp run books: a profile names a tier per venue, the run builds its schedule from these rows.
# Rates are per instrument class (a linear USDT perp, SOLUSDT); the instrument id is filled in when the rows are built. They are NOT the live/paper config (config/perp_research.json).
FEE_TIERS: dict[str, dict[str, Any]] = {
    "bybit_perp_mm": {"venue": "BYBIT", "maker_fee_bps": "-0.5", "taker_fee_bps": "5.5", "fee_source": "ASSUMED", "conservative_unverified": False,
                      "tier": "bybit_perp_mm: SCENARIO (maker rebate unconfirmed: needs the market maker program; the rebate is credited here on purpose)"},
    "binance_perp_vip0": {"venue": "BINANCE", "maker_fee_bps": "2.0", "taker_fee_bps": "5.0", "fee_source": "ASSUMED", "conservative_unverified": False,
                          "tier": "binance_perp_vip0: ASSUMED default tier"},
    "bybit_perp_vip5": {"venue": "BYBIT", "maker_fee_bps": "1.0", "taker_fee_bps": "3.2", "fee_source": "PUBLIC_SCHEDULE_UNVERIFIED", "conservative_unverified": True,
                        "tier": "bybit_perp_vip5: linear VIP 5 (owner-confirmed tier), public schedule as given by the owner 7 Nov 2026, account SOLUSDT rates not read"},
    "binance_perp_vip9": {"venue": "BINANCE", "maker_fee_bps": "0.0", "taker_fee_bps": "1.7", "fee_source": "PUBLIC_SCHEDULE_UNVERIFIED", "conservative_unverified": True,
                          "tier": "binance_perp_vip9: USD-M VIP 9 (owner-confirmed tier), public schedule as given by the owner 7 Nov 2026, account SOLUSDT rates not read; "
                                  "with BNB the taker is 1.53 (recorded, not built: a BNB fee needs a BNB price)"},
}


def tier_rows(tiers: dict[str, str], instrument_id: str) -> list[dict[str, Any]]:
    """Config rows (the format FeeSchedule.from_config reads) for {venue name: tier name}; the venue is the tier's own, so a tier cannot be applied to the wrong venue."""
    rows = []
    for venue_name, tier_name in tiers.items():
        if tier_name not in FEE_TIERS:
            raise ValueError(f"unknown fee tier {tier_name!r}; known: {sorted(FEE_TIERS)}")
        row = dict(FEE_TIERS[tier_name])
        if row["venue"].lower() != venue_name.lower():
            raise ValueError(f"fee tier {tier_name!r} belongs to {row['venue']}, not {venue_name}")
        rows.append({"instrument_id": instrument_id, "effective_from": "1970-01-01T00:00:00Z", **row})
    return rows


def schedule_for(tiers: dict[str, str], instrument_id: str) -> "FeeSchedule":
    return FeeSchedule.from_config(tier_rows(tiers, instrument_id))
