import json
import hashlib
from decimal import Decimal
from uuid import UUID

import pyarrow.parquet as pq
import pytest

from trading_platform_poc.models import EventType, MarketEvent, TradeTick
from trading_platform_poc.data_quality import (
    AnomalyCode,
    AnomalySeverity,
    DataQualityAnomaly,
)
from trading_platform_poc.recorder import JsonlRecorder
from trading_platform_poc.replay import ReplaySource


def test_records_raw_and_normalized_events(tmp_path) -> None:
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
    output = tmp_path / "run"

    with JsonlRecorder(
        output,
        flush_every=1,
        symbol="BTCUSDT",
        config_hash="config-sha",
    ) as recorder:
        recorder.write({"data": {"e": "trade", "t": 42}}, event)

    raw = json.loads((output / "raw.jsonl").read_text(encoding="utf-8"))
    normalized = (output / "events.jsonl").read_bytes()
    parquet = pq.read_table(output / "events.parquet").to_pylist()
    manifest = json.loads(
        (output / "dataset_manifest.json").read_text(encoding="utf-8")
    )
    assert raw["receive_timestamp_ns"] == 1_100
    assert raw["message"]["data"]["t"] == 42
    assert b'"event_type":"trade"' in normalized

    replayed = list(
        ReplaySource(
            output / "events.parquet",
            UUID("00000000-0000-0000-0000-000000000003"),
        ).events()
    )
    assert len(replayed) == 1
    assert replayed[0].event_type is EventType.TRADE
    assert replayed[0].payload.trade_id == 42
    assert parquet[0]["event_type"] == "trade"
    assert json.loads(parquet[0]["payload_json"])["trade_id"] == 42
    assert manifest["event_count"] == 1
    assert manifest["event_counts"] == {"trade": 1}
    assert manifest["symbol"] == "BTCUSDT"
    assert manifest["schema_version"] == 1
    assert manifest["config_hash"] == "config-sha"
    assert manifest["quarantine_count"] == 0
    for name, metadata in manifest["files"].items():
        assert metadata["sha256"] == hashlib.sha256(
            (output / name).read_bytes()
        ).hexdigest()


def test_quarantines_rejected_events_and_reports_counts(tmp_path) -> None:
    event = MarketEvent(
        EVENT_ID := UUID("00000000-0000-0000-0000-000000000010"),
        UUID("00000000-0000-0000-0000-000000000011"),
        "binance",
        "BTCUSDT",
        EventType.TRADE,
        1,
        1_000,
        1_100,
        1_200,
        1,
        TradeTick(
            "BTCUSDT",
            1,
            Decimal("100"),
            Decimal("0"),
            False,
            1_000,
        ),
    )
    anomaly = DataQualityAnomaly(
        code=AnomalyCode.INVALID_QUANTITY,
        severity=AnomalySeverity.ERROR,
        message="invalid quantity",
        timestamp_ns=1_100,
        symbol="BTCUSDT",
        event_type="trade",
        blocking=True,
    )
    output = tmp_path / "quarantined"

    with JsonlRecorder(output) as recorder:
        recorder.quarantine({"data": {"e": "trade"}}, event, [anomaly])

    manifest = json.loads(
        (output / "dataset_manifest.json").read_text(encoding="utf-8")
    )
    quarantine = json.loads(
        (output / "quarantine.jsonl").read_text(encoding="utf-8")
    )
    assert manifest["event_count"] == 0
    assert manifest["quarantine_count"] == 1
    assert manifest["quarantine_counts"] == {"invalid_quantity": 1}
    assert quarantine["event"]["event_id"] == str(EVENT_ID)
    assert quarantine["message"]["data"]["e"] == "trade"
    assert (output / "raw.jsonl").read_text(encoding="utf-8") == ""

    with pytest.raises(ValueError, match="quarantined"):
        ReplaySource(output / "quarantine.jsonl", UUID(int=1))


def test_recorder_closes_handles_when_final_flush_fails(tmp_path) -> None:
    """A failed final flush must still emit the Parquet footer.

    Without the footer every row group already written becomes unreadable,
    which would silently destroy a completed recording session.
    """
    event = MarketEvent(
        event_id=UUID("00000000-0000-0000-0000-000000000101"),
        run_id=UUID("00000000-0000-0000-0000-000000000102"),
        venue="binance",
        symbol="BTCUSDT",
        event_type=EventType.TRADE,
        exchange_sequence=7,
        exchange_timestamp_ns=1_000,
        receive_timestamp_ns=1_100,
        process_timestamp_ns=1_200,
        schema_version=1,
        payload=TradeTick(
            symbol="BTCUSDT",
            trade_id=7,
            price=Decimal("100.50"),
            quantity=Decimal("0.25"),
            buyer_is_maker=True,
            trade_timestamp_ns=1_000,
        ),
    )
    output = tmp_path / "run"
    recorder = JsonlRecorder(
        output,
        flush_every=1,
        symbol="BTCUSDT",
        config_hash="config-sha",
    )

    def _boom() -> None:
        raise OSError("disk full")

    with pytest.raises(OSError):
        with recorder:
            recorder.write({"data": {"e": "trade", "t": 7}}, event)
            recorder.flush = _boom

    assert recorder._raw_file.closed
    assert recorder._event_file.closed
    assert recorder._quarantine_file.closed
    assert pq.read_table(output / "events.parquet").num_rows == 1
    assert not (output / "dataset_manifest.json").exists()


def test_recorded_jsonl_uses_lf_endings_on_every_platform(tmp_path) -> None:
    """Dataset bytes must not depend on the host platform.

    Text mode translates "\\n" to "\\r\\n" on Windows, which would change every
    recorded file's sha256 and break cross-platform dataset verification.
    """
    event = MarketEvent(
        event_id=UUID("00000000-0000-0000-0000-000000000201"),
        run_id=UUID("00000000-0000-0000-0000-000000000202"),
        venue="binance",
        symbol="BTCUSDT",
        event_type=EventType.TRADE,
        exchange_sequence=9,
        exchange_timestamp_ns=1_000,
        receive_timestamp_ns=1_100,
        process_timestamp_ns=1_200,
        schema_version=1,
        payload=TradeTick(
            symbol="BTCUSDT",
            trade_id=9,
            price=Decimal("100.50"),
            quantity=Decimal("0.25"),
            buyer_is_maker=True,
            trade_timestamp_ns=1_000,
        ),
    )
    output = tmp_path / "run"

    with JsonlRecorder(
        output,
        flush_every=1,
        symbol="BTCUSDT",
        config_hash="config-sha",
    ) as recorder:
        recorder.write({"data": {"e": "trade", "t": 9}}, event)
        recorder.quarantine(
            {"data": {"e": "trade", "t": 10}},
            event,
            [
                DataQualityAnomaly(
                    code=AnomalyCode.INVALID_QUANTITY,
                    severity=AnomalySeverity.ERROR,
                    message="invalid quantity",
                    timestamp_ns=1_100,
                    symbol="BTCUSDT",
                    event_type="trade",
                    blocking=True,
                )
            ],
        )

    for name in ("raw.jsonl", "events.jsonl", "quarantine.jsonl"):
        payload = (output / name).read_bytes()
        assert payload, f"{name} is empty"
        assert b"\r\n" not in payload, f"{name} contains CRLF"
