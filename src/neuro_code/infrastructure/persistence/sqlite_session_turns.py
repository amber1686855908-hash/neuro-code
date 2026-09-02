"""SQLite persistence turns owner.

This module owns one cohesive persistence responsibility.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime

from neuro_code.domain.conversation.compaction import DurableCompactionItem
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.messages import SessionItem
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
from neuro_code.domain.session_tasks import SessionTask, SessionTaskKind, SessionTaskStatus
from neuro_code.domain.sessions.search import fallback_session_title, searchable_session_text
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
)
from neuro_code.infrastructure.persistence.sqlite_session_core import (
    _serialize_session_items,
    _session_items_from_json,
    _upsert_search_document,
)
from neuro_code.infrastructure.persistence.sqlite_session_plans import (
    _insert_session_task_row,
    _session_task_from_row,
    _start_session_task_row,
    _validated_session_task_id,
)
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import SessionError


class TurnsMixin(_SqliteSessionPersistenceContext):
    """Mixin owning this SQLite persistence slice."""

    async def start_turn_attempt(self, attempt: TurnRecoveryAttempt) -> None:
        """Durably accept one turn before any provider or tool boundary."""

        if not isinstance(attempt, TurnRecoveryAttempt):
            raise TypeError("attempt must be a TurnRecoveryAttempt")
        input_json = attempt.input.canonical_json() if attempt.input is not None else ""

        def start() -> None:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                _persist_turn_attempt_acceptance(
                    connection,
                    attempt=attempt,
                    input_json=input_json,
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

    async def start_plan_turn_attempt(
        self,
        attempt: TurnRecoveryAttempt,
        *,
        task: SessionTask | None = None,
        queued_task_id: str | None = None,
        started_at: datetime | None = None,
    ) -> SessionTask:
        """Accept a plan turn and establish its exact task owner atomically."""

        if not isinstance(attempt, TurnRecoveryAttempt):
            raise TypeError("attempt must be a TurnRecoveryAttempt")
        if attempt.input is None or not attempt.input.plan_execution_requested:
            raise SessionError("plan turn attempt input is required")
        if (task is None) == (queued_task_id is None):
            raise SessionError("plan turn acceptance requires one task ownership mode")
        input_json = attempt.input.canonical_json()
        if task is not None:
            if task.kind is not SessionTaskKind.PLAN_EXECUTION:
                raise SessionError("plan turn acceptance requires a plan execution task")
            if task.status is not SessionTaskStatus.RUNNING:
                raise SessionError("new plan turn acceptance requires a running task")
            if attempt.task_id != task.task_id:
                raise SessionError("plan turn attempt task ownership does not match the task")
            if started_at is not None:
                raise SessionError("new plan turn acceptance does not accept a queued start time")
        else:
            assert queued_task_id is not None
            _validated_session_task_id(queued_task_id)
            if attempt.task_id != queued_task_id:
                raise SessionError("plan turn attempt task ownership does not match the task")
            if started_at is None:
                started_at = datetime.now(UTC)
            if started_at.tzinfo is None:
                raise SessionError("queued plan task start time must be timezone-aware")

        def start() -> SessionTask:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                _persist_turn_attempt_acceptance(
                    connection,
                    attempt=attempt,
                    input_json=input_json,
                )
                if task is not None:
                    _insert_session_task_row(
                        connection,
                        session_id=attempt.session_id,
                        task=task,
                    )
                    started_task = task
                else:
                    assert queued_task_id is not None
                    assert started_at is not None
                    started_task = _start_session_task_row(
                        connection,
                        session_id=attempt.session_id,
                        task_id=queued_task_id,
                        started_at=started_at,
                    )
                connection.commit()
                return started_task
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise SessionError(
                    f"cannot atomically accept plan turn: {attempt.turn_id}"
                ) from error
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(start)

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
        *,
        task: SessionTask | None = None,
        task_event: AgentEvent | None = None,
    ) -> None:
        """Persist an explicit user-directed abandon resolution."""

        if not isinstance(event, AgentEvent) or event.kind is not AgentEventKind.TURN_ABANDONED:
            raise SessionError("abandon_turn_attempt requires a TURN_ABANDONED event")
        if not isinstance(reason, str) or not reason.strip() or len(reason.encode("utf-8")) > 512:
            raise SessionError("abandon reason is invalid")
        if event.data.get("turn_id") != turn_id:
            raise SessionError("abandon event has a different turn identity")
        if task is None and task_event is not None:
            raise SessionError("task event cannot exist without a linked task")
        if task is not None:
            if not isinstance(task, SessionTask):
                raise TypeError("task must be a SessionTask")
            if task.kind is not SessionTaskKind.PLAN_EXECUTION:
                raise SessionError("only a plan execution task may be abandoned with a turn")
            if task.status is not SessionTaskStatus.CANCELLED or task.finished_at is None:
                raise SessionError("linked plan task must be cancelled before abandon")
            if task_event is None:
                raise SessionError("a linked task abandon requires a task event")
            if (
                not isinstance(task_event, AgentEvent)
                or task_event.kind is not AgentEventKind.SESSION_TASK_CANCELLED
            ):
                raise SessionError("linked plan abandon requires a task-cancel event")
            if task_event.sequence >= event.sequence:
                raise SessionError("task-cancel event must precede the turn abandon event")
            if task_event.data.get("task") != task.to_dict():
                raise SessionError("task-cancel event does not match the linked task")
        payload = json.dumps(dict(event.data), ensure_ascii=False, separators=(",", ":"))

        def abandon() -> None:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT resolution
                           , task_id
                    FROM session_turn_attempts
                    WHERE session_id = ? AND turn_id = ?
                    """,
                    (session_id, turn_id),
                ).fetchone()
                if row is None:
                    raise SessionError(f"unknown turn attempt: {turn_id}")
                if row[0] is not None:
                    raise SessionError(f"turn attempt is already resolved: {turn_id}")
                if task is not None:
                    if row[1] != task.task_id:
                        raise SessionError("linked plan task does not own this turn attempt")
                    assert task_event is not None
                    _persist_task_terminal(
                        connection,
                        session_id=session_id,
                        task=task,
                        task_event=task_event,
                        before_sequence=event.sequence,
                    )
                elif row[1] is not None:
                    raise SessionError("linked plan task ownership is required for abandon")
                _insert_event_row(
                    connection,
                    session_id=session_id,
                    event=event,
                    payload=payload,
                )
                _resolve_abandoned_turn_attempt(
                    connection,
                    session_id=session_id,
                    turn_id=turn_id,
                    event=event,
                )
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


def _persist_turn_attempt_acceptance(
    connection: sqlite3.Connection,
    *,
    attempt: TurnRecoveryAttempt,
    input_json: str,
) -> None:
    """Insert one accepted attempt inside the caller-owned transaction."""

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
        raise SessionError(f"session {attempt.session_id} already has an open turn attempt")
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


def _resolve_abandoned_turn_attempt(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    turn_id: str,
    event: AgentEvent,
) -> None:
    """Resolve an open attempt inside the caller-owned transaction."""

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
