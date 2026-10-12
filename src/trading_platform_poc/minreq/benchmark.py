"""Performance baseline tied to a host and a build (MR-14, AT-14).

Throughput on its own is meaningless without the machine and the binary that
produced it, so every measurement is emitted alongside the build identity and
the seed that generated the workload.
"""

from __future__ import annotations

import json
import shutil
import time
import tracemalloc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from trading_platform_poc.minreq.backtest import (
    BacktestConfig,
    BacktestRunner,
)
from trading_platform_poc.minreq.manifest import build_identity
from trading_platform_poc.minreq.pipeline import Pipeline
from trading_platform_poc.minreq.replay import MultiVenueReplay
from trading_platform_poc.minreq.securitymaster import default_security_master
from trading_platform_poc.minreq.synthetic import SyntheticConfig, generate


@dataclass(frozen=True, slots=True)
class Measurement:
    name: str
    unit: str
    value: float
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "unit": self.unit,
            "value": self.value,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkThresholds:
    """Agreed limits. Numbers are deliberately conservative for a laptop."""

    min_ingestion_events_per_second: float = 500.0
    min_normalization_events_per_second: float = 250.0
    min_replay_events_per_second: float = 2_000.0
    min_simulation_events_per_second: float = 100.0
    max_per_order_overhead_us: float = 20_000.0
    max_feature_overhead_us: float = 5_000.0
    max_peak_memory_mb: float = 512.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_ingestion_events_per_second": (
                self.min_ingestion_events_per_second
            ),
            "min_normalization_events_per_second": (
                self.min_normalization_events_per_second
            ),
            "min_replay_events_per_second": self.min_replay_events_per_second,
            "min_simulation_events_per_second": (
                self.min_simulation_events_per_second
            ),
            "max_per_order_overhead_us": self.max_per_order_overhead_us,
            "max_feature_overhead_us": self.max_feature_overhead_us,
            "max_peak_memory_mb": self.max_peak_memory_mb,
        }


@dataclass(slots=True)
class BenchmarkReport:
    build: dict[str, str]
    seed: int
    workload: dict[str, Any]
    measurements: list[Measurement]
    thresholds: BenchmarkThresholds
    failures: list[str]

    @property
    def passed(self) -> bool:
        return not self.failures

    def value(self, name: str) -> float:
        for measurement in self.measurements:
            if measurement.name == name:
                return measurement.value
        raise KeyError(name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "build": dict(sorted(self.build.items())),
            "seed": self.seed,
            "workload": self.workload,
            "thresholds": self.thresholds.to_dict(),
            "measurements": [item.to_dict() for item in self.measurements],
            "failures": list(self.failures),
            "passed": self.passed,
        }

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        return target


def _timed(callback: Callable[[], Any]) -> tuple[Any, float]:
    start = time.perf_counter()
    value = callback()
    return value, max(time.perf_counter() - start, 1e-9)


def run_benchmark(
    workspace: str | Path,
    *,
    steps: int = 600,
    seed: int = 20260817,
    thresholds: BenchmarkThresholds | None = None,
) -> BenchmarkReport:
    """Measure ingestion, replay and end-to-end simulation on this host."""

    limits = thresholds or BenchmarkThresholds()
    root = Path(workspace)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    master = default_security_master()
    synthetic = SyntheticConfig(seed=seed, steps=steps)

    records, generate_seconds = _timed(lambda: generate(synthetic))
    dataset_root = root / "dataset"
    pipeline = Pipeline(dataset_root, master)

    tracemalloc.start()
    _, ingest_seconds = _timed(lambda: pipeline.ingest(records))
    manifest, normalize_seconds = _timed(pipeline.run)
    ingest_current, ingest_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    streams = pipeline.venue_streams()
    canonical_events = sum(len(events) for events in streams.values())

    def replay_pass() -> int:
        replay = MultiVenueReplay(
            {venue: list(events) for venue, events in streams.items()},
            run_id="benchmark",
        )
        return sum(1 for _ in replay.events())

    replayed, replay_seconds = _timed(replay_pass)

    config = BacktestConfig(run_id=f"benchmark-{seed}", seed=seed)
    runner = BacktestRunner(
        config,
        streams,
        root / "run",
        security_master=master,
        dataset_info=manifest,
    )
    result, simulation_seconds = _timed(runner.run)
    orders = int(result.tca.totals.get("orders", "0")) or 1

    raw_bytes = sum(
        path.stat().st_size for path in (dataset_root / "raw").rglob("*.jsonl")
    )
    canonical_bytes = sum(
        path.stat().st_size
        for path in (dataset_root / "canonical").rglob("*.jsonl")
    )

    measurements = [
        Measurement(
            "synthetic_generation_records_per_second",
            "records/s",
            round(len(records) / generate_seconds, 2),
            {"records": len(records)},
        ),
        Measurement(
            "ingestion_events_per_second",
            "records/s",
            round(len(records) / ingest_seconds, 2),
            {"records": len(records), "seconds": round(ingest_seconds, 6)},
        ),
        Measurement(
            "normalization_events_per_second",
            "events/s",
            round(canonical_events / normalize_seconds, 2),
            {
                "canonical_events": canonical_events,
                "seconds": round(normalize_seconds, 6),
            },
        ),
        Measurement(
            "replay_events_per_second",
            "events/s",
            round(replayed / replay_seconds, 2),
            {"events": replayed, "seconds": round(replay_seconds, 6)},
        ),
        Measurement(
            "simulation_events_per_second",
            "events/s",
            round(
                result.benchmark["events_processed"] / simulation_seconds, 2
            ),
            {
                "events": result.benchmark["events_processed"],
                "seconds": round(simulation_seconds, 6),
            },
        ),
        Measurement(
            "per_order_overhead_us",
            "microseconds/order",
            round(
                result.benchmark["execution_seconds"] * 1_000_000 / orders, 2
            ),
            {
                "orders": orders,
                "plans": int(result.tca.totals.get("plans", "0")),
                "execution_seconds": result.benchmark["execution_seconds"],
                "note": (
                    "Router, risk and matching time only; feature and strategy "
                    "time are reported separately."
                ),
            },
        ),
        Measurement(
            "feature_overhead_us",
            "microseconds/book event",
            round(
                result.benchmark["feature_seconds"]
                * 1_000_000
                / max(result.benchmark["book_events"], 1),
                2,
            ),
            {
                "book_events": result.benchmark["book_events"],
                "feature_seconds": result.benchmark["feature_seconds"],
            },
        ),
        Measurement(
            "strategy_overhead_us",
            "microseconds/decision",
            round(
                result.benchmark["strategy_seconds"]
                * 1_000_000
                / max(int(result.tca.totals.get("decisions", "0")) or 1, 1),
                2,
            ),
            {"strategy_seconds": result.benchmark["strategy_seconds"]},
        ),
        Measurement(
            "ingestion_peak_memory_mb",
            "megabytes",
            round(ingest_peak / (1024 * 1024), 4),
            {"current_bytes": ingest_current},
        ),
        Measurement(
            "simulation_peak_memory_mb",
            "megabytes",
            round(result.benchmark["peak_memory_bytes"] / (1024 * 1024), 4),
            {},
        ),
        Measurement(
            "raw_output_bytes", "bytes", float(raw_bytes), {}
        ),
        Measurement(
            "canonical_output_bytes", "bytes", float(canonical_bytes), {}
        ),
        Measurement(
            "ledger_output_bytes",
            "bytes",
            float(result.benchmark["ledger_bytes"]),
            {"ledger_events": result.benchmark["ledger_events"]},
        ),
    ]

    report = BenchmarkReport(
        build=build_identity(),
        seed=seed,
        workload={
            "steps": steps,
            "venues": list(synthetic.venues),
            "raw_records": len(records),
            "canonical_events": canonical_events,
            "orders": orders,
        },
        measurements=measurements,
        thresholds=limits,
        failures=[],
    )

    failures: list[str] = []
    if (
        report.value("ingestion_events_per_second")
        < limits.min_ingestion_events_per_second
    ):
        failures.append("ingestion_events_per_second below threshold")
    if (
        report.value("normalization_events_per_second")
        < limits.min_normalization_events_per_second
    ):
        failures.append("normalization_events_per_second below threshold")
    if (
        report.value("replay_events_per_second")
        < limits.min_replay_events_per_second
    ):
        failures.append("replay_events_per_second below threshold")
    if (
        report.value("simulation_events_per_second")
        < limits.min_simulation_events_per_second
    ):
        failures.append("simulation_events_per_second below threshold")
    if report.value("per_order_overhead_us") > limits.max_per_order_overhead_us:
        failures.append("per_order_overhead_us above threshold")
    if report.value("feature_overhead_us") > limits.max_feature_overhead_us:
        failures.append("feature_overhead_us above threshold")
    if (
        report.value("simulation_peak_memory_mb")
        > limits.max_peak_memory_mb
    ):
        failures.append("simulation_peak_memory_mb above threshold")
    report.failures = failures
    report.write(root / "benchmark.json")
    return report
