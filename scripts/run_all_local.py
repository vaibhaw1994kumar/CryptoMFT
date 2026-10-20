#!/usr/bin/env python3
"""Run the complete finite local acceptance flow.

Network-backed public market-data checks are opt-in. This script never loads
exchange credentials and cannot place live orders.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def run(name: str, args: list[str], *, json_result: bool = False) -> dict | None:
    print(f"\n=== {name} ===", flush=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE if json_result else None,
        check=False,
    )
    # Print the step's output before failing, so a gate's reasons are visible.
    if json_result and completed.stdout:
        print(completed.stdout, end="")
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(completed.returncode, args, output=completed.stdout)
    if not json_result:
        return None
    assert completed.stdout is not None
    return json.loads(completed.stdout)


def test_count() -> int:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [sys.executable, "-c",
         "import unittest; print(unittest.defaultTestLoader.discover('tests').countTestCases())"],
        cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, check=True,
    )
    return int(completed.stdout.strip().splitlines()[-1])


def cli(*args: str) -> list[str]:
    return [sys.executable, "-m", "crypto_stat_arb.cli", *args]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--public-network",
        action="store_true",
        help="also download the public contract master and run a bounded live-feed check",
    )
    parser.add_argument(
        "--live-seconds",
        type=int,
        default=30,
        help="bounded live public-feed runtime when --public-network is used",
    )
    options = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = ROOT / "runs" / f"local-all-{stamp}"
    run_root.mkdir(parents=True, exist_ok=False)

    run(
        "full unittest control suite",
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
    )

    raw_dir = run_root / "raw"
    run(
        "immutable raw capture",
        cli(
            "raw-capture",
            "--input", "data/samples/raw_depth.jsonl",
            "--output-dir", str(raw_dir),
            "--session-id", "local-acceptance",
        ),
    )
    captured_raw = raw_dir / "local-acceptance" / "raw.jsonl"
    run("raw hash-chain verification", cli("verify-raw", "--raw", str(captured_raw)))

    canonical = run_root / "canonical" / "events.jsonl"
    run(
        "raw-to-canonical normalization",
        cli(
            "normalize", "--config", "config/research.json",
            "--raw", str(captured_raw), "--output", str(canonical),
        ),
    )

    partition = run_root / "pipeline" / "events.jsonl"
    run(
        "checkpointed ingestion partition",
        cli(
            "pipeline-run", "--config", "config/research.json",
            "--state-db", str(run_root / "pipeline" / "state.sqlite"),
            "--job-id", "local-acceptance",
            "--partition-id", stamp,
            "--raw", str(captured_raw), "--output", str(partition),
        ),
    )

    run(
        "cross-venue alignment",
        cli(
            "align-streams", "--config", "config/research.json",
            "--input", "BINANCE=data/evidence/alignment/input/binance.jsonl",
            "--input", "BYBIT=data/evidence/alignment/input/bybit.jsonl",
            "--output", str(run_root / "aligned" / "events.jsonl"),
        ),
    )

    paper = run_root / "paper"
    paper_result = run(
        "end-to-end paper/backtest session",
        cli(
            "backtest", "--config", "config/research.json",
            "--events", "data/samples/two_venue_books.jsonl",
            "--output", str(paper),
        ),
        json_result=True,
    )
    paper_economics = run(
        "economics gate: end-to-end session",
        cli("economics-gate", "--run-dir", str(paper)),
        json_result=True,
    )
    run("Strategy SDK paper port", [sys.executable, "examples/strategy_sdk.py"])
    benchmark = run(
        "MR-14 replay/paper performance baseline",
        cli(
            "benchmark", "--config", "config/research.json",
            "--output", str(run_root / "benchmark"), "--synthetic-count", "5000",
        ),
        json_result=True,
    )
    run(
        "central structured-log query",
        cli(
            "log-query", "--db", str(paper / "structured_logs.sqlite"),
            "--level", "INFO", "--limit", "5",
        ),
    )

    registry = run_root / "paper_releases"
    created = run(
        "content-addressed paper release",
        cli(
            "paper-release", "create", "--registry", str(registry),
            "--strategy-id", "cross_venue_btc_v1",
            "--build-version", "0.1.0", "--environment", "paper-local",
            "--artifact", "STRATEGY_CODE:cross_venue_strategy:1.0.0=src/crypto_stat_arb/strategy.py",
            "--artifact", "FEATURE_DEFINITION:top_of_book:1.0.0=config/releases/cross_venue_features_v1.json",
            "--artifact", "MODEL:cross_venue_rules:1.0.0=config/releases/rules_model_v1.json",
            "--artifact", "PARAMETERS:cross_venue_parameters:1.0.0=config/releases/cross_venue_parameters_v1.json",
            "--artifact", "DATA_MANIFEST:two_venue_sample:1.0.0=data/manifests/two_venue_sample_v1.json",
            "--artifact", "SIMULATOR_CONFIG:paper_simulator:1.0.0=config/research.json",
        ),
        json_result=True,
    )
    assert created is not None
    release_id = created["release_id"]
    for action, owner in (("approve", "portfolio-manager"), ("deploy", "paper-operator")):
        run(
            f"paper release {action}",
            cli(
                "paper-release", action, "--registry", str(registry),
                "--release-id", release_id, "--owner", owner,
            ),
        )
    run(
        "paper release verification",
        cli(
            "paper-release", "verify", "--registry", str(registry),
            "--release-id", release_id,
        ),
    )
    run(
        "paper release reproduction",
        cli(
            "paper-release", "reproduce", "--registry", str(registry),
            "--release-id", release_id,
            "--output-dir", str(run_root / "reproduced_release"),
        ),
    )

    if options.public_network:
        run(
            "public Binance/Bybit contract master",
            cli(
                "contract-master", "--output-dir", str(run_root / "contract_master"),
                "--allow-partial",
            ),
        )
        run(
            "bounded public WebSocket/UDP/live-monitoring check",
            cli(
                "live-orderbook", "--config", "config/research.json",
                "--output-dir", str(run_root / "live"),
                "--session-id", "bounded-live-check",
                "--max-runtime-seconds", str(options.live_seconds),
            ),
        )
        paper_dir = run_root / "live_paper" / "bounded-paper-check" / "paper"
        run(
            "bounded live paper forward test (INTERNAL_PAPER, no orders sent)",
            cli(
                "live-paper", "--config", "config/research.json",
                "--output-dir", str(run_root / "live_paper"),
                "--session-id", "bounded-paper-check",
                "--max-runtime-seconds", str(options.live_seconds),
                "--no-udp", "--status-poll-seconds", "30",
            ),
            json_result=True,
        )
        if (paper_dir / "canonical_events.jsonl").exists():
            run(
                "replay of the live paper period",
                cli(
                    "backtest", "--config", "config/research.json",
                    "--events", str(paper_dir / "canonical_events.jsonl"),
                    "--output", str(run_root / "live_paper_replay"),
                ),
                json_result=True,
            )
            replay_economics = run(
                "economics gate: replay of the live paper period",
                cli("economics-gate", "--run-dir", str(run_root / "live_paper_replay")),
                json_result=True,
            )
            run(
                "replay-vs-paper comparison",
                cli(
                    "compare-runs", "--replay", str(run_root / "live_paper_replay"),
                    "--paper", str(paper_dir),
                    "--output", str(run_root / "replay_vs_paper.json"),
                ),
                json_result=True,
            )
            run(
                "latency calibration from paper timestamps",
                cli(
                    "calibrate-latency", "--run-dir", str(paper_dir),
                    "--output", str(run_root / "calibrated_latency.json"),
                    "--min-samples", "20",
                ),
                json_result=True,
            )

    summary = {
        "status": "PASSED",
        "run_directory": str(run_root),
        "tests": test_count(),
        "benchmark_decision": benchmark["decision"] if benchmark else None,
        "paper_opportunities": paper_result["opportunities"] if paper_result else None,
        "paper_closed_plans": paper_result["closed_plans"] if paper_result else None,
        "paper_net_pnl": paper_result["net_pnl"] if paper_result else None,
        "paper_release_id": release_id,
        "economics_gate": paper_economics["decision"] if paper_economics else None,
        "economics_warnings": paper_economics["warnings"] if paper_economics else None,
        "public_network_checks": options.public_network,
    }
    (run_root / "local_acceptance_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print("\n=== ALL REQUESTED LOCAL CHECKS PASSED ===")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
