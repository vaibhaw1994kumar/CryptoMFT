from pathlib import Path

import pytest

from trading_platform_poc.cli import _load_config, build_parser


def test_uses_default_config_when_repository_yaml_is_absent(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    config = _load_config(Path("config.yaml"))

    assert config.symbol == "BTCUSDT"


def test_explicit_missing_config_still_fails(tmp_path) -> None:
    path = tmp_path / "missing.yaml"

    with pytest.raises(FileNotFoundError, match="does not exist"):
        _load_config(path)


def test_hftbacktest_market_making_commands_are_parseable() -> None:
    parser = build_parser()

    synthetic = parser.parse_args(
        [
            "hft-synthetic",
            "--duration-seconds",
            "10",
            "--volatility-bps-per-sqrt-second",
            "12",
            "--trade-probability",
            "0.8",
        ]
    )
    assert synthetic.command == "hft-synthetic"
    assert synthetic.duration_seconds == 10
    assert synthetic.volatility_bps_per_sqrt_second == 12
    assert synthetic.trade_probability == 0.8

    grid = parser.parse_args(
        [
            "hft-grid",
            "--data",
            "data.npz",
            "--snapshot",
            "snapshot.npz",
            "--inventory-skew",
            "10",
        ]
    )
    assert grid.command == "hft-grid"
    assert grid.inventory_skew == 10
    assert grid.data == [Path("data.npz")]

    order = parser.parse_args(
        [
            "hft-order-demo",
            "--data",
            "data.npz",
            "--snapshot",
            "snapshot.npz",
            "--scenario",
            "limit_cancel",
        ]
    )
    assert order.scenario == "limit_cancel"

    binance = parser.parse_args(
        [
            "hft-convert-binance",
            "--input",
            "raw.gz",
            "--output",
            "data.npz",
        ]
    )
    assert binance.command == "hft-convert-binance"

    tardis = parser.parse_args(
        [
            "hft-convert-tardis",
            "--trade",
            "trades.csv.gz",
            "--depth",
            "incremental_book.csv.gz",
            "--output",
            "data.npz",
        ]
    )
    assert tardis.trade == [Path("trades.csv.gz")]

    snapshot = parser.parse_args(
        [
            "hft-snapshot",
            "--data",
            "day1.npz",
            "--data",
            "day2.npz",
            "--tick-size",
            "0.1",
            "--lot-size",
            "0.001",
            "--output",
            "eod.npz",
        ]
    )
    assert snapshot.data == [Path("day1.npz"), Path("day2.npz")]
