"""Operator-surface coverage for the ``mr-*`` command family (Section 10).

The commands are the only supported way to drive the platform, so each one is
parsed through the real argument parser and executed against a temporary
workspace. Every command prints one JSON document, which is what the tests
assert on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from trading_platform_poc.cli import build_parser
from trading_platform_poc.config import AppConfig, MinReqConfig
from trading_platform_poc.minreq.cli import MR_COMMANDS, run_minreq_command


def _emit(capsys, *argv: str, config: AppConfig | None = None) -> dict:
    """Run one command and return the JSON document it printed."""

    args = build_parser().parse_args(list(argv))
    assert args.command in MR_COMMANDS
    run_minreq_command(args, config or AppConfig())
    return json.loads(capsys.readouterr().out)


def test_every_command_is_registered_on_the_parser() -> None:
    parser = build_parser()
    choices = next(
        action.choices
        for action in parser._subparsers._group_actions  # noqa: SLF001
        if action.choices
    )
    assert set(MR_COMMANDS) <= set(choices)
    assert all(name.startswith("mr-") for name in MR_COMMANDS)


def test_instruments_command_prints_the_effective_dated_master(capsys) -> None:
    document = _emit(capsys, "mr-instruments", "--as-of-ns", "1800000000000000000")
    assert "BTC/USDT.SPOT" in document["economic_groups"]
    assert document["instruments"]
    assert {row["venue"] for row in document["instruments"]} >= {"binance", "okx"}


def test_fees_command_resolves_both_liquidity_sides(capsys) -> None:
    document = _emit(
        capsys, "mr-fees", "--as-of-ns", "1800000000000000000", "--venue", "binance"
    )
    liquidity = {row["liquidity"] for row in document["resolved"]}
    assert liquidity == {"maker", "taker"}
    assert document["schedule"]
    assert document["fee_book_version"]


def test_schema_command_carries_every_section_ten_document(capsys) -> None:
    document = _emit(capsys, "mr-schema")
    assert document["document_version"] == "2.1"
    for key in (
        "canonical_schema",
        "venue_capability_matrix",
        "feature_catalogue",
        "risk_limit_matrix",
        "order_state_machine",
        "monitoring",
    ):
        assert document[key], key


def test_generate_then_verify_then_quality(capsys, tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    created = _emit(
        capsys,
        "mr-generate",
        "--output",
        str(dataset),
        "--steps",
        "60",
        "--seed",
        "7",
    )
    assert created["approved"] is True
    assert created["canonical_events"] > 0
    assert created["quality_failures"] == []
    assert len(created["venues"]) >= 2

    verified = _emit(capsys, "mr-verify", "--dataset", str(dataset))
    assert verified["verified"] is True
    assert verified["checksum"] == created["checksum"]

    quality = _emit(capsys, "mr-quality", "--dataset", str(dataset))
    assert quality["approved"] is True
    assert quality["quality"]["event_count"] == created["canonical_events"]

    listed = _emit(capsys, "mr-datasets", "--root", str(tmp_path))
    assert [row["dataset_id"] for row in listed] == [quality["dataset_id"]]


def test_generate_rejects_a_single_venue(capsys, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="two venues"):
        _emit(
            capsys,
            "mr-generate",
            "--output",
            str(tmp_path / "bad"),
            "--venues",
            "binance",
        )


def test_backtest_writes_the_full_artifact_set(capsys, tmp_path: Path) -> None:
    out = tmp_path / "run"
    document = _emit(
        capsys,
        "mr-backtest",
        "--output",
        str(out),
        "--run-id",
        "cli",
        "--steps",
        "220",
        "--seed",
        "42",
    )
    assert document["run_id"] == "cli"
    assert document["mode"] == "backtest"
    assert document["reconciliation"]["breaks"] == []
    assert document["state_hash"]

    for name in (
        "run_manifest.json",
        "tca.json",
        "tca.csv",
        "tca.md",
        "route_log.json",
        "benchmark.json",
        "ledger.sqlite",
    ):
        assert (out / name).is_file(), name

    manifest = json.loads((out / "run_manifest.json").read_text("utf-8"))
    assert manifest["document_version"] == "2.1"
    assert manifest["seed"] == 42


def test_paper_mode_defaults_to_internal_paper_execution(
    capsys, tmp_path: Path
) -> None:
    # Paper fills are simulated and labelled internal_paper unless venue_testnet is
    # configured with a real venue adapter (resolve_execution_mode); a simulated fill
    # is never labelled venue_testnet.
    out = tmp_path / "paper"
    document = _emit(
        capsys, "mr-paper", "--output", str(out), "--run-id", "cli", "--steps", "220"
    )
    assert document["mode"] == "paper"
    manifest = json.loads((out / "run_manifest.json").read_text("utf-8"))
    assert manifest["config"]["matching"]["mode"] == "internal_paper"


def test_tca_and_reconcile_read_a_finished_run(capsys, tmp_path: Path) -> None:
    out = tmp_path / "run"
    _emit(
        capsys,
        "mr-backtest",
        "--output",
        str(out),
        "--run-id",
        "cli",
        "--steps",
        "220",
    )

    tca = _emit(capsys, "mr-tca", "--run-dir", str(out))
    assert int(tca["totals"]["plans"]) >= 0
    assert tca["by_venue"]
    assert tca["by_instrument"]
    assert "rejects_by_reason" in tca

    reconciled = _emit(
        capsys,
        "mr-reconcile",
        "--ledger",
        str(out / "ledger.sqlite"),
        "--run-id",
        "cli",
    )
    assert reconciled["events"] > 0
    assert reconciled["chain_digest"]
    assert reconciled["state_hash"]


def test_reconcile_reports_a_missing_ledger(capsys, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _emit(
            capsys,
            "mr-reconcile",
            "--ledger",
            str(tmp_path / "nope.sqlite"),
            "--run-id",
            "cli",
        )


def test_benchmark_command_reports_measurements(capsys, tmp_path: Path) -> None:
    document = _emit(
        capsys, "mr-benchmark", "--output", str(tmp_path / "bench"), "--steps", "120"
    )
    assert document["measurements"]
    assert document["thresholds"]
    assert "passed" in document


def test_deliverables_command_writes_every_artifact(capsys, tmp_path: Path) -> None:
    out = tmp_path / "deliverables"
    written = _emit(capsys, "mr-deliverables", "--output", str(out))
    assert written
    for path in written.values():
        assert Path(path).is_file(), path


def test_acceptance_command_can_run_a_subset(capsys, tmp_path: Path) -> None:
    summary = _emit(
        capsys,
        "mr-acceptance",
        "--output",
        str(tmp_path / "acceptance"),
        "--only",
        "AT-11",
    )
    assert summary["total"] == 1
    assert summary["failed"] == []
    assert summary["passed"] is True


# ------------------------------------------------------------------- config
def test_minreq_config_defaults_are_valid() -> None:
    settings = AppConfig().minreq
    assert len(set(settings.venues)) >= 2
    assert settings.exit_z < settings.entry_z
    assert settings.steps > 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"venues": ("binance",)},
        {"venues": ("binance", "binance")},
        {"entry_z": "1.0", "exit_z": "2.0"},
        {"steps": 0},
    ],
)
def test_minreq_config_rejects_unusable_settings(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        MinReqConfig(**overrides)
