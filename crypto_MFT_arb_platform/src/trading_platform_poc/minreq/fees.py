"""Effective-dated fees, rebates, funding and the shared cost model.

MR-09 and AT-07 require that the *same* versioned inputs drive the route
decision, the simulator, TCA and PnL. Everything that costs money is resolved
through :class:`CostModel` so those four consumers cannot drift apart.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

from trading_platform_poc.minreq.canonical import Liquidity, PriceLevel
from trading_platform_poc.minreq.securitymaster import InstrumentRecord

BPS = Decimal(10_000)
HOUR_NS = 3_600_000_000_000


class FeeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FeeRecord:
    """One effective-dated fee row for a venue/product/account tier."""

    venue: str
    account_tier: str
    maker_bps: Decimal
    taker_bps: Decimal
    maker_rebate_bps: Decimal = Decimal(0)
    product_type: str = "*"
    instrument_id: str = "*"
    effective_from_ns: int = 0
    version: int = 1
    source: str = "reference_default"

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (
            self.venue.lower(),
            self.account_tier,
            self.product_type,
            self.instrument_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "account_tier": self.account_tier,
            "product_type": self.product_type,
            "instrument_id": self.instrument_id,
            "maker_bps": str(self.maker_bps),
            "taker_bps": str(self.taker_bps),
            "maker_rebate_bps": str(self.maker_rebate_bps),
            "effective_from_ns": self.effective_from_ns,
            "version": self.version,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class FundingRecord:
    """Effective-dated funding or borrow rate for derivatives/margin legs."""

    instrument_id: str
    rate: Decimal
    interval_hours: int = 8
    effective_from_ns: int = 0
    version: int = 1
    kind: str = "funding"

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "rate": str(self.rate),
            "interval_hours": self.interval_hours,
            "effective_from_ns": self.effective_from_ns,
            "version": self.version,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class ResolvedFee:
    venue: str
    instrument_id: str
    liquidity: Liquidity
    rate_bps: Decimal
    rebate_bps: Decimal
    record_version: int
    effective_from_ns: int
    account_tier: str

    @property
    def effective_bps(self) -> Decimal:
        return self.rate_bps - self.rebate_bps

    def charge(self, notional: Decimal) -> Decimal:
        return notional * self.effective_bps / BPS

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "instrument_id": self.instrument_id,
            "liquidity": self.liquidity.value,
            "rate_bps": str(self.rate_bps),
            "rebate_bps": str(self.rebate_bps),
            "effective_bps": str(self.effective_bps),
            "fee_version": self.record_version,
            "effective_from_ns": self.effective_from_ns,
            "account_tier": self.account_tier,
        }


@dataclass(frozen=True, slots=True)
class DepthWalk:
    """Result of walking the executable book for a taker order."""

    filled_quantity: Decimal
    average_price: Decimal
    reference_price: Decimal
    slippage_bps: Decimal
    levels_consumed: int
    exhausted_book: bool

    @property
    def notional(self) -> Decimal:
        return self.filled_quantity * self.average_price

    def to_dict(self) -> dict[str, Any]:
        return {
            "filled_quantity": str(self.filled_quantity),
            "average_price": str(self.average_price),
            "reference_price": str(self.reference_price),
            "slippage_bps": str(self.slippage_bps),
            "levels_consumed": self.levels_consumed,
            "exhausted_book": self.exhausted_book,
        }


class FeeBook:
    """Versioned maker/taker/rebate schedule resolved at event time."""

    def __init__(
        self,
        records: Iterable[FeeRecord],
        funding: Iterable[FundingRecord] = (),
        *,
        account_tier: str = "default",
    ) -> None:
        self._account_tier = account_tier
        self._records: dict[tuple[str, str, str, str], list[FeeRecord]] = {}
        for record in records:
            self._records.setdefault(record.key, []).append(record)
        for rows in self._records.values():
            rows.sort(key=lambda item: (item.effective_from_ns, item.version))
        self._index = {
            key: [row.effective_from_ns for row in rows]
            for key, rows in self._records.items()
        }
        self._funding: dict[str, list[FundingRecord]] = {}
        for row in funding:
            self._funding.setdefault(row.instrument_id, []).append(row)
        for rows in self._funding.values():
            rows.sort(key=lambda item: (item.effective_from_ns, item.version))

    @property
    def account_tier(self) -> str:
        return self._account_tier

    @property
    def version(self) -> str:
        """Stable identifier of the whole schedule, recorded in run manifests."""

        parts = [
            f"{key[0]}:{key[1]}:{key[2]}:{key[3]}:{rows[-1].version}"
            for key, rows in sorted(self._records.items())
        ]
        return "|".join(parts)

    def _lookup(
        self, key: tuple[str, str, str, str], at_ns: int
    ) -> FeeRecord | None:
        rows = self._records.get(key)
        if not rows:
            return None
        index = bisect_right(self._index[key], at_ns) - 1
        if index < 0:
            return None
        return rows[index]

    def resolve(
        self,
        instrument: InstrumentRecord,
        liquidity: Liquidity,
        at_ns: int,
        *,
        account_tier: str | None = None,
    ) -> ResolvedFee:
        tier = account_tier or self._account_tier
        venue = instrument.venue.lower()
        candidates = (
            (venue, tier, instrument.product_type.value, instrument.instrument_id),
            (venue, tier, instrument.product_type.value, "*"),
            (venue, tier, "*", "*"),
            (venue, "default", "*", "*"),
        )
        for key in candidates:
            record = self._lookup(key, at_ns)
            if record is not None:
                rate = (
                    record.maker_bps
                    if liquidity is Liquidity.MAKER
                    else record.taker_bps
                )
                rebate = (
                    record.maker_rebate_bps
                    if liquidity is Liquidity.MAKER
                    else Decimal(0)
                )
                return ResolvedFee(
                    venue=venue,
                    instrument_id=instrument.instrument_id,
                    liquidity=liquidity,
                    rate_bps=rate,
                    rebate_bps=rebate,
                    record_version=record.version,
                    effective_from_ns=record.effective_from_ns,
                    account_tier=tier,
                )
        raise FeeError(
            f"no fee schedule for {venue}/{instrument.instrument_id} at {at_ns}"
        )

    def funding_rate(self, instrument_id: str, at_ns: int) -> FundingRecord | None:
        rows = self._funding.get(instrument_id)
        if not rows:
            return None
        index = (
            bisect_right([row.effective_from_ns for row in rows], at_ns) - 1
        )
        return rows[index] if index >= 0 else None

    def accrue_funding(
        self,
        instrument_id: str,
        signed_exposure: Decimal,
        held_ns: int,
        at_ns: int,
    ) -> Decimal:
        """Pro-rata funding/borrow accrual. Zero when no schedule applies."""

        record = self.funding_rate(instrument_id, at_ns)
        if record is None or held_ns <= 0 or signed_exposure == 0:
            return Decimal(0)
        interval_ns = Decimal(record.interval_hours * HOUR_NS)
        if interval_ns <= 0:
            return Decimal(0)
        periods = Decimal(held_ns) / interval_ns
        return signed_exposure * record.rate * periods

    # -------------------------------------------------------------- loading
    def to_documents(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "account_tier": self._account_tier,
            "fees": [
                row.to_dict()
                for rows in self._records.values()
                for row in rows
            ],
            "funding": [
                row.to_dict() for rows in self._funding.values() for row in rows
            ],
        }

    @classmethod
    def from_documents(cls, document: dict[str, Any]) -> FeeBook:
        fees = [
            FeeRecord(
                venue=str(row["venue"]).lower(),
                account_tier=str(row.get("account_tier", "default")),
                maker_bps=Decimal(str(row["maker_bps"])),
                taker_bps=Decimal(str(row["taker_bps"])),
                maker_rebate_bps=Decimal(str(row.get("maker_rebate_bps", 0))),
                product_type=str(row.get("product_type", "*")),
                instrument_id=str(row.get("instrument_id", "*")),
                effective_from_ns=int(row.get("effective_from_ns", 0)),
                version=int(row.get("version", 1)),
                source=str(row.get("source", "yaml")),
            )
            for row in document.get("fees", [])
        ]
        funding = [
            FundingRecord(
                instrument_id=str(row["instrument_id"]),
                rate=Decimal(str(row["rate"])),
                interval_hours=int(row.get("interval_hours", 8)),
                effective_from_ns=int(row.get("effective_from_ns", 0)),
                version=int(row.get("version", 1)),
                kind=str(row.get("kind", "funding")),
            )
            for row in document.get("funding", [])
        ]
        return cls(
            fees, funding, account_tier=str(document.get("account_tier", "default"))
        )

    @classmethod
    def from_yaml(cls, path: Path) -> FeeBook:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.from_documents(raw)

    def write_yaml(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(self.to_documents(), sort_keys=False),
            encoding="utf-8",
        )
        return path


def walk_depth(
    levels: Sequence[PriceLevel],
    quantity: Decimal,
    reference_price: Decimal,
    side: str,
) -> DepthWalk:
    """Consume book levels for a taker order and measure realised slippage."""

    if quantity <= 0:
        raise FeeError("quantity must be positive")
    remaining = quantity
    cost = Decimal(0)
    consumed = 0
    for level in levels:
        if remaining <= 0:
            break
        take = level.quantity if level.quantity < remaining else remaining
        if take <= 0:
            continue
        cost += take * level.price
        remaining -= take
        consumed += 1
    filled = quantity - remaining
    if filled <= 0:
        return DepthWalk(
            filled_quantity=Decimal(0),
            average_price=Decimal(0),
            reference_price=reference_price,
            slippage_bps=Decimal(0),
            levels_consumed=0,
            exhausted_book=True,
        )
    average = cost / filled
    if reference_price > 0:
        signed = (
            (average - reference_price)
            if side.lower() == "buy"
            else (reference_price - average)
        )
        slippage_bps = signed / reference_price * BPS
    else:
        slippage_bps = Decimal(0)
    return DepthWalk(
        filled_quantity=filled,
        average_price=average,
        reference_price=reference_price,
        slippage_bps=slippage_bps,
        levels_consumed=consumed,
        exhausted_book=remaining > 0,
    )


@dataclass(frozen=True, slots=True)
class LegEconomics:
    """Expected or realised economics of one leg, in quote currency."""

    instrument_id: str
    venue: str
    side: str
    quantity: Decimal
    price: Decimal
    notional: Decimal
    fee: Decimal
    fee_bps: Decimal
    slippage_bps: Decimal
    liquidity: Liquidity

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "venue": self.venue,
            "side": self.side,
            "quantity": str(self.quantity),
            "price": str(self.price),
            "notional": str(self.notional),
            "fee": str(self.fee),
            "fee_bps": str(self.fee_bps),
            "slippage_bps": str(self.slippage_bps),
            "liquidity": self.liquidity.value,
        }


@dataclass(frozen=True, slots=True)
class OpportunityEconomics:
    """Gross edge kept strictly separate from realised net PnL."""

    gross_edge: Decimal
    fees: Decimal
    slippage_cost: Decimal
    funding_cost: Decimal
    unwind_cost: Decimal
    net_pnl: Decimal
    legs: tuple[LegEconomics, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "gross_edge": str(self.gross_edge),
            "fees": str(self.fees),
            "slippage_cost": str(self.slippage_cost),
            "funding_cost": str(self.funding_cost),
            "unwind_cost": str(self.unwind_cost),
            "net_pnl": str(self.net_pnl),
            "legs": [leg.to_dict() for leg in self.legs],
        }


class CostModel:
    """Single source of truth for fees, slippage and opportunity PnL."""

    def __init__(self, fee_book: FeeBook) -> None:
        self._fee_book = fee_book

    @property
    def fee_book(self) -> FeeBook:
        return self._fee_book

    def leg_economics(
        self,
        instrument: InstrumentRecord,
        *,
        side: str,
        quantity: Decimal,
        price: Decimal,
        at_ns: int,
        liquidity: Liquidity = Liquidity.TAKER,
        reference_price: Decimal | None = None,
    ) -> LegEconomics:
        fee = self._fee_book.resolve(instrument, liquidity, at_ns)
        notional = quantity * price
        reference = reference_price if reference_price is not None else price
        if reference > 0:
            signed = (
                (price - reference) if side.lower() == "buy" else (reference - price)
            )
            slippage_bps = signed / reference * BPS
        else:
            slippage_bps = Decimal(0)
        return LegEconomics(
            instrument_id=instrument.instrument_id,
            venue=instrument.venue,
            side=side.lower(),
            quantity=quantity,
            price=price,
            notional=notional,
            fee=fee.charge(notional),
            fee_bps=fee.effective_bps,
            slippage_bps=slippage_bps,
            liquidity=liquidity,
        )

    def expected_edge(
        self,
        buy_instrument: InstrumentRecord,
        sell_instrument: InstrumentRecord,
        *,
        buy_price: Decimal,
        sell_price: Decimal,
        quantity: Decimal,
        at_ns: int,
        liquidity: Liquidity = Liquidity.TAKER,
        latency_penalty_bps: Decimal = Decimal(0),
    ) -> OpportunityEconomics:
        """Route-decision economics; identical arithmetic is reused for TCA."""

        buy_leg = self.leg_economics(
            buy_instrument,
            side="buy",
            quantity=quantity,
            price=buy_price,
            at_ns=at_ns,
            liquidity=liquidity,
        )
        sell_leg = self.leg_economics(
            sell_instrument,
            side="sell",
            quantity=quantity,
            price=sell_price,
            at_ns=at_ns,
            liquidity=liquidity,
        )
        gross = (sell_price - buy_price) * quantity
        fees = buy_leg.fee + sell_leg.fee
        latency_cost = (
            (buy_leg.notional + sell_leg.notional) * latency_penalty_bps / BPS
        )
        net = gross - fees - latency_cost
        return OpportunityEconomics(
            gross_edge=gross,
            fees=fees,
            slippage_cost=latency_cost,
            funding_cost=Decimal(0),
            unwind_cost=Decimal(0),
            net_pnl=net,
            legs=(buy_leg, sell_leg),
        )

    def realised(
        self,
        legs: Sequence[LegEconomics],
        *,
        reference_edge: Decimal,
        funding_cost: Decimal = Decimal(0),
        unwind_cost: Decimal = Decimal(0),
    ) -> OpportunityEconomics:
        """Realised economics from actual fills, attributed to one opportunity."""

        gross = Decimal(0)
        fees = Decimal(0)
        slippage_cost = Decimal(0)
        for leg in legs:
            direction = Decimal(-1) if leg.side == "buy" else Decimal(1)
            gross += direction * leg.notional
            fees += leg.fee
            slippage_cost += abs(leg.notional * leg.slippage_bps / BPS)
        net = gross - fees - funding_cost - unwind_cost
        return OpportunityEconomics(
            gross_edge=gross,
            fees=fees,
            slippage_cost=slippage_cost,
            funding_cost=funding_cost,
            unwind_cost=unwind_cost,
            net_pnl=net,
            legs=tuple(legs),
        )


def default_fee_book() -> FeeBook:
    """Reference schedule with a mid-run version bump to prove effective dating."""

    return FeeBook(
        [
            FeeRecord(
                venue="binance",
                account_tier="default",
                maker_bps=Decimal("1.0"),
                taker_bps=Decimal("1.0"),
                maker_rebate_bps=Decimal("0"),
                effective_from_ns=0,
                version=1,
            ),
            FeeRecord(
                venue="okx",
                account_tier="default",
                maker_bps=Decimal("0.8"),
                taker_bps=Decimal("1.0"),
                maker_rebate_bps=Decimal("0"),
                effective_from_ns=0,
                version=1,
            ),
            FeeRecord(
                venue="bybit",
                account_tier="default",
                maker_bps=Decimal("1.0"),
                taker_bps=Decimal("1.0"),
                maker_rebate_bps=Decimal("0"),
                effective_from_ns=0,
                version=1,
            ),
            FeeRecord(
                venue="paper",
                account_tier="default",
                maker_bps=Decimal("1.0"),
                taker_bps=Decimal("1.0"),
                effective_from_ns=0,
                version=1,
            ),
            # Effective-dated bump: from 2026-01-01 the binance taker fee
            # drops. Resolving a fee before that instant must still return
            # version 1, which is what makes historical replays reproducible.
            FeeRecord(
                venue="binance",
                account_tier="default",
                maker_bps=Decimal("0.9"),
                taker_bps=Decimal("0.9"),
                maker_rebate_bps=Decimal("0"),
                effective_from_ns=1_767_225_600_000_000_000,
                version=2,
            ),
        ],
        [],
    )
