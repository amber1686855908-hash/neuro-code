from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import neuro_code.infrastructure.persistence.sqlite_session_turns as sqlite_session_turns_module
from neuro_code.application.sessions.recovery import (
    TurnRecoveryInspection,
    TurnRecoveryService,
)
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.messages import ContentPart, Message, Role
from neuro_code.domain.execution import (
    MAX_TURN_INPUT_BYTES,
    AgentExecutionOutcome,
    AgentExecutionStatus,
    SessionExecutionRecord,
    TurnInput,
    TurnRecoveryAttempt,
    TurnRecoveryFact,
    TurnRecoveryFactKind,
    TurnRecoveryResolution,
    TurnRecoveryStage,
    TurnRecoveryStatus,
    TurnSource,
    VerificationRequirement,
    VerificationRequirementsSnapshot,
)
from neuro_code.domain.plans import PlanStep, SessionPlan
from neuro_code.domain.session_tasks import SessionTask, SessionTaskKind, SessionTaskStatus
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.shared.errors import ConfigurationError, SessionError


def _run_process_death(script: str, database: Path) -> int:
    environment = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = source_root + os.pathsep + environment.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-c", script, str(database)],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        check=False,
    )
    return completed.returncode


class CrashRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self._temporary_directory.name) / "sessions.db"
        self.store = SqliteSessionStore(self.database)
        await self.store.initialize()
        self.session_id = await self.store.create_session("/workspace", "provider", "model")

    async def asyncTearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _attempt(self, *, turn_id: str = "turn-1", prompt: str = "hello") -> TurnRecoveryAttempt:
        return TurnRecoveryAttempt.create(
            turn_id=turn_id,
            session_id=self.session_id,
            input=TurnInput(
                prompt,
                (ContentPart.from_text(prompt),),
            ),
            accepted_at=datetime.now(UTC),
        )

    def _plan(self) -> SessionPlan:
        return SessionPlan((PlanStep("execute the saved plan"),))

    async def test_new_plan_acceptance_owns_running_task_exactly(self) -> None:
        plan = self._plan()
        now = datetime.now(UTC)
        task = SessionTask(
            "task-plan-new",
            SessionTaskKind.PLAN_EXECUTION,
            SessionTaskStatus.RUNNING,
            now,
            plan_snapshot=plan,
        )
        attempt = TurnRecoveryAttempt.create(
            turn_id="turn-plan-new",
            session_id=self.session_id,
            input=TurnInput(
                "execute the saved plan",
                (ContentPart.from_text("execute the saved plan"),),
                plan_execution_requested=True,
                plan_execution_task_id=task.task_id,
            ),
            task_id=task.task_id,
            accepted_at=now,
        )

        await self.store.start_plan_turn_attempt(attempt, task=task)

        loaded_attempt = (await self.store.load_open_turn_attempts(self.session_id))[0]
        loaded_task = await self.store.get_session_task(self.session_id, task.task_id)
        self.assertEqual(loaded_attempt.task_id, task.task_id)
        self.assertIsNotNone(loaded_task)
        assert loaded_task is not None
        self.assertEqual(loaded_task.status, SessionTaskStatus.RUNNING)

    async def test_plan_acceptance_validates_atomic_ownership_modes(self) -> None:
        plan = self._plan()
        now = datetime.now(UTC)

        def plan_attempt(task_id: str | None) -> TurnRecoveryAttempt:
            return TurnRecoveryAttempt.create(
                turn_id=f"turn-plan-validation-{task_id or 'none'}",
                session_id=self.session_id,
                input=TurnInput(
                    "execute the saved plan",
                    (ContentPart.from_text("execute the saved plan"),),
                    plan_execution_requested=True,
                    plan_execution_task_id=task_id,
                ),
                task_id=task_id,
                accepted_at=now,
            )

        running_task = SessionTask(
            "task-plan-validation",
            SessionTaskKind.PLAN_EXECUTION,
            SessionTaskStatus.RUNNING,
            now,
            plan_snapshot=plan,
        )
        with self.assertRaises(TypeError):
            await self.store.start_plan_turn_attempt("not an attempt", task=running_task)  # type: ignore[arg-type]
        with self.assertRaisesRegex(SessionError, "input is required"):
            await self.store.start_plan_turn_attempt(self._attempt(), task=running_task)
        with self.assertRaisesRegex(SessionError, "one task ownership mode"):
            await self.store.start_plan_turn_attempt(plan_attempt(None))
        with self.assertRaisesRegex(SessionError, "one task ownership mode"):
            await self.store.start_plan_turn_attempt(
                plan_attempt(running_task.task_id),
                task=running_task,
                queued_task_id=running_task.task_id,
            )

        with self.assertRaisesRegex(SessionError, "plan execution task"):
            await self.store.start_plan_turn_attempt(
                plan_attempt("task-subagent"),
                task=SessionTask(
                    "task-subagent",
                    SessionTaskKind.SUBAGENT,
                    SessionTaskStatus.RUNNING,
                    now,
                ),
            )
        with self.assertRaisesRegex(SessionError, "running task"):
            await self.store.start_plan_turn_attempt(
                plan_attempt("task-plan-queued-validation"),
                task=SessionTask(
                    "task-plan-queued-validation",
                    SessionTaskKind.PLAN_EXECUTION,
                    SessionTaskStatus.QUEUED,
                    now,
                    plan_snapshot=plan,
                ),
            )
        with self.assertRaisesRegex(SessionError, "does not match"):
            await self.store.start_plan_turn_attempt(
                plan_attempt("task-plan-other"),
                task=running_task,
            )
        with self.assertRaisesRegex(SessionError, "queued start time"):
            await self.store.start_plan_turn_attempt(
                plan_attempt(running_task.task_id),
                task=running_task,
                started_at=now,
            )
        with self.assertRaisesRegex(SessionError, "does not match"):
            await self.store.start_plan_turn_attempt(
                plan_attempt("task-plan-queued-validation"),
                queued_task_id="task-plan-different",
            )
        with self.assertRaisesRegex(SessionError, "timezone-aware"):
            await self.store.start_plan_turn_attempt(
                plan_attempt("task-plan-queued-validation"),
                queued_task_id="task-plan-queued-validation",
                started_at=now.replace(tzinfo=None),
            )
        with self.assertRaisesRegex(SessionError, "unknown session task"):
            await self.store.start_plan_turn_attempt(
                plan_attempt("task-plan-queued-validation"),
                queued_task_id="task-plan-queued-validation",
            )

    async def test_queued_plan_acceptance_preserves_task_identity(self) -> None:
        plan = self._plan()
        queued_task = SessionTask(
            "task-plan-queued",
            SessionTaskKind.PLAN_EXECUTION,
            SessionTaskStatus.QUEUED,
            datetime.now(UTC),
            plan_snapshot=plan,
        )
        await self.store.create_session_task(self.session_id, queued_task)
        attempt = TurnRecoveryAttempt.create(
            turn_id="turn-plan-queued",
            session_id=self.session_id,
            input=TurnInput(
                "execute the saved plan",
                (ContentPart.from_text("execute the saved plan"),),
                plan_execution_requested=True,
                plan_execution_task_id=queued_task.task_id,
            ),
            task_id=queued_task.task_id,
            accepted_at=datetime.now(UTC),
        )

        await self.store.start_plan_turn_attempt(
            attempt,
            queued_task_id=queued_task.task_id,
            started_at=datetime.now(UTC),
        )

        loaded_attempt = (await self.store.load_open_turn_attempts(self.session_id))[0]
        loaded_task = await self.store.get_session_task(self.session_id, queued_task.task_id)
        self.assertEqual(loaded_attempt.task_id, queued_task.task_id)
        self.assertIsNotNone(loaded_task)
        assert loaded_task is not None
        self.assertEqual(loaded_task.status, SessionTaskStatus.RUNNING)

    async def test_plan_abandon_cancels_the_linked_running_task(self) -> None:
        plan = self._plan()
        now = datetime.now(UTC)
        task = SessionTask(
            "task-plan-abandon",
            SessionTaskKind.PLAN_EXECUTION,
            SessionTaskStatus.RUNNING,
            now,
            plan_snapshot=plan,
        )
        attempt = TurnRecoveryAttempt.create(
            turn_id="turn-plan-abandon",
            session_id=self.session_id,
            input=TurnInput(
                "execute the saved plan",
                (ContentPart.from_text("execute the saved plan"),),
                plan_execution_requested=True,
                plan_execution_task_id=task.task_id,
            ),
            task_id=task.task_id,
            accepted_at=now,
        )
        await self.store.start_turn_attempt(attempt)
        await self.store.create_session_task(self.session_id, task)

        resolved = await TurnRecoveryService(self.store).abandon(
            self.session_id,
            attempt.turn_id,
        )

        self.assertEqual(resolved.status, TurnRecoveryStatus.ABANDONED)
        loaded_task = await self.store.get_session_task(self.session_id, task.task_id)
        self.assertIsNotNone(loaded_task)
        assert loaded_task is not None
        self.assertEqual(loaded_task.status, SessionTaskStatus.CANCELLED)
        self.assertEqual(
            [event["kind"] for event in await self.store.load_events(self.session_id)],
            [
                AgentEventKind.SESSION_TASK_CANCELLED.value,
                AgentEventKind.TURN_ABANDONED.value,
            ],
        )

    async def test_new_plan_acceptance_rolls_back_when_task_insert_fails(self) -> None:
        plan = self._plan()
        now = datetime.now(UTC)
        task = SessionTask(
            "task-plan-insert-failure",
            SessionTaskKind.PLAN_EXECUTION,
            SessionTaskStatus.RUNNING,
            now,
            plan_snapshot=plan,
        )
        attempt = TurnRecoveryAttempt.create(
            turn_id="turn-plan-insert-failure",
            session_id=self.session_id,
            input=TurnInput(
                "execute the saved plan",
                (ContentPart.from_text("execute the saved plan"),),
                plan_execution_requested=True,
                plan_execution_task_id=task.task_id,
            ),
            task_id=task.task_id,
            accepted_at=now,
        )

        with (
            patch(
                "neuro_code.infrastructure.persistence.sqlite_session_turns._insert_session_task_row",
                side_effect=RuntimeError("injected task insert failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            await self.store.start_plan_turn_attempt(attempt, task=task)

        self.assertEqual(await self.store.load_open_turn_attempts(self.session_id), [])
        self.assertIsNone(await self.store.get_session_task(self.session_id, task.task_id))

    async def test_queued_plan_acceptance_rolls_back_when_activation_fails(self) -> None:
        plan = self._plan()
        queued_task = SessionTask(
            "task-plan-activation-failure",
            SessionTaskKind.PLAN_EXECUTION,
            SessionTaskStatus.QUEUED,
            datetime.now(UTC),
            plan_snapshot=plan,
        )
        await self.store.create_session_task(self.session_id, queued_task)
        attempt = TurnRecoveryAttempt.create(
            turn_id="turn-plan-activation-failure",
            session_id=self.session_id,
            input=TurnInput(
                "execute the saved plan",
                (ContentPart.from_text("execute the saved plan"),),
                plan_execution_requested=True,
                plan_execution_task_id=queued_task.task_id,
            ),
            task_id=queued_task.task_id,
            accepted_at=datetime.now(UTC),
        )

        with (
            patch(
                "neuro_code.infrastructure.persistence.sqlite_session_turns._start_session_task_row",
                side_effect=RuntimeError("injected task activation failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            await self.store.start_plan_turn_attempt(
                attempt,
                queued_task_id=queued_task.task_id,
                started_at=datetime.now(UTC),
            )

        self.assertEqual(await self.store.load_open_turn_attempts(self.session_id), [])
        loaded_task = await self.store.get_session_task(self.session_id, queued_task.task_id)
        self.assertIsNotNone(loaded_task)
        assert loaded_task is not None
        self.assertEqual(loaded_task.status, SessionTaskStatus.QUEUED)

    async def _start_linked_plan_attempt(
        self,
        *,
        turn_id: str,
        task_id: str,
    ) -> tuple[TurnRecoveryAttempt, SessionTask]:
        plan = self._plan()
        now = datetime.now(UTC)
        task = SessionTask(
            task_id,
            SessionTaskKind.PLAN_EXECUTION,
            SessionTaskStatus.RUNNING,
            now,
            plan_snapshot=plan,
        )
        attempt = TurnRecoveryAttempt.create(
            turn_id=turn_id,
            session_id=self.session_id,
            input=TurnInput(
                "execute the saved plan",
                (ContentPart.from_text("execute the saved plan"),),
                plan_execution_requested=True,
                plan_execution_task_id=task.task_id,
            ),
            task_id=task.task_id,
            accepted_at=now,
        )
        await self.store.start_plan_turn_attempt(attempt, task=task)
        return attempt, task

    async def test_plan_abandon_rolls_back_when_task_terminalization_fails(self) -> None:
        attempt, task = await self._start_linked_plan_attempt(
            turn_id="turn-plan-terminalization-failure",
            task_id="task-plan-terminalization-failure",
        )
        service = TurnRecoveryService(self.store)
        with (
            patch(
                "neuro_code.infrastructure.persistence.sqlite_session_turns._persist_task_terminal",
                side_effect=RuntimeError("injected task terminalization failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            await service.abandon(self.session_id, attempt.turn_id)

        self.assertEqual(len(await self.store.load_open_turn_attempts(self.session_id)), 1)
        loaded_task = await self.store.get_session_task(self.session_id, task.task_id)
        self.assertIsNotNone(loaded_task)
        assert loaded_task is not None
        self.assertEqual(loaded_task.status, SessionTaskStatus.RUNNING)
        self.assertEqual(await self.store.load_events(self.session_id), [])

    async def test_plan_abandon_rolls_back_when_task_event_fails(self) -> None:
        attempt, task = await self._start_linked_plan_attempt(
            turn_id="turn-plan-task-event-failure",
            task_id="task-plan-task-event-failure",
        )
        with (
            patch(
                "neuro_code.infrastructure.persistence.sqlite_session_turns._insert_event_row",
                side_effect=RuntimeError("injected task event failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            await TurnRecoveryService(self.store).abandon(self.session_id, attempt.turn_id)

        self.assertEqual(len(await self.store.load_open_turn_attempts(self.session_id)), 1)
        loaded_task = await self.store.get_session_task(self.session_id, task.task_id)
        self.assertIsNotNone(loaded_task)
        assert loaded_task is not None
        self.assertEqual(loaded_task.status, SessionTaskStatus.RUNNING)
        self.assertEqual(await self.store.load_events(self.session_id), [])

    async def test_plan_abandon_rolls_back_when_turn_event_fails(self) -> None:
        attempt, task = await self._start_linked_plan_attempt(
            turn_id="turn-plan-turn-event-failure",
            task_id="task-plan-turn-event-failure",
        )
        original_insert_event_row = sqlite_session_turns_module._insert_event_row

        def fail_turn_event(
            connection: sqlite3.Connection,
            *,
            session_id: str,
            event: AgentEvent,
            payload: str | None = None,
        ) -> None:
            if event.kind is AgentEventKind.TURN_ABANDONED:
                raise RuntimeError("injected turn event failure")
            original_insert_event_row(
                connection,
                session_id=session_id,
                event=event,
                payload=payload,
            )

        with (
            patch(
                "neuro_code.infrastructure.persistence.sqlite_session_turns._insert_event_row",
                side_effect=fail_turn_event,
            ),
            self.assertRaises(RuntimeError),
        ):
            await TurnRecoveryService(self.store).abandon(self.session_id, attempt.turn_id)

        self.assertEqual(len(await self.store.load_open_turn_attempts(self.session_id)), 1)
        loaded_task = await self.store.get_session_task(self.session_id, task.task_id)
        self.assertIsNotNone(loaded_task)
        assert loaded_task is not None
        self.assertEqual(loaded_task.status, SessionTaskStatus.RUNNING)
        self.assertEqual(await self.store.load_events(self.session_id), [])

    async def test_plan_abandon_rolls_back_when_attempt_resolution_fails(self) -> None:
        attempt, task = await self._start_linked_plan_attempt(
            turn_id="turn-plan-attempt-resolution-failure",
            task_id="task-plan-attempt-resolution-failure",
        )
        with (
            patch(
                "neuro_code.infrastructure.persistence.sqlite_session_turns._resolve_abandoned_turn_attempt",
                side_effect=RuntimeError("injected attempt resolution failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            await TurnRecoveryService(self.store).abandon(self.session_id, attempt.turn_id)

        self.assertEqual(len(await self.store.load_open_turn_attempts(self.session_id)), 1)
        loaded_task = await self.store.get_session_task(self.session_id, task.task_id)
        self.assertIsNotNone(loaded_task)
        assert loaded_task is not None
        self.assertEqual(loaded_task.status, SessionTaskStatus.RUNNING)
        self.assertEqual(await self.store.load_events(self.session_id), [])

    async def test_plan_abandon_requires_exact_running_plan_task_at_storage_boundary(self) -> None:
        attempt, task = await self._start_linked_plan_attempt(
            turn_id="turn-plan-abandon-storage-validation",
            task_id="task-plan-abandon-storage-validation",
        )
        cancelled_task = task.finish(SessionTaskStatus.CANCELLED, finished_at=datetime.now(UTC))
        turn_event = AgentEvent.create(
            2,
            AgentEventKind.TURN_ABANDONED,
            {"turn_id": attempt.turn_id},
        )
        with self.assertRaisesRegex(SessionError, "must be cancelled"):
            await self.store.abandon_turn_attempt(
                self.session_id,
                attempt.turn_id,
                turn_event,
                "test",
                task=task,
                task_event=AgentEvent.create(
                    1,
                    AgentEventKind.SESSION_TASK_CANCELLED,
                    {"task": cancelled_task.to_dict()},
                ),
            )
        with self.assertRaisesRegex(SessionError, "task event cannot exist"):
            await self.store.abandon_turn_attempt(
                self.session_id,
                attempt.turn_id,
                turn_event,
                "test",
                task_event=AgentEvent.create(
                    1,
                    AgentEventKind.SESSION_TASK_CANCELLED,
                    {"task": cancelled_task.to_dict()},
                ),
            )
        with self.assertRaisesRegex(SessionError, "task-cancel event"):
            await self.store.abandon_turn_attempt(
                self.session_id,
                attempt.turn_id,
                turn_event,
                "test",
                task=cancelled_task,
                task_event=AgentEvent.create(1, AgentEventKind.TURN_ABANDONED),
            )
        with self.assertRaisesRegex(SessionError, "must precede"):
            await self.store.abandon_turn_attempt(
                self.session_id,
                attempt.turn_id,
                turn_event,
                "test",
                task=cancelled_task,
                task_event=AgentEvent.create(
                    2,
                    AgentEventKind.SESSION_TASK_CANCELLED,
                    {"task": cancelled_task.to_dict()},
                ),
            )
        with self.assertRaisesRegex(SessionError, "does not match"):
            await self.store.abandon_turn_attempt(
                self.session_id,
                attempt.turn_id,
                turn_event,
                "test",
                task=cancelled_task,
                task_event=AgentEvent.create(
                    1,
                    AgentEventKind.SESSION_TASK_CANCELLED,
                    {"task": task.to_dict()},
                ),
            )

    async def test_plan_abandon_service_rejects_missing_or_mismatched_ownership(self) -> None:
        async def install_and_reject(
            *,
            task: SessionTask | None,
            plan_requested: bool,
            task_id: str,
            expected: str,
        ) -> None:
            session_id = await self.store.create_session("/workspace", "provider", "model")
            attempt = TurnRecoveryAttempt.create(
                turn_id=f"turn-service-validation-{task_id}",
                session_id=session_id,
                input=TurnInput(
                    "execute the saved plan",
                    (ContentPart.from_text("execute the saved plan"),),
                    plan_execution_requested=plan_requested,
                    plan_execution_task_id=task_id,
                ),
                task_id=task_id,
                accepted_at=datetime.now(UTC),
            )
            await self.store.start_turn_attempt(attempt)
            if task is not None:
                await self.store.create_session_task(session_id, task)
            with self.assertRaisesRegex(ConfigurationError, expected):
                await TurnRecoveryService(self.store).abandon(session_id, attempt.turn_id)

        now = datetime.now(UTC)
        await install_and_reject(
            task=SessionTask(
                "task-service-unexpected",
                SessionTaskKind.PLAN_EXECUTION,
                SessionTaskStatus.RUNNING,
                now,
                plan_snapshot=self._plan(),
            ),
            plan_requested=False,
            task_id="task-service-unexpected",
            expected="unexpected task ownership",
        )
        await install_and_reject(
            task=None,
            plan_requested=True,
            task_id="task-service-missing",
            expected="does not exist",
        )
        await install_and_reject(
            task=SessionTask(
                "task-service-subagent",
                SessionTaskKind.SUBAGENT,
                SessionTaskStatus.RUNNING,
                now,
            ),
            plan_requested=True,
            task_id="task-service-subagent",
            expected="only a plan execution task",
        )
        await install_and_reject(
            task=SessionTask(
                "task-service-queued",
                SessionTaskKind.PLAN_EXECUTION,
                SessionTaskStatus.QUEUED,
                now,
                plan_snapshot=self._plan(),
            ),
            plan_requested=True,
            task_id="task-service-queued",
            expected="not running",
        )

    async def test_plan_recovery_is_safe_but_retry_is_not_available(self) -> None:
        attempt, _task = await self._start_linked_plan_attempt(
            turn_id="turn-plan-no-retry",
            task_id="task-plan-no-retry",
        )
        inspection = (await TurnRecoveryService(self.store).inspect(self.session_id))[0]
        self.assertEqual(inspection.attempt.turn_id, attempt.turn_id)
        self.assertEqual(inspection.status, TurnRecoveryStatus.SAFELY_RETRYABLE)
        self.assertFalse(inspection.attempt.retry_available)
        self.assertTrue(inspection.attempt.abandon_available)
        self.assertEqual(inspection.to_dict()["task_id"], "task-plan-no-retry")
        self.assertFalse(inspection.to_dict()["retry_available"])
        self.assertTrue(inspection.to_dict()["abandon_available"])
        with self.assertRaisesRegex(ConfigurationError, "retry is unavailable"):
            await TurnRecoveryService(self.store).require_safe_retry(
                self.session_id,
                attempt.turn_id,
            )

    async def test_plan_attempt_without_explicit_owner_is_indeterminate_and_fail_closed(
        self,
    ) -> None:
        attempt = TurnRecoveryAttempt.create(
            turn_id="turn-plan-missing-owner",
            session_id=self.session_id,
            input=TurnInput(
                "execute the saved plan",
                (ContentPart.from_text("execute the saved plan"),),
                plan_execution_requested=True,
            ),
            accepted_at=datetime.now(UTC),
        )
        await self.store.start_turn_attempt(attempt)

        inspection = (await TurnRecoveryService(self.store).inspect(self.session_id))[0]
        self.assertEqual(inspection.status, TurnRecoveryStatus.INDETERMINATE)
        self.assertEqual(inspection.attempt.status_reason, "plan_task_ownership_missing")
        self.assertFalse(inspection.attempt.retry_available)
        self.assertFalse(inspection.attempt.abandon_available)
        with self.assertRaises(ConfigurationError):
            await TurnRecoveryService(self.store).abandon(self.session_id, attempt.turn_id)

    async def test_request_before_output_is_safe_and_exact_input_survives(self) -> None:
        attempt = self._attempt()
        await self.store.start_turn_attempt(attempt)
        request_event = AgentEvent.create(
            1,
            AgentEventKind.MODEL_REQUEST_STARTED,
            {
                "turn_id": attempt.turn_id,
                "recovery_fact": "model_request_started",
                "request_id": "request-1",
                "step": 1,
                "provider": "provider",
                "model": "model",
            },
        )
        await self.store.append_turn_recovery_fact(
            self.session_id,
            attempt.turn_id,
            request_event,
            TurnRecoveryFact(
                TurnRecoveryFactKind.MODEL_REQUEST_STARTED,
                request_id="request-1",
                step=1,
                provider="provider",
                model="model",
            ),
        )

        inspection = (await TurnRecoveryService(self.store).inspect_open(self.session_id))[0]
        self.assertEqual(inspection.status, TurnRecoveryStatus.SAFELY_RETRYABLE)
        self.assertIsNotNone(inspection.attempt.input)
        assert inspection.attempt.input is not None
        self.assertEqual(inspection.attempt.input.prompt, "hello")
        self.assertEqual(inspection.attempt.input.content_parts[0].text, "hello")

    async def test_storage_recovery_fact_boundaries_fail_closed(self) -> None:
        attempt = self._attempt()
        with self.assertRaises(TypeError):
            await self.store.start_turn_attempt("not an attempt")  # type: ignore[arg-type]
        with self.assertRaises(SessionError):
            await self.store.start_turn_attempt(replace(attempt, session_id="missing"))
        await self.store.start_turn_attempt(attempt)
        with self.assertRaises(SessionError):
            await self.store.start_turn_attempt(self._attempt(turn_id="turn-2"))

        request_fact = TurnRecoveryFact(
            TurnRecoveryFactKind.MODEL_REQUEST_STARTED,
            request_id="request-1",
            step=1,
            provider="provider",
            model="model",
        )
        request_event = AgentEvent.create(
            1,
            AgentEventKind.MODEL_REQUEST_STARTED,
            request_fact.to_event_data(attempt.turn_id),
        )
        with self.assertRaises(TypeError):
            await self.store.append_turn_recovery_fact(
                self.session_id,
                attempt.turn_id,
                "not an event",  # type: ignore[arg-type]
                request_fact,
            )
        with self.assertRaises(TypeError):
            await self.store.append_turn_recovery_fact(
                self.session_id,
                attempt.turn_id,
                request_event,
                "not a fact",  # type: ignore[arg-type]
            )
        with self.assertRaises(SessionError):
            await self.store.append_turn_recovery_fact(
                self.session_id,
                attempt.turn_id,
                AgentEvent.create(1, AgentEventKind.TURN_COMPLETED),
                request_fact,
            )
        with self.assertRaises(SessionError):
            await self.store.append_turn_recovery_fact(
                self.session_id,
                attempt.turn_id,
                AgentEvent.create(
                    1,
                    AgentEventKind.MODEL_REQUEST_STARTED,
                    request_fact.to_event_data("different-turn"),
                ),
                request_fact,
            )
        with self.assertRaises(SessionError):
            await self.store.append_turn_recovery_fact(
                self.session_id,
                "missing-turn",
                AgentEvent.create(
                    1,
                    AgentEventKind.MODEL_REQUEST_STARTED,
                    request_fact.to_event_data("missing-turn"),
                ),
                request_fact,
            )
        await self.store.append_turn_recovery_fact(
            self.session_id,
            attempt.turn_id,
            request_event,
            request_fact,
        )
        with self.assertRaises(SessionError):
            await self.store.append_turn_recovery_fact(
                self.session_id,
                attempt.turn_id,
                request_event,
                request_fact,
            )
        with self.assertRaises(SessionError):
            await self.store.append_event(
                self.session_id,
                AgentEvent.create(1, AgentEventKind.MODEL_OUTPUT_STARTED),
            )
        completed = AgentEvent.create(
            2,
            AgentEventKind.TURN_COMPLETED,
            {"turn_id": attempt.turn_id},
        )
        await self.store.finalize_turn(
            self.session_id,
            completed,
            [Message(Role.USER, "hello")],
            None,
            attempt.turn_id,
        )
        with self.assertRaises(SessionError):
            await self.store.append_turn_recovery_fact(
                self.session_id,
                attempt.turn_id,
                request_event,
                request_fact,
            )
        with self.assertRaises(SessionError):
            await self.store.start_turn_attempt(attempt)
        with self.assertRaises(SessionError):
            await self.store.abandon_turn_attempt(
                self.session_id,
                "missing-turn",
                AgentEvent.create(
                    3,
                    AgentEventKind.TURN_ABANDONED,
                    {"turn_id": "missing-turn"},
                ),
                "reason",
            )

        unknown_session = "session-does-not-exist"
        with self.assertRaises(SessionError):
            await self.store.finalize_turn(
                unknown_session,
                AgentEvent.create(1, AgentEventKind.TURN_COMPLETED),
                [],
                None,
            )
        with self.assertRaises(SessionError):
            await self.store.finalize_turn_failure(
                unknown_session,
                None,
                AgentEvent.create(1, AgentEventKind.TURN_FAILED),
                [],
                resolution="failed",
            )

        corrupted_session = await self.store.create_session(
            "/workspace",
            "provider",
            "model",
        )
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "UPDATE sessions SET messages_json = ? WHERE id = ?",
                ("{", corrupted_session),
            )
        with self.assertRaises(SessionError):
            await self.store.finalize_turn(
                corrupted_session,
                AgentEvent.create(1, AgentEventKind.TURN_COMPLETED),
                [],
                None,
            )
        with self.assertRaises(SessionError):
            await self.store.finalize_turn_failure(
                corrupted_session,
                None,
                AgentEvent.create(1, AgentEventKind.TURN_FAILED),
                [],
                resolution="failed",
            )
        with self.assertRaises(SessionError):
            await self.store.update_session_title(corrupted_session, "recovery")
        with self.assertRaises(SessionError):
            await self.store.save_messages(corrupted_session, [])
        with self.assertRaises(SessionError):
            await self.store.save_session_items(corrupted_session, [])
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE schema_meta SET version = 99 WHERE singleton = 1")
        with self.assertRaises(SessionError):
            await SqliteSessionStore(self.database).initialize()
        with self.assertRaises(TypeError):
            await self.store.finalize_turn(
                self.session_id,
                "not an event",  # type: ignore[arg-type]
                [],
                None,
            )
        with self.assertRaises(SessionError):
            await self.store.finalize_turn(
                self.session_id,
                AgentEvent.create(0, AgentEventKind.TURN_COMPLETED),
                [],
                None,
            )
        with self.assertRaises(TypeError):
            await self.store.finalize_turn(
                self.session_id,
                AgentEvent.create(1, AgentEventKind.TURN_COMPLETED),
                [],
                object(),  # type: ignore[arg-type]
            )
        completion_record = SessionExecutionRecord(
            AgentExecutionOutcome(
                AgentExecutionStatus.COMPLETED,
                None,
                finalized=False,
                recoverable=False,
            ),
            2,
            datetime.now(UTC),
        )
        with self.assertRaises(SessionError):
            await self.store.finalize_turn(
                self.session_id,
                AgentEvent.create(1, AgentEventKind.TURN_COMPLETED),
                [],
                completion_record,
            )
        with self.assertRaises(TypeError):
            await self.store.finalize_turn_with_compaction(
                self.session_id,
                "not an event",  # type: ignore[arg-type]
                [],
                None,
                object(),  # type: ignore[arg-type]
            )
        with self.assertRaises(SessionError):
            await self.store.finalize_turn_with_compaction(
                self.session_id,
                AgentEvent.create(1, AgentEventKind.TURN_FAILED),
                [],
                None,
                object(),  # type: ignore[arg-type]
            )
        with self.assertRaises(SessionError):
            await self.store.finalize_turn_with_compaction(
                self.session_id,
                AgentEvent.create(0, AgentEventKind.TURN_COMPLETED),
                [],
                None,
                object(),  # type: ignore[arg-type]
            )
        with self.assertRaises(TypeError):
            await self.store.finalize_turn_with_compaction(
                self.session_id,
                AgentEvent.create(1, AgentEventKind.TURN_COMPLETED),
                [],
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
            )
        with self.assertRaises(TypeError):
            await self.store.finalize_turn_with_compaction(
                self.session_id,
                AgentEvent.create(1, AgentEventKind.TURN_COMPLETED),
                [],
                None,
                object(),  # type: ignore[arg-type]
            )
        with self.assertRaises(SessionError):
            await self.store.finalize_turn_with_compaction(
                self.session_id,
                AgentEvent.create(1, AgentEventKind.TURN_COMPLETED),
                [],
                completion_record,
                object(),  # type: ignore[arg-type]
            )
        with self.assertRaises(SessionError):
            await self.store.abandon_turn_attempt(
                self.session_id,
                attempt.turn_id,
                AgentEvent.create(
                    3,
                    AgentEventKind.TURN_ABANDONED,
                    {"turn_id": attempt.turn_id},
                ),
                "reason",
            )

        collision_session = await self.store.create_session(
            "/workspace",
            "provider",
            "model",
        )
        collision_attempt = TurnRecoveryAttempt.create(
            turn_id="turn-abandon-collision",
            session_id=collision_session,
            input=TurnInput("hello"),
            accepted_at=datetime.now(UTC),
        )
        await self.store.start_turn_attempt(collision_attempt)
        collision_request = AgentEvent.create(
            1,
            AgentEventKind.MODEL_REQUEST_STARTED,
            request_fact.to_event_data(collision_attempt.turn_id),
        )
        await self.store.append_turn_recovery_fact(
            collision_session,
            collision_attempt.turn_id,
            collision_request,
            request_fact,
        )
        with self.assertRaises(SessionError):
            await self.store.abandon_turn_attempt(
                collision_session,
                collision_attempt.turn_id,
                AgentEvent.create(
                    1,
                    AgentEventKind.TURN_ABANDONED,
                    {"turn_id": collision_attempt.turn_id},
                ),
                "reason",
            )

        failure_session = await self.store.create_session(
            "/workspace",
            "provider",
            "model",
        )
        failure_attempt = TurnRecoveryAttempt.create(
            turn_id="turn-failure-collision",
            session_id=failure_session,
            input=TurnInput("hello"),
            accepted_at=datetime.now(UTC),
        )
        await self.store.start_turn_attempt(failure_attempt)
        failure_request = AgentEvent.create(
            1,
            AgentEventKind.MODEL_REQUEST_STARTED,
            request_fact.to_event_data(failure_attempt.turn_id),
        )
        await self.store.append_turn_recovery_fact(
            failure_session,
            failure_attempt.turn_id,
            failure_request,
            request_fact,
        )
        with self.assertRaises(SessionError):
            await self.store.finalize_turn_failure(
                failure_session,
                failure_attempt.turn_id,
                AgentEvent.create(
                    1,
                    AgentEventKind.TURN_FAILED,
                    {"turn_id": failure_attempt.turn_id},
                ),
                [],
                resolution="failed",
            )

    async def test_recovery_attempt_loader_marks_corruption_fail_closed(self) -> None:
        attempt = self._attempt()
        await self.store.start_turn_attempt(attempt)

        def update(column: str, value: object) -> None:
            with closing(sqlite3.connect(self.database)) as connection, connection:
                connection.execute("PRAGMA ignore_check_constraints = ON")
                connection.execute(
                    f"UPDATE session_turn_attempts SET {column} = ? WHERE turn_id = ?",
                    (value, attempt.turn_id),
                )

        for column in (
            "input_reconstructable",
            "output_started",
            "side_effecting_tool_started",
            "fact_conflict",
        ):
            update(column, 2)
            with self.subTest(column=column), self.assertRaises(SessionError):
                await self.store.load_turn_attempts(self.session_id)
            update(column, 0 if column != "input_reconstructable" else 1)

        update("input_json", "{")
        corrupted = (await self.store.load_turn_attempts(self.session_id))[0]
        self.assertTrue(corrupted.fact_conflict)
        update("input_json", attempt.input.canonical_json())
        update("input_fingerprint", "f" * 64)
        mismatched = (await self.store.load_turn_attempts(self.session_id))[0]
        self.assertTrue(mismatched.fact_conflict)
        update("input_fingerprint", attempt.input_fingerprint)
        invalid_requirements = attempt.input.to_dict()
        invalid_requirements["verification_requirements"] = {
            "schema_version": 999,
            "requirements": [],
        }
        update("input_json", json.dumps(invalid_requirements))
        invalid_requirements_attempt = (await self.store.load_turn_attempts(self.session_id))[0]
        self.assertTrue(invalid_requirements_attempt.fact_conflict)
        update("input_json", "")
        non_reconstructable = (await self.store.load_turn_attempts(self.session_id))[0]
        self.assertFalse(non_reconstructable.input_reconstructable)
        update("input_json", attempt.input.canonical_json())
        update("request_started_count", "not-an-int")
        with self.assertRaises(SessionError):
            await self.store.load_turn_attempts(self.session_id)

    async def test_sqlite_terminal_projection_validates_task_and_identity_ownership(self) -> None:
        async def fresh_attempt(turn_id: str) -> tuple[str, TurnRecoveryAttempt]:
            session_id = await self.store.create_session("/workspace", "provider", "model")
            attempt = TurnRecoveryAttempt.create(
                turn_id=turn_id,
                session_id=session_id,
                input=TurnInput("hello"),
                accepted_at=datetime.now(UTC),
            )
            await self.store.start_turn_attempt(attempt)
            return session_id, attempt

        session_id, attempt = await fresh_attempt("turn-task")
        started_at = datetime.now(UTC)
        running_task = SessionTask(
            "task-1",
            SessionTaskKind.PLAN_EXECUTION,
            SessionTaskStatus.RUNNING,
            started_at,
        )
        await self.store.create_session_task(session_id, running_task)
        finished_task = running_task.finish(
            SessionTaskStatus.COMPLETED,
            finished_at=started_at + timedelta(seconds=1),
        )
        task_event = AgentEvent.create(
            1,
            AgentEventKind.SESSION_TASK_COMPLETED,
            {"task": finished_task.to_dict()},
        )
        completed_event = AgentEvent.create(
            2,
            AgentEventKind.TURN_COMPLETED,
            {"turn_id": attempt.turn_id},
        )
        await self.store.finalize_turn(
            session_id,
            completed_event,
            [Message(Role.USER, "hello")],
            None,
            attempt.turn_id,
            finished_task,
            task_event,
        )
        saved_task = await self.store.get_session_task(session_id, running_task.task_id)
        self.assertIsNotNone(saved_task)
        assert saved_task is not None
        self.assertIs(saved_task.status, SessionTaskStatus.COMPLETED)

        invalid_task_event = AgentEvent.create(
            1,
            AgentEventKind.SESSION_TASK_COMPLETED,
            {"task": finished_task.to_dict()},
        )
        for task, event in (
            (None, invalid_task_event),
            (finished_task, None),
        ):
            other_session, other_attempt = await fresh_attempt(
                f"turn-invalid-task-{len(task_event.data)}-{task is None}"
            )
            with self.subTest(task=task, event=event), self.assertRaises(SessionError):
                await self.store.finalize_turn(
                    other_session,
                    AgentEvent.create(
                        2,
                        AgentEventKind.TURN_COMPLETED,
                        {"turn_id": other_attempt.turn_id},
                    ),
                    [Message(Role.USER, "hello")],
                    None,
                    other_attempt.turn_id,
                    task,
                    event,
                )

        unknown_session, unknown_attempt = await fresh_attempt("turn-unknown-task")
        with self.assertRaises(SessionError):
            await self.store.finalize_turn(
                unknown_session,
                AgentEvent.create(
                    2,
                    AgentEventKind.TURN_COMPLETED,
                    {"turn_id": unknown_attempt.turn_id},
                ),
                [Message(Role.USER, "hello")],
                None,
                unknown_attempt.turn_id,
                finished_task,
                task_event,
            )

        queued_session, queued_attempt = await fresh_attempt("turn-queued-task")
        queued_task = SessionTask(
            "task-queued",
            SessionTaskKind.PLAN_EXECUTION,
            SessionTaskStatus.QUEUED,
            started_at,
        )
        await self.store.create_session_task(queued_session, queued_task)
        queued_finished = SessionTask(
            queued_task.task_id,
            queued_task.kind,
            SessionTaskStatus.COMPLETED,
            queued_task.started_at,
            started_at + timedelta(seconds=1),
        )
        with self.assertRaises(SessionError):
            await self.store.finalize_turn(
                queued_session,
                AgentEvent.create(
                    2,
                    AgentEventKind.TURN_COMPLETED,
                    {"turn_id": queued_attempt.turn_id},
                ),
                [Message(Role.USER, "hello")],
                None,
                queued_attempt.turn_id,
                queued_finished,
                AgentEvent.create(1, AgentEventKind.SESSION_TASK_COMPLETED),
            )
        with self.assertRaises(SessionError):
            await self.store.finalize_turn(
                queued_session,
                AgentEvent.create(
                    2,
                    AgentEventKind.TURN_COMPLETED,
                    {"turn_id": queued_attempt.turn_id},
                ),
                [Message(Role.USER, "hello")],
                None,
                queued_attempt.turn_id,
                queued_task,
                AgentEvent.create(1, AgentEventKind.SESSION_TASK_COMPLETED),
            )

        mismatch_session, mismatch_attempt = await fresh_attempt("turn-mismatch-task")
        mismatch_task = SessionTask(
            "task-mismatch",
            SessionTaskKind.PLAN_EXECUTION,
            SessionTaskStatus.RUNNING,
            started_at,
        )
        await self.store.create_session_task(mismatch_session, mismatch_task)
        mismatch_finished = SessionTask(
            mismatch_task.task_id,
            mismatch_task.kind,
            SessionTaskStatus.COMPLETED,
            started_at + timedelta(seconds=2),
            started_at + timedelta(seconds=3),
        )
        with self.assertRaises(SessionError):
            await self.store.finalize_turn(
                mismatch_session,
                AgentEvent.create(
                    2,
                    AgentEventKind.TURN_COMPLETED,
                    {"turn_id": mismatch_attempt.turn_id},
                ),
                [Message(Role.USER, "hello")],
                None,
                mismatch_attempt.turn_id,
                mismatch_finished,
                AgentEvent.create(1, AgentEventKind.SESSION_TASK_COMPLETED),
            )

        identity_session, identity_attempt = await fresh_attempt("turn-identity")
        with self.assertRaises(SessionError):
            await self.store.finalize_turn(
                identity_session,
                AgentEvent.create(
                    1,
                    AgentEventKind.TURN_COMPLETED,
                    {"turn_id": "different-turn"},
                ),
                [Message(Role.USER, "hello")],
                None,
                identity_attempt.turn_id,
            )
        with self.assertRaises(SessionError):
            await self.store.finalize_turn_failure(
                identity_session,
                identity_attempt.turn_id,
                AgentEvent.create(
                    1,
                    AgentEventKind.TURN_FAILED,
                    {"turn_id": "different-turn"},
                ),
                [Message(Role.USER, "hello")],
                resolution="failed",
            )

        with self.assertRaises(TypeError):
            await self.store.finalize_turn_failure(
                identity_session,
                identity_attempt.turn_id,
                "not an event",  # type: ignore[arg-type]
                [],
                resolution="failed",
            )
        with self.assertRaises(SessionError):
            await self.store.finalize_turn_failure(
                identity_session,
                identity_attempt.turn_id,
                AgentEvent.create(1, AgentEventKind.TURN_COMPLETED),
                [],
                resolution="failed",
            )
        with self.assertRaises(SessionError):
            await self.store.finalize_turn_failure(
                identity_session,
                identity_attempt.turn_id,
                AgentEvent.create(1, AgentEventKind.TURN_FAILED),
                [],
                resolution="unsupported",
            )

        abandon_event = AgentEvent.create(
            1,
            AgentEventKind.TURN_ABANDONED,
            {"turn_id": identity_attempt.turn_id},
        )
        with self.assertRaises(SessionError):
            await self.store.abandon_turn_attempt(
                identity_session,
                identity_attempt.turn_id,
                "not an event",  # type: ignore[arg-type]
                "reason",
            )
        with self.assertRaises(SessionError):
            await self.store.abandon_turn_attempt(
                identity_session,
                identity_attempt.turn_id,
                AgentEvent.create(1, AgentEventKind.TURN_COMPLETED),
                "reason",
            )
        with self.assertRaises(SessionError):
            await self.store.abandon_turn_attempt(
                identity_session,
                identity_attempt.turn_id,
                abandon_event,
                "",
            )
        with self.assertRaises(SessionError):
            await self.store.abandon_turn_attempt(
                identity_session,
                identity_attempt.turn_id,
                AgentEvent.create(1, AgentEventKind.TURN_ABANDONED, {"turn_id": "other"}),
                "reason",
            )

        order_session, order_attempt = await fresh_attempt("turn-task-order")
        order_task = SessionTask(
            "task-order",
            SessionTaskKind.PLAN_EXECUTION,
            SessionTaskStatus.RUNNING,
            started_at,
        )
        await self.store.create_session_task(order_session, order_task)
        order_finished = order_task.finish(
            SessionTaskStatus.COMPLETED,
            finished_at=started_at + timedelta(seconds=1),
        )
        with self.assertRaises(SessionError):
            await self.store.finalize_turn(
                order_session,
                AgentEvent.create(
                    2,
                    AgentEventKind.TURN_COMPLETED,
                    {"turn_id": order_attempt.turn_id},
                ),
                [Message(Role.USER, "hello")],
                None,
                order_attempt.turn_id,
                order_finished,
                AgentEvent.create(2, AgentEventKind.SESSION_TASK_COMPLETED),
            )

        unknown_resolution_session = await self.store.create_session(
            "/workspace",
            "provider",
            "model",
        )
        with self.assertRaises(SessionError):
            await self.store.finalize_turn(
                unknown_resolution_session,
                AgentEvent.create(
                    1,
                    AgentEventKind.TURN_COMPLETED,
                    {"turn_id": "turn-not-accepted"},
                ),
                [Message(Role.USER, "hello")],
                None,
                "turn-not-accepted",
            )
        with self.assertRaises(SessionError):
            await self.store.finalize_turn(
                session_id,
                AgentEvent.create(
                    3,
                    AgentEventKind.TURN_COMPLETED,
                    {"turn_id": attempt.turn_id},
                ),
                [Message(Role.USER, "hello")],
                None,
                attempt.turn_id,
            )

    async def test_acceptance_without_request_marker_is_safe_to_retry(self) -> None:
        attempt = self._attempt()
        await self.store.start_turn_attempt(attempt)

        inspection = (await TurnRecoveryService(self.store).inspect_open(self.session_id))[0]
        self.assertEqual(inspection.status, TurnRecoveryStatus.SAFELY_RETRYABLE)
        self.assertEqual(inspection.attempt.request_started_count, 0)
        self.assertEqual(inspection.attempt.last_stage.value, "accepted")

    async def test_turn_input_round_trips_media_and_rejects_malformed_values(self) -> None:
        original = TurnInput(
            "mixed input",
            (
                ContentPart.from_text("text"),
                ContentPart.from_image("https://example.test/image.png"),
                ContentPart.from_audio("audio-data", "audio/wav"),
                ContentPart.from_blob("blob:stable", "blob-data", "application/octet-stream"),
            ),
            TurnSource.USER,
            True,
            "task-1",
        )
        self.assertEqual(TurnInput.from_dict(original.to_dict()), original)

        snapshot = VerificationRequirementsSnapshot.create(
            (VerificationRequirement.create(criterion="run the relevant checks"),)
        )
        structured = TurnInput("structured input", verification_requirements=snapshot)
        serialized = structured.to_dict()
        self.assertEqual(serialized["verification_requirements"], snapshot.to_dict())
        self.assertEqual(TurnInput.from_dict(serialized), structured)
        legacy = TurnInput("legacy input")
        self.assertNotIn("verification_requirements", legacy.to_dict())
        self.assertEqual(TurnInput.from_dict(legacy.to_dict()), legacy)
        self.assertNotEqual(structured.fingerprint, legacy.fingerprint)
        with self.assertRaises(ValueError):
            TurnInput.from_dict(
                {
                    **legacy.to_dict(),
                    "verification_requirements": {
                        "schema_version": 999,
                        "requirements": [],
                    },
                }
            )

        malformed = (
            None,
            {"prompt": "x", "content_parts": {}, "source": TurnSource.USER.value},
            {"prompt": "x", "content_parts": [None], "source": TurnSource.USER.value},
            {"prompt": "x", "content_parts": [{}], "source": TurnSource.USER.value},
            {
                "prompt": "x",
                "content_parts": [{"type": "unsupported"}],
                "source": TurnSource.USER.value,
            },
            {
                "prompt": "x",
                "content_parts": [{"type": "text"}],
                "source": TurnSource.USER.value,
            },
            {
                "prompt": "x",
                "content_parts": [{"type": "image"}],
                "source": TurnSource.USER.value,
            },
            {
                "prompt": "x",
                "content_parts": [{"type": "audio", "data": "x"}],
                "source": TurnSource.USER.value,
            },
            {
                "prompt": "x",
                "content_parts": [
                    {"type": "blob", "data": "x", "mime_type": "application/octet-stream"}
                ],
                "source": TurnSource.USER.value,
            },
            {"prompt": "x", "content_parts": [], "source": "unsupported"},
            {"prompt": "x", "content_parts": [], "source": 1},
            {
                "prompt": "x",
                "content_parts": [],
                "source": TurnSource.USER.value,
                "plan_execution_requested": 1,
            },
            {
                "prompt": "x",
                "content_parts": [],
                "source": TurnSource.USER.value,
                "plan_execution_task_id": 1,
            },
        )
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(ValueError):
                TurnInput.from_dict(value)

    async def test_turn_input_and_recovery_domain_bounds_are_fail_closed(self) -> None:
        invalid_inputs = (
            (None, (), TurnSource.USER, False, None),
            ("x", ("not-a-part",), TurnSource.USER, False, None),
            ("x", (), "user", False, None),
            ("x", (), TurnSource.USER, 1, None),
            ("x", (), TurnSource.USER, True, ""),
        )
        for values in invalid_inputs:
            with self.subTest(values=values), self.assertRaises(ValueError):
                TurnInput(*values)

        background = TurnInput("", source=TurnSource.BACKGROUND_TASK_AUTO_WAKE)
        self.assertTrue(background.background)
        self.assertFalse(background.reconstructable)
        background_attempt = TurnRecoveryAttempt.create(
            turn_id="turn-background",
            session_id=self.session_id,
            input=background,
            accepted_at=datetime.now(UTC),
        )
        self.assertIsNone(background_attempt.input)
        self.assertFalse(background_attempt.input_reconstructable)
        oversized = TurnRecoveryAttempt.create(
            turn_id="turn-oversized",
            session_id=self.session_id,
            input=TurnInput("x" * MAX_TURN_INPUT_BYTES),
            accepted_at=datetime.now(UTC),
        )
        self.assertFalse(oversized.input_reconstructable)
        self.assertIsNone(oversized.input)

        invalid_facts = (
            {"kind": "request"},
            {"kind": TurnRecoveryFactKind.MODEL_REQUEST_STARTED, "request_id": ""},
            {"kind": TurnRecoveryFactKind.MODEL_REQUEST_STARTED, "step": 0},
            {"kind": TurnRecoveryFactKind.TOOL_STARTED, "side_effecting": 1},
        )
        for values in invalid_facts:
            with self.subTest(values=values), self.assertRaises(ValueError):
                TurnRecoveryFact(**values)

    async def test_recovery_status_reasons_cover_terminal_and_ambiguous_states(self) -> None:
        base = self._attempt()
        now = datetime.now(UTC)
        committed = replace(
            base,
            resolution=TurnRecoveryResolution.COMMITTED,
            resolution_at=now,
            last_stage=TurnRecoveryStage.TURN_COMPLETED,
        )
        abandoned = replace(
            base,
            resolution=TurnRecoveryResolution.ABANDONED,
            resolution_at=now,
            last_stage=TurnRecoveryStage.ABANDONED,
        )
        failed = replace(base, resolution=TurnRecoveryResolution.FAILED, resolution_at=now)
        self.assertEqual(committed.status, TurnRecoveryStatus.COMMITTED)
        self.assertEqual(committed.status_reason, "atomic_turn_commit")
        self.assertEqual(abandoned.status, TurnRecoveryStatus.ABANDONED)
        self.assertEqual(abandoned.status_reason, "explicitly_abandoned")
        self.assertEqual(failed.status, TurnRecoveryStatus.ABANDONED)
        self.assertEqual(failed.status_reason, "terminal_failure_recorded")
        self.assertEqual(
            replace(base, fact_conflict=True).status_reason,
            "contradictory_recovery_facts",
        )
        self.assertEqual(
            replace(base, input_reconstructable=False, input=None).status_reason,
            "exact_input_unavailable",
        )
        background_attempt = TurnRecoveryAttempt.create(
            turn_id="turn-background-status",
            session_id=self.session_id,
            input=TurnInput("", source=TurnSource.BACKGROUND_TASK_AUTO_WAKE),
            accepted_at=now,
        )
        self.assertEqual(background_attempt.status_reason, "background_wake_is_not_reconstructable")
        self.assertEqual(
            replace(base, output_started=True).status_reason,
            "model_output_started_before_commit",
        )
        self.assertEqual(
            replace(base, side_effecting_tool_started=True).status_reason,
            "side_effecting_tool_started_before_commit",
        )
        self.assertEqual(
            replace(base, tool_started_count=1).status_reason,
            "tool_started_before_commit",
        )
        self.assertEqual(base.safe_projection()["status"], TurnRecoveryStatus.SAFELY_RETRYABLE)

    async def test_recovery_attempt_domain_validation_is_fail_closed(self) -> None:
        base = self._attempt()
        now = datetime.now(UTC)
        invalid_builders = (
            lambda: replace(base, input_fingerprint="bad"),
            lambda: replace(base, source="user"),
            lambda: replace(base, input=TurnInput("different")),
            lambda: replace(base, input_reconstructable=1),
            lambda: replace(
                base,
                accepted_at=datetime(2026, 8, 1, tzinfo=UTC).replace(tzinfo=None),
            ),
            lambda: replace(
                base,
                resolution_at=datetime(2026, 8, 1, tzinfo=UTC).replace(tzinfo=None),
            ),
            lambda: replace(
                base,
                last_stage_at=datetime(2026, 8, 1, tzinfo=UTC).replace(tzinfo=None),
            ),
            lambda: replace(base, request_started_count=-1),
            lambda: replace(base, tool_started_count=-1),
            lambda: replace(base, step=0),
            lambda: replace(base, output_started=1),
            lambda: replace(base, side_effecting_tool_started=1),
            lambda: replace(base, last_stage="accepted"),
            lambda: replace(base, fact_conflict=1),
        )
        for build in invalid_builders:
            with self.subTest(build=build), self.assertRaises(ValueError):
                build()
        with self.assertRaises(TypeError):
            TurnRecoveryAttempt.create(
                turn_id="turn-invalid-input",
                session_id=self.session_id,
                input="not a turn input",  # type: ignore[arg-type]
                accepted_at=now,
            )

    async def test_recovery_service_validates_explicit_resolution_edges(self) -> None:
        attempt = self._attempt()
        await self.store.start_turn_attempt(attempt)
        inspection = (await TurnRecoveryService(self.store).inspect_open(self.session_id))[0]
        self.assertIsInstance(inspection, TurnRecoveryInspection)
        self.assertEqual(inspection.to_dict()["turn_id"], attempt.turn_id)

        service = TurnRecoveryService(self.store)
        for reason in ("", "\x00bad", "x" * 513):
            with self.subTest(reason=reason), self.assertRaises(ConfigurationError):
                await service.abandon(self.session_id, attempt.turn_id, reason=reason)
        with self.assertRaises(ConfigurationError):
            await service.abandon(self.session_id, "turn-missing")
        with self.assertRaises(ConfigurationError):
            await service.require_safe_retry(self.session_id, "turn-missing")

        abandoned = await service.abandon(self.session_id, attempt.turn_id)
        self.assertEqual(abandoned.status, TurnRecoveryStatus.ABANDONED)
        with self.assertRaises(ConfigurationError):
            await service.abandon(self.session_id, attempt.turn_id)

    async def test_recovery_service_fails_closed_if_exact_input_is_missing(self) -> None:
        class InconsistentStore:
            def __init__(self, attempt: TurnRecoveryAttempt) -> None:
                self.attempt = attempt

            async def load_open_turn_attempts(self, _session_id: str) -> list[TurnRecoveryAttempt]:
                return [replace(self.attempt, input=None, input_reconstructable=True)]

        service = TurnRecoveryService(InconsistentStore(self._attempt()))  # type: ignore[arg-type]
        with self.assertRaisesRegex(ConfigurationError, "exact original turn input"):
            await service.require_safe_retry(self.session_id, "turn-1")

    async def test_safe_retry_returns_the_exact_original_input(self) -> None:
        attempt = self._attempt(prompt="retry me")
        await self.store.start_turn_attempt(attempt)
        retry = await TurnRecoveryService(self.store).require_safe_retry(
            self.session_id,
            attempt.turn_id,
        )
        self.assertEqual(retry.input.prompt, "retry me")

    async def test_safe_retry_preserves_the_structured_requirement_snapshot(self) -> None:
        snapshot = VerificationRequirementsSnapshot.create(
            (VerificationRequirement.create(criterion="run the relevant checks"),)
        )
        input_value = TurnInput("retry structured input", verification_requirements=snapshot)
        attempt = TurnRecoveryAttempt.create(
            turn_id="turn-structured-retry",
            session_id=self.session_id,
            input=input_value,
            accepted_at=datetime.now(UTC),
        )
        await self.store.start_turn_attempt(attempt)

        retry = await TurnRecoveryService(self.store).require_safe_retry(
            self.session_id,
            attempt.turn_id,
        )

        self.assertEqual(retry.input.verification_requirements, snapshot)
        self.assertEqual(retry.input.fingerprint, input_value.fingerprint)

    async def test_recovery_service_rejects_a_disappearing_attempt(self) -> None:
        class DisappearingStore:
            def __init__(self, attempt: TurnRecoveryAttempt) -> None:
                self.attempt = attempt

            async def load_open_turn_attempts(
                self,
                _session_id: str,
            ) -> list[TurnRecoveryAttempt]:
                return [self.attempt]

            async def next_event_sequence(self, _session_id: str) -> int:
                return 1

            async def abandon_turn_attempt(
                self,
                _session_id: str,
                _turn_id: str,
                _event: AgentEvent,
                _reason: str,
            ) -> None:
                return None

            async def load_turn_attempts(
                self,
                _session_id: str,
            ) -> list[TurnRecoveryAttempt]:
                return []

        attempt = self._attempt()
        service = TurnRecoveryService(DisappearingStore(attempt))  # type: ignore[arg-type]
        with self.assertRaisesRegex(ConfigurationError, "disappeared"):
            await service.abandon(self.session_id, attempt.turn_id)

    async def test_first_output_is_indeterminate_and_cannot_be_retried(self) -> None:
        attempt = self._attempt()
        await self.store.start_turn_attempt(attempt)
        event = AgentEvent.create(
            1,
            AgentEventKind.MODEL_OUTPUT_STARTED,
            {
                "turn_id": attempt.turn_id,
                "recovery_fact": "model_output_started",
                "request_id": "request-1",
                "step": 1,
                "output_kind": "text",
            },
        )
        await self.store.append_turn_recovery_fact(
            self.session_id,
            attempt.turn_id,
            event,
            TurnRecoveryFact(
                TurnRecoveryFactKind.MODEL_OUTPUT_STARTED,
                request_id="request-1",
                step=1,
                output_kind="text",
            ),
        )
        service = TurnRecoveryService(self.store)
        inspection = (await service.inspect_open(self.session_id))[0]
        self.assertEqual(inspection.status, TurnRecoveryStatus.INDETERMINATE)
        with self.assertRaises(ConfigurationError):
            await service.require_safe_retry(self.session_id, attempt.turn_id)

    async def test_tool_started_is_indeterminate_before_tool_terminal_event(self) -> None:
        attempt = self._attempt()
        await self.store.start_turn_attempt(attempt)
        event = AgentEvent.create(
            1,
            AgentEventKind.TOOL_STARTED,
            {
                "turn_id": attempt.turn_id,
                "recovery_fact": "tool_started",
                "id": "call-1",
                "name": "apply_patch",
                "side_effecting": True,
            },
        )
        await self.store.append_turn_recovery_fact(
            self.session_id,
            attempt.turn_id,
            event,
            TurnRecoveryFact(
                TurnRecoveryFactKind.TOOL_STARTED,
                tool_id="call-1",
                tool_name="apply_patch",
                side_effecting=True,
            ),
        )

        inspection = (await TurnRecoveryService(self.store).inspect_open(self.session_id))[0]
        self.assertEqual(inspection.status, TurnRecoveryStatus.INDETERMINATE)
        self.assertEqual(
            inspection.attempt.status_reason, "side_effecting_tool_started_before_commit"
        )

    async def test_normal_failure_is_not_exposed_as_an_interrupted_turn(self) -> None:
        attempt = self._attempt()
        await self.store.start_turn_attempt(attempt)
        event = AgentEvent.create(
            1,
            AgentEventKind.TURN_FAILED,
            {"turn_id": attempt.turn_id, "cancelled": False},
        )
        await self.store.finalize_turn_failure(
            self.session_id,
            attempt.turn_id,
            event,
            [Message(Role.USER, "hello")],
            resolution=TurnRecoveryResolution.FAILED.value,
        )

        self.assertEqual(await TurnRecoveryService(self.store).inspect(self.session_id), ())
        stored = (await self.store.load_turn_attempts(self.session_id))[0]
        self.assertEqual(stored.resolution, TurnRecoveryResolution.FAILED)

    async def test_explicit_abandon_is_durable_and_closes_open_attempt(self) -> None:
        attempt = self._attempt()
        await self.store.start_turn_attempt(attempt)

        inspection = await TurnRecoveryService(self.store).abandon(
            self.session_id,
            attempt.turn_id,
            reason="user chose a fresh turn",
        )
        self.assertEqual(inspection.status, TurnRecoveryStatus.ABANDONED)
        self.assertEqual(await self.store.load_open_turn_attempts(self.session_id), [])
        self.assertEqual(
            (await self.store.load_turn_attempts(self.session_id))[0].resolution,
            TurnRecoveryResolution.ABANDONED,
        )

    async def test_atomic_completion_resolves_attempt_at_commit_point(self) -> None:
        attempt = self._attempt()
        await self.store.start_turn_attempt(attempt)
        event = AgentEvent.create(
            1,
            AgentEventKind.TURN_COMPLETED,
            {"turn_id": attempt.turn_id, "step": 1},
        )
        await self.store.finalize_turn(
            self.session_id,
            event,
            [Message(Role.USER, "hello"), Message(Role.ASSISTANT, "done")],
            None,
            attempt.turn_id,
        )
        self.assertEqual(await self.store.load_open_turn_attempts(self.session_id), [])
        inspection = (await TurnRecoveryService(self.store).inspect_history(self.session_id))[0]
        self.assertEqual(inspection.status, TurnRecoveryStatus.COMMITTED)
        self.assertEqual(
            [row["kind"] for row in await self.store.load_events(self.session_id)],
            [AgentEventKind.TURN_COMPLETED.value],
        )

    async def test_failure_terminalization_rolls_back_all_owned_projections(self) -> None:
        attempt = self._attempt()
        await self.store.start_turn_attempt(attempt)
        event = AgentEvent.create(
            1,
            AgentEventKind.TURN_FAILED,
            {"turn_id": attempt.turn_id, "cancelled": False},
        )
        with (
            patch(
                "neuro_code.infrastructure.persistence.sqlite_session_turns._upsert_search_document",
                side_effect=RuntimeError("injected search failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            await self.store.finalize_turn_failure(
                self.session_id,
                attempt.turn_id,
                event,
                [Message(Role.USER, "hello")],
                resolution="failed",
            )
        self.assertEqual(len(await self.store.load_open_turn_attempts(self.session_id)), 1)
        self.assertEqual(await self.store.load_events(self.session_id), [])
        self.assertEqual(await self.store.load_session_items(self.session_id), [])

    async def test_process_death_after_plan_acceptance_preserves_exact_task_owner(self) -> None:
        script = """
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from neuro_code.domain.execution import TurnInput, TurnRecoveryAttempt
from neuro_code.domain.plans import PlanStep, SessionPlan
from neuro_code.domain.session_tasks import SessionTask, SessionTaskKind, SessionTaskStatus
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore

async def main() -> None:
    store = SqliteSessionStore(Path(sys.argv[1]))
    await store.initialize()
    session_id = await store.create_session('/workspace', 'provider', 'model')
    plan = SessionPlan((PlanStep('execute the saved plan'),))
    now = datetime.now(UTC)
    task = SessionTask(
        'task-plan-process-death',
        SessionTaskKind.PLAN_EXECUTION,
        SessionTaskStatus.RUNNING,
        now,
        plan_snapshot=plan,
    )
    attempt = TurnRecoveryAttempt.create(
        turn_id='turn-plan-process-death',
        session_id=session_id,
        input=TurnInput(
            'execute the saved plan',
            plan_execution_requested=True,
            plan_execution_task_id=task.task_id,
        ),
        task_id=task.task_id,
        accepted_at=now,
    )
    await store.start_plan_turn_attempt(attempt, task=task)
    os._exit(0)

asyncio.run(main())
"""
        process_database = Path(self._temporary_directory.name) / "plan-process-death.db"
        self.assertEqual(await asyncio.to_thread(_run_process_death, script, process_database), 0)

        reopened = SqliteSessionStore(process_database)
        await reopened.initialize()
        with closing(sqlite3.connect(process_database)) as connection:
            session_id = str(connection.execute("SELECT id FROM sessions").fetchone()[0])
        inspection = (await TurnRecoveryService(reopened).inspect_open(session_id))[0]
        self.assertEqual(inspection.attempt.task_id, "task-plan-process-death")
        task = await reopened.get_session_task(session_id, "task-plan-process-death")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, SessionTaskStatus.RUNNING)

        abandoned = await TurnRecoveryService(reopened).abandon(
            session_id,
            "turn-plan-process-death",
        )
        self.assertEqual(abandoned.status, TurnRecoveryStatus.ABANDONED)
        cancelled = await reopened.get_session_task(session_id, "task-plan-process-death")
        self.assertIsNotNone(cancelled)
        assert cancelled is not None
        self.assertEqual(cancelled.status, SessionTaskStatus.CANCELLED)

    async def test_process_death_after_output_is_fail_closed(self) -> None:
        script = """
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.execution import TurnInput, TurnRecoveryAttempt, TurnRecoveryFact, TurnRecoveryFactKind
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore

async def main() -> None:
    store = SqliteSessionStore(Path(sys.argv[1]))
    await store.initialize()
    session_id = await store.create_session('/workspace', 'provider', 'model')
    attempt = TurnRecoveryAttempt.create(
        turn_id='turn-child',
        session_id=session_id,
        input=TurnInput('hello'),
        accepted_at=datetime.now(UTC),
    )
    await store.start_turn_attempt(attempt)
    event = AgentEvent.create(1, AgentEventKind.MODEL_OUTPUT_STARTED, {
        'turn_id': 'turn-child', 'recovery_fact': 'model_output_started',
        'request_id': 'request-child', 'step': 1, 'output_kind': 'text',
    })
    await store.append_turn_recovery_fact(
        session_id, 'turn-child', event,
        TurnRecoveryFact(
            TurnRecoveryFactKind.MODEL_OUTPUT_STARTED,
            request_id='request-child', step=1, output_kind='text',
        ),
    )
    os._exit(0)

asyncio.run(main())
"""
        process_database = Path(self._temporary_directory.name) / "process-death.db"
        self.assertEqual(await asyncio.to_thread(_run_process_death, script, process_database), 0)

        reopened = SqliteSessionStore(process_database)
        await reopened.initialize()
        with closing(sqlite3.connect(process_database)) as connection:
            rows = connection.execute("SELECT id FROM sessions").fetchall()
        self.assertEqual(len(rows), 1)
        session_id = str(rows[0][0])
        inspection = (await TurnRecoveryService(reopened).inspect_open(session_id))[0]
        self.assertEqual(inspection.status, TurnRecoveryStatus.INDETERMINATE)
        with self.assertRaises(ConfigurationError):
            await TurnRecoveryService(reopened).require_safe_retry(session_id, "turn-child")

    async def test_process_death_after_committed_turn_stays_committed(self) -> None:
        script = """
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.messages import Message, Role
from neuro_code.domain.execution import TurnInput, TurnRecoveryAttempt
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore

async def main() -> None:
    store = SqliteSessionStore(Path(sys.argv[1]))
    await store.initialize()
    session_id = await store.create_session('/workspace', 'provider', 'model')
    attempt = TurnRecoveryAttempt.create(
        turn_id='turn-committed',
        session_id=session_id,
        input=TurnInput('hello'),
        accepted_at=datetime.now(UTC),
    )
    await store.start_turn_attempt(attempt)
    await store.finalize_turn(
        session_id,
        AgentEvent.create(1, AgentEventKind.TURN_COMPLETED, {
            'turn_id': 'turn-committed', 'step': 1,
        }),
        [Message(Role.USER, 'hello'), Message(Role.ASSISTANT, 'done')],
        None,
        'turn-committed',
    )
    os._exit(0)

import asyncio
asyncio.run(main())
"""
        process_database = Path(self._temporary_directory.name) / "committed-process-death.db"
        self.assertEqual(await asyncio.to_thread(_run_process_death, script, process_database), 0)

        reopened = SqliteSessionStore(process_database)
        await reopened.initialize()
        with closing(sqlite3.connect(process_database)) as connection:
            session_id = str(connection.execute("SELECT id FROM sessions").fetchone()[0])
        self.assertEqual(await TurnRecoveryService(reopened).inspect_open(session_id), ())
        inspection = (await TurnRecoveryService(reopened).inspect_history(session_id))[0]
        self.assertEqual(inspection.status, TurnRecoveryStatus.COMMITTED)


if __name__ == "__main__":
    unittest.main()
