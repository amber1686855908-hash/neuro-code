"""Durable ownership store for application-managed Git worktrees.

This database is deliberately separate from the session event store.  SQLite
intent and Git metadata are not one ACID transaction; the application service
uses this store for intent and later reconciliation for the actual Git state.

应用拥有的 Git worktree 持久化 ownership store.

该数据库刻意独立于会话事件 store.SQLite intent 与 Git metadata 不是一个 ACID
事务;应用服务使用本 store 保存 intent,再通过 reconciliation 对齐真实 Git 状态.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from neuro_code.application.ports.worktree import (
    ManagedWorktreeStore,
    WorktreeError,
    WorktreeFailureKind,
)
from neuro_code.domain.worktree import (
    WorktreeId,
    WorktreeKind,
    WorktreeOwnership,
    WorktreeRepositoryIdentity,
    WorktreeSnapshot,
    WorktreeState,
)
from neuro_code.shared.async_utils import run_blocking

SCHEMA_VERSION = 1
_SQLITE_TIMEOUT_SECONDS = 30.0


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("worktree timestamp is not text")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class SqliteManagedWorktreeStore(ManagedWorktreeStore):
    """SQLite implementation with an independent, versioned schema."""

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
                        "INSERT OR IGNORE INTO schema_meta(singleton, version) VALUES (1, 1)"
                    )
                    version_row = connection.execute(
                        "SELECT version FROM schema_meta WHERE singleton = 1"
                    ).fetchone()
                    if version_row is None or int(version_row[0]) != SCHEMA_VERSION:
                        raise WorktreeError(
                            f"unsupported managed worktree schema version: "
                            f"{version_row[0] if version_row else 'missing'}",
                            kind=WorktreeFailureKind.PROTOCOL,
                        )
                    _ensure_schema(connection)
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise

        async with self._write_lock:
            await run_blocking(initialize_sync)

    async def get(self, worktree_id: str, /) -> WorktreeSnapshot | None:
        identifier = WorktreeId(worktree_id).value

        def load() -> WorktreeSnapshot | None:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    """
                    SELECT worktree_id, common_dir, source_worktree, git_dir, repository_head_sha,
                           canonical_path, base_revision, base_commit_sha, branch, kind,
                           ownership, state, created_at, managed, created_by_session_id
                    FROM managed_worktrees
                    WHERE worktree_id = ?
                    """,
                    (identifier,),
                ).fetchone()
            return _snapshot_from_row(row) if row is not None else None

        return await run_blocking(load)

    async def list(
        self,
        *,
        include_removed: bool = False,
        repository_id: str | None = None,
    ) -> tuple[WorktreeSnapshot, ...]:
        if not isinstance(include_removed, bool):
            raise TypeError("include_removed must be boolean")

        def load() -> tuple[WorktreeSnapshot, ...]:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT worktree_id, common_dir, source_worktree, git_dir, repository_head_sha,
                           canonical_path, base_revision, base_commit_sha, branch, kind,
                           ownership, state, created_at, managed, created_by_session_id
                    FROM managed_worktrees
                    ORDER BY created_at ASC, worktree_id ASC
                    """
                ).fetchall()
            snapshots = tuple(_snapshot_from_row(row) for row in rows)
            if not include_removed:
                snapshots = tuple(
                    snapshot
                    for snapshot in snapshots
                    if snapshot.state is not WorktreeState.REMOVED
                )
            if repository_id is not None:
                snapshots = tuple(
                    snapshot
                    for snapshot in snapshots
                    if snapshot.repository.repository_id == repository_id
                )
            return snapshots

        return await run_blocking(load)

    async def save(self, snapshot: WorktreeSnapshot, /) -> None:
        if not isinstance(snapshot, WorktreeSnapshot):
            raise TypeError("managed worktree store accepts canonical snapshots")

        def save_sync() -> None:
            try:
                with closing(self._connect()) as connection, connection:
                    connection.execute(
                        """
                        INSERT INTO managed_worktrees(
                            worktree_id, common_dir, source_worktree, git_dir,
                            repository_head_sha, canonical_path, base_revision,
                            base_commit_sha, branch, kind, ownership, state,
                            created_at, updated_at, managed, created_by_session_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(worktree_id) DO UPDATE SET
                            common_dir = excluded.common_dir,
                            source_worktree = excluded.source_worktree,
                            git_dir = excluded.git_dir,
                            repository_head_sha = excluded.repository_head_sha,
                            canonical_path = excluded.canonical_path,
                            base_revision = excluded.base_revision,
                            base_commit_sha = excluded.base_commit_sha,
                            branch = excluded.branch,
                            kind = excluded.kind,
                            ownership = excluded.ownership,
                            state = excluded.state,
                            updated_at = excluded.updated_at,
                            managed = excluded.managed,
                            created_by_session_id = excluded.created_by_session_id
                        """,
                        (
                            snapshot.worktree_id.value,
                            str(snapshot.repository.common_dir),
                            str(snapshot.repository.source_worktree),
                            str(snapshot.repository.git_dir),
                            snapshot.repository.head_sha,
                            str(snapshot.canonical_path),
                            snapshot.base_revision,
                            snapshot.base_commit_sha,
                            snapshot.branch,
                            snapshot.kind.value,
                            snapshot.ownership.value,
                            snapshot.state.value,
                            snapshot.created_at.isoformat(),
                            _utc_now(),
                            int(snapshot.managed),
                            snapshot.created_by_session_id,
                        ),
                    )
            except sqlite3.IntegrityError as error:
                raise WorktreeError(
                    "managed worktree ownership conflicts with an existing record",
                    kind=WorktreeFailureKind.PATH_CONFLICT,
                ) from error
            except sqlite3.Error as error:
                raise WorktreeError(
                    "managed worktree ownership could not be persisted",
                    kind=WorktreeFailureKind.COMMAND_FAILED,
                ) from error

        async with self._write_lock:
            await run_blocking(save_sync)


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS managed_worktrees (
            worktree_id TEXT PRIMARY KEY,
            common_dir TEXT NOT NULL,
            source_worktree TEXT NOT NULL,
            git_dir TEXT NOT NULL,
            repository_head_sha TEXT NOT NULL,
            canonical_path TEXT NOT NULL UNIQUE,
            base_revision TEXT NOT NULL,
            base_commit_sha TEXT NOT NULL,
            branch TEXT,
            kind TEXT NOT NULL,
            ownership TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            managed INTEGER NOT NULL CHECK (managed IN (0, 1)),
            created_by_session_id TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS managed_worktrees_by_repository
        ON managed_worktrees(common_dir, created_at, worktree_id)
        """
    )


def _snapshot_from_row(row: Sequence[object] | None) -> WorktreeSnapshot:
    if row is None or len(row) != 15:
        raise WorktreeError(
            "managed worktree record is malformed", kind=WorktreeFailureKind.PROTOCOL
        )
    try:
        return WorktreeSnapshot(
            worktree_id=WorktreeId(str(row[0])),
            repository=WorktreeRepositoryIdentity(
                common_dir=Path(str(row[1])),
                source_worktree=Path(str(row[2])),
                git_dir=Path(str(row[3])),
                head_sha=str(row[4]),
            ),
            canonical_path=Path(str(row[5])),
            base_revision=str(row[6]),
            base_commit_sha=str(row[7]),
            branch=None if row[8] is None else str(row[8]),
            kind=WorktreeKind(str(row[9])),
            ownership=WorktreeOwnership(str(row[10])),
            state=WorktreeState(str(row[11])),
            created_at=_parse_timestamp(row[12]),
            managed=bool(row[13]),
            created_by_session_id=None if row[14] is None else str(row[14]),
        )
    except (TypeError, ValueError) as error:
        raise WorktreeError(
            "managed worktree record is malformed", kind=WorktreeFailureKind.PROTOCOL
        ) from error


__all__ = ["SCHEMA_VERSION", "SqliteManagedWorktreeStore"]
