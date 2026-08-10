from __future__ import annotations

import asyncio
import inspect
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import patch

from pygments.token import Keyword, Name, Number, String
from textual import events
from textual.containers import VerticalScroll
from textual.geometry import Size
from textual.widgets import Button, Input, Label, Static, TextArea
from textual.widgets.text_area import Selection

from neuro_code.application.permissions.broker import SessionApprovalBroker
from neuro_code.application.permissions.contracts import (
    PermissionApproval,
    PermissionApprovalKind,
    build_permission_request,
)
from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.application.ports.provider_catalog import (
    ProviderCatalogError,
    ProviderCatalogResult,
    ProviderConnectionSpec,
)
from neuro_code.application.ports.provider_settings import (
    ManagedProviderProfile,
    ManagedProviderSettings,
    ManagedProxyPolicy,
)
from neuro_code.application.ports.tools import ToolOutputArtifact, ToolOutputArtifactRead
from neuro_code.application.providers import ChangeProviderRequest, ProviderChangeService
from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.application.sessions import SessionTurnService
from neuro_code.application.sessions.profile_conversation import (
    InteractionModeSelectionResult,
    ProviderOption,
    ProviderSelectionResult,
    ReasoningEffortSelectionResult,
    SessionOption,
    SessionSelectionResult,
)
from neuro_code.application.sessions.selection import SessionSelectionService
from neuro_code.application.sessions.subagent_lifecycle import (
    SubagentRelationshipActionRequest,
    SubagentRelationshipActionResult,
    SubagentRelationshipLifecycleController,
)
from neuro_code.application.sessions.subagent_queries import (
    GetSubagentRelationshipRequest,
    ListSubagentRelationshipsRequest,
    SubagentRelationshipAction,
    SubagentRelationshipProjection,
    SubagentRelationshipQueryController,
)
from neuro_code.application.tools import ReadSessionToolOutputArtifactRequest
from neuro_code.application.workflows import (
    PlanExecutionService,
    PlanSchedulingService,
    QueuedPlanExecutionService,
    ReadOnlySubagentApplicationService,
    RunSubagentRequest,
    SubagentResultProjection,
)
from neuro_code.domain.background_tasks import (
    BackgroundTaskSnapshot,
    BackgroundTaskStatus,
    BackgroundTaskWakePolicy,
    BackgroundWakeLimits,
    BackgroundWakeState,
)
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
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
    SessionExecutionRecord,
    SupervisorReasonCode,
    TurnCancellationPolicy,
    TurnSource,
)
from neuro_code.domain.plans import PlanComment, PlanStep, PlanStepStatus, SessionPlan
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.session_tasks import SessionTask, SessionTaskKind, SessionTaskStatus
from neuro_code.domain.sessions import SessionSummary
from neuro_code.infrastructure.providers.provider_settings import JsonProviderSettingsStore
from neuro_code.interfaces.tui import recoverable_terminal_status
from neuro_code.shared.errors import ProviderError
from neuro_code.shared.ui_language import UiLanguage
from neuro_code.tui import (
    TUI_RELOAD_PROVIDER_SETTINGS,
    AssistantMarkdown,
    AssistantMessage,
    BackgroundWakeSettingsScreen,
    ConversationMessage,
    LanguageSettingsScreen,
    NetworkProxySettingsScreen,
    NeuroCodeApp,
    PermissionApprovalScreen,
    PromptInput,
    ProviderSelectionScreen,
    ProviderSettingsScreen,
    ProviderSetupApp,
    ReasoningEffortScreen,
    SessionSelectionScreen,
    SettingsScreen,
    ToolFeedbackMessage,
    TranscriptCopyScreen,
)
from neuro_code.tui_theme import (
    ACCENT_CODE,
    ACCENT_ERROR,
    ACCENT_LINK,
    ACCENT_NUMBER,
    ACCENT_SUCCESS,
    ACCENT_WARNING,
    BORDER_FOCUS,
    MONO_COLORS,
    MONO_SYNTAX_THEME,
    SURFACE_HOVER,
    SURFACE_SELECTED,
    TEXT_BODY,
    TEXT_EMPHASIS,
    TEXT_PLACEHOLDER,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)


class TuiConversation:
    def __init__(self) -> None:
        self._session_id: str | None = None
        self.prompts: list[str] = []

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
    ) -> AgentRunResult:
        del cancellation_policy
        self.prompts.append(prompt)
        self._session_id = "session-fixture"
        events = (
            AgentEvent.create(
                1,
                AgentEventKind.SESSION_STARTED,
                {"session_id": self._session_id},
            ),
            AgentEvent.create(2, AgentEventKind.REASONING_DELTA, {"text": "private"}),
            AgentEvent.create(
                3,
                AgentEventKind.MODEL_THINKING_COMPLETED,
                {"step": 1, "duration_seconds": 1.25},
            ),
            AgentEvent.create(
                4,
                AgentEventKind.TOOL_REQUESTED,
                {"id": "read", "name": "read_file", "arguments": {"path": "README.md"}},
            ),
            AgentEvent.create(
                5,
                AgentEventKind.TOOL_PERMISSION,
                {
                    "id": "read",
                    "name": "read_file",
                    "effect": "ask",
                    "reason": "fixture approval",
                },
            ),
            AgentEvent.create(
                6,
                AgentEventKind.TOOL_APPROVAL_REQUESTED,
                {
                    "id": "read",
                    "name": "read_file",
                    "summary": "private approval summary",
                    "reason": "fixture approval",
                },
            ),
            AgentEvent.create(
                7,
                AgentEventKind.TOOL_APPROVAL_RESOLVED,
                {
                    "id": "read",
                    "name": "read_file",
                    "effect": "allow",
                    "outcome": "allow_once",
                    "reason": "approved once",
                },
            ),
            AgentEvent.create(
                8,
                AgentEventKind.TOOL_COMPLETED,
                {
                    "id": "read",
                    "name": "read_file",
                    "content": "1\tNeuro Code project\n2\tPython agent",
                    "metadata": {"path": "/workspace/README.md", "total_lines": 2},
                    "duration_seconds": 0.42,
                },
            ),
            AgentEvent.create(9, AgentEventKind.TEXT_DELTA, {"text": "fixture "}),
            AgentEvent.create(10, AgentEventKind.TEXT_DELTA, {"text": "response"}),
            AgentEvent.create(
                11,
                AgentEventKind.TURN_COMPLETED,
                {"step": 1, "duration_seconds": 2.75},
            ),
        )
        if sink is not None:
            for event in events:
                outcome = sink(event)
                if inspect.isawaitable(outcome):
                    await outcome
        return AgentRunResult(
            self._session_id,
            "fixture response",
            (),
            (),
            events,
            1,
        )

    async def run_background_wake(self, *, sink: EventSink | None = None) -> AgentRunResult:
        return await self.run("background wake", sink=sink)


class ProviderFailureThenSuccessTuiConversation(TuiConversation):
    """Fail one provider request without invalidating the fixture session.

    让一次 Provider 请求失败,但不使 fixture 会话失效.
    """

    def __init__(self) -> None:
        super().__init__()
        self._fail_first_request = True

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
    ) -> AgentRunResult:
        if self._fail_first_request:
            self._fail_first_request = False
            self.prompts.append(prompt)
            self._session_id = "provider-failure-session"
            raise ProviderError("Responses API request failed with HTTP 402: insufficient balance")
        return await super().run(
            prompt,
            sink=sink,
            cancellation_policy=cancellation_policy,
        )


class TypedTuiConversation(TuiConversation):
    def __init__(self) -> None:
        super().__init__()
        self.turn_sources: list[TurnSource] = []

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        content_parts: tuple[ContentPart, ...] = (),
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
        turn_source: TurnSource = TurnSource.USER,
    ) -> AgentRunResult:
        del content_parts
        self.turn_sources.append(turn_source)
        return await super().run(
            prompt,
            sink=sink,
            cancellation_policy=cancellation_policy,
        )


class AutoWakeTuiConversation:
    def __init__(self) -> None:
        self._session_id = "auto-wake-session"
        self.wake_count = 0

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
    ) -> AgentRunResult:
        del prompt, sink, cancellation_policy
        raise AssertionError("auto-wake fixture must not start a user turn")

    async def run_background_wake(self, *, sink: EventSink | None = None) -> AgentRunResult:
        self.wake_count += 1
        events = (
            AgentEvent.create(
                1,
                AgentEventKind.BACKGROUND_TASK_AUTO_WAKE_STARTED,
                {"count": 1, "remaining_count": 0, "model_context_only": True},
            ),
            AgentEvent.create(
                2,
                AgentEventKind.BACKGROUND_TASK_COMPLETION_REMINDER,
                {"task_ids": ["task-fast"], "count": 1, "remaining_count": 0},
            ),
            AgentEvent.create(3, AgentEventKind.TEXT_DELTA, {"text": "wake response"}),
            AgentEvent.create(
                4,
                AgentEventKind.TURN_COMPLETED,
                {"step": 1, "duration_seconds": 0.1},
            ),
        )
        if sink is not None:
            for event in events:
                outcome = sink(event)
                if inspect.isawaitable(outcome):
                    await outcome
        return AgentRunResult(
            self._session_id,
            "wake response",
            (),
            (),
            events,
            1,
        )


class FailingAutoWakeTuiConversation(AutoWakeTuiConversation):
    async def run_background_wake(self, *, sink: EventSink | None = None) -> AgentRunResult:
        self.wake_count += 1
        reminder = AgentEvent.create(
            1,
            AgentEventKind.BACKGROUND_TASK_COMPLETION_REMINDER,
            {"task_ids": ["task-fast"], "count": 1, "remaining_count": 0},
        )
        if sink is not None:
            outcome = sink(reminder)
            if inspect.isawaitable(outcome):
                await outcome
        raise RuntimeError("wake provider failed")


class ApprovalTuiConversation:
    def __init__(self, broker: SessionApprovalBroker) -> None:
        self._broker = broker
        self._session_id: str | None = None
        self.approvals: list[PermissionApproval] = []
        self.executed = False

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
    ) -> AgentRunResult:
        del prompt, sink, cancellation_policy
        request = build_permission_request(
            "edit",
            "search_replace",
            {"path": "note.txt", "old": "private-old", "new": "private-new"},
            "interactive approval required",
        )
        approval = await self._broker.request(request)
        self.approvals.append(approval)
        self.executed = approval.allowed
        self._session_id = "approval-session"
        response = "approved" if approval.allowed else "denied"
        return AgentRunResult(self._session_id, response, (), (), (), 1)


class CancellableTuiConversation:
    def __init__(self) -> None:
        self._session_id: str | None = None
        self.prompts: list[str] = []
        self.started = asyncio.Event()
        self.cancelled = False

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
    ) -> AgentRunResult:
        del sink, cancellation_policy
        self.prompts.append(prompt)
        self._session_id = "cancel-session"
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


class PristineRewindTuiConversation:
    def __init__(self) -> None:
        self._session_id: str | None = None
        self.started = asyncio.Event()
        self.cancelled = False
        self.policies: list[TurnCancellationPolicy] = []

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
    ) -> AgentRunResult:
        del prompt
        self.policies.append(cancellation_policy)
        self._session_id = "pristine-rewind-session"
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            if sink is not None:
                event = AgentEvent.create(
                    1,
                    AgentEventKind.TURN_FAILED,
                    {
                        "cancelled": True,
                        "pristine_rewound": cancellation_policy
                        is TurnCancellationPolicy.REWIND_PRISTINE,
                    },
                )
                outcome = sink(event)
                if inspect.isawaitable(outcome):
                    await outcome
            raise
        raise AssertionError("unreachable")


class PreTokenInterjectionConversation:
    def __init__(self) -> None:
        self._session_id: str | None = None
        self.prompts: list[str] = []
        self.started: asyncio.Queue[str] = asyncio.Queue()
        self.release: asyncio.Queue[None] = asyncio.Queue()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
    ) -> AgentRunResult:
        del cancellation_policy
        self.prompts.append(prompt)
        self._session_id = "interjection-session"
        await self.started.put(prompt)
        await self.release.get()
        text = AgentEvent.create(
            len(self.prompts) * 2 - 1,
            AgentEventKind.TEXT_DELTA,
            {"text": f"response to {prompt}"},
        )
        completed = AgentEvent.create(
            len(self.prompts) * 2,
            AgentEventKind.TURN_COMPLETED,
            {"step": 1, "duration_seconds": 0.1},
        )
        if sink is not None:
            for event in (text, completed):
                outcome = sink(event)
                if inspect.isawaitable(outcome):
                    await outcome
        return AgentRunResult(
            self._session_id,
            f"response to {prompt}",
            (),
            (),
            (text, completed),
            1,
        )


class StreamingTuiConversation:
    def __init__(self) -> None:
        self._session_id: str | None = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
    ) -> AgentRunResult:
        del prompt, cancellation_policy
        self._session_id = "streaming-session"
        first = AgentEvent.create(1, AgentEventKind.TEXT_DELTA, {"text": "partial"})
        if sink is not None:
            outcome = sink(first)
            if inspect.isawaitable(outcome):
                await outcome
        self.started.set()
        await self.release.wait()
        second = AgentEvent.create(2, AgentEventKind.TEXT_DELTA, {"text": " response"})
        if sink is not None:
            outcome = sink(second)
            if inspect.isawaitable(outcome):
                await outcome
        return AgentRunResult(
            self._session_id,
            "partial response",
            (),
            (),
            (first, second),
            1,
        )


class FinalizingTuiConversation:
    def __init__(self, execution_status: str) -> None:
        self._execution_status = execution_status
        self._session_id: str | None = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
    ) -> AgentRunResult:
        del prompt, cancellation_policy
        self._session_id = "finalizing-session"
        finalizing = AgentEvent.create(
            1,
            AgentEventKind.FINALIZING_STARTED,
            {
                "execution_status": self._execution_status,
                "execution_reason": "model_step_limit",
                "recoverable": True,
            },
        )
        if sink is not None:
            outcome = sink(finalizing)
            if inspect.isawaitable(outcome):
                await outcome
        self.started.set()
        await self.release.wait()
        text = AgentEvent.create(2, AgentEventKind.TEXT_DELTA, {"text": "safe final text"})
        completed = AgentEvent.create(
            3,
            AgentEventKind.TURN_COMPLETED,
            {
                "step": 1,
                "duration_seconds": 0.25,
                "execution_status": self._execution_status,
                "recoverable": True,
            },
        )
        if sink is not None:
            for event in (text, completed):
                outcome = sink(event)
                if inspect.isawaitable(outcome):
                    await outcome
        return AgentRunResult(
            self._session_id,
            "safe final text",
            (),
            (),
            (finalizing, text, completed),
            1,
        )


class UnknownTerminalMetadataTuiConversation:
    @property
    def session_id(self) -> str | None:
        return "unknown-terminal-session"

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
    ) -> AgentRunResult:
        del prompt, cancellation_policy
        events = (
            AgentEvent.create(1, AgentEventKind.TEXT_DELTA, {"text": "ordinary text"}),
            AgentEvent.create(
                2,
                AgentEventKind.TURN_COMPLETED,
                {
                    "step": 1,
                    "duration_seconds": 0.25,
                    "execution_status": "unknown_terminal",
                    "recoverable": True,
                },
            ),
        )
        if sink is not None:
            for event in events:
                outcome = sink(event)
                if inspect.isawaitable(outcome):
                    await outcome
        return AgentRunResult("unknown-terminal-session", "ordinary text", (), (), events, 1)


class UiPreferencesFixture:
    def __init__(self) -> None:
        self.saved: list[UiLanguage] = []
        self.saved_efforts: list[ReasoningEffort] = []
        self.saved_modes: list[InteractionMode] = []

    async def load_language(self) -> UiLanguage:
        return UiLanguage.ENGLISH

    async def save_language(self, language: UiLanguage) -> None:
        self.saved.append(language)

    async def load_reasoning_effort(self) -> ReasoningEffort:
        return ReasoningEffort.HIGH

    async def save_reasoning_effort(self, effort: ReasoningEffort) -> None:
        self.saved_efforts.append(effort)

    async def load_interaction_mode(self) -> InteractionMode:
        return InteractionMode.NORMAL

    async def save_interaction_mode(self, mode: InteractionMode) -> None:
        self.saved_modes.append(mode)


class ProviderCatalogFixture:
    def __init__(
        self,
        result: ProviderCatalogResult | None = None,
        error: ProviderCatalogError | None = None,
    ) -> None:
        self.result = result or ProviderCatalogResult(())
        self.error = error
        self.calls: list[tuple[ProviderConnectionSpec, HttpClientPolicy]] = []

    async def discover_models(
        self,
        spec: ProviderConnectionSpec,
        *,
        http_policy: HttpClientPolicy,
    ) -> ProviderCatalogResult:
        self.calls.append((spec, http_policy))
        if self.error is not None:
            raise self.error
        return self.result


class BlockingProviderCatalogFixture(ProviderCatalogFixture):
    def __init__(self, result: ProviderCatalogResult) -> None:
        super().__init__(result)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def discover_models(
        self,
        spec: ProviderConnectionSpec,
        *,
        http_policy: HttpClientPolicy,
    ) -> ProviderCatalogResult:
        self.calls.append((spec, http_policy))
        self.started.set()
        await self.release.wait()
        return self.result


class ApprovalControllerFixture:
    def __init__(self) -> None:
        self.handlers: list[object | None] = []

    def set_handler(self, handler: object | None) -> None:
        self.handlers.append(handler)


class ProfileTuiController:
    def __init__(
        self,
        plan: SessionPlan | None = None,
        *,
        plan_comments: tuple[PlanComment, ...] = (),
        session_tasks: tuple[SessionTask, ...] = (),
    ) -> None:
        self._selected_profile = "first"
        self.selections: list[str] = []
        self.effort_selections: list[ReasoningEffort] = []
        self.mode_selections: list[InteractionMode] = []
        self.plan_execution_calls = 0
        self._reasoning_effort = ReasoningEffort.HIGH
        self._interaction_mode = InteractionMode.NORMAL
        self._plan = plan
        self._plan_comments = plan_comments
        self._session_tasks = session_tasks
        self._options = (
            ProviderOption(
                "first",
                "openai-chat",
                "first-model",
                True,
                True,
                default=True,
                context_window_tokens=1_000_000,
            ),
            ProviderOption(
                "second",
                "anthropic-messages",
                "second-model",
                True,
                True,
                context_window_tokens=200_000,
            ),
            ProviderOption("missing", "openai-chat", "missing-model", True, False),
        )

    @property
    def profiles(self) -> tuple[ProviderOption, ...]:
        return tuple(
            replace(option, selected=option.name == self._selected_profile)
            for option in self._options
        )

    @property
    def selected_profile(self) -> str:
        return self._selected_profile

    @property
    def reasoning_effort(self) -> ReasoningEffort:
        return self._reasoning_effort

    @property
    def effective_reasoning_effort(self) -> ReasoningEffort:
        return self._reasoning_effort.effective

    @property
    def interaction_mode(self) -> InteractionMode:
        return self._interaction_mode

    @property
    def auto_mode_unrestricted(self) -> bool:
        return False

    @property
    def plan(self) -> SessionPlan | None:
        return self._plan

    async def add_plan_comment(self, step_index: int, content: str) -> PlanComment:
        if self._plan is None:
            raise ValueError("cannot comment on a plan that has not been saved")
        comment = PlanComment(
            f"plan-comment-{len(self._plan_comments) + 1}",
            step_index,
            content,
            datetime.now(UTC),
        )
        self._plan_comments = (*self._plan_comments, comment)
        return comment

    async def list_plan_comments(self) -> tuple[PlanComment, ...]:
        return self._plan_comments

    async def set_reasoning_effort(
        self,
        effort: ReasoningEffort,
    ) -> ReasoningEffortSelectionResult:
        changed = effort is not self._reasoning_effort
        self.effort_selections.append(effort)
        self._reasoning_effort = effort
        return ReasoningEffortSelectionResult(
            requested=effort,
            effective=effort.effective,
            changed=changed,
        )

    async def set_interaction_mode(
        self,
        mode: InteractionMode,
    ) -> InteractionModeSelectionResult:
        changed = mode is not self._interaction_mode
        self.mode_selections.append(mode)
        self._interaction_mode = mode
        return InteractionModeSelectionResult(
            requested=mode,
            changed=changed,
            auto_unrestricted=False,
        )

    async def schedule_plan(self) -> SessionTask:
        if self._plan is None:
            raise ValueError("cannot schedule a plan that has not been saved")
        task = SessionTask(
            f"task-queued-{len(self._session_tasks) + 1}",
            SessionTaskKind.PLAN_EXECUTION,
            SessionTaskStatus.QUEUED,
            datetime.now(UTC),
            plan_snapshot=self._plan,
        )
        self._session_tasks = (*self._session_tasks, task)
        return task

    async def execute_plan(
        self,
        *,
        sink: EventSink | None = None,
        task_id: str | None = None,
    ) -> AgentRunResult:
        if self._plan is None:
            raise ValueError("cannot execute a plan that has not been saved")
        del task_id
        self.plan_execution_calls += 1
        events = (
            AgentEvent.create(
                1,
                AgentEventKind.PLAN_EXECUTION_REQUESTED,
                {"plan": self._plan.to_dict()},
            ),
            AgentEvent.create(
                2,
                AgentEventKind.TURN_COMPLETED,
                {"step": 1, "duration_seconds": 0.25},
            ),
        )
        if sink is not None:
            for event in events:
                outcome = sink(event)
                if inspect.isawaitable(outcome):
                    await outcome
        return AgentRunResult(
            "plan-session",
            "plan execution response",
            (),
            (),
            events,
            1,
            self._plan,
        )

    async def run_session_task(
        self,
        task_id: str,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult:
        task = await self.get_session_task(task_id)
        if task is None or task.status is not SessionTaskStatus.QUEUED:
            raise ValueError("queued task is unavailable")
        started_at = datetime.now(UTC)
        running = task.start(started_at=started_at)
        self._session_tasks = tuple(
            running if item.task_id == task_id else item for item in self._session_tasks
        )
        result = await self.execute_plan(sink=sink, task_id=task_id)
        finished_at = datetime.now(UTC)
        completed = running.finish(SessionTaskStatus.COMPLETED, finished_at=finished_at)
        self._session_tasks = tuple(
            completed if item.task_id == task_id else item for item in self._session_tasks
        )
        return result

    async def list_session_tasks(self) -> tuple[SessionTask, ...]:
        return self._session_tasks

    async def get_session_task(self, task_id: str) -> SessionTask | None:
        return next((task for task in self._session_tasks if task.task_id == task_id), None)

    async def select_profile(self, name: str) -> ProviderSelectionResult:
        self.selections.append(name)
        changed = name != self._selected_profile
        self._selected_profile = name
        model = next(option.model for option in self._options if option.name == name)
        context_window_tokens = next(
            option.context_window_tokens for option in self._options if option.name == name
        )
        return ProviderSelectionResult(
            name,
            name,
            model,
            "old-session" if changed else None,
            changed,
            context_window_tokens=context_window_tokens,
        )

    async def change_provider(self, request: ChangeProviderRequest) -> ProviderSelectionResult:
        return await self.select_profile(request.profile_name)


class FailingSessionTaskController:
    async def get_session_task(self, task_id: str) -> SessionTask | None:
        del task_id
        raise RuntimeError("task read failed")


def restored_history() -> tuple[SessionItem, ...]:
    return (
        Message(
            Role.USER,
            content_parts=(
                ContentPart.from_text("restored prompt"),
                ContentPart.from_image("data:image/png;base64,private-image"),
            ),
        ),
        PreservedContextItem(
            ContextItemKind.REASONING,
            {
                "type": "reasoning",
                "id": "private-reasoning",
                "summary": [{"type": "summary_text", "text": "never render this"}],
            },
        ),
        Message(
            Role.ASSISTANT,
            tool_calls=(ToolCall("read-1", "read_file", {"path": "private.txt"}),),
            reasoning_content="private assistant reasoning",
        ),
        Message(
            Role.TOOL,
            "private tool output",
            name="read_file",
            tool_call_id="read-1",
        ),
        Message(Role.ASSISTANT, "restored response"),
    )


class SessionTuiController:
    def __init__(self, *, current_session: str = "current-session") -> None:
        self._session_id = current_session
        self.selected: list[str] = []
        self.renamed: list[str] = []
        self.queries: list[str | None] = []
        timestamp = datetime(2026, 7, 18, 9, 30, tzinfo=UTC)
        self.options = (
            SessionOption(
                "current-session",
                "first",
                "first-model",
                timestamp,
                "first",
                current_session == "current-session",
                True,
                True,
                title="Current workspace session",
            ),
            SessionOption(
                "target-session-123456789",
                "second",
                "second-model",
                timestamp,
                "second",
                current_session == "target-session-123456789",
                True,
                True,
                title="Escaped quoted session",
                matched_fields=("title", "content"),
                snippet="[quoted] content from the restored conversation",
            ),
        )

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
    ) -> AgentRunResult:
        del prompt, sink, cancellation_policy
        return AgentRunResult(self._session_id, "ok", (), (), (), 1)

    async def list_sessions(self, query: str | None = None) -> tuple[SessionOption, ...]:
        self.queries.append(query)
        if query is not None:
            return tuple(
                option
                for option in self.options
                if query.casefold() in f"{option.title or ''} {option.snippet or ''}".casefold()
            )
        return self.options

    async def select_session(self, session_id: str) -> SessionSelectionResult:
        self.selected.append(session_id)
        previous = self._session_id
        changed = session_id != previous
        self._session_id = session_id
        return SessionSelectionResult(
            session_id,
            "second",
            "second-model",
            "second",
            "second",
            "second-model",
            previous if changed else None,
            changed,
            True,
            restored_history(),
        )

    async def rename_session(self, title: str) -> SessionSummary:
        self.renamed.append(title)
        timestamp = datetime(2026, 7, 18, 9, 30, tzinfo=UTC)
        return SessionSummary(
            self._session_id,
            "/workspace",
            "first",
            "first-model",
            timestamp,
            timestamp,
            title=title,
        )


class TaskTuiController:
    def __init__(self, snapshots: tuple[BackgroundTaskSnapshot, ...] = ()) -> None:
        self.snapshots = snapshots
        self.list_calls = 0
        self.wake_state = BackgroundWakeState()

    async def list_background_tasks(self) -> tuple[BackgroundTaskSnapshot, ...]:
        self.list_calls += 1
        return self.snapshots

    async def load_background_wake_state(self) -> BackgroundWakeState:
        return self.wake_state

    async def save_background_wake_state(self, state: BackgroundWakeState) -> None:
        self.wake_state = state


class FailingTaskTuiController:
    async def list_background_tasks(self) -> tuple[BackgroundTaskSnapshot, ...]:
        raise RuntimeError("task list failed")

    async def load_background_wake_state(self) -> BackgroundWakeState:
        raise RuntimeError("wake state failed")

    async def save_background_wake_state(self, state: BackgroundWakeState) -> None:
        del state


class ReadOnlySubagentTuiService:
    def __init__(self, projection: SubagentResultProjection) -> None:
        self.projection = projection
        self.requests: list[RunSubagentRequest] = []

    async def run_subagent(self, request: RunSubagentRequest) -> SubagentResultProjection:
        self.requests.append(request)
        return self.projection


class SubagentRelationshipTuiService:
    def __init__(self, projections: tuple[SubagentRelationshipProjection, ...]) -> None:
        self.projections = projections
        self.requests: list[str] = []

    async def list_subagent_relationships(
        self,
        request: ListSubagentRelationshipsRequest,
    ) -> tuple[SubagentRelationshipProjection, ...]:
        self.requests.append(request.parent_session_id)
        return self.projections

    async def get_subagent_relationship(
        self,
        request: GetSubagentRelationshipRequest,
    ) -> SubagentRelationshipProjection | None:
        del request
        return self.projections[0] if self.projections else None


class FailingSubagentRelationshipTuiService:
    async def list_subagent_relationships(
        self,
        request: ListSubagentRelationshipsRequest,
    ) -> tuple[SubagentRelationshipProjection, ...]:
        del request
        raise RuntimeError("relationship list failed")

    async def get_subagent_relationship(
        self,
        request: GetSubagentRelationshipRequest,
    ) -> SubagentRelationshipProjection | None:
        del request
        raise RuntimeError("relationship lookup failed")


class SubagentRelationshipLifecycleTuiService:
    def __init__(self) -> None:
        self.requests: list[SubagentRelationshipActionRequest] = []

    async def execute(
        self,
        request: SubagentRelationshipActionRequest,
    ) -> SubagentRelationshipActionResult:
        self.requests.append(request)
        return SubagentRelationshipActionResult(
            parent_session_id=request.parent_session_id,
            parent_task_id=request.parent_task_id,
            child_session_id="child-session",
            action=request.action,
            forked_session_id=("forked-session" if request.action.value == "fork" else None),
        )


def background_snapshot(
    task_id: str,
    status: BackgroundTaskStatus,
    *,
    exit_code: int | None = None,
    completion_reported: bool = False,
) -> BackgroundTaskSnapshot:
    started_at = datetime(2026, 7, 18, 9, 30, tzinfo=UTC)
    return BackgroundTaskSnapshot(
        task_id=task_id,
        command="curl -H 'secret: private-command' https://example.invalid",
        cwd="/workspace",
        status=status,
        output="private task output",
        total_output_bytes=19,
        truncated=False,
        exit_code=exit_code,
        started_at=started_at,
        finished_at=started_at if status.terminal else None,
        completion_reported=completion_reported,
    )


class NeuroCodeAppTests(unittest.IsolatedAsyncioTestCase):
    def test_recoverable_terminal_status_projection_is_fail_closed(self) -> None:
        self.assertEqual(
            recoverable_terminal_status({"execution_status": "stuck", "recoverable": True}),
            AgentExecutionStatus.STUCK,
        )
        self.assertEqual(
            recoverable_terminal_status(
                {"execution_status": "budget_limited", "recoverable": True}
            ),
            AgentExecutionStatus.BUDGET_LIMITED,
        )
        for data in (
            {"execution_status": "stuck", "recoverable": False},
            {"execution_status": "completed", "recoverable": True},
            {"execution_status": "unknown", "recoverable": True},
            {"execution_status": 1, "recoverable": True},
        ):
            with self.subTest(data=data):
                self.assertIsNone(recoverable_terminal_status(data))

    async def test_plan_queue_commands_report_unavailable_and_failed_paths(self) -> None:
        """The explicit queue surface fails closed when its collaborators are absent.

        验证显式队列接口在协作者缺失时会失败关闭."""

        unavailable = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )
        async with unavailable.run_test(size=(80, 24)):
            await unavailable._schedule_plan()
            await unavailable._run_queued_task("missing")
            self.assertIn("unavailable", unavailable.entries[-1].text.lower())

        no_plan = ProfileTuiController()
        no_plan_app = NeuroCodeApp(
            TuiConversation(),
            plan_controller=no_plan,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )
        async with no_plan_app.run_test(size=(80, 24)):
            await no_plan_app._schedule_plan()
            self.assertIn("No structured plan", no_plan_app.entries[-1].text)

        class FailingScheduler(ProfileTuiController):
            async def schedule_plan(self) -> SessionTask:
                raise RuntimeError("schedule failed")

        failing = FailingScheduler(SessionPlan((PlanStep("queued"),)))
        failing_app = NeuroCodeApp(
            TuiConversation(),
            plan_controller=failing,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )
        async with failing_app.run_test(size=(80, 24)):
            await failing_app._schedule_plan()
            self.assertIn("RuntimeError: schedule failed", failing_app.entries[-1].text)

        no_task_controller = ProfileTuiController(SessionPlan((PlanStep("queued"),)))
        no_task_app = NeuroCodeApp(
            TuiConversation(),
            plan_controller=no_task_controller,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )
        async with no_task_app.run_test(size=(80, 24)):
            await no_task_app._run_queued_task("missing")
            self.assertIn("unavailable", no_task_app.entries[-1].text.lower())

        failing_task_app = NeuroCodeApp(
            TuiConversation(),
            plan_controller=ProfileTuiController(SessionPlan((PlanStep("queued"),))),
            session_task_controller=FailingSessionTaskController(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )
        async with failing_task_app.run_test(size=(80, 24)):
            await failing_task_app._run_queued_task("task-failing")
            self.assertIn("RuntimeError: task read failed", failing_task_app.entries[-1].text)

        queued_controller = ProfileTuiController(SessionPlan((PlanStep("queued"),)))
        queued = await queued_controller.schedule_plan()
        no_mode_app = NeuroCodeApp(
            TuiConversation(),
            plan_controller=queued_controller,
            session_task_controller=queued_controller,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )
        async with no_mode_app.run_test(size=(80, 24)):
            await no_mode_app._run_queued_task(queued.task_id)
            self.assertIn("unavailable", no_mode_app.entries[-1].text.lower())

    async def test_local_quit_skips_the_model_and_detaches_approval_handler(self) -> None:
        runner = TuiConversation()
        approvals = ApprovalControllerFixture()
        app = NeuroCodeApp(
            runner,
            approval_controller=approvals,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            self.assertTrue(approvals.handlers)
            self.assertIsNotNone(approvals.handlers[-1])
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/quit"
            await pilot.press("enter")

        self.assertEqual(runner.prompts, [])
        self.assertEqual(app.return_code, 0)
        self.assertIsNone(approvals.handlers[-1])

    async def test_prompt_copy_and_paste_are_not_intercepted_by_cancel_binding(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "copy this prompt"
            prompt.selection = Selection((0, 0), (0, 9))

            await pilot.press("ctrl+c")

            self.assertEqual(app.clipboard, "copy this")
            self.assertEqual(prompt.value, "copy this prompt")
            self.assertNotIn("Cancellation requested.", [entry.text for entry in app.entries])

            prompt.selection = Selection.cursor((0, len(prompt.value)))
            await pilot.press("ctrl+v")

            self.assertEqual(prompt.value, "copy this promptcopy this")

    async def test_prompt_ctrl_shift_c_copies_a_selection_explicitly(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "selected text"
            prompt.selection = Selection((0, 0), (0, 8))

            await pilot.press("ctrl+shift+c")

            self.assertEqual(app.clipboard, "selected")
            self.assertEqual(prompt.value, "selected text")

    async def test_transcript_copy_screen_supports_arbitrary_selection(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(90, 28)) as pilot:
            app._write_entry("assistant", "First line\nSecond line")
            await pilot.press("f8")
            await pilot.pause()

            self.assertIsInstance(app.screen, TranscriptCopyScreen)
            editor = app.screen.query_one("#transcript-copy-text", TextArea)
            second_line = editor.text.splitlines().index("Second line")
            editor.selection = Selection((second_line, 0), (second_line, 6))
            await pilot.press("ctrl+c")

            self.assertEqual(app.clipboard, "Second")
            self.assertIn(
                "6",
                str(app.screen.query_one("#transcript-copy-status", Label).renderable),
            )
            await pilot.press("escape")
            await pilot.pause()
            self.assertIs(app.screen, app.screen_stack[0])

    async def test_prompt_paste_preserves_all_lines(self) -> None:
        runner = TuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.post_message(events.Paste("first line\nsecond line\r\nthird line"))
            await pilot.pause()

            self.assertEqual(prompt.value, "first line\nsecond line\nthird line")
            self.assertEqual(prompt.text.splitlines(), ["first line", "second line", "third line"])
            self.assertGreater(prompt.region.height, 1)
            self.assertLessEqual(prompt.region.height, 8)

            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if app.entries and any(entry.category == "assistant" for entry in app.entries):
                    break
            self.assertEqual(runner.prompts, ["first line\nsecond line\nthird line"])

    async def test_prompt_common_editing_shortcuts_select_all_and_insert_newline(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "first line"
            prompt.cursor_position = len(prompt.value)
            await pilot.press("shift+enter")
            await pilot.press("s", "e", "c", "o", "n", "d")
            self.assertEqual(prompt.value, "first line\nsecond")

            await pilot.press("ctrl+a")
            self.assertEqual(prompt.selected_text, "first line\nsecond")
            await pilot.press("r")
            self.assertEqual(prompt.value, "r")

    async def test_monochrome_theme_uses_the_compact_custom_chrome(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()

            self.assertEqual(app.theme, "neuro-code-mono")
            self.assertEqual(app.screen.styles.background.hex.lower(), "#0c0c0c")
            self.assertEqual(BORDER_FOCUS, "#BDBDBD")
            self.assertNotIn("#F0F0F0", NeuroCodeApp.CSS)
            self.assertIn("background: $text-muted", NeuroCodeApp.CSS)
            self.assertFalse(app.ENABLE_COMMAND_PALETTE)
            self.assertIn("NEURO / CODE", str(app.query_one("#brand", Static).renderable))
            self.assertEqual(str(app.query_one("#clock", Static).renderable).count(":"), 1)
            prompt = app.query_one("#prompt", PromptInput)
            placeholder_segments = list(app.console.render(prompt.get_line(0)))
            self.assertIn(
                TEXT_PLACEHOLDER.lower(),
                str(placeholder_segments[0].style).lower(),
            )
            self.assertEqual(len(list(app.query("#header"))), 1)
            self.assertEqual(len(list(app.query("#prompt-row"))), 1)
            self.assertEqual(len(list(app.query("#prompt-mark"))), 1)
            self.assertEqual(
                app.query_one("#prompt-mark", Static).renderable,
                "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}",
            )
            for color in MONO_COLORS:
                red, green, blue = (int(color[index : index + 2], 16) for index in (1, 3, 5))
                self.assertEqual((red, green, blue), (red, red, red))
            entry_styles = {
                str(app._render_entry(category, "content").style)
                for category in ("assistant", "system", "tool", "user")
            }
            self.assertFalse(
                any(
                    color in style
                    for style in entry_styles
                    for color in ("cyan", "green", "magenta", "yellow")
                )
            )
            self.assertFalse(
                any(
                    accent in NeuroCodeApp.CSS
                    for accent in (
                        ACCENT_CODE,
                        ACCENT_LINK,
                        ACCENT_NUMBER,
                        ACCENT_SUCCESS,
                        ACCENT_WARNING,
                        ACCENT_ERROR,
                    )
                )
            )

    async def test_user_and_assistant_messages_use_distinct_unlabelled_blocks(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "inspect the repository"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if any(entry.category == "assistant" for entry in app.entries):
                    break

            messages = list(app.query(ConversationMessage))
            user = next(message for message in messages if message.category == "user")
            assistant = next(message for message in messages if message.category == "assistant")
            user_text = str(user.renderable)
            assistant_text = str(assistant.renderable)
            self.assertTrue(user.has_class("message-user"))
            self.assertTrue(assistant.has_class("message-assistant"))
            self.assertTrue(user_text.startswith("inspect the repository"))
            self.assertIn("fixture response", assistant_text)
            self.assertNotIn("You:", user_text)
            self.assertNotIn("Assistant:", assistant_text)

    async def test_assistant_markdown_uses_semantic_styles_without_markup_injection(
        self,
    ) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)):
            rendered = app._render_entry(
                "assistant",
                "## Important\n\nUse **bold** and `code`.\n\n- [red]literal[/red]",
            )
            self.assertIsInstance(rendered, AssistantMarkdown)
            segments = list(app.console.render(rendered, app.console.options.update(width=80)))
            plain = "".join(segment.text for segment in segments)
            styled = {
                segment.text.strip(): str(segment.style)
                for segment in segments
                if segment.text.strip()
            }

            self.assertIn("Important", plain)
            self.assertIn("bold", plain)
            self.assertIn("code", plain)
            self.assertIn("[red]literal[/red]", plain)
            self.assertNotIn("**bold**", plain)
            self.assertIn(TEXT_EMPHASIS.lower(), styled["Important"].lower())
            self.assertIn(TEXT_PRIMARY.lower(), styled["bold"].lower())
            self.assertIn(ACCENT_CODE.lower(), styled["code"].lower())

    async def test_fenced_code_blocks_keep_the_custom_semantic_syntax_theme(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)):
            rendered = app._render_entry(
                "assistant",
                '```python\nclass Sample:\n    return "ok", 42\n```',
            )
            self.assertIsInstance(rendered, AssistantMarkdown)
            self.assertIs(rendered.code_theme, MONO_SYNTAX_THEME)
            plain = "".join(
                segment.text
                for segment in app.console.render(rendered, app.console.options.update(width=80))
            )
            self.assertIn("Sample", plain)
            self.assertIn(
                ACCENT_LINK.lower(),
                str(MONO_SYNTAX_THEME.get_style_for_token(Keyword)).lower(),
            )
            self.assertIn(
                ACCENT_CODE.lower(),
                str(MONO_SYNTAX_THEME.get_style_for_token(Name.Function)).lower(),
            )
            self.assertIn(
                ACCENT_SUCCESS.lower(),
                str(MONO_SYNTAX_THEME.get_style_for_token(String)).lower(),
            )
            self.assertIn(
                TEXT_EMPHASIS.lower(),
                str(MONO_SYNTAX_THEME.get_style_for_token(Number)).lower(),
            )

    async def test_markdown_prose_remains_neutral_gray(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)):
            rendered = app._render_entry("assistant", "Ordinary prose stays neutral.")
            segments = list(app.console.render(rendered, app.console.options.update(width=80)))
            prose = next(segment for segment in segments if "Ordinary prose" in segment.text)
            self.assertIn(TEXT_BODY.lower(), str(prose.style).lower())
            self.assertNotIn(ACCENT_CODE.lower(), str(prose.style).lower())

    async def test_success_warning_and_error_statuses_use_distinct_semantic_accents(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            app = ProviderSetupApp(
                provider_settings=ManagedProviderSettings(),
                provider_settings_store=JsonProviderSettingsStore(Path(directory)),
            )

            async with app.run_test(size=(100, 30)) as pilot:
                for _ in range(20):
                    await pilot.pause(0.01)
                    if isinstance(app.screen, ProviderSettingsScreen):
                        break
                screen = app.screen
                self.assertIsInstance(screen, ProviderSettingsScreen)
                status = screen.query_one("#provider-settings-connection-status", Static)
                for kind, marker, accent in (
                    ("success", "✓", ACCENT_SUCCESS),
                    ("warning", "!", ACCENT_WARNING),
                    ("error", "\N{MULTIPLICATION SIGN}", ACCENT_ERROR),
                ):
                    screen._show_connection_status("fixture status", kind=kind)
                    rendered = status.renderable
                    self.assertIn(marker, str(rendered))
                    self.assertIn(accent.lower(), str(rendered.style).lower())

    async def test_tool_notice_highlights_the_tool_name_in_an_aligned_gutter(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(80, 24)):
            rendered = app._render_entry(
                "tool",
                "Tool read_file completed.",
                ui_key="tool.completed",
                ui_values=(("name", "read_file"),),
            )
            segments = list(app.console.render(rendered, app.console.options.update(width=60)))
            tool_segments = [segment for segment in segments if "read_file" in segment.text]

            self.assertTrue(tool_segments)
            self.assertIn(ACCENT_CODE.lower(), str(tool_segments[0].style).lower())

    async def test_streaming_response_updates_one_stable_transcript_node(self) -> None:
        runner = StreamingTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "stream the answer"
            await pilot.press("enter")
            await asyncio.wait_for(runner.started.wait(), timeout=1)
            await pilot.pause()

            transcript = app.query_one("#transcript", VerticalScroll)
            pending = app._pending_assistant
            assert pending is not None
            child_count = len(transcript.children)
            self.assertIs(transcript.children[-1], pending)
            self.assertEqual(prompt.value, "")
            self.assertIn("partial", str(pending.renderable))
            self.assertEqual(list(app.query("#stream")), [])

            runner.release.set()
            for _ in range(20):
                await pilot.pause(0.01)
                if any(entry.category == "assistant" for entry in app.entries):
                    break

            self.assertEqual(len(transcript.children), child_count)
            self.assertIs(app._entry_widgets[-1], pending)
            self.assertIs(pending.parent, transcript)
            self.assertIn("partial response", str(pending.renderable))

    async def test_user_turn_uses_typed_application_turn_service_when_bound(self) -> None:
        runner = TypedTuiConversation()
        app = NeuroCodeApp(
            runner,
            turn_service=SessionTurnService(runner),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "run through the application seam"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if any(entry.category == "assistant" for entry in app.entries):
                    break

        self.assertEqual(runner.prompts, ["run through the application seam"])
        self.assertEqual(runner.turn_sources, [TurnSource.USER])

    async def test_finalizing_and_recoverable_terminal_states_preserve_the_final_response(
        self,
    ) -> None:
        messages = {
            "stuck": "The task entered a repeated loop and was stopped safely.",
            "budget_limited": "This turn reached its execution budget and was stopped safely.",
        }
        for execution_status, expected in messages.items():
            with self.subTest(execution_status=execution_status):
                runner = FinalizingTuiConversation(execution_status)
                app = NeuroCodeApp(
                    runner,
                    provider_name="fixture",
                    model_name="fixture-model",
                    cwd=Path("/workspace"),
                )

                async with app.run_test(size=(100, 30)) as pilot:
                    prompt = app.query_one("#prompt", PromptInput)
                    prompt.value = "finish safely"
                    await pilot.press("enter")
                    await asyncio.wait_for(runner.started.wait(), timeout=1)
                    await pilot.pause()

                    pending = app._pending_assistant
                    assert pending is not None
                    activity = app.query_one("#turn-activity", Static)
                    self.assertIn("Safely finalizing", str(activity.renderable))
                    self.assertNotIn("Safely finalizing", str(pending.renderable))
                    self.assertTrue(pending.has_class("message-pending"))

                    runner.release.set()
                    for _ in range(20):
                        await pilot.pause(0.01)
                        if any(entry.category == "recoverable" for entry in app.entries):
                            break

                    self.assertIn(
                        "safe final text",
                        [entry.text for entry in app.entries if entry.category == "assistant"],
                    )
                    recoverable = app.entries[-1]
                    self.assertEqual(recoverable.category, "recoverable")
                    self.assertIn(expected, recoverable.text)
                    self.assertFalse(any(entry.category == "error" for entry in app.entries))
                    widget = app._entry_widgets[-1]
                    self.assertTrue(widget.has_class("message-recoverable"))
                    self.assertFalse(prompt.disabled)
                    prompt.value = "continue with new instructions"
                    self.assertEqual(prompt.value, "continue with new instructions")

    async def test_unknown_terminal_metadata_falls_back_to_normal_completion(self) -> None:
        app = NeuroCodeApp(
            UnknownTerminalMetadataTuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "complete normally"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if any(entry.ui_key == "turn.completed" for entry in app.entries):
                    break

            self.assertIn("ordinary text", [entry.text for entry in app.entries])
            self.assertTrue(any(entry.ui_key == "turn.completed" for entry in app.entries))
            self.assertFalse(any(entry.category == "recoverable" for entry in app.entries))
            self.assertFalse(any(entry.category == "error" for entry in app.entries))
            self.assertFalse(prompt.disabled)

    async def test_resumed_recoverable_execution_record_shows_one_safe_notice(self) -> None:
        for status, reason_code, key in (
            (
                AgentExecutionStatus.STUCK,
                SupervisorReasonCode.REPEATED_ACTION_OBSERVATION,
                "session.stuck_recoverable",
            ),
            (
                AgentExecutionStatus.BUDGET_LIMITED,
                SupervisorReasonCode.MODEL_STEP_LIMIT,
                "session.budget_limited_recoverable",
            ),
        ):
            with self.subTest(status=status):
                runner = TuiConversation()
                runner._session_id = "session-resumed"
                record = SessionExecutionRecord(
                    outcome=AgentExecutionOutcome(
                        status=status,
                        reason_code=reason_code,
                        finalized=True,
                        recoverable=True,
                    ),
                    event_sequence=4,
                    completed_at=datetime.now(UTC),
                )
                app = NeuroCodeApp(
                    runner,
                    execution_record=record,
                    provider_name="fixture",
                    model_name="fixture-model",
                    cwd=Path("/workspace"),
                )

                async with app.run_test(size=(100, 30)):
                    notices = [entry for entry in app.entries if entry.ui_key == key]
                    self.assertEqual(len(notices), 1)
                    self.assertEqual(notices[0].category, "recoverable")
                    self.assertFalse(any(entry.category == "error" for entry in app.entries))

    async def test_resumed_completed_execution_record_has_no_recoverable_notice(self) -> None:
        runner = TuiConversation()
        runner._session_id = "session-resumed"
        record = SessionExecutionRecord(
            outcome=AgentExecutionOutcome(
                status=AgentExecutionStatus.COMPLETED,
                reason_code=None,
                finalized=False,
                recoverable=False,
            ),
            event_sequence=4,
            completed_at=datetime.now(UTC),
        )
        app = NeuroCodeApp(
            runner,
            execution_record=record,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)):
            self.assertFalse(
                any(
                    entry.ui_key
                    in {
                        "session.stuck_recoverable",
                        "session.budget_limited_recoverable",
                    }
                    for entry in app.entries
                )
            )

    async def test_waiting_model_uses_the_supplied_collapsing_pulse(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)):
            app._begin_pending_assistant()
            pending = app._pending_assistant
            assert pending is not None
            first_frame = str(pending.renderable)
            self.assertEqual(first_frame, "")
            self.assertFalse(pending.display)
            activity = app.query_one("#turn-activity", Static)
            self.assertTrue(activity.display)
            self.assertIn("Working", str(activity.renderable))
            self.assertTrue(any(symbol in str(activity.renderable) for symbol in "▁▂▃▄▅▆▇█"))
            loading_segments = list(
                app.console.render(activity.renderable, app.console.options.update(width=80))
            )
            self.assertIn(
                TEXT_SECONDARY.lower(),
                str(
                    next(segment.style for segment in loading_segments if "Working" in segment.text)
                ).lower(),
            )
            first_activity_frame = str(activity.renderable)

            app._advance_model_loading_animation()
            app._advance_model_loading_animation()

            second_frame = str(activity.renderable)
            self.assertNotEqual(first_activity_frame, second_frame)
            self.assertNotIn("█", str(app.query_one("#runtime-model", Static).renderable))
            await app._discard_pending_assistant()
            self.assertFalse(app._model_loading)

    async def test_settings_switches_and_persists_the_interface_language(self) -> None:
        preferences = UiPreferencesFixture()
        app = NeuroCodeApp(
            TuiConversation(),
            ui_preferences=preferences,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            app._write_entry("assistant", "literal model response")
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/setting"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, SettingsScreen):
                    break

            self.assertIsInstance(app.screen, SettingsScreen)
            self.assertEqual(list(app.screen.query("#provider-settings-form")), [])
            self.assertEqual(list(app.screen.query("#settings-languages")), [])
            clicked = await pilot.click("#settings-category-language")
            self.assertTrue(clicked)
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, LanguageSettingsScreen):
                    break

            self.assertIsInstance(app.screen, LanguageSettingsScreen)
            clicked = await pilot.click("#settings-language-zh-cn")
            self.assertTrue(clicked)
            for _ in range(20):
                await pilot.pause(0.01)
                if preferences.saved:
                    break

            self.assertEqual(preferences.saved, [UiLanguage.SIMPLIFIED_CHINESE])
            self.assertEqual(app.sub_title, "终端编程智能体")
            self.assertIn("输入 /help", prompt.placeholder)
            self.assertIn("设置", str(app.query_one("#shortcut-bar", Static).renderable))
            self.assertTrue(app.entries[0].text.startswith("已就绪"))
            self.assertIn(
                "literal model response",
                [entry.text for entry in app.entries if entry.category == "assistant"],
            )
            self.assertIn("界面语言已切换", app.entries[-1].text)

            prompt.value = "/status"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("供应商", app.entries[-1].text)
            self.assertIn("fixture/fixture-model", app.entries[-1].text)

    async def test_first_run_settings_save_a_provider_without_echoing_its_key(self) -> None:
        self.assertEqual(
            ProviderSettingsScreen._preset_for_profile(
                ManagedProviderProfile(
                    name="legacy-wrong-protocol",
                    protocol="openai-responses",
                    model="deepseek-v4-pro",
                    base_url="https://api.deepseek.com/v1",
                )
            ),
            "openai",
        )
        self.assertEqual(
            ProviderSettingsScreen._preset_for_profile(
                ManagedProviderProfile(
                    name="deepseek-explicit",
                    protocol="openai-chat",
                    dialect="deepseek-v4",
                    model="fixture-model",
                    base_url="https://proxy.invalid/v1",
                )
            ),
            "deepseek",
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            store = JsonProviderSettingsStore(Path(directory))
            app = ProviderSetupApp(
                provider_settings=ManagedProviderSettings(),
                provider_settings_store=store,
            )

            async with app.run_test(size=(110, 40)) as pilot:
                for _ in range(20):
                    await pilot.pause(0.01)
                    if isinstance(app.screen, ProviderSettingsScreen):
                        break
                self.assertIsInstance(app.screen, ProviderSettingsScreen)
                clicked = await pilot.click("#provider-settings-preset-deepseek")
                self.assertTrue(clicked)
                self.assertEqual(
                    app.screen.query_one("#provider-settings-base-url", Input).value,
                    "https://api.deepseek.com",
                )
                self.assertIn(
                    "/chat/completions",
                    str(
                        app.screen.query_one(
                            "#provider-settings-protocol-hint",
                            Static,
                        ).renderable
                    ),
                )
                app.screen.query_one("#provider-settings-name", Input).value = "personal"
                app.screen.query_one("#provider-settings-model", Input).value = "deepseek-v4-pro"
                api_key = app.screen.query_one("#provider-settings-api-key", Input)
                api_key.value = "never-echo-this-key"
                self.assertTrue(api_key.password)

                clicked = await pilot.click("#provider-settings-save")
                self.assertTrue(clicked)
                for _ in range(20):
                    await pilot.pause(0.01)
                    if (await store.load()).profiles:
                        break

            saved = await store.load()
            self.assertEqual(saved.default_provider, "personal")
            self.assertEqual(saved.profiles[0].protocol, "openai-chat")
            self.assertEqual(saved.profiles[0].dialect, "deepseek-v4")
            self.assertEqual(saved.profiles[0].model, "deepseek-v4-pro")
            self.assertNotIn("never-echo-this-key", repr(saved))

    async def test_deepseek_responses_profile_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            store = JsonProviderSettingsStore(Path(directory))
            app = ProviderSetupApp(
                provider_settings=ManagedProviderSettings(),
                provider_settings_store=store,
            )

            async with app.run_test(size=(110, 40)) as pilot:
                for _ in range(20):
                    await pilot.pause(0.01)
                    if isinstance(app.screen, ProviderSettingsScreen):
                        break
                self.assertIsInstance(app.screen, ProviderSettingsScreen)
                screen = app.screen
                screen._select_preset("openai")
                screen.query_one("#provider-settings-name", Input).value = "deepseek-responses"
                screen.query_one("#provider-settings-model", Input).value = "deepseek-v4-flash"
                screen.query_one(
                    "#provider-settings-base-url", Input
                ).value = "https://api.deepseek.com"
                screen.query_one("#provider-settings-api-key", Input).value = "never-echo-this-key"

                await pilot.click("#provider-settings-save")
                for _ in range(20):
                    await pilot.pause(0.01)
                    if (await store.load()).profiles:
                        break

            saved = await store.load()
            self.assertEqual(saved.default_provider, "deepseek-responses")
            self.assertEqual(saved.profiles[0].protocol, "openai-responses")
            self.assertEqual(saved.profiles[0].base_url, "https://api.deepseek.com")
            self.assertNotIn("never-echo-this-key", repr(saved))

    async def test_invalid_environment_proxy_stays_in_settings_and_direct_mode_recovers(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                "os.environ",
                {"ALL_PROXY": "socks://127.0.0.1:7890"},
                clear=True,
            ),
        ):
            store = JsonProviderSettingsStore(Path(directory))
            app = ProviderSetupApp(
                provider_settings=ManagedProviderSettings(),
                provider_settings_store=store,
            )

            async with app.run_test(size=(110, 44)) as pilot:
                for _ in range(20):
                    await pilot.pause(0.01)
                    if isinstance(app.screen, ProviderSettingsScreen):
                        break
                screen = app.screen
                self.assertIsInstance(screen, ProviderSettingsScreen)
                await pilot.click("#provider-settings-preset-deepseek")
                screen.query_one("#provider-settings-name", Input).value = "deepseek"
                screen.query_one("#provider-settings-model", Input).value = "deepseek-v4-flash"
                screen.query_one("#provider-settings-api-key", Input).value = "secret"

                await pilot.click("#provider-settings-save")
                await pilot.pause()

                self.assertIs(app.screen, screen)
                self.assertEqual((await store.load()).profiles, ())
                self.assertIn(
                    "ALL_PROXY",
                    str(screen.query_one("#provider-settings-error", Static).renderable),
                )

                app.screen._select_proxy_mode("direct")
                await app.screen._save_provider()
                for _ in range(20):
                    await pilot.pause(0.01)
                    if (await store.load()).profiles:
                        break

            saved = await store.load()
            self.assertEqual(saved.default_provider, "deepseek")
            self.assertEqual(saved.profiles[0].proxy_mode, "direct")
            self.assertIsNone(saved.profiles[0].proxy_url_env)

    async def test_provider_connection_loads_models_and_selects_without_saving_secret(
        self,
    ) -> None:
        catalog = ProviderCatalogFixture(
            ProviderCatalogResult(("deepseek-chat", "deepseek-reasoner"))
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            store = JsonProviderSettingsStore(Path(directory))
            app = ProviderSetupApp(
                provider_settings=ManagedProviderSettings(),
                provider_settings_store=store,
                provider_catalog=catalog,
            )

            async with app.run_test(size=(110, 48)) as pilot:
                for _ in range(20):
                    await pilot.pause(0.01)
                    if isinstance(app.screen, ProviderSettingsScreen):
                        break
                screen = app.screen
                self.assertIsInstance(screen, ProviderSettingsScreen)
                screen._select_preset("deepseek")
                screen._select_proxy_mode("direct")
                screen.query_one("#provider-settings-name", Input).value = "deepseek"
                screen.query_one("#provider-settings-api-key", Input).value = "never-render-key"

                clicked = await pilot.click("#provider-settings-test")
                self.assertTrue(clicked)
                for _ in range(20):
                    await pilot.pause(0.01)
                    if (
                        catalog.calls
                        and not screen.query_one("#provider-settings-test", Button).disabled
                    ):
                        break

                self.assertEqual(len(catalog.calls), 1)
                spec, policy = catalog.calls[0]
                self.assertEqual(spec.protocol, "openai-chat")
                self.assertEqual(spec.dialect, "deepseek-v4")
                self.assertEqual(spec.base_url, "https://api.deepseek.com")
                self.assertFalse(policy.trust_env)
                self.assertNotIn("never-render-key", repr(spec))
                status = str(
                    screen.query_one("#provider-settings-connection-status", Static).renderable
                )
                self.assertIn("Loaded 2 models", status)
                self.assertNotIn("never-render-key", status)
                self.assertTrue(screen.query_one("#provider-settings-models").display)

                model_button = screen.query_one("#provider-settings-catalog-model-1", Button)
                await screen.on_button_pressed(Button.Pressed(model_button))
                await pilot.pause()
                self.assertEqual(
                    screen.query_one("#provider-settings-model", Input).value,
                    "deepseek-reasoner",
                )

    async def test_provider_connection_error_is_localized_redacted_and_keeps_screen_open(
        self,
    ) -> None:
        catalog = ProviderCatalogFixture(
            error=ProviderCatalogError("authentication", status_code=401)
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            store = JsonProviderSettingsStore(Path(directory))
            app = ProviderSetupApp(
                provider_settings=ManagedProviderSettings(),
                provider_settings_store=store,
                provider_catalog=catalog,
                language=UiLanguage.SIMPLIFIED_CHINESE,
            )

            async with app.run_test(size=(110, 48)) as pilot:
                for _ in range(20):
                    await pilot.pause(0.01)
                    if isinstance(app.screen, ProviderSettingsScreen):
                        break
                screen = app.screen
                self.assertIsInstance(screen, ProviderSettingsScreen)
                screen._select_preset("deepseek")
                screen.query_one("#provider-settings-name", Input).value = "deepseek"
                screen.query_one("#provider-settings-api-key", Input).value = "never-render-key"

                await screen._test_connection()
                await pilot.pause()

                self.assertIs(app.screen, screen)
                self.assertEqual((await store.load()).profiles, ())
                status = str(
                    screen.query_one("#provider-settings-connection-status", Static).renderable
                )
                self.assertIn("认证失败", status)
                self.assertIn("401", status)
                self.assertNotIn("never-render-key", status)
                self.assertFalse(screen.query_one("#provider-settings-test", Button).disabled)

    async def test_provider_connection_reuses_saved_key_without_echoing_it(self) -> None:
        catalog = ProviderCatalogFixture(ProviderCatalogResult(("updated-model",)))
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            store = JsonProviderSettingsStore(Path(directory))
            settings = await store.save_profile(
                ManagedProviderProfile(
                    name="personal",
                    protocol="openai-responses",
                    model="old-model",
                    base_url="https://api.openai.com/v1",
                    proxy_mode="direct",
                    api_key="saved-secret",
                )
            )
            app = ProviderSetupApp(
                provider_settings=settings,
                provider_settings_store=store,
                provider_catalog=catalog,
                first_run=False,
                initial_profile="personal",
            )

            async with app.run_test(size=(110, 48)) as pilot:
                for _ in range(20):
                    await pilot.pause(0.01)
                    if isinstance(app.screen, ProviderSettingsScreen):
                        break
                screen = app.screen
                self.assertIsInstance(screen, ProviderSettingsScreen)
                self.assertEqual(
                    screen.query_one("#provider-settings-api-key", Input).value,
                    "",
                )

                await screen._test_connection()

                self.assertEqual(catalog.calls[0][0].api_key, "saved-secret")
                self.assertNotIn(
                    "saved-secret",
                    str(
                        screen.query_one("#provider-settings-connection-status", Static).renderable
                    ),
                )

    async def test_provider_connection_discards_result_after_draft_changes(self) -> None:
        catalog = BlockingProviderCatalogFixture(ProviderCatalogResult(("stale-model",)))
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            app = ProviderSetupApp(
                provider_settings=ManagedProviderSettings(),
                provider_settings_store=JsonProviderSettingsStore(Path(directory)),
                provider_catalog=catalog,
            )

            async with app.run_test(size=(110, 48)) as pilot:
                for _ in range(20):
                    await pilot.pause(0.01)
                    if isinstance(app.screen, ProviderSettingsScreen):
                        break
                screen = app.screen
                self.assertIsInstance(screen, ProviderSettingsScreen)
                screen._select_preset("deepseek")
                screen.query_one("#provider-settings-name", Input).value = "deepseek"
                screen.query_one("#provider-settings-api-key", Input).value = "secret"

                await pilot.click("#provider-settings-test")
                await catalog.started.wait()
                screen.query_one(
                    "#provider-settings-base-url", Input
                ).value = "https://changed.invalid/v1"
                catalog.release.set()
                for _ in range(20):
                    await pilot.pause(0.01)
                    if not screen.query_one("#provider-settings-test", Button).disabled:
                        break

                status = str(
                    screen.query_one("#provider-settings-connection-status", Static).renderable
                )
                self.assertIn("Discarded the old result", status)
                self.assertFalse(screen.query_one("#provider-settings-models").display)

    async def test_startup_proxy_error_opens_the_saved_profile_for_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonProviderSettingsStore(Path(directory))
            settings = await store.save_profile(
                ManagedProviderProfile(
                    name="deepseek",
                    protocol="openai-chat",
                    model="deepseek-v4-flash",
                    base_url="https://api.deepseek.com",
                    api_key="secret",
                )
            )
            app = ProviderSetupApp(
                provider_settings=settings,
                provider_settings_store=store,
                first_run=False,
                initial_profile="deepseek",
                initial_error="ALL_PROXY uses unsupported scheme 'socks'",
            )

            async with app.run_test(size=(110, 44)) as pilot:
                for _ in range(20):
                    await pilot.pause(0.01)
                    if isinstance(app.screen, ProviderSettingsScreen):
                        break
                screen = app.screen
                self.assertIsInstance(screen, ProviderSettingsScreen)
                self.assertTrue(screen.query_one("#provider-settings-name", Input).disabled)
                self.assertEqual(
                    screen.query_one("#provider-settings-name", Input).value,
                    "deepseek",
                )
                self.assertIn(
                    "ALL_PROXY",
                    str(screen.query_one("#provider-settings-error", Static).renderable),
                )
                self.assertEqual(
                    screen.query_one("#provider-settings-proxy-inherit", Button).variant,
                    "primary",
                )

    async def test_network_defaults_and_provider_context_capacity_are_persisted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonProviderSettingsStore(Path(directory))
            settings = await store.save_profile(
                ManagedProviderProfile(
                    name="personal",
                    protocol="openai-chat",
                    model="old-model",
                    base_url="https://provider.invalid/v1",
                    api_key="saved-secret",
                )
            )
            app = NeuroCodeApp(
                TuiConversation(),
                provider_settings_store=store,
                managed_provider_settings=settings,
                provider_name="fixture",
                model_name="fixture-model",
                cwd=Path("/workspace"),
            )

            async with app.run_test(size=(110, 44)) as pilot:
                await app.action_open_settings()
                await pilot.pause()
                self.assertIsInstance(app.screen, SettingsScreen)
                self.assertTrue(await pilot.click("#settings-category-network"))
                for _ in range(20):
                    await pilot.pause(0.01)
                    if isinstance(app.screen, NetworkProxySettingsScreen):
                        break
                self.assertIsInstance(app.screen, NetworkProxySettingsScreen)
                self.assertTrue(await pilot.click("#network-settings-direct"))
                self.assertTrue(await pilot.click("#network-settings-save"))
                for _ in range(20):
                    await pilot.pause(0.01)
                    if app.return_code is not None:
                        break

            updated = await store.load()
            self.assertEqual(updated.proxy_defaults, ManagedProxyPolicy("direct"))
            self.assertIsNone(updated.profiles[0].proxy_mode)
            self.assertEqual(app.return_code, TUI_RELOAD_PROVIDER_SETTINGS)

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            store = JsonProviderSettingsStore(Path(directory))
            app = ProviderSetupApp(
                provider_settings=ManagedProviderSettings(),
                provider_settings_store=store,
            )

            async with app.run_test(size=(110, 44)) as pilot:
                for _ in range(20):
                    await pilot.pause(0.01)
                    if isinstance(app.screen, ProviderSettingsScreen):
                        break
                screen = app.screen
                self.assertIsInstance(screen, ProviderSettingsScreen)
                screen._select_preset("deepseek")
                screen.query_one("#provider-settings-name", Input).value = "deepseek"
                screen.query_one("#provider-settings-model", Input).value = "deepseek-v4-flash"
                screen.query_one("#provider-settings-api-key", Input).value = "saved-secret"
                screen.query_one("#provider-settings-context-window", Input).value = "128000"
                await screen._save_provider()
                for _ in range(20):
                    await pilot.pause(0.01)
                    if (await store.load()).profiles:
                        break

            saved = await store.load()
            self.assertEqual(saved.profiles[0].context_window_tokens, 128_000)
            self.assertIsNone(saved.profiles[0].proxy_mode)

    async def test_background_wake_global_default_and_provider_override_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            store = JsonProviderSettingsStore(Path(directory))
            settings = await store.save_profile(
                ManagedProviderProfile(
                    name="personal",
                    protocol="openai-chat",
                    model="fixture-model",
                    base_url="https://provider.invalid/v1",
                    api_key="saved-secret",
                )
            )
            app = NeuroCodeApp(
                TuiConversation(),
                provider_settings_store=store,
                managed_provider_settings=settings,
                provider_name="personal",
                model_name="fixture-model",
                cwd=Path("/workspace"),
            )

            async with app.run_test(size=(110, 44)) as pilot:
                await app.action_open_settings()
                await pilot.pause()
                self.assertIsInstance(app.screen, SettingsScreen)
                self.assertTrue(await pilot.click("#settings-category-background-wake"))
                for _ in range(20):
                    await pilot.pause(0.01)
                    if isinstance(app.screen, BackgroundWakeSettingsScreen):
                        break
                self.assertIsInstance(app.screen, BackgroundWakeSettingsScreen)
                self.assertTrue(await pilot.click("#background-wake-settings-enabled"))
                self.assertTrue(await pilot.click("#background-wake-settings-save"))
                for _ in range(20):
                    await pilot.pause(0.01)
                    if app.return_code is not None:
                        break

            saved = await store.load()
            self.assertEqual(saved.background_task_wake_policy, BackgroundTaskWakePolicy.ENABLED)
            self.assertEqual(app.return_code, TUI_RELOAD_PROVIDER_SETTINGS)

            app = ProviderSetupApp(
                provider_settings=saved,
                provider_settings_store=store,
                first_run=False,
                initial_profile="personal",
            )
            async with app.run_test(size=(110, 48)) as pilot:
                for _ in range(20):
                    await pilot.pause(0.01)
                    if isinstance(app.screen, ProviderSettingsScreen):
                        break
                screen = app.screen
                self.assertIsInstance(screen, ProviderSettingsScreen)
                self.assertEqual(
                    screen.query_one("#provider-settings-wake-inherit", Button).variant,
                    "primary",
                )
                screen._select_background_wake_policy(BackgroundTaskWakePolicy.DISABLED)
                await screen._save_provider()
                for _ in range(20):
                    await pilot.pause(0.01)
                    if (await store.load()).profiles[0].background_task_wake_policy is not None:
                        break

            updated = await store.load()
            self.assertEqual(
                updated.effective_background_task_wake_policy("personal"),
                BackgroundTaskWakePolicy.DISABLED,
            )
            self.assertEqual(updated.background_task_wake_policy, BackgroundTaskWakePolicy.ENABLED)

    async def test_settings_edit_managed_provider_and_request_safe_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonProviderSettingsStore(Path(directory))
            settings = await store.save_profile(
                ManagedProviderProfile(
                    name="personal",
                    protocol="openai-responses",
                    model="old-model",
                    base_url="https://api.openai.com/v1",
                    api_key="saved-secret",
                )
            )
            app = NeuroCodeApp(
                TuiConversation(),
                provider_settings_store=store,
                managed_provider_settings=settings,
                provider_name="fixture",
                model_name="fixture-model",
                cwd=Path("/workspace"),
            )

            async with app.run_test(size=(110, 40)) as pilot:
                await app.action_open_settings()
                await pilot.pause()
                self.assertIsInstance(app.screen, SettingsScreen)
                await pilot.click("#settings-category-providers")
                for _ in range(20):
                    await pilot.pause(0.01)
                    if isinstance(app.screen, ProviderSettingsScreen):
                        break
                self.assertIsInstance(app.screen, ProviderSettingsScreen)
                await pilot.click("#provider-settings-profile-0")
                model = app.screen.query_one("#provider-settings-model", Input)
                model.value = "updated-model"
                self.assertEqual(
                    app.screen.query_one("#provider-settings-api-key", Input).value,
                    "",
                )
                app.screen._select_proxy_mode("direct")
                await pilot.click("#provider-settings-save")
                for _ in range(20):
                    await pilot.pause(0.01)
                    if app.return_code is not None:
                        break

            updated = await store.load()
            self.assertEqual(updated.profiles[0].model, "updated-model")
            self.assertEqual(updated.profiles[0].api_key, "saved-secret")
            self.assertEqual(updated.profiles[0].proxy_mode, "direct")
            self.assertEqual(app.return_code, TUI_RELOAD_PROVIDER_SETTINGS)

    async def test_settings_delete_requires_confirmation_and_reloads_safe_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonProviderSettingsStore(Path(directory))
            await store.save_profile(
                ManagedProviderProfile(
                    name="first",
                    protocol="openai-chat",
                    model="first-model",
                    base_url="https://first.invalid/v1",
                    proxy_mode="direct",
                    api_key="first-secret",
                )
            )
            settings = await store.save_profile(
                ManagedProviderProfile(
                    name="second",
                    protocol="openai-chat",
                    model="second-model",
                    base_url="https://second.invalid/v1",
                    proxy_mode="direct",
                    api_key="second-secret",
                )
            )
            app = NeuroCodeApp(
                TuiConversation(),
                provider_settings_store=store,
                managed_provider_settings=settings,
                provider_name="fixture",
                model_name="fixture-model",
                cwd=Path("/workspace"),
            )

            async with app.run_test(size=(110, 44)) as pilot:
                await app.action_open_settings()
                await pilot.pause()
                await pilot.click("#settings-category-providers")
                for _ in range(20):
                    await pilot.pause(0.01)
                    if isinstance(app.screen, ProviderSettingsScreen):
                        break
                self.assertIsInstance(app.screen, ProviderSettingsScreen)
                await pilot.click("#provider-settings-profile-1")
                delete = app.screen.query_one("#provider-settings-delete", Button)
                self.assertFalse(delete.disabled)

                await pilot.click("#provider-settings-delete")
                self.assertEqual(len((await store.load()).profiles), 2)
                self.assertIn("Confirm", str(delete.label))
                await pilot.pause()

                await app.screen._delete_provider()
                for _ in range(20):
                    await pilot.pause(0.01)
                    if app.return_code is not None:
                        break

            remaining = await store.load()
            self.assertEqual([profile.name for profile in remaining.profiles], ["first"])
            self.assertEqual(remaining.default_provider, "first")
            self.assertEqual(app.return_code, TUI_RELOAD_PROVIDER_SETTINGS)

    async def test_runtime_bar_shows_model_and_effort_and_localizes_labels(self) -> None:
        profiles = ProfileTuiController()
        app = NeuroCodeApp(
            TuiConversation(),
            provider_controller=profiles,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
            context_window_tokens=1_000_000,
        )

        async with app.run_test(size=(80, 24)) as pilot:
            model = app.query_one("#runtime-model", Static)
            context = app.query_one("#runtime-context", Static)
            effort = app.query_one("#runtime-effort", Static)
            workspace = app.query_one("#runtime-workspace", Static)
            mode = app.query_one("#runtime-mode", Static)
            self.assertIn("MODEL", str(model.renderable))
            self.assertIn("first · first-model", str(model.renderable))
            self.assertIn("EFFORT", str(effort.renderable))
            self.assertIn("● high", str(effort.renderable))
            self.assertIn("CTX", str(context.renderable))
            self.assertIn("~0.0%", str(context.renderable))
            self.assertIn("CWD", str(workspace.renderable))
            self.assertIn(str(Path("/workspace")), str(workspace.renderable))
            self.assertIn("MODE", str(mode.renderable))
            self.assertIn("normal", str(mode.renderable))
            model_segments = list(app.console.render(model.renderable))
            effort_segments = list(app.console.render(effort.renderable))
            mode_segments = list(app.console.render(mode.renderable))
            self.assertIn(
                ACCENT_CODE.lower(),
                str(
                    next(
                        segment.style for segment in model_segments if "first-model" in segment.text
                    )
                ).lower(),
            )
            self.assertIn(
                TEXT_EMPHASIS.lower(),
                str(
                    next(segment.style for segment in effort_segments if "high" in segment.text)
                ).lower(),
            )
            self.assertIn(
                TEXT_EMPHASIS.lower(),
                str(
                    next(segment.style for segment in mode_segments if "normal" in segment.text)
                ).lower(),
            )

            await app._language_settings_selected(UiLanguage.SIMPLIFIED_CHINESE)
            await pilot.pause()
            self.assertIn("模型", str(model.renderable))
            self.assertIn("上下文", str(context.renderable))
            self.assertIn("强度", str(effort.renderable))
            self.assertIn("工作区", str(workspace.renderable))
            self.assertIn("模式", str(mode.renderable))
            self.assertIn("first · first-model", str(model.renderable))

            app._provider_name = "same-name"
            app._model_name = "same-name"
            app._refresh_runtime_bar()
            self.assertEqual(str(model.renderable).count("same-name"), 1)
            self.assertNotIn(" · same-name", str(model.renderable))

    async def test_shift_tab_cycles_modes_and_persists_safe_auto_preview(self) -> None:
        profiles = ProfileTuiController()
        preferences = UiPreferencesFixture()
        app = NeuroCodeApp(
            TuiConversation(),
            provider_controller=profiles,
            ui_preferences=preferences,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 26)) as pilot:
            await pilot.press("shift+tab")
            await pilot.pause()
            self.assertEqual(profiles.mode_selections[-1], InteractionMode.ACCEPT_EDITS)
            self.assertEqual(preferences.saved_modes[-1], InteractionMode.ACCEPT_EDITS)
            self.assertIn(
                "accept-edits",
                str(app.query_one("#runtime-mode", Static).renderable),
            )

            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/mode auto"
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(profiles.mode_selections[-1], InteractionMode.AUTO)
            self.assertIn("safe preview", app.entries[-1].text)
            self.assertIn("auto", str(app.query_one("#runtime-mode", Static).renderable))

    async def test_context_bar_uses_provider_usage_and_status_reports_token_budget(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="deepseek",
            model_name="deepseek-v4-pro",
            cwd=Path("/workspace"),
            context_window_tokens=1_000_000,
        )

        async with app.run_test(size=(90, 24)) as pilot:
            await app._handle_event(
                AgentEvent.create(
                    1,
                    AgentEventKind.CONTEXT_USAGE_UPDATED,
                    {"used_tokens": 850_000, "estimated": False},
                )
            )
            await pilot.pause()

            context = app.query_one("#runtime-context", Static)
            self.assertIn("85.0%", str(context.renderable))
            self.assertNotIn("~", str(context.renderable))
            self.assertIn("850,000 / 1,000,000", str(context.tooltip))
            context_segments = list(app.console.render(context.renderable))
            self.assertIn(
                ACCENT_WARNING.lower(),
                str(
                    next(segment.style for segment in context_segments if "85.0%" in segment.text)
                ).lower(),
            )

            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/status"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("Context: 85.0% (850,000/1,000,000)", app.entries[-1].text)

    async def test_context_without_a_provider_window_shows_token_usage(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(90, 24)) as pilot:
            await app._handle_event(
                AgentEvent.create(
                    1,
                    AgentEventKind.CONTEXT_USAGE_UPDATED,
                    {"used_tokens": 1_536, "estimated": False},
                )
            )
            await pilot.pause()

            context = app.query_one("#runtime-context", Static)
            self.assertIn("1.5k tok", str(context.renderable))
            self.assertNotIn("?", str(context.renderable))
            self.assertNotIn("Unknown", str(context.renderable))
            self.assertEqual(context.tooltip, "1.5k tok")
            context_segments = list(app.console.render(context.renderable))
            self.assertIn(
                TEXT_SECONDARY.lower(),
                str(
                    next(
                        segment.style for segment in context_segments if "1.5k tok" in segment.text
                    )
                ).lower(),
            )

    async def test_runtime_budget_telemetry_is_not_rendered_in_the_tui(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(120, 24)) as pilot:
            await app._handle_event(
                AgentEvent.create(
                    1,
                    AgentEventKind.EXECUTION_BUDGET_UPDATED,
                    {
                        "model_calls_used": 17,
                        "model_calls_limit": 48,
                        "tool_rounds_used": 11,
                        "tool_rounds_limit": 48,
                        "tool_calls_used": 31,
                        "tool_calls_limit": 192,
                        "pressure": "normal",
                    },
                )
            )
            await pilot.pause()

            self.assertEqual(len(app.query("#runtime-budget")), 0)
            for widget_id in (
                "#runtime-model",
                "#runtime-workspace",
                "#runtime-context",
                "#runtime-effort",
                "#runtime-mode",
            ):
                rendered = str(app.query_one(widget_id, Static).renderable)
                self.assertNotIn("17/48", rendered)
                self.assertNotIn("R 11/48", rendered)
                self.assertNotIn("C 31/192", rendered)

    async def test_slash_commands_show_parameter_hints_and_tab_completes_first_option(
        self,
    ) -> None:
        runner = TuiConversation()
        profiles = ProfileTuiController()
        app = NeuroCodeApp(
            runner,
            provider_controller=profiles,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(90, 24)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            hints = app.query_one("#command-hints", Static)
            prompt.value = "/eff"
            await pilot.pause()
            self.assertTrue(hints.display)
            self.assertIn("/effort LEVEL", str(hints.renderable))

            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(prompt.value, "/effort")
            self.assertIn("/effort low", str(hints.renderable))

            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(prompt.value, "/effort low")

            prompt.value = "/provider"
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(prompt.value, "/provider first")

            prompt.value = "/resume"
            await pilot.pause()
            self.assertIn("/resume SESSION_ID", str(hints.renderable))
            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(prompt.value, "/resume ")

            prompt.value = "ordinary prompt"
            await pilot.pause()
            self.assertFalse(hints.display)
            self.assertEqual(runner.prompts, [])

            await pilot.press("ctrl+p")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, ProviderSelectionScreen):
                    break
            self.assertIsInstance(app.screen, ProviderSelectionScreen)
            focused_before = app.focused.id if app.focused is not None else None
            await pilot.press("tab")
            await pilot.pause()
            self.assertNotEqual(
                app.focused.id if app.focused is not None else None,
                focused_before,
            )
            await pilot.press("ctrl+c")

    async def test_effort_picker_switches_all_levels_and_marks_ultracode_fallback(
        self,
    ) -> None:
        profiles = ProfileTuiController()
        preferences = UiPreferencesFixture()
        app = NeuroCodeApp(
            TuiConversation(),
            provider_controller=profiles,
            ui_preferences=preferences,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+e")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, ReasoningEffortScreen):
                    break

            self.assertIsInstance(app.screen, ReasoningEffortScreen)
            self.assertLessEqual(app.screen.query_one("#effort-dialog").region.bottom, 24)
            labels = "\n".join(str(button.label) for button in app.screen.query(Button))
            for effort in ReasoningEffort:
                self.assertIn(effort.value, labels)
            clicked = await pilot.click("#effort-choice-3")
            self.assertTrue(clicked)
            for _ in range(20):
                await pilot.pause(0.01)
                if profiles.effort_selections:
                    break

            self.assertEqual(profiles.effort_selections, [ReasoningEffort.XHIGH])
            self.assertEqual(preferences.saved_efforts, [ReasoningEffort.XHIGH])
            self.assertIn(
                "⬤ xhigh",
                str(app.query_one("#runtime-effort", Static).renderable),
            )

            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/effort ultracode"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(profiles.effort_selections[-1], ReasoningEffort.ULTRACODE)
            self.assertIn("workflow orchestration is not implemented", app.entries[-1].text)
            self.assertIn(
                "⚡ ultracode → ⬤ xhigh",
                str(app.query_one("#runtime-effort", Static).renderable),
            )

            prompt.value = "/status"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("Effort: ⚡ ultracode → ⬤ xhigh", app.entries[-1].text)

    async def test_effort_validation_and_running_turn_guard_do_not_change_policy(self) -> None:
        runner = CancellableTuiConversation()
        profiles = ProfileTuiController()
        app = NeuroCodeApp(
            runner,
            provider_controller=profiles,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/effort impossible"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("Unknown effort", app.entries[-1].text)
            self.assertEqual(profiles.effort_selections, [])

            prompt.value = "long turn"
            await pilot.press("enter")
            await asyncio.wait_for(runner.started.wait(), timeout=1)
            prompt.value = "/effort low"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(profiles.effort_selections, [])
            self.assertEqual(
                app.entries[-1].text,
                "Cannot change reasoning effort while a turn is running.",
            )
            await pilot.press("ctrl+c")

    async def test_narrow_runtime_bar_keeps_effort_visible_above_the_prompt(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="provider-with-a-very-long-name",
            model_name="model-with-a-very-long-name",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(52, 18)) as pilot:
            await pilot.pause()
            runtime_bar = app.query_one("#runtime-bar")
            context = app.query_one("#runtime-context", Static)
            effort = app.query_one("#runtime-effort", Static)
            mode = app.query_one("#runtime-mode", Static)
            prompt = app.query_one("#prompt", PromptInput)
            self.assertLessEqual(runtime_bar.region.bottom, prompt.region.y)
            self.assertGreater(effort.region.width, 0)
            self.assertGreater(context.region.width, 0)
            self.assertGreater(mode.region.width, 0)
            self.assertIn("≈0 tok", str(context.renderable))
            self.assertIn("● high", str(effort.renderable))
            self.assertIn("normal", str(mode.renderable))

    async def test_terminal_size_fallback_expands_the_full_screen_layout(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            with patch("neuro_code.tui._read_terminal_size", return_value=Size(132, 41)):
                app._synchronize_terminal_size()
                await pilot.pause()

            self.assertEqual(app.screen.size, Size(132, 41))
            self.assertEqual(app.screen.region.size, Size(132, 41))
            transcript = app.query_one("#transcript", VerticalScroll)
            self.assertEqual(
                transcript.region.width + transcript.scrollbar_size_vertical,
                132,
            )
            prompt = app.query_one("#prompt", PromptInput)
            self.assertGreater(prompt.region.width, 0)
            self.assertLessEqual(prompt.region.right, app.screen.size.width)
            shortcut_bar = app.query_one("#shortcut-bar", Static)
            self.assertLess(prompt.region.bottom, shortcut_bar.region.y)
            self.assertGreater(shortcut_bar.region.height, 0)
            self.assertLessEqual(shortcut_bar.region.bottom, app.screen.size.height)

    async def test_tasks_command_lists_current_scope_without_rendering_command_or_output(
        self,
    ) -> None:
        runner = TuiConversation()
        tasks = TaskTuiController(
            (
                background_snapshot("task-running", BackgroundTaskStatus.RUNNING),
                background_snapshot(
                    "task-failed",
                    BackgroundTaskStatus.FAILED,
                    exit_code=7,
                ),
            )
        )
        app = NeuroCodeApp(
            runner,
            task_controller=tasks,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/tasks"
            await pilot.press("enter")
            await pilot.pause()

            rendered = next(
                entry.text
                for entry in reversed(app.entries)
                if "task-running · running" in entry.text
            )
            self.assertIn("task-running · running", rendered)
            self.assertIn("task-failed · failed · exit 7", rendered)
            self.assertIn("19 output bytes", rendered)
            self.assertNotIn("private-command", rendered)
            self.assertNotIn("private task output", rendered)
            self.assertEqual(runner.prompts, [])

    async def test_tasks_command_includes_durable_plan_execution_records(self) -> None:
        plan = SessionPlan(
            (
                PlanStep("Inspect the current state", PlanStepStatus.COMPLETED),
                PlanStep("Apply the reviewed change", PlanStepStatus.IN_PROGRESS),
            ),
            "Retain the execution revision for audit",
        )
        task = SessionTask(
            "task-plan",
            SessionTaskKind.PLAN_EXECUTION,
            SessionTaskStatus.COMPLETED,
            datetime(2026, 7, 28, 9, 30, tzinfo=UTC),
            datetime(2026, 7, 28, 9, 31, tzinfo=UTC),
            plan_snapshot=plan,
        )
        profiles = ProfileTuiController(session_tasks=(task,))
        app = NeuroCodeApp(
            TuiConversation(),
            session_task_controller=profiles,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/tasks"
            await pilot.press("enter")
            await pilot.pause()

            rendered = app.entries[-1].text
            self.assertIn("task-plan · plan execution · completed", rendered)
            self.assertIn("started", rendered)
            self.assertIn("finished", rendered)
            self.assertIn(f"plan revision {plan.fingerprint[:12]}", rendered)
            self.assertIn("1/2 completed", rendered)

    async def test_tasks_command_handles_empty_unavailable_failing_and_truncated_views(
        self,
    ) -> None:
        unavailable = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )
        async with unavailable.run_test(size=(110, 35)) as pilot:
            prompt = unavailable.query_one("#prompt", PromptInput)
            prompt.value = "/tasks"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("Task visibility is unavailable", unavailable.entries[-1].text)

            prompt.value = "/tasks unexpected"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("does not accept arguments", unavailable.entries[-1].text)

        empty = NeuroCodeApp(
            TuiConversation(),
            task_controller=TaskTuiController(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )
        async with empty.run_test(size=(110, 35)) as pilot:
            prompt = empty.query_one("#prompt", PromptInput)
            prompt.value = "/tasks"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("No tasks for the current session", empty.entries[-1].text)

        failing = NeuroCodeApp(
            TuiConversation(),
            task_controller=FailingTaskTuiController(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )
        async with failing.run_test(size=(110, 35)) as pilot:
            prompt = failing.query_one("#prompt", PromptInput)
            prompt.value = "/tasks"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("RuntimeError: task list failed", failing.entries[-1].text)

        snapshots = tuple(
            background_snapshot(f"task-{index}", BackgroundTaskStatus.RUNNING)
            for index in range(21)
        )
        truncated = NeuroCodeApp(
            TuiConversation(),
            task_controller=TaskTuiController(snapshots),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )
        async with truncated.run_test(size=(110, 35)) as pilot:
            prompt = truncated.query_one("#prompt", PromptInput)
            prompt.value = "/tasks"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("1 older task(s) omitted", truncated.entries[-1].text)
            self.assertNotIn("task-0 ·", truncated.entries[-1].text)
            self.assertIn("task-20 ·", truncated.entries[-1].text)

    async def test_view_task_renders_a_historical_plan_snapshot_without_starting_a_turn(
        self,
    ) -> None:
        plan = SessionPlan(
            (
                PlanStep("Inspect the current state", PlanStepStatus.COMPLETED),
                PlanStep("Apply the reviewed change", PlanStepStatus.IN_PROGRESS),
            ),
            "Retain the execution revision for audit",
        )
        task = SessionTask(
            "task-history",
            SessionTaskKind.PLAN_EXECUTION,
            SessionTaskStatus.COMPLETED,
            datetime(2026, 7, 28, 9, 30, tzinfo=UTC),
            datetime(2026, 7, 28, 9, 31, tzinfo=UTC),
            plan_snapshot=plan,
        )
        runner = TuiConversation()
        app = NeuroCodeApp(
            runner,
            session_task_controller=ProfileTuiController(session_tasks=(task,)),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/view-task task-history"
            await pilot.press("enter")
            await pilot.pause()

            rendered = app.entries[-1].text
            self.assertIn("Plan execution task task-history", rendered)
            self.assertIn(plan.fingerprint, rendered)
            self.assertIn("Retain the execution revision for audit", rendered)
            self.assertIn("[completed] Inspect the current state", rendered)
            self.assertIn("[in progress] Apply the reviewed change", rendered)
            self.assertIn("read-only", rendered)
            self.assertEqual(runner.prompts, [])

    async def test_view_task_reports_missing_or_legacy_plan_snapshots(self) -> None:
        legacy = SessionTask(
            "task-legacy",
            SessionTaskKind.PLAN_EXECUTION,
            SessionTaskStatus.COMPLETED,
            datetime(2026, 7, 28, 9, 30, tzinfo=UTC),
            datetime(2026, 7, 28, 9, 31, tzinfo=UTC),
        )
        app = NeuroCodeApp(
            TuiConversation(),
            session_task_controller=ProfileTuiController(session_tasks=(legacy,)),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/view-task missing-task"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("No durable task 'missing-task'", app.entries[-1].text)

            prompt.value = "/view-task task-legacy"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("has no saved plan snapshot", app.entries[-1].text)

    async def test_view_task_requires_an_id_and_a_session_task_reader(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            session_task_controller=ProfileTuiController(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/view-task"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("Usage: /view-task TASK_ID", app.entries[-1].text)

        unavailable = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )
        async with unavailable.run_test(size=(110, 35)) as pilot:
            prompt = unavailable.query_one("#prompt", PromptInput)
            prompt.value = "/view-task task-history"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("Task visibility is unavailable", unavailable.entries[-1].text)

    async def test_view_task_reports_a_reader_failure_without_starting_a_turn(self) -> None:
        runner = TuiConversation()
        app = NeuroCodeApp(
            runner,
            session_task_controller=FailingSessionTaskController(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/view-task task-history"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("RuntimeError: task read failed", app.entries[-1].text)
            self.assertEqual(runner.prompts, [])

    async def test_terminal_task_notification_is_emitted_once_without_raw_output(
        self,
    ) -> None:
        runner = TuiConversation()
        tasks = TaskTuiController((background_snapshot("task-fast", BackgroundTaskStatus.RUNNING),))
        app = NeuroCodeApp(
            runner,
            task_controller=tasks,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)):
            await app._poll_background_tasks()
            tasks.snapshots = (
                background_snapshot(
                    "task-fast",
                    BackgroundTaskStatus.COMPLETED,
                    exit_code=0,
                ),
            )
            await app._poll_background_tasks()
            await app._poll_background_tasks()

            notifications = [
                entry.text for entry in app.entries if "Background task task-fast" in entry.text
            ]
            self.assertEqual(notifications, ["Background task task-fast completed (exit 0)."])
            self.assertNotIn("private task output", "\n".join(notifications))

    async def test_background_task_auto_wake_is_disabled_by_default(self) -> None:
        runner = AutoWakeTuiConversation()
        tasks = TaskTuiController(
            (
                background_snapshot(
                    "task-fast",
                    BackgroundTaskStatus.COMPLETED,
                    exit_code=0,
                ),
            )
        )
        app = NeuroCodeApp(
            runner,
            task_controller=tasks,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)):
            await app._poll_background_tasks()
            await app._poll_background_tasks()

        self.assertEqual(runner.wake_count, 0)

    async def test_background_task_auto_wake_is_opt_in_bounded_and_deduplicated(self) -> None:
        runner = AutoWakeTuiConversation()
        tasks = TaskTuiController(
            (
                background_snapshot(
                    "task-fast",
                    BackgroundTaskStatus.COMPLETED,
                    exit_code=0,
                ),
            )
        )
        app = NeuroCodeApp(
            runner,
            task_controller=tasks,
            background_task_wake_policy=BackgroundTaskWakePolicy.ENABLED,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)) as pilot:
            await app._poll_background_tasks()
            for _ in range(20):
                await pilot.pause(0.01)
                if any(entry.category == "assistant" for entry in app.entries):
                    break
            await app._poll_background_tasks()

            self.assertEqual(runner.wake_count, 1)
            self.assertIn(
                ("status", "Background task completed; waking the model once."),
                [(entry.category, entry.text) for entry in app.entries],
            )
            self.assertIn(
                ("assistant", "wake response"),
                [(entry.category, entry.text) for entry in app.entries],
            )

    async def test_failed_background_wake_retains_pending_completion_for_retry(self) -> None:
        runner = FailingAutoWakeTuiConversation()
        tasks = TaskTuiController(
            (
                background_snapshot(
                    "task-fast",
                    BackgroundTaskStatus.COMPLETED,
                    exit_code=0,
                ),
            )
        )
        app = NeuroCodeApp(
            runner,
            task_controller=tasks,
            background_task_wake_policy=BackgroundTaskWakePolicy.ENABLED,
            background_wake_limits=BackgroundWakeLimits(cooldown_seconds=0.001),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)) as pilot:
            await app._poll_background_tasks()
            for _ in range(30):
                await pilot.pause(0.01)
                if any("wake provider failed" in entry.text for entry in app.entries):
                    break
            self.assertEqual(runner.wake_count, 1)
            self.assertEqual(tasks.wake_state.pending_task_ids, ("task-fast",))
            self.assertEqual(tasks.wake_state.wake_count, 0)
            self.assertFalse(tasks.wake_state.wake_in_flight)

            await app._poll_background_tasks()
            for _ in range(30):
                await pilot.pause(0.01)
                if runner.wake_count == 2:
                    break

        self.assertEqual(runner.wake_count, 2)
        self.assertEqual(tasks.wake_state.pending_task_ids, ("task-fast",))
        self.assertEqual(tasks.wake_state.wake_count, 0)

    async def test_reported_background_completion_does_not_start_an_empty_wake(self) -> None:
        runner = AutoWakeTuiConversation()
        tasks = TaskTuiController(
            (
                background_snapshot(
                    "task-reported",
                    BackgroundTaskStatus.COMPLETED,
                    exit_code=0,
                    completion_reported=True,
                ),
            )
        )
        app = NeuroCodeApp(
            runner,
            task_controller=tasks,
            background_task_wake_policy=BackgroundTaskWakePolicy.ENABLED,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)) as pilot:
            await app._poll_background_tasks()
            await pilot.pause(0.05)

        self.assertEqual(runner.wake_count, 0)
        self.assertEqual(tasks.wake_state.pending_task_ids, ())

    async def test_background_task_auto_wake_can_be_enabled_from_slash_command(self) -> None:
        runner = AutoWakeTuiConversation()
        tasks = TaskTuiController(
            (
                background_snapshot(
                    "task-fast",
                    BackgroundTaskStatus.COMPLETED,
                    exit_code=0,
                ),
            )
        )
        app = NeuroCodeApp(
            runner,
            task_controller=tasks,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/auto-wake on"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if runner.wake_count:
                    break

            self.assertEqual(runner.wake_count, 1)
            self.assertIn(
                "Background-task auto-wake is now enabled",
                "\n".join(entry.text for entry in app.entries),
            )

    async def test_auto_wake_on_creates_a_session_override_when_it_matches_the_global_default(
        self,
    ) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            managed_provider_settings=ManagedProviderSettings(
                background_task_wake_policy=BackgroundTaskWakePolicy.ENABLED
            ),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)):
            self.assertIsNone(app._background_task_wake_policy_override)
            await app._apply_background_task_wake_policy("on")
            self.assertIs(
                app._background_task_wake_policy_override,
                BackgroundTaskWakePolicy.ENABLED,
            )

    async def test_background_task_wake_state_survives_restart_without_duplicate_wake(self) -> None:
        tasks = TaskTuiController(
            (
                background_snapshot(
                    "task-fast",
                    BackgroundTaskStatus.COMPLETED,
                    exit_code=0,
                ),
            )
        )
        first_runner = AutoWakeTuiConversation()
        first_app = NeuroCodeApp(
            first_runner,
            task_controller=tasks,
            background_task_wake_policy=BackgroundTaskWakePolicy.ENABLED,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with first_app.run_test(size=(110, 35)) as pilot:
            await first_app._poll_background_tasks()
            for _ in range(20):
                await pilot.pause(0.01)
                if first_runner.wake_count:
                    break
        self.assertEqual(first_runner.wake_count, 1)
        self.assertEqual(tasks.wake_state.pending_task_ids, ())
        self.assertEqual(tasks.wake_state.wake_count, 1)

        second_runner = AutoWakeTuiConversation()
        second_app = NeuroCodeApp(
            second_runner,
            task_controller=tasks,
            background_task_wake_policy=BackgroundTaskWakePolicy.ENABLED,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )
        async with second_app.run_test(size=(110, 35)):
            await second_app._poll_background_tasks()
            await second_app._poll_background_tasks()
        self.assertEqual(second_runner.wake_count, 0)

    async def test_background_task_wake_budget_blocks_repeated_batches(self) -> None:
        runner = AutoWakeTuiConversation()
        tasks = TaskTuiController(
            (background_snapshot("task-fast", BackgroundTaskStatus.COMPLETED, exit_code=0),)
        )
        app = NeuroCodeApp(
            runner,
            task_controller=tasks,
            background_task_wake_policy=BackgroundTaskWakePolicy.ENABLED,
            background_wake_limits=BackgroundWakeLimits(max_wakes_per_session=1),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)) as pilot:
            await app._poll_background_tasks()
            for _ in range(20):
                await pilot.pause(0.01)
                if runner.wake_count:
                    break
            tasks.snapshots = (
                background_snapshot("task-next", BackgroundTaskStatus.COMPLETED, exit_code=0),
            )
            await app._poll_background_tasks()
            await pilot.pause(0.05)

        self.assertEqual(runner.wake_count, 1)
        self.assertIn("task-next", tasks.wake_state.pending_task_ids)

    def test_session_picker_labels_saved_and_mismatched_sandbox_profiles(self) -> None:
        option = replace(
            SessionTuiController().options[1],
            selectable=False,
            sandbox_profile=SandboxProfile.STRICT,
            sandbox_profile_match=False,
        )

        label = SessionSelectionScreen._label(option)

        self.assertIn("sandbox strict", label)
        self.assertIn("restart required", label)
        self.assertIn("unavailable", label)

    async def test_prompt_streams_events_and_commits_response(self) -> None:
        runner = TuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "inspect the repository"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if any(entry.category == "assistant" for entry in app.entries):
                    break

            self.assertFalse(prompt.disabled)
            self.assertEqual(runner.prompts, ["inspect the repository"])
            entries = [(entry.category, entry.text) for entry in app.entries]
            self.assertIn(("user", "inspect the repository"), entries)
            self.assertFalse(
                any(category == "status" and "Reasoning" in text for category, text in entries)
            )
            tool_entries = [text for category, text in entries if category == "tool"]
            self.assertEqual(len(tool_entries), 1)
            self.assertEqual(tool_entries[0], "read_file  ·  ✓ Read README.md · 420ms")
            self.assertIn(("assistant", "fixture response"), entries)
            self.assertEqual(entries[-1], ("status", "Turn completed in 2.8s · 1 model step(s)"))
            self.assertNotIn("private", "\n".join(text for _, text in entries))

    async def test_provider_balance_failure_keeps_the_session_input_recoverable(self) -> None:
        runner = ProviderFailureThenSuccessTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "first request"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if any(entry.category == "recoverable" for entry in app.entries):
                    break

            recoverable = next(entry for entry in app.entries if entry.category == "recoverable")
            self.assertIn("balance is insufficient", recoverable.text)
            self.assertIn("session is still open", recoverable.text)
            self.assertNotIn("ProviderError", recoverable.text)
            self.assertFalse(prompt.disabled)
            self.assertTrue(prompt.has_focus)

            prompt.value = "second request"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if any(entry.category == "assistant" for entry in app.entries):
                    break

            self.assertEqual(runner.prompts, ["first request", "second request"])
            self.assertIn(
                ("assistant", "fixture response"),
                [(entry.category, entry.text) for entry in app.entries],
            )

    async def test_expanding_truncated_tool_output_reads_bounded_session_artifact(self) -> None:
        artifact_id = "a" * 32

        class ArtifactService:
            def __init__(self) -> None:
                self.requests: list[ReadSessionToolOutputArtifactRequest] = []

            async def read(
                self, request: ReadSessionToolOutputArtifactRequest
            ) -> ToolOutputArtifactRead:
                self.requests.append(request)
                return ToolOutputArtifactRead(
                    ToolOutputArtifact(
                        artifact_id,
                        f"tool-output/{artifact_id}.log",
                        byte_count=64,
                        truncated=True,
                    ),
                    "full output line 1\nfull output line 2",
                    read_truncated=True,
                )

        runner = TuiConversation()
        runner._session_id = "artifact-session"
        artifact_service = ArtifactService()
        app = NeuroCodeApp(
            runner,
            tool_output_artifact_service=artifact_service,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 32)) as pilot:
            await app._handle_event(
                AgentEvent.create(
                    1,
                    AgentEventKind.TOOL_REQUESTED,
                    {
                        "id": "bash-call",
                        "name": "bash",
                        "arguments": {"command": "printf output"},
                    },
                )
            )
            await app._handle_event(
                AgentEvent.create(
                    2,
                    AgentEventKind.TOOL_COMPLETED,
                    {
                        "id": "bash-call",
                        "name": "bash",
                        "content": "preview output",
                        "metadata": {
                            "output_artifact_id": artifact_id,
                            "output_artifact_path": f"tool-output/{artifact_id}.log",
                            "output_artifact_bytes": 64,
                            "output_artifact_truncated": True,
                        },
                    },
                )
            )
            card = app.query_one(ToolFeedbackMessage)
            self.assertTrue(card.can_focus)
            card.focus()
            await pilot.press("enter")
            for _ in range(40):
                await pilot.pause(0.02)
                if any("full output line 2" in entry.text for entry in app.entries):
                    break

            self.assertEqual(len(artifact_service.requests), 1)
            request = artifact_service.requests[0]
            self.assertEqual(request.session_id, "artifact-session")
            self.assertEqual(request.artifact_id, artifact_id)
            card_text = next(entry.text for entry in app.entries if entry.category == "tool")
            self.assertIn("full output line 1", card_text)
            self.assertIn("full output line 2", card_text)
            self.assertIn("bounded at the read limit", card_text)
            self.assertNotIn(artifact_id, card_text)

    async def test_tool_card_updates_in_place_and_renders_a_redacted_file_diff(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 32)) as pilot:
            transcript = app.query_one("#transcript", VerticalScroll)
            await app._handle_event(
                AgentEvent.create(
                    1,
                    AgentEventKind.TOOL_REQUESTED,
                    {
                        "id": "write",
                        "name": "bash",
                        "arguments": {
                            "command": "printf 'API_KEY=sk-fixturesecret123' > src/new.py"
                        },
                    },
                )
            )
            await pilot.pause()
            child_count = len(transcript.children)

            for event in (
                AgentEvent.create(
                    2,
                    AgentEventKind.TOOL_PERMISSION,
                    {
                        "id": "write",
                        "name": "bash",
                        "effect": "allow",
                        "reason": "fixture policy",
                    },
                ),
                AgentEvent.create(
                    3,
                    AgentEventKind.TOOL_STARTED,
                    {"id": "write", "name": "bash"},
                ),
                AgentEvent.create(
                    4,
                    AgentEventKind.TOOL_COMPLETED,
                    {
                        "id": "write",
                        "name": "bash",
                        "content": "",
                        "duration_seconds": 0.125,
                        "workspace_changes": {
                            "files": [
                                {
                                    "path": "src/new.py",
                                    "status": "created",
                                    "additions": 2,
                                    "deletions": 0,
                                    "diff": (
                                        "--- /dev/null\n"
                                        "+++ b/src/new.py\n"
                                        "@@ -0,0 +1,2 @@\n"
                                        '+API_KEY = "sk-fixturesecret123"\n'
                                        '+print("ready")'
                                    ),
                                    "diff_truncated": False,
                                }
                            ],
                            "omitted_files": 0,
                            "scan_limited": False,
                        },
                    },
                ),
            ):
                await app._handle_event(event)
                await pilot.pause()
                self.assertEqual(len(transcript.children), child_count)

            tool_entries = [entry for entry in app.entries if entry.category == "tool"]
            self.assertEqual(len(tool_entries), 1)
            card = tool_entries[0].text
            self.assertIn("✓ bash(", card)
            self.assertIn("├ Allowed · fixture policy", card)
            self.assertIn("├ Created src/new.py (+2)", card)
            self.assertIn("+++ b/src/new.py", card)
            self.assertIn('+API_KEY = "[REDACTED]"', card)
            self.assertIn('+print("ready")', card)
            self.assertIn("└ Completed · 125ms", card)
            self.assertNotIn("sk-fixturesecret123", card)

            rendered_segments = list(
                app.console.render(
                    app.query_one(ToolFeedbackMessage).renderable,
                    app.console.options.update(width=100),
                )
            )
            added_segments = [
                segment for segment in rendered_segments if '+print("ready")' in segment.text
            ]
            removed_or_header_segments = [
                segment for segment in rendered_segments if "--- /dev/null" in segment.text
            ]
            command_segments = [segment for segment in rendered_segments if "bash(" in segment.text]
            success_segments = [segment for segment in rendered_segments if "✓" in segment.text]
            duration_segments = [
                segment for segment in rendered_segments if "125ms" in segment.text
            ]
            details_segments = [
                segment for segment in rendered_segments if "Details shown" in segment.text
            ]
            self.assertTrue(added_segments)
            self.assertIn(ACCENT_SUCCESS.lower(), str(added_segments[0].style).lower())
            self.assertIn(SURFACE_SELECTED.lower(), str(added_segments[0].style).lower())
            self.assertTrue(removed_or_header_segments)
            self.assertIn(ACCENT_ERROR.lower(), app._diff_line_style("-removed line").lower())
            self.assertIn(SURFACE_HOVER.lower(), app._diff_line_style("-removed line").lower())
            self.assertIn(ACCENT_CODE.lower(), str(command_segments[0].style).lower())
            self.assertIn(ACCENT_SUCCESS.lower(), str(success_segments[0].style).lower())
            self.assertIn(TEXT_SECONDARY.lower(), str(duration_segments[0].style).lower())
            self.assertIn(TEXT_SECONDARY.lower(), str(details_segments[0].style).lower())

            card_widget = app.query_one(ToolFeedbackMessage)
            self.assertTrue(card_widget.can_focus)
            self.assertIn("Details shown", card)
            card_widget.focus()
            await pilot.press("enter")
            await pilot.pause()

            collapsed_card = next(entry.text for entry in app.entries if entry.category == "tool")
            self.assertIn("Created src/new.py (+2)", collapsed_card)
            self.assertIn("Details hidden", collapsed_card)
            self.assertIn("Completed · 125ms", collapsed_card)
            self.assertNotIn("+++ b/src/new.py", collapsed_card)
            self.assertNotIn('+print("ready")', collapsed_card)
            self.assertNotIn("sk-fixturesecret123", collapsed_card)

            self.assertTrue(await pilot.click(card_widget, offset=(12, 0)))
            await pilot.pause()
            expanded_card = next(entry.text for entry in app.entries if entry.category == "tool")
            self.assertIn("+++ b/src/new.py", expanded_card)
            self.assertIn('+print("ready")', expanded_card)
            self.assertNotIn("sk-fixturesecret123", expanded_card)

            await app._language_settings_selected(UiLanguage.SIMPLIFIED_CHINESE)
            await pilot.pause()
            localized_card = next(entry.text for entry in app.entries if entry.category == "tool")
            self.assertIn("已允许 · fixture policy", localized_card)
            self.assertIn("新建 src/new.py", localized_card)
            self.assertIn("+2", localized_card)
            self.assertIn("完成 · 125ms", localized_card)
            self.assertIn("已展开详细信息", localized_card)

    async def test_local_slash_commands_do_not_call_the_model(self) -> None:
        runner = TuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/help"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("/status", app.entries[-1].text)
            self.assertIn("/cancel", app.entries[-1].text)
            self.assertIn("/sessions", app.entries[-1].text)
            self.assertIn("/rename", app.entries[-1].text)
            self.assertIn("/tasks", app.entries[-1].text)
            self.assertIn("/view-task TASK_ID", app.entries[-1].text)
            self.assertIn("/subagent PROMPT", app.entries[-1].text)
            self.assertIn("/subagents", app.entries[-1].text)

            prompt.value = "/status"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("fixture/fixture-model", app.entries[-1].text)

            prompt.value = "/clear"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual([entry.text for entry in app.entries], ["Transcript cleared."])
            self.assertEqual(runner.prompts, [])

    async def test_subagent_command_uses_safe_projection_without_parent_transcript_details(
        self,
    ) -> None:
        runner = TuiConversation()
        runner._session_id = "parent-session"
        service = ReadOnlySubagentTuiService(
            SubagentResultProjection(
                parent_session_id="parent-session",
                task_id="private-task",
                child_session_id="private-child",
                status=SessionTaskStatus.COMPLETED,
                response="Read-only repository findings",
                steps=3,
                truncated=False,
            )
        )
        app = NeuroCodeApp(
            runner,
            read_only_subagent_service=cast(ReadOnlySubagentApplicationService, service),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/subagent inspect the repository"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()

        self.assertEqual(len(service.requests), 1)
        self.assertEqual(service.requests[0].parent_session_id, "parent-session")
        self.assertEqual(service.requests[0].prompt, "inspect the repository")
        self.assertEqual(runner.prompts, [])
        rendered = "\n".join(entry.text for entry in app.entries)
        self.assertIn("Read-only subagent completed", rendered)
        self.assertIn("Read-only repository findings", rendered)
        self.assertNotIn("private-task", rendered)
        self.assertNotIn("private-child", rendered)

    async def test_subagent_command_fails_closed_without_session_or_service(self) -> None:
        no_session = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )
        async with no_session.run_test(size=(80, 24)) as pilot:
            prompt = no_session.query_one("#prompt", PromptInput)
            prompt.value = "/subagent inspect"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("unavailable", no_session.entries[-1].text.lower())

        runner = TuiConversation()
        runner._session_id = "parent-session"
        unavailable = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )
        async with unavailable.run_test(size=(80, 24)) as pilot:
            prompt = unavailable.query_one("#prompt", PromptInput)
            prompt.value = "/subagent inspect"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("unavailable", unavailable.entries[-1].text.lower())

    async def test_subagents_command_renders_bounded_relationship_metadata_only(self) -> None:
        runner = TuiConversation()
        runner._session_id = "parent-session"
        created = datetime(2026, 8, 7, 10, 15, tzinfo=UTC)
        service = SubagentRelationshipTuiService(
            (
                SubagentRelationshipProjection(
                    parent_session_id="parent-session",
                    parent_task_id="subagent-task",
                    child_session_id="child-session",
                    task_status=SessionTaskStatus.COMPLETED,
                    created_at=created,
                    child_provider="fixture-provider",
                    child_model="fixture-model",
                    child_updated_at=created,
                    available_actions=(
                        SubagentRelationshipAction.RESUME,
                        SubagentRelationshipAction.FORK,
                        SubagentRelationshipAction.DELETE,
                    ),
                ),
            )
        )
        app = NeuroCodeApp(
            runner,
            subagent_relationship_query=cast(SubagentRelationshipQueryController, service),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/subagents"
            await pilot.press("enter")
            await pilot.pause()

            rendered = app.entries[-1].text
            self.assertIn("Child subagent relationships (read-only)", rendered)
            self.assertIn("subagent-task → child-session", rendered)
            self.assertIn("fixture-provider/fixture-model", rendered)
            self.assertIn("completed", rendered)
            self.assertIn("resume, fork, delete", rendered)
            self.assertEqual(service.requests, ["parent-session"])
            self.assertEqual(runner.prompts, [])

            prompt.value = "/subagents unexpected"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("lifecycle controls are unavailable", app.entries[-1].text)

    async def test_subagents_resume_action_selects_child_without_running_model(self) -> None:
        runner = SessionTuiController(current_session="parent-session")
        lifecycle = SubagentRelationshipLifecycleTuiService()
        app = NeuroCodeApp(
            runner,
            session_controller=runner,
            session_selection_service=SessionSelectionService(runner),
            subagent_relationship_lifecycle=cast(
                SubagentRelationshipLifecycleController,
                lifecycle,
            ),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/subagents resume subagent-task"
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(
                lifecycle.requests,
                [
                    SubagentRelationshipActionRequest(
                        "parent-session",
                        "subagent-task",
                        SubagentRelationshipAction.RESUME,
                    )
                ],
            )
            self.assertEqual(runner.selected, ["child-session"])

    async def test_subagents_fork_and_delete_actions_only_project_bounded_result(self) -> None:
        runner = TuiConversation()
        runner._session_id = "parent-session"
        lifecycle = SubagentRelationshipLifecycleTuiService()
        app = NeuroCodeApp(
            runner,
            subagent_relationship_lifecycle=cast(
                SubagentRelationshipLifecycleController,
                lifecycle,
            ),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/subagents fork subagent-task"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("Forked child session forked-session", app.entries[-1].text)

            prompt.value = "/subagents delete subagent-task"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("Deleted child session child-session", app.entries[-1].text)
            self.assertEqual(
                [request.action for request in lifecycle.requests],
                [SubagentRelationshipAction.FORK, SubagentRelationshipAction.DELETE],
            )

    async def test_subagents_command_handles_empty_missing_and_reader_failure(self) -> None:
        runner = TuiConversation()
        runner._session_id = "parent-session"
        empty_service = SubagentRelationshipTuiService(())
        empty = NeuroCodeApp(
            runner,
            subagent_relationship_query=cast(
                SubagentRelationshipQueryController,
                empty_service,
            ),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )
        async with empty.run_test(size=(90, 24)) as pilot:
            prompt = empty.query_one("#prompt", PromptInput)
            prompt.value = "/subagents"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("No child subagent relationships", empty.entries[-1].text)

        no_session = NeuroCodeApp(
            TuiConversation(),
            subagent_relationship_query=cast(
                SubagentRelationshipQueryController,
                empty_service,
            ),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )
        async with no_session.run_test(size=(90, 24)) as pilot:
            prompt = no_session.query_one("#prompt", PromptInput)
            prompt.value = "/subagents"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("Start or resume a session", no_session.entries[-1].text)

        failing = NeuroCodeApp(
            runner,
            subagent_relationship_query=cast(
                SubagentRelationshipQueryController,
                FailingSubagentRelationshipTuiService(),
            ),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )
        async with failing.run_test(size=(90, 24)) as pilot:
            prompt = failing.query_one("#prompt", PromptInput)
            prompt.value = "/subagents"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("RuntimeError: relationship list failed", failing.entries[-1].text)

    async def test_running_tool_feedback_shows_elapsed_time_without_output(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            app._begin_pending_assistant()
            await app._handle_event(
                AgentEvent.create(0, AgentEventKind.TEXT_DELTA, {"text": "Partial answer."})
            )
            await app._handle_event(
                AgentEvent.create(
                    1,
                    AgentEventKind.TOOL_REQUESTED,
                    {"id": "wait", "name": "wait_tasks", "arguments": {"timeout": 30}},
                )
            )
            assistant_index = next(
                index for index, entry in enumerate(app.entries) if entry.category == "assistant"
            )
            tool_index = next(
                index for index, entry in enumerate(app.entries) if entry.category == "tool"
            )
            self.assertLess(assistant_index, tool_index)
            self.assertEqual(app.entries[assistant_index].text, "Partial answer.")
            self.assertIsNone(app._pending_assistant)
            await app._handle_event(
                AgentEvent.create(
                    2,
                    AgentEventKind.TOOL_STARTED,
                    {"id": "wait", "name": "wait_tasks"},
                )
            )
            state = app._tool_feedback_by_call[(False, "wait")]
            state.started_at = 100.0
            app._turn_activity_tool_started_at = state.started_at
            with patch.object(app, "_refresh_tool_feedback") as refresh:
                app._advance_model_loading_animation()
                refresh.assert_not_called()
                app._refresh_running_tool_elapsed()
                refresh.assert_called_once_with(state)
            with patch("neuro_code.tui.monotonic", return_value=112.7):
                app._refresh_running_tool_elapsed()
            running_text = next(entry.text for entry in app.entries if entry.category == "tool")
            self.assertIn("Waiting", running_text)
            self.assertIn("12.7s", running_text)
            activity = app.query_one("#turn-activity", Static)
            self.assertTrue(activity.display)
            self.assertIn("wait_tasks", str(activity.renderable))
            self.assertIn("12.7s", str(activity.renderable))

            await app._handle_event(
                AgentEvent.create(
                    3,
                    AgentEventKind.TOOL_COMPLETED,
                    {"id": "wait", "name": "wait_tasks", "duration_seconds": 13.4},
                )
            )
            completed_text = next(entry.text for entry in app.entries if entry.category == "tool")
            self.assertIn("Completed · 13.4s", completed_text)
            self.assertNotIn("wait_tasks", str(activity.renderable))
            await pilot.pause()

    async def test_streamed_model_steps_are_not_recombined_at_turn_completion(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)):
            app._begin_pending_assistant()
            await app._handle_event(
                AgentEvent.create(1, AgentEventKind.TEXT_DELTA, {"text": "First conclusion."})
            )
            await app._handle_event(
                AgentEvent.create(
                    2,
                    AgentEventKind.TOOL_REQUESTED,
                    {"id": "inspect", "name": "read_file", "arguments": {"path": "a.py"}},
                )
            )
            await app._handle_event(
                AgentEvent.create(3, AgentEventKind.TEXT_DELTA, {"text": "Final conclusion."})
            )

            result = AgentRunResult(
                "session",
                "First conclusion.Final conclusion.",
                (
                    Message(Role.ASSISTANT, "First conclusion."),
                    Message(Role.ASSISTANT, "Final conclusion."),
                ),
                (),
                (),
                2,
            )
            app._finish_streamed_assistant_response(result, fallback=result.response)

            assistant_entries = [
                entry.text for entry in app.entries if entry.category == "assistant"
            ]
            self.assertEqual(assistant_entries, ["First conclusion.", "Final conclusion."])
            self.assertNotIn(result.response, assistant_entries)
            self.assertFalse(app._model_loading)

    async def test_double_clicking_an_assistant_reply_opens_a_selectable_copy_view(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            app._write_entry("assistant", "Copy this response.")
            message = app._entry_widgets[-1]
            self.assertIsInstance(message, AssistantMessage)
            await message._on_click(
                events.Click(message, 0, 0, 0, 0, 1, False, False, False, chain=2)
            )
            await pilot.pause()

            self.assertIsInstance(app.screen, TranscriptCopyScreen)
            editor = app.screen.query_one("#transcript-copy-text", TextArea)
            self.assertEqual(editor.text, "Copy this response.")

    async def test_prompt_selection_copies_before_a_running_turn_is_cancelled(self) -> None:
        runner = CancellableTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "long turn"
            await pilot.press("enter")
            await asyncio.wait_for(runner.started.wait(), timeout=1)
            prompt.value = "selected draft"
            prompt.select_all()

            with patch.object(app, "copy_to_clipboard") as copy:
                app.action_cancel_turn()
                copy.assert_called_once_with("selected draft")
            self.assertFalse(runner.cancelled)

            prompt.selection = Selection.cursor(prompt.cursor_location)
            app.action_cancel_turn()
            for _ in range(20):
                await pilot.pause(0.01)
                if runner.cancelled:
                    break
            self.assertTrue(runner.cancelled)

    async def test_turn_activity_uses_the_wave_without_exposing_model_steps(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)):
            app._begin_pending_assistant()
            await app._handle_event(
                AgentEvent.create(1, AgentEventKind.TEXT_DELTA, {"text": "Partial answer."})
            )
            await app._handle_event(
                AgentEvent.create(2, AgentEventKind.MODEL_STEP_STARTED, {"step": 20})
            )
            with patch("neuro_code.tui.monotonic", return_value=125.0):
                app._advance_model_loading_animation()
                app._advance_model_loading_animation()

            activity = app.query_one("#turn-activity", Static)
            rendered = str(activity.renderable)
            self.assertTrue(activity.display)
            self.assertIn("Waiting for the model", rendered)
            self.assertTrue(any(symbol in rendered for symbol in "▁▂▃▄▅▆▇█"))
            self.assertNotIn("step", rendered)
            self.assertNotIn("20", rendered)

    async def test_compact_tool_summary_names_the_tool_and_bounded_action(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)):
            await app._handle_event(
                AgentEvent.create(
                    1,
                    AgentEventKind.TOOL_REQUESTED,
                    {
                        "id": "read-many",
                        "name": "read_files",
                        "arguments": {
                            "files": [
                                {"path": "src/a.py"},
                                {"path": "src/b.py"},
                            ]
                        },
                    },
                )
            )
            await app._handle_event(
                AgentEvent.create(
                    2,
                    AgentEventKind.TOOL_COMPLETED,
                    {
                        "id": "read-many",
                        "name": "read_files",
                        "content": "bounded output",
                        "duration_seconds": 0.12,
                    },
                )
            )

            summary = next(entry.text for entry in app.entries if entry.category == "tool")
            self.assertIn("read_files", summary)
            self.assertIn("Read 2 files", summary)
            self.assertIn("120ms", summary)

    async def test_plan_updated_is_a_single_first_class_entry_refreshed_in_place(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )
        first_plan = SessionPlan(
            (
                PlanStep("Read the architecture", PlanStepStatus.COMPLETED),
                PlanStep("Check the runtime", PlanStepStatus.IN_PROGRESS),
                PlanStep("Verify the feedback", PlanStepStatus.PENDING),
            )
        )
        updated_plan = SessionPlan(
            (
                PlanStep("Read the architecture", PlanStepStatus.COMPLETED),
                PlanStep("Check the runtime", PlanStepStatus.COMPLETED),
                PlanStep("Verify the feedback", PlanStepStatus.IN_PROGRESS),
            )
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await app._handle_event(
                AgentEvent.create(1, AgentEventKind.PLAN_UPDATED, first_plan.to_dict())
            )
            await pilot.pause()
            plan_entries = [entry for entry in app.entries if entry.category == "plan"]
            self.assertEqual(len(plan_entries), 1)
            assert app._plan_entry_index is not None
            widget = app._entry_widgets[app._plan_entry_index]
            self.assertIn("✓ Read the architecture", plan_entries[0].text)
            self.assertIn("\u203a Check the runtime", plan_entries[0].text)
            self.assertIn("□ Verify the feedback", plan_entries[0].text)

            await app._handle_event(
                AgentEvent.create(
                    2,
                    AgentEventKind.TOOL_REQUESTED,
                    {"id": "plan", "name": "update_plan", "arguments": {}},
                )
            )
            await app._handle_event(
                AgentEvent.create(
                    3,
                    AgentEventKind.TOOL_COMPLETED,
                    {"id": "plan", "name": "update_plan", "duration_seconds": 0.1},
                )
            )
            await app._handle_event(
                AgentEvent.create(4, AgentEventKind.PLAN_UPDATED, updated_plan.to_dict())
            )
            await pilot.pause()
            self.assertEqual(len([entry for entry in app.entries if entry.category == "plan"]), 1)
            assert app._plan_entry_index is not None
            self.assertIs(app._entry_widgets[app._plan_entry_index], widget)
            self.assertEqual(app._plan_entry_index, len(app.entries) - 1)
            transcript = app.query_one("#transcript", VerticalScroll)
            self.assertIs(tuple(transcript.children)[-1], widget)
            updated_text = app.entries[app._plan_entry_index].text
            self.assertIn("✓ Check the runtime", updated_text)
            self.assertIn("\u203a Verify the feedback", updated_text)

    async def test_plan_commands_show_the_saved_plan_and_start_a_plan_turn(self) -> None:
        plan = SessionPlan(
            (
                PlanStep("Inspect the current behavior", PlanStepStatus.COMPLETED),
                PlanStep("Implement the safe follow-up", PlanStepStatus.IN_PROGRESS),
            ),
            "Keep the agreed work visible in this session",
        )
        runner = TuiConversation()
        profiles = ProfileTuiController(plan)
        app = NeuroCodeApp(
            runner,
            provider_controller=profiles,
            interaction_mode_controller=profiles,
            plan_controller=profiles,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/view-plan"
            await pilot.press("enter")
            await pilot.pause()
            displayed = app.entries[-1].text
            self.assertIn("Updated plan:", displayed)
            self.assertIn("Keep the agreed work visible", displayed)
            self.assertIn("\u203a Implement the safe follow-up", displayed)

            prompt.value = "/plan Verify the implementation before editing"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if runner.prompts:
                    break
            self.assertEqual(profiles.mode_selections, [InteractionMode.PLAN])
            self.assertEqual(runner.prompts, ["Verify the implementation before editing"])

    async def test_plan_comment_command_persists_and_renders_user_feedback(self) -> None:
        plan = SessionPlan(
            (
                PlanStep("Inspect the current behavior", PlanStepStatus.COMPLETED),
                PlanStep("Implement the safe follow-up", PlanStepStatus.IN_PROGRESS),
            ),
        )
        runner = TuiConversation()
        profiles = ProfileTuiController(plan)
        app = NeuroCodeApp(
            runner,
            plan_controller=profiles,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/comment-plan 2 Keep the verification check explicit"
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(len(await profiles.list_plan_comments()), 1)
            self.assertIn("Saved comment for plan step 2.", app.entries[-2].text)
            self.assertIn("· Keep the verification check explicit", app.entries[-1].text)

    async def test_execute_plan_switches_only_to_accept_edits_and_records_the_handoff(self) -> None:
        plan = SessionPlan(
            (PlanStep("Implement the reviewed change", PlanStepStatus.IN_PROGRESS),),
            "Execute only after the user confirms",
        )
        runner = TuiConversation()
        profiles = ProfileTuiController(plan)
        profiles._interaction_mode = InteractionMode.PLAN
        app = NeuroCodeApp(
            runner,
            provider_controller=profiles,
            interaction_mode_controller=profiles,
            plan_controller=profiles,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/execute-plan"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if profiles.plan_execution_calls:
                    break

            self.assertEqual(profiles.mode_selections, [InteractionMode.ACCEPT_EDITS])
            self.assertEqual(profiles.plan_execution_calls, 1)
            self.assertIn(
                "Execution started from the current structured plan in accept-edits mode.",
                [entry.text for entry in app.entries],
            )
            self.assertIn(
                "Execute the current structured plan.", [entry.text for entry in app.entries]
            )

    async def test_execute_plan_uses_the_application_workflow_service(self) -> None:
        plan = SessionPlan((PlanStep("Execute through the application seam"),))
        runner = TuiConversation()
        profiles = ProfileTuiController(plan)
        app = NeuroCodeApp(
            runner,
            provider_controller=profiles,
            interaction_mode_controller=profiles,
            plan_controller=profiles,
            plan_execution_service=PlanExecutionService(profiles),
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/execute-plan"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if profiles.plan_execution_calls:
                    break

            self.assertEqual(profiles.plan_execution_calls, 1)

    async def test_execute_plan_requires_a_saved_plan(self) -> None:
        runner = TuiConversation()
        profiles = ProfileTuiController()
        app = NeuroCodeApp(
            runner,
            provider_controller=profiles,
            interaction_mode_controller=profiles,
            plan_controller=profiles,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/execute-plan"
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(profiles.mode_selections, [])
            self.assertEqual(profiles.plan_execution_calls, 0)
            self.assertIn("No structured plan has been saved", app.entries[-1].text)

    async def test_schedule_plan_queues_without_running_and_run_task_starts_it(self) -> None:
        plan = SessionPlan(
            (PlanStep("Run the queued plan", PlanStepStatus.IN_PROGRESS),),
            "Require an explicit start command",
        )
        runner = TuiConversation()
        profiles = ProfileTuiController(plan)

        class SchedulingSpy:
            def __init__(self, delegate: ProfileTuiController) -> None:
                self.delegate = delegate
                self.calls = 0

            async def schedule_plan(self) -> SessionTask:
                self.calls += 1
                return await self.delegate.schedule_plan()

        scheduling_spy = SchedulingSpy(profiles)
        app = NeuroCodeApp(
            runner,
            provider_controller=profiles,
            interaction_mode_controller=profiles,
            plan_controller=profiles,
            plan_scheduling_service=PlanSchedulingService(scheduling_spy),
            session_task_controller=profiles,
            queued_plan_execution_service=QueuedPlanExecutionService(profiles),
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/schedule-plan"
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(profiles.plan_execution_calls, 0)
            queued = profiles._session_tasks[0]
            self.assertIs(queued.status, SessionTaskStatus.QUEUED)
            self.assertIn(queued.task_id, app.entries[-1].text)
            self.assertEqual(scheduling_spy.calls, 1)

            prompt.value = f"/run-task {queued.task_id}"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if profiles.plan_execution_calls:
                    break

            self.assertEqual(profiles.plan_execution_calls, 1)
            self.assertIs(profiles._session_tasks[0].status, SessionTaskStatus.COMPLETED)
            self.assertIn("Run queued plan task", " ".join(entry.text for entry in app.entries))

    async def test_run_task_reports_missing_non_plan_and_non_queued_tasks(self) -> None:
        plan = SessionPlan((PlanStep("Run a plan"),))
        timestamp = datetime(2026, 7, 29, 12, tzinfo=UTC)
        running = SessionTask(
            "task-running",
            SessionTaskKind.PLAN_EXECUTION,
            SessionTaskStatus.RUNNING,
            timestamp,
            plan_snapshot=plan,
        )
        subagent = SessionTask(
            "task-subagent",
            SessionTaskKind.SUBAGENT,
            SessionTaskStatus.QUEUED,
            timestamp,
        )
        profiles = ProfileTuiController(plan, session_tasks=(running, subagent))
        app = NeuroCodeApp(
            TuiConversation(),
            plan_controller=profiles,
            session_task_controller=profiles,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            for task_id, expected in (
                ("missing", "No durable task 'missing' exists"),
                ("task-subagent", "is not a plan execution task"),
                ("task-running", "is not queued"),
            ):
                prompt.value = f"/run-task {task_id}"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIn(expected, app.entries[-1].text)

    async def test_permission_modal_blocks_until_allow_once_is_selected(self) -> None:
        broker = SessionApprovalBroker()
        runner = ApprovalTuiConversation(broker)
        app = NeuroCodeApp(
            runner,
            approval_controller=broker,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "edit the file"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, PermissionApprovalScreen):
                    break

            self.assertIsInstance(app.screen, PermissionApprovalScreen)
            self.assertFalse(runner.executed)
            approval_screen = app.screen
            assert isinstance(approval_screen, PermissionApprovalScreen)
            self.assertNotIn("private-old", approval_screen.request.summary)
            self.assertNotIn("private-new", approval_screen.request.summary)

            clicked = await pilot.click("#approval-allow-once")
            self.assertTrue(clicked)
            for _ in range(20):
                await pilot.pause(0.01)
                if runner.approvals:
                    break

            self.assertTrue(runner.executed)
            self.assertEqual(runner.approvals[0].kind, PermissionApprovalKind.ALLOW_ONCE)

    async def test_permission_modal_defaults_to_deny(self) -> None:
        broker = SessionApprovalBroker()
        runner = ApprovalTuiConversation(broker)
        app = NeuroCodeApp(
            runner,
            approval_controller=broker,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "edit the file"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, PermissionApprovalScreen):
                    break

            self.assertEqual(app.focused.id if app.focused is not None else None, "approval-deny")
            await pilot.press("ctrl+c")
            for _ in range(20):
                await pilot.pause(0.01)
                if runner.approvals:
                    break

            self.assertFalse(runner.executed)
            self.assertEqual(runner.approvals[0].kind, PermissionApprovalKind.DENY)

    async def test_ctrl_c_cancels_a_running_turn_and_keeps_input_available(self) -> None:
        runner = CancellableTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "long turn"
            await pilot.press("enter")
            await asyncio.wait_for(runner.started.wait(), timeout=1)
            self.assertFalse(prompt.disabled)

            await pilot.press("ctrl+c")
            for _ in range(20):
                await pilot.pause(0.01)
                if runner.cancelled and any(
                    entry.text == "Turn cancelled." for entry in app.entries
                ):
                    break

            self.assertTrue(runner.cancelled)
            self.assertIn("Cancellation requested.", [entry.text for entry in app.entries])
            self.assertIn("Turn cancelled.", [entry.text for entry in app.entries])
            self.assertEqual(runner.prompts, ["long turn"])

    async def test_pristine_cancel_restores_draft_and_removes_transcript_prompt(self) -> None:
        runner = PristineRewindTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "restore this prompt"
            await pilot.press("enter")
            await asyncio.wait_for(runner.started.wait(), timeout=1)

            await pilot.press("ctrl+c")
            for _ in range(30):
                await pilot.pause(0.02)
                if prompt.value == "restore this prompt" and runner.cancelled:
                    break

            self.assertTrue(runner.cancelled)
            self.assertEqual(
                runner.policies,
                [TurnCancellationPolicy.REWIND_PRISTINE],
            )
            self.assertEqual(prompt.value, "restore this prompt")
            self.assertNotIn(
                "restore this prompt",
                [entry.text for entry in app.entries if entry.category == "user"],
            )
            self.assertIn(
                "The cancelled prompt was restored to the draft.",
                [entry.text for entry in app.entries],
            )

    async def test_pristine_cancel_preserves_original_prompt_when_a_newer_draft_exists(
        self,
    ) -> None:
        runner = PristineRewindTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "original prompt"
            await pilot.press("enter")
            await asyncio.wait_for(runner.started.wait(), timeout=1)
            prompt.value = "newer draft"

            await pilot.press("ctrl+c")
            for _ in range(30):
                await pilot.pause(0.02)
                if runner.cancelled:
                    break

            self.assertEqual(prompt.value, "newer draft")
            self.assertIn(
                "original prompt",
                [entry.text for entry in app.entries if entry.category == "user"],
            )
            self.assertIn(
                "The cancelled prompt remains above because a newer draft is already in the input.",
                [entry.text for entry in app.entries],
            )

    async def test_pre_token_prompt_is_buffered_and_runs_after_current_turn(self) -> None:
        runner = PreTokenInterjectionConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "first prompt"
            await pilot.press("enter")
            self.assertEqual(
                await asyncio.wait_for(runner.started.get(), timeout=1), "first prompt"
            )

            prompt.value = "follow-up before first token"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(runner.prompts, ["first prompt"])
            self.assertIn(
                "Queued until the current response completes.",
                [entry.text for entry in app.entries],
            )

            await runner.release.put(None)
            for _ in range(30):
                await pilot.pause(0.02)
                if runner.prompts == ["first prompt", "follow-up before first token"]:
                    break
            self.assertEqual(runner.prompts, ["first prompt", "follow-up before first token"])

            await runner.release.put(None)
            for _ in range(30):
                await pilot.pause(0.02)
                if any(
                    "response to follow-up before first token" in entry.text
                    for entry in app.entries
                ):
                    break
            self.assertTrue(
                any(
                    "response to follow-up before first token" in entry.text
                    for entry in app.entries
                )
            )

    async def test_prompt_after_first_token_keeps_running_turn_guard(self) -> None:
        runner = StreamingTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "first prompt"
            await pilot.press("enter")
            await asyncio.wait_for(runner.started.wait(), timeout=1)

            prompt.value = "late follow-up"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                app.entries[-1].text,
                "A turn is already running.",
            )
            self.assertEqual(runner._session_id, "streaming-session")
            runner.release.set()

    async def test_cancelled_pre_token_turn_restores_buffered_prompt(self) -> None:
        runner = PreTokenInterjectionConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "first prompt"
            await pilot.press("enter")
            await asyncio.wait_for(runner.started.get(), timeout=1)
            prompt.value = "restore this follow-up"
            await pilot.press("enter")
            await pilot.press("ctrl+c")
            for _ in range(30):
                await pilot.pause(0.02)
                if prompt.value == "restore this follow-up":
                    break
            self.assertEqual(prompt.value, "restore this follow-up")
            self.assertEqual(runner.prompts, ["first prompt"])

    async def test_cancelled_pre_token_turn_restores_every_queued_prompt_without_autorun(
        self,
    ) -> None:
        runner = CancellableTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "first prompt"
            await pilot.press("enter")
            await asyncio.wait_for(runner.started.wait(), timeout=1)
            for follow_up in ("queued one", "queued two"):
                prompt.value = follow_up
                await pilot.press("enter")

            await pilot.press("ctrl+c")
            for _ in range(30):
                await pilot.pause(0.02)
                if runner.cancelled:
                    break

            self.assertEqual(prompt.value, "queued one\n\nqueued two")
            self.assertEqual(runner.prompts, ["first prompt"])
            self.assertFalse(app._queued_interjections)
            await pilot.pause(0.05)
            self.assertEqual(runner.prompts, ["first prompt"])

    async def test_pre_token_interjection_limit_keeps_the_unsent_prompt_in_the_input(self) -> None:
        runner = CancellableTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "first prompt"
            await pilot.press("enter")
            await asyncio.wait_for(runner.started.wait(), timeout=1)
            for index in range(4):
                prompt.value = f"queued {index}"
                await pilot.press("enter")
            prompt.value = "fifth prompt"
            await pilot.press("enter")

            self.assertEqual(prompt.value, "fifth prompt")
            self.assertEqual(len(app._queued_interjections), 4)
            self.assertIn(
                "Too many queued prompts; wait for the current turn to finish.",
                [entry.text for entry in app.entries],
            )
            await pilot.press("ctrl+c")

    async def test_clearing_the_transcript_discards_queued_interjections(self) -> None:
        runner = CancellableTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "first prompt"
            await pilot.press("enter")
            await asyncio.wait_for(runner.started.wait(), timeout=1)
            prompt.value = "stale queued prompt"
            await pilot.press("enter")
            self.assertTrue(app._queued_interjections)

            app.action_clear_transcript()
            self.assertFalse(app._queued_interjections)
            await pilot.press("ctrl+c")
            for _ in range(30):
                await pilot.pause(0.02)
                if runner.cancelled:
                    break

            self.assertEqual(runner.prompts, ["first prompt"])
            self.assertEqual(prompt.value, "")

    async def test_reasoning_delta_closes_the_pre_token_interjection_window(self) -> None:
        runner = CancellableTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "first prompt"
            await pilot.press("enter")
            await asyncio.wait_for(runner.started.wait(), timeout=1)
            await app._handle_event(
                AgentEvent.create(1, AgentEventKind.REASONING_DELTA, {"text": "thinking"})
            )
            prompt.value = "late prompt"
            await pilot.press("enter")

            self.assertFalse(app._queued_interjections)
            self.assertEqual(app.entries[-1].text, "A turn is already running.")
            await pilot.press("ctrl+c")

    async def test_cancel_command_cancels_without_starting_another_turn(self) -> None:
        runner = CancellableTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "long turn"
            await pilot.press("enter")
            await asyncio.wait_for(runner.started.wait(), timeout=1)

            prompt.value = "/cancel"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if runner.cancelled:
                    break

            self.assertTrue(runner.cancelled)
            self.assertEqual(runner.prompts, ["long turn"])

    async def test_provider_picker_switches_profile_without_rendering_credentials(self) -> None:
        runner = TuiConversation()
        profiles = ProfileTuiController()
        app = NeuroCodeApp(
            runner,
            provider_controller=ProviderChangeService(profiles),
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/provider"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, ProviderSelectionScreen):
                    break

            self.assertIsInstance(app.screen, ProviderSelectionScreen)
            missing = app.screen.query_one("#provider-choice-2", Button)
            self.assertTrue(missing.disabled)
            rendered_labels = "\n".join(str(button.label) for button in app.screen.query(Button))
            self.assertIn("credential missing", rendered_labels)
            clicked = await pilot.click("#provider-choice-1")
            self.assertTrue(clicked)
            for _ in range(20):
                await pilot.pause(0.01)
                if profiles.selections:
                    break

            self.assertEqual(profiles.selections, ["second"])
            self.assertIn("previous session old-session remains saved", app.entries[-1].text)
            self.assertIn(
                "second · second-model",
                str(app.query_one("#runtime-model", Static).renderable),
            )

            prompt.value = "/status"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("second/second-model", app.entries[-1].text)
            self.assertIn("Profile: second", app.entries[-1].text)

            await pilot.press("ctrl+p")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, ProviderSelectionScreen):
                    break
            self.assertIsInstance(app.screen, ProviderSelectionScreen)
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ProviderSelectionScreen)
            self.assertEqual(profiles.selections, ["second"])

    async def test_model_alias_selects_directly_and_switch_is_blocked_mid_turn(self) -> None:
        profiles = ProfileTuiController()
        direct_runner = TuiConversation()
        direct_app = NeuroCodeApp(
            direct_runner,
            provider_controller=profiles,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with direct_app.run_test(size=(100, 30)) as pilot:
            prompt = direct_app.query_one("#prompt", PromptInput)
            prompt.value = "/model second"
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(profiles.selections, ["second"])

        blocking_runner = CancellableTuiConversation()
        blocking_profiles = ProfileTuiController()
        blocking_app = NeuroCodeApp(
            blocking_runner,
            provider_controller=blocking_profiles,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with blocking_app.run_test(size=(100, 30)) as pilot:
            prompt = blocking_app.query_one("#prompt", PromptInput)
            prompt.value = "long turn"
            await pilot.press("enter")
            await asyncio.wait_for(blocking_runner.started.wait(), timeout=1)
            prompt.value = "/provider second"
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(blocking_profiles.selections, [])
            self.assertEqual(
                blocking_app.entries[-1].text,
                "Cannot switch provider while a turn is running.",
            )
            await pilot.press("ctrl+c")

    async def test_initial_session_history_replays_without_private_context_or_tool_output(
        self,
    ) -> None:
        controller = SessionTuiController(current_session="target-session-123456789")
        app = NeuroCodeApp(
            controller,
            session_controller=controller,
            initial_items=restored_history(),
            provider_name="second",
            model_name="second-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)) as pilot:
            await pilot.pause()

            rendered = "\n".join(entry.text for entry in app.entries)
            self.assertIn("restored prompt", rendered)
            self.assertIn("image content preserved in session", rendered)
            self.assertIn("Restored tool request: read_file.", rendered)
            self.assertIn("Restored result for read_file.", rendered)
            self.assertIn("restored response", rendered)
            self.assertIn("Resumed session target-session-123456789", rendered)
            self.assertNotIn("never render this", rendered)
            self.assertNotIn("private assistant reasoning", rendered)
            self.assertNotIn("private tool output", rendered)
            self.assertNotIn("private-image", rendered)

    async def test_workspace_session_picker_resumes_and_replaces_transcript(self) -> None:
        controller = SessionTuiController()
        app = NeuroCodeApp(
            controller,
            session_controller=controller,
            session_selection_service=SessionSelectionService(controller),
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(115, 35)) as pilot:
            await pilot.press("ctrl+r")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, SessionSelectionScreen):
                    break

            self.assertIsInstance(app.screen, SessionSelectionScreen)
            labels = "\n".join(str(button.label) for button in app.screen.query(Button))
            self.assertIn("current", labels)
            self.assertIn("target-sessi", labels)
            clicked = await pilot.click("#session-choice-1")
            self.assertTrue(clicked)
            for _ in range(20):
                await pilot.pause(0.01)
                if controller.selected:
                    break

            self.assertEqual(controller.selected, ["target-session-123456789"])
            rendered = "\n".join(entry.text for entry in app.entries)
            self.assertIn("restored prompt", rendered)
            self.assertIn("Previous session current-session remains saved", rendered)
            self.assertNotIn("Ready · first/first-model", rendered)

            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/status"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("second/second-model", app.entries[-1].text)
            self.assertIn("target-session-123456789", app.entries[-1].text)

            await pilot.press("ctrl+r")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, SessionSelectionScreen):
                    break
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, SessionSelectionScreen)

    async def test_sessions_command_searches_titles_and_content_before_opening_picker(self) -> None:
        controller = SessionTuiController()
        app = NeuroCodeApp(
            controller,
            session_controller=controller,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(120, 35)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/sessions quoted"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, SessionSelectionScreen):
                    break

            self.assertIsInstance(app.screen, SessionSelectionScreen)
            self.assertEqual(controller.queries, ["quoted"])
            self.assertEqual(app.screen.search_query, "quoted")
            title = app.screen.query_one("#session-title", Label)
            self.assertEqual(str(title.renderable), "Session search: quoted")
            self.assertNotIn("🔎", str(title.renderable))
            labels = "\n".join(str(button.label) for button in app.screen.query(Button))
            self.assertIn("Escaped quoted session", labels)
            self.assertIn("[quoted] content", labels)
            await pilot.press("escape")

    async def test_session_picker_debounces_live_search_and_replaces_options(self) -> None:
        controller = SessionTuiController()
        app = NeuroCodeApp(
            controller,
            session_controller=controller,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.press("ctrl+r")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, SessionSelectionScreen):
                    break

            self.assertIsInstance(app.screen, SessionSelectionScreen)
            search = app.screen.query_one("#session-search", Input)
            search.value = "quoted"
            await pilot.pause(0.05)
            self.assertEqual(controller.queries, [None])
            for _ in range(30):
                await pilot.pause(0.02)
                labels = "\n".join(str(button.label) for button in app.screen.query(Button))
                if (
                    controller.queries[-1:] == ["quoted"]
                    and "Current workspace session" not in labels
                    and "Escaped quoted session" in labels
                ):
                    break

            self.assertEqual(controller.queries, [None, "quoted"])
            labels = "\n".join(str(button.label) for button in app.screen.query(Button))
            self.assertIn("Escaped quoted session", labels)
            self.assertNotIn("Current workspace session", labels)
            title = app.screen.query_one("#session-title", Label)
            self.assertEqual(str(title.renderable), "Session search: quoted")
            await pilot.press("escape")

    async def test_rename_and_title_commands_update_the_current_session(self) -> None:
        controller = SessionTuiController()
        app = NeuroCodeApp(
            controller,
            session_controller=controller,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "/rename   Manual session title"
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(controller.renamed, ["  Manual session title"])
            self.assertIn("renamed to 'Manual session title'", app.entries[-1].text)

            prompt.value = "/title Alias title"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                controller.renamed,
                ["  Manual session title", "Alias title"],
            )
            self.assertIn("renamed to 'Alias title'", app.entries[-1].text)

    async def test_direct_session_resume_is_blocked_while_a_turn_is_running(self) -> None:
        runner = CancellableTuiConversation()
        sessions = SessionTuiController()
        app = NeuroCodeApp(
            runner,
            session_controller=sessions,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "long turn"
            await pilot.press("enter")
            await asyncio.wait_for(runner.started.wait(), timeout=1)

            prompt.value = "/resume target-session-123456789"
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(sessions.selected, [])
            self.assertEqual(
                app.entries[-1].text,
                "Cannot resume a session while a turn is running.",
            )
            prompt.value = "/rename Blocked title"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(sessions.renamed, [])
            self.assertEqual(
                app.entries[-1].text,
                "Cannot rename a session while a turn is running.",
            )
            await pilot.press("ctrl+c")


if __name__ == "__main__":
    unittest.main()
