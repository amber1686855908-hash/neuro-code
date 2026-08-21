from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.application.sessions.binding import ConversationBinding
from neuro_code.application.workflows import (
    MAX_SUBAGENT_RESULT_BYTES,
    ExecutePlanRequest,
    IsolatedSubagentExecutionService,
    IsolatedSubagentRuntime,
    IsolatedSubagentRuntimeFactory,
    PlanExecutionController,
    PlanExecutionService,
    PlanSchedulingController,
    PlanSchedulingService,
    QueuedPlanExecutionController,
    QueuedPlanExecutionService,
    ReadOnlySubagentApplicationService,
    RunSessionTaskRequest,
    RunSubagentRequest,
    SchedulePlanRequest,
    SubagentExecutionService,
    SubagentExecutor,
    SubagentRunResult,
)
from neuro_code.application.workflows.subagent_capabilities import SubagentCapabilitySet
from neuro_code.bootstrap.composition import ApplicationComposition
from neuro_code.bootstrap.subagent import (
    READ_ONLY_SUBAGENT_TOOL_NAMES,
    CompositionReadOnlySubagentRuntimeFactory,
)
from neuro_code.configuration.app import AppConfig, ProviderProfile
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.session_tasks import (
    SessionTask,
    SessionTaskKind,
    SessionTaskStatus,
    SubagentLink,
)
from neuro_code.shared.errors import ConfigurationError, SubagentTimeoutError


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


class IsolatedSubagentStoreFixture(SubagentStoreFixture):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[SubagentLink] = []
        self.deleted: list[str] = []

    async def save_subagent_link(self, link: SubagentLink) -> None:
        self.links.append(link)

    async def delete_session(self, session_id: str) -> None:
        self.deleted.append(session_id)


class IsolatedRuntimeFixture:
    def __init__(self, child_session_id: str = "child-session") -> None:
        self.child_session_id = child_session_id
        self.capability_fingerprint = "fixture-capability"
        self.result = AgentRunResult(child_session_id, "child result", (), (), (), 1)
        self.error: BaseException | None = None
        self.closed = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.wait_forever = False

    async def run(self, prompt: str, *, sink: EventSink | None = None) -> AgentRunResult:
        del prompt, sink
        self.started.set()
        if self.error is not None:
            raise self.error
        if self.wait_forever:
            await self.release.wait()
        return self.result

    async def close(self) -> None:
        self.closed += 1


class IsolatedRuntimeFactoryFixture:
    def __init__(self, runtime: IsolatedRuntimeFixture) -> None:
        self.runtime = runtime
        self.requests: list[tuple[RunSubagentRequest, str, SubagentCapabilitySet]] = []

    def requested_capabilities(
        self,
        request: RunSubagentRequest,
        *,
        parent_capabilities: SubagentCapabilitySet,
    ) -> SubagentCapabilitySet:
        return _subagent_parent_capability(
            cwd=parent_capabilities.cwd,
            roots=parent_capabilities.workspace_roots,
            max_steps=min(request.max_steps, parent_capabilities.max_steps),
            tools=tuple(
                name
                for name in READ_ONLY_SUBAGENT_TOOL_NAMES
                if name in parent_capabilities.allowed_tool_names
            ),
        )

    async def create(
        self,
        request: RunSubagentRequest,
        *,
        parent_task_id: str,
        capabilities: SubagentCapabilitySet,
    ) -> IsolatedSubagentRuntime:
        self.requests.append((request, parent_task_id, capabilities))
        self.runtime.capability_fingerprint = capabilities.fingerprint
        return self.runtime


class CompositionSubagentStoreFixture:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str, str | None, object]] = []
        self.deleted: list[str] = []

    async def create_session(
        self,
        cwd: str,
        provider: str,
        model: str,
        context_affinity: str | None,
        sandbox_profile: object,
    ) -> str:
        self.created.append((cwd, provider, model, context_affinity, sandbox_profile))
        return "child-session"

    async def delete_session(self, session_id: str) -> None:
        self.deleted.append(session_id)


class CompositionSubagentRunnerFixture:
    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult:
        del prompt, sink
        return AgentRunResult("child-session", "child result", (), (), (), 1)


class CompositionSubagentScopeFixture:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class CompositionSubagentBindingFactoryFixture:
    def __init__(self, config: AppConfig, store: CompositionSubagentStoreFixture) -> None:
        self.config = config
        self.store = store
        self.calls: list[dict[str, object]] = []
        self.scope = CompositionSubagentScopeFixture()
        self.fail = False

    async def create_binding(self, **kwargs: object) -> ConversationBinding:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("child binding failed")
        capabilities = cast(SubagentCapabilitySet, kwargs["capabilities"])
        return ConversationBinding(
            CompositionSubagentRunnerFixture(),
            self.config.provider,
            self.scope,
            capabilities,
        )


def _subagent_test_config() -> AppConfig:
    profile = ProviderProfile(
        name="xai",
        protocol="openai-responses",
        dialect="xai",
        model="fixture-model",
        base_url="https://api.x.ai/v1",
        api_key_env="XAI_API_KEY",
        builtin_tools=("web_search",),
    )
    return AppConfig(
        cwd=Path("/workspace"),
        state_dir=Path("/state"),
        providers={"xai": profile},
        default_provider="xai",
        selected_provider="xai",
    )


def _subagent_parent_capability(
    *,
    cwd: Path = Path("/workspace"),
    roots: tuple[Path, ...] = (Path("/workspace"),),
    sandbox_profile: SandboxProfile = SandboxProfile.OFF,
    max_steps: int = 8,
    tools: tuple[str, ...] = READ_ONLY_SUBAGENT_TOOL_NAMES,
) -> SubagentCapabilitySet:
    return SubagentCapabilitySet.from_runtime(
        tool_names=tools,
        cwd=cwd,
        additional_workspace_roots=roots[1:],
        sandbox_profile=sandbox_profile,
        enable_background_tasks=False,
        max_steps=max_steps,
    )


def _subagent_global_policy() -> SubagentCapabilitySet:
    return _subagent_parent_capability(max_steps=96)


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

        with self.assertRaisesRegex(ConfigurationError, "test-only"):
            composition.bind_subagent_executor(
                lambda: cast(SubagentExecutor, self.controller),
            )
        subagent_service = composition.bind_subagent_executor(
            lambda: cast(SubagentExecutor, self.controller),
            _test_only=True,
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


class IsolatedSubagentExecutionServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = IsolatedSubagentStoreFixture()
        self.runtime = IsolatedRuntimeFixture()
        self.factory = IsolatedRuntimeFactoryFixture(self.runtime)
        self.parent_capabilities = _subagent_parent_capability()
        self.global_policy = _subagent_global_policy()
        self.service = IsolatedSubagentExecutionService(
            cast(SessionStore, self.store),
            cast(IsolatedSubagentRuntimeFactory, self.factory),
            global_policy=self.global_policy,
            requested_capability_factory=self.factory.requested_capabilities,
            timeout_seconds=0.05,
        )

    async def test_success_persists_child_link_before_completion(self) -> None:
        result = await self.service.run_subagent(
            RunSubagentRequest("parent-session", "inspect the repository", max_steps=3),
            parent_capabilities=self.parent_capabilities,
        )

        self.assertEqual(len(self.store.created), 1)
        self.assertEqual(len(self.store.links), 1)
        link = self.store.links[0]
        self.assertEqual(link.parent_session_id, "parent-session")
        self.assertEqual(link.child_session_id, "child-session")
        self.assertEqual(link.parent_task_id, result.task.task_id)
        self.assertIs(result.link, link)
        self.assertIs(result.task.status, SessionTaskStatus.COMPLETED)
        self.assertEqual(self.runtime.closed, 1)
        self.assertEqual(self.factory.requests[0][0].max_steps, 3)
        self.assertLessEqual(self.factory.requests[0][2], self.parent_capabilities)

    async def test_missing_parent_capabilities_fails_before_child_task_creation(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "parent subagent capability"):
            await self.service.run_subagent(
                RunSubagentRequest("parent-session", "inspect"),
                parent_capabilities=cast(SubagentCapabilitySet, None),
            )

        self.assertEqual(self.store.created, [])
        self.assertEqual(self.factory.requests, [])

    async def test_requested_capability_cannot_restore_parent_workspace(self) -> None:
        parent = _subagent_parent_capability(
            cwd=Path("/workspace/project/src"),
            roots=(Path("/workspace/project/src"),),
        )

        def request_factory(
            request: RunSubagentRequest,
            *,
            parent_capabilities: SubagentCapabilitySet,
        ) -> SubagentCapabilitySet:
            del request, parent_capabilities
            return _subagent_parent_capability(
                cwd=Path("/workspace"),
                roots=(Path("/workspace"),),
                tools=("read_file",),
            )

        service = IsolatedSubagentExecutionService(
            cast(SessionStore, self.store),
            cast(IsolatedSubagentRuntimeFactory, self.factory),
            global_policy=self.global_policy,
            requested_capability_factory=request_factory,
            timeout_seconds=0.05,
        )
        with self.assertRaisesRegex(ConfigurationError, "exceeds parent"):
            await service.run_subagent(
                RunSubagentRequest("parent-session", "inspect"),
                parent_capabilities=parent,
            )

        self.assertEqual(self.store.created, [])
        self.assertEqual(self.factory.requests, [])

    async def test_requested_capability_cannot_restore_tools_or_mcp(self) -> None:
        parent = _subagent_parent_capability(tools=("read_file",))

        def request_factory(
            request: RunSubagentRequest,
            *,
            parent_capabilities: SubagentCapabilitySet,
        ) -> SubagentCapabilitySet:
            del request
            return SubagentCapabilitySet.from_runtime(
                tool_names=("read_file", "apply_patch", "bash", "mcp_lookup"),
                cwd=parent_capabilities.cwd,
                sandbox_profile=parent_capabilities.sandbox_profile,
                enable_background_tasks=True,
                max_steps=parent_capabilities.max_steps,
                mcp_tool_names=("mcp_lookup",),
                mcp_server_names=("fixture",),
            )

        service = IsolatedSubagentExecutionService(
            cast(SessionStore, self.store),
            cast(IsolatedSubagentRuntimeFactory, self.factory),
            global_policy=self.global_policy,
            requested_capability_factory=request_factory,
            timeout_seconds=0.05,
        )
        with self.assertRaisesRegex(ConfigurationError, "exceeds parent"):
            await service.run_subagent(
                RunSubagentRequest("parent-session", "inspect"),
                parent_capabilities=parent,
            )

        self.assertEqual(self.store.created, [])
        self.assertEqual(self.factory.requests, [])

    async def test_requested_capability_cannot_downgrade_parent_sandbox(self) -> None:
        parent = _subagent_parent_capability(sandbox_profile=SandboxProfile.STRICT)

        def request_factory(
            request: RunSubagentRequest,
            *,
            parent_capabilities: SubagentCapabilitySet,
        ) -> SubagentCapabilitySet:
            del request, parent_capabilities
            return _subagent_parent_capability(sandbox_profile=SandboxProfile.OFF)

        service = IsolatedSubagentExecutionService(
            cast(SessionStore, self.store),
            cast(IsolatedSubagentRuntimeFactory, self.factory),
            global_policy=self.global_policy,
            requested_capability_factory=request_factory,
            timeout_seconds=0.05,
        )
        with self.assertRaisesRegex(ConfigurationError, "exceeds parent"):
            await service.run_subagent(
                RunSubagentRequest("parent-session", "inspect"),
                parent_capabilities=parent,
            )

        self.assertEqual(self.store.created, [])
        self.assertEqual(self.factory.requests, [])

    async def test_runtime_fingerprint_mismatch_fails_closed(self) -> None:
        class MismatchingFactory(IsolatedRuntimeFactoryFixture):
            async def create(
                self,
                request: RunSubagentRequest,
                *,
                parent_task_id: str,
                capabilities: SubagentCapabilitySet,
            ) -> IsolatedSubagentRuntime:
                del request, parent_task_id, capabilities
                return self.runtime

        runtime = IsolatedRuntimeFixture()
        factory = MismatchingFactory(runtime)
        service = IsolatedSubagentExecutionService(
            cast(SessionStore, self.store),
            cast(IsolatedSubagentRuntimeFactory, factory),
            global_policy=self.global_policy,
            requested_capability_factory=factory.requested_capabilities,
            timeout_seconds=0.05,
        )
        with self.assertRaisesRegex(ConfigurationError, "metadata is inconsistent"):
            await service.run_subagent(
                RunSubagentRequest("parent-session", "inspect"),
                parent_capabilities=self.parent_capabilities,
            )

        self.assertEqual(self.store.updated[0][1].status, SessionTaskStatus.FAILED)
        self.assertEqual(runtime.closed, 1)

    async def test_runtime_failure_is_failed_and_child_link_remains_discoverable(self) -> None:
        error = RuntimeError("child provider failed")
        self.runtime.error = error

        with self.assertRaisesRegex(RuntimeError, "child provider failed"):
            await self.service.run_subagent(
                RunSubagentRequest("parent-session", "inspect the repository"),
                parent_capabilities=self.parent_capabilities,
            )

        self.assertEqual(len(self.store.links), 1)
        self.assertEqual(len(self.store.updated), 1)
        self.assertIs(self.store.updated[0][1].status, SessionTaskStatus.FAILED)
        self.assertEqual(self.runtime.closed, 1)

    async def test_child_result_must_match_linked_child_session(self) -> None:
        self.runtime.result = AgentRunResult("other-child", "child result", (), (), (), 1)

        with self.assertRaisesRegex(ConfigurationError, "different child session"):
            await self.service.run_subagent(
                RunSubagentRequest("parent-session", "inspect"),
                parent_capabilities=self.parent_capabilities,
            )
        self.assertIs(self.store.updated[0][1].status, SessionTaskStatus.FAILED)
        self.assertEqual(self.runtime.closed, 1)

    async def test_link_setup_failure_discards_fresh_child_session(self) -> None:
        class FailingLinkStore(IsolatedSubagentStoreFixture):
            async def save_subagent_link(self, link: SubagentLink) -> None:
                del link
                raise RuntimeError("link persistence failed")

        store = FailingLinkStore()
        service = IsolatedSubagentExecutionService(
            cast(SessionStore, store),
            cast(IsolatedSubagentRuntimeFactory, self.factory),
            global_policy=self.global_policy,
            requested_capability_factory=self.factory.requested_capabilities,
            timeout_seconds=0.05,
        )

        with self.assertRaisesRegex(RuntimeError, "link persistence failed"):
            await service.run_subagent(
                RunSubagentRequest("parent-session", "inspect"),
                parent_capabilities=self.parent_capabilities,
            )
        self.assertEqual(store.deleted, ["child-session"])
        self.assertEqual(self.runtime.closed, 1)
        self.assertIs(store.updated[0][1].status, SessionTaskStatus.FAILED)

    async def test_timeout_is_bounded_and_marks_task_failed(self) -> None:
        self.runtime.wait_forever = True

        with self.assertRaises(SubagentTimeoutError):
            await self.service.run_subagent(
                RunSubagentRequest("parent-session", "inspect the repository"),
                parent_capabilities=self.parent_capabilities,
            )

        self.assertEqual(len(self.store.links), 1)
        self.assertIs(self.store.updated[0][1].status, SessionTaskStatus.FAILED)
        self.assertEqual(self.runtime.closed, 1)

    async def test_cancellation_is_preserved_and_runtime_is_closed(self) -> None:
        self.runtime.wait_forever = True
        running = asyncio.create_task(
            self.service.run_subagent(
                RunSubagentRequest("parent-session", "inspect the repository"),
                parent_capabilities=self.parent_capabilities,
            )
        )
        await self.runtime.started.wait()
        running.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(len(self.store.links), 1)
        self.assertIs(self.store.updated[0][1].status, SessionTaskStatus.CANCELLED)
        self.assertEqual(self.runtime.closed, 1)

    def test_timeout_must_be_positive_and_bounded(self) -> None:
        with self.assertRaises(ValueError):
            IsolatedSubagentExecutionService(
                cast(SessionStore, self.store),
                cast(IsolatedSubagentRuntimeFactory, self.factory),
                global_policy=self.global_policy,
                requested_capability_factory=self.factory.requested_capabilities,
                timeout_seconds=0,
            )

        self.assertEqual(self.store.created, [])
        self.assertEqual(self.store.updated, [])

    async def test_setup_cancellation_finishes_parent_task_as_cancelled(self) -> None:
        release = asyncio.Event()

        class BlockingFactory:
            runtime = IsolatedRuntimeFixture()

            def requested_capabilities(
                self,
                request: RunSubagentRequest,
                *,
                parent_capabilities: SubagentCapabilitySet,
            ) -> SubagentCapabilitySet:
                return _subagent_parent_capability(
                    cwd=parent_capabilities.cwd,
                    roots=parent_capabilities.workspace_roots,
                    max_steps=min(request.max_steps, parent_capabilities.max_steps),
                    tools=tuple(
                        name
                        for name in READ_ONLY_SUBAGENT_TOOL_NAMES
                        if name in parent_capabilities.allowed_tool_names
                    ),
                )

            async def create(
                self,
                request: RunSubagentRequest,
                *,
                parent_task_id: str,
                capabilities: SubagentCapabilitySet,
            ) -> IsolatedSubagentRuntime:
                del request, parent_task_id
                self.runtime.capability_fingerprint = capabilities.fingerprint
                await release.wait()
                return self.runtime

        factory = BlockingFactory()
        service = IsolatedSubagentExecutionService(
            cast(SessionStore, self.store),
            cast(IsolatedSubagentRuntimeFactory, factory),
            global_policy=self.global_policy,
            requested_capability_factory=factory.requested_capabilities,
            timeout_seconds=0.05,
        )
        running = asyncio.create_task(
            service.run_subagent(
                RunSubagentRequest("parent-session", "inspect"),
                parent_capabilities=self.parent_capabilities,
            )
        )
        await asyncio.sleep(0)
        running.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(len(self.store.updated), 1)
        self.assertIs(self.store.updated[0][1].status, SessionTaskStatus.CANCELLED)


class ReadOnlySubagentApplicationServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = IsolatedSubagentStoreFixture()
        self.runtime = IsolatedRuntimeFixture()
        self.factory = IsolatedRuntimeFactoryFixture(self.runtime)
        self.parent_capabilities = _subagent_parent_capability()
        controller = IsolatedSubagentExecutionService(
            cast(SessionStore, self.store),
            cast(IsolatedSubagentRuntimeFactory, self.factory),
            global_policy=_subagent_global_policy(),
            requested_capability_factory=self.factory.requested_capabilities,
            timeout_seconds=0.05,
        )
        self.controller = controller
        self.service = ReadOnlySubagentApplicationService(
            controller,
            redaction_values=("super-secret",),
            max_result_bytes=32,
        )

    def test_result_limit_cannot_exceed_global_bound(self) -> None:
        with self.assertRaises(ValueError):
            ReadOnlySubagentApplicationService(
                self.controller,
                max_result_bytes=MAX_SUBAGENT_RESULT_BYTES + 1,
            )

    async def test_short_response_is_not_marked_truncated(self) -> None:
        projection = await self.service.run_subagent(
            RunSubagentRequest("parent-session", "inspect the repository"),
            parent_capabilities=self.parent_capabilities,
        )

        self.assertEqual(projection.response, "child result")
        self.assertFalse(projection.truncated)

    async def test_returns_only_bounded_redacted_result_projection(self) -> None:
        self.runtime.result = AgentRunResult(
            "child-session",
            "token=super-secret and " + "结果" * 100,
            (),
            (),
            (),
            2,
        )

        projection = await self.service.run_subagent(
            RunSubagentRequest("parent-session", "inspect the repository"),
            parent_capabilities=self.parent_capabilities,
        )

        self.assertEqual(projection.parent_session_id, "parent-session")
        self.assertEqual(projection.child_session_id, "child-session")
        self.assertIs(projection.status, SessionTaskStatus.COMPLETED)
        self.assertEqual(projection.steps, 2)
        self.assertTrue(projection.truncated)
        self.assertNotIn("super-secret", projection.response)
        self.assertLessEqual(len(projection.response.encode("utf-8")), 32)
        self.assertFalse(hasattr(projection, "messages"))
        self.assertFalse(hasattr(projection, "items"))
        self.assertFalse(hasattr(projection, "events"))

    async def test_projection_requires_parent_child_link(self) -> None:
        task = SessionTask(
            "subagent-task",
            SessionTaskKind.SUBAGENT,
            SessionTaskStatus.COMPLETED,
            datetime(2026, 8, 7, tzinfo=UTC),
            datetime(2026, 8, 7, 0, 0, 1, tzinfo=UTC),
        )

        class ControllerWithoutLink:
            async def run_subagent(
                self,
                request: RunSubagentRequest,
                *,
                parent_capabilities: SubagentCapabilitySet,
                sink: EventSink | None = None,
            ) -> SubagentRunResult:
                del request, parent_capabilities, sink
                return SubagentRunResult(
                    task,
                    AgentRunResult("child-session", "result", (), (), (), 1),
                )

        service = ReadOnlySubagentApplicationService(ControllerWithoutLink())
        with self.assertRaisesRegex(ConfigurationError, "parent link"):
            await service.run_subagent(
                RunSubagentRequest("parent-session", "inspect"),
                parent_capabilities=self.parent_capabilities,
            )

    async def test_projection_does_not_convert_child_failure(self) -> None:
        self.runtime.error = RuntimeError("child provider failed")

        with self.assertRaisesRegex(RuntimeError, "child provider failed"):
            await self.service.run_subagent(
                RunSubagentRequest("parent-session", "inspect the repository"),
                parent_capabilities=self.parent_capabilities,
            )
        self.assertIs(self.store.updated[0][1].status, SessionTaskStatus.FAILED)


class CompositionReadOnlySubagentRuntimeFactoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.config = _subagent_test_config()
        self.store = CompositionSubagentStoreFixture()
        self.composition = CompositionSubagentBindingFactoryFixture(self.config, self.store)
        self.factory = CompositionReadOnlySubagentRuntimeFactory(
            cast(ApplicationComposition, self.composition)
        )
        self.parent_capabilities = _subagent_parent_capability()

    def test_composition_exposes_a_safe_result_projection_service(self) -> None:
        composition = object.__new__(ApplicationComposition)
        composition.config = self.config
        composition.store = cast(SessionStore, self.store)
        service = composition.create_read_only_subagent_application_service()

        self.assertIsInstance(service, ReadOnlySubagentApplicationService)

    async def test_factory_creates_fresh_read_only_binding_and_closes_it(self) -> None:
        request = RunSubagentRequest("parent-session", "inspect", max_steps=3)
        capabilities = self.factory.requested_capabilities(
            request,
            parent_capabilities=self.parent_capabilities,
        )
        runtime = await self.factory.create(
            request,
            parent_task_id="subagent-task",
            capabilities=capabilities,
        )

        self.assertEqual(self.store.created[0][1:3], ("xai", "fixture-model"))
        call = self.composition.calls[0]
        self.assertEqual(call["resume_id"], "child-session")
        binding_capabilities = cast(SubagentCapabilitySet, call["capabilities"])
        self.assertEqual(binding_capabilities.max_steps, 3)
        self.assertEqual(
            binding_capabilities.allowed_tool_names,
            frozenset(READ_ONLY_SUBAGENT_TOOL_NAMES),
        )
        self.assertEqual(
            READ_ONLY_SUBAGENT_TOOL_NAMES,
            (
                "read_file",
                "read_files",
                "list_dir",
                "list_tree",
                "glob",
                "grep",
                "grep_many",
                "skill",
            ),
        )
        self.assertFalse(capabilities.background_tasks)
        selected = cast(AppConfig, call["config"])
        self.assertEqual(selected.provider.builtin_tools, ())

        result = await runtime.run("inspect")
        self.assertEqual(result.session_id, "child-session")
        await runtime.close()
        await runtime.close()
        self.assertEqual(self.composition.scope.shutdown_calls, 1)
        with self.assertRaisesRegex(ConfigurationError, "runtime is closed"):
            await runtime.run("inspect")

    async def test_factory_request_intersects_a_restricted_parent_manifest(self) -> None:
        parent = _subagent_parent_capability(
            cwd=Path("/workspace/project/src"),
            roots=(Path("/workspace/project/src"),),
            tools=("read_file",),
            max_steps=4,
        )
        capabilities = self.factory.requested_capabilities(
            RunSubagentRequest("parent-session", "inspect", max_steps=8),
            parent_capabilities=parent,
        )

        self.assertEqual(capabilities.allowed_tool_names, frozenset({"read_file"}))
        self.assertEqual(capabilities.cwd, parent.cwd)
        self.assertEqual(capabilities.workspace_roots, parent.workspace_roots)
        self.assertEqual(capabilities.max_steps, parent.max_steps)
        self.assertLessEqual(capabilities, parent)

    async def test_factory_rejects_unsafe_capability_even_before_binding(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "unsafe capability"):
            await self.factory.create(
                RunSubagentRequest("parent-session", "inspect"),
                parent_task_id="subagent-task",
                capabilities=_subagent_parent_capability(
                    tools=("read_file", "apply_patch"),
                ),
            )

        self.assertEqual(self.store.created, [])

    async def test_binding_failure_removes_child_session(self) -> None:
        self.composition.fail = True

        with self.assertRaisesRegex(RuntimeError, "child binding failed"):
            request = RunSubagentRequest("parent-session", "inspect")
            await self.factory.create(
                request,
                parent_task_id="subagent-task",
                capabilities=self.factory.requested_capabilities(
                    request,
                    parent_capabilities=self.parent_capabilities,
                ),
            )
        self.assertEqual(self.store.deleted, ["child-session"])
