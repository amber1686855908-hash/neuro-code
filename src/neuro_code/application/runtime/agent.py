from __future__ import annotations

from collections.abc import Callable, Sequence

from neuro_code.application.permissions.policy import PermissionManager, PermissionMode
from neuro_code.application.ports.approval import PermissionApprover
from neuro_code.application.ports.model import ModelProvider
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.tools import ToolCollection, ToolContext
from neuro_code.application.ports.workspace_changes import WorkspaceChangeObserver
from neuro_code.application.runtime.agent_loop import (
    AgentLoopRunner,
    AgentRunResult,
    EventSink,
)
from neuro_code.application.runtime.context_builder import ContextBuilder
from neuro_code.application.runtime.finalization import (
    AgentFinalizer,
    Finalizer,
)
from neuro_code.application.runtime.supervision import (
    AgentExecutionSupervisor,
    ExecutionControlMode,
    SupervisionObserver,
    create_observing_supervisor,
)
from neuro_code.application.runtime.tool_pipeline import ToolExecutor
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.messages import (
    ContentPart,
    SessionItem,
)
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.execution import (
    TurnCancellationPolicy,
    TurnSource,
)
from neuro_code.domain.plans import PlanComment, SessionPlan
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.workspace.instructions import InstructionDiscoveryResult
from neuro_code.domain.workspace.skills import SkillDiscoveryResult

__all__ = ["AgentRunResult", "AgentRuntime", "EventSink"]

FinalizerFactory = Callable[[ModelProvider, int, tuple[str, ...]], Finalizer]


def _create_finalizer(
    provider: ModelProvider,
    max_attempts: int,
    redaction_values: tuple[str, ...],
) -> AgentFinalizer:
    return AgentFinalizer(
        provider,
        max_attempts=max_attempts,
        redaction_values=redaction_values,
    )


DEFAULT_SYSTEM_PROMPT = """You are Neuro Code, a terminal coding agent.
Use tools when repository evidence is needed. Read before editing. Never claim a
tool action succeeded unless its result confirms success. Keep the final answer
concise and state which files or checks changed. Prefer workspace edit tools over
shell redirection when changing files so the resulting changes remain auditable."""


class AgentRuntime:
    def __init__(
        self,
        *,
        provider: ModelProvider,
        tools: ToolCollection,
        workspace_change_observer: WorkspaceChangeObserver,
        permissions: PermissionManager,
        tool_context: ToolContext,
        approver: PermissionApprover | None = None,
        session_store: SessionStore | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_steps: int = 24,
        reasoning_effort: ReasoningEffort = ReasoningEffort.HIGH,
        interaction_mode: InteractionMode | None = None,
        instruction_provider: Callable[[], InstructionDiscoveryResult | None] | None = None,
        skill_provider: Callable[[], SkillDiscoveryResult | None] | None = None,
        plan: SessionPlan | None = None,
        plan_comments: Sequence[PlanComment] = (),
        supervisor_factory: Callable[[], AgentExecutionSupervisor] | None = None,
        supervision_observer: SupervisionObserver | None = None,
        execution_control_mode: ExecutionControlMode = ExecutionControlMode.OBSERVE_ONLY,
        finalizer_factory: FinalizerFactory | None = None,
        finalizer_max_attempts: int = 2,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        if not isinstance(execution_control_mode, ExecutionControlMode):
            raise TypeError("execution_control_mode must be an ExecutionControlMode")
        if (
            not isinstance(finalizer_max_attempts, int)
            or isinstance(finalizer_max_attempts, bool)
            or finalizer_max_attempts < 1
        ):
            raise ValueError("finalizer_max_attempts must be a positive integer")
        self._provider = provider
        self._tools = tools
        self._workspace_change_observer = workspace_change_observer
        self._permissions = permissions
        self._tool_context = tool_context
        self._approver = approver
        self._session_store = session_store
        self._system_prompt = system_prompt
        self._max_steps = max_steps
        self._supervisor_factory = supervisor_factory or create_observing_supervisor
        self._supervision_observer = supervision_observer
        self._execution_control_mode = execution_control_mode
        self._finalizer_factory = finalizer_factory or _create_finalizer
        self._finalizer_max_attempts = finalizer_max_attempts
        self._auto_permission_mode = (
            PermissionMode.BYPASS
            if permissions.mode is PermissionMode.BYPASS
            else PermissionMode.ACCEPT_EDITS
        )
        inferred_mode = {
            PermissionMode.DEFAULT: InteractionMode.NORMAL,
            PermissionMode.ACCEPT_EDITS: InteractionMode.ACCEPT_EDITS,
            PermissionMode.DONT_ASK: InteractionMode.PLAN,
            PermissionMode.BYPASS: InteractionMode.AUTO,
        }[permissions.mode]
        self._context_builder = ContextBuilder(
            reasoning_effort=reasoning_effort,
            interaction_mode=interaction_mode or inferred_mode,
            plan=plan,
            instruction_provider=instruction_provider,
            skill_provider=skill_provider,
        )
        self._context_builder.set_plan_comments(plan_comments)
        self._tool_executor = ToolExecutor(
            tools=self._tools,
            permissions=self._permissions,
            approver=self._approver,
            tool_context=self._tool_context,
            session_store=self._session_store,
            workspace_change_observer=self._workspace_change_observer,
            context_builder=self._context_builder,
        )
        self._loop_runner = AgentLoopRunner(
            provider=self._provider,
            tools=self._tools,
            tool_context=self._tool_context,
            session_store=self._session_store,
            system_prompt=self._system_prompt,
            max_steps=self._max_steps,
            context_builder=self._context_builder,
            supervisor_factory=self._supervisor_factory,
            supervision_observer=self._supervision_observer,
            execution_control_mode=self._execution_control_mode,
            finalizer_factory=self._finalizer_factory,
            finalizer_max_attempts=self._finalizer_max_attempts,
            tool_executor=self._tool_executor,
        )
        self._apply_interaction_mode_permissions()

    @property
    def sandbox_profile(self) -> SandboxProfile:
        return self._tool_context.sandbox_profile

    @property
    def reasoning_effort(self) -> ReasoningEffort:
        return self._context_builder.reasoning_effort

    def set_reasoning_effort(self, effort: ReasoningEffort) -> None:
        self._context_builder.set_reasoning_effort(effort)

    @property
    def interaction_mode(self) -> InteractionMode:
        return self._context_builder.interaction_mode

    @property
    def auto_mode_unrestricted(self) -> bool:
        return self._auto_permission_mode is PermissionMode.BYPASS

    @property
    def plan(self) -> SessionPlan | None:
        return self._context_builder.plan

    @property
    def plan_comments(self) -> tuple[PlanComment, ...]:
        return self._context_builder.plan_comments

    def set_plan(self, plan: SessionPlan | None) -> None:
        self._context_builder.set_plan(plan)

    def set_plan_comments(self, comments: Sequence[PlanComment]) -> None:
        self._context_builder.set_plan_comments(comments)

    def set_interaction_mode(self, mode: InteractionMode) -> None:
        self._context_builder.set_interaction_mode(mode)
        self._apply_interaction_mode_permissions()

    def _apply_interaction_mode_permissions(self) -> None:
        permission_mode = {
            InteractionMode.NORMAL: PermissionMode.DEFAULT,
            InteractionMode.ACCEPT_EDITS: PermissionMode.ACCEPT_EDITS,
            InteractionMode.PLAN: PermissionMode.DONT_ASK,
            InteractionMode.AUTO: self._auto_permission_mode,
        }[self._context_builder.interaction_mode]
        self._permissions.set_mode(permission_mode)

    def _model_items_with_reasoning_guidance(
        self,
        items: Sequence[SessionItem],
    ) -> tuple[SessionItem, ...]:
        """Apply the selected policy to a request without persisting control text.

        Reasoning effort and interaction mode guidance are appended to the
        system message.  Repository AGENTS.md instructions are injected as a
        separate synthetic ``User`` message tagged with
        ``SyntheticReason.PROJECT_INSTRUCTIONS``, placed after the system
        message and before the first genuine user message.  This follows the
        Rust baseline's ``ProjectInstructions`` synthetic user item pattern:
        the instruction content never masquerades as a system or genuine user
        message.

        将选定策略应用到请求,但不持久化控制文本. 仓库指令作为标记为合成原因的独立 User 消息注入.
        """

        return self._context_builder.build(items)

    @property
    def instruction_result(self) -> InstructionDiscoveryResult | None:
        """Return the most recent instruction discovery result, if any.

        返回最近一次指令发现结果,如果存在."""
        return self._context_builder.instruction_result

    @property
    def skill_result(self) -> SkillDiscoveryResult | None:
        """Return the most recent skill discovery result, if any.

        返回最近一次技能发现结果,如果存在."""
        return self._context_builder.skill_result

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        content_parts: Sequence[ContentPart] = (),
        plan_execution_requested: bool = False,
        plan_execution_task_id: str | None = None,
        initial_items: Sequence[SessionItem] = (),
        source_provider: str | None = None,
        source_model: str | None = None,
        source_context_affinity: str | None = None,
        session_id: str | None = None,
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
        turn_source: TurnSource = TurnSource.USER,
    ) -> AgentRunResult:
        """Run one agent turn through the canonical main loop.

        通过规范主循环运行一个 Agent 回合."""

        return await self._loop_runner.run(
            prompt,
            sink=sink,
            content_parts=content_parts,
            plan_execution_requested=plan_execution_requested,
            plan_execution_task_id=plan_execution_task_id,
            initial_items=initial_items,
            source_provider=source_provider,
            source_model=source_model,
            source_context_affinity=source_context_affinity,
            session_id=session_id,
            cancellation_policy=cancellation_policy,
            turn_source=turn_source,
        )
