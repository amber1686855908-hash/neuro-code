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

from neuro_code.application.ports.model import ModelProvider
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.tools import ToolCollection, ToolContext
from neuro_code.application.ports.workspace_changes import WorkspaceChangeReport
from neuro_code.application.runtime.background_task_reminders import (
    BACKGROUND_TASK_COMPLETION_BATCH_LIMIT,
    format_background_task_completion_reminder,
)
from neuro_code.application.runtime.context_builder import ContextBuilder
from neuro_code.application.runtime.event_recorder import TurnEventRecorder
from neuro_code.application.runtime.finalization import (
    FinalizationEvidence,
    FinalizationResult,
    Finalizer,
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
from neuro_code.application.sessions.lifecycle import (
    SessionLifecycleService,
    StartSessionRequest,
)
from neuro_code.application.sessions.task_queries import (
    GetSessionTaskRequest,
    SessionTaskQueryService,
)
from neuro_code.domain.background_tasks.models import BackgroundTaskSnapshot
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.messages import (
    ContentPart,
    Message,
    PreservedContextItem,
    Role,
    SessionItem,
)
from neuro_code.domain.execution import (
    AgentExecutionOutcome,
    AgentExecutionStatus,
    ProgressKind,
    SupervisorDecision,
    SupervisorDecisionKind,
    SupervisorReasonCode,
    TurnCancellationPolicy,
    TurnSource,
)
from neuro_code.domain.plans import SessionPlan
from neuro_code.domain.session_tasks import SessionTask, SessionTaskKind, SessionTaskStatus
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import ConfigurationError, ProviderError
from neuro_code.shared.redaction import redact_sensitive_text

LOGGER = logging.getLogger(__name__)

EventSink = Callable[[AgentEvent], Awaitable[None] | None]


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


class AgentLoopRunner:
    """Own one agent turn's step loop and finalization orchestration.

    管理一个 Agent 回合的步骤循环和最终化编排."""

    __slots__ = (
        "_context_builder",
        "_execution_control_mode",
        "_finalizer_factory",
        "_finalizer_max_attempts",
        "_max_steps",
        "_provider",
        "_session_store",
        "_supervision_observer",
        "_supervisor_factory",
        "_system_prompt",
        "_tool_context",
        "_tool_executor",
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
        max_steps: int,
        context_builder: ContextBuilder,
        supervisor_factory: Callable[[], AgentExecutionSupervisor],
        supervision_observer: SupervisionObserver | None,
        execution_control_mode: ExecutionControlMode,
        finalizer_factory: Callable[[ModelProvider, int, tuple[str, ...]], Finalizer],
        finalizer_max_attempts: int,
        tool_executor: ToolExecutor,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._tool_context = tool_context
        self._session_store = session_store
        self._system_prompt = system_prompt
        self._max_steps = max_steps
        self._context_builder = context_builder
        self._supervisor_factory = supervisor_factory
        self._supervision_observer = supervision_observer
        self._execution_control_mode = execution_control_mode
        self._finalizer_factory = finalizer_factory
        self._finalizer_max_attempts = finalizer_max_attempts
        self._tool_executor = tool_executor

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
        prompt_parts = tuple(content_parts)
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
                await self._session_store.create_session_task(session_id, session_task)
            else:
                queued_task = await SessionTaskQueryService(self._session_store).get_session_task(
                    GetSessionTaskRequest(session_id, plan_execution_task_id)
                )
                if queued_task is None:
                    raise ConfigurationError(f"unknown queued plan task: {plan_execution_task_id}")
                if queued_task.kind is not SessionTaskKind.PLAN_EXECUTION:
                    raise ConfigurationError("only plan execution tasks can be started")
                if queued_task.status is not SessionTaskStatus.QUEUED:
                    raise ConfigurationError(f"plan task {plan_execution_task_id} is not queued")
                if queued_task.plan_snapshot != self._context_builder.plan:
                    raise ConfigurationError(
                        f"plan task {plan_execution_task_id} does not match the saved plan"
                    )
                session_task = await self._session_store.start_session_task(
                    session_id,
                    plan_execution_task_id,
                    datetime.now(UTC),
                )

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
        )
        emit = recorder.emit
        finish_session_task = recorder.finish_session_task
        record_turn_failure = recorder.record_turn_failure
        finalize_turn_completion = recorder.finalize_turn_completion

        supervisor: AgentExecutionSupervisor | None = None

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
        verification_evidence: list[str] = []

        def record_workspace_evidence(report: WorkspaceChangeReport) -> None:
            for change in report.files:
                path = redact_sensitive_text(
                    change.path,
                    explicit_values=self._tool_context.redaction_values,
                )
                workspace_evidence.append(
                    f"{change.status} {path} (+{change.additions}/-{change.deletions})"
                )

        def record_verification_evidence(observation: ToolExecutionObservation) -> None:
            if observation.progress_kind is ProgressKind.VERIFICATION:
                verification_evidence.append(
                    "A verification result was recorded before finalization."
                )

        async def complete_finalized_turn(
            decision: SupervisorDecision,
            *,
            step: int,
        ) -> AgentRunResult:
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
                ModelContext(
                    tuple(context_items),
                    context_source_provider,
                    context_source_model,
                    context_source_affinity,
                    self._context_builder.reasoning_effort,
                ),
                FinalizationEvidence(
                    decision.reason_code,
                    workspace_changes=tuple(workspace_evidence),
                    verification=tuple(verification_evidence),
                    unverified_items=(
                        "No additional verification should be claimed without recorded evidence.",
                    ),
                    blocker=(
                        f"Execution stopped because {decision.reason_code.value.replace('_', ' ')}."
                    ),
                    uncertainty=(
                        "The final response is limited to evidence already recorded in the conversation.",
                    ),
                ),
            )
            return await complete_finalization_result(finalization, decision, step=step)

        async def complete_finalization_result(
            finalization: FinalizationResult,
            decision: SupervisorDecision,
            *,
            step: int,
        ) -> AgentRunResult:
            outcome = outcome_for_terminal_decision(decision)
            final_message = Message(Role.ASSISTANT, finalization.response)
            messages.append(final_message)
            if persist_turn_context:
                context_items.append(final_message)
            await emit(AgentEventKind.TEXT_DELTA, {"text": finalization.response})
            await finish_session_task(SessionTaskStatus.COMPLETED)
            result_items = tuple(context_items) if persist_turn_context else turn_context_prefix
            await finalize_turn_completion(
                outcome,
                {
                    "step": step,
                    "stop_reason": finalization.stop_reason,
                    "input_tokens": finalization.total_input_tokens,
                    "output_tokens": finalization.total_output_tokens,
                    "duration_seconds": monotonic() - turn_started_at,
                    "execution_status": outcome.status.value,
                    "execution_reason": outcome.reason_code.value
                    if outcome.reason_code is not None
                    else None,
                    "finalized": outcome.finalized,
                    "recoverable": outcome.recoverable,
                    "finalization_status": finalization.status.value,
                    "finalization_attempts": len(finalization.attempts),
                    "illegal_tool_calls": finalization.illegal_tool_calls,
                },
                result_items,
            )
            return AgentRunResult(
                session_id,
                finalization.response,
                tuple(messages),
                result_items,
                tuple(events),
                step,
                self._context_builder.plan,
                outcome,
            )

        response_parts: list[str] = []
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
                model_items = await run_blocking(
                    self._context_builder.build,
                    (*context_items, *completion_reminders),
                )
                context = ModelContext(
                    model_items,
                    context_source_provider,
                    context_source_model,
                    context_source_affinity,
                    self._context_builder.reasoning_effort,
                )
                before_model_decision = record_supervision(
                    SupervisionCheckpoint.BEFORE_MODEL,
                    step,
                    lambda current: current.authorize_model_request(),
                )
                terminal_before_model = controlled_terminal_decision(before_model_decision)
                if terminal_before_model is not None:
                    return await complete_finalized_turn(terminal_before_model, step=step - 1)
                step_result = await ModelStepProcessor(session_store=self._session_store).consume(
                    self._provider.stream(context, self._tools.definitions()),
                    emit=emit,
                    step=step,
                    step_started_at=step_started_at,
                    session_id=session_id,
                    can_adopt_provider_origin=can_adopt_provider_origin,
                    on_imperfect=lambda: setattr(recorder, "pristine_cancel_eligible", False),
                )
                step_text = step_result.text
                step_reasoning = step_result.reasoning
                tool_calls = step_result.tool_calls
                completion = step_result.completion

                if completion is None:
                    raise ProviderError("provider stream ended without a completion event")
                if completion.input_tokens is not None:
                    output_tokens = completion.output_tokens or 0
                    await emit(
                        AgentEventKind.CONTEXT_USAGE_UPDATED,
                        {
                            "input_tokens": completion.input_tokens,
                            "output_tokens": completion.output_tokens,
                            "used_tokens": completion.input_tokens + output_tokens,
                            "estimated": completion.output_tokens is None,
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
                if completion_batch and background_tasks is not None:
                    await background_tasks.mark_completions_reported(
                        tuple(snapshot.task_id for snapshot in completion_batch)
                    )
                    # The manager has now acknowledged this batch. Never
                    # include it in a later model step of the same turn.
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
                response_parts.append(assistant_content)
                assistant_message = Message(
                    Role.ASSISTANT,
                    assistant_content,
                    tool_calls=tuple(tool_calls),
                    reasoning_content="".join(step_reasoning) or None,
                )
                messages.append(assistant_message)
                if persist_turn_context:
                    context_items.append(assistant_message)

                if not tool_calls:
                    await finish_session_task(SessionTaskStatus.COMPLETED)
                    result_items = (
                        tuple(context_items) if persist_turn_context else turn_context_prefix
                    )
                    await finalize_turn_completion(
                        AgentExecutionOutcome(
                            AgentExecutionStatus.COMPLETED,
                            None,
                            finalized=False,
                            recoverable=False,
                        ),
                        {
                            "step": step,
                            "stop_reason": completion.stop_reason,
                            "input_tokens": completion.input_tokens,
                            "output_tokens": completion.output_tokens,
                            "duration_seconds": monotonic() - turn_started_at,
                        },
                        result_items,
                    )
                    return AgentRunResult(
                        session_id,
                        "".join(response_parts),
                        tuple(messages),
                        result_items,
                        tuple(events),
                        step,
                        self._context_builder.plan,
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

                def record_tool_outcome(
                    observation: ToolExecutionObservation,
                    *,
                    model_step: int = step,
                ) -> SupervisorDecision | None:
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

                for index, call in enumerate(tool_calls):
                    try:
                        observation = await self._tool_executor.execute(
                            call,
                            messages,
                            context_items,
                            emit,
                            session_id,
                            interrupted_observation_sink=record_interrupted_tool_outcome,
                            workspace_change_sink=(
                                record_workspace_evidence
                                if self._execution_control_mode
                                is ExecutionControlMode.FINALIZE_TERMINAL
                                else None
                            ),
                        )
                        if observation is None:
                            disable_supervision("tool_observation_unavailable")
                        else:
                            record_verification_evidence(observation)
                            pending_terminal_decision = select_terminal_decision(
                                pending_terminal_decision,
                                record_tool_outcome(observation),
                            )
                    except BaseException as error:
                        await self._tool_executor.record_unstarted_tool_calls(
                            tool_calls[index + 1 :],
                            messages,
                            context_items,
                            emit,
                            cancelled=isinstance(error, asyncio.CancelledError),
                        )
                        raise
                if pending_terminal_decision is not None:
                    return await complete_finalized_turn(pending_terminal_decision, step=step)
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
            if self._session_store is not None and session_id is not None:
                await self._session_store.save_session_items(
                    session_id,
                    context_items if persist_turn_context else turn_context_prefix,
                )
            raise ProviderError(f"agent exceeded the maximum of {self._max_steps} model steps")
        except BaseException as error:
            # Preserve cancellation semantics while still making the session auditable.
            await record_turn_failure(error)
            raise


__all__ = ["AgentLoopRunner", "AgentRunResult"]
