from __future__ import annotations

from dataclasses import asdict, dataclass
import base64
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid5


RAW_SCHEMA_VERSION = "1.0.0"
GENESIS_HASH = "0" * 64


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def infer_sequence(venue: str, payload: dict[str, Any]) -> int | str | None:
    if venue.upper() == "BINANCE":
        return payload.get("u", payload.get("lastUpdateId", payload.get("a")))
    if venue.upper() == "BYBIT":
        data = payload.get("data", {})
        if isinstance(data, dict):
            return data.get("u", data.get("seq"))
    return payload.get("sequence", payload.get("seq"))


@dataclass(frozen=True, slots=True)
class RawFrame:
    raw_ref: str
    raw_schema_version: str
    adapter_version: str
    venue: str
    vendor: str
    channel: str
    host: str
    transport: str
    receive_ts_ns: int
    sequence: int | str | None
    payload_encoding: str
    frame_capture_mode: str
    frame_encoding: str
    frame_data: str
    frame_sha256: str
    payload_sha256: str
    previous_record_hash: str
    record_hash: str
    payload: dict[str, Any]
    wire_ref: str


class RawCaptureWriter:
    """Append-only, hash-chained storage for untouched public feed payloads."""

    def __init__(
        self,
        output_dir: Path,
        session_id: str,
        *,
        durable: bool = True,
    ) -> None:
        self.output_dir = output_dir
        self.session_id = session_id
        self.session_dir = output_dir / session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.raw_path = self.session_dir / "raw.jsonl"
        if self.raw_path.exists() and self.raw_path.stat().st_size:
            raise FileExistsError(
                f"raw session already exists and will not be overwritten: {self.raw_path}"
            )
        self._handle = self.raw_path.open("x", encoding="utf-8")
        self._durable = durable
        self._previous_hash = GENESIS_HASH
        self._count = 0
        self._first_ref: str | None = None
        self._last_ref: str | None = None
        self._first_receive_ts_ns: int | None = None
        self._last_receive_ts_ns: int | None = None
        self._sources: set[tuple[str, str, str, str]] = set()
        self._closed = False

    def append(
        self,
        *,
        venue: str,
        vendor: str,
        channel: str,
        host: str,
        receive_ts_ns: int,
        payload: dict[str, Any],
        adapter_version: str,
        original_frame: str | bytes | None = None,
        sequence: int | str | None = None,
        transport: str = "WEBSOCKET",
        payload_encoding: str = "JSON",
        wire_ref: str = "",
    ) -> RawFrame:
        if self._closed:
            raise RuntimeError("raw capture writer is closed")
        venue = venue.upper()
        sequence = infer_sequence(venue, payload) if sequence is None else sequence
        payload_sha256 = _hash(payload)
        if isinstance(original_frame, bytes):
            frame_bytes = original_frame
            frame_data = base64.b64encode(frame_bytes).decode("ascii")
            frame_encoding = "BASE64"
            frame_capture_mode = "WIRE_BINARY"
        elif isinstance(original_frame, str):
            frame_bytes = original_frame.encode("utf-8")
            frame_data = original_frame
            frame_encoding = "UTF8"
            frame_capture_mode = "WIRE_TEXT"
        else:
            frame_data = _canonical_json(payload)
            frame_bytes = frame_data.encode("utf-8")
            frame_encoding = "UTF8_JSON"
            frame_capture_mode = "RECONSTRUCTED_FROM_PARSED"
        frame_sha256 = sha256(frame_bytes).hexdigest()
        ordinal = self._count + 1
        raw_ref = str(uuid5(
            NAMESPACE_URL,
            f"raw|{self.session_id}|{ordinal}|{venue}|{channel}|{receive_ts_ns}|{sequence}|{payload_sha256}",
        ))
        unsigned = {
            "raw_ref": raw_ref,
            "raw_schema_version": RAW_SCHEMA_VERSION,
            "adapter_version": adapter_version,
            "venue": venue,
            "vendor": vendor,
            "channel": channel,
            "host": host,
            "transport": transport,
            "receive_ts_ns": int(receive_ts_ns),
            "sequence": sequence,
            "payload_encoding": payload_encoding,
            "frame_capture_mode": frame_capture_mode,
            "frame_encoding": frame_encoding,
            "frame_data": frame_data,
            "frame_sha256": frame_sha256,
            "payload_sha256": payload_sha256,
            "previous_record_hash": self._previous_hash,
            "payload": payload,
            "wire_ref": wire_ref,
        }
        record_hash = _hash(unsigned)
        frame = RawFrame(record_hash=record_hash, **unsigned)
        self._handle.write(_canonical_json(asdict(frame)) + "\n")
        self._handle.flush()
        if self._durable:
            os.fsync(self._handle.fileno())
        self._previous_hash = record_hash
        self._count = ordinal
        self._first_ref = self._first_ref or raw_ref
        self._last_ref = raw_ref
        self._first_receive_ts_ns = self._first_receive_ts_ns or int(receive_ts_ns)
        self._last_receive_ts_ns = int(receive_ts_ns)
        self._sources.add((venue, vendor, channel, host))
        return frame

    def close(self) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("raw capture writer already closed")
        self._handle.flush()
        if self._durable:
            os.fsync(self._handle.fileno())
        self._handle.close()
        self._closed = True
        manifest = {
            "session_id": self.session_id,
            "raw_schema_version": RAW_SCHEMA_VERSION,
            "record_count": self._count,
            "first_raw_ref": self._first_ref,
            "last_raw_ref": self._last_ref,
            "first_receive_ts_ns": self._first_receive_ts_ns,
            "last_receive_ts_ns": self._last_receive_ts_ns,
            "terminal_record_hash": self._previous_hash,
            "raw_file": str(self.raw_path),
            "raw_file_sha256": _file_hash(self.raw_path),
            "sources": [
                {"venue": v, "vendor": d, "channel": c, "host": h}
                for v, d, c, h in sorted(self._sources)
            ],
            "closed_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        manifest_path = self.session_dir / "raw_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        return manifest

    def __enter__(self) -> "RawCaptureWriter":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if not self._closed:
            self.close()


def capture_envelopes(
    envelopes: Iterable[dict[str, Any]], output_dir: Path, session_id: str
) -> dict[str, Any]:
    writer = RawCaptureWriter(output_dir, session_id)
    try:
        for envelope in envelopes:
            writer.append(
                venue=envelope["venue"],
                vendor=envelope.get("vendor", envelope["venue"]),
                channel=envelope["channel"],
                host=envelope["host"],
                receive_ts_ns=int(envelope["receive_ts_ns"]),
                sequence=envelope.get("sequence"),
                adapter_version=envelope["adapter_version"],
                original_frame=envelope.get("original_frame"),
                transport=envelope.get("transport", "WEBSOCKET"),
                payload_encoding=envelope.get("payload_encoding", "JSON"),
                payload=envelope["payload"],
            )
        return writer.close()
    except Exception:
        if not writer._closed:
            writer._handle.close()
        raise


def verify_raw_file(path: Path) -> dict[str, Any]:
    previous = GENESIS_HASH
    count = 0
    first_ref = None
    last_ref = None
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            record = json.loads(line)
            recorded_hash = record.pop("record_hash")
            if record.get("previous_record_hash") != previous:
                raise ValueError(f"hash-chain break at line {line_number}")
            if _hash(record.get("payload")) != record.get("payload_sha256"):
                raise ValueError(f"payload checksum mismatch at line {line_number}")
            frame_data = record.get("frame_data", "")
            if record.get("frame_encoding") == "BASE64":
                frame_bytes = base64.b64decode(frame_data, validate=True)
            else:
                frame_bytes = frame_data.encode("utf-8")
            if sha256(frame_bytes).hexdigest() != record.get("frame_sha256"):
                raise ValueError(f"original frame checksum mismatch at line {line_number}")
            calculated = _hash(record)
            if calculated != recorded_hash:
                raise ValueError(f"record checksum mismatch at line {line_number}")
            previous = recorded_hash
            count += 1
            first_ref = first_ref or record["raw_ref"]
            last_ref = record["raw_ref"]
    return {
        "valid": True,
        "record_count": count,
        "first_raw_ref": first_ref,
        "last_raw_ref": last_ref,
        "terminal_record_hash": previous,
        "raw_file_sha256": _file_hash(path),
    }
