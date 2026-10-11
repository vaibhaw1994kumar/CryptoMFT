from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable

from .risk import ReconciliationDecision, ReconciliationReport


RESTART_RESILIENCE_SCHEMA_VERSION = "1.0.0"


class SessionState(StrEnum):
    RECONNECTING = "RECONNECTING"
    RECONCILING = "RECONCILING"
    READY = "READY"
    SUSPENDED = "SUSPENDED"


class ConnectorState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTED = "CONNECTED"


class OwnershipError(RuntimeError):
    """Raised when a process is not the current, live fenced order owner."""


class ResumeGateError(RuntimeError):
    """Raised when new risk is attempted before reconnect/reconciliation."""


@dataclass(frozen=True, slots=True)
class OwnerLease:
    session_id: str
    owner_id: str
    fencing_token: int
    heartbeat_ts_ns: int
    lease_expires_ts_ns: int
    state: SessionState


@dataclass(frozen=True, slots=True)
class ReplayCheckpoint:
    session_id: str
    stream_id: str
    sequence: int
    raw_ref: str
    updated_ts_ns: int


class RestartCoordinator:
    """Durable process ownership, reconnect and reconcile-before-resume gate."""

    def __init__(
        self,
        path: Path,
        session_id: str,
        *,
        required_venues: tuple[str, ...],
        lease_duration_ns: int = 15_000_000_000,
        reconnect_base_ns: int = 250_000_000,
        reconnect_max_ns: int = 30_000_000_000,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if not session_id or not required_venues:
            raise ValueError("session_id and at least one required venue are required")
        if lease_duration_ns <= 0 or reconnect_base_ns <= 0:
            raise ValueError("lease and reconnect durations must be positive")
        if reconnect_max_ns < reconnect_base_ns:
            raise ValueError("reconnect_max_ns must be >= reconnect_base_ns")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.session_id = session_id
        self.required_venues = tuple(dict.fromkeys(required_venues))
        self.lease_duration_ns = lease_duration_ns
        self.reconnect_base_ns = reconnect_base_ns
        self.reconnect_max_ns = reconnect_max_ns
        self.clock_ns = clock_ns
        self.connection = sqlite3.connect(path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS restart_sessions (
                session_id TEXT PRIMARY KEY,
                owner_id TEXT,
                fencing_token INTEGER NOT NULL DEFAULT 0,
                heartbeat_ts_ns INTEGER,
                lease_expires_ts_ns INTEGER,
                state TEXT NOT NULL,
                reconciliation_required INTEGER NOT NULL DEFAULT 1,
                last_reconciliation_id TEXT,
                updated_ts_ns INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS restart_connectors (
                session_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                state TEXT NOT NULL,
                reconnect_attempts INTEGER NOT NULL DEFAULT 0,
                next_reconnect_ts_ns INTEGER,
                last_error TEXT,
                updated_ts_ns INTEGER NOT NULL,
                PRIMARY KEY(session_id, venue)
            );
            CREATE TABLE IF NOT EXISTS replay_checkpoints (
                session_id TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                raw_ref TEXT NOT NULL,
                updated_ts_ns INTEGER NOT NULL,
                PRIMARY KEY(session_id, stream_id)
            );
            CREATE TABLE IF NOT EXISTS restart_events (
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                ts_ns INTEGER NOT NULL,
                owner_id TEXT,
                fencing_token INTEGER,
                payload_json TEXT NOT NULL
            );
        """)
        now = self.clock_ns()
        self.connection.execute(
            """INSERT OR IGNORE INTO restart_sessions(
                session_id, state, reconciliation_required, updated_ts_ns
            ) VALUES(?, ?, 1, ?)""",
            (session_id, SessionState.RECONNECTING.value, now),
        )
        for venue in self.required_venues:
            self.connection.execute(
                """INSERT OR IGNORE INTO restart_connectors(
                    session_id, venue, state, updated_ts_ns
                ) VALUES(?, ?, ?, ?)""",
                (session_id, venue, ConnectorState.DISCONNECTED.value, now),
            )

    def _event(
        self, event_type: str, now_ns: int, owner_id: str | None,
        fencing_token: int | None, **payload: Any,
    ) -> None:
        self.connection.execute(
            """INSERT INTO restart_events(
                session_id, event_type, ts_ns, owner_id, fencing_token, payload_json
            ) VALUES(?, ?, ?, ?, ?, ?)""",
            (
                self.session_id, event_type, now_ns, owner_id, fencing_token,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
            ),
        )

    def acquire(self, owner_id: str, *, now_ns: int | None = None) -> OwnerLease:
        if not owner_id:
            raise ValueError("owner_id is required")
        now = self.clock_ns() if now_ns is None else now_ns
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM restart_sessions WHERE session_id=?", (self.session_id,)
            ).fetchone()
            current_owner = row["owner_id"]
            expires = row["lease_expires_ts_ns"]
            if current_owner and current_owner != owner_id and expires is not None and now <= int(expires):
                raise OwnershipError(
                    f"session is owned by {current_owner} until {int(expires)}"
                )
            token = int(row["fencing_token"]) + 1
            expiry = now + self.lease_duration_ns
            self.connection.execute(
                """UPDATE restart_sessions SET owner_id=?, fencing_token=?,
                    heartbeat_ts_ns=?, lease_expires_ts_ns=?, state=?,
                    reconciliation_required=1, last_reconciliation_id=NULL,
                    updated_ts_ns=? WHERE session_id=?""",
                (
                    owner_id, token, now, expiry, SessionState.RECONNECTING.value,
                    now, self.session_id,
                ),
            )
            self.connection.execute(
                """UPDATE restart_connectors SET state=?, reconnect_attempts=0,
                    next_reconnect_ts_ns=?, last_error=NULL, updated_ts_ns=?
                    WHERE session_id=?""",
                (ConnectorState.DISCONNECTED.value, now, now, self.session_id),
            )
            self._event("OWNER_ACQUIRED", now, owner_id, token, lease_expires_ts_ns=expiry)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return OwnerLease(
            self.session_id, owner_id, token, now, expiry, SessionState.RECONNECTING
        )

    def _assert_owner(
        self, owner_id: str, fencing_token: int, now_ns: int,
    ) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM restart_sessions WHERE session_id=?", (self.session_id,)
        ).fetchone()
        if (
            row["owner_id"] != owner_id
            or int(row["fencing_token"]) != fencing_token
            or row["lease_expires_ts_ns"] is None
            or now_ns > int(row["lease_expires_ts_ns"])
        ):
            raise OwnershipError("owner lease is expired or fenced by another process")
        return row

    def assert_owner(
        self, owner_id: str, fencing_token: int, *, now_ns: int | None = None,
    ) -> None:
        self._assert_owner(
            owner_id, fencing_token, self.clock_ns() if now_ns is None else now_ns
        )

    def heartbeat(
        self, owner_id: str, fencing_token: int, *, now_ns: int | None = None,
    ) -> OwnerLease:
        now = self.clock_ns() if now_ns is None else now_ns
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._assert_owner(owner_id, fencing_token, now)
            expiry = now + self.lease_duration_ns
            self.connection.execute(
                """UPDATE restart_sessions SET heartbeat_ts_ns=?,
                    lease_expires_ts_ns=?, updated_ts_ns=? WHERE session_id=?""",
                (now, expiry, now, self.session_id),
            )
            self._event("HEARTBEAT", now, owner_id, fencing_token, lease_expires_ts_ns=expiry)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return OwnerLease(
            self.session_id, owner_id, fencing_token, now, expiry,
            SessionState(row["state"]),
        )

    def record_disconnect(
        self, owner_id: str, fencing_token: int, venue: str, error: str,
        *, now_ns: int | None = None,
    ) -> int:
        now = self.clock_ns() if now_ns is None else now_ns
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._assert_owner(owner_id, fencing_token, now)
            row = self.connection.execute(
                """SELECT reconnect_attempts FROM restart_connectors
                   WHERE session_id=? AND venue=?""", (self.session_id, venue),
            ).fetchone()
            if row is None:
                raise KeyError(f"venue {venue} is not required by this session")
            attempt = int(row[0]) + 1
            delay = min(
                self.reconnect_max_ns,
                self.reconnect_base_ns * (2 ** (attempt - 1)),
            )
            due = now + delay
            self.connection.execute(
                """UPDATE restart_connectors SET state=?, reconnect_attempts=?,
                    next_reconnect_ts_ns=?, last_error=?, updated_ts_ns=?
                    WHERE session_id=? AND venue=?""",
                (
                    ConnectorState.DISCONNECTED.value, attempt, due, error, now,
                    self.session_id, venue,
                ),
            )
            self.connection.execute(
                """UPDATE restart_sessions SET state=?, reconciliation_required=1,
                    updated_ts_ns=? WHERE session_id=?""",
                (SessionState.RECONNECTING.value, now, self.session_id),
            )
            self._event(
                "CONNECTOR_DISCONNECTED", now, owner_id, fencing_token,
                venue=venue, error=error, reconnect_attempt=attempt,
                next_reconnect_ts_ns=due,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return due

    def reconnect_due(self, venue: str, *, now_ns: int | None = None) -> bool:
        now = self.clock_ns() if now_ns is None else now_ns
        row = self.connection.execute(
            """SELECT state, next_reconnect_ts_ns FROM restart_connectors
               WHERE session_id=? AND venue=?""", (self.session_id, venue),
        ).fetchone()
        if row is None:
            raise KeyError(venue)
        return (
            row["state"] == ConnectorState.DISCONNECTED.value
            and row["next_reconnect_ts_ns"] is not None
            and now >= int(row["next_reconnect_ts_ns"])
        )

    def run_due_reconnects(
        self, owner_id: str, fencing_token: int,
        connect: Callable[[str], None], *, now_ns: int | None = None,
    ) -> dict[str, bool]:
        """Attempt each due connector once and durably schedule any failure."""
        now = self.clock_ns() if now_ns is None else now_ns
        self._assert_owner(owner_id, fencing_token, now)
        due = [
            venue for venue in self.required_venues
            if self.reconnect_due(venue, now_ns=now)
        ]
        outcomes: dict[str, bool] = {}
        for venue in due:
            try:
                connect(venue)
            except Exception as exc:
                self.record_disconnect(
                    owner_id, fencing_token, venue, str(exc), now_ns=now
                )
                outcomes[venue] = False
            else:
                self.mark_connected(
                    owner_id, fencing_token, venue, now_ns=now
                )
                outcomes[venue] = True
        return outcomes

    def mark_connected(
        self, owner_id: str, fencing_token: int, venue: str,
        *, now_ns: int | None = None,
    ) -> SessionState:
        now = self.clock_ns() if now_ns is None else now_ns
        if venue not in self.required_venues:
            raise KeyError(venue)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._assert_owner(owner_id, fencing_token, now)
            self.connection.execute(
                """UPDATE restart_connectors SET state=?, next_reconnect_ts_ns=NULL,
                    last_error=NULL, updated_ts_ns=? WHERE session_id=? AND venue=?""",
                (ConnectorState.CONNECTED.value, now, self.session_id, venue),
            )
            disconnected = self.connection.execute(
                """SELECT COUNT(*) FROM restart_connectors
                   WHERE session_id=? AND state!=?""",
                (self.session_id, ConnectorState.CONNECTED.value),
            ).fetchone()[0]
            state = (
                SessionState.RECONCILING if disconnected == 0
                else SessionState.RECONNECTING
            )
            self.connection.execute(
                "UPDATE restart_sessions SET state=?, updated_ts_ns=? WHERE session_id=?",
                (state.value, now, self.session_id),
            )
            self._event("CONNECTOR_CONNECTED", now, owner_id, fencing_token, venue=venue)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return state

    def resume_after_reconciliation(
        self, owner_id: str, fencing_token: int, report: ReconciliationReport,
        *, now_ns: int | None = None,
    ) -> None:
        now = self.clock_ns() if now_ns is None else now_ns
        rejected = False
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._assert_owner(owner_id, fencing_token, now)
            disconnected = self.connection.execute(
                """SELECT venue FROM restart_connectors
                   WHERE session_id=? AND state!=? ORDER BY venue""",
                (self.session_id, ConnectorState.CONNECTED.value),
            ).fetchall()
            if disconnected:
                raise ResumeGateError("all required venues must reconnect before resume")
            if report.decision != ReconciliationDecision.APPROVED:
                self.connection.execute(
                    "UPDATE restart_sessions SET state=?, updated_ts_ns=? WHERE session_id=?",
                    (SessionState.SUSPENDED.value, now, self.session_id),
                )
                self._event(
                    "RECONCILIATION_REJECTED", now, owner_id, fencing_token,
                    reconciliation_id=report.reconciliation_id,
                    issues=list(report.issues),
                )
                rejected = True
            else:
                self.connection.execute(
                    """UPDATE restart_sessions SET state=?, reconciliation_required=0,
                        last_reconciliation_id=?, updated_ts_ns=? WHERE session_id=?""",
                    (
                        SessionState.READY.value, report.reconciliation_id, now,
                        self.session_id,
                    ),
                )
                self._event(
                    "RESUME_APPROVED", now, owner_id, fencing_token,
                    reconciliation_id=report.reconciliation_id,
                    prior_state=row["state"],
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        if rejected:
            raise ResumeGateError("RMS reconciliation rejected; session remains suspended")

    def assert_new_risk_allowed(
        self, owner_id: str, fencing_token: int, *, now_ns: int | None = None,
    ) -> None:
        now = self.clock_ns() if now_ns is None else now_ns
        row = self._assert_owner(owner_id, fencing_token, now)
        if row["state"] != SessionState.READY.value or bool(row["reconciliation_required"]):
            raise ResumeGateError("new risk is blocked until reconnect and reconciliation approve")

    def save_checkpoint(
        self, owner_id: str, fencing_token: int, stream_id: str, sequence: int,
        raw_ref: str, *, now_ns: int | None = None,
    ) -> ReplayCheckpoint:
        if sequence < 0 or not stream_id or not raw_ref:
            raise ValueError("checkpoint requires stream_id, non-negative sequence and raw_ref")
        now = self.clock_ns() if now_ns is None else now_ns
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._assert_owner(owner_id, fencing_token, now)
            current = self.connection.execute(
                """SELECT sequence, raw_ref FROM replay_checkpoints
                   WHERE session_id=? AND stream_id=?""", (self.session_id, stream_id),
            ).fetchone()
            if current and sequence < int(current["sequence"]):
                raise ValueError("checkpoint sequence cannot move backwards")
            if (
                current
                and sequence == int(current["sequence"])
                and raw_ref != current["raw_ref"]
            ):
                raise ValueError(
                    "same checkpoint sequence cannot reference different raw data"
                )
            self.connection.execute(
                """INSERT INTO replay_checkpoints(
                    session_id, stream_id, sequence, raw_ref, updated_ts_ns
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(session_id, stream_id) DO UPDATE SET
                    sequence=excluded.sequence, raw_ref=excluded.raw_ref,
                    updated_ts_ns=excluded.updated_ts_ns""",
                (self.session_id, stream_id, sequence, raw_ref, now),
            )
            self._event(
                "CHECKPOINT_COMMITTED", now, owner_id, fencing_token,
                stream_id=stream_id, sequence=sequence, raw_ref=raw_ref,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return ReplayCheckpoint(self.session_id, stream_id, sequence, raw_ref, now)

    def load_checkpoint(self, stream_id: str) -> ReplayCheckpoint | None:
        row = self.connection.execute(
            """SELECT * FROM replay_checkpoints
               WHERE session_id=? AND stream_id=?""", (self.session_id, stream_id),
        ).fetchone()
        return None if row is None else ReplayCheckpoint(
            row["session_id"], row["stream_id"], int(row["sequence"]),
            row["raw_ref"], int(row["updated_ts_ns"]),
        )

    def snapshot(self) -> dict[str, Any]:
        session = dict(self.connection.execute(
            "SELECT * FROM restart_sessions WHERE session_id=?", (self.session_id,)
        ).fetchone())
        connectors = [dict(row) for row in self.connection.execute(
            """SELECT venue, state, reconnect_attempts, next_reconnect_ts_ns,
                      last_error, updated_ts_ns FROM restart_connectors
               WHERE session_id=? ORDER BY venue""", (self.session_id,),
        )]
        checkpoints = [dict(row) for row in self.connection.execute(
            """SELECT stream_id, sequence, raw_ref, updated_ts_ns
               FROM replay_checkpoints WHERE session_id=? ORDER BY stream_id""",
            (self.session_id,),
        )]
        return {
            "schema_version": RESTART_RESILIENCE_SCHEMA_VERSION,
            "session": session,
            "connectors": connectors,
            "checkpoints": checkpoints,
        }

    def events(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT ordinal, event_type, ts_ns, owner_id, fencing_token, payload_json
               FROM restart_events WHERE session_id=? ORDER BY ordinal""",
            (self.session_id,),
        ).fetchall()
        return [{
            "ordinal": int(row["ordinal"]), "event_type": row["event_type"],
            "ts_ns": int(row["ts_ns"]), "owner_id": row["owner_id"],
            "fencing_token": row["fencing_token"],
            "payload": json.loads(row["payload_json"]),
        } for row in rows]

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "RestartCoordinator":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
