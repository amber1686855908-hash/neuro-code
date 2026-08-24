"""Project and durably publish completed direct Task DAG result evidence."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from neuro_code.application.ports.parent_context_relay import ParentContextRelayStore
from neuro_code.application.ports.task_dag import TaskDagStore
from neuro_code.application.ports.task_dag_result_relay import (
    TaskDagDependencyResultRelayError,
    TaskDagDependencyResultRelayStore,
)
from neuro_code.application.ports.writable_subagent import WritableSubagentLeaseStore
from neuro_code.domain.task_dag import TaskDag, TaskDagNode, TaskDagNodeState
from neuro_code.domain.task_dag_result_relay import (
    MAX_TASK_DAG_RESULT_RELAY_ITEM_BYTES,
    TaskDagDependencyResultEntry,
    TaskDagDependencyResultRelay,
)
from neuro_code.domain.writable_subagent import (
    WritableSubagentWorkspaceLease,
    WritableSubagentWorkspaceState,
)
from neuro_code.shared.errors import ConfigurationError
from neuro_code.shared.redaction import redact_sensitive_text


def _now() -> datetime:
    return datetime.now(UTC)


def _bounded_result_text(value: str, *, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    suffix = "..."
    prefix = encoded[: max(0, limit - len(suffix))].decode("utf-8", errors="ignore")
    return f"{prefix}{suffix}"[:limit], True


class TaskDagDependencyResultRelayApplicationService:
    """Own the safe projection boundary for one target execution.

    The service reads only the current immutable DAG definition, completed
    node projections, writable leases, and existing Parent Relays.  It never
    reads a child transcript or workspace and never mutates a predecessor.
    """

    __slots__ = (
        "_clock",
        "_dag_store",
        "_lease_store",
        "_parent_relay_store",
        "_parent_session_id",
        "_redaction_values",
        "_relay_store",
    )

    def __init__(
        self,
        dag_store: TaskDagStore,
        relay_store: TaskDagDependencyResultRelayStore,
        lease_store: WritableSubagentLeaseStore,
        parent_relay_store: ParentContextRelayStore,
        *,
        parent_session_id: str,
        redaction_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _now,
    ) -> None:
        if not isinstance(parent_session_id, str) or not parent_session_id.strip():
            raise ConfigurationError("DAG dependency relay parent session is missing")
        self._dag_store = dag_store
        self._relay_store = relay_store
        self._lease_store = lease_store
        self._parent_relay_store = parent_relay_store
        self._parent_session_id = parent_session_id
        self._redaction_values = tuple(redaction_values)
        self._clock = clock

    async def initialize(self) -> None:
        await self._relay_store.initialize()

    async def publish_for_target(
        self,
        dag: TaskDag,
        target: TaskDagNode,
    ) -> TaskDagDependencyResultRelay | None:
        """Publish or reuse the immutable relay for a claimed target.

        A root node has no dataflow context.  Dependent nodes must have every
        direct predecessor in ``COMPLETED`` state before this method can
        return a relay.  The durable store performs the second identity check
        under its write lock, so a stale controller cannot publish for a newer
        target generation.
        """

        if not isinstance(dag, TaskDag) or not isinstance(target, TaskDagNode):
            raise ConfigurationError("DAG dependency relay inputs must be canonical")
        if not target.dependencies:
            return None
        if target.state is not TaskDagNodeState.RUNNING:
            raise ConfigurationError("DAG dependency relay target must be RUNNING")
        current = await self._dag_store.get_task_dag(dag.dag_id)
        if current is None:
            raise ConfigurationError("DAG dependency relay target DAG is missing")
        try:
            existing = await self._relay_store.get_task_dag_dependency_relay_for_target(
                current.dag_id,
                target.node_id,
                target.generation,
            )
        except TaskDagDependencyResultRelayError as error:
            raise ConfigurationError(
                f"DAG dependency relay integrity verification failed: {error}"
            ) from error
        if existing is not None:
            self._verify_relay_identity(existing, current, target)
            return await self._reload_verified(existing)
        self._verify_target(current, target)

        entries: list[TaskDagDependencyResultEntry] = []
        truncated = False
        for predecessor_id in target.dependencies:
            predecessor = current.node(predecessor_id)
            entry = await self._project_predecessor(predecessor)
            entries.append(entry)
            truncated = truncated or entry.truncated
        relay = TaskDagDependencyResultRelay.create(
            relay_id=f"tdr-{uuid.uuid4().hex}",
            dag_id=current.dag_id,
            dag_definition_fingerprint=current.definition_fingerprint,
            target_node_id=target.node_id,
            target_node_generation=target.generation,
            target_node_definition_fingerprint=target.definition_fingerprint,
            direct_dependency_ids=target.dependencies,
            entries=tuple(entries),
            truncated=truncated,
            created_at=self._clock().astimezone(UTC),
        )
        try:
            published = await self._relay_store.insert_task_dag_dependency_relay(relay)
            verified = await self._relay_store.get_task_dag_dependency_relay_for_target(
                current.dag_id,
                target.node_id,
                target.generation,
            )
        except TaskDagDependencyResultRelayError as error:
            raise ConfigurationError(f"DAG dependency relay publication failed: {error}") from error
        if verified is None or published.publication_payload != verified.publication_payload:
            raise ConfigurationError("DAG dependency relay durability could not be verified")
        self._verify_relay(verified, current, target)
        return await self._reload_verified(verified)

    async def _reload_verified(
        self,
        relay: TaskDagDependencyResultRelay,
    ) -> TaskDagDependencyResultRelay:
        try:
            reloaded = await self._relay_store.get_task_dag_dependency_relay(relay.relay_id)
        except TaskDagDependencyResultRelayError as error:
            raise ConfigurationError(
                f"DAG dependency relay integrity verification failed: {error}"
            ) from error
        if reloaded is None or reloaded != relay:
            raise ConfigurationError("DAG dependency relay reload did not match publication")
        return reloaded

    def _verify_target(self, dag: TaskDag, target: TaskDagNode) -> None:
        if dag.parent_session_id != self._parent_session_id:
            raise ConfigurationError("DAG dependency relay parent does not match the binding")
        current = dag.node(target.node_id)
        if (
            current.state is not TaskDagNodeState.RUNNING
            or dag.active_node_id != target.node_id
            or current.generation != target.generation
            or current.definition_fingerprint != target.definition_fingerprint
        ):
            raise ConfigurationError("DAG dependency relay target snapshot is stale")

    def _verify_relay(
        self,
        relay: TaskDagDependencyResultRelay,
        dag: TaskDag,
        target: TaskDagNode,
    ) -> None:
        self._verify_target(dag, target)
        self._verify_relay_identity(relay, dag, target)

    def _verify_relay_identity(
        self,
        relay: TaskDagDependencyResultRelay,
        dag: TaskDag,
        target: TaskDagNode,
    ) -> None:
        if (
            dag.parent_session_id != self._parent_session_id
            or relay.dag_id != dag.dag_id
            or relay.dag_definition_fingerprint != dag.definition_fingerprint
            or relay.target_node_id != target.node_id
            or relay.target_node_generation != target.generation
            or relay.target_node_definition_fingerprint != target.definition_fingerprint
            or relay.direct_dependency_ids != target.dependencies
        ):
            raise ConfigurationError("DAG dependency relay identity does not match the target")
        if tuple(entry.predecessor_node_id for entry in relay.entries) != target.dependencies:
            raise ConfigurationError("DAG dependency relay predecessor set is not exact")

    async def _project_predecessor(
        self,
        predecessor: TaskDagNode,
    ) -> TaskDagDependencyResultEntry:
        if predecessor.state is not TaskDagNodeState.COMPLETED:
            raise ConfigurationError(
                f"DAG dependency relay predecessor {predecessor.node_id} is not COMPLETED"
            )
        if (
            predecessor.parent_task_id is None
            or predecessor.child_session_id is None
            or predecessor.lease_id is None
            or predecessor.worktree_id is None
            or predecessor.baseline_checkpoint_id is None
            or predecessor.relay_id is None
        ):
            raise ConfigurationError(
                f"DAG dependency relay predecessor {predecessor.node_id} evidence is incomplete"
            )
        lease = await self._lease_store.get_writable_subagent_lease_for_parent_task(
            self._parent_session_id,
            predecessor.parent_task_id,
        )
        if not isinstance(lease, WritableSubagentWorkspaceLease):
            raise ConfigurationError(
                f"DAG dependency relay predecessor {predecessor.node_id} lease is missing"
            )
        if (
            lease.parent_session_id != self._parent_session_id
            or lease.parent_task_id != predecessor.parent_task_id
            or lease.lease_id != predecessor.lease_id
            or lease.child_session_id != predecessor.child_session_id
            or lease.worktree_id.value != predecessor.worktree_id
            or lease.baseline_checkpoint_id is None
            or lease.baseline_checkpoint_id.value != predecessor.baseline_checkpoint_id
            or lease.state is not WritableSubagentWorkspaceState.PRESERVED
        ):
            raise ConfigurationError(
                f"DAG dependency relay predecessor {predecessor.node_id} lease evidence is stale"
            )
        parent_relay = await self._parent_relay_store.get_parent_context_relay_for_lease(
            predecessor.lease_id
        )
        if parent_relay is None or parent_relay.relay_id != predecessor.relay_id:
            raise ConfigurationError(
                f"DAG dependency relay predecessor {predecessor.node_id} Parent Relay is missing"
            )
        safe = redact_sensitive_text(
            predecessor.response_preview or "",
            explicit_values=self._redaction_values,
        )
        result_text, was_truncated = _bounded_result_text(
            safe,
            limit=MAX_TASK_DAG_RESULT_RELAY_ITEM_BYTES,
        )
        return TaskDagDependencyResultEntry(
            predecessor_node_id=predecessor.node_id,
            predecessor_ordinal=predecessor.ordinal,
            predecessor_generation=predecessor.generation,
            predecessor_state=predecessor.state,
            parent_task_id=predecessor.parent_task_id,
            child_session_id=predecessor.child_session_id,
            writable_lease_id=predecessor.lease_id,
            worktree_id=lease.worktree_id,
            baseline_checkpoint_id=lease.baseline_checkpoint_id,
            parent_relay_id=parent_relay.relay_id,
            final_workspace_fingerprint=lease.final_workspace_fingerprint,
            changed_file_count=lease.changed_file_count,
            result_text=result_text,
            truncated=was_truncated,
        )


__all__ = ["TaskDagDependencyResultRelayApplicationService"]
