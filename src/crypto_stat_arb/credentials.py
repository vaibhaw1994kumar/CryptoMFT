from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Protocol, TypeVar

from .models import canonical_json, stable_id


CREDENTIAL_SCHEMA_VERSION = "1.0.0"
T = TypeVar("T")


class CredentialEnvironment(StrEnum):
    OFFLINE_BACKTEST = "OFFLINE_BACKTEST"
    PAPER = "PAPER"
    TESTNET = "TESTNET"


class CredentialPermission(StrEnum):
    READ_MARKET = "READ_MARKET"
    READ_ACCOUNT = "READ_ACCOUNT"
    TRADE = "TRADE"
    CANCEL = "CANCEL"
    WITHDRAW = "WITHDRAW"


class CredentialState(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class SecretManager(Protocol):
    backend_name: str
    production_ready: bool

    def create(self, reference: str, version: int, secret: memoryview) -> None: ...
    def read(self, reference: str, version: int) -> bytearray: ...
    def rotate(
        self, reference: str, old_version: int, new_version: int,
        secret: memoryview,
    ) -> None: ...
    def revoke(self, reference: str, version: int) -> None: ...


class CallbackSecretManager:
    """Adapter for an external Vault/KMS/cloud secret-manager client."""

    production_ready = True

    def __init__(
        self, backend_name: str, *,
        create: Callable[[str, int, memoryview], None],
        read: Callable[[str, int], bytearray],
        rotate: Callable[[str, int, int, memoryview], None],
        revoke: Callable[[str, int], None],
    ):
        if not backend_name or backend_name.lower() in {"memory", "plaintext", "environment"}:
            raise ValueError("external secret manager requires a production backend name")
        self.backend_name = backend_name
        self._create = create
        self._read = read
        self._rotate = rotate
        self._revoke = revoke

    def create(self, reference: str, version: int, secret: memoryview) -> None:
        self._create(reference, version, secret)

    def read(self, reference: str, version: int) -> bytearray:
        return self._read(reference, version)

    def rotate(
        self, reference: str, old_version: int, new_version: int,
        secret: memoryview,
    ) -> None:
        self._rotate(reference, old_version, new_version, secret)

    def revoke(self, reference: str, version: int) -> None:
        self._revoke(reference, version)


class InMemorySecretManager:
    """Test-only secret manager; production brokers reject this backend."""

    backend_name = "in-memory-test-only"
    production_ready = False

    def __init__(self):
        self._values: dict[tuple[str, int], bytearray] = {}

    def create(self, reference: str, version: int, secret: memoryview) -> None:
        key = (reference, version)
        if key in self._values:
            raise ValueError("secret version already exists")
        self._values[key] = bytearray(secret)

    def read(self, reference: str, version: int) -> bytearray:
        try:
            return bytearray(self._values[(reference, version)])
        except KeyError as exc:
            raise ValueError("secret version is unavailable") from exc

    def rotate(
        self, reference: str, old_version: int, new_version: int,
        secret: memoryview,
    ) -> None:
        old_key = (reference, old_version)
        if old_key not in self._values:
            raise ValueError("current secret version is unavailable")
        new_key = (reference, new_version)
        if new_key in self._values:
            raise ValueError("new secret version already exists")
        self._values[new_key] = bytearray(secret)
        old = self._values.pop(old_key)
        for index in range(len(old)):
            old[index] = 0

    def revoke(self, reference: str, version: int) -> None:
        value = self._values.pop((reference, version), None)
        if value is None:
            raise ValueError("secret version is unavailable")
        for index in range(len(value)):
            value[index] = 0


@dataclass(frozen=True, slots=True)
class CredentialMetadata:
    credential_id: str
    venue: str
    environment: CredentialEnvironment
    permissions: tuple[CredentialPermission, ...]
    secret_reference: str
    version: int
    state: CredentialState
    created_ts_ns: int
    updated_ts_ns: int

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["environment"] = self.environment.value
        row["permissions"] = [item.value for item in self.permissions]
        row["state"] = self.state.value
        return row

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "CredentialMetadata":
        return cls(
            credential_id=str(row["credential_id"]), venue=str(row["venue"]),
            environment=CredentialEnvironment(row["environment"]),
            permissions=tuple(CredentialPermission(x) for x in row["permissions"]),
            secret_reference=str(row["secret_reference"]), version=int(row["version"]),
            state=CredentialState(row["state"]),
            created_ts_ns=int(row["created_ts_ns"]),
            updated_ts_ns=int(row["updated_ts_ns"]),
        )


def validate_offline_backtest_credentials(credential_ids: Iterable[str]) -> None:
    if any(str(value).strip() for value in credential_ids):
        raise ValueError("offline backtests prohibit exchange credentials")


SENSITIVE_CONFIG_KEYS = frozenset({
    "api_key", "api_secret", "secret_key", "private_key", "passphrase",
    "access_token", "refresh_token",
})


def reject_plaintext_credentials(value: Any, path: str = "config") -> None:
    """Reject secret-shaped values in JSON configuration at any nesting depth."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            location = f"{path}.{key}"
            if normalized in SENSITIVE_CONFIG_KEYS and child not in (None, ""):
                raise ValueError(
                    f"plaintext credential field is prohibited: {location}; use a credential ID"
                )
            reject_plaintext_credentials(child, location)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_plaintext_credentials(child, f"{path}[{index}]")


def _wipe(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


class CredentialBroker:
    """Metadata policy, ephemeral injection and tamper-evident access audit."""

    def __init__(
        self, manager: SecretManager, metadata_path: Path, audit_path: Path, *,
        production: bool = True, clock_ns: Callable[[], int] = time.time_ns,
    ):
        if production and not manager.production_ready:
            raise ValueError("production credential broker requires an external secret manager")
        self.manager = manager
        self.metadata_path = metadata_path
        self.audit_path = audit_path
        self.clock_ns = clock_ns
        self._metadata = self._load_metadata()

    def _load_metadata(self) -> dict[str, CredentialMetadata]:
        if not self.metadata_path.is_file():
            return {}
        payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        return {
            row["credential_id"]: CredentialMetadata.from_dict(row)
            for row in payload.get("credentials", [])
        }

    def _persist_metadata(self) -> None:
        payload = {
            "schema_version": CREDENTIAL_SCHEMA_VERSION,
            "secret_backend": self.manager.backend_name,
            "credentials": [
                self._metadata[key].to_dict() for key in sorted(self._metadata)
            ],
        }
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.metadata_path.with_suffix(self.metadata_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.metadata_path)

    def _audit(
        self, action: str, outcome: str, *, credential_id: str,
        actor: str, venue: str = "", environment: str = "",
        version: int = 0, permissions: Iterable[CredentialPermission] = (),
        reason: str = "",
    ) -> dict[str, Any]:
        previous_hash = "0" * 64
        if self.audit_path.is_file():
            lines = [line for line in self.audit_path.read_text(encoding="utf-8").splitlines() if line]
            if lines:
                previous_hash = json.loads(lines[-1])["record_hash"]
        ts_ns = self.clock_ns()
        row = {
            "schema_version": CREDENTIAL_SCHEMA_VERSION,
            "audit_id": stable_id("credential-audit", credential_id, action, ts_ns),
            "ts_ns": ts_ns,
            "action": action,
            "outcome": outcome,
            "credential_id": credential_id,
            "actor": actor,
            "venue": venue,
            "environment": environment,
            "version": version,
            "permissions": sorted(item.value for item in permissions),
            "reason": reason,
            "secret_backend": self.manager.backend_name,
            "secret_material_logged": False,
            "previous_hash": previous_hash,
        }
        row["record_hash"] = sha256(canonical_json(row).encode()).hexdigest()
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return row

    @staticmethod
    def _validate_policy(
        environment: CredentialEnvironment,
        permissions: Iterable[CredentialPermission],
    ) -> tuple[CredentialPermission, ...]:
        normalized = tuple(sorted(set(permissions), key=lambda item: item.value))
        if environment == CredentialEnvironment.OFFLINE_BACKTEST:
            raise ValueError("offline backtests cannot register exchange credentials")
        if not normalized:
            raise ValueError("credential requires explicit least-privilege permissions")
        if CredentialPermission.WITHDRAW in normalized:
            raise ValueError("withdrawal permission is prohibited")
        allowed = {
            CredentialPermission.READ_MARKET, CredentialPermission.READ_ACCOUNT,
            CredentialPermission.TRADE, CredentialPermission.CANCEL,
        }
        if not set(normalized).issubset(allowed):
            raise ValueError("credential contains unsupported permission")
        return normalized

    def register(
        self, *, credential_id: str, venue: str,
        environment: CredentialEnvironment,
        permissions: Iterable[CredentialPermission], secret_reference: str,
        secret: bytes | bytearray, actor: str,
    ) -> CredentialMetadata:
        if not all(value.strip() for value in (credential_id, venue, secret_reference, actor)):
            raise ValueError("credential ID, venue, secret reference and actor are required")
        if credential_id in self._metadata:
            raise ValueError("credential ID already exists")
        normalized = self._validate_policy(environment, permissions)
        material = bytearray(secret)
        if not material:
            raise ValueError("secret material is empty")
        now = self.clock_ns()
        try:
            self.manager.create(secret_reference, 1, memoryview(material))
        finally:
            _wipe(material)
        metadata = CredentialMetadata(
            credential_id, venue.upper(), environment, normalized,
            secret_reference, 1, CredentialState.ACTIVE, now, now,
        )
        self._metadata[credential_id] = metadata
        self._persist_metadata()
        self._audit(
            "REGISTER", "SUCCEEDED", credential_id=credential_id, actor=actor,
            venue=metadata.venue, environment=environment.value, version=1,
            permissions=normalized,
        )
        return metadata

    def rotate(
        self, credential_id: str, new_secret: bytes | bytearray, *, actor: str,
    ) -> CredentialMetadata:
        if not actor.strip():
            raise ValueError("rotation actor is required")
        metadata = self.metadata(credential_id)
        if metadata.state != CredentialState.ACTIVE:
            raise ValueError("revoked credential cannot be rotated")
        material = bytearray(new_secret)
        if not material:
            raise ValueError("new secret material is empty")
        new_version = metadata.version + 1
        try:
            self.manager.rotate(
                metadata.secret_reference, metadata.version, new_version,
                memoryview(material),
            )
        finally:
            _wipe(material)
        updated = CredentialMetadata(
            metadata.credential_id, metadata.venue, metadata.environment,
            metadata.permissions, metadata.secret_reference, new_version,
            metadata.state, metadata.created_ts_ns, self.clock_ns(),
        )
        self._metadata[credential_id] = updated
        self._persist_metadata()
        self._audit(
            "ROTATE", "SUCCEEDED", credential_id=credential_id, actor=actor,
            venue=updated.venue, environment=updated.environment.value,
            version=new_version, permissions=updated.permissions,
            reason=f"replaced version {metadata.version}",
        )
        return updated

    def revoke(self, credential_id: str, *, actor: str, reason: str) -> CredentialMetadata:
        if not actor.strip() or not reason.strip():
            raise ValueError("revocation actor and reason are required")
        metadata = self.metadata(credential_id)
        if metadata.state == CredentialState.REVOKED:
            return metadata
        self.manager.revoke(metadata.secret_reference, metadata.version)
        updated = CredentialMetadata(
            metadata.credential_id, metadata.venue, metadata.environment,
            metadata.permissions, metadata.secret_reference, metadata.version,
            CredentialState.REVOKED, metadata.created_ts_ns, self.clock_ns(),
        )
        self._metadata[credential_id] = updated
        self._persist_metadata()
        self._audit(
            "REVOKE", "SUCCEEDED", credential_id=credential_id, actor=actor,
            venue=updated.venue, environment=updated.environment.value,
            version=updated.version, permissions=updated.permissions, reason=reason,
        )
        return updated

    def metadata(self, credential_id: str) -> CredentialMetadata:
        try:
            return self._metadata[credential_id]
        except KeyError as exc:
            raise ValueError(f"unknown credential ID: {credential_id}") from exc

    def inject(
        self, credential_id: str, *, actor: str, venue: str,
        environment: CredentialEnvironment,
        required_permissions: Iterable[CredentialPermission],
        consumer: Callable[[memoryview], T],
    ) -> T:
        requested = tuple(sorted(set(required_permissions), key=lambda item: item.value))
        try:
            metadata = self.metadata(credential_id)
            if environment == CredentialEnvironment.OFFLINE_BACKTEST:
                raise ValueError("offline backtests prohibit credential injection")
            if metadata.state != CredentialState.ACTIVE:
                raise ValueError("credential is revoked")
            if metadata.venue != venue.upper():
                raise ValueError("credential venue mismatch")
            if metadata.environment != environment:
                raise ValueError("credential environment mismatch")
            missing = set(requested) - set(metadata.permissions)
            if missing:
                raise ValueError(
                    "credential lacks required permissions: "
                    + ",".join(sorted(item.value for item in missing))
                )
            if CredentialPermission.WITHDRAW in requested:
                raise ValueError("withdrawal permission is prohibited")
        except ValueError as exc:
            self._audit(
                "INJECT", "DENIED", credential_id=credential_id, actor=actor,
                venue=venue.upper(), environment=environment.value,
                permissions=requested, reason=str(exc),
            )
            raise
        try:
            material = bytearray(self.manager.read(
                metadata.secret_reference, metadata.version
            ))
        except Exception:
            self._audit(
                "INJECT", "FAILED", credential_id=credential_id, actor=actor,
                venue=metadata.venue, environment=metadata.environment.value,
                version=metadata.version, permissions=requested,
                reason="secret manager read failed",
            )
            raise
        try:
            result = consumer(memoryview(material))
        except Exception:
            self._audit(
                "INJECT", "FAILED", credential_id=credential_id, actor=actor,
                venue=metadata.venue, environment=metadata.environment.value,
                version=metadata.version, permissions=requested,
                reason="credential consumer failed",
            )
            raise
        finally:
            _wipe(material)
        self._audit(
            "INJECT", "SUCCEEDED", credential_id=credential_id, actor=actor,
            venue=metadata.venue, environment=metadata.environment.value,
            version=metadata.version, permissions=requested,
        )
        return result


class CredentialedTestnetTransport:
    """Inject a testnet secret into one request without exposing it to strategy code."""

    def __init__(
        self, broker: CredentialBroker, credential_id: str, venue: str,
        transport: Callable[[dict[str, Any], memoryview], Iterable[dict[str, Any]]],
        *, actor: str = "testnet-connector",
        environment: CredentialEnvironment = CredentialEnvironment.TESTNET,
    ):
        self.broker = broker
        self.credential_id = credential_id
        self.venue = venue
        self.transport = transport
        self.actor = actor
        self.environment = environment

    def __call__(self, request: dict[str, Any]) -> Iterable[dict[str, Any]]:
        permission = {
            "CANCEL": CredentialPermission.CANCEL,
            "QUERY": CredentialPermission.READ_ACCOUNT,
            "VERIFY_ACCOUNT": CredentialPermission.READ_ACCOUNT,
        }.get(str(request.get("action")), CredentialPermission.TRADE)
        return self.broker.inject(
            self.credential_id, actor=self.actor, venue=self.venue,
            environment=self.environment, required_permissions=(permission,),
            consumer=lambda secret: tuple(self.transport(request, secret)),
        )
