"""SQLite persistence subagents owner.

This module owns one cohesive persistence responsibility.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from neuro_code.application.ports.parent_context_relay import ParentContextRelayError
from neuro_code.application.ports.writable_subagent import WritableSubagentLeaseError
from neuro_code.domain.checkpoints import CheckpointId
from neuro_code.domain.parent_context_relay import ParentContextRelay, ParentContextRelayItem
from neuro_code.domain.session_tasks import SessionTaskKind, SessionTaskStatus, SubagentLink
from neuro_code.domain.worktree import WorktreeHandle, WorktreeId, WorktreeRepositoryIdentity
from neuro_code.domain.writable_subagent import (
    WritableSubagentLeaseScope,
    WritableSubagentWorkspaceLease,
    WritableSubagentWorkspaceState,
)
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
)
from neuro_code.infrastructure.persistence.sqlite_session_plans import _validated_session_task_id
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import SessionError


class SubagentsMixin(_SqliteSessionPersistenceContext):
    """Mixin owning this SQLite persistence slice."""

    async def save_subagent_link(self, link: SubagentLink) -> None:
        if not isinstance(link, SubagentLink):
            raise TypeError("subagent link must be a SubagentLink")

        def save() -> None:
            try:
                with closing(self._connect()) as connection, connection:
                    parent = connection.execute(
                        """
                        SELECT kind, status
                        FROM session_tasks
                        WHERE session_id = ? AND task_id = ?
                        """,
                        (link.parent_session_id, link.parent_task_id),
                    ).fetchone()
                    if parent is None:
                        raise SessionError(f"unknown parent subagent task: {link.parent_task_id}")
                    if parent[0] != SessionTaskKind.SUBAGENT.value:
                        raise SessionError("subagent link parent task must have subagent kind")
                    if parent[1] != SessionTaskStatus.RUNNING.value:
                        raise SessionError("subagent link parent task must be running")
                    child = connection.execute(
                        "SELECT 1 FROM sessions WHERE id = ?",
                        (link.child_session_id,),
                    ).fetchone()
                    if child is None:
                        raise SessionError(
                            f"unknown child subagent session: {link.child_session_id}"
                        )
                    connection.execute(
                        """
                        INSERT INTO subagent_links(
                            parent_session_id, parent_task_id, child_session_id, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            link.parent_session_id,
                            link.parent_task_id,
                            link.child_session_id,
                            link.created_at.isoformat(),
                        ),
                    )
                    connection.execute(
                        "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (link.parent_session_id,),
                    )
            except sqlite3.IntegrityError as error:
                raise SessionError(
                    f"subagent link already exists for task: {link.parent_task_id}"
                ) from error

        async with self._write_lock:
            await run_blocking(save)

    async def load_subagent_link(
        self,
        parent_session_id: str,
        parent_task_id: str,
    ) -> SubagentLink | None:
        _validated_session_task_id(parent_task_id)

        def load() -> SubagentLink | None:
            with closing(self._connect()) as connection:
                session = connection.execute(
                    "SELECT 1 FROM sessions WHERE id = ?",
                    (parent_session_id,),
                ).fetchone()
                if session is None:
                    raise SessionError(f"unknown session: {parent_session_id}")
                row = connection.execute(
                    """
                    SELECT parent_session_id, parent_task_id, child_session_id, created_at
                    FROM subagent_links
                    WHERE parent_session_id = ? AND parent_task_id = ?
                    """,
                    (parent_session_id, parent_task_id),
                ).fetchone()
            return _subagent_link_from_row(row) if row is not None else None

        return await run_blocking(load)

    async def list_subagent_links(
        self,
        parent_session_id: str,
        *,
        limit: int = 50,
    ) -> list[SubagentLink]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("subagent link limit must be between 1 and 1000")

        def load() -> list[SubagentLink]:
            with closing(self._connect()) as connection:
                session = connection.execute(
                    "SELECT 1 FROM sessions WHERE id = ?",
                    (parent_session_id,),
                ).fetchone()
                if session is None:
                    raise SessionError(f"unknown session: {parent_session_id}")
                rows = connection.execute(
                    """
                    SELECT parent_session_id, parent_task_id, child_session_id, created_at
                    FROM subagent_links
                    WHERE parent_session_id = ?
                    ORDER BY created_at DESC, parent_task_id DESC
                    LIMIT ?
                    """,
                    (parent_session_id, limit),
                ).fetchall()
            return [_subagent_link_from_row(row) for row in rows]

        return await run_blocking(load)

    async def insert_writable_subagent_lease(
        self,
        lease: WritableSubagentWorkspaceLease,
    ) -> WritableSubagentWorkspaceLease:
        if not isinstance(lease, WritableSubagentWorkspaceLease):
            raise TypeError("writable subagent lease must be canonical")
        if lease.state is not WritableSubagentWorkspaceState.ALLOCATING or lease.version != 0:
            raise ValueError("new writable subagent lease must start allocating at version zero")

        def insert() -> WritableSubagentWorkspaceLease:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                connection.execute("BEGIN IMMEDIATE")
                if lease.execution_scope is WritableSubagentLeaseScope.STANDALONE:
                    conflict = connection.execute(
                        """
                        SELECT 1 FROM writable_subagent_leases
                        WHERE parent_session_id = ?
                          AND state IN ('allocating', 'worktree_ready', 'baseline_ready', 'active')
                        LIMIT 1
                        """,
                        (lease.parent_session_id,),
                    ).fetchone()
                else:
                    conflict = connection.execute(
                        """
                        SELECT 1 FROM writable_subagent_leases
                        WHERE parent_session_id = ?
                          AND state IN ('allocating', 'worktree_ready', 'baseline_ready', 'active')
                          AND execution_scope = 'standalone'
                        LIMIT 1
                        """,
                        (lease.parent_session_id,),
                    ).fetchone()
                if conflict is not None:
                    raise WritableSubagentLeaseError(
                        "another writable subagent already owns the parent",
                        kind="concurrent_modification",
                    )
                connection.execute(
                    """
                    INSERT INTO writable_subagent_leases(
                        lease_id, parent_session_id, parent_task_id, worktree_id,
                        parent_capability_fingerprint, parent_workspace_root,
                        parent_common_dir, parent_source_worktree, parent_git_dir,
                        parent_repository_head_sha, base_commit_sha, canonical_child_root,
                        state, created_at, updated_at, worktree_common_dir,
                        worktree_source_worktree, worktree_git_dir, worktree_repository_head_sha,
                        worktree_path, worktree_branch, baseline_checkpoint_id,
                        child_session_id, capability_fingerprint, grant_fingerprint,
                        owner_pid, owner_token, final_workspace_fingerprint,
                        workspace_changed, changed_file_count, error_kind, execution_scope,
                        version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _writable_lease_values(lease),
                )
                connection.commit()
                return lease
            except WritableSubagentLeaseError:
                if connection is not None:
                    connection.rollback()
                raise
            except sqlite3.IntegrityError as error:
                if connection is not None:
                    connection.rollback()
                raise WritableSubagentLeaseError(
                    "another writable subagent already owns the parent or worktree",
                    kind="concurrent_modification",
                ) from error
            except sqlite3.Error as error:
                if connection is not None:
                    connection.rollback()
                raise WritableSubagentLeaseError(
                    "writable subagent lease could not be persisted",
                ) from error
            finally:
                if connection is not None:
                    connection.close()

        async with self._write_lock:
            return await run_blocking(insert)

    async def get_writable_subagent_lease(
        self,
        lease_id: str,
    ) -> WritableSubagentWorkspaceLease | None:
        _validated_session_task_id(lease_id)

        def load() -> WritableSubagentWorkspaceLease | None:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    _WRITABLE_LEASE_SELECT + " WHERE lease_id = ?",
                    (lease_id,),
                ).fetchone()
            return _writable_lease_from_row(row) if row is not None else None

        return await run_blocking(load)

    async def get_writable_subagent_lease_for_parent_task(
        self,
        parent_session_id: str,
        parent_task_id: str,
    ) -> WritableSubagentWorkspaceLease | None:
        _validated_session_task_id(parent_session_id)
        _validated_session_task_id(parent_task_id)

        def load() -> WritableSubagentWorkspaceLease | None:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    _WRITABLE_LEASE_SELECT + " WHERE parent_session_id = ? AND parent_task_id = ?",
                    (parent_session_id, parent_task_id),
                ).fetchone()
            return _writable_lease_from_row(row) if row is not None else None

        return await run_blocking(load)

    async def insert_parent_context_relay(
        self,
        relay: ParentContextRelay,
    ) -> ParentContextRelay:
        """Publish one immutable READY relay after verifying exact lease linkage."""

        if not isinstance(relay, ParentContextRelay):
            raise TypeError("parent context relay must be canonical")

        def insert() -> ParentContextRelay:
            try:
                with closing(self._connect()) as connection, connection:
                    lease = connection.execute(
                        """
                        SELECT parent_session_id, parent_task_id, child_session_id,
                               worktree_id, baseline_checkpoint_id, base_commit_sha,
                               capability_fingerprint, grant_fingerprint
                        FROM writable_subagent_leases
                        WHERE lease_id = ?
                        """,
                        (relay.lease_id,),
                    ).fetchone()
                    expected = (
                        relay.parent_session_id,
                        relay.parent_task_id,
                        relay.child_session_id,
                        relay.worktree_id.value,
                        relay.baseline_checkpoint_id.value,
                        relay.base_commit_sha,
                        relay.capability_fingerprint,
                        relay.grant_fingerprint,
                    )
                    if lease is None or tuple(lease) != expected:
                        raise ParentContextRelayError(
                            "parent context relay does not match its writable lease",
                            kind="protocol",
                        )
                    link = connection.execute(
                        """
                        SELECT child_session_id
                        FROM subagent_links
                        WHERE parent_session_id = ? AND parent_task_id = ?
                        """,
                        (relay.parent_session_id, relay.parent_task_id),
                    ).fetchone()
                    if link is None or str(link[0]) != relay.child_session_id:
                        raise ParentContextRelayError(
                            "parent context relay does not match its subagent link",
                            kind="protocol",
                        )
                    values = _parent_context_relay_values(relay)
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO parent_context_relays(
                            relay_id, lease_id, parent_session_id, parent_task_id,
                            child_session_id, worktree_id, baseline_checkpoint_id,
                            base_commit_sha, capability_fingerprint, grant_fingerprint,
                            task_prompt_fingerprint, source_item_count, items_json,
                            source_fingerprint, content_fingerprint, byte_count,
                            truncated, created_at, integrity_fingerprint, state
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready')
                        """,
                        values,
                    )
                    row = connection.execute(
                        _PARENT_CONTEXT_RELAY_SELECT + " WHERE lease_id = ?",
                        (relay.lease_id,),
                    ).fetchone()
                    if row is None:
                        raise ParentContextRelayError(
                            "parent context relay was not persisted",
                        )
                    current = _parent_context_relay_from_row(row)
                    if current != relay:
                        raise ParentContextRelayError(
                            "an immutable parent context relay already exists for this worker",
                            kind="concurrent_modification",
                        )
                    if cursor.rowcount not in {0, 1}:
                        raise ParentContextRelayError("parent context relay insert was ambiguous")
                    return current
            except ParentContextRelayError:
                raise
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ParentContextRelayError(
                    "parent context relay integrity verification failed",
                    kind="integrity",
                ) from error
            except sqlite3.Error as error:
                raise ParentContextRelayError(
                    "parent context relay could not be persisted",
                ) from error

        async with self._write_lock:
            return await run_blocking(insert)

    async def get_parent_context_relay(
        self,
        relay_id: str,
    ) -> ParentContextRelay | None:
        _validated_session_task_id(relay_id)

        def load() -> ParentContextRelay | None:
            try:
                with closing(self._connect()) as connection:
                    row = connection.execute(
                        _PARENT_CONTEXT_RELAY_SELECT + " WHERE relay_id = ?",
                        (relay_id,),
                    ).fetchone()
                return _parent_context_relay_from_row(row) if row is not None else None
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ParentContextRelayError(
                    "parent context relay integrity verification failed",
                    kind="integrity",
                ) from error
            except sqlite3.Error as error:
                raise ParentContextRelayError("parent context relay could not be loaded") from error

        return await run_blocking(load)

    async def get_parent_context_relay_for_lease(
        self,
        lease_id: str,
    ) -> ParentContextRelay | None:
        _validated_session_task_id(lease_id)

        def load() -> ParentContextRelay | None:
            try:
                with closing(self._connect()) as connection:
                    row = connection.execute(
                        _PARENT_CONTEXT_RELAY_SELECT + " WHERE lease_id = ?",
                        (lease_id,),
                    ).fetchone()
                return _parent_context_relay_from_row(row) if row is not None else None
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ParentContextRelayError(
                    "parent context relay integrity verification failed",
                    kind="integrity",
                ) from error
            except sqlite3.Error as error:
                raise ParentContextRelayError("parent context relay could not be loaded") from error

        return await run_blocking(load)

    async def list_writable_subagent_leases(
        self,
        *,
        parent_session_id: str | None = None,
        include_terminal: bool = True,
    ) -> tuple[WritableSubagentWorkspaceLease, ...]:
        if parent_session_id is not None:
            _validated_session_task_id(parent_session_id)
        if not isinstance(include_terminal, bool):
            raise TypeError("include_terminal must be boolean")

        def load() -> tuple[WritableSubagentWorkspaceLease, ...]:
            clauses: list[str] = []
            params: list[object] = []
            if parent_session_id is not None:
                clauses.append("parent_session_id = ?")
                params.append(parent_session_id)
            if not include_terminal:
                clauses.append("state IN (?, ?, ?, ?)")
                params.extend(
                    state.value
                    for state in (
                        WritableSubagentWorkspaceState.ALLOCATING,
                        WritableSubagentWorkspaceState.WORKTREE_READY,
                        WritableSubagentWorkspaceState.BASELINE_READY,
                        WritableSubagentWorkspaceState.ACTIVE,
                    )
                )
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    _WRITABLE_LEASE_SELECT + where + " ORDER BY created_at ASC, lease_id ASC",
                    params,
                ).fetchall()
            return tuple(_writable_lease_from_row(row) for row in rows)

        return await run_blocking(load)

    async def compare_and_transition_writable_subagent_lease(
        self,
        lease: WritableSubagentWorkspaceLease,
        *,
        expected_version: int,
        expected_state: WritableSubagentWorkspaceState,
    ) -> WritableSubagentWorkspaceLease:
        if not isinstance(lease, WritableSubagentWorkspaceLease):
            raise TypeError("writable subagent lease must be canonical")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise TypeError("writable lease expected version must be an integer")
        if expected_version < 0 or lease.version != expected_version:
            raise WritableSubagentLeaseError(
                "writable subagent lease version does not match the CAS claim",
                kind="concurrent_modification",
            )
        if not isinstance(expected_state, WritableSubagentWorkspaceState):
            raise TypeError("writable lease expected state must be canonical")

        def transition() -> WritableSubagentWorkspaceLease:
            try:
                with closing(self._connect()) as connection, connection:
                    current_row = connection.execute(
                        _WRITABLE_LEASE_SELECT + " WHERE lease_id = ?",
                        (lease.lease_id,),
                    ).fetchone()
                    if current_row is None:
                        raise WritableSubagentLeaseError(
                            "writable subagent lease is missing",
                            kind="unmanaged",
                        )
                    current = _writable_lease_from_row(current_row)
                    if not _same_writable_lease_identity(current, lease):
                        raise WritableSubagentLeaseError(
                            "writable subagent lease identity is immutable",
                            kind="protocol",
                        )
                    values = _writable_lease_values(replace(lease, version=expected_version + 1))
                    cursor = connection.execute(
                        """
                        UPDATE writable_subagent_leases SET
                            parent_session_id = ?, parent_task_id = ?, worktree_id = ?,
                            parent_capability_fingerprint = ?, parent_workspace_root = ?,
                            parent_common_dir = ?, parent_source_worktree = ?, parent_git_dir = ?,
                            parent_repository_head_sha = ?, base_commit_sha = ?,
                            canonical_child_root = ?, state = ?, created_at = ?, updated_at = ?,
                            worktree_common_dir = ?, worktree_source_worktree = ?,
                            worktree_git_dir = ?, worktree_repository_head_sha = ?,
                            worktree_path = ?, worktree_branch = ?, baseline_checkpoint_id = ?,
                            child_session_id = ?, capability_fingerprint = ?, grant_fingerprint = ?,
                            owner_pid = ?, owner_token = ?, final_workspace_fingerprint = ?,
                            workspace_changed = ?, changed_file_count = ?, error_kind = ?,
                            execution_scope = ?, version = ?
                        WHERE lease_id = ? AND version = ? AND state = ?
                        """,
                        (
                            *values[1:],
                            values[0],
                            expected_version,
                            expected_state.value,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise WritableSubagentLeaseError(
                            "writable subagent lease was changed by another process",
                            kind="concurrent_modification",
                        )
                return replace(lease, version=expected_version + 1)
            except WritableSubagentLeaseError:
                raise
            except sqlite3.IntegrityError as error:
                raise WritableSubagentLeaseError(
                    "writable subagent lease transition conflicts with another owner",
                    kind="concurrent_modification",
                ) from error
            except sqlite3.Error as error:
                raise WritableSubagentLeaseError(
                    "writable subagent lease transition could not be persisted",
                ) from error

        async with self._write_lock:
            return await run_blocking(transition)


_PARENT_CONTEXT_RELAY_SELECT = """
    SELECT relay_id, lease_id, parent_session_id, parent_task_id,
           child_session_id, worktree_id, baseline_checkpoint_id,
           base_commit_sha, capability_fingerprint, grant_fingerprint,
           task_prompt_fingerprint, source_item_count, items_json,
           source_fingerprint, content_fingerprint, byte_count,
           truncated, created_at, integrity_fingerprint, state
    FROM parent_context_relays
"""


def _parent_context_relay_values(relay: ParentContextRelay) -> tuple[object, ...]:
    return (
        relay.relay_id,
        relay.lease_id,
        relay.parent_session_id,
        relay.parent_task_id,
        relay.child_session_id,
        relay.worktree_id.value,
        relay.baseline_checkpoint_id.value,
        relay.base_commit_sha,
        relay.capability_fingerprint,
        relay.grant_fingerprint,
        relay.task_prompt_fingerprint,
        relay.source_item_count,
        json.dumps(
            [item.to_dict() for item in relay.items],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        relay.source_fingerprint,
        relay.content_fingerprint,
        relay.byte_count,
        int(relay.truncated),
        relay.created_at.isoformat(),
        relay.integrity_fingerprint,
    )


def _parent_context_relay_from_row(row: Sequence[object]) -> ParentContextRelay:
    if len(row) != 20:
        raise ValueError("parent context relay record is malformed")
    (
        relay_id,
        lease_id,
        parent_session_id,
        parent_task_id,
        child_session_id,
        worktree_id,
        baseline_checkpoint_id,
        base_commit_sha,
        capability_fingerprint,
        grant_fingerprint,
        task_prompt_fingerprint,
        source_item_count,
        raw_items,
        source_fingerprint,
        content_fingerprint,
        byte_count,
        raw_truncated,
        created_at,
        integrity_fingerprint,
        state,
    ) = row
    if state != "ready":
        raise ValueError("parent context relay is not READY")
    if not isinstance(source_item_count, int) or isinstance(source_item_count, bool):
        raise ValueError("parent context relay source item count is invalid")
    if not isinstance(byte_count, int) or isinstance(byte_count, bool):
        raise ValueError("parent context relay byte count is invalid")
    if raw_truncated not in (0, 1) or isinstance(raw_truncated, bool):
        raise ValueError("parent context relay truncated flag is invalid")
    payload = json.loads(str(raw_items))
    if not isinstance(payload, list):
        raise ValueError("parent context relay item payload is invalid")
    relay = ParentContextRelay(
        relay_id=str(relay_id),
        parent_session_id=str(parent_session_id),
        parent_task_id=str(parent_task_id),
        child_session_id=str(child_session_id),
        lease_id=str(lease_id),
        worktree_id=WorktreeId(str(worktree_id)),
        baseline_checkpoint_id=CheckpointId(str(baseline_checkpoint_id)),
        base_commit_sha=str(base_commit_sha),
        capability_fingerprint=str(capability_fingerprint),
        grant_fingerprint=str(grant_fingerprint),
        task_prompt_fingerprint=str(task_prompt_fingerprint),
        source_item_count=source_item_count,
        items=tuple(ParentContextRelayItem.from_dict(item) for item in payload),
        source_fingerprint=str(source_fingerprint),
        content_fingerprint=str(content_fingerprint),
        byte_count=byte_count,
        truncated=bool(raw_truncated),
        created_at=datetime.fromisoformat(str(created_at)),
    )
    if not isinstance(integrity_fingerprint, str) or (
        relay.integrity_fingerprint != integrity_fingerprint
    ):
        raise ValueError("parent context relay integrity fingerprint is inconsistent")
    return relay


_WRITABLE_LEASE_SELECT = """
    SELECT lease_id, parent_session_id, parent_task_id, worktree_id,
           parent_capability_fingerprint, parent_workspace_root,
           parent_common_dir, parent_source_worktree, parent_git_dir,
           parent_repository_head_sha, base_commit_sha, canonical_child_root,
           state, created_at, updated_at, worktree_common_dir,
           worktree_source_worktree, worktree_git_dir, worktree_repository_head_sha,
           worktree_path, worktree_branch, baseline_checkpoint_id,
           child_session_id, capability_fingerprint, grant_fingerprint,
           owner_pid, owner_token, final_workspace_fingerprint,
           workspace_changed, changed_file_count, error_kind, execution_scope, version
    FROM writable_subagent_leases
"""


def _writable_lease_values(lease: WritableSubagentWorkspaceLease) -> tuple[object, ...]:
    worktree = lease.worktree
    repository = worktree.repository if worktree is not None else None
    return (
        lease.lease_id,
        lease.parent_session_id,
        lease.parent_task_id,
        lease.worktree_id.value,
        lease.parent_capability_fingerprint,
        str(lease.parent_workspace_root),
        str(lease.parent_repository.common_dir),
        str(lease.parent_repository.source_worktree),
        str(lease.parent_repository.git_dir),
        lease.parent_repository.head_sha,
        lease.base_commit_sha,
        str(lease.canonical_child_root),
        lease.state.value,
        lease.created_at.isoformat(),
        lease.updated_at.isoformat(),
        str(repository.common_dir) if repository is not None else None,
        str(repository.source_worktree) if repository is not None else None,
        str(repository.git_dir) if repository is not None else None,
        repository.head_sha if repository is not None else None,
        str(worktree.path) if worktree is not None else None,
        worktree.branch if worktree is not None else None,
        lease.baseline_checkpoint_id.value if lease.baseline_checkpoint_id is not None else None,
        lease.child_session_id,
        lease.capability_fingerprint,
        lease.grant_fingerprint,
        lease.owner_pid,
        lease.owner_token,
        lease.final_workspace_fingerprint,
        int(lease.workspace_changed) if lease.workspace_changed is not None else None,
        lease.changed_file_count,
        lease.error_kind,
        lease.execution_scope.value,
        lease.version,
    )


def _writable_lease_from_row(
    row: Sequence[object] | None,
) -> WritableSubagentWorkspaceLease:
    if row is None or len(row) != 33:
        raise SessionError("writable subagent lease record is malformed")
    try:
        (
            lease_id,
            parent_session_id,
            parent_task_id,
            raw_worktree_id,
            parent_capability_fingerprint,
            parent_workspace_root,
            parent_common_dir,
            parent_source_worktree,
            parent_git_dir,
            parent_repository_head_sha,
            base_commit_sha,
            canonical_child_root,
            raw_state,
            raw_created_at,
            raw_updated_at,
            worktree_common_dir,
            worktree_source_worktree,
            worktree_git_dir,
            worktree_repository_head_sha,
            worktree_path,
            worktree_branch,
            raw_checkpoint_id,
            child_session_id,
            capability_fingerprint,
            grant_fingerprint,
            raw_owner_pid,
            owner_token,
            final_workspace_fingerprint,
            raw_workspace_changed,
            raw_changed_file_count,
            error_kind,
            raw_execution_scope,
            raw_version,
        ) = row
        parent_repository = WorktreeRepositoryIdentity(
            common_dir=Path(str(parent_common_dir)),
            source_worktree=Path(str(parent_source_worktree)),
            git_dir=Path(str(parent_git_dir)),
            head_sha=str(parent_repository_head_sha),
        )
        worktree: WorktreeHandle | None = None
        worktree_fields = (
            worktree_common_dir,
            worktree_source_worktree,
            worktree_git_dir,
            worktree_repository_head_sha,
            worktree_path,
        )
        if any(value is not None for value in worktree_fields):
            if any(value is None for value in worktree_fields):
                raise ValueError("writable lease worktree handle is incomplete")
            worktree_repository = WorktreeRepositoryIdentity(
                common_dir=Path(str(worktree_common_dir)),
                source_worktree=Path(str(worktree_source_worktree)),
                git_dir=Path(str(worktree_git_dir)),
                head_sha=str(worktree_repository_head_sha),
            )
            worktree = WorktreeHandle(
                worktree_id=WorktreeId(str(raw_worktree_id)),
                repository=worktree_repository,
                path=Path(str(worktree_path)),
                base_commit_sha=str(base_commit_sha),
                branch=None if worktree_branch is None else str(worktree_branch),
            )
        if raw_workspace_changed is not None and raw_workspace_changed not in (0, 1):
            raise ValueError("writable lease changed flag is invalid")
        owner_pid: int | None
        if raw_owner_pid is None:
            owner_pid = None
        elif isinstance(raw_owner_pid, int) and not isinstance(raw_owner_pid, bool):
            owner_pid = raw_owner_pid
        else:
            raise ValueError("writable lease owner pid is invalid")
        changed_file_count: int | None
        if raw_changed_file_count is None:
            changed_file_count = None
        elif isinstance(raw_changed_file_count, int) and not isinstance(
            raw_changed_file_count, bool
        ):
            changed_file_count = raw_changed_file_count
        else:
            raise ValueError("writable lease changed file count is invalid")
        if not isinstance(raw_version, int) or isinstance(raw_version, bool):
            raise ValueError("writable lease version is invalid")
        return WritableSubagentWorkspaceLease(
            lease_id=str(lease_id),
            parent_session_id=str(parent_session_id),
            parent_task_id=str(parent_task_id),
            worktree_id=WorktreeId(str(raw_worktree_id)),
            parent_capability_fingerprint=str(parent_capability_fingerprint),
            parent_workspace_root=Path(str(parent_workspace_root)),
            parent_repository=parent_repository,
            base_commit_sha=str(base_commit_sha),
            canonical_child_root=Path(str(canonical_child_root)),
            state=WritableSubagentWorkspaceState(str(raw_state)),
            created_at=datetime.fromisoformat(str(raw_created_at)),
            updated_at=datetime.fromisoformat(str(raw_updated_at)),
            worktree=worktree,
            baseline_checkpoint_id=(
                CheckpointId(str(raw_checkpoint_id)) if raw_checkpoint_id is not None else None
            ),
            child_session_id=str(child_session_id) if child_session_id is not None else None,
            capability_fingerprint=(
                str(capability_fingerprint) if capability_fingerprint is not None else None
            ),
            grant_fingerprint=str(grant_fingerprint) if grant_fingerprint is not None else None,
            owner_pid=owner_pid,
            owner_token=str(owner_token),
            final_workspace_fingerprint=(
                str(final_workspace_fingerprint)
                if final_workspace_fingerprint is not None
                else None
            ),
            workspace_changed=(
                bool(raw_workspace_changed) if raw_workspace_changed is not None else None
            ),
            changed_file_count=changed_file_count,
            error_kind=str(error_kind) if error_kind is not None else None,
            execution_scope=WritableSubagentLeaseScope(str(raw_execution_scope)),
            version=raw_version,
        )
    except (TypeError, ValueError, OverflowError) as error:
        raise SessionError("writable subagent lease contains invalid data") from error


def _same_writable_lease_identity(
    current: WritableSubagentWorkspaceLease,
    proposed: WritableSubagentWorkspaceLease,
) -> bool:
    if (
        current.lease_id != proposed.lease_id
        or current.parent_session_id != proposed.parent_session_id
        or current.parent_task_id != proposed.parent_task_id
        or current.worktree_id != proposed.worktree_id
        or current.parent_capability_fingerprint != proposed.parent_capability_fingerprint
        or current.parent_workspace_root != proposed.parent_workspace_root
        or current.parent_repository != proposed.parent_repository
        or current.base_commit_sha != proposed.base_commit_sha
        or current.canonical_child_root != proposed.canonical_child_root
        or current.execution_scope is not proposed.execution_scope
    ):
        return False
    for current_value, proposed_value in (
        (current.worktree, proposed.worktree),
        (current.baseline_checkpoint_id, proposed.baseline_checkpoint_id),
        (current.child_session_id, proposed.child_session_id),
        (current.capability_fingerprint, proposed.capability_fingerprint),
        (current.grant_fingerprint, proposed.grant_fingerprint),
    ):
        if current_value is not None and current_value != proposed_value:
            return False
    return True


def _subagent_link_from_row(row: Sequence[object]) -> SubagentLink:
    try:
        parent_session_id, parent_task_id, child_session_id, raw_created_at = row
        return SubagentLink(
            str(parent_session_id),
            str(parent_task_id),
            str(child_session_id),
            datetime.fromisoformat(str(raw_created_at)),
        )
    except (TypeError, ValueError) as error:
        raise SessionError("subagent link contains invalid data") from error
