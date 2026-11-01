from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

from .models import MarketEvent, json_default
from .pipeline import build_envelope_normalizer
from .quality import QualityGate, QualitySnapshot, QualityThresholds
from .monitoring import MonitoringThresholds, PipelineMonitor


class RetryablePipelineError(RuntimeError):
    """A transient processing failure whose source record must be retried."""


@dataclass(frozen=True, slots=True)
class PartitionSpec:
    partition_id: str
    source_path: Path
    output_path: Path


class PipelineStateStore:
    """Durable orchestration state and canonical commit log."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                created_ts_ns INTEGER NOT NULL,
                updated_ts_ns INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS partitions (
                job_id TEXT NOT NULL,
                partition_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                output_path TEXT NOT NULL,
                status TEXT NOT NULL,
                checkpoint_offset INTEGER NOT NULL DEFAULT 0,
                checkpoint_line INTEGER NOT NULL DEFAULT 0,
                source_size INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                committed_records INTEGER NOT NULL DEFAULT 0,
                rejected_records INTEGER NOT NULL DEFAULT 0,
                late_records INTEGER NOT NULL DEFAULT 0,
                last_receive_ts_ns INTEGER,
                last_error TEXT,
                updated_ts_ns INTEGER NOT NULL,
                PRIMARY KEY (job_id, partition_id),
                FOREIGN KEY (job_id) REFERENCES jobs(job_id)
            );
            CREATE TABLE IF NOT EXISTS records (
                job_id TEXT NOT NULL,
                partition_id TEXT NOT NULL,
                record_key TEXT NOT NULL,
                line_number INTEGER NOT NULL,
                receive_ts_ns INTEGER,
                event_json TEXT NOT NULL,
                committed_ts_ns INTEGER NOT NULL,
                PRIMARY KEY (job_id, partition_id, record_key)
            );
            CREATE TABLE IF NOT EXISTS rejections (
                job_id TEXT NOT NULL,
                partition_id TEXT NOT NULL,
                record_key TEXT NOT NULL,
                line_number INTEGER NOT NULL,
                error TEXT NOT NULL,
                raw_line TEXT NOT NULL,
                rejected_ts_ns INTEGER NOT NULL,
                PRIMARY KEY (job_id, partition_id, record_key)
            );
        """)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "PipelineStateStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class PipelineOrchestrator:
    def __init__(
        self,
        state_path: Path,
        normalizer: Callable[[dict[str, Any]], MarketEvent],
        *,
        clock_ns: Callable[[], int] = time.time_ns,
        quality_thresholds: QualityThresholds | None = None,
        monitoring_thresholds: MonitoringThresholds | None = None,
        instrument_version: dict[str, object] | None = None,
    ):
        self.state = PipelineStateStore(state_path)
        self.normalizer = normalizer
        self.clock_ns = clock_ns
        self.quality_thresholds = quality_thresholds or QualityThresholds()
        self.monitoring_thresholds = monitoring_thresholds or MonitoringThresholds()
        self.instrument_version = instrument_version or {
            "schema_version": "UNKNOWN", "instrument_count": 0,
            "content_sha256": "UNKNOWN",
        }

    @classmethod
    def for_canonical_pipeline(
        cls, state_path: Path, config_path: Path
    ) -> "PipelineOrchestrator":
        from .config import load_config

        cfg, _ = load_config(config_path)
        return cls(
            state_path,
            build_envelope_normalizer(config_path),
            quality_thresholds=cfg.quality_thresholds,
            monitoring_thresholds=cfg.monitoring_thresholds,
            instrument_version=cfg.instruments.version_snapshot(),
        )

    def close(self) -> None:
        self.state.close()

    def __enter__(self) -> "PipelineOrchestrator":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def register_job(self, job_id: str, *, mode: str = "CONTINUOUS") -> None:
        now = self.clock_ns()
        with self.state.db:
            self.state.db.execute(
                "INSERT OR IGNORE INTO jobs(job_id, mode, status, created_ts_ns, updated_ts_ns) VALUES(?, ?, 'RUNNING', ?, ?)",
                (job_id, mode, now, now),
            )
        stored_mode = self.state.db.execute(
            "SELECT mode FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()[0]
        if stored_mode != mode:
            raise ValueError(f"job {job_id} is already registered as {stored_mode}")

    def _register_partition(
        self, job_id: str, spec: PartitionSpec, job_mode: str
    ) -> sqlite3.Row:
        job = self.state.db.execute(
            "SELECT job_id FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if job is None:
            self.register_job(job_id, mode=job_mode)
        else:
            self.register_job(job_id, mode=job_mode)
        now = self.clock_ns()
        with self.state.db:
            self.state.db.execute(
                """INSERT OR IGNORE INTO partitions(
                    job_id, partition_id, source_path, output_path, status, updated_ts_ns
                ) VALUES(?, ?, ?, ?, 'PENDING', ?)""",
                (job_id, spec.partition_id, str(spec.source_path), str(spec.output_path), now),
            )
        row = self.state.db.execute(
            "SELECT * FROM partitions WHERE job_id=? AND partition_id=?",
            (job_id, spec.partition_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("partition registration failed")
        if (row["source_path"], row["output_path"]) != (
            str(spec.source_path), str(spec.output_path)
        ):
            raise ValueError("partition identity cannot be reused for different paths")
        return row

    @staticmethod
    def _record_key(envelope: dict[str, Any] | None, raw_line: bytes) -> str:
        if envelope and envelope.get("raw_ref"):
            return str(envelope["raw_ref"])
        return sha256(raw_line).hexdigest()

    def run_partition(
        self,
        job_id: str,
        spec: PartitionSpec,
        *,
        max_records: int | None = None,
        complete_on_eof: bool = True,
        job_mode: str = "BATCH",
    ) -> dict[str, Any]:
        if max_records is not None and max_records <= 0:
            raise ValueError("max_records must be positive")
        row = self._register_partition(job_id, spec, job_mode)
        source_size = spec.source_path.stat().st_size
        checkpoint = int(row["checkpoint_offset"])
        if source_size < checkpoint:
            self._set_partition_status(job_id, spec.partition_id, "FAILED", "source file truncated below checkpoint")
            self._update_job_status(job_id)
            raise ValueError("source file truncated below durable checkpoint")
        late_mode = row["status"] in ("COMPLETE", "COMPLETE_WITH_REJECTIONS") and source_size > checkpoint
        status = "LATE_REOPENED" if late_mode else "RUNNING"
        now = self.clock_ns()
        with self.state.db:
            self.state.db.execute(
                """UPDATE partitions SET status=?, attempts=attempts+1,
                    source_size=?, last_error=NULL, updated_ts_ns=?
                    WHERE job_id=? AND partition_id=?""",
                (status, source_size, now, job_id, spec.partition_id),
            )
            self.state.db.execute(
                "UPDATE jobs SET status='RUNNING', updated_ts_ns=? WHERE job_id=?",
                (now, job_id),
            )
        processed = 0
        with spec.source_path.open("rb") as source:
            source.seek(checkpoint)
            line_number = int(row["checkpoint_line"])
            while True:
                raw_line = source.readline()
                if not raw_line:
                    break
                next_offset = source.tell()
                line_number += 1
                if not raw_line.strip():
                    self._advance_checkpoint(job_id, spec.partition_id, next_offset, line_number, None)
                    continue
                envelope = None
                try:
                    envelope = json.loads(raw_line)
                    event = self.normalizer(envelope)
                    event_json = json.dumps(asdict(event), default=json_default, sort_keys=True)
                    record_key = self._record_key(envelope, raw_line)
                    receive_ts_ns = int(envelope.get("receive_ts_ns", event.receive_ts_ns))
                    inserted = self._commit_record(
                        job_id, spec.partition_id, record_key, line_number,
                        next_offset, receive_ts_ns, event_json, late_mode,
                    )
                    processed += int(inserted)
                except RetryablePipelineError as exc:
                    self._set_partition_status(job_id, spec.partition_id, "RETRY_WAIT", str(exc))
                    self._materialize(job_id, spec)
                    report = self.partition_report(job_id, spec.partition_id)
                    self._write_partition_report(spec, report)
                    self._write_quality_artifacts(job_id, spec, report)
                    raise
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    record_key = self._record_key(envelope, raw_line)
                    self._commit_rejection(
                        job_id, spec.partition_id, record_key, line_number,
                        next_offset, raw_line, str(exc), late_mode,
                    )
                if max_records is not None and processed >= max_records:
                    self._set_partition_status(job_id, spec.partition_id, "PAUSED", None)
                    self._materialize(job_id, spec)
                    report = self.partition_report(job_id, spec.partition_id)
                    self._write_partition_report(spec, report)
                    self._write_quality_artifacts(job_id, spec, report)
                    return report

        rejection_count = self.state.db.execute(
            "SELECT rejected_records FROM partitions WHERE job_id=? AND partition_id=?",
            (job_id, spec.partition_id),
        ).fetchone()[0]
        final_state = "COMPLETE" if complete_on_eof else "CAUGHT_UP"
        if complete_on_eof and rejection_count:
            final_state = "COMPLETE_WITH_REJECTIONS"
        self._set_partition_status(job_id, spec.partition_id, final_state, None)
        self._materialize(job_id, spec)
        self._update_job_status(job_id)
        report = self.partition_report(job_id, spec.partition_id)
        self._write_partition_report(spec, report)
        self._write_quality_artifacts(job_id, spec, report)
        return report

    def run_with_retry(
        self,
        job_id: str,
        spec: PartitionSpec,
        *,
        max_retries: int = 3,
        job_mode: str = "BATCH",
    ) -> dict[str, Any]:
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        failures = 0
        while True:
            try:
                return self.run_partition(job_id, spec, job_mode=job_mode)
            except RetryablePipelineError:
                failures += 1
                if failures > max_retries:
                    self._set_partition_status(job_id, spec.partition_id, "FAILED", "retry budget exhausted")
                    self._update_job_status(job_id)
                    raise

    def run_backfill(
        self, job_id: str, partitions: Iterable[PartitionSpec], *, max_retries: int = 3
    ) -> dict[str, Any]:
        self.register_job(job_id, mode="BACKFILL")
        for spec in sorted(partitions, key=lambda item: item.partition_id):
            self.run_with_retry(
                job_id, spec, max_retries=max_retries, job_mode="BACKFILL"
            )
        self._update_job_status(job_id)
        return self.job_report(job_id)

    def poll_once(self, job_id: str, spec: PartitionSpec) -> dict[str, Any]:
        return self.run_partition(
            job_id, spec, complete_on_eof=False, job_mode="CONTINUOUS"
        )

    def run_continuous(
        self,
        job_id: str,
        spec: PartitionSpec,
        *,
        poll_interval_seconds: float = 1.0,
        max_polls: int | None = None,
    ) -> dict[str, Any]:
        if max_polls is not None and max_polls <= 0:
            raise ValueError("max_polls must be positive")
        polls = 0
        report: dict[str, Any] = {}
        while max_polls is None or polls < max_polls:
            report = self.poll_once(job_id, spec)
            polls += 1
            if max_polls is None or polls < max_polls:
                time.sleep(max(0.0, poll_interval_seconds))
        return {**report, "polls": polls}

    def _commit_record(
        self,
        job_id: str,
        partition_id: str,
        record_key: str,
        line_number: int,
        next_offset: int,
        receive_ts_ns: int,
        event_json: str,
        late_mode: bool,
    ) -> bool:
        now = self.clock_ns()
        with self.state.db:
            cursor = self.state.db.execute(
                """INSERT OR IGNORE INTO records(
                    job_id, partition_id, record_key, line_number, receive_ts_ns,
                    event_json, committed_ts_ns
                ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (job_id, partition_id, record_key, line_number, receive_ts_ns, event_json, now),
            )
            inserted = cursor.rowcount == 1
            self.state.db.execute(
                """UPDATE partitions SET checkpoint_offset=?, checkpoint_line=?,
                    committed_records=committed_records+?, late_records=late_records+?,
                    last_receive_ts_ns=MAX(COALESCE(last_receive_ts_ns, ?), ?), updated_ts_ns=?
                    WHERE job_id=? AND partition_id=?""",
                (
                    next_offset, line_number, int(inserted), int(inserted and late_mode),
                    receive_ts_ns, receive_ts_ns, now, job_id, partition_id,
                ),
            )
        return inserted

    def _commit_rejection(
        self,
        job_id: str,
        partition_id: str,
        record_key: str,
        line_number: int,
        next_offset: int,
        raw_line: bytes,
        error: str,
        late_mode: bool,
    ) -> None:
        now = self.clock_ns()
        with self.state.db:
            cursor = self.state.db.execute(
                """INSERT OR IGNORE INTO rejections(
                    job_id, partition_id, record_key, line_number, error, raw_line, rejected_ts_ns
                ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (job_id, partition_id, record_key, line_number, error, raw_line.decode("utf-8", "replace").rstrip("\n"), now),
            )
            inserted = cursor.rowcount == 1
            self.state.db.execute(
                """UPDATE partitions SET checkpoint_offset=?, checkpoint_line=?,
                    rejected_records=rejected_records+?, late_records=late_records+?, updated_ts_ns=?
                    WHERE job_id=? AND partition_id=?""",
                (next_offset, line_number, int(inserted), int(inserted and late_mode), now, job_id, partition_id),
            )

    def _advance_checkpoint(
        self, job_id: str, partition_id: str, offset: int, line: int,
        last_receive_ts_ns: int | None,
    ) -> None:
        with self.state.db:
            self.state.db.execute(
                """UPDATE partitions SET checkpoint_offset=?, checkpoint_line=?,
                    last_receive_ts_ns=COALESCE(?, last_receive_ts_ns), updated_ts_ns=?
                    WHERE job_id=? AND partition_id=?""",
                (offset, line, last_receive_ts_ns, self.clock_ns(), job_id, partition_id),
            )

    def _set_partition_status(
        self, job_id: str, partition_id: str, status: str, error: str | None
    ) -> None:
        with self.state.db:
            self.state.db.execute(
                "UPDATE partitions SET status=?, last_error=?, updated_ts_ns=? WHERE job_id=? AND partition_id=?",
                (status, error, self.clock_ns(), job_id, partition_id),
            )

    def _update_job_status(self, job_id: str) -> None:
        states = [
            row[0] for row in self.state.db.execute(
                "SELECT status FROM partitions WHERE job_id=?", (job_id,)
            )
        ]
        complete_states = {"COMPLETE", "COMPLETE_WITH_REJECTIONS"}
        status = "COMPLETE" if states and all(x in complete_states for x in states) else "RUNNING"
        if any(x == "COMPLETE_WITH_REJECTIONS" for x in states):
            status = "COMPLETE_WITH_REJECTIONS"
        if any(x == "FAILED" for x in states):
            status = "FAILED"
        with self.state.db:
            self.state.db.execute(
                "UPDATE jobs SET status=?, updated_ts_ns=? WHERE job_id=?",
                (status, self.clock_ns(), job_id),
            )

    def _materialize(self, job_id: str, spec: PartitionSpec) -> None:
        spec.output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = spec.output_path.with_suffix(spec.output_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as sink:
            for row in self.state.db.execute(
                """SELECT event_json FROM records WHERE job_id=? AND partition_id=?
                    ORDER BY line_number, record_key""",
                (job_id, spec.partition_id),
            ):
                sink.write(row[0] + "\n")
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(temporary, spec.output_path)
        quarantine = spec.output_path.with_suffix(spec.output_path.suffix + ".quarantine.jsonl")
        temporary_q = quarantine.with_suffix(quarantine.suffix + ".tmp")
        with temporary_q.open("w", encoding="utf-8") as sink:
            for row in self.state.db.execute(
                """SELECT line_number, error, raw_line FROM rejections
                    WHERE job_id=? AND partition_id=? ORDER BY line_number, record_key""",
                (job_id, spec.partition_id),
            ):
                sink.write(json.dumps(dict(row), sort_keys=True) + "\n")
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(temporary_q, quarantine)

    @staticmethod
    def _write_partition_report(spec: PartitionSpec, report: dict[str, Any]) -> None:
        path = spec.output_path.with_suffix(spec.output_path.suffix + ".partition.json")
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)

    def _write_quality_artifacts(
        self, job_id: str, spec: PartitionSpec, report: dict[str, Any]
    ) -> None:
        job_mode = self.state.db.execute(
            "SELECT mode FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()[0]
        rejected_partition = int(report["status"] in ("COMPLETE_WITH_REJECTIONS", "FAILED"))
        snapshot = QualitySnapshot(
            events=int(report["committed_records"]),
            schema_exceptions=int(report["rejected_records"]),
            rejected_records=int(report["rejected_records"]),
            rejected_partitions=rejected_partition,
            persistence_lag_ns=(
                int(report["persistence_lag_ns"] or 0)
                if job_mode == "CONTINUOUS" else 0
            ),
            unpersisted_bytes=int(report["unpersisted_bytes"]),
        )
        gate = QualityGate(self.quality_thresholds, clock_ns=self.clock_ns)
        gate.write_artifacts(
            spec.output_path.with_suffix(spec.output_path.suffix + ".quality.json"),
            spec.output_path.with_suffix(spec.output_path.suffix + ".quality_defects.jsonl"),
            snapshot,
            scope="PIPELINE_PARTITION",
            correlation_id=f"{job_id}/{spec.partition_id}",
            limitations=(
                "Book-level gaps, stale intervals, crossed books and duplicates are evaluated after canonical replay.",
                f"Persistence-lag approval is {'enabled' if job_mode == 'CONTINUOUS' else 'not applicable'} for {job_mode} mode.",
            ),
        )
        job_report = self.job_report(job_id)
        partitions = job_report["partitions"]
        source_bytes = sum(int(row["source_size"]) for row in partitions)
        checkpoint_bytes = sum(int(row["checkpoint_offset"]) for row in partitions)
        completeness = (
            1.0 if source_bytes == 0 else checkpoint_bytes / source_bytes
        )
        persistence_lag = max(
            (int(row["persistence_lag_ns"] or 0) for row in partitions),
            default=0,
        )
        monitor = PipelineMonitor(self.monitoring_thresholds)
        monitoring_report = monitor.build(
            mode=str(job_mode), generated_ts_ns=self.clock_ns(),
            correlation_id=f"{job_id}/{spec.partition_id}",
            job={
                "job_id": job_id,
                "state": job_report["job"]["status"],
                "mode": job_mode,
            },
            data={
                "event_count": sum(int(row["committed_records"]) for row in partitions),
                "schema_exceptions": sum(int(row["rejected_records"]) for row in partitions),
                "latest_receive_ts_ns": max(
                    (int(row["last_receive_ts_ns"] or 0) for row in partitions),
                    default=0,
                ),
            },
            partitions={
                "total": len(partitions),
                "complete": sum(row["status"] in (
                    "COMPLETE", "COMPLETE_WITH_REJECTIONS", "CAUGHT_UP"
                ) for row in partitions),
                "completeness": completeness,
                "rejected": sum(row["status"] in (
                    "FAILED", "COMPLETE_WITH_REJECTIONS"
                ) for row in partitions),
                "states": {row["partition_id"]: row["status"] for row in partitions},
            },
            books={},
            versions={
                "canonical_schema_expected": "1.2.0",
                "canonical_schema_observed": ["1.2.0"],
                "schema_mismatches": 0,
                "instrument_master": self.instrument_version,
            },
            performance={"replay_events_per_second": 0},
            live={
                "reconnects": 0, "stale_disconnects": 0,
                "persistence_lag_ns": (
                    persistence_lag if job_mode == "CONTINUOUS" else 0
                ),
                "unpersisted_bytes": sum(
                    int(row["unpersisted_bytes"]) for row in partitions
                ),
            },
        )
        monitor.write(
            monitoring_report,
            spec.output_path.with_suffix(spec.output_path.suffix + ".monitoring.json"),
            spec.output_path.with_suffix(spec.output_path.suffix + ".metrics.prom"),
        )

    def partition_report(self, job_id: str, partition_id: str) -> dict[str, Any]:
        row = self.state.db.execute(
            "SELECT * FROM partitions WHERE job_id=? AND partition_id=?",
            (job_id, partition_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown partition {job_id}/{partition_id}")
        result = dict(row)
        result["unpersisted_bytes"] = max(0, int(result["source_size"]) - int(result["checkpoint_offset"]))
        last_receive = result["last_receive_ts_ns"]
        result["persistence_lag_ns"] = (
            max(0, self.clock_ns() - int(last_receive)) if last_receive is not None else None
        )
        return result

    def job_report(self, job_id: str) -> dict[str, Any]:
        job = self.state.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if job is None:
            raise KeyError(f"unknown job {job_id}")
        partitions = [
            self.partition_report(job_id, row[0])
            for row in self.state.db.execute(
                "SELECT partition_id FROM partitions WHERE job_id=? ORDER BY partition_id", (job_id,)
            )
        ]
        return {"job": dict(job), "partitions": partitions}
