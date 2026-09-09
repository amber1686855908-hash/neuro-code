"""Application-owned multi-turn session controller.

提供由应用层拥有的多回合会话控制器."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from neuro_code.application.memory.compaction import ProviderContextWindow
from neuro_code.application.memory.compaction_runtime import (
    ContextCompactionCommandResult,
    ContextCompactionRuntimeBoundary,
    ContextCompactionRuntimeRequest,
    ContextCompactionRuntimeResult,
    ContextCompactionSafePoint,
    ContextCompactionTurnProjection,
    project_context_compaction_command_result,
    project_context_compaction_failure,
    project_context_compaction_result,
)
from neuro_code.application.memory.compaction_trigger import ContextCompactionTriggerMode
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.tools import Tool
from neuro_code.application.ports.workspace import WorkspaceIdentity
from neuro_code.application.runtime.agent import AgentRunResult, AgentRuntime, EventSink
from neuro_code.application.runtime.final_response import (
    FinalResponseContract,
    ResponseSource,
)
from neuro_code.application.runtime.finalization import (
    FinalizationEvidence,
    deterministic_fallback_result,
)
from neuro_code.application.runtime.verification import (
    VerificationReport,
    VerificationTracker,
)
from neuro_code.application.sessions.execution_queries import (
    LoadExecutionRecordRequest,
    SessionExecutionQueryService,
)
from neuro_code.application.sessions.item_queries import (
    LoadSessionItemsRequest,
    SessionItemQueryService,
)
from neuro_code.application.sessions.lifecycle import SessionLifecycleService, StartSessionRequest
from neuro_code.application.sessions.recovery import (
    TurnRecoveryInspection,
    TurnRecoveryService,
)
from neuro_code.application.sessions.requirements import NormalTurnRequirementsPolicy
from neuro_code.application.sessions.service import (
    ListPlanCommentsRequest,
    LoadSessionPlanRequest,
    SessionApplicationService,
)
from neuro_code.application.sessions.summary import (
    GetSessionSummaryRequest,
    SessionSummaryQueryService,
)
from neuro_code.application.sessions.task_queries import (
    GetSessionTaskRequest,
    ListSessionTasksRequest,
    SessionTaskQueryService,
)
from neuro_code.domain.background_tasks.models import BackgroundWakeState
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.messages import ContentPart, Message, Role, SessionItem
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.execution import (
    AgentExecutionOutcome,
    AgentExecutionStatus,
    SessionExecutionRecord,
    SupervisorReasonCode,
    TurnCancellationPolicy,
    TurnInput,
    TurnRecoveryAttempt,
    TurnRecoveryResolution,
    TurnRecoveryStatus,
    TurnSource,
    VerificationRequirementsSnapshot,
)
from neuro_code.domain.plans import PlanComment, SessionPlan
from neuro_code.domain.session_tasks import (
    MAX_QUEUED_SESSION_TASKS,
    SessionTask,
    SessionTaskKind,
    SessionTaskStatus,
)
from neuro_code.domain.ultracode import (
    MAX_ULTRACODE_RESULT_BYTES,
    UltracodeDelegationDecision,
)
from neuro_code.shared.errors import ConfigurationError

_T = TypeVar("_T")

PLAN_EXECUTION_PROMPT = (
    "The user has approved the current structured plan for execution. "
    "Continue from the in-progress step, or the first pending step. "
    "Keep the plan current with update_plan as work changes. "
    "Use tools only as needed and obey all permission, workspace, and sandbox boundaries."
)


class AgentConversation:
    """Own the durable context needed to run multiple turns in one session.

    管理一个会话运行多个回合所需的持久化上下文."""

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        store: SessionStore,
        items: Sequence[SessionItem] = (),
        session_id: str | None = None,
        source_provider: str | None = None,
        source_model: str | None = None,
        source_context_affinity: str | None = None,
        execution_record: SessionExecutionRecord | None = None,
    ) -> None:
        self._runtime = runtime
        self._store = store
        self._items = tuple(items)
        self._session_id = session_id
        self._source_provider = source_provider
        self._source_model = source_model
        self._source_context_affinity = source_context_affinity
        self._execution_record = execution_record
        self._turn_lock = asyncio.Lock()

    @classmethod
    async def open(
        cls,
        *,
        runtime: AgentRuntime,
        store: SessionStore,
        cwd: Path,
        workspace_identity: WorkspaceIdentity,
        resume_id: str | None = None,
    ) -> AgentConversation:
        if resume_id is None:
            return cls(runtime=runtime, store=store)

        session_service = SessionApplicationService(store)
        summary = await SessionSummaryQueryService(store).get_session_summary(
            GetSessionSummaryRequest(resume_id)
        )
        if not workspace_identity.matches(summary.cwd, cwd):
            raise ConfigurationError(
                f"session workspace is {summary.cwd}, not the requested cwd {cwd}"
            )
        if (
            summary.sandbox_profile is not None
            and summary.sandbox_profile is not runtime.sandbox_profile
        ):
            raise ConfigurationError(
                f"session sandbox profile is {summary.sandbox_profile.value!r}, "
                f"not the active profile {runtime.sandbox_profile.value!r}"
            )
        plan = await session_service.load_session_plan(LoadSessionPlanRequest(resume_id))
        runtime.set_plan(plan)
        if plan is not None:
            runtime.set_plan_comments(
                await session_service.list_plan_comments(ListPlanCommentsRequest(resume_id, plan))
            )
        return cls(
            runtime=runtime,
            store=store,
            items=await SessionItemQueryService(store).load_session_items(
                LoadSessionItemsRequest(resume_id)
            ),
            session_id=resume_id,
            source_provider=summary.provider,
            source_model=summary.model,
            source_context_affinity=summary.context_affinity,
            execution_record=await SessionExecutionQueryService(store).load_execution_record(
                LoadExecutionRecordRequest(resume_id)
            ),
        )

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def items(self) -> tuple[SessionItem, ...]:
        return self._items

    @property
    def plan(self) -> SessionPlan | None:
        return self._runtime.plan

    @property
    def plan_comments(self) -> tuple[PlanComment, ...]:
        return self._runtime.plan_comments

    @property
    def execution_record(self) -> SessionExecutionRecord | None:
        """Return the latest safe terminal execution state restored for this session.

        返回当前会话恢复的最新安全终端执行状态."""

        return self._execution_record

    async def add_plan_comment(self, step_index: int, content: str) -> PlanComment:
        """Persist user feedback for one current-plan step without starting a turn.

        持久化当前计划步骤的一条用户反馈,但不启动模型回合."""

        if self.plan is None or self._session_id is None:
            raise ConfigurationError("cannot comment on a plan that has not been saved")
        async with self._turn_lock:
            plan = self.plan
            if plan is None or self._session_id is None:
                raise ConfigurationError("cannot comment on a plan that has not been saved")
            comment = PlanComment(
                f"plan-comment-{uuid.uuid4().hex}",
                step_index,
                content,
                datetime.now(UTC),
            )
            if comment.step_index > len(plan.steps):
                raise ConfigurationError("plan comment refers to an unknown step")
            await self._store.add_plan_comment(self._session_id, plan, comment)
            self._runtime.set_plan_comments((*self.plan_comments, comment))
            return comment

    async def list_plan_comments(self) -> tuple[PlanComment, ...]:
        if self.plan is None or self._session_id is None:
            return ()
        return await SessionApplicationService(self._store).list_plan_comments(
            ListPlanCommentsRequest(self._session_id, self.plan)
        )

    async def list_session_tasks(self) -> tuple[SessionTask, ...]:
        if self._session_id is None:
            return ()
        return await SessionTaskQueryService(self._store).list_session_tasks(
            ListSessionTasksRequest(self._session_id)
        )

    async def get_session_task(self, task_id: str) -> SessionTask | None:
        """Read one durable task from this conversation without starting a turn.

        从当前会话读取一个持久化任务,但不启动模型回合."""

        if self._session_id is None:
            return None
        return await SessionTaskQueryService(self._store).get_session_task(
            GetSessionTaskRequest(self._session_id, task_id)
        )

    async def schedule_plan(self) -> SessionTask:
        """Persist a bounded plan task without starting a model turn.

        持久化一个有界计划任务,但不启动模型回合."""

        if self.plan is None:
            raise ConfigurationError("cannot schedule a plan that has not been saved")
        if self._session_id is None:
            raise ConfigurationError("a session is required before scheduling a plan")
        async with self._turn_lock:
            plan = self.plan
            if plan is None or self._session_id is None:
                raise ConfigurationError("cannot schedule a plan that has not been saved")
            tasks = await SessionTaskQueryService(self._store).list_session_tasks(
                ListSessionTasksRequest(self._session_id)
            )
            queued_count = sum(
                task.status is SessionTaskStatus.QUEUED
                for task in tasks
                if task.kind is SessionTaskKind.PLAN_EXECUTION
            )
            if queued_count >= MAX_QUEUED_SESSION_TASKS:
                raise ConfigurationError(
                    f"at most {MAX_QUEUED_SESSION_TASKS} plan tasks may be queued"
                )
            task = SessionTask(
                f"task-{uuid.uuid4().hex}",
                SessionTaskKind.PLAN_EXECUTION,
                SessionTaskStatus.QUEUED,
                datetime.now(UTC),
                plan_snapshot=plan,
            )
            await self._store.create_session_task(self._session_id, task)
            return task

    @property
    def reasoning_effort(self) -> ReasoningEffort:
        return self._runtime.reasoning_effort

    def set_reasoning_effort(self, effort: ReasoningEffort) -> None:
        self._runtime.set_reasoning_effort(effort)

    @property
    def interaction_mode(self) -> InteractionMode:
        return self._runtime.interaction_mode

    @property
    def auto_mode_unrestricted(self) -> bool:
        return self._runtime.auto_mode_unrestricted

    def set_interaction_mode(self, mode: InteractionMode) -> None:
        self._runtime.set_interaction_mode(mode)

    @property
    def normal_requirements_enabled(self) -> bool:
        return self._runtime.normal_requirements_enabled

    def _resolve_new_normal_turn_requirements(
        self,
        requirements: VerificationRequirementsSnapshot | None,
        *,
        turn_source: TurnSource,
        ultracode_execution_id: str | None,
    ) -> VerificationRequirementsSnapshot | None:
        """Produce requirements only for fresh, user-facing normal turns.

        SessionTurnService normally resolves this before reaching the
        conversation.  The conversation keeps the same policy at the lower
        application seam for direct plan/session callers, while recovery and
        internal orchestration paths remain explicit pass-throughs.
        """

        if (
            requirements is None
            and self._runtime.normal_requirements_enabled
            and turn_source is TurnSource.USER
            and ultracode_execution_id is None
        ):
            return NormalTurnRequirementsPolicy.resolve(None)
        return requirements

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
        verification_requirements: VerificationRequirementsSnapshot | None = None,
        verification_workspace_mutation_id: str | None = None,
        resume_existing_attempt: bool = False,
    ) -> AgentRunResult:
        verification_requirements = self._resolve_new_normal_turn_requirements(
            verification_requirements,
            turn_source=turn_source,
            ultracode_execution_id=ultracode_execution_id,
        )
        async with self._turn_lock:

            async def capture_session(event: AgentEvent) -> None:
                if event.kind is AgentEventKind.SESSION_STARTED:
                    session_id = event.data.get("session_id")
                    if isinstance(session_id, str) and session_id:
                        self._session_id = session_id
                if sink is not None:
                    outcome = sink(event)
                    if inspect.isawaitable(outcome):
                        await outcome

            try:
                runtime_kwargs: dict[str, Any] = {
                    "sink": capture_session,
                    "content_parts": content_parts,
                    "initial_items": self._items,
                    "source_provider": self._source_provider,
                    "source_model": self._source_model,
                    "source_context_affinity": self._source_context_affinity,
                    "session_id": self._session_id,
                    "turn_id": turn_id,
                    "ultracode_execution_id": ultracode_execution_id,
                    "cancellation_policy": cancellation_policy,
                    "turn_source": turn_source,
                }
                if verification_requirements is not None:
                    runtime_kwargs["verification_requirements"] = verification_requirements
                if verification_workspace_mutation_id is not None:
                    runtime_kwargs["verification_workspace_mutation_id"] = (
                        verification_workspace_mutation_id
                    )
                if resume_existing_attempt:
                    runtime_kwargs["resume_existing_attempt"] = True
                result = await self._runtime.run(prompt, **runtime_kwargs)
            except asyncio.CancelledError:
                await self._reload_persisted_state()
                raise
            except Exception:
                await self._reload_persisted_state()
                raise
            self._items = result.items
            self._session_id = result.session_id
            await self._reload_plan_state()
            await self._reload_provider_origin()
            await self._reload_execution_record()
            return result

    async def ensure_persisted_session(self) -> str:
        """Create the parent session before an application-owned delegation."""

        async with self._turn_lock:
            if self._session_id is not None:
                return self._session_id
            summary = await SessionLifecycleService(self._store).start_session(
                StartSessionRequest(
                    str(self._runtime.cwd),
                    self._runtime.provider_name,
                    self._runtime.model_name,
                    self._runtime.context_affinity,
                    self._runtime.sandbox_profile,
                )
            )
            self._session_id = summary.id
            self._source_provider = summary.provider
            self._source_model = summary.model
            self._source_context_affinity = summary.context_affinity
            return summary.id

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
        verification_requirements: VerificationRequirementsSnapshot | None = None,
    ) -> AgentRunResult:
        """Commit one already-produced bounded result without calling a Provider.

        The exact turn identity is reused for idempotent recovery; a committed
        turn never appends a second assistant message.

        ``verification_requirements`` binds the durable ``TurnInput`` identity
        for an already-produced external result.  It does not claim that the
        external result was verification-aware; this boundary continues to
        use ``EXTERNAL_RESULT`` and is not the MAIN_MAX verification path.

        在不调用 Provider 的前提下提交一个已生成的有界结果.恢复时复用精确回合身份,
        已提交回合绝不会再次追加 assistant 消息。
        """

        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("external turn prompt must not be empty")
        if (
            not isinstance(response, str)
            or not response.strip()
            or len(response.encode("utf-8")) > MAX_ULTRACODE_RESULT_BYTES
        ):
            raise ConfigurationError(
                "external turn response is outside the bounded result contract"
            )
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ValueError("external turn id must not be empty")
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise ValueError("external execution id must not be empty")
        if not isinstance(decision, UltracodeDelegationDecision):
            raise TypeError("external turn decision must be canonical")
        parts = tuple(content_parts)
        if not all(isinstance(part, ContentPart) for part in parts):
            raise TypeError("external turn content parts must be canonical")

        response_contract = FinalResponseContract.committed(
            response,
            source=ResponseSource.EXTERNAL_RESULT,
        )
        return await self._commit_synthetic_turn(
            prompt,
            response=response,
            turn_id=turn_id,
            execution_id=execution_id,
            decision=decision,
            content_parts=parts,
            sink=sink,
            verification_requirements=verification_requirements,
            response_contract=response_contract,
            persist_response_event=False,
        )

    async def commit_deterministic_turn(
        self,
        prompt: str,
        *,
        turn_id: str,
        execution_id: str,
        decision: UltracodeDelegationDecision,
        content_parts: Sequence[ContentPart] = (),
        verification_requirements: VerificationRequirementsSnapshot | None = None,
        verification_workspace_mutation_id: str | None = None,
        workspace_changes: Sequence[str] = (),
        unverified_items: Sequence[str] = (),
        blocker: str | None = None,
        sink: EventSink | None = None,
    ) -> AgentRunResult:
        """Commit a parent-owned, evidence-only fallback without a Provider.

        This is deliberately a session-owner seam.  Ultracode may report why
        adoption failed, but it cannot write assistant history or completion
        events itself.  The tracker creates the bounded report, and the
        deterministic fallback is committed through the same atomic turn path
        as every other final response.
        """

        tracker = VerificationTracker(requirements=verification_requirements)
        if verification_workspace_mutation_id is not None:
            tracker.record_workspace_mutation()
        report = tracker.report()
        evidence = FinalizationEvidence(
            SupervisorReasonCode.NONE,
            workspace_changes=tuple(workspace_changes),
            verification=report.confirmed_items,
            unverified_items=(
                *report.unverified_items,
                *tuple(unverified_items),
                "No additional verification should be claimed without recorded evidence.",
            ),
            blocker=blocker,
            uncertainty=(
                "The final response is limited to evidence already recorded in the conversation.",
            ),
            verification_state=report.state,
            verification_evidence=report.evidence,
            verification_workspace_generation=report.workspace_generation,
            requirement_evaluations=report.requirement_evaluations,
        )
        finalization = deterministic_fallback_result(evidence)
        response_contract = FinalResponseContract.committed(
            finalization.response,
            source=ResponseSource.DETERMINISTIC_FALLBACK,
            verification=report,
        )
        parts = tuple(content_parts)
        if not all(isinstance(part, ContentPart) for part in parts):
            raise TypeError("deterministic turn content parts must be canonical")
        return await self._commit_synthetic_turn(
            prompt,
            response=finalization.response,
            turn_id=turn_id,
            execution_id=execution_id,
            decision=decision,
            content_parts=parts,
            sink=sink,
            verification_requirements=verification_requirements,
            verification_workspace_mutation_id=verification_workspace_mutation_id,
            response_contract=response_contract,
            verification=report,
            persist_response_event=True,
        )

    async def _commit_synthetic_turn(
        self,
        prompt: str,
        *,
        response: str,
        turn_id: str,
        execution_id: str,
        decision: UltracodeDelegationDecision,
        content_parts: Sequence[ContentPart],
        sink: EventSink | None,
        verification_requirements: VerificationRequirementsSnapshot | None,
        verification_workspace_mutation_id: str | None = None,
        response_contract: FinalResponseContract,
        verification: VerificationReport | None = None,
        persist_response_event: bool,
    ) -> AgentRunResult:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("synthetic turn prompt must not be empty")
        if (
            not isinstance(response, str)
            or not response.strip()
            or len(response.encode("utf-8")) > MAX_ULTRACODE_RESULT_BYTES
        ):
            raise ConfigurationError(
                "synthetic turn response is outside the bounded result contract"
            )
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ValueError("synthetic turn id must not be empty")
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise ValueError("synthetic execution id must not be empty")
        if not isinstance(decision, UltracodeDelegationDecision):
            raise TypeError("synthetic turn decision must be canonical")
        if not isinstance(response_contract, FinalResponseContract):
            raise TypeError("response_contract must be a FinalResponseContract")
        if not response_contract.is_committed or response_contract.response != response:
            raise ConfigurationError("synthetic turn requires a matching committed response")

        async with self._turn_lock:
            session_id = self._session_id
            if session_id is None:
                summary = await SessionLifecycleService(self._store).start_session(
                    StartSessionRequest(
                        str(self._runtime.cwd),
                        self._runtime.provider_name,
                        self._runtime.model_name,
                        self._runtime.context_affinity,
                        self._runtime.sandbox_profile,
                    )
                )
                session_id = summary.id
                self._session_id = session_id
                self._source_provider = summary.provider
                self._source_model = summary.model
                self._source_context_affinity = summary.context_affinity

            turn_input = TurnInput(
                prompt,
                tuple(content_parts),
                TurnSource.USER,
                verification_requirements=verification_requirements,
                verification_workspace_mutation_id=verification_workspace_mutation_id,
            )
            attempts = await self._store.load_turn_attempts(session_id)
            attempt = next((item for item in attempts if item.turn_id == turn_id), None)
            if attempt is not None:
                if attempt.input_fingerprint != turn_input.fingerprint:
                    raise ConfigurationError("synthetic turn identity is bound to different input")
                if attempt.resolution is TurnRecoveryResolution.COMMITTED:
                    completion = await self._load_committed_completion(
                        session_id,
                        turn_id,
                        execution_id,
                    )
                    stored_response = completion.get("response") if completion is not None else None
                    if not isinstance(stored_response, str) or not stored_response.strip():
                        raise ConfigurationError("synthetic turn committed result is missing")
                    if stored_response != response:
                        raise ConfigurationError("synthetic turn result identity conflicts")
                    stored_contract = (
                        FinalResponseContract.from_completion_metadata(stored_response, completion)
                        if completion is not None
                        else response_contract
                    )
                    await self._reload_execution_record()
                    return await self._external_result(
                        session_id,
                        stored_response,
                        turn_id,
                        sink=sink,
                        emit_completion=True,
                        response_contract=stored_contract,
                    )
                if (
                    attempt.resolution is not None
                    or attempt.status is TurnRecoveryStatus.INDETERMINATE
                ):
                    raise ConfigurationError(
                        "synthetic turn is already resolved or indeterminate; replay is disabled"
                    )
            else:
                if any(item.resolution is None for item in attempts):
                    raise ConfigurationError(
                        "session has another unresolved turn; synthetic replay is disabled"
                    )
                await self._store.start_turn_attempt(
                    TurnRecoveryAttempt.create(
                        turn_id=turn_id,
                        session_id=session_id,
                        input=turn_input,
                        accepted_at=datetime.now(UTC),
                    )
                )

            current_items = tuple(
                await SessionItemQueryService(self._store).load_session_items(
                    LoadSessionItemsRequest(session_id)
                )
            )
            if not any(isinstance(item, Message) for item in current_items):
                current_items = (*current_items, Message(Role.SYSTEM, self._runtime.system_prompt))
            user_message = Message(Role.USER, prompt, content_parts=tuple(content_parts))
            assistant_message = Message(Role.ASSISTANT, response)
            result_items = (*current_items, user_message, assistant_message)
            sequence = await self._store.next_event_sequence(session_id)
            response_event = (
                AgentEvent.create(sequence, AgentEventKind.TEXT_DELTA, {"text": response})
                if persist_response_event
                else None
            )
            completion_sequence = sequence + 1 if response_event is not None else sequence
            event_data: dict[str, object] = {
                "turn_id": turn_id,
                "ultracode_execution_id": execution_id,
                "ultracode_decision": decision.value,
                "response": response,
                "step": 0,
                **response_contract.to_completion_metadata(),
            }
            if response_contract.source is ResponseSource.EXTERNAL_RESULT:
                event_data["external_execution"] = True
            event = AgentEvent.create(
                completion_sequence,
                AgentEventKind.TURN_COMPLETED,
                event_data,
            )
            outcome = AgentExecutionOutcome(
                AgentExecutionStatus.COMPLETED,
                None,
                finalized=False,
                recoverable=False,
            )
            record = SessionExecutionRecord(outcome, completion_sequence, event.created_at)
            await self._store.finalize_turn(
                session_id,
                event,
                result_items,
                record,
                turn_id,
                committed_response_event=response_event,
            )
            self._items = tuple(result_items)
            self._execution_record = record
            await self._reload_provider_origin()
            return await self._external_result(
                session_id,
                response,
                turn_id,
                sink=sink,
                completion_event=event,
                response_event=response_event,
                verification=verification,
                response_contract=response_contract,
            )

    async def replay_committed_turn(
        self,
        prompt: str,
        *,
        turn_id: str,
        execution_id: str,
        content_parts: Sequence[ContentPart] = (),
        verification_requirements: VerificationRequirementsSnapshot | None = None,
        verification_workspace_mutation_id: str | None = None,
        sink: EventSink | None = None,
    ) -> AgentRunResult:
        """Project an already committed parent turn without committing again.

        Recovery reuses the durable completion and its metadata.  It never
        calls a provider, creates a second attempt, or turns a missing legacy
        verification report into a successful one.
        """

        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("committed turn prompt must not be empty")
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ValueError("committed turn id must not be empty")
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise ValueError("committed execution id must not be empty")
        parts = tuple(content_parts)
        if not all(isinstance(part, ContentPart) for part in parts):
            raise TypeError("committed turn content parts must be canonical")

        async with self._turn_lock:
            session_id = self._session_id
            if session_id is None:
                raise ConfigurationError("committed turn recovery requires a persisted session")
            turn_input = TurnInput(
                prompt,
                parts,
                TurnSource.USER,
                verification_requirements=verification_requirements,
                verification_workspace_mutation_id=verification_workspace_mutation_id,
            )
            attempts = await self._store.load_turn_attempts(session_id)
            attempt = next((item for item in attempts if item.turn_id == turn_id), None)
            if attempt is None or attempt.resolution is not TurnRecoveryResolution.COMMITTED:
                raise ConfigurationError("committed turn recovery requires a committed attempt")
            if attempt.input_fingerprint != turn_input.fingerprint:
                raise ConfigurationError("committed turn identity is bound to different input")
            completion = await self._load_committed_completion(
                session_id,
                turn_id,
                execution_id,
            )
            if completion is None:
                raise ConfigurationError("committed turn result is missing its exact response")
            response = completion.get("response")
            if not isinstance(response, str) or not response.strip():
                raise ConfigurationError("committed turn result is missing its exact response")
            await self._reload_execution_record()
            return await self._external_result(
                session_id,
                response,
                turn_id,
                sink=sink,
                emit_completion=True,
                response_contract=FinalResponseContract.from_completion_metadata(
                    response,
                    completion,
                ),
            )

    async def _load_external_response(
        self,
        session_id: str,
        turn_id: str,
        execution_id: str,
    ) -> str | None:
        completion = await self._load_committed_completion(session_id, turn_id, execution_id)
        response = completion.get("response") if completion is not None else None
        return response if isinstance(response, str) and response.strip() else None

    async def _load_committed_completion(
        self,
        session_id: str,
        turn_id: str,
        execution_id: str,
    ) -> dict[str, object] | None:
        for raw_event in await self._store.load_events(session_id):
            if raw_event.get("kind") != AgentEventKind.TURN_COMPLETED.value:
                continue
            data = raw_event.get("data")
            if not isinstance(data, dict):
                continue
            if data.get("turn_id") != turn_id or data.get("ultracode_execution_id") != execution_id:
                continue
            response = data.get("response")
            if isinstance(response, str) and response.strip():
                return data
        return None

    async def _external_result(
        self,
        session_id: str,
        response: str,
        turn_id: str,
        *,
        sink: EventSink | None,
        completion_event: AgentEvent | None = None,
        response_event: AgentEvent | None = None,
        emit_completion: bool = False,
        response_contract: FinalResponseContract | None = None,
        verification: VerificationReport | None = None,
    ) -> AgentRunResult:
        if response_contract is None:
            response_contract = FinalResponseContract.committed(
                response,
                source=ResponseSource.EXTERNAL_RESULT,
            )
        elif response_contract.response != response or not response_contract.is_committed:
            raise ConfigurationError("replayed turn response contract is invalid")
        delta = response_event or AgentEvent.create(
            0, AgentEventKind.TEXT_DELTA, {"text": response}
        )
        events: list[AgentEvent] = [delta]
        if sink is not None:
            outcome = sink(delta)
            if inspect.isawaitable(outcome):
                await outcome
        if completion_event is not None:
            events.append(completion_event)
            if sink is not None:
                outcome = sink(completion_event)
                if inspect.isawaitable(outcome):
                    await outcome
        elif emit_completion and sink is not None:
            replay_event = AgentEvent.create(
                0,
                AgentEventKind.TURN_COMPLETED,
                {
                    "turn_id": turn_id,
                    "external_replay": True,
                    **response_contract.to_completion_metadata(),
                },
            )
            events.append(replay_event)
            outcome = sink(replay_event)
            if inspect.isawaitable(outcome):
                await outcome
        items = tuple(
            await SessionItemQueryService(self._store).load_session_items(
                LoadSessionItemsRequest(session_id)
            )
        )
        self._items = items
        return AgentRunResult(
            session_id,
            response,
            tuple(item for item in items if isinstance(item, Message)),
            items,
            tuple(events),
            0,
            self.plan,
            self._execution_record.outcome if self._execution_record is not None else None,
            turn_id,
            response_contract=response_contract,
            verification=verification,
        )

    async def inspect_recovery(self) -> tuple[TurnRecoveryInspection, ...]:
        """Return bounded durable recovery state without taking action."""

        if self._session_id is None:
            return ()
        async with self._turn_lock:
            return await TurnRecoveryService(self._store).inspect(self._session_id)

    async def abandon_recovery(
        self,
        turn_id: str,
        *,
        reason: str = "explicit_user_resolution",
    ) -> TurnRecoveryInspection:
        """Explicitly resolve one interrupted attempt as abandoned."""

        if self._session_id is None:
            raise ConfigurationError("recovery requires a persisted session")
        async with self._turn_lock:
            return await TurnRecoveryService(self._store).abandon(
                self._session_id,
                turn_id,
                reason=reason,
            )

    async def retry_recovery(
        self,
        turn_id: str,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult:
        """Explicitly retry an exact, pre-output user turn with a new identity."""

        if self._session_id is None:
            raise ConfigurationError("recovery requires a persisted session")
        async with self._turn_lock:
            service = TurnRecoveryService(self._store)
            handoff = await service.require_safe_retry(self._session_id, turn_id)
            if handoff.input.plan_execution_requested:
                raise ConfigurationError("explicit retry is unavailable for plan execution")
            await service.abandon(
                self._session_id,
                turn_id,
                reason="retry_requested",
            )

            async def capture_session(event: AgentEvent) -> None:
                if event.kind is AgentEventKind.SESSION_STARTED:
                    session_id = event.data.get("session_id")
                    if isinstance(session_id, str) and session_id:
                        self._session_id = session_id
                if sink is not None:
                    outcome = sink(event)
                    if inspect.isawaitable(outcome):
                        await outcome

            try:
                runtime_kwargs: dict[str, Any] = {
                    "sink": capture_session,
                    "content_parts": handoff.input.content_parts,
                    "initial_items": self._items,
                    "source_provider": self._source_provider,
                    "source_model": self._source_model,
                    "source_context_affinity": self._source_context_affinity,
                    "session_id": self._session_id,
                    # Pass the persisted value even when it is None so a
                    # current launch setting cannot alter an exact retry.
                    "verification_command": handoff.input.verification_command,
                }
                if handoff.input.verification_requirements is not None:
                    runtime_kwargs["verification_requirements"] = (
                        handoff.input.verification_requirements
                    )
                if handoff.input.verification_workspace_mutation_id is not None:
                    runtime_kwargs["verification_workspace_mutation_id"] = (
                        handoff.input.verification_workspace_mutation_id
                    )
                result = await self._runtime.run(handoff.input.prompt, **runtime_kwargs)
            except asyncio.CancelledError:
                await self._reload_persisted_state()
                raise
            except Exception:
                await self._reload_persisted_state()
                raise
            self._items = result.items
            self._session_id = result.session_id
            await self._reload_plan_state()
            await self._reload_provider_origin()
            await self._reload_execution_record()
            return result

    async def trigger_context_compaction(
        self,
        request: ContextCompactionRuntimeRequest,
    ) -> ContextCompactionRuntimeResult:
        """Run one explicit compaction request under the session turn lock.

        The immutable request is the caller-owned context snapshot.  A normal
        turn and an explicit compaction cannot overlap on this conversation;
        the trigger's source fingerprint remains the stale-snapshot guard.
        Compaction rows are separate from canonical session items, so a
        successful call does not mutate this conversation's transcript.

        在会话回合锁下运行一次显式压缩请求。

        不可变请求是调用方拥有的上下文快照。普通回合与显式压缩不能在当前会话上重叠;
        触发请求的源指纹继续负责过期快照防护。压缩记录独立于规范会话条目,因此成功调用不会修改当前会话 transcript。
        """

        async with self._turn_lock:
            self._validate_context_compaction_request(request)
            return await self._runtime.trigger_context_compaction(request)

    async def compact_now(
        self,
        *,
        boundary: ContextCompactionRuntimeBoundary | None = None,
        provider_window: ProviderContextWindow | None = None,
        protected_item_count: int = 0,
    ) -> ContextCompactionCommandResult:
        """Expose one safe, user-invoked compaction command.

        The command owns the live-context snapshot and returns only the bounded
        public projection. It never starts an ordinary model turn.
        """

        effective_boundary = boundary or ContextCompactionRuntimeBoundary(
            ContextCompactionSafePoint.BEFORE_MODEL_REQUEST,
            0,
        )
        effective_window = (
            provider_window
            if provider_window is not None
            else self._runtime.provider_context_window
        )

        async def owner(
            projection: ContextCompactionTurnProjection,
        ) -> ContextCompactionCommandResult:
            return project_context_compaction_command_result(projection)

        return await self.run_explicit_context_compaction_with_owner(
            boundary=effective_boundary,
            provider_window=effective_window,
            owner=owner,
            protected_item_count=protected_item_count,
        )

    def replace_external_tools(
        self,
        tools: Sequence[Tool],
        previous_names: Sequence[str],
    ) -> None:
        """Refresh session-owned extension tools without changing the transcript."""

        self._runtime.replace_external_tools(tools, previous_names)

    async def run_context_compaction_with_owner(
        self,
        request: ContextCompactionRuntimeRequest,
        owner: Callable[[ContextCompactionTurnProjection], Awaitable[_T]],
    ) -> _T:
        """Run compaction and its explicit owner under one session lock.

        Successful gate results are projected before the owner is called. A
        timeout is offered to the owner as its bounded terminal projection;
        cancellation, Provider, storage, and unknown failures are re-raised.
        The owner callback must perform any turn finalization itself, and this
        method never enters the normal Agent loop.

        在同一个会话锁下运行压缩及其显式所有者。

        门控成功结果会在调用所有者前先完成投影。超时会以自身的有界终态投影交给所有者;取消、Provider、存储和未知失败会重新抛出。
        所有者回调必须自行完成回合最终化,本方法不会进入普通 Agent loop。
        """

        if not callable(owner):
            raise TypeError("owner must be callable")
        async with self._turn_lock:
            return await self._run_context_compaction_with_owner_locked(request, owner)

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
    ) -> _T:
        """Build and run one explicit compaction command under the turn lock.

        The live context is built inside the existing lock, then the current
        trigger service computes the plan and stale-source guard from that
        exact snapshot.  The owner receives only the same bounded projection
        used by the lower-level request API; no normal Agent turn is started.

        在现有回合锁下构建并运行一次显式压缩命令。

        实时上下文会在现有锁内构建,随后由当前触发服务根据这份精确快照计算计划和过期源保护值。
        所有者只会收到与底层请求 API 相同的有界投影;不会启动普通 Agent 回合。
        """

        if not callable(owner):
            raise TypeError("owner must be callable")
        if self._session_id is None:
            raise ConfigurationError("explicit compaction requires a persisted session")
        async with self._turn_lock:
            context = self._runtime.build_context_snapshot(
                self._items,
                source_provider=self._source_provider,
                source_model=self._source_model,
                source_context_affinity=self._source_context_affinity,
            )
            request = self._runtime.build_explicit_context_compaction_request(
                context=context,
                boundary=boundary,
                provider_window=provider_window,
                protected_item_count=protected_item_count,
                reported_input_tokens=reported_input_tokens,
                reported_output_tokens=reported_output_tokens,
                session_id=self._session_id,
                compaction_id=compaction_id or f"compaction-{uuid.uuid4().hex}",
                created_at=created_at or datetime.now(UTC),
            )
            return await self._run_context_compaction_with_owner_locked(request, owner)

    async def _run_context_compaction_with_owner_locked(
        self,
        request: ContextCompactionRuntimeRequest,
        owner: Callable[[ContextCompactionTurnProjection], Awaitable[_T]],
    ) -> _T:
        self._validate_context_compaction_request(request)
        try:
            result = await self._runtime.trigger_context_compaction(request)
        except BaseException as error:
            projection = project_context_compaction_failure(error)
            if projection is None or projection.must_propagate:
                raise
        else:
            projection = project_context_compaction_result(result)
        if not projection.ready_for_turn_finalization:
            raise ConfigurationError(
                "compaction owner requires a triggered or controlled terminal projection"
            )
        return await owner(projection)

    async def run_background_wake(self, *, sink: EventSink | None = None) -> AgentRunResult:
        """Run one model turn for pending background completions without a user prompt.

        针对待处理的后台完成事件运行一个没有用户提示的模型回合."""

        return await self.run(
            "",
            sink=sink,
            turn_source=TurnSource.BACKGROUND_TASK_AUTO_WAKE,
        )

    async def load_background_wake_state(self) -> BackgroundWakeState:
        """Load the bounded wake ledger for the current durable session.

        加载当前持久化会话的有界唤醒账本."""

        if self._session_id is None:
            return BackgroundWakeState()
        return await self._store.load_background_wake_state(self._session_id)

    async def save_background_wake_state(self, state: BackgroundWakeState) -> None:
        """Persist the bounded wake ledger without retaining task output.

        持久化有界唤醒账本,不保留任务输出."""

        if self._session_id is None:
            return
        await self._store.save_background_wake_state(self._session_id, state)

    async def execute_plan(
        self,
        *,
        sink: EventSink | None = None,
        task_id: str | None = None,
    ) -> AgentRunResult:
        """Record an explicit user handoff from a saved plan to one agent turn.

        记录用户将已保存计划明确交给一个 Agent 回合执行."""

        if self.plan is None:
            raise ConfigurationError("cannot execute a plan that has not been saved")
        async with self._turn_lock:
            if task_id is not None:
                if self._session_id is None:
                    raise ConfigurationError("a session is required before running a plan task")
                task = await SessionTaskQueryService(self._store).get_session_task(
                    GetSessionTaskRequest(self._session_id, task_id)
                )
                if task is None:
                    raise ConfigurationError(f"unknown queued plan task: {task_id}")
                if task.kind is not SessionTaskKind.PLAN_EXECUTION:
                    raise ConfigurationError("only plan execution tasks can be started")
                if task.status is not SessionTaskStatus.QUEUED:
                    raise ConfigurationError(f"plan task {task_id} is not queued")
                if task.plan_snapshot is None:
                    raise ConfigurationError(f"plan task {task_id} has no saved plan snapshot")
                self._runtime.set_plan(task.plan_snapshot)

            async def capture_session(event: AgentEvent) -> None:
                if event.kind is AgentEventKind.SESSION_STARTED:
                    session_id = event.data.get("session_id")
                    if isinstance(session_id, str) and session_id:
                        self._session_id = session_id
                if sink is not None:
                    outcome = sink(event)
                    if inspect.isawaitable(outcome):
                        await outcome

            try:
                verification_requirements = self._resolve_new_normal_turn_requirements(
                    None,
                    turn_source=TurnSource.USER,
                    ultracode_execution_id=None,
                )
                result = await self._runtime.run(
                    PLAN_EXECUTION_PROMPT,
                    sink=capture_session,
                    plan_execution_requested=True,
                    plan_execution_task_id=task_id,
                    initial_items=self._items,
                    source_provider=self._source_provider,
                    source_model=self._source_model,
                    source_context_affinity=self._source_context_affinity,
                    session_id=self._session_id,
                    verification_requirements=verification_requirements,
                )
            except asyncio.CancelledError:
                await self._reload_persisted_state()
                raise
            except Exception:
                await self._reload_persisted_state()
                raise
            self._items = result.items
            self._session_id = result.session_id
            await self._reload_plan_state()
            await self._reload_provider_origin()
            await self._reload_execution_record()
            return result

    async def run_session_task(
        self,
        task_id: str,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult:
        """Start one queued plan task after explicit user selection.

        在用户明确选择后启动一个排队中的计划任务."""

        return await self.execute_plan(sink=sink, task_id=task_id)

    async def _reload_persisted_state(self) -> None:
        if self._session_id is None:
            return
        self._items = await SessionItemQueryService(self._store).load_session_items(
            LoadSessionItemsRequest(self._session_id)
        )
        await self._reload_plan_state()
        await self._reload_provider_origin()
        await self._reload_execution_record()

    def _validate_context_compaction_request(
        self,
        request: ContextCompactionRuntimeRequest,
    ) -> None:
        if not isinstance(request, ContextCompactionRuntimeRequest):
            raise TypeError("request must be a ContextCompactionRuntimeRequest")
        if request.trigger.mode is ContextCompactionTriggerMode.EXPLICIT:
            if self._session_id is None:
                raise ConfigurationError("explicit compaction requires a persisted session")
            if request.trigger.session_id != self._session_id:
                raise ConfigurationError("compaction request is bound to a different session")

    async def _reload_plan_state(self) -> None:
        if self._session_id is None:
            self._runtime.set_plan(None)
            return
        plan = await SessionApplicationService(self._store).load_session_plan(
            LoadSessionPlanRequest(self._session_id)
        )
        self._runtime.set_plan(plan)
        if plan is not None:
            self._runtime.set_plan_comments(
                await SessionApplicationService(self._store).list_plan_comments(
                    ListPlanCommentsRequest(self._session_id, plan)
                )
            )

    async def _reload_provider_origin(self) -> None:
        if self._session_id is None:
            return
        summary = await SessionSummaryQueryService(self._store).get_session_summary(
            GetSessionSummaryRequest(self._session_id)
        )
        self._source_provider = summary.provider
        self._source_model = summary.model
        self._source_context_affinity = summary.context_affinity

    async def _reload_execution_record(self) -> None:
        if self._session_id is None:
            self._execution_record = None
            return
        self._execution_record = await SessionExecutionQueryService(
            self._store
        ).load_execution_record(LoadExecutionRecordRequest(self._session_id))


__all__ = ["PLAN_EXECUTION_PROMPT", "AgentConversation"]
