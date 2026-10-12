from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from typing import Any
from collections.abc import Callable

from .adapters import normalize_binance_depth, normalize_bybit_depth
from .config import load_config
from .models import MarketEvent, json_default, utc_now_iso


def build_envelope_normalizer(config_path: Path) -> Callable[[dict[str, Any]], MarketEvent]:
    cfg, _ = load_config(config_path)
    symbols = {
        (venue, cfg.instruments.get(venue, cfg.instrument_id).symbol): cfg.instrument_id
        for venue in cfg.venues
    }

    def resolve(venue: str, symbol: str) -> str:
        try:
            return symbols[(venue, symbol)]
        except KeyError as exc:
            raise ValueError(f"unmapped venue symbol {venue}/{symbol}") from exc

    adapters = {"BINANCE": normalize_binance_depth, "BYBIT": normalize_bybit_depth}

    def normalize(envelope: dict[str, Any]) -> MarketEvent:
        venue = envelope["venue"]
        return adapters[venue](
            envelope["payload"],
            int(envelope["receive_ts_ns"]),
            resolve,
            raw_ref=envelope.get("raw_ref", ""),
            adapter_version=envelope.get("adapter_version", ""),
        )

    return normalize


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_raw_file(config_path: Path, raw_path: Path, output_path: Path) -> dict[str, Any]:
    normalize = build_envelope_normalizer(config_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    rejected = 0
    errors: list[dict[str, Any]] = []
    trace_path = output_path.with_suffix(output_path.suffix + ".trace.jsonl")
    quarantine_path = output_path.with_suffix(output_path.suffix + ".quarantine.jsonl")
    with (
        raw_path.open(encoding="utf-8") as source,
        output_path.open("w", encoding="utf-8") as sink,
        trace_path.open("w", encoding="utf-8") as trace,
        quarantine_path.open("w", encoding="utf-8") as quarantine,
    ):
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                envelope = json.loads(line)
                venue = envelope["venue"]
                raw_ref = envelope.get("raw_ref", "")
                event = normalize(envelope)
                sink.write(json.dumps(asdict(event), default=json_default, sort_keys=True) + "\n")
                trace.write(json.dumps({
                    "raw_ref": raw_ref,
                    "canonical_event_id": event.event_id,
                    "venue": venue,
                    "channel": envelope.get("channel", ""),
                    "host": envelope.get("host", ""),
                    "receive_ts_ns": event.receive_ts_ns,
                    "source_ts_ns": event.source_ts_ns,
                    "exchange_ts_ns": event.exchange_ts_ns,
                    "sequence": event.sequence,
                    "sequence_start": event.sequence_start,
                    "quality_flags": event.quality_flags,
                    "adapter_version": event.adapter_version,
                    "canonical_schema_version": event.schema_version,
                }, sort_keys=True) + "\n")
                count += 1
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                rejected += 1
                errors.append({"line": line_number, "error": str(exc)})
                quarantine.write(json.dumps({
                    "line": line_number,
                    "error": str(exc),
                    "raw_line": line.rstrip("\n"),
                }, sort_keys=True) + "\n")
    manifest = {
        "created_at": utc_now_iso(),
        "source": str(raw_path),
        "source_sha256": _sha256(raw_path),
        "output": str(output_path),
        "output_sha256": _sha256(output_path),
        "trace": str(trace_path),
        "trace_sha256": _sha256(trace_path),
        "quarantine": str(quarantine_path),
        "quarantine_sha256": _sha256(quarantine_path),
        "normalized_records": count,
        "rejected_records": rejected,
        "errors": errors,
        "adapter_version": "0.1.0",
    }
    manifest_path = output_path.with_suffix(output_path.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest
