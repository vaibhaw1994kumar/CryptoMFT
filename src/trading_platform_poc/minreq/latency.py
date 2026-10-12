"""Seeded latency model with explicit tail behaviour (MR-05, section 5).

Latency is configurable per venue and per stage. Draws come from a dedicated
seeded generator so a replay is reproducible from the run manifest, and the
tail is modelled separately rather than being smoothed away by a single mean.

Stages that decide *when an order reaches the matching engine* are separate
from the stages that decide *when we hear about it*:

* ``entry``      our send -> the venue's matching engine (the order executes
                 against the book as it is at this moment);
* ``submit_ack`` matching engine -> our acknowledgement;
* ``fill``       matching engine -> our fill report;
* ``cancel``     our cancel send -> the venue applies it.

Per-venue presets and lognormal sampling come from ``new_backtest`` (itself
derived from ``altcoin_mm_v1/latency_model.py``) so both backtesters share one
set of measured co-location assumptions.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Mapping

MS_NS = 1_000_000


@dataclass(frozen=True, slots=True)
class LatencyDistribution:
    """Base + jitter with an explicit heavy tail.

    ``model="uniform"`` draws ``base + U(0, jitter)``. ``model="lognormal"``
    treats ``base_ns`` as the mean and ``std_ns`` as the standard deviation of
    the raw latency (right-skewed and strictly positive, as exchange RTTs are),
    clamped to ``[min_ns, max_ns]``. The explicit tail applies to both.
    """

    base_ns: int
    jitter_ns: int = 0
    tail_probability: float = 0.0
    tail_ns: int = 0
    model: str = "uniform"
    std_ns: int = 0
    min_ns: int = 0
    max_ns: int = 0

    def sample(self, rng: random.Random) -> int:
        if self.model == "lognormal" and self.base_ns > 0 and self.std_ns > 0:
            sigma2 = math.log(1.0 + (self.std_ns / self.base_ns) ** 2)
            mu = math.log(self.base_ns) - sigma2 / 2.0
            value = int(math.exp(rng.gauss(mu, math.sqrt(sigma2))))
        else:
            value = self.base_ns
            if self.jitter_ns > 0:
                value += rng.randint(0, self.jitter_ns)
        if self.tail_probability > 0 and rng.random() < self.tail_probability:
            value += self.tail_ns
        if self.max_ns > 0:
            value = min(value, self.max_ns)
        return max(self.min_ns, value, 0)

    @property
    def mean_ns(self) -> int:
        body = (
            self.base_ns
            if self.model == "lognormal"
            else self.base_ns + self.jitter_ns // 2
        )
        return int(body + self.tail_probability * self.tail_ns)

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_ns": self.base_ns,
            "jitter_ns": self.jitter_ns,
            "tail_probability": self.tail_probability,
            "tail_ns": self.tail_ns,
            "model": self.model,
            "std_ns": self.std_ns,
            "min_ns": self.min_ns,
            "max_ns": self.max_ns,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> LatencyDistribution:
        return cls(
            base_ns=int(document.get("base_ns", 0)),
            jitter_ns=int(document.get("jitter_ns", 0)),
            tail_probability=float(document.get("tail_probability", 0.0)),
            tail_ns=int(document.get("tail_ns", 0)),
            model=str(document.get("model", "uniform")),
            std_ns=int(document.get("std_ns", 0)),
            min_ns=int(document.get("min_ns", 0)),
            max_ns=int(document.get("max_ns", 0)),
        )


def lognormal_ms(mean_ms: float, std_ms: float) -> LatencyDistribution:
    """Lognormal stage with new_backtest's clamps (0.3 ms to 2 s)."""

    return LatencyDistribution(
        base_ns=int(mean_ms * MS_NS),
        model="lognormal",
        std_ns=int(std_ms * MS_NS),
        min_ns=int(0.3 * MS_NS),
        max_ns=2_000 * MS_NS,
    )


@dataclass(frozen=True, slots=True)
class VenueLatencyProfile:
    """Every stage the specification asks to be configurable per venue."""

    venue: str
    market_data: LatencyDistribution
    strategy: LatencyDistribution
    risk: LatencyDistribution
    submit_ack: LatencyDistribution
    cancel: LatencyDistribution
    fill: LatencyDistribution
    entry: LatencyDistribution = field(
        default_factory=lambda: LatencyDistribution(base_ns=12 * MS_NS)
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "market_data": self.market_data.to_dict(),
            "strategy": self.strategy.to_dict(),
            "risk": self.risk.to_dict(),
            "entry": self.entry.to_dict(),
            "submit_ack": self.submit_ack.to_dict(),
            "cancel": self.cancel.to_dict(),
            "fill": self.fill.to_dict(),
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> VenueLatencyProfile:
        def stage(name: str, default_ms: int) -> LatencyDistribution:
            raw = document.get(name)
            if raw is None:
                return LatencyDistribution(base_ns=default_ms * MS_NS)
            return LatencyDistribution.from_dict(raw)

        return cls(
            venue=str(document["venue"]),
            market_data=stage("market_data", 1),
            strategy=stage("strategy", 0),
            risk=stage("risk", 0),
            entry=stage("entry", 12),
            submit_ack=stage("submit_ack", 10),
            cancel=stage("cancel", 20),
            fill=stage("fill", 5),
        )


def default_profile(venue: str, *, base_ack_ms: int = 25) -> VenueLatencyProfile:
    """Round trip of ``base_ack_ms``: half to reach the venue, half to hear back."""

    entry_ms = max(1, base_ack_ms // 2)
    return VenueLatencyProfile(
        venue=venue,
        market_data=LatencyDistribution(
            base_ns=1 * MS_NS,
            jitter_ns=1 * MS_NS,
            tail_probability=0.01,
            tail_ns=40 * MS_NS,
        ),
        strategy=LatencyDistribution(base_ns=200_000, jitter_ns=100_000),
        risk=LatencyDistribution(base_ns=100_000, jitter_ns=50_000),
        entry=LatencyDistribution(
            base_ns=entry_ms * MS_NS,
            jitter_ns=3 * MS_NS,
            tail_probability=0.02,
            tail_ns=250 * MS_NS,
        ),
        submit_ack=LatencyDistribution(
            base_ns=max(1, base_ack_ms - entry_ms) * MS_NS,
            jitter_ns=2 * MS_NS,
        ),
        cancel=LatencyDistribution(
            base_ns=base_ack_ms * MS_NS,
            jitter_ns=5 * MS_NS,
            tail_probability=0.02,
            tail_ns=250 * MS_NS,
        ),
        fill=LatencyDistribution(
            base_ns=5 * MS_NS,
            jitter_ns=3 * MS_NS,
            tail_probability=0.01,
            tail_ns=120 * MS_NS,
        ),
    )


class LatencyModel:
    """Deterministic latency draws keyed by seed."""

    def __init__(
        self,
        profiles: Mapping[str, VenueLatencyProfile] | None = None,
        *,
        seed: int = 0,
    ) -> None:
        self._profiles = dict(profiles or {})
        self._seed = seed
        self._rng = random.Random(seed)
        self.samples: list[dict[str, Any]] = []

    @property
    def seed(self) -> int:
        return self._seed

    def profile(self, venue: str) -> VenueLatencyProfile:
        profile = self._profiles.get(venue)
        if profile is None:
            profile = default_profile(venue)
            self._profiles[venue] = profile
        return profile

    def reset(self) -> None:
        self._rng = random.Random(self._seed)
        self.samples.clear()

    def expected_ns(self, venue: str, stage: str) -> int:
        """Mean latency of a stage, used by route scoring (no draw consumed)."""

        return getattr(self.profile(venue), stage).mean_ns

    def draw(self, venue: str, stage: str) -> int:
        distribution = getattr(self.profile(venue), stage)
        value = distribution.sample(self._rng)
        self.samples.append({"venue": venue, "stage": stage, "latency_ns": value})
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self._seed,
            "profiles": {
                venue: profile.to_dict()
                for venue, profile in sorted(self._profiles.items())
            },
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> LatencyModel:
        profiles = {
            venue: VenueLatencyProfile.from_dict({**raw, "venue": venue})
            for venue, raw in document.get("profiles", {}).items()
        }
        return cls(profiles, seed=int(document.get("seed", 0)))

    def report(self) -> dict[str, Any]:
        """Event-pair latency report used by TCA and the benchmark."""

        grouped: dict[str, list[int]] = {}
        for sample in self.samples:
            grouped.setdefault(
                f"{sample['venue']}.{sample['stage']}", []
            ).append(sample["latency_ns"])
        summary: dict[str, Any] = {}
        for key, values in sorted(grouped.items()):
            ordered = sorted(values)
            count = len(ordered)
            summary[key] = {
                "count": count,
                "mean_ns": sum(ordered) // count,
                "p50_ns": ordered[count // 2],
                "p95_ns": ordered[min(count - 1, int(count * 0.95))],
                "p99_ns": ordered[min(count - 1, int(count * 0.99))],
                "max_ns": ordered[-1],
            }
        return {"seed": self._seed, "stages": summary}


def default_latency_model(
    venues: tuple[str, ...], *, seed: int = 0
) -> LatencyModel:
    return LatencyModel(
        {venue: default_profile(venue) for venue in venues}, seed=seed
    )


def profile_from_venue_spec(venue: str, spec: Any) -> VenueLatencyProfile:
    """Map a ``new_backtest.config.VenueLatencySpec`` onto every stage.

    Acknowledgements and fill reports ride the same network path as market
    data, so they reuse the feed latency; the order itself uses the entry
    latency and cancels use the cancel latency.
    """

    jitter = float(spec.jitter_ms)
    return VenueLatencyProfile(
        venue=venue,
        market_data=lognormal_ms(float(spec.feed_ms), jitter),
        strategy=LatencyDistribution(base_ns=200_000, jitter_ns=100_000),
        risk=LatencyDistribution(base_ns=100_000, jitter_ns=50_000),
        entry=lognormal_ms(float(spec.entry_ms), jitter),
        submit_ack=lognormal_ms(float(spec.feed_ms), jitter),
        cancel=lognormal_ms(float(spec.cancel_ms), jitter),
        fill=lognormal_ms(float(spec.feed_ms), jitter),
    )


def latency_preset_names() -> tuple[str, ...]:
    from new_backtest.config import LATENCY_PRESETS

    return tuple(sorted(LATENCY_PRESETS))


def latency_model_from_preset(
    preset: str, venues: tuple[str, ...], *, seed: int = 0
) -> LatencyModel:
    """Build a model from a new_backtest co-location preset.

    Venues the preset does not describe (for example OKX, which the Bybit x
    Binance presets do not cover) keep the default profile, and that choice is
    visible in ``to_dict()`` so the manifest records it.
    """

    from new_backtest.config import LATENCY_PRESETS

    try:
        specs = LATENCY_PRESETS[preset]
    except KeyError as error:
        raise ValueError(
            f"unknown latency preset {preset!r}; choose from "
            f"{', '.join(latency_preset_names())}"
        ) from error
    profiles = {
        venue: (
            profile_from_venue_spec(venue, specs[venue])
            if venue in specs
            else default_profile(venue)
        )
        for venue in venues
    }
    return LatencyModel(profiles, seed=seed)
