"""Run manifest and reproducibility record (MR-13, section 9).

A run is only reproducible if you can say exactly what produced it. The
manifest pins the code, the configuration, the data, every component version
and the seeds, and finishes with a hash of the results so two runs can be
compared by identity rather than by eyeball.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from trading_platform_poc.minreq import DOCUMENT_VERSION, LAYER_VERSION

MANIFEST_VERSION = "1.0.0"

#: Set by the container build so a report produced outside a git checkout can
#: still name the commit it came from. A run that cannot identify its own code
#: is not reproducible evidence.
REVISION_ENV_VAR = "HFT_PLATFORM_KIT_GIT_REVISION"

#: The installed distribution name. The import package and the distribution do
#: not share a name, so the lookup has to use the distribution.
DISTRIBUTION_NAMES = ("hft-platform-kit", "trading-platform-poc")

#: Fields that record when something happened rather than what happened. They
#: are kept in the document for provenance but never contribute to a hash,
#: otherwise two identical runs could never compare equal.
VOLATILE_KEYS = frozenset(
    {"created_at", "generated_at", "wall_clock_ns", "started_ns", "finished_ns"}
)


def strip_volatile(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: strip_volatile(item)
            for key, item in value.items()
            if key not in VOLATILE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [strip_volatile(item) for item in value]
    return value


def _git_revision(repository: Path | None = None) -> str:
    stamped = os.environ.get(REVISION_ENV_VAR, "").strip()
    if stamped:
        return stamped
    root = repository or Path(__file__).resolve().parents[3]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    revision = result.stdout.strip()
    return revision or "unavailable"


def _package_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    for name in DISTRIBUTION_NAMES:
        try:
            return version(name)
        except PackageNotFoundError:
            continue
        except Exception:
            break
    return "0.0.0+local"


def build_identity(repository: Path | None = None) -> dict[str, str]:
    """Everything needed to rebuild the exact binary that produced a run."""

    return {
        "package_version": _package_version(),
        "git_revision": _git_revision(repository),
        "python_version": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
    }


def hash_document(document: Any) -> str:
    payload = json.dumps(document, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class RunManifest:
    run_id: str
    mode: str
    started_ns: int
    seed: int
    build: dict[str, str]
    config: dict[str, Any]
    component_versions: dict[str, str]
    dataset: dict[str, Any]
    inputs: dict[str, Any] = field(default_factory=dict)
    results: dict[str, Any] = field(default_factory=dict)
    finished_ns: int = 0
    manifest_version: str = MANIFEST_VERSION
    document_version: str = DOCUMENT_VERSION

    @property
    def config_hash(self) -> str:
        return hash_document(strip_volatile(self.config))

    @property
    def input_hash(self) -> str:
        return hash_document(
            strip_volatile({"dataset": self.dataset, "inputs": self.inputs})
        )

    @property
    def result_hash(self) -> str:
        return hash_document(strip_volatile(self.results))

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "document_version": self.document_version,
            "layer_version": LAYER_VERSION,
            "run_id": self.run_id,
            "mode": self.mode,
            "started_ns": self.started_ns,
            "finished_ns": self.finished_ns,
            "seed": self.seed,
            "build": dict(sorted(self.build.items())),
            "component_versions": dict(sorted(self.component_versions.items())),
            "config": self.config,
            "config_hash": self.config_hash,
            "dataset": self.dataset,
            "inputs": self.inputs,
            "input_hash": self.input_hash,
            "results": self.results,
            "result_hash": self.result_hash,
        }

    def identity(self) -> str:
        """Hash of everything except wall-clock timings."""

        document = strip_volatile(self.to_dict())
        # The dataset and result documents are represented by their hashes so
        # that provenance timestamps inside them cannot leak into identity.
        document["dataset"] = document["input_hash"]
        document["inputs"] = {}
        document["results"] = document["result_hash"]
        document["build"] = {
            key: value
            for key, value in document["build"].items()
            if key in {"package_version", "git_revision"}
        }
        return hash_document(document)

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        return target

    @classmethod
    def load(cls, path: str | Path) -> dict[str, Any]:
        return json.loads(Path(path).read_text(encoding="utf-8"))


def compare_manifests(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> dict[str, Any]:
    """Explain why two runs differ, in the order a reviewer would ask."""

    differences: list[dict[str, str]] = []
    for key in (
        "config_hash",
        "input_hash",
        "result_hash",
        "seed",
        "component_versions",
        "build",
    ):
        left_value = left.get(key)
        right_value = right.get(key)
        if left_value != right_value:
            differences.append(
                {
                    "field": key,
                    "left": json.dumps(left_value, sort_keys=True, default=str),
                    "right": json.dumps(right_value, sort_keys=True, default=str),
                }
            )
    return {
        "identical": not differences,
        "differences": differences,
    }
