"""Bounded durable task-DAG values.

Task DAGs describe orchestration metadata only.  They do not replace
``SessionTask`` or carry worker transcripts, capabilities, or workspace
contents.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from neuro_code.shared.limits import MAX_SUBAGENT_PARALLELISM

MAX_TASK_DAG_NODES = 8
MAX_TASK_DAG_EDGES = 16
MAX_TASK_DAG_NODE_DEPENDENCIES = 4
MAX_TASK_DAG_ID_BYTES = 128
MAX_TASK_DAG_NODE_ID_BYTES = 128
MAX_TASK_DAG_PROMPT_BYTES = 8 * 1024
MAX_TASK_DAG_ERROR_BYTES = 256
MAX_TASK_DAG_RESPONSE_PREVIEW_BYTES = 8 * 1024
# Compatibility name for the DAG-specific validation and schema contract.
# The value is intentionally owned by the shared application limit.
MAX_TASK_DAG_PARALLELISM = MAX_SUBAGENT_PARALLELISM

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _safe_identifier(value: str, *, field_name: str, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > limit
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be a bounded safe identifier")
    return value


def _bounded_text(value: str | None, *, field_name: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or "\x00" in value or len(value.encode("utf-8")) > limit:
        raise ValueError(f"{field_name} is not bounded")
    if any(ord(character) < 32 and character not in "\n\t\r" for character in value):
        raise ValueError(f"{field_name} contains an unsafe control character")
    return value


def _digest(value: str, *, field_name: str) -> str:
    normalized = _safe_identifier(value, field_name=field_name, limit=64).casefold()
    if _SHA256.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a SHA-256 digest")
    return normalized


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


class TaskDagNodeKind(StrEnum):
    """The only executable kind admitted by the first DAG slice."""

    WRITABLE_SUBAGENT = "writable_subagent"


class TaskDagState(StrEnum):
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INDETERMINATE = "indeterminate"

    @property
    def terminal(self) -> bool:
        return self in {
            TaskDagState.COMPLETED,
            TaskDagState.FAILED,
            TaskDagState.CANCELLED,
            TaskDagState.INDETERMINATE,
        }


class TaskDagNodeState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INDETERMINATE = "indeterminate"
    SKIPPED = "skipped"

    @property
    def terminal(self) -> bool:
        return self in {
            TaskDagNodeState.COMPLETED,
            TaskDagNodeState.FAILED,
            TaskDagNodeState.CANCELLED,
            TaskDagNodeState.INDETERMINATE,
            TaskDagNodeState.SKIPPED,
        }

    @property
    def successful(self) -> bool:
        return self is TaskDagNodeState.COMPLETED


_ALLOWED_NODE_TRANSITIONS: dict[TaskDagNodeState, frozenset[TaskDagNodeState]] = {
    TaskDagNodeState.PENDING: frozenset(
        {TaskDagNodeState.READY, TaskDagNodeState.SKIPPED, TaskDagNodeState.CANCELLED}
    ),
    TaskDagNodeState.READY: frozenset(
        {TaskDagNodeState.RUNNING, TaskDagNodeState.SKIPPED, TaskDagNodeState.CANCELLED}
    ),
    TaskDagNodeState.RUNNING: frozenset(
        {
            TaskDagNodeState.COMPLETED,
            TaskDagNodeState.FAILED,
            TaskDagNodeState.CANCELLED,
            TaskDagNodeState.INDETERMINATE,
        }
    ),
    TaskDagNodeState.COMPLETED: frozenset(),
    TaskDagNodeState.FAILED: frozenset(),
    TaskDagNodeState.CANCELLED: frozenset(),
    TaskDagNodeState.INDETERMINATE: frozenset(),
    TaskDagNodeState.SKIPPED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class TaskDagNode:
    """One immutable node definition plus its durable execution projection."""

    node_id: str
    ordinal: int
    prompt: str
    dependencies: tuple[str, ...] = ()
    kind: TaskDagNodeKind = TaskDagNodeKind.WRITABLE_SUBAGENT
    state: TaskDagNodeState = TaskDagNodeState.PENDING
    generation: int = 0
    parent_task_id: str | None = None
    execution_owner_pid: int | None = None
    execution_owner_token: str | None = None
    child_session_id: str | None = None
    lease_id: str | None = None
    worktree_id: str | None = None
    baseline_checkpoint_id: str | None = None
    relay_id: str | None = None
    error_kind: str | None = None
    error_reason: str | None = None
    response_preview: str | None = None
    final_workspace_fingerprint: str | None = None
    changed_file_count: int | None = None

    def __post_init__(self) -> None:
        _safe_identifier(
            self.node_id, field_name="task DAG node id", limit=MAX_TASK_DAG_NODE_ID_BYTES
        )
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 0:
            raise ValueError("task DAG node ordinal must be non-negative")
        if (
            not isinstance(self.prompt, str)
            or not self.prompt.strip()
            or "\x00" in self.prompt
            or len(self.prompt.encode("utf-8")) > MAX_TASK_DAG_PROMPT_BYTES
        ):
            raise ValueError("task DAG node prompt must be non-empty and bounded")
        if any(ord(character) < 32 and character not in "\n\t\r" for character in self.prompt):
            raise ValueError("task DAG node prompt contains an unsafe control character")
        if len(self.dependencies) > MAX_TASK_DAG_NODE_DEPENDENCIES:
            raise ValueError("task DAG node has too many dependencies")
        if not isinstance(self.dependencies, tuple):
            raise TypeError("task DAG node dependencies must be a tuple")
        for dependency in self.dependencies:
            _safe_identifier(
                dependency,
                field_name="task DAG dependency id",
                limit=MAX_TASK_DAG_NODE_ID_BYTES,
            )
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("task DAG node dependencies must be unique")
        if not isinstance(self.kind, TaskDagNodeKind):
            raise ValueError("task DAG node kind must be canonical")
        if not isinstance(self.state, TaskDagNodeState):
            raise ValueError("task DAG node state must be canonical")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ValueError("task DAG node generation must be non-negative")
        if self.execution_owner_pid is not None and (
            isinstance(self.execution_owner_pid, bool)
            or not isinstance(self.execution_owner_pid, int)
            or self.execution_owner_pid <= 0
        ):
            raise ValueError("task DAG execution owner pid must be positive")
        if (self.execution_owner_pid is None) != (self.execution_owner_token is None):
            raise ValueError("task DAG execution owner identity must be complete")
        if self.execution_owner_token is not None:
            _safe_identifier(
                self.execution_owner_token,
                field_name="task DAG execution owner token",
                limit=MAX_TASK_DAG_ID_BYTES,
            )
        for value, field_name in (
            (self.parent_task_id, "task DAG parent task id"),
            (self.child_session_id, "task DAG child session id"),
            (self.lease_id, "task DAG lease id"),
            (self.worktree_id, "task DAG worktree id"),
            (self.baseline_checkpoint_id, "task DAG baseline checkpoint id"),
            (self.relay_id, "task DAG relay id"),
        ):
            if value is not None:
                _safe_identifier(value, field_name=field_name, limit=MAX_TASK_DAG_ID_BYTES)
        _bounded_text(
            self.error_kind, field_name="task DAG error kind", limit=MAX_TASK_DAG_ERROR_BYTES
        )
        _bounded_text(
            self.error_reason,
            field_name="task DAG error reason",
            limit=MAX_TASK_DAG_ERROR_BYTES,
        )
        _bounded_text(
            self.response_preview,
            field_name="task DAG response preview",
            limit=MAX_TASK_DAG_RESPONSE_PREVIEW_BYTES,
        )
        if self.final_workspace_fingerprint is not None:
            _digest(
                self.final_workspace_fingerprint,
                field_name="task DAG workspace fingerprint",
            )
        if self.changed_file_count is not None and (
            isinstance(self.changed_file_count, bool)
            or not isinstance(self.changed_file_count, int)
            or self.changed_file_count < 0
        ):
            raise ValueError("task DAG changed file count must be non-negative")
        if self.state is TaskDagNodeState.RUNNING and not self.parent_task_id:
            raise ValueError("running task DAG node requires an exact parent task id")

    @property
    def prompt_fingerprint(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()

    @property
    def definition_payload(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "ordinal": self.ordinal,
            "prompt": self.prompt,
            "prompt_fingerprint": self.prompt_fingerprint,
            "dependencies": list(self.dependencies),
            "kind": self.kind.value,
        }

    @property
    def definition_fingerprint(self) -> str:
        """Stable identity of this immutable node declaration."""

        return hashlib.sha256(_canonical_json(self.definition_payload)).hexdigest()

    def can_transition_to(self, state: TaskDagNodeState) -> bool:
        return state in _ALLOWED_NODE_TRANSITIONS[self.state]


@dataclass(frozen=True, slots=True)
class TaskDag:
    """A bounded, acyclic, immutable DAG definition and lifecycle snapshot."""

    dag_id: str
    parent_session_id: str
    nodes: tuple[TaskDagNode, ...]
    state: TaskDagState = TaskDagState.READY
    generation: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    active_node_id: str | None = None
    max_parallel: int = 1

    @classmethod
    def create(
        cls,
        *,
        dag_id: str,
        parent_session_id: str,
        nodes: tuple[TaskDagNode, ...],
        created_at: datetime,
        max_parallel: int = 1,
    ) -> TaskDag:
        prepared = tuple(nodes)
        for node in prepared:
            if (
                node.state is not TaskDagNodeState.PENDING
                or node.generation != 0
                or any(
                    value is not None
                    for value in (
                        node.parent_task_id,
                        node.execution_owner_pid,
                        node.execution_owner_token,
                        node.child_session_id,
                        node.lease_id,
                        node.worktree_id,
                        node.baseline_checkpoint_id,
                        node.relay_id,
                    )
                )
            ):
                raise ValueError("new task DAG nodes must not carry execution state")
        cls._validate_definition(dag_id, parent_session_id, prepared)
        initial = tuple(
            replace(
                node,
                state=(
                    TaskDagNodeState.READY if not node.dependencies else TaskDagNodeState.PENDING
                ),
            )
            for node in prepared
        )
        return cls(
            dag_id=dag_id,
            parent_session_id=parent_session_id,
            nodes=initial,
            state=TaskDagState.READY,
            generation=0,
            created_at=created_at,
            updated_at=created_at,
            active_node_id=None,
            max_parallel=max_parallel,
        )

    def __post_init__(self) -> None:
        if not isinstance(self.nodes, tuple):
            raise TypeError("task DAG nodes must be a tuple")
        _safe_identifier(self.dag_id, field_name="task DAG id", limit=MAX_TASK_DAG_ID_BYTES)
        _safe_identifier(
            self.parent_session_id,
            field_name="task DAG parent session id",
            limit=MAX_TASK_DAG_ID_BYTES,
        )
        self._validate_definition(self.dag_id, self.parent_session_id, tuple(self.nodes))
        if not isinstance(self.state, TaskDagState):
            raise ValueError("task DAG state must be canonical")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ValueError("task DAG generation must be non-negative")
        if (
            isinstance(self.max_parallel, bool)
            or not isinstance(self.max_parallel, int)
            or not 1 <= self.max_parallel <= MAX_TASK_DAG_PARALLELISM
        ):
            raise ValueError(
                f"task DAG max_parallel must be between 1 and {MAX_TASK_DAG_PARALLELISM}"
            )
        if self.created_at is not None and (
            not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None
        ):
            raise ValueError("task DAG creation time must be timezone-aware")
        if self.updated_at is not None and (
            not isinstance(self.updated_at, datetime) or self.updated_at.tzinfo is None
        ):
            raise ValueError("task DAG update time must be timezone-aware")
        if (
            self.created_at is not None
            and self.updated_at is not None
            and self.updated_at < self.created_at
        ):
            raise ValueError("task DAG update time must not precede creation time")
        if self.active_node_id is not None:
            _safe_identifier(
                self.active_node_id,
                field_name="task DAG active node id",
                limit=MAX_TASK_DAG_NODE_ID_BYTES,
            )
            node = self.node(self.active_node_id)
            if node.state is not TaskDagNodeState.RUNNING:
                raise ValueError("task DAG active node must be running")
        if self.state.terminal and self.running_node_ids:
            raise ValueError("terminal task DAG must not contain running nodes")

    @staticmethod
    def _validate_definition(
        dag_id: str,
        parent_session_id: str,
        nodes: tuple[TaskDagNode, ...],
    ) -> None:
        _safe_identifier(dag_id, field_name="task DAG id", limit=MAX_TASK_DAG_ID_BYTES)
        _safe_identifier(
            parent_session_id,
            field_name="task DAG parent session id",
            limit=MAX_TASK_DAG_ID_BYTES,
        )
        if not nodes:
            raise ValueError("task DAG must contain at least one node")
        if len(nodes) > MAX_TASK_DAG_NODES:
            raise ValueError("task DAG contains too many nodes")
        ids = [node.node_id for node in nodes]
        if len(set(ids)) != len(ids):
            raise ValueError("task DAG node ids must be unique")
        if tuple(node.ordinal for node in nodes) != tuple(range(len(nodes))):
            raise ValueError("task DAG node ordinals must match declaration order")
        known = set(ids)
        edge_count = 0
        for node in nodes:
            unknown = set(node.dependencies) - known
            if unknown:
                raise ValueError(f"task DAG node has unknown dependencies: {sorted(unknown)!r}")
            if node.node_id in node.dependencies:
                raise ValueError("task DAG node cannot depend on itself")
            edge_count += len(node.dependencies)
        if edge_count > MAX_TASK_DAG_EDGES:
            raise ValueError("task DAG contains too many edges")
        indegree = {node.node_id: len(node.dependencies) for node in nodes}
        outgoing: dict[str, list[str]] = {node.node_id: [] for node in nodes}
        for node in nodes:
            for dependency in node.dependencies:
                outgoing[dependency].append(node.node_id)
        ready = sorted(
            (node for node in nodes if indegree[node.node_id] == 0),
            key=lambda item: (item.ordinal, item.node_id),
        )
        visited = 0
        while ready:
            current = ready.pop(0)
            visited += 1
            for dependent in sorted(
                outgoing[current.node_id],
                key=lambda node_id: next(node.ordinal for node in nodes if node.node_id == node_id),
            ):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(next(node for node in nodes if node.node_id == dependent))
                    ready.sort(key=lambda item: (item.ordinal, item.node_id))
        if visited != len(nodes):
            raise ValueError("task DAG dependencies contain a cycle")

    @property
    def definition_fingerprint(self) -> str:
        payload = {
            "dag_id": self.dag_id,
            "parent_session_id": self.parent_session_id,
            "nodes": [node.definition_payload for node in self.nodes],
        }
        return hashlib.sha256(_canonical_json(payload)).hexdigest()

    def node(self, node_id: str) -> TaskDagNode:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(node_id)

    def topological_order(self) -> tuple[str, ...]:
        remaining = {node.node_id: set(node.dependencies) for node in self.nodes}
        ordered: list[str] = []
        by_id = {node.node_id: node for node in self.nodes}
        while remaining:
            ready = sorted(
                (by_id[node_id] for node_id, dependencies in remaining.items() if not dependencies),
                key=lambda item: (item.ordinal, item.node_id),
            )
            if not ready:
                raise ValueError("task DAG dependencies contain a cycle")
            for node in ready:
                ordered.append(node.node_id)
                remaining.pop(node.node_id)
                for dependencies in remaining.values():
                    dependencies.discard(node.node_id)
        return tuple(ordered)

    def ready_node_ids(self) -> tuple[str, ...]:
        return tuple(
            node.node_id
            for node in sorted(self.nodes, key=lambda item: (item.ordinal, item.node_id))
            if node.state is TaskDagNodeState.READY
        )

    @property
    def running_node_ids(self) -> tuple[str, ...]:
        """Return the durable execution set in deterministic declaration order.

        ``active_node_id`` is retained for schema and serialized-Leader
        compatibility only.  Scheduling decisions must use this derived set.
        """

        return tuple(
            node.node_id
            for node in sorted(self.nodes, key=lambda item: (item.ordinal, item.node_id))
            if node.state is TaskDagNodeState.RUNNING
        )

    def with_nodes(self, nodes: tuple[TaskDagNode, ...]) -> TaskDag:
        return replace(self, nodes=nodes)

    def node_states(self) -> Mapping[str, TaskDagNodeState]:
        return {node.node_id: node.state for node in self.nodes}


__all__ = [
    "MAX_TASK_DAG_EDGES",
    "MAX_TASK_DAG_ERROR_BYTES",
    "MAX_TASK_DAG_ID_BYTES",
    "MAX_TASK_DAG_NODES",
    "MAX_TASK_DAG_NODE_DEPENDENCIES",
    "MAX_TASK_DAG_NODE_ID_BYTES",
    "MAX_TASK_DAG_PARALLELISM",
    "MAX_TASK_DAG_PROMPT_BYTES",
    "MAX_TASK_DAG_RESPONSE_PREVIEW_BYTES",
    "TaskDag",
    "TaskDagNode",
    "TaskDagNodeKind",
    "TaskDagNodeState",
    "TaskDagState",
]
