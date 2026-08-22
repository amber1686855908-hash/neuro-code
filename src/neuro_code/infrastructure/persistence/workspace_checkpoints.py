"""Independent SQLite persistence for managed workspace checkpoints.

Checkpoint records are immutable targets; rollback attempts are a separate
CAS-controlled operation log.  This database is intentionally not the session
store, execution checkpoint store, or managed-worktree ownership database.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from neuro_code.application.ports.checkpoints import (
    CheckpointFailureKind,
    WorkspaceCheckpointError,
    WorkspaceCheckpointStore,
)
from neuro_code.domain.checkpoints import (
    CheckpointFingerprint,
    CheckpointId,
    CheckpointState,
    RollbackAttempt,
    RollbackAttemptId,
    RollbackState,
    WorkspaceCheckpoint,
)
from neuro_code.domain.worktree import WorktreeId, WorktreeRepositoryIdentity
from neuro_code.shared.async_utils import run_blocking

SCHEMA_VERSION = 1
_SQLITE_TIMEOUT_SECONDS = 30.0


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_timestamp(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is not text")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class SqliteWorkspaceCheckpointStore(WorkspaceCheckpointStore):
    """Durable checkpoint store with insert-only targets and CAS operations."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().resolve(strict=False)
        self._write_lock = asyncio.Lock()

    @property
    def database_path(self) -> Path:
        return self._database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=_SQLITE_TIMEOUT_SECONDS)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(_SQLITE_TIMEOUT_SECONDS * 1_000)}")
        return connection

    async def initialize(self) -> None:
        def initialize_sync() -> None:
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS schema_meta (
                            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                            version INTEGER NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO schema_meta(singleton, version) VALUES (1, ?)",
                        (SCHEMA_VERSION,),
                    )
                    row = connection.execute(
                        "SELECT version FROM schema_meta WHERE singleton = 1"
                    ).fetchone()
                    if row is None or int(row[0]) != SCHEMA_VERSION:
                        version = "missing" if row is None else str(row[0])
                        raise WorkspaceCheckpointError(
                            f"unsupported workspace checkpoint schema version: {version}",
                            kind=CheckpointFailureKind.PROTOCOL,
                        )
                    _ensure_schema(connection)
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise

        async with self._write_lock:
            await run_blocking(initialize_sync)

    async def get(self, checkpoint_id: CheckpointId, /) -> WorkspaceCheckpoint | None:
        if not isinstance(checkpoint_id, CheckpointId):
            raise TypeError("checkpoint id must be canonical")

        def load() -> WorkspaceCheckpoint | None:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    """
                    SELECT checkpoint_id, worktree_id, common_dir, source_worktree, git_dir,
                           repository_head_sha, canonical_path, head_sha, branch, detached,
                           created_at, source_fingerprint, artifact_path, artifact_sha256,
                           artifact_bytes, artifact_file_count, state, version
                    FROM checkpoints WHERE checkpoint_id = ?
                    """,
                    (checkpoint_id.value,),
                ).fetchone()
            return _checkpoint_from_row(row) if row is not None else None

        return await run_blocking(load)

    async def list(
        self,
        *,
        worktree_id: str | None = None,
        include_failed: bool = False,
    ) -> tuple[WorkspaceCheckpoint, ...]:
        if worktree_id is not None:
            worktree_id = WorktreeId(worktree_id).value

        def load() -> tuple[WorkspaceCheckpoint, ...]:
            where: list[str] = []
            params: list[object] = []
            if worktree_id is not None:
                where.append("worktree_id = ?")
                params.append(worktree_id)
            if not include_failed:
                where.append("state != ?")
                params.append(CheckpointState.FAILED.value)
            clause = " WHERE " + " AND ".join(where) if where else ""
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT checkpoint_id, worktree_id, common_dir, source_worktree, git_dir,
                           repository_head_sha, canonical_path, head_sha, branch, detached,
                           created_at, source_fingerprint, artifact_path, artifact_sha256,
                           artifact_bytes, artifact_file_count, state, version
                    FROM checkpoints
                    """
                    + clause
                    + " ORDER BY created_at ASC, checkpoint_id ASC",
                    params,
                ).fetchall()
            return tuple(_checkpoint_from_row(row) for row in rows)

        return await run_blocking(load)

    async def insert_capturing(self, checkpoint: WorkspaceCheckpoint, /) -> WorkspaceCheckpoint:
        _require_checkpoint(checkpoint)
        if checkpoint.state is not CheckpointState.CAPTURING or checkpoint.version != 0:
            raise ValueError("new checkpoint intent must be capturing at version zero")

        def insert() -> WorkspaceCheckpoint:
            try:
                with closing(self._connect()) as connection, connection:
                    connection.execute(
                        """
                        INSERT INTO checkpoints(
                            checkpoint_id, worktree_id, common_dir, source_worktree, git_dir,
                            repository_head_sha, canonical_path, head_sha, branch, detached,
                            created_at, source_fingerprint, artifact_path, artifact_sha256,
                            artifact_bytes, artifact_file_count, state, version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        _checkpoint_values(checkpoint),
                    )
                return checkpoint
            except sqlite3.IntegrityError as error:
                raise WorkspaceCheckpointError(
                    "checkpoint id or worktree target already has a durable record",
                    kind=CheckpointFailureKind.CONCURRENT_MODIFICATION,
                ) from error
            except sqlite3.Error as error:
                raise WorkspaceCheckpointError(
                    "checkpoint intent could not be persisted",
                    kind=CheckpointFailureKind.COMMAND_FAILED,
                ) from error

        async with self._write_lock:
            return await run_blocking(insert)

    async def compare_and_transition_checkpoint(
        self,
        checkpoint: WorkspaceCheckpoint,
        *,
        expected_version: int,
        expected_state: CheckpointState,
    ) -> WorkspaceCheckpoint:
        _require_checkpoint(checkpoint)
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise TypeError("checkpoint expected version must be an integer")
        if checkpoint.version != expected_version:
            raise WorkspaceCheckpointError(
                "checkpoint version does not match the compare-and-swap claim",
                kind=CheckpointFailureKind.CONCURRENT_MODIFICATION,
            )
        if not isinstance(expected_state, CheckpointState):
            raise TypeError("checkpoint expected state must be canonical")

        def transition() -> WorkspaceCheckpoint:
            try:
                with closing(self._connect()) as connection, connection:
                    values = _checkpoint_values(replace(checkpoint, version=expected_version + 1))
                    cursor = connection.execute(
                        """
                        UPDATE checkpoints SET
                            worktree_id = ?, common_dir = ?, source_worktree = ?, git_dir = ?,
                            repository_head_sha = ?, canonical_path = ?, head_sha = ?, branch = ?,
                            detached = ?, created_at = ?, source_fingerprint = ?, artifact_path = ?,
                            artifact_sha256 = ?, artifact_bytes = ?, artifact_file_count = ?,
                            state = ?, version = ?
                        WHERE checkpoint_id = ? AND version = ? AND state = ?
                        """,
                        (
                            *values[1:],
                            values[0],
                            expected_version,
                            expected_state.value,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise WorkspaceCheckpointError(
                            "checkpoint was changed by another process",
                            kind=CheckpointFailureKind.CONCURRENT_MODIFICATION,
                        )
                return replace(checkpoint, version=expected_version + 1)
            except sqlite3.Error as error:
                raise WorkspaceCheckpointError(
                    "checkpoint transition could not be persisted",
                    kind=CheckpointFailureKind.COMMAND_FAILED,
                ) from error

        async with self._write_lock:
            return await run_blocking(transition)

    async def get_attempt(self, attempt_id: RollbackAttemptId, /) -> RollbackAttempt | None:
        if not isinstance(attempt_id, RollbackAttemptId):
            raise TypeError("rollback attempt id must be canonical")

        def load() -> RollbackAttempt | None:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    """
                    SELECT attempt_id, checkpoint_id, worktree_id, state, started_at,
                           completed_at, expected_fingerprint, observed_fingerprint,
                           owner_pid, owner_token, error_kind, version
                    FROM rollback_attempts WHERE attempt_id = ?
                    """,
                    (attempt_id.value,),
                ).fetchone()
            return _attempt_from_row(row) if row is not None else None

        return await run_blocking(load)

    async def active_attempt(self, worktree_id: str, /) -> RollbackAttempt | None:
        identifier = WorktreeId(worktree_id).value

        def load() -> RollbackAttempt | None:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    """
                    SELECT attempt_id, checkpoint_id, worktree_id, state, started_at,
                           completed_at, expected_fingerprint, observed_fingerprint,
                           owner_pid, owner_token, error_kind, version
                    FROM rollback_attempts
                    WHERE worktree_id = ? AND state IN (?, ?)
                    ORDER BY started_at ASC, attempt_id ASC LIMIT 1
                    """,
                    (identifier, RollbackState.STARTED.value, RollbackState.INDETERMINATE.value),
                ).fetchone()
            return _attempt_from_row(row) if row is not None else None

        return await run_blocking(load)

    async def list_active_attempts(self) -> tuple[RollbackAttempt, ...]:
        def load() -> tuple[RollbackAttempt, ...]:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT attempt_id, checkpoint_id, worktree_id, state, started_at,
                           completed_at, expected_fingerprint, observed_fingerprint,
                           owner_pid, owner_token, error_kind, version
                    FROM rollback_attempts
                    WHERE state IN (?, ?)
                    ORDER BY started_at ASC, attempt_id ASC
                    """,
                    (RollbackState.STARTED.value, RollbackState.INDETERMINATE.value),
                ).fetchall()
            return tuple(_attempt_from_row(row) for row in rows)

        return await run_blocking(load)

    async def start_attempt(self, attempt: RollbackAttempt, /) -> RollbackAttempt:
        _require_attempt(attempt)
        if attempt.state is not RollbackState.STARTED or attempt.version != 0:
            raise ValueError("new rollback attempt must start at version zero")

        def insert() -> RollbackAttempt:
            try:
                with closing(self._connect()) as connection, connection:
                    connection.execute(
                        """
                        INSERT INTO rollback_attempts(
                            attempt_id, checkpoint_id, worktree_id, state, started_at,
                            completed_at, expected_fingerprint, observed_fingerprint,
                            owner_pid, owner_token, error_kind, version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        _attempt_values(attempt),
                    )
                return attempt
            except sqlite3.IntegrityError as error:
                raise WorkspaceCheckpointError(
                    "another rollback already owns this managed worktree",
                    kind=CheckpointFailureKind.ALREADY_ROLLING_BACK,
                ) from error
            except sqlite3.Error as error:
                raise WorkspaceCheckpointError(
                    "rollback attempt could not be persisted",
                    kind=CheckpointFailureKind.COMMAND_FAILED,
                ) from error

        async with self._write_lock:
            return await run_blocking(insert)

    async def compare_and_transition_attempt(
        self,
        attempt: RollbackAttempt,
        *,
        expected_version: int,
        expected_state: RollbackState,
    ) -> RollbackAttempt:
        _require_attempt(attempt)
        if attempt.version != expected_version:
            raise WorkspaceCheckpointError(
                "rollback attempt version does not match the compare-and-swap claim",
                kind=CheckpointFailureKind.CONCURRENT_MODIFICATION,
            )
        if not isinstance(expected_state, RollbackState):
            raise TypeError("rollback expected state must be canonical")

        def transition() -> RollbackAttempt:
            try:
                with closing(self._connect()) as connection, connection:
                    values = _attempt_values(replace(attempt, version=expected_version + 1))
                    cursor = connection.execute(
                        """
                        UPDATE rollback_attempts SET
                            checkpoint_id = ?, worktree_id = ?, state = ?, started_at = ?,
                            completed_at = ?, expected_fingerprint = ?, observed_fingerprint = ?,
                            owner_pid = ?, owner_token = ?, error_kind = ?, version = ?
                        WHERE attempt_id = ? AND version = ? AND state = ?
                        """,
                        (
                            *values[1:],
                            values[0],
                            expected_version,
                            expected_state.value,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise WorkspaceCheckpointError(
                            "rollback attempt was changed by another process",
                            kind=CheckpointFailureKind.CONCURRENT_MODIFICATION,
                        )
                return replace(attempt, version=expected_version + 1)
            except sqlite3.Error as error:
                raise WorkspaceCheckpointError(
                    "rollback attempt transition could not be persisted",
                    kind=CheckpointFailureKind.COMMAND_FAILED,
                ) from error

        async with self._write_lock:
            return await run_blocking(transition)


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            worktree_id TEXT NOT NULL,
            common_dir TEXT NOT NULL,
            source_worktree TEXT NOT NULL,
            git_dir TEXT NOT NULL,
            repository_head_sha TEXT NOT NULL,
            canonical_path TEXT NOT NULL,
            head_sha TEXT NOT NULL,
            branch TEXT,
            detached INTEGER NOT NULL CHECK (detached IN (0, 1)),
            created_at TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,
            artifact_path TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL,
            artifact_bytes INTEGER NOT NULL,
            artifact_file_count INTEGER NOT NULL,
            state TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS checkpoints_by_worktree_and_id "
        "ON checkpoints(worktree_id, checkpoint_id)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS rollback_attempts (
            attempt_id TEXT PRIMARY KEY,
            checkpoint_id TEXT NOT NULL REFERENCES checkpoints(checkpoint_id),
            worktree_id TEXT NOT NULL,
            state TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            expected_fingerprint TEXT NOT NULL,
            observed_fingerprint TEXT,
            owner_pid INTEGER,
            owner_token TEXT NOT NULL,
            error_kind TEXT,
            version INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS active_rollback_by_worktree "
        "ON rollback_attempts(worktree_id) WHERE state IN ('started', 'indeterminate')"
    )


def _require_checkpoint(checkpoint: WorkspaceCheckpoint) -> None:
    if not isinstance(checkpoint, WorkspaceCheckpoint):
        raise TypeError("checkpoint store accepts canonical checkpoints")


def _checkpoint_values(checkpoint: WorkspaceCheckpoint) -> tuple[object, ...]:
    return (
        checkpoint.checkpoint_id.value,
        checkpoint.worktree_id.value,
        str(checkpoint.repository.common_dir),
        str(checkpoint.repository.source_worktree),
        str(checkpoint.repository.git_dir),
        checkpoint.repository.head_sha,
        str(checkpoint.canonical_path),
        checkpoint.head_sha,
        checkpoint.branch,
        int(checkpoint.detached),
        checkpoint.created_at.isoformat(),
        checkpoint.source_fingerprint.value,
        str(checkpoint.artifact_path),
        checkpoint.artifact_sha256,
        checkpoint.artifact_bytes,
        checkpoint.artifact_file_count,
        checkpoint.state.value,
        checkpoint.version,
    )


def _checkpoint_from_row(row: Sequence[object] | None) -> WorkspaceCheckpoint:
    if row is None or len(row) != 18:
        raise WorkspaceCheckpointError(
            "workspace checkpoint record is malformed", kind=CheckpointFailureKind.PROTOCOL
        )
    try:
        return WorkspaceCheckpoint(
            checkpoint_id=CheckpointId(str(row[0])),
            worktree_id=WorktreeId(str(row[1])),
            repository=WorktreeRepositoryIdentity(
                common_dir=Path(str(row[2])),
                source_worktree=Path(str(row[3])),
                git_dir=Path(str(row[4])),
                head_sha=str(row[5]),
            ),
            canonical_path=Path(str(row[6])),
            head_sha=str(row[7]),
            branch=None if row[8] is None else str(row[8]),
            detached=bool(row[9]),
            created_at=_parse_timestamp(row[10], field_name="checkpoint created_at"),
            source_fingerprint=CheckpointFingerprint(str(row[11])),
            artifact_path=Path(str(row[12])),
            artifact_sha256=str(row[13]),
            artifact_bytes=int(str(row[14])),
            artifact_file_count=int(str(row[15])),
            state=CheckpointState(str(row[16])),
            version=int(str(row[17])),
        )
    except (TypeError, ValueError, OverflowError) as error:
        raise WorkspaceCheckpointError(
            "workspace checkpoint record is malformed", kind=CheckpointFailureKind.PROTOCOL
        ) from error


def _require_attempt(attempt: RollbackAttempt) -> None:
    if not isinstance(attempt, RollbackAttempt):
        raise TypeError("rollback store accepts canonical attempts")


def _attempt_values(attempt: RollbackAttempt) -> tuple[object, ...]:
    return (
        attempt.attempt_id.value,
        attempt.checkpoint_id.value,
        attempt.worktree_id.value,
        attempt.state.value,
        attempt.started_at.isoformat(),
        None if attempt.completed_at is None else attempt.completed_at.isoformat(),
        attempt.expected_fingerprint.value,
        None if attempt.observed_fingerprint is None else attempt.observed_fingerprint.value,
        attempt.owner_pid,
        attempt.owner_token,
        attempt.error_kind,
        attempt.version,
    )


def _attempt_from_row(row: Sequence[object] | None) -> RollbackAttempt:
    if row is None or len(row) != 12:
        raise WorkspaceCheckpointError(
            "rollback attempt record is malformed", kind=CheckpointFailureKind.PROTOCOL
        )
    try:
        return RollbackAttempt(
            attempt_id=RollbackAttemptId(str(row[0])),
            checkpoint_id=CheckpointId(str(row[1])),
            worktree_id=WorktreeId(str(row[2])),
            state=RollbackState(str(row[3])),
            started_at=_parse_timestamp(row[4], field_name="rollback started_at"),
            completed_at=(
                None
                if row[5] is None
                else _parse_timestamp(row[5], field_name="rollback completed_at")
            ),
            expected_fingerprint=CheckpointFingerprint(str(row[6])),
            observed_fingerprint=(None if row[7] is None else CheckpointFingerprint(str(row[7]))),
            owner_pid=None if row[8] is None else int(str(row[8])),
            owner_token=str(row[9]),
            error_kind=None if row[10] is None else str(row[10]),
            version=int(str(row[11])),
        )
    except (TypeError, ValueError, OverflowError) as error:
        raise WorkspaceCheckpointError(
            "rollback attempt record is malformed", kind=CheckpointFailureKind.PROTOCOL
        ) from error


__all__ = ["SCHEMA_VERSION", "SqliteWorkspaceCheckpointStore"]
