from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from neuro_code.adapters.background_tasks import LocalBackgroundTaskManager
from neuro_code.adapters.sqlite_session import SqliteSessionStore
from neuro_code.application.permissions.contracts import PermissionApproval, PermissionRequest
from neuro_code.application.ports.background_tasks import BackgroundTaskManager
from neuro_code.application.ports.tools import Tool, ToolContext
from neuro_code.application.ports.workspace_changes import (
    WorkspaceChangeCheckpoint,
    WorkspaceChangeReport,
    WorkspaceFileChange,
)
from neuro_code.application.runtime.agent import AgentRuntime
from neuro_code.domain.background_tasks import BackgroundTaskSnapshot, BackgroundTaskStatus
from neuro_code.domain.events import AgentEventKind
from neuro_code.domain.interaction_mode import InteractionMode
from neuro_code.domain.messages import (
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    ToolCall,
)
from neuro_code.domain.model_context import UPSTREAM_IMPORT_PROVIDER, ModelContext
from neuro_code.domain.model_events import (
    ModelBackendToolCompleted,
    ModelBackendToolStarted,
    ModelCompleted,
    ModelEvent,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.reasoning import ReasoningEffort
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.permissions import (
    PermissionEffect,
    PermissionManager,
    PermissionMode,
    PermissionRule,
)
from neuro_code.providers.failover import FailoverModelProvider, ProviderCandidate
from neuro_code.shared.errors import ProviderError
from neuro_code.tools import ToolRegistry, default_tool_registry
from neuro_code.tools.background_tasks import TaskOutputTool
from neuro_code.workspace_changes import FilesystemWorkspaceChangeObserver
from tests.fakes import EmptyWorkspaceChangeObserver


class ScriptedProvider:
    provider_name = "scripted"
    model_name = "fixture-model"
    context_affinity = "profile-v1:scripted"

    def __init__(self, scripts: Sequence[Sequence[ModelEvent]]) -> None:
        self._scripts = list(scripts)
        self.calls: list[ModelContext] = []
        self.tool_definitions: list[tuple[ToolDefinition, ...]] = []

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ModelEvent]:
        self.calls.append(context)
        self.tool_definitions.append(tuple(tools))
        script = self._scripts.pop(0)
        for event in script:
            yield event


class FailingProvider:
    def __init__(self, name: str) -> None:
        self.provider_name = name
        self.model_name = f"{name}-model"
        self.context_affinity = f"profile-v1:{name}"

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ModelEvent]:
        del context, tools
        raise ProviderError(f"{self.provider_name} unavailable")
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


class MinimalToolCollection:
    """A structural ToolCollection fixture with no registry-specific API."""

    def __init__(self, tools: Sequence[Tool]) -> None:
        self._tools = {tool.definition.name: tool for tool in tools}

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self._tools.values())


class FixtureWorkspaceChangeCheckpoint(WorkspaceChangeCheckpoint):
    """Opaque checkpoint used to prove the runtime does not need snapshots."""

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


class AgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
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
