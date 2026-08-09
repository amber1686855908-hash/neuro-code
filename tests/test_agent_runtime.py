from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from neuro_code.application.memory.compaction import (
    CompactionContextUsage,
    ProviderContextWindow,
)
from neuro_code.application.memory.compaction_runtime import (
    ContextCompactionRuntimeBoundary,
    ContextCompactionRuntimeGate,
    ContextCompactionRuntimeRequest,
    ContextCompactionRuntimeResult,
    ContextCompactionSafePoint,
)
from neuro_code.application.memory.compaction_trigger import (
    ContextCompactionTriggerMode,
    ContextCompactionTriggerRequest,
)
from neuro_code.application.permissions.contracts import PermissionApproval, PermissionRequest
from neuro_code.application.permissions.policy import (
    PermissionEffect,
    PermissionManager,
    PermissionMode,
    PermissionRule,
)
from neuro_code.application.ports.background_tasks import BackgroundTaskManager
from neuro_code.application.ports.model import ModelProvider, ModelToolPolicy
from neuro_code.application.ports.tools import Tool, ToolContext
from neuro_code.application.ports.workspace_changes import (
    WorkspaceChangeCheckpoint,
    WorkspaceChangeReport,
    WorkspaceFileChange,
)
from neuro_code.application.runtime.agent import AgentRuntime
from neuro_code.application.runtime.finalization import AgentFinalizer
from neuro_code.application.runtime.supervision import (
    AgentExecutionSupervisor,
    ExecutionControlMode,
    SupervisionCheckpoint,
    SupervisionMode,
    SupervisionTraceRecord,
)
from neuro_code.application.sessions import (
    GetSessionTaskRequest,
    StartSessionRequest,
)
from neuro_code.application.sessions.lifecycle import SessionLifecycleService
from neuro_code.application.sessions.task_queries import SessionTaskQueryService
from neuro_code.domain.background_tasks import BackgroundTaskSnapshot, BackgroundTaskStatus
from neuro_code.domain.conversation.context import UPSTREAM_IMPORT_PROVIDER, ModelContext
from neuro_code.domain.conversation.events import (
    AgentEvent,
    AgentEventKind,
    ModelBackendToolCompleted,
    ModelBackendToolStarted,
    ModelCompleted,
    ModelEvent,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.messages import (
    ContentPart,
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    SessionItem,
    ToolCall,
)
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.execution import (
    AgentExecutionOutcome,
    AgentExecutionStatus,
    ExecutionBudget,
    ProgressKind,
    SessionExecutionRecord,
    SupervisorDecision,
    SupervisorDecisionKind,
    SupervisorReasonCode,
    TurnSource,
)
from neuro_code.domain.plans import PlanComment, PlanStep, PlanStepStatus, SessionPlan
from neuro_code.domain.session_tasks import SessionTask, SessionTaskKind, SessionTaskStatus
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.infrastructure.background_tasks import LocalBackgroundTaskManager
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.infrastructure.providers.failover import FailoverModelProvider, ProviderCandidate
from neuro_code.infrastructure.tools.background_tasks import TaskOutputTool
from neuro_code.infrastructure.tools.registry import ToolRegistry, default_tool_registry
from neuro_code.infrastructure.workspace.changes import FilesystemWorkspaceChangeObserver
from neuro_code.shared.errors import ConfigurationError, ProviderError
from tests.fakes import EmptyWorkspaceChangeObserver


class ScriptedProvider:
    provider_name = "scripted"
    model_name = "fixture-model"
    context_affinity = "profile-v1:scripted"

    def __init__(self, scripts: Sequence[Sequence[ModelEvent | BaseException]]) -> None:
        self._scripts = list(scripts)
        self.calls: list[ModelContext] = []
        self.tool_definitions: list[tuple[ToolDefinition, ...]] = []
        self.tool_policies: list[ModelToolPolicy] = []

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        self.calls.append(context)
        self.tool_definitions.append(tuple(tools))
        self.tool_policies.append(tool_policy)
        script = self._scripts.pop(0)
        for event in script:
            if isinstance(event, BaseException):
                raise event
            yield event


class FailingProvider:
    def __init__(self, name: str) -> None:
        self.provider_name = name
        self.model_name = f"{name}-model"
        self.context_affinity = f"profile-v1:{name}"
        self.tool_policies: list[ModelToolPolicy] = []

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del context, tools
        self.tool_policies.append(tool_policy)
        raise ProviderError(f"{self.provider_name} unavailable")
        if False:
            yield ModelCompleted("stop")


class RecordingCompactionRuntimeGate(ContextCompactionRuntimeGate):
    __slots__ = ("requests",)

    def __init__(self) -> None:
        self.requests: list[ContextCompactionRuntimeRequest] = []

    async def trigger(
        self,
        request: ContextCompactionRuntimeRequest,
    ) -> ContextCompactionRuntimeResult:
        self.requests.append(request)
        raise ProviderError("compaction gate fixture failure")


class BlockingProvider:
    provider_name = "blocking"
    model_name = "blocking-model"
    context_affinity = "profile-v1:blocking"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.tool_policies: list[ModelToolPolicy] = []

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del context, tools
        self.tool_policies.append(tool_policy)
        self.started.set()
        await asyncio.Event().wait()
        if False:
            yield ModelCompleted("stop")


class GateApprover:
    def __init__(self) -> None:
        self.requested = asyncio.Event()
        self.requests: list[PermissionRequest] = []
        self._responses: asyncio.Queue[PermissionApproval] = asyncio.Queue()

    async def request(self, request: PermissionRequest) -> PermissionApproval:
        self.requests.append(request)
        self.requested.set()
        return await self._responses.get()

    def resolve(self, approval: PermissionApproval) -> None:
        self._responses.put_nowait(approval)


class ImmediateApprover:
    def __init__(self, approval: PermissionApproval) -> None:
        self.approval = approval
        self.requests: list[PermissionRequest] = []

    async def request(self, request: PermissionRequest) -> PermissionApproval:
        self.requests.append(request)
        return self.approval


class BlockingTool:
    definition = ToolDefinition(
        name="blocking_tool",
        description="Wait until the fixture turn is cancelled.",
        input_schema={"type": "object", "additionalProperties": False},
    )
    side_effecting = True

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        del arguments, context
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


class NeverStartedTool:
    definition = ToolDefinition(
        name="never_started_tool",
        description="Record an unexpected fixture execution.",
        input_schema={"type": "object", "additionalProperties": False},
    )
    side_effecting = True

    def __init__(self) -> None:
        self.executed = False

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        del arguments, context
        self.executed = True
        return ToolResult("unexpected")


class SecretEchoTool:
    definition = ToolDefinition(
        name="secret_echo",
        description="Return a fixture secret.",
        input_schema={"type": "object", "additionalProperties": False},
    )
    side_effecting = False

    def __init__(self, secret: str) -> None:
        self._secret = secret

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        del arguments, context
        return ToolResult(f"tool printed {self._secret}")


class CollectionFixtureTool:
    def __init__(self, name: str, content: str) -> None:
        self.definition = ToolDefinition(
            name=name,
            description=f"Run the {name} fixture.",
            input_schema={"type": "object", "additionalProperties": False},
        )
        self.side_effecting = False
        self.calls: list[dict[str, object]] = []
        self._content = content

    async def execute(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        del context
        self.calls.append(dict(arguments))
        return ToolResult(self._content)


class IncrementingEvidenceFixtureTool(CollectionFixtureTool):
    async def execute(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        result = await super().execute(arguments, context)
        return ToolResult(f"{result.content} {len(self.calls)}")


class MetadataFixtureTool:
    def __init__(self, name: str, result: ToolResult, *, side_effecting: bool = False) -> None:
        self.definition = ToolDefinition(
            name=name,
            description=f"Return fixture metadata for {name}.",
            input_schema={"type": "object", "additionalProperties": False},
        )
        self.side_effecting = side_effecting
        self.calls: list[dict[str, object]] = []
        self._result = result

    async def execute(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        del context
        self.calls.append(dict(arguments))
        return self._result


class MinimalToolCollection:
    """A structural ToolCollection fixture with no registry-specific API.

    提供只满足结构接口的 ToolCollection 测试夹具,不包含注册表专用 API."""

    def __init__(self, tools: Sequence[Tool]) -> None:
        self._tools = {tool.definition.name: tool for tool in tools}

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self._tools.values())


class FixtureWorkspaceChangeCheckpoint(WorkspaceChangeCheckpoint):
    """Opaque checkpoint used to prove the runtime does not need snapshots.

    提供不透明检查点,用于证明运行时不依赖快照."""

    __slots__ = ("sequence",)

    def __init__(self, sequence: int) -> None:
        self.sequence = sequence


class RecordingWorkspaceChangeObserver:
    def __init__(
        self,
        report: WorkspaceChangeReport,
        *,
        capture_error_at: int | None = None,
        capture_error: OSError | RuntimeError | None = None,
        compare_error: BaseException | None = None,
        event_log: list[str] | None = None,
    ) -> None:
        self._report = report
        self._capture_error_at = capture_error_at
        self._capture_error = capture_error
        self._compare_error = compare_error
        self._event_log = event_log
        self.capture_roots: list[Path] = []
        self.comparisons: list[tuple[WorkspaceChangeCheckpoint, WorkspaceChangeCheckpoint]] = []
        self.explicit_redactions: list[tuple[str, ...]] = []

    def capture(self, root: Path, /) -> WorkspaceChangeCheckpoint:
        self.capture_roots.append(root)
        if self._event_log is not None:
            self._event_log.append("capture")
        if self._capture_error_at == len(self.capture_roots):
            assert self._capture_error is not None
            raise self._capture_error
        return FixtureWorkspaceChangeCheckpoint(len(self.capture_roots))

    def compare(
        self,
        before: WorkspaceChangeCheckpoint,
        after: WorkspaceChangeCheckpoint,
        *,
        explicit_redactions: tuple[str, ...],
    ) -> WorkspaceChangeReport:
        self.comparisons.append((before, after))
        self.explicit_redactions.append(explicit_redactions)
        if self._event_log is not None:
            self._event_log.append("compare")
        if self._compare_error is not None:
            raise self._compare_error
        return self._report


class OrderedSideEffectTool:
    definition = ToolDefinition(
        name="ordered_side_effect",
        description="Record workspace observation order.",
        input_schema={"type": "object", "additionalProperties": False},
    )
    side_effecting = True

    def __init__(self, event_log: list[str], result: ToolResult | None = None) -> None:
        self._event_log = event_log
        self._result = result or ToolResult("completed")

    async def execute(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        del arguments, context
        self._event_log.append("execute")
        return self._result


class OSErrorSideEffectTool(OrderedSideEffectTool):
    async def execute(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        del arguments, context
        self._event_log.append("execute")
        raise OSError("fixture tool failure")


class ReleaseBackgroundTaskTool:
    definition = ToolDefinition(
        name="release_background_task",
        description="Release the managed fixture task.",
        input_schema={"type": "object", "additionalProperties": False},
    )
    side_effecting = True

    def __init__(
        self,
        trigger: Path,
        manager: LocalBackgroundTaskManager,
        task_id: str,
    ) -> None:
        self._trigger = trigger
        self._manager = manager
        self._task_id = task_id

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        del arguments, context
        self._trigger.write_text("release", encoding="utf-8")
        snapshot = await self._manager.get(self._task_id, wait_seconds=2)
        assert snapshot is not None
        assert snapshot.status.terminal
        return ToolResult("fixture task released")


class FixtureCompletionManager:
    def __init__(self, snapshots: Sequence[BackgroundTaskSnapshot]) -> None:
        self._snapshots = tuple(snapshots)
        self.reported: set[str] = set()

    async def pending_completions(self) -> tuple[BackgroundTaskSnapshot, ...]:
        return tuple(
            snapshot for snapshot in self._snapshots if snapshot.task_id not in self.reported
        )

    async def mark_completions_reported(self, task_ids: tuple[str, ...]) -> None:
        self.reported.update(task_ids)

    def as_manager(self) -> BackgroundTaskManager:
        return cast(BackgroundTaskManager, self)


def completion_snapshot(task_id: str) -> BackgroundTaskSnapshot:
    timestamp = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    return BackgroundTaskSnapshot(
        task_id=task_id,
        command="private command",
        cwd="/private/workspace",
        status=BackgroundTaskStatus.COMPLETED,
        output="private output",
        total_output_bytes=14,
        truncated=False,
        exit_code=0,
        started_at=timestamp,
        finished_at=timestamp,
    )


def observation_budget(**overrides: object) -> ExecutionBudget:
    values: dict[str, object] = {
        "max_model_calls": 24,
        "max_tool_rounds": 24,
        "max_tool_calls": 96,
        "max_calls_per_tool": 24,
        "max_wall_seconds": None,
        "max_input_tokens": None,
        "max_output_tokens": None,
        "max_total_tokens": None,
    }
    values.update(overrides)
    return ExecutionBudget(**values)  # type: ignore[arg-type]


def compaction_runtime_request_fixture() -> ContextCompactionRuntimeRequest:
    context = ModelContext((Message(Role.USER, "compact this context"),))
    usage = CompactionContextUsage.from_provider_window(
        900,
        ProviderContextWindow(
            "scripted",
            "fixture-model",
            1_000,
            context_affinity="profile-v1:scripted",
        ),
        estimated=False,
    )
    return ContextCompactionRuntimeRequest(
        ContextCompactionTriggerRequest(
            context=context,
            usage=usage,
            mode=ContextCompactionTriggerMode.DISABLED,
        ),
        ContextCompactionRuntimeBoundary(ContextCompactionSafePoint.BEFORE_MODEL_REQUEST, 0),
    )


def observing_supervisor_factory(
    budget: ExecutionBudget,
    created: list[AgentExecutionSupervisor] | None = None,
) -> Callable[[], AgentExecutionSupervisor]:
    def factory() -> AgentExecutionSupervisor:
        supervisor = AgentExecutionSupervisor(budget, mode=SupervisionMode.OBSERVE)
        if created is not None:
            created.append(supervisor)
        return supervisor

    return factory


class DecisionInjectingSupervisor(AgentExecutionSupervisor):
    """Return one explicit decision while preserving normal observation counters.

    返回一个明确决策,同时保留正常观察计数."""

    def __init__(self, budget: ExecutionBudget, decision: SupervisorDecision) -> None:
        super().__init__(budget, mode=SupervisionMode.OBSERVE)
        self._decision = decision

    def authorize_model_request(self) -> SupervisorDecision:
        super().authorize_model_request()
        return self._decision


class AgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_supervisor_model_budget_tracks_configured_max_steps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = AgentRuntime(
                provider=ScriptedProvider(((ModelTextDelta("done"), ModelCompleted("stop")),)),
                tools=MinimalToolCollection(()),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                max_steps=60,
            )

            supervisor = runtime._supervisor_factory()
            self.assertEqual(supervisor._budget.max_model_calls, 60)

    async def test_controlled_mode_uses_all_configured_model_steps_before_finalizing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = IncrementingEvidenceFixtureTool("inspect", "new evidence")
            scripts = [
                (
                    ModelToolCall(
                        ToolCall(f"inspect-{index}", "inspect", {"path": f"file-{index}.py"})
                    ),
                    ModelCompleted("tool_calls"),
                )
                for index in range(24)
            ]
            scripts.append((ModelTextDelta("safe summary"), ModelCompleted("stop")))
            provider = ScriptedProvider(scripts)
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((tool,)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                max_steps=24,
                supervisor_factory=observing_supervisor_factory(
                    observation_budget(max_model_calls=24)
                ),
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
            )

            result = await runtime.run("inspect all evidence")

            self.assertEqual(result.steps, 24)
            self.assertEqual(len(tool.calls), 24)
            self.assertEqual(len(provider.calls), 25)
            self.assertEqual(provider.tool_policies[-1], ModelToolPolicy.DISABLED)
            assert result.outcome is not None
            self.assertIs(result.outcome.status, AgentExecutionStatus.BUDGET_LIMITED)
            self.assertIs(result.outcome.reason_code, SupervisorReasonCode.MODEL_STEP_LIMIT)

    async def test_update_plan_is_persisted_emitted_and_added_to_follow_up_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / ".state" / "sessions.db")
            await store.initialize()
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(
                            ToolCall(
                                "plan-1",
                                "update_plan",
                                {
                                    "explanation": "Complete the requested feature safely",
                                    "plan": [
                                        {
                                            "step": "Inspect the current behavior",
                                            "status": "completed",
                                        },
                                        {
                                            "step": "Implement the next vertical slice",
                                            "status": "in_progress",
                                        },
                                    ],
                                },
                            )
                        ),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("Plan saved."), ModelCompleted("stop")),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
                plan=SessionPlan((PlanStep("Review the initial plan"),)),
                plan_comments=(
                    PlanComment(
                        "plan-comment-revision",
                        1,
                        "Replace this draft with a more concrete plan.",
                        datetime(2026, 7, 29, 14, tzinfo=UTC),
                    ),
                ),
            )

            result = await runtime.run("Make a plan for the feature")

            plan = SessionPlan(
                (
                    PlanStep("Inspect the current behavior", PlanStepStatus.COMPLETED),
                    PlanStep("Implement the next vertical slice", PlanStepStatus.IN_PROGRESS),
                ),
                "Complete the requested feature safely",
            )
            self.assertEqual(result.plan, plan)
            assert result.session_id is not None
            self.assertEqual(await store.load_session_plan(result.session_id), plan)
            plan_event = next(
                event for event in result.events if event.kind is AgentEventKind.PLAN_UPDATED
            )
            self.assertEqual(plan_event.data, plan.to_dict())
            second_system = next(
                message
                for message in provider.calls[1].messages
                if isinstance(message, Message) and message.role is Role.SYSTEM
            )
            self.assertIn("Current structured plan:", second_system.content)
            self.assertIn("Implement the next vertical slice", second_system.content)
            self.assertNotIn("Replace this draft with a more concrete plan", second_system.content)

    async def test_explicit_plan_execution_is_recorded_before_the_user_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            plan = SessionPlan(
                (PlanStep("Implement the approved slice", PlanStepStatus.IN_PROGRESS),),
                "Complete the work after explicit confirmation",
            )
            provider = ScriptedProvider(((ModelTextDelta("executed"), ModelCompleted("stop")),))
            runtime = AgentRuntime(
                provider=provider,
                tools=ToolRegistry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
                plan=plan,
            )

            result = await runtime.run("Execute the approved plan", plan_execution_requested=True)

            requested_index = next(
                index
                for index, event in enumerate(result.events)
                if event.kind is AgentEventKind.PLAN_EXECUTION_REQUESTED
            )
            user_index = next(
                index
                for index, event in enumerate(result.events)
                if event.kind is AgentEventKind.USER_MESSAGE
            )
            self.assertLess(requested_index, user_index)
            self.assertEqual(result.events[requested_index].data, {"plan": plan.to_dict()})
            self.assertIsNotNone(result.session_id)
            assert result.session_id is not None
            session_tasks = await store.list_session_tasks(result.session_id)
            self.assertEqual(len(session_tasks), 1)
            self.assertIs(session_tasks[0].kind, SessionTaskKind.PLAN_EXECUTION)
            self.assertIs(session_tasks[0].status, SessionTaskStatus.COMPLETED)
            self.assertEqual(session_tasks[0].plan_snapshot, plan)
            persisted_kinds = [
                event["kind"] for event in await store.load_events(result.session_id)
            ]
            self.assertIn(AgentEventKind.PLAN_EXECUTION_REQUESTED.value, persisted_kinds)
            self.assertLess(
                persisted_kinds.index(AgentEventKind.SESSION_TASK_STARTED.value),
                persisted_kinds.index(AgentEventKind.PLAN_EXECUTION_REQUESTED.value),
            )
            self.assertLess(
                persisted_kinds.index(AgentEventKind.SESSION_TASK_COMPLETED.value),
                persisted_kinds.index(AgentEventKind.TURN_COMPLETED.value),
            )

            system = next(
                message for message in provider.calls[0].messages if message.role is Role.SYSTEM
            )
            self.assertIn("Implement the approved slice", system.content)

    async def test_queued_plan_execution_reads_task_through_application_session_seam(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(
                str(root),
                "runtime-fixture",
                "fixture-model",
            )
            plan = SessionPlan(
                (PlanStep("Run the queued plan", PlanStepStatus.IN_PROGRESS),),
                "Execute only the explicitly selected task.",
            )
            await store.save_session_plan(session_id, plan)
            task = SessionTask(
                "queued-plan-task",
                SessionTaskKind.PLAN_EXECUTION,
                SessionTaskStatus.QUEUED,
                datetime(2026, 8, 6, 12, tzinfo=UTC),
                plan_snapshot=plan,
            )
            await store.create_session_task(session_id, task)
            runtime = AgentRuntime(
                provider=ScriptedProvider(((ModelTextDelta("queued"), ModelCompleted("stop")),)),
                tools=ToolRegistry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
                plan=plan,
            )
            captured: list[GetSessionTaskRequest] = []
            original = SessionTaskQueryService.get_session_task

            async def capture(
                service: SessionTaskQueryService,
                request: GetSessionTaskRequest,
            ) -> SessionTask | None:
                captured.append(request)
                return await original(service, request)

            with patch.object(SessionTaskQueryService, "get_session_task", new=capture):
                result = await runtime.run(
                    "Execute the queued plan",
                    session_id=session_id,
                    plan_execution_requested=True,
                    plan_execution_task_id=task.task_id,
                )

            self.assertEqual(result.response, "queued")
            self.assertEqual(captured, [GetSessionTaskRequest(session_id, task.task_id)])
            persisted = await store.get_session_task(session_id, task.task_id)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertIs(persisted.status, SessionTaskStatus.COMPLETED)

    async def test_new_runtime_session_uses_application_start_session_seam(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            provider = ScriptedProvider(((ModelTextDelta("started"), ModelCompleted("stop")),))
            runtime = AgentRuntime(
                provider=provider,
                tools=ToolRegistry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )
            captured: list[StartSessionRequest] = []
            original = SessionLifecycleService.start_session

            async def capture(
                service: SessionLifecycleService,
                request: StartSessionRequest,
            ) -> object:
                captured.append(request)
                return await original(service, request)

            with patch.object(SessionLifecycleService, "start_session", new=capture):
                result = await runtime.run("Create a session")

            self.assertEqual(result.response, "started")
            self.assertEqual(
                captured,
                [
                    StartSessionRequest(
                        str(root),
                        provider.provider_name,
                        provider.model_name,
                        provider.context_affinity,
                    )
                ],
            )
            self.assertIsNotNone(result.session_id)

    async def test_failed_plan_execution_marks_its_durable_task_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            runtime = AgentRuntime(
                provider=FailingProvider("failing"),
                tools=ToolRegistry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
                plan=SessionPlan((PlanStep("Try the execution"),)),
            )

            with self.assertRaisesRegex(ProviderError, "failing unavailable"):
                await runtime.run("Execute the approved plan", plan_execution_requested=True)

            session_id = (await store.list_sessions())[0].id
            tasks = await store.list_session_tasks(session_id)
            self.assertEqual(len(tasks), 1)
            self.assertIs(tasks[0].status, SessionTaskStatus.FAILED)
            event_kinds = [event["kind"] for event in await store.load_events(session_id)]
            self.assertIn(AgentEventKind.SESSION_TASK_FAILED.value, event_kinds)

    async def test_cancelled_plan_execution_marks_its_durable_task_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            provider = BlockingProvider()
            runtime = AgentRuntime(
                provider=provider,
                tools=ToolRegistry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
                plan=SessionPlan((PlanStep("Try the execution"),)),
            )

            turn = asyncio.create_task(
                runtime.run("Execute the approved plan", plan_execution_requested=True)
            )
            try:
                await asyncio.wait_for(provider.started.wait(), timeout=5)
                turn.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await turn
            finally:
                if not turn.done():
                    turn.cancel()
                    await asyncio.gather(turn, return_exceptions=True)

            session_id = (await store.list_sessions())[0].id
            tasks = await store.list_session_tasks(session_id)
            self.assertEqual(len(tasks), 1)
            self.assertIs(tasks[0].status, SessionTaskStatus.CANCELLED)
            event_kinds = [event["kind"] for event in await store.load_events(session_id)]
            self.assertLess(
                event_kinds.index(AgentEventKind.SESSION_TASK_CANCELLED.value),
                event_kinds.index(AgentEventKind.TURN_FAILED.value),
            )

    async def test_plan_execution_without_a_saved_plan_fails_before_creating_a_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            runtime = AgentRuntime(
                provider=ScriptedProvider(()),
                tools=ToolRegistry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )

            with self.assertRaisesRegex(ConfigurationError, "has not been saved"):
                await runtime.run("Execute the plan", plan_execution_requested=True)
            self.assertEqual(await store.list_sessions(), [])

    async def test_structured_user_images_reach_provider_and_events_stay_safe(self) -> None:
        provider = ScriptedProvider(((ModelTextDelta("done"), ModelCompleted("stop")),))
        runtime = AgentRuntime(
            provider=provider,
            tools=ToolRegistry(),
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=PermissionManager(),
            tool_context=ToolContext(Path("/workspace")),
        )
        image_url = "data:image/png;base64,cHJpdmF0ZS1pbWFnZQ=="

        result = await runtime.run(
            "inspect",
            content_parts=(
                ContentPart.from_text("inspect"),
                ContentPart.from_image(image_url),
            ),
        )

        user = next(message for message in provider.calls[0].messages if message.role is Role.USER)
        self.assertEqual(user.content, "inspect")
        self.assertEqual(user.content_parts[1].url, image_url)
        persisted = next(message for message in result.messages if message.role is Role.USER)
        self.assertEqual(persisted.content_parts[1].url, image_url)
        user_event = next(
            event for event in result.events if event.kind is AgentEventKind.USER_MESSAGE
        )
        self.assertIn("image content preserved", str(user_event.data["content"]))
        self.assertNotIn("cHJpdmF0ZS1pbWFnZQ==", str(user_event.data))

    async def test_structural_tool_collection_preserves_order_and_known_tool_dispatch(self) -> None:
        first = CollectionFixtureTool("first", "first completed")
        second = CollectionFixtureTool("second", "second completed")
        provider = ScriptedProvider(
            (
                (
                    ModelToolCall(ToolCall("first-call", "first", {})),
                    ModelToolCall(ToolCall("second-call", "second", {})),
                    ModelCompleted("tool_calls"),
                ),
                (ModelTextDelta("done"), ModelCompleted("stop")),
            )
        )
        observer = RecordingWorkspaceChangeObserver(
            WorkspaceChangeReport(files=(), omitted_files=0, scan_limited=False)
        )
        runtime = AgentRuntime(
            provider=provider,
            tools=MinimalToolCollection((first, second)),
            workspace_change_observer=observer,
            permissions=PermissionManager(mode=PermissionMode.BYPASS),
            tool_context=ToolContext(Path("/workspace")),
        )

        result = await runtime.run("Run both fixture tools")

        self.assertEqual(
            [definition.name for definition in provider.tool_definitions[0]],
            ["first", "second"],
        )
        self.assertEqual(first.calls, [{}])
        self.assertEqual(second.calls, [{}])
        self.assertEqual(observer.capture_roots, [])
        completed = [
            event for event in result.events if event.kind is AgentEventKind.TOOL_COMPLETED
        ]
        self.assertEqual([event.data["name"] for event in completed], ["first", "second"])

    async def test_structural_tool_collection_preserves_unknown_tool_failure(self) -> None:
        provider = ScriptedProvider(
            (
                (
                    ModelToolCall(ToolCall("missing-call", "missing", {})),
                    ModelCompleted("tool_calls"),
                ),
                (ModelTextDelta("done"), ModelCompleted("stop")),
            )
        )
        observer = RecordingWorkspaceChangeObserver(
            WorkspaceChangeReport(files=(), omitted_files=0, scan_limited=False)
        )
        runtime = AgentRuntime(
            provider=provider,
            tools=MinimalToolCollection(()),
            workspace_change_observer=observer,
            permissions=PermissionManager(mode=PermissionMode.BYPASS),
            tool_context=ToolContext(Path("/workspace")),
        )

        result = await runtime.run("Run an unknown fixture tool")

        failed = next(event for event in result.events if event.kind is AgentEventKind.TOOL_FAILED)
        self.assertEqual(failed.data["name"], "missing")
        self.assertEqual(failed.data["content"], "unknown tool: missing")
        self.assertEqual(observer.capture_roots, [])

    async def test_workspace_observer_preserves_side_effect_timing_and_payload(self) -> None:
        event_log: list[str] = []
        report = WorkspaceChangeReport(
            files=(
                WorkspaceFileChange(
                    path="note.txt",
                    status="modified",
                    additions=1,
                    deletions=1,
                    diff="--- a/note.txt\n+++ b/note.txt\n-old\n+new",
                    diff_truncated=False,
                    hidden_reason="redacted",
                ),
            ),
            omitted_files=2,
            scan_limited=False,
        )
        observer = RecordingWorkspaceChangeObserver(report, event_log=event_log)
        tool = OrderedSideEffectTool(event_log)
        provider = ScriptedProvider(
            (
                (
                    ModelToolCall(ToolCall("change", tool.definition.name, {})),
                    ModelCompleted("tool_calls"),
                ),
                (ModelTextDelta("done"), ModelCompleted("stop")),
            )
        )
        secret_name = "NEURO_CODE_WORKSPACE_CHANGE_TEST_SECRET"
        secret_value = "workspace-observer-secret"
        previous_secret = os.environ.get(secret_name)
        os.environ[secret_name] = secret_value
        try:
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((tool,)),
                workspace_change_observer=observer,
                permissions=PermissionManager(mode=PermissionMode.BYPASS),
                tool_context=ToolContext(
                    Path("/workspace"),
                    protected_environment_variables=frozenset({secret_name}),
                ),
            )

            def record_started(event: object) -> None:
                if getattr(event, "kind", None) is AgentEventKind.TOOL_STARTED:
                    event_log.append("started")

            result = await runtime.run("Change the fixture file", sink=record_started)
        finally:
            if previous_secret is None:
                del os.environ[secret_name]
            else:
                os.environ[secret_name] = previous_secret

        self.assertEqual(event_log, ["started", "capture", "execute", "capture", "compare"])
        self.assertEqual(len(observer.capture_roots), 2)
        self.assertEqual(observer.explicit_redactions, [(secret_value,)])
        completed = next(
            event for event in result.events if event.kind is AgentEventKind.TOOL_COMPLETED
        )
        payload = completed.data["workspace_changes"]
        self.assertEqual(payload, report.to_event_payload())
        assert isinstance(payload, dict)
        self.assertEqual(list(payload), ["files", "omitted_files", "scan_limited"])
        file_payload = payload["files"][0]
        self.assertEqual(
            list(file_payload),
            [
                "path",
                "status",
                "additions",
                "deletions",
                "diff",
                "diff_truncated",
                "hidden_reason",
            ],
        )

    async def test_workspace_observer_report_survives_a_converted_tool_failure(self) -> None:
        event_log: list[str] = []
        report = WorkspaceChangeReport(
            files=(
                WorkspaceFileChange(
                    path="failed.txt",
                    status="created",
                    additions=1,
                    deletions=0,
                    diff="+++ b/failed.txt\n+partial",
                    diff_truncated=False,
                ),
            ),
            omitted_files=0,
            scan_limited=False,
        )
        observer = RecordingWorkspaceChangeObserver(report, event_log=event_log)
        tool = OSErrorSideEffectTool(event_log)
        provider = ScriptedProvider(
            (
                (
                    ModelToolCall(ToolCall("change", tool.definition.name, {})),
                    ModelCompleted("tool_calls"),
                ),
                (ModelTextDelta("recovered"), ModelCompleted("stop")),
            )
        )
        runtime = AgentRuntime(
            provider=provider,
            tools=MinimalToolCollection((tool,)),
            workspace_change_observer=observer,
            permissions=PermissionManager(mode=PermissionMode.BYPASS),
            tool_context=ToolContext(Path("/workspace")),
        )

        result = await runtime.run("Run the failing fixture")

        self.assertEqual(event_log, ["capture", "execute", "capture", "compare"])
        failed = next(event for event in result.events if event.kind is AgentEventKind.TOOL_FAILED)
        self.assertIn("OSError: fixture tool failure", failed.data["content"])
        self.assertEqual(failed.data["workspace_changes"], report.to_event_payload())

    async def test_workspace_capture_failures_do_not_block_the_tool_or_emit_a_report(self) -> None:
        for capture_error_at, error_type in (
            (1, OSError),
            (1, RuntimeError),
            (2, OSError),
            (2, RuntimeError),
        ):
            with self.subTest(capture_error_at=capture_error_at, error_type=error_type):
                event_log: list[str] = []
                observer = RecordingWorkspaceChangeObserver(
                    WorkspaceChangeReport(files=(), omitted_files=0, scan_limited=False),
                    capture_error_at=capture_error_at,
                    capture_error=error_type("fixture capture failure"),
                    event_log=event_log,
                )
                tool = OrderedSideEffectTool(event_log)
                provider = ScriptedProvider(
                    (
                        (
                            ModelToolCall(ToolCall("change", tool.definition.name, {})),
                            ModelCompleted("tool_calls"),
                        ),
                        (ModelTextDelta("done"), ModelCompleted("stop")),
                    )
                )
                runtime = AgentRuntime(
                    provider=provider,
                    tools=MinimalToolCollection((tool,)),
                    workspace_change_observer=observer,
                    permissions=PermissionManager(mode=PermissionMode.BYPASS),
                    tool_context=ToolContext(Path("/workspace")),
                )

                result = await runtime.run("Run the fixture")

                completed = next(
                    event for event in result.events if event.kind is AgentEventKind.TOOL_COMPLETED
                )
                self.assertNotIn("workspace_changes", completed.data)
                self.assertEqual(len(observer.capture_roots), capture_error_at)
                self.assertEqual(observer.comparisons, [])
                self.assertEqual(event_log.count("execute"), 1)

    async def test_workspace_compare_failure_propagates_before_the_terminal_tool_event(
        self,
    ) -> None:
        event_log: list[str] = []
        observer = RecordingWorkspaceChangeObserver(
            WorkspaceChangeReport(files=(), omitted_files=0, scan_limited=False),
            compare_error=ValueError("fixture compare failure"),
            event_log=event_log,
        )
        tool = OrderedSideEffectTool(event_log)
        provider = ScriptedProvider(
            (
                (
                    ModelToolCall(ToolCall("change", tool.definition.name, {})),
                    ModelCompleted("tool_calls"),
                ),
            )
        )
        observed: list[AgentEventKind] = []
        runtime = AgentRuntime(
            provider=provider,
            tools=MinimalToolCollection((tool,)),
            workspace_change_observer=observer,
            permissions=PermissionManager(mode=PermissionMode.BYPASS),
            tool_context=ToolContext(Path("/workspace")),
        )

        with self.assertRaisesRegex(ValueError, "fixture compare failure"):
            await runtime.run("Run the fixture", sink=lambda event: observed.append(event.kind))

        self.assertEqual(event_log, ["capture", "execute", "capture", "compare"])
        self.assertNotIn(AgentEventKind.TOOL_COMPLETED, observed)
        self.assertIn(AgentEventKind.TURN_FAILED, observed)

    async def test_explicit_provider_credentials_are_redacted_at_tool_boundary(self) -> None:
        secret = "credential-without-a-recognizable-shape"
        provider = ScriptedProvider(
            (
                (
                    ModelToolCall(ToolCall("secret", "secret_echo", {})),
                    ModelCompleted("tool_calls"),
                ),
                (ModelTextDelta("done"), ModelCompleted("stop")),
            )
        )
        tools = ToolRegistry()
        tools.register(SecretEchoTool(secret))
        runtime = AgentRuntime(
            provider=provider,
            tools=tools,
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=PermissionManager(),
            tool_context=ToolContext(Path("/workspace"), redaction_values=(secret,)),
        )

        result = await runtime.run("Run the fixture tool")

        serialized = repr(result)
        self.assertNotIn(secret, serialized)
        self.assertIn("[REDACTED]", serialized)
        self.assertNotIn(secret, repr(provider.calls))

    async def test_reasoning_policy_is_request_scoped_and_not_persisted(self) -> None:
        provider = ScriptedProvider(((ModelTextDelta("done"), ModelCompleted("stop")),))
        runtime = AgentRuntime(
            provider=provider,
            tools=ToolRegistry(),
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=PermissionManager(),
            tool_context=ToolContext(Path("/workspace")),
            reasoning_effort=ReasoningEffort.XHIGH,
        )

        result = await runtime.run("Solve a difficult problem")

        context = provider.calls[0]
        self.assertIs(context.reasoning_effort, ReasoningEffort.XHIGH)
        system = next(message for message in context.messages if message.role is Role.SYSTEM)
        self.assertIn("extra-high review depth", system.content)
        persisted_system = next(
            message for message in result.messages if message.role is Role.SYSTEM
        )
        self.assertNotIn("extra-high review depth", persisted_system.content)

    async def test_provider_usage_emits_current_context_metadata(self) -> None:
        provider = ScriptedProvider(((ModelTextDelta("done"), ModelCompleted("stop", 900, 100)),))
        runtime = AgentRuntime(
            provider=provider,
            tools=ToolRegistry(),
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=PermissionManager(),
            tool_context=ToolContext(Path("/workspace")),
        )

        result = await runtime.run("Measure the context")

        usage = next(
            event for event in result.events if event.kind is AgentEventKind.CONTEXT_USAGE_UPDATED
        )
        self.assertEqual(usage.data["input_tokens"], 900)
        self.assertEqual(usage.data["output_tokens"], 100)
        self.assertEqual(usage.data["used_tokens"], 1_000)
        self.assertFalse(usage.data["estimated"])

    async def test_completion_reminder_batch_is_bounded_and_defers_overflow(self) -> None:
        snapshots = tuple(completion_snapshot(f"task-{index:02d}") for index in range(21))
        manager = FixtureCompletionManager(snapshots)
        provider = ScriptedProvider(
            ((ModelTextDelta("Handled bounded reminder."), ModelCompleted("stop")),)
        )
        runtime = AgentRuntime(
            provider=provider,
            tools=ToolRegistry(),
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=PermissionManager(),
            tool_context=ToolContext(
                Path("/workspace"),
                background_tasks=manager.as_manager(),
            ),
        )

        result = await runtime.run("Inspect completions")

        reminder = "\n".join(
            message.content
            for message in provider.calls[0].messages
            if "<background-task-completions>" in message.content
        )
        self.assertIn('"task_id":"task-00"', reminder)
        self.assertIn('"task_id":"task-19"', reminder)
        self.assertNotIn('"task_id":"task-20"', reminder)
        self.assertIn("1 additional completion(s)", reminder)
        self.assertNotIn("Use task_output", reminder)
        event = next(
            event
            for event in result.events
            if event.kind is AgentEventKind.BACKGROUND_TASK_COMPLETION_REMINDER
        )
        self.assertEqual(event.data["count"], 20)
        self.assertEqual(event.data["remaining_count"], 1)
        self.assertEqual(manager.reported, {f"task-{index:02d}" for index in range(20)})
        self.assertEqual(
            [item.task_id for item in await manager.pending_completions()],
            ["task-20"],
        )

    async def test_background_auto_wake_injects_redacted_bounded_output_once(self) -> None:
        timestamp = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
        snapshot = BackgroundTaskSnapshot(
            task_id="task-auto-wake",
            command="private command",
            cwd="/private/workspace",
            status=BackgroundTaskStatus.COMPLETED,
            output="safe completion output with private output",
            total_output_bytes=43,
            truncated=False,
            exit_code=0,
            started_at=timestamp,
            finished_at=timestamp,
        )
        manager = FixtureCompletionManager((snapshot,))
        provider = ScriptedProvider(
            ((ModelTextDelta("Reported the completion."), ModelCompleted("stop")),)
        )
        runtime = AgentRuntime(
            provider=provider,
            tools=ToolRegistry(),
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=PermissionManager(),
            tool_context=ToolContext(
                Path("/workspace"),
                background_tasks=manager.as_manager(),
                redaction_values=("private output",),
            ),
        )

        result = await runtime.run("", turn_source=TurnSource.BACKGROUND_TASK_AUTO_WAKE)

        reminders = [
            message.content
            for message in provider.calls[0].messages
            if "<background-task-completions>" in message.content
        ]
        self.assertEqual(len(reminders), 1)
        self.assertIn('"output_preview":', reminders[0])
        self.assertIn("safe completion output", reminders[0])
        self.assertIn("untrusted task evidence", reminders[0])
        self.assertNotIn("private output", reminders[0])
        self.assertNotIn("private command", reminders[0])
        self.assertNotIn("/private/workspace", reminders[0])
        self.assertEqual(await manager.pending_completions(), ())
        self.assertNotIn(
            "<background-task-completions>",
            "\n".join(item.content for item in result.items if isinstance(item, Message)),
        )

    async def test_auto_wake_does_not_replace_a_previous_recoverable_execution_record(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(str(root), "fixture", "fixture-model")
            previous_event = AgentEvent.create(1, AgentEventKind.TURN_COMPLETED, {"step": 1})
            await store.append_event(session_id, previous_event)
            previous = SessionExecutionRecord(
                AgentExecutionOutcome(
                    AgentExecutionStatus.STUCK,
                    SupervisorReasonCode.PERIODIC_CYCLE,
                    finalized=True,
                    recoverable=True,
                ),
                previous_event.sequence,
                previous_event.created_at,
            )
            await store.save_execution_record(session_id, previous)
            manager = FixtureCompletionManager((completion_snapshot("task-wake"),))
            runtime = AgentRuntime(
                provider=ScriptedProvider(
                    ((ModelTextDelta("reported completion"), ModelCompleted("stop")),)
                ),
                tools=ToolRegistry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root, background_tasks=manager.as_manager()),
                session_store=store,
            )

            await runtime.run(
                "", session_id=session_id, turn_source=TurnSource.BACKGROUND_TASK_AUTO_WAKE
            )

            self.assertEqual(await store.load_execution_record(session_id), previous)

    async def test_completion_reminder_is_not_reinjected_after_the_manager_acknowledges_it(
        self,
    ) -> None:
        manager = FixtureCompletionManager((completion_snapshot("task-once"),))
        provider = ScriptedProvider(
            (
                (
                    ModelToolCall(ToolCall("inspect", "inspect", {})),
                    ModelCompleted("tool_calls"),
                ),
                (ModelTextDelta("done"), ModelCompleted("stop")),
            )
        )
        runtime = AgentRuntime(
            provider=provider,
            tools=MinimalToolCollection((CollectionFixtureTool("inspect", "evidence"),)),
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=PermissionManager(),
            tool_context=ToolContext(Path("/workspace"), background_tasks=manager.as_manager()),
        )

        await runtime.run("inspect completion")

        first_context = "\n".join(message.content for message in provider.calls[0].messages)
        second_context = "\n".join(message.content for message in provider.calls[1].messages)
        self.assertIn("<background-task-completions>", first_context)
        self.assertNotIn("<background-task-completions>", second_context)

    async def test_completion_during_tool_step_is_reported_at_next_model_boundary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trigger = root / "release-task"
            manager = LocalBackgroundTaskManager()
            task = await manager.start_exec(
                sys.executable,
                (
                    "-c",
                    "import pathlib,time;"
                    "p=pathlib.Path('release-task');"
                    'exec("while not p.exists():\\n time.sleep(0.01)");'
                    "print('private background output')",
                ),
                display_command="private background command",
                cwd=root,
                env={},
                output_byte_limit=2_000,
                termination_grace_seconds=0.05,
            )
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("release", "release_background_task", {})),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("Observed the completion."), ModelCompleted("stop")),
                )
            )
            release = ReleaseBackgroundTaskTool(trigger, manager, task.task_id)
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((release, TaskOutputTool())),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(mode=PermissionMode.BYPASS),
                tool_context=ToolContext(root, background_tasks=manager),
            )
            try:
                result = await runtime.run("Run the fixture workflow")

                first_text = "\n".join(
                    message.content
                    for message in provider.calls[0].messages
                    if message.role is Role.USER
                )
                second_text = "\n".join(
                    message.content
                    for message in provider.calls[1].messages
                    if message.role is Role.USER
                )
                self.assertNotIn("<background-task-completions>", first_text)
                self.assertIn("<background-task-completions>", second_text)
                self.assertIn(task.task_id, second_text)
                self.assertIn('"status":"completed"', second_text)
                self.assertIn("Use task_output", second_text)
                self.assertNotIn("private background command", second_text)
                self.assertNotIn("private background output", second_text)
                self.assertNotIn(
                    "<background-task-completions>",
                    "\n".join(item.content for item in result.items if isinstance(item, Message)),
                )
                reminders = [
                    event
                    for event in result.events
                    if event.kind is AgentEventKind.BACKGROUND_TASK_COMPLETION_REMINDER
                ]
                self.assertEqual(len(reminders), 1)
                self.assertEqual(reminders[0].data["task_ids"], [task.task_id])
                self.assertTrue(reminders[0].data["model_context_only"])
                self.assertEqual(await manager.pending_completions(), ())
            finally:
                await manager.shutdown()

    async def test_pre_output_failover_is_audited_and_updates_new_session_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = FailingProvider("primary")
            fallback = ScriptedProvider(
                ((ModelTextDelta("fallback response"), ModelCompleted("stop")),)
            )
            fallback.provider_name = "fallback"
            fallback.model_name = "fallback-model"
            fallback.context_affinity = "profile-v1:fallback"
            provider = FailoverModelProvider(
                (
                    ProviderCandidate(
                        primary.provider_name,
                        primary.model_name,
                        primary.context_affinity,
                        lambda: primary,
                    ),
                    ProviderCandidate(
                        fallback.provider_name,
                        fallback.model_name,
                        fallback.context_affinity,
                        lambda: fallback,
                    ),
                )
            )
            store = SqliteSessionStore(root / ".state" / "sessions.db")
            await store.initialize()
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )

            result = await runtime.run("hello")

            self.assertEqual(result.response, "fallback response")
            kinds = [event.kind for event in result.events]
            self.assertIn(AgentEventKind.PROVIDER_ATTEMPT_FAILED, kinds)
            self.assertIn(AgentEventKind.PROVIDER_SELECTED, kinds)
            selected = next(
                event for event in result.events if event.kind is AgentEventKind.PROVIDER_SELECTED
            )
            self.assertEqual(selected.data["provider"], "fallback")
            self.assertTrue(selected.data["failover"])
            self.assertTrue(selected.data["session_origin_updated"])
            assert result.session_id is not None
            summary = await store.get_session(result.session_id)
            self.assertEqual(summary.provider, "fallback")
            self.assertEqual(summary.model, "fallback-model")
            self.assertEqual(summary.context_affinity, "profile-v1:fallback")

    async def test_failover_does_not_relabel_existing_foreign_opaque_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / ".state" / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(
                str(root),
                "original",
                "original-model",
                "profile-v1:original",
            )
            preserved = PreservedContextItem(
                ContextItemKind.REASONING,
                {
                    "type": "reasoning",
                    "id": "old-reasoning",
                    "summary": [],
                    "encrypted_content": "foreign-opaque-state",
                },
            )
            initial_items = (Message(Role.SYSTEM, "system"), preserved)
            await store.save_session_items(session_id, initial_items)

            primary = FailingProvider("primary")
            fallback = ScriptedProvider(((ModelTextDelta("safe"), ModelCompleted("stop")),))
            fallback.provider_name = "fallback"
            fallback.model_name = "fallback-model"
            fallback.context_affinity = "profile-v1:fallback"
            provider = FailoverModelProvider(
                (
                    ProviderCandidate(
                        primary.provider_name,
                        primary.model_name,
                        primary.context_affinity,
                        lambda: primary,
                    ),
                    ProviderCandidate(
                        fallback.provider_name,
                        fallback.model_name,
                        fallback.context_affinity,
                        lambda: fallback,
                    ),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )

            result = await runtime.run(
                "continue",
                initial_items=initial_items,
                source_provider="original",
                source_model="original-model",
                source_context_affinity="profile-v1:original",
                session_id=session_id,
            )

            selected = next(
                event for event in result.events if event.kind is AgentEventKind.PROVIDER_SELECTED
            )
            self.assertFalse(selected.data["session_origin_updated"])
            summary = await store.get_session(session_id)
            self.assertEqual(summary.provider, "original")
            self.assertEqual(summary.context_affinity, "profile-v1:original")

    async def test_full_headless_read_edit_command_vertical_slice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "note.txt"
            target.write_text("original", encoding="utf-8")
            verify_command = (
                f'"{sys.executable}" -c "from pathlib import Path; '
                "assert Path('note.txt').read_text() == 'changed'\""
            )
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("read", "read_file", {"path": "note.txt"})),
                        ModelCompleted("tool_calls"),
                    ),
                    (
                        ModelToolCall(
                            ToolCall(
                                "edit",
                                "search_replace",
                                {"path": "note.txt", "old": "original", "new": "changed"},
                            )
                        ),
                        ModelCompleted("tool_calls"),
                    ),
                    (
                        ModelToolCall(ToolCall("verify", "bash", {"command": verify_command})),
                        ModelCompleted("tool_calls"),
                    ),
                    (
                        ModelTextDelta("Read, edited, and verified note.txt."),
                        ModelCompleted("stop"),
                    ),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=FilesystemWorkspaceChangeObserver(),
                permissions=PermissionManager(mode=PermissionMode.BYPASS),
                tool_context=ToolContext(root),
            )

            result = await runtime.run("Update and verify note.txt")

            self.assertEqual(target.read_text(encoding="utf-8"), "changed")
            self.assertEqual(result.steps, 4)
            completed_tools = [
                event for event in result.events if event.kind is AgentEventKind.TOOL_COMPLETED
            ]
            self.assertEqual(
                [event.data["name"] for event in completed_tools],
                [
                    "read_file",
                    "search_replace",
                    "bash",
                ],
            )
            self.assertTrue(all(event.data["duration_seconds"] >= 0 for event in completed_tools))
            edit_report = completed_tools[1].data["workspace_changes"]
            self.assertIsInstance(edit_report, dict)
            assert isinstance(edit_report, dict)
            edit_changes = edit_report["files"]
            self.assertIsInstance(edit_changes, list)
            assert isinstance(edit_changes, list)
            self.assertEqual(edit_changes[0]["path"], "note.txt")
            self.assertEqual(edit_changes[0]["status"], "modified")
            self.assertIn("-original", edit_changes[0]["diff"])
            self.assertIn("+changed", edit_changes[0]["diff"])
            self.assertEqual(result.response, "Read, edited, and verified note.txt.")

    async def test_bash_file_creation_emits_an_auditable_workspace_diff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(
                            ToolCall(
                                "write",
                                "bash",
                                {"command": "printf 'hello\\n' > generated.txt"},
                            )
                        ),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("Created generated.txt."), ModelCompleted("stop")),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=FilesystemWorkspaceChangeObserver(),
                permissions=PermissionManager(mode=PermissionMode.BYPASS),
                tool_context=ToolContext(root),
            )

            result = await runtime.run("Create generated.txt")

            completed = next(
                event for event in result.events if event.kind is AgentEventKind.TOOL_COMPLETED
            )
            report = completed.data["workspace_changes"]
            self.assertIsInstance(report, dict)
            assert isinstance(report, dict)
            changes = report["files"]
            self.assertIsInstance(changes, list)
            assert isinstance(changes, list)
            self.assertEqual(changes[0]["path"], "generated.txt")
            self.assertEqual(changes[0]["status"], "created")
            self.assertIn("+++ b/generated.txt", changes[0]["diff"])
            self.assertIn("+hello", changes[0]["diff"])

    async def test_read_tool_round_trip_and_session_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note.txt").write_text("source evidence", encoding="utf-8")
            provider = ScriptedProvider(
                (
                    (
                        ModelReasoningDelta("Need to inspect the file."),
                        ModelToolCall(ToolCall("call-1", "read_file", {"path": "note.txt"})),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("The file contains source evidence."), ModelCompleted("stop")),
                )
            )
            store = SqliteSessionStore(root / ".state" / "sessions.db")
            await store.initialize()
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )

            result = await runtime.run("Read note.txt")

            self.assertEqual(result.response, "The file contains source evidence.")
            self.assertEqual(result.steps, 2)
            self.assertIsNotNone(result.session_id)
            tool_messages = [message for message in result.messages if message.role is Role.TOOL]
            self.assertEqual(len(tool_messages), 1)
            self.assertIn("source evidence", tool_messages[0].content)
            self.assertIn(AgentEventKind.TOOL_COMPLETED, [event.kind for event in result.events])
            self.assertIn(AgentEventKind.REASONING_DELTA, [event.kind for event in result.events])
            assert result.session_id is not None
            persisted = await store.load_messages(result.session_id)
            self.assertEqual(persisted, list(result.messages))
            self.assertEqual(len(provider.calls), 2)
            prior_assistant = next(
                message for message in provider.calls[1].messages if message.role is Role.ASSISTANT
            )
            self.assertEqual(prior_assistant.reasoning_content, "Need to inspect the file.")

    async def test_default_headless_policy_denies_edit_and_agent_can_recover(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "note.txt"
            target.write_text("original", encoding="utf-8")
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(
                            ToolCall(
                                "call-1",
                                "search_replace",
                                {"path": "note.txt", "old": "original", "new": "changed"},
                            )
                        ),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("I could not edit without approval."), ModelCompleted("stop")),
                )
            )
            observer = RecordingWorkspaceChangeObserver(
                WorkspaceChangeReport(files=(), omitted_files=0, scan_limited=False)
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=observer,
                permissions=PermissionManager(mode=PermissionMode.DEFAULT, interactive=False),
                tool_context=ToolContext(root),
            )

            result = await runtime.run("Edit note.txt")

            self.assertEqual(target.read_text(encoding="utf-8"), "original")
            self.assertIn(AgentEventKind.TOOL_FAILED, [event.kind for event in result.events])
            second_request = provider.calls[1].messages
            denial = [message for message in second_request if message.role is Role.TOOL]
            self.assertIn("permission denied", denial[0].content)
            self.assertEqual(observer.capture_roots, [])

    async def test_interactive_approval_blocks_the_tool_until_user_allows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "note.txt"
            target.write_text("original", encoding="utf-8")
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(
                            ToolCall(
                                "edit",
                                "search_replace",
                                {"path": "note.txt", "old": "original", "new": "changed"},
                            )
                        ),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("Edit approved and completed."), ModelCompleted("stop")),
                )
            )
            approver = GateApprover()
            store = SqliteSessionStore(root / ".state" / "sessions.db")
            await store.initialize()
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(interactive=True),
                tool_context=ToolContext(root),
                approver=approver,
                session_store=store,
            )

            turn = asyncio.create_task(runtime.run("Edit note.txt"))
            try:
                await asyncio.wait_for(approver.requested.wait(), timeout=5)
                self.assertEqual(target.read_text(encoding="utf-8"), "original")
                self.assertNotIn("changed", approver.requests[0].summary)

                approver.resolve(PermissionApproval.allow_once())
                result = await turn
            finally:
                if not turn.done():
                    turn.cancel()
                    await asyncio.gather(turn, return_exceptions=True)

            self.assertEqual(target.read_text(encoding="utf-8"), "changed")
            kinds = [event.kind for event in result.events]
            self.assertLess(
                kinds.index(AgentEventKind.TOOL_APPROVAL_REQUESTED),
                kinds.index(AgentEventKind.TOOL_APPROVAL_RESOLVED),
            )
            self.assertLess(
                kinds.index(AgentEventKind.TOOL_APPROVAL_RESOLVED),
                kinds.index(AgentEventKind.TOOL_STARTED),
            )
            resolved = next(
                event
                for event in result.events
                if event.kind is AgentEventKind.TOOL_APPROVAL_RESOLVED
            )
            self.assertEqual(resolved.data["outcome"], "allow_once")
            self.assertEqual(resolved.data["effect"], "allow")
            assert result.session_id is not None
            persisted_kinds = [
                event["kind"] for event in await store.load_events(result.session_id)
            ]
            self.assertIn(AgentEventKind.TOOL_APPROVAL_REQUESTED.value, persisted_kinds)
            self.assertIn(AgentEventKind.TOOL_APPROVAL_RESOLVED.value, persisted_kinds)

    async def test_interactive_denial_prevents_the_tool_and_returns_a_tool_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "note.txt"
            target.write_text("original", encoding="utf-8")
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(
                            ToolCall(
                                "edit",
                                "search_replace",
                                {"path": "note.txt", "old": "original", "new": "changed"},
                            )
                        ),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("The user denied the edit."), ModelCompleted("stop")),
                )
            )
            approver = ImmediateApprover(PermissionApproval.deny("not now"))
            observer = RecordingWorkspaceChangeObserver(
                WorkspaceChangeReport(files=(), omitted_files=0, scan_limited=False)
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=observer,
                permissions=PermissionManager(interactive=True),
                tool_context=ToolContext(root),
                approver=approver,
            )

            result = await runtime.run("Edit note.txt")

            self.assertEqual(target.read_text(encoding="utf-8"), "original")
            self.assertEqual(len(approver.requests), 1)
            self.assertNotIn(AgentEventKind.TOOL_STARTED, [event.kind for event in result.events])
            denial = [
                message for message in provider.calls[1].messages if message.role is Role.TOOL
            ]
            self.assertEqual(denial[0].content, "permission denied: not now")
            self.assertEqual(observer.capture_roots, [])

    async def test_cancelling_an_approval_wait_never_starts_the_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "note.txt"
            target.write_text("original", encoding="utf-8")
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(
                            ToolCall(
                                "edit",
                                "search_replace",
                                {"path": "note.txt", "old": "original", "new": "changed"},
                            )
                        ),
                        ModelCompleted("tool_calls"),
                    ),
                )
            )
            approver = GateApprover()
            observed: list[AgentEventKind] = []
            observer = RecordingWorkspaceChangeObserver(
                WorkspaceChangeReport(files=(), omitted_files=0, scan_limited=False)
            )
            store = SqliteSessionStore(root / ".state" / "sessions.db")
            await store.initialize()
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=observer,
                permissions=PermissionManager(interactive=True),
                tool_context=ToolContext(root),
                approver=approver,
                session_store=store,
            )

            turn = asyncio.create_task(
                runtime.run("Edit note.txt", sink=lambda event: observed.append(event.kind))
            )
            try:
                await asyncio.wait_for(approver.requested.wait(), timeout=5)
                turn.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await turn
            finally:
                if not turn.done():
                    turn.cancel()
                    await asyncio.gather(turn, return_exceptions=True)

            self.assertEqual(target.read_text(encoding="utf-8"), "original")
            self.assertIn(AgentEventKind.TOOL_APPROVAL_REQUESTED, observed)
            self.assertIn(AgentEventKind.TOOL_FAILED, observed)
            self.assertIn(AgentEventKind.TURN_FAILED, observed)
            self.assertNotIn(AgentEventKind.TOOL_STARTED, observed)
            self.assertEqual(observer.capture_roots, [])
            sessions = await store.list_sessions()
            self.assertEqual(len(sessions), 1)
            items = await store.load_session_items(sessions[0].id)
            tool_messages = [
                item for item in items if isinstance(item, Message) and item.role is Role.TOOL
            ]
            self.assertEqual(len(tool_messages), 1)
            self.assertEqual(tool_messages[0].tool_call_id, "edit")
            self.assertIn("cancelled", tool_messages[0].content)
            persisted_events = await store.load_events(sessions[0].id)
            cancelled_failure = next(
                event
                for event in persisted_events
                if event["kind"] == AgentEventKind.TOOL_FAILED.value
            )
            self.assertTrue(cancelled_failure["data"]["cancelled"])

    async def test_cancelling_a_running_tool_balances_all_calls_for_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocking = BlockingTool()
            pending = NeverStartedTool()
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("active", "blocking_tool", {})),
                        ModelToolCall(ToolCall("pending", "never_started_tool", {})),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("Recovered after cancellation."), ModelCompleted("stop")),
                )
            )
            store = SqliteSessionStore(root / ".state" / "sessions.db")
            await store.initialize()
            report = WorkspaceChangeReport(
                files=(
                    WorkspaceFileChange(
                        path="cancelled.txt",
                        status="created",
                        additions=1,
                        deletions=0,
                        diff="+++ b/cancelled.txt\n+partial",
                        diff_truncated=False,
                    ),
                ),
                omitted_files=0,
                scan_limited=False,
            )
            observer = RecordingWorkspaceChangeObserver(report)
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((blocking, pending)),
                workspace_change_observer=observer,
                permissions=PermissionManager(mode=PermissionMode.BYPASS),
                tool_context=ToolContext(root),
                session_store=store,
            )

            turn = asyncio.create_task(runtime.run("Run both tools"))
            await asyncio.wait_for(blocking.started.wait(), timeout=1)
            turn.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await turn

            self.assertTrue(blocking.cancelled)
            self.assertFalse(pending.executed)
            sessions = await store.list_sessions()
            self.assertEqual(len(sessions), 1)
            items = await store.load_session_items(sessions[0].id)
            assistant = next(
                item for item in items if isinstance(item, Message) and item.role is Role.ASSISTANT
            )
            tool_messages = [
                item for item in items if isinstance(item, Message) and item.role is Role.TOOL
            ]
            self.assertEqual([call.id for call in assistant.tool_calls], ["active", "pending"])
            self.assertEqual(
                [message.tool_call_id for message in tool_messages],
                ["active", "pending"],
            )
            self.assertTrue(all("cancelled" in message.content for message in tool_messages))
            events = await store.load_events(sessions[0].id)
            failures = [
                event for event in events if event["kind"] == AgentEventKind.TOOL_FAILED.value
            ]
            self.assertEqual(len(failures), 2)
            self.assertTrue(all(event["data"]["cancelled"] for event in failures))
            self.assertEqual(failures[0]["data"]["workspace_changes"], report.to_event_payload())
            self.assertNotIn("workspace_changes", failures[1]["data"])
            self.assertTrue(failures[1]["data"]["not_started"])
            self.assertEqual(len(observer.capture_roots), 2)
            self.assertEqual(len(observer.comparisons), 1)

            recovered = await runtime.run(
                "Continue in the same session",
                initial_items=items,
                session_id=sessions[0].id,
            )

            self.assertEqual(recovered.session_id, sessions[0].id)
            self.assertEqual(recovered.response, "Recovered after cancellation.")
            retry_messages = provider.calls[1].messages
            retry_assistant = next(
                message for message in retry_messages if message.role is Role.ASSISTANT
            )
            retry_results = [message for message in retry_messages if message.role is Role.TOOL]
            self.assertEqual(len(retry_assistant.tool_calls), len(retry_results))

    async def test_explicit_deny_never_reaches_or_is_overridden_by_the_approver(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "note.txt"
            target.write_text("original", encoding="utf-8")
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(
                            ToolCall(
                                "edit",
                                "search_replace",
                                {"path": "note.txt", "old": "original", "new": "changed"},
                            )
                        ),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("The edit was denied by policy."), ModelCompleted("stop")),
                )
            )
            approver = ImmediateApprover(PermissionApproval.allow_session())
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(
                    mode=PermissionMode.BYPASS,
                    rules=(PermissionRule(PermissionEffect.DENY, "search_replace"),),
                    interactive=True,
                ),
                tool_context=ToolContext(root),
                approver=approver,
            )

            result = await runtime.run("Edit note.txt")

            self.assertEqual(target.read_text(encoding="utf-8"), "original")
            self.assertEqual(approver.requests, [])
            self.assertNotIn(
                AgentEventKind.TOOL_APPROVAL_REQUESTED,
                [event.kind for event in result.events],
            )

    async def test_imported_context_and_origin_reach_every_model_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preserved = PreservedContextItem(
                ContextItemKind.REASONING,
                {
                    "type": "reasoning",
                    "id": "reasoning-imported",
                    "summary": [{"type": "summary_text", "text": "source reasoning"}],
                    "encrypted_content": "opaque-provider-state",
                },
            )
            initial_items = (
                Message(Role.SYSTEM, "source system"),
                Message(Role.USER, "source question"),
                preserved,
                Message(Role.ASSISTANT, "source answer"),
            )
            provider = ScriptedProvider(((ModelTextDelta("continued"), ModelCompleted("stop")),))
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
            )

            result = await runtime.run(
                "continue",
                initial_items=initial_items,
                source_provider=UPSTREAM_IMPORT_PROVIDER,
                source_model="xai-test-model",
            )

            self.assertEqual(len(provider.calls), 1)
            request = provider.calls[0]
            self.assertEqual(request.source_provider, UPSTREAM_IMPORT_PROVIDER)
            self.assertEqual(request.source_model, "xai-test-model")
            self.assertIn(preserved, request.preserved_items)
            self.assertEqual(result.items[: len(initial_items)], initial_items)
            self.assertNotIn(preserved, result.messages)

    async def test_provider_native_items_are_replayed_and_persisted_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note.txt").write_text("native context", encoding="utf-8")
            reasoning = PreservedContextItem(
                ContextItemKind.REASONING,
                {
                    "type": "reasoning",
                    "id": "reasoning-native",
                    "summary": [{"type": "summary_text", "text": "read the file"}],
                    "encrypted_content": "opaque-native-state",
                    "status": "completed",
                },
            )
            provider = ScriptedProvider(
                (
                    (
                        ModelReasoningDelta("read the file"),
                        ModelToolCall(ToolCall("call-native", "read_file", {"path": "note.txt"})),
                        ModelCompleted("tool_calls", context_items=(reasoning,)),
                    ),
                    (ModelTextDelta("done"), ModelCompleted("stop")),
                )
            )
            store = SqliteSessionStore(root / ".state" / "sessions.db")
            await store.initialize()
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )

            result = await runtime.run("inspect note.txt")

            self.assertEqual(provider.calls[1].source_provider, "scripted")
            self.assertEqual(provider.calls[1].source_model, "fixture-model")
            self.assertEqual(
                provider.calls[1].source_context_affinity,
                "profile-v1:scripted",
            )
            self.assertIn(reasoning, provider.calls[1].preserved_items)
            reasoning_index = result.items.index(reasoning)
            first_assistant_index = next(
                index
                for index, item in enumerate(result.items)
                if isinstance(item, Message) and item.role is Role.ASSISTANT
            )
            self.assertLess(reasoning_index, first_assistant_index)
            assert result.session_id is not None
            self.assertEqual(await store.load_session_items(result.session_id), list(result.items))
            self.assertEqual(
                (await store.get_session(result.session_id)).context_affinity,
                "profile-v1:scripted",
            )

    async def test_provider_terminal_text_is_canonical_for_results_and_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider(
                (
                    (
                        ModelTextDelta("streamed text"),
                        ModelCompleted("stop", response_text="canonical text"),
                    ),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
            )

            result = await runtime.run("answer")

            self.assertEqual(result.response, "canonical text")
            self.assertEqual(result.messages[-1].content, "canonical text")
            deltas = [
                event.data["text"]
                for event in result.events
                if event.kind is AgentEventKind.TEXT_DELTA
            ]
            self.assertEqual(deltas, ["streamed text"])

    async def test_backend_tools_emit_audit_events_without_local_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider(
                (
                    (
                        ModelBackendToolStarted("server-1", "web_search"),
                        ModelBackendToolCompleted("server-1", "web_search"),
                        ModelTextDelta("server-side research complete"),
                        ModelCompleted("stop"),
                    ),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
            )

            result = await runtime.run("research")

            backend_events = [
                event
                for event in result.events
                if event.kind
                in {
                    AgentEventKind.BACKEND_TOOL_STARTED,
                    AgentEventKind.BACKEND_TOOL_COMPLETED,
                }
            ]
            self.assertEqual(
                [event.kind for event in backend_events],
                [
                    AgentEventKind.BACKEND_TOOL_STARTED,
                    AgentEventKind.BACKEND_TOOL_COMPLETED,
                ],
            )
            self.assertEqual(dict(backend_events[0].data), {"id": "server-1", "name": "web_search"})
            self.assertGreaterEqual(backend_events[1].data["duration_seconds"], 0)
            self.assertFalse(any(message.role is Role.TOOL for message in result.messages))
            self.assertFalse(
                any(
                    event.kind
                    in {
                        AgentEventKind.TOOL_REQUESTED,
                        AgentEventKind.TOOL_PERMISSION,
                        AgentEventKind.TOOL_STARTED,
                        AgentEventKind.TOOL_COMPLETED,
                        AgentEventKind.TOOL_FAILED,
                    }
                    for event in result.events
                )
            )

    async def test_supervision_observes_a_no_tool_turn_without_changing_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            traces: list[SupervisionTraceRecord] = []
            provider = ScriptedProvider(((ModelTextDelta("done"), ModelCompleted("stop", 3, 2)),))
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                supervision_observer=traces.append,
            )

            result = await runtime.run("answer")

            self.assertEqual(result.response, "done")
            self.assertEqual(
                [record.checkpoint for record in traces],
                [SupervisionCheckpoint.BEFORE_MODEL, SupervisionCheckpoint.AFTER_MODEL],
            )
            self.assertEqual(traces[-1].snapshot.counters.model_requests, 1)
            self.assertEqual(traces[-1].snapshot.counters.model_completions, 1)
            self.assertEqual(traces[-1].snapshot.counters.tool_rounds, 0)
            self.assertNotIn("supervision", " ".join(event.kind.value for event in result.events))

    async def test_supervision_observes_one_tool_and_preserves_event_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            traces: list[SupervisionTraceRecord] = []
            tool = CollectionFixtureTool("inspect", "evidence")
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("inspect-1", "inspect", {"path": "note.txt"})),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("finished"), ModelCompleted("stop")),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((tool,)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                supervision_observer=traces.append,
            )

            result = await runtime.run("inspect")

            self.assertEqual(tool.calls, [{"path": "note.txt"}])
            self.assertEqual(
                [record.checkpoint for record in traces],
                [
                    SupervisionCheckpoint.BEFORE_MODEL,
                    SupervisionCheckpoint.AFTER_MODEL,
                    SupervisionCheckpoint.AFTER_TOOL_BATCH,
                    SupervisionCheckpoint.AFTER_TOOL,
                    SupervisionCheckpoint.BEFORE_MODEL,
                    SupervisionCheckpoint.AFTER_MODEL,
                ],
            )
            after_tool = next(
                record for record in traces if record.checkpoint is SupervisionCheckpoint.AFTER_TOOL
            )
            self.assertEqual(after_tool.snapshot.counters.tool_calls_executed, 1)
            kinds = [event.kind for event in result.events]
            self.assertLess(
                kinds.index(AgentEventKind.MODEL_STEP_STARTED),
                kinds.index(AgentEventKind.TOOL_REQUESTED),
            )
            self.assertLess(
                kinds.index(AgentEventKind.TOOL_REQUESTED),
                kinds.index(AgentEventKind.TOOL_COMPLETED),
            )
            second_step = [
                index
                for index, kind in enumerate(kinds)
                if kind is AgentEventKind.MODEL_STEP_STARTED
            ][1]
            self.assertLess(kinds.index(AgentEventKind.TOOL_COMPLETED), second_step)
            self.assertLess(second_step, kinds.index(AgentEventKind.TURN_COMPLETED))

    async def test_supervision_counts_one_round_for_multiple_tool_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            traces: list[SupervisionTraceRecord] = []
            first = CollectionFixtureTool("first", "one")
            second = CollectionFixtureTool("second", "two")
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("first-1", "first", {})),
                        ModelToolCall(ToolCall("second-1", "second", {})),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("finished"), ModelCompleted("stop")),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((first, second)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                supervision_observer=traces.append,
            )

            await runtime.run("run both")

            batch = next(
                record
                for record in traces
                if record.checkpoint is SupervisionCheckpoint.AFTER_TOOL_BATCH
            )
            after_tools = [
                record for record in traces if record.checkpoint is SupervisionCheckpoint.AFTER_TOOL
            ]
            self.assertEqual(batch.snapshot.counters.tool_rounds, 1)
            self.assertEqual(batch.snapshot.counters.tool_calls_requested, 2)
            self.assertEqual([record.tool_name for record in after_tools], ["first", "second"])
            self.assertEqual([first.calls, second.calls], [[{}], [{}]])

    async def test_supervision_loop_decision_does_not_stop_the_existing_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            traces: list[SupervisionTraceRecord] = []
            tool = CollectionFixtureTool("repeat", "same result")
            scripts = tuple(
                (
                    ModelToolCall(ToolCall(f"repeat-{index}", "repeat", {})),
                    ModelCompleted("tool_calls"),
                )
                for index in range(4)
            )
            provider = ScriptedProvider(scripts)
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((tool,)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                max_steps=4,
                supervision_observer=traces.append,
            )

            with self.assertRaisesRegex(
                ProviderError, "agent exceeded the maximum of 4 model steps"
            ):
                await runtime.run("repeat")

            self.assertEqual(len(tool.calls), 4)
            self.assertIn(
                SupervisorDecisionKind.MARK_STUCK,
                [record.decision.kind for record in traces],
            )
            self.assertNotIn(
                SupervisionCheckpoint.AFTER_MODEL,
                [record.checkpoint for record in traces if record.model_step == 5],
            )

    async def test_supervision_budget_decision_does_not_prevent_the_next_model_request(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            traces: list[SupervisionTraceRecord] = []
            tool = CollectionFixtureTool("inspect", "evidence")
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("inspect-1", "inspect", {})),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("finished"), ModelCompleted("stop")),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((tool,)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                supervisor_factory=observing_supervisor_factory(
                    observation_budget(max_model_calls=2)
                ),
                supervision_observer=traces.append,
            )

            result = await runtime.run("inspect")

            self.assertEqual(result.response, "finished")
            self.assertEqual(len(provider.calls), 2)
            self.assertNotIn(
                SupervisorDecisionKind.FINALIZE, [record.decision.kind for record in traces]
            )
            self.assertEqual(provider.tool_definitions[0], provider.tool_definitions[1])

    async def test_supervision_observes_tool_error_unknown_tool_and_permission_denial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            traces: list[SupervisionTraceRecord] = []
            failed_tool = OSErrorSideEffectTool([], ToolResult("unused"))
            denied_tool = NeverStartedTool()
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("error", failed_tool.definition.name, {})),
                        ModelCompleted("tool_calls"),
                    ),
                    (
                        ModelToolCall(ToolCall("missing", "missing", {})),
                        ModelCompleted("tool_calls"),
                    ),
                    (
                        ModelToolCall(ToolCall("denied", denied_tool.definition.name, {})),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("recovered"), ModelCompleted("stop")),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((failed_tool, denied_tool)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(
                    mode=PermissionMode.BYPASS,
                    rules=(PermissionRule(PermissionEffect.DENY, denied_tool.definition.name),),
                ),
                tool_context=ToolContext(root),
                supervision_observer=traces.append,
            )

            result = await runtime.run("recover")

            tool_records = [
                record for record in traces if record.checkpoint is SupervisionCheckpoint.AFTER_TOOL
            ]
            self.assertEqual(len(tool_records), 3)
            self.assertTrue(
                all(record.snapshot.recent_interactions[-1].is_error for record in tool_records)
            )
            self.assertEqual(result.response, "recovered")
            self.assertEqual(
                [event.kind for event in result.events].count(AgentEventKind.TOOL_FAILED),
                3,
            )
            self.assertFalse(denied_tool.executed)

    async def test_supervision_failures_do_not_change_provider_errors_or_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def failing_factory() -> AgentExecutionSupervisor:
                raise RuntimeError("supervision setup failed")

            failing_runtime = AgentRuntime(
                provider=FailingProvider("failing"),
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                supervisor_factory=failing_factory,
            )
            with self.assertRaisesRegex(ProviderError, "failing unavailable"):
                await failing_runtime.run("fail")

            completed_with_failed_supervisor = AgentRuntime(
                provider=ScriptedProvider(((ModelTextDelta("done"), ModelCompleted("stop")),)),
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                supervisor_factory=failing_factory,
            )

            completed = await completed_with_failed_supervisor.run("complete")

            self.assertEqual(completed.response, "done")
            self.assertEqual(
                [event.kind for event in completed.events],
                [
                    AgentEventKind.SESSION_STARTED,
                    AgentEventKind.USER_MESSAGE,
                    AgentEventKind.MODEL_STEP_STARTED,
                    AgentEventKind.MODEL_THINKING_COMPLETED,
                    AgentEventKind.TEXT_DELTA,
                    AgentEventKind.TURN_COMPLETED,
                ],
            )

            provider = BlockingProvider()
            cancellation_runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                supervision_observer=lambda record: (_ for _ in ()).throw(
                    RuntimeError("observer failed")
                ),
            )
            turn = asyncio.create_task(cancellation_runtime.run("cancel"))
            await asyncio.wait_for(provider.started.wait(), timeout=1)
            turn.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await turn

    async def test_supervision_isolated_for_each_runtime_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            created: list[AgentExecutionSupervisor] = []
            provider = ScriptedProvider(
                (
                    (ModelTextDelta("first"), ModelCompleted("stop")),
                    (ModelTextDelta("second"), ModelCompleted("stop")),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                supervisor_factory=observing_supervisor_factory(observation_budget(), created),
            )

            first = await runtime.run("first")
            second = await runtime.run("second")

            self.assertEqual((first.response, second.response), ("first", "second"))
            self.assertEqual(len(created), 2)
            self.assertIsNot(created[0], created[1])
            self.assertEqual(
                [supervisor.snapshot.counters.model_requests for supervisor in created], [1, 1]
            )

    async def test_default_execution_control_remains_observe_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            emitted: list[AgentEvent] = []
            tool = CollectionFixtureTool("repeat", "same")
            provider = ScriptedProvider(
                ((ModelToolCall(ToolCall("repeat-1", "repeat", {})), ModelCompleted("tool_calls")),)
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((tool,)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                max_steps=1,
            )

            with self.assertRaisesRegex(
                ProviderError, "agent exceeded the maximum of 1 model steps"
            ):
                await runtime.run("repeat", sink=emitted.append)

            self.assertEqual(tool.calls, [{}])
            self.assertEqual(provider.tool_policies, [ModelToolPolicy.ALLOWED])
            self.assertNotIn(
                AgentEventKind.FINALIZING_STARTED,
                [event.kind for event in emitted],
            )

    async def test_controlled_mode_finalizes_after_budget_limited_tool_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = CollectionFixtureTool("inspect", "evidence")
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("inspect-1", "inspect", {})),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("bounded final response"), ModelCompleted("stop")),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((tool,)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                supervisor_factory=observing_supervisor_factory(
                    observation_budget(max_model_calls=1)
                ),
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
            )

            result = await runtime.run("inspect")

            assert result.outcome is not None
            self.assertEqual(result.response, "bounded final response")
            self.assertIs(result.outcome.status, AgentExecutionStatus.BUDGET_LIMITED)
            self.assertTrue(result.outcome.finalized)
            self.assertTrue(result.outcome.recoverable)
            self.assertEqual(result.steps, 1)
            self.assertEqual(
                provider.tool_policies, [ModelToolPolicy.ALLOWED, ModelToolPolicy.DISABLED]
            )
            completed = result.events[-1]
            self.assertIs(completed.kind, AgentEventKind.TURN_COMPLETED)
            self.assertEqual(completed.data["execution_status"], "budget_limited")
            self.assertEqual(completed.data["finalization_attempts"], 1)
            finalizing = [
                event for event in result.events if event.kind is AgentEventKind.FINALIZING_STARTED
            ]
            self.assertEqual(len(finalizing), 1)
            self.assertEqual(
                finalizing[0].data,
                {
                    "execution_status": "budget_limited",
                    "execution_reason": "model_call_budget",
                    "recoverable": True,
                },
            )
            kinds = [event.kind for event in result.events]
            self.assertLess(
                kinds.index(AgentEventKind.TOOL_COMPLETED),
                kinds.index(AgentEventKind.FINALIZING_STARTED),
            )
            self.assertLess(
                kinds.index(AgentEventKind.FINALIZING_STARTED),
                kinds.index(AgentEventKind.TEXT_DELTA),
            )
            self.assertLess(
                kinds.index(AgentEventKind.TEXT_DELTA),
                kinds.index(AgentEventKind.TURN_COMPLETED),
            )

    async def test_controlled_terminal_outcome_is_persisted_with_its_completion_event(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            tool = CollectionFixtureTool("inspect", "evidence")
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("inspect-1", "inspect", {})),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("durable summary"), ModelCompleted("stop")),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((tool,)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
                supervisor_factory=observing_supervisor_factory(
                    observation_budget(max_model_calls=1)
                ),
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
            )

            result = await runtime.run("inspect")

            assert result.session_id is not None
            assert result.outcome is not None
            completed = next(
                event for event in result.events if event.kind is AgentEventKind.TURN_COMPLETED
            )
            self.assertEqual(
                await store.load_execution_record(result.session_id),
                SessionExecutionRecord(result.outcome, completed.sequence, completed.created_at),
            )

    async def test_terminal_event_is_delivered_after_finalize_turn_commits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(str(root), "fixture", "fixture-model")
            observations: list[tuple[bool, bool, int]] = []

            async def observe(event: AgentEvent) -> None:
                if event.kind is not AgentEventKind.TURN_COMPLETED:
                    return
                persisted_events = await store.load_events(session_id)
                persisted_record = await store.load_execution_record(session_id)
                observations.append(
                    (
                        any(item["sequence"] == event.sequence for item in persisted_events),
                        persisted_record is not None
                        and persisted_record.event_sequence == event.sequence,
                        len(await store.load_session_items(session_id)),
                    )
                )

            runtime = AgentRuntime(
                provider=ScriptedProvider(((ModelTextDelta("committed"), ModelCompleted("stop")),)),
                tools=ToolRegistry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )

            result = await runtime.run("inspect", session_id=session_id, sink=observe)

            self.assertEqual(observations, [(True, True, len(result.items))])

    async def test_normal_completion_replaces_a_previous_recoverable_execution_record(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(str(root), "fixture", "fixture-model")
            previous_event = AgentEvent.create(1, AgentEventKind.TURN_COMPLETED, {"step": 1})
            await store.append_event(session_id, previous_event)
            await store.save_execution_record(
                session_id,
                SessionExecutionRecord(
                    AgentExecutionOutcome(
                        AgentExecutionStatus.STUCK,
                        SupervisorReasonCode.PERIODIC_CYCLE,
                        finalized=True,
                        recoverable=True,
                    ),
                    previous_event.sequence,
                    previous_event.created_at,
                ),
            )
            runtime = AgentRuntime(
                provider=ScriptedProvider(((ModelTextDelta("continued"), ModelCompleted("stop")),)),
                tools=ToolRegistry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )

            result = await runtime.run("continue", session_id=session_id)

            completed = next(
                event for event in result.events if event.kind is AgentEventKind.TURN_COMPLETED
            )
            self.assertEqual(
                await store.load_execution_record(session_id),
                SessionExecutionRecord(
                    AgentExecutionOutcome(
                        AgentExecutionStatus.COMPLETED,
                        None,
                        finalized=False,
                        recoverable=False,
                    ),
                    completed.sequence,
                    completed.created_at,
                ),
            )

    async def test_controlled_mode_finalizes_at_hard_max_steps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = CollectionFixtureTool("inspect", "evidence")
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("inspect-1", "inspect", {})),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("hard limit summary"), ModelCompleted("stop")),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((tool,)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                max_steps=1,
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
            )

            result = await runtime.run("inspect")

            assert result.outcome is not None
            self.assertEqual(result.response, "hard limit summary")
            self.assertIs(result.outcome.status, AgentExecutionStatus.BUDGET_LIMITED)
            self.assertIs(result.outcome.reason_code, SupervisorReasonCode.MODEL_STEP_LIMIT)
            self.assertEqual(result.steps, 1)
            self.assertEqual(
                provider.tool_policies, [ModelToolPolicy.ALLOWED, ModelToolPolicy.DISABLED]
            )

    async def test_hard_max_finalization_does_not_prewrite_intermediate_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(str(root), "fixture", "fixture-model")
            tool = CollectionFixtureTool("inspect", "evidence")
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("inspect-1", "inspect", {})),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("hard limit summary"), ModelCompleted("stop")),
                )
            )
            observed_items: list[list[SessionItem]] = []

            async def observe(event: AgentEvent) -> None:
                if event.kind is AgentEventKind.FINALIZING_STARTED:
                    observed_items.append(await store.load_session_items(session_id))

            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((tool,)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
                max_steps=1,
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
            )

            result = await runtime.run("inspect", session_id=session_id, sink=observe)

            self.assertEqual(observed_items, [[]])
            self.assertEqual(await store.load_session_items(session_id), list(result.items))

    async def test_controlled_mode_finalizes_after_stuck_batch_without_stopping_mid_batch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = CollectionFixtureTool("repeat", "same result")
            scripts = [
                (
                    ModelToolCall(ToolCall(f"repeat-{index}", "repeat", {})),
                    ModelCompleted("tool_calls"),
                )
                for index in range(1, 5)
            ]
            provider = ScriptedProvider(
                (*scripts, (ModelTextDelta("stuck summary"), ModelCompleted("stop")))
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((tool,)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                max_steps=4,
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
            )

            result = await runtime.run("repeat")

            assert result.outcome is not None
            self.assertIs(result.outcome.status, AgentExecutionStatus.STUCK)
            self.assertEqual(tool.calls, [{}, {}, {}, {}])
            self.assertEqual(result.response, "stuck summary")
            self.assertEqual(len(provider.calls), 5)
            self.assertIs(provider.tool_policies[-1], ModelToolPolicy.DISABLED)

    async def test_normal_no_tool_answer_wins_over_terminal_budget_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider(
                ((ModelTextDelta("ordinary answer"), ModelCompleted("stop", 0, 1)),)
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                supervisor_factory=observing_supervisor_factory(
                    observation_budget(max_output_tokens=1)
                ),
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
            )

            result = await runtime.run("answer")

            self.assertEqual(result.response, "ordinary answer")
            self.assertIsNone(result.outcome)
            self.assertEqual(provider.tool_policies, [ModelToolPolicy.ALLOWED])
            self.assertNotIn(
                AgentEventKind.FINALIZING_STARTED,
                [event.kind for event in result.events],
            )

    async def test_terminal_decision_never_stops_a_multi_tool_batch_and_finalizes_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = CollectionFixtureTool("first", "one")
            second = CollectionFixtureTool("second", "two")
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("first-1", "first", {})),
                        ModelToolCall(ToolCall("second-1", "second", {})),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("batch final"), ModelCompleted("stop")),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((first, second)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                supervisor_factory=observing_supervisor_factory(
                    observation_budget(max_model_calls=3, max_tool_calls=1)
                ),
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
            )

            result = await runtime.run("run both")

            self.assertEqual([first.calls, second.calls], [[{}], [{}]])
            self.assertEqual(result.response, "batch final")
            tool_messages = [message for message in result.messages if message.role is Role.TOOL]
            assistant_calls = [
                call
                for message in result.messages
                if message.role is Role.ASSISTANT
                for call in message.tool_calls
            ]
            self.assertEqual(
                {message.tool_call_id for message in tool_messages},
                {call.id for call in assistant_calls},
            )
            text_events = [
                event for event in result.events if event.kind is AgentEventKind.TEXT_DELTA
            ]
            self.assertEqual([event.data["text"] for event in text_events], ["batch final"])

    async def test_finalizer_tool_calls_are_not_executed_or_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = CollectionFixtureTool("inspect", "evidence")
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("inspect-1", "inspect", {})),
                        ModelCompleted("tool_calls"),
                    ),
                    (
                        ModelToolCall(
                            ToolCall("finalizer-call", "unexpected", {"secret": "hidden"})
                        ),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("safe final"), ModelCompleted("stop")),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((tool,)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory), redaction_values=("hidden",)),
                supervisor_factory=observing_supervisor_factory(
                    observation_budget(max_model_calls=1)
                ),
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
            )

            result = await runtime.run("inspect")

            self.assertEqual(tool.calls, [{}])
            self.assertEqual(
                [event.kind for event in result.events].count(AgentEventKind.TOOL_REQUESTED),
                1,
            )
            self.assertEqual(result.response, "safe final")
            self.assertNotIn("finalizer-rejected-", repr(result.items))
            self.assertNotIn("hidden", repr(result.items))

    async def test_finalizer_factory_failure_does_not_fall_back_to_normal_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = CollectionFixtureTool("inspect", "evidence")
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("inspect-1", "inspect", {})),
                        ModelCompleted("tool_calls"),
                    ),
                )
            )

            def failing_factory(
                model: ModelProvider,
                attempts: int,
                redactions: tuple[str, ...],
            ) -> AgentFinalizer:
                del model, attempts, redactions
                raise RuntimeError("finalizer unavailable")

            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((tool,)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                supervisor_factory=observing_supervisor_factory(
                    observation_budget(max_model_calls=1)
                ),
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
                finalizer_factory=failing_factory,
            )

            with self.assertRaisesRegex(RuntimeError, "finalizer unavailable"):
                await runtime.run("inspect")

            self.assertEqual(len(provider.calls), 1)

    async def test_finalizer_provider_error_records_turn_failure_and_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            emitted: list[AgentEvent] = []
            tool = CollectionFixtureTool("inspect", "evidence")
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("inspect-1", "inspect", {})),
                        ModelCompleted("tool_calls"),
                    ),
                    (ProviderError("finalizer provider failed"),),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((tool,)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                supervisor_factory=observing_supervisor_factory(
                    observation_budget(max_model_calls=1)
                ),
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
            )

            with self.assertRaisesRegex(ProviderError, "finalizer provider failed"):
                await runtime.run("inspect", sink=emitted.append)

            self.assertIs(emitted[-1].kind, AgentEventKind.TURN_FAILED)
            self.assertEqual(emitted[-1].data["error_type"], "ProviderError")

    async def test_finalizer_cancellation_preserves_turn_failure_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            emitted: list[AgentEvent] = []
            tool = CollectionFixtureTool("inspect", "evidence")
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("inspect-1", "inspect", {})),
                        ModelCompleted("tool_calls"),
                    ),
                    (asyncio.CancelledError(),),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((tool,)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                supervisor_factory=observing_supervisor_factory(
                    observation_budget(max_model_calls=1)
                ),
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
            )

            with self.assertRaises(asyncio.CancelledError):
                await runtime.run("inspect", sink=emitted.append)

            self.assertIs(emitted[-1].kind, AgentEventKind.TURN_FAILED)
            self.assertTrue(emitted[-1].data["cancelled"])

    async def test_runtime_finalization_evidence_is_conservative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = "runtime-evidence-secret"
            tool = CollectionFixtureTool("inspect", f"evidence {secret}")
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("inspect-1", "inspect", {})),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("safe"), ModelCompleted("stop")),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((tool,)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory), redaction_values=(secret,)),
                supervisor_factory=observing_supervisor_factory(
                    observation_budget(max_model_calls=1)
                ),
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
            )

            await runtime.run("inspect")

            finalizer_instruction = next(
                message.content
                for message in provider.calls[-1].messages
                if message.role is Role.SYSTEM and message.content.startswith("You are producing")
            )
            self.assertIn("No additional verification should be claimed", finalizer_instruction)
            self.assertNotIn(secret, finalizer_instruction)
            self.assertNotIn("digest", finalizer_instruction)
            self.assertNotIn("tool result", finalizer_instruction.lower())

    async def test_finalization_workspace_evidence_is_redacted_and_excludes_diffs_and_tool_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = "workspace-evidence-secret"
            report = WorkspaceChangeReport(
                (
                    WorkspaceFileChange(
                        f"src/{secret}.py",
                        "modified",
                        3,
                        1,
                        diff=f"-{secret}\n+private diff content",
                        diff_truncated=False,
                    ),
                ),
                omitted_files=0,
                scan_limited=False,
            )
            observer = RecordingWorkspaceChangeObserver(report)
            tool = OrderedSideEffectTool([], ToolResult(f"tool output {secret}"))
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("change", tool.definition.name, {})),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("safe final response"), ModelCompleted("stop")),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((tool,)),
                workspace_change_observer=observer,
                permissions=PermissionManager(mode=PermissionMode.BYPASS),
                tool_context=ToolContext(Path(directory), redaction_values=(secret,)),
                supervisor_factory=observing_supervisor_factory(
                    observation_budget(max_model_calls=1)
                ),
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
            )

            await runtime.run("apply the change")

            finalizer_instruction = next(
                message.content
                for message in provider.calls[-1].messages
                if message.role is Role.SYSTEM and message.content.startswith("You are producing")
            )
            self.assertIn("modified src/[REDACTED].py (+3/-1)", finalizer_instruction)
            self.assertIn("Confirmed validation: none provided", finalizer_instruction)
            self.assertNotIn(secret, finalizer_instruction)
            self.assertNotIn("private diff content", finalizer_instruction)
            self.assertNotIn("tool output", finalizer_instruction)
            self.assertNotIn("digest", finalizer_instruction)
            self.assertNotIn(secret, repr(provider.calls[-1]))
            self.assertNotIn("private diff content", repr(provider.calls[-1]))
            self.assertNotIn("ToolResult(", repr(provider.calls[-1]))

    async def test_supervision_background_metadata_uses_only_stable_allowlisted_progress(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = "background-metadata-secret"
            traces: list[SupervisionTraceRecord] = []
            tool = MetadataFixtureTool(
                "task_output",
                ToolResult(
                    "background output",
                    metadata={
                        "status": "running",
                        "total_output_bytes": 30,
                        "exit_code": 0,
                        "secret": secret,
                        "nested": {"secret": secret},
                    },
                ),
            )
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("task", "task_output", {"task_id": "one"})),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("done"), ModelCompleted("stop")),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((tool,)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory), redaction_values=(secret,)),
                supervision_observer=traces.append,
            )

            result = await runtime.run("poll the task")

            tool_trace = next(
                record for record in traces if record.checkpoint is SupervisionCheckpoint.AFTER_TOOL
            )
            interaction = tool_trace.snapshot.recent_interactions[-1]
            self.assertIs(interaction.progress_kind, ProgressKind.EXTERNAL_STATE)
            self.assertNotIn(secret, repr(tool_trace))
            self.assertNotIn("nested", repr(tool_trace))
            self.assertEqual(result.response, "done")
            self.assertEqual(tool.calls, [{"task_id": "one"}])

    async def test_runtime_rejects_invalid_control_and_finalizer_attempt_configuration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            common = {
                "provider": ScriptedProvider(((ModelTextDelta("done"), ModelCompleted("stop")),)),
                "tools": default_tool_registry(),
                "workspace_change_observer": EmptyWorkspaceChangeObserver(),
                "permissions": PermissionManager(),
                "tool_context": ToolContext(Path(directory)),
            }
            with self.assertRaisesRegex(TypeError, "execution_control_mode"):
                AgentRuntime(
                    **common,
                    execution_control_mode="finalize_terminal",  # type: ignore[arg-type]
                )
            with self.assertRaisesRegex(ValueError, "finalizer_max_attempts"):
                AgentRuntime(**common, finalizer_max_attempts=True)

    async def test_runtime_compaction_seam_is_default_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = AgentRuntime(
                provider=ScriptedProvider(((ModelTextDelta("done"), ModelCompleted("stop")),)),
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
            )

            with self.assertRaisesRegex(ConfigurationError, "not configured"):
                await runtime.trigger_context_compaction(compaction_runtime_request_fixture())

    async def test_runtime_compaction_seam_forwards_only_explicit_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gate = RecordingCompactionRuntimeGate()
            request = compaction_runtime_request_fixture()
            runtime = AgentRuntime(
                provider=ScriptedProvider(((ModelTextDelta("done"), ModelCompleted("stop")),)),
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                compaction_runtime_gate=gate,
            )

            with self.assertRaisesRegex(ProviderError, "compaction gate fixture failure"):
                await runtime.trigger_context_compaction(request)

            self.assertEqual(gate.requests, [request])
            self.assertIs(gate.requests[0], request)

    async def test_runtime_rejects_invalid_compaction_gate_configuration(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(
                TypeError,
                "compaction_runtime_gate",
            ),
        ):
            AgentRuntime(
                provider=ScriptedProvider(((ModelTextDelta("done"), ModelCompleted("stop")),)),
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                compaction_runtime_gate=cast(ContextCompactionRuntimeGate, object()),
            )

    async def test_controlled_finalizer_state_is_isolated_between_turns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = CollectionFixtureTool("inspect", "evidence")
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("inspect-1", "inspect", {})),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("first final"), ModelCompleted("stop")),
                    (ModelTextDelta("second ordinary"), ModelCompleted("stop")),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=MinimalToolCollection((tool,)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                supervisor_factory=observing_supervisor_factory(
                    observation_budget(max_model_calls=1)
                ),
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
            )

            first = await runtime.run("inspect")
            second = await runtime.run("ordinary")

            self.assertEqual((first.response, second.response), ("first final", "second ordinary"))
            self.assertIsNotNone(first.outcome)
            self.assertIsNone(second.outcome)

    async def test_unimplemented_decisions_remain_observation_only(self) -> None:
        decisions = (
            SupervisorDecision(
                SupervisorDecisionKind.REPLAN,
                "a different approach is required",
                AgentExecutionStatus.RUNNING,
                False,
                SupervisorReasonCode.NO_PROGRESS,
            ),
            SupervisorDecision(
                SupervisorDecisionKind.BLOCK,
                "user intervention is required",
                AgentExecutionStatus.BLOCKED,
                False,
                SupervisorReasonCode.EXTERNAL_BLOCKED,
            ),
            SupervisorDecision(
                SupervisorDecisionKind.FAIL,
                "an internal failure was observed",
                AgentExecutionStatus.FAILED,
                False,
                SupervisorReasonCode.INTERNAL_FAILURE,
            ),
        )
        for decision in decisions:
            with self.subTest(decision=decision.kind), tempfile.TemporaryDirectory() as directory:
                provider = ScriptedProvider(
                    ((ModelTextDelta("ordinary answer"), ModelCompleted("stop")),)
                )
                runtime = AgentRuntime(
                    provider=provider,
                    tools=default_tool_registry(),
                    workspace_change_observer=EmptyWorkspaceChangeObserver(),
                    permissions=PermissionManager(),
                    tool_context=ToolContext(Path(directory)),
                    supervisor_factory=lambda decision=decision: DecisionInjectingSupervisor(
                        observation_budget(),
                        decision,
                    ),
                    execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
                    finalizer_factory=lambda model, attempts, redactions: self.fail(
                        "unimplemented decisions must not invoke the finalizer"
                    ),
                )

                result = await runtime.run("answer")

                self.assertEqual(result.response, "ordinary answer")
                self.assertIsNone(result.outcome)
                self.assertEqual(provider.tool_policies, [ModelToolPolicy.ALLOWED])

    async def test_timing_events_cover_thinking_tools_and_the_complete_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider(
                (
                    (
                        ModelReasoningDelta("private reasoning"),
                        ModelTextDelta("done"),
                        ModelCompleted("stop"),
                    ),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
            )

            result = await runtime.run("inspect")

            thinking = next(
                event
                for event in result.events
                if event.kind is AgentEventKind.MODEL_THINKING_COMPLETED
            )
            completed = next(
                event for event in result.events if event.kind is AgentEventKind.TURN_COMPLETED
            )
            self.assertEqual(thinking.data["step"], 1)
            self.assertGreaterEqual(thinking.data["duration_seconds"], 0)
            self.assertGreaterEqual(completed.data["duration_seconds"], 0)

    def test_interaction_modes_map_to_fail_closed_permission_policies(self) -> None:
        permissions = PermissionManager(interactive=True)
        runtime = AgentRuntime(
            provider=ScriptedProvider(()),
            tools=default_tool_registry(),
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=permissions,
            tool_context=ToolContext(Path("/workspace")),
        )

        runtime.set_interaction_mode(InteractionMode.PLAN)
        self.assertEqual(permissions.mode, PermissionMode.DONT_ASK)
        runtime.set_interaction_mode(InteractionMode.ACCEPT_EDITS)
        self.assertEqual(permissions.mode, PermissionMode.ACCEPT_EDITS)
        runtime.set_interaction_mode(InteractionMode.AUTO)
        self.assertEqual(permissions.mode, PermissionMode.ACCEPT_EDITS)
        self.assertFalse(runtime.auto_mode_unrestricted)

        explicit = PermissionManager(mode=PermissionMode.BYPASS, interactive=True)
        explicit_runtime = AgentRuntime(
            provider=ScriptedProvider(()),
            tools=default_tool_registry(),
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=explicit,
            tool_context=ToolContext(Path("/workspace")),
        )
        self.assertTrue(explicit_runtime.auto_mode_unrestricted)
        explicit_runtime.set_interaction_mode(InteractionMode.NORMAL)
        explicit_runtime.set_interaction_mode(InteractionMode.AUTO)
        self.assertEqual(explicit.mode, PermissionMode.BYPASS)


if __name__ == "__main__":
    unittest.main()
