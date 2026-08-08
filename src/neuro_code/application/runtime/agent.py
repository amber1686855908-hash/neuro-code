from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from neuro_code.application.memory.compaction import ProviderContextWindow
from neuro_code.application.memory.compaction_runtime import (
    ContextCompactionRuntimeBoundary,
    ContextCompactionRuntimeGate,
    ContextCompactionRuntimeRequest,
    ContextCompactionRuntimeResult,
)
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
from neuro_code.domain.conversation.context import ModelContext, estimate_context_tokens
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
from neuro_code.shared.errors import ConfigurationError

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
        compaction_runtime_gate: ContextCompactionRuntimeGate | None = None,
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
        if compaction_runtime_gate is not None and not isinstance(
            compaction_runtime_gate,
            ContextCompactionRuntimeGate,
        ):
            raise TypeError(
                "compaction_runtime_gate must be a ContextCompactionRuntimeGate or None"
            )
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
        self._compaction_runtime_gate = compaction_runtime_gate
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

    async def trigger_context_compaction(
        self,
        request: ContextCompactionRuntimeRequest,
    ) -> ContextCompactionRuntimeResult:
        """Run one explicitly supplied compaction request at the injected gate.

        The gate is absent by default, so normal turns never trigger
        compaction.  The caller must supply a complete safe-boundary request;
        this method does not derive thresholds, mutate the current context,
        emit events, or persist an execution record.

        在注入的门控处运行一次调用方显式提供的压缩请求。

        默认没有门控,因此普通回合永远不会触发压缩。调用方必须提供完整的安全边界请求;本方法不会推导阈值、修改当前上下文、发出事件或持久化执行记录。
        """

        if not isinstance(request, ContextCompactionRuntimeRequest):
            raise TypeError("request must be a ContextCompactionRuntimeRequest")
        if self._compaction_runtime_gate is None:
            raise ConfigurationError("runtime context compaction is not configured")
        return await self._compaction_runtime_gate.trigger(request)

    def build_context_snapshot(
        self,
        items: Sequence[SessionItem],
        *,
        source_provider: str | None = None,
        source_model: str | None = None,
        source_context_affinity: str | None = None,
    ) -> ModelContext:
        """Build a request-scoped context snapshot without persisting it.

        The same guidance and instruction/skill injection used by model steps
        is applied, but the returned immutable context is owned by the caller.
        This is an explicit application seam; normal ``run`` does not call it.

        构建一次请求范围的上下文快照但不持久化。
        使用与模型步骤相同的指引及指令/技能注入,但返回的不可变上下文由调用方持有。
        这是显式应用接缝,普通 ``run`` 不会调用它。
        """

        if not isinstance(items, Sequence):
            raise TypeError("items must be a sequence of SessionItem values")
        model_items = self._model_items_with_reasoning_guidance(tuple(items))
        return ModelContext(
            tuple(model_items),
            source_provider=source_provider,
            source_model=source_model,
            source_context_affinity=source_context_affinity,
            reasoning_effort=self.reasoning_effort,
        )

    def build_explicit_context_compaction_request(
        self,
        *,
        context: ModelContext,
        boundary: ContextCompactionRuntimeBoundary,
        provider_window: ProviderContextWindow | None,
        protected_item_count: int = 0,
        reported_input_tokens: int | None = None,
        reported_output_tokens: int | None = None,
        session_id: str | None = None,
        compaction_id: str | None = None,
        created_at: datetime | None = None,
        token_estimator: Callable[[Sequence[SessionItem]], int] = estimate_context_tokens,
    ) -> ContextCompactionRuntimeRequest:
        """Build an explicit compaction request through the configured gate.

        Request construction is deterministic and side-effect free. A missing
        gate fails closed rather than falling back to a normal model request.

        通过已配置的门控构建显式压缩请求。
        请求构建是确定性的且无副作用。缺少门控时会安全失败,不会退回普通模型请求。
        """

        if self._compaction_runtime_gate is None:
            raise ConfigurationError("runtime context compaction is not configured")
        return self._compaction_runtime_gate.build_explicit_request(
            context=context,
            boundary=boundary,
            provider_window=provider_window,
            protected_item_count=protected_item_count,
            reported_input_tokens=reported_input_tokens,
            reported_output_tokens=reported_output_tokens,
            session_id=session_id,
            compaction_id=compaction_id,
            created_at=created_at,
            token_estimator=token_estimator,
        )
