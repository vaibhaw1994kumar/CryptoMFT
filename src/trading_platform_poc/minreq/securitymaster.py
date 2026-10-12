"""Effective-dated instrument and security master (MR-03, AT-04).

Symbol strings are not portable across venues, so every order, position and
TCA row keys off a stable internal instrument id. Records are effective dated:
the rules that applied at the market event timestamp are the rules used, not
whatever the venue publishes today.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_UP
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

import yaml


class ProductType(str, Enum):
    SPOT = "spot"
    PERPETUAL = "perpetual"
    FUTURE = "future"


class QuantityUnit(str, Enum):
    BASE = "base"
    QUOTE = "quote"
    CONTRACTS = "contracts"


class TradingStatus(str, Enum):
    PRE_OPEN = "pre_open"
    TRADING = "trading"
    SUSPENDED = "suspended"
    MAINTENANCE = "maintenance"
    DELISTED = "delisted"
    EXPIRED = "expired"


TRADABLE_STATUSES = frozenset({TradingStatus.TRADING})
RISK_REDUCING_ONLY_STATUSES = frozenset(
    {TradingStatus.SUSPENDED, TradingStatus.MAINTENANCE}
)

DAY_NS = 86_400_000_000_000


class InstrumentError(ValueError):
    """Raised when an instrument cannot be resolved or sized."""


@dataclass(frozen=True, slots=True)
class InstrumentRecord:
    """One effective-dated version of one venue instrument."""

    instrument_id: str
    venue: str
    venue_symbol: str
    economic_group: str
    product_type: ProductType
    base_currency: str
    quote_currency: str
    settlement_currency: str
    tick_size: Decimal
    quantity_step: Decimal
    minimum_quantity: Decimal
    minimum_notional: Decimal
    quantity_unit: QuantityUnit = QuantityUnit.BASE
    contract_multiplier: Decimal = Decimal(1)
    inverse: bool = False
    maximum_quantity: Decimal | None = None
    price_band_bps: Decimal | None = None
    supported_order_types: tuple[str, ...] = ("market", "limit", "post_only")
    supported_time_in_force: tuple[str, ...] = ("gtc", "ioc", "fok")
    status: TradingStatus = TradingStatus.TRADING
    effective_from_ns: int = 0
    version: int = 1
    source: str = "static"
    expiry_timestamp_ns: int | None = None

    @property
    def tradable(self) -> bool:
        return self.status in TRADABLE_STATUSES

    @property
    def risk_reducing_only(self) -> bool:
        return self.status in RISK_REDUCING_ONLY_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "venue": self.venue,
            "venue_symbol": self.venue_symbol,
            "economic_group": self.economic_group,
            "product_type": self.product_type.value,
            "base_currency": self.base_currency,
            "quote_currency": self.quote_currency,
            "settlement_currency": self.settlement_currency,
            "tick_size": str(self.tick_size),
            "quantity_step": str(self.quantity_step),
            "minimum_quantity": str(self.minimum_quantity),
            "minimum_notional": str(self.minimum_notional),
            "quantity_unit": self.quantity_unit.value,
            "contract_multiplier": str(self.contract_multiplier),
            "inverse": self.inverse,
            "maximum_quantity": (
                None if self.maximum_quantity is None else str(self.maximum_quantity)
            ),
            "price_band_bps": (
                None if self.price_band_bps is None else str(self.price_band_bps)
            ),
            "supported_order_types": list(self.supported_order_types),
            "supported_time_in_force": list(self.supported_time_in_force),
            "status": self.status.value,
            "effective_from_ns": self.effective_from_ns,
            "version": self.version,
            "source": self.source,
            "expiry_timestamp_ns": self.expiry_timestamp_ns,
        }


@dataclass(frozen=True, slots=True)
class Exposure:
    """Order economics expressed in every unit the platform must reconcile."""

    native_quantity: Decimal
    base_exposure: Decimal
    quote_notional: Decimal
    settlement_exposure: Decimal
    settlement_currency: str


@dataclass(frozen=True, slots=True)
class SizingResult:
    """Outcome of validating and rounding one leg against the security master."""

    instrument: InstrumentRecord
    eligible: bool
    reason: str
    requested_quantity: Decimal
    quantity: Decimal
    price: Decimal | None
    exposure: Exposure | None
    decisions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument.instrument_id,
            "eligible": self.eligible,
            "reason": self.reason,
            "requested_quantity": str(self.requested_quantity),
            "quantity": str(self.quantity),
            "price": None if self.price is None else str(self.price),
            "base_exposure": (
                None if self.exposure is None else str(self.exposure.base_exposure)
            ),
            "quote_notional": (
                None if self.exposure is None else str(self.exposure.quote_notional)
            ),
            "settlement_exposure": (
                None
                if self.exposure is None
                else str(self.exposure.settlement_exposure)
            ),
            "decisions": list(self.decisions),
        }


def round_to_step(value: Decimal, step: Decimal, *, up: bool = False) -> Decimal:
    if step <= 0:
        raise InstrumentError("step must be positive")
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    return (value / step).to_integral_value(rounding=rounding) * step


def round_price_to_tick(price: Decimal, tick: Decimal, side: str) -> Decimal:
    """Round conservatively: buys never round up, sells never round down."""

    if tick <= 0:
        raise InstrumentError("tick size must be positive")
    rounding = ROUND_DOWN if side.lower() == "buy" else ROUND_UP
    return (price / tick).to_integral_value(rounding=rounding) * tick


class SecurityMaster:
    """Versioned, effective-dated instrument store."""

    def __init__(self, records: Iterable[InstrumentRecord]) -> None:
        self._by_instrument: dict[str, list[InstrumentRecord]] = {}
        self._venue_symbol_index: dict[tuple[str, str], str] = {}
        self._groups: dict[str, set[str]] = {}
        for record in records:
            self._add(record)
        for versions in self._by_instrument.values():
            versions.sort(key=lambda item: (item.effective_from_ns, item.version))
        self._effective_index = {
            instrument_id: [item.effective_from_ns for item in versions]
            for instrument_id, versions in self._by_instrument.items()
        }

    def _add(self, record: InstrumentRecord) -> None:
        self._by_instrument.setdefault(record.instrument_id, []).append(record)
        self._venue_symbol_index[
            (record.venue.lower(), record.venue_symbol.upper())
        ] = record.instrument_id
        self._groups.setdefault(record.economic_group, set()).add(
            record.instrument_id
        )

    # ---------------------------------------------------------------- lookup
    @property
    def instrument_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_instrument))

    @property
    def economic_groups(self) -> tuple[str, ...]:
        return tuple(sorted(self._groups))

    def group_members(self, economic_group: str) -> tuple[str, ...]:
        return tuple(sorted(self._groups.get(economic_group, ())))

    def versions(self, instrument_id: str) -> tuple[InstrumentRecord, ...]:
        return tuple(self._by_instrument.get(instrument_id, ()))

    def resolve(self, instrument_id: str, at_ns: int) -> InstrumentRecord:
        versions = self._by_instrument.get(instrument_id)
        if not versions:
            raise InstrumentError(f"unknown instrument: {instrument_id}")
        index = bisect_right(self._effective_index[instrument_id], at_ns) - 1
        if index < 0:
            raise InstrumentError(
                f"instrument {instrument_id} is not effective at {at_ns}"
            )
        return versions[index]

    def resolve_venue_symbol(
        self, venue: str, venue_symbol: str, at_ns: int
    ) -> InstrumentRecord:
        instrument_id = self._venue_symbol_index.get(
            (venue.lower(), venue_symbol.upper())
        )
        if instrument_id is None:
            raise InstrumentError(f"unknown venue symbol: {venue}:{venue_symbol}")
        return self.resolve(instrument_id, at_ns)

    def instrument_id_for(self, venue: str, venue_symbol: str) -> str | None:
        return self._venue_symbol_index.get((venue.lower(), venue_symbol.upper()))

    def snapshot(self, at_ns: int) -> tuple[InstrumentRecord, ...]:
        resolved = []
        for instrument_id in self.instrument_ids:
            try:
                resolved.append(self.resolve(instrument_id, at_ns))
            except InstrumentError:
                continue
        return tuple(resolved)

    def with_status(
        self,
        instrument_id: str,
        status: TradingStatus,
        effective_from_ns: int,
        *,
        source: str = "lifecycle_event",
    ) -> SecurityMaster:
        """Return a new master carrying an added lifecycle/status version."""

        current = self.resolve(instrument_id, effective_from_ns)
        updated = replace(
            current,
            status=status,
            effective_from_ns=effective_from_ns,
            version=current.version + 1,
            source=source,
        )
        records = [
            record
            for versions in self._by_instrument.values()
            for record in versions
        ]
        records.append(updated)
        return SecurityMaster(records)

    # ---------------------------------------------------------------- sizing
    def exposure(
        self,
        instrument: InstrumentRecord,
        quantity: Decimal,
        price: Decimal,
    ) -> Exposure:
        """Express one native quantity as base, quote and settlement exposure."""

        multiplier = instrument.contract_multiplier
        if instrument.quantity_unit is QuantityUnit.BASE:
            base = quantity
            quote = quantity * price
        elif instrument.quantity_unit is QuantityUnit.QUOTE:
            quote = quantity
            base = quantity / price if price > 0 else Decimal(0)
        else:  # contracts
            if instrument.inverse:
                quote = quantity * multiplier
                base = quote / price if price > 0 else Decimal(0)
            else:
                base = quantity * multiplier
                quote = base * price
        settlement = base if instrument.inverse else quote
        return Exposure(
            native_quantity=quantity,
            base_exposure=base,
            quote_notional=quote,
            settlement_exposure=settlement,
            settlement_currency=instrument.settlement_currency,
        )

    def size_order(
        self,
        instrument_id: str,
        *,
        at_ns: int,
        side: str,
        target_quantity: Decimal,
        price: Decimal | None,
        reference_price: Decimal | None = None,
        order_type: str = "limit",
        time_in_force: str = "ioc",
        allow_risk_reducing: bool = False,
    ) -> SizingResult:
        """Validate and round a leg. Every decision is recorded for the audit."""

        instrument = self.resolve(instrument_id, at_ns)
        decisions: list[str] = [
            f"instrument_version={instrument.version}",
            f"effective_from_ns={instrument.effective_from_ns}",
            f"status={instrument.status.value}",
        ]

        def fail(reason: str) -> SizingResult:
            return SizingResult(
                instrument=instrument,
                eligible=False,
                reason=reason,
                requested_quantity=target_quantity,
                quantity=Decimal(0),
                price=price,
                exposure=None,
                decisions=tuple(decisions),
            )

        if instrument.status is TradingStatus.DELISTED:
            return fail("instrument_delisted")
        if (
            instrument.expiry_timestamp_ns is not None
            and at_ns >= instrument.expiry_timestamp_ns
        ):
            return fail("instrument_expired")
        if not instrument.tradable:
            if not (instrument.risk_reducing_only and allow_risk_reducing):
                return fail(f"instrument_status_{instrument.status.value}")
            decisions.append("risk_reducing_only=true")
        if order_type.lower() not in instrument.supported_order_types:
            return fail(f"order_type_unsupported_{order_type.lower()}")
        if time_in_force.lower() not in instrument.supported_time_in_force:
            return fail(f"time_in_force_unsupported_{time_in_force.lower()}")

        effective_price = price if price is not None else reference_price
        if effective_price is None or effective_price <= 0:
            return fail("missing_reference_price")

        rounded_price: Decimal | None = None
        if price is not None:
            rounded_price = round_price_to_tick(price, instrument.tick_size, side)
            if rounded_price != price:
                decisions.append(f"price_rounded={price}->{rounded_price}")
            if rounded_price <= 0:
                return fail("price_rounds_to_zero")
            effective_price = rounded_price

        if target_quantity <= 0:
            return fail("non_positive_quantity")
        quantity = round_to_step(target_quantity, instrument.quantity_step)
        if quantity != target_quantity:
            decisions.append(f"quantity_rounded={target_quantity}->{quantity}")
        if quantity <= 0:
            return fail("quantity_rounds_to_zero")
        if quantity < instrument.minimum_quantity:
            return fail("below_minimum_quantity")
        if (
            instrument.maximum_quantity is not None
            and quantity > instrument.maximum_quantity
        ):
            return fail("above_maximum_quantity")

        exposure = self.exposure(instrument, quantity, effective_price)
        if exposure.quote_notional < instrument.minimum_notional:
            return fail("below_minimum_notional")

        if (
            instrument.price_band_bps is not None
            and reference_price is not None
            and reference_price > 0
            and rounded_price is not None
        ):
            deviation = (
                abs(rounded_price - reference_price) / reference_price * Decimal(10_000)
            )
            decisions.append(f"price_band_deviation_bps={deviation:.4f}")
            if deviation > instrument.price_band_bps:
                return fail("price_band_breach")

        return SizingResult(
            instrument=instrument,
            eligible=True,
            reason="eligible",
            requested_quantity=target_quantity,
            quantity=quantity,
            price=rounded_price,
            exposure=exposure,
            decisions=tuple(decisions),
        )

    # -------------------------------------------------------------- loading
    def to_documents(self) -> list[dict[str, Any]]:
        return [
            record.to_dict()
            for instrument_id in self.instrument_ids
            for record in self._by_instrument[instrument_id]
        ]

    @classmethod
    def from_documents(cls, documents: Iterable[dict[str, Any]]) -> SecurityMaster:
        return cls(_record_from_document(document) for document in documents)

    @classmethod
    def from_yaml(cls, path: Path) -> SecurityMaster:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        documents = raw["instruments"] if isinstance(raw, dict) else raw
        return cls.from_documents(documents)

    def write_yaml(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                {"instruments": self.to_documents()},
                sort_keys=False,
                default_flow_style=False,
            ),
            encoding="utf-8",
        )
        return path


def _record_from_document(document: dict[str, Any]) -> InstrumentRecord:
    def decimal_or_none(key: str) -> Decimal | None:
        value = document.get(key)
        return None if value is None else Decimal(str(value))

    return InstrumentRecord(
        instrument_id=str(document["instrument_id"]),
        venue=str(document["venue"]).lower(),
        venue_symbol=str(document["venue_symbol"]).upper(),
        economic_group=str(document["economic_group"]),
        product_type=ProductType(document.get("product_type", "spot")),
        base_currency=str(document["base_currency"]).upper(),
        quote_currency=str(document["quote_currency"]).upper(),
        settlement_currency=str(
            document.get("settlement_currency", document["quote_currency"])
        ).upper(),
        tick_size=Decimal(str(document["tick_size"])),
        quantity_step=Decimal(str(document["quantity_step"])),
        minimum_quantity=Decimal(str(document["minimum_quantity"])),
        minimum_notional=Decimal(str(document["minimum_notional"])),
        quantity_unit=QuantityUnit(document.get("quantity_unit", "base")),
        contract_multiplier=Decimal(str(document.get("contract_multiplier", 1))),
        inverse=bool(document.get("inverse", False)),
        maximum_quantity=decimal_or_none("maximum_quantity"),
        price_band_bps=decimal_or_none("price_band_bps"),
        supported_order_types=tuple(
            document.get("supported_order_types", ("market", "limit", "post_only"))
        ),
        supported_time_in_force=tuple(
            document.get("supported_time_in_force", ("gtc", "ioc", "fok"))
        ),
        status=TradingStatus(document.get("status", "trading")),
        effective_from_ns=int(document.get("effective_from_ns", 0)),
        version=int(document.get("version", 1)),
        source=str(document.get("source", "static")),
        expiry_timestamp_ns=(
            None
            if document.get("expiry_timestamp_ns") is None
            else int(document["expiry_timestamp_ns"])
        ),
    )


def _spot(
    instrument_id: str,
    venue: str,
    venue_symbol: str,
    group: str,
    base: str,
    quote: str,
    tick: str,
    step: str,
    minimum_quantity: str,
    minimum_notional: str,
) -> InstrumentRecord:
    return InstrumentRecord(
        instrument_id=instrument_id,
        venue=venue,
        venue_symbol=venue_symbol,
        economic_group=group,
        product_type=ProductType.SPOT,
        base_currency=base,
        quote_currency=quote,
        settlement_currency=quote,
        tick_size=Decimal(tick),
        quantity_step=Decimal(step),
        minimum_quantity=Decimal(minimum_quantity),
        minimum_notional=Decimal(minimum_notional),
        price_band_bps=Decimal("500"),
        status=TradingStatus.TRADING,
        effective_from_ns=0,
        version=1,
        source="reference_default",
    )


def default_security_master() -> SecurityMaster:
    """Reference universe: two economically comparable spot pairs, three venues.

    Deliberately narrow, as section 4 permits, so queue and impact uncertainty
    stay low while still exercising cross-venue tick, lot and notional rules.
    """

    return SecurityMaster(
        [
            _spot(
                "BTC-USDT.BINANCE.SPOT",
                "binance",
                "BTCUSDT",
                "BTC/USDT.SPOT",
                "BTC",
                "USDT",
                "0.01",
                "0.00001",
                "0.00001",
                "5",
            ),
            _spot(
                "BTC-USDT.OKX.SPOT",
                "okx",
                "BTC-USDT",
                "BTC/USDT.SPOT",
                "BTC",
                "USDT",
                "0.1",
                "0.00000001",
                "0.00001",
                "1",
            ),
            _spot(
                "BTC-USDT.BYBIT.SPOT",
                "bybit",
                "BTCUSDT",
                "BTC/USDT.SPOT",
                "BTC",
                "USDT",
                "0.01",
                "0.000001",
                "0.000048",
                "1",
            ),
            _spot(
                "ETH-USDT.BINANCE.SPOT",
                "binance",
                "ETHUSDT",
                "ETH/USDT.SPOT",
                "ETH",
                "USDT",
                "0.01",
                "0.0001",
                "0.0001",
                "5",
            ),
            _spot(
                "ETH-USDT.OKX.SPOT",
                "okx",
                "ETH-USDT",
                "ETH/USDT.SPOT",
                "ETH",
                "USDT",
                "0.01",
                "0.000001",
                "0.0001",
                "1",
            ),
            _spot(
                "ETH-USDT.BYBIT.SPOT",
                "bybit",
                "ETHUSDT",
                "ETH/USDT.SPOT",
                "ETH",
                "USDT",
                "0.01",
                "0.00001",
                "0.00062",
                "1",
            ),
        ]
    )


DEFAULT_ECONOMIC_GROUP = "BTC/USDT.SPOT"
