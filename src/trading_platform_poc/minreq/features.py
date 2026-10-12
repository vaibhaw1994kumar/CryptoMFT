"""Versioned cross-venue features with an explicit no-lookahead guard (MR-06).

Every feature carries a definition version and a lookback. A snapshot records
the exact input timestamps it consumed, and the engine refuses to produce a
snapshot from any input newer than the requested as-of time, which is what
makes the forward-looking-join test in section 5 mechanical rather than a
review opinion.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from trading_platform_poc.minreq.canonical import (
    BookValidity,
    CanonicalEvent,
    CanonicalEventType,
)
from trading_platform_poc.minreq.quality import CanonicalBook

FEATURE_SET_VERSION = "1.2.0"


class LookaheadError(RuntimeError):
    """Raised when a feature would consume an observation from the future."""


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    name: str
    version: str
    lookback_ns: int
    description: str
    inputs: tuple[str, ...]
    normalization: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "lookback_ns": self.lookback_ns,
            "description": self.description,
            "inputs": list(self.inputs),
            "normalization": self.normalization,
        }


@dataclass(frozen=True, slots=True)
class VenueView:
    """What the strategy is allowed to know about one venue at as-of time."""

    venue: str
    instrument_id: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    mid: Decimal | None
    spread_bps: Decimal | None
    validity: BookValidity
    last_update_ns: int
    data_age_ns: int
    book_hash: str

    @property
    def healthy(self) -> bool:
        return (
            self.validity is BookValidity.VALID
            and self.mid is not None
            and self.mid > 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "instrument_id": self.instrument_id,
            "best_bid": None if self.best_bid is None else str(self.best_bid),
            "best_ask": None if self.best_ask is None else str(self.best_ask),
            "mid": None if self.mid is None else str(self.mid),
            "spread_bps": None if self.spread_bps is None else str(self.spread_bps),
            "validity": self.validity.value,
            "last_update_ns": self.last_update_ns,
            "data_age_ns": self.data_age_ns,
            "healthy": self.healthy,
            "book_hash": self.book_hash,
        }


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    """Immutable feature vector with full provenance."""

    feature_id: str
    as_of_ns: int
    feature_set_version: str
    economic_group: str
    values: dict[str, Decimal | None]
    views: dict[str, VenueView]
    sample_count: int
    healthy: bool
    health_reasons: tuple[str, ...]
    max_data_age_ns: int

    def value(self, name: str) -> Decimal | None:
        return self.values.get(name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "as_of_ns": self.as_of_ns,
            "feature_set_version": self.feature_set_version,
            "economic_group": self.economic_group,
            "values": {
                key: (None if value is None else str(value))
                for key, value in sorted(self.values.items())
            },
            "views": {
                venue: view.to_dict() for venue, view in sorted(self.views.items())
            },
            "sample_count": self.sample_count,
            "healthy": self.healthy,
            "health_reasons": list(self.health_reasons),
            "max_data_age_ns": self.max_data_age_ns,
        }


FEATURE_DEFINITIONS: tuple[FeatureDefinition, ...] = (
    FeatureDefinition(
        name="mid",
        version="1.0.0",
        lookback_ns=0,
        description="Top-of-book mid price per venue at as-of time.",
        inputs=("book_snapshot", "book_delta"),
    ),
    FeatureDefinition(
        name="basis",
        version="1.1.0",
        lookback_ns=0,
        description="Rich venue mid minus cheap venue mid, in quote currency.",
        inputs=("mid",),
    ),
    FeatureDefinition(
        name="basis_bps",
        version="1.1.0",
        lookback_ns=0,
        description="Basis expressed in basis points of the reference mid.",
        inputs=("basis", "mid"),
    ),
    FeatureDefinition(
        name="basis_mean",
        version="1.2.0",
        lookback_ns=30_000_000_000,
        description="Rolling mean of basis_bps over the configured lookback.",
        inputs=("basis_bps",),
        normalization="rolling_mean",
    ),
    FeatureDefinition(
        name="basis_stdev",
        version="1.2.0",
        lookback_ns=30_000_000_000,
        description="Rolling sample standard deviation of basis_bps.",
        inputs=("basis_bps",),
        normalization="rolling_stdev",
    ),
    FeatureDefinition(
        name="basis_zscore",
        version="1.2.0",
        lookback_ns=30_000_000_000,
        description="Standardised basis used as the mean-reversion signal.",
        inputs=("basis_bps", "basis_mean", "basis_stdev"),
        normalization="zscore",
    ),
)


def feature_set_hash() -> str:
    document = json.dumps(
        [definition.to_dict() for definition in FEATURE_DEFINITIONS],
        sort_keys=True,
    )
    return hashlib.sha256(document.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class FeatureConfig:
    economic_group: str
    lookback_ns: int = 30_000_000_000
    min_samples: int = 20
    max_data_age_ns: int = 1_000_000_000
    max_samples: int = 5_000

    def to_dict(self) -> dict[str, Any]:
        return {
            "economic_group": self.economic_group,
            "lookback_ns": self.lookback_ns,
            "min_samples": self.min_samples,
            "max_data_age_ns": self.max_data_age_ns,
            "max_samples": self.max_samples,
        }


class CrossVenueFeatureEngine:
    """Maintains aligned venue books and the rolling basis statistics."""

    def __init__(
        self,
        config: FeatureConfig,
        venue_instruments: Mapping[str, str],
        *,
        stale_after_ns: int = 2_000_000_000,
    ) -> None:
        if len(venue_instruments) < 2:
            raise ValueError("cross-venue features need at least two venues")
        self.config = config
        self.venue_instruments = dict(sorted(venue_instruments.items()))
        self._books: dict[str, CanonicalBook] = {
            venue: CanonicalBook(
                venue, instrument_id, stale_after_ns=stale_after_ns
            )
            for venue, instrument_id in self.venue_instruments.items()
        }
        self._samples: deque[tuple[int, Decimal]] = deque()
        self._last_observed_ns = 0
        self._suspended: set[str] = set()

    @property
    def venues(self) -> tuple[str, ...]:
        return tuple(self.venue_instruments)

    @property
    def books(self) -> dict[str, CanonicalBook]:
        return self._books

    @property
    def reference_venue(self) -> str:
        return self.venues[0]

    @property
    def quote_venue(self) -> str:
        return self.venues[1]

    def book(self, venue: str) -> CanonicalBook:
        return self._books[venue]

    def suspend(self, venue: str) -> None:
        self._suspended.add(venue)

    def resume(self, venue: str) -> None:
        self._suspended.discard(venue)

    def observe(self, event: CanonicalEvent) -> None:
        """Absorb one canonical event. Only past information ever enters."""

        if event.venue not in self._books:
            return
        if event.instrument_id != self.venue_instruments[event.venue]:
            return
        self._last_observed_ns = max(
            self._last_observed_ns, event.receive_timestamp_ns
        )
        if event.event_type in {
            CanonicalEventType.BOOK_SNAPSHOT,
            CanonicalEventType.BOOK_DELTA,
        }:
            book = self._books[event.venue]
            book.apply(event)
            self._append_sample(event.receive_timestamp_ns)

    def _append_sample(self, as_of_ns: int) -> None:
        basis = self._raw_basis()
        if basis is None:
            return
        self._samples.append((as_of_ns, basis))
        horizon = as_of_ns - self.config.lookback_ns
        while self._samples and self._samples[0][0] < horizon:
            self._samples.popleft()
        while len(self._samples) > self.config.max_samples:
            self._samples.popleft()

    def _raw_basis(self) -> Decimal | None:
        reference = self._books[self.reference_venue].mid_price
        quote = self._books[self.quote_venue].mid_price
        if reference is None or quote is None or reference <= 0:
            return None
        return (quote - reference) / reference * Decimal(10_000)

    def views(self, as_of_ns: int) -> dict[str, VenueView]:
        views: dict[str, VenueView] = {}
        for venue, book in self._books.items():
            if book.last_update_ns > as_of_ns:
                raise LookaheadError(
                    f"{venue} book was updated at {book.last_update_ns}, "
                    f"which is after the as-of timestamp {as_of_ns}"
                )
            validity = book.validity
            if venue in self._suspended:
                validity = BookValidity.INVALID
            age = as_of_ns - book.last_update_ns if book.initialized else as_of_ns
            if (
                validity is BookValidity.VALID
                and age > self.config.max_data_age_ns
            ):
                validity = BookValidity.STALE
            views[venue] = VenueView(
                venue=venue,
                instrument_id=book.instrument_id,
                best_bid=book.best_bid,
                best_ask=book.best_ask,
                mid=book.mid_price,
                spread_bps=book.spread_bps,
                validity=validity,
                last_update_ns=book.last_update_ns,
                data_age_ns=age,
                book_hash=book.book_hash() if book.initialized else "",
            )
        return views

    def snapshot(self, as_of_ns: int) -> FeatureSnapshot:
        """Compute features as of ``as_of_ns`` using only prior observations."""

        if as_of_ns < self._last_observed_ns:
            raise LookaheadError(
                f"as-of timestamp {as_of_ns} precedes the last observation "
                f"{self._last_observed_ns}; features would be non-causal"
            )
        views = self.views(as_of_ns)
        samples = [
            value for timestamp, value in self._samples if timestamp <= as_of_ns
        ]
        values: dict[str, Decimal | None] = {}
        for venue, view in views.items():
            values[f"mid.{venue}"] = view.mid
            values[f"spread_bps.{venue}"] = view.spread_bps
            values[f"data_age_ns.{venue}"] = Decimal(view.data_age_ns)

        basis_bps = samples[-1] if samples else None
        reference_mid = views[self.reference_venue].mid
        quote_mid = views[self.quote_venue].mid
        values["basis_bps"] = basis_bps
        values["basis"] = (
            None
            if reference_mid is None or quote_mid is None
            else quote_mid - reference_mid
        )

        mean = stdev = zscore = None
        if len(samples) >= 2:
            count = Decimal(len(samples))
            mean = sum(samples, Decimal(0)) / count
            variance = sum(((value - mean) ** 2 for value in samples), Decimal(0)) / (
                count - 1
            )
            stdev = variance.sqrt() if variance > 0 else Decimal(0)
            if stdev > 0 and basis_bps is not None:
                zscore = (basis_bps - mean) / stdev
        values["basis_mean"] = mean
        values["basis_stdev"] = stdev
        values["basis_zscore"] = zscore

        reasons: list[str] = []
        for venue, view in views.items():
            if view.validity is not BookValidity.VALID:
                reasons.append(f"{venue}_book_{view.validity.value}")
            elif view.data_age_ns > self.config.max_data_age_ns:
                reasons.append(f"{venue}_data_age")
        if len(samples) < self.config.min_samples:
            reasons.append(f"warmup:{len(samples)}<{self.config.min_samples}")
        if zscore is None:
            reasons.append("zscore_unavailable")

        max_age = max((view.data_age_ns for view in views.values()), default=0)
        feature_id = hashlib.sha256(
            "|".join(
                (
                    self.config.economic_group,
                    str(as_of_ns),
                    FEATURE_SET_VERSION,
                    feature_set_hash(),
                    *(
                        f"{key}={values[key]}"
                        for key in sorted(values)
                    ),
                )
            ).encode("utf-8")
        ).hexdigest()[:32]

        return FeatureSnapshot(
            feature_id=feature_id,
            as_of_ns=as_of_ns,
            feature_set_version=FEATURE_SET_VERSION,
            economic_group=self.config.economic_group,
            values=values,
            views=views,
            sample_count=len(samples),
            healthy=not reasons,
            health_reasons=tuple(reasons),
            max_data_age_ns=max_age,
        )


def feature_catalogue() -> dict[str, Any]:
    """Section 10 deliverable: versioned feature definitions."""

    return {
        "feature_set_version": FEATURE_SET_VERSION,
        "feature_set_hash": feature_set_hash(),
        "definitions": [
            definition.to_dict() for definition in FEATURE_DEFINITIONS
        ],
    }
