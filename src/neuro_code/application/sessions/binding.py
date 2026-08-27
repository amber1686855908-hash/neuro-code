"""Typed application boundary for one active session binding.

定义当前活动会话绑定的类型化应用边界.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, TypeVar

from neuro_code.application.memory.compaction import ProviderContextWindow
from neuro_code.application.memory.compaction_runtime import (
    ContextCompactionCommandResult,
    ContextCompactionRuntimeBoundary,
    ContextCompactionRuntimeRequest,
    ContextCompactionRuntimeResult,
    ContextCompactionTurnProjection,
)
from neuro_code.application.ports.background_tasks import BackgroundTaskManager
from neuro_code.application.ports.model import ModelProvider
from neuro_code.application.ports.tools import Tool
from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.application.sessions.recovery import TurnRecoveryInspection
from neuro_code.application.workflows.subagent_capabilities import SubagentCapabilitySet
from neuro_code.domain.background_tasks.models import BackgroundWakeState
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.messages import ContentPart, SessionItem
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.execution import TurnCancellationPolicy, TurnSource
from neuro_code.domain.plans import PlanComment, SessionPlan
from neuro_code.domain.session_tasks import SessionTask
from neuro_code.domain.ultracode import UltracodeDelegationDecision

_T = TypeVar("_T")


class ConversationBindingResourceScope:
    """Own and close ephemeral resources attached to one conversation binding.

    The shared close task makes cleanup asynchronous, idempotent, and resilient
    to cancellation of any individual caller. Durable session or workspace
    state is deliberately outside this scope.
    """

    __slots__ = ("_close_callback", "_close_lock", "_close_task")

    def __init__(self, close_callback: Callable[[], Awaitable[None]]) -> None:
        if not callable(close_callback):
            raise TypeError("conversation binding close callback must be callable")
        self._close_callback = close_callback
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    async def _run_close_callback(self) -> None:
        await self._close_callback()

    async def close(self) -> None:
        async with self._close_lock:
            if self._close_task is None:
                self._close_task = asyncio.create_task(
                    self._run_close_callback(),
                    name="conversation-binding-close",
                )
            close_task = self._close_task
        await asyncio.shield(close_task)


class ConversationRunner(Protocol):
    """Minimum session runner contract required by application consumers.

    表示应用消费者所需的最小会话运行器契约.
    """

    @property
    def session_id(self) -> str | None: ...

    @property
    def reasoning_effort(self) -> ReasoningEffort: ...

    @property
    def items(self) -> tuple[SessionItem, ...]: ...

    @property
    def plan(self) -> SessionPlan | None: ...

    @property
    def plan_comments(self) -> tuple[PlanComment, ...]: ...

    async def add_plan_comment(self, step_index: int, content: str) -> PlanComment: ...

    async def list_plan_comments(self) -> tuple[PlanComment, ...]: ...

    async def list_session_tasks(self) -> tuple[SessionTask, ...]: ...

    async def get_session_task(self, task_id: str) -> SessionTask | None: ...

    async def inspect_recovery(self) -> tuple[TurnRecoveryInspection, ...]: ...

    async def abandon_recovery(
        self,
        turn_id: str,
        *,
        reason: str = "explicit_user_resolution",
    ) -> TurnRecoveryInspection: ...

    async def retry_recovery(
        self,
        turn_id: str,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult: ...

    def set_reasoning_effort(self, effort: ReasoningEffort) -> None: ...

    @property
    def interaction_mode(self) -> InteractionMode: ...

    @property
    def auto_mode_unrestricted(self) -> bool: ...

    def set_interaction_mode(self, mode: InteractionMode) -> None: ...

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        content_parts: Sequence[ContentPart] = (),
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
        turn_source: TurnSource = TurnSource.USER,
        turn_id: str | None = None,
        ultracode_execution_id: str | None = None,
    ) -> AgentRunResult: ...

    async def ensure_persisted_session(self) -> str: ...

    async def commit_external_turn(
        self,
        prompt: str,
        *,
        response: str,
        turn_id: str,
        execution_id: str,
        decision: UltracodeDelegationDecision,
        content_parts: Sequence[ContentPart] = (),
        sink: EventSink | None = None,
    ) -> AgentRunResult: ...

    async def trigger_context_compaction(
        self,
        request: ContextCompactionRuntimeRequest,
    ) -> ContextCompactionRuntimeResult: ...

    async def compact_now(self) -> ContextCompactionCommandResult: ...

    def replace_external_tools(
        self,
        tools: Sequence[Tool],
        previous_names: Sequence[str],
    ) -> None: ...

    async def run_context_compaction_with_owner(
        self,
        request: ContextCompactionRuntimeRequest,
        owner: Callable[[ContextCompactionTurnProjection], Awaitable[_T]],
    ) -> _T: ...

    async def run_explicit_context_compaction_with_owner(
        self,
        *,
        boundary: ContextCompactionRuntimeBoundary,
        provider_window: ProviderContextWindow | None,
        owner: Callable[[ContextCompactionTurnProjection], Awaitable[_T]],
        protected_item_count: int = 0,
        reported_input_tokens: int | None = None,
        reported_output_tokens: int | None = None,
        compaction_id: str | None = None,
        created_at: datetime | None = None,
    ) -> _T: ...

    async def run_background_wake(self, *, sink: EventSink | None = None) -> AgentRunResult: ...

    async def load_background_wake_state(self) -> BackgroundWakeState: ...

    async def save_background_wake_state(self, state: BackgroundWakeState) -> None: ...

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


@dataclass(frozen=True, slots=True)
class ConversationBinding:
    """Bind a runner, provider, and optional background-task scope.

    绑定运行器、Provider 和可选的后台任务作用域.
    """

    runner: ConversationRunner
    provider: ModelProvider
    background_tasks: BackgroundTaskManager | None = None
    capabilities: SubagentCapabilitySet | None = None
    resource_scope: ConversationBindingResourceScope | None = field(
        default=None,
        kw_only=True,
    )

    async def close(self) -> None:
        """Close ephemeral resources owned by this binding."""

        if self.resource_scope is not None:
            await self.resource_scope.close()
            return
        if self.background_tasks is not None:
            await self.background_tasks.shutdown()


__all__ = [
    "ConversationBinding",
    "ConversationRunner",
]
