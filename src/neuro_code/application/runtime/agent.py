from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic

from neuro_code.application.permissions.contracts import (
    PermissionApproval,
    build_permission_request,
)
from neuro_code.application.ports.approval import PermissionApprover
from neuro_code.application.ports.model import ModelProvider
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.tools import Tool, ToolCollection, ToolContext
from neuro_code.application.ports.workspace_changes import (
    WorkspaceChangeCheckpoint,
    WorkspaceChangeObserver,
    WorkspaceChangeReport,
)
from neuro_code.application.runtime.background_task_reminders import (
    BACKGROUND_TASK_COMPLETION_BATCH_LIMIT,
    format_background_task_completion_reminder,
)
from neuro_code.application.runtime.finalization import (
    AgentFinalizer,
    FinalizationEvidence,
    FinalizationResult,
    Finalizer,
)
from neuro_code.application.runtime.supervision import (
    AgentExecutionSupervisor,
    ExecutionControlMode,
    StableMetadataFact,
    SupervisionCheckpoint,
    SupervisionObserver,
    SupervisionTraceRecord,
    ToolExecutionObservation,
    create_observing_supervisor,
    stable_metadata_fact,
)
from neuro_code.domain.background_tasks import BackgroundTaskSnapshot
from neuro_code.domain.events import AgentEvent, AgentEventKind
from neuro_code.domain.execution import (
    AgentExecutionOutcome,
    AgentExecutionStatus,
    ProgressKind,
    SessionExecutionRecord,
    SupervisorDecision,
    SupervisorDecisionKind,
    SupervisorReasonCode,
    TurnCancellationPolicy,
    TurnSource,
)
from neuro_code.domain.instructions import InstructionDiscoveryResult
from neuro_code.domain.interaction_mode import InteractionMode, interaction_mode_guidance
from neuro_code.domain.messages import (
    ContentPart,
    Message,
    PreservedContextItem,
    Role,
    SessionItem,
    SyntheticReason,
    ToolCall,
)
from neuro_code.domain.model_context import ModelContext
from neuro_code.domain.model_events import (
    ModelBackendToolCompleted,
    ModelBackendToolStarted,
    ModelCompleted,
    ModelProviderAttemptFailed,
    ModelProviderSelected,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.plans import PlanComment, SessionPlan
from neuro_code.domain.reasoning import ReasoningEffort, reasoning_guidance
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.session_tasks import SessionTask, SessionTaskKind, SessionTaskStatus
from neuro_code.domain.skills import SkillDiscoveryResult
from neuro_code.domain.tools import ToolResult
from neuro_code.permissions import (
    PermissionDecision,
    PermissionEffect,
    PermissionManager,
    PermissionMode,
)
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import ConfigurationError, ProviderError, ToolError
from neuro_code.shared.redaction import redact_sensitive_text

EventSink = Callable[[AgentEvent], Awaitable[None] | None]


FinalizerFactory = Callable[[ModelProvider, int, tuple[str, ...]], Finalizer]

LOGGER = logging.getLogger(__name__)

_SUPERVISION_METADATA_KEYS = frozenset(
    {
        "client_delegated",
        "count",
        "exit_code",
        "is_background",
        "requested_count",
        "status",
        "terminal_count",
        "timed_out",
        "total_lines",
        "total_output_bytes",
        "truncated",
    }
)
_BACKGROUND_STATE_TOOL_NAMES = frozenset({"kill_task", "task_output"})


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
        self._reasoning_effort = reasoning_effort
        self._instruction_provider = instruction_provider
        self._skill_provider = skill_provider
        self._last_instruction_result: InstructionDiscoveryResult | None = None
        self._last_skill_result: SkillDiscoveryResult | None = None
        self._supervisor_factory = supervisor_factory or create_observing_supervisor
        self._supervision_observer = supervision_observer
        self._execution_control_mode = execution_control_mode
        self._finalizer_factory = finalizer_factory or _create_finalizer
        self._finalizer_max_attempts = finalizer_max_attempts
        self._plan = plan
        self._plan_comments: tuple[PlanComment, ...] = ()
        self.set_plan_comments(plan_comments)
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
        self._interaction_mode = interaction_mode or inferred_mode
        self._apply_interaction_mode_permissions()

    @property
    def sandbox_profile(self) -> SandboxProfile:
        return self._tool_context.sandbox_profile

    @property
    def reasoning_effort(self) -> ReasoningEffort:
        return self._reasoning_effort

    def set_reasoning_effort(self, effort: ReasoningEffort) -> None:
        if not isinstance(effort, ReasoningEffort):
            raise TypeError("reasoning effort must be a ReasoningEffort")
        self._reasoning_effort = effort

    @property
    def interaction_mode(self) -> InteractionMode:
        return self._interaction_mode

    @property
    def auto_mode_unrestricted(self) -> bool:
        return self._auto_permission_mode is PermissionMode.BYPASS

    @property
    def plan(self) -> SessionPlan | None:
        return self._plan

    @property
    def plan_comments(self) -> tuple[PlanComment, ...]:
        return self._plan_comments

    def set_plan(self, plan: SessionPlan | None) -> None:
        if plan is not None and not isinstance(plan, SessionPlan):
            raise TypeError("plan must be a SessionPlan or None")
        self._plan = plan
        self._plan_comments = ()

    def set_plan_comments(self, comments: Sequence[PlanComment]) -> None:
        normalized = tuple(comments)
        if not all(isinstance(comment, PlanComment) for comment in normalized):
            raise TypeError("plan comments must be PlanComment values")
        if normalized and self._plan is None:
            raise ValueError("plan comments require a saved plan")
        if self._plan is not None and any(
            comment.step_index > len(self._plan.steps) for comment in normalized
        ):
            raise ValueError("plan comments must refer to saved steps")
        self._plan_comments = normalized

    def set_interaction_mode(self, mode: InteractionMode) -> None:
        if not isinstance(mode, InteractionMode):
            raise TypeError("interaction mode must be an InteractionMode")
        self._interaction_mode = mode
        self._apply_interaction_mode_permissions()

    def _apply_interaction_mode_permissions(self) -> None:
        permission_mode = {
            InteractionMode.NORMAL: PermissionMode.DEFAULT,
            InteractionMode.ACCEPT_EDITS: PermissionMode.ACCEPT_EDITS,
            InteractionMode.PLAN: PermissionMode.DONT_ASK,
            InteractionMode.AUTO: self._auto_permission_mode,
        }[self._interaction_mode]
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
        """

        guidance_parts = [
            reasoning_guidance(self._reasoning_effort),
            interaction_mode_guidance(self._interaction_mode),
        ]
        if self._plan is not None:
            guidance_parts.append(self._plan.model_guidance())
            comments = self._plan.comment_guidance(self._plan_comments)
            if comments:
                guidance_parts.append(comments)
        guidance = "\n\n".join(guidance_parts)
        rendered = list(items)

        # Apply guidance to the system message (or create one if missing).
        system_index: int | None = None
        for index, item in enumerate(rendered):
            if isinstance(item, Message) and item.role is Role.SYSTEM:
                system_index = index
                break
        if system_index is not None:
            original = rendered[system_index]
            assert isinstance(original, Message)
            guided = Message(Role.SYSTEM, f"{original.model_content()}\n\n{guidance}")
            rendered[system_index] = guided
        else:
            rendered.insert(0, Message(Role.SYSTEM, guidance))
            system_index = 0

        # Refresh and inject repository instructions as a synthetic User message.
        instruction_result = self._refresh_instructions()
        if instruction_result is not None and instruction_result.files:
            instruction_msg = instruction_result.instruction_message()
            # Insert after the system message.
            rendered.insert(system_index + 1, instruction_msg)

        # Refresh and inject available skills as a synthetic User message.
        # Inserted after the instruction message (or after the system message
        # if no instructions were found) so the model sees skills after
        # project conventions.
        skill_result = self._refresh_skills()
        if skill_result is not None and skill_result.files:
            skill_msg = skill_result.skill_message()
            # Find the insertion point: after the instruction message if
            # present, otherwise after the system message.
            insert_at = system_index + 1
            for i in range(system_index + 1, min(len(rendered), system_index + 3)):
                item = rendered[i]
                if (
                    isinstance(item, Message)
                    and item.synthetic_reason is SyntheticReason.PROJECT_INSTRUCTIONS
                ):
                    insert_at = i + 1
                    break
            rendered.insert(insert_at, skill_msg)

        return tuple(rendered)

    def _refresh_instructions(self) -> InstructionDiscoveryResult | None:
        """Call the instruction provider to get fresh discovered instructions.

        This is called before each model step so that instruction file changes
        within the same session are picked up on the next turn.
        """
        if self._instruction_provider is None:
            self._last_instruction_result = None
            return None
        self._last_instruction_result = self._instruction_provider()
        return self._last_instruction_result

    @property
    def instruction_result(self) -> InstructionDiscoveryResult | None:
        """Return the most recent instruction discovery result, if any."""
        return self._last_instruction_result

    def _refresh_skills(self) -> SkillDiscoveryResult | None:
        """Call the skill provider to get fresh discovered skills.

        This is called before each model step so that skill file changes
        within the same session are picked up on the next turn.
        """
        if self._skill_provider is None:
            self._last_skill_result = None
            return None
        self._last_skill_result = self._skill_provider()
        return self._last_skill_result

    @property
    def skill_result(self) -> SkillDiscoveryResult | None:
        """Return the most recent skill discovery result, if any."""
        return self._last_skill_result

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
        if plan_execution_requested and self._plan is None:
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
            session_id = await self._session_store.create_session(
                str(self._tool_context.cwd),
                self._provider.provider_name,
                self._provider.model_name,
                getattr(self._provider, "context_affinity", None),
                self._tool_context.sandbox_profile,
            )
        elif self._session_store is not None and session_id is not None:
            sequence = await self._session_store.next_event_sequence(session_id) - 1
        if plan_execution_requested and (self._session_store is None or session_id is None):
            raise ConfigurationError("session-backed task storage is unavailable")
        if plan_execution_requested and self._plan is None:
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
                    plan_snapshot=self._plan,
                )
                await self._session_store.create_session_task(session_id, session_task)
            else:
                queued_task = await self._session_store.get_session_task(
                    session_id,
                    plan_execution_task_id,
                )
                if queued_task is None:
                    raise ConfigurationError(f"unknown queued plan task: {plan_execution_task_id}")
                if queued_task.kind is not SessionTaskKind.PLAN_EXECUTION:
                    raise ConfigurationError("only plan execution tasks can be started")
                if queued_task.status is not SessionTaskStatus.QUEUED:
                    raise ConfigurationError(f"plan task {plan_execution_task_id} is not queued")
                if queued_task.plan_snapshot != self._plan:
                    raise ConfigurationError(
                        f"plan task {plan_execution_task_id} does not match the saved plan"
                    )
                session_task = await self._session_store.start_session_task(
                    session_id,
                    plan_execution_task_id,
                    datetime.now(UTC),
                )

        async def deliver(event: AgentEvent) -> None:
            if sink is not None:
                outcome = sink(event)
                if inspect.isawaitable(outcome):
                    await outcome

        async def emit(
            kind: AgentEventKind,
            data: dict[str, object],
            *,
            persist: bool = True,
            deliver_event: bool = True,
        ) -> AgentEvent:
            nonlocal sequence
            sequence += 1
            event = AgentEvent.create(sequence, kind, data)
            events.append(event)
            if persist and self._session_store is not None and session_id is not None:
                await self._session_store.append_event(session_id, event)
            if deliver_event:
                await deliver(event)
            return event

        async def finish_session_task(status: SessionTaskStatus) -> None:
            nonlocal session_task
            if session_task is None:
                return
            assert self._session_store is not None
            assert session_id is not None
            task = session_task.finish(status, finished_at=datetime.now(UTC))
            await self._session_store.update_session_task(session_id, task)
            session_task = task
            if status is SessionTaskStatus.COMPLETED:
                event_kind = AgentEventKind.SESSION_TASK_COMPLETED
            elif status is SessionTaskStatus.FAILED:
                event_kind = AgentEventKind.SESSION_TASK_FAILED
            elif status is SessionTaskStatus.CANCELLED:
                event_kind = AgentEventKind.SESSION_TASK_CANCELLED
            else:
                raise AssertionError("a session task must finish in a terminal state")
            await emit(event_kind, {"task": task.to_dict()})

        async def record_turn_failure(error: BaseException) -> None:
            cancelled = isinstance(error, asyncio.CancelledError)
            pristine_rewound = cancelled and pristine_cancel_eligible
            await finish_session_task(
                SessionTaskStatus.CANCELLED if cancelled else SessionTaskStatus.FAILED
            )
            await emit(
                AgentEventKind.TURN_FAILED,
                {
                    "error_type": type(error).__name__,
                    "message": "turn cancelled" if cancelled else str(error),
                    "cancelled": cancelled,
                    "pristine_rewound": pristine_rewound,
                    "duration_seconds": monotonic() - turn_started_at,
                },
            )
            if self._session_store is not None and session_id is not None:
                await self._session_store.save_session_items(
                    session_id,
                    (
                        turn_context_prefix
                        if pristine_rewound or not persist_turn_context
                        else context_items
                    ),
                )

        async def finalize_turn_completion(
            outcome: AgentExecutionOutcome,
            data: dict[str, object],
            result_items: Sequence[SessionItem],
        ) -> None:
            completed_event = await emit(
                AgentEventKind.TURN_COMPLETED,
                data,
                persist=False,
                deliver_event=False,
            )
            record = (
                None
                if turn_source is TurnSource.BACKGROUND_TASK_AUTO_WAKE
                else SessionExecutionRecord(
                    outcome,
                    completed_event.sequence,
                    completed_event.created_at,
                )
            )
            if self._session_store is not None and session_id is not None:
                await self._session_store.finalize_turn(
                    session_id,
                    completed_event,
                    result_items,
                    record,
                )
            await deliver(completed_event)

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
                    self._reasoning_effort,
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
                self._plan,
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
                assert self._plan is not None
                await emit(
                    AgentEventKind.PLAN_EXECUTION_REQUESTED,
                    {"plan": self._plan.to_dict()},
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
                thinking_completed = False

                async def complete_thinking(
                    step_number: int = step,
                    started_at: float = step_started_at,
                ) -> None:
                    nonlocal thinking_completed
                    if thinking_completed:
                        return
                    thinking_completed = True
                    await emit(
                        AgentEventKind.MODEL_THINKING_COMPLETED,
                        {
                            "step": step_number,
                            "duration_seconds": monotonic() - started_at,
                        },
                    )

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
                step_text: list[str] = []
                step_reasoning: list[str] = []
                tool_calls: list[ToolCall] = []
                completion: ModelCompleted | None = None
                backend_tool_started_at: dict[str, float] = {}

                model_items = await run_blocking(
                    self._model_items_with_reasoning_guidance,
                    (*context_items, *completion_reminders),
                )
                context = ModelContext(
                    model_items,
                    context_source_provider,
                    context_source_model,
                    context_source_affinity,
                    self._reasoning_effort,
                )
                before_model_decision = record_supervision(
                    SupervisionCheckpoint.BEFORE_MODEL,
                    step,
                    lambda current: current.authorize_model_request(),
                )
                terminal_before_model = controlled_terminal_decision(before_model_decision)
                if terminal_before_model is not None:
                    return await complete_finalized_turn(terminal_before_model, step=step - 1)
                async for model_event in self._provider.stream(context, self._tools.definitions()):
                    if isinstance(model_event, ModelProviderAttemptFailed):
                        await emit(
                            AgentEventKind.PROVIDER_ATTEMPT_FAILED,
                            {
                                "provider": model_event.provider,
                                "model": model_event.model,
                                "error_type": model_event.error_type,
                                "message": model_event.message,
                            },
                        )
                    elif isinstance(model_event, ModelProviderSelected):
                        origin_updated = False
                        if (
                            can_adopt_provider_origin
                            and self._session_store is not None
                            and session_id is not None
                        ):
                            await self._session_store.update_session_provider(
                                session_id,
                                model_event.provider,
                                model_event.model,
                                model_event.context_affinity,
                            )
                            origin_updated = True
                        await emit(
                            AgentEventKind.PROVIDER_SELECTED,
                            {
                                "provider": model_event.provider,
                                "model": model_event.model,
                                "context_window_tokens": model_event.context_window_tokens,
                                "failover": model_event.failover,
                                "session_origin_updated": origin_updated,
                            },
                        )
                    elif isinstance(model_event, ModelTextDelta):
                        await complete_thinking()
                        if model_event.text:
                            pristine_cancel_eligible = False
                        step_text.append(model_event.text)
                        await emit(AgentEventKind.TEXT_DELTA, {"text": model_event.text})
                    elif isinstance(model_event, ModelReasoningDelta):
                        if model_event.text:
                            pristine_cancel_eligible = False
                        step_reasoning.append(model_event.text)
                        await emit(
                            AgentEventKind.REASONING_DELTA,
                            {"text": model_event.text},
                        )
                    elif isinstance(model_event, ModelBackendToolStarted):
                        await complete_thinking()
                        pristine_cancel_eligible = False
                        backend_tool_started_at[model_event.call_id] = monotonic()
                        await emit(
                            AgentEventKind.BACKEND_TOOL_STARTED,
                            {"id": model_event.call_id, "name": model_event.name},
                        )
                    elif isinstance(model_event, ModelBackendToolCompleted):
                        await complete_thinking()
                        pristine_cancel_eligible = False
                        started_at = backend_tool_started_at.pop(
                            model_event.call_id,
                            step_started_at,
                        )
                        await emit(
                            AgentEventKind.BACKEND_TOOL_COMPLETED,
                            {
                                "id": model_event.call_id,
                                "name": model_event.name,
                                "duration_seconds": monotonic() - started_at,
                            },
                        )
                    elif isinstance(model_event, ModelToolCall):
                        await complete_thinking()
                        pristine_cancel_eligible = False
                        tool_calls.append(model_event.call)
                    elif isinstance(model_event, ModelCompleted):
                        await complete_thinking()
                        pristine_cancel_eligible = False
                        completion = model_event

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
                        self._plan,
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
                        observation = await self._execute_tool(
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
                        await self._record_unstarted_tool_calls(
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

    async def _execute_tool(
        self,
        call: ToolCall,
        messages: list[Message],
        context_items: list[SessionItem],
        emit: Callable[[AgentEventKind, dict[str, object]], Awaitable[AgentEvent]],
        session_id: str | None,
        *,
        interrupted_observation_sink: Callable[[ToolExecutionObservation], None] | None = None,
        workspace_change_sink: Callable[[WorkspaceChangeReport], None] | None = None,
    ) -> ToolExecutionObservation | None:
        resolved = False
        tool_requested_at = monotonic()
        workspace_before: WorkspaceChangeCheckpoint | None = None
        change_report: WorkspaceChangeReport | None = None
        tool: Tool | None = None
        result: ToolResult | None = None
        plan_fingerprint_before = self._plan.fingerprint if self._plan is not None else None

        def terminal_event_data(result: ToolResult, **extra: object) -> dict[str, object]:
            return {
                "id": call.id,
                "name": call.name,
                **result.to_dict(),
                "duration_seconds": monotonic() - tool_requested_at,
                **extra,
            }

        def record_result(result: ToolResult) -> None:
            nonlocal resolved
            if resolved:
                return
            message = Message(Role.TOOL, result.content, name=call.name, tool_call_id=call.id)
            messages.append(message)
            context_items.append(message)
            resolved = True

        try:
            await emit(
                AgentEventKind.TOOL_REQUESTED,
                {"id": call.id, "name": call.name, "arguments": dict(call.arguments)},
            )
            tool = self._tools.get(call.name)
            if tool is None:
                result = ToolResult(f"unknown tool: {call.name}", is_error=True)
                record_result(result)
                await emit(
                    AgentEventKind.TOOL_FAILED,
                    terminal_event_data(result),
                )
                return self._tool_execution_observation(
                    call,
                    result,
                    tool=None,
                    change_report=None,
                    plan_fingerprint_before=plan_fingerprint_before,
                )

            decision = self._permissions.decide(
                call.name,
                call.arguments,
                side_effecting=tool.side_effecting,
            )
            await emit(
                AgentEventKind.TOOL_PERMISSION,
                {
                    "id": call.id,
                    "name": call.name,
                    "effect": decision.effect.value,
                    "reason": decision.reason,
                },
            )
            if decision.effect is PermissionEffect.ASK:
                request = build_permission_request(
                    call.id,
                    call.name,
                    call.arguments,
                    decision.reason,
                )
                await emit(
                    AgentEventKind.TOOL_APPROVAL_REQUESTED,
                    {
                        "id": call.id,
                        "name": call.name,
                        "reason": request.reason,
                        "summary": request.summary,
                    },
                )
                approval = (
                    await self._approver.request(request)
                    if self._approver is not None
                    else PermissionApproval.deny("interactive approval interface is unavailable")
                )
                effect = PermissionEffect.ALLOW if approval.allowed else PermissionEffect.DENY
                decision = PermissionDecision(effect, approval.reason)
                await emit(
                    AgentEventKind.TOOL_APPROVAL_RESOLVED,
                    {
                        "id": call.id,
                        "name": call.name,
                        "effect": effect.value,
                        "outcome": approval.kind.value,
                        "reason": approval.reason,
                    },
                )
            if not decision.allowed:
                result = ToolResult(f"permission denied: {decision.reason}", is_error=True)
                record_result(result)
                await emit(
                    AgentEventKind.TOOL_FAILED,
                    terminal_event_data(result),
                )
                return self._tool_execution_observation(
                    call,
                    result,
                    tool=tool,
                    change_report=None,
                    plan_fingerprint_before=plan_fingerprint_before,
                )

            await emit(AgentEventKind.TOOL_STARTED, {"id": call.id, "name": call.name})
            if tool.side_effecting:
                workspace_before = await self._capture_workspace_snapshot()
            try:
                result = await tool.execute(call.arguments, self._tool_context)
            except (ToolError, OSError, UnicodeError) as error:
                result = ToolResult(f"{type(error).__name__}: {error}", is_error=True)
            safe_content = redact_sensitive_text(
                result.content,
                explicit_values=self._tool_context.redaction_values,
            )
            if safe_content != result.content:
                result = ToolResult(
                    safe_content,
                    is_error=result.is_error,
                    metadata=result.metadata,
                )
            kind = AgentEventKind.TOOL_FAILED if result.is_error else AgentEventKind.TOOL_COMPLETED
            plan = self._plan_from_tool_result(call.name, result)
            if plan is not None:
                if self._session_store is None or session_id is None:
                    raise ToolError("session-backed plan storage is unavailable")
                await self._session_store.save_session_plan(session_id, plan)
                self._plan = plan
                self._plan_comments = ()
            record_result(result)
            terminal_data = terminal_event_data(result)
            change_report = await self._workspace_change_report(workspace_before)
            if change_report is not None:
                terminal_data["workspace_changes"] = change_report.to_event_payload()
            await emit(kind, terminal_data)
            if plan is not None:
                await emit(AgentEventKind.PLAN_UPDATED, plan.to_dict())
            if change_report is not None and workspace_change_sink is not None:
                workspace_change_sink(change_report)
            return self._tool_execution_observation(
                call,
                result,
                tool=tool,
                change_report=change_report,
                plan_fingerprint_before=plan_fingerprint_before,
            )
        except BaseException as error:
            if not resolved:
                cancelled = isinstance(error, asyncio.CancelledError)
                result = ToolResult(
                    (
                        "tool call cancelled before completion"
                        if cancelled
                        else "tool call interrupted before completion"
                    ),
                    is_error=True,
                )
                record_result(result)
                terminal_data = terminal_event_data(result, cancelled=cancelled)
                change_report = await self._workspace_change_report(workspace_before)
                if change_report is not None:
                    terminal_data["workspace_changes"] = change_report.to_event_payload()
                await emit(
                    AgentEventKind.TOOL_FAILED,
                    terminal_data,
                )
            if result is not None and interrupted_observation_sink is not None:
                observation = self._tool_execution_observation(
                    call,
                    result,
                    tool=tool,
                    change_report=change_report,
                    plan_fingerprint_before=plan_fingerprint_before,
                )
                if observation is not None:
                    interrupted_observation_sink(observation)
            raise

    def _tool_execution_observation(
        self,
        call: ToolCall,
        result: ToolResult,
        *,
        tool: Tool | None,
        change_report: WorkspaceChangeReport | None,
        plan_fingerprint_before: str | None,
    ) -> ToolExecutionObservation | None:
        """Build a fail-open, redacted supervision record after a tool terminal path."""

        try:
            workspace_changed = change_report is not None and bool(change_report.files)
            workspace_progress_token = self._workspace_progress_token(change_report)
            current_plan_fingerprint = self._plan.fingerprint if self._plan is not None else None
            plan_fingerprint = (
                current_plan_fingerprint
                if current_plan_fingerprint != plan_fingerprint_before
                else None
            )
            external_state_token = self._background_state_token(call.name, result.metadata)
            if workspace_changed:
                progress_kind = ProgressKind.WORKSPACE
            elif plan_fingerprint is not None:
                progress_kind = ProgressKind.PLAN
            elif external_state_token is not None:
                progress_kind = ProgressKind.EXTERNAL_STATE
            elif not result.is_error and tool is not None and not tool.side_effecting:
                progress_kind = ProgressKind.EVIDENCE
            else:
                progress_kind = ProgressKind.NONE
            return ToolExecutionObservation.from_result(
                tool_name=call.name,
                arguments=call.arguments,
                result_content=result.content,
                is_error=result.is_error,
                metadata_facts=self._supervision_metadata_facts(result.metadata),
                workspace_changed=workspace_changed,
                workspace_progress_token=workspace_progress_token,
                plan_fingerprint=plan_fingerprint,
                external_state_token=external_state_token,
                progress_kind=progress_kind,
                path_context=None,
                redaction_values=self._tool_context.redaction_values,
                tool_call_id=call.id,
            )
        except Exception as error:
            LOGGER.debug(
                "supervision tool observation unavailable error_type=%s",
                type(error).__name__,
            )
            return None

    def _supervision_metadata_facts(
        self,
        metadata: Mapping[str, object] | None,
    ) -> tuple[StableMetadataFact, ...]:
        if metadata is None:
            return ()
        facts: list[StableMetadataFact] = []
        for name in sorted(_SUPERVISION_METADATA_KEYS.intersection(metadata)):
            value = metadata[name]
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, int):
                rendered = str(value)
            elif isinstance(value, str):
                rendered = value
            else:
                continue
            facts.append(
                stable_metadata_fact(
                    name,
                    rendered,
                    redaction_values=self._tool_context.redaction_values,
                )
            )
        return tuple(facts)

    @staticmethod
    def _workspace_progress_token(change_report: WorkspaceChangeReport | None) -> str | None:
        if change_report is None or not change_report.files:
            return None
        payload = {
            "files": [
                {
                    "additions": change.additions,
                    "deletions": change.deletions,
                    "path": change.path,
                    "status": change.status,
                }
                for change in change_report.files
            ],
            "omitted_files": change_report.omitted_files,
            "scan_limited": change_report.scan_limited,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _background_state_token(
        tool_name: str,
        metadata: Mapping[str, object] | None,
    ) -> str | None:
        if tool_name not in _BACKGROUND_STATE_TOOL_NAMES or metadata is None:
            return None
        values: list[str] = []
        for name in ("status", "total_output_bytes", "exit_code"):
            value = metadata.get(name)
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, int):
                rendered = str(value)
            elif isinstance(value, str):
                rendered = value
            else:
                continue
            values.append(f"{name}={rendered}")
        return "|".join(values) if values else None

    @staticmethod
    def _plan_from_tool_result(name: str, result: ToolResult) -> SessionPlan | None:
        if name != "update_plan" or result.is_error or result.metadata is None:
            return None
        raw_plan = result.metadata.get("plan")
        try:
            return SessionPlan.from_dict(raw_plan)
        except ValueError as error:
            raise ToolError("update_plan returned an invalid plan") from error

    async def _capture_workspace_snapshot(self) -> WorkspaceChangeCheckpoint | None:
        try:
            return await run_blocking(
                self._workspace_change_observer.capture, self._tool_context.cwd
            )
        except (OSError, RuntimeError):
            return None

    async def _workspace_change_report(
        self,
        before: WorkspaceChangeCheckpoint | None,
    ) -> WorkspaceChangeReport | None:
        if before is None:
            return None
        after = await self._capture_workspace_snapshot()
        if after is None:
            return None
        protected_names = {
            name.casefold() for name in self._tool_context.protected_environment_variables
        }
        redactions = tuple(
            dict.fromkeys(
                value
                for name, value in os.environ.items()
                if name.casefold() in protected_names and value
            )
        )
        report = await run_blocking(
            self._workspace_change_observer.compare,
            before,
            after,
            explicit_redactions=redactions,
        )
        return report if report.should_emit else None

    @staticmethod
    async def _record_unstarted_tool_calls(
        calls: Sequence[ToolCall],
        messages: list[Message],
        context_items: list[SessionItem],
        emit: Callable[[AgentEventKind, dict[str, object]], Awaitable[AgentEvent]],
        *,
        cancelled: bool,
    ) -> None:
        if not calls:
            return
        result = ToolResult(
            (
                "tool call cancelled before execution"
                if cancelled
                else "tool call skipped because the turn stopped"
            ),
            is_error=True,
        )
        for call in calls:
            message = Message(Role.TOOL, result.content, name=call.name, tool_call_id=call.id)
            messages.append(message)
            context_items.append(message)
        for call in calls:
            await emit(
                AgentEventKind.TOOL_FAILED,
                {
                    "id": call.id,
                    "name": call.name,
                    **result.to_dict(),
                    "cancelled": cancelled,
                    "not_started": True,
                },
            )
