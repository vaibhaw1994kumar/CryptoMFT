"""Seeded, per-venue, per-stage latency model.

Ported from ``unified_trading_platform/src/trading_platform_poc/minreq/latency.py``
and wired to the presets of the bundled ``advanced_backtest`` engine so the
OMS replay and the advanced SDK backtest share one set of co-location
assumptions.

Stages (all one-way, nanoseconds):

* ``market_data`` exchange match -> our process sees the update;
* ``strategy``    update received -> strategy decision;
* ``risk``        decision -> risk/route decision complete;
* ``entry``       our order send -> venue matching engine (the order executes
                  against the book as it is at *arrival*);
* ``submit_ack``  matching engine -> our acknowledgement / reject;
* ``fill``        matching engine -> our fill report;
* ``cancel``      our cancel send -> the venue applies it.

Draws come from a dedicated seeded generator, so a replay is reproducible from
the run manifest. Tails are explicit rather than smoothed into a mean.
"""

from __future__ import annotations

from collections import deque
import math
import random
from dataclasses import dataclass, field
from typing import Any, Mapping

MS_NS = 1_000_000
STAGES = (
    "market_data", "strategy", "risk", "entry", "submit_ack", "fill", "cancel",
)
EVENT_PAIRS = {
    "market_data": ("exchange_ts", "receive_ts"),
    "strategy": ("receive_ts", "decision_ts"),
    "risk": ("decision_ts", "route_decision_ts"),
    "entry": ("order_send_ts", "venue_match_ts"),
    "submit_ack": ("venue_match_ts", "ack_observed_ts"),
    "fill": ("venue_match_ts", "fill_observed_ts"),
    "cancel": ("cancel_send_ts", "cancel_applied_ts"),
}


@dataclass(frozen=True, slots=True)
class LatencyDistribution:
    """Base + jitter (uniform) or mean/std (lognormal), plus an explicit tail."""

    base_ns: int
    jitter_ns: int = 0
    tail_probability: float = 0.0
    tail_ns: int = 0
    model: str = "uniform"
    std_ns: int = 0
    min_ns: int = 0
    max_ns: int = 0

    def __post_init__(self) -> None:
        if self.model not in {"uniform", "lognormal"}:
            raise ValueError(f"unsupported latency model {self.model!r}")
        if min(self.base_ns, self.jitter_ns, self.tail_ns, self.std_ns, self.min_ns) < 0:
            raise ValueError("latency parameters must be non-negative")
        if not 0.0 <= self.tail_probability <= 1.0:
            raise ValueError("tail probability must be within [0, 1]")

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
            self.base_ns if self.model == "lognormal"
            else self.base_ns + self.jitter_ns // 2
        )
        return int(body + self.tail_probability * self.tail_ns)

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_ns": self.base_ns, "jitter_ns": self.jitter_ns,
            "tail_probability": self.tail_probability, "tail_ns": self.tail_ns,
            "model": self.model, "std_ns": self.std_ns,
            "min_ns": self.min_ns, "max_ns": self.max_ns,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "LatencyDistribution":
        def ns(key: str) -> int:
            if f"{key}_ms" in document:
                return int(float(document[f"{key}_ms"]) * MS_NS)
            return int(document.get(f"{key}_ns", 0))

        return cls(
            base_ns=ns("base"), jitter_ns=ns("jitter"),
            tail_probability=float(document.get("tail_probability", 0.0)),
            tail_ns=ns("tail"), model=str(document.get("model", "uniform")),
            std_ns=ns("std"), min_ns=ns("min"), max_ns=ns("max"),
        )


def lognormal_ms(mean_ms: float, std_ms: float) -> LatencyDistribution:
    """Lognormal stage with advanced_backtest's clamps (0.3 ms to 2 s)."""
    return LatencyDistribution(
        base_ns=int(mean_ms * MS_NS), model="lognormal",
        std_ns=int(std_ms * MS_NS), min_ns=int(0.3 * MS_NS),
        max_ns=2_000 * MS_NS,
    )


@dataclass(frozen=True, slots=True)
class VenueLatencyProfile:
    venue: str
    market_data: LatencyDistribution
    strategy: LatencyDistribution
    risk: LatencyDistribution
    entry: LatencyDistribution
    submit_ack: LatencyDistribution
    fill: LatencyDistribution
    cancel: LatencyDistribution

    def to_dict(self) -> dict[str, Any]:
        return {"venue": self.venue, **{
            stage: getattr(self, stage).to_dict() for stage in STAGES
        }}

    def with_overrides(self, document: Mapping[str, Any]) -> "VenueLatencyProfile":
        changes = {
            stage: LatencyDistribution.from_dict(document[stage])
            for stage in STAGES if stage in document
        }
        unknown = set(document) - set(STAGES) - {"venue"}
        if unknown:
            raise ValueError(f"unknown latency stages: {sorted(unknown)}")
        values = {stage: getattr(self, stage) for stage in STAGES}
        values.update(changes)
        return VenueLatencyProfile(venue=self.venue, **values)


def default_profile(venue: str, *, round_trip_ms: int = 25) -> VenueLatencyProfile:
    """Conservative non-colocated default: half the round trip each way."""
    entry_ms = max(1, round_trip_ms // 2)
    return VenueLatencyProfile(
        venue=venue,
        market_data=LatencyDistribution(
            base_ns=1 * MS_NS, jitter_ns=1 * MS_NS,
            tail_probability=0.01, tail_ns=40 * MS_NS,
        ),
        strategy=LatencyDistribution(base_ns=200_000, jitter_ns=100_000),
        risk=LatencyDistribution(base_ns=100_000, jitter_ns=50_000),
        entry=LatencyDistribution(
            base_ns=entry_ms * MS_NS, jitter_ns=3 * MS_NS,
            tail_probability=0.02, tail_ns=250 * MS_NS,
        ),
        submit_ack=LatencyDistribution(
            base_ns=max(1, round_trip_ms - entry_ms) * MS_NS, jitter_ns=2 * MS_NS,
        ),
        fill=LatencyDistribution(
            base_ns=5 * MS_NS, jitter_ns=3 * MS_NS,
            tail_probability=0.01, tail_ns=120 * MS_NS,
        ),
        cancel=LatencyDistribution(
            base_ns=round_trip_ms * MS_NS, jitter_ns=5 * MS_NS,
            tail_probability=0.02, tail_ns=250 * MS_NS,
        ),
    )


def profile_from_venue_spec(venue: str, spec: Any) -> VenueLatencyProfile:
    """Map an ``advanced_backtest.config.VenueLatencySpec`` onto every stage.

    Acknowledgements and fill reports ride the same network path as market
    data, so they reuse the feed latency.
    """
    jitter = float(spec.jitter_ms)
    return VenueLatencyProfile(
        venue=venue,
        market_data=lognormal_ms(float(spec.feed_ms), jitter),
        strategy=LatencyDistribution(base_ns=200_000, jitter_ns=100_000),
        risk=LatencyDistribution(base_ns=100_000, jitter_ns=50_000),
        entry=lognormal_ms(float(spec.entry_ms), jitter),
        submit_ack=lognormal_ms(float(spec.feed_ms), jitter),
        fill=lognormal_ms(float(spec.feed_ms), jitter),
        cancel=lognormal_ms(float(spec.cancel_ms), jitter),
    )


def latency_preset_names() -> tuple[str, ...]:
    from .advanced_backtest.config import LATENCY_PRESETS

    return tuple(sorted(LATENCY_PRESETS))


class LatencyModel:
    """Deterministic draws keyed by seed, with a bounded sample store."""

    def __init__(
        self,
        profiles: Mapping[str, VenueLatencyProfile] | None = None,
        *,
        seed: int = 0,
        source: str = "explicit",
        max_samples_per_stage: int = 100_000,
    ) -> None:
        self._profiles = dict(profiles or {})
        self.seed = seed
        self.source = source
        self._rng = random.Random(seed)
        self.max_samples_per_stage = max_samples_per_stage
        self._samples: dict[str, deque[int]] = {}
        self._counts: dict[str, int] = {}
        self._defaulted: set[str] = set()

    def profile(self, venue: str) -> VenueLatencyProfile:
        profile = self._profiles.get(venue)
        if profile is None:
            profile = default_profile(venue)
            self._profiles[venue] = profile
            self._defaulted.add(venue)
        return profile

    def expected_ns(self, venue: str, stage: str) -> int:
        return getattr(self.profile(venue), stage).mean_ns

    def draw(self, venue: str, stage: str) -> int:
        if stage not in STAGES:
            raise ValueError(f"unknown latency stage {stage!r}")
        value = getattr(self.profile(venue), stage).sample(self._rng)
        self.observe(venue, stage, value)
        return value

    def observe(self, venue: str, stage: str, latency_ns: int) -> None:
        """Record a drawn or measured sample for the event-pair report."""
        key = f"{venue}.{stage}"
        store = self._samples.get(key)
        if store is None:
            store = self._samples[key] = deque(maxlen=self.max_samples_per_stage)
        store.append(int(latency_ns))
        self._counts[key] = self._counts.get(key, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "source": self.source,
            "defaulted_venues": sorted(self._defaulted),
            "profiles": {
                venue: profile.to_dict()
                for venue, profile in sorted(self._profiles.items())
            },
        }

    def report(self) -> dict[str, Any]:
        """Event-pair latency report (bounded percentile window per stage)."""
        stages: dict[str, Any] = {}
        for key, values in sorted(self._samples.items()):
            ordered = sorted(values)
            count = len(ordered)
            stage = key.rsplit(".", 1)[1]
            stages[key] = {
                "event_pair": list(EVENT_PAIRS[stage]),
                "count": self._counts[key],
                "window": count,
                "mean_ns": sum(ordered) // count,
                "p50_ns": ordered[count // 2],
                "p95_ns": ordered[min(count - 1, int(count * 0.95))],
                "p99_ns": ordered[min(count - 1, int(count * 0.99))],
                "max_ns": ordered[-1],
            }
        return {"schema_version": "1.0.0", **self.to_dict(), "stages": stages}


def latency_model_from_preset(
    preset: str, venues: tuple[str, ...], *, seed: int = 0
) -> LatencyModel:
    from .advanced_backtest.config import LATENCY_PRESETS

    try:
        specs = LATENCY_PRESETS[preset]
    except KeyError as exc:
        raise ValueError(
            f"unknown latency preset {preset!r}; choose from "
            f"{', '.join(latency_preset_names())}"
        ) from exc
    profiles = {}
    for venue in venues:
        spec = specs.get(venue.lower())
        profiles[venue] = (
            profile_from_venue_spec(venue, spec) if spec is not None
            else default_profile(venue)
        )
    model = LatencyModel(profiles, seed=seed, source=f"preset:{preset}")
    model._defaulted.update(venue for venue in venues if venue.lower() not in specs)
    return model


def latency_model_from_config(
    document: Mapping[str, Any] | None, venues: tuple[str, ...]
) -> LatencyModel | None:
    """Build the model from a config ``latency`` section.

    ``{"enabled": true, "seed": 7, "preset": "singapore",
       "profiles": {"BINANCE": {"entry": {"base_ms": 40, ...}}}}``

    Per-venue ``profiles`` override individual stages on top of the preset
    (or the default profile). ``enabled: false`` or a missing section returns
    ``None``, which keeps the legacy zero-latency adapter path and is labelled
    as such in run artifacts.
    """
    if not document or not document.get("enabled", True):
        return None
    seed = int(document.get("seed", 0))
    preset = document.get("preset")
    if preset:
        model = latency_model_from_preset(str(preset), venues, seed=seed)
    else:
        model = LatencyModel(
            {venue: default_profile(venue) for venue in venues},
            seed=seed, source="default",
        )
    overrides = document.get("profiles", {})
    for venue, raw in overrides.items():
        model._profiles[venue] = model.profile(venue).with_overrides(raw)
        model._defaulted.discard(venue)
    if overrides:
        model.source += "+overrides"
    return model
