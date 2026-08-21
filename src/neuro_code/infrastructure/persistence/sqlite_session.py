"""Canonical SQLite-backed implementation for the SessionStore port.

The implementation body is owned by the infrastructure persistence layer;
the legacy adapter is a compatibility facade that re-exports this class.

定义 SessionStore 端口的规范 SQLite 实现. 旧适配器仅作为兼容门面重新导出此类.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sqlite3
import time
import uuid
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from neuro_code.domain.background_tasks.models import BackgroundWakeState
from neuro_code.domain.conversation.compaction import DurableCompactionItem
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
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
from neuro_code.domain.execution import (
    AgentExecutionOutcome,
    AgentExecutionStatus,
    SessionExecutionRecord,
    SupervisorReasonCode,
    TurnInput,
    TurnRecoveryAttempt,
    TurnRecoveryFact,
    TurnRecoveryFactKind,
    TurnRecoveryResolution,
    TurnRecoveryStage,
    TurnSource,
)
from neuro_code.domain.plans import MAX_PLAN_COMMENTS, PlanComment, SessionPlan
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.session_tasks import (
    MAX_QUEUED_SESSION_TASKS,
    MAX_SESSION_TASK_ID_BYTES,
    SessionTask,
    SessionTaskKind,
    SessionTaskStatus,
    SubagentLink,
)
from neuro_code.domain.sessions import (
    SessionSnapshot,
    SessionSummary,
    normalize_session_title,
)
from neuro_code.domain.sessions.search import (
    SessionSearchHit,
    SessionSearchPage,
    fallback_session_title,
    searchable_session_text,
)
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import SessionError

SCHEMA_VERSION = 14
_SEARCH_SNIPPET_LIMIT = 500
_SQLITE_TIMEOUT_SECONDS = 30.0
_SESSION_ALIAS_NAMESPACE_LIMIT = 64
_SESSION_ALIAS_ID_LIMIT = 512


class SqliteSessionStore:
    """SQLite-backed, append-only session event store.

    Each operation owns a short-lived connection so it can safely run through
    `asyncio.to_thread` without sharing SQLite objects between threads.

    定义基于 SQLite 且只追加写入的会话事件存储. 每次操作使用短生命周期连接,以便安全地在线程中执行.
    """

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._write_lock = asyncio.Lock()

    @property
    def database_path(self) -> Path:
        return self._database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=_SQLITE_TIMEOUT_SECONDS)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {int(_SQLITE_TIMEOUT_SECONDS * 1_000)}")
            deadline = time.monotonic() + _SQLITE_TIMEOUT_SECONDS
            while True:
                try:
                    connection.execute("PRAGMA journal_mode = WAL")
                    return connection
                except sqlite3.OperationalError as error:
                    if "locked" not in str(error).casefold() or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
        except BaseException:
            connection.close()
            raise

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
                    connection.execute("DELETE FROM sessions WHERE id = ?", (current,))
                if session_id not in seen:
                    raise SessionError(f"unknown session: {session_id}")

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

    async def start_turn_attempt(self, attempt: TurnRecoveryAttempt) -> None:
        """Durably accept one turn before any provider or tool boundary."""

        if not isinstance(attempt, TurnRecoveryAttempt):
            raise TypeError("attempt must be a TurnRecoveryAttempt")
        input_json = attempt.input.canonical_json() if attempt.input is not None else ""

        def start() -> None:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                session = connection.execute(
                    "SELECT 1 FROM sessions WHERE id = ?", (attempt.session_id,)
                ).fetchone()
                if session is None:
                    raise SessionError(f"unknown session: {attempt.session_id}")
                existing = connection.execute(
                    """
                    SELECT turn_id
                    FROM session_turn_attempts
                    WHERE session_id = ? AND resolution IS NULL
                    LIMIT 1
                    """,
                    (attempt.session_id,),
                ).fetchone()
                if existing is not None:
                    raise SessionError(
                        f"session {attempt.session_id} already has an open turn attempt"
                    )
                connection.execute(
                    """
                    INSERT INTO session_turn_attempts(
                        turn_id, session_id, source, task_id, input_json,
                        input_fingerprint, input_reconstructable, accepted_at,
                        last_stage, last_stage_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt.turn_id,
                        attempt.session_id,
                        attempt.source.value,
                        attempt.task_id,
                        input_json,
                        attempt.input_fingerprint,
                        int(attempt.input_reconstructable),
                        attempt.accepted_at.isoformat(),
                        attempt.last_stage.value,
                        attempt.last_stage_at.isoformat()
                        if attempt.last_stage_at is not None
                        else attempt.accepted_at.isoformat(),
                    ),
                )
                connection.execute(
                    "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (attempt.session_id,),
                )
                connection.commit()
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise SessionError(f"cannot create turn attempt: {attempt.turn_id}") from error
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

        async with self._write_lock:
            await run_blocking(start)

    async def append_turn_recovery_fact(
        self,
        session_id: str,
        turn_id: str,
        event: AgentEvent,
        fact: TurnRecoveryFact,
    ) -> None:
        """Append a recovery marker and update its sticky facts atomically."""

        if not isinstance(event, AgentEvent):
            raise TypeError("event must be an AgentEvent")
        if not isinstance(fact, TurnRecoveryFact):
            raise TypeError("fact must be a TurnRecoveryFact")
        expected_kind = {
            TurnRecoveryFactKind.MODEL_REQUEST_STARTED: AgentEventKind.MODEL_REQUEST_STARTED,
            TurnRecoveryFactKind.MODEL_OUTPUT_STARTED: AgentEventKind.MODEL_OUTPUT_STARTED,
            TurnRecoveryFactKind.TOOL_STARTED: AgentEventKind.TOOL_STARTED,
        }[fact.kind]
        if event.kind is not expected_kind:
            raise SessionError("recovery fact event kind does not match its fact")
        if event.data.get("turn_id") != turn_id:
            raise SessionError("recovery fact event has a different turn identity")
        payload = json.dumps(dict(event.data), ensure_ascii=False, separators=(",", ":"))

        def append() -> None:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT resolution
                    FROM session_turn_attempts
                    WHERE session_id = ? AND turn_id = ?
                    """,
                    (session_id, turn_id),
                ).fetchone()
                if row is None:
                    raise SessionError(f"unknown turn attempt: {turn_id}")
                if row[0] is not None:
                    raise SessionError(f"turn attempt is already resolved: {turn_id}")
                _insert_event_row(
                    connection,
                    session_id=session_id,
                    event=event,
                    payload=payload,
                )
                if fact.kind is TurnRecoveryFactKind.MODEL_REQUEST_STARTED:
                    connection.execute(
                        """
                        UPDATE session_turn_attempts
                        SET request_started_count = request_started_count + 1,
                            request_id = ?, step = ?, provider = ?, model = ?,
                            last_stage = ?, last_stage_at = ?
                        WHERE session_id = ? AND turn_id = ?
                        """,
                        (
                            fact.request_id,
                            fact.step,
                            fact.provider,
                            fact.model,
                            TurnRecoveryStage.REQUEST_STARTED.value,
                            event.created_at.isoformat(),
                            session_id,
                            turn_id,
                        ),
                    )
                elif fact.kind is TurnRecoveryFactKind.MODEL_OUTPUT_STARTED:
                    connection.execute(
                        """
                        UPDATE session_turn_attempts
                        SET output_started = 1,
                            last_stage = ?, last_stage_at = ?
                        WHERE session_id = ? AND turn_id = ?
                        """,
                        (
                            TurnRecoveryStage.MODEL_OUTPUT_STARTED.value,
                            event.created_at.isoformat(),
                            session_id,
                            turn_id,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE session_turn_attempts
                        SET tool_started_count = tool_started_count + 1,
                            side_effecting_tool_started = CASE
                                WHEN side_effecting_tool_started = 1 OR ? = 1 THEN 1
                                ELSE 0 END,
                            last_tool_id = ?, last_tool_name = ?,
                            last_stage = ?, last_stage_at = ?
                        WHERE session_id = ? AND turn_id = ?
                        """,
                        (
                            int(fact.side_effecting),
                            fact.tool_id,
                            fact.tool_name,
                            TurnRecoveryStage.TOOL_STARTED.value,
                            event.created_at.isoformat(),
                            session_id,
                            turn_id,
                        ),
                    )
                connection.execute(
                    "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (session_id,),
                )
                connection.commit()
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise SessionError(
                    f"cannot append recovery fact {event.sequence} for turn {turn_id}"
                ) from error
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

        async with self._write_lock:
            await run_blocking(append)

    async def finalize_turn(
        self,
        session_id: str,
        event: AgentEvent,
        items: Sequence[SessionItem],
        record: SessionExecutionRecord | None,
        turn_id: str | None = None,
        task: SessionTask | None = None,
        task_event: AgentEvent | None = None,
    ) -> None:
        if not isinstance(event, AgentEvent):
            raise TypeError("event must be an AgentEvent")
        if event.kind is not AgentEventKind.TURN_COMPLETED:
            raise SessionError("finalize_turn requires a TURN_COMPLETED event")
        if (
            not isinstance(event.sequence, int)
            or isinstance(event.sequence, bool)
            or event.sequence <= 0
        ):
            raise SessionError("finalize_turn event sequence must be positive")
        if record is not None:
            if not isinstance(record, SessionExecutionRecord):
                raise TypeError("record must be a SessionExecutionRecord or None")
            if record.event_sequence != event.sequence:
                raise SessionError("execution record sequence does not match completion event")
        new_items = list(items)

        def finalize() -> None:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                _persist_finalized_turn(
                    connection,
                    session_id=session_id,
                    event=event,
                    items=new_items,
                    record=record,
                    compaction_item=None,
                    turn_id=turn_id,
                    task=task,
                    task_event=task_event,
                )
                connection.commit()
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise SessionError(
                    f"cannot finalize turn event {event.sequence} for session {session_id}"
                ) from error
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

        async with self._write_lock:
            await run_blocking(finalize)

    async def finalize_turn_with_compaction(
        self,
        session_id: str,
        event: AgentEvent,
        items: Sequence[SessionItem],
        record: SessionExecutionRecord | None,
        compaction_item: DurableCompactionItem,
        turn_id: str | None = None,
        task: SessionTask | None = None,
        task_event: AgentEvent | None = None,
    ) -> None:
        """Atomically finalize a turn and persist one compaction item.

        The event, session items, optional execution record, search projection,
        and compaction row share one SQLite transaction.  This method is an
        explicit opt-in contract; ``save_compaction_item`` remains an
        independent short operation for callers that do not own turn
        finalization.

        原子地完成一个回合并持久化一个压缩条目.

        事件、会话条目、可选执行记录、搜索投影和压缩行共享同一个 SQLite
        事务. 这是显式选择的契约; ``save_compaction_item`` 对不拥有回合最终化的
        调用方仍保持独立短操作语义.
        """

        if not isinstance(event, AgentEvent):
            raise TypeError("event must be an AgentEvent")
        if event.kind is not AgentEventKind.TURN_COMPLETED:
            raise SessionError("finalize_turn requires a TURN_COMPLETED event")
        if (
            not isinstance(event.sequence, int)
            or isinstance(event.sequence, bool)
            or event.sequence <= 0
        ):
            raise SessionError("finalize_turn event sequence must be positive")
        if record is not None:
            if not isinstance(record, SessionExecutionRecord):
                raise TypeError("record must be a SessionExecutionRecord or None")
            if record.event_sequence != event.sequence:
                raise SessionError("execution record sequence does not match completion event")
        if not isinstance(compaction_item, DurableCompactionItem):
            raise TypeError("compaction_item must be a DurableCompactionItem")
        new_items = list(items)

        def finalize() -> None:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                _persist_finalized_turn(
                    connection,
                    session_id=session_id,
                    event=event,
                    items=new_items,
                    record=record,
                    compaction_item=compaction_item,
                    turn_id=turn_id,
                    task=task,
                    task_event=task_event,
                )
                connection.commit()
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise SessionError(
                    f"cannot finalize turn event {event.sequence} for session {session_id}"
                ) from error
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

        async with self._write_lock:
            await run_blocking(finalize)

    async def finalize_turn_failure(
        self,
        session_id: str,
        turn_id: str | None,
        event: AgentEvent,
        items: Sequence[SessionItem],
        *,
        resolution: str,
        task: SessionTask | None = None,
        task_event: AgentEvent | None = None,
    ) -> None:
        """Atomically close a failed/cancelled turn and its task."""

        if not isinstance(event, AgentEvent):
            raise TypeError("event must be an AgentEvent")
        if event.kind is not AgentEventKind.TURN_FAILED:
            raise SessionError("finalize_turn_failure requires a TURN_FAILED event")
        if resolution not in {
            TurnRecoveryResolution.FAILED.value,
            TurnRecoveryResolution.CANCELLED.value,
        }:
            raise SessionError("turn failure resolution must be failed or cancelled")
        new_items = list(items)

        def finalize() -> None:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                _persist_failed_turn(
                    connection,
                    session_id=session_id,
                    turn_id=turn_id,
                    event=event,
                    items=new_items,
                    resolution=resolution,
                    task=task,
                    task_event=task_event,
                )
                connection.commit()
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise SessionError(
                    f"cannot finalize failed turn event {event.sequence} for session {session_id}"
                ) from error
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

        async with self._write_lock:
            await run_blocking(finalize)

    async def abandon_turn_attempt(
        self,
        session_id: str,
        turn_id: str,
        event: AgentEvent,
        reason: str,
    ) -> None:
        """Persist an explicit user-directed abandon resolution."""

        if not isinstance(event, AgentEvent) or event.kind is not AgentEventKind.TURN_ABANDONED:
            raise SessionError("abandon_turn_attempt requires a TURN_ABANDONED event")
        if not isinstance(reason, str) or not reason.strip() or len(reason.encode("utf-8")) > 512:
            raise SessionError("abandon reason is invalid")
        if event.data.get("turn_id") != turn_id:
            raise SessionError("abandon event has a different turn identity")
        payload = json.dumps(dict(event.data), ensure_ascii=False, separators=(",", ":"))

        def abandon() -> None:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT resolution
                    FROM session_turn_attempts
                    WHERE session_id = ? AND turn_id = ?
                    """,
                    (session_id, turn_id),
                ).fetchone()
                if row is None:
                    raise SessionError(f"unknown turn attempt: {turn_id}")
                if row[0] is not None:
                    raise SessionError(f"turn attempt is already resolved: {turn_id}")
                _insert_event_row(
                    connection,
                    session_id=session_id,
                    event=event,
                    payload=payload,
                )
                updated = connection.execute(
                    """
                    UPDATE session_turn_attempts
                    SET resolution = ?, resolution_at = ?,
                        last_stage = ?, last_stage_at = ?
                    WHERE session_id = ? AND turn_id = ? AND resolution IS NULL
                    """,
                    (
                        TurnRecoveryResolution.ABANDONED.value,
                        event.created_at.isoformat(),
                        TurnRecoveryStage.ABANDONED.value,
                        event.created_at.isoformat(),
                        session_id,
                        turn_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise SessionError(f"cannot abandon turn attempt: {turn_id}")
                connection.execute(
                    "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (session_id,),
                )
                connection.commit()
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise SessionError(f"cannot append abandon event for turn {turn_id}") from error
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

        async with self._write_lock:
            await run_blocking(abandon)

    async def load_turn_attempts(self, session_id: str) -> list[TurnRecoveryAttempt]:
        def load() -> list[TurnRecoveryAttempt]:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT turn_id, session_id, source, task_id, input_json,
                           input_fingerprint, input_reconstructable, accepted_at,
                           resolution, resolution_at, request_started_count,
                           request_id, step, provider, model, output_started,
                           tool_started_count, side_effecting_tool_started,
                           last_tool_id, last_tool_name, last_stage, last_stage_at,
                           fact_conflict
                    FROM session_turn_attempts
                    WHERE session_id = ?
                    ORDER BY accepted_at ASC, turn_id ASC
                    """,
                    (session_id,),
                ).fetchall()
            return [_turn_recovery_attempt_from_row(row) for row in rows]

        return await run_blocking(load)

    async def load_open_turn_attempts(self, session_id: str) -> list[TurnRecoveryAttempt]:
        def load() -> list[TurnRecoveryAttempt]:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT turn_id, session_id, source, task_id, input_json,
                           input_fingerprint, input_reconstructable, accepted_at,
                           resolution, resolution_at, request_started_count,
                           request_id, step, provider, model, output_started,
                           tool_started_count, side_effecting_tool_started,
                           last_tool_id, last_tool_name, last_stage, last_stage_at,
                           fact_conflict
                    FROM session_turn_attempts
                    WHERE session_id = ? AND resolution IS NULL
                    ORDER BY accepted_at ASC, turn_id ASC
                    """,
                    (session_id,),
                ).fetchall()
            return [_turn_recovery_attempt_from_row(row) for row in rows]

        return await run_blocking(load)

    async def load_messages(self, session_id: str) -> list[Message]:
        items = await self.load_session_items(session_id)
        return [item for item in items if isinstance(item, Message)]

    async def save_session_plan(self, session_id: str, plan: SessionPlan | None) -> None:
        if plan is not None and not isinstance(plan, SessionPlan):
            raise TypeError("plan must be a SessionPlan or None")
        payload = (
            json.dumps(plan.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            if plan is not None
            else ""
        )

        def save() -> None:
            with closing(self._connect()) as connection, connection:
                cursor = connection.execute(
                    """
                    UPDATE sessions
                    SET plan_json = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (payload, session_id),
                )
                if cursor.rowcount != 1:
                    raise SessionError(f"unknown session: {session_id}")
                if plan is None:
                    connection.execute(
                        "DELETE FROM session_plan_comments WHERE session_id = ?",
                        (session_id,),
                    )
                else:
                    connection.execute(
                        """
                        DELETE FROM session_plan_comments
                        WHERE session_id = ? AND plan_fingerprint != ?
                        """,
                        (session_id, plan.fingerprint),
                    )

        async with self._write_lock:
            await run_blocking(save)

    async def load_session_plan(self, session_id: str) -> SessionPlan | None:
        def load() -> SessionPlan | None:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT plan_json FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
            if row is None:
                raise SessionError(f"unknown session: {session_id}")
            payload = row[0]
            if not isinstance(payload, str) or not payload:
                return None
            try:
                return SessionPlan.from_dict(json.loads(payload))
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise SessionError(f"session {session_id} contains an invalid plan") from error

        return await run_blocking(load)

    async def save_execution_record(
        self,
        session_id: str,
        record: SessionExecutionRecord,
    ) -> None:
        if not isinstance(record, SessionExecutionRecord):
            raise TypeError("record must be a SessionExecutionRecord")

        def save() -> None:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                exists = connection.execute(
                    "SELECT 1 FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if exists is None:
                    raise SessionError(f"unknown session: {session_id}")
                event = connection.execute(
                    """
                    SELECT kind FROM events
                    WHERE session_id = ? AND sequence = ?
                    """,
                    (session_id, record.event_sequence),
                ).fetchone()
                if event is None or event[0] != "turn_completed":
                    raise SessionError(
                        "execution record must reference a persisted turn-completed event"
                    )
                _validate_execution_record_order(
                    connection,
                    session_id=session_id,
                    incoming=record,
                )
                connection.execute(
                    """
                    INSERT INTO session_execution_records(
                        session_id, event_sequence, status, reason_code,
                        finalized, recoverable, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        event_sequence = excluded.event_sequence,
                        status = excluded.status,
                        reason_code = excluded.reason_code,
                        finalized = excluded.finalized,
                        recoverable = excluded.recoverable,
                        completed_at = excluded.completed_at
                    """,
                    (
                        session_id,
                        record.event_sequence,
                        record.outcome.status.value,
                        (
                            record.outcome.reason_code.value
                            if record.outcome.reason_code is not None
                            else None
                        ),
                        int(record.outcome.finalized),
                        int(record.outcome.recoverable),
                        record.completed_at.isoformat(),
                    ),
                )
                connection.execute(
                    "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (session_id,),
                )

        async with self._write_lock:
            await run_blocking(save)

    async def save_compaction_item(
        self,
        session_id: str,
        item: DurableCompactionItem,
    ) -> None:
        if not isinstance(item, DurableCompactionItem):
            raise TypeError("item must be a DurableCompactionItem")

        def save() -> None:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _persist_compaction_item(connection, session_id, item)
                    connection.commit()
                except sqlite3.IntegrityError as error:
                    connection.rollback()
                    raise SessionError(
                        f"cannot save compaction item {item.compaction_id}"
                    ) from error
                except BaseException:
                    connection.rollback()
                    raise

        async with self._write_lock:
            await run_blocking(save)

    async def load_compaction_items(self, session_id: str) -> list[DurableCompactionItem]:
        def load() -> list[DurableCompactionItem]:
            with closing(self._connect()) as connection:
                exists = connection.execute(
                    "SELECT 1 FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if exists is None:
                    raise SessionError(f"unknown session: {session_id}")
                rows = connection.execute(
                    """
                    SELECT compaction_id, session_id, provider_name, model_name,
                           capacity_tokens, context_affinity, source_item_count,
                           protected_item_count, recent_item_count, candidate_start,
                           candidate_end, target_tokens, summary_tokens,
                           source_fingerprint, summary, summary_redacted,
                           summary_truncated, created_at
                    FROM session_compaction_items
                    WHERE session_id = ?
                    ORDER BY created_at ASC, compaction_id ASC
                    """,
                    (session_id,),
                ).fetchall()
            try:
                return [_compaction_item_from_row(row) for row in rows]
            except (TypeError, ValueError) as error:
                raise SessionError(
                    f"session {session_id} contains an invalid compaction item"
                ) from error

        return await run_blocking(load)

    async def load_execution_record(self, session_id: str) -> SessionExecutionRecord | None:
        def load() -> SessionExecutionRecord | None:
            with closing(self._connect()) as connection:
                exists = connection.execute(
                    "SELECT 1 FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if exists is None:
                    raise SessionError(f"unknown session: {session_id}")
                row = connection.execute(
                    """
                    SELECT event_sequence, status, reason_code, finalized, recoverable, completed_at
                    FROM session_execution_records
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if row is None:
                    return None
                event = connection.execute(
                    """
                    SELECT kind
                    FROM events
                    WHERE session_id = ? AND sequence = ?
                    """,
                    (session_id, row[0]),
                ).fetchone()
                if event is None or event[0] != AgentEventKind.TURN_COMPLETED.value:
                    raise SessionError(
                        f"session {session_id} execution record references an invalid completion event"
                    )
            return _session_execution_record_from_row(row, session_id=session_id)

        return await run_blocking(load)

    async def load_execution_records(
        self,
        session_ids: Sequence[str],
    ) -> tuple[SessionExecutionRecord | None, ...]:
        """Load an ordered execution projection in one read snapshot.

        The result preserves the requested ID order, including duplicate IDs.
        A known session without a record returns ``None``; an unknown session
        or a record that does not point at a persisted ``TURN_COMPLETED`` event
        preserves the single-record loader's ``SessionError`` semantics.

        在一次只读快照中加载有序的执行投影.
        返回值保留请求 ID 的顺序,包括重复 ID. 已知但没有记录的会话返回 ``None``;
        未知会话或未指向已持久化 ``TURN_COMPLETED`` 事件的记录保持单条加载器的
        ``SessionError`` 语义.
        """

        requested_ids = tuple(session_ids)
        if not requested_ids:
            return ()
        if any(not isinstance(session_id, str) or not session_id for session_id in requested_ids):
            raise SessionError("session execution record IDs must be non-empty strings")

        def load() -> tuple[SessionExecutionRecord | None, ...]:
            records: dict[str, SessionExecutionRecord | None] = {}
            unique_ids = tuple(dict.fromkeys(requested_ids))
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                try:
                    for start in range(0, len(unique_ids), 500):
                        chunk = unique_ids[start : start + 500]
                        placeholders = ", ".join("?" for _ in chunk)
                        rows = connection.execute(
                            f"""
                            SELECT s.id,
                                   r.event_sequence, r.status, r.reason_code,
                                   r.finalized, r.recoverable, r.completed_at,
                                   e.kind
                            FROM sessions AS s
                            LEFT JOIN session_execution_records AS r
                              ON r.session_id = s.id
                            LEFT JOIN events AS e
                              ON e.session_id = r.session_id
                             AND e.sequence = r.event_sequence
                            WHERE s.id IN ({placeholders})
                            """,
                            chunk,
                        ).fetchall()
                        for row in rows:
                            session_id = str(row[0])
                            if row[1] is None:
                                records[session_id] = None
                                continue
                            if row[7] != AgentEventKind.TURN_COMPLETED.value:
                                raise SessionError(
                                    f"session {session_id} execution record references "
                                    "an invalid completion event"
                                )
                            records[session_id] = _session_execution_record_from_row(
                                row[1:7],
                                session_id=session_id,
                            )
                    for session_id in unique_ids:
                        if session_id not in records:
                            raise SessionError(f"unknown session: {session_id}")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            return tuple(records[session_id] for session_id in requested_ids)

        return await run_blocking(load)

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

    async def add_plan_comment(
        self,
        session_id: str,
        plan: SessionPlan,
        comment: PlanComment,
    ) -> None:
        if not isinstance(plan, SessionPlan):
            raise TypeError("plan must be a SessionPlan")
        if not isinstance(comment, PlanComment):
            raise TypeError("comment must be a PlanComment")
        if comment.step_index > len(plan.steps):
            raise SessionError("plan comment refers to an unknown step")

        def add() -> None:
            try:
                with closing(self._connect()) as connection, connection:
                    row = connection.execute(
                        "SELECT plan_json FROM sessions WHERE id = ?",
                        (session_id,),
                    ).fetchone()
                    if row is None:
                        raise SessionError(f"unknown session: {session_id}")
                    payload = row[0]
                    if not isinstance(payload, str) or not payload:
                        raise SessionError("session has no saved plan")
                    try:
                        current_plan = SessionPlan.from_dict(json.loads(payload))
                    except (TypeError, ValueError, json.JSONDecodeError) as error:
                        raise SessionError(
                            f"session {session_id} contains an invalid plan"
                        ) from error
                    if current_plan.fingerprint != plan.fingerprint:
                        raise SessionError("session plan changed before the comment was saved")
                    comment_count = connection.execute(
                        """
                        SELECT COUNT(*) FROM session_plan_comments
                        WHERE session_id = ? AND plan_fingerprint = ?
                        """,
                        (session_id, plan.fingerprint),
                    ).fetchone()
                    if comment_count is None or int(comment_count[0]) >= MAX_PLAN_COMMENTS:
                        raise SessionError("plan comment limit reached")
                    connection.execute(
                        """
                        INSERT INTO session_plan_comments(
                            comment_id, session_id, plan_fingerprint,
                            step_index, content, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            comment.comment_id,
                            session_id,
                            plan.fingerprint,
                            comment.step_index,
                            comment.content,
                            comment.created_at.isoformat(),
                        ),
                    )
                    connection.execute(
                        "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (session_id,),
                    )
            except sqlite3.IntegrityError as error:
                raise SessionError(f"cannot save plan comment: {comment.comment_id}") from error

        async with self._write_lock:
            await run_blocking(add)

    async def list_plan_comments(
        self,
        session_id: str,
        plan: SessionPlan,
    ) -> list[PlanComment]:
        if not isinstance(plan, SessionPlan):
            raise TypeError("plan must be a SessionPlan")

        def load() -> list[PlanComment]:
            with closing(self._connect()) as connection:
                session = connection.execute(
                    "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if session is None:
                    raise SessionError(f"unknown session: {session_id}")
                rows = connection.execute(
                    """
                    SELECT comment_id, step_index, content, created_at
                    FROM session_plan_comments
                    WHERE session_id = ? AND plan_fingerprint = ?
                    ORDER BY created_at ASC, comment_id ASC
                    """,
                    (session_id, plan.fingerprint),
                ).fetchall()
            return [_plan_comment_from_row(row, session_id=session_id) for row in rows]

        return await run_blocking(load)

    async def create_session_task(self, session_id: str, task: SessionTask) -> None:
        if not isinstance(task, SessionTask):
            raise TypeError("task must be a SessionTask")
        plan_snapshot_json = (
            json.dumps(
                task.plan_snapshot.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if task.plan_snapshot is not None
            else ""
        )

        def create() -> None:
            try:
                with closing(self._connect()) as connection, connection:
                    if (
                        task.kind is SessionTaskKind.PLAN_EXECUTION
                        and task.status is SessionTaskStatus.QUEUED
                    ):
                        queued = connection.execute(
                            """
                            SELECT COUNT(*)
                            FROM session_tasks
                            WHERE session_id = ? AND kind = ? AND status = ?
                            """,
                            (
                                session_id,
                                SessionTaskKind.PLAN_EXECUTION.value,
                                SessionTaskStatus.QUEUED.value,
                            ),
                        ).fetchone()
                        if queued is not None and int(queued[0]) >= MAX_QUEUED_SESSION_TASKS:
                            raise SessionError(
                                f"at most {MAX_QUEUED_SESSION_TASKS} plan tasks may be queued"
                            )
                    connection.execute(
                        """
                        INSERT INTO session_tasks(
                            task_id, session_id, kind, status, started_at, finished_at,
                            plan_snapshot_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            task.task_id,
                            session_id,
                            task.kind.value,
                            task.status.value,
                            task.started_at.isoformat(),
                            task.finished_at.isoformat() if task.finished_at else None,
                            plan_snapshot_json,
                        ),
                    )
                    connection.execute(
                        "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (session_id,),
                    )
            except sqlite3.IntegrityError as error:
                raise SessionError(f"cannot create session task: {task.task_id}") from error

        async with self._write_lock:
            await run_blocking(create)

    async def start_session_task(
        self,
        session_id: str,
        task_id: str,
        started_at: datetime,
    ) -> SessionTask:
        """Atomically claim one queued task for explicit execution.

        原子认领一个排队任务,供明确执行."""

        _validated_session_task_id(task_id)

        def start() -> SessionTask:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT task_id, kind, status, started_at, finished_at, plan_snapshot_json
                    FROM session_tasks
                    WHERE session_id = ? AND task_id = ?
                    """,
                    (session_id, task_id),
                ).fetchone()
                if row is None:
                    raise SessionError(f"unknown session task: {task_id}")
                current = _session_task_from_row(row, session_id=session_id)
                try:
                    claimed = current.start(started_at=started_at)
                except ValueError as error:
                    raise SessionError(f"invalid session task transition: {task_id}") from error
                connection.execute(
                    """
                    UPDATE session_tasks
                    SET status = ?, started_at = ?, finished_at = NULL
                    WHERE session_id = ? AND task_id = ?
                    """,
                    (
                        claimed.status.value,
                        claimed.started_at.isoformat(),
                        session_id,
                        task_id,
                    ),
                )
                connection.execute(
                    "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (session_id,),
                )
                connection.commit()
                return claimed
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(start)

    async def update_session_task(self, session_id: str, task: SessionTask) -> None:
        if not isinstance(task, SessionTask):
            raise TypeError("task must be a SessionTask")

        def update() -> None:
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    """
                    SELECT task_id, kind, status, started_at, finished_at, plan_snapshot_json
                    FROM session_tasks
                    WHERE session_id = ? AND task_id = ?
                    """,
                    (session_id, task.task_id),
                ).fetchone()
                if row is None:
                    raise SessionError(f"unknown session task: {task.task_id}")
                current = _session_task_from_row(row, session_id=session_id)
                try:
                    if task.finished_at is None:
                        raise ValueError("session task finish time is missing")
                    expected = current.finish(task.status, finished_at=task.finished_at)
                except ValueError as error:
                    raise SessionError(
                        f"invalid session task transition: {task.task_id}"
                    ) from error
                if task != expected:
                    raise SessionError(f"invalid session task transition: {task.task_id}")
                connection.execute(
                    """
                    UPDATE session_tasks
                    SET status = ?, finished_at = ?
                    WHERE session_id = ? AND task_id = ?
                    """,
                    (
                        task.status.value,
                        task.finished_at.isoformat() if task.finished_at else None,
                        session_id,
                        task.task_id,
                    ),
                )
                connection.execute(
                    "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (session_id,),
                )

        async with self._write_lock:
            await run_blocking(update)

    async def list_session_tasks(
        self,
        session_id: str,
        *,
        limit: int = 50,
    ) -> list[SessionTask]:
        if limit <= 0:
            raise SessionError("session task limit must be positive")

        def load() -> list[SessionTask]:
            with closing(self._connect()) as connection:
                session = connection.execute(
                    "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if session is None:
                    raise SessionError(f"unknown session: {session_id}")
                rows = connection.execute(
                    """
                    SELECT task_id, kind, status, started_at, finished_at, plan_snapshot_json
                    FROM session_tasks
                    WHERE session_id = ?
                    ORDER BY started_at DESC, task_id DESC
                    LIMIT ?
                    """,
                    (session_id, limit),
                ).fetchall()
            return [_session_task_from_row(row, session_id=session_id) for row in rows]

        return await run_blocking(load)

    async def get_session_task(self, session_id: str, task_id: str) -> SessionTask | None:
        _validated_session_task_id(task_id)

        def load() -> SessionTask | None:
            with closing(self._connect()) as connection:
                session = connection.execute(
                    "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if session is None:
                    raise SessionError(f"unknown session: {session_id}")
                row = connection.execute(
                    """
                    SELECT task_id, kind, status, started_at, finished_at, plan_snapshot_json
                    FROM session_tasks
                    WHERE session_id = ? AND task_id = ?
                    """,
                    (session_id, task_id),
                ).fetchone()
            return _session_task_from_row(row, session_id=session_id) if row is not None else None

        return await run_blocking(load)

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


def _ensure_base_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            version INTEGER NOT NULL
        )
        """
    )
    connection.execute("INSERT OR IGNORE INTO schema_meta(singleton, version) VALUES (1, 1)")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            cwd TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            messages_json TEXT NOT NULL DEFAULT '[]'
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            data_json TEXT NOT NULL,
            PRIMARY KEY (session_id, sequence),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )


def _ensure_search_schema(connection: sqlite3.Connection) -> None:
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS session_search_documents (
                session_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS session_search_fts USING fts5(
                title,
                content,
                content = 'session_search_documents',
                content_rowid = 'rowid',
                tokenize = 'unicode61 remove_diacritics 2'
            )
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS session_search_documents_ai
            AFTER INSERT ON session_search_documents BEGIN
                INSERT INTO session_search_fts(rowid, title, content)
                VALUES (new.rowid, new.title, new.content);
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS session_search_documents_ad
            AFTER DELETE ON session_search_documents BEGIN
                INSERT INTO session_search_fts(session_search_fts, rowid, title, content)
                VALUES ('delete', old.rowid, old.title, old.content);
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS session_search_documents_au
            AFTER UPDATE OF title, content ON session_search_documents BEGIN
                INSERT INTO session_search_fts(session_search_fts, rowid, title, content)
                VALUES ('delete', old.rowid, old.title, old.content);
                INSERT INTO session_search_fts(rowid, title, content)
                VALUES (new.rowid, new.title, new.content);
            END
            """
        )
    except sqlite3.OperationalError as error:
        if "fts5" in str(error).casefold():
            raise SessionError("the installed SQLite build does not support FTS5") from error
        raise


def _ensure_session_alias_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session_aliases (
            namespace TEXT NOT NULL,
            external_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (namespace, external_id),
            UNIQUE (namespace, session_id),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )


def _ensure_session_plan_schema(connection: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(sessions)").fetchall()}
    if "plan_json" not in columns:
        connection.execute("ALTER TABLE sessions ADD COLUMN plan_json TEXT NOT NULL DEFAULT ''")


def _ensure_session_task_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session_tasks (
            task_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            plan_snapshot_json TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(session_tasks)").fetchall()
    }
    if "plan_snapshot_json" not in columns:
        connection.execute(
            "ALTER TABLE session_tasks ADD COLUMN plan_snapshot_json TEXT NOT NULL DEFAULT ''"
        )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS session_tasks_by_session_started
        ON session_tasks(session_id, started_at DESC, task_id DESC)
        """
    )


def _ensure_session_plan_comment_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session_plan_comments (
            comment_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            plan_fingerprint TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS session_plan_comments_by_current_plan
        ON session_plan_comments(session_id, plan_fingerprint, created_at, comment_id)
        """
    )


def _ensure_session_execution_record_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session_execution_records (
            session_id TEXT PRIMARY KEY,
            event_sequence INTEGER NOT NULL CHECK (event_sequence > 0),
            status TEXT NOT NULL,
            reason_code TEXT,
            finalized INTEGER NOT NULL CHECK (finalized IN (0, 1)),
            recoverable INTEGER NOT NULL CHECK (recoverable IN (0, 1)),
            completed_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )


def _ensure_session_background_wake_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session_background_wake_state (
            session_id TEXT PRIMARY KEY,
            announced_task_ids_json TEXT NOT NULL,
            pending_task_ids_json TEXT NOT NULL,
            wake_count INTEGER NOT NULL CHECK (wake_count >= 0),
            last_wake_at TEXT,
            wake_in_flight INTEGER NOT NULL CHECK (wake_in_flight IN (0, 1)),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )


def _ensure_subagent_link_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS subagent_links (
            parent_session_id TEXT NOT NULL,
            parent_task_id TEXT NOT NULL,
            child_session_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (parent_session_id, parent_task_id),
            FOREIGN KEY (parent_session_id) REFERENCES sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (parent_task_id) REFERENCES session_tasks(task_id) ON DELETE CASCADE,
            FOREIGN KEY (child_session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS subagent_links_by_child
        ON subagent_links(child_session_id)
        """
    )


def _ensure_session_compaction_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session_compaction_items (
            compaction_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            provider_name TEXT NOT NULL,
            model_name TEXT NOT NULL,
            capacity_tokens INTEGER NOT NULL CHECK (capacity_tokens > 0),
            context_affinity TEXT,
            source_item_count INTEGER NOT NULL CHECK (source_item_count > 0),
            protected_item_count INTEGER NOT NULL CHECK (protected_item_count >= 0),
            recent_item_count INTEGER NOT NULL CHECK (recent_item_count >= 0),
            candidate_start INTEGER NOT NULL CHECK (candidate_start >= 0),
            candidate_end INTEGER NOT NULL CHECK (candidate_end > candidate_start),
            target_tokens INTEGER NOT NULL CHECK (target_tokens > 0),
            summary_tokens INTEGER NOT NULL CHECK (summary_tokens > 0),
            source_fingerprint TEXT NOT NULL,
            summary TEXT NOT NULL,
            summary_redacted INTEGER NOT NULL CHECK (summary_redacted = 1),
            summary_truncated INTEGER NOT NULL CHECK (summary_truncated IN (0, 1)),
            created_at TEXT NOT NULL,
            UNIQUE(session_id, source_fingerprint, candidate_start, candidate_end),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS session_compaction_items_by_session
        ON session_compaction_items(session_id, created_at ASC, compaction_id ASC)
        """
    )


def _ensure_session_turn_attempt_schema(connection: sqlite3.Connection) -> None:
    """Create the canonical crash-recovery attempt projection.

    The row is the small source of truth for accepted input, sticky lifecycle
    facts, and explicit resolution.  The append-only events table remains the
    ordered audit evidence and is updated in the same transaction as each fact.
    """

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session_turn_attempts (
            turn_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            source TEXT NOT NULL,
            task_id TEXT,
            input_json TEXT NOT NULL DEFAULT '',
            input_fingerprint TEXT NOT NULL,
            input_reconstructable INTEGER NOT NULL CHECK (input_reconstructable IN (0, 1)),
            accepted_at TEXT NOT NULL,
            resolution TEXT,
            resolution_at TEXT,
            request_started_count INTEGER NOT NULL DEFAULT 0 CHECK (request_started_count >= 0),
            request_id TEXT,
            step INTEGER,
            provider TEXT,
            model TEXT,
            output_started INTEGER NOT NULL DEFAULT 0 CHECK (output_started IN (0, 1)),
            tool_started_count INTEGER NOT NULL DEFAULT 0 CHECK (tool_started_count >= 0),
            side_effecting_tool_started INTEGER NOT NULL DEFAULT 0
                CHECK (side_effecting_tool_started IN (0, 1)),
            last_tool_id TEXT,
            last_tool_name TEXT,
            last_stage TEXT NOT NULL DEFAULT 'accepted',
            last_stage_at TEXT,
            fact_conflict INTEGER NOT NULL DEFAULT 0 CHECK (fact_conflict IN (0, 1)),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS session_turn_attempts_by_session_status
        ON session_turn_attempts(session_id, resolution, accepted_at DESC, turn_id DESC)
        """
    )


def _compaction_item_from_row(row: Sequence[object]) -> DurableCompactionItem:
    (
        compaction_id,
        _session_id,
        provider_name,
        model_name,
        capacity_tokens,
        context_affinity,
        source_item_count,
        protected_item_count,
        recent_item_count,
        candidate_start,
        candidate_end,
        target_tokens,
        summary_tokens,
        source_fingerprint,
        summary,
        summary_redacted,
        summary_truncated,
        created_at,
    ) = row
    if not isinstance(compaction_id, str):
        raise ValueError("compaction item labels are invalid")
    if not isinstance(provider_name, str):
        raise ValueError("compaction item provider is invalid")
    if not isinstance(model_name, str):
        raise ValueError("compaction item model is invalid")
    if context_affinity is not None and not isinstance(context_affinity, str):
        raise ValueError("compaction item context affinity is invalid")
    if not isinstance(summary, str) or not isinstance(source_fingerprint, str):
        raise ValueError("compaction item summary fields are invalid")
    if not isinstance(summary_redacted, int) or isinstance(summary_redacted, bool):
        raise ValueError("compaction item redaction flag is invalid")
    if not isinstance(summary_truncated, int) or isinstance(summary_truncated, bool):
        raise ValueError("compaction item truncation flag is invalid")
    if not isinstance(created_at, str):
        raise ValueError("compaction item timestamp is invalid")

    def row_int(value: object, name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"compaction item {name} is invalid")
        return value

    return DurableCompactionItem(
        compaction_id=compaction_id,
        provider_name=provider_name,
        model_name=model_name,
        capacity_tokens=row_int(capacity_tokens, "capacity"),
        context_affinity=context_affinity,
        source_item_count=row_int(source_item_count, "source count"),
        protected_item_count=row_int(protected_item_count, "protected count"),
        recent_item_count=row_int(recent_item_count, "recent count"),
        candidate_range=(
            row_int(candidate_start, "candidate start"),
            row_int(candidate_end, "candidate end"),
        ),
        target_tokens=row_int(target_tokens, "target tokens"),
        summary_tokens=row_int(summary_tokens, "summary tokens"),
        source_fingerprint=source_fingerprint,
        summary=summary,
        summary_redacted=summary_redacted == 1,
        summary_truncated=summary_truncated == 1,
        created_at=datetime.fromisoformat(created_at),
    )


def _plan_comment_from_row(row: Sequence[object], *, session_id: str) -> PlanComment:
    try:
        comment_id, raw_step_index, content, raw_created_at = row
        if not isinstance(raw_step_index, int) or isinstance(raw_step_index, bool):
            raise ValueError("plan comment step index is invalid")
        return PlanComment(
            str(comment_id),
            raw_step_index,
            str(content),
            datetime.fromisoformat(str(raw_created_at)),
        )
    except (TypeError, ValueError) as error:
        raise SessionError(f"session {session_id} contains an invalid plan comment") from error


def _session_execution_record_from_row(
    row: Sequence[object],
    *,
    session_id: str,
) -> SessionExecutionRecord:
    try:
        (
            raw_event_sequence,
            raw_status,
            raw_reason_code,
            raw_finalized,
            raw_recoverable,
            raw_completed_at,
        ) = row
        if not isinstance(raw_event_sequence, int) or isinstance(raw_event_sequence, bool):
            raise ValueError("event sequence is invalid")
        if raw_finalized not in (0, 1) or isinstance(raw_finalized, bool):
            raise ValueError("finalized flag is invalid")
        if raw_recoverable not in (0, 1) or isinstance(raw_recoverable, bool):
            raise ValueError("recoverable flag is invalid")
        reason_code = (
            SupervisorReasonCode(str(raw_reason_code)) if raw_reason_code is not None else None
        )
        return SessionExecutionRecord(
            AgentExecutionOutcome(
                AgentExecutionStatus(str(raw_status)),
                reason_code,
                bool(raw_finalized),
                bool(raw_recoverable),
            ),
            raw_event_sequence,
            datetime.fromisoformat(str(raw_completed_at)),
        )
    except (TypeError, ValueError) as error:
        raise SessionError(f"session {session_id} contains an invalid execution record") from error


def _validate_execution_record_order(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    incoming: SessionExecutionRecord,
) -> None:
    row = connection.execute(
        """
        SELECT event_sequence, status, reason_code, finalized, recoverable, completed_at
        FROM session_execution_records
        WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return
    current = _session_execution_record_from_row(row, session_id=session_id)
    if incoming.event_sequence < current.event_sequence:
        raise SessionError("cannot replace a newer execution record with an older event sequence")
    if incoming.event_sequence == current.event_sequence and incoming != current:
        raise SessionError("conflicting execution records use the same event sequence")


def _insert_event_row(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    event: AgentEvent,
    payload: str | None = None,
) -> None:
    """Insert one already-created event into an open transaction."""

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
            payload
            if payload is not None
            else json.dumps(dict(event.data), ensure_ascii=False, separators=(",", ":")),
        ),
    )
    connection.execute(
        "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (session_id,),
    )


def _persist_task_terminal(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    task: SessionTask | None,
    task_event: AgentEvent | None,
    before_sequence: int,
) -> None:
    """Apply a task terminal transition inside the owning turn transaction."""

    if task is None:
        if task_event is not None:
            raise SessionError("task event cannot exist without a task")
        return
    if task_event is None:
        raise SessionError("a terminal task requires a task event")
    expected_kind = {
        SessionTaskStatus.COMPLETED: AgentEventKind.SESSION_TASK_COMPLETED,
        SessionTaskStatus.FAILED: AgentEventKind.SESSION_TASK_FAILED,
        SessionTaskStatus.CANCELLED: AgentEventKind.SESSION_TASK_CANCELLED,
    }.get(task.status)
    if expected_kind is None or task_event.kind is not expected_kind:
        raise SessionError("task terminal event does not match task status")
    if task_event.sequence >= before_sequence:
        raise SessionError("task terminal event must precede the turn terminal event")
    row = connection.execute(
        """
        SELECT task_id, kind, status, started_at, finished_at, plan_snapshot_json
        FROM session_tasks
        WHERE session_id = ? AND task_id = ?
        """,
        (session_id, task.task_id),
    ).fetchone()
    if row is None:
        raise SessionError(f"unknown session task: {task.task_id}")
    current = _session_task_from_row(row, session_id=session_id)
    try:
        if task.finished_at is None:
            raise ValueError("session task finish time is missing")
        expected = current.finish(task.status, finished_at=task.finished_at)
    except ValueError as error:
        raise SessionError(f"invalid session task transition: {task.task_id}") from error
    if task != expected:
        raise SessionError(f"invalid session task transition: {task.task_id}")
    connection.execute(
        """
        UPDATE session_tasks
        SET status = ?, finished_at = ?
        WHERE session_id = ? AND task_id = ?
        """,
        (
            task.status.value,
            task.finished_at.isoformat() if task.finished_at is not None else None,
            session_id,
            task.task_id,
        ),
    )
    _insert_event_row(connection, session_id=session_id, event=task_event)


def _turn_recovery_attempt_from_row(row: Sequence[object]) -> TurnRecoveryAttempt:
    try:
        (
            raw_turn_id,
            raw_session_id,
            raw_source,
            raw_task_id,
            raw_input_json,
            raw_fingerprint,
            raw_input_reconstructable,
            raw_accepted_at,
            raw_resolution,
            raw_resolution_at,
            raw_request_count,
            raw_request_id,
            raw_step,
            raw_provider,
            raw_model,
            raw_output_started,
            raw_tool_count,
            raw_side_effecting,
            raw_tool_id,
            raw_tool_name,
            raw_stage,
            raw_stage_at,
            raw_conflict,
        ) = row
        source = TurnSource(str(raw_source))
        resolution = (
            TurnRecoveryResolution(str(raw_resolution)) if raw_resolution is not None else None
        )
        stage = TurnRecoveryStage(str(raw_stage))
        if not isinstance(raw_input_reconstructable, int) or raw_input_reconstructable not in (
            0,
            1,
        ):
            raise ValueError("input reconstructable flag is invalid")
        if not isinstance(raw_output_started, int) or raw_output_started not in (0, 1):
            raise ValueError("output started flag is invalid")
        if not isinstance(raw_side_effecting, int) or raw_side_effecting not in (0, 1):
            raise ValueError("side-effecting flag is invalid")
        if not isinstance(raw_conflict, int) or raw_conflict not in (0, 1):
            raise ValueError("fact conflict flag is invalid")
        input_value: TurnInput | None = None
        input_reconstructable = bool(raw_input_reconstructable)
        fact_conflict = bool(raw_conflict)
        if isinstance(raw_input_json, str) and raw_input_json:
            try:
                parsed = TurnInput.from_dict(json.loads(raw_input_json))
                if parsed.fingerprint != str(raw_fingerprint):
                    input_reconstructable = False
                    fact_conflict = True
                elif input_reconstructable:
                    input_value = parsed
            except (TypeError, ValueError, json.JSONDecodeError):
                input_reconstructable = False
                fact_conflict = True
        else:
            input_reconstructable = False

        def parse_time(value: object) -> datetime | None:
            return datetime.fromisoformat(str(value)) if value is not None else None

        def parse_integer(value: object, field_name: str) -> int:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{field_name} is invalid")
            return value

        return TurnRecoveryAttempt(
            str(raw_turn_id),
            str(raw_session_id),
            source,
            str(raw_task_id) if raw_task_id is not None else None,
            str(raw_fingerprint),
            input_value,
            input_reconstructable,
            datetime.fromisoformat(str(raw_accepted_at)),
            resolution,
            parse_time(raw_resolution_at),
            parse_integer(raw_request_count, "request count"),
            str(raw_request_id) if raw_request_id is not None else None,
            parse_integer(raw_step, "step") if raw_step is not None else None,
            str(raw_provider) if raw_provider is not None else None,
            str(raw_model) if raw_model is not None else None,
            bool(raw_output_started),
            parse_integer(raw_tool_count, "tool count"),
            bool(raw_side_effecting),
            str(raw_tool_id) if raw_tool_id is not None else None,
            str(raw_tool_name) if raw_tool_name is not None else None,
            stage,
            parse_time(raw_stage_at),
            fact_conflict,
        )
    except (TypeError, ValueError) as error:
        raise SessionError("session contains an invalid turn recovery attempt") from error


def _persist_finalized_turn(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    event: AgentEvent,
    items: Sequence[SessionItem],
    record: SessionExecutionRecord | None,
    compaction_item: DurableCompactionItem | None,
    turn_id: str | None,
    task: SessionTask | None,
    task_event: AgentEvent | None,
) -> None:
    """Write all owned turn-finalization projections on one open transaction.

    This helper deliberately does not begin, commit, or roll back a
    transaction.  Its callers own those boundaries so the opt-in compaction
    variant can share exactly the same atomic unit as ordinary finalization.

    在一个已打开的事务中写入回合最终化所拥有的全部投影.

    此辅助函数刻意不开始、提交或回滚事务. 事务边界由调用方拥有,因此显式压缩
    变体可以与普通最终化共享完全相同的原子单元.
    """

    row = connection.execute(
        "SELECT messages_json, title FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise SessionError(f"unknown session: {session_id}")
    try:
        current_items = _session_items_from_json(row[0])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise SessionError(f"session {session_id} contains invalid session items") from error
    if len(items) < len(current_items) or list(items)[: len(current_items)] != current_items:
        raise SessionError("cannot rewrite the persisted session item prefix")
    if record is not None:
        _validate_execution_record_order(
            connection,
            session_id=session_id,
            incoming=record,
        )
    if turn_id is not None and event.data.get("turn_id") != turn_id:
        raise SessionError("completion event has a different turn identity")
    _persist_task_terminal(
        connection,
        session_id=session_id,
        task=task,
        task_event=task_event,
        before_sequence=event.sequence,
    )
    duplicate = connection.execute(
        "SELECT 1 FROM events WHERE session_id = ? AND sequence = ?",
        (session_id, event.sequence),
    ).fetchone()
    if duplicate is not None:
        raise SessionError(f"completion event sequence {event.sequence} already exists")
    payload = json.dumps(dict(event.data), ensure_ascii=False, separators=(",", ":"))
    items_payload = _serialize_session_items(items)
    title = str(row[1]) or fallback_session_title(items)
    _insert_event_row(connection, session_id=session_id, event=event, payload=payload)
    cursor = connection.execute(
        """
        UPDATE sessions
        SET messages_json = ?, title = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (items_payload, title, session_id),
    )
    if cursor.rowcount != 1:
        raise SessionError(f"unknown session: {session_id}")
    _upsert_search_document(
        connection,
        session_id=session_id,
        title=title,
        content=searchable_session_text(items),
    )
    if record is not None:
        connection.execute(
            """
            INSERT INTO session_execution_records(
                session_id, event_sequence, status, reason_code,
                finalized, recoverable, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                event_sequence = excluded.event_sequence,
                status = excluded.status,
                reason_code = excluded.reason_code,
                finalized = excluded.finalized,
                recoverable = excluded.recoverable,
                completed_at = excluded.completed_at
            """,
            (
                session_id,
                record.event_sequence,
                record.outcome.status.value,
                (
                    record.outcome.reason_code.value
                    if record.outcome.reason_code is not None
                    else None
                ),
                int(record.outcome.finalized),
                int(record.outcome.recoverable),
                record.completed_at.isoformat(),
            ),
        )
    if compaction_item is not None:
        _persist_compaction_item(connection, session_id, compaction_item)
    if turn_id is not None:
        _resolve_turn_attempt(
            connection,
            session_id=session_id,
            turn_id=turn_id,
            resolution=TurnRecoveryResolution.COMMITTED,
            resolved_at=event.created_at,
            stage=TurnRecoveryStage.TURN_COMPLETED,
        )


def _persist_failed_turn(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    turn_id: str | None,
    event: AgentEvent,
    items: Sequence[SessionItem],
    resolution: str,
    task: SessionTask | None,
    task_event: AgentEvent | None,
) -> None:
    """Write failure/cancellation projections in one open transaction."""

    row = connection.execute(
        "SELECT messages_json, title FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise SessionError(f"unknown session: {session_id}")
    try:
        current_items = _session_items_from_json(row[0])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise SessionError(f"session {session_id} contains invalid session items") from error
    if len(items) < len(current_items) or list(items)[: len(current_items)] != current_items:
        raise SessionError("cannot rewrite the persisted session item prefix")
    if turn_id is not None and event.data.get("turn_id") != turn_id:
        raise SessionError("failure event has a different turn identity")
    _persist_task_terminal(
        connection,
        session_id=session_id,
        task=task,
        task_event=task_event,
        before_sequence=event.sequence,
    )
    _insert_event_row(connection, session_id=session_id, event=event)
    items_payload = _serialize_session_items(items)
    title = str(row[1]) or fallback_session_title(items)
    cursor = connection.execute(
        """
        UPDATE sessions
        SET messages_json = ?, title = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (items_payload, title, session_id),
    )
    if cursor.rowcount != 1:
        raise SessionError(f"unknown session: {session_id}")
    _upsert_search_document(
        connection,
        session_id=session_id,
        title=title,
        content=searchable_session_text(items),
    )
    if turn_id is not None:
        _resolve_turn_attempt(
            connection,
            session_id=session_id,
            turn_id=turn_id,
            resolution=TurnRecoveryResolution(resolution),
            resolved_at=event.created_at,
            stage=TurnRecoveryStage.TURN_FAILED,
        )


def _resolve_turn_attempt(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    turn_id: str,
    resolution: TurnRecoveryResolution,
    resolved_at: datetime,
    stage: TurnRecoveryStage,
) -> None:
    row = connection.execute(
        """
        SELECT resolution
        FROM session_turn_attempts
        WHERE session_id = ? AND turn_id = ?
        """,
        (session_id, turn_id),
    ).fetchone()
    if row is None:
        raise SessionError(f"unknown turn attempt: {turn_id}")
    if row[0] is not None:
        raise SessionError(f"turn attempt is already resolved: {turn_id}")
    updated = connection.execute(
        """
        UPDATE session_turn_attempts
        SET resolution = ?, resolution_at = ?, last_stage = ?, last_stage_at = ?
        WHERE session_id = ? AND turn_id = ? AND resolution IS NULL
        """,
        (
            resolution.value,
            resolved_at.isoformat(),
            stage.value,
            resolved_at.isoformat(),
            session_id,
            turn_id,
        ),
    )
    if updated.rowcount != 1:
        raise SessionError(f"cannot resolve turn attempt: {turn_id}")


def _persist_compaction_item(
    connection: sqlite3.Connection,
    session_id: str,
    item: DurableCompactionItem,
) -> None:
    """Insert one compaction item into an already-open transaction.

    An identical existing ID is idempotent; an owner or payload conflict is
    rejected.  The helper never commits so callers can compose it with turn
    finalization atomically.

    在一个已打开的事务中插入一个压缩条目.

    已存在且完全相同的 ID 具有幂等性; 所有者或载荷冲突都会被拒绝. 该辅助函数
    从不提交事务,因此调用方可以将它与回合最终化组合为原子操作.
    """

    exists = connection.execute(
        "SELECT 1 FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if exists is None:
        raise SessionError(f"unknown session: {session_id}")
    existing = connection.execute(
        """
        SELECT compaction_id, session_id, provider_name, model_name,
               capacity_tokens, context_affinity, source_item_count,
               protected_item_count, recent_item_count, candidate_start,
               candidate_end, target_tokens, summary_tokens,
               source_fingerprint, summary, summary_redacted,
               summary_truncated, created_at
        FROM session_compaction_items
        WHERE compaction_id = ?
        """,
        (item.compaction_id,),
    ).fetchone()
    if existing is not None:
        if str(existing[1]) != session_id:
            raise SessionError("compaction item belongs to another session")
        if _compaction_item_from_row(existing) != item:
            raise SessionError("compaction item ID already exists with different data")
        return
    connection.execute(
        """
        INSERT INTO session_compaction_items(
            compaction_id, session_id, provider_name, model_name,
            capacity_tokens, context_affinity, source_item_count,
            protected_item_count, recent_item_count, candidate_start,
            candidate_end, target_tokens, summary_tokens,
            source_fingerprint, summary, summary_redacted,
            summary_truncated, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item.compaction_id,
            session_id,
            item.provider_name,
            item.model_name,
            item.capacity_tokens,
            item.context_affinity,
            item.source_item_count,
            item.protected_item_count,
            item.recent_item_count,
            item.candidate_range[0],
            item.candidate_range[1],
            item.target_tokens,
            item.summary_tokens,
            item.source_fingerprint,
            item.summary,
            int(item.summary_redacted),
            int(item.summary_truncated),
            item.created_at.isoformat(),
        ),
    )
    connection.execute(
        "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (session_id,),
    )


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


def _session_task_from_row(row: Sequence[object], *, session_id: str) -> SessionTask:
    try:
        task_id, raw_kind, raw_status, raw_started_at, raw_finished_at, raw_plan_snapshot = row
        started_at = datetime.fromisoformat(str(raw_started_at))
        finished_at = (
            datetime.fromisoformat(str(raw_finished_at)) if raw_finished_at is not None else None
        )
        if not isinstance(raw_plan_snapshot, str):
            raise ValueError("session task plan snapshot is invalid")
        plan_snapshot = (
            SessionPlan.from_dict(json.loads(raw_plan_snapshot)) if raw_plan_snapshot else None
        )
        return SessionTask(
            str(task_id),
            SessionTaskKind(str(raw_kind)),
            SessionTaskStatus(str(raw_status)),
            started_at,
            finished_at,
            plan_snapshot,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise SessionError(f"session {session_id} contains an invalid task") from error


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


def _session_alias_value(value: str, *, field_name: str, limit: int) -> str:
    if not value or "\x00" in value:
        raise SessionError(f"session alias {field_name} must not be empty")
    if len(value.encode("utf-8")) > limit:
        raise SessionError(f"session alias {field_name} is too large")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise SessionError(f"session alias {field_name} contains control characters")
    return value


def _validated_session_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not task_id or "\x00" in task_id:
        raise SessionError("session task id is invalid")
    if len(task_id.encode("utf-8")) > MAX_SESSION_TASK_ID_BYTES:
        raise SessionError("session task id is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in task_id):
        raise SessionError("session task id is invalid")
    return task_id


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


__all__ = ["SqliteSessionStore"]
