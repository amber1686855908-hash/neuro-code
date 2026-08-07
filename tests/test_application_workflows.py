from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from typing import cast

from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.application.workflows import (
    ExecutePlanRequest,
    PlanExecutionController,
    PlanExecutionService,
    PlanSchedulingController,
    PlanSchedulingService,
    QueuedPlanExecutionController,
    QueuedPlanExecutionService,
    RunSessionTaskRequest,
    RunSubagentRequest,
    SchedulePlanRequest,
    SubagentExecutionService,
    SubagentExecutor,
)
from neuro_code.bootstrap.composition import ApplicationComposition
from neuro_code.domain.session_tasks import SessionTask, SessionTaskKind, SessionTaskStatus
from neuro_code.shared.errors import ConfigurationError


class SubagentStoreFixture:
    def __init__(self) -> None:
        self.created: list[tuple[str, SessionTask]] = []
        self.updated: list[tuple[str, SessionTask]] = []

    async def create_session_task(self, session_id: str, task: SessionTask) -> None:
        self.created.append((session_id, task))

    async def update_session_task(self, session_id: str, task: SessionTask) -> None:
        self.updated.append((session_id, task))


class SubagentExecutorFixture:
    def __init__(self) -> None:
        self.requests: list[RunSubagentRequest] = []
        self.sinks: list[EventSink | None] = []
        self.result = AgentRunResult("child-session", "child result", (), (), (), 1)
        self.error: BaseException | None = None

    async def run(
        self,
        request: RunSubagentRequest,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult:
        self.requests.append(request)
        self.sinks.append(sink)
        if self.error is not None:
            raise self.error
        return self.result


class PlanExecutionControllerFixture:
    def __init__(self) -> None:
        self.requests: list[str | None] = []
        self.schedule_requests = 0
        self.queued_requests: list[str] = []
        self.sinks: list[EventSink | None] = []
        self.cancel = False
        self.result = AgentRunResult("session-1", "done", (), (), (), 1)
        self.scheduled_task = SessionTask(
            "task-scheduled",
            SessionTaskKind.PLAN_EXECUTION,
            SessionTaskStatus.QUEUED,
            datetime(2026, 8, 4, tzinfo=UTC),
        )

    async def execute_plan(
        self,
        *,
        sink: EventSink | None = None,
        task_id: str | None = None,
    ) -> AgentRunResult:
        self.requests.append(task_id)
        self.sinks.append(sink)
        if self.cancel:
            raise asyncio.CancelledError
        return self.result

    async def run_session_task(
        self,
        task_id: str,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult:
        self.queued_requests.append(task_id)
        self.sinks.append(sink)
        if self.cancel:
            raise asyncio.CancelledError
        return self.result

    async def schedule_plan(self) -> SessionTask:
        self.schedule_requests += 1
        if self.cancel:
            raise asyncio.CancelledError
        return self.scheduled_task


class PlanExecutionServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.controller = PlanExecutionControllerFixture()
        self.service = PlanExecutionService(cast(PlanExecutionController, self.controller))
        self.queued_service = QueuedPlanExecutionService(
            cast(QueuedPlanExecutionController, self.controller)
        )
        self.scheduling_service = PlanSchedulingService(
            cast(PlanSchedulingController, self.controller)
        )

    def test_composition_binds_the_non_owning_application_service(self) -> None:
        composition = object.__new__(ApplicationComposition)
        composition.store = cast(SessionStore, SubagentStoreFixture())

        service = composition.bind_plan_execution_controller(
            cast(PlanExecutionController, self.controller)
        )

        self.assertIsInstance(service, PlanExecutionService)

        queued_service = composition.bind_queued_plan_execution_controller(
            cast(QueuedPlanExecutionController, self.controller)
        )

        self.assertIsInstance(queued_service, QueuedPlanExecutionService)

        scheduling_service = composition.bind_plan_scheduling_controller(
            cast(PlanSchedulingController, self.controller)
        )

        self.assertIsInstance(scheduling_service, PlanSchedulingService)

        subagent_service = composition.bind_subagent_executor(
            lambda: cast(SubagentExecutor, self.controller)
        )
        self.assertIsInstance(subagent_service, SubagentExecutionService)

    async def test_execute_plan_forwards_typed_request(self) -> None:
        result = await self.service.execute_plan(ExecutePlanRequest("task-1"))

        self.assertIs(result, self.controller.result)
        self.assertEqual(self.controller.requests, ["task-1"])

    async def test_direct_execute_plan_keeps_task_id_absent(self) -> None:
        await self.service.execute_plan(ExecutePlanRequest())

        self.assertEqual(self.controller.requests, [None])

    async def test_event_sink_is_forwarded_without_becoming_service_state(self) -> None:
        async def sink(_event: object) -> None:
            return None

        await self.service.execute_plan(ExecutePlanRequest(), sink=sink)

        self.assertIs(self.controller.sinks[-1], sink)

    async def test_noncanonical_request_is_rejected_before_controller(self) -> None:
        with self.assertRaises(ValueError):
            await self.service.execute_plan(cast(ExecutePlanRequest, object()))
        self.assertEqual(self.controller.requests, [])

    def test_request_rejects_blank_task_id(self) -> None:
        with self.assertRaises(ValueError):
            ExecutePlanRequest(" ")

    async def test_cancellation_is_preserved(self) -> None:
        self.controller.cancel = True

        with self.assertRaises(asyncio.CancelledError):
            await self.service.execute_plan(ExecutePlanRequest())

        self.assertEqual(self.controller.requests, [None])

    async def test_schedule_plan_forwards_typed_request(self) -> None:
        result = await self.scheduling_service.schedule_plan(SchedulePlanRequest())

        self.assertIs(result, self.controller.scheduled_task)
        self.assertEqual(self.controller.schedule_requests, 1)

    async def test_schedule_plan_rejects_noncanonical_request(self) -> None:
        with self.assertRaises(ValueError):
            await self.scheduling_service.schedule_plan(cast(SchedulePlanRequest, object()))
        self.assertEqual(self.controller.schedule_requests, 0)

    async def test_schedule_plan_cancellation_is_preserved(self) -> None:
        self.controller.cancel = True

        with self.assertRaises(asyncio.CancelledError):
            await self.scheduling_service.schedule_plan(SchedulePlanRequest())

        self.assertEqual(self.controller.schedule_requests, 1)

    async def test_queued_task_request_forwards_identity_and_sink(self) -> None:
        async def sink(_event: object) -> None:
            return None

        result = await self.queued_service.run_session_task(
            RunSessionTaskRequest("task-queued"),
            sink=sink,
        )

        self.assertIs(result, self.controller.result)
        self.assertEqual(self.controller.queued_requests, ["task-queued"])
        self.assertIs(self.controller.sinks[-1], sink)

    async def test_noncanonical_queued_request_is_rejected_before_controller(self) -> None:
        with self.assertRaises(ValueError):
            await self.queued_service.run_session_task(cast(RunSessionTaskRequest, object()))
        self.assertEqual(self.controller.queued_requests, [])

    def test_queued_request_rejects_blank_task_id(self) -> None:
        with self.assertRaises(ValueError):
            RunSessionTaskRequest(" ")

    async def test_queued_task_cancellation_is_preserved(self) -> None:
        self.controller.cancel = True

        with self.assertRaises(asyncio.CancelledError):
            await self.queued_service.run_session_task(RunSessionTaskRequest("task-queued"))

        self.assertEqual(self.controller.queued_requests, ["task-queued"])


class SubagentExecutionServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = SubagentStoreFixture()
        self.executor = SubagentExecutorFixture()
        self.service = SubagentExecutionService(
            cast(SessionStore, self.store),
            lambda: cast(SubagentExecutor, self.executor),
        )

    def test_request_is_bounded_and_does_not_store_prompt_in_task(self) -> None:
        request = RunSubagentRequest("parent-session", "inspect the workspace", max_steps=4)

        self.assertEqual(request.max_steps, 4)
        self.assertEqual(request.prompt, "inspect the workspace")

    def test_request_rejects_unbounded_values(self) -> None:
        with self.assertRaises(ValueError):
            RunSubagentRequest("parent-session", " ")
        with self.assertRaises(ValueError):
            RunSubagentRequest("parent-session", "inspect", max_steps=13)
        with self.assertRaises(ValueError):
            RunSubagentRequest("parent-session", "x\x00")

    async def test_success_creates_and_finishes_one_subagent_task(self) -> None:
        async def sink(_event: object) -> None:
            return None

        result = await self.service.run_subagent(
            RunSubagentRequest("parent-session", "inspect the workspace"),
            sink=sink,
        )

        self.assertEqual(len(self.store.created), 1)
        session_id, created = self.store.created[0]
        self.assertEqual(session_id, "parent-session")
        self.assertIs(created.kind, SessionTaskKind.SUBAGENT)
        self.assertIs(created.status, SessionTaskStatus.RUNNING)
        self.assertIsNone(created.plan_snapshot)
        self.assertEqual(len(self.store.updated), 1)
        self.assertIs(self.store.updated[0][1].status, SessionTaskStatus.COMPLETED)
        self.assertIs(result.task, self.store.updated[0][1])
        self.assertIs(result.result, self.executor.result)
        self.assertIs(self.executor.sinks[0], sink)
        self.assertEqual(self.executor.requests[0].parent_session_id, "parent-session")

    async def test_executor_failure_finishes_task_as_failed_and_preserves_error(self) -> None:
        error = RuntimeError("child failed")
        self.executor.error = error

        with self.assertRaisesRegex(RuntimeError, "child failed"):
            await self.service.run_subagent(
                RunSubagentRequest("parent-session", "inspect the workspace")
            )

        self.assertEqual(len(self.store.updated), 1)
        self.assertIs(self.store.updated[0][1].status, SessionTaskStatus.FAILED)

    async def test_cancellation_finishes_task_as_cancelled_and_propagates(self) -> None:
        self.executor.error = asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await self.service.run_subagent(
                RunSubagentRequest("parent-session", "inspect the workspace")
            )

        self.assertEqual(len(self.store.updated), 1)
        self.assertIs(self.store.updated[0][1].status, SessionTaskStatus.CANCELLED)

    async def test_executor_factory_failure_does_not_create_a_running_task(self) -> None:
        service = SubagentExecutionService(
            cast(SessionStore, self.store),
            lambda: cast(SubagentExecutor, object()),
        )

        with self.assertRaisesRegex(ConfigurationError, "invalid executor"):
            await service.run_subagent(RunSubagentRequest("parent-session", "inspect"))

        self.assertEqual(self.store.created, [])
        self.assertEqual(self.store.updated, [])
