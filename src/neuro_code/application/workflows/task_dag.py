"""Bounded parallel execution of explicit durable task DAGs."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from neuro_code.application.ports.parent_context_relay import ParentContextRelayStore
from neuro_code.application.ports.task_dag import TaskDagError, TaskDagStore
from neuro_code.application.ports.task_dag_recovery import (
    TaskDagRecoveryClaimError,
    TaskDagRecoveryClaimStore,
)
from neuro_code.application.ports.task_dag_result_relay import (
    TaskDagDependencyResultRelayStore,
)
from neuro_code.application.ports.writable_subagent import WritableSubagentLeaseStore
from neuro_code.application.runtime.agent import EventSink
from neuro_code.application.runtime.process_liveness import owner_is_alive
from neuro_code.application.workflows.task_dag_result_relay import (
    TaskDagDependencyResultRelayApplicationService,
)
from neuro_code.application.workflows.writable_subagent import (
    RunWritableSubagentRequest,
    WritableSubagentExecutionIdentity,
    WritableSubagentResultProjection,
)
from neuro_code.domain.session_tasks import SessionTaskStatus
from neuro_code.domain.task_dag import (
    MAX_TASK_DAG_ERROR_BYTES,
    MAX_TASK_DAG_PARALLELISM,
    MAX_TASK_DAG_RESPONSE_PREVIEW_BYTES,
    TaskDag,
    TaskDagNode,
    TaskDagNodeState,
    TaskDagState,
)
from neuro_code.domain.task_dag_recovery import TaskDagRecoveryClaim
from neuro_code.domain.task_dag_result_relay import TaskDagDependencyResultRelay
from neuro_code.domain.writable_subagent import WritableSubagentWorkspaceState
from neuro_code.shared.errors import ConfigurationError

if TYPE_CHECKING:
    from neuro_code.application.ports.storage import SessionStore
    from neuro_code.application.sessions.binding import ConversationBinding


def _now() -> datetime:
    return datetime.now(UTC)


_ACTIVE_EVIDENCE_PROBE_COUNT = 8
_ACTIVE_EVIDENCE_PROBE_DELAY_SECONDS = 0.025


def _bounded_metadata_text(value: str, limit: int) -> str:
    """Keep durable diagnostics safe and bounded by UTF-8 bytes."""

    safe = "".join(
        character if ord(character) >= 32 or character in "\n\t\r" else "�" for character in value
    )
    encoded = safe.encode("utf-8")
    if len(encoded) <= limit:
        return safe
    return encoded[:limit].decode("utf-8", errors="ignore")


@dataclass(frozen=True, slots=True)
class CreateTaskDagRequest:
    """Explicit graph definition; the parent session comes from the binding."""

    dag_id: str
    nodes: tuple[TaskDagNode, ...]
    max_parallel: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.nodes, tuple):
            raise TypeError("task DAG request nodes must be a tuple")
        if (
            isinstance(self.max_parallel, bool)
            or not isinstance(self.max_parallel, int)
            or not 1 <= self.max_parallel <= MAX_TASK_DAG_PARALLELISM
        ):
            raise ValueError(
                f"task DAG request max_parallel must be between 1 and {MAX_TASK_DAG_PARALLELISM}"
            )


@dataclass(frozen=True, slots=True)
class RunTaskDagRequest:
    """Explicit request to execute one previously published DAG."""

    dag_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.dag_id, str) or not self.dag_id.strip():
            raise ValueError("task DAG request id must not be empty")


@dataclass(frozen=True, slots=True)
class RunTaskDagStepRequest:
    """Request one serialized DAG advancement.

    ``selected_node_id`` is optional only for the existing deterministic
    ``run_task_dag`` loop.  Leader callers must provide the exact node they
    selected from the current READY set.
    """

    dag_id: str
    selected_node_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.dag_id, str) or not self.dag_id.strip():
            raise ValueError("task DAG step request id must not be empty")
        if self.selected_node_id is not None and (
            not isinstance(self.selected_node_id, str) or not self.selected_node_id.strip()
        ):
            raise ValueError("task DAG selected node id must not be empty")


class TaskDagActiveNodeRecovery(StrEnum):
    """Read-only classification of an already-claimed active DAG node."""

    ACTIVE_WORKER = "active_worker"
    RECOVERY_OWNED = "recovery_owned"
    SAFE_NOT_STARTED = "safe_not_started"
    INDETERMINATE = "indeterminate"


@runtime_checkable
class TaskDagWritableService(Protocol):
    """Existing writable-subagent owner required by DAG orchestration."""

    @property
    def parent_session_id(self) -> str: ...

    async def initialize(self) -> None: ...

    async def run_subagent_with_execution_identity(
        self,
        request: RunWritableSubagentRequest,
        *,
        execution_identity: WritableSubagentExecutionIdentity,
        sink: EventSink | None = None,
    ) -> WritableSubagentResultProjection: ...

    async def reconcile_writable_subagent_workspaces(self) -> object: ...


@runtime_checkable
class TaskDagWritableWorkerFactory(Protocol):
    """Create an independent Writable owner for one parallel DAG node."""

    def create(self) -> TaskDagWritableService: ...


class TaskDagApplicationService:
    """Own DAG orchestration while delegating every node to Writable Subagent."""

    def __init__(
        self,
        store: SessionStore,
        dag_store: TaskDagStore,
        writable_service: TaskDagWritableService,
        lease_store: WritableSubagentLeaseStore,
        relay_store: ParentContextRelayStore,
        *,
        parent_binding: ConversationBinding,
        dependency_relay_store: TaskDagDependencyResultRelayStore | None = None,
        recovery_claim_store: TaskDagRecoveryClaimStore | None = None,
        writable_worker_factory: TaskDagWritableWorkerFactory | None = None,
        redaction_values: tuple[str, ...] = (),
        clock: Callable[[], datetime] = _now,
    ) -> None:
        from neuro_code.application.sessions.binding import (
            ConversationBinding as CanonicalConversationBinding,
        )

        if not isinstance(parent_binding, CanonicalConversationBinding):
            raise ConfigurationError("task DAG parent binding is required")
        if not isinstance(writable_service, TaskDagWritableService):
            raise ConfigurationError("task DAG writable service is invalid")
        parent_session_id = parent_binding.runner.session_id
        if not isinstance(parent_session_id, str) or not parent_session_id.strip():
            raise ConfigurationError("task DAG parent session identity is missing")
        if writable_service.parent_session_id != parent_session_id:
            raise ConfigurationError("task DAG writable service parent does not match binding")
        if writable_worker_factory is not None and not isinstance(
            writable_worker_factory,
            TaskDagWritableWorkerFactory,
        ):
            raise ConfigurationError("task DAG writable worker factory is invalid")
        self._store = store
        self._dag_store = dag_store
        self._writable_service = writable_service
        self._writable_worker_factory = writable_worker_factory
        self._lease_store = lease_store
        self._relay_store = relay_store
        self._parent_binding = parent_binding
        self._parent_session_id = parent_session_id
        if recovery_claim_store is None and all(
            callable(getattr(dag_store, method, None))
            for method in (
                "get_task_dag_recovery_claim",
                "insert_task_dag_recovery_claim",
                "compare_and_takeover_task_dag_recovery_claim",
            )
        ):
            recovery_claim_store = cast(TaskDagRecoveryClaimStore, dag_store)
        self._recovery_claim_store = recovery_claim_store
        self._execution_owner_pid = os.getpid()
        self._execution_owner_token = f"dag-worker-owner-{uuid.uuid4().hex}"
        self._recovery_owner_pid = os.getpid()
        self._recovery_owner_token = f"dag-recovery-owner-{uuid.uuid4().hex}"
        self._clock = clock
        self._dependency_relay_service = (
            TaskDagDependencyResultRelayApplicationService(
                dag_store,
                dependency_relay_store,
                lease_store,
                relay_store,
                parent_session_id=parent_session_id,
                redaction_values=redaction_values,
                clock=clock,
            )
            if dependency_relay_store is not None
            else None
        )

    async def create_task_dag(self, request: CreateTaskDagRequest) -> TaskDag:
        if not isinstance(request, CreateTaskDagRequest):
            raise ValueError("task DAG creation request must be canonical")
        dag = TaskDag.create(
            dag_id=request.dag_id,
            parent_session_id=self._parent_session_id,
            nodes=request.nodes,
            created_at=self._clock().astimezone(UTC),
            max_parallel=request.max_parallel,
        )
        try:
            return await self._dag_store.insert_task_dag(dag)
        except TaskDagError as error:
            raise ConfigurationError(f"task DAG publication failed: {error}") from error

    async def get_task_dag(self, request: RunTaskDagRequest) -> TaskDag | None:
        if not isinstance(request, RunTaskDagRequest):
            raise ValueError("task DAG query request must be canonical")
        dag = await self._dag_store.get_task_dag(request.dag_id)
        self._verify_parent(dag)
        return dag

    async def run_task_dag(
        self,
        request: RunTaskDagRequest,
        *,
        sink: EventSink | None = None,
    ) -> TaskDag:
        if not isinstance(request, RunTaskDagRequest):
            raise ValueError("task DAG run request must be canonical")
        while True:
            dag, recoveries = await self._prepare_task_dag_for_run(request)
            if dag.state.terminal:
                return dag

            safe_nodes = tuple(
                node_id
                for node_id in dag.running_node_ids
                if recoveries.get(node_id) is TaskDagActiveNodeRecovery.SAFE_NOT_STARTED
            )
            if safe_nodes:
                node = dag.node(safe_nodes[0])
                if node.parent_task_id is None:
                    return await self._finish_worker_node(
                        dag,
                        node,
                        TaskDagNodeState.INDETERMINATE,
                        error=RuntimeError(
                            "safe-not-started DAG node lost its persisted worker identity"
                        ),
                    )
                if await self._acquire_recovery_ownership(dag, node):
                    worker = self._worker_service_for(dag)
                    await self._execute_claimed_node(
                        dag=dag,
                        claimed=dag,
                        claimed_node=node,
                        node=node,
                        parent_task_id=node.parent_task_id,
                        sink=sink,
                        reuse_existing_dependency_relay=True,
                        writable_service=worker,
                    )
                    continue
                dag = await self._load_required(dag.dag_id)

            available_slots = max(0, dag.max_parallel - len(dag.running_node_ids))
            ready_node_ids = dag.ready_node_ids()[:available_slots]
            if ready_node_ids:
                if dag.max_parallel > 1 and self._writable_worker_factory is None:
                    raise ConfigurationError(
                        "parallel task DAG requires an independent writable worker factory"
                    )
                workers = [self._worker_service_for(dag) for _ in ready_node_ids]
                if len({id(worker) for worker in workers}) != len(workers):
                    raise ConfigurationError(
                        "parallel task DAG worker factory returned a shared writable service"
                    )
                claimed_dag, claims = await self._claim_ready_nodes(dag, ready_node_ids)
                if claims:
                    async with asyncio.TaskGroup() as task_group:
                        for index, claim in enumerate(claims):
                            task_group.create_task(
                                self._execute_claimed_node(
                                    dag=dag,
                                    claimed=claim[0],
                                    claimed_node=claim[1],
                                    node=claim[2],
                                    parent_task_id=claim[3],
                                    sink=sink,
                                    writable_service=workers[index],
                                )
                            )
                    continue
                dag = claimed_dag
                if dag.ready_node_ids():
                    continue

            if dag.running_node_ids:
                return dag
            return await self._classify_terminal_or_uncertain(dag)

    async def run_task_dag_step(
        self,
        request: RunTaskDagStepRequest,
        *,
        sink: EventSink | None = None,
    ) -> TaskDag:
        """Reconcile and execute at most one legal DAG node.

        The explicit selected-node form is the only orchestration seam exposed
        to Leader.  Claiming, dependency propagation, worker execution, and
        terminal persistence remain owned by the existing Task DAG service.
        """

        if not isinstance(request, RunTaskDagStepRequest):
            raise ValueError("task DAG step request must be canonical")
        dag, recovery = await self._prepare_task_dag_step_with_recovery(
            RunTaskDagRequest(request.dag_id)
        )
        if dag.state.terminal:
            if request.selected_node_id is not None:
                raise ConfigurationError("cannot select a node from a terminal task DAG")
            return dag
        if dag.running_node_ids:
            if request.selected_node_id is not None:
                raise ConfigurationError("task DAG already has a running node")
            if recovery is TaskDagActiveNodeRecovery.SAFE_NOT_STARTED:
                node = dag.node(dag.running_node_ids[0])
                if node.parent_task_id is None:
                    return await self._finish_worker_node(
                        dag,
                        node,
                        TaskDagNodeState.INDETERMINATE,
                        error=RuntimeError(
                            "safe-not-started DAG node lost its persisted worker identity"
                        ),
                    )
                if not await self._acquire_recovery_ownership(dag, node):
                    return await self._load_required(dag.dag_id)
                return await self._execute_claimed_node(
                    dag=dag,
                    claimed=dag,
                    claimed_node=node,
                    node=node,
                    parent_task_id=node.parent_task_id,
                    sink=sink,
                    reuse_existing_dependency_relay=True,
                    writable_service=self._worker_service_for(dag),
                )
            return dag
        ready_node_ids = dag.ready_node_ids()
        if not ready_node_ids:
            if request.selected_node_id is not None:
                raise ConfigurationError("selected task DAG node is not currently READY")
            return await self._classify_terminal_or_uncertain(dag)
        selected_node_id = request.selected_node_id or ready_node_ids[0]
        if selected_node_id not in ready_node_ids:
            raise ConfigurationError("selected task DAG node is not currently READY")
        node = dag.node(selected_node_id)
        parent_task_id = f"dag-worker-{uuid.uuid4().hex}"
        running = replace(
            node,
            state=TaskDagNodeState.RUNNING,
            generation=node.generation + 1,
            parent_task_id=parent_task_id,
            error_kind=None,
            error_reason=None,
        )
        try:
            claimed = await self._dag_store.claim_task_dag_node(
                dag.dag_id,
                running,
                expected_generation=node.generation,
                expected_state=TaskDagNodeState.READY,
                updated_at=self._clock().astimezone(UTC),
            )
        except TaskDagError as error:
            if error.kind == "concurrent_modification":
                if request.selected_node_id is not None:
                    raise ConfigurationError("selected task DAG node became stale") from error
                return await self._load_required(dag.dag_id)
            raise ConfigurationError(f"task DAG node claim failed: {error}") from error
        return await self._execute_claimed_node(
            dag=dag,
            claimed=claimed,
            claimed_node=claimed.node(node.node_id),
            node=node,
            parent_task_id=parent_task_id,
            sink=sink,
        )

    async def prepare_task_dag_step(self, request: RunTaskDagRequest) -> TaskDag:
        """Reconcile and propagate one DAG snapshot without starting a worker."""

        dag, _ = await self._prepare_task_dag_step_with_recovery(request)
        return dag

    async def _prepare_task_dag_step_with_recovery(
        self,
        request: RunTaskDagRequest,
    ) -> tuple[TaskDag, TaskDagActiveNodeRecovery | None]:
        """Prepare one serialized step and retain its recovery classification."""

        if not isinstance(request, RunTaskDagRequest):
            raise ValueError("task DAG preparation request must be canonical")
        dag, recoveries = await self._prepare_task_dag_for_run(request)
        recovery = recoveries.get(dag.running_node_ids[0]) if dag.running_node_ids else None
        return dag, recovery

    async def _prepare_task_dag_for_run(
        self,
        request: RunTaskDagRequest,
    ) -> tuple[TaskDag, dict[str, TaskDagActiveNodeRecovery | None]]:
        """Reconcile every running node and prepare deterministic scheduling."""

        if not isinstance(request, RunTaskDagRequest):
            raise ValueError("task DAG preparation request must be canonical")
        await self._writable_service.initialize()
        if self._dependency_relay_service is not None:
            await self._dependency_relay_service.initialize()
        dag = await self._load_required(request.dag_id)
        if dag.state.terminal:
            return dag, {}
        dag, recoveries = await self._reconcile_active_nodes_with_classification(dag)
        if dag.state.terminal:
            return dag, recoveries
        dag = await self._propagate_dependencies(dag)
        if dag.state.terminal:
            return dag, recoveries
        if not dag.running_node_ids and any(
            node.state is TaskDagNodeState.INDETERMINATE for node in dag.nodes
        ):
            dag = await self._set_graph_state_if_needed(dag, TaskDagState.INDETERMINATE)
            return dag, recoveries
        if not dag.running_node_ids and not dag.ready_node_ids():
            dag = await self._classify_terminal_or_uncertain(dag)
        return dag, recoveries

    def _worker_service_for(self, dag: TaskDag) -> TaskDagWritableService:
        """Select one owner without weakening the per-service Writable lock."""

        if dag.max_parallel == 1:
            return self._writable_service
        if self._writable_worker_factory is None:
            raise ConfigurationError(
                "parallel task DAG requires an independent writable worker factory"
            )
        worker = self._writable_worker_factory.create()
        if not isinstance(worker, TaskDagWritableService):
            raise ConfigurationError("parallel task DAG worker factory returned an invalid service")
        if worker.parent_session_id != self._parent_session_id:
            raise ConfigurationError("parallel task DAG worker parent does not match binding")
        return worker

    async def _claim_ready_nodes(
        self,
        dag: TaskDag,
        node_ids: tuple[str, ...],
    ) -> tuple[
        TaskDag,
        tuple[tuple[TaskDag, TaskDagNode, TaskDagNode, str], ...],
    ]:
        """Claim a deterministic batch; SQLite owns the durable capacity race."""

        current = dag
        claims: list[tuple[TaskDag, TaskDagNode, TaskDagNode, str]] = []
        for node_id in node_ids:
            node = current.node(node_id)
            if node.state is not TaskDagNodeState.READY:
                continue
            parent_task_id = f"dag-worker-{uuid.uuid4().hex}"
            running = replace(
                node,
                state=TaskDagNodeState.RUNNING,
                generation=node.generation + 1,
                parent_task_id=parent_task_id,
                execution_owner_pid=self._execution_owner_pid,
                execution_owner_token=self._execution_owner_token,
                error_kind=None,
                error_reason=None,
            )
            try:
                claimed = await self._dag_store.claim_task_dag_node(
                    current.dag_id,
                    running,
                    expected_generation=node.generation,
                    expected_state=TaskDagNodeState.READY,
                    updated_at=self._clock().astimezone(UTC),
                )
            except TaskDagError as error:
                if error.kind == "concurrent_modification":
                    current = await self._load_required(current.dag_id)
                    continue
                raise ConfigurationError(f"task DAG node claim failed: {error}") from error
            claimed_node = claimed.node(node_id)
            claims.append((claimed, claimed_node, node, parent_task_id))
            current = claimed
        return current, tuple(claims)

    async def _execute_claimed_node(
        self,
        *,
        dag: TaskDag,
        claimed: TaskDag,
        claimed_node: TaskDagNode,
        node: TaskDagNode,
        parent_task_id: str,
        sink: EventSink | None,
        reuse_existing_dependency_relay: bool = False,
        writable_service: TaskDagWritableService | None = None,
    ) -> TaskDag:
        worker = writable_service or self._writable_service
        dependency_relay = None
        if node.dependencies and self._dependency_relay_service is not None:
            # The target is already claimed RUNNING here.  Publication is the
            # last durable step before Writable Subagent may create a child
            # runtime or issue its first model request.
            try:
                if reuse_existing_dependency_relay:
                    dependency_relay = (
                        await self._dependency_relay_service.load_existing_for_target(
                            claimed,
                            claimed_node,
                        )
                    )
                    if dependency_relay is None:
                        raise ConfigurationError(
                            "safe-not-started DAG node has no existing dependency relay"
                        )
                else:
                    dependency_relay = await self._dependency_relay_service.publish_for_target(
                        claimed,
                        claimed_node,
                    )
            except asyncio.CancelledError as error:
                await asyncio.shield(
                    self._finish_worker_node(
                        claimed,
                        claimed_node,
                        TaskDagNodeState.CANCELLED,
                        error=error,
                    )
                )
                await asyncio.shield(self._cancel_remaining_graph(dag.dag_id))
                raise
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                await self._finish_worker_node(
                    claimed,
                    claimed_node,
                    TaskDagNodeState.INDETERMINATE,
                    error=error,
                )
                return await self._load_required(claimed.dag_id)
        identity = WritableSubagentExecutionIdentity(
            dag_id=dag.dag_id,
            node_id=node.node_id,
            parent_task_id=parent_task_id,
        )
        try:
            await worker.initialize()
            result = await worker.run_subagent_with_execution_identity(
                RunWritableSubagentRequest(
                    parent_session_id=self._parent_session_id,
                    prompt=node.prompt,
                    dependency_result_relay=dependency_relay,
                ),
                execution_identity=identity,
                sink=sink,
            )
        except asyncio.CancelledError as error:
            await asyncio.shield(
                self._finish_worker_node(
                    claimed,
                    claimed_node,
                    TaskDagNodeState.CANCELLED,
                    error=error,
                )
            )
            await asyncio.shield(self._cancel_remaining_graph(dag.dag_id))
            raise
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            await self._finish_worker_node(
                claimed,
                claimed_node,
                TaskDagNodeState.FAILED,
                error=error,
            )
        else:
            terminal_state = (
                TaskDagNodeState.COMPLETED
                if result.status is SessionTaskStatus.COMPLETED
                else TaskDagNodeState.FAILED
            )
            await self._finish_worker_node(
                claimed,
                claimed_node,
                terminal_state,
                result=result,
                error=(
                    None
                    if terminal_state is TaskDagNodeState.COMPLETED
                    else RuntimeError("writable worker returned a non-completed result")
                ),
            )
        return await self._load_required(dag.dag_id)

    async def _acquire_recovery_ownership(
        self,
        dag: TaskDag,
        node: TaskDagNode,
    ) -> bool:
        """Fence concurrent SAFE_NOT_STARTED controllers before Writable.

        The method deliberately owns no DAG generation.  The durable claim is
        a separate exact-identity row, and a dead owner may be replaced only
        through its versioned CAS.  A live or unproven owner is observed and
        yields the canonical current DAG without allocating a worker.
        """

        if self._recovery_claim_store is None or self._dependency_relay_service is None:
            await self._finish_worker_node(
                dag,
                node,
                TaskDagNodeState.INDETERMINATE,
                error=RuntimeError("safe-not-started DAG recovery ownership is unavailable"),
            )
            return False
        for _ in range(4):
            current = await self._load_required(dag.dag_id)
            current_node = current.node(node.node_id)
            if (
                current_node.state is not TaskDagNodeState.RUNNING
                or current_node.generation != node.generation
                or current_node.parent_task_id != node.parent_task_id
            ):
                return False
            task = await self._store.get_session_task(
                self._parent_session_id,
                node.parent_task_id or "",
            )
            lease = await self._lease_store.get_writable_subagent_lease_for_parent_task(
                self._parent_session_id,
                node.parent_task_id or "",
            )
            link = await self._store.load_subagent_link(
                self._parent_session_id,
                node.parent_task_id or "",
            )
            if task is not None or lease is not None or link is not None:
                return False
            relay = await self._dependency_relay_service.load_existing_for_target(
                current,
                current_node,
            )
            if relay is None:
                await self._finish_worker_node(
                    current,
                    current_node,
                    TaskDagNodeState.INDETERMINATE,
                    error=RuntimeError("safe-not-started DAG node has no exact dependency relay"),
                )
                return False
            proposed = self._new_recovery_claim(current, current_node, relay)
            try:
                result = await self._recovery_claim_store.insert_task_dag_recovery_claim(proposed)
            except TaskDagRecoveryClaimError as error:
                if error.kind == "concurrent_modification":
                    continue
                raise ConfigurationError(f"DAG recovery ownership failed: {error}") from error
            if result.acquired:
                return await self._revalidate_recovery_state(current, current_node, relay)
            existing = result.claim
            if not existing.same_execution(proposed):
                await self._finish_worker_node(
                    current,
                    current_node,
                    TaskDagNodeState.INDETERMINATE,
                    error=RuntimeError("DAG recovery claim identity is inconsistent"),
                )
                return False
            if owner_is_alive(existing.owner_pid):
                return False
            takeover = existing.with_owner(
                owner_pid=self._recovery_owner_pid,
                owner_token=self._recovery_owner_token,
                version=existing.version + 1,
                updated_at=self._clock().astimezone(UTC),
            )
            try:
                await self._recovery_claim_store.compare_and_takeover_task_dag_recovery_claim(
                    takeover,
                    expected_version=existing.version,
                    expected_owner_pid=existing.owner_pid,
                    expected_owner_token=existing.owner_token,
                )
            except TaskDagRecoveryClaimError as error:
                if error.kind == "concurrent_modification":
                    continue
                raise ConfigurationError(f"DAG recovery takeover failed: {error}") from error
            return await self._revalidate_recovery_state(current, current_node, relay)
        return False

    def _new_recovery_claim(
        self,
        dag: TaskDag,
        node: TaskDagNode,
        relay: object,
    ) -> TaskDagRecoveryClaim:
        if not isinstance(relay, TaskDagDependencyResultRelay):
            raise ConfigurationError("DAG recovery dependency relay is invalid")
        return TaskDagRecoveryClaim.create(
            parent_session_id=self._parent_session_id,
            dag_id=dag.dag_id,
            dag_definition_fingerprint=dag.definition_fingerprint,
            node_id=node.node_id,
            node_generation=node.generation,
            node_definition_fingerprint=node.definition_fingerprint,
            parent_task_id=node.parent_task_id or "",
            dependency_relay_id=relay.relay_id,
            dependency_relay_source_fingerprint=relay.source_fingerprint,
            dependency_relay_content_fingerprint=relay.content_fingerprint,
            dependency_relay_integrity_fingerprint=relay.integrity_fingerprint,
            owner_pid=self._recovery_owner_pid,
            owner_token=self._recovery_owner_token,
            created_at=self._clock().astimezone(UTC),
        )

    async def _revalidate_recovery_state(
        self,
        dag: TaskDag,
        node: TaskDagNode,
        relay: TaskDagDependencyResultRelay,
    ) -> bool:
        """Reconfirm the exact no-allocation state after claiming the fence."""

        current = await self._load_required(dag.dag_id)
        if (
            current.node(node.node_id).state is not TaskDagNodeState.RUNNING
            or current.node(node.node_id).generation != node.generation
        ):
            return False
        task = await self._store.get_session_task(
            self._parent_session_id,
            node.parent_task_id or "",
        )
        lease = await self._lease_store.get_writable_subagent_lease_for_parent_task(
            self._parent_session_id,
            node.parent_task_id or "",
        )
        link = await self._store.load_subagent_link(
            self._parent_session_id,
            node.parent_task_id or "",
        )
        if task is not None or lease is not None or link is not None:
            return False
        exact_relay = (
            await self._dependency_relay_service.load_existing_for_target(
                current,
                current.node(node.node_id),
            )
            if self._dependency_relay_service is not None
            else None
        )
        return exact_relay == relay

    async def reconcile_task_dag(self, request: RunTaskDagRequest) -> TaskDag:
        """Reconcile durable evidence without starting any worker."""

        if not isinstance(request, RunTaskDagRequest):
            raise ValueError("task DAG reconciliation request must be canonical")
        dag = await self._load_required(request.dag_id)
        dag, _ = await self._reconcile_active_nodes_with_classification(dag)
        return dag

    async def _load_required(self, dag_id: str) -> TaskDag:
        dag = await self._dag_store.get_task_dag(dag_id)
        self._verify_parent(dag)
        if dag is None:
            raise ConfigurationError(f"unknown task DAG: {dag_id}")
        return dag

    def _verify_parent(self, dag: TaskDag | None) -> None:
        if dag is not None and dag.parent_session_id != self._parent_session_id:
            raise ConfigurationError("task DAG parent does not match the actual binding")

    async def _propagate_dependencies(self, dag: TaskDag) -> TaskDag:
        changed = True
        while changed:
            changed = False
            states = dag.node_states()
            for node_id in dag.topological_order():
                node = dag.node(node_id)
                if node.state is not TaskDagNodeState.PENDING:
                    continue
                dependency_states = tuple(states[dependency] for dependency in node.dependencies)
                blocked_reason: str | None = None
                if any(state is TaskDagNodeState.INDETERMINATE for state in dependency_states):
                    blocked_reason = "dependency_indeterminate"
                elif any(state is TaskDagNodeState.CANCELLED for state in dependency_states):
                    blocked_reason = "dependency_cancelled"
                elif any(
                    state in {TaskDagNodeState.FAILED, TaskDagNodeState.SKIPPED}
                    for state in dependency_states
                ):
                    blocked_reason = "dependency_failed"
                elif all(state is TaskDagNodeState.COMPLETED for state in dependency_states):
                    proposed = replace(
                        node,
                        state=TaskDagNodeState.READY,
                        generation=node.generation + 1,
                    )
                else:
                    continue
                if blocked_reason is not None:
                    proposed = replace(
                        node,
                        state=TaskDagNodeState.SKIPPED,
                        generation=node.generation + 1,
                        error_kind="dependency_blocked",
                        error_reason=blocked_reason,
                    )
                try:
                    dag = await self._dag_store.compare_and_transition_task_dag_node(
                        dag.dag_id,
                        proposed,
                        expected_generation=node.generation,
                        expected_state=node.state,
                    )
                except TaskDagError as error:
                    if error.kind == "concurrent_modification":
                        return await self._load_required(dag.dag_id)
                    raise ConfigurationError(
                        f"task DAG dependency propagation failed: {error}"
                    ) from error
                changed = True
                states = dag.node_states()
        return dag

    async def _finish_worker_node(
        self,
        claimed: TaskDag,
        claimed_node: TaskDagNode,
        state: TaskDagNodeState,
        *,
        result: WritableSubagentResultProjection | None = None,
        error: BaseException | None = None,
    ) -> TaskDag:
        lease = await self._lease_store.get_writable_subagent_lease_for_parent_task(
            self._parent_session_id,
            claimed_node.parent_task_id or "",
        )
        relay_id: str | None = None
        if lease is not None:
            relay = await self._relay_store.get_parent_context_relay_for_lease(lease.lease_id)
            relay_id = relay.relay_id if relay is not None else None
        effective_state = state
        effective_error = error
        if state is TaskDagNodeState.COMPLETED and (lease is None or relay_id is None):
            effective_state = TaskDagNodeState.INDETERMINATE
            effective_error = RuntimeError("completed DAG worker evidence is incomplete")
        proposed = replace(
            claimed_node,
            state=effective_state,
            generation=claimed_node.generation + 1,
            child_session_id=(lease.child_session_id if lease is not None else None),
            lease_id=(lease.lease_id if lease is not None else None),
            worktree_id=(lease.worktree_id.value if lease is not None else None),
            baseline_checkpoint_id=(
                lease.baseline_checkpoint_id.value
                if lease is not None and lease.baseline_checkpoint_id is not None
                else None
            ),
            relay_id=relay_id,
            response_preview=(
                _bounded_metadata_text(result.response, MAX_TASK_DAG_RESPONSE_PREVIEW_BYTES)
                if result is not None
                else None
            ),
            final_workspace_fingerprint=(
                lease.final_workspace_fingerprint if lease is not None else None
            ),
            changed_file_count=(lease.changed_file_count if lease is not None else None),
            error_kind=(type(effective_error).__name__ if effective_error is not None else None),
            error_reason=(
                _bounded_metadata_text(str(effective_error), MAX_TASK_DAG_ERROR_BYTES)
                if effective_error is not None
                else None
            ),
        )
        try:
            return await self._dag_store.finish_task_dag_node(
                claimed.dag_id,
                proposed,
                expected_generation=claimed_node.generation,
                expected_state=TaskDagNodeState.RUNNING,
                updated_at=self._clock().astimezone(UTC),
            )
        except TaskDagError as error:
            if error.kind == "concurrent_modification":
                return await self._load_required(claimed.dag_id)
            raise ConfigurationError(f"task DAG node finish failed: {error}") from error

    async def _reconcile_active_node(self, dag: TaskDag) -> TaskDag:
        reconciled, _ = await self._reconcile_active_node_with_classification(dag)
        return reconciled

    async def _reconcile_active_nodes_with_classification(
        self,
        dag: TaskDag,
    ) -> tuple[TaskDag, dict[str, TaskDagActiveNodeRecovery | None]]:
        """Reconcile each durable RUNNING node without using the legacy scalar."""

        current = dag
        classifications: dict[str, TaskDagActiveNodeRecovery | None] = {}
        for node_id in dag.running_node_ids:
            current = await self._load_required(dag.dag_id)
            node = current.node(node_id)
            if node.state is not TaskDagNodeState.RUNNING:
                continue
            current, classification = await self._reconcile_one_active_node_with_classification(
                current,
                node,
            )
            classifications[node_id] = classification
        return current, classifications

    async def _load_matching_recovery_claim(
        self,
        dag: TaskDag,
        node: TaskDagNode,
    ) -> TaskDagRecoveryClaim | None:
        if (
            self._recovery_claim_store is None
            or self._dependency_relay_service is None
            or not node.dependencies
        ):
            return None
        relay = await self._dependency_relay_service.load_existing_for_target(dag, node)
        if relay is None:
            return None
        claim = await self._recovery_claim_store.get_task_dag_recovery_claim(
            dag.dag_id,
            node.node_id,
            node.generation,
        )
        if claim is None:
            return None
        expected = self._new_recovery_claim(dag, node, relay)
        if not claim.same_execution(expected):
            raise RuntimeError("DAG recovery claim identity is inconsistent")
        return claim

    async def _reconcile_active_node_with_classification(
        self,
        dag: TaskDag,
    ) -> tuple[TaskDag, TaskDagActiveNodeRecovery | None]:
        if not dag.running_node_ids:
            return dag, None
        node_id = dag.running_node_ids[0]
        current = dag
        node = current.node(node_id)
        if node.state is not TaskDagNodeState.RUNNING:
            return current, None
        return await self._reconcile_one_active_node_with_classification(current, node)

    async def _reconcile_one_active_node_with_classification(
        self,
        dag: TaskDag,
        node: TaskDagNode,
    ) -> tuple[TaskDag, TaskDagActiveNodeRecovery | None]:
        if node.state is not TaskDagNodeState.RUNNING:
            return dag, None
        if node.parent_task_id is None:
            return (
                await self._finish_worker_node(
                    dag,
                    node,
                    TaskDagNodeState.INDETERMINATE,
                    error=RuntimeError("running DAG node has no persisted worker identity"),
                ),
                TaskDagActiveNodeRecovery.INDETERMINATE,
            )
        if node.execution_owner_pid is not None and owner_is_alive(node.execution_owner_pid):
            return dag, TaskDagActiveNodeRecovery.ACTIVE_WORKER
        task = None
        lease = None
        for probe in range(_ACTIVE_EVIDENCE_PROBE_COUNT):
            task = await self._store.get_session_task(self._parent_session_id, node.parent_task_id)
            lease = await self._lease_store.get_writable_subagent_lease_for_parent_task(
                self._parent_session_id,
                node.parent_task_id,
            )
            if task is not None or lease is not None:
                break
            if probe + 1 < _ACTIVE_EVIDENCE_PROBE_COUNT:
                await asyncio.sleep(_ACTIVE_EVIDENCE_PROBE_DELAY_SECONDS)
        recovery_error: BaseException | None = None
        recovery_claim: TaskDagRecoveryClaim | None = None
        if task is None and node.dependencies and self._dependency_relay_service is not None:
            try:
                existing_relay = await self._dependency_relay_service.load_existing_for_target(
                    dag, node
                )
                if existing_relay is not None:
                    link = await self._store.load_subagent_link(
                        self._parent_session_id,
                        node.parent_task_id,
                    )
                    if link is None:
                        recovery_claim = await self._load_matching_recovery_claim(dag, node)
                        if recovery_claim is not None and owner_is_alive(recovery_claim.owner_pid):
                            return dag, TaskDagActiveNodeRecovery.RECOVERY_OWNED
                        if lease is None:
                            return dag, TaskDagActiveNodeRecovery.SAFE_NOT_STARTED
                    else:
                        recovery_error = RuntimeError(
                            "DAG worker link exists despite missing task and lease evidence"
                        )
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                recovery_error = error
        if task is None and lease is None:
            return (
                await self._finish_worker_node(
                    dag,
                    node,
                    TaskDagNodeState.INDETERMINATE,
                    error=(
                        recovery_error
                        if recovery_error is not None
                        else RuntimeError("DAG worker evidence is incomplete after restart")
                    ),
                ),
                TaskDagActiveNodeRecovery.INDETERMINATE,
            )
        if (
            task is None
            and lease is not None
            and recovery_claim is not None
            and owner_is_alive(recovery_claim.owner_pid)
        ):
            return dag, TaskDagActiveNodeRecovery.RECOVERY_OWNED
        lease_owner_pid = getattr(lease, "owner_pid", None) if lease is not None else None
        if (
            lease is not None
            and lease.state is not WritableSubagentWorkspaceState.ORPHANED
            and lease_owner_pid is not None
            and owner_is_alive(lease_owner_pid)
        ):
            return dag, TaskDagActiveNodeRecovery.ACTIVE_WORKER
        await self._writable_service.reconcile_writable_subagent_workspaces()
        task = await self._store.get_session_task(self._parent_session_id, node.parent_task_id)
        lease = await self._lease_store.get_writable_subagent_lease_for_parent_task(
            self._parent_session_id,
            node.parent_task_id,
        )
        if task is None or lease is None:
            return (
                await self._finish_worker_node(
                    dag,
                    node,
                    TaskDagNodeState.INDETERMINATE,
                    error=RuntimeError("DAG worker evidence is incomplete after restart"),
                ),
                TaskDagActiveNodeRecovery.INDETERMINATE,
            )
        if (
            lease.parent_session_id != self._parent_session_id
            or lease.parent_task_id != node.parent_task_id
            or lease.state is WritableSubagentWorkspaceState.ORPHANED
        ):
            return (
                await self._finish_worker_node(
                    dag,
                    node,
                    TaskDagNodeState.INDETERMINATE,
                    error=RuntimeError("DAG worker ownership evidence is indeterminate"),
                ),
                TaskDagActiveNodeRecovery.INDETERMINATE,
            )
        if task.status is SessionTaskStatus.COMPLETED:
            if lease.state is not WritableSubagentWorkspaceState.PRESERVED:
                return (
                    await self._finish_worker_node(
                        dag,
                        node,
                        TaskDagNodeState.INDETERMINATE,
                        error=RuntimeError("completed DAG worker workspace is not preserved"),
                    ),
                    TaskDagActiveNodeRecovery.INDETERMINATE,
                )
            return (
                await self._finish_worker_node(dag, node, TaskDagNodeState.COMPLETED),
                None,
            )
        if task.status is SessionTaskStatus.FAILED:
            return (
                await self._finish_worker_node(
                    dag,
                    node,
                    TaskDagNodeState.FAILED,
                    error=RuntimeError("reconciled writable SessionTask failure"),
                ),
                None,
            )
        if task.status is SessionTaskStatus.CANCELLED:
            return (
                await self._finish_worker_node(
                    dag,
                    node,
                    TaskDagNodeState.CANCELLED,
                    error=RuntimeError("reconciled writable SessionTask cancellation"),
                ),
                None,
            )
        return dag, TaskDagActiveNodeRecovery.ACTIVE_WORKER

    async def _classify_terminal_or_uncertain(self, dag: TaskDag) -> TaskDag:
        if dag.running_node_ids:
            return dag
        if any(node.state is TaskDagNodeState.INDETERMINATE for node in dag.nodes):
            return await self._set_graph_state_if_needed(dag, TaskDagState.INDETERMINATE)
        if any(
            node.state in {TaskDagNodeState.PENDING, TaskDagNodeState.READY} for node in dag.nodes
        ):
            return await self._set_graph_state_if_needed(dag, TaskDagState.INDETERMINATE)
        if all(node.state is TaskDagNodeState.COMPLETED for node in dag.nodes):
            return await self._set_graph_state_if_needed(dag, TaskDagState.COMPLETED)
        if any(node.state is TaskDagNodeState.CANCELLED for node in dag.nodes):
            return await self._set_graph_state_if_needed(dag, TaskDagState.CANCELLED)
        return await self._set_graph_state_if_needed(dag, TaskDagState.FAILED)

    async def _set_graph_state_if_needed(
        self,
        dag: TaskDag,
        state: TaskDagState,
    ) -> TaskDag:
        if dag.running_node_ids and state.terminal:
            return dag
        if dag.state is state:
            return dag
        proposed = replace(
            dag,
            state=state,
            generation=dag.generation + 1,
            updated_at=self._clock().astimezone(UTC),
        )
        try:
            return await self._dag_store.compare_and_transition_task_dag(
                proposed,
                expected_generation=dag.generation,
                expected_state=dag.state,
            )
        except TaskDagError as error:
            if error.kind == "concurrent_modification":
                return await self._load_required(dag.dag_id)
            raise ConfigurationError(f"task DAG state transition failed: {error}") from error

    async def _cancel_remaining_graph(self, dag_id: str) -> TaskDag:
        dag = await self._load_required(dag_id)
        for node in dag.nodes:
            if node.state not in {TaskDagNodeState.PENDING, TaskDagNodeState.READY}:
                continue
            proposed = replace(
                node,
                state=TaskDagNodeState.CANCELLED,
                generation=node.generation + 1,
                error_kind="dag_cancelled",
                error_reason="DAG execution was cancelled",
            )
            try:
                dag = await self._dag_store.compare_and_transition_task_dag_node(
                    dag.dag_id,
                    proposed,
                    expected_generation=node.generation,
                    expected_state=node.state,
                )
            except TaskDagError as error:
                if error.kind == "concurrent_modification":
                    dag = await self._load_required(dag.dag_id)
                    continue
                raise ConfigurationError(f"task DAG cancellation failed: {error}") from error
        return await self._set_graph_state_if_needed(dag, TaskDagState.CANCELLED)


__all__ = [
    "CreateTaskDagRequest",
    "RunTaskDagRequest",
    "RunTaskDagStepRequest",
    "TaskDagActiveNodeRecovery",
    "TaskDagApplicationService",
    "TaskDagWritableService",
    "TaskDagWritableWorkerFactory",
]
