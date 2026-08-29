"""Automatic, bounded application-level Ultracode delegation.

The router owns only a typed choice between the existing normal Agent path and
the existing bounded Agent Swarm path.  It has no tool, filesystem, process,
workspace, or provider authority of its own.

自动且有界的应用层 Ultracode 委派。

路由器只负责在既有普通 Agent 路径与既有有界 Agent Swarm 路径之间作出类型化选择,
自身不拥有工具、文件系统、进程、工作区或 Provider 权限。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, cast

from neuro_code.application.ports.result_adoption import ResultAdoptionRecord
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.ultracode import (
    ResultAdoptionFactory,
    UltracodeStore,
    UltracodeStoreError,
)
from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.application.runtime.process_liveness import owner_is_alive
from neuro_code.application.sessions.turns import RunTurnRequest
from neuro_code.domain.agent_swarm import (
    AgentSwarmResult,
    AgentSwarmRun,
    AgentSwarmRunState,
    terminal_result_fingerprint,
)
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.messages import ContentPart, SessionItem
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.conversation.request import context_fingerprints
from neuro_code.domain.execution import (
    TurnCancellationPolicy,
    TurnInput,
    TurnRecoveryAttempt,
    TurnRecoveryResolution,
    TurnRecoveryStatus,
    TurnSource,
)
from neuro_code.domain.result_adoption import (
    ResultAdoptionRequest,
    ResultAdoptionState,
)
from neuro_code.domain.task_dag import TaskDag, TaskDagState
from neuro_code.domain.ultracode import (
    MAX_ULTRACODE_RESULT_BYTES,
    UltracodeDelegationDecision,
    UltracodeExecution,
    UltracodeExecutionState,
    ultracode_execution_id,
    ultracode_result_adoption_id,
    ultracode_result_fingerprint,
    ultracode_swarm_run_id,
)
from neuro_code.shared.errors import ConfigurationError

if TYPE_CHECKING:
    from neuro_code.application.sessions.binding import ConversationBinding
    from neuro_code.application.workflows.agent_swarm import RunAgentSwarmRequest

MAX_ULTRACODE_LEASE_SECONDS = 3_600.0
MAX_ULTRACODE_CLASSIFIER_INPUT_BYTES = 16 * 1024
MAX_ULTRACODE_RESULT_EVENT_BYTES = MAX_ULTRACODE_RESULT_BYTES


def _now() -> datetime:
    return datetime.now(UTC)


def _safe_prompt(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > MAX_ULTRACODE_CLASSIFIER_INPUT_BYTES
        or any(ord(character) < 32 and character not in "\n\t\r" for character in value)
    ):
        raise ValueError("Ultracode prompt is invalid")


def _response(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > MAX_ULTRACODE_RESULT_BYTES
    ):
        raise ConfigurationError("Ultracode downstream result is outside its bounded contract")
    return value


def _adoption_failure_response(record: ResultAdoptionRecord) -> str:
    applied = sum(target.state.value == "applied" for target in record.targets)
    unresolved = len(record.targets) - applied
    conflicts = sum(target.state.value == "conflict" for target in record.targets)
    partial_possible = applied > 0 or record.state is ResultAdoptionState.INDETERMINATE
    return (
        "Automatic Ultracode result adoption did not complete.\n"
        f"adoption_id: {record.adoption_id}\n"
        f"terminal_state: {record.state.value}\n"
        f"applied_target_count: {applied}\n"
        f"unresolved_target_count: {unresolved}\n"
        f"conflict_target_count: {conflicts}\n"
        f"partial_parent_mutation_possible: {str(partial_possible).lower()}"
    )


def _adoption_progress_stage(state: ResultAdoptionState) -> str:
    return {
        ResultAdoptionState.COMPLETED: "adoption_completed",
        ResultAdoptionState.CONFLICT: "adoption_conflict",
        ResultAdoptionState.FAILED: "adoption_failed",
        ResultAdoptionState.INDETERMINATE: "adoption_indeterminate",
    }.get(state, "adoption_terminal")


def _context_fingerprint(items: Sequence[object]) -> str:
    # ``context_fingerprints`` accepts the canonical SessionItem union; the
    # narrow cast keeps the router independent from the concrete runner type.
    return context_fingerprints(cast(Sequence[Any], items)).context


@dataclass(frozen=True, slots=True)
class UltracodeDelegationPolicy:
    """Small deterministic local policy; it never calls a model classifier."""

    def decide(self, prompt: str) -> UltracodeDelegationDecision:
        _safe_prompt(prompt)
        bounded = prompt.casefold()
        parallel_markers = (
            "parallel",
            "in parallel",
            "independent tasks",
            "independent task",
            "multiple files",
            "multi-file",
            "many files",
            "cross-file",
            "cross-project",
            "cross-domain",
            "cross domain",
            "research",
            "decompose",
            "several independent",
            "并行",
            "多个文件",
            "多文件",
            "跨文件",
            "跨项目",
            "跨领域",
            "跨域",
            "研究",
            "拆分",
            "独立任务",
        )
        if any(marker in bounded for marker in parallel_markers):
            return UltracodeDelegationDecision.BOUNDED_SWARM
        return UltracodeDelegationDecision.MAIN_MAX


class UltracodeParentRunner(Protocol):
    @property
    def session_id(self) -> str | None: ...

    @property
    def reasoning_effort(self) -> ReasoningEffort: ...

    @property
    def items(self) -> tuple[SessionItem, ...]: ...

    async def ensure_persisted_session(self) -> str: ...

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


class UltracodeSwarm(Protocol):
    async def run(
        self,
        request: RunAgentSwarmRequest,
        *,
        sink: EventSink | None = None,
    ) -> AgentSwarmResult: ...

    async def close(self) -> None: ...


SwarmFactory = Callable[[], Awaitable[UltracodeSwarm]]


class UltracodeDelegationApplicationService:
    """Route one explicit Ultracode turn through exactly one existing owner."""

    def __init__(
        self,
        store: UltracodeStore,
        *,
        session_store: SessionStore,
        parent_binding: ConversationBinding,
        swarm_factory: SwarmFactory,
        result_adoption_factory: ResultAdoptionFactory | None = None,
        policy: UltracodeDelegationPolicy | None = None,
        clock: Callable[[], datetime] = _now,
        lease_seconds: float = 300.0,
        owner_id: str | None = None,
    ) -> None:
        from neuro_code.application.sessions.binding import (
            ConversationBinding as CanonicalConversationBinding,
        )

        if not isinstance(parent_binding, CanonicalConversationBinding):
            raise ConfigurationError("Ultracode parent binding is required")
        if not isinstance(store, UltracodeStore):
            raise ConfigurationError("Ultracode store is invalid")
        if not callable(getattr(session_store, "load_events", None)):
            raise ConfigurationError("Ultracode session store is invalid")
        if not callable(swarm_factory):
            raise ConfigurationError("Ultracode Swarm factory is required")
        if result_adoption_factory is not None and not callable(result_adoption_factory):
            raise ConfigurationError("Ultracode Result Adoption factory is invalid")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not 1.0 <= float(lease_seconds) <= MAX_ULTRACODE_LEASE_SECONDS
        ):
            raise ConfigurationError("Ultracode lease duration is invalid")
        if owner_id is not None and (not isinstance(owner_id, str) or not owner_id.strip()):
            raise ConfigurationError("Ultracode owner identity is invalid")
        self._store = store
        self._session_store = session_store
        self._parent_binding = parent_binding
        self._swarm_factory = swarm_factory
        self._result_adoption_factory = result_adoption_factory
        self._policy = policy or UltracodeDelegationPolicy()
        self._clock = clock
        self._lease_seconds = float(lease_seconds)
        self._owner_id = owner_id or f"ultracode-owner-{uuid.uuid4().hex}"
        self._owner_pid = os.getpid()
        self._owner_token = f"ultracode-owner-token-{uuid.uuid4().hex}"
        self._lock = asyncio.Lock()

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def parent_session_id(self) -> str | None:
        return self._parent_binding.runner.session_id

    async def run_turn(
        self,
        request: RunTurnRequest,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult:
        if not isinstance(request, RunTurnRequest):
            raise ValueError("Ultracode turn request must be canonical")
        if request.turn_source is not TurnSource.USER:
            raise ConfigurationError("Ultracode delegation is available only for user turns")
        runner = self._require_runner()
        if getattr(runner.reasoning_effort, "value", None) != "ultracode":
            raise ConfigurationError("Ultracode delegation requires effort=ultracode")
        session_id = await runner.ensure_persisted_session()
        if request.expected_session_id is not None and session_id != request.expected_session_id:
            raise ConfigurationError("Ultracode parent session identity does not match the request")
        turn_id = request.turn_id or f"ultracode-turn-{uuid.uuid4().hex}"
        turn_input = TurnInput(request.prompt, request.content_parts, TurnSource.USER)
        execution_id = ultracode_execution_id(session_id, turn_id)
        swarm_id = ultracode_swarm_run_id(execution_id)
        context_fp = _context_fingerprint(runner.items)
        async with self._lock:
            run = await self._claim_or_recover(
                execution_id=execution_id,
                parent_session_id=session_id,
                turn_id=turn_id,
                turn_input=turn_input,
                context_fingerprint=context_fp,
                decision=None,
                swarm_id=swarm_id,
            )
            if run.state is UltracodeExecutionState.COMPLETED:
                return await self._recover_completed(run, request, sink=sink)
            if run.state is UltracodeExecutionState.INDETERMINATE:
                return await self._recover_indeterminate(run, request, sink=sink)
            if (
                run.owner_id != self._owner_id
                or run.owner_pid != self._owner_pid
                or run.owner_token != self._owner_token
            ):
                raise ConfigurationError(
                    "another Ultracode controller owns this execution; explicit recovery is required"
                )
            try:
                return await self._drive(run, request, sink=sink)
            except asyncio.CancelledError:
                await asyncio.shield(self._mark_indeterminate(run))
                raise
            except Exception:
                await self._mark_indeterminate(run)
                raise

    def _require_runner(self) -> UltracodeParentRunner:
        runner = self._parent_binding.runner
        required = (
            "session_id",
            "reasoning_effort",
            "items",
            "ensure_persisted_session",
            "run",
            "commit_external_turn",
        )
        if any(not hasattr(runner, name) for name in required):
            raise ConfigurationError("Ultracode parent runner does not expose the required seam")
        return cast(UltracodeParentRunner, runner)

    async def _claim_or_recover(
        self,
        *,
        execution_id: str,
        parent_session_id: str,
        turn_id: str,
        turn_input: TurnInput,
        context_fingerprint: str,
        decision: UltracodeDelegationDecision | None,
        swarm_id: str,
    ) -> UltracodeExecution:
        existing = await self._store.get_ultracode_execution(execution_id)
        if existing is not None:
            if (
                existing.parent_session_id != parent_session_id
                or existing.parent_turn_id != turn_id
            ):
                raise ConfigurationError(
                    "Ultracode execution identity conflicts with the parent turn"
                )
            if existing.input_fingerprint != turn_input.fingerprint:
                raise ConfigurationError("Ultracode execution input identity conflicts")
            if existing.state is UltracodeExecutionState.DECIDED and (
                existing.context_fingerprint != context_fingerprint
            ):
                raise ConfigurationError("Ultracode decision context is stale")
            decision = existing.decision
            downstream_id = existing.downstream_id
            context_fingerprint = existing.context_fingerprint
            provider_name = existing.provider_name
            model_name = existing.model_name
            context_affinity = existing.context_affinity
        else:
            if decision is None:
                decision = self._policy.decide(turn_input.prompt)
            downstream_id = (
                turn_id if decision is UltracodeDelegationDecision.MAIN_MAX else swarm_id
            )
            provider = self._parent_binding.provider
            provider_name = provider.provider_name
            model_name = provider.model_name
            context_affinity = getattr(provider, "context_affinity", None)
        if decision is None:
            raise ConfigurationError("Ultracode delegation decision is missing")
        now = self._clock().astimezone(UTC)
        candidate = UltracodeExecution(
            execution_id=execution_id,
            parent_session_id=parent_session_id,
            parent_turn_id=turn_id,
            input_fingerprint=turn_input.fingerprint,
            context_fingerprint=context_fingerprint,
            decision=decision,
            downstream_id=downstream_id,
            provider_name=provider_name,
            model_name=model_name,
            context_affinity=context_affinity,
            state=UltracodeExecutionState.DECIDED,
            generation=0,
            owner_id=self._owner_id,
            owner_pid=self._owner_pid,
            owner_token=self._owner_token,
            lease_expires_at=now + timedelta(seconds=self._lease_seconds),
            created_at=now,
            updated_at=now,
        )
        try:
            claim = await self._store.claim_ultracode_execution(
                candidate,
                now=now,
                owner_is_alive=owner_is_alive,
            )
        except UltracodeStoreError as error:
            raise ConfigurationError(f"Ultracode durable claim failed: {error}") from error
        run = claim.execution
        if not run.same_identity(candidate):
            raise ConfigurationError("Ultracode durable identity conflicts with this request")
        if claim.acquired:
            return run
        if run.terminal:
            return run
        if (
            run.owner_id == self._owner_id
            and run.owner_pid == self._owner_pid
            and run.owner_token == self._owner_token
        ):
            return run
        raise ConfigurationError(
            "another Ultracode controller owns this execution; explicit recovery is required"
        )

    async def _drive(
        self,
        run: UltracodeExecution,
        request: RunTurnRequest,
        *,
        sink: EventSink | None,
    ) -> AgentRunResult:
        if run.state is UltracodeExecutionState.DECIDED:
            if run.decision is UltracodeDelegationDecision.MAIN_MAX:
                run = await self._transition(run, UltracodeExecutionState.MAIN_MAX_RUNNING)
                await self._progress(run, sink=sink)
                return await self._run_main(run, request, sink=sink)
            await self._ensure_parent_attempt(run, request)
            run = await self._transition(run, UltracodeExecutionState.BOUNDED_SWARM_RUNNING)
            await self._progress(run, sink=sink)
            attempts = await self._parent_attempts(run.parent_session_id)
            exact = next((item for item in attempts if item.turn_id == run.parent_turn_id), None)
            if exact is not None and exact.resolution is TurnRecoveryResolution.COMMITTED:
                return await self._recover_or_run_swarm(run, request, sink=sink)
            return await self._run_swarm(run, request, sink=sink)
        if run.state is UltracodeExecutionState.MAIN_MAX_RUNNING:
            return await self._recover_or_run_main(run, request, sink=sink)
        if run.state is UltracodeExecutionState.BOUNDED_SWARM_RUNNING:
            return await self._recover_or_run_swarm(run, request, sink=sink)
        if run.state is UltracodeExecutionState.FINALIZING:
            adoption = await self._load_adoption_record(run)
            return await self._finalize_swarm(
                run,
                request,
                sink=sink,
                terminal_state=self._finalizing_terminal_state(adoption),
            )
        raise ConfigurationError(f"unsupported Ultracode lifecycle state: {run.state.value}")

    async def _run_main(
        self,
        run: UltracodeExecution,
        request: RunTurnRequest,
        *,
        sink: EventSink | None,
    ) -> AgentRunResult:
        runner = self._require_runner()
        result = await runner.run(
            request.prompt,
            sink=sink,
            content_parts=request.content_parts,
            cancellation_policy=request.cancellation_policy,
            turn_source=request.turn_source,
            turn_id=run.parent_turn_id,
            ultracode_execution_id=run.execution_id,
        )
        response = _response(result.response)
        completed = await self._transition(
            run,
            UltracodeExecutionState.COMPLETED,
            final_response=response,
            final_result_fingerprint=ultracode_result_fingerprint(run.execution_id, response),
        )
        await self._progress(completed, sink=sink)
        return result

    async def _recover_or_run_main(
        self,
        run: UltracodeExecution,
        request: RunTurnRequest,
        *,
        sink: EventSink | None,
    ) -> AgentRunResult:
        attempts = await self._parent_attempts(run.parent_session_id)
        attempt = next((item for item in attempts if item.turn_id == run.parent_turn_id), None)
        if attempt is None:
            return await self._run_main(run, request, sink=sink)
        if attempt.resolution is TurnRecoveryResolution.COMMITTED:
            response = await self._require_parent_result(
                run.parent_session_id,
                run.parent_turn_id,
                run.execution_id,
            )
            result = await self._require_runner().commit_external_turn(
                request.prompt,
                response=response,
                turn_id=run.parent_turn_id,
                execution_id=run.execution_id,
                decision=run.decision,
                content_parts=request.content_parts,
                sink=sink,
            )
            completed = await self._transition(
                run,
                UltracodeExecutionState.COMPLETED,
                final_response=response,
                final_result_fingerprint=ultracode_result_fingerprint(run.execution_id, response),
            )
            await self._progress(completed, sink=sink)
            return result
        if (
            attempt.resolution is not None
            or attempt.status is not TurnRecoveryStatus.SAFELY_RETRYABLE
        ):
            raise ConfigurationError(
                "Ultracode MAIN_MAX has observable or resolved parent-turn state; replay is disabled"
            )
        # The parent attempt exists even though the downstream model request
        # has not started.  A crashed controller cannot prove this remains so
        # once it leaves this method, therefore the safe policy is to close the
        # Ultracode execution rather than fabricate a second attempt.
        raise ConfigurationError(
            "Ultracode MAIN_MAX recovery has an open parent attempt; explicit recovery is required"
        )

    async def _run_swarm(
        self,
        run: UltracodeExecution,
        request: RunTurnRequest,
        *,
        sink: EventSink | None,
    ) -> AgentRunResult:
        from neuro_code.application.workflows.agent_swarm import RunAgentSwarmRequest

        swarm = await self._swarm_factory()
        try:
            # The lower workflow is deliberately private.  Only bounded
            # Ultracode progress and the canonical parent result reach sink.
            swarm_result = await swarm.run(
                RunAgentSwarmRequest(run.downstream_id, request.prompt),
                sink=None,
            )
        finally:
            await swarm.close()
        if not isinstance(swarm_result, AgentSwarmResult):
            raise ConfigurationError("Ultracode Swarm returned a non-canonical result")
        if swarm_result.swarm_run_id != run.downstream_id:
            raise ConfigurationError("Ultracode Swarm result identity does not match the decision")
        await self._progress(run, sink=sink, stage="swarm_completed")
        adoption = await self._adopt_swarm_result(run, swarm_result, sink=sink)
        if adoption.state is ResultAdoptionState.COMPLETED:
            response = _response(swarm_result.final_response)
            terminal_state = UltracodeExecutionState.COMPLETED
        else:
            response = _adoption_failure_response(adoption)
            terminal_state = UltracodeExecutionState.INDETERMINATE
        finalized = await self._transition(
            run,
            UltracodeExecutionState.FINALIZING,
            final_response=response,
            final_result_fingerprint=ultracode_result_fingerprint(run.execution_id, response),
        )
        return await self._finalize_swarm(
            finalized,
            request,
            sink=sink,
            terminal_state=terminal_state,
        )

    async def _recover_or_run_swarm(
        self,
        run: UltracodeExecution,
        request: RunTurnRequest,
        *,
        sink: EventSink | None,
    ) -> AgentRunResult:
        """Reuse an observable parent result before starting another Swarm.

        The parent turn is the externally visible result boundary.  A crash
        can therefore leave the lower Swarm terminal while the Ultracode
        projection still says ``BOUNDED_SWARM_RUNNING``.  Once that exact
        result is observable, starting the Swarm again would duplicate work;
        promote the projection to ``FINALIZING`` and complete the normal
        idempotent parent commit instead.
        """

        attempts = await self._parent_attempts(run.parent_session_id)
        attempt = next((item for item in attempts if item.turn_id == run.parent_turn_id), None)
        if attempt is None:
            raise ConfigurationError(
                "Ultracode BOUNDED_SWARM has no exact parent attempt; replay is disabled"
            )
        if attempt.resolution is TurnRecoveryResolution.COMMITTED:
            response = await self._require_parent_result(
                run.parent_session_id,
                run.parent_turn_id,
                run.execution_id,
            )
            adoption = await self._load_adoption_record(run)
            terminal_state = self._finalizing_terminal_state(adoption)
            finalized = await self._transition(
                run,
                UltracodeExecutionState.FINALIZING,
                final_response=response,
                final_result_fingerprint=ultracode_result_fingerprint(run.execution_id, response),
            )
            return await self._finalize_swarm(
                finalized,
                request,
                sink=sink,
                terminal_state=terminal_state,
            )
        lower = await self._load_completed_swarm_response(run.downstream_id, run.parent_session_id)
        if lower is not None:
            await self._progress(run, sink=sink, stage="swarm_completed")
            adoption = await self._load_adoption_record(run)
            if adoption is None or not adoption.state.terminal:
                adoption = await self._adopt_swarm_result(run, lower, sink=sink)
            else:
                await self._progress_adoption(run, adoption, sink=sink)
            if adoption.state is ResultAdoptionState.COMPLETED:
                response = _response(lower.final_response)
                terminal_state = UltracodeExecutionState.COMPLETED
            else:
                response = _adoption_failure_response(adoption)
                terminal_state = UltracodeExecutionState.INDETERMINATE
            finalized = await self._transition(
                run,
                UltracodeExecutionState.FINALIZING,
                final_response=response,
                final_result_fingerprint=ultracode_result_fingerprint(run.execution_id, response),
            )
            return await self._finalize_swarm(
                finalized,
                request,
                sink=sink,
                terminal_state=terminal_state,
            )
        if (
            attempt.resolution is None
            and attempt.status is TurnRecoveryStatus.SAFELY_RETRYABLE
            and await self._has_recoverable_swarm(run.downstream_id)
        ):
            # The lower workflow has already published its exact durable
            # identity.  Continuing that existing Swarm is safe; generating
            # a new run would violate the one-branch identity contract.
            return await self._run_swarm(run, request, sink=sink)
        raise ConfigurationError(
            "Ultracode BOUNDED_SWARM has observable or unresolved parent state; replay is disabled"
        )

    async def _adopt_swarm_result(
        self,
        run: UltracodeExecution,
        swarm_result: AgentSwarmResult,
        *,
        sink: EventSink | None,
    ) -> ResultAdoptionRecord:
        if self._result_adoption_factory is None:
            raise ConfigurationError("Ultracode Result Adoption factory is required")
        adoption_id = ultracode_result_adoption_id(run.execution_id, swarm_result.swarm_run_id)
        request = ResultAdoptionRequest(adoption_id, swarm_result.swarm_run_id)
        adoption = await self._result_adoption_factory()
        await self._progress(
            run,
            sink=sink,
            stage="adoption_preparing",
            adoption_id=adoption_id,
        )
        record = await adoption.get_result_adoption(adoption_id)
        if record is not None:
            self._validate_adoption_record(run, swarm_result, record)
        if record is None or not record.state.terminal:
            await self._progress(
                run,
                sink=sink,
                stage="adoption_applying",
                adoption_id=adoption_id,
                adoption_state=(record.state.value if record is not None else None),
                record=record,
            )
            record = await adoption.adopt(request, swarm_result=swarm_result)
        self._validate_adoption_record(run, swarm_result, record)
        if not record.state.terminal:
            raise ConfigurationError("Ultracode Result Adoption did not reach a terminal state")
        await self._progress_adoption(run, record, sink=sink)
        return record

    async def _load_adoption_record(
        self,
        run: UltracodeExecution,
    ) -> ResultAdoptionRecord | None:
        if self._result_adoption_factory is None:
            raise ConfigurationError("Ultracode Result Adoption factory is required")
        adoption_id = ultracode_result_adoption_id(run.execution_id, run.downstream_id)
        adoption = await self._result_adoption_factory()
        record = await adoption.get_result_adoption(adoption_id)
        if record is not None:
            self._validate_adoption_record(run, None, record)
        return record

    @staticmethod
    def _validate_adoption_record(
        run: UltracodeExecution,
        swarm_result: AgentSwarmResult | None,
        record: ResultAdoptionRecord,
    ) -> None:
        if not isinstance(record, ResultAdoptionRecord):
            raise ConfigurationError("Ultracode Result Adoption returned a non-canonical record")
        expected_id = ultracode_result_adoption_id(run.execution_id, run.downstream_id)
        if (
            record.adoption_id != expected_id
            or record.plan.parent_session_id != run.parent_session_id
            or record.plan.swarm_run_id != run.downstream_id
        ):
            raise ConfigurationError("Ultracode Result Adoption identity does not match the run")
        if swarm_result is not None and (
            record.plan.dag_id != swarm_result.dag.dag_id
            or record.plan.dag_generation != swarm_result.dag.generation
            or record.plan.dag_definition_fingerprint != swarm_result.dag.definition_fingerprint
        ):
            raise ConfigurationError("Ultracode Result Adoption DAG identity does not match")

    async def _progress_adoption(
        self,
        run: UltracodeExecution,
        record: ResultAdoptionRecord,
        *,
        sink: EventSink | None,
    ) -> None:
        await self._progress(
            run,
            sink=sink,
            stage=_adoption_progress_stage(record.state),
            adoption_id=record.adoption_id,
            adoption_state=record.state.value,
            record=record,
        )

    async def _load_completed_swarm_response(
        self,
        swarm_run_id: str,
        parent_session_id: str,
    ) -> AgentSwarmResult | None:
        get_swarm_run = getattr(self._session_store, "get_swarm_run", None)
        get_task_dag = getattr(self._session_store, "get_task_dag", None)
        if not callable(get_swarm_run) or not callable(get_task_dag):
            return None
        try:
            run = await get_swarm_run(swarm_run_id)
            if run is None:
                return None
            if (
                not isinstance(run, AgentSwarmRun)
                or run.state is not AgentSwarmRunState.COMPLETED
                or run.parent_session_id != parent_session_id
                or run.current_dag_id is None
                or run.current_dag_generation is None
                or run.current_dag_definition_fingerprint is None
                or run.final_response is None
                or run.final_result_fingerprint is None
            ):
                raise ConfigurationError("completed Swarm identity is incomplete")
            dag = await get_task_dag(run.current_dag_id)
            if not isinstance(dag, TaskDag) or dag.state is not TaskDagState.COMPLETED:
                raise ConfigurationError("completed Swarm DAG identity is incomplete")
            if (
                dag.parent_session_id != parent_session_id
                or dag.generation != run.current_dag_generation
                or dag.definition_fingerprint != run.current_dag_definition_fingerprint
                or run.final_result_fingerprint
                != terminal_result_fingerprint(
                    run.swarm_run_id,
                    dag.dag_id,
                    dag.generation,
                    dag.definition_fingerprint,
                    run.final_response,
                )
            ):
                raise ConfigurationError("completed Swarm result integrity verification failed")
            return AgentSwarmResult(run, dag)
        except ConfigurationError:
            raise
        except Exception as error:
            raise ConfigurationError("completed Swarm result could not be recovered") from error

    @staticmethod
    def _finalizing_terminal_state(
        record: ResultAdoptionRecord | None,
    ) -> UltracodeExecutionState:
        if record is None or record.state is ResultAdoptionState.COMPLETED:
            return UltracodeExecutionState.COMPLETED
        if record.state.terminal:
            return UltracodeExecutionState.INDETERMINATE
        raise ConfigurationError("Ultracode Result Adoption is not terminal during finalization")

    async def _has_recoverable_swarm(self, swarm_run_id: str) -> bool:
        getter = getattr(self._session_store, "get_swarm_run", None)
        if not callable(getter):
            return False
        try:
            lower_run = await getter(swarm_run_id)
        except Exception as error:
            raise ConfigurationError("Ultracode Swarm identity could not be verified") from error
        return lower_run is not None and lower_run.state not in {
            AgentSwarmRunState.FAILED,
            AgentSwarmRunState.INDETERMINATE,
        }

    async def _finalize_swarm(
        self,
        run: UltracodeExecution,
        request: RunTurnRequest,
        *,
        sink: EventSink | None,
        terminal_state: UltracodeExecutionState = UltracodeExecutionState.COMPLETED,
    ) -> AgentRunResult:
        if terminal_state not in {
            UltracodeExecutionState.COMPLETED,
            UltracodeExecutionState.INDETERMINATE,
        }:
            raise ConfigurationError("invalid Ultracode terminal state")
        response = _response(run.final_response or "")
        result = await self._require_runner().commit_external_turn(
            request.prompt,
            response=response,
            turn_id=run.parent_turn_id,
            execution_id=run.execution_id,
            decision=run.decision,
            content_parts=request.content_parts,
            sink=sink,
        )
        completed = await self._transition(run, terminal_state)
        await self._progress(completed, sink=sink)
        return result

    async def _recover_completed(
        self,
        run: UltracodeExecution,
        request: RunTurnRequest,
        *,
        sink: EventSink | None,
    ) -> AgentRunResult:
        response = _response(run.final_response or "")
        return await self._require_runner().commit_external_turn(
            request.prompt,
            response=response,
            turn_id=run.parent_turn_id,
            execution_id=run.execution_id,
            decision=run.decision,
            content_parts=request.content_parts,
            sink=sink,
        )

    async def _recover_indeterminate(
        self,
        run: UltracodeExecution,
        request: RunTurnRequest,
        *,
        sink: EventSink | None,
    ) -> AgentRunResult:
        attempts = await self._parent_attempts(run.parent_session_id)
        attempt = next((item for item in attempts if item.turn_id == run.parent_turn_id), None)
        if attempt is None or attempt.resolution is not TurnRecoveryResolution.COMMITTED:
            raise ConfigurationError(
                "Ultracode execution is indeterminate; automatic replay is disabled"
            )
        response = await self._require_parent_result(
            run.parent_session_id,
            run.parent_turn_id,
            run.execution_id,
        )
        return await self._require_runner().commit_external_turn(
            request.prompt,
            response=response,
            turn_id=run.parent_turn_id,
            execution_id=run.execution_id,
            decision=run.decision,
            content_parts=request.content_parts,
            sink=sink,
        )

    async def _ensure_parent_attempt(
        self,
        run: UltracodeExecution,
        request: RunTurnRequest,
    ) -> None:
        attempts = await self._parent_attempts(run.parent_session_id)
        exact = next((item for item in attempts if item.turn_id == run.parent_turn_id), None)
        turn_input = TurnInput(request.prompt, request.content_parts, TurnSource.USER)
        if exact is not None:
            if exact.input_fingerprint != turn_input.fingerprint:
                raise ConfigurationError("Ultracode parent attempt input identity conflicts")
            if exact.resolution is not None:
                if exact.resolution is TurnRecoveryResolution.COMMITTED:
                    return
                raise ConfigurationError("Ultracode parent attempt is already resolved")
            if exact.status is not TurnRecoveryStatus.SAFELY_RETRYABLE:
                raise ConfigurationError("Ultracode parent attempt is indeterminate")
            return
        if any(item.resolution is None for item in attempts):
            raise ConfigurationError("Ultracode parent session has another unresolved turn")
        await self._session_store.start_turn_attempt(
            TurnRecoveryAttempt.create(
                turn_id=run.parent_turn_id,
                session_id=run.parent_session_id,
                input=turn_input,
                accepted_at=self._clock().astimezone(UTC),
            )
        )

    async def _parent_attempts(self, session_id: str) -> list[TurnRecoveryAttempt]:
        return await self._session_store.load_turn_attempts(session_id)

    async def _require_parent_result(
        self,
        session_id: str,
        turn_id: str,
        execution_id: str,
    ) -> str:
        for event in await self._session_store.load_events(session_id):
            if event.get("kind") != AgentEventKind.TURN_COMPLETED.value:
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            if (
                data.get("turn_id") == turn_id
                and data.get("ultracode_execution_id") == execution_id
            ):
                response = data.get("response")
                if isinstance(response, str) and response.strip():
                    return _response(response)
        raise ConfigurationError("Ultracode committed parent result is missing its exact response")

    async def _progress(
        self,
        run: UltracodeExecution,
        *,
        sink: EventSink | None,
        stage: str | None = None,
        adoption_id: str | None = None,
        adoption_state: str | None = None,
        record: ResultAdoptionRecord | None = None,
    ) -> None:
        data: dict[str, object] = {
            "ultracode_execution_id": run.execution_id,
            "decision": run.decision.value,
            "state": run.state.value,
            "downstream_id": run.downstream_id,
        }
        if stage is not None:
            data["stage"] = stage
        if adoption_id is not None:
            data["adoption_id"] = adoption_id
        if adoption_state is not None:
            data["adoption_state"] = adoption_state
        if record is not None:
            applied = sum(target.state.value == "applied" for target in record.targets)
            data.update(
                {
                    "applied_target_count": applied,
                    "unresolved_target_count": len(record.targets) - applied,
                    "conflict_target_count": sum(
                        target.state.value == "conflict" for target in record.targets
                    ),
                }
            )
        event = AgentEvent.create(
            await self._session_store.next_event_sequence(run.parent_session_id),
            AgentEventKind.ULTRACODE_DELEGATION_PROGRESS,
            data,
        )
        await self._session_store.append_event(run.parent_session_id, event)
        if sink is not None:
            outcome = sink(event)
            if inspect.isawaitable(outcome):
                await outcome

    async def _transition(
        self,
        run: UltracodeExecution,
        state: UltracodeExecutionState,
        **changes: object,
    ) -> UltracodeExecution:
        if not run.state.can_transition_to(state):
            raise ConfigurationError(
                f"invalid Ultracode lifecycle transition {run.state.value} -> {state.value}"
            )
        now = self._clock().astimezone(UTC)
        proposed = replace(
            run,
            **cast(dict[str, Any], changes),
            state=state,
            generation=run.generation + 1,
            owner_id=self._owner_id,
            owner_pid=self._owner_pid,
            owner_token=self._owner_token,
            lease_expires_at=now + timedelta(seconds=self._lease_seconds),
            updated_at=now,
        )
        try:
            return await self._store.compare_and_transition_ultracode_execution(
                proposed,
                expected_generation=run.generation,
                expected_state=run.state,
            )
        except UltracodeStoreError as error:
            raise ConfigurationError(
                "Ultracode lifecycle fence was lost; automatic replay is disabled"
            ) from error

    async def _mark_indeterminate(self, run: UltracodeExecution) -> None:
        try:
            current = await self._store.get_ultracode_execution(run.execution_id)
            if current is None or current.terminal:
                return
            if (
                current.owner_id != self._owner_id
                or current.owner_pid != self._owner_pid
                or current.owner_token != self._owner_token
            ):
                return
            await self._transition(current, UltracodeExecutionState.INDETERMINATE)
        except Exception:
            # The original failure remains the observable error.  Losing the
            # fence is itself fail-closed because no automatic replay follows.
            return


__all__ = [
    "MAX_ULTRACODE_CLASSIFIER_INPUT_BYTES",
    "MAX_ULTRACODE_LEASE_SECONDS",
    "MAX_ULTRACODE_RESULT_EVENT_BYTES",
    "RunTurnRequest",
    "UltracodeDelegationApplicationService",
    "UltracodeDelegationPolicy",
    "UltracodeParentRunner",
    "UltracodeSwarm",
]
