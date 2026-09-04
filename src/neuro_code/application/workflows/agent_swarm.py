"""Bounded internal Agent Swarm orchestration.

This layer owns only the durable identity and lifecycle of one orchestration
run.  Existing Planner, Leader, Task DAG, Writable Subagent, Relay, Worktree,
Checkpoint, LSP, and Replan services remain the authorities for their own
operations.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from neuro_code.application.ports.agent_swarm import (
    AgentSwarmStore,
    AgentSwarmStoreError,
)
from neuro_code.application.ports.task_dag import TaskDagStore
from neuro_code.application.runtime.agent import EventSink
from neuro_code.application.runtime.process_liveness import owner_is_alive
from neuro_code.application.workflows.leader import (
    LeaderRunResult,
    RunLeaderRequest,
)
from neuro_code.application.workflows.model_planning import (
    ModelDagPlanningResult,
    RunModelDagPlanningRequest,
)
from neuro_code.application.workflows.task_dag_replan import (
    RunTaskDagReplanRequest,
    TaskDagReplanResult,
)
from neuro_code.domain.agent_swarm import (
    MAX_SWARM_OBJECTIVE_BYTES,
    MAX_SWARM_RESULT_BYTES,
    AgentSwarmResult,
    AgentSwarmRun,
    AgentSwarmRunState,
    objective_fingerprint,
    terminal_result_fingerprint,
)
from neuro_code.domain.model_planning import PlanningAttemptState
from neuro_code.domain.task_dag import TaskDag, TaskDagNodeState, TaskDagState
from neuro_code.domain.task_dag_replan import MAX_DAG_REPLAN_DEPTH, DagReplanAttemptState
from neuro_code.shared.errors import ConfigurationError
from neuro_code.shared.redaction import redact_sensitive_text

if TYPE_CHECKING:
    from neuro_code.application.sessions.binding import ConversationBinding

MAX_SWARM_LEASE_SECONDS = 3_600.0


def _now() -> datetime:
    return datetime.now(UTC)


def _validate_objective(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > MAX_SWARM_OBJECTIVE_BYTES
        or any(ord(character) < 32 and character not in "\n\t\r" for character in value)
    ):
        raise ValueError("swarm objective is invalid")


def _validate_run_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("swarm run id is invalid")


def _result_fingerprint(run_id: str, dag: TaskDag, response: str) -> str:
    return terminal_result_fingerprint(
        run_id,
        dag.dag_id,
        dag.generation,
        dag.definition_fingerprint,
        response,
    )


@dataclass(frozen=True, slots=True)
class RunAgentSwarmRequest:
    """Explicit durable identity and objective for one bounded Swarm run."""

    swarm_run_id: str
    objective: str

    def __post_init__(self) -> None:
        _validate_run_id(self.swarm_run_id)
        _validate_objective(self.objective)


@runtime_checkable
class AgentSwarmPlanner(Protocol):
    @property
    def planning_session_id(self) -> str: ...

    async def run(
        self,
        request: RunModelDagPlanningRequest,
        *,
        sink: EventSink | None = None,
    ) -> ModelDagPlanningResult: ...

    async def close(self) -> None: ...


@runtime_checkable
class AgentSwarmLeader(Protocol):
    async def run(
        self,
        request: RunLeaderRequest,
        *,
        sink: EventSink | None = None,
    ) -> LeaderRunResult: ...

    async def close(self) -> None: ...


@runtime_checkable
class AgentSwarmReplanner(Protocol):
    async def run(
        self,
        request: RunTaskDagReplanRequest,
        *,
        sink: EventSink | None = None,
    ) -> TaskDagReplanResult: ...

    async def close(self) -> None: ...


PlannerFactory = Callable[[], Awaitable[AgentSwarmPlanner]]
LeaderFactory = Callable[[], Awaitable[AgentSwarmLeader]]
ReplannerFactory = Callable[[], Awaitable[AgentSwarmReplanner]]


class BoundedAgentSwarmApplicationService:
    """Drive one durable Planner → Leader → worker DAG → Replan run."""

    def __init__(
        self,
        store: AgentSwarmStore,
        dag_store: TaskDagStore,
        *,
        parent_binding: ConversationBinding,
        planner_factory: PlannerFactory,
        leader_factory: LeaderFactory,
        replanner_factory: ReplannerFactory,
        clock: Callable[[], datetime] = _now,
        lease_seconds: float = 300.0,
        redaction_values: tuple[str, ...] = (),
        owner_id: str | None = None,
    ) -> None:
        from neuro_code.application.sessions.binding import (
            ConversationBinding as CanonicalConversationBinding,
        )

        if not isinstance(parent_binding, CanonicalConversationBinding):
            raise ConfigurationError("Swarm parent binding is required")
        if (
            not callable(planner_factory)
            or not callable(leader_factory)
            or not callable(replanner_factory)
        ):
            raise ConfigurationError("Swarm lower-layer factories are required")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not 1.0 <= float(lease_seconds) <= MAX_SWARM_LEASE_SECONDS
        ):
            raise ConfigurationError("Swarm lease duration is invalid")
        parent_session_id = parent_binding.runner.session_id
        if not isinstance(parent_session_id, str) or not parent_session_id.strip():
            raise ConfigurationError("Swarm parent session identity is missing")
        if owner_id is not None and (not isinstance(owner_id, str) or not owner_id.strip()):
            raise ConfigurationError("Swarm owner identity is invalid")
        if not isinstance(store, AgentSwarmStore):
            raise ConfigurationError("Swarm store is invalid")
        if not callable(getattr(dag_store, "get_task_dag", None)):
            raise ConfigurationError("Swarm Task DAG store is invalid")
        self._store = store
        self._dag_store = dag_store
        self._parent_binding = parent_binding
        self._parent_session_id = parent_session_id
        self._planner_factory = planner_factory
        self._leader_factory = leader_factory
        self._replanner_factory = replanner_factory
        self._clock = clock
        self._lease_seconds = float(lease_seconds)
        self._redaction_values = tuple(redaction_values)
        self._owner_id = owner_id or f"swarm-owner-{uuid.uuid4().hex}"
        self._owner_pid = os.getpid()
        self._owner_token = f"swarm-owner-token-{uuid.uuid4().hex}"
        self._lock = asyncio.Lock()

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def parent_session_id(self) -> str:
        return self._parent_session_id

    async def close(self) -> None:
        """The Swarm creates lower services per phase and closes them eagerly."""

    async def run(
        self,
        request: RunAgentSwarmRequest,
        *,
        sink: EventSink | None = None,
    ) -> AgentSwarmResult:
        if not isinstance(request, RunAgentSwarmRequest):
            raise ValueError("Swarm run request must be canonical")
        async with self._lock:
            owned = False
            run: AgentSwarmRun | None = None
            try:
                run, owned = await self._claim(request)
                if run.state is AgentSwarmRunState.COMPLETED:
                    return await self._result_from_run(run)
                if run.state in {
                    AgentSwarmRunState.FAILED,
                    AgentSwarmRunState.INDETERMINATE,
                }:
                    raise ConfigurationError(
                        f"Swarm run is terminally {run.state.value}; automatic replay is disabled"
                    )
                if not owned:
                    raise ConfigurationError(
                        "another Agent Swarm controller owns this run; explicit recovery is required"
                    )
                return await self._drive(run, request.objective, sink=sink)
            except asyncio.CancelledError:
                if owned and run is not None:
                    await asyncio.shield(self._mark_indeterminate(run))
                raise
            except Exception:
                if owned and run is not None:
                    await self._mark_indeterminate(run)
                raise

    async def _claim(self, request: RunAgentSwarmRequest) -> tuple[AgentSwarmRun, bool]:
        now = self._clock().astimezone(UTC)
        planning_id = f"swarm-planning-{request.swarm_run_id}"
        candidate = AgentSwarmRun(
            swarm_run_id=request.swarm_run_id,
            parent_session_id=self._parent_session_id,
            objective_fingerprint=objective_fingerprint(
                redact_sensitive_text(
                    request.objective,
                    explicit_values=self._redaction_values,
                )
            ),
            planning_id=planning_id,
            state=AgentSwarmRunState.CLAIMED,
            generation=0,
            owner_id=self._owner_id,
            owner_pid=self._owner_pid,
            owner_token=self._owner_token,
            lease_expires_at=now + timedelta(seconds=self._lease_seconds),
            created_at=now,
            updated_at=now,
        )
        try:
            claim = await self._store.claim_swarm_run(
                candidate,
                now=now,
                owner_is_alive=owner_is_alive,
            )
        except AgentSwarmStoreError as error:
            raise ConfigurationError(f"Swarm durable claim failed: {error}") from error
        run = claim.run
        if not run.same_identity(candidate):
            raise ConfigurationError("Swarm durable identity conflicts with this request")
        if claim.acquired:
            return run, True
        if run.state is AgentSwarmRunState.COMPLETED:
            return run, False
        if (
            run.owner_id == self._owner_id
            and run.owner_pid == self._owner_pid
            and run.owner_token == self._owner_token
        ):
            return run, True
        if owner_is_alive(run.owner_pid):
            return run, False
        raise ConfigurationError(
            "Swarm run owner is not durably reclaimable; explicit recovery is required"
        )

    async def _drive(
        self,
        run: AgentSwarmRun,
        objective: str,
        *,
        sink: EventSink | None,
    ) -> AgentSwarmResult:
        while True:
            if run.state is AgentSwarmRunState.CLAIMED:
                run = await self._transition(run, AgentSwarmRunState.PLANNING)
                continue
            if run.state is AgentSwarmRunState.PLANNING:
                run = await self._plan(run, objective, sink=sink)
                continue
            if run.state is AgentSwarmRunState.PLANNED:
                run = await self._transition(run, AgentSwarmRunState.EXECUTING)
                continue
            if run.state is AgentSwarmRunState.EXECUTING:
                run = await self._execute_dag(run, objective, sink=sink)
                if run.state is AgentSwarmRunState.FINALIZING:
                    continue
                if run.state is AgentSwarmRunState.REPLANNING:
                    continue
                if run.state is AgentSwarmRunState.FAILED:
                    raise ConfigurationError("bounded Swarm successor DAG failed")
                if run.state is AgentSwarmRunState.INDETERMINATE:
                    raise ConfigurationError("Swarm execution is indeterminate")
                continue
            if run.state is AgentSwarmRunState.REPLANNING:
                run = await self._replan(run, sink=sink)
                continue
            if run.state is AgentSwarmRunState.FINALIZING:
                run = await self._finalize(run)
                continue
            if run.state is AgentSwarmRunState.COMPLETED:
                return await self._result_from_run(run)
            raise ConfigurationError(f"unsupported Swarm lifecycle state: {run.state.value}")

    async def _plan(
        self,
        run: AgentSwarmRun,
        objective: str,
        *,
        sink: EventSink | None,
    ) -> AgentSwarmRun:
        planner = await self._planner_factory()
        if not isinstance(planner, AgentSwarmPlanner):
            raise ConfigurationError("Swarm planner factory returned an invalid service")
        try:
            planned = await planner.run(
                RunModelDagPlanningRequest(run.planning_id, objective),
                sink=sink,
            )
        finally:
            await planner.close()
        self._verify_planning(run, planned)
        dag = planned.dag
        return await self._transition(
            run,
            AgentSwarmRunState.PLANNED,
            planner_session_id=planned.attempt.planner_session_id,
            planner_turn_id=planned.attempt.planner_turn_id,
            proposal_fingerprint=planned.proposal.proposal_fingerprint,
            root_dag_id=dag.dag_id,
            current_dag_id=dag.dag_id,
            current_dag_generation=dag.generation,
            current_dag_definition_fingerprint=dag.definition_fingerprint,
        )

    def _verify_planning(self, run: AgentSwarmRun, planned: ModelDagPlanningResult) -> None:
        if not isinstance(planned, ModelDagPlanningResult):
            raise ConfigurationError("Swarm planner returned a non-canonical result")
        if (
            planned.planning_id != run.planning_id
            or planned.attempt.planning_id != run.planning_id
            or planned.attempt.parent_session_id != self._parent_session_id
            or planned.attempt.objective_fingerprint != run.objective_fingerprint
            or planned.proposal.planning_id != run.planning_id
            or planned.proposal.parent_session_id != self._parent_session_id
            or planned.proposal.objective_fingerprint != run.objective_fingerprint
            or planned.proposal.intended_dag_id != planned.dag.dag_id
            or planned.attempt.intended_dag_id != planned.dag.dag_id
            or planned.dag.parent_session_id != self._parent_session_id
            or planned.proposal.proposal_fingerprint != planned.attempt.proposal_fingerprint
            or planned.attempt.state is not PlanningAttemptState.COMPLETED
        ):
            raise ConfigurationError("Swarm planner result identity is inconsistent")
        if planned.dag.state is not TaskDagState.READY:
            raise ConfigurationError("Swarm planner did not publish a fresh READY DAG")
        if run.root_dag_id is not None and run.root_dag_id != planned.dag.dag_id:
            raise ConfigurationError("Swarm root DAG identity changed during recovery")

    async def _execute_dag(
        self,
        run: AgentSwarmRun,
        objective: str,
        *,
        sink: EventSink | None,
    ) -> AgentSwarmRun:
        dag = await self._load_current_dag(run)
        leader = await self._leader_factory()
        if not isinstance(leader, AgentSwarmLeader):
            raise ConfigurationError("Swarm Leader factory returned an invalid service")
        try:
            result = await leader.run(
                RunLeaderRequest(dag.dag_id, objective),
                sink=sink,
            )
        finally:
            await leader.close()
        self._verify_leader_result(run, result)
        current = result.dag
        if current.state is TaskDagState.COMPLETED:
            response = result.final_response
            if response is None or not response.strip():
                raise ConfigurationError("completed Swarm DAG has no Leader final response")
            response = redact_sensitive_text(
                response,
                explicit_values=self._redaction_values,
            )
            if len(response.encode("utf-8")) > MAX_SWARM_RESULT_BYTES:
                raise ConfigurationError("Swarm final response exceeds its bounded result contract")
            return await self._transition(
                run,
                AgentSwarmRunState.FINALIZING,
                current_dag_id=current.dag_id,
                current_dag_generation=current.generation,
                current_dag_definition_fingerprint=current.definition_fingerprint,
                final_response=response,
                final_result_fingerprint=_result_fingerprint(run.swarm_run_id, current, response),
            )
        if current.state is TaskDagState.FAILED:
            if run.replan_revision_id is not None:
                return await self._transition(run, AgentSwarmRunState.FAILED)
            return await self._transition(
                run,
                AgentSwarmRunState.REPLANNING,
                current_dag_id=current.dag_id,
                current_dag_generation=current.generation,
                current_dag_definition_fingerprint=current.definition_fingerprint,
                replan_revision_id=f"swarm-replan-{run.swarm_run_id}",
            )
        raise ConfigurationError(
            "Swarm Leader returned a non-terminal or uncertain DAG; automatic retry is disabled"
        )

    def _verify_leader_result(self, run: AgentSwarmRun, result: LeaderRunResult) -> None:
        if not isinstance(result, LeaderRunResult):
            raise ConfigurationError("Swarm Leader returned a non-canonical result")
        dag = result.dag
        if (
            dag.parent_session_id != self._parent_session_id
            or run.current_dag_id != dag.dag_id
            or run.current_dag_generation is None
            or run.current_dag_definition_fingerprint is None
            or dag.generation < run.current_dag_generation
            or dag.definition_fingerprint != run.current_dag_definition_fingerprint
        ):
            raise ConfigurationError("Swarm Leader returned a DAG outside the exact lineage")
        if dag.state is TaskDagState.INDETERMINATE or dag.state is TaskDagState.CANCELLED:
            raise ConfigurationError("Swarm DAG is uncertain; it cannot be retried or replanned")

    async def _replan(
        self,
        run: AgentSwarmRun,
        *,
        sink: EventSink | None,
    ) -> AgentSwarmRun:
        if run.current_dag_id is None or run.replan_revision_id is None:
            raise ConfigurationError("Swarm replan identity is incomplete")
        source = await self._load_current_dag(run)
        if source.state is not TaskDagState.FAILED or source.running_node_ids:
            raise ConfigurationError("Swarm replan requires a quiescent FAILED source DAG")
        if any(node.state is TaskDagNodeState.INDETERMINATE for node in source.nodes):
            raise ConfigurationError("Swarm never replans an indeterminate source DAG")
        replanner = await self._replanner_factory()
        if not isinstance(replanner, AgentSwarmReplanner):
            raise ConfigurationError("Swarm Replan factory returned an invalid service")
        try:
            revised = await replanner.run(
                RunTaskDagReplanRequest(run.replan_revision_id, source.dag_id),
                sink=sink,
            )
        finally:
            await replanner.close()
        source_after = await self._load_current_dag(run)
        if source_after != source:
            raise ConfigurationError("Swarm Replan mutated the immutable source DAG")
        self._verify_replan(run, source, revised)
        successor = revised.successor_dag
        return await self._transition(
            run,
            AgentSwarmRunState.EXECUTING,
            current_dag_id=successor.dag_id,
            current_dag_generation=successor.generation,
            current_dag_definition_fingerprint=successor.definition_fingerprint,
            successor_dag_id=successor.dag_id,
        )

    def _verify_replan(
        self,
        run: AgentSwarmRun,
        source: TaskDag,
        revised: TaskDagReplanResult,
    ) -> None:
        if not isinstance(revised, TaskDagReplanResult):
            raise ConfigurationError("Swarm Replan returned a non-canonical result")
        if (
            revised.revision_id != run.replan_revision_id
            or revised.attempt.parent_session_id != self._parent_session_id
            or revised.attempt.source_dag_id != source.dag_id
            or revised.attempt.source_definition_fingerprint != source.definition_fingerprint
            or revised.attempt.source_generation != source.generation
            or revised.attempt.source_state is not TaskDagState.FAILED
            or revised.attempt.revision_depth != MAX_DAG_REPLAN_DEPTH
            or revised.attempt.state is not DagReplanAttemptState.COMPLETED
            or revised.successor_dag.parent_session_id != self._parent_session_id
            or revised.successor_dag.dag_id == source.dag_id
            or revised.successor_dag.dag_id != revised.attempt.successor_dag_id
            or revised.proposal.intended_successor_dag_id != revised.successor_dag.dag_id
            or revised.proposal.source_dag_id != source.dag_id
            or revised.proposal.source_definition_fingerprint != source.definition_fingerprint
            or revised.proposal.source_generation != source.generation
            or revised.proposal.evidence_fingerprint != revised.evidence.fingerprint
            or revised.attempt.evidence_fingerprint != revised.evidence.fingerprint
            or revised.proposal.proposal_fingerprint != revised.attempt.proposal_fingerprint
            or revised.successor_dag.state is not TaskDagState.READY
        ):
            raise ConfigurationError("Swarm Replan result does not preserve exact DAG lineage")
        if (
            revised.evidence.source_dag_id != source.dag_id
            or revised.evidence.source_definition_fingerprint != source.definition_fingerprint
            or revised.evidence.source_generation != source.generation
            or revised.evidence.source_terminal_state is not TaskDagState.FAILED
        ):
            raise ConfigurationError("Swarm Replan evidence does not preserve source identity")
        if (
            run.successor_dag_id is not None
            and run.successor_dag_id != revised.successor_dag.dag_id
        ):
            raise ConfigurationError("Swarm successor DAG identity changed during recovery")

    async def _finalize(self, run: AgentSwarmRun) -> AgentSwarmRun:
        dag = await self._load_current_dag(run)
        if dag.state is not TaskDagState.COMPLETED:
            raise ConfigurationError("Swarm finalization requires a completed current DAG")
        if run.final_response is None or run.final_result_fingerprint is None:
            raise ConfigurationError("Swarm finalization result identity is incomplete")
        expected = _result_fingerprint(run.swarm_run_id, dag, run.final_response)
        if expected != run.final_result_fingerprint:
            raise ConfigurationError("Swarm final result fingerprint is inconsistent")
        return await self._transition(run, AgentSwarmRunState.COMPLETED)

    async def _load_current_dag(self, run: AgentSwarmRun) -> TaskDag:
        if run.current_dag_id is None:
            raise ConfigurationError("Swarm current DAG identity is missing")
        try:
            dag = await self._dag_store.get_task_dag(run.current_dag_id)
        except Exception as error:
            raise ConfigurationError("Swarm current DAG lookup failed") from error
        if dag is None:
            raise ConfigurationError("Swarm current DAG is missing")
        if dag.parent_session_id != self._parent_session_id:
            raise ConfigurationError("Swarm current DAG is outside the actual parent binding")
        if run.current_dag_definition_fingerprint is not None and (
            dag.definition_fingerprint != run.current_dag_definition_fingerprint
        ):
            raise ConfigurationError("Swarm current DAG definition fingerprint changed")
        if run.current_dag_generation is not None and dag.generation < run.current_dag_generation:
            raise ConfigurationError("Swarm current DAG generation regressed")
        return dag

    async def _transition(
        self,
        run: AgentSwarmRun,
        state: AgentSwarmRunState,
        **changes: object,
    ) -> AgentSwarmRun:
        if not run.state.can_transition_to(state):
            raise ConfigurationError(
                f"invalid Swarm lifecycle transition {run.state.value} -> {state.value}"
            )
        now = self._clock().astimezone(UTC)
        proposed = replace(
            run,
            **cast(Any, changes),
            state=state,
            generation=run.generation + 1,
            owner_id=self._owner_id,
            owner_pid=self._owner_pid,
            owner_token=self._owner_token,
            lease_expires_at=now + timedelta(seconds=self._lease_seconds),
            updated_at=now,
        )
        try:
            return await self._store.compare_and_transition_swarm_run(
                proposed,
                expected_generation=run.generation,
                expected_state=run.state,
            )
        except AgentSwarmStoreError as error:
            raise ConfigurationError(
                "Swarm lifecycle fence was lost; automatic replay is disabled"
            ) from error

    async def _mark_indeterminate(self, run: AgentSwarmRun) -> None:
        try:
            current = await self._store.get_swarm_run(run.swarm_run_id)
            if current is None or current.owner_id != self._owner_id or current.state.terminal:
                return
            await self._transition(current, AgentSwarmRunState.INDETERMINATE)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise

    async def _result_from_run(self, run: AgentSwarmRun) -> AgentSwarmResult:
        if run.state is not AgentSwarmRunState.COMPLETED:
            raise ConfigurationError("Swarm run is not completed")
        dag = await self._load_current_dag(run)
        if dag.state is not TaskDagState.COMPLETED:
            raise ConfigurationError("completed Swarm run points to a non-completed DAG")
        if run.final_response is None or run.final_result_fingerprint is None:
            raise ConfigurationError("completed Swarm result is incomplete")
        if _result_fingerprint(run.swarm_run_id, dag, run.final_response) != (
            run.final_result_fingerprint
        ):
            raise ConfigurationError("completed Swarm result fingerprint is invalid")
        return AgentSwarmResult(run, dag)


# Keep the public spelling short while retaining the explicit bounded class.
AgentSwarmApplicationService = BoundedAgentSwarmApplicationService
BoundedSwarmApplicationService = BoundedAgentSwarmApplicationService
RunSwarmRequest = RunAgentSwarmRequest


__all__ = [
    "MAX_SWARM_LEASE_SECONDS",
    "AgentSwarmApplicationService",
    "AgentSwarmLeader",
    "AgentSwarmPlanner",
    "AgentSwarmReplanner",
    "BoundedAgentSwarmApplicationService",
    "BoundedSwarmApplicationService",
    "LeaderFactory",
    "PlannerFactory",
    "ReplannerFactory",
    "RunAgentSwarmRequest",
    "RunSwarmRequest",
]
