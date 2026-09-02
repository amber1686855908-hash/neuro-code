"""SQLite persistence plans owner.

This module owns one cohesive persistence responsibility.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime

from neuro_code.domain.plans import MAX_PLAN_COMMENTS, PlanComment, SessionPlan
from neuro_code.domain.session_tasks import (
    MAX_QUEUED_SESSION_TASKS,
    MAX_SESSION_TASK_ID_BYTES,
    SessionTask,
    SessionTaskKind,
    SessionTaskStatus,
)
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
)
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import SessionError


class PlansMixin(_SqliteSessionPersistenceContext):
    """Mixin owning this SQLite persistence slice."""

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

        def create() -> None:
            try:
                with closing(self._connect()) as connection, connection:
                    _insert_session_task_row(connection, session_id=session_id, task=task)
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
                claimed = _start_session_task_row(
                    connection,
                    session_id=session_id,
                    task_id=task_id,
                    started_at=started_at,
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


def _insert_session_task_row(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    task: SessionTask,
) -> None:
    """Insert one task inside the caller-owned transaction."""

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
    if task.kind is SessionTaskKind.PLAN_EXECUTION and task.status is SessionTaskStatus.QUEUED:
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
            raise SessionError(f"at most {MAX_QUEUED_SESSION_TASKS} plan tasks may be queued")
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


def _start_session_task_row(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    task_id: str,
    started_at: datetime,
) -> SessionTask:
    """Claim one queued task inside the caller-owned transaction."""

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
    return claimed


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


def _validated_session_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not task_id or "\x00" in task_id:
        raise SessionError("session task id is invalid")
    if len(task_id.encode("utf-8")) > MAX_SESSION_TASK_ID_BYTES:
        raise SessionError("session task id is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in task_id):
        raise SessionError("session task id is invalid")
    return task_id
