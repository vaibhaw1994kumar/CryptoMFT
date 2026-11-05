"""Queue model v2, step 7: the unknown queue (guide s3). An absent price level OUTSIDE the captured depth has an unknown queue ahead. The fixed 25-unit floor is replaced by a
distribution FITTED on in-depth observations at comparable distance from the touch (training days only), drawn per order from a seeded stream; every such fill is
flagged queue_unknown. A level that is absent INSIDE the captured depth or inside the spread is empty (queue 0) and is not touched by this module."""
from __future__ import annotations

import json
import math
import random
from pathlib import Path

BUCKETS = ((1, 1), (2, 2), (3, 3), (4, 5), (6, 8), (9, 12), (13, 10 ** 6))      # distance from the touch in ticks, inclusive


def bucket_of(d: int) -> int:
    for k, (lo, hi) in enumerate(BUCKETS):
        if lo <= d <= hi:
            return k
    return len(BUCKETS) - 1


def fit_from_snapshots(snapshots, tick: float) -> dict:
    """snapshots: iterable of dicts with bids/asks (best first). Returns per-bucket lognormal parameters of the level size at that distance."""
    sizes: list[list[float]] = [[] for _ in BUCKETS]
    for s in snapshots:
        for side in ("bids", "asks"):
            lv = s.get(side) or []
            if len(lv) < 2:
                continue
            touch = float(lv[0][0])
            for p, sz in lv[1:]:
                d = int(round(abs(float(p) - touch) / tick))
                if d >= 1 and float(sz) > 0:
                    sizes[bucket_of(d)].append(float(sz))
    out = []
    for k, xs in enumerate(sizes):
        lo, hi = BUCKETS[k]
        if len(xs) < 30:
            out.append({"d_lo": lo, "d_hi": hi, "n": len(xs), "median": None, "sigma": None})
            continue
        logs = sorted(math.log(x) for x in xs)
        mu = logs[len(logs) // 2]
        mean = sum(logs) / len(logs)
        sigma = math.sqrt(sum((v - mean) ** 2 for v in logs) / (len(logs) - 1))
        out.append({"d_lo": lo, "d_hi": hi, "n": len(xs), "median": math.exp(mu), "sigma": sigma,
                    "p10": xs[len(xs) // 10] if False else sorted(xs)[len(xs) // 10], "p90": sorted(xs)[(9 * len(xs)) // 10]})
    return {"buckets": out}


class UnknownQueueFit:
    """Loaded fit: draw(venue, distance_ticks, seed) -> queue ahead in base units. A bucket without enough observations falls back to the nearest fitted bucket."""
    def __init__(self, doc: dict) -> None:
        self.doc = doc
        self.venues = doc["venues"]

    @classmethod
    def load(cls, path: str | Path) -> "UnknownQueueFit":
        return cls(json.loads(Path(path).read_text()))

    def params(self, venue: str, distance_ticks: int) -> tuple[float, float, int]:
        bs = self.venues[venue]["buckets"]
        k = bucket_of(max(1, int(distance_ticks)))
        for j in sorted(range(len(bs)), key=lambda j: abs(j - k)):
            if bs[j]["median"] is not None:
                return bs[j]["median"], bs[j]["sigma"], j
        raise ValueError(f"no fitted bucket for {venue}")

    def draw(self, venue: str, distance_ticks: int, seed: int) -> float:
        med, sig, _ = self.params(venue, distance_ticks)
        return med * math.exp(sig * random.Random(seed).gauss(0.0, 1.0))
