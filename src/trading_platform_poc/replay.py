from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import msgspec

from trading_platform_poc.binance_feed import BinanceNormalizer
from trading_platform_poc.clock import SimulatedClock
from trading_platform_poc.datasets import list_datasets
from trading_platform_poc.models import (
    BookDelta,
    BookSnapshot,
    EventType,
    MarketEvent,
    TradeTick,
)
from trading_platform_poc.optional_dependencies import require_pyarrow_parquet


class ReplaySource:
    def __init__(self, data_path: Path, run_id: UUID) -> None:
        self._data_path = self._resolve_data_path(data_path)
        self._run_id = run_id
        self._clock = SimulatedClock()
        self._normalizer = BinanceNormalizer(run_id, self._clock)

    @property
    def data_path(self) -> Path:
        return self._data_path

    def events(self) -> Iterator[MarketEvent]:
        if self._data_path.suffix == ".parquet":
            yield from self._parquet_events()
            return
        with self._data_path.open(encoding="utf-8") as replay_file:
            for line_number, line in enumerate(replay_file, 1):
                if not line.strip():
                    continue
                envelope = json.loads(line)
                if "anomalies" in envelope:
                    continue
                message, receive_timestamp_ns = self._unpack_envelope(envelope)
                replay_timestamp_ns = max(
                    receive_timestamp_ns,
                    self._clock.now_ns(),
                )
                self._clock.advance_to(replay_timestamp_ns)
                if message.get("source") == "rest_snapshot":
                    event = self._normalizer.normalize_snapshot(
                        message["data"],
                        str(message["symbol"]),
                    )
                else:
                    event = self._normalizer.normalize(message)
                if event is None:
                    continue
                yield event

    async def paced_events(
        self,
        speed: str | float = "max",
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> AsyncIterator[MarketEvent]:
        multiplier = _speed_multiplier(speed)
        previous_timestamp_ns: int | None = None
        for event in self.events():
            if multiplier is not None and previous_timestamp_ns is not None:
                delay_ns = max(
                    0,
                    event.receive_timestamp_ns - previous_timestamp_ns,
                )
                if delay_ns:
                    await sleep(delay_ns / 1_000_000_000 / multiplier)
            previous_timestamp_ns = event.receive_timestamp_ns
            yield event

    @staticmethod
    def _resolve_data_path(data_path: Path) -> Path:
        if data_path.is_dir():
            raw_path = data_path / "raw.jsonl"
            parquet_path = data_path / "events.parquet"
            resolved = raw_path if raw_path.is_file() else parquet_path
        else:
            resolved = data_path
        if resolved.name == "quarantine.jsonl":
            raise ValueError(
                "quarantined events cannot be used as a replay source"
            )
        if not resolved.is_file():
            available = list_datasets(
                data_path.parent
                if data_path.parent != Path(".")
                else Path("data")
            )
            available_paths = ", ".join(
                item["path"] for item in available[:10]
            )
            hint = (
                f" Available datasets: {available_paths}."
                if available_paths
                else " Run the record or tardis-import command first."
            )
            raise FileNotFoundError(
                f"replay dataset does not exist: {resolved}."
                f"{hint} Do not use the literal DATASET_ID placeholder."
            )
        return resolved

    @staticmethod
    def _unpack_envelope(
        envelope: dict[str, Any],
    ) -> tuple[dict[str, Any], int]:
        if "message" in envelope and "receive_timestamp_ns" in envelope:
            return envelope["message"], int(envelope["receive_timestamp_ns"])
        data = envelope.get("data", envelope)
        exchange_timestamp_ms = data.get("E") or data.get("T") or 0
        return envelope, int(exchange_timestamp_ms) * 1_000_000

    def _parquet_events(self) -> Iterator[MarketEvent]:
        pq = require_pyarrow_parquet()
        payload_types = {
            EventType.BOOK_SNAPSHOT: BookSnapshot,
            EventType.BOOK_DELTA: BookDelta,
            EventType.TRADE: TradeTick,
        }
        for row in pq.read_table(self._data_path).to_pylist():
            event_type = EventType(row["event_type"])
            payload_type = payload_types.get(event_type)
            if payload_type is None:
                continue
            payload = msgspec.json.decode(
                row["payload_json"].encode("utf-8"),
                type=payload_type,
            )
            replay_timestamp_ns = max(
                int(row["receive_timestamp_ns"]),
                self._clock.now_ns(),
            )
            self._clock.advance_to(replay_timestamp_ns)
            yield MarketEvent(
                event_id=UUID(row["event_id"]),
                run_id=self._run_id,
                venue=row["venue"],
                symbol=row["symbol"],
                event_type=event_type,
                exchange_sequence=row["exchange_sequence"],
                exchange_timestamp_ns=int(row["exchange_timestamp_ns"]),
                receive_timestamp_ns=replay_timestamp_ns,
                process_timestamp_ns=max(
                    replay_timestamp_ns,
                    int(row["process_timestamp_ns"]),
                ),
                schema_version=int(row["schema_version"]),
                payload=payload,
            )


def _speed_multiplier(speed: str | float) -> float | None:
    if isinstance(speed, (int, float)):
        if speed <= 0:
            raise ValueError("replay speed must be positive")
        return float(speed)
    normalized = speed.lower().replace("_", "-")
    if normalized == "max":
        return None
    if normalized in {"normal", "real-time", "realtime", "1x"}:
        return 1.0
    if normalized.endswith("x"):
        normalized = normalized[:-1]
    try:
        multiplier = float(normalized)
    except ValueError as error:
        raise ValueError(
            "replay speed must be max, real-time, normal, or a positive multiplier"
        ) from error
    if multiplier <= 0:
        raise ValueError("replay speed must be positive")
    return multiplier
