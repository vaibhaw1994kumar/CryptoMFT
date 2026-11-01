"""Restartable raw-to-canonical ingestion with partitions, manifests and gates.

MR-04 requires idempotent jobs, checkpointing, backfill, partition-level
completion state and an approved-dataset concept. AT-05 requires an interrupted
job to resume without duplication or omission and produce the same manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from trading_platform_poc.minreq.canonical import (
    CanonicalEvent,
    RawReference,
    SCHEMA_VERSION,
    decode_event,
    encode_event,
)
from trading_platform_poc.minreq.quality import (
    QualityMonitor,
    QualityReport,
    QualityThresholds,
)
from trading_platform_poc.minreq.securitymaster import SecurityMaster
from trading_platform_poc.minreq.venues import (
    ADAPTER_VERSION,
    NormalizationError,
    RawRecord,
    build_normalizer,
)

INGESTION_VERSION = "1.0.0"
NORMALIZATION_VERSION = "1.1.0"
QUARANTINE_PATH = "quarantine/quarantine.jsonl"
NORMALIZATION_ERRORS = (
    NormalizationError,
    KeyError,
    ValueError,
    TypeError,
    IndexError,
    InvalidOperation,
)
DAY_NS = 86_400_000_000_000


def partition_date(timestamp_ns: int) -> str:
    return datetime.fromtimestamp(timestamp_ns / 1e9, tz=timezone.utc).strftime(
        "%Y-%m-%d"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_digest(record: RawRecord) -> str:
    return hashlib.sha256(record.to_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PartitionKey:
    venue: str
    channel: str
    date: str

    @property
    def relative(self) -> str:
        return f"{self.venue}/{self.date}/{self.channel}.jsonl"

    def as_dict(self) -> dict[str, str]:
        return {"venue": self.venue, "channel": self.channel, "date": self.date}


@dataclass
class PartitionState:
    key: PartitionKey
    records: int = 0
    first_receive_ns: int = 0
    last_receive_ns: int = 0
    complete: bool = False

    def observe(self, receive_ns: int) -> None:
        if self.records == 0:
            self.first_receive_ns = receive_ns
        self.records += 1
        self.last_receive_ns = max(self.last_receive_ns, receive_ns)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.key.as_dict(),
            "path": self.key.relative,
            "records": self.records,
            "first_receive_ns": self.first_receive_ns,
            "last_receive_ns": self.last_receive_ns,
            "complete": self.complete,
        }


@dataclass
class IngestionCheckpoint:
    """Durable state that makes an interrupted ingestion resumable."""

    dataset_id: str
    ingestion_version: str = INGESTION_VERSION
    records_written: int = 0
    duplicates_skipped: int = 0
    last_receive_ns: int = 0
    digests: set[str] = field(default_factory=set)
    partitions: dict[str, PartitionState] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "ingestion_version": self.ingestion_version,
            "records_written": self.records_written,
            "duplicates_skipped": self.duplicates_skipped,
            "last_receive_ns": self.last_receive_ns,
            "digests": sorted(self.digests),
            "partitions": {
                name: state.to_dict()
                for name, state in sorted(self.partitions.items())
            },
        }

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> IngestionCheckpoint:
        checkpoint = cls(
            dataset_id=str(document["dataset_id"]),
            ingestion_version=str(
                document.get("ingestion_version", INGESTION_VERSION)
            ),
            records_written=int(document.get("records_written", 0)),
            duplicates_skipped=int(document.get("duplicates_skipped", 0)),
            last_receive_ns=int(document.get("last_receive_ns", 0)),
            digests=set(document.get("digests", [])),
        )
        for name, state in document.get("partitions", {}).items():
            key = PartitionKey(
                venue=state["venue"], channel=state["channel"], date=state["date"]
            )
            checkpoint.partitions[name] = PartitionState(
                key=key,
                records=int(state.get("records", 0)),
                first_receive_ns=int(state.get("first_receive_ns", 0)),
                last_receive_ns=int(state.get("last_receive_ns", 0)),
                complete=bool(state.get("complete", False)),
            )
        return checkpoint


class IngestionInterrupted(RuntimeError):
    """Raised by fault injection to simulate a crashed ingestion job."""


class RawIngestor:
    """Append-only, idempotent raw capture into date/venue/channel partitions."""

    def __init__(self, dataset_root: Path, dataset_id: str | None = None) -> None:
        self.root = Path(dataset_root)
        self.dataset_id = dataset_id or self.root.name
        self.raw_root = self.root / "raw"
        self.checkpoint_path = self.root / "_checkpoint.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self.raw_root.mkdir(parents=True, exist_ok=True)
        self.checkpoint = self._load_checkpoint()

    def _load_checkpoint(self) -> IngestionCheckpoint:
        if self.checkpoint_path.is_file():
            return IngestionCheckpoint.from_dict(
                json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            )
        return IngestionCheckpoint(dataset_id=self.dataset_id)

    def save_checkpoint(self) -> None:
        temporary = self.checkpoint_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.checkpoint.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.checkpoint_path)

    def partition_path(self, key: PartitionKey) -> Path:
        return self.raw_root / key.relative

    def ingest(
        self,
        records: Iterable[RawRecord],
        *,
        checkpoint_every: int = 100,
        fail_after: int | None = None,
    ) -> IngestionCheckpoint:
        """Write records, skipping any digest already durably captured.

        ``fail_after`` raises :class:`IngestionInterrupted` after N *new*
        records so restart behaviour can be tested deterministically.
        """

        handles: dict[str, Any] = {}
        written_since_checkpoint = 0
        new_records = 0
        try:
            for record in records:
                digest = record_digest(record)
                if digest in self.checkpoint.digests:
                    self.checkpoint.duplicates_skipped += 1
                    continue
                key = PartitionKey(
                    venue=record.venue,
                    channel=record.channel,
                    date=partition_date(record.receive_timestamp_ns),
                )
                name = key.relative
                handle = handles.get(name)
                if handle is None:
                    path = self.partition_path(key)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    handle = path.open("a", encoding="utf-8", newline="\n")
                    handles[name] = handle
                handle.write(record.to_json() + "\n")
                state = self.checkpoint.partitions.setdefault(
                    name, PartitionState(key=key)
                )
                state.observe(record.receive_timestamp_ns)
                self.checkpoint.digests.add(digest)
                self.checkpoint.records_written += 1
                self.checkpoint.last_receive_ns = max(
                    self.checkpoint.last_receive_ns, record.receive_timestamp_ns
                )
                written_since_checkpoint += 1
                new_records += 1
                if written_since_checkpoint >= checkpoint_every:
                    for open_handle in handles.values():
                        open_handle.flush()
                    self.save_checkpoint()
                    written_since_checkpoint = 0
                if fail_after is not None and new_records >= fail_after:
                    for open_handle in handles.values():
                        open_handle.flush()
                    self.save_checkpoint()
                    raise IngestionInterrupted(
                        f"ingestion interrupted after {new_records} records"
                    )
        finally:
            for open_handle in handles.values():
                open_handle.close()
        self.save_checkpoint()
        return self.checkpoint

    def mark_complete(self, *keys: PartitionKey) -> None:
        targets = (
            [key.relative for key in keys]
            if keys
            else list(self.checkpoint.partitions)
        )
        for name in targets:
            state = self.checkpoint.partitions.get(name)
            if state is not None:
                state.complete = True
        self.save_checkpoint()

    def read_partition(self, key: PartitionKey) -> Iterator[tuple[int, RawRecord]]:
        path = self.partition_path(key)
        if not path.is_file():
            return
        with path.open(encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if not line.strip():
                    continue
                yield index, RawRecord.from_dict(json.loads(line))

    def partitions(self) -> tuple[PartitionState, ...]:
        return tuple(
            self.checkpoint.partitions[name]
            for name in sorted(self.checkpoint.partitions)
        )

    def raw_records(self) -> Iterator[tuple[PartitionKey, int, RawRecord]]:
        for state in self.partitions():
            for index, record in self.read_partition(state.key):
                yield state.key, index, record


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    """Lineage record: source, versions, range, checksums and approval."""

    dataset_id: str
    created_at: str
    venues: tuple[str, ...]
    instruments: tuple[str, ...]
    first_receive_ns: int
    last_receive_ns: int
    raw_records: int
    canonical_events: int
    duplicates_skipped: int
    ingestion_version: str
    normalization_version: str
    adapter_version: str
    schema_version: int
    instrument_master_version: str
    partitions: tuple[dict[str, Any], ...]
    files: dict[str, dict[str, Any]]
    quality: dict[str, Any]
    approved: bool
    checksum: str = ""

    def to_dict(self) -> dict[str, Any]:
        document = {
            "dataset_id": self.dataset_id,
            "created_at": self.created_at,
            "venues": list(self.venues),
            "instruments": list(self.instruments),
            "first_receive_ns": self.first_receive_ns,
            "last_receive_ns": self.last_receive_ns,
            "raw_records": self.raw_records,
            "canonical_events": self.canonical_events,
            "duplicates_skipped": self.duplicates_skipped,
            "ingestion_version": self.ingestion_version,
            "normalization_version": self.normalization_version,
            "adapter_version": self.adapter_version,
            "schema_version": self.schema_version,
            "instrument_master_version": self.instrument_master_version,
            "partitions": list(self.partitions),
            "files": self.files,
            "quality": self.quality,
            "approved": self.approved,
        }
        document["checksum"] = self.checksum or manifest_checksum(document)
        return document

    @classmethod
    def load(cls, path: Path) -> dict[str, Any]:
        return json.loads(Path(path).read_text(encoding="utf-8"))


def manifest_checksum(document: dict[str, Any]) -> str:
    """Content identity of a dataset.

    Operational counters describe *how* the dataset was produced rather than
    *what* it contains, so a dataset rebuilt after an interrupted ingestion
    must checksum identically to one captured in a single pass.
    """

    payload = {key: value for key, value in document.items() if key != "checksum"}
    # created_at is wall-clock and must not change the content identity.
    payload.pop("created_at", None)
    payload.pop("duplicates_skipped", None)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class CanonicalWriter:
    """Writes canonical events into venue/date partitions with checksums."""

    def __init__(self, dataset_root: Path) -> None:
        self.root = Path(dataset_root) / "canonical"
        self.root.mkdir(parents=True, exist_ok=True)
        self._handles: dict[str, Any] = {}
        self.counts: dict[str, int] = {}

    def path_for(self, venue: str, date: str) -> Path:
        return self.root / venue / date / "events.jsonl"

    def write(self, event: CanonicalEvent) -> None:
        date = partition_date(event.receive_timestamp_ns)
        name = f"{event.venue}/{date}"
        handle = self._handles.get(name)
        if handle is None:
            path = self.path_for(event.venue, date)
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("w", encoding="utf-8", newline="\n")
            self._handles[name] = handle
        handle.write(encode_event(event).decode("utf-8") + "\n")
        self.counts[name] = self.counts.get(name, 0) + 1

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()


class Pipeline:
    """Orchestrates raw ingestion, normalization, quality grading and approval."""

    def __init__(
        self,
        dataset_root: Path,
        security_master: SecurityMaster,
        *,
        dataset_id: str | None = None,
        thresholds: QualityThresholds | None = None,
        stale_after_ns: int = 2_000_000_000,
    ) -> None:
        self.root = Path(dataset_root)
        self.security_master = security_master
        self.dataset_id = dataset_id or self.root.name
        self.ingestor = RawIngestor(self.root, self.dataset_id)
        self.thresholds = thresholds or QualityThresholds()
        self.stale_after_ns = stale_after_ns

    # ------------------------------------------------------------- ingestion
    def ingest(
        self,
        records: Iterable[RawRecord],
        *,
        checkpoint_every: int = 100,
        fail_after: int | None = None,
    ) -> IngestionCheckpoint:
        return self.ingestor.ingest(
            records, checkpoint_every=checkpoint_every, fail_after=fail_after
        )

    def backfill(self, records: Iterable[RawRecord]) -> IngestionCheckpoint:
        """Backfill is just ingestion: duplicate digests are skipped."""

        return self.ingestor.ingest(records)

    # ---------------------------------------------------------- normalization
    def normalize(self) -> tuple[QualityReport, int]:
        writer = CanonicalWriter(self.root)
        monitor = QualityMonitor(
            stale_after_ns=self.stale_after_ns, thresholds=self.thresholds
        )
        normalizers = {
            venue: build_normalizer(venue, self.security_master)
            for venue in self._venues()
        }
        events: list[CanonicalEvent] = []
        quarantine: list[dict[str, Any]] = []
        for key, index, record in self.ingestor.raw_records():
            normalizer = normalizers.get(record.venue)
            if normalizer is None:
                normalizer = build_normalizer(record.venue, self.security_master)
                normalizers[record.venue] = normalizer
            reference = RawReference(
                dataset_id=self.dataset_id,
                partition=key.relative,
                record_index=index,
                sha256=record_digest(record),
            )
            try:
                events.extend(normalizer.normalize(record, reference))
            except NORMALIZATION_ERRORS as error:
                # A frame that cannot be mapped without loss is kept, byte for
                # byte, in quarantine and graded as a schema exception; it is
                # never silently dropped and never crashes the whole dataset.
                reason = f"{type(error).__name__}: {error}"
                quarantine.append(
                    {
                        "partition": key.relative,
                        "record_index": index,
                        "raw_sha256": reference.sha256,
                        "venue": record.venue,
                        "channel": record.channel,
                        "receive_timestamp_ns": record.receive_timestamp_ns,
                        "error": reason,
                    }
                )
                events.append(
                    monitor.record_schema_exception(
                        venue=record.venue,
                        channel=record.channel,
                        receive_timestamp_ns=record.receive_timestamp_ns,
                        reason=reason,
                        record_digest=reference.sha256,
                    )
                )
        self._write_quarantine(quarantine)
        events.sort(key=lambda item: item.ordering_key)
        written = 0
        try:
            for event in events:
                for graded in monitor.process(event):
                    writer.write(graded)
                    written += 1
        finally:
            writer.close()
        return monitor.report(), written

    def _write_quarantine(self, rows: list[dict[str, Any]]) -> None:
        path = self.root / QUARANTINE_PATH
        if not rows:
            if path.exists():
                path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(
            rows, key=lambda row: (row["receive_timestamp_ns"], row["raw_sha256"])
        )
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in ordered),
            encoding="utf-8",
        )

    def _venues(self) -> tuple[str, ...]:
        return tuple(
            sorted({state.key.venue for state in self.ingestor.partitions()})
        )

    # --------------------------------------------------------------- manifest
    def write_manifest(self, report: QualityReport, canonical_events: int) -> Path:
        files: dict[str, dict[str, Any]] = {}
        for path in sorted(self.root.rglob("*.jsonl")):
            relative = path.relative_to(self.root).as_posix()
            files[relative] = {
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        partitions = tuple(state.to_dict() for state in self.ingestor.partitions())
        instruments = sorted(
            {
                stream.split(":", 1)[1]
                for stream in report.per_stream
                if ":" in stream
            }
        )
        manifest = DatasetManifest(
            dataset_id=self.dataset_id,
            created_at=datetime.now(tz=timezone.utc).isoformat(),
            venues=self._venues(),
            instruments=tuple(instruments),
            first_receive_ns=min(
                (
                    state.first_receive_ns
                    for state in self.ingestor.partitions()
                    if state.records
                ),
                default=0,
            ),
            last_receive_ns=self.ingestor.checkpoint.last_receive_ns,
            raw_records=self.ingestor.checkpoint.records_written,
            canonical_events=canonical_events,
            duplicates_skipped=self.ingestor.checkpoint.duplicates_skipped,
            ingestion_version=INGESTION_VERSION,
            normalization_version=NORMALIZATION_VERSION,
            adapter_version=ADAPTER_VERSION,
            schema_version=SCHEMA_VERSION,
            instrument_master_version=instrument_master_version(
                self.security_master
            ),
            partitions=partitions,
            files=files,
            quality=report.to_dict(),
            approved=report.approved,
        )
        document = manifest.to_dict()
        path = self.root / "dataset_manifest.json"
        path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return path

    def run(self) -> dict[str, Any]:
        """Normalize everything captured so far and publish the manifest."""

        report, written = self.normalize()
        self.ingestor.mark_complete()
        manifest_path = self.write_manifest(report, written)
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- reading
    def canonical_events(
        self, venues: Sequence[str] | None = None
    ) -> Iterator[CanonicalEvent]:
        root = self.root / "canonical"
        if not root.is_dir():
            return
        for path in sorted(root.rglob("events.jsonl")):
            venue = path.relative_to(root).parts[0]
            if venues is not None and venue not in venues:
                continue
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        yield decode_event(line)

    def venue_streams(self) -> dict[str, list[CanonicalEvent]]:
        streams: dict[str, list[CanonicalEvent]] = {}
        for event in self.canonical_events():
            streams.setdefault(event.venue, []).append(event)
        for events in streams.values():
            events.sort(key=lambda item: item.ordering_key)
        return streams


def instrument_master_version(security_master: SecurityMaster) -> str:
    document = json.dumps(security_master.to_documents(), sort_keys=True)
    return hashlib.sha256(document.encode("utf-8")).hexdigest()[:16]


def load_manifest(dataset_root: Path) -> dict[str, Any]:
    path = Path(dataset_root) / "dataset_manifest.json"
    if not path.is_file():
        raise FileNotFoundError(f"dataset manifest is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_manifest(dataset_root: Path) -> dict[str, Any]:
    """Re-hash every file and confirm the manifest checksum still holds."""

    root = Path(dataset_root)
    manifest = load_manifest(root)
    mismatches: list[str] = []
    for relative, metadata in manifest.get("files", {}).items():
        path = root / relative
        if not path.is_file():
            mismatches.append(f"missing:{relative}")
            continue
        if sha256_file(path) != metadata["sha256"]:
            mismatches.append(f"checksum:{relative}")
    recomputed = manifest_checksum(manifest)
    if recomputed != manifest.get("checksum"):
        mismatches.append("manifest_checksum")
    return {
        "dataset_id": manifest["dataset_id"],
        "verified": not mismatches,
        "mismatches": mismatches,
        "checksum": manifest.get("checksum"),
        "recomputed_checksum": recomputed,
        "approved": manifest.get("approved", False),
    }


def list_datasets(root: Path) -> list[dict[str, Any]]:
    root = Path(root)
    if not root.is_dir():
        return []
    datasets = []
    for directory in sorted(root.iterdir()):
        manifest_path = directory / "dataset_manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        datasets.append(
            {
                "dataset_id": manifest["dataset_id"],
                "path": str(directory),
                "venues": manifest.get("venues", []),
                "instruments": manifest.get("instruments", []),
                "canonical_events": manifest.get("canonical_events", 0),
                "approved": manifest.get("approved", False),
                "checksum": manifest.get("checksum", ""),
            }
        )
    return datasets
