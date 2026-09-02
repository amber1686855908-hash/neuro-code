"""SQLite persistence core owner.

This module owns one cohesive persistence responsibility.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

from neuro_code.domain.background_tasks.models import BackgroundWakeState
from neuro_code.domain.conversation.events import AgentEvent
from neuro_code.domain.conversation.messages import (
    ContentPart,
    ContentPartKind,
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    SessionItem,
    ToolCall,
)
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.sessions import SessionSnapshot, SessionSummary, normalize_session_title
from neuro_code.domain.sessions.search import (
    SessionSearchHit,
    SessionSearchPage,
    fallback_session_title,
    searchable_session_text,
)
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
)
from neuro_code.infrastructure.persistence.sqlite_session_constants import (
    _SEARCH_SNIPPET_LIMIT,
    _SESSION_ALIAS_ID_LIMIT,
    _SESSION_ALIAS_NAMESPACE_LIMIT,
    SCHEMA_VERSION,
)
from neuro_code.infrastructure.persistence.sqlite_session_schema import (
    _ensure_agent_swarm_schema,
    _ensure_base_schema,
    _ensure_leader_schema,
    _ensure_model_planning_schema,
    _ensure_parent_context_relay_schema,
    _ensure_result_adoption_schema,
    _ensure_search_schema,
    _ensure_session_alias_schema,
    _ensure_session_background_wake_schema,
    _ensure_session_compaction_schema,
    _ensure_session_execution_record_schema,
    _ensure_session_plan_comment_schema,
    _ensure_session_plan_schema,
    _ensure_session_task_schema,
    _ensure_session_turn_attempt_schema,
    _ensure_subagent_link_schema,
    _ensure_task_dag_dependency_result_relay_schema,
    _ensure_task_dag_recovery_claim_schema,
    _ensure_task_dag_replan_schema,
    _ensure_task_dag_schema,
    _ensure_ultracode_schema,
    _ensure_writable_subagent_lease_schema,
    _migrate_agent_swarm_schema,
    _migrate_leader_parallel_decision_schema,
    _migrate_model_planning_schema,
    _migrate_result_adoption_schema,
    _migrate_task_dag_execution_owner_schema,
    _migrate_task_dag_parallelism_schema,
    _migrate_task_dag_replan_schema,
    _migrate_ultracode_schema,
    _migrate_writable_subagent_lease_schema,
)
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import SessionError


class CoreMixin(_SqliteSessionPersistenceContext):
    """Mixin owning this SQLite persistence slice."""

    async def initialize(self) -> None:
        def initialize_sync() -> None:
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection:
                try:
                    # Serialise schema inspection and migration across store instances and
                    # processes. ``executescript`` is deliberately avoided because it
                    # commits an open transaction before executing its script.
                    connection.execute("BEGIN IMMEDIATE")
                    _ensure_base_schema(connection)
                    version = connection.execute(
                        "SELECT version FROM schema_meta WHERE singleton = 1"
                    ).fetchone()
                    if version is not None and version[0] == 1:
                        columns = {
                            str(row[1])
                            for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
                        }
                        if "context_affinity" not in columns:
                            connection.execute(
                                "ALTER TABLE sessions ADD COLUMN context_affinity TEXT"
                            )
                        connection.execute("UPDATE schema_meta SET version = 2 WHERE singleton = 1")
                        version = (2,)
                    if version is not None and version[0] == 2:
                        columns = {
                            str(row[1])
                            for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
                        }
                        if "sandbox_profile" not in columns:
                            connection.execute(
                                "ALTER TABLE sessions ADD COLUMN sandbox_profile TEXT"
                            )
                        connection.execute("UPDATE schema_meta SET version = 3 WHERE singleton = 1")
                        version = (3,)
                    if version is not None and version[0] == 3:
                        columns = {
                            str(row[1])
                            for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
                        }
                        if "title" not in columns:
                            connection.execute(
                                "ALTER TABLE sessions ADD COLUMN title TEXT NOT NULL DEFAULT ''"
                            )
                        _ensure_search_schema(connection)
                        _backfill_search_documents(connection)
                        connection.execute("UPDATE schema_meta SET version = 4 WHERE singleton = 1")
                        version = (4,)
                    if version is not None and version[0] == 4:
                        _ensure_session_alias_schema(connection)
                        connection.execute("UPDATE schema_meta SET version = 5 WHERE singleton = 1")
                        version = (5,)
                    if version is not None and version[0] == 5:
                        _ensure_session_plan_schema(connection)
                        connection.execute("UPDATE schema_meta SET version = 6 WHERE singleton = 1")
                        version = (6,)
                    if version is not None and version[0] == 6:
                        _ensure_session_task_schema(connection)
                        connection.execute("UPDATE schema_meta SET version = 7 WHERE singleton = 1")
                        version = (7,)
                    if version is not None and version[0] == 7:
                        _ensure_session_plan_comment_schema(connection)
                        connection.execute("UPDATE schema_meta SET version = 8 WHERE singleton = 1")
                        version = (8,)
                    if version is not None and version[0] == 8:
                        _ensure_session_task_schema(connection)
                        connection.execute("UPDATE schema_meta SET version = 9 WHERE singleton = 1")
                        version = (9,)
                    if version is not None and version[0] == 9:
                        _ensure_session_execution_record_schema(connection)
                        connection.execute(
                            "UPDATE schema_meta SET version = 10 WHERE singleton = 1"
                        )
                        version = (10,)
                    if version is not None and version[0] == 10:
                        _ensure_session_background_wake_schema(connection)
                        connection.execute(
                            "UPDATE schema_meta SET version = 11 WHERE singleton = 1"
                        )
                        version = (11,)
                    if version is not None and version[0] == 11:
                        _ensure_subagent_link_schema(connection)
                        connection.execute(
                            "UPDATE schema_meta SET version = 12 WHERE singleton = 1"
                        )
                        version = (12,)
                    if version is not None and version[0] == 12:
                        _ensure_session_compaction_schema(connection)
                        connection.execute(
                            "UPDATE schema_meta SET version = 13 WHERE singleton = 1"
                        )
                        version = (13,)
                    if version is not None and version[0] == 13:
                        _ensure_session_turn_attempt_schema(connection)
                        connection.execute(
                            "UPDATE schema_meta SET version = 14 WHERE singleton = 1"
                        )
                        version = (14,)
                    if version is not None and version[0] == 14:
                        _ensure_writable_subagent_lease_schema(connection)
                        connection.execute(
                            "UPDATE schema_meta SET version = 15 WHERE singleton = 1"
                        )
                        version = (15,)
                    if version is not None and version[0] == 15:
                        _migrate_writable_subagent_lease_schema(connection)
                        connection.execute(
                            "UPDATE schema_meta SET version = 16 WHERE singleton = 1"
                        )
                        version = (16,)
                    if version is not None and version[0] == 16:
                        _ensure_parent_context_relay_schema(connection)
                        connection.execute(
                            "UPDATE schema_meta SET version = 17 WHERE singleton = 1"
                        )
                        version = (17,)
                    if version is not None and version[0] == 17:
                        _ensure_task_dag_schema(connection)
                        connection.execute(
                            "UPDATE schema_meta SET version = 18 WHERE singleton = 1"
                        )
                        version = (18,)
                    if version is not None and version[0] == 18:
                        _ensure_leader_schema(connection)
                        connection.execute(
                            "UPDATE schema_meta SET version = 19 WHERE singleton = 1"
                        )
                        version = (19,)
                    if version is not None and version[0] == 19:
                        _ensure_task_dag_dependency_result_relay_schema(connection)
                        connection.execute(
                            "UPDATE schema_meta SET version = 20 WHERE singleton = 1"
                        )
                        version = (20,)
                    if version is not None and version[0] == 20:
                        _ensure_task_dag_recovery_claim_schema(connection)
                        connection.execute(
                            "UPDATE schema_meta SET version = 21 WHERE singleton = 1"
                        )
                        version = (21,)
                    if version is not None and version[0] == 21:
                        _migrate_task_dag_parallelism_schema(connection)
                        connection.execute(
                            "UPDATE schema_meta SET version = 22 WHERE singleton = 1"
                        )
                        version = (22,)
                    if version is not None and version[0] == 22:
                        _migrate_task_dag_execution_owner_schema(connection)
                        connection.execute(
                            "UPDATE schema_meta SET version = 23 WHERE singleton = 1"
                        )
                        version = (23,)
                    if version is not None and version[0] == 23:
                        _migrate_leader_parallel_decision_schema(connection)
                        connection.execute(
                            "UPDATE schema_meta SET version = 24 WHERE singleton = 1"
                        )
                        version = (24,)
                    if version is not None and version[0] == 24:
                        _migrate_model_planning_schema(connection)
                        connection.execute(
                            "UPDATE schema_meta SET version = 25 WHERE singleton = 1"
                        )
                        version = (25,)
                    if version is not None and version[0] == 25:
                        _migrate_task_dag_replan_schema(connection)
                        connection.execute(
                            "UPDATE schema_meta SET version = 26 WHERE singleton = 1"
                        )
                        version = (26,)
                    if version is not None and version[0] == 26:
                        _migrate_agent_swarm_schema(connection)
                        connection.execute(
                            "UPDATE schema_meta SET version = 27 WHERE singleton = 1"
                        )
                        version = (27,)
                    if version is not None and version[0] == 27:
                        _migrate_ultracode_schema(connection)
                        connection.execute(
                            "UPDATE schema_meta SET version = 28 WHERE singleton = 1"
                        )
                        version = (28,)
                    if version is not None and version[0] == 28:
                        _migrate_result_adoption_schema(connection)
                        connection.execute(
                            "UPDATE schema_meta SET version = 29 WHERE singleton = 1"
                        )
                        version = (29,)
                    if version is None or version[0] != SCHEMA_VERSION:
                        raise SessionError(
                            "unsupported session schema version: "
                            f"{version[0] if version else 'missing'}"
                        )
                    _ensure_search_schema(connection)
                    _ensure_session_alias_schema(connection)
                    _ensure_session_plan_schema(connection)
                    _ensure_session_task_schema(connection)
                    _ensure_session_plan_comment_schema(connection)
                    _ensure_session_execution_record_schema(connection)
                    _ensure_session_background_wake_schema(connection)
                    _ensure_subagent_link_schema(connection)
                    _ensure_session_compaction_schema(connection)
                    _ensure_session_turn_attempt_schema(connection)
                    _ensure_writable_subagent_lease_schema(connection)
                    _ensure_parent_context_relay_schema(connection)
                    _ensure_task_dag_schema(connection)
                    _ensure_leader_schema(connection)
                    _ensure_task_dag_dependency_result_relay_schema(connection)
                    _ensure_task_dag_recovery_claim_schema(connection)
                    _ensure_ultracode_schema(connection)
                    _ensure_model_planning_schema(connection)
                    _ensure_task_dag_replan_schema(connection)
                    _ensure_agent_swarm_schema(connection)
                    _ensure_result_adoption_schema(connection)
                    _backfill_search_documents(connection, missing_only=True)
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise

        await run_blocking(initialize_sync)

    async def create_session(
        self,
        cwd: str,
        provider: str,
        model: str,
        context_affinity: str | None = None,
        sandbox_profile: SandboxProfile = SandboxProfile.OFF,
    ) -> str:
        session_id = str(uuid.uuid4())

        def create() -> None:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    """
                    INSERT INTO sessions(
                        id, cwd, provider, model, context_affinity, sandbox_profile
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        cwd,
                        provider,
                        model,
                        context_affinity,
                        sandbox_profile.value,
                    ),
                )

        async with self._write_lock:
            await run_blocking(create)
        return session_id

    async def import_session(self, snapshot: SessionSnapshot) -> str:
        summary = snapshot.summary
        payload = _serialize_session_items(snapshot.items)
        title = summary.title or fallback_session_title(snapshot.items)
        search_content = searchable_session_text(snapshot.items)

        def import_snapshot() -> None:
            try:
                with closing(self._connect()) as connection, connection:
                    connection.execute(
                        """
                        INSERT INTO sessions(
                            id, cwd, provider, model, created_at, updated_at,
                            messages_json, context_affinity, sandbox_profile, title
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            summary.id,
                            summary.cwd,
                            summary.provider,
                            summary.model,
                            summary.created_at.isoformat(),
                            summary.updated_at.isoformat(),
                            payload,
                            summary.context_affinity,
                            (
                                summary.sandbox_profile.value
                                if summary.sandbox_profile is not None
                                else None
                            ),
                            title,
                        ),
                    )
                    _upsert_search_document(
                        connection,
                        session_id=summary.id,
                        title=title,
                        content=search_content,
                    )
            except sqlite3.IntegrityError as error:
                raise SessionError(f"session already exists: {summary.id}") from error

        async with self._write_lock:
            await run_blocking(import_snapshot)
        return summary.id

    async def delete_session(self, session_id: str) -> None:
        def delete() -> None:
            with closing(self._connect()) as connection, connection:
                pending = [session_id]
                seen: set[str] = set()
                while pending:
                    current = pending.pop()
                    if current in seen:
                        continue
                    seen.add(current)
                    exists = connection.execute(
                        "SELECT 1 FROM sessions WHERE id = ?",
                        (current,),
                    ).fetchone()
                    if exists is None:
                        if current == session_id:
                            raise SessionError(f"unknown session: {session_id}")
                        continue
                    child_rows = connection.execute(
                        """
                        SELECT child_session_id
                        FROM subagent_links
                        WHERE parent_session_id = ?
                        """,
                        (current,),
                    ).fetchall()
                    pending.extend(str(row[0]) for row in child_rows)

                if session_id not in seen:
                    raise SessionError(f"unknown session: {session_id}")
                if seen:
                    placeholders = ", ".join("?" for _ in seen)
                    parameters = tuple(seen)
                    lease = connection.execute(
                        f"""
                        SELECT 1
                        FROM writable_subagent_leases
                        WHERE parent_session_id IN ({placeholders})
                           OR child_session_id IN ({placeholders})
                        LIMIT 1
                        """,
                        (*parameters, *parameters),
                    ).fetchone()
                    if lease is not None:
                        raise SessionError("session has preserved writable workspace resources")

                for current in seen:
                    connection.execute("DELETE FROM sessions WHERE id = ?", (current,))

        async with self._write_lock:
            await run_blocking(delete)

    async def fork_session(self, session_id: str) -> str:
        forked_session_id = str(uuid.uuid4())

        def fork() -> None:
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    """
                    SELECT cwd, provider, model, messages_json, context_affinity,
                           sandbox_profile, title, plan_json
                    FROM sessions
                    WHERE id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise SessionError(f"unknown session: {session_id}")
                try:
                    items = _session_items_from_json(row[3])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    raise SessionError(
                        f"session {session_id} contains invalid session items"
                    ) from error
                title = str(row[6]) or fallback_session_title(items)
                connection.execute(
                    """
                    INSERT INTO sessions(
                        id, cwd, provider, model, messages_json,
                        context_affinity, sandbox_profile, title, plan_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        forked_session_id,
                        row[0],
                        row[1],
                        row[2],
                        row[3],
                        row[4],
                        row[5],
                        title,
                        row[7],
                    ),
                )
                _upsert_search_document(
                    connection,
                    session_id=forked_session_id,
                    title=title,
                    content=searchable_session_text(items),
                )
                comments = connection.execute(
                    """
                    SELECT plan_fingerprint, step_index, content, created_at
                    FROM session_plan_comments
                    WHERE session_id = ?
                    ORDER BY created_at ASC, comment_id ASC
                    """,
                    (session_id,),
                ).fetchall()
                for plan_fingerprint, step_index, content, created_at in comments:
                    connection.execute(
                        """
                        INSERT INTO session_plan_comments(
                            comment_id, session_id, plan_fingerprint,
                            step_index, content, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"plan-comment-{uuid.uuid4().hex}",
                            forked_session_id,
                            plan_fingerprint,
                            step_index,
                            content,
                            created_at,
                        ),
                    )

        async with self._write_lock:
            await run_blocking(fork)
        return forked_session_id

    async def append_event(self, session_id: str, event: AgentEvent) -> None:
        payload = json.dumps(dict(event.data), ensure_ascii=False, separators=(",", ":"))

        def append() -> None:
            try:
                with closing(self._connect()) as connection, connection:
                    connection.execute(
                        """
                        INSERT INTO events(session_id, sequence, kind, created_at, data_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            event.sequence,
                            event.kind.value,
                            event.created_at.isoformat(),
                            payload,
                        ),
                    )
                    connection.execute(
                        "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (session_id,),
                    )
            except sqlite3.IntegrityError as error:
                raise SessionError(
                    f"cannot append event {event.sequence} to session {session_id}"
                ) from error

        async with self._write_lock:
            await run_blocking(append)

    async def update_session_provider(
        self,
        session_id: str,
        provider: str,
        model: str,
        context_affinity: str | None,
    ) -> None:
        def update() -> None:
            with closing(self._connect()) as connection, connection:
                cursor = connection.execute(
                    """
                    UPDATE sessions
                    SET provider = ?, model = ?, context_affinity = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (provider, model, context_affinity, session_id),
                )
                if cursor.rowcount != 1:
                    raise SessionError(f"unknown session: {session_id}")

        async with self._write_lock:
            await run_blocking(update)

    async def update_session_title(
        self,
        session_id: str,
        title: str,
    ) -> SessionSummary:
        try:
            normalized_title = normalize_session_title(title)
        except ValueError as error:
            raise SessionError(str(error)) from error

        def update() -> SessionSummary:
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    "SELECT messages_json FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise SessionError(f"unknown session: {session_id}")
                try:
                    items = _session_items_from_json(row[0])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    raise SessionError(
                        f"session {session_id} contains invalid session items"
                    ) from error
                search_content = searchable_session_text(items)
                connection.execute(
                    """
                    UPDATE sessions
                    SET title = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (normalized_title, session_id),
                )
                _upsert_search_document(
                    connection,
                    session_id=session_id,
                    title=normalized_title,
                    content=search_content,
                )
                summary_row = connection.execute(
                    """
                    SELECT id, cwd, provider, model, created_at, updated_at,
                           context_affinity, sandbox_profile, title
                    FROM sessions WHERE id = ?
                    """,
                    (session_id,),
                ).fetchone()
                assert summary_row is not None
                return _summary_from_row(summary_row)

        async with self._write_lock:
            return await run_blocking(update)

    async def save_messages(self, session_id: str, messages: Sequence[Message]) -> None:
        new_messages = list(messages)

        def save() -> None:
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    "SELECT messages_json, title FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if row is None:
                    raise SessionError(f"unknown session: {session_id}")
                try:
                    current_items = _session_items_from_json(row[0])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    raise SessionError(f"session {session_id} contains invalid messages") from error

                preserved_context = any(
                    isinstance(item, PreservedContextItem) for item in current_items
                )
                if preserved_context:
                    current_messages = [item for item in current_items if isinstance(item, Message)]
                    if (
                        len(new_messages) < len(current_messages)
                        or new_messages[: len(current_messages)] != current_messages
                    ):
                        raise SessionError(
                            "cannot rewrite the imported prefix of a session with "
                            "preserved context items"
                        )
                    items: list[SessionItem] = [
                        *current_items,
                        *new_messages[len(current_messages) :],
                    ]
                else:
                    items = list(new_messages)
                payload = _serialize_session_items(items)
                title = str(row[1]) or fallback_session_title(items)
                cursor = connection.execute(
                    """
                    UPDATE sessions
                    SET messages_json = ?, title = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (payload, title, session_id),
                )
                if cursor.rowcount != 1:
                    raise SessionError(f"unknown session: {session_id}")
                _upsert_search_document(
                    connection,
                    session_id=session_id,
                    title=title,
                    content=searchable_session_text(items),
                )

        async with self._write_lock:
            await run_blocking(save)

    async def save_session_items(
        self,
        session_id: str,
        items: Sequence[SessionItem],
    ) -> None:
        new_items = list(items)

        def save() -> None:
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    "SELECT messages_json, title FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if row is None:
                    raise SessionError(f"unknown session: {session_id}")
                try:
                    current_items = _session_items_from_json(row[0])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    raise SessionError(
                        f"session {session_id} contains invalid session items"
                    ) from error
                if (
                    len(new_items) < len(current_items)
                    or new_items[: len(current_items)] != current_items
                ):
                    raise SessionError("cannot rewrite the persisted session item prefix")
                payload = _serialize_session_items(new_items)
                title = str(row[1]) or fallback_session_title(new_items)
                cursor = connection.execute(
                    """
                    UPDATE sessions
                    SET messages_json = ?, title = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (payload, title, session_id),
                )
                if cursor.rowcount != 1:
                    raise SessionError(f"unknown session: {session_id}")
                _upsert_search_document(
                    connection,
                    session_id=session_id,
                    title=title,
                    content=searchable_session_text(new_items),
                )

        async with self._write_lock:
            await run_blocking(save)

    async def load_messages(self, session_id: str) -> list[Message]:
        items = await self.load_session_items(session_id)
        return [item for item in items if isinstance(item, Message)]

    async def save_background_wake_state(
        self,
        session_id: str,
        state: BackgroundWakeState,
    ) -> None:
        if not isinstance(state, BackgroundWakeState):
            raise TypeError("state must be a BackgroundWakeState")

        def save() -> None:
            with closing(self._connect()) as connection, connection:
                exists = connection.execute(
                    "SELECT 1 FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if exists is None:
                    raise SessionError(f"unknown session: {session_id}")
                connection.execute(
                    """
                    INSERT INTO session_background_wake_state(
                        session_id, announced_task_ids_json, pending_task_ids_json,
                        wake_count, last_wake_at, wake_in_flight
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        announced_task_ids_json = excluded.announced_task_ids_json,
                        pending_task_ids_json = excluded.pending_task_ids_json,
                        wake_count = excluded.wake_count,
                        last_wake_at = excluded.last_wake_at,
                        wake_in_flight = excluded.wake_in_flight
                    """,
                    (
                        session_id,
                        json.dumps(state.announced_task_ids, separators=(",", ":")),
                        json.dumps(state.pending_task_ids, separators=(",", ":")),
                        state.wake_count,
                        state.last_wake_at.isoformat() if state.last_wake_at else None,
                        int(state.wake_in_flight),
                    ),
                )
                connection.execute(
                    "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (session_id,),
                )

        async with self._write_lock:
            await run_blocking(save)

    async def load_background_wake_state(self, session_id: str) -> BackgroundWakeState:
        def load() -> BackgroundWakeState:
            with closing(self._connect()) as connection:
                exists = connection.execute(
                    "SELECT 1 FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if exists is None:
                    raise SessionError(f"unknown session: {session_id}")
                row = connection.execute(
                    """
                    SELECT announced_task_ids_json, pending_task_ids_json,
                           wake_count, last_wake_at, wake_in_flight
                    FROM session_background_wake_state
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
            if row is None:
                return BackgroundWakeState()
            return _background_wake_state_from_row(row, session_id=session_id)

        return await run_blocking(load)

    async def load_session_items(self, session_id: str) -> list[SessionItem]:
        def load() -> list[SessionItem]:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT messages_json FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
            if row is None:
                raise SessionError(f"unknown session: {session_id}")
            try:
                return _session_items_from_json(row[0])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise SessionError(f"session {session_id} contains invalid messages") from error

        return await run_blocking(load)

    async def bind_session_alias(
        self,
        namespace: str,
        external_id: str,
        session_id: str,
    ) -> None:
        normalized_namespace = _session_alias_value(
            namespace,
            field_name="namespace",
            limit=_SESSION_ALIAS_NAMESPACE_LIMIT,
        )
        normalized_external_id = _session_alias_value(
            external_id,
            field_name="external ID",
            limit=_SESSION_ALIAS_ID_LIMIT,
        )

        def bind() -> None:
            with closing(self._connect()) as connection, connection:
                exists = connection.execute(
                    "SELECT 1 FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if exists is None:
                    raise SessionError(f"unknown session: {session_id}")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO session_aliases(
                        namespace, external_id, session_id
                    ) VALUES (?, ?, ?)
                    """,
                    (normalized_namespace, normalized_external_id, session_id),
                )
                current = connection.execute(
                    """
                    SELECT session_id
                    FROM session_aliases
                    WHERE namespace = ? AND external_id = ?
                    """,
                    (normalized_namespace, normalized_external_id),
                ).fetchone()
                reverse = connection.execute(
                    """
                    SELECT external_id
                    FROM session_aliases
                    WHERE namespace = ? AND session_id = ?
                    """,
                    (normalized_namespace, session_id),
                ).fetchone()
                if current is not None and str(current[0]) != session_id:
                    raise SessionError("session alias is already bound")
                if reverse is not None and str(reverse[0]) != normalized_external_id:
                    raise SessionError("session already has an alias in this namespace")
                if current is None or reverse is None:
                    raise SessionError("cannot bind session alias")

        async with self._write_lock:
            await run_blocking(bind)

    async def resolve_session_alias(
        self,
        namespace: str,
        external_id: str,
    ) -> str:
        normalized_namespace = _session_alias_value(
            namespace,
            field_name="namespace",
            limit=_SESSION_ALIAS_NAMESPACE_LIMIT,
        )
        normalized_external_id = _session_alias_value(
            external_id,
            field_name="external ID",
            limit=_SESSION_ALIAS_ID_LIMIT,
        )

        def resolve() -> str:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    """
                    SELECT session_id
                    FROM session_aliases
                    WHERE namespace = ? AND external_id = ?
                    """,
                    (normalized_namespace, normalized_external_id),
                ).fetchone()
                if row is not None:
                    return str(row[0])
                legacy = connection.execute(
                    "SELECT id FROM sessions WHERE id = ?",
                    (normalized_external_id,),
                ).fetchone()
            if legacy is not None:
                return str(legacy[0])
            raise SessionError(f"unknown session alias: {normalized_external_id}")

        return await run_blocking(resolve)

    async def get_or_create_session_alias(
        self,
        namespace: str,
        session_id: str,
        proposed_external_id: str,
    ) -> str:
        normalized_namespace = _session_alias_value(
            namespace,
            field_name="namespace",
            limit=_SESSION_ALIAS_NAMESPACE_LIMIT,
        )
        normalized_external_id = _session_alias_value(
            proposed_external_id,
            field_name="external ID",
            limit=_SESSION_ALIAS_ID_LIMIT,
        )

        def get_or_create() -> str:
            with closing(self._connect()) as connection, connection:
                exists = connection.execute(
                    "SELECT 1 FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if exists is None:
                    raise SessionError(f"unknown session: {session_id}")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO session_aliases(
                        namespace, external_id, session_id
                    ) VALUES (?, ?, ?)
                    """,
                    (normalized_namespace, normalized_external_id, session_id),
                )
                row = connection.execute(
                    """
                    SELECT external_id
                    FROM session_aliases
                    WHERE namespace = ? AND session_id = ?
                    """,
                    (normalized_namespace, session_id),
                ).fetchone()
                if row is None:
                    raise SessionError("proposed session alias is unavailable")
                return str(row[0])

        async with self._write_lock:
            return await run_blocking(get_or_create)

    async def load_events(self, session_id: str) -> list[dict[str, Any]]:
        def load() -> list[dict[str, Any]]:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT sequence, kind, created_at, data_json
                    FROM events WHERE session_id = ? ORDER BY sequence
                    """,
                    (session_id,),
                ).fetchall()
            return [
                {
                    "sequence": row[0],
                    "kind": row[1],
                    "created_at": row[2],
                    "data": json.loads(row[3]),
                }
                for row in rows
            ]

        return await run_blocking(load)

    async def next_event_sequence(self, session_id: str) -> int:
        def load() -> int:
            with closing(self._connect()) as connection:
                session_exists = connection.execute(
                    "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if session_exists is None:
                    raise SessionError(f"unknown session: {session_id}")
                row = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            assert row is not None
            return int(row[0])

        return await run_blocking(load)

    async def list_sessions(self, *, limit: int = 50) -> list[SessionSummary]:
        if not 1 <= limit <= 1000:
            raise SessionError("session list limit must be between 1 and 1000")

        def load() -> list[SessionSummary]:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT id, cwd, provider, model, created_at, updated_at,
                           context_affinity, sandbox_profile, title
                    FROM sessions ORDER BY updated_at DESC, id DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return [_summary_from_row(row) for row in rows]

        return await run_blocking(load)

    async def list_sessions_page(
        self,
        *,
        limit: int,
        before_updated_at: datetime | None = None,
        before_id: str | None = None,
    ) -> list[SessionSummary]:
        if not 1 <= limit <= 1000:
            raise SessionError("session list limit must be between 1 and 1000")
        if (before_updated_at is None) != (before_id is None):
            raise SessionError("session list cursor fields must be provided together")
        if before_updated_at is not None and before_updated_at.tzinfo is None:
            raise SessionError("session list cursor timestamp must be timezone-aware")
        if before_id is not None and not before_id:
            raise SessionError("session list cursor ID must not be empty")

        def load() -> list[SessionSummary]:
            parameters: tuple[object, ...]
            if before_updated_at is None:
                where = ""
                parameters = (limit,)
            else:
                assert before_id is not None
                rendered_timestamp = before_updated_at.isoformat()
                where = """
                    WHERE julianday(updated_at) < julianday(?)
                       OR (
                           julianday(updated_at) = julianday(?)
                           AND id < ?
                       )
                """
                parameters = (
                    rendered_timestamp,
                    rendered_timestamp,
                    before_id,
                    limit,
                )
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    f"""
                    SELECT id, cwd, provider, model, created_at, updated_at,
                           context_affinity, sandbox_profile, title
                    FROM sessions
                    {where}
                    ORDER BY julianday(updated_at) DESC, id DESC
                    LIMIT ?
                    """,
                    parameters,
                ).fetchall()
            return [_summary_from_row(row) for row in rows]

        return await run_blocking(load)

    async def search_sessions(
        self,
        query: str,
        *,
        cwd: str | None = None,
        limit: int = 20,
        offset: int = 0,
        include_content: bool = False,
    ) -> SessionSearchPage:
        normalized_query = query.strip()
        if not normalized_query:
            raise SessionError("session search query must not be empty")
        if len(normalized_query) > 1000:
            raise SessionError("session search query must not exceed 1000 characters")
        if cwd == "":
            raise SessionError("session search cwd must not be empty")
        if not 1 <= limit <= 1000:
            raise SessionError("session search limit must be between 1 and 1000")
        if not 0 <= offset <= 1_000_000:
            raise SessionError("session search offset must be between 0 and 1000000")
        and_query, or_query = _search_match_queries(normalized_query)

        def search() -> SessionSearchPage:
            with closing(self._connect()) as connection:
                page = _run_session_search(
                    connection,
                    match_query=and_query,
                    cwd=cwd,
                    limit=limit,
                    offset=offset,
                    include_content=include_content,
                )
                if page.total_estimate == 0 and and_query != or_query:
                    return _run_session_search(
                        connection,
                        match_query=or_query,
                        cwd=cwd,
                        limit=limit,
                        offset=offset,
                        include_content=include_content,
                    )
                return page

        return await run_blocking(search)

    async def get_session(self, session_id: str) -> SessionSummary:
        def load() -> SessionSummary:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    """
                    SELECT id, cwd, provider, model, created_at, updated_at,
                           context_affinity, sandbox_profile, title
                    FROM sessions WHERE id = ?
                    """,
                    (session_id,),
                ).fetchone()
            if row is None:
                raise SessionError(f"unknown session: {session_id}")
            return _summary_from_row(row)

        return await run_blocking(load)

    async def peek_session_sandbox_profile(
        self,
        session_id: str,
    ) -> SandboxProfile | None:
        """Read a saved profile without creating or migrating the database.

        Startup uses this before entering an irreversible process sandbox. A
        missing database, session, or v2-and-earlier column represents a legacy
        session and therefore has no pinned profile.

        读取已保存的配置档案,不创建或迁移数据库. 缺失数据库、会话或旧版本字段时视为没有固定配置档案.
        """

        def peek() -> SandboxProfile | None:
            try:
                resolved = self._database_path.expanduser().resolve(strict=True)
            except FileNotFoundError:
                return None
            except OSError as error:
                raise SessionError(
                    f"cannot resolve session database {self._database_path}: {error}"
                ) from error
            if not resolved.is_file():
                return None
            sidecars = (
                resolved.with_name(f"{resolved.name}-wal"),
                resolved.with_name(f"{resolved.name}-shm"),
            )
            if any(path.exists() for path in sidecars):
                raise SessionError(
                    "cannot inspect saved session sandbox while the database has an active WAL"
                )

            try:
                with closing(
                    sqlite3.connect(
                        f"{resolved.as_uri()}?mode=ro&immutable=1",
                        uri=True,
                        timeout=30,
                    )
                ) as connection:
                    connection.execute("PRAGMA query_only = ON")
                    tables = {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        ).fetchall()
                    }
                    if "sessions" not in tables:
                        return None
                    columns = {
                        str(row[1])
                        for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
                    }
                    if "sandbox_profile" not in columns:
                        return None
                    row = connection.execute(
                        "SELECT sandbox_profile FROM sessions WHERE id = ?",
                        (session_id,),
                    ).fetchone()
            except sqlite3.Error as error:
                raise SessionError(f"cannot inspect saved session sandbox: {error}") from error
            if row is None or row[0] is None:
                return None
            return _parse_sandbox_profile(row[0], session_id=session_id)

        return await run_blocking(peek)


def _session_alias_value(value: str, *, field_name: str, limit: int) -> str:
    if not value or "\x00" in value:
        raise SessionError(f"session alias {field_name} must not be empty")
    if len(value.encode("utf-8")) > limit:
        raise SessionError(f"session alias {field_name} is too large")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise SessionError(f"session alias {field_name} contains control characters")
    return value


def _backfill_search_documents(
    connection: sqlite3.Connection,
    *,
    missing_only: bool = False,
) -> None:
    condition = "WHERE documents.session_id IS NULL" if missing_only else ""
    rows = connection.execute(
        f"""
        SELECT sessions.id, sessions.messages_json, sessions.title
        FROM sessions
        LEFT JOIN session_search_documents AS documents
          ON documents.session_id = sessions.id
        {condition}
        """
    ).fetchall()
    for session_id, payload, saved_title in rows:
        try:
            items = _session_items_from_json(payload)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            items = []
        title = str(saved_title) or fallback_session_title(items)
        if not saved_title:
            connection.execute(
                "UPDATE sessions SET title = ? WHERE id = ?",
                (title, session_id),
            )
        _upsert_search_document(
            connection,
            session_id=str(session_id),
            title=title,
            content=searchable_session_text(items),
        )


def _upsert_search_document(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    title: str,
    content: str,
) -> None:
    connection.execute(
        """
        INSERT INTO session_search_documents(session_id, title, content)
        VALUES (?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            title = excluded.title,
            content = excluded.content
        """,
        (session_id, title, content),
    )


def _search_match_queries(query: str) -> tuple[str, str]:
    tokens = [
        token
        for token in re.findall(r"[\w-]+", query, flags=re.UNICODE)
        if any(character.isalnum() for character in token)
    ]
    if not tokens:
        raise SessionError("session search query contains no searchable text")
    prefixes = [_search_token_prefix(token) for token in tokens]
    return " AND ".join(prefixes), " OR ".join(prefixes)


def _search_token_prefix(token: str) -> str:
    stem = token
    lower = token.casefold()
    if len(token) >= 4 and token.isascii() and token.isalpha():
        if lower.endswith("es"):
            stem = token[:-2]
        elif lower.endswith("s") and not lower.endswith("ss"):
            stem = token[:-1]
    escaped = stem.replace('"', '""')
    return f'"{escaped}"*'


def _run_session_search(
    connection: sqlite3.Connection,
    *,
    match_query: str,
    cwd: str | None,
    limit: int,
    offset: int,
    include_content: bool,
) -> SessionSearchPage:
    total_row = connection.execute(
        """
        SELECT COUNT(*)
        FROM session_search_fts
        JOIN session_search_documents AS documents
          ON documents.rowid = session_search_fts.rowid
        JOIN sessions ON sessions.id = documents.session_id
        WHERE session_search_fts MATCH ?
          AND (? IS NULL OR sessions.cwd = ?)
        """,
        (match_query, cwd, cwd),
    ).fetchone()
    total = int(total_row[0]) if total_row is not None else 0
    snippet_expression = (
        "snippet(session_search_fts, 1, '[', ']', ' … ', 18)" if include_content else "NULL"
    )
    rows = connection.execute(
        f"""
        SELECT sessions.id, sessions.cwd, sessions.provider, sessions.model,
               sessions.created_at, sessions.updated_at, sessions.context_affinity,
               sessions.sandbox_profile, sessions.title,
               bm25(session_search_fts, 10.0, 1.0) AS rank,
               {snippet_expression} AS snippet,
               highlight(session_search_fts, 0, char(1), char(2)) AS title_highlight,
               highlight(session_search_fts, 1, char(1), char(2)) AS content_highlight
        FROM session_search_fts
        JOIN session_search_documents AS documents
          ON documents.rowid = session_search_fts.rowid
        JOIN sessions ON sessions.id = documents.session_id
        WHERE session_search_fts MATCH ?
          AND (? IS NULL OR sessions.cwd = ?)
        ORDER BY rank ASC, sessions.updated_at DESC, sessions.id ASC
        LIMIT ? OFFSET ?
        """,
        (match_query, cwd, cwd, limit, offset),
    ).fetchall()
    results: list[SessionSearchHit] = []
    for row in rows:
        rank = float(row[9])
        score = -rank if math.isfinite(rank) else 0.0
        matched_fields: list[str] = []
        if "\x01" in str(row[11]):
            matched_fields.append("title")
        if "\x01" in str(row[12]):
            matched_fields.append("content")
        if not matched_fields:
            matched_fields.append("content")
        results.append(
            SessionSearchHit(
                summary=_summary_from_row(row[:9]),
                score=score,
                matched_fields=tuple(matched_fields),
                snippet=(str(row[10])[:_SEARCH_SNIPPET_LIMIT] if row[10] is not None else None),
            )
        )
    next_offset = offset + len(results) if offset + len(results) < total else None
    return SessionSearchPage(tuple(results), next_offset, total)


def _serialize_session_items(items: Sequence[SessionItem]) -> str:
    return json.dumps(
        [item.to_dict() for item in items],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _session_items_from_json(payload: object) -> list[SessionItem]:
    loaded: object = json.loads(str(payload))
    if not isinstance(loaded, list):
        raise TypeError("session items must be a list")
    return [_session_item_from_dict(item) for item in loaded]


def _session_item_from_dict(raw: object) -> SessionItem:
    if not isinstance(raw, dict):
        raise TypeError("session item must be an object")
    raw_type = raw.get("type")
    if isinstance(raw_type, str):
        kind = ContextItemKind(raw_type)
        return PreservedContextItem(kind, raw)
    return _message_from_dict(raw)


def _message_from_dict(raw: object) -> Message:
    if not isinstance(raw, dict):
        raise TypeError("message must be an object")
    tool_calls_raw = raw.get("tool_calls", [])
    if not isinstance(tool_calls_raw, list):
        raise TypeError("tool_calls must be a list")
    tool_calls: list[ToolCall] = []
    for item in tool_calls_raw:
        if not isinstance(item, dict) or not isinstance(item.get("arguments"), dict):
            raise TypeError("tool call must be an object")
        raw_metadata = item.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        tool_calls.append(
            ToolCall(
                id=str(item["id"]),
                name=str(item["name"]),
                arguments=item["arguments"],
                metadata=metadata,
            )
        )
    content_parts_raw = raw.get("content_parts", [])
    if not isinstance(content_parts_raw, list):
        raise TypeError("content_parts must be a list")
    content_parts = tuple(_content_part_from_dict(item) for item in content_parts_raw)
    raw_reasoning_content = raw.get("reasoning_content")
    if raw_reasoning_content is not None and not isinstance(raw_reasoning_content, str):
        raise TypeError("reasoning_content must be a string")
    return Message(
        role=Role(str(raw["role"])),
        content=str(raw.get("content", "")),
        name=str(raw["name"]) if raw.get("name") is not None else None,
        tool_call_id=(str(raw["tool_call_id"]) if raw.get("tool_call_id") is not None else None),
        tool_calls=tuple(tool_calls),
        content_parts=content_parts,
        reasoning_content=raw_reasoning_content,
    )


def _content_part_from_dict(raw: object) -> ContentPart:
    if not isinstance(raw, dict):
        raise TypeError("content part must be an object")
    kind = ContentPartKind(str(raw["type"]))
    if kind is ContentPartKind.TEXT:
        text = raw.get("text")
        if not isinstance(text, str):
            raise TypeError("text content part requires text")
        return ContentPart.from_text(text)
    if kind is ContentPartKind.IMAGE:
        url = raw.get("url")
        if not isinstance(url, str):
            raise TypeError("image content part requires url")
        return ContentPart.from_image(url)
    data = raw.get("data")
    mime_type = raw.get("mime_type", raw.get("mimeType"))
    if not isinstance(data, str) or not isinstance(mime_type, str):
        raise TypeError("binary content part requires data and MIME type")
    if kind is ContentPartKind.AUDIO:
        return ContentPart.from_audio(data, mime_type)
    if kind is ContentPartKind.BLOB:
        url = raw.get("url")
        if not isinstance(url, str):
            raise TypeError("blob content part requires url")
        return ContentPart.from_blob(url, data, mime_type)
    raise TypeError("unsupported content part kind")


def _summary_from_row(row: tuple[Any, ...]) -> SessionSummary:
    def timestamp(value: object) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace(" ", "T"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed

    return SessionSummary(
        id=str(row[0]),
        cwd=str(row[1]),
        provider=str(row[2]),
        model=str(row[3]),
        created_at=timestamp(row[4]),
        updated_at=timestamp(row[5]),
        context_affinity=str(row[6]) if row[6] is not None else None,
        sandbox_profile=(
            _parse_sandbox_profile(row[7], session_id=str(row[0])) if row[7] is not None else None
        ),
        title=str(row[8]) if row[8] else None,
    )


def _parse_sandbox_profile(value: object, *, session_id: str) -> SandboxProfile:
    try:
        return SandboxProfile.parse(str(value))
    except ValueError as error:
        raise SessionError(
            f"session {session_id} contains unsupported sandbox profile {value!r}"
        ) from error


def _background_wake_state_from_row(
    row: Sequence[object],
    *,
    session_id: str,
) -> BackgroundWakeState:
    try:
        (
            raw_announced,
            raw_pending,
            raw_wake_count,
            raw_last_wake_at,
            raw_in_flight,
        ) = row
        announced = json.loads(str(raw_announced))
        pending = json.loads(str(raw_pending))
        if not isinstance(announced, list) or not all(
            isinstance(task_id, str) for task_id in announced
        ):
            raise ValueError("announced task IDs are invalid")
        if not isinstance(pending, list) or not all(
            isinstance(task_id, str) for task_id in pending
        ):
            raise ValueError("pending task IDs are invalid")
        if not isinstance(raw_wake_count, int) or isinstance(raw_wake_count, bool):
            raise ValueError("wake count is invalid")
        if raw_in_flight not in (0, 1) or isinstance(raw_in_flight, bool):
            raise ValueError("wake in-flight flag is invalid")
        last_wake_at = (
            datetime.fromisoformat(str(raw_last_wake_at)) if raw_last_wake_at is not None else None
        )
        return BackgroundWakeState(
            announced_task_ids=tuple(announced),
            pending_task_ids=tuple(pending),
            wake_count=raw_wake_count,
            last_wake_at=last_wake_at,
            wake_in_flight=bool(raw_in_flight),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise SessionError(
            f"session {session_id} contains an invalid background wake state"
        ) from error
