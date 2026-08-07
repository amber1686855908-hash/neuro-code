from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from neuro_code.domain.background_tasks import BackgroundWakeState
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.messages import (
    ContentPart,
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    ToolCall,
)
from neuro_code.domain.execution import (
    AgentExecutionOutcome,
    AgentExecutionStatus,
    SessionExecutionRecord,
    SupervisorReasonCode,
)
from neuro_code.domain.plans import (
    MAX_PLAN_COMMENTS,
    PlanComment,
    PlanStep,
    PlanStepStatus,
    SessionPlan,
)
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.session_tasks import (
    MAX_QUEUED_SESSION_TASKS,
    SessionTask,
    SessionTaskKind,
    SessionTaskStatus,
)
from neuro_code.domain.sessions import SessionSnapshot, SessionSummary
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.shared.errors import SessionError


def _save_execution_record_in_process(
    database: str,
    session_id: str,
    record: SessionExecutionRecord,
    ready_queue,
    start_event,
    result_queue,
) -> None:
    """Attempt one execution-record write from a real OS process.

    从真实 OS 进程尝试写入一条执行记录."""

    ready_queue.put("ready")
    if not start_event.wait(timeout=10):
        result_queue.put("start-timeout")
        return

    async def save() -> None:
        store = SqliteSessionStore(Path(database))
        try:
            await store.save_execution_record(session_id, record)
        except Exception as error:
            result_queue.put(f"error:{type(error).__name__}:{error}")
        else:
            result_queue.put("ok")

    asyncio.run(save())


class SessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_plan_tasks_are_capped_and_claimed_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            session_id = await store.create_session("/workspace", "fixture", "model")
            queued_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
            for index in range(MAX_QUEUED_SESSION_TASKS):
                await store.create_session_task(
                    session_id,
                    SessionTask(
                        f"task-queued-{index}",
                        SessionTaskKind.PLAN_EXECUTION,
                        SessionTaskStatus.QUEUED,
                        queued_at,
                    ),
                )

            with self.assertRaisesRegex(SessionError, "at most 4 plan tasks"):
                await store.create_session_task(
                    session_id,
                    SessionTask(
                        "task-queued-overflow",
                        SessionTaskKind.PLAN_EXECUTION,
                        SessionTaskStatus.QUEUED,
                        queued_at,
                    ),
                )
            with self.assertRaisesRegex(SessionError, "unknown session task"):
                await store.start_session_task(session_id, "task-not-found", queued_at)

            started = await store.start_session_task(
                session_id,
                "task-queued-0",
                queued_at.replace(second=5),
            )
            self.assertIs(started.status, SessionTaskStatus.RUNNING)
            with self.assertRaisesRegex(SessionError, "cannot create session task"):
                await store.create_session_task(
                    session_id,
                    SessionTask(
                        "task-queued-1",
                        SessionTaskKind.PLAN_EXECUTION,
                        SessionTaskStatus.QUEUED,
                        queued_at,
                    ),
                )
            with self.assertRaisesRegex(SessionError, "invalid session task transition"):
                await store.start_session_task(
                    session_id,
                    "task-queued-0",
                    queued_at.replace(second=6),
                )

            await store.create_session_task(
                session_id,
                SessionTask(
                    "task-naive-start",
                    SessionTaskKind.PLAN_EXECUTION,
                    SessionTaskStatus.QUEUED,
                    queued_at,
                ),
            )
            with self.assertRaisesRegex(SessionError, "invalid session task transition"):
                await store.start_session_task(
                    session_id,
                    "task-naive-start",
                    datetime.fromisoformat("2026-07-29T12:00:06"),
                )
            await store.start_session_task(
                session_id,
                "task-naive-start",
                queued_at.replace(second=7),
            )

            await store.create_session_task(
                session_id,
                SessionTask(
                    "task-queued-replacement",
                    SessionTaskKind.PLAN_EXECUTION,
                    SessionTaskStatus.QUEUED,
                    queued_at,
                ),
            )
            tasks = await store.list_session_tasks(session_id)
            self.assertEqual(
                sum(task.status is SessionTaskStatus.QUEUED for task in tasks),
                MAX_QUEUED_SESSION_TASKS,
            )

    def test_connect_retries_only_transient_wal_locks_and_closes_on_failure(self) -> None:
        store = SqliteSessionStore(Path("/unused/sessions.db"))
        retry_connection = Mock(spec=sqlite3.Connection)
        wal_attempts = 0

        def execute_with_transient_lock(statement: str) -> Mock:
            nonlocal wal_attempts
            if statement == "PRAGMA journal_mode = WAL":
                wal_attempts += 1
                if wal_attempts < 3:
                    raise sqlite3.OperationalError("database is locked")
            return Mock()

        retry_connection.execute.side_effect = execute_with_transient_lock
        with (
            patch(
                "neuro_code.infrastructure.persistence.sqlite_session.sqlite3.connect",
                return_value=retry_connection,
            ),
            patch("neuro_code.infrastructure.persistence.sqlite_session.time.sleep") as sleep,
        ):
            self.assertIs(store._connect(), retry_connection)

        self.assertEqual(wal_attempts, 3)
        self.assertEqual(sleep.call_count, 2)
        retry_connection.close.assert_not_called()

        failed_connection = Mock(spec=sqlite3.Connection)

        def execute_with_permanent_failure(statement: str) -> Mock:
            if statement == "PRAGMA journal_mode = WAL":
                raise sqlite3.OperationalError("disk I/O error")
            return Mock()

        failed_connection.execute.side_effect = execute_with_permanent_failure
        with (
            patch(
                "neuro_code.infrastructure.persistence.sqlite_session.sqlite3.connect",
                return_value=failed_connection,
            ),
            self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O error"),
        ):
            store._connect()
        failed_connection.close.assert_called_once_with()

    async def test_import_snapshot_is_atomic_and_preserves_identity_and_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            snapshot = SessionSnapshot(
                summary=SessionSummary(
                    id="imported-id",
                    cwd="/rust/workspace",
                    provider="upstream-rust-import",
                    model="xai-test-model",
                    created_at=datetime(2026, 7, 1, tzinfo=UTC),
                    updated_at=datetime(2026, 7, 2, tzinfo=UTC),
                    sandbox_profile=SandboxProfile.STRICT,
                ),
                items=(
                    Message(
                        Role.USER,
                        "imported",
                        content_parts=(
                            ContentPart.from_text("imported"),
                            ContentPart.from_image("data:image/png;base64,fixture"),
                        ),
                    ),
                    PreservedContextItem(
                        ContextItemKind.REASONING,
                        {
                            "type": "reasoning",
                            "id": "reasoning-1",
                            "summary": [],
                            "encrypted_content": "opaque",
                        },
                    ),
                    Message(Role.ASSISTANT, "done"),
                ),
            )

            imported_id = await store.import_session(snapshot)

            self.assertEqual(imported_id, "imported-id")
            imported_summary = await store.get_session(imported_id)
            self.assertEqual(imported_summary.title, "imported")
            self.assertEqual(
                replace(imported_summary, title=None),
                snapshot.summary,
            )
            self.assertEqual(await store.load_messages(imported_id), list(snapshot.messages))
            self.assertEqual(await store.load_session_items(imported_id), list(snapshot.items))
            self.assertEqual(await store.load_events(imported_id), [])
            self.assertEqual(await store.next_event_sequence(imported_id), 1)
            with self.assertRaisesRegex(SessionError, "session already exists"):
                await store.import_session(snapshot)
            self.assertEqual(await store.load_messages(imported_id), list(snapshot.messages))

            continued = [*snapshot.messages, Message(Role.USER, "continue")]
            await store.save_messages(imported_id, continued)
            self.assertEqual(
                await store.load_session_items(imported_id),
                [*snapshot.items, continued[-1]],
            )
            with self.assertRaisesRegex(SessionError, "cannot rewrite the imported prefix"):
                await store.save_messages(imported_id, [Message(Role.USER, "rewritten")])

            native = PreservedContextItem(
                ContextItemKind.REASONING,
                {
                    "type": "reasoning",
                    "id": "reasoning-2",
                    "summary": [],
                    "encrypted_content": "native-opaque",
                },
            )
            extended_items = [*snapshot.items, continued[-1], native, Message(Role.ASSISTANT, "ok")]
            await store.save_session_items(imported_id, extended_items)
            self.assertEqual(await store.load_session_items(imported_id), extended_items)
            with self.assertRaisesRegex(
                SessionError, "cannot rewrite the persisted session item prefix"
            ):
                await store.save_session_items(imported_id, [Message(Role.USER, "replacement")])

    async def test_round_trip_messages_and_ordered_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(
                "/workspace",
                "fake",
                "test-model",
                "profile-v1:fixture",
                SandboxProfile.READ_ONLY,
            )
            messages = [
                Message(Role.USER, "inspect"),
                Message(
                    Role.ASSISTANT,
                    tool_calls=(
                        ToolCall(
                            "call-1",
                            "read_file",
                            {"path": "a.py"},
                            {"provider_signature": "opaque"},
                        ),
                    ),
                    reasoning_content="Need to read a.py.",
                ),
                Message(Role.TOOL, "content", name="read_file", tool_call_id="call-1"),
            ]
            await store.save_messages(session_id, messages)
            await store.append_event(
                session_id,
                AgentEvent.create(1, AgentEventKind.USER_MESSAGE, {"content": "inspect"}),
            )
            await store.append_event(
                session_id,
                AgentEvent.create(2, AgentEventKind.TURN_COMPLETED, {"step": 1}),
            )
            execution_record = SessionExecutionRecord(
                AgentExecutionOutcome(
                    AgentExecutionStatus.BUDGET_LIMITED,
                    SupervisorReasonCode.MODEL_STEP_LIMIT,
                    finalized=True,
                    recoverable=True,
                ),
                2,
                datetime(2026, 7, 3, 9, 58, tzinfo=UTC),
            )
            await store.save_execution_record(session_id, execution_record)

            loaded = await store.load_messages(session_id)
            events = await store.load_events(session_id)
            self.assertEqual(loaded, messages)
            self.assertEqual([event["sequence"] for event in events], [1, 2])
            self.assertEqual(await store.next_event_sequence(session_id), 3)
            self.assertEqual(await store.load_execution_record(session_id), execution_record)
            summary = await store.get_session(session_id)
            self.assertEqual(summary.id, session_id)
            self.assertEqual(summary.cwd, "/workspace")
            self.assertEqual(summary.context_affinity, "profile-v1:fixture")
            self.assertIs(summary.sandbox_profile, SandboxProfile.READ_ONLY)
            plan = SessionPlan(
                (
                    PlanStep("Inspect the persisted session", PlanStepStatus.COMPLETED),
                    PlanStep("Implement the follow-up", PlanStepStatus.IN_PROGRESS),
                ),
                "Finish the durable plan workflow",
            )
            await store.save_session_plan(session_id, plan)
            self.assertEqual(await store.load_session_plan(session_id), plan)
            comment = PlanComment(
                "plan-comment-round-trip",
                2,
                "Keep the verification command visible.",
                datetime(2026, 7, 3, 9, 59, tzinfo=UTC),
            )
            await store.add_plan_comment(session_id, plan, comment)
            self.assertEqual(await store.list_plan_comments(session_id, plan), [comment])
            replacement = SessionPlan((PlanStep("Use a revised approach"),))
            await store.save_session_plan(session_id, replacement)
            self.assertEqual(await store.list_plan_comments(session_id, replacement), [])
            await store.save_session_plan(session_id, plan)
            self.assertEqual(await store.list_plan_comments(session_id, plan), [])
            await store.save_session_plan(session_id, None)
            self.assertIsNone(await store.load_session_plan(session_id))
            task = SessionTask(
                "task-plan-execution",
                SessionTaskKind.PLAN_EXECUTION,
                SessionTaskStatus.RUNNING,
                datetime(2026, 7, 3, 10, tzinfo=UTC),
                plan_snapshot=plan,
            )
            await store.create_session_task(session_id, task)
            self.assertEqual(await store.list_session_tasks(session_id), [task])
            self.assertEqual(await store.get_session_task(session_id, task.task_id), task)
            self.assertIsNone(await store.get_session_task(session_id, "task-not-found"))
            with self.assertRaisesRegex(SessionError, "unknown session"):
                await store.get_session_task("missing-session", "task-not-found")
            other_session_id = await store.create_session(
                "/other-workspace",
                "fake",
                "test-model",
            )
            self.assertIsNone(await store.get_session_task(other_session_id, task.task_id))
            with self.assertRaisesRegex(SessionError, "task id is invalid"):
                await store.get_session_task(session_id, "task\x00invalid")
            completed = task.finish(
                SessionTaskStatus.COMPLETED,
                finished_at=datetime(2026, 7, 3, 10, 1, tzinfo=UTC),
            )
            await store.update_session_task(session_id, completed)
            self.assertEqual(await store.list_session_tasks(session_id), [completed])
            self.assertEqual(await store.get_session_task(session_id, completed.task_id), completed)
            with self.assertRaisesRegex(SessionError, "invalid session task transition"):
                await store.update_session_task(session_id, completed)
            with self.assertRaisesRegex(SessionError, "unknown session task"):
                await store.update_session_task(
                    session_id,
                    SessionTask(
                        "task-missing",
                        SessionTaskKind.PLAN_EXECUTION,
                        SessionTaskStatus.RUNNING,
                        datetime(2026, 7, 3, 10, tzinfo=UTC),
                    ),
                )
            self.assertIs(
                await store.peek_session_sandbox_profile(session_id),
                SandboxProfile.READ_ONLY,
            )
            await store.update_session_provider(
                session_id,
                "fallback",
                "fallback-model",
                "profile-v1:fallback",
            )
            summary = await store.get_session(session_id)
            self.assertEqual(summary.provider, "fallback")
            self.assertEqual(summary.model, "fallback-model")
            self.assertEqual(summary.context_affinity, "profile-v1:fallback")
            self.assertIs(summary.sandbox_profile, SandboxProfile.READ_ONLY)
            self.assertIn(summary, await store.list_sessions())

    async def test_finalize_turn_commits_event_items_and_execution_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            session_id = await store.create_session("/workspace", "fixture", "model")
            initial_items = [Message(Role.USER, "persist this turn")]
            await store.save_session_items(session_id, initial_items)
            event = AgentEvent.create(1, AgentEventKind.TURN_COMPLETED, {"step": 1})
            final_items = [*initial_items, Message(Role.ASSISTANT, "done")]
            record = SessionExecutionRecord(
                AgentExecutionOutcome(
                    AgentExecutionStatus.COMPLETED,
                    None,
                    finalized=False,
                    recoverable=False,
                ),
                event.sequence,
                event.created_at,
            )

            await store.finalize_turn(session_id, event, final_items, record)

            self.assertEqual(await store.load_session_items(session_id), final_items)
            self.assertEqual(await store.load_execution_record(session_id), record)
            persisted_events = await store.load_events(session_id)
            self.assertEqual(
                [(item["sequence"], item["kind"], item["data"]) for item in persisted_events],
                [(1, AgentEventKind.TURN_COMPLETED.value, {"step": 1})],
            )

    async def test_finalize_turn_without_record_preserves_previous_execution_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            session_id = await store.create_session("/workspace", "fixture", "model")
            initial_items = [Message(Role.USER, "user turn")]
            await store.save_session_items(session_id, initial_items)
            first_event = AgentEvent.create(1, AgentEventKind.TURN_COMPLETED, {"step": 1})
            first_items = [*initial_items, Message(Role.ASSISTANT, "user result")]
            previous_record = SessionExecutionRecord(
                AgentExecutionOutcome(
                    AgentExecutionStatus.COMPLETED,
                    None,
                    finalized=False,
                    recoverable=False,
                ),
                first_event.sequence,
                first_event.created_at,
            )
            await store.finalize_turn(session_id, first_event, first_items, previous_record)

            second_event = AgentEvent.create(2, AgentEventKind.TURN_COMPLETED, {"step": 2})
            second_items = [*first_items, Message(Role.ASSISTANT, "background result")]
            await store.finalize_turn(session_id, second_event, second_items, None)

            self.assertEqual(await store.load_session_items(session_id), second_items)
            self.assertEqual(await store.load_execution_record(session_id), previous_record)
            self.assertEqual(
                [event["sequence"] for event in await store.load_events(session_id)],
                [1, 2],
            )

    async def test_finalize_turn_rejects_invalid_completion_and_duplicate_sequences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            session_id = await store.create_session("/workspace", "fixture", "model")
            initial_items = [Message(Role.USER, "prefix")]
            await store.save_session_items(session_id, initial_items)

            with self.assertRaisesRegex(SessionError, "TURN_COMPLETED"):
                await store.finalize_turn(
                    session_id,
                    AgentEvent.create(1, AgentEventKind.USER_MESSAGE, {}),
                    initial_items,
                    None,
                )
            mismatch_event = AgentEvent.create(1, AgentEventKind.TURN_COMPLETED, {})
            mismatch_record = SessionExecutionRecord(
                AgentExecutionOutcome(
                    AgentExecutionStatus.COMPLETED,
                    None,
                    finalized=False,
                    recoverable=False,
                ),
                2,
                mismatch_event.created_at,
            )
            with self.assertRaisesRegex(SessionError, "does not match"):
                await store.finalize_turn(
                    session_id,
                    mismatch_event,
                    initial_items,
                    mismatch_record,
                )
            with self.assertRaisesRegex(SessionError, "prefix"):
                await store.finalize_turn(
                    session_id,
                    mismatch_event,
                    [Message(Role.ASSISTANT, "rewritten")],
                    None,
                )

            await store.finalize_turn(session_id, mismatch_event, initial_items, None)
            with self.assertRaisesRegex(SessionError, "already exists"):
                await store.finalize_turn(session_id, mismatch_event, initial_items, None)
            self.assertEqual(
                [event["sequence"] for event in await store.load_events(session_id)],
                [1],
            )

    async def test_finalize_turn_rolls_back_event_items_and_record_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            session_id = await store.create_session("/workspace", "fixture", "model")
            initial_items = [Message(Role.USER, "existing")]
            await store.save_session_items(session_id, initial_items)
            previous_event = AgentEvent.create(1, AgentEventKind.TURN_COMPLETED, {"step": 1})
            previous_record = SessionExecutionRecord(
                AgentExecutionOutcome(
                    AgentExecutionStatus.COMPLETED,
                    None,
                    finalized=False,
                    recoverable=False,
                ),
                previous_event.sequence,
                previous_event.created_at,
            )
            await store.finalize_turn(session_id, previous_event, initial_items, previous_record)
            next_event = AgentEvent.create(2, AgentEventKind.TURN_COMPLETED, {"step": 2})
            next_items = [*initial_items, Message(Role.ASSISTANT, "new result")]
            next_record = SessionExecutionRecord(
                AgentExecutionOutcome(
                    AgentExecutionStatus.BUDGET_LIMITED,
                    SupervisorReasonCode.MODEL_STEP_LIMIT,
                    finalized=True,
                    recoverable=True,
                ),
                next_event.sequence,
                next_event.created_at,
            )

            with (
                patch(
                    "neuro_code.infrastructure.persistence.sqlite_session._upsert_search_document",
                    side_effect=RuntimeError("index failure"),
                ),
                self.assertRaisesRegex(RuntimeError, "index failure"),
            ):
                await store.finalize_turn(session_id, next_event, next_items, next_record)

            self.assertEqual(await store.load_session_items(session_id), initial_items)
            self.assertEqual(await store.load_execution_record(session_id), previous_record)
            self.assertEqual(
                [event["sequence"] for event in await store.load_events(session_id)],
                [1],
            )

    async def test_schema_v8_migration_preserves_tasks_without_plan_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sessions.db"
            store = SqliteSessionStore(database)
            await store.initialize()
            session_id = await store.create_session("/workspace", "fixture", "model")
            task = SessionTask(
                "task-v8-plan",
                SessionTaskKind.PLAN_EXECUTION,
                SessionTaskStatus.RUNNING,
                datetime(2026, 7, 29, 10, tzinfo=UTC),
            )
            await store.create_session_task(session_id, task)

            connection = sqlite3.connect(database)
            connection.executescript(
                """
                ALTER TABLE session_tasks RENAME TO legacy_session_tasks;
                CREATE TABLE session_tasks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                INSERT INTO session_tasks(task_id, session_id, kind, status, started_at, finished_at)
                SELECT task_id, session_id, kind, status, started_at, finished_at
                FROM legacy_session_tasks;
                DROP TABLE legacy_session_tasks;
                UPDATE schema_meta SET version = 8 WHERE singleton = 1;
                """
            )
            connection.commit()
            connection.close()

            migrated = SqliteSessionStore(database)
            await migrated.initialize()
            self.assertEqual(await migrated.list_session_tasks(session_id), [task])

            connection = sqlite3.connect(database)
            self.assertEqual(
                connection.execute(
                    "SELECT version FROM schema_meta WHERE singleton = 1"
                ).fetchone(),
                (11,),
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(session_tasks)")}
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            self.assertIn("plan_snapshot_json", columns)
            self.assertIn("session_execution_records", tables)
            self.assertIn("session_background_wake_state", tables)
            self.assertEqual(
                connection.execute(
                    "SELECT plan_snapshot_json FROM session_tasks WHERE task_id = ?",
                    (task.task_id,),
                ).fetchone(),
                ("",),
            )
            connection.close()

    async def test_execution_records_are_auditable_overwritable_and_not_forked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            source_id = await store.create_session("/workspace", "fixture", "model")
            await store.append_event(
                source_id,
                AgentEvent.create(1, AgentEventKind.TURN_COMPLETED, {"step": 1}),
            )
            paused = SessionExecutionRecord(
                AgentExecutionOutcome(
                    AgentExecutionStatus.STUCK,
                    SupervisorReasonCode.PERIODIC_CYCLE,
                    finalized=True,
                    recoverable=True,
                ),
                1,
                datetime(2026, 7, 31, 12, tzinfo=UTC),
            )
            await store.save_execution_record(source_id, paused)
            self.assertEqual(await store.load_execution_record(source_id), paused)

            forked_id = await store.fork_session(source_id)
            self.assertIsNone(await store.load_execution_record(forked_id))

            await store.append_event(
                source_id,
                AgentEvent.create(2, AgentEventKind.TURN_COMPLETED, {"step": 2}),
            )
            completed = SessionExecutionRecord(
                AgentExecutionOutcome(
                    AgentExecutionStatus.COMPLETED,
                    None,
                    finalized=False,
                    recoverable=False,
                ),
                2,
                datetime(2026, 7, 31, 12, 1, tzinfo=UTC),
            )
            await store.save_execution_record(source_id, completed)
            self.assertEqual(await store.load_execution_record(source_id), completed)

            with self.assertRaisesRegex(SessionError, "turn-completed event"):
                await store.save_execution_record(
                    source_id,
                    SessionExecutionRecord(
                        completed.outcome,
                        3,
                        datetime(2026, 7, 31, 12, 2, tzinfo=UTC),
                    ),
                )

    async def test_loading_execution_record_rejects_missing_or_non_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sessions.db"
            store = SqliteSessionStore(database)
            await store.initialize()
            session_id = await store.create_session("/workspace", "fixture", "model")
            record = SessionExecutionRecord(
                AgentExecutionOutcome(
                    AgentExecutionStatus.STUCK,
                    SupervisorReasonCode.PERIODIC_CYCLE,
                    finalized=True,
                    recoverable=True,
                ),
                1,
                datetime(2026, 7, 31, 12, tzinfo=UTC),
            )
            await store.append_event(
                session_id,
                AgentEvent.create(1, AgentEventKind.TURN_COMPLETED, {"step": 1}),
            )
            await store.save_execution_record(session_id, record)

            connection = sqlite3.connect(database)
            connection.execute(
                "DELETE FROM events WHERE session_id = ? AND sequence = ?",
                (session_id, record.event_sequence),
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(SessionError, "invalid completion event"):
                await store.load_execution_record(session_id)

            connection = sqlite3.connect(database)
            connection.execute(
                "INSERT INTO events(session_id, sequence, kind, created_at, data_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    record.event_sequence,
                    AgentEventKind.USER_MESSAGE.value,
                    datetime(2026, 7, 31, 12, tzinfo=UTC).isoformat(),
                    "{}",
                ),
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(SessionError, "invalid completion event"):
                await store.load_execution_record(session_id)

    async def test_loading_execution_records_preserves_order_and_missing_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            recorded_id = await store.create_session("/recorded", "fixture", "model")
            empty_id = await store.create_session("/empty", "fixture", "model")
            record = SessionExecutionRecord(
                AgentExecutionOutcome(
                    AgentExecutionStatus.STUCK,
                    SupervisorReasonCode.NO_PROGRESS,
                    finalized=True,
                    recoverable=True,
                ),
                1,
                datetime(2026, 7, 31, 12, tzinfo=UTC),
            )
            await store.append_event(
                recorded_id,
                AgentEvent.create(1, AgentEventKind.TURN_COMPLETED, {"step": 1}),
            )
            await store.save_execution_record(recorded_id, record)

            self.assertEqual(
                await store.load_execution_records((empty_id, recorded_id, recorded_id)),
                (None, record, record),
            )
            self.assertEqual(await store.load_execution_records(()), ())
            with self.assertRaisesRegex(SessionError, "unknown session: missing"):
                await store.load_execution_records((recorded_id, "missing"))

            connection = sqlite3.connect(Path(directory) / "sessions.db")
            connection.execute(
                "UPDATE events SET kind = ? WHERE session_id = ? AND sequence = ?",
                (AgentEventKind.USER_MESSAGE.value, recorded_id, record.event_sequence),
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(SessionError, "invalid completion event"):
                await store.load_execution_records((recorded_id,))

    async def test_execution_record_writes_are_monotonic_across_turn_sequences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            session_id = await store.create_session("/workspace", "fixture", "model")
            await store.append_event(
                session_id,
                AgentEvent.create(2, AgentEventKind.TURN_COMPLETED, {"step": 2}),
            )
            newer = SessionExecutionRecord(
                AgentExecutionOutcome(
                    AgentExecutionStatus.BUDGET_LIMITED,
                    SupervisorReasonCode.MODEL_STEP_LIMIT,
                    finalized=True,
                    recoverable=True,
                ),
                2,
                datetime(2026, 7, 31, 12, 1, tzinfo=UTC),
            )
            older = SessionExecutionRecord(
                AgentExecutionOutcome(
                    AgentExecutionStatus.STUCK,
                    SupervisorReasonCode.PERIODIC_CYCLE,
                    finalized=True,
                    recoverable=True,
                ),
                1,
                datetime(2026, 7, 31, 12, tzinfo=UTC),
            )
            await store.save_execution_record(session_id, newer)
            stale_event = AgentEvent.create(1, AgentEventKind.TURN_COMPLETED, {"step": 1})
            await store.append_event(session_id, stale_event)
            with self.assertRaisesRegex(SessionError, "older event sequence"):
                await store.save_execution_record(session_id, older)
            self.assertEqual(await store.load_execution_record(session_id), newer)

            same_sequence_conflict = SessionExecutionRecord(
                AgentExecutionOutcome(
                    AgentExecutionStatus.STUCK,
                    SupervisorReasonCode.PERIODIC_CYCLE,
                    finalized=True,
                    recoverable=True,
                ),
                2,
                datetime(2026, 7, 31, 12, 2, tzinfo=UTC),
            )
            with self.assertRaisesRegex(SessionError, "same event sequence"):
                await store.save_execution_record(session_id, same_sequence_conflict)
            self.assertEqual(await store.load_execution_record(session_id), newer)

            with self.assertRaisesRegex(SessionError, "older event sequence"):
                await store.finalize_turn(session_id, stale_event, (), older)
            self.assertEqual(await store.load_execution_record(session_id), newer)

    async def test_concurrent_store_connections_keep_the_newest_execution_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sessions.db"
            setup_store = SqliteSessionStore(database)
            await setup_store.initialize()
            session_id = await setup_store.create_session("/workspace", "fixture", "model")
            for sequence in (1, 2):
                await setup_store.append_event(
                    session_id,
                    AgentEvent.create(sequence, AgentEventKind.TURN_COMPLETED, {"step": sequence}),
                )

            older = SessionExecutionRecord(
                AgentExecutionOutcome(
                    AgentExecutionStatus.STUCK,
                    SupervisorReasonCode.PERIODIC_CYCLE,
                    finalized=True,
                    recoverable=True,
                ),
                1,
                datetime(2026, 7, 31, 12, tzinfo=UTC),
            )
            newer = SessionExecutionRecord(
                AgentExecutionOutcome(
                    AgentExecutionStatus.BUDGET_LIMITED,
                    SupervisorReasonCode.MODEL_STEP_LIMIT,
                    finalized=True,
                    recoverable=True,
                ),
                2,
                datetime(2026, 7, 31, 12, 1, tzinfo=UTC),
            )
            older_store = SqliteSessionStore(database)
            newer_store = SqliteSessionStore(database)
            results = await asyncio.gather(
                older_store.save_execution_record(session_id, older),
                newer_store.save_execution_record(session_id, newer),
                return_exceptions=True,
            )

            errors = [result for result in results if isinstance(result, BaseException)]
            self.assertLessEqual(len(errors), 1)
            if errors:
                self.assertIsInstance(errors[0], SessionError)
                self.assertIn("older event sequence", str(errors[0]))
            self.assertEqual(await setup_store.load_execution_record(session_id), newer)

    async def test_cross_process_writes_keep_the_newest_execution_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sessions.db"
            setup_store = SqliteSessionStore(database)
            await setup_store.initialize()
            session_id = await setup_store.create_session("/workspace", "fixture", "model")
            for sequence in (1, 2):
                await setup_store.append_event(
                    session_id,
                    AgentEvent.create(sequence, AgentEventKind.TURN_COMPLETED, {"step": sequence}),
                )

            older = SessionExecutionRecord(
                AgentExecutionOutcome(
                    AgentExecutionStatus.STUCK,
                    SupervisorReasonCode.PERIODIC_CYCLE,
                    finalized=True,
                    recoverable=True,
                ),
                1,
                datetime(2026, 7, 31, 12, tzinfo=UTC),
            )
            newer = SessionExecutionRecord(
                AgentExecutionOutcome(
                    AgentExecutionStatus.BUDGET_LIMITED,
                    SupervisorReasonCode.MODEL_STEP_LIMIT,
                    finalized=True,
                    recoverable=True,
                ),
                2,
                datetime(2026, 7, 31, 12, 1, tzinfo=UTC),
            )
            context = multiprocessing.get_context("spawn")
            ready_queue = context.Queue()
            result_queue = context.Queue()
            start_event = context.Event()
            processes = [
                context.Process(
                    target=_save_execution_record_in_process,
                    args=(
                        str(database),
                        session_id,
                        older,
                        ready_queue,
                        start_event,
                        result_queue,
                    ),
                ),
                context.Process(
                    target=_save_execution_record_in_process,
                    args=(
                        str(database),
                        session_id,
                        newer,
                        ready_queue,
                        start_event,
                        result_queue,
                    ),
                ),
            ]
            lock_connection = sqlite3.connect(database, timeout=30)
            results: list[str] = []
            try:
                lock_connection.execute("BEGIN IMMEDIATE")
                for process in processes:
                    process.start()
                for _ in processes:
                    self.assertEqual(ready_queue.get(timeout=10), "ready")
                start_event.set()
                lock_connection.commit()

                for _ in processes:
                    results.append(result_queue.get(timeout=30))
                for process in processes:
                    process.join(timeout=30)
                    self.assertFalse(process.is_alive())
                    self.assertEqual(process.exitcode, 0)
            finally:
                if lock_connection.in_transaction:
                    lock_connection.rollback()
                lock_connection.close()
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join(timeout=5)
                ready_queue.close()
                result_queue.close()
                ready_queue.join_thread()
                result_queue.join_thread()

            self.assertEqual(len(results), len(processes))
            self.assertTrue(
                all(result == "ok" or result.startswith("error:") for result in results)
            )
            errors = [result for result in results if result != "ok"]
            self.assertLessEqual(len(errors), 1)
            if errors:
                self.assertIn("SessionError", errors[0])
                self.assertIn("older event sequence", errors[0])
            self.assertEqual(await setup_store.load_execution_record(session_id), newer)

    async def test_background_wake_state_round_trips_and_is_not_forked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            source_id = await store.create_session("/workspace", "fixture", "model")
            state = BackgroundWakeState().record_terminal_task("task-1", enqueue=True)
            await store.save_background_wake_state(source_id, state)
            self.assertEqual(await store.load_background_wake_state(source_id), state)

            forked_id = await store.fork_session(source_id)
            self.assertEqual(
                await store.load_background_wake_state(forked_id),
                BackgroundWakeState(),
            )

            reopened = SqliteSessionStore(store.database_path)
            await reopened.initialize()
            self.assertEqual(await reopened.load_background_wake_state(source_id), state)

    async def test_current_plan_comment_count_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            session_id = await store.create_session("/workspace", "fixture", "model")
            plan = SessionPlan((PlanStep("Review the bounded feedback"),))
            await store.save_session_plan(session_id, plan)
            timestamp = datetime(2026, 7, 29, 14, tzinfo=UTC)
            for index in range(MAX_PLAN_COMMENTS):
                await store.add_plan_comment(
                    session_id,
                    plan,
                    PlanComment(
                        f"plan-comment-{index}",
                        1,
                        f"Comment {index}",
                        timestamp,
                    ),
                )

            with self.assertRaisesRegex(SessionError, "comment limit"):
                await store.add_plan_comment(
                    session_id,
                    plan,
                    PlanComment(
                        "plan-comment-overflow",
                        1,
                        "One comment too many",
                        timestamp,
                    ),
                )

    async def test_fork_copies_context_without_events_and_delete_cascades_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            source_id = await store.create_session(
                "/workspace",
                "fixture",
                "model",
                "profile-v1:fixture",
                SandboxProfile.WORKSPACE,
            )
            items = [
                Message(Role.USER, "fork searchable context"),
                PreservedContextItem(
                    ContextItemKind.REASONING,
                    {
                        "type": "reasoning",
                        "id": "reasoning-fork",
                        "encrypted_content": "opaque",
                    },
                ),
                Message(Role.ASSISTANT, "source answer"),
            ]
            await store.save_session_items(source_id, items)
            await store.update_session_title(source_id, "Shared fork title")
            plan = SessionPlan(
                (
                    PlanStep("Keep source context", PlanStepStatus.COMPLETED),
                    PlanStep("Continue from the fork", PlanStepStatus.IN_PROGRESS),
                ),
                "Verify that the fork keeps its work plan",
            )
            await store.save_session_plan(source_id, plan)
            await store.add_plan_comment(
                source_id,
                plan,
                PlanComment(
                    "plan-comment-source",
                    2,
                    "Do not drop the regression tests from this fork.",
                    datetime(2026, 7, 4, 0, 0, 30, tzinfo=UTC),
                ),
            )
            await store.create_session_task(
                source_id,
                SessionTask(
                    "task-source",
                    SessionTaskKind.PLAN_EXECUTION,
                    SessionTaskStatus.COMPLETED,
                    datetime(2026, 7, 4, tzinfo=UTC),
                    datetime(2026, 7, 4, 0, 1, tzinfo=UTC),
                ),
            )
            await store.append_event(
                source_id,
                AgentEvent.create(1, AgentEventKind.TURN_COMPLETED, {"step": 1}),
            )
            await store.bind_session_alias("acp-v1", "acp-source", source_id)

            forked_id = await store.fork_session(source_id)

            self.assertNotEqual(forked_id, source_id)
            forked = await store.get_session(forked_id)
            source = await store.get_session(source_id)
            self.assertEqual(forked.cwd, source.cwd)
            self.assertEqual(forked.provider, source.provider)
            self.assertEqual(forked.model, source.model)
            self.assertEqual(forked.context_affinity, source.context_affinity)
            self.assertIs(forked.sandbox_profile, source.sandbox_profile)
            self.assertEqual(forked.title, "Shared fork title")
            self.assertEqual(await store.load_session_items(forked_id), items)
            self.assertEqual(await store.load_session_plan(forked_id), plan)
            forked_comments = await store.list_plan_comments(forked_id, plan)
            self.assertEqual(len(forked_comments), 1)
            self.assertNotEqual(forked_comments[0].comment_id, "plan-comment-source")
            self.assertEqual(forked_comments[0].step_index, 2)
            self.assertEqual(
                forked_comments[0].content,
                "Do not drop the regression tests from this fork.",
            )
            self.assertEqual(await store.list_session_tasks(forked_id), [])
            self.assertEqual(await store.load_events(forked_id), [])
            search = await store.search_sessions("fork searchable")
            self.assertEqual(
                {hit.summary.id for hit in search.results},
                {source_id, forked_id},
            )

            await store.delete_session(source_id)

            with self.assertRaisesRegex(SessionError, "unknown session"):
                await store.get_session(source_id)
            with self.assertRaisesRegex(SessionError, "unknown session alias"):
                await store.resolve_session_alias("acp-v1", "acp-source")
            search = await store.search_sessions("fork searchable")
            self.assertEqual([hit.summary.id for hit in search.results], [forked_id])
            with self.assertRaisesRegex(SessionError, "unknown session"):
                await store.delete_session(source_id)
            with self.assertRaisesRegex(SessionError, "unknown session"):
                await store.fork_session(source_id)

    async def test_session_aliases_are_durable_unique_and_support_legacy_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            first_id = await store.create_session("/workspace", "fixture", "model")
            second_id = await store.create_session("/workspace", "fixture", "model")
            third_id = await store.create_session("/workspace", "fixture", "model")

            await store.bind_session_alias("acp-v1", "acp-visible", first_id)
            await store.bind_session_alias("acp-v1", "acp-visible", first_id)

            reopened = SqliteSessionStore(store.database_path)
            await reopened.initialize()
            self.assertEqual(
                await reopened.resolve_session_alias("acp-v1", "acp-visible"),
                first_id,
            )
            self.assertEqual(
                await reopened.resolve_session_alias("acp-v1", second_id),
                second_id,
            )
            self.assertEqual(
                await reopened.get_or_create_session_alias(
                    "acp-v1",
                    first_id,
                    "unused-proposal",
                ),
                "acp-visible",
            )
            concurrent = await asyncio.gather(
                reopened.get_or_create_session_alias(
                    "acp-v1",
                    third_id,
                    "acp-third-a",
                ),
                store.get_or_create_session_alias(
                    "acp-v1",
                    third_id,
                    "acp-third-b",
                ),
            )
            self.assertEqual(len(set(concurrent)), 1)
            self.assertIn(concurrent[0], {"acp-third-a", "acp-third-b"})
            with self.assertRaisesRegex(SessionError, "already bound"):
                await reopened.bind_session_alias("acp-v1", "acp-visible", second_id)
            with self.assertRaisesRegex(SessionError, "already has an alias"):
                await reopened.bind_session_alias("acp-v1", "another-alias", first_id)
            with self.assertRaisesRegex(SessionError, "unknown session"):
                await reopened.bind_session_alias("acp-v1", "missing", "missing")
            with self.assertRaisesRegex(SessionError, "unknown session alias"):
                await reopened.resolve_session_alias("acp-v1", "missing")
            with self.assertRaisesRegex(SessionError, "must not be empty"):
                await reopened.resolve_session_alias("", "acp-visible")
            with self.assertRaisesRegex(SessionError, "contains control"):
                await reopened.resolve_session_alias("acp-v1", "bad\nid")
            with self.assertRaisesRegex(SessionError, "too large"):
                await reopened.resolve_session_alias("acp-v1", "界" * 200)

    async def test_session_list_page_uses_stable_keyset_order_and_validates_cursor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            for session_id, day in (
                ("session-newest", 3),
                ("session-middle", 2),
                ("session-oldest", 1),
            ):
                timestamp = datetime(2026, 7, day, 12, tzinfo=UTC)
                await store.import_session(
                    SessionSnapshot(
                        SessionSummary(
                            id=session_id,
                            cwd="/workspace",
                            provider="fixture",
                            model="model",
                            created_at=timestamp,
                            updated_at=timestamp,
                        ),
                        (Message(Role.USER, session_id),),
                    )
                )

            first = await store.list_sessions_page(limit=2)
            second = await store.list_sessions_page(
                limit=2,
                before_updated_at=first[-1].updated_at,
                before_id=first[-1].id,
            )

            self.assertEqual(
                [summary.id for summary in first],
                ["session-newest", "session-middle"],
            )
            self.assertEqual([summary.id for summary in second], ["session-oldest"])
            with self.assertRaisesRegex(SessionError, "provided together"):
                await store.list_sessions_page(
                    limit=2,
                    before_updated_at=first[-1].updated_at,
                )
            with self.assertRaisesRegex(SessionError, "timezone-aware"):
                await store.list_sessions_page(
                    limit=2,
                    before_updated_at=datetime(2026, 7, 1, tzinfo=UTC).replace(tzinfo=None),
                    before_id="session-oldest",
                )

    async def test_manual_title_update_is_atomic_persistent_and_searchable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            session_id = await store.create_session("/workspace", "fixture", "model")
            await store.save_messages(
                session_id,
                [Message(Role.USER, "original visible prompt")],
            )

            renamed = await store.update_session_title(
                session_id,
                "  Manual\n  searchable   title  ",
            )

            self.assertEqual(renamed.title, "Manual searchable title")
            manual_search = await store.search_sessions("manual searchable")
            self.assertEqual([hit.summary.id for hit in manual_search.results], [session_id])
            self.assertEqual(manual_search.results[0].matched_fields, ("title",))
            original_search = await store.search_sessions("original visible")
            self.assertEqual(original_search.results[0].matched_fields, ("content",))

            await store.save_messages(
                session_id,
                [
                    Message(Role.USER, "original visible prompt"),
                    Message(Role.ASSISTANT, "continued after rename"),
                ],
            )
            self.assertEqual(
                (await store.get_session(session_id)).title,
                "Manual searchable title",
            )

            with (
                patch(
                    "neuro_code.infrastructure.persistence.sqlite_session._upsert_search_document",
                    side_effect=RuntimeError("injected index failure"),
                ),
                self.assertRaisesRegex(RuntimeError, "injected index failure"),
            ):
                await store.update_session_title(session_id, "Rolled back title")
            self.assertEqual(
                (await store.get_session(session_id)).title,
                "Manual searchable title",
            )
            self.assertEqual((await store.search_sessions("rolled back")).results, ())

            truncated = await store.update_session_title(session_id, "x" * 250)
            self.assertEqual(truncated.title, "x" * 200)
            with self.assertRaisesRegex(SessionError, "title must not be empty"):
                await store.update_session_title(session_id, " \n\t ")
            with self.assertRaisesRegex(SessionError, "unknown session"):
                await store.update_session_title("missing", "Valid title")

    async def test_schema_v1_is_migrated_without_rewriting_existing_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sessions.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE schema_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL
                );
                INSERT INTO schema_meta(singleton, version) VALUES (1, 1);
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    messages_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE events (
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, sequence)
                );
                INSERT INTO sessions(id, cwd, provider, model)
                VALUES ('legacy-id', '/legacy', 'xai-responses', 'xai-test-model');
                """
            )
            connection.commit()
            connection.close()

            store = SqliteSessionStore(database)
            await store.initialize()

            summary = await store.get_session("legacy-id")
            self.assertEqual(summary.provider, "xai-responses")
            self.assertIsNone(summary.context_affinity)
            self.assertIsNone(summary.sandbox_profile)
            migrated = sqlite3.connect(database)
            version = migrated.execute(
                "SELECT version FROM schema_meta WHERE singleton = 1"
            ).fetchone()
            columns = {row[1] for row in migrated.execute("PRAGMA table_info(sessions)").fetchall()}
            tables = {row[0] for row in migrated.execute("SELECT name FROM sqlite_master")}
            task_columns = {
                row[1] for row in migrated.execute("PRAGMA table_info(session_tasks)").fetchall()
            }
            migrated.close()
            self.assertEqual(version, (11,))
            self.assertIn("context_affinity", columns)
            self.assertIn("sandbox_profile", columns)
            self.assertIn("plan_json", columns)
            self.assertIn("session_tasks", tables)
            self.assertIn("session_plan_comments", tables)
            self.assertIn("plan_snapshot_json", task_columns)

    async def test_schema_v2_peek_is_read_only_then_migrates_as_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sessions.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE schema_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL
                );
                INSERT INTO schema_meta(singleton, version) VALUES (1, 2);
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    messages_json TEXT NOT NULL DEFAULT '[]',
                    context_affinity TEXT
                );
                CREATE TABLE events (
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, sequence)
                );
                INSERT INTO sessions(id, cwd, provider, model, context_affinity)
                VALUES ('v2-id', '/legacy', 'fixture', 'model', 'profile-v1:old');
                """
            )
            connection.commit()
            connection.close()
            before = database.read_bytes()

            store = SqliteSessionStore(database)
            self.assertIsNone(await store.peek_session_sandbox_profile("v2-id"))
            self.assertEqual(database.read_bytes(), before)
            before_migration = sqlite3.connect(database)
            self.assertEqual(
                before_migration.execute(
                    "SELECT version FROM schema_meta WHERE singleton = 1"
                ).fetchone(),
                (2,),
            )
            before_migration.close()

            await store.initialize()
            summary = await store.get_session("v2-id")
            self.assertEqual(summary.context_affinity, "profile-v1:old")
            self.assertIsNone(summary.sandbox_profile)
            migrated = sqlite3.connect(database)
            self.assertEqual(
                migrated.execute("SELECT version FROM schema_meta WHERE singleton = 1").fetchone(),
                (11,),
            )
            migrated.close()

    async def test_schema_v3_migration_backfills_escaped_content_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sessions.db"
            messages = [
                {
                    "role": "user",
                    "content": 'debug escaped newlines\nand "quoted" sqlite content',
                }
            ]
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE schema_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL
                );
                INSERT INTO schema_meta(singleton, version) VALUES (1, 3);
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    messages_json TEXT NOT NULL DEFAULT '[]',
                    context_affinity TEXT,
                    sandbox_profile TEXT
                );
                CREATE TABLE events (
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, sequence)
                );
                """
            )
            connection.execute(
                """
                INSERT INTO sessions(
                    id, cwd, provider, model, messages_json, sandbox_profile
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "v3-id",
                    "/workspace",
                    "fixture",
                    "model",
                    json.dumps(messages),
                    "workspace",
                ),
            )
            connection.commit()
            connection.close()

            store = SqliteSessionStore(database)
            await store.initialize()

            summary = await store.get_session("v3-id")
            self.assertEqual(
                summary.title,
                'debug escaped newlines and "quoted" sqlite content',
            )
            page = await store.search_sessions(
                "escaped quoted",
                cwd="/workspace",
                include_content=True,
            )
            self.assertEqual([hit.summary.id for hit in page.results], ["v3-id"])
            self.assertIn("content", page.results[0].matched_fields)
            self.assertIsNotNone(page.results[0].snippet)

            migrated = sqlite3.connect(database)
            self.assertEqual(
                migrated.execute("SELECT version FROM schema_meta WHERE singleton = 1").fetchone(),
                (11,),
            )
            tables = {
                row[0]
                for row in migrated.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
            }
            migrated.close()
            self.assertIn("session_search_documents", tables)
            self.assertIn("session_search_fts", tables)

    async def test_schema_v4_migration_adds_session_aliases_without_rewriting_sessions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sessions.db"
            store = SqliteSessionStore(database)
            await store.initialize()
            session_id = await store.create_session("/workspace", "fixture", "model")
            await store.save_messages(session_id, [Message(Role.USER, "preserved")])
            connection = sqlite3.connect(database)
            connection.execute("DROP TABLE session_aliases")
            connection.execute("UPDATE schema_meta SET version = 4 WHERE singleton = 1")
            connection.commit()
            connection.close()

            migrated = SqliteSessionStore(database)
            await migrated.initialize()
            self.assertEqual(
                await migrated.load_messages(session_id),
                [Message(Role.USER, "preserved")],
            )
            await migrated.bind_session_alias("acp-v1", "acp-visible", session_id)
            self.assertEqual(
                await migrated.resolve_session_alias("acp-v1", "acp-visible"),
                session_id,
            )
            connection = sqlite3.connect(database)
            self.assertEqual(
                connection.execute(
                    "SELECT version FROM schema_meta WHERE singleton = 1"
                ).fetchone(),
                (11,),
            )
            connection.close()

    async def test_initialize_is_atomic_when_search_backfill_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sessions.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE schema_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL
                );
                INSERT INTO schema_meta(singleton, version) VALUES (1, 3);
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    messages_json TEXT NOT NULL DEFAULT '[]',
                    context_affinity TEXT,
                    sandbox_profile TEXT
                );
                CREATE TABLE events (
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, sequence)
                );
                """
            )
            connection.close()

            store = SqliteSessionStore(database)
            with (
                patch(
                    "neuro_code.infrastructure.persistence.sqlite_session._backfill_search_documents",
                    side_effect=RuntimeError("injected backfill failure"),
                ),
                self.assertRaisesRegex(RuntimeError, "injected backfill failure"),
            ):
                await store.initialize()

            failed = sqlite3.connect(database)
            self.assertEqual(
                failed.execute("SELECT version FROM schema_meta WHERE singleton = 1").fetchone(),
                (3,),
            )
            columns = {row[1] for row in failed.execute("PRAGMA table_info(sessions)")}
            tables = {
                row[0]
                for row in failed.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
            failed.close()
            self.assertNotIn("title", columns)
            self.assertNotIn("session_search_documents", tables)
            self.assertNotIn("session_search_fts", tables)

            await store.initialize()
            recovered = sqlite3.connect(database)
            self.assertEqual(
                recovered.execute("SELECT version FROM schema_meta WHERE singleton = 1").fetchone(),
                (11,),
            )
            recovered.close()

    async def test_concurrent_initialize_is_serialized_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sessions.db"
            stores = [SqliteSessionStore(database) for _ in range(8)]

            await asyncio.gather(*(store.initialize() for store in stores))
            session_id = await stores[0].create_session("/workspace", "fixture", "model")
            await stores[0].save_messages(
                session_id,
                [Message(Role.USER, "concurrent migration search marker")],
            )
            await asyncio.gather(*(store.initialize() for store in reversed(stores)))

            page = await stores[-1].search_sessions("concurrent marker")
            self.assertEqual([hit.summary.id for hit in page.results], [session_id])
            connection = sqlite3.connect(database)
            self.assertEqual(
                connection.execute(
                    "SELECT version FROM schema_meta WHERE singleton = 1"
                ).fetchone(),
                (11,),
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM session_search_documents").fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM session_search_fts").fetchone(),
                (1,),
            )
            connection.close()

    async def test_search_indexes_visible_content_with_filters_and_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            primary_id = await store.create_session(
                "/workspace",
                "fixture",
                "model",
                sandbox_profile=SandboxProfile.WORKSPACE,
            )
            primary_items = [
                Message(
                    Role.USER,
                    "<system-reminder>private injected rules</system-reminder>\n"
                    "Fix SQLite session search for escaped quoted content across all platforms",
                ),
                PreservedContextItem(
                    ContextItemKind.REASONING,
                    {
                        "type": "reasoning",
                        "id": "private",
                        "summary": [{"type": "summary_text", "text": "privatecontextmarker"}],
                        "encrypted_content": "privateciphermarker",
                    },
                ),
                Message(
                    Role.ASSISTANT,
                    "I will inspect the index.",
                    tool_calls=(
                        ToolCall(
                            "call-1",
                            "read_file",
                            {"path": "src/search_index.py", "purpose": "toolmarker"},
                        ),
                    ),
                    reasoning_content="privatethoughtmarker",
                ),
                Message(
                    Role.TOOL,
                    "privatetoolresultmarker",
                    name="read_file",
                    tool_call_id="call-1",
                ),
            ]
            await store.save_session_items(primary_id, primary_items)

            second_id = await store.create_session("/workspace", "fixture", "model")
            await store.save_messages(
                second_id,
                [Message(Role.USER, "SQLite migration notes for another session")],
            )
            other_workspace_id = await store.create_session("/other", "fixture", "model")
            await store.save_messages(
                other_workspace_id,
                [Message(Role.USER, "SQLite search belongs to another workspace")],
            )

            summary = await store.get_session(primary_id)
            self.assertEqual(
                summary.title,
                "Fix SQLite session search for escaped quoted content across all",
            )
            await store.save_session_items(
                primary_id,
                [*primary_items, Message(Role.USER, "a later title must not replace the first")],
            )
            self.assertEqual((await store.get_session(primary_id)).title, summary.title)

            page = await store.search_sessions(
                "escaped quoted",
                cwd="/workspace",
                include_content=True,
            )
            self.assertEqual([hit.summary.id for hit in page.results], [primary_id])
            self.assertEqual(page.results[0].matched_fields, ("title", "content"))
            self.assertIsNotNone(page.results[0].snippet)

            tool_page = await store.search_sessions("read_file", cwd="/workspace")
            self.assertEqual([hit.summary.id for hit in tool_page.results], [primary_id])
            for private_query in (
                "privatecontextmarker",
                "privateciphermarker",
                "privatethoughtmarker",
                "privatetoolresultmarker",
                "toolmarker",
                "search_index",
            ):
                self.assertEqual(
                    (await store.search_sessions(private_query)).results,
                    (),
                )

            first_page = await store.search_sessions("SQLite", cwd="/workspace", limit=1)
            self.assertEqual(first_page.total_estimate, 2)
            self.assertEqual(first_page.next_offset, 1)
            second_page = await store.search_sessions(
                "SQLite",
                cwd="/workspace",
                limit=1,
                offset=1,
            )
            self.assertEqual(second_page.total_estimate, 2)
            self.assertIsNone(second_page.next_offset)
            self.assertNotEqual(
                first_page.results[0].summary.id,
                second_page.results[0].summary.id,
            )
            self.assertNotIn(
                other_workspace_id,
                {hit.summary.id for hit in first_page.results + second_page.results},
            )

            fallback = await store.search_sessions("quoted migration", cwd="/workspace")
            self.assertEqual(fallback.total_estimate, 2)

            for operation in (
                store.search_sessions(""),
                store.search_sessions("***"),
                store.search_sessions("query", limit=0),
                store.search_sessions("query", offset=-1),
            ):
                with self.assertRaises(SessionError):
                    await operation

    async def test_search_handles_unicode_syntax_ranking_and_bounded_snippets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()

            title_id = await store.create_session("/workspace", "fixture", "model")
            await store.save_messages(title_id, [Message(Role.USER, "priorityneedle")])
            body_id = await store.create_session("/workspace", "fixture", "model")
            await store.save_messages(
                body_id,
                [
                    Message(Role.USER, "Discuss unrelated adapters"),
                    Message(Role.ASSISTANT, "The body mentions priorityneedle once."),
                ],
            )
            unicode_id = await store.create_session("/workspace", "fixture", "model")
            await store.save_messages(
                unicode_id,
                [Message(Role.USER, "修复 中文会话 café 搜索")],
            )
            snippet_id = await store.create_session("/workspace", "fixture", "model")
            await store.save_messages(
                snippet_id,
                [
                    Message(Role.USER, "Bound the generated snippet"),
                    Message(Role.ASSISTANT, "snippetneedle" + ("x" * 1_000)),
                ],
            )

            ranked = await store.search_sessions("priorityneedle", cwd="/workspace")
            self.assertEqual(
                [hit.summary.id for hit in ranked.results],
                [title_id, body_id],
            )
            self.assertIn("title", ranked.results[0].matched_fields)
            self.assertEqual(ranked.results[1].matched_fields, ("content",))

            unicode_page = await store.search_sessions(
                "《中文会话》 CAFÉ",
                cwd="/workspace",
            )
            self.assertEqual([hit.summary.id for hit in unicode_page.results], [unicode_id])
            sanitized = await store.search_sessions(
                'priorityneedle OR "*"',
                cwd="/workspace",
            )
            self.assertEqual(
                {hit.summary.id for hit in sanitized.results},
                {title_id, body_id},
            )

            snippet_page = await store.search_sessions(
                "snippetneedle",
                cwd="/workspace",
                include_content=True,
            )
            self.assertEqual([hit.summary.id for hit in snippet_page.results], [snippet_id])
            self.assertIsNotNone(snippet_page.results[0].snippet)
            self.assertEqual(len(snippet_page.results[0].snippet or ""), 500)

            for operation in (
                store.search_sessions("x" * 1_001),
                store.search_sessions("query", cwd=""),
                store.search_sessions("query", limit=1_001),
                store.search_sessions("query", offset=1_000_001),
            ):
                with self.assertRaises(SessionError):
                    await operation

    async def test_sandbox_peek_never_creates_state_and_rejects_corrupt_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing" / "sessions.db"
            missing_store = SqliteSessionStore(missing)
            self.assertIsNone(await missing_store.peek_session_sandbox_profile("absent"))
            self.assertFalse(missing.parent.exists())

            database = root / "sessions.db"
            store = SqliteSessionStore(database)
            await store.initialize()
            session_id = await store.create_session(
                "/workspace",
                "fixture",
                "model",
                sandbox_profile=SandboxProfile.WORKSPACE,
            )
            names_before = set(os.listdir(root))
            bytes_before = database.read_bytes()
            self.assertIs(
                await store.peek_session_sandbox_profile(session_id),
                SandboxProfile.WORKSPACE,
            )
            self.assertEqual(database.read_bytes(), bytes_before)
            self.assertEqual(set(os.listdir(root)), names_before)

            active = sqlite3.connect(database)
            active.execute(
                "UPDATE sessions SET sandbox_profile = 'strict' WHERE id = ?",
                (session_id,),
            )
            active.commit()
            with self.assertRaisesRegex(SessionError, "active WAL"):
                await store.peek_session_sandbox_profile(session_id)
            active.close()

            connection = sqlite3.connect(database)
            connection.execute(
                "UPDATE sessions SET sandbox_profile = 'custom-unsafe' WHERE id = ?",
                (session_id,),
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(SessionError, "unsupported sandbox profile"):
                await store.peek_session_sandbox_profile(session_id)
            with self.assertRaisesRegex(SessionError, "unsupported sandbox profile"):
                await store.get_session(session_id)

    async def test_unknown_sessions_and_invalid_limit_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            for operation in (
                store.get_session("missing"),
                store.load_messages("missing"),
                store.load_session_items("missing"),
                store.load_session_plan("missing"),
                store.next_event_sequence("missing"),
                store.update_session_provider("missing", "provider", "model", None),
                store.list_sessions(limit=0),
            ):
                with self.assertRaises(SessionError):
                    await operation


if __name__ == "__main__":
    unittest.main()
