import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.orchestration import (
    PartitionSpec,
    PipelineOrchestrator,
    RetryablePipelineError,
)
from crypto_stat_arb.pipeline import build_envelope_normalizer


ROOT = Path(__file__).parents[1]


def sample_lines() -> list[str]:
    return [
        line + "\n"
        for line in (ROOT / "data" / "samples" / "raw_depth.jsonl").read_text().splitlines()
        if line.strip()
    ]


class PipelineOrchestrationTests(unittest.TestCase):
    def test_interrupted_job_resumes_without_duplicate_or_omission(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "raw.jsonl"
            source.write_text("".join(sample_lines()))
            spec = PartitionSpec("2026-09-04T10", source, base / "events.jsonl")
            state = base / "pipeline.sqlite"

            with PipelineOrchestrator.for_canonical_pipeline(state, ROOT / "config" / "research.json") as pipeline:
                paused = pipeline.run_partition("job-resume", spec, max_records=1)
                self.assertEqual(paused["status"], "PAUSED")
                self.assertEqual(len(spec.output_path.read_text().splitlines()), 1)
                self.assertTrue(spec.output_path.with_suffix(".jsonl.partition.json").exists())

            with PipelineOrchestrator.for_canonical_pipeline(state, ROOT / "config" / "research.json") as pipeline:
                complete = pipeline.run_partition("job-resume", spec)
                first_bytes = spec.output_path.read_bytes()
                repeated = pipeline.run_partition("job-resume", spec)
            rows = [json.loads(line) for line in first_bytes.splitlines()]
            self.assertEqual(complete["status"], "COMPLETE")
            self.assertEqual(complete["committed_records"], 2)
            self.assertEqual(complete["checkpoint_line"], 2)
            self.assertEqual(len({row["event_id"] for row in rows}), 2)
            self.assertEqual(spec.output_path.read_bytes(), first_bytes)
            self.assertEqual(repeated["committed_records"], 2)
            monitoring = spec.output_path.with_suffix(".jsonl.monitoring.json")
            metrics = spec.output_path.with_suffix(".jsonl.metrics.prom")
            self.assertTrue(monitoring.exists())
            self.assertTrue(metrics.exists())
            self.assertEqual(json.loads(monitoring.read_text())["partitions"]["completeness"], 1.0)

    def test_completed_partition_reopens_for_late_data(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "raw.jsonl"
            source.write_text(sample_lines()[0])
            spec = PartitionSpec("2026-09-04", source, base / "events.jsonl")
            with PipelineOrchestrator.for_canonical_pipeline(base / "state.sqlite", ROOT / "config" / "research.json") as pipeline:
                pipeline.run_partition("late-job", spec)
                with source.open("a", encoding="utf-8") as sink:
                    sink.write(sample_lines()[1])
                report = pipeline.run_partition("late-job", spec)
            self.assertEqual(report["status"], "COMPLETE")
            self.assertEqual(report["committed_records"], 2)
            self.assertEqual(report["late_records"], 1)

    def test_continuous_ingestion_restarts_at_durable_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "live.jsonl"
            source.write_text(sample_lines()[0])
            spec = PartitionSpec("live", source, base / "events.jsonl")
            state = base / "state.sqlite"
            with PipelineOrchestrator.for_canonical_pipeline(state, ROOT / "config" / "research.json") as pipeline:
                first = pipeline.poll_once("continuous-job", spec)
                self.assertEqual(first["status"], "CAUGHT_UP")
            with source.open("a", encoding="utf-8") as sink:
                sink.write(sample_lines()[1])
            with PipelineOrchestrator.for_canonical_pipeline(state, ROOT / "config" / "research.json") as pipeline:
                second = pipeline.poll_once("continuous-job", spec)
            self.assertEqual(second["committed_records"], 2)
            self.assertEqual(second["unpersisted_bytes"], 0)
            self.assertEqual(len(spec.output_path.read_text().splitlines()), 2)

    def test_retry_reprocesses_uncheckpointed_record(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "raw.jsonl"
            source.write_text("".join(sample_lines()))
            normalizer = build_envelope_normalizer(ROOT / "config" / "research.json")
            calls = {"count": 0}

            def flaky(envelope):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise RetryablePipelineError("temporary storage outage")
                return normalizer(envelope)

            spec = PartitionSpec("retry", source, base / "events.jsonl")
            with PipelineOrchestrator(base / "state.sqlite", flaky) as pipeline:
                report = pipeline.run_with_retry("retry-job", spec, max_retries=1)
            self.assertEqual(report["committed_records"], 2)
            self.assertEqual(report["attempts"], 2)

    def test_backfill_tracks_partition_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            partitions = []
            for index, line in enumerate(sample_lines()):
                source = base / f"raw-{index}.jsonl"
                source.write_text(line)
                partitions.append(PartitionSpec(f"2026-09-0{index + 1}", source, base / f"events-{index}.jsonl"))
            with PipelineOrchestrator.for_canonical_pipeline(base / "state.sqlite", ROOT / "config" / "research.json") as pipeline:
                report = pipeline.run_backfill("backfill-job", reversed(partitions))
            self.assertEqual(report["job"]["status"], "COMPLETE")
            self.assertEqual([row["status"] for row in report["partitions"]], ["COMPLETE", "COMPLETE"])

    def test_permanent_error_completes_with_rejections(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "raw.jsonl"
            source.write_text(sample_lines()[0] + "{not-json}\n")
            spec = PartitionSpec("quality", source, base / "events.jsonl")
            with PipelineOrchestrator.for_canonical_pipeline(base / "state.sqlite", ROOT / "config" / "research.json") as pipeline:
                report = pipeline.run_partition("quality-job", spec)
            self.assertEqual(report["status"], "COMPLETE_WITH_REJECTIONS")
            self.assertEqual(report["rejected_records"], 1)
            self.assertEqual(len(spec.output_path.with_suffix(".jsonl.quarantine.jsonl").read_text().splitlines()), 1)
            quality = json.loads(spec.output_path.with_suffix(".jsonl.quality.json").read_text())
            self.assertEqual(quality["decision"], "REJECTED")


if __name__ == "__main__":
    unittest.main()
