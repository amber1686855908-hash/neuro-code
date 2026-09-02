"""TUI-facing application contracts.

面向 TUI 的应用层契约.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from neuro_code.application.memory.compaction_runtime import ContextCompactionCommandResult
from neuro_code.application.permissions.broker import ApprovalHandler
from neuro_code.application.providers.contracts import ProviderOption, ProviderSelectionResult
from neuro_code.application.providers.service import ChangeProviderRequest
from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.application.sessions.contracts import (
    InteractionModeSelectionResult,
    ReasoningEffortSelectionResult,
    SessionOption,
)
from neuro_code.application.sessions.selection import SessionSelectionController
from neuro_code.domain.background_tasks.models import (
    BackgroundTaskSnapshot,
    BackgroundWakeState,
)
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.messages import SessionItem
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.execution import TurnCancellationPolicy
from neuro_code.domain.plans import PlanComment, SessionPlan
from neuro_code.domain.session_tasks import SessionTask


class ConversationRunner(Protocol):
    @property
    def session_id(self) -> str | None: ...

    @property
    def items(self) -> tuple[SessionItem, ...]: ...

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
    ) -> AgentRunResult: ...

    async def run_background_wake(self, *, sink: EventSink | None = None) -> AgentRunResult: ...

    async def load_background_wake_state(self) -> BackgroundWakeState: ...

    async def save_background_wake_state(self, state: BackgroundWakeState) -> None: ...

    async def compact_now(self) -> ContextCompactionCommandResult: ...


class ApprovalController(Protocol):
    def set_handler(self, handler: ApprovalHandler | None) -> None: ...


class ProviderController(Protocol):
    @property
    def profiles(self) -> tuple[ProviderOption, ...]: ...

    @property
    def selected_profile(self) -> str: ...

    async def change_provider(self, request: ChangeProviderRequest) -> ProviderSelectionResult: ...


class ReasoningController(Protocol):
    @property
    def reasoning_effort(self) -> ReasoningEffort: ...

    @property
    def effective_reasoning_effort(self) -> ReasoningEffort: ...

    async def set_reasoning_effort(
        self,
        effort: ReasoningEffort,
    ) -> ReasoningEffortSelectionResult: ...


class InteractionModeController(Protocol):
    @property
    def interaction_mode(self) -> InteractionMode: ...

    @property
    def auto_mode_unrestricted(self) -> bool: ...

    async def set_interaction_mode(
        self,
        mode: InteractionMode,
    ) -> InteractionModeSelectionResult: ...


SessionController = SessionSelectionController


SessionSearchCallback = Callable[[str | None], Awaitable[tuple[SessionOption, ...]]]


class TaskController(Protocol):
    async def list_background_tasks(self) -> tuple[BackgroundTaskSnapshot, ...]: ...

    async def load_background_wake_state(self) -> BackgroundWakeState: ...

    async def save_background_wake_state(self, state: BackgroundWakeState) -> None: ...


class SessionTaskController(Protocol):
    async def list_session_tasks(self) -> tuple[SessionTask, ...]: ...

    async def get_session_task(self, task_id: str) -> SessionTask | None: ...


class PlanController(Protocol):
    @property
    def plan(self) -> SessionPlan | None: ...

    async def add_plan_comment(self, step_index: int, content: str) -> PlanComment: ...

    async def list_plan_comments(self) -> tuple[PlanComment, ...]: ...

    async def schedule_plan(self) -> SessionTask: ...

    async def execute_plan(
        self,
        *,
        sink: EventSink | None = None,
        task_id: str | None = None,
    ) -> AgentRunResult: ...

    async def run_session_task(
        self,
        task_id: str,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult: ...


__all__ = [
    "ApprovalController",
    "ConversationRunner",
    "InteractionModeController",
    "PlanController",
    "ProviderController",
    "ReasoningController",
    "SessionController",
    "SessionSearchCallback",
    "SessionTaskController",
    "TaskController",
]
