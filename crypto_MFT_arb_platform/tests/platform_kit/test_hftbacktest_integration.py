import json
from decimal import Decimal
from uuid import UUID

import hftbacktest
import numpy as np
import pytest

from trading_platform_poc.hftbacktest_integration import (
    export_hftbacktest_npz,
    hftbacktest_health,
)
from trading_platform_poc.models import EventType, MarketEvent, TradeTick
from trading_platform_poc.recorder import JsonlRecorder


def test_exports_real_hftbacktest_event_array(tmp_path) -> None:
    event = MarketEvent(
        event_id=UUID("00000000-0000-0000-0000-000000000001"),
        run_id=UUID("00000000-0000-0000-0000-000000000002"),
        venue="binance",
        symbol="BTCUSDT",
        event_type=EventType.TRADE,
        exchange_sequence=42,
        exchange_timestamp_ns=1_000,
        receive_timestamp_ns=1_100,
        process_timestamp_ns=1_200,
        schema_version=1,
        payload=TradeTick(
            symbol="BTCUSDT",
            trade_id=42,
            price=Decimal("100.50"),
            quantity=Decimal("0.25"),
            buyer_is_maker=True,
            trade_timestamp_ns=1_000,
        ),
    )
    dataset = tmp_path / "dataset"
    with JsonlRecorder(dataset) as recorder:
        recorder.write({"e": "trade"}, event)

    output = tmp_path / "hft-events.npz"
    assert export_hftbacktest_npz(dataset, output) == 1

    with np.load(output) as archive:
        data = archive["data"]
    assert data.dtype == hftbacktest.event_dtype
    assert data[0]["ev"] & hftbacktest.TRADE_EVENT
    assert data[0]["ev"] & hftbacktest.SELL_EVENT
    assert data[0]["order_id"] == 0
    assert hftbacktest.BacktestAsset().data(data) is not None


def test_hftbacktest_health_imports_installed_package() -> None:
    health = hftbacktest_health()
    assert health["version"] == hftbacktest.__version__
    assert health["event_dtype"] == list(hftbacktest.event_dtype.names)


def test_rejects_its_own_output_as_input(tmp_path) -> None:
    npz = tmp_path / "hft-events.npz"
    np.savez_compressed(npz, data=np.zeros(1, dtype=hftbacktest.event_dtype))

    with pytest.raises(ValueError) as error:
        export_hftbacktest_npz(npz, tmp_path / "again.npz")

    message = str(error.value)
    assert "output of this command" in message
    assert "events.jsonl" in message


def test_rejects_a_binary_file_without_a_codec_error(tmp_path) -> None:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "events.jsonl").write_bytes(b"\x00\xfb\xff not utf-8")

    with pytest.raises(ValueError) as error:
        export_hftbacktest_npz(dataset, tmp_path / "out.npz")

    assert "binary, not a text event log" in str(error.value)


def test_reports_the_line_that_is_not_json(tmp_path) -> None:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "events.jsonl").write_text(
        json.dumps(
            {
                "event_type": "book_snapshot",
                "exchange_timestamp_ns": 1_000,
                "receive_timestamp_ns": 1_100,
                "payload": {"bids": [], "asks": []},
            }
        )
        + "\nnot json at all\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as error:
        export_hftbacktest_npz(dataset, tmp_path / "out.npz")

    assert "line 2 is not JSON" in str(error.value)


def test_names_the_missing_field_for_a_foreign_event_schema(tmp_path) -> None:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "events.jsonl").write_text(
        json.dumps(
            {
                "event_type": "trade",
                "exchange_timestamp_ns": 1_000,
                "receive_timestamp_ns": 1_100,
                "payload": {"price": "100.5", "quantity": "0.25"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as error:
        export_hftbacktest_npz(dataset, tmp_path / "out.npz")

    message = str(error.value)
    assert "event 1" in message
    assert "buyer_is_maker" in message
    assert "mr-generate" in message
