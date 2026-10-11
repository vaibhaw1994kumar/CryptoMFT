"""Latency calibration and replay-vs-paper comparison.

``calibrate_latency`` fits per-venue, per-stage latency distributions from a
live paper or testnet run's timestamps and writes a ``latency`` config section
that the backtest can load directly. ``compare_runs`` replays the same period
and reports, metric by metric, whether the replay reproduces the forward test
(latency, fills, rejects, costs and missed opportunities) within tolerance.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import random
from typing import Any

from .latency import EVENT_PAIRS, LatencyDistribution


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _percentile(ordered: list[int], quantile: float) -> int:
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * quantile))]


def summarize(values: list[int]) -> dict[str, int]:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min_ns": ordered[0],
        "mean_ns": sum(ordered) // len(ordered),
        "p50_ns": _percentile(ordered, 0.50),
        "p95_ns": _percentile(ordered, 0.95),
        "p99_ns": _percentile(ordered, 0.99),
        "max_ns": ordered[-1],
    }


def fit_distribution(values: list[int]) -> LatencyDistribution:
    """Lognormal body (<= p99) plus an explicit tail for the top 1%."""
    ordered = sorted(value for value in values if value >= 0)
    if not ordered:
        raise ValueError("no non-negative latency samples")
    cut = _percentile(ordered, 0.99)
    body = [value for value in ordered if value <= cut]
    tail = [value for value in ordered if value > cut]
    # Fit in log space: feed and order latencies are right-skewed, and a
    # moment fit on raw values overstates the median. The (mean, std) below
    # are the raw-scale moments of that lognormal, which is how
    # LatencyDistribution parameterises it.
    logs = [math.log(max(value, 1_000)) for value in body]
    mu = sum(logs) / len(logs)
    sigma2 = sum((value - mu) ** 2 for value in logs) / len(logs)
    mean = math.exp(mu + sigma2 / 2)
    std = mean * math.sqrt(math.expm1(sigma2))
    tail_probability = len(tail) / len(ordered)
    tail_ns = max(0, int(sum(tail) / len(tail) - mean)) if tail else 0
    if mean > 0 and std > 0:
        return LatencyDistribution(
            base_ns=int(mean), model="lognormal", std_ns=int(std),
            tail_probability=tail_probability, tail_ns=tail_ns, min_ns=body[0],
        )
    return LatencyDistribution(
        base_ns=int(mean), tail_probability=tail_probability, tail_ns=tail_ns,
    )


def goodness_of_fit(
    distribution: LatencyDistribution, observed: list[int], *,
    seed: int = 0, draws: int = 20_000,
) -> dict[str, Any]:
    rng = random.Random(seed)
    simulated = sorted(distribution.sample(rng) for _ in range(draws))
    ordered = sorted(observed)
    result = {}
    for label, quantile in (("p50", 0.50), ("p95", 0.95), ("p99", 0.99)):
        seen = _percentile(ordered, quantile)
        model = _percentile(simulated, quantile)
        result[label] = {
            "observed_ns": seen, "model_ns": model,
            "relative_error": abs(model - seen) / seen if seen else None,
        }
    return result


def samples_from_run(run_dir: Path) -> tuple[dict[tuple[str, str], list[int]], dict[str, Any]]:
    """Collect event-pair latency samples from a live paper/testnet run."""
    rows = read_jsonl(run_dir / "paper_timestamps.jsonl")
    time_report = _read_json(run_dir / "time_integrity_report.json")
    offset = (time_report.get("host_offset_correction") or {}).get(
        "clock_offset_estimate_ns"
    )
    events = [row for row in rows if row.get("record_type") == "EVENT"]
    if offset is None and events:
        raw = sorted(row["receive_ts_ns"] - row["exchange_ts_ns"] for row in events)
        offset = min(0, _percentile(raw, 0.01))
    samples: dict[tuple[str, str], list[int]] = {}
    for row in events:
        value = row["receive_ts_ns"] - row["exchange_ts_ns"] - (offset or 0)
        if value >= 0:
            samples.setdefault((row["venue"], "market_data"), []).append(value)
    for row in rows:
        if row.get("record_type") != "DECISION":
            continue
        venue = row.get("buy_venue") or row["venue"]
        samples.setdefault((venue, "strategy"), []).append(
            max(0, row["decision_ts_ns"] - row["receive_ts_ns"])
        )
        samples.setdefault((venue, "risk"), []).append(
            max(0, row["route_decision_ts_ns"] - row["decision_ts_ns"])
        )
    testnet = [
        row for row in read_jsonl(run_dir / "testnet_order_timestamps.jsonl")
        if row.get("action") in {"SUBMIT", "REPLACE"} and row.get("response_ts_ns")
    ]
    for row in testnet:
        round_trip = max(0, row["response_ts_ns"] - row["order_send_ts_ns"])
        entry = round_trip // 2
        samples.setdefault((row["venue"], "entry"), []).append(entry)
        samples.setdefault((row["venue"], "submit_ack"), []).append(round_trip - entry)
    for row in read_jsonl(run_dir / "testnet_order_timestamps.jsonl"):
        if row.get("action") == "CANCEL" and row.get("response_ts_ns"):
            samples.setdefault((row["venue"], "cancel"), []).append(
                max(0, row["response_ts_ns"] - row["order_send_ts_ns"])
            )
    notes = {
        "host_clock_offset_ns": offset,
        "order_path": (
            "testnet REST round trip split evenly between entry and submit_ack"
            if testnet else
            "not calibrated: INTERNAL_PAPER order timestamps are model draws, "
            "calibrate entry/ack/fill from a VENUE_TESTNET run"
        ),
    }
    return samples, notes


def calibrate_latency(
    run_dir: Path, output_path: Path | None = None, *, seed: int = 0,
    min_samples: int = 30, preset: str | None = None,
) -> dict[str, Any]:
    samples, notes = samples_from_run(run_dir)
    profiles: dict[str, dict[str, Any]] = {}
    fits: dict[str, Any] = {}
    skipped: dict[str, int] = {}
    for (venue, stage), values in sorted(samples.items()):
        key = f"{venue}.{stage}"
        if len(values) < min_samples:
            skipped[key] = len(values)
            continue
        distribution = fit_distribution(values)
        profiles.setdefault(venue, {})[stage] = distribution.to_dict()
        fits[key] = {
            "event_pair": list(EVENT_PAIRS[stage]),
            "observed": summarize(values),
            "fitted": distribution.to_dict(),
            "goodness_of_fit": goodness_of_fit(distribution, values, seed=seed),
        }
    latency: dict[str, Any] = {"enabled": True, "seed": seed, "profiles": profiles}
    if preset:
        latency["preset"] = preset
    report = {
        "schema_version": "1.0.0",
        "source_run": str(run_dir),
        "min_samples": min_samples,
        "latency": latency,
        "fits": fits,
        "skipped_insufficient_samples": skipped,
        "notes": notes,
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


DEFAULT_TOLERANCES: dict[str, tuple[str, float]] = {
    "opportunities": ("relative", 0.10),
    "closed_plans": ("relative", 0.10),
    "fills": ("relative", 0.10),
    "fill_ratio": ("absolute", 0.05),
    "rejects": ("relative", 0.25),
    "cancels": ("relative", 0.25),
    "missed_opportunities": ("relative", 0.25),
    "weighted_slippage_bps": ("absolute", 1.0),
    "fees": ("relative", 0.10),
    "net_pnl": ("relative", 0.25),
}


def _run_metrics(run_dir: Path) -> tuple[dict[str, float], dict[str, Any], str]:
    summary = _read_json(run_dir / "summary.json")
    if not summary:
        raise ValueError(f"{run_dir} has no summary.json")
    tca = _read_json(run_dir / "tca_report.json").get("summary", {})
    latency = _read_json(run_dir / "latency_report.json")
    orders = int(tca.get("orders", 0) or 0)
    fills = int(tca.get("fills", 0) or 0)
    metrics = {
        "opportunities": float(summary.get("opportunities", 0)),
        "closed_plans": float(summary.get("closed_plans", 0)),
        "fills": float(fills),
        "fill_ratio": fills / orders if orders else 0.0,
        "rejects": float(tca.get("rejects", 0) or 0),
        "cancels": float(tca.get("cancels", 0) or 0),
        "missed_opportunities": float(tca.get("missed_opportunities", 0) or 0),
        "weighted_slippage_bps": float(tca.get("weighted_slippage_bps") or 0),
        "fees": float(tca.get("fees", summary.get("fees", 0)) or 0),
        "net_pnl": float(summary.get("net_pnl", 0)),
    }
    stages: dict[str, Any] = {}
    for section in ("model", "measured"):
        for key, row in ((latency.get(section) or {}).get("stages") or {}).items():
            stages.setdefault(key, row)
    return metrics, stages, str(summary.get("execution_mode", "UNKNOWN"))


def compare_runs(
    replay_dir: Path, paper_dir: Path, output_path: Path | None = None, *,
    tolerances: dict[str, tuple[str, float]] | None = None,
    latency_relative_tolerance: float = 0.5, min_opportunities: int = 30,
) -> dict[str, Any]:
    replay, replay_stages, replay_mode = _run_metrics(replay_dir)
    paper, paper_stages, paper_mode = _run_metrics(paper_dir)
    if replay_mode != "INTERNAL_SIMULATION":
        raise ValueError(f"replay run must be INTERNAL_SIMULATION, got {replay_mode}")
    if paper_mode not in {"INTERNAL_PAPER", "VENUE_TESTNET"}:
        raise ValueError(f"forward run must be INTERNAL_PAPER or VENUE_TESTNET, got {paper_mode}")
    rows: dict[str, Any] = {}
    for metric, (kind, tolerance) in (tolerances or DEFAULT_TOLERANCES).items():
        replay_value, paper_value = replay[metric], paper[metric]
        difference = replay_value - paper_value
        if kind == "relative":
            relative = (
                abs(difference) / abs(paper_value) if paper_value
                else (0.0 if replay_value == 0 else math.inf)
            )
            within = relative <= tolerance
        else:
            relative = None
            within = abs(difference) <= tolerance
        rows[metric] = {
            "replay": replay_value, "paper": paper_value,
            "difference": difference,
            "relative_difference": (
                None if relative is None or math.isinf(relative) else relative
            ),
            "tolerance": {"kind": kind, "value": tolerance},
            "within_tolerance": within,
        }
    latency_rows: dict[str, Any] = {}
    for key in sorted(set(replay_stages) & set(paper_stages)):
        entry = {}
        for percentile in ("p50_ns", "p99_ns"):
            replay_value = replay_stages[key].get(percentile)
            paper_value = paper_stages[key].get(percentile)
            if not paper_value:
                continue
            relative = abs(replay_value - paper_value) / paper_value
            entry[percentile] = {
                "replay": replay_value, "paper": paper_value,
                "relative_difference": relative,
                "within_tolerance": relative <= latency_relative_tolerance,
            }
        if entry:
            latency_rows[key] = entry
    all_within = all(row["within_tolerance"] for row in rows.values()) and all(
        item["within_tolerance"]
        for entry in latency_rows.values() for item in entry.values()
    )
    if paper["opportunities"] < min_opportunities:
        decision = "INSUFFICIENT_SAMPLE"
    else:
        decision = "CONSISTENT" if all_within else "DIVERGED"
    report = {
        "schema_version": "1.0.0",
        "replay_run": str(replay_dir),
        "forward_run": str(paper_dir),
        "forward_execution_mode": paper_mode,
        "decision": decision,
        "min_opportunities": min_opportunities,
        "metrics": rows,
        "latency": latency_rows,
        "diverged_metrics": sorted(
            [name for name, row in rows.items() if not row["within_tolerance"]]
            + [
                f"{key}.{percentile}" for key, entry in latency_rows.items()
                for percentile, item in entry.items() if not item["within_tolerance"]
            ]
        ),
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report
