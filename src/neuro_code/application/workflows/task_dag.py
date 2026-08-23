"""Deterministic serialized execution of explicit durable task DAGs."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from neuro_code.application.ports.parent_context_relay import ParentContextRelayStore
from neuro_code.application.ports.task_dag import TaskDagError, TaskDagStore
from neuro_code.application.ports.writable_subagent import WritableSubagentLeaseStore
from neuro_code.application.runtime.agent import EventSink
from neuro_code.application.workflows.writable_subagent import (
    RunWritableSubagentRequest,
    WritableSubagentExecutionIdentity,
    WritableSubagentResultProjection,
)
from neuro_code.domain.session_tasks import SessionTaskStatus
from neuro_code.domain.task_dag import (
    MAX_TASK_DAG_ERROR_BYTES,
    MAX_TASK_DAG_RESPONSE_PREVIEW_BYTES,
    TaskDag,
    TaskDagNode,
    TaskDagNodeState,
    TaskDagState,
)
from neuro_code.domain.writable_subagent import WritableSubagentWorkspaceState
from neuro_code.shared.errors import ConfigurationError

if TYPE_CHECKING:
    from neuro_code.application.ports.storage import SessionStore
    from neuro_code.application.sessions.binding import ConversationBinding


def _now() -> datetime:
    return datetime.now(UTC)


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

    def __post_init__(self) -> None:
        if not isinstance(self.nodes, tuple):
            raise TypeError("task DAG request nodes must be a tuple")


@dataclass(frozen=True, slots=True)
class RunTaskDagRequest:
    """Explicit request to execute one previously published DAG."""

    dag_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.dag_id, str) or not self.dag_id.strip():
            raise ValueError("task DAG request id must not be empty")


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
        self._store = store
        self._dag_store = dag_store
        self._writable_service = writable_service
        self._lease_store = lease_store
        self._relay_store = relay_store
        self._parent_binding = parent_binding
        self._parent_session_id = parent_session_id
        self._clock = clock

    async def create_task_dag(self, request: CreateTaskDagRequest) -> TaskDag:
        if not isinstance(request, CreateTaskDagRequest):
            raise ValueError("task DAG creation request must be canonical")
        dag = TaskDag.create(
            dag_id=request.dag_id,
            parent_session_id=self._parent_session_id,
            nodes=request.nodes,
            created_at=self._clock().astimezone(UTC),
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
        await self._writable_service.initialize()
        dag = await self._load_required(request.dag_id)
        while True:
            if dag.state.terminal:
                return dag
            dag = await self._reconcile_active_node(dag)
            if dag.state is TaskDagState.INDETERMINATE:
                return await self._set_graph_state_if_needed(dag, TaskDagState.INDETERMINATE)
            if dag.active_node_id is not None:
                # Another process owns the one allowed serial slot.  It may
                # finish later; this invocation must not start another node.
                return dag
            dag = await self._propagate_dependencies(dag)
            if dag.state.terminal:
                return dag
            if dag.active_node_id is not None:
                return dag
            if not dag.ready_node_ids():
                return await self._classify_terminal_or_uncertain(dag)
            node = dag.node(dag.ready_node_ids()[0])
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
                    dag = await self._load_required(dag.dag_id)
                    continue
                raise ConfigurationError(f"task DAG node claim failed: {error}") from error
            claimed_node = claimed.node(node.node_id)
            identity = WritableSubagentExecutionIdentity(
                dag_id=dag.dag_id,
                node_id=node.node_id,
                parent_task_id=parent_task_id,
            )
            try:
                result = await self._writable_service.run_subagent_with_execution_identity(
                    RunWritableSubagentRequest(
                        parent_session_id=self._parent_session_id,
                        prompt=node.prompt,
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
            dag = await self._load_required(dag.dag_id)

    async def reconcile_task_dag(self, request: RunTaskDagRequest) -> TaskDag:
        """Reconcile durable evidence without starting any worker."""

        if not isinstance(request, RunTaskDagRequest):
            raise ValueError("task DAG reconciliation request must be canonical")
        dag = await self._load_required(request.dag_id)
        return await self._reconcile_active_node(dag)

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
        if dag.active_node_id is None:
            return dag
        node = dag.node(dag.active_node_id)
        if node.parent_task_id is None:
            return await self._finish_worker_node(
                dag,
                node,
                TaskDagNodeState.INDETERMINATE,
                error=RuntimeError("running DAG node has no persisted worker identity"),
            )
        await self._writable_service.reconcile_writable_subagent_workspaces()
        task = await self._store.get_session_task(self._parent_session_id, node.parent_task_id)
        lease = await self._lease_store.get_writable_subagent_lease_for_parent_task(
            self._parent_session_id,
            node.parent_task_id,
        )
        if task is None or lease is None:
            return await self._finish_worker_node(
                dag,
                node,
                TaskDagNodeState.INDETERMINATE,
                error=RuntimeError("DAG worker evidence is incomplete after restart"),
            )
        if (
            lease.parent_session_id != self._parent_session_id
            or lease.parent_task_id != node.parent_task_id
            or lease.state is WritableSubagentWorkspaceState.ORPHANED
        ):
            return await self._finish_worker_node(
                dag,
                node,
                TaskDagNodeState.INDETERMINATE,
                error=RuntimeError("DAG worker ownership evidence is indeterminate"),
            )
        if task.status is SessionTaskStatus.COMPLETED:
            if lease.state is not WritableSubagentWorkspaceState.PRESERVED:
                return await self._finish_worker_node(
                    dag,
                    node,
                    TaskDagNodeState.INDETERMINATE,
                    error=RuntimeError("completed DAG worker workspace is not preserved"),
                )
            return await self._finish_worker_node(dag, node, TaskDagNodeState.COMPLETED)
        if task.status is SessionTaskStatus.FAILED:
            return await self._finish_worker_node(
                dag,
                node,
                TaskDagNodeState.FAILED,
                error=RuntimeError("reconciled writable SessionTask failure"),
            )
        if task.status is SessionTaskStatus.CANCELLED:
            return await self._finish_worker_node(
                dag,
                node,
                TaskDagNodeState.CANCELLED,
                error=RuntimeError("reconciled writable SessionTask cancellation"),
            )
        return dag

    async def _classify_terminal_or_uncertain(self, dag: TaskDag) -> TaskDag:
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
    "TaskDagApplicationService",
    "TaskDagWritableService",
]
