from __future__ import annotations

import json
import hashlib
from functools import lru_cache
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO

import msgspec

from trading_platform_poc.models import MarketEvent
from trading_platform_poc.optional_dependencies import (
    require_pyarrow,
    require_pyarrow_parquet,
)


@lru_cache(maxsize=1)
def _event_schema() -> Any:
    """Build the Parquet schema on first use.

    Deferred so that importing this module never requires pyarrow's native
    library to load.
    """
    pa = require_pyarrow()
    return pa.schema(
        [
            ("event_id", pa.string()),
            ("run_id", pa.string()),
            ("venue", pa.string()),
            ("symbol", pa.string()),
            ("event_type", pa.string()),
            ("exchange_sequence", pa.int64()),
            ("exchange_timestamp_ns", pa.int64()),
            ("receive_timestamp_ns", pa.int64()),
            ("process_timestamp_ns", pa.int64()),
            ("schema_version", pa.int32()),
            ("payload_json", pa.string()),
        ]
    )


class JsonlRecorder:
    def __init__(
        self,
        output_directory: Path,
        flush_every: int = 100,
        *,
        symbol: str | None = None,
        config_hash: str = "",
    ) -> None:
        if flush_every <= 0:
            raise ValueError("flush_every must be positive")
        self._output_directory = output_directory
        self._flush_every = flush_every
        self._configured_symbol = symbol
        self._config_hash = config_hash
        self._raw_file: TextIO | None = None
        self._event_file: TextIO | None = None
        self._quarantine_file: TextIO | None = None
        self._parquet_writer: Any | None = None
        self._parquet_rows: list[dict[str, Any]] = []
        self._pending_writes = 0
        self._event_count = 0
        self._quarantine_count = 0
        self._quarantine_counts: dict[str, int] = {}
        self._event_counts: dict[str, int] = {}
        self._symbols: set[str] = set()
        self._schema_versions: set[int] = set()

    def __enter__(self) -> JsonlRecorder:
        self._output_directory.mkdir(parents=True, exist_ok=True)
        self._raw_file = (self._output_directory / "raw.jsonl").open(
            "x", encoding="utf-8", newline="\n"
        )
        self._event_file = (self._output_directory / "events.jsonl").open(
            "xb"
        )
        self._quarantine_file = (
            self._output_directory / "quarantine.jsonl"
        ).open("x", encoding="utf-8", newline="\n")
        self._parquet_writer = require_pyarrow_parquet().ParquetWriter(
            self._output_directory / "events.parquet",
            _event_schema(),
            compression="zstd",
        )
        return self

    def write(
        self,
        raw_message: dict[str, Any],
        event: MarketEvent,
    ) -> None:
        self.write_raw(raw_message, event)
        self.write_event(event)

    def write_raw(
        self,
        raw_message: dict[str, Any],
        event: MarketEvent,
    ) -> None:
        if self._raw_file is None:
            raise RuntimeError("recorder is not open")
        self._raw_file.write(
            json.dumps(
                {
                    "receive_timestamp_ns": event.receive_timestamp_ns,
                    "message": raw_message,
                },
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        )

    def write_event(self, event: MarketEvent) -> None:
        if self._event_file is None:
            raise RuntimeError("recorder is not open")
        self._event_file.write(msgspec.json.encode(event) + b"\n")
        self._parquet_rows.append(
            {
                "event_id": str(event.event_id),
                "run_id": str(event.run_id),
                "venue": event.venue,
                "symbol": event.symbol,
                "event_type": event.event_type.value,
                "exchange_sequence": event.exchange_sequence,
                "exchange_timestamp_ns": event.exchange_timestamp_ns,
                "receive_timestamp_ns": event.receive_timestamp_ns,
                "process_timestamp_ns": event.process_timestamp_ns,
                "schema_version": event.schema_version,
                "payload_json": msgspec.json.encode(event.payload).decode(),
            }
        )
        self._event_count += 1
        self._event_counts[event.event_type.value] = (
            self._event_counts.get(event.event_type.value, 0) + 1
        )
        self._symbols.add(event.symbol)
        self._schema_versions.add(event.schema_version)
        self._pending_writes += 1
        if self._pending_writes >= self._flush_every:
            self.flush()

    def quarantine(
        self,
        raw_message: dict[str, Any],
        event: MarketEvent,
        anomalies: list[Any],
    ) -> None:
        if self._quarantine_file is None:
            raise RuntimeError("recorder is not open")
        anomaly_rows = [
            anomaly.to_dict()
            if hasattr(anomaly, "to_dict")
            else dict(anomaly)
            for anomaly in anomalies
        ]
        self._quarantine_file.write(
            json.dumps(
                {
                    "receive_timestamp_ns": event.receive_timestamp_ns,
                    "message": raw_message,
                    "event": msgspec.json.decode(
                        msgspec.json.encode(event)
                    ),
                    "anomalies": anomaly_rows,
                },
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        )
        self._quarantine_count += 1
        for anomaly in anomaly_rows:
            code = str(anomaly.get("code", "unknown"))
            self._quarantine_counts[code] = (
                self._quarantine_counts.get(code, 0) + 1
            )

    def flush(self) -> None:
        if self._raw_file is not None:
            self._raw_file.flush()
        if self._event_file is not None:
            self._event_file.flush()
        if self._quarantine_file is not None:
            self._quarantine_file.flush()
        if self._parquet_writer is not None and self._parquet_rows:
            table = require_pyarrow().Table.from_pylist(
                self._parquet_rows,
                schema=_event_schema(),
            )
            self._parquet_writer.write_table(table)
            self._parquet_rows.clear()
        self._pending_writes = 0

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.flush()
        finally:
            for handle in (
                self._raw_file,
                self._event_file,
                self._quarantine_file,
            ):
                if handle is not None:
                    handle.close()
            # Closing the writer emits the Parquet footer. Without it every
            # row group already written is unreadable, so it must happen even
            # when the final flush fails.
            if self._parquet_writer is not None:
                self._parquet_writer.close()
        self._write_manifest()

    def _write_manifest(self) -> None:
        files: dict[str, dict[str, Any]] = {}
        for name in (
            "raw.jsonl",
            "events.jsonl",
            "events.parquet",
            "quarantine.jsonl",
        ):
            path = self._output_directory / name
            files[name] = {
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        symbol: str | list[str] | None = self._configured_symbol
        if symbol is None:
            symbols = sorted(self._symbols)
            symbol = symbols[0] if len(symbols) == 1 else symbols
        versions = sorted(self._schema_versions)
        schema_version: int | list[int] | None = (
            versions[0] if len(versions) == 1 else versions or None
        )
        manifest = {
            "dataset_id": self._output_directory.name,
            "symbol": symbol,
            "schema_version": schema_version,
            "config_hash": self._config_hash,
            "event_count": self._event_count,
            "event_counts": dict(sorted(self._event_counts.items())),
            "quarantine_count": self._quarantine_count,
            "quarantine_counts": dict(
                sorted(self._quarantine_counts.items())
            ),
            "checksums": {
                name: metadata["sha256"]
                for name, metadata in files.items()
            },
            "files": files,
        }
        (self._output_directory / "dataset_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def config_sha256(config: Any) -> str:
    if hasattr(config, "model_dump"):
        config = config.model_dump(mode="json")
    canonical = json.dumps(
        config,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


DatasetRecorder = JsonlRecorder
