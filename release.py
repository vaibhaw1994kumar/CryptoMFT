from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import time
from typing import Any, Callable

from .models import canonical_json, stable_id


PAPER_RELEASE_SCHEMA_VERSION = "1.0.0"


class ArtifactKind(StrEnum):
    STRATEGY_CODE = "STRATEGY_CODE"
    FEATURE_DEFINITION = "FEATURE_DEFINITION"
    MODEL = "MODEL"
    PARAMETERS = "PARAMETERS"
    DATA_MANIFEST = "DATA_MANIFEST"
    SIMULATOR_CONFIG = "SIMULATOR_CONFIG"


class DeploymentAction(StrEnum):
    DEPLOY = "DEPLOY"
    ROLLBACK = "ROLLBACK"


@dataclass(frozen=True, slots=True)
class ArtifactInput:
    kind: ArtifactKind
    name: str
    version: str
    path: Path


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    kind: ArtifactKind
    name: str
    version: str
    source_path: str
    snapshot_path: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["kind"] = self.kind.value
        return row


@dataclass(frozen=True, slots=True)
class PaperReleaseSpec:
    strategy_id: str
    build_version: str
    environment: str
    artifacts: tuple[ArtifactInput, ...]

    def validate(self) -> None:
        if not self.strategy_id or not self.build_version or not self.environment:
            raise ValueError("strategy_id, build_version and environment are required")
        counts = {kind: 0 for kind in ArtifactKind}
        identities: set[tuple[ArtifactKind, str]] = set()
        for item in self.artifacts:
            if not item.name or not item.version:
                raise ValueError("every release artifact requires name and version")
            if not item.path.is_file():
                raise ValueError(f"release artifact does not exist: {item.path}")
            identity = (item.kind, item.name)
            if identity in identities:
                raise ValueError(f"duplicate release artifact: {item.kind}:{item.name}")
            identities.add(identity)
            counts[item.kind] += 1
        missing = [kind.value for kind, count in counts.items() if count == 0]
        if missing:
            raise ValueError(f"missing versioned release artifacts: {missing}")


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


class PaperReleaseRegistry:
    """Immutable paper-release snapshots with approval, deploy and rollback audit."""

    def __init__(self, root: Path, *, clock_ns: Callable[[], int] = time.time_ns):
        self.root = root
        self.releases_dir = root / "releases"
        self.active_path = root / "active_release.json"
        self.audit_path = root / "deployment_audit.jsonl"
        self.clock_ns = clock_ns

    @staticmethod
    def _timestamp(ts_ns: int) -> str:
        return datetime.fromtimestamp(ts_ns / 1_000_000_000, timezone.utc).isoformat()

    def _release_dir(self, release_id: str) -> Path:
        return self.releases_dir / release_id

    def _manifest_path(self, release_id: str) -> Path:
        return self._release_dir(release_id) / "release_manifest.json"

    def _approval_path(self, release_id: str) -> Path:
        return self._release_dir(release_id) / "approval.json"

    def create(self, spec: PaperReleaseSpec) -> dict[str, Any]:
        spec.validate()
        captured = []
        for ordinal, item in enumerate(sorted(
            spec.artifacts, key=lambda row: (row.kind.value, row.name)
        )):
            captured.append({
                "kind": item.kind.value,
                "name": item.name,
                "version": item.version,
                "source_path": str(item.path),
                "sha256": _file_sha256(item.path),
                "size_bytes": item.path.stat().st_size,
                "ordinal": ordinal,
            })
        identity = {
            "strategy_id": spec.strategy_id,
            "build_version": spec.build_version,
            "environment": spec.environment,
            "artifacts": [
                {
                    key: row[key] for key in (
                        "kind", "name", "version", "sha256", "size_bytes", "ordinal"
                    )
                }
                for row in captured
            ],
        }
        release_id = stable_id("paper-release", canonical_json(identity))
        release_dir = self._release_dir(release_id)
        manifest_path = self._manifest_path(release_id)
        if manifest_path.exists():
            existing = self.load(release_id)
            if existing["identity_sha256"] != sha256(
                canonical_json(identity).encode()
            ).hexdigest():
                raise ValueError("release ID collision with different content")
            return existing

        artifacts = []
        for row, item in zip(captured, sorted(
            spec.artifacts, key=lambda value: (value.kind.value, value.name)
        )):
            destination = (
                release_dir / "artifacts" / item.kind.value.lower()
                / f'{row["ordinal"]:02d}-{item.path.name}'
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item.path, destination)
            if _file_sha256(destination) != row["sha256"]:
                raise OSError(f"release snapshot verification failed: {item.name}")
            artifacts.append(ReleaseArtifact(
                kind=item.kind, name=item.name, version=item.version,
                source_path=str(item.path),
                snapshot_path=str(destination.relative_to(release_dir)),
                sha256=row["sha256"], size_bytes=row["size_bytes"],
            ).to_dict())

        created_ts_ns = self.clock_ns()
        manifest = {
            "schema_version": PAPER_RELEASE_SCHEMA_VERSION,
            "release_id": release_id,
            "created_ts_ns": created_ts_ns,
            "created_ts_utc": self._timestamp(created_ts_ns),
            "strategy_id": spec.strategy_id,
            "build": {"version": spec.build_version},
            "environment": {
                "name": spec.environment,
                "python": platform.python_version(),
                "implementation": platform.python_implementation(),
                "platform": platform.platform(),
                "executable": Path(sys.executable).name,
            },
            "artifacts": artifacts,
            "identity_sha256": sha256(canonical_json(identity).encode()).hexdigest(),
            "approval_required": True,
        }
        manifest["manifest_sha256"] = sha256(canonical_json(manifest).encode()).hexdigest()
        _atomic_json(manifest_path, manifest)
        return manifest

    def load(self, release_id: str) -> dict[str, Any]:
        path = self._manifest_path(release_id)
        if not path.is_file():
            raise ValueError(f"unknown paper release: {release_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def verify(self, release_id: str) -> dict[str, Any]:
        manifest = self.load(release_id)
        check = dict(manifest)
        expected_manifest_hash = check.pop("manifest_sha256")
        issues = []
        if sha256(canonical_json(check).encode()).hexdigest() != expected_manifest_hash:
            issues.append("MANIFEST_HASH_MISMATCH")
        release_dir = self._release_dir(release_id)
        for artifact in manifest["artifacts"]:
            snapshot = release_dir / artifact["snapshot_path"]
            if not snapshot.is_file():
                issues.append(f'MISSING_ARTIFACT:{artifact["name"]}')
            elif _file_sha256(snapshot) != artifact["sha256"]:
                issues.append(f'ARTIFACT_HASH_MISMATCH:{artifact["name"]}')
        return {
            "schema_version": PAPER_RELEASE_SCHEMA_VERSION,
            "release_id": release_id,
            "verified": not issues,
            "issues": issues,
            "artifact_count": len(manifest["artifacts"]),
            "manifest_sha256": expected_manifest_hash,
        }

    def approve(self, release_id: str, owner: str) -> dict[str, Any]:
        if not owner.strip():
            raise ValueError("approval owner is required")
        manifest = self.load(release_id)
        verification = self.verify(release_id)
        if not verification["verified"]:
            raise ValueError("cannot approve an unverifiable release")
        path = self._approval_path(release_id)
        if path.exists():
            existing = self.approval(release_id)
            if existing["owner"] != owner:
                raise ValueError("release was already approved by a different owner")
            return existing
        ts_ns = self.clock_ns()
        approval = {
            "schema_version": PAPER_RELEASE_SCHEMA_VERSION,
            "approval_id": stable_id("paper-release-approval", release_id, owner),
            "release_id": release_id,
            "owner": owner,
            "approved_ts_ns": ts_ns,
            "approved_ts_utc": self._timestamp(ts_ns),
            "manifest_sha256": manifest["manifest_sha256"],
            "decision": "APPROVED",
        }
        approval["approval_sha256"] = sha256(
            canonical_json(approval).encode()
        ).hexdigest()
        _atomic_json(path, approval)
        return approval

    def approval(self, release_id: str) -> dict[str, Any]:
        path = self._approval_path(release_id)
        if not path.is_file():
            raise ValueError(f"paper release is not approved: {release_id}")
        approval = json.loads(path.read_text(encoding="utf-8"))
        check = dict(approval)
        expected_hash = check.pop("approval_sha256", "")
        if sha256(canonical_json(check).encode()).hexdigest() != expected_hash:
            raise ValueError("paper release approval hash mismatch")
        manifest = self.load(release_id)
        if approval.get("manifest_sha256") != manifest["manifest_sha256"]:
            raise ValueError("paper release approval references another manifest")
        if approval.get("approval_id") != stable_id(
            "paper-release-approval", release_id, approval.get("owner", "")
        ):
            raise ValueError("paper release approval identity mismatch")
        return approval

    def active(self) -> dict[str, Any] | None:
        if not self.active_path.is_file():
            return None
        return json.loads(self.active_path.read_text(encoding="utf-8"))

    def _append_audit(self, event: dict[str, Any]) -> dict[str, Any]:
        previous_hash = "0" * 64
        if self.audit_path.is_file():
            lines = [line for line in self.audit_path.read_text(encoding="utf-8").splitlines() if line]
            if lines:
                previous_hash = json.loads(lines[-1])["record_hash"]
        row = {**event, "previous_hash": previous_hash}
        row["record_hash"] = sha256(canonical_json(row).encode()).hexdigest()
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return row

    def _activate(
        self, release_id: str, owner: str, action: DeploymentAction,
        reason: str = "",
    ) -> dict[str, Any]:
        if not owner.strip():
            raise ValueError("deployment owner is required")
        manifest = self.load(release_id)
        approval = self.approval(release_id)
        verification = self.verify(release_id)
        if not verification["verified"]:
            raise ValueError("cannot activate an unverifiable release")
        previous = self.active()
        if action == DeploymentAction.ROLLBACK:
            if previous is None:
                raise ValueError("cannot roll back without an active release")
            if previous["release_id"] == release_id:
                raise ValueError("rollback target is already active")
            if not reason.strip():
                raise ValueError("rollback reason is required")
        ts_ns = self.clock_ns()
        event = self._append_audit({
            "schema_version": PAPER_RELEASE_SCHEMA_VERSION,
            "deployment_id": stable_id(
                "paper-deployment", action, release_id,
                previous["release_id"] if previous else "", ts_ns,
            ),
            "action": action.value,
            "release_id": release_id,
            "from_release_id": previous["release_id"] if previous else None,
            "strategy_id": manifest["strategy_id"],
            "build_version": manifest["build"]["version"],
            "config_manifest_sha256": manifest["manifest_sha256"],
            "environment": manifest["environment"],
            "approval_id": approval["approval_id"],
            "approval_owner": approval["owner"],
            "deployment_owner": owner,
            "reason": reason,
            "ts_ns": ts_ns,
            "ts_utc": self._timestamp(ts_ns),
        })
        active = {
            "schema_version": PAPER_RELEASE_SCHEMA_VERSION,
            "release_id": release_id,
            "deployment_id": event["deployment_id"],
            "action": action.value,
            "activated_ts_ns": ts_ns,
            "manifest_sha256": manifest["manifest_sha256"],
            "build_version": manifest["build"]["version"],
            "environment": manifest["environment"]["name"],
            "approval_owner": approval["owner"],
            "deployment_owner": owner,
        }
        _atomic_json(self.active_path, active)
        return active

    def deploy(self, release_id: str, owner: str) -> dict[str, Any]:
        return self._activate(release_id, owner, DeploymentAction.DEPLOY)

    def rollback(self, release_id: str, owner: str, reason: str) -> dict[str, Any]:
        return self._activate(
            release_id, owner, DeploymentAction.ROLLBACK, reason
        )

    def reproduce(self, release_id: str, output_dir: Path) -> dict[str, Any]:
        verification = self.verify(release_id)
        if not verification["verified"]:
            raise ValueError("cannot reproduce an unverifiable release")
        if output_dir.exists() and any(output_dir.iterdir()):
            raise ValueError("reproduction output directory must be empty")
        output_dir.mkdir(parents=True, exist_ok=True)
        manifest = self.load(release_id)
        release_dir = self._release_dir(release_id)
        materialized = []
        for artifact in manifest["artifacts"]:
            source = release_dir / artifact["snapshot_path"]
            destination = output_dir / artifact["snapshot_path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if _file_sha256(destination) != artifact["sha256"]:
                raise OSError(f'reproduction copy verification failed: {artifact["name"]}')
            materialized.append({
                **artifact,
                "materialized_path": str(destination.relative_to(output_dir)),
            })
        report = {
            "schema_version": PAPER_RELEASE_SCHEMA_VERSION,
            "release_id": release_id,
            "manifest_sha256": manifest["manifest_sha256"],
            "verified": True,
            "artifacts": materialized,
        }
        _atomic_json(output_dir / "reproduction_manifest.json", report)
        return report
