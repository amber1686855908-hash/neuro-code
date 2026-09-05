"""Agent main loop orchestrator.

Stage 3F (final Runtime Kernel slice): this module owns the per-turn step
loop, supervision checkpoint sequence, batch decisions, finalization
orchestration, and the turn result value.  ``AgentRuntime.run()`` becomes a
thin delegate; event ordering, tool result pairing, cancellation, transaction,
and batch boundaries remain unchanged.

The module intentionally does not import :mod:`agent`; it depends only on
ports, domain values, runtime collaborators, and supervision/finalization
primitives.

负责一个 Agent 回合的主循环编排.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic

from neuro_code.application.execution_policy import ExecutionSegmentPolicy
from neuro_code.application.memory.compaction import (
    CompactionResumeRebuilder,
    ContextCompactionDecision,
    ProviderContextWindow,
    rebuild_context_from_latest_compatible_compaction,
)
from neuro_code.application.memory.compaction_runtime import (
    ContextCompactionRuntimeBoundary,
    ContextCompactionRuntimeGate,
    ContextCompactionSafePoint,
    ContextCompactionTimeoutError,
)
from neuro_code.application.ports.model import ModelProvider
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.tools import (
    InteractionControlTool,
    ToolCollection,
    ToolContext,
)
from neuro_code.application.ports.workspace_changes import WorkspaceChangeReport
from neuro_code.application.runtime.background_task_reminders import (
    BACKGROUND_TASK_COMPLETION_BATCH_LIMIT,
    format_background_task_completion_reminder,
)
from neuro_code.application.runtime.context_builder import ContextBuilder
from neuro_code.application.runtime.event_recorder import TurnEventRecorder
from neuro_code.application.runtime.final_response import (
    FinalResponseContract,
    ResponseSource,
)
from neuro_code.application.runtime.finalization import (
    FinalizationEvidence,
    FinalizationResult,
    FinalizationStatus,
    Finalizer,
    deterministic_fallback_result,
)
from neuro_code.application.runtime.model_step import ModelStepProcessor
from neuro_code.application.runtime.supervision import (
    AgentExecutionSupervisor,
    ExecutionControlMode,
    SupervisionCheckpoint,
    SupervisionObserver,
    SupervisionTraceRecord,
    ToolExecutionObservation,
)
from neuro_code.application.runtime.tool_pipeline import ToolExecutor
from neuro_code.application.runtime.tool_scheduler import (
    ToolBatchExecutionError,
    ToolScheduler,
)
from neuro_code.application.runtime.verification import (
    VerificationReport,
    VerificationTracker,
)
from neuro_code.application.sessions.lifecycle import (
    SessionLifecycleService,
    StartSessionRequest,
)
from neuro_code.application.sessions.task_queries import (
    GetSessionTaskRequest,
    SessionTaskQueryService,
)
from neuro_code.domain.background_tasks.models import BackgroundTaskSnapshot
from neuro_code.domain.conversation.compaction import DurableCompactionItem
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.messages import (
    ContentPart,
    Message,
    PreservedContextItem,
    Role,
    SessionItem,
    SyntheticReason,
    ToolCall,
)
from neuro_code.domain.conversation.request import ModelRequestSnapshot
from neuro_code.domain.execution import (
    AgentExecutionOutcome,
    AgentExecutionStatus,
    ExecutionBudget,
    ExecutionBudgetPressure,
    ExecutionCounters,
    ExecutionSegmentCheckpoint,
    ProgressKind,
    SupervisorDecision,
    SupervisorDecisionKind,
    SupervisorReasonCode,
    TurnCancellationPolicy,
    TurnInput,
    TurnRecoveryAttempt,
    TurnSource,
)
from neuro_code.domain.plans import PlanStepStatus, SessionPlan
from neuro_code.domain.session_tasks import SessionTask, SessionTaskKind, SessionTaskStatus
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import ConfigurationError, ProviderError
from neuro_code.shared.redaction import redact_sensitive_arguments, redact_sensitive_text

LOGGER = logging.getLogger(__name__)

EventSink = Callable[[AgentEvent], Awaitable[None] | None]


def _redacted_persisted_tool_calls(
    calls: Sequence[ToolCall],
    redaction_values: tuple[str, ...],
) -> tuple[ToolCall, ...]:
    """Keep externally sent web-search queries redacted in persisted context."""

    return tuple(
        ToolCall(
            call.id,
            call.name,
            redact_sensitive_arguments(
                call.arguments,
                explicit_values=redaction_values,
            ),
            call.metadata,
        )
        if call.name == "web_search"
        else call
        for call in calls
    )


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    session_id: str | None
    response: str
    messages: tuple[Message, ...]
    items: tuple[SessionItem, ...]
    events: tuple[AgentEvent, ...]
    steps: int
    plan: SessionPlan | None = None
    outcome: AgentExecutionOutcome | None = None
    turn_id: str | None = None
    verification: VerificationReport | None = None
    response_contract: FinalResponseContract | None = None

    def __post_init__(self) -> None:
        contract = self.response_contract
        if contract is None:
            contract = FinalResponseContract.committed(
                self.response,
                source=ResponseSource.NORMAL_MODEL,
                verification=self.verification,
            )
            object.__setattr__(self, "response_contract", contract)
        if not isinstance(contract, FinalResponseContract):
            raise TypeError("response_contract must be a FinalResponseContract or None")
        if not contract.is_committed:
            raise ValueError("AgentRunResult requires a committed final response")
        if contract.response != self.response:
            raise ValueError("response_contract response must match AgentRunResult.response")
        if self.verification is not None and (
            contract.verification_state is not self.verification.state
            or contract.verification_workspace_generation != self.verification.workspace_generation
        ):
            raise ValueError("response_contract verification projection is out of sync")


@dataclass(frozen=True, slots=True)
class _ScheduledToolOutcome:
    observation: ToolExecutionObservation | None
    messages: tuple[Message, ...] = ()
    context_items: tuple[SessionItem, ...] = ()


class AgentLoopRunner:
    """Own one agent turn's step loop and finalization orchestration.

    管理一个 Agent 回合的步骤循环和最终化编排."""

    __slots__ = (
        "_compaction_runtime_gate",
        "_context_builder",
        "_execution_budget",
        "_execution_control_mode",
        "_final_output_gate_enabled",
        "_finalizer_factory",
        "_finalizer_max_attempts",
        "_max_steps",
        "_provider",
        "_provider_context_window",
        "_segment_policy",
        "_session_store",
        "_supervision_observer",
        "_supervisor_factory",
        "_system_prompt",
        "_tool_context",
        "_tool_executor",
        "_tool_scheduler",
        "_tools",
    )

    def __init__(
        self,
        *,
        provider: ModelProvider,
        tools: ToolCollection,
        tool_context: ToolContext,
        session_store: SessionStore | None,
        system_prompt: str,
        execution_budget: ExecutionBudget,
        context_builder: ContextBuilder,
        supervisor_factory: Callable[[], AgentExecutionSupervisor],
        supervision_observer: SupervisionObserver | None,
        execution_control_mode: ExecutionControlMode,
        finalizer_factory: Callable[[ModelProvider, int, tuple[str, ...]], Finalizer],
        finalizer_max_attempts: int,
        tool_executor: ToolExecutor,
        compaction_runtime_gate: ContextCompactionRuntimeGate | None,
        provider_context_window: ProviderContextWindow | None,
        final_output_gate_enabled: bool = True,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._tool_context = tool_context
        self._session_store = session_store
        self._system_prompt = system_prompt
        if not isinstance(execution_budget, ExecutionBudget):
            raise TypeError("execution_budget must be an ExecutionBudget")
        if compaction_runtime_gate is not None and not isinstance(
            compaction_runtime_gate,
            ContextCompactionRuntimeGate,
        ):
            raise TypeError(
                "compaction_runtime_gate must be a ContextCompactionRuntimeGate or None"
            )
        if provider_context_window is not None and not isinstance(
            provider_context_window,
            ProviderContextWindow,
        ):
            raise TypeError("provider_context_window must be a ProviderContextWindow or None")
        if not isinstance(final_output_gate_enabled, bool):
            raise TypeError("final_output_gate_enabled must be a bool")
        self._execution_budget = execution_budget
        self._max_steps = execution_budget.max_model_calls
        self._segment_policy = ExecutionSegmentPolicy.from_budget(execution_budget)
        self._context_builder = context_builder
        self._supervisor_factory = supervisor_factory
        self._supervision_observer = supervision_observer
        self._execution_control_mode = execution_control_mode
        self._final_output_gate_enabled = final_output_gate_enabled
        self._finalizer_factory = finalizer_factory
        self._finalizer_max_attempts = finalizer_max_attempts
        self._tool_executor = tool_executor
        self._tool_scheduler: ToolScheduler[_ScheduledToolOutcome] = ToolScheduler(tools)
        self._compaction_runtime_gate = compaction_runtime_gate
        self._provider_context_window = provider_context_window

    @property
    def provider_context_window(self) -> ProviderContextWindow | None:
        return self._provider_context_window

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
        turn_id: str | None = None,
        ultracode_execution_id: str | None = None,
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
        turn_source: TurnSource = TurnSource.USER,
        verification_required: bool = False,
    ) -> AgentRunResult:
        prompt_parts = tuple(content_parts)
        if ultracode_execution_id is not None and (
            not isinstance(ultracode_execution_id, str)
            or not ultracode_execution_id.strip()
            or len(ultracode_execution_id.encode("utf-8")) > 128
        ):
            raise ValueError("ultracode execution id must be a bounded non-empty identifier")
        if not isinstance(turn_source, TurnSource):
            raise TypeError("turn_source must be a TurnSource")
        if turn_source is TurnSource.USER and not prompt.strip() and not prompt_parts:
            raise ValueError("prompt must not be empty")
        if turn_source is TurnSource.BACKGROUND_TASK_AUTO_WAKE and (prompt.strip() or prompt_parts):
            raise ValueError("background task auto-wake must not include a user prompt")
        if plan_execution_requested and self._context_builder.plan is None:
            raise ConfigurationError("cannot execute a plan that has not been saved")
        if plan_execution_task_id is not None and not plan_execution_requested:
            raise ConfigurationError("a session task requires plan execution")
        if not isinstance(cancellation_policy, TurnCancellationPolicy):
            raise TypeError("cancellation_policy must be a TurnCancellationPolicy")
        if not isinstance(verification_required, bool):
            raise TypeError("verification_required must be a bool")
        # Until suspended-task resume exists, one run is exactly one logical
        # user task.  A future task coordinator must move this reset to task
        # creation so resuming another execution segment preserves the first
        # mutation baseline.
        # 在支持挂起任务恢复之前,一次 run 正好对应一个逻辑用户任务. 未来任务协调器
        # 必须把重置移到任务创建处,使恢复新的执行段时保留首次变更基线.
        if self._tool_context.workspace_change_journal is not None:
            self._tool_context.workspace_change_journal.begin_task()
        verification_tracker = VerificationTracker(
            verification_required=verification_required,
        )
        turn_started_at = monotonic()
        context_items = list(initial_items)
        messages = [item for item in context_items if isinstance(item, Message)]
        context_source_provider = source_provider
        context_source_model = source_model
        context_source_affinity = source_context_affinity
        persist_turn_context = turn_source is TurnSource.USER
        can_adopt_provider_origin = not any(
            isinstance(item, PreservedContextItem) for item in context_items
        )
        if not messages:
            system_message = Message(Role.SYSTEM, self._system_prompt)
            context_items.append(system_message)
            messages.append(system_message)
        turn_context_prefix = tuple(context_items)
        pristine_cancel_eligible = cancellation_policy is TurnCancellationPolicy.REWIND_PRISTINE
        events: list[AgentEvent] = []
        sequence = 0

        background_tasks = self._tool_context.background_tasks
        if turn_source is TurnSource.BACKGROUND_TASK_AUTO_WAKE:
            if background_tasks is None:
                raise ConfigurationError("background task auto-wake is unavailable")
            pending_completions = await background_tasks.pending_completions()
            if not pending_completions:
                raise ConfigurationError("no pending background task completion is available")

        if self._session_store is not None and session_id is None:
            started_session = await SessionLifecycleService(self._session_store).start_session(
                StartSessionRequest(
                    str(self._tool_context.cwd),
                    self._provider.provider_name,
                    self._provider.model_name,
                    getattr(self._provider, "context_affinity", None),
                    self._tool_context.sandbox_profile,
                )
            )
            session_id = started_session.id
        elif self._session_store is not None and session_id is not None:
            sequence = await self._session_store.next_event_sequence(session_id) - 1
        if plan_execution_requested and (self._session_store is None or session_id is None):
            raise ConfigurationError("session-backed task storage is unavailable")
        if plan_execution_requested and self._context_builder.plan is None:
            raise ConfigurationError("cannot execute a plan that has not been saved")
        session_task: SessionTask | None = None
        if self._session_store is not None and session_id is not None:
            open_attempts = await self._session_store.load_open_turn_attempts(session_id)
            if open_attempts:
                raise ConfigurationError(
                    "session has an unresolved interrupted turn; explicitly abandon it "
                    "before starting another turn"
                )
        queued_plan_task: SessionTask | None = None
        if plan_execution_requested:
            assert self._session_store is not None
            assert session_id is not None
            if plan_execution_task_id is None:
                session_task = SessionTask(
                    f"task-{uuid.uuid4().hex}",
                    SessionTaskKind.PLAN_EXECUTION,
                    SessionTaskStatus.RUNNING,
                    datetime.now(UTC),
                    plan_snapshot=self._context_builder.plan,
                )
            else:
                queued_plan_task = await SessionTaskQueryService(
                    self._session_store
                ).get_session_task(GetSessionTaskRequest(session_id, plan_execution_task_id))
                if queued_plan_task is None:
                    raise ConfigurationError(f"unknown queued plan task: {plan_execution_task_id}")
                if queued_plan_task.kind is not SessionTaskKind.PLAN_EXECUTION:
                    raise ConfigurationError("only plan execution tasks can be started")
                if queued_plan_task.status is not SessionTaskStatus.QUEUED:
                    raise ConfigurationError(f"plan task {plan_execution_task_id} is not queued")
                if queued_plan_task.plan_snapshot != self._context_builder.plan:
                    raise ConfigurationError(
                        f"plan task {plan_execution_task_id} does not match the saved plan"
                    )

        if self._session_store is not None and session_id is not None:
            turn_id = turn_id or f"turn-{uuid.uuid4().hex}"
            turn_input = TurnInput(
                prompt,
                prompt_parts,
                turn_source,
                plan_execution_requested,
                session_task.task_id if session_task is not None else plan_execution_task_id,
            )
            attempt = TurnRecoveryAttempt.create(
                turn_id=turn_id,
                session_id=session_id,
                input=turn_input,
                task_id=(
                    session_task.task_id
                    if session_task is not None
                    else queued_plan_task.task_id
                    if queued_plan_task is not None
                    else None
                ),
                accepted_at=datetime.now(UTC),
            )
            if plan_execution_requested:
                if queued_plan_task is None:
                    assert session_task is not None
                    session_task = await self._session_store.start_plan_turn_attempt(
                        attempt,
                        task=session_task,
                    )
                else:
                    session_task = await self._session_store.start_plan_turn_attempt(
                        attempt,
                        queued_task_id=queued_plan_task.task_id,
                        started_at=datetime.now(UTC),
                    )
            else:
                await self._session_store.start_turn_attempt(attempt)

        recorder = TurnEventRecorder(
            sink=sink,
            session_store=self._session_store,
            session_id=session_id,
            turn_source=turn_source,
            turn_started_at=turn_started_at,
            persist_turn_context=persist_turn_context,
            turn_context_prefix=turn_context_prefix,
            context_items=context_items,
            events=events,
            sequence=sequence,
            session_task=session_task,
            pristine_cancel_eligible=pristine_cancel_eligible,
            turn_id=turn_id,
        )
        emit = recorder.emit
        record_turn_failure = recorder.record_turn_failure
        finalize_turn_completion = recorder.finalize_turn_completion

        supervisor: AgentExecutionSupervisor | None = None
        active_provider_window = self._provider_context_window
        active_compaction_item: DurableCompactionItem | None = None
        has_completed_model_step = False
        segment_number = 1
        segment_start_counters = ExecutionCounters()
        segment_progress_kinds: set[ProgressKind] = set()
        last_runtime_plan_content: str | None = None
        last_budget_pressure: ExecutionBudgetPressure | None = None
        replan_notice_active = False

        def disable_supervision(failure: str, error: Exception | None = None) -> None:
            nonlocal supervisor
            LOGGER.debug(
                "supervision disabled failure=%s error_type=%s",
                failure,
                type(error).__name__ if error is not None else "none",
            )
            supervisor = None

        def record_supervision(
            checkpoint: SupervisionCheckpoint,
            model_step: int,
            operation: Callable[[AgentExecutionSupervisor], SupervisorDecision],
            *,
            tool_name: str | None = None,
        ) -> SupervisorDecision | None:
            current = supervisor
            if current is None:
                return None
            try:
                decision = operation(current)
                record = SupervisionTraceRecord(
                    checkpoint,
                    model_step,
                    tool_name,
                    current.snapshot,
                    decision,
                )
                LOGGER.debug(
                    "supervision checkpoint=%s step=%s tool=%s decision=%s reason_code=%s "
                    "counters=%s status=%s",
                    record.checkpoint.value,
                    record.model_step,
                    record.tool_name,
                    record.decision.kind.value,
                    record.decision.reason_code.value,
                    record.snapshot.counters,
                    record.snapshot.status.value,
                )
                if self._supervision_observer is not None:
                    self._supervision_observer(record)
                return decision
            except Exception as error:
                disable_supervision("checkpoint", error)
                return None

        def controlled_terminal_decision(
            decision: SupervisorDecision | None,
        ) -> SupervisorDecision | None:
            if self._execution_control_mode is not ExecutionControlMode.FINALIZE_TERMINAL:
                return None
            if decision is None:
                return None
            if decision.kind not in {
                SupervisorDecisionKind.FINALIZE,
                SupervisorDecisionKind.MARK_STUCK,
                SupervisorDecisionKind.MARK_BUDGET_LIMITED,
            }:
                return None
            return decision

        def select_terminal_decision(
            current: SupervisorDecision | None,
            candidate: SupervisorDecision | None,
        ) -> SupervisorDecision | None:
            candidate = controlled_terminal_decision(candidate)
            if candidate is None:
                return current
            if current is None:
                return candidate
            priority = {
                SupervisorDecisionKind.FINALIZE: 1,
                SupervisorDecisionKind.MARK_BUDGET_LIMITED: 2,
                SupervisorDecisionKind.MARK_STUCK: 3,
            }
            return candidate if priority[candidate.kind] > priority[current.kind] else current

        def append_runtime_plan_notice() -> None:
            """Append a plan revision after the latest durable turn item.

            Runtime plan notices are transient, but keeping each revision in
            the in-memory sequence makes the next request an append-only
            extension rather than a rewrite of its stable system prefix.

            在最新持久回合条目之后追加计划修订。运行时计划通知是临时的,但将每个修订
            保留在内存序列中,可使下一请求仅追加而不改写稳定 system 前缀。
            """

            nonlocal last_runtime_plan_content
            notice = self._context_builder.plan_runtime_message()
            if notice is None:
                if last_runtime_plan_content is not None:
                    context_items.append(
                        Message(
                            Role.USER,
                            "Runtime plan update:\nNo structured plan is currently active.",
                            synthetic_reason=SyntheticReason.RUNTIME_PLAN,
                        )
                    )
                    last_runtime_plan_content = None
                return
            if notice.content != last_runtime_plan_content:
                context_items.append(notice)
                last_runtime_plan_content = notice.content

        def append_budget_pressure_notice(*, include_model_reserve: bool) -> None:
            """Append only discrete runtime-pressure transitions.

            仅追加离散的运行时压力转换通知。
            """

            nonlocal last_budget_pressure
            current = supervisor
            if (
                current is None
                or self._execution_control_mode is not ExecutionControlMode.FINALIZE_TERMINAL
            ):
                return
            usage = current.budget_usage(include_model_reserve=include_model_reserve)
            if usage.pressure is last_budget_pressure:
                return
            last_budget_pressure = usage.pressure
            notice = self._context_builder.budget_runtime_message(usage)
            if notice is not None:
                context_items.append(notice)

        def update_runtime_supervision_guidance(
            decision: SupervisorDecision | None,
        ) -> None:
            nonlocal replan_notice_active
            if self._execution_control_mode is not ExecutionControlMode.FINALIZE_TERMINAL:
                return
            if decision is not None and decision.kind is SupervisorDecisionKind.REPLAN:
                if not replan_notice_active:
                    context_items.append(self._context_builder.supervision_runtime_message())
                    replan_notice_active = True
                return
            current = supervisor
            if (
                replan_notice_active
                and current is not None
                and current.snapshot.consecutive_no_progress_rounds == 0
            ):
                context_items.append(
                    self._context_builder.supervision_runtime_message(resolved=True)
                )
                replan_notice_active = False

        def persistent_context_items() -> tuple[SessionItem, ...]:
            """Exclude all in-memory synthetic context from session writes.

            从会话写入中排除全部仅内存合成上下文。
            """

            return tuple(
                item
                for item in context_items
                if not (isinstance(item, Message) and item.synthetic_reason is not None)
            )

        def canonical_model_context() -> ModelContext:
            return ModelContext(
                tuple(context_items),
                context_source_provider,
                context_source_model,
                context_source_affinity,
                self._context_builder.reasoning_effort,
            )

        def projected_model_context() -> ModelContext:
            context = canonical_model_context()
            if active_compaction_item is None:
                return context
            durable_context = ModelContext(
                persistent_context_items(),
                context_source_provider,
                context_source_model,
                context_source_affinity,
                self._context_builder.reasoning_effort,
            )
            rebuilt = (
                CompactionResumeRebuilder()
                .rebuild(
                    durable_context,
                    (active_compaction_item,),
                )
                .context
            )
            runtime_notices = tuple(
                item
                for item in context_items
                if isinstance(item, Message) and item.synthetic_reason is not None
            )
            return ModelContext(
                (*rebuilt.items, *runtime_notices),
                rebuilt.source_provider,
                rebuilt.source_model,
                rebuilt.source_context_affinity,
                rebuilt.reasoning_effort,
            )

        async def build_request_context(
            additional_items: Sequence[SessionItem] = (),
        ) -> ModelContext:
            projected = projected_model_context()
            model_items = await run_blocking(
                self._context_builder.build,
                (*projected.items, *additional_items),
            )
            return ModelContext(
                model_items,
                projected.source_provider,
                projected.source_model,
                projected.source_context_affinity,
                self._context_builder.reasoning_effort,
            )

        async def emit_budget_usage() -> None:
            current = supervisor
            if (
                current is None
                or self._execution_control_mode is not ExecutionControlMode.FINALIZE_TERMINAL
            ):
                return
            usage = current.budget_usage()
            await emit(AgentEventKind.EXECUTION_BUDGET_UPDATED, usage.to_event_data())

        async def maybe_compact_context(
            safe_point: ContextCompactionSafePoint,
            *,
            step: int,
            usage_context: ModelContext,
        ) -> SupervisorDecision | None:
            nonlocal active_compaction_item
            gate = self._compaction_runtime_gate
            if (
                self._execution_control_mode is not ExecutionControlMode.FINALIZE_TERMINAL
                or not has_completed_model_step
                or gate is None
                or active_provider_window is None
                or self._session_store is None
                or session_id is None
            ):
                return None
            source_context = ModelContext(
                persistent_context_items(),
                context_source_provider,
                context_source_model,
                context_source_affinity,
                self._context_builder.reasoning_effort,
            )
            request = gate.build_automatic_request(
                source_context=source_context,
                usage_context=usage_context,
                boundary=ContextCompactionRuntimeBoundary(safe_point, step),
                provider_window=active_provider_window,
                protected_item_count=(
                    1
                    if source_context.items
                    and isinstance(source_context.items[0], Message)
                    and source_context.items[0].role is Role.SYSTEM
                    else 0
                ),
                session_id=session_id,
                compaction_id=f"compact-{uuid.uuid4().hex}",
                created_at=datetime.now(UTC),
            )
            assessment = gate.assess(request)
            if not assessment.will_trigger:
                return None
            plan = assessment.trigger.plan
            if (
                active_compaction_item is not None
                and active_compaction_item.source_item_count == plan.source_item_count
                and active_compaction_item.candidate_range == plan.candidate_range
            ):
                if plan.decision is ContextCompactionDecision.REQUIRED:
                    return SupervisorDecision(
                        SupervisorDecisionKind.MARK_BUDGET_LIMITED,
                        "context remains above its hard limit after bounded compaction",
                        AgentExecutionStatus.BUDGET_LIMITED,
                        False,
                        SupervisorReasonCode.CONTEXT_WINDOW_BUDGET,
                    )
                return None
            await emit(
                AgentEventKind.CONTEXT_COMPACTION_STARTED,
                {
                    "safe_point": safe_point.value,
                    "decision": plan.decision.value,
                    "source_item_count": plan.source_item_count,
                    "candidate_item_count": plan.candidate_item_count,
                },
            )
            try:
                result = await gate.trigger(request)
            except ContextCompactionTimeoutError:
                return SupervisorDecision(
                    SupervisorDecisionKind.MARK_BUDGET_LIMITED,
                    "automatic context compaction exceeded its wall-clock budget",
                    AgentExecutionStatus.BUDGET_LIMITED,
                    False,
                    SupervisorReasonCode.WALL_TIME_BUDGET,
                )
            persistence = result.trigger_result.persistence
            if persistence is None:
                return None
            active_compaction_item = persistence.item
            await emit(
                AgentEventKind.CONTEXT_COMPACTION_COMPLETED,
                {
                    "safe_point": safe_point.value,
                    "source_item_count": persistence.item.source_item_count,
                    "candidate_item_count": (
                        persistence.item.candidate_range[1] - persistence.item.candidate_range[0]
                    ),
                    "summary_tokens": persistence.item.summary_tokens,
                    "summary_truncated": persistence.item.summary_truncated,
                },
            )
            return None

        def outcome_for_terminal_decision(
            decision: SupervisorDecision,
        ) -> AgentExecutionOutcome:
            status = (
                AgentExecutionStatus.STUCK
                if decision.kind is SupervisorDecisionKind.MARK_STUCK
                else AgentExecutionStatus.BUDGET_LIMITED
            )
            return AgentExecutionOutcome(
                status,
                decision.reason_code,
                finalized=True,
                recoverable=True,
            )

        workspace_evidence: list[str] = []

        def record_workspace_evidence(report: WorkspaceChangeReport) -> None:
            for change in report.files:
                path = redact_sensitive_text(
                    change.path,
                    explicit_values=self._tool_context.redaction_values,
                )
                workspace_evidence.append(
                    f"{change.status} {path} (+{change.additions}/-{change.deletions})"
                )

        def finalization_evidence(
            decision: SupervisorDecision | None,
        ) -> FinalizationEvidence:
            verification = verification_tracker.report()
            reason_code = (
                decision.reason_code if decision is not None else SupervisorReasonCode.NONE
            )
            return FinalizationEvidence(
                reason_code,
                workspace_changes=tuple(workspace_evidence),
                verification=verification.confirmed_items,
                unverified_items=(
                    *verification.unverified_items,
                    "No additional verification should be claimed without recorded evidence.",
                ),
                verification_state=verification.state,
                verification_evidence=verification.evidence,
                verification_workspace_generation=verification.workspace_generation,
                blocker=(
                    f"Execution stopped because {decision.reason_code.value.replace('_', ' ')}."
                    if decision is not None
                    else None
                ),
                uncertainty=(
                    "The final response is limited to evidence already recorded in the conversation.",
                ),
            )

        async def complete_finalized_turn(
            decision: SupervisorDecision,
            *,
            step: int,
        ) -> AgentRunResult:
            if (
                decision.reason_code is SupervisorReasonCode.MODEL_CALL_BUDGET
                and step >= self._max_steps
            ):
                decision = SupervisorDecision(
                    SupervisorDecisionKind.MARK_BUDGET_LIMITED,
                    "agent reached its hard model-step limit",
                    AgentExecutionStatus.BUDGET_LIMITED,
                    False,
                    SupervisorReasonCode.MODEL_STEP_LIMIT,
                )
            outcome = outcome_for_terminal_decision(decision)
            await emit(
                AgentEventKind.FINALIZING_STARTED,
                {
                    "execution_status": outcome.status.value,
                    "execution_reason": outcome.reason_code.value
                    if outcome.reason_code is not None
                    else None,
                    "recoverable": outcome.recoverable,
                },
            )
            finalizer = self._finalizer_factory(
                self._provider,
                self._finalizer_max_attempts,
                self._tool_context.redaction_values,
            )
            finalization = await finalizer.finalize(
                projected_model_context(),
                finalization_evidence(decision),
            )
            return await complete_finalization_result(finalization, decision, step=step)

        async def complete_gated_terminal_turn(
            candidate: FinalResponseContract,
            *,
            step: int,
        ) -> AgentRunResult:
            """Replace a gated model candidate before any public/durable commit."""

            if candidate.is_committed:
                raise ConfigurationError("gated terminal candidate must remain provisional")
            await emit(
                AgentEventKind.FINALIZING_STARTED,
                {
                    "execution_status": AgentExecutionStatus.FINALIZING.value,
                    "execution_reason": SupervisorReasonCode.NONE.value,
                    "recoverable": False,
                },
            )
            evidence = finalization_evidence(None)
            try:
                finalizer = self._finalizer_factory(
                    self._provider,
                    self._finalizer_max_attempts,
                    self._tool_context.redaction_values,
                )
                finalization = await finalizer.finalize(
                    projected_model_context(),
                    evidence,
                )
                if (
                    not isinstance(finalization, FinalizationResult)
                    or not finalization.response.strip()
                ):
                    raise ValueError("finalizer returned an invalid response")
                if finalization.status is not FinalizationStatus.COMPLETED:
                    finalization = deterministic_fallback_result(evidence)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.debug(
                    "verification-gated finalizer failed; using deterministic fallback "
                    "error_type=%s",
                    type(error).__name__,
                )
                finalization = deterministic_fallback_result(evidence)
            return await complete_finalization_result(finalization, None, step=step)

        async def complete_finalization_result(
            finalization: FinalizationResult,
            decision: SupervisorDecision | None,
            *,
            step: int,
        ) -> AgentRunResult:
            result_outcome = (
                outcome_for_terminal_decision(decision) if decision is not None else None
            )
            completion_outcome = result_outcome or AgentExecutionOutcome(
                AgentExecutionStatus.COMPLETED,
                None,
                finalized=False,
                recoverable=False,
            )
            verification = verification_tracker.report()
            response_source = (
                ResponseSource.EVIDENCE_AWARE_FINALIZER
                if finalization.status is FinalizationStatus.COMPLETED
                else ResponseSource.DETERMINISTIC_FALLBACK
            )
            response_contract = FinalResponseContract.committed(
                finalization.response,
                source=response_source,
                verification=verification,
            )
            final_message = Message(Role.ASSISTANT, finalization.response)
            messages.append(final_message)
            if persist_turn_context:
                context_items.append(final_message)
            await emit(AgentEventKind.TEXT_DELTA, {"text": finalization.response})
            result_items = (
                persistent_context_items() if persist_turn_context else turn_context_prefix
            )
            completion_data: dict[str, object] = {
                "step": step,
                "stop_reason": finalization.stop_reason,
                "input_tokens": finalization.total_input_tokens,
                "output_tokens": finalization.total_output_tokens,
                "duration_seconds": monotonic() - turn_started_at,
                "execution_status": completion_outcome.status.value,
                "execution_reason": (
                    completion_outcome.reason_code.value
                    if completion_outcome.reason_code is not None
                    else None
                ),
                "finalized": completion_outcome.finalized,
                "recoverable": completion_outcome.recoverable,
                "finalization_status": finalization.status.value,
                "finalization_attempts": len(finalization.attempts),
                "illegal_tool_calls": finalization.illegal_tool_calls,
            }
            if ultracode_execution_id is not None:
                completion_data.update(
                    {
                        "ultracode_execution_id": ultracode_execution_id,
                        "response": finalization.response,
                    }
                )
            await finalize_turn_completion(
                completion_outcome,
                completion_data,
                result_items,
                response_contract=response_contract,
            )
            return AgentRunResult(
                session_id,
                finalization.response,
                tuple(messages),
                result_items,
                tuple(events),
                step,
                self._context_builder.plan,
                result_outcome,
                turn_id,
                verification=verification,
                response_contract=response_contract,
            )

        response_parts: list[str] = []
        # A completion reminder is intentionally a one-request tail item.  It
        # is acknowledged only after a completed provider response, after
        # which it must not be shown again.  Keeping it outside
        # ``context_items`` preserves that lifecycle while the durable
        # conversation prefix remains append-only.
        #
        # 完成提醒有意作为仅一次请求的尾部条目。仅在 Provider 完成响应后确认,此后不得
        # 再展示。将其放在 ``context_items`` 之外可保持该生命周期,同时持久化对话前缀仍
        # 仅追加。
        completion_reminders: list[Message] = []
        pending_terminal_decision: SupervisorDecision | None = None
        try:
            try:
                supervisor = self._supervisor_factory()
                if not isinstance(supervisor, AgentExecutionSupervisor):
                    raise TypeError("supervisor_factory must return an AgentExecutionSupervisor")
                supervisor.start_turn()
            except Exception as error:
                disable_supervision("start_turn", error)
            await emit(
                AgentEventKind.SESSION_STARTED,
                {
                    "session_id": session_id or "",
                    "provider": self._provider.provider_name,
                    "model": self._provider.model_name,
                },
            )
            if turn_source is TurnSource.BACKGROUND_TASK_AUTO_WAKE:
                assert background_tasks is not None
                pending_completions = await background_tasks.pending_completions()
                await emit(
                    AgentEventKind.BACKGROUND_TASK_AUTO_WAKE_STARTED,
                    {
                        "count": min(
                            len(pending_completions),
                            BACKGROUND_TASK_COMPLETION_BATCH_LIMIT,
                        ),
                        "remaining_count": max(
                            0,
                            len(pending_completions) - BACKGROUND_TASK_COMPLETION_BATCH_LIMIT,
                        ),
                        "model_context_only": True,
                    },
                )
            if session_task is not None:
                await emit(AgentEventKind.SESSION_TASK_STARTED, {"task": session_task.to_dict()})
            if plan_execution_requested:
                assert self._context_builder.plan is not None
                await emit(
                    AgentEventKind.PLAN_EXECUTION_REQUESTED,
                    {"plan": self._context_builder.plan.to_dict()},
                )
            if turn_source is TurnSource.USER:
                user_message = Message(Role.USER, prompt, content_parts=prompt_parts)
                context_items.append(user_message)
                messages.append(user_message)
                await emit(AgentEventKind.USER_MESSAGE, {"content": user_message.model_content()})
            append_runtime_plan_notice()

            if (
                self._execution_control_mode is ExecutionControlMode.FINALIZE_TERMINAL
                and self._compaction_runtime_gate is not None
                and active_provider_window is not None
                and self._session_store is not None
                and session_id is not None
            ):
                records = await self._session_store.load_compaction_items(session_id)
                resumed = rebuild_context_from_latest_compatible_compaction(
                    ModelContext(
                        persistent_context_items(),
                        context_source_provider,
                        context_source_model,
                        context_source_affinity,
                        self._context_builder.reasoning_effort,
                    ),
                    records,
                )
                if resumed.applied_compaction_ids:
                    selected_id = resumed.applied_compaction_ids[0]
                    active_compaction_item = next(
                        record for record in records if record.compaction_id == selected_id
                    )

            for step in range(1, self._max_steps + 1):
                if pending_terminal_decision is not None:
                    return await complete_finalized_turn(pending_terminal_decision, step=step - 1)
                step_started_at = monotonic()
                await emit(AgentEventKind.MODEL_STEP_STARTED, {"step": step})
                completion_batch: tuple[BackgroundTaskSnapshot, ...] = ()
                if background_tasks is not None:
                    pending_completions = await background_tasks.pending_completions()
                    completion_batch = pending_completions[:BACKGROUND_TASK_COMPLETION_BATCH_LIMIT]
                    if completion_batch:
                        remaining_count = len(pending_completions) - len(completion_batch)
                        completion_reminders.append(
                            Message(
                                Role.USER,
                                format_background_task_completion_reminder(
                                    completion_batch,
                                    remaining_count=remaining_count,
                                    task_output_tool=(
                                        "task_output"
                                        if self._tools.get("task_output") is not None
                                        else None
                                    ),
                                    include_output=(
                                        turn_source is TurnSource.BACKGROUND_TASK_AUTO_WAKE
                                    ),
                                    redaction_values=(
                                        self._tool_context.redaction_values
                                        if turn_source is TurnSource.BACKGROUND_TASK_AUTO_WAKE
                                        else ()
                                    ),
                                ),
                                synthetic_reason=SyntheticReason.RUNTIME_BACKGROUND_TASK,
                            )
                        )
                        await emit(
                            AgentEventKind.BACKGROUND_TASK_COMPLETION_REMINDER,
                            {
                                "task_ids": [snapshot.task_id for snapshot in completion_batch],
                                "statuses": [
                                    snapshot.status.value for snapshot in completion_batch
                                ],
                                "count": len(completion_batch),
                                "remaining_count": remaining_count,
                                "model_context_only": True,
                            },
                        )
                append_budget_pressure_notice(include_model_reserve=True)
                context = await build_request_context(completion_reminders)
                prior_compaction_item = active_compaction_item
                compaction_decision = await maybe_compact_context(
                    ContextCompactionSafePoint.BEFORE_MODEL_REQUEST,
                    step=step,
                    usage_context=context,
                )
                if compaction_decision is not None:
                    return await complete_finalized_turn(compaction_decision, step=step - 1)
                if active_compaction_item is not prior_compaction_item:
                    context = await build_request_context(completion_reminders)
                before_model_decision = record_supervision(
                    SupervisionCheckpoint.BEFORE_MODEL,
                    step,
                    lambda current: current.authorize_model_request(),
                )
                terminal_before_model = controlled_terminal_decision(before_model_decision)
                if terminal_before_model is not None:
                    return await complete_finalized_turn(terminal_before_model, step=step - 1)
                await emit_budget_usage()
                tool_definitions = self._tools.definitions()
                request_snapshot = ModelRequestSnapshot.build(
                    context=context,
                    tools=tool_definitions,
                    provider=self._provider.provider_name,
                    model=self._provider.model_name,
                    context_affinity=getattr(self._provider, "context_affinity", None),
                    step=step,
                    reasoning_effort=context.reasoning_effort,
                )
                await emit(
                    AgentEventKind.MODEL_REQUEST_SNAPSHOT,
                    request_snapshot.to_event_data(),
                )
                await recorder.record_model_request_started(
                    request_id=request_snapshot.request_id,
                    step=step,
                    provider=self._provider.provider_name,
                    model=self._provider.model_name,
                )

                request_id = request_snapshot.request_id
                current_step = step
                gate_active = (
                    self._final_output_gate_enabled
                    and verification_tracker.report().final_output_gate_active
                )

                async def record_output_started(
                    output_kind: str,
                    bound_request_id: str = request_id,
                    bound_step: int = current_step,
                ) -> None:
                    await recorder.record_model_output_started(
                        request_id=bound_request_id,
                        step=bound_step,
                        output_kind=output_kind,
                    )

                step_result = await ModelStepProcessor(session_store=self._session_store).consume(
                    self._provider.stream(context, tool_definitions),
                    emit=emit,
                    step=step,
                    step_started_at=step_started_at,
                    session_id=session_id,
                    can_adopt_provider_origin=can_adopt_provider_origin,
                    on_imperfect=lambda: setattr(recorder, "pristine_cancel_eligible", False),
                    on_output_started=record_output_started,
                    buffer_text=gate_active,
                )
                step_text = step_result.text
                step_reasoning = step_result.reasoning
                tool_calls = step_result.tool_calls
                completion = step_result.completion

                if completion is None:
                    raise ProviderError("provider stream ended without a completion event")
                has_completed_model_step = True
                if step_result.selected_provider is not None:
                    selected = step_result.selected_provider
                    active_provider_window = (
                        ProviderContextWindow(
                            selected.provider,
                            selected.model,
                            selected.context_window_tokens,
                            selected.context_affinity,
                        )
                        if selected.context_window_tokens is not None
                        else None
                    )
                    # A failover provider can keep its selected candidate across
                    # turns without announcing it again. Retain the last explicit
                    # selection so the next turn never falls back to stale primary
                    # context-window metadata.
                    self._provider_context_window = active_provider_window
                    if active_compaction_item is not None and (
                        active_compaction_item.provider_name != selected.provider
                        or active_compaction_item.model_name != selected.model
                        or (
                            active_compaction_item.context_affinity is not None
                            and active_compaction_item.context_affinity != selected.context_affinity
                        )
                    ):
                        active_compaction_item = None
                if completion.usage is not None:
                    processed_input_tokens = completion.usage.processed_input_tokens
                    used_tokens = (
                        processed_input_tokens + completion.output_tokens
                        if processed_input_tokens is not None
                        and completion.output_tokens is not None
                        else None
                    )
                    await emit(
                        AgentEventKind.CONTEXT_USAGE_UPDATED,
                        {
                            **completion.usage.to_event_data(),
                            "used_tokens": used_tokens,
                            "estimated": used_tokens is None,
                        },
                    )

                def observe_model_completion(
                    current: AgentExecutionSupervisor,
                    input_tokens: int | None = completion.input_tokens,
                    output_tokens: int | None = completion.output_tokens,
                ) -> SupervisorDecision:
                    return current.observe_model_completion(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )

                after_model_decision = record_supervision(
                    SupervisionCheckpoint.AFTER_MODEL,
                    step,
                    observe_model_completion,
                )
                if any(
                    limit is not None
                    for limit in (
                        self._execution_budget.max_input_tokens,
                        self._execution_budget.max_output_tokens,
                        self._execution_budget.max_total_tokens,
                    )
                ):
                    await emit_budget_usage()
                if completion_batch and background_tasks is not None:
                    await background_tasks.mark_completions_reported(
                        tuple(snapshot.task_id for snapshot in completion_batch)
                    )
                    completion_reminders.clear()
                if completion.context_items:
                    context_items.extend(completion.context_items)
                    if can_adopt_provider_origin:
                        context_source_provider = self._provider.provider_name
                        context_source_model = self._provider.model_name
                        context_source_affinity = getattr(self._provider, "context_affinity", None)
                        can_adopt_provider_origin = False
                assistant_content = (
                    completion.response_text
                    if completion.response_text is not None
                    else "".join(step_text)
                )
                if gate_active and tool_calls:
                    for text in step_text:
                        await emit(AgentEventKind.TEXT_DELTA, {"text": text})
                if gate_active and not tool_calls:
                    terminal_candidate = FinalResponseContract.provisional(
                        assistant_content,
                        source=ResponseSource.NORMAL_MODEL,
                        verification=verification_tracker.report(),
                    )
                    return await complete_gated_terminal_turn(terminal_candidate, step=step)
                response_parts.append(assistant_content)
                assistant_message = Message(
                    Role.ASSISTANT,
                    assistant_content,
                    tool_calls=_redacted_persisted_tool_calls(
                        tool_calls,
                        self._tool_context.redaction_values,
                    ),
                    reasoning_content="".join(step_reasoning) or None,
                )
                messages.append(assistant_message)
                if persist_turn_context:
                    context_items.append(assistant_message)

                if not tool_calls:
                    result_items = (
                        persistent_context_items() if persist_turn_context else turn_context_prefix
                    )
                    response = "".join(response_parts)
                    verification = verification_tracker.report()
                    response_contract = FinalResponseContract.committed(
                        response,
                        source=ResponseSource.NORMAL_MODEL,
                        verification=verification,
                    )
                    completion_data = {
                        "step": step,
                        "stop_reason": completion.stop_reason,
                        "input_tokens": completion.input_tokens,
                        "output_tokens": completion.output_tokens,
                        "duration_seconds": monotonic() - turn_started_at,
                    }
                    if ultracode_execution_id is not None:
                        completion_data.update(
                            {
                                "ultracode_execution_id": ultracode_execution_id,
                                "response": response,
                            }
                        )
                    await finalize_turn_completion(
                        AgentExecutionOutcome(
                            AgentExecutionStatus.COMPLETED,
                            None,
                            finalized=False,
                            recoverable=False,
                        ),
                        completion_data,
                        result_items,
                        response_contract=response_contract,
                    )
                    return AgentRunResult(
                        session_id,
                        response,
                        tuple(messages),
                        result_items,
                        tuple(events),
                        step,
                        self._context_builder.plan,
                        turn_id=turn_id,
                        verification=verification,
                        response_contract=response_contract,
                    )

                tool_batch = tuple(call.name for call in tool_calls)
                pending_terminal_decision = select_terminal_decision(
                    pending_terminal_decision,
                    after_model_decision,
                )

                def assess_tool_batch(
                    current: AgentExecutionSupervisor,
                    tool_batch: tuple[str, ...] = tool_batch,
                ) -> SupervisorDecision:
                    return current.assess_tool_batch(tool_batch)

                after_tool_batch_decision = record_supervision(
                    SupervisionCheckpoint.AFTER_TOOL_BATCH,
                    step,
                    assess_tool_batch,
                )
                pending_terminal_decision = select_terminal_decision(
                    pending_terminal_decision,
                    after_tool_batch_decision,
                )
                await emit_budget_usage()

                def record_tool_outcome(
                    observation: ToolExecutionObservation,
                    *,
                    model_step: int = step,
                ) -> SupervisorDecision | None:
                    verification_tracker.observe(observation)

                    def observe_tool(
                        current: AgentExecutionSupervisor,
                    ) -> SupervisorDecision:
                        return current.observe_tool_outcome(observation)

                    return record_supervision(
                        SupervisionCheckpoint.AFTER_TOOL,
                        model_step,
                        observe_tool,
                        tool_name=observation.tool_name,
                    )

                def record_interrupted_tool_outcome(
                    observation: ToolExecutionObservation,
                ) -> None:
                    record_tool_outcome(observation)

                last_tool_decision: SupervisorDecision | None = None
                interaction_calls = tuple(
                    call
                    for call in tool_calls
                    if isinstance(self._tools.get(call.name), InteractionControlTool)
                )
                if interaction_calls and len(tool_calls) != 1:
                    await self._tool_executor.record_rejected_tool_calls(
                        tool_calls,
                        messages,
                        context_items,
                        emit,
                        reason="ask_user must be issued as the only tool call in this model step.",
                    )
                    update_runtime_supervision_guidance(after_tool_batch_decision)
                    continue

                async def execute_scheduled_tool(
                    call: ToolCall,
                    isolated: bool,
                ) -> _ScheduledToolOutcome:
                    # Parallel calls get private append-only projections.  The
                    # executor still owns every permission, approval, sandbox,
                    # redaction, and event boundary; only transcript merging
                    # is deferred until model order is restored.
                    base_message_count = len(messages)
                    base_context_count = len(context_items)
                    target_messages = list(messages) if isolated else messages
                    target_context_items = list(context_items) if isolated else context_items
                    interaction_tool = isinstance(
                        self._tools.get(call.name), InteractionControlTool
                    )
                    if interaction_tool and supervisor is not None:
                        supervisor.pause_wall_clock()
                    try:
                        observation = await self._tool_executor.execute(
                            call,
                            target_messages,
                            target_context_items,
                            emit,
                            session_id,
                            interrupted_observation_sink=record_interrupted_tool_outcome,
                            workspace_change_sink=(
                                record_workspace_evidence
                                if self._execution_control_mode
                                is ExecutionControlMode.FINALIZE_TERMINAL
                                else None
                            ),
                            recovery_started_sink=(
                                (
                                    lambda tool_id, tool_name, side_effecting: (
                                        recorder.record_tool_started(
                                            tool_id=tool_id,
                                            tool_name=tool_name,
                                            side_effecting=side_effecting,
                                        )
                                    )
                                )
                                if self._session_store is not None
                                and session_id is not None
                                and turn_id is not None
                                else None
                            ),
                        )
                        return _ScheduledToolOutcome(
                            observation,
                            tuple(target_messages[base_message_count:]) if isolated else (),
                            tuple(target_context_items[base_context_count:]) if isolated else (),
                        )
                    finally:
                        if interaction_tool and supervisor is not None:
                            supervisor.resume_wall_clock()

                try:
                    scheduled_observations = await self._tool_scheduler.run(
                        tool_calls,
                        execute_scheduled_tool,
                    )
                except ToolBatchExecutionError as batch_error:
                    await self._tool_executor.record_unstarted_tool_calls(
                        batch_error.not_started,
                        messages,
                        context_items,
                        emit,
                        cancelled=isinstance(batch_error.cause, asyncio.CancelledError),
                    )
                    raise batch_error.cause from batch_error
                for scheduled in scheduled_observations:
                    messages.extend(scheduled.messages)
                    context_items.extend(scheduled.context_items)
                    observation = scheduled.observation
                    if observation is None:
                        disable_supervision("tool_observation_unavailable")
                    else:
                        last_tool_decision = record_tool_outcome(observation)
                        if (
                            supervisor is not None
                            and observation.progress_kind is not ProgressKind.NONE
                        ):
                            segment_progress_kinds.add(observation.progress_kind)
                        pending_terminal_decision = select_terminal_decision(
                            pending_terminal_decision,
                            last_tool_decision,
                        )
                if pending_terminal_decision is not None:
                    return await complete_finalized_turn(pending_terminal_decision, step=step)
                update_runtime_supervision_guidance(
                    last_tool_decision or after_tool_batch_decision or after_model_decision
                )
                append_runtime_plan_notice()
                append_budget_pressure_notice(include_model_reserve=True)
                post_batch_context = await build_request_context()
                compaction_decision = await maybe_compact_context(
                    ContextCompactionSafePoint.AFTER_TOOL_BATCH,
                    step=step,
                    usage_context=post_batch_context,
                )
                if compaction_decision is not None:
                    return await complete_finalized_turn(compaction_decision, step=step)

                current_supervisor = supervisor
                if (
                    self._execution_control_mode is ExecutionControlMode.FINALIZE_TERMINAL
                    and current_supervisor is not None
                    and segment_progress_kinds
                    and current_supervisor.snapshot.consecutive_no_progress_rounds == 0
                    and self._segment_policy.reached(
                        current_supervisor.snapshot.counters,
                        segment_start_counters,
                    )
                ):
                    usage = current_supervisor.budget_usage()
                    if (
                        usage.model_calls_remaining > 0
                        and usage.tool_rounds_remaining > 0
                        and usage.tool_calls_remaining > 0
                    ):
                        counters = current_supervisor.snapshot.counters
                        plan = self._context_builder.plan
                        checkpoint = ExecutionSegmentCheckpoint(
                            segment_number=segment_number,
                            model_calls=(
                                counters.model_requests - segment_start_counters.model_requests
                            ),
                            tool_rounds=(counters.tool_rounds - segment_start_counters.tool_rounds),
                            tool_calls=(
                                counters.tool_calls_requested
                                - segment_start_counters.tool_calls_requested
                            ),
                            progress_kinds=tuple(segment_progress_kinds),
                            plan_steps_total=len(plan.steps) if plan is not None else 0,
                            plan_steps_completed=(
                                sum(
                                    step_item.status is PlanStepStatus.COMPLETED
                                    for step_item in plan.steps
                                )
                                if plan is not None
                                else 0
                            ),
                            created_at=datetime.now(UTC),
                        )
                        await emit(
                            AgentEventKind.EXECUTION_SEGMENT_CHECKPOINTED,
                            checkpoint.to_event_data(),
                        )
                        context_items.append(
                            self._context_builder.segment_runtime_message(checkpoint)
                        )
                        segment_number += 1
                        segment_start_counters = counters
                        segment_progress_kinds.clear()
            if self._execution_control_mode is ExecutionControlMode.FINALIZE_TERMINAL:
                return await complete_finalized_turn(
                    SupervisorDecision(
                        SupervisorDecisionKind.MARK_BUDGET_LIMITED,
                        "agent reached its hard model-step limit",
                        AgentExecutionStatus.BUDGET_LIMITED,
                        False,
                        SupervisorReasonCode.MODEL_STEP_LIMIT,
                    ),
                    step=self._max_steps,
                )
            raise ProviderError(f"agent exceeded the maximum of {self._max_steps} model steps")
        except BaseException as error:
            # Preserve cancellation semantics while still making the session auditable.
            await record_turn_failure(error)
            raise


__all__ = ["AgentLoopRunner", "AgentRunResult"]
