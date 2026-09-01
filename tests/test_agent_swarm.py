from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import os
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest

from neuro_code.application.ports.agent_swarm import (
    AgentSwarmRunClaim,
    AgentSwarmStoreError,
)
from neuro_code.application.ports.model import ModelProvider
from neuro_code.application.runtime.process_liveness import owner_is_alive
from neuro_code.application.sessions.binding import ConversationBinding, ConversationRunner
from neuro_code.application.workflows.agent_swarm import (
    AgentSwarmApplicationService,
    RunAgentSwarmRequest,
)
from neuro_code.application.workflows.leader import LeaderRunResult, RunLeaderRequest
from neuro_code.application.workflows.model_planning import (
    ModelDagPlanningResult,
    RunModelDagPlanningRequest,
)
from neuro_code.application.workflows.task_dag_replan import (
    RunTaskDagReplanRequest,
    TaskDagReplanResult,
)
from neuro_code.domain.agent_swarm import (
    MAX_SWARM_RESULT_BYTES,
    AgentSwarmResult,
    AgentSwarmRun,
    AgentSwarmRunState,
    objective_fingerprint,
    terminal_result_fingerprint,
)
from neuro_code.domain.leader import LeaderDecisionRecord
from neuro_code.domain.model_planning import (
    ModelDagProposal,
    ModelDagProposalNode,
    PlanningAttempt,
    PlanningAttemptState,
    PlanningProposalRecord,
)
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.task_dag import TaskDag, TaskDagNode, TaskDagNodeState, TaskDagState
from neuro_code.domain.task_dag_replan import (
    DagReplanAttempt,
    DagReplanAttemptState,
    DagReplanEvidenceNode,
    DagReplanProposalRecord,
    TaskDagReplanEvidenceEnvelope,
)
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.shared.errors import ConfigurationError


def _now() -> datetime:
    return datetime.now(UTC)


def _parent_binding(session_id: str) -> ConversationBinding:
    runner = SimpleNamespace(session_id=session_id)
    return ConversationBinding(
        runner=cast(ConversationRunner, runner),
        provider=cast(ModelProvider, SimpleNamespace()),
    )


def _candidate(
    parent_session_id: str,
    *,
    run_id: str = "swarm-test",
    objective: str = "bounded objective",
    owner_id: str = "owner-a",
    owner_pid: int | None = None,
    state: AgentSwarmRunState = AgentSwarmRunState.CLAIMED,
    generation: int = 0,
    dag: TaskDag | None = None,
    replan_revision_id: str | None = None,
    final_response: str | None = None,
) -> AgentSwarmRun:
    now = _now()
    current_dag_id = dag.dag_id if dag is not None else None
    current_dag_generation = dag.generation if dag is not None else None
    current_dag_definition_fingerprint = dag.definition_fingerprint if dag is not None else None
    final_result = (
        terminal_result_fingerprint(
            run_id,
            dag.dag_id,
            dag.generation,
            dag.definition_fingerprint,
            final_response,
        )
        if dag is not None and final_response is not None
        else None
    )
    return AgentSwarmRun(
        swarm_run_id=run_id,
        parent_session_id=parent_session_id,
        objective_fingerprint=objective_fingerprint(objective),
        planning_id=f"swarm-planning-{run_id}",
        state=state,
        generation=generation,
        owner_id=owner_id,
        owner_pid=owner_pid or os.getpid(),
        owner_token=f"token-{owner_id}",
        lease_expires_at=now + timedelta(minutes=5),
        created_at=now,
        updated_at=now,
        root_dag_id=current_dag_id,
        current_dag_id=current_dag_id,
        current_dag_generation=current_dag_generation,
        current_dag_definition_fingerprint=current_dag_definition_fingerprint,
        replan_revision_id=replan_revision_id,
        final_response=final_response,
        final_result_fingerprint=final_result,
    )


def _dag(parent_session_id: str, dag_id: str = "swarm-dag") -> TaskDag:
    return TaskDag.create(
        dag_id=dag_id,
        parent_session_id=parent_session_id,
        nodes=(TaskDagNode("node", 0, "run bounded work"),),
        created_at=_now(),
    )


def _completed_dag(parent_session_id: str, dag_id: str = "completed-swarm-dag") -> TaskDag:
    base = _dag(parent_session_id, dag_id=dag_id)
    completed_node = replace(
        base.node("node"),
        state=TaskDagNodeState.COMPLETED,
        generation=1,
        response_preview="completed result",
    )
    return replace(
        base,
        nodes=(completed_node,),
        state=TaskDagState.COMPLETED,
        generation=1,
        updated_at=_now(),
    )


def _failed_dag(parent_session_id: str, dag_id: str = "failed-swarm-dag") -> TaskDag:
    base = _dag(parent_session_id, dag_id=dag_id)
    failed_node = replace(
        base.node("node"),
        state=TaskDagNodeState.FAILED,
        generation=1,
        error_kind="fixture_failure",
        error_reason="bounded source failure",
    )
    return replace(
        base,
        nodes=(failed_node,),
        state=TaskDagState.FAILED,
        generation=1,
        updated_at=_now(),
    )


def _replan_result(parent_session_id: str, source: TaskDag) -> TaskDagReplanResult:
    successor = _dag(parent_session_id, dag_id=f"{source.dag_id}-successor")
    evidence = TaskDagReplanEvidenceEnvelope(
        source_dag_id=source.dag_id,
        source_definition_fingerprint=source.definition_fingerprint,
        source_terminal_state=TaskDagState.FAILED,
        source_generation=source.generation,
        nodes=(
            DagReplanEvidenceNode(
                node_id="node",
                ordinal=0,
                state=TaskDagNodeState.FAILED,
                generation=source.node("node").generation,
                failure_kind="fixture_failure",
                failure_summary="bounded source failure",
            ),
        ),
    )
    proposal = ModelDagProposal(
        nodes=(ModelDagProposalNode("node", "run bounded successor work"),),
        max_parallel=1,
        reason="fixture",
    )
    now = _now()
    attempt = DagReplanAttempt(
        revision_id=f"revision-{source.dag_id}",
        parent_session_id=parent_session_id,
        source_dag_id=source.dag_id,
        source_definition_fingerprint=source.definition_fingerprint,
        source_generation=source.generation,
        source_state=TaskDagState.FAILED,
        revision_depth=1,
        evidence_fingerprint=evidence.fingerprint,
        evidence_json=evidence.canonical_json,
        planner_session_id="replan-planner",
        planner_turn_id="replan-turn",
        intended_successor_dag_id=successor.dag_id,
        state=DagReplanAttemptState.COMPLETED,
        owner_id="replan-owner",
        lease_expires_at=now + timedelta(minutes=5),
        model_response='{"bounded":true}',
        proposal_fingerprint=proposal.fingerprint,
        successor_dag_id=successor.dag_id,
        created_at=now,
        updated_at=now,
    )
    record = DagReplanProposalRecord(
        proposal_id=f"proposal-{source.dag_id}",
        revision_id=attempt.revision_id,
        parent_session_id=parent_session_id,
        source_dag_id=source.dag_id,
        source_definition_fingerprint=source.definition_fingerprint,
        source_generation=source.generation,
        evidence_fingerprint=evidence.fingerprint,
        intended_successor_dag_id=successor.dag_id,
        proposal=proposal,
        created_at=now,
    )
    return TaskDagReplanResult(
        attempt.revision_id,
        attempt,
        record,
        successor,
        evidence,
    )


async def _store_with_parent(tmp_path: Path) -> tuple[SqliteSessionStore, str]:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    await store.initialize()
    parent_session_id = await store.create_session(
        str(tmp_path),
        "fixture",
        "fixture-model",
        sandbox_profile=SandboxProfile.OFF,
    )
    return store, parent_session_id


def _planning_result(
    parent_session_id: str,
    planner_session_id: str,
    dag: TaskDag,
    planning_id: str,
) -> ModelDagPlanningResult:
    now = _now()
    proposal = ModelDagProposal(
        nodes=(ModelDagProposalNode("node", "run bounded work"),),
        max_parallel=1,
        reason="fixture",
    )
    model_response = json.dumps(proposal.payload, separators=(",", ":"))
    attempt = PlanningAttempt(
        planning_id=planning_id,
        parent_session_id=parent_session_id,
        objective_fingerprint=objective_fingerprint("bounded objective"),
        context_fingerprint="1" * 64,
        planner_session_id=planner_session_id,
        planner_turn_id=f"turn-{planning_id}",
        intended_dag_id=dag.dag_id,
        state=PlanningAttemptState.COMPLETED,
        owner_id="fixture-planner-owner",
        lease_expires_at=now + timedelta(minutes=5),
        model_response=model_response,
        proposal_fingerprint=proposal.fingerprint,
        dag_id=dag.dag_id,
        created_at=now,
        updated_at=now,
    )
    record = PlanningProposalRecord(
        proposal_id=f"proposal-{planning_id}",
        planning_id=planning_id,
        parent_session_id=parent_session_id,
        intended_dag_id=dag.dag_id,
        objective_fingerprint=attempt.objective_fingerprint,
        context_fingerprint=attempt.context_fingerprint,
        proposal=proposal,
        created_at=now,
    )
    return ModelDagPlanningResult(planning_id, attempt, record, dag)


class _PlannerFixture:
    def __init__(self, result: ModelDagPlanningResult) -> None:
        self._result = result
        self.calls = 0
        self.closed = 0

    @property
    def planning_session_id(self) -> str:
        return self._result.attempt.planner_session_id

    async def run(
        self,
        request: RunModelDagPlanningRequest,
        *,
        sink=None,
    ) -> ModelDagPlanningResult:
        del sink
        assert request.planning_id == self._result.planning_id
        self.calls += 1
        return self._result

    async def close(self) -> None:
        self.closed += 1


class _BlockingPlannerFixture:
    def __init__(self, started: asyncio.Event) -> None:
        self._started = started
        self.closed = 0

    @property
    def planning_session_id(self) -> str:
        return "blocking-planner-session"

    async def run(
        self,
        request: RunModelDagPlanningRequest,
        *,
        sink=None,
    ) -> ModelDagPlanningResult:
        del request, sink
        self._started.set()
        await asyncio.Event().wait()
        raise AssertionError("blocking planner should be cancelled")

    async def close(self) -> None:
        self.closed += 1


class _StaticLeaderFixture:
    def __init__(self, result: LeaderRunResult) -> None:
        self._result = result
        self.calls = 0
        self.closed = 0

    async def run(self, request: RunLeaderRequest, *, sink=None) -> LeaderRunResult:
        del request, sink
        self.calls += 1
        return self._result

    async def close(self) -> None:
        self.closed += 1


class _StaticReplannerFixture:
    def __init__(self, result: TaskDagReplanResult) -> None:
        self._result = result
        self.calls = 0
        self.closed = 0

    async def run(
        self,
        request: RunTaskDagReplanRequest,
        *,
        sink=None,
    ) -> TaskDagReplanResult:
        del sink
        assert request.revision_id == self._result.revision_id
        assert request.source_dag_id == self._result.attempt.source_dag_id
        self.calls += 1
        return self._result

    async def close(self) -> None:
        self.closed += 1


class _CompletingLeaderFixture:
    def __init__(self, store: SqliteSessionStore) -> None:
        self._store = store
        self.calls = 0
        self.closed = 0

    async def run(self, request: RunLeaderRequest, *, sink=None) -> LeaderRunResult:
        del sink
        self.calls += 1
        current = await self._store.get_task_dag(request.dag_id)
        assert current is not None
        node = current.node("node")
        running = replace(
            node,
            state=TaskDagNodeState.RUNNING,
            generation=node.generation + 1,
            parent_task_id="fixture-parent-task",
        )
        claimed = await self._store.claim_task_dag_node(
            current.dag_id,
            running,
            expected_generation=node.generation,
            expected_state=TaskDagNodeState.READY,
            updated_at=_now(),
            expected_dag_generation=current.generation,
        )
        finished = replace(
            claimed.node("node"),
            state=TaskDagNodeState.COMPLETED,
            generation=claimed.node("node").generation + 1,
            response_preview="recovered result",
        )
        finished_dag = await self._store.finish_task_dag_node(
            claimed.dag_id,
            finished,
            expected_generation=claimed.node("node").generation,
            expected_state=TaskDagNodeState.RUNNING,
            updated_at=_now(),
        )
        completed = await self._store.compare_and_transition_task_dag(
            replace(
                finished_dag,
                state=TaskDagState.COMPLETED,
                generation=finished_dag.generation + 1,
                updated_at=_now(),
            ),
            expected_generation=finished_dag.generation,
            expected_state=TaskDagState.RUNNING,
        )
        return LeaderRunResult(completed, "recovered result", ())

    async def close(self) -> None:
        self.closed += 1


class _IndeterminateLeaderFixture:
    def __init__(self, dag: TaskDag) -> None:
        self._dag = dag
        self.calls = 0

    async def run(self, request: RunLeaderRequest, *, sink=None) -> LeaderRunResult:
        del sink
        assert request.dag_id == self._dag.dag_id
        self.calls += 1
        uncertain_node = replace(
            self._dag.node("node"),
            state=TaskDagNodeState.INDETERMINATE,
            generation=self._dag.node("node").generation + 1,
        )
        uncertain = replace(
            self._dag,
            nodes=(uncertain_node,),
            state=TaskDagState.INDETERMINATE,
            generation=self._dag.generation + 1,
            updated_at=_now(),
        )
        return LeaderRunResult(uncertain, None, cast(tuple[LeaderDecisionRecord, ...], ()))

    async def close(self) -> None:
        return None


async def _forbidden_factory() -> NoReturn:
    raise AssertionError("unexpected lower-layer factory invocation")


def _spawn_claimed_swarm(database: str, parent_session_id: str, run_id: str) -> None:
    async def claim() -> None:
        store = SqliteSessionStore(Path(database))
        await store.initialize()
        candidate = _candidate(
            parent_session_id,
            run_id=run_id,
            owner_id=f"crashed-controller-{os.getpid()}",
            owner_pid=os.getpid(),
        )
        result = await store.claim_swarm_run(
            candidate,
            now=_now(),
            owner_is_alive=owner_is_alive,
        )
        assert result.acquired is True
        os._exit(79)

    asyncio.run(claim())


def test_swarm_domain_fingerprint_and_lifecycle_bounds() -> None:
    assert objective_fingerprint("bounded objective") == objective_fingerprint("bounded objective")
    run = _candidate("parent")
    assert run.same_identity(replace(run, owner_id="other-owner"))
    assert AgentSwarmRunState.CLAIMED.can_transition_to(AgentSwarmRunState.PLANNING)
    assert not AgentSwarmRunState.PLANNING.can_transition_to(AgentSwarmRunState.EXECUTING)
    with pytest.raises(ValueError, match="completed swarm run"):
        replace(run, state=AgentSwarmRunState.COMPLETED)
    with pytest.raises(ValueError, match="final response"):
        terminal_result_fingerprint(
            "run",
            "dag",
            0,
            "0" * 64,
            "",
        )


def test_swarm_domain_rejects_invalid_persisted_projection() -> None:
    run = _candidate("domain-parent")
    dag = _dag("domain-parent", dag_id="domain-lineage")
    with pytest.raises(ValueError, match="safe identifier"):
        replace(run, swarm_run_id="")
    with pytest.raises(ValueError, match="bounded safe text"):
        replace(run, final_response="\x01")
    with pytest.raises(ValueError, match="unsafe control"):
        objective_fingerprint("unsafe\x01objective")
    with pytest.raises(ValueError, match="generation"):
        terminal_result_fingerprint("run", "dag", True, "0" * 64, "result")
    with pytest.raises(ValueError, match="state must be canonical"):
        replace(run, state=cast(Any, "claimed"))
    with pytest.raises(ValueError, match="generation"):
        replace(run, generation=-1)
    with pytest.raises(ValueError, match="owner pid"):
        replace(run, owner_pid=0)
    with pytest.raises(ValueError, match="planner identity"):
        replace(run, planner_session_id="planner")
    with pytest.raises(ValueError, match="current DAG identity"):
        replace(run, current_dag_generation=0)
    with pytest.raises(ValueError, match="current DAG generation"):
        replace(
            _candidate("domain-parent", dag=dag),
            current_dag_generation=-1,
        )
    with pytest.raises(ValueError, match="without a root DAG"):
        replace(run, current_dag_id="orphan-dag")
    with pytest.raises(ValueError, match="requires a replan"):
        replace(_candidate("domain-parent", dag=dag), successor_dag_id="successor")
    with pytest.raises(ValueError, match="distinct"):
        replace(
            _candidate("domain-parent", dag=dag, replan_revision_id="revision"),
            successor_dag_id=dag.dag_id,
        )
    with pytest.raises(ValueError, match="requires a final response"):
        replace(
            _candidate("domain-parent", dag=dag),
            final_result_fingerprint="0" * 64,
        )
    completed = _candidate(
        "domain-parent",
        state=AgentSwarmRunState.COMPLETED,
        dag=_completed_dag("domain-parent"),
        final_response="done",
    )
    with pytest.raises(ValueError, match="inconsistent"):
        replace(completed, final_result_fingerprint="0" * 64)
    with pytest.raises(ValueError, match="pending terminal result"):
        replace(run, state=AgentSwarmRunState.FINALIZING)
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(run, lease_expires_at=datetime.fromtimestamp(0, UTC).replace(tzinfo=None))
    with pytest.raises(ValueError, match="must not precede"):
        replace(run, updated_at=run.created_at - timedelta(seconds=1))

    with pytest.raises(ValueError, match="completed durable run"):
        AgentSwarmResult(cast(Any, object()), dag)
    with pytest.raises(ValueError, match="terminal DAG"):
        AgentSwarmResult(completed, dag)
    with pytest.raises(ValueError, match="does not match"):
        AgentSwarmResult(completed, _completed_dag("domain-parent", "other-lineage"))


@pytest.mark.parametrize(
    ("run_id", "objective", "message"),
    [
        ("", "objective", "run id"),
        ("run", "", "objective"),
        ("run", "unsafe\x00objective", "objective"),
        ("unsafe\nrun", "objective", "run id"),
    ],
)
def test_swarm_request_rejects_unbounded_or_unsafe_identity(
    run_id: str,
    objective: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        RunAgentSwarmRequest(run_id, objective)


@pytest.mark.asyncio
async def test_swarm_rejects_invalid_composition_contracts(tmp_path: Path) -> None:
    store, parent_session_id = await _store_with_parent(tmp_path)
    parent_binding = _parent_binding(parent_session_id)

    def service(**overrides: object) -> AgentSwarmApplicationService:
        values: dict[str, object] = {
            "store": store,
            "dag_store": store,
            "parent_binding": parent_binding,
            "planner_factory": _forbidden_factory,
            "leader_factory": _forbidden_factory,
            "replanner_factory": _forbidden_factory,
        }
        values.update(overrides)
        return AgentSwarmApplicationService(**cast(Any, values))

    with pytest.raises(ConfigurationError, match="parent binding"):
        service(parent_binding=cast(Any, object()))
    with pytest.raises(ConfigurationError, match="lower-layer factories"):
        service(planner_factory=cast(Any, None))
    with pytest.raises(ConfigurationError, match="lease duration"):
        service(lease_seconds=0)
    with pytest.raises(ConfigurationError, match="parent session identity"):
        service(parent_binding=_parent_binding(cast(str, None)))
    with pytest.raises(ConfigurationError, match="owner identity"):
        service(owner_id="")
    with pytest.raises(ConfigurationError, match="store is invalid"):
        service(store=cast(Any, object()))
    with pytest.raises(ConfigurationError, match="Task DAG store is invalid"):
        service(dag_store=cast(Any, object()))

    valid = service()
    with pytest.raises(ValueError, match="request must be canonical"):
        await valid.run(cast(Any, object()))


@pytest.mark.asyncio
async def test_swarm_rejects_noncanonical_lower_phase_services(tmp_path: Path) -> None:
    store, parent_session_id = await _store_with_parent(tmp_path)
    parent_binding = _parent_binding(parent_session_id)

    async def invalid_factory() -> Any:
        return object()

    planner_service = AgentSwarmApplicationService(
        store,
        store,
        parent_binding=parent_binding,
        planner_factory=invalid_factory,
        leader_factory=_forbidden_factory,
        replanner_factory=_forbidden_factory,
        owner_id="invalid-planner-service",
    )
    with pytest.raises(ConfigurationError, match="planner factory"):
        await planner_service.run(
            RunAgentSwarmRequest("invalid-planner-service", "bounded objective")
        )
    planner_run = await store.get_swarm_run("invalid-planner-service")
    assert planner_run is not None
    assert planner_run.state is AgentSwarmRunState.INDETERMINATE

    ready_dag = _dag(parent_session_id, dag_id="invalid-leader-dag")
    await store.insert_task_dag(ready_dag)
    leader_run = _candidate(
        parent_session_id,
        run_id="invalid-leader-service",
        owner_id="invalid-leader-service",
        owner_pid=2_147_483_647,
        state=AgentSwarmRunState.EXECUTING,
        dag=ready_dag,
    )
    await store.claim_swarm_run(leader_run, now=_now(), owner_is_alive=owner_is_alive)
    leader_service = AgentSwarmApplicationService(
        store,
        store,
        parent_binding=parent_binding,
        planner_factory=_forbidden_factory,
        leader_factory=invalid_factory,
        replanner_factory=_forbidden_factory,
        owner_id="invalid-leader-service",
    )
    with pytest.raises(ConfigurationError, match="Leader factory"):
        await leader_service.run(
            RunAgentSwarmRequest("invalid-leader-service", "bounded objective")
        )
    leader_persisted = await store.get_swarm_run("invalid-leader-service")
    assert leader_persisted is not None
    assert leader_persisted.state is AgentSwarmRunState.INDETERMINATE

    failed_node = replace(
        ready_dag.node("node"),
        state=TaskDagNodeState.FAILED,
        generation=1,
        error_kind="fixture_failure",
        error_reason="source failed",
    )
    failed_dag = replace(
        ready_dag,
        dag_id="invalid-replanner-dag",
        nodes=(failed_node,),
        state=TaskDagState.FAILED,
        generation=1,
        updated_at=_now(),
    )
    await store.insert_task_dag(failed_dag)
    replanner_run = _candidate(
        parent_session_id,
        run_id="invalid-replanner-service",
        owner_id="invalid-replanner-service",
        owner_pid=2_147_483_647,
        state=AgentSwarmRunState.REPLANNING,
        dag=failed_dag,
        replan_revision_id="invalid-replanner-revision",
    )
    await store.claim_swarm_run(replanner_run, now=_now(), owner_is_alive=owner_is_alive)
    replanner_service = AgentSwarmApplicationService(
        store,
        store,
        parent_binding=parent_binding,
        planner_factory=_forbidden_factory,
        leader_factory=_forbidden_factory,
        replanner_factory=invalid_factory,
        owner_id="invalid-replanner-service",
    )
    with pytest.raises(ConfigurationError, match="Replan factory"):
        await replanner_service.run(
            RunAgentSwarmRequest("invalid-replanner-service", "bounded objective")
        )
    replanner_persisted = await store.get_swarm_run("invalid-replanner-service")
    assert replanner_persisted is not None
    assert replanner_persisted.state is AgentSwarmRunState.INDETERMINATE


@pytest.mark.asyncio
async def test_swarm_runtime_guardrails_and_fencing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, parent_session_id = await _store_with_parent(tmp_path)
    parent_binding = _parent_binding(parent_session_id)
    service = AgentSwarmApplicationService(
        store,
        store,
        parent_binding=parent_binding,
        planner_factory=_forbidden_factory,
        leader_factory=_forbidden_factory,
        replanner_factory=_forbidden_factory,
        owner_id="guardrail-controller",
    )
    assert service.owner_id == "guardrail-controller"
    assert service.parent_session_id == parent_session_id
    await service.close()

    terminal_dag = _failed_dag(parent_session_id, dag_id="terminal-swarm-dag")
    await store.insert_task_dag(terminal_dag)
    terminal_run = _candidate(
        parent_session_id,
        run_id="terminal-swarm-run",
        owner_id=service.owner_id,
        state=AgentSwarmRunState.FAILED,
        dag=terminal_dag,
    )
    await store.claim_swarm_run(terminal_run, now=_now(), owner_is_alive=owner_is_alive)
    with pytest.raises(ConfigurationError, match="terminally failed"):
        await service.run(RunAgentSwarmRequest("terminal-swarm-run", "bounded objective"))

    async def fail_claim(*args: Any, **kwargs: Any) -> AgentSwarmRunClaim:
        del args, kwargs
        raise AgentSwarmStoreError("claim backend unavailable")

    monkeypatch.setattr(store, "claim_swarm_run", fail_claim)
    with pytest.raises(ConfigurationError, match="durable claim failed"):
        await service.run(RunAgentSwarmRequest("claim-error", "bounded objective"))

    async def conflicting_claim(*args: Any, **kwargs: Any) -> AgentSwarmRunClaim:
        del args, kwargs
        return AgentSwarmRunClaim(
            _candidate(parent_session_id, run_id="other-identity", owner_id="other-owner"),
            True,
        )

    monkeypatch.setattr(store, "claim_swarm_run", conflicting_claim)
    with pytest.raises(ConfigurationError, match="identity conflicts"):
        await service.run(
            RunAgentSwarmRequest("identity-conflict", "bounded objective"),
        )

    async def dead_owner_claim(*args: Any, **kwargs: Any) -> AgentSwarmRunClaim:
        del args, kwargs
        return AgentSwarmRunClaim(
            _candidate(
                parent_session_id,
                run_id="unreclaimable",
                owner_id="dead-owner",
                owner_pid=2_147_483_647,
            ),
            False,
        )

    monkeypatch.setattr(store, "claim_swarm_run", dead_owner_claim)
    with pytest.raises(ConfigurationError, match="not durably reclaimable"):
        await service.run(RunAgentSwarmRequest("unreclaimable", "bounded objective"))

    ready_dag = _dag(parent_session_id, dag_id="guardrail-ready-dag")
    executing = _candidate(
        parent_session_id,
        run_id="guardrail-executing",
        state=AgentSwarmRunState.EXECUTING,
        dag=ready_dag,
    )

    async def return_indeterminate(
        run: AgentSwarmRun,
        objective: str,
        *,
        sink: Any,
    ) -> AgentSwarmRun:
        del objective, sink
        return replace(run, state=AgentSwarmRunState.INDETERMINATE)

    monkeypatch.setattr(service, "_execute_dag", return_indeterminate)
    with pytest.raises(ConfigurationError, match="execution is indeterminate"):
        await service._drive(executing, "bounded objective", sink=None)
    with pytest.raises(ConfigurationError, match="unsupported Swarm lifecycle"):
        await service._drive(
            _candidate(parent_session_id, run_id="unsupported", state=AgentSwarmRunState.FAILED),
            "bounded objective",
            sink=None,
        )

    completed_run = _candidate(
        parent_session_id,
        run_id="invalid-transition",
        state=AgentSwarmRunState.COMPLETED,
        dag=_completed_dag(parent_session_id),
        final_response="done",
    )
    with pytest.raises(ConfigurationError, match="invalid Swarm lifecycle"):
        await service._transition(completed_run, AgentSwarmRunState.COMPLETED)

    async def fail_transition(*args: Any, **kwargs: Any) -> AgentSwarmRun:
        del args, kwargs
        raise AgentSwarmStoreError("lifecycle CAS lost")

    monkeypatch.setattr(store, "compare_and_transition_swarm_run", fail_transition)
    with pytest.raises(ConfigurationError, match="lifecycle fence was lost"):
        await service._transition(
            _candidate(parent_session_id, run_id="transition-error"),
            AgentSwarmRunState.PLANNING,
        )

    original_get_task_dag = store.get_task_dag

    async def fail_dag_lookup(*args: Any, **kwargs: Any) -> TaskDag | None:
        del args, kwargs
        raise RuntimeError("DAG backend unavailable")

    monkeypatch.setattr(store, "get_task_dag", fail_dag_lookup)
    with pytest.raises(ConfigurationError, match="DAG lookup failed"):
        await service._load_current_dag(
            _candidate(parent_session_id, dag=ready_dag),
        )
    monkeypatch.setattr(store, "get_task_dag", original_get_task_dag)

    await store.insert_task_dag(_completed_dag(parent_session_id, "result-guard-dag"))
    result_dag = await store.get_task_dag("result-guard-dag")
    assert result_dag is not None
    fake_run = SimpleNamespace(
        swarm_run_id="result-guard",
        state=AgentSwarmRunState.COMPLETED,
        current_dag_id=result_dag.dag_id,
        current_dag_generation=result_dag.generation,
        current_dag_definition_fingerprint=result_dag.definition_fingerprint,
        final_response=None,
        final_result_fingerprint="0" * 64,
    )
    with pytest.raises(ConfigurationError, match="result is incomplete"):
        await service._result_from_run(cast(AgentSwarmRun, fake_run))
    fake_run.final_response = "done"
    with pytest.raises(ConfigurationError, match="result fingerprint is invalid"):
        await service._result_from_run(cast(AgentSwarmRun, fake_run))


@pytest.mark.asyncio
async def test_swarm_rejects_current_dag_and_terminal_projection_mismatches(
    tmp_path: Path,
) -> None:
    store, parent_session_id = await _store_with_parent(tmp_path)
    parent_binding = _parent_binding(parent_session_id)
    service = AgentSwarmApplicationService(
        store,
        store,
        parent_binding=parent_binding,
        planner_factory=_forbidden_factory,
        leader_factory=_forbidden_factory,
        replanner_factory=_forbidden_factory,
        owner_id="lineage-checker",
    )

    with pytest.raises(ConfigurationError, match="identity is missing"):
        await service._load_current_dag(_candidate(parent_session_id))

    missing_dag = _dag(parent_session_id, dag_id="missing-current-dag")
    with pytest.raises(ConfigurationError, match="DAG is missing"):
        await service._load_current_dag(_candidate(parent_session_id, dag=missing_dag))

    foreign_parent = await store.create_session(
        str(tmp_path),
        "fixture",
        "fixture-model",
        sandbox_profile=SandboxProfile.OFF,
    )
    foreign_dag = _dag(foreign_parent, dag_id="foreign-current-dag")
    await store.insert_task_dag(foreign_dag)
    with pytest.raises(ConfigurationError, match="outside"):
        await service._load_current_dag(_candidate(parent_session_id, dag=foreign_dag))

    await store.insert_task_dag(missing_dag)
    stored_run = _candidate(parent_session_id, dag=missing_dag)
    with pytest.raises(ConfigurationError, match="definition fingerprint"):
        await service._load_current_dag(
            replace(stored_run, current_dag_definition_fingerprint="f" * 64)
        )
    with pytest.raises(ConfigurationError, match="generation regressed"):
        await service._load_current_dag(
            replace(stored_run, current_dag_generation=missing_dag.generation + 1)
        )

    with pytest.raises(ConfigurationError, match="not completed"):
        await service._result_from_run(stored_run)
    completed_ready = _candidate(
        parent_session_id,
        run_id="completed-ready-dag",
        state=AgentSwarmRunState.COMPLETED,
        dag=missing_dag,
        final_response="not actually complete",
    )
    with pytest.raises(ConfigurationError, match="non-completed DAG"):
        await service._result_from_run(completed_ready)


@pytest.mark.asyncio
async def test_swarm_phase_validation_and_terminal_projection_guards(tmp_path: Path) -> None:
    store, parent_session_id = await _store_with_parent(tmp_path)
    parent_binding = _parent_binding(parent_session_id)
    service = AgentSwarmApplicationService(
        store,
        store,
        parent_binding=parent_binding,
        planner_factory=_forbidden_factory,
        leader_factory=_forbidden_factory,
        replanner_factory=_forbidden_factory,
        owner_id="phase-guard-controller",
    )

    planning_dag = _dag(parent_session_id, dag_id="planning-guard-dag")
    planned = _planning_result(
        parent_session_id,
        "planner-guard-session",
        planning_dag,
        "swarm-planning-planning-guard",
    )
    planning_run = _candidate(parent_session_id, run_id="planning-guard")
    with pytest.raises(ConfigurationError, match="non-canonical result"):
        service._verify_planning(planning_run, cast(Any, object()))
    with pytest.raises(ConfigurationError, match="fresh READY DAG"):
        service._verify_planning(
            planning_run,
            replace(planned, dag=_completed_dag(parent_session_id, "planning-guard-dag")),
        )
    with pytest.raises(ConfigurationError, match="root DAG identity"):
        service._verify_planning(
            replace(planning_run, root_dag_id="recovered-root"),
            planned,
        )

    leader_dag = _dag(parent_session_id, dag_id="leader-guard-dag")
    leader_run = _candidate(parent_session_id, run_id="leader-guard", dag=leader_dag)
    with pytest.raises(ConfigurationError, match="non-canonical result"):
        service._verify_leader_result(leader_run, cast(Any, object()))
    with pytest.raises(ConfigurationError, match="outside the exact lineage"):
        service._verify_leader_result(
            leader_run,
            LeaderRunResult(_dag(parent_session_id, dag_id="other-leader-dag"), None, ()),
        )

    completed_dag = _completed_dag(parent_session_id, "execute-completed-dag")
    await store.insert_task_dag(completed_dag)
    no_response_leader = _StaticLeaderFixture(LeaderRunResult(completed_dag, None, ()))

    async def no_response_factory() -> _StaticLeaderFixture:
        return no_response_leader

    no_response_service = AgentSwarmApplicationService(
        store,
        store,
        parent_binding=parent_binding,
        planner_factory=_forbidden_factory,
        leader_factory=no_response_factory,
        replanner_factory=_forbidden_factory,
        owner_id="no-response-controller",
    )
    with pytest.raises(ConfigurationError, match="no Leader final response"):
        await no_response_service._execute_dag(
            _candidate(parent_session_id, run_id="no-response", dag=completed_dag),
            "bounded objective",
            sink=None,
        )
    assert no_response_leader.closed == 1

    too_long_leader = _StaticLeaderFixture(
        LeaderRunResult(completed_dag, "x" * (MAX_SWARM_RESULT_BYTES + 1), ()),
    )

    async def too_long_factory() -> _StaticLeaderFixture:
        return too_long_leader

    too_long_service = AgentSwarmApplicationService(
        store,
        store,
        parent_binding=parent_binding,
        planner_factory=_forbidden_factory,
        leader_factory=too_long_factory,
        replanner_factory=_forbidden_factory,
        owner_id="too-long-controller",
    )
    with pytest.raises(ConfigurationError, match="bounded result contract"):
        await too_long_service._execute_dag(
            _candidate(parent_session_id, run_id="too-long", dag=completed_dag),
            "bounded objective",
            sink=None,
        )
    assert too_long_leader.closed == 1

    nonterminal_dag = _dag(parent_session_id, dag_id="execute-nonterminal-dag")
    await store.insert_task_dag(nonterminal_dag)
    nonterminal_leader = _StaticLeaderFixture(LeaderRunResult(nonterminal_dag, None, ()))

    async def nonterminal_factory() -> _StaticLeaderFixture:
        return nonterminal_leader

    nonterminal_service = AgentSwarmApplicationService(
        store,
        store,
        parent_binding=parent_binding,
        planner_factory=_forbidden_factory,
        leader_factory=nonterminal_factory,
        replanner_factory=_forbidden_factory,
        owner_id="nonterminal-controller",
    )
    with pytest.raises(ConfigurationError, match="non-terminal or uncertain"):
        await nonterminal_service._execute_dag(
            _candidate(parent_session_id, run_id="nonterminal", dag=nonterminal_dag),
            "bounded objective",
            sink=None,
        )
    assert nonterminal_leader.closed == 1


@pytest.mark.asyncio
async def test_swarm_replan_lineage_and_source_immutability_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, parent_session_id = await _store_with_parent(tmp_path)
    parent_binding = _parent_binding(parent_session_id)
    service = AgentSwarmApplicationService(
        store,
        store,
        parent_binding=parent_binding,
        planner_factory=_forbidden_factory,
        leader_factory=_forbidden_factory,
        replanner_factory=_forbidden_factory,
        owner_id="replan-guard-controller",
    )
    with pytest.raises(ConfigurationError, match="identity is incomplete"):
        await service._replan(
            _candidate(parent_session_id, state=AgentSwarmRunState.REPLANNING),
            sink=None,
        )

    ready_source = _dag(parent_session_id, dag_id="replan-ready-source")
    await store.insert_task_dag(ready_source)
    with pytest.raises(ConfigurationError, match="quiescent FAILED source"):
        await service._replan(
            _candidate(
                parent_session_id,
                run_id="replan-ready",
                state=AgentSwarmRunState.REPLANNING,
                dag=ready_source,
                replan_revision_id="ready-revision",
            ),
            sink=None,
        )

    indeterminate_base = _failed_dag(parent_session_id, dag_id="replan-indeterminate-source")
    indeterminate_node = replace(
        indeterminate_base.node("node"),
        state=TaskDagNodeState.INDETERMINATE,
        generation=2,
    )
    indeterminate_source = replace(
        indeterminate_base,
        nodes=(indeterminate_node,),
        state=TaskDagState.FAILED,
        generation=2,
        updated_at=_now(),
    )
    await store.insert_task_dag(indeterminate_source)
    with pytest.raises(ConfigurationError, match="indeterminate source"):
        await service._replan(
            _candidate(
                parent_session_id,
                run_id="replan-indeterminate",
                state=AgentSwarmRunState.REPLANNING,
                dag=indeterminate_source,
                replan_revision_id="indeterminate-revision",
            ),
            sink=None,
        )

    source = _failed_dag(parent_session_id, dag_id="replan-lineage-source")
    await store.insert_task_dag(source)
    revised = _replan_result(parent_session_id, source)
    replan_run = _candidate(
        parent_session_id,
        run_id="replan-lineage",
        state=AgentSwarmRunState.REPLANNING,
        dag=source,
        replan_revision_id=revised.revision_id,
    )
    with pytest.raises(ConfigurationError, match="non-canonical result"):
        service._verify_replan(replan_run, source, cast(Any, object()))
    with pytest.raises(ConfigurationError, match="exact DAG lineage"):
        service._verify_replan(
            replan_run,
            source,
            replace(
                revised,
                proposal=replace(
                    revised.proposal,
                    source_generation=source.generation + 1,
                ),
            ),
        )
    wrong_evidence = SimpleNamespace(
        fingerprint=revised.evidence.fingerprint,
        source_dag_id="wrong-source",
        source_definition_fingerprint=source.definition_fingerprint,
        source_generation=source.generation,
        source_terminal_state=TaskDagState.FAILED,
    )
    with pytest.raises(ConfigurationError, match="source identity"):
        service._verify_replan(
            replan_run,
            source,
            replace(revised, evidence=cast(Any, wrong_evidence)),
        )
    with pytest.raises(ConfigurationError, match="successor DAG identity"):
        service._verify_replan(
            replace(replan_run, successor_dag_id="other-successor"),
            source,
            revised,
        )

    replanner = _StaticReplannerFixture(revised)

    async def replanner_factory() -> _StaticReplannerFixture:
        return replanner

    original_get_task_dag = store.get_task_dag
    lookup_count = 0

    async def changed_source_after_first_lookup(dag_id: str) -> TaskDag | None:
        nonlocal lookup_count
        lookup_count += 1
        if lookup_count == 1:
            return await original_get_task_dag(dag_id)
        current = await original_get_task_dag(dag_id)
        assert current is not None
        return replace(current, generation=current.generation + 1, updated_at=_now())

    monkeypatch.setattr(store, "get_task_dag", changed_source_after_first_lookup)
    mutating_guard_service = AgentSwarmApplicationService(
        store,
        store,
        parent_binding=parent_binding,
        planner_factory=_forbidden_factory,
        leader_factory=_forbidden_factory,
        replanner_factory=replanner_factory,
        owner_id="replan-mutation-guard",
    )
    with pytest.raises(ConfigurationError, match="immutable source DAG"):
        await mutating_guard_service._replan(replan_run, sink=None)
    assert replanner.calls == replanner.closed == 1


@pytest.mark.asyncio
async def test_swarm_claim_race_live_owner_and_dead_owner_takeover(tmp_path: Path) -> None:
    store_a, parent_session_id = await _store_with_parent(tmp_path)
    store_b = SqliteSessionStore(tmp_path / "sessions.db")
    await store_b.initialize()
    now = _now()
    candidates = (
        _candidate(parent_session_id, run_id="claim-race", owner_id="race-a"),
        _candidate(parent_session_id, run_id="claim-race", owner_id="race-b"),
    )
    claims = await asyncio.gather(
        store_a.claim_swarm_run(candidates[0], now=now, owner_is_alive=owner_is_alive),
        store_b.claim_swarm_run(candidates[1], now=now, owner_is_alive=owner_is_alive),
    )
    assert sum(claim.acquired for claim in claims) == 1
    winner = next(claim.run for claim in claims if claim.acquired)
    loser = next(claim.run for claim in claims if not claim.acquired)
    assert loser.owner_id == winner.owner_id
    assert loser.generation == winner.generation == 0

    dead = _candidate(
        parent_session_id,
        run_id="dead-owner",
        owner_id="dead-owner",
        owner_pid=2_147_483_647,
    )
    assert (
        await store_a.claim_swarm_run(dead, now=now, owner_is_alive=owner_is_alive)
    ).acquired is True
    takeover = _candidate(
        parent_session_id,
        run_id="dead-owner",
        owner_id="fresh-owner",
        owner_pid=os.getpid(),
    )
    recovered = await store_b.claim_swarm_run(
        takeover,
        now=now,
        owner_is_alive=owner_is_alive,
    )
    assert recovered.acquired is True
    assert recovered.run.owner_id == "fresh-owner"
    assert recovered.run.generation == 1


@pytest.mark.asyncio
async def test_swarm_claim_requires_explicit_process_liveness_probe(tmp_path: Path) -> None:
    store, parent_session_id = await _store_with_parent(tmp_path)
    with pytest.raises(TypeError, match="liveness probe is required"):
        await store.claim_swarm_run(
            _candidate(parent_session_id, run_id="probe-required"),
            now=_now(),
            owner_is_alive=cast(object, None),
        )


@pytest.mark.asyncio
async def test_swarm_cas_rejects_stale_and_tampered_lifecycle_updates(tmp_path: Path) -> None:
    store, parent_session_id = await _store_with_parent(tmp_path)
    claimed = await store.claim_swarm_run(
        _candidate(parent_session_id, run_id="cas-run"),
        now=_now(),
        owner_is_alive=owner_is_alive,
    )
    assert claimed.acquired is True
    planning = replace(
        claimed.run,
        state=AgentSwarmRunState.PLANNING,
        generation=claimed.run.generation + 1,
        updated_at=_now(),
    )
    persisted = await store.compare_and_transition_swarm_run(
        planning,
        expected_generation=claimed.run.generation,
        expected_state=AgentSwarmRunState.CLAIMED,
    )
    assert persisted.state is AgentSwarmRunState.PLANNING
    stale = replace(
        persisted,
        state=AgentSwarmRunState.PLANNED,
        generation=persisted.generation + 1,
        updated_at=_now(),
    )
    with pytest.raises(AgentSwarmStoreError, match="stale"):
        await store.compare_and_transition_swarm_run(
            stale,
            expected_generation=persisted.generation,
            expected_state=AgentSwarmRunState.CLAIMED,
        )
    tampered = replace(
        persisted,
        objective_fingerprint=objective_fingerprint("different objective"),
        state=AgentSwarmRunState.PLANNED,
        generation=persisted.generation + 1,
        updated_at=_now(),
    )
    with pytest.raises(AgentSwarmStoreError, match="immutable identity"):
        await store.compare_and_transition_swarm_run(
            tampered,
            expected_generation=persisted.generation,
            expected_state=AgentSwarmRunState.PLANNING,
        )


@pytest.mark.asyncio
async def test_swarm_schema_fk_restrict_and_tamper_rejection(tmp_path: Path) -> None:
    store, parent_session_id = await _store_with_parent(tmp_path)
    dag = _dag(parent_session_id)
    await store.insert_task_dag(dag)
    candidate = _candidate(parent_session_id, run_id="fk-run", dag=dag)
    await store.claim_swarm_run(candidate, now=_now(), owner_is_alive=owner_is_alive)
    database = store.database_path
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(orchestration_swarm_runs)"
        ).fetchall()
        assert foreign_keys
        assert {row[6] for row in foreign_keys} == {"RESTRICT"}
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM task_dags WHERE dag_id = ?", (dag.dag_id,))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM sessions WHERE id = ?", (parent_session_id,))
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "UPDATE orchestration_swarm_runs SET objective_fingerprint = ? WHERE swarm_run_id = ?",
            ("not-a-fingerprint", "fk-run"),
        )
        connection.commit()
    with pytest.raises(AgentSwarmStoreError, match="invalid"):
        await store.get_swarm_run("fk-run")


@pytest.mark.asyncio
async def test_swarm_fresh_controller_recovers_claim_after_process_death(tmp_path: Path) -> None:
    store, parent_session_id = await _store_with_parent(tmp_path)
    dag = _dag(parent_session_id, dag_id="process-recovery-dag")
    await store.insert_task_dag(dag)
    planner_session_id = await store.create_session(
        str(tmp_path),
        "fixture",
        "fixture-model",
        sandbox_profile=SandboxProfile.OFF,
    )
    run_id = "process-recovery"
    context = mp.get_context("spawn")
    process = context.Process(
        target=_spawn_claimed_swarm,
        args=(str(store.database_path), parent_session_id, run_id),
    )
    process.start()
    await asyncio.to_thread(process.join, 30)
    if process.is_alive():
        process.terminate()
        process.join(5)
    assert process.exitcode == 79
    crashed = await store.get_swarm_run(run_id)
    assert crashed is not None
    assert crashed.state is AgentSwarmRunState.CLAIMED
    assert crashed.owner_pid == process.pid
    process.close()

    planner_result = _planning_result(
        parent_session_id,
        planner_session_id,
        dag,
        f"swarm-planning-{run_id}",
    )
    planner = _PlannerFixture(planner_result)
    leader = _CompletingLeaderFixture(store)
    parent_binding = _parent_binding(parent_session_id)

    async def planner_factory() -> _PlannerFixture:
        return planner

    async def leader_factory() -> _CompletingLeaderFixture:
        return leader

    service = AgentSwarmApplicationService(
        store,
        store,
        parent_binding=parent_binding,
        planner_factory=planner_factory,
        leader_factory=leader_factory,
        replanner_factory=_forbidden_factory,
        owner_id="fresh-controller",
    )
    result = await service.run(RunAgentSwarmRequest(run_id, "bounded objective"))
    assert result.run.state is AgentSwarmRunState.COMPLETED
    assert result.final_response == "recovered result"
    assert planner.calls == leader.calls == 1
    assert planner.closed == leader.closed == 1
    persisted = await store.get_swarm_run(run_id)
    assert persisted == result.run


@pytest.mark.asyncio
async def test_swarm_finalizing_recovery_reuses_terminal_result_without_lower_calls(
    tmp_path: Path,
) -> None:
    store, parent_session_id = await _store_with_parent(tmp_path)
    base = _dag(parent_session_id, dag_id="finalizing-dag")
    completed_node = replace(
        base.node("node"),
        state=TaskDagNodeState.COMPLETED,
        generation=1,
        response_preview="durable result",
    )
    completed_dag = replace(
        base,
        nodes=(completed_node,),
        state=TaskDagState.COMPLETED,
        generation=1,
        updated_at=_now(),
    )
    await store.insert_task_dag(completed_dag)
    run = _candidate(
        parent_session_id,
        run_id="finalizing-recovery",
        owner_id="crashed-owner",
        owner_pid=2_147_483_647,
        state=AgentSwarmRunState.FINALIZING,
        dag=completed_dag,
        final_response="durable result",
    )
    await store.claim_swarm_run(run, now=_now(), owner_is_alive=owner_is_alive)
    parent_binding = _parent_binding(parent_session_id)
    service = AgentSwarmApplicationService(
        store,
        store,
        parent_binding=parent_binding,
        planner_factory=_forbidden_factory,
        leader_factory=_forbidden_factory,
        replanner_factory=_forbidden_factory,
        owner_id="fresh-finalizer",
    )
    result = await service.run(RunAgentSwarmRequest("finalizing-recovery", "bounded objective"))
    assert result.final_response == "durable result"
    assert result.run.state is AgentSwarmRunState.COMPLETED
    assert result.dag == completed_dag
    tampered = replace(
        result.run,
        final_response="tampered result",
        final_result_fingerprint=terminal_result_fingerprint(
            result.run.swarm_run_id,
            completed_dag.dag_id,
            completed_dag.generation,
            completed_dag.definition_fingerprint,
            "tampered result",
        ),
        generation=result.run.generation + 1,
        updated_at=_now(),
    )
    with pytest.raises(AgentSwarmStoreError, match="immutable field final_response"):
        await store.compare_and_transition_swarm_run(
            tampered,
            expected_generation=result.run.generation,
            expected_state=AgentSwarmRunState.COMPLETED,
        )


@pytest.mark.asyncio
async def test_swarm_cancellation_is_durable_indeterminate_without_recovery_calls(
    tmp_path: Path,
) -> None:
    store, parent_session_id = await _store_with_parent(tmp_path)
    started = asyncio.Event()
    planner = _BlockingPlannerFixture(started)
    parent_binding = _parent_binding(parent_session_id)

    async def planner_factory() -> _BlockingPlannerFixture:
        return planner

    service = AgentSwarmApplicationService(
        store,
        store,
        parent_binding=parent_binding,
        planner_factory=planner_factory,
        leader_factory=_forbidden_factory,
        replanner_factory=_forbidden_factory,
        owner_id="cancelled-controller",
    )
    task = asyncio.create_task(
        service.run(RunAgentSwarmRequest("cancelled-run", "bounded objective"))
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    persisted = await store.get_swarm_run("cancelled-run")
    assert persisted is not None
    assert persisted.state is AgentSwarmRunState.INDETERMINATE
    assert planner.closed == 1


@pytest.mark.asyncio
async def test_swarm_rejects_inconsistent_planner_result_as_indeterminate(
    tmp_path: Path,
) -> None:
    store, parent_session_id = await _store_with_parent(tmp_path)
    planner_session_id = await store.create_session(
        str(tmp_path),
        "fixture",
        "fixture-model",
        sandbox_profile=SandboxProfile.OFF,
    )
    dag = _dag(parent_session_id, dag_id="invalid-planner-dag")
    valid = _planning_result(
        parent_session_id,
        planner_session_id,
        dag,
        "swarm-planning-invalid-planner",
    )
    invalid = replace(
        valid,
        proposal=replace(
            valid.proposal,
            objective_fingerprint=objective_fingerprint("different objective"),
        ),
    )
    planner = _PlannerFixture(invalid)
    parent_binding = _parent_binding(parent_session_id)

    async def planner_factory() -> _PlannerFixture:
        return planner

    service = AgentSwarmApplicationService(
        store,
        store,
        parent_binding=parent_binding,
        planner_factory=planner_factory,
        leader_factory=_forbidden_factory,
        replanner_factory=_forbidden_factory,
        owner_id="invalid-planner-controller",
    )
    with pytest.raises(ConfigurationError, match="identity"):
        await service.run(
            RunAgentSwarmRequest("invalid-planner", "bounded objective"),
        )
    persisted = await store.get_swarm_run("invalid-planner")
    assert persisted is not None
    assert persisted.state is AgentSwarmRunState.INDETERMINATE
    assert planner.calls == planner.closed == 1


@pytest.mark.asyncio
async def test_swarm_failed_successor_is_terminal_without_second_replan(tmp_path: Path) -> None:
    store, parent_session_id = await _store_with_parent(tmp_path)
    base = _dag(parent_session_id, dag_id="failed-successor-dag")
    failed_node = replace(
        base.node("node"),
        state=TaskDagNodeState.FAILED,
        generation=1,
        error_kind="fixture_failure",
        error_reason="bounded successor failed",
    )
    failed_dag = replace(
        base,
        nodes=(failed_node,),
        state=TaskDagState.FAILED,
        generation=1,
        updated_at=_now(),
    )
    await store.insert_task_dag(failed_dag)
    run = _candidate(
        parent_session_id,
        run_id="failed-successor",
        owner_id="dead-owner",
        owner_pid=2_147_483_647,
        state=AgentSwarmRunState.EXECUTING,
        dag=failed_dag,
        replan_revision_id="already-replanned",
    )
    await store.claim_swarm_run(run, now=_now(), owner_is_alive=owner_is_alive)
    leader = _StaticLeaderFixture(LeaderRunResult(failed_dag, None, ()))
    parent_binding = _parent_binding(parent_session_id)

    async def leader_factory() -> _StaticLeaderFixture:
        return leader

    service = AgentSwarmApplicationService(
        store,
        store,
        parent_binding=parent_binding,
        planner_factory=_forbidden_factory,
        leader_factory=leader_factory,
        replanner_factory=_forbidden_factory,
        owner_id="successor-failure-controller",
    )
    with pytest.raises(ConfigurationError, match="successor DAG failed"):
        await service.run(RunAgentSwarmRequest("failed-successor", "bounded objective"))
    persisted = await store.get_swarm_run("failed-successor")
    assert persisted is not None
    assert persisted.state is AgentSwarmRunState.FAILED
    assert leader.calls == leader.closed == 1


@pytest.mark.asyncio
async def test_swarm_indeterminate_leader_result_is_terminal_and_not_replanned(
    tmp_path: Path,
) -> None:
    store, parent_session_id = await _store_with_parent(tmp_path)
    dag = _dag(parent_session_id, dag_id="indeterminate-dag")
    await store.insert_task_dag(dag)
    run = _candidate(
        parent_session_id,
        run_id="indeterminate-run",
        owner_id="dead-owner",
        owner_pid=2_147_483_647,
        state=AgentSwarmRunState.EXECUTING,
        dag=dag,
    )
    await store.claim_swarm_run(run, now=_now(), owner_is_alive=owner_is_alive)
    leader = _IndeterminateLeaderFixture(dag)
    parent_binding = _parent_binding(parent_session_id)

    async def leader_factory() -> _IndeterminateLeaderFixture:
        return leader

    service = AgentSwarmApplicationService(
        store,
        store,
        parent_binding=parent_binding,
        planner_factory=_forbidden_factory,
        leader_factory=leader_factory,
        replanner_factory=_forbidden_factory,
        owner_id="fresh-indeterminate",
    )
    with pytest.raises(ConfigurationError, match="uncertain"):
        await service.run(RunAgentSwarmRequest("indeterminate-run", "bounded objective"))
    persisted = await store.get_swarm_run("indeterminate-run")
    assert persisted is not None
    assert persisted.state is AgentSwarmRunState.INDETERMINATE
    assert persisted.replan_revision_id is None
    assert leader.calls == 1
